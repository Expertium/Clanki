# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The FSRS Helper add-on stays disabled (spec addons.fsrs-helper-blocked)."""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from aqt.fsrs_helper_addon import (
    ADDON_NOTICE_SHOWN_KEY,
    disable_fsrs_helper_addon,
    is_fsrs_helper_addon,
)


class FakeAddons:
    def __init__(self, addons: dict[str, dict[str, Any]]) -> None:
        self.addons = addons
        self.toggled: list[tuple[str, bool]] = []

    def allAddons(self) -> list[str]:
        return list(self.addons)

    def addonMeta(self, folder: str) -> dict[str, Any]:
        return {"name": self.addons[folder].get("name")}

    def isEnabled(self, folder: str) -> bool:
        return self.addons[folder]["enabled"]

    def toggleEnabled(self, folder: str, enable: bool) -> None:
        self.toggled.append((folder, enable))
        self.addons[folder]["enabled"] = enable


def test_fsrs_helper_is_recognised_by_id_folder_or_name() -> None:
    assert is_fsrs_helper_addon("759844606")
    assert is_fsrs_helper_addon("fsrs4anki-helper")
    assert is_fsrs_helper_addon("123", "FSRS Helper")
    assert is_fsrs_helper_addon("123", "FSRS4Anki Helper")
    assert not is_fsrs_helper_addon("123", "FSRS Something Else")
    assert not is_fsrs_helper_addon("1771074083", "Review Heatmap")


def test_an_enabled_fsrs_helper_addon_is_disabled_at_start_up() -> None:
    addons = FakeAddons(
        {
            "759844606": {"name": "FSRS Helper", "enabled": True},
            "999": {"name": "FSRS4Anki Helper", "enabled": False},
            "2055492159": {"name": "AnkiConnect", "enabled": True},
        }
    )
    assert disable_fsrs_helper_addon(addons) == ["759844606"]
    assert addons.toggled == [("759844606", False)]


def test_the_fsrs_helper_notice_is_shown_only_once() -> None:
    from aqt.main import AnkiQt

    shown: list[str] = []
    mw = cast(
        Any,
        SimpleNamespace(
            _fsrs_helper_addon_notice_pending=True,
            pm=SimpleNamespace(meta={}, save=MagicMock()),
        ),
    )
    with patch("aqt.main.showInfo", side_effect=lambda text, **_: shown.append(text)):
        AnkiQt._show_fsrs_helper_addon_notice(mw)
        AnkiQt._show_fsrs_helper_addon_notice(mw)
    assert len(shown) == 1
    assert mw.pm.meta[ADDON_NOTICE_SHOWN_KEY] is True
    mw.pm.save.assert_called_once()


def test_enabling_the_fsrs_helper_addon_is_refused_with_a_message() -> None:
    from aqt.addons import AddonManager

    written: list[bool] = []
    for folder, name, expect_enabled in (
        ("759844606", "FSRS Helper", False),
        ("555", "FSRS4Anki Helper", False),
        ("other_addon", "Other", True),
    ):
        addon = SimpleNamespace(
            enabled=False, human_name=lambda: folder, provided_name=name
        )
        manager = MagicMock()
        manager.addon_meta.return_value = addon
        manager._disableConflicting.return_value = []
        manager.write_addon_meta.side_effect = lambda meta: written.append(meta.enabled)
        with (
            patch("aqt.addons.showInfo") as show_info,
            patch("aqt.addons.tr") as tr,
        ):
            AddonManager.toggleEnabled(manager, folder, enable=True)
        assert addon.enabled is expect_enabled
        if expect_enabled:
            show_info.assert_not_called()
        else:
            show_info.assert_called_once_with(
                tr.preferences_fsrs_helper_addon_blocked.return_value,
                textFormat="plain",
            )
    assert written == [False, False, True]


def test_installing_the_fsrs_helper_addon_leaves_it_disabled() -> None:
    from aqt.addons import AddonManager

    for previous_meta, expect_message in (({}, True), ({"name": "FSRS Helper"}, False)):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w"):
            pass
        manager = MagicMock()
        manager.readManifestFile.return_value = {
            "package": "759844606",
            "name": "FSRS Helper",
        }
        manager._manifest_schema = {"properties": {"name": {"meta": True}}}
        manager.addonMeta.return_value = dict(previous_meta)
        manager._disableConflicting.return_value = []
        with (
            patch("aqt.addons.showInfo") as show_info,
            patch("aqt.addons.tr"),
            patch("aqt.addons.gui_hooks"),
        ):
            AddonManager.install(manager, archive, force_enable=True)
        package, meta = manager.writeAddonMeta.call_args.args
        assert package == "759844606" and meta["disabled"] is True
        # a fresh install says so; an update of an installed copy is silent
        assert show_info.called is expect_message
