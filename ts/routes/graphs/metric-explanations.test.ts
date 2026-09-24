// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";
import { expect, test } from "vitest";

import { rocExplanation, rocVerdict, umPlusExplanation, umPlusVerdict } from "./metric-explanations";

// Pins spec/ui.md#ui.stats-model-metrics (the explanations). vitest loads no
// Fluent bundle, so a string is its key: what is pinned here is which text
// goes into the tooltip and which stays under the graph. The English lines
// are pinned in qt/tests/test_ui_split.py.

test("UM+ keeps its verdict under the graph and its explanation in the tooltip", () => {
    const tooltip = umPlusExplanation();
    for (
        const part of [
            tr.statisticsUmPlusDescriptionAxes(),
            tr.statisticsUmPlusDescriptionScore(),
            tr.statisticsUmPlusDescriptionOracle(),
            tr.statisticsModelMetricsDescriptionReviews(),
        ]
    ) {
        expect(tooltip).toContain(part);
    }
    expect(tooltip).not.toContain(umPlusVerdict());
});

test("AUC-ROC keeps its verdict under the graph and its explanation in the tooltip", () => {
    const tooltip = rocExplanation();
    expect(tooltip).toContain(tr.statisticsRocDescriptionCurve());
    expect(tooltip).toContain(tr.statisticsRocDescriptionAuc());
    expect(tooltip).toContain(tr.statisticsModelMetricsDescriptionReviews());
    expect(tooltip).not.toContain(rocVerdict());
});
