<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { AdvancedOnlyEditorButton } from "../ui-mode";
    import { advancedUi } from "../ui-mode";

    /**
     * Wraps the buttons the toolbar shows in Advanced mode only (spec
     * ui.editor-simple-view). Simple mode hides them with CSS instead of
     * removing them, so their keyboard shortcuts, their remove-formatting
     * entries and the formats they register keep working in both modes, and a
     * mode switch rebuilds nothing.
     */
    export let buttons: AdvancedOnlyEditorButton[];
</script>

<div
    class="advanced-only"
    class:hidden={!$advancedUi}
    data-editor-buttons={buttons.join(" ")}
>
    <slot />
</div>

<style lang="scss">
    .advanced-only {
        display: contents;

        &.hidden {
            display: none;
        }
    }
</style>
