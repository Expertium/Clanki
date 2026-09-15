// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The Stats page's Total Knowledge graph (spec ui.stats-total-knowledge):
//! for each day of the search's whole review history, the cards rated so
//! far (the upper bound) and, under FSRS-7, the sum of their R. RWKV's sum
//! is computed by the RWKV job in Python.

use std::collections::HashMap;

use anki_proto::deck_config::deck_configs_for_update::SchedulingAlgorithm as SchedulingAlgorithmProto;
use anki_proto::stats::TotalKnowledgeResponse;
use fsrs::FSRSItem;
use fsrs::MemoryState;
use fsrs::FSRS;
use itertools::Itertools;
use rayon::prelude::*;

use crate::deckconfig::algorithm::SchedulingAlgorithm;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::scheduler::fsrs::memory_state::fsrs_item_for_memory_state;
use crate::scheduler::fsrs::preset::FsrsPreset;
use crate::scheduler::fsrs::preset::FsrsPresetId;
use crate::search::SortMode;

/// Cards per FSRS-7 replay batch; a batch holds one preset's cards of
/// similar history length, and batches run in parallel.
const FSRS_BATCH_SIZE: usize = 256;
/// A search with more than 1/N of the collection's cards reads the whole
/// review log in one pass; a smaller one reads its cards' entries by index.
const SCAN_REVLOG_ABOVE_SHARE_OF_CARDS: usize = 5;
const S_MIN: f32 = 0.0001;
const S_MAX: f32 = 36500.0;
const D_MIN: f32 = 1.0;
const D_MAX: f32 = 10.0;

/// What the computation needs, read while the collection is locked; the
/// computation itself runs without the lock.
pub(crate) struct TotalKnowledgeInput {
    algorithm: SchedulingAlgorithm,
    next_day_at: TimestampSecs,
    /// The searched cards' review logs, card by card, oldest first.
    revlog: Vec<RevlogEntry>,
    /// FSRS-7 only: the presets of the reviewed cards, and each card's.
    presets: Vec<FsrsPreset>,
    preset_of_card: HashMap<CardId, usize>,
}

impl Collection {
    pub(crate) fn total_knowledge_input(&mut self, search: &str) -> Result<TotalKnowledgeInput> {
        let algorithm = self.effective_scheduling_algorithm()?;
        let next_day_at = self.timing_today()?.next_day_at;
        let guard = self.search_cards_into_table_with_stats_search(
            search,
            SortMode::NoOrder,
            Some(search),
        )?;
        let decks: HashMap<CardId, (DeckId, DeckId)> = guard
            .col
            .storage
            .db
            .prepare("select id, did, odid from cards where id in (select cid from search_cids)")?
            .query_map([], |row| Ok((row.get(0)?, (row.get(1)?, row.get(2)?))))?
            .collect::<rusqlite::Result<_>>()?;
        // the whole history, whatever the page's period
        let storage = &guard.col.storage;
        let all_cards: usize = storage
            .db
            .query_row("select count() from cards", [], |row| row.get(0))?;
        let revlog = if decks.len() * SCAN_REVLOG_ABOVE_SHARE_OF_CARDS < all_cards {
            storage.get_revlog_entries_for_searched_cards_in_card_order()?
        } else {
            let mut revlog =
                storage.get_revlog_entries_of_cards_by_scan(&decks.keys().copied().collect())?;
            // stable: each card's entries stay oldest first
            revlog.sort_by_key(|entry| entry.cid);
            revlog
        };

        let mut presets = vec![];
        let mut preset_of_card = HashMap::new();
        if algorithm == SchedulingAlgorithm::Fsrs7 {
            // fsrs_presets_for_cards reads only the cards' ids and decks
            let cards: Vec<Card> = revlog
                .iter()
                .map(|entry| entry.cid)
                .dedup()
                .map(|id| {
                    let (deck_id, original_deck_id) = decks[&id];
                    Card {
                        id,
                        deck_id,
                        original_deck_id,
                        ..Default::default()
                    }
                })
                .collect();
            let mut index_of: HashMap<FsrsPresetId, usize> = HashMap::new();
            for (card, preset) in guard.col.fsrs_presets_for_cards(&cards)? {
                let index = match index_of.get(&preset.id) {
                    Some(&index) => index,
                    None => {
                        index_of.insert(preset.id.clone(), presets.len());
                        presets.push(preset);
                        presets.len() - 1
                    }
                };
                preset_of_card.insert(card, index);
            }
        }
        Ok(TotalKnowledgeInput {
            algorithm,
            next_day_at,
            revlog,
            presets,
            preset_of_card,
        })
    }

    #[cfg(test)]
    pub(crate) fn total_knowledge(&mut self, search: &str) -> Result<TotalKnowledgeResponse> {
        self.total_knowledge_input(search)?.compute()
    }
}

/// A day relative to today (0 = today, -1 = yesterday), with the "next day
/// starts at" hour; the same days as RWKV's review history.
fn day_of(entry: &RevlogEntry, next_day_at: TimestampSecs) -> i32 {
    -((next_day_at.0 - 1 - entry.id.as_secs().0).max(0) / 86_400) as i32
}

impl TotalKnowledgeInput {
    pub(crate) fn compute(self) -> Result<TotalKnowledgeResponse> {
        let algorithm = SchedulingAlgorithmProto::from(self.algorithm) as i32;
        let cards: Vec<&[RevlogEntry]> = self
            .revlog
            .chunk_by(|a, b| a.cid == b.cid)
            .filter(|entries| entries.iter().any(is_rating))
            .collect();
        let Some(first_day) = cards
            .iter()
            .map(|entries| first_rating_day(entries, self.next_day_at))
            .min()
        else {
            return Ok(TotalKnowledgeResponse {
                algorithm,
                ..Default::default()
            });
        };
        let days = (1 - first_day) as usize;

        let mut started = vec![0u32; days];
        for entries in &cards {
            started[(first_rating_day(entries, self.next_day_at) - first_day) as usize] += 1;
        }
        let reviewed_cards = started
            .into_iter()
            .scan(0, |count, started| {
                *count += started;
                Some(*count)
            })
            .collect();

        let sum_r = if self.algorithm == SchedulingAlgorithm::Fsrs7 {
            self.fsrs7_sums(&cards, first_day, days)?
        } else {
            vec![]
        };

        Ok(TotalKnowledgeResponse {
            algorithm,
            first_day,
            reviewed_cards,
            sum_r,
        })
    }

    fn preset_of(&self, entries: &[RevlogEntry]) -> Result<usize> {
        let card = entries[0].cid;
        self.preset_of_card.get(&card).copied().or_not_found(card)
    }

    /// The sum of the cards' FSRS-7 R on each day, from a replay of each
    /// card's whole history with its preset's parameters.
    fn fsrs7_sums(
        &self,
        cards: &[&[RevlogEntry]],
        first_day: i32,
        days: usize,
    ) -> Result<Vec<f64>> {
        let mut replays: Vec<Replay> = cards
            .par_iter()
            .enumerate()
            .map_init(FsrsCache::default, |cache, (card, entries)| {
                let preset = self.preset_of(entries)?;
                let fsrs = cache.get(preset, &self.presets[preset])?;
                card_replays(fsrs, &self.presets[preset], preset, card, entries)
            })
            .collect::<Result<Vec<_>>>()?
            .into_iter()
            .flatten()
            .collect();

        replays.sort_unstable_by_key(|replay| (replay.preset, replay.item.reviews.len()));
        let batches: Vec<&[Replay]> = replays
            .chunk_by(|a, b| a.preset == b.preset)
            .flat_map(|replays| replays.chunks(FSRS_BATCH_SIZE))
            .collect();
        let computed = batches
            .par_iter()
            .map_init(FsrsCache::default, |cache, batch| {
                let preset = batch[0].preset;
                replay_states(cache.get(preset, &self.presets[preset])?, batch)
            })
            .collect::<Result<Vec<_>>>()?;
        let mut states: Vec<Vec<(RevlogId, MemoryState)>> = vec![vec![]; cards.len()];
        for (batch, computed) in batches.iter().zip_eq(computed) {
            for (replay, computed) in batch.iter().zip_eq(computed) {
                states[replay.card].extend(replay.ratings.iter().copied().zip_eq(computed));
            }
        }

        Ok(cards
            .par_iter()
            .zip(states)
            .map(|(entries, mut states)| {
                states.sort_unstable_by_key(|(id, _)| *id);
                let params = &self.presets[self.preset_of_card[&entries[0].cid]].params;
                card_timeline(entries, &states, params, self.next_day_at)
            })
            .fold(
                || vec![0.0; days],
                |mut sums, timeline| {
                    add_card_retrievability(&timeline, first_day, &mut sums);
                    sums
                },
            )
            .reduce(
                || vec![0.0; days],
                |mut a, b| {
                    a.iter_mut().zip(b).for_each(|(a, b)| *a += b);
                    a
                },
            ))
    }
}

/// A worker's FSRS-7 models, one per preset, built when first needed.
#[derive(Default)]
struct FsrsCache(HashMap<usize, FSRS>);

impl FsrsCache {
    fn get(&mut self, index: usize, preset: &FsrsPreset) -> Result<&FSRS> {
        if !self.0.contains_key(&index) {
            self.0.insert(index, preset.fsrs()?);
        }
        Ok(&self.0[&index])
    }
}

/// A rating: an answer that affects scheduling (not a manual reschedule, a
/// reset or a cram answer).
fn is_rating(entry: &RevlogEntry) -> bool {
    entry.has_rating_and_affects_scheduling()
}

fn first_rating_day(entries: &[RevlogEntry], next_day_at: TimestampSecs) -> i32 {
    day_of(
        entries.iter().find(|entry| is_rating(entry)).unwrap(),
        next_day_at,
    )
}

/// One FSRS-7 replay of part of a card's history.
struct Replay {
    card: usize,
    preset: usize,
    /// The ratings the replay covers, in order.
    ratings: Vec<RevlogId>,
    item: FSRSItem,
    starting_state: Option<MemoryState>,
}

/// The card's whole history as FSRS-7 replays: each starts where the card's
/// own memory state would start (`reviews_for_fsrs`), at a learning start
/// after a reset or a relearn, so every part of the history is replayed as
/// that part's latest memory state was. A rating before the card's first
/// interday review that has no learning step is in no replay.
fn card_replays(
    fsrs: &FSRS,
    preset: &FsrsPreset,
    preset_index: usize,
    card: usize,
    entries: &[RevlogEntry],
) -> Result<Vec<Replay>> {
    let mut replays = vec![];
    let mut end = entries.len();
    loop {
        while end > 0 && !is_rating(&entries[end - 1]) {
            end -= 1;
        }
        if end == 0 {
            break;
        }
        let Some(item) = fsrs_item_for_memory_state(
            fsrs,
            &preset.params,
            entries[..end].to_vec(),
            preset.historical_retention,
            TimestampMillis(0),
        )?
        else {
            break;
        };
        let first = item.filtered_revlogs[0].id;
        end = entries[..end]
            .iter()
            .position(|entry| entry.id == first)
            .unwrap();
        replays.push(Replay {
            card,
            preset: preset_index,
            ratings: item.filtered_revlogs.iter().map(|entry| entry.id).collect(),
            item: item.item,
            starting_state: item.starting_state,
        });
    }
    Ok(replays)
}

/// The memory state after each rating of each replay in `batch`.
fn replay_states(fsrs: &FSRS, batch: &[Replay]) -> Result<Vec<Vec<MemoryState>>> {
    // a replay whose only rating became its starting state has nothing to run
    let (empty, runnable): (Vec<_>, Vec<_>) = batch
        .iter()
        .enumerate()
        .partition(|(_, replay)| replay.item.reviews.is_empty());
    let mut out = vec![vec![]; batch.len()];
    for (index, replay) in empty {
        out[index] = replay.starting_state.into_iter().collect();
    }
    let states = fsrs.historical_memory_state_batch(
        runnable
            .iter()
            .map(|(_, replay)| replay.item.clone())
            .collect(),
        Some(
            runnable
                .iter()
                .map(|(_, replay)| replay.starting_state)
                .collect(),
        ),
    )?;
    for ((index, _), states) in runnable.into_iter().zip_eq(states) {
        out[index] = states;
    }
    Ok(out)
}

/// A rating (with FSRS-7's curve after it, if a replay covers it) or a
/// reset, on its day.
struct Event {
    day: i32,
    curve: Option<Fsrs7Curve>,
    reset: bool,
}

/// One card's ratings and resets; `states` are the memory states after its
/// ratings, sorted by review id.
fn card_timeline(
    entries: &[RevlogEntry],
    states: &[(RevlogId, MemoryState)],
    params: &[f32],
    next_day_at: TimestampSecs,
) -> Vec<Event> {
    entries
        .iter()
        .filter_map(|entry| {
            let day = day_of(entry, next_day_at);
            if is_rating(entry) {
                let curve = states
                    .binary_search_by_key(&entry.id, |(id, _)| *id)
                    .ok()
                    .map(|index| Fsrs7Curve::new(params, states[index].1));
                Some(Event {
                    day,
                    curve,
                    reset: false,
                })
            } else {
                entry.is_reset().then_some(Event {
                    day,
                    curve: None,
                    reset: true,
                })
            }
        })
        .collect()
}

/// Adds one card's R to each day from its first event through today. A day
/// takes its value from the card's last event on or before it: a rating on
/// that day gives 1, an earlier rating its curve at the whole days since,
/// a reset (or a rating no replay covers) 0.
fn add_card_retrievability(timeline: &[Event], first_day: i32, sums: &mut [f64]) {
    for (index, event) in timeline.iter().enumerate() {
        let until = timeline.get(index + 1).map_or(1, |next| next.day);
        if event.reset || until <= event.day {
            continue;
        }
        sums[(event.day - first_day) as usize] += 1.0;
        if let Some(curve) = &event.curve {
            for day in event.day + 1..until {
                sums[(day - first_day) as usize] += curve.retrievability((day - event.day) as f32);
            }
        }
    }
}

/// FSRS-7's forgetting curve for one memory state, with the terms that do
/// not depend on the elapsed time worked out once; it matches the fsrs
/// crate's `current_retrievability` (model_v7 `power_forgetting_curve`).
#[derive(Debug, Clone, Copy)]
struct Fsrs7Curve {
    fast_scale: f32,
    fast_decay: f32,
    fast_weight: f32,
    slow_scale: f32,
    slow_decay: f32,
    slow_weight: f32,
}

impl Fsrs7Curve {
    fn new(w: &[f32], state: MemoryState) -> Self {
        let s = state.stability.clamp(S_MIN, S_MAX);
        let s_fast = state.stability_fast.clamp(S_MIN, S_MAX);
        let d = state.difficulty.clamp(D_MIN, D_MAX);

        let fast_decay = -(w[23] * s_fast.powf(w[33] - 0.3)).clamp(0.01, 0.95);
        let fast_factor = (w[25].ln() * fast_decay.powi(-1)).min(60.0).exp() - 1.0;
        let slow_decay = -w[24].clamp(0.01, 0.95);
        let slow_factor = w[26].powf(slow_decay.powi(-1)) - 1.0;
        let d_timescale = ((d - 5.0) * (w[32] - 0.3)).exp();

        let fast_weight = w[27] * s_fast.powf(-w[29]);
        let slow_weight = w[28] * s.powf(w[30]) * ((d - 5.0) * (w[31] - 0.5)).exp();
        let total_weight = fast_weight + slow_weight;
        Self {
            fast_scale: fast_factor / s_fast,
            fast_decay,
            fast_weight: fast_weight / total_weight,
            slow_scale: slow_factor * d_timescale / s,
            slow_decay,
            slow_weight: slow_weight / total_weight,
        }
    }

    fn retrievability(&self, elapsed_days: f32) -> f64 {
        let t = elapsed_days.max(0.0);
        let fast = (t * self.fast_scale + 1.0).powf(self.fast_decay);
        let slow = (t * self.slow_scale + 1.0).powf(self.slow_decay);
        let retention = self.fast_weight * fast + self.slow_weight * slow;
        retention.mul_add(1.0 - 2e-5, 1e-5) as f64
    }
}

#[cfg(test)]
mod tests {
    use fsrs::DEFAULT_PARAMETERS;

    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::deckconfig::FsrsVersion;
    use crate::revlog::RevlogReviewKind;
    use crate::tests::NoteAdder;

    fn fsrs7_params() -> Vec<f32> {
        vec![
            0.4843, 3.0562, 10.9946, 32.7202, 5.6296, 0.5900, 3.1230, 2.4679, 0.2733, 1.4895,
            0.4868, 0.0010, 0.8082, 0.1723, 0.6389, 1.5767, 0.8918, 0.3341, 3.5942, 0.3455, 0.0022,
            0.2834, 2.6418, 0.5604, 1.3042, 2.5054, 0.9376, 0.0611, 0.0830, 0.6339, 0.9846, 0.2485,
            0.6014, 0.0545,
        ]
    }

    fn fsrs7_collection(params: Vec<f32>) -> Collection {
        let mut col = Collection::new();
        col.update_default_deck_config(|config| {
            config.fsrs_version = FsrsVersion::Seven as i32;
            config.fsrs_params_7 = params;
            SchedulingAlgorithm::Fsrs7.apply_to(config);
        });
        col.set_config(
            crate::config::ConfigKey::SchedulingAlgorithm,
            &SchedulingAlgorithm::Fsrs7,
        )
        .unwrap();
        col
    }

    /// Adds an entry on the day `day` (0 = today), `minute` minutes after
    /// noon of that scheduler day.
    fn entry(
        col: &mut Collection,
        card: CardId,
        day: i32,
        (kind, button, ease_factor): (RevlogReviewKind, u8, u32),
        minute: i64,
    ) {
        let next_day_at = col.timing_today().unwrap().next_day_at;
        let entry = RevlogEntry {
            id: RevlogId((next_day_at.0 + day as i64 * 86_400 - 43_200 + minute * 60) * 1000),
            cid: card,
            button_chosen: button,
            interval: if kind == RevlogReviewKind::Learning {
                -600
            } else {
                3
            },
            ease_factor,
            review_kind: kind,
            ..Default::default()
        };
        col.storage.add_revlog_entry(&entry, false).unwrap();
    }

    fn rate(col: &mut Collection, card: CardId, day: i32, kind: RevlogReviewKind, button: u8) {
        entry(col, card, day, (kind, button, 2500), 0);
    }

    /// Forget / Reset: a manual entry without an ease factor.
    fn reset(col: &mut Collection, card: CardId, day: i32) {
        entry(col, card, day, (RevlogReviewKind::Manual, 0, 0), 0);
    }

    fn add_card(col: &mut Collection) -> CardId {
        let note = NoteAdder::basic(col).add(col);
        let mut card = col.storage.all_cards_of_note(note.id).unwrap().remove(0);
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        col.storage.update_card(&card).unwrap();
        card.id
    }

    /// R of one card on each day from `first_day` through today, from the
    /// card's memory states as card info computes them.
    fn expected_card_r(col: &mut Collection, card: CardId, first_day: i32) -> Vec<f64> {
        let card = col.storage.get_card(card).unwrap().unwrap();
        let preset = col.fsrs_preset_for_card(&card).unwrap();
        let fsrs = preset.fsrs().unwrap();
        let revlog = col.storage.get_revlog_entries_for_card(card.id).unwrap();
        let next_day_at = col.timing_today().unwrap().next_day_at;
        let item = fsrs_item_for_memory_state(
            &fsrs,
            &preset.params,
            revlog.clone(),
            preset.historical_retention,
            TimestampMillis(0),
        )
        .unwrap()
        .unwrap();
        let states = fsrs
            .historical_memory_states(item.item, item.starting_state)
            .unwrap();
        let ratings: Vec<(i32, MemoryState)> = item
            .filtered_revlogs
            .iter()
            .map(|entry| day_of(entry, next_day_at))
            .zip(states)
            .collect();
        (first_day..=0)
            .map(|day| match ratings.iter().rev().find(|(rated, _)| *rated <= day) {
                None => 0.0,
                Some((rated, _)) if *rated == day => 1.0,
                Some((rated, state)) => {
                    fsrs.current_retrievability(*state, (day - rated) as f32) as f64
                }
            })
            .collect()
    }

    fn assert_close(actual: &[f64], expected: &[f64]) {
        assert_eq!(actual.len(), expected.len());
        for (day, (actual, expected)) in actual.iter().zip(expected).enumerate() {
            assert!(
                (actual - expected).abs() < 1e-5,
                "day {day}: {actual} != {expected}"
            );
        }
    }

    // Pins spec/ui.md#ui.stats-total-knowledge
    #[test]
    fn fsrs7_sums_match_the_cards_historical_memory_states() -> Result<()> {
        let mut col = fsrs7_collection(fsrs7_params());
        let first = add_card(&mut col);
        rate(&mut col, first, -60, RevlogReviewKind::Learning, 3);
        entry(
            &mut col,
            first,
            -60,
            (RevlogReviewKind::Learning, 3, 2500),
            10,
        );
        rate(&mut col, first, -57, RevlogReviewKind::Review, 3);
        rate(&mut col, first, -40, RevlogReviewKind::Review, 1);
        rate(&mut col, first, -39, RevlogReviewKind::Relearning, 3);
        rate(&mut col, first, -12, RevlogReviewKind::Review, 4);
        let second = add_card(&mut col);
        rate(&mut col, second, -30, RevlogReviewKind::Learning, 2);
        rate(&mut col, second, -29, RevlogReviewKind::Review, 3);
        // a manual reschedule (set due date) is not a rating
        entry(&mut col, second, -20, (RevlogReviewKind::Manual, 0, 2500), 0);
        // nor a cram answer
        entry(&mut col, second, -15, (RevlogReviewKind::Filtered, 3, 0), 0);
        // cards never reviewed count nowhere
        for _ in 0..10 {
            add_card(&mut col);
        }

        // the whole collection: the review log is read in one pass
        let response = col.total_knowledge("")?;
        assert_eq!(
            response.algorithm,
            SchedulingAlgorithmProto::Fsrs7 as i32
        );
        assert_eq!(response.first_day, -60);
        let expected: Vec<f64> = expected_card_r(&mut col, first, -60)
            .into_iter()
            .zip(expected_card_r(&mut col, second, -60))
            .map(|(a, b)| a + b)
            .collect();
        assert_close(&response.sum_r, &expected);
        assert_eq!(response.reviewed_cards[0], 1);
        assert_eq!(response.reviewed_cards[29], 1);
        assert_eq!(response.reviewed_cards[30], 2);
        assert_eq!(*response.reviewed_cards.last().unwrap(), 2);

        // the search sets the scope (a small one reads by index)
        let note_id = col.storage.get_card(second)?.unwrap().note_id;
        let response = col.total_knowledge(&format!("nid:{}", note_id.0))?;
        assert_eq!(response.first_day, -30);
        assert_close(&response.sum_r, &expected_card_r(&mut col, second, -30));
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-total-knowledge
    #[test]
    fn total_knowledge_covers_the_whole_history() -> Result<()> {
        let mut col = fsrs7_collection(DEFAULT_PARAMETERS.to_vec());
        let card = add_card(&mut col);
        // far older than the page's default period of a year
        rate(&mut col, card, -2000, RevlogReviewKind::Learning, 3);
        rate(&mut col, card, -1990, RevlogReviewKind::Review, 3);

        let response = col.total_knowledge("")?;
        assert_eq!(response.first_day, -2000);
        assert_eq!(response.reviewed_cards, vec![1; 2001]);
        assert_eq!(response.sum_r.len(), 2001);
        assert_close(&response.sum_r, &expected_card_r(&mut col, card, -2000));
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-total-knowledge
    #[test]
    fn a_reset_zeroes_r_until_the_next_rating() -> Result<()> {
        let mut col = fsrs7_collection(fsrs7_params());
        let card = add_card(&mut col);
        rate(&mut col, card, -20, RevlogReviewKind::Learning, 3);
        rate(&mut col, card, -17, RevlogReviewKind::Review, 3);
        // Forget, then learned again
        reset(&mut col, card, -10);
        rate(&mut col, card, -6, RevlogReviewKind::Learning, 3);
        rate(&mut col, card, -4, RevlogReviewKind::Review, 3);

        let response = col.total_knowledge("")?;
        let at = |day: i32| response.sum_r[(day + 20) as usize];
        assert_eq!(at(-20), 1.0);
        assert!(at(-11) > 0.5 && at(-11) < 1.0);
        for day in -10..=-7 {
            assert_eq!(at(day), 0.0);
        }
        assert_eq!(at(-6), 1.0);
        assert!(at(0) > 0.0 && at(0) < 1.0);
        // it stays in the upper bound
        assert_eq!(response.reviewed_cards, vec![1; 21]);

        // the days after the reset follow the replay after it, as the
        // card's memory state does
        assert_close(&response.sum_r[14..], &expected_card_r(&mut col, card, -6));
        // and the days before it the replay before it, with its own states
        let before: Vec<f64> = response.sum_r[..10].to_vec();
        assert_eq!(before[0], 1.0);
        assert_eq!(before[3], 1.0);
        assert!(before[9] < before[4]);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-total-knowledge
    #[test]
    fn rwkv_collections_get_the_upper_bound_only() -> Result<()> {
        let mut col = fsrs7_collection(fsrs7_params());
        let card = add_card(&mut col);
        rate(&mut col, card, -3, RevlogReviewKind::Learning, 3);
        for algorithm in [SchedulingAlgorithm::RwkvCurve, SchedulingAlgorithm::RwkvInstant] {
            col.set_config(crate::config::ConfigKey::SchedulingAlgorithm, &algorithm)?;
            let response = col.total_knowledge("")?;
            assert_eq!(
                response.algorithm,
                SchedulingAlgorithmProto::from(algorithm) as i32
            );
            assert_eq!(response.reviewed_cards, vec![1; 4]);
            // no FSRS-7 values under RWKV
            assert!(response.sum_r.is_empty());
        }

        // nothing reviewed: no days
        let response = col.total_knowledge(&format!("-cid:{}", card.0))?;
        assert!(response.reviewed_cards.is_empty() && response.sum_r.is_empty());
        Ok(())
    }

    #[test]
    fn scalar_curve_matches_the_fsrs_crate() -> Result<()> {
        for params in [DEFAULT_PARAMETERS.to_vec(), fsrs7_params()] {
            let fsrs = FSRS::new(&params)?;
            for stability in [0.0001, 0.05, 0.7, 3.0, 25.0, 400.0, 36500.0] {
                for stability_fast in [0.0001, 0.2, 2.5, 60.0, 36500.0] {
                    for difficulty in [1.0, 3.3, 5.0, 8.7, 10.0] {
                        let state = MemoryState {
                            stability,
                            difficulty,
                            stability_fast,
                        };
                        let curve = Fsrs7Curve::new(&params, state);
                        for elapsed in [0.0, 1.0, 2.0, 7.0, 30.0, 365.0, 5000.0] {
                            let expected = fsrs.current_retrievability(state, elapsed) as f64;
                            let actual = curve.retrievability(elapsed);
                            assert!(
                                (actual - expected).abs() < 1e-5,
                                "{state:?} at {elapsed}: {actual} != {expected}"
                            );
                        }
                    }
                }
            }
        }
        Ok(())
    }
}
