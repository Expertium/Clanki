// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";

/**
 * The collection's one scheduling algorithm, as deck options show it.
 *
 * It maps onto the per-preset RWKV-Curve / RWKV-Instant switches, which every
 * preset carries as a copy of the collection's algorithm. The algorithm is
 * chosen in Preferences; deck options show it read-only (spec/scheduling.md,
 * `sched.one-global-algorithm`; spec/deck-options.md,
 * `deck-options.scheduler-choice`).
 */
export enum SchedulerChoice {
    FSRS = 0,
    RWKV_CURVE = 1,
    RWKV_INSTANT = 2,
}

export interface SchedulerFlags {
    fsrs: boolean;
    rwkvCurve: boolean;
    rwkvInstant: boolean;
}

/**
 * The algorithm the stored flags select. A preset with both RWKV modes on
 * reads as RWKV-Curve, the mode that decides intervals. The FSRS switch does
 * not influence the value: a collection with it off still reads as FSRS.
 */
export function schedulerChoiceFromFlags(flags: SchedulerFlags): SchedulerChoice {
    if (flags.rwkvCurve) {
        return SchedulerChoice.RWKV_CURVE;
    }
    if (flags.rwkvInstant) {
        return SchedulerChoice.RWKV_INSTANT;
    }
    return SchedulerChoice.FSRS;
}

/** The algorithm's name. */
export function schedulerChoiceLabel(choice: SchedulerChoice): string {
    switch (choice) {
        case SchedulerChoice.RWKV_CURVE:
            return tr.deckConfigSchedulerChoiceRwkvCurve();
        case SchedulerChoice.RWKV_INSTANT:
            return tr.deckConfigSchedulerChoiceRwkvInstant();
        default:
            return tr.deckConfigSchedulerChoiceFsrs();
    }
}
