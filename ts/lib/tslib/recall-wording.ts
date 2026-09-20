// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * How the interface names the chance of recalling a card now (spec
 * ui.simple-recall-wording): the technical word "retrievability", or the
 * plain words "probability of recall". The user picks one of three choices
 * in Preferences > UI split; the pages receive it beside the UI mode, in
 * the response they already read the mode from.
 */

import { RecallWording } from "@generated/anki/config_pb";

export { RecallWording };

/**
 * Whether the interface uses the plain words. This is the one helper the
 * TypeScript side resolves the setting with: plain words when the setting
 * is PLAIN, or when it is BY_MODE and the UI is in Simple mode.
 */
export function plainRecallWording(
    setting: RecallWording | undefined,
    advanced: boolean,
): boolean {
    switch (setting) {
        case RecallWording.PLAIN:
            return true;
        case RecallWording.TECHNICAL:
            return false;
        default:
            return !advanced;
    }
}
