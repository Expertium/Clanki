# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/ui.md#ui.simple-mode-deck-counts and the overview part of
# spec/ui.md#ui.mode-switch.

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import anki.lang

# aqt.overview reads translated strings at import time
anki.lang.set_lang("en")

from aqt.overview import Overview  # noqa: E402


def _overview(
    *,
    advanced: bool,
    new_count: int = 0,
    learning_count: int = 0,
    review_count: int = 0,
    deck_new: int | None = None,
    deck_learn: int | None = None,
    deck_review: int | None = None,
) -> Any:
    deck_new = new_count if deck_new is None else deck_new
    deck_learn = learning_count if deck_learn is None else deck_learn
    deck_review = review_count if deck_review is None else deck_review
    return cast(
        Any,
        SimpleNamespace(
            mw=SimpleNamespace(
                advanced_ui=lambda: advanced,
                button=lambda *args, **kwargs: "",
                col=SimpleNamespace(
                    sched=SimpleNamespace(
                        counts=lambda: (new_count, learning_count, review_count)
                    ),
                    decks=SimpleNamespace(get_current_id=lambda: 1),
                    v3_scheduler=lambda: True,
                ),
            ),
            _rwkv_counts_pending=False,
        ),
    )


def _with_deck_counts(ov: Any, *, new: int, learn: int, review: int) -> Any:
    ov.mw.col.sched.deck_due_counts = lambda _did: SimpleNamespace(
        new_count=new, learn_count=learn, review_count=review
    )
    return ov


def test_overview_table_sums_learn_into_due_in_simple_mode() -> None:
    """Pins spec/ui.md#ui.simple-mode-deck-counts."""
    ov = _overview(advanced=False, new_count=3, learning_count=4, review_count=5)
    ov = _with_deck_counts(ov, new=3, learn=4, review=5)

    table = Overview._table(ov)

    assert 'class=learn-count' not in table
    assert '<span class=review-count>9</span>' in table
    assert '<span class=new-count>3</span>' in table


def test_overview_table_keeps_learn_and_due_separate_in_advanced_mode() -> None:
    """Pins spec/ui.md#ui.simple-mode-deck-counts."""
    ov = _overview(advanced=True, new_count=3, learning_count=4, review_count=5)
    ov = _with_deck_counts(ov, new=3, learn=4, review=5)

    table = Overview._table(ov)

    assert '<span class=learn-count>4</span>' in table
    assert '<span class=review-count>5</span>' in table


def test_overview_table_keeps_pending_rwkv_ellipsis_in_simple_mode() -> None:
    """A pending RWKV-Instant score cannot be summed with Learn yet, so
    Simple mode keeps showing the ellipsis (spec ui.simple-mode-deck-counts,
    sched.rwkv-instant-waits)."""
    ov = _overview(advanced=False, new_count=3, learning_count=4, review_count=5)
    ov = _with_deck_counts(ov, new=3, learn=4, review=5)
    ov._rwkv_counts_pending = True

    table = Overview._table(ov)

    assert '<span class=review-count>…</span>' in table


def test_overview_mode_redraw_repaints_the_counts_table() -> None:
    """Pins spec/ui.md#ui.simple-mode-deck-counts: switching the mode
    redraws the overview's counts table in place, the same cheap way the
    deck list's bottom row redraws (spec ui.mode-switch), without
    recomputing the due counts through a full refresh()."""
    render_page = MagicMock()
    ov = cast(Any, SimpleNamespace(_renderPage=render_page, refresh=MagicMock()))
    Overview.redraw_for_ui_mode(ov)
    render_page.assert_called_once()
    ov.refresh.assert_not_called()
