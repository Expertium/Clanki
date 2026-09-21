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
 * The switch's name, the same in both modes. It says what happens instead of
 * saying "bury siblings", because both words have to be taught first
 * (spec deck-options.simple-view).
 */
export function hideRelatedCardsTitle(): string {
    return tr.deckConfigHideRelatedCards();
}

/** The switch's help: what a related card is, and what the switch does. */
export function hideRelatedCardsHelp(): string {
    return tr.deckConfigHideRelatedCardsTooltip();
}

/** The short explanation shown when the underlined words are hovered. */
export function hideRelatedCardsHover(): string {
    return tr.deckConfigHideRelatedCardsHover();
}

export interface TitleParts {
    before: string;
    term: string;
    after: string;
}

/**
 * The title split around the words that explain themselves on hover.
 *
 * A translation is free to word the label its own way, and then the term is
 * not in it. That is not an error: `term` comes back empty and the label is
 * shown plain, rather than underlining the wrong words.
 */
export function hideRelatedCardsTitleParts(): TitleParts {
    const title = hideRelatedCardsTitle();
    const term = tr.deckConfigHideRelatedCardsTerm();
    const at = term ? title.indexOf(term) : -1;
    if (at < 0) {
        return { before: title, term: "", after: "" };
    }
    return {
        before: title.slice(0, at),
        term: title.slice(at, at + term.length),
        after: title.slice(at + term.length),
    };
}
