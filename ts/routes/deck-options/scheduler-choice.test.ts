// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test, vi } from "vitest";

vi.mock("@generated/ftl", () => ({
    deckConfigSchedulerChoiceFsrs: () => "FSRS",
    deckConfigSchedulerChoiceRwkvCurve: () => "RWKV-Curve",
    deckConfigSchedulerChoiceRwkvInstant: () => "RWKV-Instant",
}));

import {
    flagsFromSchedulerChoice,
    SchedulerChoice,
    schedulerChoiceFromFlags,
    schedulerChoices,
} from "./scheduler-choice";

// Pins spec/deck-options.md#deck-options.scheduler-choice

const ALL = [SchedulerChoice.FSRS, SchedulerChoice.RWKV_CURVE, SchedulerChoice.RWKV_INSTANT];

test("each dropdown value writes exactly one active scheduler, with FSRS on", () => {
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
});

test("flags round-trip through the dropdown value", () => {
    for (const choice of ALL) {
        expect(schedulerChoiceFromFlags(flagsFromSchedulerChoice(choice))).toBe(choice);
    }
});

test("a preset with both RWKV modes on reads as RWKV-Curve", () => {
    expect(
        schedulerChoiceFromFlags({ fsrs: true, rwkvCurve: true, rwkvInstant: true }),
    ).toBe(SchedulerChoice.RWKV_CURVE);
});

test("the FSRS switch never changes the value: SM-2 is not selectable", () => {
    expect(
        schedulerChoiceFromFlags({ fsrs: false, rwkvCurve: false, rwkvInstant: false }),
    ).toBe(SchedulerChoice.FSRS);
    expect(
        schedulerChoiceFromFlags({ fsrs: false, rwkvCurve: false, rwkvInstant: true }),
    ).toBe(SchedulerChoice.RWKV_INSTANT);
    expect(schedulerChoices().map((choice) => choice.value)).toEqual(ALL);
});
