# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The Stats page's Advanced graphs, computed while the page shows Simple
mode.

In Simple mode the page asks the backend only for the data its four graphs
draw. A click on "Advanced" then asks for every graph, and that request reads
every searched card, which takes about a second on a large collection: the
page stands still until it answers. The page's Simple request tells us that
the page is open in Simple mode, so the media server computes the Advanced
request in the background as soon as it has served the Simple one. When the
page asks, it gets that result.

The result is used only for the identical request (search, period, graph
list, the RWKV-later flag), once, within a few minutes, and only while
nothing has been written to the collection and the day has not changed since
the prefetch started; anything else is computed as before. The one write that
does not end it is the UI-mode flag itself (`ui_mode_written`): the switch
writes it as it asks for the graphs, and no graph reads it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from anki.stats_pb2 import GraphsRequest

# an older result is computed again: under FSRS-7 the Retrievability graph's
# R is of the moment the prefetch ran
_MAX_AGE_SECS = 300.0

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


def advanced_request(request: GraphsRequest) -> GraphsRequest | None:
    """The request the page sends when the user switches this one's page to
    Advanced mode, or None when this request is not a Simple-mode one.

    A Simple-mode page names the graphs it wants (`WithGraphData`); the
    switch then asks for every graph, leaving the RWKV scores of the
    Retrievability graph for a request of its own. A request for
    Retrievability alone is that later request, not a Simple-mode one.
    """
    graphs = list(request.graphs)
    if not graphs or graphs == [GraphsRequest.RETRIEVABILITY]:
        return None
    return GraphsRequest(
        search=request.search,
        days=request.days,
        graphs=[],
        rwkv_retrievability_later=True,
    )


def start(
    request: GraphsRequest,
    collection_state: tuple[int, int],
    compute: Callable[[GraphsRequest], Output],
    run_in_background: Callable[[Callable[[], None]], object],
) -> None:
    """Compute `request` in the background, for the page to take.

    Does nothing when the same request is already prefetched or running.
    `collection_state`: the collection's modification time and day now.
    """
    global _prefetch
    col_mod, today = collection_state
    key = _Key.of(request)
    result: Future[Output] = Future()
    with _lock:
        if _prefetch is not None and _prefetch.key == key:
            return
        _prefetch = _Prefetch(key, col_mod, today, time.monotonic(), result)

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

    `collection_state` gives the collection's modification time and day.
    """
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


@contextmanager
def ui_mode_write(col: Any) -> Iterator[None]:
    """Write the collection's UI-mode flag in this block, without ending a
    prefetch.

    The page's Simple | Advanced switch writes the flag as it asks for its
    Advanced graphs, and the write changes the collection's modification
    time. No graph reads the flag (the graphs handler puts the flag of the
    moment it serves into the response), so the new modification time becomes
    the one the prefetch expects. The lock is held across the write, so a
    request cannot read the new time before this.
    """
    with _lock:
        yield
        if _prefetch is not None:
            _prefetch.col_mod = col.mod


def clear() -> None:
    """Forget the prefetched output; the Stats window calls this when it
    closes."""
    global _prefetch
    with _lock:
        _prefetch = None
