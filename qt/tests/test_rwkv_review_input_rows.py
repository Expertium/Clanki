# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Locks the RWKV review inputs built from the backend's input rows (the
Stats graph's search, the Browser's values, the study queue's deck rows and
the rows of given cards) against the Python code that built them from the
parsed rows before the conversion moved to Rust: the same inputs field for
field (value and type), grouped under the same batch sizes in the same
order, and the same counts."""

from __future__ import annotations

import hashlib
import math
import random
import time
from collections.abc import Callable
from typing import Any

import pytest

from anki import scheduler_pb2
from aqt import rwkv_scheduler
from aqt.rwkv_scheduler import (
    RwkvReviewIdentity,
    RwkvReviewInput,
    RwkvReviewState,
)

Response = scheduler_pb2.RwkvReviewInputRowsForCardsResponse

# the Python conversion the inputs came from, kept here as the reference


def _reference_stable_preset_id(preset_id: str) -> int:
    if preset_id.isdecimal():
        return int(preset_id)

    digest = hashlib.blake2b(preset_id.encode("utf8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & ((1 << 63) - 1)


def _reference_valid_probability(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def _reference_review_state(
    *, state_kind: str | None, normal_state_kind: str | None, card_type: int | None
) -> int | None:
    if state_kind == "filtered":
        return int(RwkvReviewState.FILTERED)

    normal_states = {
        "new": RwkvReviewState.LEARN_START,
        "learning": RwkvReviewState.LEARNING,
        "review": RwkvReviewState.REVIEW,
        "relearning": RwkvReviewState.RELEARNING,
    }
    if (state := normal_states.get(normal_state_kind)) is not None:
        return int(state)

    return card_type


def _reference_backend_bool(row: Any, name: str, default: bool) -> bool:
    has_field = getattr(row, "HasField", None)
    if callable(has_field):
        try:
            if not has_field(name):
                return default
        except ValueError:
            pass
    value = getattr(row, name, None)
    return value if isinstance(value, bool) else default


def _reference_input(row: Any) -> RwkvReviewInput:
    preset_id = _reference_stable_preset_id(row.preset_id) if row.preset_id else None
    target_retention = (
        row.target_retention
        if _reference_valid_probability(row.target_retention)
        else 0.9
    )
    state_kind = row.current_state_kind or None
    normal_state_kind = row.current_normal_state_kind or None

    return RwkvReviewInput(
        identity=RwkvReviewIdentity(
            card_id=row.card_id,
            note_id=row.note_id,
            deck_id=row.deck_id,
            preset_id=preset_id,
        ),
        is_query=True,
        ease=None,
        duration_millis=None,
        card_type=_reference_review_state(
            state_kind=state_kind,
            normal_state_kind=normal_state_kind,
            card_type=row.card_type,
        ),
        card_queue=row.card_queue,
        card_due=row.card_due,
        interval_days=row.interval_days,
        ease_factor=row.ease_factor,
        reps=row.reps,
        lapses=row.lapses,
        day_offset=row.day_offset,
        current_state_kind=state_kind,
        current_normal_state_kind=normal_state_kind,
        current_elapsed_days=(
            row.current_elapsed_days if row.HasField("current_elapsed_days") else None
        ),
        current_elapsed_seconds=(
            row.current_elapsed_seconds
            if row.HasField("current_elapsed_seconds")
            else None
        ),
        target_retentions=(
            target_retention,
            target_retention,
            target_retention,
            target_retention,
        ),
        enforce_grade_order=_reference_backend_bool(row, "enforce_grade_order", True),
    )


def _reference_build(
    response: Any, batch_size_override: int | None
) -> tuple[list[tuple[int, list[tuple[int, RwkvReviewInput]]]], tuple[int, ...]]:
    inputs_by_batch_size: dict[int, list[tuple[int, RwkvReviewInput]]] = {}
    for row in response.rows:
        review_input = _reference_input(row)
        batch_size = (
            batch_size_override
            if batch_size_override is not None
            else (
                row.batch_size
                if isinstance(row.batch_size, int)
                and not isinstance(row.batch_size, bool)
                and 64 <= row.batch_size <= 8192
                else 512
            )
        )
        inputs_by_batch_size.setdefault(batch_size, []).append(
            (review_input.identity.card_id, review_input)
        )
    counts = (
        response.loaded_cards,
        len(response.rows),
        response.cards_with_supported_state,
        response.disabled_config_cards,
        len(response.rows),
        response.deck_configs,
    )
    return list(inputs_by_batch_size.items()), counts


def _strict(value: object) -> object:
    """`value` with every leaf's type beside it, so that 1 and True, or 1
    and 1.0, differ; floats by their bits (repr round-trips them)."""
    if isinstance(value, RwkvReviewInput | RwkvReviewIdentity):
        return (type(value).__name__,) + tuple(
            (name, _strict(getattr(value, name)))
            for name in type(value).__dataclass_fields__
        )
    if isinstance(value, tuple | list):
        return (type(value).__name__,) + tuple(_strict(item) for item in value)
    return (type(value).__name__, repr(value))


# rows covering every branch of the conversion

_PRESETS = [
    "",
    "1",
    "0",
    "00123",
    "1627549937879",
    "9" * 30,
    "١٢٣",  # Arabic-Indic digits: isdecimal, int() reads them
    "addon-preset",
    "Default",
    "café",
    " 12",
    "12a",
    "-5",
]
_STATE_KINDS = ["", "normal", "filtered", "rescheduling", "Filtered"]
_NORMAL_STATE_KINDS = ["", "new", "learning", "review", "relearning", "Review", "x"]
_RETENTIONS = [
    0.0,
    1.0,
    0.9,
    0.85,
    0.123456789,
    -0.0,
    -0.1,
    1.0000001,
    math.inf,
    -math.inf,
    math.nan,
    1e-45,
    3.4e38,
]
_BATCH_SIZES = [0, 1, 63, 64, 65, 128, 512, 8191, 8192, 8193, 4_294_967_295]
_I64 = [0, 1, -1, 2**63 - 1, -(2**63), 1_700_000_000_000]
_I32 = [0, 1, -1, 2**31 - 1, -(2**31), 12345]
_U32 = [0, 1, 2**32 - 1, 36500, 2500]


def _random_row(rng: random.Random) -> Any:
    row = Response.Row(
        card_id=rng.choice(_I64 + [rng.randrange(1, 2**41)]),
        note_id=rng.choice(_I64 + [rng.randrange(1, 2**41)]),
        deck_id=rng.choice(_I64 + [rng.randrange(1, 2**41)]),
        preset_id=rng.choice(_PRESETS),
        card_type=rng.choice(_I32 + [2, 3]),
        card_queue=rng.choice(_I32 + [-1, -2, -3, 2]),
        card_due=rng.choice(_I32 + [rng.randrange(-(2**31), 2**31)]),
        interval_days=rng.choice(_U32),
        ease_factor=rng.choice(_U32),
        reps=rng.choice(_U32),
        lapses=rng.choice(_U32),
        day_offset=rng.choice(_U32),
        current_state_kind=rng.choice(_STATE_KINDS),
        current_normal_state_kind=rng.choice(_NORMAL_STATE_KINDS),
        target_retention=rng.choice(_RETENTIONS + [rng.random()]),
        batch_size=rng.choice(_BATCH_SIZES),
    )
    if rng.random() < 0.6:
        row.current_elapsed_days = rng.choice(_U32)
    if rng.random() < 0.6:
        row.current_elapsed_seconds = rng.choice(_U32)
    if rng.random() < 0.6:
        row.enforce_grade_order = rng.random() < 0.5
    return row


def _response(rows: list[Any], rng: random.Random) -> Any:
    response = Response(
        loaded_cards=rng.choice(_U32),
        cards_with_supported_state=rng.choice(_U32),
        disabled_config_cards=rng.choice(_U32),
        deck_configs=rng.choice(_U32),
        searched_cards=rng.choice(_U32),
    )
    response.rows.extend(rows)
    return response


class _Backend:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def rwkv_review_input_rows_for_cards_raw(self, message: bytes) -> bytes:
        return self.raw

    def rwkv_review_input_rows_for_search_raw(self, message: bytes) -> bytes:
        return self.raw

    def rwkv_review_input_rows_for_deck_review_queue_raw(self, message: bytes) -> bytes:
        return self.raw


_RESPONSE_READERS: dict[str, Callable[[_Backend], object | None]] = {
    "cards": lambda backend: rwkv_scheduler._rwkv_review_input_rows_backend_response(
        backend,
        card_ids=[1, 2],
        include_suspended_review=True,
        include_new_cards=False,
    ),
    "search": lambda backend: (
        rwkv_scheduler._rwkv_review_input_rows_for_search_backend_response(
            backend, search="deck:current", include_suspended_review=True
        )
    ),
    "deck": lambda backend: (
        rwkv_scheduler._rwkv_review_input_rows_for_deck_review_queue_backend_response(
            backend, deck_id=1, include_new_cards=False
        )
    ),
}


def _built(
    response: object, batch_size_override: int | None
) -> tuple[list[tuple[int, list[tuple[int, RwkvReviewInput]]]], tuple[int, ...], int]:
    build = rwkv_scheduler._rwkv_review_input_batch_build_from_backend_response(
        reviewer=None,
        response=response,
        batch_size_override=batch_size_override,
        load_start=time.monotonic(),
        source_label="test",
        source_size=7,
    )
    assert build.searched_rows == 7
    counts = (
        build.loaded_rows,
        build.parsed_cards,
        build.cards_with_state,
        build.disabled_config_cards,
        build.eligible_cards,
        build.deck_configs,
    )
    return (
        list(build.inputs_by_batch_size.items()),
        counts,
        rwkv_scheduler._rwkv_backend_uint(response, "searched_cards"),
    )


@pytest.mark.parametrize("reader", sorted(_RESPONSE_READERS))
@pytest.mark.parametrize("batch_size_override", [None, 128, 0])
@pytest.mark.parametrize("seed", range(6))
def test_review_inputs_match_the_python_conversion(
    reader: str, batch_size_override: int | None, seed: int
) -> None:
    rng = random.Random(seed * 7919 + len(reader))
    rows = [_random_row(rng) for _ in range(rng.choice([0, 1, 3, 400]))]
    expected_response = _response(rows, rng)
    response = _RESPONSE_READERS[reader](
        _Backend(expected_response.SerializeToString())
    )
    assert response is not None

    inputs, counts, searched = _built(response, batch_size_override)
    expected_inputs, expected_counts = _reference_build(
        expected_response, batch_size_override
    )

    assert counts == expected_counts
    assert searched == expected_response.searched_cards
    assert [size for size, _ in inputs] == [size for size, _ in expected_inputs]
    assert _strict(inputs) == _strict(expected_inputs)
    assert inputs == expected_inputs


def test_every_row_branch_matches_the_python_conversion() -> None:
    """One row per value of each field that has a branch, the others
    random, so that no branch depends on luck."""
    rng = random.Random(11)
    rows = []
    for preset in _PRESETS:
        for state_kind in _STATE_KINDS:
            for normal_state_kind in _NORMAL_STATE_KINDS:
                row = _random_row(rng)
                row.preset_id = preset
                row.current_state_kind = state_kind
                row.current_normal_state_kind = normal_state_kind
                rows.append(row)
    for retention in _RETENTIONS:
        for batch_size in _BATCH_SIZES:
            row = _random_row(rng)
            row.target_retention = retention
            row.batch_size = batch_size
            rows.append(row)
    for has_days in (False, True):
        for has_seconds in (False, True):
            for grade_order in (None, False, True):
                row = _random_row(rng)
                row.ClearField("current_elapsed_days")
                row.ClearField("current_elapsed_seconds")
                row.ClearField("enforce_grade_order")
                if has_days:
                    row.current_elapsed_days = 0
                if has_seconds:
                    row.current_elapsed_seconds = 0
                if grade_order is not None:
                    row.enforce_grade_order = grade_order
                rows.append(row)
    expected_response = _response(rows, rng)
    response = _RESPONSE_READERS["search"](
        _Backend(expected_response.SerializeToString())
    )

    inputs, counts, _ = _built(response, None)
    expected_inputs, expected_counts = _reference_build(expected_response, None)

    assert counts == expected_counts
    assert _strict(inputs) == _strict(expected_inputs)


@pytest.mark.parametrize("reader", sorted(_RESPONSE_READERS))
def test_malformed_rows_read_as_no_rows(reader: str) -> None:
    assert _RESPONSE_READERS[reader](_Backend(b"\x0a\xff\xff")) is None
