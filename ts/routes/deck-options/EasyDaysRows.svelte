<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    import Item from "$lib/components/Item.svelte";

    import EasyDaysInput from "./EasyDaysInput.svelte";
    import type { DeckOptionsState } from "./lib";
    import Warning from "./Warning.svelte";

    /**
     * The Easy Days sliders and their warnings. On the Simple-mode page
     * (SimpleOptions) they are collapsed behind an "Easy Days" expander until
     * the user opens it; the Advanced-mode Easy Days section (EasyDays) shows
     * them at once under its own title (spec deck-options.simple-view).
     * Review fuzz and the load balancer are always on and have no controls
     * (spec sched.fuzz-always-on).
     */
    export let state: DeckOptionsState;
    /** Opens the Easy Days help entry of the hosting section's help modal. */
    export let openHelp: () => void = () => {};
    /** Behind an expander (Simple mode), or shown at once (Advanced mode). */
    export let collapsible = true;

    const fsrsEnabled = state.fsrs;
    const reschedule = state.fsrsReschedule;
    const config = state.currentConfig;
    const defaults = state.defaults;
    const prevEasyDaysPercentages = $config.easyDaysPercentages.slice();

    $: if ($config.easyDaysPercentages.length !== 7) {
        $config.easyDaysPercentages = defaults.easyDaysPercentages.slice();
    }

    $: easyDaysChanged = $config.easyDaysPercentages.some(
        (value, index) => value !== prevEasyDaysPercentages[index],
    );

    $: noNormalDay = $config.easyDaysPercentages.some((p) => p === 1.0)
        ? ""
        : tr.deckConfigEasyDaysNoNormalDays();

    $: rescheduleWarning =
        easyDaysChanged && !($fsrsEnabled && $reschedule)
            ? tr.deckConfigEasyDaysChange()
            : "";
</script>

<datalist id="easy_day_steplist">
    <option>0.5</option>
</datalist>

{#if collapsible}
    <!-- The name toggles the expander; the "?" next to it opens the help
         without toggling (spec deck-options.simple-view). -->
    <details class="easy-days m-1">
        <summary>
            {tr.deckConfigEasyDaysTitle()}
            <button
                type="button"
                class="easy-days-help"
                title={tr.deckConfigEasyDaysTitle()}
                on:click|preventDefault|stopPropagation={openHelp}
            >
                ?
            </button>
        </summary>

        <EasyDaysInput bind:values={$config.easyDaysPercentages} />
        <Item>
            <Warning warning={noNormalDay} />
        </Item>
        <Item>
            <Warning warning={rescheduleWarning} />
        </Item>
    </details>
{:else}
    <div class="easy-days m-1">
        <EasyDaysInput bind:values={$config.easyDaysPercentages} />
        <Item>
            <Warning warning={noNormalDay} />
        </Item>
        <Item>
            <Warning warning={rescheduleWarning} />
        </Item>
    </div>
{/if}

<style>
    .easy-days summary {
        cursor: pointer;
        margin-bottom: 0.75rem;
    }

    .easy-days-help {
        cursor: help;
        margin-left: 0.25rem;
        padding: 0 0.4rem;
        border: 1px solid var(--border);
        border-radius: 50%;
        background: transparent;
        color: var(--fg-subtle);
        font-size: smaller;
        line-height: 1.4;
    }

    .easy-days-help:hover {
        color: var(--fg);
    }
</style>
