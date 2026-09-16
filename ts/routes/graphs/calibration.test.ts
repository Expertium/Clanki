// @vitest-environment jsdom
// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
import {
    CalibrationBin,
    ReviewMetricsProgress,
    ReviewMetricsProgress_State as JobState,
    ReviewMetricsProgress_Unavailable as Unavailable,
} from "@generated/anki/stats_pb";
import { expect, test } from "vitest";

import {
    axisTenths,
    binPoints,
    COUNT_BAR_COLOUR,
    calibrationBounds,
    calibrationSeries,
    chooserOptions,
    chosenAlgorithm,
    renderCalibration,
    tiles,
} from "./calibration";

function bin(index: number, count: number, predicted: number, remembered: number) {
    return new CalibrationBin({
        index,
        count,
        sumPredicted: predicted * count,
        sumRemembered: remembered * count,
        low: Math.max(0, remembered - 0.05),
        high: Math.min(1, remembered + 0.05),
    });
}

function series(algorithm: SchedulingAlgorithm, withBins: boolean) {
    return {
        algorithm,
        unavailable: withBins ? Unavailable.AVAILABLE : Unavailable.UNSUPPORTED,
        reviews: withBins ? 400 : 0,
        falsePositiveRate: withBins ? [0, 1] : [],
        truePositiveRate: withBins ? [0, 1] : [],
        auc: withBins ? 0.7 : 0,
        sampleRole: withBins ? "final_fit" : "",
        bins: withBins ? [bin(4, 100, 0.55, 0.5), bin(19, 300, 0.95, 0.9)] : [],
        averagePredicted: withBins ? 0.85 : 0,
        actualRecall: withBins ? 0.8 : 0,
    };
}

function progress(): ReviewMetricsProgress {
    return new ReviewMetricsProgress({
        state: JobState.DONE,
        series: [
            series(SchedulingAlgorithm.FSRS7, true),
            series(SchedulingAlgorithm.RWKV_CURVE, false),
            series(SchedulingAlgorithm.RWKV_INSTANT, true),
        ],
        scored: 400,
    });
}

// Pins spec/ui.md#ui.stats-model-metrics
test("the menu offers every algorithm and allows only the ones with data", () => {
    const options = chooserOptions(progress());

    expect(options.map((option) => option.algorithm)).toEqual([
        SchedulingAlgorithm.FSRS7,
        SchedulingAlgorithm.RWKV_CURVE,
        SchedulingAlgorithm.RWKV_INSTANT,
    ]);
    expect(options.map((option) => option.available)).toEqual([true, false, true]);
});

// Pins spec/ui.md#ui.stats-model-metrics
test("the chosen algorithm is kept, and one without data is never drawn", () => {
    const data = progress();

    // the choice is kept while it can be drawn
    expect(chosenAlgorithm(data, SchedulingAlgorithm.RWKV_INSTANT)).toBe(
        SchedulingAlgorithm.RWKV_INSTANT,
    );
    // an algorithm without data falls to the first that has some, and the
    // menu then shows that one, so nothing is replaced silently
    expect(chosenAlgorithm(data, SchedulingAlgorithm.RWKV_CURVE)).toBe(
        SchedulingAlgorithm.FSRS7,
    );
    expect(chosenAlgorithm(data, null)).toBe(SchedulingAlgorithm.FSRS7);
    expect(chosenAlgorithm(null, SchedulingAlgorithm.FSRS7)).toBeNull();
});

// Pins spec/ui.md#ui.stats-model-metrics
test("a bin's point is its mean prediction against its share remembered", () => {
    const points = binPoints([bin(4, 100, 0.55, 0.5), bin(19, 300, 0.95, 0.9)]);

    expect(points).toHaveLength(2);
    expect(points[0].predicted).toBeCloseTo(0.55);
    expect(points[0].actual).toBeCloseTo(0.5);
    expect(points[1].count).toBe(300);
    expect(points[1].low).toBeCloseTo(0.85);
    expect(points[1].high).toBeCloseTo(0.95);
});

// Pins spec/ui.md#ui.stats-model-metrics
test("the tiles give the average predicted, the actual recall and the count", () => {
    const drawn = calibrationSeries(progress(), SchedulingAlgorithm.FSRS7);

    expect(drawn!.reviews).toBe(400);
    expect(tiles(drawn).map((tile) => tile.value)).toEqual(["85%", "80%", "400"]);
    expect(tiles(null)).toEqual([]);
});

// Pins spec/ui.md#ui.stats-model-metrics
test("the drawing area is square and holds the diagonal, the bars and the bars' axis", () => {
    const bounds = calibrationBounds();
    expect(bounds.width - bounds.marginLeft - bounds.marginRight).toBe(
        bounds.height - bounds.marginTop - bounds.marginBottom,
    );

    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    renderCalibration(
        svg,
        bounds,
        calibrationSeries(progress(), SchedulingAlgorithm.FSRS7),
    );

    // the dashed diagonal of a perfect algorithm, and the line through
    // the points
    const diagonal = svg.querySelector(".calibration-diagonal")!;
    expect(diagonal.getAttribute("stroke-dasharray")).toBe("6 4");
    expect(svg.querySelectorAll(".calibration-line")).toHaveLength(1);
    // one bar per bin, one point per bin, one interval per bin
    expect(svg.querySelectorAll(".calibration-counts rect")).toHaveLength(2);
    expect(svg.querySelectorAll(".calibration-drawing circle")).toHaveLength(2);
    expect(svg.querySelectorAll(".calibration-interval")).toHaveLength(2);
});

// Pins spec/ui.md#ui.stats-model-metrics
test("both axes step by 0.1, and the count bars are blue", () => {
    expect(axisTenths).toHaveLength(11);
    expect(axisTenths[1] - axisTenths[0]).toBeCloseTo(0.1);
    expect(axisTenths[axisTenths.length - 1]).toBe(1);
    // the reference diagonal stays grey; only the bars changed
    expect(COUNT_BAR_COLOUR).toBe("#6ba3d6");
});
