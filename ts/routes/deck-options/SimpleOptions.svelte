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
    import { applyPlayAudio, playAudioFromConfig } from "./autoplay-switch";
    import AlgorithmRows from "./AlgorithmRows.svelte";
    import { hideRelatedCardsHelp, hideRelatedCardsTitle } from "./bury-siblings";
    import HideRelatedCardsRow from "./HideRelatedCardsRow.svelte";
    import DailyLimitRows from "./DailyLimitRows.svelte";
    import EasyDaysRows from "./EasyDaysRows.svelte";
    import type { DeckOptionsState } from "./lib";

    /**
     * The Simple-mode page's own section: by default the settings listed in
     * spec/deck-options.md, `deck-options.simple-view`, in that order; the
     * split decides which show, and adds the daily-limit and Algorithm
     * settings a user moves to Simple mode (ui-split.ts). Advanced mode shows
     * the per-topic sections instead (DeckOptionsPage).
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
    // which of this section's settings the split shows (ui-split.ts)
    const shown = state.settingShown;

    // "Play audio automatically" is `disableAutoplay` turned the other way
    // round (spec deck-options.play-audio-switch).
    let playAudio = playAudioFromConfig($config);
    $: playAudio = playAudioFromConfig($config);
    function setPlayAudio(on: boolean): void {
        if (playAudioFromConfig(get(config)) !== on) {
            config.update((current) => applyPlayAudio(current, on));
        }
    }
    $: setPlayAudio(playAudio);

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
            title: hideRelatedCardsTitle(),
            help: hideRelatedCardsHelp(),
            url: HelpPage.Studying.siblingsAndBurying,
        },
        disableAutoplay: {
            title: tr.deckConfigPlayAudioAutomatically(),
            help: tr.deckConfigPlayAudioAutomaticallyTooltip(),
            url: HelpPage.DeckOptions.audio,
        },
        onScreenTimer: {
            title: tr.deckConfigOnScreenTimer(),
            help: tr.deckConfigShowAnswerTimerTooltip(),
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
        <DailyLimitRows
            {state}
            {openHelp}
            placement="simple"
            bind:this={dailyLimitRows}
        />

        <AlgorithmRows
            {state}
            {openHelp}
            placement="simple"
            bind:this={algorithmRows}
        />

        {#if $shown("burySiblings", "simple")}
            <Item>
                <HideRelatedCardsRow
                    {config}
                    {defaults}
                    onHelp={() => openHelp("burySiblings")}
                />
            </Item>
        {/if}

        {#if $shown("playAudio", "simple")}
            <Item>
                <SwitchRow
                    bind:value={playAudio}
                    defaultValue={playAudioFromConfig(defaults)}
                >
                    <SettingTitle on:click={() => openHelp("disableAutoplay")}>
                        {settings.disableAutoplay.title}
                    </SettingTitle>
                </SwitchRow>
            </Item>
        {/if}

        <!-- "Skip question when replaying answer" is Advanced-only by default
             (spec deck-options.simple-view); added to Simple mode, it shows
             in its Audio section below this one (ui-split.ts). -->

        {#if $shown("showTimer", "simple")}
            <Item>
                <!-- AnkiMobile hides this -->
                <div class="show-timer-switch" style="display: contents;">
                    <SwitchRow
                        bind:value={$config.showTimer}
                        defaultValue={defaults.showTimer}
                    >
                        <SettingTitle on:click={() => openHelp("onScreenTimer")}>
                            {settings.onScreenTimer.title}
                        </SettingTitle>
                    </SwitchRow>
                </div>
            </Item>
        {/if}

        {#if $shown("easyDays", "simple")}
            <EasyDaysRows {state} openHelp={() => openHelp("easyDays")} />
        {/if}
    </DynamicallySlottable>
</TitledContainer>

<style lang="scss">
    .partly-on {
        color: var(--fg-subtle);
        font-size: 0.85em;
        margin-top: -0.25em;
    }
</style>
