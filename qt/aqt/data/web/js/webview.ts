/* Copyright: Ankitects Pty Ltd and contributors
 * License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html */

// prevent backspace key from going back a page
document.addEventListener("keydown", function(evt: KeyboardEvent) {
    if (evt.keyCode !== 8) {
        return;
    }
    let isText = 0;
    const node = evt.target as Element;
    const nn = node.nodeName;
    if (nn === "INPUT" || nn === "TEXTAREA") {
        isText = 1;
    } else if (nn === "DIV" && (node as HTMLDivElement).contentEditable) {
        isText = 1;
    }
    if (!isText) {
        evt.preventDefault();
    }
});

/* A new page drawn into the open one (aqt.webview, spec ui.screen-one-frame).
 *
 * The deck list, a deck's overview and their bottom bars share one frame (the
 * same head, apart from the screen's own style sheets and scripts). When one
 * of them is drawn over another, Python stages the new body here: its style
 * sheets are loaded but not applied, and its content is built and its scripts
 * are run in a hidden box, while the old page stays on screen. Showing it,
 * with the other pages of the same screen change, is one short task: the
 * page then is the page a fresh load gives (the screen's style sheets, the
 * new body with its scripts run in order, scrolled to the top, the focus on
 * the autofocus element). */

interface ClankiStagedPage {
    token: string;
    box: HTMLElement;
    sheets: HTMLLinkElement[];
    // what the new content's scripts added to the head
    head: Node[];
}

let clankiStaged: ClankiStagedPage | null = null;
// The head of the frame: as parsed (this script is the body's first, so the
// head is complete), and what the frame's scripts add. Whatever else is in
// the head was added by the content's scripts (the heatmap's style sheet),
// and goes with the content.
const clankiFrameHead = new Set<Node>(Array.from(document.head.childNodes));

async function clankiAddedTo(parent: Node, run: () => Promise<void>): Promise<Node[]> {
    const before = new Set(Array.from(parent.childNodes));
    await run();
    return Array.from(parent.childNodes).filter((node) => !before.has(node));
}
function clankiScreenSheets(): HTMLLinkElement[] {
    return Array.from(
        document.querySelectorAll<HTMLLinkElement>("link[data-clanki-screen-css]"),
    );
}

function clankiIsFrame(node: Node): boolean {
    return node instanceof HTMLScriptElement && node.hasAttribute("data-clanki-frame");
}

/** The screen's style sheet `href`: the one the page has, or a new one,
 * loaded but not applied, after the page's current ones. */
function clankiSheet(href: string): Promise<HTMLLinkElement> {
    const current = clankiScreenSheets();
    const kept = current.find((link) => link.getAttribute("href") === href);
    if (kept) {
        return Promise.resolve(kept);
    }
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.type = "text/css";
    link.setAttribute("data-clanki-screen-css", "");
    link.media = "not all";
    return new Promise((resolve, reject) => {
        link.onload = () => resolve(link);
        link.onerror = () => reject(new Error(`${href}: not loaded`));
        link.href = href;
        const last = current[current.length - 1];
        if (last) {
            last.after(link);
        } else {
            document.head.appendChild(link);
        }
    });
}

/** Run `old`, a script that was inserted, not parsed, and so never runs:
 * a copy of it runs, and the promise is kept once it has run (a script with
 * a file, from the cache, as when the page is parsed). */
function clankiRun(old: HTMLScriptElement): Promise<void> {
    const script = document.createElement("script");
    for (const attribute of Array.from(old.attributes)) {
        script.setAttribute(attribute.name, attribute.value);
    }
    if (!script.hasAttribute("src")) {
        script.text = old.text;
        old.replaceWith(script);
        return Promise.resolve();
    }
    return clankiLoad(script, null, (node) => old.replaceWith(node));
}

/** Put `script` in the page with `insert`, and keep the promise once its
 * file (`src`, or its own) has run. */
function clankiLoad(
    script: HTMLScriptElement,
    src: string | null,
    insert: (node: HTMLScriptElement) => void,
): Promise<void> {
    return new Promise((resolve, reject) => {
        script.onload = () => resolve();
        script.onerror = () => reject(new Error(`${script.src}: not loaded`));
        if (src !== null) {
            script.src = src;
        }
        insert(script);
    });
}

/** Put the staged page in place of the current one. With `undo`, only for a
 * moment, to measure it: the returned function puts the current one back
 * (both inside one task, so nothing in between is drawn). */
function clankiPlace(staged: ClankiStagedPage, undo: boolean): () => void {
    const sheets = clankiScreenSheets();
    const media = sheets.map((link) => link.getAttribute("media"));
    for (const link of sheets) {
        if (!staged.sheets.includes(link)) {
            link.media = "not all";
        }
    }
    for (const link of staged.sheets) {
        link.removeAttribute("media");
    }
    const removed = document.createDocumentFragment();
    removed.append(
        ...Array.from(document.body.childNodes).filter(
            (node) => node !== staged.box && !clankiIsFrame(node),
        ),
    );
    const content = Array.from(staged.box.childNodes);
    document.body.append(...content);
    staged.box.remove();
    if (!undo) {
        for (const link of sheets) {
            if (!staged.sheets.includes(link)) {
                link.remove();
            }
        }
        for (const node of Array.from(document.head.childNodes)) {
            if (
                !clankiFrameHead.has(node)
                && !staged.head.includes(node)
                && !staged.sheets.includes(node as HTMLLinkElement)
            ) {
                node.remove();
            }
        }
        return () => {};
    }
    return () => {
        staged.box.append(...content);
        document.body.insertBefore(staged.box, document.body.firstChild);
        document.body.append(removed);
        sheets.forEach((link, index) => {
            const value = media[index];
            if (value === null) {
                link.removeAttribute("media");
            } else {
                link.media = value;
            }
        });
        for (const link of staged.sheets) {
            if (!sheets.includes(link)) {
                link.media = "not all";
            }
        }
    };
}

/** Stage a page, then `clankiPageReady:<token>:<height>` to Python.
 * `frameScripts`: scripts of the new page's frame that this page has not
 * run yet. */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- called from webview.py
async function clankiStagePage(
    token: string,
    body: string,
    css: string[],
    frameScripts: string[],
): Promise<void> {
    try {
        const template = document.createElement("template");
        template.innerHTML = body;
        const files = template.content.querySelectorAll("script[src]").length;
        const sheets = await Promise.all(css.map(clankiSheet));
        // the new content, built and its scripts run where it is not seen and
        // takes no room; first in the body, so that its ids are the ones found
        const box = document.createElement("div");
        box.setAttribute("data-clanki-staged", token);
        box.style.cssText = "position: fixed; top: 0; left: 0; width: 100%; height: 0;"
            + " overflow: hidden; contain: strict; visibility: hidden; opacity: 0;"
            + " pointer-events: none;";
        box.append(template.content);
        const content = Array.from(document.body.childNodes).find(
            (node) => !clankiIsFrame(node),
        ) ?? null;
        const frameHead = await clankiAddedTo(document.head, async () => {
            for (const url of frameScripts) {
                const script = document.createElement("script");
                script.setAttribute("data-clanki-frame", "");
                await clankiLoad(script, url, (node) => document.body.insertBefore(node, content));
            }
        });
        frameHead.forEach((node) => clankiFrameHead.add(node));
        document.body.insertBefore(box, document.body.firstChild);
        const head: Node[] = [];
        const added = await clankiAddedTo(document.body, async () => {
            head.push(
                ...await clankiAddedTo(document.head, async () => {
                    for (const script of Array.from(box.querySelectorAll("script"))) {
                        await clankiRun(script);
                    }
                }),
            );
        });
        // what the scripts added to the body is the new content's too
        box.append(...added);
        const staged: ClankiStagedPage = { token, box, sheets, head };
        let height = document.documentElement.offsetHeight;
        if (!files && !box.querySelector("script")) {
            // the height the page will have, for the web view that takes it
            // (the bottom bar)
            const undo = clankiPlace(staged, true);
            height = document.documentElement.offsetHeight;
            undo();
        }
        clankiStaged = staged;
        pycmd(`clankiPageReady:${token}:${height}`);
    } catch (error) {
        console.error(error);
        pycmd(`clankiPageFailed:${token}`);
    }
}

/** Show the staged page `token`; false if it is not the staged one. */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- called from webview.py
function clankiShowPage(token: string): boolean {
    const staged = clankiStaged;
    if (!staged || staged.token !== token || !staged.box.isConnected) {
        return false;
    }
    clankiStaged = null;
    clankiPlace(staged, false);
    window.scrollTo(0, 0);
    window.getSelection()?.removeAllRanges();
    (document.activeElement as HTMLElement | null)?.blur?.();
    document.querySelector<HTMLElement>("[autofocus]")?.focus();
    document.documentElement.setAttribute("data-clanki-hold", token);
    window.dispatchEvent(new Event("clanki-page-shown"));
    window.dispatchEvent(new Event("clanki-shown"));
    return true;
}

/** Run scripts Python queued for the page, each on its own, as separate
 * evaluations would. */
// eslint-disable-next-line @typescript-eslint/no-unused-vars -- called from webview.py
function clankiRunAll(scripts: string[]): void {
    for (const script of scripts) {
        try {
            (0, eval)(script);
        } catch (error) {
            console.error(error);
        }
    }
}
