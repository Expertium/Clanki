// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";
import { HelpPage } from "@tslib/help-page";

import { type HelpItem, HelpItemScheduler } from "$lib/components/types";

export type AlgorithmHelpKey = "fsrs" | "desiredRetention" | "modelParams";

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
        },
        desiredRetention: {
            title: tr.deckConfigDesiredRetention(),
            help: tr.deckConfigDesiredRetentionTooltip(),
            sched: HelpItemScheduler.FSRS,
        },
        modelParams: {
            title: tr.deckConfigWeights(),
            help: tr.deckConfigWeightsTooltip2()
                + "\n\n"
                + tr.deckConfigComputeOptimalWeightsTooltip2(),
            sched: HelpItemScheduler.FSRS,
        },
    };
}
