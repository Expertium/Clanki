// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";
import { isRpc, setAdvancedUi } from "./helpers";

/**
 * How many elements with exactly this text the user can see. The help modals
 * and the collapsed expanders hold copies that are in the DOM but drawn
 * nowhere; a copy inside a closed `<details>` even keeps its offsetParent and
 * its box, because the expander hides it with `content-visibility`. Only
 * checkVisibility() answers for all of them.
 */
async function visibleCount(page: Page, text: string): Promise<number> {
    return page.getByText(text, { exact: true }).evaluateAll((elements) =>
        elements.filter((e) =>
            e.checkVisibility({
                contentVisibilityAuto: true,
                opacityProperty: true,
                visibilityProperty: true,
            })
        ).length
    );
}

/**
 * Picks an entry of the Algorithm (global) list, the only list whose value is
 * one of the three algorithm names. The choice is not saved, so it lasts for
 * this page only; a reload brings the collection's algorithm back.
 */
async function chooseAlgorithm(page: Page, label: string): Promise<void> {
    const algorithm = page
        .getByRole("combobox")
        .filter({ hasText: /FSRS-7|RWKV-Curve|RWKV-Instant/ });
    await algorithm.click();
    // Each entry shows the label and a description line, so the accessible
    // name of the option is not the label alone.
    await page.getByRole("option").getByText(label, { exact: true }).click();
    await expect(algorithm).toHaveText(label);
}

// Pins spec/deck-options.md#deck-options.scheduler-choice,
// #deck-options.simple-view and #deck-options.fsrs-only-controls: there is
// no FSRS switch any more, the Algorithm (global) dropdown (the algorithm is
// one for the collection, spec/scheduling.md#sched.one-global-algorithm)
// exists only in Advanced mode, and so do the FSRS parameters (inside the FSRS
// advanced section);
// "Optimize All Presets" is the one optimize action and shows in Simple
// mode too.
test("Simple mode shows desired retention but no Algorithm dropdown", async ({ page }) => {
    await setAdvancedUi(page, false);
    await page.goto("/deck-options/1");
    await expect.poll(() => visibleCount(page, "Desired retention")).toBeGreaterThan(0);

    await expect(page.getByRole("checkbox", { name: /^FSRS\b/ })).toHaveCount(0);
    expect(await visibleCount(page, "Hide related cards until tomorrow")).toBeGreaterThan(0);
    expect(await visibleCount(page, "Algorithm")).toBe(0);
    expect(await visibleCount(page, "Algorithm (global)")).toBe(0);
    await expect(
        page.locator("[role=\"button\"][aria-label=\"FSRS Parameters\"]"),
    ).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Optimize Current Preset" })).toHaveCount(0);
    // Maximum reviews/day and the preset / deck / today tabs are Advanced-only
    expect(await visibleCount(page, "New cards/day")).toBeGreaterThan(0);
    expect(await visibleCount(page, "Maximum reviews/day")).toBe(0);
    await expect(page.getByRole("button", { name: "This deck" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Today only" })).toHaveCount(0);
    await expect(page.getByText("Skip question when replaying answer", { exact: true })).toHaveCount(0);

    // Optimize All Presets is FSRS-7 only, and a new collection runs
    // RWKV-Curve. The page's switch keeps the unsaved algorithm choice, so
    // Simple mode shows the button and still hides the FSRS parameters.
    try {
        await page.getByRole("button", { name: "Advanced", exact: true }).click();
        await chooseAlgorithm(page, "FSRS-7");
        await page.getByRole("button", { name: "Simple", exact: true }).click();
        await expect(page.getByRole("button", { name: "Optimize All Presets" })).toBeVisible();
        await expect(
            page.locator("[role=\"button\"][aria-label=\"FSRS Parameters\"]"),
        ).toHaveCount(0);
    } finally {
        await setAdvancedUi(page, false);
    }
});

// Pins spec/deck-options.md#deck-options.desired-retention-note: the note is
// there before anything is focused or changed.
test("the desired-retention note shows when the page opens", async ({ page }) => {
    await setAdvancedUi(page, false);
    await page.goto("/deck-options/1");

    await expect(
        page.getByText("The higher your desired retention, the more frequently cards will be shown to you."),
    ).toBeVisible();
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
        // Pins spec/ui.md#ui.global-marker
        await expect(page.getByText("Algorithm (global)", { exact: true })).toBeVisible();
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
    const parameters = page.locator("[role=\"button\"][aria-label=\"FSRS Parameters\"]");
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
        await page.clock.resume();
        await chooseAlgorithm(page, "FSRS-7");
        await expect(parameters).toHaveCount(1);
        await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));

        const defaultMs = await page.evaluate(() => (window as any).anki.defaultParameterUnlockClickTimeoutMs);
        expect(defaultMs).toBe(500);

        // The host can configure timing, and three clicks inside it unlock the input.
        await setTimeoutMs(1000);
        await expect(input).toBeDisabled();
        await clickThreeTimes(750);
        await expect(input).toBeEnabled();

        // Host preferences last for this page only; a fresh page starts at the default.
        await setTimeoutMs(2000);
        await page.reload();
        await page.clock.resume();
        await chooseAlgorithm(page, "FSRS-7");
        await page.clock.pauseAt(await page.evaluate(() => Date.now() + 1000));
        await clickThreeTimes(750);
        await expect(input).toBeDisabled();
    } finally {
        // Simple mode is the collection default; leave it for the other tests.
        await setAdvancedUi(page, false);
    }
});

// Pins spec/ui.md#ui.mode-switch: deck options have their own Simple |
// Advanced switch; it changes the page at once and stores the mode.
test("the deck-options switch changes the view at once", async ({ page }) => {
    await setAdvancedUi(page, false);
    try {
        await page.goto("/deck-options/1");
        expect(await visibleCount(page, "Maximum reviews/day")).toBe(0);

        await page.getByRole("button", { name: "Advanced", exact: true }).click();
        await expect.poll(() => visibleCount(page, "Maximum reviews/day")).toBeGreaterThan(0);

        // the flag is stored: a fresh page opens in Advanced mode
        await page.reload();
        await expect.poll(() => visibleCount(page, "Maximum reviews/day")).toBeGreaterThan(0);
    } finally {
        await setAdvancedUi(page, false);
    }
});

// Pins spec/deck-options.md#deck-options.simple-view: Easy Days is collapsed
// behind its expander only in Simple mode; Advanced mode shows the sliders.
test("Easy Days is collapsed in Simple mode and open in Advanced mode", async ({ page }) => {
    await setAdvancedUi(page, false);
    await page.goto("/deck-options/1");
    await expect(page.locator("details.easy-days")).toHaveCount(1);
    expect(await visibleCount(page, "Mon")).toBe(0);

    await setAdvancedUi(page, true);
    try {
        await page.goto("/deck-options/1");
        await expect(page.locator("details.easy-days")).toHaveCount(0);
        expect(await visibleCount(page, "Mon")).toBeGreaterThan(0);
    } finally {
        await setAdvancedUi(page, false);
    }
});

// Qt keeps the deck-options page loaded between openings and moves it to the
// chosen deck with anki.deckOptionsSwitch (qt/aqt/deckoptions.py). The
// settings must still come fresh from the collection, and the ready signal
// must carry the new generation number in its referrer, which Qt uses to
// ignore a late signal from an earlier load.
test("a kept page switched to a deck shows the collection's current settings", async ({ page }) => {
    await setAdvancedUi(page, false);
    await page.goto("/deck-options/1?g=1");
    await expect.poll(() => visibleCount(page, "Desired retention")).toBeGreaterThan(0);
    expect(await visibleCount(page, "Algorithm (global)")).toBe(0);

    // the collection changes while the page is kept
    await setAdvancedUi(page, true);
    try {
        const ready = page.waitForRequest(isRpc("deckOptionsReady"));
        await page.evaluate(() => (globalThis as any).anki.deckOptionsSwitch("/deck-options/1?g=2"));
        const referrer = new URL((await ready).headers()["referer"]);
        expect(referrer.pathname).toBe("/deck-options/1");
        expect(referrer.searchParams.get("g")).toBe("2");
        await expect.poll(() => visibleCount(page, "Algorithm (global)")).toBeGreaterThan(0);
    } finally {
        await setAdvancedUi(page, false);
    }
});

// Pins spec/deck-options.md#deck-options.simple-view (one bury switch) and
// #deck-options.glossary-term (the underlined words).
test("burying is one switch with the same name in both modes", async ({ page }) => {
    for (const advanced of [false, true]) {
        await setAdvancedUi(page, advanced);
        await page.goto("/deck-options/1");
        await expect
            .poll(() => visibleCount(page, "Hide related cards until tomorrow"))
            .toBe(1);
        // the three per-type switches are gone from both modes
        for (
            const gone of [
                "Bury new siblings",
                "Bury review siblings",
                "Bury interday learning siblings",
            ]
        ) {
            expect(await visibleCount(page, gone)).toBe(0);
        }
    }
});

test("the words \"related cards\" explain themselves on hover", async ({ page }) => {
    await setAdvancedUi(page, false);
    await page.goto("/deck-options/1");
    const term = page.locator(".glossary-term", { hasText: "related cards" }).first();
    await expect(term).toBeVisible();

    await term.hover();
    await expect(
        page.getByText("cards that belong to the same note"),
    ).toBeVisible();
});
