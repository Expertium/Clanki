<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<!--
    The one burying switch, drawn the same way in both UI modes
    (spec deck-options.simple-view). It stands for the preset's three stored
    bury settings; "related cards" explains itself on hover, so the label
    answers "what is a sibling?" without a click.
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { get, type Writable } from "svelte/store";

    import GlossaryTerm from "$lib/components/GlossaryTerm.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import SwitchRow from "$lib/components/SwitchRow.svelte";

    import {
        applyBurySiblings,
        type BurySettings,
        burySiblingsFromConfig,
        burySiblingsPartlyOn,
        hideRelatedCardsHover,
        hideRelatedCardsTitleParts,
    } from "./bury-siblings";

    export let config: Writable<BurySettings>;
    export let defaults: BurySettings;
    /** Opens the setting's own help. */
    export let onHelp: () => void;

    const parts = hideRelatedCardsTitleParts();
    const hover = hideRelatedCardsHover();

    let burySiblings = burySiblingsFromConfig($config);
    $: burySiblings = burySiblingsFromConfig($config);

    function setBurySiblings(on: boolean): void {
        if (burySiblingsFromConfig(get(config)) !== on) {
            config.update((current) => applyBurySiblings(current, on));
        }
    }

    $: setBurySiblings(burySiblings);
</script>

<SwitchRow bind:value={burySiblings} defaultValue={burySiblingsFromConfig(defaults)}>
    <SettingTitle on:click={onHelp}>
        {parts.before}{#if parts.term}<GlossaryTerm explanation={hover}
            >{parts.term}</GlossaryTerm
            >{/if}{parts.after}
    </SettingTitle>
</SwitchRow>
{#if burySiblingsPartlyOn($config)}
    <div class="partly-on">{tr.deckConfigPartlyOn()}</div>
{/if}

<style lang="scss">
    .partly-on {
        color: var(--fg-subtle);
        font-size: 0.85em;
        margin-top: -0.25em;
    }
</style>
