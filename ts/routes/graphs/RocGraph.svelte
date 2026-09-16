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
    import NoDataOverlay from "./NoDataOverlay.svelte";
    import {
        chanceLabel,
        dataNotes,
        overlayText,
        renderRoc,
        rocBounds,
        rocCurves,
        stillComputing,
        unavailableNotes,
    } from "./roc";

    const pollDelayMs = 500;
    const bounds = rocBounds();
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
    $: curves = rocCurves(progress);
    $: notes = [...dataNotes(progress), ...unavailableNotes(progress)];
    $: if (svg) {
        renderRoc(svg, bounds, curves);
    }
    $: overlay = overlayText(progress);

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

    const title = tr.statisticsRocTitle();
    const subtitle = tr.statisticsRocSubtitle();
</script>

<Graph {title} {subtitle}>
    <div class="legend">
        {#each curves as curve (curve.algorithm)}
            <span>
                <span class="swatch" style={`background-color: ${curve.colour}`}></span>
                {curve.label}
            </span>
        {/each}
        <span>
            <span class="swatch chance"></span>
            {chanceLabel()}
        </span>
        {#if stillComputing(progress)}
            <span class="computing">{tr.cardStatsCalculating()}</span>
        {/if}
    </div>
    <div class="square">
        <svg bind:this={svg} viewBox={`0 0 ${bounds.width} ${bounds.height}`}>
            <g class="roc" />
            <NoDataOverlay {bounds} text={overlay} />
        </svg>
    </div>
    <div class="description">
        <div>{tr.statisticsRocDescriptionCurve()}</div>
        <div>{tr.statisticsRocDescriptionAuc()}</div>
        <div>{tr.statisticsModelMetricsDescriptionReviews()}</div>
        {#each notes as note}
            <div class="note">{note}</div>
        {/each}
    </div>
</Graph>

<style lang="scss">
    .legend {
        display: flex;
        flex-wrap: wrap;
        gap: 1rem;
        align-items: center;
        justify-content: center;
        margin-top: 0.25rem;
        font-size: 0.9rem;
    }

    .legend span {
        display: inline-flex;
        gap: 0.35rem;
        align-items: center;
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

    .swatch {
        width: 0.8rem;
        height: 0.8rem;
        border-radius: 2px;
    }

    .swatch.chance {
        height: 0;
        border-top: 2px dashed #8a8a8a;
        border-radius: 0;
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
