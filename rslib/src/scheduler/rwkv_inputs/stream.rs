// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! (a) The replay's raw event stream: one event per rated review of the
//! replay history (spec sched.rwkv-replay-start-row), in review-id order, as
//! the collection holds it. Nothing a model derives from a review lives here;
//! that is the encoder's work (`super::published`).

use std::collections::hash_map::Entry;
use std::collections::HashMap;
use std::collections::HashSet;

use fnv::FnvHashMap;

use crate::deckconfig::DeckConfig;
use crate::deckconfig::DeckConfigId;
use crate::decks::Deck;
use crate::decks::DeckId;
use crate::prelude::*;
use crate::storage::RwkvHistoricalReviewRow;

/// One rated review of the replay history.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct RwkvReviewEvent {
    /// The review log id: when the answer was given, in milliseconds.
    pub(crate) review_id: i64,
    pub(crate) card_id: i64,
    pub(crate) note_id: i64,
    /// The card's home deck.
    pub(crate) deck_id: i64,
    /// The stable id of the preset the review counts under (see
    /// `RwkvReviewStream::event`).
    pub(crate) preset_id: i64,
    /// The answer button, 1-4.
    pub(crate) rating: i64,
    /// How long the answer took (the review log's `time`), milliseconds.
    pub(crate) duration_millis: i64,
    /// The review log's `type`.
    pub(crate) review_kind: i64,
    pub(crate) interval_days: i64,
    pub(crate) ease_factor: i64,
    /// The row the card's replay starts from: its latest Learning start, or
    /// its first rated row after its last Forget (spec
    /// sched.rwkv-replay-start-row).
    pub(crate) replay_start: bool,
}

impl RwkvReviewEvent {
    /// When the card was shown: the answer time less the time the answer
    /// took. An encoder that must not look at the answer reads this.
    #[allow(dead_code)]
    pub(crate) fn show_millis(&self) -> i64 {
        self.review_id - self.duration_millis
    }
}

/// Where each card's own preset comes from.
pub(crate) enum RwkvStreamPresets<'a> {
    /// No add-on rule moves a card, so every card's preset is its home
    /// deck's, resolved once per deck.
    HomeDeck {
        decks_by_id: &'a HashMap<DeckId, Deck>,
        configs_by_id: &'a HashMap<DeckConfigId, DeckConfig>,
    },
    /// Each card's stable preset id, where it has one.
    ByCard(HashMap<CardId, i64>),
}

/// An add-on simulator rule that routes a review to another preset while
/// the card's review count and previous interval lie in its bounds.
#[derive(Debug)]
pub(crate) struct RwkvHistoricalPresetRoute {
    pub(crate) stable_preset_id: i64,
    pub(crate) card_ids: Option<HashSet<CardId>>,
    pub(crate) min_reps: Option<u32>,
    pub(crate) max_reps: Option<u32>,
    pub(crate) min_interval_days: Option<f32>,
    pub(crate) max_interval_days: Option<f32>,
}

impl RwkvHistoricalPresetRoute {
    fn matches(&self, card_id: CardId, reps: u32, interval_days: i64) -> bool {
        self.card_ids
            .as_ref()
            .map_or(true, |card_ids| card_ids.contains(&card_id))
            && self.min_reps.map_or(true, |minimum| reps >= minimum)
            && self.max_reps.map_or(true, |maximum| reps <= maximum)
            && self
                .min_interval_days
                .map_or(true, |minimum| interval_days as f32 >= minimum)
            && self
                .max_interval_days
                .map_or(true, |maximum| interval_days as f32 <= maximum)
    }
}

/// What the stream remembers about a card: only what its reviews so far
/// say, never the review at hand.
#[derive(Debug, Default)]
struct StreamCard {
    /// The card's own stable preset id, before any route overrides it.
    preset_id: Option<i64>,
    review_count: u32,
    previous_interval_days: i64,
}

/// Turns the replay's rows into events, one row at a time and in order.
pub(crate) struct RwkvReviewStream<'a> {
    presets: RwkvStreamPresets<'a>,
    routes: &'a [RwkvHistoricalPresetRoute],
    cards: FnvHashMap<CardId, StreamCard>,
    home_deck_presets: FnvHashMap<i64, i64>,
}

impl<'a> RwkvReviewStream<'a> {
    pub(crate) fn new(
        presets: RwkvStreamPresets<'a>,
        routes: &'a [RwkvHistoricalPresetRoute],
    ) -> Self {
        Self {
            presets,
            routes,
            cards: FnvHashMap::default(),
            home_deck_presets: FnvHashMap::default(),
        }
    }

    /// The card's own stable preset id, once the stream has seen the card.
    pub(crate) fn card_preset_id(&self, card_id: i64) -> Option<i64> {
        self.cards
            .get(&CardId(card_id))
            .and_then(|card| card.preset_id)
    }

    /// The event of the next row. Its preset is the first route that
    /// matches the card's reviews before this one, else the card's own.
    pub(crate) fn event(&mut self, row: RwkvHistoricalReviewRow) -> Result<RwkvReviewEvent> {
        let card_id = CardId(row.card_id);
        let card = match self.cards.entry(card_id) {
            Entry::Occupied(entry) => entry.into_mut(),
            Entry::Vacant(entry) => {
                let preset_id = match &self.presets {
                    RwkvStreamPresets::ByCard(by_card) => by_card.get(&card_id).copied(),
                    RwkvStreamPresets::HomeDeck {
                        decks_by_id,
                        configs_by_id,
                    } => Some(match self.home_deck_presets.entry(row.deck_id) {
                        Entry::Occupied(deck) => *deck.get(),
                        Entry::Vacant(deck) => *deck.insert(rwkv_home_deck_preset_id(
                            DeckId(row.deck_id),
                            decks_by_id,
                            configs_by_id,
                        )?),
                    }),
                };
                entry.insert(StreamCard {
                    preset_id,
                    ..Default::default()
                })
            }
        };
        let preset_id = self
            .routes
            .iter()
            .find(|route| route.matches(card_id, card.review_count, card.previous_interval_days))
            .map(|route| route.stable_preset_id)
            .or(card.preset_id)
            .or_invalid("missing stable RWKV preset id")?;
        card.review_count += 1;
        card.previous_interval_days = row.interval_days;
        Ok(RwkvReviewEvent {
            review_id: row.review_id,
            card_id: row.card_id,
            note_id: row.note_id,
            deck_id: row.deck_id,
            preset_id,
            rating: row.ease,
            duration_millis: row.duration_millis,
            review_kind: row.review_kind,
            interval_days: row.interval_days,
            ease_factor: row.ease_factor,
            replay_start: row.is_learning_start,
        })
    }
}

/// The stable preset id of a card whose preset is its home deck's, which is
/// every card unless an add-on overlay rule moves it. The lookups match the
/// ones `fsrs_presets_for_cards` makes, so a collection that fails one fails
/// it the same way.
pub(crate) fn rwkv_home_deck_preset_id(
    deck_id: DeckId,
    decks_by_id: &HashMap<DeckId, Deck>,
    configs_by_id: &HashMap<DeckConfigId, DeckConfig>,
) -> Result<i64> {
    let deck = decks_by_id.get(&deck_id).or_not_found(deck_id)?;
    let config_id = deck.config_id().or_invalid("home deck is filtered")?;
    configs_by_id.get(&config_id).or_not_found(config_id)?;
    Ok(config_id.0)
}
