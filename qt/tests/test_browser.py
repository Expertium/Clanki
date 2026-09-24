# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from typing import Any, cast

import pytest

import aqt.rwkv_scheduler
from anki.collection import SearchNode
from anki.lang import without_unicode_isolation
from aqt.browser.browser import Browser
from aqt.utils import tr


class SearchRecorder:
    def __init__(self) -> None:
        self.search: str | None = None
        self.prompt: str | None = None

    def __call__(self, search: str, prompt: str | None = None) -> None:
        self.search = search
        self.prompt = prompt


class Col:
    def __init__(self, default_search: str) -> None:
        self.default_search = default_search

    def get_config_string(self, _key: Any) -> str:
        return self.default_search

    def build_search_string(self, node: SearchNode) -> str:
        assert node.deck == "current"
        return "deck:current"


def test_default_browser_search_shows_current_deck_scope() -> None:
    browser = cast(Any, Browser.__new__(Browser))
    browser.col = Col(default_search="")
    search_for = SearchRecorder()
    browser.search_for = search_for

    browser._default_search()

    assert search_for.search == "deck:current"
    assert search_for.prompt == "deck:current"


def test_configured_default_browser_search_is_shown_unchanged() -> None:
    browser = cast(Any, Browser.__new__(Browser))
    browser.col = Col(default_search="is:due")
    search_for = SearchRecorder()
    browser.search_for = search_for

    browser._default_search()

    assert search_for.search == "is:due"
    assert search_for.prompt == "is:due"


class TableSearchRecorder:
    def __init__(self) -> None:
        self.searches: list[str] = []

    def search(self, search: str) -> None:
        self.searches.append(search)

    def sorts_by_retrievability(self) -> bool:
        return False


class ImmediateQueryOp:
    def __init__(self, *, parent: Any, op: Any, success: Any) -> None:
        self.op = op
        self.success = success

    def with_progress(self) -> ImmediateQueryOp:
        raise AssertionError("a scored Browser search must not block the window")

    def run_in_background(self) -> None:
        self.success(self.op(None))


def _scored_search_browser(query: str) -> Any:
    """A Browser showing some rows, about to run a search that asks RWKV for
    its values."""
    from types import SimpleNamespace

    browser = cast(Any, Browser.__new__(Browser))
    browser.shots = []
    browser.titles = []
    browser.mw = SimpleNamespace(
        progress=SimpleNamespace(
            single_shot=lambda delay, callback: browser.shots.append((delay, callback))
        )
    )
    browser.table = TableSearchRecorder()
    browser._lastSearchTxt = query
    browser._rwkv_search_generation = 0
    browser._closeEventHasCleanedUp = False
    browser.setWindowTitle = browser.titles.append
    return browser


@pytest.mark.parametrize(
    "query",
    ["prop:rwkv:r<0.95", "prop:rwkv-curve:r<0.95"],
)
def test_rwkv_browser_search_prepares_scores_before_searching(
    monkeypatch: Any,
    query: str,
) -> None:
    from aqt.browser import browser as browser_module

    browser = _scored_search_browser(query)
    prepared: list[tuple[str, float | None]] = []

    monkeypatch.setattr(browser_module, "QueryOp", ImmediateQueryOp)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_browser_retrievability_scores",
        lambda _mw, search, warmup_wait_secs=None, **_kwargs: prepared.append(
            (search, warmup_wait_secs)
        ),
    )

    browser.search()

    # the scores are prepared first, and the preparation never holds the
    # collection waiting for the RWKV warm-up
    assert prepared == [(query, 0.0)]
    assert browser.table.searches == [query]


def test_rwkv_browser_search_waits_without_blocking_the_window(
    monkeypatch: Any,
) -> None:
    """Pins spec/ui.md#ui.browser-rwkv-search-does-not-block"""
    from aqt.browser import browser as browser_module
    from aqt.rwkv_scheduler import RwkvStatsPreparationStatus

    query = "prop:rwkv:r<0.95"
    browser = _scored_search_browser(query)
    status = [RwkvStatsPreparationStatus.PENDING]

    monkeypatch.setattr(browser_module, "QueryOp", ImmediateQueryOp)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_browser_retrievability_scores",
        lambda _mw, search, warmup_wait_secs=None, **_kwargs: status[0],
    )

    browser.search()

    # RWKV is still loading its state: no rows are replaced, the title says
    # what the window waits for, and it asks again later
    assert browser.table.searches == []
    assert browser.titles == [
        without_unicode_isolation(
            tr.browsing_rwkv_scores_pending(
                algorithm=tr.deck_config_scheduler_choice_rwkv_instant()
            )
        )
    ]
    assert [delay for delay, _ in browser.shots] == [
        browser_module.RWKV_SCORED_SEARCH_RETRY_MS
    ]

    status[0] = RwkvStatsPreparationStatus.READY
    browser.shots[-1][1]()

    assert browser.table.searches == [query]
    assert len(browser.shots) == 1

    # another search while it waits cancels the one before it
    browser.shots.clear()
    status[0] = RwkvStatsPreparationStatus.PENDING
    browser.search()
    assert len(browser.shots) == 1
    browser._lastSearchTxt = "prop:rwkv:r<0.5"
    browser._rwkv_search_generation += 1
    browser.shots[-1][1]()
    assert len(browser.shots) == 1


def test_rwkv_browser_search_stops_waiting_after_the_time_limit(
    monkeypatch: Any,
) -> None:
    """Pins spec/ui.md#ui.browser-rwkv-search-does-not-block"""
    from aqt.browser import browser as browser_module
    from aqt.rwkv_scheduler import RwkvStatsPreparationStatus

    query = "prop:rwkv:r<0.95"
    browser = _scored_search_browser(query)
    clock = [1000.0]

    monkeypatch.setattr(browser_module, "QueryOp", ImmediateQueryOp)
    monkeypatch.setattr(browser_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_browser_retrievability_scores",
        lambda _mw, search, warmup_wait_secs=None, **_kwargs: (
            RwkvStatsPreparationStatus.PENDING
        ),
    )

    browser.search()
    assert browser.table.searches == []

    clock[0] += browser_module.RWKV_SCORED_SEARCH_WAIT_SECS
    browser.shots[-1][1]()

    # the wait ends: the search runs with the scores RWKV has
    assert browser.table.searches == [query]
    assert len(browser.shots) == 1


def test_non_rwkv_browser_search_runs_without_preparation(monkeypatch: Any) -> None:
    browser = cast(Any, Browser.__new__(Browser))
    browser.mw = object()
    browser.table = TableSearchRecorder()
    browser._lastSearchTxt = "prop:r<0.95"
    browser._rwkv_search_generation = 0
    prepared: list[str] = []

    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_browser_retrievability_scores",
        lambda _mw, search, warmup_wait_secs=None, **_kwargs: prepared.append(search),
    )

    browser.search()

    assert prepared == []
    assert browser.table.searches == ["prop:r<0.95"]


# Pins spec/ui.md#ui.rwkv-curve-stored-s90: under RWKV-Curve a `prop:s`
# search gets the stored curves' S90s before it runs; under FSRS-7 it runs
# at once.
@pytest.mark.parametrize("algorithm, prepares", [("rwkvCurve", True), ("fsrs7", False)])
def test_a_stability_search_prepares_rwkv_curves_s90s(
    monkeypatch: Any, algorithm: str, prepares: bool
) -> None:
    from types import SimpleNamespace

    from aqt.browser import browser as browser_module

    query = "prop:s>10"
    browser = _scored_search_browser(query)
    browser.col = SimpleNamespace(get_config=lambda key, default=None: algorithm)
    prepared: list[str] = []
    monkeypatch.setattr(browser_module, "QueryOp", ImmediateQueryOp)
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_browser_retrievability_scores",
        lambda _mw, search, **_kwargs: prepared.append(search),
    )

    browser.search()

    assert prepared == ([query] if prepares else [])
    assert browser.table.searches == [query]


class SortedTableSearchRecorder(TableSearchRecorder):
    def sorts_by_retrievability(self) -> bool:
        return True


@pytest.mark.parametrize(
    "algorithm, prepares",
    [("rwkvCurve", True), ("rwkvInstant", True), ("fsrs7", False)],
)
def test_sorting_by_retrievability_under_rwkv_prepares_the_algorithms_scores(
    monkeypatch: Any, algorithm: str, prepares: bool
) -> None:
    """Pins spec/ui.md#ui.browser-memory-columns: the Retrievability sort
    reads the collection's own algorithm's R, which RWKV prepares first."""
    from types import SimpleNamespace

    from aqt.browser import browser as browser_module

    browser = _scored_search_browser("deck:current")
    browser.table = SortedTableSearchRecorder()
    browser.col = SimpleNamespace(get_config=lambda key, default=None: algorithm)
    prepared: list[tuple[str, bool]] = []

    monkeypatch.setattr(browser_module, "QueryOp", ImmediateQueryOp)
    monkeypatch.setattr(
        aqt.rwkv_scheduler, "rwkv_algorithm_name", lambda col: "RWKV-Curve"
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "prepare_browser_retrievability_scores",
        lambda _mw, search, warmup_wait_secs=None, for_sort=False: prepared.append(
            (search, for_sort)
        ),
    )

    browser.search()

    assert prepared == ([("deck:current", True)] if prepares else [])
    assert browser.table.searches == ["deck:current"]
