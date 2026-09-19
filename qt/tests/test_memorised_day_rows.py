# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""`MemorisedDayRows`: the rows a Memorised day scores.

The rows are packed once per rating instead of once per day, and the Rust
side derives the day and the two elapsed fields. These tests pin the two
things that could drift: the order and contents of the rows against the plain
dictionary they replace, and the equality of the predictions with the
per-day packing on the real model.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from aqt.rwkv_scheduler import RwkvReviewIdentity, RwkvReviewInput
from aqt.rwkv_srs_benchmark import (
    MemorisedDayRows,
    _packed_memorised_query_inputs,
    _packed_warm_up_reviews,
)

_MODEL_FILENAME = "RWKV_trained_on_5000_10000.bin"


def rating(card_id: int, day: int, *, ease: int = 3) -> RwkvReviewInput:
    """A rated review of `card_id` on scheduler day `day`."""

    return RwkvReviewInput(
        identity=RwkvReviewIdentity(
            card_id=card_id,
            note_id=card_id + 1_000_000,
            deck_id=1,
            preset_id=1,
        ),
        is_query=False,
        ease=ease,
        duration_millis=4200,
        card_type=2,
        card_queue=2,
        card_due=day,
        interval_days=3,
        ease_factor=2500,
        reps=2,
        lapses=0,
        day_offset=day,
        current_state_kind=None,
        current_normal_state_kind=None,
        current_elapsed_days=1,
        current_elapsed_seconds=86_400,
    )


def test_a_rated_card_keeps_its_place_and_a_removed_one_goes_to_the_end() -> None:
    """The order is the order a dict keyed by card gives, because the sum
    over the rows must keep its old order."""

    rows = MemorisedDayRows()
    for card_id in (1, 2, 3):
        rows.set(card_id, rating(card_id, day=1))
    assert rows.card_ids() == [1, 2, 3]

    # a card rated again keeps its place
    rows.set(2, rating(2, day=2))
    assert rows.card_ids() == [1, 2, 3]

    # a removed card goes to the end when it is rated again
    rows.remove(1)
    assert rows.card_ids() == [2, 3]
    assert len(rows) == 2
    rows.set(1, rating(1, day=3))
    assert rows.card_ids() == [2, 3, 1]

    # and the inputs follow the same order
    assert [row.identity.card_id for row in rows.review_inputs()] == [2, 3, 1]
    assert [row.day_offset for row in rows.review_inputs()] == [2, 1, 3]


def test_removing_many_cards_clears_the_empty_places_and_keeps_the_order() -> None:
    """Compaction must not reorder the rows; it only removes empty places."""

    rows = MemorisedDayRows()
    for card_id in range(20):
        rows.set(card_id, rating(card_id, day=card_id))
    for card_id in (3, 7, 11, 15, 19):
        rows.remove(card_id)

    expected = [card_id for card_id in range(20) if card_id not in {3, 7, 11, 15, 19}]
    assert rows.card_ids() == expected
    assert [row.identity.card_id for row in rows.review_inputs()] == expected
    assert rows.payload() == _packed_warm_up_reviews(rows.review_inputs())


def test_the_payload_holds_the_live_rows_and_nothing_else() -> None:
    rows = MemorisedDayRows()
    rows.set(5, rating(5, day=1))
    rows.set(6, rating(6, day=2))
    rows.remove(5)
    assert rows.payload() == _packed_warm_up_reviews([rating(6, day=2)])


def _rsbridge_runtime() -> Any:
    root = Path(__file__).resolve().parents[2]
    # the extension is _rsbridge.pyd on Windows and _rsbridge.so elsewhere
    extension_path = next(
        (
            candidate
            for candidate in (
                root / "out/pylib/anki/_rsbridge.pyd",
                root / "out/pylib/anki/_rsbridge.so",
            )
            if candidate.exists()
        ),
        None,
    )
    if extension_path is None:
        pytest.skip("built _rsbridge extension is unavailable")
    model_path = root / "qt/aqt/rwkv_inference" / _MODEL_FILENAME
    if not model_path.exists():
        pytest.skip(f"RWKV model is unavailable: {model_path}")

    spec = importlib.util.spec_from_file_location("_rsbridge", extension_path)
    if spec is None or spec.loader is None:
        pytest.skip(f"unable to load _rsbridge from {extension_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RwkvInference(str(model_path), 0.9, 36500)


def test_the_day_rows_predict_exactly_what_the_per_day_packing_predicted() -> None:
    """The behaviour lock. The old path packs every card again on every day;
    the new one packs a row once and gives Rust the day. The predictions must
    be the same bytes, on the real model, with real warmed-up state."""

    runtime = _rsbridge_runtime()

    ratings = [rating(card_id, day=card_id % 5) for card_id in range(1, 41)]
    runtime.warm_up_reviews_packed(_packed_warm_up_reviews(ratings), False)

    rows = MemorisedDayRows()
    for review_input in ratings:
        rows.set(review_input.identity.card_id, review_input)
    # a removed card must not be scored, and a re-rated one must move
    rows.remove(4)
    rows.remove(9)
    rows.set(11, rating(11, day=2))
    rows.remove(20)
    rows.set(20, rating(20, day=4))

    inputs = rows.review_inputs()
    assert len(inputs) == 38

    for day in (0, 3, 7, 40):
        old = bytes(
            runtime.predict_retrievability_many_from_warm_up_packed(
                _packed_memorised_query_inputs(inputs, day=day)
            )
        )
        new = bytes(
            runtime.predict_retrievability_many_from_warm_up_packed_on_day(
                rows.payload(), day
            )
        )
        assert len(old) == len(inputs) * 4
        assert new == old
