# spec/

This directory holds the **current intended behavior** of Clanki, in natural
language, organized by behavior domain rather than by code file. Entries state
what the system does **now** — they carry no history. The git history of this
directory is the change log.

## Baseline

**Any behavior with no entry in this directory is expected to match the fork
point below.** Upstream is the implicit spec; `spec/` accumulates only
divergences and areas we have touched. Do not backfill beyond that.

|                            |                                            |
| -------------------------- | ------------------------------------------ |
| Fork point                 | `796f0140a5b66f212a905b6520ab1924516c7451` |
| Fork point subject         | Test portable installer parsing as macOS   |
| Forked from                | https://github.com/JSchoreels/anki/        |
| Anki version at fork point | 26.09b1                                    |

## Entry format

One behavior per entry, four parts:

```markdown
## sched.fuzz-interval

When the scheduler computes an interval of 3 days or more, it applies a random
fuzz of ±5% (minimum ±1 day), seeded per card. Intervals under 3 days get no fuzz.
**Why:** cards introduced together would otherwise stay synchronized forever.
**Pinned by:** `test_fuzz_bounds`, `test_no_fuzz_short_intervals`
```

A stable ID heading, a testable "given X, the system does Y" statement, a
**Why:** line, and the tests that pin it. If you cannot state it as
"given X, the system does Y", the entry is too vague — split it.

## Files

Create these lazily, when a domain first gains an entry:
`scheduling.md`, `sync.md`, `deck-options.md`, `import-export.md`,
`addon-api.md`, `database.md`.

The full rules live in the repo root `CLAUDE.md`.
