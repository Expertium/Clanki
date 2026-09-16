// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! FSRS-7's forgetting curve in plain `f32` arithmetic.
//!
//! `FSRS::current_retrievability` evaluates the curve with Burn tensors of
//! one element: about 30 tensor operations, each with its own allocation,
//! roughly 25 µs per card. That made every whole-collection R pass (the
//! Stats page's Retrievability graph, `prop:r`/`prop:s` searches, sorting
//! the Browser by R or stability) take a second or more.
//!
//! [`Fsrs7Curve`] performs the same `f32` operations in the same order as
//! the fsrs crate's tensor path (`model_v7::power_forgetting_curve`, after
//! `FSRS::new` clips the parameters), so its result is bit-identical; the
//! tests compare the two on random parameters and states. Constants the
//! tensor path receives as `f64` literals are converted to `f32` the way
//! Burn converts them (`as f32`). Anything the mirror does not cover
//! (non-FSRS-7 parameter counts, non-finite values) returns `None`, and the
//! caller uses the tensor path.

use fsrs::MemoryState;

const PARAM_LEN: usize = 34;
// fsrs::simulation::{S_MIN, S_MAX, D_MIN, D_MAX}
const S_MIN: f32 = 0.0001;
const S_MAX: f32 = 36500.0;
const D_MIN: f32 = 1.0;
const D_MAX: f32 = 10.0;
// fsrs::parameter_initialization::INIT_S_MAX
const INIT_S_MAX: f32 = 100.0;

/// A literal the tensor path passes as `f64`, as Burn converts it.
fn lit(value: f64) -> f32 {
    value as f32
}

/// `fsrs::parameter_clipper_v7::clamp_safe`
fn clamp_safe(value: f32, low: f32, high: f32) -> f32 {
    let low = if low.is_finite() { low } else { 0.0 };
    let high = if high.is_finite() { high } else { low };
    let (low, high) = if low <= high {
        (low, high)
    } else {
        (high, low)
    };
    let value = if value.is_finite() { value } else { low };
    value.clamp(low, high)
}

/// `fsrs::parameter_clipper_v7::clip_fsrs7_parameters`, which `FSRS::new`
/// applies before the parameters reach the curve.
fn clip_fsrs7_parameters(p: &mut [f32; PARAM_LEN]) {
    p[0] = clamp_safe(p[0], S_MIN, INIT_S_MAX / 2.0);
    p[1] = clamp_safe(p[1], p[0], INIT_S_MAX);
    p[2] = clamp_safe(p[2], p[1], INIT_S_MAX);
    p[3] = clamp_safe(p[3], p[2], INIT_S_MAX);

    p[4] = clamp_safe(p[4], 1.0, 10.0);
    p[5] = clamp_safe(p[5], 0.001, 4.0);
    p[6] = clamp_safe(p[6], 0.1, 4.0);

    p[7] = clamp_safe(p[7], 0.0, 4.0);
    p[8] = clamp_safe(p[8], 0.0, 1.2);
    p[9] = clamp_safe(p[9], 0.3, 3.0);
    p[10] = clamp_safe(p[10], 0.01, 1.5);
    p[11] = clamp_safe(p[11], 0.1, 1.0);
    p[12] = clamp_safe(p[12], 0.0, 3.5);
    p[13] = clamp_safe(p[13], 0.0, 1.0);
    p[14] = clamp_safe(p[14], 1.0, 7.0);

    p[15] = clamp_safe(p[15], 0.0, 4.0);
    p[16] = clamp_safe(p[16], 0.0, 2.0);
    p[17] = clamp_safe(p[17], 0.5, 6.0);
    p[18] = clamp_safe(p[18], 0.001, 1.5);
    p[19] = clamp_safe(p[19], 0.001, 1.0);
    p[20] = clamp_safe(p[20], 0.0, 5.0);
    p[21] = clamp_safe(p[21], 0.0, 1.0);
    p[22] = clamp_safe(p[22], 1.0, 7.0);

    p[23] = clamp_safe(p[23], 0.01, 0.25);
    p[24] = clamp_safe(p[24], 0.01, 0.95);
    p[25] = clamp_safe(p[25], 0.2, 0.85);
    p[26] = clamp_safe(p[26], p[25], 0.99);
    p[27] = clamp_safe(p[27], 0.01, 1.0);
    p[28] = clamp_safe(p[28], 0.1, 1.0);
    p[29] = clamp_safe(p[29], 0.0, 0.9);
    p[30] = clamp_safe(p[30], 0.1, 1.1);
    p[31] = clamp_safe(p[31], 0.0, 1.0);
    p[32] = clamp_safe(p[32], 0.0, 0.6);
    p[33] = clamp_safe(p[33], 0.0, 0.6);
}

/// FSRS-7's forgetting curve for one set of parameters.
#[derive(Debug, Clone)]
pub(crate) struct Fsrs7Curve {
    w: [f32; PARAM_LEN],
}

impl Fsrs7Curve {
    /// The curve `FSRS::new(params)` runs, if it is FSRS-7's: `None` for
    /// any other parameter count and for non-finite parameters (which
    /// `FSRS::new` rejects).
    pub(crate) fn new(params: &[f32]) -> Option<Self> {
        let mut w: [f32; PARAM_LEN] = params.try_into().ok()?;
        if !w.iter().all(|p| p.is_finite()) {
            return None;
        }
        clip_fsrs7_parameters(&mut w);
        Some(Self { w })
    }

    /// Bit-identical to `FSRS::new(params)?.current_retrievability(state,
    /// days_elapsed)`, or `None` for a non-finite input (the caller then
    /// uses the tensor path, which decides what such values give).
    pub(crate) fn retrievability(&self, state: MemoryState, days_elapsed: f32) -> Option<f32> {
        if !(days_elapsed.is_finite()
            && state.stability.is_finite()
            && state.stability_fast.is_finite()
            && state.difficulty.is_finite())
        {
            return None;
        }
        let w = &self.w;
        // FSRS::current_retrievability passes days_elapsed.max(0.0); the
        // curve clamps it again
        let t = days_elapsed.max(0.0).max(0.0);
        let s = state.stability.clamp(S_MIN, S_MAX);
        let s_fast = state.stability_fast.clamp(S_MIN, S_MAX);
        let d = state.difficulty.clamp(D_MIN, D_MAX);

        let t_over_s_fast = t / s_fast;
        let decay1_mag = (w[23] * s_fast.powf(w[33] - lit(0.3))).clamp(lit(0.01), lit(0.95));
        let decay1 = -decay1_mag;
        let factor1 = (w[25].ln() * decay1.powi(-1)).min(lit(60.0)).exp() - lit(1.0);
        let r1 = (t_over_s_fast * factor1 + lit(1.0)).powf(decay1);

        let t_over_s = t / s;
        let decay2 = -w[24].clamp(lit(0.01), lit(0.95));
        let factor2 = w[26].powf(decay2.powi(-1)) - lit(1.0);
        let d_timescale = ((d + lit(-5.0)) * (w[32] - lit(0.3))).exp();
        let r2 = (t_over_s * factor2 * d_timescale + lit(1.0)).powf(decay2);

        let weight1 = w[27] * s_fast.powf(-w[29]);
        let weight2 = w[28] * s.powf(w[30]) * ((d + lit(-5.0)) * (w[31] - lit(0.5))).exp();
        let retention = (weight1 * r1 + weight2 * r2) / (weight1 + weight2);
        Some(retention * lit(1.0 - 2e-5) + lit(1e-5))
    }

    /// Bit-identical to `FSRS::new(params)?.interval_at_retrievability(state,
    /// target_retrievability)`. The crate already solves this in plain
    /// `f32`, but reads its parameters back out of the model's tensor on
    /// every call; the functions below are its solver, unchanged.
    pub(crate) fn interval_at_retrievability(
        &self,
        state: MemoryState,
        target_retrievability: f32,
    ) -> f32 {
        solver::next_interval_for_state(&self.w, state, target_retrievability.clamp(DR_MIN, DR_MAX))
    }
}

// fsrs::model::model_v7's solver constants
const DR_MIN: f32 = 0.0001;
const DR_MAX: f32 = 0.9999;

/// The fsrs crate's FSRS-7 interval solver
/// (`model_v7::fsrs7_next_interval_scalar_for_state` and the functions it
/// calls), copied operation for operation. Its curve is the crate's own
/// plain-`f32` one, which orders some operations differently from the
/// tensor path (and fuses its last step), so it is not
/// [`Fsrs7Curve::retrievability`].
mod solver {
    use fsrs::MemoryState;

    use super::DR_MAX;
    use super::DR_MIN;
    use super::D_MAX;
    use super::D_MIN;
    use super::PARAM_LEN;
    use super::S_MAX;
    use super::S_MIN;

    const INTERVAL_NEWTON_ITERS: usize = 7;
    const BISECTION_ITERS: usize = 50;
    const MIN_T: f32 = 1.0 / 86_400.0;

    /// `fsrs7_forgetting_curve_scalar_for_state`
    fn curve(w: &[f32; PARAM_LEN], t: f32, state: MemoryState) -> f32 {
        let t = t.max(0.0);
        let s = state.stability.max(S_MIN);
        let s_fast = state.stability_fast.max(S_MIN);
        let d = state.difficulty.clamp(D_MIN, D_MAX);

        let decay1_mag = (w[23] * s_fast.powf(w[33] - 0.3)).clamp(0.01, 0.95);
        let decay1 = -decay1_mag;
        let factor1 = ((w[25].ln() / decay1).min(60.0)).exp() - 1.0;
        let r1 = (1.0 + factor1 * (t / s_fast)).powf(decay1);

        let decay2 = -w[24].clamp(0.01, 0.95);
        let factor2 = w[26].powf(1.0 / decay2) - 1.0;
        let d_timescale = ((d - 5.0) * (w[32] - 0.3)).exp();
        let r2 = (1.0 + factor2 * d_timescale * (t / s)).powf(decay2);

        let weight1 = w[27] * s_fast.powf(-w[29]);
        let weight2 = w[28] * s.powf(w[30]) * ((d - 5.0) * (w[31] - 0.5)).exp();
        let retention = (weight1 * r1 + weight2 * r2) / (weight1 + weight2);
        retention.mul_add(1.0 - 2e-5, 1e-5)
    }

    /// `fsrs7_forgetting_curve_and_derivative_scalar`
    fn curve_and_derivative(w: &[f32; PARAM_LEN], t: f32, state: MemoryState) -> (f32, f32) {
        let t = t.max(0.0);
        let s = state.stability.max(S_MIN);
        let s_fast = state.stability_fast.max(S_MIN);
        let d = state.difficulty.clamp(D_MIN, D_MAX);

        let decay1_mag = (w[23] * s_fast.powf(w[33] - 0.3)).clamp(0.01, 0.95);
        let decay1 = -decay1_mag;
        let factor1 = ((w[25].ln() / decay1).min(60.0)).exp() - 1.0;
        let b1 = 1.0 + factor1 * (t / s_fast);
        let r1 = b1.powf(decay1);
        let dr1_dt = decay1 * b1.powf(decay1 - 1.0) * factor1 / s_fast;

        let decay2 = -w[24].clamp(0.01, 0.95);
        let factor2 = w[26].powf(1.0 / decay2) - 1.0;
        let d_timescale = ((d - 5.0) * (w[32] - 0.3)).exp();
        let b2 = 1.0 + factor2 * d_timescale * (t / s);
        let r2 = b2.powf(decay2);
        let dr2_dt = decay2 * b2.powf(decay2 - 1.0) * factor2 * d_timescale / s;

        let weight1 = w[27] * s_fast.powf(-w[29]);
        let weight2 = w[28] * s.powf(w[30]) * ((d - 5.0) * (w[31] - 0.5)).exp();
        let weight_sum = (weight1 + weight2).max(1e-9);
        let retention = (weight1 * r1 + weight2 * r2) / weight_sum;
        let derivative = (weight1 * dr1_dt + weight2 * dr2_dt) / weight_sum;
        (
            retention.mul_add(1.0 - 2e-5, 1e-5),
            derivative * (1.0 - 2e-5),
        )
    }

    /// `fsrs7_next_interval_bisection_scalar_for_state`
    fn bisection(w: &[f32; PARAM_LEN], state: MemoryState, desired_retention: f32) -> f32 {
        let desired_retention = desired_retention.clamp(DR_MIN, DR_MAX);
        if desired_retention >= DR_MAX {
            return 0.0;
        }
        let mut low = 0.0;
        let mut high = state.stability.max(state.stability_fast).max(1.0);
        while curve(w, high, state) > desired_retention && high < S_MAX {
            high = (high * 2.0).min(S_MAX);
        }
        for _ in 0..BISECTION_ITERS {
            let mid = (low + high) * 0.5;
            if curve(w, mid, state) > desired_retention {
                low = mid;
            } else {
                high = mid;
            }
        }
        ((low + high) * 0.5).clamp(0.0, S_MAX)
    }

    /// `fsrs7_next_interval_scalar_for_state`
    pub(super) fn next_interval_for_state(
        w: &[f32; PARAM_LEN],
        state: MemoryState,
        desired_retention: f32,
    ) -> f32 {
        let desired_retention = desired_retention.clamp(DR_MIN, DR_MAX);
        if desired_retention >= DR_MAX {
            return 0.0;
        }

        let state = MemoryState {
            stability: state.stability.clamp(S_MIN, S_MAX),
            difficulty: state.difficulty.clamp(D_MIN, D_MAX),
            stability_fast: state.stability_fast.clamp(S_MIN, S_MAX),
        };
        let min_log_t = MIN_T.ln();
        let max_log_t = S_MAX.ln();
        let mut log_t = state.stability.max(state.stability_fast).max(MIN_T).ln();
        for _ in 0..INTERVAL_NEWTON_ITERS {
            log_t = log_t.clamp(min_log_t, max_log_t);
            let t = log_t.exp().clamp(MIN_T, S_MAX);
            let (retrievability, derivative) = curve_and_derivative(w, t, state);
            let df_du = (derivative * t).min(-1e-12);
            let step = ((retrievability - desired_retention) / df_du).clamp(-4.0, 4.0);
            log_t -= step;
            if !log_t.is_finite() {
                return bisection(w, state, desired_retention);
            }
        }
        let interval = log_t.exp().clamp(0.0, S_MAX);
        let retrievability = curve(w, interval, state);
        if retrievability.is_finite() && (retrievability - desired_retention).abs() <= 1e-3 {
            interval
        } else {
            bisection(w, state, desired_retention)
        }
    }
}

#[cfg(test)]
mod test {
    use fsrs::DEFAULT_PARAMETERS;
    use fsrs::FSRS;
    use rand::rngs::StdRng;
    use rand::Rng;
    use rand::SeedableRng;

    use super::*;

    fn log_uniform(rng: &mut StdRng, low: f32, high: f32) -> f32 {
        (rng.random_range(low.ln()..high.ln())).exp()
    }

    fn random_params(rng: &mut StdRng) -> Vec<f32> {
        let mut params: Vec<f32> = DEFAULT_PARAMETERS.to_vec();
        match rng.random_range(0..3) {
            // near the defaults
            0 => {
                for p in &mut params {
                    *p *= rng.random_range(0.5..1.5);
                }
            }
            // anywhere, including far outside the clipping bounds
            1 => {
                for p in &mut params {
                    *p = rng.random_range(-2.0..3.0) * 10f32.powf(rng.random_range(-3.0..2.0));
                }
            }
            // the defaults
            _ => {}
        }
        params
    }

    fn random_state(rng: &mut StdRng) -> MemoryState {
        MemoryState {
            stability: log_uniform(rng, 1e-6, 1e6),
            stability_fast: log_uniform(rng, 1e-6, 1e6),
            difficulty: rng.random_range(-5.0..15.0),
        }
    }

    fn random_days(rng: &mut StdRng) -> f32 {
        match rng.random_range(0..6) {
            0 => 0.0,
            1 => -rng.random_range(0.0..10.0),
            2 => rng.random_range(0.0..1.0),
            _ => log_uniform(rng, 1e-6, 1e6),
        }
    }

    fn same(a: f32, b: f32) -> bool {
        a.to_bits() == b.to_bits() || (a.is_nan() && b.is_nan())
    }

    fn assert_matches_tensor_path(cases: usize, seed: u64) {
        let mut rng = StdRng::seed_from_u64(seed);
        let mut compared = 0;
        while compared < cases {
            let params = random_params(&mut rng);
            let fsrs = FSRS::new(&params).unwrap();
            let curve = Fsrs7Curve::new(&params).unwrap();
            for _ in 0..50 {
                let state = random_state(&mut rng);
                let days = random_days(&mut rng);
                let expected = fsrs.current_retrievability(state, days.max(0.0));
                let actual = curve.retrievability(state, days.max(0.0)).unwrap();
                assert!(
                    same(actual, expected),
                    "params {params:?} state {state:?} days {days}: {actual} != {expected}"
                );
                compared += 1;
            }
        }
    }

    fn random_target(rng: &mut StdRng) -> f32 {
        match rng.random_range(0..8) {
            // outside the solver's range, and its bounds
            0 => rng.random_range(-1.0..2.0),
            1 => [0.0, 0.0001, 0.9999, 1.0][rng.random_range(0..4)],
            2 => rng.random_range(0.0..1.0),
            _ => rng.random_range(0.7..0.99),
        }
    }

    fn assert_interval_matches_the_crate(cases: usize, seed: u64) {
        let mut rng = StdRng::seed_from_u64(seed);
        let mut compared = 0;
        while compared < cases {
            let params = random_params(&mut rng);
            let fsrs = FSRS::new(&params).unwrap();
            let curve = Fsrs7Curve::new(&params).unwrap();
            for _ in 0..50 {
                let state = random_state(&mut rng);
                let target = random_target(&mut rng);
                let expected = fsrs.interval_at_retrievability(state, target);
                let actual = curve.interval_at_retrievability(state, target);
                assert!(
                    same(actual, expected),
                    "params {params:?} state {state:?} target {target}: {actual} != {expected}"
                );
                compared += 1;
            }
        }
    }

    #[test]
    fn scalar_curve_is_bit_identical_to_the_tensor_path() {
        assert_matches_tensor_path(20_000, 7);
    }

    #[test]
    fn interval_at_retrievability_is_bit_identical_to_the_crate() {
        assert_interval_matches_the_crate(20_000, 5);
    }

    /// Stabilities and difficulties at, inside and past every clamp, elapsed
    /// times from 0 to far past S_MAX, and targets at the solver's bounds.
    #[test]
    fn both_are_bit_identical_at_the_edges() {
        let stabilities = [0.0, 1e-30, 1e-6, S_MIN, 0.3, 1.0, S_MAX, 1e7, f32::MAX];
        let difficulties = [-1.0, 0.0, D_MIN, 5.0, D_MAX, 11.0, 1e9];
        let days = [0.0, 1e-9, 1.0 / 86_400.0, 1.0, 400.0, S_MAX, 1e9, f32::MAX];
        let targets = [0.0, 0.0001, 0.5, 0.9, 0.9999, 1.0];
        for params in [DEFAULT_PARAMETERS.to_vec(), vec![0.0; PARAM_LEN]] {
            let fsrs = FSRS::new(&params).unwrap();
            let curve = Fsrs7Curve::new(&params).unwrap();
            for stability in stabilities {
                for stability_fast in stabilities {
                    for difficulty in difficulties {
                        let state = MemoryState {
                            stability,
                            stability_fast,
                            difficulty,
                        };
                        for days in days {
                            let expected = fsrs.current_retrievability(state, days);
                            let actual = curve.retrievability(state, days).unwrap();
                            assert!(same(actual, expected), "{state:?} {days}");
                        }
                        for target in targets {
                            let expected = fsrs.interval_at_retrievability(state, target);
                            let actual = curve.interval_at_retrievability(state, target);
                            assert!(same(actual, expected), "{state:?} {target}");
                        }
                    }
                }
            }
        }
    }

    /// The 1M-case run for the interval (release: `cargo test --release -p
    /// anki --lib curve -- --ignored`).
    #[test]
    #[ignore]
    fn interval_at_retrievability_is_bit_identical_to_the_crate_1m() {
        assert_interval_matches_the_crate(1_000_000, 13);
    }

    /// The 1M-case run of the speed protocol's equality check (release:
    /// `cargo test --release -p anki --lib curve -- --ignored`).
    #[test]
    #[ignore]
    fn scalar_curve_is_bit_identical_to_the_tensor_path_1m() {
        assert_matches_tensor_path(1_000_000, 11);
    }

    #[test]
    fn only_fsrs7_parameters_and_finite_values_use_the_scalar_curve() {
        assert!(Fsrs7Curve::new(&[]).is_none());
        assert!(Fsrs7Curve::new(&DEFAULT_PARAMETERS[..21]).is_none());
        let mut params = DEFAULT_PARAMETERS.to_vec();
        params[3] = f32::NAN;
        assert!(Fsrs7Curve::new(&params).is_none());
        let curve = Fsrs7Curve::new(&DEFAULT_PARAMETERS).unwrap();
        let state = MemoryState {
            stability: 10.0,
            stability_fast: 8.0,
            difficulty: 5.0,
        };
        assert!(curve.retrievability(state, 3.0).is_some());
        assert!(curve.retrievability(state, f32::INFINITY).is_none());
        for bad in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY] {
            assert!(curve
                .retrievability(
                    MemoryState {
                        stability: bad,
                        ..state
                    },
                    3.0
                )
                .is_none());
            assert!(curve
                .retrievability(
                    MemoryState {
                        difficulty: bad,
                        ..state
                    },
                    3.0
                )
                .is_none());
            assert!(curve
                .retrievability(
                    MemoryState {
                        stability_fast: bad,
                        ..state
                    },
                    3.0
                )
                .is_none());
        }
    }
}
