# Branding

## branding.app-name

Given any window title, dialog title, the About screen, the installer
metadata and the English interface strings that talk about the running
application, the product is called **Clanki**. Names that belong to the
ecosystem rather than to this program keep the word Anki: the file formats
(Anki Deck Package, Anki Collection Package, .apkg/.colpkg/.anki, add-on
packages), version references to old Anki releases (Anki 2.x, 23.10), the
`Documents/Anki` and `Anki2` data folders, AnkiWeb, AnkiDroid, AnkiMobile and
AnkiHub. Translations other than English are inherited from Anki and still
say Anki.

Help > Support Anki & Clanki (renamed from "Support Anki",
`branding.support-window`) is the one menu item that intentionally names
both: it now offers support for either project, so it says both names.

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

## branding.about-window-credit

The contributor list further down the About window is introduced as Anki's
credit, not Clanki's: "Clanki is built on Anki, which was written by Damien
Elmes, with patches, translation, testing and design from:", followed by the
same contributor list as upstream, unchanged. The ftl key is
`about-anki-written-by-damien-elmes-with-patches`, a Clanki string of its
own, so every language reads the corrected sentence until it is translated.

**Why:** Andrew, 2026-09-21: "The description 'Written by Damien Elmes' is
conflicting with 'Clanki is a fork of Anki...'". The window renames the app
to Clanki throughout, so upstream's "Written by Damien Elmes" reads as a
claim about Clanki and contradicts the disclaimer eight lines above it
(`branding.about-window-disclaimer`). Naming the work the credit belongs to
keeps Anki's credit intact, which the AGPL-3 licence requires, and removes
the contradiction.

**Pinned by:** `test_about_window_credits_anki_not_clanki_to_damien_elmes`,
`test_about_window_keeps_ankis_own_contributor_list`
(`qt/tests/test_about.py`).

## branding.support-window

Given Help > Support Anki & Clanki (renamed from "Support Anki", which used
to link straight to Anki's own support page), a small dialog is shown
instead, holding three things in order: a fork clarification consistent
with the About window; a link to Anki's own manual
(`https://docs.ankiweb.net`), labelled as Anki's, not Clanki's; and, kept
visually apart from the other two (a horizontal rule, its own heading
"Support development of Clanki specifically", which names the project and
not a person), Andrew's Ethereum address for anyone who wants to support
Clanki, plus a "Copy address" button that copies it to the clipboard.
The address is a Python constant
(`aqt.support.CLANKI_AUTHOR_ETHEREUM_ADDRESS`, EIP-55 checksummed —
its mixed upper/lower-case letters are not to be normalised), never
in an ftl file, so a translator cannot touch it. Its exact value:
`0xf9c22186be4dbF21dE3E149C167Ed90DE0E37279`.

**Why:** Andrew, 2026-09-18: "Idk what to do about Help->Support Anki.
Currently it links to the Anki manual. I think we should show a window
that says: 1) A clarification that this is a fork 2) A link to the
official Anki manual 3) My Ethereum address, in case somebody wants to
support me specifically." The address must be exact and copyable: a wrong
character sends a stranger's money to nobody, and retyping 42 characters by
hand is how people lose money. Andrew, 2026-09-21: the heading asks for
support for Clanki's development, not for him by name.

**Pinned by:** `test_support_window_states_it_is_a_fork`,
`test_support_window_heading_names_the_project_not_the_author`,
`test_support_window_labels_the_manual_link_as_ankis`,
`test_support_window_ethereum_address_is_exact`,
`test_support_window_ethereum_address_is_not_in_any_ftl_file`
(`qt/tests/test_support.py`).
