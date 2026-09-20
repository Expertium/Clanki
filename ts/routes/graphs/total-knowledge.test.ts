// @vitest-environment jsdom
// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
import {
    TotalKnowledgeResponse,
    TotalKnowledgeRwkvProgress,
    TotalKnowledgeRwkvProgress_State as RwkvState,
} from "@generated/anki/stats_pb";
import * as tr from "@generated/ftl";
import { expect, test } from "vitest";

import type { GraphBounds } from "./graph-helpers";
import {
    algorithmName,
    dayToDate,
    hasDrawing,
    KNOWN_COLOUR,
    knownCardsShown,
    overlayText,
    renderTotalKnowledge,
    REVIEWED_COLOUR,
    rwkvStillComputing,
    showsReviewed,
    subtitleText,
    tooltipText,
    totalKnowledgeData,
} from "./total-knowledge";

function response(algorithm: SchedulingAlgorithm, sumR: number[] = []): TotalKnowledgeResponse {
    return new TotalKnowledgeResponse({
        algorithm,
        firstDay: -4,
        reviewedCards: [1, 1, 2, 2, 3],
        sumR,
    });
}

function progress(state: RwkvState, firstDay: number, sumR: number[]): TotalKnowledgeRwkvProgress {
    return new TotalKnowledgeRwkvProgress({ state, jobId: 7, firstDay, sumR });
}

// Pins spec/ui.md#ui.stats-total-knowledge
test("FSRS-7 draws every day at once", () => {
    const data = totalKnowledgeData(
        response(SchedulingAlgorithm.FSRS7, [1, 0.9, 1.8, 1.7, 2.5]),
        null,
    );
    expect(data.computedThroughDay).toBeNull();
    expect(data.points).toEqual([
        { day: -4, reviewed: 1, known: 1 },
        { day: -3, reviewed: 1, known: 0.9 },
        { day: -2, reviewed: 2, known: 1.8 },
        { day: -1, reviewed: 2, known: 1.7 },
        { day: 0, reviewed: 3, known: 2.5 },
    ]);
});

// Pins spec/ui.md#ui.stats-total-knowledge
test("under RWKV the days its job has not reached have no sum, behind the sweep", () => {
    const rwkv = response(SchedulingAlgorithm.RWKV_CURVE);
    // before the job's first day: loading, everything pending
    let data = totalKnowledgeData(rwkv, progress(RwkvState.COMPUTING, 0, []));
    expect(data.points.every((point) => point.known === null)).toBe(true);
    expect(data.computedThroughDay).toBe(-5);
    // no FSRS-7 values in place of RWKV's
    data = totalKnowledgeData(
        new TotalKnowledgeResponse({ ...rwkv, sumR: [9, 9, 9, 9, 9] }),
        null,
    );
    expect(data.points.every((point) => point.known === null)).toBe(true);

    // RWKV's history starts a day earlier; two days done
    data = totalKnowledgeData(rwkv, progress(RwkvState.COMPUTING, -5, [0, 1, 0.9]));
    expect(data.points.map((point) => point.known)).toEqual([1, 0.9, null, null, null]);
    expect(data.computedThroughDay).toBe(-3);
    expect(rwkvStillComputing(progress(RwkvState.COMPUTING, -5, [0]))).toBe(true);

    // a day before RWKV's history counts 0
    data = totalKnowledgeData(rwkv, progress(RwkvState.COMPUTING, -2, [2]));
    expect(data.points.map((point) => point.known)).toEqual([0, 0, 2, null, null]);

    // done: every day drawn sharp
    data = totalKnowledgeData(
        rwkv,
        progress(RwkvState.DONE, -4, [1, 0.9, 1.8, 1.7, 2.5]),
    );
    expect(data.computedThroughDay).toBeNull();
    expect(data.points.map((point) => point.known)).toEqual([1, 0.9, 1.8, 1.7, 2.5]);
    expect(rwkvStillComputing(progress(RwkvState.DONE, -4, []))).toBe(false);
});

// Pins spec/ui.md#ui.stats-total-knowledge
test("the graph says what it waits for, or why it draws nothing", () => {
    const rwkv = response(SchedulingAlgorithm.RWKV_INSTANT);
    // FSRS-7's replay (or the bound) not there yet
    expect(overlayText(null, null)).toBe(tr.cardStatsCalculating());
    expect(hasDrawing(null, null)).toBe(false);
    // no RWKV model: an error, not a wait (spec sched.rwkv-no-model-error)
    const noModel = progress(RwkvState.NO_MODEL, 0, []);
    expect(overlayText(rwkv, noModel)).toBe(tr.statisticsTotalKnowledgeRwkvModelNotFound());
    expect(hasDrawing(rwkv, noModel)).toBe(false);
    // computing: the bound shows
    expect(overlayText(rwkv, progress(RwkvState.COMPUTING, 0, []))).toBeUndefined();
    expect(hasDrawing(rwkv, progress(RwkvState.COMPUTING, 0, []))).toBe(true);
    // nothing reviewed: "No data"
    const empty = new TotalKnowledgeResponse({ algorithm: SchedulingAlgorithm.FSRS7 });
    expect(overlayText(empty, null)).toBeUndefined();
    expect(hasDrawing(empty, null)).toBe(false);
});

test("days are relative to today", () => {
    const now = new Date(2026, 8, 15, 12).getTime();
    expect(dayToDate(0, now).getTime()).toBe(now);
    expect(dayToDate(-3, now).getTime()).toBe(now - 3 * 86_400_000);
});

test("the tooltip gives the day's date, known and reviewed cards", () => {
    const date = new Date(2026, 8, 15, 12);
    const lines = tooltipText({ day: 0, reviewed: 3, known: 2.46 }, date).split("<br>");
    expect(lines).toHaveLength(3);
    expect(lines[1]).toContain(tr.statisticsTotalKnowledgeKnownCards({ cards: 2 }));
    expect(lines[2]).toContain(tr.statisticsTotalKnowledgeReviewedCards({ cards: 3 }));
    expect(tooltipText({ day: 0, reviewed: 1, known: null }, date)).toContain(
        tr.cardStatsCalculating(),
    );
});

// Pins spec/ui.md#ui.stats-total-knowledge: the tooltip names a whole number
// of cards; the sum itself keeps every decimal.
test("the tooltip shows a whole number of known cards", () => {
    expect(knownCardsShown(4555.8)).toBe(4556);
    expect(knownCardsShown(2.46)).toBe(2);
    expect(knownCardsShown(0.5)).toBe(1);
    expect(knownCardsShown(0)).toBe(0);
});

// Pins spec/ui.md#ui.stats-total-knowledge: Simple mode draws Known only;
// Advanced mode has the checkbox for Reviewed, on by default.
test("Simple mode hides Reviewed; the Advanced checkbox shows and hides it", () => {
    // Simple mode: hidden, whatever the checkbox last held
    expect(showsReviewed(false, true)).toBe(false);
    expect(showsReviewed(false, false)).toBe(false);
    // Advanced mode: the checkbox decides, and it starts checked
    expect(showsReviewed(true, true)).toBe(true);
    expect(showsReviewed(true, false)).toBe(false);
});

// Pins spec/ui.md#ui.stats-total-knowledge and #ui.simple-recall-wording: the
// plain wording has a subtitle of its own, without the word "retrievability".
test("the plain wording has a subtitle of its own", () => {
    expect(subtitleText(false)).toBe(tr.statisticsTotalKnowledgeSubtitle());
    expect(subtitleText(true)).toBe(tr.statisticsTotalKnowledgeSubtitlePlain());
    expect(subtitleText(false)).not.toBe(subtitleText(true));
});

// Pins spec/ui.md#ui.stats-total-knowledge
test("the tooltip drops the Reviewed row when the graph does not draw it", () => {
    const date = new Date(2026, 8, 15, 12);
    const point = { day: 0, reviewed: 3, known: 2.46 };
    const lines = tooltipText(point, date, false).split("<br>");
    expect(lines).toHaveLength(2);
    expect(lines[1]).toContain(tr.statisticsTotalKnowledgeKnownCards({ cards: 2.5 }));
    expect(tooltipText(point, date, false)).not.toContain(
        tr.statisticsTotalKnowledgeReviewedCards({ cards: 3 }),
    );
});

function makeSvg(): SVGElement {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const name of ["total-knowledge", "x-ticks", "y-ticks", "no-data"]) {
        svg.appendChild(
            document.createElementNS("http://www.w3.org/2000/svg", "g"),
        ).setAttribute("class", name);
    }
    return svg;
}

const bounds: GraphBounds = {
    width: 600,
    height: 250,
    marginLeft: 70,
    marginRight: 70,
    marginTop: 20,
    marginBottom: 25,
};

function strokes(svg: SVGElement, colour: string): number {
    return Array.from(svg.querySelectorAll("path")).filter(
        (path) => path.getAttribute("stroke") === colour,
    ).length;
}

// Pins spec/ui.md#ui.stats-total-knowledge: with the bound hidden, only the
// Known line is drawn; the sweep line stays while RWKV computes.
test("the Reviewed line is drawn only when the graph shows it", () => {
    const data = totalKnowledgeData(
        response(SchedulingAlgorithm.FSRS7, [1, 0.9, 1.8, 1.7, 2.5]),
        null,
    );
    let svg = makeSvg();
    renderTotalKnowledge(svg, bounds, data, true);
    expect(strokes(svg, REVIEWED_COLOUR)).toBe(1);
    expect(strokes(svg, KNOWN_COLOUR)).toBe(1);

    svg = makeSvg();
    renderTotalKnowledge(svg, bounds, data, false);
    expect(strokes(svg, REVIEWED_COLOUR)).toBe(0);
    expect(strokes(svg, KNOWN_COLOUR)).toBe(1);

    // RWKV still computing: the blurred bound goes with the line, the sweep stays
    const computing = totalKnowledgeData(
        response(SchedulingAlgorithm.RWKV_CURVE),
        progress(RwkvState.COMPUTING, -4, [1, 0.9]),
    );
    svg = makeSvg();
    renderTotalKnowledge(svg, bounds, computing, true);
    expect(strokes(svg, REVIEWED_COLOUR)).toBe(2);
    expect(svg.querySelectorAll(".total-knowledge-sweep")).toHaveLength(1);

    svg = makeSvg();
    renderTotalKnowledge(svg, bounds, computing, false);
    expect(strokes(svg, REVIEWED_COLOUR)).toBe(0);
    expect(svg.querySelectorAll(".total-knowledge-sweep")).toHaveLength(1);
});

// Pins spec/ui.md#ui.stats-total-knowledge: Advanced mode names the
// collection's algorithm, and only that one (spec ui.stats-one-algorithm).
test("the algorithm is named as the deck-options list names it", () => {
    expect(algorithmName(SchedulingAlgorithm.FSRS7)).toBe(
        tr.deckConfigSchedulerChoiceFsrs(),
    );
    expect(algorithmName(SchedulingAlgorithm.RWKV_CURVE)).toBe(
        tr.deckConfigSchedulerChoiceRwkvCurve(),
    );
    expect(algorithmName(SchedulingAlgorithm.RWKV_INSTANT)).toBe(
        tr.deckConfigSchedulerChoiceRwkvInstant(),
    );
});
