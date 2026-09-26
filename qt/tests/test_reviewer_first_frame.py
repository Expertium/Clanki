# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/reviewer.md#review.first-card-one-frame: Study Now draws the
# reviewer page and its bottom bar hidden, and shows both, the bottom bar
# with its Show Answer button, once the first card is in.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import aqt.reviewer as reviewer_module
from aqt.page_reveal import PageReveal, show_js
from aqt.reviewer import Reviewer


class _Reveal(PageReveal):
    def _restart_timer(self) -> None:
        pass


def _web() -> Any:
    page = SimpleNamespace(runJavaScript=MagicMock())
    return SimpleNamespace(page=lambda: page, update=MagicMock(), repaint=MagicMock())


def _shown(web: Any, token: str) -> bool:
    return any(
        call.args == (show_js(token),)
        for call in web.page().runJavaScript.call_args_list
    )


def _reviewer(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _Reveal, str, str]:
    reveal = _Reveal()
    monkeypatch.setattr(reviewer_module, "page_reveal", lambda: reveal)
    reviewer: Any = Reviewer.__new__(Reviewer)
    reviewer.web = _web()
    reviewer.bottom = SimpleNamespace(web=_web())
    reviewer.state = "question"
    reviewer.card = SimpleNamespace(id=123)
    reviewer._question_update_id = 12
    reviewer._question_rendered = False
    reviewer._qa_transition_active = True
    reviewer.mw = SimpleNamespace(web=SimpleNamespace(setFocus=lambda: None))
    reviewer._showAnswerButton = MagicMock()
    reviewer._auto_advance_to_answer_if_enabled = lambda: None
    reviewer._run_after_question_shown_callbacks = lambda: None
    # the two pages _initWeb() drew, as stdHtml(held=True) registers them
    main = reveal.hold(reviewer.web)
    reveal.wait_for_signal(reviewer.web)
    reveal.load_started(reviewer.web)
    bottom = reveal.hold(reviewer.bottom.web)
    reveal.load_started(reviewer.bottom.web)
    reveal.page_ready(reviewer.bottom.web, f"{bottom}:64")
    reveal.page_ready(reviewer.web, f"{main}:700")
    return reviewer, reveal, main, bottom


def test_the_first_card_gets_its_answer_button_before_both_pages_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer, reveal, main, bottom = _reviewer(monkeypatch)
    # the DOM of both pages is done, but the card is not in yet
    assert not _shown(reviewer.web, main) and not _shown(reviewer.bottom.web, bottom)

    reviewer._linkHandler("qaHeld:question:12:123")

    reviewer._showAnswerButton.assert_called_once()
    assert _shown(reviewer.web, main) and _shown(reviewer.bottom.web, bottom)

    # once the card is presented, the button is not drawn a second time
    reviewer._linkHandler("qaPresented:question:12:123")
    assert reviewer._question_rendered is True
    reviewer._showAnswerButton.assert_called_once()


def test_a_later_card_draws_its_answer_button_when_presented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer, reveal, main, bottom = _reviewer(monkeypatch)
    reviewer._linkHandler("qaHeld:question:12:123")
    reviewer._question_update_id = 13
    reviewer._question_rendered = False

    reviewer._linkHandler("qaPresented:question:13:123")

    assert reviewer._showAnswerButton.call_count == 2


def test_a_stale_first_card_only_shows_the_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer, reveal, main, bottom = _reviewer(monkeypatch)

    reviewer._linkHandler("qaHeld:question:11:123")

    reviewer._showAnswerButton.assert_not_called()
    assert _shown(reviewer.web, main) and _shown(reviewer.bottom.web, bottom)


def test_study_now_draws_the_page_and_the_bottom_bar_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reveal = MagicMock()
    monkeypatch.setattr(reviewer_module, "page_reveal", lambda: reveal)
    reviewer: Any = Reviewer.__new__(Reviewer)
    reviewer.web = MagicMock()
    reviewer.bottom = SimpleNamespace(web=MagicMock())
    reviewer.revHtml = lambda: "<div id=qa></div>"
    reviewer._bottomHTML = lambda: "<center></center>"

    reviewer._initWeb()

    assert reviewer.web.stdHtml.call_args.kwargs["held"] is True
    assert reviewer.bottom.web.stdHtml.call_args.kwargs["held"] is True
    reveal.wait_for_signal.assert_called_once_with(reviewer.web)
