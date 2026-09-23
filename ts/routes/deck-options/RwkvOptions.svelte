<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { UpdateDeckConfigsMode } from "@generated/anki/deck_config_pb";
    import { Empty } from "@generated/anki/generic_pb";
    import * as tr from "@generated/ftl";
    import { postProto } from "@generated/post";
    import type Carousel from "bootstrap/js/dist/carousel";
    import type Modal from "bootstrap/js/dist/modal";

    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import HelpModal from "$lib/components/HelpModal.svelte";
    import Item from "$lib/components/Item.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import SwitchRow from "$lib/components/SwitchRow.svelte";
    import RetrievabilityText from "$lib/components/RetrievabilityText.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import type { HelpItem } from "$lib/components/types";

    import { commitEditing, type DeckOptionsState } from "./lib";
    import SpinBoxFloatRow from "./SpinBoxFloatRow.svelte";
    import { helpForMode } from "./ui-split";

    export let state: DeckOptionsState;

    const config = state.currentConfig;
    const defaults = state.defaults;
    // which settings show (ui-split.ts); in Advanced mode, every one
    const shown = state.settingShown;
    // what names retrievability shows in Advanced mode only: the
    // recommendation line, and rwkvMinimumReviewsPerDay with its help (spec
    // ui.retrievability-advanced-only)
    const advancedUi = state.advancedUi;

    let forceBuildingRwkvStateCache = false;
    let recomputingRwkvCalibrationData = false;
    $: rwkvActionInProgress =
        forceBuildingRwkvStateCache || recomputingRwkvCalibrationData;

    $: settings = {
        rwkvReview: {
            title: tr.deckConfigRwkvReviewEnabled(),
            help: tr.deckConfigRwkvReviewEnabledTooltip(),
        },
        rwkvEnforceGradeOrder: {
            title: tr.deckConfigRwkvReviewEnforceGradeOrder(),
            help: tr.deckConfigRwkvReviewEnforceGradeOrderTooltip(),
        },
        rwkvInstantOrder: {
            title: tr.deckConfigRwkvReviewInstantOrder(),
            help: tr.deckConfigRwkvReviewInstantOrderTooltip(),
        },
        rwkvMinimumReviewsPerDay: {
            title: tr.deckConfigRwkvReviewMinimumReviewsPerDay(),
            help: tr.deckConfigRwkvReviewMinimumReviewsPerDayTooltip(),
        },
        rwkvCandidateRefresh: {
            title: tr.deckConfigRwkvReviewCandidateRefresh(),
            help: tr.deckConfigRwkvReviewCandidateRefreshTooltip(),
        },
        rwkvRefreshInterval: {
            title: tr.deckConfigRwkvReviewRefreshInterval(),
            help: tr.deckConfigRwkvReviewRefreshIntervalTooltip(),
        },
        rwkvRefreshOnExit: {
            title: tr.deckConfigRwkvReviewRefreshOnExit(),
            help: tr.deckConfigRwkvReviewRefreshOnExitTooltip(),
        },
        rwkvAllowSameDayReview: {
            title: tr.deckConfigRwkvReviewAllowSameDayReview(),
            help: tr.deckConfigRwkvReviewAllowSameDayReviewTooltip(),
        },
        rwkvMinInterveningReviews: {
            title: tr.deckConfigRwkvReviewMinInterveningReviews(),
            help: tr.deckConfigRwkvReviewMinInterveningReviewsTooltip(),
        },
        rwkvMinElapsedSecs: {
            title: tr.deckConfigRwkvReviewMinElapsedSecs(),
            help: tr.deckConfigRwkvReviewMinElapsedSecsTooltip(),
        },
    };
    $: help = helpForMode<HelpItem>(settings, $advancedUi);
    $: settingKeys = Object.keys(help);
    $: helpSections = Object.values(help);

    let modal: Modal;
    let carousel: Carousel;

    function openHelpModal(index: number): void {
        modal.show();
        carousel.to(index);
    }

    function openSettingHelp(key: string): void {
        openHelpModal(settingKeys.indexOf(key));
    }

    async function forceBuildRwkvStateCache(): Promise<void> {
        forceBuildingRwkvStateCache = true;
        try {
            await saveRwkvDeckOptions();
            await postProto("forceBuildRwkvStateCache", new Empty({}), Empty);
        } finally {
            forceBuildingRwkvStateCache = false;
        }
    }

    async function recomputeRwkvCalibrationData(): Promise<void> {
        recomputingRwkvCalibrationData = true;
        try {
            await saveRwkvDeckOptions();
            await postProto("recomputeRwkvCalibrationData", new Empty({}), Empty);
        } finally {
            recomputingRwkvCalibrationData = false;
        }
    }

    // never a bare "RWKV" (spec ui.rwkv-algorithm-names)
    $: boxTitle = $config.rwkvReviewInstantOrderEnabled
        ? tr.deckConfigSchedulerChoiceRwkvInstant()
        : tr.deckConfigSchedulerChoiceRwkvCurve();

    async function saveRwkvDeckOptions(): Promise<void> {
        await commitEditing();
        await state.save(UpdateDeckConfigsMode.NORMAL);
    }
</script>

<!-- The RWKV-Curve reschedule action lives under Algorithm; this container
     only has content for RWKV-Instant or under advanced options
     (spec deck-options.advanced-view). -->
{#if $config.rwkvReviewInstantOrderEnabled || $config.rwkvReviewEnabled}
    <TitledContainer title={boxTitle}>
        <HelpModal
            title={boxTitle}
            url=""
            slot="tooltip"
            {helpSections}
            on:mount={(e) => {
                modal = e.detail.modal;
                carousel = e.detail.carousel;
            }}
        />
        <DynamicallySlottable slotHost={Item} api={{}}>
            {#if $config.rwkvReviewEnabled}
                {#if $shown("rwkvEnforceGradeOrder", "section")}
                    <Item>
                        <SwitchRow
                            bind:value={$config.rwkvReviewEnforceGradeOrder}
                            defaultValue={defaults.rwkvReviewEnforceGradeOrder}
                        >
                            <SettingTitle
                                on:click={() =>
                                    openSettingHelp("rwkvEnforceGradeOrder")}
                            >
                                {tr.deckConfigRwkvReviewEnforceGradeOrder()}
                            </SettingTitle>
                        </SwitchRow>
                    </Item>
                {/if}
            {/if}

            {#if $config.rwkvReviewInstantOrderEnabled}
                <h2 class="rwkv-subheading">Review Queue — RWKV-Instant</h2>
                {#if $advancedUi}
                    <span class="rwkv-recommendation">
                        <RetrievabilityText
                            text={tr.deckConfigRwkvReviewInstantOrderRecommended()}
                        />
                    </span>
                {/if}

                {#if $shown("rwkvAllowSameDayReview", "section")}
                    <SwitchRow
                        bind:value={$config.rwkvReviewAllowSameDayReview}
                        defaultValue={defaults.rwkvReviewAllowSameDayReview}
                    >
                        <SettingTitle
                            on:click={() => openSettingHelp("rwkvAllowSameDayReview")}
                        >
                            {tr.deckConfigRwkvReviewAllowSameDayReview()}
                        </SettingTitle>
                    </SwitchRow>
                {/if}

                {#if $config.rwkvReviewAllowSameDayReview}
                    {#if $shown("rwkvMinInterveningReviews", "section")}
                        <SpinBoxFloatRow
                            bind:value={$config.rwkvReviewMinInterveningReviews}
                            defaultValue={defaults.rwkvReviewMinInterveningReviews}
                            min={0}
                            max={10000}
                            step={1}
                        >
                            <SettingTitle
                                on:click={() =>
                                    openSettingHelp("rwkvMinInterveningReviews")}
                            >
                                {tr.deckConfigRwkvReviewMinInterveningReviews()}
                            </SettingTitle>
                        </SpinBoxFloatRow>
                    {/if}

                    {#if $shown("rwkvMinElapsedSecs", "section")}
                        <SpinBoxFloatRow
                            bind:value={$config.rwkvReviewMinElapsedSecs}
                            defaultValue={defaults.rwkvReviewMinElapsedSecs}
                            min={0}
                            max={86400}
                            step={1}
                        >
                            <SettingTitle
                                on:click={() => openSettingHelp("rwkvMinElapsedSecs")}
                            >
                                {tr.deckConfigRwkvReviewMinElapsedSecs()}
                            </SettingTitle>
                        </SpinBoxFloatRow>
                    {/if}
                {/if}

                {#if $shown("rwkvMinimumReviewsPerDay", "section")}
                    <SpinBoxFloatRow
                        bind:value={$config.rwkvReviewMinimumReviewsPerDay}
                        defaultValue={defaults.rwkvReviewMinimumReviewsPerDay}
                        min={0}
                        max={9999}
                        step={1}
                    >
                        <SettingTitle
                            on:click={() => openSettingHelp("rwkvMinimumReviewsPerDay")}
                        >
                            {tr.deckConfigRwkvReviewMinimumReviewsPerDay()}
                        </SettingTitle>
                    </SpinBoxFloatRow>
                {/if}

                {#if $shown("rwkvCandidateRefresh", "section")}
                    <SwitchRow
                        bind:value={$config.rwkvReviewCandidateRefreshEnabled}
                        defaultValue={defaults.rwkvReviewCandidateRefreshEnabled}
                    >
                        <SettingTitle
                            on:click={() => openSettingHelp("rwkvCandidateRefresh")}
                        >
                            {tr.deckConfigRwkvReviewCandidateRefresh()}
                        </SettingTitle>
                    </SwitchRow>
                {/if}

                {#if $shown("rwkvRefreshInterval", "section")}
                    <SpinBoxFloatRow
                        bind:value={$config.rwkvReviewRefreshInterval}
                        defaultValue={defaults.rwkvReviewRefreshInterval}
                        min={1}
                        max={10000}
                        step={1}
                    >
                        <SettingTitle
                            on:click={() => openSettingHelp("rwkvRefreshInterval")}
                        >
                            {tr.deckConfigRwkvReviewRefreshInterval()}
                        </SettingTitle>
                    </SpinBoxFloatRow>
                {/if}

                {#if $shown("rwkvRefreshOnExit", "section")}
                    <SwitchRow
                        bind:value={$config.rwkvReviewRefreshOnExit}
                        defaultValue={defaults.rwkvReviewRefreshOnExit}
                    >
                        <SettingTitle
                            on:click={() => openSettingHelp("rwkvRefreshOnExit")}
                        >
                            {tr.deckConfigRwkvReviewRefreshOnExit()}
                        </SettingTitle>
                    </SwitchRow>
                {/if}
            {/if}

            <!-- "Predict R for new cards based on creation time" and "Dynamic
                 Preset Addon Support" are gone; both are fixed at their
                 defaults (spec deck-options.rwkv-fixed-settings). -->
            {#if $shown("rwkvMaintenance", "section")}
                <h2 class="rwkv-subheading">Maintenance</h2>

                <div class="d-flex flex-wrap gap-2">
                    <button
                        class="btn btn-outline-primary"
                        disabled={rwkvActionInProgress}
                        on:click={() => forceBuildRwkvStateCache()}
                    >
                        {#if forceBuildingRwkvStateCache}
                            {tr.deckConfigRwkvRereadingHistory()}
                        {:else}
                            {tr.deckConfigRwkvRereadHistory()}
                        {/if}
                    </button>

                    <button
                        class="btn btn-outline-primary"
                        disabled={rwkvActionInProgress}
                        on:click={() => recomputeRwkvCalibrationData()}
                    >
                        {#if recomputingRwkvCalibrationData}
                            {tr.deckConfigRwkvPreparingStats()}
                        {:else}
                            {tr.deckConfigRwkvPrepareStats()}
                        {/if}
                    </button>
                </div>
            {/if}
        </DynamicallySlottable>
    </TitledContainer>
{/if}

<style>
    .rwkv-subheading {
        color: var(--fg-subtle);
        font-size: 0.875rem;
        font-weight: 600;
        margin: 1rem 0 0.25rem;
    }

    .rwkv-recommendation {
        color: var(--fg-subtle);
        display: block;
        font-size: 0.875rem;
    }

    .btn {
        margin-bottom: 0.375rem;
    }
</style>
