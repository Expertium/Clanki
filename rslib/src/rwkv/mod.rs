// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::collections::HashSet;
use std::fs;
use std::io;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;

use anki_proto::deck_config::deck_config::config::ReviewCardOrder;
use rayon::prelude::*;
use rusqlite::blob::Blob;
use rusqlite::params;
use rusqlite::Connection;
use rusqlite::OpenFlags;
use rusqlite::OptionalExtension;
use rusqlite::Transaction;
use rusqlite::MAIN_DB;

use crate::card::CardType;
use crate::scheduler::rwkv::relative_overdueness;

mod bulk;
#[cfg(test)]
mod collection_benchmark;
#[cfg(target_os = "macos")]
mod matmul;
#[cfg(test)]
mod query_math_bench;

const D_MODEL: usize = 128;
const CARD_FEATURES: usize = 92;
const HEADS: usize = 4;
const HEAD_SIZE: usize = D_MODEL / HEADS;
const HEAD_DIM: usize = 4 * D_MODEL;
const NUM_CURVES: usize = 128;
const ANSWER_EASES: [u8; 4] = [1, 2, 3, 4];
const S90_TARGET_RETENTION: f32 = 0.9;
const STATE_CACHE_STORE_SCHEMA_VERSION: i64 = 5;
/// The last schema that kept one serialized delta stream per segment. A store
/// at this version is migrated to `entity_states` rows before it is read.
const STATE_CACHE_STORE_CHUNKED_SCHEMA_VERSION: i64 = 4;
/// Page size of the state-cache store. One card state is about 51 KiB, so a
/// 16 KiB page keeps the per-row overflow waste under one page while a point
/// read of one entity still costs only four pages.
const STATE_CACHE_STORE_PAGE_SIZE: i64 = 16 * 1024;
const STATE_CACHE_DELTA_MAGIC: &[u8] = b"ARWKVSTATEDELTA1\0";
const STATE_CACHE_KIND_CARD: i64 = 0;
const STATE_CACHE_KIND_NOTE: i64 = 1;
const STATE_CACHE_KIND_DECK: i64 = 2;
const STATE_CACHE_KIND_PRESET: i64 = 3;
const STATE_CACHE_KIND_GLOBAL: i64 = 4;
/// The global stream holds exactly one entity, so its rows all use this id.
const STATE_CACHE_GLOBAL_ENTITY_ID: i64 = 0;
/// One row per (segment, kind, entity). A null `state` is the "this entity
/// has no cached state" that the old delta stream wrote as a zero marker. The
/// unique index is what turns a warm-up state read into a point lookup.
///
/// Do not put the states back into one blob per segment. Schema 4 did that,
/// and a blob cannot be read by key: SQLite reaches an offset inside a blob by
/// walking its overflow-page chain, so it cannot skip the pages in between.
/// A scan of Andrew's 3.28 GB store that asked for the 947,593 bytes of keys
/// alone still made 50,304 read operations and still read 3.29 GB from the
/// operating system. Only an index gives a point lookup.
const STATE_CACHE_ENTITY_STATES_DDL: &str = r#"
create table entity_states (
  id integer primary key,
  segment_id integer not null references segments(id) on delete cascade,
  kind integer not null,
  entity_id integer not null,
  state blob
);
create unique index entity_states_key on entity_states (segment_id, kind, entity_id);
"#;

const MODULE_LAYERS: [usize; 5] = [3, 4, 2, 3, 4];
const CHANNEL_MIXER_DIMS: [usize; 5] = [192, 256, 192, 256, 256];
/// Rows per block in the bulk replay's blocked projection kernels. Bounds the
/// stack scratch used per block; larger blocks amortize weight-matrix streaming
/// over more rows.
const LINEAR_BLOCK_ROWS: usize = 32;

#[cfg(test)]
#[derive(Clone)]
struct RwkvScanCapturedStep {
    r: Vec<f32>,
    k: Vec<f32>,
    v: Vec<f32>,
    w: Vec<f32>,
    a: Vec<f32>,
    k_deformed: Vec<f32>,
}

#[cfg(test)]
static RWKV_SINGLE_TIMESTEP_PROFILE_ENABLED: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);
#[cfg(test)]
static RWKV_SINGLE_TIMESTEP_PROFILE_CALLS: std::sync::atomic::AtomicU64 =
    std::sync::atomic::AtomicU64::new(0);
#[cfg(test)]
static RWKV_SINGLE_TIMESTEP_PROFILE_NANOS: std::sync::atomic::AtomicU64 =
    std::sync::atomic::AtomicU64::new(0);

#[cfg(test)]
const RWKV_WARMUP_PROFILE_BUCKETS: usize = 16;

#[cfg(test)]
#[derive(Clone, Copy)]
enum RwkvWarmupProfileBucket {
    FeaturesFor = 0,
    FeaturesStore = 1,
    ModelReview = 2,
    FeatureMlp = 3,
    ModuleCard = 4,
    ModuleDeck = 5,
    ModuleNote = 6,
    ModulePreset = 7,
    ModuleGlobal = 8,
    Heads = 9,
    ModuleRun = 10,
    TimeMixer = 11,
    ChannelMixer = 12,
    Linear = 13,
    Norm = 14,
    SingleTimestep = 15,
}

#[cfg(test)]
static RWKV_WARMUP_PROFILE_ENABLED: std::sync::atomic::AtomicBool =
    std::sync::atomic::AtomicBool::new(false);
#[cfg(test)]
static RWKV_WARMUP_PROFILE_CALLS: [std::sync::atomic::AtomicU64; RWKV_WARMUP_PROFILE_BUCKETS] =
    [const { std::sync::atomic::AtomicU64::new(0) }; RWKV_WARMUP_PROFILE_BUCKETS];
#[cfg(test)]
static RWKV_WARMUP_PROFILE_NANOS: [std::sync::atomic::AtomicU64; RWKV_WARMUP_PROFILE_BUCKETS] =
    [const { std::sync::atomic::AtomicU64::new(0) }; RWKV_WARMUP_PROFILE_BUCKETS];

#[cfg(test)]
fn start_rwkv_warmup_profile() {
    for index in 0..RWKV_WARMUP_PROFILE_BUCKETS {
        RWKV_WARMUP_PROFILE_CALLS[index].store(0, std::sync::atomic::Ordering::Relaxed);
        RWKV_WARMUP_PROFILE_NANOS[index].store(0, std::sync::atomic::Ordering::Relaxed);
    }
    RWKV_WARMUP_PROFILE_ENABLED.store(true, std::sync::atomic::Ordering::Relaxed);
}

#[cfg(test)]
fn stop_rwkv_warmup_profile() -> Vec<(&'static str, u64, u64)> {
    RWKV_WARMUP_PROFILE_ENABLED.store(false, std::sync::atomic::Ordering::Relaxed);
    [
        ("features_for", RwkvWarmupProfileBucket::FeaturesFor),
        ("features_store", RwkvWarmupProfileBucket::FeaturesStore),
        ("model_review", RwkvWarmupProfileBucket::ModelReview),
        ("feature_mlp", RwkvWarmupProfileBucket::FeatureMlp),
        ("module_card", RwkvWarmupProfileBucket::ModuleCard),
        ("module_deck", RwkvWarmupProfileBucket::ModuleDeck),
        ("module_note", RwkvWarmupProfileBucket::ModuleNote),
        ("module_preset", RwkvWarmupProfileBucket::ModulePreset),
        ("module_global", RwkvWarmupProfileBucket::ModuleGlobal),
        ("heads", RwkvWarmupProfileBucket::Heads),
        ("module_run", RwkvWarmupProfileBucket::ModuleRun),
        ("time_mixer", RwkvWarmupProfileBucket::TimeMixer),
        ("channel_mixer", RwkvWarmupProfileBucket::ChannelMixer),
        ("linear", RwkvWarmupProfileBucket::Linear),
        ("norm", RwkvWarmupProfileBucket::Norm),
        ("single_timestep", RwkvWarmupProfileBucket::SingleTimestep),
    ]
    .into_iter()
    .map(|(name, bucket)| {
        let index = bucket as usize;
        (
            name,
            RWKV_WARMUP_PROFILE_CALLS[index].load(std::sync::atomic::Ordering::Relaxed),
            RWKV_WARMUP_PROFILE_NANOS[index].load(std::sync::atomic::Ordering::Relaxed),
        )
    })
    .collect()
}

#[cfg(test)]
fn rwkv_warmup_profile_start() -> Option<std::time::Instant> {
    RWKV_WARMUP_PROFILE_ENABLED
        .load(std::sync::atomic::Ordering::Relaxed)
        .then(std::time::Instant::now)
}

#[cfg(test)]
fn rwkv_warmup_profile_record(
    bucket: RwkvWarmupProfileBucket,
    started: Option<std::time::Instant>,
) {
    if let Some(started) = started {
        let index = bucket as usize;
        RWKV_WARMUP_PROFILE_CALLS[index].fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        RWKV_WARMUP_PROFILE_NANOS[index].fetch_add(
            started.elapsed().as_nanos().min(u128::from(u64::MAX)) as u64,
            std::sync::atomic::Ordering::Relaxed,
        );
    }
}

#[cfg(test)]
fn start_rwkv_single_timestep_profile() {
    RWKV_SINGLE_TIMESTEP_PROFILE_CALLS.store(0, std::sync::atomic::Ordering::Relaxed);
    RWKV_SINGLE_TIMESTEP_PROFILE_NANOS.store(0, std::sync::atomic::Ordering::Relaxed);
    RWKV_SINGLE_TIMESTEP_PROFILE_ENABLED.store(true, std::sync::atomic::Ordering::Relaxed);
}

#[cfg(test)]
fn stop_rwkv_single_timestep_profile() -> (u64, u64) {
    RWKV_SINGLE_TIMESTEP_PROFILE_ENABLED.store(false, std::sync::atomic::Ordering::Relaxed);
    (
        RWKV_SINGLE_TIMESTEP_PROFILE_CALLS.load(std::sync::atomic::Ordering::Relaxed),
        RWKV_SINGLE_TIMESTEP_PROFILE_NANOS.load(std::sync::atomic::Ordering::Relaxed),
    )
}

#[cfg(test)]
struct RwkvScanCaptureState {
    module_id: usize,
    layer_id: usize,
    steps: Vec<RwkvScanCapturedStep>,
}

#[cfg(test)]
std::thread_local! {
    static RWKV_SCAN_CAPTURE: std::cell::RefCell<Option<RwkvScanCaptureState>> =
        const { std::cell::RefCell::new(None) };
}

#[cfg(test)]
fn start_rwkv_scan_capture(module_id: usize, layer_id: usize) {
    RWKV_SCAN_CAPTURE.with(|capture| {
        *capture.borrow_mut() = Some(RwkvScanCaptureState {
            module_id,
            layer_id,
            steps: Vec::new(),
        });
    });
}

#[cfg(test)]
fn rwkv_scan_capture_active() -> bool {
    RWKV_SCAN_CAPTURE.with(|capture| capture.borrow().is_some())
}

#[cfg(test)]
fn take_rwkv_scan_capture() -> Vec<RwkvScanCapturedStep> {
    RWKV_SCAN_CAPTURE.with(|capture| {
        capture
            .borrow_mut()
            .take()
            .map(|capture| capture.steps)
            .unwrap_or_default()
    })
}

#[cfg(test)]
#[allow(clippy::too_many_arguments)]
fn record_rwkv_scan_step(
    module_id: usize,
    layer_id: usize,
    r: &[f32],
    k: &[f32],
    v: &[f32],
    w: &[f32],
    a: &[f32],
    k_deformed: &[f32],
) {
    RWKV_SCAN_CAPTURE.with(|capture| {
        let mut capture = capture.borrow_mut();
        let Some(capture) = capture.as_mut() else {
            return;
        };
        if capture.module_id != module_id || capture.layer_id != layer_id {
            return;
        }
        capture.steps.push(RwkvScanCapturedStep {
            r: r.to_vec(),
            k: k.to_vec(),
            v: v.to_vec(),
            w: w.to_vec(),
            a: a.to_vec(),
            k_deformed: k_deformed.to_vec(),
        });
    });
}

#[cfg(test)]
const MAX_CHANNEL_MIXER_DIM: usize = 256;
#[cfg(test)]
const MAX_LORA_RANK: usize = 16;
const RETRIEVABILITY_GEMM_BATCH_SIZE: usize = 128;
/// The id a review without a note, deck or preset (the reviews of a deleted
/// card) is encoded and streamed with (spec sched.rwkv-replay-deleted-cards):
/// the deck and the preset always, the note as `RwkvIdPipeline` says. The
/// value lies far above any Anki id (epoch milliseconds), so it never meets a
/// real entity.
const ID_PLACEHOLDER: i64 = 314_159_265_358_979_323;
const ID_SPLIT: u64 = 4;
/// MT19937's state words: the `dim` first outputs after a seed read the
/// words up to `dim + MT19937_SHIFT`, so an id code needs no more of them.
const MT19937_SHIFT: usize = 397;
const MAX_ID_ENCODING_DIM: usize = 12;
const DAY_OFFSET_ENCODE_PERIODS: [f32; 7] = [3.0, 7.0, 30.0, 100.0, 365.0, 3650.0, 36500.0];
const SECONDS_PER_DAY: i64 = 86_400;

const ELAPSED_DAYS_MEAN: f32 = 1.51;
const ELAPSED_DAYS_STD: f32 = 1.62;
const ELAPSED_DAYS_CUMULATIVE_MEAN: f32 = 2.14;
const ELAPSED_DAYS_CUMULATIVE_STD: f32 = 2.25;
const ELAPSED_SECONDS_MEAN: f32 = 9.96;
const ELAPSED_SECONDS_STD: f32 = 5.21;
const ELAPSED_SECONDS_CUMULATIVE_MEAN: f32 = 10.86;
const ELAPSED_SECONDS_CUMULATIVE_STD: f32 = 5.8;
const DURATION_MEAN: f32 = 8.9;
const DURATION_STD: f32 = 1.07;
const DIFF_NEW_CARDS_MEAN: f32 = 2.945;
const DIFF_NEW_CARDS_STD: f32 = 2.011;
const DIFF_REVIEWS_MEAN: f32 = 4.64;
const DIFF_REVIEWS_STD: f32 = 2.59;
const CUM_NEW_CARDS_TODAY_MEAN: f32 = 2.55;
const CUM_NEW_CARDS_TODAY_STD: f32 = 1.41;
const CUM_REVIEWS_TODAY_MEAN: f32 = 4.59;
const CUM_REVIEWS_TODAY_STD: f32 = 1.30;

#[derive(Debug, Clone)]
pub struct ReviewInput {
    pub card_id: i64,
    pub note_id: Option<i64>,
    pub deck_id: Option<i64>,
    pub preset_id: Option<i64>,
    pub is_query: bool,
    pub ease: Option<u8>,
    pub duration_millis: Option<i64>,
    /// Legacy field name: answered inputs contain the training dataset state.
    pub card_type: Option<i64>,
    pub day_offset: Option<i64>,
    pub current_elapsed_days: Option<i64>,
    pub current_elapsed_seconds: Option<i64>,
    pub target_retentions: [Option<f32>; 4],
    pub enforce_grade_order: bool,
}

/// Which entities a review without a note, deck or preset (a deleted
/// card's) streams through and is encoded with: a property of the id
/// pipeline the model was trained with, so of the model version (spec
/// sched.rwkv-replay-deleted-cards; the RWKV session's DEPLOY_FUNCTIONS.md
/// section 7). Deck and preset share one placeholder in both.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RwkvIdPipeline {
    /// The shipped model: srs-benchmark cast the filled ids to int32, which
    /// saturated every placeholder to one value, so all missing notes are
    /// one shared note too.
    Int32,
    /// A model trained on the int64 pipeline (2026-08-21 on): a missing note
    /// is `ID_PLACEHOLDER + card_id`, a note of its own per card.
    Int64,
}

/// The id pipeline of the weights format `SrsModel::load` reads: the shipped
/// model's. A model trained another way comes with its own loader and says
/// its own pipeline.
const PUBLISHED_MODEL_ID_PIPELINE: RwkvIdPipeline = RwkvIdPipeline::Int32;

impl RwkvIdPipeline {
    /// The note a card without one streams through.
    fn missing_note_id(self, card_id: i64) -> i64 {
        match self {
            RwkvIdPipeline::Int32 => ID_PLACEHOLDER,
            RwkvIdPipeline::Int64 => ID_PLACEHOLDER + card_id,
        }
    }
}

impl ReviewInput {
    /// The note whose stream the review reads and advances. A review without
    /// a note (a deleted card's) uses the model's placeholder note
    /// (`RwkvIdPipeline`).
    fn note_key(&self, ids: RwkvIdPipeline) -> i64 {
        self.note_id
            .unwrap_or_else(|| ids.missing_note_id(self.card_id))
    }

    /// The deck stream: one shared placeholder for every review without one.
    fn deck_key(&self) -> i64 {
        self.deck_id.unwrap_or(ID_PLACEHOLDER)
    }

    /// The preset stream, as `deck_key`.
    fn preset_key(&self) -> i64 {
        self.preset_id.unwrap_or(ID_PLACEHOLDER)
    }

    /// The entities whose codes the review's features hold, in feature order.
    fn encoded_ids(&self, ids: RwkvIdPipeline) -> [(IdKind, i64); 4] {
        [
            (IdKind::Card, self.card_id),
            (IdKind::Note, self.note_key(ids)),
            (IdKind::Deck, self.deck_key()),
            (IdKind::Preset, self.preset_key()),
        ]
    }
}

/// One review of a warm-up replay, as the replay predicted it before the
/// answer (spec ui.stats-model-metrics).
#[derive(Clone, Copy, Debug)]
pub struct WarmUpPrediction {
    /// The review's place in the `reviews` slice the caller passed in.
    pub index: usize,
    /// RWKV-Instant's head for this review.
    pub retrievability: f32,
    /// RWKV-Curve's prediction of this review: the curve the replay stored
    /// at this card's previous answered review, at this review's own
    /// elapsed time. `None` on a card's first review, and on a review whose
    /// input carries no elapsed time; it is never a substitute value.
    pub curve_retrievability: Option<f32>,
}

/// The encoding of a saved per-review curve source (spec
/// ui.card-info-rwkv-curve): what `curves_from_sources` rebuilds a curve
/// from. For this model, format 1: the heads' input (`prehead_x` after
/// `prehead_norm`) as little-endian 16-bit floats, one per model dimension.
/// A model whose curve comes from other numbers (the mid-October one: its
/// raw head outputs) gets a new format number; readers never assume a width.
pub const CURVE_SOURCE_FORMAT: u32 = 1;
/// The version of the arithmetic that produces the source. Bump it when a
/// kernel change (another float order, quantization) changes `prehead_x`,
/// so that sources saved before the change read as stale. 2: the replay
/// keeps deleted cards' reviews and draws id codes from the id (spec
/// sched.rwkv-replay-deleted-cards, sched.rwkv-id-codes), which changes the
/// replayed `prehead_x` too.
pub const CURVE_SOURCE_KERNEL: u32 = 2;

/// A curve as card info draws it: recall at each asked elapsed day, and the
/// curve's S90.
pub type CurvePoints = (Vec<f32>, f32);

/// Curve sources recorded by a replay or a live answer: for each answered
/// review, its index in the call's input (always 0 for `review`), and its
/// source, `curve_source_tag().2` bytes each, in `bytes`.
#[derive(Debug, Default, Clone, PartialEq)]
pub struct CurveSources {
    pub indices: Vec<u32>,
    pub bytes: Vec<u8>,
}

pub struct ReviewState<'a> {
    pub card: Option<&'a [u8]>,
    pub deck: Option<&'a [u8]>,
    pub note: Option<&'a [u8]>,
    pub preset: Option<&'a [u8]>,
    pub global: Option<&'a [u8]>,
}

pub struct ReviewOutput {
    pub retrievability: f32,
    pub curve_retrievability: Option<f32>,
    pub button_probabilities: [f32; 4],
    pub current_interval: Option<u32>,
    /// RWKV-Curve's S90 in days, unrounded and possibly under one day (spec
    /// sched.rwkv-curve-s90).
    pub current_s90: Option<f32>,
    /// Unrounded answer intervals in days, possibly under one day (spec
    /// sched.sub-day-intervals).
    pub intervals: [Option<f32>; 4],
    /// RWKV-Curve's S90 in days, unrounded and possibly under one day (spec
    /// sched.rwkv-curve-s90).
    pub s90s: [Option<f32>; 4],
    pub card_state: Vec<u8>,
    pub deck_state: Vec<u8>,
    pub note_state: Vec<u8>,
    pub preset_state: Vec<u8>,
    pub global_state: Vec<u8>,
}

pub struct ReviewPredictionRequest {
    pub input: ReviewInput,
    pub state: ReviewStateOwned,
}

pub struct ReviewStateOwned {
    pub card: Option<Vec<u8>>,
    pub deck: Option<Vec<u8>>,
    pub note: Option<Vec<u8>>,
    pub preset: Option<Vec<u8>>,
    pub global: Option<Vec<u8>>,
}

pub struct ReviewPredictionOutput {
    pub retrievability: f32,
    pub curve_retrievability: Option<f32>,
    pub button_probabilities: [f32; 4],
    pub current_interval: Option<u32>,
    /// `current_interval` unrounded, in days (spec
    /// sched.rwkv-curve-reschedule).
    pub current_interval_unrounded: Option<f32>,
    /// RWKV-Curve's S90 in days, unrounded and possibly under one day (spec
    /// sched.rwkv-curve-s90).
    pub current_s90: Option<f32>,
    /// Unrounded answer intervals in days, possibly under one day (spec
    /// sched.sub-day-intervals).
    pub intervals: [Option<f32>; 4],
    /// RWKV-Curve's S90 in days, unrounded and possibly under one day (spec
    /// sched.rwkv-curve-s90).
    pub s90s: [Option<f32>; 4],
}

/// The subset of `ReviewPredictionOutput` that rescheduling needs, computed
/// from a single query pass against the resident warm-up state.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ReviewIntervalPrediction {
    /// The recall of the curve RWKV stored at the card's last answered
    /// review, at the time since that review (`current_curve_retrievability`);
    /// `None` when the card has no stored curve or no elapsed time.
    pub curve_retrievability: Option<f32>,
    pub current_interval: Option<u32>,
    /// `current_interval` unrounded, in days (spec
    /// sched.rwkv-curve-reschedule).
    pub current_interval_unrounded: Option<f32>,
    /// RWKV-Curve's S90 in days, unrounded and possibly under one day (spec
    /// sched.rwkv-curve-s90).
    pub current_s90: Option<f32>,
}

/// A card's days for `curve_retrievability_day_sums_from_warm_up`.
#[derive(Debug, Clone, Copy)]
pub struct RwkvCurveSpan {
    pub card_id: i64,
    /// The day of the card's last answered review.
    pub review_day: i64,
    pub first_day: i64,
    pub last_day: i64,
}

/// See `RwkvInference::current_intervals`.
struct CurrentIntervals {
    whole_days: Option<u32>,
    unrounded: Option<f32>,
    s90: Option<f32>,
}

pub struct RwkvInference {
    model: Arc<SrsModel>,
    features: FeatureState,
    curves: HashMap<i64, ReviewCurve>,
    warm_up_states: ReviewStateMaps,
    state_cache_store: Option<StateCacheStoreWriter>,
    target_retention: f32,
    max_interval_days: u32,
    /// While `Some`, every answered review this runtime replays or reviews
    /// adds its curve source here (`record_curve_sources`).
    curve_sources: Option<CurveSources>,
}

struct StateCacheStoreWriter {
    path: PathBuf,
    generation: String,
    durable: bool,
    connection: Mutex<Connection>,
}

impl StateCacheStoreWriter {
    fn open(path: PathBuf, generation: &str, durable: bool) -> io::Result<Self> {
        let connection = open_state_cache_store(&path, generation, durable)?;
        Ok(Self {
            path,
            generation: generation.to_owned(),
            durable,
            connection: Mutex::new(connection),
        })
    }

    fn matches(&self, path: &PathBuf, generation: &str, durable: bool) -> bool {
        self.path == *path && self.generation == generation && self.durable == durable
    }
}

#[derive(Clone)]
pub struct RwkvInferenceState {
    feature_state: FeatureStateForCard,
    card_id: i64,
    curve: Option<ReviewCurve>,
}

pub struct RwkvWarmUpSnapshot {
    pub card_states: Vec<(i64, Vec<u8>)>,
    pub note_states: Vec<(i64, Vec<u8>)>,
    pub deck_states: Vec<(i64, Vec<u8>)>,
    pub preset_states: Vec<(i64, Vec<u8>)>,
    pub global_state: Option<Vec<u8>>,
}

pub struct RwkvWorkloadSimulationSnapshot {
    pub card_states: Vec<(i64, Vec<u8>)>,
    pub note_states: Vec<(i64, Vec<u8>)>,
    pub deck_states: Vec<(i64, Vec<u8>)>,
    pub preset_states: Vec<(i64, Vec<u8>)>,
    pub global_state: Option<Vec<u8>>,
    pub runtime_state: Option<Vec<u8>>,
}

#[derive(Clone)]
pub struct RwkvWorkloadSimulationInput {
    pub review_input: ReviewInput,
    pub interval_days: Option<i64>,
    pub ease_factor: Option<i64>,
    pub reps: Option<i64>,
    pub lapses: Option<i64>,
}

pub struct RwkvWorkloadSimulationPoint {
    pub memorized: f32,
    pub weighted_memorized: f32,
    pub cost: f32,
    pub review_count: u32,
}

pub struct RwkvWorkloadSimulationOutput {
    pub reviewless_end_memorized: f32,
    pub reviewless_end_weighted_memorized: f32,
    pub points: Vec<(u32, RwkvWorkloadSimulationPoint)>,
}

struct RwkvWorkloadQueryPrediction {
    retrievability: f32,
    current_interval: Option<u32>,
    current_s90: Option<f32>,
}

pub struct RwkvWorkloadReviewModel {
    pub grade_seconds: [f32; 4],
    pub bucket_probabilities: Vec<(u32, [f32; 4])>,
}

pub struct RwkvWorkloadSimulationConfig {
    pub min_dr: u32,
    pub max_dr: u32,
    pub target_dr_step: u32,
    pub days_to_simulate: u32,
    pub review_limit: u32,
    pub new_limit: u32,
    pub new_cards_ignore_review_limit: bool,
    pub max_interval: u32,
    pub review_order: ReviewCardOrder,
    pub suspend_after_lapses: Option<u32>,
    pub state_update_interval: u32,
    pub review_model: RwkvWorkloadReviewModel,
}

struct ReviewPredictionWorkItem {
    features: Vec<f32>,
    state: SrsStateOwned,
}

struct ReviewPredictionBorrowedWorkItem<'a> {
    features: Vec<f32>,
    state: SrsStateRef<'a>,
}

#[derive(Clone, Copy)]
struct ReviewPredictionQueryRef<'a> {
    features: &'a [f32],
    state: SrsStateRef<'a>,
}

impl ReviewPredictionQueryRef<'_> {
    fn layer_state(&self, module_id: usize, layer_id: usize) -> Option<&LayerState> {
        self.state
            .module(module_id)
            .and_then(|state| state.layers.get(layer_id))
    }
}

struct RwkvSimulationCard {
    review_input: RwkvWorkloadSimulationInput,
    due_day: i64,
    last_review_day: i64,
    reps: i64,
    lapses: i64,
    interval_days: i64,
    ease_factor: i64,
    is_new: bool,
    suspended: bool,
}

struct RwkvWorkloadTargetConfig<'a> {
    dr: u32,
    days_to_simulate: i64,
    review_limit: usize,
    new_limit: usize,
    new_cards_ignore_review_limit: bool,
    max_interval: u32,
    review_order: ReviewCardOrder,
    suspend_after_lapses: Option<u32>,
    state_update_interval: u32,
    review_model: &'a RwkvWorkloadReviewModel,
}

struct RwkvWorkloadTargetSweep {
    inputs: Vec<RwkvWorkloadSimulationInput>,
    snapshot: RwkvWorkloadSimulationSnapshot,
    target_drs: Vec<u32>,
    days_to_simulate: i64,
    review_limit: usize,
    new_limit: usize,
    new_cards_ignore_review_limit: bool,
    max_interval: u32,
    review_order: ReviewCardOrder,
    suspend_after_lapses: Option<u32>,
    state_update_interval: u32,
    review_model: RwkvWorkloadReviewModel,
    base_features: FeatureState,
    base_curves: HashMap<i64, ReviewCurve>,
    total_steps: u32,
}

impl RwkvInference {
    pub fn load(path: PathBuf, target_retention: f32, max_interval_days: u32) -> io::Result<Self> {
        let model = Arc::new(SrsModel::load(&path)?);
        let ids = model.ids;
        Ok(Self {
            model,
            features: FeatureState::new(ids),
            curves: HashMap::new(),
            warm_up_states: ReviewStateMaps::new(ids),
            state_cache_store: None,
            target_retention,
            max_interval_days,
            curve_sources: None,
        })
    }

    /// Starts (or stops, dropping what was recorded) recording the curve
    /// source of every answered review (spec ui.card-info-rwkv-curve).
    pub fn record_curve_sources(&mut self, on: bool) {
        self.curve_sources = on.then(CurveSources::default);
    }

    /// The sources recorded since the last call; recording goes on.
    pub fn take_curve_sources(&mut self) -> CurveSources {
        self.curve_sources
            .as_mut()
            .map(std::mem::take)
            .unwrap_or_default()
    }

    /// (format, kernel, bytes per source) of the sources this model saves.
    pub fn curve_source_tag(&self) -> (u32, u32, usize) {
        (
            CURVE_SOURCE_FORMAT,
            CURVE_SOURCE_KERNEL,
            curve_source_width(),
        )
    }

    /// The curves saved as `sources` (`width` bytes each), rebuilt through
    /// this model's curve head, at `elapsed_days`, each with its S90. `None`
    /// when the sources are not this model's format and kernel, or do not
    /// split into whole sources; a source whose curve has no S90 gets `None`.
    pub fn curves_from_sources(
        &self,
        format: u32,
        kernel: u32,
        sources: &[u8],
        width: usize,
        elapsed_days: &[f32],
    ) -> Option<Vec<Option<CurvePoints>>> {
        if format != CURVE_SOURCE_FORMAT
            || kernel != CURVE_SOURCE_KERNEL
            || width != curve_source_width()
            || sources.len() % width != 0
        {
            return None;
        }
        Some(
            sources
                .par_chunks(width)
                .map(|source| {
                    let prehead = decode_curve_source(source);
                    let curve = self.model.curve_head(&prehead);
                    curve_points_and_s90(&curve, elapsed_days, self.max_interval_days)
                })
                .collect(),
        )
    }

    pub fn review(
        &mut self,
        input: ReviewInput,
        state: ReviewState<'_>,
    ) -> io::Result<ReviewOutput> {
        let heads = self.review_heads(&input, &state)?;

        if !input.is_query {
            self.features.store_review(&input);
            self.curves.insert(input.card_id, heads.curve.clone());
            // an answer finds its curve's S90 now, so the Stats and `prop:s`
            // read it without a search (spec ui.rwkv-curve-stored-s90)
            if let Some(curve) = self.curves.get(&input.card_id) {
                self.stored_curve_s90(curve);
            }
            if let Some(sources) = &mut self.curve_sources {
                sources.indices.push(0);
                encode_curve_source(&heads.prehead, &mut sources.bytes);
            }
        }

        let answer_heads = if input.is_query {
            Some(self.answer_heads(&input, &state)?)
        } else {
            None
        };
        let (intervals, s90s) = answer_heads
            .as_ref()
            .map(|heads| self.answer_intervals(&input, heads))
            .unwrap_or(([None; 4], [None; 4]));
        let current = self.current_intervals(&input, &heads);
        let curve_retrievability = current_curve_retrievability(&input, &heads.curve);

        let card_state = serialize_module_state(&heads.next_state.card);
        let deck_state = serialize_module_state(&heads.next_state.deck);
        let note_state = serialize_module_state(&heads.next_state.note);
        let preset_state = serialize_module_state(&heads.next_state.preset);
        let global_state = serialize_module_state(&heads.next_state.global);

        if !input.is_query {
            self.warm_up_states.store(&input, heads.next_state);
        }

        Ok(ReviewOutput {
            retrievability: heads.retrievability,
            curve_retrievability,
            button_probabilities: heads.button_probabilities,
            current_interval: current.whole_days,
            current_s90: current.s90,
            intervals,
            s90s,
            card_state,
            deck_state,
            note_state,
            preset_state,
            global_state,
        })
    }

    pub fn predict_many(
        &mut self,
        requests: Vec<ReviewPredictionRequest>,
    ) -> io::Result<Vec<ReviewPredictionOutput>> {
        let mut work_items = Vec::with_capacity(requests.len() * (1 + ANSWER_EASES.len()));
        for request in &requests {
            if !request.input.is_query {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "RWKV batched prediction only supports query inputs",
                ));
            }

            work_items.push(self.prediction_work_item(&request.input, &request.state)?);
            for ease in ANSWER_EASES {
                work_items.push(self.prediction_work_item(
                    &simulated_answer_input(&request.input, ease),
                    &request.state,
                )?);
            }
        }

        let heads = self.model.review_many(&work_items);
        let chunk_size = 1 + ANSWER_EASES.len();
        Ok(heads
            .chunks_exact(chunk_size)
            .zip(requests)
            .map(|(heads, request)| {
                let query_heads = &heads[0];
                let answer_heads = &heads[1..];
                let (intervals, s90s) = self.answer_intervals(&request.input, answer_heads);
                let current = self.current_intervals(&request.input, query_heads);
                ReviewPredictionOutput {
                    retrievability: query_heads.retrievability,
                    curve_retrievability: current_curve_retrievability(
                        &request.input,
                        &query_heads.curve,
                    ),
                    button_probabilities: query_heads.button_probabilities,
                    current_interval: current.whole_days,
                    current_interval_unrounded: current.unrounded,
                    current_s90: current.s90,
                    intervals,
                    s90s,
                }
            })
            .collect())
    }

    pub fn predict_retrievability_many(
        &mut self,
        requests: Vec<ReviewPredictionRequest>,
    ) -> io::Result<Vec<f32>> {
        let mut work_items = Vec::with_capacity(requests.len());
        for request in &requests {
            if !request.input.is_query {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "RWKV batched retrievability prediction only supports query inputs",
                ));
            }

            work_items.push(self.prediction_work_item(&request.input, &request.state)?);
        }

        Ok(self.model.review_retrievability_many(&work_items))
    }

    pub fn predict_retrievability_many_from_warm_up(
        &mut self,
        inputs: Vec<ReviewInput>,
    ) -> io::Result<Vec<f32>> {
        for input in &inputs {
            if !input.is_query {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "RWKV batched retrievability prediction only supports query inputs",
                ));
            }
        }

        self.warm_up_states.ensure_loaded_many(&inputs)?;

        let features = inputs
            .iter()
            .map(|input| self.features.features_for(input))
            .collect::<Vec<_>>();
        let work_items = inputs
            .iter()
            .zip(features)
            .map(|(input, features)| ReviewPredictionBorrowedWorkItem {
                features,
                state: self.warm_up_states.state_ref(input),
            })
            .collect::<Vec<_>>();

        Ok(self.model.review_retrievability_many_borrowed(&work_items))
    }

    /// Current interval and S90 for each query input, from the curve RWKV
    /// stored for the card at its last answered review (the curve after that
    /// answer), for "Reschedule cards with RWKV-Curve" (spec
    /// sched.rwkv-curve-reschedule).
    ///
    /// The curve of a query row is never read here: training masks every
    /// curve loss on query rows (`ahead_mask = (1 - is_query) * has_label`),
    /// so that head output is never supervised, and card info, the Browser,
    /// filtered decks and Stats all read the stored curve
    /// (ui.rwkv-curve-r-stored-curve). No model pass runs and no state is
    /// read or changed. A card without a stored curve gets no interval and
    /// no S90; no other value stands in for it.
    pub fn predict_current_intervals_many_from_warm_up(
        &mut self,
        inputs: Vec<ReviewInput>,
    ) -> io::Result<Vec<ReviewIntervalPrediction>> {
        for input in &inputs {
            if !input.is_query {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "RWKV batched interval prediction only supports query inputs",
                ));
            }
        }

        Ok(inputs
            .iter()
            .map(|input| match self.curves.get(&input.card_id) {
                Some(curve) => {
                    let current = self.current_intervals_for_curve(input, curve);
                    ReviewIntervalPrediction {
                        curve_retrievability: current_curve_retrievability(input, curve),
                        current_interval: current.whole_days,
                        current_interval_unrounded: current.unrounded,
                        current_s90: current.s90,
                    }
                }
                None => ReviewIntervalPrediction {
                    curve_retrievability: None,
                    current_interval: None,
                    current_interval_unrounded: None,
                    current_s90: None,
                },
            })
            .collect())
    }

    /// Curve retrievability for each query input, straight from the resident
    /// warm-up state.
    ///
    /// This is the one number the Stats Retrievability graph draws under
    /// RWKV-Curve. `predict_many` returns the same value from the same query
    /// heads (`current_curve_retrievability(&input, &query_heads.curve)`), but
    /// it also runs the four simulated-answer passes and the current-interval
    /// crossing search, and neither of those feeds this value. `None` is the
    /// same "no curve value" that `predict_many` reports.
    pub fn predict_curve_retrievability_many_from_warm_up(
        &mut self,
        inputs: Vec<ReviewInput>,
    ) -> io::Result<Vec<Option<f32>>> {
        for input in &inputs {
            if !input.is_query {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "RWKV batched curve retrievability prediction only supports query inputs",
                ));
            }
        }

        self.warm_up_states.ensure_loaded_many(&inputs)?;

        let features = inputs
            .iter()
            .map(|input| self.features.features_for(input))
            .collect::<Vec<_>>();
        let work_items = inputs
            .iter()
            .zip(features)
            .map(|(input, features)| ReviewPredictionBorrowedWorkItem {
                features,
                state: self.warm_up_states.state_ref(input),
            })
            .collect::<Vec<_>>();

        let heads = self.model.review_many_borrowed(&work_items);
        Ok(inputs
            .iter()
            .zip(heads)
            .map(|(input, heads)| current_curve_retrievability(input, &heads.curve))
            .collect())
    }

    /// Total Knowledge under RWKV-Curve (spec ui.stats-total-knowledge): for
    /// each span, the recall of the curve RWKV stored at the card's last
    /// answered review in the warm-up, on each day from the span's
    /// `first_day` through its `last_day`, at the whole days since the span's
    /// `review_day`; summed per day over the spans. Returns the first day and
    /// the sums from it. A card without a stored curve adds nothing.
    pub fn curve_retrievability_day_sums_from_warm_up(
        &self,
        spans: &[RwkvCurveSpan],
    ) -> (i64, Vec<f64>) {
        let spans: Vec<&RwkvCurveSpan> = spans
            .iter()
            .filter(|span| span.first_day <= span.last_day)
            .collect();
        let (Some(first_day), Some(last_day)) = (
            spans.iter().map(|span| span.first_day).min(),
            spans.iter().map(|span| span.last_day).max(),
        ) else {
            return (0, vec![]);
        };
        let days = (last_day - first_day + 1) as usize;
        let sums = spans
            .par_iter()
            .fold(
                || vec![0.0; days],
                |mut sums, span| {
                    if let Some(curve) = self.curves.get(&span.card_id) {
                        for day in span.first_day..=span.last_day {
                            let elapsed_seconds = (day - span.review_day) * SECONDS_PER_DAY;
                            sums[(day - first_day) as usize] +=
                                predict_curve(curve, elapsed_seconds as f32) as f64;
                        }
                    }
                    sums
                },
            )
            .reduce(
                || vec![0.0; days],
                |mut a, b| {
                    a.iter_mut().zip(b).for_each(|(a, b)| *a += b);
                    a
                },
            );
        (first_day, sums)
    }

    pub fn predict_retrievability_many_after_review(
        &self,
        answer: ReviewInput,
        query_inputs: Vec<ReviewInput>,
        snapshot: RwkvWorkloadSimulationSnapshot,
    ) -> io::Result<Vec<f32>> {
        if answer.is_query || answer.ease.is_none() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV future prediction requires an answered review input",
            ));
        }
        if query_inputs.iter().any(|input| !input.is_query) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV future prediction only supports query inputs",
            ));
        }

        let Some(runtime_state) = snapshot.runtime_state.as_deref() else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV future prediction requires a runtime state snapshot",
            ));
        };
        let mut worker = self.worker_from_cache_state(runtime_state)?;
        let mut state_maps = ReviewStateMaps::from_serialized(
            self.model.ids,
            &snapshot.card_states,
            &snapshot.note_states,
            &snapshot.deck_states,
            &snapshot.preset_states,
            snapshot.global_state.as_deref(),
        )?;
        worker.apply_simulation_answer(&answer, &mut state_maps)?;

        Ok(worker
            .workload_query_predictions_for_inputs(&query_inputs, &state_maps)
            .into_iter()
            .map(|prediction| prediction.retrievability)
            .collect())
    }

    pub fn predict_retrievability_many_after_reviews(
        &self,
        answers: Vec<ReviewInput>,
        query_inputs: Vec<ReviewInput>,
        snapshot: RwkvWorkloadSimulationSnapshot,
    ) -> io::Result<Vec<Vec<f32>>> {
        for answer in &answers {
            if answer.is_query || answer.ease.is_none() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "RWKV future prediction requires answered review inputs",
                ));
            }
        }
        if query_inputs.iter().any(|input| !input.is_query) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV future prediction only supports query inputs",
            ));
        }

        let Some(runtime_state) = snapshot.runtime_state.as_deref() else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV future prediction requires a runtime state snapshot",
            ));
        };
        let mut base_features = read_runtime_feature_state(runtime_state, self.model.ids)?;
        let base_state_maps = ReviewStateMaps::from_serialized(
            self.model.ids,
            &snapshot.card_states,
            &snapshot.note_states,
            &snapshot.deck_states,
            &snapshot.preset_states,
            snapshot.global_state.as_deref(),
        )?;

        if query_inputs.is_empty() {
            return Ok(answers.iter().map(|_| Vec::new()).collect());
        }

        if answers.first().is_some_and(|first| {
            answers
                .iter()
                .chain(&query_inputs)
                .all(|input| same_feature_identity(first, input))
        }) {
            return Ok(predict_retrievability_many_after_reviews_for_identity(
                self.model.as_ref(),
                &mut base_features,
                &base_state_maps,
                answers,
                &query_inputs,
            ));
        }

        let answer_work_items = answers
            .iter()
            .map(|answer| ReviewPredictionWorkItem {
                features: base_features.features_for(answer),
                state: base_state_maps.state_owned(answer),
            })
            .collect::<Vec<_>>();
        let answer_heads = self.model.review_many(&answer_work_items);

        let query_count = query_inputs.len();
        let mut query_work_items = Vec::with_capacity(answers.len() * query_count);
        for (answer, heads) in answers.into_iter().zip(answer_heads) {
            let mut features = base_features.clone();
            features.store_review(&answer);
            for query_input in &query_inputs {
                query_work_items.push(ReviewPredictionWorkItem {
                    features: features.features_for(query_input),
                    state: base_state_maps.state_owned_after_review(
                        &answer,
                        query_input,
                        &heads.next_state,
                    ),
                });
            }
        }

        Ok(self
            .model
            .review_many(&query_work_items)
            .chunks_exact(query_count)
            .map(|heads| {
                heads
                    .iter()
                    .map(|heads| heads.retrievability)
                    .collect::<Vec<_>>()
            })
            .collect())
    }

    pub fn predict_retrievability_many_after_reviews_from_warm_up(
        &mut self,
        answers: Vec<ReviewInput>,
        query_inputs: Vec<ReviewInput>,
    ) -> io::Result<Vec<Vec<f32>>> {
        for answer in &answers {
            if answer.is_query || answer.ease.is_none() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "RWKV resident future prediction requires answered review inputs",
                ));
            }
        }
        if query_inputs.iter().any(|input| !input.is_query) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV resident future prediction only supports query inputs",
            ));
        }
        if answers.is_empty() {
            return Ok(Vec::new());
        }
        if query_inputs.is_empty() {
            return Ok(answers.iter().map(|_| Vec::new()).collect());
        }

        let first = &answers[0];
        if !answers
            .iter()
            .chain(&query_inputs)
            .all(|input| same_feature_identity(first, input))
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV resident future prediction requires one feature identity",
            ));
        }

        self.warm_up_states.ensure_loaded_many(&answers)?;
        self.warm_up_states.ensure_loaded_many(&query_inputs)?;

        Ok(predict_retrievability_many_after_reviews_for_identity(
            self.model.as_ref(),
            &mut self.features,
            &self.warm_up_states,
            answers,
            &query_inputs,
        ))
    }

    pub fn warm_up_reviews(
        &mut self,
        reviews: Vec<ReviewInput>,
        record_predictions: bool,
    ) -> io::Result<Vec<WarmUpPrediction>> {
        // The scan-capture harness records per-timestep coefficients from the
        // answer pass only, so it needs the per-review path.
        #[cfg(test)]
        if rwkv_scan_capture_active() {
            return self.warm_up_reviews_sequential(reviews, record_predictions);
        }
        bulk::warm_up_reviews_bulk(self, reviews, record_predictions)
    }

    /// The per-review reference implementation of `warm_up_reviews`. The bulk
    /// path must match it bit-for-bit; parity tests compare the two.
    #[cfg_attr(not(test), allow(dead_code))]
    fn warm_up_reviews_sequential(
        &mut self,
        reviews: Vec<ReviewInput>,
        record_predictions: bool,
    ) -> io::Result<Vec<WarmUpPrediction>> {
        let mut predictions = vec![];
        for (index, input) in reviews.into_iter().enumerate() {
            if input.ease.is_none() {
                continue;
            }

            let query_features = record_predictions.then(|| {
                let mut query_input = input.clone();
                query_input.is_query = true;
                query_input.ease = None;
                query_input.duration_millis = None;
                self.features.features_for(&query_input)
            });

            self.warm_up_states.ensure_loaded(&input)?;
            let features = self.features.features_for(&input);
            let model = &*self.model;
            // The query and answer inputs share card/note/deck/preset ids, so
            // both passes read the same pre-review state and can run in
            // parallel; only the answer pass advances state below.
            let state = self.warm_up_states.state_ref(&input);
            let (query_retrievability, heads) = match &query_features {
                Some(query_features) => {
                    let (retrievability, heads) = rayon::join(
                        || {
                            model.review_retrievability_query_refs(&[ReviewPredictionQueryRef {
                                features: query_features,
                                state,
                            }])[0]
                        },
                        || model.review(&features, state),
                    );
                    (Some(retrievability), heads)
                }
                None => (None, model.review(&features, state)),
            };
            if let Some(retrievability) = query_retrievability {
                // RWKV-Curve's prediction of THIS review is the curve the
                // card carried before it, read here while `self.curves`
                // still holds the previous answered review's curve, and
                // evaluated at the elapsed time of the input itself. A
                // card's first review has no such curve, and gets none.
                let curve_retrievability = self
                    .curves
                    .get(&input.card_id)
                    .and_then(|curve| current_curve_retrievability(&input, curve));
                predictions.push(WarmUpPrediction {
                    index,
                    retrievability,
                    curve_retrievability,
                });
            }

            self.features.store_review(&input);
            self.curves.insert(input.card_id, heads.curve.clone());
            if let Some(sources) = &mut self.curve_sources {
                sources.indices.push(index as u32);
                encode_curve_source(&heads.prehead, &mut sources.bytes);
            }
            self.warm_up_states.store(&input, heads.next_state);
        }

        Ok(predictions)
    }

    #[cfg(test)]
    fn warm_up_reviews_with_state_compression(
        &mut self,
        reviews: &[ReviewInput],
        compression: Option<StateCompression>,
    ) -> Vec<f32> {
        let mut predictions = Vec::new();
        for input in reviews {
            if input.ease.is_none() {
                continue;
            }

            self.warm_up_states.ensure_loaded(input).unwrap();
            let mut query_input = input.clone();
            query_input.is_query = true;
            query_input.ease = None;
            query_input.duration_millis = None;
            let features = self.features.features_for(&query_input);
            let query_heads = self
                .model
                .review(&features, self.warm_up_states.state_ref(&query_input));
            predictions.push(query_heads.retrievability);

            let features = self.features.features_for(input);
            let heads = self
                .model
                .review(&features, self.warm_up_states.state_ref(input));
            self.features.store_review(input);
            self.curves.insert(input.card_id, heads.curve.clone());
            let next_state = match compression {
                Some(compression) => compressed_srs_state(heads.next_state, compression),
                None => heads.next_state,
            };
            self.warm_up_states.store(input, next_state);
        }

        predictions
    }

    pub fn warm_up_snapshot(&mut self) -> io::Result<RwkvWarmUpSnapshot> {
        self.warm_up_states.force_load_all()?;
        Ok(RwkvWarmUpSnapshot {
            card_states: serialize_state_map(&self.warm_up_states.card),
            note_states: serialize_state_map(&self.warm_up_states.note),
            deck_states: serialize_state_map(&self.warm_up_states.deck),
            preset_states: serialize_state_map(&self.warm_up_states.preset),
            global_state: self
                .warm_up_states
                .global
                .as_ref()
                .map(serialize_module_state),
        })
    }

    pub fn warm_up_state(&mut self, input: &ReviewInput) -> io::Result<ReviewStateOwned> {
        self.warm_up_states.ensure_loaded(input)?;
        Ok(self.warm_up_states.serialized_state(input))
    }

    pub fn append_warm_up_snapshot_binary(&mut self, path: PathBuf) -> io::Result<()> {
        self.warm_up_states.force_load_all()?;
        let mut out = fs::OpenOptions::new().append(true).open(path)?;
        write_snapshot_state_map(&mut out, &self.warm_up_states.card)?;
        write_snapshot_state_map(&mut out, &self.warm_up_states.note)?;
        write_snapshot_state_map(&mut out, &self.warm_up_states.deck)?;
        write_snapshot_state_map(&mut out, &self.warm_up_states.preset)?;
        write_snapshot_optional_bytes(
            &mut out,
            self.warm_up_states
                .global
                .as_ref()
                .map(serialize_module_state)
                .as_deref(),
        )?;
        let cache_state = self.cache_state();
        write_snapshot_optional_bytes(&mut out, Some(&cache_state))
    }

    #[allow(clippy::too_many_arguments)]
    pub fn write_warm_up_state_checkpoint(
        &mut self,
        path: PathBuf,
        store_generation: &str,
        parent_segment_id: Option<i64>,
        last_review_id: i64,
        review_count: i64,
        history_hash: &str,
        replay_key: &str,
        previous_review_ids: &[u8],
        previous_intervals: &[u8],
        review_counts: &[u8],
        full: bool,
        durable: bool,
    ) -> io::Result<i64> {
        // A full checkpoint restates every entity, so nothing may be left
        // behind in the store it is about to replace.
        if full {
            self.warm_up_states.force_load_all()?;
        }
        self.warm_up_states.close_state_source();
        let runtime_state_len = self.runtime_cache_state_len();
        let store = match self.state_cache_store.take() {
            Some(store) if store.matches(&path, store_generation, durable) => store,
            _ => StateCacheStoreWriter::open(path, store_generation, durable)?,
        };
        let result = (|| {
            let mut connection = store
                .connection
                .lock()
                .map_err(|_| io::Error::other("RWKV state-cache connection lock poisoned"))?;
            let transaction = connection.transaction().map_err(state_cache_store_error)?;
            validate_state_cache_parent(&transaction, parent_segment_id)?;
            transaction
                .execute(
                    r#"
insert into segments (
  parent_id,
  last_review_id,
  review_count,
  history_hash,
  replay_key,
  previous_review_ids,
  previous_intervals,
  review_counts,
  runtime_state
) values (?, ?, ?, ?, ?, ?, ?, ?, zeroblob(?))
"#,
                    params![
                        parent_segment_id,
                        last_review_id,
                        review_count,
                        history_hash,
                        replay_key,
                        previous_review_ids,
                        previous_intervals,
                        review_counts,
                        i64::try_from(runtime_state_len).map_err(|_| {
                            io::Error::new(
                                io::ErrorKind::InvalidData,
                                "RWKV runtime state is too large",
                            )
                        })?,
                    ],
                )
                .map_err(state_cache_store_error)?;
            let segment_id = transaction.last_insert_rowid();
            {
                let mut runtime_state = transaction
                    .blob_open(MAIN_DB, "segments", "runtime_state", segment_id, false)
                    .map_err(state_cache_store_error)?;
                self.write_runtime_cache_state(&mut runtime_state)?;
                runtime_state.close().map_err(state_cache_store_error)?;
            }
            self.warm_up_states
                .write_checkpoint_rows(&transaction, segment_id, full)?;
            transaction.commit().map_err(state_cache_store_error)?;
            self.warm_up_states.mark_checkpoint_saved();
            Ok(segment_id)
        })();
        if result.is_ok() {
            self.state_cache_store = Some(store);
        }
        result
    }

    pub fn finish_warm_up_state_checkpoints(&mut self) -> io::Result<()> {
        self.warm_up_states.close_state_source();
        let Some(store) = self.state_cache_store.take() else {
            return Ok(());
        };
        let connection = store
            .connection
            .into_inner()
            .map_err(|_| io::Error::other("RWKV state-cache connection lock poisoned"))?;
        connection
            .close()
            .map_err(|(_, error)| state_cache_store_error(error))
    }

    pub fn restore_warm_up_state_checkpoint(
        &mut self,
        path: PathBuf,
        store_generation: &str,
        segment_id: i64,
    ) -> io::Result<()> {
        self.finish_warm_up_state_checkpoints()?;
        migrate_state_cache_store(&path)?;
        let connection = Connection::open_with_flags(
            &path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(state_cache_store_error)?;
        validate_state_cache_store(&connection, store_generation)?;
        let runtime_state = connection
            .query_row(
                "select runtime_state from segments where id = ?",
                [segment_id],
                |row| row.get::<_, Vec<u8>>(0),
            )
            .optional()
            .map_err(state_cache_store_error)?
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "missing RWKV state-cache segment",
                )
            })?;
        let segment_chain = state_cache_segment_chain(&connection, segment_id)?;
        // Only the key index is read here. The deck, preset and global states
        // are a few megabytes together and every prediction needs them, so
        // they are read now; the card and note states are read one at a time
        // when a card actually comes up.
        let index = build_state_cache_index(&connection, &segment_chain)?;
        let mut warm_up_states = ReviewStateMaps::new(self.model.ids);
        for (entity_id, row_id) in &index.deck {
            if let Some(state) =
                read_state_cache_entity(&connection, STATE_CACHE_KIND_DECK, *entity_id, *row_id)?
            {
                warm_up_states.deck.insert(*entity_id, state);
            }
        }
        for (entity_id, row_id) in &index.preset {
            if let Some(state) =
                read_state_cache_entity(&connection, STATE_CACHE_KIND_PRESET, *entity_id, *row_id)?
            {
                warm_up_states.preset.insert(*entity_id, state);
            }
        }
        if let Some(row_id) = index.global {
            warm_up_states.global = read_state_cache_entity(
                &connection,
                STATE_CACHE_KIND_GLOBAL,
                STATE_CACHE_GLOBAL_ENTITY_ID,
                row_id,
            )?;
        }
        warm_up_states.lazy = Some(LazyStateSource {
            path,
            store_generation: store_generation.to_string(),
            connection: Mutex::new(Some(connection)),
            card: index.card,
            note: index.note,
        });
        let (features, curves) =
            read_runtime_cache_state(&runtime_state, self.max_interval_days, self.model.ids)?;

        self.warm_up_states = warm_up_states;
        self.features = features;
        self.curves = curves;
        Ok(())
    }

    pub fn restore_warm_up_snapshot(&mut self, snapshot: RwkvWarmUpSnapshot) -> io::Result<()> {
        self.warm_up_states = ReviewStateMaps::from_serialized(
            self.model.ids,
            &snapshot.card_states,
            &snapshot.note_states,
            &snapshot.deck_states,
            &snapshot.preset_states,
            snapshot.global_state.as_deref(),
        )?;
        Ok(())
    }

    pub fn restore_warm_up_state(
        &mut self,
        card_id: i64,
        note_id: Option<i64>,
        deck_id: Option<i64>,
        preset_id: Option<i64>,
        state: ReviewStateOwned,
    ) -> io::Result<()> {
        self.warm_up_states
            .restore_serialized(card_id, note_id, deck_id, preset_id, &state)
    }

    pub fn reset_warm_up_state(&mut self) {
        self.state_cache_store = None;
        self.warm_up_states = ReviewStateMaps::new(self.model.ids);
    }

    fn review_heads(
        &mut self,
        input: &ReviewInput,
        state: &ReviewState<'_>,
    ) -> io::Result<ReviewHeads> {
        let card_state = deserialize_module_state(state.card)?;
        let deck_state = deserialize_module_state(state.deck)?;
        let note_state = deserialize_module_state(state.note)?;
        let preset_state = deserialize_module_state(state.preset)?;
        let global_state = deserialize_module_state(state.global)?;
        self.review_heads_for_state(
            input,
            SrsStateRef {
                card: card_state.as_ref(),
                deck: deck_state.as_ref(),
                note: note_state.as_ref(),
                preset: preset_state.as_ref(),
                global: global_state.as_ref(),
            },
        )
    }

    fn review_heads_for_state(
        &mut self,
        input: &ReviewInput,
        state: SrsStateRef<'_>,
    ) -> io::Result<ReviewHeads> {
        let features = self.features.features_for(input);
        Ok(self.model.review(&features, state))
    }

    fn prediction_work_item(
        &mut self,
        input: &ReviewInput,
        state: &ReviewStateOwned,
    ) -> io::Result<ReviewPredictionWorkItem> {
        Ok(ReviewPredictionWorkItem {
            state: state.deserialize()?,
            features: self.features.features_for(input),
        })
    }

    fn answer_heads(
        &mut self,
        input: &ReviewInput,
        state: &ReviewState<'_>,
    ) -> io::Result<[ReviewHeads; 4]> {
        let mut heads = Vec::with_capacity(ANSWER_EASES.len());
        for ease in ANSWER_EASES {
            heads.push(self.review_heads(&simulated_answer_input(input, ease), state)?);
        }

        match heads.try_into() {
            Ok(heads) => Ok(heads),
            Err(_) => unreachable!("answer head count should be fixed"),
        }
    }

    fn answer_intervals(
        &self,
        input: &ReviewInput,
        answer_heads: &[ReviewHeads],
    ) -> ([Option<f32>; 4], [Option<f32>; 4]) {
        let curves = std::array::from_fn(|index| &answer_heads[index].curve);
        let target_retentions = std::array::from_fn(|index| {
            input.target_retentions[index].unwrap_or(self.target_retention)
        });

        (
            unrounded_intervals_for_answer_curves(
                curves,
                target_retentions,
                self.max_interval_days,
                input.enforce_grade_order,
            ),
            unrounded_intervals_for_answer_curves(
                curves,
                [S90_TARGET_RETENTION; 4],
                self.max_interval_days,
                input.enforce_grade_order,
            ),
        )
    }

    /// The current interval where the curve meets the target retention, in
    /// whole days (`interval_for_curve`) and unrounded (for the RWKV-Curve
    /// reschedule, spec sched.rwkv-curve-reschedule), and the current S90.
    fn current_intervals(&self, input: &ReviewInput, heads: &ReviewHeads) -> CurrentIntervals {
        self.current_intervals_for_curve(input, &heads.curve)
    }

    fn current_intervals_for_curve(
        &self,
        input: &ReviewInput,
        curve: &ReviewCurve,
    ) -> CurrentIntervals {
        let target_retention = input.target_retentions[2].unwrap_or(self.target_retention);
        let unrounded =
            unrounded_interval_for_curve(curve, target_retention, self.max_interval_days);
        CurrentIntervals {
            whole_days: unrounded.map(|days| clamped_interval_days(days, self.max_interval_days)),
            unrounded,
            s90: unrounded_interval_for_curve(curve, S90_TARGET_RETENTION, self.max_interval_days),
        }
    }

    /// Forgets what this state holds of one card itself: its own feature
    /// counters and its stored curve, as if it had never been reviewed. A
    /// live answer that starts the card's history again (the first answer
    /// after Forget) calls it before the answer, so the card starts fresh as
    /// a rebuild starts it (spec sched.rwkv-replay-start-row); the caller
    /// passes no card state with that answer. The shared states keep the
    /// card's earlier reviews until the next rebuild. True when the state had
    /// seen the card, so that it now differs from a rebuild's.
    pub fn forget_card(&mut self, card_id: i64) -> bool {
        self.curves.remove(&card_id);
        self.features.forget_card(card_id)
    }

    pub fn state_for_card(&self, card_id: i64) -> RwkvInferenceState {
        RwkvInferenceState {
            feature_state: self.features.state_for_card(card_id),
            card_id,
            curve: self.curves.get(&card_id).cloned(),
        }
    }

    /// RWKV-Curve's forgetting curve for `card_id` as it was stored at the
    /// card's last answered review: the recall at each of `elapsed_days`, and
    /// the curve's own S90, for card info (spec ui.card-info-rwkv-curve).
    /// None when the card has no stored curve.
    pub fn card_curve(&self, card_id: i64, elapsed_days: &[f32]) -> Option<(Vec<f32>, f32)> {
        let curve = self.curves.get(&card_id)?;
        let s90 = self.stored_curve_s90(curve)?;
        let recall = elapsed_days
            .iter()
            .map(|days| predict_curve(curve, days * SECONDS_PER_DAY as f32))
            .collect();
        Some((recall, s90))
    }

    /// The S90 of each card's stored curve, as `card_curve` gives it (spec
    /// ui.rwkv-curve-stored-s90), None for a card without one. A curve's S90
    /// is found once and kept with the curve, so this is a lookup; the
    /// curves that do not have it yet (stored by a replay, or read from an
    /// older state) get it here, in parallel.
    pub fn card_curve_s90s(&self, card_ids: &[i64]) -> Vec<Option<f32>> {
        card_ids
            .par_iter()
            .map(|card_id| {
                self.curves
                    .get(card_id)
                    .and_then(|curve| self.stored_curve_s90(curve))
            })
            .collect()
    }

    /// The S90 of a stored curve at this instance's maximum interval
    /// (`unrounded_interval_for_curve`), found on the first call and kept
    /// with the curve.
    fn stored_curve_s90(&self, curve: &ReviewCurve) -> Option<f32> {
        *curve.s90.get_or_init(|| {
            unrounded_interval_for_curve(curve, S90_TARGET_RETENTION, self.max_interval_days)
        })
    }

    /// The curves RWKV-Curve stored for `card_ids` at each card's last
    /// answered review, packed for Advance and Postpone in the collection
    /// (spec sched.advance-postpone-algorithm; `unpack_stored_curves`): the
    /// ids of the cards that have one, in the order asked, and the curves.
    /// A card without a stored curve is left out.
    pub fn card_curve_weights(&self, card_ids: &[i64]) -> (Vec<i64>, Vec<u8>) {
        pack_stored_curves(&self.curves, card_ids)
    }

    pub fn restore_state(&mut self, state: &RwkvInferenceState) {
        self.features.restore_state(&state.feature_state);
        if let Some(curve) = &state.curve {
            self.curves.insert(state.card_id, curve.clone());
        } else {
            self.curves.remove(&state.card_id);
        }
    }

    pub fn cache_state(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(self.runtime_cache_state_len());
        self.write_runtime_cache_state(&mut out)
            .expect("writing to a Vec should not fail");
        out
    }

    fn runtime_cache_state_len(&self) -> usize {
        RUNTIME_CACHE_STATE_MAGIC.len()
            + self.features.cache_state_len()
            + 4
            + 4
            + 4
            + self
                .curves
                .values()
                .map(|curve| 8 + curve.cache_state_len() + 4)
                .sum::<usize>()
    }

    /// The runtime state: the features, then how the curves' S90s were
    /// found (`STORED_CURVE_S90_KERNEL`, the maximum interval), then each
    /// stored curve with its S90 (NaN when it was not found yet).
    fn write_runtime_cache_state(&self, out: &mut impl io::Write) -> io::Result<()> {
        out.write_all(RUNTIME_CACHE_STATE_MAGIC)?;
        self.features.write_cache_state_to(out)?;
        write_state_cache_u32(out, STORED_CURVE_S90_KERNEL as usize)?;
        write_state_cache_u32(out, self.max_interval_days as usize)?;
        write_state_cache_u32(out, self.curves.len())?;
        let mut curves: Vec<_> = self.curves.iter().collect();
        curves.sort_by_key(|(card_id, _)| *card_id);
        for (card_id, curve) in curves {
            out.write_all(&card_id.to_le_bytes())?;
            curve.write_cache_state_to(out)?;
            let s90 = curve.s90.get().copied().flatten().unwrap_or(f32::NAN);
            out.write_all(&s90.to_le_bytes())?;
        }
        Ok(())
    }

    pub fn restore_cache_state(&mut self, bytes: &[u8]) -> io::Result<()> {
        let (features, curves) =
            read_runtime_cache_state(bytes, self.max_interval_days, self.model.ids)?;
        self.features = features;
        self.curves = curves;
        Ok(())
    }

    fn worker_from_cache_state(&self, bytes: &[u8]) -> io::Result<RwkvInference> {
        let (features, curves) =
            read_runtime_cache_state(bytes, self.max_interval_days, self.model.ids)?;
        Ok(self.workload_worker(features, curves))
    }

    pub fn simulate_workload(
        &mut self,
        inputs: Vec<RwkvWorkloadSimulationInput>,
        mut snapshot: RwkvWorkloadSimulationSnapshot,
        config: RwkvWorkloadSimulationConfig,
        progress: &mut dyn FnMut(u32, u32) -> io::Result<()>,
    ) -> io::Result<RwkvWorkloadSimulationOutput> {
        if config.min_dr > config.max_dr {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV workload min DR must be <= max DR",
            ));
        }
        let Some(runtime_state) = snapshot.runtime_state.take() else {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "RWKV workload simulation requires a runtime state snapshot",
            ));
        };

        let target_dr_step = config.target_dr_step.max(1);
        let target_drs = target_drs(config.min_dr, config.max_dr, target_dr_step);
        let total_steps = target_drs.len() as u32 + 1;
        progress(0, total_steps)?;
        let mut reviewless_worker = self.worker_from_cache_state(&runtime_state)?;
        let base_features = reviewless_worker.features.clone();
        let base_curves = reviewless_worker.curves.clone();
        let reviewless_state_maps = ReviewStateMaps::from_serialized(
            self.model.ids,
            &snapshot.card_states,
            &snapshot.note_states,
            &snapshot.deck_states,
            &snapshot.preset_states,
            snapshot.global_state.as_deref(),
        )?;
        let introduced_inputs = inputs
            .iter()
            .filter(|input| input.review_input.card_type != Some(CardType::New as i64))
            .cloned()
            .collect::<Vec<_>>();
        let (reviewless_end_memorized, reviewless_end_weighted_memorized) = reviewless_worker
            .simulation_memorized_for_inputs(
                &introduced_inputs,
                &reviewless_state_maps,
                S90_TARGET_RETENTION,
                config.days_to_simulate as i64,
            )?;
        progress(1, total_steps)?;

        let mut points = self.simulate_workload_targets(
            RwkvWorkloadTargetSweep {
                inputs,
                snapshot,
                target_drs,
                days_to_simulate: config.days_to_simulate as i64,
                review_limit: config.review_limit as usize,
                new_limit: config.new_limit as usize,
                new_cards_ignore_review_limit: config.new_cards_ignore_review_limit,
                max_interval: config.max_interval.max(1),
                review_order: config.review_order,
                suspend_after_lapses: config.suspend_after_lapses,
                state_update_interval: config.state_update_interval.max(1),
                review_model: config.review_model,
                base_features,
                base_curves,
                total_steps,
            },
            progress,
        )?;
        enforce_monotonic_workload_review_counts(&mut points);

        Ok(RwkvWorkloadSimulationOutput {
            reviewless_end_memorized,
            reviewless_end_weighted_memorized,
            points,
        })
    }

    fn simulate_workload_targets(
        &self,
        sweep: RwkvWorkloadTargetSweep,
        progress: &mut dyn FnMut(u32, u32) -> io::Result<()>,
    ) -> io::Result<Vec<(u32, RwkvWorkloadSimulationPoint)>> {
        let RwkvWorkloadTargetSweep {
            inputs,
            snapshot,
            target_drs,
            days_to_simulate,
            review_limit,
            new_limit,
            new_cards_ignore_review_limit,
            max_interval,
            review_order,
            suspend_after_lapses,
            state_update_interval,
            review_model,
            base_features,
            base_curves,
            total_steps,
        } = sweep;

        let target_count = target_drs.len();
        let mut results = Vec::with_capacity(target_count);
        // The warmed runtime state can be large, so keep target DR workers
        // sequential instead of cloning the state for every target at once.
        for (offset, dr) in target_drs.into_iter().enumerate() {
            let mut worker = self.workload_worker(base_features.clone(), base_curves.clone());
            let mut state_maps = ReviewStateMaps::from_serialized(
                self.model.ids,
                &snapshot.card_states,
                &snapshot.note_states,
                &snapshot.deck_states,
                &snapshot.preset_states,
                snapshot.global_state.as_deref(),
            )?;
            let point = worker.simulate_workload_for_target(
                &inputs,
                &mut state_maps,
                RwkvWorkloadTargetConfig {
                    dr,
                    days_to_simulate,
                    review_limit,
                    new_limit,
                    new_cards_ignore_review_limit,
                    max_interval,
                    review_order,
                    suspend_after_lapses,
                    state_update_interval,
                    review_model: &review_model,
                },
            )?;
            results.push((offset, dr, point));
            progress(offset as u32 + 2, total_steps)?;
        }

        results.sort_by_key(|(offset, _, _)| *offset);
        Ok(results
            .into_iter()
            .map(|(_, dr, point)| (dr, point))
            .collect())
    }

    fn workload_worker(
        &self,
        features: FeatureState,
        curves: HashMap<i64, ReviewCurve>,
    ) -> RwkvInference {
        RwkvInference {
            model: Arc::clone(&self.model),
            features,
            curves,
            warm_up_states: ReviewStateMaps::new(self.model.ids),
            state_cache_store: None,
            target_retention: self.target_retention,
            max_interval_days: self.max_interval_days,
            curve_sources: None,
        }
    }

    fn simulate_workload_for_target(
        &mut self,
        inputs: &[RwkvWorkloadSimulationInput],
        state_maps: &mut ReviewStateMaps,
        config: RwkvWorkloadTargetConfig<'_>,
    ) -> io::Result<RwkvWorkloadSimulationPoint> {
        let target_retention = config.dr as f32 / 100.0;
        let query_inputs: Vec<_> = inputs
            .iter()
            .map(|input| simulation_query_input(input, target_retention, 0))
            .collect();
        let predictions = self.workload_query_predictions_for_inputs(&query_inputs, state_maps);
        let mut cards = inputs
            .iter()
            .zip(predictions.iter())
            .map(|(input, prediction)| simulation_card(input, prediction, target_retention))
            .collect::<Vec<_>>();

        let mut total_cost = 0.0;
        let mut review_count: u32 = 0;
        for day in 0..config.days_to_simulate {
            let mut due_review_indexes: Vec<_> = cards
                .iter()
                .enumerate()
                .filter_map(|(index, card)| {
                    (!card.is_new && !card.suspended && card.due_day <= day).then_some(index)
                })
                .collect();
            let order_inputs = due_review_indexes
                .iter()
                .map(|index| simulation_query_input_for_card(&cards[*index], target_retention, day))
                .collect::<Vec<_>>();
            let order_predictions =
                self.workload_query_predictions_for_inputs(&order_inputs, state_maps);
            let prediction_by_index = due_review_indexes
                .iter()
                .copied()
                .zip(order_predictions)
                .collect::<HashMap<_, _>>();
            due_review_indexes.sort_by_key(|index| {
                simulation_review_priority(
                    &cards[*index],
                    prediction_by_index.get(index),
                    config.review_order,
                    target_retention,
                )
            });
            if config.review_limit > 0 {
                due_review_indexes.truncate(config.review_limit);
            } else {
                due_review_indexes.clear();
            }

            let mut due_new_indexes = cards
                .iter()
                .enumerate()
                .filter_map(|(index, card)| {
                    (card.is_new && !card.suspended && card.due_day <= day).then_some(index)
                })
                .collect::<Vec<_>>();
            due_new_indexes.sort_by_key(|index| cards[*index].review_input.review_input.card_id);
            due_new_indexes.truncate(config.new_limit);
            if !config.new_cards_ignore_review_limit {
                due_new_indexes
                    .truncate(config.review_limit.saturating_sub(due_review_indexes.len()));
            }
            let mut due_indexes = due_review_indexes;
            due_indexes.extend(due_new_indexes);

            let mut offset = 0;
            while offset < due_indexes.len() {
                let reviews_until_state_update =
                    config.state_update_interval - (review_count % config.state_update_interval);
                let chunk_len =
                    (reviews_until_state_update as usize).min(due_indexes.len() - offset);
                let chunk_indexes = &due_indexes[offset..offset + chunk_len];
                let query_inputs = chunk_indexes
                    .iter()
                    .map(|index| {
                        simulation_query_input_for_card(&cards[*index], target_retention, day)
                    })
                    .collect::<Vec<_>>();
                let predictions =
                    self.workload_query_predictions_for_inputs(&query_inputs, state_maps);
                let eases = chunk_indexes
                    .iter()
                    .zip(&predictions)
                    .map(|(index, prediction)| {
                        let retrievability = valid_probability_or_default(
                            prediction.retrievability,
                            target_retention,
                        );
                        let probabilities = config.review_model.probabilities_for(retrievability);
                        simulation_grade(
                            probabilities,
                            cards[*index].review_input.review_input.card_id,
                            day,
                            cards[*index].reps,
                        )
                    })
                    .collect::<Vec<_>>();
                let intervals =
                    self.selected_answer_intervals_for_inputs(&query_inputs, state_maps, &eases);

                for ((position, index), ease) in chunk_indexes.iter().enumerate().zip(&eases) {
                    let grade_seconds = config.review_model.grade_seconds[(*ease - 1) as usize];
                    total_cost += grade_seconds;
                    review_count += 1;

                    let duration_millis = (grade_seconds * 1000.0).round().max(1.0) as i64;
                    let answer_input = simulation_answer_input(
                        &cards[*index],
                        target_retention,
                        day,
                        *ease,
                        duration_millis,
                    );
                    let interval = intervals[position]
                        .unwrap_or(1)
                        .clamp(1, config.max_interval);
                    if review_count % config.state_update_interval == 0 {
                        self.apply_simulation_answer(&answer_input, state_maps)?;
                    }

                    cards[*index].last_review_day = day;
                    cards[*index].due_day = day + interval as i64;
                    cards[*index].interval_days = interval as i64;
                    cards[*index].reps += 1;
                    if cards[*index].is_new {
                        cards[*index].is_new = false;
                        cards[*index].review_input.review_input.card_type =
                            Some(CardType::Review as i64);
                    }
                    if *ease == 1 {
                        cards[*index].lapses += 1;
                        if config
                            .suspend_after_lapses
                            .is_some_and(|threshold| cards[*index].lapses >= threshold as i64)
                        {
                            cards[*index].suspended = true;
                        }
                    }
                }
                offset += chunk_len;
            }
        }

        let (memorized, weighted_memorized) = self.simulation_memorized_for_cards(
            &cards,
            state_maps,
            target_retention,
            config.days_to_simulate,
        )?;
        Ok(RwkvWorkloadSimulationPoint {
            memorized,
            weighted_memorized,
            cost: total_cost,
            review_count,
        })
    }

    fn workload_query_predictions_for_inputs(
        &mut self,
        inputs: &[ReviewInput],
        state_maps: &ReviewStateMaps,
    ) -> Vec<RwkvWorkloadQueryPrediction> {
        let work_items = inputs
            .iter()
            .map(|input| ReviewPredictionWorkItem {
                features: self.features.features_for(input),
                state: state_maps.state_owned(input),
            })
            .collect::<Vec<_>>();
        self.model
            .review_many(&work_items)
            .into_iter()
            .zip(inputs)
            .map(|(heads, input)| {
                let current = self.current_intervals(input, &heads);
                RwkvWorkloadQueryPrediction {
                    retrievability: heads.retrievability,
                    current_interval: current.whole_days,
                    current_s90: current.s90,
                }
            })
            .collect()
    }

    fn selected_answer_intervals_for_inputs(
        &mut self,
        inputs: &[ReviewInput],
        state_maps: &ReviewStateMaps,
        eases: &[u8],
    ) -> Vec<Option<u32>> {
        debug_assert_eq!(inputs.len(), eases.len());
        let answer_inputs = inputs
            .iter()
            .zip(eases)
            .map(|(input, ease)| simulated_answer_input(input, *ease))
            .collect::<Vec<_>>();
        let work_items = answer_inputs
            .iter()
            .map(|input| ReviewPredictionWorkItem {
                features: self.features.features_for(input),
                state: state_maps.state_owned(input),
            })
            .collect::<Vec<_>>();
        self.model
            .review_many(&work_items)
            .into_iter()
            .zip(inputs.iter().zip(eases))
            .map(|(heads, (input, ease))| {
                let target_retention =
                    input.target_retentions[(*ease - 1) as usize].unwrap_or(self.target_retention);
                interval_for_curve(&heads.curve, target_retention, self.max_interval_days)
            })
            .collect()
    }

    fn apply_simulation_answer(
        &mut self,
        input: &ReviewInput,
        state_maps: &mut ReviewStateMaps,
    ) -> io::Result<()> {
        let heads = self.review_heads_for_state(input, state_maps.state_ref(input))?;
        self.features.store_review(input);
        self.curves.insert(input.card_id, heads.curve.clone());
        state_maps.store(input, heads.next_state);
        Ok(())
    }

    fn simulation_memorized_for_inputs(
        &mut self,
        inputs: &[RwkvWorkloadSimulationInput],
        state_maps: &ReviewStateMaps,
        target_retention: f32,
        day: i64,
    ) -> io::Result<(f32, f32)> {
        let query_inputs = inputs
            .iter()
            .map(|input| simulation_query_input(input, target_retention, day))
            .collect::<Vec<_>>();
        let predictions = self.workload_query_predictions_for_inputs(&query_inputs, state_maps);
        Ok(memorized_from_workload_predictions(&predictions))
    }

    fn simulation_memorized_for_cards(
        &mut self,
        cards: &[RwkvSimulationCard],
        state_maps: &ReviewStateMaps,
        target_retention: f32,
        day: i64,
    ) -> io::Result<(f32, f32)> {
        let query_inputs = cards
            .iter()
            .filter(|card| !card.is_new)
            .map(|card| simulation_query_input_for_card(card, target_retention, day))
            .collect::<Vec<_>>();
        let predictions = self.workload_query_predictions_for_inputs(&query_inputs, state_maps);
        Ok(memorized_from_workload_predictions(&predictions))
    }
}

fn same_feature_identity(left: &ReviewInput, right: &ReviewInput) -> bool {
    left.card_id == right.card_id
        && left.note_id == right.note_id
        && left.deck_id == right.deck_id
        && left.preset_id == right.preset_id
}

fn predict_retrievability_many_after_reviews_for_identity(
    model: &SrsModel,
    features: &mut FeatureState,
    state_maps: &ReviewStateMaps,
    answers: Vec<ReviewInput>,
    query_inputs: &[ReviewInput],
) -> Vec<Vec<f32>> {
    let answer_work_items = answers
        .iter()
        .map(|answer| ReviewPredictionWorkItem {
            features: features.features_for(answer),
            state: state_maps.state_owned(answer),
        })
        .collect::<Vec<_>>();
    let answer_heads = model.review_many(&answer_work_items);

    let query_count = query_inputs.len();
    let mut query_work_items = Vec::with_capacity(answers.len() * query_count);
    for (answer, heads) in answers.into_iter().zip(answer_heads) {
        let feature_state = features.state_for_card(answer.card_id);
        features.store_review(&answer);
        for query_input in query_inputs {
            query_work_items.push(ReviewPredictionWorkItem {
                features: features.features_for(query_input),
                state: state_maps.state_owned_after_review(&answer, query_input, &heads.next_state),
            });
        }
        features.restore_state(&feature_state);
    }

    model
        .review_many(&query_work_items)
        .chunks_exact(query_count)
        .map(|heads| heads.iter().map(|heads| heads.retrievability).collect())
        .collect()
}

/// The magic of the runtime state `write_runtime_cache_state` writes.
const RUNTIME_CACHE_STATE_MAGIC: &[u8] = b"ARWKVPROCSTATE4";
/// The runtime state before the curves carried their S90 (the format of
/// spec sched.rwkv-id-codes, which no longer saves the id codes). It is
/// still read: each S90 is then found when it is first asked for (spec
/// ui.rwkv-curve-stored-s90). Older states (`ARWKVPROCSTATE2`) hold another
/// replay's features and are not read.
const RUNTIME_CACHE_STATE_MAGIC_WITHOUT_S90: &[u8] = b"ARWKVPROCSTATE3";
/// The version of how a stored curve's S90 is found (`predict_curve` and
/// `unrounded_interval_for_curve`). A saved S90 found another way is found
/// again. The S90 depends on nothing else but the curve's own weights and
/// the maximum interval: another model gives other curves, and the whole
/// runtime state is then another model's (Python keys it on the model's
/// SHA-256 and rebuilds it).
const STORED_CURVE_S90_KERNEL: u32 = 1;

/// Reads the magic of a runtime state: true when its curves carry their
/// S90 (the current format), false for the format before it.
fn read_runtime_cache_state_magic(cursor: &mut Cursor<'_>) -> io::Result<bool> {
    let found = cursor.bytes(RUNTIME_CACHE_STATE_MAGIC.len())?;
    if found == RUNTIME_CACHE_STATE_MAGIC {
        Ok(true)
    } else if found == RUNTIME_CACHE_STATE_MAGIC_WITHOUT_S90 {
        Ok(false)
    } else {
        Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "unexpected file magic",
        ))
    }
}

fn read_runtime_feature_state(bytes: &[u8], ids: RwkvIdPipeline) -> io::Result<FeatureState> {
    let mut cursor = Cursor::new(bytes);
    let with_s90 = read_runtime_cache_state_magic(&mut cursor)?;
    let features = FeatureState::read_cache_state(&mut cursor, ids)?;
    if with_s90 {
        cursor.u32()?;
        cursor.u32()?;
    }
    let curve_count = cursor.u32()? as usize;
    for _ in 0..curve_count {
        cursor.i64()?;
        cursor.skip_f32_vec()?;
        cursor.skip_f32_vec()?;
        if with_s90 {
            cursor.f32()?;
        }
    }
    cursor.expect_end()?;
    Ok(features)
}

/// The features and the stored curves of a runtime state. A curve keeps the
/// S90 the state holds for it only when that S90 was found the current way
/// (`STORED_CURVE_S90_KERNEL`) at `max_interval_days`; otherwise the S90 is
/// found again when asked for.
fn read_runtime_cache_state(
    bytes: &[u8],
    max_interval_days: u32,
    ids: RwkvIdPipeline,
) -> io::Result<(FeatureState, HashMap<i64, ReviewCurve>)> {
    let mut cursor = Cursor::new(bytes);
    let with_s90 = read_runtime_cache_state_magic(&mut cursor)?;
    let features = FeatureState::read_cache_state(&mut cursor, ids)?;
    let s90_found_as = if with_s90 {
        Some((cursor.u32()?, cursor.u32()?))
    } else {
        None
    };
    let curve_count = cursor.u32()? as usize;
    let mut curves = HashMap::with_capacity(curve_count);
    for _ in 0..curve_count {
        let card_id = cursor.i64()?;
        let curve = ReviewCurve::read_cache_state(&mut cursor)?;
        if let Some(found_as) = s90_found_as {
            let s90 = cursor.f32()?;
            if found_as == (STORED_CURVE_S90_KERNEL, max_interval_days) && !s90.is_nan() {
                let _ = curve.s90.set(Some(s90));
            }
        }
        curves.insert(card_id, curve);
    }
    cursor.expect_end()?;
    Ok((features, curves))
}

/// One answer-button probe of a query: the same row with `ease` pressed.
///
/// The probe keeps the query's `duration_millis`, which is `None` (the press
/// has not happened yet), so `scaled_duration` gives it 0.0. For the shipped
/// model that value is the training mean of the scaled duration (about
/// 7.3 s): the model was trained with the real duration on every answered row,
/// and the mean is the stand-in we keep for it.
///
/// A NEW model's encoder must give these probes, and the query row, the
/// "no press yet" encoding instead: scaled duration 0.0 because the press has
/// not happened, the same constant on the query row and on all four probes,
/// which is what such a model is trained with. See the RWKV session's
/// `optimization/DEPLOY_FUNCTIONS.md`, sections 4 and 6 (section 6 also gives
/// the imputed value a model trained under `RWKV_PROBE_IMPUTE_K` expects).
fn simulated_answer_input(input: &ReviewInput, ease: u8) -> ReviewInput {
    let mut input = input.clone();
    input.is_query = false;
    input.ease = Some(ease);
    input
}

fn target_drs(min_dr: u32, max_dr: u32, step: u32) -> Vec<u32> {
    let mut values = Vec::new();
    let mut dr = min_dr;
    while dr <= max_dr {
        values.push(dr);
        match dr.checked_add(step) {
            Some(next) => dr = next,
            None => break,
        }
    }
    if values.last().copied() != Some(max_dr) {
        values.push(max_dr);
    }
    values
}

fn simulation_card(
    input: &RwkvWorkloadSimulationInput,
    prediction: &RwkvWorkloadQueryPrediction,
    target_retention: f32,
) -> RwkvSimulationCard {
    let elapsed_days = input_elapsed_days(&input.review_input);
    let mut due_day = 0;
    match prediction.current_interval {
        Some(current_interval)
            if prediction.retrievability.is_finite()
                && prediction.retrievability > target_retention =>
        {
            due_day = (current_interval as i64 - elapsed_days).max(1);
        }
        _ => {}
    }
    RwkvSimulationCard {
        review_input: input.clone(),
        due_day,
        last_review_day: -elapsed_days,
        reps: input.reps.unwrap_or(0),
        lapses: input.lapses.unwrap_or(0),
        interval_days: input.interval_days.unwrap_or(1).max(1),
        ease_factor: input.ease_factor.unwrap_or(0),
        is_new: input.review_input.card_type == Some(CardType::New as i64),
        suspended: false,
    }
}

fn simulation_review_priority(
    card: &RwkvSimulationCard,
    prediction: Option<&RwkvWorkloadQueryPrediction>,
    review_order: ReviewCardOrder,
    target_retention: f32,
) -> (i64, i64) {
    let card_id = card.review_input.review_input.card_id;
    let retrievability = valid_probability_or_default(
        prediction
            .map(|prediction| prediction.retrievability)
            .unwrap_or(target_retention),
        target_retention,
    );
    let scaled_retrievability = (retrievability * 1_000_000.0).round() as i64;
    let priority = match review_order {
        ReviewCardOrder::IntervalsAscending => card.interval_days,
        ReviewCardOrder::IntervalsDescending => -card.interval_days,
        ReviewCardOrder::EaseAscending => card.ease_factor,
        ReviewCardOrder::EaseDescending => -card.ease_factor,
        ReviewCardOrder::RetrievabilityAscending => scaled_retrievability,
        ReviewCardOrder::RetrievabilityDescending => -scaled_retrievability,
        ReviewCardOrder::RelativeOverdueness => {
            (relative_overdueness(retrievability, target_retention) * 1_000_000.0).round() as i64
        }
        ReviewCardOrder::Random => (simulation_unit_hash(card_id, 0, 0) * i64::MAX as f64) as i64,
        ReviewCardOrder::Added => card_id,
        ReviewCardOrder::ReverseAdded => -card_id,
        ReviewCardOrder::Day | ReviewCardOrder::DayThenDeck | ReviewCardOrder::DeckThenDay => {
            card.due_day
        }
    };
    (priority, card_id)
}

fn simulation_query_input(
    input: &RwkvWorkloadSimulationInput,
    target_retention: f32,
    day: i64,
) -> ReviewInput {
    let elapsed_days = input_elapsed_days(&input.review_input) + day;
    simulation_input(
        &input.review_input,
        target_retention,
        day,
        elapsed_days,
        true,
        None,
        None,
    )
}

fn simulation_query_input_for_card(
    card: &RwkvSimulationCard,
    target_retention: f32,
    day: i64,
) -> ReviewInput {
    let elapsed_days = (day - card.last_review_day).max(0);
    simulation_input(
        &card.review_input.review_input,
        target_retention,
        day,
        elapsed_days,
        true,
        None,
        None,
    )
}

fn simulation_answer_input(
    card: &RwkvSimulationCard,
    target_retention: f32,
    day: i64,
    ease: u8,
    duration_millis: i64,
) -> ReviewInput {
    let elapsed_days = (day - card.last_review_day).max(0);
    simulation_input(
        &card.review_input.review_input,
        target_retention,
        day,
        elapsed_days,
        false,
        Some(ease),
        Some(duration_millis),
    )
}

fn simulation_input(
    input: &ReviewInput,
    target_retention: f32,
    day: i64,
    elapsed_days: i64,
    is_query: bool,
    ease: Option<u8>,
    duration_millis: Option<i64>,
) -> ReviewInput {
    let mut input = input.clone();
    input.is_query = is_query;
    input.ease = ease;
    input.duration_millis = duration_millis;
    input.day_offset = input.day_offset.map(|day_offset| day_offset + day);
    input.current_elapsed_days = Some(elapsed_days);
    input.current_elapsed_seconds = None;
    input.target_retentions = [
        Some(target_retention),
        Some(target_retention),
        Some(target_retention),
        Some(target_retention),
    ];
    input
}

fn input_elapsed_days(input: &ReviewInput) -> i64 {
    input.current_elapsed_days.unwrap_or(0).max(0)
}

fn memorized_from_workload_predictions(predictions: &[RwkvWorkloadQueryPrediction]) -> (f32, f32) {
    let mut memorized = 0.0;
    let mut weighted = 0.0;
    for prediction in predictions {
        if !valid_probability(prediction.retrievability) {
            continue;
        }
        memorized += prediction.retrievability;
        weighted += prediction.retrievability * s90_weight(prediction.current_s90);
    }
    (memorized, weighted)
}

fn s90_weight(current_s90: Option<f32>) -> f32 {
    let Some(current_s90) = current_s90 else {
        return 1.0;
    };
    if current_s90 <= 0.0 {
        return 1.0;
    }
    1.0 - ((-8.0 / 365.0) * current_s90).exp()
}

fn valid_probability(value: f32) -> bool {
    value.is_finite() && (0.0..=1.0).contains(&value)
}

fn valid_probability_or_default(value: f32, default: f32) -> f32 {
    if valid_probability(value) {
        value
    } else {
        default
    }
}

impl RwkvWorkloadReviewModel {
    fn probabilities_for(&self, retrievability: f32) -> [f32; 4] {
        let bucket = simulator_bucket(retrievability);
        self.bucket_probabilities
            .iter()
            .find_map(|(candidate, probabilities)| (*candidate == bucket).then_some(*probabilities))
            .unwrap_or_else(|| fallback_grade_probabilities(retrievability))
    }
}

fn fallback_grade_probabilities(retrievability: f32) -> [f32; 4] {
    let r = if retrievability.is_finite() {
        retrievability.clamp(0.0, 1.0)
    } else {
        0.9
    };
    let again = (1.0 - r).clamp(0.02, 0.85);
    let success = 1.0 - again;
    let mut hard_share = ((0.95 - r) / 0.45).clamp(0.10, 0.45);
    let mut easy_share = ((r - 0.75) / 0.25).clamp(0.05, 0.35);
    if hard_share + easy_share > 0.90 {
        let scale = 0.90 / (hard_share + easy_share);
        hard_share *= scale;
        easy_share *= scale;
    }
    let hard = success * hard_share;
    let easy = success * easy_share;
    let good = (success - hard - easy).max(0.0);
    let total = again + hard + good + easy;
    [again / total, hard / total, good / total, easy / total]
}

fn simulation_grade(probabilities: [f32; 4], card_id: i64, day: i64, reps: i64) -> u8 {
    let threshold = simulation_unit_hash(card_id, day, reps);
    let mut cumulative = 0.0;
    for (index, probability) in probabilities.iter().enumerate() {
        cumulative += *probability as f64;
        if threshold <= cumulative {
            return (index + 1) as u8;
        }
    }
    4
}

fn enforce_monotonic_workload_review_counts(points: &mut [(u32, RwkvWorkloadSimulationPoint)]) {
    let mut indexes: Vec<_> = (0..points.len()).collect();
    indexes.sort_by_key(|index| points[*index].0);

    let mut running = 0;
    for index in indexes {
        running = running.max(points[index].1.review_count);
        points[index].1.review_count = running;
    }
}

fn simulation_unit_hash(card_id: i64, day: i64, reps: i64) -> f64 {
    let mut value =
        (card_id as u64) ^ ((day as u64).wrapping_add(1)).wrapping_mul(0x9E3779B185EBCA87);
    value ^= ((reps as u64).wrapping_add(1)).wrapping_mul(0x165667B19E3779F9);
    value ^= value >> 33;
    value = value.wrapping_mul(0xFF51AFD7ED558CCD);
    value ^= value >> 33;
    value = value.wrapping_mul(0xC4CEB9FE1A85EC53);
    value ^= value >> 33;
    value as f64 / u64::MAX as f64
}

fn simulator_bucket(retrievability: f32) -> u32 {
    const BUCKET_COUNT: u32 = 20;
    if !retrievability.is_finite() {
        return BUCKET_COUNT - 1;
    }
    ((retrievability * BUCKET_COUNT as f32) as u32).clamp(0, BUCKET_COUNT - 1)
}

impl ReviewStateOwned {
    fn deserialize(&self) -> io::Result<SrsStateOwned> {
        Ok(SrsStateOwned {
            card: deserialize_module_state(self.card.as_deref())?,
            deck: deserialize_module_state(self.deck.as_deref())?,
            note: deserialize_module_state(self.note.as_deref())?,
            preset: deserialize_module_state(self.preset.as_deref())?,
            global: deserialize_module_state(self.global.as_deref())?,
        })
    }
}

#[derive(Clone)]
struct FeatureState {
    first_day_offset: Option<i64>,
    previous_day_offset: Option<i64>,
    card_set: HashMap<i64, ()>,
    last_new_cards: HashMap<i64, i64>,
    last_i: HashMap<i64, i64>,
    today: i64,
    today_reviews: i64,
    today_new_cards: i64,
    card_first_day_offset: HashMap<i64, i64>,
    card_elapsed_days_cumulative: HashMap<i64, i64>,
    card_elapsed_seconds_cumulative: HashMap<i64, i64>,
    /// The codes of the entities the stored reviews used, kept so that a
    /// replay computes each once. Only a memo: a code is a function of its
    /// id (`id_encoding`), so the map is not saved with the state, and a
    /// query, which must leave the state as it found it, computes a missing
    /// code without keeping it.
    id_encodings: HashMap<(IdKind, i64), Vec<f32>>,
    review_index: i64,
    /// The model's id pipeline: which note a review without one encodes.
    ids: RwkvIdPipeline,
}

impl FeatureState {
    fn new(ids: RwkvIdPipeline) -> Self {
        Self {
            first_day_offset: None,
            previous_day_offset: None,
            card_set: HashMap::new(),
            last_new_cards: HashMap::new(),
            last_i: HashMap::new(),
            today: -1,
            today_reviews: 0,
            today_new_cards: 0,
            card_first_day_offset: HashMap::new(),
            card_elapsed_days_cumulative: HashMap::new(),
            card_elapsed_seconds_cumulative: HashMap::new(),
            id_encodings: HashMap::new(),
            review_index: 0,
            ids,
        }
    }
}

#[derive(Clone)]
struct FeatureStateForCard {
    card_id: i64,
    first_day_offset: Option<i64>,
    previous_day_offset: Option<i64>,
    card_was_seen: bool,
    last_new_cards: Option<i64>,
    last_i: Option<i64>,
    today: i64,
    today_reviews: i64,
    today_new_cards: i64,
    card_first_day_offset: Option<i64>,
    card_elapsed_days_cumulative: Option<i64>,
    card_elapsed_seconds_cumulative: Option<i64>,
    review_index: i64,
}

impl FeatureState {
    fn state_for_card(&self, card_id: i64) -> FeatureStateForCard {
        FeatureStateForCard {
            card_id,
            first_day_offset: self.first_day_offset,
            previous_day_offset: self.previous_day_offset,
            card_was_seen: self.card_set.contains_key(&card_id),
            last_new_cards: self.last_new_cards.get(&card_id).copied(),
            last_i: self.last_i.get(&card_id).copied(),
            today: self.today,
            today_reviews: self.today_reviews,
            today_new_cards: self.today_new_cards,
            card_first_day_offset: self.card_first_day_offset.get(&card_id).copied(),
            card_elapsed_days_cumulative: self.card_elapsed_days_cumulative.get(&card_id).copied(),
            card_elapsed_seconds_cumulative: self
                .card_elapsed_seconds_cumulative
                .get(&card_id)
                .copied(),
            review_index: self.review_index,
        }
    }

    /// Forgets the card's own counters, as if the state had never seen it;
    /// the counters of the whole stream stay. True when the state had seen
    /// the card.
    fn forget_card(&mut self, card_id: i64) -> bool {
        let seen = self.card_set.remove(&card_id).is_some();
        self.last_new_cards.remove(&card_id);
        self.last_i.remove(&card_id);
        self.card_first_day_offset.remove(&card_id);
        self.card_elapsed_days_cumulative.remove(&card_id);
        self.card_elapsed_seconds_cumulative.remove(&card_id);
        seen
    }

    fn restore_state(&mut self, state: &FeatureStateForCard) {
        self.first_day_offset = state.first_day_offset;
        self.previous_day_offset = state.previous_day_offset;
        self.today = state.today;
        self.today_reviews = state.today_reviews;
        self.today_new_cards = state.today_new_cards;
        self.review_index = state.review_index;

        if state.card_was_seen {
            self.card_set.insert(state.card_id, ());
        } else {
            self.card_set.remove(&state.card_id);
        }
        restore_map_entry(
            &mut self.last_new_cards,
            state.card_id,
            state.last_new_cards,
        );
        restore_map_entry(&mut self.last_i, state.card_id, state.last_i);
        restore_map_entry(
            &mut self.card_first_day_offset,
            state.card_id,
            state.card_first_day_offset,
        );
        restore_map_entry(
            &mut self.card_elapsed_days_cumulative,
            state.card_id,
            state.card_elapsed_days_cumulative,
        );
        restore_map_entry(
            &mut self.card_elapsed_seconds_cumulative,
            state.card_id,
            state.card_elapsed_seconds_cumulative,
        );
    }

    fn features_for(&self, input: &ReviewInput) -> Vec<f32> {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        let elapsed_seconds = elapsed_seconds(input);
        let elapsed_days = elapsed_days(input, elapsed_seconds);
        let elapsed_days_cumulative = self
            .card_elapsed_days_cumulative
            .get(&input.card_id)
            .copied()
            .unwrap_or(0)
            + elapsed_days;
        let elapsed_seconds_cumulative = self
            .card_elapsed_seconds_cumulative
            .get(&input.card_id)
            .copied()
            .unwrap_or(0)
            + elapsed_seconds;

        let raw_day_offset = input.day_offset.unwrap_or(0);
        let day_offset = self
            .first_day_offset
            .map_or(0, |first_day_offset| raw_day_offset - first_day_offset);
        let day_offset_first = self
            .card_first_day_offset
            .get(&input.card_id)
            .copied()
            .unwrap_or(day_offset);
        let previous_day_offset = self.previous_day_offset.unwrap_or(0);
        let diff_new_cards = self
            .last_new_cards
            .get(&input.card_id)
            .map_or(0, |last_new_cards| {
                self.card_set.len() as i64 - last_new_cards
            });
        let diff_reviews = self
            .last_i
            .get(&input.card_id)
            .map_or(0, |last_i| (self.review_index - last_i - 1).max(0));

        let mut today_new_cards = self.today_new_cards;
        let mut today_reviews = self.today_reviews;
        if day_offset != self.today {
            today_new_cards = 0;
            today_reviews = -1;
        }
        today_reviews += 1;
        if !self.card_set.contains_key(&input.card_id) {
            today_new_cards += 1;
        }

        let mut features = Vec::with_capacity(CARD_FEATURES);
        features.extend([
            scale_elapsed_days(elapsed_days),
            scale_elapsed_days_cumulative(elapsed_days_cumulative),
            scale_elapsed_seconds(elapsed_seconds),
            cyclic_sin(elapsed_seconds, SECONDS_PER_DAY),
            cyclic_cos(elapsed_seconds, SECONDS_PER_DAY),
            scale_elapsed_seconds_cumulative(elapsed_seconds_cumulative),
            cyclic_sin(elapsed_seconds_cumulative, SECONDS_PER_DAY),
            cyclic_cos(elapsed_seconds_cumulative, SECONDS_PER_DAY),
            scaled_duration(input),
        ]);

        let rating = input.ease.unwrap_or(0);
        for ease in 1..=4 {
            features.push(if !input.is_query && rating == ease {
                1.0
            } else {
                0.0
            });
        }

        features.extend([
            if input.note_id.is_none() { 1.0 } else { 0.0 },
            if input.deck_id.is_none() { 1.0 } else { 0.0 },
            if input.preset_id.is_none() { 1.0 } else { 0.0 },
            scale_day_offset_diff(day_offset - previous_day_offset),
            ((day_offset.rem_euclid(7) as f32) - 3.0) / 3.0,
            scale_diff_new_cards(diff_new_cards),
            scale_diff_reviews(diff_reviews),
            scale_cum_new_cards_today(today_new_cards),
            scale_cum_reviews_today(today_reviews),
            if input.is_query {
                0.0
            } else {
                scale_state(input.card_type.unwrap_or(0))
            },
            if input.is_query { 1.0 } else { 0.0 },
        ]);

        for (kind, id) in input.encoded_ids(self.ids) {
            match self.id_encodings.get(&(kind, id)) {
                Some(encoding) => features.extend_from_slice(encoding),
                None => features.extend(id_encoding(kind, id)),
            }
        }
        append_day_offset_encoding(&mut features, day_offset, day_offset_first);
        debug_assert_eq!(features.len(), CARD_FEATURES);
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::FeaturesFor, profile_started);
        features
    }

    fn store_review(&mut self, input: &ReviewInput) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        let elapsed_seconds = elapsed_seconds(input);
        let elapsed_days = elapsed_days(input, elapsed_seconds);
        *self
            .card_elapsed_days_cumulative
            .entry(input.card_id)
            .or_default() += elapsed_days;
        *self
            .card_elapsed_seconds_cumulative
            .entry(input.card_id)
            .or_default() += elapsed_seconds;

        let raw_day_offset = input.day_offset.unwrap_or(0);
        let day_offset = self
            .first_day_offset
            .map_or(0, |first_day_offset| raw_day_offset - first_day_offset);
        if self.first_day_offset.is_none() {
            self.first_day_offset = Some(raw_day_offset);
        }

        if day_offset != self.today {
            self.today = day_offset;
            self.today_new_cards = 0;
            self.today_reviews = -1;
        }
        self.today_reviews += 1;
        if !self.card_set.contains_key(&input.card_id) {
            self.today_new_cards += 1;
            self.card_set.insert(input.card_id, ());
            self.card_first_day_offset.insert(input.card_id, day_offset);
        }

        self.previous_day_offset = Some(day_offset);
        for key in input.encoded_ids(self.ids) {
            self.id_encodings
                .entry(key)
                .or_insert_with(|| id_encoding(key.0, key.1));
        }
        self.last_i.insert(input.card_id, self.review_index);
        self.last_new_cards
            .insert(input.card_id, self.card_set.len() as i64);
        self.review_index += 1;

        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::FeaturesStore, profile_started);
    }

    #[cfg(test)]
    fn write_cache_state(&self, out: &mut Vec<u8>) {
        self.write_cache_state_to(out)
            .expect("writing to a Vec should not fail");
    }

    fn cache_state_len(&self) -> usize {
        state_cache_optional_i64_len(self.first_day_offset)
            + state_cache_optional_i64_len(self.previous_day_offset)
            + state_cache_i64_set_len(self.card_set.len())
            + state_cache_i64_map_len(self.last_new_cards.len())
            + state_cache_i64_map_len(self.last_i.len())
            + 3 * 8
            + state_cache_i64_map_len(self.card_first_day_offset.len())
            + state_cache_i64_map_len(self.card_elapsed_days_cumulative.len())
            + state_cache_i64_map_len(self.card_elapsed_seconds_cumulative.len())
            + 8
    }

    fn write_cache_state_to(&self, out: &mut impl io::Write) -> io::Result<()> {
        write_state_cache_optional_i64(out, self.first_day_offset)?;
        write_state_cache_optional_i64(out, self.previous_day_offset)?;
        write_state_cache_i64_set(out, self.card_set.keys().copied())?;
        write_state_cache_i64_map(out, &self.last_new_cards)?;
        write_state_cache_i64_map(out, &self.last_i)?;
        out.write_all(&self.today.to_le_bytes())?;
        out.write_all(&self.today_reviews.to_le_bytes())?;
        out.write_all(&self.today_new_cards.to_le_bytes())?;
        write_state_cache_i64_map(out, &self.card_first_day_offset)?;
        write_state_cache_i64_map(out, &self.card_elapsed_days_cumulative)?;
        write_state_cache_i64_map(out, &self.card_elapsed_seconds_cumulative)?;
        out.write_all(&self.review_index.to_le_bytes())
    }

    fn read_cache_state(cursor: &mut Cursor<'_>, ids: RwkvIdPipeline) -> io::Result<Self> {
        Ok(Self {
            first_day_offset: cursor.option_i64()?,
            previous_day_offset: cursor.option_i64()?,
            card_set: read_i64_set(cursor)?,
            last_new_cards: read_i64_map(cursor)?,
            last_i: read_i64_map(cursor)?,
            today: cursor.i64()?,
            today_reviews: cursor.i64()?,
            today_new_cards: cursor.i64()?,
            card_first_day_offset: read_i64_map(cursor)?,
            card_elapsed_days_cumulative: read_i64_map(cursor)?,
            card_elapsed_seconds_cumulative: read_i64_map(cursor)?,
            review_index: cursor.i64()?,
            id_encodings: HashMap::new(),
            ids,
        })
    }
}

fn restore_map_entry(map: &mut HashMap<i64, i64>, key: i64, value: Option<i64>) {
    if let Some(value) = value {
        map.insert(key, value);
    } else {
        map.remove(&key);
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
enum IdKind {
    Card,
    Note,
    Deck,
    Preset,
}

fn id_encoding_dim(kind: IdKind) -> usize {
    match kind {
        IdKind::Card | IdKind::Note => 12,
        IdKind::Deck | IdKind::Preset => 8,
    }
}

impl IdKind {
    fn cache_code(self) -> u8 {
        match self {
            IdKind::Card => 0,
            IdKind::Note => 1,
            IdKind::Deck => 2,
            IdKind::Preset => 3,
        }
    }
}

/// An entity's id code (spec sched.rwkv-id-codes): a function of the id
/// alone, so a replay, a live answer and a query give an entity the same
/// code whatever order they meet it in. It is torch's
/// `torch.randint(0, ID_SPLIT, (dim,), generator=g) - 1.5` with
/// `g = torch.Generator().manual_seed(id_code_seed(kind, id))`: the uniform
/// draw from {-1.5, -0.5, 0.5, 1.5} the model was trained with (training
/// draws a fresh code per id), seeded by the id instead of by the order the
/// ids came in.
fn id_encoding(kind: IdKind, id: i64) -> Vec<f32> {
    let dim = id_encoding_dim(kind);
    // MT19937 seeded as torch seeds it (`init_genrand`), then its first
    // `dim` outputs. Output i twists words i, i + 1 and i + MT19937_SHIFT
    // only, so the words past the last of those are never computed.
    let mut state = [0u32; MAX_ID_ENCODING_DIM + MT19937_SHIFT];
    state[0] = id_code_seed(kind, id);
    for index in 1..state.len() {
        let previous = state[index - 1];
        state[index] = 1_812_433_253u32
            .wrapping_mul(previous ^ (previous >> 30))
            .wrapping_add(index as u32);
    }
    (0..dim)
        .map(|index| {
            let twist = (state[index] & 0x8000_0000) | (state[index + 1] & 0x7fff_ffff);
            let mut value = state[index + MT19937_SHIFT] ^ (twist >> 1);
            if twist & 1 != 0 {
                value ^= 0x9908_b0df;
            }
            value ^= value >> 11;
            value ^= (value << 7) & 0x9d2c_5680;
            value ^= (value << 15) & 0xefc6_0000;
            value ^= value >> 18;
            (value as u64 % ID_SPLIT) as f32 - ((ID_SPLIT - 1) as f32 / 2.0)
        })
        .collect()
}

/// The seed of an id code: the low 32 bits of splitmix64 of the id, with
/// the kind in the top two bits so that a deck and a preset with the same
/// id get codes of their own. torch keeps only these 32 bits of a seed.
fn id_code_seed(kind: IdKind, id: i64) -> u32 {
    let mut z =
        ((id as u64) ^ (u64::from(kind.cache_code()) << 62)).wrapping_add(0x9e37_79b9_7f4a_7c15);
    z = (z ^ (z >> 30)).wrapping_mul(0xbf58_476d_1ce4_e5b9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94d0_49bb_1331_11eb);
    (z ^ (z >> 31)) as u32
}

fn append_day_offset_encoding(features: &mut Vec<f32>, day_offset: i64, first_day_offset: i64) {
    for period in DAY_OFFSET_ENCODE_PERIODS {
        let phase = 2.0 * std::f32::consts::PI / period;
        let day = day_offset.rem_euclid(period as i64) as f32;
        let first_day = first_day_offset.rem_euclid(period as i64) as f32;
        features.push((phase * day).sin());
        features.push((phase * day).cos());
        features.push((phase * first_day).sin());
        features.push((phase * first_day).cos());
    }
}

fn elapsed_seconds(input: &ReviewInput) -> i64 {
    if creation_elapsed_is_query_only(input) {
        return -1;
    }
    input
        .current_elapsed_seconds
        .or_else(|| {
            input
                .current_elapsed_days
                .map(|days| days * SECONDS_PER_DAY)
        })
        .unwrap_or(-1)
}

fn elapsed_days(input: &ReviewInput, elapsed_seconds: i64) -> i64 {
    if creation_elapsed_is_query_only(input) {
        return -1;
    }
    input.current_elapsed_days.unwrap_or({
        if elapsed_seconds >= 0 {
            elapsed_seconds / SECONDS_PER_DAY
        } else {
            -1
        }
    })
}

fn creation_elapsed_is_query_only(input: &ReviewInput) -> bool {
    !input.is_query && input.ease.is_some() && input.card_type == Some(0)
}

fn scaled_duration(input: &ReviewInput) -> f32 {
    input
        .duration_millis
        .map_or(0.0, |millis| scale_duration(millis as f32))
}

fn scale_elapsed_days(x: i64) -> f32 {
    (log_elapsed(x) - ELAPSED_DAYS_MEAN) / ELAPSED_DAYS_STD
}

fn scale_elapsed_days_cumulative(x: i64) -> f32 {
    (log_elapsed(x) - ELAPSED_DAYS_CUMULATIVE_MEAN) / ELAPSED_DAYS_CUMULATIVE_STD
}

fn scale_elapsed_seconds(x: i64) -> f32 {
    (log_elapsed(x) - ELAPSED_SECONDS_MEAN) / ELAPSED_SECONDS_STD
}

fn scale_elapsed_seconds_cumulative(x: i64) -> f32 {
    (log_elapsed(x) - ELAPSED_SECONDS_CUMULATIVE_MEAN) / ELAPSED_SECONDS_CUMULATIVE_STD
}

fn scale_duration(x: f32) -> f32 {
    ((10.0 + x).ln() - DURATION_MEAN) / DURATION_STD
}

fn scale_diff_new_cards(x: i64) -> f32 {
    ((3.0 + x as f32).ln() - DIFF_NEW_CARDS_MEAN) / DIFF_NEW_CARDS_STD
}

fn scale_diff_reviews(x: i64) -> f32 {
    ((3.0 + x as f32).ln() - DIFF_REVIEWS_MEAN) / DIFF_REVIEWS_STD
}

fn scale_cum_new_cards_today(x: i64) -> f32 {
    ((3.0 + x as f32).ln() - CUM_NEW_CARDS_TODAY_MEAN) / CUM_NEW_CARDS_TODAY_STD
}

fn scale_cum_reviews_today(x: i64) -> f32 {
    ((3.0 + x as f32).ln() - CUM_REVIEWS_TODAY_MEAN) / CUM_REVIEWS_TODAY_STD
}

fn scale_state(x: i64) -> f32 {
    x as f32 - 2.0
}

fn scale_day_offset_diff(x: i64) -> f32 {
    (std::f32::consts::E + x as f32).ln().ln()
}

fn log_elapsed(x: i64) -> f32 {
    if x == -1 {
        0.0
    } else {
        (1.0 + 1e-5 + x as f32).ln()
    }
}

fn cyclic_sin(value: i64, period: i64) -> f32 {
    ((value.rem_euclid(period) as f32) * 2.0 * std::f32::consts::PI / period as f32).sin()
}

fn cyclic_cos(value: i64, period: i64) -> f32 {
    ((value.rem_euclid(period) as f32) * 2.0 * std::f32::consts::PI / period as f32).cos()
}

struct WeightMap {
    tensors: HashMap<String, Tensor>,
}

struct Tensor {
    shape: Vec<usize>,
    values: Vec<f32>,
}

impl WeightMap {
    fn load(path: &PathBuf) -> io::Result<Self> {
        let data = fs::read(path)?;
        let mut cursor = Cursor::new(&data);
        cursor.expect_magic(b"ARWKVWEIGHTS1")?;
        let count = cursor.u32()? as usize;
        let mut tensors = HashMap::with_capacity(count);

        for _ in 0..count {
            let name_len = cursor.u16()? as usize;
            let name = cursor.string(name_len)?;
            let rank = cursor.u8()? as usize;
            let mut shape = Vec::with_capacity(rank);
            let mut len = 1_usize;
            for _ in 0..rank {
                let dim = cursor.u32()? as usize;
                len = len.checked_mul(dim).ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidData, "tensor is too large")
                })?;
                shape.push(dim);
            }
            let mut values = Vec::with_capacity(len);
            for _ in 0..len {
                values.push(cursor.f32()?);
            }
            tensors.insert(name, Tensor { shape, values });
        }

        cursor.expect_end()?;
        Ok(Self { tensors })
    }

    fn values(&self, name: &str) -> io::Result<Vec<f32>> {
        self.tensors
            .get(name)
            .map(|tensor| tensor.values.clone())
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, missing_weight(name)))
    }

    fn linear(&self, name: &str, input: usize, output: usize, bias: bool) -> io::Result<Linear> {
        let weight_name = format!("{name}.weight");
        let bias_name = format!("{name}.bias");
        let weight = &self.tensor(&weight_name, &[output, input])?.values;
        let mut weight_by_input = vec![0.0; weight.len()];
        for row in 0..output {
            for column in 0..input {
                weight_by_input[column * output + row] = weight[row * input + column];
            }
        }
        let bias = if bias {
            Some(self.tensor(&bias_name, &[output])?.values.clone())
        } else {
            None
        };
        Ok(Linear {
            input,
            output,
            weight_by_input,
            bias,
        })
    }

    fn layer_norm(&self, name: &str, dim: usize, eps: f32) -> io::Result<Norm> {
        Ok(Norm {
            groups: 1,
            dim,
            eps,
            weight: self
                .tensor(&format!("{name}.weight"), &[dim])?
                .values
                .clone(),
            bias: self.tensor(&format!("{name}.bias"), &[dim])?.values.clone(),
        })
    }

    fn group_norm(&self, name: &str, groups: usize, dim: usize, eps: f32) -> io::Result<Norm> {
        Ok(Norm {
            groups,
            dim,
            eps,
            weight: self
                .tensor(&format!("{name}.weight"), &[dim])?
                .values
                .clone(),
            bias: self.tensor(&format!("{name}.bias"), &[dim])?.values.clone(),
        })
    }

    fn tensor(&self, name: &str, shape: &[usize]) -> io::Result<&Tensor> {
        let tensor = self
            .tensors
            .get(name)
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, missing_weight(name)))?;
        if tensor.shape != shape {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "weight {name} has shape {:?}, expected {shape:?}",
                    tensor.shape
                ),
            ));
        }
        Ok(tensor)
    }
}

fn missing_weight(name: &str) -> String {
    format!("missing weight: {name}")
}

struct Cursor<'a> {
    data: &'a [u8],
    offset: usize,
}

impl<'a> Cursor<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { data, offset: 0 }
    }

    fn expect_magic(&mut self, magic: &[u8]) -> io::Result<()> {
        let found = self.bytes(magic.len())?;
        if found != magic {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "unexpected file magic",
            ));
        }
        Ok(())
    }

    fn expect_end(&self) -> io::Result<()> {
        if self.offset == self.data.len() {
            Ok(())
        } else {
            Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "trailing bytes in file",
            ))
        }
    }

    fn bytes(&mut self, len: usize) -> io::Result<&'a [u8]> {
        let end = self
            .offset
            .checked_add(len)
            .ok_or_else(|| io::Error::new(io::ErrorKind::UnexpectedEof, "offset overflow"))?;
        let bytes = self
            .data
            .get(self.offset..end)
            .ok_or_else(|| io::Error::new(io::ErrorKind::UnexpectedEof, "file ended early"))?;
        self.offset = end;
        Ok(bytes)
    }

    fn string(&mut self, len: usize) -> io::Result<String> {
        let bytes = self.bytes(len)?;
        String::from_utf8(bytes.to_vec())
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid utf-8 string"))
    }

    fn u8(&mut self) -> io::Result<u8> {
        Ok(self.bytes(1)?[0])
    }

    fn u16(&mut self) -> io::Result<u16> {
        let mut bytes = [0; 2];
        bytes.copy_from_slice(self.bytes(2)?);
        Ok(u16::from_le_bytes(bytes))
    }

    fn u32(&mut self) -> io::Result<u32> {
        let mut bytes = [0; 4];
        bytes.copy_from_slice(self.bytes(4)?);
        Ok(u32::from_le_bytes(bytes))
    }

    fn i64(&mut self) -> io::Result<i64> {
        let mut bytes = [0; 8];
        bytes.copy_from_slice(self.bytes(8)?);
        Ok(i64::from_le_bytes(bytes))
    }

    fn option_i64(&mut self) -> io::Result<Option<i64>> {
        match self.u8()? {
            0 => Ok(None),
            1 => Ok(Some(self.i64()?)),
            _ => Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid optional i64 tag",
            )),
        }
    }

    fn f32(&mut self) -> io::Result<f32> {
        let mut bytes = [0; 4];
        bytes.copy_from_slice(self.bytes(4)?);
        Ok(f32::from_le_bytes(bytes))
    }

    fn f32_vec(&mut self) -> io::Result<Vec<f32>> {
        let len = self.u32()? as usize;
        let mut values = Vec::with_capacity(len);
        for _ in 0..len {
            values.push(self.f32()?);
        }
        Ok(values)
    }

    fn skip_f32_vec(&mut self) -> io::Result<()> {
        let byte_len = (self.u32()? as usize)
            .checked_mul(std::mem::size_of::<f32>())
            .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidData, "vector too large"))?;
        self.bytes(byte_len)?;
        Ok(())
    }
}

struct SrsModel {
    /// The id pipeline the model was trained with (`RwkvIdPipeline`).
    ids: RwkvIdPipeline,
    features_0: Linear,
    features_norm: Norm,
    features_3: Linear,
    modules: Vec<RwkvModule>,
    prehead_norm: Norm,
    head_w_0: Linear,
    head_w_norm: Norm,
    head_w_4: Linear,
    w_linear: Linear,
    head_ahead_0: Linear,
    ahead_linear: Linear,
    head_p_0: Linear,
    p_linear: Linear,
}

#[cfg(test)]
struct ReviewRetrievabilityScratch {
    feature_hidden: [f32; HEAD_DIM],
    normalized_features: [f32; HEAD_DIM],
    module: ModuleQueryScratch,
    prehead: [f32; D_MODEL],
    head: [f32; HEAD_DIM],
    logits: [f32; 4],
    probabilities: [f32; 4],
}

#[derive(Default)]
struct ReviewRetrievabilityBatchScratch {
    feature_input: Vec<f32>,
    feature_hidden: Vec<f32>,
    normalized_features: Vec<f32>,
    x: Vec<f32>,
    module: ModuleQueryBatchScratch,
    prehead: Vec<f32>,
    head: Vec<f32>,
    logits: Vec<f32>,
}

#[cfg(test)]
impl Default for ReviewRetrievabilityScratch {
    fn default() -> Self {
        Self {
            feature_hidden: [0.0; HEAD_DIM],
            normalized_features: [0.0; HEAD_DIM],
            module: ModuleQueryScratch::default(),
            prehead: [0.0; D_MODEL],
            head: [0.0; HEAD_DIM],
            logits: [0.0; 4],
            probabilities: [0.0; 4],
        }
    }
}

impl SrsModel {
    fn load(path: &PathBuf) -> io::Result<Self> {
        let weights = WeightMap::load(path)?;
        let modules = MODULE_LAYERS
            .iter()
            .enumerate()
            .map(|(module_id, layer_count)| RwkvModule::load(&weights, module_id, *layer_count))
            .collect::<io::Result<Vec<_>>>()?;

        Ok(Self {
            ids: PUBLISHED_MODEL_ID_PIPELINE,
            features_0: weights.linear("features2card.0", CARD_FEATURES, HEAD_DIM, true)?,
            features_norm: weights.layer_norm("features2card.2", HEAD_DIM, 1e-5)?,
            features_3: weights.linear("features2card.3", HEAD_DIM, D_MODEL, true)?,
            modules,
            prehead_norm: weights.layer_norm("prehead_norm", D_MODEL, 1e-5)?,
            head_w_0: weights.linear("head_w.0", D_MODEL, D_MODEL, true)?,
            head_w_norm: weights.layer_norm("head_w.2", D_MODEL, 1e-5)?,
            head_w_4: weights.linear("head_w.4", D_MODEL, HEAD_DIM, true)?,
            w_linear: weights.linear("w_linear", HEAD_DIM, NUM_CURVES, true)?,
            head_ahead_0: weights.linear("head_ahead_logits.0", D_MODEL, HEAD_DIM, true)?,
            ahead_linear: weights.linear("ahead_linear", HEAD_DIM, NUM_CURVES, true)?,
            head_p_0: weights.linear("head_p.0", D_MODEL, HEAD_DIM, true)?,
            p_linear: weights.linear("p_linear", HEAD_DIM, 4, true)?,
        })
    }

    fn review(&self, features: &[f32], state: SrsStateRef<'_>) -> ReviewHeads {
        self.review_features(features, state)
    }

    fn review_many(&self, items: &[ReviewPredictionWorkItem]) -> Vec<ReviewHeads> {
        items
            .par_iter()
            .map(|item| self.review_features(&item.features, item.state.as_ref()))
            .collect()
    }

    fn review_many_borrowed(
        &self,
        items: &[ReviewPredictionBorrowedWorkItem<'_>],
    ) -> Vec<ReviewHeads> {
        items
            .par_iter()
            .map(|item| self.review_features(&item.features, item.state))
            .collect()
    }

    fn review_retrievability_many(&self, items: &[ReviewPredictionWorkItem]) -> Vec<f32> {
        let items = items
            .iter()
            .map(|item| ReviewPredictionQueryRef {
                features: &item.features,
                state: item.state.as_ref(),
            })
            .collect::<Vec<_>>();
        self.review_retrievability_query_refs(&items)
    }

    fn review_retrievability_many_borrowed(
        &self,
        items: &[ReviewPredictionBorrowedWorkItem<'_>],
    ) -> Vec<f32> {
        let items = items
            .iter()
            .map(|item| ReviewPredictionQueryRef {
                features: &item.features,
                state: item.state,
            })
            .collect::<Vec<_>>();
        self.review_retrievability_query_refs(&items)
    }

    /// Queries run in batches, so that each weight load serves many rows.
    /// Tests run the per-row path, the reference that the bit-exact tests
    /// pin; `batched_retrievability_matches_scalar_query_path` pins the
    /// batched path against it.
    fn review_retrievability_query_refs(&self, items: &[ReviewPredictionQueryRef<'_>]) -> Vec<f32> {
        #[cfg(not(test))]
        {
            self.review_retrievability_query_refs_batched(items)
        }

        #[cfg(test)]
        {
            self.review_retrievability_query_refs_scalar(items)
        }
    }

    #[cfg(test)]
    fn review_retrievability_query_refs_scalar(
        &self,
        items: &[ReviewPredictionQueryRef<'_>],
    ) -> Vec<f32> {
        items
            .par_iter()
            .map_init(
                // Keep the large per-worker scratch off Rayon's limited worker stacks.
                || Box::new(ReviewRetrievabilityScratch::default()),
                |scratch, item| {
                    self.review_retrievability_features(item.features, item.state, scratch)
                },
            )
            .collect()
    }

    fn review_retrievability_query_refs_batched(
        &self,
        items: &[ReviewPredictionQueryRef<'_>],
    ) -> Vec<f32> {
        // Off macOS, a smaller batch keeps every thread busy when there are
        // few items; every row's result is the same for any batch size.
        #[cfg(not(target_os = "macos"))]
        let batch_size = items
            .len()
            .div_ceil(rayon::current_num_threads())
            .next_multiple_of(4)
            .clamp(4, RETRIEVABILITY_GEMM_BATCH_SIZE);
        #[cfg(target_os = "macos")]
        let batch_size = RETRIEVABILITY_GEMM_BATCH_SIZE;
        let mut retrievabilities = vec![0.0; items.len()];
        retrievabilities
            .par_chunks_mut(batch_size)
            .zip(items.par_chunks(batch_size))
            .for_each_init(
                ReviewRetrievabilityBatchScratch::default,
                |scratch, (retrievabilities, items)| {
                    self.review_retrievability_query_batch(items, scratch, retrievabilities);
                },
            );
        retrievabilities
    }

    fn review_retrievability_query_batch(
        &self,
        items: &[ReviewPredictionQueryRef<'_>],
        scratch: &mut ReviewRetrievabilityBatchScratch,
        retrievabilities: &mut [f32],
    ) {
        let rows = items.len();
        scratch.feature_input.clear();
        scratch.feature_input.reserve(rows * CARD_FEATURES);
        for item in items {
            scratch.feature_input.extend_from_slice(item.features);
        }

        self.features_0
            .apply_batch(&scratch.feature_input, rows, &mut scratch.feature_hidden);
        silu_in_place(&mut scratch.feature_hidden);
        self.features_norm.apply_batch(
            &scratch.feature_hidden,
            rows,
            &mut scratch.normalized_features,
        );
        self.features_3
            .apply_batch(&scratch.normalized_features, rows, &mut scratch.x);
        silu_in_place(&mut scratch.x);

        for (module_id, module) in self.modules.iter().enumerate() {
            module.run_query_batch(&mut scratch.x, items, module_id, &mut scratch.module);
        }

        self.prehead_norm
            .apply_batch(&scratch.x, rows, &mut scratch.prehead);
        self.head_p_0
            .apply_batch(&scratch.prehead, rows, &mut scratch.head);
        relu_in_place(&mut scratch.head);
        self.p_linear
            .apply_batch(&scratch.head, rows, &mut scratch.logits);
        for (retrievability, logits) in retrievabilities
            .iter_mut()
            .zip(scratch.logits.chunks_exact(4))
        {
            let mut probabilities = [0.0; 4];
            softmax_into(logits, &mut probabilities);
            *retrievability = 1.0 - probabilities[0];
        }
    }

    /// The feature MLP shared by the per-review and bulk replay paths.
    fn feature_mlp(&self, features: &[f32]) -> Vec<f32> {
        let mut x = self.features_0.apply(features);
        silu_in_place(&mut x);
        x = self.features_norm.apply(&x);
        x = self.features_3.apply(&x);
        silu_in_place(&mut x);
        x
    }

    /// The recall-curve heads shared by the per-review and bulk replay paths.
    /// `prehead_x` must already have `prehead_norm` applied.
    fn curve_head(&self, prehead_x: &[f32]) -> ReviewCurve {
        let mut head_w = self.head_w_0.apply(prehead_x);
        relu_in_place(&mut head_w);
        head_w = self.head_w_norm.apply(&head_w);
        head_w = self.head_w_4.apply(&head_w);
        let weights = softmax(&self.w_linear.apply(&head_w));

        let mut ahead = self.head_ahead_0.apply(prehead_x);
        relu_in_place(&mut ahead);
        let ahead_logits = self.ahead_linear.apply(&ahead);

        ReviewCurve {
            ahead_logits,
            weights,
            s90: Default::default(),
        }
    }

    /// The retrievability head shared by the per-review and bulk replay
    /// paths. `prehead_x` must already have `prehead_norm` applied.
    fn button_probabilities_head(&self, prehead_x: &[f32]) -> [f32; 4] {
        let mut head_p = self.head_p_0.apply(prehead_x);
        relu_in_place(&mut head_p);
        let logits = self.p_linear.apply(&head_p);
        let probabilities = softmax(&logits);
        let mut out = [0.0; 4];
        out.copy_from_slice(&probabilities);
        out
    }

    fn retrievability_head(&self, prehead_x: &[f32]) -> f32 {
        1.0 - self.button_probabilities_head(prehead_x)[0]
    }

    fn review_features(&self, features: &[f32], state: SrsStateRef<'_>) -> ReviewHeads {
        #[cfg(test)]
        let review_profile_started = rwkv_warmup_profile_start();
        #[cfg(test)]
        let feature_mlp_profile_started = rwkv_warmup_profile_start();

        let x = self.feature_mlp(features);

        #[cfg(test)]
        rwkv_warmup_profile_record(
            RwkvWarmupProfileBucket::FeatureMlp,
            feature_mlp_profile_started,
        );

        #[cfg(test)]
        let module_profile_started = rwkv_warmup_profile_start();
        let (x, card_state) = self.modules[0].run(&x, state.card);
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::ModuleCard, module_profile_started);
        #[cfg(test)]
        let module_profile_started = rwkv_warmup_profile_start();
        let (x, deck_state) = self.modules[1].run(&x, state.deck);
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::ModuleDeck, module_profile_started);
        #[cfg(test)]
        let module_profile_started = rwkv_warmup_profile_start();
        let (x, note_state) = self.modules[2].run(&x, state.note);
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::ModuleNote, module_profile_started);
        #[cfg(test)]
        let module_profile_started = rwkv_warmup_profile_start();
        let (x, preset_state) = self.modules[3].run(&x, state.preset);
        #[cfg(test)]
        rwkv_warmup_profile_record(
            RwkvWarmupProfileBucket::ModulePreset,
            module_profile_started,
        );
        #[cfg(test)]
        let module_profile_started = rwkv_warmup_profile_start();
        let (x, global_state) = self.modules[4].run(&x, state.global);
        #[cfg(test)]
        rwkv_warmup_profile_record(
            RwkvWarmupProfileBucket::ModuleGlobal,
            module_profile_started,
        );

        #[cfg(test)]
        let heads_profile_started = rwkv_warmup_profile_start();
        let x = self.prehead_norm.apply(&x);
        let curve = self.curve_head(&x);
        let button_probabilities = self.button_probabilities_head(&x);
        let retrievability = 1.0 - button_probabilities[0];

        let next_state = SrsState {
            card: card_state,
            deck: deck_state,
            note: note_state,
            preset: preset_state,
            global: global_state,
        };

        let heads = ReviewHeads {
            retrievability,
            button_probabilities,
            curve,
            next_state,
            prehead: x,
        };
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::Heads, heads_profile_started);
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::ModelReview, review_profile_started);
        heads
    }

    #[cfg(test)]
    fn review_retrievability_features(
        &self,
        features: &[f32],
        state: SrsStateRef<'_>,
        scratch: &mut ReviewRetrievabilityScratch,
    ) -> f32 {
        self.features_0
            .apply_into(features, &mut scratch.feature_hidden);
        silu_in_place(&mut scratch.feature_hidden);
        self.features_norm
            .apply_into(&scratch.feature_hidden, &mut scratch.normalized_features);
        let mut x = [0.0; D_MODEL];
        self.features_3
            .apply_into(&scratch.normalized_features, &mut x);
        silu_in_place(&mut x);

        let states = [
            state.card,
            state.deck,
            state.note,
            state.preset,
            state.global,
        ];
        for (module, state) in self.modules.iter().zip(states) {
            x = module.run_query(&x, state, &mut scratch.module);
        }

        self.prehead_norm.apply_into(&x, &mut scratch.prehead);
        self.head_p_0
            .apply_into(&scratch.prehead, &mut scratch.head);
        relu_in_place(&mut scratch.head);
        self.p_linear.apply_into(&scratch.head, &mut scratch.logits);
        softmax_into(&scratch.logits, &mut scratch.probabilities);
        1.0 - scratch.probabilities[0]
    }
}

struct ReviewHeads {
    retrievability: f32,
    button_probabilities: [f32; 4],
    curve: ReviewCurve,
    next_state: SrsState,
    /// The heads' input, `prehead_x` after `prehead_norm`: the curve's
    /// source (`CURVE_SOURCE_FORMAT`).
    prehead: Vec<f32>,
}

#[derive(Clone, Default)]
struct ReviewCurve {
    ahead_logits: Vec<f32>,
    weights: Vec<f32>,
    /// The curve's S90 at the instance's maximum interval, found once
    /// (`RwkvInference::stored_curve_s90`); a new curve starts without it,
    /// so a replaced curve never keeps the old one's (spec
    /// ui.rwkv-curve-stored-s90).
    s90: std::sync::OnceLock<Option<f32>>,
}

impl ReviewCurve {
    #[cfg(test)]
    fn write_cache_state(&self, out: &mut Vec<u8>) {
        self.write_cache_state_to(out)
            .expect("writing to a Vec should not fail");
    }

    fn cache_state_len(&self) -> usize {
        8 + 4 * (self.ahead_logits.len() + self.weights.len())
    }

    fn write_cache_state_to(&self, out: &mut impl io::Write) -> io::Result<()> {
        write_state_cache_f32_slice(out, &self.ahead_logits)?;
        write_state_cache_f32_slice(out, &self.weights)
    }

    fn read_cache_state(cursor: &mut Cursor<'_>) -> io::Result<Self> {
        Ok(Self {
            ahead_logits: cursor.f32_vec()?,
            weights: cursor.f32_vec()?,
            s90: Default::default(),
        })
    }
}

#[derive(Clone, Copy)]
struct SrsStateRef<'a> {
    card: Option<&'a ModuleState>,
    deck: Option<&'a ModuleState>,
    note: Option<&'a ModuleState>,
    preset: Option<&'a ModuleState>,
    global: Option<&'a ModuleState>,
}

impl<'a> SrsStateRef<'a> {
    fn module(self, module_id: usize) -> Option<&'a ModuleState> {
        match module_id {
            0 => self.card,
            1 => self.deck,
            2 => self.note,
            3 => self.preset,
            4 => self.global,
            _ => None,
        }
    }
}

struct SrsStateOwned {
    card: Option<ModuleState>,
    deck: Option<ModuleState>,
    note: Option<ModuleState>,
    preset: Option<ModuleState>,
    global: Option<ModuleState>,
}

impl SrsStateOwned {
    fn as_ref(&self) -> SrsStateRef<'_> {
        SrsStateRef {
            card: self.card.as_ref(),
            deck: self.deck.as_ref(),
            note: self.note.as_ref(),
            preset: self.preset.as_ref(),
            global: self.global.as_ref(),
        }
    }
}

struct SrsState {
    card: ModuleState,
    deck: ModuleState,
    note: ModuleState,
    preset: ModuleState,
    global: ModuleState,
}

struct ReviewStateMaps {
    /// The model's id pipeline: which note stream a review without one
    /// reads.
    ids: RwkvIdPipeline,
    card: HashMap<i64, ModuleState>,
    note: HashMap<i64, ModuleState>,
    deck: HashMap<i64, ModuleState>,
    preset: HashMap<i64, ModuleState>,
    global: Option<ModuleState>,
    /// The card and note states of a restored checkpoint that are still in the
    /// store. `card` and `note` above hold only the entities this session has
    /// already touched.
    lazy: Option<LazyStateSource>,
    dirty_card: HashSet<i64>,
    dirty_note: HashSet<i64>,
    dirty_deck: HashSet<i64>,
    dirty_preset: HashSet<i64>,
    global_dirty: bool,
}

impl ReviewStateMaps {
    fn new(ids: RwkvIdPipeline) -> Self {
        Self {
            ids,
            card: HashMap::new(),
            note: HashMap::new(),
            deck: HashMap::new(),
            preset: HashMap::new(),
            global: None,
            lazy: None,
            dirty_card: HashSet::new(),
            dirty_note: HashSet::new(),
            dirty_deck: HashSet::new(),
            dirty_preset: HashSet::new(),
            global_dirty: false,
        }
    }

    fn from_serialized(
        ids: RwkvIdPipeline,
        card_states: &[(i64, Vec<u8>)],
        note_states: &[(i64, Vec<u8>)],
        deck_states: &[(i64, Vec<u8>)],
        preset_states: &[(i64, Vec<u8>)],
        global_state: Option<&[u8]>,
    ) -> io::Result<Self> {
        Ok(Self {
            ids,
            card: deserialize_state_map(card_states)?,
            note: deserialize_state_map(note_states)?,
            deck: deserialize_state_map(deck_states)?,
            preset: deserialize_state_map(preset_states)?,
            global: deserialize_module_state(global_state)?,
            lazy: None,
            dirty_card: HashSet::new(),
            dirty_note: HashSet::new(),
            dirty_deck: HashSet::new(),
            dirty_preset: HashSet::new(),
            global_dirty: false,
        })
    }

    fn state_ref(&self, input: &ReviewInput) -> SrsStateRef<'_> {
        SrsStateRef {
            card: self.card.get(&input.card_id),
            note: self.note.get(&input.note_key(self.ids)),
            deck: self.deck.get(&input.deck_key()),
            preset: self.preset.get(&input.preset_key()),
            global: self.global.as_ref(),
        }
    }

    fn state_owned(&self, input: &ReviewInput) -> SrsStateOwned {
        SrsStateOwned {
            card: self.card.get(&input.card_id).cloned(),
            note: self.note.get(&input.note_key(self.ids)).cloned(),
            deck: self.deck.get(&input.deck_key()).cloned(),
            preset: self.preset.get(&input.preset_key()).cloned(),
            global: self.global.clone(),
        }
    }

    fn serialized_state(&self, input: &ReviewInput) -> ReviewStateOwned {
        ReviewStateOwned {
            card: self.card.get(&input.card_id).map(serialize_module_state),
            note: self
                .note
                .get(&input.note_key(self.ids))
                .map(serialize_module_state),
            deck: self.deck.get(&input.deck_key()).map(serialize_module_state),
            preset: self
                .preset
                .get(&input.preset_key())
                .map(serialize_module_state),
            global: self.global.as_ref().map(serialize_module_state),
        }
    }

    fn state_owned_after_review(
        &self,
        answer: &ReviewInput,
        query: &ReviewInput,
        next_state: &SrsState,
    ) -> SrsStateOwned {
        SrsStateOwned {
            card: branched_module_state(
                &self.card,
                query.card_id,
                answer.card_id,
                &next_state.card,
            ),
            note: branched_module_state(
                &self.note,
                query.note_key(self.ids),
                answer.note_key(self.ids),
                &next_state.note,
            ),
            deck: branched_module_state(
                &self.deck,
                query.deck_key(),
                answer.deck_key(),
                &next_state.deck,
            ),
            preset: branched_module_state(
                &self.preset,
                query.preset_key(),
                answer.preset_key(),
                &next_state.preset,
            ),
            global: Some(next_state.global.clone()),
        }
    }

    fn store(&mut self, input: &ReviewInput, state: SrsState) {
        self.forget_lazy(input.card_id, input.note_key(self.ids));
        self.card.insert(input.card_id, state.card);
        self.mark_dirty(input);
        self.note.insert(input.note_key(self.ids), state.note);
        self.deck.insert(input.deck_key(), state.deck);
        self.preset.insert(input.preset_key(), state.preset);
        self.global = Some(state.global);
    }

    fn mark_dirty(&mut self, input: &ReviewInput) {
        self.dirty_card.insert(input.card_id);
        self.dirty_note.insert(input.note_key(self.ids));
        self.dirty_deck.insert(input.deck_key());
        self.dirty_preset.insert(input.preset_key());
        self.global_dirty = true;
    }

    /// Puts one review's saved states back. A missing id is the placeholder
    /// stream `ReviewInput::note_key` and its siblings give.
    fn restore_serialized(
        &mut self,
        card_id: i64,
        note_id: Option<i64>,
        deck_id: Option<i64>,
        preset_id: Option<i64>,
        state: &ReviewStateOwned,
    ) -> io::Result<()> {
        let note_id = note_id.unwrap_or_else(|| self.ids.missing_note_id(card_id));
        let deck_id = deck_id.unwrap_or(ID_PLACEHOLDER);
        let preset_id = preset_id.unwrap_or(ID_PLACEHOLDER);
        self.forget_lazy(card_id, note_id);
        restore_serialized_state(&mut self.card, card_id, state.card.as_deref())?;
        self.dirty_card.insert(card_id);
        restore_serialized_state(&mut self.note, note_id, state.note.as_deref())?;
        self.dirty_note.insert(note_id);
        restore_serialized_state(&mut self.deck, deck_id, state.deck.as_deref())?;
        self.dirty_deck.insert(deck_id);
        restore_serialized_state(&mut self.preset, preset_id, state.preset.as_deref())?;
        self.dirty_preset.insert(preset_id);
        self.global = deserialize_module_state(state.global.as_deref())?;
        self.global_dirty = true;
        Ok(())
    }

    fn mark_checkpoint_saved(&mut self) {
        self.dirty_card.clear();
        self.dirty_note.clear();
        self.dirty_deck.clear();
        self.dirty_preset.clear();
        self.global_dirty = false;
    }

    /// Writes this checkpoint's states as `entity_states` rows. A full
    /// checkpoint writes every resident entity; an incremental one writes only
    /// the entities this session changed, and the older segments of the chain
    /// keep the rest.
    fn write_checkpoint_rows(
        &self,
        transaction: &Transaction<'_>,
        segment_id: i64,
        full: bool,
    ) -> io::Result<()> {
        // A full checkpoint restates every entity, so a session that still has
        // states in the store would write a "full" segment holding only what it
        // touched, and every other card would lose its state. Refuse instead of
        // trusting the caller to have called `force_load_all` first.
        if full && self.lazy.is_some() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "a full RWKV state checkpoint needs every state resident",
            ));
        }
        let mut statement = transaction
            .prepare(
                "insert into entity_states (segment_id, kind, entity_id, state) \
                 values (?, ?, ?, ?)",
            )
            .map_err(state_cache_store_error)?;
        for (kind, states, dirty) in [
            (STATE_CACHE_KIND_CARD, &self.card, &self.dirty_card),
            (STATE_CACHE_KIND_NOTE, &self.note, &self.dirty_note),
            (STATE_CACHE_KIND_DECK, &self.deck, &self.dirty_deck),
            (STATE_CACHE_KIND_PRESET, &self.preset, &self.dirty_preset),
        ] {
            for entity_id in state_cache_delta_identities(states, dirty, full) {
                let state = states.get(&entity_id).map(serialize_module_state);
                statement
                    .execute(params![segment_id, kind, entity_id, state])
                    .map_err(state_cache_store_error)?;
            }
        }
        if full || self.global_dirty {
            let state = self.global.as_ref().map(serialize_module_state);
            statement
                .execute(params![
                    segment_id,
                    STATE_CACHE_KIND_GLOBAL,
                    STATE_CACHE_GLOBAL_ENTITY_ID,
                    state
                ])
                .map_err(state_cache_store_error)?;
        }
        Ok(())
    }

    fn resident(&self, kind: i64, entity_id: i64) -> bool {
        match kind {
            STATE_CACHE_KIND_CARD => self.card.contains_key(&entity_id),
            _ => self.note.contains_key(&entity_id),
        }
    }

    /// Makes one card or note state resident.
    ///
    /// A key that the index does not hold had no state in the restored chain,
    /// which is the "no cached state" a brand-new card has. A row that cannot
    /// be read, or whose own key does not match the key asked for, is an
    /// error: a partial or foreign state must never reach the model.
    fn load_lazy(&mut self, kind: i64, entity_id: i64) -> io::Result<()> {
        if self.resident(kind, entity_id) {
            return Ok(());
        }
        let Some(row_id) = self
            .lazy
            .as_ref()
            .and_then(|source| source.row_id(kind, entity_id))
        else {
            return Ok(());
        };
        let Some(source) = self.lazy.as_mut() else {
            return Ok(());
        };
        let state = source.read(kind, entity_id, row_id)?;
        source.forget(kind, entity_id);
        if let Some(state) = state {
            match kind {
                STATE_CACHE_KIND_CARD => self.card.insert(entity_id, state),
                _ => self.note.insert(entity_id, state),
            };
        }
        Ok(())
    }

    /// Makes every state one review input reads resident. Deck, preset and
    /// global are read at restore time and are always resident already.
    fn ensure_loaded(&mut self, input: &ReviewInput) -> io::Result<()> {
        if self.lazy.is_none() {
            return Ok(());
        }
        self.load_lazy(STATE_CACHE_KIND_CARD, input.card_id)?;
        self.load_lazy(STATE_CACHE_KIND_NOTE, input.note_key(self.ids))
    }

    fn ensure_loaded_many(&mut self, inputs: &[ReviewInput]) -> io::Result<()> {
        if self.lazy.is_none() {
            return Ok(());
        }
        for input in inputs {
            self.ensure_loaded(input)?;
        }
        Ok(())
    }

    /// Makes every card and note state of the restored chain resident. The
    /// paths that write or copy a full snapshot need the whole map, so they
    /// pay the read that a study session avoids.
    fn force_load_all(&mut self) -> io::Result<()> {
        let Some(source) = self.lazy.as_ref() else {
            return Ok(());
        };
        for (kind, entity_id) in source.pending_keys() {
            self.load_lazy(kind, entity_id)?;
        }
        self.lazy = None;
        Ok(())
    }

    /// Drops the index entries for one input, so a state this session wrote
    /// can never be replaced by the stored one.
    fn forget_lazy(&mut self, card_id: i64, note_id: i64) {
        let Some(source) = self.lazy.as_mut() else {
            return;
        };
        source.forget(STATE_CACHE_KIND_CARD, card_id);
        source.forget(STATE_CACHE_KIND_NOTE, note_id);
    }

    /// Closes the store handle the lazy reads use, so the app can replace or
    /// remove the store file. The next lazy read opens it again.
    fn close_state_source(&mut self) {
        if let Some(source) = self.lazy.as_ref() {
            source.close();
        }
    }
}

/// The card and note states of a restored checkpoint chain that are still in
/// the store, indexed by key.
///
/// Building this reads the store's key index only - about 1 MB where the
/// states are 3.5 GB - and a study session then reads back the few hundred
/// entities it actually reviews.
struct LazyStateSource {
    path: PathBuf,
    store_generation: String,
    /// Opened on the first lazy read and dropped whenever the app is about to
    /// touch the store file. The next read opens it again.
    connection: Mutex<Option<Connection>>,
    card: HashMap<i64, i64>,
    note: HashMap<i64, i64>,
}

impl LazyStateSource {
    fn row_id(&self, kind: i64, entity_id: i64) -> Option<i64> {
        match kind {
            STATE_CACHE_KIND_CARD => self.card.get(&entity_id).copied(),
            _ => self.note.get(&entity_id).copied(),
        }
    }

    fn forget(&mut self, kind: i64, entity_id: i64) {
        match kind {
            STATE_CACHE_KIND_CARD => self.card.remove(&entity_id),
            _ => self.note.remove(&entity_id),
        };
    }

    fn pending_keys(&self) -> Vec<(i64, i64)> {
        self.card
            .keys()
            .map(|entity_id| (STATE_CACHE_KIND_CARD, *entity_id))
            .chain(
                self.note
                    .keys()
                    .map(|entity_id| (STATE_CACHE_KIND_NOTE, *entity_id)),
            )
            .collect()
    }

    fn read(&self, kind: i64, entity_id: i64, row_id: i64) -> io::Result<Option<ModuleState>> {
        let mut guard = self
            .connection
            .lock()
            .map_err(|_| io::Error::other("RWKV state-cache reader lock poisoned"))?;
        if guard.is_none() {
            let connection = Connection::open_with_flags(
                &self.path,
                OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
            )
            .map_err(state_cache_store_error)?;
            validate_state_cache_store(&connection, &self.store_generation)?;
            *guard = Some(connection);
        }
        let connection = guard
            .as_ref()
            .ok_or_else(|| io::Error::other("missing RWKV state-cache reader"))?;
        read_state_cache_entity(connection, kind, entity_id, row_id)
    }

    fn close(&self) {
        if let Ok(mut guard) = self.connection.lock() {
            *guard = None;
        }
    }
}

/// Reads one entity's complete state.
///
/// The row carries its own key and the store carries its generation, and both
/// are checked, so a store that was replaced under us fails loudly instead of
/// returning some other entity's state. The state body itself is length- and
/// magic-checked by `deserialize_module_state`, so a short read is an error
/// rather than a partial state.
fn read_state_cache_entity(
    connection: &Connection,
    kind: i64,
    entity_id: i64,
    row_id: i64,
) -> io::Result<Option<ModuleState>> {
    let row = connection
        .query_row(
            "select kind, entity_id, state from entity_states where id = ?",
            [row_id],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, Option<Vec<u8>>>(2)?,
                ))
            },
        )
        .optional()
        .map_err(state_cache_store_error)?;
    let Some((row_kind, row_entity_id, state)) = row else {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "missing RWKV state-cache entity row",
        ));
    };
    if row_kind != kind || row_entity_id != entity_id {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "RWKV state-cache entity row key mismatch",
        ));
    }
    deserialize_module_state(state.as_deref())
}

/// The newest row of each (kind, entity) in a restored segment chain.
struct StateCacheIndex {
    card: HashMap<i64, i64>,
    note: HashMap<i64, i64>,
    deck: HashMap<i64, i64>,
    preset: HashMap<i64, i64>,
    global: Option<i64>,
}

/// Indexes a restored segment chain by key, reading no states.
///
/// A state is replaced at every review and never accumulated, so every row is
/// a full snapshot of one entity and the newest row for a key is the whole
/// answer. The chain is ordered newest first, so the first row seen wins.
fn build_state_cache_index(
    connection: &Connection,
    segment_chain: &[i64],
) -> io::Result<StateCacheIndex> {
    let mut index = StateCacheIndex {
        card: HashMap::new(),
        note: HashMap::new(),
        deck: HashMap::new(),
        preset: HashMap::new(),
        global: None,
    };
    let mut seen_global = false;
    let mut statement = connection
        .prepare("select kind, entity_id, id from entity_states where segment_id = ?")
        .map_err(state_cache_store_error)?;
    for segment_id in segment_chain {
        let rows = statement
            .query_map([segment_id], |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, i64>(2)?,
                ))
            })
            .map_err(state_cache_store_error)?;
        for row in rows {
            let (kind, entity_id, row_id) = row.map_err(state_cache_store_error)?;
            match kind {
                STATE_CACHE_KIND_CARD => {
                    index.card.entry(entity_id).or_insert(row_id);
                }
                STATE_CACHE_KIND_NOTE => {
                    index.note.entry(entity_id).or_insert(row_id);
                }
                STATE_CACHE_KIND_DECK => {
                    index.deck.entry(entity_id).or_insert(row_id);
                }
                STATE_CACHE_KIND_PRESET => {
                    index.preset.entry(entity_id).or_insert(row_id);
                }
                STATE_CACHE_KIND_GLOBAL => {
                    if !seen_global {
                        seen_global = true;
                        index.global = Some(row_id);
                    }
                }
                _ => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        "unknown RWKV state-cache entity kind",
                    ));
                }
            }
        }
    }
    Ok(index)
}

fn state_cache_delta_identities(
    states: &HashMap<i64, ModuleState>,
    dirty: &HashSet<i64>,
    full: bool,
) -> Vec<i64> {
    let mut identities = if full {
        states.keys().copied().collect::<Vec<_>>()
    } else {
        dirty.iter().copied().collect::<Vec<_>>()
    };
    identities.sort_unstable();
    identities
}

fn restore_serialized_state(
    states: &mut HashMap<i64, ModuleState>,
    id: i64,
    state: Option<&[u8]>,
) -> io::Result<()> {
    if let Some(state) = deserialize_module_state(state)? {
        states.insert(id, state);
    } else {
        states.remove(&id);
    }
    Ok(())
}

/// The state a query reads right after `answer_id`'s review: the new state
/// where the query shares the answer's stream, else the stored one.
fn branched_module_state(
    map: &HashMap<i64, ModuleState>,
    query_id: i64,
    answer_id: i64,
    next_state: &ModuleState,
) -> Option<ModuleState> {
    if query_id == answer_id {
        Some(next_state.clone())
    } else {
        map.get(&query_id).cloned()
    }
}

fn deserialize_state_map(states: &[(i64, Vec<u8>)]) -> io::Result<HashMap<i64, ModuleState>> {
    let mut map = HashMap::with_capacity(states.len());
    for (key, state) in states {
        let Some(state) = deserialize_module_state(Some(state.as_slice()))? else {
            continue;
        };
        map.insert(*key, state);
    }
    Ok(map)
}

fn serialize_state_map(states: &HashMap<i64, ModuleState>) -> Vec<(i64, Vec<u8>)> {
    let mut states: Vec<_> = states
        .iter()
        .map(|(key, state)| (*key, serialize_module_state(state)))
        .collect();
    states.sort_by_key(|(key, _)| *key);
    states
}

fn serialize_module_state(state: &ModuleState) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(b"ARWKVMODSTATE1");
    write_u32(&mut out, state.layers.len() as u32);
    for layer in &state.layers {
        write_f32_slice(
            &mut out,
            layer
                .time
                .as_ref()
                .map_or(&[][..], |time| time.x_shift.as_slice()),
        );
        write_f32_slice(
            &mut out,
            layer
                .time
                .as_ref()
                .map_or(&[][..], |time| time.matrix.as_slice()),
        );
        write_f32_slice(
            &mut out,
            layer
                .channel_shift
                .as_ref()
                .map_or(&[][..], |shift| shift.as_slice()),
        );
    }
    out
}

fn serialized_module_state_len(state: &ModuleState) -> usize {
    b"ARWKVMODSTATE1".len()
        + 4
        + state
            .layers
            .iter()
            .map(|layer| {
                12 + 4
                    * (layer.time.as_ref().map_or(0, |time| time.x_shift.len())
                        + layer.time.as_ref().map_or(0, |time| time.matrix.len())
                        + layer.channel_shift.as_ref().map_or(0, |shift| shift.len()))
            })
            .sum::<usize>()
}

fn write_serialized_module_state(out: &mut impl io::Write, state: &ModuleState) -> io::Result<()> {
    out.write_all(b"ARWKVMODSTATE1")?;
    write_snapshot_u32(out, state.layers.len())?;
    for layer in &state.layers {
        write_state_cache_f32_slice(
            out,
            layer
                .time
                .as_ref()
                .map_or(&[][..], |time| time.x_shift.as_slice()),
        )?;
        write_state_cache_f32_slice(
            out,
            layer
                .time
                .as_ref()
                .map_or(&[][..], |time| time.matrix.as_slice()),
        )?;
        write_state_cache_f32_slice(
            out,
            layer
                .channel_shift
                .as_ref()
                .map_or(&[][..], |shift| shift.as_slice()),
        )?;
    }
    Ok(())
}

fn write_snapshot_module_state(out: &mut impl io::Write, state: &ModuleState) -> io::Result<()> {
    write_snapshot_u32(out, serialized_module_state_len(state))?;
    write_serialized_module_state(out, state)
}

fn write_snapshot_state_map(
    out: &mut impl io::Write,
    states: &HashMap<i64, ModuleState>,
) -> io::Result<()> {
    write_snapshot_u32(out, states.len())?;
    let mut states = states.iter().collect::<Vec<_>>();
    states.sort_unstable_by_key(|(identity, _)| **identity);
    for (identity, state) in states {
        out.write_all(&identity.to_le_bytes())?;
        write_snapshot_module_state(out, state)?;
    }
    Ok(())
}

fn write_snapshot_optional_bytes(out: &mut impl io::Write, value: Option<&[u8]>) -> io::Result<()> {
    match value {
        Some(value) => {
            out.write_all(&[1])?;
            write_snapshot_bytes(out, value)
        }
        None => out.write_all(&[0]),
    }
}

fn write_snapshot_bytes(out: &mut impl io::Write, value: &[u8]) -> io::Result<()> {
    write_snapshot_u32(out, value.len())?;
    out.write_all(value)
}

fn write_snapshot_u32(out: &mut impl io::Write, value: usize) -> io::Result<()> {
    let value = u32::try_from(value)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "RWKV state is too large"))?;
    out.write_all(&value.to_le_bytes())
}

fn open_state_cache_store(
    path: &PathBuf,
    store_generation: &str,
    durable: bool,
) -> io::Result<Connection> {
    let connection = Connection::open(path).map_err(state_cache_store_error)?;
    if !durable {
        connection
            .execute_batch(
                "pragma journal_mode = off;
                 pragma synchronous = off;
                 pragma locking_mode = exclusive;",
            )
            .map_err(state_cache_store_error)?;
    } else {
        connection
            .execute_batch(
                "pragma journal_mode = delete;
                 pragma synchronous = full;",
            )
            .map_err(state_cache_store_error)?;
    }
    let schema_version = connection
        .pragma_query_value(None, "user_version", |row| row.get::<_, i64>(0))
        .map_err(state_cache_store_error)?;
    if schema_version == 0 {
        connection
            .pragma_update(None, "page_size", STATE_CACHE_STORE_PAGE_SIZE)
            .map_err(state_cache_store_error)?;
        connection
            .execute_batch(STATE_CACHE_ENTITY_STATES_DDL)
            .map_err(state_cache_store_error)?;
        connection
            .execute_batch(
                r#"
create table store_metadata (
  key text primary key,
  value text not null
) without rowid;
create table segments (
  id integer primary key,
  parent_id integer references segments(id),
  last_review_id integer not null,
  review_count integer not null,
  history_hash text not null,
  replay_key text not null,
  previous_review_ids blob not null,
  previous_intervals blob not null,
  review_counts blob not null,
  runtime_state blob not null
);
"#,
            )
            .map_err(state_cache_store_error)?;
        connection
            .execute(
                "insert into store_metadata (key, value) values ('generation', ?)",
                [store_generation],
            )
            .map_err(state_cache_store_error)?;
        connection
            .pragma_update(None, "user_version", STATE_CACHE_STORE_SCHEMA_VERSION)
            .map_err(state_cache_store_error)?;
    }
    validate_state_cache_store(&connection, store_generation)?;
    Ok(connection)
}

fn validate_state_cache_store(connection: &Connection, store_generation: &str) -> io::Result<()> {
    let schema_version = connection
        .pragma_query_value(None, "user_version", |row| row.get::<_, i64>(0))
        .map_err(state_cache_store_error)?;
    if schema_version != STATE_CACHE_STORE_SCHEMA_VERSION {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("unsupported RWKV state-cache store schema {schema_version}"),
        ));
    }
    let actual_generation = connection
        .query_row(
            "select value from store_metadata where key = 'generation'",
            [],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(state_cache_store_error)?;
    if actual_generation.as_deref() != Some(store_generation) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "RWKV state-cache store generation mismatch",
        ));
    }
    Ok(())
}

fn validate_state_cache_parent(
    transaction: &Transaction<'_>,
    parent_segment_id: Option<i64>,
) -> io::Result<()> {
    let Some(parent_segment_id) = parent_segment_id else {
        return Ok(());
    };
    let parent_exists = transaction
        .query_row(
            "select 1 from segments where id = ?",
            [parent_segment_id],
            |_| Ok(()),
        )
        .optional()
        .map_err(state_cache_store_error)?
        .is_some();
    if !parent_exists {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "missing RWKV state-cache parent segment",
        ));
    }
    Ok(())
}

/// The chunk rows of one schema-4 segment, in the attached store the upgrade
/// reads from.
fn old_state_cache_delta_chunk_ids(
    connection: &Connection,
    segment_id: i64,
) -> io::Result<Vec<i64>> {
    let mut statement = connection
        .prepare(
            r#"
select id, chunk_index
from old_store.segment_state_chunks
where segment_id = ?
order by chunk_index
"#,
        )
        .map_err(state_cache_store_error)?;
    let rows = statement
        .query_map([segment_id], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?))
        })
        .map_err(state_cache_store_error)?;
    let mut chunk_ids = Vec::new();
    for (expected_index, row) in rows.enumerate() {
        let (chunk_id, chunk_index) = row.map_err(state_cache_store_error)?;
        if chunk_index != expected_index as i64 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid RWKV state delta chunk sequence",
            ));
        }
        chunk_ids.push(chunk_id);
    }
    if chunk_ids.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "missing RWKV state delta chunks",
        ));
    }
    Ok(chunk_ids)
}

struct StateCacheDeltaChunkReader<'conn> {
    blob: Blob<'conn>,
    remaining_chunk_ids: std::vec::IntoIter<i64>,
}

impl<'conn> StateCacheDeltaChunkReader<'conn> {
    fn new(connection: &'conn Connection, chunk_ids: Vec<i64>) -> io::Result<Self> {
        let mut remaining_chunk_ids = chunk_ids.into_iter();
        let first_chunk_id = remaining_chunk_ids.next().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidData, "missing RWKV state delta chunk")
        })?;
        let blob = connection
            .blob_open(
                STATE_CACHE_OLD_STORE_NAME,
                "segment_state_chunks",
                "state_delta",
                first_chunk_id,
                true,
            )
            .map_err(state_cache_store_error)?;
        Ok(Self {
            blob,
            remaining_chunk_ids,
        })
    }
}

impl io::Read for StateCacheDeltaChunkReader<'_> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let mut read = 0;
        while read < buf.len() {
            let chunk_read = io::Read::read(&mut self.blob, &mut buf[read..])?;
            if chunk_read == 0 {
                let Some(next_chunk_id) = self.remaining_chunk_ids.next() else {
                    break;
                };
                self.blob
                    .reopen(next_chunk_id)
                    .map_err(state_cache_store_error)?;
            } else {
                read += chunk_read;
            }
        }
        Ok(read)
    }
}

fn state_cache_segment_chain(
    connection: &Connection,
    target_segment_id: i64,
) -> io::Result<Vec<i64>> {
    let mut chain = Vec::new();
    let mut seen = HashSet::new();
    let mut current = Some(target_segment_id);
    while let Some(segment_id) = current {
        if !seen.insert(segment_id) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "cyclic RWKV state-cache segment chain",
            ));
        }
        let parent = connection
            .query_row(
                "select parent_id from segments where id = ?",
                [segment_id],
                |row| row.get::<_, Option<i64>>(0),
            )
            .optional()
            .map_err(state_cache_store_error)?
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "missing RWKV state-cache segment",
                )
            })?;
        chain.push(segment_id);
        current = parent;
    }
    Ok(chain)
}

/// The name of the half-built store the upgrade writes beside the old one.
const STATE_CACHE_UPGRADE_SUFFIX: &str = "upgrade";
/// The name the upgrade attaches the schema-4 store under.
const STATE_CACHE_OLD_STORE_NAME: &std::ffi::CStr = c"old_store";

/// Rewrites a schema-4 store, which kept one serialized delta stream per
/// segment, as one `entity_states` row per (segment, kind, entity).
///
/// Every entry in a stream was already a full snapshot of one entity - an RNN
/// state is replaced at each review, never accumulated - so the conversion is
/// a straight copy and no review is replayed.
///
/// The new store is built in a file beside the old one and is put in its place
/// only after every row is written and counted. An interruption therefore
/// costs a second run and never the cache: the schema-4 store is untouched
/// until the rename, and a half-built file left behind is removed by the next
/// run.
fn migrate_state_cache_store(path: &Path) -> io::Result<()> {
    {
        let connection = Connection::open_with_flags(
            path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(state_cache_store_error)?;
        let schema_version = connection
            .pragma_query_value(None, "user_version", |row| row.get::<_, i64>(0))
            .map_err(state_cache_store_error)?;
        connection
            .close()
            .map_err(|(_, error)| state_cache_store_error(error))?;
        if schema_version != STATE_CACHE_STORE_CHUNKED_SCHEMA_VERSION {
            return Ok(());
        }
    }
    let upgrade_path = state_cache_upgrade_path(path);
    if upgrade_path.exists() {
        std::fs::remove_file(&upgrade_path)?;
    }
    check_state_cache_upgrade_space(path)?;
    let result = build_upgraded_state_cache_store(path, &upgrade_path);
    if result.is_err() {
        let _ = std::fs::remove_file(&upgrade_path);
        return result;
    }
    std::fs::OpenOptions::new()
        .write(true)
        .open(&upgrade_path)?
        .sync_all()?;
    std::fs::rename(&upgrade_path, path)?;
    Ok(())
}

/// Refuses the upgrade when the disk cannot hold the store twice.
///
/// The new store is built beside the old one and holds the same states, so the
/// upgrade needs about the size of the store again, plus room for the rows'
/// own overhead. Running out of space in the middle costs nothing but the
/// time - the old store is untouched until the rename - so this check exists
/// to say why, not to keep the cache safe.
fn check_state_cache_upgrade_space(path: &Path) -> io::Result<()> {
    let store_bytes = std::fs::metadata(path)?.len();
    let needed = store_bytes + store_bytes / 20;
    let directory = path.parent().unwrap_or(Path::new("."));
    let Some(available) = available_disk_space(directory) else {
        return Ok(());
    };
    if available >= needed {
        return Ok(());
    }
    Err(io::Error::other(format!(
        "the RWKV state cache cannot be upgraded: it needs {needed} bytes of \
         free space in {} and {available} bytes are free",
        directory.display()
    )))
}

/// Free space a normal user can still use in `directory`, or `None` when the
/// platform did not answer.
#[cfg(windows)]
fn available_disk_space(directory: &Path) -> Option<u64> {
    use std::os::windows::ffi::OsStrExt;

    // Declared here rather than through a new crate feature: one call, one
    // signature, and no other part of rslib asks about free space.
    unsafe extern "system" {
        fn GetDiskFreeSpaceExW(
            directory_name: *const u16,
            free_bytes_available_to_caller: *mut u64,
            total_bytes: *mut u64,
            total_free_bytes: *mut u64,
        ) -> i32;
    }

    let mut wide: Vec<u16> = directory.as_os_str().encode_wide().collect();
    wide.push(0);
    let mut available = 0_u64;
    let ok = unsafe {
        GetDiskFreeSpaceExW(
            wide.as_ptr(),
            &mut available,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    (ok != 0).then_some(available)
}

#[cfg(unix)]
fn available_disk_space(directory: &Path) -> Option<u64> {
    use std::os::unix::ffi::OsStrExt;

    let mut path = directory.as_os_str().as_bytes().to_vec();
    path.push(0);
    let mut stats: libc::statvfs = unsafe { std::mem::zeroed() };
    let ok = unsafe { libc::statvfs(path.as_ptr().cast::<libc::c_char>(), &mut stats) };
    if ok != 0 {
        return None;
    }
    Some((stats.f_bavail as u64).saturating_mul(stats.f_frsize as u64))
}

/// No answer on a platform that is neither Windows nor unix, so the upgrade
/// runs and fails on a full disk instead of refusing first.
#[cfg(not(any(windows, unix)))]
fn available_disk_space(_directory: &Path) -> Option<u64> {
    None
}

fn state_cache_upgrade_path(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".");
    name.push(STATE_CACHE_UPGRADE_SUFFIX);
    path.with_file_name(name)
}

/// Writes the whole schema-5 store to `upgrade_path`, reading the schema-4
/// store at `path` through an attached database.
fn build_upgraded_state_cache_store(path: &Path, upgrade_path: &Path) -> io::Result<()> {
    let mut connection = Connection::open(upgrade_path).map_err(state_cache_store_error)?;
    connection
        .pragma_update(None, "page_size", STATE_CACHE_STORE_PAGE_SIZE)
        .map_err(state_cache_store_error)?;
    // The file is thrown away unless it is complete, so its own durability
    // does not matter; only the rename at the end has to be ordered.
    connection
        .execute_batch(
            "pragma journal_mode = off;
             pragma synchronous = off;",
        )
        .map_err(state_cache_store_error)?;
    connection
        .execute_batch(STATE_CACHE_ENTITY_STATES_DDL)
        .map_err(state_cache_store_error)?;
    connection
        .execute_batch(
            r#"
create table store_metadata (
  key text primary key,
  value text not null
) without rowid;
create table segments (
  id integer primary key,
  parent_id integer references segments(id),
  last_review_id integer not null,
  review_count integer not null,
  history_hash text not null,
  replay_key text not null,
  previous_review_ids blob not null,
  previous_intervals blob not null,
  review_counts blob not null,
  runtime_state blob not null
);
"#,
        )
        .map_err(state_cache_store_error)?;
    let old_path = path
        .to_str()
        .ok_or_else(|| io::Error::other("RWKV state-cache path is not valid unicode"))?;
    connection
        .execute("attach database ? as old_store", [old_path])
        .map_err(state_cache_store_error)?;
    let expected = (|| -> io::Result<(i64, i64)> {
        let transaction = connection.transaction().map_err(state_cache_store_error)?;
        transaction
            .execute_batch(
                "insert into store_metadata select * from old_store.store_metadata;
                 insert into segments select * from old_store.segments;",
            )
            .map_err(state_cache_store_error)?;
        let segment_ids = {
            let mut statement = transaction
                .prepare("select id from old_store.segments order by id")
                .map_err(state_cache_store_error)?;
            let rows = statement
                .query_map([], |row| row.get::<_, i64>(0))
                .map_err(state_cache_store_error)?;
            rows.collect::<rusqlite::Result<Vec<i64>>>()
                .map_err(state_cache_store_error)?
        };
        let mut rows = 0_i64;
        let mut state_bytes = 0_i64;
        for segment_id in segment_ids {
            let (segment_rows, segment_bytes) =
                migrate_state_cache_segment(&transaction, segment_id)?;
            rows += segment_rows;
            state_bytes += segment_bytes;
        }
        transaction.commit().map_err(state_cache_store_error)?;
        Ok((rows, state_bytes))
    })()?;
    connection
        .execute_batch("detach database old_store")
        .map_err(state_cache_store_error)?;
    // `length()` of a blob column reads the row header, not the blob, so this
    // check costs a scan of the table's own pages and not of the states.
    let written = connection
        .query_row(
            "select count(*), coalesce(sum(length(state)), 0) from entity_states",
            [],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?)),
        )
        .map_err(state_cache_store_error)?;
    if written != expected {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "the upgraded RWKV state-cache store is incomplete: \
                 wrote {} rows of {} bytes, expected {} rows of {} bytes",
                written.0, written.1, expected.0, expected.1
            ),
        ));
    }
    connection
        .pragma_update(None, "user_version", STATE_CACHE_STORE_SCHEMA_VERSION)
        .map_err(state_cache_store_error)?;
    connection
        .close()
        .map_err(|(_, error)| state_cache_store_error(error))
}

/// Converts one schema-4 segment's delta stream into `entity_states` rows.
///
/// Returns the number of rows written and the number of state bytes in them,
/// so that the caller can check the finished store against the source.
fn migrate_state_cache_segment(
    transaction: &Transaction<'_>,
    segment_id: i64,
) -> io::Result<(i64, i64)> {
    let mut rows = 0_i64;
    let mut state_bytes = 0_i64;
    let chunk_ids = old_state_cache_delta_chunk_ids(transaction, segment_id)?;
    let mut statement = transaction
        .prepare(
            "insert into entity_states (segment_id, kind, entity_id, state) \
             values (?, ?, ?, ?)",
        )
        .map_err(state_cache_store_error)?;
    let mut state_delta = StateCacheDeltaChunkReader::new(transaction, chunk_ids)?;
    let mut magic = vec![0; STATE_CACHE_DELTA_MAGIC.len()];
    io::Read::read_exact(&mut state_delta, &mut magic)?;
    if magic != STATE_CACHE_DELTA_MAGIC {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid RWKV state-cache delta header",
        ));
    }
    for kind in [
        STATE_CACHE_KIND_CARD,
        STATE_CACHE_KIND_NOTE,
        STATE_CACHE_KIND_DECK,
        STATE_CACHE_KIND_PRESET,
    ] {
        for _ in 0..read_state_cache_u32(&mut state_delta)? {
            let entity_id = read_state_cache_i64(&mut state_delta)?;
            let state = read_state_cache_delta_state(&mut state_delta)?;
            rows += 1;
            state_bytes += state.as_ref().map_or(0, |state| state.len() as i64);
            statement
                .execute(params![segment_id, kind, entity_id, state])
                .map_err(state_cache_store_error)?;
        }
    }
    match read_state_cache_u8(&mut state_delta)? {
        0 => {}
        1 => {
            rows += 1;
            statement
                .execute(params![
                    segment_id,
                    STATE_CACHE_KIND_GLOBAL,
                    STATE_CACHE_GLOBAL_ENTITY_ID,
                    None::<Vec<u8>>
                ])
                .map_err(state_cache_store_error)?;
        }
        2 => {
            let size = read_state_cache_u32(&mut state_delta)? as usize;
            let mut state = vec![0; size];
            io::Read::read_exact(&mut state_delta, &mut state)?;
            rows += 1;
            state_bytes += size as i64;
            statement
                .execute(params![
                    segment_id,
                    STATE_CACHE_KIND_GLOBAL,
                    STATE_CACHE_GLOBAL_ENTITY_ID,
                    Some(state)
                ])
                .map_err(state_cache_store_error)?;
        }
        _ => return Err(invalid_state_cache_delta_marker()),
    }
    let mut trailing = [0];
    if io::Read::read(&mut state_delta, &mut trailing)? != 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "trailing RWKV state-cache delta data",
        ));
    }
    Ok((rows, state_bytes))
}

fn read_state_cache_delta_state(input: &mut impl io::Read) -> io::Result<Option<Vec<u8>>> {
    match read_state_cache_u8(input)? {
        0 => Ok(None),
        1 => {
            let size = read_state_cache_u32(input)? as usize;
            let mut state = vec![0; size];
            io::Read::read_exact(input, &mut state)?;
            Ok(Some(state))
        }
        _ => Err(invalid_state_cache_delta_marker()),
    }
}

fn read_state_cache_u8(input: &mut impl io::Read) -> io::Result<u8> {
    let mut value = [0];
    io::Read::read_exact(input, &mut value)?;
    Ok(value[0])
}

fn read_state_cache_u32(input: &mut impl io::Read) -> io::Result<u32> {
    let mut value = [0; 4];
    io::Read::read_exact(input, &mut value)?;
    Ok(u32::from_le_bytes(value))
}

fn read_state_cache_i64(input: &mut impl io::Read) -> io::Result<i64> {
    let mut value = [0; 8];
    io::Read::read_exact(input, &mut value)?;
    Ok(i64::from_le_bytes(value))
}

fn invalid_state_cache_delta_marker() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "invalid RWKV state-cache delta marker",
    )
}

fn state_cache_store_error(error: rusqlite::Error) -> io::Error {
    io::Error::other(error)
}

fn deserialize_module_state(bytes: Option<&[u8]>) -> io::Result<Option<ModuleState>> {
    let Some(bytes) = bytes else {
        return Ok(None);
    };
    let mut cursor = Cursor::new(bytes);
    cursor.expect_magic(b"ARWKVMODSTATE1")?;
    let layer_count = cursor.u32()? as usize;
    let mut layers = Vec::with_capacity(layer_count);
    for _ in 0..layer_count {
        let x_shift = cursor.f32_vec()?;
        let matrix = cursor.f32_vec()?;
        let channel_shift = cursor.f32_vec()?;
        layers.push(LayerState {
            time: Some(TimeState { x_shift, matrix }),
            channel_shift: Some(channel_shift),
        });
    }
    cursor.expect_end()?;
    Ok(Some(ModuleState { layers }))
}

fn write_u32(out: &mut Vec<u8>, value: u32) {
    out.extend_from_slice(&value.to_le_bytes());
}

fn state_cache_optional_i64_len(value: Option<i64>) -> usize {
    if value.is_some() {
        9
    } else {
        1
    }
}

fn state_cache_i64_set_len(count: usize) -> usize {
    4 + 8 * count
}

fn state_cache_i64_map_len(count: usize) -> usize {
    4 + 16 * count
}

fn write_state_cache_u32(out: &mut impl io::Write, value: usize) -> io::Result<()> {
    let value = u32::try_from(value)
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "RWKV state is too large"))?;
    out.write_all(&value.to_le_bytes())
}

fn write_state_cache_optional_i64(out: &mut impl io::Write, value: Option<i64>) -> io::Result<()> {
    match value {
        Some(value) => {
            out.write_all(&[1])?;
            out.write_all(&value.to_le_bytes())
        }
        None => out.write_all(&[0]),
    }
}

fn write_state_cache_i64_set(
    out: &mut impl io::Write,
    values: impl Iterator<Item = i64>,
) -> io::Result<()> {
    let mut values: Vec<_> = values.collect();
    values.sort_unstable();
    write_state_cache_u32(out, values.len())?;
    for value in values {
        out.write_all(&value.to_le_bytes())?;
    }
    Ok(())
}

fn read_i64_set(cursor: &mut Cursor<'_>) -> io::Result<HashMap<i64, ()>> {
    let count = cursor.u32()? as usize;
    let mut values = HashMap::with_capacity(count);
    for _ in 0..count {
        values.insert(cursor.i64()?, ());
    }
    Ok(values)
}

fn write_state_cache_i64_map(
    out: &mut impl io::Write,
    values: &HashMap<i64, i64>,
) -> io::Result<()> {
    let mut values: Vec<_> = values.iter().collect();
    values.sort_by_key(|(key, _)| *key);
    write_state_cache_u32(out, values.len())?;
    for (key, value) in values {
        out.write_all(&key.to_le_bytes())?;
        out.write_all(&value.to_le_bytes())?;
    }
    Ok(())
}

fn read_i64_map(cursor: &mut Cursor<'_>) -> io::Result<HashMap<i64, i64>> {
    let count = cursor.u32()? as usize;
    let mut values = HashMap::with_capacity(count);
    for _ in 0..count {
        values.insert(cursor.i64()?, cursor.i64()?);
    }
    Ok(values)
}

fn write_f32_slice(out: &mut Vec<u8>, values: &[f32]) {
    write_u32(out, values.len() as u32);
    for value in values {
        out.extend_from_slice(&value.to_le_bytes());
    }
}

fn write_state_cache_f32_slice(out: &mut impl io::Write, values: &[f32]) -> io::Result<()> {
    write_state_cache_u32(out, values.len())?;
    let mut buffer = [0_u8; 8192];
    for values in values.chunks(buffer.len() / 4) {
        for (destination, value) in buffer.chunks_exact_mut(4).zip(values) {
            destination.copy_from_slice(&value.to_le_bytes());
        }
        out.write_all(&buffer[..values.len() * 4])?;
    }
    Ok(())
}

/// Whole days until `curve` reaches `target_retention`: the unrounded
/// crossing (`unrounded_interval_for_curve`) rounded up, at least 1 day and
/// at most `max_interval_days`.
fn interval_for_curve(
    curve: &ReviewCurve,
    target_retention: f32,
    max_interval_days: u32,
) -> Option<u32> {
    unrounded_interval_for_curve(curve, target_retention, max_interval_days)
        .map(|days| clamped_interval_days(days, max_interval_days))
}

/// The unrounded day where `curve` reaches `target_retention`, for
/// RWKV-Curve's S90 (spec sched.rwkv-curve-s90), found on the curve itself
/// by `crossings_on_grid`.
fn unrounded_interval_for_curve(
    curve: &ReviewCurve,
    target_retention: f32,
    max_interval_days: u32,
) -> Option<f32> {
    if !(0.0..=1.0).contains(&target_retention) || max_interval_days < 1 {
        return None;
    }
    let margins_at =
        |days: f32| [predict_curve(curve, days * SECONDS_PER_DAY as f32) - target_retention];
    let [crossing] = crossings_on_grid(max_interval_days, margins_at);
    Some(crossing)
}

/// The recall of `curve` at each of `elapsed_days`, and its S90 (where it
/// meets 90% recall, `unrounded_interval_for_curve`).
/// Bytes per saved curve source (`CURVE_SOURCE_FORMAT`).
fn curve_source_width() -> usize {
    D_MODEL * 2
}

/// Appends `prehead` as a curve source (`CURVE_SOURCE_FORMAT`).
fn encode_curve_source(prehead: &[f32], out: &mut Vec<u8>) {
    let start = out.len();
    out.resize(start + curve_source_width(), 0);
    encode_curve_source_into(prehead, &mut out[start..]);
}

/// Writes `prehead` as a curve source into `out`, `curve_source_width()`
/// bytes.
fn encode_curve_source_into(prehead: &[f32], out: &mut [u8]) {
    debug_assert_eq!(prehead.len(), D_MODEL);
    for (value, bytes) in prehead.iter().zip(out.chunks_exact_mut(2)) {
        bytes.copy_from_slice(&f16_bits(*value).to_le_bytes());
    }
}

fn decode_curve_source(source: &[u8]) -> Vec<f32> {
    source
        .chunks_exact(2)
        .map(|bits| f32_from_f16_bits(u16::from_le_bytes([bits[0], bits[1]])))
        .collect()
}

/// `value` as IEEE 754 half precision, rounded to nearest, ties to even.
fn f16_bits(value: f32) -> u16 {
    let bits = value.to_bits();
    let sign = ((bits >> 16) & 0x8000) as u16;
    let exponent = ((bits >> 23) & 0xff) as i32;
    let mantissa = bits & 0x007f_ffff;
    if exponent == 0xff {
        // infinity stays infinity; NaN stays a (quiet) NaN
        return sign | 0x7c00 | if mantissa != 0 { 0x0200 } else { 0 };
    }
    let half_exponent = exponent - 127 + 15;
    if half_exponent >= 0x1f {
        return sign | 0x7c00;
    }
    if half_exponent <= 0 {
        // a subnormal half, or zero
        if half_exponent < -10 {
            return sign;
        }
        let mantissa = mantissa | 0x0080_0000;
        let shift = (14 - half_exponent) as u32;
        let rounded = mantissa + (1 << (shift - 1)) - 1 + ((mantissa >> shift) & 1);
        return sign | (rounded >> shift) as u16;
    }
    let rounded = mantissa + 0x0fff + ((mantissa >> 13) & 1);
    // a carry out of the mantissa moves into the exponent, as it should
    sign | (((half_exponent as u32) << 10) + (rounded >> 13)) as u16
}

fn f32_from_f16_bits(half: u16) -> f32 {
    let sign = ((half & 0x8000) as u32) << 16;
    let exponent = ((half >> 10) & 0x1f) as u32;
    let mantissa = (half & 0x03ff) as u32;
    let bits = match exponent {
        0 if mantissa == 0 => sign,
        0 => {
            // subnormal: shift the mantissa up to an implicit leading one
            let mut exponent = 113;
            let mut mantissa = mantissa;
            while mantissa & 0x0400 == 0 {
                mantissa <<= 1;
                exponent -= 1;
            }
            sign | (exponent << 23) | ((mantissa & 0x03ff) << 13)
        }
        0x1f => sign | 0x7f80_0000 | (mantissa << 13),
        _ => sign | ((exponent + 112) << 23) | (mantissa << 13),
    };
    f32::from_bits(bits)
}

fn curve_points_and_s90(
    curve: &ReviewCurve,
    elapsed_days: &[f32],
    max_interval_days: u32,
) -> Option<(Vec<f32>, f32)> {
    let s90 = unrounded_interval_for_curve(curve, S90_TARGET_RETENTION, max_interval_days)?;
    let recall = elapsed_days
        .iter()
        .map(|days| predict_curve(curve, days * SECONDS_PER_DAY as f32))
        .collect();
    Some((recall, s90))
}

/// Packs the curves of `card_ids` that `curves` holds: for each, its weight
/// count (u32) and its weights (f32), little-endian, in the order of the
/// returned ids.
fn pack_stored_curves(curves: &HashMap<i64, ReviewCurve>, card_ids: &[i64]) -> (Vec<i64>, Vec<u8>) {
    let mut ids = Vec::new();
    let mut bytes = Vec::new();
    for &card_id in card_ids {
        if let Some(curve) = curves.get(&card_id) {
            ids.push(card_id);
            bytes.extend_from_slice(&(curve.weights.len() as u32).to_le_bytes());
            for weight in &curve.weights {
                bytes.extend_from_slice(&weight.to_le_bytes());
            }
        }
    }
    (ids, bytes)
}

/// The curves `RwkvInference::card_curve_weights` packed, one per card id;
/// None when the bytes do not hold exactly one curve per id.
pub fn unpack_stored_curves(card_ids: &[i64], bytes: &[u8]) -> Option<Vec<(i64, StoredCurve)>> {
    let mut rest = bytes;
    let mut curves = Vec::with_capacity(card_ids.len());
    for &card_id in card_ids {
        let (count, tail) = rest.split_first_chunk::<4>()?;
        let (weights, tail) =
            tail.split_at_checked((u32::from_le_bytes(*count) as usize).checked_mul(4)?)?;
        rest = tail;
        let weights = weights
            .chunks_exact(4)
            .map(|chunk| f32::from_le_bytes(chunk.try_into().unwrap()))
            .collect();
        curves.push((
            card_id,
            StoredCurve {
                curve: ReviewCurve {
                    ahead_logits: Vec::new(),
                    weights,
                    s90: Default::default(),
                },
            },
        ));
    }
    rest.is_empty().then_some(curves)
}

/// A curve RWKV-Curve stored for a card, outside the RWKV process: the same
/// recall and crossings card info and the reschedule use.
#[derive(Clone)]
pub struct StoredCurve {
    curve: ReviewCurve,
}

impl StoredCurve {
    /// The curve's recall `elapsed_days` after the review that stored it.
    pub fn recall(&self, elapsed_days: f32) -> f32 {
        predict_curve(&self.curve, elapsed_days * SECONDS_PER_DAY as f32)
    }

    /// The unrounded day where the curve meets `target_retention`, at most
    /// `max_interval_days` (`unrounded_interval_for_curve`).
    pub fn crossing(&self, target_retention: f32, max_interval_days: u32) -> Option<f32> {
        unrounded_interval_for_curve(&self.curve, target_retention, max_interval_days)
    }
}

/// Points (days) searched inside the first day when an answer curve reaches
/// its target before day 1 (spec sched.sub-day-intervals).
const SUB_DAY_SEARCH_POINTS: [f32; 14] = [
    1.0 / 1440.0,
    5.0 / 1440.0,
    10.0 / 1440.0,
    20.0 / 1440.0,
    30.0 / 1440.0,
    1.0 / 24.0,
    2.0 / 24.0,
    3.0 / 24.0,
    4.0 / 24.0,
    6.0 / 24.0,
    8.0 / 24.0,
    12.0 / 24.0,
    16.0 / 24.0,
    20.0 / 24.0,
];

/// Unrounded answer intervals in days, for the reviewer (spec
/// sched.sub-day-intervals), found on the curves themselves by
/// `crossings_on_grid`. With grade order enforced, the distance to each
/// grade's target is pooled across grades (`pava_non_decreasing`) wherever
/// the curves are evaluated, so the crossings come out in grade order.
fn unrounded_intervals_for_answer_curves(
    curves: [&ReviewCurve; 4],
    target_retentions: [f32; 4],
    max_interval_days: u32,
    enforce_grade_order: bool,
) -> [Option<f32>; 4] {
    let valid = target_retentions.map(|target| (0.0..=1.0).contains(&target));
    if max_interval_days < 1 || (enforce_grade_order && valid.iter().any(|valid| !valid)) {
        return [None; 4];
    }
    let margins_at = |days: f32| -> [f32; 4] {
        let margins = std::array::from_fn(|index| {
            predict_curve(curves[index], days * SECONDS_PER_DAY as f32) - target_retentions[index]
        });
        if enforce_grade_order {
            pava_non_decreasing(margins)
        } else {
            margins
        }
    };
    let crossings = crossings_on_grid(max_interval_days, margins_at);
    std::array::from_fn(|index| valid[index].then_some(crossings[index]))
}

/// Where each decreasing margin (a curve minus its target, in days) first
/// reaches 0, at most `max_interval_days` (spec sched.sub-day-intervals,
/// sched.rwkv-curve-s90). The day grid of `interval_search_days` (with
/// `SUB_DAY_SEARCH_POINTS` first when some margin is already at or below 0
/// after one day) brackets each crossing; `crossing_between` then finds it
/// on the margin itself. A margin at or below 0 at the first point is
/// bracketed from 0; one that never reaches 0 gives the maximum.
fn crossings_on_grid<const N: usize>(
    max_interval_days: u32,
    margins_at: impl Fn(f32) -> [f32; N],
) -> [f32; N] {
    let mut points = Vec::new();
    if margins_at(1.0).iter().any(|margin| *margin <= 0.0) {
        points.extend(SUB_DAY_SEARCH_POINTS);
    }
    points.extend(
        interval_search_days(max_interval_days)
            .into_iter()
            .map(|day| day as f32),
    );

    let maximum = max_interval_days as f32;
    let mut crossings: [Option<f32>; N] = [None; N];
    let mut previous: [Option<(f32, f32)>; N] = [None; N];
    let mut at_zero: Option<[f32; N]> = None;
    for point in points {
        let margins = margins_at(point);
        for index in 0..N {
            if crossings[index].is_some() {
                continue;
            }
            let margin = margins[index];
            if margin <= 0.0 {
                let (low, low_margin) = previous[index].unwrap_or_else(|| {
                    (0.0, at_zero.get_or_insert_with(|| margins_at(0.0))[index])
                });
                let crossing = if low_margin > 0.0 {
                    crossing_between(low, low_margin, point, margin, |days| {
                        margins_at(days)[index]
                    })
                } else {
                    point
                };
                crossings[index] = Some(crossing.min(maximum));
            }
            previous[index] = Some((point, margin));
        }
        if crossings.iter().all(Option::is_some) {
            break;
        }
    }
    crossings.map(|crossing| crossing.unwrap_or(maximum))
}

/// The day in (low, high] where the decreasing `margin` reaches 0, given
/// `low_margin > 0 >= high_margin`: a regula falsi search (Illinois variant)
/// on the curve itself, to about a second (or a millionth of the interval,
/// where f32 days cannot resolve a second). It starts from the straight line
/// between the two points, which alone lands late because the curve bends
/// upward, and returns the earliest day found at or below the target.
fn crossing_between(
    low: f32,
    low_margin: f32,
    high: f32,
    high_margin: f32,
    margin: impl Fn(f32) -> f32,
) -> f32 {
    const MAX_STEPS: usize = 64;
    let (mut low, mut low_margin) = (low as f64, low_margin as f64);
    let (mut high, mut high_margin) = (high as f64, high_margin as f64);
    // which end the previous step moved: -1 low, 1 high
    let mut last_moved = 0;
    for _ in 0..MAX_STEPS {
        let tolerance = (1.0 / SECONDS_PER_DAY as f64).max(high * 1e-6);
        if high - low <= tolerance || low_margin <= high_margin {
            break;
        }
        let mut day = (low * high_margin - high * low_margin) / (high_margin - low_margin);
        if !(day > low && day < high) {
            day = 0.5 * (low + high);
        }
        let day_margin = margin(day as f32) as f64;
        if day_margin > 0.0 {
            low = day;
            low_margin = day_margin;
            if last_moved == -1 {
                high_margin *= 0.5;
            }
            last_moved = -1;
        } else {
            high = day;
            high_margin = day_margin;
            if last_moved == 1 {
                low_margin *= 0.5;
            }
            last_moved = 1;
        }
    }
    high as f32
}

fn pava_non_decreasing(values: [f32; 4]) -> [f32; 4] {
    #[derive(Clone, Copy)]
    struct Block {
        first: usize,
        last: usize,
        sum: f32,
        count: usize,
    }

    impl Block {
        fn mean(self) -> f32 {
            self.sum / self.count as f32
        }
    }

    let empty_block = Block {
        first: 0,
        last: 0,
        sum: 0.0,
        count: 0,
    };
    let mut blocks = [empty_block; 4];
    let mut block_count = 0;
    for (index, value) in values.into_iter().enumerate() {
        blocks[block_count] = Block {
            first: index,
            last: index,
            sum: value,
            count: 1,
        };
        block_count += 1;
        while block_count >= 2 {
            let right = blocks[block_count - 1];
            let left = blocks[block_count - 2];
            if left.mean() <= right.mean() {
                break;
            }

            blocks[block_count - 2] = Block {
                first: left.first,
                last: right.last,
                sum: left.sum + right.sum,
                count: left.count + right.count,
            };
            block_count -= 1;
        }
    }

    let mut adjusted = [0.0; 4];
    for block in &blocks[..block_count] {
        adjusted[block.first..=block.last].fill(block.mean());
    }
    adjusted
}

fn interval_search_days(max_interval_days: u32) -> Vec<u32> {
    if max_interval_days <= 30 {
        return (1..=max_interval_days).collect();
    }

    let mut days = (1..=30).collect::<Vec<_>>();
    let mut day = 45;
    while day < max_interval_days {
        days.push(day);
        day = ((day as f32) * 1.5) as u32;
    }
    days.push(max_interval_days);
    days
}

fn clamped_interval_days(elapsed_days: f32, max_interval_days: u32) -> u32 {
    elapsed_days.ceil().clamp(1.0, max_interval_days as f32) as u32
}

fn current_curve_retrievability(input: &ReviewInput, curve: &ReviewCurve) -> Option<f32> {
    let elapsed_seconds = input
        .current_elapsed_seconds
        .filter(|seconds| *seconds >= 0)
        .or_else(|| {
            input
                .current_elapsed_days
                .filter(|days| *days >= 0)
                .map(|days| days.saturating_mul(SECONDS_PER_DAY))
        })?;
    Some(predict_curve(curve, elapsed_seconds as f32))
}

fn predict_curve(curve: &ReviewCurve, elapsed_seconds: f32) -> f32 {
    let elapsed_seconds = elapsed_seconds.max(1.0);
    let mut raw_probability = 0.0;
    for (index, weight) in curve.weights.iter().enumerate() {
        let s_space_raw = linspace_exp(index, NUM_CURVES, 18.5);
        let s_space = 0.1 + (s_space_raw - 1.0) * (22.0_f32 - 18.5).exp();
        raw_probability += weight * 0.9_f32.powf(elapsed_seconds / s_space);
    }
    1e-5 + (1.0 - 2e-5) * raw_probability
}

fn linspace_exp(index: usize, count: usize, point_spread: f32) -> f32 {
    let value = if count <= 1 {
        0.0
    } else {
        point_spread * index as f32 / (count - 1) as f32
    };
    value.exp()
}

struct RwkvModule {
    layers: Vec<RwkvLayer>,
}

#[cfg(test)]
struct ModuleQueryScratch {
    current: [f32; D_MODEL],
    next: [f32; D_MODEL],
    v0: [f32; D_MODEL],
    layer: LayerQueryScratch,
}

#[derive(Default)]
struct ModuleQueryBatchScratch {
    next: Vec<f32>,
    v0: Vec<f32>,
    layer: LayerQueryBatchScratch,
}

#[cfg(test)]
impl Default for ModuleQueryScratch {
    fn default() -> Self {
        Self {
            current: [0.0; D_MODEL],
            next: [0.0; D_MODEL],
            v0: [0.0; D_MODEL],
            layer: LayerQueryScratch::default(),
        }
    }
}

impl RwkvModule {
    fn load(weights: &WeightMap, module_id: usize, layer_count: usize) -> io::Result<Self> {
        let mut layers = Vec::with_capacity(layer_count);
        for layer_id in 0..layer_count {
            layers.push(RwkvLayer::load(weights, module_id, layer_id)?);
        }
        Ok(Self { layers })
    }

    fn run(&self, input: &[f32], state: Option<&ModuleState>) -> (Vec<f32>, ModuleState) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        let mut x = input.to_vec();
        let mut v0 = vec![0.0; D_MODEL];
        let mut next_layers = Vec::with_capacity(self.layers.len());

        for (layer_id, layer) in self.layers.iter().enumerate() {
            let layer_state = state.and_then(|state| state.layers.get(layer_id));
            let (next_x, next_v0, next_layer_state) = layer.run(&x, &v0, layer_state);
            x = next_x;
            v0 = next_v0;
            next_layers.push(next_layer_state);
        }

        let output = (
            x,
            ModuleState {
                layers: next_layers,
            },
        );
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::ModuleRun, profile_started);
        output
    }

    #[cfg(test)]
    fn run_query(
        &self,
        input: &[f32],
        state: Option<&ModuleState>,
        scratch: &mut ModuleQueryScratch,
    ) -> [f32; D_MODEL] {
        let ModuleQueryScratch {
            current,
            next,
            v0,
            layer: layer_scratch,
        } = scratch;
        current.copy_from_slice(input);
        v0.fill(0.0);
        let mut current = current;
        let mut next = next;

        for (layer_id, layer) in self.layers.iter().enumerate() {
            let layer_state = state.and_then(|state| state.layers.get(layer_id));
            layer.run_query_into(current, v0, layer_state, next, layer_scratch);
            std::mem::swap(&mut current, &mut next);
        }

        *current
    }

    fn run_query_batch(
        &self,
        x: &mut Vec<f32>,
        items: &[ReviewPredictionQueryRef<'_>],
        module_id: usize,
        scratch: &mut ModuleQueryBatchScratch,
    ) {
        let rows = items.len();
        scratch.v0.resize(rows * D_MODEL, 0.0);
        scratch.v0.fill(0.0);
        let mut current = std::mem::take(x);
        let mut next = std::mem::take(&mut scratch.next);

        for (layer_id, layer) in self.layers.iter().enumerate() {
            layer.run_query_batch(
                &current,
                &mut scratch.v0,
                items,
                module_id,
                layer_id,
                &mut next,
                &mut scratch.layer,
            );
            std::mem::swap(&mut current, &mut next);
        }

        *x = current;
        scratch.next = next;
    }
}

#[derive(Clone)]
struct ModuleState {
    layers: Vec<LayerState>,
}

struct RwkvLayer {
    time_mixer: TimeMixer,
    channel_mixer: ChannelMixer,
}

#[cfg(test)]
struct LayerQueryScratch {
    time_output: [f32; D_MODEL],
    time: TimeMixerQueryScratch,
    channel: ChannelMixerQueryScratch,
}

#[derive(Default)]
struct LayerQueryBatchScratch {
    time_output: Vec<f32>,
    time: TimeMixerQueryBatchScratch,
    channel: ChannelMixerQueryBatchScratch,
}

#[cfg(test)]
impl Default for LayerQueryScratch {
    fn default() -> Self {
        Self {
            time_output: [0.0; D_MODEL],
            time: TimeMixerQueryScratch::default(),
            channel: ChannelMixerQueryScratch::default(),
        }
    }
}

impl RwkvLayer {
    fn load(weights: &WeightMap, module_id: usize, layer_id: usize) -> io::Result<Self> {
        Ok(Self {
            time_mixer: TimeMixer::load(weights, module_id, layer_id)?,
            channel_mixer: ChannelMixer::load(weights, module_id, layer_id)?,
        })
    }

    fn run(
        &self,
        input: &[f32],
        v0: &[f32],
        state: Option<&LayerState>,
    ) -> (Vec<f32>, Vec<f32>, LayerState) {
        let (x, v0, time_state) =
            self.time_mixer
                .run(input, v0, state.and_then(|state| state.time.as_ref()));
        let (x, channel_shift) = self
            .channel_mixer
            .run(&x, state.and_then(|state| state.channel_shift.as_ref()));
        (
            x,
            v0,
            LayerState {
                time: Some(time_state),
                channel_shift: Some(channel_shift),
            },
        )
    }

    #[cfg(test)]
    fn run_query_into(
        &self,
        input: &[f32],
        v0: &mut [f32; D_MODEL],
        state: Option<&LayerState>,
        out: &mut [f32; D_MODEL],
        scratch: &mut LayerQueryScratch,
    ) {
        self.time_mixer.run_query_into(
            input,
            v0,
            state.and_then(|state| state.time.as_ref()),
            &mut scratch.time_output,
            &mut scratch.time,
        );
        self.channel_mixer.run_query_into(
            &scratch.time_output,
            state.and_then(|state| state.channel_shift.as_deref()),
            out,
            &mut scratch.channel,
        );
    }

    #[allow(clippy::too_many_arguments)]
    fn run_query_batch(
        &self,
        input: &[f32],
        v0: &mut [f32],
        items: &[ReviewPredictionQueryRef<'_>],
        module_id: usize,
        layer_id: usize,
        out: &mut Vec<f32>,
        scratch: &mut LayerQueryBatchScratch,
    ) {
        self.time_mixer.run_query_batch(
            input,
            v0,
            items,
            module_id,
            layer_id,
            &mut scratch.time_output,
            &mut scratch.time,
        );
        self.channel_mixer.run_query_batch(
            &scratch.time_output,
            items,
            module_id,
            layer_id,
            out,
            &mut scratch.channel,
        );
    }
}

#[derive(Clone)]
struct LayerState {
    time: Option<TimeState>,
    channel_shift: Option<Vec<f32>>,
}

struct TimeMixer {
    #[cfg(test)]
    module_id: usize,
    layer_id: usize,
    layer_norm: Norm,
    rkvdag_lerp: Vec<f32>,
    bonus: Vec<f32>,
    w_r: Linear,
    w_k: Linear,
    w_v: Linear,
    w_o: Linear,
    k_scale_linear: Linear,
    v_scale_linear: Linear,
    v_lora: LoraSimple,
    a_lora: LoraSimple,
    d_lora: LoraSimple,
    lora_a_g: Linear,
    lora_b_g: Linear,
    out_group_norm: Norm,
}

#[cfg(test)]
struct TimeMixerQueryScratch {
    x: [f32; D_MODEL],
    mixed: [[f32; D_MODEL]; 8],
    r: [f32; D_MODEL],
    k: [f32; D_MODEL],
    v: [f32; D_MODEL],
    k_scale: [f32; HEADS],
    v_scale: [f32; HEADS],
    a: [f32; D_MODEL],
    g: [f32; D_MODEL],
    w: [f32; D_MODEL],
    k_deformed: [f32; D_MODEL],
    timestep_out: [f32; D_MODEL],
    temp: [f32; D_MODEL],
    lora_hidden: [f32; MAX_LORA_RANK],
    next_row: [f32; HEAD_SIZE],
}

#[derive(Default)]
struct TimeMixerQueryBatchScratch {
    x: Vec<f32>,
    mixed: [Vec<f32>; 8],
    r: Vec<f32>,
    k: Vec<f32>,
    v: Vec<f32>,
    k_scale: Vec<f32>,
    v_scale: Vec<f32>,
    a: Vec<f32>,
    g: Vec<f32>,
    w: Vec<f32>,
    k_deformed: Vec<f32>,
    timestep_out: Vec<f32>,
    temp: Vec<f32>,
    lora_hidden: Vec<f32>,
}

#[cfg(test)]
impl Default for TimeMixerQueryScratch {
    fn default() -> Self {
        Self {
            x: [0.0; D_MODEL],
            mixed: [[0.0; D_MODEL]; 8],
            r: [0.0; D_MODEL],
            k: [0.0; D_MODEL],
            v: [0.0; D_MODEL],
            k_scale: [0.0; HEADS],
            v_scale: [0.0; HEADS],
            a: [0.0; D_MODEL],
            g: [0.0; D_MODEL],
            w: [0.0; D_MODEL],
            k_deformed: [0.0; D_MODEL],
            timestep_out: [0.0; D_MODEL],
            temp: [0.0; D_MODEL],
            lora_hidden: [0.0; MAX_LORA_RANK],
            next_row: [0.0; HEAD_SIZE],
        }
    }
}

impl TimeMixer {
    fn load(weights: &WeightMap, module_id: usize, layer_id: usize) -> io::Result<Self> {
        let prefix = format!("rwkv_modules.{module_id}.blocks.{layer_id}.time_mixer");
        Ok(Self {
            #[cfg(test)]
            module_id,
            layer_id,
            layer_norm: weights.layer_norm(&format!("{prefix}.layer_norm"), D_MODEL, 1e-5)?,
            rkvdag_lerp: weights.values(&format!("{prefix}.rkvdag_lerp"))?,
            bonus: weights.values(&format!("{prefix}.bonus"))?,
            w_r: weights.linear(&format!("{prefix}.W_r"), D_MODEL, D_MODEL, false)?,
            w_k: weights.linear(&format!("{prefix}.W_k"), D_MODEL, D_MODEL, false)?,
            w_v: weights.linear(&format!("{prefix}.W_v"), D_MODEL, D_MODEL, false)?,
            w_o: weights.linear(&format!("{prefix}.W_o"), D_MODEL, D_MODEL, false)?,
            k_scale_linear: weights.linear(
                &format!("{prefix}.k_scale_linear"),
                D_MODEL,
                HEADS,
                true,
            )?,
            v_scale_linear: weights.linear(
                &format!("{prefix}.v_scale_linear"),
                D_MODEL,
                HEADS,
                true,
            )?,
            v_lora: LoraSimple::load(weights, &format!("{prefix}.v_lora_simple"), 8)?,
            a_lora: LoraSimple::load(weights, &format!("{prefix}.a_lora_simple"), 16)?,
            d_lora: LoraSimple::load(weights, &format!("{prefix}.d_lora_mlp"), 16)?,
            lora_a_g: weights.linear(&format!("{prefix}.lora_A_g"), D_MODEL, 16, false)?,
            lora_b_g: weights.linear(&format!("{prefix}.lora_B_g"), 16, D_MODEL, false)?,
            out_group_norm: weights.group_norm(
                &format!("{prefix}.out_group_norm"),
                HEADS,
                D_MODEL,
                64e-5,
            )?,
        })
    }

    fn run(
        &self,
        input: &[f32],
        v0: &[f32],
        state: Option<&TimeState>,
    ) -> (Vec<f32>, Vec<f32>, TimeState) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        let x = self.layer_norm.apply(input);
        let (x_shift, state_matrix) = match state {
            Some(state) => (state.x_shift.as_slice(), state.matrix.as_slice()),
            None => (x.as_slice(), &[0.0; HEADS * HEAD_SIZE * HEAD_SIZE][..]),
        };

        let parts = self.mix_parts(&x, x_shift, v0);

        #[cfg(test)]
        record_rwkv_scan_step(
            self.module_id,
            self.layer_id,
            &parts.r,
            &parts.k,
            &parts.v,
            &parts.w,
            &parts.a,
            &parts.k_deformed,
        );

        let (out, next_matrix) = single_timestep(
            &parts.r,
            &parts.k,
            &parts.v,
            &parts.w,
            &parts.a,
            &parts.k_deformed,
            state_matrix,
        );
        let out = self.mix_output(&parts, out, input);

        let output = (
            out,
            parts.next_v0,
            TimeState {
                x_shift: x,
                matrix: next_matrix,
            },
        );
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::TimeMixer, profile_started);
        output
    }

    /// The pre-recurrence per-timestep math shared by the per-review and bulk
    /// replay paths: lerp mixes, projections, loras, activations, and head
    /// normalization. `x` must already be layer-normed; `x_shift` is the
    /// previous timestep's normed input for this stream (or `x` itself when
    /// the stream has no state yet).
    fn mix_parts(&self, x: &[f32], x_shift: &[f32], v0: &[f32]) -> TimeMixParts {
        let mut mixed = [0.0; D_MODEL];
        let fill_mixed = |mix_id: usize, mixed: &mut [f32; D_MODEL]| {
            let lerp_offset = mix_id * D_MODEL;
            for channel in 0..D_MODEL {
                mixed[channel] = lerp(
                    x[channel],
                    x_shift[channel],
                    self.rkvdag_lerp[lerp_offset + channel],
                );
            }
        };

        fill_mixed(0, &mut mixed);
        let r = self.w_r.apply(&mixed);
        fill_mixed(1, &mut mixed);
        let mut k = self.w_k.apply(&mixed);
        fill_mixed(6, &mut mixed);
        let mut k_scale = self.k_scale_linear.apply(&mixed);
        sigmoid_in_place(&mut k_scale);
        fill_mixed(7, &mut mixed);
        let mut v_scale = self.v_scale_linear.apply(&mixed);
        sigmoid_in_place(&mut v_scale);

        fill_mixed(2, &mut mixed);
        let (v, next_v0) = if self.layer_id == 0 {
            let v = self.w_v.apply(&mixed);
            (v.clone(), v)
        } else {
            let mut v_lerp = self.v_lora.apply_sigmoid(&mixed);
            let w_v = self.w_v.apply(&mixed);
            for channel in 0..D_MODEL {
                v_lerp[channel] = lerp(w_v[channel], v0[channel], v_lerp[channel]);
            }
            (v_lerp, v0.to_vec())
        };

        fill_mixed(4, &mut mixed);
        let a = self.a_lora.apply_sigmoid(&mixed);
        fill_mixed(5, &mut mixed);
        let mut g = self.lora_a_g.apply(&mixed);
        sigmoid_in_place(&mut g);
        g = self.lora_b_g.apply(&g);

        fill_mixed(3, &mut mixed);
        let mut w = self.d_lora.apply_tanh(&mixed);
        for value in &mut w {
            let d = -0.5 - softplus(-*value);
            *value = (-d.exp()).exp();
        }

        normalize_heads_in_place(&mut k);
        for head in 0..HEADS {
            for index in 0..HEAD_SIZE {
                k[head * HEAD_SIZE + index] *= k_scale[head];
            }
        }

        let mut v = v;
        normalize_heads_in_place(&mut v);
        for head in 0..HEADS {
            for index in 0..HEAD_SIZE {
                v[head * HEAD_SIZE + index] *= v_scale[head];
            }
        }

        let k_deformed = k.clone();
        for channel in 0..D_MODEL {
            k[channel] *= a[channel];
        }

        TimeMixParts {
            r,
            k,
            v,
            w,
            a,
            k_deformed,
            g,
            next_v0,
        }
    }

    /// The post-recurrence per-timestep math shared by the per-review and
    /// bulk replay paths: group norm, bonus, gate, output projection, and the
    /// residual add against the un-normed layer input.
    fn mix_output(
        &self,
        parts: &TimeMixParts,
        recurrence_out: Vec<f32>,
        input: &[f32],
    ) -> Vec<f32> {
        let mut out = self.out_group_norm.apply(&recurrence_out);

        let mut bonus = [0.0; D_MODEL];
        for head in 0..HEADS {
            let base = head * HEAD_SIZE;
            let mut bonus_scale = 0.0;
            for index in 0..HEAD_SIZE {
                bonus_scale +=
                    parts.r[base + index] * self.bonus[base + index] * parts.k[base + index];
            }
            for index in 0..HEAD_SIZE {
                bonus[base + index] = bonus_scale * parts.v[base + index];
            }
        }

        for channel in 0..D_MODEL {
            out[channel] = parts.g[channel] * (out[channel] + bonus[channel]);
        }
        let mut out = self.w_o.apply(&out);
        for channel in 0..D_MODEL {
            out[channel] += input[channel];
        }
        out
    }

    #[cfg(test)]
    fn run_query_into(
        &self,
        input: &[f32],
        v0: &mut [f32; D_MODEL],
        state: Option<&TimeState>,
        out: &mut [f32; D_MODEL],
        scratch: &mut TimeMixerQueryScratch,
    ) {
        let TimeMixerQueryScratch {
            x,
            mixed,
            r,
            k,
            v,
            k_scale,
            v_scale,
            a,
            g,
            w,
            k_deformed,
            timestep_out,
            temp,
            lora_hidden,
            next_row,
        } = scratch;

        self.layer_norm.apply_into(input, x);
        let x_shift = state.map_or(x.as_slice(), |state| state.x_shift.as_slice());
        for (mix_id, mixed_row) in mixed.iter_mut().enumerate() {
            let lerp_offset = mix_id * D_MODEL;
            for channel in 0..D_MODEL {
                mixed_row[channel] = lerp(
                    x[channel],
                    x_shift[channel],
                    self.rkvdag_lerp[lerp_offset + channel],
                );
            }
        }

        self.w_r.apply_into(&mixed[0], r);
        self.w_k.apply_into(&mixed[1], k);
        self.k_scale_linear.apply_into(&mixed[6], k_scale);
        sigmoid_in_place(k_scale);
        self.v_scale_linear.apply_into(&mixed[7], v_scale);
        sigmoid_in_place(v_scale);

        if self.layer_id == 0 {
            self.w_v.apply_into(&mixed[2], v);
            v0.copy_from_slice(v);
        } else {
            self.v_lora.apply_sigmoid_into(&mixed[2], lora_hidden, v);
            self.w_v.apply_into(&mixed[2], temp);
            for channel in 0..D_MODEL {
                v[channel] = lerp(temp[channel], v0[channel], v[channel]);
            }
        }

        self.a_lora.apply_sigmoid_into(&mixed[4], lora_hidden, a);
        self.lora_a_g.apply_into(&mixed[5], lora_hidden);
        sigmoid_in_place(lora_hidden);
        self.lora_b_g.apply_into(lora_hidden, g);

        self.d_lora.apply_tanh_into(&mixed[3], lora_hidden, w);
        for value in w.iter_mut() {
            let decay = -0.5 - softplus(-*value);
            *value = (-decay.exp()).exp();
        }

        normalize_heads_in_place(k);
        for head in 0..HEADS {
            for index in 0..HEAD_SIZE {
                k[head * HEAD_SIZE + index] *= k_scale[head];
            }
        }

        normalize_heads_in_place(v);
        for head in 0..HEADS {
            for index in 0..HEAD_SIZE {
                v[head * HEAD_SIZE + index] *= v_scale[head];
            }
        }

        k_deformed.copy_from_slice(k);
        for channel in 0..D_MODEL {
            k[channel] *= a[channel];
        }

        single_timestep_query_into(
            r,
            k,
            v,
            w,
            a,
            k_deformed,
            state.map(|state| state.matrix.as_slice()),
            timestep_out,
            next_row,
        );
        self.out_group_norm.apply_into(timestep_out, temp);

        for head in 0..HEADS {
            let base = head * HEAD_SIZE;
            let mut bonus_scale = 0.0;
            for index in 0..HEAD_SIZE {
                bonus_scale += r[base + index] * self.bonus[base + index] * k[base + index];
            }
            for index in 0..HEAD_SIZE {
                let channel = base + index;
                temp[channel] = g[channel] * (temp[channel] + bonus_scale * v[channel]);
            }
        }

        self.w_o.apply_into(temp, out);
        for channel in 0..D_MODEL {
            out[channel] += input[channel];
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn run_query_batch(
        &self,
        input: &[f32],
        v0: &mut [f32],
        items: &[ReviewPredictionQueryRef<'_>],
        module_id: usize,
        layer_id: usize,
        out: &mut Vec<f32>,
        scratch: &mut TimeMixerQueryBatchScratch,
    ) {
        let rows = items.len();
        debug_assert_eq!(input.len(), rows * D_MODEL);
        debug_assert_eq!(v0.len(), rows * D_MODEL);

        self.layer_norm.apply_batch(input, rows, &mut scratch.x);
        for (mix_id, mixed) in scratch.mixed.iter_mut().enumerate() {
            mixed.resize(rows * D_MODEL, 0.0);
            let lerp_weights = &self.rkvdag_lerp[mix_id * D_MODEL..(mix_id + 1) * D_MODEL];
            mixed
                .chunks_mut(D_MODEL)
                .enumerate()
                .for_each(|(row, mixed)| {
                    let x = &scratch.x[row * D_MODEL..(row + 1) * D_MODEL];
                    let x_shift = items[row]
                        .layer_state(module_id, layer_id)
                        .and_then(|state| state.time.as_ref())
                        .map_or(x, |state| state.x_shift.as_slice());
                    for channel in 0..D_MODEL {
                        mixed[channel] = lerp(x[channel], x_shift[channel], lerp_weights[channel]);
                    }
                });
        }

        self.w_r
            .apply_batch(&scratch.mixed[0], rows, &mut scratch.r);
        self.w_k
            .apply_batch(&scratch.mixed[1], rows, &mut scratch.k);
        self.k_scale_linear
            .apply_batch(&scratch.mixed[6], rows, &mut scratch.k_scale);
        sigmoid_in_place(&mut scratch.k_scale);
        self.v_scale_linear
            .apply_batch(&scratch.mixed[7], rows, &mut scratch.v_scale);
        sigmoid_in_place(&mut scratch.v_scale);

        if self.layer_id == 0 {
            self.w_v
                .apply_batch(&scratch.mixed[2], rows, &mut scratch.v);
            v0.copy_from_slice(&scratch.v);
        } else {
            self.v_lora.apply_sigmoid_batch(
                &scratch.mixed[2],
                rows,
                &mut scratch.lora_hidden,
                &mut scratch.v,
            );
            self.w_v
                .apply_batch(&scratch.mixed[2], rows, &mut scratch.temp);
            scratch
                .v
                .chunks_mut(D_MODEL)
                .enumerate()
                .for_each(|(row, v)| {
                    let projected = &scratch.temp[row * D_MODEL..(row + 1) * D_MODEL];
                    let v0 = &v0[row * D_MODEL..(row + 1) * D_MODEL];
                    for channel in 0..D_MODEL {
                        v[channel] = lerp(projected[channel], v0[channel], v[channel]);
                    }
                });
        }

        self.a_lora.apply_sigmoid_batch(
            &scratch.mixed[4],
            rows,
            &mut scratch.lora_hidden,
            &mut scratch.a,
        );
        self.lora_a_g
            .apply_batch(&scratch.mixed[5], rows, &mut scratch.lora_hidden);
        sigmoid_in_place(&mut scratch.lora_hidden);
        self.lora_b_g
            .apply_batch(&scratch.lora_hidden, rows, &mut scratch.g);

        self.d_lora.apply_tanh_batch(
            &scratch.mixed[3],
            rows,
            &mut scratch.lora_hidden,
            &mut scratch.w,
        );
        scratch.w.iter_mut().for_each(|value| {
            *value = query_decay(*value);
        });

        scratch
            .k
            .chunks_mut(D_MODEL)
            .enumerate()
            .for_each(|(row, k)| {
                normalize_heads_in_place(k);
                let scales = &scratch.k_scale[row * HEADS..(row + 1) * HEADS];
                scale_heads_in_place(k, scales);
            });
        scratch
            .v
            .chunks_mut(D_MODEL)
            .enumerate()
            .for_each(|(row, v)| {
                normalize_heads_in_place(v);
                let scales = &scratch.v_scale[row * HEADS..(row + 1) * HEADS];
                scale_heads_in_place(v, scales);
            });

        scratch.k_deformed.resize(rows * D_MODEL, 0.0);
        scratch.k_deformed.copy_from_slice(&scratch.k);
        scratch
            .k
            .iter_mut()
            .zip(scratch.a.iter())
            .for_each(|(k, a)| *k *= *a);

        scratch.timestep_out.resize(rows * D_MODEL, 0.0);
        scratch
            .timestep_out
            .chunks_mut(D_MODEL)
            .enumerate()
            .for_each(|(row, timestep_out)| {
                let range = row * D_MODEL..(row + 1) * D_MODEL;
                let state = items[row]
                    .layer_state(module_id, layer_id)
                    .and_then(|state| state.time.as_ref())
                    .map(|state| state.matrix.as_slice());
                single_timestep_query_fast_into(
                    &scratch.r[range.clone()],
                    &scratch.k[range.clone()],
                    &scratch.v[range.clone()],
                    &scratch.w[range.clone()],
                    &scratch.a[range.clone()],
                    &scratch.k_deformed[range],
                    state,
                    timestep_out,
                );
            });
        self.out_group_norm
            .apply_batch(&scratch.timestep_out, rows, &mut scratch.temp);

        scratch
            .temp
            .chunks_mut(D_MODEL)
            .enumerate()
            .for_each(|(row, output)| {
                let base = row * D_MODEL;
                for head in 0..HEADS {
                    let head_base = head * HEAD_SIZE;
                    let mut bonus_scale = 0.0;
                    for index in 0..HEAD_SIZE {
                        let channel = base + head_base + index;
                        bonus_scale +=
                            scratch.r[channel] * self.bonus[head_base + index] * scratch.k[channel];
                    }
                    for index in 0..HEAD_SIZE {
                        let local_channel = head_base + index;
                        let channel = base + local_channel;
                        output[local_channel] = scratch.g[channel]
                            * (output[local_channel] + bonus_scale * scratch.v[channel]);
                    }
                }
            });

        self.w_o.apply_batch(&scratch.temp, rows, out);
        out.iter_mut()
            .zip(input.iter())
            .for_each(|(output, input)| *output += *input);
    }
}

/// Per-timestep time-mixer coefficients produced ahead of the recurrence.
struct TimeMixParts {
    r: Vec<f32>,
    k: Vec<f32>,
    v: Vec<f32>,
    w: Vec<f32>,
    a: Vec<f32>,
    k_deformed: Vec<f32>,
    g: Vec<f32>,
    next_v0: Vec<f32>,
}

#[derive(Clone)]
struct TimeState {
    x_shift: Vec<f32>,
    matrix: Vec<f32>,
}

struct ChannelMixer {
    layer_norm: Norm,
    lerp_k: Vec<f32>,
    w_k: Linear,
    w_v: Linear,
}

#[cfg(test)]
struct ChannelMixerQueryScratch {
    x: [f32; D_MODEL],
    mixed: [f32; D_MODEL],
    hidden: [f32; MAX_CHANNEL_MIXER_DIM],
    projected: [f32; D_MODEL],
}

#[derive(Default)]
struct ChannelMixerQueryBatchScratch {
    x: Vec<f32>,
    mixed: Vec<f32>,
    hidden: Vec<f32>,
    projected: Vec<f32>,
}

#[cfg(test)]
impl Default for ChannelMixerQueryScratch {
    fn default() -> Self {
        Self {
            x: [0.0; D_MODEL],
            mixed: [0.0; D_MODEL],
            hidden: [0.0; MAX_CHANNEL_MIXER_DIM],
            projected: [0.0; D_MODEL],
        }
    }
}

impl ChannelMixer {
    fn load(weights: &WeightMap, module_id: usize, layer_id: usize) -> io::Result<Self> {
        let channel_dim = CHANNEL_MIXER_DIMS[module_id];
        let prefix = format!("rwkv_modules.{module_id}.blocks.{layer_id}.channel_mixer");
        Ok(Self {
            layer_norm: weights.layer_norm(&format!("{prefix}.layer_norm"), D_MODEL, 1e-5)?,
            lerp_k: weights.values(&format!("{prefix}.lerp_k"))?,
            w_k: weights.linear(&format!("{prefix}.W_k"), D_MODEL, channel_dim, false)?,
            w_v: weights.linear(&format!("{prefix}.W_v"), channel_dim, D_MODEL, false)?,
        })
    }

    fn run(&self, input: &[f32], state: Option<&Vec<f32>>) -> (Vec<f32>, Vec<f32>) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        let x = self.layer_norm.apply(input);
        let x_shift = state.map_or(x.as_slice(), |state| state.as_slice());
        let out = self.mix_value(&x, x_shift, input);
        let output = (out, x);
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::ChannelMixer, profile_started);
        output
    }

    /// The per-timestep channel-mixer math shared by the per-review and bulk
    /// replay paths. `x` must already be layer-normed; `x_shift` is the
    /// previous timestep's normed input for this stream (or `x` itself when
    /// the stream has no state yet); `input` is the un-normed layer input
    /// used for the residual add.
    fn mix_value(&self, x: &[f32], x_shift: &[f32], input: &[f32]) -> Vec<f32> {
        let mut mixed = [0.0; D_MODEL];
        for channel in 0..D_MODEL {
            mixed[channel] = lerp(x[channel], x_shift[channel], self.lerp_k[channel]);
        }

        let mut k = self.w_k.apply(&mixed);
        for value in &mut k {
            *value = value.max(0.0).powi(2);
        }
        let mut out = self.w_v.apply(&k);
        for channel in 0..D_MODEL {
            out[channel] += input[channel];
        }
        out
    }

    #[cfg(test)]
    fn run_query_into(
        &self,
        input: &[f32],
        state: Option<&[f32]>,
        out: &mut [f32; D_MODEL],
        scratch: &mut ChannelMixerQueryScratch,
    ) {
        self.layer_norm.apply_into(input, &mut scratch.x);
        let x_shift = state.unwrap_or(&scratch.x);
        for (channel, mixed) in scratch.mixed.iter_mut().enumerate() {
            *mixed = lerp(scratch.x[channel], x_shift[channel], self.lerp_k[channel]);
        }

        let hidden = &mut scratch.hidden[..self.w_k.output];
        self.w_k.apply_into(&scratch.mixed, hidden);
        for value in hidden.iter_mut() {
            *value = value.max(0.0).powi(2);
        }
        self.w_v.apply_into(hidden, &mut scratch.projected);
        for channel in 0..D_MODEL {
            out[channel] = input[channel] + scratch.projected[channel];
        }
    }

    fn run_query_batch(
        &self,
        input: &[f32],
        items: &[ReviewPredictionQueryRef<'_>],
        module_id: usize,
        layer_id: usize,
        out: &mut Vec<f32>,
        scratch: &mut ChannelMixerQueryBatchScratch,
    ) {
        let rows = items.len();
        self.layer_norm.apply_batch(input, rows, &mut scratch.x);
        scratch.mixed.resize(rows * D_MODEL, 0.0);
        scratch
            .mixed
            .chunks_mut(D_MODEL)
            .enumerate()
            .for_each(|(row, mixed)| {
                let x = &scratch.x[row * D_MODEL..(row + 1) * D_MODEL];
                let x_shift = items[row]
                    .layer_state(module_id, layer_id)
                    .and_then(|state| state.channel_shift.as_deref())
                    .unwrap_or(x);
                for channel in 0..D_MODEL {
                    mixed[channel] = lerp(x[channel], x_shift[channel], self.lerp_k[channel]);
                }
            });

        self.w_k
            .apply_batch(&scratch.mixed, rows, &mut scratch.hidden);
        scratch
            .hidden
            .iter_mut()
            .for_each(|value| *value = value.max(0.0).powi(2));
        self.w_v
            .apply_batch(&scratch.hidden, rows, &mut scratch.projected);
        out.resize(rows * D_MODEL, 0.0);
        out.iter_mut()
            .zip(input.iter())
            .zip(scratch.projected.iter())
            .for_each(|((output, input), projected)| *output = *input + *projected);
    }
}

struct LoraSimple {
    a: Linear,
    b: Linear,
}

impl LoraSimple {
    fn load(weights: &WeightMap, prefix: &str, rank: usize) -> io::Result<Self> {
        Ok(Self {
            a: weights.linear(&format!("{prefix}.A"), D_MODEL, rank, false)?,
            b: weights.linear(&format!("{prefix}.B_and_lamb"), rank, D_MODEL, true)?,
        })
    }

    fn apply_sigmoid(&self, input: &[f32]) -> Vec<f32> {
        let mut out = self.b.apply(&self.a.apply(input));
        sigmoid_in_place(&mut out);
        out
    }

    #[cfg(test)]
    fn apply_sigmoid_into(&self, input: &[f32], hidden: &mut [f32], out: &mut [f32]) {
        let hidden = &mut hidden[..self.a.output];
        self.a.apply_into(input, hidden);
        self.b.apply_into(hidden, out);
        sigmoid_in_place(out);
    }

    fn apply_sigmoid_batch(
        &self,
        input: &[f32],
        rows: usize,
        hidden: &mut Vec<f32>,
        out: &mut Vec<f32>,
    ) {
        self.a.apply_batch(input, rows, hidden);
        self.b.apply_batch(hidden, rows, out);
        sigmoid_in_place(out);
    }

    fn apply_tanh(&self, input: &[f32]) -> Vec<f32> {
        let mut hidden = self.a.apply(input);
        for value in &mut hidden {
            *value = value.tanh();
        }
        self.b.apply(&hidden)
    }

    /// Block variant of `apply_sigmoid`; bit-identical per row.
    fn apply_sigmoid_block_into(&self, inputs: &[f32], outs: &mut [f32], rows: usize) {
        let rank = self.a.output;
        debug_assert!(rank <= 16);
        debug_assert!(rows <= LINEAR_BLOCK_ROWS);
        let mut hidden = [0.0; 16 * LINEAR_BLOCK_ROWS];
        let hidden = &mut hidden[..rows * rank];
        self.a.apply_block_into(inputs, hidden, rows);
        self.b.apply_block_into(hidden, outs, rows);
        sigmoid_in_place(outs);
    }

    /// Block variant of `apply_tanh`; bit-identical per row.
    fn apply_tanh_block_into(&self, inputs: &[f32], outs: &mut [f32], rows: usize) {
        let rank = self.a.output;
        debug_assert!(rank <= 16);
        debug_assert!(rows <= LINEAR_BLOCK_ROWS);
        let mut hidden = [0.0; 16 * LINEAR_BLOCK_ROWS];
        let hidden = &mut hidden[..rows * rank];
        self.a.apply_block_into(inputs, hidden, rows);
        for value in hidden.iter_mut() {
            *value = value.tanh();
        }
        self.b.apply_block_into(hidden, outs, rows);
    }

    #[cfg(test)]
    fn apply_tanh_into(&self, input: &[f32], hidden: &mut [f32], out: &mut [f32]) {
        let hidden = &mut hidden[..self.a.output];
        self.a.apply_into(input, hidden);
        for value in hidden.iter_mut() {
            *value = value.tanh();
        }
        self.b.apply_into(hidden, out);
    }

    fn apply_tanh_batch(
        &self,
        input: &[f32],
        rows: usize,
        hidden: &mut Vec<f32>,
        out: &mut Vec<f32>,
    ) {
        self.a.apply_batch(input, rows, hidden);
        hidden.iter_mut().for_each(|value| *value = value.tanh());
        self.b.apply_batch(hidden, rows, out);
    }
}

struct Linear {
    input: usize,
    output: usize,
    weight_by_input: Vec<f32>,
    bias: Option<Vec<f32>>,
}

impl Linear {
    fn apply(&self, input: &[f32]) -> Vec<f32> {
        let mut out = vec![0.0; self.output];
        self.apply_into(input, &mut out);
        out
    }

    fn apply_into(&self, input: &[f32], out: &mut [f32]) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        debug_assert_eq!(input.len(), self.input);
        debug_assert_eq!(out.len(), self.output);
        if let Some(bias) = &self.bias {
            out.copy_from_slice(bias);
        } else {
            out.fill(0.0);
        }
        for (column, scale) in input.iter().copied().enumerate() {
            if scale == 0.0 {
                continue;
            }
            let weight_column =
                &self.weight_by_input[column * self.output..(column + 1) * self.output];
            add_scaled_in_place(out, weight_column, scale);
        }
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::Linear, profile_started);
    }

    fn apply_batch(&self, input: &[f32], rows: usize, out: &mut Vec<f32>) {
        let input_len = rows
            .checked_mul(self.input)
            .expect("linear batch input is too large");
        let output_len = rows
            .checked_mul(self.output)
            .expect("linear batch output is too large");
        assert_eq!(input.len(), input_len);
        out.resize(output_len, 0.0);
        if rows == 0 {
            return;
        }

        #[cfg(target_os = "macos")]
        matmul::matrix_times_matrix(
            input,
            &self.weight_by_input,
            rows,
            self.output,
            self.input,
            out,
        );

        // Blocks of rows share each weight load. The caller already runs
        // batches in parallel, so a batch stays on one thread.
        #[cfg(not(target_os = "macos"))]
        for (output, input) in out
            .chunks_mut(LINEAR_BLOCK_ROWS * self.output)
            .zip(input.chunks(LINEAR_BLOCK_ROWS * self.input))
        {
            self.apply_block_into(input, output, input.len() / self.input);
        }

        #[cfg(target_os = "macos")]
        if let Some(bias) = &self.bias {
            out.chunks_mut(self.output).for_each(|output| {
                for (output, bias) in output.iter_mut().zip(bias) {
                    *output += bias;
                }
            });
        }
    }

    /// Applies the projection to `rows` packed input rows at once, streaming
    /// each weight column once per block instead of once per row. For every
    /// output element this performs the identical operation sequence as
    /// `apply_into` (bias init, then ascending-column fused accumulation with
    /// the same zero-column skip), so results are bit-identical to calling
    /// `apply_into` per row.
    fn apply_block_into(&self, inputs: &[f32], outs: &mut [f32], rows: usize) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();

        debug_assert_eq!(inputs.len(), rows * self.input);
        debug_assert_eq!(outs.len(), rows * self.output);
        for out in outs.chunks_exact_mut(self.output) {
            if let Some(bias) = &self.bias {
                out.copy_from_slice(bias);
            } else {
                out.fill(0.0);
            }
        }

        #[cfg(target_arch = "aarch64")]
        if rows >= 4 {
            // SAFETY: aarch64 guarantees NEON support. The helper receives
            // slices whose dimensions were checked above and only accesses
            // complete rows and output vectors within those dimensions.
            unsafe {
                linear_apply_block_neon(
                    inputs,
                    &self.weight_by_input,
                    outs,
                    rows,
                    self.input,
                    self.output,
                );
            }
            #[cfg(test)]
            rwkv_warmup_profile_record(RwkvWarmupProfileBucket::Linear, profile_started);
            return;
        }

        #[cfg(target_arch = "x86_64")]
        if rows >= 4 && x86_avx2_fma_available() {
            // SAFETY: availability was checked above. The helper receives
            // slices whose dimensions were checked above and only accesses
            // complete rows and output vectors within those dimensions.
            unsafe {
                linear_apply_block_avx2_fma(
                    inputs,
                    &self.weight_by_input,
                    outs,
                    rows,
                    self.input,
                    self.output,
                );
            }
            #[cfg(test)]
            rwkv_warmup_profile_record(RwkvWarmupProfileBucket::Linear, profile_started);
            return;
        }

        for column in 0..self.input {
            let weight_column =
                &self.weight_by_input[column * self.output..(column + 1) * self.output];
            for (row, out) in outs.chunks_exact_mut(self.output).enumerate() {
                let scale = inputs[row * self.input + column];
                if scale == 0.0 {
                    continue;
                }
                add_scaled_in_place(out, weight_column, scale);
            }
        }
        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::Linear, profile_started);
    }

    /// Uses the native matrix backend for non-persisted bulk query rows when
    /// requested. Answer rows retain `apply_block_into()`'s exact accumulation
    /// order so warm-up state snapshots remain byte-identical.
    fn apply_block_approx_into(
        &self,
        inputs: &[f32],
        outs: &mut [f32],
        rows: usize,
        approximate: bool,
    ) {
        #[cfg(not(target_os = "macos"))]
        let _ = approximate;

        #[cfg(target_os = "macos")]
        if approximate && rows >= 16 && self.input * self.output >= 4096 {
            matmul::matrix_times_matrix(
                inputs,
                &self.weight_by_input,
                rows,
                self.output,
                self.input,
                outs,
            );
            if let Some(bias) = &self.bias {
                for out in outs.chunks_exact_mut(self.output) {
                    for (value, bias) in out.iter_mut().zip(bias) {
                        *value += bias;
                    }
                }
            }
            return;
        }

        self.apply_block_into(inputs, outs, rows);
    }
}

#[inline(always)]
fn add_scaled_in_place(out: &mut [f32], weights: &[f32], scale: f32) {
    debug_assert_eq!(out.len(), weights.len());
    add_scaled_in_place_arch(out, weights, scale)
}

#[cfg(target_arch = "aarch64")]
#[inline(always)]
fn add_scaled_in_place_arch(out: &mut [f32], weights: &[f32], scale: f32) {
    // SAFETY: aarch64 guarantees NEON support, and the helper only uses
    // unaligned loads/stores within the bounds checked by its loop conditions.
    unsafe { add_scaled_in_place_neon(out, weights, scale) }
}

#[cfg(target_arch = "x86_64")]
#[inline(always)]
fn add_scaled_in_place_arch(out: &mut [f32], weights: &[f32], scale: f32) {
    if x86_avx2_fma_available() {
        // SAFETY: availability was checked above, and the helper only uses
        // unaligned loads/stores within the bounds checked by its loops.
        unsafe { add_scaled_in_place_avx2_fma(out, weights, scale) }
    } else {
        add_scaled_in_place_scalar(out, weights, scale)
    }
}

#[cfg(all(not(target_arch = "aarch64"), not(target_arch = "x86_64")))]
#[inline(always)]
fn add_scaled_in_place_arch(out: &mut [f32], weights: &[f32], scale: f32) {
    add_scaled_in_place_scalar(out, weights, scale)
}

#[cfg(target_arch = "aarch64")]
#[inline(always)]
unsafe fn add_scaled_in_place_neon(out: &mut [f32], weights: &[f32], scale: f32) {
    use std::arch::aarch64::*;

    let mut offset = 0;
    let len = out.len();
    let scale = vdupq_n_f32(scale);
    while offset + 16 <= len {
        let out_ptr = out.as_mut_ptr().add(offset);
        let weight_ptr = weights.as_ptr().add(offset);
        vst1q_f32(
            out_ptr,
            vfmaq_f32(vld1q_f32(out_ptr), vld1q_f32(weight_ptr), scale),
        );
        vst1q_f32(
            out_ptr.add(4),
            vfmaq_f32(
                vld1q_f32(out_ptr.add(4)),
                vld1q_f32(weight_ptr.add(4)),
                scale,
            ),
        );
        vst1q_f32(
            out_ptr.add(8),
            vfmaq_f32(
                vld1q_f32(out_ptr.add(8)),
                vld1q_f32(weight_ptr.add(8)),
                scale,
            ),
        );
        vst1q_f32(
            out_ptr.add(12),
            vfmaq_f32(
                vld1q_f32(out_ptr.add(12)),
                vld1q_f32(weight_ptr.add(12)),
                scale,
            ),
        );
        offset += 16;
    }
    while offset + 4 <= len {
        let out_ptr = out.as_mut_ptr().add(offset);
        let weight_ptr = weights.as_ptr().add(offset);
        vst1q_f32(
            out_ptr,
            vfmaq_f32(vld1q_f32(out_ptr), vld1q_f32(weight_ptr), scale),
        );
        offset += 4;
    }
    while offset < len {
        out[offset] += weights[offset] * vgetq_lane_f32(scale, 0);
        offset += 1;
    }
}

/// Exact four-row linear projection microkernel for AArch64 NEON machines.
///
/// Each output accumulator still visits input columns in ascending order and
/// skips zero scales, matching `Linear::apply_into()`. Four rows share each
/// weight load while retaining the column-major loop order, so input scales
/// are loaded only once per projection column.
#[cfg(target_arch = "aarch64")]
unsafe fn linear_apply_block_neon(
    inputs: &[f32],
    weights: &[f32],
    outs: &mut [f32],
    rows: usize,
    input: usize,
    output: usize,
) {
    use std::arch::aarch64::*;

    let tiled_rows = rows / 4 * 4;
    for row_base in (0..tiled_rows).step_by(4) {
        for column in 0..input {
            let scale0 = *inputs.get_unchecked(row_base * input + column);
            let scale1 = *inputs.get_unchecked((row_base + 1) * input + column);
            let scale2 = *inputs.get_unchecked((row_base + 2) * input + column);
            let scale3 = *inputs.get_unchecked((row_base + 3) * input + column);
            if scale0 == 0.0 && scale1 == 0.0 && scale2 == 0.0 && scale3 == 0.0 {
                continue;
            }

            let weight_column = weights.as_ptr().add(column * output);
            let mut output_offset = 0;
            while output_offset + 16 <= output {
                let weights = [
                    vld1q_f32(weight_column.add(output_offset)),
                    vld1q_f32(weight_column.add(output_offset + 4)),
                    vld1q_f32(weight_column.add(output_offset + 8)),
                    vld1q_f32(weight_column.add(output_offset + 12)),
                ];
                if scale0 != 0.0 {
                    add_scaled_16_neon(
                        outs.as_mut_ptr().add(row_base * output + output_offset),
                        weights,
                        scale0,
                    );
                }
                if scale1 != 0.0 {
                    add_scaled_16_neon(
                        outs.as_mut_ptr()
                            .add((row_base + 1) * output + output_offset),
                        weights,
                        scale1,
                    );
                }
                if scale2 != 0.0 {
                    add_scaled_16_neon(
                        outs.as_mut_ptr()
                            .add((row_base + 2) * output + output_offset),
                        weights,
                        scale2,
                    );
                }
                if scale3 != 0.0 {
                    add_scaled_16_neon(
                        outs.as_mut_ptr()
                            .add((row_base + 3) * output + output_offset),
                        weights,
                        scale3,
                    );
                }
                output_offset += 16;
            }
            while output_offset + 4 <= output {
                let weight = vld1q_f32(weight_column.add(output_offset));
                if scale0 != 0.0 {
                    add_scaled_4_neon(
                        outs.as_mut_ptr().add(row_base * output + output_offset),
                        weight,
                        scale0,
                    );
                }
                if scale1 != 0.0 {
                    add_scaled_4_neon(
                        outs.as_mut_ptr()
                            .add((row_base + 1) * output + output_offset),
                        weight,
                        scale1,
                    );
                }
                if scale2 != 0.0 {
                    add_scaled_4_neon(
                        outs.as_mut_ptr()
                            .add((row_base + 2) * output + output_offset),
                        weight,
                        scale2,
                    );
                }
                if scale3 != 0.0 {
                    add_scaled_4_neon(
                        outs.as_mut_ptr()
                            .add((row_base + 3) * output + output_offset),
                        weight,
                        scale3,
                    );
                }
                output_offset += 4;
            }
            while output_offset < output {
                let weight = *weight_column.add(output_offset);
                if scale0 != 0.0 {
                    *outs.get_unchecked_mut(row_base * output + output_offset) += weight * scale0;
                }
                if scale1 != 0.0 {
                    *outs.get_unchecked_mut((row_base + 1) * output + output_offset) +=
                        weight * scale1;
                }
                if scale2 != 0.0 {
                    *outs.get_unchecked_mut((row_base + 2) * output + output_offset) +=
                        weight * scale2;
                }
                if scale3 != 0.0 {
                    *outs.get_unchecked_mut((row_base + 3) * output + output_offset) +=
                        weight * scale3;
                }
                output_offset += 1;
            }
        }
    }

    for row in tiled_rows..rows {
        let out = &mut outs[row * output..(row + 1) * output];
        for column in 0..input {
            let scale = *inputs.get_unchecked(row * input + column);
            if scale == 0.0 {
                continue;
            }
            let weight_column = &weights[column * output..(column + 1) * output];
            add_scaled_in_place_neon(out, weight_column, scale);
        }
    }
}

#[cfg(target_arch = "aarch64")]
#[inline(always)]
unsafe fn add_scaled_16_neon(
    out: *mut f32,
    weights: [std::arch::aarch64::float32x4_t; 4],
    scale: f32,
) {
    use std::arch::aarch64::*;

    let scale = vdupq_n_f32(scale);
    vst1q_f32(out, vfmaq_f32(vld1q_f32(out), weights[0], scale));
    vst1q_f32(
        out.add(4),
        vfmaq_f32(vld1q_f32(out.add(4)), weights[1], scale),
    );
    vst1q_f32(
        out.add(8),
        vfmaq_f32(vld1q_f32(out.add(8)), weights[2], scale),
    );
    vst1q_f32(
        out.add(12),
        vfmaq_f32(vld1q_f32(out.add(12)), weights[3], scale),
    );
}

#[cfg(target_arch = "aarch64")]
#[inline(always)]
unsafe fn add_scaled_4_neon(out: *mut f32, weights: std::arch::aarch64::float32x4_t, scale: f32) {
    use std::arch::aarch64::*;

    vst1q_f32(out, vfmaq_f32(vld1q_f32(out), weights, vdupq_n_f32(scale)));
}

/// Whether the AVX2/FMA kernels run. Detected at run time, so one binary
/// serves CPUs with and without AVX2/FMA; the scalar code is the fallback.
///
/// Tests run the scalar code unless a test opts in with
/// `x86_simd_test_switch::with_simd`, so the scalar path stays the reference
/// that the bit-exact tests pin. The SIMD kernels have their own tests
/// against the scalar ones.
#[cfg(target_arch = "x86_64")]
#[inline(always)]
fn x86_avx2_fma_available() -> bool {
    #[cfg(test)]
    if !x86_simd_test_switch::enabled() {
        return false;
    }
    x86_avx2_fma_detected()
}

#[cfg(all(test, target_arch = "x86_64"))]
mod x86_simd_test_switch {
    use std::cell::Cell;

    thread_local! {
        static ENABLED: Cell<bool> = const { Cell::new(false) };
    }

    pub(super) fn enabled() -> bool {
        ENABLED.with(Cell::get)
    }

    /// Runs `run` with the AVX2/FMA kernels switched on, on a dedicated rayon
    /// pool of `threads` threads whose workers all have the switch on. Returns
    /// `None` when the CPU has no AVX2/FMA.
    pub(super) fn with_simd<R: Send>(threads: usize, run: impl FnOnce() -> R + Send) -> Option<R> {
        if !super::x86_avx2_fma_detected() {
            return None;
        }
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(threads)
            .start_handler(|_| ENABLED.with(|enabled| enabled.set(true)))
            .build()
            .expect("rayon pool");
        Some(pool.install(run))
    }
}

#[cfg(target_arch = "x86_64")]
#[inline(always)]
fn x86_avx2_fma_detected() -> bool {
    use std::sync::atomic::AtomicU8;
    use std::sync::atomic::Ordering;

    static AVAILABLE: AtomicU8 = AtomicU8::new(0);

    match AVAILABLE.load(Ordering::Relaxed) {
        1 => false,
        2 => true,
        _ => {
            let available =
                std::is_x86_feature_detected!("avx2") && std::is_x86_feature_detected!("fma");
            AVAILABLE.store(if available { 2 } else { 1 }, Ordering::Relaxed);
            available
        }
    }
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2,fma")]
unsafe fn add_scaled_in_place_avx2_fma(out: &mut [f32], weights: &[f32], scale: f32) {
    use std::arch::x86_64::*;

    let mut offset = 0;
    let len = out.len();
    let scale_vector = _mm256_set1_ps(scale);
    while offset + 32 <= len {
        let out_ptr = out.as_mut_ptr().add(offset);
        let weight_ptr = weights.as_ptr().add(offset);
        _mm256_storeu_ps(
            out_ptr,
            _mm256_fmadd_ps(
                _mm256_loadu_ps(weight_ptr),
                scale_vector,
                _mm256_loadu_ps(out_ptr),
            ),
        );
        _mm256_storeu_ps(
            out_ptr.add(8),
            _mm256_fmadd_ps(
                _mm256_loadu_ps(weight_ptr.add(8)),
                scale_vector,
                _mm256_loadu_ps(out_ptr.add(8)),
            ),
        );
        _mm256_storeu_ps(
            out_ptr.add(16),
            _mm256_fmadd_ps(
                _mm256_loadu_ps(weight_ptr.add(16)),
                scale_vector,
                _mm256_loadu_ps(out_ptr.add(16)),
            ),
        );
        _mm256_storeu_ps(
            out_ptr.add(24),
            _mm256_fmadd_ps(
                _mm256_loadu_ps(weight_ptr.add(24)),
                scale_vector,
                _mm256_loadu_ps(out_ptr.add(24)),
            ),
        );
        offset += 32;
    }
    while offset + 8 <= len {
        let out_ptr = out.as_mut_ptr().add(offset);
        let weight_ptr = weights.as_ptr().add(offset);
        _mm256_storeu_ps(
            out_ptr,
            _mm256_fmadd_ps(
                _mm256_loadu_ps(weight_ptr),
                scale_vector,
                _mm256_loadu_ps(out_ptr),
            ),
        );
        offset += 8;
    }
    while offset < len {
        out[offset] += weights[offset] * scale;
        offset += 1;
    }
}

/// Four-row, sixteen-output linear projection microkernel for AVX2/FMA.
///
/// A tile of four rows by sixteen outputs keeps eight accumulators in
/// registers, so each column costs two weight loads for eight FMAs. Each
/// output still sums its columns in ascending order with one FMA per column.
/// A column is skipped only when all four rows have a zero scale; otherwise
/// every row takes the FMA, also with a zero scale, because a branch per row
/// mispredicts on the sparse `relu(x)^2` inputs of the channel mixer. Adding
/// `weight * 0.0` leaves a finite sum unchanged, except that it can turn a
/// `-0.0` into `+0.0`, so the result equals `Linear::apply_into()`'s.
#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2,fma")]
unsafe fn linear_apply_block_avx2_fma(
    inputs: &[f32],
    weights: &[f32],
    outs: &mut [f32],
    rows: usize,
    input: usize,
    output: usize,
) {
    use std::arch::x86_64::*;

    debug_assert_eq!(inputs.len(), rows * input);
    debug_assert_eq!(weights.len(), input * output);
    debug_assert_eq!(outs.len(), rows * output);
    let weights_ptr = weights.as_ptr();
    let tiled_rows = rows / 4 * 4;
    for row_base in (0..tiled_rows).step_by(4) {
        let scales = [
            inputs.as_ptr().add(row_base * input),
            inputs.as_ptr().add((row_base + 1) * input),
            inputs.as_ptr().add((row_base + 2) * input),
            inputs.as_ptr().add((row_base + 3) * input),
        ];
        let out = [
            outs.as_mut_ptr().add(row_base * output),
            outs.as_mut_ptr().add((row_base + 1) * output),
            outs.as_mut_ptr().add((row_base + 2) * output),
            outs.as_mut_ptr().add((row_base + 3) * output),
        ];
        let all_zero = |column: usize| {
            ((*scales[0].add(column)).to_bits()
                | (*scales[1].add(column)).to_bits()
                | (*scales[2].add(column)).to_bits()
                | (*scales[3].add(column)).to_bits())
                << 1
                == 0
        };

        let mut offset = 0;
        while offset + 16 <= output {
            let mut acc = [[_mm256_setzero_ps(); 2]; 4];
            for row in 0..4 {
                acc[row][0] = _mm256_loadu_ps(out[row].add(offset));
                acc[row][1] = _mm256_loadu_ps(out[row].add(offset + 8));
            }
            for column in 0..input {
                if all_zero(column) {
                    continue;
                }
                let weight = weights_ptr.add(column * output + offset);
                let weight0 = _mm256_loadu_ps(weight);
                let weight1 = _mm256_loadu_ps(weight.add(8));
                for row in 0..4 {
                    let scale = _mm256_broadcast_ss(&*scales[row].add(column));
                    acc[row][0] = _mm256_fmadd_ps(weight0, scale, acc[row][0]);
                    acc[row][1] = _mm256_fmadd_ps(weight1, scale, acc[row][1]);
                }
            }
            for row in 0..4 {
                _mm256_storeu_ps(out[row].add(offset), acc[row][0]);
                _mm256_storeu_ps(out[row].add(offset + 8), acc[row][1]);
            }
            offset += 16;
        }
        if offset + 8 <= output {
            let mut acc = [_mm256_setzero_ps(); 4];
            for row in 0..4 {
                acc[row] = _mm256_loadu_ps(out[row].add(offset));
            }
            for column in 0..input {
                if all_zero(column) {
                    continue;
                }
                let weight = _mm256_loadu_ps(weights_ptr.add(column * output + offset));
                for row in 0..4 {
                    let scale = _mm256_broadcast_ss(&*scales[row].add(column));
                    acc[row] = _mm256_fmadd_ps(weight, scale, acc[row]);
                }
            }
            for row in 0..4 {
                _mm256_storeu_ps(out[row].add(offset), acc[row]);
            }
            offset += 8;
        }
        while offset < output {
            for row in 0..4 {
                let mut acc = *out[row].add(offset);
                for column in 0..input {
                    if all_zero(column) {
                        continue;
                    }
                    // Not fused, like the tail of `add_scaled_in_place_avx2_fma`.
                    acc += *weights_ptr.add(column * output + offset) * *scales[row].add(column);
                }
                *out[row].add(offset) = acc;
            }
            offset += 1;
        }
    }

    for row in tiled_rows..rows {
        let out = &mut outs[row * output..(row + 1) * output];
        for column in 0..input {
            let scale = *inputs.get_unchecked(row * input + column);
            if scale == 0.0 {
                continue;
            }
            let weight_column = &weights[column * output..(column + 1) * output];
            add_scaled_in_place_avx2_fma(out, weight_column, scale);
        }
    }
}

#[cfg(not(target_arch = "aarch64"))]
#[inline(always)]
fn add_scaled_in_place_scalar(out: &mut [f32], weights: &[f32], scale: f32) {
    for (output, weight) in out.iter_mut().zip(weights) {
        *output += weight * scale;
    }
}

#[inline(always)]
fn dot_product(left: &[f32], right: &[f32]) -> f32 {
    debug_assert_eq!(left.len(), right.len());
    dot_product_arch(left, right)
}

#[cfg(target_arch = "aarch64")]
#[inline(always)]
fn dot_product_arch(left: &[f32], right: &[f32]) -> f32 {
    // SAFETY: aarch64 guarantees NEON support, and the helper only uses
    // unaligned loads within the bounds checked by its loop conditions.
    unsafe { dot_product_neon(left, right) }
}

#[cfg(target_arch = "x86_64")]
#[inline(always)]
fn dot_product_arch(left: &[f32], right: &[f32]) -> f32 {
    if x86_avx2_fma_available() {
        // SAFETY: availability was checked above, and the helper only uses
        // unaligned loads within the bounds checked by its loops.
        unsafe { dot_product_avx2_fma(left, right) }
    } else {
        dot_product_scalar(left, right)
    }
}

#[cfg(all(not(target_arch = "aarch64"), not(target_arch = "x86_64")))]
#[inline(always)]
fn dot_product_arch(left: &[f32], right: &[f32]) -> f32 {
    dot_product_scalar(left, right)
}

#[cfg(target_arch = "aarch64")]
#[inline(always)]
unsafe fn dot_product_neon(left: &[f32], right: &[f32]) -> f32 {
    use std::arch::aarch64::*;

    let mut offset = 0;
    let len = left.len();
    let mut acc0 = vdupq_n_f32(0.0);
    let mut acc1 = vdupq_n_f32(0.0);
    let mut acc2 = vdupq_n_f32(0.0);
    let mut acc3 = vdupq_n_f32(0.0);

    while offset + 16 <= len {
        let left_ptr = left.as_ptr().add(offset);
        let right_ptr = right.as_ptr().add(offset);
        acc0 = vfmaq_f32(acc0, vld1q_f32(left_ptr), vld1q_f32(right_ptr));
        acc1 = vfmaq_f32(
            acc1,
            vld1q_f32(left_ptr.add(4)),
            vld1q_f32(right_ptr.add(4)),
        );
        acc2 = vfmaq_f32(
            acc2,
            vld1q_f32(left_ptr.add(8)),
            vld1q_f32(right_ptr.add(8)),
        );
        acc3 = vfmaq_f32(
            acc3,
            vld1q_f32(left_ptr.add(12)),
            vld1q_f32(right_ptr.add(12)),
        );
        offset += 16;
    }

    let mut acc = vaddq_f32(vaddq_f32(acc0, acc1), vaddq_f32(acc2, acc3));
    while offset + 4 <= len {
        acc = vfmaq_f32(
            acc,
            vld1q_f32(left.as_ptr().add(offset)),
            vld1q_f32(right.as_ptr().add(offset)),
        );
        offset += 4;
    }

    let mut sum = vaddvq_f32(acc);
    while offset < len {
        sum += left[offset] * right[offset];
        offset += 1;
    }
    sum
}

#[cfg(target_arch = "x86_64")]
#[target_feature(enable = "avx2,fma")]
unsafe fn dot_product_avx2_fma(left: &[f32], right: &[f32]) -> f32 {
    use std::arch::x86_64::*;

    let mut offset = 0;
    let len = left.len();
    let mut acc0 = _mm256_setzero_ps();
    let mut acc1 = _mm256_setzero_ps();
    let mut acc2 = _mm256_setzero_ps();
    let mut acc3 = _mm256_setzero_ps();

    while offset + 32 <= len {
        let left_ptr = left.as_ptr().add(offset);
        let right_ptr = right.as_ptr().add(offset);
        acc0 = _mm256_fmadd_ps(_mm256_loadu_ps(left_ptr), _mm256_loadu_ps(right_ptr), acc0);
        acc1 = _mm256_fmadd_ps(
            _mm256_loadu_ps(left_ptr.add(8)),
            _mm256_loadu_ps(right_ptr.add(8)),
            acc1,
        );
        acc2 = _mm256_fmadd_ps(
            _mm256_loadu_ps(left_ptr.add(16)),
            _mm256_loadu_ps(right_ptr.add(16)),
            acc2,
        );
        acc3 = _mm256_fmadd_ps(
            _mm256_loadu_ps(left_ptr.add(24)),
            _mm256_loadu_ps(right_ptr.add(24)),
            acc3,
        );
        offset += 32;
    }

    let mut acc = _mm256_add_ps(_mm256_add_ps(acc0, acc1), _mm256_add_ps(acc2, acc3));
    while offset + 8 <= len {
        acc = _mm256_fmadd_ps(
            _mm256_loadu_ps(left.as_ptr().add(offset)),
            _mm256_loadu_ps(right.as_ptr().add(offset)),
            acc,
        );
        offset += 8;
    }

    let mut lanes = [0.0_f32; 8];
    _mm256_storeu_ps(lanes.as_mut_ptr(), acc);
    let mut sum = lanes.iter().copied().sum::<f32>();
    while offset < len {
        sum += left[offset] * right[offset];
        offset += 1;
    }
    sum
}

#[cfg(not(target_arch = "aarch64"))]
#[inline(always)]
fn dot_product_scalar(left: &[f32], right: &[f32]) -> f32 {
    left.iter()
        .zip(right)
        .map(|(left, right)| left * right)
        .sum()
}

struct Norm {
    groups: usize,
    dim: usize,
    eps: f32,
    weight: Vec<f32>,
    bias: Vec<f32>,
}

impl Norm {
    fn apply(&self, input: &[f32]) -> Vec<f32> {
        let mut out = vec![0.0; self.dim];
        self.apply_into(input, &mut out);
        out
    }

    fn apply_into(&self, input: &[f32], out: &mut [f32]) {
        #[cfg(test)]
        let profile_started = rwkv_warmup_profile_start();
        debug_assert_eq!(input.len(), self.dim);
        debug_assert_eq!(out.len(), self.dim);
        let group_size = self.dim / self.groups;

        for group in 0..self.groups {
            let start = group * group_size;
            let end = start + group_size;
            let values = &input[start..end];
            let mean = values.iter().sum::<f32>() / group_size as f32;
            let variance = values
                .iter()
                .map(|value| {
                    let diff = value - mean;
                    diff * diff
                })
                .sum::<f32>()
                / group_size as f32;
            let scale = (variance + self.eps).sqrt().recip();
            for index in start..end {
                out[index] = (input[index] - mean) * scale * self.weight[index] + self.bias[index];
            }
        }

        #[cfg(test)]
        rwkv_warmup_profile_record(RwkvWarmupProfileBucket::Norm, profile_started);
    }

    fn apply_batch(&self, input: &[f32], rows: usize, out: &mut Vec<f32>) {
        debug_assert_eq!(input.len(), rows * self.dim);
        out.resize(rows * self.dim, 0.0);
        // Accumulate independent rows together, keeping each row's reduction
        // order unchanged so SIMD does not change normalization results.
        const LANES: usize = 4;
        let group_size = self.dim / self.groups;
        let mut inputs = input.chunks_exact(self.dim * LANES);
        let mut outputs = out.chunks_exact_mut(self.dim * LANES);
        for (input, output) in inputs.by_ref().zip(outputs.by_ref()) {
            for group in 0..self.groups {
                let start = group * group_size;
                let end = start + group_size;
                let mut means = [-0.0; LANES];
                for channel in start..end {
                    for lane in 0..LANES {
                        means[lane] += input[lane * self.dim + channel];
                    }
                }
                for mean in &mut means {
                    *mean /= group_size as f32;
                }
                let mut scales = [-0.0; LANES];
                for channel in start..end {
                    for lane in 0..LANES {
                        let diff = input[lane * self.dim + channel] - means[lane];
                        scales[lane] += diff * diff;
                    }
                }
                for scale in &mut scales {
                    *scale = (*scale / group_size as f32 + self.eps).sqrt().recip();
                }
                for lane in 0..LANES {
                    for channel in start..end {
                        let index = lane * self.dim + channel;
                        output[index] =
                            (input[index] - means[lane]) * scales[lane] * self.weight[channel]
                                + self.bias[channel];
                    }
                }
            }
        }
        outputs
            .into_remainder()
            .chunks_mut(self.dim)
            .zip(inputs.remainder().chunks(self.dim))
            .for_each(|(output, input)| self.apply_into(input, output));
    }
}

fn single_timestep(
    r: &[f32],
    k: &[f32],
    v: &[f32],
    w: &[f32],
    a: &[f32],
    k_deformed: &[f32],
    state: &[f32],
) -> (Vec<f32>, Vec<f32>) {
    let mut next_state = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];
    let mut out = vec![0.0; D_MODEL];
    single_timestep_into(r, k, v, w, a, k_deformed, state, &mut out, &mut next_state);
    (out, next_state)
}

#[allow(clippy::too_many_arguments)]
fn single_timestep_into(
    r: &[f32],
    k: &[f32],
    v: &[f32],
    w: &[f32],
    a: &[f32],
    k_deformed: &[f32],
    state: &[f32],
    out: &mut [f32],
    next_state: &mut [f32],
) {
    #[cfg(test)]
    let profile_started =
        if RWKV_SINGLE_TIMESTEP_PROFILE_ENABLED.load(std::sync::atomic::Ordering::Relaxed) {
            Some(std::time::Instant::now())
        } else {
            None
        };
    #[cfg(test)]
    let warmup_profile_started = rwkv_warmup_profile_start();

    debug_assert_eq!(state.len(), HEADS * HEAD_SIZE * HEAD_SIZE);
    debug_assert_eq!(next_state.len(), HEADS * HEAD_SIZE * HEAD_SIZE);
    debug_assert_eq!(out.len(), D_MODEL);

    for head in 0..HEADS {
        let head_base = head * HEAD_SIZE;
        let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
        let mut state_dot_k = [0.0_f32; HEAD_SIZE];
        let key_deformed = &k_deformed[head_base..head_base + HEAD_SIZE];
        let receptance = &r[head_base..head_base + HEAD_SIZE];

        for (row, value) in state_dot_k.iter_mut().enumerate() {
            let row_start = matrix_base + row * HEAD_SIZE;
            let state_row = &state[row_start..row_start + HEAD_SIZE];
            *value = dot_product(state_row, key_deformed);
        }

        update_recurrence_head(
            &k[head_base..head_base + HEAD_SIZE],
            &v[head_base..head_base + HEAD_SIZE],
            &w[head_base..head_base + HEAD_SIZE],
            &a[head_base..head_base + HEAD_SIZE],
            key_deformed,
            &state[matrix_base..matrix_base + HEAD_SIZE * HEAD_SIZE],
            &state_dot_k,
            &mut next_state[matrix_base..matrix_base + HEAD_SIZE * HEAD_SIZE],
        );

        for row in 0..HEAD_SIZE {
            let row_start = matrix_base + row * HEAD_SIZE;
            let state_row = &next_state[row_start..row_start + HEAD_SIZE];
            out[head_base + row] = dot_product(state_row, receptance);
        }
    }

    #[cfg(test)]
    if let Some(started) = profile_started {
        RWKV_SINGLE_TIMESTEP_PROFILE_CALLS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        RWKV_SINGLE_TIMESTEP_PROFILE_NANOS.fetch_add(
            started.elapsed().as_nanos().min(u128::from(u64::MAX)) as u64,
            std::sync::atomic::Ordering::Relaxed,
        );
    }
    #[cfg(test)]
    rwkv_warmup_profile_record(
        RwkvWarmupProfileBucket::SingleTimestep,
        warmup_profile_started,
    );
}

#[allow(clippy::too_many_arguments)]
#[inline(always)]
fn update_recurrence_head(
    k: &[f32],
    v: &[f32],
    w: &[f32],
    a: &[f32],
    key_deformed: &[f32],
    state: &[f32],
    state_dot_k: &[f32; HEAD_SIZE],
    next_state: &mut [f32],
) {
    #[cfg(target_arch = "x86_64")]
    if x86_avx2_fma_available() {
        // SAFETY: availability was checked above. Every head is exactly
        // `HEAD_SIZE` square and `HEAD_SIZE` is a multiple of eight.
        unsafe {
            update_recurrence_head_avx2(k, v, w, a, key_deformed, state, state_dot_k, next_state);
        }
        return;
    }

    update_recurrence_head_scalar(k, v, w, a, key_deformed, state, state_dot_k, next_state);
}

#[allow(clippy::too_many_arguments)]
fn update_recurrence_head_scalar(
    k: &[f32],
    v: &[f32],
    w: &[f32],
    a: &[f32],
    key_deformed: &[f32],
    state: &[f32],
    state_dot_k: &[f32; HEAD_SIZE],
    next_state: &mut [f32],
) {
    for row in 0..HEAD_SIZE {
        for column in 0..HEAD_SIZE {
            let old = state[row * HEAD_SIZE + column];
            next_state[row * HEAD_SIZE + column] = old * w[column]
                - state_dot_k[row] * a[column] * key_deformed[column]
                + v[row] * k[column];
        }
    }
}

#[cfg(target_arch = "x86_64")]
#[allow(clippy::too_many_arguments)]
#[target_feature(enable = "avx2")]
unsafe fn update_recurrence_head_avx2(
    k: &[f32],
    v: &[f32],
    w: &[f32],
    a: &[f32],
    key_deformed: &[f32],
    state: &[f32],
    state_dot_k: &[f32; HEAD_SIZE],
    next_state: &mut [f32],
) {
    use std::arch::x86_64::*;

    debug_assert_eq!(k.len(), HEAD_SIZE);
    debug_assert_eq!(v.len(), HEAD_SIZE);
    debug_assert_eq!(w.len(), HEAD_SIZE);
    debug_assert_eq!(a.len(), HEAD_SIZE);
    debug_assert_eq!(key_deformed.len(), HEAD_SIZE);
    debug_assert_eq!(state.len(), HEAD_SIZE * HEAD_SIZE);
    debug_assert_eq!(next_state.len(), HEAD_SIZE * HEAD_SIZE);

    for row in 0..HEAD_SIZE {
        let state_dot = _mm256_set1_ps(state_dot_k[row]);
        let value = _mm256_set1_ps(v[row]);
        let row_base = row * HEAD_SIZE;
        for column in (0..HEAD_SIZE).step_by(8) {
            let old = _mm256_loadu_ps(state.as_ptr().add(row_base + column));
            let weighted = _mm256_mul_ps(old, _mm256_loadu_ps(w.as_ptr().add(column)));
            let penalty = _mm256_mul_ps(
                _mm256_mul_ps(state_dot, _mm256_loadu_ps(a.as_ptr().add(column))),
                _mm256_loadu_ps(key_deformed.as_ptr().add(column)),
            );
            let injected = _mm256_mul_ps(value, _mm256_loadu_ps(k.as_ptr().add(column)));
            let updated = _mm256_add_ps(_mm256_sub_ps(weighted, penalty), injected);
            _mm256_storeu_ps(next_state.as_mut_ptr().add(row_base + column), updated);
        }
    }
}

#[cfg(test)]
#[allow(clippy::too_many_arguments)]
fn single_timestep_query_into(
    r: &[f32],
    k: &[f32],
    v: &[f32],
    w: &[f32],
    a: &[f32],
    k_deformed: &[f32],
    state: Option<&[f32]>,
    out: &mut [f32],
    next_row: &mut [f32],
) {
    debug_assert_eq!(out.len(), D_MODEL);
    debug_assert_eq!(next_row.len(), HEAD_SIZE);
    debug_assert!(
        state.is_none() || state.is_some_and(|state| state.len() == HEADS * HEAD_SIZE * HEAD_SIZE)
    );

    for head in 0..HEADS {
        let head_base = head * HEAD_SIZE;
        let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
        let mut state_dot_k = [0.0_f32; HEAD_SIZE];
        let key_deformed = &k_deformed[head_base..head_base + HEAD_SIZE];
        let receptance = &r[head_base..head_base + HEAD_SIZE];

        if let Some(state) = state {
            for (row, value) in state_dot_k.iter_mut().enumerate() {
                let row_start = matrix_base + row * HEAD_SIZE;
                *value = dot_product(&state[row_start..row_start + HEAD_SIZE], key_deformed);
            }
        }

        for row in 0..HEAD_SIZE {
            for (column, next) in next_row.iter_mut().enumerate().take(HEAD_SIZE) {
                let channel = head_base + column;
                let matrix_index = matrix_base + row * HEAD_SIZE + column;
                let old = state.map_or(0.0, |state| state[matrix_index]);
                *next = old * w[channel] - state_dot_k[row] * a[channel] * k_deformed[channel]
                    + v[head_base + row] * k[channel];
            }
            out[head_base + row] = dot_product(next_row, receptance);
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn single_timestep_query_fast_into(
    r: &[f32],
    k: &[f32],
    v: &[f32],
    w: &[f32],
    a: &[f32],
    k_deformed: &[f32],
    state: Option<&[f32]>,
    out: &mut [f32],
) {
    debug_assert_eq!(out.len(), D_MODEL);
    debug_assert!(
        state.is_none() || state.is_some_and(|state| state.len() == HEADS * HEAD_SIZE * HEAD_SIZE)
    );

    for head in 0..HEADS {
        let head_base = head * HEAD_SIZE;
        let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
        let receptance = &r[head_base..head_base + HEAD_SIZE];
        let key = &k[head_base..head_base + HEAD_SIZE];
        let key_deformed = &k_deformed[head_base..head_base + HEAD_SIZE];
        let key_receptance = dot_product(key, receptance);

        let Some(state) = state else {
            for row in 0..HEAD_SIZE {
                out[head_base + row] = v[head_base + row] * key_receptance;
            }
            continue;
        };

        let mut decayed_receptance = [0.0; HEAD_SIZE];
        let mut adaptation_receptance = 0.0;
        for column in 0..HEAD_SIZE {
            let channel = head_base + column;
            decayed_receptance[column] = w[channel] * receptance[column];
            adaptation_receptance += a[channel] * key_deformed[column] * receptance[column];
        }

        for row in 0..HEAD_SIZE {
            let row_start = matrix_base + row * HEAD_SIZE;
            let state_row = &state[row_start..row_start + HEAD_SIZE];
            let state_dot_key = dot_product(state_row, key_deformed);
            let retained = dot_product(state_row, &decayed_receptance);
            out[head_base + row] = retained - state_dot_key * adaptation_receptance
                + v[head_base + row] * key_receptance;
        }
    }
}

fn normalize_heads_in_place(values: &mut [f32]) {
    for head in 0..HEADS {
        let start = head * HEAD_SIZE;
        let end = start + HEAD_SIZE;
        let norm = values[start..end]
            .iter()
            .map(|value| value * value)
            .sum::<f32>()
            .sqrt()
            .max(1e-12);
        for value in &mut values[start..end] {
            *value /= norm;
        }
    }
}

fn scale_heads_in_place(values: &mut [f32], scales: &[f32]) {
    debug_assert_eq!(values.len(), D_MODEL);
    debug_assert_eq!(scales.len(), HEADS);
    for head in 0..HEADS {
        for index in 0..HEAD_SIZE {
            values[head * HEAD_SIZE + index] *= scales[head];
        }
    }
}

fn softmax(input: &[f32]) -> Vec<f32> {
    let max = input
        .iter()
        .copied()
        .fold(f32::NEG_INFINITY, |a, b| a.max(b));
    let mut out = input
        .iter()
        .map(|value| (*value - max).exp())
        .collect::<Vec<_>>();
    let sum = out.iter().sum::<f32>();
    for value in &mut out {
        *value /= sum;
    }
    out
}

fn softmax_into(input: &[f32], out: &mut [f32]) {
    debug_assert_eq!(input.len(), out.len());
    let max = input
        .iter()
        .copied()
        .fold(f32::NEG_INFINITY, |a, b| a.max(b));
    for (output, value) in out.iter_mut().zip(input) {
        *output = (*value - max).exp();
    }
    let sum = out.iter().sum::<f32>();
    for value in out {
        *value /= sum;
    }
}

fn sigmoid_in_place(values: &mut [f32]) {
    for value in values {
        *value = sigmoid(*value);
    }
}

fn sigmoid(value: f32) -> f32 {
    if value >= 0.0 {
        1.0 / (1.0 + (-value).exp())
    } else {
        let exp = value.exp();
        exp / (1.0 + exp)
    }
}

fn query_decay(value: f32) -> f32 {
    // exp(-exp(-0.5 - softplus(-x))) = exp(-exp(-0.5) * sigmoid(x)).
    // Query-only: recurrent state updates keep their original arithmetic.
    const SCALE: f32 = 0.606_530_67; // exp(-0.5), rounded to f32
    (-SCALE * sigmoid(value)).exp()
}

fn softplus(value: f32) -> f32 {
    if value > 20.0 {
        value
    } else if value < -20.0 {
        value.exp()
    } else {
        value.exp().ln_1p()
    }
}

fn silu_in_place(values: &mut [f32]) {
    for value in values {
        *value *= sigmoid(*value);
    }
}

fn relu_in_place(values: &mut [f32]) {
    for value in values {
        *value = value.max(0.0);
    }
}

fn lerp(start: f32, end: f32, weight: f32) -> f32 {
    start + weight * (end - start)
}

#[cfg(test)]
#[derive(Clone, Copy)]
struct StateCompression {
    shift_bits: Option<u8>,
    matrix_rank: usize,
    matrix_bits: Option<u8>,
    power_iterations: usize,
}

#[cfg(test)]
fn compressed_srs_state(state: SrsState, compression: StateCompression) -> SrsState {
    SrsState {
        card: compressed_module_state(state.card, compression),
        deck: compressed_module_state(state.deck, compression),
        note: compressed_module_state(state.note, compression),
        preset: compressed_module_state(state.preset, compression),
        global: compressed_module_state(state.global, compression),
    }
}

#[cfg(test)]
fn compressed_module_state(mut state: ModuleState, compression: StateCompression) -> ModuleState {
    for layer in &mut state.layers {
        if let Some(time) = layer.time.as_mut() {
            if let Some(bits) = compression.shift_bits {
                quantize_dequantize_symmetric(&mut time.x_shift, bits);
            }
            time.matrix = low_rank_matrix_state(&time.matrix, compression);
        }
        if let (Some(bits), Some(channel_shift)) =
            (compression.shift_bits, layer.channel_shift.as_mut())
        {
            quantize_dequantize_symmetric(channel_shift, bits);
        }
    }
    state
}

#[cfg(test)]
fn low_rank_matrix_state(matrix: &[f32], compression: StateCompression) -> Vec<f32> {
    debug_assert_eq!(matrix.len(), HEADS * HEAD_SIZE * HEAD_SIZE);
    let mut out = vec![0.0; matrix.len()];
    for head in 0..HEADS {
        let start = head * HEAD_SIZE * HEAD_SIZE;
        let end = start + HEAD_SIZE * HEAD_SIZE;
        low_rank_head_matrix(
            &matrix[start..end],
            &mut out[start..end],
            compression.matrix_rank,
            compression.matrix_bits,
            compression.power_iterations,
        );
    }
    out
}

#[cfg(test)]
fn low_rank_head_matrix(
    matrix: &[f32],
    out: &mut [f32],
    rank: usize,
    bits: Option<u8>,
    power_iterations: usize,
) {
    debug_assert_eq!(matrix.len(), HEAD_SIZE * HEAD_SIZE);
    debug_assert_eq!(out.len(), HEAD_SIZE * HEAD_SIZE);
    if rank == 0 {
        return;
    }

    let mut residual = matrix.to_vec();
    let mut left = vec![0.0; HEAD_SIZE];
    let mut right = vec![0.0; HEAD_SIZE];
    let mut next_left = vec![0.0; HEAD_SIZE];

    for component in 0..rank {
        initialize_power_vector(&residual, &mut left, component);
        for _ in 0..power_iterations {
            multiply_transpose_matrix_vector(&residual, &left, &mut right);
            let right_norm = normalize_vector(&mut right);
            if right_norm <= 1e-12 {
                break;
            }
            multiply_matrix_vector(&residual, &right, &mut next_left);
            let left_norm = normalize_vector(&mut next_left);
            if left_norm <= 1e-12 {
                break;
            }
            left.copy_from_slice(&next_left);
        }

        multiply_transpose_matrix_vector(&residual, &left, &mut right);
        let sigma = normalize_vector(&mut right);
        if sigma <= 1e-12 || !sigma.is_finite() {
            break;
        }

        let root = sigma.sqrt();
        let mut left_factor = left.iter().map(|value| value * root).collect::<Vec<_>>();
        let mut right_factor = right.iter().map(|value| value * root).collect::<Vec<_>>();
        if let Some(bits) = bits {
            quantize_dequantize_symmetric(&mut left_factor, bits);
            quantize_dequantize_symmetric(&mut right_factor, bits);
        }

        for (row, &left) in left_factor.iter().enumerate() {
            for (column, &right) in right_factor.iter().enumerate() {
                let index = row * HEAD_SIZE + column;
                let value = left * right;
                out[index] += value;
                residual[index] -= value;
            }
        }
    }
}

#[cfg(test)]
fn initialize_power_vector(matrix: &[f32], out: &mut [f32], component: usize) {
    for (row, out) in out.iter_mut().enumerate().take(HEAD_SIZE) {
        let row_start = row * HEAD_SIZE;
        let row_sum = matrix[row_start..row_start + HEAD_SIZE]
            .iter()
            .copied()
            .sum::<f32>();
        *out = row_sum + 0.001 * ((row + component + 1) as f32);
    }
    if normalize_vector(out) <= 1e-12 {
        out.fill(0.0);
        out[component % HEAD_SIZE] = 1.0;
    }
}

#[cfg(test)]
fn multiply_matrix_vector(matrix: &[f32], vector: &[f32], out: &mut [f32]) {
    for (row, out) in out.iter_mut().enumerate().take(HEAD_SIZE) {
        let row_start = row * HEAD_SIZE;
        *out = matrix[row_start..row_start + HEAD_SIZE]
            .iter()
            .zip(vector)
            .map(|(left, right)| left * right)
            .sum();
    }
}

#[cfg(test)]
fn multiply_transpose_matrix_vector(matrix: &[f32], vector: &[f32], out: &mut [f32]) {
    out.fill(0.0);
    for (row, &value) in vector.iter().enumerate().take(HEAD_SIZE) {
        let row_start = row * HEAD_SIZE;
        for (column, out) in out.iter_mut().enumerate().take(HEAD_SIZE) {
            *out += matrix[row_start + column] * value;
        }
    }
}

#[cfg(test)]
fn normalize_vector(values: &mut [f32]) -> f32 {
    let norm = values.iter().map(|value| value * value).sum::<f32>().sqrt();
    if norm > 0.0 && norm.is_finite() {
        for value in values {
            *value /= norm;
        }
    }
    norm
}

#[cfg(test)]
fn quantize_dequantize_symmetric(values: &mut [f32], bits: u8) {
    let qmax = (1_i32 << (bits.saturating_sub(1) as u32)) - 1;
    if qmax <= 0 {
        values.fill(0.0);
        return;
    }
    let amax = values.iter().copied().map(f32::abs).fold(0.0, f32::max);
    if amax == 0.0 || !amax.is_finite() {
        values.fill(0.0);
        return;
    }
    let scale = amax / qmax as f32;
    for value in values {
        let quantized = (*value / scale).round().clamp(-(qmax as f32), qmax as f32);
        *value = quantized * scale;
    }
}

#[cfg(test)]
mod tests {

    /// The share of an answer that finding its curve's S90 takes.
    #[test]
    #[ignore]
    fn answer_s90_cost() {
        let weights = embedded_weights_path().unwrap();
        let mut inference = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        let reviews: Vec<ReviewInput> = bulk_parity_reviews(2_000)
            .into_iter()
            .filter(|review| review.ease.is_some())
            .collect();
        let mut answer = std::time::Duration::ZERO;
        let mut s90 = std::time::Duration::ZERO;
        for review in reviews.iter().cloned() {
            let card_id = review.card_id;
            let started = std::time::Instant::now();
            inference
                .review(
                    review,
                    ReviewState {
                        card: None,
                        deck: None,
                        note: None,
                        preset: None,
                        global: None,
                    },
                )
                .unwrap();
            answer += started.elapsed();
            let fresh = ReviewCurve {
                s90: Default::default(),
                ..inference.curves[&card_id].clone()
            };
            let started = std::time::Instant::now();
            std::hint::black_box(inference.stored_curve_s90(&fresh));
            s90 += started.elapsed();
        }
        let n = reviews.len() as f64;
        println!(
            "answers={} answer_ms={:.3} of_which_s90_ms={:.3}",
            reviews.len(),
            answer.as_secs_f64() * 1000.0 / n,
            s90.as_secs_f64() * 1000.0 / n
        );
    }

    /// Cost of finding the S90 of every stored curve of a real runtime
    /// state (RWKV_S90_BENCH_STATE = a runtime_state blob).
    #[test]
    #[ignore]
    fn stored_curve_s90_cost_on_a_real_state() {
        let path = std::env::var("RWKV_S90_BENCH_STATE").unwrap();
        let bytes = std::fs::read(path).unwrap();
        let (_, curves) =
            read_runtime_cache_state(&bytes, 36500, PUBLISHED_MODEL_ID_PIPELINE).unwrap();
        let curves: Vec<&ReviewCurve> = curves.values().collect();
        let started = std::time::Instant::now();
        let serial: Vec<Option<f32>> = curves
            .iter()
            .map(|curve| unrounded_interval_for_curve(curve, S90_TARGET_RETENTION, 36500))
            .collect();
        let serial_ms = started.elapsed().as_secs_f64() * 1000.0;
        let started = std::time::Instant::now();
        let parallel: Vec<Option<f32>> = curves
            .par_iter()
            .map(|curve| {
                *curve.s90.get_or_init(|| {
                    unrounded_interval_for_curve(curve, S90_TARGET_RETENTION, 36500)
                })
            })
            .collect();
        let parallel_ms = started.elapsed().as_secs_f64() * 1000.0;
        assert_eq!(serial, parallel);
        let started = std::time::Instant::now();
        let kept: Vec<Option<f32>> = curves
            .par_iter()
            .map(|curve| *curve.s90.get().unwrap())
            .collect();
        let lookup_ms = started.elapsed().as_secs_f64() * 1000.0;
        assert_eq!(serial, kept);
        println!(
            "curves={} weights={} serial_ms={serial_ms:.1} per_curve_us={:.2} parallel_ms={parallel_ms:.1} lookup_ms={lookup_ms:.2} threads={}",
            curves.len(),
            curves.first().map_or(0, |c| c.weights.len()),
            serial_ms * 1000.0 / curves.len() as f64,
            rayon::current_num_threads()
        );
    }
    use std::env;
    use std::path::PathBuf;
    use std::time::Instant;

    use rusqlite::Connection;

    use super::collection_benchmark::deck_config_ids_by_deck;
    use super::*;

    struct TimeMixerAffineTransform {
        matrix: Vec<f32>,
        bias: Vec<f32>,
    }

    struct TimeMixerStateAffineTransform {
        matrix_by_head: Vec<f32>,
        bias: Vec<f32>,
    }

    fn deterministic_values(len: usize, seed: f32) -> Vec<f32> {
        (0..len)
            .map(|index| {
                let value = index as f32 + 1.0;
                (value * seed).sin() * 0.25 + (value * seed * 0.37).cos() * 0.1
            })
            .collect()
    }

    fn deterministic_step_vectors(step: usize) -> RwkvScanCapturedStep {
        let offset = step as f32 * 0.001;
        RwkvScanCapturedStep {
            r: deterministic_values(D_MODEL, 0.013 + offset),
            k: deterministic_values(D_MODEL, 0.017 + offset),
            v: deterministic_values(D_MODEL, 0.019 + offset),
            w: deterministic_values(D_MODEL, 0.023 + offset),
            a: deterministic_values(D_MODEL, 0.029 + offset),
            k_deformed: deterministic_values(D_MODEL, 0.031 + offset),
        }
    }

    fn time_mixer_transform_for_head_row(
        head: usize,
        row: usize,
        k: &[f32],
        v: &[f32],
        w: &[f32],
        a: &[f32],
        k_deformed: &[f32],
    ) -> TimeMixerAffineTransform {
        let head_base = head * HEAD_SIZE;
        let mut matrix = vec![0.0; HEAD_SIZE * HEAD_SIZE];
        let mut bias = vec![0.0; HEAD_SIZE];

        for input_column in 0..HEAD_SIZE {
            for output_column in 0..HEAD_SIZE {
                let output_channel = head_base + output_column;
                matrix[input_column * HEAD_SIZE + output_column] =
                    if input_column == output_column {
                        w[output_channel]
                    } else {
                        0.0
                    } - k_deformed[head_base + input_column]
                        * a[output_channel]
                        * k_deformed[output_channel];
            }
        }

        for output_column in 0..HEAD_SIZE {
            bias[output_column] = v[head_base + row] * k[head_base + output_column];
        }

        TimeMixerAffineTransform { matrix, bias }
    }

    fn time_mixer_state_transform(
        k: &[f32],
        v: &[f32],
        w: &[f32],
        a: &[f32],
        k_deformed: &[f32],
    ) -> TimeMixerStateAffineTransform {
        let mut matrix_by_head = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];
        let mut bias = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];

        for head in 0..HEADS {
            let head_base = head * HEAD_SIZE;
            let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
            for input_column in 0..HEAD_SIZE {
                for output_column in 0..HEAD_SIZE {
                    let output_channel = head_base + output_column;
                    matrix_by_head[matrix_base + input_column * HEAD_SIZE + output_column] =
                        if input_column == output_column {
                            w[output_channel]
                        } else {
                            0.0
                        } - k_deformed[head_base + input_column]
                            * a[output_channel]
                            * k_deformed[output_channel];
                }
            }

            for row in 0..HEAD_SIZE {
                let row_start = matrix_base + row * HEAD_SIZE;
                for output_column in 0..HEAD_SIZE {
                    bias[row_start + output_column] =
                        v[head_base + row] * k[head_base + output_column];
                }
            }
        }

        TimeMixerStateAffineTransform {
            matrix_by_head,
            bias,
        }
    }

    fn compose_affine_transforms(
        first: &TimeMixerAffineTransform,
        second: &TimeMixerAffineTransform,
    ) -> TimeMixerAffineTransform {
        let mut matrix = vec![0.0; HEAD_SIZE * HEAD_SIZE];
        let mut bias = vec![0.0; HEAD_SIZE];

        for input_column in 0..HEAD_SIZE {
            for output_column in 0..HEAD_SIZE {
                matrix[input_column * HEAD_SIZE + output_column] = (0..HEAD_SIZE)
                    .map(|middle| {
                        first.matrix[input_column * HEAD_SIZE + middle]
                            * second.matrix[middle * HEAD_SIZE + output_column]
                    })
                    .sum();
            }
        }

        for (output_column, value) in bias.iter_mut().enumerate() {
            *value = second.bias[output_column]
                + (0..HEAD_SIZE)
                    .map(|middle| {
                        first.bias[middle] * second.matrix[middle * HEAD_SIZE + output_column]
                    })
                    .sum::<f32>();
        }

        TimeMixerAffineTransform { matrix, bias }
    }

    fn compose_time_mixer_state_transforms(
        first: &TimeMixerStateAffineTransform,
        second: &TimeMixerStateAffineTransform,
    ) -> TimeMixerStateAffineTransform {
        let mut matrix_by_head = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];
        let mut bias = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];

        for head in 0..HEADS {
            let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
            for input_column in 0..HEAD_SIZE {
                for output_column in 0..HEAD_SIZE {
                    matrix_by_head[matrix_base + input_column * HEAD_SIZE + output_column] = (0
                        ..HEAD_SIZE)
                        .map(|middle| {
                            first.matrix_by_head[matrix_base + input_column * HEAD_SIZE + middle]
                                * second.matrix_by_head
                                    [matrix_base + middle * HEAD_SIZE + output_column]
                        })
                        .sum();
                }
            }

            for row in 0..HEAD_SIZE {
                let row_start = matrix_base + row * HEAD_SIZE;
                for output_column in 0..HEAD_SIZE {
                    bias[row_start + output_column] = second.bias[row_start + output_column]
                        + (0..HEAD_SIZE)
                            .map(|middle| {
                                first.bias[row_start + middle]
                                    * second.matrix_by_head
                                        [matrix_base + middle * HEAD_SIZE + output_column]
                            })
                            .sum::<f32>();
                }
            }
        }

        TimeMixerStateAffineTransform {
            matrix_by_head,
            bias,
        }
    }

    fn compose_time_mixer_step_range(
        steps: &[RwkvScanCapturedStep],
        start: usize,
        end: usize,
    ) -> TimeMixerStateAffineTransform {
        assert!(start < end);
        let first = &steps[start];
        let mut transform =
            time_mixer_state_transform(&first.k, &first.v, &first.w, &first.a, &first.k_deformed);
        for step in &steps[start + 1..end] {
            let next =
                time_mixer_state_transform(&step.k, &step.v, &step.w, &step.a, &step.k_deformed);
            transform = compose_time_mixer_state_transforms(&transform, &next);
        }

        transform
    }

    fn chunked_time_mixer_state_transform(
        steps: &[RwkvScanCapturedStep],
        chunk_size: usize,
    ) -> TimeMixerStateAffineTransform {
        assert!(!steps.is_empty());
        assert!(chunk_size > 0);
        let ranges = (0..steps.len())
            .step_by(chunk_size)
            .map(|start| {
                let end = (start + chunk_size).min(steps.len());
                (start, end)
            })
            .collect::<Vec<_>>();
        let chunk_transforms = std::thread::scope(|scope| {
            let handles = ranges
                .iter()
                .map(|&(start, end)| {
                    scope.spawn(move || compose_time_mixer_step_range(steps, start, end))
                })
                .collect::<Vec<_>>();

            handles
                .into_iter()
                .map(|handle| handle.join().unwrap())
                .collect::<Vec<_>>()
        });

        let mut transforms = chunk_transforms.into_iter();
        let mut transform = transforms.next().unwrap();
        for next in transforms {
            transform = compose_time_mixer_state_transforms(&transform, &next);
        }

        transform
    }

    fn apply_affine_transform(row_state: &[f32], transform: &TimeMixerAffineTransform) -> Vec<f32> {
        (0..HEAD_SIZE)
            .map(|output_column| {
                transform.bias[output_column]
                    + (0..HEAD_SIZE)
                        .map(|input_column| {
                            row_state[input_column]
                                * transform.matrix[input_column * HEAD_SIZE + output_column]
                        })
                        .sum::<f32>()
            })
            .collect()
    }

    fn apply_time_mixer_state_transform(
        state: &[f32],
        transform: &TimeMixerStateAffineTransform,
    ) -> Vec<f32> {
        let mut output = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];

        for head in 0..HEADS {
            let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
            for row in 0..HEAD_SIZE {
                let row_start = matrix_base + row * HEAD_SIZE;
                for output_column in 0..HEAD_SIZE {
                    output[row_start + output_column] = transform.bias[row_start + output_column]
                        + (0..HEAD_SIZE)
                            .map(|input_column| {
                                state[row_start + input_column]
                                    * transform.matrix_by_head
                                        [matrix_base + input_column * HEAD_SIZE + output_column]
                            })
                            .sum::<f32>();
                }
            }
        }

        output
    }

    fn assert_close(left: &[f32], right: &[f32], tolerance: f32) {
        assert_eq!(left.len(), right.len());
        for (index, (left, right)) in left.iter().zip(right).enumerate() {
            let diff = (left - right).abs();
            assert!(
                diff <= tolerance,
                "values differ at {index}: left={left}, right={right}, diff={diff}, tolerance={tolerance}"
            );
        }
    }

    #[cfg(any(target_arch = "aarch64", target_arch = "x86_64"))]
    fn deterministic_simd_values(len: usize, period: usize, scale: f32) -> Vec<f32> {
        (0..len)
            .map(|index| {
                let centered = index as i32 % period as i32 - period as i32 / 2;
                centered as f32 * scale
            })
            .collect()
    }

    #[cfg(target_arch = "x86_64")]
    #[test]
    fn add_scaled_in_place_avx2_fma_matches_scalar() {
        if !x86_avx2_fma_detected() {
            eprintln!("skipping: AVX2/FMA not available");
            return;
        }

        let mut scalar = deterministic_simd_values(101, 17, 0.03125);
        let mut simd = scalar.clone();
        let weights = deterministic_simd_values(101, 23, -0.015625);

        add_scaled_in_place_scalar(&mut scalar, &weights, 0.75);
        // SAFETY: the runtime feature check above confirms AVX2/FMA support.
        unsafe { add_scaled_in_place_avx2_fma(&mut simd, &weights, 0.75) };

        assert_close(&simd, &scalar, 1e-6);
    }

    #[cfg(target_arch = "x86_64")]
    #[test]
    fn dot_product_avx2_fma_matches_scalar() {
        if !x86_avx2_fma_detected() {
            eprintln!("skipping: AVX2/FMA not available");
            return;
        }

        let left = deterministic_simd_values(101, 19, 0.046875);
        let right = deterministic_simd_values(101, 29, -0.0234375);

        let scalar = dot_product_scalar(&left, &right);
        // SAFETY: the runtime feature check above confirms AVX2/FMA support.
        let simd = unsafe { dot_product_avx2_fma(&left, &right) };

        assert!(
            (simd - scalar).abs() <= 1e-5,
            "simd={simd}, scalar={scalar}, diff={}",
            (simd - scalar).abs()
        );
    }

    #[cfg(target_arch = "x86_64")]
    #[test]
    fn update_recurrence_head_avx2_matches_scalar_bit_exactly() {
        if !x86_avx2_fma_detected() {
            eprintln!("skipping: AVX2/FMA not available");
            return;
        }

        let k = deterministic_simd_values(HEAD_SIZE, 11, 0.0078125);
        let v = deterministic_simd_values(HEAD_SIZE, 13, -0.015625);
        let w = deterministic_simd_values(HEAD_SIZE, 17, 0.9921875);
        let a = deterministic_simd_values(HEAD_SIZE, 19, -0.00390625);
        let key_deformed = deterministic_simd_values(HEAD_SIZE, 23, 0.01171875);
        let state = deterministic_simd_values(HEAD_SIZE * HEAD_SIZE, 29, -0.001953125);
        let state_dot_k_values = deterministic_simd_values(HEAD_SIZE, 31, 0.005859375);
        let state_dot_k: [f32; HEAD_SIZE] = state_dot_k_values.try_into().unwrap();
        let mut expected = vec![0.0; HEAD_SIZE * HEAD_SIZE];
        let mut actual = vec![0.0; HEAD_SIZE * HEAD_SIZE];

        update_recurrence_head_scalar(
            &k,
            &v,
            &w,
            &a,
            &key_deformed,
            &state,
            &state_dot_k,
            &mut expected,
        );
        // SAFETY: the runtime feature check above confirms AVX2 support.
        unsafe {
            update_recurrence_head_avx2(
                &k,
                &v,
                &w,
                &a,
                &key_deformed,
                &state,
                &state_dot_k,
                &mut actual,
            );
        }

        assert_eq!(actual, expected);
    }

    #[cfg(any(target_arch = "aarch64", target_arch = "x86_64"))]
    #[test]
    fn linear_apply_block_arch_matches_per_row_path_bit_exactly() {
        let input = 13;
        let output = 19;
        let rows = 7;
        let linear = Linear {
            input,
            output,
            weight_by_input: deterministic_simd_values(input * output, 29, -0.0078125),
            bias: Some(deterministic_simd_values(output, 11, 0.015625)),
        };
        let mut inputs = deterministic_simd_values(rows * input, 17, 0.03125);
        for index in (0..inputs.len()).step_by(9) {
            inputs[index] = 0.0;
        }

        let check = || {
            let mut expected = vec![0.0; rows * output];
            for (input_row, output_row) in inputs
                .chunks_exact(input)
                .zip(expected.chunks_exact_mut(output))
            {
                linear.apply_into(input_row, output_row);
            }
            let mut actual = vec![0.0; rows * output];
            linear.apply_block_into(&inputs, &mut actual, rows);
            assert_eq!(actual, expected);
        };
        check();
        #[cfg(target_arch = "x86_64")]
        if x86_simd_test_switch::with_simd(1, check).is_none() {
            eprintln!("skipping the AVX2/FMA half: AVX2/FMA not available");
        }
    }

    fn sequential_time_mixer_state(steps: &[RwkvScanCapturedStep]) -> Vec<f32> {
        let mut state = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];
        for step in steps {
            let (_, next_state) = single_timestep(
                &step.r,
                &step.k,
                &step.v,
                &step.w,
                &step.a,
                &step.k_deformed,
                &state,
            );
            state = next_state;
        }

        state
    }

    fn scan_reduce_time_mixer_state(
        steps: &[RwkvScanCapturedStep],
        bulk_size: usize,
        leaf_size: usize,
    ) -> Vec<f32> {
        assert!(bulk_size > 0);
        assert!(leaf_size > 0);
        let mut state = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];

        for bulk in steps.chunks(bulk_size) {
            let leaf_transforms = bulk
                .par_chunks(leaf_size)
                .map(|chunk| compose_time_mixer_step_range(chunk, 0, chunk.len()))
                .collect::<Vec<_>>();
            let mut leaf_transforms = leaf_transforms.into_iter();
            let Some(mut bulk_transform) = leaf_transforms.next() else {
                continue;
            };
            for next in leaf_transforms {
                bulk_transform = compose_time_mixer_state_transforms(&bulk_transform, &next);
            }
            state = apply_time_mixer_state_transform(&state, &bulk_transform);
        }

        state
    }

    fn max_abs_diff(left: &[f32], right: &[f32]) -> f32 {
        left.iter()
            .zip(right)
            .map(|(left, right)| (left - right).abs())
            .fold(0.0, f32::max)
    }

    fn parse_scan_bench_sizes(value: &str) -> Vec<usize> {
        value
            .split(',')
            .filter_map(|value| value.trim().parse::<usize>().ok())
            .filter(|value| *value > 0)
            .collect()
    }

    fn auto_scan_bench_sizes(
        step_count: usize,
        memory_budget_bytes: u64,
        bytes_per_review_all_layers: usize,
    ) -> Vec<usize> {
        let max_by_memory = (memory_budget_bytes / bytes_per_review_all_layers as u64)
            .max(1)
            .try_into()
            .unwrap_or(usize::MAX);
        let max_size = step_count.min(max_by_memory).max(1);
        let mut sizes = Vec::new();
        let mut size = 64_usize;
        while size < max_size {
            sizes.push(size);
            size = size.saturating_mul(2);
        }
        sizes.push(max_size);
        sizes.sort_unstable();
        sizes.dedup();
        sizes
    }

    fn scan_bench_env_usize(name: &str, default: usize) -> usize {
        std::env::var(name)
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(default)
    }

    fn scan_bench_env_u64(name: &str, default: u64) -> u64 {
        std::env::var(name)
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(default)
    }

    #[derive(Clone, Copy)]
    struct ScanBenchTiming {
        days_elapsed: i64,
        next_day_at: i64,
    }

    fn scan_bench_timing() -> ScanBenchTiming {
        let now_secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs() as i64;
        let days_elapsed = now_secs / SECONDS_PER_DAY;
        ScanBenchTiming {
            days_elapsed,
            next_day_at: (days_elapsed + 1) * SECONDS_PER_DAY,
        }
    }

    struct ScanBenchHistoricalReviewRow {
        review_id: i64,
        card_id: i64,
        note_id: i64,
        deck_id: i64,
        ease: i64,
        duration_millis: i64,
        review_kind: i64,
        is_learning_start: bool,
    }

    fn collection_reviews_for_scan_bench(
        path: &std::path::Path,
        limit: usize,
        deck_id: Option<i64>,
    ) -> rusqlite::Result<Vec<ReviewInput>> {
        let uri = format!("file:{}?mode=ro&immutable=1", path.to_string_lossy());
        let db = rusqlite::Connection::open_with_flags(
            uri,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI,
        )?;
        let preset_by_deck = deck_config_ids_by_deck(&db)?;
        let current_deck_id_sql = scan_bench_current_deck_id_sql(&db)?;
        let limit_clause = if limit == 0 {
            String::new()
        } else {
            format!(" limit {limit}")
        };
        let deck_clause = deck_id.map_or(String::new(), |deck_id| {
            format!(" and {current_deck_id_sql} = {deck_id}")
        });
        let sql = format!(
            "
with eligible as (
  select
    r.id,
    r.cid,
    c.nid,
    {current_deck_id_sql} as deck_id,
    r.ease,
    r.time,
    r.type,
    lag(r.type) over (partition by r.cid order by r.id) as previous_type
  from revlog r
  join cards c on c.id = r.cid
  where r.ease between 1 and 4
    and r.type in (0, 1, 2, 3, 4, 5)
    and not (r.type = 3 and r.factor = 0)
    {deck_clause}
), learning_starts as (
  select cid, max(id) as start_id
  from eligible
  where type = 0 and (previous_type is null or previous_type != 0)
  group by cid
), last_forgets as (
  select cid, max(id) as forget_id
  from revlog
  where type = 4 and factor = 0
  group by cid
), fallback_starts as (
  select e.cid as cid, min(e.id) as start_id
  from eligible e
  left join learning_starts l on l.cid = e.cid
  left join last_forgets f on f.cid = e.cid
  where l.cid is null
    and (f.forget_id is null or e.id > f.forget_id)
  group by e.cid
), retained_starts as (
  select cid, start_id from learning_starts
  union all
  select cid, start_id from fallback_starts
)
select
  e.id,
  e.cid,
  e.nid,
  e.deck_id,
  e.ease,
  e.time,
  e.type,
  e.id = s.start_id
from eligible e
join retained_starts s on s.cid = e.cid
where e.id >= s.start_id
order by e.id, e.cid
{limit_clause}"
        );
        let timing = scan_bench_timing();
        let mut stmt = db.prepare(&sql)?;
        let rows = stmt.query_map([], |row| {
            Ok(ScanBenchHistoricalReviewRow {
                review_id: row.get(0)?,
                card_id: row.get(1)?,
                note_id: row.get(2)?,
                deck_id: row.get(3)?,
                ease: row.get(4)?,
                duration_millis: row.get(5)?,
                review_kind: row.get(6)?,
                is_learning_start: row.get(7)?,
            })
        })?;

        let mut previous_review_id_by_card = HashMap::new();
        let mut reviews = Vec::new();
        for row in rows {
            let row = row?;
            let previous_review_id = previous_review_id_by_card.insert(row.card_id, row.review_id);
            let day_offset = scan_bench_historical_day_offset(row.review_id, &timing);
            let elapsed_seconds = previous_review_id
                .map_or(-1, |previous| ((row.review_id - previous) / 1000).max(0));
            let elapsed_days = previous_review_id.map_or(-1, |previous| {
                (day_offset - scan_bench_historical_day_offset(previous, &timing)).max(0)
            });
            reviews.push(ReviewInput {
                card_id: row.card_id,
                note_id: Some(row.note_id),
                deck_id: Some(row.deck_id),
                preset_id: preset_by_deck.get(&row.deck_id).copied().flatten(),
                is_query: false,
                ease: Some(row.ease as u8),
                duration_millis: Some(row.duration_millis),
                card_type: Some(scan_bench_historical_state(
                    row.review_kind,
                    row.is_learning_start,
                )),
                day_offset: Some(day_offset),
                current_elapsed_days: Some(elapsed_days),
                current_elapsed_seconds: Some(elapsed_seconds),
                target_retentions: [Some(0.9), Some(0.9), Some(0.9), Some(0.9)],
                enforce_grade_order: true,
            });
        }

        Ok(reviews)
    }

    fn scan_bench_current_deck_id_sql(db: &rusqlite::Connection) -> rusqlite::Result<&'static str> {
        Ok(if scan_bench_table_has_column(db, "cards", "odid")? {
            "case when c.odid != 0 then c.odid else c.did end"
        } else {
            "c.did"
        })
    }

    fn scan_bench_table_has_column(
        db: &rusqlite::Connection,
        table: &str,
        column: &str,
    ) -> rusqlite::Result<bool> {
        let mut stmt = db.prepare(&format!("pragma table_info({table})"))?;
        let columns = stmt.query_map([], |row| row.get::<_, String>(1))?;
        for name in columns {
            if name? == column {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn scan_bench_historical_state(review_kind: i64, is_learning_start: bool) -> i64 {
        if is_learning_start {
            0
        } else {
            review_kind + 1
        }
    }

    fn scan_bench_historical_day_offset(review_id: i64, timing: &ScanBenchTiming) -> i64 {
        let review_secs = review_id / 1000;
        let days_before_today = (timing.next_day_at - 1 - review_secs).max(0) / SECONDS_PER_DAY;
        (timing.days_elapsed - days_before_today).max(0)
    }

    fn scan_bench_global_layer_matrix(
        inference: &RwkvInference,
        layer_id: usize,
    ) -> Option<&[f32]> {
        inference
            .warm_up_states
            .global
            .as_ref()?
            .layers
            .get(layer_id)?
            .time
            .as_ref()
            .map(|state| state.matrix.as_slice())
    }

    fn write_scan_bench_capture(
        path: &std::path::Path,
        steps: &[RwkvScanCapturedStep],
        final_state: &[f32],
    ) -> std::io::Result<()> {
        let mut file = std::io::BufWriter::new(std::fs::File::create(path)?);
        std::io::Write::write_all(&mut file, b"ARWKVSCAN1")?;
        std::io::Write::write_all(&mut file, &(D_MODEL as u32).to_le_bytes())?;
        std::io::Write::write_all(&mut file, &(HEADS as u32).to_le_bytes())?;
        std::io::Write::write_all(&mut file, &(HEAD_SIZE as u32).to_le_bytes())?;
        std::io::Write::write_all(&mut file, &(steps.len() as u64).to_le_bytes())?;
        for step in steps {
            for values in [
                &step.r,
                &step.k,
                &step.v,
                &step.w,
                &step.a,
                &step.k_deformed,
            ] {
                for value in values {
                    std::io::Write::write_all(&mut file, &value.to_le_bytes())?;
                }
            }
        }
        std::io::Write::write_all(&mut file, &(final_state.len() as u32).to_le_bytes())?;
        for value in final_state {
            std::io::Write::write_all(&mut file, &value.to_le_bytes())?;
        }
        Ok(())
    }

    #[test]
    #[ignore]
    fn rwkv_bulk_collection_benchmark() {
        let Ok(collection_path) = std::env::var("ANKI_RWKV_BULK_BENCH_COLLECTION")
            .or_else(|_| std::env::var("ANKI_RWKV_SCAN_BENCH_COLLECTION"))
        else {
            eprintln!(
                "set ANKI_RWKV_BULK_BENCH_COLLECTION to a copied collection.anki2 path to run this benchmark"
            );
            return;
        };
        let weights_path = std::env::var("ANKI_RWKV_BULK_BENCH_MODEL")
            .or_else(|_| std::env::var("ANKI_RWKV_SCAN_BENCH_MODEL"))
            .unwrap_or_else(|_| "qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin".to_string());
        let review_limit = scan_bench_env_usize("ANKI_RWKV_BULK_BENCH_LIMIT", 0);
        let deck_id = std::env::var("ANKI_RWKV_BULK_BENCH_DECK_ID")
            .or_else(|_| std::env::var("ANKI_RWKV_SCAN_BENCH_DECK_ID"))
            .ok()
            .and_then(|value| value.parse().ok());
        let chunk_rows = std::env::var("ANKI_RWKV_BULK_BENCH_CHUNK_ROWS")
            .ok()
            .and_then(|value| value.parse::<usize>().ok());
        let call_rows = std::env::var("ANKI_RWKV_BULK_BENCH_CALL_ROWS")
            .ok()
            .and_then(|value| value.parse::<usize>().ok())
            .filter(|&value| value > 0);
        let record_predictions = std::env::var("ANKI_RWKV_BULK_BENCH_RECORD_PREDICTIONS")
            .is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"));
        let force_fast_query = std::env::var("ANKI_RWKV_BULK_BENCH_FAST_QUERY")
            .is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"));
        let profile = std::env::var("ANKI_RWKV_BULK_BENCH_PROFILE").map_or(true, |value| {
            !matches!(value.as_str(), "0" | "false" | "FALSE" | "no" | "NO")
        });
        // Tests run the scalar kernels; set this to run the AVX2/FMA ones.
        let simd = std::env::var("ANKI_RWKV_BULK_BENCH_SIMD")
            .is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"));
        let predictions_out = std::env::var("ANKI_RWKV_BULK_BENCH_PREDICTIONS_OUT").ok();

        let collection_path = std::path::PathBuf::from(collection_path);
        let weights_path = std::path::PathBuf::from(weights_path);
        let load_reviews_started = std::time::Instant::now();
        let reviews = collection_reviews_for_scan_bench(&collection_path, review_limit, deck_id)
            .expect("collection review load failed");
        let load_reviews_ms = load_reviews_started.elapsed().as_secs_f64() * 1000.0;
        assert!(
            !reviews.is_empty(),
            "collection review load returned no rows"
        );

        let mut inference =
            RwkvInference::load(weights_path.clone(), 0.9, 36_500).expect("RWKV load failed");
        if profile {
            start_rwkv_single_timestep_profile();
            start_rwkv_warmup_profile();
        }
        let warmup_started = std::time::Instant::now();
        let mut run_warmup = || {
            if let Some(call_rows) = call_rows {
                let mut predictions = Vec::new();
                for (call_index, call) in reviews.chunks(call_rows).enumerate() {
                    let offset = call_index * call_rows;
                    let call_predictions = if force_fast_query {
                        bulk::warm_up_reviews_bulk_fast_query_chunked(
                            &mut inference,
                            call.to_vec(),
                            record_predictions,
                            chunk_rows.unwrap_or(16_384),
                        )
                    } else if let Some(chunk_rows) = chunk_rows {
                        bulk::warm_up_reviews_bulk_chunked(
                            &mut inference,
                            call.to_vec(),
                            record_predictions,
                            chunk_rows,
                        )
                    } else {
                        inference.warm_up_reviews(call.to_vec(), record_predictions)
                    }
                    .expect("RWKV warm-up call failed");
                    predictions.extend(call_predictions.into_iter().map(|prediction| {
                        WarmUpPrediction {
                            index: prediction.index + offset,
                            ..prediction
                        }
                    }));
                }
                Ok::<_, std::io::Error>(predictions)
            } else if force_fast_query {
                bulk::warm_up_reviews_bulk_fast_query_chunked(
                    &mut inference,
                    reviews.clone(),
                    record_predictions,
                    chunk_rows.unwrap_or(16_384),
                )
            } else if let Some(chunk_rows) = chunk_rows {
                bulk::warm_up_reviews_bulk_chunked(
                    &mut inference,
                    reviews.clone(),
                    record_predictions,
                    chunk_rows,
                )
            } else {
                inference.warm_up_reviews(reviews.clone(), record_predictions)
            }
            .expect("RWKV warm-up failed")
        };
        #[cfg(target_arch = "x86_64")]
        let predictions = if simd {
            let threads = std::env::var("RAYON_NUM_THREADS")
                .ok()
                .and_then(|value| value.parse().ok())
                .unwrap_or(0);
            x86_simd_test_switch::with_simd(threads, run_warmup).expect("AVX2/FMA not available")
        } else {
            run_warmup()
        };
        #[cfg(not(target_arch = "x86_64"))]
        let predictions = run_warmup();
        let warmup_ms = warmup_started.elapsed().as_secs_f64() * 1000.0;
        let warmup_profile = profile.then(stop_rwkv_warmup_profile);
        let single_timestep_profile = profile.then(stop_rwkv_single_timestep_profile);

        println!("collection={}", collection_path.display());
        println!("weights={}", weights_path.display());
        println!("collection_reviews={}", reviews.len());
        println!("record_predictions={record_predictions}");
        println!(
            "query_path={}",
            if force_fast_query {
                "forced-fast-test-hook"
            } else {
                "production-default"
            }
        );
        println!("profile={profile}");
        if let Some(chunk_rows) = chunk_rows {
            println!("chunk_rows={chunk_rows}");
        }
        if let Some(call_rows) = call_rows {
            println!("call_rows={call_rows}");
        }
        println!("simd={simd}");
        println!("predictions={}", predictions.len());
        if let Some(path) = &predictions_out {
            // One line per prediction: the review index, then the RWKV-Instant
            // and RWKV-Curve values as f32 bits in hex (`-` for no curve value),
            // so that a comparison sees every bit.
            let lines = predictions
                .iter()
                .map(|prediction| {
                    format!(
                        "{} {:08x} {}
",
                        prediction.index,
                        prediction.retrievability.to_bits(),
                        prediction
                            .curve_retrievability
                            .map_or("-".to_string(), |value| format!("{:08x}", value.to_bits()))
                    )
                })
                .collect::<String>();
            std::fs::write(path, lines).expect("write predictions");
        }
        if record_predictions {
            let prediction_values = predictions
                .iter()
                .map(|prediction| prediction.retrievability)
                .collect::<Vec<_>>();
            let outcomes = predictions
                .iter()
                .map(|prediction| reviews[prediction.index].ease != Some(1))
                .collect::<Vec<_>>();
            let metrics = MetricAccumulator::from_predictions(&prediction_values, &outcomes);
            println!("log_loss={:.15}", metrics.log_loss);
        }
        println!("load_reviews_ms={load_reviews_ms:.3}");
        println!("warmup_ms={warmup_ms:.3}");
        if let Ok(threads) = std::env::var("RAYON_NUM_THREADS") {
            println!("rayon_num_threads={threads}");
        }
        if let Some(warmup_profile) = warmup_profile {
            for (name, calls, nanos) in warmup_profile {
                let ms = nanos as f64 / 1_000_000.0;
                println!("warmup_profile_{name}_calls={calls}");
                println!("warmup_profile_{name}_ms={ms:.3}");
                println!(
                    "warmup_profile_{name}_fraction={:.6}",
                    ms / warmup_ms.max(f64::MIN_POSITIVE)
                );
            }
        }
        if let Some((calls, nanos)) = single_timestep_profile {
            println!("single_timestep_calls={calls}");
            println!("single_timestep_ms={:.3}", nanos as f64 / 1_000_000.0);
            println!(
                "single_timestep_warmup_fraction={:.6}",
                (nanos as f64 / 1_000_000.0) / warmup_ms.max(f64::MIN_POSITIVE)
            );
        }
    }

    #[test]
    #[ignore]
    fn rwkv_scan_collection_benchmark() {
        let Ok(collection_path) = std::env::var("ANKI_RWKV_SCAN_BENCH_COLLECTION") else {
            eprintln!(
                "set ANKI_RWKV_SCAN_BENCH_COLLECTION to a copied collection.anki2 path to run this benchmark"
            );
            return;
        };
        let weights_path = std::env::var("ANKI_RWKV_SCAN_BENCH_MODEL")
            .unwrap_or_else(|_| "qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin".to_string());
        let review_limit = scan_bench_env_usize("ANKI_RWKV_SCAN_BENCH_LIMIT", 0);
        let leaf_size = scan_bench_env_usize("ANKI_RWKV_SCAN_BENCH_LEAF_SIZE", 64);
        let layer_id = scan_bench_env_usize("ANKI_RWKV_SCAN_BENCH_LAYER_ID", MODULE_LAYERS[4] - 1);
        let deck_id = std::env::var("ANKI_RWKV_SCAN_BENCH_DECK_ID")
            .ok()
            .and_then(|value| value.parse().ok());
        let memory_budget_bytes = scan_bench_env_u64(
            "ANKI_RWKV_SCAN_BENCH_MEMORY_BUDGET_BYTES",
            16 * 1024 * 1024 * 1024,
        );
        let extend_to_memory_budget = std::env::var("ANKI_RWKV_SCAN_BENCH_EXTEND_TO_MEMORY_BUDGET")
            .is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"));
        let profile_single_timestep = std::env::var("ANKI_RWKV_SCAN_BENCH_PROFILE_SINGLE_TIMESTEP")
            .is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"));
        let profile_warmup = std::env::var("ANKI_RWKV_SCAN_BENCH_PROFILE_WARMUP")
            .is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"));
        let selected_layer_step_bytes = 6 * D_MODEL * std::mem::size_of::<f32>();
        let all_layer_step_bytes = selected_layer_step_bytes * MODULE_LAYERS.iter().sum::<usize>();

        let collection_path = std::path::PathBuf::from(collection_path);
        let weights_path = std::path::PathBuf::from(weights_path);
        let load_reviews_started = std::time::Instant::now();
        let reviews = collection_reviews_for_scan_bench(&collection_path, review_limit, deck_id)
            .expect("collection review load failed");
        let load_reviews_ms = load_reviews_started.elapsed().as_secs_f64() * 1000.0;
        assert!(
            !reviews.is_empty(),
            "collection review load returned no rows"
        );
        assert!(
            layer_id < MODULE_LAYERS[4],
            "global layer id is out of range"
        );

        let mut inference =
            RwkvInference::load(weights_path.clone(), 0.9, 36_500).expect("RWKV load failed");
        start_rwkv_scan_capture(4, layer_id);
        if profile_single_timestep {
            start_rwkv_single_timestep_profile();
        }
        if profile_warmup {
            start_rwkv_warmup_profile();
        }
        let warmup_started = std::time::Instant::now();
        inference
            .warm_up_reviews(reviews.clone(), false)
            .expect("RWKV warm-up failed");
        let warmup_ms = warmup_started.elapsed().as_secs_f64() * 1000.0;
        let warmup_profile = if profile_warmup {
            Some(stop_rwkv_warmup_profile())
        } else {
            None
        };
        let single_timestep_profile = if profile_single_timestep {
            Some(stop_rwkv_single_timestep_profile())
        } else {
            None
        };
        let mut steps = take_rwkv_scan_capture();
        assert_eq!(steps.len(), reviews.len());
        let runtime_matrix = scan_bench_global_layer_matrix(&inference, layer_id)
            .expect("captured global layer state is missing")
            .to_vec();
        let captured_steps = steps.len();
        let capture_state = sequential_time_mixer_state(&steps);
        let capture_error = max_abs_diff(&capture_state, &runtime_matrix);
        if let Ok(capture_path) = std::env::var("ANKI_RWKV_SCAN_BENCH_CAPTURE_OUTPUT") {
            write_scan_bench_capture(std::path::Path::new(&capture_path), &steps, &capture_state)
                .expect("failed to write RWKV scan capture");
            println!("capture_output={capture_path}");
        }

        if extend_to_memory_budget {
            let target_steps = (memory_budget_bytes / all_layer_step_bytes as u64)
                .max(1)
                .try_into()
                .unwrap_or(usize::MAX);
            if steps.len() < target_steps {
                steps.reserve(target_steps - steps.len());
                for index in steps.len()..target_steps {
                    steps.push(steps[index % captured_steps].clone());
                }
            }
        }

        let sequential_started = std::time::Instant::now();
        let sequential_state = sequential_time_mixer_state(&steps);
        let sequential_ms = sequential_started.elapsed().as_secs_f64() * 1000.0;

        let bulk_sizes = std::env::var("ANKI_RWKV_SCAN_BENCH_BULK_SIZES")
            .ok()
            .map(|value| parse_scan_bench_sizes(&value))
            .filter(|sizes| !sizes.is_empty())
            .unwrap_or_else(|| {
                auto_scan_bench_sizes(steps.len(), memory_budget_bytes, all_layer_step_bytes)
            });

        let mut csv_rows = vec![
            "bulk_size,selected_layer_memory_mb,all_layer_memory_mb,scan_ms,speedup_vs_sequential,max_abs_error"
                .to_string(),
        ];

        println!("collection={}", collection_path.display());
        println!("weights={}", weights_path.display());
        println!("collection_reviews={captured_steps}");
        println!("benchmark_steps={}", steps.len());
        println!("extended_to_memory_budget={extend_to_memory_budget}");
        println!("captured_global_module_layer={layer_id}");
        println!("load_reviews_ms={load_reviews_ms:.3}");
        println!("warmup_ms={warmup_ms:.3}");
        if let Some(profile) = &warmup_profile {
            for (name, calls, nanos) in profile {
                let ms = *nanos as f64 / 1_000_000.0;
                println!("warmup_profile_{name}_calls={calls}");
                println!("warmup_profile_{name}_ms={ms:.3}");
                println!(
                    "warmup_profile_{name}_fraction={:.6}",
                    ms / warmup_ms.max(f64::MIN_POSITIVE)
                );
            }
        }
        if let Some((calls, nanos)) = single_timestep_profile {
            println!("single_timestep_calls={calls}");
            println!("single_timestep_ms={:.3}", nanos as f64 / 1_000_000.0);
            println!(
                "single_timestep_warmup_fraction={:.6}",
                (nanos as f64 / 1_000_000.0) / warmup_ms.max(f64::MIN_POSITIVE)
            );
        }
        println!("sequential_single_timestep_ms={sequential_ms:.3}");
        println!("capture_vs_runtime_max_abs_error={capture_error:.9}");
        println!("leaf_size={leaf_size}");
        println!("memory_budget_bytes={memory_budget_bytes}");
        println!("bulk_size,selected_layer_memory_mb,all_layer_memory_mb,scan_ms,speedup_vs_sequential,max_abs_error");

        for bulk_size in bulk_sizes {
            let started = std::time::Instant::now();
            let state = scan_reduce_time_mixer_state(&steps, bulk_size, leaf_size);
            let scan_ms = started.elapsed().as_secs_f64() * 1000.0;
            let error = max_abs_diff(&state, &sequential_state);
            let speedup = sequential_ms / scan_ms.max(f64::MIN_POSITIVE);
            let selected_layer_memory_mb =
                bulk_size as f64 * selected_layer_step_bytes as f64 / (1024.0 * 1024.0);
            let all_layer_memory_mb =
                bulk_size as f64 * all_layer_step_bytes as f64 / (1024.0 * 1024.0);
            let row = format!(
                "{bulk_size},{selected_layer_memory_mb:.3},{all_layer_memory_mb:.3},{scan_ms:.3},{speedup:.6},{error:.9}"
            );
            println!("{row}");
            csv_rows.push(row);
            std::hint::black_box(state);
        }

        if let Ok(output_path) = std::env::var("ANKI_RWKV_SCAN_BENCH_OUTPUT") {
            std::fs::write(&output_path, csv_rows.join("\n"))
                .expect("failed to write RWKV scan benchmark CSV");
            println!("output={output_path}");
        }
    }

    #[test]
    fn single_timestep_state_update_supports_affine_composition() {
        let r1 = deterministic_values(D_MODEL, 0.013);
        let k1 = deterministic_values(D_MODEL, 0.017);
        let v1 = deterministic_values(D_MODEL, 0.019);
        let w1 = deterministic_values(D_MODEL, 0.023);
        let a1 = deterministic_values(D_MODEL, 0.029);
        let k_deformed1 = deterministic_values(D_MODEL, 0.031);
        let r2 = deterministic_values(D_MODEL, 0.037);
        let k2 = deterministic_values(D_MODEL, 0.041);
        let v2 = deterministic_values(D_MODEL, 0.043);
        let w2 = deterministic_values(D_MODEL, 0.047);
        let a2 = deterministic_values(D_MODEL, 0.053);
        let k_deformed2 = deterministic_values(D_MODEL, 0.059);
        let state0 = deterministic_values(HEADS * HEAD_SIZE * HEAD_SIZE, 0.061);

        let (_, state1) = single_timestep(&r1, &k1, &v1, &w1, &a1, &k_deformed1, &state0);
        let (_, sequential_state) = single_timestep(&r2, &k2, &v2, &w2, &a2, &k_deformed2, &state1);

        let mut composed_state = vec![0.0; HEADS * HEAD_SIZE * HEAD_SIZE];
        for head in 0..HEADS {
            let matrix_base = head * HEAD_SIZE * HEAD_SIZE;
            for row in 0..HEAD_SIZE {
                let row_start = matrix_base + row * HEAD_SIZE;
                let first =
                    time_mixer_transform_for_head_row(head, row, &k1, &v1, &w1, &a1, &k_deformed1);
                let second =
                    time_mixer_transform_for_head_row(head, row, &k2, &v2, &w2, &a2, &k_deformed2);
                let composed = compose_affine_transforms(&first, &second);
                let row_state =
                    apply_affine_transform(&state0[row_start..row_start + HEAD_SIZE], &composed);
                composed_state[row_start..row_start + HEAD_SIZE].copy_from_slice(&row_state);
            }
        }

        assert_close(&composed_state, &sequential_state, 1e-5);
    }

    #[test]
    fn single_timestep_chunked_affine_reduction_matches_sequential() {
        let steps = (0..32).map(deterministic_step_vectors).collect::<Vec<_>>();
        let state0 = deterministic_values(HEADS * HEAD_SIZE * HEAD_SIZE, 0.061);

        let mut sequential_state = state0.clone();
        for step in &steps {
            let (_, next_state) = single_timestep(
                &step.r,
                &step.k,
                &step.v,
                &step.w,
                &step.a,
                &step.k_deformed,
                &sequential_state,
            );
            sequential_state = next_state;
        }

        let transform = chunked_time_mixer_state_transform(&steps, 8);
        let reduced_state = apply_time_mixer_state_transform(&state0, &transform);

        assert_close(&reduced_state, &sequential_state, 1e-4);
    }

    fn embedded_weights_path() -> Option<PathBuf> {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin");
        path.exists().then_some(path)
    }

    fn bulk_parity_reviews(count: usize) -> Vec<ReviewInput> {
        let mut state = 0x9e3779b97f4a7c15_u64;
        let mut next = move || {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (state >> 33) as i64
        };
        (0..count)
            .map(|index| {
                let card_id = 100 + next().rem_euclid(23);
                ReviewInput {
                    card_id,
                    note_id: (index % 11 != 3).then_some(1000 + card_id / 2),
                    deck_id: (index % 13 != 5).then_some(2000 + next().rem_euclid(3)),
                    preset_id: (index % 17 != 7).then_some(3000 + next().rem_euclid(2)),
                    is_query: false,
                    ease: (index % 19 != 11).then_some((next().rem_euclid(4) + 1) as u8),
                    duration_millis: Some(1500 + next().rem_euclid(20_000)),
                    card_type: Some(next().rem_euclid(3)),
                    day_offset: Some(7300 + (index as i64) / 7),
                    current_elapsed_days: Some(next().rem_euclid(45) - 1),
                    current_elapsed_seconds: Some(next().rem_euclid(45 * 86_400) - 1),
                    target_retentions: [Some(0.9), Some(0.9), Some(0.9), Some(0.9)],
                    enforce_grade_order: true,
                }
            })
            .collect()
    }

    fn bulk_benchmark_reviews(count: usize) -> Vec<ReviewInput> {
        let mut state = 0x243f6a8885a308d3_u64;
        let mut next = move || {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (state >> 33) as i64
        };
        (0..count)
            .map(|index| {
                let card_id = 100 + index as i64 % 5_000;
                ReviewInput {
                    card_id,
                    note_id: Some(10_000 + card_id / 2),
                    deck_id: Some(20_000 + card_id.rem_euclid(8)),
                    preset_id: Some(30_000 + card_id.rem_euclid(4)),
                    is_query: false,
                    ease: Some((next().rem_euclid(4) + 1) as u8),
                    duration_millis: Some(1_500 + next().rem_euclid(20_000)),
                    card_type: Some(next().rem_euclid(3)),
                    day_offset: Some(7_300 + index as i64 / 10),
                    current_elapsed_days: Some(next().rem_euclid(45) - 1),
                    current_elapsed_seconds: Some(next().rem_euclid(45 * 86_400) - 1),
                    target_retentions: [Some(0.9), Some(0.9), Some(0.9), Some(0.9)],
                    enforce_grade_order: true,
                }
            })
            .collect()
    }

    #[test]
    #[ignore]
    fn rwkv_synthetic_bulk_benchmark() {
        let rows = scan_bench_env_usize("ANKI_RWKV_SYNTHETIC_BENCH_ROWS", 50_000);
        let samples = scan_bench_env_usize("ANKI_RWKV_SYNTHETIC_BENCH_SAMPLES", 5).max(1);
        let warmup_samples = scan_bench_env_usize("ANKI_RWKV_SYNTHETIC_BENCH_WARMUPS", 1);
        let chunk_rows = std::env::var("ANKI_RWKV_SYNTHETIC_BENCH_CHUNK_ROWS")
            .ok()
            .and_then(|value| value.parse::<usize>().ok())
            .filter(|&value| value > 0);
        let record_predictions = std::env::var("ANKI_RWKV_BULK_BENCH_RECORD_PREDICTIONS")
            .is_ok_and(|value| matches!(value.as_str(), "1" | "true" | "TRUE" | "yes" | "YES"));
        let model_path = embedded_weights_path().expect("RWKV parity model is missing");
        let reviews = bulk_benchmark_reviews(rows);
        let mut timings = Vec::with_capacity(samples);
        let mut log_losses = Vec::with_capacity(samples);

        println!("synthetic_rows={rows}");
        println!("samples={samples}");
        println!("warmup_samples={warmup_samples}");
        println!("record_predictions={record_predictions}");
        if let Some(chunk_rows) = chunk_rows {
            println!("chunk_rows={chunk_rows}");
        }
        if let Ok(threads) = std::env::var("RAYON_NUM_THREADS") {
            println!("rayon_num_threads={threads}");
        }

        for sample in 0..warmup_samples + samples {
            let mut inference =
                RwkvInference::load(model_path.clone(), 0.9, 36_500).expect("RWKV load failed");
            let sample_reviews = reviews.clone();
            let started = std::time::Instant::now();
            let predictions = if let Some(chunk_rows) = chunk_rows {
                bulk::warm_up_reviews_bulk_fast_query_chunked(
                    &mut inference,
                    sample_reviews,
                    record_predictions,
                    chunk_rows,
                )
            } else {
                inference.warm_up_reviews(sample_reviews, record_predictions)
            }
            .expect("RWKV warm-up failed");
            let elapsed_ms = started.elapsed().as_secs_f64() * 1_000.0;
            let log_loss = record_predictions.then(|| prediction_log_loss(&predictions, &reviews));
            std::hint::black_box((inference, predictions));
            if sample < warmup_samples {
                println!("warmup_{}_ms={elapsed_ms:.3}", sample + 1);
                if let Some(log_loss) = log_loss {
                    println!("warmup_{}_log_loss={log_loss:.15}", sample + 1);
                }
            } else {
                println!("sample_{}_ms={elapsed_ms:.3}", sample - warmup_samples + 1);
                timings.push(elapsed_ms);
                if let Some(log_loss) = log_loss {
                    println!(
                        "sample_{}_log_loss={log_loss:.15}",
                        sample - warmup_samples + 1
                    );
                    log_losses.push(log_loss);
                }
            }
        }

        timings.sort_by(f64::total_cmp);
        let median_ms = if timings.len() % 2 == 0 {
            let middle = timings.len() / 2;
            (timings[middle - 1] + timings[middle]) / 2.0
        } else {
            timings[timings.len() / 2]
        };
        println!("median_ms={median_ms:.3}");
        println!("min_ms={:.3}", timings[0]);
        println!("max_ms={:.3}", timings[timings.len() - 1]);
        if let Some((&first, rest)) = log_losses.split_first() {
            assert!(
                rest.iter().all(|&log_loss| log_loss == first),
                "logloss changed between benchmark samples: {log_losses:?}"
            );
            println!("log_loss={first:.15}");
        }
    }

    fn sorted_states(mut states: Vec<(i64, Vec<u8>)>) -> Vec<(i64, Vec<u8>)> {
        states.sort_by_key(|(id, _)| *id);
        states
    }

    fn sorted_curve_bytes(curves: &HashMap<i64, ReviewCurve>) -> Vec<(i64, Vec<u8>)> {
        let mut serialized: Vec<(i64, Vec<u8>)> = curves
            .iter()
            .map(|(id, curve)| {
                let mut bytes = Vec::new();
                curve.write_cache_state(&mut bytes);
                (*id, bytes)
            })
            .collect();
        serialized.sort_by_key(|(id, _)| *id);
        serialized
    }

    fn assert_warm_up_parity(sequential: &mut RwkvInference, bulk: &mut RwkvInference) {
        let sequential_snapshot = sequential.warm_up_snapshot().unwrap();
        let bulk_snapshot = bulk.warm_up_snapshot().unwrap();
        assert_eq!(
            sorted_states(sequential_snapshot.card_states),
            sorted_states(bulk_snapshot.card_states),
            "card states diverged"
        );
        assert_eq!(
            sorted_states(sequential_snapshot.note_states),
            sorted_states(bulk_snapshot.note_states),
            "note states diverged"
        );
        assert_eq!(
            sorted_states(sequential_snapshot.deck_states),
            sorted_states(bulk_snapshot.deck_states),
            "deck states diverged"
        );
        assert_eq!(
            sorted_states(sequential_snapshot.preset_states),
            sorted_states(bulk_snapshot.preset_states),
            "preset states diverged"
        );
        assert_eq!(
            sequential_snapshot.global_state, bulk_snapshot.global_state,
            "global state diverged"
        );
        assert_eq!(
            sorted_curve_bytes(&sequential.curves),
            sorted_curve_bytes(&bulk.curves),
            "curves diverged"
        );
    }

    fn assert_prediction_parity(sequential: &[WarmUpPrediction], bulk: &[WarmUpPrediction]) {
        assert_eq!(sequential.len(), bulk.len(), "prediction counts diverged");
        for (expected, actual) in sequential.iter().zip(bulk) {
            assert_eq!(expected.index, actual.index, "prediction order diverged");
            assert_eq!(
                expected.retrievability.to_bits(),
                actual.retrievability.to_bits(),
                "prediction values diverged at review {}",
                expected.index
            );
            assert_curve_parity(expected, actual);
        }
    }

    /// RWKV-Curve's per-review value must match between the two paths bit
    /// for bit, including which reviews have none (spec
    /// ui.stats-model-metrics).
    fn assert_curve_parity(expected: &WarmUpPrediction, actual: &WarmUpPrediction) {
        match (expected.curve_retrievability, actual.curve_retrievability) {
            (None, None) => {}
            (Some(expected_value), Some(actual_value)) => assert_eq!(
                expected_value.to_bits(),
                actual_value.to_bits(),
                "curve value diverged at review {}",
                expected.index
            ),
            (expected_value, actual_value) => panic!(
                "curve presence diverged at review {}: {expected_value:?} vs {actual_value:?}",
                expected.index
            ),
        }
    }

    fn assert_prediction_close(
        expected: &[WarmUpPrediction],
        actual: &[WarmUpPrediction],
        tolerance: f32,
    ) {
        assert_eq!(expected.len(), actual.len(), "prediction counts diverged");
        for (expected, actual) in expected.iter().zip(actual) {
            assert_eq!(expected.index, actual.index, "prediction order diverged");
            let delta = (expected.retrievability - actual.retrievability).abs();
            assert!(
                delta <= tolerance,
                "prediction diverged at review {}: {} vs {} (delta {delta})",
                expected.index,
                expected.retrievability,
                actual.retrievability
            );
            match (expected.curve_retrievability, actual.curve_retrievability) {
                (None, None) => {}
                (Some(expected_value), Some(actual_value)) => {
                    let delta = (expected_value - actual_value).abs();
                    assert!(
                        delta <= tolerance,
                        "curve diverged at review {}: {expected_value} vs {actual_value}",
                        expected.index
                    );
                }
                (expected_value, actual_value) => panic!(
                    "curve presence diverged at review {}: {expected_value:?} vs {actual_value:?}",
                    expected.index
                ),
            }
        }
    }

    fn prediction_log_loss(predictions: &[WarmUpPrediction], reviews: &[ReviewInput]) -> f64 {
        let values = predictions
            .iter()
            .map(|prediction| prediction.retrievability)
            .collect::<Vec<_>>();
        let outcomes = predictions
            .iter()
            .map(|prediction| reviews[prediction.index].ease != Some(1))
            .collect::<Vec<_>>();
        MetricAccumulator::from_predictions(&values, &outcomes).log_loss
    }

    /// Pins sched.rwkv-curve-reschedule and the `is:rwkv-curve:due` interval
    /// (ui.rwkv-curve-r-stored-curve): "Reschedule cards with RWKV-Curve"
    /// takes the current interval and S90 from the curve RWKV stored at the
    /// card's last answered review, not from the curve of a query row (a head
    /// output that training never supervises). A card without a stored curve
    /// gets no interval and no S90, and the call changes no state.
    #[test]
    fn reschedule_intervals_come_from_the_stored_curve() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let mut inference = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        let reviews = curve_source_reviews(160);
        inference
            .warm_up_reviews_sequential(reviews.clone(), false)
            .unwrap();

        // one query per card, a day after that card's most recent review
        let mut seen = std::collections::HashSet::new();
        let queries = reviews
            .iter()
            .rev()
            .filter(|review| seen.insert(review.card_id))
            .map(|review| ReviewInput {
                is_query: true,
                ease: None,
                duration_millis: None,
                card_type: Some(2),
                day_offset: review.day_offset.map(|day| day + 1),
                current_elapsed_days: Some(1),
                current_elapsed_seconds: Some(SECONDS_PER_DAY),
                ..review.clone()
            })
            .collect::<Vec<_>>();
        assert!(queries.len() > 10, "fixture should cover many cards");

        // the curve of each query row, which the reschedule must not read
        let work_items = queries
            .iter()
            .map(|input| {
                let state = inference.warm_up_state(input).unwrap();
                inference.prediction_work_item(input, &state).unwrap()
            })
            .collect::<Vec<_>>();
        let query_heads = inference.model.review_many(&work_items);
        let unseen = ReviewInput {
            card_id: 99_999,
            ..queries[0].clone()
        };
        let mut asked = queries.clone();
        asked.push(unseen);
        let state_before = inference.cache_state();
        let actual = inference
            .predict_current_intervals_many_from_warm_up(asked)
            .unwrap();
        assert_eq!(
            inference.cache_state(),
            state_before,
            "the reschedule's read must not change the state"
        );

        assert_eq!(actual.len(), queries.len() + 1);
        let mut with_interval = 0;
        let mut differs_from_query_curve = 0;
        for ((input, actual), query_heads) in queries.iter().zip(&actual).zip(&query_heads) {
            let curve = inference.curves.get(&input.card_id).expect("stored curve");
            let unrounded = unrounded_interval_for_curve(curve, 0.9, 36_500);
            assert_eq!(actual.current_interval_unrounded, unrounded);
            assert_eq!(
                actual.current_interval,
                unrounded.map(|days| clamped_interval_days(days, 36_500))
            );
            assert_eq!(
                actual.current_s90,
                unrounded_interval_for_curve(curve, S90_TARGET_RETENTION, 36_500)
            );
            assert_eq!(
                actual.curve_retrievability,
                current_curve_retrievability(input, curve)
            );
            with_interval += usize::from(actual.current_interval.is_some());
            differs_from_query_curve += usize::from(
                actual.current_s90
                    != unrounded_interval_for_curve(
                        &query_heads.curve,
                        S90_TARGET_RETENTION,
                        36_500,
                    ),
            );
        }
        assert!(with_interval > 0, "fixture should produce intervals");
        assert!(
            differs_from_query_curve > 0,
            "the query row's curve must differ somewhere, or this pin cannot fail"
        );
        // the whole days `is:rwkv-curve:due` compares with the elapsed days
        // (ui.rwkv-curve-r-stored-curve) must differ somewhere too
        assert!(
            queries
                .iter()
                .zip(&actual)
                .zip(&query_heads)
                .any(|((input, actual), query_heads)| {
                    let target = input.target_retentions[2].unwrap_or(0.9);
                    actual.current_interval
                        != unrounded_interval_for_curve(&query_heads.curve, target, 36_500)
                            .map(|days| clamped_interval_days(days, 36_500))
                }),
            "the query row's whole-day interval must differ somewhere"
        );

        let unseen = actual.last().unwrap();
        assert_eq!(unseen.curve_retrievability, None);
        assert_eq!(unseen.current_interval, None);
        assert_eq!(unseen.current_interval_unrounded, None);
        assert_eq!(unseen.current_s90, None);
    }

    /// Pins the fast Stats Retrievability path: the query-only,
    /// resident-state curve retrievability must equal, bit for bit, the
    /// `curve_retrievability` that the full `predict_many` path returns.
    #[test]
    fn curve_retrievability_from_warm_up_matches_predict_many() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let mut inference = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        let reviews = bulk_parity_reviews(120);
        inference
            .warm_up_reviews_sequential(reviews.clone(), false)
            .unwrap();

        // one query per card, built from that card's most recent review
        let mut seen = std::collections::HashSet::new();
        let queries = reviews
            .iter()
            .rev()
            .filter(|review| seen.insert(review.card_id))
            .map(|review| ReviewInput {
                is_query: true,
                ease: None,
                ..review.clone()
            })
            .collect::<Vec<_>>();
        assert!(queries.len() > 10, "fixture should cover many cards");

        let requests = queries
            .iter()
            .map(|input| ReviewPredictionRequest {
                input: input.clone(),
                state: inference.warm_up_state(input).unwrap(),
            })
            .collect::<Vec<_>>();
        let expected = inference.predict_many(requests).unwrap();
        let actual = inference
            .predict_curve_retrievability_many_from_warm_up(queries)
            .unwrap();

        assert_eq!(expected.len(), actual.len());
        let mut with_curve = 0;
        for (expected, curve_retrievability) in expected.iter().zip(&actual) {
            assert_eq!(expected.curve_retrievability, *curve_retrievability);
            with_curve += usize::from(curve_retrievability.is_some());
        }
        assert!(with_curve > 0, "fixture should produce curve values");
    }

    #[test]
    fn curve_retrievability_from_warm_up_rejects_answered_inputs() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let mut inference = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        let reviews = bulk_parity_reviews(4);
        let error = inference
            .predict_curve_retrievability_many_from_warm_up(reviews)
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
    }

    // Pins spec/ui.md#ui.stats-total-knowledge: under RWKV-Curve, Total
    // Knowledge reads each card's curve as RWKV stored it at the card's last
    // review in the warm-up (the curve card info draws), on whole days.
    #[test]
    fn curve_day_sums_from_warm_up_are_the_stored_curves() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let mut inference = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        let reviews = bulk_parity_reviews(120);
        inference.warm_up_reviews(reviews.clone(), false).unwrap();
        let mut card_ids: Vec<i64> = reviews.iter().map(|review| review.card_id).collect();
        card_ids.sort_unstable();
        card_ids.dedup();
        assert!(card_ids.len() > 10, "fixture should cover many cards");

        let spans: Vec<RwkvCurveSpan> = card_ids
            .iter()
            .enumerate()
            .map(|(index, &card_id)| RwkvCurveSpan {
                card_id,
                review_day: 100 + index as i64 % 3,
                first_day: 101 + index as i64 % 3,
                last_day: 110 + index as i64 % 7,
            })
            .chain([
                // a card without a curve adds nothing
                RwkvCurveSpan {
                    card_id: -1,
                    review_day: 90,
                    first_day: 95,
                    last_day: 120,
                },
                // nor an empty span
                RwkvCurveSpan {
                    card_id: card_ids[0],
                    review_day: 90,
                    first_day: 99,
                    last_day: 98,
                },
            ])
            .collect();
        let (first_day, sums) = inference.curve_retrievability_day_sums_from_warm_up(&spans);

        assert_eq!(first_day, 95);
        let mut expected = vec![0.0f64; sums.len()];
        for span in spans.iter().filter(|span| span.card_id > 0) {
            let elapsed_days: Vec<f32> = (span.first_day..=span.last_day)
                .map(|day| (day - span.review_day) as f32)
                .collect();
            if let Some((recall, _)) = inference.card_curve(span.card_id, &elapsed_days) {
                for (day, recall) in (span.first_day..=span.last_day).zip(recall) {
                    expected[(day - first_day) as usize] += recall as f64;
                }
            }
        }
        assert_eq!(sums.len(), (120 - 95 + 1) as usize);
        for (actual, expected) in sums.iter().zip(&expected) {
            assert!((actual - expected).abs() < 1e-9, "{actual} != {expected}");
        }
        assert!(sums[(102 - 95) as usize] > 1.0);
        assert!(inference
            .curve_retrievability_day_sums_from_warm_up(&[])
            .1
            .is_empty());
    }

    #[test]
    fn bulk_warm_up_matches_sequential() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = bulk_parity_reviews(230);
        for record_predictions in [false, true] {
            let mut sequential = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
            let sequential_predictions = sequential
                .warm_up_reviews_sequential(reviews.clone(), record_predictions)
                .unwrap();

            let mut bulk = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
            let bulk_predictions =
                bulk::warm_up_reviews_bulk(&mut bulk, reviews.clone(), record_predictions).unwrap();

            assert_prediction_close(&sequential_predictions, &bulk_predictions, 1e-5);
            if record_predictions {
                let sequential_log_loss = prediction_log_loss(&sequential_predictions, &reviews);
                let bulk_log_loss = prediction_log_loss(&bulk_predictions, &reviews);
                let log_loss_delta = (bulk_log_loss - sequential_log_loss).abs();
                assert!(
                    log_loss_delta <= 1e-7,
                    "logloss changed: before={sequential_log_loss:.15}, after={bulk_log_loss:.15}, delta={log_loss_delta:.15}"
                );
            }
            assert_warm_up_parity(&mut sequential, &mut bulk);
            assert_eq!(
                sequential.cache_state().len(),
                bulk.cache_state().len(),
                "runtime cache size diverged (record={record_predictions})"
            );
        }
    }

    /// Measurement harness, not a pin. Point it at a **copy** of a real
    /// state-cache store:
    ///
    /// ```text
    /// ANKI_RWKV_STATE_STORE=<path> ANKI_RWKV_STATE_GENERATION=<generation>
    /// ANKI_RWKV_STATE_SEGMENT=<segment id> ANKI_RWKV_STATE_READS=<count>
    /// cargo test --release -p anki --lib rwkv_state_store_startup_benchmark
    ///     -- --ignored --nocapture
    /// ```
    #[test]
    #[ignore]
    fn rwkv_state_store_startup_benchmark() {
        let Ok(store) = std::env::var("ANKI_RWKV_STATE_STORE") else {
            eprintln!("skipping: ANKI_RWKV_STATE_STORE is not set");
            return;
        };
        let generation = std::env::var("ANKI_RWKV_STATE_GENERATION").unwrap();
        let segment_id: i64 = std::env::var("ANKI_RWKV_STATE_SEGMENT")
            .unwrap()
            .parse()
            .unwrap();
        let reads: usize = std::env::var("ANKI_RWKV_STATE_READS")
            .unwrap_or_else(|_| "300".to_string())
            .parse()
            .unwrap();
        let weights = embedded_weights_path().expect("RWKV weights are missing");
        let mut inference = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        let started = std::time::Instant::now();
        inference
            .restore_warm_up_state_checkpoint(store.into(), &generation, segment_id)
            .unwrap();
        println!("restore_ms {}", started.elapsed().as_secs_f64() * 1000.0);
        let source = inference
            .warm_up_states
            .lazy
            .as_ref()
            .expect("restored session should read states lazily");
        println!(
            "indexed_cards {} indexed_notes {} resident_decks {} resident_presets {}",
            source.card.len(),
            source.note.len(),
            inference.warm_up_states.deck.len(),
            inference.warm_up_states.preset.len()
        );
        let mut card_ids = source.card.keys().copied().collect::<Vec<_>>();
        card_ids.sort_unstable();
        let step = (card_ids.len() / reads.max(1)).max(1);
        let sample = card_ids
            .into_iter()
            .step_by(step)
            .take(reads)
            .collect::<Vec<_>>();
        let started = std::time::Instant::now();
        for card_id in &sample {
            inference
                .warm_up_states
                .load_lazy(STATE_CACHE_KIND_CARD, *card_id)
                .unwrap();
        }
        let elapsed = started.elapsed().as_secs_f64() * 1000.0;
        println!(
            "card_reads {} total_ms {elapsed} per_read_ms {}",
            sample.len(),
            elapsed / sample.len() as f64
        );
    }

    /// Reviews over many cards and notes, several reviews per card, so that
    /// the cards end the warm-up in states that predict different values.
    fn lazy_state_reviews(cards: i64, per_card: i64) -> Vec<ReviewInput> {
        let mut state = 0x6a09e667f3bcc908_u64;
        let mut next = move || {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (state >> 33) as i64
        };
        (0..cards * per_card)
            .map(|index| {
                let card_id = 100 + index % cards;
                ReviewInput {
                    card_id,
                    note_id: Some(10_000 + card_id / 2),
                    deck_id: Some(20_000 + card_id.rem_euclid(8)),
                    preset_id: Some(30_000 + card_id.rem_euclid(4)),
                    is_query: false,
                    ease: Some((next().rem_euclid(4) + 1) as u8),
                    duration_millis: Some(1_500 + next().rem_euclid(20_000)),
                    card_type: Some(next().rem_euclid(3)),
                    day_offset: Some(7_300 + index / cards * 30),
                    current_elapsed_days: Some(next().rem_euclid(120) - 1),
                    current_elapsed_seconds: Some(next().rem_euclid(120 * 86_400) - 1),
                    target_retentions: [Some(0.9), Some(0.9), Some(0.9), Some(0.9)],
                    enforce_grade_order: true,
                }
            })
            .collect()
    }

    /// Pins `sched.rwkv-lazy-state-load`: a restored session that reads card
    /// and note states one key at a time predicts exactly what the fully
    /// resident warm-up predicts, and reads back only the keys it asks for.
    ///
    /// The fixture has 900 cards over 450 notes, and the check below proves
    /// the predictions differ from card to card, so a state read for the
    /// wrong key would change a prediction and fail this test.
    #[test]
    fn lazy_state_reads_match_resident_predictions() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = lazy_state_reviews(900, 4);
        let temporary_dir = tempfile::tempdir().unwrap();
        let store_path = temporary_dir.path().join("state.sqlite3");
        let generation = "lazy-generation";

        let mut resident = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        resident.warm_up_reviews(reviews.clone(), false).unwrap();
        let segment_id = resident
            .write_warm_up_state_checkpoint(
                store_path.clone(),
                generation,
                None,
                reviews.len() as i64,
                reviews.len() as i64,
                "lazy-hash",
                "replay-key",
                b"previous-ids",
                b"previous-intervals",
                b"review-counts",
                true,
                false,
            )
            .unwrap();
        resident.finish_warm_up_state_checkpoints().unwrap();

        let mut seen = HashSet::new();
        let queries = reviews
            .iter()
            .rev()
            .filter(|review| seen.insert(review.card_id))
            .map(|review| ReviewInput {
                is_query: true,
                ease: None,
                ..review.clone()
            })
            .collect::<Vec<_>>();
        assert!(
            queries.len() > 300,
            "fixture should cover more than 300 cards, got {}",
            queries.len()
        );
        let sample = queries[..300].to_vec();

        let mut lazy = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        lazy.restore_warm_up_state_checkpoint(store_path, generation, segment_id)
            .unwrap();
        let indexed_cards = lazy
            .warm_up_states
            .lazy
            .as_ref()
            .expect("restored session should read states lazily")
            .card
            .len();
        assert_eq!(
            indexed_cards,
            queries.len(),
            "every card should be indexed and none of them resident yet"
        );
        assert!(
            lazy.warm_up_states.card.is_empty(),
            "no card state should be resident before a card comes up"
        );

        let expected = resident
            .predict_retrievability_many_from_warm_up(sample.clone())
            .unwrap();
        let actual = lazy
            .predict_retrievability_many_from_warm_up(sample.clone())
            .unwrap();
        assert_eq!(expected.len(), sample.len());
        for (index, (expected, actual)) in expected.iter().zip(&actual).enumerate() {
            assert_eq!(
                expected.to_bits(),
                actual.to_bits(),
                "retrievability diverged at row {index}"
            );
        }

        // Only the 300 cards asked for were read back.
        assert_eq!(
            lazy.warm_up_states.card.len(),
            300,
            "lazy session should hold only the cards it predicted"
        );
        assert_eq!(
            lazy.warm_up_states.lazy.as_ref().unwrap().card.len(),
            indexed_cards - 300,
            "the rest of the cards should still be in the store"
        );

        // The state each card was predicted from must be the resident one,
        // byte for byte. A pin that cannot fail is not a pin, so the check
        // below also proves the 300 cards have 300 different states: a read
        // that returned another card's state would change these bytes.
        let mut card_states = Vec::new();
        for input in &sample {
            let lazy_state = lazy.warm_up_state(input).unwrap();
            let resident_state = resident.warm_up_state(input).unwrap();
            assert_eq!(
                lazy_state.card, resident_state.card,
                "card {} state diverged",
                input.card_id
            );
            assert_eq!(
                lazy_state.note, resident_state.note,
                "note of card {} diverged",
                input.card_id
            );
            assert_eq!(
                lazy_state.deck, resident_state.deck,
                "deck of card {} diverged",
                input.card_id
            );
            assert_eq!(
                lazy_state.preset, resident_state.preset,
                "preset of card {} diverged",
                input.card_id
            );
            assert_eq!(lazy_state.global, resident_state.global, "global diverged");
            card_states.push(lazy_state.card.expect("card state"));
        }
        let distinct = card_states.iter().collect::<HashSet<_>>();
        assert!(
            distinct.len() > 250,
            "the fixture's cards should nearly all carry their own state, got {} of {}",
            distinct.len(),
            card_states.len()
        );
    }

    /// Rewrites a schema-5 store in the schema-4 format the upgrade has to
    /// read, and returns the rows it wrote, keyed by (kind, entity).
    ///
    /// The code that wrote schema 4 is gone, so the bytes are laid out here
    /// from the format's own description. The returned map is what the test
    /// checks the upgraded store against, so the check does not go through the
    /// upgrade's own reader.
    fn downgrade_state_cache_store_to_v4(
        path: &std::path::Path,
        chunk_bytes: usize,
    ) -> HashMap<(i64, i64, i64), Option<Vec<u8>>> {
        let connection = Connection::open(path).unwrap();
        let mut rows: HashMap<(i64, i64, i64), Option<Vec<u8>>> = HashMap::new();
        {
            let mut statement = connection
                .prepare("select segment_id, kind, entity_id, state from entity_states")
                .unwrap();
            let mapped = statement
                .query_map([], |row| {
                    Ok((
                        (
                            row.get::<_, i64>(0)?,
                            row.get::<_, i64>(1)?,
                            row.get::<_, i64>(2)?,
                        ),
                        row.get::<_, Option<Vec<u8>>>(3)?,
                    ))
                })
                .unwrap();
            for row in mapped {
                let (key, state) = row.unwrap();
                rows.insert(key, state);
            }
        }
        let segment_ids = {
            let mut statement = connection
                .prepare("select id from segments order by id")
                .unwrap();
            let mapped = statement.query_map([], |row| row.get::<_, i64>(0)).unwrap();
            mapped.map(|row| row.unwrap()).collect::<Vec<i64>>()
        };
        connection
            .execute_batch(
                r#"
drop table entity_states;
create table segment_state_chunks (
  id integer primary key,
  segment_id integer not null references segments(id) on delete cascade,
  chunk_index integer not null,
  state_delta blob not null,
  unique (segment_id, chunk_index)
);
"#,
            )
            .unwrap();
        for segment_id in segment_ids {
            let mut stream = Vec::new();
            stream.extend_from_slice(STATE_CACHE_DELTA_MAGIC);
            for kind in [
                STATE_CACHE_KIND_CARD,
                STATE_CACHE_KIND_NOTE,
                STATE_CACHE_KIND_DECK,
                STATE_CACHE_KIND_PRESET,
            ] {
                let mut entities = rows
                    .iter()
                    .filter(|((row_segment, row_kind, _), _)| {
                        *row_segment == segment_id && *row_kind == kind
                    })
                    .map(|((_, _, entity_id), state)| (*entity_id, state.clone()))
                    .collect::<Vec<_>>();
                entities.sort_by_key(|(entity_id, _)| *entity_id);
                stream.extend_from_slice(&(entities.len() as u32).to_le_bytes());
                for (entity_id, state) in entities {
                    stream.extend_from_slice(&entity_id.to_le_bytes());
                    match state {
                        Some(state) => {
                            stream.push(1);
                            stream.extend_from_slice(&(state.len() as u32).to_le_bytes());
                            stream.extend_from_slice(&state);
                        }
                        None => stream.push(0),
                    }
                }
            }
            match rows.get(&(
                segment_id,
                STATE_CACHE_KIND_GLOBAL,
                STATE_CACHE_GLOBAL_ENTITY_ID,
            )) {
                None => stream.push(0),
                Some(None) => stream.push(1),
                Some(Some(state)) => {
                    stream.push(2);
                    stream.extend_from_slice(&(state.len() as u32).to_le_bytes());
                    stream.extend_from_slice(state);
                }
            }
            for (chunk_index, chunk) in stream.chunks(chunk_bytes).enumerate() {
                connection
                    .execute(
                        "insert into segment_state_chunks (segment_id, chunk_index, state_delta) \
                         values (?, ?, ?)",
                        params![segment_id, chunk_index as i64, chunk],
                    )
                    .unwrap();
            }
        }
        connection
            .pragma_update(
                None,
                "user_version",
                STATE_CACHE_STORE_CHUNKED_SCHEMA_VERSION,
            )
            .unwrap();
        connection.close().unwrap();
        rows
    }

    /// Pins the upgrade of a store an older Clanki wrote: every row of the
    /// upgraded store carries the bytes the schema-4 stream held for that key,
    /// and the upgraded store restores the session the states came from.
    #[test]
    fn a_schema_4_store_upgrades_without_changing_a_state() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = lazy_state_reviews(40, 3);
        let temporary_dir = tempfile::tempdir().unwrap();
        let store_path = temporary_dir.path().join("state.sqlite3");
        let generation = "upgrade-generation";

        let mut expected = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        expected.warm_up_reviews(reviews.clone(), false).unwrap();
        let segment_id = expected
            .write_warm_up_state_checkpoint(
                store_path.clone(),
                generation,
                None,
                reviews.len() as i64,
                reviews.len() as i64,
                "upgrade-hash",
                "replay-key",
                b"previous-ids",
                b"previous-intervals",
                b"review-counts",
                true,
                false,
            )
            .unwrap();
        expected.finish_warm_up_state_checkpoints().unwrap();

        // 512-byte chunks, so a state spans about a hundred of them and the
        // upgrade's reader has to cross chunk boundaries inside one entity.
        let written = downgrade_state_cache_store_to_v4(&store_path, 512);
        assert!(written.len() > 60, "fixture should hold many entities");
        let version: i64 = Connection::open(&store_path)
            .unwrap()
            .pragma_query_value(None, "user_version", |row| row.get(0))
            .unwrap();
        assert_eq!(version, STATE_CACHE_STORE_CHUNKED_SCHEMA_VERSION);

        migrate_state_cache_store(&store_path).unwrap();

        let connection = Connection::open(&store_path).unwrap();
        let version: i64 = connection
            .pragma_query_value(None, "user_version", |row| row.get(0))
            .unwrap();
        assert_eq!(version, STATE_CACHE_STORE_SCHEMA_VERSION);
        assert!(
            !state_cache_upgrade_path(&store_path).exists(),
            "the half-built store should be gone"
        );
        let mut upgraded: HashMap<(i64, i64, i64), Option<Vec<u8>>> = HashMap::new();
        {
            let mut statement = connection
                .prepare("select segment_id, kind, entity_id, state from entity_states")
                .unwrap();
            let mapped = statement
                .query_map([], |row| {
                    Ok((
                        (
                            row.get::<_, i64>(0)?,
                            row.get::<_, i64>(1)?,
                            row.get::<_, i64>(2)?,
                        ),
                        row.get::<_, Option<Vec<u8>>>(3)?,
                    ))
                })
                .unwrap();
            for row in mapped {
                let (key, state) = row.unwrap();
                assert!(
                    upgraded.insert(key, state).is_none(),
                    "a key should appear once per segment"
                );
            }
        }
        assert_eq!(upgraded, written, "the upgrade changed a stored state");
        let chunk_table: i64 = connection
            .query_row(
                "select count(*) from sqlite_master where type = 'table' and name = ?",
                ["segment_state_chunks"],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(chunk_table, 0, "the old chunk table should be gone");
        drop(connection);

        let mut restored = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        restored
            .restore_warm_up_state_checkpoint(store_path, generation, segment_id)
            .unwrap();
        assert_warm_up_parity(&mut expected, &mut restored);
    }

    /// Pins the guard on the full-checkpoint writer: a session that still has
    /// states in the store may not write a segment that claims to hold them
    /// all. The three callers load everything first; a fourth one that forgets
    /// gets an error instead of a store with the other cards dropped.
    #[test]
    fn a_full_checkpoint_refuses_a_session_that_still_reads_lazily() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = lazy_state_reviews(40, 3);
        let temporary_dir = tempfile::tempdir().unwrap();
        let store_path = temporary_dir.path().join("state.sqlite3");
        let generation = "guard-generation";

        let mut resident = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        resident.warm_up_reviews(reviews.clone(), false).unwrap();
        let segment_id = resident
            .write_warm_up_state_checkpoint(
                store_path.clone(),
                generation,
                None,
                reviews.len() as i64,
                reviews.len() as i64,
                "guard-hash",
                "replay-key",
                b"previous-ids",
                b"previous-intervals",
                b"review-counts",
                true,
                false,
            )
            .unwrap();
        resident.finish_warm_up_state_checkpoints().unwrap();

        let mut lazy = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        lazy.restore_warm_up_state_checkpoint(store_path.clone(), generation, segment_id)
            .unwrap();
        assert!(lazy.warm_up_states.lazy.is_some());
        let error = {
            let mut connection =
                Connection::open(temporary_dir.path().join("guard.sqlite3")).unwrap();
            connection
                .execute_batch("create table segments (id integer primary key);")
                .unwrap();
            connection
                .execute_batch(STATE_CACHE_ENTITY_STATES_DDL)
                .unwrap();
            connection
                .execute("insert into segments (id) values (1)", [])
                .unwrap();
            let transaction = connection.transaction().unwrap();
            lazy.warm_up_states
                .write_checkpoint_rows(&transaction, 1, true)
                .expect_err("a full checkpoint needs every state resident")
        };
        assert!(
            error.to_string().contains("needs every state resident"),
            "unexpected error: {error}"
        );

        // The same writer accepts the full checkpoint once the states are read.
        lazy.warm_up_states.force_load_all().unwrap();
        let mut connection = Connection::open(temporary_dir.path().join("guard2.sqlite3")).unwrap();
        connection
            .execute_batch("create table segments (id integer primary key);")
            .unwrap();
        connection
            .execute_batch(STATE_CACHE_ENTITY_STATES_DDL)
            .unwrap();
        connection
            .execute("insert into segments (id) values (1)", [])
            .unwrap();
        let transaction = connection.transaction().unwrap();
        lazy.warm_up_states
            .write_checkpoint_rows(&transaction, 1, true)
            .unwrap();
        transaction.commit().unwrap();
    }

    /// Pins the day-to-day flow: a session that restored lazily and then
    /// answered more reviews writes a checkpoint that holds only what it
    /// changed, and the chain still restores every state.
    #[test]
    fn a_lazy_session_writes_a_complete_checkpoint_chain() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = lazy_state_reviews(400, 3);
        let split = reviews.len() * 2 / 3;
        let prefix = reviews[..split].to_vec();
        // A short tail, so that the second checkpoint covers only some of the
        // cards and the rest have to come from the first one.
        let suffix = reviews[split..split + 60].to_vec();
        let reviews = reviews[..split + 60].to_vec();
        let temporary_dir = tempfile::tempdir().unwrap();
        let store_path = temporary_dir.path().join("state.sqlite3");
        let generation = "chain-generation";

        let mut expected = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        expected.warm_up_reviews(reviews.clone(), false).unwrap();

        let mut staged = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        staged.warm_up_reviews(prefix, false).unwrap();
        let first_segment = staged
            .write_warm_up_state_checkpoint(
                store_path.clone(),
                generation,
                None,
                split as i64,
                split as i64,
                "prefix-hash",
                "replay-key",
                b"previous-ids",
                b"previous-intervals",
                b"review-counts",
                true,
                false,
            )
            .unwrap();
        staged.finish_warm_up_state_checkpoints().unwrap();

        let mut session = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        session
            .restore_warm_up_state_checkpoint(store_path.clone(), generation, first_segment)
            .unwrap();
        session.warm_up_reviews(suffix, false).unwrap();
        let second_segment = session
            .write_warm_up_state_checkpoint(
                store_path.clone(),
                generation,
                Some(first_segment),
                reviews.len() as i64,
                reviews.len() as i64,
                "final-hash",
                "replay-key",
                b"previous-ids",
                b"previous-intervals",
                b"review-counts",
                false,
                false,
            )
            .unwrap();
        session.finish_warm_up_state_checkpoints().unwrap();

        let connection = Connection::open(&store_path).unwrap();
        let first_rows: i64 = connection
            .query_row(
                "select count(*) from entity_states where segment_id = ?",
                [first_segment],
                |row| row.get(0),
            )
            .unwrap();
        let second_rows: i64 = connection
            .query_row(
                "select count(*) from entity_states where segment_id = ?",
                [second_segment],
                |row| row.get(0),
            )
            .unwrap();
        assert!(
            second_rows < first_rows,
            "the second checkpoint should hold only what the session changed: \
             {second_rows} >= {first_rows}"
        );

        let mut restored = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        restored
            .restore_warm_up_state_checkpoint(store_path, generation, second_segment)
            .unwrap();
        assert_warm_up_parity(&mut expected, &mut restored);
    }

    /// Pins the "never a stale-but-plausible state" half of
    /// `sched.rwkv-lazy-state-load`: a row that does not carry the key it was
    /// asked for is an error, not a state.
    #[test]
    fn lazy_state_read_rejects_a_foreign_row() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = bulk_parity_reviews(40);
        let temporary_dir = tempfile::tempdir().unwrap();
        let store_path = temporary_dir.path().join("state.sqlite3");
        let generation = "foreign-generation";

        let mut resident = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        resident.warm_up_reviews(reviews.clone(), false).unwrap();
        let segment_id = resident
            .write_warm_up_state_checkpoint(
                store_path.clone(),
                generation,
                None,
                40,
                40,
                "foreign-hash",
                "replay-key",
                b"previous-ids",
                b"previous-intervals",
                b"review-counts",
                true,
                false,
            )
            .unwrap();
        resident.finish_warm_up_state_checkpoints().unwrap();

        let mut lazy = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        lazy.restore_warm_up_state_checkpoint(store_path, generation, segment_id)
            .unwrap();
        let mut card_ids = lazy
            .warm_up_states
            .lazy
            .as_ref()
            .unwrap()
            .card
            .keys()
            .copied()
            .collect::<Vec<_>>();
        card_ids.sort_unstable();
        assert!(card_ids.len() > 1);
        let (first, second) = (card_ids[0], card_ids[1]);
        {
            let source = lazy.warm_up_states.lazy.as_mut().unwrap();
            let first_row = source.card[&first];
            let second_row = source.card[&second];
            source.card.insert(first, second_row);
            source.card.insert(second, first_row);
        }
        let error = lazy
            .warm_up_states
            .load_lazy(STATE_CACHE_KIND_CARD, first)
            .expect_err("a row with another card's key must not be accepted");
        assert!(
            error.to_string().contains("key mismatch"),
            "unexpected error: {error}"
        );
    }

    #[test]
    fn delta_state_store_restores_checkpoint_chain() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = bulk_parity_reviews(40);
        let prefix = reviews[..32].to_vec();
        let suffix = reviews[..8].to_vec();
        let temporary_dir = tempfile::tempdir().unwrap();
        let store_path = temporary_dir.path().join("state.sqlite3");
        let generation = "test-generation";

        let mut expected_prefix = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        expected_prefix
            .warm_up_reviews(prefix.clone(), false)
            .unwrap();

        let mut expected_final = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        expected_final.warm_up_reviews(prefix, false).unwrap();
        let first_segment = expected_final
            .write_warm_up_state_checkpoint(
                store_path.clone(),
                generation,
                None,
                32,
                32,
                "prefix-hash",
                "replay-key",
                b"previous-ids",
                b"previous-intervals",
                b"review-counts",
                false,
                false,
            )
            .unwrap();
        expected_final
            .warm_up_reviews(suffix.clone(), false)
            .unwrap();
        let second_segment = expected_final
            .write_warm_up_state_checkpoint(
                store_path.clone(),
                generation,
                Some(first_segment),
                40,
                40,
                "final-hash",
                "replay-key",
                b"previous-ids",
                b"previous-intervals",
                b"review-counts",
                false,
                false,
            )
            .unwrap();
        assert!(expected_final.state_cache_store.is_some());
        expected_final.finish_warm_up_state_checkpoints().unwrap();
        assert!(expected_final.state_cache_store.is_none());

        let connection = Connection::open(&store_path).unwrap();
        let page_size: i64 = connection
            .pragma_query_value(None, "page_size", |row| row.get(0))
            .unwrap();
        assert_eq!(page_size, STATE_CACHE_STORE_PAGE_SIZE);
        let first_state_rows: i64 = connection
            .query_row(
                "select count(*) from entity_states where segment_id = ?",
                [first_segment],
                |row| row.get(0),
            )
            .unwrap();
        let second_state_rows: i64 = connection
            .query_row(
                "select count(*) from entity_states where segment_id = ?",
                [second_segment],
                |row| row.get(0),
            )
            .unwrap();
        let first_card_rows: i64 = connection
            .query_row(
                "select count(*) from entity_states where segment_id = ? and kind = ?",
                params![first_segment, STATE_CACHE_KIND_CARD],
                |row| row.get(0),
            )
            .unwrap();
        assert!(
            first_card_rows > 1,
            "test checkpoint should store many card rows"
        );
        assert!(
            second_state_rows < first_state_rows,
            "suffix checkpoint should hold only dirty states: {second_state_rows} >= {first_state_rows}"
        );

        let mut restored_prefix = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        restored_prefix
            .restore_warm_up_state_checkpoint(store_path.clone(), generation, first_segment)
            .unwrap();
        assert_warm_up_parity(&mut expected_prefix, &mut restored_prefix);

        let mut restored_final = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        restored_final
            .restore_warm_up_state_checkpoint(store_path, generation, second_segment)
            .unwrap();
        assert_warm_up_parity(&mut expected_final, &mut restored_final);
    }

    #[test]
    fn streamed_module_state_matches_existing_serialization() {
        let state = ModuleState {
            layers: vec![
                LayerState {
                    time: Some(TimeState {
                        x_shift: vec![1.0, -2.5, f32::INFINITY],
                        matrix: vec![0.0, f32::NAN, 42.25, -0.0],
                    }),
                    channel_shift: Some(vec![3.5, -7.75]),
                },
                LayerState {
                    time: None,
                    channel_shift: None,
                },
            ],
        };
        let serialized = serialize_module_state(&state);
        let mut streamed = Vec::new();
        write_serialized_module_state(&mut streamed, &state).unwrap();
        assert_eq!(streamed, serialized);

        let mut expected_snapshot = Vec::new();
        write_snapshot_bytes(&mut expected_snapshot, &serialized).unwrap();
        let mut streamed_snapshot = Vec::new();
        write_snapshot_module_state(&mut streamed_snapshot, &state).unwrap();
        assert_eq!(streamed_snapshot, expected_snapshot);
    }

    #[test]
    fn same_card_multi_answer_prediction_matches_individual_branches() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = bulk_parity_reviews(64);
        let mut inference = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        inference.warm_up_reviews(reviews.clone(), false).unwrap();

        let mut query = reviews.last().unwrap().clone();
        query.is_query = true;
        query.ease = None;
        query.duration_millis = None;
        let answers = ANSWER_EASES
            .into_iter()
            .map(|ease| simulated_answer_input(&query, ease))
            .collect::<Vec<_>>();
        fn snapshot_of(inference: &mut RwkvInference) -> RwkvWorkloadSimulationSnapshot {
            let states = inference.warm_up_snapshot().unwrap();
            RwkvWorkloadSimulationSnapshot {
                card_states: states.card_states,
                note_states: states.note_states,
                deck_states: states.deck_states,
                preset_states: states.preset_states,
                global_state: states.global_state,
                runtime_state: Some(inference.cache_state()),
            }
        }

        let expected = answers
            .iter()
            .map(|answer| {
                let snapshot = snapshot_of(&mut inference);
                inference
                    .predict_retrievability_many_after_review(
                        answer.clone(),
                        vec![query.clone()],
                        snapshot,
                    )
                    .unwrap()[0]
            })
            .collect::<Vec<_>>();
        let snapshot = snapshot_of(&mut inference);
        let snapshot_actual = inference
            .predict_retrievability_many_after_reviews(
                answers.clone(),
                vec![query.clone()],
                snapshot,
            )
            .unwrap()
            .into_iter()
            .map(|scores| scores[0])
            .collect::<Vec<_>>();
        let resident_state_before = inference.cache_state();
        let resident_actual = inference
            .predict_retrievability_many_after_reviews_from_warm_up(answers, vec![query])
            .unwrap()
            .into_iter()
            .map(|scores| scores[0])
            .collect::<Vec<_>>();

        assert_close(&snapshot_actual, &expected, 1e-6);
        assert_close(&resident_actual, &expected, 1e-6);
        assert_eq!(inference.cache_state(), resident_state_before);
    }

    #[test]
    fn bulk_warm_up_chunked_calls_match_sequential() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = bulk_parity_reviews(160);

        let mut sequential = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        let sequential_predictions = sequential
            .warm_up_reviews_sequential(reviews.clone(), true)
            .unwrap();

        // Uneven bridge-style calls plus a tiny internal chunk size, so both
        // cross-call and cross-chunk stream continuity are exercised.
        let mut bulk = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        let mut bulk_predictions = Vec::new();
        let mut offset = 0;
        for call in [37, 96, 27] {
            let calls: Vec<ReviewInput> = reviews[offset..offset + call].to_vec();
            let predictions =
                bulk::warm_up_reviews_bulk_chunked(&mut bulk, calls, true, 7).unwrap();
            bulk_predictions.extend(predictions.into_iter().map(|prediction| WarmUpPrediction {
                index: prediction.index + offset,
                ..prediction
            }));
            offset += call;
        }

        assert_prediction_parity(&sequential_predictions, &bulk_predictions);
        assert_warm_up_parity(&mut sequential, &mut bulk);
    }

    /// The gap the RWKV session named: a card's FIRST row inside a batch
    /// has its previous answered review in an EARLIER call, so the bulk
    /// path must read that card's carried-in curve, not None and not this
    /// batch's last row. Rows 1-3 of one card go in the first call and rows
    /// 4-5 in the second (spec ui.stats-model-metrics).
    #[test]
    fn curve_values_survive_a_card_split_across_two_warm_up_calls() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        // one card, five answered reviews, so every row after the first has
        // a previous row and therefore a curve
        let reviews: Vec<ReviewInput> = (0..5)
            .map(|index| ReviewInput {
                card_id: 42,
                note_id: Some(4200),
                deck_id: Some(2000),
                preset_id: Some(3000),
                is_query: false,
                ease: Some(if index % 4 == 0 { 1 } else { 3 }),
                duration_millis: Some(2500),
                card_type: Some(1),
                day_offset: Some(7300 + index as i64 * 3),
                current_elapsed_days: Some(3),
                current_elapsed_seconds: Some(3 * 86_400),
                target_retentions: [None; 4],
                enforce_grade_order: false,
            })
            .collect();

        let mut sequential = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        let expected = sequential
            .warm_up_reviews_sequential(reviews.clone(), true)
            .unwrap();
        assert_eq!(expected.len(), 5);
        // the card's first review has no earlier curve, and gets none
        assert!(expected[0].curve_retrievability.is_none());
        for prediction in &expected[1..] {
            assert!(
                prediction.curve_retrievability.is_some(),
                "review {} should have a curve",
                prediction.index
            );
        }

        // the same history, split 3 + 2 across two calls on one inference
        let mut bulk = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        let mut actual = Vec::new();
        let mut offset = 0;
        for call in [3usize, 2] {
            let batch: Vec<ReviewInput> = reviews[offset..offset + call].to_vec();
            let predictions = bulk::warm_up_reviews_bulk(&mut bulk, batch, true).unwrap();
            actual.extend(predictions.into_iter().map(|prediction| WarmUpPrediction {
                index: prediction.index + offset,
                ..prediction
            }));
            offset += call;
        }

        assert_eq!(actual.len(), expected.len());
        for (expected, actual) in expected.iter().zip(&actual) {
            assert_curve_parity(expected, actual);
        }
        // row 4 is the first of the SECOND call: its curve comes from row 3,
        // which this call never saw, and it must not be None
        assert!(actual[3].curve_retrievability.is_some());
        assert!(actual[0].curve_retrievability.is_none());
    }

    /// The whole batch, many cards, both paths: today's equivalence test
    /// covers `retrievability` only, so this one covers the curve as well.
    #[test]
    fn bulk_warm_up_curve_values_match_sequential_over_a_batch() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = bulk_parity_reviews(230);

        let mut sequential = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        let expected = sequential
            .warm_up_reviews_sequential(reviews.clone(), true)
            .unwrap();
        let mut bulk = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        let actual = bulk::warm_up_reviews_bulk(&mut bulk, reviews, true).unwrap();

        assert_eq!(expected.len(), actual.len());
        let mut with_curve = 0;
        let mut without_curve = 0;
        for (expected, actual) in expected.iter().zip(&actual) {
            assert_curve_parity(expected, actual);
            if expected.curve_retrievability.is_some() {
                with_curve += 1;
            } else {
                without_curve += 1;
            }
        }
        // the fixture must exercise both sides, or the test could not fail
        assert!(with_curve > 100, "only {with_curve} reviews carry a curve");
        assert!(
            without_curve > 0,
            "no review is a card's first, so None is never checked"
        );
    }

    #[test]
    fn bulk_state_only_wavefront_matches_sequential_across_chunks() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = bulk_parity_reviews(160);

        let mut sequential = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        sequential
            .warm_up_reviews_sequential(reviews.clone(), false)
            .unwrap();

        let mut wavefront = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        let mut offset = 0;
        for call in [37, 96, 27] {
            let predictions = bulk::warm_up_reviews_bulk_chunked(
                &mut wavefront,
                reviews[offset..offset + call].to_vec(),
                false,
                7,
            )
            .unwrap();
            assert!(predictions.is_empty());
            offset += call;
        }

        assert_warm_up_parity(&mut sequential, &mut wavefront);
        assert_eq!(sequential.cache_state(), wavefront.cache_state());
    }

    #[test]
    fn bulk_fast_query_preserves_state_with_small_prediction_delta() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = bulk_parity_reviews(160);

        let mut exact = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        let exact_predictions =
            bulk::warm_up_reviews_bulk_chunked(&mut exact, reviews.clone(), true, 7).unwrap();
        let mut fast = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        let fast_predictions =
            bulk::warm_up_reviews_bulk_fast_query_chunked(&mut fast, reviews, true, 7).unwrap();

        assert_eq!(exact_predictions.len(), fast_predictions.len());
        let mut max_delta = 0.0_f32;
        let mut max_curve_delta = 0.0_f32;
        for (exact, fast) in exact_predictions.iter().zip(&fast_predictions) {
            assert_eq!(exact.index, fast.index);
            max_delta = max_delta.max((exact.retrievability - fast.retrievability).abs());
            match (exact.curve_retrievability, fast.curve_retrievability) {
                (None, None) => {}
                (Some(exact_value), Some(fast_value)) => {
                    max_curve_delta = max_curve_delta.max((exact_value - fast_value).abs());
                }
                (exact_value, fast_value) => panic!(
                    "curve presence diverged at review {}: {exact_value:?} vs {fast_value:?}",
                    exact.index
                ),
            }
        }
        assert!(
            max_curve_delta <= 1e-5,
            "max fast bulk curve delta: {max_curve_delta}"
        );
        assert!(
            max_delta <= 1e-5,
            "max fast bulk prediction delta: {max_delta}"
        );
        assert_warm_up_parity(&mut exact, &mut fast);
    }

    #[test]
    fn clamped_interval_days_rounds_up_to_target_crossing() {
        assert_eq!(clamped_interval_days(1.0, 10), 1);
        assert_eq!(clamped_interval_days(1.1, 10), 2);
        assert_eq!(clamped_interval_days(12.0, 10), 10);
    }

    #[test]
    fn interval_for_curve_returns_max_when_target_not_reached() {
        let curve = ReviewCurve {
            ahead_logits: vec![20.0],
            weights: vec![1.0],
            s90: Default::default(),
        };

        assert_eq!(interval_for_curve(&curve, 0.0, 365), Some(365));
    }

    #[test]
    fn predict_curve_ignores_ahead_logits_and_is_monotonic() {
        let curve = ReviewCurve {
            ahead_logits: vec![-20.0, 20.0, -20.0, 20.0],
            weights: vec![0.2, 0.3, 0.5],
            s90: Default::default(),
        };
        let same_weights = ReviewCurve {
            ahead_logits: vec![20.0, -20.0, 20.0, -20.0],
            weights: curve.weights.clone(),
            s90: Default::default(),
        };
        let elapsed_seconds = [1.0, 60.0, 3_600.0, 86_400.0, 2_592_000.0];
        let values = elapsed_seconds.map(|seconds| predict_curve(&curve, seconds));

        assert_eq!(
            values,
            elapsed_seconds.map(|seconds| predict_curve(&same_weights, seconds))
        );
        assert!(values.windows(2).all(|pair| pair[0] >= pair[1]));
    }

    #[test]
    fn pava_averages_adjacent_grade_order_violations() {
        let adjusted = pava_non_decreasing([0.30, 0.25, 0.60, 0.90]);

        assert!((adjusted[0] - 0.275).abs() < 1e-6);
        assert!((adjusted[1] - 0.275).abs() < 1e-6);
        assert_eq!(adjusted[2], 0.60);
        assert_eq!(adjusted[3], 0.90);
    }

    #[test]
    fn pava_merges_again_when_an_averaged_block_still_violates_order() {
        let adjusted = pava_non_decreasing([0.55, 0.40, 0.35, 0.90]);
        let pooled = (0.55 + 0.40 + 0.35) / 3.0;

        assert!(adjusted[..3]
            .iter()
            .all(|value| (*value - pooled).abs() < 1e-6));
        assert_eq!(adjusted[3], 0.90);
    }

    #[test]
    fn pava_crossings_are_ordered_with_per_grade_targets() {
        // margins that cross 0 at 80, 20, 60 and 90 days; pooled, the first
        // two cross together at 50
        let raw_crossing_days = [80.0, 20.0, 60.0, 90.0];
        let crossings = crossings_on_grid(100, |day| {
            pava_non_decreasing(std::array::from_fn(|index| {
                (raw_crossing_days[index] - day) / 1_024.0
            }))
        });
        for (crossing, expected) in crossings.into_iter().zip([50.0, 50.0, 60.0, 90.0]) {
            assert!((crossing - expected).abs() < 1e-3, "{crossings:?}");
        }
    }

    fn basis_curve(basis_index: usize) -> ReviewCurve {
        let mut weights = vec![0.0; basis_index + 1];
        weights[basis_index] = 1.0;
        ReviewCurve {
            ahead_logits: vec![0.0],
            weights,
            s90: Default::default(),
        }
    }

    /// A basis curve's time constant in days: its curve is 0.9^(t/s), so it
    /// meets 90% at t = s (up to predict_curve's 1e-5 floor).
    fn basis_s90_days(basis_index: usize) -> f32 {
        let s_space_raw = linspace_exp(basis_index, NUM_CURVES, 18.5);
        (0.1 + (s_space_raw - 1.0) * (22.0_f32 - 18.5).exp()) / SECONDS_PER_DAY as f32
    }

    // Pins spec/scheduling.md#sched.rwkv-curve-s90 and
    // sched.sub-day-intervals: an interval is where RWKV-Curve's own curve
    // meets the target (not a straight line between grid points).
    #[test]
    fn intervals_are_where_the_curve_meets_the_target() {
        // one exponential: exactly its time constant
        for basis in [30, 40, 50, 60, 70, 80, 90, 100] {
            let expected = basis_s90_days(basis);
            let s90 = unrounded_interval_for_curve(&basis_curve(basis), 0.9, 36_500).unwrap();
            assert!(
                (s90 - expected).abs() <= 1e-4 * expected + 2.0 / SECONDS_PER_DAY as f32,
                "basis {basis}: {s90} vs {expected}"
            );
        }
        // mixtures, with and without grade order: the curve is at the target
        // at the interval and still above it a few seconds before
        let mixture = |bases: [usize; 3]| {
            let mut weights = vec![0.0; NUM_CURVES];
            for basis in bases {
                weights[basis] = 1.0 / 3.0;
            }
            ReviewCurve {
                ahead_logits: vec![0.0],
                weights,
                s90: Default::default(),
            }
        };
        let curves = [
            mixture([20, 60, 90]),
            mixture([40, 70, 100]),
            mixture([50, 80, 110]),
            mixture([60, 90, 120]),
        ];
        let refs = std::array::from_fn(|index| &curves[index]);
        for target in [0.8, 0.9, 0.95] {
            for enforce in [false, true] {
                let intervals =
                    unrounded_intervals_for_answer_curves(refs, [target; 4], 36_500, enforce);
                for (index, interval) in intervals.into_iter().enumerate() {
                    let interval = interval.unwrap();
                    let at = |days: f32| predict_curve(refs[index], days * SECONDS_PER_DAY as f32);
                    assert!(at(interval) <= target + 1e-6, "{target} {enforce} {index}");
                    let earlier = interval - 5.0 / SECONDS_PER_DAY as f32 - interval * 2e-6;
                    assert!(at(earlier) > target, "{target} {enforce} {index}");
                }
            }
        }
    }

    #[test]
    fn a_fast_forgetting_curve_gives_a_sub_day_interval() {
        // basis 32 has a stability of about an hour (R = 0.9 at t = s), so
        // the curve meets 0.9 a little after 0.04 days
        let curves = [
            basis_curve(32),
            basis_curve(32),
            basis_curve(32),
            basis_curve(32),
        ];
        let refs = std::array::from_fn(|index| &curves[index]);
        let intervals = unrounded_intervals_for_answer_curves(refs, [0.9; 4], 36_500, true);
        for interval in intervals {
            let interval = interval.unwrap();
            assert!(interval > 0.03 && interval < 0.05, "{interval}");
        }
        // and the sub-day crossing is where the curve meets the target
        let crossing = intervals[0].unwrap();
        let retrievability = predict_curve(&curves[0], crossing * SECONDS_PER_DAY as f32);
        assert!((retrievability - 0.9).abs() < 0.02, "{retrievability}");
    }

    // Pins spec/scheduling.md#sched.rwkv-curve-s90: RWKV-Curve's S90 is the
    // unrounded point where the curve meets 90%, under a day for a fast
    // curve, and rounds up to the whole-day S90 computed before.
    #[test]
    fn rwkv_curve_s90_is_unrounded() {
        for basis in [1, 3, 10, 20, 32, 40, 60, 80, 100, 110] {
            let curve = basis_curve(basis);
            let s90 = unrounded_interval_for_curve(&curve, S90_TARGET_RETENTION, 36_500).unwrap();
            let days = interval_for_curve(&curve, S90_TARGET_RETENTION, 36_500).unwrap();
            assert_eq!(clamped_interval_days(s90, 36_500), days, "basis {basis}");
            // the same search as the answer S90s without grade order
            let refs = [&curve, &curve, &curve, &curve];
            let answer_s90 = unrounded_intervals_for_answer_curves(
                refs,
                [S90_TARGET_RETENTION; 4],
                36_500,
                false,
            )[0]
            .unwrap();
            assert_eq!(s90, answer_s90, "basis {basis}");
        }
        let fast = unrounded_interval_for_curve(&basis_curve(32), 0.9, 36_500).unwrap();
        assert!(fast > 0.03 && fast < 0.05, "{fast}");
        let slow = unrounded_interval_for_curve(&basis_curve(60), 0.9, 36_500).unwrap();
        assert!(slow > 1.0 && slow != slow.round(), "{slow}");
    }

    // Pins spec/ui.md#ui.card-info-rwkv-curve: every answered review of a
    // replay saves one curve source, the bulk and the per-review paths save
    // the same bytes (also across a chunk boundary, and with or without the
    // per-review predictions), and the last source of each card rebuilds the
    // curve the replay stored for that card, to within 16-bit rounding. So
    // the last segment card info rebuilds is the segment it draws today.
    #[test]
    fn curve_sources_rebuild_the_curve_stored_at_each_review() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let reviews = curve_source_reviews(160);
        let answered: Vec<usize> = (0..reviews.len())
            .filter(|&index| reviews[index].ease.is_some())
            .collect();
        let (_, _, width) = RwkvInference::load(weights.clone(), 0.9, 36_500)
            .unwrap()
            .curve_source_tag();

        let mut sequential = RwkvInference::load(weights.clone(), 0.9, 36_500).unwrap();
        sequential.record_curve_sources(true);
        sequential
            .warm_up_reviews_sequential(reviews.clone(), true)
            .unwrap();
        let expected = sequential.take_curve_sources();
        assert_eq!(
            expected.indices,
            answered
                .iter()
                .map(|&index| index as u32)
                .collect::<Vec<_>>()
        );
        assert_eq!(expected.bytes.len(), answered.len() * width);

        let mut bulk = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        bulk.record_curve_sources(true);
        let split = 70;
        bulk.warm_up_reviews(reviews[..split].to_vec(), false)
            .unwrap();
        let mut actual = bulk.take_curve_sources();
        bulk.warm_up_reviews(reviews[split..].to_vec(), true)
            .unwrap();
        let second = bulk.take_curve_sources();
        actual
            .indices
            .extend(second.indices.iter().map(|index| index + split as u32));
        actual.bytes.extend(second.bytes);
        assert_eq!(actual, expected, "bulk and per-review sources diverged");
        assert_warm_up_parity(&mut sequential, &mut bulk);

        let days = [0.0, 0.01, 0.5, 1.0, 3.0, 10.0, 30.0, 100.0, 365.0];
        let mut last_by_card = HashMap::new();
        for (position, &index) in actual.indices.iter().enumerate() {
            last_by_card.insert(reviews[index as usize].card_id, position);
        }
        assert!(last_by_card.len() > 10, "fixture should cover many cards");
        assert!(
            decode_curve_source(&actual.bytes)
                .iter()
                .all(|value| value.is_finite()),
            "a source holds a value that is not finite"
        );
        let mut s90s = HashSet::new();
        for (card_id, position) in last_by_card {
            let source = &actual.bytes[position * width..(position + 1) * width];
            let rebuilt = bulk
                .curves_from_sources(
                    CURVE_SOURCE_FORMAT,
                    CURVE_SOURCE_KERNEL,
                    source,
                    width,
                    &days,
                )
                .unwrap();
            let (recall, s90) = rebuilt[0].clone().unwrap();
            let (stored_recall, stored_s90) = bulk.card_curve(card_id, &days).unwrap();
            for (rebuilt, stored) in recall.iter().zip(&stored_recall) {
                assert!(
                    (rebuilt - stored).abs() < 1e-4,
                    "card {card_id}: recall {rebuilt} vs stored {stored}"
                );
            }
            // an S90 of seconds sits where the curve is flat, so its
            // rounding error is larger in relative terms: allow 3% and one
            // second
            assert!(
                (s90 - stored_s90).abs() <= 0.03 * stored_s90 + 1.0 / SECONDS_PER_DAY as f32,
                "card {card_id}: S90 {s90} vs stored {stored_s90}"
            );
            s90s.insert(stored_s90.to_bits());
        }
        // the cards' curves differ, so a source that rebuilt a fixed curve
        // would fail above
        assert!(s90s.len() > 5, "the fixture's curves should differ");
    }

    /// Reviews with real elapsed times: each card's elapsed days and
    /// seconds since its own previous review, -1 on its first.
    fn curve_source_reviews(count: usize) -> Vec<ReviewInput> {
        let mut state = 0x2545_f491_4f6c_dd1d_u64;
        let mut next = move || {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (state >> 33) as i64
        };
        let mut previous: HashMap<i64, i64> = HashMap::new();
        (0..count)
            .map(|index| {
                let card_id = 100 + next().rem_euclid(23);
                let seconds = index as i64 * 7_919 + next().rem_euclid(3_600);
                let day = 7_300 + seconds / SECONDS_PER_DAY;
                let before = previous.insert(card_id, seconds);
                ReviewInput {
                    card_id,
                    note_id: Some(1000 + card_id / 2),
                    deck_id: Some(2000 + card_id % 3),
                    preset_id: Some(3000),
                    is_query: false,
                    ease: Some((next().rem_euclid(4) + 1) as u8),
                    duration_millis: Some(1500 + next().rem_euclid(20_000)),
                    card_type: Some(if before.is_some() { 1 } else { 0 }),
                    day_offset: Some(day),
                    current_elapsed_days: Some(
                        before.map_or(-1, |before| day - (7_300 + before / SECONDS_PER_DAY)),
                    ),
                    current_elapsed_seconds: Some(before.map_or(-1, |before| seconds - before)),
                    target_retentions: [Some(0.9); 4],
                    enforce_grade_order: true,
                }
            })
            .collect()
    }

    // Pins spec/ui.md#ui.card-info-rwkv-curve: a source is read only by the
    // model, format and kernel that saved it, and nothing is recorded while
    // recording is off.
    #[test]
    fn curve_sources_of_another_format_are_not_read() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let mut inference = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        inference
            .warm_up_reviews(bulk_parity_reviews(20), false)
            .unwrap();
        assert_eq!(inference.take_curve_sources(), CurveSources::default());

        let (format, kernel, width) = inference.curve_source_tag();
        let source = vec![0; width];
        let days = [1.0];
        assert!(inference
            .curves_from_sources(format, kernel, &source, width, &days)
            .is_some());
        assert!(inference
            .curves_from_sources(format + 1, kernel, &source, width, &days)
            .is_none());
        assert!(inference
            .curves_from_sources(format, kernel + 1, &source, width, &days)
            .is_none());
        assert!(inference
            .curves_from_sources(format, kernel, &source[1..], width, &days)
            .is_none());
    }

    #[test]
    fn f16_encoding_round_trips_and_rounds_to_nearest_even() {
        for bits in 0..=u16::MAX {
            let value = f32_from_f16_bits(bits);
            if value.is_nan() {
                assert!(f32_from_f16_bits(f16_bits(value)).is_nan());
                continue;
            }
            assert_eq!(f16_bits(value), bits, "half {bits:#06x} = {value}");
        }
        assert_eq!(f16_bits(1.0), 0x3c00);
        assert_eq!(f16_bits(-2.0), 0xc000);
        assert_eq!(f16_bits(65504.0), 0x7bff);
        assert_eq!(f16_bits(70000.0), 0x7c00);
        assert_eq!(f16_bits(1e-9), 0);
        // exactly halfway between 1.0 and the next half: ties to even
        assert_eq!(f16_bits(1.0 + 2f32.powi(-11)), 0x3c00);
        assert_eq!(f16_bits(1.0 + 3.0 * 2f32.powi(-11)), 0x3c02);
        // the smallest subnormal half, and half of it (ties to even: zero)
        assert_eq!(f16_bits(2f32.powi(-24)), 0x0001);
        assert_eq!(f16_bits(2f32.powi(-25)), 0x0000);
        let mut state = 12345_u32;
        for _ in 0..100_000 {
            state = state.wrapping_mul(1_103_515_245).wrapping_add(12_345);
            let value = (state as f32 / u32::MAX as f32 - 0.5) * 16.0;
            let back = f32_from_f16_bits(f16_bits(value));
            assert!(
                (back - value).abs() <= value.abs() * 2f32.powi(-11) + 2f32.powi(-25),
                "{value} came back as {back}"
            );
        }
    }

    // Pins spec/ui.md#ui.card-info-rwkv-curve: card info gets the stored
    // curve's recall at the times asked for, and the S90 of that same curve.
    #[test]
    fn card_curve_points_are_the_curve_and_its_s90() {
        for basis in [32, 60, 100] {
            let curve = basis_curve(basis);
            let days = [0.0, 0.01, 1.0, 30.0, 365.0];
            let (recall, s90) = curve_points_and_s90(&curve, &days, 36_500).unwrap();
            for (day, recall) in days.iter().zip(&recall) {
                assert_eq!(*recall, predict_curve(&curve, day * SECONDS_PER_DAY as f32));
            }
            assert_eq!(
                Some(s90),
                unrounded_interval_for_curve(&curve, S90_TARGET_RETENTION, 36_500)
            );
            let at_s90 = predict_curve(&curve, s90 * SECONDS_PER_DAY as f32);
            assert!((at_s90 - 0.9).abs() < 1e-3, "basis {basis}: {at_s90}");
        }
    }

    // Pins spec/ui.md#ui.rwkv-curve-stored-s90: an answer finds its stored
    // curve's S90 at once; the batch read gives every stored curve's S90
    // (finding the ones a replay left without it), the same value card info
    // shows; the runtime state keeps them for the same maximum interval,
    // and a state from before them is read with the S90s found again.
    #[test]
    fn a_stored_curve_keeps_its_s90() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        let mut inference = RwkvInference::load(weights, 0.9, 36_500).unwrap();
        let reviews: Vec<ReviewInput> = bulk_parity_reviews(60)
            .into_iter()
            .filter(|review| review.ease.is_some())
            .collect();
        let (replayed, answered) = reviews.split_at(reviews.len() - 1);
        inference
            .warm_up_reviews_sequential(replayed.to_vec(), false)
            .unwrap();
        // a replay stores its curves without their S90
        assert!(inference
            .curves
            .values()
            .all(|curve| curve.s90.get().is_none()));

        let answer = answered[0].clone();
        let card_id = answer.card_id;
        inference
            .review(
                answer,
                ReviewState {
                    card: None,
                    deck: None,
                    note: None,
                    preset: None,
                    global: None,
                },
            )
            .unwrap();
        let expected = |curve: &ReviewCurve| {
            unrounded_interval_for_curve(curve, S90_TARGET_RETENTION, 36_500).unwrap()
        };
        let answered_curve = &inference.curves[&card_id];
        let kept = answered_curve.s90.get().copied().flatten().unwrap();
        assert!((kept - expected(answered_curve)).abs() <= 1e-6);

        let mut card_ids: Vec<i64> = inference.curves.keys().copied().collect();
        card_ids.sort_unstable();
        card_ids.push(-1);
        let s90s = inference.card_curve_s90s(&card_ids);
        assert_eq!(s90s.last(), Some(&None));
        for (card_id, s90) in card_ids.iter().zip(&s90s).take(card_ids.len() - 1) {
            let curve = &inference.curves[card_id];
            let s90 = s90.unwrap();
            assert!((s90 - expected(curve)).abs() <= 1e-6, "{card_id}: {s90}");
            assert_eq!(inference.card_curve(*card_id, &[1.0]).unwrap().1, s90);
        }

        let state = inference.cache_state();
        let (_, curves) = read_runtime_cache_state(&state, 36_500, inference.model.ids).unwrap();
        for (card_id, s90) in card_ids.iter().zip(&s90s).take(card_ids.len() - 1) {
            assert_eq!(curves[card_id].s90.get().copied().flatten(), *s90);
        }
        // found at another maximum interval: found again
        let (_, curves) = read_runtime_cache_state(&state, 3_650, inference.model.ids).unwrap();
        assert!(curves.values().all(|curve| curve.s90.get().is_none()));
        // a state from before the S90s: read, and found again
        let mut old = RUNTIME_CACHE_STATE_MAGIC_WITHOUT_S90.to_vec();
        inference.features.write_cache_state_to(&mut old).unwrap();
        write_state_cache_u32(&mut old, inference.curves.len()).unwrap();
        for card_id in &card_ids[..card_ids.len() - 1] {
            old.extend_from_slice(&card_id.to_le_bytes());
            inference.curves[card_id]
                .write_cache_state_to(&mut old)
                .unwrap();
        }
        let mut restored =
            RwkvInference::load(embedded_weights_path().unwrap(), 0.9, 36_500).unwrap();
        restored.restore_cache_state(&old).unwrap();
        assert!(restored
            .curves
            .values()
            .all(|curve| curve.s90.get().is_none()));
        assert_eq!(restored.card_curve_s90s(&card_ids), s90s);
    }

    // Pins spec/ui.md#ui.rwkv-curve-stored-s90: a curve that stays above 90%
    // recall up to the maximum interval has the maximum interval as its S90,
    // as card info gave it before the S90s were kept, and keeps it through
    // the runtime state (not NaN); S90s found another way are found again.
    #[test]
    fn a_curve_above_ninety_percent_has_the_maximum_interval_as_its_s90() {
        let Some(weights) = embedded_weights_path() else {
            eprintln!("skipping: embedded RWKV weights not found");
            return;
        };
        for max_interval_days in [36_500, 3_650] {
            let mut inference =
                RwkvInference::load(weights.clone(), 0.9, max_interval_days).unwrap();
            let curve = basis_curve(NUM_CURVES - 1);
            assert!(predict_curve(&curve, max_interval_days as f32 * SECONDS_PER_DAY as f32) > 0.9);
            let (_, before) = curve_points_and_s90(&curve, &[], max_interval_days).unwrap();
            assert_eq!(before, max_interval_days as f32);
            inference.curves.insert(7, curve);
            assert_eq!(inference.card_curve_s90s(&[7]), vec![Some(before)]);
            assert_eq!(inference.card_curve(7, &[]).unwrap().1, before);

            let state = inference.cache_state();
            let (_, curves) =
                read_runtime_cache_state(&state, max_interval_days, inference.model.ids).unwrap();
            assert_eq!(curves[&7].s90.get().copied(), Some(Some(before)));

            // a state whose S90s were found another way
            let kernel_at = RUNTIME_CACHE_STATE_MAGIC.len() + inference.features.cache_state_len();
            let mut other_kernel = state.clone();
            other_kernel[kernel_at..kernel_at + 4]
                .copy_from_slice(&(STORED_CURVE_S90_KERNEL + 1).to_le_bytes());
            let (_, curves) =
                read_runtime_cache_state(&other_kernel, max_interval_days, inference.model.ids)
                    .unwrap();
            assert!(curves[&7].s90.get().is_none());
        }
    }

    // Pins spec/scheduling.md#sched.advance-postpone-algorithm: Advance and
    // Postpone get each card's stored curve as it is (only the cards that
    // have one), and evaluate it as card info and the reschedule do.
    #[test]
    fn stored_curves_reach_the_collection_unchanged() {
        let curves = HashMap::from([
            (1, basis_curve(60)),
            (
                3,
                ReviewCurve {
                    ahead_logits: vec![0.5],
                    weights: vec![0.2, 0.0, 0.3, 0.5],
                    s90: Default::default(),
                },
            ),
        ]);
        let (ids, bytes) = pack_stored_curves(&curves, &[3, 2, 1]);
        assert_eq!(ids, [3, 1]);
        let unpacked = unpack_stored_curves(&ids, &bytes).unwrap();
        assert_eq!(unpacked.len(), 2);
        for (card_id, stored) in &unpacked {
            let curve = &curves[card_id];
            assert_eq!(stored.curve.weights, curve.weights);
            for days in [0.0, 0.5, 3.0, 40.0] {
                assert_eq!(
                    stored.recall(days),
                    predict_curve(curve, days * SECONDS_PER_DAY as f32)
                );
            }
            assert_eq!(
                stored.crossing(0.85, 36_500),
                unrounded_interval_for_curve(curve, 0.85, 36_500)
            );
        }
        // one curve per id, and nothing left over
        assert!(unpack_stored_curves(&[3], &bytes).is_none());
        assert!(unpack_stored_curves(&[3, 1, 7], &bytes).is_none());
        assert!(unpack_stored_curves(&ids, &bytes[..bytes.len() - 1]).is_none());
    }

    #[test]
    fn unrounded_intervals_are_not_rounded_after_the_first_day() {
        let curves = [
            basis_curve(60),
            basis_curve(60),
            basis_curve(60),
            basis_curve(60),
        ];
        let refs = std::array::from_fn(|index| &curves[index]);
        let interval =
            unrounded_intervals_for_answer_curves(refs, [0.9; 4], 36_500, false)[2].unwrap();
        assert!(interval >= 1.0);
        assert_ne!(
            interval,
            interval.round(),
            "{interval} should keep its fraction"
        );
    }

    #[test]
    fn answer_curve_grade_order_enforcement_can_be_disabled() {
        let curve_for_basis_index = |basis_index: usize| {
            let mut weights = vec![0.0; basis_index + 1];
            weights[basis_index] = 1.0;
            ReviewCurve {
                ahead_logits: vec![0.0],
                weights,
                s90: Default::default(),
            }
        };
        let curves = [
            curve_for_basis_index(100),
            curve_for_basis_index(80),
            curve_for_basis_index(90),
            curve_for_basis_index(110),
        ];
        let curve_refs = std::array::from_fn(|index| &curves[index]);

        let raw = unrounded_intervals_for_answer_curves(curve_refs, [0.75; 4], 36_500, false);
        let adjusted = unrounded_intervals_for_answer_curves(curve_refs, [0.75; 4], 36_500, true);

        assert!(raw[0] > raw[1]);
        assert!(adjusted
            .windows(2)
            .all(|pair| pair[0].unwrap() <= pair[1].unwrap()));
        assert_ne!(adjusted, raw);
    }

    #[test]
    fn simulated_answer_input_preserves_review_context() {
        let input = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: true,
            ease: None,
            duration_millis: None,
            card_type: Some(2),
            day_offset: Some(42),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [Some(0.81), Some(0.82), Some(0.83), Some(0.84)],
            enforce_grade_order: true,
        };

        let good = simulated_answer_input(&input, 3);

        assert!(!good.is_query);
        assert_eq!(good.ease, Some(3));
        assert_eq!(good.card_id, input.card_id);
        assert_eq!(good.current_elapsed_days, input.current_elapsed_days);
        assert_eq!(good.target_retentions, input.target_retentions);
        assert!(good.enforce_grade_order);
    }

    #[test]
    fn new_card_creation_elapsed_is_query_only() {
        let query = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: true,
            ease: None,
            duration_millis: None,
            card_type: Some(0),
            day_offset: Some(42),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [Some(0.81), Some(0.82), Some(0.83), Some(0.84)],
            enforce_grade_order: true,
        };

        assert_eq!(elapsed_seconds(&query), 259_200);
        assert_eq!(elapsed_days(&query, elapsed_seconds(&query)), 3);

        let answer = simulated_answer_input(&query, 3);

        assert_eq!(elapsed_seconds(&answer), -1);
        assert_eq!(elapsed_days(&answer, elapsed_seconds(&answer)), -1);
    }

    #[test]
    fn current_curve_retrievability_uses_elapsed_seconds_then_days() {
        let mut input = ReviewInput {
            card_id: 123,
            note_id: None,
            deck_id: None,
            preset_id: None,
            is_query: true,
            ease: None,
            duration_millis: None,
            card_type: Some(2),
            day_offset: Some(42),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(123_456),
            target_retentions: [Some(0.9); 4],
            enforce_grade_order: true,
        };
        let curve = ReviewCurve {
            ahead_logits: vec![],
            weights: vec![0.2, 0.3, 0.5],
            s90: Default::default(),
        };

        assert_eq!(
            current_curve_retrievability(&input, &curve),
            Some(predict_curve(&curve, 123_456.0))
        );
        input.current_elapsed_seconds = None;
        assert_eq!(
            current_curve_retrievability(&input, &curve),
            Some(predict_curve(&curve, 3.0 * SECONDS_PER_DAY as f32))
        );
        input.current_elapsed_days = Some(-1);
        assert_eq!(current_curve_retrievability(&input, &curve), None);
    }

    #[test]
    fn query_timestep_matches_stateful_output_without_retaining_state() {
        let values = (0..D_MODEL)
            .map(|index| (index as f32 - 64.0) / 128.0)
            .collect::<Vec<_>>();
        let r = values.iter().map(|value| value * 0.7).collect::<Vec<_>>();
        let k = values.iter().map(|value| value * -0.4).collect::<Vec<_>>();
        let v = values.iter().map(|value| value * 0.3).collect::<Vec<_>>();
        let w = values
            .iter()
            .map(|value| 0.85 + value * 0.05)
            .collect::<Vec<_>>();
        let a = values
            .iter()
            .map(|value| 0.5 + value * 0.1)
            .collect::<Vec<_>>();
        let k_deformed = values.iter().map(|value| value * -0.2).collect::<Vec<_>>();
        let state = (0..HEADS * HEAD_SIZE * HEAD_SIZE)
            .map(|index| (index % 31) as f32 * 0.002 - 0.03)
            .collect::<Vec<_>>();
        let zero_state = vec![0.0; state.len()];

        for query_state in [None, Some(state.as_slice())] {
            let stateful_state = query_state.unwrap_or(&zero_state);
            let (expected, _) = single_timestep(&r, &k, &v, &w, &a, &k_deformed, stateful_state);
            let mut actual = [0.0; D_MODEL];
            let mut next_row = [0.0; HEAD_SIZE];
            single_timestep_query_into(
                &r,
                &k,
                &v,
                &w,
                &a,
                &k_deformed,
                query_state,
                &mut actual,
                &mut next_row,
            );

            assert_eq!(actual.as_slice(), expected);

            let mut fast = [0.0; D_MODEL];
            single_timestep_query_fast_into(
                &r,
                &k,
                &v,
                &w,
                &a,
                &k_deformed,
                query_state,
                &mut fast,
            );
            let max_delta = fast
                .iter()
                .zip(&expected)
                .map(|(fast, expected)| (fast - expected).abs())
                .fold(0.0_f32, f32::max);
            assert!(max_delta <= 1e-6, "max fast timestep delta: {max_delta}");
        }
    }

    #[test]
    fn batched_retrievability_matches_scalar_query_path() {
        let model_path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin");
        assert!(model_path.exists(), "RWKV parity model is missing");

        fn input(index: usize, is_query: bool) -> ReviewInput {
            ReviewInput {
                card_id: 10_000 + (index % 97) as i64,
                note_id: Some(20_000 + (index % 53) as i64),
                deck_id: Some(30_000 + (index % 7) as i64),
                preset_id: Some(40_000 + (index % 3) as i64),
                is_query,
                ease: (!is_query).then_some((index % 4 + 1) as u8),
                duration_millis: (!is_query).then_some(500 + index as i64 % 2_000),
                card_type: Some((index % 3) as i64),
                day_offset: Some((index / 11) as i64),
                current_elapsed_days: Some((index % 31) as i64),
                current_elapsed_seconds: Some((index % 31 * 86_400) as i64),
                target_retentions: [Some(0.9); 4],
                enforce_grade_order: true,
            }
        }

        let mut inference = RwkvInference::load(model_path, 0.9, 36_500).unwrap();
        inference
            .warm_up_reviews((0..257).map(|index| input(index, false)).collect(), false)
            .unwrap();
        let inputs = (0..129)
            .map(|index| input(index + 257, true))
            .collect::<Vec<_>>();
        let features = inputs
            .iter()
            .map(|input| inference.features.features_for(input))
            .collect::<Vec<_>>();
        let items = inputs
            .iter()
            .zip(&features)
            .map(|(input, features)| ReviewPredictionQueryRef {
                features,
                state: inference.warm_up_states.state_ref(input),
            })
            .collect::<Vec<_>>();

        let scalar = inference
            .model
            .review_retrievability_query_refs_scalar(&items);
        assert!(inference
            .model
            .review_retrievability_query_refs_batched(&[])
            .is_empty());
        let batched = inference
            .model
            .review_retrievability_query_refs_batched(&items);
        let max_delta = |batched: &[f32]| {
            scalar
                .iter()
                .zip(batched)
                .map(|(scalar, batched)| (scalar - batched).abs())
                .fold(0.0_f32, f32::max)
        };
        let delta = max_delta(&batched);
        assert!(delta <= 1e-6, "max batch prediction delta: {delta}");

        // The AVX2/FMA kernels change the summation order and fuse
        // multiply-adds, so they stay within float noise of the scalar path.
        #[cfg(target_arch = "x86_64")]
        if let Some(simd) = x86_simd_test_switch::with_simd(2, || {
            inference
                .model
                .review_retrievability_query_refs_batched(&items)
        }) {
            let delta = max_delta(&simd);
            assert!(delta <= 1e-5, "max AVX2/FMA prediction delta: {delta}");
        }
    }

    #[test]
    fn batched_normalization_preserves_scalar_bits() {
        for (dim, groups) in [(16, 1), (128, 1), (128, 4), (128, 8), (256, 1), (512, 1)] {
            let norm = Norm {
                dim,
                groups,
                eps: 1e-5,
                weight: (0..dim).map(|i| (i as f32 * 0.17).sin()).collect(),
                bias: (0..dim).map(|i| (i as f32 * 0.03).cos()).collect(),
            };
            for rows in [0, 1, 3, 4, 5, 127, 128, 129] {
                for magnitude in [0.0, 1e-8, 1.0, 1e4] {
                    let input = (0..rows * dim)
                        .map(|i| (i as f32 * 0.37).sin() * magnitude)
                        .collect::<Vec<_>>();
                    let expected = input
                        .chunks(dim)
                        .flat_map(|row| norm.apply(row))
                        .map(f32::to_bits)
                        .collect::<Vec<_>>();
                    let mut actual = Vec::new();
                    norm.apply_batch(&input, rows, &mut actual);
                    assert_eq!(
                        actual.into_iter().map(f32::to_bits).collect::<Vec<_>>(),
                        expected,
                        "dim={dim} groups={groups} rows={rows} magnitude={magnitude}"
                    );
                }
            }
        }
    }

    #[test]
    fn query_decay_matches_original_transform() {
        let mut max_delta = 0.0_f32;
        let values = (-100_000..=100_000).map(|i| i as f32 / 1_000.0).chain([
            f32::NEG_INFINITY,
            f32::INFINITY,
            -f32::MAX,
            f32::MAX,
            -0.0,
            f32::MIN_POSITIVE,
        ]);
        for value in values {
            let decay = -0.5 - softplus(-value);
            let expected = (-decay.exp()).exp();
            let actual = query_decay(value);
            assert!(actual.is_finite() && (0.0..=1.0).contains(&actual));
            max_delta = max_delta.max((actual - expected).abs());
        }
        assert!(max_delta <= 1.2e-7, "maximum decay delta: {max_delta}");
        assert!(query_decay(f32::NAN).is_nan());
    }

    #[test]
    fn restore_serialized_warm_up_state_replaces_and_removes_scopes() {
        let serialized = serialize_module_state(&ModuleState {
            layers: vec![LayerState {
                time: Some(TimeState {
                    x_shift: vec![1.0, 2.0],
                    matrix: vec![3.0, 4.0],
                }),
                channel_shift: Some(vec![5.0, 6.0]),
            }],
        });
        let populated = ReviewStateOwned {
            card: Some(serialized.clone()),
            note: Some(serialized.clone()),
            deck: Some(serialized.clone()),
            preset: Some(serialized.clone()),
            global: Some(serialized.clone()),
        };
        let mut states = ReviewStateMaps::new(PUBLISHED_MODEL_ID_PIPELINE);

        states
            .restore_serialized(1, Some(2), Some(3), Some(4), &populated)
            .unwrap();

        assert_eq!(
            serialize_module_state(states.card.get(&1).unwrap()),
            serialized
        );
        assert_eq!(
            serialize_module_state(states.note.get(&2).unwrap()),
            serialized
        );
        assert_eq!(
            serialize_module_state(states.deck.get(&3).unwrap()),
            serialized
        );
        assert_eq!(
            serialize_module_state(states.preset.get(&4).unwrap()),
            serialized
        );
        assert_eq!(
            serialize_module_state(states.global.as_ref().unwrap()),
            serialized
        );

        states
            .restore_serialized(
                1,
                Some(2),
                Some(3),
                Some(4),
                &ReviewStateOwned {
                    card: None,
                    note: None,
                    deck: None,
                    preset: None,
                    global: None,
                },
            )
            .unwrap();

        assert!(states.card.is_empty());
        assert!(states.note.is_empty());
        assert!(states.deck.is_empty());
        assert!(states.preset.is_empty());
        assert!(states.global.is_none());
    }

    #[test]
    fn workload_review_counts_are_monotonic_by_target_dr() {
        fn point(review_count: u32) -> RwkvWorkloadSimulationPoint {
            RwkvWorkloadSimulationPoint {
                memorized: 0.0,
                weighted_memorized: 0.0,
                cost: 0.0,
                review_count,
            }
        }

        let mut points = vec![
            (31, point(8)),
            (30, point(10)),
            (34, point(12)),
            (33, point(11)),
        ];

        enforce_monotonic_workload_review_counts(&mut points);

        assert_eq!(points[0].1.review_count, 10);
        assert_eq!(points[1].1.review_count, 10);
        assert_eq!(points[2].1.review_count, 12);
        assert_eq!(points[3].1.review_count, 11);
    }

    #[test]
    fn feature_state_scales_duration_from_milliseconds() {
        let features = FeatureState::new(PUBLISHED_MODEL_ID_PIPELINE);
        let input = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1234),
            card_type: Some(2),
            day_offset: Some(42),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [None; 4],
            enforce_grade_order: true,
        };

        let values = features.features_for(&input);

        assert_eq!(values[8], scale_duration(1234.0));

        let query_values = features.features_for(&ReviewInput {
            is_query: true,
            ease: None,
            duration_millis: None,
            ..input
        });
        assert_eq!(query_values[8], 0.0);
    }

    #[test]
    fn feature_state_scales_dataset_review_state() {
        let input = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1234),
            card_type: Some(2),
            day_offset: Some(42),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [None; 4],
            enforce_grade_order: true,
        };

        for (state, expected_scaled_state) in [(2, 0.0), (4, 2.0), (5, 3.0)] {
            let values =
                FeatureState::new(PUBLISHED_MODEL_ID_PIPELINE).features_for(&ReviewInput {
                    card_type: Some(state),
                    ..input.clone()
                });
            assert_eq!(values[22], expected_scaled_state);
        }

        let query_values =
            FeatureState::new(PUBLISHED_MODEL_ID_PIPELINE).features_for(&ReviewInput {
                is_query: true,
                ease: None,
                card_type: Some(4),
                ..input
            });
        assert_eq!(query_values[22], 0.0);
    }

    #[test]
    fn feature_state_normalizes_day_offset_to_first_raw_review_day() {
        let mut features = FeatureState::new(PUBLISHED_MODEL_ID_PIPELINE);
        let first = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1234),
            card_type: Some(2),
            day_offset: Some(100),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [None; 4],
            enforce_grade_order: true,
        };
        let second = ReviewInput {
            card_id: 124,
            note_id: Some(457),
            day_offset: Some(101),
            ..first.clone()
        };

        features.store_review(&first);
        let values = features.features_for(&second);

        assert_eq!(features.first_day_offset, Some(100));
        assert_eq!(values[17], -2.0 / 3.0);
    }

    fn id_test_input() -> ReviewInput {
        ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1234),
            card_type: Some(2),
            day_offset: Some(42),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [None; 4],
            enforce_grade_order: true,
        }
    }

    /// A review without a note, deck or preset (a deleted card's) has the
    /// three flags set. Under the shipped model's id pipeline (int32) it has
    /// the codes and streams of the one placeholder note, deck and preset all
    /// such reviews share; under the int64 pipeline its note is a
    /// placeholder of its own per card, while deck and preset stay shared
    /// (spec sched.rwkv-replay-deleted-cards).
    #[test]
    fn missing_ids_encode_as_the_model_versions_placeholders() {
        let input = ReviewInput {
            note_id: None,
            deck_id: None,
            preset_id: None,
            ..id_test_input()
        };
        let other = ReviewInput {
            card_id: 124,
            ..input.clone()
        };
        assert_eq!(PUBLISHED_MODEL_ID_PIPELINE, RwkvIdPipeline::Int32);
        for ids in [RwkvIdPipeline::Int32, RwkvIdPipeline::Int64] {
            let features = FeatureState::new(ids);
            let values = features.features_for(&input);
            let other_values = features.features_for(&other);
            assert_eq!(&values[13..16], &[1.0, 1.0, 1.0]);
            assert_eq!(
                &values[48..56],
                &id_encoding(IdKind::Deck, ID_PLACEHOLDER)[..]
            );
            assert_eq!(
                &values[56..64],
                &id_encoding(IdKind::Preset, ID_PLACEHOLDER)[..]
            );
            // deck and preset are shared in both pipelines
            assert_eq!(&other_values[48..64], &values[48..64]);
            assert_eq!(input.deck_key(), ID_PLACEHOLDER);
            assert_eq!(input.preset_key(), ID_PLACEHOLDER);
            let (note, other_note) = (input.note_key(ids), other.note_key(ids));
            assert_eq!(&values[36..48], &id_encoding(IdKind::Note, note)[..]);
            match ids {
                RwkvIdPipeline::Int32 => {
                    assert_eq!((note, other_note), (ID_PLACEHOLDER, ID_PLACEHOLDER));
                    assert_eq!(&other_values[36..48], &values[36..48]);
                }
                RwkvIdPipeline::Int64 => {
                    assert_eq!(
                        (note, other_note),
                        (ID_PLACEHOLDER + 123, ID_PLACEHOLDER + 124)
                    );
                    assert_ne!(&other_values[36..48], &values[36..48]);
                }
            }
        }
        // a real note is its own in both
        assert_eq!(id_test_input().note_key(RwkvIdPipeline::Int64), 456);
    }

    /// The reviews of deleted cards advance real placeholder streams, as
    /// training streamed its placeholder entities (spec
    /// sched.rwkv-replay-deleted-cards): one shared note, deck and preset
    /// under the shipped model's pipeline; a note per card, deck and preset
    /// shared, under the int64 one. Before, each read a fresh state and kept
    /// nothing.
    #[test]
    fn reviews_without_ids_stream_through_the_model_versions_placeholders() {
        let Some(model_path) = embedded_weights_path() else {
            return;
        };
        let reviews = [200, 201]
            .map(|card_id| ReviewInput {
                card_id,
                note_id: None,
                deck_id: None,
                preset_id: None,
                ..id_test_input()
            })
            .to_vec();
        for (ids, notes) in [
            (RwkvIdPipeline::Int32, vec![ID_PLACEHOLDER]),
            (
                RwkvIdPipeline::Int64,
                vec![ID_PLACEHOLDER + 200, ID_PLACEHOLDER + 201],
            ),
        ] {
            let mut inference = RwkvInference::load(model_path.clone(), 0.9, 36_500).unwrap();
            if ids != inference.model.ids {
                Arc::get_mut(&mut inference.model).unwrap().ids = ids;
                inference.features = FeatureState::new(ids);
                inference.warm_up_states = ReviewStateMaps::new(ids);
            }
            inference.warm_up_reviews(reviews.clone(), false).unwrap();
            let states = &inference.warm_up_states;
            let mut note_keys: Vec<i64> = states.note.keys().copied().collect();
            note_keys.sort_unstable();
            assert_eq!(note_keys, notes);
            for map in [&states.deck, &states.preset] {
                assert_eq!(map.keys().copied().collect::<Vec<_>>(), [ID_PLACEHOLDER]);
            }
            assert_eq!(states.card.len(), 2);
        }
    }

    /// An id code is torch's `randint(0, 4, (dim,), generator=g) - 1.5` with
    /// `g = torch.Generator().manual_seed(id_code_seed(kind, id))` (spec
    /// sched.rwkv-id-codes). The expected values come from torch 2.x itself.
    #[test]
    fn id_codes_match_torch_seeded_by_the_id() {
        assert_eq!(id_code_seed(IdKind::Card, 123), 1_658_732_843);
        assert_eq!(id_code_seed(IdKind::Note, ID_PLACEHOLDER), 1_456_629_865);
        let values = FeatureState::new(PUBLISHED_MODEL_ID_PIPELINE).features_for(&id_test_input());
        assert_eq!(
            &values[24..36],
            &[1.5, 1.5, 1.5, -0.5, -0.5, -1.5, -1.5, 1.5, 0.5, 0.5, -1.5, 0.5]
        );
        assert_eq!(
            &values[36..48],
            &[0.5, 0.5, 1.5, -1.5, 0.5, 1.5, -0.5, 1.5, 1.5, -0.5, -0.5, -0.5]
        );
        assert_eq!(
            &values[48..56],
            &[-1.5, -1.5, -1.5, -1.5, -1.5, -0.5, -1.5, -1.5]
        );
        assert_eq!(
            &values[56..64],
            &[0.5, -0.5, 1.5, -0.5, -0.5, 1.5, 1.5, -0.5]
        );
        // the kind is part of the seed: a deck and a preset with one id
        // differ
        assert_eq!(
            id_encoding(IdKind::Deck, 10),
            [1.5, -1.5, 1.5, -1.5, -1.5, -1.5, -0.5, -1.5]
        );
        assert_eq!(
            id_encoding(IdKind::Note, ID_PLACEHOLDER),
            [1.5, 1.5, -1.5, 1.5, -0.5, -0.5, -1.5, -1.5, -1.5, 1.5, 1.5, 1.5]
        );
    }

    /// An entity's code does not depend on which entities came first, and a
    /// query leaves the state as it found it: a query of an unseen card
    /// before a replay changes no later feature (spec sched.rwkv-id-codes).
    #[test]
    fn id_codes_do_not_depend_on_order_and_queries_change_nothing() {
        let first = id_test_input();
        let second = ReviewInput {
            card_id: 124,
            note_id: Some(457),
            deck_id: Some(790),
            preset_id: Some(11),
            ..first.clone()
        };
        let unseen_query = ReviewInput {
            card_id: 999,
            note_id: Some(998),
            deck_id: Some(997),
            is_query: true,
            ease: None,
            duration_millis: None,
            ..first.clone()
        };

        let mut plain = FeatureState::new(PUBLISHED_MODEL_ID_PIPELINE);
        plain.store_review(&first);
        let plain_second = plain.features_for(&second);

        let mut queried = FeatureState::new(PUBLISHED_MODEL_ID_PIPELINE);
        queried.features_for(&unseen_query);
        queried.store_review(&first);
        queried.features_for(&unseen_query);
        assert!(!queried.id_encodings.contains_key(&(IdKind::Card, 999)));
        assert_eq!(queried.features_for(&second), plain_second);

        // the second review's codes are its own, met first or second
        let alone = FeatureState::new(PUBLISHED_MODEL_ID_PIPELINE).features_for(&second);
        assert_eq!(&alone[24..64], &plain_second[24..64]);
    }

    #[test]
    fn feature_state_initializes_today_like_benchmark() {
        let features = FeatureState::new(PUBLISHED_MODEL_ID_PIPELINE);
        let input = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1234),
            card_type: Some(2),
            day_offset: Some(0),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [None; 4],
            enforce_grade_order: true,
        };

        let values = features.features_for(&input);

        assert_eq!(features.today, -1);
        assert_eq!(values[21], scale_cum_reviews_today(0));
    }

    #[test]
    fn feature_state_cache_round_trips_without_id_codes() {
        let mut features = FeatureState::new(PUBLISHED_MODEL_ID_PIPELINE);
        let input = id_test_input();
        features.store_review(&input);
        let next_input = ReviewInput {
            card_id: 124,
            note_id: Some(457),
            deck_id: Some(790),
            preset_id: Some(11),
            ..input.clone()
        };
        let expected = [
            features.features_for(&input),
            features.features_for(&next_input),
        ];

        let mut cache = Vec::new();
        features.write_cache_state(&mut cache);
        assert_eq!(cache.len(), features.cache_state_len());
        let mut cursor = Cursor::new(&cache);
        let restored =
            FeatureState::read_cache_state(&mut cursor, PUBLISHED_MODEL_ID_PIPELINE).unwrap();
        cursor.expect_end().unwrap();

        assert!(restored.id_encodings.is_empty());
        assert_eq!(restored.features_for(&input), expected[0]);
        assert_eq!(restored.features_for(&next_input), expected[1]);
    }

    // Pins spec/scheduling.md#sched.rwkv-live-learning-start-fresh: a forgotten
    // card gets the features of a card the state never saw, apart from its own
    // id code; the counters of the whole stream stay.
    #[test]
    fn a_forgotten_card_has_the_features_of_an_unseen_card() {
        let input = |card_id, day_offset, elapsed_days: i64, card_type| ReviewInput {
            card_id,
            note_id: Some(card_id + 1),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1200),
            card_type: Some(card_type),
            day_offset: Some(day_offset),
            current_elapsed_days: Some(elapsed_days),
            current_elapsed_seconds: Some(elapsed_days * 86_400),
            target_retentions: [None; 4],
            enforce_grade_order: true,
        };
        let mut features = FeatureState::new(PUBLISHED_MODEL_ID_PIPELINE);
        features.store_review(&input(123, 40, -1, 0));
        features.store_review(&input(5, 41, -1, 0));
        features.store_review(&input(123, 45, 5, 2));
        let before = features.review_index;

        assert!(features.forget_card(123));
        assert!(!features.forget_card(123));

        assert_eq!(features.review_index, before);
        assert!(features.card_set.contains_key(&5));
        let unseen = features.clone();
        let forgotten = features.features_for(&input(123, 50, -1, 0));
        let never_seen = unseen.features_for(&input(777, 50, -1, 0));
        // the elapsed, rating, count and state features, then (after the
        // card, note, deck and preset codes) the day features
        let ids_end = 24
            + [IdKind::Card, IdKind::Note, IdKind::Deck, IdKind::Preset]
                .map(id_encoding_dim)
                .iter()
                .sum::<usize>();
        assert_eq!(forgotten[..24], never_seen[..24]);
        assert_eq!(forgotten[ids_end..], never_seen[ids_end..]);
    }

    #[test]
    fn feature_state_for_card_restores_review_mutations() {
        let mut features = FeatureState::new(PUBLISHED_MODEL_ID_PIPELINE);
        let before = features.state_for_card(123);
        let input = ReviewInput {
            card_id: 123,
            note_id: Some(456),
            deck_id: Some(789),
            preset_id: Some(10),
            is_query: false,
            ease: Some(3),
            duration_millis: Some(1200),
            card_type: Some(2),
            day_offset: Some(42),
            current_elapsed_days: Some(3),
            current_elapsed_seconds: Some(259_200),
            target_retentions: [None; 4],
            enforce_grade_order: true,
        };

        features.store_review(&input);
        assert_eq!(features.review_index, 1);
        assert!(features.card_set.contains_key(&123));
        assert_eq!(features.last_i.get(&123), Some(&0));
        assert_eq!(features.card_elapsed_days_cumulative.get(&123), Some(&3));
        assert_eq!(
            features.card_elapsed_seconds_cumulative.get(&123),
            Some(&259_200)
        );

        features.restore_state(&before);
        assert_eq!(features.review_index, 0);
        assert_eq!(features.today, -1);
        assert!(!features.card_set.contains_key(&123));
        assert!(!features.last_i.contains_key(&123));
        assert!(!features.last_new_cards.contains_key(&123));
        assert!(!features.card_first_day_offset.contains_key(&123));
        assert!(!features.card_elapsed_days_cumulative.contains_key(&123));
        assert!(!features.card_elapsed_seconds_cumulative.contains_key(&123));
        assert_eq!(features.previous_day_offset, None);
        assert_eq!(features.today_reviews, 0);
        assert_eq!(features.today_new_cards, 0);
    }

    #[test]
    #[ignore]
    fn rwkv_state_compression_metrics() {
        let Ok(collection_path) = env::var("ANKI_RWKV_STATE_COMPRESSION_COLLECTION") else {
            eprintln!("set ANKI_RWKV_STATE_COMPRESSION_COLLECTION to a copied collection.anki2");
            return;
        };
        let weights_path = env::var("ANKI_RWKV_STATE_COMPRESSION_MODEL")
            .unwrap_or_else(|_| "qt/aqt/rwkv_inference/RWKV_trained_on_5000_10000.bin".into());
        let limit = env_usize("ANKI_RWKV_STATE_COMPRESSION_LIMIT", 5_000);
        let power_iterations = env_usize("ANKI_RWKV_STATE_COMPRESSION_POWER_ITERS", 8);
        let selected_configs = env::var("ANKI_RWKV_STATE_COMPRESSION_CONFIGS")
            .ok()
            .map(|configs| {
                configs
                    .split(',')
                    .map(str::trim)
                    .filter(|config| !config.is_empty())
                    .map(str::to_string)
                    .collect::<Vec<_>>()
            })
            .filter(|configs| !configs.is_empty());
        let target_deck_ids = env_i64s("ANKI_RWKV_STATE_COMPRESSION_TARGET_DECK_IDS");

        let load_start = Instant::now();
        let (reviews, outcomes, target_reviews) =
            load_metric_reviews(&collection_path, limit, &target_deck_ids)
                .expect("failed to load metric reviews");
        assert!(!reviews.is_empty(), "no eligible reviews loaded");
        eprintln!(
            "rwkv_state_compression_metric_inputs reviews={} limit={} load_ms={:.1}",
            reviews.len(),
            limit,
            load_start.elapsed().as_secs_f64() * 1000.0
        );
        if let Some(configs) = &selected_configs {
            eprintln!(
                "rwkv_state_compression_metric_configs configs={}",
                configs.join(",")
            );
        }

        let configs = [
            ("raw", None),
            (
                "shift-int4",
                Some(StateCompression {
                    shift_bits: Some(4),
                    matrix_rank: HEAD_SIZE,
                    matrix_bits: None,
                    power_iterations,
                }),
            ),
            (
                "rank4-int8-shift-int4",
                Some(StateCompression {
                    shift_bits: Some(4),
                    matrix_rank: 4,
                    matrix_bits: Some(8),
                    power_iterations,
                }),
            ),
            (
                "rank4-int8",
                Some(StateCompression {
                    shift_bits: None,
                    matrix_rank: 4,
                    matrix_bits: Some(8),
                    power_iterations,
                }),
            ),
            (
                "rank4-int4-shift-int4",
                Some(StateCompression {
                    shift_bits: Some(4),
                    matrix_rank: 4,
                    matrix_bits: Some(4),
                    power_iterations,
                }),
            ),
            (
                "rank2-int8-shift-int4",
                Some(StateCompression {
                    shift_bits: Some(4),
                    matrix_rank: 2,
                    matrix_bits: Some(8),
                    power_iterations,
                }),
            ),
            (
                "rank2-int8",
                Some(StateCompression {
                    shift_bits: None,
                    matrix_rank: 2,
                    matrix_bits: Some(8),
                    power_iterations,
                }),
            ),
            (
                "rank2-int4-shift-int4",
                Some(StateCompression {
                    shift_bits: Some(4),
                    matrix_rank: 2,
                    matrix_bits: Some(4),
                    power_iterations,
                }),
            ),
            (
                "rank1-int4-shift-int4",
                Some(StateCompression {
                    shift_bits: Some(4),
                    matrix_rank: 1,
                    matrix_bits: Some(4),
                    power_iterations,
                }),
            ),
        ];

        let mut baseline = None;
        println!(
            "config,reviews,log_loss,delta_log_loss,rmse_bins,delta_rmse_bins,rmse_bins_weighted,delta_rmse_bins_weighted,elapsed_ms"
        );
        for (label, compression) in configs {
            if let Some(configs) = &selected_configs {
                if !configs.iter().any(|config| config == label) {
                    continue;
                }
            }
            let start = Instant::now();
            let mut inference = RwkvInference::load(PathBuf::from(&weights_path), 0.9, 36_500)
                .expect("RWKV load failed");
            let predictions =
                inference.warm_up_reviews_with_state_compression(&reviews, compression);
            assert_eq!(predictions.len(), outcomes.len());
            let target_predictions = predictions
                .iter()
                .zip(&target_reviews)
                .filter_map(|(prediction, target)| target.then_some(*prediction))
                .collect::<Vec<_>>();
            let target_outcomes = outcomes
                .iter()
                .zip(&target_reviews)
                .filter_map(|(outcome, target)| target.then_some(*outcome))
                .collect::<Vec<_>>();
            let metrics =
                MetricAccumulator::from_predictions(&target_predictions, &target_outcomes);
            let baseline_metrics = baseline.get_or_insert(metrics);
            println!(
                "{label},{},{:.9},{:.9},{:.9},{:.9},{:.9},{:.9},{:.1}",
                target_outcomes.len(),
                metrics.log_loss,
                metrics.log_loss - baseline_metrics.log_loss,
                metrics.rmse_bins,
                metrics.rmse_bins - baseline_metrics.rmse_bins,
                metrics.rmse_bins_weighted,
                metrics.rmse_bins_weighted - baseline_metrics.rmse_bins_weighted,
                start.elapsed().as_secs_f64() * 1000.0,
            );
        }
    }

    fn env_usize(name: &str, default: usize) -> usize {
        env::var(name)
            .ok()
            .and_then(|value| value.parse().ok())
            .unwrap_or(default)
    }

    fn env_i64s(name: &str) -> Vec<i64> {
        env::var(name)
            .ok()
            .map(|values| {
                values
                    .split(',')
                    .filter_map(|value| value.trim().parse().ok())
                    .collect()
            })
            .unwrap_or_default()
    }

    fn load_metric_reviews(
        collection_path: &str,
        limit: usize,
        target_deck_ids: &[i64],
    ) -> rusqlite::Result<(Vec<ReviewInput>, Vec<bool>, Vec<bool>)> {
        let connection = Connection::open_with_flags(
            collection_path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )?;
        let created_secs: i64 =
            connection.query_row("select crt from col limit 1", [], |row| row.get(0))?;
        let preset_by_deck = deck_config_ids_by_deck(&connection)?;
        let mut previous_review_id_by_card = HashMap::new();
        let mut reviews = Vec::new();
        let mut outcomes = Vec::new();
        let mut target_reviews = Vec::new();
        let sql = format!(
            "\
with eligible as (
  select
    r.id,
    r.cid,
    c.nid,
    c.did,
    r.ease,
    r.time,
    r.type,
    lag(r.type) over (partition by r.cid order by r.id) as previous_type
  from revlog r
  join cards c on c.id = r.cid
  where r.ease between 1 and 4
    and r.type in (0, 1, 2, 3, 4, 5)
    and not (r.type = 3 and r.factor = 0)
), learning_starts as (
  select cid, max(id) as start_id
  from eligible
  where type = 0 and (previous_type is null or previous_type != 0)
  group by cid
), last_forgets as (
  select cid, max(id) as forget_id
  from revlog
  where type = 4 and factor = 0
  group by cid
), fallback_starts as (
  select e.cid as cid, min(e.id) as start_id
  from eligible e
  left join learning_starts l on l.cid = e.cid
  left join last_forgets f on f.cid = e.cid
  where l.cid is null
    and (f.forget_id is null or e.id > f.forget_id)
  group by e.cid
), retained_starts as (
  select cid, start_id from learning_starts
  union all
  select cid, start_id from fallback_starts
)
select e.id, e.cid, e.nid, e.did, e.ease, e.time, e.type, e.id = s.start_id
from eligible e
join retained_starts s on s.cid = e.cid
where e.id >= s.start_id
order by e.id, e.cid
{}",
            if limit > 0 { "limit ?" } else { "" }
        );
        let mut statement = connection.prepare(&sql)?;
        let mut rows = if limit > 0 {
            statement.query([limit as i64])?
        } else {
            statement.query([])?
        };
        while let Some(row) = rows.next()? {
            let review_id: i64 = row.get(0)?;
            let card_id: i64 = row.get(1)?;
            let note_id: i64 = row.get(2)?;
            let deck_id: i64 = row.get(3)?;
            let ease: u8 = row.get(4)?;
            let duration_millis: i64 = row.get(5)?;
            let review_kind: i64 = row.get(6)?;
            let is_learning_start: bool = row.get(7)?;
            let previous_review_id = previous_review_id_by_card.insert(card_id, review_id);
            let elapsed_seconds = previous_review_id
                .map(|previous| ((review_id - previous) / 1000).max(0))
                .unwrap_or(-1);
            let review_secs = review_id / 1000;
            let day_offset = ((review_secs - created_secs).max(0)) / SECONDS_PER_DAY;
            let elapsed_days = previous_review_id.map_or(-1, |previous| {
                let previous_secs = previous / 1000;
                let previous_day_offset = ((previous_secs - created_secs).max(0)) / SECONDS_PER_DAY;
                (day_offset - previous_day_offset).max(0)
            });
            let preset_id = preset_by_deck.get(&deck_id).copied().flatten();
            outcomes.push(ease != 1);
            target_reviews.push(target_deck_ids.is_empty() || target_deck_ids.contains(&deck_id));
            reviews.push(ReviewInput {
                card_id,
                note_id: Some(note_id),
                deck_id: Some(deck_id),
                preset_id,
                is_query: false,
                ease: Some(ease),
                duration_millis: Some(duration_millis),
                card_type: Some(historical_state(review_kind, is_learning_start)),
                day_offset: Some(day_offset),
                current_elapsed_days: Some(elapsed_days),
                current_elapsed_seconds: Some(elapsed_seconds),
                target_retentions: [None; 4],
                enforce_grade_order: true,
            });
        }
        Ok((reviews, outcomes, target_reviews))
    }

    fn historical_state(review_kind: i64, is_learning_start: bool) -> i64 {
        if is_learning_start {
            0
        } else {
            review_kind + 1
        }
    }

    #[derive(Clone, Copy)]
    struct MetricSummary {
        log_loss: f64,
        rmse_bins: f64,
        rmse_bins_weighted: f64,
    }

    struct MetricAccumulator {
        log_loss_sum: f64,
        bins: [MetricBin; 20],
    }

    #[derive(Clone, Copy, Default)]
    struct MetricBin {
        count: usize,
        prediction_sum: f64,
        outcome_sum: f64,
    }

    impl MetricAccumulator {
        fn from_predictions(predictions: &[f32], outcomes: &[bool]) -> MetricSummary {
            let mut accumulator = Self {
                log_loss_sum: 0.0,
                bins: [MetricBin::default(); 20],
            };
            for (&prediction, &outcome) in predictions.iter().zip(outcomes) {
                accumulator.record(prediction, outcome);
            }
            accumulator.finish(predictions.len())
        }

        fn record(&mut self, prediction: f32, outcome: bool) {
            let prediction = valid_probability(prediction as f64);
            let outcome_value = if outcome { 1.0 } else { 0.0 };
            self.log_loss_sum += -(outcome_value * prediction.ln()
                + (1.0 - outcome_value) * (1.0 - prediction).ln());
            let bin =
                ((prediction * self.bins.len() as f64).floor() as usize).min(self.bins.len() - 1);
            let metric_bin = &mut self.bins[bin];
            metric_bin.count += 1;
            metric_bin.prediction_sum += prediction;
            metric_bin.outcome_sum += outcome_value;
        }

        fn finish(self, count: usize) -> MetricSummary {
            let mut unweighted_sum = 0.0;
            let mut weighted_sum = 0.0;
            let mut bin_count = 0;
            for bin in self.bins {
                if bin.count == 0 {
                    continue;
                }
                let predicted = bin.prediction_sum / bin.count as f64;
                let observed = bin.outcome_sum / bin.count as f64;
                let squared = (predicted - observed).powi(2);
                unweighted_sum += squared;
                weighted_sum += squared * bin.count as f64;
                bin_count += 1;
            }
            MetricSummary {
                log_loss: self.log_loss_sum / count as f64,
                rmse_bins: (unweighted_sum / bin_count as f64).sqrt(),
                rmse_bins_weighted: (weighted_sum / count as f64).sqrt(),
            }
        }
    }

    fn valid_probability(value: f64) -> f64 {
        if value.is_finite() {
            value.clamp(1e-6, 1.0 - 1e-6)
        } else {
            0.5
        }
    }
}
