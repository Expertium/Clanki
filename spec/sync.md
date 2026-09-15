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
touched. With FSRS off nothing runs.

**Why:** upstream PR 4717 (JSchoreels). Cards merge as whole rows by `mtime`,
so a device that recomputed FSRS data before seeing another device's review
kept stale memory state after the sync, and the only cure was a full sync. The
agreed-value exception is Andrew's review finding of 2026-06-20: a momentary
difference in last review time must not wipe a memory state both devices
already held.

**Pinned by:** `fsrs_stale_card_state_is_reconciled_during_sync`,
`fsrs_metadata_conflict_is_reconciled_without_rescheduling`,
`fsrs_itemless_card_state_is_cleared_during_sync`,
`post_sync_reconcile_keeps_agreed_memory_state_of_itemless_card`,
`fsrs_conflicts_are_reconciled_per_preset`,
`fsrs_reconciliation_uses_original_deck_for_filtered_cards`,
`fsrs_reconciliation_respects_deck_overrides_within_one_preset`,
`fsrs_state_is_recomputed_from_reviews_on_both_devices`
(`rslib/src/sync/collection/tests.rs`); `fsrs_sync_conflict_*`
(`rslib/src/sync/collection/chunks.rs`) for what counts as a conflict.

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
package were foreign. A failure leaves the cards as they came and does not stop the
open, the sync or the import. With FSRS off nothing runs. Cards with `s_int`
are never touched.

**Why:** Andrew, 2026-09-15 (interval audit #4). Read as it came, such a
card's FSRS-6 stability became FSRS-7's internal stability as well as its
S90, which made its next intervals about 2.3 times too long. He chose "S90 =
stored stability" for these cards; where the card has a usable review log,
that log already holds the other client's reviews, so the real FSRS-7 state
comes from it.

**Pinned by:** `fsrs7_state_of_a_foreign_card_is_rebuilt_during_sync`,
`fsrs7_state_of_a_foreign_card_is_rebuilt_on_open`
(`rslib/src/sync/collection/tests.rs`);
`imported_foreign_fsrs_state_becomes_an_fsrs7_state`
(`rslib/src/import_export/package/apkg/tests.rs`);
`only_rows_without_the_internal_stability_are_foreign`
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
(`sched.global-algorithm-migration`). The collection's key wins over a
preset's flags because the config table syncs as a whole, newest first, and
presets sync row by row. The step runs after the sync because deck configs
travel before the post-sync passes, and a change written during the sync
would be left unsent. Nothing is written when all presets agree. An
algorithm that changes by sync asks no question
(`sched.algorithm-change-prompt`). The wire protocol does not change.

**Why:** Andrew, 2026-09-15: one algorithm for the whole collection; other
clients only know the per-preset flags, so the mirror must repair what they
change.

**Pinned by:** `sync_reverts_a_preset_another_client_gave_another_algorithm`
(`rslib/src/sync/collection/tests.rs`).
