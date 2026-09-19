// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
import { getDeckConfigsForUpdate } from "@generated/backend";
import { loadSimpleItems } from "@tslib/ui-split";

import { DeckOptionsState } from "../lib";
import type { PageLoad } from "./$types";

export const load = (async ({ params }) => {
    const deckId = Number(params.deckId);

    const did = BigInt(deckId);
    const [info, simpleItems] = await Promise.all([
        getDeckConfigsForUpdate({ did }),
        loadSimpleItems(),
    ]);
    const state = new DeckOptionsState(BigInt(did), info);
    state.simpleItems.set(simpleItems);

    return { state };
}) satisfies PageLoad;
