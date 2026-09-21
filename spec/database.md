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
