# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins spec/ui.md#ui.reviewer-simple-view."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ADVANCED_ONLY = [
    "bury_current_card",
    "forget_current_card",
    "on_set_due",
    "on_previous_card_info",
    "bury_current_note",
    "suspend_current_note",
    "on_create_copy",
    "on_seek_backward",
    "on_seek_forward",
    "onRecordVoice",
    "onReplayRecorded",
    "toggle_auto_advance",
]
KEPT = [
    "suspend_current_card",
    "onOptions",
    "on_card_info",
    "toggle_mark_on_current_note",
    "delete_current_note",
    "replayAudio",
    "on_pause_audio",
]


@pytest.fixture
def reviewer(monkeypatch: pytest.MonkeyPatch) -> Any:
    from aqt.reviewer import Reviewer
    from aqt.utils import tr

    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "x")
    reviewer: Any = Reviewer.__new__(Reviewer)
    mode = {"advanced": False}
    reviewer.mode = mode
    flag = SimpleNamespace(label="Red", index=1)
    reviewer.mw = SimpleNamespace(
        advanced_ui=lambda: mode["advanced"],
        flags=SimpleNamespace(all=lambda: [flag]),
    )
    reviewer.card = SimpleNamespace(user_flag=lambda: 0)
    reviewer.auto_advance_enabled = False
    return reviewer


def _callbacks(opts: list[Any]) -> list[Any]:
    return [row[2] for row in opts if row and len(row) > 2]


def _submenus(opts: list[Any]) -> list[Any]:
    return [row for row in opts if row and len(row) == 2]


def test_simple_mode_more_menu_keeps_only_the_everyday_items(reviewer):
    simple = _callbacks(reviewer._contextMenu())
    for name in ADVANCED_ONLY:
        assert getattr(reviewer, name) not in simple, name
    for name in KEPT:
        assert getattr(reviewer, name) in simple, name
    # Flag Card is the menu's only submenu, and flags are Advanced-only
    assert _submenus(reviewer._contextMenu()) == []


def test_advanced_mode_more_menu_is_unchanged(reviewer):
    reviewer.mode["advanced"] = True
    opts = reviewer._contextMenu()
    advanced = _callbacks(opts)
    for name in ADVANCED_ONLY + KEPT:
        assert getattr(reviewer, name) in advanced, name
    assert len(_submenus(opts)) == 1


def _english(path: str, key: str) -> str:
    text = (Path(__file__).parents[2] / path).read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(key)} = (.*)$", text, re.MULTILINE)
    assert match, key
    return match.group(1)


def test_marking_is_called_tagging():
    """Marking adds the tag "marked", so the English labels say Tag."""
    assert _english("ftl/core/studying.ftl", "studying-mark-note") == "Tag Note"
    assert _english("ftl/core/browsing.ftl", "browsing-toggle-mark") == "Toggle Tag"
