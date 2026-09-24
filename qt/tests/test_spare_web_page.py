# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The spare started web page that Add, Edit, the Browser's editor and Card
Info adopt instead of starting a renderer of their own."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

import aqt
import aqt.fsrs_predictions
from aqt import rwkv_scheduler, webview
from aqt.webview import AnkiWebViewKind, _SparePage


class _History:
    def __init__(self) -> None:
        self.cleared = False

    def clear(self) -> None:
        self.cleared = True


class _Page:
    # the signal is connected through the patched qconnect
    loadFinished = None

    def __init__(self, handler: Callable[[str], Any], kind: AnkiWebViewKind) -> None:
        self._onBridgeCmd = handler
        self._kind = kind
        self.parent: object = None
        self.loaded: list[str] = []
        self._history = _History()
        self.on_load: Callable[[bool], None] | None = None

    def setBackgroundColor(self, color: object) -> None:
        pass

    def setParent(self, parent: object) -> None:
        self.parent = parent

    def history(self) -> _History:
        return self._history

    def load(self, url: object) -> None:
        self.loaded.append(str(url))

    def deleteLater(self) -> None:
        pass


class _Progress:
    def __init__(self) -> None:
        self.scheduled: list[tuple[int, Callable[[], None]]] = []

    def single_shot(
        self, ms: int, func: Callable[[], None], requires_collection: bool = True
    ) -> None:
        self.scheduled.append((ms, func))


@pytest.fixture
def spare(monkeypatch: pytest.MonkeyPatch) -> tuple[_SparePage, Any]:
    mw = SimpleNamespace(
        col=object(),
        state="deckBrowser",
        progress=_Progress(),
        app=SimpleNamespace(activeModalWidget=lambda: None),
        mediaServer=SimpleNamespace(
            set_page_html=lambda *args: None, clear_page_html=lambda *args: None
        ),
        serverURL=lambda: "http://127.0.0.1:1/",
    )
    monkeypatch.setattr(aqt, "mw", mw)
    monkeypatch.setattr(rwkv_scheduler, "rwkv_state_cache_loading", lambda _mw: False)
    monkeypatch.setattr(aqt.fsrs_predictions, "is_holding_collection", lambda: False)
    pages: list[_Page] = []

    def make_page(handler: Callable[[str], Any], kind: AnkiWebViewKind) -> _Page:
        page = _Page(handler, kind)
        pages.append(page)
        return page

    monkeypatch.setattr(webview, "AnkiWebPage", make_page)
    monkeypatch.setattr(
        webview, "qconnect", lambda signal, slot: setattr(pages[-1], "on_load", slot)
    )
    mw.pages = pages
    return _SparePage(), mw


def _warm_now(mw: Any) -> None:
    _ms, func = mw.progress.scheduled.pop()
    func()


def test_the_first_spare_waits_for_start_up_then_starts_a_page(spare: Any) -> None:
    spare_page, mw = spare
    spare_page.on_profile_did_open()
    assert [ms for ms, _ in mw.progress.scheduled] == [_SparePage.WARM_DELAY_MS]
    _warm_now(mw)
    [page] = mw.pages
    assert page.loaded and "legacyPageData" in page.loaded[0]


def test_a_view_adopts_a_loaded_spare_and_a_new_one_follows(spare: Any) -> None:
    spare_page, mw = spare
    spare_page.on_profile_did_open()
    _warm_now(mw)
    [page] = mw.pages
    view, handler = object(), lambda cmd: cmd

    # still loading: the view makes its own page
    assert spare_page.take(AnkiWebViewKind.EDITOR, handler, view) is None
    page.on_load(True)
    # another kind of view never takes it
    assert spare_page.take(AnkiWebViewKind.DECK_STATS, handler, view) is None

    taken = spare_page.take(AnkiWebViewKind.BROWSER_CARD_INFO, handler, view)
    assert taken is page
    assert page.parent is view
    assert page._onBridgeCmd is handler
    assert page._kind is AnkiWebViewKind.BROWSER_CARD_INFO
    assert page.history().cleared
    # used once only; a new spare is made a little later
    assert spare_page.take(AnkiWebViewKind.EDITOR, handler, view) is None
    assert _SparePage.REWARM_MS in [ms for ms, _ in mw.progress.scheduled]


@pytest.mark.parametrize(
    "busy",
    ["state", "modal", "rwkv", "fsrs"],
)
def test_no_spare_is_started_while_something_holds_the_user(
    spare: Any, monkeypatch: pytest.MonkeyPatch, busy: str
) -> None:
    spare_page, mw = spare
    if busy == "state":
        mw.state = "profileManager"
    elif busy == "modal":
        mw.app.activeModalWidget = object
    elif busy == "rwkv":
        monkeypatch.setattr(
            rwkv_scheduler, "rwkv_state_cache_loading", lambda _mw: True
        )
    else:
        monkeypatch.setattr(aqt.fsrs_predictions, "is_holding_collection", lambda: True)
    spare_page.on_profile_did_open()
    _warm_now(mw)
    assert mw.pages == []
    assert [ms for ms, _ in mw.progress.scheduled] == [_SparePage.WARM_RETRY_MS]


def test_closing_the_profile_releases_the_spare_and_stops_the_warm_up(
    spare: Any,
) -> None:
    spare_page, mw = spare
    spare_page.on_profile_did_open()
    _warm_now(mw)
    mw.pages[0].on_load(True)
    spare_page.on_profile_will_close()
    assert spare_page.take(AnkiWebViewKind.EDITOR, lambda cmd: None, object()) is None
    # a warm-up scheduled for the closed profile does nothing
    spare_page._warm(spare_page._profile - 1)
    assert len(mw.pages) == 1
