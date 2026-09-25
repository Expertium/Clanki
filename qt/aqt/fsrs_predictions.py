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

Three rules keep it out of the user's way at start-up. It never begins while
the RWKV state cache is loading, because that load already holds the
collection and a 3.3 GB restore behind a backfill is a stall the user
watches; it waits and asks again. It works ONE PRESET PER CALL, so the
collection is free between presets and the main thread is never shut out
for the length of a whole backfill. And it does not begin until the user has
left Clanki alone for USER_IDLE_SECS. One preset holds the collection for up
to five seconds on a large collection, and at start-up, when the pass used
to run, that is exactly when the user clicks: deck options took 3.5 s to
open 8 s after the profile opened, and the main window froze for 2-4 s. It
reports no progress of its own and clears none, so it cannot disturb what
the main thread is showing.

Once it has begun it never waits for the user again. Between two presets it
rests, for a multiple of the preset just done while the user works and for a
moment while the user is away, and the rest is bounded either way
(`rest_seconds`). A wait for the user to stop, which every key press
restarted, made no progress at all while he studied, so the pass stood
unfinished for a whole session and the graphs kept saying that their numbers
were being computed.

A pass that fails says so. It cannot report progress, so a failure left no
trace at all beyond a log line, and an empty FSRS-7 series looks the same as
one that is merely still being computed. A pass cut short because its
collection closed did not fail, and stops without a word.

Before the predictions, the same pass optimizes the FSRS-7 parameters of
every preset whose "Optimize every N days" is due (spec
deck-options.fsrs-auto-optimize), one preset per call and with the same rest
between two of them. New parameters drop that preset's predictions, so the
rest of the pass then writes them again for the new parameters.

It runs at most once a day on its own, which is the upkeep the graphs need:
a stored row is a validation fold, and the per-answer rows written while
reviewing carry a different sample role that the graph's role order hides
behind the folds, so live answering does not keep the set fresh.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

# the profile key holding the day number of the last finished pass
LAST_PASS_DAY_KEY = "lastFsrsPredictionPassDay"
# how long to wait before asking again while the RWKV state cache loads
RWKV_RETRY_SECS = 5.0
# how long Clanki must go without a key press, click or scroll before the
# pass BEGINS: its first call holds the collection for up to several
# seconds, and at start-up that is exactly when the user clicks
USER_IDLE_SECS = 10.0
# how often that wait looks again, so the pass stops as soon as the profile
# closes under it instead of at the end of a whole countdown
START_WAIT_CHECK_SECS = 0.25
# Once it has begun, the pass rests between two presets instead of waiting
# for the user; these size that rest (`rest_seconds`).
# the user counts as working for this long after a key press, a click or a
# scroll
USER_ACTIVE_SECS = 2.0
# while the user works, the pass rests this many times as long as the preset
# it has just done, so it leaves most of the machine to him
REST_RATIO = 2.0
# it always rests a moment, so that a click waiting for the collection takes
# it before the pass asks again
MIN_REST_SECS = 0.05
# and never more than this, so that the whole pass still finishes in about
# twice its own length rather than standing unfinished
MAX_REST_SECS = 5.0

_lock = threading.Lock()
_running = False
# true only while a backend call of the pass holds the collection, not
# while the pass waits for a pause and not while it rests
_holding_collection = False
_waiting = False
# a failed pass warns once per session, not once per preset and not once
# per retry
_failure_reported = False


def rwkv_startup_busy(mw: Any) -> bool:
    """True while the RWKV state cache is loading or building. That load
    holds the collection, so the pass must not queue in front of it."""
    return bool(getattr(mw, "_rwkv_state_cache_loading", False))


def reset_failure_report() -> None:
    """Forget that a failure was reported. For tests only."""

    global _failure_reported
    with _lock:
        _failure_reported = False


def is_running() -> bool:
    """True while a pass is writing rows, so a graph can say so instead of
    showing an algorithm as absent."""
    with _lock:
        return _running


def is_holding_collection() -> bool:
    """True while the pass is inside a backend call, which holds the
    collection for up to several seconds."""
    return _holding_collection


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


def seconds_since_input(mw: Any) -> float | None:
    """How long ago the user last pressed a key, clicked or scrolled in
    Clanki; None where nothing tracks it."""
    last_input_at = getattr(getattr(mw, "app", None), "last_input_at", None)
    if not isinstance(last_input_at, float):
        return None
    return time.monotonic() - last_input_at


def _wait_for_a_pause(mw: Any, col: Any) -> bool:
    """Waits, BEFORE the pass begins, until the user has left Clanki alone
    for USER_IDLE_SECS. False when the collection closed meanwhile: the pass
    then stops.

    Only the start waits. Once the pass has begun it rests between two
    presets instead (`rest_seconds`), because this wait makes no progress at
    all while the user works: every key press starts the countdown again.
    """
    while True:
        if mw.col is not col:
            return False
        since_input = seconds_since_input(mw)
        if since_input is None or since_input >= USER_IDLE_SECS:
            return True
        # in short pieces, so that a profile closing under the wait stops
        # the pass at once instead of up to USER_IDLE_SECS later
        time.sleep(min(USER_IDLE_SECS - since_input, START_WAIT_CHECK_SECS))


def rest_seconds(mw: Any, preset_seconds: float) -> float:
    """How long the pass rests after a preset that took `preset_seconds`
    (spec ui.stats-fsrs-predictions-ready).

    While the user works, the rest is a multiple of the preset just done,
    bounded by MAX_REST_SECS, so the pass gives him most of the machine back
    between presets and still finishes in about twice its own length. While
    the user is away it is only long enough to let a click that is already
    waiting for the collection take it first. It is never a wait for the
    user to stop.
    """
    since_input = seconds_since_input(mw)
    working = since_input is not None and since_input < USER_ACTIVE_SECS
    rest = max(preset_seconds, 0.0) * REST_RATIO if working else 0.0
    return min(max(rest, MIN_REST_SECS), MAX_REST_SECS)


def _rest_after(mw: Any, col: Any, started: float) -> bool:
    """Rests after the preset that began at `started`, so that the user's
    own work goes in between two presets instead of behind all of them.
    False when the collection closed: the pass then stops.

    The pass holds nothing here, and nothing across the rest: it takes the
    collection inside a backend call and gives it back when that call
    returns, so there is no lock to hand back for the length of the rest.
    """
    if mw.col is not col:
        return False
    time.sleep(rest_seconds(mw, time.monotonic() - started))
    return mw.col is col


@contextmanager
def _holding() -> Iterator[None]:
    global _holding_collection
    _holding_collection = True
    try:
        yield
    finally:
        _holding_collection = False


def _run(mw: Any, col: Any) -> None:
    global _running
    try:
        # the pass does not begin until the user has paused: asking which
        # presets are stale holds the collection too
        if not _wait_for_a_pause(mw, col):
            return
        optimized = _auto_optimize(mw, col)
        if optimized is None:
            return
        failed = not optimized
        # the generated backend method already returns the ids, not the
        # response message; reading a field off them raised AttributeError
        # on the pass's first line and the log was the only place it showed
        started = time.monotonic()
        with _holding():
            presets = list(col._backend.stale_fsrs_prediction_presets())
        if not _rest_after(mw, col, started):
            return
        written = 0
        for index, preset in enumerate(presets):
            started = time.monotonic()
            try:
                with _holding():
                    written += col._backend.refresh_fsrs_review_predictions(
                        deck_config_id=preset
                    )
            except Exception:
                # one bad preset does not stop the others (spec
                # ui.stats-fsrs-predictions-ready); a closed collection
                # stops the pass quietly, below
                if _collection_closed(mw, col):
                    raise
                logger.exception("FSRS review predictions of preset %s failed", preset)
                failed = True
            # the collection is free here, so anything the user does goes in
            # between two presets instead of behind all of them
            if index + 1 < len(presets) and not _rest_after(mw, col, started):
                return
        logger.debug(
            "stored %s FSRS review predictions over %s presets", written, len(presets)
        )
        if failed:
            # the day stays open, so the next pass tries the failed presets
            # again
            report_failure(mw)
        else:
            _record_finished(mw, col)
    except Exception:
        if _collection_closed(mw, col):
            # the backend gives the collection back between the steps of a
            # preset, so a close or a full sync can take it in between and
            # the next step finds none: the pass was cut short, it did not
            # fail, and the day stays undone
            logger.info("the FSRS review prediction pass stopped: collection closed")
        else:
            logger.exception("the FSRS review prediction pass failed")
            report_failure(mw)
    finally:
        with _lock:
            _running = False
    if mw.col is not col:
        # a profile that opened while this pass still ran found it running
        # and asked for nothing; ask for it now
        ensure_ready(mw)


def _collection_closed(mw: Any, col: Any) -> bool:
    """True when the pass's collection is no longer open: the profile
    closed or switched (mw.col moved on), or a full sync closed it in place
    (its db is gone)."""
    return mw.col is not col or col.db is None


def _auto_optimize(mw: Any, col: Any) -> bool | None:
    """Optimizes the presets that are due, one per call, with a rest between
    two of them. None when the collection closed meanwhile; False when a
    preset could not be optimized. That preset is logged and skipped, and
    the others are still optimized (spec deck-options.fsrs-auto-optimize)."""
    started = time.monotonic()
    with _holding():
        presets = list(col._backend.fsrs_presets_due_for_auto_optimize())
    if not _rest_after(mw, col, started):
        return None
    changed = False
    all_optimized = True
    for preset in presets:
        started = time.monotonic()
        try:
            with _holding():
                changed |= bool(
                    col._backend.auto_optimize_fsrs_preset(deck_config_id=preset)
                )
        except Exception:
            if _collection_closed(mw, col):
                raise
            logger.exception("optimizing FSRS preset %s failed", preset)
            all_optimized = False
        if not _rest_after(mw, col, started):
            return None
    if changed:
        # cards' memory states (and due dates, with "Reschedule cards when
        # desired retention changes") moved: the screens show them again,
        # as after a save in deck options
        mw.taskman.run_on_main(lambda: _refresh_screens(mw, col))
    return all_optimized


def _refresh_screens(mw: Any, col: Any) -> None:
    from anki.collection import OpChanges
    from aqt import gui_hooks

    if mw.col is not col:
        return
    changes = OpChanges(card=True, deck_config=True, study_queues=True)
    gui_hooks.operation_did_execute(changes, None)
    gui_hooks.state_did_reset()


def report_failure(mw: Any) -> None:
    """Tell the user that the pass failed, once per session.

    A pass that writes nothing and says nothing looks exactly like a
    collection that needs no pass. This one died on its first line for two
    days, and the only sign was an empty FSRS-7 series
    (spec ui.stats-fsrs-predictions-ready).
    """

    global _failure_reported
    with _lock:
        if _failure_reported:
            return
        _failure_reported = True
    run_on_main = getattr(getattr(mw, "taskman", None), "run_on_main", None)
    if not callable(run_on_main):
        return

    def show() -> None:
        from aqt.utils import showWarning, tr

        showWarning(tr.qt_misc_fsrs_predictions_pass_failed(), parent=mw)

    run_on_main(show)
