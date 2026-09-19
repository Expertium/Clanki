// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { applyAutoOptimizeDays, autoOptimizeDaysFromConfig, timeToOptimizeShown } from "./auto-optimize";

// Pins spec/deck-options.md#deck-options.fsrs-auto-optimize
test("a preset with no value optimizes every 7 days", () => {
    expect(autoOptimizeDaysFromConfig({})).toBe(7);
    expect(autoOptimizeDaysFromConfig({ fsrsAutoOptimizeDays: 0 })).toBe(0);
    const config = {};
    // showing the default writes nothing
    expect(applyAutoOptimizeDays(config, 7)).toStrictEqual({});
    expect(applyAutoOptimizeDays(config, 30)).toStrictEqual({ fsrsAutoOptimizeDays: 30 });
});

// Pins spec/deck-options.md#deck-options.fsrs-auto-optimize
test("time to optimize shows only when the preset never optimizes by itself", () => {
    expect(timeToOptimizeShown({}, 400)).toBe(false);
    expect(timeToOptimizeShown({ fsrsAutoOptimizeDays: 0 }, 400)).toBe(true);
    expect(timeToOptimizeShown({ fsrsAutoOptimizeDays: 0 }, 10)).toBe(false);
});
