// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
use std::collections::HashMap;

use chrono::Datelike;

use crate::prelude::*;
use crate::scheduler::answering::get_fuzz_factor;
use crate::scheduler::states::fuzz::constrained_fuzz_bounds;
use crate::scheduler::states::fuzz::minimum_review_fuzz_interval;
use crate::scheduler::states::fuzz::with_review_fuzz;
use crate::scheduler::states::fuzz::ReviewFuzzConfig;
use crate::scheduler::states::load_balancer::build_easy_days_percentages;
use crate::scheduler::states::load_balancer::calculate_easy_days_modifiers;
use crate::scheduler::states::load_balancer::select_weighted_interval;
use crate::scheduler::states::load_balancer::EasyDay;
use crate::scheduler::states::load_balancer::LoadBalancerInterval;

pub struct Rescheduler {
    today: i32,
    next_day_at: TimestampSecs,
    due_cnt_per_day_by_preset: HashMap<DeckConfigId, HashMap<i32, usize>>,
    due_today_by_preset: HashMap<DeckConfigId, usize>,
    reviewed_today_by_preset: HashMap<DeckConfigId, usize>,
    easy_days_percentages_by_preset: HashMap<DeckConfigId, [EasyDay; 7]>,
    review_fuzz_config: ReviewFuzzConfig,
}

impl Rescheduler {
    pub fn new(col: &mut Collection) -> Result<Self> {
        let timing = col.timing_today()?;
        let deck_stats = col.storage.get_deck_due_counts()?;
        let deck_map = col.storage.get_decks_map()?;
        let did_to_dcid = deck_map
            .values()
            .filter_map(|deck| Some((deck.id, deck.config_id()?)))
            .collect::<HashMap<_, _>>();

        let mut due_cnt_per_day_by_preset: HashMap<DeckConfigId, HashMap<i32, usize>> =
            HashMap::new();
        for (did, due_date, count) in deck_stats {
            let deck_config_id = did_to_dcid.get(&did).or_not_found(did)?;
            due_cnt_per_day_by_preset
                .entry(*deck_config_id)
                .or_default()
                .entry(due_date)
                .and_modify(|e| *e += count)
                .or_insert(count);
        }

        let today = timing.days_elapsed as i32;
        let due_today_by_preset = due_cnt_per_day_by_preset
            .iter()
            .map(|(deck_config_id, config_dues)| {
                let due_today = config_dues
                    .iter()
                    .filter(|(&due, _)| due <= today)
                    .map(|(_, &count)| count)
                    .sum();
                (*deck_config_id, due_today)
            })
            .collect();

        let next_day_at = timing.next_day_at;
        let reviewed_stats = col.storage.studied_today_by_deck(timing.next_day_at)?;
        let mut reviewed_today_by_preset: HashMap<DeckConfigId, usize> = HashMap::new();
        for (did, count) in reviewed_stats {
            if let Some(&deck_config_id) = &did_to_dcid.get(&did) {
                *reviewed_today_by_preset.entry(deck_config_id).or_default() += count;
            }
        }

        let configs = col.storage.get_deck_config_map()?;
        let easy_days_percentages_by_preset = build_easy_days_percentages(&configs)?;
        let review_fuzz_config = col.review_fuzz_config();

        Ok(Self {
            today,
            next_day_at,
            due_cnt_per_day_by_preset,
            due_today_by_preset,
            reviewed_today_by_preset,
            easy_days_percentages_by_preset,
            review_fuzz_config,
        })
    }

    pub fn update_due_cnt_per_day(
        &mut self,
        due_before: i32,
        due_after: i32,
        deck_config_id: DeckConfigId,
    ) {
        if let Some(counts) = self.due_cnt_per_day_by_preset.get_mut(&deck_config_id) {
            if let Some(count) = counts.get_mut(&due_before) {
                *count -= 1;
            }
            *counts.entry(due_after).or_default() += 1;
        }

        if due_before <= self.today && due_after > self.today {
            if let Some(count) = self.due_today_by_preset.get_mut(&deck_config_id) {
                *count -= 1;
            }
        }
        if due_before > self.today && due_after <= self.today {
            *self.due_today_by_preset.entry(deck_config_id).or_default() += 1;
        }
    }

    fn due_today(&self, deck_config_id: DeckConfigId) -> usize {
        *self.due_today_by_preset.get(&deck_config_id).unwrap_or(&0)
    }

    fn reviewed_today(&self, deck_config_id: DeckConfigId) -> usize {
        *self
            .reviewed_today_by_preset
            .get(&deck_config_id)
            .unwrap_or(&0)
    }

    pub fn find_interval(
        &self,
        interval: f32,
        minimum_interval: u32,
        maximum_interval: u32,
        days_elapsed: u32,
        deckconfig_id: DeckConfigId,
        fuzz_seed: Option<u64>,
    ) -> Option<u32> {
        let (before_days, after_days) = constrained_fuzz_bounds(
            interval,
            minimum_interval,
            maximum_interval,
            self.review_fuzz_config,
        );

        // Don't reschedule the card when it's overdue
        if after_days < days_elapsed {
            return None;
        }
        // Don't reschedule the card to the past
        let before_days = before_days.max(days_elapsed);

        // Generate possible intervals and their review counts
        let possible_intervals: Vec<u32> = (before_days..=after_days).collect();
        let review_counts: Vec<usize> = possible_intervals
            .iter()
            .map(|&ivl| {
                if ivl > days_elapsed {
                    let check_due = self.today + ivl as i32 - days_elapsed as i32;
                    *self
                        .due_cnt_per_day_by_preset
                        .get(&deckconfig_id)
                        .and_then(|counts| counts.get(&check_due))
                        .unwrap_or(&0)
                } else {
                    // today's workload is the sum of backlogs, cards due today and cards reviewed
                    // today
                    self.due_today(deckconfig_id) + self.reviewed_today(deckconfig_id)
                }
            })
            .collect();
        let weekdays: Vec<usize> = possible_intervals
            .iter()
            .map(|&ivl| {
                self.next_day_at
                    .adding_secs(days_elapsed as i64 * -86400)
                    .adding_secs((ivl - 1) as i64 * 86400)
                    .local_datetime()
                    .unwrap()
                    .weekday()
                    .num_days_from_monday() as usize
            })
            .collect();

        let easy_days_load = self.easy_days_percentages_by_preset.get(&deckconfig_id)?;
        let easy_days_modifier =
            calculate_easy_days_modifiers(easy_days_load, &weekdays, &review_counts);

        let intervals =
            possible_intervals
                .iter()
                .enumerate()
                .map(|(interval_index, &target_interval)| LoadBalancerInterval {
                    target_interval,
                    review_count: review_counts[interval_index],
                    sibling_modifier: 1.0,
                    easy_days_modifier: easy_days_modifier[interval_index],
                });

        select_weighted_interval(intervals, fuzz_seed)
    }
}

/// Whole days for a review card that a reschedule gives the unrounded
/// `interval`: at least the previous interval when the new one still reaches
/// it within the fuzz range (`minimum_review_fuzz_interval`), at least 1, at
/// most `max_interval`; the load balancer and Easy Days pick the day when
/// `rescheduler` is given (and the card is not overdue past the fuzz range),
/// else review fuzz does. `fuzz_seed` is the card's seed for its previous
/// review.
#[allow(clippy::too_many_arguments)]
pub(crate) fn rescheduled_interval_days(
    rescheduler: Option<&Rescheduler>,
    interval: f32,
    previous_interval: u32,
    max_interval: u32,
    days_elapsed: u32,
    deckconfig_id: DeckConfigId,
    fuzz_seed: Option<u64>,
    review_fuzz_config: ReviewFuzzConfig,
) -> u32 {
    let min_interval = minimum_review_fuzz_interval(
        interval,
        previous_interval,
        max_interval,
        review_fuzz_config,
    )
    .max(1);
    rescheduler
        .and_then(|rescheduler| {
            rescheduler.find_interval(
                interval,
                min_interval,
                max_interval,
                days_elapsed,
                deckconfig_id,
                fuzz_seed,
            )
        })
        .unwrap_or_else(|| {
            with_review_fuzz(
                get_fuzz_factor(fuzz_seed),
                interval,
                min_interval,
                max_interval,
                review_fuzz_config,
            )
        })
}

#[cfg(test)]
mod test {
    use std::collections::HashSet;

    use super::*;

    // Pins spec/scheduling.md#sched.rwkv-curve-reschedule (the step the
    // FSRS-7 and RWKV-Curve reschedules share): with the cards' fuzz seeds,
    // cards given the same interval spread over its fuzz range, through the
    // load balancer and through plain review fuzz.
    #[test]
    fn rescheduled_interval_days_are_fuzzed_and_load_balanced() -> Result<()> {
        let mut col = Collection::new();
        let load_balancer = Rescheduler::new(&mut col)?;
        let config = ReviewFuzzConfig::default();
        let (lower, upper) = constrained_fuzz_bounds(50.0, 1, 36_500, config);
        for rescheduler in [None, Some(&load_balancer)] {
            let days = (0..20)
                .map(|seed| {
                    rescheduled_interval_days(
                        rescheduler,
                        50.0,
                        0,
                        36_500,
                        5,
                        DeckConfigId(1),
                        Some(1_000 + seed),
                        config,
                    )
                })
                .collect::<HashSet<_>>();
            assert!(days.len() > 3, "{days:?}");
            assert!(
                days.iter().all(|day| (lower..=upper).contains(day)),
                "{days:?}"
            );
        }
        Ok(())
    }
}
