<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { GraphsRequest_Graph } from "@generated/anki/stats_pb";
    import { GraphsRequest, GraphsResponse } from "@generated/anki/stats_pb";
    import { getGraphPreferences, setGraphPreferences } from "@generated/backend";
    import { postProtoWithResponse } from "@generated/post";
    import { onDestroy, tick } from "svelte";
    import { type Writable, writable } from "svelte/store";

    import { autoSavingPrefs } from "$lib/sveltelib/preferences";

    import { daysToRevlogRange } from "./graph-helpers";

    export let search: Writable<string>;
    export let days: Writable<number>;
    /** The graphs to ask the backend for: [] = every graph; null = none yet
     * (the page does not know yet which graphs it shows). */
    export let graphs: Writable<GraphsRequest_Graph[] | null> = writable([]);

    const prefsPromise = autoSavingPrefs(
        () => getGraphPreferences({}),
        setGraphPreferences,
    );
    const rwkvStatsPendingHeader = "X-Anki-Rwkv-Stats-Pending";
    const rwkvStatsRetryDelayMs = 2_000;
    // while RWKV calculates, the page keeps asking until the scores arrive:
    // it shows "Calculating…", never another algorithm's values (spec
    // ui.stats-one-algorithm)
    const rwkvStatsMaxRetries = Number.POSITIVE_INFINITY;

    let sourceData: GraphsResponse | null = null;
    /** Whether sourceData holds every graph. */
    let sourceComplete = false;
    let loading = true;
    let activeRequestId = 0;
    let inFlightKey = "";
    let inFlightGraphs: Promise<GraphDataResponse> | null = null;
    let currentSearch = $search;
    let currentDays = $days;
    let currentGraphs = $graphs;
    let pendingSearch = $search;
    let pendingDays = $days;
    let pendingGraphs: GraphsRequest_Graph[] = [];
    let updateScheduled = false;
    let rwkvStatsRetryKey = "";
    let rwkvStatsRetryCount = 0;
    let rwkvStatsRetryTimer: number | undefined;
    $: currentSearch = $search;
    $: currentDays = $days;
    $: currentGraphs = $graphs;
    $: if ($graphs !== null) {
        scheduleSourceDataUpdate($search, $days, $graphs);
    }

    interface GraphDataResponse {
        data: GraphsResponse;
        rwkvStatsPending: boolean;
    }

    function graphDataKey(
        search: string,
        days: number,
        graphs: GraphsRequest_Graph[],
    ): string {
        return `${days}\0${graphs.join(",")}\0${search}`;
    }

    function graphDebugLoggingEnabled(): boolean {
        return (
            typeof location !== "undefined" &&
            new URLSearchParams(location.search).has("graphDebug")
        );
    }

    function formatGraphDetails(details: Record<string, unknown>): string {
        return JSON.stringify(details);
    }

    function logGraphTiming(message: string, details: Record<string, unknown>): void {
        const text = `${message}: ${formatGraphDetails(details)}`;
        if (graphDebugLoggingEnabled()) {
            console.warn(text);
        } else {
            console.debug(text);
        }
    }

    function graphData(
        search: string,
        days: number,
        graphs: GraphsRequest_Graph[],
    ): Promise<GraphDataResponse> {
        const key = graphDataKey(search, days, graphs);
        if (inFlightGraphs && inFlightKey === key) {
            return inFlightGraphs;
        }

        inFlightKey = key;
        const start = performance.now();
        logGraphTiming("graphs request started", { search, days, graphs });
        inFlightGraphs = postProtoWithResponse(
            "graphs",
            new GraphsRequest({ search, days, graphs }),
            GraphsResponse,
        )
            .then(({ output, headers }) => ({
                data: output,
                rwkvStatsPending: headers.get(rwkvStatsPendingHeader) === "1",
            }))
            .finally(() => {
                logGraphTiming("graphs request finished", {
                    search,
                    days,
                    elapsedMs: performance.now() - start,
                });
                if (inFlightKey === key) {
                    inFlightGraphs = null;
                }
            });
        return inFlightGraphs;
    }

    function clearRwkvStatsRetryTimer(): void {
        if (rwkvStatsRetryTimer != null) {
            window.clearTimeout(rwkvStatsRetryTimer);
            rwkvStatsRetryTimer = undefined;
        }
    }

    function resetRwkvStatsRetryForKey(key: string): void {
        if (rwkvStatsRetryKey !== key) {
            clearRwkvStatsRetryTimer();
            rwkvStatsRetryKey = key;
            rwkvStatsRetryCount = 0;
        }
    }

    function handleRwkvStatsRetry(
        search: string,
        days: number,
        graphs: GraphsRequest_Graph[],
        requestId: number,
        rwkvStatsPending: boolean,
    ): boolean {
        const key = graphDataKey(search, days, graphs);
        resetRwkvStatsRetryForKey(key);
        if (!rwkvStatsPending) {
            clearRwkvStatsRetryTimer();
            rwkvStatsRetryCount = 0;
            return false;
        }
        if (rwkvStatsRetryTimer != null) {
            return true;
        }
        if (rwkvStatsRetryCount >= rwkvStatsMaxRetries) {
            logGraphTiming("graphs RWKV stats retry exhausted", {
                search,
                days,
                requestId,
                retries: rwkvStatsRetryCount,
            });
            return false;
        }

        rwkvStatsRetryCount += 1;
        const retry = rwkvStatsRetryCount;
        logGraphTiming("graphs RWKV stats retry scheduled", {
            search,
            days,
            requestId,
            retry,
            delayMs: rwkvStatsRetryDelayMs,
        });
        rwkvStatsRetryTimer = window.setTimeout(() => {
            rwkvStatsRetryTimer = undefined;
            if (
                requestId !== activeRequestId ||
                search !== currentSearch ||
                days !== currentDays ||
                graphs !== currentGraphs
            ) {
                logGraphTiming("graphs RWKV stats retry ignored", {
                    search,
                    days,
                    requestId,
                    activeRequestId,
                });
                return;
            }
            logGraphTiming("graphs RWKV stats retry started", {
                search,
                days,
                requestId,
                retry,
            });
            scheduleSourceDataUpdate(search, days, graphs);
        }, rwkvStatsRetryDelayMs);
        return true;
    }

    function scheduleSourceDataUpdate(
        search: string,
        days: number,
        graphs: GraphsRequest_Graph[],
    ): void {
        pendingSearch = search;
        pendingDays = days;
        pendingGraphs = graphs;
        resetRwkvStatsRetryForKey(graphDataKey(search, days, graphs));
        activeRequestId += 1;
        if (updateScheduled) {
            return;
        }

        updateScheduled = true;
        Promise.resolve().then(() => {
            updateScheduled = false;
            updateSourceData(
                pendingSearch,
                pendingDays,
                pendingGraphs,
                activeRequestId,
            );
        });
    }

    async function updateSourceData(
        search: string,
        days: number,
        graphs: GraphsRequest_Graph[],
        requestId: number,
    ): Promise<void> {
        // ensure the fast-loading preferences come first
        await prefsPromise;
        if (requestId !== activeRequestId) {
            return;
        }
        const start = performance.now();
        let applied = false;
        let slowTimer: number | undefined;
        loading = true;
        if (graphDebugLoggingEnabled()) {
            const slowTimerDetails = (): Record<string, unknown> => ({
                search,
                days,
                requestId,
                activeRequestId,
                elapsedMs: performance.now() - start,
            });
            slowTimer = window.setTimeout(() => {
                console.warn(
                    `graphs frontend still loading: ${formatGraphDetails(
                        slowTimerDetails(),
                    )}`,
                );
            }, 2000);
        }
        try {
            const data = await graphData(search, days, graphs);
            logGraphTiming("graphs data received", {
                search,
                days,
                requestId,
                activeRequestId,
                elapsedMs: performance.now() - start,
                rwkvStatsPending: data.rwkvStatsPending,
            });
            if (requestId === activeRequestId) {
                const applyStart = performance.now();
                sourceData = data.data;
                sourceComplete = graphs.length === 0;
                const retryPending = handleRwkvStatsRetry(
                    search,
                    days,
                    graphs,
                    requestId,
                    data.rwkvStatsPending,
                );
                loading = retryPending;
                await tick();
                applied = true;
                logGraphTiming("graphs data applied", {
                    search,
                    days,
                    requestId,
                    requestElapsedMs: applyStart - start,
                    applyElapsedMs: performance.now() - applyStart,
                    elapsedMs: performance.now() - start,
                    rwkvStatsPending: data.rwkvStatsPending,
                    retryPending,
                });
            } else {
                logGraphTiming("graphs data ignored", {
                    search,
                    days,
                    requestId,
                    activeRequestId,
                    elapsedMs: performance.now() - start,
                });
            }
        } finally {
            if (slowTimer != null) {
                window.clearTimeout(slowTimer);
            }
            if (!applied && requestId === activeRequestId) {
                loading = false;
            }
        }
    }

    $: revlogRange = daysToRevlogRange($days);

    onDestroy(clearRwkvStatsRetryTimer);
</script>

<!--
We block graphs loading until the preferences have been fetched, so graphs
don't have to worry about a null initial value. We don't do the same for the
graph data, as it gets updated as the user changes options, and we don't want
the current graphs to disappear until the new graphs have finished loading.
-->
{#await prefsPromise then prefs}
    <slot {revlogRange} {prefs} {sourceData} {sourceComplete} {loading} />
{/await}
