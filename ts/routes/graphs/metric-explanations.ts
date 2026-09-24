// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";

/*
 * The model-quality graphs keep one short line under the graph, which way is
 * better, and put how the number is calculated in the tooltip of the info
 * badge next to the title (spec ui.stats-model-metrics). Andrew, 2026-09-23:
 * "Reading is for nerds, lol. Let's not have too much text unless the user
 * asks for it".
 */

/** Between two paragraphs of a tooltip (InfoTooltip keeps the line breaks). */
const PARAGRAPH = "\n\n";

/** The line under the UM+ graph. */
export function umPlusVerdict(): string {
    return tr.statisticsUmPlusVerdict();
}

/** The UM+ graph's tooltip: how to read it and how the numbers are made. */
export function umPlusExplanation(): string {
    return [
        tr.statisticsUmPlusDescriptionAxes(),
        tr.statisticsUmPlusDescriptionScore(),
        tr.statisticsUmPlusDescriptionOracle(),
        tr.statisticsModelMetricsDescriptionReviews(),
    ].join(PARAGRAPH);
}

/** The line under the AUC-ROC graph. */
export function rocVerdict(): string {
    return tr.statisticsRocVerdict();
}

/** The AUC-ROC graph's tooltip. */
export function rocExplanation(): string {
    return [
        tr.statisticsRocDescriptionCurve(),
        tr.statisticsRocDescriptionAuc(),
        tr.statisticsModelMetricsDescriptionReviews(),
    ].join(PARAGRAPH);
}

/** The calibration graph's tooltip; it has no line under the graph. */
export function calibrationExplanation(): string {
    return [
        tr.statisticsCalibrationDescriptionLine(),
        tr.statisticsCalibrationDescriptionBars(),
        tr.statisticsModelMetricsDescriptionReviews(),
    ].join(PARAGRAPH);
}

/**
 * A tooltip with the notes about the data after its explanation: the hidden
 * groups, the reviews scored, missing or newer predictions. Andrew,
 * 2026-09-24, for UM+ and calibration: move the text under the graph into the
 * tooltip.
 */
export function withNotes(explanation: string, notes: string[]): string {
    return [explanation, ...notes].join(PARAGRAPH);
}
