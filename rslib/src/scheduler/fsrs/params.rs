// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
use std::collections::HashMap;
use std::collections::HashSet;
use std::iter;
use std::path::Path;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::sync::Mutex;
use std::thread;
use std::time::Duration;

use anki_io::write_file;
use anki_proto::scheduler::ComputeFsrsParamsResponse;
use anki_proto::stats::revlog_entry;
use anki_proto::stats::Dataset;
use anki_proto::stats::DeckEntry;
use chrono::NaiveDate;
use chrono::NaiveTime;
use fsrs::compute_parameters;
use fsrs::evaluate_with_time_series_splits;
use fsrs::CombinedProgressState;
use fsrs::ComputeParametersInput;
use fsrs::ComputeParametersVersion;
use fsrs::FSRSItem;
use fsrs::FSRSReview;
use fsrs::ModelEvaluation;
use fsrs::FSRS;
use itertools::Itertools;
use prost::Message;
use rayon::prelude::*;

use crate::deckconfig::effective_fsrs7_params;
use crate::decks::immediate_parent_name;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::revlog::RevlogReviewKind;
use crate::scheduler::fsrs::curve::Fsrs7Curve;
use crate::search::Node;
use crate::search::SearchNode;
use crate::search::SortMode;
use crate::storage::FsrsReviewRetrievabilityCacheRow;
use crate::storage::FsrsReviewRetrievabilitySampleRole;

pub(crate) type Params = Vec<f32>;

const FSRS_VALIDATION_FOLDS: usize = 5;
/// Cards per call when the memory states of a replay are computed together.
/// The same size as the Total Knowledge replay uses.
const FSRS_MEMORY_STATE_BATCH: usize = 256;
const FSRS_CALIBRATION_PROGRESS_SCALE: usize = 1000;

pub(crate) fn ignore_revlogs_before_date_to_ms(
    ignore_revlogs_before_date: &String,
) -> Result<TimestampMillis> {
    Ok(match ignore_revlogs_before_date {
        s if s.is_empty() => 0,
        s => NaiveDate::parse_from_str(s.as_str(), "%Y-%m-%d")
            .or_else(|err| invalid_input!(err, "Error parsing date: {s}"))?
            .and_time(NaiveTime::from_hms_milli_opt(0, 0, 0, 0).unwrap())
            .and_utc()
            .timestamp_millis(),
    }
    .into())
}

pub(crate) fn ignore_revlogs_before_ms_from_config(config: &DeckConfig) -> Result<TimestampMillis> {
    ignore_revlogs_before_date_to_ms(&config.inner.ignore_revlogs_before_date)
}

pub struct ComputeParamsRequest<'t> {
    pub search: &'t str,
    pub ignore_revlogs_before_ms: TimestampMillis,
    pub current_preset: u32,
    pub total_presets: u32,
    pub current_params: &'t Params,
    pub num_of_relearning_steps: usize,
    pub health_check: bool,
    pub enable_scheduling_penalties: bool,
}

pub(crate) struct PreparedComputeParams {
    pub current_params: Params,
    pub num_of_relearning_steps: usize,
    pub enable_scheduling_penalties: bool,
    pub items: Vec<FSRSItem>,
    pub item_card_ids: Vec<i64>,
    pub item_revlog_ids: Vec<RevlogId>,
    pub fsrs_prediction_sources: Vec<FsrsReviewPredictionSource>,
    pub target_counts: TrainingTargetCounts,
}

pub(crate) struct PrepareComputeParamsInput<'a> {
    pub search: &'a str,
    pub ignore_revlogs_before: TimestampMillis,
    pub current_params: &'a [f32],
    pub num_of_relearning_steps: usize,
    pub enable_scheduling_penalties: bool,
}

#[derive(Default, Clone, Copy, Debug, PartialEq, Eq)]
pub enum ComputeParamsProgressPhase {
    #[default]
    OptimizingFsrsParams = 0,
}

pub(crate) struct FsrsReviewRetrievabilityProgress {
    training_progress: Arc<Mutex<CombinedProgressState>>,
    completed_validation_folds: Arc<AtomicUsize>,
}

/// r: retention
fn log_loss_adjustment(r: f32) -> f32 {
    0.623 * (4. * r * (1. - r)).powf(0.738)
}

/// r: retention
///
/// c: review count
fn rmse_adjustment(r: f32, c: u32) -> f32 {
    0.0135 / (r.powf(0.504) - 1.14) + 0.176 / ((c as f32 / 1000.).powf(0.825) + 2.22) + 0.101
}

#[derive(Clone)]
struct TrainingItemsForFsrs {
    items: Vec<FSRSItem>,
    card_ids: Option<Vec<i64>>,
    revlog_ids: Option<Vec<RevlogId>>,
    prediction_sources: Vec<FsrsReviewPredictionSource>,
}

impl TrainingItemsForFsrs {
    fn with_card_and_revlog_ids(
        items: Vec<FSRSItem>,
        card_ids: Vec<i64>,
        revlog_ids: Vec<RevlogId>,
        prediction_sources: Vec<FsrsReviewPredictionSource>,
    ) -> Self {
        debug_assert_eq!(items.len(), card_ids.len());
        debug_assert_eq!(items.len(), revlog_ids.len());
        Self {
            items,
            card_ids: Some(card_ids),
            revlog_ids: Some(revlog_ids),
            prediction_sources,
        }
    }

    fn slice(&self, start: usize, end: usize) -> Self {
        Self {
            items: self.items[start..end].to_vec(),
            card_ids: self.card_ids.as_ref().map(|ids| ids[start..end].to_vec()),
            revlog_ids: self.revlog_ids.as_ref().map(|ids| ids[start..end].to_vec()),
            prediction_sources: Vec::new(),
        }
    }

    fn target_counts(&self) -> TrainingTargetCounts {
        training_target_counts_from_items(&self.items)
    }
}

#[derive(Clone)]
pub(crate) struct FsrsReviewPredictionSource {
    reviews: Vec<FSRSReview>,
    targets: Vec<(RevlogId, usize)>,
}

#[derive(Clone)]
pub(crate) struct FsrsReviewPredictionContext {
    items: Vec<FSRSItem>,
    card_ids: Vec<i64>,
    revlog_ids: Vec<RevlogId>,
    sources: Vec<FsrsReviewPredictionSource>,
    num_relearning_steps: usize,
    enable_scheduling_penalties: bool,
}

impl FsrsReviewPredictionContext {
    pub(crate) fn from_prepared(prepared: &PreparedComputeParams) -> Self {
        Self {
            items: prepared.items.clone(),
            card_ids: prepared.item_card_ids.clone(),
            revlog_ids: prepared.item_revlog_ids.clone(),
            sources: prepared.fsrs_prediction_sources.clone(),
            num_relearning_steps: prepared.num_of_relearning_steps,
            enable_scheduling_penalties: prepared.enable_scheduling_penalties,
        }
    }
}

fn has_long_term_target(item: &FSRSItem) -> bool {
    item.reviews
        .last()
        .is_some_and(|review| review.delta_t >= 1.0)
}

fn training_search<'a>(search: &'a str, search_for_training: Option<&'a str>) -> &'a str {
    match search_for_training.map(str::trim) {
        Some(non_empty) if !non_empty.is_empty() => non_empty,
        _ => search,
    }
}

fn uses_external_evaluation(training_search: &str, evaluation_search: &str) -> bool {
    training_search != evaluation_search
}

fn training_target_counts_from_items(items: &[FSRSItem]) -> TrainingTargetCounts {
    let total_targets = items.len();
    let long_term_targets = items
        .iter()
        .filter(|item| has_long_term_target(item))
        .count();
    let short_term_targets = total_targets.saturating_sub(long_term_targets);
    TrainingTargetCounts {
        total_targets,
        long_term_targets,
        short_term_targets,
    }
}

fn health_check_passed_for_evaluated_targets(eval: ModelEvaluation, items: &[FSRSItem]) -> bool {
    let fsrs_items = items.len() as u32;
    if fsrs_items == 0 {
        return false;
    }
    let r = items.iter().fold(0, |passed, item| {
        passed
            + (item
                .reviews
                .last()
                .map(|reviews| reviews.rating)
                .unwrap_or(0)
                > 1) as u32
    }) as f32
        / fsrs_items as f32;
    let adjusted_log_loss = eval.log_loss / log_loss_adjustment(r);
    let adjusted_rmse = eval.rmse_bins / rmse_adjustment(r, fsrs_items);
    adjusted_log_loss <= 1.11 || adjusted_rmse <= 1.53
}

fn time_series_split_items(
    sorted_items: TrainingItemsForFsrs,
    n_splits: usize,
) -> Vec<(TrainingItemsForFsrs, TrainingItemsForFsrs)> {
    if sorted_items.items.is_empty() || n_splits == 0 {
        return vec![];
    }
    let total_items = sorted_items.items.len();
    let segment_size = total_items / (n_splits + 1);
    if segment_size == 0 {
        return vec![];
    }
    (0..n_splits)
        .map(|i| {
            let test_start = (i + 1) * segment_size;
            let test_end = if i == n_splits - 1 {
                total_items
            } else {
                (i + 2) * segment_size
            };
            (
                sorted_items.slice(0, test_start),
                sorted_items.slice(test_start, test_end),
            )
        })
        .collect()
}

fn evaluate_from_training_to_external_targets(
    ComputeParametersInput {
        training_config,
        train_set,
        card_ids,
        enable_short_term,
        enable_sched_penalties,
        model_version,
        num_relearning_steps,
        ..
    }: ComputeParametersInput,
    evaluation_set: Vec<FSRSItem>,
) -> Result<ModelEvaluation> {
    if train_set.is_empty() || evaluation_set.is_empty() {
        return Err(fsrs::FSRSError::NotEnoughData.into());
    }
    let parameters = compute_parameters(ComputeParametersInput {
        training_config,
        train_set,
        card_ids,
        progress: None,
        enable_short_term,
        enable_sched_penalties,
        model_version,
        num_relearning_steps,
    })?;
    Ok(FSRS::new(&parameters)?.evaluate(evaluation_set, |_| true)?)
}

pub(crate) fn compute_params_from_prepared(
    PreparedComputeParams {
        current_params,
        num_of_relearning_steps,
        enable_scheduling_penalties,
        items,
        item_card_ids,
        item_revlog_ids,
        fsrs_prediction_sources: _,
        target_counts: _,
    }: PreparedComputeParams,
    progress: Option<Arc<Mutex<CombinedProgressState>>>,
    health_check: bool,
) -> Result<ComputeFsrsParamsResponse> {
    let fsrs_items = items.len() as u32;
    if fsrs_items == 0 {
        return Ok(ComputeFsrsParamsResponse {
            params: current_params,
            fsrs_items,
            health_check_passed: None,
        });
    }

    let input = ComputeParametersInput {
        training_config: None,
        train_set: items.clone(),
        card_ids: Some(item_card_ids.clone()),
        progress: progress.clone(),
        enable_short_term: true,
        enable_sched_penalties: enable_scheduling_penalties,
        model_version: ComputeParametersVersion::Fsrs7,
        num_relearning_steps: Some(num_of_relearning_steps),
    };
    let params = compute_parameters(input)?;

    let health_check_items = TrainingItemsForFsrs::with_card_and_revlog_ids(
        items.clone(),
        item_card_ids.clone(),
        item_revlog_ids.clone(),
        Vec::new(),
    );
    let health_check_passed = if health_check && health_check_items.items.len() > 300 {
        evaluate_with_time_series_splits(
            ComputeParametersInput {
                training_config: None,
                train_set: health_check_items.items.clone(),
                card_ids: health_check_items.card_ids.clone(),
                progress: None,
                enable_short_term: true,
                enable_sched_penalties: enable_scheduling_penalties,
                model_version: ComputeParametersVersion::Fsrs7,
                num_relearning_steps: Some(num_of_relearning_steps),
            },
            |_| true,
        )
        .ok()
        .map(|eval| health_check_passed_for_evaluated_targets(eval, &health_check_items.items))
    } else {
        None
    };

    Ok(ComputeFsrsParamsResponse {
        params,
        fsrs_items,
        health_check_passed,
    })
}

fn calibration_progress_total(total_validation_folds: usize) -> usize {
    if total_validation_folds == 0 {
        1
    } else {
        total_validation_folds * FSRS_CALIBRATION_PROGRESS_SCALE
    }
}

fn calibration_progress_current(
    completed_validation_folds: usize,
    total_validation_folds: usize,
    training_progress: &CombinedProgressState,
) -> usize {
    if total_validation_folds == 0 {
        return 0;
    }

    let completed = completed_validation_folds.min(total_validation_folds);
    let fold_current = training_progress.current();
    let fold_total = training_progress.total();
    let current_fold_progress = if completed == total_validation_folds || fold_total == 0 {
        0
    } else {
        (fold_current * FSRS_CALIBRATION_PROGRESS_SCALE / fold_total)
            .min(FSRS_CALIBRATION_PROGRESS_SCALE.saturating_sub(1))
    };
    completed * FSRS_CALIBRATION_PROGRESS_SCALE + current_fold_progress
}

impl Collection {
    /// Note this does not return an error if there are less than 400 items -
    /// the caller should instead check the fsrs_items count in the return
    /// value.
    pub fn compute_params(
        &mut self,
        request: ComputeParamsRequest,
    ) -> Result<ComputeFsrsParamsResponse> {
        let ComputeParamsRequest {
            search,
            ignore_revlogs_before_ms: ignore_revlogs_before,
            current_preset,
            total_presets,
            current_params,
            num_of_relearning_steps,
            health_check,
            enable_scheduling_penalties,
        } = request;

        self.clear_progress();
        let prepared = self.prepare_compute_params(PrepareComputeParamsInput {
            search,
            ignore_revlogs_before,
            current_params,
            num_of_relearning_steps,
            enable_scheduling_penalties,
        })?;

        if prepared.items.is_empty() {
            return Ok(ComputeFsrsParamsResponse {
                params: current_params.to_vec(),
                fsrs_items: 0,
                health_check_passed: None,
            });
        }
        // adapt the progress handler to our built-in progress handling

        let create_progress_thread = || -> Result<_> {
            let mut anki_progress = self.new_progress_handler::<ComputeParamsProgress>();
            anki_progress.update(false, |p| {
                p.current_preset = current_preset;
                p.total_presets = total_presets;
            })?;
            let progress = CombinedProgressState::new_shared();
            let progress2 = progress.clone();
            let progress_thread = thread::spawn(move || {
                let mut finished = false;
                while !finished {
                    thread::sleep(Duration::from_millis(100));
                    let mut guard = progress.lock().unwrap();
                    if let Err(_err) = anki_progress.update(false, |s| {
                        s.total_iterations = guard.total() as u32;
                        s.current_iteration = guard.current() as u32;
                        s.reviews = prepared.target_counts.total_targets as u32;
                        s.long_term_reviews = prepared.target_counts.long_term_targets as u32;
                        s.short_term_reviews = prepared.target_counts.short_term_targets as u32;
                        finished = guard.finished();
                    }) {
                        guard.want_abort = true;
                        return;
                    }
                }
            });
            Ok((progress2, progress_thread))
        };

        let (progress, progress_thread) = create_progress_thread()?;
        let output = compute_params_from_prepared(prepared, Some(progress.clone()), health_check);
        progress_thread.join().ok();
        output
    }

    pub(crate) fn compute_fsrs_review_retrievability_calibration_cache(
        &mut self,
        params: &[f32],
        context: &FsrsReviewPredictionContext,
        include_validation_folds: bool,
    ) -> Result<u32> {
        self.clear_progress();
        let (progress, done, progress_thread) = self
            .create_fsrs_review_retrievability_progress_thread(context, include_validation_folds)?;
        let rows = fsrs_review_retrievability_cache_rows(
            params,
            context,
            include_validation_folds,
            Some(&progress),
        );
        done.store(true, Ordering::Release);
        progress_thread.join().ok();
        let rows = rows?;
        let stored = self
            .storage
            .set_fsrs_review_retrievability_predictions(&rows, "fsrs_calibration_recompute")?;
        tracing::debug!(
            predictions = stored,
            "stored FSRS review retrievability calibration cache"
        );
        Ok(stored as u32)
    }

    fn create_fsrs_review_retrievability_progress_thread(
        &self,
        context: &FsrsReviewPredictionContext,
        include_validation_folds: bool,
    ) -> Result<(
        FsrsReviewRetrievabilityProgress,
        Arc<AtomicBool>,
        thread::JoinHandle<()>,
    )> {
        let mut anki_progress = self.new_progress_handler::<ComputeParamsProgress>();
        let total_validation_folds = if include_validation_folds {
            FSRS_VALIDATION_FOLDS
        } else {
            0
        };
        let target_counts = training_target_counts_from_items(&context.items);
        let total_iterations = calibration_progress_total(total_validation_folds);
        anki_progress.update(false, |p| {
            p.current_iteration = 0;
            p.total_iterations = total_iterations as u32;
            p.reviews = target_counts.total_targets as u32;
            p.long_term_reviews = target_counts.long_term_targets as u32;
            p.short_term_reviews = target_counts.short_term_targets as u32;
            p.current_preset = 1;
            p.total_presets = 1;
        })?;

        let progress = FsrsReviewRetrievabilityProgress {
            training_progress: CombinedProgressState::new_shared(),
            completed_validation_folds: Arc::new(AtomicUsize::new(0)),
        };
        let done = Arc::new(AtomicBool::new(false));
        let training_progress = progress.training_progress.clone();
        let completed_validation_folds = progress.completed_validation_folds.clone();
        let done_for_thread = done.clone();
        let progress_thread = thread::spawn(move || {
            while !done_for_thread.load(Ordering::Acquire) {
                thread::sleep(Duration::from_millis(100));
                let mut guard = training_progress.lock().unwrap();
                let current_iterations = calibration_progress_current(
                    completed_validation_folds.load(Ordering::Acquire),
                    total_validation_folds,
                    &guard,
                );
                if anki_progress
                    .update(false, |s| {
                        s.current_iteration = current_iterations as u32;
                        s.total_iterations = total_iterations as u32;
                    })
                    .is_err()
                {
                    guard.want_abort = true;
                    return;
                }
            }
        });
        Ok((progress, done, progress_thread))
    }

    pub(crate) fn prepare_compute_params(
        &mut self,
        input: PrepareComputeParamsInput<'_>,
    ) -> Result<PreparedComputeParams> {
        let PrepareComputeParamsInput {
            search,
            ignore_revlogs_before,
            current_params,
            num_of_relearning_steps,
            enable_scheduling_penalties,
        } = input;
        let revlogs = self.revlog_for_srs(search)?;
        let training_items = fsrs_items_for_training(revlogs, ignore_revlogs_before);
        let target_counts = training_items.target_counts();
        let TrainingItemsForFsrs {
            items,
            card_ids,
            revlog_ids,
            prediction_sources,
        } = training_items;
        Ok(PreparedComputeParams {
            current_params: current_params.to_vec(),
            num_of_relearning_steps,
            enable_scheduling_penalties,
            items,
            item_card_ids: card_ids.unwrap_or_default(),
            item_revlog_ids: revlog_ids.unwrap_or_default(),
            fsrs_prediction_sources: prediction_sources,
            target_counts,
        })
    }

    pub(crate) fn revlog_for_srs(
        &mut self,
        search: impl TryIntoSearch,
    ) -> Result<Vec<RevlogEntry>> {
        let search = search.try_into_search()?;
        // a whole-collection search can match revlog entries of deleted cards, too
        if let Node::Group(nodes) = &search {
            if let &[Node::Search(SearchNode::WholeCollection)] = &nodes[..] {
                return self.storage.get_all_revlog_entries_in_card_order();
            }
        }
        self.search_cards_into_table(search, SortMode::NoOrder)?
            .col
            .storage
            .get_revlog_entries_for_searched_cards_in_card_order()
    }

    /// Used for exporting revlogs for algorithm research.
    pub fn export_dataset(&mut self, min_entries: usize, target_path: &Path) -> Result<()> {
        let revlog_entries = self.storage.get_revlog_entries_for_export_dataset()?;
        if revlog_entries.len() < min_entries {
            return Err(AnkiError::FsrsInsufficientData);
        }
        let revlogs = revlog_entries
            .into_iter()
            .map(revlog_entry_to_proto)
            .collect_vec();
        let cards = self.storage.get_all_card_entries()?;

        let decks_map = self.storage.get_decks_map()?;
        let deck_name_to_id: HashMap<String, DeckId> = decks_map
            .into_iter()
            .map(|(id, deck)| (deck.name.to_string(), id))
            .collect();

        let decks = self
            .storage
            .get_all_decks()?
            .into_iter()
            .filter_map(|deck| {
                if let Some(preset_id) = deck.config_id().map(|id| id.0) {
                    let parent_id = immediate_parent_name(&deck.name.to_string())
                        .and_then(|parent_name| deck_name_to_id.get(parent_name))
                        .map(|id| id.0)
                        .unwrap_or(0);
                    Some(DeckEntry {
                        id: deck.id.0,
                        parent_id,
                        preset_id,
                    })
                } else {
                    None
                }
            })
            .collect_vec();
        let next_day_at = self.timing_today()?.next_day_at.0;
        let dataset = Dataset {
            revlogs,
            cards,
            decks,
            next_day_at,
        };
        let data = dataset.encode_to_vec();
        write_file(target_path, data)?;
        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub fn evaluate_params(
        &mut self,
        search: &str,
        search_for_training: Option<&str>,
        ignore_revlogs_before: TimestampMillis,
        num_of_relearning_steps: usize,
        enable_scheduling_penalties: bool,
    ) -> Result<ModelEvaluation> {
        let training_search = training_search(search, search_for_training);
        let training_revlogs = self.revlog_for_srs(training_search)?;
        let evaluation_revlogs = if training_search == search {
            training_revlogs.clone()
        } else {
            self.revlog_for_srs(search)?
        };
        let training_items = fsrs_items_for_training(training_revlogs, ignore_revlogs_before);
        let evaluation_items = fsrs_items_for_training(evaluation_revlogs, ignore_revlogs_before);
        let target_counts = evaluation_items.target_counts();
        let mut anki_progress = self.new_progress_handler::<ComputeParamsProgress>();
        anki_progress.state.reviews = target_counts.total_targets as u32;
        anki_progress.state.long_term_reviews = target_counts.long_term_targets as u32;
        anki_progress.state.short_term_reviews = target_counts.short_term_targets as u32;
        // Ensure UI receives review counts even in paths that don't emit per-fold
        // progress.
        let _ = anki_progress.update(false, |_| {});

        let eval = if uses_external_evaluation(training_search, search) {
            evaluate_from_training_to_external_targets(
                ComputeParametersInput {
                    training_config: None,
                    train_set: training_items.items,
                    card_ids: training_items.card_ids,
                    progress: None,
                    enable_short_term: true,
                    enable_sched_penalties: enable_scheduling_penalties,
                    model_version: ComputeParametersVersion::Fsrs7,
                    num_relearning_steps: Some(num_of_relearning_steps),
                },
                evaluation_items.items,
            )?
        } else {
            evaluate_with_time_series_splits(
                ComputeParametersInput {
                    training_config: None,
                    train_set: evaluation_items.items,
                    card_ids: evaluation_items.card_ids,
                    progress: None,
                    enable_short_term: true,
                    enable_sched_penalties: enable_scheduling_penalties,
                    model_version: ComputeParametersVersion::Fsrs7,
                    num_relearning_steps: Some(num_of_relearning_steps),
                },
                |ip| {
                    anki_progress
                        .update(false, |p| {
                            p.total_iterations = ip.total as u32;
                            p.current_iteration = ip.current as u32;
                        })
                        .is_ok()
                },
            )?
        };
        Ok(eval)
    }

    pub fn evaluate_params_legacy(
        &mut self,
        params: &Params,
        search: &str,
        ignore_revlogs_before: TimestampMillis,
    ) -> Result<ModelEvaluation> {
        let mut anki_progress = self.new_progress_handler::<ComputeParamsProgress>();
        let guard = self.search_cards_into_table(search, SortMode::NoOrder)?;
        let revlogs: Vec<RevlogEntry> = guard
            .col
            .storage
            .get_revlog_entries_for_searched_cards_in_card_order()?;
        let items = fsrs_items_for_training(revlogs, ignore_revlogs_before);
        let target_counts = items.target_counts();
        anki_progress.state.reviews = target_counts.total_targets as u32;
        anki_progress.state.long_term_reviews = target_counts.long_term_targets as u32;
        anki_progress.state.short_term_reviews = target_counts.short_term_targets as u32;
        let fsrs = FSRS::new(effective_fsrs7_params(params))?;
        Ok(fsrs.evaluate(items.items, |ip| {
            anki_progress
                .update(false, |p| {
                    p.total_iterations = ip.total as u32;
                    p.current_iteration = ip.current as u32;
                })
                .is_ok()
        })?)
    }
}

#[derive(Default, Clone, Copy, Debug)]
pub struct ComputeParamsProgress {
    pub current_iteration: u32,
    pub total_iterations: u32,
    /// Total training targets used by optimizer (long-term + same-day)
    pub reviews: u32,
    /// Targets where delta_t >= 1 day
    pub long_term_reviews: u32,
    /// Targets where delta_t < 1 day
    pub short_term_reviews: u32,
    /// Only used in 'compute all params' case
    pub current_preset: u32,
    /// Only used in 'compute all params' case
    pub total_presets: u32,
    pub phase: ComputeParamsProgressPhase,
}

#[derive(Default, Clone, Debug)]
pub struct ComputeAllParamsProgress {
    pub current_iteration: u32,
    pub total_iterations: u32,
    pub presets: Vec<ComputeAllParamsPresetProgress>,
}

#[derive(Default, Clone, Debug)]
pub struct ComputeAllParamsPresetProgress {
    pub name: String,
    pub current_iteration: u32,
    pub total_iterations: u32,
    pub reviews: u32,
    pub long_term_reviews: u32,
    pub short_term_reviews: u32,
    pub finished: bool,
    pub skipped: bool,
    pub phase: ComputeParamsProgressPhase,
}

#[derive(Default, Clone, Copy, Debug)]
pub(crate) struct TrainingTargetCounts {
    pub total_targets: usize,
    pub long_term_targets: usize,
    pub short_term_targets: usize,
}

/// Convert a series of revlog entries sorted by card id into FSRS items.
fn fsrs_items_for_training(
    revlogs: Vec<RevlogEntry>,
    review_revlogs_before: TimestampMillis,
) -> TrainingItemsForFsrs {
    let mut prediction_sources = Vec::new();
    let mut revlogs = revlogs
        .into_iter()
        .chunk_by(|r| r.cid)
        .into_iter()
        .filter_map(|(cid, entries)| {
            reviews_for_fsrs(entries.collect(), true, review_revlogs_before).map(|reviews| {
                if let Some(source) =
                    fsrs_prediction_source_from_filtered_revlogs(&reviews.filtered_revlogs)
                {
                    prediction_sources.push(source);
                }
                reviews
                    .fsrs_items
                    .into_iter()
                    .map(move |(revlog_id, item)| (revlog_id, cid, item))
            })
        })
        .flatten()
        .collect_vec();
    // Sort by RevlogId
    revlogs.sort_by_key(|(revlog_id, _, _)| revlog_id.0);
    let (revlog_ids, card_items): (Vec<_>, Vec<_>) = revlogs
        .into_iter()
        .map(|(revlog_id, card_id, item)| (revlog_id, (card_id.0, item)))
        .unzip();
    let (card_ids, items) = card_items.into_iter().unzip();
    TrainingItemsForFsrs::with_card_and_revlog_ids(items, card_ids, revlog_ids, prediction_sources)
}

fn fsrs_prediction_source_from_filtered_revlogs(
    entries: &[RevlogEntry],
) -> Option<FsrsReviewPredictionSource> {
    let delta_ts = fsrs_review_delta_ts(entries);
    let reviews = entries
        .iter()
        .zip(delta_ts.iter())
        .map(|(entry, &delta_t)| FSRSReview {
            rating: entry.button_chosen as u32,
            delta_t,
        })
        .collect_vec();
    let targets = entries
        .iter()
        .zip(delta_ts.iter())
        .enumerate()
        .filter_map(|(idx, (entry, _))| (idx >= 1).then_some((entry.id, idx)))
        .collect_vec();

    (!targets.is_empty()).then_some(FsrsReviewPredictionSource { reviews, targets })
}

pub(crate) fn fsrs_review_retrievability_cache_rows(
    params: &[f32],
    context: &FsrsReviewPredictionContext,
    include_validation_folds: bool,
    progress: Option<&FsrsReviewRetrievabilityProgress>,
) -> Result<Vec<FsrsReviewRetrievabilityCacheRow>> {
    let mut rows =
        fsrs_review_retrievability_predictions_for_targets(params, &context.sources, None)?
            .into_iter()
            .map(|(revlog_id, prediction)| FsrsReviewRetrievabilityCacheRow {
                revlog_id,
                prediction,
                sample_role: FsrsReviewRetrievabilitySampleRole::FinalFit,
                fold_index: -1,
            })
            .collect_vec();
    if fsrs_review_retrievability_progress_wants_abort(progress) {
        return Err(AnkiError::Interrupted);
    }
    if include_validation_folds {
        match fsrs_validation_retrievability_cache_rows(context, progress) {
            Ok(validation_rows) => rows.extend(validation_rows),
            Err(AnkiError::Interrupted) => return Err(AnkiError::Interrupted),
            Err(err) => tracing::debug!(?err, "failed to compute FSRS validation cache rows"),
        }
    }
    Ok(rows)
}

fn fsrs_review_retrievability_predictions_for_targets(
    params: &[f32],
    sources: &[FsrsReviewPredictionSource],
    target_revlog_ids: Option<&HashSet<RevlogId>>,
) -> Result<Vec<(RevlogId, f32)>> {
    if sources.is_empty() {
        return Ok(Vec::new());
    }

    // FSRS-7 only (spec sched.fsrs7-only): the fsrs crate runs FSRS-6 for 0,
    // 17, 19 or 21 values
    let params = effective_fsrs7_params(params);
    // The scalar copy of the forgetting curve gives the same bits as
    // `FSRS::current_retrievability` at a fraction of its cost, because the
    // tensor path allocates about thirty one-element tensors for every
    // review (see `Fsrs7Curve`). It covers FSRS-7 parameters only; anything
    // else falls back to the tensor path.
    let curve = Fsrs7Curve::new(params);
    // Only the cards that hold a wanted rating are replayed, shortest
    // history first so that a batch pads little.
    let mut wanted: Vec<&FsrsReviewPredictionSource> = sources
        .iter()
        .filter(|source| {
            target_revlog_ids.is_none_or(|ids| {
                source
                    .targets
                    .iter()
                    .any(|(revlog_id, _)| ids.contains(revlog_id))
            })
        })
        .collect();
    wanted.sort_unstable_by_key(|source| source.reviews.len());

    // Cards are independent, so the batches run in parallel; each worker
    // builds its own model, as the Total Knowledge replay does.
    let per_batch: Vec<Vec<(RevlogId, f32)>> = wanted
        .par_chunks(FSRS_MEMORY_STATE_BATCH)
        .map_init(
            || FSRS::new(params).ok(),
            |fsrs, batch| {
                let Some(fsrs) = fsrs.as_ref() else {
                    return Ok(Vec::new());
                };
                let items = batch
                    .iter()
                    .map(|source| FSRSItem {
                        reviews: source.reviews.clone(),
                    })
                    .collect();
                let states = fsrs.historical_memory_state_batch(items, None)?;
                let mut predictions = Vec::new();
                for (source, memory_states) in batch.iter().zip(states) {
                    for &(revlog_id, review_index) in &source.targets {
                        if target_revlog_ids.is_some_and(|ids| !ids.contains(&revlog_id)) {
                            continue;
                        }
                        let Some(previous_state) = review_index
                            .checked_sub(1)
                            .and_then(|index| memory_states.get(index))
                        else {
                            continue;
                        };
                        let Some(review) = source.reviews.get(review_index) else {
                            continue;
                        };
                        let prediction = curve
                            .as_ref()
                            .and_then(|curve| curve.retrievability(*previous_state, review.delta_t))
                            .unwrap_or_else(|| {
                                fsrs.current_retrievability(*previous_state, review.delta_t)
                            });
                        if prediction.is_finite() && (0.0..=1.0).contains(&prediction) {
                            predictions.push((revlog_id, prediction));
                        }
                    }
                }
                Ok(predictions)
            },
        )
        .collect::<Result<Vec<_>>>()?;

    Ok(per_batch.into_iter().flatten().collect())
}

fn fsrs_review_retrievability_progress_wants_abort(
    progress: Option<&FsrsReviewRetrievabilityProgress>,
) -> bool {
    progress.is_some_and(|progress| progress.training_progress.lock().unwrap().want_abort)
}

fn fsrs_validation_retrievability_cache_rows(
    context: &FsrsReviewPredictionContext,
    progress: Option<&FsrsReviewRetrievabilityProgress>,
) -> Result<Vec<FsrsReviewRetrievabilityCacheRow>> {
    if context.items.is_empty() || context.sources.is_empty() {
        return Ok(Vec::new());
    }
    let splits = time_series_split_items(
        TrainingItemsForFsrs::with_card_and_revlog_ids(
            context.items.clone(),
            context.card_ids.clone(),
            context.revlog_ids.clone(),
            Vec::new(),
        ),
        FSRS_VALIDATION_FOLDS,
    );
    if splits.is_empty() {
        return Ok(Vec::new());
    }

    let mut rows = Vec::new();
    for (fold_index, (train_items, test_items)) in splits.into_iter().enumerate() {
        if let Some(progress) = progress {
            progress
                .completed_validation_folds
                .store(fold_index, Ordering::Release);
        }
        if fsrs_review_retrievability_progress_wants_abort(progress) {
            return Err(AnkiError::Interrupted);
        }
        let Some(test_revlog_ids) = test_items.revlog_ids else {
            continue;
        };
        let parameters = match compute_parameters(ComputeParametersInput {
            training_config: None,
            train_set: train_items.items,
            card_ids: train_items.card_ids,
            progress: progress.map(|progress| progress.training_progress.clone()),
            enable_short_term: true,
            enable_sched_penalties: context.enable_scheduling_penalties,
            model_version: ComputeParametersVersion::Fsrs7,
            num_relearning_steps: Some(context.num_relearning_steps),
        }) {
            Ok(parameters) => parameters,
            Err(fsrs::FSRSError::Interrupted) => return Err(AnkiError::Interrupted),
            Err(err) => {
                tracing::debug!(?err, fold_index, "skipping FSRS validation cache fold");
                continue;
            }
        };
        let target_revlog_ids = test_revlog_ids.into_iter().collect::<HashSet<_>>();
        let predictions = match fsrs_review_retrievability_predictions_for_targets(
            &parameters,
            &context.sources,
            Some(&target_revlog_ids),
        ) {
            Ok(predictions) => predictions,
            Err(err) => {
                tracing::debug!(
                    ?err,
                    fold_index,
                    "skipping FSRS validation cache fold predictions"
                );
                continue;
            }
        };
        rows.extend(predictions.into_iter().map(|(revlog_id, prediction)| {
            FsrsReviewRetrievabilityCacheRow {
                revlog_id,
                prediction,
                sample_role: FsrsReviewRetrievabilitySampleRole::ValidationFold,
                fold_index: fold_index as i32,
            }
        }));
        if let Some(progress) = progress {
            progress
                .completed_validation_folds
                .store(fold_index + 1, Ordering::Release);
        }
    }
    Ok(rows)
}

pub(crate) struct ReviewsForFsrs {
    /// The revlog entries that remain after filtering (e.g. excluding
    /// review entries prior to a card being reset).
    pub filtered_revlogs: Vec<RevlogEntry>,
    /// FSRS items derived from the filtered revlogs.
    pub fsrs_items: Vec<(RevlogId, FSRSItem)>,
    /// True if there is enough history to derive memory state from history
    /// alone. If false, memory state will be derived from SM2.
    pub revlogs_complete: bool,
}

/// Filter out unwanted revlog entries, then create a series of FSRS items for
/// training/memory state calculation.
///
/// Filtering consists of removing revlog entries before the supplied timestamp,
/// and removing items such as reviews that happened prior to a card being reset
/// to new.
pub(crate) fn reviews_for_fsrs(
    mut entries: Vec<RevlogEntry>,
    training: bool,
    ignore_revlogs_before: TimestampMillis,
) -> Option<ReviewsForFsrs> {
    let mut first_of_last_learn_entries = None;
    let mut first_user_grade_idx = None;
    let mut revlogs_complete = false;
    // Working backwards from the latest review...
    for (index, entry) in entries.iter().enumerate().rev() {
        if entry.is_cramming() {
            continue;
        }
        // For incomplete review histories, initial memory state is based on the first
        // user-graded review after the cutoff date with interval >= 1d.
        let within_cutoff = entry.id.0 > ignore_revlogs_before.0;
        let user_graded = entry.has_rating();
        let interday = entry.interval >= 1 || entry.interval <= -86400;
        if user_graded && within_cutoff && interday {
            first_user_grade_idx = Some(index);
        }

        if user_graded && entry.review_kind == RevlogReviewKind::Learning {
            first_of_last_learn_entries = Some(index);
            revlogs_complete = true;
        } else if entry.is_reset() {
            // Ignore entries prior to a `Reset` if a learning step has come after,
            // but consider revlogs complete.
            if first_of_last_learn_entries.is_some() {
                revlogs_complete = true;
                break;
            // Ignore entries prior to a `Reset` if the user has graded a card
            // after the reset.
            } else if first_user_grade_idx.is_some() {
                revlogs_complete = false;
                break;
            // User has not graded the card since it was reset, so all history
            // filtered out.
            } else {
                return None;
            }
        // Previous versions of Anki didn't add a revlog entry when the card was
        // reset.
        } else if first_of_last_learn_entries.is_some() {
            break;
        }
    }
    if training {
        // While training, ignore the entire card if the first learning step of the last
        // group of learning steps is before the ignore_revlogs_before date
        if let Some(idx) = first_of_last_learn_entries {
            if entries[idx].id.0 < ignore_revlogs_before.0 {
                return None;
            }
        }
    } else {
        // While reviewing, if the first learning step is before the ignore date,
        // we ignore it, and will fall back on SM2 info and the last user grade below.
        if let Some(idx) = first_of_last_learn_entries {
            if entries[idx].id.0 < ignore_revlogs_before.0 && idx < entries.len() - 1 {
                revlogs_complete = false;
                first_of_last_learn_entries = None;
            }
        }
    }
    if let Some(idx) = first_of_last_learn_entries {
        // start from the learning step
        if idx > 0 {
            entries.drain(..idx);
        }
    } else if training {
        // when training, we ignore cards that don't have any learning steps
        return None;
    } else {
        // if no valid user grades were found, ignore the card.
        let idx = first_user_grade_idx?;
        // if there are no learning entries, but the user has reviewed the card,
        // we ignore all entries before the first grade
        if idx > 0 {
            entries.drain(..idx);
        }
    }

    // Filter out unwanted entries
    entries.retain(|entry| entry.has_rating_and_affects_scheduling());

    let delta_ts = fsrs_review_delta_ts(&entries);

    let items = if training {
        // Convert the remaining entries into separate FSRSItems, where each item
        // contains all reviews done until then.
        let mut items = Vec::with_capacity(entries.len());
        let mut current_reviews = Vec::with_capacity(entries.len());
        for (idx, (entry, &delta_t)) in entries.iter().zip(delta_ts.iter()).enumerate() {
            current_reviews.push(FSRSReview {
                rating: entry.button_chosen as u32,
                delta_t,
            });
            // FSRS-7 trains on same-day reviews too (spec sched.fsrs7-only)
            if idx >= 1 {
                items.push((
                    entry.id,
                    FSRSItem {
                        reviews: current_reviews.clone(),
                    },
                ));
            }
        }
        items
    } else {
        // When not training, we only need the final FSRS item, which represents
        // the complete history of the card. This avoids expensive clones in a loop.
        let reviews = entries
            .iter()
            .zip(delta_ts.iter())
            .map(|(entry, &delta_t)| FSRSReview {
                rating: entry.button_chosen as u32,
                delta_t,
            })
            .collect();
        let last_entry = entries.last().unwrap();

        vec![(last_entry.id, FSRSItem { reviews })]
    };

    if items.is_empty() {
        None
    } else {
        Some(ReviewsForFsrs {
            fsrs_items: items,
            revlogs_complete,
            filtered_revlogs: entries,
        })
    }
}

/// The elapsed time before each review, in fractional days from the revlog
/// millisecond ids: FSRS-7's input (spec sched.fsrs7-fractional-elapsed-time).
fn fsrs_review_delta_ts(entries: &[RevlogEntry]) -> Vec<f32> {
    iter::once(0.0f32)
        .chain(entries.iter().tuple_windows().map(|(previous, current)| {
            let elapsed_millis = current.id.0.saturating_sub(previous.id.0).max(1) as f32;
            elapsed_millis / 86_400_000.0
        }))
        .collect_vec()
}

fn revlog_entry_to_proto(e: RevlogEntry) -> anki_proto::stats::RevlogEntry {
    anki_proto::stats::RevlogEntry {
        id: e.id.0,
        cid: e.cid.0,
        usn: 0,
        button_chosen: e.button_chosen as u32,
        interval: e.interval,
        last_interval: e.last_interval,
        ease_factor: e.ease_factor,
        taken_millis: e.taken_millis,
        review_kind: match e.review_kind {
            RevlogReviewKind::Learning => revlog_entry::ReviewKind::Learning,
            RevlogReviewKind::Review => revlog_entry::ReviewKind::Review,
            RevlogReviewKind::Relearning => revlog_entry::ReviewKind::Relearning,
            RevlogReviewKind::Filtered => revlog_entry::ReviewKind::Filtered,
            RevlogReviewKind::Manual => revlog_entry::ReviewKind::Manual,
            RevlogReviewKind::Rescheduled => revlog_entry::ReviewKind::Rescheduled,
        } as i32,
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use std::path::Path;
    use std::path::PathBuf;
    use std::time::Instant;

    use rusqlite::params;
    use tempfile::tempdir;
    use tempfile::TempDir;

    use super::*;
    use crate::collection::Collection;
    use crate::collection::CollectionBuilder;

    const FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE_FOR_BENCH: &str =
        "search_stats_fsrs_review_retrievability";

    const NEXT_DAY_AT: TimestampSecs = TimestampSecs(86400 * 1000);

    fn days_ago_ms(days_ago: i64) -> TimestampMillis {
        ((NEXT_DAY_AT.0 - days_ago * 86400) * 1000).into()
    }

    pub(crate) fn revlog(review_kind: RevlogReviewKind, days_ago: i64) -> RevlogEntry {
        let button_chosen = match review_kind {
            RevlogReviewKind::Manual | RevlogReviewKind::Rescheduled => 0,
            _ => 3,
        };
        RevlogEntry {
            review_kind,
            id: days_ago_ms(days_ago).into(),
            button_chosen,
            interval: 1,
            ..Default::default()
        }
    }

    fn revlog_for_card(cid: i64, review_kind: RevlogReviewKind, days_ago: i64) -> RevlogEntry {
        RevlogEntry {
            cid: CardId(cid),
            ..revlog(review_kind, days_ago)
        }
    }

    pub(crate) fn review(delta_t: u32) -> FSRSReview {
        FSRSReview {
            rating: 3,
            delta_t: delta_t as f32,
        }
    }

    fn review_f(delta_t: f32) -> FSRSReview {
        FSRSReview { rating: 3, delta_t }
    }

    pub(crate) fn convert_ignore_before(
        revlog: &[RevlogEntry],
        training: bool,
        ignore_before: TimestampMillis,
    ) -> Option<Vec<FSRSItem>> {
        reviews_for_fsrs(revlog.to_vec(), training, ignore_before)
            .map(|i| i.fsrs_items.into_iter().map(|(_, item)| item).collect_vec())
    }

    pub(crate) fn convert(revlog: &[RevlogEntry], training: bool) -> Option<Vec<FSRSItem>> {
        convert_ignore_before(revlog, training, 0.into())
    }

    #[test]
    fn compute_params_from_prepared_returns_current_params_without_items() -> Result<()> {
        let current_params = fsrs::DEFAULT_PARAMETERS.to_vec();
        let output = compute_params_from_prepared(
            PreparedComputeParams {
                current_params: current_params.clone(),
                num_of_relearning_steps: 1,
                enable_scheduling_penalties: true,
                items: vec![],
                item_card_ids: vec![],
                item_revlog_ids: vec![],
                fsrs_prediction_sources: vec![],
                target_counts: TrainingTargetCounts::default(),
            },
            None,
            false,
        )?;

        assert_eq!(output.params, current_params);
        assert_eq!(output.fsrs_items, 0);
        assert_eq!(output.health_check_passed, None);
        Ok(())
    }

    fn benchmark_collection_from_env(var: &str) -> Result<Option<(Collection, TempDir, PathBuf)>> {
        let Ok(source) = std::env::var(var) else {
            eprintln!("set {var} to a copied collection.anki2 path to run this benchmark");
            return Ok(None);
        };

        let tempdir = tempdir()?;
        let col_path = tempdir.path().join("feature-bench.anki2");
        std::fs::copy(source, &col_path)?;
        let mut builder = CollectionBuilder::new(&col_path);
        builder.with_desktop_media_paths();
        Ok(Some((builder.build()?, tempdir, col_path)))
    }

    fn benchmark_wal_path(path: &Path) -> PathBuf {
        let mut wal = path.as_os_str().to_os_string();
        wal.push("-wal");
        wal.into()
    }

    fn benchmark_file_size(path: &Path) -> u64 {
        std::fs::metadata(path)
            .map(|metadata| metadata.len())
            .unwrap_or(0)
    }

    fn benchmark_checkpoint_truncate(col: &mut Collection) -> Result<()> {
        col.storage
            .db
            .execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")?;
        Ok(())
    }

    fn benchmark_clear_fsrs_cache_rows(col: &mut Collection) -> Result<()> {
        col.storage.db.execute_batch(&format!(
            "DELETE FROM {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE_FOR_BENCH};"
        ))?;
        Ok(())
    }

    fn benchmark_legacy_autocommit_fsrs_store(
        col: &mut Collection,
        rows: &[FsrsReviewRetrievabilityCacheRow],
        source: &str,
    ) -> Result<usize> {
        if let Some(row) = rows.first() {
            col.storage
                .set_fsrs_review_retrievability_predictions(std::slice::from_ref(row), source)?;
            benchmark_clear_fsrs_cache_rows(col)?;
        }

        let updated_at = TimestampMillis::now().0;
        let mut stored = 0;
        let mut stmt = col.storage.db.prepare_cached(&format!(
            "
            INSERT OR REPLACE INTO {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE_FOR_BENCH}
                (revlog_id, prediction, source, updated_at, sample_role, fold_index)
            VALUES (?, ?, ?, ?, ?, ?)
            "
        ))?;

        for row in rows {
            if row.revlog_id.0 > 0
                && row.prediction.is_finite()
                && (0.0..=1.0).contains(&row.prediction)
            {
                stmt.execute(params![
                    row.revlog_id,
                    row.prediction,
                    source,
                    updated_at,
                    row.sample_role.as_str(),
                    row.fold_index
                ])?;
                stored += 1;
            }
        }

        Ok(stored)
    }

    #[test]
    fn fsrs_retrievability_cache_rows_can_skip_validation_folds() -> Result<()> {
        let mut items = Vec::new();
        let mut card_ids = Vec::new();
        let mut revlog_ids = Vec::new();
        let mut sources = Vec::new();

        for index in 0..12 {
            let revlog_id = RevlogId(index + 1);
            let reviews = vec![
                FSRSReview {
                    rating: 3,
                    delta_t: 0.0,
                },
                FSRSReview {
                    rating: if index % 2 == 0 { 3 } else { 1 },
                    delta_t: 1.0 + index as f32,
                },
            ];
            items.push(FSRSItem {
                reviews: reviews.clone(),
            });
            card_ids.push(index + 1);
            revlog_ids.push(revlog_id);
            sources.push(FsrsReviewPredictionSource {
                reviews,
                targets: vec![(revlog_id, 1)],
            });
        }

        let context = FsrsReviewPredictionContext {
            items,
            card_ids,
            revlog_ids,
            sources,
            num_relearning_steps: 1,
            enable_scheduling_penalties: false,
        };
        let final_fit_rows = fsrs_review_retrievability_cache_rows(
            &fsrs::DEFAULT_PARAMETERS,
            &context,
            false,
            None,
        )?;
        assert!(!final_fit_rows.is_empty());
        assert!(final_fit_rows
            .iter()
            .all(|row| row.sample_role == FsrsReviewRetrievabilitySampleRole::FinalFit));

        let validation_rows =
            fsrs_review_retrievability_cache_rows(&fsrs::DEFAULT_PARAMETERS, &context, true, None)?;
        assert!(validation_rows
            .iter()
            .any(|row| row.sample_role == FsrsReviewRetrievabilitySampleRole::ValidationFold));
        Ok(())
    }

    // Pins spec/scheduling.md#sched.fsrs7-only: calibration predictions from
    // parameters that are not 34 finite values are the FSRS-7 defaults' ones,
    // never FSRS-6's.
    #[test]
    fn fsrs7_only_calibration_predictions_without_fsrs7_params_use_the_defaults() -> Result<()> {
        let sources = (0..6)
            .map(|index| FsrsReviewPredictionSource {
                reviews: vec![
                    FSRSReview {
                        rating: 3,
                        delta_t: 0.0,
                    },
                    FSRSReview {
                        rating: if index % 2 == 0 { 3 } else { 1 },
                        delta_t: 2.0 + index as f32,
                    },
                ],
                targets: vec![(RevlogId(index + 1), 1)],
            })
            .collect_vec();
        let defaults = fsrs_review_retrievability_predictions_for_targets(
            &fsrs::DEFAULT_PARAMETERS,
            &sources,
            None,
        )?;
        assert_eq!(defaults.len(), sources.len());
        let fsrs6 = fsrs_review_retrievability_predictions_for_targets(
            &fsrs::FSRS6_DEFAULT_PARAMETERS,
            &sources,
            None,
        )?;
        assert_eq!(fsrs6, defaults);
        let empty = fsrs_review_retrievability_predictions_for_targets(&[], &sources, None)?;
        assert_eq!(empty, defaults);
        let preview =
            fsrs_review_retrievability_predictions_for_targets(&[0.5; 35], &sources, None)?;
        assert_eq!(preview, defaults);
        Ok(())
    }

    #[test]
    #[ignore]
    fn fsrs_optimize_cache_store_feature_benchmark() -> Result<()> {
        let Some((mut col, _tempdir, col_path)) =
            benchmark_collection_from_env("ANKI_FEATURE_BENCH_COLLECTION")?
        else {
            return Ok(());
        };
        let search = std::env::var("ANKI_FEATURE_BENCH_SEARCH").unwrap_or_default();
        let wal_path = benchmark_wal_path(&col_path);

        let prepare_started = Instant::now();
        let prepared = col.prepare_compute_params(PrepareComputeParamsInput {
            search: &search,
            ignore_revlogs_before: TimestampMillis(0),
            current_params: &fsrs::DEFAULT_PARAMETERS,
            num_of_relearning_steps: 1,
            enable_scheduling_penalties: false,
        })?;
        let prepared_items = prepared.items.len();
        let prepared_targets = prepared.target_counts.total_targets;
        let eval_items = prepared.items.clone();
        let fsrs_prediction_context = FsrsReviewPredictionContext::from_prepared(&prepared);
        let prepare_ms = prepare_started.elapsed().as_secs_f64() * 1000.0;

        let optimize_started = Instant::now();
        let output = compute_params_from_prepared(prepared, None, false)?;
        let optimize_ms = optimize_started.elapsed().as_secs_f64() * 1000.0;

        let eval_started = Instant::now();
        let eval = FSRS::new(&output.params)?.evaluate(eval_items, |_| true)?;
        let eval_ms = eval_started.elapsed().as_secs_f64() * 1000.0;

        let rows_started = Instant::now();
        let predictions = fsrs_review_retrievability_cache_rows(
            &output.params,
            &fsrs_prediction_context,
            true,
            None,
        )?;
        let rows_ms = rows_started.elapsed().as_secs_f64() * 1000.0;

        if predictions.is_empty() {
            println!(
                "fsrs_optimize_cache_store_feature_benchmark search={search:?} \
                 prepared_items={prepared_items} prepared_targets={prepared_targets} \
                 cache_rows=0 prepare_ms={prepare_ms:.3} optimize_ms={optimize_ms:.3} \
                 eval_ms={eval_ms:.3} log_loss={:.9} rmse_bins={:.9} rows_ms={rows_ms:.3}",
                eval.log_loss, eval.rmse_bins
            );
            return Ok(());
        }

        benchmark_checkpoint_truncate(&mut col)?;
        let legacy_started = Instant::now();
        let legacy_stored =
            benchmark_legacy_autocommit_fsrs_store(&mut col, &predictions, "fsrs_feature_bench")?;
        let legacy_ms = legacy_started.elapsed().as_secs_f64() * 1000.0;
        let legacy_wal_bytes = benchmark_file_size(&wal_path);

        benchmark_clear_fsrs_cache_rows(&mut col)?;
        benchmark_checkpoint_truncate(&mut col)?;
        let batched_started = Instant::now();
        let batched_stored = col
            .storage
            .set_fsrs_review_retrievability_predictions(&predictions, "fsrs_feature_bench")?;
        let batched_ms = batched_started.elapsed().as_secs_f64() * 1000.0;
        let batched_wal_bytes = benchmark_file_size(&wal_path);

        benchmark_checkpoint_truncate(&mut col)?;
        let repeated_started = Instant::now();
        let repeated_stored = col
            .storage
            .set_fsrs_review_retrievability_predictions(&predictions, "fsrs_feature_bench")?;
        let repeated_ms = repeated_started.elapsed().as_secs_f64() * 1000.0;
        let repeated_wal_bytes = benchmark_file_size(&wal_path);

        let legacy_total_ms = prepare_ms + optimize_ms + rows_ms + legacy_ms;
        let batched_total_ms = prepare_ms + optimize_ms + rows_ms + batched_ms;
        let repeated_total_ms = prepare_ms + optimize_ms + rows_ms + repeated_ms;

        println!(
            "fsrs_optimize_cache_store_feature_benchmark search={search:?} \
             prepared_items={prepared_items} prepared_targets={prepared_targets} \
             cache_rows={} prepare_ms={prepare_ms:.3} optimize_ms={optimize_ms:.3} \
             eval_ms={eval_ms:.3} log_loss={:.9} rmse_bins={:.9} rows_ms={rows_ms:.3} \
             legacy_stored={legacy_stored} legacy_store_ms={legacy_ms:.3} \
             legacy_wal_bytes={legacy_wal_bytes} batched_stored={batched_stored} \
             batched_store_ms={batched_ms:.3} batched_wal_bytes={batched_wal_bytes} \
             repeated_stored={repeated_stored} repeated_store_ms={repeated_ms:.3} \
             repeated_wal_bytes={repeated_wal_bytes} legacy_total_ms={legacy_total_ms:.3} \
             batched_total_ms={batched_total_ms:.3} repeated_total_ms={repeated_total_ms:.3}",
            predictions.len(),
            eval.log_loss,
            eval.rmse_bins
        );

        assert_eq!(legacy_stored, predictions.len());
        assert_eq!(batched_stored, predictions.len());
        assert_eq!(repeated_stored, predictions.len());
        Ok(())
    }

    #[macro_export]
    macro_rules! fsrs_items {
        ($($reviews:expr),*) => {
            Some(vec![
                $(
                    FSRSItem {
                        reviews: $reviews.to_vec()
                    }
                ),*
            ])
        };
    }

    #[test]
    fn delta_t_is_correct() -> Result<()> {
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 1),
                    revlog(RevlogReviewKind::Review, 0)
                ],
                true,
            ),
            fsrs_items!([review(0), review(1)])
        );
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 15),
                    revlog(RevlogReviewKind::Learning, 13),
                    revlog(RevlogReviewKind::Review, 10),
                    revlog(RevlogReviewKind::Review, 5)
                ],
                true,
            ),
            fsrs_items!(
                [review(0), review(2)],
                [review(0), review(2), review(3)],
                [review(0), review(2), review(3), review(5)]
            )
        );
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 15),
                    revlog(RevlogReviewKind::Learning, 13),
                ],
                true,
            ),
            fsrs_items!([review(0), review(2),])
        );
        Ok(())
    }

    #[test]
    fn card_ids_align_with_sorted_training_items() {
        let training_items = fsrs_items_for_training(
            vec![
                revlog_for_card(1, RevlogReviewKind::Learning, 10),
                revlog_for_card(1, RevlogReviewKind::Review, 7),
                revlog_for_card(1, RevlogReviewKind::Review, 1),
                revlog_for_card(2, RevlogReviewKind::Learning, 9),
                revlog_for_card(2, RevlogReviewKind::Review, 8),
            ],
            0.into(),
        );

        assert_eq!(training_items.card_ids.as_deref(), Some(&[2, 1, 1][..]));
        assert_eq!(
            training_items
                .items
                .iter()
                .map(|item| item.reviews.len())
                .collect_vec(),
            vec![2, 2, 3]
        );
    }

    #[test]
    fn cram_is_filtered() {
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 10),
                    revlog(RevlogReviewKind::Review, 9),
                    revlog(RevlogReviewKind::Filtered, 7),
                    revlog(RevlogReviewKind::Review, 4),
                ],
                true,
            ),
            fsrs_items!([review(0), review(1)], [review(0), review(1), review(5)])
        );
    }

    #[test]
    fn set_due_date_is_filtered() {
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 10),
                    revlog(RevlogReviewKind::Review, 9),
                    RevlogEntry {
                        ease_factor: 100,
                        ..revlog(RevlogReviewKind::Manual, 7)
                    },
                    revlog(RevlogReviewKind::Review, 4),
                ],
                true,
            ),
            fsrs_items!([review(0), review(1)], [review(0), review(1), review(5)])
        );
    }

    #[test]
    fn card_reset_drops_all_previous_history() {
        // If Reset comes in between two Learn entries, only the ones after the Reset
        // are used.
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 10),
                    RevlogEntry {
                        ease_factor: 0,
                        ..revlog(RevlogReviewKind::Manual, 7)
                    },
                    revlog(RevlogReviewKind::Learning, 4),
                    revlog(RevlogReviewKind::Review, 0),
                ],
                true,
            ),
            fsrs_items!([review(0), review(4)])
        );
        // Return None if Reset is the last entry or is followed by only manual entries.
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 10),
                    revlog(RevlogReviewKind::Review, 9),
                    RevlogEntry {
                        ease_factor: 0,
                        ..revlog(RevlogReviewKind::Manual, 7)
                    },
                    RevlogEntry {
                        ease_factor: 100,
                        ..revlog(RevlogReviewKind::Manual, 7)
                    },
                ],
                false,
            ),
            None,
        );
        // If non-learning user-graded entries are found after Reset, return None during
        // training but return the remaining entries during memory state calculation.
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Learning, 10),
                    revlog(RevlogReviewKind::Review, 9),
                    RevlogEntry {
                        ease_factor: 0,
                        ..revlog(RevlogReviewKind::Manual, 7)
                    },
                    revlog(RevlogReviewKind::Review, 1),
                    revlog(RevlogReviewKind::Relearning, 0),
                ],
                true,
            ),
            None,
        );
        assert_eq!(
            convert(
                &[
                    revlog(RevlogReviewKind::Review, 9),
                    RevlogEntry {
                        ease_factor: 0,
                        ..revlog(RevlogReviewKind::Manual, 7)
                    },
                    revlog(RevlogReviewKind::Review, 1),
                    revlog(RevlogReviewKind::Relearning, 0),
                ],
                false,
            ),
            fsrs_items!([review(0), review(1)])
        );
    }

    #[test]
    fn single_learning_step_skipped_when_training() {
        assert_eq!(
            convert(&[revlog(RevlogReviewKind::Learning, 1),], true),
            None,
        );
        assert_eq!(
            convert(&[revlog(RevlogReviewKind::Learning, 1),], false),
            fsrs_items!([review(0)])
        );
    }

    #[test]
    fn fsrs7_training_includes_same_day_only_targets() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 1),
            revlog(RevlogReviewKind::Review, 1),
            revlog(RevlogReviewKind::Review, 1),
        ];
        // Pins spec/scheduling.md#sched.fsrs7-only: same-day reviews are always
        // training targets.
        assert_eq!(
            convert(revlogs, true),
            Some(vec![
                FSRSItem {
                    reviews: vec![review(0), review_f(1.0 / 86_400_000.0)],
                },
                FSRSItem {
                    reviews: vec![
                        review(0),
                        review_f(1.0 / 86_400_000.0),
                        review_f(1.0 / 86_400_000.0),
                    ],
                },
            ])
        );
    }

    #[test]
    fn empty_dataset_returns_not_enough_data() {
        let err = evaluate_with_time_series_splits(
            ComputeParametersInput {
                training_config: None,
                train_set: vec![],
                card_ids: None,
                progress: None,
                enable_short_term: true,
                enable_sched_penalties: true,
                model_version: ComputeParametersVersion::Fsrs7,
                num_relearning_steps: Some(1),
            },
            |_| true,
        )
        .unwrap_err();
        assert!(matches!(err, fsrs::FSRSError::NotEnoughData));
    }

    #[test]
    fn health_check_adjustment_uses_pass_rate_of_evaluated_targets() {
        let mut items = vec![];
        for _ in 0..19 {
            items.push(FSRSItem {
                reviews: vec![review(0), review(2)],
            });
        }
        items.push(FSRSItem {
            reviews: vec![
                review(0),
                FSRSReview {
                    rating: 1,
                    delta_t: 2.0,
                },
            ],
        });
        for _ in 0..4 {
            items.push(FSRSItem {
                reviews: vec![
                    review(0),
                    FSRSReview {
                        rating: 1,
                        delta_t: 0.5,
                    },
                ],
            });
        }

        // The health check evaluates every target, same-day ones included
        // (spec sched.fsrs7-only), so the same-day lapses lower the pass rate the
        // adjustment uses. Without them the same evaluation fails.
        let long_term_only = items
            .iter()
            .filter(|item| has_long_term_target(item))
            .cloned()
            .collect_vec();
        assert_eq!(long_term_only.len(), 20);
        let eval = ModelEvaluation {
            log_loss: 0.3,
            rmse_bins: 1.0,
        };
        assert!(health_check_passed_for_evaluated_targets(eval, &items));
        assert!(!health_check_passed_for_evaluated_targets(
            eval,
            &long_term_only
        ));
    }

    #[test]
    fn fsrs7_training_keeps_same_day_targets_after_long_term_review() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 3),
            revlog(RevlogReviewKind::Review, 2),
            revlog(RevlogReviewKind::Review, 2),
        ];
        assert_eq!(
            convert(revlogs, true),
            Some(vec![
                FSRSItem {
                    reviews: vec![review(0), review(1)],
                },
                FSRSItem {
                    reviews: vec![review(0), review(1), review_f(1.0 / 86_400_000.0)],
                },
            ])
        );
    }

    #[test]
    fn fsrs_items_for_training_keeps_items_without_long_term_review_for_fsrs7() {
        let revlogs = vec![
            RevlogEntry {
                cid: CardId(1),
                ..revlog(RevlogReviewKind::Learning, 1)
            },
            RevlogEntry {
                cid: CardId(1),
                ..revlog(RevlogReviewKind::Review, 1)
            },
            RevlogEntry {
                cid: CardId(1),
                ..revlog(RevlogReviewKind::Review, 1)
            },
        ];
        let items = fsrs_items_for_training(revlogs, TimestampMillis(0));
        assert_eq!(
            items.items,
            vec![
                FSRSItem {
                    reviews: vec![review(0), review_f(1.0 / 86_400_000.0)],
                },
                FSRSItem {
                    reviews: vec![
                        review(0),
                        review_f(1.0 / 86_400_000.0),
                        review_f(1.0 / 86_400_000.0),
                    ],
                },
            ]
        );
        assert_eq!(items.card_ids, Some(vec![1, 1]));
    }

    #[test]
    fn fsrs_items_for_training_reports_long_and_same_day_target_counts() {
        let revlogs = vec![
            RevlogEntry {
                cid: CardId(1),
                ..revlog(RevlogReviewKind::Learning, 3)
            },
            RevlogEntry {
                cid: CardId(1),
                ..revlog(RevlogReviewKind::Review, 2)
            },
            RevlogEntry {
                cid: CardId(1),
                ..revlog(RevlogReviewKind::Review, 2)
            },
        ];
        let fsrs7_items = fsrs_items_for_training(revlogs, TimestampMillis(0));
        let fsrs7_counts = fsrs7_items.target_counts();
        assert_eq!(fsrs7_counts.total_targets, 2);
        assert_eq!(fsrs7_counts.long_term_targets, 1);
        assert_eq!(fsrs7_counts.short_term_targets, 1);
    }

    #[test]
    fn fsrs7_same_day_delta_uses_fractional_elapsed_time() {
        let base = days_ago_ms(1).0 + 3_600_000;
        let revlogs = vec![
            RevlogEntry {
                id: RevlogId(base),
                ..revlog(RevlogReviewKind::Learning, 1)
            },
            RevlogEntry {
                id: RevlogId(base + 3_600_000),
                ..revlog(RevlogReviewKind::Review, 1)
            },
        ];
        let converted = convert(&revlogs, true).unwrap();
        let delta = converted[0].reviews[1].delta_t;
        assert!(delta > 0.0);
        assert!((delta - (1.0 / 24.0)).abs() < 1e-6);
    }

    #[test]
    fn fsrs7_same_day_delta_is_positive_when_timestamps_equal() {
        let base = days_ago_ms(1).0;
        let revlogs = vec![
            RevlogEntry {
                id: RevlogId(base),
                ..revlog(RevlogReviewKind::Learning, 1)
            },
            RevlogEntry {
                id: RevlogId(base),
                ..revlog(RevlogReviewKind::Review, 1)
            },
        ];
        let converted = convert(&revlogs, true).unwrap();
        assert!(converted[0].reviews[1].delta_t > 0.0);
    }

    #[test]
    fn fsrs7_interday_delta_uses_fractional_elapsed_time() {
        let revlogs = vec![
            RevlogEntry {
                id: RevlogId(days_ago_ms(3).0),
                ..revlog(RevlogReviewKind::Learning, 3)
            },
            RevlogEntry {
                // 0.5 day after the D-1 boundary -> elapsed days differs from elapsed timestamp.
                id: RevlogId(days_ago_ms(1).0 + 43_200_000),
                ..revlog(RevlogReviewKind::Review, 1)
            },
        ];
        let converted7 = convert(&revlogs, true).unwrap();
        assert!((converted7[0].reviews[1].delta_t - 2.5).abs() < 1e-6);
    }

    #[test]
    fn ignores_cards_before_ignore_before_date_when_training() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 10),
            revlog(RevlogReviewKind::Learning, 8),
        ];
        // | = Ignore before
        // L = learning step
        // L L |
        assert_eq!(convert_ignore_before(revlogs, true, days_ago_ms(7)), None);
        // L | L
        assert_eq!(convert_ignore_before(revlogs, true, days_ago_ms(9)), None);
        // L (|L) (exact same millisecond)
        assert_eq!(
            convert_ignore_before(revlogs, true, days_ago_ms(10)),
            convert(revlogs, true)
        );
        // | L L
        assert_eq!(
            convert_ignore_before(revlogs, true, days_ago_ms(11)),
            convert(revlogs, true)
        );
    }

    #[test]
    fn partially_ignored_learning_steps_terminate_training() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 10),
            revlog(RevlogReviewKind::Learning, 8),
            revlog(RevlogReviewKind::Review, 6),
        ];
        // | = Ignore before
        // L = learning step
        // L | L R
        assert_eq!(convert_ignore_before(revlogs, true, days_ago_ms(9)), None);
    }

    #[test]
    fn skip_initial_relearning_steps() {
        let revlogs = &[
            revlog(RevlogReviewKind::Review, 10),
            RevlogEntry {
                button_chosen: 1, // Again
                interval: -600,
                ..revlog(RevlogReviewKind::Review, 8)
            },
            revlog(RevlogReviewKind::Relearning, 8),
            revlog(RevlogReviewKind::Review, 6),
        ];
        // | = Ignore before
        // A = Again
        // X = Relearning
        // R | A X R
        assert_eq!(
            convert_ignore_before(revlogs, false, days_ago_ms(9)),
            fsrs_items!([review(0), review(2)])
        );
    }

    #[test]
    fn ignore_before_date_between_learning_steps_when_reviewing() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 10),
            revlog(RevlogReviewKind::Learning, 8),
            revlog(RevlogReviewKind::Review, 2),
        ];
        // L | L R
        assert_ne!(
            convert_ignore_before(revlogs, false, days_ago_ms(9)),
            convert(revlogs, false)
        );
        assert_eq!(
            convert_ignore_before(revlogs, false, days_ago_ms(9))
                .unwrap()
                .last()
                .unwrap()
                .reviews
                .len(),
            2
        );
        // | L L R
        assert_eq!(
            convert_ignore_before(revlogs, false, days_ago_ms(11)),
            convert(revlogs, false)
        );
    }

    #[test]
    fn handle_ignore_before_when_no_learning_steps() {
        let revlogs = &[
            revlog(RevlogReviewKind::Review, 10),
            revlog(RevlogReviewKind::Review, 8),
            revlog(RevlogReviewKind::Review, 6),
        ];
        // R | R R
        assert_eq!(
            convert_ignore_before(revlogs, false, days_ago_ms(9))
                .unwrap()
                .last()
                .unwrap()
                .reviews
                .len(),
            2
        );
    }

    #[test]
    fn ignore_before_after_last_revlog_entry() {
        let revlogs = &[
            revlog(RevlogReviewKind::Learning, 10),
            revlog(RevlogReviewKind::Review, 6),
        ];
        // L R |
        assert_eq!(convert_ignore_before(revlogs, false, days_ago_ms(4)), None);
    }

    #[test]
    fn training_search_uses_override_when_non_empty() {
        assert_eq!(
            super::training_search("deck:train", Some("deck:eval")),
            "deck:eval"
        );
        assert_eq!(
            super::training_search("deck:train", Some("   deck:eval2  ")),
            "deck:eval2"
        );
    }

    #[test]
    fn training_search_falls_back_to_search_when_empty() {
        assert_eq!(super::training_search("deck:train", None), "deck:train");
        assert_eq!(
            super::training_search("deck:train", Some("   ")),
            "deck:train"
        );
    }

    #[test]
    fn external_evaluation_is_enabled_only_for_different_searches() {
        assert!(super::uses_external_evaluation(
            "preset:vocabulary rated:1",
            "preset:vocabulary"
        ));
        assert!(!super::uses_external_evaluation(
            "preset:vocabulary",
            "preset:vocabulary"
        ));
    }

    #[test]
    fn external_target_evaluation_rejects_empty_sets() {
        let err = super::evaluate_from_training_to_external_targets(
            ComputeParametersInput {
                training_config: None,
                train_set: vec![],
                card_ids: None,
                progress: None,
                enable_short_term: true,
                enable_sched_penalties: true,
                model_version: ComputeParametersVersion::Fsrs7,
                num_relearning_steps: Some(1),
            },
            vec![FSRSItem {
                reviews: vec![review(0), review(2)],
            }],
        )
        .unwrap_err();
        assert!(matches!(
            err,
            AnkiError::FsrsInsufficientData | AnkiError::FsrsInsufficientReviews { .. }
        ));
    }

    #[test]
    fn external_target_evaluation_rejects_empty_evaluation_set() {
        let err = super::evaluate_from_training_to_external_targets(
            ComputeParametersInput {
                training_config: None,
                train_set: vec![FSRSItem {
                    reviews: vec![review(0), review(2)],
                }],
                card_ids: None,
                progress: None,
                enable_short_term: true,
                enable_sched_penalties: true,
                model_version: ComputeParametersVersion::Fsrs7,
                num_relearning_steps: Some(1),
            },
            vec![],
        )
        .unwrap_err();
        assert!(matches!(
            err,
            AnkiError::FsrsInsufficientData | AnkiError::FsrsInsufficientReviews { .. }
        ));
    }
}
