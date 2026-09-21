<!--
    Copyright: Ankitects Pty Ltd and contributors
    License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    /**
     * Underline the whole title while the pointer is on it, to say the title
     * can be clicked for help.
     *
     * A title that already underlines part of itself turns this off: two
     * underlines at once, one under the whole label and one under the words
     * that explain themselves, read as one underline and neither says
     * anything (spec deck-options.glossary-term).
     */
    export let underlineOnHover = true;
</script>

<!-- svelte-ignore a11y-no-static-element-interactions -->
<!-- svelte-ignore a11y-click-events-have-key-events -->
<div class="setting-title" class:underline-on-hover={underlineOnHover} on:click>
    <slot />
</div>

<style lang="scss">
    .setting-title {
        cursor: help;
        &.underline-on-hover:hover {
            text-decoration: underline dotted var(--fg-subtle);
        }
    }
</style>
