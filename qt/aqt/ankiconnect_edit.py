# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The Edit dialog that AnkiConnect's guiEditNote opens, ported from the
AnkiConnect add-on's edit.py (GPLv3 or later). Like Edit Current, but:

* it has a Preview button to preview the cards of the note;
* it has Previous/Next buttons to go through the dialog's history;
* it has a Browse button to open that history in the Browser;
* it has no bar with the Close button.

The registry, geometry and search tags keep the add-on's names, so its
saved window size carries over.
"""

from __future__ import annotations

from typing import Any

import aqt
import aqt.browser.previewer
import aqt.editcurrent
import aqt.editor
import aqt.forms
from anki.consts import QUEUE_TYPE_SUSPENDED
from anki.errors import NotFoundError
from anki.notes import NoteId
from anki.utils import ids2str
from aqt import gui_hooks
from aqt.qt import QCloseEvent, QKeySequence, QMainWindow, QShortcut, Qt
from aqt.utils import restoreGeom, saveGeom, tooltip

DOMAIN_PREFIX = "foosoft.ankiconnect."


def get_note_by_note_id(note_id: int) -> Any:
    return aqt.mw.col.get_note(NoteId(note_id))


def is_card_suspended(card: Any) -> bool:
    return card.queue == QUEUE_TYPE_SUSPENDED


def filter_valid_note_ids(note_ids: list[int]) -> list[int]:
    return aqt.mw.col.db.list("select id from notes where id in " + ids2str(note_ids))


class DecentPreviewer(aqt.browser.previewer.MultiCardPreviewer):
    class Adapter:
        def get_current_card(self) -> Any:
            raise NotImplementedError

        def can_select_previous_card(self) -> bool:
            raise NotImplementedError

        def can_select_next_card(self) -> bool:
            raise NotImplementedError

        def select_previous_card(self) -> None:
            raise NotImplementedError

        def select_next_card(self) -> None:
            raise NotImplementedError

    def __init__(self, adapter: Adapter) -> None:
        super().__init__(parent=None, mw=aqt.mw, on_close=lambda: None)  # type: ignore[arg-type]
        self.adapter = adapter
        self.last_card_id = 0

    def card(self) -> Any:
        return self.adapter.get_current_card()

    def card_changed(self) -> bool:
        current_card_id = self.adapter.get_current_card().id
        changed = self.last_card_id != current_card_id
        self.last_card_id = current_card_id
        return changed

    # the buttons are sometimes disabled a little late and can still be
    # pressed, so check again
    def _on_prev_card(self) -> None:
        if self.adapter.can_select_previous_card():
            self.adapter.select_previous_card()
            self.render_card()

    def _on_next_card(self) -> None:
        if self.adapter.can_select_next_card():
            self.adapter.select_next_card()
            self.render_card()

    def _should_enable_prev(self) -> bool:
        return (
            self.showing_answer_and_can_show_question()
            or self.adapter.can_select_previous_card()
        )

    def _should_enable_next(self) -> bool:
        return (
            self.showing_question_and_can_show_answer()
            or self.adapter.can_select_next_card()
        )

    def _render_scheduled(self) -> None:
        super()._render_scheduled()
        self._updateButtons()

    def showing_answer_and_can_show_question(self) -> bool:
        return self._state == "answer" and not self._show_both_sides

    def showing_question_and_can_show_answer(self) -> bool:
        return self._state == "question"


class ReadyCardsAdapter(DecentPreviewer.Adapter):
    def __init__(self, cards: list[Any]) -> None:
        self.cards = cards
        self.current = 0

    def get_current_card(self) -> Any:
        return self.cards[self.current]

    def can_select_previous_card(self) -> bool:
        return self.current > 0

    def can_select_next_card(self) -> bool:
        return self.current < len(self.cards) - 1

    def select_previous_card(self) -> None:
        self.current -= 1

    def select_next_card(self) -> None:
        self.current += 1


class History:
    """Note ids, not notes: note objects do not implement __eq__."""

    number_of_notes_to_keep_in_history = 25

    def __init__(self) -> None:
        self.note_ids: list[int] = []

    def append(self, note: Any) -> None:
        if note.id in self.note_ids:
            self.note_ids.remove(note.id)
        self.note_ids.append(note.id)
        self.note_ids = self.note_ids[-self.number_of_notes_to_keep_in_history :]

    def has_note_to_left_of(self, note: Any) -> bool:
        return note.id in self.note_ids and note.id != self.note_ids[0]

    def has_note_to_right_of(self, note: Any) -> bool:
        return note.id in self.note_ids and note.id != self.note_ids[-1]

    def get_note_to_left_of(self, note: Any) -> Any:
        note_id = self.note_ids[self.note_ids.index(note.id) - 1]
        return get_note_by_note_id(note_id)

    def get_note_to_right_of(self, note: Any) -> Any:
        note_id = self.note_ids[self.note_ids.index(note.id) + 1]
        return get_note_by_note_id(note_id)

    def get_last_note(self) -> Any:  # IndexError when the history is empty
        return get_note_by_note_id(self.note_ids[-1])

    def remove_invalid_notes(self) -> None:
        self.note_ids = filter_valid_note_ids(self.note_ids)


history = History()


def trigger_search_for_dialog_history_notes(
    search_context: Any, use_history_order: bool
) -> None:
    search_context.search = " or ".join(
        f"nid:{note_id}" for note_id in history.note_ids
    )

    if use_history_order:
        search_context.order = f"""case c.nid {
            " ".join(
                f"when {note_id} then {n}"
                for (n, note_id) in enumerate(reversed(history.note_ids))
            )
        } end asc"""


class Edit(aqt.editcurrent.EditCurrent):
    dialog_geometry_tag = DOMAIN_PREFIX + "edit"
    dialog_registry_tag = DOMAIN_PREFIX + "Edit"
    dialog_search_tag = DOMAIN_PREFIX + "edit.history"

    # aqt.dialogs.open() calls the constructor the first time and reopen()
    # while the dialog exists
    def __init__(self, note: Any) -> None:
        QMainWindow.__init__(self, None, Qt.WindowType.Window)
        self.form = aqt.forms.editcurrent.Ui_Dialog()
        self.form.setupUi(self)
        self.setWindowTitle("Edit")
        self.setMinimumWidth(250)
        self.setMinimumHeight(400)
        restoreGeom(self, self.dialog_geometry_tag)

        # no Close button bar: the form has none, and EditCurrent's own
        # __init__, which adds it, is not called
        self.setup_editor_buttons()

        self.show()
        self.bring_to_foreground()

        history.remove_invalid_notes()
        history.append(note)
        self.show_note(note)

        gui_hooks.operation_did_execute.append(self.on_operation_did_execute)
        gui_hooks.editor_did_load_note.append(self.editor_did_load_note)

    def reopen(self, note: Any) -> None:  # type: ignore[override]
        history.append(note)
        self.show_note(note)
        self.bring_to_foreground()

    def cleanup(self) -> None:
        gui_hooks.editor_did_load_note.remove(self.editor_did_load_note)
        gui_hooks.operation_did_execute.remove(self.on_operation_did_execute)

        self.editor.cleanup()
        saveGeom(self, self.dialog_geometry_tag)
        aqt.dialogs.markClosed(self.dialog_registry_tag)

    def closeEvent(self, evt: QCloseEvent | None) -> None:
        self.editor.call_after_note_saved(self.cleanup)

    # Brings the window to the front; without it, a dialog opened from
    # Yomitan appears behind other windows on Windows (see the add-on's
    # notes on SetForegroundWindow and QWidget::activateWindow).
    def bring_to_foreground(self) -> None:
        aqt.mw.app.processEvents()
        self.activateWindow()
        self.raise_()

    # hooks while the dialog is open

    def on_operation_did_execute(self, changes: Any, handler: Any) -> None:  # type: ignore[override]
        if changes.note_text and handler is not self.editor:
            self.reload_notes_after_user_action_elsewhere()

    def editor_did_load_note(self, _editor: Any) -> None:
        self.enable_disable_next_and_previous_buttons()

    # loading notes

    # editor.card makes the "Cards…" button work
    def show_note(self, note: Any) -> None:
        self.note = note
        cards = note.cards()

        self.editor.set_note(note)
        self.editor.card = cards[0] if cards else None

        if any(is_card_suspended(card) for card in cards):
            tooltip(
                "Some of the cards associated with this note have been suspended",
                parent=self,
            )

    def reload_notes_after_user_action_elsewhere(self) -> None:
        history.remove_invalid_notes()

        try:
            self.note.load()  # also updates the fields
        except NotFoundError:
            try:
                self.note = history.get_last_note()
            except IndexError:
                self.cleanup()
                return

        self.show_note(self.note)

    # actions

    # Searches twice: once to select the note's cards, then for the whole
    # history, keeping that selection. The sort column set to the search
    # tag hides the sort indicator and asks for the history order.
    def show_browser(self, *_: Any) -> None:
        def search_input_select_all(hook_browser: Any, *_: Any) -> None:
            hook_browser.form.searchEdit.lineEdit().selectAll()
            gui_hooks.browser_did_change_row.remove(search_input_select_all)

        gui_hooks.browser_did_change_row.append(search_input_select_all)

        browser = aqt.dialogs.open("Browser", aqt.mw)
        browser.table._state.sort_column = self.dialog_search_tag
        browser.table._set_sort_indicator()

        browser.search_for(f"nid:{self.note.id}")
        browser.table.select_all()
        browser.search_for(self.dialog_search_tag)

    def show_preview(self, *_: Any) -> Any:
        if cards := self.note.cards():
            previewer = DecentPreviewer(ReadyCardsAdapter(cards))
            previewer.open()
            return previewer
        else:
            tooltip("No cards found", parent=self)
            return None

    def show_previous(self, *_: Any) -> None:
        if history.has_note_to_left_of(self.note):
            self.show_note(history.get_note_to_left_of(self.note))

    def show_next(self, *_: Any) -> None:
        if history.has_note_to_right_of(self.note):
            self.show_note(history.get_note_to_right_of(self.note))

    # buttons and keys

    def setup_editor_buttons(self) -> None:
        gui_hooks.editor_did_init.append(self.add_preview_button)
        gui_hooks.editor_did_init_buttons.append(self.add_right_hand_side_buttons)

        # browser mode shows the Preview button
        self.editor = aqt.editor.Editor(
            aqt.mw,
            self.form.fieldsArea,
            self,
            editor_mode=aqt.editor.EditorMode.BROWSER,
        )

        gui_hooks.editor_did_init_buttons.remove(self.add_right_hand_side_buttons)
        gui_hooks.editor_did_init.remove(self.add_preview_button)

    def add_preview_button(self, editor: Any) -> None:
        QShortcut(QKeySequence("Ctrl+Shift+P"), self, self.show_preview)
        editor._links["preview"] = lambda _editor: self.show_preview() and None

    def add_right_hand_side_buttons(self, buttons: list[str], editor: Any) -> None:
        extra_button_class = "anki-connect-button"
        editor.web.eval(
            """
            (function(){
                const style = document.createElement("style");
                style.innerHTML = `
                    .anki-connect-button {
                        white-space: nowrap;
                        width: auto;
                        padding: 0 2px;
                        font-size: var(--base-font-size);
                    }
                    .anki-connect-button:disabled {
                        pointer-events: none;
                        opacity: .4;
                    }
                `;
                document.head.appendChild(style);
            })();
        """
        )

        def add(cmd: str, function: Any, label: str, tip: str, keys: str) -> None:
            button_html = editor.addButton(
                icon=None,
                cmd=DOMAIN_PREFIX + cmd,
                id=DOMAIN_PREFIX + cmd,
                func=function,
                label=f"&nbsp;&nbsp;{label}&nbsp;&nbsp;",
                tip=f"{tip} ({keys})",
                keys=keys,
            )

            button_html = button_html.replace(
                'class="', f'class="{extra_button_class} '
            )
            buttons.append(button_html)

        add("browse", self.show_browser, "Browse", "Browse", "Ctrl+F")
        add("previous", self.show_previous, "&lt;", "Previous", "Alt+Left")
        add("next", self.show_next, "&gt;", "Next", "Alt+Right")

    def run_javascript_after_toolbar_ready(self, js: str) -> None:
        js = f"setTimeout(function() {{ {js} }}, 1)"
        js = f'require("anki/ui").loaded.then(() => {js})'
        self.editor.web.eval(js)

    def enable_disable_next_and_previous_buttons(self) -> None:
        def to_js(boolean: bool) -> str:
            return "true" if boolean else "false"

        disable_previous = not (history.has_note_to_left_of(self.note))
        disable_next = not (history.has_note_to_right_of(self.note))

        self.run_javascript_after_toolbar_ready(
            f"""
            document.getElementById("{DOMAIN_PREFIX}previous")
                    .disabled = {to_js(disable_previous)};
            document.getElementById("{DOMAIN_PREFIX}next")
                    .disabled = {to_js(disable_next)};
        """
        )

    @classmethod
    def browser_will_search(cls, search_context: Any) -> None:
        if search_context.search == cls.dialog_search_tag:
            trigger_search_for_dialog_history_notes(
                search_context=search_context,
                use_history_order=cls.dialog_search_tag
                == search_context.browser.table._state.sort_column,
            )

    @classmethod
    def register_with_anki(cls) -> None:
        if cls.dialog_registry_tag not in aqt.dialogs._dialogs:
            aqt.dialogs.register_dialog(cls.dialog_registry_tag, cls)
            gui_hooks.browser_will_search.append(cls.browser_will_search)

    @classmethod
    def open_dialog_and_show_note_with_id(cls, note_id: int) -> Any:  # NotFoundError
        cls.register_with_anki()
        note = get_note_by_note_id(note_id)
        return aqt.dialogs.open(cls.dialog_registry_tag, note)
