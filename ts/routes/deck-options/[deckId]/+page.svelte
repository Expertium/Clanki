<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    import { afterNavigate, goto } from "$app/navigation";
    import { page as pageState } from "$app/state";
    import DeckOptionsPage from "../DeckOptionsPage.svelte";
    import { commitEditing } from "../lib";
    import type { PageData } from "./$types";
    import { deckOptionsReady, deckOptionsRequireClose } from "@generated/backend";

    export let data: PageData;
    let page: DeckOptionsPage;

    globalThis.anki ||= {};
    globalThis.anki.deckOptionsPendingChanges = async (): Promise<void> => {
        await commitEditing();
        if (
            !(await data.state.isModified()) ||
            confirm(tr.cardTemplatesDiscardChanges())
        ) {
            // Either there was no change, or the user accepted to discard the changes.
            deckOptionsRequireClose({});
        }
    };
    globalThis.anki.deckOptionsSaved = (): void => {
        location.reload();
    };
    // Qt keeps this page loaded between openings of the deck-options window
    // (qt/aqt/deckoptions.py). Opening the window again moves the page to
    // `url` (a deck and a generation number) without a reload: the loader
    // runs again, so the settings come fresh from the collection, and the
    // {#key} below builds the whole page anew from them.
    globalThis.anki.deckOptionsSwitch = async (url: string): Promise<void> => {
        try {
            await goto(url, { invalidateAll: true, replaceState: true });
        } catch {
            location.assign(url);
            return;
        }
        if (pageState.error) {
            // show the error the way a fresh load shows it
            location.assign(url);
        }
    };

    function closeRequested(): void {
        globalThis.anki.deckOptionsPendingChanges();
    }

    // Runs once the page is first shown and again after every switch.
    afterNavigate(() => {
        globalThis.$deckOptions = new Promise((resolve, _reject) => {
            resolve(page);
        });
        data.state.resolveOriginalConfigs();
        // Qt reads the page's generation number (the "g" parameter) from
        // this request's referrer, so a late answer from an earlier load or
        // switch is ignored.
        deckOptionsReady({});
    });
</script>

{#key data.state}
    <DeckOptionsPage state={data.state} bind:this={page} on:close={closeRequested} />
{/key}
