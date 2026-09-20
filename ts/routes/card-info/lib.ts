// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
import type { CardStatsResponse } from "@generated/anki/stats_pb";
import * as tr2 from "@generated/ftl";
import { plainRecallWording } from "@tslib/recall-wording";
import { DAY, timeSpan, TimespanUnit, Timestamp } from "@tslib/time";

function dateString(timestamp: bigint): string {
    return new Timestamp(Number(timestamp)).dateString();
}

export interface StatsRow {
    label: string;
    value: string | number | bigint;
}

// The row that carries an RWKV-Instant card's R (rwkv_scheduler.py
// RWKV_CARD_INFO_R_LABEL); it shows as the card's Retrievability.
const rwkvRLabel = "RWKV computed R";

function percent(value: number): string {
    return `${(value * 100).toFixed(0)}%`;
}

function stabilityRow(days: number): StatsRow {
    let stability = timeSpan(days * 86400, false, false);
    if (days > 31) {
        stability += ` (${timeSpan(days * 86400, false, false, TimespanUnit.Days)})`;
    }
    return { label: tr2.cardStatsFsrsStability(), value: stability };
}

/**
 * Stability, difficulty and one retrievability, from the collection's
 * algorithm only (spec ui.card-info-one-algorithm): FSRS-7 shows all three;
 * RWKV-Curve its curve's S90 (no row while RWKV has no curve,
 * ui.card-info-rwkv-curve) and its curve's R now; RWKV-Instant only RWKV's
 * R. RWKV has no difficulty and RWKV-Instant no stability.
 */
function memoryStateRows(stats: CardStatsResponse, rwkvR: string | undefined): StatsRow[] {
    // the plain wording never says "retrievability" (spec
    // ui.simple-recall-wording)
    const label = plainRecallWording(stats.recallWording, stats.advancedUi)
        ? tr2.cardStatsFsrsRetrievabilityPlain()
        : tr2.cardStatsFsrsRetrievability();
    const retrievability = (value: string): StatsRow => ({
        label,
        value,
    });
    switch (stats.schedulingAlgorithm) {
        case SchedulingAlgorithm.RWKV_CURVE: {
            const curve = stats.rwkvCurve;
            const rows = curve?.s90 !== undefined ? [stabilityRow(curve.s90)] : [];
            rows.push(
                retrievability(
                    curve?.currentRecall !== undefined
                        ? percent(curve.currentRecall)
                        : tr2.cardStatsCalculating(),
                ),
            );
            return rows;
        }
        case SchedulingAlgorithm.RWKV_INSTANT:
            return [retrievability(rwkvR ?? tr2.cardStatsCalculating())];
        default: {
            const state = stats.memoryState!;
            const rows = [
                stabilityRow(state.stability),
                {
                    label: tr2.cardStatsFsrsDifficulty(),
                    value: percent((state.difficulty - 1.0) / 9.0),
                },
            ];
            if (stats.fsrsRetrievability != null) {
                rows.push(retrievability(percent(stats.fsrsRetrievability)));
            }
            return rows;
        }
    }
}

/**
 * RWKV-Instant has no forgetting curve of its own, so card info draws none
 * rather than FSRS-7's (spec ui.card-info-one-algorithm).
 */
export function showsForgettingCurve(stats: CardStatsResponse): boolean {
    return stats.memoryState != null
        && stats.schedulingAlgorithm !== SchedulingAlgorithm.RWKV_INSTANT;
}

export function rowsFromStats(stats: CardStatsResponse): StatsRow[] {
    const statsRows: StatsRow[] = [];
    // an RWKV-Instant card's R; it shows as the card's retrievability
    const rwkvRRow = stats.extraRows.find((row) => row.label === rwkvRLabel);

    statsRows.push({ label: tr2.cardStatsAdded(), value: dateString(stats.added) });

    if (stats.firstReview != null) {
        statsRows.push({
            label: tr2.cardStatsFirstReview(),
            value: dateString(stats.firstReview),
        });
    }
    if (stats.latestReview != null) {
        statsRows.push({
            label: tr2.cardStatsLatestReview(),
            value: dateString(stats.latestReview),
        });
    }

    if (stats.dueDate != null) {
        statsRows.push({
            label: tr2.statisticsDueDate(),
            value: dateString(stats.dueDate),
        });
    }
    if (stats.duePosition != null) {
        statsRows.push({
            label: tr2.cardStatsNewCardPosition(),
            value: stats.duePosition,
        });
    }

    if (stats.interval) {
        statsRows.push({
            label: tr2.cardStatsInterval(),
            value: timeSpan(stats.interval * DAY),
        });
    }
    if (stats.memoryState) {
        // Simple mode shows no difficulty, stability or retrievability
        if (stats.advancedUi) {
            statsRows.push(...memoryStateRows(stats, rwkvRRow?.value));
        }
    } else if (stats.ease && stats.desiredRetention === undefined) {
        // Prevent showing ease when FSRS is enabled but no memory state exists.
        statsRows.push({
            label: tr2.cardStatsEase(),
            value: `${stats.ease / 10}%`,
        });
    }

    statsRows.push({ label: tr2.cardStatsReviewCount(), value: stats.reviews });
    statsRows.push({ label: tr2.cardStatsLapseCount(), value: stats.lapses });

    if (stats.totalSecs) {
        statsRows.push({
            label: tr2.cardStatsAverageTime(),
            value: timeSpan(stats.averageSecs),
        });
        statsRows.push({
            label: tr2.cardStatsTotalTime(),
            value: timeSpan(stats.totalSecs),
        });
    }

    statsRows.push({ label: tr2.cardStatsCardTemplate(), value: stats.cardType });
    statsRows.push({ label: tr2.cardStatsNoteType(), value: stats.notetype });
    let deck: string;
    if (stats.originalDeck) {
        deck = `${stats.deck} (${stats.originalDeck})`;
    } else {
        deck = stats.deck;
    }
    statsRows.push({ label: tr2.cardStatsDeckName(), value: deck });
    statsRows.push({ label: tr2.cardStatsPreset(), value: stats.preset });

    for (const row of stats.extraRows) {
        if (row !== rwkvRRow) {
            statsRows.push(row);
        }
    }

    statsRows.push({ label: tr2.cardStatsCardId(), value: stats.cardId });
    statsRows.push({ label: tr2.cardStatsNoteId(), value: stats.noteId });

    if (stats.customData) {
        let value: string;
        try {
            const obj = JSON.parse(stats.customData);
            value = Object.entries(obj)
                .map(([k, v]) => `${k}=${v}`)
                .join(" ");
        } catch (exc) {
            value = stats.customData;
        }
        statsRows.push({
            label: tr2.cardStatsCustomData(),
            value: value,
        });
    }

    return statsRows;
}
