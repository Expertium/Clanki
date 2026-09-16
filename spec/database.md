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
