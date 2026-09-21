# Deck options

## deck-options.scheduler-choice

Given the deck-options screen in Advanced mode, the collection's algorithm
(`sched.one-global-algorithm`) is chosen from one dropdown titled
**Algorithm (global)** and the blue globe (`ui.global-marker`), with the
values FSRS-7, RWKV-Curve and RWKV-Instant. It shows the
same value on every preset: choosing a value gives every preset on the page
that algorithm, and the save makes it the collection's algorithm
(`sched.algorithm-change-prompt`). Simple mode has no Algorithm dropdown. A
save that only changes a preset's flags cannot change its algorithm: the
saved presets take the collection's, and the collection `fsrs` switch stays
on. SM-2 is not selectable. Each algorithm corresponds to these stored
flags, and nothing else:

| Value        | collection `fsrs` | preset `rwkv_review_enabled` | preset `rwkv_review_instant_order_enabled` |
| ------------ | ----------------- | ---------------------------- | ------------------------------------------ |
| FSRS-7       | on                | off                          | off                                        |
| RWKV-Curve   | on                | on                           | off                                        |
| RWKV-Instant | on                | off                          | on                                         |

One algorithm schedules a preset at any time. A preset stored with both RWKV
flags on (written by an older build or an add-on) is RWKV-Curve everywhere:
whenever the collection loads it — from the database or from sync JSON — the
backend reads `rwkv_review_instant_order_enabled` as off, and so does the Qt
code that reads the stored JSON, so no RWKV-Instant queue order, due count,
search or answer-button rule applies to it. The stored flag itself is
cleared the next time the preset is saved. A collection whose `fsrs` switch
is off has it turned on when the deck-options screen shows a preset, and
nothing is written until the user saves. The underlying flags, their
storage in the `jschoreels.rwkv` bag, and the scheduler behavior behind
each single algorithm are unchanged. A preset whose flags do not match the
collection's algorithm shows that algorithm and takes it on the next save.

In the open dropdown each algorithm shows a short description under its
name (FSRS-7: each card's grades and the time between reviews only, the least
accurate; RWKV-Curve: the default, a neural network that uses more
information but not card content, with intervals like FSRS-7;
RWKV-Instant: the same network, best at keeping retention at the desired
level, no intervals, dueness decided again after each review, possibly
unintuitive). The revert button restores RWKV-Curve, the algorithm of a new
collection (`deck-options.new-preset-defaults`).

**Why:** the three switches were independent and could be combined in ways
the user did not mean; desired retention was also only editable inside the
FSRS block even though RWKV reads it, which is resolved by keeping FSRS on
for every RWKV mode. Andrew, 2026-09-15: there must only be one algorithm at
any given time, and each choice needs a description. Later the same day:
the choice stays global but lives in deck options, not Preferences, because
people open deck options far more often; the "(global)" mark and the globe
say that it is not a per-preset setting.

**Pinned by:** `ts/routes/deck-options/scheduler-choice.test.ts`;
`deck_options_save_cannot_change_the_algorithm`,
`deck_options_show_and_change_the_algorithm`
(`rslib/src/deckconfig/algorithm.rs`);
`ts/tests/e2e/deck-options.test.ts` checks that no FSRS switch remains;
`a_preset_stored_with_both_rwkv_modes_reads_as_rwkv_curve`
(`rslib/src/deckconfig/fork_fields.rs`);
`test_one_algorithm_per_preset_both_rwkv_modes_read_as_curve`
(`qt/tests/test_rwkv_scheduler.py`).

## deck-options.rwkv-fixed-settings

Given any preset, RWKV predicts a new card's retrievability before its first
learning review from the time since the card was created, and replays a
card's history with its current preset only: "Predict R for new cards based
on creation time" is always on and "Dynamic Preset Addon Support" always
off. Neither has a control in deck options any more (nor the "New Cards"
and "Card History" headings above them), and a value stored in a preset,
from an older build or another client, is ignored. The stored field itself
is kept, so older clients read their own value.

**Why:** Andrew, 2026-09-15: remove both from deck options; the dynamic
preset add-on support is not needed, and the card's creation date will
become a proper RWKV input feature later.

**Pinned by:** `test_rwkv_first_review_elapsed_from_card_creation_is_always_on`,
`test_rwkv_review_input_uses_card_creation_even_if_stored_off`,
`test_reviewer_rwkv_cache_survives_a_stored_creation_elapsed_change`,
`test_reviewer_rwkv_warmup_ignores_stored_dynamic_preset_replay`
(`qt/tests/test_rwkv_scheduler.py`).

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

## deck-options.optimize-all-clears-bad-params

When the user starts "Optimize All Presets" and one or more presets hold FSRS
parameters that the optimizer cannot use, Clanki replaces those parameters with
the defaults and optimizes. It asks no question and shows no dialog. Presets
with usable parameters keep them.
**Why:** the optimizer cannot run on parameters it refuses, and the defaults are
the only other value it can start from, so the question had one sensible answer
(Andrew, 2026-09-16).
**Pinned by:** `optimize_all_presets_clears_the_parameters_it_cannot_use` in
`ts/routes/deck-options/lib.test.ts`

## deck-options.fsrs-only-controls

Given the deck-options screen, these controls appear only while FSRS-7 is
the selected algorithm: the "Optimize All Presets" button (in Simple mode
and in Advanced mode) and, in Advanced mode only (`deck-options.simple-view`),
the FSRS parameters (FSRS-7 only, `sched.fsrs7-only`; there is no version
selector) and the FSRS advanced section (Help Me Decide, the search filter,
Check Health, Evaluate where enabled,
and the FSRS simulator). Under RWKV-Curve and RWKV-Instant none of them is
shown, and the "Compare RWKV with FSRS" action is gone. "Optimize All
Presets" is the one optimize action: "Optimize Current Preset", its
optimization-result comparison, the custom decay table and the "Check
health when optimizing" switch no longer exist (Check Health stays; a stored
`fsrsHealthCheck` value is ignored). One search filter serves both
optimization and evaluation; the separate evaluation filter no longer
exists, and a stored `fsrsEvaluationSearch` value is ignored. FSRS-7
optimization always includes same-day reviews, always uses scheduling
penalties and always weights the training items by recency (the fsrs crate
applies recency weighting unconditionally): the two switches and "Same-day
reviews: Help Me Decide" are gone, and the stored
`fsrs7IncludeSameDayOptimize` / `fsrs7EnableSchedulingPenalties` values are
ignored.

**Why:** Andrew, 2026-09-14: RWKV's parameters are frozen; a proper
RWKV-Instant simulator is out of scope; FSRS-7 must always be optimized
with same-day reviews, recency and scheduling penalties, with no choice;
one optimize action is simpler than two, and a user who reached FSRS-7 in
Simple mode must still be able to optimize.

**Pinned by:** `fsrs7_optimize_always_includes_same_day_reviews`,
`fsrs7_scheduling_penalties_are_always_enabled`
(`rslib/src/deckconfig/update.rs`); `ts/tests/e2e/deck-options.test.ts`
("Optimize All Presets" visible in Simple mode, no "Optimize Current
Preset" in either mode). The rest of the visibility is markup.

## deck-options.fsrs-advanced-not-collapsed

Given deck options in Advanced mode with FSRS-7, the FSRS advanced settings
(Help Me Decide, the parameters, the search filter, "Optimize every N days",
Check Health, the simulator) are drawn under a divider, not inside a
collapsed "Advanced settings" expander. There is no expander to open.

**Why:** Andrew, 2026-09-20: "Remove the 'Advanced settings' collapsing.
It's already shown only in Advanced UI mode." Hiding Advanced-mode settings
behind a second click inside Advanced mode says the same thing twice.

**Pinned by:** `test_the_fsrs_advanced_settings_are_not_in_an_expander`
(`qt/tests/test_deckoptions.py`).

## deck-options.fsrs-auto-optimize

Given a collection with FSRS enabled, whatever its algorithm, Clanki
optimizes each preset's FSRS-7 parameters by itself: every preset has "Optimize every N
days" (Advanced mode, in the FSRS advanced section; unset means 7, 0 means
never). Once a day, after the collection opens and once the user has left
Clanki alone, the background pass of `ui.stats-fsrs-predictions-ready`
first optimizes every preset whose N days have passed since its last
optimization (a preset never optimized is due at once), one preset per
call and resting between two of them rather than waiting for the user to
stop, with the same reviews and settings as "Optimize All Presets". It
holds the collection only to read the reviews and to save; a preset saved
in deck options while it trains keeps the saved values and the result is
dropped. The new parameters are saved as a deck-options save would save
them: the cards' memory states follow them (their due dates too when
"Reschedule cards when desired retention changes" is on), and the preset's
stored per-review predictions go and are written again by the same pass.
The save is not undoable, so Undo keeps undoing the user's own last action.
The day is recorded even when the parameters did not change, and "Optimize
All Presets" records it for every preset. The screens refresh after a
change. "Time to optimize" shows only for a preset with 0 days. Under
RWKV-Curve and RWKV-Instant the preset is optimized all the same, because
the Stats graphs compare RWKV with FSRS-7 and FSRS-7 needs current
parameters there; the new parameters then change FSRS-7's memory states and
predictions only, and never a card's due date, whatever the reschedule
choice.

**Why:** Andrew, 2026-09-19: "neither FSRS nor RWKV should make the user
decide to optimize parameters/rebuild states. That should be done
automatically. For FSRS-7 that means automatic optimization every N days as
a new Deck Options setting." (CLAUDE.md, Planned direction 9.) Every 7
days because the optimization is fast now. Under RWKV: Andrew would have
optimized when Stats opens, but not at the cost of lag, so the idle
background pass does it instead.

**Pinned by:** `a_preset_is_optimized_again_after_its_days`,
`under_rwkv_it_optimizes_but_never_reschedules`, `a_save_during_training_drops_the_result`
(rslib/src/scheduler/fsrs/auto_optimize.rs);
`test_due_presets_are_optimized_before_the_predictions`,
`test_a_started_pass_never_waits_for_the_user_to_stop`,
`test_the_fake_auto_optimize_matches_the_real_backend`
(qt/tests/test_fsrs_predictions.py); `auto-optimize.test.ts`.

## deck-options.desired-retention-note

Given the deck-options screen under FSRS-7 or RWKV-Curve, a note box sits
below the desired-retention row from the moment the page opens, with no
need to focus the field first. While the value equals the one the page
opened with, it reads "The higher your desired retention, the more
frequently cards will be shown to you." Under FSRS-7, once the value
changes, it shows FSRS-7's approximate workload compared with the starting
value, or a warning when FSRS-7 cannot compute it (for example, parameters
of the wrong length). Under RWKV-Curve it always shows the note: RWKV has no
workload estimate, and FSRS-7's estimate and its parameter warnings never
appear. Under RWKV-Instant the box explains how RWKV-Instant uses desired
retention instead.

**Why:** Andrew, 2026-09-15: "make sure this is ALWAYS shown when opening
deck options and before changing DR"; and, under RWKV-Curve, changing
desired retention showed "Expected 0 or 34 values (FSRS-7), but found 21."
from FSRS-7's workload estimate, which mixes two algorithms.

**Pinned by:** `ts/tests/e2e/deck-options.test.ts` ("the desired-retention
note shows when the page opens"). The rest is markup.

## deck-options.reschedule-on-change

Given the collection-wide "Reschedule cards when desired retention changes"
setting (Preferences > Review > Scheduler, stored as `fsrsReschedule`;
`deck-options.collection-wide-in-preferences`), which applies to every
algorithm, and a deck-options save while it is on:

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
action any more; this setting is the one rescheduling control. The save
reads the stored value; the `fsrs_reschedule` field of the save request is
ignored.

**Why:** Andrew, 2026-09-14: changing desired retention affects dueness for
any algorithm, so one setting, named for that, serves all three algorithms.
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
simulate with their FSRS parameters. The "R*f(S)" graph weights each card by
its S90 (the time its simulated forgetting curve takes to reach 90% recall),
not by the simulator's internal stability: weight = 1 − e^(−8·S90/365), with
the S90 interpolated between exact grid values (weights within 0.00005).

**Why:** plan item 6 — RWKV uses many more input features and processes all
cards together instead of independently, so a correct RWKV simulator is out of
scope. Andrew, 2026-09-15: every graph that uses a stability uses the S90.

**Pinned by:** `test_post_handler_list_has_no_rwkv_workload_handlers`
(`qt/tests/test_mediasrv.py`); "simulate request carries no RWKV fields"
(`ts/routes/deck-options/simulate-fsrs-request.test.ts`);
`weighted_memorized_for_cards_uses_retrievability_times_stability_weight`,
`simulated_s90_weights_match_the_exact_s90_weights`
(`rslib/src/scheduler/fsrs/simulator.rs`).

## deck-options.reschedule-choice-remembered

Given the "Reschedule cards when desired retention changes" setting
(Preferences > Review > Scheduler; `deck-options.collection-wide-in-preferences`),
its value is the collection flag `fsrsReschedule` (absent or off until the
user turns it on), and the dialog shows the stored value. Two things read
it: the deck-options save, for the rescheduling it triggers
(`deck-options.reschedule-on-change`), and the schedule half of the
post-sync FSRS reconcile pass (`sync.post-sync-reschedule-gate`), which runs
only while it is on. A deck-options save never writes it.

**Why:** the post-sync reconcile must respect the user's rescheduling
opt-out (Andrew's review of upstream PR 4717, 2026-06-20). Until 2026-09-14
the switch sat on the deck-options page, was sent with each save and opened
off every time, so the last saved choice was the only record of it; as a
Preferences setting it is simply stored.

**Pinned by:** `scheduling_preferences_carry_the_collection_wide_settings`
(`rslib/src/preferences.rs`),
`deck_options_save_leaves_the_collection_wide_settings_alone`
(`rslib/src/deckconfig/update.rs`).

## deck-options.advanced-view

Given the collection flag `advancedUi` (default off; `spec/ui.md`,
`ui.mode-switch`), the deck-options screen hides the RWKV settings listed
below and shows them only while the flag is on. The mode is changed from
the main window or from the page's own switch (`ui.mode-switch`). Hidden
settings keep
their stored values and keep taking effect. With the flag off the whole page
is one section (`deck-options.simple-view`); this entry lists what the RWKV
section shows once the flag is on.

There is no FSRS version selector: FSRS-7 is the only FSRS model
(`sched.fsrs7-only`). (Under RWKV the FSRS controls are not shown at all;
see `deck-options.fsrs-only-controls`.)

Hidden: keep RWKV intervals in answer order; minimum reviews per day; faster
approximate queue updates; queue update interval; update queue after reviewing;
minimum other reviews and minimum seconds before a same-day repeat; the
Rebuild RWKV State and Recompute Calibration actions. Two former RWKV settings
are gone in both modes (`deck-options.rwkv-fixed-settings`).

The Algorithm dropdown exists only in Advanced mode
(`deck-options.simple-view`); in Simple mode the collection's algorithm
stays as it is (`sched.one-global-algorithm`). The RWKV section exists only in Advanced
mode, under either RWKV mode; the same-day repeat switch in it shows while
RWKV-Instant is the algorithm.

**Why:** plan item 2 — a Simplified view is the default; the remaining RWKV
knobs have defaults that suit nearly everyone.

**Pinned by:** `advanced_ui_flag_is_reported`
(`rslib/src/deckconfig/update.rs`) for the flag plumbing. The visibility
itself is markup and has no unit test.

## deck-options.glossary-term

Given a deck-options label that contains a word the user may not know, that
word carries a dotted underline and a short explanation that appears when the
pointer rests on it or the word takes keyboard focus. The underline is drawn
at all times, so the word says it can be asked about before it is hovered.
The explanation is a few words with no full stop, and is plain text: a
translation cannot put markup in it. Clicking the label still opens the
setting's own help, as any label does.

Such a label is not underlined as a whole while the pointer is on it, unlike
every other deck-options label. Two dotted underlines at once, one under the
label and one under the term, read as a single underline and neither says
anything. A label whose translation does not contain the term underlines
nothing of its own and keeps the ordinary whole-label hover underline.

Today one label uses this: "Hide related cards until tomorrow", where
"related cards" reads "cards that belong to the same note"
(`deck-options.simple-view`).

The words to underline are a translated string of their own. When that string
is not inside the translated label -- a translation free to word the label its
own way -- nothing is underlined and the label is shown plain, rather than
underlining the wrong words.

**Why:** Andrew, 2026-09-21: "'Related cards' is underlined and it displays
'cards that belong to the same note'. This way the user will get what this
setting does even without clicking on the tooltip." A setting's help opens on
a click the user has to decide to make, and one word in a label is often the
whole difficulty. He noted that nothing in Anki does this today, which is why
it is one component rather than markup repeated per setting.

**Pinned by:** `ts/routes/deck-options/bury-siblings.test.ts` (the label is
split around the term, and a label without the term is left plain);
`ts/tests/e2e/deck-options.test.ts` ("only the term is underlined").

## deck-options.simple-view

Given the collection flag `advancedUi` off (Simple mode, the default;
`spec/ui.md`, `ui.mode-switch`), the deck-options screen is one section,
titled "Deck Options", with exactly these controls in this order:

1. New cards/day, without the preset / This deck / Today only tabs: the
   box edits the level that is in effect (a deck or today override when one
   is set, else the preset), and the page neither writes nor clears an
   override. Maximum reviews/day is not shown and keeps its stored value;
2. Desired retention, likewise without its preset / This deck tabs, and, as
   in Advanced mode, the First intervals table and the "Optimize All
   Presets" button for FSRS-7 and the RWKV-Instant information box
   (`deck-options.first-intervals`, `deck-options.fsrs-only-controls`) —
   without the Algorithm dropdown, which is Advanced-only;
3. Bury siblings — one switch;
4. Play audio automatically (`deck-options.play-audio-switch`);
5. On-screen timer — the `showTimer` setting (Advanced mode's "Show on-screen
   timer");
6. the Easy Days sliders, collapsed behind an "Easy Days" expander (plain,
   not bold) that the user opens by clicking its name; a small "?" next to
   the name opens the Easy Days help without toggling the expander. In
   Advanced mode the Easy Days section shows the sliders at once, with no
   expander (its title and help are the section's own).

Add-on components render after the section, in both modes.

That list is the default of the split (`spec/ui.md`,
`ui.split-configurable`), which has one item per setting: the seven above
(Bury siblings is the one switch, Desired retention includes its notes, the
First intervals table and the RWKV-Instant box, and Optimize All Presets
is its own item) and each setting Advanced mode shows (the three bury
switches each, the limit tabs and the desired-retention tabs as one item
each, each RWKV setting, the Maintenance buttons as one item, the FSRS
advanced settings each: Help me decide, the parameters, the search
filter, the health check with Evaluate, the simulator). In Simple mode a
setting the user takes out of the Simple section is not drawn (a hidden
limit or retention box keeps its value logic, as Maximum reviews/day does
by default). A setting the user adds goes where it belongs:

- Maximum reviews/day, the limit tabs, the Algorithm dropdown, the
  desired-retention tabs and the FSRS advanced settings go into the Simple
  section, next to New cards/day and Desired retention, whose controls
  they share (the FSRS ones under the same divider as in Advanced mode);
- every other one goes into its Advanced-mode section (New cards, Lapses,
  Display order, RWKV, Burying, Audio, Timers, Auto advance, Advanced),
  drawn below the Simple section and above the add-ons, in Advanced
  mode's order; a section is drawn only when it has such a setting, and
  it shows only those.

Burying is one switch, drawn the same way in both modes and named "Hide
related cards until tomorrow". Its help says what a related card is and what
the switch does, in two short paragraphs. Neither the name nor the help says
"bury" or "sibling", because a new user has to be taught both words first.
The words "related cards" inside the label carry a dotted underline and
explain themselves on hover, with "cards that belong to the same note"
(`deck-options.glossary-term`); the underline is always drawn, so the label
answers the question without a click. A translation that words the label
differently, so that the term is not inside it, underlines nothing and shows
the label plain.

Advanced mode draws that same switch in its "Burying" section and has no
others: the three per-type switches ("Bury new siblings", "Bury review
siblings", "Bury interday learning siblings") are gone from the screen, so
no mode can set the three stored settings individually. A preset that
arrives with some but not all of them on, from another client, shows "Partly
on" and is normalised by toggling the switch. Preferences > UI split lists
one item for burying, under the switch's name.

That switch stands for three stored settings: it reads as on
only while `buryNew`, `buryReviews` and `buryInterdayLearning` are all on;
turning it on or off writes all three. Showing a preset writes nothing: a
preset with some but not all three bury settings on keeps them until the
switch is toggled, and shows the caption "Partly on (set in Advanced mode)"
under the switch (the switch reads as off; turning it on writes all three).
Its revert button restores off.

Given the flag on (Advanced mode), the screen has the per-topic sections
(Daily limits, New cards, Lapses, Display order, Algorithm, RWKV, Burying,
Audio, Timers, Auto advance, Easy Days, Advanced) with the three separate
bury switches, and it alone shows: the
preset / This deck / Today only tabs of the daily limits and the preset /
This deck tabs of desired retention, Maximum reviews/day, the
Algorithm dropdown, Learning steps, Insertion order, Relearning steps, Leech
threshold, Leech action, the whole Display order section, the FSRS advanced
section (`deck-options.fsrs-only-controls`), Skip question when replaying
answer (off by default), Maximum answer seconds, the
whole Auto advance section, Maximum interval, Minimum interval, Ignore cards
reviewed before, and the RWKV settings of `deck-options.advanced-view`.
Hidden settings keep their stored values and keep taking effect. Neither
mode shows a collection-wide setting
(`deck-options.collection-wide-in-preferences`).

**Why:** Andrew, 2026-09-14, plan item 2: Simple mode is one short list of
the settings a new user needs; everything else belongs to Advanced mode.
One bury switch is enough there, because the split settings only matter to
power users. Andrew, 2026-09-15: "Stop on-screen timer on answer" is gone
from both modes (`review.timer-keeps-running`), so the timer switch stands
for one setting and needs no "Partly on" caption. Andrew, 2026-09-21:
Simple mode needed a beginner-friendly name for the bury switch, so it
says "Hide related cards until tomorrow" with a shorter help. Later the
same day he collapsed the three Advanced switches into that one and gave
it the same name in both modes: "This way the user will get what this
setting does even without clicking on the tooltip." Told that no mode
could then set the three settings individually, he kept the design: a
per-card-type bury choice is a power-user knob Clanki is shedding.

**Pinned by:** `ts/routes/deck-options/bury-siblings.test.ts` (the combined
switch, and Simple mode's name and help);
`ts/routes/deck-options/ui-split.test.ts` (the default draws the Simple
section only; added settings go to the Simple section or their own
section, once; Advanced mode draws every setting) and
`test_every_default_equals_todays_split` (`qt/tests/test_ui_split.py`);
`ts/tests/e2e/deck-options.test.ts` (the FSRS parameters, the Algorithm
dropdown, the limit tabs and Skip question when replaying answer exist only
in Advanced mode; Desired retention and Bury siblings are visible in Simple
mode). The section layout itself is markup.

## deck-options.play-audio-switch

Given a preset, the deck-options screen shows its `disableAutoplay` setting,
in both modes, as a switch named "Play audio automatically" that is on
while `disableAutoplay` is off. Turning the switch off stores
`disableAutoplay` on, turning it on stores it off, and showing a preset
writes nothing. A preset that never changed the setting shows the switch
on; the revert button restores on. What plays is unchanged: the stored
setting still decides it.

**Why:** Andrew, 2026-09-15: a positive switch ("Play audio
automatically", on by default) is simpler than the negative "Don't play
audio automatically".

**Pinned by:** `ts/routes/deck-options/autoplay-switch.test.ts`.

## deck-options.collection-wide-in-preferences

Given the deck-options screen, every setting on it belongs to one preset
(or, for the limit tabs, to the current deck), except the Algorithm, which
applies to the whole collection and is marked "(global)"
(`deck-options.scheduler-choice`). The three other settings that apply to
the whole collection live in Preferences > Review, in the Scheduler group
and a "Custom scheduling" group, and nowhere else: Limits start from
top (`applyAllParentLimits`), Reschedule cards when desired retention
changes (`fsrsReschedule`; `deck-options.reschedule-choice-remembered`),
and Custom scheduling (`cardStateCustomizer`). They are read and written
through the `Preferences.Scheduling` message; the matching fields of the
deck-options save request are ignored, so a deck-options save never
overwrites a Preferences change. The deck-options page still reads one of
them: the reschedule choice, for the Easy Days warning.

**Why:** Andrew, 2026-09-14: a setting that affects all decks and presets
should not be in deck options to begin with; moving them beats marking them.

**Pinned by:** `scheduling_preferences_carry_the_collection_wide_settings`
(`rslib/src/preferences.rs`),
`deck_options_save_leaves_the_collection_wide_settings_alone` and
`fsrs_short_term_with_steps_flag_roundtrip` (`rslib/src/deckconfig/update.rs`);
`test_update_collection_writes_the_collection_wide_scheduling_settings`
(`qt/tests/test_preferences.py`);
`test_reschedule_snapshot_reads_the_stored_reschedule_choice` and
`test_rwkv_curve_reschedule_not_needed_without_the_switch`
(`qt/tests/test_rwkv_scheduler.py`, the after-save RWKV refresh reads the
stored choice); "collection-wide settings are not on the deck-options page"
(`ts/tests/e2e/deck-options.test.ts`); "dataForSaving" in
`ts/routes/deck-options/lib.test.ts` (the save does not carry them).

## deck-options.new-preset-defaults

Given a new preset — added on the deck-options screen, created by
`col.decks.add_config()` without a source, or reset with "Restore
defaults" — its learning steps and relearning steps are empty and its
algorithm is the collection's (`sched.one-global-algorithm`), or, in a
collection that has none yet, RWKV-Curve (`rwkv_review_enabled` on,
`rwkv_review_instant_order_enabled` off), its leech action is Tag Only, its
maximum reviews/day is 9999, it buries siblings (new, review and interday
learning) and its review sort order is descending retrievability (most
likely to be recalled first); the revert buttons restore these values. Given a new collection, its default preset has these values,
so the collection starts on RWKV-Curve. Existing presets keep their stored
values: a stored preset without the RWKV flag still reads as FSRS-7, and
scheduling outcomes for existing presets do not change. (Same-day reviews for
(re)learning steps are always allowed: `sched.same-day-steps-always-on`.)

**Why:** Andrew, 2026-09-14: RWKV-Curve is the algorithm new users should
get, and it needs no learning steps. Andrew, 2026-09-15: no practical review
cap by default; descending retrievability keeps retention closest to the
desired retention when not every due card gets done (a backlog, a session
stopped early). Andrew, 2026-09-16: bury siblings by default, because a
sibling seen on the same day gives the answer away.
Existing presets must keep the assumptions their review histories were
built on.

**Pinned by:** `new_preset_has_no_steps_and_runs_rwkv_curve`,
`fresh_collection_starts_with_new_preset_defaults`,
`stored_preset_without_rwkv_flag_stays_off` (`rslib/src/deckconfig/mod.rs`);
`every_preset_write_takes_the_collection_algorithm`
(`rslib/src/deckconfig/algorithm.rs`).

## deck-options.no-difficulty-order-under-rwkv

Given a preset whose algorithm is RWKV-Curve or RWKV-Instant, the review
sort order dropdown does not offer "Easy cards first" or "Difficult cards
first" (the difficulty orders, stored as `EASE_ASCENDING` /
`EASE_DESCENDING`). A preset that stores one of them reads as the default
order, descending retrievability (`deck-options.new-preset-defaults`), when
the deck-options screen shows it under RWKV, in either
mode, and saving writes that value. Under FSRS-7 both orders stay available.

**Why:** Andrew, 2026-09-15: difficulty is an FSRS state variable, so
sorting RWKV cards by it has no meaning.

**Pinned by:** `ts/routes/deck-options/review-order.test.ts`.

## deck-options.new-retrievability-order-instant-only

Given a preset, the new-card gather orders "Ascending retrievability" and
"Descending retrievability" (named as `ui.simple-recall-wording` says) rank
new cards by RWKV-Instant's scores, so they are offered only when the
collection runs RWKV-Instant. Under FSRS-7 and
RWKV-Curve the dropdown does not list them, a preset that stores one of them
reads as "Deck" on the deck-options screen (and saving writes that), and the
study queue gathers such a preset's new cards as "Deck" does, without reading
any RWKV-Instant score.

**Why:** Andrew, 2026-09-19: under RWKV-Curve these orders read
RWKV-Instant's values, which mixes two algorithms (RWKV-Curve has no value
for a new card); "hide it".

**Pinned by:** `ts/routes/deck-options/review-order.test.ts` ("the
retrievability new-card orders are offered only under RWKV-Instant"),
`retrievability_gather_outside_rwkv_instant_ignores_instant_scores`
(`rslib/src/scheduler/queue/builder/mod.rs`).

## deck-options.historical-retention-fixed

Given any preset, historical retention is 0.9. A memory state inferred from
SM-2 data (a card with no review log, or a truncated one) uses 0.9 whatever
`historical_retention` the preset stores; the post-sync reconcile
(`sync.fsrs-reconcile-after-sync`), the FSRS simulator and add-on
preset overlays use 0.9 as well, and the value reported for a preset
(`fsrs_preset_for_card`) is 0.9: the inferred FSRS-7 state reaches 90%
recall at the card's interval (`sched.fsrs7-sm2-conversion`). The control is gone
from the screen; the proto field and the stored value stay, and are ignored.

**Why:** Andrew, 2026-09-14: one setting less. The stored value only shaped
memory states inferred from SM-2 data under the older FSRS versions, and
0.9 is the value nearly every preset had.

**Pinned by:** `stored_historical_retention_is_ignored`,
`post_sync_reconcile_ignores_the_stored_historical_retention`
(`rslib/src/scheduler/fsrs/memory_state.rs`),
`fsrs_preset_is_derived_from_deck_config`,
`fsrs_preset_overlay_uses_first_matching_rule`
(`rslib/src/scheduler/fsrs/preset.rs`).
