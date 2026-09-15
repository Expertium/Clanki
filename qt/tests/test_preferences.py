# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from copy import deepcopy
from unittest.mock import MagicMock, patch

from anki.collection import Preferences as PreferencesProto
from aqt.preferences import (
    Algorithm,
    Preferences,
    after_algorithm_change,
    ask_reschedule_after_algorithm_change,
)


def make_prefs() -> PreferencesProto:
    prefs = PreferencesProto()
    prefs.scheduling.learn_ahead_secs = 20 * 60
    prefs.scheduling.rollover = 4
    prefs.scheduling.apply_all_parent_limits = True
    prefs.scheduling.fsrs_reschedule = True
    prefs.scheduling.card_state_customizer = "// custom"
    prefs.scheduling.algorithm = Algorithm.RWKV_CURVE
    prefs.reviewing.show_remaining_due_counts = True
    prefs.reviewing.show_intervals_on_buttons = True
    prefs.reviewing.time_limit_secs = 0
    prefs.reviewing.hide_audio_play_buttons = False
    prefs.reviewing.interrupt_audio_when_answering = False
    prefs.reviewing.show_colored_buttons = True
    prefs.reviewing.two_button_mode = True
    prefs.reviewing.review_heatmap_enabled = True
    prefs.editing.adding_defaults_to_current_deck = True
    prefs.editing.paste_images_as_png = False
    prefs.editing.paste_strips_formatting = True
    prefs.editing.render_latex = False
    prefs.editing.default_search_text = ""
    prefs.editing.ignore_accents_in_search = True
    prefs.backups.daily = 2
    prefs.backups.weekly = 1
    prefs.backups.monthly = 1
    prefs.backups.minimum_interval_mins = 1
    return prefs


def make_form(prefs: PreferencesProto) -> MagicMock:
    """Build a form mock whose widgets report the values already in `prefs`."""
    form = MagicMock()
    form.lrnCutoff.value.return_value = int(prefs.scheduling.learn_ahead_secs / 60)
    form.dayOffset.value.return_value = prefs.scheduling.rollover
    form.applyAllParentLimits.isChecked.return_value = (
        prefs.scheduling.apply_all_parent_limits
    )
    form.fsrsReschedule.isChecked.return_value = prefs.scheduling.fsrs_reschedule
    form.customScheduling.toPlainText.return_value = (
        prefs.scheduling.card_state_customizer
    )
    form.algorithm.currentData.return_value = prefs.scheduling.algorithm
    form.showProgress.isChecked.return_value = prefs.reviewing.show_remaining_due_counts
    form.showEstimates.isChecked.return_value = (
        prefs.reviewing.show_intervals_on_buttons
    )
    form.showFuzzDelta.isChecked.return_value = (
        prefs.reviewing.show_fuzz_delta_on_buttons
    )
    form.showColoredButtons.isChecked.return_value = (
        prefs.reviewing.show_colored_buttons
    )
    form.twoButtonMode.isChecked.return_value = prefs.reviewing.two_button_mode
    form.timeLimit.value.return_value = int(prefs.reviewing.time_limit_secs / 60)
    form.showPlayButtons.isChecked.return_value = (
        not prefs.reviewing.hide_audio_play_buttons
    )
    form.interrupt_audio.isChecked.return_value = (
        prefs.reviewing.interrupt_audio_when_answering
    )
    form.useCurrent.currentIndex.return_value = (
        0 if prefs.editing.adding_defaults_to_current_deck else 1
    )
    form.pastePNG.isChecked.return_value = prefs.editing.paste_images_as_png
    form.paste_strips_formatting.isChecked.return_value = (
        prefs.editing.paste_strips_formatting
    )
    form.render_latex.isChecked.return_value = prefs.editing.render_latex
    form.default_search_text.text.return_value = prefs.editing.default_search_text
    form.ignore_accents_in_search.isChecked.return_value = (
        prefs.editing.ignore_accents_in_search
    )
    form.daily_backups.value.return_value = prefs.backups.daily
    form.weekly_backups.value.return_value = prefs.backups.weekly
    form.monthly_backups.value.return_value = prefs.backups.monthly
    form.minutes_between_backups.value.return_value = (
        prefs.backups.minimum_interval_mins
    )
    return form


def make_dialog(prefs: PreferencesProto, form: MagicMock) -> Preferences:
    dialog = Preferences.__new__(Preferences)
    dialog.mw = MagicMock()
    dialog.form = form
    dialog.prefs = prefs
    dialog.old_prefs = deepcopy(prefs)
    dialog.heatmap_tab = MagicMock()
    dialog.heatmap_tab.is_enabled.return_value = prefs.reviewing.review_heatmap_enabled
    dialog.heatmap_tab.save.return_value = False
    return dialog


@patch("aqt.preferences.set_preferences")
def test_update_collection_skips_backend_when_unchanged(
    mock_set_preferences: MagicMock,
) -> None:
    prefs = make_prefs()
    form = make_form(prefs)
    dialog = make_dialog(prefs, form)

    on_done = MagicMock()
    dialog.update_collection(on_done)

    mock_set_preferences.assert_not_called()
    dialog.mw.apply_collection_options.assert_called_once()
    on_done.assert_called_once()


@patch("aqt.preferences.set_preferences")
def test_update_collection_calls_backend_when_changed(
    mock_set_preferences: MagicMock,
) -> None:
    prefs = make_prefs()
    form = make_form(prefs)
    form.dayOffset.value.return_value = prefs.scheduling.rollover + 1
    dialog = make_dialog(prefs, form)

    on_done = MagicMock()
    dialog.update_collection(on_done)

    mock_set_preferences.assert_called_once_with(
        parent=dialog, preferences=dialog.prefs
    )
    op = mock_set_preferences.return_value
    op.success.assert_called_once()
    op.success.return_value.run_in_background.assert_called_once()

    # on_done() is only invoked once the backend op's success callback runs
    on_done.assert_not_called()
    success_callback = op.success.call_args.args[0]
    success_callback()
    dialog.mw.apply_collection_options.assert_called_once()
    on_done.assert_called_once()


# Pins spec/deck-options.md#deck-options.collection-wide-in-preferences
@patch("aqt.preferences.set_preferences")
def test_update_collection_writes_the_collection_wide_scheduling_settings(
    mock_set_preferences: MagicMock,
) -> None:
    prefs = make_prefs()
    form = make_form(prefs)
    form.applyAllParentLimits.isChecked.return_value = False
    form.fsrsReschedule.isChecked.return_value = False
    form.customScheduling.toPlainText.return_value = "// changed"
    dialog = make_dialog(prefs, form)

    dialog.update_collection(MagicMock())

    scheduling = dialog.prefs.scheduling
    assert scheduling.apply_all_parent_limits is False
    assert scheduling.fsrs_reschedule is False
    assert scheduling.card_state_customizer == "// changed"
    mock_set_preferences.assert_called_once_with(
        parent=dialog, preferences=dialog.prefs
    )


# Pins spec/ui.md#ui.review-heatmap
@patch("aqt.preferences.set_preferences")
def test_update_collection_writes_the_review_heatmap_preference(
    mock_set_preferences: MagicMock,
) -> None:
    prefs = make_prefs()
    form = make_form(prefs)
    dialog = make_dialog(prefs, form)
    dialog.heatmap_tab.is_enabled.return_value = False

    dialog.update_collection(MagicMock())

    assert dialog.prefs.reviewing.review_heatmap_enabled is False
    mock_set_preferences.assert_called_once_with(
        parent=dialog, preferences=dialog.prefs
    )


# Pins spec/scheduling.md#sched.algorithm-change-prompt
@patch("aqt.preferences.after_algorithm_change")
@patch("aqt.preferences.ask_reschedule_after_algorithm_change")
@patch("aqt.preferences.set_preferences")
def test_an_algorithm_change_asks_and_then_reschedules(
    mock_set_preferences: MagicMock, mock_ask: MagicMock, mock_after: MagicMock
) -> None:
    prefs = make_prefs()
    form = make_form(prefs)
    form.algorithm.currentData.return_value = Algorithm.FSRS7
    dialog = make_dialog(prefs, form)
    mock_ask.return_value = True

    dialog.update_collection(MagicMock())

    assert dialog.prefs.scheduling.algorithm == Algorithm.FSRS7
    mock_ask.assert_called_once_with(dialog, Algorithm.FSRS7)
    # the reschedule runs after the new algorithm is saved
    mock_after.assert_not_called()
    mock_set_preferences.return_value.success.call_args.args[0]()
    mock_after.assert_called_once_with(dialog.mw, Algorithm.FSRS7, True)


# Pins spec/scheduling.md#sched.algorithm-change-prompt
@patch("aqt.preferences.after_algorithm_change")
@patch("aqt.preferences.ask_reschedule_after_algorithm_change")
@patch("aqt.preferences.set_preferences")
def test_no_question_without_an_algorithm_change(
    mock_set_preferences: MagicMock, mock_ask: MagicMock, mock_after: MagicMock
) -> None:
    prefs = make_prefs()
    form = make_form(prefs)
    form.dayOffset.value.return_value = prefs.scheduling.rollover + 1
    dialog = make_dialog(prefs, form)

    dialog.update_collection(MagicMock())
    mock_set_preferences.return_value.success.call_args.args[0]()

    mock_ask.assert_not_called()
    mock_after.assert_not_called()


# Pins spec/scheduling.md#sched.algorithm-change-prompt
@patch("aqt.preferences.QMessageBox")
def test_no_question_for_rwkv_instant(mock_box: MagicMock) -> None:
    assert (
        ask_reschedule_after_algorithm_change(MagicMock(), Algorithm.RWKV_INSTANT)
        is False
    )
    mock_box.assert_not_called()


# Pins spec/scheduling.md#sched.algorithm-change-prompt
@patch("aqt.operations.CollectionOp")
@patch("aqt.rwkv_scheduler.reschedule_rwkv_review_cards_with_progress")
@patch("aqt.rwkv_scheduler.rwkv_instant_retention_did_change")
def test_after_an_algorithm_change_the_chosen_reschedule_runs(
    mock_refresh: MagicMock, mock_rwkv_reschedule: MagicMock, mock_op: MagicMock
) -> None:
    mw = MagicMock()

    after_algorithm_change(mw, Algorithm.RWKV_CURVE, False)
    mock_refresh.assert_called_once_with(mw)
    mock_rwkv_reschedule.assert_not_called()
    mock_op.assert_not_called()

    after_algorithm_change(mw, Algorithm.RWKV_CURVE, True)
    mock_rwkv_reschedule.assert_called_once_with(mw)

    after_algorithm_change(mw, Algorithm.FSRS7, True)
    col = MagicMock()
    mock_op.call_args.args[1](col)
    col._backend.reschedule_all_cards_with_fsrs7.assert_called_once_with()
    mock_op.return_value.run_in_background.assert_called_once()


# Pins spec/ui.md#ui.mode-switch
def test_the_algorithm_list_is_advanced_only() -> None:
    for advanced in (False, True):
        dialog = make_dialog(make_prefs(), MagicMock())
        dialog.mw.advanced_ui.return_value = advanced

        dialog.setup_algorithm(Algorithm.RWKV_CURVE)

        combo = dialog.form.algorithm
        assert [c.args[1] for c in combo.addItem.call_args_list] == [
            Algorithm.FSRS7,
            Algorithm.RWKV_CURVE,
            Algorithm.RWKV_INSTANT,
        ]
        combo.findData.assert_called_once_with(Algorithm.RWKV_CURVE)
        combo.setVisible.assert_called_once_with(advanced)
        dialog.form.algorithmLabel.setVisible.assert_called_once_with(advanced)
