# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The FSRS Helper add-on stays disabled (spec addons.fsrs-helper-blocked).

It is written for the FSRS versions of official Anki, not for Clanki's FSRS-7,
so it is disabled at start-up, refused when the user enables it, and installed
disabled, the same way as the Review Heatmap and AnkiConnect add-ons, for a
different reason."""

from __future__ import annotations

import re

# AnkiWeb id, and the folder names of a source install
ADDON_DIRS = ("759844606", "fsrs4anki-helper", "fsrs4anki_helper", "fsrs_helper")
ADDON_NAMES = ("fsrshelper", "fsrs4ankihelper")
ADDON_NOTICE_SHOWN_KEY = "fsrsHelperAddonNoticeShown"


def _normalized_name(name: object) -> str:
    if not isinstance(name, str):
        return ""
    return re.sub(r"[\s_\-]", "", name).lower()


def is_fsrs_helper_addon(folder: str, name: object = None) -> bool:
    """True for the FSRS Helper add-on: its AnkiWeb id, a source install, or
    an add-on whose manifest or meta name is FSRS Helper."""
    return folder.lower() in ADDON_DIRS or _normalized_name(name) in ADDON_NAMES


def disable_fsrs_helper_addon(addon_manager: object) -> list[str]:
    """Disable every installed, enabled copy. Returns the folders disabled."""

    all_addons = getattr(addon_manager, "allAddons", None)
    is_enabled = getattr(addon_manager, "isEnabled", None)
    toggle = getattr(addon_manager, "toggleEnabled", None)
    addon_meta = getattr(addon_manager, "addonMeta", None)
    if not (callable(all_addons) and callable(is_enabled) and callable(toggle)):
        return []
    disabled = []
    for folder in all_addons():
        name = None
        if callable(addon_meta):
            try:
                name = addon_meta(folder).get("name")
            except Exception:
                name = None
        if is_fsrs_helper_addon(folder, name) and is_enabled(folder):
            toggle(folder, enable=False)
            disabled.append(folder)
    return disabled
