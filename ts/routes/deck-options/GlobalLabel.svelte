<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { mdiWeb } from "$lib/components/icons";
    import Icon from "$lib/components/Icon.svelte";

    export let title: string;
</script>

{title}
<!-- A meridian globe in the link colour marks collection-wide settings
     (spec ui.global-marker). -->
<div class="inline-icon" title={tr.deckConfigAffectsEntireCollection()}>
    <Icon icon={mdiWeb} />
</div>

<style lang="scss">
    .inline-icon {
        display: inline-block;
        width: 1em;
        fill: var(--fg-link);
        margin: 0.25em;
    }
</style>
