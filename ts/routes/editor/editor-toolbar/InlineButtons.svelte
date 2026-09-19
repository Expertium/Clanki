<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->

<script context="module" lang="ts">
    import type { DefaultSlotInterface } from "$lib/sveltelib/dynamic-slotting";

    export interface InlineButtonsAPI extends DefaultSlotInterface {
        setColorButtons: (colors: [string, string]) => void;
    }
</script>

<script lang="ts">
    import ButtonGroup from "$lib/components/ButtonGroup.svelte";
    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import Item from "$lib/components/Item.svelte";

    import BoldButton from "./BoldButton.svelte";
    import HighlightColorButton from "./HighlightColorButton.svelte";
    import ItalicButton from "./ItalicButton.svelte";
    import RemoveFormatButton from "./RemoveFormatButton.svelte";
    import SubscriptButton from "./SubscriptButton.svelte";
    import SuperscriptButton from "./SuperscriptButton.svelte";
    import TextColorButton from "./TextColorButton.svelte";
    import UnderlineButton from "./UnderlineButton.svelte";
    import { shownButtons } from "../ui-mode";
    import SplitButtons from "./SplitButtons.svelte";

    let textColor: string = "black";
    let highlightColor: string = "black";

    function setColorButtons([textClr, highlightClr]: [string, string]): void {
        textColor = textClr;
        highlightColor = highlightClr;
    }

    export let api = {} as InlineButtonsAPI;
    Object.assign(api, {
        setColorButtons,
    });
    // For legacy editor
    Object.assign(globalThis, { setColorButtons });
</script>

<DynamicallySlottable slotHost={Item} {api}>
    <Item>
        <SplitButtons buttons={["bold", "italic", "underline"]}>
            <ButtonGroup>
                <SplitButtons buttons={["bold"]}>
                    <BoldButton --border-left-radius="5px" />
                </SplitButtons>
                <SplitButtons buttons={["italic"]}>
                    <ItalicButton />
                </SplitButtons>
                <SplitButtons buttons={["underline"]}>
                    <UnderlineButton --border-right-radius="5px" />
                </SplitButtons>
            </ButtonGroup>
        </SplitButtons>
    </Item>

    <Item>
        <SplitButtons buttons={["superscript", "subscript"]}>
            <ButtonGroup>
                <SplitButtons buttons={["superscript"]}>
                    <SuperscriptButton --border-left-radius="5px" />
                </SplitButtons>
                <SplitButtons buttons={["subscript"]}>
                    <SubscriptButton --border-right-radius="5px" />
                </SplitButtons>
            </ButtonGroup>
        </SplitButtons>
    </Item>

    <Item>
        <SplitButtons buttons={["textColor", "highlightColor"]}>
            <ButtonGroup
                class={$shownButtons.has("highlightColor")
                    ? ""
                    : "colour-buttons-simple"}
            >
                <SplitButtons buttons={["textColor"]}>
                    <TextColorButton color={textColor} />
                </SplitButtons>
                <SplitButtons buttons={["highlightColor"]}>
                    <HighlightColorButton color={highlightColor} />
                </SplitButtons>
            </ButtonGroup>
        </SplitButtons>
    </Item>

    <Item>
        <SplitButtons buttons={["removeFormat"]}>
            <ButtonGroup>
                <RemoveFormatButton />
            </ButtonGroup>
        </SplitButtons>
    </Item>
</DynamicallySlottable>

<style lang="scss">
    /* The highlight colour is hidden (as Simple mode does by default), so the text colour's own
       dropdown arrow is the right end of the group. */
    :global(.colour-buttons-simple .icon-button:last-child) {
        --border-right-radius: 5px;
    }
</style>
