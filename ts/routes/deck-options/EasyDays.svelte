<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { HelpPage } from "@tslib/help-page";
    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import HelpModal from "$lib/components/HelpModal.svelte";
    import Item from "$lib/components/Item.svelte";
    import TitledContainer from "$lib/components/TitledContainer.svelte";
    import type { DeckOptionsState } from "./lib";
    import Warning from "./Warning.svelte";
    import EasyDaysInput from "./EasyDaysInput.svelte";
    import { type HelpItem, HelpItemScheduler } from "$lib/components/types";

    export let state: DeckOptionsState;
    export let api: Record<string, never>;

    // Review fuzz and the load balancer are always on and have no controls
    // here (spec sched.fuzz-always-on); only Easy Days is the user's choice.
    const fsrsEnabled = state.fsrs;
    const reschedule = state.fsrsReschedule;
    const config = state.currentConfig;
    const defaults = state.defaults;
    const prevEasyDaysPercentages = $config.easyDaysPercentages.slice();
    const settings = {
        easyDays: {
            title: tr.deckConfigEasyDaysTitle(),
            help: tr.deckConfigEasyDaysTooltip(),
            url: HelpPage.DeckOptions.fsrs,
            sched: HelpItemScheduler.FSRS,
        },
    };
    const helpSections: HelpItem[] = Object.values(settings);

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

<TitledContainer title={tr.deckConfigEasyDaysTitle()}>
    <HelpModal
        title={tr.deckConfigEasyDaysTitle()}
        url={HelpPage.DeckOptions.fsrs}
        slot="tooltip"
        fsrs={$fsrsEnabled}
        {helpSections}
    />
    <DynamicallySlottable slotHost={Item} {api}>
        <EasyDaysInput bind:values={$config.easyDaysPercentages} />
        <Item>
            <Warning warning={noNormalDay} />
        </Item>
        <Item>
            <Warning warning={rescheduleWarning} />
        </Item>
    </DynamicallySlottable>
</TitledContainer>
