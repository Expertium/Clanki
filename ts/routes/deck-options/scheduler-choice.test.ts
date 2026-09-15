// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test, vi } from "vitest";

vi.mock("@generated/ftl", () => ({
    deckConfigSchedulerChoiceFsrs: () => "FSRS-7",
    deckConfigSchedulerChoiceRwkvCurve: () => "RWKV-Curve",
    deckConfigSchedulerChoiceRwkvInstant: () => "RWKV-Instant",
}));

import { SchedulerChoice, schedulerChoiceFromFlags, schedulerChoiceLabel } from "./scheduler-choice";

// Pins spec/deck-options.md#deck-options.scheduler-choice

test("each preset's flags read as one algorithm", () => {
    expect(schedulerChoiceFromFlags({ fsrs: true, rwkvCurve: false, rwkvInstant: false })).toBe(
        SchedulerChoice.FSRS,
    );
    expect(schedulerChoiceFromFlags({ fsrs: true, rwkvCurve: true, rwkvInstant: false })).toBe(
        SchedulerChoice.RWKV_CURVE,
    );
    expect(schedulerChoiceFromFlags({ fsrs: true, rwkvCurve: false, rwkvInstant: true })).toBe(
        SchedulerChoice.RWKV_INSTANT,
    );
});

test("a preset with both RWKV modes on reads as RWKV-Curve", () => {
    expect(
        schedulerChoiceFromFlags({ fsrs: true, rwkvCurve: true, rwkvInstant: true }),
    ).toBe(SchedulerChoice.RWKV_CURVE);
});

test("the FSRS switch never changes the value: SM-2 is not an algorithm here", () => {
    expect(
        schedulerChoiceFromFlags({ fsrs: false, rwkvCurve: false, rwkvInstant: false }),
    ).toBe(SchedulerChoice.FSRS);
    expect(
        schedulerChoiceFromFlags({ fsrs: false, rwkvCurve: false, rwkvInstant: true }),
    ).toBe(SchedulerChoice.RWKV_INSTANT);
});

test("each algorithm has its name", () => {
    expect(
        [SchedulerChoice.FSRS, SchedulerChoice.RWKV_CURVE, SchedulerChoice.RWKV_INSTANT].map(
            schedulerChoiceLabel,
        ),
    ).toEqual(["FSRS-7", "RWKV-Curve", "RWKV-Instant"]);
});
