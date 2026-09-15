// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! What each answer button schedules under FSRS-7 and RWKV-Curve (spec
//! `sched.sub-day-intervals`).
//!
//! A button whose unrounded interval is under 12 hours stays unrounded: it
//! goes to the intraday learning queue, in seconds, without review fuzz. A
//! button at 12 hours or more gets whole days (at least one) after review
//! fuzz. Among the day
//! buttons each is at least one day above the day button before it (Again <
//! Hard < Good < Easy); among the sub-day buttons each is at least as long as
//! the sub-day button before it.

use super::fsrs_interval_as_secs;
use super::fuzz::minimum_review_fuzz_interval;
use super::StateContext;

/// Unrounded intervals from this many days on are scheduled in whole days:
/// "Anything >=12h rounds up to 1d" (Andrew, 2026-09-15).
pub(crate) const SUB_DAY_LIMIT_DAYS: f32 = 0.5;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ButtonInterval {
    /// Under 12 hours: the intraday learning queue, in seconds.
    Secs(u32),
    /// 12 hours or more: whole days after fuzz, and how far fuzz moved them.
    Days { days: u32, fuzz_delta_days: i32 },
}

/// Which day rules apply to the card being answered.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DayRule {
    /// A review card. Again is clamped to the minimum lapse interval and not
    /// fuzzed (fuzz applies when the card leaves relearning); Hard, Good and
    /// Easy keep the previous interval while it lies within the fuzz range.
    Review { previous_interval: u32 },
    /// A new, learning or relearning card: every day button is fuzzed.
    Graduating,
}

/// The four buttons' outcomes, in Again, Hard, Good, Easy order. `None` in
/// `unrounded` (days) means the button is decided elsewhere, by a learning
/// step; it gets `None` back and does not take part in the ordering.
pub(crate) fn button_intervals(
    ctx: &StateContext,
    unrounded: [Option<f32>; 4],
    rule: DayRule,
) -> [Option<ButtonInterval>; 4] {
    let sub_day_allowed = ctx.fsrs_uses_short_term_learning_queue();
    let mut previous_secs = 0;
    let mut previous_days: Option<u32> = None;
    let mut out = [None; 4];

    for (index, interval) in unrounded.into_iter().enumerate() {
        let Some(interval) = interval else {
            continue;
        };
        if sub_day_allowed && interval < SUB_DAY_LIMIT_DAYS {
            let secs =
                fsrs_interval_as_secs(interval, ctx.fsrs_minimum_interval_secs).max(previous_secs);
            previous_secs = secs;
            out[index] = Some(ButtonInterval::Secs(secs));
            continue;
        }

        let floor = previous_days.map_or(1, |days| days + 1);
        let (days, fuzz_delta_days) = match rule {
            DayRule::Review { .. } if index == 0 => {
                let (minimum, maximum) =
                    ctx.min_and_max_review_intervals(ctx.minimum_lapse_interval.max(floor));
                let days = interval
                    .clamp(minimum as f32, maximum as f32)
                    .round()
                    .max(1.0) as u32;
                (days, 0)
            }
            DayRule::Review { previous_interval } => {
                let minimum = minimum_review_fuzz_interval(
                    interval,
                    previous_interval,
                    ctx.maximum_review_interval,
                    ctx.review_fuzz_config,
                )
                .max(floor);
                let (minimum, maximum) = ctx.min_and_max_review_intervals(minimum);
                ctx.with_review_fuzz_and_delta(interval, minimum, maximum)
            }
            DayRule::Graduating => {
                let (minimum, maximum) = ctx.min_and_max_review_intervals(floor);
                ctx.with_review_fuzz_and_delta(interval.round().max(1.0), minimum, maximum)
            }
        };
        previous_days = Some(days);
        out[index] = Some(ButtonInterval::Days {
            days,
            fuzz_delta_days,
        });
    }

    out
}

#[cfg(test)]
mod test {
    use super::*;

    fn ctx() -> StateContext<'static> {
        let mut ctx = StateContext::defaults_for_testing();
        ctx.fsrs_allow_short_term = true;
        ctx.fsrs_short_term_with_steps_enabled = true;
        ctx.fuzz_factor = None;
        ctx
    }

    fn all(a: f32, h: f32, g: f32, e: f32) -> [Option<f32>; 4] {
        [Some(a), Some(h), Some(g), Some(e)]
    }

    fn days(days: u32) -> Option<ButtonInterval> {
        Some(ButtonInterval::Days {
            days,
            fuzz_delta_days: 0,
        })
    }

    fn secs(secs: u32) -> Option<ButtonInterval> {
        Some(ButtonInterval::Secs(secs))
    }

    // Pins spec/scheduling.md#sched.sub-day-intervals
    #[test]
    fn all_day_buttons_form_a_strict_chain_starting_at_again() {
        for rule in [
            DayRule::Graduating,
            DayRule::Review {
                previous_interval: 0,
            },
        ] {
            let out = button_intervals(&ctx(), all(3.0, 3.0, 3.0, 3.0), rule);
            assert_eq!(out, [days(3), days(4), days(5), days(6)], "{rule:?}");
        }
    }

    #[test]
    fn sub_day_buttons_stay_unrounded_and_never_go_backwards() {
        let out = button_intervals(&ctx(), all(0.01, 0.005, 0.25, 0.4), DayRule::Graduating);
        // 0.005 d is shorter than Again's 0.01 d, so it is raised to it
        assert_eq!(out, [secs(864), secs(864), secs(21_600), secs(34_560)]);
    }

    #[test]
    fn mixed_buttons_chain_only_among_the_day_buttons() {
        let out = button_intervals(&ctx(), all(0.1, 0.2, 1.2, 1.4), DayRule::Graduating);
        // Good is the first day button (floor 1 day); Easy is one above it
        assert_eq!(out, [secs(8640), secs(17_280), days(1), days(2)]);
    }

    // Pins spec/scheduling.md#sched.sub-day-intervals: 12 hours or more is
    // a whole day, for review cards too.
    #[test]
    fn twelve_hours_or_more_is_a_whole_day() {
        for rule in [
            DayRule::Graduating,
            DayRule::Review {
                previous_interval: 1,
            },
        ] {
            let out = button_intervals(&ctx(), all(0.25, 0.5, 0.75, 0.99), rule);
            assert_eq!(out, [secs(21_600), days(1), days(2), days(3)], "{rule:?}");
        }
    }

    #[test]
    fn a_day_or_more_is_whole_days_even_just_above_one() {
        let out = button_intervals(&ctx(), all(1.0, 1.0, 1.0, 1.0), DayRule::Graduating);
        assert_eq!(out, [days(1), days(2), days(3), days(4)]);
    }

    #[test]
    fn step_buttons_are_skipped_and_do_not_floor_later_ones() {
        let out = button_intervals(
            &ctx(),
            [None, None, Some(2.0), Some(2.0)],
            DayRule::Graduating,
        );
        assert_eq!(out, [None, None, days(2), days(3)]);
    }

    #[test]
    fn without_the_intraday_queue_sub_day_intervals_round_up_to_a_day() {
        // the intraday queue is unusable, e.g. FSRS parameters without
        // short-term terms or "Skip learning/relearning queues" on
        let mut ctx = ctx();
        ctx.fsrs_allow_short_term = false;
        let out = button_intervals(&ctx, all(0.2, 0.3, 0.4, 0.6), DayRule::Graduating);
        assert_eq!(out, [days(1), days(2), days(3), days(4)]);
    }

    #[test]
    fn the_minimum_interval_floors_sub_day_buttons() {
        let mut ctx = ctx();
        ctx.fsrs_minimum_interval_secs = 600;
        let out = button_intervals(
            &ctx,
            [Some(0.000_01), None, None, None],
            DayRule::Graduating,
        );
        assert_eq!(out, [secs(600), None, None, None]);
    }

    #[test]
    fn review_again_is_clamped_without_fuzz_and_floors_hard() {
        let mut ctx = ctx();
        ctx.fuzz_factor = Some(0.99);
        ctx.minimum_lapse_interval = 3;
        let out = button_intervals(
            &ctx,
            all(1.5, 2.0, 30.0, 40.0),
            DayRule::Review {
                previous_interval: 10,
            },
        );
        assert_eq!(
            out[0],
            Some(ButtonInterval::Days {
                days: 3,
                fuzz_delta_days: 0
            })
        );
        // Hard's target is 2 days, but it must stay above Again's 3
        let Some(ButtonInterval::Days { days: hard, .. }) = out[1] else {
            panic!("hard should be in days");
        };
        assert!(hard >= 4);
    }

    #[test]
    fn the_maximum_interval_caps_every_day_button() {
        let mut ctx = ctx();
        ctx.maximum_review_interval = 5;
        let out = button_intervals(
            &ctx,
            all(10.0, 11.0, 12.0, 13.0),
            DayRule::Review {
                previous_interval: 30,
            },
        );
        assert_eq!(out, [days(5), days(5), days(5), days(5)]);
    }
}
