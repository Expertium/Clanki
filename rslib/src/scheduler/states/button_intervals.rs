// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! What each answer button schedules under FSRS-7 and RWKV-Curve (spec
//! `sched.sub-day-intervals`).
//!
//! A button decided by a learning or relearning step keeps the step's delay.
//! A button whose unrounded interval is under 18 hours stays unrounded: it
//! goes to the intraday learning queue, in seconds, without review fuzz. A
//! button at 18 hours or more gets whole days (at least one) after review
//! fuzz. Every button is at least as long as the buttons before it (Again <=
//! Hard <= Good <= Easy), steps included: a sub-day button is at least as
//! long as the sub-day button or sub-day step before it, and a day button is
//! at least one day above the day button or day-long step before it. A
//! button after a day button or a day-long step is a day button.

use super::fsrs_interval_as_secs;
use super::fuzz::minimum_review_fuzz_interval;
use super::StateContext;
use super::SECONDS_PER_DAY;

/// Unrounded intervals from this many days on are scheduled in whole days.
/// Andrew, 2026-09-15: "Anything >=12h rounds up to 1d"; 2026-09-21: "Raise
/// that to 18h".
pub(crate) const SUB_DAY_LIMIT_DAYS: f32 = 0.75;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ButtonInterval {
    /// Under 18 hours: the intraday learning queue, in seconds.
    Secs(u32),
    /// 18 hours or more: whole days after fuzz, and how far fuzz moved them.
    Days { days: u32, fuzz_delta_days: i32 },
}

/// What decides one answer button.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) enum ButtonInput {
    /// A learning or relearning step with this delay decides the button. The
    /// step keeps its delay, and the buttons after it are at least as long.
    Step { secs: u32 },
    /// The model's (FSRS-7's or RWKV-Curve's) unrounded interval, in days.
    Model { days: f32 },
}

impl ButtonInput {
    /// The step's delay if a step decides the button, else the model's
    /// interval.
    pub(crate) fn new(step_secs: Option<u32>, model_days: f32) -> Self {
        match step_secs {
            Some(secs) => Self::Step { secs },
            None => Self::Model { days: model_days },
        }
    }
}

/// Which day rules apply to the card being answered.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DayRule {
    /// A review card. Again is clamped to the minimum lapse interval and not
    /// fuzzed (fuzz applies when the card leaves relearning); Hard, Good and
    /// Easy keep the previous interval while it lies within the fuzz range.
    Review { previous_interval: u32 },
    /// A relearning card: Again follows the review rule (clamped to the
    /// minimum lapse interval, not fuzzed); Hard, Good and Easy leave
    /// relearning and are fuzzed like `Graduating`.
    Relearning,
    /// A new or learning card: every day button is fuzzed.
    Graduating,
}

/// The four buttons' outcomes, in Again, Hard, Good, Easy order. A button
/// decided by a step gets `None` back (the step decides it elsewhere), but
/// its delay still floors the buttons after it.
pub(crate) fn button_intervals(
    ctx: &StateContext,
    inputs: [ButtonInput; 4],
    rule: DayRule,
) -> [Option<ButtonInterval>; 4] {
    let sub_day_allowed = ctx.fsrs_uses_short_term_learning_queue();
    let mut previous_secs = 0;
    let mut previous_days: Option<u32> = None;
    let mut out = [None; 4];

    for (index, input) in inputs.into_iter().enumerate() {
        let interval = match input {
            ButtonInput::Step { secs } => {
                if (secs as f32) < SUB_DAY_LIMIT_DAYS * SECONDS_PER_DAY {
                    previous_secs = previous_secs.max(secs);
                } else {
                    // a day-long step counts as its delay in whole days,
                    // rounded up
                    let days = secs.div_ceil(SECONDS_PER_DAY as u32).max(1);
                    previous_days = Some(previous_days.map_or(days, |d| d.max(days)));
                }
                continue;
            }
            ButtonInput::Model { days } => days,
        };
        if sub_day_allowed && interval < SUB_DAY_LIMIT_DAYS && previous_days.is_none() {
            let secs =
                fsrs_interval_as_secs(interval, ctx.fsrs_minimum_interval_secs).max(previous_secs);
            previous_secs = secs;
            out[index] = Some(ButtonInterval::Secs(secs));
            continue;
        }

        let floor = previous_days.map_or(1, |days| days + 1);
        let (days, fuzz_delta_days) = match rule {
            DayRule::Review { .. } | DayRule::Relearning if index == 0 => {
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
            DayRule::Graduating | DayRule::Relearning => {
                // fuzz and the load balancer take the unrounded interval, as
                // for review cards; the minimum (at least 1) keeps it a day
                let (minimum, maximum) = ctx.min_and_max_review_intervals(floor);
                ctx.with_review_fuzz_and_delta(interval, minimum, maximum)
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

    fn all(a: f32, h: f32, g: f32, e: f32) -> [ButtonInput; 4] {
        [model(a), model(h), model(g), model(e)]
    }

    fn model(days: f32) -> ButtonInput {
        ButtonInput::Model { days }
    }

    fn step(secs: u32) -> ButtonInput {
        ButtonInput::Step { secs }
    }

    /// A zero-second step: it floors nothing, so the button takes no part.
    fn skip() -> ButtonInput {
        step(0)
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

    // Pins spec/scheduling.md#sched.sub-day-intervals: 18 hours or more is
    // a whole day, for review cards too, and below it stays in seconds.
    #[test]
    fn eighteen_hours_or_more_is_a_whole_day() {
        for rule in [
            DayRule::Graduating,
            DayRule::Review {
                previous_interval: 1,
            },
        ] {
            // 6h and 12h stay sub-day; 18h and 23.76h become whole days
            let out = button_intervals(&ctx(), all(0.25, 0.5, 0.75, 0.99), rule);
            assert_eq!(
                out,
                [secs(21_600), secs(43_200), days(1), days(2)],
                "{rule:?}"
            );
        }
    }

    // The boundary itself: just under 18 hours is seconds, exactly 18 hours
    // is a day.
    #[test]
    fn the_sub_day_limit_is_eighteen_hours() {
        assert_eq!(SUB_DAY_LIMIT_DAYS, 0.75);
        let out = button_intervals(
            &ctx(),
            [model(0.749_99), skip(), skip(), skip()],
            DayRule::Graduating,
        );
        let Some(ButtonInterval::Secs(_)) = out[0] else {
            panic!("just under 18 hours should stay in seconds");
        };
        let out = button_intervals(
            &ctx(),
            [model(0.75), skip(), skip(), skip()],
            DayRule::Graduating,
        );
        assert_eq!(out[0], days(1));
    }

    #[test]
    fn a_day_or_more_is_whole_days_even_just_above_one() {
        let out = button_intervals(&ctx(), all(1.0, 1.0, 1.0, 1.0), DayRule::Graduating);
        assert_eq!(out, [days(1), days(2), days(3), days(4)]);
    }

    #[test]
    fn step_buttons_get_none_back() {
        let out = button_intervals(
            &ctx(),
            [step(60), step(330), model(2.0), model(2.0)],
            DayRule::Graduating,
        );
        assert_eq!(out, [None, None, days(2), days(3)]);
    }

    // Pins spec/scheduling.md#sched.sub-day-intervals: a model button is at
    // least as long as a sub-day step before it.
    #[test]
    fn a_sub_day_step_floors_the_sub_day_buttons_after_it() {
        let out = button_intervals(
            &ctx(),
            [step(600), step(900), model(0.005), model(0.02)],
            DayRule::Graduating,
        );
        // Good's 432 s is raised to Hard's 900 s step; Easy's 1728 s stays
        assert_eq!(out, [None, None, secs(900), secs(1728)]);
    }

    // Pins spec/scheduling.md#sched.sub-day-intervals: the review's two
    // cases with steps "10m 1d" and default FSRS-7 parameters, where Easy
    // (and Good) came out shorter than a step before them.
    #[test]
    fn a_day_long_step_makes_the_buttons_after_it_day_buttons() {
        // a new card after Again, at the 10 m step: Hard 12 h 5 m (step),
        // Good 1 d (step), Easy 2.25 h (model)
        let out = button_intervals(
            &ctx(),
            [step(600), step(43_500), step(86_400), model(0.094)],
            DayRule::Graduating,
        );
        assert_eq!(out, [None, None, None, days(2)]);
        // at the 1 d step: Hard 1 d (step), Good 2.7 h and Easy 3.3 h
        // (model)
        let out = button_intervals(
            &ctx(),
            [step(600), step(86_400), model(0.1125), model(0.1375)],
            DayRule::Graduating,
        );
        assert_eq!(out, [None, None, days(2), days(3)]);
        // the same for a relearning card
        let out = button_intervals(
            &ctx(),
            [step(600), step(86_400), model(0.1125), model(0.1375)],
            DayRule::Relearning,
        );
        assert_eq!(out, [None, None, days(2), days(3)]);
    }

    #[test]
    fn a_day_long_step_counts_as_its_delay_rounded_up_to_days() {
        // 18 hours is a day-long step (1 day); 36 hours is 2 days
        let out = button_intervals(
            &ctx(),
            [step(600), step(64_800), model(0.2), model(1.0)],
            DayRule::Graduating,
        );
        assert_eq!(out, [None, None, days(2), days(3)]);
        let out = button_intervals(
            &ctx(),
            [step(600), step(129_600), model(0.2), model(5.0)],
            DayRule::Graduating,
        );
        assert_eq!(out, [None, None, days(3), days(5)]);
    }

    // Pins spec/scheduling.md#sched.sub-day-intervals: a sub-day interval
    // after a day button is raised to a day button.
    #[test]
    fn a_sub_day_interval_after_a_day_button_is_a_day_button() {
        let out = button_intervals(&ctx(), all(0.1, 1.2, 0.5, 0.6), DayRule::Graduating);
        assert_eq!(out, [secs(8640), days(1), days(2), days(3)]);
    }

    // A review card's Again step (a relearning step) floors the sub-day
    // passing buttons.
    #[test]
    fn a_review_again_step_floors_sub_day_passing_buttons() {
        let out = button_intervals(
            &ctx(),
            [step(600), model(0.001), model(0.01), model(2.0)],
            DayRule::Review {
                previous_interval: 3,
            },
        );
        assert_eq!(out[..3], [None, secs(600), secs(864)]);
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
            [model(0.000_01), skip(), skip(), skip()],
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

    // Pins spec/scheduling.md#sched.rwkv-curve-fuzz: Again on a relearning
    // card follows the review rule; the other buttons graduate with fuzz.
    #[test]
    fn relearning_again_is_clamped_without_fuzz() {
        let mut ctx = ctx();
        ctx.fuzz_factor = Some(0.99);
        ctx.minimum_lapse_interval = 3;
        let out = button_intervals(&ctx, all(1.5, 5.0, 30.0, 40.0), DayRule::Relearning);
        assert_eq!(out[0], days(3));
        let Some(ButtonInterval::Days {
            fuzz_delta_days, ..
        }) = out[2]
        else {
            panic!("good should be in days");
        };
        assert!(fuzz_delta_days > 0, "good is fuzzed");
    }

    // Pins spec/scheduling.md#sched.sub-day-intervals: a graduating or
    // relearning button is fuzzed from its unrounded interval, like a review
    // button, not from the interval rounded to whole days.
    #[test]
    fn graduating_buttons_are_fuzzed_from_the_unrounded_interval() {
        let mut ctx = ctx();
        ctx.fuzz_factor = Some(0.95);
        // 3.4 days: fuzz range 2-5 (from 3 days it would be 2-4 -> 4);
        // 6.6 days: fuzz range 5-8 (from 7 days it would be 5-9 -> 9)
        for (interval, expected) in [(3.4, 5), (6.6, 8)] {
            for rule in [DayRule::Graduating, DayRule::Relearning] {
                let out = button_intervals(&ctx, [skip(), skip(), model(interval), skip()], rule);
                let Some(ButtonInterval::Days { days, .. }) = out[2] else {
                    panic!("good should be in days");
                };
                assert_eq!(days, expected, "{interval} days, {rule:?}");
            }
        }
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
