# Branding

## branding.app-name

Given any window title, dialog title, the About screen, the installer
metadata and the English interface strings that talk about the running
application, the product is called **Clanki**. Names that belong to the
ecosystem rather than to this program keep the word Anki: the file formats
(Anki Deck Package, Anki Collection Package, .apkg/.colpkg/.anki, add-on
packages), version references to old Anki releases (Anki 2.x, 23.10), the
`Documents/Anki` and `Anki2` data folders, AnkiWeb, AnkiDroid, AnkiMobile and
AnkiHub, and the "Support Anki" donation link. Translations other than English
are inherited from Anki and still say Anki.

**Why:** Andrew, 2026-09-14: everything visible should say Clanki.

**Pinned by:** `test_visible_product_name_is_clanki`
(`qt/tests/test_branding.py`). The strings themselves are markup.

## branding.version-string

Given an add-on that reads `anki.buildinfo.version`, `anki.version` or
`aqt.appVersion`, it gets the version number of the official Anki release that
Clanki is built on (currently `26.09`), with no fork suffix, so add-on
compatibility checks behave exactly as on that Anki release. `.version` is the
single source; the Clanki update check (`spec/updates.md`) compares against
Clanki's own GitHub releases and is unaffected.

**Why:** Andrew, 2026-09-14: add-on compatibility.

**Pinned by:** `test_version_string_is_the_official_anki_release`
(`qt/tests/test_branding.py`).
