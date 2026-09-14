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

/** How many elements with exactly this text are rendered (help modals hold hidden copies). */
async function visibleCount(page: Page, text: string): Promise<number> {
    return page
        .getByText(text, { exact: true })
        .evaluateAll((elements) => elements.filter((e) => (e as HTMLElement).offsetParent !== null).length);
}

// Pins spec/deck-options.md#deck-options.scheduler-choice,
// #deck-options.simple-view and #deck-options.fsrs-only-controls: there is
// no FSRS switch any more, the Algorithm dropdown exists only in Advanced
// mode, and so do the FSRS parameters (inside the FSRS advanced section);
// "Optimize All Presets" is the one optimize action and shows in Simple
// mode too.
test("Simple mode shows desired retention but no Algorithm dropdown", async ({ page }) => {
    await setAdvancedUi(page, false);
    await page.goto("/deck-options/1");

    await expect(page.getByRole("checkbox", { name: /^FSRS\b/ })).toHaveCount(0);
    await expect(page.getByText("Algorithm", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Desired retention", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Bury siblings", { exact: true }).first()).toBeVisible();
    await expect(
        page.locator('[role="button"][aria-label="FSRS Parameters"]'),
    ).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Optimize All Presets" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Optimize Current Preset" })).toHaveCount(0);
    // Maximum reviews/day and the preset / deck / today tabs are Advanced-only
    expect(await visibleCount(page, "New cards/day")).toBeGreaterThan(0);
    expect(await visibleCount(page, "Maximum reviews/day")).toBe(0);
    await expect(page.getByRole("button", { name: "This deck" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Today only" })).toHaveCount(0);
    await expect(page.getByText("Skip question when replaying answer", { exact: true })).toHaveCount(0);
});

// Pins spec/deck-options.md#deck-options.collection-wide-in-preferences: the
// collection-wide settings are not on the deck-options page in either mode.
test("collection-wide settings are not on the deck-options page", async ({ page }) => {
    const collectionWide = [
        "Limits start from top",
        "Skip learning/relearning queues with FSRS/RWKV",
        "Reschedule cards when desired retention changes",
        "Custom scheduling",
    ];
    await setAdvancedUi(page, true);
    try {
        await page.goto("/deck-options/1");
        await expect(page.getByText("Algorithm", { exact: true }).first()).toBeVisible();
        await expect(page.getByRole("button", { name: "This deck" }).first()).toBeVisible();
        expect(await visibleCount(page, "Maximum reviews/day")).toBeGreaterThan(0);
        await expect(page.getByRole("button", { name: "Today only" }).first()).toBeVisible();
        for (const title of collectionWide) {
            await expect(page.getByText(title, { exact: true })).toHaveCount(0);
        }
        await expect(page.getByRole("button", { name: "Optimize Current Preset" })).toHaveCount(0);
        await expect(page.getByText("Check health when optimizing", { exact: false })).toHaveCount(0);
    } finally {
        // Simple mode is the collection default; leave it for the other tests.
        await setAdvancedUi(page, false);
    }
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
