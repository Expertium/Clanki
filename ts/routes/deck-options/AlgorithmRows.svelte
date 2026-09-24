<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { DeckConfig_Config } from "@generated/anki/deck_config_pb";
    import { get } from "svelte/store";

    import EnumSelectorRow from "$lib/components/EnumSelectorRow.svelte";
    import Item from "$lib/components/Item.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";

    import type { AlgorithmHelpKey } from "./algorithm-help";
    import { algorithmHelpSettings } from "./algorithm-help";
    import FsrsOptions from "./FsrsOptions.svelte";
    import GlobalLabel from "./GlobalLabel.svelte";
    import type { DeckOptionsState } from "./lib";
    import {
        newGatherPriorityWithoutRetrievability,
        reviewOrderForAlgorithm,
    } from "./review-order";
    import type { Placement } from "./ui-split";
    import {
        flagsFromSchedulerChoice,
        SchedulingAlgorithm,
        schedulerChoices,
    } from "./scheduler-choice";

    /**
     * The Algorithm block: the collection's one algorithm (Advanced mode
     * only; a global setting, spec sched.one-global-algorithm) and the FSRS
     * options. Hosted by the Advanced-mode Algorithm section
     * (FsrsOptionsOuter) and by the Simple-mode page (SimpleOptions), which
     * own the help modal and receive the help key to open.
     */
    export let state: DeckOptionsState;
    export let openHelp: (key: AlgorithmHelpKey) => void;
    /** Where these rows are drawn (ui-split.ts). */
    export let placement: Placement = "section";

    let fsrsOptionsComponent: FsrsOptions | undefined;
    export function onPresetChange() {
        if (fsrsOptionsComponent) {
            fsrsOptionsComponent.onPresetChange();
        }
    }

    const fsrs = state.fsrs;
    const config = state.currentConfig;
    const shown = state.settingShown;
    const schedulingAlgorithm = state.schedulingAlgorithm;
    const settings = algorithmHelpSettings();

    // Every preset carries the collection's algorithm; a preset that does
    // not (both RWKV modes on, or saved by an older client) takes it. FSRS
    // is always on; SM-2 is not an algorithm here (spec
    // deck-options.scheduler-choice).
    // Store writes happen inside plain functions so no reactive declaration
    // depends on another one that it also writes to.
    function normalizeSchedulerFlags(
        current: DeckConfig_Config,
        fsrsOn: boolean,
        algorithm: SchedulingAlgorithm,
    ): void {
        const flags = flagsFromSchedulerChoice(algorithm);
        if (
            current.rwkvReviewEnabled !== flags.rwkvCurve ||
            current.rwkvReviewInstantOrderEnabled !== flags.rwkvInstant
        ) {
            config.update((c) => {
                c.rwkvReviewEnabled = flags.rwkvCurve;
                c.rwkvReviewInstantOrderEnabled = flags.rwkvInstant;
                return c;
            });
        }
        if (!fsrsOn) {
            fsrs.set(true);
        }
        // Difficulty orders are FSRS-only; under RWKV a stored one reads as
        // the default order (spec deck-options.no-difficulty-order-under-rwkv).
        const rwkv = flags.rwkvCurve || flags.rwkvInstant;
        const order = reviewOrderForAlgorithm(current.reviewOrder, rwkv);
        if (order !== current.reviewOrder) {
            config.update((c) => {
                c.reviewOrder = order;
                return c;
            });
        }
        // No algorithm ranks new cards by retrievability: a stored
        // retrievability new-card order reads as the default (spec
        // deck-options.no-new-card-retrievability-order).
        const gather = newGatherPriorityWithoutRetrievability(
            current.newCardGatherPriority,
        );
        if (gather !== current.newCardGatherPriority) {
            config.update((c) => {
                c.newCardGatherPriority = gather;
                return c;
            });
        }
    }
    $: normalizeSchedulerFlags($config, $fsrs, $schedulingAlgorithm);

    let choice: SchedulingAlgorithm = get(schedulingAlgorithm);
    // Runs on every change of the dropdown value; a no-op when the value is
    // already the collection's algorithm.
    function applyChoice(value: SchedulingAlgorithm): void {
        if (value !== get(schedulingAlgorithm)) {
            state.setSchedulingAlgorithm(value);
        }
    }
    $: applyChoice(choice);
    const choices = schedulerChoices();
</script>

<!-- Advanced-only by default (ui-split.ts). The one global setting on this page, so it carries a
     "(global)" mark and the globe (spec ui.global-marker). The manual RWKV-Curve
     reschedule action is gone: the "Reschedule cards when desired retention
     changes" Preferences setting covers every algorithm
     (spec deck-options.reschedule-on-change). -->
{#if $shown("algorithm", placement)}
    <Item>
        <EnumSelectorRow
            bind:value={choice}
            defaultValue={SchedulingAlgorithm.RWKV_CURVE}
            {choices}
        >
            <SettingTitle on:click={() => openHelp("fsrs")}>
                <GlobalLabel title={settings.fsrs.title} />
            </SettingTitle>
        </EnumSelectorRow>
    </Item>
{/if}

{#if $fsrs}
    <FsrsOptions
        bind:this={fsrsOptionsComponent}
        {state}
        {placement}
        openHelpModal={(key) => openHelp(key)}
        {onPresetChange}
    />
{/if}
