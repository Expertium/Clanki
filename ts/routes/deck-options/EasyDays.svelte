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
    import { type HelpItem, HelpItemScheduler } from "$lib/components/types";

    import EasyDaysRows from "./EasyDaysRows.svelte";
    import type { DeckOptionsState } from "./lib";

    /** The Advanced-mode Easy Days section. The Simple-mode page hosts EasyDaysRows itself. */
    export let state: DeckOptionsState;
    export let api: Record<string, never>;

    const fsrsEnabled = state.fsrs;
    const settings = {
        easyDays: {
            title: tr.deckConfigEasyDaysTitle(),
            help: tr.deckConfigEasyDaysTooltip(),
            url: HelpPage.DeckOptions.fsrs,
            sched: HelpItemScheduler.FSRS,
        },
    };
    const helpSections: HelpItem[] = Object.values(settings);

    let modal: Modal;
    let carousel: Carousel;

    function openHelp(): void {
        modal.show();
        carousel.to(0);
    }
</script>

<TitledContainer title={tr.deckConfigEasyDaysTitle()}>
    <HelpModal
        title={tr.deckConfigEasyDaysTitle()}
        url={HelpPage.DeckOptions.fsrs}
        slot="tooltip"
        fsrs={$fsrsEnabled}
        {helpSections}
        on:mount={(e) => {
            modal = e.detail.modal;
            carousel = e.detail.carousel;
        }}
    />
    <DynamicallySlottable slotHost={Item} {api}>
        <EasyDaysRows {state} {openHelp} />
    </DynamicallySlottable>
</TitledContainer>
