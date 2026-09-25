# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Locks the values of `_rwkv_stored_curve_retrievabilities_for_inputs`, the
RWKV-Curve R of the Stats graph (spec ui.rwkv-curve-r-stored-curve), against
the Python code that computed them before the evaluation moved to Rust:
every value equal to the last bit, the same cards skipped, the same order."""

from __future__ import annotations

import math
import random
import struct
import sys
from array import array
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from aqt import rwkv_scheduler
from aqt.rwkv_scheduler import set_reviewer_backend

# the Python evaluation the Stats graph used, kept here as the reference
_DECAY_RATES = tuple(
    math.log(0.9) / (0.1 + (math.exp(18.5 * index / 127) - 1.0) * math.exp(22.0 - 18.5))
    for index in range(128)
)


def _reference_recall(weights: Sequence[float], elapsed_seconds: int) -> float | None:
    if not weights or len(weights) > len(_DECAY_RATES):
        return None
    elapsed = max(float(elapsed_seconds), 1.0)
    raw_probability = sum(
        weight * math.exp(elapsed * rate) for weight, rate in zip(weights, _DECAY_RATES)
    )
    return 1e-5 + (1.0 - 2e-5) * raw_probability


def _reference_elapsed(review_input: Any) -> int | None:
    seconds = review_input.current_elapsed_seconds
    if isinstance(seconds, int) and seconds >= 0:
        return seconds
    days = review_input.current_elapsed_days
    if isinstance(days, int) and days >= 0:
        return days * 86_400
    return None


def _reference_unpack(
    card_ids: Sequence[int], packed: bytes
) -> dict[int, array] | None:
    curves: dict[int, array] = {}
    offset = 0
    for card_id in card_ids:
        if offset + 4 > len(packed):
            return None
        (count,) = struct.unpack_from("<I", packed, offset)
        offset += 4
        end = offset + 4 * count
        if end > len(packed):
            return None
        weights = array("f")
        weights.frombytes(packed[offset:end])
        if sys.byteorder != "little":
            weights.byteswap()
        curves[int(card_id)] = weights
        offset = end
    return curves if offset == len(packed) else None


def _valid(value: object) -> bool:
    return isinstance(value, float) and math.isfinite(value) and 0 <= value <= 1


def _reference(
    inputs: Sequence[tuple[int, Any]], ids: Sequence[int], packed: bytes
) -> list[tuple[int, float]]:
    curves = _reference_unpack(ids, packed)
    if curves is None:
        return []
    out = []
    for card_id, review_input in inputs:
        weights = curves.get(card_id)
        elapsed_seconds = _reference_elapsed(review_input)
        if weights is None or elapsed_seconds is None:
            continue
        retrievability = _reference_recall(weights, elapsed_seconds)
        if _valid(retrievability):
            out.append((card_id, retrievability))
    return out


def _packed(curves: Sequence[tuple[int, Sequence[float]]]) -> tuple[list[int], bytes]:
    return [card_id for card_id, _ in curves], b"".join(
        struct.pack("<I", len(weights)) + struct.pack(f"<{len(weights)}f", *weights)
        for _, weights in curves
    )


def _input(seconds: object, days: object) -> SimpleNamespace:
    return SimpleNamespace(current_elapsed_seconds=seconds, current_elapsed_days=days)


def _run(
    inputs: Sequence[tuple[int, Any]], ids: list[int], packed: bytes
) -> list[tuple[int, float]] | None:
    backend = SimpleNamespace(card_curve_weights=lambda _card_ids: (ids, packed))
    previous = set_reviewer_backend(backend)  # type: ignore[arg-type]
    try:
        return rwkv_scheduler._rwkv_stored_curve_retrievabilities_for_inputs(inputs)
    finally:
        set_reviewer_backend(previous)


def _softmax_curve(rng: random.Random, length: int) -> list[float]:
    raw = [math.exp(rng.gauss(0, 3)) for _ in range(length)]
    total = sum(raw)
    return list(array("f", [value / total for value in raw]))


def _elapsed(rng: random.Random) -> SimpleNamespace:
    kind = rng.randrange(6)
    if kind == 0:
        return _input(rng.randrange(0, 600), rng.randrange(0, 2))
    if kind == 1:
        return _input(rng.randrange(0, 10**9), None)
    if kind == 2:
        return _input(None, rng.randrange(0, 20_000))
    if kind == 3:
        return _input(-1, rng.randrange(0, 400))
    if kind == 4:
        return _input(None, None)
    return _input(0, 0)


def test_stored_curve_values_match_the_python_evaluation() -> None:
    rng = random.Random(20260925)
    curves: list[tuple[int, Sequence[float]]] = []
    for card_id in range(1, 1500):
        kind = rng.randrange(10)
        if kind == 0:
            continue  # no stored curve
        if kind == 1:
            curves.append((card_id, _softmax_curve(rng, rng.randrange(1, 128))))
        elif kind == 2:
            # any weights RWKV could hold, sums above 1 included
            curves.append(
                (
                    card_id,
                    list(array("f", [rng.uniform(-0.5, 1.5) for _ in range(128)])),
                )
            )
        else:
            curves.append((card_id, _softmax_curve(rng, 128)))
    # a curve of an unknown shape and an empty one give no value
    curves.append((5000, [0.5] * 129))
    curves.append((5001, []))
    # an id packed twice: the later curve counts
    curves.append((7, _softmax_curve(rng, 128)))
    ids, packed = _packed(curves)
    inputs = [(card_id, _elapsed(rng)) for card_id in range(1, 1500)]
    inputs += [(5000, _input(10, 0)), (5001, _input(10, 0)), (9999, _input(10, 0))]
    # a card asked for twice, at two elapsed times
    inputs += [(3, _input(86_400 * 30, 30)), (3, _input(5, 0))]

    expected = _reference(inputs, ids, packed)
    assert len(expected) > 900
    assert _run(inputs, ids, packed) == expected


@pytest.mark.parametrize(
    "packed",
    [
        b"",  # nothing for an id
        struct.pack("<I", 3) + struct.pack("<2f", 0.5, 0.5),  # cut short
        struct.pack("<I", 1) + struct.pack("<f", 0.5) + b"\0",  # a byte too many
    ],
)
def test_malformed_curves_give_no_value(packed: bytes) -> None:
    assert _reference([(1, _input(10, 0))], [1], packed) == []
    assert _run([(1, _input(10, 0))], [1], packed) == []


def test_no_curves_give_no_value() -> None:
    backend = SimpleNamespace(card_curve_weights=lambda _card_ids: None)
    previous = set_reviewer_backend(backend)  # type: ignore[arg-type]
    try:
        assert (
            rwkv_scheduler._rwkv_stored_curve_retrievabilities_for_inputs(
                [(1, _input(10, 0))]
            )
            == []
        )
    finally:
        set_reviewer_backend(previous)
