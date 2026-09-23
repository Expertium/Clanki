// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";
import { plainRecallWording, RecallWording } from "@tslib/recall-wording";
import { expect, test, vi } from "vitest";

import type { DataPoint } from "./forgetting-curve";
import {
    chartRevlog,
    CurveAlgorithm,
    curveInputs,
    forgettingCurveMessage,
    forgettingCurveTooltip,
    offersCurveToggle,
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

// The RULE: no FSRS-7 value reaches an RWKV-Curve card's chart, and the
// sharpest way to say that is that FSRS-7's parameters change nothing, with
// or without RWKV's curves after the earlier reviews.
test("no FSRS-7 value reaches an RWKV-Curve card's chart", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-16T00:00:00Z"));
    try {
        const past = [{ reviewTime: twoReviews()[1].time, recall: [1, 0.8, 0.3], s90: 4 }];
        // params 23 to 33 are the ones FSRS-7's own curve reads
        const otherParams = fsrs7Params().map((value, index) => (index >= 23 ? value * 0.5 : value));
        for (const extra of [{}, { past }]) {
            const rwkvCurve = { elapsedDays: [0, 10, 100], recall: [1, 0.5, 0.1], s90: 2, ...extra };
            const drawn = prepareData(
                chartRevlog(twoReviews(), rwkvCurve),
                30,
                fsrs7Params(),
                rwkvCurve,
            );
            const drawnWithOtherParams = prepareData(
                chartRevlog(twoReviews(), rwkvCurve),
                30,
                otherParams,
                rwkvCurve,
            );
            expect(drawn.length).toBeGreaterThan(0);
            expect(drawnWithOtherParams).toEqual(drawn);
            // every S90 on the chart is one of RWKV's own
            expect(drawn.every((point) => [2, 4].includes(point.stabilityS90))).toBe(true);
        }

        // and the two parameter sets really do draw different charts for an
        // FSRS-7 card, so this test can fail
        const fsrs = prepareData(chartRevlog(twoReviews()), 30, fsrs7Params());
        const fsrsWithOtherParams = prepareData(chartRevlog(twoReviews()), 30, otherParams);
        expect(fsrsWithOtherParams).not.toEqual(fsrs);
        expect(chartRevlog(twoReviews())).toHaveLength(2);
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
        const data = prepareData(revlog, 30, fsrs7Params(), rwkvCurve);

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
        const data = prepareData(chartRevlog(reviews as any, rwkvCurve), 30, fsrs7Params(), rwkvCurve);
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
// here the point is that the plain wording takes the plain string and the
// technical wording the technical one.
test("the plain forgetting-curve tooltip does not say retrievability", () => {
    expect(recallLabel(true)).toBe(tr.cardStatsFsrsRetrievabilityPlain());
    expect(recallLabel(true)).not.toBe(tr.cardStatsFsrsRetrievability());

    const tooltip = forgettingCurveTooltip(tooltipPoint(), 30, true);
    expect(tooltip).not.toContain(`${tr.cardStatsFsrsRetrievability()}: 82.88%`);
    expect(tooltip).toContain(`${tr.cardStatsFsrsRetrievabilityPlain()}: 82.88%`);
});

test("the technical forgetting-curve tooltip keeps retrievability", () => {
    expect(recallLabel(false)).toBe(tr.cardStatsFsrsRetrievability());

    const tooltip = forgettingCurveTooltip(tooltipPoint(), 30, false);
    expect(tooltip).toContain(`${tr.cardStatsFsrsRetrievability()}: 82.88%`);
});

// Pins spec/ui.md#ui.simple-recall-wording: the setting and the mode together
// choose the wording, in every layer.
test("the wording setting and the mode together choose the curve's label", () => {
    const cases: [RecallWording, boolean, string][] = [
        [RecallWording.BY_MODE, false, tr.cardStatsFsrsRetrievabilityPlain()],
        [RecallWording.BY_MODE, true, tr.cardStatsFsrsRetrievability()],
        [RecallWording.TECHNICAL, false, tr.cardStatsFsrsRetrievability()],
        [RecallWording.TECHNICAL, true, tr.cardStatsFsrsRetrievability()],
        [RecallWording.PLAIN, false, tr.cardStatsFsrsRetrievabilityPlain()],
        [RecallWording.PLAIN, true, tr.cardStatsFsrsRetrievabilityPlain()],
    ];
    for (const [setting, advanced, expected] of cases) {
        expect(recallLabel(plainRecallWording(setting, advanced))).toBe(expected);
    }
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
