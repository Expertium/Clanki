// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use anki_proto::deck_config::deck_configs_for_update::SchedulingAlgorithm as SchedulingAlgorithmProto;
use fsrs::MemoryState;
use fsrs::FSRS;

use crate::card::CardType;
use crate::card::FsrsMemoryState;
use crate::deckconfig::algorithm::SchedulingAlgorithm;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::scheduler::fsrs::memory_state::fsrs_current_retrievability_for_state;
use crate::scheduler::fsrs::memory_state::fsrs_item_for_memory_state;
use crate::scheduler::fsrs::memory_state::fsrs_memory_state_for_params;
use crate::scheduler::timing::is_unix_epoch_timestamp;

impl Collection {
    pub fn card_stats(&mut self, cid: CardId) -> Result<anki_proto::stats::CardStatsResponse> {
        let mut card = self.storage.get_card(cid)?.or_not_found(cid)?;
        let note = self
            .storage
            .get_note(card.note_id)?
            .or_not_found(card.note_id)?;
        let nt = self
            .get_notetype(note.notetype_id)?
            .or_not_found(note.notetype_id)?;
        let deck = self
            .storage
            .get_deck(card.deck_id)?
            .or_not_found(card.deck_id)?;
        let revlog = self.storage.get_revlog_entries_for_card(card.id)?;

        let (average_secs, total_secs) = average_and_total_secs_strings(&revlog);
        let timing = self.timing_today()?;

        let last_review_time = if let Some(last_review_time) = card.last_review_time {
            last_review_time
        } else {
            let mut new_card = card.clone();
            let last_review_time = self
                .storage
                .time_of_last_review(card.id)?
                .unwrap_or_default();

            new_card.last_review_time = Some(last_review_time);

            self.storage.update_card(&new_card)?;
            last_review_time
        };

        let seconds_elapsed = timing.now.elapsed_secs_since_clamped(last_review_time);

        let original_deck = if card.original_deck_id == DeckId(0) {
            deck.clone()
        } else {
            self.storage
                .get_deck(card.original_deck_id)?
                .or_not_found(card.original_deck_id)?
        };
        if card.ctype != CardType::New && card.memory_state.is_none() {
            self.compute_and_update_memory_state(&mut card)?;
        }

        let fsrs_preset = self.fsrs_preset_for_card(&card)?;

        let algorithm = self.effective_scheduling_algorithm()?;
        let advanced_ui = self.get_config_bool(BoolKey::AdvancedUi);
        // FSRS-7's curve of an RWKV-Curve card, for the toggle in Advanced
        // mode: FSRS-7's own states from the history, not the stored one,
        // which holds RWKV-Curve's S90
        let fsrs7_revlog = if advanced_ui && algorithm == SchedulingAlgorithm::RwkvCurve {
            self.stats_revlog_entries_with_historical_memory_states(&card, revlog.clone())?
        } else {
            Vec::new()
        };

        let revlog_entries =
            self.stats_revlog_entries_with_memory_state(&card, last_review_time, revlog.clone())?;
        // the chart's curves, in Advanced mode only (spec
        // ui.card-info-one-algorithm): FSRS-7's own reviews of an RWKV-Curve
        // card for the toggle, else an FSRS-7 card's reviews
        let fsrs7_curves = match algorithm {
            _ if !advanced_ui || card.memory_state.is_none() => None,
            SchedulingAlgorithm::Fsrs7 => fsrs7_curves(&fsrs_preset.params, &revlog_entries)?,
            SchedulingAlgorithm::RwkvCurve => fsrs7_curves(&fsrs_preset.params, &fsrs7_revlog)?,
            SchedulingAlgorithm::RwkvInstant => None,
        };
        let fsrs_retrievability =
            card.memory_state
                .zip(Some(seconds_elapsed))
                .map(|(state, seconds)| {
                    fsrs_current_retrievability_for_state(
                        &fsrs_preset.params,
                        state,
                        seconds as f32 / 86_400.0,
                    )
                });
        Ok(anki_proto::stats::CardStatsResponse {
            card_id: card.id.into(),
            note_id: card.note_id.into(),
            deck: deck.human_name(),
            added: card.id.as_secs().0,
            first_review: revlog
                .iter()
                .find(|entry| entry.has_rating())
                .map(|entry| entry.id.as_secs().0),
            // last_review_time is not used to ensure cram revlogs are included.
            latest_review: revlog
                .iter()
                .rfind(|entry| entry.has_rating())
                .map(|entry| entry.id.as_secs().0),
            due_date: self.due_date(&card)?,
            due_position: self.position(&card),
            interval: card.interval,
            ease: card.ease_factor as u32,
            reviews: card.reps,
            lapses: card.lapses,
            average_secs,
            total_secs,
            card_type: nt.get_template(card.template_idx)?.name.clone(),
            notetype: nt.name.clone(),
            revlog: revlog_entries,
            memory_state: card.memory_state.map(Into::into),
            fsrs_retrievability: fsrs_retrievability.transpose()?,
            custom_data: card.custom_data,
            fsrs_params: fsrs_preset.params,
            preset: fsrs_preset.name,
            original_deck: if original_deck != deck {
                Some(original_deck.human_name())
            } else {
                None
            },
            desired_retention: card.desired_retention,
            extra_rows: vec![],
            rwkv_curve: None,
            scheduling_algorithm: SchedulingAlgorithmProto::from(algorithm) as i32,
            advanced_ui,
            fsrs7_revlog,
            fsrs7_curves,
        })
    }

    pub fn get_review_logs(&mut self, cid: CardId) -> Result<anki_proto::stats::ReviewLogs> {
        let revlogs = self.storage.get_revlog_entries_for_card(cid)?;
        Ok(anki_proto::stats::ReviewLogs {
            entries: revlogs.iter().rev().map(stats_revlog_entry).collect(),
        })
    }

    fn due_date(&mut self, card: &Card) -> Result<Option<i64>> {
        Ok(match card.ctype {
            CardType::New => None,
            CardType::Review | CardType::Learn | CardType::Relearn => {
                let due = if card.original_due != 0 {
                    card.original_due
                } else {
                    card.due
                };
                if !is_unix_epoch_timestamp(due) {
                    let days_remaining = due - (self.timing_today()?.days_elapsed as i32);
                    let mut due_timestamp = TimestampSecs::now();
                    due_timestamp.0 += (days_remaining as i64) * 86_400;
                    Some(due_timestamp.0)
                } else {
                    Some(due as i64)
                }
            }
        })
    }

    fn position(&mut self, card: &Card) -> Option<i32> {
        if let Some(original_pos) = card.original_position {
            return Some(original_pos as i32);
        }
        match card.ctype {
            CardType::New => Some(card.due),
            _ => None,
        }
    }

    fn stats_revlog_entries_with_memory_state(
        self: &mut Collection,
        card: &Card,
        last_review_time: TimestampSecs,
        revlog: Vec<RevlogEntry>,
    ) -> Result<Vec<anki_proto::stats::card_stats_response::StatsRevlogEntry>> {
        Ok(with_current_memory_state_on_latest_review(
            card.memory_state,
            last_review_time,
            self.stats_revlog_entries_with_historical_memory_states(card, revlog)?,
        ))
    }

    /// The reviews, newest first, each with the memory state the card's
    /// preset's FSRS parameters give it from the history before and at it.
    fn stats_revlog_entries_with_historical_memory_states(
        self: &mut Collection,
        card: &Card,
        revlog: Vec<RevlogEntry>,
    ) -> Result<Vec<anki_proto::stats::card_stats_response::StatsRevlogEntry>> {
        let fsrs_preset = self.fsrs_preset_for_card(card)?;
        let historical_retention = fsrs_preset.historical_retention;
        let params = &fsrs_preset.params;
        let fsrs = fsrs_preset.fsrs()?;
        let ignore_before = fsrs_preset.ignore_revlogs_before_ms();

        let mut result = Vec::new();
        if let Some(item) = fsrs_item_for_memory_state(
            &fsrs,
            params,
            revlog.clone(),
            historical_retention,
            ignore_before,
        )? {
            let memory_states = fsrs.historical_memory_states(item.item, item.starting_state)?;
            let mut revlog_index = 0;
            for entry in revlog {
                let mut stats_entry = stats_revlog_entry(&entry);
                let memory_state: Option<FsrsMemoryState> = if revlog_index >= memory_states.len() {
                    // The removed revlog is in the end of the revlog, so we use the last memory
                    // state
                    Some(fsrs_memory_state_for_params(
                        params,
                        memory_states[memory_states.len() - 1],
                    )?)
                } else if entry.id == item.filtered_revlogs[revlog_index].id {
                    revlog_index += 1;
                    Some(fsrs_memory_state_for_params(
                        params,
                        memory_states[revlog_index - 1],
                    )?)
                } else if revlog_index == 0 {
                    // The removed revlog is in the start of the revlog, so we don't have a memory
                    // state for it
                    None
                } else {
                    // The removed revlog is in the middle of the revlog, so we use the memory
                    // state for the previous revlog entry
                    Some(fsrs_memory_state_for_params(
                        params,
                        memory_states[revlog_index],
                    )?)
                };
                stats_entry.memory_state = memory_state.map(|s| s.into());
                result.push(stats_entry);
            }
            Ok(result.into_iter().rev().collect())
        } else {
            Ok(revlog.iter().rev().map(stats_revlog_entry).collect())
        }
    }
}

/// Card info's elapsed times for FSRS-7's curves: 0, then
/// `FSRS7_CURVE_TIMES` times evenly spaced in log time from one minute to
/// 100 years, as RWKV-Curve's curves reach the page.
const FSRS7_CURVE_TIMES: usize = 300;
const FSRS7_CURVE_FIRST_DAYS: f32 = 1.0 / 1440.0;
const FSRS7_CURVE_LAST_DAYS: f32 = 36_500.0;

fn fsrs7_curve_elapsed_days() -> Vec<f32> {
    let step =
        (FSRS7_CURVE_LAST_DAYS / FSRS7_CURVE_FIRST_DAYS).ln() / (FSRS7_CURVE_TIMES - 1) as f32;
    std::iter::once(0.0)
        .chain((0..FSRS7_CURVE_TIMES).map(|i| FSRS7_CURVE_FIRST_DAYS * (step * i as f32).exp()))
        .collect()
}

/// FSRS-7's forgetting curve after each review of `entries` that has a
/// memory state, computed by fsrs-rs with the preset's parameters (which it
/// clips), for card info's chart: card info keeps no copy of the curve
/// (spec sched.fsrs-rs-latest). None when no review has a memory state.
fn fsrs7_curves(
    params: &[f32],
    entries: &[anki_proto::stats::card_stats_response::StatsRevlogEntry],
) -> Result<Option<anki_proto::stats::card_stats_response::Fsrs7Curves>> {
    use anki_proto::stats::card_stats_response::fsrs7_curves::Segment;
    if entries.iter().all(|entry| entry.memory_state.is_none()) {
        return Ok(None);
    }
    let fsrs = FSRS::new(params)?;
    let elapsed_days = fsrs7_curve_elapsed_days();
    let segments = entries
        .iter()
        .filter_map(|entry| {
            let state = MemoryState::from(FsrsMemoryState::from(entry.memory_state?));
            Some(Segment {
                review_time: entry.time,
                recall: elapsed_days
                    .iter()
                    .map(|&days| fsrs.current_retrievability(state, days))
                    .collect(),
            })
        })
        .collect();
    Ok(Some(anki_proto::stats::card_stats_response::Fsrs7Curves {
        elapsed_days,
        segments,
        params: params.to_vec(),
    }))
}

/// fsrs-rs's recall of each memory state at each of its elapsed days, with
/// the parameters it clips: the values card info's chart draws, one per
/// point, so the drawn FSRS-7 curve is the crate's own everywhere (spec
/// sched.fsrs-rs-latest).
pub(crate) fn fsrs_curve_recall(
    input: anki_proto::stats::FsrsCurveRecallRequest,
) -> Result<anki_proto::stats::FsrsCurveRecallResponse> {
    use anki_proto::stats::fsrs_curve_recall_response::Curve;
    let fsrs = FSRS::new(&input.params)?;
    let curves = input
        .curves
        .into_iter()
        .map(|curve| {
            let state = curve
                .memory_state
                .or_invalid("an FSRS-7 curve needs a memory state")?;
            let state = MemoryState::from(FsrsMemoryState::from(state));
            Ok(Curve {
                recall: curve
                    .elapsed_days
                    .iter()
                    .map(|&days| fsrs.current_retrievability(state, days))
                    .collect(),
            })
        })
        .collect::<Result<_>>()?;
    Ok(anki_proto::stats::FsrsCurveRecallResponse { curves })
}

fn with_current_memory_state_on_latest_review(
    memory_state: Option<FsrsMemoryState>,
    last_review_time: TimestampSecs,
    mut entries: Vec<anki_proto::stats::card_stats_response::StatsRevlogEntry>,
) -> Vec<anki_proto::stats::card_stats_response::StatsRevlogEntry> {
    if let Some(memory_state) = memory_state {
        if let Some(entry) = entries
            .iter_mut()
            .find(|entry| entry.button_chosen > 0 && entry.time == last_review_time.0)
        {
            entry.memory_state = Some(memory_state.into());
        }
    }
    entries
}

fn average_and_total_secs_strings(revlog: &[RevlogEntry]) -> (f32, f32) {
    let normal_answer_count = revlog.iter().filter(|r| r.has_rating()).count();
    let total_secs: f32 = revlog
        .iter()
        .map(|entry| (entry.taken_millis as f32) / 1000.0)
        .sum();
    if normal_answer_count == 0 || total_secs == 0.0 {
        (0.0, 0.0)
    } else {
        (total_secs / normal_answer_count as f32, total_secs)
    }
}

fn stats_revlog_entry(
    entry: &RevlogEntry,
) -> anki_proto::stats::card_stats_response::StatsRevlogEntry {
    anki_proto::stats::card_stats_response::StatsRevlogEntry {
        time: entry.id.as_secs().0,
        review_kind: entry.review_kind.into(),
        button_chosen: entry.button_chosen as u32,
        interval: entry.interval_secs(),
        ease: entry.ease_factor,
        taken_secs: entry.taken_millis as f32 / 1000.,
        memory_state: None,
        last_interval: entry.last_interval_secs(),
    }
}

#[cfg(test)]
mod test {
    use anki_proto::deck_config::deck_configs_for_update::current_deck::Limits;
    use anki_proto::deck_config::UpdateDeckConfigsMode;

    use super::*;
    use crate::card::FsrsMemoryState;
    use crate::deckconfig::FsrsVersion;
    use crate::deckconfig::UpdateDeckConfigsRequest;
    use crate::revlog::RevlogEntry;
    use crate::revlog::RevlogReviewKind;
    use crate::scheduler::fsrs::memory_state::fsrs_current_retrievability_for_state;
    use crate::scheduler::fsrs::preset::AddonFsrsPreset;
    use crate::scheduler::fsrs::preset::AddonFsrsVersion;
    use crate::scheduler::fsrs::preset::FsrsPresetOverlay;
    use crate::scheduler::fsrs::preset::FsrsPresetRule;
    use crate::scheduler::fsrs::preset::FSRS_PRESET_OVERLAY_CONFIG_KEY;
    use crate::search::SortMode;

    fn fsrs7_params_for_retrievability_test() -> Vec<f32> {
        vec![
            0.4843, 3.0562, 10.9946, 32.7202, 5.6296, 0.5900, 3.1230, 2.4679, 0.2733, 1.4895,
            0.4868, 0.0010, 0.8082, 0.1723, 0.6389, 1.5767, 0.8918, 0.3341, 3.5942, 0.3455, 0.0022,
            0.2834, 2.6418, 0.5604, 1.3042, 2.5054, 0.9376, 0.0611, 0.0830, 0.6339, 0.9846, 0.2485,
            0.6014, 0.0545,
        ]
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

    fn test_collection() -> Result<(Collection, CardId)> {
        let mut col = Collection::new();
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;
        let cid = col.search_cards("", SortMode::NoOrder)?[0];
        Ok((col, cid))
    }

    #[test]
    fn stats() -> Result<()> {
        let (mut col, cid) = test_collection()?;
        let _report = col.card_stats(cid)?;

        Ok(())
    }

    // Pins spec/scheduling.md#sched.fsrs7-bad-ignore-before-date: card info
    // reads an unparsable "ignore reviews before" date as no date.
    #[test]
    fn card_stats_survive_a_bad_ignore_before_date() -> Result<()> {
        let (mut col, cid) = test_collection()?;
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        col.update_default_deck_config(|config| config.rwkv_review_enabled = false);
        col.grade_now(anki_proto::scheduler::GradeNowRequest {
            card_ids: vec![cid.into()],
            rating: anki_proto::scheduler::card_answer::Rating::Good as i32,
            card_options: vec![],
        })?;
        col.update_default_deck_config(|config| {
            config.ignore_revlogs_before_date = "2024-02-30".into();
        });
        let stats = col.card_stats(cid)?;
        assert!(stats.revlog[0].memory_state.is_some());
        Ok(())
    }

    // Pins spec/ui.md#ui.card-info-one-algorithm
    #[test]
    fn card_stats_report_the_algorithm_and_the_mode() -> Result<()> {
        let (mut col, cid) = test_collection()?;
        let stats = col.card_stats(cid)?;
        // the test collection's Default preset runs FSRS-7; Simple mode is
        // the default
        assert_eq!(
            stats.scheduling_algorithm(),
            SchedulingAlgorithmProto::Fsrs7
        );
        assert!(!stats.advanced_ui);

        col.update_default_deck_config(|config| config.rwkv_review_instant_order_enabled = true);
        col.set_config_bool(BoolKey::AdvancedUi, true, false)?;
        let stats = col.card_stats(cid)?;
        assert_eq!(
            stats.scheduling_algorithm(),
            SchedulingAlgorithmProto::RwkvInstant
        );
        assert!(stats.advanced_ui);
        Ok(())
    }

    // Pins spec/ui.md#ui.card-info-rwkv-curve (the FSRS-7 / RWKV-Curve toggle)
    #[test]
    fn an_rwkv_curve_card_carries_fsrs7s_own_states_only_in_advanced_mode() -> Result<()> {
        let (mut col, cid) = test_collection()?;
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        col.grade_now(anki_proto::scheduler::GradeNowRequest {
            card_ids: vec![cid.into()],
            rating: anki_proto::scheduler::card_answer::Rating::Good as i32,
            card_options: vec![],
        })?;
        // FSRS-7 draws its own curve from the plain reviews
        assert!(col.card_stats(cid)?.fsrs7_revlog.is_empty());

        // RWKV-Curve in Simple mode has no toggle
        col.update_default_deck_config(|config| config.rwkv_review_enabled = true);
        assert!(col.card_stats(cid)?.fsrs7_revlog.is_empty());

        // under RWKV-Curve the card stores RWKV-Curve's S90, not FSRS-7's
        col.set_config_bool(BoolKey::AdvancedUi, true, false)?;
        let mut card = col.storage.get_card(cid)?.unwrap();
        card.memory_state.as_mut().unwrap().stability = 1234.0;
        col.storage.update_card(&card)?;
        let stats = col.card_stats(cid)?;
        assert_eq!(
            stats.scheduling_algorithm(),
            SchedulingAlgorithmProto::RwkvCurve
        );
        assert_eq!(stats.fsrs7_revlog.len(), stats.revlog.len());
        assert_eq!(stats.fsrs7_revlog[0].time, stats.revlog[0].time);
        // the toggle's reviews carry FSRS-7's own state from the history...
        let own = stats.fsrs7_revlog[0].memory_state.unwrap();
        assert_ne!(own.stability, 1234.0);
        assert!(own.stability > 0.0);
        // ...while the plain reviews keep the stored one, as before
        assert_eq!(stats.revlog[0].memory_state.unwrap().stability, 1234.0);
        Ok(())
    }

    // Pins spec/scheduling.md#sched.fsrs-rs-latest (card info's curve):
    // card info gets FSRS-7's curves from fsrs-rs, after each review, only in
    // Advanced mode, and for an RWKV-Curve card from FSRS-7's own states.
    #[test]
    fn card_info_curves_are_the_crates_own() -> Result<()> {
        let (mut col, cid) = test_collection()?;
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        col.update_default_deck_config(|config| config.rwkv_review_enabled = false);
        for rating in [
            anki_proto::scheduler::card_answer::Rating::Good,
            anki_proto::scheduler::card_answer::Rating::Hard,
        ] {
            col.grade_now(anki_proto::scheduler::GradeNowRequest {
                card_ids: vec![cid.into()],
                rating: rating as i32,
                card_options: vec![],
            })?;
        }
        // Simple mode draws no curve
        assert!(col.card_stats(cid)?.fsrs7_curves.is_none());

        col.set_config_bool(BoolKey::AdvancedUi, true, false)?;
        let stats = col.card_stats(cid)?;
        let curves = stats.fsrs7_curves.unwrap();
        assert_eq!(curves.elapsed_days.len(), 301);
        assert_eq!(curves.elapsed_days[0], 0.0);
        assert!((curves.elapsed_days[1] - 1.0 / 1440.0).abs() < 1e-7);
        assert!((curves.elapsed_days[300] - 36_500.0).abs() < 1.0);
        let fsrs = FSRS::new(&stats.fsrs_params)?;
        let with_state: Vec<_> = stats
            .revlog
            .iter()
            .filter(|entry| entry.memory_state.is_some())
            .collect();
        assert_eq!(curves.segments.len(), with_state.len());
        for (entry, segment) in with_state.iter().zip(&curves.segments) {
            assert_eq!(segment.review_time, entry.time);
            let state = MemoryState::from(FsrsMemoryState::from(entry.memory_state.unwrap()));
            for (&days, &recall) in curves.elapsed_days.iter().zip(&segment.recall) {
                assert_eq!(recall, fsrs.current_retrievability(state, days));
            }
            // the chart's own points, between and beyond the grid: the
            // crate's value at each one, nothing joined by straight lines
            let chart_days: Vec<f32> = (1..2000).map(|step| step as f32 * 0.0537).collect();
            let recall = fsrs_curve_recall(anki_proto::stats::FsrsCurveRecallRequest {
                params: curves.params.clone(),
                curves: vec![anki_proto::stats::fsrs_curve_recall_request::Curve {
                    memory_state: entry.memory_state,
                    elapsed_days: chart_days.clone(),
                }],
            })?;
            assert_eq!(recall.curves.len(), 1);
            for (&days, &recall) in chart_days.iter().zip(&recall.curves[0].recall) {
                assert_eq!(recall, fsrs.current_retrievability(state, days));
            }
        }
        assert_eq!(curves.params, stats.fsrs_params);

        // an RWKV-Curve card: from FSRS-7's own states, not the stored S90
        col.update_default_deck_config(|config| config.rwkv_review_enabled = true);
        let mut card = col.storage.get_card(cid)?.unwrap();
        card.memory_state.as_mut().unwrap().stability = 1234.0;
        col.storage.update_card(&card)?;
        let stats = col.card_stats(cid)?;
        let curves = stats.fsrs7_curves.unwrap();
        let own = stats.fsrs7_revlog[0].memory_state.unwrap();
        assert_eq!(curves.segments[0].review_time, stats.fsrs7_revlog[0].time);
        let state = MemoryState::from(FsrsMemoryState::from(own));
        assert_eq!(
            curves.segments[0].recall[150],
            fsrs.current_retrievability(state, curves.elapsed_days[150])
        );
        Ok(())
    }

    #[test]
    fn stats_calculate_memory_state_if_not_present() -> Result<()> {
        let (mut col, cid) = test_collection()?;

        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        // review the card as easy
        col.grade_now(anki_proto::scheduler::GradeNowRequest {
            card_ids: vec![cid.into()],
            rating: anki_proto::scheduler::card_answer::Rating::Easy as i32,
            card_options: vec![],
        })?;
        let mut card = col.storage.get_card(cid)?.unwrap();
        assert!(card.memory_state.is_some());

        card.memory_state = None;
        col.storage.update_card(&card)?;

        let card = col.storage.get_card(cid)?.unwrap();
        assert!(card.memory_state.is_none());

        let report = col.card_stats(cid)?;
        let card = col.storage.get_card(cid)?.unwrap();

        assert!(report.memory_state.is_some());
        assert!(card.memory_state.is_some());

        Ok(())
    }

    #[test]
    fn card_stats_retrievability_uses_selected_model_curve() -> Result<()> {
        let mut col = Collection::new();
        let params = fsrs7_params_for_retrievability_test();
        set_selected_fsrs7_params(&mut col, params.clone())?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;

        let cid = col.search_cards("", SortMode::NoOrder)?[0];
        let mut card = col.storage.get_card(cid)?.unwrap();
        let stability = 42.0;
        let elapsed_days = 120.0;
        let timing = col.timing_today()?;
        let state = FsrsMemoryState {
            stability,
            stability_internal: stability,
            stability_fast: Some(17.0),
            difficulty: 8.0,
        };
        card.memory_state = Some(state);
        card.last_review_time = Some(timing.now.adding_secs(-(elapsed_days as i64) * 86_400));
        card.decay = Some(params[23]);
        col.storage.update_card(&card)?;

        let report = col.card_stats(cid)?;
        let expected = fsrs_current_retrievability_for_state(&params, state, elapsed_days)?;
        assert_eq!(
            report.fsrs_retrievability.map(|v| format!("{v:.6}")),
            Some(format!("{expected:.6}"))
        );
        Ok(())
    }

    #[test]
    fn card_stats_uses_card_level_fsrs_preset() -> Result<()> {
        let mut col = Collection::new();
        let params = fsrs7_params_for_retrievability_test();

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;

        col.set_config(
            FSRS_PRESET_OVERLAY_CONFIG_KEY,
            &FsrsPresetOverlay {
                presets: vec![AddonFsrsPreset {
                    id: "addon:test:card-info".into(),
                    name: "Card Info Dynamic".into(),
                    fsrs_version: AddonFsrsVersion::Seven,
                    params: params.clone(),
                    desired_retention: 0.81,
                    historical_retention: 0.9,
                    ignore_revlogs_before_date: String::new(),
                }],
                rules: vec![FsrsPresetRule {
                    search: "deck:Default".into(),
                    preset_id: "addon:test:card-info".into(),
                }],
                simulator_rules: Vec::new(),
            },
        )?;

        let cid = col.search_cards("", SortMode::NoOrder)?[0];
        let report = col.card_stats(cid)?;
        assert_eq!(report.preset, "Card Info Dynamic");
        assert_eq!(report.fsrs_params, params);
        Ok(())
    }

    #[test]
    fn card_stats_latest_revlog_uses_current_memory_state() -> Result<()> {
        let mut col = Collection::new();
        let params = fsrs7_params_for_retrievability_test();
        set_selected_fsrs7_params(&mut col, params.clone())?;

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, DeckId(1))?;

        let cid = col.search_cards("", SortMode::NoOrder)?[0];
        let last_review_time = TimestampSecs::now();
        let stability = 0.0101;
        let stability_internal = 0.0733;
        let mut card = col.storage.get_card(cid)?.unwrap();
        card.memory_state = Some(FsrsMemoryState {
            stability,
            stability_internal,
            stability_fast: None,
            difficulty: 9.168,
        });
        card.last_review_time = Some(last_review_time);
        card.decay = Some(params[23]);
        col.storage.update_card(&card)?;
        col.storage.add_revlog_entry(
            &RevlogEntry {
                id: RevlogId(last_review_time.0 * 1000),
                cid,
                usn: Usn(0),
                button_chosen: 3,
                review_kind: RevlogReviewKind::Learning,
                ..Default::default()
            },
            false,
        )?;

        let report = col.card_stats(cid)?;
        let latest = report.revlog[0].memory_state.as_ref().unwrap();
        assert!((latest.stability - stability).abs() < 0.0001);
        assert!((latest.stability_internal.unwrap() - stability_internal).abs() < 0.0001);
        Ok(())
    }
}
