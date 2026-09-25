# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Locks the request that publishes the Stats graph's RWKV scores
(`_set_rwkv_stats_graph_scores`, SetRwkvStatsGraphScores) against the Python
code that built it one Score message per card before the encoding moved to
Rust: the same scores in the same order, each optional field present or
absent as before, every value equal to the last bit, and the same errors
for values the request cannot hold."""

from __future__ import annotations

import math
import random
import struct
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from types import SimpleNamespace
from typing import Any

import pytest

from anki import scheduler_pb2
from aqt import rwkv_scheduler

Request = scheduler_pb2.RwkvStatsGraphScoresRequest
_OPTIONAL_FIELDS = (
    "retrievability",
    "intervening_reviews",
    "target_retention",
    "curve_due",
    "curve_retrievability",
)


# the Python encoding the scores had, kept here as the reference


def _reference_valid_probability(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def _reference_request(
    search: str,
    scores: list[tuple[Any, Any]],
    target_retentions_by_card_id: Mapping[Any, Any],
    intervening_reviews_by_card_id: Mapping[Any, Any],
    curve_due_card_ids: AbstractSet[Any],
    curve_retrievabilities_by_card_id: Mapping[Any, Any],
) -> Any:
    score_messages = []
    retrievabilities_by_card_id: dict[Any, Any] = dict(scores)
    for card_id in curve_retrievabilities_by_card_id:
        retrievabilities_by_card_id.setdefault(card_id, None)
    for card_id, retrievability in retrievabilities_by_card_id.items():
        score = Request.Score(card_id=card_id)
        if retrievability is not None:
            score.retrievability = retrievability
        target_retention = target_retentions_by_card_id.get(card_id)
        if _reference_valid_probability(target_retention):
            score.target_retention = target_retention
        intervening_reviews = intervening_reviews_by_card_id.get(card_id)
        if isinstance(intervening_reviews, int) and intervening_reviews >= 0:
            score.intervening_reviews = intervening_reviews
        if card_id in curve_due_card_ids:
            score.curve_due = True
        curve_retrievability = curve_retrievabilities_by_card_id.get(card_id)
        if _reference_valid_probability(curve_retrievability):
            score.curve_retrievability = curve_retrievability
        score_messages.append(score)
    return Request(search=search, scores=score_messages)


def _bits(value: object) -> object:
    if isinstance(value, float):
        return struct.pack("<f", value).hex()
    return value


def _strict(request: Any) -> tuple[object, ...]:
    """The request field by field: each optional field's presence, and
    floats by their f32 bits (NaN and -0.0 included)."""
    return (request.search,) + tuple(
        (score.card_id,)
        + tuple(
            (name, _bits(getattr(score, name)) if score.HasField(name) else None)
            for name in _OPTIONAL_FIELDS
        )
        for score in request.scores
    )


class _Backend:
    """Records the request, whichever method the publication calls."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def set_rwkv_stats_graph_scores(self, *, search: str, scores: list[Any]) -> None:
        self.requests.append(Request(search=search, scores=scores))

    def set_rwkv_stats_graph_scores_raw(self, message: bytes) -> bytes:
        request = Request()
        request.ParseFromString(message)
        self.requests.append(request)
        return b""


def _published(
    search: str,
    scores: list[tuple[Any, Any]],
    target_retentions_by_card_id: dict[Any, Any],
    intervening_reviews_by_card_id: dict[Any, Any],
    curve_due_card_ids: AbstractSet[Any],
    curve_retrievabilities_by_card_id: dict[Any, Any],
) -> Any:
    backend = _Backend()
    rwkv_scheduler._set_rwkv_stats_graph_scores(
        SimpleNamespace(),
        search,
        scores,
        target_retentions_by_card_id=target_retentions_by_card_id,
        intervening_reviews_by_card_id=intervening_reviews_by_card_id,
        curve_due_card_ids=curve_due_card_ids,
        curve_retrievabilities_by_card_id=curve_retrievabilities_by_card_id,
        collection_backend=backend,
    )
    (request,) = backend.requests
    return request


_PROBABILITIES: list[Any] = [
    0.0,
    1.0,
    -0.0,
    0.5,
    0.1,
    1 / 3,
    0.999999999,
    1e-45,
    1e-300,
    0,
    1,
    True,
    False,
    -0.1,
    1.0000001,
    2,
    math.nan,
    math.inf,
    -math.inf,
    None,
    "0.5",
]
_RETRIEVABILITIES: list[Any] = [
    0.0,
    1.0,
    -0.0,
    0.25,
    1 / 3,
    1e300,
    -1e300,
    math.nan,
    math.inf,
    1.5,
    -2.0,
    0,
    1,
    True,
    3,
]
_INTERVENING: list[Any] = [
    0,
    1,
    5,
    2**32 - 1,
    -1,
    True,
    False,
    3.0,
    None,
    "2",
]


def _random_case(
    rng: random.Random, size: int
) -> tuple[
    list[tuple[Any, Any]],
    dict[Any, Any],
    dict[Any, Any],
    frozenset[Any],
    dict[Any, Any],
]:
    card_ids = [rng.randrange(1, 2**41) for _ in range(size)]
    card_ids += [0, -1, 2**63 - 1, -(2**63)]
    rng.shuffle(card_ids)

    def pick(values: list[Any]) -> Any:
        return rng.choice(values) if rng.random() < 0.3 else rng.random()

    # repeated ids: the last value wins, at the first one's place
    scores = [
        (rng.choice(card_ids), rng.choice(_RETRIEVABILITIES + [rng.random()]))
        for _ in range(size)
    ]
    target_retentions = {
        card_id: pick(_PROBABILITIES) for card_id in card_ids if rng.random() < 0.5
    }
    intervening = {
        card_id: rng.choice(_INTERVENING + [rng.randrange(0, 50)])
        for card_id in card_ids
        if rng.random() < 0.5
    }
    curve_due = frozenset(card_id for card_id in card_ids if rng.random() < 0.3)
    curve = {
        card_id: pick(_PROBABILITIES) for card_id in card_ids if rng.random() < 0.6
    }
    return scores, target_retentions, intervening, curve_due, curve


@pytest.mark.parametrize("seed", range(12))
def test_stats_scores_request_matches_the_python_encoding(seed: int) -> None:
    rng = random.Random(seed)
    case = _random_case(rng, rng.choice([0, 1, 5, 300]))
    expected = _reference_request("deck:current", *case)

    request = _published("deck:current", *case)

    assert _strict(request) == _strict(expected)


def test_every_optional_value_matches_the_python_encoding() -> None:
    card_ids = list(range(1, 1 + len(_PROBABILITIES) * len(_INTERVENING)))
    values = [(p, i) for p in _PROBABILITIES for i in _INTERVENING]
    scores = [
        (card_id, _RETRIEVABILITIES[index % len(_RETRIEVABILITIES)])
        for index, card_id in enumerate(card_ids)
        if index % 4
    ]
    target_retentions = {card_id: p for card_id, (p, _) in zip(card_ids, values)}
    intervening = {card_id: i for card_id, (_, i) in zip(card_ids, values)}
    curve = {card_id: p for card_id, (p, _) in zip(reversed(card_ids), values)}
    case = (
        scores,
        target_retentions,
        intervening,
        frozenset(card_ids[::3]),
        curve,
    )

    assert _strict(_published("is:rwkv:due", *case)) == _strict(
        _reference_request("is:rwkv:due", *case)
    )


def test_nothing_to_publish_is_an_empty_request() -> None:
    request = _published("deck:x", [], {}, {}, frozenset(), {})

    assert _strict(request) == ("deck:x",)


@pytest.mark.parametrize(
    ("case", "error"),
    [
        (([(2**63, 0.5)], {}, {}, frozenset(), {}), ValueError),
        (([(-(2**63) - 1, 0.5)], {}, {}, frozenset(), {}), ValueError),
        (([(1.0, 0.5)], {}, {}, frozenset(), {}), TypeError),
        (([(1, "0.5")], {}, {}, frozenset(), {}), TypeError),
        (([(1, 0.5)], {}, {1: 2**32}, frozenset(), {}), ValueError),
        (([], {}, {}, frozenset(), {2**63: 0.5}), ValueError),
    ],
)
def test_values_the_request_cannot_hold_raise_as_before(
    case: tuple[Any, ...], error: type[Exception]
) -> None:
    with pytest.raises(error):
        _reference_request("deck:x", *case)
    with pytest.raises(error):
        _published("deck:x", *case)
