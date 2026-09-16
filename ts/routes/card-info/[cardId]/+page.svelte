<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { page } from "$app/state";

    import CardInfo from "../CardInfo.svelte";
    import type { PageData } from "./$types";
    import { goto, invalidate } from "$app/navigation";
    import { onDestroy } from "svelte";

    export let data: PageData;

    const showRevlog = page.url.searchParams.get("revlog") !== "0";

    // While RWKV is not ready the response says so; ask again until the curve
    // arrives, so the user does not have to reopen card info
    // (spec ui.card-info-curve-messages).
    const RWKV_CURVE_RETRY_MS = 2000;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;

    function scheduleRetry(pending: boolean): void {
        clearTimeout(retryTimer);
        retryTimer = undefined;
        if (pending) {
            retryTimer = setTimeout(
                () => invalidate("anki:card-info"),
                RWKV_CURVE_RETRY_MS,
            );
        }
    }

    $: scheduleRetry(data.info?.rwkvCurve?.pending ?? false);

    onDestroy(() => clearTimeout(retryTimer));

    globalThis.anki ||= {};
    globalThis.anki.updateCard = async (card_id: string): Promise<void> => {
        const path = `/card-info/${card_id}`;
        if (page.params.cardId === card_id) {
            await invalidate("anki:card-info");
        } else {
            await goto(path).catch(() => {
                window.location.href = path;
            });
        }
    };
</script>

<CardInfo stats={data.info} {showRevlog} />
