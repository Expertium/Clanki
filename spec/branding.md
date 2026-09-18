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

## branding.about-window-disclaimer

Given Help > About Clanki, the first thing shown, before Clanki's own
description of itself, is a bordered notice saying: Clanki is a fork of
Anki, not official Anki, and not affiliated with, endorsed by, or supported
by AnkiTects Pty Ltd or Damien Elmes; it is a derivative work distributed
under the same AGPL-3 license as Anki, and Anki's own copyright and
contributors (listed further down, unchanged) keep their credit; a link to
official Anki (`aqt.appWebsite`, `https://apps.ankiweb.net/`); and a link to
Clanki's own repository (`aqt.branding.CLANKI_REPOSITORY`,
`https://github.com/Expertium/Clanki`). The version and build information
below it is unchanged.

**Why:** Andrew, 2026-09-18: "make a clear distinction between Anki and
Clanki. Make sure anyone who reads it walks away knowing that this is not
official Anki" — every renamed ftl string below this notice (About Clanki's
title, the AGPL3 blurb, "Clanki is a friendly, intelligent...") already says
"Clanki" throughout, in a way that alone reads as if this were Damien
Elmes's own project by that name, so a plain rename is not enough; an
explicit disclaimer is needed and must come first, not last.

**Pinned by:** `test_about_window_states_it_is_not_official_anki`,
`test_about_window_links_to_official_anki_and_to_clankis_own_repository`,
`test_about_window_disclaimer_is_shown_before_clankis_own_description`
(`qt/tests/test_about.py`).
