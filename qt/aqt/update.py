# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from typing import Callable

import aqt
from anki.buildinfo import buildhash
from anki.buildinfo import version as version_str
from anki.collection import GithubRelease
from aqt.operations import QueryOp
from aqt.package import (
    download_github_update_and_install as _download_github_update_and_install,
)
from aqt.qt import *
from aqt.utils import show_warning, tooltip, tr


def _release_is_newer(
    release: GithubRelease,
    *,
    current_version: str = version_str,
    current_buildhash: str = buildhash,
) -> bool:
    from packaging.version import Version

    release_version = Version(release.tag_name)
    installed_version = Version(current_version)
    release_base = Version(release_version.public.split("+", maxsplit=1)[0])
    installed_base = Version(installed_version.public.split("+", maxsplit=1)[0])

    if release_base != installed_base:
        return release_base > installed_base

    target = release.target_commitish.lower()
    installed_hash = current_buildhash.lower()
    if target and installed_hash and target.startswith(installed_hash):
        return False

    # NOTE: this used to also return True whenever `len(target) >= 8`, on the
    # theory that a release whose target_commitish is a real commit SHA
    # (rather than a short branch name like "main") is definitely a
    # different, newer build than ours if the hash didn't match above. That
    # reasoning doesn't hold: our own release workflow always pins
    # target_commitish to the full build SHA (.github/workflows/release.yml,
    # `--target "$RELEASE_SHA"`), so the clause was true for every release,
    # always. A dev/source build's own buildhash never matches a past
    # release's commit either, so every dev build was told an update was
    # available even when its version was not older than the latest release.
    # Only a version-number comparison is real evidence of being older.
    return release_version > installed_version


def check_for_update(*, parent: aqt.AnkiQt, manual: bool) -> None:
    from packaging.version import Version

    version = Version(version_str)

    def on_success(release: GithubRelease) -> None:
        if _release_is_newer(release):
            prompt_and_install_github_update(parent, release)
        elif manual:
            tooltip(tr.addons_no_updates_available(), parent=parent)

    def on_failure(exc: Exception) -> None:
        if manual:
            show_warning(str(exc), parent=parent)
        else:
            print(f"update check failed: {exc}")

    op = get_latest_release_op(
        parent=parent,
        include_prerelease=version.is_prerelease,
        on_success=on_success,
    ).failure(on_failure)
    if manual:
        op = op.with_progress()
    op.run_in_background()


def prompt_and_install_github_update(mw: aqt.AnkiQt, release: GithubRelease) -> None:
    msg = (
        tr.qt_misc_anki_updatedanki_has_been_released(val=release.tag_name)
        + tr.qt_misc_would_you_like_to_download_it()
    )

    msgbox = QMessageBox(mw)
    msgbox.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    msgbox.setIcon(QMessageBox.Icon.Information)
    msgbox.setText(msg)

    msgbox.setDefaultButton(QMessageBox.StandardButton.Yes)
    ret = msgbox.exec()

    if ret == QMessageBox.StandardButton.Yes:
        _download_github_update_and_install(release)


def get_latest_release_op(
    parent: QWidget,
    include_prerelease: bool,
    on_success: Callable[[GithubRelease], None],
) -> QueryOp:
    return QueryOp(
        parent=parent,
        op=lambda col: col._backend.get_latest_release(
            include_prerelease=include_prerelease
        ),
        success=on_success,
    )
