# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The model-quality graphs' job (spec ui.stats-model-metrics).

The curves themselves are the backend's now. `_reference_roc_curve` below is
the sweep exactly as this module ran it in Python, kept as the oracle of
`test_the_rust_curve_is_the_one_python_computed`: it and
`a_large_curve_is_bit_for_bit_the_one_python_computed` in
`rslib/src/stats/roc.rs` run on the same generated ratings and assert the
same three numbers, so the two languages are pinned to each other.
"""

from __future__ import annotations

import struct
from types import SimpleNamespace

import pytest

import aqt.stats_metrics as metrics
from anki.stats_pb2 import ReviewPredictionsResponse

Series = metrics.Series

_MASK = (1 << 64) - 1
_MULTIPLIER = 0x2545F4914F6CDD1D


def _dataset(count: int) -> tuple[list[float], list[bool]]:
    """The ratings the Rust test generates, with the same generator:
    xorshift64*, 20 bits of each draw as a prediction, and an answer drawn
    against it, so the curve has a realistic shape and tens of thousands of
    ties."""
    state = _MULTIPLIER

    def advance() -> int:
        nonlocal state
        state ^= state >> 12
        state ^= (state << 25) & _MASK
        state ^= state >> 27
        return (state * _MULTIPLIER) & _MASK

    predictions = []
    remembered = []
    for _ in range(count):
        bits = (advance() >> 40) & 0xFFFFF
        predictions.append(bits / 1048576.0)
        remembered.append(((advance() >> 40) & 0xFFFFF) < bits)
    return predictions, remembered


def _reference_roc_curve(
    predictions: list[float], remembered: list[bool], max_points: int = 512
) -> tuple[list[tuple[float, float]], float]:
    """The ROC curve as this module computed it in Python, unchanged."""
    pairs = sorted(zip(predictions, remembered, strict=True), reverse=True)
    positives = sum(1 for _, value in pairs if value)
    negatives = len(pairs) - positives
    if not positives or not negatives:
        return [], 0.0

    points: list[tuple[float, float]] = [(0.0, 0.0)]
    area = 0.0
    true_positives = 0
    false_positives = 0
    index = 0
    while index < len(pairs):
        threshold = pairs[index][0]
        while index < len(pairs) and pairs[index][0] == threshold:
            if pairs[index][1]:
                true_positives += 1
            else:
                false_positives += 1
            index += 1
        x = false_positives / negatives
        y = true_positives / positives
        previous_x, previous_y = points[-1]
        area += (x - previous_x) * (y + previous_y) / 2
        points.append((x, y))
    return _reference_thinned(points, max_points), area


def _reference_thinned(
    points: list[tuple[float, float]], max_points: int
) -> list[tuple[float, float]]:
    if len(points) <= max_points:
        return points
    step = (len(points) - 1) / (max_points - 1)
    thinned = [points[round(index * step)] for index in range(max_points - 1)]
    thinned.append(points[-1])
    return thinned


def _digest(points: list[tuple[float, float]]) -> int:
    """The Rust test's fold over the raw bits of the points, so both
    languages can pin the same curve without running the other."""
    value = 0
    for point in points:
        for number in point:
            bits = struct.unpack("<Q", struct.pack("<d", number))[0]
            value = ((value * 0x100000001B3) & _MASK) ^ bits
    return value


# Pins spec/ui.md#ui.stats-model-metrics
def test_the_rust_curve_is_the_one_python_computed() -> None:
    """The three numbers `rslib/src/stats/roc.rs` asserts on the same
    300000 generated ratings. If the backend's sweep ever stops matching
    the Python one, one of the two tests fails."""
    predictions, remembered = _dataset(300_000)
    assert sum(remembered) == 150_021

    points, auc = _reference_roc_curve(predictions, remembered)

    assert struct.unpack("<Q", struct.pack("<d", auc))[0] == 0x3FEAA61E654A0E74
    assert len(points) == 512
    assert _digest(points) == 0x4F63F7982E49A3C1


def _series(
    algorithm: int,
    *,
    auc: float = 0.0,
    reviews: int = 0,
    role: str = "",
    unavailable: int = metrics.Unavailable.AVAILABLE,
    average_predicted: float = 0.0,
) -> Series:
    return Series(
        algorithm=algorithm,
        unavailable=unavailable,
        reviews=reviews,
        sample_role=role,
        auc=auc,
        average_predicted=average_predicted,
        false_positive_rate=[0.0, 1.0] if reviews else [],
        true_positive_rate=[0.0, 1.0] if reviews else [],
    )


def _absent(algorithm: int) -> Series:
    return _series(algorithm, unavailable=metrics.Unavailable.NO_REVIEWS)


def _job_of(response: ReviewPredictionsResponse) -> metrics._Job:
    class _Backend:
        def review_predictions(self, search: str, days: int) -> object:
            return response

    class _Collection:
        _backend = _Backend()

    job = metrics._Job(job_id=1, key=("test",))
    metrics._compute(SimpleNamespace(col=_Collection()), job, "deck:current", 365)
    return job


def test_the_job_passes_each_algorithms_series_through() -> None:
    job = _job_of(
        ReviewPredictionsResponse(
            series=[
                _series(metrics.FSRS_7, auc=0.75, reviews=4, role="validation_fold"),
                _series(
                    metrics.RWKV_CURVE,
                    auc=0.5,
                    reviews=4,
                    role="final_fit",
                    average_predicted=0.7,
                ),
                _series(
                    metrics.RWKV_INSTANT,
                    auc=0.25,
                    reviews=4,
                    role="final_fit",
                    average_predicted=0.75,
                ),
            ],
            scored=4,
            fsrs_only=2,
            rwkv_only=1,
            unscored=3,
            shared=4,
            newest_scored_secs=1_700_000_000,
            newer_reviews=5,
        )
    )
    progress = job.progress()

    by_algorithm = {series.algorithm: series for series in progress.series}
    assert by_algorithm[metrics.FSRS_7].auc == 0.75
    assert by_algorithm[metrics.FSRS_7].sample_role == "validation_fold"
    assert by_algorithm[metrics.RWKV_INSTANT].auc == 0.25
    # RWKV-Curve is read from its own rows, never from RWKV-Instant's
    curve = by_algorithm[metrics.RWKV_CURVE]
    assert curve.unavailable == metrics.Unavailable.AVAILABLE
    assert curve.reviews == 4
    assert curve.sample_role == "final_fit"
    # its own numbers, not RWKV-Instant's
    assert (
        curve.average_predicted != by_algorithm[metrics.RWKV_INSTANT].average_predicted
    )
    # the graph can say what was left out and how fresh the rows are
    assert progress.scored == 4
    assert progress.shared == 4
    assert (progress.fsrs_only, progress.rwkv_only, progress.unscored) == (2, 1, 3)
    assert progress.newest_scored_secs == 1_700_000_000
    assert progress.newer_reviews == 5


def test_the_job_keeps_its_own_copy_of_the_response() -> None:
    """The series and the UM+ pairs reach the page unchanged. The job keeps
    its own copies, because a sub-message taken straight out of the response
    shares the response's memory and kept all of it alive after the Stats
    window closed; this pins that the copies are exact."""
    import gc

    from anki.stats_pb2 import UmPlusBin, UmPlusPair

    pair = UmPlusPair(
        algorithm_a=metrics.FSRS_7,
        algorithm_b=metrics.RWKV_INSTANT,
        bins=[UmPlusBin(count=3)],
        um_a=0.25,
        um_b=0.125,
        slope_a=0.5,
        reviews=7,
    )
    fsrs = _series(metrics.FSRS_7, auc=1.0, reviews=2, role="validation_fold")
    job = _job_of(
        ReviewPredictionsResponse(
            series=[fsrs, _absent(metrics.RWKV_CURVE), _absent(metrics.RWKV_INSTANT)],
            scored=2,
            um_plus=[pair],
        )
    )
    gc.collect()

    assert list(job.um_plus) == [pair]
    assert list(job.progress().um_plus) == [pair]
    by_algorithm = {series.algorithm: series for series in job.progress().series}
    assert by_algorithm[metrics.FSRS_7] == fsrs


# Pins spec/ui.md#ui.stats-model-metrics
def test_an_algorithm_whose_rows_nothing_wrote_says_so() -> None:
    # RWKV-Instant has rows; RWKV-Curve has none yet
    job = _job_of(
        ReviewPredictionsResponse(
            series=[
                _absent(metrics.FSRS_7),
                _absent(metrics.RWKV_CURVE),
                _series(metrics.RWKV_INSTANT, auc=1.0, reviews=2, role="final_fit"),
            ],
            scored=2,
        )
    )
    by_algorithm = {series.algorithm: series for series in job.progress().series}

    # not "the model cannot compute it": nothing has recorded it yet
    assert (
        by_algorithm[metrics.RWKV_CURVE].unavailable == metrics.Unavailable.NOT_RECORDED
    )
    assert not by_algorithm[metrics.RWKV_CURVE].false_positive_rate


# Pins spec/ui.md#ui.stats-model-metrics
def test_a_partly_recorded_algorithm_says_how_far_back_it_reaches() -> None:
    job = _job_of(
        ReviewPredictionsResponse(
            series=[
                _absent(metrics.FSRS_7),
                _series(metrics.RWKV_CURVE, auc=1.0, reviews=2, role="final_fit"),
                _absent(metrics.RWKV_INSTANT),
            ],
            scored=2,
            rwkv_curve_oldest_secs=1_700_000_000,
            rwkv_curve_earlier_reviews=4321,
        )
    )
    curve = {series.algorithm: series for series in job.progress().series}[
        metrics.RWKV_CURVE
    ]

    # a series covering days must not look like one covering years
    assert curve.unavailable == metrics.Unavailable.AVAILABLE
    assert curve.recorded_from_secs == 1_700_000_000
    assert curve.earlier_reviews == 4321


# Pins spec/ui.md#ui.stats-model-metrics
def test_not_recorded_text_does_not_ask_the_user_to_rebuild() -> None:
    from pathlib import Path

    ftl = Path(__file__).parents[2] / "ftl" / "core" / "statistics.ftl"
    line = next(
        line
        for line in ftl.read_text(encoding="utf-8").splitlines()
        if line.startswith("statistics-model-metrics-not-recorded =")
    )
    # the user never decides to rebuild RWKV's history (Planned direction 9)
    for instruction in ("deck options", "Read Review History Again", "Use "):
        assert instruction not in line


# Pins spec/scheduling.md#sched.rwkv-recordings-automatic
def test_while_the_recording_pass_runs_the_graphs_say_it_is_computing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aqt.rwkv_scheduler

    monkeypatch.setattr(
        aqt.rwkv_scheduler, "rwkv_recordings_pass_running", lambda: True
    )
    # RWKV-Instant has rows; RWKV-Curve has none yet
    job = _job_of(
        ReviewPredictionsResponse(
            series=[
                _absent(metrics.FSRS_7),
                _absent(metrics.RWKV_CURVE),
                _series(metrics.RWKV_INSTANT, auc=1.0, reviews=2, role="final_fit"),
            ],
            scored=2,
        )
    )
    by_algorithm = {series.algorithm: series for series in job.progress().series}

    # the pass is writing the rows now, so the graph says that, not that
    # nothing ever recorded them
    assert (
        by_algorithm[metrics.RWKV_CURVE].unavailable
        == metrics.Unavailable.COMPUTING_PREDICTIONS
    )
