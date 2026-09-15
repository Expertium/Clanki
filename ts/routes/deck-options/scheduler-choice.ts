// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
import * as tr from "@generated/ftl";

/**
 * The collection's one scheduling algorithm, chosen in the deck-options
 * Algorithm list. Every preset carries it as its RWKV-Curve / RWKV-Instant
 * switches, and FSRS stays on under every algorithm (spec/scheduling.md,
 * `sched.one-global-algorithm`; spec/deck-options.md,
 * `deck-options.scheduler-choice`).
 */
export { SchedulingAlgorithm };

export interface SchedulerFlags {
    rwkvCurve: boolean;
    rwkvInstant: boolean;
}

/** The preset switches an algorithm writes. */
export function flagsFromSchedulerChoice(algorithm: SchedulingAlgorithm): SchedulerFlags {
    return {
        rwkvCurve: algorithm === SchedulingAlgorithm.RWKV_CURVE,
        rwkvInstant: algorithm === SchedulingAlgorithm.RWKV_INSTANT,
    };
}

export interface SchedulerChoiceOption {
    label: string;
    value: SchedulingAlgorithm;
    /** Shown under the label in the open dropdown. */
    description: string;
}

/** The dropdown entries, in display order. */
export function schedulerChoices(): SchedulerChoiceOption[] {
    return [
        {
            label: tr.deckConfigSchedulerChoiceFsrs(),
            value: SchedulingAlgorithm.FSRS7,
            description: tr.deckConfigSchedulerChoiceFsrsDescription(),
        },
        {
            label: tr.deckConfigSchedulerChoiceRwkvCurve(),
            value: SchedulingAlgorithm.RWKV_CURVE,
            description: tr.deckConfigSchedulerChoiceRwkvCurveDescription(),
        },
        {
            label: tr.deckConfigSchedulerChoiceRwkvInstant(),
            value: SchedulingAlgorithm.RWKV_INSTANT,
            description: tr.deckConfigSchedulerChoiceRwkvInstantDescription(),
        },
    ];
}
