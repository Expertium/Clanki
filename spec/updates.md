# Updates

## updates.release-source

Clanki checks its own repository for updates:
`https://api.github.com/repos/Expertium/Clanki/releases` (and `/releases/latest`
when prereleases are excluded). It must never query another project's
repository.

**Why:** the fork point inherited `JSchoreels/anki` in these constants. Left
alone, Clanki offered his fork as an update to itself, so accepting the prompt
would replace Clanki with a different program.

**Pinned by:** `release_urls_point_at_clanki` (`rslib/src/backend/github.rs`)

**Known consequence, not yet fixed:** the repo is private and has no releases,
so the check currently fails rather than reporting "no updates". See
`updates.dev-build-always-offered` for a second, separate defect.

## updates.dev-build-always-offered

A build made from source is always told an update is available, whatever its
version string.

`_release_is_newer` (`qt/aqt/update.py`) ends with
`return release_version > installed_version or len(target) >= 8`. A source
build's buildhash is the local git commit, which never matches a published
release's commit, so the check falls through to `len(target) >= 8`, which is
true for any full-length SHA.

**Why:** recorded because it looks like a version-numbering bug and is not one.
Removing `+fsrs7` from the version did not cause it and did not change it.

**Pinned by:** nothing yet. This entry documents current behavior; it is not a
decision to keep it.
