# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins spec/ui.md#ui.deleted-widget-callbacks-are-skipped."""

from __future__ import annotations

import os
from typing import Any

import pytest


@pytest.fixture(scope="module")
def app() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from aqt.qt import QApplication

    return QApplication.instance() or QApplication([])


def test_ensure_widget_in_screen_boundaries_does_nothing_for_a_deleted_widget(
    app: Any,
) -> None:
    """A delayed screen-boundary retry (aqt.mw.progress.timer, 50ms) can fire
    after the widget it was scheduled for, e.g. a closed Card Info dialog,
    is gone. It must not raise "wrapped C/C++ object ... has been deleted"."""
    from aqt.qt import QWidget, sip
    from aqt.utils import ensureWidgetInScreenBoundaries

    widget = QWidget()
    sip.delete(widget)
    assert sip.isdeleted(widget)

    # must return quietly instead of raising RuntimeError
    ensureWidgetInScreenBoundaries(widget)


def test_show_info_on_a_deleted_parent_falls_back_to_the_main_window(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operation's success handler (e.g. rename_tag's empty-rename notice)
    may run after the widget it was built for has closed. showInfo (and so
    showWarning/showCritical) must fall back to the main window rather than
    crash while building a QMessageBox on a deleted parent."""
    import aqt
    from aqt import utils
    from aqt.qt import QMessageBox, QWidget, sip

    class FakeMw(QWidget):
        pass

    mw = FakeMw()
    mw.app = app  # type: ignore[attr-defined]
    monkeypatch.setattr(aqt, "mw", mw, raising=False)

    captured: list[Any] = []

    class NonBlockingMessageBox(QMessageBox):
        def exec(self) -> Any:
            captured.append(self)
            return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(utils, "QMessageBox", NonBlockingMessageBox)

    closed = QWidget()
    sip.delete(closed)
    assert sip.isdeleted(closed)

    utils.showInfo("hello", parent=closed)

    assert len(captured) == 1
    assert captured[0].parent() is mw
