# Clanki

Clanki = **Anki + clanker**: a fork of Anki in which every change is made by AI.

## Lineage

- Base: **https://github.com/JSchoreels/anki/** — we branch from that fork, not
  from upstream `ankitects/anki`, because it already carries RWKV and FSRS work.
- Chain: `ankitects/anki` → `JSchoreels/anki` → **Clanki**.
- Fork point: commit `796f0140a` ("Test portable installer parsing as macOS"),
  aligned with Anki 26.09b1.
- `origin` points at the Clanki repo. The JSchoreels fork is the `upstream`
  remote. Never push to `upstream`.

## Planned direction

1. **RWKV neural network** for scheduling. A **separate Claude session** works on
   making it more accurate and more efficient. Do not duplicate or rewrite that
   work here without checking first.
2. **UI split: Simplified / Advanced**, in the SuperMemo style. **Simplified is
   the default.** Many settings get hidden. The current deck-options UI is far
   too complex, even by the standards of Anki power users. Hiding a setting is a
   UI change, not a behavior change — the underlying setting keeps working.
3. **Rescheduling must not write to the card's history.** Today, FSRS/RWKV
   rescheduling adds an entry to the card's review log. It should not. Working
   reference implementation: the rescheduling in the **FSRS Helper** add-on.
   Note: this one **is** a behavior change under the contract below — the
   collection DB can detect it — so it needs a `spec/` entry and a pinning test.
4. **Native AnkiConnect.** Integrate the functionality of
   https://github.com/JSchoreels/anki-connect into the core, so it does not need
   to be installed as an add-on. Its HTTP API surface is a hard compatibility
   boundary: existing AnkiConnect clients must keep working.
5. **A few Search Stats Extended graphs.** Port a **handful** of the graphs from
   https://github.com/JSchoreels/Anki-Search-Stats-Extended — deliberately **not**
   all of them. Ask which ones before porting; picking the subset is a product
   decision, not an implementation detail.
6. **Remove Adaptive Desired Retention (ADR).** In this codebase the feature is
   named **dynamic desired retention** — it is the same thing; the underlying
   `fsrs` crate types are `CostAdrPolicy` and `CostAdrNextStates`. Known surface:
   `rslib/src/scheduler/fsrs/dynamic_desired_retention.rs`,
   `ts/routes/deck-options/dynamic-desired-retention.ts` and its test, plus
   references in `ts/routes/deck-options/FsrsOptions.svelte` and
   `rslib/src/scheduler/fsrs/simulator.rs`. The `fsrs` crate dependency stays;
   only Anki's use of ADR goes. Removing a user-visible option is a behavior
   change: it needs a `spec/` entry, and deck presets that already stored ADR
   settings must still load without error.
7. **The simulator stays FSRS-only.** Remove or deactivate the RWKV simulator
   path. Reason: RWKV uses many more input features, and it processes all cards
   together instead of treating them as independent. A correct RWKV simulator is
   a very large job and is **out of scope** — do not start one. Known surface:
   `rwkvWorkload`, `rwkvWorkloadSampleLimit`, `rwkvWorkloadTargetStep`,
   `rwkvWorkloadStateUpdateInterval` and the FSRS-vs-RWKV comparison mode in
   `ts/routes/deck-options/SimulatorModal.svelte`, the matching fields on
   `simulateFsrsRequest` (so `proto/` too), and
   `ts/routes/deck-options/simulator-workload.ts`. One thing to check rather than
   assume: `rslib/src/scheduler/fsrs/simulator.rs` imports
   `scheduler::rwkv::relative_overdueness`. Confirm whether that is RWKV
   simulation or just a shared helper before deleting it.
8. Many smaller changes and tweaks.

## Changes already made in Clanki

- Removed `+fsrs7` from the version name. `.version` is now `26.09b1`
  (was `26.09b1+fsrs7`). Note: `qt/tests/test_update.py` still hardcodes
  `26.09b1+fsrs7` in its own fixtures. That test does not read `.version`, so it
  still passes. The fork's release tags used the `+fsrs7.build.N` form.

---

# Behavior contract

- **Code is disposable; observable behavior is sacred.** Observable behavior =
  anything a user, the sync server, an add-on, or the collection DB can detect:
  card scheduling outcomes, intervals, queue order, DB contents, API responses,
  file formats. **NOT** behavior: speed, memory, internal structure, log text.
- Refactor as aggressively as you like, **if the behavior-lock tests pass**. No
  behavior-lock test covering the area = write one first, then refactor.
- Never change observable behavior unless the user explicitly asked for that
  change in this conversation. This includes bug fixes: if the fix changes
  input→output, say so and wait for approval.
- **Chesterton's Fence applies to behavior, not code.** If you cannot tell why
  the system behaves some way — especially anything "obviously wrong" — ask.
  Users may depend on it (Hyrum's Law).

The load-bearing quirks in Anki specifically are: the collection database
schema, the sync protocol, the add-on API surface, and scheduling outcomes that
users' review histories are built on. Treat all four as behavior.

Why the enforcement matters here: Anki is Rust + TypeScript/Svelte + Python, and
the user reads only the Python. For most of this codebase **the test suite is
the only reviewer that exists**. A large diff is exactly where a silent behavior
change hides. The scheduler is deterministic given (collection state, review
log, clock), so golden tests (simulate N reviews, snapshot the resulting
intervals and queue) plus property tests are a genuinely strong behavior lock.

# Spec (`spec/` directory)

- `spec/` holds the **current intended behavior** in natural language, organized
  by **behavior domain**, not by code file (`spec/scheduling.md`, `spec/sync.md`,
  `spec/deck-options.md`, `spec/import-export.md`, `spec/addon-api.md`,
  `spec/database.md`). Code files move and get rewritten; behavior domains are
  stable.
- **One entry per behavior**, with four parts: a stable ID heading (e.g.
  `sched.fuzz-interval`), a testable "given X, the system does Y" statement, a
  **Why:** line, and a **Pinned by:** line naming the tests that lock it. If you
  cannot write the statement as "given X, the system does Y", the entry is too
  vague — split it. Example:

  ```markdown
  ## sched.fuzz-interval
  When the scheduler computes an interval of 3 days or more, it applies a random
  fuzz of ±5% (minimum ±1 day), seeded per card. Intervals under 3 days get no fuzz.
  **Why:** cards introduced together would otherwise stay synchronized forever.
  **Pinned by:** `test_fuzz_bounds`, `test_no_fuzz_short_intervals`
  ```

- Entries state **current** behavior only — no history. The git history of
  `spec/` is the change log: `git diff` of a `behavior:` commit is the
  before/after description, and `git log --follow spec/scheduling.md` is the
  full audit trail, generated for free. One artifact, so it cannot drift from
  itself.
- **Baseline rule:** any behavior with no entry in `spec/` is expected to match
  the fork point recorded in `spec/README.md`. Upstream is the implicit spec.
  `spec/` accumulates only divergences and touched areas — this is what makes
  the scheme affordable for a fork.
- Every intentional behavior change edits the relevant spec entry **and** its
  pinning test **in the same commit**, prefixed `behavior:`. Behavior-preserving
  work is prefixed `refactor:` and must not touch `spec/`. Deciding which prefix
  applies, before making the change, is where mistakes get caught.
- Before refactoring an unspecced area, add the entry for the behavior you are
  about to preserve. The moment you study old behavior in order to keep it is
  the cheapest moment to write it down. **Never backfill `spec/` beyond that.**
- Never write an entry for internals (speed, memory, structure).

# Local overrides for this machine

- The "Testing with the user's collection" section below is inherited from the
  JSchoreels fork and names a **macOS** path
  (`/Users/jschoreels/Library/Application Support/Anki2/...`). That path does not
  exist here. This is Windows; the profile lives under
  `%APPDATA%\Anki2\`. The safety rule still applies in full: copy a backup to a
  temp directory, never open or modify the live profile in place, and ask first
  if a test needs newer state than the backups hold.
- `just` and `pwsh` are **not** installed on this machine, so the `just` recipes
  in the section below do not run as written. Build through the `./ninja`
  wrapper under Git Bash until that changes.
- The section below ends with a reference to `@.claude/user.md`, which does not
  exist in this repo.
- **Detached builds must never be able to prompt.** The first build here hung for
  two hours at the `node_modules` step: corepack wanted to ask "download
  yarn@4.11.0?" and waited forever on a stdin that a detached process does not
  have. Set `COREPACK_ENABLE_DOWNLOAD_PROMPT=0` and redirect stdin from `NUL`,
  so any future prompt fails fast instead of hanging silently. Judge a detached
  build by **CPU time deltas**, not by whether the processes still exist — a
  hung build looks alive.

---

# Claude Code Configuration

> **Note:** Every command you need — building, running, testing, linting,
> formatting — is defined as a recipe in the project `justfile`. Run
> `just --list` to see them. Do not invoke `./ninja`, `./run`, or scripts
> under `./tools` directly — use the `just` recipes instead.

## Project Overview

Anki is a spaced repetition flashcard program with a multi-layered architecture. Main components:

- Web frontend: Svelte/TypeScript in ts/
- PyQt GUI, which embeds the web components in aqt/
- Python library which wraps our rust Layer (pylib/, with Rust module in pylib/rsbridge)
- Core Rust layer in rslib/
- Protobuf definitions in proto/ that are used by the different layers to
  talk to each other.

## Running Anki

To build and run Anki in development mode:

```
just run
```

This builds pylib and qt, then launches Anki with debugging enabled. Web
views are served at http://localhost:40000/_anki/pages/ (e.g.,
deckconfig.html). Use `just run-optimized` for a release-optimized build.
For live-reloading during web development, run `just web-watch` in a
separate terminal — it monitors ts/, sass/, and qt/aqt/data/web/ and
auto-rebuilds on changes (`just rebuild-web` triggers a one-off rebuild).

## Building/checking

`just check` will format the code and run the main build & checks.
Please do this as a final step before marking a task as completed.

Run `just` (or `just --list`) to see all available commands.

## Release notes

When developing a user-visible feature, update `RELEASE.md` in the same
change, even if the feature has not been released yet. Add it to the existing
unreleased section so the release notes remain current throughout development.

## Releases

Push the exact release commit and wait for its complete remote CI matrix before
dispatching a draft or public release. Use the `just release::draft` recipe for
drafts; its local preflight waits for CI and only dispatches the GitHub release
workflow after CI succeeds. If CI fails, fix the failure, push the new commit,
and run the recipe again. Do not invoke `release.yml` directly or pass
`--skip-ci-check=true` unless the user explicitly requests bypassing the CI
gate.

## Quick iteration

During development, you can build/check subsections of our code:

- Rust: `cargo check`
- Python: `just lint` (runs mypy/ruff), and if wheel-related, `just wheels`
- TypeScript/Svelte: `just lint` (includes check:svelte and check:typescript)

Language-specific tests are also available: `just test-rust`, `just test-py`,
`just test-ts`. Use `just fmt` / `just fix-fmt` for formatting and
`just fix-lint` to auto-fix lint issues.

TypeScript/Svelte browser e2e tests live in `ts/tests/e2e/` and run with
`just test-e2e`. The harness launches a temporary Anki instance and drives
mediasrv pages with Playwright's Chromium.

When a bug involves UI state, focus, event timing, shortcut/click routing,
embedded webviews, or async reviewer transitions, do not rely on unit tests
alone if the behavior remains uncertain. Add or run a targeted runtime/UI
smoke test using the existing harness where possible, such as Playwright e2e,
an offscreen temporary Anki reviewer session, or another small mock UI flow
that exercises the user interaction end to end.

## Testing with the user's collection

By default, when a test needs data from the user's Anki collection, use an
existing backup from
`/Users/jschoreels/Library/Application Support/Anki2/Main Profile/backups/`.
Choose a suitable backup (normally the most recent), copy it to a dedicated
temporary directory under `/private/tmp` or the workspace, and extract or use
the copy there as needed. Never extract, open, or modify the backup in place,
and never point test or analysis tools at the live profile database.

An existing backup may be used without asking the user to quit Anki. If the
test requires collection state newer than the available backups, or requires
modifying or restoring the active profile, ask the user first and follow the
Anki SQLite safety workflow.

Be mindful that some changes (such as modifications to .proto files) may
need a full build with `just check` first.

## Build tooling

`just` recipes wrap our build system (implemented in build/), which takes
care of downloading required deps and invoking our build steps. See the
project `justfile` for the full set of recipes.

## Translations

ftl/ contains our Fluent translation files. We have scripts in rslib/i18n
to auto-generate an API for Rust, TypeScript and Python so that our code can
access the translations in a type-safe manner. Changes should be made to
ftl/core or ftl/qt. Except for features specific to our Qt interface, prefer
the core module. When adding new strings, confirm the appropriate ftl file
first, and try to match the existing style.

## Protobuf and IPC

Our build scripts use the .proto files to define our Rust library's
non-Rust API. pylib/rsbridge exposes that API, and \_backend.py exposes
snake_case methods for each protobuf RPC that call into the API.
Similar tooling creates a @generated/backend TypeScript module for
communicating with the Rust backend (which happens over POST requests).

## Fixing errors

When dealing with build errors or failing tests, invoke 'check' or one
of the quick iteration commands regularly. This helps verify your changes
are correct. To locate other instances of a problem, run the check again -
don't attempt to grep the codebase.

## Ignores

The files in out/ are auto-generated. Mostly you should ignore that folder,
though you may sometimes find it useful to view out/{pylib/anki,qt/\_aqt,ts/lib/generated} when dealing with cross-language communication or our other generated sourcecode.

## Installer

The code for our Briefcase-based installer is in qt/installer, with
separate templates for each platform (mac-template/, linux-template/,
windows-template/).

## Rust dependencies

Prefer adding to the root workspace, and using dep.workspace = true in the individual Rust project.

## Rust utilities

rslib/{process,io} contain some helpers for file and process operations,
which provide better error messages/context and some ergonomics. Use them
when possible.

## Rust error handling

in rslib, use error/mod.rs's AnkiError/Result and snafu. In our other Rust modules, prefer anyhow + additional context where appropriate. Unwrapping
in build scripts/tests is fine.

## Individual preferences

See @.claude/user.md
