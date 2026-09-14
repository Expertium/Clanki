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
   Done 2026-09-13: RWKV-Curve intervals now go through the same fuzz, load
   balancer and sibling dispersal as FSRS intervals (`spec/scheduling.md`,
   `sched.rwkv-curve-fuzz`). Before that, RWKV-Curve overwrote the fuzzed
   state and zeroed the delta, so making RWKV the default would have switched
   fuzz off for everyone.
2. **UI split: Simplified / Advanced**, in the SuperMemo style. **Simplified is
   the default.** Many settings get hidden. The current deck-options UI is far
   too complex, even by the standards of Anki power users. Hiding a setting is a
   UI change, not a behavior change — the underlying setting keeps working.
   Done 2026-09-14: the deck-options page in Simple mode is one section
   (`ts/routes/deck-options/SimpleOptions.svelte`; `spec/deck-options.md`,
   `deck-options.simple-view`): New cards/day, Maximum reviews/day, Algorithm
   with desired retention (plus the FSRS-7 First intervals table, the
   RWKV-Instant box and the RWKV-Curve reschedule button), one Bury siblings
   switch (all three bury settings), Don't play audio automatically, one
   On-screen timer switch (show + stop on
   answer), Easy Days. Advanced mode keeps the per-topic sections. Alongside:
   new presets have empty steps and run RWKV-Curve
   (`deck-options.new-preset-defaults`; the Rust and Python test fixtures
   `Collection::new()` / `getEmptyCol()` restate the upstream SM-2 preset so
   the upstream tests keep their assumptions), and historical retention is
   fixed at 0.9 (`deck-options.historical-retention-fixed`). Later the same
   day: "Allow same-day review for (re)learning steps" is always on and
   "New cards ignore review limit" is always off, both controls removed
   (`spec/scheduling.md`, `sched.same-day-steps-always-on`,
   `sched.new-cards-never-ignore-review-limit`); the Algorithm dropdown is
   Advanced-only, the Easy Days sliders sit behind a collapsed expander, and
   the one rescheduling control is the "Reschedule cards when desired
   retention changes" setting (every algorithm; the manual RWKV-Curve
   reschedule button is gone). Then: the four collection-wide settings
   (Limits start from top, Skip learning/relearning queues, the reschedule
   choice, Custom scheduling) moved to Preferences > Review
   (`deck-options.collection-wide-in-preferences`; the deck-options save
   ignores their request fields), "Optimize Current Preset" and the
   health-check switch are gone, and "Optimize All Presets" shows in both
   modes (`deck-options.fsrs-only-controls`).
3. **Rescheduling must not write to the card's history.** Done 2026-09-14:
   the FSRS "reschedule cards on change" path no longer logs a `Rescheduled`
   review-log row (the RWKV-Curve reschedule never did). See
   `spec/scheduling.md`, `sched.reschedule-no-revlog`.
4. **Native AnkiConnect.** Integrate the functionality of
   https://github.com/JSchoreels/anki-connect into the core, so it does not need
   to be installed as an add-on. Its HTTP API surface is a hard compatibility
   boundary: existing AnkiConnect clients must keep working.
5. **A few Search Stats Extended graphs.** Port a **handful** of the graphs from
   https://github.com/JSchoreels/Anki-Search-Stats-Extended — deliberately **not**
   all of them. Ask which ones before porting; picking the subset is a product
   decision, not an implementation detail.
6. Many smaller changes and tweaks.
7. **A big speed overhaul of Clanki** (Andrew, 2026-09-14). Every claimed
   speedup of any part of the code must follow this measurement protocol:
   1. Isolate the part under test. Never benchmark it inside a running Anki
      copy; drive the isolated function or module directly.
   2. Run "before" and "after" **in parallel**, each on one CPU thread with
      the worker pinned to its own core, so that any outside disturbance
      (another process, thermal or power events) hits both runs alike
      instead of one of them. Lock the CPU frequency first with a PowerShell
      command (`powercfg` min/max processor state to the same value; restore
      it afterwards).
   3. Collect at least 100 independent measurements for "before" and 100 for
      "after".
   4. Decide with the Wilcoxon signed-rank test on the paired measurements.
      Accept a speedup only when the p-value is below 0.01 **and** the
      median speedup is at least 2.5%. Anything else is "no change", however
      promising it looks.

   And a rule on what a speedup may change in the outputs:
   - Bit-exact speedups (the output does not change at all) are always
     welcome.
   - A speedup that is not bit-exact but only causes minor output differences
     that do not matter in practice is fine, provided you are sure nothing
     breaks in a subtle way. Always ask yourself "Is there an edge case where
     this would blow up?" and check it before applying.
   - A speedup that is not bit-exact and changes the outputs significantly is
     applied only after consulting Andrew.

## Changes already made in Clanki

- Ported upstream PR 4717 (FSRS sync reconciliation, JSchoreels) with
  Andrew's 2026-06-20 review fixes (2026-09-14): after a normal sync the
  client rebuilds the FSRS data of conflicting cards from the merged review
  log (`spec/sync.md`). The schedule half is gated by the remembered
  "Reschedule cards when desired retention changes" choice (`BoolKey::FsrsReschedule`; a
  Preferences setting since 2026-09-14, before that written on deck-options
  save while the switch itself was never persisted), never fires on
  a pure deck move, and restores the last real review's interval instead of
  recomputing with `next_interval`, so it needs no `Rescheduler` at all;
  agreed memory state survives an itemless reconcile; a forgotten card stays
  forgotten; no review-log rows are written. Wire protocol unchanged.
- Branding (2026-09-14): the visible product name is `aqt.APP_NAME` =
  "Clanki" (window titles, dialogs, About, installer `formal_name`, English
  ftl strings about the running app). The version string add-ons read stays
  the official Anki release number, and the data folder stays `Anki2`
  (`spec/branding.md`). When merging upstream, re-run the ftl rename rule in
  the spec rather than hand-editing strings.
- Removed Dynamic Desired Retention (ADR) end to end (2026-09-14): proto
  fields reserved, `rslib/src/scheduler/fsrs/dynamic_desired_retention.rs`,
  the deck-options controls, the simulator mode, the plot page and the add-on
  hooks are gone. Legacy presets still load (`spec/scheduling.md`,
  `sched.no-dynamic-desired-retention`). The `fsrs` crate dependency stays.
- The simulator is FSRS-only (2026-09-14): the RWKV workload simulation path
  is gone end to end (`SimulatorModal.svelte` run mode and FSRS/RWKV
  comparison, the `rwkv_workload_*` request fields, now reserved 36-38, the
  `*RwkvWorkload` mediasrv handlers and the Python simulation in
  `qt/aqt/rwkv_scheduler.py`). A correct RWKV simulator is out of scope
  (`spec/deck-options.md`, `deck-options.simulator-fsrs-only`).
  `scheduler::rwkv::relative_overdueness` is a shared review-order helper and
  stays. The Rust `rwkv::simulate_workload` and its `rsbridge` binding are
  now unreachable from Python but were left for the RWKV session to remove.
- Removed `+fsrs7` from the version name. `.version` now tracks the official
  Anki release (`26.09` since the 26.09 merge; it was `26.09b1+fsrs7`).
  Note: `qt/tests/test_update.py` still hardcodes
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
schema, the sync **wire protocol**, the add-on API surface, and scheduling
outcomes that users' review histories are built on. Treat all four as behavior.

"Sync wire protocol" means exactly three things: the serialized shapes
(`Chunk`, `UnchunkedChanges`, `SyncMeta`, graves), the protocol version
constants in `rslib/src/sync/version.rs`, and the collection schema on full
sync. Those are what AnkiWeb sees. They must not change, or AnkiWeb sync dies.
**Client-side merge logic is fair game** — what the client does with rows after
it receives them (`apply_chunk`, `merge_cards`, post-sync reconcile passes).
The JSchoreels fork already diverges there (observation hooks feeding
`remote_review_ids` to the RWKV cache), and AnkiWeb cannot tell. Merge logic is
still observable behavior, because it decides DB contents after a sync, so it
needs a `spec/sync.md` entry and pinning tests like anything else.

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
- **The build needs a real `rsync`.** `build/runner/src/rsync.rs` calls
  `Command::new("rsync")` — the runner's `rsync` subcommand is only a thin
  wrapper around the external tool, not a reimplementation. Without it the
  `qt:aqt:data:web:sveltekit` and `qt:aqt:data:web:js:vendor:mathjax` steps fail.
  MSYS2 is installed at `C:\msys64` and provides it (rsync 3.5.0).
  **Append** `/c/msys64/usr/bin` to `PATH`, never prepend it, so that MSYS2 does
  not shadow Git Bash's own tools.
- Working build command, from a Git Bash shell at the repo root:

  ```bash
  export PATH="$HOME/.cargo/bin:$PATH:/c/msys64/usr/bin"
  export COREPACK_ENABLE_DOWNLOAD_PROMPT=0
  ./ninja pylib qt
  ```

  The detached wrapper that runs this is `C:\Users\Andrew\clanki-logs\build3.cmd`.
- **`./run` does not work on Windows.** The `run` shell script calls
  `${PYENV}/bin/python`, which is a Unix venv layout; here the interpreter is at
  `out/pyenv/Scripts/python.exe`. Launch the built app like this instead:

  ```bash
  out/pyenv/Scripts/python tools/run.py -b C:/Users/Andrew/clanki-devbase
  ```

- **Always pass `-b`.** Every Anki build on this machine shares the default base
  folder `%APPDATA%\Anki2`, which holds Andrew's real 210 MB collection and his
  add-ons. A dev build can upgrade the collection schema, after which older
  builds refuse to open it. `-b` (or the `ANKI_BASE` env var) points the dev
  build at a throwaway base — `C:\Users\Andrew\clanki-devbase`. The installed
  JSchoreels build is often running and holding that collection open.
- The section below ends with a reference to `@.claude/user.md`, which does not
  exist in this repo.
- **`./ninja check` has one step that fails on this PC for reasons unrelated
  to the code.** `check:format:dprint` fetches plugins from plugins.dprint.dev
  (Cloudflare) on first run and can sit for 30+ minutes with open sockets and
  zero CPU on this connection; run the check without it and format Rust with
  `check:format:rust` (which uses the repo's pinned _nightly_ rustfmt — stable
  `cargo fmt` ignores `group_imports` and passes code that nightly rejects).
- **The installer templates are git submodules.** `qt/installer/windows-template`
  and `mac-template` are empty in a fresh clone until
  `git submodule update --init -- qt/installer/windows-template qt/installer/mac-template`.
  Without them `qt/tests/test_installer.py` fails inside Briefcase with
  "Unable to clone application template" (exit status 200). The `ftl/*-repo`
  submodules are handled by the build itself.
- **Close the running Clanki before a build that touches PyQt.** `uv sync`
  cannot replace `out/pyenv/.../PyQt6/Qt6/resources/*.bin` while the app holds
  them, and the `pyenv` step fails with "used by another process" (os error 32).
- **Detached builds must never be able to prompt.** The first build here hung for
  two hours at the `node_modules` step: corepack wanted to ask "download
  yarn@4.11.0?" and waited forever on a stdin that a detached process does not
  have. Set `COREPACK_ENABLE_DOWNLOAD_PROMPT=0` and redirect stdin from `NUL`,
  so any future prompt fails fast instead of hanging silently. Judge a detached
  build by **CPU time deltas**, not by whether the processes still exist — a
  hung build looks alive.
- **Never run `./ninja` from the Claude Code Bash or PowerShell tool.** Inside
  the tool sandbox n2 cannot spawn `out/rust/release/runner.exe` by its
  forward-slash relative path and every build dies at `build:configure` with
  "CreateProcessA: The system cannot find the file specified". Run builds and
  `./ninja check:*` targets only through a detached `.cmd` wrapper
  (`C:\Users\Andrew\clanki-logs\buildNN.cmd`, one new number per run,
  launched with `rwkv-anki-autoresearch\scratchpad\detach.ps1`) and wait for
  the `DONE_EXIT_` line in its log. `cargo test`/`clippy`/`fmt`, `pytest`,
  `ruff` and `mypy` run fine from the tool directly. Run `format:prettier` in
  its own wrapper before `check:format:prettier`: in one ninja run the check
  races the formatter and fails on files it is about to rewrite.

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

## Testing guidance

Before adding or changing unit or component tests, read and follow the
[Writing Unit Tests for Anki](docs-site/developers/unit-testing.mdx).

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
