<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<!--
    A text in which the word "retrievability" is underlined and explains
    itself on hover (spec ui.retrievability-advanced-only).
-->
<script lang="ts">
    import {
        retrievabilityExplanation,
        retrievabilityParts,
    } from "@tslib/retrievability";

    import GlossaryTerm from "./GlossaryTerm.svelte";

    export let text: string;

    $: parts = retrievabilityParts(text);
    const explanation = retrievabilityExplanation();
</script>

<!-- on one line: whitespace between the parts would show in the text -->
{#each parts as part}{#if part.term}<GlossaryTerm {explanation}>
            {part.text}
        </GlossaryTerm>{:else}{part.text}{/if}{/each}
