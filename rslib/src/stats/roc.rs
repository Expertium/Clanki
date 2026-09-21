// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The AUC-ROC curve of one algorithm (spec ui.stats-model-metrics).
//!
//! This used to run in Python, over the whole per-review list sent across
//! the backend boundary; a collection with 2.3 million scored ratings spent
//! about five seconds per algorithm there. Every step is kept bit for bit:
//! the same descending order, the same grouping of equal predictions into
//! one step, the same running trapezoid sum in the same order, and the same
//! thinning with Python's round-half-to-even.
//! `test_the_rust_curve_is_the_one_python_computed` in
//! `qt/tests/test_stats_metrics.py` runs the Python sweep on the same
//! generated ratings and asserts the same numbers this module's own
//! `a_large_curve_is_bit_for_bit_the_one_python_computed` asserts.

use rayon::prelude::*;

/// Points of a drawn curve; the area uses every point.
pub(crate) const CURVE_POINTS: usize = 512;

/// The ROC curve of one algorithm and the area under it.
///
/// A point is one threshold: the share of forgotten reviews it calls
/// remembered (x) against the share of remembered reviews it calls
/// remembered (y). Reviews with the same prediction are one step, so ties
/// move the curve diagonally and the area follows the trapezoid rule. With
/// only one kind of answer there is no curve and the area is 0.
///
/// The predictions are widened to f64 before any arithmetic, so the sums
/// are the ones Python computed on the same values.
pub(crate) fn roc_curve(
    predictions: &[f64],
    remembered: &[bool],
    max_points: usize,
) -> (Vec<(f64, f64)>, f64) {
    let positives = remembered.iter().filter(|answer| **answer).count();
    let negatives = remembered.len() - positives;
    if positives == 0 || negatives == 0 {
        return (vec![], 0.0);
    }
    let mut pairs: Vec<(f64, bool)> = predictions
        .iter()
        .copied()
        .zip(remembered.iter().copied())
        .collect();
    // Only the grouping of equal predictions reaches the result, so an
    // unstable parallel sort gives the same curve as Python's stable one.
    pairs.par_sort_unstable_by(|a, b| b.0.total_cmp(&a.0));

    let mut points: Vec<(f64, f64)> = vec![(0.0, 0.0)];
    let mut area = 0.0f64;
    let mut true_positives = 0usize;
    let mut false_positives = 0usize;
    let mut index = 0usize;
    while index < pairs.len() {
        let threshold = pairs[index].0;
        // the first row of the group is taken before the test, so a value
        // that is equal to nothing (a NaN a caller let through) is its own
        // group instead of looping for ever
        loop {
            if pairs[index].1 {
                true_positives += 1;
            } else {
                false_positives += 1;
            }
            index += 1;
            if index >= pairs.len() || pairs[index].0 != threshold {
                break;
            }
        }
        let x = false_positives as f64 / negatives as f64;
        let y = true_positives as f64 / positives as f64;
        let (previous_x, previous_y) = points[points.len() - 1];
        area += (x - previous_x) * (y + previous_y) / 2.0;
        points.push((x, y));
    }
    (thinned(points, max_points), area)
}

/// Every nth point of a long curve, with both ends kept.
///
/// `round_ties_even` is Python's `round`, which breaks a tie towards the
/// even index; `f64::round` would break it away from zero and pick a
/// different point of the curve.
fn thinned(points: Vec<(f64, f64)>, max_points: usize) -> Vec<(f64, f64)> {
    if points.len() <= max_points || max_points < 2 {
        return points;
    }
    let step = (points.len() - 1) as f64 / (max_points - 1) as f64;
    let mut thinned: Vec<(f64, f64)> = (0..max_points - 1)
        .map(|index| points[(index as f64 * step).round_ties_even() as usize])
        .collect();
    thinned.push(points[points.len() - 1]);
    thinned
}

#[cfg(test)]
mod tests {
    use super::*;

    fn curve(predictions: &[f64], remembered: &[bool]) -> (Vec<(f64, f64)>, f64) {
        roc_curve(predictions, remembered, CURVE_POINTS)
    }

    #[test]
    fn auc_ranks_remembered_reviews_above_forgotten_ones() {
        let (points, auc) = curve(&[0.9, 0.8, 0.7, 0.6], &[true, false, true, false]);
        assert_eq!(auc, 0.75);
        assert_eq!(
            points,
            vec![(0.0, 0.0), (0.0, 0.5), (0.5, 0.5), (0.5, 1.0), (1.0, 1.0)]
        );
    }

    #[test]
    fn a_perfect_order_has_auc_one_and_a_reversed_one_has_zero() {
        let (_, perfect) = curve(&[0.9, 0.8, 0.2, 0.1], &[true, true, false, false]);
        let (_, reversed) = curve(&[0.9, 0.8, 0.2, 0.1], &[false, false, true, true]);
        assert_eq!(perfect, 1.0);
        assert_eq!(reversed, 0.0);
    }

    #[test]
    fn reviews_with_the_same_prediction_are_one_step() {
        let (points, auc) = curve(&[0.5, 0.5], &[true, false]);
        assert_eq!(auc, 0.5);
        assert_eq!(points, vec![(0.0, 0.0), (1.0, 1.0)]);
    }

    #[test]
    fn one_kind_of_answer_has_no_curve() {
        assert_eq!(curve(&[0.9, 0.5], &[true, true]), (vec![], 0.0));
        assert_eq!(curve(&[0.9, 0.5], &[false, false]), (vec![], 0.0));
        assert_eq!(curve(&[], &[]), (vec![], 0.0));
    }

    #[test]
    fn a_long_curve_is_thinned_but_keeps_its_ends() {
        let predictions: Vec<f64> = (0..5000).map(|index| index as f64 / 5000.0).collect();
        let remembered: Vec<bool> = (0..5000).map(|index| index % 2 == 0).collect();
        let (points, auc) = roc_curve(&predictions, &remembered, 64);
        assert_eq!(points.len(), 64);
        assert_eq!(points[0], (0.0, 0.0));
        assert_eq!(points[63], (1.0, 1.0));
        assert!((0.0..=1.0).contains(&auc));
    }

    /// The dataset the Python side of the bit-exact pin builds, with the
    /// same generator: xorshift64*, 20 bits of each draw as a prediction,
    /// and an answer drawn against it, so the curve has a realistic shape
    /// and tens of thousands of ties.
    fn dataset(count: usize) -> (Vec<f64>, Vec<bool>) {
        const MULTIPLIER: u64 = 0x2545_F491_4F6C_DD1D;
        fn advance(state: &mut u64) -> u64 {
            *state ^= *state >> 12;
            *state ^= *state << 25;
            *state ^= *state >> 27;
            state.wrapping_mul(MULTIPLIER)
        }
        let mut state = MULTIPLIER;
        let mut predictions = Vec::with_capacity(count);
        let mut remembered = Vec::with_capacity(count);
        for _ in 0..count {
            let bits = (advance(&mut state) >> 40) & 0xF_FFFF;
            predictions.push(bits as f64 / 1_048_576.0);
            remembered.push((advance(&mut state) >> 40) & 0xF_FFFF < bits);
        }
        (predictions, remembered)
    }

    /// An order-sensitive fold over the raw bits of the points, so the
    /// Python test can pin the same curve without running Rust.
    fn digest(points: &[(f64, f64)]) -> u64 {
        let mut value = 0u64;
        for point in points {
            for number in [point.0, point.1] {
                value = value.wrapping_mul(0x100_0000_01B3) ^ number.to_bits();
            }
        }
        value
    }

    /// The curve of 300000 ratings, to the last bit of every number.
    ///
    /// `test_the_rust_curve_is_the_one_python_computed` in
    /// `qt/tests/test_stats_metrics.py` runs the reference implementation
    /// on the same dataset and asserts the same three numbers, so the two
    /// tests together pin that moving the sweep into Rust changed nothing.
    #[test]
    fn a_large_curve_is_bit_for_bit_the_one_python_computed() {
        let (predictions, remembered) = dataset(300_000);
        assert_eq!(remembered.iter().filter(|answer| **answer).count(), 150_021);
        let (points, auc) = roc_curve(&predictions, &remembered, CURVE_POINTS);
        assert_eq!(auc.to_bits(), 0x3FEA_A61E_654A_0E74);
        assert_eq!(points.len(), 512);
        assert_eq!(digest(&points), 0x4F63_F798_2E49_A3C1);
    }

    /// Python's `round` breaks a tie towards the even index, and
    /// `f64::round` breaks it away from zero; the two pick different
    /// points of the curve. Seven points thinned to five have step 1.5,
    /// so index 1 lands on 1.5 and index 3 on 4.5: Python gives 2 and 4,
    /// `f64::round` would give 2 and 5.
    #[test]
    fn thinning_rounds_a_tie_the_way_python_does() {
        let line = |count: usize| -> Vec<(f64, f64)> {
            (0..count).map(|i| (i as f64, i as f64)).collect()
        };
        // a curve no longer than the limit is untouched
        assert_eq!(thinned(line(3), 3), line(3));
        // step 4/3: indices 0, 1.333 -> 1, 2.666 -> 3, then the last point
        assert_eq!(
            thinned(line(5), 4),
            vec![(0.0, 0.0), (1.0, 1.0), (3.0, 3.0), (4.0, 4.0)]
        );
        // step 1.5: the two ties round to the even index
        assert_eq!(
            thinned(line(7), 5),
            vec![(0.0, 0.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0), (6.0, 6.0)]
        );
    }
}
