<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { type CardStatsResponse_StatsRevlogEntry as RevlogEntry } from "@generated/anki/stats_pb";
    import * as tr from "@generated/ftl";
    import Graph from "../graphs/Graph.svelte";
    import NoDataOverlay from "../graphs/NoDataOverlay.svelte";
    import AxisTicks from "../graphs/AxisTicks.svelte";
    import { writable, type Writable } from "svelte/store";
    import InputBox from "../graphs/InputBox.svelte";
    import {
        renderForgettingCurve,
        TimeRange,
        calculateMaxDays,
        chartRevlog,
        CurveAlgorithm,
        curveInputs,
        forgettingCurveMessage,
        offersCurveToggle,
        type RwkvCurvePoints,
    } from "./forgetting-curve";
    import { defaultGraphBounds } from "../graphs/graph-helpers";
    import HoverColumns from "../graphs/HoverColumns.svelte";

    export let revlog: RevlogEntry[];
    export let desiredRetention: number;
    export let fsrsParams: number[] = [];
    export let rwkvCurve: RwkvCurvePoints | undefined = undefined;
    /** FSRS-7's own reviews of an RWKV-Curve card, sent only in Advanced
     * mode, for the FSRS-7 / RWKV-Curve toggle (spec ui.card-info-rwkv-curve). */
    export let fsrs7Revlog: RevlogEntry[] = [];
    // Simple mode's tooltip says "Probability of recall" (spec
    // ui.simple-recall-wording)
    /** Whether the tooltip says "probability of recall" instead of
     * "retrievability" (spec ui.simple-recall-wording). */
    export let plainRecall: boolean = false;
    let svg: HTMLElement | SVGElement | null = null;
    const bounds = defaultGraphBounds();
    const title = tr.cardStatsFsrsForgettingCurveTitle();

    // the collection's algorithm first; the other one only on request
    let chosen = CurveAlgorithm.RwkvCurve;
    $: showsToggle = offersCurveToggle(rwkvCurve, fsrs7Revlog);
    $: drawn = curveInputs(revlog, rwkvCurve, fsrs7Revlog, chosen);

    $: filteredRevlog = chartRevlog(drawn.revlog, drawn.rwkvCurve);
    // why there is no curve, in plain words, instead of "NO DATA"
    // (spec ui.card-info-curve-messages)
    $: emptyMessage = forgettingCurveMessage(drawn.revlog, drawn.rwkvCurve);
    $: maxDays = calculateMaxDays(filteredRevlog, TimeRange.AllTime);

    let defaultTimeRange = TimeRange.Week;
    const timeRange: Writable<TimeRange> = writable(defaultTimeRange);

    $: if (maxDays > 365) {
        defaultTimeRange = TimeRange.AllTime;
    } else if (maxDays > 30) {
        defaultTimeRange = TimeRange.Year;
    } else if (maxDays > 7) {
        defaultTimeRange = TimeRange.Month;
    }

    $: $timeRange = defaultTimeRange;

    $: renderForgettingCurve(
        filteredRevlog,
        $timeRange,
        svg as SVGElement,
        bounds,
        desiredRetention,
        fsrsParams,
        drawn.rwkvCurve,
        plainRecall,
    );
</script>

<div class="forgetting-curve">
    {#if showsToggle}
        <InputBox>
            <div class="time-range-selector">
                <label>
                    <input
                        type="radio"
                        bind:group={chosen}
                        value={CurveAlgorithm.RwkvCurve}
                    />
                    {tr.deckConfigSchedulerChoiceRwkvCurve()}
                </label>
                <label>
                    <input
                        type="radio"
                        bind:group={chosen}
                        value={CurveAlgorithm.Fsrs7}
                    />
                    {tr.deckConfigSchedulerChoiceFsrs()}
                </label>
            </div>
        </InputBox>
    {/if}
    {#if maxDays > 7}
        <InputBox>
            <div class="time-range-selector">
                <label>
                    <input
                        type="radio"
                        bind:group={$timeRange}
                        value={TimeRange.Week}
                    />
                    {tr.cardStatsFsrsForgettingCurveFirstWeek()}
                </label>
                <label>
                    <input
                        type="radio"
                        bind:group={$timeRange}
                        value={TimeRange.Month}
                    />
                    {tr.cardStatsFsrsForgettingCurveFirstMonth()}
                </label>
                {#if maxDays > 30}
                    <label>
                        <input
                            type="radio"
                            bind:group={$timeRange}
                            value={TimeRange.Year}
                        />
                        {tr.cardStatsFsrsForgettingCurveFirstYear()}
                    </label>
                {/if}
                {#if maxDays > 365}
                    <label>
                        <input
                            type="radio"
                            bind:group={$timeRange}
                            value={TimeRange.AllTime}
                        />
                        {tr.cardStatsFsrsForgettingCurveAllTime()}
                    </label>
                {/if}
            </div>
        </InputBox>
    {/if}
    <Graph {title}>
        <svg bind:this={svg} viewBox={`0 0 ${bounds.width} ${bounds.height}`}>
            <AxisTicks {bounds} />
            <HoverColumns />
            <NoDataOverlay {bounds} text={emptyMessage} />
        </svg>
    </Graph>
</div>

<style>
    .forgetting-curve {
        width: 100%;
        max-width: 50em;
        margin-bottom: 10em;
    }

    .time-range-selector {
        display: flex;
        justify-content: space-around;
        margin-bottom: 1em;
        width: 100%;
        max-width: 50em;
    }

    .time-range-selector label {
        display: flex;
        align-items: center;
    }

    .time-range-selector input {
        margin-right: 0.5em;
    }
</style>
