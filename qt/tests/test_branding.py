# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

# Pins spec/branding.md#branding.app-name and #branding.version-string.

import re

import anki.buildinfo
import aqt


def test_visible_product_name_is_clanki() -> None:
    assert aqt.APP_NAME == "Clanki"
    assert aqt.application_name() in ("Clanki", "Clanki Portable")


def test_version_string_is_the_official_anki_release() -> None:
    # Add-ons compare this against the Anki release they support, so it must
    # carry no fork suffix and match aqt.appVersion.
    assert aqt.appVersion == anki.buildinfo.version
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", anki.buildinfo.version)
