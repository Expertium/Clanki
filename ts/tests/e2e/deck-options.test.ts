// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "./fixtures";

// Pins spec/deck-options.md#deck-options.scheduler-choice: there is no FSRS
// switch any more, the algorithm comes from one dropdown, and the FSRS
// options are always mounted.
test("Algorithm dropdown replaces the FSRS switch", async ({ page }) => {
    await page.goto("/deck-options/1");

    await expect(page.getByRole("checkbox", { name: /^FSRS\b/ })).toHaveCount(0);
    await expect(page.getByText("Algorithm", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("FSRS-7", { exact: true }).first()).toBeVisible();
    await expect(
        page.locator('[role="button"][aria-label="FSRS Parameters"]'),
    ).toHaveCount(1);
});

test("FSRS parameter unlock timing is per page", async ({ page }) => {
    await page.clock.install();
    await page.goto("/deck-options/1");

    const advanced = page.locator("details.fsrs-advanced");
    const parameters = page.locator('[role="button"][aria-label="FSRS Parameters"]');
    const input = parameters.locator("textarea");
    await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));

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
});
