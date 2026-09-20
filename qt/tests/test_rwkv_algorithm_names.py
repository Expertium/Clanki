# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins spec/ui.md#ui.rwkv-algorithm-names."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[2]


@pytest.fixture
def names(monkeypatch: pytest.MonkeyPatch) -> None:
    from aqt.utils import tr

    monkeypatch.setattr(
        tr, "deck_config_scheduler_choice_rwkv_curve", lambda: "RWKV-Curve"
    )
    monkeypatch.setattr(
        tr, "deck_config_scheduler_choice_rwkv_instant", lambda: "RWKV-Instant"
    )


@pytest.mark.parametrize(
    "search,name",
    [
        ("prop:rwkv:r<0.9", "RWKV-Instant"),
        ("is:rwkv:due", "RWKV-Instant"),
        ("prop:rwkv-curve:r<0.9", "RWKV-Curve"),
    ],
)
def test_a_search_names_the_rwkv_algorithm_it_reads(names, search, name):
    from aqt.rwkv_scheduler import rwkv_algorithm_name_for_search

    assert rwkv_algorithm_name_for_search(search) == name


@pytest.mark.parametrize(
    "stored,name", [("rwkvCurve", "RWKV-Curve"), ("rwkvInstant", "RWKV-Instant")]
)
def test_the_collection_names_its_rwkv_algorithm(names, stored, name):
    from aqt.rwkv_scheduler import rwkv_algorithm_name

    col = SimpleNamespace(get_config=lambda key, default=None: stored)
    assert rwkv_algorithm_name(col) == name


def test_no_english_label_says_a_bare_rwkv_about_values():
    """A bare "RWKV" may name the model (its file, its state); a label for
    values, scores, intervals, queues or menus names the algorithm."""
    allowed = re.compile(
        r"RWKV model|no RWKV model|RWKV review history|RWKV cannot schedule"
    )
    offenders = []
    for path in list((ROOT / "ftl/core").glob("*.ftl")) + list(
        (ROOT / "ftl/qt").glob("*.ftl")
    ):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            text = line.split("=", 1)[1]
            bare = re.sub(r"RWKV-(Curve|Instant)", "", text)
            if "RWKV" in bare and not allowed.search(text):
                offenders.append(f"{path.name}: {line.strip()}")
    assert offenders == []


# Pins spec/ui.md#ui.plain-progress-text
def test_the_waiting_messages_name_no_algorithm() -> None:
    from pathlib import Path

    ftl = (Path(__file__).parents[2] / "ftl" / "qt" / "qt-misc.ftl").read_text(
        encoding="utf-8"
    )
    for key in (
        "qt-misc-rwkv-curve-intervals-pending",
        "qt-misc-rwkv-instant-scores-pending",
    ):
        line = next(x for x in ftl.splitlines() if x.startswith(key + " ="))
        text = line.split("=", 1)[1]
        assert "RWKV" not in text and "Waiting" not in text, line
