// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Total Knowledge under RWKV (spec ui.stats-total-knowledge): everything the
//! job's day loop in `qt/aqt/total_knowledge.py` reads, built in one call
//! from the replay inputs (`scheduler::rwkv_inputs`): each day's reviews as
//! packed warm-up rows, the searched cards' ratings and resets as the day
//! loop consumes them, and the digests that keep earlier runs' sums. Python
//! built all of this review by review before; the values are the same.

use std::collections::HashMap;
use std::collections::HashSet;

use anki_proto::scheduler::rwkv_historical_review_inputs_request::FirstReviewElapsedSource;
use anki_proto::stats::TotalKnowledgeRwkvReplayRequest;
use anki_proto::stats::TotalKnowledgeRwkvReplayResponse;

use super::blake2b::Blake2b;
use crate::prelude::*;
use crate::scheduler::rwkv::RwkvCollectionHold;
use crate::scheduler::rwkv_inputs::i64_column;
use crate::scheduler::rwkv_inputs::published::PublishedReviewInput;
use crate::scheduler::rwkv_inputs::published::PACKED_WARM_UP_ROW_BYTES;
use crate::scheduler::rwkv_inputs::rwkv_replay_inputs_job_in_parts;
use crate::scheduler::rwkv_inputs::RwkvReplayInputsSettings;

/// What `_history_digest` hashes after each review's record:
/// `repr((review.target_retentions, review.enforce_grade_order))` of a
/// historical review.
const HISTORICAL_REVIEW_OPTIONS_REPR: &[u8] = b"((None, None, None, None), True)";

/// A searched card's rating (the index of its review in the replay) or reset.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct CardEvent {
    review_id: i64,
    day: i64,
    /// None: a reset.
    review_index: Option<usize>,
}

pub(crate) fn total_knowledge_rwkv_replay(
    input: TotalKnowledgeRwkvReplayRequest,
    part_rows: usize,
    hold: &mut RwkvCollectionHold,
) -> Result<TotalKnowledgeRwkvReplayResponse> {
    let started = std::time::Instant::now();
    let (job, resets) =
        rwkv_replay_inputs_job_in_parts(&[], &input.stable_preset_ids, part_rows, hold, |col| {
            col.storage.rwkv_reset_review_ids_and_cards()
        })?;
    let inputs = job.encode(&RwkvReplayInputsSettings {
        first_review_elapsed_source: FirstReviewElapsedSource::DeckConfig,
        first_review_uses_creation_by_config_id: &input.first_review_uses_creation_by_config_id,
        hash_history: false,
        recovery_checkpoint_max_age_millis: 0,
    })?;
    let reviews = inputs.reviews;
    let searched: HashSet<i64> = input
        .card_ids
        .chunks_exact(8)
        .map(|bytes| i64::from_le_bytes(bytes.try_into().unwrap()))
        .collect();
    let events = card_events(&reviews, &resets, &searched, input.today, input.next_day_at);
    let mut response = replay_response(&reviews, &events, input.today, &input.digest_days);
    response.preset_card_ids = i64_column(inputs.cards.iter().map(|card| card.card_id));
    response.card_fsrs_preset_ids = inputs.card_fsrs_preset_ids;
    tracing::debug!(
        reviews = reviews.len(),
        cards = events.len(),
        elapsed_ms = started.elapsed().as_secs_f64() * 1000.0,
        "built Total Knowledge's RWKV replay"
    );
    Ok(response)
}

/// `_card_events`: each searched card's ratings in the replay and its resets,
/// in review-id order, without the resets before its first rating; the
/// cards in the order of their first rating.
fn card_events(
    reviews: &[PublishedReviewInput],
    resets: &[(i64, i64)],
    searched: &HashSet<i64>,
    today: i64,
    next_day_at: i64,
) -> Vec<(i64, Vec<CardEvent>)> {
    let mut events: Vec<(i64, Vec<CardEvent>)> = Vec::new();
    let mut index_by_card: HashMap<i64, usize> = HashMap::new();
    for (review_index, review) in reviews.iter().enumerate() {
        if !searched.contains(&review.card_id) {
            continue;
        }
        let index = *index_by_card.entry(review.card_id).or_insert_with(|| {
            events.push((review.card_id, Vec::new()));
            events.len() - 1
        });
        events[index].1.push(CardEvent {
            review_id: review.review_id,
            day: review.day_offset,
            review_index: Some(review_index),
        });
    }
    for &(review_id, card_id) in resets {
        // a reset matters only after a rating
        if let Some(&index) = index_by_card.get(&card_id) {
            events[index].1.push(CardEvent {
                review_id,
                day: python_day_offset(review_id, today, next_day_at),
                review_index: None,
            });
        }
    }
    for (_, card_events) in &mut events {
        card_events.sort_by_key(|event| event.review_id);
        let first_rating = card_events
            .iter()
            .position(|event| event.review_index.is_some())
            .unwrap_or(card_events.len());
        card_events.drain(..first_rating);
    }
    events.retain(|(_, card_events)| !card_events.is_empty());
    events
}

/// `rwkv._historical_review_day_offset`, with Python's floor division.
fn python_day_offset(review_id: i64, today: i64, next_day_at: i64) -> i64 {
    let review_secs = review_id.div_euclid(1000);
    let days_before_today = (next_day_at - 1 - review_secs).max(0) / 86_400;
    (today - days_before_today).max(0)
}

fn replay_response(
    reviews: &[PublishedReviewInput],
    events: &[(i64, Vec<CardEvent>)],
    today: i64,
    digest_days: &[i64],
) -> TotalKnowledgeRwkvReplayResponse {
    let first_day = reviews
        .first()
        .map(|review| review.day_offset)
        .into_iter()
        .chain(events.iter().map(|(_, card_events)| card_events[0].day))
        .chain([today])
        .min()
        .unwrap();

    // `_changes_by_day`: the day's last event of each card, with the day of
    // the card's next event (the day after today for its last), in the
    // order of the cards; the day loop reads them day by day
    let mut changes: Vec<(i64, i64, i64, i64)> = Vec::new();
    for (card_id, card_events) in events {
        for (position, event) in card_events.iter().enumerate() {
            let until = match card_events.get(position + 1) {
                None => today + 1,
                Some(later) if later.day != event.day => later.day,
                Some(_) => continue,
            };
            let review_index = event.review_index.map_or(-1, |index| index as i64);
            changes.push((event.day, *card_id, review_index, until));
        }
    }
    changes.sort_by_key(|change| change.0);

    let days = first_day..=today;
    let mut review_end = 0;
    let mut change_end = 0;
    let mut day_review_ends = Vec::new();
    let mut day_change_ends = Vec::new();
    for day in days {
        while review_end < reviews.len() && reviews[review_end].day_offset <= day {
            review_end += 1;
        }
        while change_end < changes.len() && changes[change_end].0 <= day {
            change_end += 1;
        }
        day_review_ends.push(review_end as i64);
        day_change_ends.push(change_end as i64);
    }

    let mut packed_rows = Vec::with_capacity(reviews.len() * PACKED_WARM_UP_ROW_BYTES);
    for review in reviews {
        review.write_packed_row(&mut packed_rows);
    }
    TotalKnowledgeRwkvReplayResponse {
        first_day,
        review_count: reviews.len() as u64,
        day_review_ends: i64_column(day_review_ends.into_iter()),
        day_change_ends: i64_column(day_change_ends.into_iter()),
        packed_rows,
        change_card_ids: i64_column(changes.iter().map(|change| change.1)),
        change_review_indexes: i64_column(changes.iter().map(|change| change.2)),
        change_until_days: i64_column(changes.iter().map(|change| change.3)),
        digests: digest_days
            .iter()
            .map(|&day| history_digest(reviews, events, day))
            .collect(),
        ..Default::default()
    }
}

/// `_history_digest`: every review up to `through_day`, then the searched
/// cards' events up to it, by card.
fn history_digest(
    reviews: &[PublishedReviewInput],
    events: &[(i64, Vec<CardEvent>)],
    through_day: i64,
) -> String {
    let mut digest = Blake2b::new(20);
    let mut record = Vec::with_capacity(256);
    for review in reviews {
        if review.day_offset > through_day {
            break;
        }
        record.clear();
        review.write_delta_record(&mut record);
        digest.update(&record);
        digest.update(HISTORICAL_REVIEW_OPTIONS_REPR);
    }
    let mut by_card: Vec<&(i64, Vec<CardEvent>)> = events.iter().collect();
    by_card.sort_by_key(|(card_id, _)| *card_id);
    for (card_id, card_events) in by_card {
        for event in card_events {
            if event.day <= through_day {
                let reset = if event.review_index.is_none() {
                    "True"
                } else {
                    "False"
                };
                digest.update(
                    format!("{card_id}:{}:{}:{reset}", event.review_id, event.day).as_bytes(),
                );
            }
        }
    }
    digest.hex_digest()
}

#[cfg(test)]
mod test {
    use super::*;

    fn review(review_id: i64, card_id: i64, day_offset: i64) -> PublishedReviewInput {
        PublishedReviewInput {
            review_id,
            card_id,
            note_id: 1,
            deck_id: 1,
            preset_id: 1,
            ease: 3,
            duration_millis: 1_000,
            card_type: 2,
            review_kind: 1,
            interval_days: 1,
            ease_factor: 2_500,
            day_offset,
            elapsed_days: 1,
            elapsed_seconds: 86_400,
        }
    }

    #[test]
    fn day_offsets_floor_like_python() {
        // one second before the day starts is the day before
        assert_eq!(python_day_offset(86_399_000, 10, 86_400), 10);
        assert_eq!(python_day_offset(-1, 10, 86_400), 9);
        assert_eq!(python_day_offset(-1, 0, 86_400), 0);
    }

    #[test]
    fn events_drop_resets_before_the_first_rating_and_keep_the_rating_order() {
        let reviews = [
            review(10, 2, 1),
            review(20, 1, 1),
            review(30, 3, 2),
            review(40, 2, 3),
        ];
        let resets = [(5, 2), (25, 1), (35, 4)];
        let searched = HashSet::from([1, 2, 4]);
        let events = card_events(&reviews, &resets, &searched, 5, 1_000_000);
        let cards: Vec<i64> = events.iter().map(|(card_id, _)| *card_id).collect();
        assert_eq!(cards, [2, 1]);
        assert_eq!(
            events[0]
                .1
                .iter()
                .map(|event| event.review_id)
                .collect::<Vec<_>>(),
            [10, 40]
        );
        assert_eq!(
            events[1]
                .1
                .iter()
                .map(|event| event.review_index)
                .collect::<Vec<_>>(),
            [Some(1), None]
        );
    }
}
