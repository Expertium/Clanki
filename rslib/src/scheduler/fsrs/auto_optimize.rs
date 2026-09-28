// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Optimizing FSRS-7's parameters without being asked (spec
//! deck-options.fsrs-auto-optimize).
//!
//! The user never decides when to optimize: each preset has "Optimize every
//! N days", and a background pass optimizes the presets that are due. One
//! preset per call, and the call holds the collection only to read the
//! reviews and to save the result, not while it trains; a save of the
//! preset in between (deck options) wins, and the result is dropped.

use std::collections::HashMap;

use fsrs::FSRS;

use crate::collection::CollectionOpenId;
use crate::deckconfig::algorithm::SchedulingAlgorithm;
use crate::deckconfig::DeckConfig;
use crate::prelude::*;
use crate::scheduler::fsrs::memory_state::UpdateMemoryStateEntry;
use crate::scheduler::fsrs::memory_state::UpdateMemoryStateRequest;
use crate::scheduler::fsrs::params::compute_params_from_prepared;
use crate::scheduler::fsrs::params::fsrs_optimizer_search;
use crate::scheduler::fsrs::params::ignore_revlogs_before_ms_from_config;
use crate::scheduler::fsrs::params::prepared_compute_params;
use crate::scheduler::fsrs::params::PrepareComputeParamsInput;
use crate::scheduler::fsrs::params::PreparedComputeParams;
use crate::scheduler::fsrs::predictions::PREDICTION_READ_PART_CARDS;
use crate::scheduler::fsrs::HISTORICAL_RETENTION;
use crate::scheduler::rwkv::RwkvCollectionHold;
use crate::search::Node;
use crate::search::SearchNode;
use crate::storage::comma_separated_ids;

/// "Optimize every N days" when the preset has never been given a value.
pub(crate) const DEFAULT_FSRS_AUTO_OPTIMIZE_DAYS: u32 = 7;

impl DeckConfig {
    /// Days between two automatic optimizations; 0 means never.
    pub(crate) fn fsrs_auto_optimize_days(&self) -> u32 {
        self.inner
            .fsrs_auto_optimize_days
            .unwrap_or(DEFAULT_FSRS_AUTO_OPTIMIZE_DAYS)
    }

    /// Stored FSRS-7 parameters that FSRS-7 cannot run (an outdated
    /// 35-value preview, or any count other than 34): the preset runs the
    /// FSRS-7 defaults until it is optimized (spec sched.fsrs7-only).
    pub(crate) fn holds_unusable_fsrs7_params(&self) -> bool {
        !self.inner.fsrs_params_7.is_empty()
            && self.fsrs_params() != self.inner.fsrs_params_7.as_slice()
    }

    /// Due when its days have passed, and at once when it holds unusable
    /// FSRS-7 parameters, whatever its last optimization; never with 0
    /// days (spec deck-options.fsrs-auto-optimize).
    fn fsrs_auto_optimize_due(&self, today: u32) -> bool {
        let days = self.fsrs_auto_optimize_days();
        if days == 0 {
            return false;
        }
        if self.holds_unusable_fsrs7_params() {
            return true;
        }
        match self.inner.fsrs_last_optimized_day {
            None => true,
            Some(last) => today.saturating_sub(last) >= days,
        }
    }
}

/// One preset's optimization, split so that only reading its reviews and
/// saving the result need the collection.
pub(crate) struct FsrsAutoOptimizeJob {
    key: FsrsAutoOptimizeJobKey,
    prepared: PreparedComputeParams,
}

/// The preset as it was read. A save in between changes the mtime or the
/// parameters, and the result is then dropped: the user's save wins.
#[derive(PartialEq, Eq, Debug)]
pub(crate) struct FsrsAutoOptimizeJobKey {
    // the open collection the job read; another one, even an identical
    // copy, is not the job's (spec deck-options.fsrs-auto-optimize)
    collection: CollectionOpenId,
    preset: DeckConfigId,
    preset_mtime: TimestampSecs,
    // as bits; a save within the same second leaves the mtime unchanged
    params: Vec<u32>,
}

impl FsrsAutoOptimizeJobKey {
    fn of(collection: CollectionOpenId, config: &DeckConfig) -> Self {
        Self {
            collection,
            preset: config.id,
            preset_mtime: config.mtime_secs,
            params: config.fsrs_params().iter().map(|p| p.to_bits()).collect(),
        }
    }
}

impl FsrsAutoOptimizeJob {
    /// Trains the parameters. Needs no collection.
    pub(crate) fn params(self) -> Result<(FsrsAutoOptimizeJobKey, Vec<f32>, u32)> {
        let response = compute_params_from_prepared(self.prepared, None, false)?;
        Ok((self.key, response.params, response.fsrs_items))
    }
}

/// Cards per part of the review read of a due preset: about 20 ms of the
/// collection at most for the largest preset of Andrew's collection, as the
/// prediction pass's read (`PREDICTION_READ_PART_CARDS`).
pub(crate) const AUTO_OPTIMIZE_READ_PART_CARDS: usize = PREDICTION_READ_PART_CARDS;
/// Reads in parts that a write may interrupt before a read is made in one
/// piece instead.
const AUTO_OPTIMIZE_READ_ATTEMPTS: usize = 3;

/// [`Collection::fsrs_auto_optimize_job`], with the collection held for the
/// checks and the search, then for the reviews of `part_cards` cards at a
/// time, then to check nothing changed; the training items are made without
/// it (spec deck-options.fsrs-auto-optimize). In one piece, the read and the
/// items of the largest preset of Andrew's collection held the collection
/// for about half a second. The parts are one read only when nothing wrote
/// to the collection between the first hold and the last
/// (`SqliteStorage::change_stamp`); after a write the read starts over, and a
/// collection that keeps changing is read in one piece.
pub(crate) fn fsrs_auto_optimize_job_in_parts(
    preset: DeckConfigId,
    part_cards: usize,
    hold: &mut RwkvCollectionHold,
) -> Result<Option<FsrsAutoOptimizeJob>> {
    for _ in 0..AUTO_OPTIMIZE_READ_ATTEMPTS {
        let mut start = None;
        hold(&mut |col| {
            let stamp = col.storage.change_stamp();
            let found = match col.fsrs_auto_optimize_due_preset(preset)? {
                Some(config) => {
                    let search = fsrs_optimizer_search(&config)?;
                    let cards = col.cards_for_srs(search.as_str())?;
                    let key = FsrsAutoOptimizeJobKey::of(col.state.open_id, &config);
                    Some((config, key, cards))
                }
                None => None,
            };
            start = Some((stamp, found));
            Ok(())
        })?;
        let (stamp, found) = start.or_invalid("preset never read")?;
        let Some((config, key, cards)) = found else {
            return Ok(None);
        };
        let Some(cards) = cards else {
            // a whole-collection search: one piece
            break;
        };
        let mut revlogs = vec![];
        let mut unchanged = true;
        for part in cards.chunks(part_cards.max(1)) {
            hold(&mut |col| {
                unchanged = col.storage.change_stamp() == stamp;
                if unchanged {
                    revlogs.extend(
                        col.storage
                            .get_revlog_entries_of_cards_in_card_order(part)?,
                    );
                }
                Ok(())
            })?;
            if !unchanged {
                break;
            }
        }
        if unchanged {
            hold(&mut |col| {
                unchanged = col.storage.change_stamp() == stamp;
                Ok(())
            })?;
        }
        if !unchanged {
            continue;
        }
        let prepared = prepared_compute_params(
            revlogs,
            ignore_revlogs_before_ms_from_config(&config)?,
            config.fsrs_params(),
            config.inner.relearn_steps.len(),
            true,
        );
        return Ok(Some(FsrsAutoOptimizeJob { key, prepared }));
    }
    let mut job = None;
    hold(&mut |col| {
        job = col.fsrs_auto_optimize_job(preset)?;
        Ok(())
    })?;
    Ok(job)
}

impl Collection {
    /// The presets that are due, in id order. Under RWKV too: the Stats
    /// graphs compare RWKV with FSRS-7, and FSRS-7 needs current parameters
    /// for that.
    pub(crate) fn fsrs_presets_due_for_auto_optimize(&mut self) -> Result<Vec<DeckConfigId>> {
        if !self.auto_optimize_applies()? {
            return Ok(vec![]);
        }
        let today = self.timing_today()?.days_elapsed;
        let mut due: Vec<_> = self
            .storage
            .all_deck_config()?
            .into_iter()
            .filter(|config| config.fsrs_auto_optimize_due(today))
            .map(|config| config.id)
            .collect();
        due.sort_unstable();
        Ok(due)
    }

    fn auto_optimize_applies(&mut self) -> Result<bool> {
        Ok(self.get_config_bool(BoolKey::Fsrs))
    }

    /// Reads one due preset's reviews. None when the preset is gone or no
    /// longer due.
    pub(crate) fn fsrs_auto_optimize_job(
        &mut self,
        preset: DeckConfigId,
    ) -> Result<Option<FsrsAutoOptimizeJob>> {
        let Some(config) = self.fsrs_auto_optimize_due_preset(preset)? else {
            return Ok(None);
        };
        // the same review set as "Optimize All Presets"
        let search = fsrs_optimizer_search(&config)?;
        let current_params = config.fsrs_params().to_vec();
        let prepared = self.prepare_compute_params(PrepareComputeParamsInput {
            search: &search,
            ignore_revlogs_before: ignore_revlogs_before_ms_from_config(&config)?,
            current_params: &current_params,
            num_of_relearning_steps: config.inner.relearn_steps.len(),
            enable_scheduling_penalties: true,
        })?;
        Ok(Some(FsrsAutoOptimizeJob {
            key: FsrsAutoOptimizeJobKey::of(self.state.open_id, &config),
            prepared,
        }))
    }

    /// The preset, when it exists and is due.
    fn fsrs_auto_optimize_due_preset(
        &mut self,
        preset: DeckConfigId,
    ) -> Result<Option<DeckConfig>> {
        if !self.auto_optimize_applies()? {
            return Ok(None);
        }
        let Some(config) = self.storage.get_deck_config(preset)? else {
            return Ok(None);
        };
        if !config.fsrs_auto_optimize_due(self.timing_today()?.days_elapsed) {
            return Ok(None);
        }
        Ok(Some(config))
    }

    /// Saves the trained parameters as a save in deck options would: the
    /// cards' memory states follow them, and the stored per-review
    /// predictions are marked stale; the pass that called this deletes
    /// them in parts before it writes the preset again. The day is recorded
    /// even when nothing changed, so a preset with too few reviews is not
    /// retrained every day. Unusable
    /// FSRS-7 parameters that training did not replace are cleared: the
    /// preset ran the FSRS-7 defaults with them and runs them without, and
    /// it is then not due again at every pass. Not undoable: the pass runs
    /// while the user reviews, and Undo must stay theirs. Returns true when
    /// the parameters changed.
    pub(crate) fn apply_fsrs_auto_optimize(
        &mut self,
        key: FsrsAutoOptimizeJobKey,
        params: Vec<f32>,
        fsrs_items: u32,
    ) -> Result<bool> {
        self.transact_no_undo(|col| {
            let Some(mut config) = col.storage.get_deck_config(key.preset)? else {
                return Ok(false);
            };
            if FsrsAutoOptimizeJobKey::of(col.state.open_id, &config) != key {
                return Ok(false);
            }
            let changed = fsrs_items > 0 && params != config.fsrs_params().to_vec();
            config.inner.fsrs_last_optimized_day = Some(col.timing_today()?.days_elapsed);
            if changed {
                FSRS::new(&params)?;
                config.inner.fsrs_params_7 = params;
            } else if config.holds_unusable_fsrs7_params() {
                config.inner.fsrs_params_7.clear();
            }
            col.add_or_update_deck_config(&mut config)?;
            if changed {
                col.mark_fsrs_predictions_stale(&[config.id])?;
                col.update_memory_state_of_preset(&config)?;
            }
            Ok(changed)
        })
    }

    fn update_memory_state_of_preset(&mut self, config: &DeckConfig) -> Result<()> {
        let mut decks = vec![];
        let mut deck_desired_retention = HashMap::new();
        for deck in self.storage.get_all_decks()? {
            if let Ok(normal) = deck.normal() {
                if DeckConfigId(normal.config_id) == config.id {
                    decks.push(deck.id);
                    if let Some(retention) = normal.desired_retention {
                        deck_desired_retention.insert(deck.id, retention);
                    }
                }
            }
        }
        if decks.is_empty() {
            return Ok(());
        }
        // FSRS-7 intervals only where FSRS-7 schedules: under RWKV the new
        // parameters change FSRS-7's memory states, never a due date
        let reschedule = self.get_config_bool(BoolKey::FsrsReschedule)
            && self.effective_scheduling_algorithm()? == SchedulingAlgorithm::Fsrs7;
        self.update_memory_state(vec![UpdateMemoryStateEntry {
            req: Some(UpdateMemoryStateRequest {
                params: config.fsrs_params().to_vec(),
                preset_desired_retention: config.inner.desired_retention,
                max_interval: config.inner.maximum_review_interval,
                review_fuzz_config: self.stored_review_fuzz_config().review_fuzz_config(),
                reschedule,
                historical_retention: HISTORICAL_RETENTION,
                deck_desired_retention,
                keep_stability: config.inner.rwkv_review_enabled,
            }),
            search: Node::Search(SearchNode::DeckIdsWithoutChildren(comma_separated_ids(
                &decks,
            ))),
            ignore_before: ignore_revlogs_before_ms_from_config(config)?,
            preset_name: config.name.clone(),
            current_preset: 1,
            total_presets: 1,
        }])
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::collection::CollectionBuilder;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;
    use crate::search::SortMode;
    use crate::tests::NoteAdder;

    fn fsrs_collection() -> Collection {
        let mut col = Collection::new();
        add_fsrs_reviews(&mut col);
        col
    }

    fn add_fsrs_reviews(col: &mut Collection) {
        col.set_config_bool(BoolKey::Fsrs, true, false).unwrap();
        let note = NoteAdder::basic(col).add(col);
        let card = col
            .storage
            .all_cards_of_note(note.id)
            .unwrap()
            .pop()
            .unwrap();
        for (days_ago, interval) in [(40, 0), (39, 3), (30, 10)] {
            col.storage
                .add_revlog_entry(
                    &RevlogEntry {
                        id: RevlogId(TimestampMillis::now().0 - days_ago * 86_400_000),
                        cid: card.id,
                        button_chosen: 3,
                        review_kind: if interval == 0 {
                            RevlogReviewKind::Learning
                        } else {
                            RevlogReviewKind::Review
                        },
                        interval,
                        ease_factor: 2500,
                        ..Default::default()
                    },
                    false,
                )
                .unwrap();
        }
    }

    fn trained(params: &[f32]) -> Vec<f32> {
        let mut params = params.to_vec();
        params[0] *= 1.01;
        params
    }

    // Pins spec/deck-options.md#deck-options.fsrs-auto-optimize
    #[test]
    fn a_preset_is_optimized_again_after_its_days() -> Result<()> {
        let mut col = fsrs_collection();
        let preset = DeckConfigId(1);
        // never optimized: due at once, every 7 days by default
        assert_eq!(col.fsrs_presets_due_for_auto_optimize()?, vec![preset]);

        let job = col.fsrs_auto_optimize_job(preset)?.expect("a job");
        let before = col.storage.get_deck_config(preset)?.unwrap();
        let params = trained(before.fsrs_params());
        assert!(col.apply_fsrs_auto_optimize(job.key, params.clone(), 3)?);
        let after = col.storage.get_deck_config(preset)?.unwrap();
        assert_eq!(after.inner.fsrs_params_7, params);
        assert!(col.fsrs_presets_due_for_auto_optimize()?.is_empty());

        // due again once the preset's days have passed; 0 means never
        let mut config = after;
        config.inner.fsrs_last_optimized_day = Some(100);
        for (days, today, due) in [
            (None, 106, false),
            (None, 107, true),
            (Some(7), 106, false),
            (Some(7), 107, true),
            (Some(0), 10_000, false),
        ] {
            config.inner.fsrs_auto_optimize_days = days;
            assert_eq!(
                config.fsrs_auto_optimize_due(today),
                due,
                "{days:?} {today}"
            );
        }
        Ok(())
    }

    // Pins spec/deck-options.md#deck-options.fsrs-auto-optimize: a preset
    // with unusable FSRS-7 parameters (an outdated 35-value preview) is due
    // at the next pass, even when it was optimized today, and is optimized
    // without a question; parameters training cannot replace are cleared, so
    // it is not due again. 0 days still means never.
    #[test]
    fn a_preset_with_unusable_params_is_optimized_at_the_next_pass() -> Result<()> {
        let mut col = fsrs_collection();
        let preset = DeckConfigId(1);
        let today = col.timing_today()?.days_elapsed;
        let store = |col: &mut Collection, params: Vec<f32>, days: Option<u32>| {
            let mut config = col.storage.get_deck_config(preset).unwrap().unwrap();
            config.inner.fsrs_params_7 = params;
            config.inner.fsrs_last_optimized_day = Some(today);
            config.inner.fsrs_auto_optimize_days = days;
            col.storage.update_deck_conf(&config).unwrap();
        };
        let preview = vec![0.5; 35];

        // optimized today with usable parameters: not due
        store(&mut col, fsrs::DEFAULT_PARAMETERS.to_vec(), None);
        assert!(col.fsrs_presets_due_for_auto_optimize()?.is_empty());
        // the same preset with 35 values: due at once, and optimized
        store(&mut col, preview.clone(), None);
        assert_eq!(col.fsrs_presets_due_for_auto_optimize()?, vec![preset]);
        let job = col.fsrs_auto_optimize_job(preset)?.expect("a job");
        // training starts from the FSRS-7 defaults the preset runs
        let trained_params = trained(&fsrs::DEFAULT_PARAMETERS);
        assert!(col.apply_fsrs_auto_optimize(job.key, trained_params.clone(), 3)?);
        let after = col.storage.get_deck_config(preset)?.unwrap();
        assert_eq!(after.inner.fsrs_params_7, trained_params);
        assert!(col.fsrs_presets_due_for_auto_optimize()?.is_empty());

        // too few reviews to train: the unusable values go, the preset keeps
        // running the defaults, and it is not due again
        store(&mut col, preview.clone(), None);
        let job = col.fsrs_auto_optimize_job(preset)?.expect("a job");
        assert!(!col.apply_fsrs_auto_optimize(job.key, fsrs::DEFAULT_PARAMETERS.to_vec(), 0)?);
        let after = col.storage.get_deck_config(preset)?.unwrap();
        assert!(after.inner.fsrs_params_7.is_empty());
        assert_eq!(after.fsrs_params(), fsrs::DEFAULT_PARAMETERS);
        assert!(col.fsrs_presets_due_for_auto_optimize()?.is_empty());

        // "Optimize every 0 days" (never) is left alone
        store(&mut col, preview, Some(0));
        assert!(col.fsrs_presets_due_for_auto_optimize()?.is_empty());
        Ok(())
    }

    fn review_card_due(col: &mut Collection) -> (CardId, i32) {
        let cid = col.search_cards("", SortMode::NoOrder).unwrap()[0];
        let mut card = col.storage.get_card(cid).unwrap().unwrap();
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        card.interval = 10;
        card.due = 5;
        col.storage.update_card(&card).unwrap();
        (cid, card.due)
    }

    fn due_after_optimizing(algorithm: SchedulingAlgorithm) -> Result<(i32, i32)> {
        let mut col = fsrs_collection();
        col.set_config_bool(BoolKey::FsrsReschedule, true, false)?;
        let preset = DeckConfigId(1);
        let mut config = col.storage.get_deck_config(preset)?.unwrap();
        algorithm.apply_to(&mut config.inner);
        col.storage.update_deck_conf(&config)?;
        let (cid, before) = review_card_due(&mut col);
        assert_eq!(col.fsrs_presets_due_for_auto_optimize()?, vec![preset]);
        let job = col.fsrs_auto_optimize_job(preset)?.expect("a job");
        let params = trained(&trained(&trained(config.fsrs_params())));
        assert!(col.apply_fsrs_auto_optimize(job.key, params, 3)?);
        Ok((before, col.storage.get_card(cid)?.unwrap().due))
    }

    // Pins spec/deck-options.md#deck-options.fsrs-auto-optimize
    #[test]
    fn under_rwkv_it_optimizes_but_never_reschedules() -> Result<()> {
        // the control: under FSRS-7 the new parameters move the due date
        let (before, after) = due_after_optimizing(SchedulingAlgorithm::Fsrs7)?;
        assert_ne!(before, after);
        for algorithm in [
            SchedulingAlgorithm::RwkvCurve,
            SchedulingAlgorithm::RwkvInstant,
        ] {
            let (before, after) = due_after_optimizing(algorithm)?;
            assert_eq!(before, after, "{algorithm:?}");
        }
        Ok(())
    }

    // Pins spec/deck-options.md#deck-options.fsrs-auto-optimize: the
    // training runs without the collection, so a save in deck options
    // meanwhile must win.
    #[test]
    fn a_save_during_training_drops_the_result() -> Result<()> {
        let mut col = fsrs_collection();
        let preset = DeckConfigId(1);
        let job = col.fsrs_auto_optimize_job(preset)?.expect("a job");
        let mut config = col.storage.get_deck_config(preset)?.unwrap();
        let saved = trained(&trained(config.fsrs_params()));
        config.inner.fsrs_params_7 = saved.clone();
        col.storage.update_deck_conf(&config)?;

        assert!(!col.apply_fsrs_auto_optimize(job.key, trained(&saved), 3)?);
        let after = col.storage.get_deck_config(preset)?.unwrap();
        assert_eq!(after.inner.fsrs_params_7, saved);
        assert_eq!(after.inner.fsrs_last_optimized_day, None);
        Ok(())
    }

    // Pins spec/deck-options.md#deck-options.fsrs-auto-optimize: every
    // profile opens on the one backend, so a job still training when the
    // profile switches finds another collection when it saves. Even an
    // identical copy is not the job's collection.
    #[test]
    fn a_job_never_saves_into_another_collection() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let first_path = dir.path().join("first.anki2");
        let second_path = dir.path().join("second.anki2");
        {
            let mut col = CollectionBuilder::new(&first_path).build()?;
            add_fsrs_reviews(&mut col);
            col.close(None)?;
        }
        std::fs::copy(&first_path, &second_path)?;
        let preset = DeckConfigId(1);

        let mut first = CollectionBuilder::new(&first_path).build()?;
        let job = first.fsrs_auto_optimize_job(preset)?.expect("a job");
        let params = trained(
            first
                .storage
                .get_deck_config(preset)?
                .unwrap()
                .fsrs_params(),
        );
        // the profile switches while the job trains
        first.close(None)?;
        let mut second = CollectionBuilder::new(&second_path).build()?;
        let before = second.storage.get_deck_config(preset)?.unwrap();

        assert!(!second.apply_fsrs_auto_optimize(job.key, params, 3)?);
        let after = second.storage.get_deck_config(preset)?.unwrap();
        assert_eq!(after.inner.fsrs_params_7, before.inner.fsrs_params_7);
        assert_eq!(after.inner.fsrs_last_optimized_day, None);
        assert_eq!(after.mtime_secs, before.mtime_secs);
        Ok(())
    }

    /// A card of the default deck with three reviews, `later` days closer
    /// to today; its review ids are unique however fast cards are added.
    fn card_with_three_reviews(col: &mut Collection, later: i64) {
        let note = NoteAdder::basic(col).add(col);
        let card = col
            .storage
            .all_cards_of_note(note.id)
            .unwrap()
            .pop()
            .unwrap();
        let now = TimestampMillis::now().0 + card.id.0 % 100_000;
        for (days_ago, interval) in [(40, 0), (39, 3), (30, 10)] {
            col.storage
                .add_revlog_entry(
                    &RevlogEntry {
                        id: RevlogId(now - (days_ago - later) * 86_400_000),
                        cid: card.id,
                        button_chosen: 3,
                        review_kind: if interval == 0 {
                            RevlogReviewKind::Learning
                        } else {
                            RevlogReviewKind::Review
                        },
                        interval,
                        ease_factor: 2500,
                        ..Default::default()
                    },
                    false,
                )
                .unwrap();
        }
    }

    fn job_fields(
        job: &FsrsAutoOptimizeJob,
    ) -> (
        &FsrsAutoOptimizeJobKey,
        &[f32],
        crate::scheduler::fsrs::params::FsrsReviewPredictionContext,
        (usize, usize, usize),
    ) {
        let counts = job.prepared.target_counts;
        (
            &job.key,
            &job.prepared.current_params,
            crate::scheduler::fsrs::params::FsrsReviewPredictionContext::from_prepared(
                &job.prepared,
            ),
            (
                counts.total_targets,
                counts.long_term_targets,
                counts.short_term_targets,
            ),
        )
    }

    /// The collection is free between the parts of a due preset's read, and
    /// the parts give the job of one piece, whatever their size.
    // Pins spec/deck-options.md#deck-options.fsrs-auto-optimize
    #[test]
    fn a_job_read_in_parts_is_the_job_read_in_one_piece() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, false)?;
        for later in 0..7 {
            card_with_three_reviews(&mut col, later);
        }
        let preset = DeckConfigId(1);
        let whole = col.fsrs_auto_optimize_job(preset)?.expect("a job");
        assert!(!whole.prepared.items.is_empty());
        for part_cards in [1, 2, 3, 7, 1_000] {
            let mut holds = 0;
            let in_parts = fsrs_auto_optimize_job_in_parts(preset, part_cards, &mut |step| {
                holds += 1;
                step(&mut col)
            })?
            .expect("a job");
            assert_eq!(
                job_fields(&in_parts),
                job_fields(&whole),
                "part_cards={part_cards}"
            );
            // the checks and the search, the parts, the last check
            assert_eq!(holds, 1 + 7usize.div_ceil(part_cards) + 1);
        }

        // a write between two parts reads again; a collection that keeps
        // changing is read in one piece
        let mut holds = 0;
        let in_parts = fsrs_auto_optimize_job_in_parts(preset, 2, &mut |step| {
            holds += 1;
            if holds == 2 {
                card_with_three_reviews(&mut col, 10);
            }
            step(&mut col)
        })?
        .expect("a job");
        let whole = col.fsrs_auto_optimize_job(preset)?.expect("a job");
        assert_eq!(job_fields(&in_parts), job_fields(&whole));
        assert_eq!(holds, 2 + (1 + 4 + 1));
        let mut holds = 0;
        let in_parts = fsrs_auto_optimize_job_in_parts(preset, 2, &mut |step| {
            holds += 1;
            card_with_three_reviews(&mut col, 10 + holds);
            step(&mut col)
        })?
        .expect("a job");
        let whole = col.fsrs_auto_optimize_job(preset)?.expect("a job");
        assert_eq!(job_fields(&in_parts), job_fields(&whole));
        assert_eq!(holds, 3 * 2 + 1);

        // no job for a preset that is not due, in one hold
        let job = col.fsrs_auto_optimize_job(preset)?.expect("a job");
        col.apply_fsrs_auto_optimize(job.key, vec![], 0)?;
        let mut holds = 0;
        assert!(fsrs_auto_optimize_job_in_parts(preset, 2, &mut |step| {
            holds += 1;
            step(&mut col)
        })?
        .is_none());
        assert_eq!(holds, 1);
        Ok(())
    }

    /// A measurement harness, not a test: how long one due preset's
    /// optimization holds the collection, in one piece and in parts: the
    /// read (`fsrs_auto_optimize_job`), then the save
    /// (`apply_fsrs_auto_optimize`). Works on a COPY of a collection
    /// (`ANKI_STALE_PRESETS_BENCH_COL`), preset `ANKI_BENCH_PRESET`; the
    /// preset is made due before each round and its parameters are put back
    /// after it. With `ANKI_BENCH_CHANGE_PARAMS` set, the trained parameters
    /// are moved a little before the save, so that it updates the cards'
    /// memory states. Run `cargo test -p anki --release
    /// bench_auto_optimize_holds -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn bench_auto_optimize_holds() -> Result<()> {
        use std::time::Instant;

        let path = std::env::var("ANKI_STALE_PRESETS_BENCH_COL")
            .expect("set ANKI_STALE_PRESETS_BENCH_COL to a copy of a collection");
        let preset = DeckConfigId(
            std::env::var("ANKI_BENCH_PRESET")
                .expect("set ANKI_BENCH_PRESET")
                .parse()
                .unwrap(),
        );
        let mut col = CollectionBuilder::new(path).build()?;
        let original = col.storage.get_deck_config(preset)?.unwrap();
        let ms = |at: Instant| at.elapsed().as_secs_f64() * 1000.0;
        for round in 0..3 {
            let mut config = original.clone();
            config.inner.fsrs_last_optimized_day = Some(1);
            col.storage.update_deck_conf(&config)?;
            let at = Instant::now();
            let whole = col.fsrs_auto_optimize_job(preset)?.expect("due");
            let whole_ms = ms(at);
            let mut longest = 0f64;
            let at = Instant::now();
            let job = fsrs_auto_optimize_job_in_parts(
                preset,
                AUTO_OPTIMIZE_READ_PART_CARDS,
                &mut |step| {
                    let held = Instant::now();
                    let result = step(&mut col);
                    longest = longest.max(ms(held));
                    result
                },
            )?
            .expect("due");
            let parts_ms = ms(at);
            drop(whole);
            let at = Instant::now();
            let (key, mut params, items) = job.params()?;
            let train_ms = ms(at);
            if std::env::var("ANKI_BENCH_CHANGE_PARAMS").is_ok() {
                // as when the week's reviews move the parameters: the save
                // then updates the memory states of the preset's cards
                params[0] *= 1.01;
            }
            let at = Instant::now();
            let changed = col.apply_fsrs_auto_optimize(key, params, items)?;
            let apply_ms = ms(at);
            println!(
                "round {round}: read in one piece {whole_ms:.1} ms; in parts {parts_ms:.1} ms, \
                 longest part {longest:.1} ms; training {train_ms:.0} ms; save {apply_ms:.1} ms \
                 (changed {changed})"
            );
            col.storage.update_deck_conf(&original)?;
        }
        Ok(())
    }
}
