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
