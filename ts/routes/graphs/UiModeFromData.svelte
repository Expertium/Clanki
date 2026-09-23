<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { GraphsResponse } from "@generated/anki/stats_pb";
    import type { Writable } from "svelte/store";

    /** The collection's UI mode comes with each data load; the page's
     * switch changes it in between (spec ui.mode-switch). */
    export let data: GraphsResponse;
    export let advancedUi: Writable<boolean>;
    export let known: boolean;
    /** The mode the page's own switch chose, until a data load reports it:
     * a load the switch started can run before the main window has stored
     * the new mode, and must not switch the page back. */
    export let switchChoice: boolean | null = null;

    $: {
        if (switchChoice === null || switchChoice === data.advancedUi) {
            advancedUi.set(data.advancedUi);
            switchChoice = null;
        }
        known = true;
    }
</script>
