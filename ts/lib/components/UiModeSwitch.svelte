<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { Bool, Empty } from "@generated/anki/generic_pb";
    import * as tr from "@generated/ftl";
    import { postProto } from "@generated/post";
    import type { Writable } from "svelte/store";

    /**
     * The Simple | Advanced switch at the top right of deck options and of
     * the Stats page: the collection's one UI mode, the same flag as the main
     * window's switch (spec ui.mode-switch). A click switches the page at
     * once and tells the main window, which stores the flag and redraws; it
     * does not wait for Save.
     */
    export let advancedUi: Writable<boolean>;
    /** What Simple hides on this page. */
    export let tooltip: string;

    async function choose(advanced: boolean): Promise<void> {
        if (advanced === $advancedUi) {
            return;
        }
        advancedUi.set(advanced);
        await postProto("setAdvancedUi", new Bool({ val: advanced }), Empty);
    }
</script>

<div class="ui-mode" role="group" aria-label={tr.deckConfigUiMode()} title={tooltip}>
    <button
        class="ui-mode-option"
        class:active={!$advancedUi}
        aria-pressed={!$advancedUi}
        on:click={() => choose(false)}
    >
        {tr.deckConfigUiModeSimple()}
    </button>
    <button
        class="ui-mode-option"
        class:active={$advancedUi}
        aria-pressed={$advancedUi}
        on:click={() => choose(true)}
    >
        {tr.deckConfigUiModeAdvanced()}
    </button>
</div>

<style lang="scss">
    /* the main window's pill (qt/aqt/data/web/css/toolbar.scss .ui-mode) */
    .ui-mode {
        display: inline-flex;
        flex-shrink: 0;
        align-self: center;
        margin-left: 0.75rem;
        border: 1px solid var(--border);
        border-radius: 999px;
        overflow: hidden;
        background: var(--canvas-elevated);
    }

    .ui-mode-option {
        padding: 3px 12px;
        border: none;
        background: transparent;
        font-weight: bold;
        color: var(--fg);
        cursor: pointer;

        &.active {
            background: var(--fg);
            color: var(--canvas);
            cursor: default;
        }

        /* the pages' global `button:not(.btn, .btn-close):hover` rule gives
           every button a 1px border on hover, and it outranks the `border:
           none` above, so the pill grew by 2px under the mouse (spec
           ui.mode-switch) */
        &:hover {
            border: none;
        }

        &:not(.active):hover {
            background: var(--canvas-inset);
        }
    }
</style>
