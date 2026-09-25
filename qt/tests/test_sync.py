# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins the sync half of spec/ui.md#ui.close-says-what-it-waits-for, and
spec/sync.md#sync.algorithm-change-notice."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anki.lang

anki.lang.set_lang("en")

import aqt.sync  # noqa: E402
from anki.lang import without_unicode_isolation  # noqa: E402
from aqt.utils import tr  # noqa: E402


class _TaskManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def with_progress(self, task: Any, on_done: Any, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _mw() -> Any:
    taskman = _TaskManager()
    return SimpleNamespace(
        taskman=taskman,
        col=SimpleNamespace(
            sync_collection=lambda auth, media: None,
            latest_progress=lambda: SimpleNamespace(HasField=lambda _name: False),
        ),
        pm=SimpleNamespace(
            sync_auth=lambda: SimpleNamespace(hkey="k"),
            media_syncing_enabled=lambda: False,
        ),
    )


class _Timer:
    """A QTimer stand-in: the real one refuses a parent that is not a QObject,
    and this test is about the progress window's words, not about timers."""

    def __init__(self, _parent: Any) -> None:
        self.timeout = SimpleNamespace(connect=lambda _handler: None)

    def start(self, _ms: int) -> None:
        pass

    def stop(self) -> None:
        pass

    def deleteLater(self) -> None:
        pass


def test_the_collection_sync_names_ankiweb_and_offers_cancel(monkeypatch) -> None:
    mw = _mw()
    monkeypatch.setattr(aqt.sync, "QTimer", _Timer)
    monkeypatch.setattr(aqt.sync, "qconnect", lambda _signal, _handler: None)

    aqt.sync.sync_collection(mw, lambda: None)

    assert len(mw.taskman.calls) == 1
    call = mw.taskman.calls[0]
    # the old window said only "Checking...", which named neither the step
    # nor who was being waited for
    assert call["label"] == tr.sync_contacting_ankiweb()
    assert call["title"] == tr.sync_syncing_with_ankiweb()
    assert "AnkiWeb" in call["label"]
    assert "AnkiWeb" in call["title"]
    assert call["label"] != tr.sync_checking()
    # the wait can be stopped, and the window says so
    assert call["cancel_label"] == tr.actions_cancel()


def test_cancelling_the_sync_aborts_it() -> None:
    """The Cancel button sets the flag the sync's own timer already reads."""
    aborted: list[str] = []
    progress = SimpleNamespace(
        update=lambda **_kwargs: None,
        set_title=lambda _title: None,
        want_cancel=lambda: True,
    )
    sync_progress = SimpleNamespace(added="1", removed="0", stage="Syncing")
    mw = SimpleNamespace(
        progress=progress,
        col=SimpleNamespace(
            latest_progress=lambda: SimpleNamespace(
                HasField=lambda name: name == "normal_sync",
                normal_sync=sync_progress,
            ),
            abort_sync=lambda: aborted.append("abort"),
        ),
    )

    aqt.sync.on_normal_sync_timer(mw)

    assert aborted == ["abort"]


def _finish_normal_sync(monkeypatch, out: Any) -> list[dict[str, Any]]:
    """Runs sync_collection up to the server's answer `out`; returns the
    tooltips it showed."""
    tooltips: list[dict[str, Any]] = []
    done: list[Any] = []
    mw = _mw()
    mw.taskman = SimpleNamespace(
        with_progress=lambda _task, on_done, **_kwargs: done.append(on_done)
    )
    mw.col._load_scheduler = lambda: None
    mw.pm.set_host_number = lambda _number: None
    mw.media_syncer = SimpleNamespace(start_monitoring=lambda: None)
    monkeypatch.setattr(aqt.sync, "QTimer", _Timer)
    monkeypatch.setattr(aqt.sync, "qconnect", lambda _signal, _handler: None)
    monkeypatch.setattr(aqt.sync, "tooltip", lambda **kwargs: tooltips.append(kwargs))

    aqt.sync.sync_collection(mw, lambda: None)
    done[0](SimpleNamespace(result=lambda: out))
    return tooltips


def test_a_sync_that_changed_the_algorithm_says_so_once(monkeypatch) -> None:
    """Pins spec/sync.md#sync.algorithm-change-notice."""
    from anki.sync import SyncOutput

    out = SyncOutput(
        required=SyncOutput.NO_CHANGES,
        algorithm_changed_to="rwkv_curve",
        algorithm_changed_by_fsrs_off=True,
    )
    tooltips = _finish_normal_sync(monkeypatch, out)
    # a tooltip: it blocks nothing and goes away by itself
    assert [without_unicode_isolation(t["msg"]) for t in tooltips] == [
        "A sync from another device turned FSRS off, so Clanki now uses RWKV-Curve."
    ]
    assert tooltips[0]["period"] == aqt.sync.ALGORITHM_CHANGE_NOTICE_MS

    out = SyncOutput(required=SyncOutput.NO_CHANGES, algorithm_changed_to="fsrs7")
    tooltips = _finish_normal_sync(monkeypatch, out)
    assert [without_unicode_isolation(t["msg"]) for t in tooltips] == [
        "A sync changed the scheduling algorithm. Clanki now uses FSRS-7."
    ]

    # a sync that changed nothing says only that it is complete
    tooltips = _finish_normal_sync(
        monkeypatch, SyncOutput(required=SyncOutput.NO_CHANGES)
    )
    assert [without_unicode_isolation(t["msg"]) for t in tooltips] == [
        tr.sync_collection_complete()
    ]
