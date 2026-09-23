<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<!--
    The info badge of a help window, but it explains on hover or keyboard
    focus instead of opening a window: for a "how is this calculated" note
    that most people never need, kept off the page until asked for (spec
    ui.stats-model-metrics).
-->
<script lang="ts">
    import { usesArabicScript } from "@tslib/i18n";
    import Tooltip from "bootstrap/js/dist/tooltip";
    import { onDestroy } from "svelte";

    import { infoCircle } from "$lib/components/icons";

    import Badge from "./Badge.svelte";
    import Icon from "./Icon.svelte";

    /** Plain text; a blank line starts a new paragraph. */
    export let text: string;

    let instance: Tooltip | undefined;

    function tooltip(node: HTMLElement) {
        instance = new Tooltip(node, {
            title: text,
            placement: "bottom",
            trigger: "hover focus",
            // translations are plain text; never let one inject markup
            html: false,
            customClass: "info-tooltip",
        });
        return {
            destroy() {
                instance?.dispose();
                instance = undefined;
            },
        };
    }

    onDestroy(() => {
        instance?.dispose();
        instance = undefined;
    });
</script>

<!-- a new text (the recall wording changed) makes a new tooltip -->
{#key text}
    <!-- focusable on purpose: a keyboard user reads the explanation on focus -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <span class="info-anchor" tabindex="0" role="note" aria-label={text} use:tooltip>
        <Badge iconSize={125} flipX={usesArabicScript()}>
            <Icon icon={infoCircle} />
        </Badge>
    </span>
{/key}

<style lang="scss">
    .info-anchor {
        cursor: help;
    }

    /* a few sentences, not a few words: wider than a label's tooltip */
    :global(.info-tooltip .tooltip-inner) {
        max-width: 28rem;
        text-align: left;
        white-space: pre-line;
    }
</style>
