// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import {
    applyMaxSameDayReviews,
    MAX_SAME_DAY_REVIEWS_NO_LIMIT,
    maxSameDayReviewsFromConfig,
    type SameDayReviewsSettings,
} from "./same-day-reviews";

// Pins spec/scheduling.md#sched.max-same-day-reviews (the deck-options row)
test("an unset limit reads as no limit and is left unset", () => {
    const config: SameDayReviewsSettings = {};
    expect(maxSameDayReviewsFromConfig(config)).toBe(MAX_SAME_DAY_REVIEWS_NO_LIMIT);
    applyMaxSameDayReviews(config, MAX_SAME_DAY_REVIEWS_NO_LIMIT);
    expect(config.maxSameDayReviews).toBeUndefined();
});

test("editing writes the number, 0 included", () => {
    const config: SameDayReviewsSettings = {};
    applyMaxSameDayReviews(config, 2);
    expect(config.maxSameDayReviews).toBe(2);
    applyMaxSameDayReviews(config, 0);
    expect(config.maxSameDayReviews).toBe(0);
    expect(maxSameDayReviewsFromConfig(config)).toBe(0);
});
