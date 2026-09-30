# Sync

The wire protocol (the serialized `Chunk`, `UnchunkedChanges`, `SyncMeta` and
grave shapes, the version constants in `rslib/src/sync/version.rs`, and the
collection schema on full sync) is unchanged from the fork point. Every entry
below is client-side merge logic: what the client does with rows after they
arrive. The sync server (AnkiWeb or a self-hosted server) cannot tell.

## sync.fsrs-reconcile-after-sync

Given a normal sync on a collection with FSRS on, and a card whose local row
was modified since the last sync while the server sent a row for the same card
that differs in memory state, desired retention, decay, last review time,
`interval`, `due` or `original_due` (with FSRS data on at least one side): after
all chunks are merged and before local changes are uploaded, the client
recomputes that card's FSRS data from the merged review log with the card's
current preset (grouping the flagged cards by preset, using the home deck for a
card in a filtered deck, and a deck-level desired-retention override when the
home deck has one). Memory state and last review time are rebuilt from the
merged review log; desired retention and decay are set from the preset. A card
whose merged review log holds no real review (only manual or filtered entries,
or nothing) gets its memory state cleared and its last review time taken from
the log — except that a value both rows agreed on is kept as it is. The
repaired row is marked modified so the same sync uploads it, and the other
device takes it on its next sync. Cards that only differ in deck, card type or
queue, cards the server did not send, and cards not modified locally are not
touched. With FSRS off nothing runs. A card whose home deck is missing or
filtered, or whose preset is missing, is computed with the Default preset
(`sched.fsrs7-preset-fallback`), and an unparsable "ignore reviews before"
date counts as no date (`sched.fsrs7-bad-ignore-before-date`); a failure
leaves the cards as the merge left them and does not stop the sync.

**Why:** upstream PR 4717 (JSchoreels). Cards merge as whole rows by `mtime`,
so a device that recomputed FSRS data before seeing another device's review
kept stale memory state after the sync, and the only cure was a full sync. The
agreed-value exception is Andrew's review finding of 2026-06-20: a momentary
difference in last review time must not wipe a memory state both devices
already held. The damaged-card rule: Andrew, 2026-09-24, "Fix the bugs on
our side", for the cross-cutting review of that day: one such card in a
conflict failed the whole normal sync, and every retry failed the same way.

**Pinned by:** `fsrs_stale_card_state_is_reconciled_during_sync`,
`fsrs_metadata_conflict_is_reconciled_without_rescheduling`,
`fsrs_itemless_card_state_is_cleared_during_sync`,
`post_sync_reconcile_keeps_agreed_memory_state_of_itemless_card`,
`fsrs_conflicts_are_reconciled_per_preset`,
`fsrs_reconciliation_uses_original_deck_for_filtered_cards`,
`fsrs_reconciliation_respects_deck_overrides_within_one_preset`,
`fsrs_state_is_recomputed_from_reviews_on_both_devices`
(`rslib/src/sync/collection/tests.rs`); `fsrs_sync_conflict_*`
(`rslib/src/sync/collection/chunks.rs`) for what counts as a conflict;
`a_damaged_card_does_not_stop_the_repair_or_the_reconcile`
(`rslib/src/scheduler/fsrs/memory_state.rs`).

## sync.fsrs7-state-of-foreign-cards

Given a card whose stored memory state has a stability (`s`) and a
difficulty (`d`) but no FSRS-7 internal stability (`s_int`) — Clanki always
writes it, so the row was last written by another client, such as official
Anki or AnkiDroid, which drop `s_int` and `s_fast` and compute an FSRS-6
state — Clanki gives the card an FSRS-7 memory state again with its home
preset's parameters (the home deck for a card in a filtered deck):

- from the card's review log, the way the post-sync reconcile does
  (`sync.fsrs-reconcile-after-sync`), when the log holds a usable review;
  the last review time then comes from the log;
- otherwise, the FSRS-7 state whose S90 is the stored stability, with the
  stored difficulty (clamped to 1–10) and the fast/internal ratio of the
  fsrs crate's interval conversion; the last review time stays.

Desired retention and decay are set from the preset. Due dates, intervals and
the review log do not change. The repaired row is marked modified, so a sync
uploads it. This runs when the collection opens (which covers a full download
and a restored backup), in every normal sync after the reconcile and before
the upload (so the same sync uploads the rows it repaired), and after an
`.apkg` import with scheduling, for the imported cards whose rows in the
package were foreign. A card whose home deck is missing or filtered, or
whose preset is missing, is repaired with the Default preset
(`sched.fsrs7-preset-fallback`), and an unparsable "ignore reviews before"
date counts as no date (`sched.fsrs7-bad-ignore-before-date`), so one such
card does not stop the repair of the others. A failure leaves the cards as
they came and does not stop the open, the sync or the import. With FSRS off
nothing runs. Cards with `s_int` are never touched.

The open-time pass reads every card, so it runs only when the collection
changed: after it runs, the collection's change time (`col.mod`) is saved in
the retrievability-cache sidecar (local, not synced; the collection itself is
not written), and an open whose change time equals the saved one skips it. A
program that edits the file without changing `col.mod` is seen at the first
open after the next change. A missing sidecar counts as changed.

**Why:** Andrew, 2026-09-15 (interval audit #4). Read as it came, such a
card's FSRS-6 stability became FSRS-7's internal stability as well as its
S90, which made its next intervals about 2.3 times too long. He chose "S90 =
stored stability" for these cards; where the card has a usable review log,
that log already holds the other client's reviews, so the real FSRS-7 state
comes from it. The damaged-card rule: Andrew, 2026-09-24, "Fix the bugs on
our side": one card with a missing home deck failed the repair of every
card, so all of them kept the FSRS-6 stability as their internal one.
The skip: Andrew, 2026-09-25: the scan cost 20-26 ms on every open; he
accepted that an edit which leaves the change time alone waits for the next
change.

**Pinned by:** `fsrs7_state_of_a_foreign_card_is_rebuilt_during_sync`,
`fsrs7_state_of_a_foreign_card_is_rebuilt_on_open`,
`the_open_scan_for_foreign_cards_waits_for_a_change`
(`rslib/src/sync/collection/tests.rs`);
`imported_foreign_fsrs_state_becomes_an_fsrs7_state`
(`rslib/src/import_export/package/apkg/tests.rs`);
`only_rows_without_the_internal_stability_are_foreign`,
`a_damaged_card_does_not_stop_the_repair_or_the_reconcile`
(`rslib/src/scheduler/fsrs/memory_state.rs`).

## sync.post-sync-reschedule-gate

Given a card flagged by `sync.fsrs-reconcile-after-sync` whose two rows also
differed in `interval`, `due` or `original_due`, the client restores the
card's schedule only when the collection's remembered "Reschedule cards on
change" choice (`deck-options.reschedule-choice-remembered`) is on, the card
is a review card that is not suspended, and the merged review log's latest
real review scheduled a day interval. It then sets the card's interval to the
interval that review scheduled and its due day to the review's day plus that
interval — into `original_due` while the card sits in a filtered deck, leaving
its position in the filtered deck alone. No new interval is computed with
FSRS, and no fuzz or load balancing is applied, so the card lands exactly where
the reviewing device put it. In every other case (choice off, card only moved
deck, card in learning or suspended, latest real review left the card in
(re)learning, or a reset since the last review) the schedule is left as the
row merge decided.

**Why:** Andrew's review of PR 4717 (2026-06-20): the PR recomputed a fresh
interval with fuzz for every schedule conflict, so the due date matched
neither device, it ignored the "Reschedule cards when desired retention changes" opt-out, and it
fired on pure deck moves. Restoring the last real review's interval makes the
two devices agree and needs no whole-collection load-balancer scan per card.

**Pinned by:** `fsrs_stale_card_state_is_reconciled_during_sync`,
`post_sync_reconcile_keeps_stale_schedule_when_reschedule_on_change_is_off`,
`post_sync_reconcile_leaves_schedule_alone_when_card_only_moved_deck`,
`fsrs_mixed_schedule_and_metadata_conflicts_reconcile_selectively`,
`fsrs_filtered_card_schedule_conflict_uses_original_deck`
(`rslib/src/sync/collection/tests.rs`);
`restore_review_schedule_after_sync_*` and
`last_revlog_info_*_scheduled_interval*`
(`rslib/src/scheduler/fsrs/memory_state.rs`);
`fsrs_sync_conflict_ignores_a_card_that_only_moved_deck`,
`fsrs_sync_conflict_ignores_type_and_queue_changes_alone`
(`rslib/src/sync/collection/chunks.rs`).

## sync.no-unforget-on-sync

Given a card that one device forgot (reset to new, with the reset logged as a
manual review-log entry) after the other device had modified the card's FSRS
data, and the forgotten row winning the merge, the sync leaves the card new
with no memory state on both devices: the reconcile pass treats the reset
entry as the end of the card's usable history and does not rebuild memory
state or a schedule from the reviews before it.

**Why:** Andrew's review of PR 4717 (2026-06-20): a reconcile pass that
rebuilt state from the full review log could silently un-forget a card.

**Pinned by:** `sync_does_not_unforget_a_card`
(`rslib/src/sync/collection/tests.rs`),
`last_revlog_info_has_no_scheduled_interval_after_a_reset`
(`rslib/src/scheduler/fsrs/memory_state.rs`).

## sync.no-revlog-rows-from-post-sync-reschedule

Given any card the post-sync reconcile pass rewrites — memory state, desired
retention, decay, last review time or schedule — the card's review log gains
no row: after the sync each device holds exactly the real reviews and manual
entries the two devices logged, and no `Rescheduled` entry.

**Why:** plan item 3 — rescheduling must not write to the card's history; the
review log is input to FSRS optimization and must hold only what the user did.

**Pinned by:** `fsrs_stale_card_state_is_reconciled_during_sync` (row count
unchanged by a reschedule), `post_sync_reconcile_keeps_stale_schedule_when_reschedule_on_change_is_off`,
`post_sync_reconcile_leaves_schedule_alone_when_card_only_moved_deck`,
`sync_does_not_unforget_a_card`, and the `assert_no_reschedule_rows` checks in
the other reconcile tests (`rslib/src/sync/collection/tests.rs`).

## sync.global-algorithm-mirror

Given a normal sync, once it has finished, the presets are brought in line
with the collection's algorithm (`sched.one-global-algorithm`) in a separate
step: a preset that another client switched to another algorithm (a client
that knows only the preset flags, such as an older build, AnkiDroid or an
add-on) gets the collection's algorithm back, and the next sync uploads it.
A collection that arrived without the `schedulingAlgorithm` key gets one
(`sched.global-algorithm-migration`). When the collection's algorithm is
FSRS-7, the cards of a preset that the step moves from RWKV-Curve get their
FSRS-7 memory state computed again from their review logs (due dates stay),
so the RWKV-Curve S90 another client wrote is not shown as FSRS-7's. A
collection whose FSRS switch the sync turned off follows `sched.no-sm2`,
which compares the user's last choice in Clanki with the sync before this
one. The
collection's key wins over a preset's flags because the config table syncs
as a whole, newest first, and presets sync row by row. The step runs after the sync because deck configs
travel before the post-sync passes, and a change written during the sync
would be left unsent. Nothing is written when all presets agree. An
algorithm that changes by sync asks no question
(`sched.algorithm-change-prompt`). The wire protocol does not change.

**Why:** Andrew, 2026-09-15: one algorithm for the whole collection; other
clients only know the per-preset flags, so the mirror must repair what they
change.

**Pinned by:** `sync_reverts_a_preset_another_client_gave_another_algorithm`
(`rslib/src/sync/collection/tests.rs`);
`a_move_to_fsrs7_recomputes_the_cards_of_rwkv_curve_presets`
(`rslib/src/deckconfig/algorithm.rs`).

## sync.algorithm-change-syncs

Given the user saves deck options with a new algorithm (the Algorithm
dropdown, `sched.one-global-algorithm`) in a profile with a sync account,
Clanki starts a normal sync once the save, and the reschedule the user chose
with it (`sched.algorithm-change-prompt`), have finished. It is the sync of
the sync button (the same progress window, errors and post-sync work), and
it runs only when the collection needs a normal sync: with a full sync
needed (on this side, or found only when the server answers) it starts
none and asks nothing, and the sync button offers the full sync as before.
Without a sync account, while a media sync runs, or when the sync status
check gets no answer from the server, nothing happens. A failed sync shows
what a failed sync from the button shows.

**Why:** Andrew, 2026-09-25: the config syncs as one block and the side that
changed last replaces the other side's, so an algorithm choice that waits
for the next sync can be replaced by another device's newer settings.
Sending it at once closes most of that window.

**Pinned by:** `test_an_algorithm_change_syncs_at_once`,
`test_after_an_algorithm_change_the_chosen_reschedule_runs`
(`qt/tests/test_deckoptions.py`);
`test_the_sync_after_an_algorithm_change_does_not_ask_for_a_full_sync`
(`qt/tests/test_main.py`);
`test_a_sync_that_must_not_ask_leaves_a_full_sync_to_the_user`
(`qt/tests/test_sync.py`).

## sync.algorithm-change-notice

Given a normal sync after which the collection's algorithm (the one deck
options show) differs from the one before the sync, Clanki shows a
tooltip for 10 seconds in place of "Collection sync complete.": "A sync
from another device turned FSRS off, so Clanki now uses RWKV-Curve." when
the sync turned the FSRS switch off (`sched.no-sm2`), otherwise "A sync
changed the scheduling algorithm. Clanki now uses FSRS-7." (with the
algorithm's name). The notice is a tooltip, not a window, so it blocks
nothing and shows at no start-up; each such sync shows it once. A sync
after which the algorithm is the same shows no notice, also when the pass
put the user's choice back. The Rust sync returns the change as
`SyncCollectionResponse.algorithm_changed_to` (the algorithm's stored name)
and `algorithm_changed_by_fsrs_off`; the wire protocol does not change.

**Why:** Andrew, 2026-09-25: a sync that changes the algorithm must say
so, without a modal window ("No waiting windows", CLAUDE.md item 11).

**Pinned by:**
`a_sync_bringing_fsrs_off_newer_than_the_users_choice_moves_to_rwkv_curve`,
`a_users_choice_newer_than_another_devices_fsrs_off_stays_after_sync`
(`rslib/src/sync/collection/tests.rs`);
`test_a_sync_that_changed_the_algorithm_says_so_once`
(`qt/tests/test_sync.py`).

## sync.full-sync-stops-background-passes

Given a full sync (a download or an upload) that begins while RWKV or FSRS-7
background work runs (the start-up restore or build of the RWKV state, the
recording pass, the exact rebuild, the idle save of the stored state, the
FSRS-7 prediction and auto-optimize pass), the sync first stops that work,
the way the close does (`ui.close-stops-rwkv-work`): each pass stops at its
next check, a writer drops the rows of its unfinished batch, and the sync
waits at most 10 seconds for the passes before it takes the collection. A
pass still inside one long call after that stops at its next check all the
same, and writes nothing into the collection the sync brought. A pass that
the full sync stopped shows no message; a build stopped this way does not
say that the review history could not be read. The FSRS-7 pass does not
start while the full sync runs.

When the sync has reopened the collection, nothing the passes knew of the
old collection carries over: the exact rebuild's wanted work and its undo
entries go, as at a profile open, and after a download the post-sync refresh
builds the RWKV state of the collection as it is now (an upload keeps it:
`sync.full-upload-keeps-rwkv-state`). The FSRS-7 pass is asked for again;
it waits for that refresh.

Given any sync that changed the collection while the start-up restore or
build of the RWKV state runs, the post-sync refresh takes over: the restore
or build ends without a message and without starting a build of its own,
and the FSRS-7 pass keeps waiting until the refresh has ended. Between two
presets the FSRS-7 pass waits while a restore, build or post-sync refresh of
the RWKV state runs.

Given a close that stopped waiting for the exact rebuild, the first rebuild
request of the collection opened next starts a rebuild for that collection.
The old collection's rebuild thread does not take the request.

**Why:** bug hunt of 2026-09-26, reproduced on 2026-09-29 with a local sync
server: a full download 45 seconds into the start-up build of a collection
of 868,034 reviews showed "Your review history could not be read.". The
stopped build also let the FSRS-7 pass start during the post-sync refresh;
the pass's new parameters threw the refresh's state away after 9 seconds, so
there was no RWKV state after the sync, and the stored state still belonged
to the old collection. A full sync keeps the same collection object, so the
checks a pass made against a closed profile could not see it.

**Pinned by:** `test_a_full_sync_stops_the_background_passes_first`
(`qt/tests/test_sync.py`);
`test_a_full_sync_stops_the_recording_pass_and_the_writers`,
`test_a_pass_that_outlived_the_full_sync_wait_still_stops`,
`test_a_build_that_a_full_sync_stopped_reports_nothing`,
`test_a_build_superseded_by_the_post_sync_refresh_leaves_it_the_flag`,
`test_a_build_that_fails_by_itself_still_says_so`,
`test_a_full_sync_drops_the_rebuild_the_old_collection_wanted`,
`test_the_rebuild_thread_of_a_closed_profile_takes_no_new_request`
(`qt/tests/test_rwkv_scheduler.py`);
`test_a_full_sync_stops_the_pass_and_asks_for_it_again`
(`qt/tests/test_fsrs_predictions.py`).

## sync.full-upload-keeps-rwkv-state

Given a full upload (after the conflict question, or to an empty server)
while the resident RWKV state is ready and its replay semantics key still
matches the collection's, the state stays through the reopen and through
the reset after the sync, and no post-sync refresh runs: no "review history
after sync" window opens, and the first answer after the upload waits for
nothing. The same holds when the upload fails or is cancelled. The upload
changes only sync bookkeeping in the local file (graves, pending sync
numbers, the collection's sync number, schema time, last sync time and
modification time), none of which the replay reads. A state kept after a
delete or a move of cards with reviews, which waits for its exact rebuild
(`sched.rwkv-history-change-keeps-state`), stays too: the reopen drops the
rebuild's plans with the rest of the old open, so the rebuild is asked for
again, and it runs once the full sync has ended. The stored cache keeps its
history-change mark, so a close before that rebuild saves still gives the
quick open. A state whose semantics key no longer matches (after a preset
change) goes, and the post-sync refresh builds it as before. A full download
always goes through the refresh. When the refresh does not run, the maintenance it starts when it
ends (the re-read of a history with skipped synced reviews) starts when the
full sync ends.

Given File > Export > Anki Collection Package (.colpkg), which closes the
collection and opens it again, the export does the same as a full upload:
the RWKV and FSRS-7 background passes stop before the close
(`sync.full-sync-stops-background-passes`), and after the reopen, also after
a failed export, the resident state stays under the same rule. When it
cannot stay, it is restored or built in the background, as at start-up,
with no window. A build that the export stopped says nothing.

Given any such reopen (a full sync or the export), the undo steps of the
new open count from 1 again, so the RWKV undo entries of the old open go:
an undo in the new open never rolls an answer of the old open out of the
resident state.

**Why:** measured on 2026-09-29 with a local sync server and a collection
of 868,308 reviews: after a full upload the post-sync refresh threw the
state away and restored it behind a window for 1.8 s, although the
collection held the same reviews. A .colpkg export during the start-up build
showed "Your review history could not be read." and left no state; after an
export with the state kept, undoing a flag change whose undo count was an
earlier answer's rolled that answer out of the RWKV state. With a note
with reviews deleted just before the upload (the exact rebuild still
running), the state went, and the refresh built the whole history behind the
window for 99 s; kept, no window opens, the first answer is ready in 0.6 s,
and the rebuild swaps in 96 s later, with values bit for bit the same as a
build from the whole history.

**Pinned by:** `test_a_full_upload_keeps_the_rwkv_state_through_its_reopen`,
`test_a_full_download_does_not_keep_the_rwkv_state`
(`qt/tests/test_sync.py`);
`test_a_full_upload_that_kept_the_rwkv_state_skips_the_post_sync_refresh`
(`qt/tests/test_main.py`);
`test_a_full_upload_keeps_the_resident_state_through_its_reopen`,
`test_a_reopen_forgets_the_undo_frames_of_the_last_open`,
`test_the_end_of_a_full_sync_that_kept_the_state_starts_the_maintenance`,
`test_a_colpkg_export_keeps_the_state_or_restores_it_without_a_window`
(`qt/tests/test_rwkv_scheduler.py`);
`test_a_colpkg_export_stops_the_passes_and_keeps_the_rwkv_state`
(`qt/tests/test_colpkg_export.py`).
