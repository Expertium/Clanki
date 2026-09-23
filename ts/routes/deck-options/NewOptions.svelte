<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { DeckConfig_Config_NewCardInsertOrder } from "@generated/anki/deck_config_pb";
    import * as tr from "@generated/ftl";
    import { HelpPage } from "@tslib/help-page";
    import type Carousel from "bootstrap/js/dist/carousel";
    import type Modal from "bootstrap/js/dist/modal";
    import { get } from "svelte/store";

    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import EnumSelectorRow from "$lib/components/EnumSelectorRow.svelte";
    import HelpModal from "$lib/components/HelpModal.svelte";
    import Item from "$lib/components/Item.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import type { HelpItem } from "$lib/components/types";

    import { newInsertOrderChoices } from "./choices";
    import {
        applyMaxSameDayReviews,
        MAX_SAME_DAY_REVIEWS_NO_LIMIT,
        maxSameDayReviewsFromConfig,
        maxSameDayReviewsShown,
    } from "./same-day-reviews";
    import type { DeckOptionsState } from "./lib";
    import { intervalSettingsApply, stepsTooLargeWarning } from "./scheduler-choice";
    import SpinBoxRow from "./SpinBoxRow.svelte";
    import StepsInputRow from "./StepsInputRow.svelte";
    import Warning from "./Warning.svelte";

    export let state: DeckOptionsState;
    export let api = {};

    const config = state.currentConfig;
    // which settings show (ui-split.ts)
    const shown = state.settingShown;
    const defaults = state.defaults;
    const fsrs = state.fsrs;

    let stepsTooLarge: string;
    $: {
        const lastLearnStepInDays = $config.learnSteps.length
            ? $config.learnSteps[$config.learnSteps.length - 1] / 60 / 24
            : 0;
        stepsTooLarge = stepsTooLargeWarning($config, $fsrs, lastLearnStepInDays);
    }

    $: insertionOrderRandom =
        $config.newCardInsertOrder == DeckConfig_Config_NewCardInsertOrder.RANDOM
            ? tr.deckConfigNewInsertionOrderRandomWithV3()
            : "";

    // Unset (no limit) shows as 9999; editing writes the number (spec
    // sched.max-same-day-reviews).
    let maxSameDayReviews = maxSameDayReviewsFromConfig($config);
    // RWKV-Instant has no intervals, so the settings that shape one are not
    // shown (spec sched.rwkv-instant-no-steps)
    $: intervalSettings = intervalSettingsApply($config);
    $: maxSameDayReviews = maxSameDayReviewsFromConfig($config);
    function setMaxSameDayReviews(value: number): void {
        if (maxSameDayReviewsFromConfig(get(config)) !== value) {
            config.update((current) => applyMaxSameDayReviews(current, value));
        }
    }
    $: setMaxSameDayReviews(maxSameDayReviews);

    const settings = {
        learningSteps: {
            title: tr.deckConfigLearningSteps(),
            help: tr.deckConfigLearningStepsTooltip(),
            url: HelpPage.DeckOptions.learningSteps,
        },
        maxSameDayReviews: {
            title: tr.deckConfigMaxSameDayReviews(),
            help: tr.deckConfigMaxSameDayReviewsTooltip(),
            url: HelpPage.DeckOptions.learningSteps,
        },
        insertionOrder: {
            title: tr.deckConfigNewInsertionOrder(),
            help: tr.deckConfigNewInsertionOrderTooltip(),
            url: HelpPage.DeckOptions.insertionOrder,
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

<TitledContainer title={tr.schedulingNewCards()}>
    <HelpModal
        title={tr.schedulingNewCards()}
        url={HelpPage.DeckOptions.newCards}
        slot="tooltip"
        {helpSections}
        fsrs={$fsrs}
        on:mount={(e) => {
            modal = e.detail.modal;
            carousel = e.detail.carousel;
        }}
    />
    <DynamicallySlottable slotHost={Item} {api}>
        {#if intervalSettings && $shown("learningSteps", "section")}
            <Item>
                <StepsInputRow
                    bind:value={$config.learnSteps}
                    defaultValue={defaults.learnSteps}
                >
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(
                                Object.keys(settings).indexOf("learningSteps"),
                            )}
                    >
                        {settings.learningSteps.title}
                    </SettingTitle>
                </StepsInputRow>
            </Item>

            <Item>
                <Warning warning={stepsTooLarge} />
            </Item>
        {/if}

        {#if intervalSettings && $fsrs && maxSameDayReviewsShown($config)}
            {#if $shown("maxSameDayReviews", "section")}
                <Item>
                    <SpinBoxRow
                        bind:value={maxSameDayReviews}
                        defaultValue={MAX_SAME_DAY_REVIEWS_NO_LIMIT}
                    >
                        <SettingTitle
                            on:click={() =>
                                openHelpModal(
                                    Object.keys(settings).indexOf("maxSameDayReviews"),
                                )}
                        >
                            {settings.maxSameDayReviews.title}
                        </SettingTitle>
                    </SpinBoxRow>
                </Item>
            {/if}
        {/if}

        {#if $shown("insertionOrder", "section")}
            <Item>
                <EnumSelectorRow
                    bind:value={$config.newCardInsertOrder}
                    defaultValue={defaults.newCardInsertOrder}
                    choices={newInsertOrderChoices()}
                    breakpoint={"md"}
                >
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(
                                Object.keys(settings).indexOf("insertionOrder"),
                            )}
                    >
                        {settings.insertionOrder.title}
                    </SettingTitle>
                </EnumSelectorRow>
            </Item>

            <Item>
                <Warning warning={insertionOrderRandom} />
            </Item>
        {/if}
    </DynamicallySlottable>
</TitledContainer>
