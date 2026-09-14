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
