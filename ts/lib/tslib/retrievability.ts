// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The word "retrievability" explains itself on hover wherever a text shows
 * it (spec ui.retrievability-advanced-only): it is underlined, and the hover
 * says "probability of recall". The texts that show it are Advanced-only.
 */

import * as tr from "@generated/ftl";

/** A piece of a text: the explained word, or the text around it. */
export interface TextPart {
    text: string;
    term: boolean;
}

/** `text` cut around every occurrence of `term`, whatever its case. A text
 * without it is one plain part, and so is every text when `term` is empty. */
export function termParts(text: string, term: string): TextPart[] {
    const parts: TextPart[] = [];
    const needle = term.toLocaleLowerCase();
    const haystack = text.toLocaleLowerCase();
    let from = 0;
    while (needle) {
        const at = haystack.indexOf(needle, from);
        if (at < 0) {
            break;
        }
        if (at > from) {
            parts.push({ text: text.slice(from, at), term: false });
        }
        parts.push({ text: text.slice(at, at + needle.length), term: true });
        from = at + needle.length;
    }
    if (from < text.length) {
        parts.push({ text: text.slice(from), term: false });
    }
    return parts;
}

/** `text` cut around the word "retrievability" of the current language. */
export function retrievabilityParts(text: string): TextPart[] {
    return termParts(text, tr.cardStatsRetrievabilityTerm());
}

/** What the hover says. */
export function retrievabilityExplanation(): string {
    return tr.cardStatsRetrievabilityExplanation();
}
