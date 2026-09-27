# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The recording pass frees the replay inputs of the history it read in
steps, after it returns or stops, and returns what it returned before."""

from __future__ import annotations

import weakref

import pytest

import aqt.rwkv_scheduler as rs
from aqt.rwkv_scheduler import (
    RwkvHistoricalReviewInputs,
    RwkvReviewIdentity,
    RwkvReviewInput,
)


def _history(n: int) -> RwkvHistoricalReviewInputs:
    reviews = [
        RwkvReviewInput(
            identity=RwkvReviewIdentity(
                card_id=1_000 + i, note_id=None, deck_id=None, preset_id=None
            ),
            is_query=False,
            ease=3,
            duration_millis=1_000,
            card_type=2,
            card_queue=2,
            card_due=10,
            interval_days=1,
            ease_factor=2500,
            reps=1,
            lapses=0,
            day_offset=i,
            current_state_kind=None,
            current_normal_state_kind=None,
            current_elapsed_days=1,
            current_elapsed_seconds=86_400,
        )
        for i in range(n)
    ]
    return RwkvHistoricalReviewInputs(
        reviews=reviews,
        review_ids=list(range(n)),
        previous_review_id_by_card={},
        previous_interval_days_by_card={},
        review_count_by_card={},
        last_review_id=n - 1,
        review_count=n,
    )


def test_every_input_is_freed_and_one_still_held_elsewhere_is_kept() -> None:
    history = _history(3 * rs._FREE_REVIEW_INPUTS_STEP + 7)
    kept = history.reviews[5]
    refs = [weakref.ref(review) for review in history.reviews]
    histories = [history]
    del history

    rs._free_review_inputs_in_steps(histories)

    assert histories == []
    assert [ref() for ref in refs if ref() is not None] == [kept]


def test_the_inputs_are_not_changed_for_another_holder_of_the_list() -> None:
    history = _history(10)
    same_list = history.reviews
    before = list(same_list)

    rs._free_review_inputs_in_steps([history])

    assert same_list == before


@pytest.mark.parametrize("result", [True, False])
def test_the_pass_returns_its_result_and_frees_the_history(
    monkeypatch: pytest.MonkeyPatch, result: bool
) -> None:
    box = [_history(100)]
    refs = [weakref.ref(review) for review in box[0].reviews]

    def inner(mw: object, *, read_history, **_kwargs) -> bool:
        read_history(box.pop())
        return result

    monkeypatch.setattr(rs, "_recompute_rwkv_calibration_data", inner)

    assert rs.recompute_rwkv_calibration_data(object()) is result
    assert all(ref() is None for ref in refs)


def test_a_pass_that_raises_still_frees_the_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    box = [_history(100)]
    refs = [weakref.ref(review) for review in box[0].reviews]

    def inner(mw: object, *, read_history, **_kwargs) -> bool:
        read_history(box.pop())
        raise rs.RwkvCurveRecordingUnavailable("no curve recorder")

    monkeypatch.setattr(rs, "_recompute_rwkv_calibration_data", inner)

    with pytest.raises(rs.RwkvCurveRecordingUnavailable):
        rs.recompute_rwkv_calibration_data(object())
    assert all(ref() is None for ref in refs)
