// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";
import { HelpPage } from "@tslib/help-page";

import { type HelpItem, HelpItemScheduler } from "$lib/components/types";

export type AlgorithmHelpKey =
    | "fsrs"
    | "desiredRetention"
    | "modelParams"
    | "rescheduleCardsOnChange"
    | "healthCheck";

/**
 * Help entries for the Algorithm block. The Advanced-mode section and the
 * Simple-mode page both host the block, so both need these for their help
 * modal; the block opens them by key.
 */
export function algorithmHelpSettings(): Record<AlgorithmHelpKey, HelpItem> {
    return {
        fsrs: {
            title: tr.deckConfigScheduler(),
            help: tr.deckConfigSchedulerTooltip(),
            url: HelpPage.DeckOptions.fsrs,
            global: true,
        },
        desiredRetention: {
            title: tr.deckConfigDesiredRetention(),
            help: tr.deckConfigDesiredRetentionTooltip()
                + "\n\n"
                + tr.deckConfigDesiredRetentionTooltip2(),
            sched: HelpItemScheduler.FSRS,
        },
        modelParams: {
            title: tr.deckConfigWeights(),
            help: tr.deckConfigWeightsTooltip2()
                + "\n\n"
                + tr.deckConfigComputeOptimalWeightsTooltip2(),
            sched: HelpItemScheduler.FSRS,
        },
        rescheduleCardsOnChange: {
            title: tr.deckConfigRescheduleCardsOnChange(),
            help: tr.deckConfigRescheduleCardsOnChangeTooltip(),
            sched: HelpItemScheduler.FSRS,
            global: true,
        },
        healthCheck: {
            title: tr.deckConfigHealthCheck(),
            help: tr.deckConfigAffectsEntireCollection()
                + "\n\n"
                + tr.deckConfigHealthCheckTooltip1()
                + "\n\n"
                + tr.deckConfigHealthCheckTooltip2(),
            sched: HelpItemScheduler.FSRS,
            global: true,
        },
    };
}
