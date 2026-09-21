# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The Stats page's model-quality graphs (spec ui.stats-model-metrics).

Every graph needs the same thing: for each rating of the searched cards in
the page's period, the probability of recall an algorithm predicted before
that answer, and the answer itself. Both algorithms write those predictions
per review while they run, so the backend reads them instead of computing
them again, and only rows that nothing fitted on the review produced are
used. Each algorithm is scored on every rating it has a row for, and the
graph names the ratings the algorithms share.

The backend also draws each algorithm's curves from those rows and sends
the finished series. The rows themselves never cross the boundary: Andrew's
collection has about 2.3 million of them per algorithm, and sweeping them in
Python cost about five seconds per algorithm on top of the transfer.

This module turns those series into the page's message: it adds the reasons
an absent series is absent, which depend on what is running in this process.

The page starts the job, polls it and cancels it when it closes. The reading
is quick, but it is still a background job: the window never waits for it,
and a finished result is kept for the session.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from array import array
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import aqt.fsrs_predictions
import aqt.rwkv_scheduler
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
    shared: int = 0
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
                shared=self.shared,
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
    card_ids = sorted(col.find_cards(search))
    # a digest, not the ids: four kept keys of a large search held megabytes
    cards = hashlib.blake2b(array("q", card_ids).tobytes(), digest_size=16).digest()
    key = (cards, days, col.mod, col.sched.today)
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
        job.scored = data.scored
        job.shared = data.shared
        job.fsrs_only = data.fsrs_only
        job.rwkv_only = data.rwkv_only
        job.unscored = data.unscored
        job.newest_scored_secs = data.newest_scored_secs
        job.newer_reviews = data.newer_reviews
        job.shared_ratings = data.shared_ratings
        job.um_plus = [_copied(item) for item in data.um_plus]

    # copies, so nothing below keeps the response alive or writes into it
    by_algorithm = {series.algorithm: _copied(series) for series in data.series}
    fsrs = by_algorithm[FSRS_7]
    # A pass is writing FSRS-7's rows: either it has never run, or the
    # parameters changed and the backend dropped the rows they produced. Say
    # that rather than "no review it predicts", which would be wrong, and
    # never draw the values the old parameters made (spec
    # ui.stats-fsrs-predictions-ready).
    if fsrs.unavailable == Unavailable.NO_REVIEWS and aqt.fsrs_predictions.is_running():
        fsrs = Series(algorithm=FSRS_7, unavailable=Unavailable.COMPUTING_PREDICTIONS)
    # the RWKV recording pass writes both algorithms' rows, so while it runs
    # an empty series is one being computed, not one that nothing recorded
    # (spec sched.rwkv-recordings-automatic)
    rwkv_pass_running = aqt.rwkv_scheduler.rwkv_recordings_pass_running()
    job.set_series(fsrs)
    instant = by_algorithm[RWKV_INSTANT]
    if instant.unavailable == Unavailable.NO_REVIEWS and rwkv_pass_running:
        instant = Series(
            algorithm=RWKV_INSTANT, unavailable=Unavailable.COMPUTING_PREDICTIONS
        )
    job.set_series(instant)
    # RWKV-Curve's prediction of a past review is the curve the replay had
    # stored at the card's previous answered review, evaluated at that
    # review's elapsed time. The warm-up records it per review, so the
    # series is read like any other; when no row exists yet the reason says
    # that Clanki has not recorded them, never that the model cannot
    # compute them (spec ui.stats-model-metrics).
    curve = by_algorithm[RWKV_CURVE]
    if curve.unavailable == Unavailable.NO_REVIEWS:
        curve = Series(
            algorithm=RWKV_CURVE,
            unavailable=(
                Unavailable.COMPUTING_PREDICTIONS
                if rwkv_pass_running
                else Unavailable.NOT_RECORDED
            ),
        )
    else:
        # how far back the recording reaches, so a series covering days
        # cannot look like one covering years
        curve.recorded_from_secs = data.rwkv_curve_oldest_secs
        curve.earlier_reviews = data.rwkv_curve_earlier_reviews
    job.set_series(curve)


def _copied(message: Any) -> Any:
    """A copy that owns its memory. A sub-message taken straight out of a
    response shares the response's memory, so keeping one keeps the whole
    response alive after the Stats window has closed, and writing to one
    writes into the response."""
    copy = type(message)()
    copy.CopyFrom(message)
    return copy
