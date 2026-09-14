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
    import type { HelpItem } from "$lib/components/types";

    import { applyPlayAudio, playAudioFromConfig } from "./autoplay-switch";
    import type { DeckOptionsState } from "./lib";

    export let state: DeckOptionsState;
    export let api: Record<string, never>;

    const config = state.currentConfig;
    const defaults = state.defaults;

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

    const settings = {
        disableAutoplay: {
            title: tr.deckConfigPlayAudioAutomatically(),
            help: tr.deckConfigPlayAudioAutomaticallyTooltip(),
        },
        skipQuestionWhenReplaying: {
            title: tr.deckConfigSkipQuestionWhenReplaying(),
            help: tr.deckConfigAlwaysIncludeQuestionAudioTooltip(),
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

<TitledContainer title={tr.deckConfigAudioTitle()}>
    <HelpModal
        title={tr.deckConfigAudioTitle()}
        url={HelpPage.DeckOptions.audio}
        slot="tooltip"
        {helpSections}
        on:mount={(e) => {
            modal = e.detail.modal;
            carousel = e.detail.carousel;
        }}
    />
    <DynamicallySlottable slotHost={Item} {api}>
        <Item>
            <SwitchRow
                bind:value={playAudio}
                defaultValue={playAudioFromConfig(defaults)}
            >
                <SettingTitle
                    on:click={() =>
                        openHelpModal(Object.keys(settings).indexOf("disableAutoplay"))}
                >
                    {settings.disableAutoplay.title}
                </SettingTitle>
            </SwitchRow>
        </Item>

        <Item>
            <SwitchRow
                bind:value={$config.skipQuestionWhenReplayingAnswer}
                defaultValue={defaults.skipQuestionWhenReplayingAnswer}
            >
                <SettingTitle
                    on:click={() =>
                        openHelpModal(
                            Object.keys(settings).indexOf("skipQuestionWhenReplaying"),
                        )}
                >
                    {settings.skipQuestionWhenReplaying.title}
                </SettingTitle>
            </SwitchRow>
        </Item>
    </DynamicallySlottable>
</TitledContainer>
