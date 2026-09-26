// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The RWKV replay's inputs, built from the review log in Rust. This is the
//! one builder of them: Python only reads the rows where the backend cannot
//! (`RwkvReplayInputsFromRowsRequest`) and turns the result into its review
//! inputs one at a time, where it needs one.
//!
//! Two stages, so that a model with another input layout changes only the
//! second:
//! - (a) `stream`: the raw per-review event stream. One event per rated review
//!   of the replay history, holding what the collection knows about it (answer
//!   time, show time, duration, rating, card, note, deck and preset ids, the
//!   replay start) and nothing a model derives.
//! - (b) an encoder per input layout (`RwkvFeatureLayout`): `published`, the
//!   feature encoder of today's published model. It turns the stream into the
//!   model's per-review input records and their packed warm-up rows.
//!
//! What an encoder may read: the event at hand, what the replay knows of the
//! card's earlier reviews (`RwkvReplayCard`) and its own state, which only
//! earlier events built. It computes the event's features from that state
//! first and advances the state with the event's answer after, so nothing
//! that depends on an answer (a grade mean, a sibling's entry) reaches the
//! features of the review it answers. The published model measures its
//! intervals from answer times (the review ids); an encoder that must not
//! see the answer measures them from `RwkvReviewEvent::show_millis`. A new
//! input layout is a new `RwkvFeatureLayout`, a new encoder beside
//! `published` over the same stream (`RwkvReplayEncoder`), and a new name for
//! the state cache's layout tag (`_RWKV_FEATURE_LAYOUT` in
//! `qt/aqt/rwkv_scheduler.py`), so that states replayed with one encoder are
//! rebuilt rather than read by a model that expects another. An encoder with
//! state beyond the card's (a model's per-collection counters) also gives
//! that state to `RwkvReplaySeed`, so that a read after a cutoff goes on from
//! it.
//!
//! The replay (`replay`) runs the rows in order, the way Python's build ran
//! them: it counts every row, replays the rows after the cutoff that it can
//! encode, and keeps what the state cache stores (the per-card state, the
//! history hash, the recovery checkpoint).

pub(crate) mod published;
pub(crate) mod stream;

use std::collections::HashMap;
use std::collections::HashSet;

use anki_proto::scheduler::rwkv_historical_review_inputs_request::FirstReviewElapsedSource;
use anki_proto::scheduler::RwkvHistoricalReviewInputsRequest;
use anki_proto::scheduler::RwkvHistoricalReviewInputsResponse;
use anki_proto::scheduler::RwkvReplayInputsFromRowsRequest;
use fnv::FnvHashMap;
use fnv::FnvHashSet;
use prost::Message;

use self::published::PublishedEncoder;
use self::published::PublishedReviewInput;
use self::published::RwkvFirstReviewElapsed;
use self::published::RwkvHistoryHashChain;
use self::published::RwkvReplayDays;
use self::published::PACKED_WARM_UP_ROW_BYTES;
use self::stream::RwkvHistoricalPresetRoute;
use self::stream::RwkvReviewEvent;
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

/// The input layout a model reads, named as the state cache names it
/// (`_RWKV_FEATURE_LAYOUT`). Each has its own encoder.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RwkvFeatureLayout {
    /// Today's published model: 92 input features (`published`).
    Published92,
}

impl RwkvFeatureLayout {
    /// The layout of a tag; the empty tag is today's published layout.
    pub(crate) fn from_tag(tag: &str) -> Result<Self> {
        match tag {
            "" | "published-92" => Ok(Self::Published92),
            other => invalid_input!("unknown RWKV feature layout {other:?}"),
        }
    }
}

/// An encoder of one input layout: the record of each event, in order.
pub(crate) trait RwkvReplayEncoder {
    type Record: RwkvReplayRecord;

    /// The record of `event`. `card` is what the replay knows of the card's
    /// earlier reviews; the replay advances it after this.
    fn encode(&mut self, event: &RwkvReviewEvent, card: &RwkvReplayCard) -> Self::Record;
}

/// One review's record in some input layout.
pub(crate) trait RwkvReplayRecord {
    fn review_id(&self) -> i64;
    /// The bytes the history hash chains (`RwkvHistoryHashChain`).
    fn write_delta_record(&self, out: &mut Vec<u8>);
    /// The row the model is warmed up with.
    fn write_packed_row(&self, out: &mut Vec<u8>);
}

/// What the replay knows of a card's earlier reviews: Python's three
/// per-card maps, each on its own, since a state the read goes on from may
/// hold a card in one and not another.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct RwkvReplayCard {
    pub(crate) previous_review_id: Option<i64>,
    pub(crate) previous_interval_days: Option<i64>,
    pub(crate) review_count: Option<i64>,
}

/// A card the read replayed, with its state after the read (or at the
/// checkpoint).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct RwkvReplayCardState {
    pub(crate) card_id: i64,
    pub(crate) previous_review_id: i64,
    pub(crate) previous_interval_days: i64,
    pub(crate) review_count: i64,
}

/// The state a read goes on from: the state cache's per-card maps and hash
/// after its last review.
#[derive(Debug, Default)]
pub(crate) struct RwkvReplaySeed {
    pub(crate) previous_review_ids: Vec<(i64, i64)>,
    pub(crate) previous_interval_days: Vec<(i64, i64)>,
    pub(crate) review_counts: Vec<(i64, i64)>,
    /// None: the empty history's.
    pub(crate) history_hash: Option<String>,
    /// The reviews up to the cutoff are counted from `review_counts`
    /// instead of from the rows (a read of the rows after a cutoff).
    pub(crate) counts_before_cutoff: bool,
}

/// How the replay runs, besides its rows.
pub(crate) struct RwkvReplaySettings {
    pub(crate) hash_history: bool,
    /// Above 0: also the state before the first review newer than the
    /// newest row less this many milliseconds.
    pub(crate) recovery_checkpoint_max_age_millis: i64,
    /// The rows up to it are counted and not replayed.
    pub(crate) after_review_id: Option<i64>,
    pub(crate) seed: RwkvReplaySeed,
}

/// The rows a replay runs and what their events need.
pub(crate) struct RwkvReplayRows<'a> {
    pub(crate) rows: Vec<RwkvHistoricalReviewRow>,
    /// None: every row can be encoded. Else false where a row is counted
    /// and not replayed.
    pub(crate) encodable: Option<Vec<bool>>,
    pub(crate) presets: RwkvStreamPresets<'a>,
    pub(crate) routes: &'a [RwkvHistoricalPresetRoute],
}

/// The state of the replay before one of its reviews.
pub(crate) struct RwkvReplayCheckpoint {
    /// The reviews this read replayed before it.
    pub(crate) review_count: usize,
    pub(crate) last_review_id: i64,
    pub(crate) history_hash: String,
    /// The cards the read had replayed by then, with their state then.
    pub(crate) cards: Vec<RwkvReplayCardState>,
}

/// A replay's records and what the state cache keeps of it.
pub(crate) struct RwkvReplay<R> {
    pub(crate) reviews: Vec<R>,
    /// Each review's review log values, whatever the layout: its type,
    /// interval and ease factor.
    pub(crate) review_log: Vec<[i64; 3]>,
    /// Every card this read replayed, in the order it first reached them,
    /// with its state after the read.
    pub(crate) cards: Vec<RwkvReplayCardState>,
    /// Each of `cards`' own preset, before any route overrides it; None for
    /// a card that is gone or has none.
    pub(crate) card_own_preset_ids: Vec<Option<i64>>,
    pub(crate) last_review_id: i64,
    pub(crate) review_count: u64,
    pub(crate) history_hash: Option<String>,
    pub(crate) checkpoint: Option<RwkvReplayCheckpoint>,
}

/// Runs the rows in order, as Python's `_historical_rwkv_review_inputs` ran
/// them: every row counts towards `review_count`; a row after the cutoff
/// that can be encoded is replayed, from what the replay knows of its card.
pub(crate) fn replay<E: RwkvReplayEncoder>(
    rows: RwkvReplayRows,
    settings: &RwkvReplaySettings,
    encoder: &mut E,
) -> Result<RwkvReplay<E::Record>> {
    let RwkvReplayRows {
        rows,
        encodable,
        presets,
        routes,
    } = rows;
    let seed = &settings.seed;
    let mut cards: FnvHashMap<i64, RwkvReplayCard> = FnvHashMap::default();
    for &(card_id, review_id) in &seed.previous_review_ids {
        cards.entry(card_id).or_default().previous_review_id = Some(review_id);
    }
    for &(card_id, interval_days) in &seed.previous_interval_days {
        cards.entry(card_id).or_default().previous_interval_days = Some(interval_days);
    }
    for &(card_id, review_count) in &seed.review_counts {
        cards.entry(card_id).or_default().review_count = Some(review_count);
    }
    let mut history_hash = match (settings.hash_history, &seed.history_hash) {
        (false, _) => None,
        (true, None) => Some(RwkvHistoryHashChain::new()),
        (true, Some(hex)) => Some(RwkvHistoryHashChain::from_hex(hex)?),
    };
    let after = settings.after_review_id;
    let recovery_cutoff_review_id = (settings.recovery_checkpoint_max_age_millis > 0)
        .then(|| {
            rows.iter()
                .rev()
                .find(|row| after.map_or(true, |after| row.review_id > after))
                .map(|row| row.review_id - settings.recovery_checkpoint_max_age_millis)
        })
        .flatten();

    let mut stream = RwkvReviewStream::new(presets, routes);
    let mut reviews: Vec<E::Record> = Vec::with_capacity(rows.len());
    let mut review_log = Vec::with_capacity(rows.len());
    // the cards this read replayed, in the order it first reached them
    let mut replayed_cards: Vec<i64> = Vec::new();
    let mut replayed: FnvHashSet<i64> = FnvHashSet::default();
    let mut checkpoint = None;
    let mut last_review_id = after.unwrap_or(0);
    // Python's counts: `retained` counts every row, and the count a read
    // reports is `retained` as of the last row it replayed, or of a later
    // row no newer than that one
    let mut retained: u64 = if after.is_some() && seed.counts_before_cutoff {
        seed.review_counts
            .iter()
            .map(|&(_, count)| count)
            .sum::<i64>()
            .try_into()
            .or_invalid("RWKV replay review counts below 0")?
    } else {
        0
    };
    let mut review_count = retained;
    for (index, row) in rows.into_iter().enumerate() {
        retained += 1;
        if row.review_id <= last_review_id {
            review_count = retained;
        }
        if after.is_some_and(|after| row.review_id <= after) {
            continue;
        }
        if encodable
            .as_ref()
            .is_some_and(|encodable| !encodable[index])
        {
            continue;
        }
        if let (Some(cutoff), Some(previous)) = (recovery_cutoff_review_id, reviews.last()) {
            if row.review_id > cutoff && checkpoint.is_none() {
                checkpoint = Some(RwkvReplayCheckpoint {
                    review_count: reviews.len(),
                    last_review_id: previous.review_id(),
                    history_hash: history_hash
                        .as_ref()
                        .map(RwkvHistoryHashChain::hex)
                        .unwrap_or_default(),
                    cards: card_states(&replayed_cards, &cards),
                });
            }
        }
        let review_id = row.review_id;
        let card_id = row.card_id;
        let interval_days = row.interval_days;
        review_log.push([row.review_kind, row.interval_days, row.ease_factor]);
        let card = cards.entry(card_id).or_default();
        let event = stream.event(
            row,
            card.review_count.unwrap_or(0),
            card.previous_interval_days.unwrap_or(0),
        )?;
        let record = encoder.encode(&event, card);
        card.previous_review_id = Some(review_id);
        card.previous_interval_days = Some(interval_days);
        card.review_count = Some(card.review_count.unwrap_or(0) + 1);
        if replayed.insert(card_id) {
            replayed_cards.push(card_id);
        }
        if let Some(history_hash) = &mut history_hash {
            history_hash.update(&record);
        }
        reviews.push(record);
        last_review_id = last_review_id.max(review_id);
        review_count = retained;
    }
    let card_states = card_states(&replayed_cards, &cards);
    let card_own_preset_ids = card_states
        .iter()
        .map(|card| stream.card_preset_id(card.card_id))
        .collect();
    Ok(RwkvReplay {
        reviews,
        review_log,
        cards: card_states,
        card_own_preset_ids,
        last_review_id,
        review_count,
        history_hash: history_hash.as_ref().map(RwkvHistoryHashChain::hex),
        checkpoint,
    })
}

/// The replayed cards with their state now, in the order given.
fn card_states(
    card_ids: &[i64],
    cards: &FnvHashMap<i64, RwkvReplayCard>,
) -> Vec<RwkvReplayCardState> {
    card_ids
        .iter()
        .map(|card_id| {
            let card = cards[card_id];
            RwkvReplayCardState {
                card_id: *card_id,
                previous_review_id: card.previous_review_id.unwrap_or(0),
                previous_interval_days: card.previous_interval_days.unwrap_or(0),
                review_count: card.review_count.unwrap_or(0),
            }
        })
        .collect()
}

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

/// How the replay inputs of the whole history are encoded; the fields of
/// the request.
pub(crate) struct RwkvReplayInputsSettings<'a> {
    pub(crate) first_review_elapsed_source: FirstReviewElapsedSource,
    pub(crate) first_review_uses_creation_by_config_id: &'a HashMap<i64, bool>,
    pub(crate) hash_history: bool,
    /// Above 0: also the state before the first review newer than the
    /// newest review less this many milliseconds.
    pub(crate) recovery_checkpoint_max_age_millis: i64,
}

/// The whole history's replay inputs in today's published layout.
pub(crate) struct RwkvReplayInputs {
    pub(crate) replay: RwkvReplay<PublishedReviewInput>,
    /// Each card's own preset, as `GetFsrsPresetIdsForCards` gives it.
    pub(crate) card_fsrs_preset_ids: Vec<String>,
    pub(crate) active_ignored_review_ids: Vec<i64>,
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
    /// does, so that the caller reads the rows and the presets itself and
    /// hands them to the same encoder (`RwkvReplayInputsFromRowsRequest`): a
    /// review id below 0, a home deck that is missing or filtered or whose
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

        let replay = replay(
            RwkvReplayRows {
                rows,
                encodable: None,
                presets: match &presets_by_card {
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
                routes: &[],
            },
            &RwkvReplaySettings {
                hash_history: settings.hash_history,
                recovery_checkpoint_max_age_millis: settings.recovery_checkpoint_max_age_millis,
                after_review_id: None,
                seed: RwkvReplaySeed::default(),
            },
            &mut PublishedEncoder::new(
                RwkvReplayDays::from(&timing),
                match settings.first_review_elapsed_source {
                    FirstReviewElapsedSource::DeckConfig => RwkvFirstReviewElapsed::DeckConfig {
                        decks_by_id: &decks_by_id,
                        configs_by_id: &configs_by_id,
                        requested: settings.first_review_uses_creation_by_config_id,
                    },
                    FirstReviewElapsedSource::CardCreation => RwkvFirstReviewElapsed::CardCreation,
                    FirstReviewElapsedSource::Missing => RwkvFirstReviewElapsed::Missing,
                },
            ),
        )?;
        let card_fsrs_preset_ids = replay
            .cards
            .iter()
            .zip(&replay.card_own_preset_ids)
            .map(|(card, own_preset_id)| match &presets_by_card {
                Some(by_card) => by_card
                    .get(&CardId(card.card_id))
                    .map(|(_, preset_id)| preset_id.clone()),
                None => own_preset_id.map(|preset_id| preset_id.to_string()),
            })
            .collect::<Vec<_>>();
        let deleted_cards: HashSet<i64> = replay
            .reviews
            .iter()
            .filter(|review| review.deck_id.is_none())
            .map(|review| review.card_id)
            .collect();
        let card_fsrs_preset_ids = replay
            .cards
            .iter()
            .zip(card_fsrs_preset_ids)
            .map(|(card, preset_id)| {
                if deleted_cards.contains(&card.card_id) {
                    // a card that is gone has no preset
                    Some(String::new())
                } else {
                    preset_id
                }
            })
            .collect::<Option<Vec<_>>>()
            .or_invalid("a replay card without a preset")?;
        Ok(RwkvReplayInputs {
            replay,
            card_fsrs_preset_ids,
            active_ignored_review_ids,
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

/// The RPC: the whole history's inputs in the layout asked for.
pub(crate) fn rwkv_historical_review_inputs(
    input: RwkvHistoricalReviewInputsRequest,
    part_rows: usize,
    hold: &mut RwkvCollectionHold,
) -> Result<RwkvHistoricalReviewInputsResponse> {
    let started = std::time::Instant::now();
    let layout = RwkvFeatureLayout::from_tag(&input.feature_layout)?;
    let (job, ()) = rwkv_replay_inputs_job_in_parts(
        &rwkv_sorted_review_ids(&input.ignored_review_ids),
        &input.stable_preset_ids,
        part_rows,
        hold,
        |_| Ok(()),
    )?;
    let response = match layout {
        RwkvFeatureLayout::Published92 => {
            let inputs = job.encode(&RwkvReplayInputsSettings {
                first_review_elapsed_source: input.first_review_elapsed_source(),
                first_review_uses_creation_by_config_id: &input
                    .first_review_uses_creation_by_config_id,
                hash_history: input.hash_history,
                recovery_checkpoint_max_age_millis: input.recovery_checkpoint_max_age_millis,
            })?;
            let mut response = inputs_response(&inputs.replay, PACKED_WARM_UP_ROW_BYTES);
            response.active_ignored_review_ids = inputs.active_ignored_review_ids;
            response.card_fsrs_preset_ids = inputs.card_fsrs_preset_ids;
            response
        }
    };
    tracing::debug!(
        reviews = response.review_ids.len() / 8,
        elapsed_ms = started.elapsed().as_secs_f64() * 1000.0,
        "built RWKV replay inputs in Rust"
    );
    Ok(response)
}

/// The inputs of rows the caller read itself
/// (`RwkvReplayInputsFromRowsRequest`), encoded as the whole history's are.
/// Reads no collection.
pub fn rwkv_replay_inputs_from_rows(request: &[u8]) -> Result<Vec<u8>> {
    let input = RwkvReplayInputsFromRowsRequest::decode(request)?;
    let layout = RwkvFeatureLayout::from_tag(&input.feature_layout)?;
    let count = input.review_ids.len() / 8;
    let int64s = |column: &[u8], name: &str| -> Result<Vec<i64>> {
        require!(
            column.len() % 8 == 0,
            "RWKV replay column {name} is not int64s"
        );
        Ok(i64_values(column))
    };
    let review_ids = int64s(&input.review_ids, "review_ids")?;
    let columns = [
        (&input.card_ids, "card_ids"),
        (&input.note_ids, "note_ids"),
        (&input.deck_ids, "deck_ids"),
        (&input.eases, "eases"),
        (&input.durations_millis, "durations_millis"),
        (&input.review_kinds, "review_kinds"),
        (&input.interval_days, "interval_days"),
        (&input.ease_factors, "ease_factors"),
    ]
    .into_iter()
    .map(|(column, name)| {
        let values = int64s(column, name)?;
        require!(
            values.len() == count,
            "RWKV replay column {name} has another length"
        );
        Ok(values)
    })
    .collect::<Result<Vec<_>>>()?;
    for (flags, name) in [
        (&input.learning_starts, "learning_starts"),
        (&input.has_note, "has_note"),
        (&input.has_deck, "has_deck"),
        (&input.encodable, "encodable"),
    ] {
        require!(
            flags.len() == count,
            "RWKV replay column {name} has another length"
        );
    }
    let rows = (0..count)
        .map(|index| RwkvHistoricalReviewRow {
            review_id: review_ids[index],
            card_id: columns[0][index],
            note_id: (input.has_note[index] != 0).then_some(columns[1][index]),
            deck_id: (input.has_deck[index] != 0).then_some(columns[2][index]),
            ease: columns[3][index],
            duration_millis: columns[4][index],
            review_kind: columns[5][index],
            interval_days: columns[6][index],
            ease_factor: columns[7][index],
            is_learning_start: input.learning_starts[index] != 0,
        })
        .collect();
    let encodable = input.encodable.iter().map(|&flag| flag != 0).collect();

    let preset_card_ids = int64s(&input.preset_card_ids, "preset_card_ids")?;
    let preset_ids = int64s(&input.preset_ids, "preset_ids")?;
    require!(
        preset_ids.len() == preset_card_ids.len()
            && input.has_preset.len() == preset_card_ids.len(),
        "RWKV replay presets have another length"
    );
    let presets = preset_card_ids
        .iter()
        .zip(&preset_ids)
        .zip(&input.has_preset)
        .map(|((&card_id, &preset_id), &has_preset)| {
            (CardId(card_id), (has_preset != 0).then_some(preset_id))
        })
        .collect();
    let routes = input
        .routes
        .iter()
        .map(|route| {
            Ok(RwkvHistoricalPresetRoute {
                stable_preset_id: route.stable_preset_id,
                card_ids: if route.has_card_ids {
                    Some(
                        int64s(&route.card_ids, "route card_ids")?
                            .into_iter()
                            .map(CardId)
                            .collect(),
                    )
                } else {
                    None
                },
                min_reps: route.min_reps,
                max_reps: route.max_reps,
                min_interval_days: route.min_interval_days,
                max_interval_days: route.max_interval_days,
            })
        })
        .collect::<Result<Vec<_>>>()?;
    let pairs = |cards: &[u8], values: &[u8], name: &str| -> Result<Vec<(i64, i64)>> {
        let cards = int64s(cards, name)?;
        let values = int64s(values, name)?;
        require!(
            cards.len() == values.len(),
            "RWKV replay seed {name} has another length"
        );
        Ok(cards.into_iter().zip(values).collect())
    };
    let settings = RwkvReplaySettings {
        hash_history: input.hash_history,
        recovery_checkpoint_max_age_millis: input.recovery_checkpoint_max_age_millis,
        after_review_id: input.after_review_id,
        seed: RwkvReplaySeed {
            previous_review_ids: pairs(
                &input.seed_previous_review_id_cards,
                &input.seed_previous_review_ids,
                "previous review ids",
            )?,
            previous_interval_days: pairs(
                &input.seed_previous_interval_cards,
                &input.seed_previous_interval_days,
                "previous intervals",
            )?,
            review_counts: pairs(
                &input.seed_review_count_cards,
                &input.seed_review_counts,
                "review counts",
            )?,
            history_hash: (!input.previous_history_hash.is_empty())
                .then(|| input.previous_history_hash.clone()),
            counts_before_cutoff: input.counts_before_cutoff,
        },
    };
    let days = RwkvReplayDays {
        days_elapsed: input.days_elapsed,
        next_day_at: input.next_day_at,
    };
    let rows = RwkvReplayRows {
        rows,
        encodable: Some(encodable),
        presets: RwkvStreamPresets::ByCardOrNone(presets),
        routes: &routes,
    };
    let response = match layout {
        RwkvFeatureLayout::Published92 => {
            let first_review = match input.first_review_elapsed_source() {
                FirstReviewElapsedSource::DeckConfig => {
                    RwkvFirstReviewElapsed::ByDeck(&input.first_review_uses_creation_by_deck_id)
                }
                FirstReviewElapsedSource::CardCreation => RwkvFirstReviewElapsed::CardCreation,
                FirstReviewElapsedSource::Missing => RwkvFirstReviewElapsed::Missing,
            };
            inputs_response(
                &replay(
                    rows,
                    &settings,
                    &mut PublishedEncoder::new(days, first_review),
                )?,
                PACKED_WARM_UP_ROW_BYTES,
            )
        }
    };
    Ok(response.encode_to_vec())
}

/// The response of a replay, without the fields only the backend's read of
/// the collection knows (the active ignored reviews, the cards' presets).
fn inputs_response<R: RwkvReplayRecord>(
    replay: &RwkvReplay<R>,
    packed_row_bytes: usize,
) -> RwkvHistoricalReviewInputsResponse {
    let reviews = &replay.reviews;
    let log_column = |index: usize| i64_column(replay.review_log.iter().map(|log| log[index]));
    let card_column = |cards: &[RwkvReplayCardState], value: fn(&RwkvReplayCardState) -> i64| {
        i64_column(cards.iter().map(value))
    };
    let mut packed_rows = Vec::with_capacity(reviews.len() * packed_row_bytes);
    for review in reviews {
        review.write_packed_row(&mut packed_rows);
    }
    let mut response = RwkvHistoricalReviewInputsResponse {
        last_review_id: replay.last_review_id,
        review_count: replay.review_count,
        history_hash: replay.history_hash.clone().unwrap_or_default(),
        packed_rows,
        review_ids: i64_column(reviews.iter().map(RwkvReplayRecord::review_id)),
        review_kinds: log_column(0),
        interval_days: log_column(1),
        ease_factors: log_column(2),
        cards: card_column(&replay.cards, |card| card.card_id),
        card_previous_review_ids: card_column(&replay.cards, |card| card.previous_review_id),
        card_previous_interval_days: card_column(&replay.cards, |card| card.previous_interval_days),
        card_review_counts: card_column(&replay.cards, |card| card.review_count),
        ..Default::default()
    };
    if let Some(checkpoint) = &replay.checkpoint {
        response.checkpoint_review_count = checkpoint.review_count as u64;
        response.checkpoint_last_review_id = checkpoint.last_review_id;
        response.checkpoint_history_hash = checkpoint.history_hash.clone();
        response.checkpoint_card_count = checkpoint.cards.len() as u64;
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

/// The values of an int64 column; a trailing partial value is dropped.
pub(crate) fn i64_values(column: &[u8]) -> Vec<i64> {
    column
        .chunks_exact(8)
        .map(|bytes| i64::from_le_bytes(bytes.try_into().unwrap()))
        .collect()
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
        assert_eq!(inputs.replay.reviews.len(), 4);
        assert_eq!(
            inputs.replay.history_hash.as_deref(),
            Some(fingerprint.history_hash.as_str())
        );
        assert_eq!(inputs.replay.reviews[0].card_type, 0);
        assert_eq!(inputs.replay.reviews[0].elapsed_seconds, 10);
        assert_eq!(inputs.replay.cards[0].review_count, 4);
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
        let rows: Vec<&[u8]> = response
            .packed_rows
            .chunks_exact(PACKED_WARM_UP_ROW_BYTES)
            .collect();
        let row_i64 =
            |row: &[u8], at: usize| i64::from_le_bytes(row[at..at + 8].try_into().unwrap());
        // the card id follows the presence bits; the note, deck and preset
        // ids follow it
        assert_eq!(
            rows.iter().map(|row| row_i64(row, 4)).collect::<Vec<_>>(),
            [deleted.id.0, card.id.0, deleted.id.0, card.id.0]
        );
        assert_eq!(
            rows.iter()
                .map(|row| u32::from_le_bytes(row[..4].try_into().unwrap()) & 0b111)
                .collect::<Vec<_>>(),
            [0, 0b111, 0, 0b111]
        );
        assert_eq!(
            rows.iter().map(|row| row_i64(row, 12)).collect::<Vec<_>>(),
            [0, 10, 0, 10]
        );
        assert_eq!(
            rows.iter().map(|row| row_i64(row, 20)).collect::<Vec<_>>(),
            [0, 1, 0, 1]
        );
        assert_eq!(row_i64(rows[0], 28), 0);
        assert_eq!(i64_values(&response.cards), [deleted.id.0, card.id.0]);
        assert_eq!(response.card_fsrs_preset_ids, ["", "1"]);
        // the deleted card's second review measures from its first: the
        // elapsed seconds are the row's last int64 before the retentions
        assert_eq!(row_i64(rows[2], 70), 2);
        assert_the_fingerprint_accepts(&mut col, inputs)?;

        let job = self::inputs(&mut col, &[], &settings())?;
        let review = &job.replay.reviews[0];
        assert_eq!(
            (review.note_id, review.deck_id, review.preset_id),
            (None, None, None)
        );
        let mut packed = Vec::new();
        review.write_packed_row(&mut packed);
        // bits 0-2 (note, deck, preset) clear, bits 3-8 set
        assert_eq!(&packed[..4], &0x1f8u32.to_le_bytes());
        packed.clear();
        job.replay.reviews[1].write_packed_row(&mut packed);
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
            .replay
            .reviews
            .iter()
            .map(|review| review.review_id)
            .collect();
        assert_eq!(review_ids, [first, first + 2_000]);
        // the first rated row is the start row: the learn-start state code
        assert_eq!(inputs.replay.reviews[0].card_type, 0);
        assert_ne!(inputs.replay.reviews[1].card_type, 0);

        for part_rows in [1, 2, 1_000] {
            let rpc = inputs_from_the_rpc(&mut col, &ignored, part_rows)?;
            assert_eq!(i64_values(&rpc.review_ids), [first, first + 2_000]);
            assert_eq!(Some(&rpc.history_hash), inputs.replay.history_hash.as_ref());
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
        assert_eq!(after.replay.reviews, before.replay.reviews);
        assert_eq!(after.replay.cards, before.replay.cards);
        assert_eq!(after.replay.history_hash, before.replay.history_hash);
        let review_ids: Vec<i64> = after
            .replay
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
        let checkpoint = inputs.replay.checkpoint.expect("a checkpoint");
        assert_eq!(checkpoint.review_count, 2);
        assert_eq!(checkpoint.last_review_id, start + day);
        assert_eq!(checkpoint.cards.len(), 2);
        assert_eq!(checkpoint.cards[0].review_count, 1);
        assert_eq!(checkpoint.history_hash.len(), 64);
        Ok(())
    }

    /// Two cards in two decks, a deleted card, and reviews over weeks.
    fn collection_with_history() -> Result<Collection> {
        let mut col = Collection::new();
        let deck = col.get_or_create_normal_deck("Other")?;
        let mut first = Card::new(NoteId(10), 0, DeckId(1), 0);
        let mut second = Card::new(NoteId(11), 0, deck.id, 0);
        col.add_card(&mut first)?;
        col.add_card(&mut second)?;
        let deleted = Card {
            id: CardId(first.id.0 - 7_000),
            ..first.clone()
        };
        let day = 86_400_000;
        let start = first.id.0.max(second.id.0) + 10_000;
        for (step, (card, kind)) in [
            (&first, RevlogReviewKind::Learning),
            (&deleted, RevlogReviewKind::Learning),
            (&second, RevlogReviewKind::Review),
            (&first, RevlogReviewKind::Review),
            (&deleted, RevlogReviewKind::Review),
            (&second, RevlogReviewKind::Relearning),
            (&first, RevlogReviewKind::Review),
            (&second, RevlogReviewKind::Review),
        ]
        .into_iter()
        .enumerate()
        {
            add_review(&mut col, card, start + step as i64 * 3 * day, kind)?;
        }
        Ok(col)
    }

    /// The rows request of `rows`, with the presets and first-review rule
    /// the backend's own read gives them.
    fn rows_request(
        col: &mut Collection,
        rows: &[RwkvHistoricalReviewRow],
    ) -> Result<RwkvReplayInputsFromRowsRequest> {
        let timing = col.timing_today()?;
        let decks = col.storage.get_decks_map()?;
        let mut request = RwkvReplayInputsFromRowsRequest {
            first_review_elapsed_source: FirstReviewElapsedSource::CardCreation as i32,
            days_elapsed: timing.days_elapsed.into(),
            next_day_at: timing.next_day_at.0,
            hash_history: true,
            ..Default::default()
        };
        let mut presets = Vec::new();
        for row in rows {
            let push = |column: &mut Vec<u8>, value: i64| column.extend(value.to_le_bytes());
            push(&mut request.review_ids, row.review_id);
            push(&mut request.card_ids, row.card_id);
            push(&mut request.note_ids, row.note_id.unwrap_or(0));
            push(&mut request.deck_ids, row.deck_id.unwrap_or(0));
            push(&mut request.eases, row.ease);
            push(&mut request.durations_millis, row.duration_millis);
            push(&mut request.review_kinds, row.review_kind);
            push(&mut request.interval_days, row.interval_days);
            push(&mut request.ease_factors, row.ease_factor);
            request.learning_starts.push(row.is_learning_start.into());
            request.has_note.push(row.note_id.is_some().into());
            request.has_deck.push(row.deck_id.is_some().into());
            request.encodable.push(1);
            if let Some(deck_id) = row.deck_id {
                let preset = decks[&DeckId(deck_id)].config_id().unwrap().0;
                if !presets.contains(&(row.card_id, preset)) {
                    presets.push((row.card_id, preset));
                }
            }
        }
        for (card_id, preset_id) in presets {
            request.preset_card_ids.extend(card_id.to_le_bytes());
            request.preset_ids.extend(preset_id.to_le_bytes());
            request.has_preset.push(1);
        }
        Ok(request)
    }

    fn response(
        request: &RwkvReplayInputsFromRowsRequest,
    ) -> Result<RwkvHistoricalReviewInputsResponse> {
        Ok(RwkvHistoricalReviewInputsResponse::decode(
            rwkv_replay_inputs_from_rows(&request.encode_to_vec())?.as_slice(),
        )?)
    }

    /// Rows Python read itself go through the same encoder as the backend's
    /// own read of the whole history.
    #[test]
    fn rows_read_elsewhere_are_encoded_as_the_backends_own_read() -> Result<()> {
        let mut col = collection_with_history()?;
        let rows = col.storage.rwkv_historical_review_rows(&[])?.0;
        let from_rows = response(&rows_request(&mut col, &rows)?)?;
        let own = rwkv_historical_review_inputs(
            RwkvHistoricalReviewInputsRequest {
                hash_history: true,
                first_review_elapsed_source: FirstReviewElapsedSource::CardCreation as i32,
                ..Default::default()
            },
            3,
            &mut |step| step(&mut col),
        )?;
        assert_eq!(own.review_count, 8);
        assert_eq!(own.packed_rows.len(), 8 * PACKED_WARM_UP_ROW_BYTES);
        assert_eq!(
            RwkvHistoricalReviewInputsResponse {
                active_ignored_review_ids: vec![],
                card_fsrs_preset_ids: vec![],
                ..own
            },
            from_rows
        );
        Ok(())
    }

    /// A read after a cutoff goes on from the state the whole read had
    /// there: it ends with the whole read's hash, count and card states, and
    /// its rows are the whole read's after the cutoff.
    #[test]
    fn a_read_after_a_cutoff_goes_on_from_the_state_before_it() -> Result<()> {
        let mut col = collection_with_history()?;
        let rows = col.storage.rwkv_historical_review_rows(&[])?.0;
        let whole = response(&rows_request(&mut col, &rows)?)?;
        for cutoff in 1..rows.len() {
            let before = response(&rows_request(&mut col, &rows[..cutoff])?)?;
            let mut after = rows_request(&mut col, &rows[cutoff..])?;
            after.after_review_id = Some(rows[cutoff - 1].review_id);
            after.counts_before_cutoff = true;
            after.previous_history_hash = before.history_hash.clone();
            after.seed_previous_review_id_cards = before.cards.clone();
            after.seed_previous_review_ids = before.card_previous_review_ids.clone();
            after.seed_previous_interval_cards = before.cards.clone();
            after.seed_previous_interval_days = before.card_previous_interval_days.clone();
            after.seed_review_count_cards = before.cards.clone();
            after.seed_review_counts = before.card_review_counts.clone();
            let after = response(&after)?;
            assert_eq!(after.history_hash, whole.history_hash);
            assert_eq!(after.review_count, whole.review_count);
            assert_eq!(after.last_review_id, whole.last_review_id);
            assert_eq!(
                after.packed_rows,
                whole.packed_rows[cutoff * PACKED_WARM_UP_ROW_BYTES..]
            );
        }
        Ok(())
    }

    /// A row the caller could not read as whole numbers is counted and not
    /// replayed; a card the caller resolved no preset for has none; the rows
    /// up to a cutoff without a stored state are counted, and the cards
    /// start afresh after it.
    #[test]
    fn rows_are_counted_as_python_counted_them() -> Result<()> {
        let mut col = collection_with_history()?;
        let rows = col.storage.rwkv_historical_review_rows(&[])?.0;
        let mut request = rows_request(&mut col, &rows)?;
        request.encodable[3] = 0;
        request.has_preset = vec![0; request.has_preset.len()];
        let built = response(&request)?;
        assert_eq!(built.review_count, 8);
        assert_eq!(i64_values(&built.review_ids).len(), 7);
        assert!(built
            .packed_rows
            .chunks_exact(PACKED_WARM_UP_ROW_BYTES)
            .all(|row| row[0] & 0b100 == 0));

        let mut request = rows_request(&mut col, &rows)?;
        request.after_review_id = Some(rows[4].review_id);
        let built = response(&request)?;
        assert_eq!(built.review_count, 8);
        assert_eq!(i64_values(&built.review_ids).len(), 3);
        // the first review of each card after the cutoff has no previous
        // one: its elapsed seconds are the -1 sentinel
        let elapsed_seconds = |row: &[u8]| i64::from_le_bytes(row[70..78].try_into().unwrap());
        assert!(built
            .packed_rows
            .chunks_exact(PACKED_WARM_UP_ROW_BYTES)
            .take(2)
            .all(|row| elapsed_seconds(row) == -1));
        Ok(())
    }

    #[test]
    fn an_unknown_feature_layout_fails() -> Result<()> {
        let mut col = collection_with_history()?;
        let rows = col.storage.rwkv_historical_review_rows(&[])?.0;
        let mut request = rows_request(&mut col, &rows)?;
        request.feature_layout = "published-92".into();
        assert!(response(&request).is_ok());
        request.feature_layout = "another-layout".into();
        assert!(response(&request).is_err());
        Ok(())
    }
}
