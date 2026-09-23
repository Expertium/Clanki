// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";

import { rocAucDescription } from "./roc";

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
export function rocExplanation(plainRecall: boolean): string {
    return [
        tr.statisticsRocDescriptionCurve(),
        rocAucDescription(plainRecall),
        tr.statisticsModelMetricsDescriptionReviews(),
    ].join(PARAGRAPH);
}
