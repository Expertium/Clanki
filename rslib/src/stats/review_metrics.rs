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
use anki_proto::stats::ReviewPredictionsResponse;
use rayon::prelude::*;

use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::search::SortMode;
use crate::stats::algorithms::PredictionStore;
use crate::stats::algorithms::PredictsRecall;
use crate::stats::algorithms::RoleChoice;
use crate::stats::algorithms::FSRS7;
use crate::stats::algorithms::RWKV_CURVE;
use crate::stats::algorithms::RWKV_INSTANT;

/// One model's cached predictions for the searched ratings, and the role
/// they came from.
struct CachedPredictions {
    role: String,
    by_review: HashMap<RevlogId, f32>,
}

impl CachedPredictions {
    fn none() -> Self {
        Self {
            role: String::new(),
            by_review: HashMap::new(),
        }
    }
}

impl Collection {
    /// Reads both models' cached predictions of the search's ratings.
    pub(crate) fn review_predictions(
        &mut self,
        search: &str,
        days: u32,
    ) -> Result<ReviewPredictionsResponse> {
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
        let ratings: Vec<RevlogEntry> = storage
            .get_revlog_entries_for_searched_cards()?
            .into_iter()
            .filter(|entry| entry.has_rating_and_affects_scheduling() && entry.id.0 > cutoff.0)
            .collect();
        // each algorithm names itself; no read can reach another
        // algorithm's rows without saying whose they are
        let fsrs = read_predictions(storage, &FSRS7, cutoff)?;
        let rwkv = read_predictions(storage, &RWKV_INSTANT, cutoff)?;
        let rwkv_curve = read_predictions(storage, &RWKV_CURVE, cutoff)?;

        let mut response = ReviewPredictionsResponse {
            fsrs_role: fsrs.role.clone(),
            rwkv_role: rwkv.role.clone(),
            rwkv_curve_role: rwkv_curve.role.clone(),
            ..Default::default()
        };
        // Each model is scored on every rating it has an honest row for, so
        // a model's curve never disappears because the other model has no
        // rows. The ratings both models scored are counted separately, and
        // they are the ones the graph compares the two models on (spec
        // ui.stats-model-metrics).
        let mut ratings = ratings;
        ratings.sort_unstable_by_key(|entry| entry.id);
        for entry in &ratings {
            let fsrs_value = fsrs.by_review.get(&entry.id);
            let rwkv_value = rwkv.by_review.get(&entry.id);
            let curve_value = rwkv_curve.by_review.get(&entry.id);
            if fsrs_value.is_some() || rwkv_value.is_some() || curve_value.is_some() {
                response.revlog_ids.push(entry.id.0);
                response.card_ids.push(entry.cid.0);
                response.remembered.push(entry.button_chosen > 1);
                // a model without a row for this rating has no number
                // here, and its series simply skips the rating rather
                // than borrowing the other model's value
                response
                    .fsrs_predictions
                    .push(fsrs_value.copied().unwrap_or(f32::NAN));
                response
                    .rwkv_predictions
                    .push(rwkv_value.copied().unwrap_or(f32::NAN));
                response
                    .rwkv_curve_predictions
                    .push(curve_value.copied().unwrap_or(f32::NAN));
            }
            match (fsrs_value, rwkv_value) {
                (Some(_), Some(_)) => response.shared += 1,
                (Some(_), None) => response.fsrs_only += 1,
                (None, Some(_)) => response.rwkv_only += 1,
                (None, None) => response.unscored += 1,
            }
        }
        // a comparison needs both models to have scored something here
        response.shared_ratings =
            response.shared + response.fsrs_only > 0 && response.shared + response.rwkv_only > 0;

        // how fresh the predictions are: the newest rating either model has
        // scored, and the ratings of the search after it
        let newest = ratings
            .iter()
            .filter(|entry| {
                fsrs.by_review.contains_key(&entry.id)
                    || rwkv.by_review.contains_key(&entry.id)
                    || rwkv_curve.by_review.contains_key(&entry.id)
            })
            .map(|entry| entry.id)
            .max();
        if let Some(newest) = newest {
            response.newest_scored_secs = newest.as_secs().0;
            response.newer_reviews = ratings
                .iter()
                .filter(|entry| entry.id > newest)
                .count()
                .try_into()
                .unwrap_or(u32::MAX);
        } else {
            response.newer_reviews = ratings.len().try_into().unwrap_or(u32::MAX);
        }

        response.fsrs_bins = calibration_bins(
            &response.fsrs_predictions,
            &response.remembered,
            &response.card_ids,
        );
        response.rwkv_bins = calibration_bins(
            &response.rwkv_predictions,
            &response.remembered,
            &response.card_ids,
        );
        // How far back the curve recording reaches. A collection that has
        // been replayed since the recording shipped has rows for its whole
        // history; one that has not has rows only from the day it started,
        // and the graph must say so rather than show three days of data
        // looking like three years.
        let oldest_curve = ratings
            .iter()
            .find(|entry| rwkv_curve.by_review.contains_key(&entry.id))
            .map(|entry| entry.id);
        if let Some(oldest) = oldest_curve {
            response.rwkv_curve_oldest_secs = oldest.as_secs().0;
            response.rwkv_curve_earlier_reviews = ratings
                .iter()
                .filter(|entry| entry.id < oldest)
                .count()
                .try_into()
                .unwrap_or(u32::MAX);
        }

        response.rwkv_curve_bins = calibration_bins(
            &response.rwkv_curve_predictions,
            &response.remembered,
            &response.card_ids,
        );
        // one entry per pair of algorithms that share ratings; three
        // algorithms make three pairs, and a pair with no shared rating is
        // simply absent
        let pairs = [
            (
                SchedulingAlgorithmProto::Fsrs7,
                &response.fsrs_predictions,
                SchedulingAlgorithmProto::RwkvCurve,
                &response.rwkv_curve_predictions,
            ),
            (
                SchedulingAlgorithmProto::Fsrs7,
                &response.fsrs_predictions,
                SchedulingAlgorithmProto::RwkvInstant,
                &response.rwkv_predictions,
            ),
            (
                SchedulingAlgorithmProto::RwkvCurve,
                &response.rwkv_curve_predictions,
                SchedulingAlgorithmProto::RwkvInstant,
                &response.rwkv_predictions,
            ),
        ];
        let mut um_plus = vec![];
        for (algorithm_a, predictions_a, algorithm_b, predictions_b) in pairs {
            if let Some(pair) = um_plus_pair(
                algorithm_a,
                predictions_a,
                algorithm_b,
                predictions_b,
                &response.remembered,
            ) {
                um_plus.push(pair);
            }
        }
        response.um_plus = um_plus;
        Ok(response)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::revlog::RevlogReviewKind;
    use crate::storage::FsrsReviewRetrievabilityCacheRow;
    use crate::storage::FsrsReviewRetrievabilitySampleRole;
    use crate::storage::RwkvReviewRetrievabilityCacheRow;
    use crate::storage::RwkvReviewRetrievabilitySampleRole;
    use crate::tests::NoteAdder;

    fn add_card(col: &mut Collection) -> CardId {
        let note = NoteAdder::basic(col).add(col);
        let mut card = col.storage.all_cards_of_note(note.id).unwrap().remove(0);
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        col.storage.update_card(&card).unwrap();
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

        let response = col.review_predictions("", 0)?;
        // RWKV can score both ratings, so both are listed
        assert_eq!(response.revlog_ids, vec![fitted.0, honest.0]);
        // FSRS-7's final-fit row is never used, so that rating has no FSRS
        // number at all; it is not filled in from RWKV's
        assert!(response.fsrs_predictions[0].is_nan());
        assert_eq!(response.fsrs_predictions[1], 0.4);
        assert_eq!(response.rwkv_predictions, vec![0.8, 0.3]);
        assert_eq!(response.remembered, vec![true, false]);
        assert_eq!(response.fsrs_role, "validation_fold");
        // RWKV's weights saw no review of this collection, so any role counts
        assert_eq!(response.rwkv_role, "final_fit");
        // the rating only RWKV could score is counted as its own
        assert_eq!(response.rwkv_only, 1);
        assert_eq!(response.fsrs_only, 0);
        assert_eq!(response.shared, 1);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn the_shared_ratings_are_counted_not_enforced() -> Result<()> {
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

        let response = col.review_predictions("", 0)?;
        // every rating either model scored is kept, not only the shared one
        assert_eq!(
            response.revlog_ids,
            vec![shared.0, fsrs_alone.0, rwkv_alone.0]
        );
        assert_eq!(response.shared, 1);
        assert_eq!(response.fsrs_only, 1);
        assert_eq!(response.rwkv_only, 1);
        assert_eq!(response.unscored, 1);
        assert!(response.shared_ratings);
        assert_eq!(response.fsrs_role, "post_optimization");
        // the rating a model has no row for is NaN, never the other's value
        assert!(response.fsrs_predictions[2].is_nan());
        assert!(response.rwkv_predictions[1].is_nan());
        let _ = neither;
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn each_algorithm_keeps_every_rating_it_can_score() -> Result<()> {
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

        let response = col.review_predictions("", 0)?;
        assert_eq!(response.revlog_ids.len(), 10);
        assert_eq!(
            response
                .rwkv_predictions
                .iter()
                .filter(|value| value.is_finite())
                .count(),
            10
        );
        assert_eq!(
            response
                .fsrs_predictions
                .iter()
                .filter(|value| value.is_finite())
                .count(),
            1
        );
        assert_eq!(response.shared, 1);
        assert_eq!(response.rwkv_only, 9);
        assert!(response.shared_ratings);
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

        let response = col.review_predictions("", 0)?;
        assert_eq!(response.revlog_ids, vec![first.0, second.0]);
        assert_eq!(response.rwkv_predictions, vec![0.9, 0.4]);
        assert!(!response.shared_ratings);
        assert!(response.fsrs_role.is_empty());
        // FSRS-7 has no number for these ratings, and is not filled in
        assert!(response.fsrs_predictions.iter().all(|value| value.is_nan()));
        assert_eq!(response.rwkv_only, 2);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn rwkv_takes_the_role_with_the_most_rows() -> Result<()> {
        let mut col = Collection::new();
        let card = add_card(&mut col);
        let reviews: Vec<RevlogId> = (0..5)
            .map(|index| rate(&mut col, card, -30 + index, 3))
            .collect();
        // one post-optimization row, four final-fit rows: RWKV's roles
        // carry no honesty order, so the bigger role wins
        store_rwkv_role(
            &col,
            reviews[0],
            0.5,
            RwkvReviewRetrievabilitySampleRole::PostOptimization,
        );
        for review in &reviews[1..] {
            store_rwkv_role(
                &col,
                *review,
                0.8,
                RwkvReviewRetrievabilitySampleRole::FinalFit,
            );
        }

        let response = col.review_predictions("", 0)?;
        assert_eq!(response.rwkv_role, "final_fit");
        assert_eq!(response.revlog_ids.len(), 4);
        // the post-optimization row is not mixed in, and its rating is
        // therefore scored by nothing
        assert_eq!(response.unscored, 1);
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
        // every rating has the same prediction, so they share one bin
        assert_eq!(response.rwkv_bins.len(), 1);
        let bin = &response.rwkv_bins[0];
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

        let first = col.review_predictions("", 0)?.rwkv_bins;
        let second = col.review_predictions("", 0)?.rwkv_bins;
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

        assert_eq!(
            col.review_predictions("", 0)?.revlog_ids,
            vec![old.0, recent.0]
        );
        assert_eq!(col.review_predictions("", 365)?.revlog_ids, vec![recent.0]);
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

        let response = col.review_predictions("", 0)?;
        assert_eq!(response.revlog_ids, vec![scored.0]);
        assert_eq!(response.newest_scored_secs, scored.as_secs().0);
        // the two ratings after it are named, never silently dropped
        assert_eq!(response.newer_reviews, 2);
        assert_eq!(response.unscored, 2);
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

/// One algorithm's rows, from a single sample role: the role its own
/// contract chooses out of the roles it has rows for. Roles are never
/// mixed, and no other algorithm's rows are reachable from here.
fn read_predictions(
    storage: &crate::storage::SqliteStorage,
    algorithm: &PredictsRecall,
    after: TimestampMillis,
) -> Result<CachedPredictions> {
    let stored = match algorithm.store {
        PredictionStore::Generic => storage.review_prediction_roles(algorithm.id())?,
        PredictionStore::Legacy(table) => storage.cached_review_prediction_roles(table)?,
    };
    let count_of = |role: &str| {
        stored
            .iter()
            .find(|(stored, _)| stored == role)
            .map_or(0, |(_, count)| *count)
    };
    let chosen = match algorithm.role_choice {
        RoleChoice::MostRows => algorithm
            .honest_roles
            .iter()
            .filter(|role| count_of(role) > 0)
            .max_by_key(|role| count_of(role)),
        RoleChoice::FirstHonest => algorithm
            .honest_roles
            .iter()
            .find(|role| count_of(role) > 0),
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
        by_review: rows.into_iter().collect(),
    })
}
