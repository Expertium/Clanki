# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The Browser's Retrievability and Stability cells under RWKV (spec
ui.browser-memory-columns).

The backend fills those cells with FSRS-7's values only. Under RWKV-Curve and
RWKV-Instant the values live in RWKV's own state, so the table asks for them
here: for the rows it draws, in the background, a batch at a time. A value is
redrawn when it is no longer fresh (spec sched.rwkv-r-freshness): after any
change of the collection (a review, an undo, a reset, a deck move), at the
start of a new day, or when its time tolerance has passed. There is no timer:
a stale value is recomputed when its row is drawn again, and its old text
stays in the cell until the new one arrives.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from aqt import rwkv_scheduler
from aqt.browser.table import CellRow
from aqt.operations import QueryOp

logger = logging.getLogger(__name__)

# A value younger than this is never recomputed on a redraw, so the redraw its
# own arrival causes does not start another round (a card reviewed under ten
# minutes ago has no tolerance at all).
MIN_REFRESH_SECONDS = 1.0
# the rows one background round computes at most
BATCH_SIZE = 200
# a round RWKV could not answer (state still loading, or busy) is retried
RETRY_MS = 2000

RETRIEVABILITY = "retrievability"
STABILITY = "stability"
# the elapsed time a missing value counts as: the longest tolerance applies
NO_VALUE_ELAPSED = 10**9


@dataclass(frozen=True)
class _Value:
    retrievability: str
    stability: str
    elapsed_seconds: int
    computed_at: float
    generation: int
    next_day_at: int


def _format_retrievability(value: float | None) -> str:
    # as the backend formats FSRS-7's R
    return "" if value is None else f"{value * 100:.0f}%"


def _format_stability(col: Any, days: float | None) -> str:
    # as the backend formats FSRS-7's stability (`time_span`, not precise)
    return "" if days is None else col.format_timespan(days * 86_400)


class RwkvColumnValues:
    """Fills the Retrievability and Stability cells of a card-mode table
    from RWKV. `changed` gets the card ids whose cells have new text."""

    def __init__(
        self,
        mw: Any,
        changed: Callable[[Sequence[int]], None],
        compute: Callable[[Any, Sequence[int]], dict[int, Any] | None] | None = None,
        run_in_background: Callable[[Callable[[Any], Any], Callable[[Any], None]], None]
        | None = None,
    ) -> None:
        self._mw = mw
        self._changed = changed
        self._compute = compute or rwkv_scheduler.rwkv_browser_values
        self._run_in_background = run_in_background or self._query_op
        self._values: dict[int, _Value] = {}
        self._wanted: dict[int, None] = {}
        self._running = False
        self._retry_scheduled = False
        self._closed = False
        # bumped by every change of the collection: a value computed before
        # it is stale
        self._generation = 0
        self.algorithm = "fsrs7"

    def refresh_algorithm(self) -> None:
        """Read the collection's algorithm again (after a search or a
        change of the columns)."""
        algorithm = rwkv_scheduler.collection_algorithm(self._mw.col)
        if algorithm != self.algorithm:
            self._values.clear()
            self._wanted.clear()
        self.algorithm = algorithm

    @property
    def active(self) -> bool:
        return self.algorithm != "fsrs7"

    def collection_changed(self) -> None:
        """Any change of the collection (a review, an undo, a reset, a deck
        move) makes every value stale; they are recomputed as their rows
        are drawn."""
        self._generation += 1

    def close(self) -> None:
        self._closed = True
        self._values.clear()
        self._wanted.clear()

    def fill(
        self,
        card_id: int,
        row: CellRow,
        retrievability: int | None,
        stability: int | None,
    ) -> None:
        """Put the card's RWKV text into the row's cells (column indices, or
        None when the column is not shown), and ask for a fresh value if the
        one it has is missing or stale."""
        value = self._values.get(card_id)
        if value is not None:
            if retrievability is not None:
                row.cells[retrievability].text = value.retrievability
            if stability is not None:
                row.cells[stability].text = (
                    value.stability if self.algorithm == "rwkvCurve" else ""
                )
        if value is None or self._stale(value):
            self._want(card_id)

    def _stale(self, value: _Value, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if value.generation != self._generation or now >= value.next_day_at:
            return True
        age = now - value.computed_at
        if age < MIN_REFRESH_SECONDS:
            return False
        return age > rwkv_scheduler.rwkv_r_time_tolerance_seconds(
            value.elapsed_seconds + age
        )

    def _want(self, card_id: int) -> None:
        self._wanted[card_id] = None
        if not self._running and not self._retry_scheduled:
            self._running = True
            self._mw.progress.single_shot(0, self._start)

    def _start(self) -> None:
        if self._closed or not self._wanted:
            self._running = False
            return
        card_ids = list(self._wanted)[:BATCH_SIZE]
        for card_id in card_ids:
            self._wanted.pop(card_id, None)
        generation = self._generation
        algorithm = self.algorithm
        started = time.time()

        def compute(col: Any) -> tuple[dict[int, _Value] | None, int]:
            next_day_at = col.sched._timing_today().next_day_at
            values = self._compute(self._mw, card_ids)
            if values is None:
                return None, next_day_at
            formatted = {}
            for card_id in card_ids:
                value = values.get(card_id)
                formatted[card_id] = _Value(
                    retrievability=_format_retrievability(
                        value.retrievability if value is not None else None
                    ),
                    stability=_format_stability(
                        col, value.s90 if value is not None else None
                    ),
                    # a card RWKV has no value for keeps its blank cells
                    # until the collection changes
                    elapsed_seconds=(
                        value.elapsed_seconds if value is not None else NO_VALUE_ELAPSED
                    ),
                    computed_at=started,
                    generation=generation,
                    next_day_at=next_day_at,
                )
            return formatted, next_day_at

        def done(result: tuple[dict[int, _Value] | None, int]) -> None:
            values, _next_day_at = result
            self._running = False
            if self._closed:
                return
            if values is None:
                # RWKV is not ready: keep the rows waiting and ask again
                for card_id in card_ids:
                    self._wanted.setdefault(card_id, None)
                self._retry_later()
                return
            if algorithm == self.algorithm:
                self._values.update(values)
                self._changed(card_ids)
            if self._wanted:
                self._running = True
                self._mw.progress.single_shot(0, self._start)

        self._run_in_background(compute, done)

    def _query_op(
        self, op: Callable[[Any], Any], success: Callable[[Any], None]
    ) -> None:
        def failed(exc: Exception) -> None:
            self._running = False
            logger.error("RWKV Browser values failed: %s", exc)

        QueryOp(parent=self._mw, op=op, success=success).failure(
            failed
        ).run_in_background()

    def _retry_later(self) -> None:
        if self._retry_scheduled:
            return
        self._retry_scheduled = True

        def retry() -> None:
            self._retry_scheduled = False
            if self._wanted and not self._running and not self._closed:
                self._running = True
                self._start()

        self._mw.progress.single_shot(RETRY_MS, retry)
