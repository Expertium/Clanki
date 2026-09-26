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
    document.documentElement.className = "clanki-held";
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
    document.documentElement.className = "";
    vi.useRealTimers();
    vi.restoreAllMocks();
});

// spec review.first-card-one-frame: a new reviewer page is drawn hidden;
// its first card tells Python it is in, and the paint wait starts only once
// the page is shown, so that it waits for a frame the user sees.
test("a held page reports its first card and waits to be shown", async () => {
    mocks.preloadResources.mockResolvedValue();
    const frameCallbacks: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
        frameCallbacks.push(callback);
        return frameCallbacks.length;
    });

    _showQuestion("first question", "first answer", "card card1", "question:1:101");
    await flushPromises();

    expect(document.getElementById("qa")!.innerHTML).toBe("first question");
    expect(mocks.bridgeCommand).toHaveBeenCalledWith("qaHeld:question:1:101");
    expect(frameCallbacks).toHaveLength(0);

    // Python shows the page (aqt.page_reveal.show_js)
    document.documentElement.classList.remove("clanki-held");
    window.dispatchEvent(new Event("clanki-shown"));
    await flushPromises();
    expect(frameCallbacks).toHaveLength(1);
    frameCallbacks.shift()!(0);
    frameCallbacks.shift()!(16);
    await flushPromises();

    expect(mocks.bridgeCommand).toHaveBeenCalledWith("qaPresented:question:1:101");
});

test("a page that is not held does not wait", async () => {
    document.documentElement.className = "";
    mocks.preloadResources.mockResolvedValue();
    const frameCallbacks: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
        frameCallbacks.push(callback);
        return frameCallbacks.length;
    });

    _showQuestion("second question", "second answer", "card card1", "question:2:202");
    await flushPromises();

    expect(mocks.bridgeCommand).not.toHaveBeenCalledWith("qaHeld:question:2:202");
    expect(frameCallbacks).toHaveLength(1);
    frameCallbacks.shift()!(0);
    frameCallbacks.shift()!(16);
    await flushPromises();
    expect(mocks.bridgeCommand).toHaveBeenCalledWith("qaPresented:question:2:202");
});

// last: its update never finishes here (no frame is painted)
test("a held page shows itself when Python does not answer", async () => {
    vi.useFakeTimers();
    mocks.preloadResources.mockResolvedValue();
    vi.spyOn(window, "requestAnimationFrame").mockImplementation(() => 1);

    _showQuestion("first question", "first answer", "card card1", "question:1:101");
    await vi.advanceTimersByTimeAsync(0);
    expect(document.documentElement.classList.contains("clanki-held")).toBe(true);

    await vi.advanceTimersByTimeAsync(1000);
    expect(document.documentElement.classList.contains("clanki-held")).toBe(false);
});
