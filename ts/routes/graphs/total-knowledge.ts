// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The Total Knowledge graph (spec ui.stats-total-knowledge): per day of the
 * search's whole review history, the cards reviewed so far (the upper
 * bound) and the sum of their retrievability under the collection's
 * algorithm. FSRS-7's sum comes with the bound; RWKV's arrives day by day
 * from its job, and the days it has not reached yet are drawn blurred,
 * behind a sweep line. Simple mode draws the sum only; Advanced mode has a
 * checkbox for the bound.
 */

import { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
import type { TotalKnowledgeResponse, TotalKnowledgeRwkvProgress } from "@generated/anki/stats_pb";
import { TotalKnowledgeRwkvProgress_State as RwkvState } from "@generated/anki/stats_pb";
import * as tr from "@generated/ftl";
import { localizedDate, localizedNumber } from "@tslib/i18n";
import { area, axisBottom, axisLeft, bisector, line, max, pointer, scaleLinear, scaleTime, select } from "d3";

import type { GraphBounds } from "./graph-helpers";
import { setDataAvailable } from "./graph-helpers";
import { hideTooltip, showTooltip } from "./tooltip-utils.svelte";

const DAY_MS = 86_400_000;
export const KNOWN_COLOUR = "#3b82f6";
export const REVIEWED_COLOUR = "#8a8a8a";

export interface TotalKnowledgePoint {
    /** Days relative to today (0 = today). */
    day: number;
    reviewed: number;
    /** The sum of R; null while RWKV has not reached this day. */
    known: number | null;
}

export interface TotalKnowledgeData {
    points: TotalKnowledgePoint[];
    /**
     * While RWKV computes: the last day whose sum is known (the day before
     * the first one while none is). Null when every day is known.
     */
    computedThroughDay: number | null;
}

export function isRwkv(algorithm: SchedulingAlgorithm): boolean {
    return algorithm !== SchedulingAlgorithm.FSRS7;
}

/** The collection's algorithm, named as the deck-options list names it. */
export function algorithmName(algorithm: SchedulingAlgorithm): string {
    switch (algorithm) {
        case SchedulingAlgorithm.RWKV_CURVE:
            return tr.deckConfigSchedulerChoiceRwkvCurve();
        case SchedulingAlgorithm.RWKV_INSTANT:
            return tr.deckConfigSchedulerChoiceRwkvInstant();
        default:
            return tr.deckConfigSchedulerChoiceFsrs();
    }
}

/**
 * The line under the title. The plain wording says the same thing without
 * the word "retrievability" (spec ui.stats-total-knowledge,
 * ui.simple-recall-wording).
 */
export function subtitleText(plainRecall: boolean): string {
    return plainRecall
        ? tr.statisticsTotalKnowledgeSubtitlePlain()
        : tr.statisticsTotalKnowledgeSubtitle();
}

/**
 * Whether the "Reviewed" bound is drawn (spec ui.stats-total-knowledge):
 * never in Simple mode, and in Advanced mode as the graph's own checkbox
 * says. The checkbox starts on, so Advanced mode draws it by default.
 */
export function showsReviewed(advanced: boolean, checked: boolean): boolean {
    return advanced && checked;
}

/** RWKV's sum on `day`, or null while its job has not reached it. */
function rwkvKnown(progress: TotalKnowledgeRwkvProgress | null, day: number): number | null {
    if (!progress || progress.sumR.length === 0) {
        return null;
    }
    // before RWKV's history starts, no card has an R
    if (day < progress.firstDay) {
        return 0;
    }
    const index = day - progress.firstDay;
    return index < progress.sumR.length ? progress.sumR[index] : null;
}

/**
 * The bound with the sum of R of the collection's algorithm only: FSRS-7's
 * from the backend, RWKV's from its job. Never one algorithm's values in
 * place of the other's.
 */
export function totalKnowledgeData(
    response: TotalKnowledgeResponse,
    rwkv: TotalKnowledgeRwkvProgress | null,
): TotalKnowledgeData {
    const underRwkv = isRwkv(response.algorithm);
    const points = response.reviewedCards.map((reviewed, index) => {
        const day = response.firstDay + index;
        const known = underRwkv ? rwkvKnown(rwkv, day) : (response.sumR[index] ?? null);
        return { day, reviewed, known };
    });
    let computedThroughDay: number | null = null;
    if (underRwkv && rwkv?.state !== RwkvState.DONE) {
        computedThroughDay = rwkv && rwkv.sumR.length
            ? rwkv.firstDay + rwkv.sumR.length - 1
            : response.firstDay - 1;
    }
    return { points, computedThroughDay };
}

/** Whether the page asks the RWKV job again for more days. */
export function rwkvStillComputing(progress: TotalKnowledgeRwkvProgress | null): boolean {
    return progress?.state === RwkvState.COMPUTING;
}

/** Text over the graph instead of the drawing, or undefined for none. */
export function overlayText(
    response: TotalKnowledgeResponse | null,
    rwkv: TotalKnowledgeRwkvProgress | null,
): string | undefined {
    if (!response) {
        return tr.cardStatsCalculating();
    }
    if (rwkv?.state === RwkvState.NO_MODEL) {
        return tr.statisticsTotalKnowledgeRwkvModelNotFound();
    }
    if (rwkv?.state === RwkvState.FAILED) {
        return rwkv.error || undefined;
    }
    return undefined;
}

/** Whether the graph is drawn at all (the overlay covers it otherwise). */
export function hasDrawing(
    response: TotalKnowledgeResponse | null,
    rwkv: TotalKnowledgeRwkvProgress | null,
): boolean {
    return Boolean(
        response
            && response.reviewedCards.length
            && rwkv?.state !== RwkvState.NO_MODEL
            && rwkv?.state !== RwkvState.FAILED,
    );
}

export function dayToDate(day: number, now: number = Date.now()): Date {
    return new Date(now + day * DAY_MS);
}

export function renderTotalKnowledge(
    svgElem: SVGElement,
    bounds: GraphBounds,
    data: TotalKnowledgeData | null,
    /** Draw the "Reviewed" bound as well as "Known" (`showsReviewed`). */
    reviewed = true,
    now: number = Date.now(),
): void {
    const svg = select(svgElem);
    const drawing = svg.select<SVGGElement>(".total-knowledge");
    drawing.selectAll("*").remove();
    svg.selectAll(".total-knowledge-hover").remove();
    if (!data || !data.points.length) {
        setDataAvailable(svg, false);
        return;
    }
    const { points, computedThroughDay } = data;
    const date = (day: number) => dayToDate(day, now);

    const x = scaleTime()
        .domain([date(points[0].day), date(points[points.length - 1].day)])
        .range([bounds.marginLeft, bounds.width - bounds.marginRight]);
    // always the bound's maximum, so hiding the "Reviewed" line keeps the
    // scale it has in Advanced mode (and RWKV's day-by-day sums, which only
    // grow, never rescale the axis under the user)
    const y = scaleLinear()
        .domain([0, Math.max(1, max(points, (p) => p.reviewed) ?? 1)])
        .nice()
        .range([bounds.height - bounds.marginBottom, bounds.marginTop]);

    svg.select<SVGGElement>(".x-ticks")
        .call(axisBottom(x).ticks(6).tickSizeOuter(0))
        .attr("direction", "ltr");
    svg.select<SVGGElement>(".y-ticks")
        .call(
            axisLeft(y)
                .ticks(bounds.height / 50)
                .tickSizeOuter(0)
                .tickFormat((n) => localizedNumber(n as number, 0)),
        )
        .attr("direction", "ltr");

    // the sweep: sharp on its left, blurred on its right
    const left = bounds.marginLeft;
    const right = bounds.width - bounds.marginRight;
    const sweepX = computedThroughDay === null
        ? right
        : Math.min(right, Math.max(left, x(date(computedThroughDay))));
    const defs = drawing.append("defs");
    defs.append("filter")
        .attr("id", "total-knowledge-blur")
        .append("feGaussianBlur")
        .attr("stdDeviation", 3);
    defs.append("clipPath")
        .attr("id", "total-knowledge-done")
        .append("rect")
        .attr("x", 0)
        .attr("y", 0)
        .attr("width", sweepX)
        .attr("height", bounds.height);
    defs.append("clipPath")
        .attr("id", "total-knowledge-pending")
        .append("rect")
        .attr("x", sweepX)
        .attr("y", 0)
        .attr("width", Math.max(0, bounds.width - sweepX))
        .attr("height", bounds.height);

    const reviewedLine = line<TotalKnowledgePoint>()
        .x((p) => x(date(p.day)))
        .y((p) => y(p.reviewed));
    const knownArea = area<TotalKnowledgePoint>()
        .defined((p) => p.known !== null)
        .x((p) => x(date(p.day)))
        .y0(y(0))
        .y1((p) => y(p.known ?? 0));
    const knownLine = line<TotalKnowledgePoint>()
        .defined((p) => p.known !== null)
        .x((p) => x(date(p.day)))
        .y((p) => y(p.known ?? 0));

    const done = drawing.append("g").attr("clip-path", "url(#total-knowledge-done)");
    done.append("path")
        .attr("d", knownArea(points))
        .attr("fill", KNOWN_COLOUR)
        .attr("fill-opacity", 0.25);
    done.append("path")
        .attr("d", knownLine(points))
        .attr("fill", "none")
        .attr("stroke", KNOWN_COLOUR)
        .attr("stroke-width", 1.5);
    if (reviewed) {
        done.append("path")
            .attr("d", reviewedLine(points))
            .attr("fill", "none")
            .attr("stroke", REVIEWED_COLOUR)
            .attr("stroke-width", 1.5)
            .attr("stroke-dasharray", "4,3");
    }

    if (computedThroughDay !== null) {
        if (reviewed) {
            drawing
                .append("g")
                .attr("clip-path", "url(#total-knowledge-pending)")
                .append("g")
                .attr("filter", "url(#total-knowledge-blur)")
                .append("path")
                .attr("d", reviewedLine(points))
                .attr("fill", "none")
                .attr("stroke", REVIEWED_COLOUR)
                .attr("stroke-width", 3);
        }
        drawing
            .append("line")
            .attr("class", "total-knowledge-sweep")
            .attr("x1", sweepX)
            .attr("x2", sweepX)
            .attr("y1", bounds.marginTop)
            .attr("y2", bounds.height - bounds.marginBottom)
            .attr("stroke", KNOWN_COLOUR)
            .attr("stroke-width", 2);
    }

    // hover: the day's values
    const focus = drawing
        .append("line")
        .attr("y1", bounds.marginTop)
        .attr("y2", bounds.height - bounds.marginBottom)
        .attr("stroke", "currentColor")
        .attr("stroke-opacity", 0.4)
        .style("display", "none");
    const byDate = bisector((p: TotalKnowledgePoint) => date(p.day).getTime()).center;
    svg.append("rect")
        .attr("class", "total-knowledge-hover")
        .attr("x", left)
        .attr("y", bounds.marginTop)
        .attr("width", right - left)
        .attr("height", bounds.height - bounds.marginTop - bounds.marginBottom)
        .attr("fill", "transparent")
        .on("mousemove", (event: MouseEvent) => {
            const [mouseX] = pointer(event);
            const point = points[byDate(points, x.invert(mouseX).getTime())];
            const pointX = x(date(point.day));
            focus.attr("x1", pointX).attr("x2", pointX).style("display", null);
            showTooltip(
                tooltipText(point, date(point.day), reviewed),
                event.pageX,
                event.pageY,
            );
        })
        .on("mouseout", () => {
            focus.style("display", "none");
            hideTooltip();
        });

    setDataAvailable(svg, true);
}

/**
 * The "Known" value the tooltip shows: a whole number of cards. The rounding
 * is for display only; the sum itself keeps every decimal.
 */
export function knownCardsShown(known: number): number {
    return Math.round(known);
}

export function tooltipText(
    point: TotalKnowledgePoint,
    date: Date,
    /** The graph draws the "Reviewed" bound, so the tooltip names it too. */
    reviewed = true,
): string {
    const known = point.known === null
        ? `${tr.statisticsTotalKnowledgeKnown()}: ${tr.cardStatsCalculating()}`
        : tr.statisticsTotalKnowledgeKnownCards({ cards: knownCardsShown(point.known) });
    const lines = [
        localizedDate(date),
        `<span style="color:${KNOWN_COLOUR}">■</span> ${known}`,
    ];
    if (reviewed) {
        lines.push(
            `<span style="color:${REVIEWED_COLOUR}">■</span> ${
                tr.statisticsTotalKnowledgeReviewedCards({ cards: point.reviewed })
            }`,
        );
    }
    return lines.join("<br>");
}
