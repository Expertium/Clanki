# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The Stats page's model-quality graphs (spec ui.stats-model-metrics).

Every graph needs the same thing: for each rating of the searched cards in
the page's period, the probability of recall an algorithm predicted before
that answer, and the answer itself. Both algorithms write those predictions
per review while they run, so the backend reads them instead of computing
them again, and only rows that nothing fitted on the review produced are
used. The two algorithms are scored on the same reviews.

The page starts the job, polls it and cancels it when it closes. The reading
is quick, but it is still a background job: the window never waits for it,
and a finished result is kept for the session.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from anki.deck_config_pb2 import DeckConfigsForUpdate
from anki.stats_pb2 import ReviewMetricsProgress

logger = logging.getLogger(__name__)

Progress = ReviewMetricsProgress
Series = ReviewMetricsProgress.Series
State = ReviewMetricsProgress.State
Unavailable = ReviewMetricsProgress.Unavailable
Algorithm = DeckConfigsForUpdate.SchedulingAlgorithm

FSRS_7 = Algorithm.FSRS7
RWKV_CURVE = Algorithm.RWKV_CURVE
RWKV_INSTANT = Algorithm.RWKV_INSTANT
# menu order, and the order of the series in the progress message
ALGORITHMS = (FSRS_7, RWKV_CURVE, RWKV_INSTANT)

# points of a drawn ROC curve; the AUC uses every point
_CURVE_POINTS = 512
_MAX_CACHED_RESULTS = 4


@dataclass
class _Job:
    job_id: int
    key: tuple[object, ...]
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    state: State.ValueType = State.COMPUTING
    series: dict[Algorithm.ValueType, Series] = field(default_factory=dict)
    scored: int = 0
    fsrs_only: int = 0
    rwkv_only: int = 0
    unscored: int = 0
    newest_scored_secs: int = 0
    newer_reviews: int = 0
    shared_ratings: bool = False
    um_plus: list[Any] = field(default_factory=list)
    error: str = ""

    def progress(self) -> Progress:
        with self.lock:
            return Progress(
                state=self.state,
                job_id=self.job_id,
                series=[
                    self.series.get(
                        algorithm,
                        Series(algorithm=algorithm, unavailable=Unavailable.NOT_READY),
                    )
                    for algorithm in ALGORITHMS
                ],
                scored=self.scored,
                fsrs_only=self.fsrs_only,
                rwkv_only=self.rwkv_only,
                unscored=self.unscored,
                newest_scored_secs=self.newest_scored_secs,
                newer_reviews=self.newer_reviews,
                shared_ratings=self.shared_ratings,
                um_plus=self.um_plus,
                error=self.error,
            )

    def set_series(self, series: Series) -> None:
        with self.lock:
            self.series[series.algorithm] = series

    def set_unavailable(
        self,
        algorithms: Iterable[Algorithm.ValueType],
        reason: Unavailable.ValueType,
    ) -> None:
        for algorithm in algorithms:
            self.set_series(Series(algorithm=algorithm, unavailable=reason))


_lock = threading.Lock()
_job: _Job | None = None
_next_job_id = 1
# key -> the finished progress, so a mode switch or a second visit is free
_results: dict[tuple[object, ...], Progress] = {}


def start(mw: Any, search: str, days: int) -> Progress:
    """Starts the job for the search and period, or joins the running one
    for the same data; a finished result is returned at once."""

    global _job, _next_job_id

    col = mw.col
    card_ids = tuple(sorted(col.find_cards(search)))
    key = (card_ids, days, col.mod, col.sched.today)
    with _lock:
        if (cached := _results.get(key)) is not None:
            return cached
        current = _job
        if (
            current is not None
            and current.key == key
            and not current.cancel_event.is_set()
        ):
            with current.lock:
                running = current.state == State.COMPUTING
            if running:
                return current.progress()
        if current is not None:
            current.cancel_event.set()
        job = _Job(job_id=_next_job_id, key=key)
        _next_job_id += 1
        _job = job
    threading.Thread(
        target=_run,
        args=(mw, job, search, days),
        name="stats-metrics",
        daemon=True,
    ).start()
    return job.progress()


def progress(job_id: int) -> Progress:
    with _lock:
        job = _job
    if job is None or job.job_id != job_id:
        return Progress(state=State.CANCELLED, job_id=job_id)
    return job.progress()


def cancel(job_id: int | None = None) -> None:
    """Stops the running job (the given one, or whichever runs)."""
    with _lock:
        job = _job
    if job is not None and (job_id is None or job.job_id == job_id):
        job.cancel_event.set()


def _run(mw: Any, job: _Job, search: str, days: int) -> None:
    try:
        _compute(mw, job, search, days)
    except InterruptedError:
        with job.lock:
            job.state = State.CANCELLED
    except Exception as exc:
        logger.exception("Stats model-quality job failed")
        with job.lock:
            job.state = State.FAILED
            job.error = str(exc)
    else:
        with job.lock:
            job.state = State.DONE
        finished = job.progress()
        with _lock:
            _results[job.key] = finished
            while len(_results) > _MAX_CACHED_RESULTS:
                del _results[next(iter(_results))]


def _compute(mw: Any, job: _Job, search: str, days: int) -> None:
    data = mw.col._backend.review_predictions(search=search, days=days)
    if job.cancel_event.is_set():
        raise InterruptedError()
    with job.lock:
        job.scored = len(data.revlog_ids)
        job.fsrs_only = data.fsrs_only
        job.rwkv_only = data.rwkv_only
        job.unscored = data.unscored
        job.newest_scored_secs = data.newest_scored_secs
        job.newer_reviews = data.newer_reviews
        job.shared_ratings = data.shared_ratings
        job.um_plus = list(data.um_plus)

    job.set_series(
        _series(
            FSRS_7,
            data.fsrs_predictions,
            data.remembered,
            data.fsrs_role,
            data.fsrs_bins,
        )
    )
    job.set_series(
        _series(
            RWKV_INSTANT,
            data.rwkv_predictions,
            data.remembered,
            data.rwkv_role,
            data.rwkv_bins,
        )
    )
    # RWKV-Curve's prediction of a past review is the curve stored at the
    # card's previous answered review. Nothing stores that per review yet,
    # so the algorithm is absent, never replaced by another's values.
    job.set_unavailable([RWKV_CURVE], Unavailable.UNSUPPORTED)


def _series(
    algorithm: Algorithm.ValueType,
    predictions: Sequence[float],
    remembered: Sequence[bool],
    role: str = "",
    bins: Sequence[Any] = (),
) -> Series:
    """One algorithm's curves: its ROC curve with the area under it, and
    its calibration bins as the backend binned them."""
    if not role or not predictions:
        return Series(algorithm=algorithm, unavailable=Unavailable.NO_REVIEWS)
    points, auc = roc_curve(predictions, remembered)
    if not points:
        return Series(algorithm=algorithm, unavailable=Unavailable.NO_REVIEWS)
    return Series(
        algorithm=algorithm,
        reviews=len(predictions),
        sample_role=role,
        false_positive_rate=[point[0] for point in points],
        true_positive_rate=[point[1] for point in points],
        auc=auc,
        bins=bins,
        average_predicted=sum(predictions) / len(predictions),
        actual_recall=sum(1 for answer in remembered if answer) / len(remembered),
    )


def roc_curve(
    predictions: Sequence[float],
    remembered: Sequence[bool],
    max_points: int = _CURVE_POINTS,
) -> tuple[list[tuple[float, float]], float]:
    """The ROC curve of one algorithm and the area under it.

    A point is one threshold: the share of forgotten reviews it calls
    remembered (x) against the share of remembered reviews it calls
    remembered (y). Reviews with the same prediction are one step, so ties
    move the curve diagonally and the area follows the trapezoid rule. With
    only one kind of answer there is no curve and the area is 0.
    """
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
    return _thinned(points, max_points), area


def _thinned(
    points: list[tuple[float, float]], max_points: int
) -> list[tuple[float, float]]:
    """Every nth point of a long curve, with both ends kept."""
    if len(points) <= max_points:
        return points
    step = (len(points) - 1) / (max_points - 1)
    thinned = [points[round(index * step)] for index in range(max_points - 1)]
    thinned.append(points[-1])
    return thinned
