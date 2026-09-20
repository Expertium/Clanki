# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins spec/ui.md#ui.no-waiting-windows."""

from __future__ import annotations

import os
from typing import Any

import pytest


@pytest.fixture(scope="module")
def app() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from aqt.qt import QApplication

    return QApplication.instance() or QApplication([])


def test_the_click_path_operations_open_no_waiting_window(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expanding a deck, clicking a deck and answering a card run without
    the "Processing..." window, however long they wait."""
    import aqt
    from anki.decks import DeckCollapseScope, DeckId
    from anki.scheduler.v3 import CardAnswer
    from aqt.operations.deck import set_current_deck, set_deck_collapsed
    from aqt.operations.scheduling import answer_card
    from aqt.qt import QWidget

    parent = QWidget()
    windowed: list[object] = []
    windowless: list[object] = []
    mw = type(
        "MW",
        (),
        {
            "taskman": type(
                "T",
                (),
                {
                    "with_progress": lambda self, *a, **k: windowed.append(a),
                    "run_in_background": lambda self, *a, **k: windowless.append(a),
                    "with_backend_progress": lambda self, *a, **k: windowed.append(a),
                },
            )(),
            "_increase_background_ops": lambda self: None,
            "_decrease_background_ops": lambda self: None,
            "col": None,
        },
    )()
    monkeypatch.setattr(aqt, "mw", mw, raising=False)

    set_deck_collapsed(
        parent=parent,
        deck_id=DeckId(1),
        collapsed=True,
        scope=DeckCollapseScope.REVIEWER,
    ).run_in_background()
    set_current_deck(parent=parent, deck_id=DeckId(1)).run_in_background()
    answer_card(parent=parent, answer=CardAnswer()).run_in_background()

    assert windowed == [], "a click of the review path opened a waiting window"
    assert len(windowless) == 3
