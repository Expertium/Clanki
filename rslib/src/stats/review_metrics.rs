// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The Stats page's model-quality graphs (spec ui.stats-model-metrics):
//! each algorithm's predicted probability of recall before every rating of
//! the search, with the answer itself.
//!
//! The predictions are not computed here. Both models write them per review
//! while they run - FSRS-7 when its parameters are optimized, RWKV when its
//! state cache is built - and this reads those rows. A row counts only when
//! nothing that produced it was fitted on that very review, and both models
//! are scored on the same reviews, so their numbers can be compared.

use std::collections::HashMap;

use anki_proto::deck_config::deck_configs_for_update::SchedulingAlgorithm as SchedulingAlgorithmProto;
use anki_proto::stats::ReviewPredictionsResponse;

use rayon::prelude::*;

use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::search::SortMode;
use crate::storage::FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE;
use crate::storage::RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE;

/// FSRS-7's parameters are fitted on this collection, so only the rows of a
/// model that had not seen the review count: a validation fold, or a run
/// after the optimization that produced the parameters. `final_fit` rows
/// are never used, not even labelled.
const FSRS_ROLES: &[&str] = &["validation_fold", "post_optimization"];
/// RWKV's weights are frozen and were trained on other people's reviews,
/// and a replayed prediction is built from the reviews before it, so its
/// raw output cannot have seen the review whatever role the row carries.
/// With no honesty order to keep, it takes the role that covers the most
/// reviews, so the comparison rests on as many ratings as possible.
const RWKV_ROLES: &[&str] = &["test_fold", "post_optimization", "final_fit"];

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
        let fsrs = read_predictions(
            storage,
            FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE,
            FSRS_ROLES,
            cutoff,
            false,
        )?;
        let rwkv = read_predictions(
            storage,
            RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE,
            RWKV_ROLES,
            cutoff,
            true,
        )?;

        let mut response = ReviewPredictionsResponse {
            fsrs_role: fsrs.role.clone(),
            rwkv_role: rwkv.role.clone(),
            ..Default::default()
        };
        // Two models with rows are compared, so they are scored on the
        // ratings they share; one model alone is not a comparison, so it
        // keeps all of its own ratings (spec ui.stats-model-metrics).
        let shared = !fsrs.by_review.is_empty() && !rwkv.by_review.is_empty();
        response.shared_ratings = shared;
        let mut ratings = ratings;
        ratings.sort_unstable_by_key(|entry| entry.id);
        for entry in &ratings {
            let fsrs_value = fsrs.by_review.get(&entry.id);
            let rwkv_value = rwkv.by_review.get(&entry.id);
            let scored = if shared {
                fsrs_value.is_some() && rwkv_value.is_some()
            } else {
                fsrs_value.is_some() || rwkv_value.is_some()
            };
            if scored {
                response.revlog_ids.push(entry.id.0);
                response.card_ids.push(entry.cid.0);
                response.remembered.push(entry.button_chosen > 1);
                // a model without a row for this rating has no number
                // here, and its series is absent rather than filled in
                response
                    .fsrs_predictions
                    .push(fsrs_value.copied().unwrap_or(f32::NAN));
                response
                    .rwkv_predictions
                    .push(rwkv_value.copied().unwrap_or(f32::NAN));
            }
            match (fsrs_value, rwkv_value) {
                (Some(_), Some(_)) => {}
                (Some(_), None) => response.fsrs_only += 1,
                (None, Some(_)) => response.rwkv_only += 1,
                (None, None) => response.unscored += 1,
            }
        }

        // how fresh the predictions are: the newest rating either model has
        // scored, and the ratings of the search after it
        let newest = ratings
            .iter()
            .filter(|entry| {
                fsrs.by_review.contains_key(&entry.id) || rwkv.by_review.contains_key(&entry.id)
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
        // one entry per pair of algorithms that share ratings; today only
        // FSRS-7 and RWKV-Instant have stored predictions
        if let Some(pair) = um_plus_pair(
            SchedulingAlgorithmProto::Fsrs7,
            &response.fsrs_predictions,
            SchedulingAlgorithmProto::RwkvInstant,
            &response.rwkv_predictions,
            &response.remembered,
        ) {
            response.um_plus.push(pair);
        }
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
        assert_eq!(response.revlog_ids, vec![honest.0]);
        assert_eq!(response.fsrs_predictions, vec![0.4]);
        assert_eq!(response.rwkv_predictions, vec![0.3]);
        assert_eq!(response.remembered, vec![false]);
        assert_eq!(response.fsrs_role, "validation_fold");
        // RWKV's weights saw no review of this collection, so any role counts
        assert_eq!(response.rwkv_role, "final_fit");
        // the rating only RWKV could score is left out, and counted
        assert_eq!(response.rwkv_only, 1);
        assert_eq!(response.fsrs_only, 0);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn both_algorithms_are_scored_on_the_same_ratings() -> Result<()> {
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
        assert_eq!(response.revlog_ids, vec![shared.0]);
        assert_eq!(response.fsrs_only, 1);
        assert_eq!(response.rwkv_only, 1);
        assert_eq!(response.unscored, 1);
        assert_eq!(response.fsrs_role, "post_optimization");
        let _ = neither;
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
/// A fixed seed, so the same reviews always give the same interval.
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

/// One card's answers in one bin, for the bootstrap. A card is reviewed a
/// handful of times, so it touches a handful of bins: keeping only those
/// makes a resampling round read a few entries per card instead of twenty.
#[derive(Clone, Copy)]
struct BinTally {
    bin: usize,
    remembered: f32,
    count: f32,
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
    let mut predicted = vec![0.0f64; BIN_COUNT];
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
                tally.remembered += answer as f32;
                tally.count += 1.0;
            }
            None => card.push(BinTally {
                bin,
                remembered: answer as f32,
                count: 1.0,
            }),
        }
    }

    let intervals = bootstrap_intervals(&by_card.into_values().collect::<Vec<_>>());
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
    // Each round is its own draw, so the rounds run in parallel; each keeps
    // the seed of its own number, so the interval does not depend on the
    // machine or on how the work is shared out.
    let rounds: Vec<Vec<Option<f64>>> = (0..BOOTSTRAP_ROUNDS)
        .into_par_iter()
        .map(|round| {
            let mut state = BOOTSTRAP_SEED.wrapping_add((round as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15));
            let mut next = move || {
                // xorshift64*, so the interval is the same on every machine
                state ^= state >> 12;
                state ^= state << 25;
                state ^= state >> 27;
                state.wrapping_mul(0x2545_F491_4F6C_DD1D)
            };
            let mut remembered = vec![0.0f64; BIN_COUNT];
            let mut counts = vec![0.0f64; BIN_COUNT];
            for _ in 0..cards.len() {
                for tally in &cards[(next() % cards.len() as u64) as usize] {
                    remembered[tally.bin] += tally.remembered as f64;
                    counts[tally.bin] += tally.count as f64;
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
    let mut counts = vec![0u32; UM_BIN_COUNT];
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

/// One model's rows, from a single sample role: the first role of `roles`
/// that has any row, or, when `most_rows` is set, the role of `roles` with
/// the most rows. Roles are never mixed.
fn read_predictions(
    storage: &crate::storage::SqliteStorage,
    table: &str,
    roles: &[&str],
    after: TimestampMillis,
    most_rows: bool,
) -> Result<CachedPredictions> {
    let stored = storage.cached_review_prediction_roles(table)?;
    let count_of = |role: &str| {
        stored
            .iter()
            .find(|(stored, _)| stored == role)
            .map_or(0, |(_, count)| *count)
    };
    let chosen = if most_rows {
        roles
            .iter()
            .filter(|role| count_of(role) > 0)
            .max_by_key(|role| count_of(role))
    } else {
        roles.iter().find(|role| count_of(role) > 0)
    };
    let Some(role) = chosen else {
        return Ok(CachedPredictions::none());
    };
    Ok(CachedPredictions {
        role: (*role).to_string(),
        by_review: storage
            .cached_review_predictions(table, role, after)?
            .into_iter()
            .collect(),
    })
}
