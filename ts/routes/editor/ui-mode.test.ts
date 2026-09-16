// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import {
    ADVANCED_ONLY_EDITOR_BUTTONS,
    EDITOR_BUTTONS,
    editorButtonsForMode,
} from "./ui-mode";

// Pins spec/ui.md#ui.editor-simple-view: the list Simple mode shows.
test("Simple mode shows only the Simple editor buttons, in toolbar order", () => {
    expect(editorButtonsForMode(false)).toStrictEqual([
        "fields",
        "bold",
        "italic",
        "underline",
        "textColor",
        "removeFormat",
        "attachMedia",
    ]);
});

// Pins spec/ui.md#ui.editor-simple-view: Advanced mode hides nothing.
test("Advanced mode shows every editor button", () => {
    expect(editorButtonsForMode(true)).toStrictEqual(EDITOR_BUTTONS);
});

// Pins spec/ui.md#ui.editor-simple-view: the Advanced-only list.
test("the Advanced-only buttons are the ones Simple drops", () => {
    const simple = editorButtonsForMode(false);
    const dropped = EDITOR_BUTTONS.filter((button) => !simple.includes(button));
    expect(dropped).toStrictEqual([...ADVANCED_ONLY_EDITOR_BUTTONS]);
});
