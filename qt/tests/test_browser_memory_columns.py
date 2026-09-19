# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The Browser's Retrievability and Stability columns under RWKV (spec
ui.browser-memory-columns) and the freshness of RWKV's R (spec
sched.rwkv-r-freshness)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from aqt import rwkv_scheduler
from aqt.browser.table import rwkv_values
from aqt.browser.table.rwkv_values import RwkvColumnValues
from aqt.rwkv_scheduler import RwkvBrowserValue

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR


# Pins spec/scheduling.md#sched.rwkv-r-freshness
@pytest.mark.parametrize(
    "elapsed, tolerance",
    [
        (0, 0.0),
        (10 * MINUTE - 1, 0.0),
        (10 * MINUTE, 60.0),
        (HOUR - 1, 60.0),
        (HOUR, 600.0),
        (DAY - 1, 600.0),
        (DAY, 3600.0),
        (400 * DAY, 3600.0),
    ],
)
def test_the_time_tolerance_follows_the_time_since_the_last_review(
    elapsed: int, tolerance: float
) -> None:
    assert rwkv_scheduler.rwkv_r_time_tolerance_seconds(elapsed) == tolerance


# The table side
######################################################################


class Cell:
    def __init__(self, text: str) -> None:
        self.text = text


class Row:
    def __init__(self) -> None:
        # Sort Field, Retrievability, Stability; the backend leaves the RWKV
        # cells blank
        self.cells = (Cell("front"), Cell(""), Cell(""))


class Harness:
    def __init__(self, algorithm: str) -> None:
        self.algorithm = algorithm
        self.timers: list[tuple[int, Callable[[], None]]] = []
        self.computed: list[list[int]] = []
        self.changed: list[list[int]] = []
        self.results: dict[int, RwkvBrowserValue] | None = {}
        self.col = SimpleNamespace(
            get_config=lambda key, default=None: self.algorithm,
            sched=SimpleNamespace(
                _timing_today=lambda: SimpleNamespace(next_day_at=self.next_day_at)
            ),
            format_timespan=lambda seconds: f"{seconds / DAY:.0f} days",
        )
        self.next_day_at = 2_000_000
        mw = SimpleNamespace(
            col=self.col,
            progress=SimpleNamespace(
                single_shot=lambda ms, func: self.timers.append((ms, func))
            ),
        )
        self.values = RwkvColumnValues(
            mw,
            lambda card_ids: self.changed.append(list(card_ids)),
            compute=self.compute,
            run_in_background=lambda op, success: success(op(self.col)),
        )
        self.values.refresh_algorithm()

    def compute(self, _mw: Any, card_ids: Any) -> dict[int, RwkvBrowserValue] | None:
        self.computed.append(list(card_ids))
        if self.results is None:
            return None
        return {cid: v for cid, v in self.results.items() if cid in card_ids}

    def run_timers(self, limit: int = 20) -> list[int]:
        delays = []
        while self.timers and len(delays) < limit:
            ms, func = self.timers.pop(0)
            delays.append(ms)
            func()
        return delays

    def fill(self, card_id: int) -> Row:
        row = Row()
        self.values.fill(card_id, row, 1, 2)  # type: ignore[arg-type]
        return row


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1_000_000.0]
    monkeypatch.setattr(rwkv_values.time, "time", lambda: now[0])
    return now


# Pins spec/ui.md#ui.browser-memory-columns
def test_fsrs7_collections_leave_the_backends_cells_alone(clock: list[float]) -> None:
    harness = Harness("fsrs7")
    assert not harness.values.active


# Pins spec/ui.md#ui.browser-memory-columns
def test_rwkv_curve_rows_get_the_stored_curves_r_and_s90_in_the_background(
    clock: list[float],
) -> None:
    harness = Harness("rwkvCurve")
    harness.results = {
        1: RwkvBrowserValue(retrievability=0.864, s90=318.0, elapsed_seconds=40 * DAY)
    }
    # the first draw asks for the value and shows nothing yet
    row = harness.fill(1)
    assert [cell.text for cell in row.cells] == ["front", "", ""]
    harness.run_timers()
    assert harness.computed == [[1]]
    assert harness.changed == [[1]]
    row = harness.fill(1)
    assert [cell.text for cell in row.cells] == ["front", "86%", "318 days"]
    # fresh: drawing it again computes nothing
    harness.run_timers()
    assert harness.computed == [[1]]


# Pins spec/ui.md#ui.browser-memory-columns
def test_rwkv_instant_rows_get_the_rating_heads_r_and_no_stability(
    clock: list[float],
) -> None:
    harness = Harness("rwkvInstant")
    harness.results = {
        1: RwkvBrowserValue(retrievability=0.5, s90=None, elapsed_seconds=40 * DAY)
    }
    harness.fill(1)
    harness.run_timers()
    row = harness.fill(1)
    assert [cell.text for cell in row.cells] == ["front", "50%", ""]


# Pins spec/ui.md#ui.browser-memory-columns (no other algorithm stands in)
def test_a_card_rwkv_has_no_value_for_stays_blank(clock: list[float]) -> None:
    harness = Harness("rwkvCurve")
    harness.results = {}
    harness.fill(1)
    harness.run_timers()
    assert [cell.text for cell in harness.fill(1).cells] == ["front", "", ""]
    # and it is not asked for again on every draw
    clock[0] += 5 * MINUTE
    harness.fill(1)
    harness.run_timers()
    assert harness.computed == [[1]]


# Pins spec/scheduling.md#sched.rwkv-r-freshness
def test_a_change_of_the_collection_makes_every_value_stale(clock: list[float]) -> None:
    harness = Harness("rwkvInstant")
    harness.results = {
        1: RwkvBrowserValue(retrievability=0.5, s90=None, elapsed_seconds=40 * DAY)
    }
    harness.fill(1)
    harness.run_timers()
    harness.values.collection_changed()
    harness.results[1] = replace(harness.results[1], retrievability=0.4)
    # the old text stays until the new value arrives
    assert harness.fill(1).cells[1].text == "50%"
    harness.run_timers()
    assert harness.computed == [[1], [1]]
    assert harness.fill(1).cells[1].text == "40%"


# Pins spec/scheduling.md#sched.rwkv-r-freshness
@pytest.mark.parametrize(
    "elapsed, fresh_for",
    [(5 * MINUTE, 1.0), (30 * MINUTE, 60.0), (5 * HOUR, 600.0), (3 * DAY, 3600.0)],
)
def test_a_value_is_recomputed_once_its_time_tolerance_has_passed(
    clock: list[float], elapsed: int, fresh_for: float
) -> None:
    harness = Harness("rwkvInstant")
    harness.results = {
        1: RwkvBrowserValue(retrievability=0.5, s90=None, elapsed_seconds=elapsed)
    }
    harness.fill(1)
    harness.run_timers()
    # a card reviewed under ten minutes ago is still not recomputed by the
    # draw its own value causes
    clock[0] += fresh_for - 0.5
    harness.fill(1)
    harness.run_timers()
    assert harness.computed == [[1]]
    clock[0] += 1.0
    harness.fill(1)
    harness.run_timers()
    assert harness.computed == [[1], [1]]


# Pins spec/scheduling.md#sched.rwkv-r-freshness
def test_a_new_day_makes_every_value_stale(clock: list[float]) -> None:
    harness = Harness("rwkvInstant")
    harness.next_day_at = int(clock[0]) + 10
    harness.results = {
        1: RwkvBrowserValue(retrievability=0.5, s90=None, elapsed_seconds=40 * DAY)
    }
    harness.fill(1)
    harness.run_timers()
    clock[0] += 11
    harness.fill(1)
    harness.run_timers()
    assert harness.computed == [[1], [1]]


def test_rwkv_not_ready_asks_again_later(clock: list[float]) -> None:
    harness = Harness("rwkvInstant")
    harness.results = None
    harness.fill(1)
    # the first round, then a retry every RETRY_MS while RWKV is not ready
    assert harness.run_timers(limit=3) == [
        0,
        rwkv_values.RETRY_MS,
        rwkv_values.RETRY_MS,
    ]
    assert harness.computed == [[1], [1], [1]]
    # once RWKV answers, the retries stop
    harness.results = {
        1: RwkvBrowserValue(retrievability=0.5, s90=None, elapsed_seconds=40 * DAY)
    }
    harness.run_timers()
    assert harness.timers == []
    assert harness.fill(1).cells[1].text == "50%"


def test_a_switch_of_algorithm_drops_the_values(clock: list[float]) -> None:
    harness = Harness("rwkvInstant")
    harness.results = {
        1: RwkvBrowserValue(retrievability=0.5, s90=None, elapsed_seconds=40 * DAY)
    }
    harness.fill(1)
    harness.run_timers()
    harness.algorithm = "rwkvCurve"
    harness.values.refresh_algorithm()
    assert harness.fill(1).cells[1].text == ""


# The RWKV side
######################################################################


def _input(card_id: int, elapsed_seconds: int) -> Any:
    return SimpleNamespace(
        current_elapsed_seconds=elapsed_seconds, current_elapsed_days=None
    )


@pytest.fixture
def rwkv_ready(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(searches=[], curve_calls=[], scored=[])
    backend = SimpleNamespace(
        card_curve=lambda card_id, days: (
            state.curve_calls.append((card_id, days)) or ([0.8], 12.5)
        )
    )
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", backend)
    monkeypatch.setattr(
        rwkv_scheduler, "_prepare_reviewer_backend_for_card_info", lambda r: True
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_capture_reviewer_backend_prediction_state_token",
        lambda reviewer: "token",
    )
    build = SimpleNamespace(
        inputs_by_batch_size={512: [(1, _input(1, 2 * DAY)), (2, _input(2, 3 * DAY))]}
    )

    def batches(**kwargs: Any) -> Any:
        state.searches.append(kwargs["search"])
        return build

    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_review_input_batches_for_search", batches
    )
    monkeypatch.setattr(
        rwkv_scheduler, "_rwkv_curve_enabled_input_build", lambda r, b: b
    )

    @contextmanager
    def access(**_kwargs: Any) -> Iterator[object]:
        yield backend

    monkeypatch.setattr(
        rwkv_scheduler, "_try_reviewer_backend_prediction_access", access
    )

    def scores(inputs: Any, *, batch_size: int, state_token: Any) -> Any:
        state.scored.append([card_id for card_id, _ in inputs])
        return [(card_id, 0.3) for card_id, _ in inputs]

    monkeypatch.setattr(rwkv_scheduler, "_rwkv_review_scores_for_inputs", scores)
    return state


def _mw(algorithm: str) -> Any:
    return SimpleNamespace(
        col=SimpleNamespace(get_config=lambda key, default=None: algorithm)
    )


# Pins spec/ui.md#ui.browser-memory-columns
def test_rwkv_curve_values_are_the_stored_curve_at_the_elapsed_time_and_its_s90(
    rwkv_ready: SimpleNamespace,
) -> None:
    values = rwkv_scheduler.rwkv_browser_values(_mw("rwkvCurve"), [1, 2])
    assert rwkv_ready.searches == ["cid:1,2"]
    assert rwkv_ready.curve_calls == [(1, [2.0]), (2, [3.0])]
    assert values == {
        1: RwkvBrowserValue(retrievability=0.8, s90=12.5, elapsed_seconds=2 * DAY),
        2: RwkvBrowserValue(retrievability=0.8, s90=12.5, elapsed_seconds=3 * DAY),
    }
    assert rwkv_ready.scored == []


# Pins spec/ui.md#ui.browser-memory-columns
def test_rwkv_instant_values_are_the_rating_heads_r_without_stability(
    rwkv_ready: SimpleNamespace,
) -> None:
    values = rwkv_scheduler.rwkv_browser_values(_mw("rwkvInstant"), [1, 2])
    assert rwkv_ready.scored == [[1, 2]]
    assert rwkv_ready.curve_calls == []
    assert values == {
        1: RwkvBrowserValue(retrievability=0.3, s90=None, elapsed_seconds=2 * DAY),
        2: RwkvBrowserValue(retrievability=0.3, s90=None, elapsed_seconds=3 * DAY),
    }


def test_fsrs7_and_a_missing_model_give_no_rwkv_values(
    rwkv_ready: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert rwkv_scheduler.rwkv_browser_values(_mw("fsrs7"), [1]) == {}
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)
    monkeypatch.setattr(
        rwkv_scheduler, "configure_reviewer_backend_from_environment", lambda: False
    )
    assert rwkv_scheduler.rwkv_browser_values(_mw("rwkvInstant"), [1]) == {}


def test_rwkv_not_ready_gives_none_so_the_table_asks_again(
    rwkv_ready: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rwkv_scheduler, "_prepare_reviewer_backend_for_card_info", lambda r: False
    )
    assert rwkv_scheduler.rwkv_browser_values(_mw("rwkvInstant"), [1]) is None


# Pins spec/ui.md#ui.browser-memory-columns (the backend's reading)
@pytest.mark.parametrize(
    "key, default_preset, algorithm",
    [
        ("rwkvInstant", None, "rwkvInstant"),
        (
            None,
            {"rwkvReviewEnabled": False, "rwkvReviewInstantOrderEnabled": False},
            "fsrs7",
        ),
        (None, {"rwkvReviewEnabled": True}, "rwkvCurve"),
        (None, None, "rwkvCurve"),
    ],
)
def test_the_collection_algorithm_is_read_as_the_backend_reads_it(
    key: str | None, default_preset: dict | None, algorithm: str
) -> None:
    col = SimpleNamespace(
        get_config=lambda k, default=None: key,
        decks=SimpleNamespace(get_config=lambda conf_id: default_preset),
    )
    assert rwkv_scheduler.collection_algorithm(col) == algorithm
