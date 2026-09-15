// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { graphsForMode } from "./ui-mode";

// Pins spec/ui.md#ui.mode-switch: Simple shows only the Simple graphs, in
// page order; Advanced shows all; a page without a Simple list shows all.
test("Simple mode keeps only the Simple graphs, in page order", () => {
    const graphs = ["today", "reviews", "counts", "intervals", "retention", "buttons"];
    const simple = ["retention", "reviews", "counts"];
    expect(graphsForMode(graphs, simple, false)).toStrictEqual(["reviews", "counts", "retention"]);
    expect(graphsForMode(graphs, simple, true)).toStrictEqual(graphs);
    expect(graphsForMode(graphs, null, false)).toStrictEqual(graphs);
});
