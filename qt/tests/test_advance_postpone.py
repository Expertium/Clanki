# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/ui.md#ui.advance-postpone (and the Advance/Postpone part of
# ui.mode-switch) and spec/scheduling.md#sched.advance-postpone-algorithm
# (the RWKV-Curve curves the moves send).

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import anki.lang
from anki.lang import without_unicode_isolation

anki.lang.set_lang("en")

import aqt.advance_postpone as ap  # noqa: E402
import aqt.rwkv_scheduler  # noqa: E402
from anki.scheduler.base import (  # noqa: E402
    AdvancePostponeCandidates,
    AdvancePostponePreview,
    AdvancePostponeResponse,
)
from aqt.browser.browser import Browser  # noqa: E402


def _mw(
    *,
    advanced: bool = True,
    algorithm: str | None = "fsrs7",
) -> Any:
    return SimpleNamespace(
        advanced_ui=lambda: advanced,
        col=SimpleNamespace(
            get_config=lambda key, default=None: algorithm,
            get_config_bool=lambda key: advanced,
        ),
    )


@pytest.mark.parametrize(
    "advanced,algorithm,shown",
    [
        (True, "fsrs7", True),
        (True, "rwkvCurve", True),
        (True, None, True),
        (False, "fsrs7", False),
        (False, "rwkvCurve", False),
        (True, "rwkvInstant", False),
    ],
)
def test_deck_menu_has_advance_and_postpone_in_advanced_mode_but_not_rwkv_instant(
    advanced: bool, algorithm: str | None, shown: bool
) -> None:
    menu = MagicMock()
    ap.add_deck_menu_actions(menu, _mw(advanced=advanced, algorithm=algorithm), 7)
    assert menu.addAction.call_count == (2 if shown else 0)
    if shown:
        labels = [
            without_unicode_isolation(call.args[0])
            for call in menu.addAction.call_args_list
        ]
        assert labels == ["Advance Cards...", "Postpone Cards..."]


def test_deck_menu_entries_open_the_dialog_for_the_deck(monkeypatch: Any) -> None:
    opened: list[dict[str, Any]] = []
    monkeypatch.setattr(ap, "advance_postpone", lambda **kwargs: opened.append(kwargs))
    menu = MagicMock()
    mw = _mw()
    actions = [MagicMock(), MagicMock()]
    menu.addAction.side_effect = actions
    ap.add_deck_menu_actions(menu, mw, 7)
    for action in actions:
        (slot,) = action.triggered.connect.call_args.args
        slot(False)
    assert [(call["mode"], call["deck_id"], call["mw"]) for call in opened] == [
        (ap.ADVANCE, 7, mw),
        (ap.POSTPONE, 7, mw),
    ]


def _browser(*, selected: int, mw: Any) -> Any:
    browser = cast(Any, Browser.__new__(Browser))
    browser.mw = mw
    browser.table = SimpleNamespace(len_selection=lambda: selected)
    browser.action_advance = MagicMock()
    browser.action_postpone = MagicMock()
    browser.editor = SimpleNamespace(call_after_note_saved=lambda callback: callback())
    browser.selected_cards = lambda: [11, 12]
    return browser


def test_browser_actions_follow_the_mode_the_algorithm_and_the_selection() -> None:
    for mw, selected, visible in [
        (_mw(), 2, True),
        (_mw(), 0, True),
        (_mw(advanced=False), 2, False),
        (_mw(algorithm="rwkvInstant"), 2, False),
    ]:
        browser = _browser(selected=selected, mw=mw)
        browser._update_advance_postpone_actions()
        for action in (browser.action_advance, browser.action_postpone):
            action.setVisible.assert_called_once_with(visible)
            action.setEnabled.assert_called_once_with(selected > 0)


def test_browser_actions_act_on_the_selected_cards(monkeypatch: Any) -> None:
    opened: list[dict[str, Any]] = []
    monkeypatch.setattr(ap, "advance_postpone", lambda **kwargs: opened.append(kwargs))
    browser = _browser(selected=2, mw=_mw())
    browser.advance_cards()
    browser.postpone_cards()
    assert [(call["mode"], call["card_ids"]) for call in opened] == [
        (ap.ADVANCE, [11, 12]),
        (ap.POSTPONE, [11, 12]),
    ]


def _preview(card_ids: list[int], *, safe: int = 0, **counts: int) -> Any:
    return AdvancePostponePreview(
        card_ids=card_ids,
        retrievability_before=[0.9] * len(card_ids),
        retrievability_after=[0.8 + 0.01 * index for index in range(len(card_ids))],
        safe_count=safe,
        **counts,
    )


class FakeSched:
    def __init__(self, *, rwkv_curve: bool, preview: Any) -> None:
        self.rwkv_curve = rwkv_curve
        self.preview = preview
        self.requests: list[tuple[str, Any]] = []

    def advance_postpone_candidates(self, request: Any) -> Any:
        self.requests.append(("candidates", _copy(request)))
        return AdvancePostponeCandidates(
            card_ids=list(self.preview.card_ids) + [99], rwkv_curve=self.rwkv_curve
        )

    def preview_advance_postpone(self, request: Any) -> Any:
        self.requests.append(("preview", _copy(request)))
        return self.preview

    def advance_postpone(self, request: Any) -> Any:
        self.requests.append(("move", _copy(request)))
        response = AdvancePostponeResponse(count=len(request.card_ids))
        response.changes.study_queues = True
        return response


def _copy(request: Any) -> Any:
    copied = type(request)()
    copied.CopyFrom(request)
    return copied


def test_the_plan_for_a_deck_needs_no_rwkv_curves_under_fsrs7(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "rwkv_stored_curves_for_cards",
        lambda *_args, **_kwargs: pytest.fail("no RWKV under FSRS-7"),
    )
    sched = FakeSched(rwkv_curve=False, preview=_preview([1, 2, 3], safe=2))
    request = ap.scope_request(ap.POSTPONE, deck_id=5)
    plan = ap.gather_plan(cast(Any, SimpleNamespace(sched=sched)), _mw(), request)
    assert not plan.selected_cards and not plan.rwkv_curve
    assert list(plan.preview.card_ids) == [1, 2, 3]
    assert [kind for kind, _ in sched.requests] == ["candidates", "preview"]
    _, sent = sched.requests[1]
    assert (sent.mode, sent.deck_id, list(sent.card_ids)) == (ap.POSTPONE, 5, [])
    assert not sent.rwkv_curves


def test_rwkv_curve_moves_send_the_stored_curves(monkeypatch: Any) -> None:
    asked: list[list[int]] = []

    def curves(_mw: Any, card_ids: Any) -> tuple[list[int], bytes]:
        asked.append(list(card_ids))
        return [card_ids[0]], b"curve"

    monkeypatch.setattr(aqt.rwkv_scheduler, "rwkv_stored_curves_for_cards", curves)
    kept: list[list[int]] = []

    def keep_state(_mw: Any, card_ids: Any, mutate: Any) -> Any:
        kept.append(list(card_ids))
        return mutate()

    monkeypatch.setattr(
        aqt.rwkv_scheduler, "mutate_cards_keeping_rwkv_state", keep_state
    )
    sched = FakeSched(rwkv_curve=True, preview=_preview([1, 2, 3]))
    col = cast(Any, SimpleNamespace(sched=sched))
    plan = ap.gather_plan(
        col, _mw(), ap.scope_request(ap.ADVANCE, card_ids=[1, 2, 3, 99])
    )
    assert plan.rwkv_curve and plan.selected_cards
    # the preview gets the curves of every candidate
    assert asked == [[1, 2, 3, 99]]
    _, sent = sched.requests[1]
    assert (list(sent.rwkv_curve_card_ids), sent.rwkv_curves) == ([1], b"curve")

    # the move fetches the curves of the chosen cards again, and keeps the
    # resident RWKV state through the study-queue change
    op = ap.move_cards(
        parent=cast(Any, None),
        mw=_mw(),
        mode=ap.ADVANCE,
        card_ids=[2, 3],
        rwkv_curve=True,
    )
    response = op._op(col)
    assert response.count == 2
    assert asked[-1] == [2, 3] and kept == [[2, 3]]
    _, moved = sched.requests[-1]
    assert (moved.mode, list(moved.card_ids)) == (ap.ADVANCE, [2, 3])
    assert (list(moved.rwkv_curve_card_ids), moved.rwkv_curves) == ([2], b"curve")


def test_without_rwkv_curves_every_card_is_left_out(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        aqt.rwkv_scheduler, "rwkv_stored_curves_for_cards", lambda *_args: None
    )
    sched = FakeSched(rwkv_curve=True, preview=_preview([]))
    ap.gather_plan(
        cast(Any, SimpleNamespace(sched=sched)), _mw(), ap.scope_request(ap.ADVANCE)
    )
    _, sent = sched.requests[1]
    assert (list(sent.rwkv_curve_card_ids), sent.rwkv_curves) == ([], b"")


def test_the_move_is_one_collection_op_with_the_chosen_cards(monkeypatch: Any) -> None:
    moves: list[dict[str, Any]] = []

    class FakeOp:
        def run_in_background(self) -> None:
            pass

    def move_cards(**kwargs: Any) -> Any:
        moves.append(kwargs)
        return FakeOp()

    class FakeDialog:
        def __init__(self, _parent: Any, plan: Any) -> None:
            self.plan = plan

        def exec(self) -> bool:
            return True

        def count(self) -> int:
            return 2

    monkeypatch.setattr(ap, "move_cards", move_cards)
    monkeypatch.setattr(ap, "AdvancePostponeDialog", FakeDialog)
    plan = ap.AdvancePostponePlan(
        mode=ap.POSTPONE,
        selected_cards=False,
        rwkv_curve=False,
        preview=_preview([4, 5, 6]),
    )
    ap.ask_and_move(cast(Any, None), _mw(), plan)
    assert [(m["mode"], m["card_ids"], m["rwkv_curve"]) for m in moves] == [
        (ap.POSTPONE, [4, 5], False)
    ]


def test_the_op_moves_exactly_the_chosen_cards_under_fsrs7(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        aqt.rwkv_scheduler,
        "mutate_cards_keeping_rwkv_state",
        lambda *_args: pytest.fail("no RWKV under FSRS-7"),
    )
    sched = FakeSched(rwkv_curve=False, preview=_preview([]))
    op = ap.move_cards(
        parent=cast(Any, None),
        mw=_mw(),
        mode=ap.POSTPONE,
        card_ids=[7, 8],
        rwkv_curve=False,
    )
    assert isinstance(op, ap.CollectionOp)
    response = op._op(cast(Any, SimpleNamespace(sched=sched)))
    assert response.count == 2
    assert [kind for kind, _ in sched.requests] == ["move"]
    _, moved = sched.requests[0]
    assert (moved.mode, list(moved.card_ids), moved.HasField("deck_id")) == (
        ap.POSTPONE,
        [7, 8],
        False,
    )


def test_no_candidates_says_so_instead_of_asking(monkeypatch: Any) -> None:
    shown: list[str] = []
    monkeypatch.setattr(ap, "tooltip", lambda text, parent=None: shown.append(text))
    monkeypatch.setattr(
        ap, "AdvancePostponeDialog", lambda *_args: pytest.fail("no dialog")
    )
    plan = ap.AdvancePostponePlan(
        mode=ap.ADVANCE,
        selected_cards=False,
        rwkv_curve=True,
        preview=_preview([], without_curve=3),
    )
    ap.ask_and_move(cast(Any, None), _mw(), plan)
    assert [without_unicode_isolation(text) for text in shown] == [
        "There are no cards to advance.<br>3 cards were left out because"
        " RWKV-Curve has no forgetting curve for them yet."
    ]


def test_default_count_is_the_safe_count_for_a_deck_and_all_for_a_selection() -> None:
    def plan(selected: bool, safe: int, count: int) -> Any:
        return ap.AdvancePostponePlan(
            mode=ap.ADVANCE,
            selected_cards=selected,
            rwkv_curve=False,
            preview=_preview(list(range(count)), safe=safe),
        )

    assert ap.default_count(plan(False, 3, 50)) == 3
    assert ap.default_count(plan(False, 40, 50)) == 10
    assert ap.default_count(plan(False, 0, 50)) == 0
    assert ap.default_count(plan(True, 3, 50)) == 50


def test_the_effect_is_the_mean_retrievability_of_the_chosen_cards() -> None:
    def effect(count: int) -> str:
        return without_unicode_isolation(
            ap.effect_for_count(_preview([1, 2, 3]), count)
        )

    assert effect(0) == ""
    assert effect(2) == "Mean retrievability at review: 90.0% → 80.5%"
    assert effect(3) == "Mean retrievability at review: 90.0% → 81.0%"


def test_the_effect_line_explains_retrievability_on_hover() -> None:
    """Pins spec/ui.md#ui.retrievability-advanced-only: the dialog's effect
    line underlines the word and explains it on hover."""
    import os

    from aqt import retrievability
    from aqt.qt import QApplication

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    _app = QApplication.instance() or QApplication([])
    plan = ap.AdvancePostponePlan(
        mode=ap.ADVANCE,
        selected_cards=True,
        rwkv_curve=False,
        preview=_preview([1, 2]),
    )
    dialog = ap.AdvancePostponeDialog(None, plan)
    assert "<u>retrievability</u>" in dialog.effect.text()
    assert dialog.effect.toolTip() == retrievability.explanation()
