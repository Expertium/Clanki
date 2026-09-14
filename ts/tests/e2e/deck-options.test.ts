// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

/**
 * Switches the collection between Simple and Advanced mode (the `advancedUi`
 * flag; spec/ui.md, `ui.mode-switch`) through the backend, as the main
 * window's toolbar does. The deck-options page has no switch of its own.
 *
 * The body is a hand-encoded `SetConfigBoolRequest`: field 1 (`key`, varint)
 * = `ConfigKey.Bool.ADVANCED_UI` (31), field 2 (`value`, varint) = 1. A
 * false value is the proto default and is left out.
 */
async function setAdvancedUi(page: Page, on: boolean): Promise<void> {
    const body = on ? [0x08, 0x1f, 0x10, 0x01] : [0x08, 0x1f];
    const response = await page.request.post("/_anki/setConfigBool", {
        headers: { "Content-Type": "application/binary" },
        data: Buffer.from(body),
    });
    expect(response.ok()).toBeTruthy();
}

// Pins spec/deck-options.md#deck-options.scheduler-choice and
// #deck-options.simple-view: there is no FSRS switch any more, the algorithm
// comes from one dropdown in both modes, and the FSRS parameters (inside the
// FSRS advanced section) exist only in Advanced mode.
test("Algorithm dropdown replaces the FSRS switch, in Simple mode", async ({ page }) => {
    await setAdvancedUi(page, false);
    await page.goto("/deck-options/1");

    await expect(page.getByRole("checkbox", { name: /^FSRS\b/ })).toHaveCount(0);
    await expect(page.getByText("Algorithm", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("FSRS-7", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Bury siblings", { exact: true }).first()).toBeVisible();
    await expect(
        page.locator('[role="button"][aria-label="FSRS Parameters"]'),
    ).toHaveCount(0);
    await expect(page.getByText("Reschedule cards on change", { exact: true })).toHaveCount(0);
});

test("FSRS parameter unlock timing is per page (Advanced mode)", async ({ page }) => {
    const advanced = page.locator("details.fsrs-advanced");
    const parameters = page.locator('[role="button"][aria-label="FSRS Parameters"]');
    const input = parameters.locator("textarea");

    async function setTimeoutMs(ms: number): Promise<void> {
        await page.evaluate((ms) => (window as any).anki.setParameterUnlockClickTimeoutMs(ms), ms);
    }

    async function clickThreeTimes(interval: number): Promise<void> {
        await parameters.click();
        await page.clock.runFor(interval);
        await parameters.click();
        await expect(input).toBeDisabled();
        await page.clock.runFor(interval);
        await parameters.click();
    }

    await setAdvancedUi(page, true);
    try {
        await page.clock.install();
        await page.goto("/deck-options/1");
        await expect(parameters).toHaveCount(1);
        await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));

        const defaultMs = await page.evaluate(() => (window as any).anki.defaultParameterUnlockClickTimeoutMs);
        expect(defaultMs).toBe(500);

        // The host can configure timing, and three clicks inside it unlock the input.
        await setTimeoutMs(1000);
        await advanced.locator("summary").click();
        await expect(input).toBeDisabled();
        await clickThreeTimes(750);
        await expect(input).toBeEnabled();

        // Host preferences last for this page only; a fresh page starts at the default.
        await setTimeoutMs(2000);
        await page.reload();
        await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));
        await advanced.locator("summary").click();
        await clickThreeTimes(750);
        await expect(input).toBeDisabled();
    } finally {
        // Simple mode is the collection default; leave it for the other tests.
        await setAdvancedUi(page, false);
    }
});
