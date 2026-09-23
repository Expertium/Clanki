# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins spec/ui.md#ui.browser-simple-view."""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

import pytest

ADVANCED_ONLY = [
    ("menuEdit", "actionSelectNotes"),
    ("menuEdit", "actionInvertSelection"),
    ("menuEdit", "actionCreateFilteredDeck"),
    ("menu_Notes", "actionCopy"),
    ("menu_Notes", "actionExport"),
    ("menu_Notes", "actionChangeModel"),
    ("menu_Notes", "actionFindDuplicates"),
    ("menu_Notes", "actionFindReplace"),
    ("menu_Notes", "actionManage_Note_Types"),
    ("menu_Cards", "action_set_due_date"),
    ("menu_Cards", "action_grade_now"),
    ("menu_Cards", "action_forget"),
    ("menu_Cards", "actionReposition"),
    ("menu_Cards", "action_toggle_bury"),
    ("menuqt_accel_view", "action_toggle_mode"),
]
KEPT = [
    ("menuEdit", "actionUndo"),
    ("menuEdit", "actionSelectAll"),
    ("menu_Notes", "actionAdd"),
    ("menu_Notes", "actionAdd_Tags"),
    ("menu_Notes", "actionRemove_Tags"),
    ("menu_Notes", "actionClear_Unused_Tags"),
    ("menu_Notes", "actionToggle_Mark"),
    ("menu_Notes", "actionDelete"),
    ("menu_Cards", "actionChange_Deck"),
    ("menu_Cards", "actionToggle_Suspend"),
    ("menu_Cards", "action_Info"),
    ("menuqt_accel_view", "actionFullScreen"),
    ("menuqt_accel_view", "actionToggleSidebar"),
]


@pytest.fixture(scope="module")
def qapp() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from aqt.qt import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def browser(qapp: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A Browser window with its real menus and stand-ins for the rest."""
    import aqt.forms
    from aqt.browser.browser import Browser
    from aqt.qt import QMainWindow, QWidget
    from aqt.utils import tr

    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "x")
    browser: Any = Browser.__new__(Browser)
    QMainWindow.__init__(browser)
    browser.form = aqt.forms.browser.Ui_Dialog()
    browser.form.setupUi(browser)
    mode = {"advanced": False}
    browser.mode = mode
    choices: dict[str, bool] = {}
    browser.choices = choices
    browser.mw = SimpleNamespace(
        advanced_ui=lambda: mode["advanced"],
        col=SimpleNamespace(get_config=lambda key, default=None: choices),
    )
    browser._switch = QWidget(browser)
    browser.toolbar_modes = []
    browser.sidebar = SimpleNamespace(
        toolbar=SimpleNamespace(
            actions=lambda: [], apply_ui_mode=browser.toolbar_modes.append
        ),
        refresh=lambda: None,
    )
    browser.table = SimpleNamespace(
        is_notes_mode=lambda: False, apply_ui_mode=lambda: None
    )
    browser._setup_remove_leech_tag_action()
    return browser


def _in_menu(browser: Any, menu: str, action: str) -> bool:
    return getattr(browser.form, action) in getattr(browser.form, menu).actions()


def test_simple_mode_takes_the_advanced_only_items_out_of_the_menus(browser):
    before = {m: list(getattr(browser.form, m).actions()) for m, _ in ADVANCED_ONLY}

    browser._setup_ui_mode()

    for menu, action in ADVANCED_ONLY:
        assert not _in_menu(browser, menu, action), action
    for menu, action in KEPT:
        assert _in_menu(browser, menu, action), action
    assert not browser.form.menuJump.menuAction().isVisible()
    assert not browser.form.menuLayout.menuAction().isVisible()
    assert not browser.form.menuFlag.menuAction().isVisible()
    assert browser._switch.isHidden()
    assert browser.toolbar_modes == [False]

    browser.mode["advanced"] = True
    browser.apply_ui_mode()

    # Advanced mode puts every item back at its own place
    for menu, actions in before.items():
        assert getattr(browser.form, menu).actions() == actions
    assert browser.form.menuJump.menuAction().isVisible()
    assert browser.form.menuLayout.menuAction().isVisible()
    assert browser.form.menuFlag.menuAction().isVisible()
    assert not browser._switch.isHidden()
    assert browser.toolbar_modes == [False, True]


def test_hidden_items_keep_their_shortcuts(browser):
    browser._setup_ui_mode()

    hidden = [getattr(browser.form, a) for _, a in ADVANCED_ONLY]
    for menu in (browser.form.menuJump, browser.form.menuLayout, browser.form.menuFlag):
        hidden += menu.actions()
    for action in hidden:
        if action.isSeparator():
            continue
        # a visible action held by the window answers its shortcut
        assert action.isVisible(), action.objectName()
        assert action in browser.actions(), action.objectName()


def test_an_addon_menu_entry_stays_through_a_mode_switch(browser):
    from aqt.qt import QAction

    addon = QAction("add-on", browser)
    browser.form.menu_Cards.addAction(addon)
    browser._setup_ui_mode()
    browser.mode["advanced"] = True
    browser.apply_ui_mode()
    browser.mode["advanced"] = False
    browser.apply_ui_mode()

    assert browser.form.menu_Cards.actions()[-1] is addon


class _Backend:
    def __init__(self) -> None:
        self.columns: list[list[str]] = []

    def set_active_browser_columns(self, columns: list[str]) -> None:
        self.columns.append(list(columns))


class _Col:
    def __init__(self, advanced: bool) -> None:
        self.advanced = advanced
        self._backend = _Backend()
        self.stored = ["noteFld", "template", "cardEase", "deck"]

    def get_config_bool(self, _key: Any) -> bool:
        return self.advanced

    def get_config(self, _key: Any, default: Any = None) -> Any:
        return default

    def load_browser_card_columns(self) -> list[str]:
        self._backend.set_active_browser_columns(self.stored)
        return list(self.stored)

    def set_browser_card_columns(self, columns: list[str]) -> None:
        self.stored = list(columns)


def test_simple_mode_shows_fixed_columns_and_keeps_the_stored_choice():
    from aqt.browser.table.state import CardState

    # no Retrievability: Advanced-only (spec ui.retrievability-advanced-only)
    simple_columns = ["noteFld", "deck", "cardDue", "cardIvl"]
    col = _Col(advanced=False)
    state = CardState(col)  # type: ignore[arg-type]

    assert state.active_columns == simple_columns
    assert col._backend.columns == [simple_columns]
    state.toggle_active_column("cardEase")
    assert state.active_columns == simple_columns
    assert col.stored == ["noteFld", "template", "cardEase", "deck"]
    # its own column widths, so Advanced mode's layout survives
    assert state.GEOMETRY_KEY_PREFIX != "editor"

    col.advanced = True
    advanced = CardState(col)  # type: ignore[arg-type]
    assert advanced.active_columns == ["noteFld", "template", "cardEase", "deck"]
    assert advanced.GEOMETRY_KEY_PREFIX == "editor"


@pytest.mark.parametrize("advanced", [False, True])
def test_saved_searches_flags_and_note_types_are_advanced_only(advanced):
    from aqt.browser.sidebar.item import SidebarItem, SidebarItemType
    from aqt.browser.sidebar.tree import SidebarStage, SidebarTreeView

    tree: Any = SidebarTreeView.__new__(SidebarTreeView)
    tree.mw = SimpleNamespace(advanced_ui=lambda: advanced)
    built: list[SidebarStage] = []
    for stage, method in [
        (SidebarStage.SAVED_SEARCHES, "_saved_searches_tree"),
        (SidebarStage.NOTETYPES, "_notetype_tree"),
        (SidebarStage.FLAGS, "_flags_tree"),
        (SidebarStage.DECKS, "_deck_tree"),
        (SidebarStage.TAGS, "_tag_tree"),
    ]:
        setattr(tree, method, lambda root, stage=stage: built.append(stage))
    root = SidebarItem("", "", item_type=SidebarItemType.ROOT)
    for stage in SidebarStage:
        if stage in (SidebarStage.CARD_STATE, SidebarStage.TODAY):
            continue
        tree._build_stage(root, stage)

    expected = [SidebarStage.DECKS, SidebarStage.TAGS]
    if advanced:
        expected += [
            SidebarStage.SAVED_SEARCHES,
            SidebarStage.FLAGS,
            SidebarStage.NOTETYPES,
        ]
    assert sorted(built, key=lambda s: s.value) == sorted(
        expected, key=lambda s: s.value
    )


def test_a_hidden_item_runs_from_its_shortcut(browser, qapp):
    from PyQt6.QtTest import QTest

    browser._setup_ui_mode()
    action = browser.form.action_forget
    assert not _in_menu(browser, "menu_Cards", "action_forget")
    fired: list[bool] = []
    action.triggered.connect(lambda *_: fired.append(True))
    browser.show()
    browser.activateWindow()
    qapp.processEvents()

    QTest.keySequence(browser, action.shortcut())  # type: ignore[call-overload]
    qapp.processEvents()

    assert fired == [True]
    browser.hide()


def test_remove_leech_tag_follows_remove_tags_in_both_modes(browser):
    """Pins spec/ui.md#ui.browser-remove-leech-tag: a tag item, so Simple
    mode shows it too."""
    browser._setup_ui_mode()

    actions = browser.form.menu_Notes.actions()
    remove_tags = actions.index(browser.form.actionRemove_Tags)
    assert actions[remove_tags + 1] is browser.action_remove_leech_tag
    browser.mode["advanced"] = True
    browser.apply_ui_mode()
    assert browser.action_remove_leech_tag in browser.form.menu_Notes.actions()


def test_remove_leech_tag_removes_only_the_leech_tag(browser, monkeypatch):
    import aqt.browser.browser as browser_module

    calls: list[tuple] = []

    def fake_remove(*, parent, note_ids, space_separated_tags):
        calls.append((parent, note_ids, space_separated_tags))
        return SimpleNamespace(run_in_background=lambda initiator: None)

    monkeypatch.setattr(browser_module, "remove_tags_from_notes", fake_remove)
    browser.table = SimpleNamespace(len_selection=lambda: 2)
    browser.editor = SimpleNamespace(call_after_note_saved=lambda f: f())
    browser.selected_notes = lambda: [11, 12]

    browser.remove_leech_tag_from_selected_notes()

    assert calls == [(browser, [11, 12], "leech")]
