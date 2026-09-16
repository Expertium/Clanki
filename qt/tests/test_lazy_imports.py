# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""bs4, requests and the HTTP client load on first use, not while aqt starts."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

import anki.httpclient
import aqt.addons
import aqt.editor_legacy


def _env() -> dict[str, str]:
    """The child interpreter needs the same import paths as this one."""
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if isinstance(p, str) and p),
    }


def test_addons_module_keeps_the_http_client_name() -> None:
    assert aqt.addons.HttpClient is anki.httpclient.HttpClient
    assert "HttpClient" in dir(aqt.addons)


def test_editor_keeps_the_names_it_used_to_import() -> None:
    import bs4

    assert aqt.editor_legacy.bs4 is bs4
    assert aqt.editor_legacy.BeautifulSoup is bs4.BeautifulSoup
    assert aqt.editor_legacy.HttpClient is anki.httpclient.HttpClient
    assert aqt.editor_legacy.requests.__name__ == "requests"
    for name in ("bs4", "BeautifulSoup", "HttpClient", "requests"):
        assert name in dir(aqt.editor_legacy)


@pytest.mark.parametrize("module", ["aqt.addons", "aqt.editor_legacy"])
def test_unknown_names_still_raise(module: str) -> None:
    import importlib

    with pytest.raises(AttributeError):
        getattr(importlib.import_module(module), "no_such_name")


def test_starting_aqt_does_not_import_bs4() -> None:
    code = "import sys\nimport aqt\nimport aqt.main\nprint('bs4' in sys.modules)\n"
    out = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=_env(),
    )
    assert out.stdout.strip() == "False"
