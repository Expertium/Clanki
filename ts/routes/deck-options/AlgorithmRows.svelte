<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { DeckConfig_Config } from "@generated/anki/deck_config_pb";
    import * as tr from "@generated/ftl";

    import Col from "$lib/components/Col.svelte";
    import Item from "$lib/components/Item.svelte";
    import Row from "$lib/components/Row.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";

    import type { AlgorithmHelpKey } from "./algorithm-help";
    import { algorithmHelpSettings } from "./algorithm-help";
    import FsrsOptions from "./FsrsOptions.svelte";
    import type { DeckOptionsState } from "./lib";
    import { reviewOrderForAlgorithm } from "./review-order";
    import { schedulerChoiceFromFlags, schedulerChoiceLabel } from "./scheduler-choice";

    /**
     * The Algorithm block: the collection's algorithm, read-only (Advanced
     * mode only; it is chosen in Preferences, spec
     * sched.one-global-algorithm) and the FSRS options. Hosted by the
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

    // A stored preset with both RWKV modes on reads as RWKV-Curve. FSRS is
    // always on; SM-2 is not an algorithm here. Both are normalized on load
    // (spec deck-options.scheduler-choice).
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

    // every preset carries the collection's algorithm
    $: algorithmLabel = schedulerChoiceLabel(
        schedulerChoiceFromFlags({
            fsrs: $fsrs,
            rwkvCurve: $config.rwkvReviewEnabled,
            rwkvInstant: $config.rwkvReviewInstantOrderEnabled,
        }),
    );
    $: if (!$fsrs) {
        newlyEnabled = true;
    }
</script>

<!-- Advanced-only and read-only: the algorithm is a Preferences setting
     (spec sched.one-global-algorithm). The manual RWKV-Curve reschedule
     action is gone: the "Reschedule cards when desired retention changes"
     Preferences setting covers every algorithm
     (spec deck-options.reschedule-on-change). -->
{#if $advancedUi}
    <Item>
        <Row --cols={13}>
            <Col --col-size={7} breakpoint="md">
                <SettingTitle on:click={() => openHelp("fsrs")}>
                    {settings.fsrs.title}
                </SettingTitle>
            </Col>
            <Col --col-size={6} breakpoint="md">
                {tr.deckConfigAlgorithmSetInPreferences({ algorithm: algorithmLabel })}
            </Col>
        </Row>
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
