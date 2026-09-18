// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The note editor's Simple | Advanced split (spec/ui.md, ui.editor-simple-view).
 */

import type { Locator, Page } from "@playwright/test";

import { expect, test } from "./fixtures";
import { bridgeCalls, chooseEditorMode, editableField } from "./helpers";

/** The buttons Simple mode keeps, by the start of their tooltip or label. */
const SIMPLE_BUTTONS = [
    "Fields...",
    "Bold text",
    "Italic text",
    "Underline text",
    "Text color",
    "Remove formatting",
    "Attach pictures",
];

/** The buttons only Advanced mode shows. */
const ADVANCED_ONLY_BUTTONS = [
    "Cards...",
    "Options",
    "Superscript",
    "Subscript",
    "Text highlight color",
    "Unordered list",
    "Ordered list",
    "Alignment",
    "Record audio",
    "Equations",
];

function toolbarButton(page: Page, name: string): Locator {
    if (name.endsWith("...")) {
        return page.locator(".editor-toolbar button", { hasText: name }).first();
    }
    return page.locator(`.editor-toolbar button[title^="${name}"]`).first();
}

async function expectSimpleToolbar(page: Page): Promise<void> {
    for (const name of SIMPLE_BUTTONS) {
        await expect(toolbarButton(page, name)).toBeVisible();
    }
    for (const name of ADVANCED_ONLY_BUTTONS) {
        await expect(toolbarButton(page, name)).toBeHidden();
    }
}

async function expectAdvancedToolbar(page: Page): Promise<void> {
    for (const name of [...SIMPLE_BUTTONS, ...ADVANCED_ONLY_BUTTONS]) {
        await expect(toolbarButton(page, name)).toBeVisible();
    }
}

test("the editor switch changes the toolbar at once and keeps the typed text", async ({ editor: page }) => {
    await chooseEditorMode(page, "Simple");
    await expectSimpleToolbar(page);

    const field = editableField(page, 0);
    await field.click();
    await page.keyboard.type("hello mode switch");
    await expect(field).toHaveText("hello mode switch");

    // The field element must survive the switch: a reload of the note would
    // replace it and lose the text.
    await field.evaluate((element) => {
        (element as unknown as Record<string, unknown>).__e2eMarker = true;
    });

    await chooseEditorMode(page, "Advanced");
    await expectAdvancedToolbar(page);

    await expect(field).toHaveText("hello mode switch");
    expect(
        await field.evaluate(
            (element) => (element as unknown as Record<string, unknown>).__e2eMarker,
        ),
    ).toBe(true);

    await chooseEditorMode(page, "Simple");
    await expectSimpleToolbar(page);
    await expect(field).toHaveText("hello mode switch");
});

test("a hidden button keeps its shortcut", async ({ editor: page }) => {
    await chooseEditorMode(page, "Simple");
    await expect(toolbarButton(page, "Cards...")).toBeHidden();

    await editableField(page, 0).click();
    await page.keyboard.press("Control+l");

    await expect.poll(() => bridgeCalls(page)).toContain("cards");
});

test("add-on buttons show in both modes", async ({ editor: page }) => {
    // The same call Python makes for `editor_did_init_buttons`
    // (qt/aqt/editor.py, `_set_ready`).
    await page.evaluate(() => {
        const w = window as any;
        w.require("anki/NoteEditor").instances[0].toolbar.toolbar.append({
            component: w.editorToolbar.AddonButtons,
            id: "addons",
            props: {
                buttons: [
                    "<button class=\"anki-addon-button linkb\" title=\"E2E add-on button\""
                    + " data-cantoggle=\"0\" data-command=\"e2eAddon\">addon</button>",
                ],
            },
        });
    });

    const addonButton = page.locator(".editor-toolbar button.anki-addon-button");
    await expect(addonButton).toBeVisible();

    await chooseEditorMode(page, "Simple");
    await expect(addonButton).toBeVisible();
    await expect(toolbarButton(page, "Cards...")).toBeHidden();

    await chooseEditorMode(page, "Advanced");
    await expect(addonButton).toBeVisible();

    await chooseEditorMode(page, "Simple");
});
