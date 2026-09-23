// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The deck-options page's part of the Simple | Advanced split (spec
 * deck-options.simple-view, ui.split-configurable): one item per setting.
 *
 * Simple mode shows the Simple section (SimpleOptions) in its own layout,
 * with those of its settings (CURATED) that the split gives it. A setting
 * the user adds to Simple mode goes where it belongs: the daily-limit and
 * Algorithm settings into the Simple section, next to New cards/day and
 * Desired retention, which share their controls; every other one into its
 * Advanced-mode section, drawn below the Simple section (a section shows
 * only when it has such a setting). Advanced mode shows the sections with
 * every setting, as before.
 *
 * The item ids are "deckOptions." + the keys below; qt/aqt/ui_split.py lists
 * the same ones, in the same order, with their names and defaults. The
 * settings of ADVANCED_ONLY are not items of the split.
 */

import { itemShown, type SimpleItems } from "@tslib/ui-split";

/** The settings of the Simple section (shown there by default). */
export const CURATED = [
    "newLimit",
    "desiredRetention",
    "optimizeAllPresets",
    "burySiblings",
    "playAudio",
    "showTimer",
    "easyDays",
] as const;

/** The settings Simple mode draws in the Simple section: CURATED, and the
 * other settings of the controls the Simple section shares with the Daily
 * limits and Algorithm sections. */
export const IN_SIMPLE_SECTION: readonly string[] = [
    ...CURATED,
    "reviewLimit",
    "dailyLimitTabs",
    "algorithm",
    "desiredRetentionTabs",
    "fsrsHelpMeDecide",
    "fsrsParams",
    "fsrsAutoOptimizeDays",
    "fsrsSearchFilter",
    "fsrsHealthCheck",
    "fsrsSimulator",
];

/** Each Advanced-mode section and its settings, in page order. A setting of
 * CURATED that the section also shows (in Advanced mode) is listed too. */
export const SECTIONS = {
    dailyLimits: ["newLimit", "reviewLimit", "dailyLimitTabs"],
    newCards: ["learningSteps", "maxSameDayReviews", "insertionOrder"],
    lapses: ["relearningSteps", "leechThreshold", "leechAction", "leechOnlyIfYoung"],
    displayOrder: ["newCardSortOrder", "newReviewPriority", "interdayStepPriority"],
    algorithm: [
        "algorithm",
        "desiredRetention",
        "desiredRetentionTabs",
        "optimizeAllPresets",
        "fsrsHelpMeDecide",
        "fsrsParams",
        "fsrsAutoOptimizeDays",
        "fsrsSearchFilter",
        "fsrsHealthCheck",
        "fsrsSimulator",
    ],
    rwkv: [
        "rwkvEnforceGradeOrder",
        "rwkvAllowSameDayReview",
        "rwkvMinInterveningReviews",
        "rwkvMinElapsedSecs",
        "rwkvCandidateRefresh",
        "rwkvRefreshInterval",
        "rwkvRefreshOnExit",
        "rwkvMaintenance",
    ],
    burying: ["burySiblings"],
    audio: ["playAudio", "skipQuestionWhenReplaying"],
    timers: ["maximumAnswerSecs", "showTimer"],
    autoAdvance: ["secondsToShowQuestion", "secondsToShowAnswer", "waitForAudio", "questionAction", "answerAction"],
    easyDays: ["easyDays"],
    advanced: [
        "maximumInterval",
        "fsrsMinimumInterval",
        "ignoreReviewsBefore",
    ],
} as const;

/**
 * The settings that name retrievability, in their choices or their help
 * (spec ui.retrievability-advanced-only): the two orders that offer
 * retrievability orders, and RWKV's minimum reviews per day. Only Advanced
 * mode draws them, each in its own section. They are not items of the split,
 * so Simple mode never draws them, whatever the split says and when it
 * cannot be read.
 */
export const ADVANCED_ONLY = [
    "newGatherPriority",
    "reviewSortOrder",
    "rwkvMinimumReviewsPerDay",
] as const;

export type Section = keyof typeof SECTIONS;
export type SettingKey =
    | (typeof SECTIONS)[Section][number]
    | (typeof CURATED)[number]
    | (typeof ADVANCED_ONLY)[number];

/** Where a setting is drawn: the Simple section, or an Advanced-mode section. */
export type Placement = "simple" | "section";

/** Every setting of the split once, in page order: the Simple section's,
 * then the others. ADVANCED_ONLY is not in it. */
export function allSettings(): SettingKey[] {
    const keys: SettingKey[] = [...CURATED];
    for (const settings of Object.values(SECTIONS)) {
        for (const key of settings) {
            if (!keys.includes(key)) {
                keys.push(key);
            }
        }
    }
    return keys;
}

export function deckOptionsItemId(key: SettingKey): string {
    return `deckOptions.${key}`;
}

const inSimpleSection = new Set<string>(IN_SIMPLE_SECTION);
const advancedOnly = new Set<string>(ADVANCED_ONLY);

/**
 * Whether a setting is drawn at a place. Advanced mode draws the sections
 * with every setting (and never the Simple section). Simple mode draws each
 * setting the split shows once: in the Simple section (IN_SIMPLE_SECTION) or
 * in its own section; it never draws ADVANCED_ONLY.
 */
export function settingShown(
    key: SettingKey,
    placement: Placement,
    advanced: boolean,
    simpleItems: SimpleItems | null,
): boolean {
    if (advanced) {
        return placement === "section";
    }
    if (advancedOnly.has(key)) {
        return false;
    }
    const shown = itemShown(deckOptionsItemId(key), false, simpleItems);
    return shown && (placement === "simple") === inSimpleSection.has(key);
}

/**
 * The help of a section's settings, as its help modal lists them: Simple
 * mode leaves out the help of ADVANCED_ONLY, which names retrievability
 * (spec ui.retrievability-advanced-only).
 */
export function helpForMode<T>(help: Record<string, T>, advanced: boolean): Record<string, T> {
    if (advanced) {
        return help;
    }
    return Object.fromEntries(Object.entries(help).filter(([key]) => !advancedOnly.has(key)));
}

/** Whether Simple mode draws a section: when it has a setting to show. */
export function sectionShown(
    section: Section,
    advanced: boolean,
    simpleItems: SimpleItems | null,
): boolean {
    return SECTIONS[section].some((key) => settingShown(key, "section", advanced, simpleItems));
}
