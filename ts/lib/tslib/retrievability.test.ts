// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { termParts } from "./retrievability";

// Pins spec/ui.md#ui.retrievability-advanced-only (the hover)
test("every occurrence of the word is cut out, whatever its case", () => {
    expect(termParts("Average retrievability", "retrievability")).toStrictEqual([
        { text: "Average ", term: false },
        { text: "retrievability", term: true },
    ]);
    expect(termParts("Retrievability: the retrievability of a card", "retrievability")).toStrictEqual([
        { text: "Retrievability", term: true },
        { text: ": the ", term: false },
        { text: "retrievability", term: true },
        { text: " of a card", term: false },
    ]);
});

test("a text without the word, or without a word to look for, stays whole", () => {
    expect(termParts("Stability", "retrievability")).toStrictEqual([{ text: "Stability", term: false }]);
    expect(termParts("Retrievability", "")).toStrictEqual([{ text: "Retrievability", term: false }]);
    expect(termParts("", "retrievability")).toStrictEqual([]);
});
