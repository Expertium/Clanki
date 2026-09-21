// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { intervalSettingsApply, runsRwkvInstant } from "./scheduler-choice";

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
