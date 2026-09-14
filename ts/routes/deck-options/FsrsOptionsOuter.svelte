<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { DeckConfig_Config } from "@generated/anki/deck_config_pb";
    import * as tr from "@generated/ftl";
    import { HelpPage } from "@tslib/help-page";
    import type Carousel from "bootstrap/js/dist/carousel";
    import type Modal from "bootstrap/js/dist/modal";
    import { get } from "svelte/store";

    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import HelpModal from "$lib/components/HelpModal.svelte";
    import Item from "$lib/components/Item.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import EnumSelectorRow from "$lib/components/EnumSelectorRow.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import { type HelpItem, HelpItemScheduler } from "$lib/components/types";

    import FsrsOptions from "./FsrsOptions.svelte";
    import GlobalLabel from "./GlobalLabel.svelte";
    import type { DeckOptionsState } from "./lib";
    import {
        flagsFromSchedulerChoice,
        SchedulerChoice,
        schedulerChoiceFromFlags,
        schedulerChoices,
    } from "./scheduler-choice";

    export let state: DeckOptionsState;
    export let api: Record<string, never>;

    let fsrsOptionsComponent: FsrsOptions | undefined;
    export function onPresetChange() {
        if (fsrsOptionsComponent) {
            fsrsOptionsComponent.onPresetChange();
        }
    }

    const fsrs = state.fsrs;
    const config = state.currentConfig;
    const advanced = state.deckOptionsAdvanced;
    let newlyEnabled = false;

    // A stored preset with both RWKV modes on cannot be represented by the
    // dropdown: RWKV-Curve wins. Every RWKV mode implies FSRS on. Both are
    // normalized on load (spec deck-options.scheduler-choice). Store writes
    // happen inside plain functions so no reactive declaration depends on
    // another one that it also writes to.
    function normalizeSchedulerFlags(
        current: DeckConfig_Config,
        fsrsOn: boolean,
    ): void {
        if (current.rwkvReviewEnabled && current.rwkvReviewInstantOrderEnabled) {
            config.update((c) => {
                c.rwkvReviewInstantOrderEnabled = false;
                return c;
            });
        }
        if (
            (current.rwkvReviewEnabled || current.rwkvReviewInstantOrderEnabled) &&
            !fsrsOn
        ) {
            fsrs.set(true);
        }
    }
    $: normalizeSchedulerFlags($config, $fsrs);

    let schedulerChoice: SchedulerChoice = SchedulerChoice.FSRS;
    $: schedulerChoice = schedulerChoiceFromFlags({
        fsrs: $fsrs,
        rwkvCurve: $config.rwkvReviewEnabled,
        rwkvInstant: $config.rwkvReviewInstantOrderEnabled,
    });

    // Runs on every change of the dropdown value; a no-op when the stored
    // flags already read as that value, so loading a preset changes nothing.
    function applySchedulerChoice(choice: SchedulerChoice): void {
        const current = get(config);
        const stored = schedulerChoiceFromFlags({
            fsrs: get(fsrs),
            rwkvCurve: current.rwkvReviewEnabled,
            rwkvInstant: current.rwkvReviewInstantOrderEnabled,
        });
        if (stored === choice) {
            return;
        }
        const flags = flagsFromSchedulerChoice(choice);
        fsrs.set(flags.fsrs);
        config.update((c) => {
            c.rwkvReviewEnabled = flags.rwkvCurve;
            c.rwkvReviewInstantOrderEnabled = flags.rwkvInstant;
            return c;
        });
    }
    $: applySchedulerChoice(schedulerChoice);
    $: schedulerChoiceList = schedulerChoices({
        advanced: $advanced,
        current: schedulerChoice,
    });
    $: if (!$fsrs) {
        newlyEnabled = true;
    }

    const settings = {
        fsrs: {
            title: tr.deckConfigScheduler(),
            help: tr.deckConfigSchedulerTooltip(),
            url: HelpPage.DeckOptions.fsrs,
            global: true,
        },
        desiredRetention: {
            title: tr.deckConfigDesiredRetention(),
            help:
                tr.deckConfigDesiredRetentionTooltip() +
                "\n\n" +
                tr.deckConfigDesiredRetentionTooltip2(),
            sched: HelpItemScheduler.FSRS,
        },
        modelParams: {
            title: tr.deckConfigWeights(),
            help:
                tr.deckConfigWeightsTooltip2() +
                "\n\n" +
                tr.deckConfigComputeOptimalWeightsTooltip2(),
            sched: HelpItemScheduler.FSRS,
        },
        rescheduleCardsOnChange: {
            title: tr.deckConfigRescheduleCardsOnChange(),
            help: tr.deckConfigRescheduleCardsOnChangeTooltip(),
            sched: HelpItemScheduler.FSRS,
            global: true,
        },
        healthCheck: {
            title: tr.deckConfigHealthCheck(),
            help:
                tr.deckConfigAffectsEntireCollection() +
                "\n\n" +
                tr.deckConfigHealthCheckTooltip1() +
                "\n\n" +
                tr.deckConfigHealthCheckTooltip2(),
            sched: HelpItemScheduler.FSRS,
            global: true,
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

<TitledContainer title={"Scheduler"}>
    <HelpModal
        title={"Scheduler"}
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
        <Item>
            <EnumSelectorRow
                bind:value={schedulerChoice}
                defaultValue={SchedulerChoice.FSRS}
                choices={schedulerChoiceList}
            >
                <SettingTitle
                    on:click={() =>
                        openHelpModal(Object.keys(settings).indexOf("fsrs"))}
                >
                    <GlobalLabel title={settings.fsrs.title} />
                </SettingTitle>
            </EnumSelectorRow>
        </Item>

        {#if $fsrs}
            <FsrsOptions
                bind:this={fsrsOptionsComponent}
                {state}
                {newlyEnabled}
                openHelpModal={(key) =>
                    openHelpModal(Object.keys(settings).indexOf(key))}
                {onPresetChange}
            />
        {/if}
    </DynamicallySlottable>
</TitledContainer>
