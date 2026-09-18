# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/branding.md#branding.about-window-disclaimer.

from __future__ import annotations

import pytest

import anki.lang

# aqt.about reads translated strings at import time (the contributor list is
# built at import, ftl accessors are called at call time)
anki.lang.set_lang("en")

import aqt.about  # noqa: E402
import aqt.branding  # noqa: E402


def test_about_window_states_it_is_not_official_anki() -> None:
    html = aqt.about._fork_disclaimer_html()
    lowered = html.lower()
    assert "clanki" in lowered and "fork" in lowered and "anki" in lowered
    assert "not official anki" in lowered
    assert "not affiliated with" in lowered
    assert "damien elmes" in lowered
    assert "agpl-3" in lowered or "agpl3" in lowered


def test_about_window_links_to_official_anki_and_to_clankis_own_repository() -> None:
    html = aqt.about._fork_disclaimer_html()
    assert aqt.appWebsite in html
    assert aqt.branding.CLANKI_REPOSITORY in html
    assert aqt.branding.CLANKI_REPOSITORY == "https://github.com/Expertium/Clanki"


def test_about_window_disclaimer_is_shown_before_clankis_own_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aqt.about, "qVersion", lambda: "6.0.0")
    monkeypatch.setattr(aqt.about, "qWebEngineChromiumVersion", lambda: "120.0.0")

    text = aqt.about._about_text()

    disclaimer_at = text.find(aqt.about._fork_disclaimer_html())
    lede_at = text.find("friendly")
    assert disclaimer_at != -1 and lede_at != -1
    assert disclaimer_at < lede_at


def test_about_window_keeps_version_and_build_information(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(aqt.about, "qVersion", lambda: "6.0.0")
    monkeypatch.setattr(aqt.about, "qWebEngineChromiumVersion", lambda: "120.0.0")

    text = aqt.about._about_text()

    assert "Python" in text and "Qt 6.0.0" in text and "Chromium 120" in text
