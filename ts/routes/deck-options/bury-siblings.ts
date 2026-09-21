// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import * as tr from "@generated/ftl";

/**
 * The Simple-mode "Hide related cards until tomorrow" switch stands for the
 * three bury settings of a preset (spec/deck-options.md,
 * `deck-options.simple-view`). Advanced mode keeps the three separate
 * switches and keeps the word "sibling".
 */
export interface BurySettings {
    buryNew: boolean;
    buryReviews: boolean;
    buryInterdayLearning: boolean;
}

/** The switch reads as on only while all three settings are on. */
export function burySiblingsFromConfig(config: BurySettings): boolean {
    return config.buryNew && config.buryReviews && config.buryInterdayLearning;
}

/**
 * Some but not all of the three settings are on (set in Advanced mode): the
 * switch reads as off and shows a "Partly on" caption
 * (spec deck-options.simple-view).
 */
export function burySiblingsPartlyOn(config: BurySettings): boolean {
    const on = [config.buryNew, config.buryReviews, config.buryInterdayLearning];
    return on.some(Boolean) && !on.every(Boolean);
}

/**
 * Sets all three settings to `on`, in place, and returns the config. A no-op
 * when the switch already reads as `on`, so showing a preset writes nothing:
 * a preset with only some of the settings on keeps them until the switch is
 * toggled.
 */
export function applyBurySiblings<T extends BurySettings>(config: T, on: boolean): T {
    if (burySiblingsFromConfig(config) === on) {
        return config;
    }
    config.buryNew = on;
    config.buryReviews = on;
    config.buryInterdayLearning = on;
    return config;
}

/**
 * Simple mode's name for the switch. Advanced mode keeps "Bury siblings" and
 * its three per-type switches; Simple mode says what happens instead, because
 * "sibling" and "bury" are words a new user has to be taught first
 * (spec deck-options.simple-view).
 */
export function hideRelatedCardsTitle(): string {
    return tr.deckConfigHideRelatedCards();
}

/**
 * Simple mode's help for the switch: what a related card is, and what the
 * switch does. The per-type explanations belong to Advanced mode, so Simple
 * mode does not append them.
 */
export function hideRelatedCardsHelp(): string {
    return tr.deckConfigHideRelatedCardsTooltip();
}
