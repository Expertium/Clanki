// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";
import { expect, test } from "vitest";

import {
    calibrationExplanation,
    rocExplanation,
    rocVerdict,
    umPlusExplanation,
    umPlusVerdict,
    withNotes,
} from "./metric-explanations";

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

test("calibration puts its whole explanation in the tooltip", () => {
    const tooltip = calibrationExplanation();
    expect(tooltip).toContain(tr.statisticsCalibrationDescriptionLine());
    expect(tooltip).toContain(tr.statisticsCalibrationDescriptionBars());
    expect(tooltip).toContain(tr.statisticsModelMetricsDescriptionReviews());
});

test("the notes about the data follow the explanation in the tooltip", () => {
    expect(withNotes("how to read it", ["13 small groups are hidden.", "Scored on 5 reviews."])).toBe(
        "how to read it\n\n13 small groups are hidden.\n\nScored on 5 reviews.",
    );
    expect(withNotes("how to read it", [])).toBe("how to read it");
});
