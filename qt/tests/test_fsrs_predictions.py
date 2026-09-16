# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The automatic FSRS-7 prediction pass (spec ui.stats-fsrs-predictions-ready)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import aqt.fsrs_predictions as predictions


class _Presets:
    def __init__(self, ids: list[int]) -> None:
        self.deck_config_ids = ids


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

    def stale_fsrs_prediction_presets(self) -> _Presets:
        self.presets_asked += 1
        return _Presets(list(self._presets))

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
    return SimpleNamespace(col=collection, pm=SimpleNamespace(profile={}))


def _quiet() -> tuple[threading.Event, threading.Event]:
    started = threading.Event()
    release = threading.Event()
    release.set()
    return started, release


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
