// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "./fixtures";

// Pins spec/ui.md `ui.answer-button-focus-visible` (B29 in
// clanki-logs/reuse_and_bottlenecks.md): the dashed focus indicator on an
// answer button must show for keyboard focus and must NOT show for a mouse
// click, before or after release. This loads the real compiled
// css/reviewer-bottom.css against markup shaped like
// Reviewer._answerButtons() (qt/aqt/reviewer.py), the same way
// reviewer-mathjax.spec.ts loads the real reviewer JS bundle against a
// minimal mocked page — the actual live reviewer page is a Qt webview
// (served at /_anki/legacyPageData?id=<webview-id>) rather than a plain
// navigable URL, so this is the closest e2e route to the real stylesheet.
test("answer button focus indicator shows for keyboard focus, not for a mouse click", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/reviewer-focus-visible-test", (route) =>
        route.fulfill({
            contentType: "text/html",
            body: `<html><head>
                <link rel="stylesheet" href="/_anki/css/reviewer-bottom.css">
                <script>window.pycmd = () => {};</script>
                </head><body>
                <div id="middle">
                <center><table cellpadding=0 cellspacing=0><tr>
                <td align=center><button class="answerButton answerIncorrect" data-ease="1" onclick='pycmd("ease1")'>Again</button></td>
                <td align=center><button class="answerButton answerCorrect" data-ease="2" onclick='pycmd("ease2")'>Good</button></td>
                </tr></table></center>
                </div>
                </body></html>`,
        }));
    await page.goto("/reviewer-focus-visible-test");

    const again = page.locator("button.answerButton.answerIncorrect");
    const good = page.locator("button.answerButton.answerCorrect");

    // A real mouse click (Playwright dispatches genuine input events) focuses
    // the button, exactly like Andrew's report. The dashed indicator must
    // not appear while the button holds focus after the click.
    await good.click();
    await expect(good).toBeFocused();
    await expect(good).toHaveCSS("border-style", "solid");

    // Move focus with the keyboard instead: Tab from "Again" reaches "Good"
    // in document order. This is genuine keyboard navigation, so Chromium's
    // :focus-visible heuristic marks the newly focused button, and the
    // dashed indicator must show.
    await again.focus();
    await page.keyboard.press("Tab");
    await expect(good).toBeFocused();
    await expect(good).toHaveCSS("border-style", "dashed");

    expect(errors).toEqual([]);
});
