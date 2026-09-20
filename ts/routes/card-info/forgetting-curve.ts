// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    type CardStatsResponse_StatsRevlogEntry as RevlogEntry,
    RevlogEntry_ReviewKind,
} from "@generated/anki/stats_pb";
import * as tr from "@generated/ftl";
import { timeSpan } from "@tslib/time";
import { axisBottom, axisLeft, line, max, min, pointer, scaleLinear, scaleTime, select } from "d3";
import { type GraphBounds, setDataAvailable } from "../graphs/graph-helpers";
import { hideTooltip, showTooltip } from "../graphs/tooltip-utils.svelte";

const MIN_POINTS = 1000;
const FSRS7_PARAM_COUNT = 34;
const S90_TARGET_RETRIEVABILITY = 0.9;
const S_MAX = 36_500;
const S90_SEARCH_STEPS = 32;

function forgettingCurveFsrs7(
    stability: number,
    stabilityFast: number,
    difficulty: number,
    daysElapsed: number,
    params: number[],
): number {
    const decay1Mag = Math.min(0.95, Math.max(0.01, params[23] * Math.pow(stabilityFast, params[33] - 0.3)));
    const decay1 = -decay1Mag;
    const decay2 = -Math.min(0.95, Math.max(0.01, params[24]));
    const factor1 = Math.exp(Math.min(60.0, Math.log(params[25]) / decay1)) - 1;
    const factor2 = Math.pow(params[26], 1 / decay2) - 1;
    const dTimescale = Math.exp((difficulty - 5.0) * (params[32] - 0.3));

    const r1 = Math.pow(1 + factor1 * (daysElapsed / stabilityFast), decay1);
    const r2 = Math.pow(1 + factor2 * dTimescale * (daysElapsed / stability), decay2);

    const weight1 = params[27] * Math.pow(stabilityFast, -params[29]);
    const weight2 = params[28]
        * Math.pow(stability, params[30])
        * Math.exp((difficulty - 5.0) * (params[31] - 0.5));
    const retrievability = (weight1 * r1 + weight2 * r2) / (weight1 + weight2);
    return Math.min(0.9999, Math.max(0.0001, retrievability * (1.0 - 2e-5) + 1e-5));
}

// FSRS-7 is the only model (spec sched.fsrs7-only); the backend always sends
// its 34 parameters with a card's memory state.
function forgettingCurve(
    stability: number,
    stabilityFast: number,
    difficulty: number,
    daysElapsed: number,
    params: number[],
): number {
    return forgettingCurveFsrs7(stability, stabilityFast, difficulty, daysElapsed, params);
}

export function stabilityS90(
    stability: number,
    params: number[] | undefined,
    stabilityFast = stability,
    difficulty = 5.0,
): number {
    if (!params || params.length !== FSRS7_PARAM_COUNT) {
        return stability;
    }

    let low = 0;
    let high = Math.max(stability, 1);
    while (
        forgettingCurve(stability, stabilityFast, difficulty, high, params)
            > S90_TARGET_RETRIEVABILITY
        && high < S_MAX
    ) {
        high = Math.min(high * 2, S_MAX);
    }

    for (let i = 0; i < S90_SEARCH_STEPS; i++) {
        const mid = (low + high) / 2;
        if (
            forgettingCurve(stability, stabilityFast, difficulty, mid, params)
                > S90_TARGET_RETRIEVABILITY
        ) {
            low = mid;
        } else {
            high = mid;
        }
    }

    return (low + high) / 2;
}

/**
 * RWKV-Curve's curve after one earlier answered review of the card (spec
 * ui.card-info-rwkv-curve): the review's time in seconds, as the revlog entry
 * has it, and recall on the same elapsed days as the curve after the last
 * review.
 */
export interface RwkvPastSegment {
    reviewTime: bigint | number;
    recall: number[];
    s90: number;
}

/**
 * RWKV-Curve's forgetting curve after the card's last review, for a card whose
 * preset runs RWKV-Curve (spec ui.card-info-rwkv-curve): recall at each elapsed
 * day (ascending), and the curve's S90. Without points RWKV has no curve for
 * the card yet, and the chart stops at the last review. `past` holds the
 * curves after the earlier reviews that have one stored.
 */
export interface RwkvCurvePoints {
    elapsedDays: number[];
    recall: number[];
    s90?: number;
    /** True while RWKV is not ready; the curve arrives in a later request. */
    pending?: boolean;
    past?: RwkvPastSegment[];
}

/** The recall of `curve` at `days`, linear between its points. */
export function rwkvRecallAt(curve: RwkvCurvePoints, days: number): number {
    const xs = curve.elapsedDays;
    const high = xs.findIndex((x) => x >= days);
    if (high <= 0) {
        return curve.recall[high === 0 ? 0 : xs.length - 1];
    }
    const low = high - 1;
    const fraction = (days - xs[low]) / (xs[high] - xs[low]);
    return curve.recall[low] + (curve.recall[high] - curve.recall[low]) * fraction;
}

export interface DataPoint {
    date: Date;
    daysSinceFirstLearn: number;
    elapsedDaysSinceLastReview: number;
    retrievability: number;
    stability: number;
    stabilityS90: number;
    /** A break in the line: the review before it has no curve to draw. */
    gap?: boolean;
}

export enum TimeRange {
    Week,
    Month,
    Year,
    AllTime,
}

const MAX_DAYS = {
    [TimeRange.Week]: 7,
    [TimeRange.Month]: 30,
    [TimeRange.Year]: 365,
    [TimeRange.AllTime]: Infinity,
};

function filterDataByTimeRange(data: DataPoint[], maxDays: number): DataPoint[] {
    return data.filter((point) => point.daysSinceFirstLearn <= maxDays);
}

export function filterRevlogEntryByReviewKind(entry: RevlogEntry): boolean {
    return (
        entry.reviewKind !== RevlogEntry_ReviewKind.MANUAL
        && entry.reviewKind !== RevlogEntry_ReviewKind.RESCHEDULED
        && (entry.reviewKind !== RevlogEntry_ReviewKind.FILTERED || entry.ease !== 0)
    );
}

export function filterRevlog(revlog: RevlogEntry[]): RevlogEntry[] {
    const result: RevlogEntry[] = [];
    for (const entry of revlog) {
        if (
            (entry.reviewKind === RevlogEntry_ReviewKind.MANUAL && entry.ease === 0)
            || entry.memoryState === undefined
        ) {
            break;
        }
        result.push(entry);
    }

    return result.filter((entry) => filterRevlogEntryByReviewKind(entry));
}

/** The card's latest answered review, if it has one. */
export function latestAnsweredReview(revlog: RevlogEntry[]): RevlogEntry | undefined {
    return revlog.find((entry) => entry.buttonChosen > 0 && filterRevlogEntryByReviewKind(entry));
}

/** An RWKV-Curve card's stored curves after its earlier reviews, by review time. */
function rwkvPastSegments(rwkvCurve: RwkvCurvePoints): Map<number, RwkvPastSegment> {
    const segments = new Map<number, RwkvPastSegment>();
    for (const segment of rwkvCurve.past ?? []) {
        const time = Number(segment.reviewTime);
        if (!segments.has(time)) {
            segments.set(time, segment);
        }
    }
    return segments;
}

/**
 * The reviews the chart starts its segments at, newest first. For an FSRS-7
 * card, the reviews FSRS-7 has a memory state for. For an RWKV-Curve card, its
 * answered reviews from the oldest one that has an RWKV curve stored, and
 * always the last answered one: only RWKV's own curves are drawn, never
 * FSRS-7's (spec ui.card-info-rwkv-curve). RWKV's curve needs no FSRS-7 memory
 * state, so the RWKV-Curve chart also draws for a card that FSRS-7 has no
 * memory state for, a card that was reset for example (spec
 * ui.card-info-curve-messages).
 */
export function chartRevlog(revlog: RevlogEntry[], rwkvCurve?: RwkvCurvePoints): RevlogEntry[] {
    if (rwkvCurve) {
        const answered = revlog.filter(
            (entry) => entry.buttonChosen > 0 && filterRevlogEntryByReviewKind(entry),
        );
        const segments = rwkvPastSegments(rwkvCurve);
        // newest first, so the oldest review with a curve has the highest index
        let oldest = 0;
        answered.forEach((entry, index) => {
            if (segments.has(Number(entry.time))) {
                oldest = index;
            }
        });
        return answered.slice(0, oldest + 1);
    }
    return filterRevlog(revlog);
}

/**
 * The message the forgetting-curve box shows instead of a curve, or undefined
 * when it draws one. Each message says why there is no curve, in the
 * collection's own algorithm; none of them falls back to the other algorithm
 * (spec ui.card-info-curve-messages).
 */
export function forgettingCurveMessage(
    revlog: RevlogEntry[],
    rwkvCurve?: RwkvCurvePoints,
): string | undefined {
    if (rwkvCurve?.pending) {
        return tr.cardStatsCalculating();
    }
    const latest = latestAnsweredReview(revlog);
    if (latest === undefined) {
        return tr.cardStatsForgettingCurveNoAnswerYet();
    }
    if (rwkvCurve !== undefined && rwkvCurve.elapsedDays.length === 0) {
        return tr.cardStatsForgettingCurveNoRwkvCurve();
    }
    if (chartRevlog(revlog, rwkvCurve).length > 0) {
        return undefined;
    }
    const reset = revlog.find(
        (entry) => entry.reviewKind === RevlogEntry_ReviewKind.MANUAL && entry.ease === 0,
    );
    if (reset !== undefined && reset.time >= latest.time) {
        return tr.cardStatsForgettingCurveCardWasReset();
    }
    return tr.cardStatsForgettingCurveNotEnoughHistory();
}

/**
 * The points of an RWKV-Curve card's chart (spec ui.card-info-rwkv-curve):
 * after each review, RWKV's own curve after that review, up to the next
 * review; after the last one, up to now and then as a preview. A review with
 * no stored curve gets no segment, only a break in the line. No FSRS-7 value
 * is read here.
 */
function prepareRwkvData(
    revlog: RevlogEntry[],
    maxDays: number,
    rwkvCurve: RwkvCurvePoints,
): DataPoint[] {
    const reviews = revlog.slice().reverse();
    if (reviews.length === 0) {
        return [];
    }
    const segments = rwkvPastSegments(rwkvCurve);
    const step = Math.min(maxDays / MIN_POINTS, 1);
    const data: DataPoint[] = [];
    const push = (
        time: number,
        daysSinceFirstLearn: number,
        elapsed: number,
        retrievability: number,
        s90: number,
        gap = false,
    ) => {
        const point: DataPoint = {
            date: new Date(time * 1000),
            daysSinceFirstLearn,
            elapsedDaysSinceLastReview: elapsed,
            retrievability,
            stability: s90,
            stabilityS90: s90,
        };
        if (gap) {
            point.gap = true;
        }
        data.push(point);
    };

    const firstTime = Number(reviews[0].time);
    let s90 = segments.get(firstTime)?.s90 ?? rwkvCurve.s90 ?? 0;
    for (let index = 0; index < reviews.length; index++) {
        const reviewTime = Number(reviews[index].time);
        const sinceFirst = (reviewTime - firstTime) / 86400;
        const last = index === reviews.length - 1;
        // after the last review, the curve RWKV holds for the card now
        const lastCurve = rwkvCurve.elapsedDays.length > 0
            ? { recall: rwkvCurve.recall, s90: rwkvCurve.s90 ?? 0 }
            : undefined;
        const segment = last ? lastCurve : segments.get(reviewTime);
        if (segment) {
            s90 = segment.s90;
        }
        push(reviewTime, sinceFirst, 0, 100, s90);
        if (last) {
            if (!segment) {
                return filterDataByTimeRange(data, maxDays);
            }
            break;
        }
        const nextTime = Number(reviews[index + 1].time);
        const totalDaysElapsed = (nextTime - reviewTime) / 86400;
        if (!segment) {
            push(reviewTime, sinceFirst, 0, 100, s90, true);
            continue;
        }
        const curve = { elapsedDays: rwkvCurve.elapsedDays, recall: segment.recall };
        let elapsedDays = 0;
        while (elapsedDays < totalDaysElapsed - step) {
            elapsedDays += step;
            push(
                reviewTime + elapsedDays * 86400,
                sinceFirst + elapsedDays,
                elapsedDays,
                rwkvRecallAt(curve, elapsedDays) * 100,
                s90,
            );
        }
    }

    // after the last review, RWKV's curve after it: to now, then a preview
    const lastReviewTime = Number(reviews[reviews.length - 1].time);
    const sinceFirst = (lastReviewTime - firstTime) / 86400;
    const now = Date.now() / 1000;
    const totalDaysSinceLastReview = (now - lastReviewTime) / 86400;
    let elapsedDays = 0;
    while (elapsedDays < totalDaysSinceLastReview - step) {
        elapsedDays += step;
        push(
            lastReviewTime + elapsedDays * 86400,
            sinceFirst + elapsedDays,
            elapsedDays,
            rwkvRecallAt(rwkvCurve, elapsedDays) * 100,
            s90,
        );
    }
    push(
        now,
        sinceFirst + totalDaysSinceLastReview,
        totalDaysSinceLastReview,
        rwkvRecallAt(rwkvCurve, totalDaysSinceLastReview) * 100,
        s90,
    );
    const previewDays = maxDays - totalDaysSinceLastReview;
    let previewDaysElapsed = 0;
    while (previewDaysElapsed < previewDays) {
        previewDaysElapsed += step;
        push(
            now + previewDaysElapsed * 86400,
            sinceFirst + totalDaysSinceLastReview + previewDaysElapsed,
            totalDaysSinceLastReview + previewDaysElapsed,
            rwkvRecallAt(rwkvCurve, elapsedDays + previewDaysElapsed) * 100,
            s90,
        );
    }
    return filterDataByTimeRange(data, maxDays);
}

export function prepareData(
    revlog: RevlogEntry[],
    maxDays: number,
    params: number[],
    rwkvCurve?: RwkvCurvePoints,
) {
    if (rwkvCurve) {
        return prepareRwkvData(revlog, maxDays, rwkvCurve);
    }
    const data: DataPoint[] = [];
    let lastReviewTime = 0;
    let lastStability = 0;
    let lastStabilityFast = 0;
    let lastDifficulty = 5.0;
    let lastStabilityS90 = 0;
    const step = Math.min(maxDays / MIN_POINTS, 1);
    let daysSinceFirstLearn = 0;

    revlog
        .slice()
        .reverse()
        .forEach((entry, index) => {
            const reviewTime = Number(entry.time);
            if (index === 0) {
                lastReviewTime = reviewTime;
                lastStability = entry.memoryState?.stabilityInternal
                    ?? entry.memoryState?.stability
                    ?? 0;
                lastStabilityFast = entry.memoryState?.stabilityFast ?? lastStability;
                lastDifficulty = entry.memoryState?.difficulty ?? 5.0;
                lastStabilityS90 = entry.memoryState?.stability
                    ?? stabilityS90(
                        lastStability,
                        params,
                        lastStabilityFast,
                        lastDifficulty,
                    );
                data.push({
                    date: new Date(reviewTime * 1000),
                    daysSinceFirstLearn: 0,
                    elapsedDaysSinceLastReview: 0,
                    retrievability: 100,
                    stability: lastStability,
                    stabilityS90: lastStabilityS90,
                });
                return;
            }

            const totalDaysElapsed = (reviewTime - lastReviewTime) / 86400;
            let elapsedDays = 0;
            while (elapsedDays < totalDaysElapsed - step) {
                elapsedDays += step;
                const retrievability = forgettingCurve(
                    lastStability,
                    lastStabilityFast,
                    lastDifficulty,
                    elapsedDays,
                    params,
                );
                data.push({
                    date: new Date((lastReviewTime + elapsedDays * 86400) * 1000),
                    daysSinceFirstLearn: data[data.length - 1].daysSinceFirstLearn + step,
                    elapsedDaysSinceLastReview: elapsedDays,
                    retrievability: retrievability * 100,
                    stability: lastStability,
                    stabilityS90: lastStabilityS90,
                });
            }
            daysSinceFirstLearn += totalDaysElapsed;
            data.push({
                date: new Date((lastReviewTime + totalDaysElapsed * 86400) * 1000),
                daysSinceFirstLearn: daysSinceFirstLearn,
                retrievability: 100,
                elapsedDaysSinceLastReview: 0,
                stability: lastStability,
                stabilityS90: lastStabilityS90,
            });

            lastReviewTime = reviewTime;
            lastStability = entry.memoryState?.stabilityInternal
                ?? entry.memoryState?.stability
                ?? 0;
            lastStabilityFast = entry.memoryState?.stabilityFast ?? lastStability;
            lastDifficulty = entry.memoryState?.difficulty ?? 5.0;
            lastStabilityS90 = entry.memoryState?.stability
                ?? stabilityS90(
                    lastStability,
                    params,
                    lastStabilityFast,
                    lastDifficulty,
                );
        });

    if (data.length === 0) {
        return [];
    }
    const lastSegmentRecall = (days: number) =>
        forgettingCurve(lastStability, lastStabilityFast, lastDifficulty, days, params);

    const now = Date.now() / 1000;
    const totalDaysSinceLastReview = (now - lastReviewTime) / 86400;
    let elapsedDays = 0;
    while (elapsedDays < totalDaysSinceLastReview - step) {
        elapsedDays += step;
        const retrievability = lastSegmentRecall(elapsedDays);
        data.push({
            date: new Date((lastReviewTime + elapsedDays * 86400) * 1000),
            daysSinceFirstLearn: data[data.length - 1].daysSinceFirstLearn + step,
            elapsedDaysSinceLastReview: elapsedDays,
            retrievability: retrievability * 100,
            stability: lastStability,
            stabilityS90: lastStabilityS90,
        });
    }
    daysSinceFirstLearn += totalDaysSinceLastReview;
    const retrievability = lastSegmentRecall(totalDaysSinceLastReview);
    data.push({
        date: new Date(now * 1000),
        daysSinceFirstLearn: daysSinceFirstLearn,
        elapsedDaysSinceLastReview: totalDaysSinceLastReview,
        retrievability: retrievability * 100,
        stability: lastStability,
        stabilityS90: lastStabilityS90,
    });

    const previewDays = maxDays - totalDaysSinceLastReview;
    let previewDaysElapsed = 0;
    while (previewDaysElapsed < previewDays) {
        previewDaysElapsed += step;
        const retrievability = lastSegmentRecall(elapsedDays + previewDaysElapsed);
        data.push({
            date: new Date((now + previewDaysElapsed * 86400) * 1000),
            daysSinceFirstLearn: data[data.length - 1].daysSinceFirstLearn + step,
            elapsedDaysSinceLastReview: totalDaysSinceLastReview + previewDaysElapsed,
            retrievability: retrievability * 100,
            stability: lastStability,
            stabilityS90: lastStabilityS90,
        });
    }

    const filteredData = filterDataByTimeRange(data, maxDays);
    return filteredData;
}

export function calculateMaxDays(filteredRevlog: RevlogEntry[], timeRange: TimeRange): number {
    if (filteredRevlog.length === 0) {
        return 0;
    }
    const today = new Date();
    const daysSinceFirstLearn = (today.getTime() / 1000 - Number(filteredRevlog[filteredRevlog.length - 1].time))
        / 86400;
    const totalDaysSinceLastReview = (today.getTime() / 1000 - Number(filteredRevlog[0].time))
        / 86400;
    const lastScheduledDays = filteredRevlog[0].interval / 86400;
    const previewDays = Math.max(lastScheduledDays * 1.5 - totalDaysSinceLastReview, lastScheduledDays * 0.5);
    return Math.min(daysSinceFirstLearn + previewDays, MAX_DAYS[timeRange]);
}

/**
 * The name the forgetting curve's tooltip gives to the card's chance of recall
 * now. The plain wording never says "retrievability" (spec/ui.md,
 * `ui.simple-recall-wording`).
 */
export function recallLabel(plainRecall: boolean): string {
    return plainRecall
        ? tr.cardStatsFsrsRetrievabilityPlain()
        : tr.cardStatsFsrsRetrievability();
}

/** The hover text of one point of the forgetting curve. */
export function forgettingCurveTooltip(
    d: DataPoint,
    maxDays: number,
    plainRecall: boolean,
): string {
    return `${maxDays >= 365 ? "Date" : "Date Time"}: ${
        maxDays >= 365 ? d.date.toLocaleDateString() : d.date.toLocaleString()
    }<br>
        ${tr.cardStatsReviewLogElapsedTime()}: ${timeSpan(d.elapsedDaysSinceLastReview * 86400)}<br>${
        recallLabel(plainRecall)
    }: ${d.retrievability.toFixed(2)}%<br>${tr.cardStatsFsrsStability()} (S90): ${timeSpan(d.stabilityS90 * 86400)}`;
}

export function renderForgettingCurve(
    filteredRevlog: RevlogEntry[],
    timeRange: TimeRange,
    svgElem: SVGElement,
    bounds: GraphBounds,
    desiredRetention: number,
    params?: number[],
    rwkvCurve?: RwkvCurvePoints,
    plainRecall = false,
) {
    const svg = select(svgElem);
    const trans = svg.transition().duration(600) as any;
    const noRwkvCurveYet = rwkvCurve !== undefined && rwkvCurve.elapsedDays.length === 0;
    if (filteredRevlog.length === 0 || params?.length !== FSRS7_PARAM_COUNT || noRwkvCurveYet) {
        setDataAvailable(svg, false);
        return;
    }
    const maxDays = calculateMaxDays(filteredRevlog, timeRange);

    const data = prepareData(filteredRevlog, maxDays, params, rwkvCurve);

    if (data.length === 0) {
        setDataAvailable(svg, false);
        return;
    } else {
        setDataAvailable(svg, true);
    }

    svg.selectAll(".forgetting-curve-line").remove();
    svg.select(".hover-columns").remove();

    const xMin = min(data, d => d.date);
    const xMax = max(data, d => d.date);
    const x = scaleTime()
        .domain([xMin!, xMax!])
        .range([bounds.marginLeft, bounds.width - bounds.marginRight]);
    const yMin = Math.max(
        0,
        100 - 1.2 * (100 - Math.min(...data.map((d) => d.retrievability))),
    );
    const y = scaleLinear()
        .domain([yMin, 100])
        .range([bounds.height - bounds.marginBottom, bounds.marginTop]);

    svg.select<SVGGElement>(".x-ticks")
        .call((selection) => selection.transition(trans).call(axisBottom(x).ticks(5).tickSizeOuter(0)))
        .attr("direction", "ltr");

    svg.select<SVGGElement>(".y-ticks")
        .attr("transform", `translate(${bounds.marginLeft},0)`)
        .call((selection) => selection.transition(trans).call(axisLeft(y).tickSizeOuter(0)))
        .attr("direction", "ltr");

    svg.select(".y-ticks .y-axis-title").remove();
    svg.select(".y-ticks")
        .append("text")
        .attr("class", "y-axis-title")
        .attr("transform", "rotate(-90)")
        .attr("y", 0 - bounds.marginLeft)
        .attr("x", 0 - (bounds.height / 2))
        .attr("font-size", "1rem")
        .attr("dy", "1.1em")
        .attr("fill", "currentColor");

    // a review without a stored RWKV curve leaves a break in the line
    const lineGenerator = line<DataPoint>()
        .defined((d) => !d.gap)
        .x((d) => x(d.date))
        .y((d) => y(d.retrievability));

    // gradient color
    const desiredRetentionY = desiredRetention * 100;
    svg.append("linearGradient")
        .attr("id", "line-gradient")
        .attr("gradientUnits", "userSpaceOnUse")
        .attr("x1", 0)
        .attr("y1", y(0))
        .attr("x2", 0)
        .attr("y2", y(100))
        .selectAll("stop")
        .data([
            { offset: "0%", color: "tomato" },
            { offset: `${desiredRetentionY}%`, color: "steelblue" },
            { offset: "100%", color: "green" },
        ])
        .enter().append("stop")
        .attr("offset", d => d.offset)
        .attr("stop-color", d => d.color);

    // Split data into past and future
    const today = new Date();
    const pastData = data.filter(d => d.date <= today);
    const futureData = data.filter(d => d.date >= today);

    // Draw solid line for past data
    svg.append("path")
        .datum(pastData)
        .attr("class", "forgetting-curve-line")
        .attr("fill", "none")
        .attr("stroke", "url(#line-gradient)")
        .attr("stroke-width", 1.5)
        .attr("d", lineGenerator);

    // Draw dashed line for future data
    svg.append("path")
        .datum(futureData)
        .attr("class", "forgetting-curve-line")
        .attr("fill", "none")
        .attr("stroke", "url(#line-gradient)")
        .attr("stroke-width", 1.5)
        .attr("stroke-dasharray", "4 4")
        .attr("d", lineGenerator);

    svg.select(".desired-retention-line").remove();
    if (desiredRetentionY > yMin) {
        svg.append("line")
            .attr("class", "desired-retention-line")
            .attr("x1", bounds.marginLeft)
            .attr("x2", bounds.width - bounds.marginRight)
            .attr("y1", y(desiredRetentionY))
            .attr("y2", y(desiredRetentionY))
            .attr("stroke", "steelblue")
            .attr("stroke-dasharray", "4 4")
            .attr("stroke-width", 1.2);
    }

    const focusLine = svg.append("line")
        .attr("class", "focus-line")
        .attr("y1", bounds.marginTop)
        .attr("y2", bounds.height - bounds.marginBottom)
        .attr("stroke", "black")
        .attr("stroke-width", 1)
        .style("opacity", 0);

    function tooltipText(d: DataPoint): string {
        return forgettingCurveTooltip(d, maxDays, plainRecall);
    }

    // hover/tooltip
    svg.append("g")
        .attr("class", "hover-columns")
        .selectAll("rect")
        .data(data.filter((d) => !d.gap))
        .join("rect")
        .attr("x", d => x(d.date) - 1)
        .attr("y", bounds.marginTop)
        .attr("width", 2)
        .attr("height", bounds.height - bounds.marginTop - bounds.marginBottom)
        .attr("fill", "transparent")
        .on("mousemove", (event: MouseEvent, d: DataPoint) => {
            const [x1, y1] = pointer(event, document.body);
            const [_, y2] = pointer(event, svg.node());

            const lineY = y(desiredRetentionY);
            focusLine.attr("x1", x(d.date) - 1).attr("x2", x(d.date) + 1).style(
                "opacity",
                1,
            );
            let text = tooltipText(d);
            const desiredRetentionPercent = desiredRetention * 100;
            if (y2 >= lineY - 10 && y2 <= lineY + 10) {
                text += `<br>${tr.cardStatsFsrsForgettingCurveDesiredRetention()}: ${
                    desiredRetentionPercent.toFixed(0)
                }%`;
            }
            showTooltip(text, x1, y1);
        })
        .on("mouseout", () => {
            focusLine.style("opacity", 0);
            hideTooltip();
        });
}
