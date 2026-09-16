// @vitest-environment jsdom
// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
import {
    ReviewMetricsProgress,
    ReviewMetricsProgress_State as JobState,
    ReviewMetricsProgress_Unavailable as Unavailable,
} from "@generated/anki/stats_pb";
import * as tr from "@generated/ftl";
import { expect, test } from "vitest";

import {
    axisTenths,
    chanceLabel,
    curveLabel,
    dataNotes,
    overlayText,
    renderRoc,
    rocBounds,
    rocCurves,
    stillComputing,
    unavailableNotes,
} from "./roc";

function curve(algorithm: SchedulingAlgorithm, auc: number, sampleRole = "validation_fold") {
    return {
        algorithm,
        unavailable: Unavailable.AVAILABLE,
        reviews: 100,
        falsePositiveRate: [0, 0, 1],
        truePositiveRate: [0, 1, 1],
        auc,
        sampleRole,
    };
}

function missing(algorithm: SchedulingAlgorithm, unavailable: Unavailable) {
    return {
        algorithm,
        unavailable,
        reviews: 0,
        falsePositiveRate: [],
        truePositiveRate: [],
        auc: 0,
        sampleRole: "",
    };
}

// Pins spec/ui.md#ui.stats-model-metrics
test("every algorithm with data gets its own curve", () => {
    const progress = new ReviewMetricsProgress({
        state: JobState.DONE,
        series: [
            curve(SchedulingAlgorithm.FSRS7, 0.723),
            curve(SchedulingAlgorithm.RWKV_CURVE, 0.75),
            curve(SchedulingAlgorithm.RWKV_INSTANT, 0.76),
        ],
    });

    const curves = rocCurves(progress);

    expect(curves.map((c) => c.algorithm)).toEqual([
        SchedulingAlgorithm.FSRS7,
        SchedulingAlgorithm.RWKV_CURVE,
        SchedulingAlgorithm.RWKV_INSTANT,
    ]);
    // each curve has its own colour, so no two series can be confused
    expect(new Set(curves.map((c) => c.colour)).size).toBe(3);
    expect(curves[0].points).toEqual([[0, 0], [0, 1], [1, 1]]);
    expect(unavailableNotes(progress)).toEqual([]);
    expect(overlayText(progress)).toBeUndefined();
});

// Pins spec/ui.md#ui.stats-model-metrics
test("an algorithm without data is absent and says why", () => {
    const progress = new ReviewMetricsProgress({
        state: JobState.DONE,
        series: [
            curve(SchedulingAlgorithm.FSRS7, 0.7),
            missing(SchedulingAlgorithm.RWKV_CURVE, Unavailable.UNSUPPORTED),
            missing(SchedulingAlgorithm.RWKV_INSTANT, Unavailable.NO_MODEL),
        ],
    });

    expect(rocCurves(progress).map((c) => c.algorithm)).toEqual([
        SchedulingAlgorithm.FSRS7,
    ]);
    const notes = unavailableNotes(progress);
    expect(notes).toHaveLength(2);
    // each note names its own algorithm and its own reason, never another
    // algorithm's values
    expect(notes[0]).toBe(
        tr.statisticsModelMetricsUnsupported({
            algorithm: tr.deckConfigSchedulerChoiceRwkvCurve(),
        }),
    );
    expect(notes[1]).toBe(
        tr.statisticsModelMetricsNoModel({
            algorithm: tr.deckConfigSchedulerChoiceRwkvInstant(),
        }),
    );
});

// Pins spec/ui.md#ui.stats-model-metrics
test("the legend names the algorithm and its AUC to four decimals", () => {
    expect(curveLabel(SchedulingAlgorithm.FSRS7, 0.723)).toBe(
        tr.statisticsRocLegend({
            algorithm: tr.deckConfigSchedulerChoiceFsrs(),
            auc: "0.7230",
        }),
    );
    expect(chanceLabel()).toBe(tr.statisticsRocChance({ auc: "0.5000" }));
});

// Pins spec/ui.md#ui.stats-model-metrics
test("the page says it is computing until a curve arrives", () => {
    const computing = new ReviewMetricsProgress({
        state: JobState.COMPUTING,
        series: [missing(SchedulingAlgorithm.FSRS7, Unavailable.NOT_READY)],
    });

    expect(stillComputing(computing)).toBe(true);
    expect(overlayText(computing)).toBe(tr.cardStatsCalculating());
    expect(overlayText(null)).toBe(tr.cardStatsCalculating());
    // a curve of its own ends the message, even while another is computing
    const partly = new ReviewMetricsProgress({
        state: JobState.COMPUTING,
        series: [
            curve(SchedulingAlgorithm.FSRS7, 0.7),
            missing(SchedulingAlgorithm.RWKV_CURVE, Unavailable.NOT_READY),
        ],
    });
    expect(overlayText(partly)).toBeUndefined();
});

// Pins spec/ui.md#ui.stats-model-metrics
test("the drawing area is square", () => {
    const bounds = rocBounds();

    expect(bounds.width - bounds.marginLeft - bounds.marginRight).toBe(
        bounds.height - bounds.marginTop - bounds.marginBottom,
    );
});

// Pins spec/ui.md#ui.stats-model-metrics
test("the diagonal of random chance is drawn dashed", () => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const progress = new ReviewMetricsProgress({
        state: JobState.DONE,
        series: [curve(SchedulingAlgorithm.FSRS7, 0.7)],
    });

    renderRoc(svg, rocBounds(), rocCurves(progress));

    // the axes draw their own paths, so look inside the curve group
    const paths = Array.from(svg.querySelectorAll(".roc-curve path"));
    expect(paths).toHaveLength(2);
    expect(paths[0].getAttribute("stroke-dasharray")).toBe("6 4");
    expect(paths[1].getAttribute("stroke-dasharray")).toBeNull();
});

// Pins spec/ui.md#ui.stats-model-metrics
test("the graph says what it scored, what it left out and how fresh it is", () => {
    const progress = new ReviewMetricsProgress({
        state: JobState.DONE,
        series: [
            curve(SchedulingAlgorithm.FSRS7, 0.7, "validation_fold"),
            missing(SchedulingAlgorithm.RWKV_CURVE, Unavailable.UNSUPPORTED),
            curve(SchedulingAlgorithm.RWKV_INSTANT, 0.8, "final_fit"),
        ],
        scored: 4000,
        fsrsOnly: 10,
        rwkvOnly: 20,
        unscored: 30,
        newestScoredSecs: 1700000000n,
        newerReviews: 5,
    });

    const notes = dataNotes(progress);

    // the shared count, what was left out, one role line per drawn
    // algorithm, and the staleness line
    expect(notes).toHaveLength(5);
    expect(notes[2]).toBe(
        tr.statisticsModelMetricsRole({
            algorithm: tr.deckConfigSchedulerChoiceFsrs(),
            role: "validation_fold",
        }),
    );
    expect(notes[3]).toBe(
        tr.statisticsModelMetricsRole({
            algorithm: tr.deckConfigSchedulerChoiceRwkvInstant(),
            role: "final_fit",
        }),
    );
    // nothing is said about an algorithm that has no curve
    expect(notes.join(" ")).not.toContain("undefined");
    // and a graph with nothing left out says nothing about it
    const clean = new ReviewMetricsProgress({
        state: JobState.DONE,
        series: [curve(SchedulingAlgorithm.FSRS7, 0.7)],
        scored: 10,
    });
    expect(dataNotes(clean)).toHaveLength(2);
});

// Pins spec/ui.md#ui.stats-model-metrics
test("both axes step by 0.1", () => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const progress = new ReviewMetricsProgress({
        state: JobState.DONE,
        series: [curve(SchedulingAlgorithm.FSRS7, 0.7)],
    });

    renderRoc(svg, rocBounds(), rocCurves(progress));

    expect(axisTenths).toHaveLength(11);
    // 11 ticks on each of the two axes
    expect(svg.querySelectorAll(".tick")).toHaveLength(22);
    // the decimal separator follows the locale, so match on either
    const labels = Array.from(svg.querySelectorAll(".tick text")).map((t) => t.textContent);
    expect(labels.filter((l) => /^0[.,]1$/.test(l ?? ""))).toHaveLength(2);
    expect(labels.filter((l) => /^0[.,]7$/.test(l ?? ""))).toHaveLength(2);
});
