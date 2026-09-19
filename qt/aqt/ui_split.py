# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""The Simple | Advanced split as data (spec ui.split-configurable).

Every part of the interface that Simple mode may hide is one item of the
registry below: a stable id, an English name, the area and group it belongs
to, and whether Simple mode shows it by default. The defaults are the split
the earlier, fixed rules made (spec ui.simple-mode-tools-hidden,
ui.browser-simple-view, ui.reviewer-simple-view and the others), so nothing
changes until a user edits the split in Preferences > UI split.

Advanced mode shows every item. Simple mode shows an item when the user's
choice says so, else by the item's default. The collection config key
``uiSplit`` holds only the user's differences from the defaults, as a map
item id -> bool, so it syncs with the collection and an item added later
gets its own default.

The Simple | Advanced switch itself, Preferences and this tab are not items:
they are always shown, so no one can lock themselves out.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from anki.config import Config
from aqt.utils import tr

CONFIG_KEY = "uiSplit"


class Area(Enum):
    MAIN_WINDOW = "main"
    REVIEWER = "reviewer"
    BROWSER = "browser"


def _area_label(area: Area) -> str:
    return {
        Area.MAIN_WINDOW: tr.preferences_ui_split_main_window,
        Area.REVIEWER: tr.preferences_ui_split_reviewer,
        Area.BROWSER: tr.preferences_ui_split_browser,
    }[area]()


def plain(text: str) -> str:
    """A menu label without its keyboard accelerator marks."""
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


@dataclass(frozen=True)
class UiItem:
    id: str
    area: Area
    group: Callable[[], str] | None
    label: Callable[[], str]
    simple: bool
    """Whether Simple mode shows the item by default."""

    def area_label(self) -> str:
        return _area_label(self.area)


def _items(
    area: Area,
    group: Callable[[], str] | None,
    rows: Iterable[tuple[str, Callable[[], str], bool]],
) -> list[UiItem]:
    return [UiItem(id, area, group, label, simple) for id, label, simple in rows]


def _t(fn: Callable[[], str]) -> Callable[[], str]:
    """A label from a menu string: accelerator marks dropped."""
    return lambda: plain(fn())


def _both(first: Callable[[], str], second: Callable[[], str]) -> Callable[[], str]:
    return lambda: f"{first()} / {second()}"


def _retrievability_column() -> str:
    # Simple mode never says "retrievability" (spec ui.simple-recall-wording)
    import aqt

    mw = aqt.mw
    advanced = bool(mw and mw.col and mw.advanced_ui())
    return (
        tr.card_stats_fsrs_retrievability()
        if advanced
        else tr.card_stats_recall_probability()
    )


_MAIN = Area.MAIN_WINDOW
_BROWSER = Area.BROWSER

# The Browser's Cards-mode columns, in the order Simple mode shows them: the
# default four first, in their order, then the rest as the column list has
# them (spec ui.browser-simple-view).
BROWSER_COLUMNS: list[tuple[str, Callable[[], str], bool]] = [
    ("noteFld", tr.browsing_sort_field, True),
    ("deck", tr.decks_deck, True),
    ("cardDue", tr.statistics_due_date, True),
    ("cardIvl", tr.browsing_interval, True),
    ("question", tr.browsing_question, False),
    ("answer", tr.browsing_answer, False),
    ("template", tr.card_stats_card_template, False),
    ("note", tr.card_stats_note_type, False),
    ("noteTags", tr.editing_tags, False),
    ("cardEase", tr.browsing_ease, False),
    ("cardLapses", tr.scheduling_lapses, False),
    ("cardReps", tr.scheduling_reviews, False),
    ("noteCrt", tr.browsing_created, False),
    ("cardMod", tr.search_card_modified, False),
    ("noteMod", tr.search_note_modified, False),
    ("originalPosition", tr.card_stats_new_card_position, False),
    ("stability", tr.card_stats_fsrs_stability, False),
    ("difficulty", tr.card_stats_fsrs_difficulty, False),
    ("retrievability", _retrievability_column, False),
]


def browser_column_item_id(column: str) -> str:
    return f"browser.column.{column}"


ITEMS: list[UiItem] = [
    # Main window: Tools menu (spec ui.simple-mode-tools-hidden)
    *_items(
        _MAIN,
        _t(tr.qt_accel_tools),
        [
            ("main.tools.study_deck", tr.qt_misc_study_deck, True),
            ("main.tools.create_filtered", tr.qt_misc_create_filtered_deck, False),
            ("main.tools.check_database", _t(tr.qt_accel_check_database), False),
            ("main.tools.check_media", _t(tr.qt_accel_check_media), False),
            ("main.tools.empty_cards", tr.qt_misc_empty_cards, False),
            ("main.tools.addons", tr.qt_misc_addons, True),
            ("main.tools.note_types", tr.qt_misc_manage_note_types, False),
            ("main.tools.check_for_updates", tr.addons_check_for_updates, True),
        ],
    ),
    # the deck list's bottom row (spec ui.get-decks-and-get-addons)
    *_items(
        _MAIN,
        tr.preferences_ui_split_deck_list,
        [
            ("main.deck_list.find_decks_online", tr.decks_get_shared, True),
            ("main.deck_list.get_addons", tr.addons_get_addons, False),
            ("main.deck_list.create_deck", tr.decks_create_deck, True),
            ("main.deck_list.import_file", tr.decks_import_file, False),
        ],
    ),
    # the deck menu, the gear next to a deck (spec ui.mode-switch,
    # ui.advance-postpone)
    *_items(
        _MAIN,
        tr.preferences_ui_split_deck_menu,
        [
            ("main.deck_menu.rename", tr.actions_rename, True),
            ("main.deck_menu.options", tr.actions_options, True),
            (
                "main.deck_menu.advance_postpone",
                _both(tr.actions_advance_cards, tr.actions_postpone_cards),
                False,
            ),
            ("main.deck_menu.rwkv", tr.decks_rwkv, False),
            ("main.deck_menu.export", tr.actions_export, True),
            ("main.deck_menu.delete", tr.actions_delete, True),
        ],
    ),
    # the deck screen (overview): its bottom row
    # (spec ui.simple-mode-tools-hidden)
    *_items(
        _MAIN,
        tr.preferences_ui_split_deck_screen,
        [
            ("main.overview.options", tr.actions_options, True),
            ("main.overview.rebuild", tr.actions_rebuild, True),
            ("main.overview.empty", tr.studying_empty, True),
            ("main.overview.custom_study", tr.actions_custom_study, False),
            ("main.overview.unbury", tr.studying_unbury, True),
            ("main.overview.description", tr.scheduling_description, True),
        ],
    ),
    # the Learn count of the deck list and the deck screen; hidden, it is
    # added to Due (spec ui.simple-mode-deck-counts)
    *_items(
        _MAIN,
        tr.preferences_ui_split_counts,
        [("main.learn_count", tr.preferences_ui_split_learn_count, False)],
    ),
    # the reviewer's More menu (spec ui.reviewer-simple-view)
    *_items(
        Area.REVIEWER,
        None,
        [
            ("reviewer.flag", tr.studying_flag_card, False),
            ("reviewer.bury_card", tr.studying_bury_card, False),
            ("reviewer.reset_card", tr.actions_forget_card, False),
            ("reviewer.set_due_date", tr.actions_set_due_date, False),
            ("reviewer.suspend_card", tr.actions_suspend_card, True),
            ("reviewer.options", tr.actions_options, True),
            ("reviewer.card_info", tr.actions_card_info, True),
            ("reviewer.previous_card_info", tr.actions_previous_card_info, False),
            ("reviewer.tag_note", tr.studying_mark_note, True),
            ("reviewer.bury_note", tr.studying_bury_note, False),
            ("reviewer.suspend_note", tr.studying_suspend_note, False),
            ("reviewer.create_copy", tr.actions_create_copy, False),
            ("reviewer.delete_note", tr.studying_delete_note, True),
            ("reviewer.replay_audio", tr.actions_replay_audio, True),
            ("reviewer.pause_audio", tr.studying_pause_audio, True),
            ("reviewer.audio_back", tr.studying_audio_5s, False),
            ("reviewer.audio_forward", tr.studying_audio_and5s, False),
            ("reviewer.record_voice", tr.studying_record_own_voice, False),
            ("reviewer.replay_voice", tr.studying_replay_own_voice, False),
            ("reviewer.auto_advance", tr.actions_auto_advance, False),
        ],
    ),
    # the Browser (spec ui.browser-simple-view)
    *_items(
        _BROWSER,
        _t(tr.qt_accel_edit),
        [
            ("browser.edit.undo", _t(tr.qt_accel_undo), True),
            ("browser.edit.redo", _t(tr.qt_accel_redo), True),
            ("browser.edit.select_all", _t(tr.qt_accel_select_all), True),
            ("browser.edit.select_notes", _t(tr.qt_accel_select_notes), False),
            ("browser.edit.invert_selection", _t(tr.qt_accel_invert_selection), False),
            ("browser.edit.close", tr.actions_close, True),
            ("browser.edit.create_filtered", tr.qt_misc_create_filtered_deck, False),
        ],
    ),
    *_items(
        _BROWSER,
        _t(tr.qt_accel_go),
        [("browser.go", tr.preferences_ui_split_whole_menu, False)],
    ),
    *_items(
        _BROWSER,
        _t(tr.qt_accel_notes),
        [
            ("browser.notes.add", tr.browsing_add_notes, True),
            ("browser.notes.create_copy", tr.actions_create_copy, False),
            ("browser.notes.export", _t(tr.qt_accel_export_notes), False),
            ("browser.notes.add_tags", tr.browsing_add_tags2, True),
            ("browser.notes.remove_tags", tr.browsing_remove_tags, True),
            ("browser.notes.remove_leech_tag", tr.browsing_remove_leech_tag, True),
            ("browser.notes.clear_unused_tags", tr.browsing_clear_unused_tags, True),
            ("browser.notes.toggle_tag", tr.browsing_toggle_mark, True),
            ("browser.notes.change_note_type", tr.browsing_change_note_type2, False),
            (
                "browser.notes.find_duplicates",
                _t(tr.qt_accel_find_duplicates),
                False,
            ),
            (
                "browser.notes.find_and_replace",
                _t(tr.qt_accel_find_and_replace),
                False,
            ),
            ("browser.notes.note_types", tr.browsing_manage_note_types, False),
            ("browser.notes.delete", tr.actions_delete, True),
        ],
    ),
    *_items(
        _BROWSER,
        _t(tr.qt_accel_cards),
        [
            ("browser.cards.change_deck", tr.browsing_change_deck2, True),
            ("browser.cards.set_due_date", _t(tr.qt_accel_set_due_date), False),
            ("browser.cards.grade_now", tr.actions_grade_now, False),
            (
                "browser.cards.advance_postpone",
                _both(tr.actions_advance_cards, tr.actions_postpone_cards),
                False,
            ),
            ("browser.cards.reset", _t(tr.qt_accel_forget), False),
            ("browser.cards.reposition", tr.browsing_reposition, False),
            ("browser.cards.toggle_suspend", tr.browsing_toggle_suspend, True),
            ("browser.cards.toggle_bury", tr.browsing_toggle_bury, False),
            ("browser.cards.flag", tr.browsing_flag, False),
            ("browser.cards.info", _t(tr.qt_accel_info), True),
        ],
    ),
    *_items(
        _BROWSER,
        _t(tr.qt_accel_view),
        [
            (
                "browser.view.toggle_cards_notes",
                tr.browsing_toggle_showing_cards_notes,
                False,
            ),
            ("browser.view.full_screen", _t(tr.qt_accel_full_screen), True),
            ("browser.view.toggle_sidebar", tr.qt_accel_toggle_sidebar, True),
            ("browser.view.zoom_in", _t(tr.qt_accel_zoom_editor_in), True),
            ("browser.view.zoom_out", _t(tr.qt_accel_zoom_editor_out), True),
            ("browser.view.reset_zoom", _t(tr.qt_accel_reset_zoom), True),
            ("browser.view.layout", _t(tr.qt_accel_layout), False),
        ],
    ),
    *_items(
        _BROWSER,
        tr.preferences_ui_split_search_bar,
        [
            (
                "browser.cards_notes_switch",
                tr.preferences_ui_split_cards_notes_switch,
                False,
            )
        ],
    ),
    *_items(
        _BROWSER,
        tr.browsing_sidebar,
        [
            ("browser.sidebar.select_tool", tr.preferences_ui_split_select_tool, False),
            (
                "browser.sidebar.saved_searches",
                tr.browsing_sidebar_saved_searches,
                False,
            ),
            ("browser.sidebar.today", tr.browsing_today, True),
            ("browser.sidebar.flags", tr.browsing_sidebar_flags, False),
            ("browser.sidebar.card_state", tr.browsing_sidebar_card_state, True),
            ("browser.sidebar.decks", tr.browsing_sidebar_decks, True),
            ("browser.sidebar.note_types", tr.browsing_sidebar_notetypes, False),
            ("browser.sidebar.tags", tr.browsing_sidebar_tags, True),
        ],
    ),
    *_items(
        _BROWSER,
        tr.preferences_ui_split_columns,
        [
            (browser_column_item_id(column), label, simple)
            for column, label, simple in BROWSER_COLUMNS
        ],
    ),
]

ITEMS_BY_ID: dict[str, UiItem] = {item.id: item for item in ITEMS}


# Storage
######################################################################


def overrides(col: Any) -> dict[str, bool]:
    """The user's differences from the defaults (only booleans count)."""
    if col is None:
        return {}
    try:
        raw = col.get_config(CONFIG_KEY, {})
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(value, bool)}


def shown_in_simple(
    col: Any, item_id: str, choices: dict[str, bool] | None = None
) -> bool:
    """Whether Simple mode shows the item."""
    item = ITEMS_BY_ID[item_id]
    if choices is None:
        choices = overrides(col)
    return choices.get(item_id, item.simple)


def set_shown_in_simple(col: Any, item_id: str, shown: bool) -> None:
    """Store a choice; a choice equal to the default is not stored."""
    item = ITEMS_BY_ID[item_id]
    raw = col.get_config(CONFIG_KEY, {})
    stored = dict(raw) if isinstance(raw, dict) else {}
    if shown == item.simple:
        stored.pop(item_id, None)
    else:
        stored[item_id] = shown
    if stored:
        col.set_config(CONFIG_KEY, stored)
    else:
        col.remove_config(CONFIG_KEY)


def reset(col: Any) -> None:
    """Back to the defaults. Choices for ids this version does not know (a
    newer Clanki's items, synced here) are kept."""
    raw = col.get_config(CONFIG_KEY, {})
    stored = dict(raw) if isinstance(raw, dict) else {}
    kept = {key: value for key, value in stored.items() if key not in ITEMS_BY_ID}
    if kept:
        col.set_config(CONFIG_KEY, kept)
    else:
        col.remove_config(CONFIG_KEY)


# Lookup
######################################################################


def _every_item(item_id: str) -> bool:
    ITEMS_BY_ID[item_id]  # an unknown id is a bug in either mode
    return True


def visibility(mw: Any) -> Callable[[str], bool]:
    """A lookup "is this item shown in the current mode", reading the mode
    and the user's choices once."""
    if mw.advanced_ui():
        return _every_item
    choices = overrides(getattr(mw, "col", None))
    return lambda item_id: shown_in_simple(None, item_id, choices)


def shown(mw: Any, item_id: str) -> bool:
    """Whether the current mode shows the item: Advanced mode shows every
    item; Simple mode the ones the split gives it."""
    return visibility(mw)(item_id)


def col_visibility(col: Any) -> Callable[[str], bool]:
    """The same lookup, for code that has only the collection."""
    if col.get_config_bool(Config.Bool.ADVANCED_UI):
        return _every_item
    choices = overrides(col)
    return lambda item_id: shown_in_simple(None, item_id, choices)


# Menus
######################################################################


def show_in_menu(menu: Any, action: Any, show: bool, layout: list[Any]) -> None:
    """Take an action out of a menu, or put it back at its place in
    ``layout`` (the menu's actions as first built); add-on entries in the
    same menu stay where they are. An action taken out keeps its keyboard
    shortcut as long as the window holds it too, which ``setVisible(False)``
    would switch off."""
    present = action in menu.actions()
    if show == present:
        return
    if not show:
        menu.removeAction(action)
        return
    for later in layout[layout.index(action) + 1 :]:
        if later in menu.actions():
            menu.insertAction(later, action)
            return
    menu.addAction(action)
