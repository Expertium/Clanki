# Deck options

## deck-options.scheduler-choice

Given the deck-options screen, the algorithm for a preset is chosen from one
dropdown labelled **Algorithm** with the values FSRS-7, RWKV-Curve and
RWKV-Instant. SM-2 is not selectable: the collection `fsrs` switch is on for
every value. The value maps onto the stored flags as follows, and nothing
else:

| Value        | collection `fsrs` | preset `rwkv_review_enabled` | preset `rwkv_review_instant_order_enabled` |
| ------------ | ----------------- | ---------------------------- | ------------------------------------------ |
| FSRS-7       | on                | off                          | off                                        |
| RWKV-Curve   | on                | on                           | off                                        |
| RWKV-Instant | on                | off                          | on                                         |

Two stored states cannot be represented and are normalized when the preset is
shown, so that saving writes the represented state: a preset with both RWKV
flags on reads as RWKV-Curve and `rwkv_review_instant_order_enabled` is
cleared; a collection whose `fsrs` switch is off has it turned on, whatever
the preset's RWKV flags. Presets that are not opened are not touched, and
nothing is written until the user saves. The underlying flags, their storage in the
`jschoreels.rwkv` bag, and the scheduler behavior behind each one are
unchanged; only the way the screen sets them is.

**Why:** the three switches were independent and could be combined in ways
the user did not mean; desired retention was also only editable inside the
FSRS block even though RWKV reads it, which the dropdown resolves by keeping
FSRS on for every RWKV mode.

**Pinned by:** `ts/routes/deck-options/scheduler-choice.test.ts`;
`ts/tests/e2e/deck-options.test.ts` checks that no FSRS switch remains.

## deck-options.first-intervals

Given the deck-options screen with FSRS-7 selected, a **First intervals**
table shows the intervals a new card gets after its first answer (Again,
Hard, Good, Easy) at the current and the selected desired retention. The
follow-up rows of the old "New card intervals at graduation" table are no
longer shown. With RWKV-Curve or RWKV-Instant selected the table is hidden:
RWKV uses more than the first grade, so the table does not describe it.

Given RWKV-Instant selected, the desired-retention information box explains
that RWKV-Instant has no intervals, that desired retention still controls
the workload, and that the number of due cards changes after every review.
The interval-based "desired retention is very low/high" warning is not
shown for RWKV-Instant.

**Why:** Andrew, 2026-09-14: the table only makes sense for FSRS, since it
only uses grades for the first review; the interval warnings do not apply
to RWKV-Instant.

**Pinned by:** markup only; no unit test.

## deck-options.fsrs-only-controls

Given the deck-options screen, these controls appear only while FSRS-7 is the
selected algorithm: the FSRS parameters, the Optimize buttons, and the FSRS
advanced section (Help Me Decide, the FSRS version selector, the search
filter, Check Health, and the FSRS simulator). Under RWKV-Curve and
RWKV-Instant none of them is shown, and the "Compare RWKV with FSRS" action
is gone. One search filter serves both optimization and evaluation; the
separate evaluation filter no longer exists, and a stored
`fsrsEvaluationSearch` value is ignored. FSRS-7 optimization always includes
same-day reviews and never uses scheduling penalties: the two switches and
"Same-day reviews: Help Me Decide" are gone, and the stored
`fsrs7IncludeSameDayOptimize` / `fsrs7EnableSchedulingPenalties` values are
ignored, also by "Optimize all presets".

**Why:** Andrew, 2026-09-14: RWKV's parameters are frozen; a proper
RWKV-Instant simulator is out of scope; the same-day and penalty settings
should be the defaults and never shown.

**Pinned by:** `fsrs7_optimize_always_includes_same_day_reviews`,
`fsrs7_scheduling_penalties_are_never_enabled`
(`rslib/src/deckconfig/update.rs`). The visibility is markup.

## deck-options.reschedule-on-change

Given the collection-wide "Reschedule cards on change" switch, now shown for
every algorithm, and a deck-options save with it on:

- presets running FSRS-7 or RWKV-Instant reschedule with FSRS intervals as
  before, when their parameters or desired retention changed;
- for RWKV-Instant, dueness is also recomputed with the new desired
  retention at once: when such a preset's desired retention changed, when a
  preset became RWKV-Instant, or when the target deck's own override changed
  while the deck keeps an RWKV-Instant preset, the installed RWKV queue
  scores and deck-browser counts are discarded and the study screens
  refresh, so the due counts and the review queue use the new threshold;
- presets running RWKV-Curve are never rescheduled with FSRS intervals (their
  FSRS memory states are still recomputed). Instead, when such a preset's
  desired retention changed, when a preset became RWKV-Curve, or when the
  target deck's own desired-retention override changed while the deck keeps an
  RWKV-Curve preset, the RWKV-Curve reschedule runs over the whole collection
  after the save, with its own progress dialog.

**Why:** Andrew, 2026-09-14: changing desired retention affects dueness for
any algorithm. Writing FSRS intervals onto RWKV-Curve cards would undo RWKV's
intervals, so those presets get the RWKV reschedule instead.

**Pinned by:** `fsrs_reschedule_skips_rwkv_curve_presets`
(`rslib/src/deckconfig/update.rs`); `test_rwkv_curve_reschedule_*`,
`test_reschedule_rwkv_curve_after_save_runs_only_when_needed`,
`test_rwkv_instant_refresh_needed_*` and
`test_refresh_rwkv_instant_after_save_invalidates_and_resets`
(`qt/tests/test_rwkv_scheduler.py`).

## deck-options.advanced-view

Given the collection flag `deckOptionsAdvanced` (default off), the deck-options
screen hides the RWKV settings listed below and shows them only while the flag
is on. The flag is a collection-wide view preference, written immediately when
the switch at the top of the page changes, and is not part of the deck-options
save. Hidden settings keep their stored values and keep taking effect.

Hidden under every algorithm: the FSRS version selector, so that the FSRS-7
label stays true. (Under RWKV the FSRS controls are not shown at all; see
`deck-options.fsrs-only-controls`.)

Hidden: keep RWKV intervals in answer order; minimum reviews per day; faster
approximate queue updates; queue update interval; update queue after reviewing;
minimum other reviews and minimum seconds before a same-day repeat; predict R
for new cards from creation time; dynamic preset add-on support; the Rebuild
RWKV State and Recompute Calibration actions.

The "Reschedule Cards with RWKV-Curve Intervals" action sits in the
Algorithm section while RWKV-Curve is selected. The RWKV section itself is
shown only while RWKV-Instant is selected (the same-day repeat switch) or
while advanced options are on under either RWKV mode.

**Why:** plan item 2 — a Simplified view is the default; the remaining RWKV
knobs have defaults that suit nearly everyone.

**Pinned by:** `deck_options_advanced_flag_is_reported`
(`rslib/src/deckconfig/update.rs`) for the flag plumbing. The visibility
itself is markup and has no unit test.
