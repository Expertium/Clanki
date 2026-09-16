// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The AUC-ROC graph (spec ui.stats-model-metrics): one curve per scheduling
 * algorithm, drawn from the reviews of the search in the page's period. The
 * curves and their areas come from the job in Python; this module only draws
 * them. An algorithm that cannot be computed has no curve and is named in a
 * note under the graph; it is never replaced by another algorithm's curve.
 */

import { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
import type { ReviewMetricsProgress, ReviewMetricsProgress_Series } from "@generated/anki/stats_pb";
import {
    ReviewMetricsProgress_State as JobState,
    ReviewMetricsProgress_Unavailable as Unavailable,
} from "@generated/anki/stats_pb";
import * as tr from "@generated/ftl";
import { localizedDate, localizedNumber } from "@tslib/i18n";
import { axisBottom, axisLeft, line, scaleLinear, select } from "d3";

import type { GraphBounds } from "./graph-helpers";

/** One colour per algorithm, so a curve keeps its colour everywhere. */
export const ALGORITHM_COLOURS: Record<number, string> = {
    [SchedulingAlgorithm.FSRS7]: "#3b82f6",
    [SchedulingAlgorithm.RWKV_CURVE]: "#ef4444",
    [SchedulingAlgorithm.RWKV_INSTANT]: "#22a06b",
};
const CHANCE_COLOUR = "#8a8a8a";

/** The drawing area is square, so random chance runs at 45 degrees. */
export function rocBounds(): GraphBounds {
    return {
        width: 500,
        height: 465,
        marginLeft: 55,
        marginRight: 45,
        marginTop: 20,
        marginBottom: 45,
    };
}

export interface RocCurve {
    algorithm: SchedulingAlgorithm;
    label: string;
    colour: string;
    auc: number;
    reviews: number;
    points: [number, number][];
}

/** The algorithm's name, as the deck-options list names it. */
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

/** "FSRS-7, AUC=0.7230" */
export function curveLabel(algorithm: SchedulingAlgorithm, auc: number): string {
    return tr.statisticsRocLegend({
        algorithm: algorithmName(algorithm),
        auc: localizedNumber(auc, 4),
    });
}

export function chanceLabel(): string {
    return tr.statisticsRocChance({ auc: localizedNumber(0.5, 4) });
}

export function hasCurve(series: ReviewMetricsProgress_Series): boolean {
    return series.unavailable === Unavailable.AVAILABLE && series.falsePositiveRate.length > 0;
}

/** The curves to draw, in the progress message's order. */
export function rocCurves(progress: ReviewMetricsProgress | null): RocCurve[] {
    if (!progress) {
        return [];
    }
    return progress.series.filter(hasCurve).map((series) => ({
        algorithm: series.algorithm,
        label: curveLabel(series.algorithm, series.auc),
        colour: ALGORITHM_COLOURS[series.algorithm] ?? CHANCE_COLOUR,
        auc: series.auc,
        reviews: series.reviews,
        points: series.falsePositiveRate.map(
            (x, index) => [x, series.truePositiveRate[index]] as [number, number],
        ),
    }));
}

/** Why an algorithm has no curve, in one sentence, or null while it works. */
export function unavailableText(series: ReviewMetricsProgress_Series): string | null {
    const algorithm = algorithmName(series.algorithm);
    switch (series.unavailable) {
        case Unavailable.NO_MODEL:
            return tr.statisticsModelMetricsNoModel({ algorithm });
        case Unavailable.NO_PARAMS:
            return tr.statisticsModelMetricsNoParams({ algorithm });
        case Unavailable.NO_REVIEWS:
            return tr.statisticsModelMetricsNoReviews({ algorithm });
        case Unavailable.UNSUPPORTED:
            return tr.statisticsModelMetricsUnsupported({ algorithm });
        default:
            return null;
    }
}

/** The notes under the graph: one line per algorithm without a curve. */
export function unavailableNotes(progress: ReviewMetricsProgress | null): string[] {
    if (!progress) {
        return [];
    }
    return progress.series
        .map(unavailableText)
        .filter((text): text is string => text !== null);
}

/**
 * What the graph says about its own data (spec ui.stats-model-metrics): how
 * many reviews every drawn algorithm shares, what was left out, which stored
 * predictions each algorithm used, and how fresh they are.
 */
export function dataNotes(progress: ReviewMetricsProgress | null): string[] {
    if (!progress) {
        return [];
    }
    const notes: string[] = [];
    if (progress.scored > 0) {
        notes.push(
            tr.statisticsModelMetricsScored({ reviews: progress.scored }),
        );
    }
    if (progress.fsrsOnly + progress.rwkvOnly + progress.unscored > 0) {
        notes.push(
            tr.statisticsModelMetricsLeftOut({
                fsrs: localizedNumber(progress.fsrsOnly, 0),
                rwkv: localizedNumber(progress.rwkvOnly, 0),
                none: localizedNumber(progress.unscored, 0),
            }),
        );
    }
    for (const series of progress.series) {
        if (hasCurve(series) && series.sampleRole) {
            notes.push(
                tr.statisticsModelMetricsRole({
                    algorithm: algorithmName(series.algorithm),
                    role: series.sampleRole,
                }),
            );
        }
    }
    if (progress.newerReviews > 0) {
        notes.push(
            tr.statisticsModelMetricsStale({
                date: progress.newestScoredSecs > 0n
                    ? localizedDate(new Date(Number(progress.newestScoredSecs) * 1000))
                    : "-",
                reviews: progress.newerReviews,
            }),
        );
    }
    return notes;
}

/** The text shown instead of a graph while there is nothing to draw. */
export function overlayText(progress: ReviewMetricsProgress | null): string | undefined {
    if (progress && rocCurves(progress).length > 0) {
        return undefined;
    }
    if (!progress || progress.state === JobState.COMPUTING) {
        return tr.cardStatsCalculating();
    }
    if (progress.state === JobState.FAILED) {
        return progress.error;
    }
    return tr.statisticsNoData();
}

export function stillComputing(progress: ReviewMetricsProgress | null): boolean {
    return progress !== null && progress.state === JobState.COMPUTING;
}

export function renderRoc(
    svgElem: SVGElement,
    bounds: GraphBounds,
    curves: RocCurve[],
): void {
    const svg = select(svgElem);
    svg.selectAll(".roc-curve").remove();
    svg.selectAll(".roc-axis").remove();

    const x = scaleLinear()
        .domain([0, 1])
        .range([bounds.marginLeft, bounds.width - bounds.marginRight]);
    const y = scaleLinear()
        .domain([0, 1])
        .range([bounds.height - bounds.marginBottom, bounds.marginTop]);
    const path = line<[number, number]>()
        .x((point) => x(point[0]))
        .y((point) => y(point[1]));

    const axes = svg.append("g").attr("class", "roc-axis");
    axes.append("g")
        .attr("transform", `translate(0, ${bounds.height - bounds.marginBottom})`)
        .call(axisBottom(x).ticks(5).tickFormat((value) => localizedNumber(value as number, 1)))
        .attr("opacity", 0.6);
    axes.append("g")
        .attr("transform", `translate(${bounds.marginLeft}, 0)`)
        .call(axisLeft(y).ticks(5).tickFormat((value) => localizedNumber(value as number, 1)))
        .attr("opacity", 0.6);
    axes.append("text")
        .attr("x", (bounds.marginLeft + bounds.width - bounds.marginRight) / 2)
        .attr("y", bounds.height - 8)
        .attr("text-anchor", "middle")
        .attr("fill", "currentColor")
        .attr("font-size", "12px")
        .text(tr.statisticsRocFalsePositiveRate());
    axes.append("text")
        .attr("transform", "rotate(-90)")
        .attr("x", -(bounds.marginTop + bounds.height - bounds.marginBottom) / 2)
        .attr("y", 14)
        .attr("text-anchor", "middle")
        .attr("fill", "currentColor")
        .attr("font-size", "12px")
        .text(tr.statisticsRocTruePositiveRate());

    const drawing = svg.append("g").attr("class", "roc-curve");
    drawing
        .append("path")
        .attr("d", path([[0, 0], [1, 1]]))
        .attr("fill", "none")
        .attr("stroke", CHANCE_COLOUR)
        .attr("stroke-width", 1.5)
        .attr("stroke-dasharray", "6 4");
    for (const curve of curves) {
        drawing
            .append("path")
            .attr("d", path(curve.points))
            .attr("fill", "none")
            .attr("stroke", curve.colour)
            .attr("stroke-width", 2);
    }
}
