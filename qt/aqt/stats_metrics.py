# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The Stats page's model-quality graphs (spec ui.stats-model-metrics).

Every graph needs the same thing: for each rating of the searched cards in
the page's period, the probability of recall an algorithm predicted before
that answer, and the answer itself. FSRS-7's predictions come from the
backend. RWKV's come from a background replay of the whole review history:
the warm-up already predicts every review as it applies it, which gives
RWKV-Instant its number, and RWKV-Curve's comes from a curve prediction of
the same reviews before they are applied.

The page starts the job, polls it, and cancels it when it closes. A series
appears as soon as its own data is ready, so FSRS-7 is drawn while RWKV is
still replaying. An algorithm that cannot be computed stays absent with a
reason; it is never replaced by another algorithm's values.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
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

# the reviews of one replay step; each card appears at most once in a step,
# so a card's own earlier answer is always applied before it is predicted
_MAX_STEP = 4096
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
    rwkv_done: float = 0.0
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
                rwkv_done=self.rwkv_done,
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
# key -> the finished series, so a mode switch or a second visit is free
_results: dict[tuple[object, ...], list[Series]] = {}


def start(mw: Any, search: str, days: int) -> Progress:
    """Starts the job for the search and period, or joins the running one
    for the same data; a finished result is returned at once."""

    global _job, _next_job_id

    col = mw.col
    card_ids = tuple(sorted(col.find_cards(search)))
    key = (card_ids, days, col.mod, col.sched.today)
    with _lock:
        if (cached := _results.get(key)) is not None:
            return Progress(state=State.DONE, series=list(cached))
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
        args=(mw, job, search, days, frozenset(card_ids)),
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


def _run(mw: Any, job: _Job, search: str, days: int, card_ids: frozenset[int]) -> None:
    try:
        _compute(mw, job, search, days, card_ids)
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
            result = [job.series[algorithm] for algorithm in ALGORITHMS]
        with _lock:
            _results[job.key] = result
            while len(_results) > _MAX_CACHED_RESULTS:
                del _results[next(iter(_results))]


def _compute(
    mw: Any, job: _Job, search: str, days: int, card_ids: frozenset[int]
) -> None:
    _fsrs7_series(mw, job, search, days)
    if job.cancel_event.is_set():
        raise InterruptedError()
    _rwkv_series(mw, job, days, card_ids)


def _fsrs7_series(mw: Any, job: _Job, search: str, days: int) -> None:
    predictions = mw.col._backend.review_predictions(search=search, days=days)
    if predictions.no_params:
        job.set_unavailable([FSRS_7], Unavailable.NO_PARAMS)
        return
    job.set_series(_series(FSRS_7, predictions.predictions, predictions.remembered))


def _rwkv_series(mw: Any, job: _Job, days: int, card_ids: frozenset[int]) -> None:
    """Replays the whole review history through a separate RWKV runtime and
    keeps every prediction of a rating the graphs use."""

    import aqt.rwkv_scheduler as rwkv

    if not rwkv.rwkv_model_available():
        job.set_unavailable([RWKV_CURVE, RWKV_INSTANT], Unavailable.NO_MODEL)
        return

    reviewer = SimpleNamespace(mw=mw)
    timing = rwkv._timing_today(reviewer)
    today = getattr(timing, "days_elapsed", None)
    if not isinstance(today, int):
        raise ValueError("scheduler timing is unavailable")
    first_day = today - days + 1 if days else None

    history = rwkv._historical_rwkv_review_inputs(reviewer)
    reviews = list(zip(history.review_ids, history.reviews, strict=True))
    if not reviews:
        job.set_unavailable([RWKV_CURVE, RWKV_INSTANT], Unavailable.NO_REVIEWS)
        return

    runtime = new_runtime()
    curve_predict = getattr(
        runtime, "predict_curve_retrievability_many_from_warm_up", None
    )
    if not callable(curve_predict):
        # this build's RWKV cannot predict the stored curve of one review
        job.set_unavailable([RWKV_CURVE], Unavailable.UNSUPPORTED)
        curve_predict = None

    # a card's first review in RWKV's history has no earlier rating, so no
    # algorithm predicts it (spec ui.stats-model-metrics)
    seen: set[int] = set()
    wanted: dict[int, bool] = {}
    instant: dict[int, float] = {}
    curve: dict[int, float] = {}
    outcomes: dict[int, bool] = {}

    def record(review_id: int, retrievability: float) -> None:
        if wanted.get(review_id):
            instant[review_id] = retrievability

    done = 0
    for step in _steps(reviews):
        if job.cancel_event.is_set():
            raise InterruptedError()
        wanted.clear()
        for review_id, review in step:
            card_id = review.identity.card_id
            first = card_id not in seen
            seen.add(card_id)
            day = review.day_offset
            keep = (
                not first
                and card_id in card_ids
                and review.ease is not None
                and isinstance(day, int)
                and (first_day is None or day >= first_day)
            )
            wanted[review_id] = keep
            if keep:
                outcomes[review_id] = int(review.ease) > 1
        if curve_predict is not None:
            queries = [
                _query_input(review)
                for review_id, review in step
                if wanted.get(review_id)
            ]
            ids = [review_id for review_id, _ in step if wanted.get(review_id)]
            if queries:
                for review_id, value in zip(ids, curve_predict(queries), strict=True):
                    if value is not None:
                        curve[review_id] = float(value)
        runtime.warm_up_reviews(
            [review for _, review in step],
            review_ids=[review_id for review_id, _ in step],
            prediction_recorder=record,
            return_snapshot=False,
        )
        done += len(step)
        with job.lock:
            job.rwkv_done = done / len(reviews)

    job.set_series(_series_from_map(RWKV_INSTANT, instant, outcomes))
    if curve_predict is not None:
        job.set_series(_series_from_map(RWKV_CURVE, curve, outcomes))


def _steps(
    reviews: Sequence[tuple[int, Any]],
) -> Iterable[list[tuple[int, Any]]]:
    """Splits the history into steps in which no card appears twice, so a
    review is always predicted before the card's own answer is applied."""
    step: list[tuple[int, Any]] = []
    cards: set[int] = set()
    for review_id, review in reviews:
        card_id = review.identity.card_id
        if card_id in cards or len(step) >= _MAX_STEP:
            yield step
            step = []
            cards = set()
        step.append((review_id, review))
        cards.add(card_id)
    if step:
        yield step


def _query_input(review: Any) -> Any:
    """The review as RWKV's question of it: what the model predicted before
    the answer."""
    return replace(review, is_query=True, ease=None, duration_millis=None)


def _new_runtime() -> Any:
    import aqt.rwkv_scheduler
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    model_path = aqt.rwkv_scheduler._current_embedded_rwkv_model_path()
    if model_path is None:
        raise ValueError("no RWKV model")
    return _RustRwkvRuntime(
        model_path=model_path,
        target_retention=aqt.rwkv_scheduler._RWKV_DEFAULT_TARGET_RETENTION,
        max_interval_days=36_500,
    )


# replaced in tests
new_runtime: Callable[[], Any] = _new_runtime


def _series_from_map(
    algorithm: Algorithm.ValueType,
    predictions: dict[int, float],
    outcomes: dict[int, bool],
) -> Series:
    ids = sorted(predictions)
    return _series(
        algorithm,
        [predictions[review_id] for review_id in ids],
        [outcomes[review_id] for review_id in ids],
    )


def _series(
    algorithm: Algorithm.ValueType,
    predictions: Sequence[float],
    remembered: Sequence[bool],
) -> Series:
    """One algorithm's ROC curve and its AUC."""
    points, auc = roc_curve(predictions, remembered)
    if not points:
        return Series(algorithm=algorithm, unavailable=Unavailable.NO_REVIEWS)
    return Series(
        algorithm=algorithm,
        reviews=len(predictions),
        false_positive_rate=[point[0] for point in points],
        true_positive_rate=[point[1] for point in points],
        auc=auc,
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
