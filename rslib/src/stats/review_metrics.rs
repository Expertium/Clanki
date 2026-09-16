// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The Stats page's model-quality graphs (spec ui.stats-model-metrics):
//! FSRS-7's predicted probability of recall before every answer, with the
//! answer itself. The RWKV predictions of the same answers come from the
//! RWKV job in Python; the graphs themselves are drawn from both.

use std::collections::hash_map::Entry;
use std::collections::HashMap;

use anki_proto::stats::ReviewPredictionsResponse;
use fsrs::FSRSItem;
use fsrs::MemoryState;
use fsrs::FSRS;
use itertools::Itertools;
use rayon::prelude::*;

use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::scheduler::fsrs::curve::Fsrs7Curve;
use crate::scheduler::fsrs::params::fsrs_prediction_source_from_filtered_revlogs;
use crate::scheduler::fsrs::params::reviews_for_fsrs;
use crate::scheduler::fsrs::params::FsrsReviewPredictionSource;
use crate::scheduler::fsrs::preset::FsrsPreset;
use crate::scheduler::fsrs::preset::FsrsPresetId;
use crate::search::SortMode;

/// Cards per FSRS-7 replay batch; a batch holds one preset's cards of
/// similar history length, and batches run in parallel.
const FSRS_BATCH_SIZE: usize = 256;
/// A search with more than 1/N of the collection's cards reads the whole
/// review log in one pass; a smaller one reads its cards' entries by index.
const SCAN_REVLOG_ABOVE_SHARE_OF_CARDS: usize = 5;

/// What the computation needs, read while the collection is locked; the
/// computation itself runs without the lock.
pub(crate) struct ReviewPredictionsInput {
    /// Ratings before this timestamp are not predicted; 0 = the whole
    /// history.
    cutoff: TimestampSecs,
    /// The searched cards' review logs, card by card, oldest first.
    revlog: Vec<RevlogEntry>,
    /// The presets of the reviewed cards, and each card's.
    presets: Vec<FsrsPreset>,
    preset_of_card: HashMap<CardId, usize>,
}

impl Collection {
    pub(crate) fn review_predictions_input(
        &mut self,
        search: &str,
        days: u32,
    ) -> Result<ReviewPredictionsInput> {
        let timing = self.timing_today()?;
        let cutoff = if days == 0 {
            TimestampSecs(0)
        } else {
            TimestampSecs(timing.next_day_at.0 - (days as i64) * 86_400)
        };
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
        // the whole history of each card, whatever the page's period: the
        // memory state before a rating comes from every earlier rating
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
        let mut presets = vec![];
        let mut preset_of_card = HashMap::new();
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
        Ok(ReviewPredictionsInput {
            cutoff,
            revlog,
            presets,
            preset_of_card,
        })
    }

    #[cfg(test)]
    pub(crate) fn review_predictions(
        &mut self,
        search: &str,
        days: u32,
    ) -> Result<ReviewPredictionsResponse> {
        self.review_predictions_input(search, days)?.compute()
    }
}

/// One card's FSRS-7 replay: the item to run, and which ratings it predicts.
struct CardSource {
    card: CardId,
    preset: usize,
    source: FsrsReviewPredictionSource,
    /// Whether each target's answer was Hard, Good or Easy.
    remembered: Vec<bool>,
    /// The seconds of each target's answer.
    answered_at: Vec<TimestampSecs>,
}

impl ReviewPredictionsInput {
    pub(crate) fn compute(self) -> Result<ReviewPredictionsResponse> {
        let cards: Vec<&[RevlogEntry]> = self.revlog.chunk_by(|a, b| a.cid == b.cid).collect();
        let sources: Vec<CardSource> = cards
            .par_iter()
            .filter_map(|entries| self.card_source(entries))
            .collect();
        if self.presets.iter().all(|preset| preset.fsrs().is_err()) && !sources.is_empty() {
            return Ok(ReviewPredictionsResponse {
                no_params: true,
                ..Default::default()
            });
        }

        // one batch per preset and history length, run in parallel
        let mut order: Vec<usize> = (0..sources.len()).collect();
        order.sort_unstable_by_key(|&index| {
            (sources[index].preset, sources[index].source.reviews().len())
        });
        let batches: Vec<&[usize]> = order
            .chunk_by(|&a, &b| sources[a].preset == sources[b].preset)
            .flat_map(|batch| batch.chunks(FSRS_BATCH_SIZE))
            .collect();
        let computed: Vec<Vec<Vec<MemoryState>>> = batches
            .par_iter()
            .map_init(FsrsCache::default, |cache, batch| {
                let preset = sources[batch[0]].preset;
                let Some(fsrs) = cache.get(preset, &self.presets[preset]) else {
                    // a preset without usable FSRS-7 parameters predicts
                    // nothing; it is never replaced by another preset's
                    return Ok(vec![vec![]; batch.len()]);
                };
                Ok(fsrs.historical_memory_state_batch(
                    batch
                        .iter()
                        .map(|&index| FSRSItem {
                            reviews: sources[index].source.reviews().to_vec(),
                        })
                        .collect(),
                    None,
                )?)
            })
            .collect::<Result<Vec<_>>>()?;

        let curves: Vec<Option<Fsrs7Curve>> = self
            .presets
            .iter()
            .map(|preset| Fsrs7Curve::new(&preset.params))
            .collect();
        let mut rows: Vec<(RevlogId, CardId, f32, bool)> = vec![];
        for (batch, computed) in batches.iter().zip_eq(computed) {
            for (&index, states) in batch.iter().zip_eq(computed) {
                let source = &sources[index];
                let Some(curve) = &curves[source.preset] else {
                    continue;
                };
                for (position, &(revlog_id, review)) in source.source.targets().iter().enumerate() {
                    if source.answered_at[position] < self.cutoff {
                        continue;
                    }
                    let Some(state) = review.checked_sub(1).and_then(|index| states.get(index))
                    else {
                        continue;
                    };
                    let delta_t = source.source.reviews()[review].delta_t;
                    let Some(prediction) = curve.retrievability(*state, delta_t) else {
                        continue;
                    };
                    if !prediction.is_finite() || !(0.0..=1.0).contains(&prediction) {
                        continue;
                    }
                    rows.push((
                        revlog_id,
                        source.card,
                        prediction,
                        source.remembered[position],
                    ));
                }
            }
        }
        rows.sort_unstable_by_key(|row| row.0);

        let mut response = ReviewPredictionsResponse::default();
        for (revlog_id, card_id, prediction, remembered) in rows {
            response.revlog_ids.push(revlog_id.0);
            response.card_ids.push(card_id.0);
            response.predictions.push(prediction);
            response.remembered.push(remembered);
        }
        Ok(response)
    }

    /// One card's replay, or None when FSRS-7 predicts none of its ratings.
    fn card_source(&self, entries: &[RevlogEntry]) -> Option<CardSource> {
        let card = entries[0].cid;
        let preset = *self.preset_of_card.get(&card)?;
        let reviews = reviews_for_fsrs(entries.to_vec(), true, TimestampMillis(0))?;
        let source = fsrs_prediction_source_from_filtered_revlogs(&reviews.filtered_revlogs)?;
        let mut remembered = vec![];
        let mut answered_at = vec![];
        for &(_, review) in source.targets() {
            let entry = reviews.filtered_revlogs.get(review)?;
            remembered.push(entry.button_chosen > 1);
            answered_at.push(entry.id.as_secs());
        }
        Some(CardSource {
            card,
            preset,
            source,
            remembered,
            answered_at,
        })
    }
}

#[cfg(test)]
mod tests {
    use fsrs::DEFAULT_PARAMETERS;

    use super::*;
    use crate::card::CardQueue;
    use crate::card::CardType;
    use crate::deckconfig::algorithm::SchedulingAlgorithm;
    use crate::deckconfig::FsrsVersion;
    use crate::revlog::RevlogId;
    use crate::revlog::RevlogReviewKind;
    use crate::tests::NoteAdder;

    fn fsrs7_collection() -> Collection {
        let mut col = Collection::new();
        col.update_default_deck_config(|config| {
            config.fsrs_version = FsrsVersion::Seven as i32;
            config.fsrs_params_7 = DEFAULT_PARAMETERS.to_vec();
            SchedulingAlgorithm::Fsrs7.apply_to(config);
        });
        col.set_config(
            crate::config::ConfigKey::SchedulingAlgorithm,
            &SchedulingAlgorithm::Fsrs7,
        )
        .unwrap();
        col
    }

    fn add_card(col: &mut Collection) -> CardId {
        let note = NoteAdder::basic(col).add(col);
        let mut card = col.storage.all_cards_of_note(note.id).unwrap().remove(0);
        card.ctype = CardType::Review;
        card.queue = CardQueue::Review;
        col.storage.update_card(&card).unwrap();
        card.id
    }

    /// Adds a rating on the day `day` (0 = today), at noon of that day.
    fn rate(
        col: &mut Collection,
        card: CardId,
        day: i32,
        kind: RevlogReviewKind,
        button: u8,
    ) -> RevlogId {
        let next_day_at = col.timing_today().unwrap().next_day_at;
        let entry = RevlogEntry {
            id: RevlogId((next_day_at.0 + day as i64 * 86_400 - 43_200) * 1000),
            cid: card,
            button_chosen: button,
            interval: if kind == RevlogReviewKind::Learning {
                -600
            } else {
                3
            },
            ease_factor: 2500,
            review_kind: kind,
            ..Default::default()
        };
        col.storage.add_revlog_entry(&entry, false).unwrap();
        entry.id
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn fsrs7_predictions_follow_an_earlier_rating() -> Result<()> {
        let mut col = fsrs7_collection();
        let card = add_card(&mut col);
        let first = rate(&mut col, card, -40, RevlogReviewKind::Learning, 3);
        let second = rate(&mut col, card, -37, RevlogReviewKind::Review, 1);
        let third = rate(&mut col, card, -36, RevlogReviewKind::Relearning, 3);
        let fourth = rate(&mut col, card, -10, RevlogReviewKind::Review, 4);

        let response = col.review_predictions("", 0)?;
        // the first rating has no prediction; the others do, in order
        assert_eq!(response.revlog_ids, vec![second.0, third.0, fourth.0]);
        assert_eq!(response.card_ids, vec![card.0; 3]);
        // Again = forgotten, Good and Easy = remembered
        assert_eq!(response.remembered, vec![false, true, true]);
        assert!(!response.no_params);
        for prediction in &response.predictions {
            assert!((0.0..=1.0).contains(prediction), "{prediction}");
        }
        // a longer wait predicts less recall than a shorter one
        assert!(response.predictions[2] < response.predictions[1]);
        assert_ne!(first.0, second.0);
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn a_cards_first_rating_has_no_prediction() -> Result<()> {
        let mut col = fsrs7_collection();
        let card = add_card(&mut col);
        rate(&mut col, card, -5, RevlogReviewKind::Learning, 3);
        // cards never rated are in no series either
        add_card(&mut col);

        assert!(col.review_predictions("", 0)?.revlog_ids.is_empty());
        Ok(())
    }

    // Pins spec/ui.md#ui.stats-model-metrics
    #[test]
    fn the_period_selects_the_ratings_not_the_history() -> Result<()> {
        let mut col = fsrs7_collection();
        let card = add_card(&mut col);
        rate(&mut col, card, -400, RevlogReviewKind::Learning, 3);
        let old = rate(&mut col, card, -390, RevlogReviewKind::Review, 3);
        let recent = rate(&mut col, card, -10, RevlogReviewKind::Review, 3);

        let whole = col.review_predictions("", 0)?;
        assert_eq!(whole.revlog_ids, vec![old.0, recent.0]);

        // a year's period keeps the recent rating only, and its prediction
        // still comes from the whole history before it
        let year = col.review_predictions("", 365)?;
        assert_eq!(year.revlog_ids, vec![recent.0]);
        assert_eq!(year.predictions, vec![whole.predictions[1]]);
        Ok(())
    }
}

/// A worker's FSRS-7 models, one per preset, built when first needed; a
/// preset without usable FSRS-7 parameters has none.
#[derive(Default)]
struct FsrsCache(HashMap<usize, Option<FSRS>>);

impl FsrsCache {
    fn get(&mut self, index: usize, preset: &FsrsPreset) -> Option<&FSRS> {
        match self.0.entry(index) {
            Entry::Occupied(entry) => entry.into_mut(),
            Entry::Vacant(entry) => entry.insert(preset.fsrs().ok()),
        }
        .as_ref()
    }
}
