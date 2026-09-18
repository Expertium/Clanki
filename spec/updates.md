# Updates

## updates.release-source

Clanki checks its own repository for updates:
`https://api.github.com/repos/Expertium/Clanki/releases` (and `/releases/latest`
when prereleases are excluded). It must never query another project's
repository. `Expertium/Clanki` is a public repository, so both endpoints need
no authentication. A repository with zero releases is reported as "no update
available", not as an error: GitHub 404s `/releases/latest` and 200s
`/releases` with `[]` when there is nothing to return, and both are treated
the same as "nothing to offer".

**Why:** the fork point inherited `JSchoreels/anki` in these constants. Left
alone, Clanki offered his fork as an update to itself, so accepting the prompt
would replace Clanki with a different program.

**Pinned by:** `release_urls_point_at_clanki`,
`no_releases_at_all_is_reported_as_no_updates`,
`empty_releases_array_is_reported_as_no_updates`,
`other_failures_are_not_reported_as_no_updates` (`rslib/src/backend/github.rs`)

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
