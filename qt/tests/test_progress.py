# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins the Cancel button of spec/ui.md#ui.close-says-what-it-waits-for.

Escape and the title-bar X have always set the cancel flag on every progress
window. The button makes that visible, and only on the waits that ask for it.
"""

from __future__ import annotations

import anki.lang

anki.lang.set_lang("en")

import aqt.progress  # noqa: E402


def _dialog() -> aqt.progress.ProgressDialog:
    """A ProgressDialog with only what the button code touches.

    The real one builds Qt widgets, which needs a QApplication; these tests
    are about which flag the button sets, not about how it is drawn.
    """
    dialog = aqt.progress.ProgressDialog.__new__(aqt.progress.ProgressDialog)
    dialog.wantCancel = False
    dialog._cancel_button = None
    return dialog


def test_a_progress_window_has_no_cancel_button_unless_asked() -> None:
    dialog = _dialog()

    assert dialog._cancel_button is None


def test_the_cancel_button_sets_the_same_flag_as_escape() -> None:
    dialog = _dialog()

    dialog._on_cancel_clicked()

    assert dialog.wantCancel is True


def test_the_start_signature_carries_a_cancel_label() -> None:
    """`progress.start` and `taskman.with_progress` both take the label, so a
    caller that wants a button can ask for one without touching the dialog."""
    import inspect

    import aqt.taskman

    for function in (
        aqt.progress.ProgressManager.start,
        aqt.taskman.TaskManager.with_progress,
    ):
        parameters = inspect.signature(function).parameters
        assert "cancel_label" in parameters, function.__qualname__
        # nothing gains a button by default
        assert parameters["cancel_label"].default is None
