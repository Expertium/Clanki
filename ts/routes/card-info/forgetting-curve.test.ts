// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";
import { expect, test, vi } from "vitest";

import type { DataPoint } from "./forgetting-curve";
import {
    chartCurve,
    chartRevlog,
    CurveAlgorithm,
    curveInputs,
    forgettingCurveMessage,
    forgettingCurveTooltip,
    fsrs7CurvePoints,
    offersCurveToggle,
    prepareData,
    rwkvRecallAt,
} from "./forgetting-curve";

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
            reviewKind: 1,
            buttonChosen: 3,
            ease: 2500,
            memoryState: { stability: 30, stabilityInternal: 20, difficulty: 5 },
        },
        {
            time: Date.parse("2024-01-01T00:00:00Z") / 1000,
            reviewKind: 0,
            buttonChosen: 3,
            ease: 2500,
            memoryState: { stability: 12, stabilityInternal: 10, difficulty: 5 },
        },
    ];
}

/** A reset (Forget): a manual entry with ease 0 and no answer after it. */
function resetThenNothing(): any {
    return [
        { time: Date.parse("2024-01-12T00:00:00Z") / 1000, reviewKind: 4, buttonChosen: 0, ease: 0 },
        ...twoReviews(),
    ];
}

/** FSRS-7's curves as the backend sends them, for twoReviews(): after the
 * last review, then after the first one. */
function fsrs7Curves(latest: number[], first: number[]): any {
    return {
        elapsedDays: [0, 10, 100],
        segments: [
            { reviewTime: BigInt(twoReviews()[0].time), recall: latest },
            { reviewTime: BigInt(twoReviews()[1].time), recall: first },
        ],
    };
}

// Pins spec/scheduling.md#sched.fsrs-rs-latest: card info draws the FSRS-7
// curves fsrs-rs computed in the backend, and keeps no copy of the curve.
test("an FSRS-7 chart draws the backend's curve after each review, with its S90", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-16T00:00:00Z"));
    try {
        const revlog = chartRevlog(twoReviews());
        const curve = fsrs7CurvePoints(revlog, fsrs7Curves([1, 0.5, 0.1], [1, 0.8, 0.3]))!;
        expect(chartCurve(revlog, undefined, fsrs7Curves([1, 0.5, 0.1], [1, 0.8, 0.3]))).toEqual(curve);
        const data = prepareData(revlog, 30, curve);

        // five days after the first review: halfway to 10 days on its curve
        const target = Date.parse("2024-01-06T00:00:00Z");
        const fiveDays = data.reduce((best, point) =>
            Math.abs(point.date.getTime() - target) < Math.abs(best.date.getTime() - target) ? point : best
        );
        expect(fiveDays.retrievability).toBeCloseTo(90, 0);
        expect(fiveDays.stabilityS90).toBe(12);
        // now, five days after the last review, on the last review's curve
        const now = data.find((point) => point.date.getTime() === Date.parse("2024-01-16T00:00:00Z"));
        expect(now?.retrievability).toBeCloseTo(75, 3);
        expect(now?.stabilityS90).toBe(30);
    } finally {
        vi.useRealTimers();
    }
});

test("without the backend's FSRS-7 curve the chart has nothing to draw", () => {
    const revlog = chartRevlog(twoReviews());
    expect(fsrs7CurvePoints(revlog, undefined)).toBeUndefined();
    expect(fsrs7CurvePoints(revlog, { elapsedDays: [], segments: [] })).toBeUndefined();
    // no curve for the last review
    const onlyFirst = fsrs7Curves([1, 0.5, 0.1], [1, 0.8, 0.3]);
    onlyFirst.segments.shift();
    expect(fsrs7CurvePoints(revlog, onlyFirst)).toBeUndefined();
});

// The RULE: no FSRS-7 value reaches an RWKV-Curve card's chart, and the
// sharpest way to say that is that FSRS-7's curves change nothing, with or
// without RWKV's curves after the earlier reviews.
test("no FSRS-7 value reaches an RWKV-Curve card's chart", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-16T00:00:00Z"));
    try {
        const past = [{ reviewTime: twoReviews()[1].time, recall: [1, 0.8, 0.3], s90: 4 }];
        const curves = fsrs7Curves([1, 0.9, 0.7], [1, 0.95, 0.8]);
        const otherCurves = fsrs7Curves([1, 0.6, 0.2], [1, 0.7, 0.4]);
        for (const extra of [{}, { past }]) {
            const rwkvCurve = { elapsedDays: [0, 10, 100], recall: [1, 0.5, 0.1], s90: 2, ...extra };
            const revlog = chartRevlog(twoReviews(), rwkvCurve);
            expect(chartCurve(revlog, rwkvCurve, curves)).toBe(rwkvCurve);
            const drawn = prepareData(revlog, 30, chartCurve(revlog, rwkvCurve, curves)!);
            const drawnWithOtherCurves = prepareData(revlog, 30, chartCurve(revlog, rwkvCurve, otherCurves)!);
            expect(drawn.length).toBeGreaterThan(0);
            expect(drawnWithOtherCurves).toEqual(drawn);
            // every S90 on the chart is one of RWKV's own
            expect(drawn.every((point) => [2, 4].includes(point.stabilityS90))).toBe(true);
        }

        // and the two sets of FSRS-7 curves really do draw different charts
        // for an FSRS-7 card, so this test can fail
        const revlog = chartRevlog(twoReviews());
        const fsrs = prepareData(revlog, 30, chartCurve(revlog, undefined, curves)!);
        const fsrsWithOtherCurves = prepareData(revlog, 30, chartCurve(revlog, undefined, otherCurves)!);
        expect(fsrsWithOtherCurves).not.toEqual(fsrs);
        expect(revlog).toHaveLength(2);
    } finally {
        vi.useRealTimers();
    }
});

test("an RWKV-Curve card draws RWKV's curve after every review that has one", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-16T00:00:00Z"));
    try {
        const rwkvCurve = {
            elapsedDays: [0, 10, 100],
            recall: [1, 0.5, 0.1],
            s90: 2,
            past: [{ reviewTime: BigInt(twoReviews()[1].time), recall: [1, 0.8, 0.3], s90: 4 }],
        };
        const revlog = chartRevlog(twoReviews(), rwkvCurve);
        expect(revlog.map((entry) => entry.time)).toEqual(twoReviews().map((entry: any) => entry.time));
        const data = prepareData(revlog, 30, rwkvCurve);

        // the chart starts at the first review, on that review's own curve
        expect(data[0].date.getTime()).toBe(Date.parse("2024-01-01T00:00:00Z"));
        const target = Date.parse("2024-01-06T00:00:00Z");
        const fiveDays = data.reduce((best, point) =>
            Math.abs(point.date.getTime() - target) < Math.abs(best.date.getTime() - target) ? point : best
        );
        expect(fiveDays.retrievability).toBeCloseTo(90, 0);
        expect(fiveDays.stabilityS90).toBe(4);
        // after the last review, the curve after it
        const now = data.find((point) => point.date.getTime() === Date.parse("2024-01-16T00:00:00Z"));
        expect(now?.retrievability).toBeCloseTo(75, 3);
        expect(now?.stabilityS90).toBe(2);
        expect(data.some((point) => point.gap)).toBe(false);
    } finally {
        vi.useRealTimers();
    }
});

test("a review without a stored RWKV curve gets no segment, and none is invented", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-26T00:00:00Z"));
    try {
        const reviews = [
            {
                time: Date.parse("2024-01-21T00:00:00Z") / 1000,
                reviewKind: 1,
                buttonChosen: 3,
                ease: 2500,
            },
            ...twoReviews(),
        ];
        const oldest = { reviewTime: twoReviews()[1].time, recall: [1, 0.8, 0.3], s90: 4 };
        const rwkvCurve = { elapsedDays: [0, 10, 100], recall: [1, 0.5, 0.1], s90: 2, past: [oldest] };
        // the middle review has no curve: its segment is a break in the line
        const data = prepareData(chartRevlog(reviews as any, rwkvCurve), 30, rwkvCurve);
        const middle = Date.parse("2024-01-11T00:00:00Z");
        const next = Date.parse("2024-01-21T00:00:00Z");
        const inside = data.filter((point) => point.date.getTime() > middle && point.date.getTime() < next);
        expect(inside).toHaveLength(0);
        expect(data.filter((point) => point.gap)).toHaveLength(1);

        // reviews older than the oldest stored curve are not charted at all
        const onlyMiddle = { ...rwkvCurve, past: [{ ...oldest, reviewTime: middle / 1000 }] };
        expect(chartRevlog(reviews as any, onlyMiddle).map((entry) => entry.time)).toEqual([
            next / 1000,
            middle / 1000,
        ]);
        // with no stored curve at all, only the last review is charted
        expect(chartRevlog(reviews as any, { ...rwkvCurve, past: [] })).toHaveLength(1);
    } finally {
        vi.useRealTimers();
    }
});

// Pins spec/ui.md#ui.card-info-curve-messages

test("a reset card still draws RWKV-Curve's curve from its last answer", () => {
    const rwkvCurve = { elapsedDays: [0, 10], recall: [1, 0.5], s90: 2 };
    // FSRS-7 has no memory state after a reset, so its chart is empty...
    expect(chartRevlog(resetThenNothing())).toHaveLength(0);
    // ...but RWKV's stored curve needs none, so the chart draws.
    expect(chartRevlog(resetThenNothing(), rwkvCurve).map((entry) => entry.time)).toEqual([
        twoReviews()[0].time,
    ]);
});

test("the forgetting curve says why it has no curve, and never says NO DATA", () => {
    const rwkvCurve = { elapsedDays: [0, 10], recall: [1, 0.5], s90: 2 };
    // a curve draws: no message
    expect(forgettingCurveMessage(twoReviews())).toBeUndefined();
    expect(forgettingCurveMessage(twoReviews(), rwkvCurve)).toBeUndefined();
    // RWKV is not ready yet
    expect(forgettingCurveMessage(twoReviews(), { elapsedDays: [], recall: [], pending: true }))
        .toBe(tr.cardStatsCalculating());
    // RWKV is ready but has no curve for the card
    expect(forgettingCurveMessage(twoReviews(), { elapsedDays: [], recall: [] }))
        .toBe(tr.cardStatsForgettingCurveNoRwkvCurve());
    // the card has never been answered
    expect(forgettingCurveMessage([])).toBe(tr.cardStatsForgettingCurveNoAnswerYet());
    // FSRS-7 after a reset
    expect(forgettingCurveMessage(resetThenNothing()))
        .toBe(tr.cardStatsForgettingCurveCardWasReset());
});

test("after the last review an RWKV-Curve card follows RWKV's curve and S90", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-16T00:00:00Z"));
    try {
        const rwkvCurve = { elapsedDays: [0, 10, 100], recall: [1, 0.5, 0.1], s90: 2 };
        const revlog = chartRevlog(twoReviews(), rwkvCurve);
        const data = prepareData(revlog, 30, rwkvCurve);

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
        const data = prepareData(twoReviews(), 30, { elapsedDays: [], recall: [] });
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

test("the forgetting-curve tooltip names retrievability", () => {
    const tooltip = forgettingCurveTooltip(tooltipPoint(), 30);
    expect(tooltip).toContain(`${tr.cardStatsFsrsRetrievability()}: 82.88%`);
});

// Pins spec/ui.md#ui.card-info-rwkv-curve (the FSRS-7 / RWKV-Curve toggle):
// one algorithm at a time, RWKV-Curve's unless FSRS-7 is chosen, and the
// toggle only where the backend sent FSRS-7's own reviews (Advanced mode).
test("the curve toggle draws one algorithm at a time", () => {
    const rwkv = { elapsedDays: [0, 1, 10], recall: [1, 0.9, 0.5], s90: 1 };
    const plain = twoReviews();
    const fsrs7 = twoReviews().map((entry: any) => ({
        ...entry,
        memoryState: { ...entry.memoryState, stability: entry.memoryState.stability + 1 },
    }));

    expect(offersCurveToggle(rwkv, fsrs7)).toBe(true);
    // Simple mode, or an FSRS-7 card: the backend sends no FSRS-7 reviews
    expect(offersCurveToggle(rwkv, [])).toBe(false);
    expect(offersCurveToggle(undefined, fsrs7)).toBe(false);

    const byDefault = curveInputs(plain, rwkv, fsrs7, CurveAlgorithm.RwkvCurve);
    expect(byDefault.revlog).toBe(plain);
    expect(byDefault.rwkvCurve).toBe(rwkv);

    const fsrs = curveInputs(plain, rwkv, fsrs7, CurveAlgorithm.Fsrs7);
    expect(fsrs.revlog).toBe(fsrs7);
    // no RWKV curve rides along with FSRS-7's reviews
    expect(fsrs.rwkvCurve).toBeUndefined();

    // no toggle: FSRS-7 cannot be chosen, the card keeps its own curves
    const without = curveInputs(plain, rwkv, [], CurveAlgorithm.Fsrs7);
    expect(without.revlog).toBe(plain);
    expect(without.rwkvCurve).toBe(rwkv);
});
