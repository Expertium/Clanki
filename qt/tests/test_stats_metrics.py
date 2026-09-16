# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The model-quality graphs' maths (spec ui.stats-model-metrics)."""

from __future__ import annotations

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


def test_a_series_carries_its_algorithm_and_its_review_count() -> None:
    series = metrics._series(
        metrics.FSRS_7, [0.9, 0.8, 0.7, 0.6], [True, False, True, False]
    )

    assert series.algorithm == metrics.FSRS_7
    assert series.unavailable == metrics.Unavailable.AVAILABLE
    assert series.reviews == 4
    assert series.auc == 0.75
    assert len(series.false_positive_rate) == len(series.true_positive_rate)


def test_a_series_without_a_curve_says_why() -> None:
    series = metrics._series(metrics.RWKV_CURVE, [0.9, 0.5], [True, True])

    assert series.algorithm == metrics.RWKV_CURVE
    assert series.unavailable == metrics.Unavailable.NO_REVIEWS
    assert not series.false_positive_rate


def test_a_replay_step_never_holds_a_card_twice() -> None:
    class _Identity:
        def __init__(self, card_id: int) -> None:
            self.card_id = card_id

    class _Review:
        def __init__(self, card_id: int) -> None:
            self.identity = _Identity(card_id)

    reviews = [(index, _Review(card)) for index, card in enumerate([1, 2, 1, 3, 3])]

    steps = list(metrics._steps(reviews))

    assert [[review_id for review_id, _ in step] for step in steps] == [
        [0, 1],
        [2, 3],
        [4],
    ]
