// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { EDITOR_BUTTONS, editorButtonsForMode, editorItemId } from "./ui-mode";

// The split as Python sends it with no choices made: the defaults of
// spec/ui.md#ui.editor-simple-view (qt/aqt/ui_split.py pins them).
const DEFAULT_SIMPLE = ["fields", "bold", "italic", "underline", "textColor", "removeFormat", "attachMedia"];
const defaults = Object.fromEntries(
    EDITOR_BUTTONS.map((button) => [editorItemId(button), DEFAULT_SIMPLE.includes(button)]),
);

// Pins spec/ui.md#ui.editor-simple-view: the list Simple mode shows.
test("Simple mode shows only the Simple editor buttons, in toolbar order", () => {
    expect(editorButtonsForMode(false, defaults)).toStrictEqual(DEFAULT_SIMPLE);
});

// Pins spec/ui.md#ui.editor-simple-view: Advanced mode hides nothing.
test("Advanced mode shows every editor button", () => {
    const none = Object.fromEntries(EDITOR_BUTTONS.map((button) => [editorItemId(button), false]));
    expect(editorButtonsForMode(true, none)).toStrictEqual(EDITOR_BUTTONS);
});

// Pins spec/ui.md#ui.split-configurable: the user's choices, both ways.
test("Simple mode follows the split in both directions", () => {
    const choices = { ...defaults, "editor.mathjax": true, "editor.bold": false };
    const shown = editorButtonsForMode(false, choices);
    expect(shown).toContain("mathjax");
    expect(shown).not.toContain("bold");
    expect(shown.indexOf("mathjax")).toBeGreaterThan(shown.indexOf("attachMedia"));
});

// Without the split (not read yet, or unreadable) nothing is hidden.
test("without the split every button shows", () => {
    expect(editorButtonsForMode(false, null)).toStrictEqual(EDITOR_BUTTONS);
});
