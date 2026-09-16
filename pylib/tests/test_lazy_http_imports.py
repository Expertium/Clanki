# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The HTTP client loads on first use, and the names add-ons read stay."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

import anki.sync
from anki.httpclient import HttpClient


def _env() -> dict[str, str]:
    """The child interpreter needs the same import paths as this one."""
    return {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}


def test_sync_module_keeps_its_legacy_http_client_names() -> None:
    assert anki.sync.HttpClient is HttpClient
    assert anki.sync.AnkiRequestsClient is HttpClient
    names = dir(anki.sync)
    assert "HttpClient" in names
    assert "AnkiRequestsClient" in names
    assert "SyncAuth" in names


def test_sync_module_raises_for_an_unknown_name() -> None:
    with pytest.raises(AttributeError):
        anki.sync.no_such_name


def test_importing_the_sync_module_does_not_import_requests() -> None:
    code = (
        "import sys\n"
        "import anki.sync\n"
        "print('requests' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert out.stdout.strip() == "False"
