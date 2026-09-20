// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { snapToStep } from "./spin-box";

// Pins spec/ui.md#ui.spin-box-steps
test("desired retention keeps whole percents", () => {
    // the case Andrew reported: 65.3% showed "65%" and stored 0.653
    expect(snapToStep(0.653, 0.01)).toBe(0.65);
    expect(snapToStep(0.657, 0.01)).toBe(0.66);
    expect(snapToStep(0.9, 0.01)).toBe(0.9);
    expect(snapToStep(0.99, 0.01)).toBe(0.99);
    expect(snapToStep(0.1, 0.01)).toBe(0.1);
});

// Pins spec/ui.md#ui.spin-box-steps
test("a value already on a step does not move", () => {
    for (let percent = 10; percent <= 99; percent++) {
        expect(snapToStep(percent / 100, 0.01)).toBe(Number((percent / 100).toFixed(2)));
    }
});

// Pins spec/ui.md#ui.spin-box-steps
test("whole-number boxes stay whole numbers", () => {
    expect(snapToStep(9.4, 1)).toBe(9);
    expect(snapToStep(9.5, 1)).toBe(10);
    expect(snapToStep(200, 1)).toBe(200);
});

test("a step that cannot round leaves the value alone", () => {
    expect(snapToStep(0.653, 0)).toBe(0.653);
    expect(snapToStep(0.653, -1)).toBe(0.653);
    expect(snapToStep(Number.NaN, 0.01)).toBe(Number.NaN);
});

test("small steps keep their places", () => {
    expect(snapToStep(0.12345, 0.001)).toBe(0.123);
    expect(snapToStep(1.00000005, 1e-7)).toBe(1.0000001);
});
