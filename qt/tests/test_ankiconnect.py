# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/ankiconnect.md (ankiconnect.http-protocol, ankiconnect.actions,
# ankiconnect.one-algorithm, ankiconnect.settings, ankiconnect.addon-blocked).
#
# The server runs on an ephemeral port against a test collection, and every
# request goes over HTTP, as a client's would.

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

import anki.lang
import aqt
from anki.cards_pb2 import FsrsMemoryState
from anki.collection import Collection
from anki.decks import DeckConfigId, DeckId
from aqt import ankiconnect, ankiconnect_server
from aqt.ankiconnect import (
    ADDON_NOTICE_SHOWN_KEY,
    AnkiConnect,
    AnkiConnectService,
    AnkiConnectSettings,
    action_names,
    disable_ankiconnect_addon,
    is_ankiconnect_addon,
    migrate_addon_settings,
)
from aqt.ankiconnect_server import ServerState

# strings, and the field checksums of duplicate checks, need the language
anki.lang.set_lang("en")

# the add-on's actions (AnkiWeb 2055492159) and its fork's additions
ADDON_ACTIONS = """
version requestPermission getProfiles getActiveProfile loadProfile sync multi
getNumCardsReviewedToday getNumCardsReviewedByDay getCollectionStatsHTML
deckNames deckNamesAndIds getDecks createDeck changeDeck deleteDecks
getDeckConfig saveDeckConfig setDeckConfigId cloneDeckConfigId
removeDeckConfigId getDeckStats storeMediaFile retrieveMediaFile
getMediaFilesNames deleteMediaFile getMediaDirPath addNote canAddNote
canAddNoteWithErrorDetail updateNoteFields updateNote updateNoteModel
updateNoteTags getNoteTags addTags removeTags getTags clearUnusedTags
replaceTags replaceTagsInAllNotes setEaseFactors setSpecificValueOfCard
getEaseFactors suspend unsuspend suspended areSuspended areDue getIntervals
modelNames createModel modelNamesAndIds findModelsById findModelsByName
modelNameFromId modelFieldNames modelFieldDescriptions modelFieldFonts
modelFieldsOnTemplates modelTemplates modelStyling updateModelTemplates
updateModelStyling findAndReplaceInModels modelTemplateRename
modelTemplateReposition modelTemplateAdd modelTemplateRemove
modelFieldRename modelFieldReposition modelFieldAdd modelFieldRemove
modelFieldSetFont modelFieldSetFontSize modelFieldSetDescription
deckNameFromId findNotes findCards cardsInfo cardsModTime forgetCards
relearnCards answerCards cardReviews getReviewsOfCards setDueDate
reloadCollection getLatestReviewID insertReviews notesInfo notesModTime
deleteNotes removeEmptyNotes cardsToNotes guiBrowse guiEditNote
guiSelectNote guiSelectCard guiSelectedNotes guiAddCards guiReviewActive
guiCurrentCard guiStartCardTimer guiShowQuestion guiShowAnswer
guiAnswerCard guiUndo guiDeckOverview guiDeckBrowser guiDeckReview
guiImportFile guiExitAnki guiCheckDatabase addNotes canAddNotes
canAddNotesWithErrorDetail exportPackage importPackage apiReflect
""".split()
FORK_ACTIONS = (
    "cardsDetails gradeNow repositionNewCards guiAddNoteSetData guiPlayAudio".split()
)


# the test server
######################################################################


class FakeTaskman:
    """The main thread's queue, run at once on the calling thread."""

    def __init__(self) -> None:
        self.on_main = 0
        self.users = 0

    def run_on_main(self, closure: Any) -> None:
        self.on_main += 1
        closure()

    def run_in_background(
        self, task: Any, on_done: Any = None, uses_collection: bool = True
    ) -> Future:
        future: Future = Future()
        try:
            future.set_result(task())
        except BaseException as e:
            future.set_exception(e)
        return future

    def collection_use_started(self) -> None:
        self.users += 1

    def collection_use_finished(self, *_: Any) -> None:
        self.users -= 1


class Server:
    def __init__(self, service: AnkiConnectService, col: Collection, mw: Any) -> None:
        self.service = service
        self.col = col
        self.mw = mw

    @property
    def port(self) -> int:
        return self.service.port

    def request(
        self,
        body: bytes | dict | None,
        headers: dict[str, str] | None = None,
        method: str = "POST",
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        data = json.dumps(body).encode() if isinstance(body, dict) else (body or b"")
        conn.request(method, "/", body=data, headers=headers or {})
        response = conn.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        conn.close()
        return result

    def configure(self, **changes: Any) -> None:
        """New settings, the port left as it is (no restart)."""
        self.service.apply_settings(replace(self.service.settings, **changes))

    def send(self, action: str, version: int = 6, **params: Any) -> Any:
        body: dict[str, Any] = {"action": action, "version": version}
        if params:
            body["params"] = params
        key = self.service.settings.api_key
        if key is not None:
            body["key"] = key
        status, _, data = self.request(body)
        assert status == 200
        return json.loads(data)

    def invoke(self, action: str, **params: Any) -> Any:
        reply = self.send(action, **params)
        assert reply["error"] is None, reply["error"]
        return reply["result"]

    def error(self, action: str, **params: Any) -> str:
        reply = self.send(action, **params)
        assert reply["result"] is None
        assert isinstance(reply["error"], str)
        return reply["error"]

    # fixtures of the collection

    def add_basic(self, front: str = "front", back: str = "back", **extra: Any) -> int:
        note = {
            "deckName": "Default",
            "modelName": "Basic",
            "fields": {"Front": front, "Back": back},
        }
        note.update(extra)
        return self.invoke("addNote", note=note)

    def cards_of(self, nid: int) -> list[int]:
        return self.invoke("findCards", query=f"nid:{nid}")


def _make_mw(col: Collection) -> Any:
    pm = SimpleNamespace(
        meta={},
        name="User 1",
        profile=None,
        db=None,
        profiles=lambda: ["User 1", "Other"],
        sync_auth=lambda: None,
        media_syncing_enabled=lambda: True,
        save=MagicMock(),
        load=MagicMock(),
        set_host_number=MagicMock(),
        set_current_sync_url=MagicMock(),
    )
    return SimpleNamespace(
        col=col,
        pm=pm,
        taskman=FakeTaskman(),
        reviewer=None,
        state="deckBrowser",
        update_undo_actions=MagicMock(),
        undo=MagicMock(),
        onOverview=MagicMock(),
        moveToState=MagicMock(),
        onCheckDB=MagicMock(),
        close=MagicMock(),
        isVisible=MagicMock(return_value=False),
        loadProfile=MagicMock(),
        profileDiag=MagicMock(),
        windowIcon=MagicMock(),
        on_sync_button_clicked=MagicMock(),
        unloadProfileAndShowProfileManager=MagicMock(),
    )


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    col = Collection(str(tmp_path / "collection.anki2"))
    mw = _make_mw(col)
    service = AnkiConnectService(mw, AnkiConnectSettings(enabled=True, bind_port=0))
    # the refresh is checked by its own test; here it just stays pending
    monkeypatch.setattr(service, "_schedule_refresh", lambda: None)
    # FSRS-7 unless a test says otherwise: the collection itself, so that the
    # cards are answered by the algorithm the action reports, and the
    # AnkiConnect layer, which the algorithm tests below drive for real
    col.set_config("schedulingAlgorithm", "fsrs7")
    col.decks.update_config(col.decks.get_config(DeckConfigId(1)))
    monkeypatch.setattr(AnkiConnect, "_card_algorithm", lambda self, card: "fsrs7")
    service.start(listen_now=True)
    # the "listening" status went to the main thread
    mw.taskman.on_main = 0
    yield Server(service, col, mw)
    service.shutdown()
    col.close()


# the action list
######################################################################


def test_the_action_list_is_the_addons_and_the_forks() -> None:
    assert action_names() == sorted(ADDON_ACTIONS + FORK_ACTIONS)


def test_every_action_is_covered() -> None:
    source = Path(__file__).read_text(encoding="utf8")
    missing = [
        name
        for name in action_names()
        if not re.search(
            rf"""(invoke|error|send)\(\s*"{name}"|"action": "{name}\"""", source
        )
    ]
    assert missing == []


# HTTP protocol (ankiconnect.http-protocol)
######################################################################


def test_version_over_http(server: Server) -> None:
    status, headers, body = server.request({"action": "version", "version": 6})
    assert status == 200
    assert json.loads(body) == {"result": 6, "error": None}
    assert headers["Content-Type"] == "application/json"
    assert headers["Access-Control-Allow-Origin"] == "http://localhost"
    assert headers["Access-Control-Allow-Headers"] == "*"
    assert int(headers["Content-Length"]) == len(body)


def test_api_version_4_gives_the_bare_result(server: Server) -> None:
    assert server.send("version", version=4) == 6
    # no version at all is version 4
    status, _, body = server.request({"action": "deckNames"})
    assert json.loads(body) == ["Default"]
    # errors keep the error shape
    assert server.send("nonsense", version=4) == {
        "result": None,
        "error": "unsupported action",
    }


def test_empty_body_gives_the_api_version(server: Server) -> None:
    status, _, body = server.request(None, method="GET")
    assert status == 200
    assert json.loads(body) == {"apiVersion": "AnkiConnect v.6"}


def test_invalid_json_gives_an_error(server: Server) -> None:
    status, _, body = server.request(b"{not json")
    reply = json.loads(body)
    assert status == 200 and reply["result"] is None
    assert reply["error"].startswith("Expecting property name")


def test_schema_is_checked(server: Server) -> None:
    for request in ({"version": 6}, {"action": ""}, {"action": "version", "params": 1}):
        reply = json.loads(server.request(request)[2])
        assert reply["result"] is None and reply["error"]


def test_unsupported_action(server: Server) -> None:
    assert server.error("noSuchAction") == "unsupported action"
    assert server.error("_call") == "unsupported action"


def test_wrong_parameter_gives_pythons_message(server: Server) -> None:
    assert server.error("findNotes", nonsense=1) == (
        "AnkiConnect.findNotes() got an unexpected keyword argument 'nonsense'"
    )


def test_api_key_is_required_when_set(server: Server) -> None:
    server.configure(api_key="secret")
    body = {"action": "deckNames", "version": 6}
    assert json.loads(server.request(body)[2])["error"] == (
        "valid api key must be provided"
    )
    wrong = json.loads(server.request({**body, "key": "wrong"})[2])
    assert wrong["error"] == "valid api key must be provided"
    assert json.loads(server.request({**body, "key": "secret"})[2]) == {
        "result": ["Default"],
        "error": None,
    }
    # requestPermission needs no key
    reply = json.loads(server.request({"action": "requestPermission", "version": 6})[2])
    assert reply["result"] == {
        "permission": "granted",
        "requireApikey": True,
        "version": 6,
    }
    # each action of multi needs its own key
    reply = json.loads(
        server.request(
            {
                "action": "multi",
                "version": 6,
                "key": "secret",
                "params": {
                    "actions": [
                        {"action": "version", "version": 6},
                        {"action": "version", "version": 6, "key": "secret"},
                    ]
                },
            }
        )[2]
    )
    assert reply["result"] == [
        {"result": None, "error": "valid api key must be provided"},
        {"result": 6, "error": None},
    ]


def test_api_key_without_one_set_a_key_is_refused(server: Server) -> None:
    # as in the add-on: key != apiKey (None)
    reply = json.loads(
        server.request({"action": "version", "version": 6, "key": "x"})[2]
    )
    assert reply["error"] == "valid api key must be provided"


@pytest.mark.parametrize(
    "origin, allowed, header",
    [
        ("http://localhost", True, "http://localhost"),
        ("http://127.0.0.1", True, "http://127.0.0.1"),
        ("https://127.0.0.1", True, "https://127.0.0.1"),
        ("http://127.0.0.1:8080", True, "http://127.0.0.1:8080"),
        # the add-on tests the http prefix twice: https with a port is out
        ("https://127.0.0.1:8080", False, "http://localhost"),
        ("chrome-extension://abc", True, "chrome-extension://abc"),
        ("moz-extension://abc", True, "moz-extension://abc"),
        ("safari-web-extension://abc", True, "safari-web-extension://abc"),
        ("https://example.com", False, "http://localhost"),
    ],
)
def test_cors_origins(server: Server, origin: str, allowed: bool, header: str) -> None:
    status, headers, body = server.request(
        {"action": "version", "version": 6}, headers={"Origin": origin}
    )
    assert headers["Access-Control-Allow-Origin"] == header
    if allowed:
        assert status == 200 and json.loads(body)["result"] == 6
    else:
        assert status == 403 and body == b""


def test_cors_star_and_listed_origins(server: Server) -> None:
    server.configure(cors_origins=("https://example.com",))
    status, headers, _ = server.request(
        {"action": "version", "version": 6}, headers={"Origin": "https://example.com"}
    )
    assert (
        status == 200
        and headers["Access-Control-Allow-Origin"] == "https://example.com"
    )
    # http://localhost is no longer listed, so 127.0.0.1 is not admitted
    assert (
        server.request({"action": "version"}, headers={"Origin": "http://127.0.0.1"})[0]
        == 403
    )
    server.configure(cors_origins=("*",))
    status, headers, _ = server.request(
        {"action": "version"}, headers={"Origin": "https://anything.org"}
    )
    assert status == 200 and headers["Access-Control-Allow-Origin"] == "*"


def test_cors_deprecated_single_origin(server: Server) -> None:
    server.configure(cors_origin="https://one.org")
    status, _, _ = server.request(
        {"action": "version"}, headers={"Origin": "https://one.org"}
    )
    assert status == 200


def test_options_preflight(server: Server) -> None:
    status, headers, body = server.request(
        None,
        method="OPTIONS",
        headers={
            "Origin": "http://localhost",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert status == 200 and body == b""
    assert headers["Access-Control-Allow-Private-Network"] == "true"
    status, headers, _ = server.request(None, method="OPTIONS")
    assert "Access-Control-Allow-Private-Network" not in headers


def test_forbidden_origin_gets_no_body_and_runs_nothing(server: Server) -> None:
    status, headers, body = server.request(
        {"action": "createDeck", "version": 6, "params": {"deck": "Evil"}},
        headers={"Origin": "https://evil.example"},
    )
    assert status == 403 and body == b""
    assert "Evil" not in server.invoke("deckNames")


class FakeMessageBox:
    StandardButton = None  # set below
    Icon = None
    answer: Any = None
    ignore_ticked = False
    shown: list[str] = []

    def __init__(self, parent: Any = None) -> None:
        pass

    def setText(self, text: str) -> None:
        FakeMessageBox.shown.append(text)

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: None

    def exec(self) -> Any:
        return FakeMessageBox.answer

    def checkBox(self) -> Any:
        return SimpleNamespace(isChecked=lambda: FakeMessageBox.ignore_ticked)


def _patch_message_box(
    monkeypatch: pytest.MonkeyPatch, answer: str, ignore: bool
) -> None:
    from aqt import qt

    FakeMessageBox.StandardButton = qt.QMessageBox.StandardButton  # type: ignore[assignment]
    FakeMessageBox.Icon = qt.QMessageBox.Icon  # type: ignore[assignment]
    FakeMessageBox.answer = getattr(qt.QMessageBox.StandardButton, answer)
    FakeMessageBox.ignore_ticked = ignore
    FakeMessageBox.shown = []
    monkeypatch.setattr(qt, "QMessageBox", FakeMessageBox)
    monkeypatch.setattr(qt, "QCheckBox", lambda **kwargs: None)


def test_request_permission_granted_for_allowed_origins(server: Server) -> None:
    status, headers, body = server.request(
        {"action": "requestPermission", "version": 6},
        headers={"Origin": "http://localhost"},
    )
    assert json.loads(body)["result"] == {
        "permission": "granted",
        "requireApikey": False,
        "version": 6,
    }


def test_request_permission_asks_and_yes_allows_the_origin(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_message_box(monkeypatch, "Yes", ignore=False)
    status, headers, body = server.request(
        {"action": "requestPermission", "version": 6},
        headers={"Origin": "https://site.example"},
    )
    assert status == 200
    # the reply is readable by the asking page
    assert headers["Access-Control-Allow-Origin"] == "https://site.example"
    assert json.loads(body)["result"]["permission"] == "granted"
    assert "https://site.example" in FakeMessageBox.shown[0]
    assert "https://site.example" in server.service.settings.cors_origins
    assert server.mw.pm.meta["ankiConnect"]["webCorsOriginList"][-1] == (
        "https://site.example"
    )
    status, _, _ = server.request(
        {"action": "version"}, headers={"Origin": "https://site.example"}
    )
    assert status == 200


def test_request_permission_no_with_ignore_denies_from_then_on(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_message_box(monkeypatch, "No", ignore=True)
    request = {"action": "requestPermission", "version": 6}
    headers = {"Origin": "https://nag.example"}
    assert json.loads(server.request(request, headers=headers)[2])["result"] == {
        "permission": "denied"
    }
    assert server.service.settings.ignore_origins == ("https://nag.example",)
    FakeMessageBox.shown = []
    assert json.loads(server.request(request, headers=headers)[2])["result"] == {
        "permission": "denied"
    }
    assert FakeMessageBox.shown == []


def test_multi_runs_each_action(server: Server) -> None:
    result = server.invoke(
        "multi",
        actions=[
            {"action": "deckNames"},
            {"action": "version", "version": 6},
            {"action": "nope", "version": 6},
        ],
    )
    # an action without a version is version 4: its bare result
    assert result == [
        ["Default"],
        {"result": 6, "error": None},
        {"result": None, "error": "unsupported action"},
    ]


def test_requests_run_one_at_a_time_off_the_main_thread(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = 0
    overlaps = []
    threads = []
    users = []
    real = AnkiConnect.deckNames

    def slow(self: AnkiConnect) -> Any:
        nonlocal running
        running += 1
        overlaps.append(running)
        threads.append(threading.current_thread())
        users.append(server.mw.taskman.users)
        time.sleep(0.05)
        running -= 1
        return real(self)

    monkeypatch.setattr(AnkiConnect, "deckNames", ankiconnect.api()(slow))
    workers = [
        threading.Thread(target=lambda: server.invoke("deckNames")) for _ in range(5)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert overlaps == [1] * 5
    assert all(t is not threading.main_thread() for t in threads)
    # each counts as collection use, so the periodic backup waits
    assert users == [1] * 5
    assert server.mw.taskman.users == 0
    # a collection action does not go to the main thread
    assert server.mw.taskman.on_main == 0


def test_gui_actions_go_to_the_main_thread(server: Server) -> None:
    server.invoke("guiDeckBrowser")
    assert server.mw.taskman.on_main == 1
    server.mw.moveToState.assert_called_once_with("deckBrowser")


def test_large_body_is_read_whole(server: Server) -> None:
    data = os.urandom(3 * 1024 * 1024)
    name = server.invoke(
        "storeMediaFile", filename="big.bin", data=base64.b64encode(data).decode()
    )
    assert name == "big.bin"
    assert base64.b64decode(server.invoke("retrieveMediaFile", filename="big.bin")) == (
        data
    )


# Miscellaneous actions
######################################################################


def test_version_action(server: Server) -> None:
    assert server.invoke("version") == 6


def test_profiles(server: Server) -> None:
    assert server.invoke("getProfiles") == ["User 1", "Other"]
    assert server.invoke("getActiveProfile") == "User 1"
    assert server.invoke("loadProfile", name="Nobody") is False
    assert server.invoke("loadProfile", name="Other") is True
    server.mw.pm.load.assert_called_once_with("Other")
    server.mw.loadProfile.assert_called_once()


def test_sync_needs_auth(server: Server) -> None:
    assert server.error("sync") == "sync: auth not configured"


def test_sync_then_the_sync_button(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = SimpleNamespace(
        NO_CHANGES=0,
        NORMAL_SYNC=1,
        FULL_SYNC=2,
        required=0,
        host_number=3,
        new_endpoint="",
        remote_collection_changed=False,
    )
    server.mw.pm.sync_auth = lambda: "auth"
    monkeypatch.setattr(server.col, "sync_collection", lambda auth, media: out)
    assert server.invoke("sync") is None
    server.mw.pm.set_host_number.assert_called_once_with(3)
    server.mw.on_sync_button_clicked.assert_called_once()


def test_sync_that_brought_reviews_refreshes_rwkv_first(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aqt import rwkv_scheduler

    out = SimpleNamespace(
        NO_CHANGES=0,
        NORMAL_SYNC=1,
        required=0,
        host_number=1,
        new_endpoint="",
        remote_collection_changed=True,
        remote_non_review_collection_changed=False,
        remote_review_ids=[5, 6],
    )
    server.mw.pm.sync_auth = lambda: "auth"
    monkeypatch.setattr(server.col, "sync_collection", lambda auth, media: out)
    calls = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "refresh_rwkv_state_after_sync",
        lambda mw, on_done, remote_review_ids: (
            calls.append(remote_review_ids) or on_done()
        ),
    )
    server.invoke("sync")
    assert calls == [(5, 6)]
    server.mw.on_sync_button_clicked.assert_called_once()


def test_sync_fails_when_a_full_sync_is_needed(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = SimpleNamespace(NO_CHANGES=0, NORMAL_SYNC=1, required=2)
    server.mw.pm.sync_auth = lambda: "auth"
    monkeypatch.setattr(server.col, "sync_collection", lambda auth, media: out)
    assert server.error("sync").startswith("Sync status 2 not one of [0, 1]")
    server.mw.on_sync_button_clicked.assert_not_called()


def test_review_counts_and_stats(server: Server) -> None:
    nid = server.add_basic()
    (cid,) = server.cards_of(nid)
    assert server.invoke("getNumCardsReviewedToday") == 0
    assert server.invoke("answerCards", answers=[{"cardId": cid, "ease": 3}]) == [True]
    assert server.invoke("getNumCardsReviewedToday") == 1
    by_day = server.invoke("getNumCardsReviewedByDay")
    assert len(by_day) == 1 and by_day[0][1] == 1
    assert "<" in server.invoke("getCollectionStatsHTML", wholeCollection=True)


def test_api_reflect(server: Server) -> None:
    result = server.invoke("apiReflect", scopes=["actions"])
    assert result["scopes"] == ["actions"]
    assert result["actions"] == action_names()
    assert server.invoke(
        "apiReflect", scopes=["actions"], actions=["version", "window", "nope"]
    ) == {"scopes": ["actions"], "actions": ["version"]}
    assert server.error("apiReflect", scopes="actions") == "scopes has invalid value"


def test_reload_collection(server: Server) -> None:
    assert server.invoke("reloadCollection") is None
    server.mw.col = None
    assert server.error("reloadCollection") == "collection is not available"
    server.mw.col = server.col


# Decks
######################################################################


def test_decks(server: Server) -> None:
    assert server.invoke("deckNames") == ["Default"]
    did = server.invoke("createDeck", deck="Japanese::Words")
    names = server.invoke("deckNamesAndIds")
    assert names["Japanese::Words"] == did and names["Default"] == 1
    assert server.invoke("deckNameFromId", deckId=did) == "Japanese::Words"
    # a missing id is the Default deck (decks.get falls back to it), as in
    # the add-on
    assert server.invoke("deckNameFromId", deckId=123456) == "Default"

    nid = server.add_basic()
    (cid,) = server.cards_of(nid)
    assert server.invoke("getDecks", cards=[cid]) == {"Default": [cid]}
    assert server.invoke("changeDeck", cards=[cid], deck="Japanese::Words") is None
    assert server.invoke("getDecks", cards=[cid]) == {"Japanese::Words": [cid]}

    stats = server.invoke("getDeckStats", decks=["Japanese::Words"])
    assert stats[str(did)]["new_count"] == 1
    assert stats[str(did)]["total_in_deck"] == 1

    assert server.error("deleteDecks", decks=["Japanese::Words"]) == (
        "Since Anki 2.1.28 it's not possible to delete decks without deleting cards as well"
    )
    server.invoke("deleteDecks", decks=["Japanese::Words", "Missing"], cardsToo=True)
    assert "Japanese::Words" not in server.invoke("deckNames")
    assert server.invoke("findCards", query=f"cid:{cid}") == []


def test_deck_config(server: Server) -> None:
    assert server.invoke("getDeckConfig", deck="Missing") is False
    config = server.invoke("getDeckConfig", deck="Default")
    assert config["id"] == 1
    config["new"]["perDay"] = 33
    assert server.invoke("saveDeckConfig", config=config) is True
    assert server.invoke("getDeckConfig", deck="Default")["new"]["perDay"] == 33
    assert server.invoke("saveDeckConfig", config={**config, "id": 999}) is False

    clone = server.invoke("cloneDeckConfigId", name="Copy", cloneFrom=1)
    assert isinstance(clone, int)
    assert server.invoke("cloneDeckConfigId", name="X", cloneFrom=999) is False
    assert server.invoke("setDeckConfigId", decks=["Default"], configId=clone) is True
    assert server.invoke("getDeckConfig", deck="Default")["id"] == clone
    assert server.invoke("setDeckConfigId", decks=["Missing"], configId=clone) is False
    server.invoke("setDeckConfigId", decks=["Default"], configId=1)
    assert server.invoke("removeDeckConfigId", configId=clone) is True
    assert server.invoke("removeDeckConfigId", configId=clone) is False


# Media
######################################################################


def test_media(server: Server, tmp_path: Path) -> None:
    data = base64.b64encode(b"hello").decode()
    assert server.invoke("storeMediaFile", filename="_hello.txt", data=data) == (
        "_hello.txt"
    )
    assert server.invoke("retrieveMediaFile", filename="_hello.txt") == data
    assert "_hello.txt" in server.invoke("getMediaFilesNames", pattern="_hell*")
    assert server.invoke("getMediaDirPath") == os.path.abspath(server.col.media.dir())
    md5 = hashlib.md5(b"hello").hexdigest()
    assert (
        server.invoke("storeMediaFile", filename="_hello.txt", data=data, skipHash=md5)
        is None
    )
    path = tmp_path / "from_disk.txt"
    path.write_bytes(b"disk")
    assert server.invoke("storeMediaFile", filename="_disk.txt", path=str(path)) == (
        "_disk.txt"
    )
    assert server.error("storeMediaFile", filename="x.txt") == (
        'You must provide a "data", "path", or "url" field.'
    )
    server.invoke("deleteMediaFile", filename="_hello.txt")
    assert server.invoke("retrieveMediaFile", filename="_hello.txt") is False


def test_media_from_url(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    urls = []
    monkeypatch.setattr(
        AnkiConnect, "_download", lambda self, url: urls.append(url) or b"web"
    )
    assert server.invoke(
        "storeMediaFile", filename="_web.txt", url="https://example.com/a"
    ) == ("_web.txt")
    assert urls == ["https://example.com/a"]


# Notes
######################################################################


def test_add_note_and_notes_info(server: Server) -> None:
    nid = server.add_basic("犬", "dog", tags=["jp", "animal"])
    info = server.invoke("notesInfo", notes=[nid, 123])
    assert info[1] == {}
    note = info[0]
    assert note["noteId"] == nid and note["modelName"] == "Basic"
    assert note["profile"] == "User 1"
    assert sorted(note["tags"]) == ["animal", "jp"]
    assert note["fields"] == {
        "Front": {"value": "犬", "order": 0},
        "Back": {"value": "dog", "order": 1},
    }
    assert note["cards"] == server.cards_of(nid)
    assert server.invoke("notesInfo", query="dog") == [note]
    assert server.error("notesInfo") == 'Must provide either "notes" or a "query"'
    mod = server.invoke("notesModTime", notes=[nid, 5])
    assert mod == [{"noteId": nid, "mod": note["mod"]}, {}]


def test_add_note_field_names_ignore_case(server: Server) -> None:
    nid = server.invoke(
        "addNote",
        note={
            "deckName": "Default",
            "modelName": "Basic",
            "fields": {"front": "a", "BACK": "b"},
        },
    )
    fields = server.invoke("notesInfo", notes=[nid])[0]["fields"]
    assert fields["Front"]["value"] == "a" and fields["Back"]["value"] == "b"


def test_add_note_errors_and_duplicates(server: Server) -> None:
    server.add_basic("same")
    note = {
        "deckName": "Default",
        "modelName": "Basic",
        "fields": {"Front": "same", "Back": ""},
    }
    assert server.error("addNote", note=note) == (
        "cannot create note because it is a duplicate"
    )
    assert server.invoke("canAddNote", note=note) is False
    assert server.invoke("canAddNoteWithErrorDetail", note=note) == {
        "canAdd": False,
        "error": "cannot create note because it is a duplicate",
    }
    assert server.invoke(
        "canAddNotes", notes=[note, {**note, "fields": {"Front": "new"}}]
    ) == [
        False,
        True,
    ]
    assert server.invoke("canAddNotesWithErrorDetail", notes=[note]) == [
        {"canAdd": False, "error": "cannot create note because it is a duplicate"}
    ]
    allowed = {**note, "options": {"allowDuplicate": True}}
    assert isinstance(server.invoke("addNote", note=allowed), int)
    assert (
        server.error("addNote", note={**note, "options": {"allowDuplicate": "yes"}})
        == 'option parameter "allowDuplicate" must be boolean'
    )
    empty = {**note, "fields": {"Front": "", "Back": "x"}}
    assert (
        server.error("addNote", note=empty) == "cannot create note because it is empty"
    )
    assert server.error("addNote", note={**note, "modelName": "Nope"}) == (
        "model was not found: Nope"
    )
    assert server.error("addNote", note={**note, "deckName": "Nope"}) == (
        "deck was not found: Nope"
    )
    # the deck scope: the same text in another deck is not a duplicate
    server.invoke("createDeck", deck="Other")
    scoped = {
        **note,
        "deckName": "Other",
        "options": {"duplicateScope": "deck"},
    }
    assert server.invoke("canAddNote", note=scoped) is True
    scoped["options"]["duplicateScopeOptions"] = {"deckName": "Default"}
    assert server.invoke("canAddNote", note=scoped) is False


def test_add_note_with_media(server: Server) -> None:
    data = base64.b64encode(b"img").decode()
    nid = server.add_basic(
        picture=[{"filename": "_p.png", "data": data, "fields": ["Back"]}],
        audio={"filename": "_a.mp3", "data": data, "fields": ["Front"]},
    )
    fields = server.invoke("notesInfo", notes=[nid])[0]["fields"]
    assert fields["Back"]["value"] == 'back<img src="_p.png">'
    assert fields["Front"]["value"] == "front[sound:_a.mp3]"
    # the fork: media without fields is stored and added to no field
    nid = server.add_basic("other", video={"filename": "_v.mp4", "data": data})
    assert server.invoke("notesInfo", notes=[nid])[0]["fields"]["Front"]["value"] == (
        "other"
    )
    assert server.invoke("retrieveMediaFile", filename="_v.mp4") == data


def test_add_notes_all_or_nothing(server: Server) -> None:
    good = {"deckName": "Default", "modelName": "Basic", "fields": {"Front": "g1"}}
    ids = server.invoke("addNotes", notes=[good, {**good, "fields": {"Front": "g2"}}])
    assert len(ids) == 2
    error = server.error("addNotes", notes=[{**good, "fields": {"Front": "g3"}}, good])
    assert error == "['cannot create note because it is a duplicate']"
    assert server.invoke("findNotes", query="g3") == []


def test_update_notes(server: Server) -> None:
    nid = server.add_basic("q", "a")
    server.invoke("updateNoteFields", note={"id": nid, "fields": {"Back": "new"}})
    assert (
        server.invoke("notesInfo", notes=[nid])[0]["fields"]["Back"]["value"] == "new"
    )
    server.invoke(
        "updateNote", note={"id": nid, "fields": {"Front": "q2"}, "tags": ["t"]}
    )
    info = server.invoke("notesInfo", notes=[nid])[0]
    assert info["fields"]["Front"]["value"] == "q2" and info["tags"] == ["t"]
    assert server.error("updateNote", note={"id": nid}) == (
        'Must provide a "fields" or "tags" property.'
    )
    assert server.error("updateNoteFields", note={"id": 42, "fields": {}}) == (
        "Note was not found: 42"
    )
    server.invoke(
        "updateNoteModel",
        note={
            "id": nid,
            "modelName": "Basic (and reversed card)",
            "fields": {"front": "F", "Back": "B"},
            "tags": ["moved"],
        },
    )
    info = server.invoke("notesInfo", notes=[nid])[0]
    assert info["modelName"] == "Basic (and reversed card)"
    assert info["tags"] == ["moved"]
    assert server.error("updateNoteModel", note={"id": nid}) == "Model name is required"


def test_tags(server: Server) -> None:
    nid = server.add_basic()
    server.invoke("addTags", notes=[nid], tags="one two")
    assert server.invoke("getNoteTags", note=nid) == ["one", "two"]
    server.invoke("removeTags", notes=[nid], tags="one")
    assert server.invoke("getNoteTags", note=nid) == ["two"]
    server.invoke("updateNoteTags", note=nid, tags=["a", "b"])
    assert server.invoke("getNoteTags", note=nid) == ["a", "b"]
    assert server.error("updateNoteTags", note=nid, tags=[1]) == (
        "Must provide tags as a list of strings"
    )
    server.invoke(
        "replaceTags", notes=[nid, 999], tag_to_replace="a", replace_with_tag="c"
    )
    assert sorted(server.invoke("getNoteTags", note=nid)) == ["b", "c"]
    server.invoke("replaceTagsInAllNotes", tag_to_replace="b", replace_with_tag="d")
    assert sorted(server.invoke("getNoteTags", note=nid)) == ["c", "d"]
    assert set(server.invoke("getTags")) >= {"c", "d"}
    server.invoke("clearUnusedTags")
    assert set(server.invoke("getTags")) == {"c", "d"}


def test_delete_notes_and_cards_to_notes(server: Server) -> None:
    nid = server.add_basic()
    cids = server.cards_of(nid)
    assert server.invoke("cardsToNotes", cards=cids) == [nid]
    server.invoke("deleteNotes", notes=[nid])
    assert server.invoke("findNotes", query=f"nid:{nid}") == []


def test_remove_empty_notes_removes_unused_note_types(server: Server) -> None:
    server.add_basic()
    assert "Cloze" in server.invoke("modelNames")
    server.invoke("removeEmptyNotes")
    names = server.invoke("modelNames")
    assert "Basic" in names and "Cloze" not in names


# Cards
######################################################################


def test_find_notes_and_cards(server: Server) -> None:
    nid = server.add_basic("apple")
    cids = server.cards_of(nid)
    assert server.invoke("findNotes", query="apple") == [nid]
    assert server.invoke("findNotes") == []
    assert server.invoke("findCards", query="apple") == cids
    assert server.invoke("findCards") == []
    details = server.invoke(
        "findCards",
        query="apple",
        fields=["due", "type", "queue", "interval", "reps"],
        noteFields=["Front"],
    )
    assert details == [
        {
            "cardId": cids[0],
            "fields": {"Front": {"value": "apple", "order": 0}},
            "due": server.invoke("cardsInfo", cards=cids)[0]["due"],
            "type": 0,
            "queue": 0,
            "interval": 0,
            "reps": 0,
        }
    ]
    assert server.error("findCards", query="apple", fields=["ease"]) == (
        "unsupported field requested: ease"
    )


def test_rwkv_retrievability_searches_are_prepared_as_in_the_browser(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aqt import rwkv_scheduler

    prepared = []
    monkeypatch.setattr(
        rwkv_scheduler,
        "prepare_browser_retrievability_scores",
        lambda mw, search: prepared.append((mw, search)),
    )
    server.invoke("findCards", query="deck:Default")
    server.invoke("findNotes", query="deck:Default")
    assert prepared == []
    assert server.invoke("findCards", query="prop:rwkv:r<0.9") == []
    assert server.invoke("findNotes", query="prop:rwkv-curve:r<0.9") == []
    assert prepared == [
        (server.mw, "prop:rwkv:r<0.9"),
        (server.mw, "prop:rwkv-curve:r<0.9"),
    ]


def test_cards_info_all_mode_keeps_the_addons_keys(server: Server) -> None:
    nid = server.add_basic("q", "a")
    (cid,) = server.cards_of(nid)
    info, missing = server.invoke("cardsInfo", cards=[cid, 1])
    assert missing == {}
    assert list(info) == [
        "cardId",
        "fields",
        "fieldOrder",
        "question",
        "answer",
        "modelName",
        "ord",
        "deckName",
        "css",
        "factor",
        "interval",
        "note",
        "type",
        "queue",
        "due",
        "reps",
        "lapses",
        "left",
        "mod",
        "nextReviews",
        "flags",
    ]
    assert info["cardId"] == cid and info["note"] == nid
    assert info["deckName"] == "Default" and info["modelName"] == "Basic"
    assert "q" in info["question"] and "a" in info["answer"]
    assert len(info["nextReviews"]) == 4 and all(info["nextReviews"])
    assert server.invoke("cardsModTime", cards=[cid, 1]) == [
        {"cardId": cid, "mod": info["mod"]},
        {},
    ]


def test_cards_info_modes_and_note_fields(server: Server) -> None:
    nid = server.add_basic("q", "a")
    (cid,) = server.cards_of(nid)
    (compact,) = server.invoke("cardsInfo", cards=[cid], retrieved_info_mode="compact")
    assert "question" not in compact and "nextReviews" not in compact
    assert compact["interval"] == 0 and compact["deckName"] == "Default"
    (only,) = server.invoke(
        "cardsInfo", cards=[cid], retrieved_info_mode="FIELDS_ONLY", noteFields=["Back"]
    )
    assert only == {"cardId": cid, "fields": {"Back": {"value": "a", "order": 1}}}
    assert server.error("cardsInfo", cards=[cid], retrieved_info_mode="x") == (
        "invalid retrieved_info_mode: X"
    )
    assert server.error("cardsInfo", cards=[cid], fields=["due"]) == (
        "unsupported field requested: due"
    )


def test_cards_details(server: Server) -> None:
    nid = server.add_basic("q", "a")
    (cid,) = server.cards_of(nid)
    assert server.invoke("cardsDetails", cards=[cid]) == [cid]
    assert server.invoke("cardsDetails", cards=None) == []
    assert server.invoke("cardsDetails", cards=[cid], fields=["reps"]) == [
        {"cardId": cid, "reps": 0}
    ]
    assert server.error("cardsDetails", cards=cid, fields=["reps"]) == (
        f"cards should be a list: {cid}"
    )


def test_suspend(server: Server) -> None:
    cids = server.cards_of(server.add_basic("a")) + server.cards_of(
        server.add_basic("b")
    )
    assert server.invoke("suspend", cards=cids) is True
    assert server.invoke("areSuspended", cards=cids + [1]) == [True, True, None]
    assert server.invoke("suspended", card=cids[0]) is True
    # every card already suspended: the add-on's loop skips the second one,
    # which is then suspended again
    assert server.invoke("suspend", cards=cids) is True
    assert server.invoke("unsuspend", cards=cids) is None
    assert server.invoke("areSuspended", cards=cids) == [False, False]


def test_ease_factors_and_card_values(server: Server) -> None:
    (cid,) = server.cards_of(server.add_basic())
    assert server.invoke(
        "setEaseFactors", cards=[cid, 1], easeFactors=[3100, 2000]
    ) == [
        True,
        False,
    ]
    assert server.invoke("getEaseFactors", cards=[cid, 1]) == [3100, None]
    assert (
        server.invoke("setSpecificValueOfCard", card=cid, keys=["ivl"], newValues=[5])
        is False
    )
    assert server.invoke(
        "setSpecificValueOfCard",
        card=cid,
        keys=["flags"],
        newValues=[2],
    ) == [True]
    assert server.invoke("cardsInfo", cards=[cid])[0]["flags"] == 2
    assert server.invoke(
        "setSpecificValueOfCard",
        card=cid,
        keys=["ivl"],
        newValues=[5],
        warning_check=True,
    ) == [True]
    assert (
        server.invoke("setSpecificValueOfCard", card=[cid], keys=[], newValues=[])
        is False
    )


def test_answering_and_reviews(server: Server) -> None:
    (cid,) = server.cards_of(server.add_basic())
    assert server.invoke("areDue", cards=[cid]) == [True]
    assert server.invoke("getIntervals", cards=[cid]) == [0]
    assert server.invoke(
        "answerCards", answers=[{"cardId": cid, "ease": 3}, {"cardId": 1, "ease": 3}]
    ) == [True, False]
    reviews = server.invoke("getReviewsOfCards", cards=[cid])
    (review,) = reviews[str(cid)]
    assert set(review) == {
        "id",
        "usn",
        "ease",
        "ivl",
        "lastIvl",
        "factor",
        "time",
        "type",
    }
    assert review["ease"] == 3
    assert server.invoke("getIntervals", cards=[cid]) == [review["ivl"]]
    assert server.invoke("getIntervals", cards=[cid], complete=True) == [
        [review["ivl"]]
    ]
    assert server.invoke("getLatestReviewID", deck="Default") == review["id"]
    rows = server.invoke("cardReviews", deck="Default", startID=0)
    assert rows[0][0] == review["id"] and rows[0][1] == cid
    assert server.invoke("areDue", cards=[cid]) in ([True], [False])
    assert server.invoke("gradeNow", cards=[cid], ease=4) is True
    assert len(server.invoke("getReviewsOfCards", cards=[cid])[str(cid)]) == 2
    assert (
        server.error("gradeNow", cards=[cid], ease=5) == "ease must be between 1 and 4"
    )


def test_insert_reviews(server: Server) -> None:
    (cid,) = server.cards_of(server.add_basic())
    server.invoke("insertReviews", reviews=[[1000, cid, -1, 3, 4, 1, 2500, 6000, 1]])
    assert server.invoke("getReviewsOfCards", cards=[cid])[str(cid)] == [
        {
            "id": 1000,
            "usn": -1,
            "ease": 3,
            "ivl": 4,
            "lastIvl": 1,
            "factor": 2500,
            "time": 6000,
            "type": 1,
        }
    ]


def test_forget_relearn_and_due_dates(server: Server) -> None:
    (cid,) = server.cards_of(server.add_basic())
    assert server.invoke("setDueDate", cards=[cid], days="3") is True
    card = server.invoke("cardsInfo", cards=[cid])[0]
    assert card["type"] == 2 and card["queue"] == 2
    server.invoke("relearnCards", cards=[cid])
    card = server.invoke("cardsInfo", cards=[cid])[0]
    assert card["type"] == 3 and card["queue"] == 1
    server.invoke("forgetCards", cards=[cid])
    assert server.invoke("cardsInfo", cards=[cid])[0]["type"] == 0


def test_reposition_new_cards(server: Server) -> None:
    first = server.cards_of(server.add_basic("1"))[0]
    second = server.cards_of(server.add_basic("2"))[0]
    reviewed = server.cards_of(server.add_basic("3"))[0]
    server.invoke("setDueDate", cards=[reviewed], days="1")
    result = server.invoke(
        "repositionNewCards",
        orderedCardIds=[second, first, second, reviewed, 7],
        startPosition=10,
        step=5,
        shift=False,
    )
    assert result == {
        "requested": 5,
        "deduped": 4,
        "eligibleNew": 2,
        "repositioned": 2,
        "skippedNotFound": [7],
        "skippedNotNew": [reviewed],
        "appliedStartPosition": 10,
        "appliedStep": 5,
        "appliedShift": False,
    }
    dues = {
        c["cardId"]: c["due"] for c in server.invoke("cardsInfo", cards=[first, second])
    }
    assert dues == {second: 10, first: 15}
    assert (
        server.error(
            "repositionNewCards",
            orderedCardIds=[],
            startPosition=1,
            step=1,
            shift=False,
        )
        == "orderedCardIds should be a non-empty list: []"
    )


# Note types
######################################################################


def test_models(server: Server) -> None:
    created = server.invoke(
        "createModel",
        modelName="Vocab",
        inOrderFields=["Word", "Meaning"],
        cardTemplates=[
            {"Name": "Recall", "Front": "{{Word}}", "Back": "{{FrontSide}}{{Meaning}}"}
        ],
        css=".card {}",
    )
    assert created["name"] == "Vocab"
    assert (
        server.error(
            "createModel", modelName="Vocab", inOrderFields=["a"], cardTemplates=[{}]
        )
        == "Model name already exists"
    )
    assert "Vocab" in server.invoke("modelNames")
    mid = server.invoke("modelNamesAndIds")["Vocab"]
    assert server.invoke("findModelsById", modelIds=[mid])[0]["name"] == "Vocab"
    assert server.invoke("findModelsByName", modelNames=["Vocab"])[0]["id"] == mid
    assert (
        server.error("findModelsByName", modelNames=["No"]) == "model was not found: No"
    )
    assert server.invoke("modelNameFromId", modelId=mid) == "Vocab"
    assert server.invoke("modelFieldNames", modelName="Vocab") == ["Word", "Meaning"]
    assert server.invoke("modelFieldDescriptions", modelName="Vocab") == ["", ""]
    fonts = server.invoke("modelFieldFonts", modelName="Vocab")
    assert set(fonts) == {"Word", "Meaning"} and "font" in fonts["Word"]
    assert server.invoke("modelFieldsOnTemplates", modelName="Vocab") == {
        "Recall": [["Word"], ["Meaning"]]
    }
    assert server.invoke("modelTemplates", modelName="Vocab") == {
        "Recall": {"Front": "{{Word}}", "Back": "{{FrontSide}}{{Meaning}}"}
    }
    assert server.invoke("modelStyling", modelName="Vocab") == {"css": ".card {}"}

    server.invoke(
        "updateModelTemplates",
        model={"name": "Vocab", "templates": {"Recall": {"Front": "<b>{{Word}}</b>"}}},
    )
    assert server.invoke("modelTemplates", modelName="Vocab")["Recall"]["Front"] == (
        "<b>{{Word}}</b>"
    )
    server.invoke(
        "updateModelStyling", model={"name": "Vocab", "css": ".card {color: red}"}
    )
    assert (
        server.invoke(
            "findAndReplaceInModels",
            modelName="Vocab",
            findText="red",
            replaceText="blue",
        )
        == 1
    )
    assert server.invoke("modelStyling", modelName="Vocab") == {
        "css": ".card {color: blue}"
    }

    server.invoke(
        "modelTemplateAdd",
        modelName="Vocab",
        template={"Name": "Reverse", "Front": "{{Meaning}}", "Back": "{{Word}}"},
    )
    server.invoke(
        "modelTemplateRename",
        modelName="Vocab",
        oldTemplateName="Reverse",
        newTemplateName="Rev",
    )
    server.invoke(
        "modelTemplateReposition", modelName="Vocab", templateName="Rev", index=0
    )
    assert list(server.invoke("modelTemplates", modelName="Vocab")) == ["Rev", "Recall"]
    server.invoke("modelTemplateRemove", modelName="Vocab", templateName="Rev")
    assert list(server.invoke("modelTemplates", modelName="Vocab")) == ["Recall"]

    server.invoke("modelFieldAdd", modelName="Vocab", fieldName="Notes", index=0)
    server.invoke(
        "modelFieldRename",
        modelName="Vocab",
        oldFieldName="Notes",
        newFieldName="Extra",
    )
    server.invoke("modelFieldReposition", modelName="Vocab", fieldName="Extra", index=2)
    assert server.invoke("modelFieldNames", modelName="Vocab") == [
        "Word",
        "Meaning",
        "Extra",
    ]
    server.invoke(
        "modelFieldSetFont", modelName="Vocab", fieldName="Word", font="Meiryo"
    )
    server.invoke(
        "modelFieldSetFontSize", modelName="Vocab", fieldName="Word", fontSize=30
    )
    assert server.invoke("modelFieldFonts", modelName="Vocab")["Word"] == {
        "font": "Meiryo",
        "size": 30,
    }
    assert (
        server.error(
            "modelFieldSetFontSize", modelName="Vocab", fieldName="Word", fontSize="big"
        )
        == "fontSize should be an integer: big"
    )
    assert (
        server.invoke(
            "modelFieldSetDescription",
            modelName="Vocab",
            fieldName="Word",
            description="the word",
        )
        is True
    )
    assert server.invoke("modelFieldDescriptions", modelName="Vocab")[0] == "the word"
    server.invoke("modelFieldRemove", modelName="Vocab", fieldName="Extra")
    assert server.invoke("modelFieldNames", modelName="Vocab") == ["Word", "Meaning"]
    assert server.error("modelFieldRemove", modelName="Vocab", fieldName="Nope") == (
        "field was not found in Vocab: Nope"
    )


# Packages
######################################################################


def test_export_and_import_package(server: Server, tmp_path: Path) -> None:
    server.add_basic("exported")
    path = str(tmp_path / "deck.apkg")
    assert server.invoke("exportPackage", deck="Default", path=path) is True
    assert os.path.exists(path)
    assert server.invoke("exportPackage", deck="Missing", path=path) is False
    server.invoke("deleteNotes", notes=server.invoke("findNotes", query="exported"))
    assert server.invoke("importPackage", path=path) is True
    assert len(server.invoke("findNotes", query="exported")) == 1


# GUI actions
######################################################################


def test_gui_browse(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    nid = server.add_basic("browse me")
    browser = MagicMock()
    browser.table._model.active_column_index.return_value = 2
    opened = []
    monkeypatch.setattr(
        aqt.dialogs,
        "open",
        lambda name, *args, **kwargs: opened.append(name) or browser,
    )
    result = server.invoke(
        "guiBrowse",
        query="browse",
        reorderCards={"order": "descending", "columnId": "noteCrt"},
    )
    assert result == server.cards_of(nid)
    assert opened == ["Browser"]
    browser.form.searchEdit.lineEdit.return_value.setText.assert_called_once_with(
        "browse"
    )
    browser.table._on_sort_column_changed.assert_called_once()
    assert (
        server.error(
            "guiBrowse", query="x", reorderCards={"order": "sideways", "columnId": "a"}
        )
        == "invalid card order: sideways"
    )
    browser.table._model.active_column_index.return_value = None
    assert (
        server.error(
            "guiBrowse", reorderCards={"order": "ascending", "columnId": "zzz"}
        )
        == "invalid columnId: zzz"
    )


def test_gui_edit_note(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    from aqt import ankiconnect_edit

    opened = []
    monkeypatch.setattr(
        ankiconnect_edit.Edit,
        "open_dialog_and_show_note_with_id",
        classmethod(lambda cls, nid: opened.append(nid)),
    )
    nid = server.add_basic()
    assert server.invoke("guiEditNote", note=nid) is None
    assert opened == [nid]


def test_gui_select_cards(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    (cid,) = server.cards_of(server.add_basic())
    monkeypatch.setitem(aqt.dialogs._dialogs, "Browser", [None, None])
    assert server.invoke("guiSelectCard", card=cid) is False
    assert server.invoke("guiSelectedNotes") == []
    browser = MagicMock()
    browser.selectedNotes.return_value = [5]
    monkeypatch.setitem(aqt.dialogs._dialogs, "Browser", [None, browser])
    assert server.invoke("guiSelectCard", card=cid) is True
    browser.table.select_single_card.assert_called_once_with(cid)
    assert server.invoke("guiSelectNote", note=cid) is True
    assert server.invoke("guiSelectedNotes") == [5]


def test_gui_add_cards(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    add_cards = MagicMock()
    add_cards.editor.note.id = 0
    monkeypatch.setattr(aqt.dialogs, "open", lambda name, *args: add_cards)
    monkeypatch.setitem(aqt.dialogs._dialogs, "AddCards", [None, None])
    assert server.invoke("guiAddCards") == 0
    assert (
        server.invoke(
            "guiAddCards",
            note={
                "deckName": "Default",
                "modelName": "Basic",
                "fields": {"Front": "f"},
            },
        )
        == 0
    )
    note = add_cards.editor.set_note.call_args.args[0]
    assert note["Front"] == "f"
    assert (
        server.error("guiAddCards", note={"deckName": "Nope", "modelName": "Basic"})
        == "deck was not found: Nope"
    )


def test_gui_add_note_set_data(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(aqt.dialogs._dialogs, "AddCards", [None, None])
    assert server.invoke("guiAddNoteSetData", note={"fields": {"Front": "x"}}) == {
        "error": "Add Note dialog is not open",
        "code": 1,
    }
    editor_note = server.col.new_note(server.col.models.by_name("Basic"))
    editor_note["Front"] = "a"
    editor_note.tags = ["old"]
    add_cards = MagicMock()
    add_cards.editor.note = editor_note
    monkeypatch.setitem(aqt.dialogs._dialogs, "AddCards", [None, add_cards])
    assert (
        server.invoke(
            "guiAddNoteSetData",
            note={"fields": {"Front": "b"}, "tags": ["new"]},
            append=True,
        )
        is True
    )
    assert editor_note["Front"] == "ab"
    assert sorted(editor_note.tags) == ["new", "old"]
    add_cards.editor.loadNote.assert_called_once()
    assert server.error("guiAddNoteSetData", note={"fields": {"Nope": "b"}}) == (
        'Field "Nope" not found in current note'
    )


def _reviewing(server: Server) -> Any:
    (cid,) = server.cards_of(server.add_basic("review q", "review a"))
    card = server.col.get_card(cid)
    reviewer = SimpleNamespace(
        card=card,
        state="answer",
        mw=server.mw,
        _v3=None,
        _answerButtonList=lambda: ((1, "Again"), (2, "Hard"), (3, "Good"), (4, "Easy")),
        _showQuestion=MagicMock(),
        _showAnswer=MagicMock(),
        _answerCard=MagicMock(),
        replayAudio=MagicMock(),
    )
    server.mw.reviewer = reviewer
    server.mw.state = "review"
    return reviewer


def test_gui_review(server: Server) -> None:
    assert server.error("guiReviewActive") == "reviewer is not available"
    reviewer = _reviewing(server)
    assert server.invoke("guiReviewActive") is True
    current = server.invoke("guiCurrentCard")
    assert current["cardId"] == reviewer.card.id
    assert current["buttons"] == [1, 2, 3, 4]
    assert len(current["nextReviews"]) == 4 and all(current["nextReviews"])
    assert current["template"] == "Card 1" and current["deckName"] == "Default"
    assert server.invoke("guiStartCardTimer") is True
    assert server.invoke("guiShowQuestion") is True
    reviewer._showQuestion.assert_called_once()
    assert server.invoke("guiShowAnswer") is True
    reviewer._showAnswer.assert_called_once()
    assert server.invoke("guiAnswerCard", ease=5) is False
    assert server.invoke("guiAnswerCard", ease=3) is True
    reviewer._answerCard.assert_called_once_with(3)
    assert server.invoke("guiPlayAudio") is True
    reviewer.replayAudio.assert_called_once()
    server.mw.state = "overview"
    assert server.invoke("guiReviewActive") is False
    assert server.invoke("guiPlayAudio") is False
    assert server.error("guiCurrentCard") == "Gui review is not currently active."


def test_gui_screens(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    assert server.invoke("guiUndo") is True
    server.mw.undo.assert_called_once()
    assert server.invoke("guiDeckOverview", name="Nope") is False
    assert server.invoke("guiDeckOverview", name="Default") is True
    server.mw.onOverview.assert_called_once()
    assert server.invoke("guiDeckReview", name="Default") is True
    server.mw.moveToState.assert_called_with("review")
    assert server.invoke("guiDeckBrowser") is None
    server.mw.moveToState.assert_called_with("deckBrowser")
    assert server.invoke("guiCheckDatabase") is True
    server.mw.onCheckDB.assert_called_once()


def test_gui_import_and_exit(server: Server, monkeypatch: pytest.MonkeyPatch) -> None:
    from aqt import qt
    from aqt.import_export import importing

    imported = []
    monkeypatch.setattr(
        importing, "import_file", lambda mw, path: imported.append(path)
    )
    monkeypatch.setattr(
        importing, "prompt_for_file_then_import", lambda mw: imported.append(None)
    )
    server.mw.windowFlags = MagicMock(return_value=qt.Qt.WindowType.Window)
    server.mw.setWindowFlags = MagicMock()
    server.mw.show = MagicMock()
    assert server.invoke("guiImportFile", path="C:/deck.apkg") is None
    assert server.invoke("guiImportFile") is None
    assert imported == ["C:/deck.apkg", None]
    shots = []
    monkeypatch.setattr(
        qt, "QTimer", SimpleNamespace(singleShot=lambda ms, fn: shots.append((ms, fn)))
    )
    assert server.invoke("guiExitAnki") is None
    assert shots == [(1000, server.mw.close)]


# One algorithm (ankiconnect.one-algorithm)
######################################################################


def test_card_algorithm_follows_the_card_preset(tmp_path: Path) -> None:
    from aqt import rwkv_scheduler

    service = AnkiConnectService(_make_mw(cast(Any, None)), AnkiConnectSettings())
    actions = AnkiConnect(service)
    card = cast(Any, SimpleNamespace(id=1))
    for curve, instant, expected in (
        (False, False, "fsrs7"),
        (True, False, "rwkvCurve"),
        (False, True, "rwkvInstant"),
    ):
        with (
            patch.object(rwkv_scheduler, "rwkv_review_enabled", return_value=curve),
            patch.object(
                rwkv_scheduler, "answer_intervals_hidden", return_value=instant
            ),
        ):
            assert actions._card_algorithm(card) == expected


def test_prop_values_follow_the_algorithm(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aqt import rwkv_scheduler

    server.col.set_config("fsrs", True)
    (cid,) = server.cards_of(server.add_basic())
    server.invoke("answerCards", answers=[{"cardId": cid, "ease": 3}])
    # The test and cardsInfo each compute R "now", a moment apart. Seconds
    # after an answer R still falls fast (0.99999 to 0.99966 was seen), so
    # the review is moved a year back, where R is flat to 1e-6 over that gap.
    card = server.col.get_card(cid)
    assert card.last_review_time is not None
    card.last_review_time -= 365 * 86_400
    server.col.update_card(card, skip_undo_entry=True)
    stats = server.col.card_stats_data(cid)
    fields = ["prop:r", "prop:s", "prop:d"]

    assert stats.HasField("memory_state")
    (fsrs,) = server.invoke(
        "cardsInfo", cards=[cid], fields=fields, retrieved_info_mode="FIELDS_ONLY"
    )
    assert fsrs["prop:s"] == pytest.approx(stats.memory_state.stability)
    assert fsrs["prop:d"] == pytest.approx(stats.memory_state.difficulty)
    assert fsrs["prop:r"] == (
        pytest.approx(stats.fsrs_retrievability)
        if stats.HasField("fsrs_retrievability")
        else None
    )
    (details,) = server.invoke("cardsDetails", cards=[cid], fields=["prop:d"])
    # cardsDetails' prop:d is the desired retention, as in the fork
    assert details["prop:d"] == (
        pytest.approx(stats.desired_retention)
        if stats.HasField("desired_retention")
        else None
    )

    monkeypatch.setattr(AnkiConnect, "_card_algorithm", lambda self, card: "rwkvCurve")
    curve = rwkv_scheduler.RwkvCardCurve(
        elapsed_days=(), recall=(), s90=12.5, current_recall=0.93
    )
    monkeypatch.setattr(rwkv_scheduler, "rwkv_card_info_curve", lambda *a, **k: curve)
    (rwkv_curve,) = server.invoke(
        "cardsInfo", cards=[cid], fields=fields, retrieved_info_mode="FIELDS_ONLY"
    )
    assert rwkv_curve["prop:s"] == 12.5 and rwkv_curve["prop:r"] == 0.93
    assert rwkv_curve["prop:d"] is None
    monkeypatch.setattr(rwkv_scheduler, "rwkv_card_info_curve", lambda *a, **k: None)
    (no_curve,) = server.invoke(
        "cardsDetails", cards=[cid], fields=["prop:r", "prop:s"]
    )
    assert no_curve["prop:r"] is None and no_curve["prop:s"] is None

    monkeypatch.setattr(
        AnkiConnect, "_card_algorithm", lambda self, card: "rwkvInstant"
    )
    monkeypatch.setattr(
        AnkiConnect, "_rwkv_instant_retrievability", lambda self, card: 0.81
    )
    (instant,) = server.invoke(
        "cardsInfo", cards=[cid], fields=fields, retrieved_info_mode="FIELDS_ONLY"
    )
    assert instant == {
        "cardId": cid,
        "fields": instant["fields"],
        "prop:r": 0.81,
        "prop:s": None,
        "prop:d": None,
    }


@pytest.mark.parametrize("algorithm", ["rwkvCurve", "rwkvInstant"])
def test_next_reviews_are_hidden_for_rwkv(
    server: Server, monkeypatch: pytest.MonkeyPatch, algorithm: str
) -> None:
    from aqt import rwkv_scheduler

    (cid,) = server.cards_of(server.add_basic())
    monkeypatch.setattr(AnkiConnect, "_card_algorithm", lambda self, card: algorithm)
    assert server.invoke("cardsInfo", cards=[cid])[0]["nextReviews"] == ["", "", "", ""]
    reviewer = _reviewing(server)
    monkeypatch.setattr(rwkv_scheduler, "answer_intervals_pending", lambda r, c: True)
    assert server.invoke("guiCurrentCard")["nextReviews"] == ["", "", "", ""]
    if algorithm == "rwkvCurve":
        # once RWKV-Curve has given the intervals: the buttons' labels
        reviewer._v3 = SimpleNamespace(
            states=server.col._backend.get_scheduling_states(reviewer.card.id)
        )
        monkeypatch.setattr(
            rwkv_scheduler, "answer_intervals_pending", lambda r, c: False
        )
        labels = server.col.sched.describe_next_states(reviewer._v3.states)
        assert server.invoke("guiCurrentCard")["nextReviews"] == list(labels)


def test_are_due_is_null_for_rwkv_instant_review_cards(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    (review,) = server.cards_of(server.add_basic("r"))
    (new,) = server.cards_of(server.add_basic("n"))
    server.invoke("setDueDate", cards=[review], days="0")
    assert server.invoke("areDue", cards=[review, new]) == [True, True]
    monkeypatch.setattr(
        AnkiConnect, "_card_algorithm", lambda self, card: "rwkvInstant"
    )
    assert server.invoke("areDue", cards=[review, new]) == [None, True]


# Answering under each algorithm (ankiconnect.one-algorithm)
######################################################################
#
# These tests drive the server over HTTP against a collection of one
# algorithm, with a stand-in RWKV model, so `_card_algorithm` decides as it
# does for a client. `server` (FSRS-7) is not used here.


class _RwkvBackend:
    """Stands in for the RWKV model: the same prediction for every card."""

    def __init__(self, prediction: Any) -> None:
        self.prediction = prediction

    def predict_review(self, *, reviewer: Any, card: Any) -> Any:
        return self.prediction


def _curve_prediction() -> Any:
    """RWKV-Curve for every card: Good in 200 days, S90 210 days."""
    from aqt.rwkv_scheduler import RwkvIntervalOverride, RwkvReviewPrediction

    return RwkvReviewPrediction(
        retrievability=0.83,
        interval_overrides=RwkvIntervalOverride(again=0.2, hard=90, good=200, easy=300),
        s90_overrides=RwkvIntervalOverride(again=1, hard=95, good=210, easy=320),
    )


@pytest.fixture
def algorithm_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Server]]:
    from aqt import rwkv_scheduler

    started: list[tuple[AnkiConnectService, Collection]] = []
    previous = rwkv_scheduler.set_reviewer_backend(None)

    def make(algorithm: str, model: Any = "curve", ready: bool = True) -> Server:
        col = Collection(str(tmp_path / f"{algorithm}-{len(started)}.anki2"))
        # a preset write takes the collection's algorithm
        # (sched.one-global-algorithm)
        col.set_config("schedulingAlgorithm", algorithm)
        col.decks.update_config(col.decks.get_config(DeckConfigId(1)))
        mw = _make_mw(col)
        service = AnkiConnectService(mw, AnkiConnectSettings(enabled=True, bind_port=0))
        monkeypatch.setattr(service, "_schedule_refresh", lambda: None)
        service.start(listen_now=True)
        mw.taskman.on_main = 0
        started.append((service, col))
        backend = _RwkvBackend(_curve_prediction()) if model == "curve" else model
        rwkv_scheduler.set_reviewer_backend(backend)
        if backend is not None and ready:
            rwkv_scheduler._reviewer_backend_warmup_states[(id(backend), id(col))] = (
                None
            )
        return Server(service, col, mw)

    try:
        yield make
    finally:
        rwkv_scheduler.set_reviewer_backend(previous)
        rwkv_scheduler._reviewer_backend_warmup_states.clear()
        for service, col in started:
            service.shutdown()
            col.close()


def _add_review_card(col: Collection) -> int:
    """A review card of 20 days, as test_grade_now.py builds it."""
    note = col.new_note(col.models.by_name("Basic"))
    note.fields[0] = "front"
    col.add_note(note, DeckId(1))
    card = note.cards()[0]
    card.type = 2
    card.queue = 2
    card.ivl = 20
    card.due = col.sched.today
    card.memory_state = FsrsMemoryState(stability=20, difficulty=5)
    card.last_review_time = card.id // 1000 - 20 * 86_400
    col.update_card(card, skip_undo_entry=True)
    return card.id


def _assert_answered_as_the_reviewer(
    col: Collection, card_id: int, algorithm: str, fsrs7_good: int
) -> None:
    card = col.get_card(card_id)
    assert col.db.scalar("select count() from revlog where cid = ?", card_id) == 1
    if algorithm == "rwkvCurve":
        # RWKV-Curve's 200 days with review fuzz, and its S90 of 210, never
        # FSRS-7's interval
        assert 190 <= card.ivl <= 210, (card.ivl, fsrs7_good)
        assert abs(card.ivl - fsrs7_good) > 20, (card.ivl, fsrs7_good)
        assert card.memory_state is not None
        assert card.memory_state.stability == pytest.approx(210)
    else:
        # FSRS-7 and RWKV-Instant store the FSRS states
        # (sched.rwkv-instant-no-intervals)
        assert card.ivl == fsrs7_good


@pytest.mark.parametrize("algorithm", ["fsrs7", "rwkvCurve", "rwkvInstant"])
def test_answer_cards_answers_each_algorithm_as_the_reviewer(
    algorithm_server: Callable[..., Server], algorithm: str
) -> None:
    server = algorithm_server(algorithm)
    card_id = _add_review_card(server.col)
    fsrs7_good = server.col.sched.get_scheduling_states(
        card_id
    ).good.normal.review.scheduled_days

    assert server.invoke("answerCards", answers=[{"cardId": card_id, "ease": 3}]) == [
        True
    ]

    _assert_answered_as_the_reviewer(server.col, card_id, algorithm, fsrs7_good)


@pytest.mark.parametrize("algorithm", ["fsrs7", "rwkvCurve", "rwkvInstant"])
def test_grade_now_answers_each_algorithm_as_the_reviewer(
    algorithm_server: Callable[..., Server], algorithm: str
) -> None:
    server = algorithm_server(algorithm)
    card_id = _add_review_card(server.col)
    fsrs7_good = server.col.sched.get_scheduling_states(
        card_id
    ).good.normal.review.scheduled_days

    assert server.invoke("gradeNow", cards=[card_id], ease=3) is True

    _assert_answered_as_the_reviewer(server.col, card_id, algorithm, fsrs7_good)


def test_answer_cards_gives_false_while_rwkv_curve_has_no_intervals(
    algorithm_server: Callable[..., Server],
) -> None:
    # no RWKV model at all: RWKV-Curve has no intervals for the card
    server = algorithm_server("rwkvCurve", None)
    card_id = _add_review_card(server.col)

    # the missing card keeps its place in the result, as in the add-on
    assert server.invoke(
        "answerCards",
        answers=[{"cardId": 1, "ease": 3}, {"cardId": card_id, "ease": 3}],
    ) == [False, False]

    card = server.col.get_card(card_id)
    assert (card.ivl, card.queue) == (20, 2)
    assert server.invoke("getReviewsOfCards", cards=[card_id])[str(card_id)] == []


def test_answer_cards_keeps_the_per_card_ease_under_rwkv_curve(
    algorithm_server: Callable[..., Server],
) -> None:
    server = algorithm_server("rwkvCurve")
    again = _add_review_card(server.col)
    good = _add_review_card(server.col)

    assert server.invoke(
        "answerCards",
        answers=[{"cardId": again, "ease": 1}, {"cardId": good, "ease": 3}],
    ) == [True, True]

    # RWKV-Curve gives Again 0.2 days and Good 200 days
    assert server.col.get_card(again).ivl < 10
    assert 190 <= server.col.get_card(good).ivl <= 210


def test_grade_now_fails_naming_the_cards_without_rwkv_curve_intervals(
    algorithm_server: Callable[..., Server],
) -> None:
    server = algorithm_server("rwkvCurve", None)
    card_id = _add_review_card(server.col)

    error = server.error("gradeNow", cards=[card_id], ease=3)

    assert "RWKV-Curve has no intervals yet" in error and str(card_id) in error
    card = server.col.get_card(card_id)
    assert (card.ivl, card.queue) == (20, 2)
    assert server.invoke("getReviewsOfCards", cards=[card_id])[str(card_id)] == []


def test_gui_answer_card_is_false_while_rwkv_curve_has_no_intervals(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aqt import rwkv_scheduler

    reviewer = _reviewing(server)
    monkeypatch.setattr(rwkv_scheduler, "answer_intervals_pending", lambda r, c: True)
    assert server.invoke("guiAnswerCard", ease=3) is False
    reviewer._answerCard.assert_not_called()
    monkeypatch.setattr(rwkv_scheduler, "answer_intervals_pending", lambda r, c: False)
    assert server.invoke("guiAnswerCard", ease=3) is True
    reviewer._answerCard.assert_called_once_with(3)


# Refreshing the screens (ankiconnect.actions)
######################################################################


def test_changes_refresh_the_screens_once_per_burst(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aqt import gui_hooks

    fired = []
    monkeypatch.setattr(
        gui_hooks,
        "operation_did_execute",
        lambda changes, handler: fired.append(changes),
    )
    monkeypatch.setattr(gui_hooks, "state_did_reset", lambda: None)
    server.invoke("deckNames")
    assert server.service._pending.names == set()
    server.add_basic("1")
    server.add_basic("2")
    server.invoke("addTags", notes=server.invoke("findNotes", query="1"), tags="t")
    assert {"note", "card", "study_queues", "tag"} <= server.service._pending.names
    server.service.refresh_screens()
    assert len(fired) == 1
    assert fired[0].note and fired[0].card and fired[0].study_queues
    assert not fired[0].notetype
    server.mw.update_undo_actions.assert_called_once()
    server.service.refresh_screens()
    assert len(fired) == 1


def test_a_change_rwkv_cannot_keep_is_refreshed_on_its_own(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aqt import gui_hooks

    fired = []
    monkeypatch.setattr(
        gui_hooks,
        "operation_did_execute",
        lambda changes, handler: fired.append(changes),
    )
    monkeypatch.setattr(gui_hooks, "state_did_reset", lambda: None)
    nid = server.add_basic()
    (cid,) = server.cards_of(nid)
    assert fired == []
    server.invoke("insertReviews", reviews=[[1000, cid, -1, 3, 4, 1, 2500, 6000, 1]])
    # the pending burst first, then the new reviews alone
    assert len(fired) == 2
    assert fired[0].note and fired[1].card and not fired[1].note


def test_the_burst_refresh_waits_for_a_running_request(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aqt import qt

    shots: list[Any] = []
    monkeypatch.setattr(
        qt, "QTimer", SimpleNamespace(singleShot=lambda ms, fn: shots.append(fn))
    )
    refreshed: list[int] = []
    monkeypatch.setattr(server.service, "refresh_screens", lambda: refreshed.append(1))
    with server.service._lock:
        server.service._refresh_from_timer()
    assert refreshed == [] and shots == [server.service._refresh_from_timer]
    server.service._refresh_from_timer()
    assert refreshed == [1]


def test_note_changes_keep_the_rwkv_state(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aqt import rwkv_scheduler

    wrapped = []

    def preserving(col: Any, mutation: Any, **kwargs: Any) -> Any:
        wrapped.append(kwargs)
        return mutation()

    monkeypatch.setattr(
        rwkv_scheduler, "run_collection_mutation_preserving_rwkv_state", preserving
    )
    nid = server.add_basic()
    (cid,) = server.cards_of(nid)
    server.invoke("suspend", cards=[cid])
    server.invoke("updateNoteFields", note={"id": nid, "fields": {"Back": "x"}})
    server.invoke("insertReviews", reviews=[[1000, cid, -1, 3, 4, 1, 2500, 6000, 1]])
    assert wrapped == [
        {"force_reconciliation": True},
        {"force_reconciliation": True, "card_ids": (cid,)},
        {"force_reconciliation": True, "note_ids": (nid,)},
    ]


# Settings (ankiconnect.settings)
######################################################################


def test_settings_defaults_and_storage() -> None:
    settings = AnkiConnectSettings()
    assert settings.enabled is False
    assert (settings.bind_address, settings.bind_port) == ("127.0.0.1", 8765)
    assert settings.cors_origins == ("http://localhost",)
    assert settings.api_key is None
    assert AnkiConnectSettings.from_config(settings.to_config()) == settings
    stored = AnkiConnectSettings.from_config(
        {
            "enabled": True,
            "webBindAddress": "0.0.0.0",
            "webBindPort": 9000,
            "webCorsOriginList": ["*", 3],
            "apiKey": "k",
            "ignoreOriginList": ["x"],
            "apiLogPath": "log.txt",
            "webTimeout": 500,
        }
    )
    assert stored == AnkiConnectSettings(
        enabled=True,
        bind_address="0.0.0.0",
        bind_port=9000,
        cors_origins=("*",),
        api_key="k",
        ignore_origins=("x",),
        api_log_path="log.txt",
        web_timeout_ms=500,
    )
    # invalid values are the defaults
    assert (
        AnkiConnectSettings.from_config(
            {"webBindPort": 70000, "enabled": "yes", "apiKey": 5}
        )
        == AnkiConnectSettings()
    )
    pm = SimpleNamespace(meta={}, profile=None, db=None)
    ankiconnect.save_settings(pm, stored)
    assert ankiconnect.load_settings(pm) == stored


def test_settings_apply_restarts_only_for_a_new_address(server: Server) -> None:
    port = server.port
    server.configure(cors_origins=("*",), api_key="k")
    assert server.service.port == port
    server.configure(enabled=False)
    assert server.service.port == 0
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    assert server.mw.pm.meta["ankiConnect"]["enabled"] is False


def test_settings_from_the_preferences_form() -> None:
    from aqt.ankiconnect_prefs import settings_from_form, status_text

    initial = AnkiConnectSettings(api_key="")
    settings = settings_from_form(
        initial,
        enabled=True,
        address=" ",
        port=9001,
        origins="http://localhost\n\n https://x.org \n",
        api_key="",
    )
    assert settings.enabled and settings.bind_port == 9001
    assert settings.bind_address == "127.0.0.1"
    assert settings.cors_origins == ("http://localhost", "https://x.org")
    # an untouched key field keeps the add-on's empty-string key
    assert settings.api_key == ""
    assert (
        settings_from_form(
            AnkiConnectSettings(api_key="k"),
            enabled=True,
            address="a",
            port=1,
            origins="",
            api_key="",
        ).api_key
        is None
    )
    with patch("aqt.ankiconnect_prefs.tr") as tr:
        status_text(
            ankiconnect_server.ServerStatus(ServerState.LISTENING, "a", 1), True
        )
        # a string, so the port is not written as "8,765"
        tr.preferences_ankiconnect_status_listening.assert_called_once_with(
            address="a", port="1"
        )
        status_text(
            ankiconnect_server.ServerStatus(ServerState.LISTENING, "a", 1), False
        )
        tr.preferences_ankiconnect_status_off.assert_called_once()


def test_port_in_use_is_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ankiconnect_server, "RETRY_SECS", 0.05)
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    mw = _make_mw(cast(Any, None))
    service = AnkiConnectService(mw, AnkiConnectSettings(enabled=True, bind_port=port))
    warnings = []
    monkeypatch.setattr(service, "_warn_listen_failed", warnings.append)
    try:
        service.start()
        deadline = time.time() + 10
        while (
            service.status.state != ServerState.PORT_IN_USE and time.time() < deadline
        ):
            time.sleep(0.01)
        assert service.status.state == ServerState.PORT_IN_USE
        time.sleep(0.2)
        # told once, however often it fails
        assert len(warnings) == 1 and warnings[0].port == port
        blocker.close()
        while service.status.state != ServerState.LISTENING and time.time() < deadline:
            time.sleep(0.01)
        assert service.status.state == ServerState.LISTENING
        assert service.port == port
    finally:
        blocker.close()
        service.shutdown()


# The add-on (ankiconnect.addon-blocked, ankiconnect.settings)
######################################################################


class FakeAddons:
    def __init__(self, addons: dict[str, dict[str, Any]]) -> None:
        self.addons = addons
        self.toggled: list[tuple[str, bool]] = []

    def allAddons(self) -> list[str]:
        return list(self.addons)

    def addonMeta(self, folder: str) -> dict[str, Any]:
        return {"name": self.addons[folder].get("name")}

    def isEnabled(self, folder: str) -> bool:
        return self.addons[folder]["enabled"]

    def getConfig(self, folder: str) -> dict[str, Any] | None:
        return self.addons[folder].get("config")

    def toggleEnabled(self, folder: str, enable: bool) -> None:
        self.toggled.append((folder, enable))
        self.addons[folder]["enabled"] = enable


def test_ankiconnect_addon_is_recognised_by_id_or_name() -> None:
    assert is_ankiconnect_addon("2055492159")
    assert is_ankiconnect_addon("1635024181")
    assert is_ankiconnect_addon("AnkiConnectDev")
    assert is_ankiconnect_addon("12345", "AnkiConnect")
    assert is_ankiconnect_addon("12345", "AnkiConnect Extended")
    assert is_ankiconnect_addon("12345", "anki-connect")
    assert not is_ankiconnect_addon("1771074083", "Review Heatmap")
    assert not is_ankiconnect_addon("12345", "AnkiConnect helper tools")


def test_migration_takes_the_addons_settings() -> None:
    addons = FakeAddons(
        {
            "2055492159": {
                "name": "AnkiConnect",
                "enabled": True,
                "config": {
                    "apiKey": "key",
                    "webBindPort": 8766,
                    "webCorsOriginList": ["http://localhost", "https://x.org"],
                },
            },
            "other": {"name": "Other", "enabled": True},
        }
    )
    pm = SimpleNamespace(meta={}, profile=None, db=None)
    settings = migrate_addon_settings(pm, addons)
    assert settings.enabled is True
    assert settings.api_key == "key" and settings.bind_port == 8766
    assert settings.cors_origins == ("http://localhost", "https://x.org")
    assert pm.meta["ankiConnect"]["enabled"] is True
    # read only: nothing was written to the add-on
    assert addons.toggled == []
    # the second start keeps what is stored
    addons.addons["2055492159"]["config"]["webBindPort"] = 1
    assert migrate_addon_settings(pm, addons).bind_port == 8766


def test_migration_without_the_addon_or_with_it_disabled() -> None:
    pm = SimpleNamespace(meta={}, profile=None, db=None)
    assert migrate_addon_settings(pm, FakeAddons({})) == AnkiConnectSettings()
    pm = SimpleNamespace(meta={}, profile=None, db=None)
    disabled = FakeAddons(
        {
            "2055492159": {
                "name": "AnkiConnect",
                "enabled": False,
                "config": {"webBindPort": 9},
            }
        }
    )
    settings = migrate_addon_settings(pm, disabled)
    assert settings.enabled is False and settings.bind_port == 9


def test_an_enabled_ankiconnect_addon_is_disabled() -> None:
    addons = FakeAddons(
        {
            "2055492159": {"name": "AnkiConnect", "enabled": True},
            "1635024181": {"name": "AnkiConnect Extended", "enabled": False},
            "999": {"name": "AnkiConnect", "enabled": True},
            "1771074083": {"name": "Review Heatmap", "enabled": True},
        }
    )
    assert disable_ankiconnect_addon(addons) == ["2055492159", "999"]
    assert addons.toggled == [("2055492159", False), ("999", False)]


def test_start_up_turns_the_builtin_on_in_place_of_the_addon() -> None:
    from aqt.main import AnkiQt

    addons = FakeAddons({"2055492159": {"name": "AnkiConnect", "enabled": True}})
    pm = SimpleNamespace(meta={}, profile=None, db=None)
    mw = cast(Any, SimpleNamespace(pm=pm, addonManager=addons))
    assert AnkiQt._take_over_ankiconnect_addon(mw) is True
    assert pm.meta["ankiConnect"]["enabled"] is True
    assert addons.toggled == [("2055492159", False)]
    # nothing to disable the next time
    assert AnkiQt._take_over_ankiconnect_addon(mw) is False


def test_the_ankiconnect_notice_is_shown_only_once() -> None:
    from aqt.main import AnkiQt

    shown: list[str] = []
    mw = cast(
        Any,
        SimpleNamespace(
            _ankiconnect_addon_notice_pending=True,
            pm=SimpleNamespace(meta={}, save=MagicMock()),
        ),
    )
    with patch("aqt.main.showInfo", side_effect=lambda text, **_: shown.append(text)):
        AnkiQt._show_ankiconnect_addon_notice(mw)
        AnkiQt._show_ankiconnect_addon_notice(mw)
    assert len(shown) == 1
    assert mw.pm.meta[ADDON_NOTICE_SHOWN_KEY] is True
    mw.pm.save.assert_called_once()
    # once shown, a copy disabled later brings no notice
    addons = FakeAddons({"2055492159": {"name": "AnkiConnect", "enabled": True}})
    taking_over = cast(Any, SimpleNamespace(pm=mw.pm, addonManager=addons))
    assert AnkiQt._take_over_ankiconnect_addon(taking_over) is False
    assert addons.toggled == [("2055492159", False)]


def test_enabling_the_ankiconnect_addon_is_refused_with_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aqt.addons import AddonManager

    service = AnkiConnectService(_make_mw(cast(Any, None)), AnkiConnectSettings())
    monkeypatch.setattr(ankiconnect, "_instance", service)
    monkeypatch.setattr(service, "start", lambda listen_now=False: None)
    written: list[bool] = []
    for folder, name, expect_enabled in (
        ("2055492159", "AnkiConnect", False),
        ("555", "AnkiConnect Extended", False),
        ("other_addon", "Other", True),
    ):
        addon = SimpleNamespace(
            enabled=False, human_name=lambda: folder, provided_name=name
        )
        manager = MagicMock()
        manager.addon_meta.return_value = addon
        manager._disableConflicting.return_value = []
        manager.write_addon_meta.side_effect = lambda meta: written.append(meta.enabled)
        with (
            patch("aqt.addons.showInfo") as show_info,
            patch("aqt.addons.tr") as tr,
        ):
            AddonManager.toggleEnabled(manager, folder, enable=True)
        assert addon.enabled is expect_enabled
        if expect_enabled:
            show_info.assert_not_called()
        else:
            show_info.assert_called_once_with(
                tr.preferences_ankiconnect_addon_blocked.return_value,
                textFormat="plain",
            )
    assert written == [False, False, True]
    # the built-in AnkiConnect was turned on instead
    assert service.settings.enabled is True


def test_installing_the_ankiconnect_addon_leaves_it_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import zipfile

    from aqt.addons import AddonManager

    service = AnkiConnectService(_make_mw(cast(Any, None)), AnkiConnectSettings())
    monkeypatch.setattr(ankiconnect, "_instance", service)
    monkeypatch.setattr(service, "start", lambda listen_now=False: None)
    for previous_meta, expect_message in (({}, True), ({"name": "AnkiConnect"}, False)):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w"):
            pass
        manager = MagicMock()
        manager.readManifestFile.return_value = {
            "package": "2055492159",
            "name": "AnkiConnect",
        }
        manager._manifest_schema = {"properties": {"name": {"meta": True}}}
        manager.addonMeta.return_value = dict(previous_meta)
        manager._disableConflicting.return_value = []
        with (
            patch("aqt.addons.showInfo") as show_info,
            patch("aqt.addons.tr"),
            patch("aqt.addons.gui_hooks"),
        ):
            AddonManager.install(manager, archive, force_enable=True)
        package, meta = manager.writeAddonMeta.call_args.args
        assert package == "2055492159" and meta["disabled"] is True
        # a fresh install says so; an update of an installed copy is silent
        assert show_info.called is expect_message
    assert service.settings.enabled is True
