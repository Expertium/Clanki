// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { GraphsRequest_Graph as Graph } from "@generated/anki/stats_pb";
import { expect, test } from "vitest";

import { graphsForMode, simpleDataOf, simpleGraphsOf, statsItemId } from "./ui-mode";

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

// The page's graphs as +page.svelte lists them (ids and data).
const items = [
    { id: "today", data: [Graph.TODAY] },
    { id: "reviews", data: [Graph.REVIEWS] },
    { id: "cardCounts", data: [Graph.CARD_COUNTS] },
    { id: "intervals", data: [Graph.INTERVALS] },
    { id: "totalKnowledge", data: [] },
    { id: "roc", data: [] },
    { id: "trueRetention", data: [Graph.TRUE_RETENTION] },
];
const names = items.map((item) => item.id);
// the split as Python sends it with no choices made (qt/aqt/ui_split.py)
const defaults = Object.fromEntries(
    items.map((item) => [
        statsItemId(item),
        ["reviews", "cardCounts", "totalKnowledge", "trueRetention"].includes(item.id),
    ]),
);

// Pins spec/ui.md#ui.mode-switch with the split's defaults: the same four
// graphs and the same data request as before the split was configurable.
test("the default split shows the four Simple graphs and asks for their data", () => {
    expect(simpleGraphsOf(names, items, defaults)).toStrictEqual([
        "reviews",
        "cardCounts",
        "totalKnowledge",
        "trueRetention",
    ]);
    expect(simpleDataOf(items, defaults)).toStrictEqual([
        Graph.REVIEWS,
        Graph.CARD_COUNTS,
        Graph.TRUE_RETENTION,
    ]);
});

// Pins spec/ui.md#ui.split-configurable: a graph moved either way.
test("Simple mode follows the split in both directions", () => {
    const choices = { ...defaults, "stats.intervals": true, "stats.reviews": false };
    expect(simpleGraphsOf(names, items, choices)).toStrictEqual([
        "cardCounts",
        "intervals",
        "totalKnowledge",
        "trueRetention",
    ]);
    expect(simpleDataOf(items, choices)).toStrictEqual([
        Graph.CARD_COUNTS,
        Graph.INTERVALS,
        Graph.TRUE_RETENTION,
    ]);
});

test("graphs with their own request ask for one small part only", () => {
    const own = Object.fromEntries(items.map((item) => [statsItemId(item), item.id === "roc"]));
    expect(simpleGraphsOf(names, items, own)).toStrictEqual(["roc"]);
    // an empty list would mean every graph
    expect(simpleDataOf(items, own)).toStrictEqual([Graph.CARD_COUNTS]);
});

test("without the split every graph shows, with all its data", () => {
    expect(simpleGraphsOf(names, items, null)).toStrictEqual(names);
    expect(simpleDataOf(items, null)).toStrictEqual([]);
});
