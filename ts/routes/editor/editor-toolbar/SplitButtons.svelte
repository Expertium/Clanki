<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { EditorButton } from "../ui-mode";
    import { shownButtons } from "../ui-mode";

    /**
     * Wraps editor buttons the Simple | Advanced split may hide (spec
     * ui.editor-simple-view, ui.split-configurable): shown while at least one
     * of `buttons` is. A hidden button is hidden with CSS instead of being
     * removed, so its keyboard shortcut, its remove-formatting entry and the
     * format it registers keep working in both modes, and a mode switch
     * rebuilds nothing.
     */
    export let buttons: EditorButton[];

    $: hidden = !buttons.some((button) => $shownButtons.has(button));
</script>

<div class="split-buttons" class:hidden data-editor-buttons={buttons.join(" ")}>
    <slot />
</div>

<style lang="scss">
    .split-buttons {
        display: contents;

        &.hidden {
            display: none;
        }
    }
</style>
