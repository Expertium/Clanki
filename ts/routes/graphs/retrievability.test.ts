// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
import {
    GraphsResponse,
    GraphsResponse_Retrievability,
    GraphsResponse_Retrievability_Series,
} from "@generated/anki/stats_pb";
import * as tr from "@generated/ftl";
import { plainRecallWording, RecallWording } from "@tslib/recall-wording";
import { expect, test } from "vitest";

import type { GraphData } from "./retrievability";
import {
    fsrsColour,
    prepareData,
    retrievabilityTitle,
    rwkvColour,
    rwkvScoresPending,
    shouldShowRetrievabilityGraph,
} from "./retrievability";
import { algorithmName } from "./total-knowledge";

test("retrievability graph is shown when RWKV data exists without FSRS", () => {
    const sourceData = new GraphsResponse({
        fsrs: false,
        retrievability: new GraphsResponse_Retrievability({
            rwkv: new GraphsResponse_Retrievability_Series({
                retrievability: { 75: 1 },
            }),
        }),
    });

    expect(shouldShowRetrievabilityGraph(sourceData)).toBe(true);
});

// Pins spec/ui.md#ui.stats-one-algorithm
test("while RWKV calculates, the graph shows and says so, with no other values", () => {
    const pending = new GraphsResponse({
        fsrs: true,
        retrievability: new GraphsResponse_Retrievability({ rwkvPending: true }),
    });
    expect(shouldShowRetrievabilityGraph(pending)).toBe(true);
    expect(rwkvScoresPending(pending)).toBe(true);
    expect(rwkvScoresPending(new GraphsResponse({ fsrs: true }))).toBe(false);
});

test("retrievability graph remains available for FSRS without scored cards", () => {
    expect(shouldShowRetrievabilityGraph(new GraphsResponse({ fsrs: true }))).toBe(true);
});

test("retrievability graph stays hidden when neither FSRS nor RWKV is active", () => {
    expect(shouldShowRetrievabilityGraph(new GraphsResponse())).toBe(false);
});

function graphData(algorithm: SchedulingAlgorithm): GraphData {
    const series = {
        retrievability: new Map([[75, 1]]),
        average: 75,
        sumByCard: 0.75,
        sumByNote: 0.75,
    };
    const rwkv = algorithm !== SchedulingAlgorithm.FSRS7;

    return {
        active: series,
        fsrs: rwkv ? null : series,
        rwkv: rwkv ? series : null,
        algorithm,
    };
}

function drawn(algorithm: SchedulingAlgorithm): { query: string; label: string } {
    let query = "";
    const [histogram] = prepareData(
        graphData(algorithm),
        (_type, detail) => {
            query = detail.query;
        },
        true,
    );
    const bin = histogram!.series[0].bins.find((bin) => bin.length)!;
    histogram!.onClick!(bin);
    return { query, label: histogram!.series[0].label };
}

// Pins spec/ui.md#ui.stats-one-algorithm
test("a bar searches the collection's own algorithm's R, never another's", () => {
    expect(drawn(SchedulingAlgorithm.FSRS7).query).toBe(
        "\"prop:r>=0.75\" AND \"prop:r<0.8\"",
    );
    expect(drawn(SchedulingAlgorithm.RWKV_CURVE).query).toBe(
        "\"prop:rwkv-curve:r>=0.75\" AND \"prop:rwkv-curve:r<0.8\"",
    );
    expect(drawn(SchedulingAlgorithm.RWKV_INSTANT).query).toBe(
        "\"prop:rwkv:r>=0.75\" AND \"prop:rwkv:r<0.8\"",
    );
});

// Pins spec/ui.md#ui.stats-one-algorithm
test("the series is named after the algorithm, not just RWKV", () => {
    const labels = [
        SchedulingAlgorithm.FSRS7,
        SchedulingAlgorithm.RWKV_CURVE,
        SchedulingAlgorithm.RWKV_INSTANT,
    ].map((algorithm) => drawn(algorithm).label);
    expect(labels).toEqual([
        algorithmName(SchedulingAlgorithm.FSRS7),
        algorithmName(SchedulingAlgorithm.RWKV_CURVE),
        algorithmName(SchedulingAlgorithm.RWKV_INSTANT),
    ]);
    expect(new Set(labels).size).toBe(3);
});

// Pins spec/ui.md#ui.stats-graph-colours
test("the two retrievability series draw green and blue, and neither is amber", () => {
    expect(fsrsColour).toBe("#2f9e44");
    expect(rwkvColour).toBe("#1c7ed6");
    expect(fsrsColour).not.toBe(rwkvColour);
});

/** The graph never searches in these tests. */
function noSearch(): void {
    return;
}

// Pins spec/ui.md#ui.simple-recall-wording
test("with the plain wording the graph says probability of recall", () => {
    const plain = prepareData(graphData(SchedulingAlgorithm.FSRS7), noSearch, true, undefined, true);
    const technical = prepareData(graphData(SchedulingAlgorithm.FSRS7), noSearch, true, undefined, false);

    expect(plain[1][0].label).toBe(tr.statisticsAverageRetrievabilityPlain());
    expect(technical[1][0].label).toBe(tr.statisticsAverageRetrievability());
    expect(retrievabilityTitle(true)).toBe(tr.statisticsCardRetrievabilityTitlePlain());
    expect(retrievabilityTitle(false)).toBe(tr.statisticsCardRetrievabilityTitle());
});

// Pins spec/ui.md#ui.simple-recall-wording
test("the wording setting and the mode together choose the graph's title", () => {
    const cases: [RecallWording, boolean, string][] = [
        [RecallWording.BY_MODE, false, tr.statisticsCardRetrievabilityTitlePlain()],
        [RecallWording.BY_MODE, true, tr.statisticsCardRetrievabilityTitle()],
        [RecallWording.TECHNICAL, false, tr.statisticsCardRetrievabilityTitle()],
        [RecallWording.TECHNICAL, true, tr.statisticsCardRetrievabilityTitle()],
        [RecallWording.PLAIN, false, tr.statisticsCardRetrievabilityTitlePlain()],
        [RecallWording.PLAIN, true, tr.statisticsCardRetrievabilityTitlePlain()],
    ];
    for (const [setting, advanced, expected] of cases) {
        expect(retrievabilityTitle(plainRecallWording(setting, advanced))).toBe(expected);
    }
});
