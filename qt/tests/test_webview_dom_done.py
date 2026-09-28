# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""spec/ui.md#ui.late-dom-done-ignored: a domDone counts only for the page
the web view is loading now."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aqt.webview import AnkiWebView, page_load_number


def _view(expected_load: str | None) -> Any:
    """A web view waiting for a page, with one action queued for it (the
    reviewer's first question)."""
    ran: list[str] = []
    view = SimpleNamespace(
        _shouldIgnoreWebEvent=lambda: False,
        _filterSet=True,
        _domDone=False,
        _expected_load=expected_load,
        _pendingActions=[("eval", ("_showQuestion()",))],
        ran=ran,
    )

    def run_actions() -> None:
        while view._pendingActions and view._domDone:
            name, args = view._pendingActions.pop(0)
            ran.append(args[0])

    view._maybeRunActions = run_actions
    return view


def test_a_late_dom_done_of_the_page_before_does_not_run_the_new_pages_actions() -> (
    None
):
    """The deck list's page (load 7) said domDone after the review screen had
    started loading its own page (load 8). Its question then ran on the deck
    list's page ("_showQuestion is not defined"), and the review page's own
    domDone found nothing left to run: the review screen stayed blank."""
    view = _view(expected_load="8")

    AnkiWebView._onBridgeCmd(view, "domDone:7")

    assert not view._domDone and view.ran == []

    AnkiWebView._onBridgeCmd(view, "domDone:8")

    assert view._domDone and view.ran == ["_showQuestion()"]


def test_a_plain_dom_done_counts_only_for_a_page_without_a_load_number() -> None:
    # a page of its own URL (Stats, deck options) sends a plain domDone
    view = _view(expected_load=None)
    AnkiWebView._onBridgeCmd(view, "domDone")
    assert view.ran == ["_showQuestion()"]

    # a plain domDone is from another page than a numbered one being loaded
    view = _view(expected_load="3")
    AnkiWebView._onBridgeCmd(view, "domDone")
    assert view.ran == []

    # and a numbered one from another page than one without a number
    view = _view(expected_load=None)
    AnkiWebView._onBridgeCmd(view, "domDone:3")
    assert view.ran == []


def test_the_load_number_is_read_from_the_page_url() -> None:
    assert page_load_number("id=2235975453104&load=12") == "12"
    assert page_load_number("load=5&id=1") == "5"
    assert page_load_number("id=2235975453104") is None
    assert page_load_number("id=1&reload=4") is None
    assert page_load_number("") is None
