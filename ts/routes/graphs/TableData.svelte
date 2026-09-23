<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { direction } from "@tslib/i18n";

    import RetrievabilityText from "$lib/components/RetrievabilityText.svelte";

    import type { TableDatum } from "./graph-helpers";

    export let tableData: TableDatum[];
</script>

<div>
    <table dir={direction()}>
        <tbody>
            {#each tableData as { label, value }}
                <tr>
                    <!-- a label that names retrievability explains it on hover
                    (spec ui.retrievability-advanced-only) -->
                    <td class="align-end"><RetrievabilityText text={label} />:</td>
                    <td class="align-start">{value}</td>
                </tr>
            {/each}
        </tbody>
    </table>
</div>

<style lang="scss">
    div {
        display: flex;
        justify-content: center;
    }

    td {
        padding: 3px;
    }

    .align-end {
        text-align: end;
    }

    .align-start {
        text-align: start;
    }
</style>
