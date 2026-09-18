# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from anki.collection import GithubRelease
from aqt.update import _release_is_newer


def release(tag: str, target: str) -> GithubRelease:
    return GithubRelease(tag_name=tag, target_commitish=target)


def test_release_is_newer_uses_version_and_release_commit() -> None:
    assert _release_is_newer(
        release("26.09b2+fsrs7.build.90", "bbbbbbbb1234"),
        current_version="26.09b1+fsrs7",
        current_buildhash="aaaaaaaa",
    )
    assert _release_is_newer(
        release("26.09b1+fsrs7.build.90", "bbbbbbbb1234"),
        current_version="26.09b1+fsrs7",
        current_buildhash="aaaaaaaa",
    )
    assert not _release_is_newer(
        release("26.09b1+fsrs7.build.90", "aaaaaaaa1234"),
        current_version="26.09b1+fsrs7",
        current_buildhash="aaaaaaaa",
    )
    assert not _release_is_newer(
        release("26.08+fsrs7.build.89", "bbbbbbbb1234"),
        current_version="26.09b1+fsrs7",
        current_buildhash="aaaaaaaa",
    )


def test_release_is_newer_requires_a_strictly_newer_version() -> None:
    """Pins spec/updates.md#updates.dev-build-always-offered: a build whose
    version is not older than the newest release is not offered an update,
    even when its buildhash does not match the release's commit. A
    different commit is not, by itself, evidence of being older: a
    dev/source build's own commit never matches a past release's, and our
    release workflow always pins target_commitish to a full commit SHA, so
    a hash mismatch is the ordinary case, not a special one."""
    # A dev build sitting on the same (unreleased-since) version as the
    # latest release, built from some other commit: not newer. This is the
    # exact case that used to always report "update available".
    assert not _release_is_newer(
        release("26.09", "a" * 40),
        current_version="26.09",
        current_buildhash="bbbbbbbb",
    )
    # The release's commit is exactly ours: still not newer (this already
    # worked before the fix, via the buildhash check above the fallback).
    assert not _release_is_newer(
        release("26.09", "bbbbbbbb" + "c" * 32),
        current_version="26.09",
        current_buildhash="bbbbbbbb",
    )
    # A genuinely newer version bump under the legacy same-base build-number
    # scheme is still detected without needing the buildhash at all.
    assert _release_is_newer(
        release("26.09+build.2", "a" * 40),
        current_version="26.09+build.1",
        current_buildhash="bbbbbbbb",
    )
    # An older version, different commit: not newer (the old fallback would
    # wrongly say True here too, since len(target) >= 8).
    assert not _release_is_newer(
        release("26.09+build.1", "a" * 40),
        current_version="26.09+build.2",
        current_buildhash="bbbbbbbb",
    )
