# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Total Knowledge keeps its finished days across runs (spec
ui.stats-total-knowledge-incremental).

The fake model's values depend on the day AND on how much history it has
warmed up, so a cached day that was wrong, or a later day that started from
the wrong state, would change the sums and fail these tests."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

import aqt.rwkv_scheduler
from aqt import total_knowledge
from aqt.rwkv_scheduler import RwkvReviewIdentity, RwkvReviewInput

NEXT_DAY_AT = 1_000_000


def rating(card_id: int, day: int, ease: int = 3) -> tuple[int, RwkvReviewInput]:
    review_id = (NEXT_DAY_AT - (20 - day) * 86_400 - 43_200 + card_id) * 1000
    return review_id, RwkvReviewInput(
        identity=RwkvReviewIdentity(card_id=card_id, note_id=card_id, deck_id=1),
        is_query=False,
        ease=ease,
        duration_millis=5000,
        card_type=2,
        card_queue=2,
        card_due=day,
        interval_days=1,
        ease_factor=2500,
        reps=1,
        lapses=0,
        day_offset=day,
        current_state_kind=None,
        current_normal_state_kind=None,
        current_elapsed_days=1,
        current_elapsed_seconds=86_400,
    )


class DayRuntime:
    def __init__(self) -> None:
        self.warmed = 0
        self.scored_days: list[int] = []

    def warm_up_reviews_in_place(self, reviews: list[Any]) -> None:
        self.warmed += len(reviews)

    def curve_retrievability_day_sums_from_warm_up(
        self, spans: list[tuple[int, int, int, int]]
    ) -> tuple[int, list[float]]:
        first = min(span[2] for span in spans)
        last = max(span[3] for span in spans)
        sums = [0.0] * (last - first + 1)
        for _card, review_day, first_day, last_day in spans:
            for day in range(first_day, last_day + 1):
                sums[day - first] += 0.9 ** (day - review_day) * (
                    1 + self.warmed / 1000
                )
        return first, sums


HISTORY = [
    rating(1, 1),
    rating(3, 1),
    rating(2, 2),
    rating(1, 4, ease=2),
    rating(3, 5),
    rating(2, 6),
    rating(1, 8),
]


def run(
    monkeypatch: pytest.MonkeyPatch,
    folder: Any,
    history: list[tuple[int, RwkvReviewInput]],
    today: int,
    curve: bool,
) -> tuple[list[float], DayRuntime]:
    runtime = DayRuntime()
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "_timing_today",
        lambda reviewer: SimpleNamespace(
            days_elapsed=today, next_day_at=NEXT_DAY_AT + (today - 20) * 86_400
        ),
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        lambda reviewer, **_kwargs: SimpleNamespace(
            review_ids=[review_id for review_id, _ in history],
            reviews=[review for _, review in history],
        ),
    )

    def predict(runtime_: DayRuntime, rows: Any, *, day: int) -> list[float]:
        runtime_.scored_days.append(day)
        return [0.5 + runtime_.warmed / 1000 + day / 10_000] * len(rows)

    monkeypatch.setattr(
        aqt.rwkv_scheduler, "_predict_rwkv_memorised_day_from_rows", predict
    )
    monkeypatch.setattr(total_knowledge, "new_runtime", lambda: runtime)
    mw = SimpleNamespace(
        col=SimpleNamespace(
            path=os.path.join(str(folder), "collection.anki2"),
            db=SimpleNamespace(all=lambda sql: []),
        )
    )
    job = total_knowledge._Job(job_id=1, key=(), curve=curve)
    total_knowledge._compute(mw, job, frozenset({1, 2}))
    return list(job.sum_r), runtime


def cache_file(folder: Any) -> str:
    return os.path.join(
        str(folder), "collection" + total_knowledge._DAY_SUMS_CACHE_SUFFIX
    )


# Pins spec/ui.md#ui.stats-total-knowledge-incremental
@pytest.mark.parametrize("curve", [False, True])
def test_a_run_that_reuses_the_days_matches_a_full_run_bit_for_bit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, curve: bool
) -> None:
    run(monkeypatch, tmp_path, HISTORY, today=10, curve=curve)
    grown = HISTORY + [rating(2, 10), rating(1, 11)]
    incremental, incremental_runtime = run(
        monkeypatch, tmp_path, grown, today=11, curve=curve
    )
    os.remove(cache_file(tmp_path))
    full, full_runtime = run(monkeypatch, tmp_path, grown, today=11, curve=curve)

    assert incremental == full
    # the whole history is still warmed up, day by day
    assert incremental_runtime.warmed == full_runtime.warmed == len(grown)
    if not curve:
        # only the days after the cached ones are scored
        assert min(incremental_runtime.scored_days) == 10
        assert min(full_runtime.scored_days) < 10


# Pins spec/ui.md#ui.stats-total-knowledge-incremental
def test_a_changed_old_review_throws_the_kept_days_away(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    run(monkeypatch, tmp_path, HISTORY, today=10, curve=False)
    edited = list(HISTORY)
    review_id, review = edited[3]
    edited[3] = (review_id, replace(review, ease=4))
    _sums, runtime = run(monkeypatch, tmp_path, edited, today=10, curve=False)
    assert min(runtime.scored_days) < 4


# Pins spec/ui.md#ui.stats-total-knowledge-incremental
def test_the_kept_days_stop_before_today_and_survive_a_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    sums, _runtime = run(monkeypatch, tmp_path, HISTORY, today=10, curve=False)
    with open(cache_file(tmp_path), encoding="utf-8") as file:
        (entry,) = json.load(file)["entries"].values()
    # today is not kept: its reviews are not all in yet
    assert entry["last_day"] == 9
    assert entry["sums"] == sums[:-1]
    # a new process reads the file: the same day again scores only today
    _sums, runtime = run(monkeypatch, tmp_path, HISTORY, today=10, curve=False)
    assert runtime.scored_days == [10]
