# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The automatic FSRS-7 prediction pass (spec ui.stats-fsrs-predictions-ready)."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import aqt.fsrs_predictions as predictions


class _Backend:
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.calls = 0
        self._started = started
        self._release = release

    def refresh_fsrs_review_predictions(self) -> int:
        self.calls += 1
        self._started.set()
        self._release.wait(5)
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
