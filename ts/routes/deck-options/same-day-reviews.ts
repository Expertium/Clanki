// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// "Max number of same-day reviews" (spec sched.max-same-day-reviews). The
// preset stores no value for "no limit"; the spin box shows that as 9999.

export const MAX_SAME_DAY_REVIEWS_NO_LIMIT = 9999;

export interface SameDayReviewsSettings {
    maxSameDayReviews?: number;
}

export function maxSameDayReviewsFromConfig(config: SameDayReviewsSettings): number {
    return config.maxSameDayReviews ?? MAX_SAME_DAY_REVIEWS_NO_LIMIT;
}

/** The row shows only while the preset has no learning steps, because the
 * limit applies only then; with steps, the steps decide the same-day
 * reviews. */
export function maxSameDayReviewsShown(config: { learnSteps: number[] }): boolean {
    return config.learnSteps.length === 0;
}

/** The config with the limit set to `value`; the same object when it already
 * reads as `value`, so showing a preset writes nothing. */
export function applyMaxSameDayReviews<T extends SameDayReviewsSettings>(
    config: T,
    value: number,
): T {
    if (maxSameDayReviewsFromConfig(config) === value) {
        return config;
    }
    config.maxSameDayReviews = value;
    return config;
}
