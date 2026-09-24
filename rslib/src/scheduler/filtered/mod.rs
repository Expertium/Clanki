// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod card;
mod custom_study;

use std::cmp::Ordering;
use std::collections::HashMap;
use std::hash::Hasher;

use fnv::FnvHasher;

use crate::card::Card;
use crate::collection::RwkvStatsGraphScoreEntry;
use crate::config::ConfigKey;
use crate::config::SchedulerVersion;
use crate::deckconfig::algorithm::SchedulingAlgorithm;
use crate::decks::FilteredDeck;
use crate::decks::FilteredSearchOrder;
use crate::decks::FilteredSearchTerm;
use crate::error::FilteredDeckError;
use crate::prelude::*;
use crate::scheduler::fsrs::memory_state::FsrsCardCurves;
use crate::scheduler::rwkv::relative_overdueness;
use crate::scheduler::timing::SchedTimingToday;
use crate::search::writer::deck_search;
use crate::search::writer::normalize_search;
use crate::search::SortMode;
use crate::storage::card::filtered::order_and_limit_for_search;

/// Contains the parts of a filtered deck required for modifying its settings in
/// the UI.
pub struct FilteredDeckForUpdate {
    pub id: DeckId,
    pub human_name: String,
    pub config: FilteredDeck,
    pub allow_empty: bool,
}

/// The search name under which the filtered-deck preparation publishes the
/// RWKV scores of the deck's own candidate cards. A retrievability order
/// reads only this map (spec sched.filtered-deck-one-algorithm). No search a
/// user types can start with a NUL character.
pub const FILTERED_DECK_RWKV_SCORES_SEARCH: &str = "\u{0}filtered-deck";

pub(crate) struct DeckFilterContext<'a> {
    pub target_deck: DeckId,
    pub config: &'a FilteredDeck,
    pub usn: Usn,
    pub timing: SchedTimingToday,
}

impl Collection {
    /// Get an existing filtered deck, or create a new one if `deck_id` is 0.
    /// The new deck will not be added to the DB.
    pub fn get_or_create_filtered_deck(
        &mut self,
        deck_id: DeckId,
    ) -> Result<FilteredDeckForUpdate> {
        let deck = if deck_id.0 == 0 {
            self.new_filtered_deck_for_adding()?
        } else {
            self.storage.get_deck(deck_id)?.or_not_found(deck_id)?
        };

        deck.try_into()
    }

    /// If the provided `deck_id` is 0, add provided deck to the DB, and rebuild
    /// it. If the searches are invalid or do not match anything, adding is
    /// aborted. If an existing deck is provided, it will be updated.
    /// Invalid searches or an empty match will abort the update.
    /// Returns the deck_id, which will have changed if the id was 0.
    pub fn add_or_update_filtered_deck(
        &mut self,
        deck: FilteredDeckForUpdate,
    ) -> Result<OpOutput<DeckId>> {
        self.transact(Op::BuildFilteredDeck, |col| {
            col.add_or_update_filtered_deck_inner(deck)
        })
    }

    pub fn empty_filtered_deck(&mut self, did: DeckId) -> Result<OpOutput<()>> {
        self.transact(Op::EmptyFilteredDeck, |col| {
            let deck = col.get_deck(did)?.or_not_found(did)?;
            col.return_all_cards_in_filtered_deck(&deck)
        })
    }

    // Unlike the old Python code, this also marks the cards as modified.
    pub fn rebuild_filtered_deck(&mut self, did: DeckId) -> Result<OpOutput<usize>> {
        self.transact(Op::RebuildFilteredDeck, |col| {
            let deck = col.get_deck(did)?.or_not_found(did)?;
            col.rebuild_filtered_deck_inner(&deck, col.usn()?)
        })
    }
}

impl Collection {
    pub(crate) fn return_all_cards_in_filtered_deck(&mut self, deck: &Deck) -> Result<()> {
        if !deck.is_filtered() {
            return Err(FilteredDeckError::FilteredDeckRequired.into());
        }
        let cids = self.storage.all_cards_in_single_deck(deck.id)?;
        self.return_cards_to_home_deck(&cids)
    }

    // Unlike the old Python code, this also marks the cards as modified.
    fn return_cards_to_home_deck(&mut self, cids: &[CardId]) -> Result<()> {
        let usn = self.usn()?;
        for cid in cids {
            if let Some(mut card) = self.storage.get_card(*cid)? {
                let original = card.clone();
                card.remove_from_filtered_deck_restoring_queue();
                self.update_card_inner(&mut card, original, usn)?;
            }
        }
        Ok(())
    }

    fn build_filtered_deck(&mut self, ctx: DeckFilterContext) -> Result<usize> {
        let start = -100_000;
        let mut position = start;
        let fsrs = self.get_config_bool(BoolKey::Fsrs);
        let algorithm = self.effective_scheduling_algorithm()?;
        for term in ctx.config.search_terms.iter().take(2) {
            position = self.move_cards_matching_term(&ctx, term, position, fsrs, algorithm)?;
        }

        Ok((position - start) as usize)
    }

    /// Move matching cards into filtered deck.
    /// Returns the new starting position.
    fn move_cards_matching_term(
        &mut self,
        ctx: &DeckFilterContext,
        term: &FilteredSearchTerm,
        mut position: i32,
        fsrs: bool,
        algorithm: SchedulingAlgorithm,
    ) -> Result<i32> {
        let search = format!(
            "{} -is:suspended -is:buried -deck:filtered",
            if term.search.trim().is_empty() {
                "".to_string()
            } else {
                format!("({})", term.search)
            }
        );

        // under RWKV a retrievability or relative-overdueness order is
        // RWKV's own, never the SQL order's FSRS or SM-2 formula (spec
        // sched.filtered-deck-one-algorithm)
        let rwkv = algorithm != SchedulingAlgorithm::Fsrs7;
        if let Some(order) = exact_fsrs_search_order(term.order()).filter(|_| fsrs || rwkv) {
            return self.move_cards_matching_term_with_exact_fsrs_order(
                ctx, term, &search, position, order, algorithm,
            );
        }

        let order = order_and_limit_for_search(term, ctx.timing, fsrs);

        for mut card in self.all_cards_for_search_in_order(&search, SortMode::Custom(order))? {
            let original = card.clone();
            card.move_into_filtered_deck(ctx, position);
            self.update_card_inner(&mut card, original, ctx.usn)?;
            position += 1;
        }

        Ok(position)
    }

    fn move_cards_matching_term_with_exact_fsrs_order(
        &mut self,
        ctx: &DeckFilterContext,
        term: &FilteredSearchTerm,
        search: &str,
        mut position: i32,
        order: ExactFsrsSearchOrder,
        algorithm: SchedulingAlgorithm,
    ) -> Result<i32> {
        let mut cards_with_keys = Vec::new();
        let mut curves = FsrsCardCurves::default();
        let rwkv_scores = self
            .rwkv_stats_graph_score_entries_for_search(
                ctx.timing.days_elapsed,
                Some(FILTERED_DECK_RWKV_SCORES_SEARCH),
            )
            .unwrap_or_default();
        let keys = FilteredOrderKeys {
            algorithm,
            rwkv_scores: &rwkv_scores,
        };
        let cards = self.all_cards_for_search(search)?;
        if algorithm == SchedulingAlgorithm::Fsrs7
            || matches!(order, ExactFsrsSearchOrder::RelativeOverdueness)
        {
            // the keys read the cards' presets: resolve their add-on overlay
            // presets with one search per rule instead of one per card
            let card_refs: Vec<&Card> = cards.iter().collect();
            self.resolve_fsrs_overlay_presets_for_cards(&card_refs)?;
        }
        for card in cards {
            let key =
                exact_fsrs_search_key_for_card(self, &card, ctx.timing, order, &mut curves, &keys)?;
            let hash = fnvhash_card_and_mod(&card);
            cards_with_keys.push((card, key, hash));
        }

        // a card without a value from the collection's algorithm goes to the
        // end, whichever the direction
        cards_with_keys.sort_unstable_by(|(card_a, key_a, hash_a), (card_b, key_b, hash_b)| {
            let ord = match (key_a, key_b) {
                (Some(a), Some(b)) => {
                    let ord = a.partial_cmp(b).unwrap_or(Ordering::Equal);
                    if order.reverse() {
                        ord.reverse()
                    } else {
                        ord
                    }
                }
                (Some(_), None) => Ordering::Less,
                (None, Some(_)) => Ordering::Greater,
                (None, None) => Ordering::Equal,
            };
            ord.then_with(|| hash_a.cmp(hash_b))
                .then_with(|| card_a.id.cmp(&card_b.id))
        });

        for (mut card, _, _) in cards_with_keys.into_iter().take(term.limit as usize) {
            let original = card.clone();
            card.move_into_filtered_deck(ctx, position);
            self.update_card_inner(&mut card, original, ctx.usn)?;
            position += 1;
        }

        Ok(position)
    }

    fn get_next_filtered_deck_name(&self) -> NativeDeckName {
        NativeDeckName::from_native_str(format!(
            "Filtered Deck {}",
            TimestampSecs::now().time_string()
        ))
    }

    fn add_or_update_filtered_deck_inner(
        &mut self,
        mut update: FilteredDeckForUpdate,
    ) -> Result<DeckId> {
        let usn = self.usn()?;
        let allow_empty = update.allow_empty;

        // check the searches are valid, and normalize them
        for term in &mut update.config.search_terms {
            term.search = normalize_search(&term.search)?
        }

        // add or update the deck
        let mut deck: Deck;
        if update.id.0 == 0 {
            deck = Deck::new_filtered();
            apply_update_to_filtered_deck(&mut deck, update);
            self.add_deck_inner(&mut deck, usn)?;
        } else {
            let original = self.storage.get_deck(update.id)?.or_not_found(update.id)?;
            deck = original.clone();
            apply_update_to_filtered_deck(&mut deck, update);
            self.update_deck_inner(&mut deck, original, usn)?;
        }

        // rebuild it
        let count = self.rebuild_filtered_deck_inner(&deck, usn)?;

        // if it failed to match any cards, we revert the changes
        if count == 0 && !allow_empty {
            Err(FilteredDeckError::SearchReturnedNoCards.into())
        } else {
            // update current deck and return id
            self.set_config(ConfigKey::CurrentDeckId, &deck.id)?;
            Ok(deck.id)
        }
    }

    fn rebuild_filtered_deck_inner(&mut self, deck: &Deck, usn: Usn) -> Result<usize> {
        if self.scheduler_version() == SchedulerVersion::V1 {
            return Err(AnkiError::SchedulerUpgradeRequired);
        }

        let config = deck.filtered()?;
        let timing = self.timing_today()?;
        let ctx = DeckFilterContext {
            target_deck: deck.id,
            config,
            usn,
            timing,
        };

        self.return_all_cards_in_filtered_deck(deck)?;
        self.build_filtered_deck(ctx)
    }

    fn new_filtered_deck_for_adding(&mut self) -> Result<Deck> {
        let mut deck = Deck {
            name: self.get_next_filtered_deck_name(),
            ..Deck::new_filtered()
        };
        if let Some(current) = self.get_deck(self.get_current_deck_id())? {
            if !current.is_filtered() && current.id.0 != 0 {
                // start with a search based on the selected deck name
                let search = deck_search(&current.human_name());
                let term1 = deck
                    .filtered_mut()
                    .unwrap()
                    .search_terms
                    .get_mut(0)
                    .unwrap();
                term1.search = format!("{search} is:due");
                let term2 = deck
                    .filtered_mut()
                    .unwrap()
                    .search_terms
                    .get_mut(1)
                    .unwrap();
                term2.search = format!("{search} is:new");
            }
        }

        Ok(deck)
    }
}

impl TryFrom<Deck> for FilteredDeckForUpdate {
    type Error = AnkiError;

    fn try_from(value: Deck) -> Result<Self, Self::Error> {
        let human_name = value.human_name();
        match value.kind {
            DeckKind::Filtered(filtered) => Ok(FilteredDeckForUpdate {
                id: value.id,
                human_name,
                config: filtered,
                allow_empty: false,
            }),
            _ => invalid_input!("not filtered"),
        }
    }
}

fn apply_update_to_filtered_deck(deck: &mut Deck, update: FilteredDeckForUpdate) {
    deck.id = update.id;
    deck.name = NativeDeckName::from_human_name(&update.human_name);
    deck.kind = DeckKind::Filtered(update.config);
}

#[derive(Clone, Copy)]
enum ExactFsrsSearchOrder {
    Retrievability { reverse: bool },
    RelativeOverdueness,
}

impl ExactFsrsSearchOrder {
    fn reverse(self) -> bool {
        matches!(self, Self::Retrievability { reverse: true })
    }
}

fn exact_fsrs_search_order(order: FilteredSearchOrder) -> Option<ExactFsrsSearchOrder> {
    match order {
        FilteredSearchOrder::RetrievabilityAscending => {
            Some(ExactFsrsSearchOrder::Retrievability { reverse: false })
        }
        FilteredSearchOrder::RetrievabilityDescending => {
            Some(ExactFsrsSearchOrder::Retrievability { reverse: true })
        }
        FilteredSearchOrder::RelativeOverdueness => Some(ExactFsrsSearchOrder::RelativeOverdueness),
        _ => None,
    }
}

fn fnvhash_card_and_mod(card: &Card) -> i64 {
    let mut hasher = FnvHasher::default();
    hasher.write_i64(card.id.0);
    hasher.write_i64(card.mtime.0);
    hasher.finish() as i64
}

/// What a filtered deck's retrievability order reads: the collection's
/// algorithm, and the RWKV scores the filtered-deck preparation published
/// for the deck's own cards (`FILTERED_DECK_RWKV_SCORES_SEARCH`).
struct FilteredOrderKeys<'a> {
    algorithm: SchedulingAlgorithm,
    rwkv_scores: &'a HashMap<CardId, RwkvStatsGraphScoreEntry>,
}

/// The card's retrievability under the collection's algorithm only (spec
/// sched.filtered-deck-one-algorithm): FSRS-7's (SM-2's relative overdueness
/// for a card without a memory state, as upstream); under RWKV-Curve the
/// card's stored curve now, under RWKV-Instant its rating head, both from
/// the map the preparation published for this deck. None when the
/// algorithm has no value for the card; no other algorithm's value stands in.
fn exact_retrievability_key_for_card(
    col: &mut Collection,
    card: &Card,
    timing: SchedTimingToday,
    curves: &mut FsrsCardCurves,
    keys: &FilteredOrderKeys,
) -> Result<Option<f32>> {
    let entry = keys.rwkv_scores.get(&card.id);
    match keys.algorithm {
        SchedulingAlgorithm::RwkvCurve => Ok(entry.and_then(|entry| entry.curve_retrievability)),
        SchedulingAlgorithm::RwkvInstant => Ok(entry.and_then(|entry| entry.retrievability)),
        SchedulingAlgorithm::Fsrs7 => {
            if let Some(state) = card.memory_state {
                let elapsed_days = card.seconds_since_last_review(&timing) as f32 / 86_400.0;
                curves
                    .current_retrievability(col, card, state, elapsed_days)
                    .map(Some)
            } else {
                Ok(Some(sm2_relative_overdueness_key(card, timing)))
            }
        }
    }
}

/// The card's key in an exact filtered-deck order; `curves` keeps the
/// presets and models of the cards seen before.
fn exact_fsrs_search_key_for_card(
    col: &mut Collection,
    card: &Card,
    timing: SchedTimingToday,
    order: ExactFsrsSearchOrder,
    curves: &mut FsrsCardCurves,
    keys: &FilteredOrderKeys,
) -> Result<Option<f32>> {
    match order {
        ExactFsrsSearchOrder::Retrievability { .. } => {
            exact_retrievability_key_for_card(col, card, timing, curves, keys)
        }
        ExactFsrsSearchOrder::RelativeOverdueness => match keys.algorithm {
            SchedulingAlgorithm::Fsrs7 => {
                if let Some(state) = card.memory_state {
                    let elapsed_days = card.seconds_since_last_review(&timing) as f32 / 86_400.0;
                    curves
                        .relative_overdueness(col, card, state, elapsed_days)
                        .map(Some)
                } else {
                    Ok(Some(sm2_relative_overdueness_key(card, timing)))
                }
            }
            // under RWKV, RWKV's own retrievability over the desired
            // retention, as its review order ranks it; never FSRS-7's memory
            // state (spec sched.filtered-deck-one-algorithm)
            SchedulingAlgorithm::RwkvCurve | SchedulingAlgorithm::RwkvInstant => {
                let Some(retrievability) =
                    exact_retrievability_key_for_card(col, card, timing, curves, keys)?
                else {
                    return Ok(None);
                };
                let target = curves.desired_retention(col, card)?;
                Ok(Some(relative_overdueness(retrievability, target)))
            }
        },
    }
}

fn sm2_relative_overdueness_key(card: &Card, timing: SchedTimingToday) -> f32 {
    let due = card.original_or_current_due() as i64;
    let review_day = due.saturating_sub(card.interval as i64);
    let days_elapsed = if due > 365_000 {
        (timing.next_day_at.0 as u32).saturating_sub(due as u32) / 86_400
    } else {
        timing.days_elapsed.saturating_sub(review_day as u32)
    };
    -((days_elapsed as f32) + 0.001) / (card.interval as f32).max(1.0)
}

#[cfg(test)]
mod test {
    use std::collections::HashMap;

    use anki_proto::deck_config::deck_configs_for_update::current_deck::Limits;
    use anki_proto::deck_config::UpdateDeckConfigsMode;

    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::card::FsrsMemoryState;
    use crate::config::BoolKey;
    use crate::deckconfig::FsrsVersion;
    use crate::deckconfig::UpdateDeckConfigsRequest;
    use crate::decks::FilteredSearchOrder;

    fn fsrs7_keys(scores: &HashMap<CardId, RwkvStatsGraphScoreEntry>) -> FilteredOrderKeys<'_> {
        FilteredOrderKeys {
            algorithm: SchedulingAlgorithm::Fsrs7,
            rwkv_scores: scores,
        }
    }

    fn set_selected_fsrs7_params(col: &mut Collection, params: Vec<f32>) -> Result<()> {
        let output = col.get_deck_configs_for_update(DeckId(1))?;
        let mut input = UpdateDeckConfigsRequest {
            target_deck_id: DeckId(1),
            configs: output
                .all_config
                .into_iter()
                .map(|c| c.config.unwrap().into())
                .collect(),
            removed_config_ids: vec![],
            mode: UpdateDeckConfigsMode::Normal,
            limits: Limits::default(),
            new_cards_ignore_review_limit: false,
            fsrs: true,
            load_balancer_enabled: false,
            fsrs_short_term_with_steps_enabled: false,
            review_fuzz_config: Default::default(),
        };
        input.configs[0].inner.fsrs_version = FsrsVersion::Seven as i32;
        input.configs[0].inner.fsrs_params_7 = params;
        col.update_deck_configs(input)?;
        Ok(())
    }

    /// One cache for all the cards of a build gives each card the key of the
    /// fsrs crate's tensor model, bit for bit, whatever its preset.
    #[test]
    fn kept_curves_give_the_tensor_path_keys() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let timing = col.timing_today()?;
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut cards = Vec::new();
        for index in 0..3u8 {
            let mut deck = col.get_or_create_normal_deck(&format!("D{index}"))?;
            let mut config = DeckConfig::default();
            config.inner.fsrs_params_7 = fsrs::DEFAULT_PARAMETERS
                .iter()
                .map(|w| w * (0.9 + 0.1 * index as f32))
                .collect();
            config.inner.desired_retention = 0.8 + 0.05 * index as f32;
            col.add_or_update_deck_config(&mut config)?;
            deck.normal_mut()?.config_id = config.id.0;
            col.add_or_update_deck(&mut deck)?;
            for n in 0..4u8 {
                let mut note = nt.new_note();
                col.add_note(&mut note, deck.id)?;
                let mut card = col.storage.get_card_by_ordinal(note.id, 0)?.unwrap();
                card.ctype = CardType::Review;
                card.queue = CardQueue::Review;
                card.interval = 10;
                card.memory_state = Some(FsrsMemoryState {
                    stability: 3.0 * (n + 1) as f32,
                    stability_internal: 2.0 * (n + 1) as f32,
                    stability_fast: (n % 2 == 0).then_some(n as f32 + 0.5),
                    difficulty: 2.0 + 2.0 * n as f32,
                });
                card.desired_retention = (n == 3).then_some(0.93);
                card.last_review_time =
                    Some(timing.now.adding_secs(-((n + index) as i64 + 1) * 86_400));
                col.storage.update_card(&card)?;
                cards.push(card);
            }
        }
        for order in [
            ExactFsrsSearchOrder::Retrievability { reverse: false },
            ExactFsrsSearchOrder::RelativeOverdueness,
        ] {
            let mut curves = FsrsCardCurves::default();
            let no_scores = HashMap::new();
            let keys = fsrs7_keys(&no_scores);
            for card in &cards {
                let key = exact_fsrs_search_key_for_card(
                    &mut col,
                    card,
                    timing,
                    order,
                    &mut curves,
                    &keys,
                )?
                .unwrap();
                let preset = col.fsrs_preset_for_card(card)?;
                let fsrs = fsrs::FSRS::new(&preset.params)?;
                let state = card.memory_state.unwrap().into();
                let elapsed = card.seconds_since_last_review(&timing) as f32 / 86_400.0;
                let expected = match order {
                    ExactFsrsSearchOrder::Retrievability { .. } => {
                        fsrs.current_retrievability(state, elapsed.max(0.0))
                    }
                    ExactFsrsSearchOrder::RelativeOverdueness => {
                        let target = card
                            .desired_retention
                            .unwrap_or(preset.desired_retention)
                            .clamp(0.0001, 0.9999);
                        -elapsed.max(0.0)
                            / fsrs.interval_at_retrievability(state, target).max(0.0001)
                    }
                };
                assert_eq!(key.to_bits(), expected.to_bits(), "{card:?}");
            }
        }
        Ok(())
    }

    #[test]
    fn filtered_deck_retrievability_order_uses_exact_model() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        set_selected_fsrs7_params(
            &mut col,
            vec![
                0.4843, 3.0562, 10.9946, 32.7202, 5.6296, 0.5900, 3.1230, 2.4679, 0.2733, 1.4895,
                0.4868, 0.0010, 0.8082, 0.1723, 0.6389, 1.5767, 0.8918, 0.3341, 3.5942, 0.3455,
                0.0022, 0.2834, 2.6418, 0.5604, 1.3042, 2.5054, 0.9376, 0.0611, 0.0830, 0.6339,
                0.9846, 0.2485, 0.6014, 0.0545,
            ],
        )?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note1 = nt.new_note();
        let mut note2 = nt.new_note();
        col.add_note(&mut note1, DeckId(1))?;
        col.add_note(&mut note2, DeckId(1))?;
        let mut ids = col.search_cards("", SortMode::NoOrder)?;
        ids.sort();

        let timing = col.timing_today()?;
        let mut card1 = col.storage.get_card(ids[0])?.unwrap();
        let mut card2 = col.storage.get_card(ids[1])?.unwrap();
        for card in [&mut card1, &mut card2] {
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.due = 100;
            card.interval = 20;
            card.memory_state = Some(FsrsMemoryState {
                stability: 10.0,
                stability_internal: 10.0,
                stability_fast: Some(20.0),
                difficulty: 5.0,
            });
            card.desired_retention = Some(0.8);
            card.last_review_time = Some(timing.now.adding_secs(-20 * 86_400));
        }
        card2.memory_state = Some(FsrsMemoryState {
            stability: 10.0,
            stability_internal: 10.0,
            stability_fast: Some(5.0),
            difficulty: 8.0,
        });
        card1.decay = Some(0.1);
        card2.decay = Some(2.0);
        col.storage.update_card(&card1)?;
        col.storage.update_card(&card2)?;
        let no_scores = HashMap::new();
        let key1 = exact_retrievability_key_for_card(
            &mut col,
            &card1,
            timing,
            &mut FsrsCardCurves::default(),
            &fsrs7_keys(&no_scores),
        )?
        .unwrap();
        let key2 = exact_retrievability_key_for_card(
            &mut col,
            &card2,
            timing,
            &mut FsrsCardCurves::default(),
            &fsrs7_keys(&no_scores),
        )?
        .unwrap();
        assert_ne!(key1, key2);

        let mut deck = col.get_or_create_filtered_deck(DeckId(0))?;
        deck.allow_empty = true;
        deck.config.search_terms[0].search = "is:review".into();
        deck.config.search_terms[0].limit = 2;
        deck.config.search_terms[0].order = FilteredSearchOrder::RetrievabilityAscending as i32;
        deck.config.search_terms[1].search = String::new();
        deck.config.search_terms[1].limit = 0;
        let filtered_did = col.add_or_update_filtered_deck(deck)?.output;

        let mut filtered_cards = col
            .storage
            .all_cards_in_single_deck(filtered_did)?
            .into_iter()
            .map(|cid| {
                let card = col.storage.get_card(cid)?.or_not_found(cid)?;
                Ok(card)
            })
            .collect::<Result<Vec<_>>>()?;
        filtered_cards.sort_by_key(|card| card.due);
        let ordered_ids: Vec<_> = filtered_cards.into_iter().map(|card| card.id).collect();

        let mut expected_order = vec![
            (card1.id, key1, fnvhash_card_and_mod(&card1)),
            (card2.id, key2, fnvhash_card_and_mod(&card2)),
        ];
        expected_order.sort_unstable_by(|(id_a, key_a, hash_a), (id_b, key_b, hash_b)| {
            key_a
                .partial_cmp(key_b)
                .unwrap_or(Ordering::Equal)
                .then_with(|| hash_a.cmp(hash_b))
                .then_with(|| id_a.cmp(id_b))
        });
        let expected_ids: Vec<_> = expected_order.into_iter().map(|(id, _, _)| id).collect();
        assert_eq!(ordered_ids, expected_ids);
        Ok(())
    }

    // With add-on preset overlay rules, an exact filtered-deck order resolves
    // its cards' overlay presets with one search per rule, not one per card.
    #[test]
    fn filtered_deck_exact_order_resolves_overlay_presets_per_rule() -> Result<()> {
        use crate::scheduler::fsrs::preset::AddonFsrsPreset;
        use crate::scheduler::fsrs::preset::AddonFsrsVersion;
        use crate::scheduler::fsrs::preset::FsrsPresetOverlay;
        use crate::scheduler::fsrs::preset::FsrsPresetRule;
        use crate::scheduler::fsrs::preset::FSRS_PRESET_OVERLAY_CONFIG_KEY;
        use crate::scheduler::fsrs::preset::PER_CARD_OVERLAY_SEARCHES;

        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let timing = col.timing_today()?;
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        for index in 0..8 {
            let mut note = nt.new_note();
            note.set_field(0, format!("front {index}"))?;
            col.add_note(&mut note, DeckId(1))?;
            if index % 2 == 0 {
                col.add_tags_to_notes(&[note.id], "overlay")?;
            }
            let mut card = col.storage.get_card_by_ordinal(note.id, 0)?.unwrap();
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.due = 0;
            card.interval = 5;
            card.memory_state = Some(FsrsMemoryState {
                stability: 5.0 + index as f32,
                stability_internal: 5.0 + index as f32,
                stability_fast: None,
                difficulty: 5.0,
            });
            card.last_review_time = Some(timing.now.adding_secs(-5 * 86_400));
            col.storage.update_card(&card)?;
        }
        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:overlay".into(),
                    name: "Overlay".into(),
                    fsrs_version: AddonFsrsVersion::Seven,
                    params: Vec::new(),
                    desired_retention: 0.8,
                    historical_retention: 0.9,
                    ignore_revlogs_before_date: String::new(),
                }],
                rules: vec![FsrsPresetRule {
                    search: "tag:overlay".into(),
                    preset_id: "addon:test:overlay".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;

        for order in [
            FilteredSearchOrder::RetrievabilityAscending,
            FilteredSearchOrder::RelativeOverdueness,
        ] {
            col.state.fsrs_preset_overlay_cache = None;
            PER_CARD_OVERLAY_SEARCHES.with(|count| count.set(0));
            let mut deck = col.get_or_create_filtered_deck(DeckId(0))?;
            deck.allow_empty = true;
            deck.config.search_terms[0].search = "is:review".into();
            deck.config.search_terms[0].limit = 100;
            deck.config.search_terms[0].order = order as i32;
            deck.config.search_terms[1].search = String::new();
            deck.config.search_terms[1].limit = 0;
            let filtered_did = col.add_or_update_filtered_deck(deck)?.output;
            assert_eq!(
                col.storage.all_cards_in_single_deck(filtered_did)?.len(),
                8,
                "{order:?}"
            );
            assert_eq!(
                PER_CARD_OVERLAY_SEARCHES.with(|count| count.get()),
                0,
                "{order:?}"
            );
            col.remove_decks_and_child_decks(&[filtered_did])?;
        }
        Ok(())
    }

    #[test]
    fn filtered_relative_overdueness_uses_exact_target_interval() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let timing = col.timing_today()?;
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;
        let mut card = col.storage.get_card_by_ordinal(note.id, 0)?.unwrap();
        card.memory_state = Some(FsrsMemoryState {
            stability: 10.0,
            stability_internal: 10.0,
            stability_fast: Some(5.0),
            difficulty: 8.0,
        });
        card.last_review_time = Some(timing.now.adding_secs(-20 * 86_400));

        // one cache for both: the card's own desired retention is not kept
        let mut curves = FsrsCardCurves::default();
        let no_scores = HashMap::new();
        card.desired_retention = Some(0.8);
        let first = exact_fsrs_search_key_for_card(
            &mut col,
            &card,
            timing,
            ExactFsrsSearchOrder::RelativeOverdueness,
            &mut curves,
            &fsrs7_keys(&no_scores),
        )?;
        card.desired_retention = Some(0.95);
        let second = exact_fsrs_search_key_for_card(
            &mut col,
            &card,
            timing,
            ExactFsrsSearchOrder::RelativeOverdueness,
            &mut curves,
            &fsrs7_keys(&no_scores),
        )?;

        assert_ne!(first, second);
        assert!(matches!(
            exact_fsrs_search_order(FilteredSearchOrder::RelativeOverdueness),
            Some(ExactFsrsSearchOrder::RelativeOverdueness)
        ));
        Ok(())
    }

    fn rwkv_entry(rating_head: Option<f32>, curve: Option<f32>) -> RwkvStatsGraphScoreEntry {
        RwkvStatsGraphScoreEntry {
            retrievability: rating_head,
            curve_retrievability: curve,
            intervening_reviews: None,
            target_retention: None,
            curve_due: false,
        }
    }

    fn filtered_order(col: &mut Collection, order: FilteredSearchOrder) -> Result<Vec<CardId>> {
        let mut deck = col.get_or_create_filtered_deck(DeckId(0))?;
        deck.allow_empty = true;
        deck.config.search_terms[0].search = "is:review".into();
        deck.config.search_terms[0].limit = 10;
        deck.config.search_terms[0].order = order as i32;
        deck.config.search_terms[1].search = String::new();
        deck.config.search_terms[1].limit = 0;
        let filtered_did = col.add_or_update_filtered_deck(deck)?.output;
        let mut cards = col
            .storage
            .all_cards_in_single_deck(filtered_did)?
            .into_iter()
            .map(|cid| col.storage.get_card(cid)?.or_not_found(cid))
            .collect::<Result<Vec<_>>>()?;
        cards.sort_by_key(|card| card.due);
        let ids = cards.iter().map(|card| card.id).collect();
        col.empty_filtered_deck(filtered_did)?;
        col.remove_decks_and_child_decks(&[filtered_did])?;
        Ok(ids)
    }

    /// Pins spec sched.filtered-deck-one-algorithm: under RWKV, "Relative
    /// overdueness" is RWKV's retrievability over the desired retention, as
    /// RWKV's review order ranks it; FSRS-7's memory state plays no part.
    #[test]
    fn filtered_deck_relative_overdueness_under_rwkv_is_rwkvs_own() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let timing = col.timing_today()?;
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        // FSRS-7's memory states alone would order them a, b (a waited longer)
        let mut ids = Vec::new();
        for (days_since_review, desired_retention) in [(40, None), (5, Some(0.5)), (20, None)] {
            let mut note = nt.new_note();
            col.add_note(&mut note, DeckId(1))?;
            let mut card = col.storage.get_card_by_ordinal(note.id, 0)?.unwrap();
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.due = 100;
            card.interval = 10;
            card.memory_state = Some(FsrsMemoryState {
                stability: 10.0,
                stability_internal: 10.0,
                stability_fast: None,
                difficulty: 5.0,
            });
            card.desired_retention = desired_retention;
            card.last_review_time = Some(timing.now.adding_secs(-days_since_review * 86_400));
            col.storage.update_card(&card)?;
            ids.push(card.id);
        }
        let (a, b, c) = (ids[0], ids[1], ids[2]);
        // b has the lower curve value (0.45 < 0.6) but also a desired
        // retention of 0.5, so it is less overdue: 0.45/0.5 > 0.6/0.9; c has
        // no RWKV value and goes last
        col.set_rwkv_stats_graph_score_entries(
            FILTERED_DECK_RWKV_SCORES_SEARCH.to_string(),
            HashMap::from([
                (a, rwkv_entry(Some(0.9), Some(0.6))),
                (b, rwkv_entry(Some(0.3), Some(0.45))),
            ]),
        )?;
        col.set_config(
            ConfigKey::SchedulingAlgorithm,
            &SchedulingAlgorithm::RwkvCurve,
        )?;
        assert_eq!(
            filtered_order(&mut col, FilteredSearchOrder::RetrievabilityAscending)?,
            vec![b, a, c]
        );
        assert_eq!(
            filtered_order(&mut col, FilteredSearchOrder::RelativeOverdueness)?,
            vec![a, b, c]
        );
        // under RWKV-Instant the rating head: 0.9/0.9 vs 0.3/0.5
        col.set_config(
            ConfigKey::SchedulingAlgorithm,
            &SchedulingAlgorithm::RwkvInstant,
        )?;
        assert_eq!(
            filtered_order(&mut col, FilteredSearchOrder::RelativeOverdueness)?,
            vec![b, a, c]
        );
        Ok(())
    }

    /// Pins spec sched.filtered-deck-one-algorithm: a filtered deck's
    /// retrievability order never uses another algorithm's value. Under
    /// RWKV-Curve it is the curve value, under RWKV-Instant the rating head,
    /// both only from the map published for the deck's own cards; a card with
    /// no value from its algorithm goes to the end in both directions; FSRS-7
    /// reads no RWKV value at all.
    #[test]
    fn filtered_deck_retrievability_order_uses_only_the_collections_algorithm() -> Result<()> {
        let mut col = Collection::new();
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        let timing = col.timing_today()?;
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        // FSRS-7 alone would order them c, a, b (c waited longest)
        let mut ids = Vec::new();
        for days_since_review in [20, 5, 40] {
            let mut note = nt.new_note();
            col.add_note(&mut note, DeckId(1))?;
            let mut card = col.storage.get_card_by_ordinal(note.id, 0)?.unwrap();
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            // a positive due: the build writes the position there
            card.due = 100;
            card.interval = 10;
            card.memory_state = Some(FsrsMemoryState {
                stability: 10.0,
                stability_internal: 10.0,
                stability_fast: None,
                difficulty: 5.0,
            });
            card.last_review_time = Some(timing.now.adding_secs(-days_since_review * 86_400));
            col.storage.update_card(&card)?;
            ids.push(card.id);
        }
        let (a, b, c) = (ids[0], ids[1], ids[2]);

        // the deck's own map: c has no value from either RWKV head
        col.set_rwkv_stats_graph_score_entries(
            FILTERED_DECK_RWKV_SCORES_SEARCH.to_string(),
            HashMap::from([
                (a, rwkv_entry(Some(0.2), Some(0.9))),
                (b, rwkv_entry(Some(0.8), Some(0.3))),
            ]),
        )?;
        // values of other places, published later, must not be borrowed
        col.set_rwkv_stats_graph_score_entries(
            "deck:current".to_string(),
            HashMap::from([(c, rwkv_entry(Some(0.01), Some(0.01)))]),
        )?;
        col.set_rwkv_deck_count_scores(DeckId(1), HashMap::from([(c, 0.02)]))?;

        col.set_config(
            ConfigKey::SchedulingAlgorithm,
            &SchedulingAlgorithm::RwkvCurve,
        )?;
        assert_eq!(
            filtered_order(&mut col, FilteredSearchOrder::RetrievabilityAscending)?,
            vec![b, a, c]
        );
        assert_eq!(
            filtered_order(&mut col, FilteredSearchOrder::RetrievabilityDescending)?,
            vec![a, b, c]
        );

        col.set_config(
            ConfigKey::SchedulingAlgorithm,
            &SchedulingAlgorithm::RwkvInstant,
        )?;
        assert_eq!(
            filtered_order(&mut col, FilteredSearchOrder::RetrievabilityAscending)?,
            vec![a, b, c]
        );
        assert_eq!(
            filtered_order(&mut col, FilteredSearchOrder::RetrievabilityDescending)?,
            vec![b, a, c]
        );

        col.set_config(ConfigKey::SchedulingAlgorithm, &SchedulingAlgorithm::Fsrs7)?;
        assert_eq!(
            filtered_order(&mut col, FilteredSearchOrder::RetrievabilityAscending)?,
            vec![c, a, b]
        );

        // under RWKV even with FSRS off, the order is RWKV's, not SM-2's
        col.set_config_bool(BoolKey::Fsrs, false, true)?;
        col.set_config(
            ConfigKey::SchedulingAlgorithm,
            &SchedulingAlgorithm::RwkvCurve,
        )?;
        assert_eq!(
            filtered_order(&mut col, FilteredSearchOrder::RetrievabilityAscending)?,
            vec![b, a, c]
        );
        Ok(())
    }
}
