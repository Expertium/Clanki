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
use crate::scheduler::rwkv::RwkvCollectionHold;
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

/// Review-log rows per part of the stale-preset read: about 15 ms of the
/// collection on a fast machine.
pub(crate) const STALE_PRESETS_PART_ROWS: usize = 32_768;
/// Reads in parts that a write may interrupt before the stale presets are
/// read in one piece instead.
const STALE_PRESETS_READ_ATTEMPTS: usize = 3;

/// `Collection::presets_with_stale_fsrs_review_predictions`, with the
/// collection held for one part of the review log at a time (spec
/// ui.stats-fsrs-predictions-ready). In one piece it held the collection
/// for 1.9 s on a large collection, and a click in the reviewer waited for
/// it. The parts are one read only when nothing wrote to the collection
/// between the first and the last (`SqliteStorage::change_stamp`). After a
/// write the read starts over; a collection that keeps changing is read in
/// one piece. Either way the result is that of one read.
pub(crate) fn presets_with_stale_fsrs_review_predictions_in_parts(
    part_rows: usize,
    hold: &mut RwkvCollectionHold,
) -> Result<Vec<DeckConfigId>> {
    for _ in 0..STALE_PRESETS_READ_ATTEMPTS {
        let mut uncovered: HashMap<DeckId, u32> = HashMap::new();
        let mut stamp = None;
        let mut after = Some(i64::MIN);
        let mut unchanged = true;
        while let (Some(from), true) = (after, unchanged) {
            hold(&mut |col| {
                let now = col.storage.change_stamp();
                unchanged = *stamp.get_or_insert(now) == now;
                if unchanged {
                    let (part, next) = col
                        .storage
                        .decks_with_uncovered_fsrs_review_predictions_part(from, Some(part_rows))?;
                    for (deck, reviews) in part {
                        *uncovered.entry(deck).or_default() += reviews;
                    }
                    after = next;
                }
                Ok(())
            })?;
        }
        if !unchanged {
            continue;
        }
        let mut stale = None;
        hold(&mut |col| {
            if Some(col.storage.change_stamp()) == stamp {
                let uncovered = std::mem::take(&mut uncovered).into_iter().collect();
                stale = Some(col.presets_of_uncovered_decks(uncovered)?);
            }
            Ok(())
        })?;
        if let Some(stale) = stale {
            return Ok(stale);
        }
    }
    let mut stale = None;
    hold(&mut |col| {
        stale = Some(col.presets_with_stale_fsrs_review_predictions()?);
        Ok(())
    })?;
    stale.or_invalid("stale presets never read")
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
        let (uncovered, _) = self
            .storage
            .decks_with_uncovered_fsrs_review_predictions_part(i64::MIN, None)?;
        self.presets_of_uncovered_decks(uncovered)
    }

    /// The presets of the decks that hold uncovered reviews, given with how
    /// many each holds.
    fn presets_of_uncovered_decks(
        &mut self,
        uncovered: Vec<(DeckId, u32)>,
    ) -> Result<Vec<DeckConfigId>> {
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

    fn cover(col: &mut Collection, review: RevlogId) -> Result<()> {
        col.storage.set_fsrs_review_retrievability_predictions(
            &[FsrsReviewRetrievabilityCacheRow {
                revlog_id: review,
                prediction: 0.9,
                sample_role: FsrsReviewRetrievabilitySampleRole::ValidationFold,
                fold_index: 0,
            }],
            "test",
        )?;
        Ok(())
    }

    /// Three presets over 15 reviews, interleaved in the review log: the
    /// default one and "Other" have uncovered reviews, "Covered" has none.
    /// Returns the collection and the decks of "Other" and "Covered".
    fn collection_with_three_presets() -> Result<(Collection, Deck, Deck)> {
        let mut col = Collection::new();
        let other = DeckAdder::new("other")
            .with_config(|config| config.name = "Other".to_string())
            .add(&mut col);
        let covered = DeckAdder::new("covered")
            .with_config(|config| config.name = "Covered".to_string())
            .add(&mut col);
        for days_ago in [50, 40, 30, 20, 10] {
            rated_card(&mut col, days_ago);
            let review = rated_card_in(&mut col, covered.id, days_ago + 1);
            cover(&mut col, review)?;
            rated_card_in(&mut col, other.id, days_ago + 2);
        }
        Ok((col, other, covered))
    }

    /// The collection is free between the parts of the read, and the parts
    /// give exactly the presets of one read, whatever their size.
    // Pins spec/ui.md#ui.stats-fsrs-predictions-ready
    #[test]
    fn stale_presets_in_parts_are_those_of_one_read() -> Result<()> {
        let (mut col, other, _) = collection_with_three_presets()?;
        let whole = col.presets_with_stale_fsrs_review_predictions()?;
        let mut expected = vec![DeckConfigId(1), other.config_id().unwrap()];
        expected.sort_unstable();
        assert_eq!(whole, expected);
        for part_rows in [1, 2, 3, 7, 15, 1_000] {
            let mut holds = 0;
            let in_parts =
                presets_with_stale_fsrs_review_predictions_in_parts(part_rows, &mut |step| {
                    holds += 1;
                    step(&mut col)
                })?;
            assert_eq!(in_parts, whole, "part_rows={part_rows}");
            // the parts of the 15 review-log rows (one more when a last part
            // is full) and the mapping to presets: never one hold for the
            // whole read
            assert_eq!(holds, (15 / part_rows + 1) + 1, "part_rows={part_rows}");
        }
        Ok(())
    }

    /// A write between two parts would mix two collections in one read, so
    /// the read starts over; a collection that keeps changing is read in one
    /// piece. Either way the presets are those of one read of the collection
    /// as the pass finds it at the end.
    #[test]
    fn a_write_between_the_parts_makes_the_stale_presets_read_again() -> Result<()> {
        let (mut col, _, covered) = collection_with_three_presets()?;
        let covered_preset = covered.config_id().unwrap();
        let days_ago = std::cell::Cell::new(100);
        // an uncovered review early in the review log, in a part the read
        // has already passed: without the check, the pass would miss it
        let review_now = |col: &mut Collection| {
            days_ago.set(days_ago.get() + 1);
            rated_card_in(col, covered.id, days_ago.get());
        };

        let mut holds = 0;
        let stale = presets_with_stale_fsrs_review_predictions_in_parts(4, &mut |step| {
            holds += 1;
            if holds == 2 {
                review_now(&mut col);
            }
            step(&mut col)
        })?;
        assert_eq!(stale, col.presets_with_stale_fsrs_review_predictions()?);
        assert!(stale.contains(&covered_preset));
        // the first read stopped at its second part; the second read is
        // whole: the 16 review-log rows and the mapping
        assert_eq!(holds, 2 + ((16 / 4 + 1) + 1));

        // a review before every hold: after three reads, one piece
        let mut holds = 0;
        let stale = presets_with_stale_fsrs_review_predictions_in_parts(4, &mut |step| {
            holds += 1;
            review_now(&mut col);
            step(&mut col)
        })?;
        assert_eq!(stale, col.presets_with_stale_fsrs_review_predictions()?);
        assert_eq!(holds, 3 * 2 + 1);
        Ok(())
    }

    /// A measurement harness, not a test: how long the stale-preset read
    /// holds the collection with the query it replaced, in one piece, and
    /// read in parts, at most in one hold, in alternating pairs. One CSV line
    /// per pair: `pair,old_query_ms,longest_hold_ms,in_parts_total_ms`. Point
    /// `ANKI_STALE_PRESETS_BENCH_COL` at a COPY of a collection and run
    /// `cargo test -p anki --release bench_stale_presets_holds -- --ignored
    /// --nocapture`; `ANKI_STALE_PRESETS_BENCH_PAIRS` sets the pairs (100).
    #[test]
    #[ignore]
    fn bench_stale_presets_holds() -> Result<()> {
        use std::time::Instant;

        let path = std::env::var("ANKI_STALE_PRESETS_BENCH_COL")
            .expect("set ANKI_STALE_PRESETS_BENCH_COL to a copy of a collection");
        let pairs: usize = std::env::var("ANKI_STALE_PRESETS_BENCH_PAIRS")
            .map_or(100, |pairs| pairs.parse().unwrap());
        let mut col = crate::collection::CollectionBuilder::new(path).build()?;
        let ms = |duration: std::time::Duration| duration.as_secs_f64() * 1000.0;
        // the query before this change, led by the cards
        let old_query = |col: &mut Collection| -> Result<_> {
            let at = Instant::now();
            let uncovered: Vec<(DeckId, u32)> = col
                .storage
                .db
                .prepare(
                    "select c.did, count(*) from revlog r
                     join cards c on c.id = r.cid
                     where r.ease > 0
                       and not exists (
                           select 1 from retrievability_cache.search_stats_fsrs_review_retrievability t
                           where t.revlog_id = r.id and t.sample_role = 'validation_fold'
                       )
                     group by c.did",
                )?
                .query_and_then((), |row| Ok((row.get(0)?, row.get(1)?)))?
                .collect::<Result<_>>()?;
            let stale = col.presets_of_uncovered_decks(uncovered)?;
            Ok((stale, ms(at.elapsed())))
        };
        let in_parts = |col: &mut Collection| -> Result<_> {
            let mut longest = 0f64;
            let at = Instant::now();
            let stale = presets_with_stale_fsrs_review_predictions_in_parts(
                STALE_PRESETS_PART_ROWS,
                &mut |step| {
                    let held = Instant::now();
                    let result = step(col);
                    longest = longest.max(ms(held.elapsed()));
                    result
                },
            )?;
            Ok((stale, longest, ms(at.elapsed())))
        };
        println!("pair,old_query_ms,longest_hold_ms,in_parts_total_ms");
        for pair in 0..pairs {
            let (old, parts) = if pair % 2 == 0 {
                let old = old_query(&mut col)?;
                (old, in_parts(&mut col)?)
            } else {
                let parts = in_parts(&mut col)?;
                (old_query(&mut col)?, parts)
            };
            assert_eq!(old.0, parts.0, "pair {pair}");
            println!("{pair},{:.2},{:.2},{:.2}", old.1, parts.1, parts.2);
        }
        Ok(())
    }
}
