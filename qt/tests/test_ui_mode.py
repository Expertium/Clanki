# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/ui.md#ui.mode-switch.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import anki.lang

# aqt.deckbrowser reads translated strings at import time
anki.lang.set_lang("en")

from aqt.deckbrowser import DeckBrowser  # noqa: E402
from aqt.toolbar import Toolbar  # noqa: E402


def _toolbar(advanced: bool) -> tuple[Toolbar, list[bool]]:
    calls: list[bool] = []
    mw = SimpleNamespace(
        col=object(),
        advanced_ui=lambda: advanced,
        set_advanced_ui=calls.append,
        _sync_advanced_ui_action=lambda: None,
    )
    return Toolbar(cast(Any, mw), MagicMock()), calls


def _options(html: str) -> tuple[str, str]:
    simple, advanced = html.split("</a>")[:2]
    assert simple.endswith(">Simple") and advanced.endswith(">Advanced")
    return simple, advanced


def test_toggle_marks_the_active_side() -> None:
    toolbar, _ = _toolbar(False)
    simple, advanced = _options(toolbar._create_ui_mode_toggle())
    assert 'aria-pressed="true"' in simple and "active" in simple
    assert 'aria-pressed="false"' in advanced and "active" not in advanced

    toolbar, _ = _toolbar(True)
    simple, advanced = _options(toolbar._create_ui_mode_toggle())
    assert 'aria-pressed="false"' in simple and "active" not in simple
    assert 'aria-pressed="true"' in advanced and "active" in advanced


def test_toggle_sits_in_the_right_tray() -> None:
    toolbar, _ = _toolbar(False)
    assert 'id="ui-mode"' in toolbar._right_tray_content()


def test_clicking_a_side_sets_the_mode() -> None:
    toolbar, calls = _toolbar(False)
    toolbar._create_ui_mode_toggle()
    toolbar.link_handlers["uimode:advanced"]()
    toolbar.link_handlers["uimode:simple"]()
    assert calls == [True, False]


def test_no_toggle_without_a_collection() -> None:
    mw = SimpleNamespace(col=None)
    assert Toolbar(cast(Any, mw), MagicMock())._create_ui_mode_toggle() == ""


def test_deck_browser_bottom_row_is_hidden_in_simple_mode() -> None:
    drawn: list[str] = []

    def draw(buf: str = "", **_kwargs: object) -> None:
        drawn.append(buf)

    browser = cast(
        Any,
        SimpleNamespace(
            mw=SimpleNamespace(advanced_ui=lambda: False),
            drawLinks=DeckBrowser.drawLinks,
            bottom=SimpleNamespace(draw=draw),
            _linkHandler=lambda _url: None,
        ),
    )
    DeckBrowser._drawButtons(browser)
    assert drawn == [""]

    browser.mw = SimpleNamespace(advanced_ui=lambda: True)
    DeckBrowser._drawButtons(browser)
    assert "<button" in drawn[1]
