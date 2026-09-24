// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The RWKV replay's inputs, built from the review log in Rust.
//!
//! Two stages, so that a model with another input layout changes only the
//! second:
//! - (a) `stream`: the raw per-review event stream. One event per rated
//!   review of the replay history, holding what the collection knows about
//!   it (answer time, show time, duration, rating, card, note, deck and
//!   preset ids, the replay start) and nothing a model derives.
//! - (b) `published`: the feature encoder of today's published model. It
//!   turns the stream into the model's per-review input records, the records
//!   Python's `_historical_rwkv_review_inputs` builds.
//!
//! This module reads the collection for both and runs them for the whole
//! history (`rwkv_historical_review_inputs`). Its output is that of the
//! Python build, value for value; where it cannot be (a collection Python
//! reads another way, see `RwkvReplayInputsJob::encode`), it fails and
//! Python builds the inputs itself.

pub(crate) mod published;
pub(crate) mod stream;

use std::collections::HashMap;
use std::collections::HashSet;

use anki_proto::scheduler::rwkv_historical_review_inputs_request::FirstReviewElapsedSource;
use anki_proto::scheduler::RwkvHistoricalReviewInputsRequest;
use anki_proto::scheduler::RwkvHistoricalReviewInputsResponse;

use self::published::PublishedCardState;
use self::published::PublishedEncoder;
use self::published::PublishedReviewInput;
use self::published::RwkvFirstReviewElapsed;
use self::published::RwkvHistoryHashChain;
use self::stream::RwkvReviewStream;
use self::stream::RwkvStreamPresets;
use crate::deckconfig::DeckConfig;
use crate::deckconfig::DeckConfigId;
use crate::decks::Deck;
use crate::decks::DeckId;
use crate::prelude::*;
use crate::scheduler::fsrs::preset::FsrsPresetId;
use crate::scheduler::rwkv::rwkv_historical_rows_in_parts;
use crate::scheduler::rwkv::RwkvCollectionHold;
use crate::scheduler::timing::SchedTimingToday;
use crate::storage::RwkvHistoricalReviewRow;

/// Everything the replay inputs read from the collection: the rows of the
/// whole history (no review ignored) and what they need besides.
pub(crate) struct RwkvReplayInputsJob {
    rows: Vec<RwkvHistoricalReviewRow>,
    timing: SchedTimingToday,
    presets_by_card: Option<HashMap<CardId, i64>>,
    decks_by_id: HashMap<DeckId, Deck>,
    configs_by_id: HashMap<DeckConfigId, DeckConfig>,
}

/// How the replay inputs are encoded; the fields of the request.
pub(crate) struct RwkvReplayInputsSettings<'a> {
    pub(crate) ignored_review_ids: &'a [i64],
    pub(crate) first_review_elapsed_source: FirstReviewElapsedSource,
    pub(crate) first_review_uses_creation_by_config_id: &'a HashMap<i64, bool>,
    pub(crate) hash_history: bool,
    /// Above 0: also the state before the first review newer than the
    /// newest review less this many milliseconds.
    pub(crate) recovery_checkpoint_max_age_millis: i64,
}

/// The state of the replay before one of its reviews.
pub(crate) struct RwkvReplayCheckpoint {
    pub(crate) review_count: usize,
    pub(crate) last_review_id: i64,
    pub(crate) history_hash: String,
    pub(crate) cards: Vec<PublishedCardState>,
}

/// The whole history's replay inputs.
pub(crate) struct RwkvReplayInputs {
    pub(crate) reviews: Vec<PublishedReviewInput>,
    /// Every card of the replay, in the order it first reaches them.
    pub(crate) cards: Vec<PublishedCardState>,
    pub(crate) active_ignored_review_ids: Vec<i64>,
    pub(crate) history_hash: Option<String>,
    pub(crate) checkpoint: Option<RwkvReplayCheckpoint>,
}

impl Collection {
    /// What the replay inputs read besides the review log. `stable_preset_ids`
    /// holds the stable ids Python gave the add-on presets.
    pub(crate) fn rwkv_replay_inputs_job(
        &mut self,
        rows: Vec<RwkvHistoricalReviewRow>,
        stable_preset_ids: &HashMap<String, i64>,
    ) -> Result<RwkvReplayInputsJob> {
        let timing = self.timing_today()?;
        // As in the fingerprint: only an add-on overlay rule can move a card
        // to a preset other than its home deck's, so without one the cards
        // need not be loaded.
        let presets_by_card = if self.fsrs_preset_overlay_has_rules()? {
            let card_ids = rows
                .iter()
                .map(|row| CardId(row.card_id))
                .collect::<HashSet<_>>()
                .into_iter()
                .collect::<Vec<_>>();
            let cards = self.all_cards_for_ids(&card_ids, false)?;
            let presets = self.fsrs_presets_for_cards(&cards)?;
            Some(
                presets
                    .iter()
                    .map(|(card_id, preset)| {
                        Ok((card_id, python_stable_preset_id(&preset.id, stable_preset_ids)?))
                    })
                    .collect::<Result<HashMap<_, _>>>()?,
            )
        } else {
            None
        };
        Ok(RwkvReplayInputsJob {
            rows,
            timing,
            presets_by_card,
            decks_by_id: self.storage.get_decks_map()?,
            configs_by_id: self.storage.get_deck_config_map()?,
        })
    }
}

impl RwkvReplayInputsJob {
    /// The replay inputs. Reads nothing from the collection.
    ///
    /// Fails where Python would read the collection another way than this
    /// does, so that the caller falls back to Python rather than get other
    /// values: a review id below 0 (Python floors the division by 1000,
    /// Rust truncates it), a home deck that is missing or filtered or whose
    /// preset is missing (Python falls back to the default preset or the
    /// deck itself), and a preset id Python would hash (a negative deck
    /// config id, an add-on preset Python gave no stable id).
    pub(crate) fn encode(self, settings: &RwkvReplayInputsSettings) -> Result<RwkvReplayInputs> {
        let Self {
            mut rows,
            timing,
            presets_by_card,
            decks_by_id,
            configs_by_id,
        } = self;

        // The ignored reviews leave the rows after the start rows are found,
        // as in Python, and only those still in the rows count as active.
        let ignored: HashSet<i64> = settings.ignored_review_ids.iter().copied().collect();
        let mut active_ignored_review_ids = Vec::new();
        if !ignored.is_empty() {
            rows.retain(|row| {
                let keep = !ignored.contains(&row.review_id);
                if !keep {
                    active_ignored_review_ids.push(row.review_id);
                }
                keep
            });
            active_ignored_review_ids.sort_unstable();
            active_ignored_review_ids.dedup();
        }

        let mut checked_decks = HashSet::new();
        for row in &rows {
            require!(row.review_id >= 0, "RWKV replay review id below 0");
            if checked_decks.insert(row.deck_id) {
                let deck = decks_by_id
                    .get(&DeckId(row.deck_id))
                    .or_not_found(row.deck_id)?;
                let config_id = deck.config_id().or_invalid("home deck is filtered")?;
                require!(config_id.0 >= 0, "RWKV replay preset id below 0");
                configs_by_id.get(&config_id).or_not_found(config_id)?;
            }
        }

        let recovery_cutoff_review_id = rows
            .last()
            .filter(|_| settings.recovery_checkpoint_max_age_millis > 0)
            .map(|row| row.review_id - settings.recovery_checkpoint_max_age_millis);
        let routes = Vec::new();
        let mut stream = RwkvReviewStream::new(
            match presets_by_card {
                Some(by_card) => RwkvStreamPresets::ByCard(by_card),
                None => RwkvStreamPresets::HomeDeck {
                    decks_by_id: &decks_by_id,
                    configs_by_id: &configs_by_id,
                },
            },
            &routes,
        );
        let mut encoder = PublishedEncoder::new(
            timing,
            match settings.first_review_elapsed_source {
                FirstReviewElapsedSource::DeckConfig => RwkvFirstReviewElapsed::DeckConfig {
                    decks_by_id: &decks_by_id,
                    configs_by_id: &configs_by_id,
                    requested: settings.first_review_uses_creation_by_config_id,
                },
                FirstReviewElapsedSource::CardCreation => RwkvFirstReviewElapsed::CardCreation,
                FirstReviewElapsedSource::Missing => RwkvFirstReviewElapsed::Missing,
            },
        );
        let mut history_hash = settings.hash_history.then(RwkvHistoryHashChain::new);
        let mut checkpoint = None;
        let mut reviews: Vec<PublishedReviewInput> = Vec::with_capacity(rows.len());
        for row in rows {
            let event = stream.event(row)?;
            if let (Some(cutoff), Some(previous)) = (recovery_cutoff_review_id, reviews.last()) {
                if event.review_id > cutoff && checkpoint.is_none() {
                    checkpoint = Some(RwkvReplayCheckpoint {
                        review_count: reviews.len(),
                        last_review_id: previous.review_id,
                        history_hash: history_hash
                            .as_ref()
                            .map(RwkvHistoryHashChain::hex)
                            .unwrap_or_default(),
                        cards: encoder.cards().to_vec(),
                    });
                }
            }
            let review = encoder.encode(&event);
            if let Some(history_hash) = &mut history_hash {
                history_hash.update(&review);
            }
            reviews.push(review);
        }
        Ok(RwkvReplayInputs {
            reviews,
            cards: encoder.cards().to_vec(),
            active_ignored_review_ids,
            history_hash: history_hash.as_ref().map(RwkvHistoryHashChain::hex),
            checkpoint,
        })
    }
}

/// The stable id Python gives a preset, `_stable_preset_id(str(id))`: a deck
/// config's own id; an add-on preset's from `stable_preset_ids`, which Python
/// fills with the ids it computes.
fn python_stable_preset_id(
    preset_id: &FsrsPresetId,
    stable_preset_ids: &HashMap<String, i64>,
) -> Result<i64> {
    match preset_id {
        FsrsPresetId::DeckConfig(id) => {
            require!(id.0 >= 0, "RWKV replay preset id below 0");
            Ok(id.0)
        }
        FsrsPresetId::Addon(id) => stable_preset_ids
            .get(id)
            .copied()
            .or_invalid("missing stable id for add-on FSRS preset"),
    }
}

/// `RwkvReplayInputsJob` for the whole history, read with the collection
/// held for one part of the review log at a time
/// (`rwkv_historical_rows_in_parts`), and `extra`, read in the same hold as
/// the job. A collection that keeps changing is read in one piece.
pub(crate) fn rwkv_replay_inputs_job_in_parts<T>(
    stable_preset_ids: &HashMap<String, i64>,
    part_rows: usize,
    hold: &mut RwkvCollectionHold,
    mut extra: impl FnMut(&mut Collection) -> Result<T>,
) -> Result<(RwkvReplayInputsJob, T)> {
    if let Some((job, _)) = rwkv_historical_rows_in_parts(&[], part_rows, hold, |col, rows, _| {
        Ok((
            col.rwkv_replay_inputs_job(rows, stable_preset_ids)?,
            extra(col)?,
        ))
    })? {
        return Ok(job);
    }
    let mut job = None;
    hold(&mut |col| {
        let rows = col.storage.rwkv_historical_review_rows(&[])?.0;
        job = Some((
            col.rwkv_replay_inputs_job(rows, stable_preset_ids)?,
            extra(col)?,
        ));
        Ok(())
    })?;
    job.or_invalid("replay inputs not read")
}

/// The RPC: the inputs as little-endian int64 columns.
pub(crate) fn rwkv_historical_review_inputs(
    input: RwkvHistoricalReviewInputsRequest,
    part_rows: usize,
    hold: &mut RwkvCollectionHold,
) -> Result<RwkvHistoricalReviewInputsResponse> {
    let started = std::time::Instant::now();
    let (job, ()) =
        rwkv_replay_inputs_job_in_parts(&input.stable_preset_ids, part_rows, hold, |_| Ok(()))?;
    let inputs = job.encode(&RwkvReplayInputsSettings {
        ignored_review_ids: &input.ignored_review_ids,
        first_review_elapsed_source: input.first_review_elapsed_source(),
        first_review_uses_creation_by_config_id: &input.first_review_uses_creation_by_config_id,
        hash_history: input.hash_history,
        recovery_checkpoint_max_age_millis: input.recovery_checkpoint_max_age_millis,
    })?;
    let response = inputs_response(inputs);
    tracing::debug!(
        reviews = response.review_count,
        elapsed_ms = started.elapsed().as_secs_f64() * 1000.0,
        "built RWKV replay inputs in Rust"
    );
    Ok(response)
}

fn inputs_response(inputs: RwkvReplayInputs) -> RwkvHistoricalReviewInputsResponse {
    let RwkvReplayInputs {
        reviews,
        cards,
        active_ignored_review_ids,
        history_hash,
        checkpoint,
    } = inputs;
    let review_column = |value: fn(&PublishedReviewInput) -> i64| i64_column(reviews.iter().map(value));
    let card_column =
        |cards: &[PublishedCardState], value: fn(&PublishedCardState) -> i64| {
            i64_column(cards.iter().map(value))
        };
    let mut response = RwkvHistoricalReviewInputsResponse {
        review_ids: review_column(|review| review.review_id),
        card_ids: review_column(|review| review.card_id),
        note_ids: review_column(|review| review.note_id),
        deck_ids: review_column(|review| review.deck_id),
        preset_ids: review_column(|review| review.preset_id),
        eases: review_column(|review| review.ease),
        durations_millis: review_column(|review| review.duration_millis),
        card_types: review_column(|review| review.card_type),
        review_kinds: review_column(|review| review.review_kind),
        interval_days: review_column(|review| review.interval_days),
        ease_factors: review_column(|review| review.ease_factor),
        day_offsets: review_column(|review| review.day_offset),
        elapsed_days: review_column(|review| review.elapsed_days),
        elapsed_seconds: review_column(|review| review.elapsed_seconds),
        cards: card_column(&cards, |card| card.card_id),
        card_previous_review_ids: card_column(&cards, |card| card.previous_review_id),
        card_previous_interval_days: card_column(&cards, |card| card.previous_interval_days),
        card_review_counts: card_column(&cards, |card| card.review_count),
        last_review_id: reviews
            .iter()
            .map(|review| review.review_id)
            .max()
            .unwrap_or(0),
        review_count: reviews.len() as u64,
        history_hash: history_hash.unwrap_or_default(),
        active_ignored_review_ids,
        ..Default::default()
    };
    if let Some(checkpoint) = checkpoint {
        response.checkpoint_review_count = checkpoint.review_count as u64;
        response.checkpoint_last_review_id = checkpoint.last_review_id;
        response.checkpoint_history_hash = checkpoint.history_hash;
        response.checkpoint_card_previous_review_ids =
            card_column(&checkpoint.cards, |card| card.previous_review_id);
        response.checkpoint_card_previous_interval_days =
            card_column(&checkpoint.cards, |card| card.previous_interval_days);
        response.checkpoint_card_review_counts =
            card_column(&checkpoint.cards, |card| card.review_count);
    }
    response
}

/// One little-endian int64 per value.
pub(crate) fn i64_column(values: impl ExactSizeIterator<Item = i64>) -> Vec<u8> {
    let mut column = Vec::with_capacity(values.len() * 8);
    for value in values {
        column.extend_from_slice(&value.to_le_bytes());
    }
    column
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::card::Card;
    use crate::notes::NoteId;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;

    fn add_review(
        col: &mut Collection,
        card: &Card,
        review_id: i64,
        review_kind: RevlogReviewKind,
    ) -> Result<()> {
        col.storage.add_revlog_entry(
            &RevlogEntry {
                id: RevlogId(review_id),
                cid: card.id,
                usn: Usn(0),
                button_chosen: 3,
                interval: 1,
                ease_factor: 2_500,
                taken_millis: 1_000,
                review_kind,
                ..Default::default()
            },
            false,
        )?;
        Ok(())
    }

    fn inputs(col: &mut Collection, settings: &RwkvReplayInputsSettings) -> Result<RwkvReplayInputs> {
        let rows = col.storage.rwkv_historical_review_rows(&[])?.0;
        col.rwkv_replay_inputs_job(rows, &HashMap::new())?
            .encode(settings)
    }

    fn settings(ignored_review_ids: &[i64]) -> RwkvReplayInputsSettings<'_> {
        static REQUESTED: std::sync::OnceLock<HashMap<i64, bool>> = std::sync::OnceLock::new();
        RwkvReplayInputsSettings {
            ignored_review_ids,
            first_review_elapsed_source: FirstReviewElapsedSource::DeckConfig,
            first_review_uses_creation_by_config_id: REQUESTED.get_or_init(HashMap::new),
            hash_history: true,
            recovery_checkpoint_max_age_millis: 0,
        }
    }

    /// The fingerprint and the inputs hash the same history through the
    /// same encoder.
    #[test]
    fn inputs_hash_to_the_fingerprint() -> Result<()> {
        let mut col = Collection::new();
        let mut card = Card::new(NoteId(10), 0, DeckId(1), 0);
        col.add_card(&mut card)?;
        for (step, kind) in [
            RevlogReviewKind::Learning,
            RevlogReviewKind::Learning,
            RevlogReviewKind::Review,
            RevlogReviewKind::Relearning,
        ]
        .into_iter()
        .enumerate()
        {
            add_review(&mut col, &card, card.id.0 + 10_000 + step as i64 * 86_400_000, kind)?;
        }
        let inputs = inputs(&mut col, &settings(&[]))?;
        let fingerprint = col.rwkv_historical_review_fingerprint(Default::default())?;
        assert_eq!(inputs.reviews.len(), 4);
        assert_eq!(inputs.history_hash.as_deref(), Some(fingerprint.history_hash.as_str()));
        assert_eq!(inputs.reviews[0].card_type, 0);
        assert_eq!(inputs.reviews[0].elapsed_seconds, 10);
        assert_eq!(inputs.cards[0].review_count, 4);
        Ok(())
    }

    /// An ignored review leaves the rows after the start rows are found, as
    /// in Python: an ignored Learning start still starts the history, so the
    /// rows before it stay out.
    #[test]
    fn ignored_reviews_leave_after_the_start_rows_are_found() -> Result<()> {
        let mut col = Collection::new();
        let mut card = Card::new(NoteId(10), 0, DeckId(1), 0);
        col.add_card(&mut card)?;
        let first = card.id.0 + 10_000;
        add_review(&mut col, &card, first, RevlogReviewKind::Review)?;
        add_review(&mut col, &card, first + 1_000, RevlogReviewKind::Learning)?;
        add_review(&mut col, &card, first + 2_000, RevlogReviewKind::Review)?;
        let inputs = inputs(&mut col, &settings(&[first + 1_000, 5]))?;
        assert_eq!(inputs.active_ignored_review_ids, [first + 1_000]);
        let review_ids: Vec<i64> = inputs.reviews.iter().map(|review| review.review_id).collect();
        assert_eq!(review_ids, [first + 2_000]);
        Ok(())
    }

    /// Where Python reads the collection another way, the backend refuses,
    /// and Python builds the inputs itself.
    #[test]
    fn a_collection_python_reads_another_way_is_refused() -> Result<()> {
        let mut col = Collection::new();
        let mut card = Card::new(NoteId(10), 0, DeckId(1), 0);
        col.add_card(&mut card)?;
        add_review(&mut col, &card, card.id.0 + 10_000, RevlogReviewKind::Review)?;
        assert!(inputs(&mut col, &settings(&[])).is_ok());

        // a review id below 0: Python floors its division, Rust truncates
        add_review(&mut col, &card, -1_500, RevlogReviewKind::Review)?;
        assert!(inputs(&mut col, &settings(&[])).is_err());
        assert!(inputs(&mut col, &settings(&[-1_500])).is_ok());

        // a home deck that is gone: Python gives it no deck preset
        col.storage
            .db
            .execute("update cards set did = 987654321", [])?;
        assert!(inputs(&mut col, &settings(&[-1_500])).is_err());
        Ok(())
    }

    #[test]
    fn the_recovery_checkpoint_is_the_state_before_the_first_recent_review() -> Result<()> {
        let mut col = Collection::new();
        let mut first = Card::new(NoteId(10), 0, DeckId(1), 0);
        let mut second = Card::new(NoteId(11), 0, DeckId(1), 0);
        col.add_card(&mut first)?;
        col.add_card(&mut second)?;
        let day = 86_400_000;
        let start = first.id.0.max(second.id.0) + 10_000;
        add_review(&mut col, &first, start, RevlogReviewKind::Review)?;
        add_review(&mut col, &second, start + day, RevlogReviewKind::Review)?;
        add_review(&mut col, &first, start + 20 * day, RevlogReviewKind::Review)?;
        add_review(&mut col, &second, start + 21 * day, RevlogReviewKind::Review)?;
        let inputs = inputs(
            &mut col,
            &RwkvReplayInputsSettings {
                recovery_checkpoint_max_age_millis: 8 * day,
                ..settings(&[])
            },
        )?;
        let checkpoint = inputs.checkpoint.expect("a checkpoint");
        assert_eq!(checkpoint.review_count, 2);
        assert_eq!(checkpoint.last_review_id, start + day);
        assert_eq!(checkpoint.cards.len(), 2);
        assert_eq!(checkpoint.cards[0].review_count, 1);
        assert_eq!(checkpoint.history_hash.len(), 64);
        Ok(())
    }
}
