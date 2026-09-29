# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# The deck list, a deck's overview and their bottom bar are drawn into the
# page open in their web view instead of loading a new page, where that page
# has the same frame (aqt.webview, stdHtml(into_open_page=True)); the screen
# still changes in one step (spec ui.screen-one-frame), and every other case
# loads a new page as before.

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import aqt
from aqt import gui_hooks, webview
from aqt.page_reveal import HOLD_ATTRIBUTE, PageReveal, show_js


class _Reveal(PageReveal):
    """No Qt timers: the test fires them itself."""

    def _restart_timer(self) -> None:
        pass

    def _show_in_turn(self, entries: list, then: Any) -> None:
        if not entries:
            then()
            return
        self._show(entries[0], lambda: self._show_in_turn(entries[1:], then))


class _Page:
    def __init__(self) -> None:
        self.scripts: list[tuple[str, Any]] = []

    def runJavaScript(self, js: str, callback: Any = None) -> None:
        self.scripts.append((js, callback))

    def answer(self, prefix: str, value: Any) -> None:
        """Call back the last script that starts with `prefix`."""
        js, callback = next(
            s for s in reversed(self.scripts) if s[0].startswith(prefix)
        )
        callback(value)


class _View:
    """The parts of AnkiWebView that draw a page, on a fake page."""

    stdHtml = webview.AnkiWebView.stdHtml
    _stage_into_open_page = webview.AnkiWebView._stage_into_open_page
    drawing_into_open_page = webview.AnkiWebView.drawing_into_open_page
    _load_instead = webview.AnkiWebView._load_instead
    _on_page_staged = webview.AnkiWebView._on_page_staged
    show_staged_page = webview.AnkiWebView.show_staged_page
    _maybeRunActions = webview.AnkiWebView._maybeRunActions
    _queueAction = webview.AnkiWebView._queueAction
    _open_page = None
    _staged_page = None
    _content_serial = 0

    def __init__(self, reveal: PageReveal) -> None:
        self.reveal = reveal
        self.title = "t"
        self._page = _Page()
        self._domDone = True
        self._pendingActions: list = []
        self.loads: list[str] = []
        self.evals: list[str] = []
        self.allow_drops = True

    def page(self) -> _Page:
        return self._page

    def webBundlePath(self, name: str) -> str:
        return f"http://s/_anki/{name}"

    def bundledCSS(self, name: str) -> str:
        return f'<link rel="stylesheet" href="{self.webBundlePath(name)}">'

    def bundledScript(self, name: str) -> str:
        return f'<script src="{self.webBundlePath(name)}"></script>'

    def standard_css(self) -> str:
        return ""

    def setHtml(self, html: str, context: Any = None) -> None:
        self._pendingActions = []
        self._domDone = True
        self._setHtml(html, context)

    def _setHtml(self, html: str, context: Any) -> None:
        # load_url(): a load forgets the open page
        self._content_serial += 1
        self._open_page = None
        self._staged_page = None
        self.reveal.load_started(self)
        self._domDone = False
        self.loads.append(html)

    def _evalWithCallback(self, js: str, cb: Any) -> None:
        self.evals.append(js)

    def eval(self, js: str) -> None:
        self._queueAction("eval", js, None)

    def set_open_links_externally(self, value: bool) -> None:
        pass

    def show(self) -> None:
        pass

    def update(self) -> None:
        pass

    def _onHeight(self, height: int) -> None:
        self.height = height

    def loaded(self) -> None:
        """The loaded page's DOM is done."""
        self._domDone = True
        self._maybeRunActions()


@pytest.fixture
def reveal(monkeypatch: pytest.MonkeyPatch) -> _Reveal:
    reveal = _Reveal()
    monkeypatch.setattr("aqt.page_reveal._reveal", reveal)
    monkeypatch.setattr(
        aqt,
        "mw",
        SimpleNamespace(
            baseHTML=lambda: "<base>",
            mediaServer=SimpleNamespace(set_page_html=MagicMock()),
            pm=MagicMock(),
        ),
        raising=False,
    )
    monkeypatch.setattr(webview.sip, "isdeleted", lambda obj: False)
    monkeypatch.setattr(gui_hooks.webview_will_set_content, "_hooks", [])
    return reveal


def _draw(view: _View, body: str, css: str = "css/deckbrowser.css", **kw: Any) -> None:
    view.stdHtml(body, css=[css], context=None, held=True, into_open_page=True, **kw)


def _open(view: _View, reveal: _Reveal) -> None:
    """The first page: loaded, and shown."""
    _draw(view, "<p>one</p>")
    assert len(view.loads) == 1
    token = view.loads[0].split(f'{HOLD_ATTRIBUTE}="')[1].split('"')[0]
    view.loaded()
    reveal.page_ready(view, f"{token}:10")


def test_the_first_page_is_loaded_with_its_screen_sheets_and_frame_scripts_marked(
    reveal: _Reveal,
) -> None:
    view = _View(reveal)
    _open(view, reveal)
    html = view.loads[0]
    assert (
        '<link data-clanki-screen-css rel="stylesheet" href="http://s/_anki/css/deckbrowser.css">'
        in html
    )
    assert (
        '<script data-clanki-frame src="http://s/_anki/js/webview.js"></script>' in html
    )
    assert view._open_page is not None


def test_a_page_with_the_same_frame_is_staged_in_the_open_page_and_shown_with_the_others(
    reveal: _Reveal,
) -> None:
    view, other = _View(reveal), _View(reveal)
    _open(view, reveal)
    other_token = reveal.hold(other)
    reveal.load_started(other)

    _draw(view, "<p>two</p>", css="css/overview.css")
    # not loaded: staged in the open page, with the new screen sheet
    assert len(view.loads) == 1
    stage, _ = view.page().scripts[-1]
    args = json.loads(
        "[" + stage.split("clankiStagePage(")[1].rsplit("), true)", 1)[0] + "]"
    )
    token, body, css, scripts = args
    assert body == "<p>two</p>"
    assert css == ["http://s/_anki/css/overview.css"]
    assert scripts == []
    # actions queued now wait for the page
    view.eval("counts()")
    assert view.evals == []

    view._on_page_staged(f"{token}:20")
    assert not any(js.startswith("clankiShowPage") for js, _ in view.page().scripts)
    reveal.page_ready(other, f"{other_token}:5")
    # the drawn page first, with the queued action in the same task; the
    # loaded page once it is shown
    show, _ = view.page().scripts[-1]
    assert show == f'clankiShowPage("{token}") && (clankiRunAll(["counts()"]), true)'
    assert not any(js == show_js(other_token) for js, _ in other.page().scripts)
    view.page().answer("clankiShowPage", True)
    assert any(js == show_js(other_token) for js, _ in other.page().scripts)
    assert view._domDone and view._staged_page is None
    assert view._open_page.css == ("http://s/_anki/css/overview.css",)


def test_the_content_serial_changes_for_a_staged_page_and_for_a_loaded_one(
    reveal: _Reveal,
) -> None:
    """A screen that drew a page knows from the content serial whether the
    page on screen is still its own. A page drawn into the open page keeps the
    load number, so the serial has to change for it too."""
    view = _View(reveal)
    _open(view, reveal)
    loaded = view._content_serial

    _draw(view, "<p>two</p>", css="css/overview.css")
    assert len(view.loads) == 1
    staged = view._content_serial
    assert staged != loaded

    # a page that cannot be staged (it is still being drawn) is loaded
    _draw(view, "<p>three</p>", css="css/other.css")
    assert view._content_serial not in (loaded, staged)


def test_another_frame_or_mixed_screen_sheets_or_an_add_on_load_a_new_page(
    reveal: _Reveal, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _View(reveal)
    _open(view, reveal)
    view.title = "another"
    _draw(view, "<p>two</p>")
    assert len(view.loads) == 2

    view = _View(reveal)
    _open(view, reveal)
    view.stdHtml(
        "<p>two</p>",
        css=["css/deckbrowser.css", "css/extra.css"],
        held=True,
        into_open_page=True,
    )
    assert len(view.loads) == 2

    view = _View(reveal)
    _open(view, reveal)
    monkeypatch.setattr(
        gui_hooks.webview_will_set_content, "_hooks", [lambda content, context: None]
    )
    _draw(view, "<p>two</p>")
    assert len(view.loads) == 2
    assert "data-clanki-screen-css" not in view.loads[1]


def test_a_page_that_cannot_be_staged_is_loaded_still_held(reveal: _Reveal) -> None:
    view = _View(reveal)
    _open(view, reveal)
    _draw(view, "<p>two</p>")
    token = json.loads(
        "["
        + view.page()
        .scripts[-1][0]
        .split("clankiStagePage(")[1]
        .rsplit("), true)", 1)[0]
        + "]"
    )[0]
    view.eval("counts()")
    view.page().answer("typeof clankiStagePage", False)
    assert len(view.loads) == 2
    assert f'{HOLD_ATTRIBUTE}="{token}"' in view.loads[1]
    assert reveal.is_held(view)
    # the queued action runs in the loaded page
    view.loaded()
    assert view.evals == ["counts()"]
    reveal.page_ready(view, f"{token}:10")
    assert any(js == show_js(token) for js, _ in view.page().scripts)


def test_a_page_not_staged_by_the_timeout_is_loaded(reveal: _Reveal) -> None:
    view = _View(reveal)
    _open(view, reveal)
    _draw(view, "<p>two</p>")
    reveal.show_all()
    assert len(view.loads) == 2
    token = view.loads[1].split(f'{HOLD_ATTRIBUTE}="')[1].split('"')[0]
    # a late answer of the staging is ignored; the loaded page shows when done
    view._on_page_staged(f"{token}:20")
    assert not any(js.startswith("clankiShowPage") for js, _ in view.page().scripts)
    view.loaded()
    reveal.page_ready(view, f"{token}:10")
    assert any(js == show_js(token) for js, _ in view.page().scripts)


def test_a_page_that_lost_the_staged_content_is_loaded_again(reveal: _Reveal) -> None:
    view = _View(reveal)
    _open(view, reveal)
    _draw(view, "<p>two</p>")
    token = json.loads(
        "["
        + view.page()
        .scripts[-1][0]
        .split("clankiStagePage(")[1]
        .rsplit("), true)", 1)[0]
        + "]"
    )[0]
    view.eval("counts()")
    view._on_page_staged(f"{token}:20")
    view.page().answer("clankiShowPage", False)
    assert len(view.loads) == 2
    assert "<p>two</p>" in view.loads[1]
    assert reveal.is_held(view)
    view.loaded()
    assert view.evals == ["counts()"]


def test_the_deck_list_and_overview_load_a_new_page_for_an_add_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anki.lang

    anki.lang.set_lang("en")
    from aqt.overview import Overview

    def draw() -> bool:
        web = MagicMock()
        ov: Any = SimpleNamespace(
            mw=SimpleNamespace(
                col=SimpleNamespace(
                    decks=SimpleNamespace(current=lambda: {"name": "Deck", "dyn": 0}),
                    sched=SimpleNamespace(_is_finished=lambda: False),
                )
            ),
            web=web,
            _rwkv_counts_pending=False,
            _desc=lambda deck: "",
            _table=lambda: "<table></table>",
            _rwkv_pending_notice=lambda: "",
            _body=Overview._body,
        )
        Overview._renderPage(ov)
        return web.stdHtml.call_args.kwargs["into_open_page"]

    monkeypatch.setattr(gui_hooks.overview_will_render_content, "_hooks", [])
    monkeypatch.setattr(gui_hooks.overview_did_refresh, "_hooks", [])
    assert draw() is True
    monkeypatch.setattr(
        gui_hooks.overview_did_refresh, "_hooks", [lambda overview: None]
    )
    assert draw() is False


def test_a_page_that_only_shares_its_frame_is_loaded_and_can_be_drawn_into(
    reveal: _Reveal,
) -> None:
    # the deck list: loaded, marked, and the overview drawn into it
    view = _View(reveal)
    view.stdHtml(
        "<p>decks</p>", css=["css/deckbrowser.css"], held=True, shares_frame=True
    )
    assert len(view.loads) == 1 and "data-clanki-screen-css" in view.loads[0]
    token = view.loads[0].split(f'{HOLD_ATTRIBUTE}="')[1].split('"')[0]
    view.loaded()
    reveal.page_ready(view, f"{token}:10")
    view.stdHtml(
        "<p>decks</p>", css=["css/deckbrowser.css"], held=True, shares_frame=True
    )
    assert len(view.loads) == 2
    view.loaded()
    _draw(view, "<p>deck</p>", css="css/overview.css")
    assert len(view.loads) == 2
    assert view.drawing_into_open_page()


def test_the_bottom_bar_is_drawn_into_its_open_page_with_the_main_page() -> None:
    from aqt.toolbar import BottomBar

    for main_drawn in (True, False):
        web = MagicMock()
        mw = MagicMock()
        mw.web.drawing_into_open_page.return_value = main_drawn
        bar: Any = SimpleNamespace(web=web, _centerBody=BottomBar._centerBody, mw=mw)
        BottomBar.draw(
            bar, buf="<button>", link_handler=lambda url: None, web_context=object()
        )
        assert web.stdHtml.call_args.kwargs["shares_frame"] is True
        assert web.stdHtml.call_args.kwargs["into_open_page"] is main_drawn
