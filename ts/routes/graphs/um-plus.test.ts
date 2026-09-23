// @vitest-environment jsdom
// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
import {
    ReviewMetricsProgress,
    ReviewMetricsProgress_State as JobState,
    UmPlusBin,
    UmPlusPair,
} from "@generated/anki/stats_pb";
import * as tr from "@generated/ftl";
import { expect, test } from "vitest";

import {
    chosenPair,
    PAIR_FLOOR,
    pairKey,
    pairOptions,
    renderUmPlus,
    SMALL_GROUP,
    thinPairNotes,
    umPlusBounds,
    umPlusView,
} from "./um-plus";

function bin(index: number, count: number, difference: number, errorA: number, errorB: number) {
    return new UmPlusBin({
        index,
        count,
        sumDifference: difference * count,
        sumErrorA: errorA * count,
        sumErrorB: errorB * count,
    });
}

function pair(): UmPlusPair {
    return new UmPlusPair({
        algorithmA: SchedulingAlgorithm.FSRS7,
        algorithmB: SchedulingAlgorithm.RWKV_INSTANT,
        bins: [
            bin(16, 1000, -0.2, -0.05, 0.01),
            bin(20, 4000, 0, 0.0, 0.0),
            bin(24, 50, 0.2, 0.09, 0.0),
        ],
        umA: 0.0345,
        umB: 0.0051,
        slopeA: 0.35,
        slopeB: -0.02,
        reviews: 5050,
    });
}

function progress(): ReviewMetricsProgress {
    return new ReviewMetricsProgress({ state: JobState.DONE, umPlus: [pair()] });
}

// Pins spec/ui.md#ui.stats-model-metrics
test("the menu offers the pairs the backend could compare", () => {
    const options = pairOptions(progress());

    expect(options).toHaveLength(1);
    expect(options[0].key).toBe(
        `${SchedulingAlgorithm.FSRS7}-${SchedulingAlgorithm.RWKV_INSTANT}`,
    );
    expect(options[0].label).toBe(
        tr.statisticsUmPlusPair({
            first: tr.deckConfigSchedulerChoiceFsrs(),
            second: tr.deckConfigSchedulerChoiceRwkvInstant(),
        }),
    );
    expect(pairOptions(null)).toEqual([]);
});

// Pins spec/ui.md#ui.stats-model-metrics
test("the chosen pair is kept, and an unknown one falls to the first", () => {
    const data = progress();

    expect(pairKey(chosenPair(data, pairOptions(data)[0].key)!)).toBe(
        pairOptions(data)[0].key,
    );
    expect(chosenPair(data, "9-9")).toBe(data.umPlus[0]);
    expect(chosenPair(null, null)).toBeNull();
});

/** A pair with too few shared ratings to be worth drawing. */
function thinPair(): UmPlusPair {
    return new UmPlusPair({
        algorithmA: SchedulingAlgorithm.FSRS7,
        algorithmB: SchedulingAlgorithm.RWKV_INSTANT,
        bins: [bin(20, 23, 0, 0.0, 0.0)],
        umA: 0.01,
        umB: 0.01,
        slopeA: 0,
        slopeB: 0,
        reviews: 23,
    });
}

// Pins spec/ui.md#ui.stats-model-metrics
test("a pair with too few shared reviews is named, not drawn", () => {
    const data = new ReviewMetricsProgress({
        state: JobState.DONE,
        umPlus: [thinPair()],
    });

    // Andrew's collection before FSRS-7's predictions exist: the one pair
    // rests on 23 ratings, so the menu offers nothing and nothing is drawn
    expect(pairOptions(data)).toHaveLength(0);
    expect(chosenPair(data, null)).toBeNull();

    // and the reason says what it has, what it needs and what would fill it
    const notes = thinPairNotes(data);
    expect(notes).toHaveLength(1);
    expect(notes[0]).toBe(
        tr.statisticsUmPlusTooFew({
            pair: tr.statisticsUmPlusPair({
                first: tr.deckConfigSchedulerChoiceFsrs(),
                second: tr.deckConfigSchedulerChoiceRwkvInstant(),
            }),
            reviews: 23,
            needed: String(PAIR_FLOOR),
        }),
    );

    // a pair at the floor is drawn, and says nothing
    const atFloor = new ReviewMetricsProgress({
        state: JobState.DONE,
        umPlus: [new UmPlusPair({ ...thinPair(), reviews: PAIR_FLOOR })],
    });
    expect(pairOptions(atFloor)).toHaveLength(1);
    expect(thinPairNotes(atFloor)).toHaveLength(0);
});

// Pins spec/ui.md#ui.stats-model-metrics
test("small groups are hidden until they are asked for, and counted", () => {
    const hidden = umPlusView(pair(), false)!;
    const shown = umPlusView(pair(), true)!;

    expect(SMALL_GROUP).toBe(200);
    expect(hidden.points).toHaveLength(2);
    expect(hidden.hidden).toBe(1);
    expect(shown.points).toHaveLength(3);
    expect(shown.hidden).toBe(0);
    // a point is the group's mean difference against its mean error, and
    // its bubble is the group's share of the ratings
    expect(shown.points[0].difference).toBeCloseTo(-0.2);
    expect(shown.points[0].errorA).toBeCloseTo(-0.05);
    expect(shown.points[0].share).toBeCloseTo(1000 / 5050);
});

// Pins spec/ui.md#ui.stats-model-metrics
test("the legend gives each algorithm its UM+ and its slope", () => {
    const view = umPlusView(pair(), true)!;

    expect(view.labelA).toBe(
        tr.statisticsUmPlusLegend({
            algorithm: tr.deckConfigSchedulerChoiceFsrs(),
            um: "0.0345",
            slope: "0.35",
        }),
    );
    expect(view.labelB).toBe(
        tr.statisticsUmPlusLegend({
            algorithm: tr.deckConfigSchedulerChoiceRwkvInstant(),
            um: "0.0051",
            slope: "-0.02",
        }),
    );
    // the two algorithms never share a colour
    expect(view.colourA).not.toBe(view.colourB);
});

// Pins spec/ui.md#ui.stats-model-metrics
test("the graph draws a line and a bubble per group for each algorithm", () => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");

    renderUmPlus(svg, umPlusBounds(), umPlusView(pair(), true));

    expect(svg.querySelectorAll(".um-plus-zero")).toHaveLength(1);
    expect(svg.querySelectorAll("path.um-plus-a")).toHaveLength(1);
    expect(svg.querySelectorAll("path.um-plus-b")).toHaveLength(1);
    expect(svg.querySelectorAll(".um-plus-a-bubble")).toHaveLength(3);
    expect(svg.querySelectorAll(".um-plus-b-bubble")).toHaveLength(3);
});

test("the UM+ X axis names the difference in predicted retrievability", () => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    renderUmPlus(svg, umPlusBounds(), null);
    const texts = Array.from(svg.querySelectorAll("text")).map((t) => t.textContent);
    expect(texts).toContain(tr.statisticsUmPlusDifference());
});
