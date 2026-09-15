// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { FsrsMemoryState } from "@generated/anki/cards_pb";
import {
    CardStatsResponse,
    CardStatsResponse_CardInfoRow,
    CardStatsResponse_RwkvCurve,
} from "@generated/anki/stats_pb";
import * as tr2 from "@generated/ftl";
import { timeSpan } from "@tslib/time";
import { expect, test } from "vitest";

import { rowsFromStats } from "./lib";

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

test("keeps RWKV comparison rows together after FSRS retrievability", () => {
    const rows = rowsFromStats(
        baseStats({
            desiredRetention: 0.9,
            fsrsRetrievability: 0.8,
            memoryState: new FsrsMemoryState({ stability: 20, difficulty: 7.3 }),
            extraRows: [
                new CardStatsResponse_CardInfoRow({ label: "Other", value: "last" }),
                new CardStatsResponse_CardInfoRow({
                    label: "Retrievability source",
                    value: "RWKV",
                }),
                new CardStatsResponse_CardInfoRow({ label: "RWKV computed R", value: "79%" }),
                new CardStatsResponse_CardInfoRow({
                    label: "RWKV : Answer Button Probability",
                    value: "Again 5%",
                }),
            ],
        }),
    );

    const labels = rows.map((row) => row.label);
    const fsrsIndex = labels.indexOf(tr2.cardStatsFsrsComputedR());
    expect(labels.slice(fsrsIndex, fsrsIndex + 4)).toEqual([
        tr2.cardStatsFsrsComputedR(),
        "RWKV computed R",
        "RWKV : Answer Button Probability",
        "Retrievability source",
    ]);
    expect(labels.indexOf("Other")).toBeGreaterThan(labels.indexOf("Retrievability source"));
});

function rwkvCurveStats(s90?: number): CardStatsResponse {
    return baseStats({
        desiredRetention: 0.9,
        fsrsRetrievability: 0.8,
        memoryState: new FsrsMemoryState({ stability: 20, difficulty: 7.3 }),
        rwkvCurve: new CardStatsResponse_RwkvCurve(
            s90 === undefined ? {} : { elapsedDays: [0, 1], recall: [1, 0.9], s90 },
        ),
        extraRows: [
            new CardStatsResponse_CardInfoRow({ label: "RWKV computed R", value: "79%" }),
            new CardStatsResponse_CardInfoRow({ label: "Retrievability source", value: "RWKV" }),
        ],
    });
}

test("an RWKV-Curve card shows its curve's S90 and no FSRS-7 difficulty or retrievability", () => {
    // spec ui.card-info-rwkv-curve
    const rows = rowsFromStats(rwkvCurveStats(1.5));

    const labels = rows.map((row) => row.label);
    const stabilityIndex = labels.indexOf(tr2.cardStatsFsrsStability());
    expect(rows[stabilityIndex].value).toBe(timeSpan(1.5 * 86400, false, false));
    expect(labels.slice(stabilityIndex + 1, stabilityIndex + 3)).toEqual([
        "RWKV computed R",
        "Retrievability source",
    ]);
    expect(labels).not.toContain(tr2.cardStatsFsrsDifficulty());
    expect(labels).not.toContain(tr2.cardStatsFsrsComputedR());
});

test("an RWKV-Curve card without a curve shows no stability", () => {
    const labels = rowsFromStats(rwkvCurveStats()).map((row) => row.label);

    expect(labels).not.toContain(tr2.cardStatsFsrsStability());
    expect(labels).not.toContain(tr2.cardStatsFsrsDifficulty());
    expect(labels).toContain("RWKV computed R");
});
