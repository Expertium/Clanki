# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pins spec/ui.md#ui.split-configurable: the Simple | Advanced split is a
registry of items with defaults equal to the earlier fixed split, the user's
differences are stored in the collection config, and every area reads the
split in both directions."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

import anki.lang

# several aqt modules read translated strings at import time
anki.lang.set_lang("en")

from anki.config import Config  # noqa: E402
from aqt import ui_split  # noqa: E402

# Today's split, restated from the spec entries that set it
# (ui.simple-mode-tools-hidden, ui.get-decks-and-get-addons, ui.mode-switch,
# ui.advance-postpone, ui.simple-mode-deck-counts, ui.reviewer-simple-view,
# ui.browser-simple-view): True = Simple mode shows the item.
TODAY: dict[str, bool] = {
    "main.tools.study_deck": True,
    "main.tools.create_filtered": False,
    "main.tools.check_database": False,
    "main.tools.check_media": False,
    "main.tools.empty_cards": False,
    "main.tools.addons": True,
    "main.tools.note_types": False,
    "main.tools.check_for_updates": True,
    "main.deck_list.find_decks_online": True,
    "main.deck_list.get_addons": False,
    "main.deck_list.create_deck": True,
    "main.deck_list.import_file": False,
    "main.deck_menu.rename": True,
    "main.deck_menu.options": True,
    "main.deck_menu.advance_postpone": False,
    "main.deck_menu.rwkv": False,
    "main.deck_menu.export": True,
    "main.deck_menu.delete": True,
    "main.overview.options": True,
    "main.overview.rebuild": True,
    "main.overview.empty": True,
    "main.overview.custom_study": False,
    "main.overview.unbury": True,
    "main.overview.description": True,
    "main.learn_count": False,
    "reviewer.flag": False,
    "reviewer.bury_card": False,
    "reviewer.reset_card": False,
    "reviewer.set_due_date": False,
    "reviewer.suspend_card": True,
    "reviewer.options": True,
    "reviewer.card_info": True,
    "reviewer.previous_card_info": False,
    "reviewer.tag_note": True,
    "reviewer.bury_note": False,
    "reviewer.suspend_note": False,
    "reviewer.create_copy": False,
    "reviewer.delete_note": True,
    "reviewer.replay_audio": True,
    "reviewer.pause_audio": True,
    "reviewer.audio_back": False,
    "reviewer.audio_forward": False,
    "reviewer.record_voice": False,
    "reviewer.replay_voice": False,
    "reviewer.auto_advance": False,
    "browser.edit.undo": True,
    "browser.edit.redo": True,
    "browser.edit.select_all": True,
    "browser.edit.select_notes": False,
    "browser.edit.invert_selection": False,
    "browser.edit.close": True,
    "browser.edit.create_filtered": False,
    "browser.go": False,
    "browser.notes.add": True,
    "browser.notes.create_copy": False,
    "browser.notes.export": False,
    "browser.notes.add_tags": True,
    "browser.notes.remove_tags": True,
    "browser.notes.remove_leech_tag": True,
    "browser.notes.clear_unused_tags": True,
    "browser.notes.toggle_tag": True,
    "browser.notes.change_note_type": False,
    "browser.notes.find_duplicates": False,
    "browser.notes.find_and_replace": False,
    "browser.notes.note_types": False,
    "browser.notes.delete": True,
    "browser.cards.change_deck": True,
    "browser.cards.set_due_date": False,
    "browser.cards.grade_now": False,
    "browser.cards.advance_postpone": False,
    "browser.cards.reset": False,
    "browser.cards.reposition": False,
    "browser.cards.toggle_suspend": True,
    "browser.cards.toggle_bury": False,
    "browser.cards.flag": False,
    "browser.cards.info": True,
    "browser.view.toggle_cards_notes": False,
    "browser.view.full_screen": True,
    "browser.view.toggle_sidebar": True,
    "browser.view.zoom_in": True,
    "browser.view.zoom_out": True,
    "browser.view.reset_zoom": True,
    "browser.view.layout": False,
    "browser.cards_notes_switch": False,
    "browser.sidebar.select_tool": False,
    "browser.sidebar.saved_searches": False,
    "browser.sidebar.today": True,
    "browser.sidebar.flags": False,
    "browser.sidebar.card_state": True,
    "browser.sidebar.decks": True,
    "browser.sidebar.note_types": False,
    "browser.sidebar.tags": True,
    **{
        f"browser.column.{column}": column
        in ("noteFld", "deck", "cardDue", "cardIvl", "retrievability")
        for column in [
            "noteFld",
            "deck",
            "cardDue",
            "cardIvl",
            "retrievability",
            "question",
            "answer",
            "template",
            "note",
            "noteTags",
            "cardEase",
            "cardLapses",
            "cardReps",
            "noteCrt",
            "cardMod",
            "noteMod",
            "originalPosition",
            "stability",
            "difficulty",
        ]
    },
    # the note editor (spec ui.editor-simple-view)
    "editor.fields": True,
    "editor.cards": False,
    "editor.settings": False,
    "editor.bold": True,
    "editor.italic": True,
    "editor.underline": True,
    "editor.superscript": False,
    "editor.subscript": False,
    "editor.textColor": True,
    "editor.highlightColor": False,
    "editor.removeFormat": True,
    "editor.unorderedList": False,
    "editor.orderedList": False,
    "editor.alignment": False,
    "editor.attachMedia": True,
    "editor.recordAudio": False,
    "editor.mathjax": False,
    # the Stats page (spec ui.mode-switch): Reviews, Card Counts, Retention
    # and Total Knowledge
    "stats.today": False,
    "stats.futureDue": False,
    "stats.calendar": False,
    "stats.reviews": True,
    "stats.cardCounts": True,
    "stats.intervals": False,
    "stats.stability": False,
    "stats.ease": False,
    "stats.difficulty": False,
    "stats.retrievability": False,
    "stats.totalKnowledge": True,
    "stats.roc": False,
    "stats.calibration": False,
    "stats.umPlus": False,
    "stats.trueRetention": True,
    "stats.hours": False,
    "stats.buttons": False,
    "stats.added": False,
    # deck options (spec deck-options.simple-view): the Simple section's
    # settings; every other setting is Advanced-only
    **{
        f"deckOptions.{key}": key
        in (
            "newLimit",
            "desiredRetention",
            "optimizeAllPresets",
            "burySiblings",
            "playAudio",
            "showTimer",
            "easyDays",
        )
        for key in [
            "newLimit",
            "desiredRetention",
            "optimizeAllPresets",
            "burySiblings",
            "playAudio",
            "showTimer",
            "easyDays",
            "reviewLimit",
            "dailyLimitTabs",
            "learningSteps",
            "maxSameDayReviews",
            "graduatingInterval",
            "easyInterval",
            "insertionOrder",
            "relearningSteps",
            "lapseMinimumInterval",
            "leechThreshold",
            "leechAction",
            "leechOnlyIfYoung",
            "newGatherPriority",
            "newCardSortOrder",
            "newReviewPriority",
            "interdayStepPriority",
            "reviewSortOrder",
            "algorithm",
            "desiredRetentionTabs",
            "fsrsHelpMeDecide",
            "fsrsParams",
            "fsrsAutoOptimizeDays",
            "fsrsSearchFilter",
            "fsrsHealthCheck",
            "fsrsSimulator",
            "rwkvEnforceGradeOrder",
            "rwkvAllowSameDayReview",
            "rwkvMinInterveningReviews",
            "rwkvMinElapsedSecs",
            "rwkvMinimumReviewsPerDay",
            "rwkvCandidateRefresh",
            "rwkvRefreshInterval",
            "rwkvRefreshOnExit",
            "rwkvMaintenance",
            "skipQuestionWhenReplaying",
            "maximumAnswerSecs",
            "secondsToShowQuestion",
            "secondsToShowAnswer",
            "waitForAudio",
            "questionAction",
            "answerAction",
            "maximumInterval",
            "fsrsMinimumInterval",
            "ignoreReviewsBefore",
            "startingEase",
            "easyBonus",
            "intervalModifier",
            "hardInterval",
            "newInterval",
        ]
    },
}


class ConfigCol:
    """A collection's config, enough for the split."""

    def __init__(
        self,
        advanced: bool = False,
        stored: Any = None,
        recall_wording: str | None = None,
    ) -> None:
        self.advanced = advanced
        self.conf: dict[str, Any] = {}
        self.strings: dict[Any, str] = {}
        if stored is not None:
            self.conf[ui_split.CONFIG_KEY] = stored
        if recall_wording is not None:
            self.strings[Config.String.RECALL_WORDING] = recall_wording

    def get_config_string(self, key: Any) -> str:
        return self.strings.get(key, "")

    def set_config_string(self, key: Any, value: str) -> None:
        self.strings[key] = value

    def get_config(self, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self.conf.get(key, default))

    def set_config(self, key: str, value: Any) -> None:
        self.conf[key] = copy.deepcopy(value)

    def remove_config(self, key: str) -> None:
        self.conf.pop(key, None)

    def get_config_bool(self, _key: Any) -> bool:
        return self.advanced


def _mw(advanced: bool, choices: dict[str, bool] | None = None, **kwargs: Any) -> Any:
    col = ConfigCol(advanced, choices)
    for name, value in kwargs.items():
        setattr(col, name, value)
    return SimpleNamespace(advanced_ui=lambda: advanced, col=col)


@pytest.fixture(scope="module")
def qapp() -> Any:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from aqt.qt import QApplication

    return QApplication.instance() or QApplication([])


# The registry
######################################################################


def test_every_default_equals_todays_split() -> None:
    assert {item.id: item.simple for item in ui_split.ITEMS} == TODAY


def test_ids_are_unique_and_every_item_has_an_english_name() -> None:
    ids = [item.id for item in ui_split.ITEMS]
    assert len(ids) == len(set(ids))
    for item in ui_split.ITEMS:
        assert item.label().strip(), item.id
        assert "&" not in item.label().replace("&&", ""), item.id
        assert item.area_label().strip(), item.id
        if item.group is not None:
            assert item.group().strip(), item.id


def test_the_switch_and_preferences_are_never_items() -> None:
    ids = " ".join(item.id for item in ui_split.ITEMS)
    assert "preferences" not in ids
    assert "advanced_ui" not in ids and "ui_mode" not in ids


def _repo_file(path: str) -> str:
    from pathlib import Path

    return (Path(__file__).parents[2] / path).read_text(encoding="utf-8")


def test_the_editor_page_knows_the_same_buttons_in_the_same_order() -> None:
    import re

    source = _repo_file("ts/routes/editor/ui-mode.ts")
    block = source.split("export const EDITOR_BUTTONS: EditorButton[] = [")[1]
    names = re.findall(r'"(\w+)"', block.split("];")[0])
    assert names == [name for name, _, _ in ui_split.EDITOR_BUTTONS]


def test_the_stats_page_knows_the_same_graphs_in_the_same_order() -> None:
    import re

    source = _repo_file("ts/routes/graphs/+page.svelte")
    block = source.split("const graphItems: GraphItem[] = [")[1].split("];")[0]
    names = re.findall(r'id: "(\w+)"', block)
    assert names == [name for name, _, _ in ui_split.STATS_GRAPHS]


def test_the_deck_options_page_knows_the_same_settings_in_the_same_order() -> None:
    """The page's allSettings(): the Simple section's settings, then each
    section's, without repeats."""
    import re

    source = _repo_file("ts/routes/deck-options/ui-split.ts")
    curated_block = source.split("export const CURATED = [")[1].split("]")[0]
    sections_block = source.split("export const SECTIONS = {")[1].split("} as const;")[
        0
    ]
    names: list[str] = []
    for name in re.findall(r'"(\w+)"', curated_block) + re.findall(
        r'"(\w+)"', sections_block
    ):
        if name not in names:
            names.append(name)
    registry = [
        item.id.removeprefix("deckOptions.")
        for item in ui_split.ITEMS
        if item.area is ui_split.Area.DECK_OPTIONS
    ]
    assert names == registry


def test_the_web_pages_get_every_item_with_the_choices_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    import aqt
    from anki import generic_pb2
    from aqt import mediasrv

    col = ConfigCol(stored={"editor.mathjax": True, "stats.reviews": False})
    monkeypatch.setattr(aqt, "mw", SimpleNamespace(col=col), raising=False)
    reply = generic_pb2.Json()
    reply.ParseFromString(mediasrv.get_ui_split())
    items = json.loads(reply.json)
    assert set(items) == set(TODAY)
    assert items["editor.mathjax"] is True and items["stats.reviews"] is False
    assert items["editor.bold"] is True and items["stats.today"] is False
    assert mediasrv.get_ui_split in mediasrv.post_handler_list


# Storage
######################################################################


def test_only_differences_from_the_defaults_are_stored() -> None:
    col = ConfigCol()
    ui_split.set_shown_in_simple(col, "main.tools.check_database", True)
    ui_split.set_shown_in_simple(col, "main.tools.study_deck", False)
    assert col.conf[ui_split.CONFIG_KEY] == {
        "main.tools.check_database": True,
        "main.tools.study_deck": False,
    }
    ui_split.set_shown_in_simple(col, "main.tools.check_database", False)
    assert col.conf[ui_split.CONFIG_KEY] == {"main.tools.study_deck": False}
    ui_split.set_shown_in_simple(col, "main.tools.study_deck", True)
    assert ui_split.CONFIG_KEY not in col.conf


def test_reset_keeps_choices_for_items_this_version_does_not_know() -> None:
    col = ConfigCol(stored={"main.tools.check_database": True, "future.item": True})
    ui_split.set_shown_in_simple(col, "main.tools.study_deck", False)
    assert col.conf[ui_split.CONFIG_KEY]["future.item"] is True
    ui_split.reset(col)
    assert col.conf[ui_split.CONFIG_KEY] == {"future.item": True}


def test_malformed_choices_are_ignored() -> None:
    assert ui_split.overrides(ConfigCol(stored=["not", "a", "map"])) == {}
    col = ConfigCol(stored={"main.tools.check_database": "yes"})
    assert not ui_split.shown_in_simple(col, "main.tools.check_database")


def test_choices_live_in_the_collection_config(tmp_path: Any) -> None:
    from anki.collection import Collection

    col = Collection(str(tmp_path / "collection.anki2"))
    try:
        ui_split.set_shown_in_simple(col, "reviewer.flag", True)
        assert col.get_config(ui_split.CONFIG_KEY) == {"reviewer.flag": True}
        assert ui_split.shown_in_simple(col, "reviewer.flag")
        ui_split.reset(col)
        assert col.get_config(ui_split.CONFIG_KEY, None) is None
        assert not ui_split.shown_in_simple(col, "reviewer.flag")
    finally:
        col.close()


# Lookup
######################################################################


def test_advanced_mode_shows_every_item_whatever_the_choices() -> None:
    hidden = {item.id: False for item in ui_split.ITEMS}
    shows = ui_split.visibility(_mw(True, hidden))
    assert all(shows(item.id) for item in ui_split.ITEMS)


def test_simple_mode_follows_the_choices_in_both_directions() -> None:
    shows = ui_split.visibility(
        _mw(False, {"main.tools.check_database": True, "main.tools.study_deck": False})
    )
    assert shows("main.tools.check_database")
    assert not shows("main.tools.study_deck")
    # an item without a choice keeps its default
    assert shows("main.tools.addons") and not shows("main.tools.note_types")


def test_an_unknown_item_id_is_an_error() -> None:
    with pytest.raises(KeyError):
        ui_split.shown(_mw(False), "no.such.item")
    with pytest.raises(KeyError):
        ui_split.shown(_mw(True), "no.such.item")


# The areas read the split
######################################################################


def test_tools_menu_follows_the_split(qapp: Any) -> None:
    import aqt.forms
    from aqt.main import AnkiQt
    from aqt.qt import QMainWindow

    win: Any = QMainWindow()
    win.form = aqt.forms.main.Ui_MainWindow()
    win.form.setupUi(win)
    mw = _mw(False, {"main.tools.check_database": True, "main.tools.study_deck": False})
    win.advanced_ui = mw.advanced_ui
    win.col = mw.col
    win._tools_menu_items = lambda: AnkiQt._tools_menu_items(win)
    AnkiQt._sync_tools_menu_for_ui_mode(win)
    actions = win.form.menuTools.actions()
    assert win.form.actionFullDatabaseCheck in actions
    assert win.form.actionStudyDeck not in actions
    # hidden, it keeps its shortcut: the window holds it
    assert win.form.actionStudyDeck in win.actions()
    assert win.form.actionPreferences in actions


def test_deck_list_buttons_follow_the_split() -> None:
    from aqt.deckbrowser import DeckBrowser

    browser: Any = DeckBrowser.__new__(DeckBrowser)
    browser.mw = _mw(
        False, {"main.deck_list.import_file": True, "main.deck_list.create_deck": False}
    )
    html = browser._buttons_html()
    assert 'pycmd("import")' in html
    assert 'pycmd("create")' not in html
    assert 'pycmd("shared")' in html and 'pycmd("get_addons")' not in html


def test_an_addon_deck_list_button_is_not_ours_to_hide() -> None:
    from aqt.deckbrowser import DeckBrowser

    browser: Any = DeckBrowser.__new__(DeckBrowser)
    browser.mw = _mw(False)
    browser.drawLinks = DeckBrowser.drawLinks + [["", "addon_cmd", "Add-on"]]
    assert 'pycmd("addon_cmd")' in browser._buttons_html()


def test_deck_menu_follows_the_split(monkeypatch: pytest.MonkeyPatch) -> None:
    from aqt import deckbrowser
    from aqt.deckbrowser import DeckBrowser

    menus: list[MagicMock] = []

    def make_menu(*_args: Any) -> MagicMock:
        menu = MagicMock()
        menus.append(menu)
        return menu

    monkeypatch.setattr(deckbrowser, "QMenu", make_menu)
    monkeypatch.setattr(deckbrowser, "QCursor", MagicMock())
    browser: Any = DeckBrowser.__new__(DeckBrowser)
    browser.mw = _mw(
        False,
        {"main.deck_menu.rwkv": True, "main.deck_menu.rename": False},
        get_config=lambda key, default=None: (
            {"main.deck_menu.rwkv": True, "main.deck_menu.rename": False}
            if key == ui_split.CONFIG_KEY
            else "rwkvCurve"
        ),
    )
    browser._showOptions("1")
    menu = menus[0]
    labels = [call.args[0] for call in menu.addAction.call_args_list]
    from aqt.utils import tr

    assert tr.actions_rename() not in labels
    assert tr.actions_options() in labels and tr.actions_delete() in labels
    menu.addMenu.assert_called_once_with(tr.decks_rwkv())
    # Advance and Postpone keep their default (hidden)
    assert not any(tr.actions_advance_cards() in label for label in labels)


def test_learn_count_follows_the_split() -> None:
    from anki.decks import DeckTreeNode
    from aqt.deckbrowser import DeckBrowser, RenderDeckNodeContext

    browser: Any = DeckBrowser.__new__(DeckBrowser)
    browser._rwkv_pending_deck_ids = set()
    browser.mw = _mw(False, {"main.learn_count": True})
    node = DeckTreeNode(deck_id=1, name="d", learn_count=2, review_count=3, level=1)
    # the tree reads the split once and hands the answer to every row, so the
    # row takes it from the context rather than asking again
    ctx = RenderDeckNodeContext(
        current_deck_id=0,
        review_limit_labels={1: ("", "")},
        show_learn_count=True,
    )
    row = browser._render_deck_node(node, ctx)
    assert 'id="deck-1-learn-count"' in row
    assert 'id="deck-1-review-count" class="review-count">3<' in row

    # Simple mode: no Learn column, and Learn folded into Due
    ctx = RenderDeckNodeContext(
        current_deck_id=0,
        review_limit_labels={1: ("", "")},
        show_learn_count=False,
    )
    row = browser._render_deck_node(node, ctx)
    assert 'id="deck-1-learn-count"' not in row
    assert 'id="deck-1-review-count" class="review-count">5<' in row


def test_overview_bottom_bar_follows_the_split() -> None:
    from aqt.overview import Overview

    draws: list[str] = []
    mw = _mw(
        False, {"main.overview.custom_study": True, "main.overview.options": False}
    )
    mw.col.decks = SimpleNamespace(current=lambda: {"dyn": False})
    mw.col.sched = SimpleNamespace(have_buried=lambda: False)
    ov: Any = SimpleNamespace(
        mw=mw,
        bottom=SimpleNamespace(draw=lambda buf="", **_kwargs: draws.append(buf)),
        _linkHandler=lambda _url: None,
    )
    Overview._renderBottom(ov)
    assert 'pycmd("studymore")' in draws[0]
    assert 'pycmd("opts")' not in draws[0]
    assert 'pycmd("description")' in draws[0]


def test_reviewer_more_menu_follows_the_split(monkeypatch: pytest.MonkeyPatch) -> None:
    from aqt.reviewer import Reviewer
    from aqt.utils import tr

    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "x")
    reviewer: Any = Reviewer.__new__(Reviewer)
    reviewer.mw = _mw(False, {"reviewer.flag": True, "reviewer.suspend_card": False})
    reviewer.mw.flags = SimpleNamespace(
        all=lambda: [SimpleNamespace(label="Red", index=1)]
    )
    reviewer.card = SimpleNamespace(user_flag=lambda: 0)
    reviewer.auto_advance_enabled = False
    opts = reviewer._contextMenu()
    callbacks = [row[2] for row in opts if row and len(row) > 2]
    assert len([row for row in opts if row and len(row) == 2]) == 1
    assert reviewer.suspend_current_card not in callbacks
    assert reviewer.onOptions in callbacks
    # no separator at an end or twice in a row
    assert opts[0] is not None and opts[-1] is not None
    assert all(a is not None or b is not None for a, b in zip(opts, opts[1:]))


@pytest.fixture
def browser(qapp: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    import aqt.forms
    from aqt.browser.browser import Browser
    from aqt.qt import QMainWindow, QWidget
    from aqt.utils import tr

    monkeypatch.setattr(tr, "_translate", lambda *args, **kwargs: "x")
    browser: Any = Browser.__new__(Browser)
    QMainWindow.__init__(browser)
    browser.form = aqt.forms.browser.Ui_Dialog()
    browser.form.setupUi(browser)
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


def test_browser_menus_follow_the_split(browser: Any) -> None:
    f = browser.form
    browser.mw = _mw(
        False,
        {
            "browser.cards.set_due_date": True,
            "browser.edit.undo": False,
            "browser.go": True,
            "browser.cards_notes_switch": True,
            "browser.sidebar.select_tool": True,
        },
    )
    browser._setup_ui_mode()
    assert f.action_set_due_date in f.menu_Cards.actions()
    assert f.actionUndo not in f.menuEdit.actions()
    assert f.actionUndo in browser.actions() and f.actionUndo.isVisible()
    assert f.menuJump.menuAction().isVisible()
    assert not f.menuFlag.menuAction().isVisible()
    assert not browser._switch.isHidden()
    assert browser.toolbar_modes == [True]


def test_browser_sidebar_sections_follow_the_split() -> None:
    from aqt.browser.sidebar.item import SidebarItem, SidebarItemType
    from aqt.browser.sidebar.tree import SIDEBAR_SECTIONS, SidebarTreeView

    tree: Any = SidebarTreeView.__new__(SidebarTreeView)
    tree.mw = _mw(False, {"browser.sidebar.flags": True, "browser.sidebar.tags": False})
    built = []
    for method in (
        "_saved_searches_tree",
        "_card_state_tree",
        "_today_tree",
        "_flags_tree",
        "_deck_tree",
        "_notetype_tree",
        "_tag_tree",
    ):
        setattr(tree, method, lambda root, method=method: built.append(method))
    root = SidebarItem("", "", item_type=SidebarItemType.ROOT)
    for stage in SIDEBAR_SECTIONS:
        tree._build_stage(root, stage)
    assert sorted(built) == sorted(
        ["_card_state_tree", "_today_tree", "_flags_tree", "_deck_tree"]
    )


def test_browser_simple_columns_follow_the_split() -> None:
    from aqt.browser.table.state import simple_card_columns

    col = ConfigCol(
        stored={"browser.column.cardEase": True, "browser.column.deck": False}
    )
    assert simple_card_columns(cast(Any, col)) == [
        "noteFld",
        "cardDue",
        "cardIvl",
        "retrievability",
        "cardEase",
    ]
    # at least one column
    none = {
        ui_split.browser_column_item_id(c): False
        for c, _, _ in ui_split.BROWSER_COLUMNS
    }
    assert simple_card_columns(cast(Any, ConfigCol(stored=none))) == ["noteFld"]


# The Preferences tab
######################################################################


def _tab(qapp: Any, stored: Any = None) -> tuple[Any, Any]:
    from aqt.ui_split_prefs import UiSplitPreferences

    mw = SimpleNamespace(col=ConfigCol(stored=stored), redraw_for_ui_split=MagicMock())
    return UiSplitPreferences(cast(Any, mw)), mw


def _leaf(tab: Any, item_id: str) -> Any:
    from aqt.ui_split_prefs import ITEM_ID_ROLE

    return next(leaf for leaf in tab._leaves if leaf.data(0, ITEM_ID_ROLE) == item_id)


def test_tab_lists_every_item_with_its_state(qapp: Any) -> None:
    from aqt.qt import Qt
    from aqt.ui_split_prefs import CHECK_COLUMN

    tab, _ = _tab(qapp, {"reviewer.flag": True})
    assert len(tab._leaves) == len(ui_split.ITEMS)
    assert tab.tree.topLevelItemCount() == len(ui_split.Area)
    assert _leaf(tab, "reviewer.flag").checkState(CHECK_COLUMN) == Qt.CheckState.Checked
    assert (
        _leaf(tab, "reviewer.bury_card").checkState(CHECK_COLUMN)
        == Qt.CheckState.Unchecked
    )
    assert (
        _leaf(tab, "reviewer.card_info").checkState(CHECK_COLUMN)
        == Qt.CheckState.Checked
    )


def test_tab_stores_a_change_at_once_and_redraws(qapp: Any) -> None:
    from aqt.qt import Qt
    from aqt.ui_split_prefs import CHECK_COLUMN

    tab, mw = _tab(qapp)
    _leaf(tab, "main.tools.check_database").setCheckState(
        CHECK_COLUMN, Qt.CheckState.Checked
    )
    assert mw.col.conf[ui_split.CONFIG_KEY] == {"main.tools.check_database": True}
    mw.redraw_for_ui_split.assert_called_once()


def test_tab_search_filters_the_list(qapp: Any) -> None:
    tab, _ = _tab(qapp)
    target = _leaf(tab, "main.tools.check_media")
    tab.search.setText(target.text(0).upper())
    shown = [leaf for leaf in tab._leaves if not leaf.isHidden()]
    assert target in shown
    assert _leaf(tab, "reviewer.flag") not in shown
    # a group's name matches its items
    group = _leaf(tab, "browser.sidebar.tags").parent()
    tab.search.setText(group.text(0))
    assert not _leaf(tab, "browser.sidebar.tags").isHidden()
    assert not _leaf(tab, "browser.sidebar.decks").isHidden()
    tab.search.setText("")
    assert not any(leaf.isHidden() for leaf in tab._leaves)


def test_tab_reset_restores_the_defaults(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aqt.ui_split_prefs
    from aqt.qt import Qt
    from aqt.ui_split_prefs import CHECK_COLUMN

    monkeypatch.setattr(aqt.ui_split_prefs, "askUser", lambda *a, **k: True)
    tab, mw = _tab(qapp, {"reviewer.flag": True})
    mw.col.strings[Config.String.RECALL_WORDING] = ui_split.RECALL_PLAIN
    tab.on_reset()
    assert ui_split.CONFIG_KEY not in mw.col.conf
    assert ui_split.recall_wording(mw.col) == ui_split.RECALL_BY_MODE
    assert tab.recall_wording.currentData() == ui_split.RECALL_BY_MODE
    assert (
        _leaf(tab, "reviewer.flag").checkState(CHECK_COLUMN) == Qt.CheckState.Unchecked
    )
    mw.redraw_for_ui_split.assert_called_once()


# The recall wording (spec ui.simple-recall-wording)
######################################################################


def test_the_wording_setting_and_the_mode_together_choose_the_words() -> None:
    """Pins spec/ui.md#ui.simple-recall-wording."""
    cases = [
        (ui_split.RECALL_BY_MODE, False, True),
        (ui_split.RECALL_BY_MODE, True, False),
        (ui_split.RECALL_TECHNICAL, False, False),
        (ui_split.RECALL_TECHNICAL, True, False),
        (ui_split.RECALL_PLAIN, False, True),
        (ui_split.RECALL_PLAIN, True, True),
    ]
    for wording, advanced, plain in cases:
        col = ConfigCol(advanced=advanced, recall_wording=wording)
        assert ui_split.plain_recall_wording(cast(Any, col)) is plain, (
            wording,
            advanced,
        )


def test_an_unset_or_unknown_wording_reads_as_by_mode() -> None:
    """Pins spec/ui.md#ui.simple-recall-wording."""
    assert ui_split.recall_wording(ConfigCol()) == ui_split.RECALL_BY_MODE
    assert (
        ui_split.recall_wording(ConfigCol(recall_wording="nonsense"))
        == ui_split.RECALL_BY_MODE
    )
    assert ui_split.recall_wording(None) == ui_split.RECALL_BY_MODE

    col = ConfigCol()
    ui_split.set_recall_wording(col, ui_split.RECALL_PLAIN)
    assert col.strings[Config.String.RECALL_WORDING] == ui_split.RECALL_PLAIN
    assert ui_split.recall_wording(col) == ui_split.RECALL_PLAIN
    with pytest.raises(ValueError):
        ui_split.set_recall_wording(col, "nonsense")


def test_the_item_labels_follow_the_wording_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins spec/ui.md#ui.simple-recall-wording: the Browser's Retrievability
    column and the Stats graph are named by the setting, in both modes."""
    import aqt
    from aqt.utils import tr

    def labels(wording: str, advanced: bool) -> tuple[str, str]:
        col = ConfigCol(advanced=advanced, recall_wording=wording)
        monkeypatch.setattr(
            aqt, "mw", SimpleNamespace(col=col, advanced_ui=lambda: advanced), False
        )
        return (
            ui_split.ITEMS_BY_ID["browser.column.retrievability"].label(),
            ui_split.ITEMS_BY_ID["stats.retrievability"].label(),
        )

    technical = (
        tr.card_stats_fsrs_retrievability(),
        tr.statistics_card_retrievability_title(),
    )
    plain = (
        tr.card_stats_fsrs_retrievability_plain(),
        tr.statistics_card_retrievability_title_plain(),
    )
    assert labels(ui_split.RECALL_BY_MODE, False) == plain
    assert labels(ui_split.RECALL_BY_MODE, True) == technical
    assert labels(ui_split.RECALL_TECHNICAL, False) == technical
    assert labels(ui_split.RECALL_TECHNICAL, True) == technical
    assert labels(ui_split.RECALL_PLAIN, False) == plain
    assert labels(ui_split.RECALL_PLAIN, True) == plain


def test_the_tab_offers_three_choices_and_stores_one_at_once(
    qapp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pins spec/ui.md#ui.simple-recall-wording: the one item of the tab that
    is not a "Show in Simple mode" checkbox."""
    import aqt

    tab, mw = _tab(qapp)
    monkeypatch.setattr(aqt, "mw", mw, False)
    combo = tab.recall_wording
    assert [combo.itemData(i) for i in range(combo.count())] == list(
        ui_split.RECALL_WORDING_CHOICES
    )
    assert combo.currentData() == ui_split.RECALL_BY_MODE

    combo.setCurrentIndex(combo.findData(ui_split.RECALL_TECHNICAL))
    assert ui_split.recall_wording(mw.col) == ui_split.RECALL_TECHNICAL
    mw.redraw_for_ui_split.assert_called_once()
    # the tab's own item names follow at once
    from aqt.utils import tr

    column = _leaf(tab, "browser.column.retrievability")
    assert column.text(0) == tr.card_stats_fsrs_retrievability()

    combo.setCurrentIndex(combo.findData(ui_split.RECALL_PLAIN))
    assert ui_split.recall_wording(mw.col) == ui_split.RECALL_PLAIN
    assert column.text(0) == tr.card_stats_fsrs_retrievability_plain()


# Pins spec/deck-options.md#deck-options.simple-view (Simple mode's name for
# the bury switch). The English text is read from the ftl source rather than
# through tr.*(): other tests in this folder change the language for the
# process, so a tr.*() assertion here passes or fails by test order. The
# wiring to the key is checked separately, which no language affects.
# It is not in ts/routes/deck-options/bury-siblings.test.ts because vitest
# loads no Fluent bundle: there every tr.*() returns "missing key: <key>".

DECK_CONFIG_FTL = Path(__file__).parents[2] / "ftl" / "core" / "deck-config.ftl"
STATISTICS_FTL = DECK_CONFIG_FTL.with_name("statistics.ftl")


def english_message(key: str, path: Path = DECK_CONFIG_FTL) -> str:
    """The message's English text, straight out of the ftl source.

    Fluent puts a one-line message after "key = " and an indented block under
    "key =", with blank lines allowed inside the block.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if not line.startswith(f"{key} ="):
            continue
        head = line.split("=", 1)[1].strip()
        if head:
            return head
        body: list[str] = []
        for following in lines[index + 1 :]:
            if following.strip() and not following.startswith(" "):
                break
            body.append(following.strip())
        return "\n".join(body).strip()
    raise AssertionError(f"{key} is not in {path.name}")


# Pins spec/deck-options.md#deck-options.steps-warning-names-the-algorithm
def test_the_long_steps_warning_names_the_algorithm_not_fsrs() -> None:
    warning = english_message("deck-config-steps-too-large-for-algorithm")
    assert warning == (
        "When { $algorithm } is enabled, steps of 1 day or more are not recommended."
    )
    # the algorithm's own name goes in; FSRS is only one of them
    assert "FSRS" not in warning


# Pins spec/ui.md#ui.stats-model-metrics (the one line under each graph)
def test_the_metric_graphs_say_which_way_is_better_in_one_line() -> None:
    assert (
        english_message("statistics-um-plus-verdict", STATISTICS_FTL)
        == "UM+ and slope: closer to 0 is better."
    )
    assert (
        english_message("statistics-roc-verdict", STATISTICS_FTL)
        == "AUC: higher is better, and 0.5 is random chance."
    )


def test_simple_mode_names_the_bury_switch_without_bury_or_sibling() -> None:
    title = english_message("deck-config-hide-related-cards")
    assert title == "Hide related cards until tomorrow"
    assert "bury" not in title.lower()
    assert "sibling" not in title.lower()


def test_the_ui_split_row_uses_simple_modes_name() -> None:
    from aqt.utils import tr

    rows = [
        row
        for _section, items in ui_split.DECK_OPTIONS_SETTINGS
        for row in items
        if row[0] == "burySiblings"
    ]
    assert len(rows) == 1
    # bound methods of the same object are equal but not identical
    assert rows[0][1] == tr.deck_config_hide_related_cards


def test_the_three_per_type_bury_switches_are_gone() -> None:
    """One switch in both modes, so no mode sets the three settings on their
    own (spec deck-options.simple-view)."""
    ids = {
        row[0] for _section, items in ui_split.DECK_OPTIONS_SETTINGS for row in items
    }
    assert "burySiblings" in ids
    for gone in ("buryNew", "buryReviews", "buryInterdayLearning"):
        assert gone not in ids


def test_the_underlined_term_is_inside_the_label() -> None:
    """Otherwise nothing is underlined and the label shows plain
    (spec deck-options.glossary-term)."""
    title = english_message("deck-config-hide-related-cards")
    term = english_message("deck-config-hide-related-cards-term")
    assert term in title
    assert term == "related cards"


def test_the_hover_explains_the_term_without_using_it() -> None:
    hover = english_message("deck-config-hide-related-cards-hover")
    assert hover == "cards that belong to the same note"
    # a few words, and no full stop: it sits in a tooltip, not a paragraph
    assert len(hover.split()) <= 8
    assert not hover.endswith(".")
    # the word it exists to avoid
    assert "sibling" not in hover.lower()


def test_the_simple_bury_help_is_short_and_says_related_cards() -> None:
    help_text = english_message("deck-config-hide-related-cards-tooltip")
    assert "related cards" in help_text.lower()
    assert "sibling" not in help_text.lower()
    # two short paragraphs, not the five-part Advanced text
    assert len(help_text) < 400
    assert help_text.count("\n\n") == 1
