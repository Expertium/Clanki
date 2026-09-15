// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { fsrsParamDiagnostics, OUTDATED_FSRS7_PREVIEW_PARAMS_WARNING } from "./fsrs-param-diagnostics";

// Pins spec/scheduling.md#sched.fsrs7-only: FSRS-7 only
test("accepts the FSRS-7 defaults (empty) and 34 FSRS-7 parameters", () => {
    for (const count of [0, 34]) {
        expect(fsrsParamDiagnostics(Array(count).fill(1)).valid).toBe(true);
    }
});

test("rejects the parameter counts of older FSRS versions", () => {
    for (const count of [17, 19, 21]) {
        expect(fsrsParamDiagnostics(Array(count).fill(1)).valid).toBe(false);
    }
});

test("rejects unexpected FSRS parameter counts", () => {
    const diagnostics = fsrsParamDiagnostics([1, 2, 3]);

    expect(diagnostics.valid).toBe(false);
    expect(diagnostics.validCount).toBe(false);
    expect(diagnostics.count).toBe(3);
    expect(diagnostics.outdatedFsrs7PreviewParams).toBe(false);
});

test("flags outdated 35-parameter FSRS-7 preview params", () => {
    const diagnostics = fsrsParamDiagnostics(Array(35).fill(1));

    expect(diagnostics.valid).toBe(false);
    expect(diagnostics.validCount).toBe(false);
    expect(diagnostics.outdatedFsrs7PreviewParams).toBe(true);
    expect(OUTDATED_FSRS7_PREVIEW_PARAMS_WARNING).toContain("35 values");
    expect(OUTDATED_FSRS7_PREVIEW_PARAMS_WARNING).toContain("34 values");
});

test("reports non-finite FSRS parameter indexes and values", () => {
    const params = Array(34).fill(1);
    params[2] = Number.NaN;
    params[5] = Number.POSITIVE_INFINITY;

    const diagnostics = fsrsParamDiagnostics(params);

    expect(diagnostics.valid).toBe(false);
    expect(diagnostics.validCount).toBe(true);
    expect(diagnostics.nonFiniteIndexes).toStrictEqual([2, 5]);
    expect(diagnostics.nonFiniteValues).toStrictEqual(["NaN", "Infinity"]);
});
