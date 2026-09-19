// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// "Optimize every N days" (spec deck-options.fsrs-auto-optimize). A preset
// that stores no value optimizes every 30 days; 0 means never.

export const DEFAULT_FSRS_AUTO_OPTIMIZE_DAYS = 30;

export interface AutoOptimizeSettings {
    fsrsAutoOptimizeDays?: number;
}

export function autoOptimizeDaysFromConfig(config: AutoOptimizeSettings): number {
    return config.fsrsAutoOptimizeDays ?? DEFAULT_FSRS_AUTO_OPTIMIZE_DAYS;
}

/** The config with the days set to `value`; the same object when it already
 * reads as `value`, so showing a preset writes nothing. */
export function applyAutoOptimizeDays<T extends AutoOptimizeSettings>(
    config: T,
    value: number,
): T {
    if (autoOptimizeDaysFromConfig(config) === value) {
        return config;
    }
    config.fsrsAutoOptimizeDays = value;
    return config;
}

/** "Time to optimize" asks the user to act, so it shows only for a preset
 * that does not optimize by itself. */
export function timeToOptimizeShown(
    config: AutoOptimizeSettings,
    daysSinceLastOptimization: number,
): boolean {
    return autoOptimizeDaysFromConfig(config) === 0 && daysSinceLastOptimization > 30;
}
