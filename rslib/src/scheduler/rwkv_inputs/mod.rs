// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The RWKV replay's inputs, built from the review log in Rust.
//!
//! Two stages, so that a model with another input layout changes only the
//! second:
//! - (a) `stream`: the raw per-review event stream. One event per rated review
//!   of the replay history, holding what the collection knows about it (answer
//!   time, show time, duration, rating, card, note, deck and preset ids, the
//!   replay start) and nothing a model derives.
//! - (b) `published`: the feature encoder of today's published model. It turns
//!   the stream into the model's per-review input records, the records Python's
//!   `_historical_rwkv_review_inputs` builds.
//!
//! What an encoder may read: the event at hand and its own state, which only
//! earlier events built. It computes the event's features from that state
//! first and advances the state with the event's answer after, so nothing
//! that depends on an answer (a grade mean, a sibling's entry) reaches the
//! features of the review it answers. The published model measures its
//! intervals from answer times (the review ids); an encoder that must not
//! see the answer measures them from `RwkvReviewEvent::show_millis`. A new
//! input layout is a new encoder beside `published` over the same stream,
//! and a new name for the state cache's layout tag (`_RWKV_FEATURE_LAYOUT`
//! in `qt/aqt/rwkv_scheduler.py`), so that states replayed with one encoder
//! are rebuilt rather than read by a model that expects another.
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
use crate::scheduler::rwkv::rwkv_sorted_review_ids;
use crate::scheduler::rwkv::RwkvCollectionHold;
use crate::scheduler::timing::SchedTimingToday;
use crate::storage::RwkvHistoricalReviewRow;

/// Everything the replay inputs read from the collection: the rows of the
/// whole history, read without the ignored reviews as the fingerprint reads
/// them, and what they need besides.
pub(crate) struct RwkvReplayInputsJob {
    rows: Vec<RwkvHistoricalReviewRow>,
    /// The ignored reviews that still belong to the rated history, as the
    /// reader gives them (`rwkv_active_ignored_review_ids`).
    active_ignored_review_ids: Vec<i64>,
    timing: SchedTimingToday,
    /// Each card's preset, as the stable id and as the backend's preset id,
    /// where an add-on rule can move cards; else every card's preset is its
    /// home deck's.
    presets_by_card: Option<HashMap<CardId, (i64, String)>>,
    decks_by_id: HashMap<DeckId, Deck>,
    configs_by_id: HashMap<DeckConfigId, DeckConfig>,
}

/// How the replay inputs are encoded; the fields of the request.
pub(crate) struct RwkvReplayInputsSettings<'a> {
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
    /// Each card's own preset, as `GetFsrsPresetIdsForCards` gives it.
    pub(crate) card_fsrs_preset_ids: Vec<String>,
    pub(crate) active_ignored_review_ids: Vec<i64>,
    pub(crate) history_hash: Option<String>,
    pub(crate) checkpoint: Option<RwkvReplayCheckpoint>,
}

impl Collection {
    /// What the replay inputs read besides the review log. `rows` and
    /// `active_ignored_review_ids` are what the reader gave.
    /// `stable_preset_ids` holds the stable ids Python gave the add-on
    /// presets.
    pub(crate) fn rwkv_replay_inputs_job(
        &mut self,
        rows: Vec<RwkvHistoricalReviewRow>,
        active_ignored_review_ids: Vec<i64>,
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
                        Ok((
                            card_id,
                            (
                                python_stable_preset_id(&preset.id, stable_preset_ids)?,
                                fsrs_preset_id_string(&preset.id),
                            ),
                        ))
                    })
                    .collect::<Result<HashMap<_, _>>>()?,
            )
        } else {
            None
        };
        Ok(RwkvReplayInputsJob {
            rows,
            active_ignored_review_ids,
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
            rows,
            active_ignored_review_ids,
            timing,
            presets_by_card,
            decks_by_id,
            configs_by_id,
        } = self;

        let mut checked_decks = HashSet::new();
        for row in &rows {
            require!(row.review_id >= 0, "RWKV replay review id below 0");
            // a deleted card's review has no deck to check
            let Some(deck_id) = row.deck_id else {
                continue;
            };
            if checked_decks.insert(deck_id) {
                let deck = decks_by_id.get(&DeckId(deck_id)).or_not_found(deck_id)?;
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
            match &presets_by_card {
                Some(by_card) => RwkvStreamPresets::ByCard(
                    by_card
                        .iter()
                        .map(|(card_id, (stable_id, _))| (*card_id, *stable_id))
                        .collect(),
                ),
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
        let cards = encoder.cards().to_vec();
        let deleted_cards: HashSet<i64> = reviews
            .iter()
            .filter(|review| review.deck_id.is_none())
            .map(|review| review.card_id)
            .collect();
        let card_fsrs_preset_ids = cards
            .iter()
            .map(|card| {
                if deleted_cards.contains(&card.card_id) {
                    // a card that is gone has no preset
                    return Some(String::new());
                }
                match &presets_by_card {
                    Some(by_card) => by_card
                        .get(&CardId(card.card_id))
                        .map(|(_, preset_id)| preset_id.clone()),
                    None => stream
                        .card_preset_id(card.card_id)
                        .map(|preset_id| preset_id.to_string()),
                }
            })
            .collect::<Option<Vec<_>>>()
            .or_invalid("a replay card without a preset")?;
        Ok(RwkvReplayInputs {
            reviews,
            cards,
            card_fsrs_preset_ids,
            active_ignored_review_ids,
            history_hash: history_hash.as_ref().map(RwkvHistoryHashChain::hex),
            checkpoint,
        })
    }
}

/// `GetFsrsPresetIdsForCards`' preset id.
fn fsrs_preset_id_string(preset_id: &FsrsPresetId) -> String {
    match preset_id {
        FsrsPresetId::DeckConfig(id) => id.0.to_string(),
        FsrsPresetId::Addon(id) => id.clone(),
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
///
/// The reader drops the ignored reviews before it finds each card's start
/// row, exactly as the fingerprint's read does (spec
/// sched.rwkv-replay-start-row), so the inputs and the fingerprint replay the
/// same history.
pub(crate) fn rwkv_replay_inputs_job_in_parts<T>(
    ignored_review_ids: &[RevlogId],
    stable_preset_ids: &HashMap<String, i64>,
    part_rows: usize,
    hold: &mut RwkvCollectionHold,
    mut extra: impl FnMut(&mut Collection) -> Result<T>,
) -> Result<(RwkvReplayInputsJob, T)> {
    if let Some((job, _)) = rwkv_historical_rows_in_parts(
        ignored_review_ids,
        part_rows,
        hold,
        |col, rows, active_ignored_review_ids| {
            Ok((
                col.rwkv_replay_inputs_job(rows, active_ignored_review_ids, stable_preset_ids)?,
                extra(col)?,
            ))
        },
    )? {
        return Ok(job);
    }
    let mut job = None;
    hold(&mut |col| {
        let (rows, active_ignored_review_ids) = col
            .storage
            .rwkv_historical_review_rows(ignored_review_ids)?;
        job = Some((
            col.rwkv_replay_inputs_job(rows, active_ignored_review_ids, stable_preset_ids)?,
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
    let (job, ()) = rwkv_replay_inputs_job_in_parts(
        &rwkv_sorted_review_ids(&input.ignored_review_ids),
        &input.stable_preset_ids,
        part_rows,
        hold,
        |_| Ok(()),
    )?;
    let inputs = job.encode(&RwkvReplayInputsSettings {
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
        card_fsrs_preset_ids,
        active_ignored_review_ids,
        history_hash,
        checkpoint,
    } = inputs;
    let review_column =
        |value: fn(&PublishedReviewInput) -> i64| i64_column(reviews.iter().map(value));
    let card_column = |cards: &[PublishedCardState], value: fn(&PublishedCardState) -> i64| {
        i64_column(cards.iter().map(value))
    };
    let mut response = RwkvHistoricalReviewInputsResponse {
        review_ids: review_column(|review| review.review_id),
        card_ids: review_column(|review| review.card_id),
        note_ids: review_column(|review| review.note_id.unwrap_or(0)),
        deck_ids: review_column(|review| review.deck_id.unwrap_or(0)),
        preset_ids: review_column(|review| review.preset_id.unwrap_or(0)),
        deleted_cards: reviews
            .iter()
            .map(|review| u8::from(review.deck_id.is_none()))
            .collect(),
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
        card_fsrs_preset_ids,
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
    use crate::scheduler::rwkv::rwkv_historical_review_rows_in_parts;

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

    /// The inputs, read in parts of two rows as the RPC reads them.
    fn inputs(
        col: &mut Collection,
        ignored_review_ids: &[i64],
        settings: &RwkvReplayInputsSettings,
    ) -> Result<RwkvReplayInputs> {
        let (job, ()) = rwkv_replay_inputs_job_in_parts(
            &rwkv_sorted_review_ids(ignored_review_ids),
            &HashMap::new(),
            2,
            &mut |step| step(col),
            |_| Ok(()),
        )?;
        job.encode(settings)
    }

    fn settings() -> RwkvReplayInputsSettings<'static> {
        static REQUESTED: std::sync::OnceLock<HashMap<i64, bool>> = std::sync::OnceLock::new();
        RwkvReplayInputsSettings {
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
            add_review(
                &mut col,
                &card,
                card.id.0 + 10_000 + step as i64 * 86_400_000,
                kind,
            )?;
        }
        let inputs = inputs(&mut col, &[], &settings())?;
        let fingerprint = col.rwkv_historical_review_fingerprint(Default::default())?;
        assert_eq!(inputs.reviews.len(), 4);
        assert_eq!(
            inputs.history_hash.as_deref(),
            Some(fingerprint.history_hash.as_str())
        );
        assert_eq!(inputs.reviews[0].card_type, 0);
        assert_eq!(inputs.reviews[0].elapsed_seconds, 10);
        assert_eq!(inputs.cards[0].review_count, 4);
        Ok(())
    }

    /// The RPC's inputs of the whole history, read in parts of `part_rows`.
    fn inputs_from_the_rpc(
        col: &mut Collection,
        ignored_review_ids: &[i64],
        part_rows: usize,
    ) -> Result<RwkvHistoricalReviewInputsResponse> {
        rwkv_historical_review_inputs(
            RwkvHistoricalReviewInputsRequest {
                ignored_review_ids: ignored_review_ids.to_vec(),
                hash_history: true,
                ..Default::default()
            },
            part_rows,
            &mut |step| step(col),
        )
    }

    fn i64_values(column: &[u8]) -> Vec<i64> {
        column
            .chunks_exact(8)
            .map(|bytes| i64::from_le_bytes(bytes.try_into().unwrap()))
            .collect()
    }

    /// The fingerprint accepts the inputs' history, asked as the state cache
    /// asks it: with the active ignored reviews the inputs gave, which the
    /// cache stores. It replays the same rows, from the same start rows.
    fn assert_the_fingerprint_accepts(
        col: &mut Collection,
        inputs: &RwkvHistoricalReviewInputsResponse,
    ) -> Result<()> {
        let fingerprint = col.rwkv_historical_review_fingerprint(
            anki_proto::scheduler::RwkvHistoricalReviewFingerprintRequest {
                ignored_review_ids: inputs.active_ignored_review_ids.clone(),
                expected_identity: Some(anki_proto::scheduler::RwkvHistoricalReviewIdentity {
                    last_review_id: inputs.last_review_id,
                    review_count: inputs.review_count,
                    history_hash: inputs.history_hash.clone(),
                }),
                ..Default::default()
            },
        )?;
        assert_eq!(fingerprint.history_hash, inputs.history_hash);
        assert_eq!(fingerprint.review_count, inputs.review_count);
        assert_eq!(
            fingerprint.active_ignored_review_ids,
            inputs.active_ignored_review_ids
        );
        assert!(fingerprint.history_is_valid);
        Ok(())
    }

    /// Pins spec sched.rwkv-replay-deleted-cards: the reviews of a card that
    /// is gone stay in the replay, in review order among the others, with no
    /// note, deck or preset, and the fingerprint replays the same history.
    #[test]
    fn a_deleted_cards_reviews_stay_in_the_replay_without_ids() -> Result<()> {
        let mut col = Collection::new();
        let mut card = Card::new(NoteId(10), 0, DeckId(1), 0);
        col.add_card(&mut card)?;
        // a card whose row is gone: its reviews are all that is left of it
        let deleted = Card {
            id: CardId(card.id.0 - 5_000),
            ..card.clone()
        };
        let first = card.id.0 + 10_000;
        add_review(&mut col, &deleted, first, RevlogReviewKind::Learning)?;
        add_review(&mut col, &card, first + 1_000, RevlogReviewKind::Learning)?;
        add_review(&mut col, &deleted, first + 2_000, RevlogReviewKind::Review)?;
        add_review(&mut col, &card, first + 3_000, RevlogReviewKind::Review)?;

        let response = inputs_from_the_rpc(&mut col, &[], 2)?;
        let inputs = &response;
        assert_eq!(inputs.review_count, 4);
        assert_eq!(
            i64_values(&inputs.card_ids),
            [deleted.id.0, card.id.0, deleted.id.0, card.id.0]
        );
        assert_eq!(inputs.deleted_cards, [1, 0, 1, 0]);
        assert_eq!(i64_values(&inputs.note_ids), [0, 10, 0, 10]);
        assert_eq!(i64_values(&inputs.deck_ids), [0, 1, 0, 1]);
        assert_eq!(i64_values(&inputs.preset_ids)[0], 0);
        assert_eq!(i64_values(&inputs.cards), [deleted.id.0, card.id.0]);
        assert_eq!(inputs.card_fsrs_preset_ids, ["", "1"]);
        // the deleted card's second review measures from its first
        assert_eq!(i64_values(&inputs.elapsed_seconds)[2], 2);
        assert_the_fingerprint_accepts(&mut col, inputs)?;

        let job = self::inputs(&mut col, &[], &settings())?;
        let review = &job.reviews[0];
        assert_eq!(
            (review.note_id, review.deck_id, review.preset_id),
            (None, None, None)
        );
        let mut packed = Vec::new();
        review.write_packed_row(&mut packed);
        // bits 0-2 (note, deck, preset) clear, bits 3-8 set
        assert_eq!(&packed[..4], &0x1f8u32.to_le_bytes());
        packed.clear();
        job.reviews[1].write_packed_row(&mut packed);
        assert_eq!(&packed[..4], &0x1ffu32.to_le_bytes());
        Ok(())
    }

    /// Pins spec sched.rwkv-replay-start-row: the ignored reviews leave the
    /// rows BEFORE the start rows are found, as the fingerprint drops them.
    /// An ignored Learning start is no start: the card with no other Learning
    /// row starts at its first rated row, as training would start it, and the
    /// fingerprint replays the same rows from the same start.
    #[test]
    fn an_ignored_learning_start_is_dropped_before_the_start_row_is_found() -> Result<()> {
        let mut col = Collection::new();
        let mut card = Card::new(NoteId(10), 0, DeckId(1), 0);
        col.add_card(&mut card)?;
        let first = card.id.0 + 10_000;
        add_review(&mut col, &card, first, RevlogReviewKind::Review)?;
        add_review(&mut col, &card, first + 1_000, RevlogReviewKind::Learning)?;
        add_review(&mut col, &card, first + 2_000, RevlogReviewKind::Review)?;
        let ignored = [first + 1_000, 5];

        let inputs = inputs(&mut col, &ignored, &settings())?;
        assert_eq!(inputs.active_ignored_review_ids, [first + 1_000]);
        let review_ids: Vec<i64> = inputs
            .reviews
            .iter()
            .map(|review| review.review_id)
            .collect();
        assert_eq!(review_ids, [first, first + 2_000]);
        // the first rated row is the start row: the learn-start state code
        assert_eq!(inputs.reviews[0].card_type, 0);
        assert_ne!(inputs.reviews[1].card_type, 0);

        for part_rows in [1, 2, 1_000] {
            let rpc = inputs_from_the_rpc(&mut col, &ignored, part_rows)?;
            assert_eq!(i64_values(&rpc.review_ids), [first, first + 2_000]);
            assert_eq!(Some(&rpc.history_hash), inputs.history_hash.as_ref());
            assert_eq!(rpc.active_ignored_review_ids, [first + 1_000]);
            assert_the_fingerprint_accepts(&mut col, &rpc)?;
        }

        // the rows the backend hands Python for its own build, whole and in
        // parts, start at the same row
        for part_rows in [1, 3, 1_000] {
            let rows = rwkv_historical_review_rows_in_parts(
                &rwkv_sorted_review_ids(&ignored),
                part_rows,
                &mut |step| step(&mut col),
            )?;
            let starts: Vec<(i64, bool)> = rows
                .iter()
                .map(|row| (row.review_id, row.is_learning_start))
                .collect();
            assert_eq!(starts, [(first, true), (first + 2_000, false)]);
        }
        Ok(())
    }

    /// An ignored review between two Learning runs joins them into one: the
    /// later run is no start any more, so the card starts at the earlier
    /// one, as the fingerprint starts it.
    #[test]
    fn an_ignored_review_between_two_learning_runs_joins_them() -> Result<()> {
        let mut col = Collection::new();
        let mut card = Card::new(NoteId(10), 0, DeckId(1), 0);
        col.add_card(&mut card)?;
        let first = card.id.0 + 10_000;
        add_review(&mut col, &card, first, RevlogReviewKind::Learning)?;
        add_review(&mut col, &card, first + 1_000, RevlogReviewKind::Review)?;
        add_review(&mut col, &card, first + 2_000, RevlogReviewKind::Learning)?;
        add_review(&mut col, &card, first + 3_000, RevlogReviewKind::Review)?;
        let ignored = [first + 1_000];
        let rpc = inputs_from_the_rpc(&mut col, &ignored, 2)?;
        assert_eq!(
            i64_values(&rpc.review_ids),
            [first, first + 2_000, first + 3_000]
        );
        assert_the_fingerprint_accepts(&mut col, &rpc)?;
        Ok(())
    }

    /// The inputs as they were built before the ignored reviews left the
    /// rows before the start rows: the start rows found on every rated row,
    /// and the ignored reviews dropped after.
    fn inputs_with_the_ignored_reviews_dropped_after_the_start_rows(
        col: &mut Collection,
        ignored_review_ids: &[i64],
    ) -> Result<RwkvReplayInputs> {
        let mut rows = col.storage.rwkv_historical_review_rows(&[])?.0;
        let mut active_ignored_review_ids = Vec::new();
        rows.retain(|row| {
            let keep = !ignored_review_ids.contains(&row.review_id);
            if !keep {
                active_ignored_review_ids.push(row.review_id);
            }
            keep
        });
        col.rwkv_replay_inputs_job(rows, active_ignored_review_ids, &HashMap::new())?
            .encode(&settings())
    }

    /// Where no ignored review decides a start row, the history is the one
    /// the inputs gave before the ignored reviews left the rows first: an
    /// ignored review after the start row with no Learning row after it, an
    /// ignored review before a Learning start, and a review id that is in no
    /// review log.
    #[test]
    fn an_ignored_review_that_decides_no_start_row_leaves_the_history_unchanged() -> Result<()> {
        let mut col = Collection::new();
        let mut first_card = Card::new(NoteId(10), 0, DeckId(1), 0);
        let mut second_card = Card::new(NoteId(11), 0, DeckId(1), 0);
        col.add_card(&mut first_card)?;
        col.add_card(&mut second_card)?;
        let first = first_card.id.0.max(second_card.id.0) + 10_000;
        // Learning start, Review, Review (ignored), Relearning
        add_review(&mut col, &first_card, first, RevlogReviewKind::Learning)?;
        add_review(
            &mut col,
            &first_card,
            first + 1_000,
            RevlogReviewKind::Review,
        )?;
        add_review(
            &mut col,
            &first_card,
            first + 2_000,
            RevlogReviewKind::Review,
        )?;
        add_review(
            &mut col,
            &first_card,
            first + 3_000,
            RevlogReviewKind::Relearning,
        )?;
        // Review (ignored), Learning start, Review
        add_review(
            &mut col,
            &second_card,
            first + 500,
            RevlogReviewKind::Review,
        )?;
        add_review(
            &mut col,
            &second_card,
            first + 1_500,
            RevlogReviewKind::Learning,
        )?;
        add_review(
            &mut col,
            &second_card,
            first + 2_500,
            RevlogReviewKind::Review,
        )?;
        let ignored = [first + 500, first + 2_000, 7];

        let before =
            inputs_with_the_ignored_reviews_dropped_after_the_start_rows(&mut col, &ignored)?;
        let after = inputs(&mut col, &ignored, &settings())?;
        assert_eq!(after.reviews, before.reviews);
        assert_eq!(after.cards, before.cards);
        assert_eq!(after.history_hash, before.history_hash);
        let review_ids: Vec<i64> = after
            .reviews
            .iter()
            .map(|review| review.review_id)
            .collect();
        assert_eq!(
            review_ids,
            [
                first,
                first + 1_000,
                first + 1_500,
                first + 2_500,
                first + 3_000
            ]
        );
        // the ignored review before the second card's start row is in no
        // history, but it is still a rated review of the card, so it is
        // active, as the fingerprint counts it
        assert_eq!(before.active_ignored_review_ids, [first + 2_000]);
        assert_eq!(
            after.active_ignored_review_ids,
            [first + 500, first + 2_000]
        );
        let rpc = inputs_from_the_rpc(&mut col, &ignored, 3)?;
        assert_the_fingerprint_accepts(&mut col, &rpc)?;
        Ok(())
    }

    /// Where Python reads the collection another way, the backend refuses,
    /// and Python builds the inputs itself.
    #[test]
    fn a_collection_python_reads_another_way_is_refused() -> Result<()> {
        let mut col = Collection::new();
        let mut card = Card::new(NoteId(10), 0, DeckId(1), 0);
        col.add_card(&mut card)?;
        add_review(
            &mut col,
            &card,
            card.id.0 + 10_000,
            RevlogReviewKind::Review,
        )?;
        assert!(inputs(&mut col, &[], &settings()).is_ok());

        // a review id below 0: Python floors its division, Rust truncates
        add_review(&mut col, &card, -1_500, RevlogReviewKind::Review)?;
        assert!(inputs(&mut col, &[], &settings()).is_err());
        assert!(inputs(&mut col, &[-1_500], &settings()).is_ok());

        // a home deck that is gone: Python gives it no deck preset
        col.storage
            .db
            .execute("update cards set did = 987654321", [])?;
        assert!(inputs(&mut col, &[-1_500], &settings()).is_err());
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
        add_review(
            &mut col,
            &second,
            start + 21 * day,
            RevlogReviewKind::Review,
        )?;
        let inputs = inputs(
            &mut col,
            &[],
            &RwkvReplayInputsSettings {
                recovery_checkpoint_max_age_millis: 8 * day,
                ..settings()
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
