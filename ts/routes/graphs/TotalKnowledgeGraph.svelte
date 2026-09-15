<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { DeckConfigsForUpdate_SchedulingAlgorithm as SchedulingAlgorithm } from "@generated/anki/deck_config_pb";
    import {
        TotalKnowledgeRwkvJob,
        TotalKnowledgeRwkvProgress,
        TotalKnowledgeRwkvProgress_State as RwkvState,
        TotalKnowledgeRwkvRequest,
        type TotalKnowledgeResponse,
    } from "@generated/anki/stats_pb";
    import { totalKnowledge } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { postProto } from "@generated/post";
    import { getContext, onDestroy } from "svelte";
    import { type Readable, readable } from "svelte/store";

    import AxisTicks from "./AxisTicks.svelte";
    import Graph from "./Graph.svelte";
    import { defaultGraphBounds } from "./graph-helpers";
    import NoDataOverlay from "./NoDataOverlay.svelte";
    import {
        hasDrawing,
        isRwkv,
        KNOWN_COLOUR,
        overlayText,
        renderTotalKnowledge,
        REVIEWED_COLOUR,
        rwkvStillComputing,
        totalKnowledgeData,
    } from "./total-knowledge";

    const pollDelayMs = 500;
    const bounds = defaultGraphBounds();
    // the page's search sets the cards; its period does not apply
    const search =
        getContext<Readable<string> | undefined>("graphsSearch") ??
        readable("deck:current");

    let svg: SVGElement | null = null;
    let response: TotalKnowledgeResponse | null = null;
    let rwkv: TotalKnowledgeRwkvProgress | null = null;
    let loadError: string | undefined = undefined;
    let requestId = 0;
    let pollTimer: number | undefined;
    let restarted = false;

    $: load($search);
    $: data = response ? totalKnowledgeData(response, rwkv) : null;
    $: if (svg) {
        renderTotalKnowledge(svg, bounds, hasDrawing(response, rwkv) ? data : null);
    }
    $: overlay = loadError ?? overlayText(response, rwkv);

    function stopPolling(): void {
        if (pollTimer !== undefined) {
            window.clearTimeout(pollTimer);
            pollTimer = undefined;
        }
    }

    function cancelJob(): void {
        if (rwkvStillComputing(rwkv) && rwkv) {
            postProto(
                "totalKnowledgeRwkvCancel",
                new TotalKnowledgeRwkvJob({ jobId: rwkv.jobId }),
                TotalKnowledgeRwkvProgress,
                { alertOnError: false },
            ).catch(() => undefined);
        }
    }

    function failed(error: unknown): TotalKnowledgeRwkvProgress {
        return new TotalKnowledgeRwkvProgress({
            state: RwkvState.FAILED,
            error: String(error),
        });
    }

    async function load(search: string): Promise<void> {
        const id = ++requestId;
        stopPolling();
        cancelJob();
        response = null;
        rwkv = null;
        loadError = undefined;
        let loaded: TotalKnowledgeResponse;
        try {
            loaded = await totalKnowledge({ search }, { alertOnError: false });
        } catch (error) {
            if (id === requestId) {
                loadError = String(error);
            }
            return;
        }
        if (id !== requestId) {
            return;
        }
        response = loaded;
        if (!isRwkv(loaded.algorithm) || !loaded.reviewedCards.length) {
            return;
        }
        restarted = false;
        await startRwkv(
            search,
            loaded.algorithm === SchedulingAlgorithm.RWKV_CURVE,
            id,
        );
    }

    async function startRwkv(
        search: string,
        curve: boolean,
        id: number,
    ): Promise<void> {
        let progress: TotalKnowledgeRwkvProgress;
        try {
            progress = await postProto(
                "totalKnowledgeRwkvStart",
                new TotalKnowledgeRwkvRequest({ search, curve }),
                TotalKnowledgeRwkvProgress,
                { alertOnError: false },
            );
        } catch (error) {
            progress = failed(error);
        }
        showProgress(progress, id, () => startRwkv(search, curve, id));
    }

    function showProgress(
        progress: TotalKnowledgeRwkvProgress,
        id: number,
        restart: () => void,
    ): void {
        if (id !== requestId) {
            return;
        }
        // stopped from outside while this page still shows it (the page was
        // redrawn, say by a mode switch, and the old graph's cancel came
        // late): ask once more
        if (progress.state === RwkvState.CANCELLED && !restarted) {
            restarted = true;
            restart();
            return;
        }
        rwkv = progress;
        if (rwkvStillComputing(progress)) {
            pollTimer = window.setTimeout(
                () => poll(progress.jobId, id, restart),
                pollDelayMs,
            );
        }
    }

    async function poll(jobId: number, id: number, restart: () => void): Promise<void> {
        pollTimer = undefined;
        let progress: TotalKnowledgeRwkvProgress;
        try {
            progress = await postProto(
                "totalKnowledgeRwkvProgress",
                new TotalKnowledgeRwkvJob({ jobId }),
                TotalKnowledgeRwkvProgress,
                { alertOnError: false },
            );
        } catch (error) {
            progress = failed(error);
        }
        showProgress(progress, id, restart);
    }

    // leaving the page stops RWKV's job
    onDestroy(() => {
        requestId++;
        stopPolling();
        cancelJob();
    });

    const title = tr.statisticsTotalKnowledgeTitle();
    const subtitle = tr.statisticsTotalKnowledgeSubtitle();
</script>

<Graph {title} {subtitle}>
    <div class="legend">
        <span>
            <span class="swatch" style={`background-color: ${KNOWN_COLOUR}`}></span>
            {tr.statisticsTotalKnowledgeKnown()}
        </span>
        <span>
            <span class="swatch" style={`background-color: ${REVIEWED_COLOUR}`}></span>
            {tr.statisticsTotalKnowledgeReviewed()}
        </span>
        {#if rwkvStillComputing(rwkv)}
            <span class="computing">{tr.cardStatsCalculating()}</span>
        {/if}
    </div>
    <svg bind:this={svg} viewBox={`0 0 ${bounds.width} ${bounds.height}`}>
        <g class="total-knowledge" />
        <AxisTicks {bounds} />
        <NoDataOverlay {bounds} text={overlay} />
    </svg>
</Graph>

<style lang="scss">
    .legend {
        display: flex;
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

    .swatch {
        width: 0.8rem;
        height: 0.8rem;
        border-radius: 2px;
    }

    .computing {
        opacity: 0.7;
    }
</style>
