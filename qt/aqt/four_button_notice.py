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
comes close, around it rather than across it, and does nothing when it is
reached: it is a joke, and the only way back to two buttons is the checkbox
that turned them off.
"""

from __future__ import annotations

import os
import random
from math import hypot

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
FLEE_GAP = 25
FLEE_CHECK_MS = 25

#: How long the fleeing button takes to reach its new spot, so that it flies
#: there instead of jumping.
FLEE_MS = 220

#: How finely a flight is looked at when deciding whether it would carry the
#: button across the cursor.
FLIGHT_STEPS = 12

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


def _flight_gap(start: QPoint, target: QPoint, size: QSize, cursor: QPoint) -> int:
    """The closest the cursor comes to a button of `size` while it moves from
    `start` to `target`, squared: the smallest gap anywhere along the way,
    looked at step by step. Pure, so the rule that the button flies around
    the cursor instead of across it is pinned without a mouse
    (spec review.four-button-notice)."""
    worst = _cursor_gap(QRect(start, size), cursor)
    for step in range(1, FLIGHT_STEPS + 1):
        moved = QPoint(
            start.x() + (target.x() - start.x()) * step // FLIGHT_STEPS,
            start.y() + (target.y() - start.y()) * step // FLIGHT_STEPS,
        )
        worst = min(worst, _cursor_gap(QRect(moved, size), cursor))
    return worst


def _route_gap(
    start: QPoint, way: QPoint, target: QPoint, size: QSize, cursor: QPoint
) -> int:
    """The same for a flight that goes by `way`: for a straight flight `way`
    is `target`."""
    worst = _flight_gap(start, way, size, cursor)
    if way != target:
        worst = min(worst, _flight_gap(way, target, size, cursor))
    return worst


def _far_corners(area: QRect, size: QSize, cursor: QPoint) -> list[QPoint]:
    """The spots for a widget of `size` inside `area` that leave the cursor
    as far off as the room allows. Pure, so the rule that the button can
    never be reached is pinned without a mouse
    (spec review.four-button-notice)."""
    last_x = area.left() + max(area.width() - size.width(), 0)
    last_y = area.top() + max(area.height() - size.height(), 0)
    corners = [
        QPoint(x, y) for x in (area.left(), last_x) for y in (area.top(), last_y)
    ]
    gaps = [_cursor_gap(QRect(spot, size), cursor) for spot in corners]
    farthest = max(gaps)
    # any corner nearly as far away as the farthest one, so that the button
    # does not always land in the same spot
    return [spot for spot, gap in zip(corners, gaps) if gap * 10 >= farthest * 9]


def _flee_route(
    area: QRect, start: QPoint, size: QSize, cursor: QPoint, rng: random.Random
) -> tuple[QPoint, QPoint]:
    """Where the button goes when the cursor comes close: a far spot inside
    `area` for a button of `size` that is at `start`, and the spot to go by on
    the way, which is the far spot itself when flying straight there is the
    clearest route. The button takes a flight that keeps the cursor at least
    as far off along the way as it stands now, and where every flight would
    come closer -- that is, carry the button across the cursor -- it stays
    where it is, the spot it is at being returned twice, so that it never
    moves towards the cursor. Pure, so the rule that the button can never be
    reached is pinned without a mouse (spec review.four-button-notice)."""
    last_x = area.left() + max(area.width() - size.width(), 0)
    last_y = area.top() + max(area.height() - size.height(), 0)
    corners = [
        QPoint(x, y) for x in (area.left(), last_x) for y in (area.top(), last_y)
    ]
    standing = _cursor_gap(QRect(start, size), cursor)
    # staying where it is stands as one of the routes, so a flight that would
    # carry the button closer to the cursor than it stands already is never
    # taken: only a flight that keeps the cursor at least as far off is, and
    # where none does, the button stays put
    routes: list[tuple[int, QPoint, QPoint]] = [(standing, start, start)]
    for target in _far_corners(area, size, cursor):
        # a straight flight is one of the routes weighed up for each spot
        way, room = target, _route_gap(start, target, target, size, cursor)
        for corner in corners:
            if corner == target:
                continue
            by_corner = _route_gap(start, corner, target, size, cursor)
            if by_corner > room:
                way, room = corner, by_corner
        routes.append((room, target, way))

    clearest = max(room for room, _, _ in routes)
    even = [(target, way) for room, target, way in routes if room * 10 >= clearest * 9]
    # among routes that are as clear as each other, a flight is worth more
    # than standing still, which is what is left when every flight would come
    # closer to the cursor
    moves = [route for route in even if route != (start, start)]
    return rng.choice(moves or even)


def _length(start: QPoint, end: QPoint) -> float:
    """How far `start` is from `end`."""
    return hypot(end.x() - start.x(), end.y() - start.y())


def _along(start: QPoint, end: QPoint, part: float) -> QPoint:
    """The spot `part` of the way from `start` to `end`."""
    return QPoint(
        round(start.x() + (end.x() - start.x()) * part),
        round(start.y() + (end.y() - start.y()) * part),
    )


def _point_on_path(start: QPoint, way: QPoint, target: QPoint, part: float) -> QPoint:
    """The spot a flight from `start` to `target` by way of `way` has reached
    after `part` of the distance, which is 0 at the start and 1 at the
    target. Pure, so the shape of the flight is pinned without a mouse
    (spec review.four-button-notice)."""
    first = _length(start, way)
    second = _length(way, target)
    walked = part * (first + second)
    if walked <= first:
        # still on the first leg, which is no leg at all where the button
        # already stands where it is going by
        return _along(start, way, walked / first) if first else QPoint(start)
    return _along(way, target, (walked - first) / second)


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
        # the flight is animated as the share of the way covered, and the
        # button is put wherever that share is, so that the flight can bend
        # round the cursor instead of going straight over it
        self._flight = QVariantAnimation(self)
        self._flight.setDuration(FLEE_MS)
        self._flight.setStartValue(0.0)
        self._flight.setEndValue(1.0)
        self._flight.setEasingCurve(QEasingCurve.Type.OutCubic)
        qconnect(self._flight.valueChanged, self._follow_path)
        self._from = QPoint()
        self._by = QPoint()
        self._to = QPoint()

    def fly_to(self, way: QPoint, target: QPoint) -> None:
        """Head for `target` from wherever the button is now, by way of `way`
        when the way there bends round the cursor, taking FLEE_MS over it, so
        that it flies instead of jumping there."""
        self._from = self.pos()
        self._by = way
        self._to = target
        self._flight.stop()
        self._flight.start()

    def _follow_path(self, part: float) -> None:
        """The animation's slot: put the button where `part` of the way along
        its path is."""
        self.move(_point_on_path(self._from, self._by, self._to, part))

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
        strip's own coordinates. Returns whether the button was sent off:
        where every flight would carry it across the cursor, it stays where
        it is."""
        button = QRect(self.button.pos(), self.button.size())
        if not force and _cursor_gap(button, cursor) > FLEE_GAP**2:
            return False
        target, way = _flee_route(
            self.rect(), self.button.pos(), self.button.size(), cursor, _random
        )
        if target == way == self.button.pos():
            return False
        self.button.fly_to(way, target)
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
    # Qt lines a button box up with the right-hand edge on Windows; the
    # notice's two buttons belong in the middle, one above the other
    button_box.setCenterButtons(True)
    qconnect(button_box.accepted, dialog.accept)
    layout.addWidget(button_box)

    dialog.setLayout(layout)
    dialog.setMinimumWidth(MINIMUM_WIDTH)
    dialog.show()
    layout.activate()
    area.start_above(understand)
    return dialog
