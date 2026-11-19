# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The notice shown when four answer buttons are turned on
(spec review.four-button-notice).

Turning "Show only Again and Good answer buttons" off gives the reviewer the
four buttons in the manual, where Hard is a passing grade with its own
interval. This notice repeats what the manual says about the four buttons at
the moment the switch happens, so nobody turns Hard into something it is not.

Its second button, "I want to use Hard as fail", starts straight above the
button that accepts the notice, flies away from the cursor whenever the cursor
comes close, and does nothing when it is reached: it is a joke, and the only
way back to two buttons is the checkbox that turned them off.
"""

from __future__ import annotations

import os
import random
from typing import cast

import aqt
from anki.utils import dev_mode
from aqt.qt import *
from aqt.utils import disable_help_button, tr

#: pm.meta flag: the notice has been shown once (spec review.four-button-notice)
SHOWN_KEY = "fourButtonNoticeShown"

#: set by tools/run.py: a run started by the source launcher is a developer's
#: build even when ANKIDEV is not set
DEV_RUN_ENV = "CLANKI_DEV_RUN"

#: How close the cursor may come to the fleeing button, and how often it
#: looks, in milliseconds.
FLEE_GAP = 60
FLEE_CHECK_MS = 50

#: How long the fleeing button takes to reach its new spot, so that it flies
#: there instead of jumping.
FLEE_MS = 220

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
    """The button that can never be clicked: it flies out of the way before a
    click can land on it, and a click that does land is swallowed
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
        self._flight = QPropertyAnimation(self, cast(QByteArray, b"pos"), self)
        self._flight.setDuration(FLEE_MS)
        self._flight.setEasingCurve(QEasingCurve.Type.OutCubic)

    def fly_to(self, target: QPoint) -> None:
        """Head for `target` from wherever the button is now, taking FLEE_MS
        over it, so that it flies instead of jumping there."""
        self._flight.stop()
        self._flight.setStartValue(self.pos())
        self._flight.setEndValue(target)
        self._flight.start()

    def is_flying(self) -> bool:
        return self._flight.state() == QAbstractAnimation.State.Running

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        # no super() call: nothing is emitted, so the button does nothing
        # even when the cursor catches it
        self.area.flee_from_the_real_cursor()


class _FleeArea(QWidget):
    """The strip the fleeing button lives in. It starts the button above the
    button that accepts the notice, keeps it inside itself and sends it off
    whenever the cursor comes close (spec review.four-button-notice)."""

    def __init__(self, button_text: str) -> None:
        super().__init__()
        self.setMinimumHeight(STRIP_HEIGHT)
        self.button = _FleeingButton(self, button_text)
        self._anchor: QWidget | None = None
        self._placed = False
        self._timer = QTimer(self)
        self._timer.setInterval(FLEE_CHECK_MS)
        qconnect(self._timer.timeout, self.flee_from_the_real_cursor)

    def start_above(self, widget: QWidget) -> None:
        """Put the button straight above `widget`, the button that accepts the
        notice, and leave it there until the cursor comes close."""
        self._anchor = widget
        self.place_above_anchor()

    def place_above_anchor(self) -> None:
        """Move the button to the middle of the anchor's column, as low in
        the strip as it goes, which puts it straight above the anchor."""
        if self._anchor is None:
            return
        anchor = self._anchor
        middle = self.mapFromGlobal(anchor.mapToGlobal(anchor.rect().center()))
        self.button.move(
            self._kept_inside(
                middle.x() - self.button.width() // 2,
                self.height() - self.button.height(),
            )
        )
        self.button.raise_()
        self._placed = True

    def showEvent(self, event: QShowEvent | None) -> None:
        self._timer.start()
        if self._placed:
            self.flee_from_cursor(self._cursor_in_area())
        else:
            self.place_above_anchor()
        super().showEvent(event)

    def hideEvent(self, event: QHideEvent | None) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        if not self._placed:
            self.place_above_anchor()
        elif not self.rect().contains(QRect(self.button.pos(), self.button.size())):
            # the strip shrank under the button: bring it back in
            self.flee_from_cursor(self._cursor_in_area(), force=True)
        super().resizeEvent(event)

    def flee_from_the_real_cursor(self) -> None:
        """The timer's slot: send the button away from the mouse."""
        self.flee_from_cursor(self._cursor_in_area())

    def flee_from_cursor(self, cursor: QPoint, force: bool = False) -> bool:
        """Send the button out of `cursor`'s reach, `cursor` being in this
        strip's own coordinates. Returns whether the button was sent off."""
        button = QRect(self.button.pos(), self.button.size())
        if not force and _cursor_gap(button, cursor) > FLEE_GAP**2:
            return False
        target = _flee_target(self.rect(), self.button.size(), cursor, _random)
        self.button.fly_to(target)
        return True

    def _kept_inside(self, x: int, y: int) -> QPoint:
        """`(x, y)` with the button kept inside the strip."""
        room = self.rect().adjusted(0, 0, -self.button.width(), -self.button.height())
        return QPoint(
            max(room.left(), min(x, room.right())),
            max(room.top(), min(y, room.bottom())),
        )

    def _cursor_in_area(self) -> QPoint:
        return self.mapFromGlobal(QCursor.pos())


class _NoticeDialog(QDialog):
    """The notice cannot be dismissed: it has no close button in its title bar
    and Esc and Alt+F4 do nothing, so accepting it is the only way out
    (spec review.four-button-notice)."""

    def closeEvent(self, event: QCloseEvent | None) -> None:
        if event is not None:
            event.ignore()

    def reject(self) -> None:
        pass


def _development_build() -> bool:
    """True in a developer's build: one started with ANKIDEV set (`run.bat`
    and `./run` set it), or by the source launcher, which marks its run so
    that a shortcut starting `tools/run.py` directly counts too. The notice
    repeats there, so that a developer sees it on every switch
    (spec review.four-button-notice)."""
    return bool(dev_mode) or os.environ.get(DEV_RUN_ENV) == "1"


def should_show(mw: aqt.AnkiQt) -> bool:
    """True the first time, and always in a development build, where the
    notice comes back on every switch for testing
    (spec review.four-button-notice)."""
    if _development_build():
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
    if not _development_build():
        mark_shown(mw)

    dialog = _NoticeDialog(mw, Qt.WindowType.Window)
    dialog.setWindowTitle(tr.preferences_four_button_notice_title())
    # no cross to click: the notice is accepted, not dismissed
    dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
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
    layout.activate()
    area.start_above(understand)
    return dialog
