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

Given the deck-options screen in Advanced mode (`deck-options.simple-view`),
these controls appear only while FSRS-7 is the selected algorithm: the FSRS
parameters, the Optimize buttons, and the FSRS advanced section (Help Me
Decide, the FSRS version selector, the search filter, Check Health, and the
FSRS simulator). Under RWKV-Curve and RWKV-Instant none of them is shown, in
Simple mode none of them is shown under any algorithm, and the "Compare RWKV
with FSRS" action is gone. One search filter serves both optimization and evaluation; the
separate evaluation filter no longer exists, and a stored
`fsrsEvaluationSearch` value is ignored. FSRS-7 optimization always includes
same-day reviews, always uses scheduling penalties and always weights the
training items by recency (the fsrs crate applies recency weighting
unconditionally): the two switches and "Same-day reviews: Help Me Decide"
are gone, and the stored `fsrs7IncludeSameDayOptimize` /
`fsrs7EnableSchedulingPenalties` values are ignored, also by "Optimize all
presets".

**Why:** Andrew, 2026-09-14: RWKV's parameters are frozen; a proper
RWKV-Instant simulator is out of scope; FSRS-7 must always be optimized
with same-day reviews, recency and scheduling penalties, with no choice.

**Pinned by:** `fsrs7_optimize_always_includes_same_day_reviews`,
`fsrs7_scheduling_penalties_are_always_enabled`
(`rslib/src/deckconfig/update.rs`). The visibility is markup.

## deck-options.reschedule-on-change

Given the collection-wide "Reschedule cards when desired
retention changes" switch (stored as `fsrsReschedule`), shown for every algorithm in
Advanced mode only (`deck-options.simple-view`), and a deck-options save
with it on:

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

There is no separate manual "Reschedule Cards with RWKV-Curve Intervals"
action any more; this switch is the one rescheduling control.

**Why:** Andrew, 2026-09-14: changing desired retention affects dueness for
any algorithm, so one switch, named for that, serves all three algorithms.
Writing FSRS intervals onto RWKV-Curve cards would undo RWKV's intervals, so
those presets get the RWKV reschedule instead.

**Pinned by:** `fsrs_reschedule_skips_rwkv_curve_presets`
(`rslib/src/deckconfig/update.rs`); `test_rwkv_curve_reschedule_*`,
`test_reschedule_rwkv_curve_after_save_runs_only_when_needed`,
`test_rwkv_instant_refresh_needed_*` and
`test_refresh_rwkv_instant_after_save_invalidates_and_resets`
(`qt/tests/test_rwkv_scheduler.py`).

## deck-options.simulator-fsrs-only

Given the deck-options simulator ("FSRS Simulator" and "Help Me Decide"), it
simulates with FSRS only. The RWKV run mode and the FSRS/RWKV comparison no
longer exist, and neither do the RWKV sample cap, DR step and state stride
settings. `SimulateFsrsReviewRequest` carries no `rwkv_workload_*` fields
(numbers 36-38 are reserved), and the desktop no longer serves the
`simulateRwkvWorkload`, `startRwkvWorkload`, `rwkvWorkloadResult`,
`cancelRwkvWorkload` and `rwkvWorkloadProgress` endpoints. RWKV presets
simulate with their FSRS parameters.

**Why:** plan item 6 — RWKV uses many more input features and processes all
cards together instead of independently, so a correct RWKV simulator is out of
scope.

**Pinned by:** `test_post_handler_list_has_no_rwkv_workload_handlers`
(`qt/tests/test_mediasrv.py`); "simulate request carries no RWKV fields"
(`ts/routes/deck-options/simulate-fsrs-request.test.ts`).

## deck-options.reschedule-choice-remembered

Given a deck-options save, the collection stores the value of the
"Reschedule cards when desired retention changes" switch at that save under the collection flag
`fsrsReschedule` (absent or off until the first save with the switch on).
The switch itself still opens off every time, and the save-time rescheduling
it triggers is unchanged (`deck-options.reschedule-on-change`). The stored
value is read by one thing only: the schedule half of the post-sync FSRS
reconcile pass (`sync.post-sync-reschedule-gate`), which runs only while it
is on.

**Why:** the post-sync reconcile must respect the user's rescheduling
opt-out (Andrew's review of upstream PR 4717, 2026-06-20), and the switch is
sent with each save rather than stored, so the last saved choice is the only
record of it.

**Pinned by:** `deck_options_save_remembers_reschedule_on_change_choice`
(`rslib/src/deckconfig/update.rs`).

## deck-options.advanced-view

Given the collection flag `advancedUi` (default off; `spec/ui.md`,
`ui.mode-switch`), the deck-options screen hides the RWKV settings listed
below and shows them only while the flag is on. The page has no switch of
its own; the mode is changed from the main window. Hidden settings keep
their stored values and keep taking effect. With the flag off the whole page
is one section (`deck-options.simple-view`); this entry lists what the RWKV
section shows once the flag is on.

Hidden under every algorithm: the FSRS version selector, so that the FSRS-7
label stays true. (Under RWKV the FSRS controls are not shown at all; see
`deck-options.fsrs-only-controls`.)

Hidden: keep RWKV intervals in answer order; minimum reviews per day; faster
approximate queue updates; queue update interval; update queue after reviewing;
minimum other reviews and minimum seconds before a same-day repeat; predict R
for new cards from creation time; dynamic preset add-on support; the Rebuild
RWKV State and Recompute Calibration actions.

The Algorithm dropdown itself exists only in Advanced mode
(`deck-options.simple-view`); in Simple mode a preset keeps its stored
algorithm (new presets: RWKV-Curve, `deck-options.new-preset-defaults`). The
RWKV section exists only in Advanced mode, under either RWKV mode; the
same-day repeat switch in it shows while RWKV-Instant is selected.

**Why:** plan item 2 — a Simplified view is the default; the remaining RWKV
knobs have defaults that suit nearly everyone.

**Pinned by:** `advanced_ui_flag_is_reported`
(`rslib/src/deckconfig/update.rs`) for the flag plumbing. The visibility
itself is markup and has no unit test.

## deck-options.simple-view

Given the collection flag `advancedUi` off (Simple mode, the default;
`spec/ui.md`, `ui.mode-switch`), the deck-options screen is one section,
titled "Deck Options", with exactly these controls in this order:

1. New cards/day and Maximum reviews/day, each with the preset / deck /
   today tabs;
2. Desired retention and, as in Advanced mode, the First intervals table
   for FSRS-7 and the RWKV-Instant information box
   (`deck-options.first-intervals`) — without the Algorithm dropdown, which
   is Advanced-only;
3. Bury siblings — one switch;
4. Don't play audio automatically;
5. Skip question when replaying answer;
6. On-screen timer — one switch;
7. the Easy Days sliders, collapsed behind an "Easy Days" expander that
   the user opens by clicking it (collapsed in Advanced mode too).

Add-on components render after the section, in both modes.

The two combined switches stand for several stored settings. Bury siblings
reads as on only while `buryNew`, `buryReviews` and `buryInterdayLearning`
are all on; turning it on or off writes all three. On-screen timer reads as
on while `showTimer` is on; turning it on or off writes `showTimer` and
`stopTimerOnAnswer` together. Showing a preset writes nothing: a preset
whose stored settings do not match its switch value (some bury settings on,
or the timer shown without stopping on answer) keeps them until the switch
is toggled. The revert button of each combined switch restores off.

Given the flag on (Advanced mode), the screen has the per-topic sections
(Daily limits, New cards, Lapses, Display order, Algorithm, RWKV, Burying,
Audio, Timers, Auto advance, Easy Days, Advanced) with the three separate
bury switches and the two separate timer settings, and it alone shows: the
Algorithm dropdown, Limits start from top, Learning steps, Insertion order,
Relearning steps, Skip learning/relearning queues, Leech threshold, Leech
action, the whole Display order section, Reschedule cards when changing
desired retention, the FSRS Optimize buttons and the
FSRS advanced section (`deck-options.fsrs-only-controls`), Maximum answer
seconds, the whole Auto advance section, Maximum interval, Minimum interval,
Ignore cards reviewed before, Custom scheduling, and the RWKV settings of
`deck-options.advanced-view`. Hidden settings keep their stored values and
keep taking effect.

**Why:** Andrew, 2026-09-14, plan item 2: Simple mode is one short list of
the settings a new user needs; everything else belongs to Advanced mode.
One bury switch and one timer switch are enough there, because the split
settings only matter to power users.

**Pinned by:** `ts/routes/deck-options/bury-siblings.test.ts`,
`ts/routes/deck-options/timer-switch.test.ts` (the combined switches);
`ts/tests/e2e/deck-options.test.ts` (the FSRS parameters and the Algorithm
dropdown exist only in Advanced mode; Desired retention and Bury siblings
are visible in Simple mode). The section layout itself is markup.

## deck-options.new-preset-defaults

Given a new preset — added on the deck-options screen, created by
`col.decks.add_config()` without a source, or reset with "Restore
defaults" — its learning steps and relearning steps are empty and its
algorithm is RWKV-Curve (`rwkv_review_enabled` on,
`rwkv_review_instant_order_enabled` off); the revert buttons for the steps
restore empty. Given a new collection, its default preset has these values,
so the collection starts on RWKV-Curve. Existing presets keep their stored
values: a stored preset without the RWKV flag still reads as FSRS-7, and
scheduling outcomes for existing presets do not change. (Same-day reviews for
(re)learning steps are always allowed: `sched.same-day-steps-always-on`.)

**Why:** Andrew, 2026-09-14: RWKV-Curve is the algorithm new users should
get, and it needs no learning steps. Existing presets must keep the
assumptions their review histories were built on.

**Pinned by:** `new_preset_has_no_steps_and_runs_rwkv_curve`,
`fresh_collection_starts_with_new_preset_defaults`,
`stored_preset_without_rwkv_flag_stays_off` (`rslib/src/deckconfig/mod.rs`).

## deck-options.historical-retention-fixed

Given any preset, historical retention is 0.9. A memory state inferred from
SM-2 data (a card with no review log, or a truncated one) uses 0.9 whatever
`historical_retention` the preset stores; the FSRS simulator and add-on
preset overlays use 0.9 as well, and the value reported for a preset
(`fsrs_preset_for_card`) is 0.9. Only FSRS-4/5/6 presets ever read the
value: FSRS-7 infers a state from the interval alone. The control is gone
from the screen; the proto field and the stored value stay, and are ignored.

**Why:** Andrew, 2026-09-14: one setting less. The stored value only shaped
memory states inferred from SM-2 data under the older FSRS versions, and
0.9 is the value nearly every preset had.

**Pinned by:** `stored_historical_retention_is_ignored`
(`rslib/src/scheduler/fsrs/memory_state.rs`),
`fsrs_preset_is_derived_from_deck_config`,
`fsrs_preset_overlay_uses_first_matching_rule`
(`rslib/src/scheduler/fsrs/preset.rs`).
