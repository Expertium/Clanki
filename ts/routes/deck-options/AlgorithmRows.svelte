<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { DeckConfig_Config } from "@generated/anki/deck_config_pb";
    import { get } from "svelte/store";

    import Item from "$lib/components/Item.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import EnumSelectorRow from "$lib/components/EnumSelectorRow.svelte";

    import type { AlgorithmHelpKey } from "./algorithm-help";
    import { algorithmHelpSettings } from "./algorithm-help";
    import FsrsOptions from "./FsrsOptions.svelte";
    import type { DeckOptionsState } from "./lib";
    import { reviewOrderForAlgorithm } from "./review-order";
    import {
        flagsFromSchedulerChoice,
        SchedulerChoice,
        schedulerChoiceFromFlags,
        schedulerChoices,
    } from "./scheduler-choice";

    /**
     * The Algorithm block: the dropdown (Advanced mode only, spec
     * deck-options.simple-view) and the FSRS options. Hosted by the
     * Advanced-mode Algorithm section (FsrsOptionsOuter) and by the
     * Simple-mode page (SimpleOptions), which own the help modal and receive
     * the help key to open.
     */
    export let state: DeckOptionsState;
    export let openHelp: (key: AlgorithmHelpKey) => void;

    let fsrsOptionsComponent: FsrsOptions | undefined;
    export function onPresetChange() {
        if (fsrsOptionsComponent) {
            fsrsOptionsComponent.onPresetChange();
        }
    }

    const fsrs = state.fsrs;
    const config = state.currentConfig;
    const advancedUi = state.advancedUi;
    const settings = algorithmHelpSettings();
    let newlyEnabled = false;

    // A stored preset with both RWKV modes on cannot be represented by the
    // dropdown: RWKV-Curve wins. FSRS is always on; SM-2 is not selectable
    // here. Both are normalized on load (spec deck-options.scheduler-choice).
    // Store writes happen inside plain functions so no reactive declaration
    // depends on another one that it also writes to.
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
        if (!fsrsOn) {
            fsrs.set(true);
        }
        // Difficulty orders are FSRS-only; under RWKV a stored one reads as
        // the default order (spec deck-options.no-difficulty-order-under-rwkv).
        const rwkv = current.rwkvReviewEnabled || current.rwkvReviewInstantOrderEnabled;
        const order = reviewOrderForAlgorithm(current.reviewOrder, rwkv);
        if (order !== current.reviewOrder) {
            config.update((c) => {
                c.reviewOrder = order;
                return c;
            });
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
    const schedulerChoiceList = schedulerChoices();
    // The revert button restores the new-preset algorithm (RWKV-Curve,
    // spec deck-options.new-preset-defaults).
    const defaultSchedulerChoice = schedulerChoiceFromFlags({
        fsrs: true,
        rwkvCurve: state.defaults.rwkvReviewEnabled,
        rwkvInstant: state.defaults.rwkvReviewInstantOrderEnabled,
    });
    $: if (!$fsrs) {
        newlyEnabled = true;
    }
</script>

<!-- The dropdown is Advanced-only; in Simple mode the preset keeps its
     stored algorithm (new presets: RWKV-Curve). The manual RWKV-Curve
     reschedule action is gone: the "Reschedule cards when desired retention
     changes" Preferences setting covers every algorithm
     (spec deck-options.reschedule-on-change). -->
{#if $advancedUi}
    <Item>
        <EnumSelectorRow
            bind:value={schedulerChoice}
            defaultValue={defaultSchedulerChoice}
            choices={schedulerChoiceList}
        >
            <SettingTitle on:click={() => openHelp("fsrs")}>
                {settings.fsrs.title}
            </SettingTitle>
        </EnumSelectorRow>
    </Item>
{/if}

{#if $fsrs}
    <FsrsOptions
        bind:this={fsrsOptionsComponent}
        {state}
        {newlyEnabled}
        openHelpModal={(key) => openHelp(key)}
        {onPresetChange}
    />
{/if}
