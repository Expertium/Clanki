// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    GraphsResponse,
    GraphsResponse_Retrievability,
    GraphsResponse_Retrievability_Series,
} from "@generated/anki/stats_pb";
import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures";
import { isRpc, setAdvancedUi } from "./helpers";

const largeSeedCount = Number(process.env.ANKI_E2E_SEED_REVIEW_CARDS || 0);
const fakeRwkvBackendEnabled = process.env.ANKI_E2E_FAKE_RWKV_BACKEND === "1";
const graphDebugPath = "/graphs?currentDeckId=1&graphDebug=1";

function graphResponsePromise(page: Page) {
    return page.waitForResponse((response) => {
        return response.request().method() === "POST"
            && new URL(response.url()).pathname === "/_anki/graphs";
    });
}

function collectGraphConsoleMessages(page: Page): string[] {
    const graphConsoleMessages: string[] = [];
    page.on("console", (message) => {
        const text = message.text();
        if (text.includes("graphs")) {
            graphConsoleMessages.push(`[${message.type()}] ${text}`);
        }
    });

    return graphConsoleMessages;
}

async function expectGraphConsoleMessage(
    graphConsoleMessages: string[],
    expected: string,
): Promise<void> {
    await expect.poll(() => {
        return graphConsoleMessages.some((message) => message.includes(expected));
    }).toBeTruthy();
}

// Today and Card Retrievability are Advanced-only graphs: in Simple mode the
// Stats page draws Reviews, Card Counts, Retention and Total Knowledge alone
// (spec/ui.md, `ui.mode-switch`), so both tests below ask for Advanced mode
// and put the collection back in Simple mode, its default, afterwards.
test("graphs page clears loading state after graph data arrives", async ({ page }) => {
    const graphConsoleMessages = collectGraphConsoleMessages(page);
    const responsePromise = graphResponsePromise(page);

    await setAdvancedUi(page, true);
    try {
        await page.goto(graphDebugPath);
        await expect(page.locator("#statisticsSearchText")).toBeVisible();

        const graphResponse = await responsePromise;
        expect(graphResponse.ok()).toBeTruthy();

        await expect(page.locator(".spin.loading")).toHaveCount(0);
        await expect(page.getByRole("heading", { name: "Today" })).toBeVisible();
        await expectGraphConsoleMessage(graphConsoleMessages, "graphs postProto decoded");
        await expectGraphConsoleMessage(graphConsoleMessages, "graphs data applied");

        await test.info().attach("graphs-console", {
            body: graphConsoleMessages.join("\n") || "(no graph console messages)",
            contentType: "text/plain",
        });
    } finally {
        await setAdvancedUi(page, false);
    }
});

test("RWKV retrievability graph is visible when FSRS is disabled", async ({ page }) => {
    await page.route("**/_anki/graphs", async (route) => {
        const response = await route.fetch();
        const graphs = GraphsResponse.fromBinary(await response.body());
        graphs.fsrs = false;
        graphs.retrievability ??= new GraphsResponse_Retrievability();
        graphs.retrievability.rwkv = new GraphsResponse_Retrievability_Series({
            retrievability: { 75: 1 },
            average: 75,
            sumByCard: 0.75,
            sumByNote: 0.75,
        });
        await route.fulfill({
            response,
            body: Buffer.from(graphs.toBinary()),
        });
    });

    await setAdvancedUi(page, true);
    try {
        await page.goto(graphDebugPath);

        await expect(page.getByRole("heading", { name: "Card Retrievability" })).toBeVisible();
    } finally {
        await setAdvancedUi(page, false);
    }
});

// Pins spec/ui.md#ui.stats-total-knowledge: Simple mode draws the estimate
// alone and explains it in plain words; Advanced mode has the Reviewed
// checkbox, the algorithm's name and the upper-bound note. Neither the
// checkbox nor a mode switch asks the backend for the graph again: the page
// keeps the graph's block (the list is keyed by component), so its loaded
// data and the checkbox itself survive the switch.
test("Total Knowledge: the modes differ, and switching does not load it again", async ({ page }) => {
    const description =
        "This is Clanki's best estimate of how many cards you knew at each point in your review history.";
    const upperBound =
        "Reviewed is an upper bound on your knowledge: it counts every card you have ever rated, as if you never forgot one.";
    let loads = 0;
    page.on("request", (request) => {
        if (isRpc("totalKnowledge")(request)) {
            loads += 1;
        }
    });

    await page.goto(graphDebugPath);
    await expect(page.getByRole("heading", { name: "Total Knowledge" })).toBeVisible();
    await expect.poll(() => loads).toBe(1);

    await page.getByRole("button", { name: "Advanced", exact: true }).click();
    const reviewed = page.getByRole("checkbox", { name: "Reviewed" });
    await expect(reviewed).toBeChecked();
    // Fluent wraps the algorithm's name in isolation marks, so match the start
    await expect(page.getByText(/^Algorithm: /)).toBeVisible();
    await expect(page.getByText(upperBound)).toBeVisible();
    await expect(page.getByText(description)).toHaveCount(0);

    // the checkbox only hides the line
    await reviewed.uncheck();
    await expect(reviewed).not.toBeChecked();

    await page.getByRole("button", { name: "Simple", exact: true }).click();
    await expect(page.getByText(description)).toBeVisible();
    await expect(page.getByRole("checkbox", { name: "Reviewed" })).toHaveCount(0);
    await expect(page.getByText(upperBound)).toHaveCount(0);

    // back to Advanced: the same graph, so the checkbox kept its state
    await page.getByRole("button", { name: "Advanced", exact: true }).click();
    await expect(page.getByRole("checkbox", { name: "Reviewed" })).not.toBeChecked();
    expect(loads).toBe(1);

    // leave the collection in its default mode for the tests that follow
    await page.getByRole("button", { name: "Simple", exact: true }).click();
    await expect(page.getByText(description)).toBeVisible();
});

test("seeded RWKV graphs page clears loading after bulk stats scoring", async ({ page }) => {
    test.skip(
        largeSeedCount <= 0 || !fakeRwkvBackendEnabled,
        "requires ANKI_E2E_SEED_REVIEW_CARDS and ANKI_E2E_FAKE_RWKV_BACKEND=1",
    );
    test.setTimeout(120_000);

    const graphConsoleMessages = collectGraphConsoleMessages(page);
    const responsePromise = graphResponsePromise(page);
    const start = Date.now();

    await page.goto(graphDebugPath);
    await expect(page.locator("#statisticsSearchText")).toBeVisible();

    const graphResponse = await responsePromise;
    const responseElapsedMs = Date.now() - start;
    expect(graphResponse.ok()).toBeTruthy();

    await expect(page.locator(".spin.loading")).toHaveCount(0, { timeout: 15_000 });
    const clearElapsedMs = Date.now() - start;
    await expect(page.getByRole("heading", { name: "Card Counts" })).toBeVisible();
    await expect(page.getByRole("row", { name: /Total\s+10,000/ })).toBeVisible();
    await expectGraphConsoleMessage(graphConsoleMessages, "graphs postProto decoded");
    await expectGraphConsoleMessage(graphConsoleMessages, "graphs data applied");

    console.log(
        `seeded RWKV graphs loaded: cards=${largeSeedCount} response_elapsed_ms=${responseElapsedMs} clear_elapsed_ms=${clearElapsedMs}`,
    );

    await test.info().attach("seeded-rwkv-graphs-console", {
        body: graphConsoleMessages.join("\n") || "(no graph console messages)",
        contentType: "text/plain",
    });
});
