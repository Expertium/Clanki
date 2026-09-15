// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { FsrsMemoryState } from "@generated/anki/cards_pb";
import { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
import {
    CardStatsResponse,
    CardStatsResponse_CardInfoRow,
    CardStatsResponse_RwkvCurve,
} from "@generated/anki/stats_pb";
import * as tr2 from "@generated/ftl";
import { timeSpan } from "@tslib/time";
import { expect, test } from "vitest";

import { rowsFromStats, showsForgettingCurve } from "./lib";

function baseStats(overrides?: Partial<CardStatsResponse>): CardStatsResponse {
    return new CardStatsResponse({
        cardId: 1n,
        noteId: 2n,
        deck: "Default",
        added: 1n,
        ease: 2500,
        reviews: 0,
        lapses: 0,
        averageSecs: 0,
        totalSecs: 0,
        cardType: "Card 1",
        notetype: "Basic",
        customData: "",
        preset: "Default",
        advancedUi: true,
        ...overrides,
    });
}

test("shows ease when desiredRetention is undefined", () => {
    const rows = rowsFromStats(baseStats({ desiredRetention: undefined }));

    expect(rows).toContainEqual({
        label: tr2.cardStatsEase(),
        value: "250%",
    });
    expect(rows.find((row) => row.label === tr2.cardStatsFsrsStability())).toBeUndefined();
    expect(rows.find((row) => row.label === tr2.cardStatsFsrsDifficulty())).toBeUndefined();
});

test("hides ease when desiredRetention is provided", () => {
    const rows = rowsFromStats(baseStats({ desiredRetention: 0.9 }));

    expect(rows.find((row) => row.label === tr2.cardStatsEase())).toBeUndefined();
    expect(rows.find((row) => row.label === tr2.cardStatsFsrsStability())).toBeUndefined();
    expect(rows.find((row) => row.label === tr2.cardStatsFsrsDifficulty())).toBeUndefined();
});

test("with memoryState and undefined desiredRetention, shows FSRS rows and hides ease", () => {
    const rows = rowsFromStats(
        baseStats({
            desiredRetention: undefined,
            memoryState: new FsrsMemoryState({ stability: 15, difficulty: 5.5 }),
        }),
    );

    expect(rows.find((row) => row.label === tr2.cardStatsFsrsStability())).toBeDefined();
    expect(rows).toContainEqual({
        label: tr2.cardStatsFsrsDifficulty(),
        value: "50%",
    });
    expect(rows.find((row) => row.label === tr2.cardStatsEase())).toBeUndefined();
});

test("with memoryState and desiredRetention, shows FSRS rows and hides ease", () => {
    const rows = rowsFromStats(
        baseStats({
            desiredRetention: 0.9,
            memoryState: new FsrsMemoryState({ stability: 20, difficulty: 7.3 }),
        }),
    );

    expect(rows.find((row) => row.label === tr2.cardStatsFsrsStability())).toBeDefined();
    expect(rows).toContainEqual({
        label: tr2.cardStatsFsrsDifficulty(),
        value: "70%",
    });
    expect(rows.find((row) => row.label === tr2.cardStatsEase())).toBeUndefined();
});

// Pins spec/ui.md#ui.card-info-one-algorithm
const fsrs7State = new FsrsMemoryState({ stability: 20, difficulty: 7.3 });

function labelsOf(rows: { label: string }[]): string[] {
    return rows.map((row) => row.label);
}

test("FSRS-7 shows stability, difficulty and one retrievability", () => {
    const rows = rowsFromStats(
        baseStats({ desiredRetention: 0.9, fsrsRetrievability: 0.8, memoryState: fsrs7State }),
    );

    expect(rows).toContainEqual({ label: tr2.cardStatsFsrsRetrievability(), value: "80%" });
    const labels = labelsOf(rows);
    expect(labels).toContain(tr2.cardStatsFsrsStability());
    expect(labels).toContain(tr2.cardStatsFsrsDifficulty());
    expect(labels.filter((label) => label === tr2.cardStatsFsrsRetrievability())).toHaveLength(1);
});

test("Simple mode shows no difficulty, stability or retrievability", () => {
    for (const schedulingAlgorithm of [SchedulingAlgorithm.FSRS7, SchedulingAlgorithm.RWKV_INSTANT]) {
        const labels = labelsOf(
            rowsFromStats(
                baseStats({
                    advancedUi: false,
                    schedulingAlgorithm,
                    desiredRetention: 0.9,
                    fsrsRetrievability: 0.8,
                    memoryState: fsrs7State,
                    extraRows: [new CardStatsResponse_CardInfoRow({ label: "RWKV computed R", value: "79%" })],
                }),
            ),
        );
        for (
            const hidden of [
                tr2.cardStatsFsrsStability(),
                tr2.cardStatsFsrsDifficulty(),
                tr2.cardStatsFsrsRetrievability(),
                "RWKV computed R",
            ]
        ) {
            expect(labels).not.toContain(hidden);
        }
    }
});

function rwkvCurveStats(curve: Partial<CardStatsResponse_RwkvCurve>): CardStatsResponse {
    return baseStats({
        schedulingAlgorithm: SchedulingAlgorithm.RWKV_CURVE,
        desiredRetention: 0.9,
        fsrsRetrievability: 0.8,
        memoryState: fsrs7State,
        rwkvCurve: new CardStatsResponse_RwkvCurve(curve),
    });
}

test("an RWKV-Curve card shows its curve's S90 and R, and no difficulty", () => {
    // also spec ui.card-info-rwkv-curve
    const rows = rowsFromStats(
        rwkvCurveStats({ elapsedDays: [0, 1], recall: [1, 0.9], s90: 1.5, currentRecall: 0.93 }),
    );

    expect(rows).toContainEqual({
        label: tr2.cardStatsFsrsStability(),
        value: timeSpan(1.5 * 86400, false, false),
    });
    // the curve's R, not FSRS-7's 80%
    expect(rows).toContainEqual({ label: tr2.cardStatsFsrsRetrievability(), value: "93%" });
    expect(labelsOf(rows)).not.toContain(tr2.cardStatsFsrsDifficulty());
});

test("an RWKV-Curve card without a curve shows no stability and a calculating R", () => {
    const rows = rowsFromStats(rwkvCurveStats({}));

    expect(labelsOf(rows)).not.toContain(tr2.cardStatsFsrsStability());
    expect(rows).toContainEqual({
        label: tr2.cardStatsFsrsRetrievability(),
        value: tr2.cardStatsCalculating(),
    });
});

test("an RWKV-Instant card shows only RWKV's R, once, and no forgetting curve", () => {
    const stats = baseStats({
        schedulingAlgorithm: SchedulingAlgorithm.RWKV_INSTANT,
        desiredRetention: 0.9,
        fsrsRetrievability: 0.8,
        memoryState: fsrs7State,
        extraRows: [
            new CardStatsResponse_CardInfoRow({ label: "Other", value: "kept" }),
            new CardStatsResponse_CardInfoRow({ label: "RWKV computed R", value: "79%" }),
        ],
    });
    const rows = rowsFromStats(stats);

    expect(rows).toContainEqual({ label: tr2.cardStatsFsrsRetrievability(), value: "79%" });
    expect(rows).toContainEqual({ label: "Other", value: "kept" });
    const labels = labelsOf(rows);
    expect(labels).not.toContain("RWKV computed R");
    expect(labels).not.toContain(tr2.cardStatsFsrsStability());
    expect(labels).not.toContain(tr2.cardStatsFsrsDifficulty());
    expect(showsForgettingCurve(stats)).toBe(false);
    expect(showsForgettingCurve(baseStats({ memoryState: fsrs7State }))).toBe(true);

    const pending = rowsFromStats(
        baseStats({ schedulingAlgorithm: SchedulingAlgorithm.RWKV_INSTANT, memoryState: fsrs7State }),
    );
    expect(pending).toContainEqual({
        label: tr2.cardStatsFsrsRetrievability(),
        value: tr2.cardStatsCalculating(),
    });
});