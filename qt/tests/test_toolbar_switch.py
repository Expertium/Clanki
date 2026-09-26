# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/ui.md#ui.toolbar-switch-one-frame: the toolbar's switch between
# the Study screen and the other screens is one frame, shown with the pages
# of the screen it belongs to, and the bottom bar changes height once.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import aqt.page_reveal as page_reveal_module
import aqt.toolbar as toolbar_module
from aqt.page_reveal import PageReveal, show_js
from aqt.toolbar import TopWebView


class _Reveal(PageReveal):
    def _restart_timer(self) -> None:
        pass


def _web() -> Any:
    page = SimpleNamespace(runJavaScript=MagicMock())
    return SimpleNamespace(
        page=lambda: page,
        eval=MagicMock(),
        setFixedHeight=MagicMock(),
        _onHeight=MagicMock(),
    )


def _load(reveal: PageReveal, web: Any) -> str:
    token = reveal.hold(web)
    reveal.load_started(web)
    return token


@pytest.fixture
def later(monkeypatch: pytest.MonkeyPatch) -> list:
    """QTimer.singleShot(0, fn) collects fn; the test runs it."""
    calls: list = []
    monkeypatch.setattr(
        page_reveal_module.QTimer, "singleShot", lambda ms, fn: calls.append(fn)
    )
    return calls


def test_an_expected_page_holds_the_others_until_it_is_drawn_and_ready() -> None:
    reveal = _Reveal()
    main, bottom = _web(), _web()
    reveal.expect(main)
    b = _load(reveal, bottom)
    reveal.page_ready(bottom, f"{b}:60")
    assert not bottom.page().runJavaScript.called

    m = _load(reveal, main)
    reveal.page_ready(main, f"{m}:700")
    bottom.page().runJavaScript.assert_called_once_with(show_js(b))
    main.page().runJavaScript.assert_called_once_with(show_js(m))


def test_an_expected_page_that_is_drawn_without_hold_ends_the_wait() -> None:
    reveal = _Reveal()
    main, bottom = _web(), _web()
    reveal.expect(main)
    b = _load(reveal, bottom)
    reveal.page_ready(bottom, b)

    reveal.load_started(main)  # the congratulations page, not held
    bottom.page().runJavaScript.assert_called_once_with(show_js(b))


def test_a_height_asked_while_the_bar_was_expected_is_taken_when_it_shows() -> None:
    reveal = _Reveal()
    main, bottom = _web(), _web()
    reveal.expect(main)
    reveal.expect(bottom)
    # the state change measures the bottom bar before the screen draws it
    assert reveal.fit_height_when_shown(bottom)
    b, m = _load(reveal, bottom), _load(reveal, main)
    reveal.page_ready(bottom, f"{b}:48")
    bottom._onHeight.assert_not_called()

    reveal.page_ready(main, f"{m}:700")
    bottom._onHeight.assert_called_once_with(48)


def test_changes_to_other_views_run_just_before_the_held_pages_show(later) -> None:
    reveal = _Reveal()
    main = _web()
    order: list[str] = []
    main.page().runJavaScript.side_effect = lambda js: order.append("page")
    m = _load(reveal, main)
    reveal.at_show(lambda: order.append("toolbar"))
    assert order == []

    reveal.page_ready(main, m)
    assert order == ["toolbar", "page"]


def test_a_change_made_before_the_screen_change_joins_it(later) -> None:
    # the Study screen's cleanup raises the toolbar before the deck list
    # expects its pages
    reveal = _Reveal()
    main = _web()
    ran: list[str] = []
    reveal.at_show(lambda: ran.append("elevate"))
    reveal.expect(main)
    later.pop()()
    assert ran == []

    m = _load(reveal, main)
    reveal.page_ready(main, m)
    assert ran == ["elevate"]


def test_a_change_with_no_screen_change_runs_at_the_end_of_the_event(later) -> None:
    reveal = _Reveal()
    ran: list[str] = []
    reveal.at_show(lambda: ran.append("flatten"))
    assert ran == []
    later.pop()()
    assert ran == ["flatten"]


def test_a_blocker_delays_the_show_until_it_ends() -> None:
    reveal = _Reveal()
    main = _web()
    m = _load(reveal, main)
    unblock = reveal.block()
    reveal.page_ready(main, m)
    assert not main.page().runJavaScript.called

    unblock()
    main.page().runJavaScript.assert_called_once_with(show_js(m))
    unblock()  # a second call does nothing


def _toolbar(monkeypatch: pytest.MonkeyPatch, reveal: PageReveal) -> Any:
    monkeypatch.setattr(toolbar_module, "page_reveal", lambda: reveal)
    view: Any = SimpleNamespace(eval=MagicMock(), _switch_js=None)
    view._switch_with_the_screen = lambda js: TopWebView._switch_with_the_screen(
        view, js
    )
    return view


@pytest.mark.parametrize("switch", ["flatten", "elevate"])
def test_the_toolbar_switches_with_the_screen_and_without_transition(
    monkeypatch: pytest.MonkeyPatch, later, switch: str
) -> None:
    reveal = _Reveal()
    main = _web()
    m = _load(reveal, main)
    view = _toolbar(monkeypatch, reveal)

    getattr(TopWebView, switch)(view)
    view.eval.assert_not_called()

    reveal.page_ready(main, m)
    script = view.eval.call_args.args[0]
    assert 'classList.add("instant")' in script
    assert 'classList.remove("instant")' in script
    assert script.index('"flat"') < script.index('classList.remove("instant")')


def test_the_toolbar_css_turns_its_transition_off_while_switching() -> None:
    from pathlib import Path

    scss = (
        Path(toolbar_module.__file__).parent / "data" / "web" / "css" / "toolbar.scss"
    ).read_text(encoding="utf8")
    assert ".fancy.instant & {\n        transition: none;" in scss


def test_the_first_cards_toolbar_background_comes_with_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reveal = _Reveal()
    main = _web()
    m = _load(reveal, main)
    view = _toolbar(monkeypatch, reveal)
    main.evalWithCallback = MagicMock()
    view.mw = SimpleNamespace(
        pm=SimpleNamespace(minimalist_mode=lambda: False),
        web=main,
    )
    main.height = lambda: 700
    view.web_height = 0
    view.set_body_height = MagicMock()

    TopWebView.update_background_image(view)
    reveal.page_ready(main, m)
    # the page waits for the toolbar's background
    assert not main.page().runJavaScript.called

    _script, callback = main.evalWithCallback.call_args.args
    callback("rgb(39, 40, 40) none repeat scroll 0% 0% / auto padding-box border-box")
    assert "rgb(39, 40, 40)" in view.eval.call_args.args[0]
    main.page().runJavaScript.assert_called_once_with(show_js(m))


def test_screens_expect_their_pages_when_they_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt import deckbrowser, overview

    for module, cls in (
        (deckbrowser, deckbrowser.DeckBrowser),
        (overview, overview.Overview),
    ):
        reveal = MagicMock()
        monkeypatch.setattr(module, "page_reveal", lambda reveal=reveal: reveal)
        screen: Any = SimpleNamespace(
            web=MagicMock(),
            bottom=SimpleNamespace(web=MagicMock()),
            mw=MagicMock(),
            _linkHandler=None,
            _refresh=lambda **kw: None,
            refresh=lambda: None,
            _shortcutKeys=lambda: [],
        )
        monkeypatch.setattr(module.av_player, "stop_and_clear_queue", lambda: None)
        cls.show(screen)
        expected = [call.args[0] for call in reveal.expect.call_args_list]
        assert expected == [screen.web, screen.bottom.web]
