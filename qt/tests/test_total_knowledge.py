# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The Total Knowledge graph's RWKV job (spec ui.stats-total-knowledge)."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest

import aqt.rwkv_scheduler
from anki.stats_pb2 import (
    TotalKnowledgeRwkvJob,
    TotalKnowledgeRwkvProgress,
    TotalKnowledgeRwkvRequest,
)
from aqt import total_knowledge

Progress = TotalKnowledgeRwkvProgress
TODAY = 10
NEXT_DAY_AT = 1_000_000


def review_id(day: int, minute: int = 0) -> int:
    """A review id on scheduler day `day` (TODAY is today)."""
    return (NEXT_DAY_AT - (TODAY - day) * 86_400 - 43_200 + minute * 60) * 1000


def review(card_id: int, day: int) -> SimpleNamespace:
    return SimpleNamespace(identity=SimpleNamespace(card_id=card_id), day_offset=day)


class FakeRuntime:
    """Instant head: R 0.5 for every card. Curve head: 0.9 ** days since the
    card's last review."""

    def __init__(self) -> None:
        self.warmed_up: list[list[int]] = []
        self.queried: dict[int, list[int]] = {}

    def warm_up_reviews_in_place(self, reviews: Sequence[Any]) -> None:
        self.warmed_up.append([r.identity.card_id for r in reviews])

    def curve_retrievability_day_sums_from_warm_up(
        self, spans: Sequence[tuple[int, int, int, int]]
    ) -> tuple[int, list[float]]:
        first = min(span[2] for span in spans)
        last = max(span[3] for span in spans)
        sums = [0.0] * (last - first + 1)
        for _card, review_day, first_day, last_day in spans:
            for day in range(first_day, last_day + 1):
                sums[day - first] += 0.9 ** (day - review_day)
        return first, sums


@pytest.fixture
def rwkv_history(monkeypatch: pytest.MonkeyPatch) -> FakeRuntime:
    # card 3 is outside the search; card 1 is reset on day 7
    reviews = [
        (review_id(1), review(3, 1)),
        (review_id(2), review(1, 2)),
        (review_id(4), review(2, 4)),
        (review_id(5), review(1, 5)),
        (review_id(5, 1), review(1, 5)),
    ]
    runtime = FakeRuntime()
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "_timing_today",
        lambda reviewer: SimpleNamespace(days_elapsed=TODAY, next_day_at=NEXT_DAY_AT),
    )
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        lambda reviewer, **_kwargs: SimpleNamespace(
            review_ids=[review_id for review_id, _ in reviews],
            reviews=[review for _, review in reviews],
        ),
    )

    def predict(runtime_: object, rows: Any, *, day: int) -> list[float]:
        runtime.queried[day] = sorted(rows.card_ids())
        return [0.5] * len(rows)

    monkeypatch.setattr(
        aqt.rwkv_scheduler, "_predict_rwkv_memorised_day_from_rows", predict
    )
    monkeypatch.setattr(total_knowledge, "new_runtime", lambda: runtime)
    return runtime


def fake_mw() -> Any:
    # resets: card 1 on day 7, card 3 (not in the search) on day 8
    resets = [(review_id(7), 1), (review_id(8), 3)]
    return SimpleNamespace(
        col=SimpleNamespace(db=SimpleNamespace(all=lambda sql: resets))
    )


def compute(curve: bool) -> total_knowledge._Job:
    job = total_knowledge._Job(job_id=1, key=(), curve=curve)
    total_knowledge._compute(fake_mw(), job, frozenset({1, 2}))
    return job


# Pins spec/ui.md#ui.stats-total-knowledge
def test_rwkv_instant_sums_the_instant_heads_r_of_the_searched_cards(
    rwkv_history: FakeRuntime,
) -> None:
    job = compute(curve=False)

    # from RWKV's first day (day 1) through today, relative to today
    assert job.first_day == 1 - TODAY
    # a rating day counts 1, the other days the instant head's R; a reset
    # zeroes the card until its next rating
    assert job.sum_r == pytest.approx(
        [0.0, 1.0, 0.5, 1.5, 1.5, 1.0, 0.5, 0.5, 0.5, 0.5]
    )
    # the whole collection warms up, day by day
    assert rwkv_history.warmed_up[0] == [3]
    assert [3] not in rwkv_history.warmed_up[1:]
    # a card rated today and a reset card are not asked
    assert rwkv_history.queried[5] == [2]
    assert rwkv_history.queried[7] == [2]
    assert rwkv_history.queried[3] == [1]


# Pins spec/ui.md#ui.stats-total-knowledge
def test_rwkv_curve_sums_the_curves_stored_at_each_review(
    rwkv_history: FakeRuntime,
) -> None:
    job = compute(curve=True)

    assert job.first_day == 1 - TODAY
    assert job.sum_r == pytest.approx(
        [0.0, 1.0, 0.9, 1.81, 1.9, 1.71, 0.729, 0.9**4, 0.9**5, 0.9**6]
    )
    # the curve head never asks the instant head
    assert rwkv_history.queried == {}


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A collection under RWKV whose jobs wait until released."""
    state: dict[str, Any] = {"release": threading.Event(), "runs": []}
    monkeypatch.setattr(aqt.rwkv_scheduler, "rwkv_model_available", lambda: True)
    monkeypatch.setattr(total_knowledge, "_job", None)
    monkeypatch.setattr(total_knowledge, "_results", {})

    def compute(
        mw: object, job: total_knowledge._Job, card_ids: frozenset[int]
    ) -> None:
        with job.lock:
            job.first_day = -2
            job.sum_r.append(0.25)
        state["runs"].append((job.job_id, job.curve, card_ids))
        if not state["release"].wait(5) or job.cancel_event.is_set():
            raise InterruptedError()
        with job.lock:
            job.sum_r.extend([0.5, 0.75])

    monkeypatch.setattr(total_knowledge, "_compute", compute)
    cards = {"deck:a": [1, 2], "deck:b": [3]}
    state["mw"] = SimpleNamespace(
        col=SimpleNamespace(
            find_cards=lambda search: cards[search],
            mod=100,
            sched=SimpleNamespace(today=TODAY),
        )
    )
    return state


def wait_until_done(job_id: int) -> Progress:
    for _ in range(500):
        progress = total_knowledge.rwkv_progress(job_id)
        if progress.state != Progress.COMPUTING:
            return progress
        threading.Event().wait(0.01)
    raise AssertionError("the job did not finish")


def wait_for_runs(state: dict[str, Any], count: int) -> None:
    for _ in range(500):
        if len(state["runs"]) >= count:
            return
        threading.Event().wait(0.01)
    raise AssertionError("the job did not start")


# Pins spec/ui.md#ui.stats-total-knowledge
def test_the_page_joins_the_running_job_and_the_result_is_kept(
    started: dict[str, Any],
) -> None:
    mw = started["mw"]
    first = total_knowledge.start_rwkv(mw, "deck:a", curve=True)
    assert first.state == Progress.COMPUTING
    wait_for_runs(started, 1)
    # asking again for the same cards joins the job
    again = total_knowledge.start_rwkv(mw, "deck:a", curve=True)
    assert again.job_id == first.job_id
    assert len(started["runs"]) == 1
    assert started["runs"][0][1:] == (True, frozenset({1, 2}))

    started["release"].set()
    done = wait_until_done(first.job_id)
    assert done.state == Progress.DONE
    assert (done.first_day, list(done.sum_r)) == (-2, [0.25, 0.5, 0.75])

    # the finished result is kept for the session
    cached = total_knowledge.start_rwkv(mw, "deck:a", curve=True)
    assert cached.state == Progress.DONE
    assert list(cached.sum_r) == [0.25, 0.5, 0.75]
    assert len(started["runs"]) == 1

    # new reviews (the collection changed) compute again
    mw.col.mod = 101
    assert total_knowledge.start_rwkv(mw, "deck:a", curve=True).job_id != first.job_id
    wait_for_runs(started, 2)


def test_a_kept_result_holds_a_digest_of_the_card_ids_not_the_ids(
    started: dict[str, Any],
) -> None:
    # the ids of a 159k-card search cost 8.5 MB per kept result
    mw = started["mw"]
    job = total_knowledge.start_rwkv(mw, "deck:a", curve=True)
    wait_for_runs(started, 1)
    started["release"].set()
    assert wait_until_done(job.job_id).state == Progress.DONE

    (key,) = total_knowledge._results
    assert key == (True, total_knowledge._cards_digest((1, 2)), 100, TODAY)
    assert len(key[1]) == 16
    # another search over the same cards reads the same result
    assert total_knowledge._cards_digest((1, 2)) != total_knowledge._cards_digest((1,))


# Pins spec/ui.md#ui.stats-total-knowledge
def test_leaving_the_page_or_another_search_cancels_the_job(
    started: dict[str, Any],
) -> None:
    mw = started["mw"]
    first = total_knowledge.start_rwkv(mw, "deck:a", curve=False)
    # another search replaces the job
    second = total_knowledge.start_rwkv(mw, "deck:b", curve=False)
    assert second.job_id != first.job_id
    assert total_knowledge.rwkv_progress(first.job_id).state == Progress.CANCELLED
    # closing the page (or the Stats window) stops it
    total_knowledge.cancel_rwkv(second.job_id)
    started["release"].set()
    assert wait_until_done(second.job_id).state == Progress.CANCELLED
    # and nothing is kept
    assert total_knowledge._results == {}


def test_a_stopped_job_is_not_joined(started: dict[str, Any]) -> None:
    mw = started["mw"]
    first = total_knowledge.start_rwkv(mw, "deck:a", curve=True)
    total_knowledge.cancel_rwkv(first.job_id)
    # the same cards again, before the job noticed: a new job
    second = total_knowledge.start_rwkv(mw, "deck:a", curve=True)
    assert second.job_id != first.job_id
    started["release"].set()
    assert wait_until_done(second.job_id).state == Progress.DONE


# Pins spec/scheduling.md#sched.rwkv-no-model-error
def test_without_an_rwkv_model_the_graph_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aqt.rwkv_scheduler, "rwkv_model_available", lambda: False)
    monkeypatch.setattr(total_knowledge, "_job", None)
    mw = SimpleNamespace(col=None)
    assert total_knowledge.start_rwkv(mw, "", curve=True).state == Progress.NO_MODEL
    assert total_knowledge._job is None


# Pins spec/ui.md#ui.stats-total-knowledge
def test_the_page_starts_polls_and_cancels_through_mediasrv(
    started: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import aqt
    from aqt.mediasrv import (
        app,
        exposed_backend_list,
        post_handler_list,
        total_knowledge_rwkv_cancel,
        total_knowledge_rwkv_progress,
        total_knowledge_rwkv_start,
    )

    assert {
        total_knowledge_rwkv_start,
        total_knowledge_rwkv_progress,
        total_knowledge_rwkv_cancel,
    } <= set(post_handler_list)
    # the upper bound and FSRS-7's sum come from the backend
    assert "total_knowledge" in exposed_backend_list

    monkeypatch.setattr(aqt, "mw", started["mw"], raising=False)
    data = TotalKnowledgeRwkvRequest(search="deck:b", curve=True).SerializeToString()
    with app.test_request_context(data=data):
        started_progress = Progress.FromString(total_knowledge_rwkv_start())
    assert started_progress.state == Progress.COMPUTING
    wait_for_runs(started, 1)
    assert started["runs"][0][1:] == (True, frozenset({3}))

    job = TotalKnowledgeRwkvJob(job_id=started_progress.job_id).SerializeToString()
    with app.test_request_context(data=job):
        polled = Progress.FromString(total_knowledge_rwkv_progress())
    assert polled.job_id == started_progress.job_id
    assert list(polled.sum_r) == [0.25]

    with app.test_request_context(data=job):
        assert total_knowledge_rwkv_cancel() == b""
    started["release"].set()
    assert wait_until_done(started_progress.job_id).state == Progress.CANCELLED


def test_a_closed_page_stops_the_job_while_the_history_is_built(
    rwkv_history: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The history build is about ten seconds of Python on a large
    collection. A page that closes during it stops the job at the build's next
    progress report, so the main window does not wait for it."""
    job = total_knowledge._Job(job_id=1, key=(), curve=False)
    reached_rows = []

    def history(reviewer: Any, *, progress: Any) -> Any:
        progress("Preparing RWKV review inputs", 0, 2000)
        job.cancel_event.set()  # the page closes here
        progress("Preparing RWKV review inputs", 1000, 2000)
        reached_rows.append(2000)  # never reached
        raise AssertionError("the build went on after the page closed")

    monkeypatch.setattr(aqt.rwkv_scheduler, "_historical_rwkv_review_inputs", history)
    with pytest.raises(InterruptedError):
        total_knowledge._compute(fake_mw(), job, frozenset({1, 2}))
    assert reached_rows == []
    assert rwkv_history.warmed_up == []


def test_a_page_closed_before_the_job_starts_builds_no_history(
    rwkv_history: FakeRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = total_knowledge._Job(job_id=1, key=(), curve=False)
    job.cancel_event.set()
    built = []
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "_historical_rwkv_review_inputs",
        lambda reviewer, **kwargs: built.append(1),
    )
    with pytest.raises(InterruptedError):
        total_knowledge._compute(fake_mw(), job, frozenset({1, 2}))
    assert built == []
