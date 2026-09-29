# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins what the application-wide event filter does. Qt hands it every event
of every object; it keeps the time of the user's last input (the FSRS-7
predictions pass waits for a pause) and moves the global cursor."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture(scope="module")
def app() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from aqt.qt import QApplication

    return QApplication.instance() or QApplication([])


class _Filter:
    """AnkiApp.eventFilter on a stand-in for the application: its constants
    and two recording cursor calls."""

    def __init__(self) -> None:
        import aqt

        constants = {
            name: value
            for name, value in vars(aqt.AnkiApp).items()
            if name.lstrip("_").isupper()
        }
        self.calls: list[str] = []
        self.app = SimpleNamespace(
            **constants,
            last_input_at=0.0,
            setOverrideCursor=lambda cursor: self.calls.append("pointer"),
            restoreOverrideCursor=lambda: self.calls.append("restore"),
        )

    def __call__(self, source: Any, kind: Any) -> bool:
        import aqt
        from aqt.qt import QCloseEvent, QEvent, QKeyEvent, Qt

        if kind == QEvent.Type.Close:
            event: QEvent = QCloseEvent()
        elif kind == QEvent.Type.KeyPress:
            # the filter reads a key press's key on Linux and macOS
            event = QKeyEvent(kind, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier)
        else:
            event = QEvent(kind)
        return aqt.AnkiApp.eventFilter(self.app, source, event)  # type: ignore[arg-type]


def test_only_input_events_move_the_last_input_time(app: Any) -> None:
    from aqt.qt import QEvent

    kinds = QEvent.Type
    filter_ = _Filter()
    for kind in (
        kinds.KeyPress,
        kinds.MouseButtonPress,
        kinds.MouseButtonDblClick,
        kinds.Wheel,
        kinds.TouchBegin,
    ):
        filter_.app.last_input_at = 0.0
        assert filter_(None, kind) is False
        assert filter_.app.last_input_at > 0.0, kind
    for kind in (
        kinds.Paint,
        kinds.Timer,
        kinds.MouseMove,
        kinds.MouseButtonRelease,
        kinds.KeyRelease,
        kinds.Enter,
        kinds.Leave,
    ):
        filter_.app.last_input_at = 0.0
        assert filter_(None, kind) is False
        assert filter_.app.last_input_at == 0.0, kind


def test_the_cursor_is_a_pointer_over_enabled_buttons_and_closed_combo_boxes(
    app: Any,
) -> None:
    from aqt.qt import (
        QCheckBox,
        QComboBox,
        QEvent,
        QLabel,
        QPushButton,
        QTabBar,
        QToolButton,
    )

    filter_ = _Filter()
    editable = QComboBox()
    editable.setEditable(True)
    disabled = QPushButton()
    disabled.setEnabled(False)
    cases = [
        (QPushButton(), "pointer"),
        (QCheckBox(), "pointer"),
        (QToolButton(), "pointer"),
        (QTabBar(), "pointer"),
        (QComboBox(), "pointer"),
        (disabled, "restore"),
        (editable, "restore"),
        (QLabel(), "restore"),
        (None, "restore"),
    ]
    for kind in (QEvent.Type.Enter, QEvent.Type.HoverEnter):
        for widget, expected in cases:
            filter_.calls.clear()
            assert filter_(widget, kind) is False
            assert filter_.calls == [expected], (kind, widget)


def test_the_cursor_is_restored_when_the_pointer_leaves_or_a_window_closes(
    app: Any,
) -> None:
    from aqt.qt import QEvent, QPushButton

    filter_ = _Filter()
    for kind in (QEvent.Type.Leave, QEvent.Type.HoverLeave, QEvent.Type.Close):
        filter_.calls.clear()
        assert filter_(QPushButton(), kind) is False
        assert filter_.calls == ["restore"], kind
    filter_.calls.clear()
    assert filter_(QPushButton(), QEvent.Type.Paint) is False
    assert filter_.calls == []
