<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import {
        ReviewMetricsJob,
        ReviewMetricsProgress,
        ReviewMetricsProgress_State as JobState,
        ReviewMetricsRequest,
    } from "@generated/anki/stats_pb";
    import * as tr from "@generated/ftl";
    import { postProto } from "@generated/post";
    import { getContext, onDestroy } from "svelte";
    import { type Readable, readable } from "svelte/store";

    import Graph from "./Graph.svelte";
    import { chosenUmPlusPair, showSmallUmPlusGroups } from "./metrics-choice";
    import NoDataOverlay from "./NoDataOverlay.svelte";
    import { dataNotes, overlayText, stillComputing, unavailableNotes } from "./roc";
    import {
        chosenPair,
        pairKey,
        pairOptions,
        renderUmPlus,
        umPlusBounds,
        umPlusView,
    } from "./um-plus";

    const pollDelayMs = 500;
    const bounds = umPlusBounds();
    const search =
        getContext<Readable<string> | undefined>("graphsSearch") ??
        readable("deck:current");
    const days =
        getContext<Readable<number> | undefined>("graphsDays") ?? readable(365);

    let svg: SVGElement | null = null;
    let progress: ReviewMetricsProgress | null = null;
    let requestId = 0;
    let pollTimer: number | undefined;

    $: load($search, $days);
    $: options = pairOptions(progress);
    $: pair = chosenPair(progress, $chosenUmPlusPair);
    $: view = umPlusView(pair, $showSmallUmPlusGroups);
    $: notes = [...dataNotes(progress), ...unavailableNotes(progress)];
    $: if (svg) {
        renderUmPlus(svg, bounds, view);
    }
    $: overlay = view ? undefined : overlayText(progress);

    function choose(event: Event): void {
        chosenUmPlusPair.set((event.target as HTMLSelectElement).value);
    }

    function stopPolling(): void {
        if (pollTimer !== undefined) {
            window.clearTimeout(pollTimer);
            pollTimer = undefined;
        }
    }

    function cancelJob(): void {
        if (stillComputing(progress) && progress) {
            postProto(
                "reviewMetricsCancel",
                new ReviewMetricsJob({ jobId: progress.jobId }),
                ReviewMetricsProgress,
                { alertOnError: false },
            ).catch(() => undefined);
        }
    }

    function failed(error: unknown): ReviewMetricsProgress {
        return new ReviewMetricsProgress({
            state: JobState.FAILED,
            error: String(error),
        });
    }

    async function load(search: string, days: number): Promise<void> {
        const id = ++requestId;
        stopPolling();
        cancelJob();
        progress = null;
        let started: ReviewMetricsProgress;
        try {
            started = await postProto(
                "reviewMetricsStart",
                new ReviewMetricsRequest({ search, days }),
                ReviewMetricsProgress,
                { alertOnError: false },
            );
        } catch (error) {
            started = failed(error);
        }
        show(started, id);
    }

    function show(next: ReviewMetricsProgress, id: number): void {
        if (id !== requestId) {
            return;
        }
        progress = next;
        if (stillComputing(next)) {
            pollTimer = window.setTimeout(() => poll(next.jobId, id), pollDelayMs);
        }
    }

    async function poll(jobId: number, id: number): Promise<void> {
        pollTimer = undefined;
        let next: ReviewMetricsProgress;
        try {
            next = await postProto(
                "reviewMetricsProgress",
                new ReviewMetricsJob({ jobId }),
                ReviewMetricsProgress,
                { alertOnError: false },
            );
        } catch (error) {
            next = failed(error);
        }
        show(next, id);
    }

    // leaving the page stops the job
    onDestroy(() => {
        requestId++;
        stopPolling();
        cancelJob();
    });

    const title = tr.statisticsUmPlusTitle();
    const subtitle = tr.statisticsUmPlusSubtitle();
</script>

<Graph {title} {subtitle}>
    <div class="controls">
        <label>
            {tr.statisticsUmPlusAlgorithms()}
            <select on:change={choose} value={pair ? pairKey(pair) : ""}>
                {#each options as option (option.key)}
                    <option value={option.key}>{option.label}</option>
                {/each}
            </select>
        </label>
        <label class="small-groups">
            <input type="checkbox" bind:checked={$showSmallUmPlusGroups} />
            {tr.statisticsUmPlusSmallGroups()}
        </label>
        {#if stillComputing(progress)}
            <span class="computing">{tr.cardStatsCalculating()}</span>
        {/if}
    </div>
    {#if view}
        <div class="legend">
            <span>
                <span class="swatch" style={`background-color: ${view.colourA}`}></span>
                {view.labelA}
            </span>
            <span>
                <span class="swatch" style={`background-color: ${view.colourB}`}></span>
                {view.labelB}
            </span>
        </div>
    {/if}
    <svg bind:this={svg} viewBox={`0 0 ${bounds.width} ${bounds.height}`}>
        <g class="um-plus" />
        <NoDataOverlay {bounds} text={overlay} />
    </svg>
    <div class="description">
        <div>{tr.statisticsUmPlusDescriptionAxes()}</div>
        <div>{tr.statisticsUmPlusDescriptionScore()}</div>
        <div>{tr.statisticsModelMetricsDescriptionReviews()}</div>
        {#if view && view.hidden > 0}
            <div>{tr.statisticsUmPlusHidden({ groups: String(view.hidden) })}</div>
        {/if}
        {#each notes as note}
            <div class="note">{note}</div>
        {/each}
    </div>
</Graph>

<style lang="scss">
    .controls,
    .legend {
        display: flex;
        flex-wrap: wrap;
        gap: 1rem;
        align-items: center;
        justify-content: center;
        margin-top: 0.25rem;
        font-size: 0.9rem;
    }

    .controls label,
    .legend span {
        display: inline-flex;
        gap: 0.35rem;
        align-items: center;
    }

    .small-groups {
        cursor: pointer;
    }

    .swatch {
        width: 0.8rem;
        height: 0.8rem;
        border-radius: 2px;
    }

    .description {
        margin-top: 0.5rem;
        text-align: center;
        font-size: 0.85rem;
        opacity: 0.8;
    }

    .note {
        margin-top: 0.25rem;
        opacity: 0.9;
    }

    .computing {
        opacity: 0.7;
    }
</style>
