// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The Stats page's model-quality graphs (spec ui.stats-model-metrics):
//! each algorithm's predicted probability of recall before every rating of
//! the search, with the answer itself.
//!
//! The predictions are not computed here. Both models write them per review
//! while they run - FSRS-7 when its parameters are optimized, RWKV when its
//! state cache is built - and this reads those rows. A row counts only when
//! nothing that produced it was fitted on that very review. Each model is
//! scored on every rating it can score, and the ratings both models scored
//! are counted, so the graph can say which reviews the two numbers share.

use std::collections::HashMap;

use anki_proto::deck_config::deck_configs_for_update::SchedulingAlgorithm as SchedulingAlgorithmProto;
use anki_proto::stats::review_metrics_progress::Series;
use anki_proto::stats::review_metrics_progress::Unavailable;
use anki_proto::stats::CalibrationBin;
use anki_proto::stats::ReviewPredictionsResponse;
use rayon::prelude::*;

use crate::prelude::*;
use crate::search::SortMode;
use crate::stats::algorithms::PredictionStore;
use crate::stats::algorithms::PredictsRecall;
use crate::stats::algorithms::RoleChoice;
use crate::stats::algorithms::FSRS7;
use crate::stats::algorithms::RWKV_CURVE;
use crate::stats::algorithms::RWKV_INSTANT;
use crate::stats::roc::roc_curve;
use crate::stats::roc::CURVE_POINTS;
use crate::storage::SearchedRating;
use crate::storage::SqliteStorage;

/// One model's cached predictions for the searched ratings, and the role
/// they came from. The predictions are in ascending review order, one
/// entry per review.
struct CachedPredictions {
    role: String,
    by_review: Vec<(RevlogId, f32)>,
}

impl CachedPredictions {
    fn none() -> Self {
        Self {
            role: String::new(),
            by_review: vec![],
        }
    }
}

/// Walks one model's predictions beside the ratings. Both lists are in
/// ascending review order, so each model is joined to the ratings in one
/// pass. A hash map would have to be built first, and a collection with a
/// million ratings builds three of them only to read each entry once.
struct PredictionCursor<'a> {
    by_review: &'a [(RevlogId, f32)],
    next: usize,
}

impl<'a> PredictionCursor<'a> {
    fn new(predictions: &'a CachedPredictions) -> Self {
        Self {
            by_review: &predictions.by_review,
            next: 0,
        }
    }

    /// This model's prediction of `review`, if it has one. The reviews are
    /// asked for in ascending order, so the cursor only ever moves on.
    fn of(&mut self, review: RevlogId) -> Option<f32> {
        while self
            .by_review
            .get(self.next)
            .is_some_and(|(id, _)| *id < review)
        {
            self.next += 1;
        }
        match self.by_review.get(self.next) {
            Some(&(id, prediction)) if id == review => Some(prediction),
            _ => None,
        }
    }
}

/// Every rating of the search and period that at least one algorithm has
/// an honest row for, with each algorithm's prediction of it. The three
/// prediction lists and `remembered` have the same length; an algorithm
/// with no row for a rating has NaN there, never another algorithm's
/// value. These lists never leave the backend: a large collection holds
/// millions of entries per algorithm, and only the series below are drawn.
#[derive(Default)]
struct ScoredRatings {
    revlog_ids: Vec<i64>,
    card_ids: Vec<i64>,
    remembered: Vec<bool>,
    fsrs_predictions: Vec<f32>,
    rwkv_predictions: Vec<f32>,
    rwkv_curve_predictions: Vec<f32>,
    fsrs_role: String,
    rwkv_role: String,
    rwkv_curve_role: String,
    shared: u32,
    fsrs_only: u32,
    rwkv_only: u32,
    unscored: u32,
    newest_scored_secs: i64,
    newer_reviews: u32,
    rwkv_curve_oldest_secs: i64,
    rwkv_curve_earlier_reviews: u32,
}

impl Collection {
    /// The model-quality graphs' data (spec ui.stats-model-metrics): one
    /// finished series per algorithm, with the counts the page prints
    /// under the graph.
    pub(crate) fn review_predictions(
        &mut self,
        search: &str,
        days: u32,
    ) -> Result<ReviewPredictionsResponse> {
        let rows = self.scored_ratings(search, days)?;
        // Each algorithm's finished series: the curve, its area, the
        // calibration bins and the tiles' averages, all computed here so
        // the predictions themselves never cross the boundary. The three
        // read the same lists and write nothing, so they are computed at
        // the same time; the page waits for the slowest of them instead of
        // for all three, and every number is the one a single thread gave.
        let finished = [
            (
                SchedulingAlgorithmProto::Fsrs7,
                &rows.fsrs_predictions,
                &rows.fsrs_role,
            ),
            (
                SchedulingAlgorithmProto::RwkvCurve,
                &rows.rwkv_curve_predictions,
                &rows.rwkv_curve_role,
            ),
            (
                SchedulingAlgorithmProto::RwkvInstant,
                &rows.rwkv_predictions,
                &rows.rwkv_role,
            ),
        ]
        .into_par_iter()
        .map(|(algorithm, predictions, role)| {
            let bins = calibration_bins(predictions, &rows.remembered, &rows.card_ids);
            series(algorithm, predictions, &rows.remembered, role, bins)
        })
        .collect();
        let mut response = ReviewPredictionsResponse {
            series: finished,
            scored: rows.revlog_ids.len().try_into().unwrap_or(u32::MAX),
            shared: rows.shared,
            fsrs_only: rows.fsrs_only,
            rwkv_only: rows.rwkv_only,
            unscored: rows.unscored,
            newest_scored_secs: rows.newest_scored_secs,
            newer_reviews: rows.newer_reviews,
            rwkv_curve_oldest_secs: rows.rwkv_curve_oldest_secs,
            rwkv_curve_earlier_reviews: rows.rwkv_curve_earlier_reviews,
            // a comparison needs both models to have scored something here
            shared_ratings: rows.shared + rows.fsrs_only > 0 && rows.shared + rows.rwkv_only > 0,
            um_plus: vec![],
        };
        // one entry per pair of algorithms that share ratings; three
        // algorithms make three pairs, and a pair with no shared rating is
        // simply absent
        let pairs = [
            (
                SchedulingAlgorithmProto::Fsrs7,
                &rows.fsrs_predictions,
                SchedulingAlgorithmProto::RwkvCurve,
                &rows.rwkv_curve_predictions,
            ),
            (
                SchedulingAlgorithmProto::Fsrs7,
                &rows.fsrs_predictions,
                SchedulingAlgorithmProto::RwkvInstant,
                &rows.rwkv_predictions,
            ),
            (
                SchedulingAlgorithmProto::RwkvCurve,
                &rows.rwkv_curve_predictions,
                SchedulingAlgorithmProto::RwkvInstant,
                &rows.rwkv_predictions,
            ),
        ];
        for (algorithm_a, predictions_a, algorithm_b, predictions_b) in pairs {
            if let Some(pair) = um_plus_pair(
                algorithm_a,
                predictions_a,
                algorithm_b,
                predictions_b,
                &rows.remembered,
            ) {
                response.um_plus.push(pair);
            }
        }
        Ok(response)
    }

    /// Reads every algorithm's cached predictions of the search's ratings.
    fn scored_ratings(&mut self, search: &str, days: u32) -> Result<ScoredRatings> {
        let timing = self.timing_today()?;
        let cutoff = if days == 0 {
            TimestampMillis(0)
        } else {
            TimestampMillis((timing.next_day_at.0 - (days as i64) * 86_400) * 1000)
        };
        let guard = self.search_cards_into_table_with_stats_search(
            search,
            SortMode::NoOrder,
            Some(search),
        )?;
        let storage = &guard.col.storage;
        // the same rule the other Stats reads use: once the search covers
        // much of the collection, one pass over the review log beats an
        // index lookup per card
        let collection_cards: u32 =
            storage
                .db
                .query_row("select count() from cards", [], |row| row.get(0))?;
        let many_cards = guard.cards * 2 >= collection_cards as usize;
        // each algorithm names itself; no read can reach another
        // algorithm's rows without saying whose they are
        let (mut ratings, fsrs, rwkv, rwkv_curve) =
            read_ratings_and_predictions(storage, cutoff, many_cards)?;
        // No algorithm knows anything about a card before its first answer,
        // so that rating says nothing about any of them; srs-benchmark
        // leaves it out too (Andrew, 2026-09-23). The same holds for the
        // first rating after a Forget or at a new learning start, where
        // every algorithm starts the card again
        let first_ratings = storage.sequence_start_ratings_of_searched_cards()?;

        let mut rows = ScoredRatings {
            fsrs_role: fsrs.role.clone(),
            rwkv_role: rwkv.role.clone(),
            rwkv_curve_role: rwkv_curve.role.clone(),
            ..Default::default()
        };
        // Every rating an algorithm has an honest row for is read; when two
        // or more algorithms have rows, only the ratings all of them scored
        // are kept, below, so that their numbers compare the same reviews
        // (spec ui.stats-model-metrics).
        ratings.sort_unstable_by_key(|entry| entry.id);
        let mut fsrs_cursor = PredictionCursor::new(&fsrs);
        let mut rwkv_cursor = PredictionCursor::new(&rwkv);
        let mut curve_cursor = PredictionCursor::new(&rwkv_curve);
        // where the ratings the models reach begin and end, for the two
        // freshness counts below
        let mut last_scored: Option<usize> = None;
        let mut first_curve: Option<usize> = None;
        for (index, entry) in ratings.iter().enumerate() {
            if first_ratings.binary_search(&entry.id).is_ok() {
                continue;
            }
            let fsrs_value = fsrs_cursor.of(entry.id);
            let rwkv_value = rwkv_cursor.of(entry.id);
            let curve_value = curve_cursor.of(entry.id);
            if fsrs_value.is_some() || rwkv_value.is_some() || curve_value.is_some() {
                last_scored = Some(index);
                rows.revlog_ids.push(entry.id.0);
                rows.card_ids.push(entry.cid.0);
                rows.remembered.push(entry.button_chosen > 1);
                // a model without a row for this rating has no number
                // here, and its series simply skips the rating rather
                // than borrowing the other model's value
                rows.fsrs_predictions.push(fsrs_value.unwrap_or(f32::NAN));
                rows.rwkv_predictions.push(rwkv_value.unwrap_or(f32::NAN));
                rows.rwkv_curve_predictions
                    .push(curve_value.unwrap_or(f32::NAN));
            }
            if curve_value.is_some() && first_curve.is_none() {
                first_curve = Some(index);
            }
            match (fsrs_value, rwkv_value) {
                (Some(_), Some(_)) => rows.shared += 1,
                (Some(_), None) => rows.fsrs_only += 1,
                (None, Some(_)) => rows.rwkv_only += 1,
                (None, None) => rows.unscored += 1,
            }
        }

        keep_the_ratings_every_algorithm_scored(&mut rows);

        // how fresh the predictions are: the newest rating either model has
        // scored, and the ratings of the search after it
        let counted = |count: usize| count.try_into().unwrap_or(u32::MAX);
        match last_scored {
            Some(index) => {
                rows.newest_scored_secs = ratings[index].id.as_secs().0;
                rows.newer_reviews = counted(ratings.len() - 1 - index);
            }
            None => rows.newer_reviews = counted(ratings.len()),
        }

        // How far back the curve recording reaches. A collection that has
        // been replayed since the recording shipped has rows for its whole
        // history; one that has not has rows only from the day it started,
        // and the graph must say so rather than show three days of data
        // looking like three years.
        if let Some(index) = first_curve {
            rows.rwkv_curve_oldest_secs = ratings[index].id.as_secs().0;
            rows.rwkv_curve_earlier_reviews = counted(index);
        }
        Ok(rows)
    }
}

/// With two or more algorithms that scored any rating, only the ratings all
/// of them scored stay: each algorithm on its own ratings would compare
/// different reviews. On Andrew's collection the ratings only RWKV-Instant
/// had (mostly first reviews) put it below RWKV-Curve, while on the reviews
/// both scored it was above (spec ui.stats-model-metrics). One algorithm
/// alone keeps all of its ratings. The counts (`shared`, `fsrs_only`,
/// `rwkv_only`, `unscored`) are the coverage before this, as they were.
fn keep_the_ratings_every_algorithm_scored(rows: &mut ScoredRatings) {
    let has_any = |predictions: &[f32]| predictions.iter().any(|value| value.is_finite());
    let present = [
        has_any(&rows.fsrs_predictions),
        has_any(&rows.rwkv_predictions),
        has_any(&rows.rwkv_curve_predictions),
    ];
    if present.iter().filter(|&&present| present).count() < 2 {
        return;
    }
    let keep: Vec<bool> = (0..rows.revlog_ids.len())
        .map(|index| {
            [
                rows.fsrs_predictions[index],
                rows.rwkv_predictions[index],
                rows.rwkv_curve_predictions[index],
            ]
            .iter()
            .zip(present)
            .all(|(value, present)| !present || value.is_finite())
        })
        .collect();
    fn retain<T: Copy>(values: &mut Vec<T>, keep: &[bool]) {
        let mut index = 0;
        values.retain(|_| {
            index += 1;
            keep[index - 1]
        });
    }
    retain(&mut rows.revlog_ids, &keep);
    retain(&mut rows.card_ids, &keep);
    retain(&mut rows.remembered, &keep);
    retain(&mut rows.fsrs_predictions, &keep);
    retain(&mut rows.rwkv_predictions, &keep);
    retain(&mut rows.rwkv_curve_predictions, &keep);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;
    use crate::storage::FsrsReviewRetrievabilityCacheRow;
    use crate::storage::FsrsReviewRetrievabilitySampleRole;
    use crate::storage::RwkvReviewRetrievabilityCacheRow;
    use crate::storage::RwkvReviewRetrievabilitySampleRole;
    use crate::tests::NoteAdder;

    /// One algorithm's series in the response.
    fn series_of(
        response: &ReviewPredictionsResponse,
        algorithm: SchedulingAlgorithmProto,
    ) -> &Series {
        response
            .series
            .iter()
            .find(|series| series.algorithm == algorithm as i32)
            .unwrap()
    }

    /// A review card whose first rating, long ago, no algorithm scored: the
    /// graphs leave a card's first rating out, so the ratings a test adds
    /// are the ones it means.
    fn add_card(col: &mut Collection) -> CardId {
        let note = NoteAdder::basic(col).add(col);
        let mut card = col.storage.all_cards_of_note(note.id).unwrap().remove(0);
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        col.storage.update_card(&card).unwrap();
        // one id per card: in 2001, before every rating a test makes
        let first = RevlogEntry {
            id: RevlogId(1_000_000_000_000 + card.id.0 % 10_000_000_000),
            cid: card.id,
            button_chosen: 3,
            interval: 3,
            ease_factor: 2500,
            review_kind: RevlogReviewKind::Review,
            ..Default::default()
        };
        col.storage.add_revlog_entry(&first, false).unwrap();
        card.id
    }

    /// A rating on the day `day` (0 = today), at noon of that day.
    fn rate(col: &mut Collection, card: CardId, day: i32, button: u8) -> RevlogId {
        let next_day_at = col.timing_today().unwrap().next_day_at;
        let entry = RevlogEntry {
            id: RevlogId((next_day_at.0 + day as i64 * 86_400 - 43_200) * 1000),
            cid: card,
            button_chosen: button,
            interval: 3,
            ease_factor: 2500,
            review_kind: RevlogReviewKind::Review,
            ..Default::default()
        };
        col.storage.add_revlog_entry(&entry, false).unwrap();
        entry.id
    }

    fn store_fsrs(
        col: &Collection,
        review: RevlogId,
        prediction: f32,
        role: FsrsReviewRetrievabilitySampleRole,
    ) {
        col.storage
            .set_fsrs_review_retrievability_predictions(
                &[FsrsReviewRetrievabilityCacheRow {
                    revlog_id: review,
                    prediction,
                    sample_role: role,
                    fold_index: -1,
                }],
                "test",
            )
            .unwrap();
    }

    fn store_rwkv_role(
        col: &Collection,
        review: RevlogId,
        prediction: f32,
        role: RwkvReviewRetrievabilitySampleRole,
    ) {
        col.storage
            .set_rwkv_review_retrievability_predictions(
                &[RwkvReviewRetrievabilityCacheRow {
                    revlog_id: review,
                    prediction,
                    sample_role: role,
                    fold_index: -1,
                }],
                "test",
            )
            .unwrap();
    }

    fn store_rwkv(col: &Collection, review: RevlogId, prediction: f32) {
        col.storage
            .set_rwkv_review_retrievability_predictions(
                &[RwkvReviewRetrievabilityCacheRow {
                    revlog_id: review,
                    prediction,
                    sample_role: RwkvReviewRetrievabilitySampleRole::FinalFit,
                    fold_index: -1,
                }],
                "test",
            )
            .unwrap();
    }

    /// A review-log row of any kind, on the day `day` (0 = today).
    fn log_entry(
        col: &mut Collection,
        card: CardId,
        day: i32,
        button: u8,
        ease_factor: u32,
        review_kind: RevlogReviewKind,
    ) -> RevlogId {
        let next_day_at = col.timing_today().unwrap().next_day_at;
        let entry = RevlogEntry {
            id: RevlogId((next_day_at.0 + day as i64 * 86_400 - 43_200) * 1000),
            cid: card,
            button_chosen: button,
            interval: 3,
            ease_factor,
            review_kind,
            ..Default::default()
        };
        col.storage.add_revlog_entry(&entry, false).unwrap();
        entry.id
    }

    // Pins spec/ui.md#ui.stats-model-metrics: the graphs read the ratings
    // of the searched cards that affect scheduling - the same ones the
    // whole review-log row gives, by either of the two read plans.
    #[test]
    fn the_ratings_read_are_the_searched_cards_ratings() -> Result<()> {
        let mut col = Collection::new();
        let cards: Vec<CardId> = (0..4).map(|_| add_card(&mut col)).collect();
        let mut rated = vec![];
        for (index, card) in cards.iter().enumerate() {
            for day in 0..5 {
                rated.push(rate(
                    &mut col,
                    *card,
                    -40 + day * 4 + index as i32,
                    1 + (day % 4) as u8,
                ));
            }
        }
        // a set due date, a reset and a cram answer are not ratings; a
        // filtered review that kept its ease is one
        let due_date = log_entry(&mut col, cards[0], -12, 0, 2500, RevlogReviewKind::Manual);
        let reset = log_entry(&mut col, cards[0], -11, 0, 0, RevlogReviewKind::Manual);
        let cram = log_entry(&mut col, cards[1], -10, 3, 0, RevlogReviewKind::Filtered);
        let kept = log_entry(&mut col, cards[1], -9, 3, 2500, RevlogReviewKind::Filtered);

        for search in ["", "deck:Default", "deck:none"] {
            let guard = col.search_cards_into_table(search, SortMode::NoOrder)?;
            let storage = &guard.col.storage;
            let mut expected: Vec<(RevlogId, CardId, u8)> = storage
                .get_revlog_entries_for_searched_cards()?
                .into_iter()
                .filter(|entry| entry.has_rating_and_affects_scheduling())
                .map(|entry| (entry.id, entry.cid, entry.button_chosen))
                .collect();
            expected.sort_unstable();
            for many_cards in [false, true] {
                let mut read: Vec<(RevlogId, CardId, u8)> = storage
                    .searched_ratings_that_affect_scheduling(TimestampMillis(0), many_cards)?
                    .into_iter()
                    .map(|rating| (rating.id, rating.cid, rating.button_chosen))
                    .collect();
                read.sort_unstable();
                assert_eq!(read, expected, "search {search:?}, many_cards {many_cards}");
            }
            // the period is exclusive of its own moment, as the graphs ask
            let after =
                storage.searched_ratings_that_affect_scheduling(TimestampMillis(kept.0), false)?;
            assert!(after.iter().all(|rating| rating.id.0 > kept.0));
            drop(guard);
        }
        // every rating is there, the three rows that are not ratings are
        // not, and the fixture is not accidentally empty
        let guard = col.search_cards_into_table("", SortMode::NoOrder)?;
        let all = guard
            .col
            .storage
            .searched_ratings_that_affect_scheduling(TimestampMillis(0), true)?;
        let ids: Vec<RevlogId> = all.iter().map(|rating| rating.id).collect();
        // (and each card's first rating, which add_card makes)
        assert_eq!(ids.len(), rated.len() + 1 + cards.len());
        assert!(rated.iter().all(|review| ids.contains(review)));
        assert!(ids.contains(&kept));
        for absent in [due_date, reset, cram] {
            assert!(!ids.contains(&absent));
        }
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn only_rows_the_algorithm_had_not_seen_are_used() -> Result<()> {
        let mut col = Collection::new();
        let card = add_card(&mut col);
        let fitted = rate(&mut col, card, -20, 3);
        let honest = rate(&mut col, card, -10, 1);
        // the final fit has seen the review it predicts, so it never counts
        store_fsrs(
            &col,
            fitted,
            0.9,
            FsrsReviewRetrievabilitySampleRole::FinalFit,
        );
        store_fsrs(
            &col,
            honest,
            0.4,
            FsrsReviewRetrievabilitySampleRole::ValidationFold,
        );
        store_rwkv(&col, fitted, 0.8);
        store_rwkv(&col, honest, 0.3);

        let rows = col.scored_ratings("", 0)?;
        // FSRS-7's final-fit row is never used, so FSRS-7 has no number for
        // that rating, and with two algorithms only the rating both scored
        // is kept; the other is never filled in from RWKV's
        assert_eq!(rows.revlog_ids, vec![honest.0]);
        assert_eq!(rows.fsrs_predictions, vec![0.4]);
        assert_eq!(rows.rwkv_predictions, vec![0.3]);
        assert_eq!(rows.remembered, vec![false]);
        assert_eq!(rows.fsrs_role, "validation_fold");
        // RWKV's weights saw no review of this collection, so any role
        // counts, and it names none
        assert_eq!(rows.rwkv_role, "");
        // the coverage counts are taken before the shared ratings are kept
        assert_eq!(rows.rwkv_only, 1);
        assert_eq!(rows.fsrs_only, 0);
        assert_eq!(rows.shared, 1);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn two_algorithms_are_scored_on_the_ratings_both_scored() -> Result<()> {
        let mut col = Collection::new();
        let card = add_card(&mut col);
        let shared = rate(&mut col, card, -30, 3);
        let fsrs_alone = rate(&mut col, card, -20, 3);
        let rwkv_alone = rate(&mut col, card, -15, 3);
        let neither = rate(&mut col, card, -5, 3);
        for review in [shared, fsrs_alone] {
            store_fsrs(
                &col,
                review,
                0.5,
                FsrsReviewRetrievabilitySampleRole::PostOptimization,
            );
        }
        for review in [shared, rwkv_alone] {
            store_rwkv(&col, review, 0.6);
        }

        let rows = col.scored_ratings("", 0)?;
        // only the rating both models scored: each model on its own
        // ratings would compare different reviews
        assert_eq!(rows.revlog_ids, vec![shared.0]);
        assert_eq!(rows.fsrs_predictions, vec![0.5]);
        assert_eq!(rows.rwkv_predictions, vec![0.6]);
        // the coverage is still counted
        assert_eq!(rows.shared, 1);
        assert_eq!(rows.fsrs_only, 1);
        assert_eq!(rows.rwkv_only, 1);
        assert_eq!(rows.unscored, 1);
        assert_eq!(col.review_predictions("", 0)?.scored, 1);
        assert_eq!(rows.fsrs_role, "post_optimization");
        let _ = (fsrs_alone, rwkv_alone, neither);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn a_single_rating_of_one_algorithm_narrows_the_comparison_to_it() -> Result<()> {
        let mut col = Collection::new();
        let card = add_card(&mut col);
        // one model with many rows beside a model with a single row: the
        // single row must not take the other model's ratings away
        let mut reviews = vec![];
        for index in 0..10 {
            let review = rate(&mut col, card, -40 + index, 3);
            store_rwkv(&col, review, 0.6);
            reviews.push(review);
        }
        store_fsrs(
            &col,
            reviews[0],
            0.5,
            FsrsReviewRetrievabilitySampleRole::PostOptimization,
        );

        let rows = col.scored_ratings("", 0)?;
        // the comparison rests on the one rating both scored; the page
        // says so with the count
        assert_eq!(rows.revlog_ids, vec![reviews[0].0]);
        assert_eq!(rows.shared, 1);
        assert_eq!(rows.rwkv_only, 9);
        assert!(col.review_predictions("", 0)?.shared_ratings);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn one_algorithm_alone_keeps_all_of_its_ratings() -> Result<()> {
        let mut col = Collection::new();
        let card = add_card(&mut col);
        let first = rate(&mut col, card, -30, 3);
        let second = rate(&mut col, card, -20, 1);
        // only RWKV has rows: one curve is not a comparison, so it keeps
        // every rating it can score
        store_rwkv(&col, first, 0.9);
        store_rwkv(&col, second, 0.4);

        let rows = col.scored_ratings("", 0)?;
        assert_eq!(rows.revlog_ids, vec![first.0, second.0]);
        assert_eq!(rows.rwkv_predictions, vec![0.9, 0.4]);
        assert!(!col.review_predictions("", 0)?.shared_ratings);
        assert!(rows.fsrs_role.is_empty());
        // FSRS-7 has no number for these ratings, and is not filled in
        assert!(rows.fsrs_predictions.iter().all(|value| value.is_nan()));
        assert_eq!(rows.rwkv_only, 2);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn rwkv_takes_the_newest_row_of_each_rating_across_its_roles() -> Result<()> {
        let mut col = Collection::new();
        let card = add_card(&mut col);
        let reviews: Vec<RevlogId> = (0..5)
            .map(|index| rate(&mut col, card, -30 + index, 3))
            .collect();
        // an older recording under one role, then a newer one of two of the
        // same ratings under another: RWKV's roles carry no honesty order,
        // and the newest row is the current model's
        for review in &reviews {
            store_rwkv_role(
                &col,
                *review,
                0.8,
                RwkvReviewRetrievabilitySampleRole::FinalFit,
            );
        }
        std::thread::sleep(std::time::Duration::from_millis(5));
        for review in &reviews[..2] {
            store_rwkv_role(
                &col,
                *review,
                0.5,
                RwkvReviewRetrievabilitySampleRole::PostOptimization,
            );
        }

        let rows = col.scored_ratings("", 0)?;
        // every rating either role scored, each once, the newer row winning
        assert_eq!(rows.revlog_ids.len(), 5);
        assert_eq!(rows.rwkv_predictions, vec![0.5, 0.5, 0.8, 0.8, 0.8]);
        // more rows of a role do not make it win: the bigger role is older
        assert_eq!(rows.rwkv_role, "");
        assert_eq!(rows.unscored, 0);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn a_cards_first_rating_is_never_scored() -> Result<()> {
        let mut col = Collection::new();
        let note = NoteAdder::basic(&mut col).add(&mut col);
        let card = col.storage.all_cards_of_note(note.id)?.remove(0).id;
        let first = rate(&mut col, card, -30, 1);
        let second = rate(&mut col, card, -20, 3);
        for review in [first, second] {
            store_rwkv(&col, review, 0.7);
        }
        // one algorithm, so nothing narrows the ratings but this rule
        let rows = col.scored_ratings("", 0)?;
        assert_eq!(rows.revlog_ids, vec![second.0]);
        // a period that starts after the first rating still knows it was
        // the first: the rule looks at the card's whole history
        let third = rate(&mut col, card, -2, 3);
        store_rwkv(&col, third, 0.7);
        assert_eq!(
            col.scored_ratings("", 25)?.revlog_ids,
            vec![second.0, third.0]
        );
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics: the first rating after a
    // Forget, and a learning start after other ratings, begin a new learning
    // sequence, so they are left out like a card's first rating
    #[test]
    fn ratings_after_a_reset_or_a_learning_start_are_never_scored() -> Result<()> {
        let mut col = Collection::new();
        let card = add_card(&mut col);
        let learn = |col: &mut Collection, day, button| {
            log_entry(col, card, day, button, 0, RevlogReviewKind::Learning)
        };
        let first = learn(&mut col, -60, 3);
        let second_step = learn(&mut col, -59, 3);
        let review = rate(&mut col, card, -50, 3);
        // Forget: a manual row with a zero ease factor
        log_entry(&mut col, card, -45, 0, 0, RevlogReviewKind::Manual);
        let after_forget = rate(&mut col, card, -40, 3);
        let review_2 = rate(&mut col, card, -30, 1);
        // Set Due Date is a manual row with an ease factor: no reset
        log_entry(&mut col, card, -25, 0, 2500, RevlogReviewKind::Manual);
        let after_set_due = rate(&mut col, card, -20, 3);
        // an old client's relearn from scratch: a Learning row after reviews
        let relearn_start = learn(&mut col, -10, 1);
        let relearn_step = learn(&mut col, -9, 3);
        let all = [
            first,
            second_step,
            review,
            after_forget,
            review_2,
            after_set_due,
            relearn_start,
            relearn_step,
        ];
        for id in all {
            store_rwkv(&col, id, 0.7);
        }

        let rows = col.scored_ratings("", 0)?;
        assert_eq!(
            rows.revlog_ids,
            [second_step, review, review_2, after_set_due, relearn_step]
                .map(|id| id.0)
                .to_vec()
        );
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn calibration_bins_and_their_intervals() -> Result<()> {
        let mut col = Collection::new();
        let card = add_card(&mut col);
        let mut reviews = vec![];
        for index in 0..20 {
            let review = rate(
                &mut col,
                card,
                -100 + index,
                if index % 4 == 0 { 1 } else { 3 },
            );
            store_rwkv(&col, review, 0.75);
            reviews.push(review);
        }

        let response = col.review_predictions("", 0)?;
        let bins = &series_of(&response, SchedulingAlgorithmProto::RwkvInstant).bins;
        // every rating has the same prediction, so they share one bin
        assert_eq!(bins.len(), 1);
        let bin = &bins[0];
        assert_eq!(bin.count, 20);
        assert_eq!(bin.index, bin_of(0.75) as u32);
        // 15 of the 20 answers were remembered
        assert!((bin.sum_remembered - 15.0).abs() < 1e-9);
        assert!((bin.sum_predicted - 15.0).abs() < 1e-4);
        // one card gives a degenerate interval: every resample is the same
        assert!((bin.low - 0.75).abs() < 1e-9);
        assert!((bin.high - 0.75).abs() < 1e-9);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn um_plus_groups_the_ratings_by_how_far_the_algorithms_differ() -> Result<()> {
        let mut col = Collection::new();
        let card = add_card(&mut col);
        // four ratings, two remembered; the two algorithms differ by 0.2
        // on every one of them, so they all fall in one group
        for index in 0..4 {
            let review = rate(&mut col, card, -40 + index, if index < 2 { 3 } else { 1 });
            store_fsrs(
                &col,
                review,
                0.9,
                FsrsReviewRetrievabilitySampleRole::ValidationFold,
            );
            store_rwkv(&col, review, 0.7);
        }

        let response = col.review_predictions("", 0)?;
        let pair = &response.um_plus[0];
        assert_eq!(pair.reviews, 4);
        assert_eq!(pair.bins.len(), 1);
        let bin = &pair.bins[0];
        assert_eq!(bin.index, um_bin_of(0.2) as u32);
        assert!((bin.sum_difference - 0.8).abs() < 1e-5);
        // FSRS-7 predicted 0.9 where the mean answer was 0.5, RWKV 0.7
        assert!((pair.um_a - 0.4).abs() < 1e-5, "{}", pair.um_a);
        assert!((pair.um_b - 0.2).abs() < 1e-5, "{}", pair.um_b);
        // closer to zero is better, so RWKV wins this pair
        assert!(pair.um_b < pair.um_a);
        // one group gives no slope
        assert_eq!(pair.slope_a, 0.0);
        Ok(())
    }

    /// The bootstrap as it ran before the rounds went parallel: one
    /// stream, one thread, the cards in the order given.
    fn one_thread_intervals(cards: &[Vec<BinTally>]) -> Vec<(f64, f64)> {
        let mut shares: Vec<Vec<f64>> = vec![vec![]; BIN_COUNT];
        if cards.is_empty() {
            return vec![(0.0, 0.0); BIN_COUNT];
        }
        let mut state = BOOTSTRAP_SEED;
        let mut next = || {
            state ^= state >> 12;
            state ^= state << 25;
            state ^= state >> 27;
            state.wrapping_mul(0x2545_F491_4F6C_DD1D)
        };
        for _ in 0..BOOTSTRAP_ROUNDS {
            let mut remembered = [0.0f64; BIN_COUNT];
            let mut counts = [0.0f64; BIN_COUNT];
            for _ in 0..cards.len() {
                for tally in &cards[(next() % cards.len() as u64) as usize] {
                    remembered[tally.bin] += tally.remembered;
                    counts[tally.bin] += tally.count;
                }
            }
            for bin in 0..BIN_COUNT {
                if counts[bin] > 0.0 {
                    shares[bin].push(remembered[bin] / counts[bin]);
                }
            }
        }
        shares
            .into_iter()
            .map(|mut values| {
                if values.is_empty() {
                    return (0.0, 0.0);
                }
                values.sort_by(|a, b| a.total_cmp(b));
                (
                    percentile(&values, LOW_PERCENTILE),
                    percentile(&values, HIGH_PERCENTILE),
                )
            })
            .collect()
    }

    /// Cards with tallies spread over many bins, so a changed draw changes
    /// a number. One card cannot catch that: every resample of one card is
    /// the same card, and its interval is degenerate whatever the draws.
    fn spread_cards() -> Vec<Vec<BinTally>> {
        (0..40)
            .map(|card| {
                // every card touches the same few bins, with its own
                // answers, so resampling the cards moves each bin's share
                (0..5)
                    .map(|step| BinTally {
                        bin: step * 3,
                        remembered: f64::from((card + step) % 3 != 0)
                            * f64::from(1 + card as u32 % 2),
                        count: f64::from(1 + card as u32 % 2),
                    })
                    .collect()
            })
            .collect()
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn the_parallel_bootstrap_draws_what_one_thread_drew() -> Result<()> {
        let cards = spread_cards();
        let expected = one_thread_intervals(&cards);
        let actual = bootstrap_intervals(&cards);

        assert_eq!(actual.len(), expected.len());
        let mut moving = 0;
        for (bin, (&(low, high), &(want_low, want_high))) in
            actual.iter().zip(&expected).enumerate()
        {
            assert_eq!(
                (low, high),
                (want_low, want_high),
                "bin {bin} moved when the rounds went parallel"
            );
            if want_low != want_high {
                moving += 1;
            }
        }
        // the fixture must actually exercise the draws, or the test could
        // not fail: several bins need an interval with width
        assert!(moving >= 5, "only {moving} bins have a real interval");
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn the_same_reviews_always_give_the_same_interval() -> Result<()> {
        // the cards are resampled by position, so their order must not
        // depend on a hash map's iteration order
        let mut col = Collection::new();
        let mut day = -300;
        for index in 0..12 {
            let card = add_card(&mut col);
            for step in 0..8 {
                let review = rate(
                    &mut col,
                    card,
                    day,
                    if (index + step) % 3 == 0 { 1 } else { 3 },
                );
                store_rwkv(
                    &col,
                    review,
                    (0.30 + ((index * 8 + step) % 13) as f32 * 0.05).min(0.99),
                );
                day += 1;
            }
        }

        let bins = |col: &mut Collection| -> Result<Vec<CalibrationBin>> {
            let response = col.review_predictions("", 0)?;
            Ok(series_of(&response, SchedulingAlgorithmProto::RwkvInstant)
                .bins
                .clone())
        };
        let first = bins(&mut col)?;
        let second = bins(&mut col)?;
        assert!(first.len() >= 8, "the fixture must fill several bins");
        for (a, b) in first.iter().zip(&second) {
            assert_eq!((a.index, a.low, a.high), (b.index, b.low, b.high));
        }
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn the_period_selects_the_ratings() -> Result<()> {
        let mut col = Collection::new();
        let card = add_card(&mut col);
        let old = rate(&mut col, card, -400, 3);
        let recent = rate(&mut col, card, -10, 3);
        for review in [old, recent] {
            store_fsrs(
                &col,
                review,
                0.5,
                FsrsReviewRetrievabilitySampleRole::ValidationFold,
            );
            store_rwkv(&col, review, 0.6);
        }

        assert_eq!(col.scored_ratings("", 0)?.revlog_ids, vec![old.0, recent.0]);
        assert_eq!(col.scored_ratings("", 365)?.revlog_ids, vec![recent.0]);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn newer_ratings_than_the_stored_predictions_are_reported() -> Result<()> {
        let mut col = Collection::new();
        let card = add_card(&mut col);
        let scored = rate(&mut col, card, -20, 3);
        rate(&mut col, card, -2, 3);
        rate(&mut col, card, -1, 3);
        store_fsrs(
            &col,
            scored,
            0.5,
            FsrsReviewRetrievabilitySampleRole::ValidationFold,
        );
        store_rwkv(&col, scored, 0.6);

        let rows = col.scored_ratings("", 0)?;
        assert_eq!(rows.revlog_ids, vec![scored.0]);
        assert_eq!(rows.newest_scored_secs, scored.as_secs().0);
        // the two ratings after it are named, never silently dropped
        assert_eq!(rows.newer_reviews, 2);
        assert_eq!(rows.unscored, 2);
        Ok(())
    }
}

/// Bins per unit of difference in the UM+ comparison, as in
/// `UM_plus_plot.py`: 20 each side of "the algorithms agree", 41 in all.
const UM_BINS_PER_UNIT: i32 = 20;
const UM_BIN_COUNT: usize = (2 * UM_BINS_PER_UNIT + 1) as usize;

/// Bins of the calibration graph, over the predicted probability.
const BIN_COUNT: usize = 20;
/// Resamples of the cards for a bin's confidence interval.
const BOOTSTRAP_ROUNDS: usize = 500;
const LOW_PERCENTILE: f64 = 0.025;
const HIGH_PERCENTILE: f64 = 0.975;
/// A fixed seed which, with the fixed card order above, makes the same
/// reviews always give the same interval.
const BOOTSTRAP_SEED: u64 = 0x5f37_59df;

/// The bin of a predicted probability: the bins are log-spaced, so the
/// crowded high probabilities get more of them than the low ones (the same
/// binning as the Search Stats Extended fork).
fn bin_of(prediction: f32) -> usize {
    let scaled = ((BIN_COUNT + 1) as f64).ln() * prediction.clamp(0.0, 1.0) as f64;
    (scaled.exp().floor() as usize)
        .saturating_sub(1)
        .min(BIN_COUNT - 1)
}

/// One card's answers in ONE bin, for the bootstrap. A card is reviewed a
/// handful of times, so it touches a handful of bins; keeping only those
/// makes a resampling round read a few entries per card instead of twenty.
#[derive(Clone, Copy)]
struct BinTally {
    bin: usize,
    remembered: f64,
    count: f64,
}

/// One algorithm's finished series (spec ui.stats-model-metrics): its ROC
/// curve with the area under it, its calibration bins, and the two
/// averages the panel's tiles show.
///
/// The lists hold one entry per rating ANY algorithm scored, and a rating
/// this algorithm has no row for is NaN. Those are dropped here, so the
/// series covers exactly the ratings this algorithm predicted and never
/// borrows another algorithm's value. An algorithm with no role has no
/// usable row at all, which is not an error.
fn series(
    algorithm: SchedulingAlgorithmProto,
    predictions: &[f32],
    remembered: &[bool],
    role: &str,
    bins: Vec<CalibrationBin>,
) -> Series {
    let absent = || Series {
        algorithm: algorithm as i32,
        unavailable: Unavailable::NoReviews as i32,
        ..Default::default()
    };
    let mut own_predictions: Vec<f64> = vec![];
    let mut own_remembered: Vec<bool> = vec![];
    for (prediction, answer) in predictions.iter().zip(remembered) {
        if prediction.is_finite() {
            own_predictions.push(*prediction as f64);
            own_remembered.push(*answer);
        }
    }
    // an algorithm with rows from no single role (RWKV's newest row of
    // each rating) has no role to name, but it has predictions
    if own_predictions.is_empty() {
        return absent();
    }
    let (points, auc) = roc_curve(&own_predictions, &own_remembered, CURVE_POINTS);
    if points.is_empty() {
        return absent();
    }
    let reviews = own_predictions.len();
    Series {
        algorithm: algorithm as i32,
        unavailable: Unavailable::Available as i32,
        reviews: reviews as u32,
        sample_role: role.to_string(),
        false_positive_rate: points.iter().map(|point| point.0 as f32).collect(),
        true_positive_rate: points.iter().map(|point| point.1 as f32).collect(),
        auc,
        bins,
        average_predicted: own_predictions.iter().sum::<f64>() / reviews as f64,
        actual_recall: own_remembered.iter().filter(|answer| **answer).count() as f64
            / reviews as f64,
        recorded_from_secs: 0,
        earlier_reviews: 0,
    }
}

/// The calibration bins of one model over the scored ratings: each bin's
/// mean prediction, its share of remembered answers, and the 2.5 and 97.5
/// percentiles of that share over 500 resamples of the cards (spec
/// ui.stats-model-metrics). Resampling cards rather than reviews keeps a
/// card's own reviews together, because they are not independent.
fn calibration_bins(
    predictions: &[f32],
    remembered: &[bool],
    card_ids: &[i64],
) -> Vec<anki_proto::stats::CalibrationBin> {
    let mut sums = vec![(0.0f64, 0.0f64); BIN_COUNT];
    let mut predicted = [0.0f64; BIN_COUNT];
    let mut by_card: HashMap<i64, Vec<BinTally>> = HashMap::new();
    for ((&prediction, &remembered), &card_id) in predictions.iter().zip(remembered).zip(card_ids) {
        if !prediction.is_finite() || !(0.0..=1.0).contains(&prediction) {
            continue;
        }
        let bin = bin_of(prediction);
        let answer = f64::from(remembered);
        predicted[bin] += prediction as f64;
        sums[bin].0 += answer;
        sums[bin].1 += 1.0;
        let card = by_card.entry(card_id).or_default();
        match card.iter_mut().find(|tally| tally.bin == bin) {
            Some(tally) => {
                tally.remembered += answer;
                tally.count += 1.0;
            }
            None => card.push(BinTally {
                bin,
                remembered: answer,
                count: 1.0,
            }),
        }
    }

    // The cards are resampled in a fixed order. A HashMap hands out its
    // values in an order that differs between runs, and the bootstrap draws
    // cards by position, so without this the same reviews gave a different
    // interval every time the page was opened (spec ui.stats-model-metrics).
    let mut by_card: Vec<(i64, Vec<BinTally>)> = by_card.into_iter().collect();
    by_card.sort_unstable_by_key(|(card_id, _)| *card_id);
    let cards: Vec<Vec<BinTally>> = by_card.into_iter().map(|(_, tallies)| tallies).collect();
    let intervals = bootstrap_intervals(&cards);
    (0..BIN_COUNT)
        .filter(|&bin| sums[bin].1 > 0.0)
        .map(|bin| anki_proto::stats::CalibrationBin {
            index: bin as u32,
            sum_predicted: predicted[bin],
            sum_remembered: sums[bin].0,
            count: sums[bin].1 as u32,
            low: intervals[bin].0,
            high: intervals[bin].1,
        })
        .collect()
}

/// For each bin, the 2.5 and 97.5 percentiles of its share of remembered
/// answers over resamples of the cards; (0, 0) for a bin no resample fills.
fn bootstrap_intervals(cards: &[Vec<BinTally>]) -> Vec<(f64, f64)> {
    if cards.is_empty() {
        return vec![(0.0, 0.0); BIN_COUNT];
    }
    // The draws are one stream, exactly as they were when the rounds ran one
    // after another, so every round sees the cards it saw before and the
    // intervals do not move. Only the state each round STARTS from is walked
    // here; the rounds themselves then run in parallel from it, so the result
    // does not depend on the machine or on how the work is shared out.
    // xorshift64*, so the interval is the same on every machine
    fn advance(state: &mut u64) -> u64 {
        *state ^= *state >> 12;
        *state ^= *state << 25;
        *state ^= *state >> 27;
        state.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    let mut state = BOOTSTRAP_SEED;
    let mut round_seeds = Vec::with_capacity(BOOTSTRAP_ROUNDS);
    for _ in 0..BOOTSTRAP_ROUNDS {
        round_seeds.push(state);
        for _ in 0..cards.len() {
            advance(&mut state);
        }
    }

    let rounds: Vec<Vec<Option<f64>>> = round_seeds
        .into_par_iter()
        .map(|seed| {
            let mut state = seed;
            let mut remembered = [0.0f64; BIN_COUNT];
            let mut counts = [0.0f64; BIN_COUNT];
            for _ in 0..cards.len() {
                let draw = advance(&mut state);
                // a card holds only the bins it has answers in
                for tally in &cards[(draw % cards.len() as u64) as usize] {
                    remembered[tally.bin] += tally.remembered;
                    counts[tally.bin] += tally.count;
                }
            }
            (0..BIN_COUNT)
                .map(|bin| (counts[bin] > 0.0).then(|| remembered[bin] / counts[bin]))
                .collect()
        })
        .collect();

    let mut shares: Vec<Vec<f64>> = vec![vec![]; BIN_COUNT];
    for round in rounds {
        for (bin, share) in round.into_iter().enumerate() {
            if let Some(share) = share {
                shares[bin].push(share);
            }
        }
    }
    shares
        .into_iter()
        .map(|mut values| {
            if values.is_empty() {
                return (0.0, 0.0);
            }
            values.sort_by(|a, b| a.total_cmp(b));
            (
                percentile(&values, LOW_PERCENTILE),
                percentile(&values, HIGH_PERCENTILE),
            )
        })
        .collect()
}

/// The percentile of sorted values, interpolated between the two nearest.
fn percentile(values: &[f64], percentile: f64) -> f64 {
    let position = (values.len() - 1) as f64 * percentile;
    let lower = position.floor() as usize;
    let upper = position.ceil() as usize;
    let weight = position - lower as f64;
    values[lower] * (1.0 - weight) + values[upper] * weight
}

/// The bin of a difference between two predictions: rounded to the nearest
/// twentieth, with agreement in the middle (`UM_plus_plot.py`'s binning).
fn um_bin_of(difference: f64) -> usize {
    ((difference * UM_BINS_PER_UNIT as f64).round() as i32 + UM_BINS_PER_UNIT)
        .clamp(0, UM_BIN_COUNT as i32 - 1) as usize
}

/// One pair of algorithms as the UM+ comparison sees them (spec
/// ui.stats-model-metrics): the ratings are grouped by how far the two
/// predictions differ, and each algorithm is scored by how far its mean
/// prediction sits from the mean answer inside those groups. A rating only
/// one of them predicted is in no group.
fn um_plus_pair(
    algorithm_a: SchedulingAlgorithmProto,
    predictions_a: &[f32],
    algorithm_b: SchedulingAlgorithmProto,
    predictions_b: &[f32],
    remembered: &[bool],
) -> Option<anki_proto::stats::UmPlusPair> {
    let mut difference = vec![0.0f64; UM_BIN_COUNT];
    let mut error_a = vec![0.0f64; UM_BIN_COUNT];
    let mut error_b = vec![0.0f64; UM_BIN_COUNT];
    let mut counts = [0u32; UM_BIN_COUNT];
    let mut reviews = 0u32;
    for ((&a, &b), &remembered) in predictions_a.iter().zip(predictions_b).zip(remembered) {
        if !a.is_finite() || !b.is_finite() {
            continue;
        }
        let (a, b, answer) = (a as f64, b as f64, f64::from(remembered));
        let bin = um_bin_of(a - b);
        difference[bin] += a - b;
        error_a[bin] += a - answer;
        error_b[bin] += b - answer;
        counts[bin] += 1;
        reviews += 1;
    }
    if reviews == 0 {
        return None;
    }

    let bins: Vec<anki_proto::stats::UmPlusBin> = (0..UM_BIN_COUNT)
        .filter(|&bin| counts[bin] > 0)
        .map(|bin| anki_proto::stats::UmPlusBin {
            index: bin as u32,
            sum_difference: difference[bin],
            sum_error_a: error_a[bin],
            sum_error_b: error_b[bin],
            count: counts[bin],
        })
        .collect();
    Some(anki_proto::stats::UmPlusPair {
        algorithm_a: algorithm_a as i32,
        algorithm_b: algorithm_b as i32,
        um_a: weighted_rms(&bins, |bin| bin.sum_error_a / bin.count as f64),
        um_b: weighted_rms(&bins, |bin| bin.sum_error_b / bin.count as f64),
        slope_a: weighted_slope(&bins, |bin| bin.sum_error_a / bin.count as f64),
        slope_b: weighted_slope(&bins, |bin| bin.sum_error_b / bin.count as f64),
        reviews,
        bins,
    })
}

/// The root mean square of the bins' values, weighted by their counts: the
/// Universal Metric Plus itself.
fn weighted_rms(
    bins: &[anki_proto::stats::UmPlusBin],
    value: impl Fn(&anki_proto::stats::UmPlusBin) -> f64,
) -> f64 {
    let total: f64 = bins.iter().map(|bin| bin.count as f64).sum();
    if total == 0.0 {
        return 0.0;
    }
    (bins
        .iter()
        .map(|bin| bin.count as f64 * value(bin).powi(2))
        .sum::<f64>()
        / total)
        .sqrt()
}

/// The slope of a value against the difference over the bins, weighted by
/// their counts; 0 when the differences are all alike.
fn weighted_slope(
    bins: &[anki_proto::stats::UmPlusBin],
    value: impl Fn(&anki_proto::stats::UmPlusBin) -> f64,
) -> f64 {
    let total: f64 = bins.iter().map(|bin| bin.count as f64).sum();
    if total == 0.0 {
        return 0.0;
    }
    let mean_x: f64 = bins
        .iter()
        .map(|bin| bin.count as f64 * (bin.sum_difference / bin.count as f64))
        .sum::<f64>()
        / total;
    let mean_y: f64 = bins
        .iter()
        .map(|bin| bin.count as f64 * value(bin))
        .sum::<f64>()
        / total;
    let covariance: f64 = bins
        .iter()
        .map(|bin| {
            bin.count as f64
                * (bin.sum_difference / bin.count as f64 - mean_x)
                * (value(bin) - mean_y)
        })
        .sum();
    let variance: f64 = bins
        .iter()
        .map(|bin| bin.count as f64 * (bin.sum_difference / bin.count as f64 - mean_x).powi(2))
        .sum();
    if variance > 1e-12 {
        covariance / variance
    } else {
        0.0
    }
}

/// The four reads the graphs are made of: the search's ratings, and each
/// algorithm's predictions of them.
///
/// The two big prediction reads ask the retrievability-cache sidecar alone
/// and need no temporary table, so each takes a read-only connection of its
/// own and runs beside the review-log read, which asks for the search's
/// temporary table and so has to stay on the collection's connection. The
/// four reads are of committed rows and write nothing, so which connection
/// reads what changes no value and no order; only the waiting is shared
/// out. With no second connection to be had (a cache in memory, an open
/// transaction) all four run on the collection's connection, one after
/// another, and give the same four lists.
fn read_ratings_and_predictions(
    storage: &SqliteStorage,
    cutoff: TimestampMillis,
    many_cards: bool,
) -> Result<(
    Vec<SearchedRating>,
    CachedPredictions,
    CachedPredictions,
    CachedPredictions,
)> {
    let readers = storage
        .open_retrievability_cache_reader()
        .zip(storage.open_retrievability_cache_reader());
    let Some((fsrs_reader, rwkv_reader)) = readers else {
        let ratings = storage.searched_ratings_that_affect_scheduling(cutoff, many_cards)?;
        return Ok((
            ratings,
            read_predictions(storage, &FSRS7, cutoff)?,
            read_predictions(storage, &RWKV_INSTANT, cutoff)?,
            read_predictions(storage, &RWKV_CURVE, cutoff)?,
        ));
    };
    std::thread::scope(|scope| {
        let fsrs = scope.spawn(move || read_predictions(&fsrs_reader, &FSRS7, cutoff));
        let rwkv = scope.spawn(move || read_predictions(&rwkv_reader, &RWKV_INSTANT, cutoff));
        // RWKV-Curve's rows are in the table every algorithm shares, whose
        // reader creates it when it is missing; a write belongs on the
        // collection's connection, so this read stays here
        let ratings = storage.searched_ratings_that_affect_scheduling(cutoff, many_cards);
        let rwkv_curve = read_predictions(storage, &RWKV_CURVE, cutoff);
        let joined = |thread: std::thread::ScopedJoinHandle<'_, Result<CachedPredictions>>| {
            thread.join().unwrap_or_else(|payload| {
                std::panic::resume_unwind(payload);
            })
        };
        let (fsrs, rwkv) = (joined(fsrs), joined(rwkv));
        Ok((ratings?, fsrs?, rwkv?, rwkv_curve?))
    })
}

/// One algorithm's rows, from a single sample role: the role its own
/// contract chooses out of the roles it has rows for. Roles are never
/// mixed, and no other algorithm's rows are reachable from here.
fn read_predictions(
    storage: &SqliteStorage,
    algorithm: &PredictsRecall,
    after: TimestampMillis,
) -> Result<CachedPredictions> {
    // an algorithm that takes the first role it has rows for only asks
    // whether each role has one, which an index answers at once; counting
    // every role's reviews reads a row per review of the collection
    let has_any_row = |role: &str| match algorithm.store {
        PredictionStore::Generic => storage.review_prediction_role_exists(algorithm.id(), role),
        PredictionStore::Legacy(table) => storage.cached_review_prediction_role_exists(table, role),
    };
    let chosen = match algorithm.role_choice {
        RoleChoice::FirstHonest => {
            let mut first = None;
            for role in algorithm.honest_roles {
                if has_any_row(role)? {
                    first = Some(role);
                    break;
                }
            }
            first
        }
        RoleChoice::NewestRow => {
            // no single role: the page names none
            let rows = match algorithm.store {
                PredictionStore::Generic => storage.review_predictions_newest_of(
                    algorithm.id(),
                    algorithm.honest_roles,
                    after,
                )?,
                PredictionStore::Legacy(table) => storage.cached_review_predictions_newest_of(
                    table,
                    algorithm.honest_roles,
                    after,
                )?,
            };
            return Ok(CachedPredictions {
                role: String::new(),
                by_review: rows,
            });
        }
    };
    let Some(role) = chosen else {
        return Ok(CachedPredictions::none());
    };
    let rows = match algorithm.store {
        PredictionStore::Generic => storage.review_predictions_of(algorithm.id(), role, after)?,
        PredictionStore::Legacy(table) => storage.cached_review_predictions(table, role, after)?,
    };
    Ok(CachedPredictions {
        role: (*role).to_string(),
        by_review: rows,
    })
}
