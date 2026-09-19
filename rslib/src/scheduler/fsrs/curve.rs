// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! FSRS-7's forgetting curve, one model per parameter set.
//!
//! Clanki once kept its own plain-`f32` copy of this curve, because the fsrs
//! crate evaluated it with Burn tensors at ~25 µs per card. fsrs-rs removed
//! Burn (upstream #447), and its own curve and interval solver are now scalar
//! code, so [`Fsrs7Curve`] only holds one `FSRS` per parameter set for the
//! callers that evaluate many cards; every value is the crate's own (spec
//! sched.fsrs-rs-latest).

use fsrs::MemoryState;
use fsrs::FSRS;

const PARAM_LEN: usize = 34;

#[derive(Debug, Clone)]
pub(crate) struct Fsrs7Curve {
    fsrs: FSRS,
}

impl Fsrs7Curve {
    /// The model `FSRS::new(params)` makes, if it is FSRS-7's: `None` for any
    /// other parameter count and for non-finite parameters (which
    /// `FSRS::new` rejects).
    pub(crate) fn new(params: &[f32]) -> Option<Self> {
        if params.len() != PARAM_LEN || !params.iter().all(|p| p.is_finite()) {
            return None;
        }
        FSRS::new(params).ok().map(|fsrs| Self { fsrs })
    }

    /// `FSRS::current_retrievability`, or `None` for a non-finite input (the
    /// caller decides what such values give).
    pub(crate) fn retrievability(&self, state: MemoryState, days_elapsed: f32) -> Option<f32> {
        let finite = days_elapsed.is_finite()
            && state.stability.is_finite()
            && state.stability_fast.is_finite()
            && state.difficulty.is_finite();
        finite.then(|| self.fsrs.current_retrievability(state, days_elapsed))
    }

    /// `FSRS::interval_at_retrievability`.
    pub(crate) fn interval_at_retrievability(
        &self,
        state: MemoryState,
        target_retrievability: f32,
    ) -> f32 {
        self.fsrs
            .interval_at_retrievability(state, target_retrievability)
    }
}

#[cfg(test)]
mod test {
    use fsrs::DEFAULT_PARAMETERS;

    use super::*;

    // Pins spec/scheduling.md#sched.fsrs-rs-latest
    #[test]
    fn the_curve_is_the_crates_own() {
        let curve = Fsrs7Curve::new(&DEFAULT_PARAMETERS).unwrap();
        let fsrs = FSRS::new(&DEFAULT_PARAMETERS).unwrap();
        let state = MemoryState {
            stability: 12.5,
            difficulty: 6.0,
            stability_fast: 3.0,
        };
        for days in [0.0, 0.5, 3.0, 40.0, 900.0] {
            assert_eq!(
                curve.retrievability(state, days).unwrap().to_bits(),
                fsrs.current_retrievability(state, days).to_bits()
            );
        }
        assert_eq!(
            curve.interval_at_retrievability(state, 0.9).to_bits(),
            fsrs.interval_at_retrievability(state, 0.9).to_bits()
        );
        assert!(curve.retrievability(state, f32::NAN).is_none());
        assert!(Fsrs7Curve::new(&[1.0; 21]).is_none());
        assert!(Fsrs7Curve::new(&[f32::NAN; 34]).is_none());
    }
}
