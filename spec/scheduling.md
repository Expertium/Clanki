# Scheduling

## sched.sibling-dispersal-gate

Given a card whose home preset has **"Bury review siblings"** enabled and the
load balancer enabled, the day chosen for its next review interval is picked
from the fuzz range with a penalty on days already holding a sibling: ×0.000001
on the sibling's day, ×0.2 / ×0.4 / ×0.6 / ×0.8 at ±1 / ±2 / ±3 / ±4 days.
"Bury new siblings" and "Bury interday learning siblings" have no effect on
fuzz; they only bury cards in the queue.

**Why:** inherited from upstream Anki; users read the three toggles as one
feature, so the asymmetry is recorded here rather than rediscovered.

**Pinned by:** `external_intervals_are_dispersed_away_from_siblings`
(`rslib/src/scheduler/states/load_balancer.rs`) for the penalty and the gate;
the gate's wiring is at `rslib/src/scheduler/answering/mod.rs`
(`review_load_balancer_ctx`).

## sched.rwkv-curve-fuzz

Given a card whose preset has **"Use RWKV-Curve for answer intervals"**
enabled, the unrounded interval RWKV-Curve supplies for each button replaces
FSRS's interval for that button, and the answer states are then built by the
same rules as FSRS intervals (`sched.sub-day-intervals`): a sub-day interval
goes to the intraday queue; an interval of a day or more gets the same review
fuzz, the same load balancer, the same sibling dispersal
(`sched.sibling-dispersal-gate`), the same 90-day load-balance limit, and the
same floors — Again on a review card is clamped but not fuzzed; Hard, Good
and Easy each keep the previous interval when it still lies within the
configured fuzz range; and each day button sits at least one day above the
day button before it. The resulting fuzz delta is recorded on the state and
shown above the answer buttons when that preference is on. This applies to
every card the preset schedules, new and learning cards included. The S90
RWKV-Curve supplies for a button becomes that answer's stability.

Before this entry, RWKV-Curve wrote its interval over the already-fuzzed FSRS
state and set the delta to 0, so RWKV-Curve users got no fuzz and no sibling
dispersal at all.

**Why:** fuzz and sibling dispersal are properties of the _scheduling
outcome_, not of FSRS; switching the interval source must not switch them off.

**Pinned by:** `scheduling_states_with_intervals_apply_the_fsrs_rules`
(`rslib/src/scheduler/answering/mod.rs`),
`external_intervals_are_dispersed_away_from_siblings`
(`rslib/src/scheduler/states/load_balancer.rs`);
`test_rwkv_curve_states_come_from_the_backend_with_unrounded_intervals`,
`test_rwkv_curve_states_only_send_supplied_ratings`,
`test_reviewer_rwkv_curve_intervals_go_through_review_fuzz`
(`qt/tests/test_rwkv_scheduler.py`).

## sched.rwkv-instant-no-intervals

Given a card whose home preset runs RWKV-Instant (`rwkv_review_instant_order_enabled`
on and `rwkv_review_enabled` off, `deck-options.scheduler-choice`), the
answer buttons show no next-review interval and no fuzz delta, whatever the
"Show next review time above answer buttons" preference says. The buttons,
their labels and shortcuts, and the states stored on answer (FSRS states,
whose due dates RWKV-Instant reorders) are unchanged. Cards of FSRS-7 and
RWKV-Curve presets show their intervals as before.

**Why:** Andrew, 2026-09-15: RWKV-Instant has no intervals, so none may be
shown above its answer buttons, ever; the FSRS intervals it showed before
mixed a second algorithm into the screen.

**Pinned by:** `test_answer_buttons_show_no_intervals_for_rwkv_instant`
(`qt/tests/test_reviewer.py`),
`test_one_algorithm_per_preset_both_rwkv_modes_read_as_curve`
(`qt/tests/test_rwkv_scheduler.py`).

## sched.sub-day-intervals

Given a card scheduled by FSRS-7 or RWKV-Curve and the unrounded interval
each answer button would give it (FSRS's, or RWKV-Curve's per
`sched.rwkv-curve-fuzz`), every button of every card — new, learning,
relearning or review — is decided the same way, in the order Again, Hard,
Good, Easy:

- a button decided by a remaining learning or relearning step keeps the
  step's delay and takes no part in what follows;
- a button whose unrounded interval is under one day (24 hours) goes to the
  intraday learning queue with that interval in seconds, unrounded and
  without review fuzz (at least the preset's minimum interval, 1 second by
  default), and at least as long as the sub-day button before it; a
  learning card stays learning, and a review or relearning card becomes a
  relearning card with no remaining steps (a passing answer keeps its lapse
  count, and the card's interval field holds a whole number of days, at
  least 1);
- a button of one day or more gets whole days after review fuzz, and at
  least one day more than the day button before it: with all four at a day
  or more, Hard ≥ Again + 1, Good ≥ Hard + 1 and Easy ≥ Good + 1.

For RWKV-Curve the answer curves are searched inside the first day as well
(at 1, 5, 10, 20 and 30 minutes and 1, 2, 3, 4, 6, 8, 12, 16 and 20 hours)
when a curve reaches its target before day 1, with linear interpolation
between points as for later days; rounded up to whole days these unrounded
intervals equal the whole-day intervals RWKV computed before. FSRS-7 uses
the intraday queue whatever its parameters, and so does RWKV-Curve
(`sched.fsrs7-only`: there is no other FSRS model). Once a card has had its
preset's maximum of
same-day reviews for the day (`sched.max-same-day-reviews`) there is no
intraday queue for it, and a sub-day interval rounds up to one day.

**Why:** Andrew, 2026-09-15: both FSRS-7 and RWKV-Curve should freely
schedule intervals under a day for any card and any answer button; the
ordering rule for mixed sub-day and day buttons is the one he approved.
Before this entry only learning and relearning answers under half a day
went intraday, a review card's Again never did (it was clamped to the
minimum lapse interval first), and RWKV-Curve rounded up to whole days.

**Pinned by:** `button_intervals::test::*`
(`rslib/src/scheduler/states/button_intervals.rs`),
`scheduling_states_with_intervals_apply_the_fsrs_rules`
(`rslib/src/scheduler/answering/mod.rs`),
`unrounded_answer_intervals_round_up_to_the_day_intervals`,
`a_fast_forgetting_curve_gives_a_sub_day_interval`,
`unrounded_intervals_are_not_rounded_after_the_first_day`
(`rslib/src/rwkv/mod.rs`); the existing learning, relearning and review
state tests; `test_rwkv_curve_states_*` and
`test_unrounded_interval_from_recall_curve_keeps_sub_day_crossings`
(`qt/tests/test_rwkv_scheduler.py`).

## sched.rwkv-relative-overdueness

Given a preset running RWKV-Curve or RWKV-Instant whose review sort order is
"Relative overdueness", when the study queue gathers due review cards and
interday learning cards that it does not rank by RWKV-Instant queue scores,
it ranks them by RWKV's own measure, lowest first, and applies the daily
limits in that order. A card with an RWKV-Curve retrievability score for
today gets that retrievability divided by its target retention (the card's
desired retention, else the preset's), the key RWKV-Instant ranks its
scores by. A card without a score gets the value of the exponential
forgetting curve through the interval RWKV scheduled for it:
target ^ (days since the last review / interval − 1) — 1 when the card is
due exactly, less the more it is overdue. Ties go by a hash of card id and
modification time, then card id. The FSRS memory state plays no part.

**Why:** Andrew, 2026-09-15: only one algorithm at a time, and asked which
measure the order should use, he chose "RWKV curve scores" (falling back to
RWKV's interval for unscored cards). Before this entry the order came from
an SQL function that applied a one-component FSRS forgetting curve to the
card's FSRS-7 internal stability — neither RWKV's measure nor FSRS-7's.

**Pinned by:** `rwkv_curve_relative_overdueness_uses_rwkv_not_fsrs`,
`rwkv_curve_relative_overdueness_without_scores_uses_the_rwkv_interval`
(`rslib/src/scheduler/queue/builder/mod.rs`).

## sched.max-same-day-reviews

Given a card whose preset is scheduled by FSRS (any version, RWKV-Curve and
RWKV-Instant included), has no learning steps, and has a "Max number of
same-day reviews" N, and k, the number of the card's reviews logged since
the start of the current day (answers 1-4, not counting filtered-deck
reviews that did not reschedule):
when k ≥ N, every answer button schedules the card as it would with no
learning or relearning queue — remaining learning and relearning steps are
skipped, and an interval under one day rounds up to one day — so the card
does not come back today. When k < N the steps and sub-day intervals apply
as usual (`sched.sub-day-intervals`). A same-day review is a review after
the card's first review of the day, so N = 0 means a card never comes back
on the day it was studied, and N = 1 allows one return. The start of the
day is the day rollover ("Next day starts at"). RWKV-Curve intervals go
through the same rule (`sched.rwkv-curve-fuzz`). A preset with learning
steps has no limit, whatever N it stores: its steps decide the same-day
reviews, and its relearning steps apply as usual. A preset with no stored
value has no limit; the deck-options row, in Advanced mode under Learning
steps, shows only while Learning steps is empty, shows the unset value as
9999, and editing it stores the number shown. The First intervals preview
of a new card treats only N = 0 as a limit, since a new card has no
reviews yet. The limit is stored with the preset and syncs with it. SM-2
presets are unaffected.

The collection-wide "Skip learning/relearning queues with FSRS/RWKV"
Preferences switch is gone. When a collection that has it on is opened,
every preset gets a limit of 0 and the switch is cleared, so this happens
once and a later change to a preset's limit stays. A preset with learning
steps keeps its steps after this, where the switch skipped them.

**Why:** Andrew, 2026-09-15: the switch was hard to understand; a
per-preset limit on same-day reviews replaces it, and 0 gives the old
behavior. The limit is for presets without learning steps, because with
steps the steps already decide the same-day reviews; it applies to
RWKV-Curve as well as FSRS.

**Pinned by:** `max_same_day_reviews_limits_intraday_answers`,
`max_same_day_reviews_limits_rwkv_curve_intervals`,
`fsrs_learning_queue_bypass_keeps_rwkv_relearning_answer_in_review_queue`
(`rslib/src/scheduler/answering/mod.rs`);
`learning_queues_switch_becomes_a_zero_limit_on_open`,
`max_same_day_reviews_survives_storage_and_schema11`
(`rslib/src/deckconfig/mod.rs`); `ts/routes/deck-options/same-day-reviews.test.ts`.

## sched.fsrs7-only

Given any preset, FSRS-7 is the FSRS model that schedules it; Clanki has no
other. The preset runs with its stored FSRS-7 parameters when they are 34
finite values, and otherwise with the FSRS-7 default parameters (the same
values as srs-benchmark's FSRS-7 `init_w`) — so a preset that was never
optimized runs FSRS-7 with the defaults, even when it holds trained FSRS-6,
FSRS-5 or FSRS-4.5 parameters. The stored FSRS version and the FSRS-6/5/4.5
parameter sets are kept in the collection unchanged (other clients read
them) and are ignored everywhere: answering, memory states, rescheduling,
the simulator, and the retrievability shown in the browser and card info.
There is
no FSRS version selector. Add-on preset overlays are FSRS-7 too: an overlay
whose parameters are not 34 finite values runs with the FSRS-7 defaults (its
`fsrs_version` is still accepted and ignored). Training ("Optimize All
Presets", Evaluate, Check Health) always fits FSRS-7 and always includes
same-day reviews with the exact elapsed time; the `fsrs_version` and
`include_same_day_reviews*` request fields are ignored. Optimizing writes
the FSRS-7 parameter set only. Rescheduling on a desired-retention change
computes each card's interval from its whole FSRS-7 memory state (internal
and fast stability, difficulty).

When a collection is opened for the first time with this rule, every card of
a preset whose parameters changed with it (never optimized, or running an
older version's parameters) gets its memory state and decay computed again
with the FSRS-7 parameters it now runs with; due dates are not changed. This
happens once (`fsrs7OnlyMigrated`).

**Why:** Andrew, 2026-09-15: "unoptimized FSRS-7 should use default
parameters of FSRS-7"; he chose this for presets with trained FSRS-6
parameters too, and asked to cut out FSRS-6 code. Before this entry, a
preset that showed as FSRS-7 without FSRS-7 parameters ran the FSRS-6 model
(its FSRS-6 parameters, or the FSRS-6 defaults) on whole days, and the
reschedule treated a card's S90 as its only stability.

**Pinned by:** the `fsrs7_only_*` tests in `rslib/src/deckconfig/mod.rs`
and `rslib/src/deckconfig/update.rs` (including the migration);
`ts/routes/deck-options/fsrs-params.test.ts`,
`ts/routes/deck-options/fsrs-param-diagnostics.test.ts`.

## sched.fsrs7-fractional-elapsed-time

Given a card answered with FSRS (always FSRS-7, `sched.fsrs7-only`), the
elapsed time FSRS uses for the answer's
retrievability and next states is the exact time since the card's last
review, in fractional days, for every card: new, learning, relearning and
review, in any queue. This is the same elapsed time training and the
memory-state rebuild take from the review log (millisecond timestamps), so
the model sees the same kind of input when it is trained and when it is
used. The day rollover plays no part in it.

**Why:** Andrew, 2026-09-15: FSRS-7 must use fractional, not integer,
interval lengths as inputs, both in training and in deployment. Before this
entry, review and interday-learning cards got whole days from the rollover
(a review Monday 23:00 answered Wednesday 05:00 with a 04:00 rollover was
2 days, not 1.25), while training used the exact 1.25.

**Pinned by:** `fsrs7_gets_fractional_elapsed_time_like_training`,
`fsrs7_review_answer_uses_the_exact_elapsed_time`
(`rslib/src/scheduler/answering/mod.rs`);
`fsrs7_interday_delta_uses_fractional_elapsed_time`,
`fsrs7_same_day_delta_uses_fractional_elapsed_time`
(`rslib/src/scheduler/fsrs/params.rs`) for the training side.

## sched.no-dynamic-desired-retention

Given a deck preset (or an add-on FSRS preset overlay) that stored dynamic
desired retention ("ADR") settings — the `fsrs_dynamic_desired_retention_*`
keys inside the `jschoreels.fsrs` fork-fields blob, the matching camelCase keys
in legacy schema11 JSON, or proto fields 55-65 of `DeckConfig.Config` — the
collection loads the preset without error, ignores those settings, and
schedules every card with the preset's (or deck's) fixed desired retention: the
FSRS next states for Again/Hard/Good/Easy are computed for that single
retention, and the card's stored `desired_retention` is set to it on answer.
The legacy keys are dropped the next time the preset is saved. The ADR controls
in deck options, the ADR fields on the scheduling and optimizer RPCs, the ADR
simulator mode, the ADR plot page and the ADR add-on hooks no longer exist.

**Why:** plan item 6 in `CLAUDE.md` — remove Adaptive/Dynamic Desired
Retention from Clanki (Andrew, 2026-09-14).

**Pinned by:** `legacy_dynamic_desired_retention_fork_fields_are_ignored`
(`rslib/src/deckconfig/fork_fields.rs`),
`legacy_dynamic_desired_retention_keys_load_and_are_dropped`
(`rslib/src/deckconfig/schema11.rs`),
`legacy_dynamic_desired_retention_preset_schedules_with_fixed_desired_retention`
(`rslib/src/scheduler/answering/mod.rs`).

## sched.fuzz-always-on

Given any collection, review fuzz and the load balancer are always active.
The stored "review fuzz enabled" flag and the collection `loadBalancerEnabled`
flag are ignored when read, so a `false` written by an earlier build has no
effect; the fuzz factors (base and the three interval-band factors) keep their
stored values but have no controls, and the deck-options Easy Days section
shows only the Easy Days controls. Sibling dispersal and the fuzz applied to
RWKV-Curve intervals (`sched.rwkv-curve-fuzz`) are unchanged.

**Why:** Andrew, 2026-09-14: fuzz and load balancing should always be
enabled; only Easy Days is the user's choice.

**Pinned by:** `collection_review_fuzz_ignores_the_disabled_flag`
(`rslib/src/scheduler/states/fuzz.rs`), `load_balancer_is_always_on`
(`rslib/src/config/bool.rs`).


## sched.same-day-steps-always-on

Given any collection, same-day reviews for (re)learning steps are always
allowed under FSRS: the collection flag `fsrsShortTermWithStepsEnabled` reads
as on whatever value is stored, so a `false` written by an earlier build or
by an add-on has no effect, and the "Allow same-day review for (re)learning
steps" switch is gone from the deck-options screen. The new-card interval
preview also always assumes it on. A preset's "Max number of same-day
reviews" can still keep a card out of the intraday queue
(`sched.max-same-day-reviews`).

**Why:** Andrew, 2026-09-14: the setting should be on for everyone and not
be a choice.

**Pinned by:** `same_day_steps_are_always_allowed`
(`rslib/src/config/bool.rs`); `fsrs_short_term_with_steps_flag_roundtrip`
(`rslib/src/deckconfig/update.rs`, a save that writes off still reads on).

## sched.new-cards-never-ignore-review-limit

Given any collection, new cards always count against the review limit: the
collection flag `newCardsIgnoreReviewLimit` reads as off whatever value is
stored, so a `true` written by an earlier build has no effect, the "New cards
ignore review limit" switch is gone from the deck-options screen and from the
FSRS simulator, and the simulator ignores the matching request field.

**Why:** Andrew, 2026-09-14: the new-card limit and the review limit are
enough; the extra switch is not needed.

**Pinned by:** `new_cards_never_ignore_the_review_limit`
(`rslib/src/config/bool.rs`); `new_cards_never_ignore_review_limit`
(`rslib/src/scheduler/queue/builder/mod.rs`).

## sched.reschedule-no-revlog

Given "Reschedule cards when desired retention changes" applying new FSRS intervals on a
deck-options save, or the RWKV-Curve reschedule, the affected cards get a new
interval, due date, memory state and desired retention, and no row is added
to the review log. Rows of kind `Rescheduled` that older builds wrote are
still read (statistics keep excluding them). "Set Due Date" and "Forget" are
not rescheduling and still write their `Manual` rows.

**Why:** plan item 3 (Andrew): rescheduling must not write to the card's
history, as the FSRS Helper add-on does it. The rescheduled rows carried no
answer and only cluttered the history and the review count.

**Pinned by:** `reschedule_on_change_writes_no_revlog_rows`
(`rslib/src/scheduler/fsrs/memory_state.rs`).
