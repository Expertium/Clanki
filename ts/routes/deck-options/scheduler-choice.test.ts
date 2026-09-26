// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import * as tr from "@generated/ftl";

import { intervalSettingsApply, runsRwkvInstant, stepsNotEmptyWarning } from "./scheduler-choice";

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

// Pins spec/deck-options.md#deck-options.steps-warning-when-not-empty.
// vitest loads no Fluent bundle, so a string is its key: what is pinned here
// is when the warning shows. The English sentence is pinned in
// qt/tests/test_ui_split.py.

test("the steps warning shows for any step and only for a non-empty field", () => {
    const warning = tr.deckConfigStepsFieldNotEmpty();
    // a relearning step of 1 minute (Andrew's case), a learning step, a long step
    expect(stepsNotEmptyWarning([1])).toBe(warning);
    expect(stepsNotEmptyWarning([1, 10])).toBe(warning);
    expect(stepsNotEmptyWarning([3 * 24 * 60])).toBe(warning);
    expect(stepsNotEmptyWarning([])).toBe("");
});
