// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { applyOnScreenTimer, onScreenTimerFromConfig, onScreenTimerPartlyOn, type TimerSettings } from "./timer-switch";

// Pins spec/deck-options.md#deck-options.simple-view (the On-screen timer switch)

function settings(showTimer: boolean, stopTimerOnAnswer: boolean): TimerSettings {
    return { showTimer, stopTimerOnAnswer };
}

test("the switch reads as on when the timer is shown, whatever the stop setting", () => {
    expect(onScreenTimerFromConfig(settings(true, true))).toBe(true);
    expect(onScreenTimerFromConfig(settings(true, false))).toBe(true);
    expect(onScreenTimerFromConfig(settings(false, false))).toBe(false);
    expect(onScreenTimerFromConfig(settings(false, true))).toBe(false);
});

test("turning the switch on or off sets both settings", () => {
    expect(applyOnScreenTimer(settings(false, false), true)).toEqual(settings(true, true));
    expect(applyOnScreenTimer(settings(false, true), true)).toEqual(settings(true, true));
    expect(applyOnScreenTimer(settings(true, true), false)).toEqual(settings(false, false));
    expect(applyOnScreenTimer(settings(true, false), false)).toEqual(settings(false, false));
});

test("a shown timer that does not stop on answer reads as partly on", () => {
    expect(onScreenTimerPartlyOn(settings(true, false))).toBe(true);
    expect(onScreenTimerPartlyOn(settings(true, true))).toBe(false);
    expect(onScreenTimerPartlyOn(settings(false, false))).toBe(false);
    // stopping a timer that is not shown changes nothing
    expect(onScreenTimerPartlyOn(settings(false, true))).toBe(false);
});

test("a preset that already reads as the switch value is left alone", () => {
    const shownNotStopped = settings(true, false);
    expect(applyOnScreenTimer(shownNotStopped, true)).toBe(shownNotStopped);
    expect(shownNotStopped).toEqual(settings(true, false));

    const hiddenButStopping = settings(false, true);
    expect(applyOnScreenTimer(hiddenButStopping, false)).toBe(hiddenButStopping);
    expect(hiddenButStopping).toEqual(settings(false, true));
});
