<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { bridgeCommand } from "@tslib/bridgecommand";
    import { getPlatformString } from "@tslib/shortcuts";

    import ButtonGroup from "$lib/components/ButtonGroup.svelte";
    import ButtonGroupItem, {
        createProps,
        setSlotHostContext,
        updatePropsList,
    } from "$lib/components/ButtonGroupItem.svelte";
    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import LabelButton from "$lib/components/LabelButton.svelte";
    import Shortcut from "$lib/components/Shortcut.svelte";
    import { context } from "../NoteEditor.svelte";
    import { openFieldsDialog, openCardsDialog } from "@generated/backend";
    import { shownButtons } from "../ui-mode";
    import SplitButtons from "./SplitButtons.svelte";

    export let api = {};
    const { isLegacy, saveNow } = context.get();
    const keyCombination = "Control+L";
</script>

<ButtonGroup class={$shownButtons.has("cards") ? "" : "notetype-buttons-simple"}>
    <DynamicallySlottable
        slotHost={ButtonGroupItem}
        {createProps}
        {updatePropsList}
        {setSlotHostContext}
        {api}
    >
        <ButtonGroupItem>
            <SplitButtons buttons={["fields"]}>
                <LabelButton
                    tooltip={tr.editingCustomizeFields()}
                    on:click={async () => {
                        await saveNow();
                        if (isLegacy) {
                            bridgeCommand("fields");
                        } else {
                            await openFieldsDialog({});
                        }
                    }}
                >
                    {tr.editingFields()}...
                </LabelButton>
            </SplitButtons>
        </ButtonGroupItem>

        <ButtonGroupItem>
            <SplitButtons buttons={["cards"]}>
                <LabelButton
                    tooltip="{tr.editingCustomizeCardTemplates()} ({getPlatformString(
                        keyCombination,
                    )})"
                    on:click={async () => {
                        await saveNow();
                        if (isLegacy) {
                            bridgeCommand("cards");
                        } else {
                            await openCardsDialog({});
                        }
                    }}
                >
                    {tr.editingCards()}...
                </LabelButton>
            </SplitButtons>
            <!-- outside the wrapper: Ctrl+L keeps working in Simple mode
                 (spec ui.editor-simple-view) -->
            <Shortcut
                {keyCombination}
                on:action={async () => {
                    await saveNow();
                    bridgeCommand("cards");
                }}
            />
        </ButtonGroupItem>

        <slot />
    </DynamicallySlottable>
</ButtonGroup>

<style lang="scss">
    /* Cards... is hidden (as Simple mode does by default), so Fields... is on its own and keeps both
       rounded ends. */
    :global(.notetype-buttons-simple .button-group-item:first-child) {
        --border-right-radius: 5px !important;
    }
</style>
