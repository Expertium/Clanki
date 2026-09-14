<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { createEventDispatcher } from "svelte";
    import type { Writable } from "svelte/store";

    import "$lib/sveltelib/export-runtime";

    import Container from "$lib/components/Container.svelte";
    import Row from "$lib/components/Row.svelte";
    import type { DynamicSvelteComponent } from "$lib/sveltelib/dynamicComponent";

    import Addons from "./Addons.svelte";
    import AdvancedOptions from "./AdvancedOptions.svelte";
    import AudioOptions from "./AudioOptions.svelte";
    import AutoAdvance from "./AutoAdvance.svelte";
    import BuryOptions from "./BuryOptions.svelte";
    import ConfigSelector from "./ConfigSelector.svelte";
    import DailyLimits from "./DailyLimits.svelte";
    import DisplayOrder from "./DisplayOrder.svelte";
    import FsrsOptionsOuter from "./FsrsOptionsOuter.svelte";
    import HtmlAddon from "./HtmlAddon.svelte";
    import LapseOptions from "./LapseOptions.svelte";
    import type { DeckOptionsState } from "./lib";
    import NewOptions from "./NewOptions.svelte";
    import RwkvOptions from "./RwkvOptions.svelte";
    import SimpleOptions from "./SimpleOptions.svelte";
    import TimerOptions from "./TimerOptions.svelte";
    import EasyDays from "./EasyDays.svelte";

    export let state: DeckOptionsState;
    const dispatch = createEventDispatcher<{ close: void }>();
    const addons = state.addonComponents;
    // Simple mode is one section; Advanced mode is the per-topic sections
    // (spec deck-options.simple-view). The mode is the collection flag set
    // from the main window; this page has no switch of its own.
    const advancedUi = state.advancedUi;

    export function auxData(): Writable<Record<string, unknown>> {
        return state.currentAuxData;
    }

    export function addSvelteAddon(component: DynamicSvelteComponent): void {
        $addons = [...$addons, component];
    }

    export function addHtmlAddon(html: string, mounted: () => void): void {
        $addons = [
            ...$addons,
            {
                component: HtmlAddon,
                html,
                mounted,
            },
        ];
    }

    export const options = {};
    export const dailyLimits = {};
    export const newOptions = {};
    export const lapseOptions = {};
    export const buryOptions = {};
    export const displayOrder = {};
    export const timerOptions = {};
    export const audioOptions = {};
    export const advancedOptions = {};
    export const easyDays = {};

    let dailyLimitsComponent: DailyLimits | undefined;
    let fsrsOptionsOuterComponent: FsrsOptionsOuter | undefined;
    let simpleOptionsComponent: SimpleOptions | undefined;

    function onPresetChange() {
        if (dailyLimitsComponent) {
            dailyLimitsComponent.onPresetChange();
        }
        if (fsrsOptionsOuterComponent) {
            fsrsOptionsOuterComponent.onPresetChange();
        }
        if (simpleOptionsComponent) {
            simpleOptionsComponent.onPresetChange();
        }
    }
</script>

<ConfigSelector
    {state}
    on:presetchange={onPresetChange}
    on:close={() => dispatch("close")}
/>

<div class="deck-options-page" class:simple={!$advancedUi}>
    <Container
        breakpoint="sm"
        --gutter-inline="0.25rem"
        --gutter-block="0.75rem"
        class="container-columns"
    >
        {#if $advancedUi}
            <div>
                <Row class="row-columns">
                    <DailyLimits
                        {state}
                        api={dailyLimits}
                        bind:this={dailyLimitsComponent}
                    />
                </Row>

                <Row class="row-columns">
                    <NewOptions {state} api={newOptions} />
                </Row>

                <Row class="row-columns">
                    <LapseOptions {state} api={lapseOptions} />
                </Row>

                <Row class="row-columns">
                    <DisplayOrder {state} api={displayOrder} />
                </Row>

                <Row class="row-columns">
                    <FsrsOptionsOuter
                        {state}
                        api={{}}
                        bind:this={fsrsOptionsOuterComponent}
                    />
                </Row>

                <Row class="row-columns">
                    <RwkvOptions {state} />
                </Row>
            </div>

            <div>
                <Row class="row-columns">
                    <BuryOptions {state} api={buryOptions} />
                </Row>

                <Row class="row-columns">
                    <AudioOptions {state} api={audioOptions} />
                </Row>

                <Row class="row-columns">
                    <TimerOptions {state} api={timerOptions} />
                </Row>

                <Row class="row-columns">
                    <AutoAdvance {state} api={timerOptions} />
                </Row>

                {#if $addons.length}
                    <Row class="row-columns">
                        <Addons {state} />
                    </Row>
                {/if}

                <Row class="row-columns">
                    <EasyDays {state} api={easyDays} />
                </Row>

                <Row class="row-columns">
                    <AdvancedOptions {state} api={advancedOptions} />
                </Row>
            </div>
        {:else}
            <div>
                <Row class="row-columns">
                    <SimpleOptions
                        {state}
                        api={options}
                        bind:this={simpleOptionsComponent}
                    />
                </Row>

                {#if $addons.length}
                    <Row class="row-columns">
                        <Addons {state} />
                    </Row>
                {/if}
            </div>
        {/if}
    </Container>
</div>

<style lang="scss">
    @use "$lib/sass/breakpoints" as bp;

    .deck-options-page {
        overflow-x: hidden;
        word-break: break-word;

        :global(.container-columns) {
            display: grid;
            gap: 0px;
        }

        @include bp.with-breakpoint("lg") {
            :global(.container-columns) {
                grid-template-columns: repeat(2, 1fr);
                gap: 20px;
            }

            &.simple :global(.container-columns) {
                grid-template-columns: minmax(0, 1fr);
                max-width: 52rem;
                margin-inline: auto;
            }
        }
    }
</style>
