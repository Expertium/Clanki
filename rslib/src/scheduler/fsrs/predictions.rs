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
use std::time::Duration;

use crate::deckconfig::DeckConfig;
use crate::prelude::*;
use crate::scheduler::fsrs::params::fsrs_review_retrievability_cache_rows;
use crate::scheduler::fsrs::params::FsrsReviewPredictionContext;
use crate::scheduler::fsrs::params::PrepareComputeParamsInput;
use crate::search::writer::preset_search;
use crate::storage::FsrsReviewRetrievabilityCacheRow;

/// What the pass stores its rows under. The same string names them again
/// when a batch has to be taken back.
pub(crate) const FSRS_PREDICTION_PASS_SOURCE: &str = "fsrs_calibration_recompute";

/// How many rows one write holds the collection for. A whole preset in one
/// write held it for over six seconds on a large collection, and every
/// click in that time waited; a batch of this size is a fraction of a
/// second (spec ui.stats-fsrs-predictions-ready). This is the shape the
/// RWKV recording pass already uses (spec sched.rwkv-recordings-automatic):
/// short batches, and the user's own work in between two of them.
pub(crate) const PREDICTION_WRITE_BATCH_ROWS: usize = 10_000;

/// How long the pass rests between two batches. The collection is free the
/// whole time, and the rest is what lets a click that is already waiting
/// for it take it before the pass asks again.
const PREDICTION_WRITE_BATCH_REST: Duration = Duration::from_millis(5);

/// What one batch write did.
pub(crate) enum PredictionBatchOutcome {
    /// The batch is stored; this many rows went in.
    Stored(u32),
    /// The preset was saved or its decks changed since the job read them.
    /// The batches already written are gone, and the pass stops: the preset
    /// stays stale for the next pass.
    Superseded,
}

/// Writes one preset's rows a batch at a time, giving the collection back
/// between two batches (spec ui.stats-fsrs-predictions-ready). `with_col`
/// takes the collection for one batch and gives it back when it returns, so
/// a click waits for one batch at most rather than for the whole preset.
/// Returns how many rows were written, or 0 when the preset was superseded
/// mid-write.
pub(crate) fn store_fsrs_review_predictions_in_batches<F>(
    job: &FsrsReviewPredictionJob,
    rows: &[FsrsReviewRetrievabilityCacheRow],
    batch_rows: usize,
    mut with_col: F,
) -> Result<u32>
where
    F: FnMut(
        &FsrsReviewPredictionJob,
        &[FsrsReviewRetrievabilityCacheRow],
        &[FsrsReviewRetrievabilityCacheRow],
    ) -> Result<PredictionBatchOutcome>,
{
    let batch_rows = batch_rows.max(1);
    let mut written = 0u32;
    let mut done = 0usize;
    while done < rows.len() {
        let end = (done + batch_rows).min(rows.len());
        match with_col(job, &rows[done..end], &rows[..done])? {
            PredictionBatchOutcome::Superseded => return Ok(0),
            PredictionBatchOutcome::Stored(stored) => written += stored,
        }
        done = end;
        if done < rows.len() {
            std::thread::sleep(PREDICTION_WRITE_BATCH_REST);
        }
    }
    Ok(written)
}

/// One preset's recompute, split so that only reading its reviews and
/// writing its rows need the collection; the fold fits in [`Self::rows`] run
/// without it.
pub(crate) struct FsrsReviewPredictionJob {
    key: FsrsReviewPredictionJobKey,
    params: Vec<f32>,
    context: FsrsReviewPredictionContext,
}

/// What the rows depend on besides the reviews read: the preset as saved
/// (its parameters, and anything else a save changes) and the decks that
/// use it. If either differs at write time the rows are dropped, since a
/// parameter change deletes the old rows and must not see them come back.
#[derive(PartialEq, Eq, Debug)]
struct FsrsReviewPredictionJobKey {
    preset: DeckConfigId,
    preset_mtime: TimestampSecs,
    // as bits; a save within the same second leaves the mtime unchanged
    params: Vec<u32>,
    decks: Vec<DeckId>,
}

impl FsrsReviewPredictionJob {
    /// The preset's rows, in review-id order. The folds produce them
    /// grouped by fold, which scatters each batch's writes over the whole
    /// cache index; in this order one batch touches one range of it, and
    /// the rows a batch shares with its neighbours are written once rather
    /// than once per batch. Nothing downstream depends on the order: the
    /// write is an upsert keyed by review, fold and source.
    pub(crate) fn rows(&self) -> Result<Vec<FsrsReviewRetrievabilityCacheRow>> {
        let mut rows =
            fsrs_review_retrievability_cache_rows(&self.params, &self.context, true, None)?;
        rows.sort_unstable_by_key(|row| (row.revlog_id, row.fold_index));
        Ok(rows)
    }
}

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

    /// Reads what ONE preset's recompute needs; None when the preset is
    /// gone or has no reviews to predict. One preset per call, so the
    /// collection is free between them and the main thread is never shut
    /// out for the length of a whole backfill; the backend also releases it
    /// while [`FsrsReviewPredictionJob::rows`] runs (spec
    /// ui.stats-fsrs-predictions-ready). The rows are validation folds, so
    /// nothing that produced a row had seen the review it predicts (spec
    /// ui.stats-model-metrics).
    pub(crate) fn fsrs_review_prediction_job(
        &mut self,
        preset: DeckConfigId,
    ) -> Result<Option<FsrsReviewPredictionJob>> {
        let Some(config) = self.storage.get_deck_config(preset)? else {
            return Ok(None);
        };
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
            return Ok(None);
        }
        Ok(Some(FsrsReviewPredictionJob {
            key: self.fsrs_review_prediction_job_key(&config)?,
            params,
            context: FsrsReviewPredictionContext::from_prepared(&prepared),
        }))
    }

    /// Writes one batch of a job's rows, unless its preset was saved or its
    /// decks changed since the job read them. `already_written` is the rows
    /// of this job that earlier batches stored; on a mismatch they are
    /// deleted and nothing more is written, so the cache never holds rows
    /// that two different sets of parameters produced, and the preset stays
    /// stale for the next pass. No progress handling: this runs while the
    /// user is doing something else, and a background pass must not clear
    /// or contend with the progress the main thread is showing.
    pub(crate) fn store_fsrs_review_prediction_batch(
        &mut self,
        job: &FsrsReviewPredictionJob,
        batch: &[FsrsReviewRetrievabilityCacheRow],
        already_written: &[FsrsReviewRetrievabilityCacheRow],
    ) -> Result<PredictionBatchOutcome> {
        let superseded = match self.storage.get_deck_config(job.key.preset)? {
            // the preset is gone, so its rows are nobody's
            None => true,
            Some(config) => self.fsrs_review_prediction_job_key(&config)? != job.key,
        };
        if superseded {
            let reviews: Vec<RevlogId> = already_written.iter().map(|row| row.revlog_id).collect();
            self.storage
                .clear_fsrs_review_predictions_of_reviews(&reviews, FSRS_PREDICTION_PASS_SOURCE)?;
            return Ok(PredictionBatchOutcome::Superseded);
        }
        let stored = self
            .storage
            .set_fsrs_review_retrievability_predictions(batch, FSRS_PREDICTION_PASS_SOURCE)?;
        Ok(PredictionBatchOutcome::Stored(stored as u32))
    }

    fn fsrs_review_prediction_job_key(
        &mut self,
        config: &DeckConfig,
    ) -> Result<FsrsReviewPredictionJobKey> {
        let mut decks: Vec<DeckId> = self
            .storage
            .get_all_decks()?
            .into_iter()
            .filter(|deck| deck.config_id() == Some(config.id))
            .map(|deck| deck.id)
            .collect();
        decks.sort_unstable();
        Ok(FsrsReviewPredictionJobKey {
            preset: config.id,
            preset_mtime: config.mtime_secs,
            params: config.fsrs_params().iter().map(|p| p.to_bits()).collect(),
            decks,
        })
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;
    use crate::storage::FsrsReviewRetrievabilityCacheRow;
    use crate::storage::FsrsReviewRetrievabilitySampleRole;
    use crate::tests::DeckAdder;
    use crate::tests::NoteAdder;

    fn rated_card(col: &mut Collection, days_ago: i64) -> RevlogId {
        rated_card_in(col, DeckId(1), days_ago)
    }

    fn rated_card_in(col: &mut Collection, deck: DeckId, days_ago: i64) -> RevlogId {
        let note = NoteAdder::basic(col).deck(deck).add(col);
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

    fn card_with_reviews(col: &mut Collection) {
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

    fn fold_rows(col: &Collection) -> usize {
        col.storage
            .db
            .query_row(
                "select count() from search_stats_fsrs_review_retrievability",
                [],
                |row| row.get(0),
            )
            // the table appears with its first write
            .unwrap_or(0)
    }

    /// Writes a whole preset in batches of `batch_rows`, the way the backend
    /// does, and reports the size of every batch.
    fn store_in_batches(
        col: &mut Collection,
        job: &FsrsReviewPredictionJob,
        rows: &[FsrsReviewRetrievabilityCacheRow],
        batch_rows: usize,
        batch_sizes: &mut Vec<usize>,
    ) -> Result<u32> {
        store_fsrs_review_predictions_in_batches(job, rows, batch_rows, |job, batch, written| {
            batch_sizes.push(batch.len());
            col.store_fsrs_review_prediction_batch(job, batch, written)
        })
    }

    fn bump_params(col: &mut Collection, preset: DeckConfigId) -> Result<()> {
        let mut config = col.storage.get_deck_config(preset)?.unwrap();
        let mut params = config.fsrs_params().to_vec();
        params[0] *= 1.01;
        config.inner.fsrs_params_7 = params;
        col.storage.update_deck_conf(&config)?;
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-fsrs-predictions-ready: the rows are computed
    // without the collection, so a preset saved meanwhile must not get back
    // the rows its save deleted.
    #[test]
    fn rows_computed_before_a_preset_save_are_not_written() -> Result<()> {
        let mut col = Collection::new();
        for _ in 0..4 {
            card_with_reviews(&mut col);
        }
        let preset = DeckConfigId(1);

        let job = col.fsrs_review_prediction_job(preset)?.expect("a job");
        let rows = job.rows()?;
        assert!(!rows.is_empty());
        bump_params(&mut col, preset)?;
        let mut sizes = vec![];
        assert_eq!(
            store_in_batches(&mut col, &job, &rows, rows.len(), &mut sizes)?,
            0
        );
        assert_eq!(fold_rows(&col), 0);

        // with nothing saved in between, the same steps write the rows
        let job = col.fsrs_review_prediction_job(preset)?.expect("a job");
        let rows = job.rows()?;
        let mut sizes = vec![];
        assert_eq!(
            store_in_batches(&mut col, &job, &rows, rows.len(), &mut sizes)? as usize,
            rows.len()
        );
        assert_eq!(fold_rows(&col), rows.len());
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-fsrs-predictions-ready: the write holds the
    // collection for one bounded batch, not for a whole preset.
    #[test]
    fn a_presets_rows_are_written_one_bounded_batch_at_a_time() -> Result<()> {
        let mut col = Collection::new();
        for _ in 0..6 {
            card_with_reviews(&mut col);
        }
        let preset = DeckConfigId(1);
        let job = col.fsrs_review_prediction_job(preset)?.expect("a job");
        let rows = job.rows()?;
        assert!(rows.len() > 2, "the preset needs several rows to batch");

        let mut sizes = vec![];
        let written = store_in_batches(&mut col, &job, &rows, 2, &mut sizes)?;

        // every batch is bounded, they cover the rows exactly once, and a
        // preset of this many rows takes more than one of them
        assert!(sizes.len() > 1, "one batch is not a bounded write");
        assert!(
            sizes.iter().all(|size| *size <= 2),
            "a batch ran over: {sizes:?}"
        );
        assert_eq!(sizes.iter().sum::<usize>(), rows.len());
        assert_eq!(written as usize, rows.len());
        assert_eq!(fold_rows(&col), rows.len());
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-fsrs-predictions-ready: a preset saved while
    // its rows are being written takes back the batches already written, so
    // the cache never holds rows that two sets of parameters produced.
    #[test]
    fn a_save_midway_through_the_write_takes_back_what_was_written() -> Result<()> {
        let mut col = Collection::new();
        for _ in 0..6 {
            card_with_reviews(&mut col);
        }
        let preset = DeckConfigId(1);

        // another preset, with a row of its own that nothing here may touch
        let other_deck = DeckAdder::new("other")
            .with_config(|config| config.name = "Other".to_string())
            .add(&mut col);
        let other_review = rated_card_in(&mut col, other_deck.id, 15);
        col.storage.set_fsrs_review_retrievability_predictions(
            &[FsrsReviewRetrievabilityCacheRow {
                revlog_id: other_review,
                prediction: 0.7,
                sample_role: FsrsReviewRetrievabilitySampleRole::ValidationFold,
                fold_index: 0,
            }],
            FSRS_PREDICTION_PASS_SOURCE,
        )?;
        assert_eq!(fold_rows(&col), 1);

        let job = col.fsrs_review_prediction_job(preset)?.expect("a job");
        let rows = job.rows()?;
        assert!(rows.len() > 2, "the preset needs several rows to batch");

        // the preset is saved after the first batch has gone in
        let mut batches = 0;
        let written =
            store_fsrs_review_predictions_in_batches(&job, &rows, 2, |job, batch, written| {
                batches += 1;
                if batches == 2 {
                    // the first batch is on disk by now
                    assert!(fold_rows(&col) > 1, "the first batch wrote nothing");
                    bump_params(&mut col, preset)?;
                }
                col.store_fsrs_review_prediction_batch(job, batch, written)
            })?;

        // the pass stopped at the batch that found the save, and took back
        // what it had written
        assert_eq!(written, 0);
        assert_eq!(batches, 2);
        // only the other preset's row is left
        assert_eq!(fold_rows(&col), 1);
        assert_eq!(
            col.storage.db.query_row(
                "select revlog_id from search_stats_fsrs_review_retrievability",
                [],
                |row| row.get::<_, i64>(0),
            )?,
            other_review.0
        );
        Ok(())
    }
}
