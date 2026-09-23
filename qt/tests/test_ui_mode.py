# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/ui.md#ui.mode-switch.

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

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
    assert 'pycmd("shared")' in drawn[0] and 'pycmd("create")' in drawn[0]
    assert 'pycmd("import")' not in drawn[0]
    assert 'pycmd("get_addons")' not in drawn[0]

    browser.mw = SimpleNamespace(advanced_ui=lambda: True)
    DeckBrowser._drawButtons(browser)
    assert 'pycmd("shared")' in drawn[1] and 'pycmd("create")' in drawn[1]
    assert 'pycmd("import")' in drawn[1]
    assert 'pycmd("get_addons")' in drawn[1]


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
                _sync_tools_menu_for_ui_mode=lambda: None,
                toolbar=MagicMock(),
                deckBrowser=MagicMock(),
                overview=MagicMock(),
                form=MagicMock(),
                reset=MagicMock(),
            ),
        )
        mw.redraw_for_ui_split = lambda: AnkiQt.redraw_for_ui_split(mw)
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


def test_deck_browser_mode_redraw_updates_the_bottom_bar_and_the_tree() -> None:
    """Pins spec/ui.md#ui.simple-mode-deck-counts: the tree's New/Learn/Due
    columns depend on the mode too, so the mode redraw now updates both the
    bottom bar and the tree, each in place when it can."""
    browser = cast(
        Any,
        SimpleNamespace(
            _render_data=object(),
            _renderPage=MagicMock(),
            _drawButtons=MagicMock(),
            _redraw_buttons_in_place=MagicMock(return_value=False),
            _redraw_tree_in_place=MagicMock(return_value=False),
            refresh=MagicMock(),
        ),
    )
    DeckBrowser.redraw_for_ui_mode(browser)
    browser._drawButtons.assert_called_once()
    browser._redraw_tree_in_place.assert_called_once()
    browser._renderPage.assert_called_once_with(reuse=True)
    browser.refresh.assert_not_called()

    # both swap in place: no drawing, no page reload
    browser._drawButtons.reset_mock()
    browser._renderPage.reset_mock()
    browser._redraw_buttons_in_place.return_value = True
    browser._redraw_tree_in_place.return_value = True
    DeckBrowser.redraw_for_ui_mode(browser)
    browser._drawButtons.assert_not_called()
    browser._renderPage.assert_not_called()

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
        # this test is scoped to the bottom bar; the tree's own in-place
        # redraw (spec ui.simple-mode-deck-counts) is exercised separately
        browser._redraw_tree_in_place = MagicMock(return_value=True)  # type: ignore[method-assign]
        web._bridge_context = DeckBrowserBottomBar(browser)
        return browser, web

    browser, web = browser_with_bar(True)
    browser.redraw_for_ui_mode()
    web.stdHtml.assert_not_called()
    script = web.eval.call_args.args[0]
    assert ".deck-buttons" in script and 'pycmd(\\"import\\")' in script
    assert 'pycmd(\\"get_addons\\")' in script
    web.adjustHeightToFit.assert_called_once()

    # the bar shows another screen's buttons: it is drawn
    browser, web = browser_with_bar(False)
    web._bridge_context = object()
    browser.redraw_for_ui_mode()
    web.stdHtml.assert_called_once()
    assert 'pycmd("import")' not in web.stdHtml.call_args.args[0]
    assert 'pycmd("get_addons")' not in web.stdHtml.call_args.args[0]

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


def test_filtered_deck_failure_never_names_retrievability() -> None:
    """Pins spec/ui.md#ui.retrievability-advanced-only: the message can show
    in Simple mode, so it names the algorithm and not the word."""
    from aqt.operations.scheduling import _filtered_deck_preparation_failed_message

    for advanced in (False, True):
        col = cast(
            Any,
            SimpleNamespace(
                get_config_bool=lambda key, advanced=advanced: (
                    advanced and key == Config.Bool.ADVANCED_UI
                ),
            ),
        )
        message = _filtered_deck_preparation_failed_message(col)
        assert "retrievability" not in message.lower()
        assert "could not be prepared" in message


TOOLS_ADVANCED_ONLY = [
    "actionCreateFiltered",
    "actionFullDatabaseCheck",
    "actionCheckMediaDatabase",
    "actionEmptyCards",
    "actionNoteTypes",
]
TOOLS_SHARED = [
    "actionStudyDeck",
    "actionAdd_ons",
    "action_check_for_updates",
    "actionPreferences",
]


@pytest.fixture(scope="module")
def qapp() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from aqt.qt import QApplication

    return QApplication.instance() or QApplication([])


def _tools_window(
    qapp: Any, advanced: bool, choices: dict[str, bool] | None = None
) -> Any:
    """A window with the main window's real menus, synced to the mode."""
    import aqt.forms
    from aqt.qt import QMainWindow

    win: Any = QMainWindow()
    win.form = aqt.forms.main.Ui_MainWindow()
    win.form.setupUi(win)
    mode = {"advanced": advanced}
    win.mode = mode
    win.advanced_ui = lambda: mode["advanced"]
    win.col = SimpleNamespace(
        get_config=lambda key, default=None: choices if choices is not None else default
    )
    win._tools_menu_items = lambda: AnkiQt._tools_menu_items(win)
    AnkiQt._sync_tools_menu_for_ui_mode(win)
    return win


def _in_tools(win: Any, action: str) -> bool:
    return getattr(win.form, action) in win.form.menuTools.actions()


def test_tools_menu_hides_power_user_items_in_simple_mode(qapp: Any) -> None:
    """Pins spec/ui.md#ui.simple-mode-tools-hidden."""
    win = _tools_window(qapp, False)
    for action in TOOLS_ADVANCED_ONLY:
        assert not _in_tools(win, action), action


def test_tools_menu_shows_power_user_items_in_advanced_mode(qapp: Any) -> None:
    """Pins spec/ui.md#ui.simple-mode-tools-hidden."""
    win = _tools_window(qapp, True)
    for action in TOOLS_ADVANCED_ONLY + TOOLS_SHARED:
        assert _in_tools(win, action), action


def test_tools_menu_keeps_shared_items_in_both_modes(qapp: Any) -> None:
    """Study Deck..., Add-ons, Check for Updates and Preferences stay in the
    menu in both modes (spec ui.simple-mode-tools-hidden)."""
    for advanced in (False, True):
        win = _tools_window(qapp, advanced)
        for action in TOOLS_SHARED:
            assert _in_tools(win, action), action


def test_hidden_tools_items_keep_their_shortcuts(qapp: Any) -> None:
    """A hidden Tools item is taken out of the menu, not made invisible, and
    the window holds it, so its shortcut still runs it (spec
    ui.split-configurable)."""
    win = _tools_window(qapp, False)
    for name in TOOLS_ADVANCED_ONLY:
        action = getattr(win.form, name)
        assert action.isVisible(), name
        assert action in win.actions(), name


def test_switching_the_mode_puts_tools_items_back_in_place(qapp: Any) -> None:
    win = _tools_window(qapp, True)
    layout = list(win.form.menuTools.actions())
    win.mode["advanced"] = False
    AnkiQt._sync_tools_menu_for_ui_mode(win)
    win.mode["advanced"] = True
    AnkiQt._sync_tools_menu_for_ui_mode(win)
    assert win.form.menuTools.actions() == layout


def test_switching_the_mode_updates_the_tools_menu(qapp: Any) -> None:
    """Pins spec/ui.md#ui.simple-mode-tools-hidden."""
    # advanced_ui() must reflect the just-written mode, like the real
    # collection-backed one does, since _sync_tools_menu_for_ui_mode (below)
    # reads it again after set_config_bool runs.
    win = _tools_window(qapp, False)
    assert not _in_tools(win, "actionCreateFiltered")
    col = MagicMock()
    col.get_config.side_effect = lambda _key, default=None: default
    col.set_config_bool.side_effect = lambda _key, val: win.mode.update(advanced=val)
    mw = cast(
        Any,
        SimpleNamespace(
            col=col,
            state="deckBrowser",
            advanced_ui=win.advanced_ui,
            _sync_advanced_ui_action=lambda: None,
            toolbar=MagicMock(),
            deckBrowser=MagicMock(),
            reset=MagicMock(),
        ),
    )
    # real methods, not stubbed: this test checks what they actually do
    mw._sync_tools_menu_for_ui_mode = lambda: AnkiQt._sync_tools_menu_for_ui_mode(win)
    mw.redraw_for_ui_split = lambda: AnkiQt.redraw_for_ui_split(mw)
    AnkiQt.set_advanced_ui(mw, True)
    assert _in_tools(win, "actionCreateFiltered")


def test_switching_the_mode_on_the_overview_redraws_its_bottom_bar() -> None:
    """Pins spec/ui.md#ui.simple-mode-tools-hidden: the overview's Custom
    Study button depends on the mode the same way the deck list's bottom row
    does (spec ui.mode-switch), so it is redrawn in place too."""
    mw = cast(
        Any,
        SimpleNamespace(
            col=MagicMock(),
            state="overview",
            advanced_ui=lambda: False,
            _sync_advanced_ui_action=lambda: None,
            _sync_tools_menu_for_ui_mode=lambda: None,
            toolbar=MagicMock(),
            overview=MagicMock(),
            form=MagicMock(),
            reset=MagicMock(),
        ),
    )
    mw.redraw_for_ui_split = lambda: AnkiQt.redraw_for_ui_split(mw)
    AnkiQt.set_advanced_ui(mw, True)
    mw.overview.redraw_for_ui_mode.assert_called_once()
    mw.reset.assert_not_called()


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
