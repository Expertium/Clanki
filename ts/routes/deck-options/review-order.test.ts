// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfig_Config_ReviewCardOrder as ReviewCardOrder } from "@generated/anki/deck_config_pb";
import { expect, test } from "vitest";

import { DEFAULT_REVIEW_ORDER, reviewOrderForAlgorithm, withoutDifficultyOrdersUnderRwkv } from "./review-order";

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
