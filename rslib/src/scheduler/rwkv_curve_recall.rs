// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! RWKV-Curve's R of the Stats graph (spec ui.rwkv-curve-r-stored-curve):
//! each card's stored curve at the time since its last answered review.
//!
//! Python evaluated these curves itself, in doubles, one card at a time
//! (`_rwkv_stored_curve_retrievabilities_for_inputs` in
//! `qt/aqt/rwkv_scheduler.py`), which held the GIL for about 80 ms per
//! 3,000 cards while the Stats window opened. This is the same evaluation,
//! operation for operation, so that every value stays equal to the last bit:
//! the same doubles, the decay rates Python computed, the platform `exp` that
//! Python's `math.exp` calls, and the compensated (Neumaier) summation of
//! Python's `sum()` over floats since 3.12. It is not rslib's own
//! `predict_curve`, which works in f32.

use std::collections::HashMap;

/// For each `(card id, elapsed seconds)` query, the recall of the card's
/// stored curve at that time; NaN where Python gave no value: a card without
/// a curve, an unknown elapsed time (`None`), or a curve of an unknown shape
/// (no weights, or more than `decay_rates` has).
///
/// `ids` and `packed` are what `RwkvInference::card_curve_weights` returns:
/// for each id, a little-endian u32 weight count and that many f32 weights.
/// None when the bytes do not hold exactly one curve per id. An id packed
/// twice keeps its later curve.
pub fn stored_curve_recalls(
    ids: &[i64],
    packed: &[u8],
    queries: &[(i64, Option<i64>)],
    decay_rates: &[f64],
) -> Option<Vec<f64>> {
    let curves = unpack(ids, packed)?;
    Some(
        queries
            .iter()
            .map(|&(card_id, elapsed)| {
                match (curves.get(&card_id), elapsed) {
                    // four bytes per weight
                    (Some(weights), Some(elapsed))
                        if !weights.is_empty() && weights.len() / 4 <= decay_rates.len() =>
                    {
                        recall(weights, elapsed, decay_rates)
                    }
                    _ => f64::NAN,
                }
            })
            .collect(),
    )
}

fn unpack<'a>(ids: &[i64], packed: &'a [u8]) -> Option<HashMap<i64, &'a [u8]>> {
    let mut curves = HashMap::with_capacity(ids.len());
    let mut rest = packed;
    for &card_id in ids {
        let (count, tail) = rest.split_first_chunk::<4>()?;
        let (weights, tail) =
            tail.split_at_checked((u32::from_le_bytes(*count) as usize).checked_mul(4)?)?;
        curves.insert(card_id, weights);
        rest = tail;
    }
    rest.is_empty().then_some(curves)
}

/// Python: `1e-5 + (1.0 - 2e-5) * sum(weight * math.exp(elapsed * rate) ...)`
/// with `elapsed = max(float(elapsed_seconds), 1.0)`.
fn recall(weights: &[u8], elapsed_seconds: i64, decay_rates: &[f64]) -> f64 {
    let elapsed = elapsed_seconds as f64;
    let elapsed = if 1.0 > elapsed { 1.0 } else { elapsed };
    let mut sum = PythonFloatSum::default();
    for (weight, rate) in weights.chunks_exact(4).zip(decay_rates) {
        let weight = f32::from_le_bytes(weight.try_into().unwrap()) as f64;
        sum.add(weight * (elapsed * rate).exp());
    }
    1e-5 + (1.0 - 2e-5) * sum.total()
}

/// CPython's `sum()` of floats (`builtin_sum_impl`, 3.12 and later): the
/// start value 0 plus the first item, then Neumaier's compensated summation,
/// the compensation added at the end only when it is non-zero and finite.
#[derive(Default)]
struct PythonFloatSum {
    started: bool,
    total: f64,
    compensation: f64,
}

impl PythonFloatSum {
    fn add(&mut self, x: f64) {
        if !self.started {
            self.started = true;
            self.total = 0.0 + x;
            return;
        }
        let t = self.total + x;
        if self.total.abs() >= x.abs() {
            self.compensation += (self.total - t) + x;
        } else {
            self.compensation += (x - t) + self.total;
        }
        self.total = t;
    }

    fn total(&self) -> f64 {
        if self.compensation != 0.0 && self.compensation.is_finite() {
            self.total + self.compensation
        } else {
            self.total
        }
    }
}

#[cfg(test)]
mod test {
    use super::*;

    fn packed(curves: &[(i64, &[f32])]) -> (Vec<i64>, Vec<u8>) {
        let mut bytes = Vec::new();
        for (_, weights) in curves {
            bytes.extend_from_slice(&(weights.len() as u32).to_le_bytes());
            for weight in *weights {
                bytes.extend_from_slice(&weight.to_le_bytes());
            }
        }
        (curves.iter().map(|(id, _)| *id).collect(), bytes)
    }

    #[test]
    fn cards_without_a_value_get_nan() {
        let (ids, bytes) = packed(&[(1, &[0.5, 0.5]), (2, &[]), (3, &[0.1; 3])]);
        let rates = [-1e-3, -1e-6];
        let values = stored_curve_recalls(
            &ids,
            &bytes,
            &[(1, Some(0)), (1, None), (2, Some(5)), (3, Some(5)), (4, Some(5))],
            &rates,
        )
        .unwrap();
        // elapsed 0 counts as one second
        let expected = 1e-5 + (1.0 - 2e-5) * (0.5 * (-1e-3f64).exp() + 0.5 * (-1e-6f64).exp());
        assert_eq!(values[0], expected);
        assert!(values[1..].iter().all(|value| value.is_nan()));
    }

    #[test]
    fn malformed_bytes_give_none() {
        let (ids, mut bytes) = packed(&[(1, &[0.5])]);
        assert!(stored_curve_recalls(&ids, &bytes[..6], &[], &[-1.0]).is_none());
        bytes.push(0);
        assert!(stored_curve_recalls(&ids, &bytes, &[], &[-1.0]).is_none());
    }

    #[test]
    fn the_sum_is_compensated_like_python() {
        let mut sum = PythonFloatSum::default();
        for x in [1e16, 1.0, -1e16] {
            sum.add(x);
        }
        // Python: sum([1e16, 1.0, -1e16]) == 1.0
        assert_eq!(sum.total(), 1.0);
        let mut sum = PythonFloatSum::default();
        sum.add(-0.0);
        // Python: sum([-0.0]) == 0.0, the start value being the integer 0
        assert!(sum.total().is_sign_positive());
    }
}
