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
    predictions.BETWEEN_PRESETS_SECS = 0.05

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
        assert began - ended >= predictions.BETWEEN_PRESETS_SECS / 2


# Pins spec/ui.md#ui.stats-fsrs-predictions-ready
def test_the_pass_waits_for_a_pause_in_what_the_user_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = _quiet()
    backend = _Backend(started, release, presets=[11, 22])
    mw = _mw(backend)
    monkeypatch.setattr(predictions, "BETWEEN_PRESETS_SECS", 0.0)
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
def test_a_preset_waits_for_the_next_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    release = threading.Event()
    backend = _Backend(started, release, presets=[11, 22])
    mw = _mw(backend)
    monkeypatch.setattr(predictions, "BETWEEN_PRESETS_SECS", 0.0)
    monkeypatch.setattr(predictions, "USER_IDLE_SECS", 0.3)
    mw.app = SimpleNamespace(last_input_at=time.monotonic() - 1.0)

    predictions.ensure_ready(mw)
    assert started.wait(5)
    # the user does something while the first preset is being computed
    clicked = time.monotonic()
    mw.app.last_input_at = clicked
    release.set()
    while predictions.is_running():
        pass

    assert backend.refreshed == [11, 22]
    # the second preset waited for the next pause instead of landing on top
    # of what the user was doing
    assert backend.spans[1][0] - clicked >= predictions.USER_IDLE_SECS


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
    started, release = _quiet()
    backend = _Backend(started, release)
    mw = _mw(backend)
    monkeypatch.setattr(predictions, "USER_IDLE_SECS", 0.3)
    mw.app = SimpleNamespace(last_input_at=time.monotonic())

    predictions.ensure_ready(mw)
    mw.col = None
    while predictions.is_running():
        pass

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
