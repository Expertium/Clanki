// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The calibration graph (spec ui.stats-model-metrics): for one chosen
 * algorithm, the share of reviews it really remembered against the
 * probability it predicted, with the reviews of each bin behind it. A
 * perfectly calibrated algorithm follows the diagonal.
 */

import type { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
import type { CalibrationBin, ReviewMetricsProgress, ReviewMetricsProgress_Series } from "@generated/anki/stats_pb";
import { ReviewMetricsProgress_Unavailable as Unavailable } from "@generated/anki/stats_pb";
import * as tr from "@generated/ftl";
import { localizedNumber } from "@tslib/i18n";
import { axisBottom, axisLeft, axisRight, line, max, scaleLinear, select } from "d3";

import type { GraphBounds } from "./graph-helpers";
import { algorithmName, ALGORITHM_COLOURS } from "./roc";

/** Every tenth: the axes step by 0.1, not 0.2 (spec ui.stats-model-metrics). */
export const axisTenths = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1];

const COUNT_COLOUR = "#8a8a8a";
/** The count bars, blue so that they do not read as a disabled graph. */
export const COUNT_BAR_COLOUR = "#6ba3d6";

/** The drawing area is square, as on the AUC-ROC graph. */
export function calibrationBounds(): GraphBounds {
    return {
        width: 510,
        height: 465,
        marginLeft: 55,
        marginRight: 55,
        marginTop: 20,
        marginBottom: 45,
    };
}

export interface CalibrationPoint {
    /** The bin's mean predicted probability. */
    predicted: number;
    /** The share of its reviews that were remembered. */
    actual: number;
    count: number;
    low: number;
    high: number;
}

export interface CalibrationSeries {
    algorithm: SchedulingAlgorithm;
    label: string;
    colour: string;
    points: CalibrationPoint[];
    reviews: number;
    averagePredicted: number;
    actualRecall: number;
}

export function binPoints(bins: CalibrationBin[]): CalibrationPoint[] {
    return bins
        .filter((bin) => bin.count > 0)
        .map((bin) => ({
            predicted: bin.sumPredicted / bin.count,
            actual: bin.sumRemembered / bin.count,
            count: bin.count,
            low: bin.low,
            high: bin.high,
        }));
}

export function hasCalibration(series: ReviewMetricsProgress_Series): boolean {
    return series.unavailable === Unavailable.AVAILABLE && series.bins.length > 0;
}

/** The algorithms the chooser offers, in menu order. */
export function chooserOptions(
    progress: ReviewMetricsProgress | null,
): { algorithm: SchedulingAlgorithm; label: string; available: boolean }[] {
    if (!progress) {
        return [];
    }
    return progress.series.map((series) => ({
        algorithm: series.algorithm,
        label: algorithmName(series.algorithm),
        available: hasCalibration(series),
    }));
}

/**
 * The algorithm to draw: the chosen one while it has a graph, else the
 * first that has one. An algorithm is never replaced silently: the chooser
 * shows which one is drawn.
 */
export function chosenAlgorithm(
    progress: ReviewMetricsProgress | null,
    chosen: SchedulingAlgorithm | null,
): SchedulingAlgorithm | null {
    if (!progress) {
        return null;
    }
    const drawable = progress.series.filter(hasCalibration);
    if (chosen !== null && drawable.some((series) => series.algorithm === chosen)) {
        return chosen;
    }
    return drawable.length > 0 ? drawable[0].algorithm : null;
}

export function calibrationSeries(
    progress: ReviewMetricsProgress | null,
    chosen: SchedulingAlgorithm | null,
): CalibrationSeries | null {
    const algorithm = chosenAlgorithm(progress, chosen);
    if (algorithm === null || !progress) {
        return null;
    }
    const series = progress.series.find((one) => one.algorithm === algorithm)!;
    return {
        algorithm,
        label: algorithmName(algorithm),
        colour: ALGORITHM_COLOURS[algorithm] ?? COUNT_COLOUR,
        points: binPoints(series.bins),
        reviews: series.reviews,
        averagePredicted: series.averagePredicted,
        actualRecall: series.actualRecall,
    };
}

/** The three tiles above the graph. */
export function tiles(series: CalibrationSeries | null): { label: string; value: string }[] {
    if (!series) {
        return [];
    }
    return [
        {
            label: tr.statisticsCalibrationAveragePredicted(),
            value: localizedNumber(series.averagePredicted * 100, 1) + "%",
        },
        {
            label: tr.statisticsCalibrationActualRecall(),
            value: localizedNumber(series.actualRecall * 100, 1) + "%",
        },
        {
            label: tr.statisticsCalibrationReviews(),
            value: localizedNumber(series.reviews, 0),
        },
    ];
}

export function renderCalibration(
    svgElem: SVGElement,
    bounds: GraphBounds,
    series: CalibrationSeries | null,
): void {
    const svg = select(svgElem);
    svg.selectAll(".calibration-drawing").remove();

    const x = scaleLinear()
        .domain([0, 1])
        .range([bounds.marginLeft, bounds.width - bounds.marginRight]);
    const y = scaleLinear()
        .domain([0, 1])
        .range([bounds.height - bounds.marginBottom, bounds.marginTop]);
    const drawing = svg.append("g").attr("class", "calibration-drawing");

    drawing
        .append("g")
        .attr("transform", `translate(0, ${bounds.height - bounds.marginBottom})`)
        .call(axisBottom(x).tickValues(axisTenths).tickFormat((value) => localizedNumber(value as number, 1)))
        .attr("opacity", 0.6);
    drawing
        .append("g")
        .attr("transform", `translate(${bounds.marginLeft}, 0)`)
        .call(axisLeft(y).tickValues(axisTenths).tickFormat((value) => localizedNumber(value as number, 1)))
        .attr("opacity", 0.6);
    drawing
        .append("text")
        .attr("x", (bounds.marginLeft + bounds.width - bounds.marginRight) / 2)
        .attr("y", bounds.height - 8)
        .attr("text-anchor", "middle")
        .attr("fill", "currentColor")
        .attr("font-size", "12px")
        .text(tr.statisticsCalibrationPredicted());
    drawing
        .append("text")
        .attr("transform", "rotate(-90)")
        .attr("x", -(bounds.marginTop + bounds.height - bounds.marginBottom) / 2)
        .attr("y", 14)
        .attr("text-anchor", "middle")
        .attr("fill", "currentColor")
        .attr("font-size", "12px")
        .text(tr.statisticsCalibrationActual());

    // the diagonal of a perfectly calibrated algorithm
    drawing
        .append("path")
        .attr(
            "d",
            line<[number, number]>()
                .x((point) => x(point[0]))
                .y((point) => y(point[1]))([[0, 0], [1, 1]]),
        )
        .attr("class", "calibration-diagonal")
        .attr("fill", "none")
        .attr("stroke", COUNT_COLOUR)
        .attr("stroke-width", 1.5)
        .attr("stroke-dasharray", "6 4");

    if (!series || series.points.length === 0) {
        return;
    }

    // the reviews of each bin, behind the line, on their own axis
    const counts = drawing.append("g").attr("class", "calibration-counts");
    const top = max(series.points.map((point) => point.count)) ?? 0;
    const countScale = scaleLinear()
        .domain([0, top])
        .range([bounds.height - bounds.marginBottom, bounds.marginTop]);
    const width = Math.max(
        2,
        (bounds.width - bounds.marginLeft - bounds.marginRight) / (series.points.length * 1.5),
    );
    for (const point of series.points) {
        counts
            .append("rect")
            .attr("x", x(point.predicted) - width / 2)
            .attr("y", countScale(point.count))
            .attr("width", width)
            .attr("height", bounds.height - bounds.marginBottom - countScale(point.count))
            .attr("fill", COUNT_BAR_COLOUR)
            .attr("opacity", 0.25);
    }
    drawing
        .append("g")
        .attr("transform", `translate(${bounds.width - bounds.marginRight}, 0)`)
        .call(axisRight(countScale).ticks(4))
        .attr("opacity", 0.5);

    // the error bars, then the line, then its points
    for (const point of series.points) {
        if (point.high > point.low) {
            drawing
                .append("line")
                .attr("class", "calibration-interval")
                .attr("x1", x(point.predicted))
                .attr("x2", x(point.predicted))
                .attr("y1", y(point.low))
                .attr("y2", y(point.high))
                .attr("stroke", series.colour)
                .attr("stroke-width", 1.5)
                .attr("opacity", 0.6);
        }
    }
    drawing
        .append("path")
        .attr(
            "d",
            line<CalibrationPoint>()
                .x((point) => x(point.predicted))
                .y((point) => y(point.actual))(series.points),
        )
        .attr("class", "calibration-line")
        .attr("fill", "none")
        .attr("stroke", series.colour)
        .attr("stroke-width", 2);
    for (const point of series.points) {
        drawing
            .append("circle")
            .attr("cx", x(point.predicted))
            .attr("cy", y(point.actual))
            .attr("r", 3)
            .attr("fill", series.colour);
    }
}
