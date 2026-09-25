# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The automatic FSRS-7 prediction pass (spec ui.stats-fsrs-predictions-ready)."""

from __future__ import annotations

import inspect
import threading
import time
from types import SimpleNamespace

import pytest

import aqt.fsrs_predictions as predictions


class _Backend:
    def __init__(
        self,
        started: threading.Event,
        release: threading.Event,
        presets: list[int] | None = None,
    ) -> None:
        self.calls = 0
        self.presets_asked = 0
        self.refreshed: list[int] = []
        # when each preset's call started and ended, so a test can see that
        # the collection was free in between
        self.spans: list[tuple[float, float]] = []
        self._presets = presets if presets is not None else [1]
        self._started = started
        self._release = release
        self.due_for_optimize: list[int] = []
        self.optimized: list[int] = []
        self.optimize_changes = True
        # every backend call of a pass, in order
        self.order: list[str] = []

    def fsrs_presets_due_for_auto_optimize(self) -> list[int]:
        return list(self.due_for_optimize)

    def auto_optimize_fsrs_preset(self, deck_config_id: int) -> bool:
        self.order.append("optimize")
        self.optimized.append(deck_config_id)
        return self.optimize_changes

    def stale_fsrs_prediction_presets(self) -> list[int]:
        """The SHAPE the generated backend really returns: the ids, not a
        response message. The fake used to wrap them in an object with a
        `deck_config_ids` field, which the real backend has never had, so
        the pass read a field that did not exist, raised on its first line
        and wrote nothing for two days."""

        self.presets_asked += 1
        return list(self._presets)

    def refresh_fsrs_review_predictions(self, deck_config_id: int) -> int:
        began = time.monotonic()
        self.calls += 1
        self.order.append("refresh")
        self.refreshed.append(deck_config_id)
        self._started.set()
        self._release.wait(5)
        self.spans.append((began, time.monotonic()))
        return 7


def _mw(backend: _Backend, today: int = 3) -> SimpleNamespace:
    collection = SimpleNamespace(_backend=backend, sched=SimpleNamespace(today=today))
    warnings: list[object] = []
    return SimpleNamespace(
        col=collection,
        pm=SimpleNamespace(profile={}),
        taskman=SimpleNamespace(run_on_main=warnings.append),
        reported_failures=warnings,
    )


def _quiet() -> tuple[threading.Event, threading.Event]:
    started = threading.Event()
    release = threading.Event()
    release.set()
    return started, release


@pytest.fixture(autouse=True)
def _forget_the_failure_report() -> object:
    predictions.reset_failure_report()
    yield
    predictions.reset_failure_report()


# Pins spec/ui.md#ui.stats-fsrs-predictions-ready
def test_the_pass_runs_by_itself_and_only_once_a_day() -> None:
    started, release = _quiet()
    backend = _Backend(started, release)
    mw = _mw(backend)

    predictions.ensure_ready(mw)
    started.wait(5)
    while predictions.is_running():
        pass

    assert backend.calls == 1
    assert mw.pm.profile[predictions.LAST_PASS_DAY_KEY] == 3

    # the same day, on its own, it does not run again
    predictions.ensure_ready(mw)
    assert backend.calls == 1

    # a parameter change does not wait for tomorrow
    predictions.ensure_ready(mw, force=True)
    while predictions.is_running():
        pass
    assert backend.calls == 2

    # and the next day it runs on its own again
    mw.col.sched.today = 4
    predictions.ensure_ready(mw)
    while predictions.is_running():
        pass
    assert backend.calls == 3


# Pins spec/ui.md#ui.stats-fsrs-predictions-ready
def test_a_graph_can_see_that_the_pass_is_running() -> None:
    started = threading.Event()
    release = threading.Event()
    backend = _Backend(started, release)
    mw = _mw(backend)

    assert not predictions.is_running()
    predictions.ensure_ready(mw)
    started.wait(5)
    # while it writes rows the graph says so, instead of calling FSRS-7
    # absent for want of reviews
    assert predictions.is_running()
    # and a second request does not start a second pass
    predictions.ensure_ready(mw, force=True)
    assert backend.calls == 1

    release.set()
    while predictions.is_running():
        pass
    assert not predictions.is_running()


# Pins spec/ui.md#ui.stats-fsrs-predictions-ready
def test_the_pass_waits_for_the_rwkv_state_cache() -> None:
    started, release = _quiet()
    backend = _Backend(started, release)
    mw = _mw(backend)
    # the RWKV state cache holds the collection while it loads: the pass
    # must not queue in front of it
    mw._rwkv_state_cache_loading = True
    predictions.RWKV_RETRY_SECS = 0.05

    predictions.ensure_ready(mw)
    time.sleep(0.15)
    assert backend.presets_asked == 0
    assert backend.calls == 0
    assert not predictions.is_running()

    # once the load is done, the waiting pass starts by itself
    mw._rwkv_state_cache_loading = False
    assert started.wait(5)
    while predictions.is_running():
        pass
    assert backend.calls == 1


# Pins spec/ui.md#ui.stats-fsrs-predictions-ready
def test_the_collection_is_free_between_presets() -> None:
    started, release = _quiet()
    backend = _Backend(started, release, presets=[11, 22, 33])
    mw = _mw(backend)

    predictions.ensure_ready(mw)
    started.wait(5)
    while predictions.is_running():
        pass

    # one call per stale preset, never one call for all of them
    assert backend.presets_asked == 1
    assert backend.refreshed == [11, 22, 33]
    # and each call ended before the next began, with a gap: the collection
    # is free in between, so the main thread can get in
    for (_, ended), (began, _) in zip(backend.spans, backend.spans[1:]):
        assert began >= ended
        assert began - ended >= predictions.MIN_REST_SECS / 2


# Pins spec/ui.md#ui.stats-fsrs-predictions-ready
def test_the_pass_waits_for_a_pause_in_what_the_user_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = _quiet()
    backend = _Backend(started, release, presets=[11, 22])
    mw = _mw(backend)
    monkeypatch.setattr(predictions, "USER_IDLE_SECS", 0.3)
    # the user has just clicked
    mw.app = SimpleNamespace(last_input_at=time.monotonic())

    predictions.ensure_ready(mw)
    # not even the question of which presets are stale: that holds the
    # collection too
    time.sleep(0.15)
    assert backend.presets_asked == 0
    assert predictions.is_running()

    # after the pause it starts by itself
    assert started.wait(5)
    while predictions.is_running():
        pass
    assert backend.spans[0][0] - mw.app.last_input_at >= predictions.USER_IDLE_SECS
    assert backend.refreshed == [11, 22]
    assert mw.pm.profile[predictions.LAST_PASS_DAY_KEY] == 3


# Pins spec/ui.md#ui.stats-fsrs-predictions-ready
def test_a_started_pass_never_waits_for_the_user_to_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule: once the pass has begun, no piece of it waits for a pause.

    It used to wait for USER_IDLE_SECS of quiet before every preset. A user
    who keeps working never gives it one, so the pass made no progress at
    all while he studied: measured on a 966,822-row collection, 0 of 11
    presets in five minutes of use, for a job of 25 seconds.
    """

    started = threading.Event()
    release = threading.Event()
    backend = _Backend(started, release, presets=[11, 22, 33])
    # the optimization half of the pass is under the same rule
    backend.due_for_optimize = [11, 22]
    mw = _mw(backend)
    # long enough that a pass which waited for a pause would never finish
    monkeypatch.setattr(predictions, "USER_IDLE_SECS", 30.0)
    mw.app = SimpleNamespace(last_input_at=time.monotonic() - 60.0)

    predictions.ensure_ready(mw)
    assert started.wait(5)

    # the user works throughout, so a countdown would restart forever
    stop = threading.Event()

    def working() -> None:
        while not stop.wait(0.01):
            mw.app.last_input_at = time.monotonic()

    worker = threading.Thread(target=working, daemon=True)
    worker.start()
    release.set()
    deadline = time.monotonic() + 10.0
    while predictions.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    worker.join(1)
    still_running = predictions.is_running()
    # so that a pass which did wait does not run on into the next test
    mw.col = None
    while predictions.is_running():
        time.sleep(0.01)

    assert not still_running
    assert backend.optimized == [11, 22]
    assert backend.refreshed == [11, 22, 33]
    assert mw.pm.profile[predictions.LAST_PASS_DAY_KEY] == 3


# Pins spec/ui.md#ui.stats-fsrs-predictions-ready
def test_the_rest_between_presets_is_a_bounded_multiple_of_the_preset() -> None:
    """The rest is sized, not open-ended: a multiple of the preset just done
    while the user works, a moment while he is away, bounded either way."""

    working = SimpleNamespace(app=SimpleNamespace(last_input_at=time.monotonic()))
    away = SimpleNamespace(
        app=SimpleNamespace(
            last_input_at=time.monotonic() - predictions.USER_ACTIVE_SECS - 1.0
        )
    )
    nothing_tracked = SimpleNamespace()

    # while the user works: REST_RATIO times the preset just done
    assert predictions.rest_seconds(working, 0.5) == pytest.approx(
        0.5 * predictions.REST_RATIO
    )
    # never longer than MAX_REST_SECS, however long the preset was
    assert predictions.rest_seconds(working, 3600.0) == predictions.MAX_REST_SECS
    # and never shorter than MIN_REST_SECS, so a click waiting for the
    # collection takes it before the pass asks again
    assert predictions.rest_seconds(working, 0.0) == predictions.MIN_REST_SECS

    # while the user is away, or where nothing tracks him: a moment only
    assert predictions.rest_seconds(away, 10.0) == predictions.MIN_REST_SECS
    assert predictions.rest_seconds(nothing_tracked, 10.0) == predictions.MIN_REST_SECS


def test_the_pass_holds_the_collection_only_inside_its_backend_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the deck-options warm-up checks before it reads the collection
    on the main thread: a pass that only waits for a pause holds nothing."""
    started = threading.Event()
    release = threading.Event()
    backend = _Backend(started, release)
    mw = _mw(backend)
    monkeypatch.setattr(predictions, "USER_IDLE_SECS", 0.3)
    mw.app = SimpleNamespace(last_input_at=time.monotonic())

    predictions.ensure_ready(mw)
    time.sleep(0.1)
    assert predictions.is_running()
    assert not predictions.is_holding_collection()

    assert started.wait(5)
    assert predictions.is_holding_collection()
    release.set()
    while predictions.is_running():
        pass
    assert not predictions.is_holding_collection()


# Pins spec/ui.md#ui.stats-fsrs-predictions-ready
def test_a_pass_waiting_for_a_pause_stops_when_the_collection_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And it stops AT ONCE, not at the end of the countdown it is in.

    The wait used to sleep for the whole remaining countdown before looking
    at the collection again, so closing the profile left the pass alive for
    up to USER_IDLE_SECS.
    """

    started, release = _quiet()
    backend = _Backend(started, release)
    mw = _mw(backend)
    monkeypatch.setattr(predictions, "USER_IDLE_SECS", 30.0)
    mw.app = SimpleNamespace(last_input_at=time.monotonic())

    predictions.ensure_ready(mw)
    time.sleep(0.1)
    closed = time.monotonic()
    mw.col = None
    deadline = closed + 5.0
    while predictions.is_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not predictions.is_running()
    assert time.monotonic() - closed < predictions.USER_IDLE_SECS / 2

    # nothing was asked or written, no failure was reported, and the day
    # is not counted as done
    assert backend.presets_asked == 0
    assert backend.calls == 0
    assert not mw.reported_failures
    assert predictions.LAST_PASS_DAY_KEY not in mw.pm.profile


# Pins spec/ui.md#ui.stats-fsrs-predictions-ready
def test_the_fake_backend_returns_what_the_real_backend_returns() -> None:
    """The fake's shape is checked against the generated backend's own.

    The pass read `.deck_config_ids` off the result for two days. Every test
    passed, because the fake had invented that field. A fake that is never
    compared with the real thing pins nothing.
    """

    from anki._backend_generated import RustBackendGenerated as RustBackend

    real = inspect.signature(RustBackend.stale_fsrs_prediction_presets)
    fake = inspect.signature(_Backend.stale_fsrs_prediction_presets)
    # a sequence of ids on both sides, never a message with a field on it
    assert "Sequence[int]" in str(real.return_annotation)
    assert "list[int]" in str(fake.return_annotation)
    assert list(real.parameters) == list(fake.parameters) == ["self"]

    assert (
        inspect.signature(RustBackend.refresh_fsrs_review_predictions).return_annotation
        == "int"
    )
    assert list(
        inspect.signature(RustBackend.refresh_fsrs_review_predictions).parameters
    ) == list(inspect.signature(_Backend.refresh_fsrs_review_predictions).parameters)


# Pins spec/ui.md#ui.stats-fsrs-predictions-ready
def test_a_pass_that_fails_says_so() -> None:
    class _BrokenBackend(_Backend):
        def stale_fsrs_prediction_presets(self) -> list[int]:
            self.presets_asked += 1
            raise RuntimeError("the backend said no")

    started, release = _quiet()
    backend = _BrokenBackend(started, release)
    mw = _mw(backend)
    predictions.reset_failure_report()

    predictions.ensure_ready(mw)
    while predictions.is_running():
        pass

    # it wrote nothing AND it said so: an empty series alone cannot be told
    # apart from a collection that needs no pass
    assert backend.calls == 0
    assert len(mw.reported_failures) == 1
    # the day is not recorded, so the pass tries again rather than counting
    # a failure as today's upkeep
    assert predictions.LAST_PASS_DAY_KEY not in mw.pm.profile

    # a second failure in the same session does not warn again
    mw.col.sched.today = 4
    predictions.ensure_ready(mw)
    while predictions.is_running():
        pass
    assert len(mw.reported_failures) == 1


# Pins spec/deck-options.md#deck-options.fsrs-optimize-skips-bad-presets
def test_one_bad_preset_does_not_stop_the_others() -> None:
    class _OneBadPreset(_Backend):
        def auto_optimize_fsrs_preset(self, deck_config_id: int) -> bool:
            if deck_config_id == 2:
                self.order.append("optimize")
                raise RuntimeError("invalid search")
            return super().auto_optimize_fsrs_preset(deck_config_id)

        def refresh_fsrs_review_predictions(self, deck_config_id: int) -> int:
            if deck_config_id == 5:
                self.order.append("refresh")
                raise RuntimeError("invalid date")
            return super().refresh_fsrs_review_predictions(deck_config_id)

    started, release = _quiet()
    backend = _OneBadPreset(started, release, presets=[4, 5, 6])
    backend.due_for_optimize = [1, 2, 3]
    # no screen refresh, so the only call on the main thread is the warning
    backend.optimize_changes = False
    mw = _mw(backend)

    predictions.ensure_ready(mw)
    started.wait(5)
    while predictions.is_running():
        pass

    # every other preset is optimized and refreshed, after the bad one too
    assert backend.optimized == [1, 3]
    assert backend.refreshed == [4, 6]
    assert backend.order == ["optimize"] * 3 + ["refresh"] * 3
    # the failure is reported once, and the day stays open for a retry
    assert len(mw.reported_failures) == 1
    assert predictions.LAST_PASS_DAY_KEY not in mw.pm.profile


# Pins spec/deck-options.md#deck-options.fsrs-auto-optimize
def test_due_presets_are_optimized_before_the_predictions() -> None:
    started, release = _quiet()
    backend = _Backend(started, release, presets=[1, 2])
    backend.due_for_optimize = [2]
    mw = _mw(backend)

    predictions.ensure_ready(mw)
    started.wait(5)
    while predictions.is_running():
        pass

    # the predictions come after the optimization they depend on
    assert backend.optimized == [2]
    assert backend.refreshed == [1, 2]
    assert backend.order == ["optimize", "refresh", "refresh"]
    # new parameters moved the cards, so the screens are asked to show them
    # again on the main thread
    assert len(mw.reported_failures) == 1 and callable(mw.reported_failures[0])

    # nothing changed: the screens are left alone
    backend.optimize_changes = False
    mw.reported_failures.clear()
    predictions.ensure_ready(mw, force=True)
    while predictions.is_running():
        pass
    assert mw.reported_failures == []


# Pins spec/deck-options.md#deck-options.fsrs-auto-optimize
def test_the_fake_auto_optimize_matches_the_real_backend() -> None:
    from anki._backend_generated import RustBackendGenerated as RustBackend

    real = inspect.signature(RustBackend.fsrs_presets_due_for_auto_optimize)
    assert "Sequence[int]" in str(real.return_annotation)
    optimize = inspect.signature(RustBackend.auto_optimize_fsrs_preset)
    assert optimize.return_annotation == "bool"
    assert list(optimize.parameters) == list(
        inspect.signature(_Backend.auto_optimize_fsrs_preset).parameters
    )
