// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The note editor's Simple | Advanced split (spec ui.editor-simple-view).
 *
 * Only the editor's own buttons are listed here. Buttons an add-on adds
 * (`editor_did_init_buttons`, `editor_did_init_left_buttons`) are not in the
 * list and show in both modes.
 */

import { ConfigKey_Bool } from "@generated/anki/config_pb";
import { getConfigBool } from "@generated/backend";
import { itemShown, loadSimpleItems, type SimpleItems } from "@tslib/ui-split";
import { derived, get, writable } from "svelte/store";

/** A button of the editor's own toolbar. */
export type EditorButton =
    | "fields"
    | "cards"
    | "settings"
    | "bold"
    | "italic"
    | "underline"
    | "superscript"
    | "subscript"
    | "textColor"
    | "highlightColor"
    | "removeFormat"
    | "unorderedList"
    | "orderedList"
    | "alignment"
    | "attachMedia"
    | "recordAudio"
    | "mathjax";

/** Every editor button, in toolbar order. */
export const EDITOR_BUTTONS: EditorButton[] = [
    "fields",
    "cards",
    "settings",
    "bold",
    "italic",
    "underline",
    "superscript",
    "subscript",
    "textColor",
    "highlightColor",
    "removeFormat",
    "unorderedList",
    "orderedList",
    "alignment",
    "attachMedia",
    "recordAudio",
    "mathjax",
];

/** The split's item id of a button (qt/aqt/ui_split.py). */
export function editorItemId(button: EditorButton): string {
    return `editor.${button}`;
}

/**
 * The buttons the toolbar shows, in toolbar order: every button in Advanced
 * mode; in Simple mode those the split gives it (spec ui.split-configurable,
 * whose defaults are ui.editor-simple-view's list). Without the split every
 * button shows.
 */
export function editorButtonsForMode(advanced: boolean, simpleItems: SimpleItems | null): EditorButton[] {
    return EDITOR_BUTTONS.filter((button) => itemShown(editorItemId(button), advanced, simpleItems));
}

/**
 * The collection's UI mode: read once when the editor page loads, then set by
 * the toolbar's own switch. A hidden button stays in the DOM, so its keyboard
 * shortcut and its formats keep working in both modes.
 *
 * It starts as Advanced, so that no button is missing before the flag has
 * arrived.
 */
const mode = writable(true);

/** What the toolbar reads. */
export const advancedUi = { subscribe: mode.subscribe };

/** The split, read once per page load; null until then (every button
 * shows). */
const simpleItems = writable<SimpleItems | null>(null);

/** The buttons the toolbar shows now. */
export const shownButtons = derived(
    [mode, simpleItems],
    ([$mode, $simpleItems]) => new Set(editorButtonsForMode($mode, $simpleItems)),
);

/** The mode the toolbar's own switch chose, if it was used. */
let chosen = false;

/**
 * What the toolbar's switch writes. It also remembers that the user chose,
 * so that a slow answer of the first read cannot switch the page back.
 */
export const modeSwitch = {
    subscribe: mode.subscribe,
    set(advanced: boolean): void {
        chosen = true;
        mode.set(advanced);
    },
    update(updater: (advanced: boolean) => boolean): void {
        modeSwitch.set(updater(get(mode)));
    },
};

let asked = false;

/** Reads the flag once per page load. */
export function loadEditorUiMode(): void {
    if (asked) {
        return;
    }
    asked = true;
    loadSimpleItems().then((items) => simpleItems.set(items));
    getConfigBool({ key: ConfigKey_Bool.ADVANCED_UI }, { alertOnError: false })
        .then(({ val }) => {
            if (!chosen) {
                mode.set(val);
            }
        })
        .catch(() => {
            // the toolbar keeps the mode it has
        });
}
