# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Keeping FSRS-7's per-review predictions ready (spec
ui.stats-fsrs-predictions-ready).

The model-quality graphs read stored rows and never compute; this is what
fills them. The pass runs by itself, off the main thread, so the user never
has to find a button and the graphs never wait for it: once after the
collection opens if any preset lacks rows, and again whenever a preset's
FSRS-7 parameters change, because the backend drops that preset's rows in
the same transaction as the change.

Two rules keep it out of the user's way at start-up. It never begins while
the RWKV state cache is loading, because that load already holds the
collection and a 3.3 GB restore behind a backfill is a stall the user
watches; it waits and asks again. And it recomputes ONE PRESET PER CALL, so
the collection is free between presets and the main thread is never shut out
for the length of a whole backfill. It reports no progress of its own and
clears none, so it cannot disturb what the main thread is showing.

It runs at most once a day on its own, which is the upkeep the graphs need:
a stored row is a validation fold, and the per-answer rows written while
reviewing carry a different sample role that the graph's role order hides
behind the folds, so live answering does not keep the set fresh.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# the profile key holding the day number of the last finished pass
LAST_PASS_DAY_KEY = "lastFsrsPredictionPassDay"
# how long to wait before asking again while the RWKV state cache loads
RWKV_RETRY_SECS = 5.0
# how long to leave the collection free between two presets
BETWEEN_PRESETS_SECS = 0.25

_lock = threading.Lock()
_running = False
_waiting = False


def rwkv_startup_busy(mw: Any) -> bool:
    """True while the RWKV state cache is loading or building. That load
    holds the collection, so the pass must not queue in front of it."""
    return bool(getattr(mw, "_rwkv_state_cache_loading", False))


def is_running() -> bool:
    """True while a pass is writing rows, so a graph can say so instead of
    showing an algorithm as absent."""
    with _lock:
        return _running


def ensure_ready(mw: Any, *, force: bool = False) -> None:
    """Starts the pass unless one runs already, or unless one has already
    finished today and `force` is not set."""

    col = getattr(mw, "col", None)
    if col is None:
        return
    with _lock:
        global _running, _waiting
        if _running:
            return
        if not force and _finished_today(mw, col):
            return
        if rwkv_startup_busy(mw):
            # the RWKV state cache is loading: ask again rather than start
            # behind it (spec ui.stats-fsrs-predictions-ready)
            if _waiting:
                return
            _waiting = True
            timer = threading.Timer(RWKV_RETRY_SECS, _retry, args=(mw, force))
            timer.daemon = True
            timer.start()
            return
        _running = True
    threading.Thread(
        target=_run,
        args=(mw, col),
        name="fsrs-predictions",
        daemon=True,
    ).start()


def _retry(mw: Any, force: bool) -> None:
    global _waiting
    with _lock:
        _waiting = False
    ensure_ready(mw, force=force)


def _finished_today(mw: Any, col: Any) -> bool:
    try:
        today = int(col.sched.today)
    except Exception:
        return False
    profile = getattr(getattr(mw, "pm", None), "profile", None)
    if not isinstance(profile, dict):
        return False
    return profile.get(LAST_PASS_DAY_KEY) == today


def _record_finished(mw: Any, col: Any) -> None:
    profile = getattr(getattr(mw, "pm", None), "profile", None)
    if not isinstance(profile, dict):
        return
    try:
        profile[LAST_PASS_DAY_KEY] = int(col.sched.today)
    except Exception:
        pass


def _run(mw: Any, col: Any) -> None:
    global _running
    try:
        presets = list(col._backend.stale_fsrs_prediction_presets().deck_config_ids)
        written = 0
        for index, preset in enumerate(presets):
            if index:
                # the collection is free here, so anything the user does
                # goes in between two presets instead of behind all of them
                time.sleep(BETWEEN_PRESETS_SECS)
            written += col._backend.refresh_fsrs_review_predictions(
                deck_config_id=preset
            )
        logger.debug(
            "stored %s FSRS review predictions over %s presets", written, len(presets)
        )
        _record_finished(mw, col)
    except Exception:
        logger.exception("the FSRS review prediction pass failed")
    finally:
        with _lock:
            _running = False
