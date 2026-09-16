# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The model-quality graphs' maths (spec ui.stats-model-metrics)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import aqt.stats_metrics as metrics


def test_auc_ranks_remembered_reviews_above_forgotten_ones() -> None:
    points, auc = metrics.roc_curve([0.9, 0.8, 0.7, 0.6], [True, False, True, False])

    assert auc == 0.75
    assert points == [(0.0, 0.0), (0.0, 0.5), (0.5, 0.5), (0.5, 1.0), (1.0, 1.0)]


def test_a_perfect_order_has_auc_one_and_a_reversed_one_has_zero() -> None:
    _, perfect = metrics.roc_curve([0.9, 0.8, 0.2, 0.1], [True, True, False, False])
    _, reversed_order = metrics.roc_curve(
        [0.9, 0.8, 0.2, 0.1], [False, False, True, True]
    )

    assert perfect == 1.0
    assert reversed_order == 0.0


def test_reviews_with_the_same_prediction_are_one_step() -> None:
    points, auc = metrics.roc_curve([0.5, 0.5], [True, False])

    assert auc == 0.5
    assert points == [(0.0, 0.0), (1.0, 1.0)]


def test_one_kind_of_answer_has_no_curve() -> None:
    assert metrics.roc_curve([0.9, 0.5], [True, True]) == ([], 0.0)
    assert metrics.roc_curve([0.9, 0.5], [False, False]) == ([], 0.0)
    assert metrics.roc_curve([], []) == ([], 0.0)


def test_a_long_curve_is_thinned_but_keeps_its_ends() -> None:
    predictions = [index / 5000 for index in range(5000)]
    remembered = [index % 2 == 0 for index in range(5000)]

    points, auc = metrics.roc_curve(predictions, remembered, max_points=64)

    assert len(points) == 64
    assert points[0] == (0.0, 0.0)
    assert points[-1] == (1.0, 1.0)
    assert 0.0 <= auc <= 1.0


def test_a_series_carries_its_algorithm_its_count_and_its_role() -> None:
    series = metrics._series(
        metrics.FSRS_7,
        [0.9, 0.8, 0.7, 0.6],
        [True, False, True, False],
        "validation_fold",
    )

    assert series.algorithm == metrics.FSRS_7
    assert series.unavailable == metrics.Unavailable.AVAILABLE
    assert series.reviews == 4
    assert series.auc == 0.75
    assert series.sample_role == "validation_fold"
    assert len(series.false_positive_rate) == len(series.true_positive_rate)


def test_a_series_without_a_curve_says_why() -> None:
    series = metrics._series(
        metrics.RWKV_INSTANT, [0.9, 0.5], [True, True], "final_fit"
    )

    assert series.algorithm == metrics.RWKV_INSTANT
    assert series.unavailable == metrics.Unavailable.NO_REVIEWS
    assert not series.false_positive_rate


def test_a_series_skips_the_ratings_it_has_no_row_for() -> None:
    # the backend sends one entry per rating ANY algorithm scored, and marks
    # the ones this algorithm cannot score with NaN (spec
    # ui.stats-model-metrics)
    series = metrics._series(
        metrics.RWKV_INSTANT,
        [0.9, float("nan"), 0.6, float("nan")],
        [True, True, False, False],
        "final_fit",
    )

    assert series.unavailable == metrics.Unavailable.AVAILABLE
    # only the two ratings it predicted, never the other algorithm's values
    assert series.reviews == 2
    assert series.auc == 1.0
    assert series.average_predicted == pytest.approx(0.75)
    assert series.actual_recall == pytest.approx(0.5)


def test_one_row_of_another_algorithm_does_not_shrink_this_one() -> None:
    # Andrew's collection: RWKV had rows for nearly every rating and FSRS-7
    # for a handful; the handful must not take RWKV's ratings away
    predictions = [0.9 - index * 0.01 for index in range(10)]
    remembered = [index % 2 == 0 for index in range(10)]
    series = metrics._series(metrics.RWKV_INSTANT, predictions, remembered, "final_fit")

    assert series.reviews == 10


def test_an_algorithm_with_no_usable_rows_is_absent() -> None:
    # no role means the backend found no row the algorithm had not seen
    series = metrics._series(metrics.FSRS_7, [0.9, 0.5], [True, False], "")

    assert series.unavailable == metrics.Unavailable.NO_REVIEWS
    assert series.reviews == 0


def test_the_job_reads_the_backend_and_keeps_rwkv_curve_absent() -> None:
    from anki.stats_pb2 import ReviewPredictionsResponse

    class _Backend:
        def review_predictions(self, search: str, days: int) -> object:
            return ReviewPredictionsResponse(
                revlog_ids=[1, 2, 3, 4],
                card_ids=[1, 1, 2, 2],
                remembered=[True, False, True, False],
                fsrs_predictions=[0.9, 0.8, 0.7, 0.6],
                rwkv_predictions=[0.6, 0.7, 0.8, 0.9],
                fsrs_role="validation_fold",
                rwkv_role="final_fit",
                fsrs_only=2,
                rwkv_only=1,
                unscored=3,
                shared=4,
                newest_scored_secs=1_700_000_000,
                newer_reviews=5,
            )

    class _Collection:
        _backend = _Backend()

    job = metrics._Job(job_id=1, key=("test",))
    metrics._compute(SimpleNamespace(col=_Collection()), job, "deck:current", 365)
    progress = job.progress()

    by_algorithm = {series.algorithm: series for series in progress.series}
    assert by_algorithm[metrics.FSRS_7].auc == 0.75
    assert by_algorithm[metrics.FSRS_7].sample_role == "validation_fold"
    assert by_algorithm[metrics.RWKV_INSTANT].auc == 0.25
    # RWKV-Curve has no stored per-review value: absent, never another's
    assert (
        by_algorithm[metrics.RWKV_CURVE].unavailable == metrics.Unavailable.UNSUPPORTED
    )
    assert not by_algorithm[metrics.RWKV_CURVE].false_positive_rate
    # the graph can say what was left out and how fresh the rows are
    assert progress.scored == 4
    assert progress.shared == 4
    assert (progress.fsrs_only, progress.rwkv_only, progress.unscored) == (2, 1, 3)
    assert progress.newest_scored_secs == 1_700_000_000
    assert progress.newer_reviews == 5
