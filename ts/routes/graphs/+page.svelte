<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { GraphsRequest_Graph as Graph } from "@generated/anki/stats_pb";

    import AddedGraph from "./AddedGraph.svelte";
    import ButtonsGraph from "./ButtonsGraph.svelte";
    import CalendarGraph from "./CalendarGraph.svelte";
    import CardCounts from "./CardCounts.svelte";
    import DifficultyGraph from "./DifficultyGraph.svelte";
    import EaseGraph from "./EaseGraph.svelte";
    import FutureDue from "./FutureDue.svelte";
    import GraphsPage from "./GraphsPage.svelte";
    import HourGraph from "./HourGraph.svelte";
    import IntervalsGraph from "./IntervalsGraph.svelte";
    import RangeBox from "./RangeBox.svelte";
    import RetrievabilityGraph from "./RetrievabilityGraph.svelte";
    import RocGraph from "./RocGraph.svelte";
    import ReviewsGraph from "./ReviewsGraph.svelte";
    import StabilityGraph from "./StabilityGraph.svelte";
    import TodayStats from "./TodayStats.svelte";
    import TotalKnowledgeGraph from "./TotalKnowledgeGraph.svelte";
    import TrueRetention from "./TrueRetention.svelte";

    // In Simple mode the page asks the backend only for the data the Simple
    // graphs draw (graphs with their own request need none of it)
    const simpleData = [Graph.REVIEWS, Graph.CARD_COUNTS, Graph.TRUE_RETENTION];
    const graphs = [
        TodayStats,
        FutureDue,
        CalendarGraph,
        ReviewsGraph,
        CardCounts,
        IntervalsGraph,
        StabilityGraph,
        EaseGraph,
        DifficultyGraph,
        RetrievabilityGraph,
        TotalKnowledgeGraph,
        RocGraph,
        TrueRetention,
        HourGraph,
        ButtonsGraph,
        AddedGraph,
    ];
    // Simple mode shows only these (spec ui.mode-switch)
    const simpleGraphs = [ReviewsGraph, CardCounts, TrueRetention, TotalKnowledgeGraph];
</script>

<GraphsPage
    {graphs}
    {simpleGraphs}
    {simpleData}
    initialSearch="deck:current"
    initialDays={365}
    controller={RangeBox}
/>
