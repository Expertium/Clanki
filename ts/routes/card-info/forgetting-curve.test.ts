// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";
import { expect, test, vi } from "vitest";

import type { DataPoint } from "./forgetting-curve";
import {
    chartRevlog,
    forgettingCurveTooltip,
    prepareData,
    recallLabel,
    rwkvRecallAt,
    stabilityS90,
} from "./forgetting-curve";

function fsrs7Params(): number[] {
    return [
        0.1104,
        2.2395,
        3.9221,
        11.7841,
        6.1686,
        0.6457,
        3.6807,
        1.9795,
        0.0,
        1.3826,
        0.7024,
        0.5999,
        0.8146,
        0.6398,
        1.0,
        1.3207,
        0.6707,
        3.8668,
        0.4416,
        0.0934,
        1.8631,
        0.6162,
        1.0869,
        0.1567,
        0.0801,
        0.2421,
        0.9464,
        0.1433,
        0.7145,
        0.0,
        0.5667,
        0.3734,
        0.5333,
        0.3048,
    ];
}

test("stabilityS90 returns the stored stability without FSRS-7 params", () => {
    expect(stabilityS90(10, undefined)).toBe(10);
    // FSRS-6 (21 values) and the FSRS-7 preview (35 values) are not FSRS-7
    expect(stabilityS90(10, Array(21).fill(1))).toBe(10);
    expect(stabilityS90(10, Array(35).fill(1))).toBe(10);
});

test("stabilityS90 derives S90 from FSRS-7 curve params", () => {
    expect(stabilityS90(10, fsrs7Params())).toBeCloseTo(12.8789, 3);
});

test("stabilityS90 uses both FSRS-7 stabilities and difficulty", () => {
    const scalar = stabilityS90(10, fsrs7Params());
    const fullState = stabilityS90(10, fsrs7Params(), 5, 8);

    expect(fullState).not.toBeCloseTo(scalar, 3);
});

test("prepareData carries S90 for the forgetting curve tooltip", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-03T00:00:00Z"));

    try {
        const data = prepareData(
            [
                {
                    time: Date.parse("2024-01-01T00:00:00Z") / 1000,
                    memoryState: {
                        stability: 12.87887574872002,
                        stabilityInternal: 10,
                        difficulty: 5,
                    },
                },
            ] as any,
            2,
            fsrs7Params(),
        );

        expect(data.at(-1)?.stability).toBe(10);
        expect(data.at(-1)?.stabilityS90).toBeCloseTo(12.8789, 3);
    } finally {
        vi.useRealTimers();
    }
});

test("prepareData computes FSRS-7 retrievability from the full memory state", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-21T00:00:00Z"));

    try {
        const data = prepareData(
            [
                {
                    time: Date.parse("2024-01-01T00:00:00Z") / 1000,
                    memoryState: {
                        stability: 10,
                        stabilityInternal: 10,
                        stabilityFast: 5,
                        difficulty: 8,
                    },
                },
            ] as any,
            20,
            fsrs7Params(),
        );

        expect(data.at(-1)?.retrievability).toBeCloseTo(82.88255, 4);
    } finally {
        vi.useRealTimers();
    }
});

// Pins spec/ui.md#ui.card-info-rwkv-curve

test("rwkvRecallAt interpolates between the curve's points", () => {
    const curve = { elapsedDays: [0, 10, 20], recall: [1, 0.5, 0.25] };
    expect(rwkvRecallAt(curve, 5)).toBeCloseTo(0.75, 6);
    expect(rwkvRecallAt(curve, 15)).toBeCloseTo(0.375, 6);
    expect(rwkvRecallAt(curve, 0)).toBe(1);
    expect(rwkvRecallAt(curve, 30)).toBe(0.25);
});

function twoReviews(): any {
    // newest first, as card info sends them
    return [
        {
            time: Date.parse("2024-01-11T00:00:00Z") / 1000,
            memoryState: { stability: 30, stabilityInternal: 20, difficulty: 5 },
        },
        {
            time: Date.parse("2024-01-01T00:00:00Z") / 1000,
            memoryState: { stability: 12, stabilityInternal: 10, difficulty: 5 },
        },
    ];
}

test("an RWKV-Curve card's chart starts at its last review: no FSRS-7 segments", () => {
    const rwkvCurve = { elapsedDays: [0, 10], recall: [1, 0.5], s90: 2 };
    expect(chartRevlog(twoReviews(), rwkvCurve).map((entry) => entry.time)).toEqual([
        twoReviews()[0].time,
    ]);
    expect(chartRevlog(twoReviews())).toHaveLength(2);
});

test("after the last review an RWKV-Curve card follows RWKV's curve and S90", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-16T00:00:00Z"));
    try {
        const rwkvCurve = { elapsedDays: [0, 10, 100], recall: [1, 0.5, 0.1], s90: 2 };
        const revlog = chartRevlog(twoReviews(), rwkvCurve);
        const data = prepareData(revlog, 30, fsrs7Params(), rwkvCurve);

        // five days after the last review: halfway to 10 days on RWKV's curve
        const now = data.find((point) => point.date.getTime() === Date.parse("2024-01-16T00:00:00Z"));
        expect(now?.retrievability).toBeCloseTo(75, 3);
        expect(data.every((point) => point.stabilityS90 === 2)).toBe(true);
        expect(data[0].date.getTime()).toBe(Date.parse("2024-01-11T00:00:00Z"));
    } finally {
        vi.useRealTimers();
    }
});

test("without an RWKV curve yet the chart stops at the last review", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-16T00:00:00Z"));
    try {
        const data = prepareData(twoReviews(), 30, fsrs7Params(), { elapsedDays: [], recall: [] });
        expect(data.at(-1)?.date.getTime()).toBe(Date.parse("2024-01-11T00:00:00Z"));
    } finally {
        vi.useRealTimers();
    }
});

function tooltipPoint(): DataPoint {
    return {
        date: new Date("2024-01-16T00:00:00Z"),
        daysSinceFirstLearn: 10,
        elapsedDaysSinceLastReview: 5,
        retrievability: 82.88,
        stability: 20,
        stabilityS90: 20,
    };
}

// spec/ui.md, ui.simple-recall-wording. The English wording of the two strings
// is pinned in Rust (simple_mode_names_the_retrievability_column_in_plain_words);
// here the point is that Simple mode takes the plain string and Advanced mode
// the technical one.
test("Simple mode's forgetting-curve tooltip does not say retrievability", () => {
    expect(recallLabel(false)).toBe(tr.cardStatsRecallProbability());
    expect(recallLabel(false)).not.toBe(tr.cardStatsFsrsRetrievability());

    const tooltip = forgettingCurveTooltip(tooltipPoint(), 30, false);
    expect(tooltip).not.toContain(tr.cardStatsFsrsRetrievability());
    expect(tooltip).toContain(`${tr.cardStatsRecallProbability()}: 82.88%`);
});

test("Advanced mode's forgetting-curve tooltip keeps retrievability", () => {
    expect(recallLabel(true)).toBe(tr.cardStatsFsrsRetrievability());

    const tooltip = forgettingCurveTooltip(tooltipPoint(), 30, true);
    expect(tooltip).toContain(`${tr.cardStatsFsrsRetrievability()}: 82.88%`);
});