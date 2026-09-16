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


def test_toggle_switches_in_place() -> None:
    """The mode switch updates the control with a script, without reloading
    the toolbar page (spec ui.mode-switch)."""
    for advanced in (False, True):
        toolbar, _ = _toolbar(advanced)
        simple, advanced_option = _options(toolbar._create_ui_mode_toggle())
        assert 'data-mode="simple"' in simple
        assert 'data-mode="advanced"' in advanced_option
        toolbar.update_ui_mode_toggle()
        web = cast(MagicMock, toolbar.web)
        web.eval.assert_called_once_with(
            f"setUiMode({'true' if advanced else 'false'})"
        )
        web.stdHtml.assert_not_called()


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


def test_deck_browser_bottom_row_has_no_import_in_simple_mode() -> None:
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
    browser._buttons_html = lambda: DeckBrowser._buttons_html(browser)
    DeckBrowser._drawButtons(browser)
    assert drawn[0].count("<button") == 2
    assert 'pycmd("shared")' in drawn[0] and 'pycmd("create")' in drawn[0]
    assert "import" not in drawn[0]

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


def test_switching_the_mode_redraws_without_a_full_reset() -> None:
    def switch(state: str) -> Any:
        mw = cast(
            Any,
            SimpleNamespace(
                col=MagicMock(),
                state=state,
                advanced_ui=lambda: False,
                _sync_advanced_ui_action=lambda: None,
                toolbar=MagicMock(),
                deckBrowser=MagicMock(),
                reset=MagicMock(),
            ),
        )
        AnkiQt.set_advanced_ui(mw, True)
        mw.col.set_config_bool.assert_called_once_with(Config.Bool.ADVANCED_UI, True)
        # the toolbar control switches in place: a reload would clear the
        # sync button's state
        mw.toolbar.update_ui_mode_toggle.assert_called_once()
        mw.toolbar.draw.assert_not_called()
        # a full reset would recompute the RWKV due counts
        mw.reset.assert_not_called()
        return mw

    mw = switch("deckBrowser")
    mw.deckBrowser.redraw_for_ui_mode.assert_called_once()
    mw = switch("review")
    mw.deckBrowser.redraw_for_ui_mode.assert_not_called()


def test_deck_browser_mode_redraw_only_draws_the_bottom_bar() -> None:
    browser = cast(
        Any,
        SimpleNamespace(
            _render_data=object(),
            _renderPage=MagicMock(),
            _drawButtons=MagicMock(),
            _redraw_buttons_in_place=MagicMock(return_value=False),
            refresh=MagicMock(),
        ),
    )
    DeckBrowser.redraw_for_ui_mode(browser)
    # the page with the tree on screen does not read the mode
    browser._drawButtons.assert_called_once()
    browser._renderPage.assert_not_called()
    browser.refresh.assert_not_called()

    # the buttons swapped in the open bar: no drawing
    browser._drawButtons.reset_mock()
    browser._redraw_buttons_in_place.return_value = True
    DeckBrowser.redraw_for_ui_mode(browser)
    browser._drawButtons.assert_not_called()

    # nothing rendered yet: a normal refresh
    browser = cast(
        Any,
        SimpleNamespace(
            _renderPage=MagicMock(), _drawButtons=MagicMock(), refresh=MagicMock()
        ),
    )
    DeckBrowser.redraw_for_ui_mode(browser)
    browser._renderPage.assert_not_called()
    browser._drawButtons.assert_not_called()
    browser.refresh.assert_called_once()


def test_mode_switch_swaps_the_deck_list_buttons_in_place() -> None:
    from aqt import gui_hooks
    from aqt.deckbrowser import DeckBrowserBottomBar
    from aqt.toolbar import BottomBar

    def browser_with_bar(advanced: bool) -> tuple[Any, MagicMock]:
        mw = MagicMock()
        mw.advanced_ui.return_value = advanced
        web = MagicMock()
        browser = DeckBrowser.__new__(DeckBrowser)
        browser.mw = mw
        browser.bottom = BottomBar(mw, web)
        browser._render_data = cast(Any, object())
        web._bridge_context = DeckBrowserBottomBar(browser)
        return browser, web

    browser, web = browser_with_bar(True)
    browser.redraw_for_ui_mode()
    web.stdHtml.assert_not_called()
    script = web.eval.call_args.args[0]
    assert ".deck-buttons" in script and 'pycmd(\\"import\\")' in script
    assert script.count("<button") == 3
    web.adjustHeightToFit.assert_called_once()

    # the bar shows another screen's buttons: it is drawn
    browser, web = browser_with_bar(False)
    web._bridge_context = object()
    browser.redraw_for_ui_mode()
    web.stdHtml.assert_called_once()
    assert web.stdHtml.call_args.args[0].count("<button") == 2

    # an add-on decorates freshly drawn pages: the bar is drawn
    def addon_handler(web_content: Any, context: Any) -> None:
        pass

    addon_handler.__module__ = "some_addon"
    gui_hooks.webview_will_set_content.append(addon_handler)
    try:
        browser, web = browser_with_bar(False)
        browser.redraw_for_ui_mode()
        web.stdHtml.assert_called_once()
        web.eval.assert_not_called()
    finally:
        gui_hooks.webview_will_set_content.remove(addon_handler)

    # an add-on replaced the drawing of the buttons: it is drawn its way
    browser, web = browser_with_bar(False)
    browser._drawButtons = MagicMock()  # type: ignore[method-assign]
    browser.redraw_for_ui_mode()
    browser._drawButtons.assert_called_once()
    web.eval.assert_not_called()


def test_filtered_deck_failure_avoids_retrievability_in_simple_mode() -> None:
    """Pins spec/ui.md#ui.simple-recall-wording."""
    from aqt.operations.scheduling import _filtered_deck_preparation_failed_message

    def col(advanced: bool) -> Any:
        return cast(
            Any,
            SimpleNamespace(
                get_config_bool=lambda key: advanced and key == Config.Bool.ADVANCED_UI
            ),
        )

    simple = _filtered_deck_preparation_failed_message(col(False))
    assert "retrievability" not in simple.lower()
    assert "probability of recall" in simple.lower()

    advanced = _filtered_deck_preparation_failed_message(col(True))
    assert "retrievability" in advanced.lower()


def test_deck_options_mode_switch_sets_the_main_window_mode(
    monkeypatch: Any,
) -> None:
    """The deck-options switch writes the same flag through the main window,
    which redraws its own switch (spec ui.mode-switch)."""
    import aqt
    from anki import generic_pb2
    from aqt import mediasrv

    calls: list[bool] = []
    mw = SimpleNamespace(
        set_advanced_ui=calls.append,
        taskman=SimpleNamespace(run_on_main=lambda fn: fn()),
    )
    monkeypatch.setattr(aqt, "mw", mw, raising=False)
    for value in (True, False):
        request = SimpleNamespace(data=generic_pb2.Bool(val=value).SerializeToString())
        monkeypatch.setattr(mediasrv, "request", request)
        assert mediasrv.set_advanced_ui() == b""
    assert calls == [True, False]
    assert mediasrv.set_advanced_ui in mediasrv.post_handler_list
