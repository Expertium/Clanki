# Database

Behaviors of the collection database that Clanki guarantees. See
`spec/README.md` for how these entries work.

## database.sort-field-index

When Clanki opens a collection, the collection holds an index
`ix_notes_sfld_nocase` on `notes (sfld collate nocase)`. Clanki creates it when
it is missing, so a collection that arrives from a full sync download, or from
another client, gets it at the next open. The schema version does not change,
and neither does anything the sync protocol sees.
**Why:** the browser sorts by the sort field (`n.sfld collate nocase`). Without
the index every sorted search reads the whole notes table: on Andrew's
collection (159k cards) 97.1 MB and 215 ms, against 3.6 MB and 46 ms with the
index, which costs 3.5 MB of file (measured 2026-09-16).
**Pinned by:** `sort_field_index_exists_and_the_browser_sort_uses_it`
(`rslib/src/storage/sqlite.rs`)

## database.prediction-read-index

Each of the two per-review prediction tables in the retrievability-cache
sidecar (`search_stats_fsrs_review_retrievability`,
`search_stats_rwkv_review_retrievability`) carries one index,
`ix_fsrs_review_retrievability_covering` and
`ix_rwkv_review_retrievability_covering`, on
`(sample_role, revlog_id, prediction, updated_at, fold_index, source)`.
Those are the five columns the stats read selects plus the two it filters
and orders by, so the read never touches the table. Clanki creates the index
when it is missing and drops the narrow
`ix_*_review_retrievability_role_revlog` that came before it, so a cache
written by an earlier build is upgraded at the next collection open. A
collection with no cache yet gets the index on an empty table, at no cost.

**The one-time cost.** A cache that already holds rows pays for the build
once. Measured on Andrew's cache (1,351,016 RWKV rows and 967,048 FSRS rows,
2026-09-21): **4.47 s on the first open, and 0.1 ms on every open after**.
This is the one-time first-start work that CLAUDE.md item 11 allows, and it
is inside that rule's 15-second limit.

**Why:** the narrow index held only `(sample_role, revlog_id)`, so the read
found each row in the index and then seeked into the table for the other
four columns, once per row. Measured on 2026-09-21, 120 paired reads each,
alternating on one pinned core:

| Read                    | Rows      | Before  | After   | Gain  | p     |
| ----------------------- | --------- | ------- | ------- | ----- | ----- |
| FSRS, storage cost only | 527,441   | 2176 ms | 164 ms  | 92.6% | 2e-21 |
| FSRS, rows returned     | 527,441   | 3022 ms | 750 ms  | 75.1% | 2e-21 |
| RWKV, storage cost only | 1,153,609 | 663 ms  | 374 ms  | 43.5% | 2e-21 |
| RWKV, rows returned     | 1,153,609 | 2044 ms | 1646 ms | 19.3% | 3e-20 |

The two shapes differ because the probe returns its rows into Python, which
costs about a microsecond each and is charged to both arms alike. Clanki
reads these rows in Rust, where that cost is far lower, so the storage
figure is the one the app sees and the row figure is a floor.

The protocol's own shape, the two arms in parallel on pinned cores with the
assignment swapped, was run as well on the FSRS storage read and agreed:
1896 ms against 126 ms, a 93.5% gain. That run also showed why the swap is
required here. The same read measured 2169 ms on one core set of the 5950X
and 1707 ms on the other, so one unswapped run would have reported a number
21% off.

The FSRS table gains most although it holds half the rows: its `final_fit`
rows sit between five folds of `validation_fold` rows, so the seeks the old
index needed landed on scattered pages. The index costs 98 MB on that cache,
after the narrow one it replaces is dropped: the file goes from 332 MB to
430 MB.

**Pinned by:** `the_prediction_read_uses_a_covering_index`,
`the_narrow_prediction_index_is_replaced_not_joined`,
`a_collection_that_arrives_with_the_narrow_index_is_upgraded`
(`rslib/src/storage/revlog/mod.rs`).

## database.legacy-retrievability-cache-cleanup

Given a collection whose main file holds the legacy retrievability cache
tables of the JSchoreels fork (`search_stats_fsrs_review_retrievability`,
`search_stats_rwkv_review_retrievability`), opening it or running Check
Database moves their rows to the retrievability-cache sidecar, drops the
tables, and requires one full sync, so the server copy loses them too.
Given a collection without those tables, Check Database requires no full
sync, the first time or any later time.

**Why:** Andrew, 2026-09-24, "Fix the bugs on our side", for the
cross-cutting review of that day: the first Check Database on every
collection required a full sync even when nothing was dropped, and a marker
kept only on the uploading side made a later Check Database require
another one after a Download. With AnkiDroid in use, each full sync can
drop the unsynced reviews of one side.

**Pinned by:** `check_database_without_legacy_tables_needs_no_full_sync`,
`legacy_retrievability_cache_tables_force_one_way_sync`
(`rslib/src/dbcheck.rs`),
`legacy_main_retrievability_cache_tables_migrate_to_sidecar`
(`rslib/src/storage/revlog/mod.rs`).

## database.sidecar-recovery

Given a collection, Clanki attaches two local databases that sit beside it.
Neither syncs, neither is part of the collection file, and so neither is
seen by a full sync, a backup, official Anki or AnkiDroid:

- `collection.retrievability-cache.sqlite`, the retrievability cache. Every
  table in it but one is computed again from the review log:
  `search_stats_fsrs_review_retrievability` and `fsrs_prediction_coverage`
  by the FSRS-7 prediction pass (`ui.stats-fsrs-predictions-ready`);
  `search_stats_rwkv_review_retrievability`, `review_predictions`,
  `rwkv_curve_sources` and `rwkv_curve_source_tags` by the RWKV recording
  pass (`sched.rwkv-recordings-automatic`); `foreign_card_scan` is a stamp
  whose absence only makes the next open scan once. The one exception is
  `review_scheduler` (`sched.review-scheduler-record`).
- `collection.scheduler-record.sqlite`, the scheduler record: a copy of
  `review_scheduler` and nothing else.

Each answer writes its record into both files in the answer's transaction,
the copy first; a failed write into one does not stop the other. The copy is
attached first, and SQLite commits the attached WAL databases in the order
they were attached, so a crash between the two commits leaves the copy
ahead of the cache, never behind it.

When the collection opens, each file gets a cheap check: its 100-byte header
must be an SQLite header, the file must not be shorter than the page count in
that header (judged only when no WAL holds pages), and it must attach and
give its schema. A file that fails is renamed to
`<name>.damaged-YYYY-MM-DD-HHMMSS.sqlite`, with its `-wal`, `-journal` and
`-shm` files, and a new empty file takes its place; nothing is deleted. A
damaged file that cannot be moved is replaced by a memory database for that
session. The collection always opens. Then the two `review_scheduler` tables
are brought into step: when their row counts or largest review ids differ,
each gets the rows it lacks, plus any rows that can still be read from a
damaged file. In that step a row a file already holds keeps its value.

After the open, nothing is shown unless records were lost; a replaced file is
written to the log. The records count as lost when a damaged file gave
nothing back and the other file cannot stand in for it: it did not exist
before this open, or it was damaged too. Clanki then shows a tooltip for 10
seconds. A replaced cache makes the FSRS-7 prediction pass forget that it
finished today and the RWKV recording pass count its rows again; both then
refill the cache in the background at their usual moments, with no window.

Check Database runs quick_check on the collection, the copy and the cache
separately. A damaged collection stops the check with "collection corrupt",
as before. A damaged copy or cache is replaced as at the open, the check goes
on, and its report says so, and also says when records were lost; a replaced
cache starts the FSRS-7 prediction pass at once. The check keeps VACUUM of the
collection and REINDEX and ANALYZE of all three databases.

**Why:** Andrew, 2026-09-26: "keep a copy + build a new one if the old is
damaged". A retrievability cache that was not a valid database stopped the
collection from opening, and nothing rebuilt it. The open check reads 100
bytes and the file length per file, so it adds no visible time at start-up;
damage inside a page is left to Check Database, whose quick_check of the
cache costs 2.3 s on Andrew's 636 MB cache.

**Pinned by:** `a_garbage_cache_is_replaced_and_keeps_the_record`,
`a_truncated_cache_is_replaced_and_keeps_the_record`,
`a_missing_cache_gets_the_record_back`,
`a_damaged_copy_is_made_again_from_the_cache`,
`records_are_lost_only_when_neither_file_holds_them`,
`a_new_copy_is_filled_from_the_cache`,
`the_two_files_are_brought_into_step_at_open`,
`the_copy_is_attached_before_the_cache`,
`check_database_replaces_a_damaged_cache_and_keeps_the_record`
(`rslib/src/storage/sidecar.rs`);
`the_record_copy_stays_in_step_after_answers_and_undo`
(`rslib/src/scheduler/answering/mod.rs`);
`the_scheduler_record_copy_stays_in_step_through_syncs`
(`rslib/src/sync/collection/tests.rs`);
`test_a_replaced_cache_restarts_the_passes`,
`test_a_replaced_copy_leaves_the_passes_alone`,
`test_lost_records_are_said_only_when_lost`
(`qt/tests/test_sidecar_recovery.py`).

## database.collection-file-locked

While Clanki has a collection open, the collection file itself is locked
against every other connection: a second attempt to read `collection.anki2`,
in this process or another one, fails with "database is locked". The
retrievability-cache sidecar beside it is not locked that way. A second
connection may attach and read `collection.retrievability-cache.sqlite` while
Clanki holds the collection, and sees every row Clanki has committed to it.
**Why:** two processes writing one collection would lose reviews, so the
collection keeps the exclusive lock Anki has always taken. The sidecar holds
no collection data - it is a cache of per-review numbers, never synced and
never read by official Anki or AnkiDroid - and the Stats page reads a million
of its rows beside its read of the review log. SQLite will not take a
database out of exclusive locking once it is in WAL mode, so the sidecar has
to be left out of the lock when the collection opens, not freed later.
**Pinned by:** `the_collection_is_locked_and_the_sidecar_is_not`
(`rslib/src/storage/sqlite.rs`)

## database.dbproxy-read-only

Given a statement that Python code or AnkiDroid runs through the backend's
DB proxy (`col.db.execute`, `scalar`, `all`, `first`, `list`,
`executemany`), the proxy classifies it by what SQLite reports for the
prepared statement, never by its text. A statement is a read when SQLite
reports that it changes nothing in the database (`sqlite3_stmt_readonly`)
**and** it has a result set: result columns, which the prepared statement
has before it runs. A read that matches no row is still a read. A read keeps the Undo step the user has (for
example "Undo Answer Card"), keeps the study queues, and does not mark the
collection modified. Every other statement is a write: it drops the Undo
step and the study queues, and the next commit sets the collection's
modification time, as upstream Anki does for every write. So `WITH ...
SELECT`, `PRAGMA table_info(...)` and `VALUES (...)` are reads, and
`WITH ... DELETE`, `WITH ... UPDATE`, `WITH ... INSERT` and
`INSERT ... RETURNING` are writes. `SAVEPOINT`, `RELEASE`, `BEGIN`, `COMMIT`,
`ROLLBACK`, `ATTACH` and `DETACH` have no result set, so they are writes too,
although SQLite calls them read-only: they change what the connection sees.
A statement that SQLite cannot prepare returns its error and changes
nothing.
**Why:** 2026-09-25 speed hunt: the proxy called every statement that did
not start with SELECT a write. The RWKV history read (`with eligible as
...`), which runs when a new card gets its first answer under RWKV, at
Grade Now, and 65 times in a post-sync refresh, deleted "Undo Answer Card"
and "Undo Grade Now", and made the next screen build the study queue again.
A text rule cannot tell `WITH ... SELECT` from `WITH ... DELETE`; SQLite can.
The classification adds no second prepare: the statement goes into the
connection's statement cache, and the query that follows takes it from there.
**Pinned by:** `a_read_keeps_the_undo_step_whatever_its_first_word`,
`a_write_drops_the_undo_step_whatever_its_first_word`,
`execute_many_of_a_with_write_drops_the_undo_step`,
`a_history_read_after_an_answer_keeps_undo_answer_card`,
`a_statement_that_fails_to_prepare_returns_the_error`
(`rslib/src/backend/dbproxy.rs`);
`grade_now_rebuilds_the_queue_and_a_history_read_keeps_its_undo`
(`rslib/src/scheduler/reviews.rs`);
`test_the_rwkv_history_read_keeps_undo_answer_card`
(`qt/tests/test_rwkv_scheduler.py`).
