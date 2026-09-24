# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Total Knowledge's replay, built by the backend, runs the day loop exactly
as the replay built in Python does (spec ui.stats-total-knowledge).

The backend (`TotalKnowledgeRwkvReplay`) hands over each day's reviews as
packed warm-up rows and the searched cards' ratings and resets; Python used
to build both from RWKV's review inputs. Run on the same collection, the two
must warm the model up on the same bytes, score the same rows, sum the same
values and keep the same day sums under the same digests.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import aqt.rwkv_scheduler
from anki.collection import Collection
from aqt import total_knowledge
from aqt.rwkv_srs_benchmark import (
    _PACKED_PREDICTION_REQUEST_ROW,
    _packed_review_input_row,
)
from tests.test_rwkv_replay_inputs_backend import DAY_MS, _build


class RecordingRuntime:
    """Records every warm-up as packed bytes, and answers from them, so a
    different warm-up or a different row changes the sums."""

    def __init__(self) -> None:
        self.warm_ups: list[bytes] = []
        self.spans: list[list[tuple[int, int, int, int]]] = []
        self.payloads: list[tuple[int, bytes]] = []

    def warm_up_reviews_in_place(self, reviews: Sequence[Any]) -> None:
        self.warm_ups.append(b"".join(_packed_review_input_row(r) for r in reviews))

    def warm_up_packed_rows_in_place(self, rows: bytes, count: int) -> None:
        assert len(rows) == count * _PACKED_PREDICTION_REQUEST_ROW.size
        self.warm_ups.append(bytes(rows))

    def _seen(self) -> float:
        return sum(len(warm_up) for warm_up in self.warm_ups) / 1e6

    def curve_retrievability_day_sums_from_warm_up(
        self, spans: Sequence[tuple[int, int, int, int]]
    ) -> tuple[int, list[float]]:
        self.spans.append(list(spans))
        first = min(span[2] for span in spans)
        last = max(span[3] for span in spans)
        sums = [0.0] * (last - first + 1)
        for card_id, review_day, first_day, last_day in spans:
            for day in range(first_day, last_day + 1):
                sums[day - first] += 0.9 ** (day - review_day) * (
                    1 + (card_id % 7) / 10 + self._seen()
                )
        return first, sums


@pytest.fixture
def col(tmp_path: Path) -> Iterator[Collection]:
    col = _build(tmp_path / "total-knowledge.anki2")
    # resets: of searched and unsearched cards, before and after ratings
    card_ids = sorted(col.db.list("select id from cards"))
    first_review = 1_600_000_000_000
    for n, card_id in enumerate(card_ids[::3]):
        col.db.execute(
            "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, "
            "type) values (?, ?, -1, 0, 0, 0, 0, 0, 4)",
            first_review + (n % 9) * 2 * DAY_MS + 555 + n,
            card_id,
        )
    try:
        yield col
    finally:
        col.close()


def _run(
    col: Collection, monkeypatch: pytest.MonkeyPatch, curve: bool, backend: bool
) -> tuple[total_knowledge._Job, RecordingRuntime, dict[str, Any]]:
    runtime = RecordingRuntime()
    card_ids = frozenset(sorted(col.db.list("select id from cards"))[::2])

    def predict(runtime_: RecordingRuntime, rows: Any, *, day: int) -> list[float]:
        payload = rows.payload()
        runtime_.payloads.append((day, payload))
        return [
            (len(payload) % 1000) / 1000 + index / 1e4 + day / 1e5 + runtime_._seen()
            for index in range(len(rows))
        ]

    with monkeypatch.context() as patch:
        patch.setattr(total_knowledge, "new_runtime", lambda: runtime)
        patch.setattr(
            aqt.rwkv_scheduler, "_predict_rwkv_memorised_day_from_rows", predict
        )
        if not backend:
            patch.setattr(total_knowledge, "_backend_replay", lambda *_args: None)
        built: list[object] = []
        real = total_knowledge._backend_replay
        if backend:
            patch.setattr(
                total_knowledge,
                "_backend_replay",
                lambda *args: built.append(real(*args)) or built[-1],
            )
        job = total_knowledge._Job(job_id=1, key=(), curve=curve)
        total_knowledge._compute(SimpleNamespace(col=col), job, card_ids)
        if backend:
            assert built and built[0] is not None, "the backend built no replay"
    cache_path = total_knowledge._day_sums_cache_path(SimpleNamespace(col=col))
    assert cache_path is not None
    with open(cache_path, encoding="utf-8") as file:
        cache = json.load(file)
    os.replace(cache_path, cache_path + (".backend" if backend else ".python"))
    for entry in cache["entries"].values():
        del entry["written"]
    return job, runtime, cache


@pytest.mark.parametrize("curve", [True, False], ids=["curve", "instant"])
def test_the_backends_replay_runs_the_day_loop_as_pythons_does(
    col: Collection, monkeypatch: pytest.MonkeyPatch, curve: bool
) -> None:
    job, runtime, cache = _run(col, monkeypatch, curve, backend=True)
    python_job, python_runtime, python_cache = _run(
        col, monkeypatch, curve, backend=False
    )

    assert len(job.sum_r) > 100
    assert job.first_day == python_job.first_day
    assert job.sum_r == python_job.sum_r
    assert runtime.warm_ups == python_runtime.warm_ups
    assert runtime.spans == python_runtime.spans
    assert runtime.payloads == python_runtime.payloads
    # the kept sums carry the same digest, so either build reuses them
    assert cache == python_cache
    if curve:
        assert runtime.spans
    else:
        assert runtime.payloads


def test_kept_sums_are_read_back_through_the_backends_digest(
    col: Collection, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, _, _ = _run(col, monkeypatch, True, backend=False)
    cache_path = total_knowledge._day_sums_cache_path(SimpleNamespace(col=col))
    assert cache_path is not None
    # the sums Python kept, read by a run the backend builds
    os.replace(cache_path + ".python", cache_path)
    reads: list[int] = []
    real = total_knowledge._cached_day_sums
    with monkeypatch.context() as patch:
        patch.setattr(
            total_knowledge,
            "_cached_day_sums",
            lambda *args: reads.append(len(result := real(*args))) or result,
        )
        again, runtime, _ = _run(col, monkeypatch, True, backend=True)
    assert reads and reads[0] == len(first.sum_r) - 1
    assert again.sum_r == first.sum_r
    assert runtime.warm_ups
