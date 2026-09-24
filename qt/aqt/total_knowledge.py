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
import json
import logging
import os
import sys
import threading
import time
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from anki.stats_pb2 import TotalKnowledgeRwkvProgress

logger = logging.getLogger(__name__)

Progress = TotalKnowledgeRwkvProgress

_MAX_CACHED_RESULTS = 8

# The per-day sums of earlier runs, kept across restarts in a file beside the
# collection (spec ui.stats-total-knowledge-incremental). Bump the version
# whenever this module changes what a day's sum is.
_DAY_SUMS_CACHE_VERSION = 1
_DAY_SUMS_CACHE_SUFFIX = ".total-knowledge-cache.json"


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


@dataclass
class _Replay:
    """The history as the day loop reads it: built by the backend
    (`_backend_replay`) or, where it cannot, in Python (`_python_replay`),
    with the same values."""

    first_day: int
    has_reviews: bool
    # for each day of the loop, from first_day: how many reviews end by it
    day_review_ends: Sequence[int]
    # (runtime, start, end): warm the runtime up on the reviews [start, end)
    warm_up: Callable[[Any, int, int], None]
    # a day's changes: (card, the rating to score from or None for a reset,
    # the day of the card's next change)
    changes: Callable[[int], Sequence[tuple[int, Any, int]]]
    # (rows, card, rating): make the rating the card's row
    set_rating: Callable[[Any, int, Any], None]
    # `_history_digest` through a day
    digest: Callable[[int], str | None]


def _compute(mw: Any, job: _Job, card_ids: frozenset[int]) -> None:
    import aqt.rwkv_scheduler as rwkv
    from aqt.rwkv_srs_benchmark import MemorisedDayRows

    reviewer = SimpleNamespace(mw=mw)
    timing = rwkv._timing_today(reviewer)
    today = getattr(timing, "days_elapsed", None)
    next_day_at = getattr(timing, "next_day_at", None)
    if not isinstance(today, int) or not isinstance(next_day_at, int):
        raise ValueError("scheduler timing is unavailable")

    # a page that is already gone does not start the history at all
    if job.cancel_event.is_set():
        raise InterruptedError()
    cache_key = _day_sums_cache_key(job.curve, card_ids)
    replay = _backend_replay(
        mw, card_ids, today, next_day_at, _digest_days(mw, cache_key, today)
    ) or _python_replay(mw, job, card_ids, today, next_day_at)
    if job.cancel_event.is_set():
        raise InterruptedError()

    first_day = replay.first_day
    with job.lock:
        job.first_day = first_day - today
    if not replay.has_reviews:
        with job.lock:
            job.sum_r = [0.0] * (today - first_day + 1)
        return

    runtime = new_runtime()
    cached = _cached_day_sums(mw, cache_key, first_day, today, replay.digest)
    # the days up to `cached_through` take their sums from an earlier run: the
    # model is causal, so a day's sum depends only on the reviews up to it,
    # and the digest proved those reviews unchanged. They are still warmed up
    # the same way, so every later day starts from the same state.
    cached_through = first_day - 1 + len(cached)
    spans_sums = [0.0] * (today - first_day + 1)
    # instant head: each card whose R comes from an earlier rating. The rows
    # are packed once per rating, not once per day: only the day and the two
    # elapsed fields change from day to day, and the Rust side derives those
    # (spec ui.stats-total-knowledge)
    last_rating = MemorisedDayRows()
    review_start = 0
    for day in range(first_day, today + 1):
        if job.cancel_event.is_set():
            raise InterruptedError()
        review_end = replay.day_review_ends[day - first_day]
        replay.warm_up(runtime, review_start, review_end)
        review_start = review_end

        changes = replay.changes(day)
        # the curve head keeps no rows
        if not job.curve:
            for card_id, _rating, _until in changes:
                last_rating.remove(card_id)
        total = 0.0
        if not job.curve and last_rating and day > cached_through:
            predictions = rwkv._predict_rwkv_memorised_day_from_rows(
                runtime, last_rating, day=day
            )
            total += sum(min(max(float(r), 0.0), 1.0) for r in predictions)
        spans: list[tuple[int, int, int, int]] = []
        for card_id, rating, until in changes:
            if rating is None:
                continue
            # a rating day counts 1
            total += 1.0
            if job.curve:
                if until > day + 1:
                    spans.append((card_id, day, day + 1, until - 1))
            else:
                replay.set_rating(last_rating, card_id, rating)
        # a span that starts on a cached day still reaches later days
        if spans:
            start, sums = runtime.curve_retrievability_day_sums_from_warm_up(spans)
            for offset, value in enumerate(sums):
                spans_sums[start - first_day + offset] += value
        if job.curve:
            total += spans_sums[day - first_day]
        if day <= cached_through:
            total = cached[day - first_day]
        with job.lock:
            job.sum_r.append(total)

    with job.lock:
        day_sums = list(job.sum_r)
    _store_day_sums(mw, cache_key, first_day, today, replay.digest, day_sums)


def _digest_days(mw: Any, cache_key: str, today: int) -> list[int]:
    """The days whose digest a run reads, each once: yesterday, through which
    it keeps its sums, and the last day of the sums an earlier run kept (the
    same day when Stats opens again on the day it last ran)."""
    entry = _read_day_sums_cache(mw).get(cache_key)
    last_day = entry.get("last_day") if isinstance(entry, dict) else None
    return sorted({today - 1} | ({last_day} if isinstance(last_day, int) else set()))


def _backend_replay(
    mw: Any,
    card_ids: frozenset[int],
    today: int,
    next_day_at: int,
    digest_days: list[int],
) -> _Replay | None:
    """The replay as the backend builds it (TotalKnowledgeRwkvReplay), or
    None where it does not; Python then builds it (`_python_replay`)."""
    import aqt.rwkv_scheduler as rwkv
    from aqt.rwkv_srs_benchmark import _PACKED_PREDICTION_REQUEST_ROW

    reviewer = SimpleNamespace(mw=mw)
    build = getattr(
        getattr(getattr(mw, "col", None), "_backend", None),
        "total_knowledge_rwkv_replay",
        None,
    )
    if (
        not callable(build)
        or sys.byteorder != "little"
        or rwkv._rwkv_dynamic_preset_replay_enabled_for_collection(reviewer)
    ):
        return None
    try:
        response = build(
            card_ids=array("q", card_ids).tobytes(),
            stable_preset_ids=rwkv._rwkv_stable_preset_ids(reviewer),
            first_review_uses_creation_by_config_id=(
                rwkv._rwkv_first_review_uses_creation_by_config_id(reviewer)
            ),
            today=today,
            next_day_at=next_day_at,
            digest_days=digest_days,
        )
    except Exception:
        logger.debug(
            "the backend did not build the Total Knowledge replay", exc_info=True
        )
        return None
    if not rwkv._remember_backend_preset_ids(
        reviewer,
        memoryview(response.preset_card_ids).cast("q").tolist(),
        response.card_fsrs_preset_ids,
    ):
        return None

    rows = response.packed_rows
    width = _PACKED_PREDICTION_REQUEST_ROW.size
    first_day = response.first_day
    change_ends = memoryview(response.day_change_ends).cast("q")
    change_cards = memoryview(response.change_card_ids).cast("q")
    change_reviews = memoryview(response.change_review_indexes).cast("q")
    change_until = memoryview(response.change_until_days).cast("q")
    digests = dict(zip(digest_days, response.digests, strict=True))

    def changes(day: int) -> list[tuple[int, Any, int]]:
        index = day - first_day
        start = change_ends[index - 1] if index else 0
        end = change_ends[index]
        return [
            (card_id, None if review < 0 else review, until)
            for card_id, review, until in zip(
                change_cards[start:end].tolist(),
                change_reviews[start:end].tolist(),
                change_until[start:end].tolist(),
                strict=True,
            )
        ]

    return _Replay(
        first_day=first_day,
        has_reviews=response.review_count > 0,
        day_review_ends=memoryview(response.day_review_ends).cast("q"),
        warm_up=lambda runtime, start, end: runtime.warm_up_packed_rows_in_place(
            rows[start * width : end * width], end - start
        ),
        changes=changes,
        set_rating=lambda last_rating, card_id, review: last_rating.set_row(
            card_id, rows[review * width : (review + 1) * width]
        ),
        digest=digests.get,
    )


def _python_replay(
    mw: Any,
    job: _Job,
    card_ids: frozenset[int],
    today: int,
    next_day_at: int,
) -> _Replay:
    """The replay built in Python, from RWKV's review inputs."""
    import aqt.rwkv_scheduler as rwkv

    def stop_if_cancelled(_label: str, _value: int | None, _max: int | None) -> None:
        # the history build reports every thousand rows; a page that closed
        # stops it there instead of after its ten seconds of Python, which
        # the main window would otherwise wait out
        if job.cancel_event.is_set():
            raise InterruptedError()

    def stop_between_steps() -> None:
        # the history query runs in card-id ranges and the preparation after
        # it in bounded steps, so a page that closed stops the read after one
        # of them instead of after the whole 3-4 s query
        if job.cancel_event.is_set():
            raise InterruptedError()

    # RWKV's own history of every card: its state depends on all of them.
    # Without the history hash, which nothing here reads.
    history = rwkv._historical_rwkv_review_inputs(
        SimpleNamespace(mw=mw),
        progress=stop_if_cancelled,
        between_steps=stop_between_steps,
        hash_history=False,
    )
    reviews: list[tuple[int, Any, int]] = [
        (review_id, review, review.day_offset)
        for review_id, review in zip(history.review_ids, history.reviews, strict=True)
        if isinstance(review.day_offset, int)
    ]
    resets = mw.col.db.all("select id, cid from revlog where type = 4 and factor = 0")
    events = _card_events(reviews, resets, card_ids, today, next_day_at)
    first_day = min(
        [day for _, _, day in reviews[:1]]
        + [card_events[0].day for card_events in events.values()]
        + [today]
    )
    day_review_ends = []
    review_index = 0
    for day in range(first_day, today + 1):
        while review_index < len(reviews) and reviews[review_index][2] == day:
            review_index += 1
        day_review_ends.append(review_index)
    changes_by_day = {
        day: [(card_id, event.review, until) for card_id, event, until in changes]
        for day, changes in _changes_by_day(events, today).items()
    }
    return _Replay(
        first_day=first_day,
        has_reviews=bool(reviews),
        day_review_ends=day_review_ends,
        warm_up=lambda runtime, start, end: runtime.warm_up_reviews_in_place(
            [review for _, review, _ in reviews[start:end]]
        ),
        changes=lambda day: changes_by_day.get(day, ()),
        set_rating=lambda last_rating, card_id, review: last_rating.set(
            card_id, review
        ),
        digest=lambda through_day: _history_digest(reviews, events, through_day),
    )


def _day_sums_cache_key(curve: bool, card_ids: frozenset[int]) -> str:
    return ("curve-" if curve else "instant-") + _cards_digest(
        tuple(sorted(card_ids))
    ).hex()


def _day_sums_cache_path(mw: Any) -> str | None:
    path = getattr(getattr(mw, "col", None), "path", None)
    if not isinstance(path, str) or not path:
        return None
    return os.path.splitext(path)[0] + _DAY_SUMS_CACHE_SUFFIX


def _model_identity() -> str:
    import aqt.rwkv_scheduler

    path = aqt.rwkv_scheduler._current_embedded_rwkv_model_path()
    if path is None:
        return ""
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    return f"{os.path.basename(str(path))}:{stat.st_size}:{stat.st_mtime_ns}"


def _history_digest(
    reviews: Sequence[tuple[int, Any, int]],
    events: dict[int, list[_Event]],
    through_day: int,
) -> str:
    """What the sums up to `through_day` read: every review RWKV replays up to
    that day (its whole record, the day included, so a moved day boundary
    shows too), and the searched cards' ratings and resets."""
    import aqt.rwkv_scheduler as rwkv

    digest = hashlib.blake2b(digest_size=20)
    for review_id, review, day in reviews:
        if day > through_day:
            break
        digest.update(rwkv._encode_rwkv_delta_record(review_id, review))
        digest.update(
            repr((review.target_retentions, review.enforce_grade_order)).encode()
        )
    for card_id in sorted(events):
        for event in events[card_id]:
            if event.day <= through_day:
                digest.update(
                    f"{card_id}:{event.review_id}:{event.day}:"
                    f"{event.review is None}".encode()
                )
    return digest.hexdigest()


def _read_day_sums_cache(mw: Any) -> dict[str, Any]:
    path = _day_sums_cache_path(mw)
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != _DAY_SUMS_CACHE_VERSION:
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _cached_day_sums(
    mw: Any,
    key: str,
    first_day: int,
    today: int,
    digest: Callable[[int], str | None],
) -> list[float]:
    """The sums of the days before today that an earlier run left, or [] when
    anything they read has changed since."""
    entry = _read_day_sums_cache(mw).get(key)
    if not isinstance(entry, dict):
        return []
    sums = entry.get("sums")
    last_day = entry.get("last_day")
    if (
        entry.get("first_day") != first_day
        or not isinstance(last_day, int)
        or not isinstance(sums, list)
        or len(sums) != last_day - first_day + 1
        or last_day >= today
        or not all(isinstance(value, float) for value in sums)
        or entry.get("model") != _model_identity()
        or entry.get("digest") != digest(last_day)
    ):
        return []
    return sums


def _store_day_sums(
    mw: Any,
    key: str,
    first_day: int,
    today: int,
    digest: Callable[[int], str | None],
    sums: Sequence[float],
) -> None:
    """Keep the sums of the finished days (all but today, whose reviews are
    not all in yet) for the next run; the newest few searches only."""
    path = _day_sums_cache_path(mw)
    last_day = today - 1
    if path is None or last_day < first_day:
        return
    try:
        entries = _read_day_sums_cache(mw)
        entries.pop(key, None)
        entries[key] = {
            "first_day": first_day,
            "last_day": last_day,
            "sums": [float(value) for value in sums[: last_day - first_day + 1]],
            "model": _model_identity(),
            "digest": digest(last_day),
            "written": time.time(),
        }
        while len(entries) > _MAX_CACHED_RESULTS:
            del entries[next(iter(entries))]
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as file:
            json.dump({"version": _DAY_SUMS_CACHE_VERSION, "entries": entries}, file)
        os.replace(temporary, path)
    except OSError:
        logger.exception("Total Knowledge day sums not kept")


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
