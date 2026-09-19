<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
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

    import {
        calibrationBounds,
        calibrationSeries,
        chooserOptions,
        chosenAlgorithm,
        renderCalibration,
        tiles,
    } from "./calibration";
    import Graph from "./Graph.svelte";
    import NoDataOverlay from "./NoDataOverlay.svelte";
    import { chosenCalibrationAlgorithm } from "./metrics-choice";
    import { dataNotes, overlayText, stillComputing, unavailableNotes } from "./roc";

    const pollDelayMs = 500;
    const bounds = calibrationBounds();
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
    $: options = chooserOptions(progress);
    $: drawn = chosenAlgorithm(progress, $chosenCalibrationAlgorithm);
    $: series = calibrationSeries(progress, $chosenCalibrationAlgorithm);
    $: panelTiles = tiles(series);
    $: notes = [...dataNotes(progress), ...unavailableNotes(progress)];
    $: if (svg) {
        renderCalibration(svg, bounds, series);
    }
    $: overlay = series ? undefined : overlayText(progress);

    function choose(event: Event): void {
        const value = Number((event.target as HTMLSelectElement).value);
        chosenCalibrationAlgorithm.set(value as SchedulingAlgorithm);
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

    const title = tr.statisticsCalibrationTitle();
    const subtitle = tr.statisticsCalibrationSubtitle();
</script>

<Graph {title} {subtitle}>
    <div class="chooser">
        <label>
            {tr.statisticsCalibrationAlgorithm()}
            <select on:change={choose} value={drawn ?? ""}>
                {#each options as option (option.algorithm)}
                    <option value={option.algorithm} disabled={!option.available}>
                        {option.label}
                    </option>
                {/each}
            </select>
        </label>
        {#if stillComputing(progress)}
            <span class="computing">{tr.cardStatsCalculating()}</span>
        {/if}
    </div>
    <div class="tiles">
        {#each panelTiles as tile (tile.label)}
            <div class="tile">
                <div class="value">{tile.value}</div>
                <div class="label">{tile.label}</div>
            </div>
        {/each}
    </div>
    <div class="square">
        <svg bind:this={svg} viewBox={`0 0 ${bounds.width} ${bounds.height}`}>
            <g class="calibration" />
            <!-- only a message: with no text the overlay says "No data" over the graph -->
            {#if overlay}
                <NoDataOverlay {bounds} text={overlay} />
            {/if}
        </svg>
    </div>
    <div class="description">
        <div>{tr.statisticsCalibrationDescriptionLine()}</div>
        <div>{tr.statisticsCalibrationDescriptionBars()}</div>
        <div>{tr.statisticsModelMetricsDescriptionReviews()}</div>
        {#each notes as note}
            <div class="note">{note}</div>
        {/each}
    </div>
</Graph>

<style lang="scss">
    .chooser {
        display: flex;
        gap: 1rem;
        align-items: center;
        justify-content: center;
        margin-top: 0.25rem;
        font-size: 0.9rem;
    }

    .chooser label {
        display: inline-flex;
        gap: 0.35rem;
        align-items: center;
    }

    .tiles {
        display: flex;
        gap: 1.5rem;
        justify-content: center;
        margin: 0.5rem 0;
    }

    .tile {
        text-align: center;
    }

    .tile .value {
        font-size: 1.1rem;
        font-weight: bold;
    }

    .tile .label {
        font-size: 0.8rem;
        opacity: 0.8;
    }

    /* the drawing area keeps its 1:1 shape at every width */
    .square {
        width: 100%;
        margin: 0 auto;
    }

    .square svg {
        width: 100%;
        height: auto;
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
