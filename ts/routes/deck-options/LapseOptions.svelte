<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { DeckConfig_Config_LeechAction } from "@generated/anki/deck_config_pb";
    import * as tr from "@generated/ftl";
    import { HelpPage } from "@tslib/help-page";
    import type Carousel from "bootstrap/js/dist/carousel";
    import type Modal from "bootstrap/js/dist/modal";

    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import EnumSelectorRow from "$lib/components/EnumSelectorRow.svelte";
    import HelpModal from "$lib/components/HelpModal.svelte";
    import Item from "$lib/components/Item.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import SwitchRow from "$lib/components/SwitchRow.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import type { HelpItem } from "$lib/components/types";

    import { leechChoices } from "./choices";
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

    // RWKV-Instant has no intervals, so a step has nothing to measure
    // (spec sched.rwkv-instant-no-steps)
    $: intervalSettings = intervalSettingsApply($config);

    let stepsTooLarge: string;
    $: {
        const lastRelearnStepInDays = $config.relearnSteps.length
            ? $config.relearnSteps[$config.relearnSteps.length - 1] / 60 / 24
            : 0;
        stepsTooLarge = stepsTooLargeWarning($config, $fsrs, lastRelearnStepInDays);
    }

    const settings = {
        relearningSteps: {
            title: tr.deckConfigRelearningSteps(),
            help: tr.deckConfigRelearningStepsTooltip(),
            url: HelpPage.DeckOptions.relearningSteps,
        },
        leechThreshold: {
            title: tr.schedulingLeechThreshold(),
            help: tr.deckConfigLeechThresholdTooltip(),
            url: HelpPage.Leeches.leeches,
        },
        leechAction: {
            title: tr.schedulingLeechAction(),
            help: tr.deckConfigLeechActionTooltip(),
            url: HelpPage.Leeches.waiting,
        },
        leechOnlyIfYoung: {
            title: tr.deckConfigLeechOnlyIfYoung(),
            help: tr.deckConfigLeechOnlyIfYoungTooltip(),
            url: HelpPage.Leeches.waiting,
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

<TitledContainer title={tr.schedulingLapses()}>
    <HelpModal
        title={tr.schedulingLapses()}
        url={HelpPage.DeckOptions.lapses}
        slot="tooltip"
        fsrs={$fsrs}
        {helpSections}
        on:mount={(e) => {
            modal = e.detail.modal;
            carousel = e.detail.carousel;
        }}
    />
    <DynamicallySlottable slotHost={Item} {api}>
        {#if intervalSettings && $shown("relearningSteps", "section")}
            <Item>
                <StepsInputRow
                    bind:value={$config.relearnSteps}
                    defaultValue={defaults.relearnSteps}
                >
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(
                                Object.keys(settings).indexOf("relearningSteps"),
                            )}
                    >
                        {settings.relearningSteps.title}
                    </SettingTitle>
                </StepsInputRow>
            </Item>

            <Item>
                <Warning warning={stepsTooLarge} />
            </Item>
        {/if}

        <!-- Same-day reviews for (re)learning steps are always allowed
             (spec sched.same-day-steps-always-on); there is no switch.
             "Skip learning/relearning queues" is a Preferences setting
             (spec deck-options.collection-wide-in-preferences). -->

        {#if $shown("leechThreshold", "section")}
            <Item>
                <SpinBoxRow
                    bind:value={$config.leechThreshold}
                    defaultValue={defaults.leechThreshold}
                    min={1}
                >
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(
                                Object.keys(settings).indexOf("leechThreshold"),
                            )}
                    >
                        {settings.leechThreshold.title}
                    </SettingTitle>
                </SpinBoxRow>
            </Item>
        {/if}

        {#if $shown("leechAction", "section")}
            <Item>
                <EnumSelectorRow
                    bind:value={$config.leechAction}
                    defaultValue={defaults.leechAction}
                    choices={leechChoices()}
                    breakpoint="md"
                >
                    <SettingTitle
                        on:click={() =>
                            openHelpModal(Object.keys(settings).indexOf("leechAction"))}
                    >
                        {settings.leechAction.title}
                    </SettingTitle>
                </EnumSelectorRow>
            </Item>
        {/if}

        {#if $config.leechAction === DeckConfig_Config_LeechAction.SUSPEND}
            {#if $shown("leechOnlyIfYoung", "section")}
                <Item>
                    <SwitchRow
                        bind:value={$config.leechOnlyIfYoung}
                        defaultValue={defaults.leechOnlyIfYoung}
                    >
                        <SettingTitle
                            on:click={() =>
                                openHelpModal(
                                    Object.keys(settings).indexOf("leechOnlyIfYoung"),
                                )}
                        >
                            {settings.leechOnlyIfYoung.title}
                        </SettingTitle>
                    </SwitchRow>
                </Item>
            {/if}
        {/if}
    </DynamicallySlottable>
</TitledContainer>
