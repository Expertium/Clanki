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
goes to the intraday queue; an interval of 12 hours or more gets the same
review fuzz, the same load balancer, the same sibling dispersal
(`sched.sibling-dispersal-gate`), the same 90-day load-balance limit, and the
same floors — Again on a review card, and on a relearning card without
relearning steps, is clamped to the minimum lapse interval but not fuzzed;
Hard, Good and Easy of a review card each keep the previous interval when it still lies within the
configured fuzz range; and each day button sits at least one day above the
day button before it. The resulting fuzz delta is recorded on the state and
shown above the answer buttons when that preference is on. This applies to
every card the preset schedules, new and learning cards included. The S90
RWKV-Curve supplies for a button becomes that answer's stability, in the
states the backend builds, and the "leech only if young" check
(`leech_only_if_young`) compares RWKV-Curve's Again S90 with 21 days, not
FSRS-7's (`sched.next-state-s90`).

Before this entry, RWKV-Curve wrote its interval over the already-fuzzed FSRS
state and set the delta to 0, so RWKV-Curve users got no fuzz and no sibling
dispersal at all.

**Why:** fuzz and sibling dispersal are properties of the _scheduling
outcome_, not of FSRS; switching the interval source must not switch them off.
Andrew, 2026-09-15: Again on a relearning card without steps follows the
review rule, as upstream did (it had been fuzzed like a graduating card).
Later the same day (audit of RWKV-Curve): the young-leech check must not
use FSRS-7's S90 for an RWKV-Curve card, since two cards with the same
RWKV-Curve intervals were leeches or not depending on FSRS-7.

**Pinned by:** `scheduling_states_with_intervals_apply_the_fsrs_rules`,
`rwkv_curve_s90s_decide_the_young_leech_check`
(`rslib/src/scheduler/answering/mod.rs`),
`external_intervals_are_dispersed_away_from_siblings`
(`rslib/src/scheduler/states/load_balancer.rs`),
`relearning_again_is_clamped_without_fuzz`
(`rslib/src/scheduler/states/button_intervals.rs`);
`test_rwkv_curve_states_come_from_the_backend_with_unrounded_intervals`,
`test_rwkv_curve_states_only_send_supplied_ratings`,
`test_reviewer_rwkv_curve_intervals_go_through_review_fuzz`
(`qt/tests/test_rwkv_scheduler.py`).

## sched.rwkv-answers-with-fsrs-switch-off

Given a collection whose algorithm is RWKV-Curve or RWKV-Instant (the
`schedulingAlgorithm` key, or before the collection has one, the Default
preset's, `sched.one-global-algorithm`) and whose collection `fsrs` switch is
still off — a new collection until the first deck-options save — the answer
states are FSRS states, as with the switch on: RWKV-Curve's intervals replace
FSRS's (`sched.rwkv-curve-fuzz`), and RWKV-Instant's answers store FSRS
states. Nothing is written to turn the switch on, so a new, empty collection
still needs no full sync (`sched.global-algorithm-migration`). Everything
else that reads the switch is unchanged.

**Why:** Andrew, 2026-09-15 (audit of RWKV-Curve): a new Clanki collection
runs RWKV-Curve, but with the switch off the answer states were SM-2's, so
the answer buttons and the answer used SM-2 intervals while RWKV-Curve's S90
was stored — two algorithms in one answer.

**Pinned by:** `rwkv_answers_use_fsrs_states_while_the_fsrs_switch_is_off`
(`rslib/src/scheduler/answering/mod.rs`).

## sched.rwkv-curve-s90

Given a card that RWKV-Curve predicts for, its S90 — the current one (card
info, the RWKV-Curve reschedule) and the one for each answer button (the
stability stored on answer, `sched.rwkv-curve-fuzz`) — is the point in days,
unrounded, where RWKV-Curve's forgetting curve meets 90% recall: searched on
the same points as the answer intervals (`sched.sub-day-intervals`),
including the points inside the first day when the curve is at or below 90%
after one day; the grid points bracket the crossing and the crossing itself
is found on the curve, as for the answer intervals. It can be under one day.
The answer S90s keep grade order the same way as the answer intervals when
that is enforced. A preview answer (a filtered deck that does not
reschedule) stores no S90: the card's memory state is left as it was.

**Why:** Andrew, 2026-09-15: RWKV-Curve's S90 should be fractional, like
FSRS-7's. Before this entry it was searched on whole days only and rounded up
to whole days, at least 1.

**Pinned by:** `rwkv_curve_s90_is_unrounded`,
`intervals_are_where_the_curve_meets_the_target` (`rslib/src/rwkv/mod.rs`);
`preview` (`rslib/src/scheduler/answering/preview.rs`).

## sched.rwkv-curve-s90-kept

Given a card of an RWKV-Curve preset that has a memory state, when FSRS-7
computes its memory state again — a move to another deck whose preset runs
RWKV-Curve, or a change of the preset's FSRS parameters, desired retention,
Easy Days or fuzz in deck options — the card keeps the S90 stored in
its memory state (the one RWKV-Curve wrote, `sched.rwkv-curve-s90`); only
the FSRS-7 fields (internal and fast stability, difficulty) and the stored
decay and desired retention change. A card moved to a deck whose preset runs
FSRS-7, and every card when the collection switches to FSRS-7
(`sched.one-global-algorithm`), gets FSRS-7's own S90. Three passes still
rebuild the whole state, S90 included: the one-time FSRS-7 migration of a
preset's parameters (`sched.fsrs7-only`), whose stored S90 may predate
RWKV-Curve, the post-sync rebuild of conflicting
cards (`sync.fsrs-reconcile-after-sync`) and the repair of cards last
reviewed in another client (`sync.fsrs7-state-of-foreign-cards`).

**Why:** Andrew, 2026-09-15: a deck move or a preset change must not replace
RWKV-Curve's S90 with FSRS-7's; one algorithm's values must not mix into the
other's.

**Pinned by:** `rwkv_curve_cards_keep_their_s90_when_fsrs7_recomputes`
(`rslib/src/scheduler/fsrs/memory_state.rs`).

## sched.rwkv-curve-reschedule

Given a review card that the RWKV-Curve reschedule reschedules
(`deck-options.reschedule-on-change`), its new interval is RWKV-Curve's
current interval — the unrounded day where the card's curve meets its target
retention, found as for the answer intervals (`sched.sub-day-intervals`) —
turned into whole days exactly as the FSRS-7 reschedule turns FSRS-7's
unrounded interval into days: rounded to the nearest day like an answer, at
least 1, at most the home preset's maximum interval, and not below the
interval before the card's last review while the new interval still reaches
it within the fuzz range; then the load balancer and Easy Days pick the day
within the fuzz range (else plain review fuzz), seeded per card for its last
review as in the FSRS-7 reschedule, and each rescheduled card counts toward
the load of the cards after it. The card is due that many days after its
last review.

Its memory state changes as on an RWKV-Curve answer: the S90 becomes
RWKV-Curve's current S90 (`sched.rwkv-curve-s90`), and the internal and fast
stabilities keep their values — a card without a fast stability still has
none. A card without a usable FSRS-7 state gets the FSRS-7 state whose own
S90 is RWKV-Curve's (`sched.fsrs7-sm2-conversion`).

**Why:** Andrew, 2026-09-15 (audit of RWKV-Curve): "RWKV-Curve should adopt
fuzz/LB"; the reschedule rounded the crossing up (1.1 d gave 2 d where an
answer gives 1 d), ignored the maximum interval, and put every card with
the same interval on the same day. One algorithm's values must not mix into
the other's: the reschedule wrote RWKV-Curve's S90 into FSRS-7's fast
stability when the card had none, which an answer never does.

**Pinned by:** `reschedule_turns_the_unrounded_interval_into_days_like_fsrs7`,
`apply_review_reschedule_changes_only_the_s90_of_a_memory_state`,
`apply_review_reschedule_without_memory_state_gets_an_fsrs7_state_with_that_s90`
(`rslib/src/scheduler/rwkv.rs`);
`rescheduled_interval_days_are_fuzzed_and_load_balanced`
(`rslib/src/scheduler/fsrs/rescheduler.rs`);
`current_intervals_from_warm_up_match_predict_many` (`rslib/src/rwkv/mod.rs`);
`test_rwkv_reschedule_items_carry_the_unrounded_interval`,
`test_apply_rwkv_review_reschedule_includes_target_retention`
(`qt/tests/test_rwkv_scheduler.py`).

## sched.rwkv-no-model-error

Given a collection that runs RWKV-Curve or RWKV-Instant and no usable RWKV
model (the model file is missing, or the RWKV backend does not load):

- when the profile opens, a warning says that the RWKV model file is missing
  or does not load, so RWKV cannot schedule the cards, and suggests
  reinstalling Clanki or choosing FSRS-7 in deck options; no offer to build
  or restore the RWKV state follows;
- the reviewer's answer area for an RWKV-Curve card shows "RWKV model not
  found" instead of "Waiting for RWKV-Curve…", and does not ask again;
- card info's retrievability of an RWKV-Instant card reads "RWKV model not
  found" instead of "Calculating…".

**Why:** Andrew, 2026-09-15: while RWKV is not ready, show "…" or
"Calculating…"; with no model, show an error rather than waiting forever or
using FSRS-7's values.

**Pinned by:** `test_answer_buttons_say_the_rwkv_model_is_missing`
(`qt/tests/test_reviewer.py`);
`test_startup_without_a_model_warns_instead_of_offering_a_state`,
`test_rwkv_instant_card_info_says_the_model_is_missing`
(`qt/tests/test_rwkv_scheduler.py`).

## sched.rwkv-instant-waits

Given a collection that runs RWKV-Instant:

- the study queue takes review cards only from RWKV-Instant's scores for the
  studied deck: a review card is gathered when its score makes it due, and a
  review card without a score is not gathered, even when its FSRS-7 due date
  has come. Until RWKV-Instant has scored the studied deck, the queue holds no
  review cards (learning and new cards still come) and reports that the
  scores are pending;
- a normal deck's review count, in the deck list and in the overview, is the
  number of its scored cards whose score makes them due, plus the
  daily-minimum pulls; a card without a score counts nothing, and FSRS-7's
  due count never stands in;
- while the scores are pending, the deck list shows the review count as "…",
  also when the scoring fails, finds nothing it can score, or gives a stale
  result (the next refresh of the deck list tries again); the overview shows
  the review count as "…", the note "Waiting for RWKV-Instant…" ("RWKV model
  not found" when RWKV cannot run) and no congratulations screen, and asks
  again every 2 s while RWKV can run; the reviewer shows the review count as
  "…".

**Why:** Andrew, 2026-09-15: never mix two algorithms; while RWKV is not
ready, show "…" or "Calculating…", and scheduling waits. Before this entry,
RWKV-Instant gathered FSRS-7-due cards when it had no scores for the deck or
none for a card, and its counts fell back to FSRS-7's.

**Pinned by:** `rwkv_instant_without_scores_gathers_no_reviews_and_reports_pending`,
`rwkv_instant_unscored_due_reviews_wait_for_their_score`
(`rslib/src/scheduler/queue/builder/mod.rs`);
`rwkv_deck_tree_counts_exclude_ineligible_scored_reviews`
(`rslib/src/decks/tree.rs`);
`test_deck_browser_count_failure_keeps_the_review_count_pending`,
`test_overview_waits_for_rwkv_instant_instead_of_congratulating`,
`test_overview_retries_while_rwkv_instant_scores_are_pending`
(`qt/tests/test_rwkv_scheduler.py`);
`test_remaining_review_count_is_pending_without_rwkv_instant_scores`
(`qt/tests/test_reviewer.py`).

## sched.study-queue-kept-after-answer

Given a studied deck, when a card is answered, the study queue is updated in
place, as in upstream Anki: the answered card leaves it, a (re)learning card
goes back in by its due time, and the counts go down. The queue is not built
again. In a deck without RWKV-Instant, the reviewer then empties the RWKV
score map; when that map is already empty, the queue is kept. So the
retrievability orders (FSRS-7's, and RWKV-Curve's own measure,
`sched.rwkv-review-order`) keep the order the queue was built with until
something else rebuilds it (another operation, a new day, the next study
session). Installing RWKV-Instant scores, or emptying a map that held
scores, still builds the queue again, and RWKV-Instant still patches the
answered card's score in place after an answer and still waits for its
scores (`sched.rwkv-instant-waits`). A kept queue holds only cards the
running algorithm gathered; nothing of another algorithm enters it.

**Why:** Andrew, 2026-09-15: the rebuild after every answer froze the window
for 60 ms under RWKV-Curve and 430 ms under FSRS-7 on a deck with 20,000 due
cards. Before this entry, emptying an empty map dropped the queue, so it was
built again after every answer.

**Pinned by:** `emptying_empty_rwkv_scores_keeps_the_study_queue`
(`rslib/src/scheduler/queue/builder/mod.rs`).

## sched.rwkv-startup-no-window

Given a collection whose RWKV state is restored, or built, when the profile
opens, Clanki opens no progress window and disables no part of the main
window. The work runs on a background thread while the deck list, the
overview, the reviewer, the menus and every dialog stay usable. The one-time
conversion of an old store is the single exception and keeps its own window
(`sched.rwkv-lazy-state-upgrade-window`).

While that work runs, every screen that would show an RWKV number shows its
own quiet state instead of a number: the deck list and the overview wait for
the counts and ask again, the reviewer says its intervals are being prepared,
the Browser leaves its RWKV columns empty and retries, the graphs say they
are being computed, and card info says the same for the RWKV-Curve chart. No
screen shows an FSRS-7 value in place of an RWKV one, and none shows a number
computed without the RWKV state. When the work ends, every open card-info
window draws its card again, so a chart that said it was being computed shows
the chart without the user asking for it.

The whole-history query the restore and the build run is split into
`HISTORY_QUERY_PARTS` card-id ranges, so the collection is free between the
parts instead of held for one query of several seconds.

**Why:** Andrew, 2026-09-20 (CLAUDE.md, Planned direction 10 and 11):
"Starting Clanki should be near-instantaneous", and Clanki shows no waiting
window at start-up. Opening his profile showed a modal "Starting" window and
made the main thread unusable for about 34 seconds (27.2 s, then 6.7 s). A
window that the user cannot dismiss, in front of work the user never asked
for, is exactly what rule 11 forbids; the work itself still has to happen, so
it moves out of the user's way instead of going away.

**Pinned by:** `test_the_startup_restore_opens_no_window`,
`test_the_startup_build_opens_no_window`,
`test_open_card_info_is_drawn_again_when_the_rwkv_state_is_ready`,
`test_startup_loads_usable_rwkv_state_cache_without_a_window`
(`qt/tests/test_rwkv_scheduler.py`)

## sched.rwkv-state-cache-startup-build

Given a collection that runs RWKV-Curve or RWKV-Instant, a usable RWKV model,
and no usable local RWKV state (no saved state cache, or one that does not
load), when the profile opens and any automatic startup sync has finished:

- Clanki builds the RWKV state cache and the calibration data (the historical
  retrievability rows) at once, without asking and without a progress window
  (`sched.rwkv-startup-no-window`);
- it starts this build once per profile open, and skips it when the RWKV
  state became ready in the meantime;
- when the build ends it shows one short message.

**Why:** Andrew, 2026-09-15: "Don't show this at startup, just build both"
(the question offered "Build State Only", "Build State + Calibration Data"
and Cancel). The same build started by a button, rather than by start-up,
keeps its window: the user asked for it and waits for it.

**Pinned by:** `test_startup_builds_the_state_and_the_calibration_data_without_asking`,
`test_the_startup_build_opens_no_window`
(`qt/tests/test_rwkv_scheduler.py`).

## sched.rwkv-recordings-automatic

Given a collection that runs RWKV-Curve or RWKV-Instant and a usable RWKV
model, whenever its RWKV state becomes ready (loaded or built at start-up,
read again, or refreshed after a sync), Clanki itself does the RWKV work
that would otherwise need a button, without asking and without a message:

- when the state skips synced reviews that are older than the replay window,
  it reads the whole review history again, in the "Getting Ready" progress
  window, once the sync has finished;
- otherwise, when the per-review recordings (the RWKV-Instant and RWKV-Curve
  rows the Stats graphs read, `ui.stats-model-metrics`, and the curve
  sources card info draws, `ui.card-info-rwkv-curve`) were not made by a
  full recording pass with the running model (the SHA-256 of its weights),
  the running curve-source format and kernel and the current replay
  semantics, or their rows are gone from the cache file beside the
  collection, it runs that pass at most once per profile open. That pass is
  minutes of work on a large collection, so it never runs at start-up and
  never in a progress window: it starts only after the user has left Clanki
  alone for ten seconds and no card is on the review screen, and it runs on
  a thread of its own. **That thread is its own, never the task manager's
  collection worker.** There is one collection worker, and answering a card,
  clicking a deck, the deck list, the Browser and the Stats all go through
  it; a pass that walked the whole history on it would put every one of them
  behind it for minutes.

  **The pass replays in a model runtime of its own.** It loads a second
  runtime from the same weights file, with the same settings and the same
  entry point, replays the whole history into that one, and releases it as
  soon as it is done. The shared runtime, the one the reviewer predicts
  from, is neither claimed, locked, rested against nor invalidated: a
  prediction asked for while the pass runs is served, from a state the pass
  never touched. The rows the pass records are the same rows either way,
  because the model and the replay are the same.

  It reads the review history before it loads that runtime, in parts, and
  then replays in short batches of about a thousand reviews. **Between two
  batches it rests**, so the pass takes a known, small share of the machine
  while the user works: the rest is a multiple of the batch just done.
  While the user is away it is the shortest rest there is. **It is never a
  wait for the user to stop.** The pass stops by itself once the profile it
  started in has closed, checked both between two batches and at every
  progress report. Nothing is shown while it runs, except
  that the model-quality graphs say their numbers are being computed
  instead of saying that nothing recorded them; a finished pass shows one
  short message. A finished pass remembers what it recorded
  with (`rwkv-state-cache/recordings.json` in the profile folder), so the
  next start-up runs none. A state-cache build that replays the whole
  history and records all three (RWKV-Instant's rows, RWKV-Curve's rows and
  the curve sources), such as the build on a first start, is that pass too:
  it remembers the same, and no recording pass runs right after it.

Neither starts while a card is on the review screen: each waits until the
review screen closes. Neither starts while the main window is disabled (a
sync in progress, or the profile closing); the next time the state becomes
ready finds the same reason and starts it then. A sync that skipped old
reviews shows no message. The deck-options buttons "Read Review History
Again" and "Prepare Stats Graphs" stay, as a manual fallback.

A card on the screen stops the pass rather than resting it, and the pass
starts again once the user leaves the reviewer. Reviewing always wins. This
is a safety net: a pass that has a runtime of its own leaves the reviewer's
state alone, so a card shown while it runs gets its intervals and the stop
changes nothing.

_Limitation:_ a backend that cannot load a second runtime — a test double,
or a backend of another kind — falls back to the shared one, claimed and
restored as it was before. Such a pass owns the half-replayed state, so a
prediction asked for while it runs is refused and falls back, it hands the
backend back between two batches so a click waits for one batch at most,
and a card on the screen really does have to stop it. Clanki's own
embedded RWKV backend always loads its own runtime, so the fallback is for
code that replaces it. It would go away if every backend could load a
second runtime.

**Why:** Andrew, 2026-09-19 (CLAUDE.md Planned direction 9, "Everything Just
Works"): the user never decides to rebuild RWKV states; a message or a
button that asks the user to read the history again or prepare data is a
design bug. His collection showed "RWKV-Curve is not shown yet: Clanki has
not recorded its predictions" on AUC-ROC, and a sync warned that "synced
reviews are older than 8 days" and asked him to press "Read Review History
Again".

The batches and the short rest are the same rule seen from the other side.
A pass that waited for ten seconds of quiet between two batches made no
progress at all while he studied, because every key press restarted the
wait: measured on his collection, 0 of 656,402 reviews in five minutes of
use, and a job of two minutes stood unfinished for an hour. Because
it never finished, RWKV-Curve's per-review rows stayed empty (0 rows, next
to 966,964 for FSRS-7 and 1,350,982 for RWKV-Instant) and RWKV-Curve was
missing from the AUC-ROC graph and greyed out in the Calibration menu.
Holding the backend for the whole pass cost the same: a click waited for it
for longer than 30 s, the pass's whole length.

Resting and stopping were both workarounds for one runtime shared between
two jobs. A second runtime costs 11 MB of weights, measured, and the pass
no longer replaces the reviewer's state at all, so it no longer needs the
copy of that state it used to take and restore. That is what removes the
conflict instead of scheduling around it.

**Pinned by:** `test_missing_or_stale_recordings_start_the_recording_pass_by_itself`,
`test_the_pass_replays_in_a_runtime_of_its_own`,
`test_a_prediction_is_served_while_the_pass_runs`,
`test_the_pass_releases_its_own_runtime`,
`test_its_own_runtime_is_the_same_model`,
`test_the_pass_records_the_same_rows_in_either_runtime`,
`test_a_backend_that_cannot_load_a_second_runtime_falls_back`,
`test_the_recording_pass_waits_until_no_card_is_being_reviewed`,
`test_the_recording_pass_waits_until_the_user_leaves_clanki_alone`,
`test_the_pass_rests_a_bounded_time_between_batches`,
`test_the_pass_hands_the_backend_back_while_it_rests`,
`test_the_pass_reads_the_history_before_it_claims_the_backend`,
`test_the_pass_runs_in_the_background_without_a_progress_window`,
`test_the_recording_pass_leaves_the_collection_worker_free`,
`test_the_recording_pass_stops_when_the_profile_closes`,
`test_while_the_recording_pass_runs_the_graphs_say_it_is_computing`
(`qt/tests/test_stats_metrics.py`),
`test_nothing_starts_while_the_main_window_is_disabled`,
`test_a_full_recording_pass_marks_the_recordings_current`,
`test_a_full_recording_build_leaves_no_recording_pass_due`,
`test_post_sync_refresh_ignores_reviews_older_than_eight_days`
(`qt/tests/test_rwkv_scheduler.py`).

## sched.rwkv-lazy-state-load

Given a saved RWKV state cache, when a profile opens, Clanki reads only the
shared states and a key index from it. The deck, preset and global states are
read in full; every card and note state stays in the store and is read one key
at a time, the first time that card or note is part of a prediction, an answer
or a warm-up row.

- A read returns the entity's complete state as of its latest write, or it
  fails with an error. A key the index does not hold has no cached state, and
  the card takes the same path a card with no state has always taken.
- A row whose own kind and id are not the ones asked for, and a store whose
  generation is not the restored one, are errors, not states.
- The deck, preset and global states are current before any prediction runs.
- A state this session wrote is never replaced by the stored one.

The store keeps one row per (segment, kind, entity), so the newest segment of
the restored chain that holds a key answers the read in one lookup. A store
written by an older Clanki, which held one serialized delta stream per
segment, is converted to rows once, at the first profile open that reads it;
no review is replayed and no state changes.

**Why:** Andrew, 2026-09-16: "can we do anything about the long ass loading of
RWKV states on Clanki startup?" His cache is 3.51 GB and a study session needs
about 25 MB of it, so reading all of it behind the start-up progress window
cost about 4 seconds and 3.3 GB of memory at every profile open, and more when
the file is not in the operating system's cache. A state is replaced at every
review and never accumulated, so every stored row is a full snapshot of one
entity and a point lookup is exact.

**Pinned by:** `lazy_state_reads_match_resident_predictions`,
`lazy_state_read_rejects_a_foreign_row`,
`a_lazy_session_writes_a_complete_checkpoint_chain`,
`delta_state_store_restores_checkpoint_chain` (`rslib/src/rwkv/mod.rs`);
`test_rwkv_delta_store_prune_removes_unreachable_entity_states`
(`qt/tests/test_rwkv_scheduler.py`).

## sched.rwkv-lazy-state-upgrade-window

Given a saved RWKV state cache in the old format, when a profile opens, a
progress window titled "One-time update" reads "Clanki is reorganising its
saved review data so it can start faster. This happens once and can take some
time." The conversion runs once, inside that wait; every later start-up of
the same profile opens no window at all
(`sched.rwkv-startup-no-window`). A profile with a converted store, and a
profile with no store at all, never show the one-time words and never show a
window.

**Why:** Andrew, 2026-09-16, wrote both strings himself. This conversion is
the one start-up wait that the user is told about and that happens once, so
it is the "other exception where it is expected to wait" that rule 11 allows,
next to optimizing parameters, a backup and a database check. It takes 15.6
seconds on his 3.51 GB store and changes the file the next start-up reads, so
a silent 15-second pause on one start-up in a profile's life would look like
a fault. It names what the user waits for, not the format it converts, and it
promises "once" because the converted store is never converted again. The
promise is safe to print because the conversion builds a new file and renames
it into place: ending the wait early loses nothing.

**Pinned by:** `test_only_the_one_time_upgrade_gets_the_one_time_words`
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

## sched.rwkv-curve-buttons-wait

Given a card whose preset runs RWKV-Curve, shown with its answer in the
reviewer, the answer buttons appear only after RWKV-Curve has given the
intervals for this showing of the card. Until then the button area shows
"Waiting for RWKV-Curve…" and answer keys and clicks do nothing. While it
waits, the reviewer both asks RWKV-Curve again (after 50 ms, doubling up to
once a second) and restores RWKV-Curve's resident state off the main thread,
one restore at a time; leaving the card ends the wait. Asking again alone
would never end the wait: the other review-time restore of the state runs
after an answer, and an answer is blocked while the buttons wait.

The wait covers every reason RWKV-Curve has no intervals yet:

| Reason                                                                                                                      | Ends by itself?                       |
| --------------------------------------------------------------------------------------------------------------------------- | ------------------------------------- |
| its state still loading                                                                                                     | yes                                   |
| its state not loaded (cold after a queue change, a sync or an undo)                                                         | only because the reviewer restores it |
| another RWKV task holding it                                                                                                | yes                                   |
| its state changing during the prediction                                                                                    | yes                                   |
| no prediction                                                                                                               | depends on the run                    |
| an error, also an error while the answer states are built from RWKV-Curve's intervals after the prediction itself succeeded | depends on the run                    |
| a button without an interval                                                                                                | no: see below                         |

After an error no prediction is kept, so an answer stores no RWKV-Curve S90
with states that are not RWKV-Curve's.

The wait always ends. When RWKV-Curve gave a prediction for this showing of
the card and that prediction has no interval for a button, asking again gives
the same answer: the button area says at once that RWKV-Curve has no interval
for this card, and does not wait. Otherwise, after 60 seconds the button area
says that RWKV-Curve did not give the intervals in 60 seconds, that its state
could not be loaded, and what the user can do (try again, restart Clanki, or
choose FSRS-7 for the deck), with a "Try again" button that waits again on the
same card. Without a usable RWKV model it does not wait at all
(`sched.rwkv-no-model-error`). In every one of these cases the buttons stay
hidden: FSRS-7 intervals never stand in for RWKV-Curve's.

Each prediction belongs to one showing: it is cleared before the next
prediction and once an answer has used it, so a later showing of the same card
never reuses its intervals or its S90. A preview in a filtered deck without
rescheduling (no review intervals) and cards of FSRS-7 and RWKV-Instant
presets never wait at the answer buttons (RWKV-Instant waits in the study
queue instead, `sched.rwkv-instant-waits`).

**Why:** Andrew, 2026-09-15: the buttons must never show, and an answer must
never store, the intervals of one algorithm while another is on. Before this
entry, whenever RWKV-Curve had no intervals the buttons showed FSRS-7's and
the answer stored them; and a prediction from an earlier showing of the card
could supply the S90 of a later answer. Andrew, 2026-09-16: the wait never
ended once RWKV-Curve's state went cold in the middle of a session, because
only an answer restored the state and the wait blocked the answer.

**Pinned by:** `test_answer_buttons_wait_for_rwkv_curve_intervals`,
`test_the_waiting_answer_buttons_restore_the_rwkv_curve_state`,
`test_answer_buttons_stop_waiting_for_rwkv_curve_after_a_minute`,
`test_answer_buttons_say_rwkv_curve_has_no_interval_for_the_card`,
`test_answers_are_ignored_while_rwkv_curve_intervals_are_pending`
(`qt/tests/test_reviewer.py`);
`test_answer_intervals_pending_until_rwkv_curve_gives_the_intervals`,
`test_answer_intervals_unavailable_only_when_rwkv_curve_answered`,
`test_the_answer_button_wait_can_restore_the_resident_state`,
`test_failed_rwkv_prediction_leaves_the_buttons_waiting`,
`test_error_building_rwkv_curve_states_leaves_the_buttons_waiting`,
`test_set_answer_rwkv_metadata_clears_the_prediction`
(`qt/tests/test_rwkv_scheduler.py`).

## sched.grade-now-rwkv-curve

Given Grade Now answering cards with one rating (the Browser's Cards >
Grade Now, or `grade_cards_now` in `aqt.operations.scheduling`), each card
is answered as the reviewer answers it once it has shown the card:

- under RWKV-Curve, the answer states are the ones the reviewer's answer
  buttons get from RWKV-Curve's prediction for the card
  (`sched.rwkv-curve-fuzz`: RWKV-Curve's unrounded intervals with review
  fuzz and, while the study queues are loaded, the load balancer and
  sibling dispersal); the card stores RWKV-Curve's S90 for that rating
  (`sched.rwkv-curve-s90`); its review-log row has the review kind, and the
  RWKV retrievability cache the retrievability, that the reviewer's answer
  writes. A card whose answer buttons would wait for RWKV-Curve
  (`sched.rwkv-curve-buttons-wait`: no usable model, the RWKV state not
  ready, other RWKV work holding it for more than 30 s, no prediction, or no
  interval for a button) is left unanswered, and the Browser's message says
  how many cards were not graded. FSRS-7's states never stand in: the
  backend's Grade Now refuses a card of an RWKV-Curve collection whose
  states the caller does not supply. A preview card of a filtered deck that
  does not reschedule gets its preview states, as in the reviewer;
- under RWKV-Instant, the card gets the FSRS states the reviewer stores
  (`sched.rwkv-instant-no-intervals`), with the review kind and RWKV
  retrievability the reviewer's answer writes;
- under FSRS-7, nothing changes.

Each answer takes 0 ms, and all the cards graded at once are one undo step.

**Why:** Andrew's standing rule: never mix two algorithms; hide rather than
fall back. Before this entry, Grade Now under RWKV-Curve answered every card
with FSRS-7's states: a review card that RWKV-Curve gives 200 days and an
S90 of 210 days for Good got FSRS-7's 88 days and FSRS-7's S90 of 88.3 days.

**Pinned by:**
`grade_now_under_rwkv_curve_answers_like_the_reviewer_and_never_with_fsrs7`,
`grade_now_under_rwkv_instant_answers_with_fsrs_states`
(`rslib/src/scheduler/reviews.rs`);
`test_grade_now_gives_rwkv_curve_cards_rwkv_curve_intervals`,
`test_grade_now_leaves_rwkv_curve_cards_without_intervals_unanswered`,
`test_backend_grade_now_refuses_rwkv_curve_cards_without_their_states`,
`test_grade_now_under_rwkv_instant_answers_as_the_reviewer`,
`test_grade_now_under_fsrs7_keeps_the_given_options`
(`qt/tests/test_grade_now.py`).

## sched.rwkv-replay-start-row

Given a card whose review log the RWKV replay reads, the replay starts the
card at its latest learning start (the latest rated Learning row that does not
follow another Learning row). Given a card with no rated Learning row at all,
the replay starts it at its first rated row after its last Forget row, or at
its first rated row when the card has no Forget row. The rows before the start
row are dropped, not merged, and the start row always gets the first-review
treatment: the elapsed sentinel and no previous-interval features. Manual rows
never enter the sequence: Forget (a manual row with a zero ease factor) only
cuts the history, and Set Due Date (a manual row with a non-zero ease factor)
is not a cut point, because it does not reset the card's memory. Modern Anki
writes Set Due Date as a Rescheduled row with a zero ease, and old Anki wrote it
as a Manual row with a non-zero ease factor; neither is rated, so neither cuts.
Only the last Forget counts.

The start row always carries the learn-start state code, whatever the row's own
kind, because the training dataset gives the first surviving row of every card
that code.

The deck option that measures a first review's elapsed time from the card's
creation is the one exception to the first-row treatment: it reaches a **real
Learning start only**, never a fallback start row, which keeps the elapsed
sentinel. **Why:** the training dataset gives every start row the sentinel,
including a relearn start after a Forget, and never a creation age, so the
sentinel is the parity-correct value for any start row. The creation-age option
on a real Learning start is already this fork's own deviation from training, a
deck option Andrew chose; its scope must not widen to rows whose "first review"
is only the first row the collection holds.

**The rule has one implementation: the SQL.** The backend query
(`rwkv_historical_review_rows`, `rslib/src/storage/revlog/mod.rs`) and the
Python replay query (`_historical_rwkv_review_rows`,
`qt/aqt/rwkv_scheduler.py`) each compute the start row and return an
`is_learning_start` column; every reader takes that column. No caller
re-derives the start row, so the reviewer's replay and Grade Now cannot drift
apart: Grade Now reads the same query, filtered to the graded card.

The two SQL texts are held together by the backend fingerprint, which hashes
the rows the backend read and compares them with the rows Python read. A text
that drifts reports `history_is_valid = false` instead of disagreeing in
silence.

The Forget cut applies **only** to a card with no rated Learning row. A card
that has a learning start keeps that start row, so a Forget after it is ignored
and the rows from before it stay. A card with no learning start whose last row
is a Forget gets no start row at all and leaves the replay, because the Forget
reset it and no rated row follows.

**This asymmetry is deliberate; do not "fix" it.** The training dataset builder
drops manual rows before it masks, so a Forget is invisible there unless a
Learning row follows it. Rule 1 therefore reproduces training exactly for a card
that has a learning start. The cut exists only for the card that training never
saw: the one with no Learning row, where the Forget is the only evidence of a
reset.

**Why:** Andrew, 2026-09-16. A review log with no Learning row comes from an
import, from another application or from an old scheduler. Before this entry
such a card could not get the first-review treatment, so the replay read its
first row as a mid-history review with no state behind it. A Forget is the
only event that resets the card's memory, so it is the only point the replay
may cut at.

**Pinned by:** `rwkv_replay_keeps_the_latest_learning_start`,
`rwkv_replay_card_without_a_learning_row_starts_at_its_first_rated_row`,
`rwkv_replay_card_without_a_learning_row_starts_after_its_forget`,
`rwkv_replay_set_due_date_does_not_cut_the_history`,
`rwkv_replay_uses_only_the_last_forget`,
`rwkv_replay_learning_start_wins_over_a_later_forget`,
`rwkv_replay_drops_a_card_whose_last_row_is_a_forget`
(`rslib/src/storage/revlog/mod.rs`) and
`test_historical_fallback_start_row_gets_the_learn_start_state`,
`test_historical_replay_drops_the_rows_before_a_fallback_card_forget`,
`test_historical_replay_keeps_a_learning_start_over_a_later_forget`,
`test_grade_now_and_the_replay_agree_on_a_forgotten_fallback_card`,
`test_historical_rwkv_inputs_do_not_use_creation_for_a_fallback_start`
(`qt/tests/test_rwkv_scheduler.py`) and
`test_replay_sql_that_drifts_from_the_backend_fails_the_fingerprint`
(`qt/tests/test_rwkv_replay_sql_drift.py`).

## sched.rwkv-exact-elapsed

Given a learning card that RWKV predicts for, the elapsed time RWKV gets is
the exact time since the card's last review, the same as for review,
relearning and filtered cards, and the same as the review history RWKV
learns from. Only when the card's last review is unknown does it keep the
scheduling state's elapsed time.

**Why:** Andrew, 2026-09-15. Before this entry a learning card got the
scheduling state's elapsed time, which rebuilds the last review time from the
card's due time minus its current learning step. That is wrong whenever the
card's delay did not come from that step: with no learning steps (a sub-day
interval from the algorithm) it counted only from the due time, and a card
studied ahead of its due time wrapped to about 49,700 days.

**Pinned by:** `test_rwkv_review_input_uses_exact_elapsed_for_learning_cards`,
`test_rwkv_review_input_keeps_state_elapsed_for_learning_without_history`
(`qt/tests/test_rwkv_scheduler.py`).

## sched.sub-day-intervals

Given a card scheduled by FSRS-7 or RWKV-Curve and the unrounded interval
each answer button would give it (FSRS's, or RWKV-Curve's per
`sched.rwkv-curve-fuzz`), every button of every card — new, learning,
relearning or review — is decided the same way, in the order Again, Hard,
Good, Easy:

- a button decided by a remaining learning or relearning step keeps the
  step's delay and takes no part in what follows;
- a button whose unrounded interval is under 12 hours goes to the
  intraday learning queue with that interval in seconds, unrounded and
  without review fuzz (at least the preset's minimum interval, 1 second by
  default), and at least as long as the sub-day button before it; a
  learning card stays learning, and a review or relearning card becomes a
  relearning card with no remaining steps (a passing answer keeps its lapse
  count, and the card's interval field holds a whole number of days, at
  least 1);
- a button of 12 hours or more gets whole days (at least 1) after review
  fuzz, and at least one day more than the day button before it: with all
  four at 12 hours or more, Hard ≥ Again + 1, Good ≥ Hard + 1 and
  Easy ≥ Good + 1. The fuzz range and the load balancer take the unrounded
  interval for every card (new, learning, relearning and review), so, with
  the same fuzz, 6.6 days gives the same range (5–8 days) on a new card as
  on a review card.

For RWKV-Curve the answer curves are searched inside the first day as well
(at 1, 5, 10, 20 and 30 minutes and 1, 2, 3, 4, 6, 8, 12, 16 and 20 hours)
when a curve reaches its target before day 1, then on the day grid (every
day up to 30, then growing by half each step, up to the maximum interval).
The grid points only bracket each crossing (from 0 when the first point is
already below the target); the interval is the point where RWKV-Curve's own
curve meets the target, found on the curve by a regula falsi search to about
a second. RWKV-Curve's curve (a weighted mix of exponentials) has no closed
form for that point, and a straight line between two grid points lands late
because the curve bends upward (by 0.27% on median, up to 6.9% between one
and two days). FSRS-7 uses
the intraday queue whatever its parameters, and so does RWKV-Curve
(`sched.fsrs7-only`: there is no other FSRS model). Once a card has had its
preset's maximum of
same-day reviews for the day (`sched.max-same-day-reviews`) there is no
intraday queue for it, and a sub-day interval rounds up to one day.

**Why:** Andrew, 2026-09-15: both FSRS-7 and RWKV-Curve should freely
schedule intervals under a day for any card and any answer button; the
ordering rule for mixed sub-day and day buttons is the one he approved.
Later the same day, after an audit showed that a sub-day interval longer
than the time left until the day rollover is cut short at the rollover:
"Anything >=12h rounds up to 1d" (a shorter interval that crosses the
rollover stays due at the rollover). The same audit showed the straight
line between grid points overshooting; Andrew chose to find the crossing on
RWKV-Curve's curve itself.
Before this entry only learning and relearning answers under half a day
went intraday, a review card's Again never did (it was clamped to the
minimum lapse interval first), and RWKV-Curve rounded up to whole days.
The FSRS-7 interval audit (2026-09-15; Andrew: fix it) found new, learning
and relearning buttons fuzzed from the interval rounded to whole days (6.6
days gave the range 5–9 days, a review card 5–8), an upstream leftover.

**Pinned by:** `button_intervals::test::*`
(`rslib/src/scheduler/states/button_intervals.rs`),
`scheduling_states_with_intervals_apply_the_fsrs_rules`
(`rslib/src/scheduler/answering/mod.rs`),
`intervals_are_where_the_curve_meets_the_target`,
`pava_crossings_are_ordered_with_per_grade_targets`,
`a_fast_forgetting_curve_gives_a_sub_day_interval`,
`unrounded_intervals_are_not_rounded_after_the_first_day`
(`rslib/src/rwkv/mod.rs`); the existing learning, relearning and review
state tests; `test_rwkv_curve_states_*` and
`test_unrounded_interval_from_recall_curve_keeps_sub_day_crossings`
(`qt/tests/test_rwkv_scheduler.py`).

## sched.rwkv-review-order

Given a preset running RWKV-Curve or RWKV-Instant whose review sort order is
"Retrievability ascending", "Retrievability descending" or "Relative
overdueness", when the study queue gathers due review cards and interday
learning cards that it does not rank by RWKV-Instant queue scores (under
RWKV-Instant only interday learning cards: its review cards come only from
its scores, `sched.rwkv-instant-waits`), it ranks
them by RWKV's own measure and applies the daily limits in that order. A
card's retrievability is its RWKV-Curve retrievability score for today; a
card without a score gets the value of the exponential forgetting curve
through the interval RWKV scheduled for it, target ^ (days since the last
review / interval), where the target is the card's desired retention, else
the preset's. Retrievability ascending puts the lowest first, descending
the highest first. Relative overdueness puts the lowest retrievability /
target first — the key RWKV-Instant ranks its scores by; it is 1 when the
card is due exactly and less the more it is overdue. Ties go by a hash of
card id and modification time, then card id. The FSRS memory state plays no
part.

**Why:** Andrew, 2026-09-15: only one algorithm at a time. Asked which
measure relative overdueness should use, he chose "RWKV curve scores"
(falling back to RWKV's interval for unscored cards), and then the same for
the retrievability orders. Before this entry, relative overdueness came from
an SQL function that applied a one-component FSRS forgetting curve to the
card's FSRS-7 internal stability — neither RWKV's measure nor FSRS-7's —
and the retrievability orders gathered the cards in due-day order.

**Pinned by:** `rwkv_curve_relative_overdueness_uses_rwkv_not_fsrs`,
`rwkv_curve_relative_overdueness_without_scores_uses_the_rwkv_interval`,
`rwkv_curve_retrievability_orders_use_rwkv`
(`rslib/src/scheduler/queue/builder/mod.rs`).

## sched.filtered-deck-one-algorithm

Given a filtered deck whose search term is ordered by "Retrievability
ascending" or "Retrievability descending", a build (create, rebuild) orders
the matching cards by the retrievability of the collection's algorithm only
(`sched.one-global-algorithm`):

| Algorithm    | A card's key                                                    |
| ------------ | --------------------------------------------------------------- |
| FSRS-7       | FSRS-7's R (SM-2's relative overdueness without a memory state) |
| RWKV-Curve   | its stored curve now (`ui.rwkv-curve-r-stored-curve`)           |
| RWKV-Instant | RWKV-Instant's R                                                |

Before the build, Clanki scores the cards that the deck's searches match
with that algorithm and keeps those scores under the deck's own name. The
build reads only them, never scores that the Stats page, a Browser search or
another place published. A card with no value from the algorithm (no RWKV
state, no stored curve, or RWKV not available) goes after every card with a
value, in both directions; among themselves such cards keep the tie order (a
hash of card id and modification time, then card id). Under RWKV no FSRS-7
or SM-2 value stands in, even when FSRS is switched off. Under FSRS-7 no RWKV
value is read.

A search term ordered by "Relative overdueness" follows the same rule.
Under FSRS-7 it is unchanged: elapsed days over FSRS-7's interval at the
desired retention (SM-2's relative overdueness without a memory state).
Under RWKV-Curve and RWKV-Instant a card's key is that algorithm's
retrievability from the same map, divided by the card's desired retention
(its own, else its preset's), lowest first: the ranking of RWKV's own
review order of that name. A card with no RWKV value goes last, and the
FSRS-7 memory state plays no part.

The backend's "RWKV retrievability of a card" call follows the same rule:
RWKV-Instant's R under RWKV-Instant, the curve value under RWKV-Curve, none
under FSRS-7.

**Why:** Andrew, 2026-09-19: fix the algorithm mixing; "the RWKV-Instant part
needs fixing, yeah". Before, the order took the newest score any place had
published, whichever search it scored: under RWKV-Curve that was RWKV-Instant's
rating head, a card outside that search got FSRS-7's or SM-2's value, and a
card without a memory state got SM-2's. On a copy of his collection
(RELEASE build) the deck's own scoring takes 10-15 ms for a deck of 198 due
cards, 1.1-1.4 s for all 25,552 due cards of the collection and 1.7-2.2 s for
every card, on the build's background thread. "Relative overdueness" joined
the rule later the same day, from the approved speed bundle ("Ok to all speed
suggestions"): it still ranked RWKV cards by FSRS-7's memory state.

**Pinned by:** `filtered_deck_retrievability_order_uses_only_the_collections_algorithm`,
`filtered_deck_relative_overdueness_under_rwkv_is_rwkvs_own`
(`rslib/src/scheduler/filtered/mod.rs`),
`test_filtered_deck_prepares_rwkv_scores_for_relative_overdueness`,
`rwkv_retrievability_score_is_the_collections_algorithms`
(`rslib/src/scheduler/service/mod.rs`),
`test_filtered_deck_retrievability_order_scores_its_own_cards`,
`test_filtered_deck_key_matches_the_rust_build`,
`test_filtered_deck_retrievability_prepares_rwkv_candidate_scores`
(`qt/tests/test_rwkv_scheduler.py`)

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
the simulator, the review-retrievability calibration data, and the
retrievability shown in the browser and card info. FSRS-7 has 34
parameters: 35 values come from an older, pre-release FSRS-7 preview, and
fewer than 34 from FSRS-6 or older; neither is FSRS-7.
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
`fsrs7_only_calibration_predictions_without_fsrs7_params_use_the_defaults`
in `rslib/src/scheduler/fsrs/params.rs`;
`ts/routes/card-info/forgetting-curve.test.ts` (`stabilityS90`);
`ts/routes/deck-options/fsrs-params.test.ts`,
`ts/routes/deck-options/fsrs-param-diagnostics.test.ts`.

## sched.fsrs-rs-latest

Given any FSRS-7 computation (memory states, next states, retrievability,
intervals, optimization, evaluation), Clanki uses the latest fsrs-rs: the
dependency follows the `main` branch of open-spaced-repetition/fsrs-rs, and
`Cargo.lock` records the exact commit, which moves only on a deliberate
`cargo update -p fsrs`. Every FSRS-7 value is the crate's own: Clanki keeps
no copy of the FSRS-7 curve or interval solver (the retrievability of the
Browser, Stats, searches, sorts, queue orders and Total Knowledge included).

**Why:** Andrew, 2026-09-16: "don't pin to a specific commit, always use the
latest version of fsrs-rs (there won't be FSRS-8 for years, if ever)"; the
move was approved with the speed bundle on 2026-09-19. Moving from c9562d6 to
c137ee6 removes Burn: FSRS-7 inference is plain `f32` code in the crate, so
Clanki's own copy of the curve (kept only because Burn was slow) went, and
with it any chance of two different values. On a copy of Andrew's collection
(FSRS-7, 46,772 reviewed cards): 17 of 187,088 button intervals move by one
day (all over 360 days: float rounding), memory states differ by under 1e-4
relative, and the new optimizer fits his reviews as well (review-weighted
log loss 0.0003 lower over 9 presets). Measured (120 pairs, RELEASE builds):
memory states from the history 9.6x faster, next states 15.6x faster,
optimization unchanged.

**Pinned by:** `the_curve_is_the_crates_own`
(`rslib/src/scheduler/fsrs/curve.rs`).

## sched.fsrs7-sm2-conversion

Given a card whose FSRS memory state must be inferred from an interval — a
review card with no usable review log (its current interval and ease), a
truncated review log (its first entry's interval and ease), a card the
simulator finds without a memory state, or a card RWKV-Curve answers or
reschedules that has no usable FSRS memory state (RWKV's S90) — the card gets the FSRS-7 state
whose forgetting curve reaches the historical retention (0.9,
`deck-options.historical-retention-fixed`) at that interval: its S90 is the
interval. The state has difficulty 5 (or, for a truncated log whose first
entry FSRS wrote, the difficulty stored in that entry) and a fast stability
of 0.8 times the internal stability; the internal stability is solved for.
Ease is not used. An interval the curve cannot reach gives the nearest
stability bound (0.0001 or 36,500 days). A card RWKV-Curve answers keeps
RWKV's S90 as its S90. The `FsrsNextInterval` add-on API
(`col.fsrs_next_interval`) takes the stability it is given as the card's
S90 and returns the interval of this state at the requested retention.

**Why:** Andrew, 2026-09-15, "yep, do it" (fix the conversion), then "check
RWKV-Curve too, since S90 can (and should) be calculated for it too" and
"fix it ... for cards with missing review logs too". The fsrs crate's FSRS-7
conversion put the interval into the internal stability, which is not the
90% point of FSRS-7's two-component curve: a 100-day interval gave an S90 of
about 226 days, and RWKV-Curve's fallback state did the same with its S90.
The add-on API made the same mistake; Andrew, 2026-09-15: treat its input as
the S90.

**Pinned by:** `sm2_conversion_gives_the_interval_as_s90`,
`truncated_revlog_starting_state_keeps_the_interval_as_s90`,
`scaling_to_an_unreachable_interval_gives_the_stability_bound`,
`fsrs_state_for_an_rwkv_s90_has_that_s90`, `next_interval_api_takes_the_s90`,
`stored_historical_retention_is_ignored`
(`rslib/src/scheduler/fsrs/memory_state.rs`);
`rwkv_s90_answer_without_memory_state_gets_an_fsrs7_state_with_that_s90`
(`rslib/src/scheduler/answering/mod.rs`);
`apply_review_reschedule_without_memory_state_gets_an_fsrs7_state_with_that_s90`
(`rslib/src/scheduler/rwkv.rs`).

## sched.next-state-s90

Given a card answered with FSRS-7, the memory state of each answer's next
state (the scheduling states the reviewer gets, `get_scheduling_states`, the
custom-scheduling JavaScript's `states.*.memoryState`, and add-ons) carries
the S90 of that state as its `stability`: the time until FSRS-7's forgetting
curve for that state reaches 90% recall, as a stored card's stability does.
FSRS-7's internal and fast stabilities are in their own fields
(`stability_internal`, `stability_fast`). Answering stores the S90 computed
from the internal and fast stabilities and the difficulty, so a script that
changes only `stability` does not change the stored state. The "young leech"
check (`leech_only_if_young`) compares Again's S90 with 21 days.

**Why:** Andrew, 2026-09-15: every stability a user or an add-on sees is the
S90, never FSRS-7's internal stability. Before this entry the next states
carried the internal stability in `stability`, so the card info "FSRS Next
S90" row and add-ons showed the internal stability under the S90 name.

**Pinned by:** `next_states_carry_the_s90_as_stability`
(`rslib/src/scheduler/answering/mod.rs`),
`leech_only_if_young_uses_fsrs_stability`
(`rslib/src/scheduler/states/review.rs`).

## sched.fsrs7-fractional-elapsed-time

Given a card answered with FSRS (always FSRS-7, `sched.fsrs7-only`), the
elapsed time FSRS uses for the answer's
retrievability and next states is the exact time since the card's last
review, in fractional days, for every card: new, learning, relearning and
review, in any queue. This is the same elapsed time training and the
memory-state rebuild take from the review log (millisecond timestamps), so
the model sees the same kind of input when it is trained and when it is
used. The day rollover plays no part in it. The elapsed time runs up to the
answer (the review log's timestamp), not up to the moment the card was
shown: the memory state stored with an answer is FSRS-7's next state for the
chosen button at that elapsed time, and the interval stays the one the
button showed. With a custom scheduling script set, the memory state the
answer carries is stored as it is (the script may have set it).

**Why:** Andrew, 2026-09-15: FSRS-7 must use fractional, not integer,
interval lengths as inputs, both in training and in deployment. Before this
entry, review and interday-learning cards got whole days from the rollover
(a review Monday 23:00 answered Wednesday 05:00 with a 04:00 rollover was
2 days, not 1.25), while training used the exact 1.25. The FSRS-7 interval
audit (2026-09-15; Andrew: fix it) then found the stored memory state
computed when the card was shown, so the model saw the elapsed time minus
the answer time: a new card answered Good 20 seconds after being shown got a
4.9% shorter next interval than a rebuild from the review log, Again 10.7%.

**Pinned by:** `fsrs7_gets_fractional_elapsed_time_like_training`,
`fsrs7_review_answer_uses_the_exact_elapsed_time`,
`fsrs7_answer_stores_the_memory_state_at_the_answer_time`,
`rwkv_s90_answer_preserves_undo_and_internal_fsrs_stability` (the custom
script case) (`rslib/src/scheduler/answering/mod.rs`);
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
deck-options save, the RWKV-Curve reschedule, or "Reschedule all cards now"
after a change of the algorithm (sched.algorithm-change-prompt), the
affected cards get a new
interval, due date, memory state and desired retention, and no row is added
to the review log. Advance and Postpone (`sched.advance`, `sched.postpone`)
change only the interval and the due date, and add no row either. Rows of
kind `Rescheduled` that older builds wrote are
still read (statistics keep excluding them). "Set Due Date" and "Forget" are
not rescheduling and still write their `Manual` rows.

**Why:** plan item 3 (Andrew): rescheduling must not write to the card's
history, as the FSRS Helper add-on does it. The rescheduled rows carried no
answer and only cluttered the history and the review count.

**Pinned by:** `reschedule_on_change_writes_no_revlog_rows`
(`rslib/src/scheduler/fsrs/memory_state.rs`);
`a_switch_to_fsrs7_recomputes_memory_states_and_the_reschedule_writes_no_review_log`
(`rslib/src/deckconfig/algorithm.rs`);
`a_move_writes_no_review_log_and_undoes_in_one_step`
(`rslib/src/scheduler/advance_postpone.rs`).

## sched.advance

Given Advance for a scope (a deck and its subdecks — cards in a filtered
deck by their home deck —, the cards selected in the Browser), its
candidates are the review cards of the scope in the review queue (not
suspended, buried or relearning) whose due date (the original due date of a
card in a filtered deck) is after today, less a card reviewed today that is
due tomorrow (nothing to bring forward). Each has a target interval T: the
unrounded day where the collection's forgetting curve for the card
(`sched.advance-postpone-algorithm`) meets its desired retention (the one
stored on the card, else its preset's), searched up to 36,500 days. With e
the whole days since its last review (its last review time, else the
review log's, else its due date minus its interval), its key is
1 − e / max(T, 1), the share of the target interval still to go. Candidates
go in ascending key, then the longer T first, then card id; those with a
key under 0.13 are "relatively safe" to advance. Advancing N cards takes the
first N: each becomes due today (tomorrow when it was reviewed today) and
its interval becomes the days from its last review to that day, at least 1.
No fuzz or load balancer applies: the point is to review these cards now.
The memory state, desired retention and everything else stay as they are;
no review-log row is written (`sched.reschedule-no-revlog`); one undo step
restores every card. The preview gives, per candidate, its retrievability
on its due day and on the new due day (whole days after its last review).

**Why:** Andrew, 2026-09-15: "integrate Advance and Postpone features from
the FSRS Helper add-on, and make them work with RWKV-Curve too". The order,
the 13% safety threshold and "due today" are the add-on's.

**Pinned by:**
`advance_takes_the_cards_closest_to_their_target_first_and_makes_them_due_today`,
`a_deck_scope_takes_subdecks_and_filtered_cards`,
`a_move_writes_no_review_log_and_undoes_in_one_step`
(`rslib/src/scheduler/advance_postpone.rs`).

## sched.postpone

Given Postpone for a scope (as for `sched.advance`), its candidates are the
review cards of the scope in the review queue whose due date is today or
earlier, less the cards whose days since the last review e plus 1 exceed
their home preset's maximum interval (they are counted and left out: there
is no later day to move them to). With T and the desired retention as for
Advance and I the card's interval, its key is (e + 0.075·I) / max(T, 1) − 1,
how far the time until review after the postponement exceeds the target
interval. Candidates go in ascending key, then the longer T first, then
card id; those with a key under 0.15 are "relatively safe" to postpone.
Postponing N cards takes the first N, in that order: each gets the
unrounded interval e + 0.075·I (the middle of the add-on's 5–10%
extension), turned into whole days in its fuzz range, at least e + 1 (never
today or earlier) and at most the maximum interval, with the load balancer
and Easy Days picking the day as in the reschedules (else review fuzz),
seeded per card for its last review; each moved card counts toward the load
of the cards after it. It becomes due that many days after its last review.
Only the interval and the due date change, no review-log row is written,
and one undo step restores every card. The preview gives, per candidate,
its retrievability today and at the unrounded new interval.

**Why:** Andrew, 2026-09-15 (as `sched.advance`). The order and the 15%
threshold are the add-on's; the add-on's own random 5–10% becomes Clanki's
fuzz range and load balancer, so postponed cards spread over lighter days
and respect Easy Days like every other interval.

**Pinned by:**
`postpone_takes_the_least_overdue_cards_first_and_moves_them_past_today`
(`rslib/src/scheduler/advance_postpone.rs`);
`rescheduled_interval_days_are_fuzzed_and_load_balanced`
(`rslib/src/scheduler/fsrs/rescheduler.rs`, the shared fuzz step).

## sched.advance-postpone-algorithm

Given Advance or Postpone, the forgetting curve of every card is the
collection's algorithm's (`sched.one-global-algorithm`), never another's:

- FSRS-7: FSRS-7's curve of the card's whole memory state (internal and
  fast stability, difficulty), with its preset's effective FSRS-7
  parameters (`sched.fsrs7-only`); T comes from the same state.
- RWKV-Curve: the curve RWKV-Curve stored for the card at its last answered
  review, as card info shows it (`ui.card-info-rwkv-curve`); T is where
  that curve meets the desired retention, found on the curve as for the
  answer intervals (`sched.sub-day-intervals`).
- RWKV-Instant: none. Advance and Postpone are not available (there are no
  due dates to move) and do not show (`ui.advance-postpone`).

A card without such a curve — no FSRS-7 memory state, or no stored
RWKV-Curve curve (RWKV's state still loading or busy, no model, no answered
review yet) — is left out and counted; nothing falls back to the other
algorithm. RWKV-Curve's stored S90 is kept (`sched.rwkv-curve-s90-kept`).

**Why:** Andrew, 2026-09-15: make them work with RWKV-Curve too; the
standing rule never to mix two algorithms (hide rather than fall back).

**Pinned by:** `rwkv_curve_uses_the_stored_curves_and_leaves_out_cards_without_one`,
`rwkv_instant_has_no_advance_or_postpone`
(`rslib/src/scheduler/advance_postpone.rs`);
`stored_curves_reach_the_collection_unchanged` (`rslib/src/rwkv/mod.rs`);
`test_rwkv_curve_moves_send_the_stored_curves` (`qt/tests/test_advance_postpone.py`).

## sched.review-scheduler-record

When a card is answered, Clanki records which algorithm scheduled that
review: `fsrs7`, `rwkv_curve` or `rwkv_instant`, the algorithm of the card's
preset at the moment of the answer. One row per review, keyed by the review
log id, in the `review_scheduler` table of the retrievability-cache sidecar
beside the collection.

- The record is written once. A second write of the same review keeps the
  first answer, because the algorithm that scheduled a review cannot change
  afterwards.
- A review answered before this version has no row. It is absent, not
  guessed: nothing in the review log says which algorithm set its interval.
- A failure to write the record never fails the answer. The answer is the
  user's work; the record is ours.
- The table lives in the sidecar, not in the collection, so the collection
  schema and the sync wire protocol are untouched. It does not sync: a review
  answered on another client has no row here.

**Why:** Andrew, 2026-09-20: "for every review, record whether it was
scheduled using FSRS-7, RWKV-Curve or RWKV-Instant. We'll later add another
stat: how well the algorithm performs on all reviews (logloss and AUC) vs how
well it performs _on reviews that it scheduled_." An algorithm judged only on
reviews another algorithm chose is judged on the wrong sample.

**Pinned by:** `every_answer_records_the_algorithm_that_scheduled_it`,
`a_review_keeps_the_algorithm_it_was_first_recorded_with`
(`rslib/src/scheduler/answering/mod.rs`).

## sched.one-global-algorithm

Given a collection with the `schedulingAlgorithm` config key (`fsrs7`,
`rwkvCurve` or `rwkvInstant`), every preset schedules with that algorithm:
each preset carries it as its two RWKV flags (the table in
`deck-options.scheduler-choice`), and the flags are only a copy of the key.
The key is chosen in deck options, in the **Algorithm (global)** dropdown
(`deck-options.scheduler-choice`, Advanced mode only; in Simple mode the
algorithm stays as it is). Preferences has no algorithm setting.

Every write of a preset takes the key's algorithm, whatever flags it
carries: the deck-options save and Add preset / Restore defaults, the
presets of an .apkg import, and the preset writes of add-ons and
AnkiConnect (`col.decks.add_config`, `update_config`, `saveDeckConfig`).
Presets are brought in line with the key when the collection opens and
after every normal sync (`sync.global-algorithm-mirror`); only presets that
differ are written, and nothing at all is written when all agree.

A deck-options save that carries a new algorithm (`scheduling_algorithm`
of the save request) makes it the key before the presets are saved, in the
same undoable step, and turns the collection `fsrs` switch on. A change to
FSRS-7, or any change while `fsrs` was off, computes every card's FSRS-7
memory state again from its review log (no due date changes), so no
RWKV-Curve stability stays behind. Deck options show the key; in a
collection without one, the Default preset's algorithm, and saving that same
value writes nothing. A save without the field keeps the algorithm.

**Why:** Andrew, 2026-09-15: "there should never be a situation where
different decks use different algorithms. Algorithm selection should be a
global thing." Later the same day: it is chosen in deck options, which people
open far more often than Preferences. The key is the source of truth; the
flags stay as a copy so the scheduler, the Qt code and other clients that
read them see the same algorithm.

**Pinned by:** `every_preset_write_takes_the_collection_algorithm`,
`deck_options_save_cannot_change_the_algorithm`,
`deck_options_show_and_change_the_algorithm`,
`a_switch_to_fsrs7_recomputes_memory_states_and_the_reschedule_writes_no_review_log`
(`rslib/src/deckconfig/algorithm.rs`).

## sched.global-algorithm-migration

Given a collection without the `schedulingAlgorithm` key that has cards,
when it opens (or after a sync or an .apkg import) the key becomes the
algorithm of the presets that schedule the most review and relearning cards
(`type` 2 or 3), each card counted by its home deck's preset (a card in a
filtered deck by its original deck; a deck or preset that is missing counts
as the Default preset). With no review or relearning cards, all cards
count. Ties go to RWKV-Curve, then FSRS-7, then RWKV-Instant. Every preset
then takes that algorithm (`sched.one-global-algorithm`); due dates and
memory states do not change. Given a collection without cards, nothing is
written, so a new, empty collection does not need a full sync.

**Why:** Andrew, 2026-09-15: a collection whose presets used different
algorithms keeps the one that schedules the most review cards.

**Pinned by:**
`a_collection_without_an_algorithm_gets_the_one_with_most_review_cards`,
`migration_counts_filtered_cards_by_home_deck_and_breaks_ties`,
`a_collection_without_cards_gets_no_algorithm`
(`rslib/src/deckconfig/algorithm.rs`);
`new_empty_collection_should_not_require_full_sync`
(`rslib/src/sync/collection/tests.rs`).

## sched.algorithm-change-prompt

Given the user saves deck options after changing the Algorithm to FSRS-7 or
RWKV-Curve, a question asks every time, before the save: "Reschedule all
cards now" or "Keep due dates" (closing the question keeps them). The
answer replaces the RWKV-Curve reschedule and the RWKV-Instant refresh that
a desired-retention change in the same save would start
(`deck-options.reschedule-on-change`). "Reschedule all cards now"
gives every card the new algorithm's due date after the change is saved:
FSRS-7 computes every card's memory state and interval with its preset's
parameters; RWKV-Curve runs its reschedule of all decks. Neither writes
review-log rows (`sched.reschedule-no-revlog`). A change to RWKV-Instant
asks nothing, since it has no intervals to reschedule; neither does a save
without a change of the algorithm, or a change that arrives by sync. After
any change the RWKV targets and queue scores are dropped and the study
screens refresh.

**Why:** Andrew, 2026-09-15: on a change of the algorithm, ask each time
whether to reschedule all cards now or keep their due dates; the question
appears only when the algorithm changes.

**Pinned by:** `test_an_algorithm_change_asks_and_then_reschedules`,
`test_no_question_without_an_algorithm_change`,
`test_no_question_for_rwkv_instant`,
`test_the_question_offers_reschedule_or_keep`,
`test_after_an_algorithm_change_the_chosen_reschedule_runs`
(`qt/tests/test_deckoptions.py`);
`a_switch_to_fsrs7_recomputes_memory_states_and_the_reschedule_writes_no_review_log`
(`rslib/src/deckconfig/algorithm.rs`, which also checks that the FSRS-7
reschedule refuses to run under another algorithm).

## sched.rwkv-r-freshness

Given a collection under RWKV-Curve or RWKV-Instant, an RWKV R that Clanki
computed and kept is used again only while it is fresh. It stops being fresh
at once on any change of the collection: a review, an undo or a reset
anywhere (the review of one card moves the RWKV-Instant R of every other
card), and the start of a new day. Between changes it stays fresh for a time
tolerance set by the time since the card's last review:

| Time since the card's last review | Fresh for  |
| --------------------------------- | ---------- |
| under 10 minutes                  | never kept |
| 10 minutes to 1 hour              | 1 minute   |
| 1 hour to 1 day                   | 10 minutes |
| 1 day or more                     | 1 hour     |

A kept set of values (a score map) is fresh for the shortest tolerance of
its cards. No old value is corrected with the stored curve's change over
time: after it goes stale a value is computed again. What follows:

- the Stats page's kept score map (`ui.stats-rwkv-scores-kept`), which the
  Browser's `prop:rwkv…` searches and its Retrievability sort share, is
  scored again once it is no longer fresh;
- a filtered deck scores its own cards when it is built, never from a kept
  map, and no score map, Stats or filtered, takes a card's score from the
  study queue's scores, which carry no time;
- the Browser's Retrievability and Stability cells are computed again when
  their row is drawn after they went stale (`ui.browser-memory-columns`);
- the kept Total Knowledge result (`ui.stats-total-knowledge`) holds each
  day's R at whole days since each rating, so it depends on no time within a
  day; it is dropped on any change of the collection and on a new day.

FSRS-7 and RWKV-Curve keep no R of their own: every reader computes it from
the card's memory state or stored curve at the time it reads.

**Why:** Andrew, 2026-09-19, approving the proposal measured on a replay of
one day of his reviews (1,090 reviews, 38,522 cards): one review moves the
RWKV-Instant R of other cards by 0.16 percentage points in the median card
and up to 38 (siblings 1.4 in the median), so no class of cards can skip a
recomputation; with no review, the 99th percentile of the drift stays under
the 0.5 point the whole-percent display hides for the tolerances above; the
stored curve's change over time does not predict RWKV-Instant's (it was no
better than keeping the old value). A single-card RWKV query costs about
0.4 ms and 50 rows about 3 ms, so computing again is cheap; a
whole-collection map costs about 2 s.

**Pinned by:** `qt/tests/test_browser_memory_columns.py`
(`test_the_time_tolerance_follows_the_time_since_the_last_review`,
`test_a_change_of_the_collection_makes_every_value_stale`,
`test_a_value_is_recomputed_once_its_time_tolerance_has_passed`,
`test_a_new_day_makes_every_value_stale`);
`test_prepare_stats_retrievability_scores_reuse_ends_with_the_time_tolerance`,
`test_rwkv_scores_fresh_until_takes_the_shortest_tolerance_of_the_cards`,
`test_prepare_stats_scores_every_card_now_not_from_the_queue_scores`,
`test_prepare_stats_retrievability_scores_scores_again_after_a_new_day`,
`test_filtered_deck_retrievability_prepares_rwkv_candidate_scores`
(`qt/tests/test_rwkv_scheduler.py`);
`test_the_page_joins_the_running_job_and_the_result_is_kept`
(`qt/tests/test_total_knowledge.py`).
