// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::borrow::Cow;
use std::cmp::Ordering;
use std::fmt::Display;
use std::hash::Hasher;
use std::path::Path;
use std::path::PathBuf;
use std::sync::atomic::AtomicU64;
use std::sync::Arc;

use bitflags::bitflags;
use fnv::FnvHasher;
use fsrs::current_retrievability;
use fsrs::FSRS5_DEFAULT_DECAY;
use regex::Regex;
use rusqlite::functions::FunctionFlags;
use rusqlite::params;
use rusqlite::trace::TraceEvent;
use rusqlite::Connection;
use serde_json::Value;
use unicase::UniCase;

use super::sidecar::attach_sidecars;
use super::sidecar::replace_damaged_sidecars;
use super::sidecar::SidecarRecovery;
use super::sidecar::SCHEDULER_RECORD_DB_SCHEMA;
use super::upgrades::SCHEMA_MAX_VERSION;
use super::upgrades::SCHEMA_MIN_VERSION;
use super::upgrades::SCHEMA_STARTING_VERSION;
use super::SchemaVersion;
use crate::cloze::strip_clozes;
use crate::config::schema11::schema11_config_as_string;
use crate::error::DbErrorKind;
use crate::prelude::*;
use crate::scheduler::timing::local_minutes_west_for_stamp;
use crate::scheduler::timing::v1_creation_date;
use crate::storage::card::data::CardData;
use crate::text::without_combining;
use crate::text::CowMapping;

fn unicase_compare(s1: &str, s2: &str) -> Ordering {
    UniCase::new(s1).cmp(&UniCase::new(s2))
}

// fixme: rollback savepoint when tags not changed
// fixme: need to drop out of wal prior to vacuuming to fix page size of older
// collections

// currently public for dbproxy
#[derive(Debug)]
pub struct SqliteStorage {
    // currently crate-visible for dbproxy
    pub(crate) db: Connection,
    /// What the open found wrong with the sidecar databases, and did about
    /// it (spec database.sidecar-recovery).
    pub(crate) sidecar_recovery: SidecarRecovery,
}

pub(crate) const RETRIEVABILITY_CACHE_DB_SCHEMA: &str = "retrievability_cache";

fn open_or_create_collection_db(
    path: &Path,
    persistent_retrievability_cache: bool,
) -> Result<(Connection, SidecarRecovery)> {
    let db = Connection::open(path)?;

    if std::env::var("TRACESQL").is_ok() {
        db.trace_v2(
            rusqlite::trace::TraceEventCodes::SQLITE_TRACE_STMT,
            Some(trace),
        );
    }

    db.busy_timeout(std::time::Duration::from_secs(0))?;

    // The collection itself, and only it: one process at a time opens a
    // collection, and this is the lock that says so. Without the schema name
    // the mode would also cover every database attached later, which is what
    // the retrievability-cache sidecar below must not have. SQLite refuses to
    // take an exclusive database back to normal once it is in WAL mode, so
    // the sidecar has to be left out here rather than freed afterwards.
    db.pragma_update(Some("main"), "locking_mode", "exclusive")?;
    db.pragma_update(None, "page_size", 4096)?;
    db.pragma_update(None, "cache_size", -40 * 1024)?;
    // Read pages through a memory map instead of one read call per page: the
    // pages then live in the OS file cache, shared, not in private memory, and
    // a scan of a large table (a deck list after other screens pushed its
    // pages out of the 40 MiB cache above, a sorted browser search) needs no
    // system call per page.
    db.pragma_update(None, "mmap_size", 1_i64 << 30)?;
    db.pragma_update(None, "legacy_file_format", false)?;
    db.pragma_update(None, "journal_mode", "wal")?;
    // Android has no /tmp folder, and fails in the default config.
    #[cfg(target_os = "android")]
    db.pragma_update(None, "temp_store", &"memory")?;

    db.set_prepared_statement_cache_capacity(50);

    add_field_index_function(&db)?;
    add_regexp_function(&db)?;
    add_regexp_fields_function(&db)?;
    add_regexp_tags_function(&db)?;
    add_process_text_function(&db)?;
    add_fnvhash_function(&db)?;
    add_extract_original_position_function(&db)?;
    add_extract_custom_data_function(&db)?;
    add_extract_fsrs_variable(&db)?;
    add_extract_fsrs_retrievability(&db)?;
    add_extract_fsrs_relative_retrievability(&db)?;
    add_card_and_review_changes_function(&db)?;

    db.create_collation("unicase", unicase_compare)?;

    // the retrievability cache, and the copy of the one table in it that
    // nothing can compute again; a damaged one is replaced, so the
    // collection always opens (spec database.sidecar-recovery)
    let recovery = attach_sidecars(&db, persistent_retrievability_cache.then_some(path))?;

    Ok((db, recovery))
}

pub(crate) fn retrievability_cache_path(collection_path: &Path) -> PathBuf {
    if collection_path == Path::new(":memory:") {
        PathBuf::from(":memory:")
    } else {
        collection_path.with_extension("retrievability-cache.sqlite")
    }
}

impl SqliteStorage {
    /// This is provided as an escape hatch for when you need to do something
    /// not directly supported by this library. Please exercise caution when
    /// using it.
    pub fn db(&self) -> &Connection {
        &self.db
    }

    /// A second connection that reads the retrievability-cache sidecar and
    /// nothing else, so a read of it can run beside a read of the
    /// collection instead of after it. The sidecar keeps its schema name,
    /// so the cache's queries run on this connection unchanged, and the
    /// connection can only read: it runs no schema setup and no migration,
    /// and `query_only` refuses a write however it is asked for.
    ///
    /// `None` means "read on the collection's own connection instead",
    /// which gives the same rows:
    /// - A collection in memory, and one opened for a database check, keep the
    ///   cache in memory, and a memory database belongs to the one connection
    ///   that made it.
    /// - An open transaction may hold cache rows that are not committed yet.
    ///   Another connection cannot see those, and reading them is what this is
    ///   for.
    pub(crate) fn open_retrievability_cache_reader(&self) -> Option<Self> {
        if !self.db.is_autocommit() {
            return None;
        }
        match self.try_open_retrievability_cache_reader() {
            Ok(reader) => reader,
            Err(err) => {
                tracing::debug!(?err, "could not open a second reader of the cache");
                None
            }
        }
    }

    fn try_open_retrievability_cache_reader(&self) -> Result<Option<Self>> {
        let path: String = self.db.query_row(
            "select file from pragma_database_list where name = ?1",
            [RETRIEVABILITY_CACHE_DB_SCHEMA],
            |row| row.get(0),
        )?;
        if path.is_empty() {
            return Ok(None);
        }
        let db = Connection::open_with_flags(
            ":memory:",
            rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        // the sidecar is opened for writing and then forbidden to write: a
        // reader that may not create the wal-index cannot open a database
        // in WAL mode at all
        db.busy_timeout(std::time::Duration::from_secs(1))?;
        db.execute(
            &format!("ATTACH DATABASE ? AS {RETRIEVABILITY_CACHE_DB_SCHEMA}"),
            [path.as_str()],
        )?;
        db.pragma_update(
            Some(RETRIEVABILITY_CACHE_DB_SCHEMA),
            "mmap_size",
            1_i64 << 30,
        )?;
        db.pragma_update(None, "query_only", true)?;
        db.set_prepared_statement_cache_capacity(8);
        Ok(Some(Self {
            db,
            sidecar_recovery: SidecarRecovery::default(),
        }))
    }
}
/// Adds sql function card_and_review_changes(): how many rows of the
/// collection's cards and revlog tables this connection has inserted, updated
/// or deleted since it opened, counted by an update hook. Unlike
/// total_changes(), writes to any other table (config, decks) or to an
/// attached database (the retrievability cache) do not count, so a reader can
/// tell that neither table changed without scanning them. One exception: a
/// DELETE without a WHERE clause empties a table without calling the hook, so
/// a reader that must see that too compares the tables' largest ids as well.
fn add_card_and_review_changes_function(db: &Connection) -> rusqlite::Result<()> {
    let changes = Arc::new(AtomicU64::new(0));
    let counter = changes.clone();
    db.update_hook(Some(move |_action, db_name: &str, table: &str, _rowid| {
        if db_name == "main" && (table == "cards" || table == "revlog") {
            counter.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        }
    }));
    db.create_scalar_function(
        "card_and_review_changes",
        0,
        FunctionFlags::SQLITE_UTF8,
        move |_ctx| Ok(changes.load(std::sync::atomic::Ordering::Relaxed) as i64),
    )
}

/// Adds sql function field_at_index(flds, index)
/// to split provided fields and return field at zero-based index.
/// If out of range, returns empty string.
fn add_field_index_function(db: &Connection) -> rusqlite::Result<()> {
    db.create_scalar_function(
        "field_at_index",
        2,
        FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| {
            let mut fields = ctx.get_raw(0).as_str()?.split('\x1f');
            let idx: u16 = ctx.get(1)?;
            Ok(fields.nth(idx as usize).unwrap_or("").to_string())
        },
    )
}

bitflags! {
    pub(crate) struct ProcessTextFlags: u8 {
        const NoCombining = 1;
        const StripClozes = 1 << 1;
    }
}

fn add_process_text_function(db: &Connection) -> rusqlite::Result<()> {
    db.create_scalar_function(
        "process_text",
        2,
        FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| {
            let mut text = Cow::from(ctx.get_raw(0).as_str()?);
            let opt = ProcessTextFlags::from_bits_truncate(ctx.get_raw(1).as_i64()? as u8);
            if opt.contains(ProcessTextFlags::StripClozes) {
                text = text.map_cow(strip_clozes);
            }
            if opt.contains(ProcessTextFlags::NoCombining) {
                text = text.map_cow(without_combining);
            }
            Ok(text.get_owned())
        },
    )
}

fn add_fnvhash_function(db: &Connection) -> rusqlite::Result<()> {
    db.create_scalar_function("fnvhash", -1, FunctionFlags::SQLITE_DETERMINISTIC, |ctx| {
        let mut hasher = FnvHasher::default();
        for idx in 0..ctx.len() {
            hasher.write_i64(ctx.get(idx)?);
        }
        Ok(hasher.finish() as i64)
    })
}

/// Adds sql function regexp(regex, string) -> is_match
/// Taken from the rusqlite docs
type BoxError = Box<dyn std::error::Error + Send + Sync + 'static>;
fn add_regexp_function(db: &Connection) -> rusqlite::Result<()> {
    db.create_scalar_function(
        "regexp",
        2,
        FunctionFlags::SQLITE_DETERMINISTIC,
        move |ctx| {
            assert_eq!(ctx.len(), 2, "called with unexpected number of arguments");

            let re: Arc<Regex> = ctx
                .get_or_create_aux(0, |vr| -> std::result::Result<_, BoxError> {
                    Ok(Regex::new(vr.as_str()?)?)
                })?;

            let is_match = {
                let text = ctx
                    .get_raw(1)
                    .as_str()
                    .map_err(|e| rusqlite::Error::UserFunctionError(e.into()))?;

                re.is_match(text)
            };

            Ok(is_match)
        },
    )
}

/// Adds sql function `regexp_fields(regex, note_flds, indices...) -> is_match`.
/// If no indices are provided, all fields are matched against.
fn add_regexp_fields_function(db: &Connection) -> rusqlite::Result<()> {
    db.create_scalar_function(
        "regexp_fields",
        -1,
        FunctionFlags::SQLITE_DETERMINISTIC,
        move |ctx| {
            assert!(ctx.len() > 1, "not enough arguments");

            let re: Arc<Regex> = ctx
                .get_or_create_aux(0, |vr| -> std::result::Result<_, BoxError> {
                    Ok(Regex::new(vr.as_str()?)?)
                })?;
            let fields = ctx.get_raw(1).as_str()?;
            if ctx.len() == 2 {
                return Ok(fields.split('\x1f').any(|field| re.is_match(field)));
            }

            for (idx, field) in fields.split('\x1f').enumerate() {
                for arg_idx in 2..ctx.len() {
                    let selected_idx: usize = ctx.get(arg_idx)?;
                    if selected_idx == idx && re.is_match(field) {
                        return Ok(true);
                    }
                }
            }

            Ok(false)
        },
    )
}

/// Adds sql function `regexp_tags(regex, tags) -> is_match`.
fn add_regexp_tags_function(db: &Connection) -> rusqlite::Result<()> {
    db.create_scalar_function(
        "regexp_tags",
        2,
        FunctionFlags::SQLITE_DETERMINISTIC,
        move |ctx| {
            assert_eq!(ctx.len(), 2, "called with unexpected number of arguments");

            let re: Arc<Regex> = ctx
                .get_or_create_aux(0, |vr| -> std::result::Result<_, BoxError> {
                    Ok(Regex::new(vr.as_str()?)?)
                })?;
            let mut tags = ctx.get_raw(1).as_str()?.split(' ');

            Ok(tags.any(|tag| re.is_match(tag)))
        },
    )
}

/// eg. extract_original_position(c.data) -> number | null
/// Parse original card position from c.data (this is only populated after card
/// has been reviewed)
fn add_extract_original_position_function(db: &Connection) -> rusqlite::Result<()> {
    db.create_scalar_function(
        "extract_original_position",
        1,
        FunctionFlags::SQLITE_DETERMINISTIC,
        move |ctx| {
            assert_eq!(ctx.len(), 1, "called with unexpected number of arguments");

            let Ok(card_data) = ctx.get_raw(0).as_str() else {
                return Ok(None);
            };

            match &CardData::from_str(card_data).original_position {
                Some(position) => Ok(Some(*position as i64)),
                None => Ok(None),
            }
        },
    )
}

/// eg. extract_custom_data(card.data, 'r') -> string | null
fn add_extract_custom_data_function(db: &Connection) -> rusqlite::Result<()> {
    db.create_scalar_function(
        "extract_custom_data",
        2,
        FunctionFlags::SQLITE_DETERMINISTIC,
        move |ctx| {
            assert_eq!(ctx.len(), 2, "called with unexpected number of arguments");

            let Ok(card_data) = ctx.get_raw(0).as_str() else {
                return Ok(None);
            };
            if card_data.is_empty() {
                return Ok(None);
            }
            let Ok(key) = ctx.get_raw(1).as_str() else {
                return Ok(None);
            };
            let custom_data = &CardData::from_str(card_data).custom_data;
            let Ok(value) = serde_json::from_str::<Value>(custom_data) else {
                return Ok(None);
            };
            let v = value.get(key).map(|v| match v {
                Value::String(s) => s.to_owned(),
                _ => v.to_string(),
            });
            Ok(v)
        },
    )
}

/// eg. extract_fsrs_variable(card.data, 's' | 's_int' | 's_fast' | 'd' |
/// 'dr') -> float | null
fn add_extract_fsrs_variable(db: &Connection) -> rusqlite::Result<()> {
    db.create_scalar_function(
        "extract_fsrs_variable",
        2,
        FunctionFlags::SQLITE_DETERMINISTIC,
        move |ctx| {
            assert_eq!(ctx.len(), 2, "called with unexpected number of arguments");

            let Ok(card_data) = ctx.get_raw(0).as_str() else {
                return Ok(None);
            };
            if card_data.is_empty() {
                return Ok(None);
            }
            let Ok(key) = ctx.get_raw(1).as_str() else {
                return Ok(None);
            };
            let card_data = &CardData::from_str(card_data);
            Ok(match key {
                "s" => card_data.fsrs_stability,
                "s_int" => card_data.fsrs_stability_internal,
                "s_fast" => card_data.fsrs_stability_fast,
                "d" => card_data.fsrs_difficulty,
                "dr" => card_data.fsrs_desired_retention,
                _ => panic!("invalid key: {key}"),
            })
        },
    )
}

/// eg. extract_fsrs_retrievability(card.data, card.due, card.ivl,
/// timing.days_elapsed, timing.next_day_at, timing.now) -> float | null
fn add_extract_fsrs_retrievability(db: &Connection) -> rusqlite::Result<()> {
    db.create_scalar_function(
        "extract_fsrs_retrievability",
        6,
        FunctionFlags::SQLITE_DETERMINISTIC,
        move |ctx| {
            assert_eq!(ctx.len(), 6, "called with unexpected number of arguments");
            let Ok(card_data) = ctx.get_raw(0).as_str() else {
                return Ok(None);
            };
            if card_data.is_empty() {
                return Ok(None);
            }
            let card_data = &CardData::from_str(card_data);
            let Ok(due) = ctx.get_raw(1).as_i64() else {
                return Ok(None);
            };
            let Ok(now) = ctx.get_raw(5).as_i64() else {
                return Ok(None);
            };
            let seconds_elapsed = if let Some(last_review_time) = card_data.last_review_time {
                // This and any following
                // (x as u32).saturating_sub(y as u32)
                // must not be changed to
                // x.saturating_sub(y) as u32
                // as x and y are i64's and saturating_sub will therfore allow negative numbers
                // before converting to u32 in the latter example.
                (now as u32).saturating_sub(last_review_time.0 as u32)
            } else if due > 365_000 {
                // (re)learning card in seconds
                let Ok(ivl) = ctx.get_raw(2).as_i64() else {
                    return Ok(None);
                };
                let last_review_time = (due as u32).saturating_sub(ivl as u32);
                (now as u32).saturating_sub(last_review_time)
            } else {
                let Ok(ivl) = ctx.get_raw(2).as_i64() else {
                    return Ok(None);
                };
                // timing.days_elapsed
                let Ok(today) = ctx.get_raw(3).as_i64() else {
                    return Ok(None);
                };
                let review_day = (due as u32).saturating_sub(ivl as u32);
                (today as u32).saturating_sub(review_day) * 86_400
            };
            let decay = card_data.decay.unwrap_or(FSRS5_DEFAULT_DECAY);
            let retrievability = card_data.memory_state().map(|state| {
                current_retrievability(state.into(), seconds_elapsed as f32 / 86_400.0, decay)
            });
            Ok(retrievability)
        },
    )
}

/// eg. extract_fsrs_relative_retrievability(card.data, card.due,
/// card.ivl, timing.days_elapsed, timing.next_day_at, timing.now) -> float |
/// null. The higher the number, the higher the card's retrievability relative
/// to the configured desired retention.
fn add_extract_fsrs_relative_retrievability(db: &Connection) -> rusqlite::Result<()> {
    db.create_scalar_function(
        "extract_fsrs_relative_retrievability",
        6,
        FunctionFlags::SQLITE_DETERMINISTIC,
        move |ctx| {
            assert_eq!(ctx.len(), 6, "called with unexpected number of arguments");

            let Ok(due) = ctx.get_raw(1).as_i64() else {
                return Ok(None);
            };
            let Ok(interval) = ctx.get_raw(2).as_i64() else {
                return Ok(None);
            };
            /*
            // Unused
            let Ok(next_day_at) = ctx.get_raw(4).as_i64() else {
                return Ok(None);
            };
            */
            let Ok(now) = ctx.get_raw(5).as_i64() else {
                return Ok(None);
            };
            let secs_elapsed = if due > 365_000 {
                // (re)learning card with due in seconds

                // Don't change this to now.subtracting_sub(due) as u32
                // for the same reasons listed in the comment
                // in add_extract_fsrs_retrievability
                (now as u32).saturating_sub(due as u32)
            } else {
                // timing.days_elapsed
                let Ok(today) = ctx.get_raw(3).as_i64() else {
                    return Ok(None);
                };
                let review_day = due.saturating_sub(interval);
                (today as u32).saturating_sub(review_day as u32) * 86_400
            };
            if let Ok(card_data) = ctx.get_raw(0).as_str() {
                if !card_data.is_empty() {
                    let card_data = &CardData::from_str(card_data);
                    if let (Some(state), Some(mut desired_retrievability)) =
                        (card_data.memory_state(), card_data.fsrs_desired_retention)
                    {
                        // avoid div by zero
                        desired_retrievability = desired_retrievability.max(0.0001);
                        let decay = card_data.decay.unwrap_or(FSRS5_DEFAULT_DECAY);

                        let seconds_elapsed =
                            if let Some(last_review_time) = card_data.last_review_time {
                                // Don't change this to now.subtracting_sub(due) as u32
                                // for the same reasons listed in the comment
                                // in add_extract_fsrs_retrievability
                                (now as u32).saturating_sub(last_review_time.0 as u32)
                            } else {
                                secs_elapsed
                            };

                        let current_retrievability = current_retrievability(
                            state.into(),
                            seconds_elapsed as f32 / 86_400.0,
                            decay,
                        )
                        .max(0.0001);

                        return Ok(Some(
                            -(current_retrievability.powf(-1.0 / decay) - 1.)
                                / (desired_retrievability.powf(-1.0 / decay) - 1.),
                        ));
                    }
                }
            }
            let days_elapsed = secs_elapsed / 86_400;
            // FSRS data missing; fall back to SM2 ordering
            Ok(Some(
                -((days_elapsed as f32) + 0.001) / (interval as f32).max(1.0),
            ))
        },
    )
}

/// Fetch schema version from database.
/// Return (must_create, version)
fn schema_version(db: &Connection) -> Result<(bool, u8)> {
    if !db
        .prepare("select null from sqlite_master where type = 'table' and name = 'col'")?
        .exists([])?
    {
        return Ok((true, SCHEMA_STARTING_VERSION));
    }

    Ok((
        false,
        db.query_row("select ver from col", [], |r| r.get(0))?,
    ))
}

fn trace(event: TraceEvent) {
    if let TraceEvent::Stmt(_, sql) = event {
        println!("sql: {}", sql.trim().replace('\n', " "));
    }
}

impl SqliteStorage {
    /// Differs after anything wrote to the collection, or to a database
    /// attached to it: the connection's identity and the rows it changed
    /// since it opened. The collection is opened in exclusive mode, so every
    /// write goes through this connection. A read done in parts, with the
    /// collection free in between, is one read when the stamp is the same
    /// before the first part and after the last.
    pub(crate) fn change_stamp(&self) -> (usize, u64) {
        // SAFETY: the handle's address is read, never the handle
        let connection = unsafe { self.db.handle() } as usize;
        (connection, self.db.total_changes())
    }

    pub(crate) fn open_or_create(
        path: &Path,
        tr: &I18n,
        server: bool,
        check_integrity: bool,
    ) -> Result<Self> {
        let (db, sidecar_recovery) = open_or_create_collection_db(path, !check_integrity)?;
        let (create, ver) = schema_version(&db)?;

        let err = match ver {
            v if v < SCHEMA_MIN_VERSION => Some(DbErrorKind::FileTooOld),
            v if v > SCHEMA_MAX_VERSION => Some(DbErrorKind::FileTooNew),
            12 | 13 => {
                // as schema definition changed, user must perform clean
                // shutdown to return to schema 11 prior to running this version
                Some(DbErrorKind::FileTooNew)
            }
            _ => None,
        };
        if let Some(kind) = err {
            return Err(AnkiError::db_error("", kind));
        }

        if check_integrity {
            let s =
                db.pragma_query_value(None, "integrity_check", |row| row.get::<_, String>(0))?;
            require!(s == "ok", "corrupt: {s}");
        }

        let upgrade = ver != SCHEMA_MAX_VERSION;
        if create || upgrade {
            db.execute("begin exclusive", [])?;
        }

        if create {
            db.execute_batch(include_str!("schema11.sql"))?;
            // start at schema 11, then upgrade below
            let crt = TimestampSecs(v1_creation_date());
            let offset = if server {
                None
            } else {
                Some(local_minutes_west_for_stamp(crt)?)
            };
            db.execute(
                "update col set crt=?, scm=?, ver=?, conf=?",
                params![
                    crt,
                    TimestampMillis::now(),
                    SCHEMA_STARTING_VERSION,
                    &schema11_config_as_string(offset)
                ],
            )?;
        }

        let storage = Self {
            db,
            sidecar_recovery,
        };

        if create || upgrade {
            storage.upgrade_to_latest_schema(ver, server)?;
        }

        if create {
            storage.add_default_deck_config(tr)?;
            storage.add_default_deck(tr)?;
            storage.add_stock_notetypes(tr)?;
        }

        if create || upgrade {
            storage.commit_trx()?;
        }

        storage.ensure_sort_field_index()?;

        if storage.migrate_review_retrievability_cache_to_sidecar()? > 0 {
            storage.mark_review_retrievability_cache_cleanup_full_sync()?;
            storage.set_schema_modified_time(TimestampMillis::now())?;
        }

        Ok(storage)
    }

    /// Creates the index the browser's sort needs, if the collection has none
    /// (spec database.sort-field-index). The schema version does not change,
    /// so a collection stays readable by upstream Anki and by the other
    /// clients; a collection that arrives from a full sync download simply
    /// gets the index the next time it opens.
    pub(crate) fn ensure_sort_field_index(&self) -> Result<()> {
        self.db.execute_batch(
            "create index if not exists ix_notes_sfld_nocase on notes (sfld collate nocase);",
        )?;
        Ok(())
    }

    pub(crate) fn close(self, desired_version: Option<SchemaVersion>) -> Result<()> {
        if let Some(version) = desired_version {
            self.downgrade_to(version)?;
            if version.has_journal_mode_delete() {
                self.db.pragma_update(None, "journal_mode", "delete")?;
            }
        }
        Ok(())
    }

    /// Flush data from WAL file into DB, so the DB is safe to copy. Caller must
    /// not call this while there is an active transaction.
    pub(crate) fn checkpoint(&self) -> Result<()> {
        if !self.db.is_autocommit() {
            return Err(AnkiError::db_error(
                "active transaction",
                DbErrorKind::Other,
            ));
        }
        self.db
            .query_row_and_then("pragma wal_checkpoint(truncate)", [], |row| {
                let error_code: i64 = row.get(0)?;
                if error_code != 0 {
                    Err(AnkiError::db_error(
                        "unable to checkpoint",
                        DbErrorKind::Other,
                    ))
                } else {
                    Ok(())
                }
            })
    }

    // Standard transaction start/stop
    //////////////////////////////////////

    pub(crate) fn begin_trx(&self) -> Result<()> {
        self.db.prepare_cached("begin exclusive")?.execute([])?;
        Ok(())
    }

    pub(crate) fn commit_trx(&self) -> Result<()> {
        if !self.db.is_autocommit() {
            self.db.prepare_cached("commit")?.execute([])?;
        }
        Ok(())
    }

    pub(crate) fn rollback_trx(&self) -> Result<()> {
        if !self.db.is_autocommit() {
            self.db.execute("rollback", [])?;
        }
        Ok(())
    }

    // Savepoints
    //////////////////////////////////////////
    //
    // This is necessary at the moment because Anki's current architecture uses
    // long-running transactions as an undo mechanism. Once a proper undo
    // mechanism has been added to all existing functionality, we could
    // transition these to standard commits.

    pub(crate) fn begin_rust_trx(&self) -> Result<()> {
        self.db.prepare_cached("savepoint rust")?.execute([])?;
        Ok(())
    }

    pub(crate) fn commit_rust_trx(&self) -> Result<()> {
        self.db.prepare_cached("release rust")?.execute([])?;
        Ok(())
    }

    pub(crate) fn rollback_rust_trx(&self) -> Result<()> {
        self.db.prepare_cached("rollback to rust")?.execute([])?;
        Ok(())
    }

    /// Run `func` inside a savepoint named `name`: its writes commit together
    /// on success (nested in any open transaction) and are rolled back on
    /// error. Many single-row writes outside a transaction would each commit
    /// on their own, which dominates filling a temporary table.
    pub(crate) fn in_savepoint<T>(
        &self,
        name: &str,
        func: impl FnOnce() -> Result<T>,
    ) -> Result<T> {
        self.db.execute_batch(&format!("savepoint {name}"))?;
        match func() {
            Ok(value) => {
                self.db.execute_batch(&format!("release {name}"))?;
                Ok(value)
            }
            Err(err) => {
                // the original error is the one to report
                let _ = self
                    .db
                    .execute_batch(&format!("rollback to {name}; release {name}"));
                Err(err)
            }
        }
    }

    //////////////////////////////////////////

    /// true if corrupt/can't access
    /// None when `schema`'s quick_check passes, else what it reported.
    pub(crate) fn quick_check_damage(&self, schema: &str) -> Option<String> {
        match self
            .db
            .pragma_query_value(Some(schema), "quick_check", |row| row.get::<_, String>(0))
        {
            Ok(result) if result == "ok" => None,
            Ok(result) => Some(result),
            Err(err) => Some(err.to_string()),
        }
    }

    /// Checks the two sidecar databases the way Check Database checks the
    /// collection, and replaces a damaged one (spec
    /// database.sidecar-recovery). The result is also what the next
    /// `TakeSidecarRecovery` reports.
    pub(crate) fn check_sidecars(&mut self) -> Result<SidecarRecovery> {
        let copy_damage = self.quick_check_damage(SCHEDULER_RECORD_DB_SCHEMA);
        let cache_damage = self.quick_check_damage(RETRIEVABILITY_CACHE_DB_SCHEMA);
        for (schema, damage) in [
            (SCHEDULER_RECORD_DB_SCHEMA, &copy_damage),
            (RETRIEVABILITY_CACHE_DB_SCHEMA, &cache_damage),
        ] {
            if let Some(damage) = damage {
                tracing::warn!(schema, damage, "sidecar failed quick_check");
            }
        }
        let recovery = replace_damaged_sidecars(&self.db, copy_damage, cache_damage)?;
        self.sidecar_recovery = recovery.clone();
        Ok(recovery)
    }

    pub(crate) fn optimize(&self) -> Result<()> {
        self.db.execute_batch("vacuum; reindex; analyze")?;
        Ok(())
    }

    #[cfg(test)]
    pub(crate) fn db_scalar<T: rusqlite::types::FromSql>(&self, sql: &str) -> Result<T> {
        self.db.query_row(sql, [], |r| r.get(0)).map_err(Into::into)
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum SqlSortOrder {
    Ascending,
    Descending,
}

impl Display for SqlSortOrder {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "{}",
            match self {
                SqlSortOrder::Ascending => "asc",
                SqlSortOrder::Descending => "desc",
            }
        )
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::scheduler::answering::test::v3_test_collection;
    use crate::storage::card::ReviewOrderSubclause;
    use crate::tests::NoteAdder;

    /// SQLite is built without STAT4 (LIBSQLITE3_FLAGS in .cargo/config.toml):
    /// with sqlite_stat4 rows, every query on an indexed column was planned
    /// again for each new bound value.
    #[test]
    fn sqlite_is_built_without_stat4() -> Result<()> {
        let col = Collection::new();
        let db = &col.storage.db;
        let options: Vec<String> = db
            .prepare("pragma compile_options")?
            .query_map([], |row| row.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        assert!(options
            .iter()
            .any(|option| option.starts_with("ENABLE_FTS5")));
        assert!(!options.iter().any(|option| option.contains("STAT4")));
        // ANALYZE keeps no samples
        db.execute_batch("analyze")?;
        let stat4: bool = db.query_row(
            "select exists(select 1 from sqlite_master where name = 'sqlite_stat4')",
            [],
            |row| row.get(0),
        )?;
        assert!(!stat4);
        Ok(())
    }

    /// Card generation takes each note's cards as one run of rows, also when
    /// a note's cards are not next to each other in card id order.
    #[test]
    fn existing_cards_of_a_notetype_come_grouped_by_note() -> Result<()> {
        let mut col = Collection::new();
        let first = NoteAdder::basic(&mut col).add(&mut col);
        let second = NoteAdder::basic(&mut col).add(&mut col);
        // a later card of the first note, after the second note's card
        let mut card = col.storage.all_cards_of_note(first.id)?.remove(0);
        card.id = CardId(0);
        card.template_idx = 1;
        col.add_card(&mut card)?;
        let nids: Vec<NoteId> = col
            .storage
            .existing_cards_for_notetype(first.notetype_id)?
            .iter()
            .map(|card| card.nid)
            .collect();
        assert_eq!(nids, vec![first.id, first.id, second.id]);
        Ok(())
    }

    #[test]
    fn missing_memory_state_falls_back_to_sm2() -> Result<()> {
        let (mut col, _cids) = v3_test_collection(1)?;
        col.set_config_bool(BoolKey::Fsrs, true, true)?;
        col.answer_easy();

        let timing = col.timing_today()?;
        let sql_func = ReviewOrderSubclause::RelativeOverdueness { fsrs: true, timing }
            .to_string()
            .replace(" asc", "");
        let sql = format!("select {sql_func} from cards");

        // value from fsrs
        let mut pos: Option<f64>;
        pos = col.storage.db_scalar(&sql).unwrap();
        assert_eq!(pos, Some(0.0));
        // erasing the memory state should not result in None output
        col.storage.db.execute("update cards set data=''", [])?;
        pos = col.storage.db_scalar(&sql).unwrap();
        assert!(pos.is_some());
        // but it won't match the fsrs value
        assert!(pos.unwrap() < -0.0);
        Ok(())
    }
}

#[cfg(test)]
mod sort_field_index_test {
    use crate::collection::Collection;
    use crate::error::Result;

    /// Pins spec/database.md#database.sort-field-index.
    #[test]
    fn sort_field_index_exists_and_the_browser_sort_uses_it() -> Result<()> {
        let col = Collection::new();

        let exists: bool = col.storage.db.query_row(
            "select exists(select 1 from sqlite_master where type = 'index' and name = ?)",
            ["ix_notes_sfld_nocase"],
            |row| row.get(0),
        )?;
        assert!(exists, "the sort-field index is missing");

        let plan: String = col.storage.db.query_row(
            "explain query plan select n.id from notes n order by n.sfld collate nocase asc",
            [],
            |row| row.get(3),
        )?;
        assert!(
            plan.contains("ix_notes_sfld_nocase"),
            "the browser sort does not use the index: {plan}"
        );

        // a collection that arrives without the index gets it at the next open
        col.storage
            .db
            .execute_batch("drop index ix_notes_sfld_nocase;")?;
        col.storage.ensure_sort_field_index()?;
        let exists: bool = col.storage.db.query_row(
            "select exists(select 1 from sqlite_master where type = 'index' and name = ?)",
            ["ix_notes_sfld_nocase"],
            |row| row.get(0),
        )?;
        assert!(exists, "the index was not created again");

        Ok(())
    }
}

#[cfg(test)]
mod collection_lock_test {
    use tempfile::tempdir;

    use crate::collection::CollectionBuilder;
    use crate::error::Result;
    use crate::storage::sqlite::retrievability_cache_path;
    use crate::storage::sqlite::RETRIEVABILITY_CACHE_DB_SCHEMA;

    /// Pins spec/database.md#database.collection-file-locked.
    #[test]
    fn the_collection_is_locked_and_the_sidecar_is_not() -> Result<()> {
        let dir = tempdir()?;
        let col_path = dir.path().join("locked.anki2");
        let col = CollectionBuilder::new(&col_path).build()?;
        // a row of the sidecar, so that it exists and holds something the
        // second connection can be asked for
        col.storage.set_rwkv_review_retrievability_prediction(
            crate::prelude::RevlogId(1),
            0.25,
            "test",
        )?;

        let second = rusqlite::Connection::open(&col_path)?;
        let locked = second.query_row("select count() from col", [], |row| row.get::<_, i64>(0));
        assert!(
            locked.is_err(),
            "a second connection read the open collection"
        );

        let reader = col
            .storage
            .open_retrievability_cache_reader()
            .expect("no reader of the sidecar");
        let rows: i64 = reader.db.query_row(
            "select count() from retrievability_cache.search_stats_rwkv_review_retrievability",
            [],
            |row| row.get(0),
        )?;
        assert_eq!(rows, 1, "the second connection did not see the row");
        assert!(
            retrievability_cache_path(&col_path).exists(),
            "the sidecar is not a file"
        );
        assert_eq!(RETRIEVABILITY_CACHE_DB_SCHEMA, "retrievability_cache");
        Ok(())
    }

    /// A collection in memory keeps its cache in memory too, and a memory
    /// database belongs to the one connection that made it.
    #[test]
    fn a_collection_in_memory_has_no_second_reader() {
        let col = crate::collection::Collection::new();
        assert!(col.storage.open_retrievability_cache_reader().is_none());
    }
}

#[cfg(test)]
mod card_and_review_changes_test {
    use crate::collection::Collection;
    use crate::config::BoolKey;
    use crate::error::Result;
    use crate::prelude::RevlogId;

    fn changes(col: &Collection) -> Result<i64> {
        col.storage.db_scalar("select card_and_review_changes()")
    }

    /// The count moves with every row of cards and revlog written, and with
    /// nothing else: not config, decks or notes, and not the attached
    /// retrievability cache.
    #[test]
    fn only_card_and_review_rows_are_counted() -> Result<()> {
        let mut col = Collection::new();
        let start = changes(&col)?;

        col.set_config_bool(BoolKey::Fsrs, true, false)?;
        col.storage
            .set_rwkv_review_retrievability_prediction(RevlogId(1), 0.25, "test")?;
        col.storage.db.execute("update col set mod = mod + 1", [])?;
        assert_eq!(changes(&col)?, start, "a write elsewhere was counted");

        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        col.add_note(&mut note, crate::prelude::DeckId(1))?;
        let after_add = changes(&col)?;
        assert!(after_add > start, "adding a card was not counted");

        col.storage
            .db
            .execute("update cards set due = due + 1", [])?;
        let after_update = changes(&col)?;
        assert_eq!(after_update, after_add + 1, "one card row updated");

        col.storage.db.execute(
            "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type) \
             values (1, 1, -1, 3, 1, 0, 2500, 1000, 1)",
            [],
        )?;
        col.storage
            .db
            .execute("delete from revlog where id = 1", [])?;
        assert_eq!(changes(&col)?, after_update + 2, "a review row in and out");
        Ok(())
    }
}
