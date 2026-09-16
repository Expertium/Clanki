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
        )?;
        let rwkv = read_predictions(
            storage,
            RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE,
            RWKV_ROLES,
            cutoff,
        )?;

        let mut response = ReviewPredictionsResponse {
            fsrs_role: fsrs.role.clone(),
            rwkv_role: rwkv.role.clone(),
            ..Default::default()
        };
        let mut ratings = ratings;
        ratings.sort_unstable_by_key(|entry| entry.id);
        for entry in &ratings {
            match (fsrs.by_review.get(&entry.id), rwkv.by_review.get(&entry.id)) {
                (Some(&fsrs_value), Some(&rwkv_value)) => {
                    response.revlog_ids.push(entry.id.0);
                    response.card_ids.push(entry.cid.0);
                    response.remembered.push(entry.button_chosen > 1);
                    response.fsrs_predictions.push(fsrs_value);
                    response.rwkv_predictions.push(rwkv_value);
                }
                (Some(_), None) => response.fsrs_only += 1,
                (None, Some(_)) => response.rwkv_only += 1,
                (None, None) => response.unscored += 1,
            }
        }

        // how fresh the predictions are: the newest rating either model has
        // scored, and the ratings of the search after it
        let newest = [
            storage.newest_cached_review_prediction(
                FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE,
                &fsrs.role,
            )?,
            storage.newest_cached_review_prediction(
                RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE,
                &rwkv.role,
            )?,
        ]
        .into_iter()
        .flatten()
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

/// The first role of `roles` that has any prediction for the searched
/// ratings, and its rows; roles are never mixed.
fn read_predictions(
    storage: &crate::storage::SqliteStorage,
    table: &str,
    roles: &[&str],
    after: TimestampMillis,
) -> Result<CachedPredictions> {
    for role in roles {
        let rows = storage.cached_review_predictions(table, role, after)?;
        if !rows.is_empty() {
            return Ok(CachedPredictions {
                role: (*role).to_string(),
                by_review: rows.into_iter().collect(),
            });
        }
    }
    Ok(CachedPredictions::none())
}
