// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    DeckConfig_Config_NewCardGatherPriority as NewCardGatherPriority,
    DeckConfig_Config_ReviewCardOrder as ReviewCardOrder,
} from "@generated/anki/deck_config_pb";
import { expect, test } from "vitest";

import {
    DEFAULT_REVIEW_ORDER,
    newGatherChoicesForAlgorithm,
    newGatherPriorityForAlgorithm,
    reviewOrderForAlgorithm,
    withoutDifficultyOrdersUnderRwkv,
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

// Pins spec/deck-options.md#deck-options.new-retrievability-order-instant-only
test("the retrievability new-card orders are offered only under RWKV-Instant", () => {
    const choices = [
        NewCardGatherPriority.DECK,
        NewCardGatherPriority.ASCENDING_RETRIEVABILITY,
        NewCardGatherPriority.DESCENDING_RETRIEVABILITY,
        NewCardGatherPriority.RANDOM_CARDS,
    ].map((value) => ({ value }));

    expect(newGatherChoicesForAlgorithm(choices, true)).toEqual(choices);
    expect(newGatherChoicesForAlgorithm(choices, false).map((c) => c.value)).toEqual([
        NewCardGatherPriority.DECK,
        NewCardGatherPriority.RANDOM_CARDS,
    ]);
    // a stored one reads as the default under another algorithm
    expect(
        newGatherPriorityForAlgorithm(NewCardGatherPriority.ASCENDING_RETRIEVABILITY, false),
    ).toBe(NewCardGatherPriority.DECK);
    expect(
        newGatherPriorityForAlgorithm(NewCardGatherPriority.DESCENDING_RETRIEVABILITY, true),
    ).toBe(NewCardGatherPriority.DESCENDING_RETRIEVABILITY);
    expect(newGatherPriorityForAlgorithm(NewCardGatherPriority.RANDOM_CARDS, false)).toBe(
        NewCardGatherPriority.RANDOM_CARDS,
    );
});
