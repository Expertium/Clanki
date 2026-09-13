// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Review fuzz for intervals supplied by an external scheduler (RWKV-Curve).
//!
//! The supplied day counts are treated exactly like the ideal intervals FSRS
//! produces: the same fuzz range, the same load balancer, the same sibling
//! dispersal, and the same floors that keep Hard < Good < Easy and stop fuzz
//! from shrinking an interval that grew.

use super::fuzz::minimum_review_fuzz_interval;
use super::StateContext;

/// Target intervals in days, one per answer button. `None` means the
/// external scheduler did not supply an interval for that button.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct ReviewIntervalOverrides {
    pub again: Option<u32>,
    pub hard: Option<u32>,
    pub good: Option<u32>,
    pub easy: Option<u32>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FuzzedInterval {
    pub scheduled_days: u32,
    /// Difference between the fuzzed interval and the unfuzzed target, as
    /// shown above the answer buttons when that preference is on.
    pub fuzz_delta_days: i32,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct FuzzedIntervalOverrides {
    pub again: Option<FuzzedInterval>,
    pub hard: Option<FuzzedInterval>,
    pub good: Option<FuzzedInterval>,
    pub easy: Option<FuzzedInterval>,
}

/// Apply the review-state fuzz rules to externally supplied intervals.
///
/// `previous_interval` is the card's current review interval in days, or 0
/// when the card is not currently in review.
///
/// This mirrors `ReviewState::failing_review_interval` and
/// `ReviewState::passing_fsrs_review_intervals`: Again is clamped but never
/// fuzzed (FSRS applies fuzz when the card leaves relearning), Hard may not
/// let fuzz shrink an interval that grew, and Good/Easy are floored one day
/// above the fuzzed button before them.
pub(crate) fn fuzz_review_interval_overrides(
    ctx: &StateContext,
    previous_interval: u32,
    overrides: ReviewIntervalOverrides,
) -> FuzzedIntervalOverrides {
    let again = overrides.again.map(|interval| {
        let (minimum, maximum) = ctx.min_and_max_review_intervals(ctx.minimum_lapse_interval);
        FuzzedInterval {
            scheduled_days: interval.clamp(minimum, maximum),
            fuzz_delta_days: 0,
        }
    });

    // If the interval is larger than last time, don't allow fuzz to go backwards
    let greater_than_last = |interval: u32| {
        if interval > previous_interval {
            previous_interval + 1
        } else {
            // User may have changed their retention factor; don't limit
            0
        }
    };
    let fuzz = |interval: u32, minimum: u32| {
        let (minimum, maximum) = ctx.min_and_max_review_intervals(minimum);
        let (scheduled_days, fuzz_delta_days) =
            ctx.with_review_fuzz_and_delta(interval as f32, minimum, maximum);
        FuzzedInterval {
            scheduled_days,
            fuzz_delta_days,
        }
    };

    let mut floor = 0;
    let hard = overrides.hard.map(|interval| {
        let fuzzed = fuzz(
            interval,
            minimum_review_fuzz_interval(
                interval as f32,
                previous_interval,
                ctx.maximum_review_interval,
            )
            .max(1),
        );
        floor = fuzzed.scheduled_days + 1;
        fuzzed
    });
    let good = overrides.good.map(|interval| {
        let fuzzed = fuzz(interval, greater_than_last(interval).max(floor));
        floor = fuzzed.scheduled_days + 1;
        fuzzed
    });
    let easy = overrides
        .easy
        .map(|interval| fuzz(interval, greater_than_last(interval).max(floor)));

    FuzzedIntervalOverrides {
        again,
        hard,
        good,
        easy,
    }
}

#[cfg(test)]
mod test {
    use super::*;

    fn all(again: u32, hard: u32, good: u32, easy: u32) -> ReviewIntervalOverrides {
        ReviewIntervalOverrides {
            again: Some(again),
            hard: Some(hard),
            good: Some(good),
            easy: Some(easy),
        }
    }

    fn days(fuzzed: FuzzedIntervalOverrides) -> [u32; 4] {
        [
            fuzzed.again.unwrap().scheduled_days,
            fuzzed.hard.unwrap().scheduled_days,
            fuzzed.good.unwrap().scheduled_days,
            fuzzed.easy.unwrap().scheduled_days,
        ]
    }

    #[test]
    fn again_is_clamped_but_never_fuzzed() {
        let mut ctx = StateContext::defaults_for_testing();
        ctx.fuzz_factor = Some(0.99);
        ctx.minimum_lapse_interval = 3;
        let fuzzed = fuzz_review_interval_overrides(&ctx, 10, all(1, 20, 30, 40));
        assert_eq!(
            fuzzed.again.unwrap(),
            FuzzedInterval {
                scheduled_days: 3,
                fuzz_delta_days: 0
            }
        );
    }

    #[test]
    fn passing_buttons_keep_fsrs_floors_without_fuzz() {
        let mut ctx = StateContext::defaults_for_testing();
        ctx.fuzz_factor = None;
        // targets equal to each other: floors must force hard < good < easy,
        // and hard may not drop below the previous interval + 1
        let fuzzed = fuzz_review_interval_overrides(&ctx, 10, all(1, 12, 12, 12));
        assert_eq!(days(fuzzed), [1, 12, 13, 14]);
        assert_eq!(fuzzed.good.unwrap().fuzz_delta_days, 0);
    }

    #[test]
    fn maximum_interval_caps_every_button() {
        let mut ctx = StateContext::defaults_for_testing();
        ctx.fuzz_factor = Some(0.99);
        ctx.maximum_review_interval = 30;
        let fuzzed = fuzz_review_interval_overrides(&ctx, 5, all(1, 50, 60, 70));
        assert_eq!(days(fuzzed), [1, 30, 30, 30]);
    }

    #[test]
    fn missing_buttons_stay_missing_and_do_not_floor_later_ones() {
        let ctx = StateContext::defaults_for_testing();
        let fuzzed = fuzz_review_interval_overrides(
            &ctx,
            0,
            ReviewIntervalOverrides {
                good: Some(7),
                ..Default::default()
            },
        );
        assert!(fuzzed.again.is_none() && fuzzed.hard.is_none() && fuzzed.easy.is_none());
        assert_eq!(fuzzed.good.unwrap().scheduled_days, 7);
    }

    #[test]
    fn fuzz_is_applied_with_the_same_seed_as_fsrs() {
        // fuzz_factor 0.99 selects the top of the fuzz range, as it does for
        // FSRS intervals (see fuzz::test::fuzz_delta_matches_selected_interval)
        let mut ctx = StateContext::defaults_for_testing();
        ctx.fuzz_factor = Some(0.99);
        let fuzzed = fuzz_review_interval_overrides(
            &ctx,
            1,
            ReviewIntervalOverrides {
                hard: Some(7),
                ..Default::default()
            },
        );
        assert_eq!(
            fuzzed.hard.unwrap(),
            FuzzedInterval {
                scheduled_days: 9,
                fuzz_delta_days: 2
            }
        );
    }
}
