// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    DeckConfig_Config,
    DeckConfig_Config_ReviewCardOrder,
} from "@generated/anki/deck_config_pb";
import { SimulateFsrsReviewRequest } from "@generated/anki/scheduler_pb";
import { expect, test } from "vitest";

import {
    HELP_ME_DECIDE_ENFORCE_MONOTONIC_SUCCESS_GRADE_PROBS_DEFAULT,
    HELP_ME_DECIDE_TRANSITION_BLEND_ALPHA_DEFAULT,
} from "./help-me-decide-defaults";
import { buildSimulateFsrsRequest } from "./simulate-fsrs-request";

const sampleParams = [0.4, 0.6, 2.4, 5.8, 6.8, 0.6, 0.9, 0.05];
const sampleEasyDays = [1, 1, 0.5, 1, 1, 0, 1];

function sampleConfig(): DeckConfig_Config {
    return new DeckConfig_Config({
        desiredRetention: 0.87,
        newPerDay: 15,
        reviewsPerDay: 250,
        maximumReviewInterval: 3650,
        easyDaysPercentages: sampleEasyDays,
        reviewOrder: DeckConfig_Config_ReviewCardOrder.RANDOM,
        historicalRetention: 0.92,
        learnSteps: [1, 10, 60],
        relearnSteps: [10],
    });
}

function buildRequest(reviewFuzzEnabled = true): SimulateFsrsReviewRequest {
    return buildSimulateFsrsRequest({
        config: sampleConfig(),
        params: sampleParams,
        search: "deck:Default",
        newCardsIgnoreReviewLimit: true,
        reviewFuzzEnabled,
        reviewFuzzBase: 0.05,
        reviewFuzzFactorShort: 0.15,
        reviewFuzzFactorMid: 0.1,
        reviewFuzzFactorLong: 0.05,
    });
}

// Pins spec/deck-options.md deck-options.simulator-fsrs-only: the simulator
// request is FSRS-only and has no RWKV fields.
test("simulate request carries no RWKV fields", () => {
    const request = buildRequest();

    const rwkvKeys = Object.keys(request).filter((key) => /rwkv/i.test(key));
    expect(rwkvKeys).toStrictEqual([]);
    expect(
        SimulateFsrsReviewRequest.fields.findJsonName("rwkvWorkloadSampleLimit"),
    ).toBeUndefined();
    expect(SimulateFsrsReviewRequest.fields.find(36)).toBeUndefined();
    expect(SimulateFsrsReviewRequest.fields.find(37)).toBeUndefined();
    expect(SimulateFsrsReviewRequest.fields.find(38)).toBeUndefined();
});

test("simulate request fills the FSRS fields from the preset", () => {
    const request = buildRequest();

    expect(request.params).toStrictEqual(sampleParams);
    expect(request.desiredRetention).toBeCloseTo(0.87);
    expect(request.newLimit).toBe(15);
    expect(request.reviewLimit).toBe(250);
    expect(request.maxInterval).toBe(3650);
    expect(request.search).toBe("deck:Default");
    expect(request.newCardsIgnoreReviewLimit).toBe(true);
    expect(request.easyDaysPercentages).toStrictEqual(sampleEasyDays);
    expect(request.reviewOrder).toBe(DeckConfig_Config_ReviewCardOrder.RANDOM);
    expect(request.historicalRetention).toBeCloseTo(0.92);
    expect(request.learningStepCount).toBe(3);
    expect(request.relearningStepCount).toBe(1);
    expect(request.reviewFuzzBase).toBeCloseTo(0.05);
    expect(request.reviewFuzzFactorShort).toBeCloseTo(0.15);
    expect(request.reviewFuzzFactorMid).toBeCloseTo(0.1);
    expect(request.reviewFuzzFactorLong).toBeCloseTo(0.05);
    expect(request.helpMeDecideTransitionBlendAlpha).toBe(
        HELP_ME_DECIDE_TRANSITION_BLEND_ALPHA_DEFAULT,
    );
    expect(request.helpMeDecideEnforceMonotonicSuccessGradeProbs).toBe(
        HELP_ME_DECIDE_ENFORCE_MONOTONIC_SUCCESS_GRADE_PROBS_DEFAULT,
    );
    // Days to simulate and the extra deck size are set by the simulator
    // modal before each run, not by the builder.
    expect(request.daysToSimulate).toBe(0);
    expect(request.deckSize).toBe(0);
});

test("simulate request zeroes the fuzz settings when fuzz is off", () => {
    const request = buildRequest(false);

    expect(request.reviewFuzzBase).toBe(0);
    expect(request.reviewFuzzFactorShort).toBe(0);
    expect(request.reviewFuzzFactorMid).toBe(0);
    expect(request.reviewFuzzFactorLong).toBe(0);
});
