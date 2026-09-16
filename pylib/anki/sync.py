# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from typing import Any

from anki import sync_pb2

# public exports
SyncAuth = sync_pb2.SyncAuth
SyncOutput = sync_pb2.SyncCollectionResponse
SyncStatus = sync_pb2.SyncStatusResponse


# Legacy attributes some add-ons may be using. They resolve on access,
# because importing the HTTP client pulls in `requests`, and this module is
# imported while the main window starts up.

_LEGACY_HTTP_CLIENT_NAMES = ("HttpClient", "AnkiRequestsClient")


def __getattr__(name: str) -> Any:
    if name in _LEGACY_HTTP_CLIENT_NAMES:
        from anki.httpclient import HttpClient

        return HttpClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    hidden = {"Any", "_LEGACY_HTTP_CLIENT_NAMES", "__getattr__", "__dir__"}
    return sorted(set(globals()) - hidden | set(_LEGACY_HTTP_CLIENT_NAMES))


class Syncer:
    def sync(self) -> str:
        pass
