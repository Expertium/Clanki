# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The notice shown when four answer buttons are turned on
(spec review.four-button-notice).

Turning "Show only Again and Good answer buttons" off gives the reviewer the
four buttons in the manual, where Hard is a passing grade with its own
interval. This notice repeats what the manual says about the four buttons at
the moment the switch happens, so nobody turns Hard into something it is not.

Its second button, "I want to use Hard as fail", runs away from the cursor
and does nothing when it is reached: it is a joke, and the only way back to
two buttons is the checkbox that turned them off.
"""

from __future__ import annotations

import random

import aqt
from anki.utils import dev_mode
from aqt.qt import *
from aqt.utils import disable_help_button, tr

#: pm.meta flag: the notice has been shown once (spec review.four-button-notice)
SHOWN_KEY = "fourButtonNoticeShown"

#: How close the cursor may come to the fleeing button, and how often it
#: looks, in milliseconds.
FLEE_GAP = 60
FLEE_CHECK_MS = 50

#: Room for the fleeing button to move around in, and the narrowest the
#: notice looks sensible at (the four paragraphs are long).
STRIP_HEIGHT = 96
MINIMUM_WIDTH = 520

_random = random.Random()


def _notice_html() -> str:
    """The manual's four buttons, as the notice shows them. Pure, so the
    text can be pinned without a window (spec review.four-button-notice)."""

    def entry(button: str, keys: str, description: str) -> str:
        return (
            f"<p><b>{button}</b> ({tr.preferences_four_button_notice_shortcut(keys=keys)})"
            f"<br>{description}</p>"
        )

    return (
        f"<p>{tr.preferences_four_button_notice_intro()}</p>"
        + entry(
            tr.studying_again(),
            "1",
            tr.preferences_four_button_notice_again(),
        )
        + entry(
            tr.studying_hard(),
            "2",
            tr.preferences_four_button_notice_hard(),
        )
        + entry(
            tr.studying_good(),
            "3, Space, Enter",
            tr.preferences_four_button_notice_good(),
        )
        + entry(
            tr.studying_easy(),
            "4",
            tr.preferences_four_button_notice_easy(),
        )
    )


def _cursor_gap(rect: QRect, cursor: QPoint) -> int:
    """How far the cursor is from the nearest edge of `rect`, or 0 when it is
    inside. Squared, so the whole calculation stays in whole numbers."""
    across = max(rect.left() - cursor.x(), 0, cursor.x() - rect.right())
    down = max(rect.top() - cursor.y(), 0, cursor.y() - rect.bottom())
    return across * across + down * down


def _flee_target(
    area: QRect, size: QSize, cursor: QPoint, rng: random.Random
) -> QPoint:
    """A spot for a widget of `size` inside `area`, as far from `cursor` as
    the room allows. Pure, so the rule that the button can never be reached
    is pinned without a mouse (spec review.four-button-notice)."""
    last_x = area.left() + max(area.width() - size.width(), 0)
    last_y = area.top() + max(area.height() - size.height(), 0)
    corners = [
        QPoint(x, y) for x in (area.left(), last_x) for y in (area.top(), last_y)
    ]
    gaps = [_cursor_gap(QRect(spot, size), cursor) for spot in corners]
    farthest = max(gaps)
    # any corner nearly as far away as the farthest one, so that the button
    # does not always land in the same spot
    best = [spot for spot, gap in zip(corners, gaps) if gap * 10 >= farthest * 9]
    return rng.choice(best)


class _FleeingButton(QPushButton):
    """The button that can never be clicked: it is moved out of the way
    before a click can land on it, and a click that does land is swallowed
    (spec review.four-button-notice)."""

    def __init__(self, area: _FleeArea, text: str) -> None:
        super().__init__(text, area)
        self.area = area
        self.setAutoDefault(False)
        self.setDefault(False)
        # out of the keyboard's way as well: Tab never reaches it, so Space
        # and Enter cannot press it either
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.resize(self.sizeHint())

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        # no super() call: nothing is emitted, so the button does nothing
        # even when the cursor catches it
        self.area.flee_from_the_real_cursor()


class _FleeArea(QWidget):
    """The strip the fleeing button lives in. It keeps the button inside
    itself and moves it whenever the cursor comes close
    (spec review.four-button-notice)."""

    def __init__(self, button_text: str) -> None:
        super().__init__()
        self.setMinimumHeight(STRIP_HEIGHT)
        self.button = _FleeingButton(self, button_text)
        self._timer = QTimer(self)
        self._timer.setInterval(FLEE_CHECK_MS)
        qconnect(self._timer.timeout, self.flee_from_the_real_cursor)

    def showEvent(self, event: QShowEvent | None) -> None:
        self._timer.start()
        self.flee_from_cursor(self._cursor_in_area(), force=True)
        super().showEvent(event)

    def hideEvent(self, event: QHideEvent | None) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        self.flee_from_cursor(self._cursor_in_area(), force=True)
        super().resizeEvent(event)

    def flee_from_the_real_cursor(self) -> None:
        """The timer's slot: move the button away from the mouse."""
        self.flee_from_cursor(self._cursor_in_area())

    def flee_from_cursor(self, cursor: QPoint, force: bool = False) -> bool:
        """Move the button out of `cursor`'s reach, `cursor` being in this
        strip's own coordinates. Returns whether the button moved."""
        button = QRect(self.button.pos(), self.button.size())
        if not force and _cursor_gap(button, cursor) > FLEE_GAP**2:
            return False
        target = _flee_target(self.rect(), self.button.size(), cursor, _random)
        self.button.move(target)
        self.button.raise_()
        return True

    def _cursor_in_area(self) -> QPoint:
        return self.mapFromGlobal(QCursor.pos())


def should_show(mw: aqt.AnkiQt) -> bool:
    """True the first time, and always in a development build, where the
    notice comes back on every switch for testing
    (spec review.four-button-notice)."""
    if dev_mode:
        return True
    return not mw.pm.meta.get(SHOWN_KEY, False)


def mark_shown(mw: aqt.AnkiQt) -> None:
    """Remember that the notice has been shown, so that it does not come
    back."""
    mw.pm.meta[SHOWN_KEY] = True
    mw.pm.save()


def show(mw: aqt.AnkiQt) -> QDialog | None:
    """Show the notice, or return None when it was shown before."""
    if not should_show(mw):
        return None
    if not dev_mode:
        mark_shown(mw)

    dialog = QDialog(mw, Qt.WindowType.Window)
    dialog.setWindowTitle(tr.preferences_four_button_notice_title())
    disable_help_button(dialog)
    mw.garbage_collect_on_dialog_finish(dialog)

    layout = QVBoxLayout()

    label = QLabel()
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setText(_notice_html())
    label.setWordWrap(True)
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextBrowserInteraction
        | Qt.TextInteractionFlag.TextSelectableByMouse
    )
    layout.addWidget(label)

    area = _FleeArea(tr.preferences_four_button_notice_hard_as_fail())
    layout.addWidget(area)

    button_box = QDialogButtonBox()
    understand = QPushButton(tr.preferences_four_button_notice_understand())
    understand.setDefault(True)
    button_box.addButton(understand, QDialogButtonBox.ButtonRole.AcceptRole)
    qconnect(button_box.accepted, dialog.accept)
    layout.addWidget(button_box)

    dialog.setLayout(layout)
    dialog.setMinimumWidth(MINIMUM_WIDTH)
    dialog.show()
    return dialog
