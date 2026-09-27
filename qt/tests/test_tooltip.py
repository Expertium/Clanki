# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins spec/ui.md#ui.tooltip-style."""

from __future__ import annotations

import os
from typing import Any

import pytest


@pytest.fixture(scope="module")
def app() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from aqt.qt import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    import aqt
    from aqt.qt import QTimer, QWidget

    window = QWidget()
    window.resize(600, 400)
    window.show()

    class Progress:
        def timer(self, *args: Any, **kwargs: Any) -> QTimer:
            return QTimer()

    monkeypatch.setattr(
        aqt, "mw", type("MW", (), {"app": app, "progress": Progress()})(), raising=False
    )
    yield window
    from aqt.utils import closeTooltip

    closeTooltip()


def test_a_tooltip_is_a_rounded_toast_in_the_themes_colours(window: Any) -> None:
    from aqt import utils
    from aqt.qt import Qt
    from aqt.theme import theme_manager

    utils.tooltip("Collection sync complete.", parent=window)
    outer = utils._tooltipLabel
    assert outer is not None
    # the outer widget only holds the margin the shadow is drawn in
    assert outer.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    inner = outer.layout().itemAt(0).widget()
    assert inner.text() == "Collection sync complete."
    style = inner.styleSheet()
    for part in (
        "border-radius",
        theme_manager.var(utils.TOOLTIP_BACKGROUND),
        theme_manager.var(utils.TOOLTIP_TEXT),
        theme_manager.var(utils.TOOLTIP_BORDER),
    ):
        assert part in style
    # grey-blue with black text by day, obsidian black with white text by
    # night (spec ui.tooltip-style)
    assert utils.TOOLTIP_BACKGROUND == {"light": "#e2e5ec", "dark": "#0a0a0a"}
    assert utils.TOOLTIP_TEXT == {"light": "#000000", "dark": "#ffffff"}
    assert inner.graphicsEffect() is not None  # the soft shadow
    # no web view: a tooltip costs no process and no page load
    assert "WebView" not in type(inner).__name__ + type(outer).__name__


def test_a_tooltip_closes_on_a_click_and_on_close(window: Any) -> None:
    from aqt import utils

    utils.tooltip("hi", parent=window)
    outer = utils._tooltipLabel
    assert outer is not None and outer.isVisible()
    outer.mousePressEvent(_press())
    assert not outer.isVisible()
    utils.closeTooltip()
    assert utils._tooltipLabel is None


def test_a_tooltip_on_a_deleted_parent_falls_back_to_the_main_window(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operation's success handler (e.g. a Browser suspend that finished
    after the Browser closed) can call tooltip(parent=...) with a widget
    that is already gone. It must not raise "wrapped C/C++ object ... has
    been deleted"; the tooltip shows over the main window instead."""
    import aqt
    from aqt import utils
    from aqt.qt import QTimer, QWidget, sip

    class Progress:
        def timer(self, *args: Any, **kwargs: Any) -> QTimer:
            return QTimer()

    class FakeMw(QWidget):
        pass

    mw = FakeMw()
    mw.app = app  # type: ignore[attr-defined]
    mw.progress = Progress()  # type: ignore[attr-defined]
    monkeypatch.setattr(aqt, "mw", mw, raising=False)

    closed = QWidget()
    sip.delete(closed)
    assert sip.isdeleted(closed)

    utils.tooltip("hi", parent=closed)
    try:
        outer = utils._tooltipLabel
        assert outer is not None
        assert outer.parent() is mw
    finally:
        utils.closeTooltip()


def _press() -> Any:
    from aqt.qt import QEvent, QMouseEvent, QPointF, Qt

    return QMouseEvent(
        QEvent.Type.MouseButtonPress,
        QPointF(1.0, 1.0),
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
