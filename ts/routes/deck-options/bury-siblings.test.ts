// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { applyBurySiblings, type BurySettings, burySiblingsFromConfig, burySiblingsPartlyOn } from "./bury-siblings";

// Pins spec/deck-options.md#deck-options.simple-view (the Bury siblings switch)

function settings(buryNew: boolean, buryReviews: boolean, buryInterdayLearning: boolean): BurySettings {
    return { buryNew, buryReviews, buryInterdayLearning };
}

test("the switch reads as on only when all three bury settings are on", () => {
    expect(burySiblingsFromConfig(settings(true, true, true))).toBe(true);
    expect(burySiblingsFromConfig(settings(false, false, false))).toBe(false);
    expect(burySiblingsFromConfig(settings(true, false, false))).toBe(false);
    expect(burySiblingsFromConfig(settings(true, true, false))).toBe(false);
    expect(burySiblingsFromConfig(settings(false, true, true))).toBe(false);
});

test("some but not all bury settings on reads as partly on", () => {
    expect(burySiblingsPartlyOn(settings(true, false, false))).toBe(true);
    expect(burySiblingsPartlyOn(settings(true, true, false))).toBe(true);
    expect(burySiblingsPartlyOn(settings(false, true, true))).toBe(true);
    expect(burySiblingsPartlyOn(settings(true, true, true))).toBe(false);
    expect(burySiblingsPartlyOn(settings(false, false, false))).toBe(false);
});

test("turning the switch on or off sets all three settings", () => {
    expect(applyBurySiblings(settings(true, false, false), true)).toEqual(settings(true, true, true));
    expect(applyBurySiblings(settings(false, false, false), true)).toEqual(settings(true, true, true));
    expect(applyBurySiblings(settings(true, true, true), false)).toEqual(settings(false, false, false));
});

test("a preset that already reads as the switch value is left alone", () => {
    const mixed = settings(true, false, true);
    expect(applyBurySiblings(mixed, false)).toBe(mixed);
    expect(mixed).toEqual(settings(true, false, true));

    const allOn = settings(true, true, true);
    expect(applyBurySiblings(allOn, true)).toBe(allOn);
    expect(allOn).toEqual(settings(true, true, true));
});

test("the value round-trips through the switch", () => {
    for (const on of [true, false]) {
        expect(burySiblingsFromConfig(applyBurySiblings(settings(true, false, true), on))).toBe(on);
    }
});
