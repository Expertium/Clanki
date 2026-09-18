# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/ui.md#ui.simple-mode-tools-hidden (the overview's Custom Study
# button) and the redraw-in-place part of spec/ui.md#ui.mode-switch.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import anki.lang

# aqt.overview reads translated strings at import time
anki.lang.set_lang("en")

from aqt.overview import Overview  # noqa: E402


def _overview(*, advanced: bool, dyn: bool = False, buried: bool = False) -> Any:
    draws: list[str] = []
    ov = cast(
        Any,
        SimpleNamespace(
            mw=SimpleNamespace(
                advanced_ui=lambda: advanced,
                col=SimpleNamespace(
                    decks=SimpleNamespace(current=lambda: {"dyn": dyn}),
                    sched=SimpleNamespace(have_buried=lambda: buried),
                ),
            ),
            bottom=SimpleNamespace(
                draw=lambda buf="", **_kwargs: draws.append(buf)
            ),
            _linkHandler=lambda _url: None,
        ),
    )
    ov._draws = draws
    return ov


def test_overview_bottom_bar_hides_custom_study_in_simple_mode() -> None:
    """Pins spec/ui.md#ui.simple-mode-tools-hidden."""
    ov = _overview(advanced=False)
    Overview._renderBottom(ov)
    assert 'pycmd("studymore")' not in ov._draws[0]
    assert 'pycmd("opts")' in ov._draws[0]


def test_overview_bottom_bar_shows_custom_study_in_advanced_mode() -> None:
    """Pins spec/ui.md#ui.simple-mode-tools-hidden."""
    ov = _overview(advanced=True)
    Overview._renderBottom(ov)
    assert 'pycmd("studymore")' in ov._draws[0]


def test_overview_bottom_bar_description_and_options_stay_in_both_modes() -> None:
    for advanced in (False, True):
        ov = _overview(advanced=advanced)
        Overview._renderBottom(ov)
        assert 'pycmd("opts")' in ov._draws[0]
        assert 'pycmd("description")' in ov._draws[0]


def test_overview_mode_redraw_only_draws_the_bottom_bar() -> None:
    """Pins spec/ui.md#ui.simple-mode-tools-hidden: switching the mode
    redraws the overview's bottom bar in place, the same cheap way the deck
    list's bottom row does (spec ui.mode-switch), without recomputing the due
    counts."""
    render_bottom = MagicMock()
    ov = cast(
        Any,
        SimpleNamespace(_renderBottom=render_bottom, refresh=MagicMock()),
    )
    Overview.redraw_for_ui_mode(ov)
    render_bottom.assert_called_once()
    ov.refresh.assert_not_called()
