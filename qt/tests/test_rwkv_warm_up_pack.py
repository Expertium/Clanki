# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The packed warm-up request of a batch of RWKV review inputs is the same
bytes whichever packer made it: `_packed_warm_up_reviews` (Rust, in
_rsbridge) against the Python packer it replaced, copied below as it was."""

from __future__ import annotations

import enum
import random
import struct
from collections.abc import Sequence
from dataclasses import replace

import pytest

from aqt.rwkv_scheduler import RwkvReviewIdentity, RwkvReviewInput
from aqt.rwkv_srs_benchmark import (
    _PACKED_PREDICTION_REQUEST_HEADER,
    _PACKED_WARM_UP_REVIEW_MAGIC,
    _packed_review_input_row,
    _packed_warm_up_reviews,
)


def _python_packed_warm_up_reviews(reviews: Sequence[RwkvReviewInput]) -> bytes:
    """The Python packer, as it was before it moved to Rust."""
    payload = bytearray(
        _PACKED_PREDICTION_REQUEST_HEADER.pack(
            _PACKED_WARM_UP_REVIEW_MAGIC,
            len(reviews),
        )
    )
    for review_input in reviews:
        payload.extend(_packed_review_input_row(review_input))
    return bytes(payload)


class _State(enum.IntEnum):
    LEARN = 1
    REVIEW = 2


def _review(**changes: object) -> RwkvReviewInput:
    review = RwkvReviewInput(
        identity=RwkvReviewIdentity(
            card_id=1_700_000_000_123, note_id=1_700_000_000_001, deck_id=1, preset_id=7
        ),
        is_query=False,
        ease=3,
        duration_millis=5_432,
        card_type=2,
        card_queue=2,
        card_due=100,
        interval_days=12,
        ease_factor=2500,
        reps=4,
        lapses=0,
        day_offset=19_000,
        current_state_kind=None,
        current_normal_state_kind=None,
        current_elapsed_days=3,
        current_elapsed_seconds=3 * 86_400 + 17,
        target_retentions=(0.9, 0.85, 0.9, 0.95),
        enforce_grade_order=True,
    )
    identity = changes.pop("identity", None)
    if isinstance(identity, dict):
        review = replace(review, identity=replace(review.identity, **identity))
    return replace(review, **changes)


def _random_review(rng: random.Random) -> RwkvReviewInput:
    def maybe(value: object) -> object:
        return None if rng.random() < 0.2 else value

    return _review(
        identity={
            "card_id": rng.randrange(-(2**63), 2**63),
            "note_id": maybe(rng.randrange(-(2**63), 2**63)),
            "deck_id": maybe(rng.randrange(0, 2**40)),
            "preset_id": maybe(rng.randrange(0, 2**62)),
        },
        is_query=rng.random() < 0.3,
        ease=maybe(rng.randrange(0, 256)),
        duration_millis=maybe(rng.randrange(-(2**63), 2**63)),
        card_type=maybe(rng.choice([0, 1, 2, 3, _State.LEARN, _State.REVIEW])),
        day_offset=maybe(rng.randrange(-100_000, 100_000)),
        current_elapsed_days=maybe(rng.randrange(-1, 40_000)),
        current_elapsed_seconds=maybe(rng.randrange(-1, 2**40)),
        target_retentions=tuple(
            maybe(rng.choice([rng.random(), 1, 0, True, rng.uniform(-3e38, 3e38)]))
            for _ in range(4)
        ),
        enforce_grade_order=rng.random() < 0.7,
    )


def test_the_rust_packer_gives_the_python_packer_s_bytes() -> None:
    rng = random.Random(20260926)
    reviews = [_random_review(rng) for _ in range(5_000)]
    assert _packed_warm_up_reviews(reviews) == _python_packed_warm_up_reviews(reviews)


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"is_query": True, "ease": None, "duration_millis": None},
        {"identity": {"note_id": None, "deck_id": None, "preset_id": None}},
        {"target_retentions": (None, None, None, None)},
        {"target_retentions": (1, 0, True, False)},
        {"target_retentions": (0.1, 1e-45, 3.4e38, -0.0)},
        {"target_retentions": (float("nan"), float("inf"), float("-inf"), 0.5)},
        {"card_type": _State.REVIEW, "ease": True},
        {"ease": 255, "duration_millis": -(2**63), "day_offset": 2**63 - 1},
        {"ease": 4.9, "duration_millis": 12.7, "current_elapsed_days": -3.5},
        {"ease": "2", "duration_millis": "-17"},
        {"is_query": 1, "enforce_grade_order": 0},
        {"is_query": "", "enforce_grade_order": "yes"},
        {"identity": {"card_id": True}},
        {"identity": {"card_id": -(2**63)}},
        {"identity": {"note_id": 2.5}},
    ],
)
def test_each_field_is_packed_as_python_packed_it(changes: dict) -> None:
    reviews = [_review(**changes)]
    assert _packed_warm_up_reviews(reviews) == _python_packed_warm_up_reviews(reviews)


def test_no_reviews_is_the_header_alone() -> None:
    assert _packed_warm_up_reviews([]) == _python_packed_warm_up_reviews([])
    assert _packed_warm_up_reviews(()) == struct.pack("<8sI", b"ARWKVWU2", 0)


def test_a_tuple_of_reviews_packs_as_a_list_does() -> None:
    reviews = [_review(), _review(ease=1)]
    assert _packed_warm_up_reviews(tuple(reviews)) == _packed_warm_up_reviews(reviews)


def test_more_rows_than_one_block_keep_their_order() -> None:
    rng = random.Random(7)
    reviews = [_random_review(rng) for _ in range(3 * 1024 + 5)]
    assert _packed_warm_up_reviews(reviews) == _python_packed_warm_up_reviews(reviews)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"ease": 256}, struct.error),
        ({"ease": -1}, struct.error),
        ({"duration_millis": 2**63}, struct.error),
        ({"identity": {"card_id": 2**63}}, struct.error),
        ({"identity": {"card_id": 1.0}}, struct.error),
        ({"identity": {"card_id": None}}, struct.error),
        ({"target_retentions": (1e39, None, None, None)}, OverflowError),
        ({"ease": "three"}, ValueError),
        ({"target_retentions": ("x", None, None, None)}, ValueError),
        ({"target_retentions": (0.9, 0.9, 0.9)}, IndexError),
    ],
)
def test_a_value_its_field_cannot_hold_raises_as_python_raised(
    changes: dict, error: type[Exception]
) -> None:
    reviews = [_review(**changes)]
    with pytest.raises(error):
        _python_packed_warm_up_reviews(reviews)
    with pytest.raises(error):
        _packed_warm_up_reviews(reviews)
