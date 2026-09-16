# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TypeVar

import aqt
import aqt.forms
from anki.cards import CardId
from anki.collection import (
    CARD_TYPE_NEW,
    Collection,
    Config,
    OpChanges,
    OpChangesWithCount,
    OpChangesWithId,
)
from anki.decks import DeckId, FilteredDeckConfig
from anki.notes import NoteId
from anki.scheduler import CustomStudyRequest, FilteredDeckForUpdate, UnburyDeck
from anki.scheduler.base import ScheduleCardsAsNew
from anki.scheduler.v3 import CardAnswer, GradeNowCardOptions
from anki.scheduler.v3 import Scheduler as V3Scheduler
from aqt.operations import CollectionOp
from aqt.qt import *
from aqt.utils import disable_help_button, getText, tooltip, tr

_T = TypeVar("_T")


def _run_preserving_rwkv_state(
    col: Collection,
    mutation: Callable[[], _T],
    *,
    card_ids: Sequence[int] = (),
    note_ids: Sequence[int] = (),
    require_no_preset_overlay: bool = False,
) -> _T:
    from aqt import rwkv_scheduler

    return rwkv_scheduler.run_collection_mutation_preserving_rwkv_state(
        col,
        mutation,
        card_ids=card_ids,
        note_ids=note_ids,
        require_no_preset_overlay=require_no_preset_overlay,
    )


def set_due_date_dialog(
    *,
    parent: QWidget,
    card_ids: Sequence[CardId],
    config_key: Config.String.V | None,
) -> CollectionOp[OpChanges] | None:
    assert aqt.mw
    if not card_ids:
        return None

    default_text = (
        aqt.mw.col.get_config_string(config_key) if config_key is not None else ""
    )
    prompt = "\n".join(
        [
            tr.scheduling_set_due_date_prompt(cards=len(card_ids)),
            tr.scheduling_set_due_date_prompt_hint(),
        ]
    )
    (days, success) = getText(
        prompt=prompt,
        parent=parent,
        default=default_text,
        title=tr.actions_set_due_date(),
    )
    if not success or not days.strip():
        return None
    else:
        return CollectionOp(
            parent,
            lambda col: _run_preserving_rwkv_state(
                col,
                lambda: col.sched.set_due_date(card_ids, days, config_key),
                card_ids=card_ids,
            ),
        ).success(
            lambda _: tooltip(
                tr.scheduling_set_due_date_done(cards=len(card_ids)),
                parent=parent,
            )
        )


@dataclass(frozen=True)
class GradeNowResult:
    changes: OpChanges
    answered_card_ids: tuple[CardId, ...]
    # RWKV-Curve gave no intervals for these cards, so they were not answered
    unanswered_card_ids: tuple[CardId, ...]


def grade_cards_now(
    col: Collection,
    card_ids: Sequence[int],
    ease: int,
    card_options: Sequence[GradeNowCardOptions] = (),
    *,
    reviewer: object | None = None,
) -> GradeNowResult:
    """Answer each card with `ease` (1 Again, 2 Hard, 3 Good, 4 Easy) as the
    reviewer would, in one undoable step, without the GUI (spec
    sched.grade-now-rwkv-curve). Under RWKV-Curve a card RWKV-Curve gives no
    intervals for is left unanswered and listed in `unanswered_card_ids`.
    Blocks while RWKV predicts, so call it off the main thread.

    `reviewer` is the running reviewer, whose RWKV state follows the
    answers; by default the main window's when `col` is its collection.
    """
    from aqt import rwkv_scheduler

    if reviewer is None:
        mw = aqt.mw
        reviewer = (
            getattr(mw, "reviewer", None) or SimpleNamespace(mw=mw)
            if mw is not None and getattr(mw, "col", None) is col
            else SimpleNamespace(mw=SimpleNamespace(col=col))
        )
    cards = rwkv_scheduler.rwkv_grade_now_cards(
        reviewer, card_ids, ease, tuple(card_options)
    )
    answered = tuple(CardId(card_id) for card_id in cards.answered_card_ids)
    unanswered = tuple(CardId(card_id) for card_id in cards.unanswered_card_ids)
    if not answered:
        return GradeNowResult(OpChanges(), answered, unanswered)

    reconciliation = rwkv_scheduler.prepare_grade_now_reconciliation(reviewer, answered)
    changes = col._backend.grade_now(
        card_ids=answered,
        rating=rwkv_scheduler.grade_now_rating(ease),
        card_options=cards.card_options,
    )
    rwkv_scheduler.record_grade_now_answers(reconciliation)
    return GradeNowResult(changes, answered, unanswered)


def grade_now(
    *,
    parent: QWidget,
    card_ids: Sequence[CardId],
    ease: int,
    card_options: Sequence[GradeNowCardOptions] | None = None,
) -> CollectionOp[OpChanges]:
    card_ids = tuple(card_ids)
    card_options = tuple(card_options or ())
    result: list[GradeNowResult] = []

    def grade_now_v3(col: Collection) -> OpChanges:
        result.append(grade_cards_now(col, card_ids, ease, card_options))
        return result[0].changes

    def done(_: OpChanges) -> None:
        lines = [
            tr.scheduling_graded_cards_done(cards=len(result[0].answered_card_ids))
        ]
        if skipped := len(result[0].unanswered_card_ids):
            lines.append(tr.qt_misc_rwkv_curve_grade_now_skipped(cards=skipped))
        tooltip("<br>".join(lines), parent=parent)

    return CollectionOp(parent, grade_now_v3).success(done)


def forget_cards(
    *,
    parent: QWidget,
    card_ids: Sequence[CardId],
    context: ScheduleCardsAsNew.Context.V | None = None,
) -> CollectionOp[OpChanges] | None:
    assert aqt.mw

    dialog = QDialog(parent)
    disable_help_button(dialog)
    form = aqt.forms.forget.Ui_Dialog()
    form.setupUi(dialog)

    if context is not None:
        defaults = aqt.mw.col.sched.schedule_cards_as_new_defaults(context)
        form.restore_position.setChecked(defaults.restore_position)
        form.reset_counts.setChecked(defaults.reset_counts)

    if not dialog.exec():
        return None

    restore_position = form.restore_position.isChecked()
    reset_counts = form.reset_counts.isChecked()

    return CollectionOp(
        parent,
        lambda col: _run_preserving_rwkv_state(
            col,
            lambda: col.sched.schedule_cards_as_new(
                card_ids,
                restore_position=restore_position,
                reset_counts=reset_counts,
                context=context,
            ),
            card_ids=card_ids,
        ),
    ).success(
        lambda _: tooltip(
            tr.scheduling_forgot_cards(cards=len(card_ids)), parent=parent
        )
    )


def reposition_new_cards_dialog(
    *,
    parent: QWidget,
    card_ids: Sequence[CardId],
) -> CollectionOp[OpChangesWithCount] | None:
    from aqt import mw

    assert mw
    assert mw.col.db

    row = mw.col.db.first(
        f"select min(due), max(due) from cards where type={CARD_TYPE_NEW} and odid=0"
    )
    assert row
    (min_position, max_position) = row
    min_position = max(min_position or 0, 0)
    max_position = max_position or 0

    dialog = QDialog(parent)
    disable_help_button(dialog)
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    form = aqt.forms.reposition.Ui_Dialog()
    form.setupUi(dialog)

    txt = tr.browsing_queue_top(val=min_position)
    txt += "\n" + tr.browsing_queue_bottom(val=max_position)
    form.label.setText(txt)

    form.start.selectAll()

    defaults = mw.col.sched.reposition_defaults()
    form.randomize.setChecked(defaults.random)
    form.shift.setChecked(defaults.shift)

    if not dialog.exec():
        return None

    start = form.start.value()
    step = form.step.value()
    randomize = form.randomize.isChecked()
    shift = form.shift.isChecked()

    return reposition_new_cards(
        parent=parent,
        card_ids=card_ids,
        starting_from=start,
        step_size=step,
        randomize=randomize,
        shift_existing=shift,
    )


def reposition_new_cards(
    *,
    parent: QWidget,
    card_ids: Sequence[CardId],
    starting_from: int,
    step_size: int,
    randomize: bool,
    shift_existing: bool,
) -> CollectionOp[OpChangesWithCount]:
    return CollectionOp(
        parent,
        lambda col: _run_preserving_rwkv_state(
            col,
            lambda: col.sched.reposition_new_cards(
                card_ids=card_ids,
                starting_from=starting_from,
                step_size=step_size,
                randomize=randomize,
                shift_existing=shift_existing,
            ),
            card_ids=card_ids,
            require_no_preset_overlay=shift_existing,
        ),
    ).success(
        lambda out: tooltip(
            tr.browsing_changed_new_position(count=out.count), parent=parent
        )
    )


def suspend_cards(
    *,
    parent: QWidget,
    card_ids: Sequence[CardId],
) -> CollectionOp[OpChangesWithCount]:
    return CollectionOp(
        parent,
        lambda col: _run_preserving_rwkv_state(
            col,
            lambda: col.sched.suspend_cards(card_ids),
            card_ids=card_ids,
        ),
    )


def suspend_note(
    *,
    parent: QWidget,
    note_ids: Sequence[NoteId],
) -> CollectionOp[OpChangesWithCount]:
    return CollectionOp(
        parent,
        lambda col: _run_preserving_rwkv_state(
            col,
            lambda: col.sched.suspend_notes(note_ids),
            note_ids=note_ids,
        ),
    )


def unsuspend_cards(
    *, parent: QWidget, card_ids: Sequence[CardId]
) -> CollectionOp[OpChanges]:
    return CollectionOp(
        parent,
        lambda col: _run_preserving_rwkv_state(
            col,
            lambda: col.sched.unsuspend_cards(card_ids),
            card_ids=card_ids,
        ),
    )


def bury_cards(
    *,
    parent: QWidget,
    card_ids: Sequence[CardId],
) -> CollectionOp[OpChangesWithCount]:
    return CollectionOp(
        parent,
        lambda col: _run_preserving_rwkv_state(
            col,
            lambda: col.sched.bury_cards(card_ids),
            card_ids=card_ids,
        ),
    )


def bury_notes(
    *,
    parent: QWidget,
    note_ids: Sequence[NoteId],
) -> CollectionOp[OpChangesWithCount]:
    return CollectionOp(
        parent,
        lambda col: _run_preserving_rwkv_state(
            col,
            lambda: col.sched.bury_notes(note_ids),
            note_ids=note_ids,
        ),
    )


def unbury_cards(
    *, parent: QWidget, card_ids: Sequence[CardId]
) -> CollectionOp[OpChanges]:
    return CollectionOp(
        parent,
        lambda col: _run_preserving_rwkv_state(
            col,
            lambda: col.sched.unbury_cards(card_ids),
            card_ids=card_ids,
        ),
    )


def rebuild_filtered_deck(
    *, parent: QWidget, deck_id: DeckId
) -> CollectionOp[OpChangesWithCount]:
    return CollectionOp(parent, lambda col: _rebuild_filtered_deck(col, deck_id))


def empty_filtered_deck(*, parent: QWidget, deck_id: DeckId) -> CollectionOp[OpChanges]:
    return CollectionOp(
        parent,
        lambda col: _run_preserving_rwkv_state(
            col,
            lambda: col.sched.empty_filtered_deck(deck_id),
            require_no_preset_overlay=True,
        ),
    )


def add_or_update_filtered_deck(
    *,
    parent: QWidget,
    deck: FilteredDeckForUpdate,
) -> CollectionOp[OpChangesWithId]:
    return CollectionOp(parent, lambda col: _add_or_update_filtered_deck(col, deck))


def _rebuild_filtered_deck(
    col: Collection,
    deck_id: DeckId,
) -> OpChangesWithCount:
    deck = col.sched.get_or_create_filtered_deck(deck_id=deck_id)
    _prepare_filtered_deck_retrievability_scores(col, deck.config)
    return _run_preserving_rwkv_state(
        col,
        lambda: col.sched.rebuild_filtered_deck(deck_id),
        require_no_preset_overlay=True,
    )


def _add_or_update_filtered_deck(
    col: Collection,
    deck: FilteredDeckForUpdate,
) -> OpChangesWithId:
    _prepare_filtered_deck_retrievability_scores(col, deck.config)
    return _run_preserving_rwkv_state(
        col,
        lambda: col.sched.add_or_update_filtered_deck(deck),
        require_no_preset_overlay=True,
    )


def _prepare_filtered_deck_retrievability_scores(
    col: Collection,
    config: FilteredDeckConfig,
) -> None:
    from aqt import rwkv_scheduler

    mw = aqt.mw
    if mw is not None and mw.col is col:
        reviewer = getattr(mw, "reviewer", None) or SimpleNamespace(mw=mw)
    else:
        reviewer = SimpleNamespace(mw=SimpleNamespace(col=col))
    status = rwkv_scheduler.prepare_filtered_deck_retrievability_scores(
        reviewer,
        config,
    )
    if status in {
        rwkv_scheduler.RwkvStatsPreparationStatus.PENDING,
        rwkv_scheduler.RwkvStatsPreparationStatus.FAILED,
    }:
        raise RuntimeError(_filtered_deck_preparation_failed_message(col))


def _filtered_deck_preparation_failed_message(col: Collection) -> str:
    """Simple mode never says "retrievability" (spec ui.simple-recall-wording)."""
    if col.get_config_bool(Config.Bool.ADVANCED_UI):
        return tr.qt_misc_rwkv_filtered_deck_preparation_failed()
    return tr.qt_misc_rwkv_filtered_deck_preparation_failed_simple()


def unbury_deck(
    *,
    parent: QWidget,
    deck_id: DeckId,
    mode: UnburyDeck.Mode.V = UnburyDeck.ALL,
) -> CollectionOp[OpChanges]:
    return CollectionOp(
        parent,
        lambda col: _run_preserving_rwkv_state(
            col,
            lambda: col.sched.unbury_deck(deck_id=deck_id, mode=mode),
            require_no_preset_overlay=True,
        ),
    )


def answer_card(
    *,
    parent: QWidget,
    answer: CardAnswer,
    after_answer: Callable[[], None] | None = None,
) -> CollectionOp[OpChanges]:
    def answer_v3(col: Collection) -> OpChanges:
        assert isinstance(col.sched, V3Scheduler)
        changes = col.sched.answer_card(answer)
        if after_answer is not None:
            after_answer()
        return changes

    return CollectionOp(parent, answer_v3)


def custom_study(
    *,
    parent: QWidget,
    request: CustomStudyRequest,
) -> CollectionOp[OpChanges]:
    return CollectionOp(
        parent,
        lambda col: _run_preserving_rwkv_state(
            col,
            lambda: col.sched.custom_study(request),
            require_no_preset_overlay=True,
        ),
    )
