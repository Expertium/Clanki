// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";

/** The switch's box, rounded to a tenth of a pixel. */
async function switchBox(page: Page): Promise<string> {
    return page.locator(".ui-mode").first().evaluate((element) => {
        const box = element.getBoundingClientRect();
        return [box.x, box.y, box.width, box.height]
            .map((value) => value.toFixed(1))
            .join(" ");
    });
}

/**
 * Hovers each side of the page's Simple | Advanced switch with a real mouse
 * and checks the switch's box against its box at rest. The pages' global
 * `button:not(.btn, .btn-close):hover` rule gives a hovered button a 1px
 * border, and that rule outranks the switch's own `border: none`; so this
 * fails if the switch ever takes that border again.
 */
async function expectSizeKeptUnderTheMouse(page: Page): Promise<void> {
    const options = page.locator(".ui-mode").first().locator("button");
    await expect(options).toHaveCount(2);
    await page.mouse.move(0, 0);
    const atRest = await switchBox(page);
    for (const index of [0, 1]) {
        await options.nth(index).hover();
        await expect(options.nth(index)).toHaveCSS("border-top-style", "none");
        expect(await switchBox(page)).toBe(atRest);
    }
}

// Pins spec/ui.md#ui.mode-switch: the control keeps its size when the mouse
// is over either side.
test("the Simple | Advanced switch keeps its size under the mouse", async ({ page }) => {
    await page.goto("/deck-options/1");
    await expect(page.locator(".ui-mode").first()).toBeVisible();
    await expectSizeKeptUnderTheMouse(page);

    await page.goto("/graphs?currentDeckId=1");
    await expect(page.locator(".ui-mode").first()).toBeVisible();
    await expectSizeKeptUnderTheMouse(page);
});
