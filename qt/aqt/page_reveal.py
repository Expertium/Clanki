# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pages drawn hidden and shown together (spec ui.screen-one-frame).

A screen change often loads two pages at once: the main web view and the
bottom bar. Each page used to appear as soon as Chromium had something to
paint, so the screen went through half-built frames: the bottom bar changed
before the main view, and the main view was painted while its parser waited
between two scripts (the review heatmap without its squares, then without
its stats line).

A held page carries the class `clanki-held` on its `<html>`, which makes it
transparent (`opacity: 0`: the layout stays, so scripts that measure still
work, and Chromium does not count a transparent page as painted). While the
new page shows nothing, Chromium keeps showing the previous page of that web
view. A held page is ready when its DOM is done, or, for a page that fills
itself in later (the reviewer's first card), when the page says so. Once
every held page is ready, all of them are shown in one go, so the screen
goes from the old state to the finished new state.

Each held page has its own token in `data-clanki-hold`, and says it is ready
with that token (`clankiHeld:<token>`, sent by the bridge script right after
`domDone`). A `domDone` that arrives late from the page before it cannot
count for the new one.

A page never stays hidden: Python shows everything still held
HOLD_TIMEOUT_MS after the last hold began (a page whose DOM is not done by
then is shown the moment it is), and the page's own CSS shows it after
CSS_TIMEOUT_MS even if Python never answers. Chromium keeps the previous
page only for about half a second, so a page slower than that shows as it
fills in, as every page did before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aqt.qt import QTimer, sip

if TYPE_CHECKING:
    from aqt.webview import AnkiWebView

HOLD_CLASS = "clanki-held"
HOLD_ATTRIBUTE = "data-clanki-hold"
HOLD_TIMEOUT_MS = 500
CSS_TIMEOUT_MS = 2000

HOLD_CSS = f"""<style>
html.{HOLD_CLASS} {{
    opacity: 0;
    will-change: opacity;
    animation: clanki-held-show 0s linear {CSS_TIMEOUT_MS}ms forwards;
}}
@keyframes clanki-held-show {{
    to {{ opacity: 1; }}
}}
</style>"""

# sent by the bridge script of every page (aqt.webview), after domDone
READY_JS = f"""
var clankiHold = document.documentElement.getAttribute("{HOLD_ATTRIBUTE}");
if (clankiHold) {{
    pycmd("clankiHeld:" + clankiHold + ":" + document.documentElement.offsetHeight);
}}
"""
READY_COMMAND = "clankiHeld:"


def show_js(token: str) -> str:
    """Show the page, if it is still the held page `token`. `clanki-shown`
    lets the page's own script wait for that moment."""
    return f"""(function(){{
const root = document.documentElement;
if (root.getAttribute("{HOLD_ATTRIBUTE}") !== {token!r}) return;
root.classList.remove("{HOLD_CLASS}");
window.dispatchEvent(new Event("clanki-shown"));
}})();"""


class _Held:
    def __init__(self, web: AnkiWebView, token: str) -> None:
        self.web = web
        self.token = token
        self.ready_at_dom_done = True
        # the load that hold() announced has not started yet
        self.load_pending = True
        self.dom_done = False
        self.ready = False
        # the page's height, sent with its ready message, and whether the
        # web view takes it when the page is shown (the bottom bar)
        self.height: int | None = None
        self.fit_height = False


class PageReveal:
    """The pages held right now, keyed by web view."""

    def __init__(self) -> None:
        self._held: dict[int, _Held] = {}
        # held pages the timeout gave up on, shown when their DOM is done
        self._released: dict[int, _Held] = {}
        self._count = 0
        self._timer: QTimer | None = None

    def hold(self, web: AnkiWebView) -> str:
        """`web` is about to load a held page. Returns the page's token."""
        self._count += 1
        entry = _Held(web, f"h{self._count}")
        self._held[id(web)] = entry
        self._released.pop(id(web), None)
        self._restart_timer()
        return entry.token

    def wait_for_signal(self, web: AnkiWebView) -> None:
        """The held page of `web` is ready when mark_ready() is called, not
        when its DOM is done."""
        if entry := self._held.get(id(web)):
            entry.ready_at_dom_done = False

    def load_started(self, web: AnkiWebView) -> None:
        """`web` starts to load a page: the held page announced by hold(),
        or else a page that is not held."""
        entry = self._held.get(id(web))
        if entry is not None and entry.load_pending:
            entry.load_pending = False
            return
        self._released.pop(id(web), None)
        if self._held.pop(id(web), None) is not None:
            self._show_if_all_ready()

    def is_held(self, web: AnkiWebView) -> bool:
        return id(web) in self._held

    def page_ready(self, web: AnkiWebView, message: str) -> None:
        """The DOM of a held page is done: `message` is its token and its
        height, `<token>:<height>`."""
        token, _, height = message.partition(":")
        for entry in (self._held.get(id(web)), self._released.get(id(web))):
            if entry is not None and entry.token == token and height.isdigit():
                entry.height = int(height)
        if (entry := self._released.get(id(web))) and entry.token == token:
            del self._released[id(web)]
            self._show(entry)
            return
        entry = self._held.get(id(web))
        if entry is None or entry.token != token:
            return
        entry.dom_done = True
        if entry.ready_at_dom_done:
            entry.ready = True
            self._show_if_all_ready()

    def mark_ready(self, web: AnkiWebView) -> None:
        """The held page of `web` says it is ready (wait_for_signal())."""
        if entry := self._released.pop(id(web), None):
            self._show(entry)
            return
        if entry := self._held.get(id(web)):
            entry.dom_done = entry.ready = True
            self._show_if_all_ready()

    def fit_height_when_shown(self, web: AnkiWebView) -> bool:
        """If `web` holds a page, it takes that page's height when the page
        is shown, and this returns True. Resizing the bottom bar before its
        new page shows would show the old page at the new height."""
        entry = self._held.get(id(web)) or self._released.get(id(web))
        if entry is None:
            return False
        entry.fit_height = True
        return True

    def show_all(self) -> None:
        """Show every held page now, or, where its DOM is not done yet, the
        moment it is."""
        held, self._held = list(self._held.values()), {}
        if self._timer is not None:
            self._timer.stop()
        for entry in held:
            if entry.dom_done:
                self._show(entry)
            else:
                self._released[id(entry.web)] = entry

    def _show(self, entry: _Held) -> None:
        # straight to the page, not through the web view's queue: the page
        # has just said it is there
        if _deleted(entry.web):
            return
        if entry.fit_height and entry.height is not None:
            entry.web.setFixedHeight(entry.height)
        entry.web.page().runJavaScript(show_js(entry.token))

    def _show_if_all_ready(self) -> None:
        if all(entry.ready for entry in self._held.values()):
            self.show_all()

    def _restart_timer(self) -> None:
        """Show everything still held HOLD_TIMEOUT_MS from now."""
        if self._timer is None:
            self._timer = QTimer()
            self._timer.setSingleShot(True)
            self._timer.timeout.connect(self.show_all)
        self._timer.start(HOLD_TIMEOUT_MS)


def _deleted(web: object) -> bool:
    return isinstance(web, sip.simplewrapper) and sip.isdeleted(web)


_reveal = PageReveal()


def page_reveal() -> PageReveal:
    return _reveal
