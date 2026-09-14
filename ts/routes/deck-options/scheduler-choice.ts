// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";

/**
 * The single "Scheduler" choice shown in deck options.
 *
 * It maps onto three stored flags: the collection-wide FSRS switch and the
 * per-preset RWKV-Curve / RWKV-Instant switches. Exactly one scheduler is
 * active at a time, and FSRS is always on: SM-2 is not selectable from this
 * screen (spec/deck-options.md, `deck-options.scheduler-choice`).
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
 * Derive the dropdown value from the stored flags. A preset with both RWKV
 * modes on cannot be represented; it reads as RWKV-Curve, the mode that
 * decides intervals. The FSRS switch does not influence the value: a
 * collection with it off still reads as FSRS, and the screen turns it on.
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

/** The flags a dropdown value writes. FSRS is on for every value. */
export function flagsFromSchedulerChoice(choice: SchedulerChoice): SchedulerFlags {
    return {
        fsrs: true,
        rwkvCurve: choice === SchedulerChoice.RWKV_CURVE,
        rwkvInstant: choice === SchedulerChoice.RWKV_INSTANT,
    };
}

export interface SchedulerChoiceOption {
    label: string;
    value: SchedulerChoice;
}

/** The dropdown entries, in display order. */
export function schedulerChoices(): SchedulerChoiceOption[] {
    return [
        { label: tr.deckConfigSchedulerChoiceFsrs(), value: SchedulerChoice.FSRS },
        {
            label: tr.deckConfigSchedulerChoiceRwkvCurve(),
            value: SchedulerChoice.RWKV_CURVE,
        },
        {
            label: tr.deckConfigSchedulerChoiceRwkvInstant(),
            value: SchedulerChoice.RWKV_INSTANT,
        },
    ];
}
