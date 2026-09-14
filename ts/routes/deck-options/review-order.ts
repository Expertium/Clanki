// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckConfig_Config_ReviewCardOrder as ReviewCardOrder } from "@generated/anki/deck_config_pb";

/**
 * The review sort orders that sort by FSRS difficulty ("Easy cards first",
 * "Difficult cards first"; stored as the old ease orders). Difficulty is an
 * FSRS state variable, so under RWKV-Curve and RWKV-Instant these orders are
 * not offered (spec deck-options.no-difficulty-order-under-rwkv).
 */
export const DIFFICULTY_REVIEW_ORDERS: readonly ReviewCardOrder[] = [
    ReviewCardOrder.EASE_ASCENDING,
    ReviewCardOrder.EASE_DESCENDING,
];

/** The review sort order new presets get: least likely to be recalled first. */
export const DEFAULT_REVIEW_ORDER = ReviewCardOrder.RETRIEVABILITY_ASCENDING;

export function isDifficultyReviewOrder(order: ReviewCardOrder): boolean {
    return DIFFICULTY_REVIEW_ORDERS.includes(order);
}

/** Drops the difficulty orders from a list of choices when RWKV schedules. */
export function withoutDifficultyOrdersUnderRwkv<T extends { value: ReviewCardOrder }>(
    choices: T[],
    rwkv: boolean,
): T[] {
    return rwkv ? choices.filter((choice) => !isDifficultyReviewOrder(choice.value)) : choices;
}

/**
 * The order a preset should hold: a difficulty order under RWKV becomes the
 * default order, anything else is kept.
 */
export function reviewOrderForAlgorithm(
    order: ReviewCardOrder,
    rwkv: boolean,
): ReviewCardOrder {
    return rwkv && isDifficultyReviewOrder(order) ? DEFAULT_REVIEW_ORDER : order;
}
