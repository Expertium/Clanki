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

use anki_proto::stats::ReviewPredictionsResponse;

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
            if fsrs_value.is_some() || rwkv_value.is_some() {
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

/// One card's answers in one bin, for the bootstrap.
#[derive(Default, Clone, Copy)]
struct BinTally {
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
    let mut sums = vec![BinTally::default(); BIN_COUNT];
    let mut predicted = [0.0f64; BIN_COUNT];
    let mut by_card: HashMap<i64, Vec<BinTally>> = HashMap::new();
    for ((&prediction, &remembered), &card_id) in predictions.iter().zip(remembered).zip(card_ids) {
        if !prediction.is_finite() || !(0.0..=1.0).contains(&prediction) {
            continue;
        }
        let bin = bin_of(prediction);
        let answer = f64::from(remembered);
        predicted[bin] += prediction as f64;
        sums[bin].remembered += answer;
        sums[bin].count += 1.0;
        let card = by_card
            .entry(card_id)
            .or_insert_with(|| vec![BinTally::default(); BIN_COUNT]);
        card[bin].remembered += answer;
        card[bin].count += 1.0;
    }

    let intervals = bootstrap_intervals(&by_card.into_values().collect::<Vec<_>>());
    (0..BIN_COUNT)
        .filter(|&bin| sums[bin].count > 0.0)
        .map(|bin| anki_proto::stats::CalibrationBin {
            index: bin as u32,
            sum_predicted: predicted[bin],
            sum_remembered: sums[bin].remembered,
            count: sums[bin].count as u32,
            low: intervals[bin].0,
            high: intervals[bin].1,
        })
        .collect()
}

/// For each bin, the 2.5 and 97.5 percentiles of its share of remembered
/// answers over resamples of the cards; (0, 0) for a bin no resample fills.
fn bootstrap_intervals(cards: &[Vec<BinTally>]) -> Vec<(f64, f64)> {
    let mut shares: Vec<Vec<f64>> = vec![vec![]; BIN_COUNT];
    if cards.is_empty() {
        return vec![(0.0, 0.0); BIN_COUNT];
    }
    let mut state = BOOTSTRAP_SEED;
    let mut next = || {
        // xorshift64*, so the interval is the same on every machine
        state ^= state >> 12;
        state ^= state << 25;
        state ^= state >> 27;
        state.wrapping_mul(0x2545_F491_4F6C_DD1D)
    };
    for _ in 0..BOOTSTRAP_ROUNDS {
        let mut remembered = [0.0f64; BIN_COUNT];
        let mut counts = [0.0f64; BIN_COUNT];
        for _ in 0..cards.len() {
            let card = &cards[(next() % cards.len() as u64) as usize];
            for bin in 0..BIN_COUNT {
                remembered[bin] += card[bin].remembered;
                counts[bin] += card[bin].count;
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

/// The percentile of sorted values, interpolated between the two nearest.
fn percentile(values: &[f64], percentile: f64) -> f64 {
    let position = (values.len() - 1) as f64 * percentile;
    let lower = position.floor() as usize;
    let upper = position.ceil() as usize;
    let weight = position - lower as f64;
    values[lower] * (1.0 - weight) + values[upper] * weight
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
