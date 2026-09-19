// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import {
    allSettings,
    CURATED,
    deckOptionsItemId,
    IN_SIMPLE_SECTION,
    type Section,
    SECTIONS,
    sectionShown,
    settingShown,
} from "./ui-split";

// The split as Python sends it with no choices made (qt/aqt/ui_split.py
// pins these defaults): the Simple section's settings only.
const defaults = Object.fromEntries(
    allSettings().map((key) => [deckOptionsItemId(key), (CURATED as readonly string[]).includes(key)]),
);
const sections = Object.keys(SECTIONS) as Section[];

// Pins spec/deck-options.md#deck-options.simple-view: by default Simple mode
// is the one Simple section, with its own settings and no other section.
test("the default split draws the Simple section only", () => {
    for (const key of allSettings()) {
        expect(settingShown(key, "simple", false, defaults)).toBe((CURATED as readonly string[]).includes(key));
        expect(settingShown(key, "section", false, defaults)).toBe(false);
    }
    for (const section of sections) {
        expect(sectionShown(section, false, defaults)).toBe(false);
    }
});

// Pins spec/deck-options.md#deck-options.advanced-view: Advanced mode draws
// the sections with every setting, whatever the split says.
test("Advanced mode draws every setting in its section", () => {
    const none = Object.fromEntries(allSettings().map((key) => [deckOptionsItemId(key), false]));
    for (const key of allSettings()) {
        expect(settingShown(key, "section", true, none)).toBe(true);
        expect(settingShown(key, "simple", true, none)).toBe(false);
    }
    for (const section of sections) {
        expect(sectionShown(section, true, none)).toBe(true);
    }
});

// Pins spec/ui.md#ui.split-configurable for deck options: an added setting
// shows once, where it belongs; a removed one is gone.
test("Simple mode follows the split in both directions", () => {
    const choices = {
        ...defaults,
        "deckOptions.learningSteps": true,
        "deckOptions.reviewLimit": true,
        "deckOptions.fsrsParams": true,
        "deckOptions.burySiblings": false,
    };
    // a setting of its own section: that section, below the Simple one
    expect(settingShown("learningSteps", "section", false, choices)).toBe(true);
    expect(settingShown("learningSteps", "simple", false, choices)).toBe(false);
    expect(sectionShown("newCards", false, choices)).toBe(true);
    expect(sectionShown("lapses", false, choices)).toBe(false);
    // a setting that shares its control with the Simple section: there
    for (const key of ["reviewLimit", "fsrsParams"] as const) {
        expect(settingShown(key, "simple", false, choices)).toBe(true);
        expect(settingShown(key, "section", false, choices)).toBe(false);
    }
    expect(sectionShown("dailyLimits", false, choices)).toBe(false);
    expect(sectionShown("algorithm", false, choices)).toBe(false);
    // a Simple section's setting taken out
    expect(settingShown("burySiblings", "simple", false, choices)).toBe(false);
});

// The Simple section's own settings never appear twice in Simple mode.
test("a Simple section setting is never drawn in a section in Simple mode", () => {
    const all = Object.fromEntries(allSettings().map((key) => [deckOptionsItemId(key), true]));
    for (const key of IN_SIMPLE_SECTION) {
        expect(settingShown(key as never, "section", false, all)).toBe(false);
    }
    expect(sectionShown("easyDays", false, all)).toBe(false);
});

test("without the split every setting shows", () => {
    expect(settingShown("learningSteps", "section", false, null)).toBe(true);
    expect(settingShown("newLimit", "simple", false, null)).toBe(true);
});

test("every setting is listed once", () => {
    const keys = allSettings();
    expect(new Set(keys).size).toBe(keys.length);
});
