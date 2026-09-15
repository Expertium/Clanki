// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Advance and Postpone, ported from the FSRS Helper add-on (spec
//! sched.advance, sched.postpone, sched.advance-postpone-algorithm).
//!
//! Advance brings review cards that are not due yet forward to today;
//! Postpone moves due review cards a little into the future. Both take the
//! cards in the add-on's order (the ones that lose least first) and change
//! only their due date and interval: no memory state and no review-log row.

use std::collections::HashMap;

use fsrs::MemoryState;
use fsrs::FSRS;

use crate::card::CardQueue;
use crate::card::CardType;
use crate::deckconfig::algorithm::SchedulingAlgorithm;
use crate::prelude::*;
use crate::rwkv::StoredCurve;
use crate::scheduler::answering::get_fuzz_seed;
use crate::scheduler::fsrs::curve::Fsrs7Curve;
use crate::scheduler::fsrs::preset::FsrsPresetId;
use crate::scheduler::fsrs::rescheduler::fuzzed_interval_days;
use crate::scheduler::fsrs::rescheduler::Rescheduler;
use crate::search::JoinSearches;
use crate::search::SearchNode;
use crate::search::StateKind;
use crate::storage::comma_separated_ids;

/// Advance: a card whose remaining share of its target interval is under
/// this is "relatively safe" to advance (the add-on's threshold).
const ADVANCE_SAFE_REMAINING: f32 = 0.13;
/// Postpone: a card whose elapsed time after the postponement exceeds its
/// target interval by less than this share is "relatively safe" to postpone.
const POSTPONE_SAFE_OVERDUE: f32 = 0.15;
/// Postpone extends a card's interval by this share of it (the middle of the
/// add-on's random 5-10%), counted from today.
const POSTPONE_EXTENSION: f32 = 0.075;
/// The target interval is searched up to FSRS's stability bound.
const TARGET_INTERVAL_MAX_DAYS: u32 = 36_500;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum AdvancePostponeMode {
    Advance,
    Postpone,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum AdvancePostponeScope {
    Collection,
    /// The deck and its subdecks (cards in a filtered deck by their home deck).
    Deck(DeckId),
    Cards(Vec<CardId>),
}

pub(crate) struct AdvancePostponeRequest {
    pub mode: AdvancePostponeMode,
    pub scope: AdvancePostponeScope,
    /// RWKV-Curve's stored curve of each card that has one; used only while
    /// the collection runs RWKV-Curve.
    pub rwkv_curves: HashMap<CardId, StoredCurve>,
}

/// The review cards of a scope on the mode's side of today, before their
/// curves are known: the cards whose RWKV-Curve curves the caller must send.
#[derive(Debug, Default, PartialEq)]
pub(crate) struct AdvancePostponeCandidates {
    pub card_ids: Vec<CardId>,
    /// The collection runs RWKV-Curve: the preview and the move need each
    /// card's stored curve.
    pub rwkv_curve: bool,
}

#[derive(Debug, Default, PartialEq)]
pub(crate) struct AdvancePostponePreview {
    /// The cards in the order Advance or Postpone takes them.
    pub card_ids: Vec<CardId>,
    /// For each of them, its retrievability on the day it would be reviewed
    /// without the move, and on its new due day.
    pub retrievability_before: Vec<f32>,
    pub retrievability_after: Vec<f32>,
    /// How many of the first cards are "relatively safe" to move.
    pub safe_count: u32,
    /// Cards left out: the algorithm has no forgetting curve for them yet.
    pub without_curve: u32,
    /// Postpone: cards left out because they already reach the maximum
    /// interval.
    pub at_maximum_interval: u32,
}

#[derive(Debug, Default, PartialEq)]
pub(crate) struct AdvancePostponeOutcome {
    pub count: u32,
    /// Mean retrievability of the moved cards before and after the move.
    pub retrievability_before: f32,
    pub retrievability_after: f32,
    pub without_curve: u32,
    pub at_maximum_interval: u32,
}

/// A card's forgetting curve in the collection's algorithm, never another
/// algorithm's (spec sched.advance-postpone-algorithm).
enum CardCurve {
    Fsrs7 {
        curve: Fsrs7Curve,
        state: MemoryState,
    },
    RwkvCurve(StoredCurve),
}

impl CardCurve {
    /// Retrievability `days` after the card's last review.
    fn recall(&self, days: f32) -> Option<f32> {
        match self {
            CardCurve::Fsrs7 { curve, state } => curve.retrievability(*state, days),
            CardCurve::RwkvCurve(curve) => Some(curve.recall(days)),
        }
        .filter(|recall| recall.is_finite())
    }
}

struct Candidate {
    card: Card,
    deckconfig_id: DeckConfigId,
    max_interval: u32,
    /// Whole days since the last review; its day is `today - days_elapsed`.
    days_elapsed: u32,
    /// The due day, from the original due of a card in a filtered deck.
    due: i32,
    /// Where the card's curve meets its desired retention, unrounded days.
    target_interval: f32,
    curve: CardCurve,
    key: f32,
}

impl Candidate {
    /// Advance: due today, or tomorrow for a card reviewed today.
    fn advanced_interval(&self) -> u32 {
        self.days_elapsed.max(1)
    }

    /// Postpone: the unrounded interval, the elapsed days plus 7.5% of the
    /// card's interval.
    fn postponed_interval(&self) -> f32 {
        self.days_elapsed as f32 + POSTPONE_EXTENSION * self.card.interval as f32
    }

    /// The add-on's order and safety measure: for Advance, the share of the
    /// target interval still to go; for Postpone, how far the elapsed time
    /// after the postponement exceeds the target interval.
    fn key(mode: AdvancePostponeMode, days_elapsed: u32, interval: u32, target: f32) -> f32 {
        let target = target.max(1.0);
        match mode {
            AdvancePostponeMode::Advance => 1.0 - days_elapsed as f32 / target,
            AdvancePostponeMode::Postpone => {
                (days_elapsed as f32 + POSTPONE_EXTENSION * interval as f32) / target - 1.0
            }
        }
    }

    fn is_safe(&self, mode: AdvancePostponeMode) -> bool {
        match mode {
            AdvancePostponeMode::Advance => self.key < ADVANCE_SAFE_REMAINING,
            AdvancePostponeMode::Postpone => self.key < POSTPONE_SAFE_OVERDUE,
        }
    }

    /// Retrievability on the day the card would be reviewed without the
    /// move (its due day; today for a due card), and on its new due day
    /// given as days after the last review.
    fn retrievability(&self, today: i32, new_interval: f32) -> Option<(f32, f32)> {
        let last_review_day = today - self.days_elapsed as i32;
        let review_day = self.due.max(today);
        Some((
            self.curve.recall((review_day - last_review_day) as f32)?,
            self.curve.recall(new_interval)?,
        ))
    }
}

struct Candidates {
    /// In the order Advance or Postpone takes them.
    ordered: Vec<Candidate>,
    without_curve: u32,
    at_maximum_interval: u32,
    today: i32,
}

impl Collection {
    pub(crate) fn advance_postpone_candidates(
        &mut self,
        mode: AdvancePostponeMode,
        scope: &AdvancePostponeScope,
    ) -> Result<AdvancePostponeCandidates> {
        let algorithm = self.advance_postpone_algorithm()?;
        let today = self.timing_today()?.days_elapsed as i32;
        Ok(AdvancePostponeCandidates {
            card_ids: self
                .advance_postpone_cards(mode, scope, today)?
                .iter()
                .map(|card| card.id)
                .collect(),
            rwkv_curve: algorithm == SchedulingAlgorithm::RwkvCurve,
        })
    }

    pub(crate) fn preview_advance_postpone(
        &mut self,
        request: &AdvancePostponeRequest,
    ) -> Result<AdvancePostponePreview> {
        let candidates = self.advance_postpone_candidates_in_order(request)?;
        let mut preview = AdvancePostponePreview {
            without_curve: candidates.without_curve,
            at_maximum_interval: candidates.at_maximum_interval,
            ..Default::default()
        };
        for candidate in &candidates.ordered {
            let new_interval = match request.mode {
                AdvancePostponeMode::Advance => candidate.advanced_interval() as f32,
                AdvancePostponeMode::Postpone => candidate.postponed_interval(),
            };
            // a candidate has a usable curve, so both are there
            let (before, after) = candidate
                .retrievability(candidates.today, new_interval)
                .unwrap_or_default();
            preview.card_ids.push(candidate.card.id);
            preview.retrievability_before.push(before);
            preview.retrievability_after.push(after);
            if candidate.is_safe(request.mode) {
                preview.safe_count += 1;
            }
        }
        Ok(preview)
    }

    /// Moves every candidate of the request's scope, in order, as one
    /// undoable step that writes no review-log row (spec
    /// sched.reschedule-no-revlog).
    pub(crate) fn advance_postpone(
        &mut self,
        request: &AdvancePostponeRequest,
    ) -> Result<OpOutput<AdvancePostponeOutcome>> {
        let op = match request.mode {
            AdvancePostponeMode::Advance => Op::AdvanceCards,
            AdvancePostponeMode::Postpone => Op::PostponeCards,
        };
        let usn = self.usn()?;
        self.transact(op, |col| {
            let candidates = col.advance_postpone_candidates_in_order(request)?;
            let today = candidates.today;
            let mut rescheduler = (request.mode == AdvancePostponeMode::Postpone
                && col.get_config_bool(BoolKey::LoadBalancerEnabled))
            .then(|| Rescheduler::new(col))
            .transpose()?;
            let review_fuzz_config = col.review_fuzz_config();
            let mut outcome = AdvancePostponeOutcome {
                without_curve: candidates.without_curve,
                at_maximum_interval: candidates.at_maximum_interval,
                ..Default::default()
            };
            let (mut before_sum, mut after_sum) = (0.0, 0.0);
            for candidate in candidates.ordered {
                let interval = match request.mode {
                    AdvancePostponeMode::Advance => candidate.advanced_interval(),
                    AdvancePostponeMode::Postpone => fuzzed_interval_days(
                        rescheduler.as_ref(),
                        candidate.postponed_interval(),
                        // never today or earlier
                        candidate.days_elapsed + 1,
                        candidate.max_interval,
                        candidate.days_elapsed,
                        candidate.deckconfig_id,
                        get_fuzz_seed(&candidate.card, true),
                        review_fuzz_config,
                    ),
                };
                let new_due = today - candidate.days_elapsed as i32 + interval as i32;
                if let Some((before, after)) = candidate.retrievability(today, interval as f32) {
                    before_sum += before;
                    after_sum += after;
                }
                if let Some(rescheduler) = &mut rescheduler {
                    rescheduler.update_due_cnt_per_day(
                        candidate.due,
                        new_due,
                        candidate.deckconfig_id,
                    );
                }
                let mut card = candidate.card;
                let original = card.clone();
                card.interval = interval;
                if card.is_filtered() {
                    card.original_due = new_due;
                } else {
                    card.due = new_due;
                }
                col.update_card_inner(&mut card, original, usn)?;
                outcome.count += 1;
            }
            if outcome.count > 0 {
                outcome.retrievability_before = before_sum / outcome.count as f32;
                outcome.retrievability_after = after_sum / outcome.count as f32;
            }
            Ok(outcome)
        })
    }

    /// The collection's algorithm; RWKV-Instant has no due dates to move.
    fn advance_postpone_algorithm(&self) -> Result<SchedulingAlgorithm> {
        let algorithm = self.effective_scheduling_algorithm()?;
        require!(
            algorithm != SchedulingAlgorithm::RwkvInstant,
            "Advance and Postpone are not available under RWKV-Instant"
        );
        Ok(algorithm)
    }

    /// The review cards of `scope` (not suspended or buried) on the mode's
    /// side of today, with their last review time where the review log has
    /// it.
    fn advance_postpone_cards(
        &mut self,
        mode: AdvancePostponeMode,
        scope: &AdvancePostponeScope,
        today: i32,
    ) -> Result<Vec<Card>> {
        let search = match scope {
            AdvancePostponeScope::Collection => SearchBuilder::from(StateKind::Review),
            AdvancePostponeScope::Deck(deck_id) => {
                let deck = self.get_deck(*deck_id)?.or_not_found(*deck_id)?;
                let deck_ids = self.storage.deck_id_with_children(&deck)?;
                SearchBuilder::from_decks(&deck_ids).and(StateKind::Review)
            }
            AdvancePostponeScope::Cards(card_ids) => {
                SearchNode::CardIds(comma_separated_ids(card_ids)).and(StateKind::Review)
            }
        };
        let mut cards: Vec<Card> = self
            .all_cards_for_search(search)?
            .into_iter()
            .filter(|card| {
                card.ctype == CardType::Review
                    && card.queue == CardQueue::Review
                    && match mode {
                        AdvancePostponeMode::Advance => card.original_or_current_due() > today,
                        AdvancePostponeMode::Postpone => card.original_or_current_due() <= today,
                    }
            })
            .collect();
        let missing: Vec<CardId> = cards
            .iter()
            .filter(|card| card.last_review_time.is_none())
            .map(|card| card.id)
            .collect();
        let review_times = self.storage.times_of_last_review(&missing)?;
        for card in &mut cards {
            if card.last_review_time.is_none() {
                card.last_review_time = review_times.get(&card.id).copied();
            }
        }
        Ok(cards)
    }

    fn advance_postpone_candidates_in_order(
        &mut self,
        request: &AdvancePostponeRequest,
    ) -> Result<Candidates> {
        let algorithm = self.advance_postpone_algorithm()?;
        let timing = self.timing_today()?;
        let today = timing.days_elapsed as i32;
        let cards = self.advance_postpone_cards(request.mode, &request.scope, today)?;
        let presets = self.fsrs_presets_for_cards(&cards)?;
        let decks = self.storage.get_decks_map()?;
        let configs = self.storage.get_deck_config_map()?;
        let mut models: HashMap<FsrsPresetId, (Fsrs7Curve, FSRS)> = HashMap::new();
        let mut candidates = Candidates {
            ordered: Vec::with_capacity(cards.len()),
            without_curve: 0,
            at_maximum_interval: 0,
            today,
        };
        for card in cards {
            let due = card.original_or_current_due();
            let days_elapsed = match card.last_review_time {
                Some(last_review) => timing.next_day_at.elapsed_days_since(last_review) as u32,
                None => (today - (due - card.interval as i32)).max(0) as u32,
            };
            if request.mode == AdvancePostponeMode::Advance
                && today - days_elapsed as i32 + days_elapsed.max(1) as i32 >= due
            {
                // due tomorrow after a review today: nothing to bring forward
                continue;
            }
            let deckconfig_id = decks
                .get(&card.original_or_current_deck_id())
                .and_then(|deck| deck.config_id())
                .unwrap_or(DeckConfigId(1));
            let max_interval = configs
                .get(&deckconfig_id)
                .map(|config| config.inner.maximum_review_interval.max(1))
                .unwrap_or(TARGET_INTERVAL_MAX_DAYS);
            if request.mode == AdvancePostponeMode::Postpone && days_elapsed + 1 > max_interval {
                candidates.at_maximum_interval += 1;
                continue;
            }
            let Some(preset) = presets.get(&card.id) else {
                candidates.without_curve += 1;
                continue;
            };
            let target_retention = card
                .desired_retention
                .filter(|retention| retention.is_finite() && *retention > 0.0 && *retention < 1.0)
                .unwrap_or(preset.desired_retention);
            let curve_and_target = match algorithm {
                SchedulingAlgorithm::RwkvCurve => {
                    request.rwkv_curves.get(&card.id).and_then(|curve| {
                        let target = curve.crossing(target_retention, TARGET_INTERVAL_MAX_DAYS)?;
                        Some((CardCurve::RwkvCurve(curve.clone()), target))
                    })
                }
                _ => match card.memory_state {
                    Some(memory_state) => {
                        if !models.contains_key(&preset.id) {
                            // the preset's effective FSRS-7 parameters
                            // (spec sched.fsrs7-only)
                            let curve = Fsrs7Curve::new(&preset.params)
                                .or_invalid("invalid FSRS-7 parameters")?;
                            models.insert(preset.id.clone(), (curve, preset.fsrs()?));
                        }
                        let (curve, fsrs) = &models[&preset.id];
                        let state: MemoryState = memory_state.into();
                        let target = fsrs.interval_at_retrievability(state, target_retention);
                        Some((
                            CardCurve::Fsrs7 {
                                curve: curve.clone(),
                                state,
                            },
                            target,
                        ))
                    }
                    None => None,
                },
            };
            let Some((curve, target_interval)) =
                curve_and_target.filter(|(_, target)| target.is_finite() && *target > 0.0)
            else {
                candidates.without_curve += 1;
                continue;
            };
            let key = Candidate::key(request.mode, days_elapsed, card.interval, target_interval);
            let candidate = Candidate {
                card,
                deckconfig_id,
                max_interval,
                days_elapsed,
                due,
                target_interval,
                curve,
                key,
            };
            if candidate
                .retrievability(today, days_elapsed as f32)
                .is_none()
            {
                candidates.without_curve += 1;
                continue;
            }
            candidates.ordered.push(candidate);
        }
        // the add-on's order; among equal keys, the longer target interval
        // (the more stable card) first
        candidates.ordered.sort_by(|a, b| {
            a.key
                .total_cmp(&b.key)
                .then(b.target_interval.total_cmp(&a.target_interval))
                .then(a.card.id.cmp(&b.card.id))
        });
        Ok(candidates)
    }
}

impl TryFrom<anki_proto::scheduler::AdvancePostponeRequest> for AdvancePostponeRequest {
    type Error = AnkiError;

    fn try_from(input: anki_proto::scheduler::AdvancePostponeRequest) -> Result<Self> {
        use anki_proto::scheduler::advance_postpone_request::Mode;
        let mode = match input.mode() {
            Mode::Advance => AdvancePostponeMode::Advance,
            Mode::Postpone => AdvancePostponeMode::Postpone,
        };
        let scope = if !input.card_ids.is_empty() {
            AdvancePostponeScope::Cards(input.card_ids.into_iter().map(CardId).collect())
        } else if let Some(deck_id) = input.deck_id {
            AdvancePostponeScope::Deck(DeckId(deck_id))
        } else {
            AdvancePostponeScope::Collection
        };
        let rwkv_curves =
            crate::rwkv::unpack_stored_curves(&input.rwkv_curve_card_ids, &input.rwkv_curves)
                .or_invalid("invalid RWKV-Curve curves")?
                .into_iter()
                .map(|(card_id, curve)| (CardId(card_id), curve))
                .collect();
        Ok(Self {
            mode,
            scope,
            rwkv_curves,
        })
    }
}

impl From<AdvancePostponeCandidates> for anki_proto::scheduler::AdvancePostponeCandidatesResponse {
    fn from(candidates: AdvancePostponeCandidates) -> Self {
        Self {
            card_ids: candidates.card_ids.into_iter().map(|id| id.0).collect(),
            rwkv_curve: candidates.rwkv_curve,
        }
    }
}

impl From<AdvancePostponePreview> for anki_proto::scheduler::AdvancePostponePreview {
    fn from(preview: AdvancePostponePreview) -> Self {
        Self {
            card_ids: preview.card_ids.into_iter().map(|id| id.0).collect(),
            retrievability_before: preview.retrievability_before,
            retrievability_after: preview.retrievability_after,
            safe_count: preview.safe_count,
            without_curve: preview.without_curve,
            at_maximum_interval: preview.at_maximum_interval,
        }
    }
}

impl From<OpOutput<AdvancePostponeOutcome>> for anki_proto::scheduler::AdvancePostponeResponse {
    fn from(out: OpOutput<AdvancePostponeOutcome>) -> Self {
        Self {
            changes: Some(out.changes.into()),
            count: out.output.count,
            retrievability_before: out.output.retrievability_before,
            retrievability_after: out.output.retrievability_after,
            without_curve: out.output.without_curve,
            at_maximum_interval: out.output.at_maximum_interval,
        }
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::card::FsrsMemoryState;
    use crate::config::ConfigKey;
    use crate::notes::NoteId;

    const DAY: i64 = 86_400;

    struct Setup {
        col: Collection,
        today: i32,
    }

    impl Setup {
        fn new(algorithm: SchedulingAlgorithm) -> Self {
            let mut col = Collection::new();
            col.set_config(ConfigKey::SchedulingAlgorithm, &algorithm)
                .unwrap();
            col.update_default_deck_config(|config| {
                config.desired_retention = 0.9;
                config.maximum_review_interval = 36_500;
            });
            let today = col.timing_today().unwrap().days_elapsed as i32;
            Self { col, today }
        }

        /// A review card last reviewed `elapsed` days ago, due in `due_in`
        /// days, with FSRS-7 stabilities `stability` (fast 0.8 of it).
        fn card(&mut self, elapsed: i64, due_in: i32, stability: f32) -> CardId {
            let mut card = Card::new(NoteId(10), 0, DeckId(1), self.today + due_in);
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.interval = (elapsed as i32 + due_in).max(1) as u32;
            card.memory_state = Some(FsrsMemoryState {
                stability,
                stability_internal: stability,
                stability_fast: Some(stability * 0.8),
                difficulty: 5.0,
            });
            let next_day_at = self.col.timing_today().unwrap().next_day_at;
            // the middle of the day `elapsed` days before today
            card.last_review_time = Some(next_day_at.adding_secs(-elapsed * DAY - DAY / 2));
            self.col.add_card(&mut card).unwrap();
            card.id
        }

        fn request(&self, mode: AdvancePostponeMode) -> AdvancePostponeRequest {
            AdvancePostponeRequest {
                mode,
                scope: AdvancePostponeScope::Collection,
                rwkv_curves: HashMap::new(),
            }
        }

        fn due_and_interval(&self, card_id: CardId) -> (i32, u32) {
            let card = self.col.storage.get_card(card_id).unwrap().unwrap();
            (card.due - self.today, card.interval)
        }
    }

    // Pins spec/scheduling.md#sched.advance: under FSRS-7, Advance takes the
    // cards that are not due yet whose share of the target interval still
    // to go is smallest first, calls those under 13% safe, and makes the
    // chosen ones due today with the days since their last review as their
    // interval.
    #[test]
    fn advance_takes_the_cards_closest_to_their_target_first_and_makes_them_due_today() {
        let mut s = Setup::new(SchedulingAlgorithm::Fsrs7);
        // the target interval at the desired retention (0.9) of the state
        // every card below has, with the preset's FSRS-7 parameters
        let target = FSRS::new(&fsrs::DEFAULT_PARAMETERS)
            .unwrap()
            .interval_at_retrievability(
                MemoryState {
                    stability: 20.0,
                    stability_fast: 16.0,
                    difficulty: 5.0,
                },
                0.9,
            )
            .round() as i64;
        assert!(target > 10, "{target}");
        // 1 day to go (safe), half the interval to go, and just reviewed
        let almost = s.card(target - 1, 1, 20.0);
        let halfway = s.card(target / 2, (target - target / 2) as i32, 20.0);
        let just_reviewed = s.card(1, target as i32 - 1, 20.0);
        let due_today = s.card(target, 0, 20.0);
        let mut request = s.request(AdvancePostponeMode::Advance);
        let preview = s.col.preview_advance_postpone(&request).unwrap();
        assert_eq!(preview.card_ids, [almost, halfway, just_reviewed]);
        assert_eq!(preview.safe_count, 1);
        assert_eq!(preview.without_curve, 0);
        // moving a card forward raises its retrievability at review
        for (before, after) in preview
            .retrievability_before
            .iter()
            .zip(&preview.retrievability_after)
        {
            assert!(after > before, "{before} -> {after}");
        }

        // the move takes the scope it is given: here the first two cards
        request.scope = AdvancePostponeScope::Cards(vec![almost, halfway]);
        let outcome = s.col.advance_postpone(&request).unwrap().output;
        assert_eq!(outcome.count, 2);
        assert!(outcome.retrievability_after > outcome.retrievability_before);
        let target = target as u32;
        assert_eq!(s.due_and_interval(almost), (0, target - 1));
        assert_eq!(s.due_and_interval(halfway), (0, target / 2));
        assert_eq!(
            s.due_and_interval(just_reviewed),
            (target as i32 - 1, target)
        );
        assert_eq!(s.due_and_interval(due_today), (0, target));
    }

    // Pins spec/scheduling.md#sched.postpone: under FSRS-7, Postpone takes the
    // due cards that are least overdue for their target interval first, and
    // moves each at least one day past today, within the fuzz range of its
    // elapsed days plus 7.5% of its interval and at most the maximum
    // interval; a card already at the maximum interval is left out.
    #[test]
    fn postpone_takes_the_least_overdue_cards_first_and_moves_them_past_today() {
        let mut s = Setup::new(SchedulingAlgorithm::Fsrs7);
        // the same state and interval (40 days): the less overdue first
        let overdue = s.card(60, -20, 40.0);
        let on_time = s.card(40, 0, 40.0);
        let slightly_overdue = s.card(45, -5, 40.0);
        let not_due = s.card(5, 5, 40.0);
        let mut request = s.request(AdvancePostponeMode::Postpone);
        let preview = s.col.preview_advance_postpone(&request).unwrap();
        assert_eq!(preview.card_ids, [on_time, slightly_overdue, overdue]);
        // moving a card back lowers its retrievability at review
        for (before, after) in preview
            .retrievability_before
            .iter()
            .zip(&preview.retrievability_after)
        {
            assert!(after < before, "{before} -> {after}");
        }

        let outcome = s.col.advance_postpone(&request).unwrap().output;
        assert_eq!(outcome.count, 3);
        assert!(outcome.retrievability_after < outcome.retrievability_before);
        // the elapsed days plus 3 (7.5% of 40); unit tests have no fuzz seed,
        // so the unrounded interval is rounded
        assert_eq!(s.due_and_interval(on_time), (3, 43));
        assert_eq!(s.due_and_interval(slightly_overdue), (3, 48));
        assert_eq!(s.due_and_interval(overdue), (3, 63));
        assert_eq!(s.due_and_interval(not_due), (5, 10));

        // a short interval still moves at least one day past today
        let short = s.card(4, 0, 3.0);
        request.scope = AdvancePostponeScope::Cards(vec![short]);
        s.col.advance_postpone(&request).unwrap();
        assert_eq!(s.due_and_interval(short), (1, 5));

        // at the maximum interval there is no later day to move to
        s.col.update_default_deck_config(|config| {
            config.maximum_review_interval = 30;
        });
        let at_max = s.card(30, 0, 40.0);
        request.scope = AdvancePostponeScope::Cards(vec![at_max]);
        let preview = s.col.preview_advance_postpone(&request).unwrap();
        assert!(preview.card_ids.is_empty());
        assert_eq!(preview.at_maximum_interval, 1);
        assert_eq!(s.col.advance_postpone(&request).unwrap().output.count, 0);
        assert_eq!(s.due_and_interval(at_max), (0, 30));
    }

    /// Weights of a mixture of one RWKV basis curve: that curve alone.
    fn basis_curve(card_id: CardId, basis: usize) -> (CardId, StoredCurve) {
        let mut weights = vec![0.0f32; basis + 1];
        weights[basis] = 1.0;
        let mut bytes = (weights.len() as u32).to_le_bytes().to_vec();
        for weight in weights {
            bytes.extend_from_slice(&weight.to_le_bytes());
        }
        let (_, curve) = crate::rwkv::unpack_stored_curves(&[card_id.0], &bytes)
            .unwrap()
            .remove(0);
        (card_id, curve)
    }

    // Pins spec/scheduling.md#sched.advance-postpone-algorithm: under
    // RWKV-Curve the order, the safety count and the retrievability come
    // from each card's stored RWKV-Curve curve, never from its FSRS-7 memory
    // state, and a card without a stored curve is left out and counted.
    #[test]
    fn rwkv_curve_uses_the_stored_curves_and_leaves_out_cards_without_one() {
        let mut s = Setup::new(SchedulingAlgorithm::RwkvCurve);
        // FSRS-7 would take the first card first (its FSRS-7 target interval
        // is far shorter), but its RWKV-Curve curve is the slower one
        let slow_curve = s.card(10, 5, 5.0);
        let fast_curve = s.card(10, 5, 500.0);
        let no_curve = s.card(10, 5, 20.0);
        // basis 60 meets 90% after about 2.4 days, basis 80 after about 44
        let mut request = s.request(AdvancePostponeMode::Advance);
        request.rwkv_curves =
            HashMap::from([basis_curve(slow_curve, 80), basis_curve(fast_curve, 60)]);
        let preview = s.col.preview_advance_postpone(&request).unwrap();
        assert_eq!(preview.card_ids, [fast_curve, slow_curve]);
        assert_eq!(preview.without_curve, 1);
        // the fast curve is past its target (safe), the slow one is not
        assert_eq!(preview.safe_count, 1);
        // retrievability at the old due day (15 days after the review) and
        // today (10 days after)
        let (_, slow) = basis_curve(slow_curve, 80);
        assert_eq!(preview.retrievability_before[1], slow.recall(15.0));
        assert_eq!(preview.retrievability_after[1], slow.recall(10.0));

        let outcome = s.col.advance_postpone(&request).unwrap().output;
        assert_eq!((outcome.count, outcome.without_curve), (2, 1));
        assert_eq!(s.due_and_interval(slow_curve), (0, 10));
        assert_eq!(s.due_and_interval(no_curve), (5, 15));
        // the memory states stay as they were: RWKV-Curve's S90 is kept
        let card = s.col.storage.get_card(slow_curve).unwrap().unwrap();
        assert_eq!(card.memory_state.unwrap().stability, 5.0);
    }

    // Pins spec/scheduling.md#sched.advance-postpone-algorithm: RWKV-Instant
    // has no due dates to move.
    #[test]
    fn rwkv_instant_has_no_advance_or_postpone() {
        let mut s = Setup::new(SchedulingAlgorithm::RwkvInstant);
        let card = s.card(10, 5, 20.0);
        for mode in [AdvancePostponeMode::Advance, AdvancePostponeMode::Postpone] {
            let request = s.request(mode);
            assert!(s
                .col
                .advance_postpone_candidates(mode, &request.scope)
                .is_err());
            assert!(s.col.preview_advance_postpone(&request).is_err());
            assert!(s.col.advance_postpone(&request).is_err());
        }
        assert_eq!(s.due_and_interval(card), (5, 15));
    }

    // Pins spec/scheduling.md#sched.reschedule-no-revlog and sched.advance:
    // a move writes no review-log row, is one undo step, and its undo
    // restores every card.
    #[test]
    fn a_move_writes_no_review_log_and_undoes_in_one_step() {
        let mut s = Setup::new(SchedulingAlgorithm::Fsrs7);
        let first = s.card(19, 1, 20.0);
        let second = s.card(18, 2, 20.0);
        let revlog_rows = s
            .col
            .storage
            .get_all_revlog_entries(TimestampSecs(0))
            .unwrap()
            .len();
        let request = s.request(AdvancePostponeMode::Advance);
        let output = s.col.advance_postpone(&request).unwrap();
        assert_eq!(output.output.count, 2);
        assert_eq!(output.changes.op, Op::AdvanceCards);
        assert_eq!(s.due_and_interval(first).0, 0);
        assert_eq!(s.due_and_interval(second).0, 0);
        assert_eq!(
            s.col
                .storage
                .get_all_revlog_entries(TimestampSecs(0))
                .unwrap()
                .len(),
            revlog_rows
        );
        s.col.undo().unwrap();
        assert_eq!(s.due_and_interval(first), (1, 20));
        assert_eq!(s.due_and_interval(second), (2, 20));
    }

    // Pins spec/scheduling.md#sched.advance: the deck scope takes the deck,
    // its subdecks, and their cards that sit in a filtered deck (by their
    // original due date), and leaves suspended cards out.
    #[test]
    fn a_deck_scope_takes_subdecks_and_filtered_cards() {
        let mut s = Setup::new(SchedulingAlgorithm::Fsrs7);
        let parent = s.col.get_or_create_normal_deck("parent").unwrap().id;
        let child = s.col.get_or_create_normal_deck("parent::child").unwrap().id;
        let other = s.col.get_or_create_normal_deck("other").unwrap().id;
        let in_parent = s.card(19, 1, 20.0);
        let in_child = s.card(18, 2, 20.0);
        let elsewhere = s.card(17, 3, 20.0);
        let suspended = s.card(16, 4, 20.0);
        let filtered = s.card(15, 5, 20.0);
        for (card_id, deck_id) in [
            (in_parent, parent),
            (in_child, child),
            (elsewhere, other),
            (suspended, parent),
            (filtered, parent),
        ] {
            let mut card = s.col.storage.get_card(card_id).unwrap().unwrap();
            card.deck_id = deck_id;
            if card_id == suspended {
                card.queue = CardQueue::Suspended;
            }
            if card_id == filtered {
                card.original_deck_id = deck_id;
                card.original_due = card.due;
                card.deck_id = other;
                card.due = -100_000;
            }
            s.col.storage.update_card(&card).unwrap();
        }
        let request = AdvancePostponeRequest {
            scope: AdvancePostponeScope::Deck(parent),
            ..s.request(AdvancePostponeMode::Advance)
        };
        let preview = s.col.preview_advance_postpone(&request).unwrap();
        assert_eq!(preview.card_ids, [in_parent, in_child, filtered]);
        s.col.advance_postpone(&request).unwrap();
        let card = s.col.storage.get_card(filtered).unwrap().unwrap();
        assert_eq!(
            (card.original_due - s.today, card.due, card.interval),
            (0, -100_000, 15)
        );
    }
}
