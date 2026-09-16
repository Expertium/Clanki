# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Advance and Postpone, ported from the FSRS Helper add-on (spec
sched.advance, sched.postpone, ui.advance-postpone).

The scheduling math is in the backend; this module gathers the preview in
the background, asks how many cards to move, and moves them as one undoable
background operation. Under RWKV-Curve it also fetches each card's stored
RWKV-Curve curve from the RWKV process, since the backend has no RWKV model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from anki.collection import Collection
from anki.scheduler.base import (
    AdvancePostponePreview,
    AdvancePostponeRequest,
    AdvancePostponeResponse,
)
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.utils import disable_help_button, tooltip, tr

ADVANCE = AdvancePostponeRequest.ADVANCE
POSTPONE = AdvancePostponeRequest.POSTPONE

# The add-on's default count when a deck is the scope (at most the safe count)
DEFAULT_COUNT = 10


def advance_postpone_available(mw: Any) -> bool:
    """Whether the actions show: Advanced mode only, and never under
    RWKV-Instant, which has no due dates to move (spec ui.advance-postpone)."""
    try:
        if not mw.advanced_ui():
            return False
        return mw.col.get_config("schedulingAlgorithm", None) != "rwkvInstant"
    except Exception:
        return False


def add_deck_menu_actions(menu: QMenu, mw: Any, deck_id: int) -> None:
    """The deck menu's (the gear next to a deck) Advance and Postpone entries,
    for the deck and its subdecks."""
    if not advance_postpone_available(mw):
        return
    for mode, label in (
        (ADVANCE, tr.actions_advance_cards()),
        (POSTPONE, tr.actions_postpone_cards()),
    ):
        action = menu.addAction(tr.actions_with_ellipsis(action=label))
        assert action is not None
        qconnect(
            action.triggered,
            lambda _checked=False, mode=mode: advance_postpone(
                parent=mw, mw=mw, mode=mode, deck_id=deck_id
            ),
        )


@dataclass
class AdvancePostponePlan:
    mode: int
    # the cards chosen for the preview: the Browser's selection
    selected_cards: bool
    rwkv_curve: bool
    preview: AdvancePostponePreview


def scope_request(
    mode: int,
    *,
    deck_id: int | None = None,
    card_ids: Sequence[int] = (),
) -> AdvancePostponeRequest:
    request = AdvancePostponeRequest(mode=cast(Any, mode))
    if card_ids:
        request.card_ids.extend(card_ids)
    elif deck_id is not None:
        request.deck_id = deck_id
    return request


def add_rwkv_curves(
    mw: Any, request: AdvancePostponeRequest, card_ids: Sequence[int]
) -> None:
    """Attach RWKV-Curve's stored curves of `card_ids`; without them (RWKV not
    ready, no model) every card counts as one without a curve."""
    from aqt.rwkv_scheduler import rwkv_stored_curves_for_cards

    curves = rwkv_stored_curves_for_cards(mw, card_ids)
    del request.rwkv_curve_card_ids[:]
    request.rwkv_curves = b""
    if curves is not None:
        ids, packed = curves
        request.rwkv_curve_card_ids.extend(ids)
        request.rwkv_curves = packed


def gather_plan(
    col: Collection, mw: Any, request: AdvancePostponeRequest
) -> AdvancePostponePlan:
    """Runs in the background: the candidates, their curves, the preview."""
    candidates = col.sched.advance_postpone_candidates(request)
    if candidates.rwkv_curve:
        add_rwkv_curves(mw, request, candidates.card_ids)
    return AdvancePostponePlan(
        mode=request.mode,
        selected_cards=bool(request.card_ids),
        rwkv_curve=candidates.rwkv_curve,
        preview=col.sched.preview_advance_postpone(request),
    )


def advance_postpone(
    *,
    parent: QWidget,
    mw: Any,
    mode: int,
    deck_id: int | None = None,
    card_ids: Sequence[int] = (),
) -> None:
    """Show the Advance or Postpone dialog for a deck (and its subdecks) or for
    the given cards, then move the chosen number of cards."""
    request = scope_request(mode, deck_id=deck_id, card_ids=card_ids)
    QueryOp(
        parent=parent,
        op=lambda col: gather_plan(col, mw, request),
        success=lambda plan: ask_and_move(parent, mw, plan),
    ).with_progress().run_in_background()


def skipped_notes(mode: int, preview: Any, rwkv_curve: bool) -> list[str]:
    notes = []
    if preview.without_curve:
        notes.append(
            tr.scheduling_advance_postpone_without_curve(
                count=preview.without_curve,
                algorithm="RWKV-Curve" if rwkv_curve else "FSRS-7",
            )
        )
    if mode == POSTPONE and preview.at_maximum_interval:
        notes.append(
            tr.scheduling_postpone_at_maximum_interval(
                count=preview.at_maximum_interval
            )
        )
    return notes


def effect_text(before: float, after: float) -> str:
    return tr.scheduling_advance_postpone_effect(
        before=f"{before * 100:.1f}%", after=f"{after * 100:.1f}%"
    )


def effect_for_count(preview: AdvancePostponePreview, count: int) -> str:
    """The mean retrievability at review of the first `count` cards, without
    and with the move."""
    if count <= 0:
        return ""
    before = sum(preview.retrievability_before[:count]) / count
    after = sum(preview.retrievability_after[:count]) / count
    return effect_text(before, after)


def default_count(plan: AdvancePostponePlan) -> int:
    """Every eligible card of a Browser selection; for a deck, the add-on's
    default: the safe count, at most 10."""
    available = len(plan.preview.card_ids)
    if plan.selected_cards:
        return available
    return min(plan.preview.safe_count, DEFAULT_COUNT, available)


class AdvancePostponeDialog(QDialog):
    """How many cards to move, with the effect on their retrievability."""

    def __init__(self, parent: QWidget | None, plan: AdvancePostponePlan) -> None:
        super().__init__(parent)
        self.plan = plan
        preview = plan.preview
        advance = plan.mode == ADVANCE
        available = len(preview.card_ids)
        self.setWindowTitle(
            tr.actions_advance_cards() if advance else tr.actions_postpone_cards()
        )
        disable_help_button(self)

        layout = QVBoxLayout(self)
        texts = [
            (
                tr.scheduling_advance_available
                if advance
                else tr.scheduling_postpone_available
            )(count=available)
            + " "
            + (tr.scheduling_advance_safe if advance else tr.scheduling_postpone_safe)(
                count=preview.safe_count
            ),
            tr.scheduling_advance_postpone_warning(),
            *skipped_notes(plan.mode, preview, plan.rwkv_curve),
        ]
        for text in texts:
            label = QLabel(text)
            label.setWordWrap(True)
            layout.addWidget(label)

        row = QHBoxLayout()
        row.addWidget(
            QLabel(
                tr.scheduling_advance_count()
                if advance
                else tr.scheduling_postpone_count()
            )
        )
        self.spin = QSpinBox()
        self.spin.setRange(0, available)
        self.spin.setValue(default_count(plan))
        row.addWidget(self.spin)
        row.addStretch()
        layout.addLayout(row)

        self.effect = QLabel()
        layout.addWidget(self.effect)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        qconnect(self.buttons.accepted, self.accept)
        qconnect(self.buttons.rejected, self.reject)
        layout.addWidget(self.buttons)

        qconnect(self.spin.valueChanged, self.update_effect)
        self.update_effect()

    def count(self) -> int:
        return self.spin.value()

    def update_effect(self) -> None:
        count = self.count()
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert ok is not None
        ok.setEnabled(count > 0)
        self.effect.setText(effect_for_count(self.plan.preview, count))


def ask_and_move(parent: QWidget, mw: Any, plan: AdvancePostponePlan) -> None:
    if not plan.preview.card_ids:
        tooltip(
            "<br>".join(
                [
                    (
                        tr.scheduling_advance_no_cards()
                        if plan.mode == ADVANCE
                        else tr.scheduling_postpone_no_cards()
                    ),
                    *skipped_notes(plan.mode, plan.preview, plan.rwkv_curve),
                ]
            ),
            parent=parent,
        )
        return
    dialog = AdvancePostponeDialog(parent, plan)
    if not dialog.exec() or dialog.count() == 0:
        return
    move_cards(
        parent=parent,
        mw=mw,
        mode=plan.mode,
        card_ids=list(plan.preview.card_ids[: dialog.count()]),
        rwkv_curve=plan.rwkv_curve,
    ).run_in_background()


def move_cards(
    *,
    parent: QWidget,
    mw: Any,
    mode: int,
    card_ids: Sequence[int],
    rwkv_curve: bool,
) -> CollectionOp[AdvancePostponeResponse]:
    """The move of the chosen cards, as one undoable background operation."""
    request = scope_request(mode, card_ids=card_ids)

    def op(col: Collection) -> AdvancePostponeResponse:
        if not rwkv_curve:
            return col.sched.advance_postpone(request)
        from aqt.rwkv_scheduler import mutate_cards_keeping_rwkv_state

        add_rwkv_curves(mw, request, card_ids)
        return mutate_cards_keeping_rwkv_state(
            mw, card_ids, lambda: col.sched.advance_postpone(request)
        )

    def done(response: AdvancePostponeResponse) -> None:
        text = (
            tr.scheduling_advance_done(count=response.count)
            if mode == ADVANCE
            else tr.scheduling_postpone_done(count=response.count)
        )
        if response.count:
            text += "<br>" + effect_text(
                response.retrievability_before, response.retrievability_after
            )
        tooltip(text, parent=parent)

    return CollectionOp(parent, op).success(done)
