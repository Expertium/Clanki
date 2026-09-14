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
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import SwitchRow from "$lib/components/SwitchRow.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import type { HelpItem } from "$lib/components/types";

    import DailyLimitRows from "./DailyLimitRows.svelte";
    import GlobalLabel from "./GlobalLabel.svelte";
    import type { DeckOptionsState } from "./lib";

    /**
     * The Advanced-mode Daily Limits section. The Simple-mode page hosts
     * DailyLimitRows itself; the two collection-wide switches are
     * Advanced-only (spec deck-options.simple-view).
     */
    export let state: DeckOptionsState;
    export let api: Record<string, never>;

    let dailyLimitRows: DailyLimitRows | undefined;
    export function onPresetChange() {
        if (dailyLimitRows) {
            dailyLimitRows.onPresetChange();
        }
    }

    const applyAllParentLimits = state.applyAllParentLimits;

    const v3Extra =
        "\n\n" + tr.deckConfigLimitDeckV3() + "\n\n" + tr.deckConfigTabDescription();
    const reviewV3Extra = "\n\n" + tr.deckConfigLimitInterdayBoundByReviews() + v3Extra;
    const applyAllParentLimitsHelp =
        tr.deckConfigAffectsEntireCollection() +
        "\n\n" +
        tr.deckConfigApplyAllParentLimitsTooltip();

    const settings = {
        newLimit: {
            title: tr.schedulingNewCardsday(),
            help: tr.deckConfigNewLimitTooltip() + v3Extra,
            url: HelpPage.DeckOptions.newCardsday,
        },
        reviewLimit: {
            title: tr.schedulingMaximumReviewsday(),
            help: tr.deckConfigReviewLimitTooltip() + reviewV3Extra,
            url: HelpPage.DeckOptions.maximumReviewsday,
        },
        applyAllParentLimits: {
            title: tr.deckConfigApplyAllParentLimits(),
            help: applyAllParentLimitsHelp,
            url: HelpPage.DeckOptions.limitsFromTop,
            global: true,
        },
    };
    const helpSections: HelpItem[] = Object.values(settings);

    let modal: Modal;
    let carousel: Carousel;

    function openHelp(key: keyof typeof settings): void {
        modal.show();
        carousel.to(Object.keys(settings).indexOf(key));
    }
</script>

<TitledContainer title={tr.deckConfigDailyLimits()}>
    <HelpModal
        title={tr.deckConfigDailyLimits()}
        url={HelpPage.DeckOptions.dailyLimits}
        slot="tooltip"
        {helpSections}
        on:mount={(e) => {
            modal = e.detail.modal;
            carousel = e.detail.carousel;
        }}
    />
    <DynamicallySlottable slotHost={Item} {api}>
        <DailyLimitRows {state} {openHelp} bind:this={dailyLimitRows} />

        <!-- "New cards ignore review limit" is gone: new cards always count
             against the review limit (spec sched.new-cards-never-ignore-review-limit). -->
        <Item>
            <SwitchRow bind:value={$applyAllParentLimits} defaultValue={false}>
                <SettingTitle on:click={() => openHelp("applyAllParentLimits")}>
                    <GlobalLabel title={settings.applyAllParentLimits.title} />
                </SettingTitle>
            </SwitchRow>
        </Item>
    </DynamicallySlottable>
</TitledContainer>
