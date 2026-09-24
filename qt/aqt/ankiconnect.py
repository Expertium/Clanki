# Copyright: Ankitects Pty Ltd and contributors
# Copyright 2016-2021 Alex Yatskov (AnkiConnect)
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Clanki's built-in AnkiConnect (spec ankiconnect.*).

Ported from the AnkiConnect add-on (AnkiWeb 2055492159, Alex Yatskov and
contributors, GPLv3 or later) together with the additions of its JSchoreels
fork, "AnkiConnect Extended" (https://github.com/JSchoreels/anki-connect).
Its HTTP API is a compatibility boundary: every action keeps its name, its
parameters, its result and its error text, so clients such as Yomitan keep
working unchanged. The differences are listed in spec/ankiconnect.md:
chiefly that the socket I/O runs on threads of its own, collection work
runs off the main thread (only the gui* actions and a few others go to it),
the screens refresh after a change instead of a full reset, and scheduling
values follow the collection's one algorithm.

The settings live in the profile manager's global meta (`ankiConnect`),
like the add-on's, which lived in the add-ons folder shared by all
profiles; Preferences > AnkiConnect edits them.
"""

from __future__ import annotations

import base64
import glob
import hashlib
import hmac
import json
import os
import os.path
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, TypeVar

import anki
import anki.utils
from anki.cards import Card
from anki.collection import OpChanges
from anki.consts import MODEL_CLOZE
from anki.errors import NotFoundError
from anki.notes import Note
from aqt.ankiconnect_server import (
    ServerState,
    ServerStatus,
    WebServer,
    WebSettings,
    format_exception_reply,
    format_success_reply,
)

if TYPE_CHECKING:
    from aqt.main import AnkiQt
    from aqt.operations.scheduling import GradeNowResult

_T = TypeVar("_T")

API_VERSION = 6
DEFAULT_PORT = 8765
DEFAULT_ADDRESS = "127.0.0.1"
DEFAULT_CORS_ORIGINS = ("http://localhost",)

# pm.meta key holding the settings
SETTINGS_KEY = "ankiConnect"
# pm.meta flag: the "add-on disabled" notice was shown
ADDON_NOTICE_SHOWN_KEY = "ankiConnectAddonNoticeShown"
# the add-on's folders: AnkiWeb ids of AnkiConnect and of the JSchoreels
# fork (AnkiConnect Extended), and the names of source installs (link.sh
# makes "AnkiConnectDev"), compared without case
ADDON_DIRS = (
    "2055492159",
    "1635024181",
    "ankiconnectdev",
    "ankiconnect",
    "anki-connect",
    "anki_connect",
)
# an add-on whose manifest/meta name is one of these, compared without
# case, spaces, hyphens or underscores
ADDON_NAMES = ("ankiconnect", "ankiconnectextended")


# Settings
######################################################################


@dataclass(frozen=True)
class AnkiConnectSettings:
    """The add-on's config.json/config.md settings, plus the on/off switch.

    Off by default: a new user gets no open port until they turn it on (or
    try to install the add-on). A user whose AnkiConnect add-on was enabled
    keeps it working: it is migrated on, with its settings."""

    enabled: bool = False
    bind_address: str = DEFAULT_ADDRESS
    bind_port: int = DEFAULT_PORT
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS
    # None: requests need no key (the add-on's `apiKey: null`)
    api_key: str | None = None
    ignore_origins: tuple[str, ...] = ()
    # the deprecated single origin `webCorsOrigin`
    cors_origin: str | None = None
    # the add-on's request log (`apiLogPath`), no control in Preferences
    api_log_path: str | None = None
    # timeout of storeMediaFile's downloads, in ms (`webTimeout`)
    web_timeout_ms: int = 10000

    def to_config(self) -> dict[str, Any]:
        """Stored with the add-on's key names."""
        return {
            "enabled": self.enabled,
            "webBindAddress": self.bind_address,
            "webBindPort": self.bind_port,
            "webCorsOriginList": list(self.cors_origins),
            "apiKey": self.api_key,
            "ignoreOriginList": list(self.ignore_origins),
            "webCorsOrigin": self.cors_origin,
            "apiLogPath": self.api_log_path,
            "webTimeout": self.web_timeout_ms,
        }

    @classmethod
    def from_config(cls, data: object) -> AnkiConnectSettings:
        """Settings from stored JSON (ours or the add-on's); anything
        missing or invalid is the default."""

        if not isinstance(data, Mapping):
            return cls()
        default = cls()
        values: dict[str, Any] = {}
        enabled = data.get("enabled")
        if isinstance(enabled, bool):
            values["enabled"] = enabled
        address = data.get("webBindAddress")
        if isinstance(address, str) and address.strip():
            values["bind_address"] = address.strip()
        port = data.get("webBindPort")
        if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536:
            values["bind_port"] = port
        for key, name in (
            ("webCorsOriginList", "cors_origins"),
            ("ignoreOriginList", "ignore_origins"),
        ):
            items = data.get(key)
            if isinstance(items, (list, tuple)):
                values[name] = tuple(item for item in items if isinstance(item, str))
        api_key = data.get("apiKey")
        if api_key is None or isinstance(api_key, str):
            values["api_key"] = api_key
        for key, name in (
            ("webCorsOrigin", "cors_origin"),
            ("apiLogPath", "api_log_path"),
        ):
            value = data.get(key)
            if isinstance(value, str) and value:
                values[name] = value
        timeout = data.get("webTimeout")
        if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
            values["web_timeout_ms"] = timeout
        return replace(default, **values)

    def web_settings(self) -> WebSettings:
        return WebSettings(
            bind_address=self.bind_address,
            bind_port=self.bind_port,
            cors_origins=self.cors_origins,
            # the add-on read its default from this variable
            extra_origin=self.cors_origin or os.getenv("ANKICONNECT_CORS_ORIGIN"),
            api_version=API_VERSION,
        )


def load_settings(pm: object) -> AnkiConnectSettings:
    meta = getattr(pm, "meta", None)
    stored = meta.get(SETTINGS_KEY) if isinstance(meta, Mapping) else None
    return AnkiConnectSettings.from_config(stored)


def save_settings(pm: object, settings: AnkiConnectSettings) -> None:
    """Store the settings in the global meta and write it to disk."""
    meta = getattr(pm, "meta", None)
    if not isinstance(meta, dict):
        return
    meta[SETTINGS_KEY] = settings.to_config()
    _write_meta(pm)


def _write_meta(pm: object) -> None:
    # pm.save() also writes the open profile, which may not exist yet (the
    # add-ons load before a profile is chosen); write the global row only
    try:
        if getattr(pm, "profile", None) is not None:
            pm.save()  # type: ignore[attr-defined]
            return
        db = getattr(pm, "db", None)
        pickle = getattr(pm, "_pickle", None)
        if db is None or not callable(pickle):
            return
        db.execute(
            "update profiles set data = ? where name = ? collate nocase",
            pickle(pm.meta),  # type: ignore[attr-defined]
            "_global",
        )
        db.commit()
    except Exception:
        import traceback

        traceback.print_exc()


# The add-on
######################################################################


def _normalized_name(name: object) -> str:
    if not isinstance(name, str):
        return ""
    return re.sub(r"[\s_\-]", "", name).lower()


def is_ankiconnect_addon(folder: str, name: object = None) -> bool:
    """True for the AnkiConnect add-on (either AnkiWeb id, a source
    install) or any add-on whose manifest or meta name is AnkiConnect."""
    return folder.lower() in ADDON_DIRS or _normalized_name(name) in ADDON_NAMES


def ankiconnect_addon_folders(addon_manager: object) -> list[str]:
    """The installed copies of the add-on."""
    all_addons = getattr(addon_manager, "allAddons", None)
    addon_meta = getattr(addon_manager, "addonMeta", None)
    if not callable(all_addons):
        return []
    folders = []
    for folder in all_addons():
        name = None
        if callable(addon_meta):
            try:
                name = addon_meta(folder).get("name")
            except Exception:
                name = None
        if is_ankiconnect_addon(folder, name):
            folders.append(folder)
    return folders


def migrate_addon_settings(pm: object, addon_manager: object) -> AnkiConnectSettings:
    """On the first start with the built-in AnkiConnect: its settings from
    an installed add-on (read only: its config.json with the user's changes
    from its meta.json, as the add-on manager merges them), on when the
    add-on was enabled. Later starts keep the stored settings."""

    meta = getattr(pm, "meta", None)
    if isinstance(meta, Mapping) and isinstance(meta.get(SETTINGS_KEY), Mapping):
        return load_settings(pm)
    get_config = getattr(addon_manager, "getConfig", None)
    is_enabled = getattr(addon_manager, "isEnabled", None)
    config: Mapping[str, Any] = {}
    enabled = False
    folders = ankiconnect_addon_folders(addon_manager)
    # an enabled copy first, then the official add-on's id
    for folder in sorted(
        folders,
        key=lambda f: (
            not (callable(is_enabled) and is_enabled(f)),
            f != ADDON_DIRS[0],
        ),
    ):
        folder_enabled = bool(callable(is_enabled) and is_enabled(folder))
        enabled = enabled or folder_enabled
        if not config and callable(get_config):
            try:
                config = get_config(folder) or {}
            except Exception:
                config = {}
    settings = replace(AnkiConnectSettings.from_config(config), enabled=enabled)
    save_settings(pm, settings)
    return settings


def disable_ankiconnect_addon(addon_manager: object) -> list[str]:
    """Disable every installed, enabled copy of the add-on (both would want
    the same port). Returns the folders disabled."""

    is_enabled = getattr(addon_manager, "isEnabled", None)
    toggle = getattr(addon_manager, "toggleEnabled", None)
    if not (callable(is_enabled) and callable(toggle)):
        return []
    disabled = []
    for folder in ankiconnect_addon_folders(addon_manager):
        if is_enabled(folder):
            toggle(folder, enable=False)
            disabled.append(folder)
    return disabled


# What an action changes, for the screens' refresh
######################################################################

_NOTES = (
    "card",
    "note",
    "note_text",
    "tag",
    "browser_table",
    "browser_sidebar",
    "study_queues",
    "mtime",
)
_TAGS = ("note", "note_text", "tag", "browser_table", "browser_sidebar", "mtime")
_CARDS = ("card", "browser_table", "study_queues", "mtime")
_DECKS = (
    "deck",
    "card",
    "browser_table",
    "browser_sidebar",
    "study_queues",
    "mtime",
)
_DECK_CONFIG = ("deck_config", "study_queues", "mtime")
_NOTETYPES = (
    "notetype",
    "note",
    "note_text",
    "card",
    "browser_table",
    "browser_sidebar",
    "study_queues",
    "mtime",
)
_EVERYTHING = tuple(f.name for f in OpChanges.DESCRIPTOR.fields if f.name != "kind")


def changes_from(names: Iterable[str]) -> OpChanges:
    changes = OpChanges()
    for name in names:
        setattr(changes, name, True)
    return changes


# Actions
######################################################################


def api(
    *,
    main: bool = False,
    changes: Sequence[str] = (),
    preserve: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> Callable[[_T], _T]:
    """Mark an AnkiConnect action (the add-on's `util.api()`).

    main: runs on the main thread (it touches windows or the profile
    manager); every other action runs on the connection's thread.
    changes: the OpChanges fields the screens refresh for afterwards.
    preserve: the action is a non-review change the RWKV state survives,
    wrapped the way Clanki's own screens wrap the same change; the function
    gives the card_ids / note_ids of the request."""

    def decorator(func: _T) -> _T:
        setattr(func, "api", True)
        setattr(func, "versions", ())
        setattr(func, "main_thread", main)
        setattr(func, "changes", tuple(changes))
        setattr(func, "preserve", preserve)
        return func

    return decorator


def _ids(value: object) -> tuple[int, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(v for v in value if isinstance(v, int) and not isinstance(v, bool))
    if isinstance(value, int) and not isinstance(value, bool):
        return (value,)
    return ()


def _card_ids(params: dict[str, Any]) -> dict[str, Any]:
    return {"card_ids": _ids(params.get("cards"))}


def _note_ids(params: dict[str, Any]) -> dict[str, Any]:
    return {"note_ids": _ids(params.get("notes"))}


def _note_id(params: dict[str, Any]) -> dict[str, Any]:
    note = params.get("note")
    if isinstance(note, Mapping):
        return {"note_ids": _ids(note.get("id"))}
    return {"note_ids": _ids(note)}


def _no_ids(params: dict[str, Any]) -> dict[str, Any]:
    return {}


class MediaType:
    Audio = 1
    Video = 2
    Picture = 3


def _batched(items: Sequence[_T], n: int) -> Iterable[Sequence[_T]]:
    for start in range(0, len(items), n):
        yield items[start : start + n]


class AnkiConnect:
    """The actions. Names, parameters, results and error messages are the
    add-on's (and its fork's); a TypeError for a wrong parameter reads
    "AnkiConnect.<action>() got an unexpected keyword argument ...", as
    there."""

    def __init__(self, service: AnkiConnectService) -> None:
        self._service = service

    # dispatch (the add-on's handler)

    def handler(self, request: dict[str, Any]) -> Any:
        self._service.log_event("request", request)

        name = request.get("action", "")
        version = request.get("version", 4)
        params = request.get("params", {})
        key = request.get("key")

        try:
            if not _key_matches(key, self._service.settings.api_key) and (
                name != "requestPermission"
            ):
                raise Exception("valid api key must be provided")

            method = self._action(name)
            if method is None:
                raise Exception("unsupported action")

            api_return_value = self._call(method, params)
            reply = format_success_reply(version, api_return_value)
        except Exception as e:
            reply = format_exception_reply(version, e)

        self._service.log_event("reply", reply)
        return reply

    def _action(self, name: object) -> Callable[..., Any] | None:
        if not isinstance(name, str) or name.startswith("_"):
            return None
        method = getattr(self, name, None)
        if callable(method) and getattr(method, "api", False):
            return method
        return None

    def _call(self, method: Callable[..., Any], params: Any) -> Any:
        def run() -> Any:
            return method(**params)

        action: Callable[[], Any] = run
        preserve = getattr(method, "preserve", None)
        if preserve is not None:
            action = self._preserving_rwkv_state(
                run, preserve(params) if isinstance(params, dict) else {}
            )

        changes = getattr(method, "changes", ())
        # A change RWKV cannot keep its state through is refreshed for on
        # its own: the refresh of a burst tells RWKV that every change in it
        # was wrapped, so none may hide in one.
        alone = bool(changes) and preserve is None
        if alone:
            self._service.refresh_now()
        try:
            if getattr(method, "main_thread", False):
                return self._service.run_on_main(action)
            return action()
        finally:
            if changes:
                self._service.note_changes(changes)
            if alone:
                self._service.refresh_now()

    def _preserving_rwkv_state(
        self, action: Callable[[], Any], ids: dict[str, Any]
    ) -> Callable[[], Any]:
        """The change wrapped as Clanki's own screens wrap it, so the RWKV
        state survives it instead of being rebuilt."""

        def run() -> Any:
            from aqt import rwkv_scheduler

            return rwkv_scheduler.run_collection_mutation_preserving_rwkv_state(
                self.collection(), action, force_reconciliation=True, **ids
            )

        return run

    # access

    def window(self) -> AnkiQt:
        return self._service.mw

    def reviewer(self) -> Any:
        reviewer = self.window().reviewer
        if reviewer is None:
            raise Exception("reviewer is not available")
        return reviewer

    def collection(self) -> Any:
        collection = self.window().col
        if collection is None:
            raise Exception("collection is not available")
        return collection

    def decks(self) -> Any:
        decks = self.collection().decks
        if decks is None:
            raise Exception("decks are not available")
        return decks

    def scheduler(self) -> Any:
        scheduler = self.collection().sched
        if scheduler is None:
            raise Exception("scheduler is not available")
        return scheduler

    def database(self) -> Any:
        database = self.collection().db
        if database is None:
            raise Exception("database is not available")
        return database

    def media(self) -> Any:
        media = self.collection().media
        if media is None:
            raise Exception("media is not available")
        return media

    def save_model(self, models: Any, ankiModel: Any) -> None:
        models.update_dict(ankiModel)

    def getModel(self, modelName: str) -> Any:
        model = self.collection().models.by_name(modelName)
        if model is None:
            raise Exception("model was not found: {}".format(modelName))
        return model

    def getField(self, model: Any, fieldName: str) -> Any:
        fieldMap = self.collection().models.field_map(model)
        if fieldName not in fieldMap:
            raise Exception(
                "field was not found in {}: {}".format(model["name"], fieldName)
            )
        return fieldMap[fieldName][1]

    def getTemplate(self, model: Any, templateName: str) -> Any:
        for ankiTemplate in model["tmpls"]:
            if ankiTemplate["name"] == templateName:
                return ankiTemplate
        raise Exception(
            "template was not found in {}: {}".format(model["name"], templateName)
        )

    def startEditing(self) -> None:
        # the add-on called mw.requireReset() here, a full reset of every
        # screen; the actions that change the collection now declare what
        # they change, and the screens refresh for exactly that
        pass

    def createNote(self, note: Any) -> Note:
        collection = self.collection()

        model = collection.models.by_name(note["modelName"])
        if model is None:
            raise Exception("model was not found: {}".format(note["modelName"]))

        deck = collection.decks.by_name(note["deckName"])
        if deck is None:
            raise Exception("deck was not found: {}".format(note["deckName"]))

        ankiNote = Note(collection, model)
        if "tags" in note:
            ankiNote.tags = note["tags"]

        for name, value in note["fields"].items():
            for ankiName in ankiNote.keys():
                if name.lower() == ankiName.lower():
                    ankiNote[ankiName] = value
                    break

        self.addMediaFromNote(ankiNote, note)

        allowDuplicate = False
        duplicateScope = None
        duplicateScopeDeckName = None
        duplicateScopeCheckChildren = False
        duplicateScopeCheckAllModels = False

        if "options" in note:
            options = note["options"]
            if "allowDuplicate" in options:
                allowDuplicate = options["allowDuplicate"]
                if type(allowDuplicate) is not bool:
                    raise Exception('option parameter "allowDuplicate" must be boolean')
            if "duplicateScope" in options:
                duplicateScope = options["duplicateScope"]
            if "duplicateScopeOptions" in options:
                duplicateScopeOptions = options["duplicateScopeOptions"]
                if "deckName" in duplicateScopeOptions:
                    duplicateScopeDeckName = duplicateScopeOptions["deckName"]
                if "checkChildren" in duplicateScopeOptions:
                    duplicateScopeCheckChildren = duplicateScopeOptions["checkChildren"]
                    if type(duplicateScopeCheckChildren) is not bool:
                        raise Exception(
                            'option parameter "duplicateScopeOptions.checkChildren" must be boolean'
                        )
                if "checkAllModels" in duplicateScopeOptions:
                    duplicateScopeCheckAllModels = duplicateScopeOptions[
                        "checkAllModels"
                    ]
                    if type(duplicateScopeCheckAllModels) is not bool:
                        raise Exception(
                            'option parameter "duplicateScopeOptions.checkAllModels" must be boolean'
                        )

        duplicateOrEmpty = self.isNoteDuplicateOrEmptyInScope(
            ankiNote,
            deck,
            collection,
            duplicateScope,
            duplicateScopeDeckName,
            duplicateScopeCheckChildren,
            duplicateScopeCheckAllModels,
        )

        if duplicateOrEmpty == 1:
            raise Exception("cannot create note because it is empty")
        elif duplicateOrEmpty == 2:
            if allowDuplicate:
                return ankiNote
            raise Exception("cannot create note because it is a duplicate")
        elif duplicateOrEmpty == 0:
            return ankiNote
        else:
            raise Exception("cannot create note for unknown reason")

    def isNoteDuplicateOrEmptyInScope(
        self,
        note: Note,
        deck: Any,
        collection: Any,
        duplicateScope: Any,
        duplicateScopeDeckName: Any,
        duplicateScopeCheckChildren: bool,
        duplicateScopeCheckAllModels: bool,
    ) -> int:
        # 1 if empty, 2 if a duplicate, 0 otherwise
        if duplicateScope != "deck" and not duplicateScopeCheckAllModels:
            return note.fields_check() or 0

        val = note.fields[0]
        if not val.strip():
            return 1
        csum = anki.utils.field_checksum(val)

        dids = None
        if duplicateScope == "deck":
            did = deck["id"]
            if duplicateScopeDeckName is not None:
                deck2 = collection.decks.by_name(duplicateScopeDeckName)
                if deck2 is None:
                    # an invalid deck cannot hold a duplicate
                    return 0
                did = deck2["id"]

            dids = {did: True}
            if duplicateScopeCheckChildren:
                for kv in collection.decks.children(did):
                    dids[kv[1]] = True

        query = "select id from notes where csum=?"
        queryArgs: list[Any] = [csum]
        if note.id:
            query += " and id!=?"
            queryArgs.append(note.id)
        if not duplicateScopeCheckAllModels:
            query += " and mid=?"
            queryArgs.append(note.mid)

        for noteId in note.col.db.list(query, *queryArgs):
            if dids is None:
                return 2
            for cardDeckId in note.col.db.list(
                "select did from cards where nid=?", noteId
            ):
                if cardDeckId in dids:
                    return 2

        return 0

    def raiseNotFoundError(self, errorMsg: str) -> None:
        raise NotFoundError(errorMsg, None, None, None)

    def getCard(self, card_id: int) -> Card:
        try:
            return self.collection().get_card(card_id)
        except NotFoundError:
            self.raiseNotFoundError("Card was not found: {}".format(card_id))
            raise

    def getNote(self, note_id: int) -> Note:
        try:
            return self.collection().get_note(note_id)
        except NotFoundError:
            self.raiseNotFoundError("Note was not found: {}".format(note_id))
            raise

    def deckStatsToJson(self, due_tree: Any) -> dict[str, Any]:
        return {
            "deck_id": due_tree.deck_id,
            "name": due_tree.name,
            "new_count": due_tree.new_count,
            "learn_count": due_tree.learn_count,
            "review_count": due_tree.review_count,
            "total_in_deck": due_tree.total_in_deck,
        }

    def collectDeckTreeChildren(self, parent_node: Any) -> dict[int, Any]:
        allNodes = {parent_node.deck_id: parent_node}
        for child in parent_node.children:
            for deckId, childNode in self.collectDeckTreeChildren(child).items():
                allNodes[deckId] = childNode
        return allNodes

    # scheduling values of one algorithm (spec ankiconnect.one-algorithm)

    def _algorithm_reviewer(self) -> Any:
        mw = self.window()
        return getattr(mw, "reviewer", None) or SimpleNamespace(mw=mw)

    def _card_algorithm(self, card: Card) -> str:
        """'fsrs7', 'rwkvCurve' or 'rwkvInstant': what schedules the card,
        as card info decides it."""
        from aqt import rwkv_scheduler

        reviewer = self._algorithm_reviewer()
        try:
            if rwkv_scheduler.rwkv_review_enabled(reviewer, card):
                return "rwkvCurve"
            if rwkv_scheduler.answer_intervals_hidden(reviewer, card):
                return "rwkvInstant"
        except Exception:
            pass
        return "fsrs7"

    def _rwkv_curve(self, card: Card) -> Any:
        """RWKV-Curve's curve for the card at the time since its last
        answered review (what card info shows), or None."""
        from aqt import rwkv_scheduler

        last = self.database().scalar(
            "select max(id) from revlog where cid=? and ease > 0", card.id
        )
        elapsed_days = max(0.0, time.time() - last / 1000) / 86_400 if last else None
        try:
            return rwkv_scheduler.rwkv_card_info_curve(
                self._algorithm_reviewer(), card, elapsed_days=elapsed_days
            )
        except Exception:
            return None

    def _rwkv_instant_retrievability(self, card: Card) -> float | None:
        """RWKV-Instant's R for the card, the value card info shows, or None
        while RWKV cannot give it."""
        from aqt import rwkv_scheduler

        reviewer = self._algorithm_reviewer()
        try:
            if rwkv_scheduler._reviewer_backend is None:
                rwkv_scheduler.configure_reviewer_backend_from_environment()
            diagnostics = rwkv_scheduler._queried_card_info_diagnostics(
                reviewer,
                card,
                fallback_source="FSRS" if card.memory_state else "SM2",
                _candidate=rwkv_scheduler._card_info_review_candidate(reviewer, card),
            )
        except Exception:
            return None
        value = getattr(diagnostics, "retrievability", None)
        return float(value) if isinstance(value, (int, float)) else None

    def _prop_values(
        self, card: Card, requested: Sequence[str], d_is_difficulty: bool
    ) -> dict[str, Any]:
        """prop:r, prop:s and prop:d of the fork's cardsInfo / cardsDetails /
        findCards. FSRS-7: its R, S90 and difficulty. RWKV-Curve: the curve's
        recall now and S90, no difficulty. RWKV-Instant: RWKV's R only.
        prop:d of cardsDetails is the card's desired retention (as in the
        fork), whatever the algorithm."""
        algorithm = self._card_algorithm(card)
        stats = self.collection().card_stats_data(card.id)
        curve = self._rwkv_curve(card) if algorithm == "rwkvCurve" else None
        values: dict[str, Any] = {}
        for field_name in requested:
            if field_name == "prop:r":
                if algorithm == "fsrs7":
                    values[field_name] = (
                        stats.fsrs_retrievability
                        if stats.HasField("fsrs_retrievability")
                        else None
                    )
                elif algorithm == "rwkvCurve":
                    values[field_name] = curve.current_recall if curve else None
                else:
                    values[field_name] = self._rwkv_instant_retrievability(card)
            elif field_name == "prop:s":
                if algorithm == "fsrs7":
                    values[field_name] = (
                        stats.memory_state.stability
                        if stats.HasField("memory_state")
                        else None
                    )
                elif algorithm == "rwkvCurve":
                    values[field_name] = curve.s90 if curve else None
                else:
                    values[field_name] = None
            elif field_name == "prop:d":
                if not d_is_difficulty:
                    values[field_name] = (
                        stats.desired_retention
                        if stats.HasField("desired_retention")
                        else None
                    )
                elif algorithm == "fsrs7":
                    values[field_name] = (
                        stats.memory_state.difficulty
                        if stats.HasField("memory_state")
                        else None
                    )
                else:
                    values[field_name] = None
        return values

    def _next_reviews(self, card: Card) -> list[str]:
        """cardsInfo's nextReviews: FSRS-7's four interval labels; none for
        RWKV-Curve (its intervals come from the prediction the reviewer makes
        when the card is shown) and RWKV-Instant (no intervals)."""
        if self._card_algorithm(card) != "fsrs7":
            return ["", "", "", ""]
        collection = self.collection()
        states = collection._backend.get_scheduling_states(card.id)
        return list(collection._backend.describe_next_states(states))

    def _prepare_search(self, query: str) -> None:
        """Before a search that needs RWKV scores, prepare them as the
        Browser does, so the result is the Browser's."""
        from aqt import rwkv_scheduler

        if rwkv_scheduler.search_needs_rwkv_values(self.collection(), query):
            rwkv_scheduler.prepare_browser_retrievability_scores(self.window(), query)

    #
    # Miscellaneous
    #

    @api()
    def version(self) -> int:
        return API_VERSION

    @api(main=True)
    def requestPermission(self, origin: str, allowed: bool) -> dict[str, Any]:
        settings = self._service.settings
        results: dict[str, Any] = {"permission": "denied"}

        if allowed:
            results = {
                "permission": "granted",
                "requireApikey": bool(settings.api_key),
                "version": API_VERSION,
            }
        elif origin in settings.ignore_origins:
            pass  # denied
        else:
            from aqt.qt import QCheckBox, QMessageBox, Qt
            from aqt.utils import tr

            msg = QMessageBox(None)
            msg.setWindowTitle(tr.preferences_ankiconnect_permission_title())
            msg.setText(tr.preferences_ankiconnect_permission_text(origin=origin))
            msg.setInformativeText(tr.preferences_ankiconnect_permission_details())
            msg.setWindowIcon(self.window().windowIcon())
            msg.setIcon(QMessageBox.Icon.Question)
            msg.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            msg.setDefaultButton(QMessageBox.StandardButton.No)
            msg.setCheckBox(
                QCheckBox(
                    text=tr.preferences_ankiconnect_permission_ignore(origin=origin),
                    parent=msg,
                )
            )
            msg.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)
            pressedButton = msg.exec()

            if pressedButton == QMessageBox.StandardButton.Yes:
                self._service.apply_settings(
                    replace(
                        self._service.settings,
                        cors_origins=(*self._service.settings.cors_origins, origin),
                    )
                )
                results = {
                    "permission": "granted",
                    "requireApikey": bool(self._service.settings.api_key),
                    "version": API_VERSION,
                }
            elif (
                origin
                and pressedButton == QMessageBox.StandardButton.No
                and msg.checkBox().isChecked()  # type: ignore[union-attr]
            ):
                self._service.apply_settings(
                    replace(
                        self._service.settings,
                        ignore_origins=(*self._service.settings.ignore_origins, origin),
                    )
                )

        return results

    @api(main=True)
    def getProfiles(self) -> list[str]:
        return self.window().pm.profiles()

    @api(main=True)
    def getActiveProfile(self) -> str | None:
        return self.window().pm.name

    @api(main=True)
    def loadProfile(self, name: str) -> bool:
        return self._load_profile(name)

    def _load_profile(self, name: str) -> bool:
        from aqt.qt import QTimer

        mw = self.window()
        if name not in mw.pm.profiles():
            return False

        if mw.isVisible():
            cur_profile = mw.pm.name
            if cur_profile != name:
                mw.unloadProfileAndShowProfileManager()

                def waiter() -> None:
                    # wait until the main window is closed (a sync on close
                    # can take a while)
                    if mw.isVisible():
                        QTimer.singleShot(1000, waiter)
                    else:
                        self._load_profile(name)

                waiter()
        else:
            mw.pm.load(name)
            mw.loadProfile()
            mw.profileDiag.closeWithoutQuitting()

        return True

    @api()
    def sync(self) -> None:
        """The add-on's sync: a normal sync that fails when a full sync is
        needed, then the Sync button's sync. Clanki runs the first one as a
        background task, and tells RWKV about the reviews it brought in, as
        its own sync does (the second sync finds nothing new)."""
        mw = self.window()
        auth = mw.pm.sync_auth()
        if not auth:
            raise Exception("sync: auth not configured")
        collection = self.collection()
        media = mw.pm.media_syncing_enabled()
        out = self._service.run_collection_task(
            lambda: collection.sync_collection(auth, media)
        )
        accepted_sync_statuses = [out.NO_CHANGES, out.NORMAL_SYNC]
        if out.required not in accepted_sync_statuses:
            raise Exception(
                f"Sync status {out.required} not one of {accepted_sync_statuses} - see SyncCollectionResponse.ChangesRequired for list of sync statuses: https://github.com/ankitects/anki/blob/e41c4573d789afe8b020fab5d9d1eede50c3fa3d/proto/anki/sync.proto#L57-L65"
            )
        self._service.run_on_main(lambda: self._after_sync(out))

    def _after_sync(self, out: Any) -> None:
        from aqt import rwkv_scheduler

        mw = self.window()
        if mw.col is not None:
            mw.col._load_scheduler()
        mw.pm.set_host_number(out.host_number)
        if out.new_endpoint:
            mw.pm.set_current_sync_url(out.new_endpoint)

        def sync_button() -> None:
            mw.on_sync_button_clicked()

        if out.remote_collection_changed:
            ignored = (
                ()
                if out.remote_non_review_collection_changed
                else tuple(out.remote_review_ids)
            )
            rwkv_scheduler.refresh_rwkv_state_after_sync(
                mw, sync_button, remote_review_ids=ignored
            )
        else:
            sync_button()

    @api()
    def multi(self, actions: list[Any]) -> list[Any]:
        return list(map(self.handler, actions))

    @api()
    def getNumCardsReviewedToday(self) -> int:
        return self.database().scalar(
            "select count() from revlog where id > ?",
            (self.scheduler().day_cutoff - 86400) * 1000,
        )

    @api()
    def getNumCardsReviewedByDay(self) -> list[Any]:
        return self.database().all(
            'select date(id/1000 - ?, "unixepoch", "localtime") as day, count() from revlog group by day order by day desc',
            int(time.strftime("%H", time.localtime(self.scheduler().day_cutoff)))
            * 3600,
        )

    @api()
    def getCollectionStatsHTML(self, wholeCollection: bool = True) -> str:
        stats = self.collection().stats()
        stats.wholeCollection = wholeCollection
        return stats.report()

    #
    # Decks
    #

    @api()
    def deckNames(self) -> list[str]:
        return [x.name for x in self.decks().all_names_and_ids()]

    @api()
    def deckNamesAndIds(self) -> dict[str, int]:
        decks = {}
        for deck in self.deckNames():
            decks[deck] = self.decks().id(deck)
        return decks

    @api()
    def getDecks(self, cards: list[int]) -> dict[str, list[int]]:
        decks: dict[str, list[int]] = {}
        for card in cards:
            did = self.database().scalar("select did from cards where id=?", card)
            deck = self.decks().get(did)["name"]
            if deck in decks:
                decks[deck].append(card)
            else:
                decks[deck] = [card]
        return decks

    @api(changes=_DECKS, preserve=_no_ids)
    def createDeck(self, deck: str) -> int:
        self.startEditing()
        return self.decks().id(deck)

    @api(changes=_DECKS)
    def changeDeck(self, cards: list[int], deck: str) -> None:
        self.startEditing()

        did = self.collection().decks.id(deck)
        mod = anki.utils.int_time()
        usn = self.collection().usn()

        scids = anki.utils.ids2str(cards)
        # take the cards out of filtered decks first
        self.collection().sched.remFromDyn(cards)

        self.collection().db.execute(
            "update cards set usn=?, mod=?, did=? where id in " + scids, usn, mod, did
        )

    @api(changes=_DECKS)
    def deleteDecks(self, decks: list[str], cardsToo: bool = False) -> None:
        if not cardsToo:
            raise Exception(
                "Since Anki 2.1.28 it's not possible "
                "to delete decks without deleting cards as well"
            )
        self.startEditing()
        decks_to_delete = filter(lambda d: d in self.deckNames(), decks)
        for deck in decks_to_delete:
            did = self.decks().id(deck)
            self.decks().remove([did])

    @api()
    def getDeckConfig(self, deck: str) -> Any:
        if deck not in self.deckNames():
            return False

        collection = self.collection()
        did = collection.decks.id(deck)
        return collection.decks.config_dict_for_deck_id(did)

    @api(changes=_DECK_CONFIG)
    def saveDeckConfig(self, config: dict[str, Any]) -> bool:
        collection = self.collection()

        config["id"] = str(config["id"])
        config["mod"] = anki.utils.int_time()
        config["usn"] = collection.usn()
        if int(config["id"]) not in [c["id"] for c in collection.decks.all_config()]:
            return False
        try:
            collection.decks.save(config)
            collection.decks.update_config(config)
        except Exception:
            return False
        return True

    @api(changes=_DECK_CONFIG)
    def setDeckConfigId(self, decks: list[str], configId: Any) -> bool:
        configId = int(configId)
        for deck in decks:
            if deck not in self.deckNames():
                return False

        collection = self.collection()

        for deck in decks:
            try:
                did = collection.decks.id(deck)
                deck_dict = collection.decks.get(did)
                deck_dict["conf"] = configId
                collection.decks.save(deck_dict)
            except Exception:
                return False

        return True

    @api(changes=_DECK_CONFIG)
    def cloneDeckConfigId(self, name: str, cloneFrom: Any = "1") -> Any:
        configId = int(cloneFrom)
        collection = self.collection()
        if configId not in [c["id"] for c in collection.decks.all_config()]:
            return False

        config = collection.decks.get_config(configId)
        return collection.decks.add_config_returning_id(name, config)

    @api(changes=_DECK_CONFIG)
    def removeDeckConfigId(self, configId: Any) -> bool:
        collection = self.collection()
        if int(configId) not in [c["id"] for c in collection.decks.all_config()]:
            return False

        collection.decks.remove_config(configId)
        return True

    @api()
    def getDeckStats(self, decks: list[str]) -> dict[int, Any]:
        collection = self.collection()
        scheduler = self.scheduler()
        responseDict = {}
        deckIds = list(map(collection.decks.id, decks))

        allDeckNodes = self.collectDeckTreeChildren(scheduler.deck_due_tree())
        for deckId, deckNode in allDeckNodes.items():
            if deckId in deckIds:
                responseDict[deckId] = self.deckStatsToJson(deckNode)
        return responseDict

    #
    # Media
    #

    @api()
    def storeMediaFile(
        self,
        filename: str,
        data: str | None = None,
        path: str | None = None,
        url: str | None = None,
        skipHash: str | None = None,
        deleteExisting: bool = True,
    ) -> str | None:
        if not (data or path or url):
            raise Exception('You must provide a "data", "path", or "url" field.')
        if data:
            mediaData = base64.b64decode(data)
        elif path:
            with open(path, "rb") as f:
                mediaData = f.read()
        elif url:
            mediaData = self._download(url)

        if skipHash is None:
            skip = False
        else:
            m = hashlib.md5()
            m.update(mediaData)
            skip = skipHash == m.hexdigest()

        if skip:
            return None
        if deleteExisting:
            self.deleteMediaFile(filename)
        return self.media().write_data(os.path.basename(filename), mediaData)

    def _download(self, url: str) -> bytes:
        from anki.httpclient import HttpClient

        client = HttpClient()
        # seconds, as a float like the add-on's
        client.timeout = self._service.settings.web_timeout_ms / 1000  # type: ignore[assignment]
        resp = client.get(url)
        if resp.status_code != 200:
            raise Exception(
                "{} download failed with return code {}".format(url, resp.status_code)
            )
        return client.stream_content(resp)

    @api()
    def retrieveMediaFile(self, filename: str) -> Any:
        filename = os.path.basename(filename)
        filename = unicodedata.normalize("NFC", filename)
        filename = self.media()._legacy_strip_illegal(filename)

        path = os.path.join(self.media().dir(), filename)
        if os.path.exists(path):
            with open(path, "rb") as file:
                return base64.b64encode(file.read()).decode("ascii")

        return False

    @api()
    def getMediaFilesNames(self, pattern: str = "*") -> list[str]:
        path = os.path.join(self.media().dir(), pattern)
        return [os.path.basename(p) for p in glob.glob(path)]

    @api()
    def deleteMediaFile(self, filename: str) -> None:
        self.media().trash_files([filename])

    @api()
    def getMediaDirPath(self) -> str:
        return os.path.abspath(self.media().dir())

    #
    # Notes
    #

    @api(changes=_NOTES, preserve=_no_ids)
    def addNote(self, note: dict[str, Any]) -> int:
        self.startEditing()
        ankiNote = self.createNote(note)

        collection = self.collection()
        # the deck createNote checked (the add-on's legacy addNote took it
        # from the note type, where createNote had put it)
        collection.add_note(ankiNote, collection.decks.by_name(note["deckName"])["id"])
        nCardsAdded = len(ankiNote.cards())
        if nCardsAdded < 1:
            raise Exception(
                "The field values you have provided would make an empty question on all cards."
            )

        return ankiNote.id

    def addMediaFromNote(self, ankiNote: Note, note: Any) -> None:
        audioObjectOrList = note.get("audio")
        self.addMedia(ankiNote, audioObjectOrList, MediaType.Audio)

        videoObjectOrList = note.get("video")
        self.addMedia(ankiNote, videoObjectOrList, MediaType.Video)

        pictureObjectOrList = note.get("picture")
        self.addMedia(ankiNote, pictureObjectOrList, MediaType.Picture)

    def addMedia(self, ankiNote: Note, mediaObjectOrList: Any, mediaType: int) -> None:
        if mediaObjectOrList is None:
            return

        if isinstance(mediaObjectOrList, list):
            mediaList = mediaObjectOrList
        else:
            mediaList = [mediaObjectOrList]

        for media in mediaList:
            if media is not None:
                try:
                    mediaFilename = self.storeMediaFile(
                        media["filename"],
                        data=media.get("data"),
                        path=media.get("path"),
                        url=media.get("url"),
                        skipHash=media.get("skipHash"),
                        deleteExisting=media.get("deleteExisting"),
                    )

                    if (
                        mediaFilename is not None
                        and "fields" in media
                        and isinstance(media["fields"], list)
                    ):
                        for field_name in media["fields"]:
                            if field_name in ankiNote:
                                if mediaType is MediaType.Picture:
                                    ankiNote[field_name] += '<img src="{}">'.format(
                                        mediaFilename
                                    )
                                elif (
                                    mediaType is MediaType.Audio
                                    or mediaType is MediaType.Video
                                ):
                                    ankiNote[field_name] += "[sound:{}]".format(
                                        mediaFilename
                                    )

                except Exception as e:
                    errorMessage = (
                        str(e)
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                    )
                    for field_name in media["fields"]:
                        if field_name in ankiNote:
                            ankiNote[field_name] += errorMessage

    @api()
    def canAddNote(self, note: dict[str, Any]) -> bool:
        try:
            return bool(self.createNote(note))
        except Exception:
            return False

    @api()
    def canAddNoteWithErrorDetail(self, note: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"canAdd": bool(self.createNote(note))}
        except Exception as e:
            return {"canAdd": False, "error": str(e)}

    @api(changes=_NOTES, preserve=_note_id)
    def updateNoteFields(self, note: dict[str, Any]) -> None:
        ankiNote = self.getNote(note["id"])

        self.startEditing()
        for name, value in note["fields"].items():
            if name in ankiNote:
                ankiNote[name] = value

        self.addMediaFromNote(ankiNote, note)

        self.collection().update_note(ankiNote, skip_undo_entry=True)

    @api(changes=_NOTES, preserve=_note_id)
    def updateNote(self, note: dict[str, Any]) -> None:
        updated = False
        if "fields" in note.keys():
            self.updateNoteFields(note)
            updated = True
        if "tags" in note.keys():
            self.updateNoteTags(note["id"], note["tags"])
            updated = True
        if not updated:
            raise Exception('Must provide a "fields" or "tags" property.')

    @api(changes=_NOTETYPES)
    def updateNoteModel(self, note: dict[str, Any]) -> None:
        note_id = note.get("id")
        if not note_id:
            raise ValueError("Note ID is required")

        new_model_name = note.get("modelName")
        if not new_model_name:
            raise ValueError("Model name is required")

        new_fields = note.get("fields")
        if not new_fields or not isinstance(new_fields, dict):
            raise ValueError("Fields must be provided as a dictionary")

        new_tags = note.get("tags", [])

        anki_note = self.getNote(note_id)

        collection = self.collection()
        new_model = collection.models.by_name(new_model_name)
        if not new_model:
            raise ValueError(f"Model '{new_model_name}' not found")

        anki_note.mid = new_model["id"]
        anki_note._fmap = collection.models.field_map(new_model)
        anki_note.fields = [""] * len(new_model["flds"])

        for name, value in new_fields.items():
            for anki_name in anki_note.keys():
                if name.lower() == anki_name.lower():
                    anki_note[anki_name] = value
                    break

        anki_note.tags = new_tags

        collection.update_note(anki_note, skip_undo_entry=True)

    @api(changes=_TAGS, preserve=_note_id)
    def updateNoteTags(self, note: int, tags: Any) -> None:
        if isinstance(tags, str):
            tags = [tags]
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise Exception("Must provide tags as a list of strings")

        for old_tag in self.getNoteTags(note):
            self.removeTags([note], old_tag)
        for new_tag in tags:
            self.addTags([note], new_tag)

    @api()
    def getNoteTags(self, note: int) -> list[str]:
        return self.getNote(note).tags

    @api(changes=_TAGS, preserve=_note_ids)
    def addTags(self, notes: list[int], tags: str, add: bool = True) -> None:
        self.startEditing()
        if add:
            self.collection().tags.bulk_add(notes, tags)
        else:
            self.collection().tags.bulk_remove(notes, tags)

    @api(changes=_TAGS, preserve=_note_ids)
    def removeTags(self, notes: list[int], tags: str) -> None:
        return self.addTags(notes, tags, False)

    @api()
    def getTags(self) -> list[str]:
        return self.collection().tags.all()

    @api(changes=_TAGS, preserve=_no_ids)
    def clearUnusedTags(self) -> None:
        self.collection().tags.clear_unused_tags()

    @api(changes=_TAGS, preserve=_note_ids)
    def replaceTags(
        self, notes: list[int], tag_to_replace: str, replace_with_tag: str
    ) -> None:
        # the add-on showed a progress window and reset every screen; this
        # runs off the main thread and the screens refresh afterwards
        for nid in notes:
            try:
                note = self.getNote(nid)
            except NotFoundError:
                continue

            if note.has_tag(tag_to_replace):
                note.remove_tag(tag_to_replace)
                note.add_tag(replace_with_tag)
                self.collection().update_note(note, skip_undo_entry=True)

    @api(changes=_TAGS, preserve=_no_ids)
    def replaceTagsInAllNotes(self, tag_to_replace: str, replace_with_tag: str) -> None:
        collection = self.collection()
        for nid in collection.db.list("select id from notes"):
            note = self.getNote(nid)
            if note.has_tag(tag_to_replace):
                note.remove_tag(tag_to_replace)
                note.add_tag(replace_with_tag)
                self.collection().update_note(note, skip_undo_entry=True)

    @api(changes=_CARDS)
    def setEaseFactors(self, cards: list[int], easeFactors: list[int]) -> list[bool]:
        couldSetEaseFactors = []
        for i, card in enumerate(cards):
            try:
                ankiCard = self.getCard(card)
            except NotFoundError:
                couldSetEaseFactors.append(False)
                continue

            couldSetEaseFactors.append(True)
            ankiCard.factor = easeFactors[i]
            self.collection().update_card(ankiCard, skip_undo_entry=True)

        return couldSetEaseFactors

    @api(changes=_CARDS)
    def setSpecificValueOfCard(
        self, card: Any, keys: Any, newValues: Any, warning_check: bool = False
    ) -> Any:
        if isinstance(card, list):
            print("card has to be int, not list")
            return False

        if not isinstance(keys, list) or not isinstance(newValues, list):
            print("keys and newValues have to be lists.")
            return False

        if len(newValues) != len(keys):
            print("Invalid list lengths.")
            return False

        for key in keys:
            if key in [
                "did",
                "id",
                "ivl",
                "lapses",
                "left",
                "mod",
                "nid",
                "odid",
                "odue",
                "ord",
                "queue",
                "reps",
                "type",
                "usn",
            ]:
                if warning_check is False:
                    return False

        result: list[Any] = []
        try:
            ankiCard = self.getCard(card)
            for i, key in enumerate(keys):
                setattr(ankiCard, key, newValues[i])
            self.collection().update_card(ankiCard, skip_undo_entry=True)
            result.append(True)
        except Exception as e:
            result.append([False, str(e)])
        return result

    @api()
    def getEaseFactors(self, cards: list[int]) -> list[int | None]:
        # the card's stored ease factor; Clanki's algorithms keep it but do
        # not use it (spec ankiconnect.one-algorithm)
        easeFactors: list[int | None] = []
        for card in cards:
            try:
                ankiCard = self.getCard(card)
            except NotFoundError:
                easeFactors.append(None)
                continue

            easeFactors.append(ankiCard.factor)

        return easeFactors

    @api(changes=_CARDS, preserve=_card_ids)
    def suspend(self, cards: list[int], suspend: bool = True) -> bool:
        # the add-on removes from the list while iterating over it, which
        # skips the card after each one removed; kept as it is
        for card in cards:
            if self.suspended(card) == suspend:
                cards.remove(card)

        if len(cards) == 0:
            return False

        scheduler = self.scheduler()
        self.startEditing()
        if suspend:
            scheduler.suspend_cards(cards)
        else:
            scheduler.unsuspend_cards(cards)

        return True

    @api(changes=_CARDS, preserve=_card_ids)
    def unsuspend(self, cards: list[int]) -> None:
        self.suspend(cards, False)

    @api()
    def suspended(self, card: int) -> bool:
        return self.getCard(card).queue == -1

    @api()
    def areSuspended(self, cards: list[int]) -> list[bool | None]:
        suspended: list[bool | None] = []
        for card in cards:
            try:
                suspended.append(self.suspended(card))
            except NotFoundError:
                suspended.append(None)

        return suspended

    @api()
    def areDue(self, cards: list[int]) -> list[bool | None]:
        """As the add-on, except that a review card of an RWKV-Instant
        preset gives null: RWKV-Instant decides from its scores when such a
        card is due, not from its stored due date (spec
        ankiconnect.one-algorithm)."""
        due: list[bool | None] = []
        for card in cards:
            if self.findCards("cid:{} is:new".format(card)):
                due.append(True)
            else:
                date, ivl = self.collection().db.all(
                    "select id/1000.0, ivl from revlog where cid = ?", card
                )[-1]
                if ivl >= -1200:
                    ankiCard = self.getCard(card)
                    if (
                        ankiCard.queue == 2
                        and self._card_algorithm(ankiCard) == "rwkvInstant"
                    ):
                        due.append(None)
                    else:
                        due.append(bool(self.findCards("cid:{} is:due".format(card))))
                else:
                    due.append(date - ivl <= time.time())

        return due

    @api()
    def getIntervals(self, cards: list[int], complete: bool = False) -> list[Any]:
        intervals: list[Any] = []
        for card in cards:
            if self.findCards("cid:{} is:new".format(card)):
                intervals.append(0)
            else:
                interval = self.collection().db.list(
                    "select ivl from revlog where cid = ?", card
                )
                if not complete:
                    interval = interval[-1]
                intervals.append(interval)

        return intervals

    @api()
    def modelNames(self) -> list[str]:
        return [n.name for n in self.collection().models.all_names_and_ids()]

    @api(changes=_NOTETYPES)
    def createModel(
        self,
        modelName: str,
        inOrderFields: list[str],
        cardTemplates: list[dict[str, str]],
        css: str | None = None,
        isCloze: bool = False,
    ) -> Any:
        if len(inOrderFields) == 0:
            raise Exception("Must provide at least one field for inOrderFields")
        if len(cardTemplates) == 0:
            raise Exception("Must provide at least one card for cardTemplates")
        if modelName in [n.name for n in self.collection().models.all_names_and_ids()]:
            raise Exception("Model name already exists")

        collection = self.collection()
        mm = collection.models

        m = mm.new(modelName)
        if isCloze:
            m["type"] = MODEL_CLOZE

        for field_name in inOrderFields:
            fm = mm.new_field(field_name)
            mm.add_field(m, fm)

        if css is not None:
            m["css"] = css

        cardCount = 1
        for card in cardTemplates:
            cardName = "Card " + str(cardCount)
            if "Name" in card:
                cardName = card["Name"]

            t = mm.new_template(cardName)
            cardCount += 1
            t["qfmt"] = card["Front"]
            t["afmt"] = card["Back"]
            mm.add_template(m, t)

        mm.add(m)
        return m

    @api()
    def modelNamesAndIds(self) -> dict[str, int]:
        models = {}
        for model in self.modelNames():
            models[model] = int(self.collection().models.by_name(model)["id"])

        return models

    @api()
    def findModelsById(self, modelIds: list[int]) -> list[Any]:
        models = []
        for id in modelIds:
            model = self.collection().models.get(id)
            if model is None:
                raise Exception("model was not found: {}".format(id))
            else:
                models.append(model)
        return models

    @api()
    def findModelsByName(self, modelNames: list[str]) -> list[Any]:
        models = []
        for name in modelNames:
            model = self.collection().models.by_name(name)
            if model is None:
                raise Exception("model was not found: {}".format(name))
            else:
                models.append(model)
        return models

    @api()
    def modelNameFromId(self, modelId: int) -> str:
        model = self.collection().models.get(modelId)
        if model is None:
            raise Exception("model was not found: {}".format(modelId))
        else:
            return model["name"]

    @api()
    def modelFieldNames(self, modelName: str) -> list[str]:
        model = self.collection().models.by_name(modelName)
        if model is None:
            raise Exception("model was not found: {}".format(modelName))
        else:
            return [field["name"] for field in model["flds"]]

    @api()
    def modelFieldDescriptions(self, modelName: str) -> list[str]:
        model = self.collection().models.by_name(modelName)
        if model is None:
            raise Exception("model was not found: {}".format(modelName))
        else:
            try:
                return [field["description"] for field in model["flds"]]
            except KeyError:
                return ["" for field in model["flds"]]

    @api()
    def modelFieldFonts(self, modelName: str) -> dict[str, Any]:
        model = self.getModel(modelName)

        fonts = {}
        for field_dict in model["flds"]:
            fonts[field_dict["name"]] = {
                "font": field_dict["font"],
                "size": field_dict["size"],
            }

        return fonts

    @api()
    def modelFieldsOnTemplates(self, modelName: str) -> dict[str, Any]:
        model = self.collection().models.by_name(modelName)
        if model is None:
            raise Exception("model was not found: {}".format(modelName))

        templates = {}
        for template in model["tmpls"]:
            fields: list[list[str]] = []
            for side in ["qfmt", "afmt"]:
                fieldsForSide = []

                # based on _fieldsOnTemplate from aqt/clayout.py
                matches = re.findall("{{[^#/}]+?}}", template[side])
                for match in matches:
                    # remove braces and modifiers
                    match = re.sub(r"[{}]", "", match)
                    match = match.split(":")[-1]

                    # the answer side leaves out the question side's fields
                    # and FrontSide
                    if match == "FrontSide" or side == "afmt" and match in fields[0]:
                        continue
                    fieldsForSide.append(match)

                fields.append(fieldsForSide)

            templates[template["name"]] = fields

        return templates

    @api()
    def modelTemplates(self, modelName: str) -> dict[str, Any]:
        model = self.collection().models.by_name(modelName)
        if model is None:
            raise Exception("model was not found: {}".format(modelName))

        templates = {}
        for template in model["tmpls"]:
            templates[template["name"]] = {
                "Front": template["qfmt"],
                "Back": template["afmt"],
            }

        return templates

    @api()
    def modelStyling(self, modelName: str) -> dict[str, str]:
        model = self.collection().models.by_name(modelName)
        if model is None:
            raise Exception("model was not found: {}".format(modelName))

        return {"css": model["css"]}

    @api(changes=_NOTETYPES)
    def updateModelTemplates(self, model: dict[str, Any]) -> None:
        models = self.collection().models
        ankiModel = models.by_name(model["name"])
        if ankiModel is None:
            raise Exception("model was not found: {}".format(model["name"]))

        templates = model["templates"]
        for ankiTemplate in ankiModel["tmpls"]:
            template = templates.get(ankiTemplate["name"])
            if template:
                qfmt = template.get("Front")
                if qfmt:
                    ankiTemplate["qfmt"] = qfmt

                afmt = template.get("Back")
                if afmt:
                    ankiTemplate["afmt"] = afmt

        self.save_model(models, ankiModel)

    @api(changes=_NOTETYPES)
    def updateModelStyling(self, model: dict[str, Any]) -> None:
        models = self.collection().models
        ankiModel = models.by_name(model["name"])
        if ankiModel is None:
            raise Exception("model was not found: {}".format(model["name"]))

        ankiModel["css"] = model["css"]

        self.save_model(models, ankiModel)

    @api(changes=_NOTETYPES)
    def findAndReplaceInModels(
        self,
        modelName: str,
        findText: str,
        replaceText: str,
        front: bool = True,
        back: bool = True,
        css: bool = True,
    ) -> int:
        if not modelName:
            ankiModel = self.collection().models.all_names()
        else:
            model = self.collection().models.by_name(modelName)
            if model is None:
                raise Exception("model was not found: {}".format(modelName))
            ankiModel = [modelName]
        updatedModels = 0
        for model_name in ankiModel:
            model = self.collection().models.by_name(model_name)
            checkForText = False
            if css and findText in model["css"]:
                checkForText = True
                model["css"] = model["css"].replace(findText, replaceText)
            for tmpls in model.get("tmpls"):
                if front and findText in tmpls["qfmt"]:
                    checkForText = True
                    tmpls["qfmt"] = tmpls["qfmt"].replace(findText, replaceText)
                if back and findText in tmpls["afmt"]:
                    checkForText = True
                    tmpls["afmt"] = tmpls["afmt"].replace(findText, replaceText)
            self.save_model(self.collection().models, model)
            if checkForText:
                updatedModels += 1
        return updatedModels

    @api(changes=_NOTETYPES)
    def modelTemplateRename(
        self, modelName: str, oldTemplateName: str, newTemplateName: str
    ) -> None:
        mm = self.collection().models
        model = self.getModel(modelName)
        ankiTemplate = self.getTemplate(model, oldTemplateName)

        ankiTemplate["name"] = newTemplateName
        self.save_model(mm, model)

    @api(changes=_NOTETYPES)
    def modelTemplateReposition(
        self, modelName: str, templateName: str, index: int
    ) -> None:
        mm = self.collection().models
        model = self.getModel(modelName)
        ankiTemplate = self.getTemplate(model, templateName)

        mm.reposition_template(model, ankiTemplate, index)
        self.save_model(mm, model)

    @api(changes=_NOTETYPES)
    def modelTemplateAdd(self, modelName: str, template: dict[str, str]) -> None:
        mm = self.collection().models
        model = self.getModel(modelName)
        name = template["Name"]
        qfmt = template["Front"]
        afmt = template["Back"]

        # updates the template if it already exists (without saving it, as
        # the add-on does)
        for ankiTemplate in model["tmpls"]:
            if ankiTemplate["name"] == name:
                ankiTemplate["qfmt"] = qfmt
                ankiTemplate["afmt"] = afmt
                return

        ankiTemplate = mm.new_template(name)
        ankiTemplate["qfmt"] = qfmt
        ankiTemplate["afmt"] = afmt
        mm.add_template(model, ankiTemplate)

        self.save_model(mm, model)

    @api(changes=_NOTETYPES)
    def modelTemplateRemove(self, modelName: str, templateName: str) -> None:
        mm = self.collection().models
        model = self.getModel(modelName)
        ankiTemplate = self.getTemplate(model, templateName)

        mm.remove_template(model, ankiTemplate)
        self.save_model(mm, model)

    @api(changes=_NOTETYPES)
    def modelFieldRename(
        self, modelName: str, oldFieldName: str, newFieldName: str
    ) -> None:
        mm = self.collection().models
        model = self.getModel(modelName)
        field_dict = self.getField(model, oldFieldName)

        mm.rename_field(model, field_dict, newFieldName)

        self.save_model(mm, model)

    @api(changes=_NOTETYPES)
    def modelFieldReposition(self, modelName: str, fieldName: str, index: int) -> None:
        mm = self.collection().models
        model = self.getModel(modelName)
        field_dict = self.getField(model, fieldName)

        mm.reposition_field(model, field_dict, index)

        self.save_model(mm, model)

    @api(changes=_NOTETYPES)
    def modelFieldAdd(
        self, modelName: str, fieldName: str, index: int | None = None
    ) -> None:
        mm = self.collection().models
        model = self.getModel(modelName)

        # only adds the field if it doesn't already exist
        fieldMap = mm.field_map(model)
        if fieldName not in fieldMap:
            field_dict = mm.new_field(fieldName)
            mm.add_field(model, field_dict)

        # repositions, even if the field already exists
        if index is not None:
            fieldMap = mm.field_map(model)
            newField = fieldMap[fieldName][1]
            mm.reposition_field(model, newField, index)

        self.save_model(mm, model)

    @api(changes=_NOTETYPES)
    def modelFieldRemove(self, modelName: str, fieldName: str) -> None:
        mm = self.collection().models
        model = self.getModel(modelName)
        field_dict = self.getField(model, fieldName)

        mm.remove_field(model, field_dict)

        self.save_model(mm, model)

    @api(changes=_NOTETYPES)
    def modelFieldSetFont(self, modelName: str, fieldName: str, font: str) -> None:
        mm = self.collection().models
        model = self.getModel(modelName)
        field_dict = self.getField(model, fieldName)

        if not isinstance(font, str):
            raise Exception("font should be a string: {}".format(font))

        field_dict["font"] = font

        self.save_model(mm, model)

    @api(changes=_NOTETYPES)
    def modelFieldSetFontSize(
        self, modelName: str, fieldName: str, fontSize: int
    ) -> None:
        mm = self.collection().models
        model = self.getModel(modelName)
        field_dict = self.getField(model, fieldName)

        if not isinstance(fontSize, int):
            raise Exception("fontSize should be an integer: {}".format(fontSize))

        field_dict["size"] = fontSize

        self.save_model(mm, model)

    @api(changes=_NOTETYPES)
    def modelFieldSetDescription(
        self, modelName: str, fieldName: str, description: str
    ) -> bool:
        mm = self.collection().models
        model = self.getModel(modelName)
        field_dict = self.getField(model, fieldName)

        if not isinstance(description, str):
            raise Exception("description should be a string: {}".format(description))

        if "description" in field_dict:
            field_dict["description"] = description
            self.save_model(mm, model)
            return True
        return False

    @api()
    def deckNameFromId(self, deckId: int) -> str:
        deck = self.collection().decks.get(deckId)
        if deck is None:
            raise Exception("deck was not found: {}".format(deckId))

        return deck["name"]

    @api()
    def findNotes(self, query: str | None = None) -> list[int]:
        if query is None:
            return []

        self._prepare_search(query)
        return list(map(int, self.collection().find_notes(query)))

    @api()
    def findCards(
        self,
        query: str | None = None,
        fields: list[str] | None = None,
        noteFields: list[str] | None = None,
    ) -> list[Any]:
        if query is None:
            return []

        self._prepare_search(query)
        card_ids = list(map(int, self.collection().find_cards(query)))
        if fields is None and noteFields is None:
            return card_ids

        return self.cardsDetails(card_ids, fields, noteFields)

    @api()
    def cardsDetails(
        self,
        cards: list[int] | None,
        fields: list[str] | None = None,
        noteFields: list[str] | None = None,
    ) -> list[Any]:
        if cards is None:
            return []
        if not isinstance(cards, list):
            raise Exception("cards should be a list: {}".format(cards))
        card_ids = list(map(int, cards))

        requested_fields = fields
        requested_note_fields = noteFields

        if requested_fields is None and requested_note_fields is None:
            return card_ids

        if requested_fields is not None and not isinstance(requested_fields, list):
            raise Exception("fields should be a list: {}".format(requested_fields))

        if requested_note_fields is not None and not isinstance(
            requested_note_fields, list
        ):
            raise Exception(
                "noteFields should be a list: {}".format(requested_note_fields)
            )
        requested_note_fields_set = (
            set(requested_note_fields) if requested_note_fields is not None else None
        )

        for field_name in requested_fields or []:
            if field_name not in [
                "prop:r",
                "prop:s",
                "prop:d",
                "due",
                "queue",
                "type",
                "interval",
                "reps",
            ]:
                raise Exception("unsupported field requested: {}".format(field_name))

        result = []
        for cid in card_ids:
            card = self.getCard(cid)
            card_result: dict[str, Any] = {"cardId": card.id}

            if requested_note_fields is not None:
                note = card.note()
                model = card.note_type()
                note_fields = {}
                for info in model["flds"]:
                    order = info["ord"]
                    name = info["name"]
                    if (
                        requested_note_fields_set is not None
                        and name not in requested_note_fields_set
                    ):
                        continue
                    note_fields[name] = {"value": note.fields[order], "order": order}
                card_result["fields"] = note_fields

            if requested_fields:
                props = [f for f in requested_fields if f.startswith("prop:")]
                prop_values = (
                    self._prop_values(card, props, d_is_difficulty=False)
                    if props
                    else {}
                )
                for field_name in requested_fields:
                    if field_name in prop_values:
                        card_result[field_name] = prop_values[field_name]
                    elif field_name == "due":
                        card_result[field_name] = card.due
                    elif field_name == "queue":
                        card_result[field_name] = card.queue
                    elif field_name == "type":
                        card_result[field_name] = card.type
                    elif field_name == "interval":
                        card_result[field_name] = card.ivl
                    elif field_name == "reps":
                        card_result[field_name] = card.reps
            result.append(card_result)

        return result

    @api()
    def cardsInfo(
        self,
        cards: list[int],
        fields: list[str] | None = None,
        noteFields: list[str] | None = None,
        retrieved_info_mode: str = "ALL",
    ) -> list[Any]:
        requested_fields = fields
        if requested_fields is not None:
            if not isinstance(requested_fields, list):
                raise Exception("fields should be a list: {}".format(requested_fields))

            for field_name in requested_fields:
                if field_name not in ["prop:r", "prop:s", "prop:d"]:
                    raise Exception(
                        "unsupported field requested: {}".format(field_name)
                    )

        requested_note_fields = noteFields
        if requested_note_fields is not None and not isinstance(
            requested_note_fields, list
        ):
            raise Exception(
                "noteFields should be a list: {}".format(requested_note_fields)
            )
        requested_note_fields_set = (
            set(requested_note_fields) if requested_note_fields is not None else None
        )

        if not isinstance(retrieved_info_mode, str):
            raise Exception(
                "retrieved_info_mode should be a string: {}".format(retrieved_info_mode)
            )
        info_mode = retrieved_info_mode.upper()
        if info_mode not in ["ALL", "COMPACT", "FIELDS_ONLY"]:
            raise Exception("invalid retrieved_info_mode: {}".format(info_mode))

        result: list[Any] = []
        for cid in cards:
            try:
                card = self.getCard(cid)
                model = card.note_type()
                note = card.note()
                note_fields = {}
                for info in model["flds"]:
                    order = info["ord"]
                    name = info["name"]
                    if (
                        requested_note_fields_set is not None
                        and name not in requested_note_fields_set
                    ):
                        continue
                    note_fields[name] = {"value": note.fields[order], "order": order}

                card_result: dict[str, Any]
                if info_mode == "ALL":
                    # the add-on's keys, in its order
                    card_result = {
                        "cardId": card.id,
                        "fields": note_fields,
                        "fieldOrder": card.ord,
                        "question": card.question(),
                        "answer": card.answer(),
                        "modelName": model["name"],
                        "ord": card.ord,
                        "deckName": self.deckNameFromId(card.did),
                        "css": model["css"],
                        # 10 times the ease percentage: 310% is 3100
                        "factor": card.factor,
                        "interval": card.ivl,
                        "note": card.nid,
                        "type": card.type,
                        "queue": card.queue,
                        "due": card.due,
                        "reps": card.reps,
                        "lapses": card.lapses,
                        "left": card.left,
                        "mod": card.mod,
                        "nextReviews": self._next_reviews(card),
                        "flags": card.flags,
                    }
                elif info_mode == "COMPACT":
                    card_result = {
                        "cardId": card.id,
                        "fields": note_fields,
                        "fieldOrder": card.ord,
                        "ord": card.ord,
                        "deckName": self.deckNameFromId(card.did),
                        "factor": card.factor,
                        "interval": card.ivl,
                        "note": card.nid,
                        "type": card.type,
                        "queue": card.queue,
                        "due": card.due,
                        "reps": card.reps,
                        "lapses": card.lapses,
                        "mod": card.mod,
                        "flags": card.flags,
                    }
                else:
                    card_result = {"cardId": card.id, "fields": note_fields}

                if requested_fields:
                    card_result.update(
                        self._prop_values(card, requested_fields, d_is_difficulty=True)
                    )

                result.append(card_result)
            except NotFoundError:
                # keep the input and the result lists aligned
                result.append({})

        return result

    @api()
    def cardsModTime(self, cards: list[int]) -> list[Any]:
        result: list[Any] = []
        for cid in cards:
            try:
                card = self.getCard(cid)
                result.append({"cardId": card.id, "mod": card.mod})
            except NotFoundError:
                result.append({})
        return result

    @api(changes=_CARDS, preserve=_card_ids)
    def forgetCards(self, cards: list[int]) -> None:
        from anki.scheduler.base import ScheduleCardsAsNew

        self.startEditing()
        request = ScheduleCardsAsNew(
            card_ids=cards,
            log=True,
            restore_position=True,
            reset_counts=False,
            context=None,
        )
        self.collection()._backend.schedule_cards_as_new(request)

    @api(changes=_CARDS)
    def relearnCards(self, cards: list[int]) -> None:
        self.startEditing()
        scids = anki.utils.ids2str(cards)
        self.collection().db.execute(
            "update cards set type=3, queue=1 where id in " + scids
        )

    def _grade_now(self, cards: Sequence[int], ease: int) -> GradeNowResult:
        """The Browser's Grade Now, without the GUI: every algorithm's cards
        are answered the way the reviewer answers them (spec
        sched.grade-now-rwkv-curve). Blocks while RWKV-Curve predicts, which
        is why no action that calls it runs on the main thread."""
        from aqt.operations.scheduling import grade_cards_now

        return grade_cards_now(
            self.collection(), cards, ease, reviewer=self._algorithm_reviewer()
        )

    @api(changes=_CARDS)
    def gradeNow(self, cards: list[int], ease: int) -> bool:
        """As the fork, for every algorithm: each card is answered as the
        reviewer answers it. A card RWKV-Curve has no intervals for is not
        graded, and the action fails naming it (spec
        ankiconnect.one-algorithm)."""
        if ease < 1 or ease > 4:
            raise Exception("ease must be between 1 and 4")

        result = self._grade_now(cards, ease)
        if result.unanswered_card_ids:
            ids = ", ".join(str(cid) for cid in result.unanswered_card_ids)
            raise Exception(
                f"gradeNow: RWKV-Curve has no intervals yet for card(s) {ids}, "
                "so they were not graded; the other cards were graded"
            )
        return True

    @api(changes=_CARDS)
    def answerCards(self, answers: list[dict[str, Any]]) -> list[bool]:
        """As the add-on. A card RWKV-Curve schedules is answered with
        RWKV-Curve's intervals, as the reviewer answers it, and gives false
        while RWKV-Curve has no intervals for it (spec
        ankiconnect.one-algorithm)."""
        from aqt import rwkv_scheduler

        scheduler = self.scheduler()
        card_ids = [a.get("cardId") for a in answers if isinstance(a, Mapping)]
        reconciliation = rwkv_scheduler.prepare_grade_now_reconciliation(
            self._algorithm_reviewer(), card_ids
        )
        success: list[bool] = []
        # RWKV-Curve's cards wait until this reconciliation is recorded: Grade
        # Now makes one of its own, and two over the same new review-log rows
        # would force a full RWKV state rebuild. {ease: [(index, card id)]}
        curve_cards: dict[int, list[tuple[int, int]]] = {}
        try:
            for answer in answers:
                try:
                    cid = answer["cardId"]
                    ease = answer["ease"]
                    card = self.getCard(cid)
                    if self._card_algorithm(card) == "rwkvCurve":
                        if ease not in (1, 2, 3, 4):
                            raise Exception("invalid ease")
                        curve_cards.setdefault(ease, []).append((len(success), cid))
                        success.append(False)
                        continue
                    card.start_timer()
                    scheduler.answerCard(card, ease)
                    success.append(True)
                except NotFoundError:
                    success.append(False)
        finally:
            rwkv_scheduler.record_grade_now_answers(reconciliation)

        for ease, graded in curve_cards.items():
            answered = self._grade_now(
                [cid for _, cid in graded], ease
            ).answered_card_ids
            for index, cid in graded:
                success[index] = cid in answered

        return success

    @api()
    def cardReviews(self, deck: str, startID: int) -> list[Any]:
        return self.database().all(
            "select id, cid, usn, ease, ivl, lastIvl, factor, time, type from revlog "
            "where id>? and cid in (select id from cards where did=?)",
            startID,
            self.decks().id(deck),
        )

    @api()
    def getReviewsOfCards(self, cards: list[int]) -> dict[int, Any]:
        COLUMNS = [
            "cid",
            "id",
            "usn",
            "ease",
            "ivl",
            "lastIvl",
            "factor",
            "time",
            "type",
        ]

        cid_to_reviews: dict[int, list[Any]] = {}
        # 999 is the maximum number of variables sqlite allows
        for cid_batch in _batched(cards, 999):
            placeholders = ",".join("?" * len(cid_batch))

            cid_reviews = self.collection().db.all(
                "select {} from revlog where cid in ({})".format(
                    ", ".join(COLUMNS), placeholders
                ),
                *cid_batch,
            )
            for cid_review in cid_reviews:
                cid = cid_review[0]
                reviews = cid_to_reviews.get(cid, [])
                reviews.append(cid_review[1:])
                cid_to_reviews[cid] = reviews

        result = {}
        for card in cards:
            result[card] = [
                dict(zip(COLUMNS[1:], review))
                for review in cid_to_reviews.get(card, [])
            ]

        return result

    @api(changes=_CARDS, preserve=_card_ids)
    def setDueDate(self, cards: list[int], days: str) -> bool:
        self.scheduler().set_due_date(cards, days, config_key=None)
        return True

    @api(changes=_CARDS, preserve=lambda p: {"card_ids": _ids(p.get("orderedCardIds"))})
    def repositionNewCards(
        self, orderedCardIds: Any, startPosition: Any, step: Any, shift: Any
    ) -> dict[str, Any]:
        if not isinstance(orderedCardIds, list) or len(orderedCardIds) == 0:
            raise Exception(
                "orderedCardIds should be a non-empty list: {}".format(orderedCardIds)
            )
        if not all(
            isinstance(card_id, int) and not isinstance(card_id, bool)
            for card_id in orderedCardIds
        ):
            raise Exception(
                "orderedCardIds should contain only integers: {}".format(orderedCardIds)
            )
        if (
            not isinstance(startPosition, int)
            or isinstance(startPosition, bool)
            or startPosition < 1
        ):
            raise Exception(
                "startPosition should be an integer >= 1: {}".format(startPosition)
            )
        if not isinstance(step, int) or isinstance(step, bool) or step < 1:
            raise Exception("step should be an integer >= 1: {}".format(step))
        if not isinstance(shift, bool):
            raise Exception("shift should be a boolean: {}".format(shift))

        deduped_card_ids = []
        seen_card_ids = set()
        for card_id in orderedCardIds:
            if card_id in seen_card_ids:
                continue
            seen_card_ids.add(card_id)
            deduped_card_ids.append(card_id)

        eligible_new_cards = []
        skipped_not_found = []
        skipped_not_new = []

        for card_id in deduped_card_ids:
            try:
                self.getCard(card_id)
            except NotFoundError:
                skipped_not_found.append(card_id)
                continue

            if self.findCards("cid:{} is:new".format(card_id)):
                eligible_new_cards.append(card_id)
            else:
                skipped_not_new.append(card_id)

        if len(eligible_new_cards) > 0:
            self.collection()._backend.sort_cards(
                card_ids=eligible_new_cards,
                starting_from=startPosition,
                step_size=step,
                randomize=False,
                shift_existing=shift,
            )

        return {
            "requested": len(orderedCardIds),
            "deduped": len(deduped_card_ids),
            "eligibleNew": len(eligible_new_cards),
            "repositioned": len(eligible_new_cards),
            "skippedNotFound": skipped_not_found,
            "skippedNotNew": skipped_not_new,
            "appliedStartPosition": startPosition,
            "appliedStep": step,
            "appliedShift": shift,
        }

    @api()
    def reloadCollection(self) -> None:
        # the add-on's col.reset() has done nothing since Anki 2.1.50; only
        # its "collection is not available" error is left
        self.collection()

    @api()
    def getLatestReviewID(self, deck: str) -> int:
        return (
            self.database().scalar(
                "select max(id) from revlog where cid in (select id from cards where did=?)",
                self.decks().id(deck),
            )
            or 0
        )

    @api(changes=_CARDS)
    def insertReviews(self, reviews: list[Any]) -> None:
        if len(reviews) > 0:
            sql = "insert into revlog(id,cid,usn,ease,ivl,lastIvl,factor,time,type) values "
            for row in reviews:
                sql += "(%s)," % ",".join(map(str, row))
            sql = sql[:-1]
            self.database().execute(sql)

    @api()
    def notesInfo(
        self, notes: list[int] | None = None, query: str | None = None
    ) -> list[Any]:
        if notes is None and query is None:
            raise Exception('Must provide either "notes" or a "query"')

        if query is not None:
            notes = self.findNotes(query)
        assert notes is not None

        nid_to_card_ids: dict[int, list[int]] = {}
        # 999 is the maximum number of variables sqlite allows
        for nid_batch in _batched(notes, 999):
            placeholders = ",".join("?" * len(nid_batch))

            cid_and_nids = self.collection().db.all(
                "select id, nid from cards where nid in ({}) order by ord".format(
                    placeholders
                ),
                *nid_batch,
            )
            for cid, nid in cid_and_nids:
                card_ids = nid_to_card_ids.get(nid, [])
                card_ids.append(cid)
                nid_to_card_ids[nid] = card_ids

        result: list[Any] = []
        for nid in notes:
            try:
                note = self.getNote(nid)
                model = note.note_type()

                fields = {}
                for info in model["flds"]:
                    order = info["ord"]
                    name = info["name"]
                    fields[name] = {"value": note.fields[order], "order": order}

                result.append(
                    {
                        "noteId": note.id,
                        "profile": self.window().pm.name,
                        "tags": note.tags,
                        "fields": fields,
                        "modelName": model["name"],
                        "mod": note.mod,
                        "cards": nid_to_card_ids[nid],
                    }
                )
            except NotFoundError:
                result.append({})

        return result

    @api()
    def notesModTime(self, notes: list[int]) -> list[Any]:
        result: list[Any] = []
        for nid in notes:
            try:
                note = self.getNote(nid)
                result.append({"noteId": note.id, "mod": note.mod})
            except NotFoundError:
                result.append({})
        return result

    @api(changes=_NOTES, preserve=_note_ids)
    def deleteNotes(self, notes: list[int]) -> None:
        self.collection().remove_notes(notes)

    @api(changes=_NOTETYPES)
    def removeEmptyNotes(self) -> None:
        # despite its name, the add-on's action removes the note types that
        # have no notes; kept as it is
        for model in self.collection().models.all():
            if self.collection().models.use_count(model) == 0:
                self.collection().models.remove(model["id"])

    @api()
    def cardsToNotes(self, cards: list[int]) -> list[int]:
        return self.collection().db.list(
            "select distinct nid from cards where id in " + anki.utils.ids2str(cards)
        )

    #
    # GUI (the windows are the main thread's; the searches are not)
    #

    @api()
    def guiBrowse(
        self, query: str | None = None, reorderCards: dict[str, Any] | None = None
    ) -> list[Any]:
        def open_browser() -> None:
            import aqt
            from aqt.qt import Qt

            browser = aqt.dialogs.open("Browser", self.window())
            browser.activateWindow()

            if query is not None:
                browser.form.searchEdit.lineEdit().setText(query)
                if hasattr(browser, "onSearch"):
                    browser.onSearch()
                else:
                    browser.onSearchActivated()

            if reorderCards is not None:
                if not isinstance(reorderCards, dict):
                    raise Exception(
                        "reorderCards should be a dict: {}".format(reorderCards)
                    )
                if not ("columnId" in reorderCards and "order" in reorderCards):
                    raise Exception('Must provide a "columnId" and a "order" property"')

                cardOrder = reorderCards["order"]
                if cardOrder not in ("ascending", "descending"):
                    raise Exception(
                        "invalid card order: {}".format(reorderCards["order"])
                    )

                sort_order = (
                    Qt.SortOrder.DescendingOrder
                    if cardOrder == "descending"
                    else Qt.SortOrder.AscendingOrder
                )
                columnId = browser.table._model.active_column_index(
                    reorderCards["columnId"]
                )
                if columnId is None:
                    raise Exception(
                        "invalid columnId: {}".format(reorderCards["columnId"])
                    )

                browser.table._on_sort_column_changed(columnId, sort_order)

        self._service.run_on_main(open_browser)
        return self.findCards(query)

    @api(main=True)
    def guiEditNote(self, note: int) -> None:
        from aqt.ankiconnect_edit import Edit

        Edit.open_dialog_and_show_note_with_id(note)

    @api(main=True)
    def guiSelectNote(self, note: int) -> bool:
        print(
            "guiSelectNote actually selects card IDs and is deprecated; use guiSelectCard"
        )
        return self.guiSelectCard(note)

    @api(main=True)
    def guiSelectCard(self, card: int) -> bool:
        import aqt

        (creator, instance) = aqt.dialogs._dialogs["Browser"]
        if instance is None:
            return False
        instance.table.clear_selection()
        instance.table.select_single_card(card)
        return True

    @api(main=True)
    def guiSelectedNotes(self) -> list[int]:
        import aqt

        (creator, instance) = aqt.dialogs._dialogs["Browser"]
        if instance is None:
            return []
        return list(instance.selectedNotes())

    @api(main=True)
    def guiAddCards(self, note: dict[str, Any] | None = None) -> int:
        import aqt

        if note is not None:
            collection = self.collection()

            deck = collection.decks.by_name(note["deckName"])
            if deck is None:
                raise Exception("deck was not found: {}".format(note["deckName"]))

            collection.decks.select(deck["id"])
            savedMid = deck.pop("mid", None)

            model = collection.models.by_name(note["modelName"])
            if model is None:
                raise Exception("model was not found: {}".format(note["modelName"]))

            collection.models.set_current(model)
            collection.models.update(model)

            ankiNote = Note(collection, model)

            # fill the note first, so its id is known
            if "fields" in note:
                for name, value in note["fields"].items():
                    if name in ankiNote:
                        ankiNote[name] = value

            self.addMediaFromNote(ankiNote, note)

            if "tags" in note:
                ankiNote.tags = note["tags"]

            def openNewWindow() -> None:
                addCards = aqt.dialogs.open("AddCards", self.window())

                if savedMid:
                    deck["mid"] = savedMid

                addCards.editor.set_note(ankiNote)

                addCards.activateWindow()

                aqt.dialogs.open("AddCards", self.window())
                addCards.setAndFocusNote(addCards.editor.note)

            currentWindow = aqt.dialogs._dialogs["AddCards"][1]

            if currentWindow is not None:
                currentWindow.closeWithCallback(openNewWindow)
            else:
                openNewWindow()

            return ankiNote.id

        else:
            addCards = aqt.dialogs.open("AddCards", self.window())
            addCards.activateWindow()

            return addCards.editor.note.id

    @api(main=True)
    def guiAddNoteSetData(self, note: dict[str, Any], append: bool = False) -> Any:
        """Set fields and media in the Add Note dialog if it is open (the
        fork's action). True on success, an error dict if it is not open."""
        import aqt

        addCards = aqt.dialogs._dialogs.get("AddCards", [None, None])[1]
        if addCards is None or not hasattr(addCards, "editor"):
            return {"error": "Add Note dialog is not open", "code": 1}
        collection = self.collection()
        if "deckName" in note:
            deck = collection.decks.by_name(note["deckName"])
            if deck is None:
                raise Exception(f'Deck "{note["deckName"]}" not found')
            addCards.set_deck(deck["id"])
        if "modelName" in note:
            model = collection.models.by_name(note["modelName"])
            if model is None:
                raise Exception(f'Model "{note["modelName"]}" not found')
            addCards.set_note_type(model["id"])

        editorNote = addCards.editor.note
        if "fields" in note:
            for name, value in note["fields"].items():
                if name not in editorNote:
                    raise Exception(f'Field "{name}" not found in current note')
                if append:
                    editorNote[name] = str(editorNote[name]) + str(value)
                else:
                    editorNote[name] = value
        if "tags" in note:
            if append:
                new_tags = (
                    note["tags"] if isinstance(note["tags"], list) else [note["tags"]]
                )
                editorNote.tags = list(set(editorNote.tags + new_tags))
            else:
                editorNote.tags = note["tags"]
        self.addMediaFromNote(editorNote, note)
        addCards.editor.loadNote()
        return True

    @api(main=True)
    def guiReviewActive(self) -> bool:
        return self.reviewer().card is not None and self.window().state == "review"

    @api(main=True)
    def guiCurrentCard(self) -> dict[str, Any]:
        if not self.guiReviewActive():
            raise Exception("Gui review is not currently active.")

        reviewer = self.reviewer()
        card = reviewer.card
        model = card.note_type()
        note = card.note()

        fields = {}
        for info in model["flds"]:
            order = info["ord"]
            name = info["name"]
            fields[name] = {"value": note.fields[order], "order": order}

        buttonList = reviewer._answerButtonList()
        return {
            "cardId": card.id,
            "fields": fields,
            "fieldOrder": card.ord,
            "question": card.question(),
            "answer": card.answer(),
            "buttons": [b[0] for b in buttonList],
            "nextReviews": self._current_card_next_reviews(reviewer, card, buttonList),
            "modelName": model["name"],
            "deckName": self.deckNameFromId(card.did),
            "css": model["css"],
            "template": card.template()["name"],
        }

    def _current_card_next_reviews(
        self, reviewer: Any, card: Card, buttonList: Sequence[Any]
    ) -> list[str]:
        """What the answer buttons show: FSRS-7's intervals; RWKV-Curve's
        once it has given them for this showing of the card (none before);
        none for RWKV-Instant."""
        from aqt import rwkv_scheduler

        algorithm = self._card_algorithm(card)
        if algorithm == "rwkvInstant":
            return ["" for _ in buttonList]
        if algorithm == "rwkvCurve":
            states = getattr(getattr(reviewer, "_v3", None), "states", None)
            if states is None or rwkv_scheduler.answer_intervals_pending(
                reviewer, card
            ):
                return ["" for _ in buttonList]
            labels = self.collection().sched.describe_next_states(states)
            return [labels[b[0] - 1] for b in buttonList]
        return [
            reviewer.mw.col.sched.nextIvlStr(reviewer.card, b[0], True)
            for b in buttonList
        ]

    @api(main=True)
    def guiStartCardTimer(self) -> bool:
        if not self.guiReviewActive():
            return False

        card = self.reviewer().card
        if card is not None:
            card.start_timer()
            return True

        return False

    @api(main=True)
    def guiShowQuestion(self) -> bool:
        if self.guiReviewActive():
            self.reviewer()._showQuestion()
            return True

        return False

    @api(main=True)
    def guiShowAnswer(self) -> bool:
        if self.guiReviewActive():
            self.window().reviewer._showAnswer()
            return True

        return False

    @api(main=True)
    def guiAnswerCard(self, ease: int) -> bool:
        from aqt import rwkv_scheduler

        if not self.guiReviewActive():
            return False

        reviewer = self.reviewer()
        if reviewer.state != "answer":
            return False
        if ease <= 0 or ease > self.scheduler().answerButtons(reviewer.card):
            return False
        # the reviewer ignores an answer while RWKV-Curve's intervals are
        # pending (spec sched.rwkv-curve-buttons-wait), so say so instead of
        # reporting an answer that did not happen
        if rwkv_scheduler.answer_intervals_pending(reviewer, reviewer.card):
            return False

        reviewer._answerCard(ease)
        return True

    @api(main=True)
    def guiUndo(self) -> bool:
        self.window().undo()
        return True

    @api(main=True)
    def guiDeckOverview(self, name: str) -> bool:
        collection = self.window().col
        if collection is not None:
            deck = collection.decks.by_name(name)
            if deck is not None:
                collection.decks.select(deck["id"])
                self.window().onOverview()
                return True

        return False

    @api(main=True)
    def guiDeckBrowser(self) -> None:
        self.window().moveToState("deckBrowser")

    @api(main=True)
    def guiDeckReview(self, name: str) -> bool:
        if self.guiDeckOverview(name):
            self.window().moveToState("review")
            return True

        return False

    @api(main=True)
    def guiImportFile(self, path: str | None = None) -> None:
        """Open the Import File dialog with the given file (forward slashes
        on Windows); without a path, the user picks one."""
        from aqt.import_export.importing import import_file, prompt_for_file_then_import
        from aqt.qt import Qt

        mw = self.window()
        on_top = Qt.WindowType.WindowStaysOnTopHint
        # bring the window up for the user to review the import settings
        try:
            mw.setWindowFlags(mw.windowFlags() | on_top)
            mw.show()
        finally:
            mw.setWindowFlags(mw.windowFlags() & ~on_top)
            mw.show()

        if path is None:
            prompt_for_file_then_import(mw)
        else:
            import_file(mw, path)

    @api(main=True)
    def guiExitAnki(self) -> None:
        from aqt.qt import QTimer

        # after the reply has gone out
        QTimer.singleShot(1000, self.window().close)

    @api(main=True)
    def guiCheckDatabase(self) -> bool:
        self.window().onCheckDB()
        return True

    @api(main=True)
    def guiPlayAudio(self) -> bool:
        if not self.guiReviewActive():
            return False

        self.reviewer().replayAudio()
        return True

    @api(changes=_NOTES, preserve=_no_ids)
    def addNotes(self, notes: list[dict[str, Any]]) -> list[int]:
        results = []
        errs = []

        for note in notes:
            try:
                results.append(self.addNote(note))
            except Exception as e:
                # every note is tried, so all the errors are reported
                errs.append(str(e))

        if errs:
            # nothing is added when a note fails
            self.deleteNotes(results)
            raise Exception(str(errs))

        return results

    @api()
    def canAddNotes(self, notes: list[dict[str, Any]]) -> list[bool]:
        results = []
        for note in notes:
            results.append(self.canAddNote(note))

        return results

    @api()
    def canAddNotesWithErrorDetail(self, notes: list[dict[str, Any]]) -> list[Any]:
        results = []
        for note in notes:
            results.append(self.canAddNoteWithErrorDetail(note))

        return results

    @api()
    def exportPackage(self, deck: str, path: str, includeSched: bool = False) -> bool:
        from anki.collection import DeckIdLimit, ExportAnkiPackageOptions

        collection = self.collection()
        if collection is not None:
            deck_dict = collection.decks.by_name(deck)
            if deck_dict is not None:
                collection.export_anki_package(
                    out_path=path,
                    limit=DeckIdLimit(deck_id=deck_dict["id"]),
                    options=ExportAnkiPackageOptions(
                        with_scheduling=includeSched,
                        with_deck_configs=includeSched,
                        with_media=True,
                        legacy=True,
                    ),
                )
                return True

        return False

    @api(changes=_EVERYTHING)
    def importPackage(self, path: str) -> bool:
        from anki.collection import ImportAnkiPackageOptions, ImportAnkiPackageRequest

        collection = self.collection()
        if collection is not None:
            self.startEditing()
            options = ImportAnkiPackageOptions(with_scheduling=True)
            options.with_deck_configs = True
            collection.import_anki_package(
                ImportAnkiPackageRequest(package_path=path, options=options)
            )
            return True

        return False

    @api()
    def apiReflect(self, scopes: Any = None, actions: Any = None) -> dict[str, Any]:
        if not isinstance(scopes, list):
            raise Exception("scopes has invalid value")
        if not (actions is None or isinstance(actions, list)):
            raise Exception("actions has invalid value")

        cls = type(self)
        scopes2: list[str] = []
        result: dict[str, Any] = {"scopes": scopes2}

        if "actions" in scopes:
            if actions is None:
                actions = dir(cls)

            methodNames = []
            for methodName in actions:
                method = getattr(cls, methodName, None)
                if method is not None and getattr(method, "api", False):
                    methodNames.append(methodName)

            scopes2.append("actions")
            result["actions"] = methodNames

        return result


def _key_matches(key: object, expected: str | None) -> bool:
    """The add-on's `key != apiKey`, compared in constant time for strings."""
    if isinstance(key, str) and isinstance(expected, str):
        return hmac.compare_digest(key.encode("utf-8"), expected.encode("utf-8"))
    return key == expected


def action_names() -> list[str]:
    return sorted(
        name
        for name in dir(AnkiConnect)
        if getattr(getattr(AnkiConnect, name), "api", False)
    )


# Service: settings, server, threads and refreshes
######################################################################


@dataclass
class _PendingChanges:
    names: set[str] = field(default_factory=set)
    scheduled: bool = False


class AnkiConnectService:
    """Owns the settings and the HTTP server; runs requests one at a time,
    in the order they arrive (as the add-on did on the main thread), each
    counted as collection use so the periodic backup waits for it."""

    # the screens refresh this long after the last change, so a burst of
    # requests (a script adding 100 notes) redraws them once
    REFRESH_DELAY_MS = 100

    def __init__(self, mw: AnkiQt, settings: AnkiConnectSettings | None = None) -> None:
        self.mw = mw
        self.settings = settings if settings is not None else load_settings(mw.pm)
        self.actions = AnkiConnect(self)
        self._server: WebServer | None = None
        self._lock = threading.Lock()
        self._closing = False
        self._pending = _PendingChanges()
        self._pending_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._log_file: Any = None
        self._port_warning_shown = False
        self.status = ServerStatus(ServerState.OFF)
        # tells an open Preferences tab of a new status
        self.status_listeners: list[Callable[[ServerStatus], None]] = []

    # lifecycle

    def start(self, listen_now: bool = False) -> None:
        """Listen, if on. The listen happens on the listener thread, or on
        this one with `listen_now` (raising when it fails; tests)."""
        if not self.settings.enabled or self._server is not None:
            return
        self._closing = False
        self._open_log()
        self._server = WebServer(
            self.settings.web_settings(), self.handle_request, self._on_status
        )
        if listen_now:
            self._server.listen_now()
        else:
            self._server.start()

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.stop()
        self._close_log()

    def shutdown(self) -> None:
        """At exit: stop listening; requests waiting for the main thread
        give up."""
        self._closing = True
        self.stop()

    def apply_settings(self, settings: AnkiConnectSettings) -> None:
        """Store new settings and apply them: a new address, port or on/off
        restarts the server; new origins or a new key apply to the next
        request."""
        old = self.settings
        self.settings = settings
        save_settings(self.mw.pm, settings)
        restart = (
            settings.enabled != old.enabled
            or settings.bind_address != old.bind_address
            or settings.bind_port != old.bind_port
            or settings.api_log_path != old.api_log_path
        )
        if restart:
            self._port_warning_shown = False
            self.stop()
            self.start()
        elif self._server is not None:
            self._server.update_settings(settings.web_settings())

    @property
    def port(self) -> int:
        return self._server.port if self._server is not None else 0

    def _on_status(self, status: ServerStatus) -> None:
        self.status = status
        if status.state == ServerState.LISTENING:
            self._port_warning_shown = False

        def notify() -> None:
            for listener in list(self.status_listeners):
                try:
                    listener(status)
                except Exception:
                    pass
            if (
                status.state in (ServerState.PORT_IN_USE, ServerState.ERROR)
                and not self._port_warning_shown
            ):
                self._port_warning_shown = True
                self._warn_listen_failed(status)

        taskman = getattr(self.mw, "taskman", None)
        if taskman is not None:
            taskman.run_on_main(notify)

    def _warn_listen_failed(self, status: ServerStatus) -> None:
        """Once per failure: a tooltip rather than the add-on's modal box,
        since Clanki keeps trying and the start-up is not held up."""
        from aqt.utils import tooltip, tr

        if status.state == ServerState.PORT_IN_USE:
            text = tr.preferences_ankiconnect_port_in_use(
                address=status.address, port=str(status.port)
            )
        else:
            text = tr.preferences_ankiconnect_status_error(
                address=status.address,
                port=str(status.port),
                error=status.error,
            )
        try:
            tooltip(text, period=8000, parent=self.mw)
        except Exception:
            print(text)

    # requests

    def handle_request(self, params: dict[str, Any]) -> Any:
        """A decoded request from the HTTP side (its connection thread)."""
        taskman = getattr(self.mw, "taskman", None)
        with self._lock:
            if taskman is not None:
                taskman.collection_use_started()
            try:
                return self.actions.handler(params)
            finally:
                if taskman is not None:
                    taskman.collection_use_finished()

    def run_on_main(self, fn: Callable[[], _T]) -> _T:
        """Run `fn` on the main thread and wait for its result."""
        if threading.current_thread() is threading.main_thread():
            return fn()
        done = threading.Event()
        outcome: dict[str, Any] = {}

        def run() -> None:
            try:
                outcome["result"] = fn()
            except BaseException as e:
                outcome["error"] = e
            finally:
                done.set()

        self.mw.taskman.run_on_main(run)
        while not done.wait(0.5):
            if self._closing:
                raise Exception("Clanki is closing")
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]

    def run_collection_task(self, fn: Callable[[], _T]) -> _T:
        """Run `fn` as a background collection task (serialized with the
        GUI's own, such as its sync) and wait for its result."""
        holder: dict[str, Any] = {}

        def submit() -> None:
            holder["future"] = self.mw.taskman.run_in_background(
                fn, uses_collection=True
            )

        self.run_on_main(submit)
        return holder["future"].result()

    # refreshing the screens

    def note_changes(self, names: Iterable[str]) -> None:
        with self._pending_lock:
            self._pending.names.update(names)
            if self._pending.scheduled:
                return
            self._pending.scheduled = True
        taskman = getattr(self.mw, "taskman", None)
        if taskman is not None:
            taskman.run_on_main(self._schedule_refresh)

    def _schedule_refresh(self) -> None:
        from aqt.qt import QTimer

        QTimer.singleShot(self.REFRESH_DELAY_MS, self._refresh_from_timer)

    def _refresh_from_timer(self) -> None:
        """The burst's refresh, between two requests: while one runs, the
        collection may be changing, and the screens (RWKV above all, which
        checks that nothing changed since its state was kept) must see it
        at rest. So wait for the request instead of blocking on it."""
        if not self._lock.acquire(blocking=False):
            from aqt.qt import QTimer

            QTimer.singleShot(self.REFRESH_DELAY_MS // 2, self._refresh_from_timer)
            return
        try:
            self.refresh_screens()
        finally:
            self._lock.release()

    def refresh_now(self) -> None:
        """From a request (which holds the request lock): refresh the screens
        for the changes so far, and wait until that is done."""
        with self._pending_lock:
            if not self._pending.names:
                return
        self.run_on_main(self.refresh_screens)

    def refresh_screens(self) -> None:
        """On the main thread, with no request running: tell the screens
        what the requests changed, as a CollectionOp does after its change."""
        from aqt import gui_hooks

        with self._pending_lock:
            names, self._pending.names = self._pending.names, set()
            self._pending.scheduled = False
        col = getattr(self.mw, "col", None)
        if not names or col is None:
            return
        changes = changes_from(names)
        self.mw.update_undo_actions()
        gui_hooks.operation_did_execute(changes, None)
        if col.op_made_changes(changes):
            gui_hooks.state_did_reset()

    # the add-on's request log (apiLogPath)

    def _open_log(self) -> None:
        path = self.settings.api_log_path
        if path and self._log_file is None:
            try:
                self._log_file = open(path, "w", encoding="utf8")
            except OSError:
                self._log_file = None

    def _close_log(self) -> None:
        log, self._log_file = self._log_file, None
        if log is not None:
            try:
                log.close()
            except OSError:
                pass

    def log_event(self, name: str, data: Any) -> None:
        log = self._log_file
        if log is None:
            return
        with self._log_lock:
            try:
                log.write("[{}]\n".format(name))
                json.dump(data, log, indent=4, sort_keys=True)
                log.write("\n\n")
                log.flush()
            except (OSError, TypeError, ValueError):
                pass


_instance: AnkiConnectService | None = None


def instance() -> AnkiConnectService | None:
    return _instance


def initialize(mw: AnkiQt) -> AnkiConnectService:
    """Create the service and listen if on (called once the add-ons are set
    up, before a profile opens: getProfiles and loadProfile work without
    one, as in the add-on)."""
    global _instance
    service = AnkiConnectService(mw)
    _instance = service
    service.start()
    return service
