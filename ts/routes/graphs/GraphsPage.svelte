<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { ConfigKey_Bool } from "@generated/anki/config_pb";
    import type { GraphsRequest_Graph } from "@generated/anki/stats_pb";
    import { getConfigBool } from "@generated/backend";
    import { bridgeCommand } from "@tslib/bridgecommand";
    import type { RecallWording } from "@tslib/recall-wording";
    import { loadSimpleItems, type SimpleItems } from "@tslib/ui-split";
    import type { Component } from "svelte";
    import { setContext } from "svelte";
    import { writable } from "svelte/store";

    import { pageTheme } from "$lib/sveltelib/theme";

    import RangeBox from "./RangeBox.svelte";
    import type { GraphItem } from "./ui-mode";
    import { graphsForMode, simpleDataOf, simpleGraphsOf } from "./ui-mode";
    import UiModeFromData from "./UiModeFromData.svelte";
    import WithGraphData from "./WithGraphData.svelte";

    export let initialSearch: string;
    export let initialDays: number;

    const search = writable(initialSearch);
    const days = writable(initialDays);
    // for graphs that load their own data, such as Total Knowledge and the
    // model-quality graphs
    setContext("graphsSearch", search);
    setContext("graphsDays", days);

    export let graphs: Component<any>[];
    /** Each graph's item of the Simple | Advanced split and the data it
     * draws, in the order of `graphs`; null = every graph in both modes
     * (spec ui.mode-switch, ui.split-configurable). */
    export let graphItems: GraphItem[] | null = null;
    /** See RangeBox */
    export let controller: Component<any> | null = RangeBox;

    /** The collection's UI mode: read when the page loads, then from the
     * data and the page's switch. */
    const advancedUi = writable(true);
    // for graphs that show different controls in each mode, such as Total
    // Knowledge (spec ui.mode-switch)
    setContext("graphsAdvancedUi", advancedUi);
    /** The recall wording, for the graphs that load their own data and so
     * never see the response that carries it (spec ui.simple-recall-wording). */
    const recallWording = writable<RecallWording | undefined>(undefined);
    setContext("graphsRecallWording", recallWording);
    let modeKnown = false;
    /** See UiModeFromData */
    let switchChoice: boolean | null = null;
    const modeSwitch = {
        subscribe: advancedUi.subscribe,
        set(advanced: boolean): void {
            switchChoice = advanced;
            advancedUi.set(advanced);
        },
        update(updater: (advanced: boolean) => boolean): void {
            modeSwitch.set(updater($advancedUi));
        },
    };

    /** The graphs to ask the backend for: [] = every graph; null = not
     * known yet, until the page knows its mode. */
    const hasSimpleView = graphItems !== null;
    /** The graphs Simple mode shows, once the split is read. */
    let simpleGraphs: Component<any>[] | null = null;
    let simpleData: GraphsRequest_Graph[] = [];
    const wanted = writable<GraphsRequest_Graph[] | null>(hasSimpleView ? null : []);
    if (graphItems !== null) {
        const items = graphItems;
        let split: SimpleItems | null = null;
        Promise.all([
            getConfigBool({ key: ConfigKey_Bool.ADVANCED_UI }, { alertOnError: false })
                .then(({ val }) => advancedUi.set(val))
                .catch(() => {}),
            loadSimpleItems().then((loaded) => (split = loaded)),
        ]).finally(() => {
            simpleGraphs = simpleGraphsOf(graphs, items, split);
            simpleData = simpleDataOf(items, split);
            wanted.set($advancedUi ? [] : simpleData);
        });
    }
    // Advanced mode draws every graph; Simple mode keeps the data it has
    $: if ($advancedUi && $wanted !== null && $wanted.length > 0) {
        wanted.set([]);
    }

    function browserSearch(event: CustomEvent) {
        bridgeCommand(`browserSearch: ${$search} ${event.detail.query}`);
    }
</script>

<WithGraphData
    {search}
    {days}
    graphs={wanted}
    let:sourceData
    let:sourceComplete
    let:loading
    let:prefs
    let:revlogRange
>
    {#if sourceData}
        <UiModeFromData
            data={sourceData}
            {advancedUi}
            {recallWording}
            bind:switchChoice
            bind:known={modeKnown}
        />
    {/if}
    {#if controller}
        <svelte:component
            this={controller}
            {search}
            {days}
            {loading}
            advancedUi={simpleGraphs && modeKnown ? modeSwitch : null}
        />
    {/if}

    <div class="graphs-container">
        {#if sourceData && revlogRange}
            <!-- the Simple graphs until the data of every graph is in;
            keyed by the component, so a graph both modes show keeps its
            block, and with it whatever it has loaded on its own (Total
            Knowledge's request and its RWKV job) across a mode switch -->
            {#each graphsForMode(graphs, simpleGraphs, $advancedUi && sourceComplete) as graph (graph)}
                <svelte:component
                    this={graph}
                    {sourceData}
                    {prefs}
                    {revlogRange}
                    nightMode={$pageTheme.isDark}
                    on:search={browserSearch}
                />
            {/each}
        {/if}
    </div>
    <div class="spacer"></div>
</WithGraphData>

<style lang="scss">
    .graphs-container {
        display: grid;
        gap: 1em;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        // required on Safari to stretch whole width
        width: calc(100vw - 3em);
        margin-left: 1em;
        margin-right: 1em;

        @media only screen and (max-width: 600px) {
            width: calc(100vw - 1rem);
            margin-left: 0.5rem;
            margin-right: 0.5rem;
        }

        @media only screen and (max-width: 1400px) {
            grid-template-columns: 1fr 1fr;
        }
        @media only screen and (max-width: 1200px) {
            grid-template-columns: 1fr;
        }
        @media only screen and (max-width: 600px) {
            font-size: 12px;
        }

        @media only print {
            // grid layout does not honor page-break-inside
            display: block;
            margin-top: 3em;
        }
    }

    .spacer {
        height: 1.5em;
    }
</style>
