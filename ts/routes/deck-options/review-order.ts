// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    DeckConfig_Config_NewCardGatherPriority as NewCardGatherPriority,
    DeckConfig_Config_ReviewCardOrder as ReviewCardOrder,
} from "@generated/anki/deck_config_pb";

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

/** The review sort order new presets get: most likely to be recalled first. */
export const DEFAULT_REVIEW_ORDER = ReviewCardOrder.RETRIEVABILITY_DESCENDING;

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

/**
 * The new-card gather orders that rank by RWKV-Instant's scores. Only
 * RWKV-Instant has a value for a new card, so under FSRS-7 and RWKV-Curve
 * they are not offered (spec deck-options.new-retrievability-order-instant-only).
 */
export const INSTANT_NEW_GATHER_ORDERS: readonly NewCardGatherPriority[] = [
    NewCardGatherPriority.ASCENDING_RETRIEVABILITY,
    NewCardGatherPriority.DESCENDING_RETRIEVABILITY,
];

/** The new-card gather order a preset gets when it held one of those. */
export const DEFAULT_NEW_GATHER_PRIORITY = NewCardGatherPriority.DECK;

/** Drops the RWKV-Instant new-card orders unless RWKV-Instant schedules. */
export function newGatherChoicesForAlgorithm<T extends { value: NewCardGatherPriority }>(
    choices: T[],
    instant: boolean,
): T[] {
    return instant
        ? choices
        : choices.filter((choice) => !INSTANT_NEW_GATHER_ORDERS.includes(choice.value));
}

/** The new-card gather order a preset should hold under its algorithm. */
export function newGatherPriorityForAlgorithm(
    priority: NewCardGatherPriority,
    instant: boolean,
): NewCardGatherPriority {
    return !instant && INSTANT_NEW_GATHER_ORDERS.includes(priority)
        ? DEFAULT_NEW_GATHER_PRIORITY
        : priority;
}
