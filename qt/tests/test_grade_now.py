# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/scheduling.md#sched.grade-now-rwkv-curve.

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from anki.cards_pb2 import FsrsMemoryState
from anki.collection import Collection
from anki.decks import DeckConfigId, DeckId
from anki.errors import InvalidInput
from anki.scheduler.v3 import CardAnswer
from aqt import rwkv_scheduler
from aqt.operations.scheduling import grade_cards_now
from aqt.rwkv_scheduler import (
    RwkvIntervalOverride,
    RwkvReviewPrediction,
    set_reviewer_backend,
)

GOOD = 3

# RWKV-Curve's Good is 200 days, and it goes through the same review fuzz as
# any other interval (spec sched.rwkv-curve-fuzz). The default fuzz delta at
# 200 days is 1 + 0.15 * (7 - 2.5) + 0.10 * (20 - 7) + 0.05 * (200 - 20) =
# 11.975 days, so the interval lands anywhere in 188..212, and the load
# balancer moves it only inside that range. These bounds are what the code
# produces: a narrower 190..210 stood here and failed about one run in three,
# because a card whose fuzz picked 188, 189, 211 or 212 is correct.
RWKV_CURVE_GOOD_BOUNDS = (188, 212)


@pytest.fixture(autouse=True)
def no_rwkv_backend() -> Iterator[None]:
    previous = set_reviewer_backend(None)
    try:
        yield
    finally:
        set_reviewer_backend(previous)
        rwkv_scheduler._reviewer_backend_warmup_states.clear()
        rwkv_scheduler._reviewer_backend_resident_ignored_review_ids.clear()


def _use_rwkv(col: Collection, backend: object | None, *, ready: bool = True) -> None:
    """Make `backend` the RWKV model, with its state for `col` loaded when
    `ready`."""
    set_reviewer_backend(backend)  # type: ignore[arg-type]
    if backend is not None and ready:
        rwkv_scheduler._reviewer_backend_warmup_states[(id(backend), id(col))] = None


class _RwkvBackend:
    """Stands in for the RWKV model: the same prediction for every card."""

    def __init__(self, prediction: RwkvReviewPrediction | None) -> None:
        self.prediction = prediction
        self.predicted_card_ids: list[int] = []

    def predict_review(self, *, reviewer: object, card: object) -> object:
        self.predicted_card_ids.append(getattr(card, "id"))
        return self.prediction


# RWKV-Curve's prediction for every card: Good in 200 days, S90 210 days
_CURVE_PREDICTION = RwkvReviewPrediction(
    retrievability=0.83,
    interval_overrides=RwkvIntervalOverride(again=0.2, hard=90, good=200, easy=300),
    s90_overrides=RwkvIntervalOverride(again=1, hard=95, good=210, easy=320),
)


def _collection(tmp_path: Path, algorithm: str) -> Collection:
    col = Collection(str(tmp_path / "grade-now.anki2"))
    col.set_config("schedulingAlgorithm", algorithm)
    # a preset write takes the collection's algorithm (sched.one-global-algorithm)
    col.decks.update_config(col.decks.get_config(DeckConfigId(1)))
    return col


def _add_review_card(col: Collection) -> int:
    note = col.new_note(col.models.by_name("Basic"))
    note.fields[0] = "front"
    col.add_note(note, DeckId(1))
    card = note.cards()[0]
    card.type = 2
    card.queue = 2
    card.ivl = 20
    card.due = col.sched.today
    card.memory_state = FsrsMemoryState(stability=20, difficulty=5)
    card.last_review_time = card.id // 1000 - 20 * 86_400
    col.update_card(card, skip_undo_entry=True)
    return card.id


def _reviewer(col: Collection) -> object:
    return SimpleNamespace(mw=SimpleNamespace(col=col))


def _fsrs7_good_days(col: Collection, card_id: int) -> int:
    return col.sched.get_scheduling_states(card_id).good.normal.review.scheduled_days


def test_grade_now_gives_rwkv_curve_cards_rwkv_curve_intervals(
    tmp_path: Path,
) -> None:
    col = _collection(tmp_path, "rwkvCurve")
    try:
        card_id = _add_review_card(col)
        fsrs7_good = _fsrs7_good_days(col, card_id)
        backend = _RwkvBackend(_CURVE_PREDICTION)
        _use_rwkv(col, backend)

        result = grade_cards_now(col, [card_id], GOOD, reviewer=_reviewer(col))

        assert result.answered_card_ids == (card_id,)
        assert result.unanswered_card_ids == ()
        assert backend.predicted_card_ids == [card_id]
        card = col.get_card(card_id)
        # RWKV-Curve's 200 days after review fuzz and the load balancer,
        # not FSRS-7's Good interval
        low, high = RWKV_CURVE_GOOD_BOUNDS
        assert low <= card.ivl <= high, (card.ivl, fsrs7_good)
        assert abs(card.ivl - fsrs7_good) > 20, (card.ivl, fsrs7_good)
        assert card.due == col.sched.today + card.ivl
        # RWKV-Curve's S90 for Good is the stored stability
        assert card.memory_state is not None
        assert card.memory_state.stability == pytest.approx(210)
        # one review-log row, as the reviewer's answer writes it
        rows = col.db.all("select ease, type, ivl from revlog where cid = ?", card_id)
        assert rows == [[3, 1, card.ivl]]
        assert col.db.scalar(
            "select prediction from search_stats_rwkv_review_retrievability"
        ) == pytest.approx(0.83)
        assert col.undo_status().undo == col.tr.actions_grade_now()
    finally:
        col.close()


def test_grade_now_leaves_rwkv_curve_cards_without_intervals_unanswered(
    tmp_path: Path,
) -> None:
    col = _collection(tmp_path, "rwkvCurve")
    try:
        card_id = _add_review_card(col)
        # no RWKV model, its state not loaded yet, a prediction without intervals
        for backend, ready in (
            (None, True),
            (_RwkvBackend(_CURVE_PREDICTION), False),
            (_RwkvBackend(RwkvReviewPrediction(retrievability=0.8)), True),
        ):
            _use_rwkv(col, backend, ready=ready)

            result = grade_cards_now(col, [card_id], GOOD, reviewer=_reviewer(col))

            assert result.answered_card_ids == ()
            assert result.unanswered_card_ids == (card_id,)
            card = col.get_card(card_id)
            assert (card.ivl, card.queue) == (20, 2)
            assert card.memory_state is not None
            assert card.memory_state.stability == pytest.approx(20)
            assert col.db.scalar("select count() from revlog") == 0
    finally:
        col.close()


def test_backend_grade_now_refuses_rwkv_curve_cards_without_their_states(
    tmp_path: Path,
) -> None:
    col = _collection(tmp_path, "rwkvCurve")
    try:
        card_id = _add_review_card(col)

        with pytest.raises(InvalidInput):
            col._backend.grade_now(
                card_ids=[card_id], rating=CardAnswer.GOOD, card_options=[]
            )

        assert col.get_card(card_id).ivl == 20
        assert col.db.scalar("select count() from revlog") == 0
    finally:
        col.close()


def test_grade_now_under_rwkv_instant_answers_as_the_reviewer(
    tmp_path: Path,
) -> None:
    col = _collection(tmp_path, "rwkvInstant")
    try:
        card_id = _add_review_card(col)
        fsrs_good = _fsrs7_good_days(col, card_id)
        _use_rwkv(col, _RwkvBackend(RwkvReviewPrediction(retrievability=0.61)))

        result = grade_cards_now(col, [card_id], GOOD, reviewer=_reviewer(col))

        assert result.answered_card_ids == (card_id,)
        # RWKV-Instant stores the FSRS states its reviewer stores, and the
        # retrievability its reviewer's answer carries
        # (sched.rwkv-instant-no-intervals)
        assert col.get_card(card_id).ivl == fsrs_good
        assert col.db.scalar(
            "select prediction from search_stats_rwkv_review_retrievability"
        ) == pytest.approx(0.61)
    finally:
        col.close()


def test_grade_now_under_fsrs7_keeps_the_given_options(tmp_path: Path) -> None:
    col = _collection(tmp_path, "fsrs7")
    try:
        card_id = _add_review_card(col)
        backend = _RwkvBackend(_CURVE_PREDICTION)
        _use_rwkv(col, backend)

        cards = rwkv_scheduler.rwkv_grade_now_cards(_reviewer(col), [card_id], GOOD)

        assert cards.answered_card_ids == (card_id,)
        assert not cards.card_options[0].HasField("scheduling_states")
        assert backend.predicted_card_ids == []
        grade_cards_now(col, [card_id], GOOD, reviewer=_reviewer(col))
        assert col.db.scalar("select count() from revlog") == 1
    finally:
        col.close()
