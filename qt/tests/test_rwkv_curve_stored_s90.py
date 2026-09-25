# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""RWKV-Curve's stability in the Stats Stability graph and `prop:s` is the
S90 of each card's stored curve (spec ui.rwkv-curve-stored-s90)."""

from __future__ import annotations

import math
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from aqt import rwkv_scheduler
from aqt.rwkv_scheduler import RwkvStatsPreparationStatus


def _mw(algorithm: str, card_ids: list[int], published: list[Any]) -> Any:
    backend = SimpleNamespace(
        set_rwkv_curve_s90s=lambda card_ids, s90s: published.append((card_ids, s90s))
    )
    return SimpleNamespace(
        col=SimpleNamespace(
            get_config=lambda key, default=None: algorithm,
            db=SimpleNamespace(list=lambda sql: list(card_ids)),
            _backend=backend,
        )
    )


@pytest.fixture
def rwkv_ready(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(asked=[])

    def card_curve_s90s(packed: bytes) -> bytes:
        card_ids = struct.unpack(f"<{len(packed) // 8}q", packed)
        state.asked.append(list(card_ids))
        # card 2 has no stored curve
        return struct.pack(
            f"<{len(card_ids)}f",
            *[math.nan if card_id == 2 else card_id * 10.0 for card_id in card_ids],
        )

    backend = SimpleNamespace(card_curve_s90s=card_curve_s90s)
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", backend)
    monkeypatch.setattr(
        rwkv_scheduler, "_prepare_reviewer_backend_for_card_info", lambda r: True
    )
    monkeypatch.setattr(
        rwkv_scheduler,
        "_capture_reviewer_backend_prediction_state_token",
        lambda reviewer: "token",
    )

    @contextmanager
    def access(**_kwargs: Any) -> Iterator[object]:
        yield backend

    monkeypatch.setattr(
        rwkv_scheduler, "_try_reviewer_backend_prediction_access", access
    )
    return state


# Pins spec/ui.md#ui.rwkv-curve-stored-s90
def test_rwkv_curve_publishes_the_stored_curves_s90s(
    rwkv_ready: SimpleNamespace,
) -> None:
    published: list[Any] = []
    status = rwkv_scheduler.publish_rwkv_curve_s90s(
        _mw("rwkvCurve", [1, 2, 3], published)
    )
    assert status == RwkvStatsPreparationStatus.READY
    assert rwkv_ready.asked == [[1, 2, 3]]
    [(card_ids, s90s)] = published
    assert struct.unpack("<3q", card_ids) == (1, 2, 3)
    one, two, three = struct.unpack("<3f", s90s)
    assert (one, three) == (10.0, 30.0)
    assert math.isnan(two)


# Pins spec/ui.md#ui.rwkv-curve-stored-s90
@pytest.mark.parametrize("algorithm", ["fsrs7", "rwkvInstant"])
def test_other_algorithms_publish_no_curve_s90s(
    rwkv_ready: SimpleNamespace, algorithm: str
) -> None:
    published: list[Any] = []
    status = rwkv_scheduler.publish_rwkv_curve_s90s(_mw(algorithm, [1], published))
    assert status == RwkvStatsPreparationStatus.READY
    assert published == []
    assert rwkv_ready.asked == []


def test_rwkv_not_ready_publishes_nothing_and_asks_again(
    rwkv_ready: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rwkv_scheduler, "_prepare_reviewer_backend_for_card_info", lambda r: False
    )
    published: list[Any] = []
    status = rwkv_scheduler.publish_rwkv_curve_s90s(_mw("rwkvCurve", [1], published))
    assert status == RwkvStatsPreparationStatus.PENDING
    assert published == []


def test_without_a_model_no_card_has_a_curve_s90(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rwkv_scheduler, "_reviewer_backend", None)
    monkeypatch.setattr(
        rwkv_scheduler, "configure_reviewer_backend_from_environment", lambda: False
    )
    published: list[Any] = []
    status = rwkv_scheduler.publish_rwkv_curve_s90s(_mw("rwkvCurve", [1], published))
    assert status == RwkvStatsPreparationStatus.UNAVAILABLE
    assert published == [(b"", b"")]


# Pins spec/ui.md#ui.rwkv-curve-stored-s90: a `prop:s` search under
# RWKV-Curve gets the S90s before it runs, in the Browser and AnkiConnect
@pytest.mark.parametrize(
    "algorithm, search, needs",
    [
        ("rwkvCurve", "prop:s>10", True),
        ("rwkvCurve", "deck:x PROP:S<=3", True),
        ("rwkvCurve", "prop:r>0.5", False),
        ("fsrs7", "prop:s>10", False),
        ("rwkvInstant", "prop:s>10", False),
        ("fsrs7", "prop:rwkv-curve:r<0.9", True),
    ],
)
def test_searches_that_need_rwkv_values(
    algorithm: str, search: str, needs: bool
) -> None:
    col = SimpleNamespace(get_config=lambda key, default=None: algorithm)
    assert rwkv_scheduler.search_needs_rwkv_values(col, search) is needs


def test_a_curve_stability_search_publishes_the_s90s_and_scores_nothing(
    rwkv_ready: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    scored: list[str] = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "prepare_stats_retrievability_scores",
        lambda reviewer, search, **kwargs: scored.append(search),
    )
    published: list[Any] = []
    mw = _mw("rwkvCurve", [1, 3], published)
    status = rwkv_scheduler.prepare_browser_retrievability_scores(mw, "prop:s>5")
    assert status == RwkvStatsPreparationStatus.READY
    assert len(published) == 1
    assert scored == []
