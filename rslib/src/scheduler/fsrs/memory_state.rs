// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::collections::HashSet;

use anki_proto::scheduler::ComputeMemoryStateResponse;
use fsrs::FSRSItem;
use fsrs::MemoryState;
use fsrs::NextStates;
use fsrs::DEFAULT_PARAMETERS;
use fsrs::FSRS;
use itertools::Either;
use itertools::Itertools;

use super::curve::Fsrs7Curve;
use super::rescheduler::rescheduled_interval_days;
use super::rescheduler::Rescheduler;
use crate::card::CardQueue;
use crate::card::CardType;
use crate::card::FsrsMemoryState;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::scheduler::answering::get_fuzz_seed;
use crate::scheduler::fsrs::params::ignore_revlogs_before_ms_from_config;
use crate::scheduler::fsrs::params::reviews_for_fsrs;
use crate::scheduler::fsrs::params::Params;
use crate::scheduler::fsrs::params_fingerprint;
use crate::scheduler::fsrs::round_to_two_decimals;
use crate::scheduler::fsrs::HISTORICAL_RETENTION;
use crate::scheduler::states::fuzz::ReviewFuzzConfig;
use crate::scheduler::timing::SchedTimingToday;
use crate::search::Negated;
use crate::search::Node;
use crate::search::SearchNode;
use crate::search::StateKind;
use crate::storage::comma_separated_ids;

#[cfg(test)]
const S_MIN: f32 = 0.0001;

#[derive(Debug, Clone, Default)]
pub struct ComputeMemoryProgress {
    pub current_cards: u32,
    pub total_cards: u32,
    pub preset_name: String,
    pub current_preset: u32,
    pub total_presets: u32,
    pub rescheduling: bool,
    pub saving: bool,
    pub presets: Vec<ComputeMemoryPresetProgress>,
}

#[derive(Debug, Clone, Default)]
pub struct ComputeMemoryPresetProgress {
    pub name: String,
    pub current_cards: u32,
    pub total_cards: u32,
    pub finished: bool,
    pub rescheduling: bool,
    pub saving: bool,
}

struct ComputedMemoryState {
    memory_state: Option<FsrsMemoryState>,
    desired_retention: f32,
    decay: f32,
}

#[derive(Debug, Clone, PartialEq)]
pub struct FsrsDesiredRetentionForInterval {
    pub interval_target_desired_retention: f32,
}

/// The single decay value stored on cards (`card.decay`, read by other
/// clients and custom scheduling). FSRS-7 uses a mixture curve; this is its
/// first decay component, of the parameters the preset runs with (spec
/// sched.fsrs7-only).
pub(crate) fn get_decay_from_params(params: &[f32]) -> f32 {
    crate::deckconfig::effective_fsrs7_params(params)[23]
}

pub(crate) fn fsrs_current_retrievability_for_params(
    params: &[f32],
    stability: f32,
    elapsed_days: f32,
) -> Result<f32> {
    fsrs_current_retrievability_scalar_for_params(params, stability, elapsed_days)
}

/// Calculate retrievability from the complete stored FSRS state.
///
/// FSRS-7's forgetting curve depends on both stability traces and difficulty,
/// so built-in callers must use this instead of the scalar compatibility
/// helper below.
pub(crate) fn fsrs_current_retrievability_for_state(
    params: &[f32],
    state: FsrsMemoryState,
    elapsed_days: f32,
) -> Result<f32> {
    fsrs_current_retrievability_for_memory_state(params, state.into(), elapsed_days)
}

/// `FSRS::new(params)?.current_retrievability(..)`, through the bit-identical
/// scalar curve when it covers the input (see [`Fsrs7Curve`]).
fn fsrs_current_retrievability_for_memory_state(
    params: &[f32],
    state: MemoryState,
    elapsed_days: f32,
) -> Result<f32> {
    let elapsed_days = elapsed_days.max(0.0);
    let retrievability =
        match Fsrs7Curve::new(params).and_then(|curve| curve.retrievability(state, elapsed_days)) {
            Some(retrievability) => retrievability,
            None => FSRS::new(params)?.current_retrievability(state, elapsed_days),
        };
    require!(retrievability.is_finite(), "invalid FSRS parameter values");
    Ok(retrievability)
}

/// Return the negative fraction of the target interval that has elapsed.
///
/// The FSRS-7 mixture curve has no scalar decay that can be inverted in SQL,
/// so the target interval must be derived from the complete memory state.
pub(crate) fn fsrs_relative_overdueness_for_state(
    params: &[f32],
    state: FsrsMemoryState,
    elapsed_days: f32,
    target_retrievability: f32,
) -> Result<f32> {
    FsrsCurveModel::new(params).relative_overdueness(state, elapsed_days, target_retrievability)
}

/// One parameter set's forgetting curve, for callers that need it for many
/// cards (the queue's retrievability orders): [`Fsrs7Curve`] where it covers
/// the input, else the fsrs crate's model, made when first needed. The
/// results are the bits of `FSRS::new(params)`'s.
pub(crate) struct FsrsCurveModel {
    params: Vec<f32>,
    curve: Option<Fsrs7Curve>,
    fsrs: Option<FSRS>,
}

impl FsrsCurveModel {
    pub(crate) fn new(params: &[f32]) -> Self {
        Self {
            params: params.to_vec(),
            curve: Fsrs7Curve::new(params),
            fsrs: None,
        }
    }

    fn fsrs(&mut self) -> Result<&FSRS> {
        if self.fsrs.is_none() {
            self.fsrs = Some(FSRS::new(&self.params)?);
        }
        Ok(self.fsrs.as_ref().unwrap())
    }

    /// `fsrs_current_retrievability_for_state` of these parameters.
    pub(crate) fn current_retrievability(
        &mut self,
        state: FsrsMemoryState,
        elapsed_days: f32,
    ) -> Result<f32> {
        let state = state.into();
        let elapsed_days = elapsed_days.max(0.0);
        let retrievability = match self
            .curve
            .as_ref()
            .and_then(|curve| curve.retrievability(state, elapsed_days))
        {
            Some(retrievability) => retrievability,
            None => self.fsrs()?.current_retrievability(state, elapsed_days),
        };
        require!(retrievability.is_finite(), "invalid FSRS parameter values");
        Ok(retrievability)
    }

    /// `fsrs_relative_overdueness_for_state` of these parameters.
    pub(crate) fn relative_overdueness(
        &mut self,
        state: FsrsMemoryState,
        elapsed_days: f32,
        target_retrievability: f32,
    ) -> Result<f32> {
        let state = state.into();
        let target_retrievability = target_retrievability.clamp(0.0001, 0.9999);
        let target_interval = match &self.curve {
            Some(curve) => curve.interval_at_retrievability(state, target_retrievability),
            None => self
                .fsrs()?
                .interval_at_retrievability(state, target_retrievability),
        };
        let relative_overdueness = -elapsed_days.max(0.0) / target_interval.max(0.0001);
        require!(
            relative_overdueness.is_finite(),
            "invalid FSRS parameter values"
        );
        Ok(relative_overdueness)
    }
}

/// Scalar compatibility helper for callers that do not have a complete
/// FSRS-7 state. It assumes difficulty 5 and equal slow/fast stability.
pub(crate) fn fsrs_current_retrievability_scalar_for_params(
    params: &[f32],
    stability: f32,
    elapsed_days: f32,
) -> Result<f32> {
    fsrs_current_retrievability_for_memory_state(
        params,
        MemoryState {
            stability,
            difficulty: 5.0,
            stability_fast: stability,
        },
        elapsed_days,
    )
}

/// The interval at `desired_retention` of the FSRS-7 state whose S90 is
/// `s90` (spec sched.fsrs7-sm2-conversion): the `FsrsNextInterval` add-on API
/// is given the S90 a card shows, not FSRS-7's internal stability.
pub(crate) fn fsrs_next_interval_for_s90(
    params: &[f32],
    s90: f32,
    desired_retention: f32,
) -> Result<f32> {
    let fsrs = FSRS::new(params)?;
    let state = memory_state_from_sm2_with_params(&fsrs, params, 2.5, s90, 0.9)?;
    Ok(fsrs.next_interval_for_state(state, desired_retention.clamp(0.0001, 0.9999)))
}

pub(crate) fn fsrs_interval_at_retrievability_for_params(
    params: &[f32],
    stability: f32,
    target_retrievability: f32,
) -> Result<f32> {
    let fsrs = FSRS::new(params)?;
    Ok(fsrs.interval_at_retrievability(
        MemoryState {
            stability,
            difficulty: 5.0,
            stability_fast: stability,
        },
        target_retrievability.clamp(0.0001, 0.9999),
    ))
}

pub(crate) fn fsrs_memory_state_for_params(
    params: &[f32],
    memory_state: MemoryState,
) -> Result<FsrsMemoryState> {
    let fsrs = FSRS::new(params)?;
    Ok(fsrs_memory_state_for_fsrs(&fsrs, memory_state))
}

/// The S90 of each of FSRS's next states: Again, Hard, Good, Easy.
pub(crate) fn fsrs_next_states_s90(fsrs: &FSRS, states: &NextStates) -> [f32; 4] {
    [&states.again, &states.hard, &states.good, &states.easy]
        .map(|state| fsrs.interval_at_retrievability(state.memory, 0.9))
}

pub(crate) fn fsrs_memory_state_for_fsrs(
    fsrs: &FSRS,
    memory_state: MemoryState,
) -> FsrsMemoryState {
    let stability = fsrs.interval_at_retrievability(memory_state, 0.9);
    FsrsMemoryState {
        stability,
        stability_internal: memory_state.stability,
        stability_fast: Some(memory_state.stability_fast),
        difficulty: memory_state.difficulty,
    }
}

/// Compute memory state from SM-2 fields: the state whose forgetting curve
/// reaches `sm2_retention` at `interval` days (spec
/// sched.fsrs7-sm2-conversion).
///
/// The fsrs crate's conversion gives the shape of the state (difficulty 5,
/// fast stability 0.8 of the internal one). Its FSRS-7 version puts the
/// interval into the internal stability, which is not the interval at 90%
/// recall of FSRS-7's two-component curve (a 100-day interval gave an S90 of
/// about 226 days), so the state is then scaled to the interval.
pub(crate) fn memory_state_from_sm2_with_params(
    fsrs: &FSRS,
    _params: &[f32],
    ease_factor: f32,
    interval: f32,
    sm2_retention: f32,
) -> Result<MemoryState> {
    let shape = fsrs.memory_state_from_sm2(ease_factor, interval, sm2_retention)?;
    Ok(scale_state_to_interval(
        fsrs,
        shape,
        interval,
        sm2_retention,
    ))
}

/// The FSRS-7 memory state whose S90 is `s90`, for a card that has only an
/// S90 (RWKV-Curve's, spec sched.fsrs7-sm2-conversion): the conversion of
/// an interval scheduled at 90% retention.
pub(crate) fn fsrs_memory_state_for_s90(params: &[f32], s90: f32) -> Result<FsrsMemoryState> {
    let fsrs = FSRS::new(params)?;
    let state = memory_state_from_sm2_with_params(&fsrs, params, 2.5, s90, 0.9)?;
    Ok(FsrsMemoryState {
        stability: s90,
        stability_internal: state.stability,
        stability_fast: Some(state.stability_fast),
        difficulty: state.difficulty,
    })
}

/// The FSRS-7 memory state with `difficulty` whose S90 is `s90`, for a card
/// another client wrote that has no usable review log (spec
/// sync.fsrs7-state-of-foreign-cards): the fast/internal stability ratio of
/// the crate's interval conversion, scaled to the S90. None when the crate
/// cannot convert the S90.
pub(crate) fn fsrs_memory_state_for_s90_and_difficulty(
    fsrs: &FSRS,
    s90: f32,
    difficulty: f32,
) -> Option<FsrsMemoryState> {
    let shape = fsrs.memory_state_from_sm2(2.5, s90, 0.9).ok()?;
    let shape = MemoryState {
        difficulty: difficulty.clamp(1.0, 10.0),
        ..shape
    };
    let state = scale_state_to_interval(fsrs, shape, s90, 0.9);
    Some(FsrsMemoryState {
        stability: s90,
        stability_internal: state.stability,
        stability_fast: Some(state.stability_fast),
        difficulty: state.difficulty,
    })
}

/// The fsrs crate's stability bounds.
const STABILITY_MIN: f32 = 0.0001;
const STABILITY_MAX: f32 = 36500.0;

/// The state with the difficulty and the fast/internal stability ratio of
/// `shape` whose forgetting curve reaches `retention` at `interval` days.
///
/// The interval at a given retention grows with the internal stability, so a
/// secant search on the log of the stability, kept inside a bracket that
/// shrinks at every step (a step that leaves it is a bisection), finds the
/// scale. A target the curve cannot reach gives the nearest bound.
pub(crate) fn scale_state_to_interval(
    fsrs: &FSRS,
    shape: MemoryState,
    interval: f32,
    retention: f32,
) -> MemoryState {
    const TOLERANCE: f64 = 1e-5;
    const MAX_STEPS: usize = 64;
    let ratio = shape.stability_fast / shape.stability;
    if !(ratio.is_finite()
        && ratio > 0.0
        && interval.is_finite()
        && interval > 0.0
        && retention > 0.0
        && retention < 1.0)
    {
        return shape;
    }
    let target = (interval.clamp(STABILITY_MIN, STABILITY_MAX) as f64).ln();
    let state_at = |log_stability: f64| {
        let stability = (log_stability.exp() as f32).clamp(STABILITY_MIN, STABILITY_MAX);
        MemoryState {
            stability,
            stability_fast: (stability * ratio).clamp(STABILITY_MIN, STABILITY_MAX),
            difficulty: shape.difficulty,
        }
    };
    let error_at = |log_stability: f64| {
        let reached = fsrs.interval_at_retrievability(state_at(log_stability), retention);
        (reached.max(f32::MIN_POSITIVE) as f64).ln() - target
    };
    let mut low = (STABILITY_MIN as f64).ln();
    let mut high = (STABILITY_MAX as f64).ln();
    let mut x = (shape.stability.clamp(STABILITY_MIN, STABILITY_MAX) as f64).ln();
    let mut error = error_at(x);
    let mut previous: Option<(f64, f64)> = None;
    for _ in 0..MAX_STEPS {
        if !error.is_finite() || error.abs() <= TOLERANCE {
            break;
        }
        if error < 0.0 {
            low = x;
        } else {
            high = x;
        }
        let mut next = match previous {
            Some((previous_x, previous_error)) if error != previous_error => {
                x - error * (x - previous_x) / (error - previous_error)
            }
            _ => x - error,
        };
        if !(next > low && next < high) {
            next = 0.5 * (low + high);
        }
        previous = Some((x, error));
        x = next;
        error = error_at(x);
    }
    if error.is_finite() {
        state_at(x)
    } else {
        shape
    }
}

#[derive(Debug)]
pub(crate) struct UpdateMemoryStateRequest {
    pub params: Params,
    pub preset_desired_retention: f32,
    pub historical_retention: f32,
    pub max_interval: u32,
    pub review_fuzz_config: ReviewFuzzConfig,
    pub reschedule: bool,
    pub deck_desired_retention: HashMap<DeckId, f32>,
    /// The preset runs RWKV-Curve: a card keeps the S90 stored in its memory
    /// state, which RWKV-Curve wrote; only the FSRS-7 fields are computed
    /// again (spec sched.rwkv-curve-s90-kept).
    pub keep_stability: bool,
}

pub(crate) struct UpdateMemoryStateEntry {
    pub req: Option<UpdateMemoryStateRequest>,
    pub search: Node,
    pub ignore_before: TimestampMillis,
    pub preset_name: String,
    pub current_preset: u32,
    pub total_presets: u32,
}

trait ChunkIntoVecs<T> {
    fn chunk_into_vecs(&mut self, chunk_size: usize) -> impl Iterator<Item = Vec<T>>;
}

impl<T> ChunkIntoVecs<T> for Vec<T> {
    fn chunk_into_vecs(&mut self, chunk_size: usize) -> impl Iterator<Item = Vec<T>> {
        std::iter::from_fn(move || {
            (!self.is_empty()).then(|| self.drain(..chunk_size.min(self.len())).collect())
        })
    }
}

/// What differed between a locally modified card row and the row the server
/// sent for it, recorded while a sync chunk was merged. The post-sync
/// reconcile pass uses it to decide how much of the card to rebuild.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct FsrsSyncConflict {
    /// `interval`, `due` or `original_due` differed. Deck, type and queue
    /// changes alone do not count: a card that only moved deck must keep its
    /// schedule.
    pub(crate) schedule_differs: bool,
    /// Both rows carried the same memory state (or both had none).
    pub(crate) memory_state_agreed: bool,
    /// Both rows carried the same last review time (or both had none).
    pub(crate) last_review_time_agreed: bool,
}

impl FsrsSyncConflict {
    /// Fold a second observation of the same card into this one.
    pub(crate) fn merge(&mut self, other: FsrsSyncConflict) {
        self.schedule_differs |= other.schedule_differs;
        self.memory_state_agreed &= other.memory_state_agreed;
        self.last_review_time_agreed &= other.last_review_time_agreed;
    }
}

impl Collection {
    /// For each provided set of params, locate cards with the provided search,
    /// and update their memory state.
    /// Should be called inside a transaction.
    /// If Params are None, it means the user disabled FSRS, and the existing
    /// memory state should be removed.
    pub(crate) fn update_memory_state(
        &mut self,
        entries: Vec<UpdateMemoryStateEntry>,
    ) -> Result<()> {
        let timing = self.timing_today()?;
        let usn = self.usn()?;
        let mut preset_progress = entries
            .iter()
            .map(|entry| ComputeMemoryPresetProgress {
                name: entry.preset_name.clone(),
                rescheduling: entry.req.as_ref().is_some_and(|req| req.reschedule),
                ..Default::default()
            })
            .collect_vec();

        for (
            entry_index,
            UpdateMemoryStateEntry {
                req,
                search,
                ignore_before,
                preset_name,
                current_preset,
                total_presets,
            },
        ) in entries.into_iter().enumerate()
        {
            let search = SearchBuilder::all([search, SearchNode::State(StateKind::New).negated()]);
            let revlog = self.revlog_for_srs(search)?;

            let Some(req) = &req else {
                let items = fsrs_items_for_memory_states(
                    &FSRS::new(&DEFAULT_PARAMETERS)?,
                    &DEFAULT_PARAMETERS,
                    revlog,
                    0.9,
                    ignore_before,
                )?;

                Self::prepare_memory_progress_entry(&mut preset_progress, entry_index, items.len());
                let on_updated_card = self.create_progress_closure(
                    preset_progress.clone(),
                    entry_index,
                    preset_name.clone(),
                    current_preset,
                    total_presets,
                    false,
                )?;

                // clear FSRS data if FSRS is disabled
                self.clear_fsrs_data_for_cards(
                    items.iter().map(|(card_id, _)| *card_id),
                    usn,
                    on_updated_card,
                )?;
                Self::finish_memory_progress_entry(&mut preset_progress, entry_index, items.len());
                continue;
            };
            let fsrs = FSRS::new(&req.params)?;
            let params = &req.params[..];
            let last_revlog_info = req.reschedule.then(|| get_last_revlog_info(&revlog));
            let keep_stability = req.keep_stability;
            let items = fsrs_items_for_memory_states(
                &fsrs,
                params,
                revlog,
                req.historical_retention,
                ignore_before,
            )?;

            Self::prepare_memory_progress_entry(&mut preset_progress, entry_index, items.len());
            let mut on_updated_card = self.create_progress_closure(
                preset_progress.clone(),
                entry_index,
                preset_name,
                current_preset,
                total_presets,
                req.reschedule,
            )?;
            let item_count = items.len();

            let (items, cards_without_items): (Vec<(CardId, FsrsItemForMemoryState)>, Vec<CardId>) =
                items.into_iter().partition_map(|(card_id, item)| {
                    if let Some(item) = item {
                        Either::Left((card_id, item))
                    } else {
                        Either::Right(card_id)
                    }
                });

            let decay = get_decay_from_params(&req.params);

            // Store decay and desired retention in the card so that add-ons, card info,
            // stats and browser search/sorts don't need to access the deck config.
            // Unlike memory states, scheduler doesn't use decay and dr stored in the card.
            let set_decay_and_desired_retention = move |card: &mut Card| {
                let deck_id = card.original_or_current_deck_id();

                let desired_retention = *req
                    .deck_desired_retention
                    .get(&deck_id)
                    .unwrap_or(&req.preset_desired_retention);

                card.desired_retention = Some(desired_retention);
                card.decay = Some(decay);
            };

            self.update_memory_state_for_itemless_cards(
                cards_without_items,
                set_decay_and_desired_retention,
                usn,
                &mut on_updated_card,
            )?;

            let mut rescheduler =
                if req.reschedule && self.get_config_bool(BoolKey::LoadBalancerEnabled) {
                    Some(Rescheduler::new(self)?)
                } else {
                    None
                };

            let reschedule =
                move |card: &mut Card, collection: &mut Self, fsrs: &FSRS| -> Result<()> {
                    // we are rescheduling
                    let Some(last_revlog_info) = &last_revlog_info else {
                        return Ok(());
                    };

                    // we have a last review time for the card
                    let Some(last_info) = last_revlog_info.get(&card.id) else {
                        return Ok(());
                    };
                    let Some(last_review) = &last_info.last_reviewed_at else {
                        return Ok(());
                    };
                    // the card isn't in (re)learning or suspended
                    if !(card.ctype == CardType::Review && card.queue != CardQueue::Suspended) {
                        return Ok(());
                    };

                    let deck = collection
                        .get_deck(card.original_or_current_deck_id())?
                        .or_not_found(card.original_or_current_deck_id())?;
                    let deckconfig_id = deck.config_id().unwrap();
                    // reschedule it
                    let days_elapsed = timing.next_day_at.elapsed_days_since(*last_review) as i32;
                    let previous_interval = last_info.previous_interval.unwrap_or(0);
                    // the card's whole FSRS-7 state (internal and fast
                    // stability, difficulty), not its S90 as a lone stability
                    let interval = fsrs.next_interval_for_state(
                        card.memory_state
                            .expect("We set it before this function is called")
                            .into(),
                        card.desired_retention
                            .expect("We set it before this function is called"),
                    );
                    card.interval = rescheduled_interval_days(
                        rescheduler.as_ref(),
                        interval,
                        previous_interval,
                        req.max_interval,
                        days_elapsed as u32,
                        deckconfig_id,
                        get_fuzz_seed(card, true),
                        req.review_fuzz_config,
                    );
                    let due = if card.original_due != 0 {
                        &mut card.original_due
                    } else {
                        &mut card.due
                    };
                    let new_due =
                        (timing.days_elapsed as i32) - days_elapsed + card.interval as i32;
                    if let Some(rescheduler) = &mut rescheduler {
                        rescheduler.update_due_cnt_per_day(*due, new_due, deckconfig_id);
                    }
                    *due = new_due;
                    // Rescheduling changes the card only; it writes no review-log
                    // row (spec sched.reschedule-no-revlog).

                    Ok(())
                };

            self.update_memory_state_for_cards_with_items(
                items,
                &fsrs,
                set_decay_and_desired_retention,
                reschedule,
                keep_stability,
                usn,
                on_updated_card,
            )?;
            Self::finish_memory_progress_entry(&mut preset_progress, entry_index, item_count);
        }
        Ok(())
    }

    /// After a normal sync has merged the server's rows, rebuild the FSRS data
    /// of every card whose local pending row conflicted with the server's row
    /// (spec `sync.fsrs-reconcile-after-sync`). Memory state, desired
    /// retention, decay and last review time are recomputed from the merged
    /// review log with the card's current preset. The schedule is only
    /// restored from the last real review when the user's remembered
    /// "Reschedule cards on change" choice is on
    /// (spec `sync.post-sync-reschedule-gate`). Nothing here writes a review
    /// log row (spec `sync.no-revlog-rows-from-post-sync-reschedule`).
    ///
    /// Runs inside the sync transaction, before the local changes are sent, so
    /// the repaired rows are uploaded by the same sync.
    pub(crate) fn reconcile_fsrs_state_after_sync(
        &mut self,
        conflicts: HashMap<CardId, FsrsSyncConflict>,
    ) -> Result<()> {
        if conflicts.is_empty() || !self.get_config_bool(BoolKey::Fsrs) {
            return Ok(());
        }
        let restore_schedule = self.get_config_bool(BoolKey::FsrsReschedule);
        let (card_ids_by_config, deck_desired_retention) =
            self.card_ids_by_home_config(conflicts.keys().copied())?;

        let timing = self.timing_today()?;
        let usn = self.usn()?;
        for (config_id, card_ids) in card_ids_by_config {
            let config = self
                .storage
                .get_deck_config(config_id)?
                .or_not_found(config_id)?;
            let revlog =
                self.revlog_for_srs(SearchNode::CardIds(comma_separated_ids(&card_ids)))?;
            let params = config.fsrs_params();
            let fsrs = FSRS::new(params)?;
            let last_revlog_info = get_last_revlog_info(&revlog);
            // spec deck-options.historical-retention-fixed
            let items = fsrs_items_for_memory_states(
                &fsrs,
                params,
                revlog,
                HISTORICAL_RETENTION,
                ignore_revlogs_before_ms_from_config(&config)?,
            )?;

            let (items, mut cards_without_items): (
                Vec<(CardId, FsrsItemForMemoryState)>,
                Vec<CardId>,
            ) = items.into_iter().partition_map(|(card_id, item)| {
                if let Some(item) = item {
                    Either::Left((card_id, item))
                } else {
                    Either::Right(card_id)
                }
            });
            // a card without any review log row is missing from `items`
            let seen: HashSet<CardId> = items
                .iter()
                .map(|(card_id, _)| *card_id)
                .chain(cards_without_items.iter().copied())
                .collect();
            cards_without_items.extend(card_ids.iter().copied().filter(|id| !seen.contains(id)));

            let decay = get_decay_from_params(params);
            let preset_desired_retention = config.inner.desired_retention;
            let set_decay_and_desired_retention = |card: &mut Card| {
                let deck_id = card.original_or_current_deck_id();
                let desired_retention = *deck_desired_retention
                    .get(&deck_id)
                    .unwrap_or(&preset_desired_retention);
                card.desired_retention = Some(desired_retention);
                card.decay = Some(decay);
            };
            tracing::debug!(
                config_id = config_id.0,
                cards = card_ids.len(),
                cards_with_items = items.len(),
                itemless_cards = cards_without_items.len(),
                restore_schedule,
                "recomputing fsrs state after sync"
            );

            self.reconcile_itemless_cards_after_sync(
                cards_without_items,
                &conflicts,
                &last_revlog_info,
                set_decay_and_desired_retention,
                usn,
            )?;
            self.reconcile_cards_with_items_after_sync(
                items,
                &fsrs,
                &conflicts,
                &last_revlog_info,
                restore_schedule,
                timing,
                set_decay_and_desired_retention,
                usn,
            )?;
        }

        Ok(())
    }

    /// The cards grouped by their home deck's preset (a card in a filtered
    /// deck counts in its original deck), with the desired retention of each
    /// home deck that overrides its preset's.
    #[allow(clippy::type_complexity)]
    fn card_ids_by_home_config(
        &mut self,
        card_ids: impl Iterator<Item = CardId>,
    ) -> Result<(HashMap<DeckConfigId, Vec<CardId>>, HashMap<DeckId, f32>)> {
        let mut card_ids_by_config: HashMap<DeckConfigId, Vec<CardId>> = HashMap::new();
        let mut deck_desired_retention: HashMap<DeckId, f32> = HashMap::new();
        for card_id in card_ids {
            let card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
            let deck_id = card.original_or_current_deck_id();
            let deck = self.get_deck(deck_id)?.or_not_found(deck_id)?;
            let config_id = deck.config_id().or_invalid("home deck is filtered")?;
            card_ids_by_config
                .entry(config_id)
                .or_default()
                .push(card_id);
            if let Ok(normal) = deck.normal() {
                if let Some(desired_retention) = normal.desired_retention {
                    deck_desired_retention.insert(deck_id, desired_retention);
                }
            }
        }
        Ok((card_ids_by_config, deck_desired_retention))
    }

    /// Gives every card whose row another client wrote (a memory state
    /// without FSRS-7's internal stability: official Anki and AnkiDroid drop
    /// it, and their memory state is not an FSRS-7 one) its FSRS-7 memory
    /// state again, with its home preset's parameters (spec
    /// sync.fsrs7-state-of-foreign-cards): from its review log when that
    /// has a usable review, else the FSRS-7 state whose S90 is the stored
    /// stability, keeping the stored difficulty. Desired retention and decay
    /// come from the preset, as in the post-sync reconcile. Due dates stay.
    /// The rows are marked modified, so the next sync uploads them. With
    /// FSRS off nothing runs. Returns the number of such cards found.
    ///
    /// Expects a transaction; `repair_fsrs7_state_of_foreign_cards` opens one.
    pub(crate) fn repair_fsrs7_state_of_foreign_cards_inner(&mut self) -> Result<usize> {
        if !self.get_config_bool(BoolKey::Fsrs) {
            return Ok(0);
        }
        let card_ids = self.storage.card_ids_with_foreign_fsrs_state()?;
        self.repair_fsrs7_state_of_cards_inner(card_ids)
    }

    /// The repair of `repair_fsrs7_state_of_foreign_cards_inner` for cards
    /// known to be foreign (an import finds them in the package, before
    /// writing them gives them an internal stability): their stored
    /// stability is taken as the S90.
    pub(crate) fn repair_fsrs7_state_of_cards_inner(
        &mut self,
        card_ids: Vec<CardId>,
    ) -> Result<usize> {
        if card_ids.is_empty() || !self.get_config_bool(BoolKey::Fsrs) {
            return Ok(0);
        }
        let (card_ids_by_config, deck_desired_retention) =
            self.card_ids_by_home_config(card_ids.iter().copied())?;
        let timing = self.timing_today()?;
        let usn = self.usn()?;
        for (config_id, card_ids) in card_ids_by_config {
            let config = self
                .storage
                .get_deck_config(config_id)?
                .or_not_found(config_id)?;
            let revlog =
                self.revlog_for_srs(SearchNode::CardIds(comma_separated_ids(&card_ids)))?;
            let params = config.fsrs_params();
            let fsrs = FSRS::new(params)?;
            let last_revlog_info = get_last_revlog_info(&revlog);
            let (items, mut cards_without_items): (
                Vec<(CardId, FsrsItemForMemoryState)>,
                Vec<CardId>,
            ) = fsrs_items_for_memory_states(
                &fsrs,
                params,
                revlog,
                HISTORICAL_RETENTION,
                ignore_revlogs_before_ms_from_config(&config)?,
            )?
            .into_iter()
            .partition_map(|(card_id, item)| match item {
                Some(item) => Either::Left((card_id, item)),
                None => Either::Right(card_id),
            });
            // a card without any review log row is missing from `items`
            let seen: HashSet<CardId> = items
                .iter()
                .map(|(card_id, _)| *card_id)
                .chain(cards_without_items.iter().copied())
                .collect();
            cards_without_items.extend(card_ids.iter().copied().filter(|id| !seen.contains(id)));

            let decay = get_decay_from_params(params);
            let preset_desired_retention = config.inner.desired_retention;
            let set_decay_and_desired_retention = |card: &mut Card| {
                let deck_id = card.original_or_current_deck_id();
                card.desired_retention = Some(
                    *deck_desired_retention
                        .get(&deck_id)
                        .unwrap_or(&preset_desired_retention),
                );
                card.decay = Some(decay);
            };
            for card_id in cards_without_items {
                let mut card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
                let Some(state) = card.memory_state.and_then(|stored| {
                    fsrs_memory_state_for_s90_and_difficulty(
                        &fsrs,
                        stored.stability,
                        stored.difficulty,
                    )
                }) else {
                    continue;
                };
                set_decay_and_desired_retention(&mut card);
                card.memory_state = Some(state);
                self.update_reconciled_card_after_sync(&mut card, usn)?;
            }
            // no conflicts: last review time from the review log, schedule kept
            self.reconcile_cards_with_items_after_sync(
                items,
                &fsrs,
                &HashMap::new(),
                &last_revlog_info,
                false,
                timing,
                set_decay_and_desired_retention,
                usn,
            )?;
        }
        Ok(card_ids.len())
    }

    /// `repair_fsrs7_state_of_foreign_cards_inner` in its own transaction,
    /// without an undo entry; nothing is written when no card needs it.
    pub(crate) fn repair_fsrs7_state_of_foreign_cards(&mut self) -> Result<usize> {
        if !self.get_config_bool(BoolKey::Fsrs)
            || self.storage.card_ids_with_foreign_fsrs_state()?.is_empty()
        {
            return Ok(0);
        }
        self.transact_no_undo(|col| col.repair_fsrs7_state_of_foreign_cards_inner())
    }

    /// Marks the card as changed locally so the running sync uploads it. No
    /// undo entry: a sync cannot be undone.
    fn update_reconciled_card_after_sync(&mut self, card: &mut Card, usn: Usn) -> Result<()> {
        card.set_modified(usn);
        self.storage.update_card(card)
    }

    /// Cards whose merged review log holds no real review. Their memory state
    /// cannot be derived, so it is cleared - unless both devices already held
    /// the same state, which the conflict then gives no reason to touch (spec
    /// `sync.fsrs-reconcile-after-sync`).
    fn reconcile_itemless_cards_after_sync(
        &mut self,
        cards: Vec<CardId>,
        conflicts: &HashMap<CardId, FsrsSyncConflict>,
        last_revlog_info: &HashMap<CardId, LastRevlogInfo>,
        mut set_decay_and_desired_retention: impl FnMut(&mut Card),
        usn: Usn,
    ) -> Result<()> {
        for card_id in cards {
            let conflict = conflicts.get(&card_id).copied().unwrap_or_default();
            let mut card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
            set_decay_and_desired_retention(&mut card);
            if !conflict.memory_state_agreed {
                card.memory_state = None;
            }
            if !conflict.last_review_time_agreed {
                card.last_review_time = last_revlog_info
                    .get(&card_id)
                    .and_then(|info| info.last_reviewed_at);
            }
            self.update_reconciled_card_after_sync(&mut card, usn)?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn reconcile_cards_with_items_after_sync(
        &mut self,
        items: Vec<(CardId, FsrsItemForMemoryState)>,
        fsrs: &FSRS,
        conflicts: &HashMap<CardId, FsrsSyncConflict>,
        last_revlog_info: &HashMap<CardId, LastRevlogInfo>,
        restore_schedule: bool,
        timing: SchedTimingToday,
        mut set_decay_and_desired_retention: impl FnMut(&mut Card),
        usn: Usn,
    ) -> Result<()> {
        const FSRS_BATCH_SIZE: usize = 1000;

        let mut to_update = Vec::new();
        let mut fsrs_items = Vec::new();
        let mut starting_states = Vec::new();

        for (card_id, item) in items.into_iter() {
            to_update.push(card_id);
            fsrs_items.push(item.item);
            starting_states.push(item.starting_state);
        }

        let mut p = permutation::sort_unstable_by_key(&fsrs_items, |item| item.reviews.len());
        p.apply_slice_in_place(&mut to_update);
        p.apply_slice_in_place(&mut fsrs_items);
        p.apply_slice_in_place(&mut starting_states);

        for ((to_update, fsrs_items), starting_states) in to_update
            .chunk_into_vecs(FSRS_BATCH_SIZE)
            .zip_eq(fsrs_items.chunk_into_vecs(FSRS_BATCH_SIZE))
            .zip_eq(starting_states.chunk_into_vecs(FSRS_BATCH_SIZE))
        {
            let memory_states = fsrs.memory_state_batch(fsrs_items, starting_states)?;

            for (card_id, memory_state) in to_update.into_iter().zip_eq(memory_states) {
                let conflict = conflicts.get(&card_id).copied().unwrap_or_default();
                let mut card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
                set_decay_and_desired_retention(&mut card);
                card.memory_state = Some(fsrs_memory_state_for_fsrs(fsrs, memory_state));
                let last_info = last_revlog_info.get(&card_id);
                if !conflict.last_review_time_agreed {
                    card.last_review_time = last_info.and_then(|info| info.last_reviewed_at);
                }
                if restore_schedule && conflict.schedule_differs {
                    restore_review_schedule_after_sync(&mut card, last_info, timing);
                }
                self.update_reconciled_card_after_sync(&mut card, usn)?;
            }
        }

        Ok(())
    }

    fn prepare_memory_progress_entry(
        preset_progress: &mut [ComputeMemoryPresetProgress],
        entry_index: usize,
        total_cards: usize,
    ) {
        if let Some(progress) = preset_progress.get_mut(entry_index) {
            progress.total_cards = total_cards as u32;
            progress.finished = total_cards == 0;
        }
    }

    fn finish_memory_progress_entry(
        preset_progress: &mut [ComputeMemoryPresetProgress],
        entry_index: usize,
        total_cards: usize,
    ) {
        if let Some(progress) = preset_progress.get_mut(entry_index) {
            progress.current_cards = total_cards as u32;
            progress.total_cards = total_cards as u32;
            progress.finished = true;
        }
    }

    fn set_active_memory_progress(progress: &mut ComputeMemoryProgress, active_index: usize) {
        if let Some(active) = progress.presets.get(active_index) {
            progress.current_cards = active.current_cards;
            progress.total_cards = active.total_cards;
            progress.preset_name.clone_from(&active.name);
            progress.rescheduling = active.rescheduling;
            progress.saving = active.saving;
        }
    }

    fn create_progress_closure(
        &self,
        presets: Vec<ComputeMemoryPresetProgress>,
        active_index: usize,
        preset_name: String,
        current_preset: u32,
        total_presets: u32,
        rescheduling: bool,
    ) -> Result<impl FnMut() -> Result<()>> {
        let mut progress = self.new_progress_handler::<ComputeMemoryProgress>();
        progress.update(false, |s| {
            s.presets = presets;
            s.preset_name = preset_name;
            s.current_preset = current_preset;
            s.total_presets = total_presets;
            s.rescheduling = rescheduling;
            s.saving = false;
            Self::set_active_memory_progress(s, active_index);
        })?;
        let on_updated_card = move || {
            progress.update(true, |p| {
                if let Some(active) = p.presets.get_mut(active_index) {
                    active.current_cards = active
                        .current_cards
                        .saturating_add(1)
                        .min(active.total_cards);
                    active.finished = active.current_cards >= active.total_cards;
                }
                Self::set_active_memory_progress(p, active_index);
            })
        };
        Ok(on_updated_card)
    }

    fn clear_fsrs_data_for_cards(
        &mut self,
        cards: impl Iterator<Item = CardId>,
        usn: Usn,
        mut on_updated_card: impl FnMut() -> Result<()>,
    ) -> Result<()> {
        for card_id in cards {
            let mut card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
            let original = card.clone();
            card.clear_fsrs_data();
            self.update_card_inner(&mut card, original, usn)?;
            on_updated_card()?
        }
        Ok(())
    }

    fn update_memory_state_for_itemless_cards(
        &mut self,
        cards: Vec<CardId>,
        mut set_decay_and_desired_retention: impl FnMut(&mut Card),
        usn: Usn,
        mut on_updated_card: impl FnMut() -> Result<()>,
    ) -> Result<()> {
        for card_id in cards {
            let mut card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
            let original = card.clone();
            set_decay_and_desired_retention(&mut card);
            card.memory_state = None;
            self.update_card_inner(&mut card, original, usn)?;
            on_updated_card()?;
        }
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    fn update_memory_state_for_cards_with_items(
        &mut self,
        items: Vec<(CardId, FsrsItemForMemoryState)>,
        fsrs: &FSRS,
        mut set_decay_and_desired_retention: impl FnMut(&mut Card),
        mut maybe_reschedule_card: impl FnMut(&mut Card, &mut Self, &FSRS) -> Result<()>,
        keep_stability: bool,
        usn: Usn,
        mut on_updated_card: impl FnMut() -> Result<()>,
    ) -> Result<()> {
        const FSRS_BATCH_SIZE: usize = 1000;

        let mut to_update = Vec::new();
        let mut fsrs_items = Vec::new();
        let mut starting_states = Vec::new();

        for (card_id, item) in items.into_iter() {
            to_update.push(card_id);
            fsrs_items.push(item.item);
            starting_states.push(item.starting_state);
        }

        // fsrs.memory_state_batch is O(nm) where n is the number of cards and m is the
        // max review count between all items. Therefore we want to pass batches
        // to fsrs.memory_state_batch where the review count is relatively even.
        let mut p = permutation::sort_unstable_by_key(&fsrs_items, |item| item.reviews.len());
        p.apply_slice_in_place(&mut to_update);
        p.apply_slice_in_place(&mut fsrs_items);
        p.apply_slice_in_place(&mut starting_states);

        for ((to_update, fsrs_items), starting_states) in to_update
            .chunk_into_vecs(FSRS_BATCH_SIZE)
            .zip_eq(fsrs_items.chunk_into_vecs(FSRS_BATCH_SIZE))
            .zip_eq(starting_states.chunk_into_vecs(FSRS_BATCH_SIZE))
        {
            let memory_states = fsrs.memory_state_batch(fsrs_items, starting_states)?;

            for (card_id, memory_state) in to_update.into_iter().zip_eq(memory_states) {
                let mut card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
                let original = card.clone();
                set_decay_and_desired_retention(&mut card);
                let mut memory_state = fsrs_memory_state_for_fsrs(fsrs, memory_state);
                if let Some(stored) = card.memory_state.filter(|_| keep_stability) {
                    memory_state.stability = stored.stability;
                }
                card.memory_state = Some(memory_state);
                maybe_reschedule_card(&mut card, self, fsrs)?;
                self.update_card_inner(&mut card, original, usn)?;
                on_updated_card()?;
            }
        }
        Ok(())
    }

    fn fsrs_params_for_card_id(&mut self, card_id: CardId) -> Result<Vec<f32>> {
        let card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
        Ok(self.fsrs_preset_for_card(&card)?.params)
    }

    fn fsrs_params_for_config_id(&mut self, config_id: DeckConfigId) -> Result<Vec<f32>> {
        let config = self
            .storage
            .get_deck_config(config_id)?
            .or_not_found(config_id)?;
        Ok(config.fsrs_params().to_vec())
    }

    pub fn fsrs_current_retrievability_for_card(
        &mut self,
        card_id: CardId,
        stability: f32,
        elapsed_days: f32,
    ) -> Result<f32> {
        let params = self.fsrs_params_for_card_id(card_id)?;
        fsrs_current_retrievability_for_params(&params, stability, elapsed_days)
    }

    pub(crate) fn fsrs_current_retrievability_for_card_state(
        &mut self,
        card_id: CardId,
        state: FsrsMemoryState,
        elapsed_days: f32,
    ) -> Result<f32> {
        let params = self.fsrs_params_for_card_id(card_id)?;
        fsrs_current_retrievability_for_state(&params, state, elapsed_days)
    }

    pub(crate) fn fsrs_relative_overdueness_for_card_state(
        &mut self,
        card: &Card,
        state: FsrsMemoryState,
        elapsed_days: f32,
    ) -> Result<f32> {
        let preset = self.fsrs_preset_for_card(card)?;
        fsrs_relative_overdueness_for_state(
            &preset.params,
            state,
            elapsed_days,
            card.desired_retention.unwrap_or(preset.desired_retention),
        )
    }

    pub fn fsrs_next_interval_for_card(
        &mut self,
        card_id: CardId,
        stability: f32,
        desired_retention: f32,
    ) -> Result<f32> {
        let params = self.fsrs_params_for_card_id(card_id)?;
        fsrs_next_interval_for_s90(&params, stability, desired_retention)
    }

    pub fn fsrs_interval_at_retrievability_for_card(
        &mut self,
        card_id: CardId,
        stability: f32,
        target_retrievability: f32,
    ) -> Result<f32> {
        let params = self.fsrs_params_for_card_id(card_id)?;
        fsrs_interval_at_retrievability_for_params(&params, stability, target_retrievability)
    }

    pub fn fsrs_interval_at_retrievability_for_cards(
        &mut self,
        cards: &[(CardId, f32)],
        target_retrievability: f32,
    ) -> Result<Vec<f32>> {
        let cards: Vec<(CardId, f32, f32)> = cards
            .iter()
            .map(|(card_id, stability)| (*card_id, *stability, target_retrievability))
            .collect();
        self.fsrs_interval_at_retrievability_for_card_targets(&cards)
    }

    pub fn fsrs_interval_at_retrievability_for_card_targets(
        &mut self,
        cards: &[(CardId, f32, f32)],
    ) -> Result<Vec<f32>> {
        let card_ids: Vec<CardId> = cards.iter().map(|(card_id, _, _)| *card_id).collect();
        let card_rows = self.all_cards_for_ids(&card_ids, false)?;
        let presets_by_card = self.fsrs_presets_for_cards(&card_rows)?;
        let mut fsrs_by_preset_id = HashMap::new();
        let mut intervals = Vec::with_capacity(cards.len());

        for (card_id, stability, target_retrievability) in cards {
            let preset = presets_by_card.get(card_id).or_not_found(*card_id)?;
            if !fsrs_by_preset_id.contains_key(&preset.id) {
                fsrs_by_preset_id.insert(preset.id.clone(), FSRS::new(&preset.params)?);
            }
            let fsrs = fsrs_by_preset_id
                .get(&preset.id)
                .expect("FSRS instance inserted");
            intervals.push(fsrs.interval_at_retrievability(
                MemoryState {
                    stability: *stability,
                    difficulty: 5.0,
                    stability_fast: *stability,
                },
                target_retrievability.clamp(0.0001, 0.9999),
            ));
        }

        Ok(intervals)
    }

    pub fn fsrs_desired_retention_for_intervals(
        &mut self,
        cards: &[(CardId, f32)],
    ) -> Result<Vec<FsrsDesiredRetentionForInterval>> {
        let card_ids: Vec<CardId> = cards
            .iter()
            .map(|(card_id, _desired_retention)| *card_id)
            .collect();
        let card_rows = self.all_cards_for_ids(&card_ids, false)?;
        let card_by_id: HashMap<CardId, _> = card_rows.iter().map(|card| (card.id, card)).collect();
        let presets_by_card = self.fsrs_presets_for_cards(&card_rows)?;
        let mut targets = Vec::with_capacity(cards.len());

        for (card_id, desired_retention) in cards {
            card_by_id.get(card_id).or_not_found(*card_id)?;
            presets_by_card.get(card_id).or_not_found(*card_id)?;
            targets.push(FsrsDesiredRetentionForInterval {
                interval_target_desired_retention: *desired_retention,
            });
        }

        Ok(targets)
    }

    pub fn fsrs_interval_at_retrievability_for_configs(
        &mut self,
        configs: &[(DeckConfigId, f32)],
        target_retrievability: f32,
    ) -> Result<Vec<f32>> {
        let mut params_by_config: HashMap<DeckConfigId, Vec<f32>> = HashMap::new();
        let mut intervals = Vec::with_capacity(configs.len());
        for (config_id, stability) in configs {
            let params = if let Some(params) = params_by_config.get(config_id) {
                params
            } else {
                let params = self.fsrs_params_for_config_id(*config_id)?;
                params_by_config.insert(*config_id, params);
                params_by_config
                    .get(config_id)
                    .expect("config params inserted")
            };
            intervals.push(fsrs_interval_at_retrievability_for_params(
                params,
                *stability,
                target_retrievability,
            )?);
        }
        Ok(intervals)
    }

    pub fn compute_memory_state(&mut self, card_id: CardId) -> Result<ComputeMemoryStateResponse> {
        let card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
        let computed = self.compute_memory_state_for_card(&card, false)?;
        Ok(ComputeMemoryStateResponse {
            state: computed.memory_state.map(Into::into),
            desired_retention: computed.desired_retention,
            decay: computed.decay,
        })
    }

    pub(crate) fn recompute_fsrs_data_for_card(&mut self, card: &mut Card) -> Result<()> {
        if card.ctype == CardType::New {
            return Ok(());
        }

        let computed = self.compute_memory_state_for_card(card, true)?;
        card.memory_state = computed.memory_state;
        card.desired_retention = Some(computed.desired_retention);
        card.decay = Some(computed.decay);
        Ok(())
    }

    fn compute_memory_state_for_card(
        &mut self,
        card: &Card,
        infer_from_current_card_state: bool,
    ) -> Result<ComputedMemoryState> {
        let fsrs_preset = self.fsrs_preset_for_card(card)?;
        let desired_retention = fsrs_preset.desired_retention;
        let historical_retention = fsrs_preset.historical_retention;
        let params = &fsrs_preset.params;
        let decay = get_decay_from_params(params);
        let fsrs = FSRS::new(params)?;
        let mut revlog = self.storage.get_revlog_entries_for_card(card.id)?;
        let revlog_count = revlog.len();
        revlog.sort_unstable_by_key(|entry| entry.id);
        let item = fsrs_item_for_memory_state(
            &fsrs,
            params,
            revlog,
            historical_retention,
            fsrs_preset.ignore_revlogs_before_ms()?,
        )?;
        let memory_state = if item.is_some() || infer_from_current_card_state {
            let mut card = card.clone();
            card.set_memory_state(&fsrs, params, item, historical_retention)?;
            card.memory_state
        } else {
            None
        };
        tracing::debug!(
            card_id = card.id.0,
            preset_id = ?fsrs_preset.id,
            preset_name = fsrs_preset.name.as_str(),
            params_len = params.len(),
            params_fingerprint = format_args!("{:016x}", params_fingerprint(params)),
            desired_retention = round_to_two_decimals(desired_retention),
            historical_retention = round_to_two_decimals(historical_retention),
            decay = round_to_two_decimals(decay),
            revlog_count,
            computed_s90 = memory_state.map(|state| round_to_two_decimals(state.stability)),
            computed_internal_stability = memory_state
                .map(|state| round_to_two_decimals(state.stability_internal)),
            computed_difficulty = memory_state.map(|state| round_to_two_decimals(state.difficulty)),
            "computed FSRS memory state"
        );

        Ok(ComputedMemoryState {
            memory_state,
            desired_retention,
            decay,
        })
    }

    // Used for extra-ordinary circumstances where a memory state is needed but is
    // not availiable, e.g. the card has been moved to a different deck.
    // Try to use update_memory_state where you can.
    pub fn compute_and_update_memory_state(&mut self, card: &mut Card) -> Result<()> {
        let fsrs_data = self.compute_memory_state(card.id)?;
        card.memory_state = fsrs_data.state.map(Into::into);
        card.desired_retention = Some(fsrs_data.desired_retention);
        card.decay = Some(fsrs_data.decay);
        self.storage.update_card(card)?;
        Ok(())
    }
}

impl Card {
    pub(crate) fn set_memory_state(
        &mut self,
        fsrs: &FSRS,
        params: &[f32],
        item: Option<FsrsItemForMemoryState>,
        historical_retention: f32,
    ) -> Result<()> {
        let memory_state = if let Some(i) = item {
            Some(fsrs.memory_state(i.item, i.starting_state)?)
        } else if self.ctype == CardType::New || self.interval == 0 {
            None
        } else {
            // no valid revlog entries; infer state from current card state
            Some(memory_state_from_sm2_with_params(
                fsrs,
                params,
                self.ease_factor(),
                self.interval as f32,
                historical_retention,
            )?)
        };
        self.memory_state = memory_state.map(|state| fsrs_memory_state_for_fsrs(fsrs, state));
        Ok(())
    }
}

#[derive(Debug, Clone)]
pub(crate) struct FsrsItemForMemoryState {
    pub item: FSRSItem,
    /// When revlogs have been truncated, this stores the initial state at first
    /// review
    pub starting_state: Option<MemoryState>,
    pub filtered_revlogs: Vec<RevlogEntry>,
}

/// Like [fsrs_item_for_memory_state], but for updating multiple cards at once.
pub(crate) fn fsrs_items_for_memory_states(
    fsrs: &FSRS,
    params: &[f32],
    revlogs: Vec<RevlogEntry>,
    historical_retention: f32,
    ignore_revlogs_before: TimestampMillis,
) -> Result<Vec<(CardId, Option<FsrsItemForMemoryState>)>> {
    revlogs
        .into_iter()
        .chunk_by(|r| r.cid)
        .into_iter()
        .map(|(card_id, group)| {
            Ok((
                card_id,
                fsrs_item_for_memory_state(
                    fsrs,
                    params,
                    group.collect(),
                    historical_retention,
                    ignore_revlogs_before,
                )?,
            ))
        })
        .collect()
}

pub(crate) struct LastRevlogInfo {
    /// Used to determine the actual elapsed time between the last time the user
    /// reviewed the card and now, so that we can determine an accurate period
    /// when the card has subsequently been rescheduled to a different day.
    pub(crate) last_reviewed_at: Option<TimestampSecs>,
    /// The interval before the latest review. Used to prevent fuzz from going
    /// backwards when rescheduling the card
    pub(crate) previous_interval: Option<u32>,
    /// The interval in days that the latest review scheduled. None when that
    /// review left the card in (re)learning, or after a reset.
    pub(crate) scheduled_interval: Option<u32>,
}

/// Return a map of cards to info about last review.
pub(crate) fn get_last_revlog_info(revlogs: &[RevlogEntry]) -> HashMap<CardId, LastRevlogInfo> {
    let mut out = HashMap::new();
    revlogs
        .iter()
        .chunk_by(|r| r.cid)
        .into_iter()
        .for_each(|(card_id, group)| {
            let mut last_reviewed_at = None;
            let mut previous_interval = None;
            let mut scheduled_interval = None;
            for e in group.into_iter() {
                if e.has_rating_and_affects_scheduling() {
                    last_reviewed_at = Some(e.id.as_secs());
                    previous_interval = if e.last_interval >= 0 && e.button_chosen > 1 {
                        Some(e.last_interval as u32)
                    } else {
                        None
                    };
                    scheduled_interval = (e.interval > 0).then_some(e.interval as u32);
                } else if e.is_reset() {
                    last_reviewed_at = None;
                    previous_interval = None;
                    scheduled_interval = None;
                }
            }
            out.insert(
                card_id,
                LastRevlogInfo {
                    last_reviewed_at,
                    previous_interval,
                    scheduled_interval,
                },
            );
        });
    out
}

/// Give a review card back the schedule its latest real review produced:
/// the interval that review scheduled, due on the review's day plus that
/// interval (written to `original_due` while the card sits in a filtered
/// deck). Used after a sync when the surviving card row did not reflect the
/// merged review log (spec `sync.post-sync-reschedule-gate`). Nothing is
/// recomputed with FSRS and no fuzz or load balancing is applied, so the card
/// ends up exactly where the reviewing device put it. Returns whether the
/// card changed. Cards that are not in the review queue, suspended cards, and
/// cards whose latest review left them in (re)learning are left alone.
pub(crate) fn restore_review_schedule_after_sync(
    card: &mut Card,
    last_revlog_info: Option<&LastRevlogInfo>,
    timing: SchedTimingToday,
) -> bool {
    let Some(last_info) = last_revlog_info else {
        return false;
    };
    let (Some(last_review), Some(interval)) =
        (last_info.last_reviewed_at, last_info.scheduled_interval)
    else {
        return false;
    };
    if !(card.ctype == CardType::Review && card.queue != CardQueue::Suspended) {
        return false;
    }
    let days_since_review = timing.next_day_at.elapsed_days_since(last_review) as i32;
    let new_due = timing.days_elapsed as i32 - days_since_review + interval as i32;
    let due = if card.original_due != 0 {
        &mut card.original_due
    } else {
        &mut card.due
    };
    let changed = card.interval != interval || *due != new_due;
    *due = new_due;
    card.interval = interval;
    changed
}

/// When calculating memory state, only the last FSRSItem is required. If the
/// revlog is non-empty and no learning steps have been detected (indicative of
/// a truncated revlog), we return the starting state inferred from the first
/// revlog entry, so that the first review is not treated as if started from
/// scratch.
pub(crate) fn fsrs_item_for_memory_state(
    fsrs: &FSRS,
    params: &[f32],
    entries: Vec<RevlogEntry>,
    historical_retention: f32,
    ignore_revlogs_before: TimestampMillis,
) -> Result<Option<FsrsItemForMemoryState>> {
    struct FirstReview {
        interval: f32,
        ease_factor: f32,
    }
    if let Some(mut output) = reviews_for_fsrs(entries, false, ignore_revlogs_before) {
        let mut item = output.fsrs_items.pop().unwrap().1;
        if output.revlogs_complete {
            Ok(Some(FsrsItemForMemoryState {
                item,
                starting_state: None,
                filtered_revlogs: output.filtered_revlogs,
            }))
        } else if let Some(first_user_grade) = output.filtered_revlogs.first() {
            // the revlog has been truncated, but not fully
            let first_review = FirstReview {
                interval: first_user_grade.interval.max(1) as f32,
                ease_factor: if first_user_grade.ease_factor == 0 {
                    2500
                } else {
                    first_user_grade.ease_factor
                } as f32
                    / 1000.0,
            };
            let mut starting_state = memory_state_from_sm2_with_params(
                fsrs,
                params,
                first_review.ease_factor,
                first_review.interval,
                historical_retention,
            )?;
            // if the ease factor is less than 1.1, the revlog entry is generated by FSRS
            if first_review.ease_factor <= 1.1 {
                starting_state.difficulty = (first_review.ease_factor - 0.1) * 9.0 + 1.0;
                // the difficulty changes where the curve crosses the retention
                starting_state = scale_state_to_interval(
                    fsrs,
                    starting_state,
                    first_review.interval,
                    historical_retention,
                );
            }
            // remove the first review because it has been converted to the starting state
            item.reviews.remove(0);
            Ok(Some(FsrsItemForMemoryState {
                item,
                starting_state: Some(starting_state),
                filtered_revlogs: output.filtered_revlogs,
            }))
        } else {
            // only manual and rescheduled revlogs; treat like empty
            Ok(None)
        }
    } else {
        // no revlogs (new card or caused by ignore_revlogs_before or deleted revlogs)
        Ok(None)
    }
}

#[cfg(test)]
mod tests {
    use anki_proto::deck_config::deck_configs_for_update::current_deck::Limits;
    use anki_proto::deck_config::UpdateDeckConfigsMode;
    use fsrs::MemoryState;
    use fsrs::DEFAULT_PARAMETERS;
    use fsrs::FSRS6_DEFAULT_PARAMETERS;

    use super::*;
    use crate::deckconfig::FsrsVersion;
    use crate::deckconfig::UpdateDeckConfigsRequest;
    use crate::revlog::RevlogId;
    use crate::revlog::RevlogReviewKind;
    use crate::scheduler::fsrs::params::tests::convert;
    use crate::scheduler::fsrs::params::tests::revlog;
    use crate::scheduler::fsrs::preset::AddonFsrsPreset;
    use crate::scheduler::fsrs::preset::AddonFsrsVersion;
    use crate::scheduler::fsrs::preset::FsrsPresetOverlay;
    use crate::scheduler::fsrs::preset::FsrsPresetRule;
    use crate::scheduler::fsrs::preset::FSRS_PRESET_OVERLAY_CONFIG_KEY;
    use crate::search::SortMode;

    /// Floating point precision can vary between platforms, and each FSRS
    /// update tends to result in small changes to these numbers, so we
    /// round them.
    fn assert_int_eq(actual: Option<FsrsMemoryState>, expected: Option<FsrsMemoryState>) {
        let actual = actual.unwrap();
        let expected = expected.unwrap();
        assert_eq!(actual.stability.round(), expected.stability.round());
        assert_eq!(actual.difficulty.round(), expected.difficulty.round());
    }

    // Pins spec/deck-options.md#deck-options.historical-retention-fixed: a
    // preset that stores 0.7 computes the same memory states as one that
    // stores 0.9. The SM-2 conversion (spec sched.fsrs7-sm2-conversion) uses
    // 0.9, so the card gets the FSRS-7 state whose S90 is its interval.
    #[test]
    fn stored_historical_retention_is_ignored() -> Result<()> {
        fn inferred_memory_state(stored_historical_retention: f32) -> Result<FsrsMemoryState> {
            let mut col = Collection::new();
            col.set_config_bool(BoolKey::Fsrs, true, false)?;
            col.update_default_deck_config(|config| {
                config.historical_retention = stored_historical_retention;
            });
            NoteAdder::basic(&mut col).add(&mut col);
            // A review card without a revlog: its memory state is inferred
            // from the SM-2 interval and ease.
            let mut card = col.get_first_card();
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.interval = 100;
            card.ease_factor = 2500;
            card.due = col.timing_today()?.days_elapsed as i32;
            col.recompute_fsrs_data_for_card(&mut card)?;
            card.memory_state.or_invalid("no memory state")
        }

        let with_stored_0_7 = inferred_memory_state(0.7)?;
        let with_stored_0_9 = inferred_memory_state(0.9)?;
        assert_eq!(with_stored_0_7, with_stored_0_9);

        let fsrs = FSRS::new(&DEFAULT_PARAMETERS)?;
        let at_0_9 =
            memory_state_from_sm2_with_params(&fsrs, &DEFAULT_PARAMETERS, 2.5, 100.0, 0.9)?;
        assert_int_eq(
            Some(with_stored_0_7),
            Some(fsrs_memory_state_for_fsrs(&fsrs, at_0_9)),
        );
        assert!((with_stored_0_7.stability - 100.0).abs() < 0.01);
        Ok(())
    }

    // Pins spec/deck-options.md#deck-options.historical-retention-fixed: the
    // post-sync reconcile ignores a stored historical retention too.
    #[test]
    fn post_sync_reconcile_ignores_the_stored_historical_retention() -> Result<()> {
        fn reconciled_state(stored_historical_retention: f32) -> Result<FsrsMemoryState> {
            let mut col = Collection::new();
            col.set_config_bool(BoolKey::Fsrs, true, false)?;
            col.update_default_deck_config(|config| {
                config.historical_retention = stored_historical_retention;
            });
            NoteAdder::basic(&mut col).add(&mut col);
            let mut card = col.get_first_card();
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.interval = 30;
            card.ease_factor = 2500;
            col.storage.update_card(&card)?;
            // a truncated review log (it starts with a review): the starting
            // state comes from the SM-2 interval and ease
            for days_ago in [40, 10] {
                col.storage.add_revlog_entry(
                    &RevlogEntry {
                        cid: card.id,
                        ease_factor: 2500,
                        interval: 30,
                        ..revlog(RevlogReviewKind::Review, days_ago)
                    },
                    true,
                )?;
            }
            col.transact_no_undo(|col| {
                col.reconcile_fsrs_state_after_sync(HashMap::from([(
                    card.id,
                    FsrsSyncConflict::default(),
                )]))
            })?;
            col.storage
                .get_card(card.id)?
                .unwrap()
                .memory_state
                .or_invalid("no memory state")
        }

        assert_eq!(reconciled_state(0.7)?, reconciled_state(0.9)?);
        Ok(())
    }

    // Pins spec/sync.md#sync.fsrs7-state-of-foreign-cards: only a memory
    // state without FSRS-7's internal stability marks a row as foreign.
    #[test]
    fn only_rows_without_the_internal_stability_are_foreign() -> Result<()> {
        let mut col = Collection::new();
        for _ in 0..4 {
            NoteAdder::basic(&mut col).add(&mut col);
        }
        let card_ids = col.search_cards("", SortMode::NoOrder)?;
        for (card_id, data) in card_ids.iter().zip([
            r#"{"s":20.0,"d":6.0,"dr":0.9}"#,
            r#"{"s":20.0,"s_int":30.0,"d":6.0}"#,
            r#"{"s":20.0}"#,
            "",
        ]) {
            col.storage
                .db
                .execute("update cards set data = ? where id = ?", (data, card_id))?;
        }
        assert_eq!(
            col.storage.card_ids_with_foreign_fsrs_state()?,
            vec![card_ids[0]]
        );
        Ok(())
    }

    /// Valid FSRS-7 parameters that differ from the defaults: a slower decay
    /// of the slow curve component.
    fn other_fsrs7_params() -> Vec<f32> {
        let mut params = DEFAULT_PARAMETERS.to_vec();
        params[24] += 0.2;
        params
    }

    fn assert_close(actual: f32, expected: f32) {
        assert!(
            (actual - expected).abs() <= 1e-3 * expected,
            "{actual} vs {expected}"
        );
    }

    // Pins spec/scheduling.md#sched.fsrs7-sm2-conversion: the state inferred
    // from an SM-2 interval has that interval as its S90, with difficulty 5
    // and fast stability 0.8 of the internal one. The fsrs crate alone put the
    // interval into the internal stability (100 days gave an S90 of ~226).
    #[test]
    fn sm2_conversion_gives_the_interval_as_s90() -> Result<()> {
        for params in [DEFAULT_PARAMETERS.to_vec(), other_fsrs7_params()] {
            let fsrs = FSRS::new(&params)?;
            for interval in [1.0, 7.0, 100.0, 3650.0] {
                let state = memory_state_from_sm2_with_params(&fsrs, &params, 2.5, interval, 0.9)?;
                assert_close(fsrs.interval_at_retrievability(state, 0.9), interval);
                assert_eq!(state.difficulty, 5.0);
                assert_close(state.stability_fast, 0.8 * state.stability);
                assert_close(fsrs_memory_state_for_fsrs(&fsrs, state).stability, interval);
            }
        }
        // a retention other than 0.9 is met at the interval too
        let fsrs = FSRS::new(&DEFAULT_PARAMETERS)?;
        let state = memory_state_from_sm2_with_params(&fsrs, &DEFAULT_PARAMETERS, 2.5, 30.0, 0.8)?;
        assert_close(fsrs.interval_at_retrievability(state, 0.8), 30.0);
        Ok(())
    }

    // Pins spec/scheduling.md#sched.fsrs7-sm2-conversion: a truncated review
    // log whose first entry FSRS wrote (ease field under 1.1) starts from its
    // stored difficulty, and the starting state keeps the entry's interval as
    // its S90 with that difficulty.
    #[test]
    fn truncated_revlog_starting_state_keeps_the_interval_as_s90() -> Result<()> {
        let fsrs = FSRS::new(&DEFAULT_PARAMETERS)?;
        let item = fsrs_item_for_memory_state(
            &fsrs,
            &DEFAULT_PARAMETERS,
            vec![
                RevlogEntry {
                    ease_factor: 1050,
                    interval: 30,
                    ..revlog(RevlogReviewKind::Review, 40)
                },
                revlog(RevlogReviewKind::Review, 0),
            ],
            0.9,
            0.into(),
        )?
        .unwrap();
        let state = item.starting_state.unwrap();
        assert_close(state.difficulty, 9.55);
        assert_close(fsrs.interval_at_retrievability(state, 0.9), 30.0);
        Ok(())
    }

    #[test]
    fn scaling_to_an_unreachable_interval_gives_the_stability_bound() -> Result<()> {
        let fsrs = FSRS::new(&DEFAULT_PARAMETERS)?;
        let shape = MemoryState {
            stability: 10.0,
            stability_fast: 8.0,
            difficulty: 10.0,
        };
        // at difficulty 10 the default curve cannot reach 90% after 36,500 days
        let state = scale_state_to_interval(&fsrs, shape, 36_500.0, 0.9);
        assert!((state.stability - 36_500.0).abs() < 0.1, "{state:?}");
        assert!(state.stability_fast.is_finite() && state.difficulty == 10.0);
        // inputs it cannot scale are returned unchanged
        assert_eq!(scale_state_to_interval(&fsrs, shape, f32::NAN, 0.9), shape);
        assert_eq!(scale_state_to_interval(&fsrs, shape, 10.0, 1.0), shape);
        Ok(())
    }

    // Pins spec/scheduling.md#sched.fsrs7-sm2-conversion: a card RWKV-Curve
    // answers without an FSRS memory state gets the FSRS-7 state whose S90 is
    // RWKV's S90.
    #[test]
    fn fsrs_state_for_an_rwkv_s90_has_that_s90() -> Result<()> {
        let state = fsrs_memory_state_for_s90(&DEFAULT_PARAMETERS, 20.0)?;
        assert_eq!(state.stability, 20.0);
        let fsrs = FSRS::new(&DEFAULT_PARAMETERS)?;
        assert_close(fsrs.interval_at_retrievability(state.into(), 0.9), 20.0);
        Ok(())
    }

    // Pins spec/scheduling.md#sched.fsrs7-sm2-conversion: the FsrsNextInterval
    // API takes the stability it is given as the card's S90.
    #[test]
    fn next_interval_api_takes_the_s90() -> Result<()> {
        let mut col = Collection::new();
        NoteAdder::basic(&mut col).add(&mut col);
        let card_id = col.get_first_card().id;
        // at 90% the interval is the S90 itself
        assert_close(col.fsrs_next_interval_for_card(card_id, 20.0, 0.9)?, 20.0);
        // at another retention it is the interval of the state with that S90
        let fsrs = FSRS::new(&DEFAULT_PARAMETERS)?;
        let state = memory_state_from_sm2_with_params(&fsrs, &DEFAULT_PARAMETERS, 2.5, 20.0, 0.9)?;
        assert_close(
            col.fsrs_next_interval_for_card(card_id, 20.0, 0.8)?,
            fsrs.next_interval_for_state(state, 0.8),
        );
        Ok(())
    }

    fn make_review_card(col: &mut Collection, note_id: NoteId, stability: f32) -> Result<CardId> {
        let mut card = col
            .storage
            .all_cards_of_note(note_id)?
            .into_iter()
            .next()
            .or_not_found(note_id)?;
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.interval = 10;
        card.due = col.timing_today()?.days_elapsed as i32;
        card.memory_state = Some(FsrsMemoryState {
            stability,
            stability_internal: stability,
            stability_fast: None,
            difficulty: 5.0,
        });
        card.last_review_time = Some(TimestampSecs::now().adding_secs(-5 * 86_400));
        col.storage.update_card(&card)?;
        Ok(card.id)
    }

    fn set_selected_fsrs_params_for_deck(
        col: &mut Collection,
        deck_id: DeckId,
        version: FsrsVersion,
        params: Vec<f32>,
    ) -> Result<DeckConfigId> {
        let output = col.get_deck_configs_for_update(deck_id)?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: deck_id,
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            fsrs: true,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            review_fuzz_config: Default::default(),
        };
        match version {
            FsrsVersion::Six => {
                input.configs[0].inner.fsrs_version = FsrsVersion::Six as i32;
                input.configs[0].inner.fsrs_params_6 = params;
            }
            FsrsVersion::Seven => {
                input.configs[0].inner.fsrs_version = FsrsVersion::Seven as i32;
                input.configs[0].inner.fsrs_params_7 = params;
            }
            _ => unreachable!("unsupported FSRS version in test helper"),
        }
        col.update_deck_configs(input)?;
        let deck = col.get_deck(deck_id)?.or_not_found(deck_id)?;
        Ok(DeckConfigId(deck.normal()?.config_id))
    }

    fn assign_new_fsrs_config_to_deck(
        col: &mut Collection,
        deck_id: DeckId,
        version: FsrsVersion,
        params: Vec<f32>,
    ) -> Result<DeckConfigId> {
        let output = col.get_deck_configs_for_update(deck_id)?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: deck_id,
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            fsrs: true,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            review_fuzz_config: Default::default(),
        };
        let mut new_config = input.configs[0].clone();
        new_config.id = DeckConfigId(0);
        match version {
            FsrsVersion::Six => {
                new_config.inner.fsrs_version = FsrsVersion::Six as i32;
                new_config.inner.fsrs_params_6 = params;
            }
            FsrsVersion::Seven => {
                new_config.inner.fsrs_version = FsrsVersion::Seven as i32;
                new_config.inner.fsrs_params_7 = params;
            }
            _ => unreachable!("unsupported FSRS version in test helper"),
        }
        input.configs.push(new_config);
        col.update_deck_configs(input)?;
        let deck = col.get_deck(deck_id)?.or_not_found(deck_id)?;
        Ok(DeckConfigId(deck.normal()?.config_id))
    }

    fn review_entry(days_ago: i64, interval: i32, last_interval: i32) -> RevlogEntry {
        RevlogEntry {
            cid: CardId(1),
            interval,
            last_interval,
            ..revlog(RevlogReviewKind::Review, days_ago)
        }
    }

    #[test]
    fn last_revlog_info_reports_interval_scheduled_by_latest_review() {
        let entries = vec![review_entry(10, 4, 1), review_entry(3, 12, 4)];
        let info = get_last_revlog_info(&entries);
        let info = info.get(&CardId(1)).unwrap();
        assert_eq!(info.scheduled_interval, Some(12));
        assert_eq!(info.previous_interval, Some(4));
        assert_eq!(info.last_reviewed_at, Some(entries[1].id.as_secs()));
    }

    #[test]
    fn last_revlog_info_has_no_scheduled_interval_when_latest_review_is_a_learning_step() {
        let entries = vec![
            review_entry(10, 4, 1),
            RevlogEntry {
                cid: CardId(1),
                button_chosen: 1,
                interval: -600,
                last_interval: 4,
                ..revlog(RevlogReviewKind::Relearning, 3)
            },
        ];
        let info = get_last_revlog_info(&entries);
        assert_eq!(info.get(&CardId(1)).unwrap().scheduled_interval, None);
    }

    #[test]
    fn last_revlog_info_has_no_scheduled_interval_after_a_reset() {
        let entries = vec![
            review_entry(10, 4, 1),
            RevlogEntry {
                cid: CardId(1),
                ease_factor: 0,
                ..revlog(RevlogReviewKind::Manual, 3)
            },
        ];
        let info = get_last_revlog_info(&entries);
        let info = info.get(&CardId(1)).unwrap();
        assert_eq!(info.scheduled_interval, None);
        assert_eq!(info.last_reviewed_at, None);
    }

    fn timing_for_restore_tests() -> SchedTimingToday {
        let now = TimestampSecs::now();
        SchedTimingToday {
            now,
            days_elapsed: 100,
            next_day_at: now.adding_secs(3600),
        }
    }

    fn info_reviewed_days_ago(days_ago: i64, scheduled_interval: Option<u32>) -> LastRevlogInfo {
        LastRevlogInfo {
            last_reviewed_at: Some(TimestampSecs::now().adding_secs(-days_ago * 86_400)),
            previous_interval: Some(2),
            scheduled_interval,
        }
    }

    #[test]
    fn restore_review_schedule_after_sync_puts_card_where_last_review_did() {
        let mut card = Card {
            ctype: CardType::Review,
            queue: CardQueue::Review,
            interval: 5,
            due: 103,
            ..Default::default()
        };
        let timing = timing_for_restore_tests();
        assert!(restore_review_schedule_after_sync(
            &mut card,
            Some(&info_reviewed_days_ago(3, Some(12))),
            timing
        ));
        assert_eq!(card.interval, 12);
        // reviewed on day 97, so due on day 97 + 12
        assert_eq!(card.due, 109);
        // idempotent
        assert!(!restore_review_schedule_after_sync(
            &mut card,
            Some(&info_reviewed_days_ago(3, Some(12))),
            timing
        ));
    }

    #[test]
    fn restore_review_schedule_after_sync_writes_original_due_in_filtered_deck() {
        let mut card = Card {
            ctype: CardType::Review,
            queue: CardQueue::Review,
            interval: 5,
            due: -100_000,
            original_due: 103,
            original_deck_id: DeckId(1),
            deck_id: DeckId(2),
            ..Default::default()
        };
        let timing = timing_for_restore_tests();
        assert!(restore_review_schedule_after_sync(
            &mut card,
            Some(&info_reviewed_days_ago(0, Some(7))),
            timing
        ));
        assert_eq!(card.interval, 7);
        assert_eq!(card.original_due, 107);
        assert_eq!(card.due, -100_000);
    }

    #[test]
    fn restore_review_schedule_after_sync_skips_cards_it_cannot_place() {
        let timing = timing_for_restore_tests();
        let review_card = Card {
            ctype: CardType::Review,
            queue: CardQueue::Review,
            interval: 5,
            due: 103,
            ..Default::default()
        };
        // no review information at all
        let mut card = review_card.clone();
        assert!(!restore_review_schedule_after_sync(&mut card, None, timing));
        assert_eq!(card, review_card);
        // the latest review left the card in (re)learning
        let mut card = review_card.clone();
        assert!(!restore_review_schedule_after_sync(
            &mut card,
            Some(&info_reviewed_days_ago(1, None)),
            timing
        ));
        assert_eq!(card, review_card);
        // suspended
        let mut card = Card {
            queue: CardQueue::Suspended,
            ..review_card.clone()
        };
        let before = card.clone();
        assert!(!restore_review_schedule_after_sync(
            &mut card,
            Some(&info_reviewed_days_ago(1, Some(9))),
            timing
        ));
        assert_eq!(card, before);
        // not a review card
        let mut card = Card {
            ctype: CardType::Relearn,
            queue: CardQueue::Learn,
            ..review_card.clone()
        };
        let before = card.clone();
        assert!(!restore_review_schedule_after_sync(
            &mut card,
            Some(&info_reviewed_days_ago(1, Some(9))),
            timing
        ));
        assert_eq!(card, before);
    }

    // Pins spec/scheduling.md#sched.reschedule-no-revlog
    #[test]
    fn reschedule_on_change_writes_no_revlog_rows() -> Result<()> {
        let mut col = Collection::new();
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "q")?;
        col.add_note(&mut note, DeckId(1))?;
        let cid = make_review_card(&mut col, note.id, 30.0)?;
        for days_ago in [40, 20, 5] {
            col.storage.add_revlog_entry(
                &RevlogEntry {
                    ease_factor: 2500,
                    interval: 10,
                    cid,
                    ..revlog(RevlogReviewKind::Review, days_ago)
                },
                false,
            )?;
        }
        let rows_before = col.storage.get_revlog_entries_for_card(cid)?.len();
        let due_before = col.storage.get_card(cid)?.unwrap().due;

        // the reschedule choice is a stored Preferences setting
        // (spec deck-options.collection-wide-in-preferences)
        col.set_config_bool(BoolKey::FsrsReschedule, true, false)?;
        let output = col.get_deck_configs_for_update(DeckId(1))?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: DeckId(1),
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            fsrs: true,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            review_fuzz_config: Default::default(),
        };
        input.configs[0].inner.desired_retention = 0.7;
        col.update_deck_configs(input)?;

        let card = col.storage.get_card(cid)?.unwrap();
        assert_eq!(card.desired_retention, Some(0.7));
        assert_ne!(card.due, due_before, "the card was rescheduled");
        assert_eq!(
            col.storage.get_revlog_entries_for_card(cid)?.len(),
            rows_before,
            "rescheduling must not write review-log rows"
        );
        Ok(())
    }

    // Pins spec/scheduling.md#sched.rwkv-curve-s90-kept
    #[test]
    fn rwkv_curve_cards_keep_their_s90_when_fsrs7_recomputes() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, false)?;
        col.update_default_deck_config(|config| config.rwkv_review_enabled = true);
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "q")?;
        col.add_note(&mut note, DeckId(1))?;
        let cid = make_review_card(&mut col, note.id, 30.0)?;
        for days_ago in [40, 20, 5] {
            col.storage.add_revlog_entry(
                &RevlogEntry {
                    ease_factor: 2500,
                    interval: 10,
                    cid,
                    ..revlog(RevlogReviewKind::Review, days_ago)
                },
                false,
            )?;
        }
        // the S90 an RWKV-Curve answer stored
        let mut card = col.storage.get_card(cid)?.unwrap();
        col.recompute_fsrs_data_for_card(&mut card)?;
        card.memory_state.as_mut().unwrap().stability = 123.0;
        col.storage.update_card(&card)?;
        let fsrs7_state = |col: &mut Collection| -> FsrsMemoryState {
            col.compute_memory_state(cid).unwrap().state.unwrap().into()
        };
        let stored_state = |col: &Collection| {
            col.storage
                .get_card(cid)
                .unwrap()
                .unwrap()
                .memory_state
                .unwrap()
        };

        // a preset change computes the FSRS-7 fields again, not the S90
        let mut params = DEFAULT_PARAMETERS.to_vec();
        params[0] += 1.0;
        let output = col.get_deck_configs_for_update(DeckId(1))?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: DeckId(1),
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            fsrs: true,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            review_fuzz_config: Default::default(),
        };
        input.configs[0].inner.fsrs_version = FsrsVersion::Seven as i32;
        input.configs[0].inner.fsrs_params_7 = params;
        col.update_deck_configs(input)?;
        let state = stored_state(&col);
        let computed = fsrs7_state(&mut col);
        assert_eq!(state.stability, 123.0);
        // the stored state keeps three decimals
        assert!((state.difficulty - computed.difficulty).abs() < 0.001);
        assert!((state.stability_internal - computed.stability_internal).abs() < 0.001);

        // so does a move to another RWKV-Curve deck
        let curve = crate::tests::DeckAdder::new("curve")
            .with_config(|config| config.inner.rwkv_review_enabled = true)
            .add(&mut col);
        col.set_deck(&[cid], curve.id)?;
        assert_eq!(stored_state(&col).stability, 123.0);

        // under FSRS-7 the S90 is FSRS-7's own
        let fsrs = crate::tests::DeckAdder::new("fsrs")
            .with_config(|config| config.inner.rwkv_review_enabled = false)
            .add(&mut col);
        col.set_deck(&[cid], fsrs.id)?;
        let computed = fsrs7_state(&mut col);
        assert_ne!(computed.stability, 123.0);
        assert!((stored_state(&col).stability - computed.stability).abs() < 0.001);
        Ok(())
    }

    #[test]
    fn bypassed_learning_is_handled() -> Result<()> {
        // cards without any learning steps due to truncated history still have memory
        // state calculated
        let fsrs = FSRS::new(&[]).unwrap();
        let item = fsrs_item_for_memory_state(
            &fsrs,
            &[],
            vec![
                RevlogEntry {
                    ease_factor: 2500,
                    interval: 100,
                    ..revlog(RevlogReviewKind::Review, 99)
                },
                revlog(RevlogReviewKind::Review, 0),
            ],
            0.9,
            0.into(),
        )?
        .unwrap();
        assert_int_eq(
            item.starting_state.map(Into::into),
            Some(FsrsMemoryState {
                stability: 100.0,
                stability_internal: 100.0,
                stability_fast: None,
                difficulty: 5.003576,
            }),
        );
        let mut card = Card {
            reps: 1,
            ..Default::default()
        };
        card.set_memory_state(&fsrs, &[], Some(item), 0.9)?;
        assert_int_eq(
            card.memory_state,
            Some(FsrsMemoryState {
                stability: 248.9251,
                stability_internal: 248.9251,
                stability_fast: None,
                difficulty: 4.9938006,
            }),
        );
        // cards with a single review-type entry also get memory states from revlog
        // rather than card states
        let item = fsrs_item_for_memory_state(
            &fsrs,
            &[],
            vec![RevlogEntry {
                ease_factor: 2500,
                interval: 100,
                ..revlog(RevlogReviewKind::Review, 100)
            }],
            0.9,
            0.into(),
        )?
        .unwrap();
        assert!(item.item.reviews.is_empty());
        card.set_memory_state(&fsrs, &[], Some(item), 0.9)?;
        assert_int_eq(
            card.memory_state,
            Some(FsrsMemoryState {
                stability: 100.0,
                stability_internal: 100.0,
                stability_fast: None,
                difficulty: 5.003576,
            }),
        );
        Ok(())
    }

    #[test]
    fn zero_history_is_handled() -> Result<()> {
        // when the history is empty, no items are produced
        assert_eq!(convert(&[], false), None);
        // but memory state should still be inferred, by using the card's current state
        let mut card = Card {
            ctype: CardType::Review,
            interval: 100,
            ease_factor: 1300,
            reps: 1,
            ..Default::default()
        };
        card.set_memory_state(&FSRS::new(&[]).unwrap(), &[], None, 0.9)?;
        assert_int_eq(
            card.memory_state,
            Some(
                MemoryState {
                    stability: 99.999954,
                    difficulty: 10.0,
                    stability_fast: 99.999954,
                }
                .into(),
            ),
        );
        Ok(())
    }

    fn reconstructed_same_day_delta(params: &[f32]) -> Result<f32> {
        let fsrs = FSRS::new(params)?;
        let next_day_at = TimestampSecs(86_400 * 1000);
        let base = (next_day_at.0 - 86_400 + 3_600) * 1000;
        let item = fsrs_item_for_memory_state(
            &fsrs,
            params,
            vec![
                RevlogEntry {
                    id: RevlogId(base),
                    ..revlog(RevlogReviewKind::Learning, 1)
                },
                RevlogEntry {
                    id: RevlogId(base + 3_600_000),
                    ..revlog(RevlogReviewKind::Review, 1)
                },
            ],
            0.9,
            0.into(),
        )?
        .unwrap();
        Ok(item.item.reviews[1].delta_t)
    }

    fn relearning_card_1779209293223_revlogs() -> Vec<RevlogEntry> {
        let ratings = [1, 1, 3, 3, 3, 3, 3, 3, 1, 3, 3, 3, 3, 3, 3, 3];
        let elapsed_millis = [
            454_026, 1_544, 4_430, 2_798, 1_981, 3_691, 10_876, 80_546, 1_799, 36_528, 2_281,
            7_919, 265_132, 2_144, 13_635,
        ];
        let mut id = 1_779_281_655_000;
        let mut revlogs = Vec::with_capacity(ratings.len());
        for (index, rating) in ratings.into_iter().enumerate() {
            if index > 0 {
                id += elapsed_millis[index - 1];
            }
            revlogs.push(RevlogEntry {
                id: RevlogId(id),
                button_chosen: rating,
                review_kind: RevlogReviewKind::Learning,
                ..Default::default()
            });
        }
        revlogs
    }

    #[test]
    fn fsrs7_memory_state_reconstruction_preserves_reported_relearning_elapsed_time() -> Result<()>
    {
        let fsrs = FSRS::new(&DEFAULT_PARAMETERS)?;
        let revlogs = relearning_card_1779209293223_revlogs();
        let reconstructed =
            fsrs_item_for_memory_state(&fsrs, &DEFAULT_PARAMETERS, revlogs.clone(), 0.9, 0.into())?
                .unwrap();
        // The plain revlog conversion gives the same fractional elapsed time
        // (spec sched.fsrs7-only: there is no whole-day conversion any more).
        let converted = reviews_for_fsrs(revlogs, false, 0.into())
            .unwrap()
            .fsrs_items
            .pop()
            .unwrap()
            .1;

        let reconstructed_elapsed_secs = reconstructed
            .item
            .reviews
            .iter()
            .map(|review| review.delta_t)
            .sum::<f32>()
            * 86_400.0;
        let converted_elapsed_secs = converted
            .reviews
            .iter()
            .map(|review| review.delta_t)
            .sum::<f32>()
            * 86_400.0;

        assert!((reconstructed_elapsed_secs - 889.33).abs() < 0.01);
        assert!((converted_elapsed_secs - 889.33).abs() < 0.01);
        Ok(())
    }

    #[test]
    fn fsrs7_memory_state_reconstruction_uses_card_1779209293223_real_timestamps() -> Result<()> {
        let params = vec![
            0.164_769_28,
            1.781_043_3,
            3.960_628_7,
            17.227_514,
            5.775_701_5,
            0.350_562_72,
            3.297_959,
            2.193_530_6,
            0.315_878_87,
            1.232_677_5,
            0.383919,
            0.006_497_408_7,
            0.687_791_05,
            0.0,
            0.541_600_17,
            1.3993783,
            0.961_712_66,
            0.372_928_4,
            3.647_044_7,
            0.443_311_54,
            0.001,
            0.37068215,
            2.665_900_5,
            0.563_425_4,
            1.305_368_8,
            2.5,
            0.910_621_2,
            0.134_659_71,
            0.534_766_55,
            0.632_104_46,
            0.978_705_9,
            0.194_363_62,
            0.696_230_1,
            0.121_837_68,
        ];
        let fsrs = FSRS::new(&params)?;
        let revlogs: Vec<_> = [
            (1_779_280_455_399, 1, -794, 631),
            (1_779_280_909_425, 1, -76, 969),
            (1_779_280_910_969, 3, -76, 964),
            (1_779_280_915_399, 3, -76, 958),
            (1_779_280_918_197, 3, -76, 953),
            (1_779_280_920_178, 3, -76, 948),
            (1_779_280_923_869, 3, -76, 942),
            (1_779_280_934_745, 3, -76, 937),
            (1_779_281_015_291, 1, -9, 1050),
            (1_779_281_017_090, 3, -9, 1044),
            (1_779_281_053_618, 3, -49, 1038),
            (1_779_281_055_899, 3, -49, 1032),
            (1_779_281_063_818, 3, -49, 1025),
            (1_779_281_328_950, 3, -248, 1019),
            (1_779_281_331_094, 3, -248, 1013),
            (1_779_281_344_729, 3, -248, 1008),
            (1_779_281_802_797, 3, -572, 1002),
            (1_779_281_809_803, 4, 1, 960),
        ]
        .into_iter()
        .map(|(id, button_chosen, interval, ease_factor)| RevlogEntry {
            id: RevlogId(id),
            button_chosen,
            interval,
            ease_factor,
            review_kind: RevlogReviewKind::Learning,
            ..Default::default()
        })
        .collect();
        let item =
            fsrs_item_for_memory_state(&fsrs, &params, revlogs.clone(), 0.9, 0.into())?.unwrap();
        let elapsed_secs = item
            .item
            .reviews
            .iter()
            .map(|review| review.delta_t)
            .sum::<f32>()
            * 86_400.0;
        let internal = fsrs.memory_state(item.item, item.starting_state)?;
        let s90 = fsrs.interval_at_retrievability(internal, 0.9);

        assert!((elapsed_secs - 1354.404).abs() < 0.01);
        assert!((internal.stability - 0.0033932074).abs() < 1e-6);
        assert!((s90 - 0.03385916).abs() < 1e-6);
        assert!((internal.difficulty - 8.922556).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs7_memory_state_reconstruction_uses_fractional_same_day_delta() -> Result<()> {
        let delta = reconstructed_same_day_delta(&DEFAULT_PARAMETERS)?;
        assert!((delta - (1.0 / 24.0)).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn minimum_stability_uses_fsrs_floor() {
        assert_eq!(S_MIN, 0.0001);
    }

    #[test]
    fn fsrs7_sm2_conversion_handles_small_legacy_decay_slot() -> Result<()> {
        let params = vec![
            0.1558, 3.0107, 6.2423, 22.3570, 5.6837, 0.5279, 2.2999, 1.9751, 0.2886, 1.2884,
            0.8518, 0.0149, 0.7189, 0.6297, 0.3777, 2.8929, 0.9740, 0.5923, 3.6757, 0.8299, 0.0010,
            0.6994, 2.6457, 0.5673, 1.3138, 2.5067, 0.9955, 0.0499, 0.4071, 0.5686, 0.8969, 0.2210,
            0.8008, 0.0147,
        ];
        let fsrs = FSRS::new(&params)?;
        let state = memory_state_from_sm2_with_params(&fsrs, &params, 2.5, 100.0, 0.9)?;
        assert!(state.stability.is_finite());
        assert!(state.difficulty.is_finite());
        Ok(())
    }

    #[test]
    fn fsrs_math_helpers_match_inference_fsrs7() -> Result<()> {
        let params = DEFAULT_PARAMETERS.to_vec();
        let stability = 14.2;
        let elapsed_days = 21.0;
        let target_retrievability = 0.9;

        let expected = FSRS::new(&params)?.current_retrievability(
            MemoryState {
                stability,
                difficulty: 5.0,
                stability_fast: stability,
            },
            elapsed_days,
        );
        let actual = fsrs_current_retrievability_for_params(&params, stability, elapsed_days)?;
        assert!((actual - expected).abs() < 1e-6);

        let expected_interval_at_target = FSRS::new(&params)?.interval_at_retrievability(
            MemoryState {
                stability,
                difficulty: 5.0,
                stability_fast: stability,
            },
            target_retrievability,
        );
        let actual_interval_at_target =
            fsrs_interval_at_retrievability_for_params(&params, stability, target_retrievability)?;
        assert!((actual_interval_at_target - expected_interval_at_target).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs7_full_state_retrievability_preserves_fast_stability_and_difficulty() -> Result<()> {
        let params = DEFAULT_PARAMETERS.to_vec();
        let elapsed_days = 20.0;
        let first = FsrsMemoryState {
            stability: 10.0,
            stability_internal: 10.0,
            stability_fast: Some(5.0),
            difficulty: 5.0,
        };
        let second = FsrsMemoryState {
            stability_fast: Some(20.0),
            ..first
        };
        let third = FsrsMemoryState {
            difficulty: 8.0,
            ..first
        };

        let fsrs = FSRS::new(&params)?;
        let expected = fsrs.current_retrievability(first.into(), elapsed_days);
        let actual = fsrs_current_retrievability_for_state(&params, first, elapsed_days)?;
        assert!((actual - expected).abs() < 1e-6);
        assert_ne!(
            actual,
            fsrs_current_retrievability_for_state(&params, second, elapsed_days)?
        );
        assert_ne!(
            actual,
            fsrs_current_retrievability_for_state(&params, third, elapsed_days)?
        );

        let relative_overdueness =
            fsrs_relative_overdueness_for_state(&params, first, elapsed_days, 0.9)?;
        let expected_target_interval = fsrs.interval_at_retrievability(first.into(), 0.9);
        assert!((relative_overdueness + elapsed_days / expected_target_interval).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_batch_matches_singular_calls() -> Result<()> {
        let mut col = Collection::new();
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, DeckId(1))?;

        let mut card_ids = col.search_cards("", SortMode::NoOrder)?;
        card_ids.sort();
        let target_retrievability = 0.9;
        let cards = vec![(card_ids[0], 12.0), (card_ids[1], 42.0)];

        let batch = col.fsrs_interval_at_retrievability_for_cards(&cards, target_retrievability)?;
        let singular_1 =
            col.fsrs_interval_at_retrievability_for_card(card_ids[0], 12.0, target_retrievability)?;
        let singular_2 =
            col.fsrs_interval_at_retrievability_for_card(card_ids[1], 42.0, target_retrievability)?;

        assert_eq!(batch.len(), 2);
        assert!((batch[0] - singular_1).abs() < 1e-6);
        assert!((batch[1] - singular_2).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_batch_uses_overlay_presets() -> Result<()> {
        let mut col = Collection::new();
        let deck_params = DEFAULT_PARAMETERS.to_vec();
        set_selected_fsrs_params_for_deck(
            &mut col,
            DeckId(1),
            FsrsVersion::Seven,
            deck_params.clone(),
        )?;
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut tagged_note = nt.new_note();
        let mut fallback_note = nt.new_note();
        col.add_note(&mut tagged_note, DeckId(1))?;
        col.add_note(&mut fallback_note, DeckId(1))?;
        col.add_tags_to_notes(&[tagged_note.id], "medical")?;

        let addon_params = other_fsrs7_params();
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:medical".into(),
                    name: "Medical".into(),
                    fsrs_version: AddonFsrsVersion::Seven,
                    params: addon_params.clone(),
                    desired_retention: 0.81,
                    historical_retention: 0.71,
                    ignore_revlogs_before_date: String::new(),
                }],
                rules: vec![crate::scheduler::fsrs::preset::FsrsPresetRule {
                    search: "tag:medical".into(),
                    preset_id: "addon:test:medical".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;

        let cids = col.search_cards("", SortMode::NoOrder)?;
        let cards = col.all_cards_for_ids(&cids, false)?;
        let tagged_card = cards
            .iter()
            .find(|card| card.note_id == tagged_note.id)
            .or_not_found(tagged_note.id)?;
        let fallback_card = cards
            .iter()
            .find(|card| card.note_id == fallback_note.id)
            .or_not_found(fallback_note.id)?;
        let target_retrievability = 0.9;

        let intervals = col.fsrs_interval_at_retrievability_for_cards(
            &[(tagged_card.id, 12.0), (fallback_card.id, 24.0)],
            target_retrievability,
        )?;
        let expected_tagged =
            fsrs_interval_at_retrievability_for_params(&addon_params, 12.0, target_retrievability)?;
        let expected_fallback =
            fsrs_interval_at_retrievability_for_params(&deck_params, 24.0, target_retrievability)?;

        assert_eq!(intervals.len(), 2);
        assert!((intervals[0] - expected_tagged).abs() < 1e-6);
        assert!((intervals[1] - expected_fallback).abs() < 1e-6);
        require!(
            expected_tagged != expected_fallback,
            "test requires distinct presets"
        );
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_variable_batch_uses_item_targets() -> Result<()> {
        let mut col = Collection::new();
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, DeckId(1))?;

        let mut card_ids = col.search_cards("", SortMode::NoOrder)?;
        card_ids.sort();
        let cards = vec![(card_ids[0], 12.0, 0.9), (card_ids[1], 12.0, 0.8)];

        let intervals = col.fsrs_interval_at_retrievability_for_card_targets(&cards)?;
        let singular_1 = col.fsrs_interval_at_retrievability_for_card(card_ids[0], 12.0, 0.9)?;
        let singular_2 = col.fsrs_interval_at_retrievability_for_card(card_ids[1], 12.0, 0.8)?;

        assert_eq!(intervals.len(), 2);
        assert!((intervals[0] - singular_1).abs() < 1e-6);
        assert!((intervals[1] - singular_2).abs() < 1e-6);
        require!(
            intervals[0] != intervals[1],
            "test requires distinct targets"
        );
        Ok(())
    }

    #[test]
    fn fsrs_desired_retention_for_intervals_uses_overlay_presets() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, false)?;
        col.update_default_deck_config(|config| {
            config.fsrs_version = FsrsVersion::Seven as i32;
            config.fsrs_params_7 = DEFAULT_PARAMETERS.to_vec();
            config.desired_retention = 0.65;
        });
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:medical".into(),
                    name: "Medical".into(),
                    fsrs_version: AddonFsrsVersion::Seven,
                    params: DEFAULT_PARAMETERS.to_vec(),
                    desired_retention: 0.9,
                    historical_retention: 0.9,
                    ignore_revlogs_before_date: String::new(),
                }],
                rules: vec![FsrsPresetRule {
                    search: "tag:medical".into(),
                    preset_id: "addon:test:medical".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut tagged_note = nt.new_note();
        let mut fallback_note = nt.new_note();
        col.add_note(&mut tagged_note, DeckId(1))?;
        col.add_note(&mut fallback_note, DeckId(1))?;
        col.add_tags_to_notes(&[tagged_note.id], "medical")?;
        let tagged_card_id = make_review_card(&mut col, tagged_note.id, 10.0)?;
        let fallback_card_id = make_review_card(&mut col, fallback_note.id, 20.0)?;

        let batch = col.fsrs_desired_retention_for_intervals(&[
            (tagged_card_id, 0.9),
            (fallback_card_id, 0.85),
        ])?;

        assert_eq!(batch.len(), 2);
        assert!((batch[0].interval_target_desired_retention - 0.9).abs() < 1e-6);
        assert!((batch[1].interval_target_desired_retention - 0.85).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_by_config_batch_uses_fsrs7_config() -> Result<()> {
        let mut col = Collection::new();
        let params_7 = DEFAULT_PARAMETERS.to_vec();
        let config_id = set_selected_fsrs_params_for_deck(
            &mut col,
            DeckId(1),
            FsrsVersion::Seven,
            params_7.clone(),
        )?;
        let target_retrievability = 0.9;
        let stability = 17.3;

        let actual = col.fsrs_interval_at_retrievability_for_configs(
            &[(config_id, stability)],
            target_retrievability,
        )?[0];
        let expected = fsrs_interval_at_retrievability_for_params(
            &params_7,
            stability,
            target_retrievability,
        )?;
        assert!((actual - expected).abs() < 1e-6);
        Ok(())
    }

    // Pins spec/scheduling.md#sched.fsrs7-only: a preset whose stored version
    // is FSRS-6, with FSRS-6 parameters and no FSRS-7 ones, runs the FSRS-7
    // defaults.
    #[test]
    fn fsrs_interval_at_retrievability_by_config_batch_runs_fsrs7_defaults_for_fsrs6_config(
    ) -> Result<()> {
        let mut col = Collection::new();
        let params_6 = FSRS6_DEFAULT_PARAMETERS.to_vec();
        let config_id = set_selected_fsrs_params_for_deck(
            &mut col,
            DeckId(1),
            FsrsVersion::Six,
            params_6.clone(),
        )?;
        let target_retrievability = 0.9;
        let stability = 11.2;

        let actual = col.fsrs_interval_at_retrievability_for_configs(
            &[(config_id, stability)],
            target_retrievability,
        )?[0];
        let expected = fsrs_interval_at_retrievability_for_params(
            &DEFAULT_PARAMETERS,
            stability,
            target_retrievability,
        )?;
        assert!((actual - expected).abs() < 1e-6);
        let config = col.get_deck_config(config_id, false)?.unwrap();
        assert_eq!(config.inner.fsrs_params_6, params_6);
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_by_config_batch_supports_mixed_configs() -> Result<()> {
        let mut col = Collection::new();
        let params_7 = DEFAULT_PARAMETERS.to_vec();
        let other_params = other_fsrs7_params();
        let config_1 = set_selected_fsrs_params_for_deck(
            &mut col,
            DeckId(1),
            FsrsVersion::Seven,
            params_7.clone(),
        )?;
        let second_deck = col.get_or_create_normal_deck("second-config-batch")?;
        let config_2 = assign_new_fsrs_config_to_deck(
            &mut col,
            second_deck.id,
            FsrsVersion::Seven,
            other_params.clone(),
        )?;
        require!(config_1 != config_2, "test requires different config ids");

        let target_retrievability = 0.9;
        let actual = col.fsrs_interval_at_retrievability_for_configs(
            &[(config_1, 20.0), (config_2, 20.0)],
            target_retrievability,
        )?;
        let expected_1 =
            fsrs_interval_at_retrievability_for_params(&params_7, 20.0, target_retrievability)?;
        let expected_2 =
            fsrs_interval_at_retrievability_for_params(&other_params, 20.0, target_retrievability)?;
        assert_eq!(actual.len(), 2);
        assert!((actual[0] - expected_1).abs() < 1e-6);
        assert!((actual[1] - expected_2).abs() < 1e-6);
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_by_config_batch_preserves_order_with_duplicates(
    ) -> Result<()> {
        let mut col = Collection::new();
        let params_7 = DEFAULT_PARAMETERS.to_vec();
        let other_params = other_fsrs7_params();
        let config_1 = set_selected_fsrs_params_for_deck(
            &mut col,
            DeckId(1),
            FsrsVersion::Seven,
            params_7.clone(),
        )?;
        let second_deck = col.get_or_create_normal_deck("second-config-duplicates")?;
        let config_2 = assign_new_fsrs_config_to_deck(
            &mut col,
            second_deck.id,
            FsrsVersion::Seven,
            other_params.clone(),
        )?;
        let target_retrievability = 0.9;
        let request = vec![
            (config_2, 8.0),
            (config_1, 12.0),
            (config_2, 21.0),
            (config_1, 12.0),
        ];
        let actual =
            col.fsrs_interval_at_retrievability_for_configs(&request, target_retrievability)?;
        let expected = vec![
            fsrs_interval_at_retrievability_for_params(&other_params, 8.0, target_retrievability)?,
            fsrs_interval_at_retrievability_for_params(&params_7, 12.0, target_retrievability)?,
            fsrs_interval_at_retrievability_for_params(&other_params, 21.0, target_retrievability)?,
            fsrs_interval_at_retrievability_for_params(&params_7, 12.0, target_retrievability)?,
        ];
        assert_eq!(actual.len(), expected.len());
        for (a, e) in actual.into_iter().zip(expected) {
            assert!((a - e).abs() < 1e-6);
        }
        Ok(())
    }

    #[test]
    fn fsrs_interval_at_retrievability_by_config_batch_matches_card_helper() -> Result<()> {
        let mut col = Collection::new();
        let params_7 = DEFAULT_PARAMETERS.to_vec();
        let other_params = other_fsrs7_params();
        let config_1 =
            set_selected_fsrs_params_for_deck(&mut col, DeckId(1), FsrsVersion::Seven, params_7)?;
        let second_deck = col.get_or_create_normal_deck("second-config-parity")?;
        let config_2 = assign_new_fsrs_config_to_deck(
            &mut col,
            second_deck.id,
            FsrsVersion::Seven,
            other_params,
        )?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, second_deck.id)?;
        let mut card_ids = col.search_cards("", SortMode::NoOrder)?;
        card_ids.sort();
        let card_1 = card_ids[0];
        let card_2 = card_ids[1];

        let target_retrievability = 0.9;
        let stability_1 = 16.0;
        let stability_2 = 24.0;
        let by_config = col.fsrs_interval_at_retrievability_for_configs(
            &[(config_1, stability_1), (config_2, stability_2)],
            target_retrievability,
        )?;
        let by_card_1 = col.fsrs_interval_at_retrievability_for_card(
            card_1,
            stability_1,
            target_retrievability,
        )?;
        let by_card_2 = col.fsrs_interval_at_retrievability_for_card(
            card_2,
            stability_2,
            target_retrievability,
        )?;
        assert_eq!(by_config.len(), 2);
        assert!((by_config[0] - by_card_1).abs() < 1e-6);
        assert!((by_config[1] - by_card_2).abs() < 1e-6);
        Ok(())
    }

    mod update_memory_state {
        use super::*;

        #[test]
        fn no_req_clears_fsrs_data() -> Result<()> {
            let mut col = Collection::new();
            let nt = col.get_notetype_by_name("Basic")?.unwrap();
            let mut note1 = nt.new_note();
            col.add_note(&mut note1, DeckId(1))?;
            let mut card = col
                .storage
                .all_cards_of_note(note1.id)?
                .into_iter()
                .next()
                .unwrap();
            let card_id = card.id;
            // Make the card not new
            card.ctype = CardType::Review;
            card.interval = 1;
            // Set FSRS parameters
            card.memory_state = Some(FsrsMemoryState {
                stability: 1.0,
                stability_internal: 1.0,
                stability_fast: None,
                difficulty: 1.0,
            });
            card.desired_retention = Some(0.123);
            card.decay = Some(0.456);

            col.storage.update_card(&card)?;

            // Add a revlog entry so the card is found within update_memory_state
            let mut rev = revlog(RevlogReviewKind::Review, 1);
            rev.cid = card_id;
            col.storage.add_revlog_entry(&rev, false)?;

            let entry = UpdateMemoryStateEntry {
                req: None,
                search: Node::Search(SearchNode::WholeCollection),
                ignore_before: TimestampMillis(0),
                preset_name: "Default".to_string(),
                current_preset: 1,
                total_presets: 1,
            };
            col.transact(Op::UpdateDeckConfig, |col| {
                col.update_memory_state(vec![entry]).unwrap();
                Ok(())
            })
            .unwrap();

            let card = col.storage.get_card(card_id)?.unwrap();
            assert_eq!(card.memory_state, None);
            assert_eq!(card.desired_retention, None);
            assert_eq!(card.decay, None);

            Ok(())
        }
    }
}
