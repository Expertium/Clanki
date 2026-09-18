# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The Stats page's Total Knowledge graph under RWKV (spec
ui.stats-total-knowledge).

The upper bound and FSRS-7's sum of R come from the backend
(``TotalKnowledge``). Under RWKV a background job replays the collection's
review history through a separate RWKV runtime, day by day, and sums the
search's cards' R per day: the curve head's recall under RWKV-Curve, the
instant head's under RWKV-Instant. The page starts the job, polls its
progress (the days done so far) and cancels it when it closes. Finished
results are kept for the session.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from anki.stats_pb2 import TotalKnowledgeRwkvProgress

logger = logging.getLogger(__name__)

Progress = TotalKnowledgeRwkvProgress

_MAX_CACHED_RESULTS = 8


@dataclass
class _Event:
    """A rating (with its RWKV review input) or a reset of one card."""

    review_id: int
    day: int
    review: Any | None


@dataclass
class _Job:
    job_id: int
    key: tuple[object, ...]
    curve: bool
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    state: Progress.State.ValueType = Progress.COMPUTING
    # relative to today, like the backend's days
    first_day: int = 0
    sum_r: list[float] = field(default_factory=list)
    error: str = ""

    def progress(self) -> Progress:
        with self.lock:
            return Progress(
                state=self.state,
                job_id=self.job_id,
                first_day=self.first_day,
                sum_r=self.sum_r,
                error=self.error,
            )


_lock = threading.Lock()
_job: _Job | None = None
_next_job_id = 1
# key -> (first day, sums) of finished jobs
_results: dict[tuple[object, ...], tuple[int, list[float]]] = {}


def _cards_digest(card_ids: Sequence[int]) -> bytes:
    """A 16-byte digest of the search's card ids.

    The key of a kept result holds this instead of the ids themselves: on a
    159,000-card collection one such tuple of ids costs 8.5 MB, so the eight
    kept results held 68 MB. Two searches share a key when they match the
    same cards, as before."""
    return hashlib.blake2b(array("q", card_ids).tobytes(), digest_size=16).digest()


def start_rwkv(mw: Any, search: str, *, curve: bool) -> Progress:
    """Starts the job for the search's cards, or joins the running one for
    the same cards and collection state; a finished result is returned at
    once. With no usable RWKV model, says so instead (spec
    sched.rwkv-no-model-error)."""

    global _job, _next_job_id

    import aqt.rwkv_scheduler

    if not aqt.rwkv_scheduler.rwkv_model_available():
        return Progress(state=Progress.NO_MODEL)
    col = mw.col
    card_ids = tuple(sorted(col.find_cards(search)))
    key = (curve, _cards_digest(card_ids), col.mod, col.sched.today)
    with _lock:
        if (cached := _results.get(key)) is not None:
            return Progress(state=Progress.DONE, first_day=cached[0], sum_r=cached[1])
        current = _job
        if (
            current is not None
            and current.key == key
            and not current.cancel_event.is_set()
        ):
            with current.lock:
                running = current.state == Progress.COMPUTING
            if running:
                return current.progress()
        if current is not None:
            current.cancel_event.set()
        job = _Job(job_id=_next_job_id, key=key, curve=curve)
        _next_job_id += 1
        _job = job
    threading.Thread(
        target=_run,
        args=(mw, job, frozenset(card_ids)),
        name="total-knowledge-rwkv",
        daemon=True,
    ).start()
    return job.progress()


def rwkv_progress(job_id: int) -> Progress:
    with _lock:
        job = _job
    if job is None or job.job_id != job_id:
        return Progress(state=Progress.CANCELLED, job_id=job_id)
    return job.progress()


def cancel_rwkv(job_id: int | None = None) -> None:
    """Stops the running job (the given one, or whichever runs); its
    partial sums are dropped."""
    with _lock:
        job = _job
    if job is not None and (job_id is None or job.job_id == job_id):
        job.cancel_event.set()


def _run(mw: Any, job: _Job, card_ids: frozenset[int]) -> None:
    try:
        _compute(mw, job, card_ids)
    except InterruptedError:
        with job.lock:
            job.state = Progress.CANCELLED
    except _NoModel:
        with job.lock:
            job.state = Progress.NO_MODEL
    except Exception as exc:
        logger.exception("Total Knowledge RWKV job failed")
        with job.lock:
            job.state = Progress.FAILED
            job.error = str(exc)
    else:
        with job.lock:
            job.state = Progress.DONE
            result = (job.first_day, list(job.sum_r))
        with _lock:
            _results[job.key] = result
            while len(_results) > _MAX_CACHED_RESULTS:
                del _results[next(iter(_results))]


class _NoModel(Exception):
    pass


def _new_runtime() -> Any:
    import aqt.rwkv_scheduler
    from aqt.rwkv_srs_benchmark import _RustRwkvRuntime

    model_path = aqt.rwkv_scheduler._current_embedded_rwkv_model_path()
    if model_path is None:
        raise _NoModel()
    return _RustRwkvRuntime(
        model_path=model_path,
        target_retention=aqt.rwkv_scheduler._RWKV_DEFAULT_TARGET_RETENTION,
        max_interval_days=36_500,
    )


# replaced in tests
new_runtime: Callable[[], Any] = _new_runtime


def _compute(mw: Any, job: _Job, card_ids: frozenset[int]) -> None:
    import aqt.rwkv_scheduler as rwkv
    from aqt.rwkv_srs_benchmark import MemorisedDayRows

    reviewer = SimpleNamespace(mw=mw)
    timing = rwkv._timing_today(reviewer)
    today = getattr(timing, "days_elapsed", None)
    next_day_at = getattr(timing, "next_day_at", None)
    if not isinstance(today, int) or not isinstance(next_day_at, int):
        raise ValueError("scheduler timing is unavailable")

    # RWKV's own history of every card: its state depends on all of them
    history = rwkv._historical_rwkv_review_inputs(reviewer)
    reviews: list[tuple[int, Any, int]] = [
        (review_id, review, review.day_offset)
        for review_id, review in zip(history.review_ids, history.reviews, strict=True)
        if isinstance(review.day_offset, int)
    ]
    resets = mw.col.db.all("select id, cid from revlog where type = 4 and factor = 0")
    events = _card_events(reviews, resets, card_ids, today, next_day_at)
    if job.cancel_event.is_set():
        raise InterruptedError()

    first_day = min(
        [day for _, _, day in reviews[:1]]
        + [card_events[0].day for card_events in events.values()]
        + [today]
    )
    with job.lock:
        job.first_day = first_day - today
    if not reviews:
        with job.lock:
            job.sum_r = [0.0] * (today - first_day + 1)
        return

    runtime = new_runtime()
    changes_by_day = _changes_by_day(events, today)
    spans_sums = [0.0] * (today - first_day + 1)
    # instant head: each card whose R comes from an earlier rating. The rows
    # are packed once per rating, not once per day: only the day and the two
    # elapsed fields change from day to day, and the Rust side derives those
    # (spec ui.stats-total-knowledge)
    last_rating = MemorisedDayRows()
    review_index = 0
    for day in range(first_day, today + 1):
        if job.cancel_event.is_set():
            raise InterruptedError()
        day_start = review_index
        while review_index < len(reviews) and reviews[review_index][2] == day:
            review_index += 1
        runtime.warm_up_reviews_in_place(
            [review for _, review, _ in reviews[day_start:review_index]]
        )

        changes = changes_by_day.get(day, ())
        for card_id, _event, _until in changes:
            last_rating.remove(card_id)
        total = 0.0
        if not job.curve and last_rating:
            predictions = rwkv._predict_rwkv_memorised_day_from_rows(
                runtime, last_rating, day=day
            )
            total += sum(min(max(float(r), 0.0), 1.0) for r in predictions)
        spans: list[tuple[int, int, int, int]] = []
        for card_id, event, until in changes:
            if event.review is None:
                continue
            # a rating day counts 1
            total += 1.0
            if job.curve:
                if until > day + 1:
                    spans.append((card_id, day, day + 1, until - 1))
            else:
                last_rating.set(card_id, event.review)
        if spans:
            start, sums = runtime.curve_retrievability_day_sums_from_warm_up(spans)
            for offset, value in enumerate(sums):
                spans_sums[start - first_day + offset] += value
        if job.curve:
            total += spans_sums[day - first_day]
        with job.lock:
            job.sum_r.append(total)


def _card_events(
    reviews: Sequence[tuple[int, Any, int]],
    resets: Sequence[Sequence[int]],
    card_ids: frozenset[int],
    today: int,
    next_day_at: int,
) -> dict[int, list[_Event]]:
    """Each selected card's ratings in RWKV's history and its resets, in
    order. RWKV's history of a card starts at its latest learning start, so
    ratings before a reset and relearn are not in it."""
    import aqt.rwkv_scheduler as rwkv

    events: dict[int, list[_Event]] = {}
    for review_id, review, day in reviews:
        card_id = review.identity.card_id
        if card_id in card_ids:
            events.setdefault(card_id, []).append(
                _Event(review_id=review_id, day=day, review=review)
            )
    for review_id, card_id in resets:
        # a reset matters only after a rating
        if card_id in events:
            day = rwkv._historical_review_day_offset(
                review_id, days_elapsed=today, next_day_at=next_day_at
            )
            events[card_id].append(_Event(review_id=review_id, day=day, review=None))
    for card_events in events.values():
        card_events.sort(key=lambda event: event.review_id)
        while card_events and card_events[0].review is None:
            card_events.pop(0)
    return {
        card_id: card_events for card_id, card_events in events.items() if card_events
    }


def _changes_by_day(
    events: dict[int, list[_Event]], today: int
) -> dict[int, list[tuple[int, _Event, int]]]:
    """For each day, the cards whose value that day comes from an event of
    that day: (card, the day's last event, the day of the card's next event,
    or the day after today)."""
    changes: dict[int, list[tuple[int, _Event, int]]] = {}
    for card_id, card_events in events.items():
        for event, later in zip(card_events, [*card_events[1:], None]):
            if later is None:
                changes.setdefault(event.day, []).append((card_id, event, today + 1))
            elif later.day != event.day:
                changes.setdefault(event.day, []).append((card_id, event, later.day))
    return changes
