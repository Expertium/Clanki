// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Keeping FSRS-7's per-review predictions ready for the model-quality
//! graphs (spec ui.stats-fsrs-predictions-ready).
//!
//! The graphs read stored rows and never compute; this is what fills them.
//! A prediction belongs to the parameters that produced it, so a preset's
//! rows go the moment its parameters change and the pass writes them again.
//! Every pass here is preset-wide and collection-wide: it never follows the
//! search the Stats page happens to show, because a pass that filled only
//! the deck on screen would leave the same gap everywhere else.

use std::collections::HashMap;

use crate::deckconfig::DeckConfig;
use crate::prelude::*;
use crate::scheduler::fsrs::params::FsrsReviewPredictionContext;
use crate::scheduler::fsrs::params::PrepareComputeParamsInput;
use crate::search::writer::preset_search;

impl Collection {
    /// The presets whose cards hold rated reviews that no validation fold
    /// covers. A preset with no such review needs no pass.
    pub(crate) fn presets_with_stale_fsrs_review_predictions(
        &mut self,
    ) -> Result<Vec<DeckConfigId>> {
        let uncovered = self
            .storage
            .decks_with_uncovered_fsrs_review_predictions()?;
        if uncovered.is_empty() {
            return Ok(vec![]);
        }
        let config_of_deck: HashMap<DeckId, DeckConfigId> = self
            .storage
            .get_all_decks()?
            .into_iter()
            .filter_map(|deck| {
                let config_id = deck.config_id()?;
                Some((deck.id, config_id))
            })
            .collect();
        let mut stale: Vec<DeckConfigId> = uncovered
            .into_iter()
            .filter(|(_, reviews)| *reviews > 0)
            .filter_map(|(deck_id, _)| config_of_deck.get(&deck_id).copied())
            .collect();
        stale.sort_unstable();
        stale.dedup();
        Ok(stale)
    }

    /// Drops the stored predictions of these presets' cards. Called when
    /// their parameters change, so that no row made by superseded
    /// parameters can reach a graph.
    pub(crate) fn clear_fsrs_review_predictions_of_presets(
        &mut self,
        presets: &[DeckConfigId],
    ) -> Result<usize> {
        if presets.is_empty() {
            return Ok(0);
        }
        let decks: Vec<DeckId> = self
            .storage
            .get_all_decks()?
            .into_iter()
            .filter(|deck| {
                deck.config_id()
                    .is_some_and(|config_id| presets.contains(&config_id))
            })
            .map(|deck| deck.id)
            .collect();
        self.storage.clear_fsrs_review_predictions_for_decks(&decks)
    }

    /// Recomputes the predictions of every preset that has none for some of
    /// its reviews, and returns how many rows it wrote. The rows are
    /// validation folds, so nothing that produced a row had seen the review
    /// it predicts (spec ui.stats-model-metrics).
    pub(crate) fn refresh_fsrs_review_predictions(&mut self) -> Result<u32> {
        let stale = self.presets_with_stale_fsrs_review_predictions()?;
        if stale.is_empty() {
            return Ok(0);
        }
        let configs: HashMap<DeckConfigId, DeckConfig> = self
            .storage
            .all_deck_config()?
            .into_iter()
            .map(|config| (config.id, config))
            .collect();
        let mut written = 0;
        for preset in stale {
            let Some(config) = configs.get(&preset) else {
                continue;
            };
            written += self.refresh_fsrs_review_predictions_of_preset(config)?;
        }
        Ok(written)
    }

    fn refresh_fsrs_review_predictions_of_preset(&mut self, config: &DeckConfig) -> Result<u32> {
        let search = preset_search(&config.name);
        let params = config.fsrs_params().to_vec();
        let prepared = self.prepare_compute_params(PrepareComputeParamsInput {
            search: &search,
            ignore_revlogs_before: config
                .inner
                .ignore_revlogs_before_date
                .parse()
                .unwrap_or(0.into()),
            current_params: &params,
            num_of_relearning_steps: config.inner.relearn_steps.len(),
            // always on (spec deck-options.fsrs-only-controls)
            enable_scheduling_penalties: true,
        })?;
        if prepared.items.is_empty() {
            return Ok(0);
        }
        let context = FsrsReviewPredictionContext::from_prepared(&prepared);
        self.compute_fsrs_review_retrievability_calibration_cache(&params, &context, true)
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;
    use crate::storage::FsrsReviewRetrievabilityCacheRow;
    use crate::storage::FsrsReviewRetrievabilitySampleRole;
    use crate::tests::NoteAdder;

    fn rated_card(col: &mut Collection, days_ago: i64) -> RevlogId {
        let note = NoteAdder::basic(col).add(col);
        let card = col
            .storage
            .all_cards_of_note(note.id)
            .unwrap()
            .pop()
            .unwrap();
        let review = RevlogId(TimestampMillis::now().0 - days_ago * 86_400_000);
        col.storage
            .add_revlog_entry(
                &RevlogEntry {
                    id: review,
                    cid: card.id,
                    button_chosen: 3,
                    review_kind: RevlogReviewKind::Review,
                    interval: 10,
                    ease_factor: 2500,
                    ..Default::default()
                },
                false,
            )
            .unwrap();
        review
    }

    // Pins spec/ui.md#ui.stats-fsrs-predictions-ready
    #[test]
    fn the_pass_covers_every_preset_with_uncovered_reviews() -> Result<()> {
        let mut col = Collection::new();
        let first = rated_card(&mut col, 30);
        let second = rated_card(&mut col, 20);

        // no stored row covers either review, so the preset is stale
        assert_eq!(
            col.presets_with_stale_fsrs_review_predictions()?,
            vec![DeckConfigId(1)]
        );

        // a validation fold over one of them is not enough
        col.storage.set_fsrs_review_retrievability_predictions(
            &[FsrsReviewRetrievabilityCacheRow {
                revlog_id: first,
                prediction: 0.9,
                sample_role: FsrsReviewRetrievabilitySampleRole::ValidationFold,
                fold_index: 0,
            }],
            "test",
        )?;
        assert_eq!(
            col.presets_with_stale_fsrs_review_predictions()?,
            vec![DeckConfigId(1)]
        );

        // a row of another role does not count: the graph takes the first
        // role of its list that has any row, so answering never fills the
        // folds
        col.storage.set_fsrs_review_retrievability_predictions(
            &[FsrsReviewRetrievabilityCacheRow {
                revlog_id: second,
                prediction: 0.8,
                sample_role: FsrsReviewRetrievabilitySampleRole::PostOptimization,
                fold_index: -1,
            }],
            "test",
        )?;
        assert_eq!(
            col.presets_with_stale_fsrs_review_predictions()?,
            vec![DeckConfigId(1)]
        );

        // both reviews covered by folds: nothing left to do
        col.storage.set_fsrs_review_retrievability_predictions(
            &[FsrsReviewRetrievabilityCacheRow {
                revlog_id: second,
                prediction: 0.8,
                sample_role: FsrsReviewRetrievabilitySampleRole::ValidationFold,
                fold_index: 0,
            }],
            "test",
        )?;
        assert!(col.presets_with_stale_fsrs_review_predictions()?.is_empty());
        Ok(())
    }
}
