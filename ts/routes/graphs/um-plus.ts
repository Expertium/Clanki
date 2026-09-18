// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The UM+ comparison (spec ui.stats-model-metrics): two algorithms, the
 * ratings grouped by how far their predictions differ, and each algorithm's
 * mean error inside those groups. Where the two disagree most is where a
 * wrong algorithm shows itself, so a line that stays near zero across the
 * whole width is the better algorithm, whatever its average error.
 */

import type { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
import type { ReviewMetricsProgress, UmPlusBin, UmPlusPair } from "@generated/anki/stats_pb";
import * as tr from "@generated/ftl";
import { localizedNumber } from "@tslib/i18n";
import { axisBottom, axisLeft, line, scaleLinear, select } from "d3";

import type { GraphBounds } from "./graph-helpers";
import { ALGORITHM_COLOURS, algorithmName } from "./roc";

/** A group with fewer ratings than this is hidden unless asked for. */
export const SMALL_GROUP = 200;
/**
 * A pair with fewer shared ratings than this is named but never drawn
 * (spec ui.stats-model-metrics). UM+ spreads a pair's ratings over 41
 * groups, so under this floor the middle groups hold a handful of ratings
 * each and one card's run of answers moves a bubble visibly. A graph built
 * on that misleads worse than an absent one.
 */
export const PAIR_FLOOR = 200;
const ZERO_COLOUR = "#8a8a8a";

/** The UM+ graph is wide, not square: its x axis is a difference. */
export function umPlusBounds(): GraphBounds {
    return {
        width: 600,
        height: 320,
        marginLeft: 60,
        marginRight: 20,
        marginTop: 20,
        marginBottom: 45,
    };
}

export interface UmPlusPoint {
    difference: number;
    errorA: number;
    errorB: number;
    count: number;
    /** The group's share of the pair's ratings, 0 to 1. */
    share: number;
}

export interface UmPlusView {
    algorithmA: SchedulingAlgorithm;
    algorithmB: SchedulingAlgorithm;
    labelA: string;
    labelB: string;
    colourA: string;
    colourB: string;
    points: UmPlusPoint[];
    reviews: number;
    hidden: number;
}

export function pairKey(pair: UmPlusPair): string {
    return `${pair.algorithmA}-${pair.algorithmB}`;
}

export function pairLabel(pair: UmPlusPair): string {
    return tr.statisticsUmPlusPair({
        first: algorithmName(pair.algorithmA),
        second: algorithmName(pair.algorithmB),
    });
}

/** A pair with enough shared ratings to be worth drawing. */
export function drawable(pair: UmPlusPair): boolean {
    return pair.reviews >= PAIR_FLOOR;
}

/**
 * The pairs the menu offers: only those with enough shared ratings. A pair
 * that exists but is too thin is not offered, and is named under the graph
 * instead, so it is never drawn and never silently missing.
 */
export function pairOptions(
    progress: ReviewMetricsProgress | null,
): { key: string; label: string }[] {
    if (!progress) {
        return [];
    }
    return progress.umPlus.filter(drawable).map((pair) => ({
        key: pairKey(pair),
        label: pairLabel(pair),
    }));
}

/**
 * One line per pair that has ratings but too few of them, saying how many
 * it has, how many it needs, and what would fill it.
 */
export function thinPairNotes(progress: ReviewMetricsProgress | null): string[] {
    if (!progress) {
        return [];
    }
    return progress.umPlus
        .filter((pair) => !drawable(pair))
        .map((pair) =>
            tr.statisticsUmPlusTooFew({
                pair: pairLabel(pair),
                reviews: pair.reviews,
                needed: String(PAIR_FLOOR),
            })
        );
}

/** The chosen pair while it is drawable, else the first drawable one. */
export function chosenPair(
    progress: ReviewMetricsProgress | null,
    chosen: string | null,
): UmPlusPair | null {
    if (!progress) {
        return null;
    }
    const pairs = progress.umPlus.filter(drawable);
    if (pairs.length === 0) {
        return null;
    }
    return pairs.find((pair) => pairKey(pair) === chosen) ?? pairs[0];
}

function points(bins: UmPlusBin[], reviews: number): UmPlusPoint[] {
    return bins.map((bin) => ({
        difference: bin.sumDifference / bin.count,
        errorA: bin.sumErrorA / bin.count,
        errorB: bin.sumErrorB / bin.count,
        count: bin.count,
        share: reviews > 0 ? bin.count / reviews : 0,
    }));
}

export function umPlusView(
    pair: UmPlusPair | null,
    showSmallGroups: boolean,
): UmPlusView | null {
    if (!pair) {
        return null;
    }
    const all = points(pair.bins, pair.reviews);
    const drawn = showSmallGroups ? all : all.filter((point) => point.count >= SMALL_GROUP);
    return {
        algorithmA: pair.algorithmA,
        algorithmB: pair.algorithmB,
        labelA: tr.statisticsUmPlusLegend({
            algorithm: algorithmName(pair.algorithmA),
            um: localizedNumber(pair.umA, 4),
            slope: localizedNumber(pair.slopeA, 3),
        }),
        labelB: tr.statisticsUmPlusLegend({
            algorithm: algorithmName(pair.algorithmB),
            um: localizedNumber(pair.umB, 4),
            slope: localizedNumber(pair.slopeB, 3),
        }),
        colourA: ALGORITHM_COLOURS[pair.algorithmA] ?? ZERO_COLOUR,
        colourB: ALGORITHM_COLOURS[pair.algorithmB] ?? ZERO_COLOUR,
        points: drawn,
        reviews: pair.reviews,
        hidden: all.length - drawn.length,
    };
}

export function renderUmPlus(
    svgElem: SVGElement,
    bounds: GraphBounds,
    view: UmPlusView | null,
): void {
    const svg = select(svgElem);
    svg.selectAll(".um-plus-drawing").remove();
    const drawing = svg.append("g").attr("class", "um-plus-drawing");

    const spread = Math.max(
        0.1,
        ...(view?.points ?? []).map((point) => Math.abs(point.difference)),
    );
    const x = scaleLinear()
        .domain([-spread, spread])
        .range([bounds.marginLeft, bounds.width - bounds.marginRight]);
    const errors = (view?.points ?? []).flatMap((point) => [point.errorA, point.errorB]);
    const reach = Math.max(0.05, ...errors.map((error) => Math.abs(error)));
    const y = scaleLinear()
        .domain([-reach, reach])
        .range([bounds.height - bounds.marginBottom, bounds.marginTop]);

    drawing
        .append("g")
        .attr("transform", `translate(0, ${bounds.height - bounds.marginBottom})`)
        .call(axisBottom(x).ticks(7).tickFormat((value) => localizedNumber(value as number, 2)))
        .attr("opacity", 0.6);
    drawing
        .append("g")
        .attr("transform", `translate(${bounds.marginLeft}, 0)`)
        .call(axisLeft(y).ticks(5).tickFormat((value) => localizedNumber(value as number, 2)))
        .attr("opacity", 0.6);
    drawing
        .append("text")
        .attr("x", (bounds.marginLeft + bounds.width - bounds.marginRight) / 2)
        .attr("y", bounds.height - 8)
        .attr("text-anchor", "middle")
        .attr("fill", "currentColor")
        .attr("font-size", "12px")
        .text(tr.statisticsUmPlusDifference());
    drawing
        .append("text")
        .attr("transform", "rotate(-90)")
        .attr("x", -(bounds.marginTop + bounds.height - bounds.marginBottom) / 2)
        .attr("y", 14)
        .attr("text-anchor", "middle")
        .attr("fill", "currentColor")
        .attr("font-size", "12px")
        .text(tr.statisticsUmPlusError());

    // a perfect algorithm sits on this line whatever the disagreement
    drawing
        .append("line")
        .attr("class", "um-plus-zero")
        .attr("x1", bounds.marginLeft)
        .attr("x2", bounds.width - bounds.marginRight)
        .attr("y1", y(0))
        .attr("y2", y(0))
        .attr("stroke", ZERO_COLOUR)
        .attr("stroke-width", 1.5);

    if (!view || view.points.length === 0) {
        return;
    }
    const shape = line<UmPlusPoint>().x((point) => x(point.difference));
    for (
        const [colour, error, klass] of [
            [view.colourA, (point: UmPlusPoint) => point.errorA, "um-plus-a"],
            [view.colourB, (point: UmPlusPoint) => point.errorB, "um-plus-b"],
        ] as [string, (point: UmPlusPoint) => number, string][]
    ) {
        drawing
            .append("path")
            .attr("class", klass)
            .attr("d", shape.y((point) => y(error(point)))(view.points))
            .attr("fill", "none")
            .attr("stroke", colour)
            .attr("stroke-width", 2);
        for (const point of view.points) {
            drawing
                .append("circle")
                .attr("class", `${klass}-bubble`)
                .attr("cx", x(point.difference))
                .attr("cy", y(error(point)))
                // the area of the bubble is the group's share of the ratings
                .attr("r", 2 + Math.sqrt(point.share) * 16)
                .attr("fill", colour)
                .attr("opacity", 0.5);
        }
    }
}
