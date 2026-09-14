// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test, vi } from "vitest";

vi.mock("@generated/ftl", () => ({
    deckConfigSchedulerChoiceFsrs: () => "FSRS",
    deckConfigSchedulerChoiceRwkvCurve: () => "RWKV-Curve",
    deckConfigSchedulerChoiceRwkvInstant: () => "RWKV-Instant",
    deckConfigSchedulerChoiceSm2: () => "SM-2",
}));

import {
    flagsFromSchedulerChoice,
    SchedulerChoice,
    schedulerChoiceFromFlags,
    schedulerChoices,
} from "./scheduler-choice";

// Pins spec/deck-options.md#deck-options.scheduler-choice

test("each dropdown value writes exactly one active scheduler", () => {
    expect(flagsFromSchedulerChoice(SchedulerChoice.FSRS)).toEqual({
        fsrs: true,
        rwkvCurve: false,
        rwkvInstant: false,
    });
    expect(flagsFromSchedulerChoice(SchedulerChoice.RWKV_CURVE)).toEqual({
        fsrs: true,
        rwkvCurve: true,
        rwkvInstant: false,
    });
    expect(flagsFromSchedulerChoice(SchedulerChoice.RWKV_INSTANT)).toEqual({
        fsrs: true,
        rwkvCurve: false,
        rwkvInstant: true,
    });
    expect(flagsFromSchedulerChoice(SchedulerChoice.SM2)).toEqual({
        fsrs: false,
        rwkvCurve: false,
        rwkvInstant: false,
    });
});

test("flags round-trip through the dropdown value", () => {
    for (
        const choice of [
            SchedulerChoice.FSRS,
            SchedulerChoice.RWKV_CURVE,
            SchedulerChoice.RWKV_INSTANT,
            SchedulerChoice.SM2,
        ]
    ) {
        expect(schedulerChoiceFromFlags(flagsFromSchedulerChoice(choice))).toBe(choice);
    }
});

test("a preset with both RWKV modes on reads as RWKV-Curve", () => {
    expect(
        schedulerChoiceFromFlags({ fsrs: true, rwkvCurve: true, rwkvInstant: true }),
    ).toBe(SchedulerChoice.RWKV_CURVE);
    // RWKV flags win over the FSRS switch, which they imply
    expect(
        schedulerChoiceFromFlags({ fsrs: false, rwkvCurve: false, rwkvInstant: true }),
    ).toBe(SchedulerChoice.RWKV_INSTANT);
});

test("SM-2 is offered only behind advanced options or when already selected", () => {
    const values = (advanced: boolean, current: SchedulerChoice) =>
        schedulerChoices({ advanced, current }).map((choice) => choice.value);

    expect(values(false, SchedulerChoice.FSRS)).toEqual([
        SchedulerChoice.FSRS,
        SchedulerChoice.RWKV_CURVE,
        SchedulerChoice.RWKV_INSTANT,
    ]);
    expect(values(true, SchedulerChoice.FSRS)).toContain(SchedulerChoice.SM2);
    expect(values(false, SchedulerChoice.SM2)).toContain(SchedulerChoice.SM2);
});
