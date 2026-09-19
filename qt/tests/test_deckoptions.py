# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import aqt.mediasrv
from anki.collection import OpChanges
from anki.decks import UpdateDeckConfigs
from aqt.deckoptions import (
    SchedulingAlgorithm,
    _DeckOptionsWebViews,
    after_algorithm_change,
    ask_reschedule_after_algorithm_change,
    on_deck_options_page_ready,
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


# The spare deck-options web view (qt/aqt/deckoptions.py, _DeckOptionsWebViews)
######################################################################


class FakeView:
    """Stands in for _DeckOptionsWebView: records loads and switches and
    never touches QtWebEngine."""

    def __init__(self) -> None:
        self.generation = 0
        self.owner = None
        self.show_on_ready = False
        self.loads: list[str] = []
        self.evals: list[str] = []
        self.shown = False
        self.cleaned_up = False

    def load_sveltekit_page(self, path: str) -> None:
        self.loads.append(path)

    def eval(self, js: str) -> None:
        self.evals.append(js)

    def hide_while_preserving_layout(self) -> None:
        self.shown = False

    def show(self) -> None:
        self.shown = True

    def cleanup(self) -> None:
        self.cleaned_up = True

    def deleteLater(self) -> None:
        pass


def fake_dialog(deck_id: int) -> MagicMock:
    dialog = MagicMock()
    dialog.deck_id = deck_id
    return dialog


@pytest.fixture
def views():
    """A fresh keeper with fake views, on a main window at the deck list;
    mw's timers are collected in `views.timers` instead of being started."""
    timers: list = []
    with (
        patch("aqt.deckoptions._DeckOptionsWebView", FakeView),
        patch("aqt.deckoptions.sip") as mock_sip,
        patch("aqt.deckoptions.theme_manager") as mock_theme,
        patch("aqt.rwkv_scheduler.rwkv_state_cache_loading") as loading,
        patch("aqt.mw", create=True) as mw,
    ):
        mock_sip.isdeleted.return_value = False
        mock_theme.night_mode = False
        loading.return_value = False
        mw.state = "deckBrowser"
        mw.app.activeModalWidget.return_value = None
        mw.col.decks.get_current_id.return_value = 9
        mw.progress.single_shot.side_effect = lambda ms, fn: timers.append(fn)
        keeper = _DeckOptionsWebViews()
        keeper.timers = timers  # type: ignore[attr-defined]
        keeper.loading = loading  # type: ignore[attr-defined]
        yield keeper


def page_ready(views: _DeckOptionsWebViews, web: FakeView) -> None:
    views.on_page_ready(web.generation)


def warmed_spare(views) -> FakeView:
    views.on_profile_did_open()
    views.timers.pop()()
    spare = views._spare
    page_ready(views, spare)
    return spare


def test_the_spare_waits_while_the_fsrs_prediction_pass_holds_the_collection(
    views,
) -> None:
    """The warm-up reads the current deck on the main thread; the pass holds
    the collection for seconds, so a warm-up then would freeze the window."""
    views.on_profile_did_open()
    with patch("aqt.fsrs_predictions.is_holding_collection", return_value=True):
        views.timers.pop()()
    assert views._spare is None
    # it tries again later, by itself
    views.timers.pop()()
    assert views._spare is not None


def test_without_a_spare_the_window_loads_its_deck(views) -> None:
    dialog = fake_dialog(5)
    web = views.take(dialog)
    assert web.loads == [f"deck-options/5?g={web.generation}"]
    assert not web.evals
    dialog.set_ready.assert_not_called()

    page_ready(views, web)
    dialog.set_ready.assert_called_once_with()


def test_the_spare_is_made_after_start_up_and_never_shown(views) -> None:
    views.on_profile_did_open()
    views.loading.return_value = True
    views.timers.pop()()
    # start-up work still running: try again later
    assert views._spare is None
    views.loading.return_value = False
    views.timers.pop()()
    spare = views._spare
    assert spare.loads == [f"deck-options/9?g={spare.generation}"]
    page_ready(views, spare)
    assert not spare.shown


def test_the_spare_switches_to_the_chosen_deck_and_reloads_its_data(views) -> None:
    spare = warmed_spare(views)
    dialog = fake_dialog(7)
    assert views.take(dialog) is spare
    # no new load: the page runs its loader again, for deck 7
    assert len(spare.loads) == 1
    assert spare.evals == [
        f'anki.deckOptionsSwitch("/deck-options/7?g={spare.generation}");'
    ]
    assert not spare.shown
    dialog.set_ready.assert_not_called()
    page_ready(views, spare)
    dialog.set_ready.assert_called_once_with()
    assert spare.shown


def test_a_spare_still_loading_gets_a_full_load(views) -> None:
    views.on_profile_did_open()
    views.timers.pop()()
    spare = views._spare
    stale = spare.generation
    dialog = fake_dialog(5)
    assert views.take(dialog) is spare
    assert spare.loads[-1] == f"deck-options/5?g={spare.generation}"
    assert not spare.evals
    # the ready signal of the warm-up load comes late: ignored
    views.on_page_ready(stale)
    dialog.set_ready.assert_not_called()
    page_ready(views, spare)
    dialog.set_ready.assert_called_once_with()


def test_each_view_serves_one_window_and_a_new_spare_follows(views) -> None:
    spare = warmed_spare(views)
    web = views.take(fake_dialog(5))
    page_ready(views, web)
    views.give_back(web)
    assert web.cleaned_up
    views.timers.pop()()
    new_spare = views._spare
    assert new_spare is not None and new_spare is not spare
    page_ready(views, new_spare)
    assert views.take(fake_dialog(6)) is new_spare
    assert new_spare.evals


def test_a_second_window_gets_its_own_view(views) -> None:
    first = views.take(fake_dialog(5))
    second_dialog = fake_dialog(6)
    second = views.take(second_dialog)
    assert second is not first
    page_ready(views, second)
    second_dialog.set_ready.assert_called_once_with()
    views.give_back(second)
    assert second.cleaned_up and not first.cleaned_up
    # no spare while a window is still open
    assert not views.timers


def test_the_spare_is_released_when_the_profile_closes(views) -> None:
    spare = warmed_spare(views)
    views.on_profile_will_close()
    assert spare.cleaned_up
    assert views.take(fake_dialog(5)) is not spare


def test_a_warm_up_of_a_closed_profile_does_nothing(views) -> None:
    views.on_profile_did_open()
    warm = views.timers.pop()
    views.on_profile_will_close()
    warm()
    assert views._spare is None


@pytest.mark.parametrize(
    "referrer, generation",
    [
        ("http://127.0.0.1:40000/deck-options/5?g=12#night", 12),
        ("http://127.0.0.1:40000/deck-options/5?g=3", 3),
        ("http://127.0.0.1:5173/", None),
        (None, None),
    ],
)
def test_the_generation_comes_from_the_page_url(referrer, generation) -> None:
    with patch("aqt.deckoptions._web_views") as keeper:
        on_deck_options_page_ready(referrer)
    keeper.on_page_ready.assert_called_once_with(generation)
