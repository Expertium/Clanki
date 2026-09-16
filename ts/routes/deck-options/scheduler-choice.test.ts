// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test, vi } from "vitest";

vi.mock("@generated/ftl", () => ({
    deckConfigSchedulerChoiceFsrs: () => "FSRS-7",
    deckConfigSchedulerChoiceRwkvCurve: () => "RWKV-Curve",
    deckConfigSchedulerChoiceRwkvInstant: () => "RWKV-Instant",
    deckConfigSchedulerChoiceFsrsDescription: () => "fsrs",
    deckConfigSchedulerChoiceRwkvCurveDescription: () => "curve",
    deckConfigSchedulerChoiceRwkvInstantDescription: () => "instant",
}));

import { flagsFromSchedulerChoice, schedulerChoices, SchedulingAlgorithm } from "./scheduler-choice";

// Pins spec/deck-options.md#deck-options.scheduler-choice

test("each algorithm writes one set of preset switches", () => {
    expect(flagsFromSchedulerChoice(SchedulingAlgorithm.FSRS7)).toEqual({
        rwkvCurve: false,
        rwkvInstant: false,
    });
    expect(flagsFromSchedulerChoice(SchedulingAlgorithm.RWKV_CURVE)).toEqual({
        rwkvCurve: true,
        rwkvInstant: false,
    });
    expect(flagsFromSchedulerChoice(SchedulingAlgorithm.RWKV_INSTANT)).toEqual({
        rwkvCurve: false,
        rwkvInstant: true,
    });
});

test("the list offers the three algorithms in order", () => {
    expect(schedulerChoices().map((choice) => [choice.value, choice.label])).toEqual([
        [SchedulingAlgorithm.FSRS7, "FSRS-7"],
        [SchedulingAlgorithm.RWKV_CURVE, "RWKV-Curve"],
        [SchedulingAlgorithm.RWKV_INSTANT, "RWKV-Instant"],
    ]);
});
