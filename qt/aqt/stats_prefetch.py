# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The Stats window starts its page's first graphs request while the page
loads.

A new Stats window spends a few hundred milliseconds starting its web page
(the renderer, the scripts, the translations, the preferences) before the
page asks for its graphs, and the backend then computes them. The window
knows that first request (the page's search and period, and the graphs of
the collection's UI mode), so it has the media server compute it as the
window opens; when the page asks, it gets that result. The result is used
only for the identical request, while nothing has been written to the
collection since it started, on the same day and within a few seconds;
otherwise the request is computed as before.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass

from anki.stats_pb2 import GraphsRequest

# the page's first request (ts/routes/graphs/+page.svelte: initialSearch,
# initialDays and simpleData)
PAGE_SEARCH = "deck:current"
PAGE_DAYS = 365
SIMPLE_GRAPHS = (
    GraphsRequest.REVIEWS,
    GraphsRequest.CARD_COUNTS,
    GraphsRequest.TRUE_RETENTION,
)
_MAX_AGE_SECS = 10.0

Output = tuple[bytes, dict[str, str]]


@dataclass(frozen=True)
class _Key:
    search: str
    days: int
    graphs: tuple[int, ...]
    rwkv_retrievability_later: bool

    @classmethod
    def of(cls, request: GraphsRequest) -> _Key:
        return cls(
            request.search,
            request.days,
            tuple(request.graphs),
            request.rwkv_retrievability_later,
        )


@dataclass
class _Prefetch:
    key: _Key
    col_mod: int
    today: int
    started: float
    result: Future[Output]


_lock = threading.Lock()
_prefetch: _Prefetch | None = None


def first_page_request(advanced_ui: bool) -> GraphsRequest:
    """The graphs request the Stats page sends first (WithGraphData)."""
    graphs = [] if advanced_ui else list(SIMPLE_GRAPHS)
    return GraphsRequest(
        search=PAGE_SEARCH,
        days=PAGE_DAYS,
        graphs=graphs,
        rwkv_retrievability_later=not graphs,
    )


def start(
    request: GraphsRequest,
    collection_state: tuple[int, int],
    compute: Callable[[GraphsRequest], Output],
    run_in_background: Callable[[Callable[[], None]], object],
) -> None:
    """Compute `request` in the background, for the page to take.
    `collection_state`: the collection's modification time and day now."""
    global _prefetch
    col_mod, today = collection_state
    result: Future[Output] = Future()
    prefetch = _Prefetch(_Key.of(request), col_mod, today, time.monotonic(), result)
    with _lock:
        _prefetch = prefetch

    def task() -> None:
        try:
            result.set_result(compute(request))
        except BaseException as exc:  # noqa: BLE001  (handed to the taker)
            result.set_exception(exc)

    run_in_background(task)


def take(
    request: GraphsRequest, collection_state: Callable[[], tuple[int, int]]
) -> Output | None:
    """The prefetched output for `request` (waiting for it if still
    computing), or None when there is none that may be used.
    `collection_state` gives the collection's modification time and day."""
    global _prefetch
    with _lock:
        prefetch, _prefetch = _prefetch, None
    if (
        prefetch is None
        or prefetch.key != _Key.of(request)
        or time.monotonic() - prefetch.started > _MAX_AGE_SECS
    ):
        return None
    try:
        output = prefetch.result.result()
    except Exception:
        return None
    # anything written since it started, or a new day: compute again
    if collection_state() != (prefetch.col_mod, prefetch.today):
        return None
    return output


def forget() -> None:
    global _prefetch
    with _lock:
        _prefetch = None
