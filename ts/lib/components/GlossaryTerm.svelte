<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<!--
    A word inside a label that explains itself on hover.

    A setting's own help opens on a click, which the user has to decide to
    make. One word in the label is usually the whole difficulty ("sibling",
    "leech", "interday"), and a hover answers it before the user has to ask
    (spec deck-options.glossary-term). The underline is always drawn, so the
    word says it can be hovered without being hovered first.
-->
<script lang="ts">
    import Tooltip from "bootstrap/js/dist/tooltip";
    import { onDestroy } from "svelte";

    /** The short explanation, a few words. */
    export let explanation: string;

    let instance: Tooltip | undefined;

    function tooltip(node: HTMLElement) {
        instance = new Tooltip(node, {
            title: explanation,
            placement: "top",
            trigger: "hover focus",
            // the label is plain text; never let a translation inject markup
            html: false,
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

<span class="glossary-term" tabindex="0" role="note" use:tooltip>
    <slot />
</span>

<style lang="scss">
    .glossary-term {
        text-decoration: underline dotted var(--fg-subtle);
        text-underline-offset: 0.2em;
        cursor: help;
    }
</style>
