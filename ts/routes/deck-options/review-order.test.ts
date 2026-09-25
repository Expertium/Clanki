// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    DeckConfig_Config_NewCardGatherPriority as NewCardGatherPriority,
    DeckConfig_Config_ReviewCardOrder as ReviewCardOrder,
} from "@generated/anki/deck_config_pb";
import { expect, test } from "vitest";

import {
    DEFAULT_REVIEW_ORDER,
    newGatherPriorityWithoutRetrievability,
    reviewOrderForAlgorithm,
    withoutDifficultyOrdersUnderRwkv,
    withoutRetrievabilityNewGatherOrders,
} from "./review-order";

// Pins spec/deck-options.md#deck-options.no-difficulty-order-under-rwkv

const choices = [
    { value: ReviewCardOrder.DAY },
    { value: ReviewCardOrder.EASE_ASCENDING },
    { value: ReviewCardOrder.EASE_DESCENDING },
    { value: ReviewCardOrder.RETRIEVABILITY_ASCENDING },
];

test("difficulty orders are offered only when FSRS schedules", () => {
    expect(withoutDifficultyOrdersUnderRwkv(choices, false)).toStrictEqual(choices);
    expect(withoutDifficultyOrdersUnderRwkv(choices, true).map((c) => c.value)).toStrictEqual([
        ReviewCardOrder.DAY,
        ReviewCardOrder.RETRIEVABILITY_ASCENDING,
    ]);
});

test("a difficulty order under RWKV becomes the default order", () => {
    expect(DEFAULT_REVIEW_ORDER).toBe(ReviewCardOrder.RETRIEVABILITY_DESCENDING);
    expect(reviewOrderForAlgorithm(ReviewCardOrder.EASE_ASCENDING, true)).toBe(
        ReviewCardOrder.RETRIEVABILITY_DESCENDING,
    );
    expect(reviewOrderForAlgorithm(ReviewCardOrder.EASE_DESCENDING, true)).toBe(
        ReviewCardOrder.RETRIEVABILITY_DESCENDING,
    );
    // other orders, and any order under FSRS, are kept
    expect(reviewOrderForAlgorithm(ReviewCardOrder.DAY, true)).toBe(ReviewCardOrder.DAY);
    expect(reviewOrderForAlgorithm(ReviewCardOrder.EASE_DESCENDING, false)).toBe(
        ReviewCardOrder.EASE_DESCENDING,
    );
});

// Pins spec/deck-options.md#deck-options.no-new-card-retrievability-order
test("the retrievability new-card orders are never offered", () => {
    const choices = [
        NewCardGatherPriority.DECK,
        NewCardGatherPriority.ASCENDING_RETRIEVABILITY,
        NewCardGatherPriority.DESCENDING_RETRIEVABILITY,
        NewCardGatherPriority.RANDOM_CARDS,
    ].map((value) => ({ value }));

    expect(withoutRetrievabilityNewGatherOrders(choices).map((c) => c.value)).toEqual([
        NewCardGatherPriority.DECK,
        NewCardGatherPriority.RANDOM_CARDS,
    ]);
    // a stored one reads as the default, whatever the algorithm
    expect(
        newGatherPriorityWithoutRetrievability(NewCardGatherPriority.ASCENDING_RETRIEVABILITY),
    ).toBe(NewCardGatherPriority.DECK);
    expect(
        newGatherPriorityWithoutRetrievability(NewCardGatherPriority.DESCENDING_RETRIEVABILITY),
    ).toBe(NewCardGatherPriority.DECK);
    expect(newGatherPriorityWithoutRetrievability(NewCardGatherPriority.RANDOM_CARDS)).toBe(
        NewCardGatherPriority.RANDOM_CARDS,
    );
});
