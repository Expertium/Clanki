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

// Pins spec/ui.md#ui.stats-total-knowledge: the page keys each graph's block
// by the component, so a graph both modes show keeps the same key over a mode
// switch. Its block, and with it everything the graph loaded on its own
// (Total Knowledge's response and its RWKV job), survives the switch.
test("a mode switch keeps one block per graph that both modes show", () => {
    const graphs = [{ id: "today" }, { id: "reviews" }, { id: "knowledge" }];
    const simple = [graphs[1], graphs[2]];
    const before = graphsForMode(graphs, simple, false);
    const after = graphsForMode(graphs, simple, true);
    for (const graph of before) {
        // the same object, so the keyed block is moved, never rebuilt
        expect(after.filter((other) => other === graph)).toHaveLength(1);
    }
});
