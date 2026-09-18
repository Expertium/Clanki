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

`_release_is_newer` (`qt/aqt/update.py`) offers an update only when the
release's version is a strict improvement over the installed one: either its
base version is higher, or (same base version) its full version compares
higher under the historical `+build.N` local-version scheme. A build whose
version is not older than the newest release is never offered an update,
even when its buildhash does not match the release's commit — a different
commit is not, by itself, evidence of being older. (A commit that IS an exact
match to the release's pinned commit is still short-circuited to "not
newer" earlier in the function, before the version comparison runs.)

**Why:** the removed `... or len(target) >= 8` fallback treated "the
release's `target_commitish` is a full commit SHA I don't match" as proof
that the release was newer. Clanki's own release workflow
(`.github/workflows/release.yml`, `gh release create --target "$RELEASE_SHA"`)
always pins `target_commitish` to the full build SHA, so that condition was
true for every real release, always. A dev/source build's own buildhash is
essentially never a match for some past release's commit either (that is the
ordinary state of active development between releases, not an edge case), so
the fallback made every dev/source build - and any two builds sharing a
version but not a commit - report an update as available whether or not one
was actually older.

**Pinned by:** `test_release_is_newer_requires_a_strictly_newer_version`,
`test_release_is_newer_uses_version_and_release_commit`
(`qt/tests/test_update.py`)
