// @vitest-environment jsdom
// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test, vi } from "vitest";

import { defaultGraphBounds } from "../graphs/graph-helpers";
import { chartCurve, chartRevlog, renderForgettingCurve, TimeRange } from "./forgetting-curve";

function twoReviews(): any {
    // newest first, as card info sends them
    return [
        {
            time: Date.parse("2024-01-11T00:00:00Z") / 1000,
            reviewKind: 1,
            buttonChosen: 3,
            ease: 2500,
            interval: 20 * 86400,
            memoryState: { stability: 30, stabilityInternal: 20, difficulty: 5 },
        },
        {
            time: Date.parse("2024-01-01T00:00:00Z") / 1000,
            reviewKind: 0,
            buttonChosen: 3,
            ease: 2500,
            interval: 10 * 86400,
            memoryState: { stability: 12, stabilityInternal: 10, difficulty: 5 },
        },
    ];
}

function fsrs7Curves(): any {
    return {
        elapsedDays: [0, 10, 100],
        segments: [
            { reviewTime: BigInt(twoReviews()[0].time), recall: [1, 0.5, 0.1] },
            { reviewTime: BigInt(twoReviews()[1].time), recall: [1, 0.8, 0.3] },
        ],
    };
}

function svgWithNoDataOverlay(): SVGElement {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const name of ["no-data", "x-ticks", "y-ticks"]) {
        const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
        group.setAttribute("class", name);
        svg.appendChild(group);
    }
    return svg;
}

// The chart draws the curve the backend sent (spec sched.fsrs-rs-latest), and
// nothing without one.
test("the forgetting curve draws the backend's FSRS-7 curve, and nothing without it", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2024-01-16T00:00:00Z"));
    try {
        const revlog = chartRevlog(twoReviews());
        const svg = svgWithNoDataOverlay();
        renderForgettingCurve(
            revlog,
            TimeRange.AllTime,
            svg,
            defaultGraphBounds(),
            0.9,
            chartCurve(revlog, undefined, fsrs7Curves()),
        );
        // the past as a solid line, the preview as a dashed one
        const lines = svg.querySelectorAll(".forgetting-curve-line");
        expect(lines).toHaveLength(2);
        expect(lines[0].getAttribute("d")).toBeTruthy();
        expect(lines[1].getAttribute("stroke-dasharray")).toBe("4 4");
        expect(svg.querySelectorAll(".hover-columns rect").length).toBeGreaterThan(0);
        expect(svg.querySelector(".desired-retention-line")).not.toBeNull();

        // without the backend's curve: no line
        const empty = svgWithNoDataOverlay();
        renderForgettingCurve(
            revlog,
            TimeRange.AllTime,
            empty,
            defaultGraphBounds(),
            0.9,
            chartCurve(revlog, undefined, undefined),
        );
        expect(empty.querySelectorAll(".forgetting-curve-line")).toHaveLength(0);
    } finally {
        vi.useRealTimers();
    }
});
