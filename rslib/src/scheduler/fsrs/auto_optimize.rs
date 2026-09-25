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

use crate::deckconfig::algorithm::SchedulingAlgorithm;
use crate::deckconfig::DeckConfig;
use crate::prelude::*;
use crate::scheduler::fsrs::memory_state::UpdateMemoryStateEntry;
use crate::scheduler::fsrs::memory_state::UpdateMemoryStateRequest;
use crate::scheduler::fsrs::params::compute_params_from_prepared;
use crate::scheduler::fsrs::params::fsrs_optimizer_search;
use crate::scheduler::fsrs::params::ignore_revlogs_before_ms_from_config;
use crate::scheduler::fsrs::params::PrepareComputeParamsInput;
use crate::scheduler::fsrs::params::PreparedComputeParams;
use crate::scheduler::fsrs::HISTORICAL_RETENTION;
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

    fn fsrs_auto_optimize_due(&self, today: u32) -> bool {
        let days = self.fsrs_auto_optimize_days();
        if days == 0 {
            return false;
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
    preset: DeckConfigId,
    preset_mtime: TimestampSecs,
    // as bits; a save within the same second leaves the mtime unchanged
    params: Vec<u32>,
}

impl FsrsAutoOptimizeJobKey {
    fn of(config: &DeckConfig) -> Self {
        Self {
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
        if !self.auto_optimize_applies()? {
            return Ok(None);
        }
        let Some(config) = self.storage.get_deck_config(preset)? else {
            return Ok(None);
        };
        if !config.fsrs_auto_optimize_due(self.timing_today()?.days_elapsed) {
            return Ok(None);
        }
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
            key: FsrsAutoOptimizeJobKey::of(&config),
            prepared,
        }))
    }

    /// Saves the trained parameters as a save in deck options would: the
    /// cards' memory states follow them, and the stored per-review
    /// predictions go. The day is recorded even when nothing changed, so a
    /// preset with too few reviews is not retrained every day. Not undoable:
    /// the pass runs while the user reviews, and Undo must stay theirs.
    /// Returns true when the parameters changed.
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
            if FsrsAutoOptimizeJobKey::of(&config) != key {
                return Ok(false);
            }
            let changed = fsrs_items > 0 && params != config.fsrs_params().to_vec();
            config.inner.fsrs_last_optimized_day = Some(col.timing_today()?.days_elapsed);
            if changed {
                FSRS::new(&params)?;
                config.inner.fsrs_params_7 = params;
            }
            col.add_or_update_deck_config(&mut config)?;
            if changed {
                col.clear_fsrs_review_predictions_of_presets(&[config.id])?;
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
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;
    use crate::search::SortMode;
    use crate::tests::NoteAdder;

    fn fsrs_collection() -> Collection {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, false).unwrap();
        let note = NoteAdder::basic(&mut col).add(&mut col);
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
        col
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
}
