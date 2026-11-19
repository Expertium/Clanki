# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The four-answer-buttons notice (spec review.four-button-notice)."""

from __future__ import annotations

import os
import random
from pathlib import Path
from typing import Any, cast

import pytest

import anki.lang

anki.lang.set_lang("en")

import aqt.four_button_notice as notice  # noqa: E402
from aqt import AnkiQt  # noqa: E402
from aqt.qt import (  # noqa: E402
    QDialog,
    QPoint,
    QPushButton,
    QRect,
    QSize,
    Qt,
    QWidget,
    qconnect,
)
from aqt.utils import tr  # noqa: E402


class FakeProfileManager:
    def __init__(self) -> None:
        self.meta: dict[str, bool] = {}
        self.saves = 0

    def save(self) -> None:
        self.saves += 1


class FakeMainWindow(QWidget):
    """As much of the main window as the notice asks for: a real parent
    widget and the two calls it makes on the window."""

    def __init__(self) -> None:
        super().__init__()
        self.pm = FakeProfileManager()
        self.deleted_later: list[QDialog] = []

    def garbage_collect_on_dialog_finish(self, dialog: QDialog) -> None:
        self.deleted_later.append(dialog)

    def as_anki_qt(self) -> AnkiQt:
        """Itself, as the parameter the notice declares."""
        return cast(AnkiQt, self)


@pytest.fixture(scope="module")
def qapp() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from aqt.qt import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def mw(qapp: Any) -> FakeMainWindow:
    return FakeMainWindow()


@pytest.fixture(autouse=True)
def shipped_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tests that are not about the development build behave like a
    shipped one, whatever ANKIDEV or the source launcher say on the machine
    running them."""
    monkeypatch.setattr(notice, "dev_mode", "")
    monkeypatch.delenv(notice.DEV_RUN_ENV, raising=False)


def shown_notice(mw: FakeMainWindow, qapp: Any) -> QDialog:
    dialog = notice.show(mw.as_anki_qt())
    assert dialog is not None
    dialog.show()
    qapp.processEvents()
    return dialog


def strip_of(dialog: QDialog) -> notice._FleeArea:
    area = dialog.findChild(notice._FleeArea)
    assert area is not None
    return area


def accepting_button(dialog: QDialog) -> QPushButton:
    """The notice's button that accepts it, "I understand that Hard is a
    passing grade"."""
    button = next(
        button
        for button in dialog.findChildren(QPushButton)
        if button.text() == tr.preferences_four_button_notice_understand()
    )
    return button


def test_the_notice_repeats_the_manuals_own_words_for_the_four_buttons(
    mw: FakeMainWindow, qapp: Any
) -> None:
    from aqt.qt import QLabel

    dialog = shown_notice(mw, qapp)
    label = dialog.findChild(QLabel)
    assert label is not None
    # the ftl values wrap their lines, so compare with the wrapping taken out,
    # and without the isolates Fluent puts around a placeable
    text = " ".join(label.text().split()).replace("\u2068", "").replace("\u2069", "")
    for button in ("Again", "Hard", "Good", "Easy"):
        assert f"<b>{button}</b>" in text
    for shortcut in (
        "Keyboard shortcut: 1",
        "Keyboard shortcut: 2",
        "Keyboard shortcut: 3, Space, Enter",
        "Keyboard shortcut: 4",
    ):
        assert shortcut in text
    assert "it counts as a fail in a real-life context outside of Anki" in text
    assert "you had doubts about it or it took a long time to recall" in text
    assert "this should be the most commonly used button" in text
    assert "it took no mental effort to recall it" in text


def test_the_notice_has_the_two_buttons_at_the_bottom(
    mw: FakeMainWindow, qapp: Any
) -> None:
    from aqt.qt import QDialogButtonBox, QPushButton

    dialog = shown_notice(mw, qapp)
    labels = [button.text() for button in dialog.findChildren(QPushButton)]
    assert tr.preferences_four_button_notice_understand() in labels
    assert tr.preferences_four_button_notice_hard_as_fail() in labels
    # the one that accepts the notice sits in the button box at the bottom;
    # the one that runs away lives in its own strip
    box = dialog.findChild(QDialogButtonBox)
    assert box is not None
    understand = accepting_button(dialog)
    assert understand.parent() is box
    assert (
        strip_of(dialog).button.text()
        == tr.preferences_four_button_notice_hard_as_fail()
    )


def test_the_two_buttons_sit_in_the_middle_of_the_notice(
    mw: FakeMainWindow, qapp: Any
) -> None:
    """Qt lines a button box up with the right-hand edge on Windows, which is
    why the notice asks for centred buttons: the accepting button and the one
    above it belong in the middle, not over on the right."""
    dialog = shown_notice(mw, qapp)
    area = strip_of(dialog)
    area._timer.stop()  # the test places the button; the real cursor must not
    # room to spare: with the text as wide as its box, centring cannot be seen
    dialog.resize(900, dialog.height())
    layout = dialog.layout()
    assert layout is not None
    layout.activate()
    qapp.processEvents()
    area.start_above(accepting_button(dialog))
    qapp.processEvents()

    middle = dialog.rect().center().x()
    accept = accepting_button(dialog)
    assert abs(accept.mapTo(dialog, accept.rect().center()).x() - middle) <= 2
    assert abs(area.button.mapTo(dialog, area.button.rect().center()).x() - middle) <= 2


def test_the_understand_button_closes_the_notice(mw: FakeMainWindow, qapp: Any) -> None:
    dialog = shown_notice(mw, qapp)
    accepting_button(dialog).click()
    qapp.processEvents()
    assert not dialog.isVisible()


def test_the_notice_is_shown_once_and_remembered(
    mw: FakeMainWindow, qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notice, "dev_mode", "")
    assert notice.should_show(mw.as_anki_qt()) is True

    assert notice.show(mw.as_anki_qt()) is not None

    assert mw.pm.meta[notice.SHOWN_KEY] is True
    assert mw.pm.saves == 1
    assert notice.should_show(mw.as_anki_qt()) is False
    # and it does not come back on a later switch
    assert notice.show(mw.as_anki_qt()) is None


def test_an_ankidev_build_shows_the_notice_every_time(
    mw: FakeMainWindow, qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(notice, "dev_mode", "1")

    assert notice.show(mw.as_anki_qt()) is not None
    assert notice.show(mw.as_anki_qt()) is not None

    # nothing is written down, so the shipped build still shows it once
    assert notice.SHOWN_KEY not in mw.pm.meta


def test_a_run_started_by_the_source_launcher_shows_the_notice_every_time(
    mw: FakeMainWindow, qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(notice.DEV_RUN_ENV, "1")

    assert notice.show(mw.as_anki_qt()) is not None
    assert notice.show(mw.as_anki_qt()) is not None

    assert notice.SHOWN_KEY not in mw.pm.meta


def test_the_source_launcher_marks_its_run() -> None:
    """A shortcut starting `tools/run.py` is a developer's build too
    (spec review.four-button-notice), which is what the marker is for."""
    launcher = Path(__file__).resolve().parents[2] / "tools" / "run.py"

    assert f'"{notice.DEV_RUN_ENV}"' in launcher.read_text(encoding="utf-8")


AREA = QRect(0, 0, 500, notice.STRIP_HEIGHT)
BUTTON = QSize(170, 30)


@pytest.mark.parametrize("x", [0, 90, 250, 410, 499])
@pytest.mark.parametrize("y", [0, 47, 95])
def test_the_button_hides_farther_than_the_cursor_can_reach(x: int, y: int) -> None:
    cursor = QPoint(x, y)
    for seed in range(20):
        spot = notice._flee_target(AREA, BUTTON, cursor, random.Random(seed))
        rect = QRect(spot, BUTTON)
        assert AREA.contains(rect)
        assert not rect.contains(cursor)
        assert notice._cursor_gap(rect, cursor) > notice.FLEE_GAP**2


def test_a_strip_too_small_to_flee_from_keeps_the_button_in_it() -> None:
    area = QRect(0, 0, 80, 20)
    spot = notice._flee_target(area, BUTTON, QPoint(40, 10), random.Random(0))
    assert spot.x() >= area.left()
    assert spot.y() >= area.top()


def test_the_button_moves_off_a_cursor_that_reaches_it(
    mw: FakeMainWindow, qapp: Any
) -> None:
    from PyQt6.QtTest import QTest

    dialog = shown_notice(mw, qapp)
    area = strip_of(dialog)
    on_the_button = QRect(area.button.pos(), area.button.size()).center()

    assert area.flee_from_cursor(on_the_button) is True
    QTest.qWait(notice.FLEE_MS + 100)  # type: ignore[call-arg, arg-type]

    assert not QRect(area.button.pos(), area.button.size()).contains(on_the_button)


def test_the_button_starts_straight_above_the_understand_button(
    mw: FakeMainWindow, qapp: Any
) -> None:
    dialog = shown_notice(mw, qapp)
    area = strip_of(dialog)
    understand = accepting_button(dialog)

    fleeing_centre = area.button.mapToGlobal(area.button.rect().center())
    accepting_centre = understand.mapToGlobal(understand.rect().center())

    # the same column, one row up: the joke button starts where a second
    # button would sit, not in a corner
    assert abs(fleeing_centre.x() - accepting_centre.x()) <= 1
    assert fleeing_centre.y() < accepting_centre.y()


def test_the_button_flies_to_its_new_spot_instead_of_jumping(
    mw: FakeMainWindow, qapp: Any
) -> None:
    from PyQt6.QtTest import QTest

    dialog = shown_notice(mw, qapp)
    area = strip_of(dialog)
    before = area.button.pos()
    on_the_button = QRect(before, area.button.size()).center()

    assert area.flee_from_cursor(on_the_button) is True

    # it did not jump: it is on its way, and it is still moving partway there
    assert area.button.is_flying()
    QTest.qWait(notice.FLEE_MS // 3)  # type: ignore[call-arg, arg-type]
    partway = area.button.pos()
    assert partway != before
    QTest.qWait(notice.FLEE_MS)  # type: ignore[call-arg, arg-type]
    assert area.button.pos() != partway
    assert not QRect(area.button.pos(), area.button.size()).contains(on_the_button)


def test_the_notice_cannot_be_closed_with_the_cross_or_escape(
    mw: FakeMainWindow, qapp: Any
) -> None:
    dialog = shown_notice(mw, qapp)

    # no cross in the title bar to click
    assert not dialog.windowFlags() & Qt.WindowType.WindowCloseButtonHint

    # and neither the window's close event nor Esc gets rid of it: accepting
    # it is the only way out
    assert dialog.close() is False
    qapp.processEvents()
    assert dialog.isVisible()
    dialog.reject()
    qapp.processEvents()
    assert dialog.isVisible()


def test_the_button_stays_put_while_the_cursor_is_far_away(
    mw: FakeMainWindow, qapp: Any
) -> None:
    dialog = shown_notice(mw, qapp)
    area = strip_of(dialog)
    before = area.button.pos()

    assert area.flee_from_cursor(QPoint(-1000, -1000)) is False

    assert area.button.pos() == before


def test_pressing_the_fleeing_button_does_nothing(
    mw: FakeMainWindow, qapp: Any
) -> None:
    from PyQt6.QtTest import QTest

    dialog = shown_notice(mw, qapp)
    area = strip_of(dialog)
    presses: list[bool] = []
    qconnect(area.button.clicked, lambda: presses.append(True))

    QTest.mouseClick(area.button, Qt.MouseButton.LeftButton)  # type: ignore[call-overload]
    qapp.processEvents()

    assert presses == []
    # the notice is still open: nothing was accepted and nothing was changed
    assert dialog.isVisible()


def test_the_fleeing_button_cannot_be_reached_with_the_keyboard(
    mw: FakeMainWindow, qapp: Any
) -> None:
    dialog = shown_notice(mw, qapp)
    area = strip_of(dialog)

    assert area.button.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert area.button.isDefault() is False
    assert area.button.autoDefault() is False
