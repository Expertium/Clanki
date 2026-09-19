// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The Simple | Advanced split (spec ui.split-configurable): which items
 * Simple mode shows. The registry and its defaults live in Python
 * (qt/aqt/ui_split.py); a page asks for every item id -> "shown in Simple
 * mode", with the user's choices from Preferences > UI split applied.
 */

import { Empty, Json } from "@generated/anki/generic_pb";
import { postProto } from "@generated/post";

/** Item id -> whether Simple mode shows it. */
export type SimpleItems = Record<string, boolean>;

/** The split, or null when it cannot be read (the page then shows every
 * item, as Advanced mode does, so nothing is ever lost). */
export async function loadSimpleItems(): Promise<SimpleItems | null> {
    try {
        const reply = await postProto("getUiSplit", new Empty(), Json, { alertOnError: false });
        const parsed = JSON.parse(new TextDecoder().decode(reply.json));
        return parsed && typeof parsed === "object" ? (parsed as SimpleItems) : null;
    } catch {
        return null;
    }
}

/**
 * Whether the page shows an item: Advanced mode shows every item; Simple
 * mode the ones the split gives it. Without the split every item shows.
 */
export function itemShown(
    id: string,
    advanced: boolean,
    simpleItems: SimpleItems | null,
): boolean {
    return advanced || simpleItems === null || simpleItems[id] === true;
}
