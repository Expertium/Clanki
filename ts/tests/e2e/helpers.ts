// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { ConfigKey_Bool, GetConfigBoolRequest } from "@generated/anki/config_pb";
import { Bool } from "@generated/anki/generic_pb";
import type { Locator, Page, Request, Response } from "@playwright/test";
import { expect } from "@playwright/test";

// ---------------------------------------------------------------------------
// RPC URL helpers
// ---------------------------------------------------------------------------

export function rpcUrl(method: string): string {
    return `/_anki/${method}`;
}

/**
 * Returns a Playwright request predicate that matches only this exact RPC
 * endpoint. Uses endsWith() so "addNote" never accidentally matches
 * "addNoteBulk" or similar future methods.
 */
export function isRpc(method: string): (req: Request) => boolean {
    const suffix = rpcUrl(method);
    return (req) => req.url().endsWith(suffix);
}

export function isRpcResponse(method: string): (resp: Response) => boolean {
    return (resp) => isRpc(method)(resp.request());
}

// ---------------------------------------------------------------------------
// UI mode (spec/ui.md, `ui.mode-switch`)
//
// mediasrv answers `setAdvancedUi` (qt/aqt/mediasrv.py) for the mode; the raw
// backend method `setConfigBool` is not in its `exposed_backend_list`, so a
// page cannot write the flag directly. `getConfigBool` is exposed and reads it.
// ---------------------------------------------------------------------------

async function rpcPost(page: Page, method: string, body: Uint8Array): Promise<Buffer> {
    const response = await page.request.post(rpcUrl(method), {
        headers: { "Content-Type": "application/binary" },
        data: Buffer.from(body),
    });
    expect(response.ok(), `${rpcUrl(method)} answered ${response.status()}`).toBeTruthy();
    return response.body();
}

/** The collection's Advanced UI flag; false = Simple mode, the default. */
export async function advancedUi(page: Page): Promise<boolean> {
    const request = new GetConfigBoolRequest({ key: ConfigKey_Bool.ADVANCED_UI });
    const body = await rpcPost(page, "getConfigBool", request.toBinary());
    return Bool.fromBinary(new Uint8Array(body)).val;
}

/**
 * Switches the collection between Simple and Advanced mode, as the main
 * window's toolbar switch does. The mediasrv handler hands the change to the
 * Qt main thread and answers at once, so the flag is read back until the
 * change has landed.
 */
export async function setAdvancedUi(page: Page, on: boolean): Promise<void> {
    await rpcPost(page, "setAdvancedUi", new Bool({ val: on }).toBinary());
    await expect.poll(() => advancedUi(page)).toBe(on);
}

// ---------------------------------------------------------------------------
// Field locators
//
// DOM shape (NoteEditor.svelte → EditorField.svelte → RichTextInput.svelte):
//
//   .field-container[data-index="N"]          ← EditorField.svelte:97,102
//     .rich-text-editable                     ← RichTextInput.svelte:245
//       #shadow-root (open, via attachShadow)
//         anki-editable[contenteditable=true] ← ContentEditable.svelte:44
// ---------------------------------------------------------------------------

export function fieldContainer(page: Page, index: number): Locator {
    return page.locator(`.field-container[data-index="${index}"]`);
}

/**
 * The contenteditable element for a field. Playwright pierces open shadow roots
 * automatically through chained locator() calls, so no special pierce syntax is
 * needed.
 */
export function editableField(page: Page, index: number): Locator {
    return fieldContainer(page, index)
        .locator(".rich-text-editable")
        .locator("anki-editable[contenteditable='true']");
}

// ---------------------------------------------------------------------------
// Chooser helpers
// ---------------------------------------------------------------------------

export function chooserButton(page: Page, kind: "notetype" | "deck"): Locator {
    return page.locator("button.chooser-button").nth(kind === "notetype" ? 0 : 1);
}

export async function openChooserAndSelect(
    page: Page,
    kind: "notetype" | "deck",
    itemName: string,
): Promise<void> {
    await chooserButton(page, kind).click();
    const modal = page.locator(".modal.show");
    await modal.waitFor({ state: "visible", timeout: 5_000 });
    await modal.getByRole("button", { name: `Select ${itemName}` }).click();
    await modal.waitFor({ state: "hidden", timeout: 5_000 });
    await chooserButton(page, kind).filter({ hasText: itemName }).waitFor({
        state: "visible",
        timeout: 5_000,
    });
}

// ---------------------------------------------------------------------------
// Bridge call inspection
// ---------------------------------------------------------------------------

export function bridgeCalls(page: Page): Promise<string[]> {
    return page.evaluate(() => (window as any).__bridgeCalls as string[]);
}

// ---------------------------------------------------------------------------
// Protobuf decode
//
// The generated types (proto3.makeMessageType) expose a static fromBinary()
// on the returned object. Pass the message class/type directly and it decodes
// the binary postData from the intercepted Playwright Request.
//
// Requires that out/ts/lib/generated/ was built (./ninja ts or equivalent).
// ---------------------------------------------------------------------------

type BinaryDecodable<T> = {
    fromBinary(bytes: Uint8Array): T;
};

type BinaryEncodable = {
    toBinary(): Uint8Array;
};

export function decodeRequestBody<T>(
    request: Request,
    messageType: BinaryDecodable<T>,
): T {
    const body = request.postDataBuffer();
    if (!body) {
        throw new Error(`Request to ${request.url()} had no postData`);
    }
    try {
        return messageType.fromBinary(new Uint8Array(body));
    } catch (e) {
        throw new Error(`Failed to decode protobuf from ${request.url()}: ${e}`);
    }
}

export async function callRpc(
    page: Page,
    method: string,
    message: BinaryEncodable,
    opChangesType = 0,
): Promise<Uint8Array> {
    const responseBytes = await page.evaluate(
        async ({ method, body, opChangesType }) => {
            const response = await fetch(`/_anki/${method}`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/binary",
                    "Anki-Op-Changes": opChangesType.toString(),
                },
                body: new Uint8Array(body),
            });
            if (!response.ok) {
                throw new Error(
                    `RPC ${method} failed with ${response.status}: ${await response.text()}`,
                );
            }
            return Array.from(new Uint8Array(await response.arrayBuffer()));
        },
        { method, body: Array.from(message.toBinary()), opChangesType },
    );
    return new Uint8Array(responseBytes);
}

// ---------------------------------------------------------------------------
// Paste helper
//
// The editor's handlePaste (data-transfer.ts) reads from event.clipboardData,
// which is only available inside the browser context. The event must therefore
// be constructed and dispatched via page.evaluate / locator.evaluate.
// ---------------------------------------------------------------------------

export async function pasteData(
    locator: Locator,
    data: Record<string, string>,
): Promise<void> {
    await locator.evaluate((el, data) => {
        const dt = new DataTransfer();
        for (const [mimeType, value] of Object.entries(data)) {
            dt.setData(mimeType, value);
        }
        el.dispatchEvent(
            new ClipboardEvent("paste", {
                clipboardData: dt,
                bubbles: true,
                cancelable: true,
            }),
        );
    }, data);
}
