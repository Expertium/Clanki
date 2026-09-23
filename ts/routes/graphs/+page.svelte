<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { GraphsRequest_Graph as Graph } from "@generated/anki/stats_pb";

    import AddedGraph from "./AddedGraph.svelte";
    import ButtonsGraph from "./ButtonsGraph.svelte";
    import CalendarGraph from "./CalendarGraph.svelte";
    import CalibrationGraph from "./CalibrationGraph.svelte";
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
    import UmPlusGraph from "./UmPlusGraph.svelte";

    import type { GraphItem } from "./ui-mode";

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
        CalibrationGraph,
        UmPlusGraph,
        TrueRetention,
        HourGraph,
        ButtonsGraph,
        AddedGraph,
    ];
    // Each graph's item of the Simple | Advanced split (qt/aqt/ui_split.py
    // lists the same ids, in the same order) and the data it draws: in
    // Simple mode the page asks the backend only for the data of the graphs
    // it shows (graphs with their own request need none of it).
    // advancedOnly: the graph names retrievability, which only Advanced mode
    // shows (spec ui.retrievability-advanced-only)
    const graphItems: GraphItem[] = [
        { id: "today", data: [Graph.TODAY] },
        { id: "futureDue", data: [Graph.FUTURE_DUE] },
        { id: "calendar", data: [Graph.REVIEWS] },
        { id: "reviews", data: [Graph.REVIEWS] },
        { id: "cardCounts", data: [Graph.CARD_COUNTS] },
        { id: "intervals", data: [Graph.INTERVALS] },
        { id: "stability", data: [Graph.STABILITY], advancedOnly: true },
        { id: "ease", data: [Graph.EASES] },
        { id: "difficulty", data: [Graph.DIFFICULTY] },
        { id: "retrievability", data: [Graph.RETRIEVABILITY], advancedOnly: true },
        { id: "totalKnowledge", data: [], advancedOnly: true },
        { id: "roc", data: [], advancedOnly: true },
        { id: "calibration", data: [], advancedOnly: true },
        { id: "umPlus", data: [], advancedOnly: true },
        { id: "trueRetention", data: [Graph.TRUE_RETENTION] },
        { id: "hours", data: [Graph.HOURS] },
        { id: "buttons", data: [Graph.BUTTONS] },
        { id: "added", data: [Graph.ADDED] },
    ];
</script>

<GraphsPage
    {graphs}
    {graphItems}
    initialSearch="deck:current"
    initialDays={365}
    controller={RangeBox}
/>
