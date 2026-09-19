<!--
    Copyright: Ankitects Pty Ltd and contributors
    License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    import Item from "$lib/components/Item.svelte";
    import SettingTitle from "$lib/components/SettingTitle.svelte";

    import type { DeckOptionsState } from "./lib";
    import { ValueTab } from "./lib";
    import SpinBoxRow from "./SpinBoxRow.svelte";
    import TabbedValue from "./TabbedValue.svelte";
    import type { Placement } from "./ui-split";
    import Warning from "./Warning.svelte";

    /**
     * New cards/day and Maximum reviews/day, with their preset / deck / today
     * tabs. Maximum reviews/day and the tabs are Advanced-only by default
     * (spec deck-options.simple-view); the split decides (ui-split.ts). Hosted by the
     * Advanced-mode Daily Limits section (DailyLimits) and by the Simple-mode
     * page (SimpleOptions), which own the help modal and receive the help key
     * to open.
     */
    export let state: DeckOptionsState;
    export let openHelp: (key: "newLimit" | "reviewLimit") => void;
    /** Where these rows are drawn (ui-split.ts). */
    export let placement: Placement = "section";

    export function onPresetChange() {
        newTabs[0] = new ValueTab(
            tr.deckConfigSharedPreset(),
            $config.newPerDay,
            (value) => ($config.newPerDay = value!),
            $config.newPerDay,
            null,
        );
        reviewTabs[0] = new ValueTab(
            tr.deckConfigSharedPreset(),
            $config.reviewsPerDay,
            (value) => ($config.reviewsPerDay = value!),
            $config.reviewsPerDay,
            null,
        );
    }

    const config = state.currentConfig;
    const limits = state.deckLimits;
    const defaults = state.defaults;
    const shown = state.settingShown;
    $: showTabs = $shown("dailyLimitTabs", placement);

    $: reviewsTooLow =
        Math.min(9999, newValue * 10) > reviewsValue
            ? tr.deckConfigReviewsTooLow({
                  cards: newValue,
                  expected: Math.min(9999, newValue * 10),
              })
            : "";

    const newTabs: ValueTab[] = [
        new ValueTab(
            tr.deckConfigSharedPreset(),
            $config.newPerDay,
            (value) => ($config.newPerDay = value!),
            $config.newPerDay,
            null,
        ),
        new ValueTab(
            tr.deckConfigDeckOnly(),
            $limits.new ?? null,
            (value) => ($limits.new = value ?? undefined),
            null,
            null,
        ),
        new ValueTab(
            tr.deckConfigTodayOnly(),
            $limits.newTodayActive ? ($limits.newToday ?? null) : null,
            (value) => ($limits.newToday = value ?? undefined),
            null,
            $limits.newToday ?? null,
        ),
    ];

    const reviewTabs: ValueTab[] = [
        new ValueTab(
            tr.deckConfigSharedPreset(),
            $config.reviewsPerDay,
            (value) => ($config.reviewsPerDay = value!),
            $config.reviewsPerDay,
            null,
        ),
        new ValueTab(
            tr.deckConfigDeckOnly(),
            $limits.review ?? null,
            (value) => ($limits.review = value ?? undefined),
            null,
            null,
        ),
        new ValueTab(
            tr.deckConfigTodayOnly(),
            $limits.reviewTodayActive ? ($limits.reviewToday ?? null) : null,
            (value) => ($limits.reviewToday = value ?? undefined),
            null,
            $limits.reviewToday ?? null,
        ),
    ];

    let newValue = 0;
    let reviewsValue = 0;
</script>

<!-- The value logic stays whether a row shows or not: a hidden row keeps
     the stored limit, only its control is hidden. -->
<Item>
    <div class:hidden-row={!$shown("newLimit", placement)}>
        <SpinBoxRow bind:value={newValue} defaultValue={defaults.newPerDay}>
            <TabbedValue slot="tabs" tabs={newTabs} bind:value={newValue} {showTabs} />
            <SettingTitle on:click={() => openHelp("newLimit")}>
                {tr.schedulingNewCardsday()}
            </SettingTitle>
        </SpinBoxRow>
    </div>
</Item>

<Item>
    <div class:hidden-row={!$shown("reviewLimit", placement)}>
        <SpinBoxRow bind:value={reviewsValue} defaultValue={defaults.reviewsPerDay}>
            <TabbedValue
                slot="tabs"
                tabs={reviewTabs}
                bind:value={reviewsValue}
                {showTabs}
            />
            <SettingTitle on:click={() => openHelp("reviewLimit")}>
                {tr.schedulingMaximumReviewsday()}
            </SettingTitle>
        </SpinBoxRow>
    </div>
</Item>

{#if $shown("reviewLimit", placement)}
    <Item>
        <Warning warning={reviewsTooLow} />
    </Item>
{/if}

<style>
    .hidden-row {
        display: none;
    }
</style>
