// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { type DeckConfig } from "@generated/anki/deck_config_pb";
import { SimulateFsrsReviewRequest } from "@generated/anki/scheduler_pb";

import { escapeSearchText } from "./lib";

function workloadSearchForPreset(
    deckNameForSearch: string,
    presetName: string,
): string {
    return `deck:"${deckNameForSearch}" preset:"${escapeSearchText(presetName)}" -is:suspended`;
}

export function workloadRequestForPreset(
    baseRequest: SimulateFsrsReviewRequest,
    deckNameForSearch: string,
    config: DeckConfig,
): SimulateFsrsReviewRequest {
    const inner = config.config!;
    const request = new SimulateFsrsReviewRequest(baseRequest);
    // FSRS-7 only; empty means the FSRS-7 defaults (spec sched.fsrs7-only)
    request.params = inner.fsrsParams7;
    request.desiredRetention = inner.desiredRetention;
    request.search = workloadSearchForPreset(deckNameForSearch, config.name);
    request.workloadPresetLabel = config.name;
    request.historicalRetention = inner.historicalRetention;
    request.learningStepCount = inner.learnSteps.length;
    request.relearningStepCount = inner.relearnSteps.length;
    return request;
}
