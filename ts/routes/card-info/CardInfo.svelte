<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { CardStatsResponse } from "@generated/anki/stats_pb";
    import { plainRecallWording } from "@tslib/recall-wording";

    import Container from "$lib/components/Container.svelte";
    import Row from "$lib/components/Row.svelte";

    import CardInfoPlaceholder from "./CardInfoPlaceholder.svelte";
    import CardStats from "./CardStats.svelte";
    import Revlog from "./Revlog.svelte";
    import ForgettingCurve from "./ForgettingCurve.svelte";
    import { showsForgettingCurve } from "./lib";

    export let stats: CardStatsResponse | null = null;
    export let showRevlog: boolean = true;
    export let showCurve: boolean = true;

    $: fsrsEnabled = stats?.memoryState != null;
    // spec ui.simple-recall-wording
    $: plainRecall = stats
        ? plainRecallWording(stats.recallWording, stats.advancedUi)
        : false;
    $: desiredRetention = stats?.desiredRetention ?? 0.9;
</script>

<Container breakpoint="md" --gutter-inline="1rem" --gutter-block="0.5rem">
    {#if stats}
        <Row>
            <CardStats {stats} />
        </Row>

        {#if showRevlog}
            <Row>
                <Revlog revlog={stats.revlog} {fsrsEnabled} />
            </Row>
        {/if}
        {#if showsForgettingCurve(stats) && showCurve}
            <Row>
                <ForgettingCurve
                    revlog={stats.revlog}
                    {desiredRetention}
                    fsrsParams={stats.fsrsParams}
                    rwkvCurve={stats.rwkvCurve}
                    fsrs7Revlog={stats.fsrs7Revlog}
                    {plainRecall}
                />
            </Row>
        {/if}
    {:else}
        <CardInfoPlaceholder />
    {/if}
</Container>
