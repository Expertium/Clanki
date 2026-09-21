# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/branding.md#branding.support-window.

from __future__ import annotations

from pathlib import Path

import anki.lang

anki.lang.set_lang("en")

import aqt.support  # noqa: E402

# The exact string, spelled out here too so a change to aqt/support.py's own
# constant is caught by a diff against an independent copy, not just against
# itself (spec branding.support-window). Andrew supplied this string
# directly; it must never be normalised, reformatted, or retyped by hand.
EXPECTED_ETHEREUM_ADDRESS = "0xf9c22186be4dbF21dE3E149C167Ed90DE0E37279"


def test_support_window_ethereum_address_is_exact() -> None:
    assert aqt.support.CLANKI_AUTHOR_ETHEREUM_ADDRESS == EXPECTED_ETHEREUM_ADDRESS
    # format sanity: 40 hex characters after 0x, mixed case (the EIP-55
    # checksum itself was not independently verified -- no keccak library
    # was available -- so this only guards the shape, not the checksum)
    body = aqt.support.CLANKI_AUTHOR_ETHEREUM_ADDRESS
    assert body.startswith("0x") and len(body) == 42
    assert all(c in "0123456789abcdefABCDEF" for c in body[2:])
    assert body != body.lower() and body != body.upper()


def test_support_window_ethereum_address_is_not_in_any_ftl_file() -> None:
    ftl_root = Path(__file__).parents[2] / "ftl"
    offenders = [
        p
        for p in ftl_root.rglob("*.ftl")
        if EXPECTED_ETHEREUM_ADDRESS in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_support_window_states_it_is_a_fork() -> None:
    html = aqt.support._support_html()
    lowered = html.lower()
    assert "clanki" in lowered and "fork" in lowered and "not official anki" in lowered


def test_support_window_labels_the_manual_link_as_ankis() -> None:
    html = aqt.support._support_html()
    assert aqt.support.ANKI_MANUAL_URL in html
    assert aqt.support.ANKI_MANUAL_URL == "https://docs.ankiweb.net"
    lowered = html.lower()
    assert "anki's own manual" in lowered
    assert "not clanki's" in lowered


def test_support_window_heading_names_the_project_not_the_author() -> None:
    """The heading asks for support for Clanki's development, not for Andrew
    by name (spec branding.support-window)."""
    html = aqt.support._support_html()
    lowered = html.lower()
    assert "support development of clanki specifically" in lowered
    assert "andrew" not in lowered


def test_support_window_shows_the_address_kept_apart_from_the_manual_link() -> None:
    """The Ethereum section comes after a visual separator, not interleaved
    with the manual link (spec branding.support-window: "kept visually
    separate from anything to do with supporting Anki")."""
    html = aqt.support._support_html()
    manual_at = html.find(aqt.support.ANKI_MANUAL_URL)
    separator_at = html.find("<hr>")
    address_at = html.find(aqt.support.CLANKI_AUTHOR_ETHEREUM_ADDRESS)
    assert manual_at != -1 and separator_at != -1 and address_at != -1
    assert manual_at < separator_at < address_at


def test_support_window_is_registered_and_replaces_the_direct_link() -> None:
    """Help > Support Anki & Clanki opens the dialog rather than calling
    openLink directly (spec branding.support-window)."""
    import inspect

    import aqt.main

    assert "Support" in aqt.DialogManager._dialogs
    assert aqt.DialogManager._dialogs["Support"][0] is aqt.support.show
    source = inspect.getsource(aqt.main.AnkiQt.onDonate)
    assert 'dialogs.open("Support"' in source
    assert "openLink" not in source
