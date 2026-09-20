// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * Round a value to the nearest step of a spin box (spec ui.spin-box-steps).
 *
 * A spin box shows as many decimal places as its step has, so a value between
 * two steps is displayed as one of them while a different number is stored.
 * Desired retention is the case that matters: the box steps by 1%, and 65.3%
 * used to show "65%" and save 0.653.
 *
 * The rounding is done on the value divided by the step, and the result is
 * cleaned of the error that binary floating point leaves behind, so that
 * 0.653 with a step of 0.01 gives exactly 0.65 and not 0.6500000000000001.
 */
export function snapToStep(value: number, step: number): number {
    if (!Number.isFinite(value) || !Number.isFinite(step) || step <= 0) {
        return value;
    }
    const steps = Math.round(value / step);
    // as many decimal places as the step has, and never more than a double
    // can state exactly
    const places = Math.min(decimalPlacesOf(step), 15);
    return Number((steps * step).toFixed(places));
}

function decimalPlacesOf(step: number): number {
    const text = step.toString();
    if (text.includes("e") || text.includes("E")) {
        // an exponent, such as 1e-7: everything after the point
        return Math.max(0, -Math.floor(Math.log10(step)));
    }
    return text.split(".")[1]?.length ?? 0;
}
