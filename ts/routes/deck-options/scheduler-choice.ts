// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";

/**
 * The single "Scheduler" choice shown in deck options.
 *
 * It maps onto three stored flags: the collection-wide FSRS switch and the
 * per-preset RWKV-Curve / RWKV-Instant switches. Exactly one scheduler is
 * active at a time (spec/deck-options.md, `deck-options.scheduler-choice`).
 */
export enum SchedulerChoice {
    FSRS = 0,
    RWKV_CURVE = 1,
    RWKV_INSTANT = 2,
    /** FSRS off. Only offered behind "Show advanced options". */
    SM2 = 3,
}

export interface SchedulerFlags {
    fsrs: boolean;
    rwkvCurve: boolean;
    rwkvInstant: boolean;
}

/**
 * Derive the dropdown value from the stored flags. A preset with both RWKV
 * modes on cannot be represented; it reads as RWKV-Curve, the mode that
 * decides intervals.
 */
export function schedulerChoiceFromFlags(flags: SchedulerFlags): SchedulerChoice {
    if (flags.rwkvCurve) {
        return SchedulerChoice.RWKV_CURVE;
    }
    if (flags.rwkvInstant) {
        return SchedulerChoice.RWKV_INSTANT;
    }
    return flags.fsrs ? SchedulerChoice.FSRS : SchedulerChoice.SM2;
}

/** The flags a dropdown value writes. Every RWKV mode requires FSRS on. */
export function flagsFromSchedulerChoice(choice: SchedulerChoice): SchedulerFlags {
    return {
        fsrs: choice !== SchedulerChoice.SM2,
        rwkvCurve: choice === SchedulerChoice.RWKV_CURVE,
        rwkvInstant: choice === SchedulerChoice.RWKV_INSTANT,
    };
}

export interface SchedulerChoiceOption {
    label: string;
    value: SchedulerChoice;
}

/**
 * The dropdown entries. SM-2 is listed only when advanced options are shown,
 * or when it is the current value so the selection stays representable.
 */
export function schedulerChoices(options: {
    advanced: boolean;
    current: SchedulerChoice;
}): SchedulerChoiceOption[] {
    const choices: SchedulerChoiceOption[] = [
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
    if (options.advanced || options.current === SchedulerChoice.SM2) {
        choices.push({
            label: tr.deckConfigSchedulerChoiceSm2(),
            value: SchedulerChoice.SM2,
        });
    }
    return choices;
}
