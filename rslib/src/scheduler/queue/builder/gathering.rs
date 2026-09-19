// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::hash::Hasher;

use fnv::FnvHasher;

use super::DueCard;
use super::NewCard;
use super::QueueBuilder;
use crate::card::Card;
use crate::collection::RwkvReviewQueueScoreEntry;
use crate::deckconfig::NewCardGatherPriority;
use crate::deckconfig::ReviewCardOrder;
use crate::decks::limits::LimitKind;
use crate::prelude::*;
use crate::scheduler::fsrs::memory_state::FsrsCardCurves;
use crate::scheduler::queue::DeferredRwkvReview;
use crate::scheduler::queue::DueCardKind;
use crate::scheduler::rwkv::rwkv_review_candidate_metadata;
use crate::scheduler::rwkv::rwkv_review_order_keys;
use crate::scheduler::rwkv::rwkv_review_relative_overdueness;
use crate::scheduler::rwkv::rwkv_review_score_eligibility;
use crate::scheduler::rwkv::rwkv_review_score_eligibility_ignoring_retention;
use crate::scheduler::rwkv::RwkvReviewScoreEligibility;
use crate::scheduler::timing::SchedTimingToday;
use crate::storage::card::NewCardSorting;

#[derive(Debug, Clone, Copy)]
struct DueCardForRetrievabilitySort {
    card: DueCard,
    counts_towards_review_limit: bool,
    interday_or_review: bool,
}

pub(super) const RWKV_REVIEW_GATHER_MIN_CHUNK_SIZE: usize = 256;

impl QueueBuilder {
    pub(super) fn gather_cards(&mut self, col: &mut Collection) -> Result<()> {
        if self.context.sort_options.uses_rwkv_review_order() {
            self.gather_intraday_learning_cards(col)?;
            self.gather_due_cards(col, DueCardKind::Learning)?;
            // without scores, no review cards: the queue waits for RWKV
            // instead of using FSRS-7's due dates (spec sched.rwkv-instant-waits)
            if self.context.uses_rwkv_review_order() {
                self.gather_review_cards_with_rwkv_scores(col)?;
            }
            self.gather_new_cards(col)?;
            return Ok(());
        }

        if self.context.non_news_sorted_by_retrievability() {
            self.gather_due_non_new_cards_with_exact_retrievability(col)?;
            self.gather_new_cards(col)?;
            return Ok(());
        }

        self.gather_intraday_learning_cards(col)?;
        self.gather_due_cards(col, DueCardKind::Learning)?;
        self.gather_due_cards(col, DueCardKind::Review)?;
        self.gather_new_cards(col)?;

        Ok(())
    }

    fn gather_review_cards_with_rwkv_scores(&mut self, col: &mut Collection) -> Result<()> {
        if matches!(
            self.context.sort_options.review_order,
            ReviewCardOrder::RetrievabilityAscending
                | ReviewCardOrder::RetrievabilityDescending
                | ReviewCardOrder::RelativeOverdueness
        ) {
            self.gather_review_cards_by_rwkv_priority(col)
        } else {
            self.gather_review_cards_by_configured_order(col)
        }
    }

    fn gather_review_cards_by_rwkv_priority(&mut self, col: &mut Collection) -> Result<()> {
        if self.limits.root_limit_reached(LimitKind::Review) {
            return Ok(());
        }

        let scores = self.context.rwkv_review_queue_scores.clone().unwrap();
        let review_order = self.context.sort_options.review_order;
        let relative_overdueness = matches!(review_order, ReviewCardOrder::RelativeOverdueness);
        let candidate_metadata = if relative_overdueness {
            let card_ids = scores.keys().copied().collect::<Vec<_>>();
            rwkv_review_candidate_metadata(col, &card_ids, self.context.timing)?
        } else {
            HashMap::new()
        };
        let priorities = scores
            .iter()
            .filter_map(|(&card_id, &score)| {
                if !score.retrievability.is_finite() {
                    return None;
                }
                let priority = match review_order {
                    ReviewCardOrder::RetrievabilityDescending => -score.retrievability,
                    ReviewCardOrder::RelativeOverdueness => rwkv_review_relative_overdueness(
                        score.retrievability,
                        candidate_metadata.get(&card_id)?,
                        score.target_retention,
                    ),
                    _ => score.retrievability,
                };
                priority.is_finite().then_some((card_id, priority))
            })
            .collect::<HashMap<_, _>>();
        let mut ranked_scores: Vec<_> = scores
            .iter()
            .filter_map(|(&card_id, &score)| {
                priorities
                    .contains_key(&card_id)
                    .then_some((card_id, score))
            })
            .collect();
        let compare_scores =
            |(card_id_a, _): &(CardId, RwkvReviewQueueScoreEntry),
             (card_id_b, _): &(CardId, RwkvReviewQueueScoreEntry)| {
                priorities[card_id_a]
                    .total_cmp(&priorities[card_id_b])
                    .then_with(|| card_id_a.cmp(card_id_b))
            };
        let mut remaining_scores = ranked_scores.as_mut_slice();
        let mut chunk_size = (self.limits.remaining_root_limit(LimitKind::Review) as usize)
            .max(RWKV_REVIEW_GATHER_MIN_CHUNK_SIZE)
            .min(remaining_scores.len());
        while !remaining_scores.is_empty() && !self.limits.root_limit_reached(LimitKind::Review) {
            if chunk_size < remaining_scores.len() {
                remaining_scores.select_nth_unstable_by(chunk_size, compare_scores);
            }
            let (score_chunk, rest) = remaining_scores.split_at_mut(chunk_size);
            score_chunk.sort_unstable_by(compare_scores);
            let chunk_card_ids: Vec<_> = score_chunk.iter().map(|(card_id, _)| *card_id).collect();
            let mut cards_by_id = HashMap::with_capacity(score_chunk.len());
            col.storage
                .for_each_review_card_in_active_decks_with_ids(&chunk_card_ids, |card| {
                    cards_by_id.insert(card.id, card);
                    Ok(true)
                })?;
            let active_card_ids: Vec<_> = cards_by_id.keys().copied().collect();
            let fetched_metadata;
            let chunk_metadata = if relative_overdueness {
                &candidate_metadata
            } else {
                fetched_metadata =
                    rwkv_review_candidate_metadata(col, &active_card_ids, self.context.timing)?;
                &fetched_metadata
            };

            for &(card_id, score) in score_chunk.iter() {
                if self.limits.root_limit_reached(LimitKind::Review) {
                    break;
                }
                let Some(card) = cards_by_id.get(&card_id).copied() else {
                    continue;
                };
                let metadata = chunk_metadata.get(&card_id).or_not_found(card_id)?;
                let eligibility = rwkv_review_score_eligibility(
                    score.retrievability,
                    metadata,
                    self.context.sort_options.rwkv_review_allow_same_day_review,
                    self.context
                        .sort_options
                        .rwkv_review_min_intervening_reviews,
                    self.context.sort_options.rwkv_review_min_elapsed_secs,
                    score.intervening_reviews,
                    score.target_retention,
                );
                match eligibility {
                    RwkvReviewScoreEligibility::Eligible => {}
                    RwkvReviewScoreEligibility::Deferred { .. } => {
                        self.deferred_rwkv_reviews.insert(
                            card_id,
                            DeferredRwkvReview::from_eligibility(
                                eligibility,
                                self.context.timing.now,
                            )
                            .unwrap(),
                        );
                        continue;
                    }
                    RwkvReviewScoreEligibility::Blocked => continue,
                }
                if !self
                    .limits
                    .limit_reached(card.current_deck_id, LimitKind::Review)?
                    && self.add_due_card(card)
                {
                    self.limits
                        .reserve_review(card.current_deck_id, card.original_deck_id)?;
                }
            }

            remaining_scores = rest;
            chunk_size = chunk_size.saturating_mul(2).min(remaining_scores.len());
        }

        if !self.limits.root_limit_reached(LimitKind::Review)
            && self.limits.any_rwkv_review_minimum_remaining()
        {
            self.gather_rwkv_review_minimum_cards(
                col,
                &ranked_scores,
                relative_overdueness.then_some(&priorities),
            )?;
        }
        self.sort_review_cards_by_rwkv_priority(&priorities);
        Ok(())
    }

    fn gather_review_cards_by_configured_order(&mut self, col: &mut Collection) -> Result<()> {
        if self.limits.root_limit_reached(LimitKind::Review) {
            return Ok(());
        }

        let scores = self.context.rwkv_review_queue_scores.clone().unwrap();
        let scored_card_ids: Vec<_> = scores.keys().copied().collect();
        let metadata = rwkv_review_candidate_metadata(col, &scored_card_ids, self.context.timing)?;
        let mut eligibility_by_card = HashMap::new();
        let mut deferred_reviews = Vec::new();
        for (&card_id, score) in scores.iter() {
            let Some(metadata) = metadata.get(&card_id) else {
                continue;
            };
            let eligibility = rwkv_review_score_eligibility(
                score.retrievability,
                metadata,
                self.context.sort_options.rwkv_review_allow_same_day_review,
                self.context
                    .sort_options
                    .rwkv_review_min_intervening_reviews,
                self.context.sort_options.rwkv_review_min_elapsed_secs,
                score.intervening_reviews,
                score.target_retention,
            );
            match eligibility {
                RwkvReviewScoreEligibility::Eligible | RwkvReviewScoreEligibility::Blocked => {}
                RwkvReviewScoreEligibility::Deferred { .. } => {
                    deferred_reviews.push((
                        card_id,
                        DeferredRwkvReview::from_eligibility(eligibility, self.context.timing.now)
                            .unwrap(),
                    ));
                }
            }
            eligibility_by_card.insert(card_id, eligibility);
        }
        self.deferred_rwkv_reviews.extend(deferred_reviews);
        let mut cards = Vec::new();

        col.storage.for_each_review_card_in_active_decks(
            self.context.timing,
            self.context.sort_options.review_order,
            self.context.fsrs,
            |card| {
                cards.push(card);
                Ok(true)
            },
        )?;

        for card in cards.iter().copied() {
            if self.limits.root_limit_reached(LimitKind::Review) {
                break;
            }
            // an unscored card waits for its score (spec sched.rwkv-instant-waits)
            let eligible = matches!(
                eligibility_by_card.get(&card.id),
                Some(RwkvReviewScoreEligibility::Eligible)
            );
            if eligible
                && !self
                    .limits
                    .limit_reached(card.current_deck_id, LimitKind::Review)?
                && self.add_due_card(card)
            {
                self.limits
                    .reserve_review(card.current_deck_id, card.original_deck_id)?;
            }
        }

        if self.limits.any_rwkv_review_minimum_remaining() {
            let mut pull_candidates = cards.clone();
            pull_candidates.sort_by(|card_a, card_b| {
                let score_a = scores
                    .get(&card_a.id)
                    .map(|score| score.retrievability)
                    .filter(|score| score.is_finite());
                let score_b = scores
                    .get(&card_b.id)
                    .map(|score| score.retrievability)
                    .filter(|score| score.is_finite());
                match (score_a, score_b) {
                    (Some(score_a), Some(score_b)) => score_a
                        .total_cmp(&score_b)
                        .then_with(|| card_a.id.cmp(&card_b.id)),
                    (Some(_), None) => std::cmp::Ordering::Less,
                    (None, Some(_)) => std::cmp::Ordering::Greater,
                    (None, None) => std::cmp::Ordering::Equal,
                }
            });
            for card in pull_candidates {
                if !self.limits.any_rwkv_review_minimum_remaining() {
                    break;
                }
                if self.limits.root_limit_reached(LimitKind::Review) {
                    break;
                }
                if !self
                    .limits
                    .rwkv_review_minimum_remaining(card.current_deck_id)?
                    || !matches!(
                        eligibility_by_card.get(&card.id),
                        Some(RwkvReviewScoreEligibility::Blocked)
                    )
                {
                    continue;
                }
                let score = scores.get(&card.id).or_not_found(card.id)?;
                let metadata = metadata.get(&card.id).or_not_found(card.id)?;
                let eligibility = rwkv_review_score_eligibility_ignoring_retention(
                    score.retrievability,
                    metadata,
                    self.context.sort_options.rwkv_review_allow_same_day_review,
                    self.context
                        .sort_options
                        .rwkv_review_min_intervening_reviews,
                    self.context.sort_options.rwkv_review_min_elapsed_secs,
                    score.intervening_reviews,
                );
                match eligibility {
                    RwkvReviewScoreEligibility::Eligible => {}
                    RwkvReviewScoreEligibility::Deferred { .. } => {
                        self.deferred_rwkv_reviews.insert(
                            card.id,
                            DeferredRwkvReview::from_eligibility(
                                eligibility,
                                self.context.timing.now,
                            )
                            .unwrap(),
                        );
                        continue;
                    }
                    RwkvReviewScoreEligibility::Blocked => continue,
                }
                if !self
                    .limits
                    .limit_reached(card.current_deck_id, LimitKind::Review)?
                    && self.add_due_card(card)
                {
                    self.limits
                        .reserve_review(card.current_deck_id, card.original_deck_id)?;
                }
            }
        }

        let configured_position: HashMap<_, _> = cards
            .iter()
            .enumerate()
            .map(|(position, card)| (card.id, position))
            .collect();
        self.review.sort_by_key(|card| {
            configured_position
                .get(&card.id)
                .copied()
                .unwrap_or(usize::MAX)
        });
        Ok(())
    }

    fn gather_rwkv_review_minimum_cards(
        &mut self,
        col: &mut Collection,
        ranked_scores: &[(CardId, RwkvReviewQueueScoreEntry)],
        relative_overdueness_priorities: Option<&HashMap<CardId, f32>>,
    ) -> Result<()> {
        let mut pull_scores = ranked_scores.to_vec();
        pull_scores.sort_unstable_by(|(card_id_a, score_a), (card_id_b, score_b)| {
            let priority_a = relative_overdueness_priorities
                .and_then(|priorities| priorities.get(card_id_a))
                .copied()
                .unwrap_or(score_a.retrievability);
            let priority_b = relative_overdueness_priorities
                .and_then(|priorities| priorities.get(card_id_b))
                .copied()
                .unwrap_or(score_b.retrievability);
            priority_a
                .total_cmp(&priority_b)
                .then_with(|| card_id_a.cmp(card_id_b))
        });
        for score_chunk in pull_scores.chunks(RWKV_REVIEW_GATHER_MIN_CHUNK_SIZE) {
            if self.limits.root_limit_reached(LimitKind::Review) {
                break;
            }
            let chunk_card_ids: Vec<_> = score_chunk.iter().map(|(card_id, _)| *card_id).collect();
            let mut cards_by_id = HashMap::with_capacity(score_chunk.len());
            col.storage
                .for_each_review_card_in_active_decks_with_ids(&chunk_card_ids, |card| {
                    cards_by_id.insert(card.id, card);
                    Ok(true)
                })?;
            let active_card_ids: Vec<_> = cards_by_id.keys().copied().collect();
            let metadata =
                rwkv_review_candidate_metadata(col, &active_card_ids, self.context.timing)?;

            for &(card_id, score) in score_chunk {
                if self.limits.root_limit_reached(LimitKind::Review) {
                    break;
                }
                let Some(card) = cards_by_id.get(&card_id).copied() else {
                    continue;
                };
                if !self
                    .limits
                    .rwkv_review_minimum_remaining(card.current_deck_id)?
                {
                    continue;
                }
                let metadata = metadata.get(&card_id).or_not_found(card_id)?;
                if !matches!(
                    rwkv_review_score_eligibility(
                        score.retrievability,
                        metadata,
                        self.context.sort_options.rwkv_review_allow_same_day_review,
                        self.context
                            .sort_options
                            .rwkv_review_min_intervening_reviews,
                        self.context.sort_options.rwkv_review_min_elapsed_secs,
                        score.intervening_reviews,
                        score.target_retention,
                    ),
                    RwkvReviewScoreEligibility::Blocked
                ) {
                    continue;
                }
                let eligibility = rwkv_review_score_eligibility_ignoring_retention(
                    score.retrievability,
                    metadata,
                    self.context.sort_options.rwkv_review_allow_same_day_review,
                    self.context
                        .sort_options
                        .rwkv_review_min_intervening_reviews,
                    self.context.sort_options.rwkv_review_min_elapsed_secs,
                    score.intervening_reviews,
                );
                match eligibility {
                    RwkvReviewScoreEligibility::Eligible => {}
                    RwkvReviewScoreEligibility::Deferred { .. } => {
                        self.deferred_rwkv_reviews.insert(
                            card_id,
                            DeferredRwkvReview::from_eligibility(
                                eligibility,
                                self.context.timing.now,
                            )
                            .unwrap(),
                        );
                        continue;
                    }
                    RwkvReviewScoreEligibility::Blocked => continue,
                }
                if !self
                    .limits
                    .limit_reached(card.current_deck_id, LimitKind::Review)?
                    && self.add_due_card(card)
                {
                    self.limits
                        .reserve_review(card.current_deck_id, card.original_deck_id)?;
                }
            }
        }
        Ok(())
    }

    fn sort_review_cards_by_rwkv_priority(&mut self, priorities: &HashMap<CardId, f32>) {
        self.review.sort_by(|card_a, card_b| {
            match (priorities.get(&card_a.id), priorities.get(&card_b.id)) {
                (Some(priority_a), Some(priority_b)) => priority_a
                    .total_cmp(priority_b)
                    .then_with(|| card_a.id.cmp(&card_b.id)),
                (Some(_), None) => std::cmp::Ordering::Less,
                (None, Some(_)) => std::cmp::Ordering::Greater,
                (None, None) => std::cmp::Ordering::Equal,
            }
        });
    }

    fn gather_due_non_new_cards_with_exact_retrievability(
        &mut self,
        col: &mut Collection,
    ) -> Result<()> {
        let mut due_cards = Vec::new();
        // the rows of the interday cards, read by their gather query; the
        // few due intraday cards are read one by one
        let mut rows = HashMap::new();
        self.gather_intraday_learning_cards_for_retrievability_sort(col, &mut due_cards)?;
        self.gather_due_cards_for_retrievability_sort(
            col,
            DueCardKind::Learning,
            &mut due_cards,
            &mut rows,
        )?;
        self.gather_due_cards_for_retrievability_sort(
            col,
            DueCardKind::Review,
            &mut due_cards,
            &mut rows,
        )?;

        // the result is sorted by (key, hash, id) below, so the order the
        // cards were gathered in does not matter
        let mut keys =
            ExactReviewOrderKeys::new(self.context.timing, self.context.sort_options.review_order);
        let mut with_key = Vec::with_capacity(due_cards.len());
        for candidate in due_cards {
            let card_id = candidate.card.id;
            let key = match rows.get(&card_id) {
                Some(card) => keys.key(col, card)?,
                None => {
                    let card = col.storage.get_card(card_id)?.or_not_found(card_id)?;
                    keys.key(col, &card)?
                }
            };
            with_key.push((candidate, key, fnvhash_due_card(&candidate.card)));
        }
        let descending = matches!(
            self.context.sort_options.review_order,
            ReviewCardOrder::RetrievabilityDescending
        );
        with_key.sort_by(
            |(candidate_a, key_a, hash_a), (candidate_b, key_b, hash_b)| {
                let ord = key_a.total_cmp(key_b);
                let ord = if descending { ord.reverse() } else { ord };
                ord.then_with(|| hash_a.cmp(hash_b))
                    .then_with(|| candidate_a.card.id.cmp(&candidate_b.card.id))
            },
        );

        for (candidate, _, _) in with_key {
            if candidate.counts_towards_review_limit
                && (self.limits.root_limit_reached(LimitKind::Review)
                    || self
                        .limits
                        .limit_reached(candidate.card.current_deck_id, LimitKind::Review)?)
            {
                continue;
            }

            if self
                .add_due_card_for_retrievability_sort(candidate.card, candidate.interday_or_review)
            {
                self.r_sorted_non_new.push(candidate.card);

                if candidate.counts_towards_review_limit {
                    self.limits.reserve_review(
                        candidate.card.current_deck_id,
                        candidate.card.original_deck_id,
                    )?;
                }
            }
        }

        Ok(())
    }

    fn gather_intraday_learning_cards(&mut self, col: &mut Collection) -> Result<()> {
        col.storage.for_each_intraday_card_in_active_decks(
            self.context.timing.next_day_at,
            |card| {
                if self.card_is_pinned(card.id) {
                    return;
                }
                self.get_and_update_bury_mode_for_note(card.into());
                self.learning.push(card);
            },
        )?;

        Ok(())
    }

    fn gather_intraday_learning_cards_for_retrievability_sort(
        &mut self,
        col: &mut Collection,
        due_cards: &mut Vec<DueCardForRetrievabilitySort>,
    ) -> Result<()> {
        col.storage.for_each_intraday_card_in_active_decks(
            self.context.timing.next_day_at,
            |card| {
                if self.card_is_pinned(card.id) {
                    return;
                }
                if card.due <= self.context.timing.now.0 as i32 {
                    due_cards.push(DueCardForRetrievabilitySort {
                        card,
                        counts_towards_review_limit: false,
                        interday_or_review: false,
                    });
                } else {
                    self.learning.push(card);
                }
            },
        )?;

        Ok(())
    }

    fn gather_due_cards_for_retrievability_sort(
        &mut self,
        col: &mut Collection,
        kind: DueCardKind,
        due_cards: &mut Vec<DueCardForRetrievabilitySort>,
        rows: &mut HashMap<CardId, Card>,
    ) -> Result<()> {
        col.storage
            .for_each_due_card_row_in_active_decks(self.context.timing, kind, |card| {
                due_cards.push(DueCardForRetrievabilitySort {
                    card: DueCard::from_card(&card, kind),
                    counts_towards_review_limit: true,
                    interday_or_review: true,
                });
                rows.insert(card.id, card);
                Ok(())
            })
    }

    fn gather_due_cards(&mut self, col: &mut Collection, kind: DueCardKind) -> Result<()> {
        if self.limits.root_limit_reached(LimitKind::Review) {
            return Ok(());
        }
        if self.context.sort_options.review_order_from_rwkv_keys() {
            return self.gather_due_cards_by_rwkv_keys(col, kind);
        }
        col.storage.for_each_due_card_in_active_decks(
            self.context.timing,
            self.context.sort_options.review_order,
            kind,
            self.context.fsrs,
            |card| {
                if self.limits.root_limit_reached(LimitKind::Review) {
                    return Ok(false);
                }
                if !self
                    .limits
                    .limit_reached(card.current_deck_id, LimitKind::Review)?
                    && self.add_due_card(card)
                {
                    self.limits
                        .reserve_review(card.current_deck_id, card.original_deck_id)?;
                }
                Ok(true)
            },
        )
    }

    /// RWKV presets sorted by retrievability or relative overdueness take
    /// RWKV's own measure (spec sched.rwkv-review-order), not FSRS's: every
    /// due card of `kind` is ranked by `rwkv_review_order_keys`, then the
    /// limits apply in that order.
    fn gather_due_cards_by_rwkv_keys(
        &mut self,
        col: &mut Collection,
        kind: DueCardKind,
    ) -> Result<()> {
        // one query gives both the queue entries and the rows the keys need;
        // the result is sorted by (key, hash, id) below, so the order the
        // rows come in does not matter
        let mut cards = Vec::new();
        col.storage
            .for_each_due_card_row_in_active_decks(self.context.timing, kind, |card| {
                cards.push(card);
                Ok(())
            })?;
        let due_cards: Vec<_> = cards
            .iter()
            .map(|card| DueCard::from_card(card, kind))
            .collect();
        let keys = rwkv_review_order_keys(
            col,
            cards,
            self.context.timing,
            self.context.sort_options.review_order,
        )?;
        let mut with_key: Vec<_> = due_cards
            .into_iter()
            .map(|card| {
                let key = keys.get(&card.id).copied().unwrap_or(f32::INFINITY);
                (card, key, fnvhash_due_card(&card))
            })
            .collect();
        with_key.sort_by(|(card_a, key_a, hash_a), (card_b, key_b, hash_b)| {
            key_a
                .total_cmp(key_b)
                .then_with(|| hash_a.cmp(hash_b))
                .then_with(|| card_a.id.cmp(&card_b.id))
        });
        for (card, _, _) in with_key {
            if self.limits.root_limit_reached(LimitKind::Review) {
                break;
            }
            if !self
                .limits
                .limit_reached(card.current_deck_id, LimitKind::Review)?
                && self.add_due_card(card)
            {
                self.limits
                    .reserve_review(card.current_deck_id, card.original_deck_id)?;
            }
        }
        Ok(())
    }

    fn gather_new_cards(&mut self, col: &mut Collection) -> Result<()> {
        // Every gather order below takes nothing once the root limit is
        // reached, so skip its query: a sorted scan of all new cards would
        // otherwise run on each queue build of a day whose new cards are done.
        if self.limits.root_limit_reached(LimitKind::New) {
            return Ok(());
        }
        let salt = Self::knuth_salt(self.context.timing.days_elapsed);
        match self.context.sort_options.new_gather_priority {
            NewCardGatherPriority::Deck => {
                self.gather_new_cards_by_deck(col, NewCardSorting::LowestPosition)
            }
            NewCardGatherPriority::DeckThenRandomNotes => {
                self.gather_new_cards_by_deck(col, NewCardSorting::RandomNotes(salt))
            }
            NewCardGatherPriority::LowestPosition => {
                self.gather_new_cards_sorted(col, NewCardSorting::LowestPosition)
            }
            NewCardGatherPriority::HighestPosition => {
                self.gather_new_cards_sorted(col, NewCardSorting::HighestPosition)
            }
            // RWKV-Instant only; any other algorithm gathers as Deck does
            // (spec deck-options.new-retrievability-order-instant-only)
            NewCardGatherPriority::AscendingRetrievability
            | NewCardGatherPriority::DescendingRetrievability
                if self.context.rwkv_review_queue_scores.is_none() =>
            {
                self.gather_new_cards_by_deck(col, NewCardSorting::LowestPosition)
            }
            NewCardGatherPriority::AscendingRetrievability => {
                self.gather_new_cards_by_retrievability(col, false)
            }
            NewCardGatherPriority::DescendingRetrievability => {
                self.gather_new_cards_by_retrievability(col, true)
            }
            NewCardGatherPriority::RandomNotes => {
                self.gather_new_cards_sorted(col, NewCardSorting::RandomNotes(salt))
            }
            NewCardGatherPriority::RandomCards => {
                self.gather_new_cards_sorted(col, NewCardSorting::RandomCards(salt))
            }
        }
    }

    fn gather_new_cards_by_deck(
        &mut self,
        col: &mut Collection,
        sort: NewCardSorting,
    ) -> Result<()> {
        for deck_id in col.storage.get_active_deck_ids_sorted()? {
            if self.limits.root_limit_reached(LimitKind::New) {
                break;
            }
            if self.limits.limit_reached(deck_id, LimitKind::New)? {
                continue;
            }
            col.storage
                .for_each_new_card_in_deck(deck_id, sort, |card| {
                    let limit_reached = self.limits.limit_reached(deck_id, LimitKind::New)?;
                    if !limit_reached && self.add_new_card(card) {
                        self.limits
                            .decrement_deck_and_parent_limits(deck_id, LimitKind::New)?;
                    }
                    Ok(!limit_reached)
                })?;
        }

        Ok(())
    }

    fn gather_new_cards_sorted(
        &mut self,
        col: &mut Collection,
        order: NewCardSorting,
    ) -> Result<()> {
        col.storage
            .for_each_new_card_in_active_decks(order, |card| {
                if self.limits.root_limit_reached(LimitKind::New) {
                    return Ok(false);
                }
                if !self
                    .limits
                    .limit_reached(card.current_deck_id, LimitKind::New)?
                    && self.add_new_card(card)
                {
                    self.limits
                        .decrement_deck_and_parent_limits(card.current_deck_id, LimitKind::New)?;
                }
                Ok(true)
            })
    }

    fn gather_new_cards_by_retrievability(
        &mut self,
        col: &mut Collection,
        descending: bool,
    ) -> Result<()> {
        let mut cards = Vec::new();
        col.storage
            .for_each_new_card_in_active_decks(NewCardSorting::LowestPosition, |card| {
                cards.push(card);
                Ok(true)
            })?;

        if let Some(scores) = self.context.rwkv_review_queue_scores.as_ref() {
            cards.sort_by(|card_a, card_b| {
                let score_a = scores
                    .get(&card_a.id)
                    .map(|score| score.retrievability)
                    .filter(|score| score.is_finite());
                let score_b = scores
                    .get(&card_b.id)
                    .map(|score| score.retrievability)
                    .filter(|score| score.is_finite());
                match (score_a, score_b) {
                    (Some(score_a), Some(score_b)) => {
                        let ord = score_a.total_cmp(&score_b);
                        if descending {
                            ord.reverse()
                        } else {
                            ord
                        }
                    }
                    (Some(_), None) => std::cmp::Ordering::Less,
                    (None, Some(_)) => std::cmp::Ordering::Greater,
                    (None, None) => std::cmp::Ordering::Equal,
                }
            });
        }

        for card in cards {
            if self.limits.root_limit_reached(LimitKind::New) {
                break;
            }
            if !self
                .limits
                .limit_reached(card.current_deck_id, LimitKind::New)?
                && self.add_new_card(card)
            {
                self.limits
                    .decrement_deck_and_parent_limits(card.current_deck_id, LimitKind::New)?;
            }
        }

        Ok(())
    }

    /// True if limit should be decremented.
    pub(super) fn add_due_card(&mut self, card: DueCard) -> bool {
        if self.card_is_pinned(card.id) {
            return false;
        }
        let added = self.add_due_card_for_retrievability_sort(card, true);
        if added {
            match card.kind {
                DueCardKind::Review => self.review.push(card),
                DueCardKind::Learning => self.day_learning.push(card),
            }
        }

        added
    }

    pub(super) fn add_due_card_for_retrievability_sort(
        &mut self,
        card: DueCard,
        interday_or_review: bool,
    ) -> bool {
        if self.card_is_pinned(card.id) {
            return false;
        }
        let bury_this_card = self
            .get_and_update_bury_mode_for_note(card.into())
            .map(|mode| match card.kind {
                DueCardKind::Review => mode.bury_reviews,
                DueCardKind::Learning if interday_or_review => mode.bury_interday_learning,
                DueCardKind::Learning => false,
            })
            .unwrap_or_default();
        !bury_this_card
    }

    // True if limit should be decremented.
    pub(super) fn add_new_card(&mut self, card: NewCard) -> bool {
        if self.card_is_pinned(card.id) {
            return false;
        }
        let bury_this_card = self
            .get_and_update_bury_mode_for_note(card.into())
            .map(|mode| mode.bury_new)
            .unwrap_or_default();
        // no previous siblings seen?
        if bury_this_card {
            false
        } else {
            self.new.push(card);
            true
        }
    }

    // Generates a salt for use with fnvhash. Useful to increase randomness
    // when the base salt is a small integer.
    fn knuth_salt(base_salt: u32) -> u32 {
        base_salt.wrapping_mul(2654435761)
    }
}

fn elapsed_seconds_since_last_review(card: &Card, timing: SchedTimingToday) -> u32 {
    if let Some(last_review_time) = card.last_review_time {
        timing.now.elapsed_secs_since_clamped(last_review_time)
    } else {
        let due = card.original_or_current_due() as i64;
        if due > 365_000 {
            let last_review_time = TimestampSecs(due.saturating_sub(card.interval as i64));
            timing.now.elapsed_secs_since_clamped(last_review_time)
        } else {
            let review_day = due.saturating_sub(card.interval as i64);
            timing.days_elapsed.saturating_sub(review_day as u32) * 86_400
        }
    }
}

/// The sort keys of FSRS's retrievability orders: the card's retrievability,
/// or its relative overdueness (spec sched.fsrs7-only). During one queue
/// build each home deck's preset and each parameter set's model are made
/// once, not once per card.
struct ExactReviewOrderKeys {
    timing: SchedTimingToday,
    order: ReviewCardOrder,
    curves: FsrsCardCurves,
}

impl ExactReviewOrderKeys {
    fn new(timing: SchedTimingToday, order: ReviewCardOrder) -> Self {
        Self {
            timing,
            order,
            curves: FsrsCardCurves::default(),
        }
    }

    fn key(&mut self, col: &mut Collection, card: &Card) -> Result<f32> {
        let timing = self.timing;
        let Some(state) = card.memory_state else {
            // keep SM2-style fallback ordering when FSRS state is missing
            let due = card.original_or_current_due() as i64;
            let review_day = due.saturating_sub(card.interval as i64);
            let days_elapsed = if due > 365_000 {
                (timing.next_day_at.0 as u32).saturating_sub(due as u32) / 86_400
            } else {
                timing.days_elapsed.saturating_sub(review_day as u32)
            };
            return Ok(-((days_elapsed as f32) + 0.001) / (card.interval as f32).max(1.0));
        };
        let elapsed_days = elapsed_seconds_since_last_review(card, timing) as f32 / 86_400.0;
        if matches!(self.order, ReviewCardOrder::RelativeOverdueness) {
            self.curves
                .relative_overdueness(col, card, state, elapsed_days)
        } else {
            self.curves
                .current_retrievability(col, card, state, elapsed_days)
        }
    }
}

fn fnvhash_due_card(card: &DueCard) -> i64 {
    let mut hasher = FnvHasher::default();
    hasher.write_i64(card.id.0);
    hasher.write_i64(card.mtime.0);
    hasher.finish() as i64
}

#[cfg(test)]
mod test {
    use fsrs::DEFAULT_PARAMETERS;
    use fsrs::FSRS;

    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::scheduler::fsrs::preset::AddonFsrsPreset;
    use crate::scheduler::fsrs::preset::AddonFsrsVersion;
    use crate::scheduler::fsrs::preset::FsrsPresetOverlay;
    use crate::scheduler::fsrs::preset::FsrsPresetRule;
    use crate::scheduler::fsrs::preset::FSRS_PRESET_OVERLAY_CONFIG_KEY;

    /// The key as the queue computed it card by card: the card read again,
    /// its preset resolved and its model built for each card.
    fn per_card_key(
        col: &mut Collection,
        card_id: CardId,
        timing: SchedTimingToday,
        order: ReviewCardOrder,
    ) -> Result<f32> {
        let card = col.storage.get_card(card_id)?.or_not_found(card_id)?;
        if let Some(state) = card.memory_state {
            let elapsed_days = elapsed_seconds_since_last_review(&card, timing) as f32 / 86_400.0;
            if matches!(order, ReviewCardOrder::RelativeOverdueness) {
                col.fsrs_relative_overdueness_for_card_state(&card, state, elapsed_days)
            } else {
                col.fsrs_current_retrievability_for_card_state(card.id, state, elapsed_days)
            }
        } else {
            let due = card.original_or_current_due() as i64;
            let review_day = due.saturating_sub(card.interval as i64);
            let days_elapsed = if due > 365_000 {
                (timing.next_day_at.0 as u32).saturating_sub(due as u32) / 86_400
            } else {
                timing.days_elapsed.saturating_sub(review_day as u32)
            };
            Ok(-((days_elapsed as f32) + 0.001) / (card.interval as f32).max(1.0))
        }
    }

    struct Lcg(u64);

    impl Lcg {
        fn next(&mut self) -> u64 {
            self.0 = self
                .0
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            self.0 >> 33
        }

        fn below(&mut self, n: u64) -> u64 {
            self.next() % n
        }

        fn unit(&mut self) -> f32 {
            self.below(1_000_000) as f32 / 1_000_000.0
        }
    }

    fn perturbed_params(rng: &mut Lcg) -> Vec<f32> {
        DEFAULT_PARAMETERS
            .iter()
            .map(|param| param * (0.9 + 0.2 * rng.unit()))
            .collect()
    }

    /// Decks with their own presets (one with its own desired retention, one
    /// taken over by an add-on overlay preset), and cards of every kind the
    /// keys see.
    fn random_collection(seed: u64, with_overlay: bool) -> Result<(Collection, Vec<CardId>)> {
        let mut rng = Lcg(seed);
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let mut decks = Vec::new();
        for index in 0..4 {
            let mut deck = col.get_or_create_normal_deck(&format!("D{index}"))?;
            let mut config = DeckConfig::default();
            config.inner.fsrs_params_7 = perturbed_params(&mut rng);
            config.inner.desired_retention = 0.8 + 0.15 * rng.unit();
            col.add_or_update_deck_config(&mut config)?;
            let normal = deck.normal_mut()?;
            normal.config_id = config.id.0;
            if index == 3 {
                normal.desired_retention = Some(0.85);
            }
            col.add_or_update_deck(&mut deck)?;
            decks.push(deck.id);
        }
        if with_overlay {
            col.set_config(
                FSRS_PRESET_OVERLAY_CONFIG_KEY,
                &FsrsPresetOverlay {
                    presets: vec![AddonFsrsPreset {
                        id: "addon:test:keys".into(),
                        name: "Keys".into(),
                        fsrs_version: AddonFsrsVersion::Seven,
                        params: perturbed_params(&mut rng),
                        desired_retention: 0.87,
                        historical_retention: 0.9,
                        ignore_revlogs_before_date: String::new(),
                    }],
                    rules: vec![FsrsPresetRule {
                        search: "deck:D1".into(),
                        preset_id: "addon:test:keys".into(),
                    }],
                    simulator_rules: Vec::new(),
                },
            )?;
        }
        let timing = col.timing_today()?;
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut ids = Vec::new();
        for _ in 0..40 {
            let deck_id = decks[rng.below(4) as usize];
            let mut note = nt.new_note();
            note.set_field(0, "front")?;
            col.add_note(&mut note, deck_id)?;
            let mut card = col.storage.get_card_by_ordinal(note.id, 0)?.unwrap();
            let (queue, ctype) = match rng.below(3) {
                0 => (CardQueue::Review, CardType::Review),
                1 => (CardQueue::DayLearn, CardType::Relearn),
                _ => (CardQueue::Learn, CardType::Relearn),
            };
            card.queue = queue;
            card.ctype = ctype;
            card.interval = 1 + rng.below(400) as u32;
            card.due = if queue == CardQueue::Learn {
                timing.now.0 as i32 - rng.below(3600) as i32
            } else {
                timing.days_elapsed as i32 - rng.below(30) as i32
            };
            if rng.below(5) > 0 {
                let stability = 0.5 + 300.0 * rng.unit();
                card.memory_state = Some(FsrsMemoryState {
                    stability,
                    stability_internal: stability * (0.5 + rng.unit()),
                    stability_fast: (rng.below(2) == 0).then(|| stability * rng.unit()),
                    difficulty: 1.0 + 9.0 * rng.unit(),
                });
            }
            if rng.below(3) > 0 {
                card.last_review_time =
                    Some(timing.now.adding_secs(-(rng.below(90 * 86_400) as i64)));
            }
            if rng.below(3) == 0 {
                card.desired_retention = Some(0.7 + 0.25 * rng.unit());
            }
            if rng.below(6) == 0 {
                // a card whose home deck is another deck, as in a filtered deck
                card.original_deck_id = decks[rng.below(4) as usize];
                card.original_due = card.due;
            }
            col.storage.update_card(&card)?;
            ids.push(card.id);
        }
        Ok((col, ids))
    }

    #[test]
    fn kept_presets_and_models_give_the_per_card_keys() -> Result<()> {
        for seed in 0..12 {
            let (mut col, ids) = random_collection(seed, seed % 3 == 0)?;
            let timing = col.timing_today()?;
            for order in [
                ReviewCardOrder::RetrievabilityAscending,
                ReviewCardOrder::RetrievabilityDescending,
                ReviewCardOrder::RelativeOverdueness,
            ] {
                let mut keys = ExactReviewOrderKeys::new(timing, order);
                for &card_id in &ids {
                    let card = col.storage.get_card(card_id)?.unwrap();
                    let kept = keys.key(&mut col, &card)?;
                    let per_card = per_card_key(&mut col, card_id, timing, order)?;
                    assert_eq!(
                        kept.to_bits(),
                        per_card.to_bits(),
                        "seed {seed} {order:?} card {card_id}"
                    );
                }
            }
        }
        Ok(())
    }

    /// The key of a card with a memory state through the fsrs crate's tensor
    /// model, as the queue computed it before the plain-f32 curve.
    fn tensor_path_key(
        col: &mut Collection,
        card: &Card,
        timing: SchedTimingToday,
        order: ReviewCardOrder,
    ) -> Result<f32> {
        let state = card.memory_state.unwrap();
        let elapsed_days = elapsed_seconds_since_last_review(card, timing) as f32 / 86_400.0;
        let preset = col.fsrs_preset_for_card(card)?;
        let fsrs = FSRS::new(&preset.params)?;
        Ok(if matches!(order, ReviewCardOrder::RelativeOverdueness) {
            let target = card
                .desired_retention
                .unwrap_or(preset.desired_retention)
                .clamp(0.0001, 0.9999);
            -elapsed_days.max(0.0)
                / fsrs
                    .interval_at_retrievability(state.into(), target)
                    .max(0.0001)
        } else {
            fsrs.current_retrievability(state.into(), elapsed_days.max(0.0))
        })
    }

    #[test]
    fn keys_are_the_bits_of_the_tensor_path() -> Result<()> {
        for seed in 0..6 {
            let (mut col, ids) = random_collection(seed, seed % 3 == 0)?;
            let timing = col.timing_today()?;
            for order in [
                ReviewCardOrder::RetrievabilityAscending,
                ReviewCardOrder::RelativeOverdueness,
            ] {
                let mut keys = ExactReviewOrderKeys::new(timing, order);
                let mut check = |col: &mut Collection, card: &Card| -> Result<()> {
                    let key = keys.key(col, card)?;
                    let tensor = tensor_path_key(col, card, timing, order)?;
                    assert_eq!(
                        key.to_bits(),
                        tensor.to_bits(),
                        "seed {seed} {order:?} card {:?}",
                        card
                    );
                    Ok(())
                };
                for &card_id in &ids {
                    let card = col.storage.get_card(card_id)?.unwrap();
                    if card.memory_state.is_some() {
                        check(&mut col, &card)?;
                    }
                }
                // the edges: stabilities below, at and above the clamps, no
                // fast stability, difficulty outside its range, elapsed time
                // 0, one second and more than a century, desired retention
                // at and past the solver's bounds
                for &card_id in ids.iter().take(2) {
                    let mut card = col.storage.get_card(card_id)?.unwrap();
                    for stability_internal in [1e-6, 0.0001, 0.3, 36500.0, 1e6] {
                        for stability_fast in [None, Some(1e-6), Some(2.0), Some(1e5)] {
                            for difficulty in [0.5, 1.0, 10.0, 12.0] {
                                for elapsed_secs in [0, 1, 86_400 * 50_000] {
                                    for desired_retention in [None, Some(0.0), Some(0.9999)] {
                                        card.memory_state = Some(FsrsMemoryState {
                                            stability: stability_internal,
                                            stability_internal,
                                            stability_fast,
                                            difficulty,
                                        });
                                        card.last_review_time =
                                            Some(timing.now.adding_secs(-elapsed_secs));
                                        card.desired_retention = desired_retention;
                                        check(&mut col, &card)?;
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        Ok(())
    }

    type DueCardFields = (CardId, NoteId, TimestampSecs, i32, DeckId, DeckId, u32);

    fn due_card_fields(card: &DueCard) -> DueCardFields {
        (
            card.id,
            card.note_id,
            card.mtime,
            card.due,
            card.current_deck_id,
            card.original_deck_id,
            card.reps,
        )
    }

    #[test]
    fn due_card_rows_are_the_due_cards_and_their_rows() -> Result<()> {
        for seed in 0..4 {
            let (mut col, _) = random_collection(seed, false)?;
            let timing = col.timing_today()?;
            // every deck is active
            let root = col.get_or_create_normal_deck("Default")?;
            col.storage.update_active_decks(&root)?;
            col.storage
                .db
                .execute_batch("insert or ignore into active_decks select id from decks")?;
            for kind in [DueCardKind::Review, DueCardKind::Learning] {
                let mut expected = Vec::new();
                col.storage.for_each_due_card_in_active_decks(
                    timing,
                    ReviewCardOrder::Day,
                    kind,
                    true,
                    |card| {
                        expected.push(due_card_fields(&card));
                        Ok(true)
                    },
                )?;
                let mut rows = Vec::new();
                col.storage
                    .for_each_due_card_row_in_active_decks(timing, kind, |card| {
                        rows.push(card);
                        Ok(())
                    })?;
                let mut got: Vec<_> = rows
                    .iter()
                    .map(|card| due_card_fields(&DueCard::from_card(card, kind)))
                    .collect();
                assert!(!expected.is_empty(), "seed {seed}");
                expected.sort();
                got.sort();
                assert_eq!(got, expected, "seed {seed}");
                let ids: Vec<_> = rows.iter().map(|card| card.id).collect();
                let mut loaded = col.all_cards_for_ids(&ids, false)?;
                loaded.sort_by_key(|card| card.id);
                rows.sort_by_key(|card| card.id);
                assert_eq!(rows, loaded, "seed {seed}");
            }
        }
        Ok(())
    }
}
