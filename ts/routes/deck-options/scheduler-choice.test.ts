// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import * as tr from "@generated/ftl";

import { intervalSettingsApply, runsRwkvInstant, schedulerLabel, stepsTooLargeWarning } from "./scheduler-choice";

// Pins spec/scheduling.md#sched.rwkv-instant-no-steps (the settings side)

const preset = (rwkvCurve: boolean, rwkvInstant: boolean) => ({
    rwkvReviewEnabled: rwkvCurve,
    rwkvReviewInstantOrderEnabled: rwkvInstant,
});

test("only the RWKV-Instant preset runs RWKV-Instant", () => {
    expect(runsRwkvInstant(preset(false, true))).toBe(true);
    expect(runsRwkvInstant(preset(false, false))).toBe(false);
    expect(runsRwkvInstant(preset(true, false))).toBe(false);
    // RWKV-Curve wins when a preset from an older version carries both
    expect(runsRwkvInstant(preset(true, true))).toBe(false);
});

test("the interval settings apply to every algorithm except RWKV-Instant", () => {
    // FSRS-7 and RWKV-Curve both schedule with an interval
    expect(intervalSettingsApply(preset(false, false))).toBe(true);
    expect(intervalSettingsApply(preset(true, false))).toBe(true);
    expect(intervalSettingsApply(preset(false, true))).toBe(false);
});

// Pins spec/deck-options.md#deck-options.steps-warning-names-the-algorithm.
// vitest loads no Fluent bundle, so a string is its key: what is pinned here
// is which name the warning takes and when it shows. The English sentence is
// pinned in qt/tests/test_ui_split.py.

test("the long-steps warning names the algorithm that schedules the preset", () => {
    expect(schedulerLabel(preset(false, false))).toBe(tr.deckConfigSchedulerChoiceFsrs());
    expect(schedulerLabel(preset(true, false))).toBe(tr.deckConfigSchedulerChoiceRwkvCurve());
    expect(schedulerLabel(preset(false, true))).toBe(tr.deckConfigSchedulerChoiceRwkvInstant());
    // RWKV-Curve wins when a preset from an older version carries both
    expect(schedulerLabel(preset(true, true))).toBe(tr.deckConfigSchedulerChoiceRwkvCurve());
});

test("the long-steps warning shows from a last step of one day", () => {
    const curve = preset(true, false);
    expect(stepsTooLargeWarning(curve, true, 1)).not.toBe("");
    expect(stepsTooLargeWarning(curve, true, 3)).not.toBe("");
    expect(stepsTooLargeWarning(curve, true, 23 / 24)).toBe("");
    expect(stepsTooLargeWarning(curve, true, 0)).toBe("");
    expect(stepsTooLargeWarning(curve, false, 3)).toBe("");
});
