# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import aqt.mediasrv
from anki.collection import OpChanges
from anki.decks import UpdateDeckConfigs
from aqt.deckoptions import (
    SchedulingAlgorithm,
    after_algorithm_change,
    ask_reschedule_after_algorithm_change,
)


def save_from_deck_options(input: UpdateDeckConfigs) -> dict[str, MagicMock]:
    """Run the deck-options save handler with its collaborators mocked; the
    save op itself is not run, its success callback is returned."""
    with ExitStack() as stack:
        mocks = {
            name: stack.enter_context(patch(target, create=create))
            for name, target, create in [
                ("mw", "aqt.mw", True),
                ("op", "aqt.mediasrv.update_deck_configs_op", False),
                ("ask", "aqt.mediasrv.ask_reschedule_after_algorithm_change", False),
                ("after", "aqt.mediasrv.after_algorithm_change", False),
                ("saved", "aqt.mediasrv._on_update_deck_configs_success", False),
                (
                    "snapshot",
                    "aqt.rwkv_scheduler.rwkv_curve_reschedule_snapshot",
                    False,
                ),
            ]
        }
        stack.enter_context(
            patch(
                "aqt.mediasrv.request",
                new=SimpleNamespace(data=input.SerializeToString()),
            )
        )
        mocks["mw"].taskman.run_on_main.side_effect = lambda fn: fn()
        aqt.mediasrv.update_deck_configs_and_close()
        mocks["on_success"] = mocks["op"].return_value.success.call_args.args[0]
        mocks["on_success"](OpChanges())
    return mocks


# Pins spec/scheduling.md#sched.algorithm-change-prompt
def test_an_algorithm_change_asks_and_then_reschedules() -> None:
    mocks = save_from_deck_options(
        UpdateDeckConfigs(scheduling_algorithm=SchedulingAlgorithm.FSRS7)
    )

    mw = mocks["mw"]
    mocks["ask"].assert_called_once_with(
        mw.app.activeModalWidget.return_value, SchedulingAlgorithm.FSRS7
    )
    # the answer replaces the RWKV reschedules of a retention change
    mocks["snapshot"].assert_not_called()
    assert mocks["saved"].call_args.kwargs == {"close_on_success": True}
    mocks["after"].assert_called_once_with(
        mw, SchedulingAlgorithm.FSRS7, mocks["ask"].return_value
    )


# Pins spec/scheduling.md#sched.algorithm-change-prompt
def test_no_question_without_an_algorithm_change() -> None:
    mocks = save_from_deck_options(UpdateDeckConfigs())

    mocks["ask"].assert_not_called()
    mocks["after"].assert_not_called()
    assert mocks["saved"].call_args.kwargs == {
        "close_on_success": True,
        "rwkv_snapshot": mocks["snapshot"].return_value,
    }


# Pins spec/scheduling.md#sched.algorithm-change-prompt
@patch("aqt.deckoptions.QMessageBox")
def test_no_question_for_rwkv_instant(mock_box: MagicMock) -> None:
    assert (
        ask_reschedule_after_algorithm_change(
            MagicMock(), SchedulingAlgorithm.RWKV_INSTANT
        )
        is False
    )
    mock_box.assert_not_called()


# Pins spec/scheduling.md#sched.algorithm-change-prompt
@patch("aqt.deckoptions.tr")
@patch("aqt.deckoptions.QMessageBox")
def test_the_question_offers_reschedule_or_keep(
    mock_box: MagicMock, _tr: MagicMock
) -> None:
    box = mock_box.return_value
    reschedule, keep = MagicMock(), MagicMock()
    box.addButton.side_effect = [reschedule, keep]

    box.clickedButton.return_value = reschedule
    assert ask_reschedule_after_algorithm_change(
        MagicMock(), SchedulingAlgorithm.RWKV_CURVE
    )
    # Esc and the default button keep the due dates
    box.setEscapeButton.assert_called_once_with(keep)
    box.setDefaultButton.assert_called_once_with(keep)

    box.addButton.side_effect = [reschedule, keep]
    box.clickedButton.return_value = keep
    assert not ask_reschedule_after_algorithm_change(
        MagicMock(), SchedulingAlgorithm.FSRS7
    )


# Pins spec/scheduling.md#sched.algorithm-change-prompt
@patch("aqt.operations.CollectionOp")
@patch("aqt.rwkv_scheduler.reschedule_rwkv_review_cards_with_progress")
@patch("aqt.rwkv_scheduler.rwkv_instant_retention_did_change")
def test_after_an_algorithm_change_the_chosen_reschedule_runs(
    mock_refresh: MagicMock, mock_rwkv_reschedule: MagicMock, mock_op: MagicMock
) -> None:
    mw = MagicMock()

    after_algorithm_change(mw, SchedulingAlgorithm.RWKV_CURVE, False)
    mock_refresh.assert_called_once_with(mw)
    mock_rwkv_reschedule.assert_not_called()
    mock_op.assert_not_called()

    after_algorithm_change(mw, SchedulingAlgorithm.RWKV_CURVE, True)
    mock_rwkv_reschedule.assert_called_once_with(mw)

    after_algorithm_change(mw, SchedulingAlgorithm.FSRS7, True)
    col = MagicMock()
    mock_op.call_args.args[1](col)
    col._backend.reschedule_all_cards_with_fsrs7.assert_called_once_with()
    mock_op.return_value.run_in_background.assert_called_once()
