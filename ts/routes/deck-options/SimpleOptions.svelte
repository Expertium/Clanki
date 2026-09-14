<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { HelpPage } from "@tslib/help-page";
    import type Carousel from "bootstrap/js/dist/carousel";
    import type Modal from "bootstrap/js/dist/modal";
    import { get } from "svelte/store";

    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import HelpModal from "$lib/components/HelpModal.svelte";
    import Item from "$lib/components/Item.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import SwitchRow from "$lib/components/SwitchRow.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import { type HelpItem, HelpItemScheduler } from "$lib/components/types";

    import { algorithmHelpSettings } from "./algorithm-help";
    import AlgorithmRows from "./AlgorithmRows.svelte";
    import { applyBurySiblings, burySiblingsFromConfig } from "./bury-siblings";
    import DailyLimitRows from "./DailyLimitRows.svelte";
    import EasyDaysRows from "./EasyDaysRows.svelte";
    import type { DeckOptionsState } from "./lib";
    import { applyOnScreenTimer, onScreenTimerFromConfig } from "./timer-switch";

    /**
     * The Simple-mode page: one section with the settings listed in
     * spec/deck-options.md, `deck-options.simple-view`, in that order.
     * Advanced mode shows the per-topic sections instead (DeckOptionsPage).
     */
    export let state: DeckOptionsState;
    export let api: Record<string, never>;

    let dailyLimitRows: DailyLimitRows | undefined;
    let algorithmRows: AlgorithmRows | undefined;
    export function onPresetChange() {
        if (dailyLimitRows) {
            dailyLimitRows.onPresetChange();
        }
        if (algorithmRows) {
            algorithmRows.onPresetChange();
        }
    }

    const config = state.currentConfig;
    const defaults = state.defaults;
    const fsrs = state.fsrs;

    // One switch stands for the three bury settings, and one for the two
    // timer settings. The switch value follows the preset; a toggle writes
    // every setting it stands for; showing a preset writes nothing, because
    // the writes are skipped when the preset already reads as the switch
    // value (the same pattern as the Algorithm dropdown).
    let burySiblings = burySiblingsFromConfig($config);
    $: burySiblings = burySiblingsFromConfig($config);
    function setBurySiblings(on: boolean): void {
        if (burySiblingsFromConfig(get(config)) !== on) {
            config.update((current) => applyBurySiblings(current, on));
        }
    }
    $: setBurySiblings(burySiblings);

    let onScreenTimer = onScreenTimerFromConfig($config);
    $: onScreenTimer = onScreenTimerFromConfig($config);
    function setOnScreenTimer(on: boolean): void {
        if (onScreenTimerFromConfig(get(config)) !== on) {
            config.update((current) => applyOnScreenTimer(current, on));
        }
    }
    $: setOnScreenTimer(onScreenTimer);

    const algorithmHelp = algorithmHelpSettings();
    const settings = {
        newLimit: {
            title: tr.schedulingNewCardsday(),
            help: tr.deckConfigNewLimitTooltip(),
            url: HelpPage.DeckOptions.newCardsday,
        },
        reviewLimit: {
            title: tr.schedulingMaximumReviewsday(),
            help: tr.deckConfigReviewLimitTooltip(),
            url: HelpPage.DeckOptions.maximumReviewsday,
        },
        fsrs: algorithmHelp.fsrs,
        desiredRetention: algorithmHelp.desiredRetention,
        burySiblings: {
            title: tr.deckConfigBurySiblings(),
            help:
                tr.deckConfigBurySiblingsSimpleTooltip() +
                "\n\n" +
                tr.deckConfigBuryNewTooltip() +
                "\n\n" +
                tr.deckConfigBuryReviewTooltip() +
                "\n\n" +
                tr.deckConfigBuryInterdayLearningTooltip() +
                "\n\n" +
                tr.deckConfigBuryPriorityTooltip(),
            url: HelpPage.Studying.siblingsAndBurying,
        },
        disableAutoplay: {
            title: tr.deckConfigDisableAutoplay(),
            help: tr.deckConfigDisableAutoplayTooltip(),
            url: HelpPage.DeckOptions.audio,
        },
        onScreenTimer: {
            title: tr.deckConfigOnScreenTimer(),
            help:
                tr.deckConfigShowAnswerTimerTooltip() +
                "\n\n" +
                tr.deckConfigStopTimerOnAnswerTooltip(),
            url: HelpPage.DeckOptions.timer,
        },
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

    // The Algorithm block also asks for keys of Advanced-only settings; those
    // have no entry here and open nothing.
    function openHelp(key: string): void {
        const index = Object.keys(settings).indexOf(key);
        if (index < 0) {
            return;
        }
        modal.show();
        carousel.to(index);
    }
</script>

<TitledContainer title={tr.deckConfigTitle()}>
    <HelpModal
        title={tr.deckConfigTitle()}
        url="https://docs.ankiweb.net/deck-options.html"
        slot="tooltip"
        fsrs={$fsrs}
        {helpSections}
        on:mount={(e) => {
            modal = e.detail.modal;
            carousel = e.detail.carousel;
        }}
    />
    <DynamicallySlottable slotHost={Item} {api}>
        <DailyLimitRows {state} {openHelp} bind:this={dailyLimitRows} />

        <AlgorithmRows {state} {openHelp} bind:this={algorithmRows} />

        <Item>
            <SwitchRow
                bind:value={burySiblings}
                defaultValue={burySiblingsFromConfig(defaults)}
            >
                <SettingTitle on:click={() => openHelp("burySiblings")}>
                    {settings.burySiblings.title}
                </SettingTitle>
            </SwitchRow>
        </Item>

        <Item>
            <SwitchRow
                bind:value={$config.disableAutoplay}
                defaultValue={defaults.disableAutoplay}
            >
                <SettingTitle on:click={() => openHelp("disableAutoplay")}>
                    {settings.disableAutoplay.title}
                </SettingTitle>
            </SwitchRow>
        </Item>

        <!-- "Skip question when replaying answer" is Advanced-only
             (spec deck-options.simple-view). -->

        <Item>
            <!-- AnkiMobile hides this -->
            <div class="show-timer-switch" style="display: contents;">
                <SwitchRow
                    bind:value={onScreenTimer}
                    defaultValue={onScreenTimerFromConfig(defaults)}
                >
                    <SettingTitle on:click={() => openHelp("onScreenTimer")}>
                        {settings.onScreenTimer.title}
                    </SettingTitle>
                </SwitchRow>
            </div>
        </Item>

        <EasyDaysRows {state} openHelp={() => openHelp("easyDays")} />
    </DynamicallySlottable>
</TitledContainer>
