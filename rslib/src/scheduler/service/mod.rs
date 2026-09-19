// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod answering;
mod states;

use std::collections::HashMap;

use anki_proto::cards;
use anki_proto::generic;
use anki_proto::scheduler;
use anki_proto::scheduler::ComputeFsrsParamsResponse;
use anki_proto::scheduler::ComputeMemoryStateResponse;
use anki_proto::scheduler::ComputeOptimalRetentionResponse;
use anki_proto::scheduler::FsrsBenchmarkResponse;
use anki_proto::scheduler::FsrsCurrentRetrievabilityRequest;
use anki_proto::scheduler::FsrsCurrentRetrievabilityResponse;
use anki_proto::scheduler::FsrsDesiredRetentionForIntervalsBatchRequest;
use anki_proto::scheduler::FsrsDesiredRetentionForIntervalsBatchResponse;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityBatchRequest;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityBatchResponse;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityByConfigBatchRequest;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityByConfigBatchResponse;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityResponse;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityVariableBatchRequest;
use anki_proto::scheduler::FsrsIntervalAtRetrievabilityVariableBatchResponse;
use anki_proto::scheduler::FsrsNextIntervalRequest;
use anki_proto::scheduler::FsrsNextIntervalResponse;
use anki_proto::scheduler::FsrsPresetForCardResponse;
use anki_proto::scheduler::FsrsPresetIdsForCardsResponse;
use anki_proto::scheduler::FuzzDeltaRequest;
use anki_proto::scheduler::FuzzDeltaResponse;
use anki_proto::scheduler::GetOptimalRetentionParametersResponse;
use anki_proto::scheduler::RwkvAnsweredCardQueueScorePatchRequest;
use anki_proto::scheduler::RwkvCardInfoScoreRequest;
use anki_proto::scheduler::RwkvHistoricalReviewFingerprintRequest;
use anki_proto::scheduler::RwkvHistoricalReviewFingerprintResponse;
use anki_proto::scheduler::RwkvRetrievabilityScoreResponse;
use anki_proto::scheduler::RwkvReviewInputRowsForCardsRequest;
use anki_proto::scheduler::RwkvReviewInputRowsForCardsResponse;
use anki_proto::scheduler::RwkvReviewInputRowsForDeckReviewQueueRequest;
use anki_proto::scheduler::RwkvReviewInputRowsForSearchRequest;
use anki_proto::scheduler::RwkvReviewQueueInterveningReviewsRequest;
use anki_proto::scheduler::RwkvReviewQueueScoresRequest;
use anki_proto::scheduler::RwkvReviewRescheduleRequest;
use anki_proto::scheduler::RwkvReviewRetrievabilityCacheRowsRequest;
use anki_proto::scheduler::RwkvStatsGraphScoresRequest;
use anki_proto::scheduler::SchedulingStatesWithIntervalsRequest;
use anki_proto::scheduler::SimulateFsrsReviewRequest;
use anki_proto::scheduler::SimulateFsrsReviewResponse;
use anki_proto::scheduler::SimulateFsrsWorkloadResponse;
use fsrs::benchmark;
use fsrs::compute_parameters;
use fsrs::ComputeParametersInput;
use fsrs::ComputeParametersVersion;
use fsrs::FSRSItem;
use fsrs::FSRSReview;
use fsrs::FSRS;

use crate::backend::Backend;
use crate::collection::RwkvReviewQueueScoreEntry;
use crate::collection::RwkvStatsGraphScoreEntry;
use crate::deckconfig::effective_fsrs7_params;
use crate::deckconfig::FsrsVersion;
use crate::prelude::*;
use crate::scheduler::advance_postpone::AdvancePostponeRequest;
use crate::scheduler::answering::PreviewDelays;
use crate::scheduler::fsrs::batch::ComputeParamsBatchInput;
use crate::scheduler::fsrs::memory_state::fsrs_next_states_s90;
use crate::scheduler::fsrs::params::ComputeParamsRequest;
use crate::scheduler::fsrs::params::FsrsReviewPredictionContext;
use crate::scheduler::fsrs::params::PrepareComputeParamsInput;
use crate::scheduler::fsrs::preset::FsrsPreset;
use crate::scheduler::fsrs::preset::FsrsPresetId;
use crate::scheduler::new::NewCardDueOrder;
use crate::scheduler::rwkv::RwkvReviewRescheduleItem;
use crate::scheduler::states::CardState;
use crate::scheduler::states::LearnState;
use crate::scheduler::states::SchedulingStates;
use crate::search::SortMode;
use crate::stats::studied_today;
use crate::storage::RwkvReviewRetrievabilityCacheRow;
use crate::storage::RwkvReviewRetrievabilitySampleRole;

fn rwkv_score_entries(
    scores: Vec<scheduler::rwkv_review_queue_scores_request::Score>,
) -> Result<HashMap<CardId, RwkvReviewQueueScoreEntry>> {
    let mut entries = HashMap::with_capacity(scores.len());
    for score in scores {
        entries.insert(score.card_id.into(), rwkv_score_entry(score)?);
    }
    Ok(entries)
}

fn rwkv_score_entry(
    score: scheduler::rwkv_review_queue_scores_request::Score,
) -> Result<RwkvReviewQueueScoreEntry> {
    require!(
        score.retrievability.is_finite() && (0.0..=1.0).contains(&score.retrievability),
        "invalid RWKV retrievability"
    );
    if let Some(target_retention) = score.target_retention {
        require!(
            target_retention.is_finite() && (0.0..=1.0).contains(&target_retention),
            "invalid RWKV target retention"
        );
    }
    Ok(RwkvReviewQueueScoreEntry {
        retrievability: score.retrievability,
        intervening_reviews: score.intervening_reviews,
        target_retention: score.target_retention,
    })
}

impl crate::services::SchedulerService for Collection {
    /// This behaves like _updateCutoff() in older code - it also unburies at
    /// the start of a new day.
    fn sched_timing_today(&mut self) -> Result<scheduler::SchedTimingTodayResponse> {
        let timing = self.timing_today()?;
        self.unbury_if_day_rolled_over(timing)?;
        Ok(timing.into())
    }

    /// Fetch data from DB and return rendered string.
    fn studied_today(&mut self) -> Result<generic::String> {
        self.studied_today().map(Into::into)
    }

    /// Message rendering only, for old graphs.
    fn studied_today_message(
        &mut self,
        input: scheduler::StudiedTodayMessageRequest,
    ) -> Result<generic::String> {
        Ok(studied_today(input.cards, input.seconds as f32, &self.tr).into())
    }

    fn update_stats(&mut self, input: scheduler::UpdateStatsRequest) -> Result<()> {
        self.transact_no_undo(|col| {
            let today = col.current_due_day(0)?;
            let usn = col.usn()?;
            col.update_deck_stats(today, usn, input)
        })
    }

    fn extend_limits(&mut self, input: scheduler::ExtendLimitsRequest) -> Result<()> {
        self.transact_no_undo(|col| {
            let today = col.current_due_day(0)?;
            let usn = col.usn()?;
            col.extend_limits(
                today,
                usn,
                input.deck_id.into(),
                input.new_delta,
                input.review_delta,
            )
        })
    }

    fn counts_for_deck_today(
        &mut self,
        input: anki_proto::decks::DeckId,
    ) -> Result<scheduler::CountsForDeckTodayResponse> {
        self.counts_for_deck_today(input.did.into())
    }

    fn congrats_info(&mut self) -> Result<scheduler::CongratsInfoResponse> {
        self.congrats_info()
    }

    fn restore_buried_and_suspended_cards(
        &mut self,
        input: anki_proto::cards::CardIds,
    ) -> Result<anki_proto::collection::OpChanges> {
        let cids: Vec<_> = input.cids.into_iter().map(CardId).collect();
        self.unbury_or_unsuspend_cards(&cids).map(Into::into)
    }

    fn unbury_deck(
        &mut self,
        input: scheduler::UnburyDeckRequest,
    ) -> Result<anki_proto::collection::OpChanges> {
        self.unbury_deck(input.deck_id.into(), input.mode())
            .map(Into::into)
    }

    fn bury_or_suspend_cards(
        &mut self,
        input: scheduler::BuryOrSuspendCardsRequest,
    ) -> Result<anki_proto::collection::OpChangesWithCount> {
        let mode = input.mode();
        let cids = if input.card_ids.is_empty() {
            self.storage
                .card_ids_of_notes(&input.note_ids.into_newtype(NoteId))?
        } else {
            input.card_ids.into_newtype(CardId)
        };
        self.bury_or_suspend_cards(&cids, mode).map(Into::into)
    }

    fn empty_filtered_deck(
        &mut self,
        input: anki_proto::decks::DeckId,
    ) -> Result<anki_proto::collection::OpChanges> {
        self.empty_filtered_deck(input.did.into()).map(Into::into)
    }

    fn rebuild_filtered_deck(
        &mut self,
        input: anki_proto::decks::DeckId,
    ) -> Result<anki_proto::collection::OpChangesWithCount> {
        self.rebuild_filtered_deck(input.did.into()).map(Into::into)
    }

    fn schedule_cards_as_new(
        &mut self,
        input: scheduler::ScheduleCardsAsNewRequest,
    ) -> Result<anki_proto::collection::OpChanges> {
        let cids = input.card_ids.into_newtype(CardId);
        self.reschedule_cards_as_new(
            &cids,
            input.log,
            input.restore_position,
            input.reset_counts,
            input
                .context
                .and_then(|s| scheduler::schedule_cards_as_new_request::Context::try_from(s).ok()),
        )
        .map(Into::into)
    }

    fn schedule_cards_as_new_defaults(
        &mut self,
        input: scheduler::ScheduleCardsAsNewDefaultsRequest,
    ) -> Result<scheduler::ScheduleCardsAsNewDefaultsResponse> {
        Ok(Collection::reschedule_cards_as_new_defaults(
            self,
            input.context(),
        ))
    }

    fn set_due_date(
        &mut self,
        input: scheduler::SetDueDateRequest,
    ) -> Result<anki_proto::collection::OpChanges> {
        let config = input.config_key.map(|v| v.key().into());
        let days = input.days;
        let cids = input.card_ids.into_newtype(CardId);
        self.set_due_date(&cids, &days, config).map(Into::into)
    }

    fn advance_postpone_candidates(
        &mut self,
        input: scheduler::AdvancePostponeRequest,
    ) -> Result<scheduler::AdvancePostponeCandidatesResponse> {
        let request = AdvancePostponeRequest::try_from(input)?;
        self.advance_postpone_candidates(request.mode, &request.scope)
            .map(Into::into)
    }

    fn preview_advance_postpone(
        &mut self,
        input: scheduler::AdvancePostponeRequest,
    ) -> Result<scheduler::AdvancePostponePreview> {
        let request = AdvancePostponeRequest::try_from(input)?;
        self.preview_advance_postpone(&request).map(Into::into)
    }

    fn advance_postpone(
        &mut self,
        input: scheduler::AdvancePostponeRequest,
    ) -> Result<scheduler::AdvancePostponeResponse> {
        let request = AdvancePostponeRequest::try_from(input)?;
        self.advance_postpone(&request).map(Into::into)
    }

    fn grade_now(
        &mut self,
        input: scheduler::GradeNowRequest,
    ) -> Result<anki_proto::collection::OpChanges> {
        self.grade_now(input).map(Into::into)
    }

    fn sort_cards(
        &mut self,
        input: scheduler::SortCardsRequest,
    ) -> Result<anki_proto::collection::OpChangesWithCount> {
        let cids = input.card_ids.into_newtype(CardId);
        let (start, step, random, shift) = (
            input.starting_from,
            input.step_size,
            input.randomize,
            input.shift_existing,
        );
        let order = if random {
            NewCardDueOrder::Random
        } else {
            NewCardDueOrder::Preserve
        };

        self.sort_cards(&cids, start, step, order, shift)
            .map(Into::into)
    }

    fn reposition_defaults(&mut self) -> Result<scheduler::RepositionDefaultsResponse> {
        Ok(Collection::reposition_defaults(self))
    }

    fn sort_deck(
        &mut self,
        input: scheduler::SortDeckRequest,
    ) -> Result<anki_proto::collection::OpChangesWithCount> {
        self.sort_deck_legacy(input.deck_id.into(), input.randomize)
            .map(Into::into)
    }

    fn get_scheduling_states(
        &mut self,
        input: anki_proto::cards::CardId,
    ) -> Result<scheduler::SchedulingStates> {
        let cid: CardId = input.into();
        self.get_scheduling_states(cid).map(Into::into)
    }

    fn get_scheduling_states_with_opts(
        &mut self,
        input: scheduler::GetSchedulingStatesRequest,
    ) -> Result<scheduler::SchedulingStates> {
        self.get_scheduling_states_with_desired_retention_override(
            CardId(input.card_id),
            input.desired_retention_override,
        )
        .map(Into::into)
    }

    fn describe_next_states(
        &mut self,
        input: scheduler::SchedulingStates,
    ) -> Result<generic::StringList> {
        let states: SchedulingStates = input.into();
        self.describe_next_states(&states).map(Into::into)
    }

    fn state_is_leech(&mut self, input: scheduler::SchedulingState) -> Result<generic::Bool> {
        let state: CardState = input.into();
        Ok(state.leeched().into())
    }

    fn answer_card(
        &mut self,
        input: scheduler::CardAnswer,
    ) -> Result<anki_proto::collection::OpChanges> {
        self.answer_card(&mut input.into()).map(Into::into)
    }

    fn upgrade_scheduler(&mut self) -> Result<()> {
        self.transact_no_undo(|col| col.upgrade_to_v2_scheduler())
    }

    fn get_queued_cards(
        &mut self,
        input: scheduler::GetQueuedCardsRequest,
    ) -> Result<scheduler::QueuedCards> {
        self.get_queued_cards(
            input.fetch_limit as usize,
            input.intraday_learning_only,
            input.skip_scheduling_states,
        )
        .map(Into::into)
    }

    fn rebuild_queued_cards_preserving_current_card(
        &mut self,
        input: cards::CardId,
    ) -> Result<scheduler::QueuedCards> {
        Collection::rebuild_queued_cards_preserving_current_card(self, input.into()).map(Into::into)
    }

    fn custom_study(
        &mut self,
        input: scheduler::CustomStudyRequest,
    ) -> Result<anki_proto::collection::OpChanges> {
        self.custom_study(input).map(Into::into)
    }

    fn custom_study_defaults(
        &mut self,
        input: scheduler::CustomStudyDefaultsRequest,
    ) -> Result<scheduler::CustomStudyDefaultsResponse> {
        self.custom_study_defaults(input.deck_id.into())
    }

    fn compute_fsrs_params(
        &mut self,
        input: scheduler::ComputeFsrsParamsRequest,
    ) -> Result<scheduler::ComputeFsrsParamsResponse> {
        // FSRS-7 only, with same-day reviews (spec sched.fsrs7-only): the
        // request's fsrs_version and include_same_day_reviews are ignored
        let current_params = effective_fsrs7_params(&input.current_params).to_vec();
        self.compute_params(ComputeParamsRequest {
            search: &input.search,
            ignore_revlogs_before_ms: input.ignore_revlogs_before_ms.into(),
            current_preset: 1,
            total_presets: 1,
            current_params: &current_params,
            num_of_relearning_steps: input.num_of_relearning_steps as usize,
            health_check: input.health_check,
            // always on (spec deck-options.fsrs-only-controls); the request field is ignored
            enable_scheduling_penalties: true,
        })
    }

    fn compute_fsrs_params_batch(
        &mut self,
        input: scheduler::ComputeFsrsParamsBatchRequest,
    ) -> Result<scheduler::ComputeFsrsParamsBatchResponse> {
        let mut response_meta = Vec::with_capacity(input.items.len());
        let mut jobs = Vec::with_capacity(input.items.len());

        for (index, item) in input.items.into_iter().enumerate() {
            let current_params = effective_fsrs7_params(&item.current_params).to_vec();
            let prepared = self.prepare_compute_params(PrepareComputeParamsInput {
                search: &item.search,
                ignore_revlogs_before: item.ignore_revlogs_before_ms.into(),
                current_params: &current_params,
                num_of_relearning_steps: item.num_of_relearning_steps as usize,
                // always on (spec deck-options.fsrs-only-controls); the request field is ignored
                enable_scheduling_penalties: true,
            })?;
            response_meta.push((item.id.clone(), item.name.clone()));
            jobs.push(ComputeParamsBatchInput {
                index,
                name: item.name,
                prepared,
            });
        }

        let items = self
            .compute_params_batch(jobs)?
            .into_iter()
            .map(|output| {
                let (id, name) = &response_meta[output.index];
                let params = output.result?;
                Ok(scheduler::compute_fsrs_params_batch_response::Item {
                    id: id.clone(),
                    name: name.clone(),
                    params: params.params,
                    fsrs_items: params.fsrs_items,
                })
            })
            .collect::<Result<Vec<_>>>()?;

        Ok(scheduler::ComputeFsrsParamsBatchResponse { items })
    }

    fn fsrs_presets_due_for_auto_optimize(
        &mut self,
    ) -> Result<scheduler::StaleFsrsPredictionPresetsResponse> {
        Ok(scheduler::StaleFsrsPredictionPresetsResponse {
            deck_config_ids: self
                .fsrs_presets_due_for_auto_optimize()?
                .into_iter()
                .map(|preset| preset.0)
                .collect(),
        })
    }

    fn stale_fsrs_prediction_presets(
        &mut self,
    ) -> Result<scheduler::StaleFsrsPredictionPresetsResponse> {
        Ok(scheduler::StaleFsrsPredictionPresetsResponse {
            deck_config_ids: self
                .presets_with_stale_fsrs_review_predictions()?
                .into_iter()
                .map(|preset| preset.0)
                .collect(),
        })
    }

    fn compute_fsrs_review_retrievability_calibration(
        &mut self,
        input: scheduler::ComputeFsrsReviewRetrievabilityCalibrationRequest,
    ) -> Result<generic::UInt32> {
        let params = effective_fsrs7_params(&input.params).to_vec();
        let prepared = self.prepare_compute_params(PrepareComputeParamsInput {
            search: &input.search,
            ignore_revlogs_before: input.ignore_revlogs_before_ms.into(),
            current_params: &params,
            num_of_relearning_steps: input.num_of_relearning_steps as usize,
            // always on (spec deck-options.fsrs-only-controls); the request field is ignored
            enable_scheduling_penalties: true,
        })?;
        let context = FsrsReviewPredictionContext::from_prepared(&prepared);
        let count = self.compute_fsrs_review_retrievability_calibration_cache(
            &input.params,
            &context,
            input.include_validation_folds,
        )?;
        Ok(generic::UInt32 { val: count })
    }

    fn simulate_fsrs_review(
        &mut self,
        input: SimulateFsrsReviewRequest,
    ) -> Result<SimulateFsrsReviewResponse> {
        self.simulate_review(input)
    }

    fn simulate_fsrs_workload(
        &mut self,
        input: SimulateFsrsReviewRequest,
    ) -> Result<SimulateFsrsWorkloadResponse> {
        self.simulate_workload(input)
    }

    fn compute_optimal_retention(
        &mut self,
        input: SimulateFsrsReviewRequest,
    ) -> Result<ComputeOptimalRetentionResponse> {
        Ok(ComputeOptimalRetentionResponse {
            optimal_retention: self.compute_optimal_retention(input)?,
        })
    }

    fn get_fsrs_new_card_intervals(
        &mut self,
        input: scheduler::GetFsrsNewCardIntervalsRequest,
    ) -> Result<generic::StringList> {
        let config = crate::deckconfig::DeckConfig {
            inner: input.config.unwrap_or_default(),
            ..Default::default()
        };
        let fsrs = FSRS::new(config.fsrs_params())?;
        // FSRS-7 may always schedule inside a day (spec
        // sched.sub-day-intervals)
        let fsrs_allow_short_term = true;
        // Always on (spec sched.same-day-steps-always-on); the request field
        // is kept for wire compatibility and ignored.
        let fsrs_short_term_with_steps_enabled = true;
        // A new card has no reviews today, so only a limit of 0 applies
        // (spec sched.max-same-day-reviews).
        let same_day_review_limit_reached = config.effective_max_same_day_reviews() == Some(0);
        let review_fuzz_config = self.review_fuzz_config();
        let make_ctx = |memory_state: Option<fsrs::MemoryState>,
                        days_elapsed: f32|
         -> Result<crate::scheduler::states::StateContext<'_>> {
            let fsrs_next_states = fsrs.next_states_with_elapsed_days(
                memory_state,
                config.inner.desired_retention,
                days_elapsed,
            )?;
            let fsrs_next_s90 = Some(fsrs_next_states_s90(&fsrs, &fsrs_next_states));

            Ok(crate::scheduler::states::StateContext {
                fuzz_factor: None,
                fsrs_next_states: Some(fsrs_next_states),
                fsrs_short_term_with_steps_enabled,
                same_day_review_limit_reached,
                fsrs_allow_short_term,
                steps: crate::scheduler::states::steps::LearningSteps::new(
                    &config.inner.learn_steps,
                ),
                graduating_interval_good: config.inner.graduating_interval_good,
                graduating_interval_easy: config.inner.graduating_interval_easy,
                initial_ease_factor: config.inner.initial_ease,
                hard_multiplier: config.inner.hard_multiplier,
                easy_multiplier: config.inner.easy_multiplier,
                interval_multiplier: config.inner.interval_multiplier,
                review_fuzz_config,
                maximum_review_interval: config.inner.maximum_review_interval,
                fsrs_minimum_interval_secs: config.inner.fsrs_minimum_interval_secs,
                leech_threshold: config.inner.leech_threshold,
                leech_only_if_young: config.inner.leech_only_if_young,
                fsrs_next_s90,
                load_balancer_ctx: None,
                relearn_steps: crate::scheduler::states::steps::LearningSteps::new(
                    &config.inner.relearn_steps,
                ),
                lapse_multiplier: config.inner.lapse_multiplier,
                minimum_lapse_interval: config.inner.minimum_lapse_interval,
                in_filtered_deck: false,
                preview_delays: PreviewDelays::default(),
            })
        };
        let next_followup_states =
            |state: &crate::scheduler::states::CardState| -> Result<SchedulingStates> {
                let (memory_state, days_elapsed) = match state {
                    crate::scheduler::states::CardState::Normal(
                        crate::scheduler::states::NormalState::Learning(state),
                    ) => (
                        state.memory_state.map(Into::into),
                        if state.scheduled_secs == 0 {
                            0.0
                        } else {
                            state.scheduled_secs as f32 / 86_400.0
                        },
                    ),
                    crate::scheduler::states::CardState::Normal(
                        crate::scheduler::states::NormalState::Review(state),
                    ) => (
                        state.memory_state.map(Into::into),
                        state.scheduled_days as f32,
                    ),
                    crate::scheduler::states::CardState::Normal(
                        crate::scheduler::states::NormalState::Relearning(state),
                    ) => (
                        state.learning.memory_state.map(Into::into),
                        if state.learning.scheduled_secs == 0 {
                            state.review.scheduled_days as f32
                        } else {
                            state.learning.scheduled_secs as f32 / 86_400.0
                        },
                    ),
                    crate::scheduler::states::CardState::Normal(
                        crate::scheduler::states::NormalState::New(_),
                    )
                    | crate::scheduler::states::CardState::Filtered(_) => (None, 0.0),
                };
                let ctx = make_ctx(memory_state, days_elapsed)?;
                Ok(state.next_states(&ctx))
            };
        let ctx = make_ctx(None, 0.0)?;
        let states = LearnState {
            remaining_steps: u32::from(!config.inner.learn_steps.is_empty()),
            scheduled_secs: 0,
            elapsed_secs: 0,
            memory_state: None,
        }
        .next_states(&ctx);
        let base = self.describe_next_states(&states)?;
        let again_followup_labels =
            self.describe_next_states(&next_followup_states(&states.again)?)?;
        let good_followup_labels =
            self.describe_next_states(&next_followup_states(&states.good)?)?;
        Ok(generic::StringList {
            vals: vec![
                base[0].clone(),
                base[1].clone(),
                base[2].clone(),
                base[3].clone(),
                again_followup_labels[2].clone(),
                again_followup_labels[0].clone(),
                good_followup_labels[0].clone(),
                good_followup_labels[2].clone(),
            ],
        })
    }

    fn evaluate_params(
        &mut self,
        input: scheduler::EvaluateParamsRequest,
    ) -> Result<scheduler::EvaluateParamsResponse> {
        // FSRS-7 with same-day reviews (spec sched.fsrs7-only); the request's
        // fsrs_version and include_same_day_reviews* fields are ignored
        let ret = self.evaluate_params(
            &input.search,
            input.search_for_training.as_deref(),
            input.ignore_revlogs_before_ms.into(),
            input.num_of_relearning_steps as usize,
            // always on (spec deck-options.fsrs-only-controls); the request field is ignored
            true,
        )?;
        Ok(scheduler::EvaluateParamsResponse {
            log_loss: ret.log_loss,
            rmse_bins: ret.rmse_bins,
        })
    }

    fn evaluate_params_legacy(
        &mut self,
        input: scheduler::EvaluateParamsLegacyRequest,
    ) -> Result<scheduler::EvaluateParamsResponse> {
        let ret = self.evaluate_params_legacy(
            &input.params,
            &input.search,
            input.ignore_revlogs_before_ms.into(),
        )?;
        Ok(scheduler::EvaluateParamsResponse {
            log_loss: ret.log_loss,
            rmse_bins: ret.rmse_bins,
        })
    }

    fn get_optimal_retention_parameters(
        &mut self,
        input: scheduler::GetOptimalRetentionParametersRequest,
    ) -> Result<scheduler::GetOptimalRetentionParametersResponse> {
        let revlogs = self
            .search_cards_into_table(&input.search, SortMode::NoOrder)?
            .col
            .storage
            .get_revlog_entries_for_searched_cards_in_card_order()?;
        let simulator_config = self.get_optimal_retention_parameters(revlogs)?;
        Ok(GetOptimalRetentionParametersResponse {
            deck_size: simulator_config.deck_size as u32,
            learn_span: simulator_config.learn_span as u32,
            max_cost_perday: simulator_config.max_cost_perday,
            max_ivl: simulator_config.max_ivl,
            first_rating_prob: simulator_config.first_rating_prob.to_vec(),
            review_rating_prob: simulator_config.review_rating_prob.to_vec(),
            loss_aversion: 1.0,
            learn_limit: simulator_config.learn_limit as u32,
            review_limit: simulator_config.review_limit as u32,
            learning_step_transitions: simulator_config
                .learning_step_transitions
                .iter()
                .flatten()
                .cloned()
                .collect(),
            relearning_step_transitions: simulator_config
                .relearning_step_transitions
                .iter()
                .flatten()
                .cloned()
                .collect(),
            state_rating_costs: simulator_config
                .state_rating_costs
                .iter()
                .flatten()
                .cloned()
                .collect(),
            learning_step_count: simulator_config.learning_step_count as u32,
            relearning_step_count: simulator_config.relearning_step_count as u32,
        })
    }

    fn compute_memory_state(&mut self, input: cards::CardId) -> Result<ComputeMemoryStateResponse> {
        self.compute_memory_state(input.into())
    }

    fn reschedule_all_cards_with_fsrs7(&mut self) -> Result<anki_proto::collection::OpChanges> {
        self.reschedule_all_cards_with_fsrs7().map(Into::into)
    }

    fn get_fsrs_preset_for_card(
        &mut self,
        input: cards::CardId,
    ) -> Result<FsrsPresetForCardResponse> {
        let card_id = input.into();
        let card = self.storage.get_card(card_id)?.or_not_found(card_id)?;
        let preset = self.fsrs_preset_for_card(&card)?;
        Ok(fsrs_preset_to_proto(preset))
    }

    fn fuzz_delta(&mut self, input: FuzzDeltaRequest) -> Result<FuzzDeltaResponse> {
        Ok(FuzzDeltaResponse {
            delta_days: self.get_fuzz_delta(input.card_id.into(), input.interval)?,
        })
    }

    fn scheduling_states_with_intervals(
        &mut self,
        input: SchedulingStatesWithIntervalsRequest,
    ) -> Result<anki_proto::scheduler::SchedulingStates> {
        self.scheduling_states_with_intervals(
            CardId(input.card_id),
            [input.again, input.hard, input.good, input.easy],
            [
                input.again_s90,
                input.hard_s90,
                input.good_s90,
                input.easy_s90,
            ],
        )
        .map(Into::into)
    }

    fn fsrs_current_retrievability(
        &mut self,
        input: FsrsCurrentRetrievabilityRequest,
    ) -> Result<FsrsCurrentRetrievabilityResponse> {
        Ok(FsrsCurrentRetrievabilityResponse {
            retrievability: self.fsrs_current_retrievability_for_card(
                input.card_id.into(),
                input.stability,
                input.elapsed_days,
            )?,
        })
    }

    fn fsrs_next_interval(
        &mut self,
        input: FsrsNextIntervalRequest,
    ) -> Result<FsrsNextIntervalResponse> {
        Ok(FsrsNextIntervalResponse {
            interval: self.fsrs_next_interval_for_card(
                input.card_id.into(),
                input.stability,
                input.desired_retention,
            )?,
        })
    }

    fn fsrs_interval_at_retrievability(
        &mut self,
        input: scheduler::FsrsIntervalAtRetrievabilityRequest,
    ) -> Result<FsrsIntervalAtRetrievabilityResponse> {
        Ok(FsrsIntervalAtRetrievabilityResponse {
            interval: self.fsrs_interval_at_retrievability_for_card(
                input.card_id.into(),
                input.stability,
                input.target_retrievability,
            )?,
        })
    }

    fn fsrs_interval_at_retrievability_batch(
        &mut self,
        input: FsrsIntervalAtRetrievabilityBatchRequest,
    ) -> Result<FsrsIntervalAtRetrievabilityBatchResponse> {
        let cards: Vec<(CardId, f32)> = input
            .items
            .iter()
            .map(|item| (item.card_id.into(), item.stability))
            .collect();
        let intervals =
            self.fsrs_interval_at_retrievability_for_cards(&cards, input.target_retrievability)?;
        let items = input
            .items
            .into_iter()
            .zip(intervals)
            .map(|(item, interval)| {
                scheduler::fsrs_interval_at_retrievability_batch_response::Item {
                    card_id: item.card_id,
                    interval,
                }
            })
            .collect();
        Ok(FsrsIntervalAtRetrievabilityBatchResponse { items })
    }

    fn fsrs_interval_at_retrievability_variable_batch(
        &mut self,
        input: FsrsIntervalAtRetrievabilityVariableBatchRequest,
    ) -> Result<FsrsIntervalAtRetrievabilityVariableBatchResponse> {
        let cards: Vec<(CardId, f32, f32)> = input
            .items
            .iter()
            .map(|item| {
                (
                    item.card_id.into(),
                    item.stability,
                    item.target_retrievability,
                )
            })
            .collect();
        let intervals = self.fsrs_interval_at_retrievability_for_card_targets(&cards)?;
        let items = input
            .items
            .into_iter()
            .zip(intervals)
            .map(|(item, interval)| {
                scheduler::fsrs_interval_at_retrievability_variable_batch_response::Item {
                    request_index: item.request_index,
                    interval,
                }
            })
            .collect();
        Ok(FsrsIntervalAtRetrievabilityVariableBatchResponse { items })
    }

    fn fsrs_desired_retention_for_intervals_batch(
        &mut self,
        input: FsrsDesiredRetentionForIntervalsBatchRequest,
    ) -> Result<FsrsDesiredRetentionForIntervalsBatchResponse> {
        let cards: Vec<(CardId, f32)> = input
            .items
            .iter()
            .map(|item| (item.card_id.into(), item.desired_retention))
            .collect();
        let targets = self.fsrs_desired_retention_for_intervals(&cards)?;
        let items = input
            .items
            .into_iter()
            .zip(targets)
            .map(|(item, target)| {
                scheduler::fsrs_desired_retention_for_intervals_batch_response::Item {
                    request_index: item.request_index,
                    interval_target_desired_retention: target.interval_target_desired_retention,
                }
            })
            .collect();
        Ok(FsrsDesiredRetentionForIntervalsBatchResponse { items })
    }

    fn fsrs_interval_at_retrievability_by_config_batch(
        &mut self,
        input: FsrsIntervalAtRetrievabilityByConfigBatchRequest,
    ) -> Result<FsrsIntervalAtRetrievabilityByConfigBatchResponse> {
        let configs: Vec<(DeckConfigId, f32)> = input
            .items
            .iter()
            .map(|item| (DeckConfigId(item.config_id), item.stability))
            .collect();
        let intervals = self
            .fsrs_interval_at_retrievability_for_configs(&configs, input.target_retrievability)?;
        let items = input
            .items
            .into_iter()
            .zip(intervals)
            .map(|(item, interval)| {
                scheduler::fsrs_interval_at_retrievability_by_config_batch_response::Item {
                    request_index: item.request_index,
                    interval,
                }
            })
            .collect();
        Ok(FsrsIntervalAtRetrievabilityByConfigBatchResponse { items })
    }

    fn set_rwkv_review_queue_scores(&mut self, input: RwkvReviewQueueScoresRequest) -> Result<()> {
        let scores = rwkv_score_entries(input.scores)?;
        self.set_rwkv_review_queue_score_entries(input.deck_id.into(), scores)
    }

    fn patch_answered_card_rwkv_review_queue_score(
        &mut self,
        input: RwkvAnsweredCardQueueScorePatchRequest,
    ) -> Result<()> {
        let entry = input.score.map(rwkv_score_entry).transpose()?;
        self.patch_answered_card_rwkv_review_queue_score_entry(
            input.deck_id.into(),
            input.card_id.into(),
            entry,
        )
    }

    fn set_rwkv_deck_count_scores(&mut self, input: RwkvReviewQueueScoresRequest) -> Result<()> {
        let scores = rwkv_score_entries(input.scores)?;
        self.set_rwkv_deck_count_score_entries(input.deck_id.into(), scores)
    }

    fn clear_rwkv_deck_count_scores(&mut self) -> Result<()> {
        Collection::clear_rwkv_deck_count_scores(self);
        Ok(())
    }

    fn update_rwkv_review_queue_intervening_reviews(
        &mut self,
        input: RwkvReviewQueueInterveningReviewsRequest,
    ) -> Result<()> {
        let intervening_reviews_by_card_id = input
            .items
            .into_iter()
            .map(|item| (item.card_id.into(), item.intervening_reviews))
            .collect();
        Collection::update_rwkv_review_queue_intervening_reviews(
            self,
            input.deck_id.into(),
            intervening_reviews_by_card_id,
        )
    }

    fn set_rwkv_stats_graph_scores(&mut self, input: RwkvStatsGraphScoresRequest) -> Result<()> {
        let mut scores = HashMap::with_capacity(input.scores.len());
        for score in input.scores {
            if let Some(retrievability) = score.retrievability {
                require!(
                    retrievability.is_finite() && (0.0..=1.0).contains(&retrievability),
                    "invalid RWKV retrievability"
                );
            }
            if let Some(target_retention) = score.target_retention {
                require!(
                    target_retention.is_finite() && (0.0..=1.0).contains(&target_retention),
                    "invalid RWKV target retention"
                );
            }
            if let Some(curve_retrievability) = score.curve_retrievability {
                require!(
                    curve_retrievability.is_finite() && (0.0..=1.0).contains(&curve_retrievability),
                    "invalid RWKV-Curve retrievability"
                );
            }
            scores.insert(
                score.card_id.into(),
                RwkvStatsGraphScoreEntry {
                    retrievability: score.retrievability,
                    curve_retrievability: score.curve_retrievability,
                    intervening_reviews: score.intervening_reviews,
                    target_retention: score.target_retention,
                    curve_due: score.curve_due.unwrap_or(false),
                },
            );
        }
        self.set_rwkv_stats_graph_score_entries(input.search, scores)
    }

    fn set_rwkv_card_info_score(&mut self, input: RwkvCardInfoScoreRequest) -> Result<()> {
        if let Some(retrievability) = input.retrievability {
            require!(
                retrievability.is_finite() && (0.0..=1.0).contains(&retrievability),
                "invalid RWKV retrievability"
            );
        }
        if let Some(curve_retrievability) = input.curve_retrievability {
            require!(
                curve_retrievability.is_finite() && (0.0..=1.0).contains(&curve_retrievability),
                "invalid RWKV-Curve retrievability"
            );
        }
        self.set_rwkv_card_info_scores(
            input.card_id.into(),
            input.retrievability,
            input.curve_retrievability,
        )
    }

    fn get_rwkv_retrievability_score(
        &mut self,
        input: cards::CardId,
    ) -> Result<RwkvRetrievabilityScoreResponse> {
        let days_elapsed = self.timing_today()?.days_elapsed;
        Ok(RwkvRetrievabilityScoreResponse {
            retrievability: self
                .rwkv_retrievability_score_of_algorithm(input.into(), days_elapsed)?,
        })
    }

    /// One algorithm's per-review predictions, into the generic table. The
    /// algorithm is named in the request, so a write cannot land in another
    /// algorithm's rows (spec ui.stats-model-metrics).
    fn set_review_predictions(
        &mut self,
        input: scheduler::ReviewPredictionRowsRequest,
    ) -> Result<generic::UInt32> {
        require!(!input.source.is_empty(), "missing prediction source");
        let algorithm = input.algorithm();
        let contract = crate::stats::algorithms::ALGORITHMS
            .iter()
            .find(|entry| entry.algorithm == algorithm);
        let Some(contract) = contract else {
            invalid_input!("unknown scheduling algorithm");
        };
        let mut rows = Vec::with_capacity(input.rows.len());
        for row in input.rows {
            require!(row.revlog_id > 0, "invalid review id");
            require!(
                row.prediction.is_finite() && (0.0..=1.0).contains(&row.prediction),
                "invalid prediction"
            );
            // the role must be one this algorithm's own contract allows,
            // rather than one a shared list allows
            let Some(sample_role) = contract
                .honest_roles
                .iter()
                .find(|role| **role == row.sample_role)
            else {
                invalid_input!("sample role is not one this algorithm declares");
            };
            rows.push(crate::storage::ReviewPredictionRow {
                revlog_id: RevlogId(row.revlog_id),
                prediction: row.prediction,
                sample_role,
                fold_index: row.fold_index,
            });
        }
        let stored = self
            .storage
            .set_review_predictions(algorithm as i32, &rows, &input.source)?;
        Ok(generic::UInt32 { val: stored as u32 })
    }

    fn set_rwkv_review_retrievability_cache_rows(
        &mut self,
        input: RwkvReviewRetrievabilityCacheRowsRequest,
    ) -> Result<generic::UInt32> {
        require!(
            !input.source.is_empty(),
            "missing RWKV retrievability source"
        );
        let mut rows = Vec::with_capacity(input.rows.len());
        for row in input.rows {
            require!(row.revlog_id > 0, "invalid RWKV review id");
            require!(
                row.prediction.is_finite() && (0.0..=1.0).contains(&row.prediction),
                "invalid RWKV retrievability"
            );
            let Some(sample_role) = RwkvReviewRetrievabilitySampleRole::from_str(&row.sample_role)
            else {
                invalid_input!("invalid RWKV retrievability sample role");
            };
            rows.push(RwkvReviewRetrievabilityCacheRow {
                revlog_id: RevlogId(row.revlog_id),
                prediction: row.prediction,
                sample_role,
                fold_index: row.fold_index,
            });
        }
        let count = self
            .storage
            .set_rwkv_review_retrievability_predictions(&rows, &input.source)?;
        Ok(generic::UInt32 { val: count as u32 })
    }

    fn apply_rwkv_review_reschedule(
        &mut self,
        input: RwkvReviewRescheduleRequest,
    ) -> Result<anki_proto::collection::OpChangesWithCount> {
        let items = input
            .items
            .into_iter()
            .map(|item| RwkvReviewRescheduleItem {
                card_id: item.card_id.into(),
                interval: item.interval,
                elapsed_days: item.elapsed_days,
                s90: item.s90,
                target_retention: item.target_retention,
            })
            .collect();
        self.apply_rwkv_review_reschedule(items).map(Into::into)
    }

    fn rwkv_review_input_rows_for_cards(
        &mut self,
        input: RwkvReviewInputRowsForCardsRequest,
    ) -> Result<RwkvReviewInputRowsForCardsResponse> {
        Collection::rwkv_review_input_rows_for_cards(self, input)
    }

    fn rwkv_review_input_rows_for_search(
        &mut self,
        input: RwkvReviewInputRowsForSearchRequest,
    ) -> Result<RwkvReviewInputRowsForCardsResponse> {
        Collection::rwkv_review_input_rows_for_search(self, input)
    }

    fn rwkv_review_input_rows_for_deck_review_queue(
        &mut self,
        input: RwkvReviewInputRowsForDeckReviewQueueRequest,
    ) -> Result<RwkvReviewInputRowsForCardsResponse> {
        Collection::rwkv_review_input_rows_for_deck_review_queue(self, input)
    }

    fn get_fsrs_preset_ids_for_cards(
        &mut self,
        input: cards::CardIds,
    ) -> Result<FsrsPresetIdsForCardsResponse> {
        let mut cards = Vec::with_capacity(input.cids.len());
        for cid in input.cids {
            let card_id = CardId(cid);
            cards.push(self.storage.get_card(card_id)?.or_not_found(card_id)?);
        }

        let presets = self.fsrs_presets_for_cards(&cards)?;
        let items = cards
            .into_iter()
            .filter_map(|card| {
                presets.get(card.id).map(|preset| {
                    scheduler::fsrs_preset_ids_for_cards_response::Item {
                        card_id: card.id.0,
                        preset_id: fsrs_preset_id_to_string(preset.id.clone()),
                    }
                })
            })
            .collect();

        Ok(FsrsPresetIdsForCardsResponse { items })
    }

    fn rwkv_historical_review_fingerprint(
        &mut self,
        input: RwkvHistoricalReviewFingerprintRequest,
    ) -> Result<RwkvHistoricalReviewFingerprintResponse> {
        Collection::rwkv_historical_review_fingerprint(self, input)
    }
}

fn fsrs_preset_to_proto(preset: FsrsPreset) -> FsrsPresetForCardResponse {
    FsrsPresetForCardResponse {
        id: fsrs_preset_id_to_string(preset.id),
        name: preset.name,
        fsrs_version: FsrsVersion::Seven as i32,
        params: preset.params,
        desired_retention: preset.desired_retention,
        historical_retention: preset.historical_retention,
        ignore_revlogs_before_date: preset.ignore_revlogs_before_date,
    }
}

fn fsrs_preset_id_to_string(id: FsrsPresetId) -> String {
    match id {
        FsrsPresetId::DeckConfig(id) => id.0.to_string(),
        FsrsPresetId::Addon(id) => id,
    }
}

impl crate::services::BackendSchedulerService for Backend {
    /// The collection is held to read the preset's reviews and to write the
    /// rows, never while the folds are fitted, so a user action waits only
    /// for the short read or write (spec ui.stats-fsrs-predictions-ready).
    fn refresh_fsrs_review_predictions(
        &self,
        input: scheduler::RefreshFsrsReviewPredictionsRequest,
    ) -> Result<generic::UInt32> {
        let preset = DeckConfigId(input.deck_config_id);
        let Some(job) = self.with_col(|col| col.fsrs_review_prediction_job(preset))? else {
            return Ok(0u32.into());
        };
        let rows = job.rows()?;
        Ok(self
            .with_col(|col| col.store_fsrs_review_prediction_rows(&job, &rows))?
            .into())
    }

    fn auto_optimize_fsrs_preset(
        &self,
        input: scheduler::RefreshFsrsReviewPredictionsRequest,
    ) -> Result<generic::Bool> {
        let preset = DeckConfigId(input.deck_config_id);
        let Some(job) = self.with_col(|col| col.fsrs_auto_optimize_job(preset))? else {
            return Ok(false.into());
        };
        let (key, params, fsrs_items) = job.params()?;
        Ok(self
            .with_col(|col| col.apply_fsrs_auto_optimize(key, params, fsrs_items))?
            .into())
    }

    fn compute_fsrs_params_from_items(
        &self,
        req: scheduler::ComputeFsrsParamsFromItemsRequest,
    ) -> Result<scheduler::ComputeFsrsParamsResponse> {
        let fsrs_items = req.items.len() as u32;
        let params = compute_parameters(ComputeParametersInput {
            training_config: None,
            train_set: req.items.into_iter().map(fsrs_item_proto_to_fsrs).collect(),
            card_ids: None,
            progress: None,
            enable_short_term: true,
            enable_sched_penalties: false,
            model_version: ComputeParametersVersion::default(),
            num_relearning_steps: None,
        })?;
        Ok(ComputeFsrsParamsResponse {
            params,
            fsrs_items,
            health_check_passed: None,
        })
    }

    fn fsrs_benchmark(
        &self,
        req: scheduler::FsrsBenchmarkRequest,
    ) -> Result<scheduler::FsrsBenchmarkResponse> {
        let train_set = req
            .train_set
            .into_iter()
            .map(fsrs_item_proto_to_fsrs)
            .collect();
        let params = benchmark(ComputeParametersInput {
            training_config: None,
            train_set,
            card_ids: None,
            progress: None,
            enable_short_term: true,
            enable_sched_penalties: false,
            model_version: ComputeParametersVersion::default(),
            num_relearning_steps: None,
        });
        Ok(FsrsBenchmarkResponse { params })
    }

    fn export_dataset(&self, req: scheduler::ExportDatasetRequest) -> Result<()> {
        self.with_col(|col| {
            col.export_dataset(
                req.min_entries.try_into().unwrap(),
                req.target_path.as_ref(),
            )
        })
    }
}

fn fsrs_item_proto_to_fsrs(item: anki_proto::scheduler::FsrsItem) -> FSRSItem {
    FSRSItem {
        reviews: item
            .reviews
            .into_iter()
            .map(fsrs_review_proto_to_fsrs)
            .collect(),
    }
}

fn fsrs_review_proto_to_fsrs(review: anki_proto::scheduler::FsrsReview) -> FSRSReview {
    FSRSReview {
        delta_t: review.delta_t as f32,
        rating: review.rating,
    }
}

impl Collection {
    /// The card's RWKV retrievability today under the collection's own
    /// algorithm only (spec sched.filtered-deck-one-algorithm): RWKV-Instant's
    /// rating head, RWKV-Curve's curve value, none under FSRS-7.
    fn rwkv_retrievability_score_of_algorithm(
        &mut self,
        card_id: crate::card::CardId,
        days_elapsed: u32,
    ) -> Result<Option<f32>> {
        use crate::deckconfig::algorithm::SchedulingAlgorithm;
        Ok(match self.effective_scheduling_algorithm()? {
            SchedulingAlgorithm::Fsrs7 => None,
            SchedulingAlgorithm::RwkvInstant => {
                self.rwkv_retrievability_score_for_day(card_id, days_elapsed)
            }
            SchedulingAlgorithm::RwkvCurve => self
                .rwkv_curve_retrievability_scores_for_day(days_elapsed, None)
                .and_then(|scores| scores.get(&card_id).copied()),
        })
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use super::fsrs_preset_to_proto;
    use super::FsrsVersion;
    use crate::collection::RwkvStatsGraphScoreEntry;
    use crate::config::ConfigKey;
    use crate::deckconfig::algorithm::SchedulingAlgorithm;
    use crate::prelude::*;
    use crate::scheduler::fsrs::preset::FsrsPreset;
    use crate::scheduler::fsrs::preset::FsrsPresetId;

    /// Pins spec sched.filtered-deck-one-algorithm: the RWKV retrievability
    /// the backend reports for a card is the collection's algorithm's only.
    #[test]
    fn rwkv_retrievability_score_is_the_collections_algorithms() -> Result<()> {
        let mut col = Collection::new();
        let card_id = CardId(7);
        let days_elapsed = col.timing_today()?.days_elapsed;
        col.set_rwkv_stats_graph_score_entries(
            "deck:current".to_string(),
            HashMap::from([(
                card_id,
                RwkvStatsGraphScoreEntry {
                    retrievability: Some(0.2),
                    curve_retrievability: Some(0.9),
                    intervening_reviews: None,
                    target_retention: None,
                    curve_due: false,
                },
            )]),
        )?;
        let score = |col: &mut Collection, algorithm| -> Result<Option<f32>> {
            col.set_config(ConfigKey::SchedulingAlgorithm, &algorithm)?;
            col.rwkv_retrievability_score_of_algorithm(card_id, days_elapsed)
        };
        assert_eq!(
            score(&mut col, SchedulingAlgorithm::RwkvInstant)?,
            Some(0.2)
        );
        assert_eq!(score(&mut col, SchedulingAlgorithm::RwkvCurve)?, Some(0.9));
        assert_eq!(score(&mut col, SchedulingAlgorithm::Fsrs7)?, None);
        Ok(())
    }

    #[test]
    fn fsrs_preset_response_exposes_preset_fields() {
        let response = fsrs_preset_to_proto(FsrsPreset {
            id: FsrsPresetId::Addon("addon:test".into()),
            name: "Test".into(),
            params: vec![0.0; 34],
            desired_retention: 0.86,
            historical_retention: 0.9,
            ignore_revlogs_before_date: "2024-01-01".into(),
        });

        assert_eq!(response.id, "addon:test");
        assert_eq!(response.name, "Test");
        // Pins spec/scheduling.md#sched.fsrs7-only: every preset runs FSRS-7.
        assert_eq!(response.fsrs_version, FsrsVersion::Seven as i32);
        assert_eq!(response.params, vec![0.0; 34]);
        assert_eq!(response.desired_retention, 0.86);
        assert_eq!(response.historical_retention, 0.9);
        assert_eq!(response.ignore_revlogs_before_date, "2024-01-01");
    }
}

#[cfg(test)]
mod upstream_tests;
