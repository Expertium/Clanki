<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import {
        type DeckConfig_Config,
        UpdateDeckConfigsMode,
    } from "@generated/anki/deck_config_pb";
    import { DeckId } from "@generated/anki/decks_pb";
    import { Empty } from "@generated/anki/generic_pb";
    import { postProto } from "@generated/post";
    import { get } from "svelte/store";

    import Item from "$lib/components/Item.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";
    import EnumSelectorRow from "$lib/components/EnumSelectorRow.svelte";

    import type { AlgorithmHelpKey } from "./algorithm-help";
    import { algorithmHelpSettings } from "./algorithm-help";
    import FsrsOptions from "./FsrsOptions.svelte";
    import GlobalLabel from "./GlobalLabel.svelte";
    import { commitEditing, type DeckOptionsState } from "./lib";
    import {
        flagsFromSchedulerChoice,
        SchedulerChoice,
        schedulerChoiceFromFlags,
        schedulerChoices,
    } from "./scheduler-choice";

    /**
     * The Algorithm block: the dropdown, the RWKV-Curve reschedule action and
     * the FSRS options. Hosted by the Advanced-mode Algorithm section
     * (FsrsOptionsOuter) and by the Simple-mode page (SimpleOptions), which
     * own the help modal and receive the help key to open.
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
    $: if (!$fsrs) {
        newlyEnabled = true;
    }

    // The RWKV-Curve reschedule action: saves, then asks the desktop to
    // rewrite review intervals with the current RWKV-Curve predictions.
    let reschedulingRwkvReviewCards = false;
    async function rescheduleRwkvReviewCards(): Promise<void> {
        reschedulingRwkvReviewCards = true;
        try {
            await commitEditing();
            await state.save(UpdateDeckConfigsMode.NORMAL);
            await postProto(
                "rescheduleRwkvReviewCards",
                new DeckId({ did: state.getTargetDeckId() }),
                Empty,
            );
        } finally {
            reschedulingRwkvReviewCards = false;
        }
    }
</script>

<Item>
    <EnumSelectorRow
        bind:value={schedulerChoice}
        defaultValue={SchedulerChoice.FSRS}
        choices={schedulerChoiceList}
    >
        <SettingTitle on:click={() => openHelp("fsrs")}>
            <GlobalLabel title={settings.fsrs.title} />
        </SettingTitle>
    </EnumSelectorRow>
</Item>

{#if $config.rwkvReviewEnabled}
    <Item>
        <button
            class="btn btn-outline-primary"
            disabled={reschedulingRwkvReviewCards}
            on:click={() => rescheduleRwkvReviewCards()}
        >
            {#if reschedulingRwkvReviewCards}
                Rescheduling Cards with RWKV-Curve Intervals...
            {:else}
                Reschedule Cards with RWKV-Curve Intervals
            {/if}
        </button>
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
