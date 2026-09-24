// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! (b) The feature encoder of today's published model (92 input features,
//! `rslib/src/rwkv`): turns the event stream into the model's per-review
//! input records, the records Python's `_historical_rwkv_review_inputs`
//! builds, field for field. The model turns a record into its feature
//! vector itself.

use std::collections::HashMap;

use fnv::FnvHashMap;
use sha2::Digest;
use sha2::Sha256;

use super::stream::RwkvReviewEvent;
use crate::card::CardQueue;
use crate::deckconfig::DeckConfig;
use crate::deckconfig::DeckConfigId;
use crate::decks::Deck;
use crate::decks::DeckId;
use crate::scheduler::timing::SchedTimingToday;

const RWKV_HISTORY_HASH_DOMAIN: &[u8] = b"anki-rwkv-state-cache-history-v1\0";

/// The size of one row of the packed warm-up format that `rsbridge` reads
/// (`_PACKED_PREDICTION_REQUEST_ROW`, `<IqqqqBBqqqqqffffB`).
pub(crate) const PACKED_WARM_UP_ROW_BYTES: usize = 95;

/// Where the first review of a card measures its elapsed time from.
pub(crate) enum RwkvFirstReviewElapsed<'a> {
    /// From the card's creation where its home deck's preset says so
    /// (`requested`, the desktop's value per preset, before the preset's
    /// own).
    DeckConfig {
        decks_by_id: &'a HashMap<DeckId, Deck>,
        configs_by_id: &'a HashMap<DeckConfigId, DeckConfig>,
        requested: &'a HashMap<i64, bool>,
    },
    CardCreation,
    Missing,
}

/// One review as today's model reads it: Python's `RwkvReviewInput` of a
/// historical review, with its review id.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct PublishedReviewInput {
    pub(crate) review_id: i64,
    pub(crate) card_id: i64,
    pub(crate) note_id: i64,
    pub(crate) deck_id: i64,
    pub(crate) preset_id: i64,
    pub(crate) ease: i64,
    pub(crate) duration_millis: i64,
    /// The dataset state: 0 on the replay's start row, else the review
    /// log's type plus one.
    pub(crate) card_type: i64,
    pub(crate) review_kind: i64,
    pub(crate) interval_days: i64,
    pub(crate) ease_factor: i64,
    pub(crate) day_offset: i64,
    pub(crate) elapsed_days: i64,
    pub(crate) elapsed_seconds: i64,
}

impl PublishedReviewInput {
    pub(crate) fn card_queue(&self) -> i64 {
        match self.review_kind {
            0 => CardQueue::Learn as i8 as i64,
            2 => CardQueue::DayLearn as i8 as i64,
            _ => CardQueue::Review as i8 as i64,
        }
    }

    pub(crate) fn state_kinds(&self) -> (Option<&'static str>, Option<&'static str>) {
        match self.review_kind {
            0 => (Some("normal"), Some("learning")),
            2 => (Some("normal"), Some("relearning")),
            3 => (Some("filtered"), None),
            _ => (Some("normal"), Some("review")),
        }
    }

    /// Python's `_encode_rwkv_delta_record`: the record the history hash and
    /// the state cache's delta log are made of.
    pub(crate) fn write_delta_record(&self, out: &mut Vec<u8>) {
        write_i64(out, self.review_id);
        write_i64(out, self.card_id);
        write_optional_i64(out, Some(self.note_id));
        write_optional_i64(out, Some(self.deck_id));
        write_optional_i64(out, Some(self.preset_id));
        // is_query
        out.push(0);
        write_optional_i64(out, Some(self.ease));
        write_optional_i64(out, Some(self.duration_millis));
        write_optional_i64(out, Some(self.card_type));
        write_optional_i64(out, Some(self.card_queue()));
        // card_due
        write_optional_i64(out, None);
        write_optional_i64(out, Some(self.interval_days));
        write_optional_i64(out, Some(self.ease_factor));
        // reps, lapses
        write_optional_i64(out, None);
        write_optional_i64(out, None);
        write_optional_i64(out, Some(self.day_offset));
        let (state_kind, normal_state_kind) = self.state_kinds();
        write_optional_string(out, state_kind);
        write_optional_string(out, normal_state_kind);
        write_optional_i64(out, Some(self.elapsed_days));
        write_optional_i64(out, Some(self.elapsed_seconds));
    }

    /// Python's `_packed_review_input_row`: the row `rsbridge` warms the
    /// model up with. No target retentions; the grade order is enforced.
    pub(crate) fn write_packed_row(&self, out: &mut Vec<u8>) {
        // note, deck, preset, ease, duration, card type, day, elapsed days
        // and elapsed seconds are present; the four retentions are not
        const PRESENCE: u32 = 0x1ff;
        out.extend_from_slice(&PRESENCE.to_le_bytes());
        for value in [self.card_id, self.note_id, self.deck_id, self.preset_id] {
            out.extend_from_slice(&value.to_le_bytes());
        }
        // is_query
        out.push(0);
        out.push(self.ease as u8);
        for value in [
            self.duration_millis,
            self.card_type,
            self.day_offset,
            self.elapsed_days,
            self.elapsed_seconds,
        ] {
            out.extend_from_slice(&value.to_le_bytes());
        }
        out.extend_from_slice(&[0; 16]);
        // enforce_grade_order
        out.push(1);
    }
}

/// What the encoder remembers about one card.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct PublishedCardState {
    pub(crate) card_id: i64,
    pub(crate) previous_review_id: i64,
    pub(crate) previous_interval_days: i64,
    pub(crate) review_count: i64,
}

/// Encodes a replay's events in order. The cards it has seen stay in the
/// order it first saw them.
pub(crate) struct PublishedEncoder<'a> {
    timing: SchedTimingToday,
    first_review: RwkvFirstReviewElapsed<'a>,
    index_by_card: FnvHashMap<i64, usize>,
    cards: Vec<PublishedCardState>,
}

impl<'a> PublishedEncoder<'a> {
    pub(crate) fn new(timing: SchedTimingToday, first_review: RwkvFirstReviewElapsed<'a>) -> Self {
        Self {
            timing,
            first_review,
            index_by_card: FnvHashMap::default(),
            cards: Vec::new(),
        }
    }

    pub(crate) fn encode(&mut self, event: &RwkvReviewEvent) -> PublishedReviewInput {
        let day_offset = rwkv_historical_day_offset(event.review_id, &self.timing);
        let cards = &mut self.cards;
        let index = *self.index_by_card.entry(event.card_id).or_insert_with(|| {
            cards.push(PublishedCardState {
                card_id: event.card_id,
                previous_review_id: 0,
                previous_interval_days: 0,
                review_count: 0,
            });
            cards.len() - 1
        });
        let first_review = &self.first_review;
        let card = &mut cards[index];
        let (elapsed_days, elapsed_seconds) = if card.review_count > 0 {
            (
                (day_offset - rwkv_historical_day_offset(card.previous_review_id, &self.timing))
                    .max(0),
                ((event.review_id - card.previous_review_id) / 1000).max(0),
            )
        } else if event.replay_start
            // Only a real Learning start may measure elapsed from the card's
            // creation. A fallback start row (`sched.rwkv-replay-start-row`)
            // is only the first row we hold, not the card's known first
            // review, so its creation age would invent an interval.
            && event.review_kind == 0
            && first_review_uses_card_creation(first_review, event.deck_id)
        {
            let elapsed_seconds = ((event.review_id - event.card_id) / 1000).max(0);
            (elapsed_seconds / 86_400, elapsed_seconds)
        } else {
            (-1, -1)
        };
        card.previous_review_id = event.review_id;
        card.previous_interval_days = event.interval_days;
        card.review_count += 1;
        PublishedReviewInput {
            review_id: event.review_id,
            card_id: event.card_id,
            note_id: event.note_id,
            deck_id: event.deck_id,
            preset_id: event.preset_id,
            ease: event.rating,
            duration_millis: event.duration_millis,
            card_type: if event.replay_start {
                0
            } else {
                event.review_kind + 1
            },
            review_kind: event.review_kind,
            interval_days: event.interval_days,
            ease_factor: event.ease_factor,
            day_offset,
            elapsed_days,
            elapsed_seconds,
        }
    }

    /// Every card seen so far, in the order the encoder first saw them.
    pub(crate) fn cards(&self) -> &[PublishedCardState] {
        &self.cards
    }
}

fn first_review_uses_card_creation(first_review: &RwkvFirstReviewElapsed, deck_id: i64) -> bool {
    match first_review {
        RwkvFirstReviewElapsed::DeckConfig {
            decks_by_id,
            configs_by_id,
            requested,
        } => decks_by_id
            .get(&DeckId(deck_id))
            .and_then(Deck::config_id)
            .is_some_and(|config_id| {
                requested
                    .get(&config_id.0)
                    .copied()
                    .or_else(|| {
                        configs_by_id.get(&config_id).map(|config| {
                            config
                                .inner
                                .rwkv_review_first_review_elapsed_from_card_creation
                        })
                    })
                    .unwrap_or(false)
            }),
        RwkvFirstReviewElapsed::CardCreation => true,
        RwkvFirstReviewElapsed::Missing => false,
    }
}

pub(crate) fn rwkv_historical_day_offset(review_id: i64, timing: &SchedTimingToday) -> i64 {
    let review_secs = review_id / 1000;
    let days_before_today = (timing.next_day_at.0 - 1 - review_secs).max(0) / 86_400;
    (timing.days_elapsed as i64 - days_before_today).max(0)
}

/// The history hash: a SHA-256 chain over the reviews' delta records, the
/// state cache's identity of a history.
#[derive(Clone)]
pub(crate) struct RwkvHistoryHashChain {
    hash: [u8; 32],
    /// one record's scratch space, so that a replay of hundreds of
    /// thousands of reviews allocates one buffer instead of one per review
    record: Vec<u8>,
}

impl RwkvHistoryHashChain {
    pub(crate) fn new() -> Self {
        Self {
            hash: Sha256::digest(RWKV_HISTORY_HASH_DOMAIN).into(),
            record: Vec::with_capacity(256),
        }
    }

    pub(crate) fn update(&mut self, review: &PublishedReviewInput) {
        self.record.clear();
        review.write_delta_record(&mut self.record);
        let mut digest = Sha256::new();
        digest.update(RWKV_HISTORY_HASH_DOMAIN);
        digest.update(self.hash);
        digest.update(&self.record);
        self.hash = digest.finalize().into();
    }

    pub(crate) fn hex(&self) -> String {
        use std::fmt::Write;

        self.hash
            .iter()
            .fold(String::with_capacity(64), |mut output, byte| {
                write!(output, "{byte:02x}").unwrap();
                output
            })
    }
}

fn write_i64(out: &mut Vec<u8>, value: i64) {
    out.extend_from_slice(&value.to_le_bytes());
}

fn write_optional_i64(out: &mut Vec<u8>, value: Option<i64>) {
    match value {
        Some(value) => {
            out.push(1);
            write_i64(out, value);
        }
        None => out.push(0),
    }
}

fn write_optional_string(out: &mut Vec<u8>, value: Option<&str>) {
    match value {
        Some(value) => {
            out.push(1);
            let value = value.as_bytes();
            out.extend_from_slice(&(value.len() as u32).to_le_bytes());
            out.extend_from_slice(value);
        }
        None => out.push(0),
    }
}
