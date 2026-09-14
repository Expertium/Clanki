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

    import type { AlgorithmHelpKey } from "./algorithm-help";
    import { algorithmHelpSettings } from "./algorithm-help";
    import AlgorithmRows from "./AlgorithmRows.svelte";
    import type { DeckOptionsState } from "./lib";

    /** The Advanced-mode Algorithm section: a titled container around AlgorithmRows. */
    export let state: DeckOptionsState;
    export let api: Record<string, never>;

    let algorithmRows: AlgorithmRows | undefined;
    export function onPresetChange() {
        if (algorithmRows) {
            algorithmRows.onPresetChange();
        }
    }

    const fsrs = state.fsrs;
    const settings = algorithmHelpSettings();
    const helpSections: HelpItem[] = Object.values(settings);

    let modal: Modal;
    let carousel: Carousel;

    function openHelp(key: AlgorithmHelpKey): void {
        modal.show();
        carousel.to(Object.keys(settings).indexOf(key));
    }
</script>

<TitledContainer title={tr.deckConfigScheduler()}>
    <HelpModal
        title={tr.deckConfigScheduler()}
        url={HelpPage.DeckOptions.fsrs}
        slot="tooltip"
        fsrs={$fsrs}
        {helpSections}
        on:mount={(e) => {
            modal = e.detail.modal;
            carousel = e.detail.carousel;
        }}
    />
    <DynamicallySlottable slotHost={Item} {api}>
        <AlgorithmRows {state} {openHelp} bind:this={algorithmRows} />
    </DynamicallySlottable>
</TitledContainer>
