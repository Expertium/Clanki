<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { HelpPage } from "@tslib/help-page";
    import type Carousel from "bootstrap/js/dist/carousel";
    import type Modal from "bootstrap/js/dist/modal";

    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import HelpModal from "$lib/components/HelpModal.svelte";
    import Item from "$lib/components/Item.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import type { HelpItem } from "$lib/components/types";

    import { hideRelatedCardsHelp, hideRelatedCardsTitle } from "./bury-siblings";
    import HideRelatedCardsRow from "./HideRelatedCardsRow.svelte";
    import type { DeckOptionsState } from "./lib";

    export let state: DeckOptionsState;
    export let api: Record<string, never>;

    const config = state.currentConfig;
    // which settings show (ui-split.ts)
    const shown = state.settingShown;
    const defaults = state.defaults;

    // One switch, the same one Simple mode draws: the three stored settings
    // are no longer separately settable (spec deck-options.simple-view).
    const settings = {
        burySiblings: {
            title: hideRelatedCardsTitle(),
            help: hideRelatedCardsHelp(),
        },
    };
    const helpSections: HelpItem[] = Object.values(settings);

    let modal: Modal;
    let carousel: Carousel;

    function openHelpModal(index: number): void {
        modal.show();
        carousel.to(index);
    }
</script>

<TitledContainer title={tr.deckConfigBuryTitle()}>
    <HelpModal
        title={tr.deckConfigBuryTitle()}
        url={HelpPage.Studying.siblingsAndBurying}
        slot="tooltip"
        {helpSections}
        on:mount={(e) => {
            modal = e.detail.modal;
            carousel = e.detail.carousel;
        }}
    />
    <DynamicallySlottable slotHost={Item} {api}>
        {#if $shown("burySiblings", "section")}
            <Item>
                <HideRelatedCardsRow
                    {config}
                    {defaults}
                    onHelp={() => openHelpModal(0)}
                />
            </Item>
        {/if}
    </DynamicallySlottable>
</TitledContainer>
