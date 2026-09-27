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
    /// None, as the deck and the preset, for a review of a deleted card
    /// (spec sched.rwkv-replay-deleted-cards).
    pub(crate) note_id: Option<i64>,
    /// The card's home deck.
    pub(crate) deck_id: Option<i64>,
    /// The stable id of the preset the review counts under (see
    /// `RwkvReviewStream::event`).
    pub(crate) preset_id: Option<i64>,
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
    /// took. An encoder that must not look at the answer reads this; the
    /// published encoder measures from answer times, so none does yet.
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
    /// Each card's stable preset id, where it has one. A card the map lacks
    /// has none, and its review fails unless a route gives it one.
    ByCard(HashMap<CardId, i64>),
    /// Each card's stable preset id as the caller resolved it, None where it
    /// resolved none: such a card's review has no preset (the caller read
    /// the rows and the presets itself). A card the map lacks fails, as in
    /// `ByCard`.
    ByCardOrNone(HashMap<CardId, Option<i64>>),
}

/// An add-on simulator rule that routes a review to another preset while
/// the card's review count and previous interval lie in its bounds.
#[derive(Debug)]
pub(crate) struct RwkvHistoricalPresetRoute {
    pub(crate) stable_preset_id: i64,
    pub(crate) card_ids: Option<HashSet<CardId>>,
    pub(crate) min_reps: Option<i64>,
    pub(crate) max_reps: Option<i64>,
    pub(crate) min_interval_days: Option<f64>,
    pub(crate) max_interval_days: Option<f64>,
}

impl RwkvHistoricalPresetRoute {
    /// Compares as Python's `_historical_preset_id_for_review` does: the
    /// review counts as whole numbers, the interval as a float.
    fn matches(&self, card_id: CardId, reps: i64, interval_days: i64) -> bool {
        self.card_ids
            .as_ref()
            .map_or(true, |card_ids| card_ids.contains(&card_id))
            && self.min_reps.map_or(true, |minimum| reps >= minimum)
            && self.max_reps.map_or(true, |maximum| reps <= maximum)
            && self
                .min_interval_days
                .map_or(true, |minimum| interval_days as f64 >= minimum)
            && self
                .max_interval_days
                .map_or(true, |maximum| interval_days as f64 <= maximum)
    }
}

/// A card's own preset, as the stream found it.
#[derive(Debug, Clone, Copy)]
enum StreamPreset {
    Id(i64),
    /// The caller resolved no preset for the card.
    None,
    /// The card has no preset where one is required.
    Missing,
}

/// Turns the replay's rows into events, one row at a time and in order.
pub(crate) struct RwkvReviewStream<'a> {
    presets: RwkvStreamPresets<'a>,
    routes: &'a [RwkvHistoricalPresetRoute],
    /// Each card's own preset, before any route overrides it.
    cards: FnvHashMap<CardId, StreamPreset>,
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
        match self.cards.get(&CardId(card_id)) {
            Some(StreamPreset::Id(preset_id)) => Some(*preset_id),
            _ => None,
        }
    }

    /// The event of the next row. Its preset is the first route that
    /// matches the card's reviews before this one (`review_count` of them,
    /// the last with `previous_interval_days`; 0 and 0 before the first),
    /// else the card's own. A deleted card's review has no preset, as it has
    /// no deck.
    pub(crate) fn event(
        &mut self,
        row: RwkvHistoricalReviewRow,
        review_count: i64,
        previous_interval_days: i64,
    ) -> Result<RwkvReviewEvent> {
        let card_id = CardId(row.card_id);
        let preset_id = match row.deck_id {
            // a card that is gone has no deck, so no preset
            None => None,
            Some(deck_id) => {
                let own = match self.cards.entry(card_id) {
                    Entry::Occupied(entry) => *entry.get(),
                    Entry::Vacant(entry) => *entry.insert(match &self.presets {
                        RwkvStreamPresets::ByCard(by_card) => by_card
                            .get(&card_id)
                            .map_or(StreamPreset::Missing, |id| StreamPreset::Id(*id)),
                        RwkvStreamPresets::ByCardOrNone(by_card) => match by_card.get(&card_id) {
                            Some(Some(id)) => StreamPreset::Id(*id),
                            Some(None) => StreamPreset::None,
                            None => StreamPreset::Missing,
                        },
                        RwkvStreamPresets::HomeDeck {
                            decks_by_id,
                            configs_by_id,
                        } => StreamPreset::Id(match self.home_deck_presets.entry(deck_id) {
                            Entry::Occupied(deck) => *deck.get(),
                            Entry::Vacant(deck) => *deck.insert(rwkv_home_deck_preset_id(
                                DeckId(deck_id),
                                decks_by_id,
                                configs_by_id,
                            )?),
                        }),
                    }),
                };
                let routed = self
                    .routes
                    .iter()
                    .find(|route| route.matches(card_id, review_count, previous_interval_days));
                match (routed, own) {
                    (Some(route), _) => Some(route.stable_preset_id),
                    (None, StreamPreset::Id(id)) => Some(id),
                    (None, StreamPreset::None) => None,
                    (None, StreamPreset::Missing) => {
                        invalid_input!("missing stable RWKV preset id")
                    }
                }
            }
        };
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
/// every card unless an add-on overlay rule moves it. A home deck that is
/// missing or filtered, or whose preset is missing, is an error here, while
/// `fsrs_presets_for_cards` gives FSRS-7 the Default preset for it (spec
/// sched.fsrs7-preset-fallback).
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
