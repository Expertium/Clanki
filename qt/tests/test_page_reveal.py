# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/ui.md#ui.screen-one-frame: the pages of one screen change are
# drawn hidden and shown together, once each is ready.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import aqt
from aqt.page_reveal import (
    HOLD_ATTRIBUTE,
    HOLD_CLASS,
    HOLD_CSS,
    READY_COMMAND,
    READY_JS,
    PageReveal,
    show_js,
)


class _Reveal(PageReveal):
    """No Qt timer: the test fires the timeout itself."""

    def __init__(self) -> None:
        super().__init__()
        self.timer_starts = 0

    def _restart_timer(self) -> None:
        self.timer_starts += 1


def _web() -> Any:
    page = SimpleNamespace(runJavaScript=MagicMock())
    return SimpleNamespace(
        page=lambda: page, eval=MagicMock(), setFixedHeight=MagicMock()
    )


def _shown(web: Any, token: str) -> bool:
    return any(
        call.args == (show_js(token),)
        for call in web.page().runJavaScript.call_args_list
    )


def _load(reveal: PageReveal, web: Any, *, signal: bool = False) -> str:
    """A held page drawn into `web`, as stdHtml() does it."""
    token = reveal.hold(web)
    if signal:
        reveal.wait_for_signal(web)
    reveal.load_started(web)
    return token


def test_held_pages_are_shown_together_once_each_dom_is_done() -> None:
    reveal = _Reveal()
    main, bottom = _web(), _web()
    m, b = _load(reveal, main), _load(reveal, bottom)

    reveal.page_ready(bottom, b)
    assert not _shown(bottom, b) and not _shown(main, m)

    reveal.page_ready(main, m)
    assert _shown(main, m) and _shown(bottom, b)
    assert not reveal.is_held(main) and not reveal.is_held(bottom)


def test_a_late_message_from_the_previous_page_does_not_show_the_new_one() -> None:
    reveal = _Reveal()
    main, bottom = _web(), _web()
    old = _load(reveal, main)
    new = _load(reveal, main)  # drawn again before the first page was ready
    b = _load(reveal, bottom)
    reveal.page_ready(bottom, b)

    reveal.page_ready(main, old)
    assert not _shown(main, new) and not _shown(bottom, b)

    reveal.page_ready(main, new)
    assert _shown(main, new) and _shown(bottom, b)


def test_a_page_that_waits_for_its_signal_is_not_ready_at_dom_done() -> None:
    reveal = _Reveal()
    main, bottom = _web(), _web()
    m = _load(reveal, main, signal=True)
    b = _load(reveal, bottom)
    reveal.page_ready(bottom, b)
    reveal.page_ready(main, m)
    assert not _shown(main, m) and not _shown(bottom, b)

    reveal.mark_ready(main)
    assert _shown(main, m) and _shown(bottom, b)


def test_a_page_loaded_again_without_a_hold_stops_holding_the_others() -> None:
    reveal = _Reveal()
    main, bottom = _web(), _web()
    m, b = _load(reveal, main), _load(reveal, bottom)
    reveal.page_ready(bottom, b)

    reveal.load_started(main)  # a page that is not held
    assert _shown(bottom, b)
    assert not _shown(main, m)
    assert not reveal.is_held(main)


def test_the_timeout_shows_what_is_ready_and_the_rest_when_it_is() -> None:
    reveal = _Reveal()
    main, bottom = _web(), _web()
    m, b = _load(reveal, main), _load(reveal, bottom)
    assert reveal.timer_starts == 2
    reveal.page_ready(bottom, b)

    reveal.show_all()
    assert _shown(bottom, b) and not _shown(main, m)

    reveal.page_ready(main, m)
    assert _shown(main, m)


def test_a_held_bottom_bar_takes_its_new_height_when_it_is_shown() -> None:
    reveal = _Reveal()
    main, bottom = _web(), _web()
    m, b = _load(reveal, main), _load(reveal, bottom)
    assert reveal.fit_height_when_shown(bottom)
    reveal.page_ready(bottom, f"{b}:64")
    bottom.setFixedHeight.assert_not_called()

    reveal.page_ready(main, f"{m}:700")
    bottom.setFixedHeight.assert_called_once_with(64)
    # the main view is not resized: nothing asked for its height
    main.setFixedHeight.assert_not_called()
    # a web view that holds nothing measures its page as before
    assert not reveal.fit_height_when_shown(bottom)


def test_a_page_that_is_not_held_is_left_alone() -> None:
    reveal = _Reveal()
    web = _web()
    reveal.page_ready(web, "h1")
    reveal.mark_ready(web)
    reveal.load_started(web)
    web.page().runJavaScript.assert_not_called()
    web.eval.assert_not_called()


def test_the_hold_hides_without_removing_the_layout_and_ends_by_itself() -> None:
    # opacity, not display: scripts that measure the page (the heatmap's
    # calendar) still see its layout; the animation shows the page if
    # Python never does
    assert f"html.{HOLD_CLASS}" in HOLD_CSS
    assert "opacity: 0" in HOLD_CSS
    assert "display: none" not in HOLD_CSS
    assert "animation" in HOLD_CSS
    js = show_js("h7")
    assert f'classList.remove("{HOLD_CLASS}")' in js
    assert "'h7'" in js and "clanki-shown" in js
    assert READY_COMMAND in READY_JS


def _std_html_page(monkeypatch: pytest.MonkeyPatch, *, held: bool) -> tuple[str, Any]:
    from aqt import webview

    monkeypatch.setattr(
        aqt,
        "mw",
        SimpleNamespace(
            baseHTML=lambda: "<base>",
            pm=MagicMock(),
        ),
    )
    reveal = _Reveal()
    monkeypatch.setattr("aqt.page_reveal._reveal", reveal)
    # setHtml() starts the load, as the real one does through load_url()
    set_html = MagicMock(side_effect=lambda *args: reveal.load_started(view))
    view = SimpleNamespace(
        title="t",
        bundledCSS=lambda name: f"<link {name}>",
        bundledScript=lambda name: f"<script {name}>",
        standard_css=lambda: "",
        setHtml=set_html,
    )
    webview.AnkiWebView.stdHtml(view, "<p>body</p>", context=None, held=held)  # type: ignore[arg-type]
    return view.setHtml.call_args.args[0], (view, reveal)


def test_a_held_page_carries_the_class_and_its_style_and_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html, (view, reveal) = _std_html_page(monkeypatch, held=True)
    root = html.split("<html", 1)[1].split(">", 1)[0]
    assert HOLD_CLASS in root
    assert f'{HOLD_ATTRIBUTE}="h1"' in root
    assert HOLD_CSS in html
    assert reveal.is_held(view)


def test_a_page_drawn_without_hold_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html, (view, reveal) = _std_html_page(monkeypatch, held=False)
    assert HOLD_CLASS not in html and HOLD_ATTRIBUTE not in html
    assert not reveal.is_held(view)


def test_the_overview_is_drawn_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    import anki.lang

    anki.lang.set_lang("en")
    from aqt import gui_hooks
    from aqt.overview import Overview

    monkeypatch.setattr(gui_hooks.overview_will_render_content, "_hooks", [])
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

    assert web.stdHtml.call_args.kwargs["held"] is True


def test_the_bottom_bar_is_drawn_hidden() -> None:
    from aqt.toolbar import BottomBar

    web = MagicMock()
    bar: Any = SimpleNamespace(web=web, _centerBody=BottomBar._centerBody)
    BottomBar.draw(
        bar, buf="<button>", link_handler=lambda url: None, web_context=object()
    )

    assert web.stdHtml.call_args.kwargs["held"] is True
