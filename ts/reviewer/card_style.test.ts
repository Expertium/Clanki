// @vitest-environment jsdom
// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { afterEach, beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
    bridgeCommand: vi.fn(),
    preloadResources: vi.fn<(html: string) => Promise<void>>(),
}));

vi.mock("jquery/dist/jquery", () => ({ default: {} }));
vi.mock("@tslib/bridgecommand", () => ({ bridgeCommand: mocks.bridgeCommand }));
vi.mock("@tslib/runtime-require", () => ({ registerPackage: vi.fn() }));
vi.mock("../routes/image-occlusion/review", () => ({
    imageOcclusionAPI: { setup: vi.fn() },
}));
vi.mock("./answering", () => ({ mutateNextCardStates: vi.fn() }));
vi.mock("./browser_selector", () => ({ addBrowserClasses: vi.fn() }));
vi.mock("./images", () => ({
    allImagesLoaded: () => Promise.resolve([]),
    preloadAnswerImages: vi.fn(),
}));
vi.mock("./preload", () => ({ preloadResources: mocks.preloadResources }));

import { _showQuestion } from "./index";

async function flushPromises(): Promise<void> {
    for (let i = 0; i < 20; i++) {
        await Promise.resolve();
    }
}

beforeEach(() => {
    document.body.innerHTML = "<div id=\"qa\">old answer</div>";
    document.body.className = "";
    mocks.bridgeCommand.mockReset();
    mocks.preloadResources.mockReset();
    vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
    Object.assign(globalThis, {
        MathJax: {
            startup: { promise: Promise.resolve() },
            typesetClear: vi.fn(),
            typesetPromise: () => Promise.resolve(),
        },
    });
});

afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
});

// spec review.card-style-with-content: while a card's script loads,
// Chromium paints what is there, so the card's classes must already be on
// the body when its content goes in.
test("the card's classes are on the body as soon as its content is", async () => {
    mocks.preloadResources.mockResolvedValue();
    vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => 1);

    // the script never loads here, so the update stops while it waits for it
    _showQuestion(
        "<p>question</p><script src=\"_card.js\"></script>",
        "answer",
        "card card1",
        "question:1:101",
    );
    await flushPromises();

    expect(document.getElementById("qa")!.innerHTML).toContain("<p>question</p>");
    expect(document.body.className).toBe("card card1");
});
