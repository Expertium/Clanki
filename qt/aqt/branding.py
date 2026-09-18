# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The product name shown to the user (spec branding.app-name).

This module has no imports so that it can be used while `aqt` itself is
still importing. The version string that add-ons read
(`anki.buildinfo.version`, `aqt.appVersion`) stays that of the official Anki
release Clanki is built on, and the data folder stays `Anki2`.
"""

APP_NAME = "Clanki"

# Clanki's own repository (spec branding.about-window-disclaimer): shown in
# the About and Support windows so a reader can find Clanki's own source,
# distinct from official Anki's.
CLANKI_REPOSITORY = "https://github.com/Expertium/Clanki"
