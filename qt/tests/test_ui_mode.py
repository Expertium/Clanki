# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/ui.md#ui.mode-switch.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import anki.lang
from anki.config import Config

# aqt.deckbrowser reads translated strings at import time
anki.lang.set_lang("en")

from aqt.deckbrowser import DeckBrowser  # noqa: E402
from aqt.main import AnkiQt  # noqa: E402
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


def test_deck_browser_bottom_row_keeps_only_create_deck_in_simple_mode() -> None:
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
    assert drawn[0].count("<button") == 1
    assert 'pycmd("create")' in drawn[0]
    assert "shared" not in drawn[0] and "import" not in drawn[0]

    browser.mw = SimpleNamespace(advanced_ui=lambda: True)
    DeckBrowser._drawButtons(browser)
    assert drawn[1].count("<button") == 3


def test_deck_menu_rwkv_submenu_is_advanced_only() -> None:
    def browser(advanced: bool) -> Any:
        return cast(
            Any,
            SimpleNamespace(
                mw=SimpleNamespace(advanced_ui=lambda: advanced),
                _reschedule_with_rwkv_curve=lambda _did: None,
                _reschedule_all_decks_with_rwkv_curve=lambda: None,
            ),
        )

    menu = MagicMock()
    DeckBrowser._add_rwkv_menu(browser(False), menu, "1")
    menu.addMenu.assert_not_called()

    menu = MagicMock()
    DeckBrowser._add_rwkv_menu(browser(True), menu, "1")
    menu.addMenu.assert_called_once()
    assert menu.addMenu.return_value.addAction.call_count == 2


def test_addons_menu_entry_follows_the_mode_unless_addons_are_installed() -> None:
    def shown(advanced: bool, addons: list[str]) -> bool:
        action = MagicMock()
        mw = cast(
            Any,
            SimpleNamespace(
                advanced_ui=lambda: advanced,
                addonManager=SimpleNamespace(allAddons=lambda: addons),
                form=SimpleNamespace(actionAdd_ons=action),
            ),
        )
        AnkiQt._sync_addons_action(mw)
        action.setVisible.assert_called_once()
        return bool(action.setVisible.call_args.args[0])

    assert shown(True, [])
    assert shown(True, ["some_addon"])
    assert not shown(False, [])
    assert shown(False, ["some_addon"])


def test_switching_the_mode_redraws_without_a_full_reset() -> None:
    def switch(state: str) -> Any:
        mw = cast(
            Any,
            SimpleNamespace(
                col=MagicMock(),
                state=state,
                advanced_ui=lambda: False,
                _sync_advanced_ui_action=lambda: None,
                _sync_addons_action=lambda: None,
                toolbar=MagicMock(),
                deckBrowser=MagicMock(),
                reset=MagicMock(),
            ),
        )
        AnkiQt.set_advanced_ui(mw, True)
        mw.col.set_config_bool.assert_called_once_with(Config.Bool.ADVANCED_UI, True)
        mw.toolbar.draw.assert_called_once()
        # a full reset would recompute the RWKV due counts
        mw.reset.assert_not_called()
        return mw

    mw = switch("deckBrowser")
    mw.deckBrowser.redraw_for_ui_mode.assert_called_once()
    mw = switch("review")
    mw.deckBrowser.redraw_for_ui_mode.assert_not_called()


def test_deck_browser_mode_redraw_reuses_the_tree_on_screen() -> None:
    browser = cast(
        Any,
        SimpleNamespace(
            _render_data=object(),
            _renderPage=MagicMock(),
            refresh=MagicMock(),
        ),
    )
    DeckBrowser.redraw_for_ui_mode(browser)
    browser._renderPage.assert_called_once_with(reuse=True)
    browser.refresh.assert_not_called()

    # nothing rendered yet: a normal refresh
    browser = cast(Any, SimpleNamespace(_renderPage=MagicMock(), refresh=MagicMock()))
    DeckBrowser.redraw_for_ui_mode(browser)
    browser._renderPage.assert_not_called()
    browser.refresh.assert_called_once()
