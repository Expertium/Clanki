// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The two databases Clanki attaches beside the collection, and what it does
//! when one of them is damaged (spec database.sidecar-recovery).
//!
//! - The retrievability-cache sidecar
//!   (`collection.retrievability-cache.sqlite`) holds per-review numbers that
//!   the FSRS-7 prediction pass and the RWKV recording pass compute again from
//!   the review log, plus one table nothing can compute again:
//!   `review_scheduler`, which algorithm scheduled each review.
//! - The scheduler record (`collection.scheduler-record.sqlite`) is a copy of
//!   that one table, so a damaged sidecar cannot take it along.
//!
//! Neither file syncs, and neither is part of the collection file, so no
//! other client, full sync or backup sees them.
//!
//! A file that fails its check at open is moved aside (renamed with the
//! date, never deleted) and a new one is made. The collection always opens:
//! when a damaged file cannot even be moved, that database is kept in
//! memory for the session.

use std::fs;
use std::io::Read;
use std::path::Path;
use std::path::PathBuf;

use rusqlite::Connection;
use rusqlite::OpenFlags;

use super::revlog::REVIEW_SCHEDULER_TABLE;
use super::sqlite::retrievability_cache_path;
use super::sqlite::RETRIEVABILITY_CACHE_DB_SCHEMA;
use crate::prelude::*;

/// The schema name of the copy of `review_scheduler`.
pub(crate) const SCHEDULER_RECORD_DB_SCHEMA: &str = "scheduler_record";

/// The two sidecars in the order they are attached. The copy comes first:
/// SQLite commits the attached WAL databases of one transaction one after
/// another in this order, so a crash between the two commits leaves the
/// copy ahead of the cache, never behind it.
const SIDECARS: [&str; 2] = [SCHEDULER_RECORD_DB_SCHEMA, RETRIEVABILITY_CACHE_DB_SCHEMA];

/// The files that travel with a database when it is moved aside. They go
/// before the database itself: a WAL or a hot journal left beside a new,
/// empty file would be read into it.
const COMPANION_SUFFIXES: [&str; 3] = ["-wal", "-journal", "-shm"];

/// What the open (or Check Database) found and did about the two sidecars.
#[derive(Debug, Default, Clone, PartialEq, Eq)]
pub struct SidecarRecovery {
    /// The retrievability cache failed its check and was replaced. Its
    /// recomputable rows are rebuilt by the passes that make them.
    pub cache_replaced: bool,
    /// The copy of the scheduler record failed its check and was replaced;
    /// it was filled again from the cache.
    pub record_copy_replaced: bool,
    /// Rows of the scheduler record may be gone: the file that held them was
    /// damaged, nothing could be read from it, and the other file could not
    /// stand in for it.
    pub records_lost: bool,
    /// Where the damaged files were moved.
    pub moved_aside: Vec<PathBuf>,
}

impl SidecarRecovery {
    pub(crate) fn anything_replaced(&self) -> bool {
        self.cache_replaced || self.record_copy_replaced
    }
}

pub(crate) fn scheduler_record_path(collection_path: &Path) -> PathBuf {
    if collection_path == Path::new(":memory:") {
        PathBuf::from(":memory:")
    } else {
        collection_path.with_extension("scheduler-record.sqlite")
    }
}

fn sidecar_path(schema: &str, collection_path: &Path) -> PathBuf {
    if schema == SCHEDULER_RECORD_DB_SCHEMA {
        scheduler_record_path(collection_path)
    } else {
        retrievability_cache_path(collection_path)
    }
}

type SchedulerRecordRow = (i64, String, i64);

/// How one sidecar ended up attached.
#[derive(Debug)]
enum Attached {
    /// The file passed its check. `existed` is false when it was not there
    /// and SQLite made it.
    Kept { existed: bool },
    /// The file failed its check. `salvaged` holds the scheduler-record rows
    /// that could still be read from it, or None when none could.
    Replaced {
        aside: Option<PathBuf>,
        salvaged: Option<Vec<SchedulerRecordRow>>,
    },
    /// A memory database by design: the collection is in memory, or opened
    /// only to check a downloaded file.
    Memory,
}

impl Attached {
    fn replaced(&self) -> bool {
        matches!(self, Self::Replaced { .. })
    }

    /// The file can stand in for the other one: it passed its check and held
    /// its rows before this open, or rows could still be read from it.
    fn can_stand_in(&self) -> bool {
        match self {
            Self::Kept { existed } => *existed,
            Self::Replaced { salvaged, .. } => salvaged.is_some(),
            Self::Memory => false,
        }
    }

    fn salvage_failed(&self) -> bool {
        matches!(self, Self::Replaced { salvaged: None, .. })
    }
}

/// Attaches the scheduler-record copy and the retrievability cache to a
/// newly opened collection connection, replacing a damaged one, and brings
/// the two copies of `review_scheduler` into step. `collection_path` is None
/// for a collection whose sidecars live in memory.
pub(crate) fn attach_sidecars(
    db: &Connection,
    collection_path: Option<&Path>,
) -> Result<SidecarRecovery> {
    attach_sidecars_with(db, collection_path, [None, None])
}

/// `known_damage` names damage found elsewhere (Check Database), per sidecar
/// in `SIDECARS` order; such a file is replaced without being checked again.
fn attach_sidecars_with(
    db: &Connection,
    collection_path: Option<&Path>,
    known_damage: [Option<String>; 2],
) -> Result<SidecarRecovery> {
    let [copy_damage, cache_damage] = known_damage;
    let copy = attach_one(db, SCHEDULER_RECORD_DB_SCHEMA, collection_path, copy_damage)?;
    let cache = attach_one(
        db,
        RETRIEVABILITY_CACHE_DB_SCHEMA,
        collection_path,
        cache_damage,
    )?;

    let mut salvaged = vec![];
    let mut moved_aside = vec![];
    for attached in [&copy, &cache] {
        if let Attached::Replaced {
            aside,
            salvaged: rows,
        } = attached
        {
            moved_aside.extend(aside.clone());
            salvaged.extend(rows.iter().flatten().cloned());
        }
    }
    bring_scheduler_records_into_step(db, &salvaged)?;

    let recovery = SidecarRecovery {
        cache_replaced: cache.replaced(),
        record_copy_replaced: copy.replaced(),
        records_lost: (cache.salvage_failed() && !copy.can_stand_in())
            || (copy.salvage_failed() && !cache.can_stand_in()),
        moved_aside,
    };
    if recovery.anything_replaced() {
        tracing::warn!(?recovery, "replaced a damaged sidecar database");
    }
    Ok(recovery)
}

/// Replaces the sidecars that Check Database found damaged, while the
/// collection stays open. Returns what was done; nothing when the
/// collection lives in memory.
pub(crate) fn replace_damaged_sidecars(
    db: &Connection,
    copy_damage: Option<String>,
    cache_damage: Option<String>,
) -> Result<SidecarRecovery> {
    let main: String = db.query_row(
        "select file from pragma_database_list where name = 'main'",
        [],
        |row| row.get(0),
    )?;
    if main.is_empty() || (copy_damage.is_none() && cache_damage.is_none()) {
        return Ok(SidecarRecovery::default());
    }
    // both, so they are attached again in the order that keeps the copy's
    // commit first
    for schema in SIDECARS {
        db.execute(&format!("DETACH DATABASE {schema}"), [])?;
    }
    attach_sidecars_with(db, Some(Path::new(&main)), [copy_damage, cache_damage])
}

fn attach_one(
    db: &Connection,
    schema: &str,
    collection_path: Option<&Path>,
    known_damage: Option<String>,
) -> Result<Attached> {
    let Some(collection_path) = collection_path else {
        attach(db, schema, ":memory:")?;
        return Ok(Attached::Memory);
    };
    let path = sidecar_path(schema, collection_path);
    let existed = path.exists();
    let damage = known_damage.or_else(|| file_damage(&path)).or_else(|| {
        match checked_attach(db, schema, &path) {
            Ok(()) => None,
            Err(err) => Some(err.to_string()),
        }
    });
    let Some(damage) = damage else {
        return Ok(Attached::Kept { existed });
    };
    tracing::warn!(file = ?path, damage, "sidecar database failed its check");
    // a failed check may have left it attached
    let _ = db.execute(&format!("DETACH DATABASE {schema}"), []);

    match move_aside(&path) {
        Ok(aside) => {
            let salvaged = salvage_scheduler_records(&aside);
            if checked_attach(db, schema, &path).is_ok() {
                return Ok(Attached::Replaced {
                    aside: Some(aside),
                    salvaged,
                });
            }
            let _ = db.execute(&format!("DETACH DATABASE {schema}"), []);
            tracing::warn!(file = ?path, "could not make a new sidecar; kept in memory");
            attach(db, schema, ":memory:")?;
            Ok(Attached::Replaced {
                aside: Some(aside),
                salvaged,
            })
        }
        Err(err) => {
            tracing::warn!(file = ?path, ?err, "could not move a damaged sidecar aside; kept in memory");
            let salvaged = salvage_scheduler_records(&path);
            attach(db, schema, ":memory:")?;
            Ok(Attached::Replaced {
                aside: None,
                salvaged,
            })
        }
    }
}

fn attach(db: &Connection, schema: &str, file: &str) -> Result<()> {
    db.execute(&format!("ATTACH DATABASE ? AS {schema}"), [file])?;
    db.pragma_update(Some(schema), "journal_mode", "wal")?;
    Ok(())
}

/// Attaches and reads the schema: a file that is not a database fails here.
fn checked_attach(db: &Connection, schema: &str, path: &Path) -> Result<()> {
    attach(db, schema, &path.to_string_lossy())?;
    db.query_row(
        &format!("select count(*) from {schema}.sqlite_master"),
        [],
        |row| row.get::<_, i64>(0),
    )?;
    Ok(())
}

/// The cheap check made before the file is attached: its header, and
/// whether it is shorter than the header says. It reads 100 bytes and the
/// file's length, so it costs nothing at start-up; damage inside a page is
/// left to Check Database's quick_check.
///
/// The length is judged only when no WAL holds pages: during a checkpoint
/// the header can already count pages that are still in the WAL.
pub(crate) fn file_damage(path: &Path) -> Option<String> {
    let len = match fs::metadata(path) {
        Ok(meta) => meta.len(),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return None,
        Err(err) => return Some(format!("cannot read: {err}")),
    };
    if len == 0 {
        // SQLite reads an empty file as an empty database
        return None;
    }
    let mut header = [0u8; 100];
    if let Err(err) = fs::File::open(path).and_then(|mut file| file.read_exact(&mut header)) {
        return Some(format!("no complete header: {err}"));
    }
    if &header[..16] != b"SQLite format 3\0" {
        return Some("not an SQLite database".into());
    }
    let page_size = match u16::from_be_bytes([header[16], header[17]]) {
        1 => 65_536,
        size => size as u64,
    };
    if page_size < 512 || !page_size.is_power_of_two() {
        return Some(format!("page size {page_size}"));
    }
    let wal_holds_pages = fs::metadata(with_suffix(path, "-wal")).is_ok_and(|meta| meta.len() > 0);
    // the page count is valid when the change counter matches the
    // version-valid-for number
    let pages = u32::from_be_bytes(header[28..32].try_into().unwrap()) as u64;
    if !wal_holds_pages && header[24..28] == header[92..96] && len < pages * page_size {
        return Some(format!(
            "{len} bytes, but the header counts {pages} pages of {page_size}"
        ));
    }
    None
}

fn with_suffix(path: &Path, suffix: &str) -> PathBuf {
    let mut name = path.as_os_str().to_owned();
    name.push(suffix);
    PathBuf::from(name)
}

/// `collection.retrievability-cache.sqlite` ->
/// `collection.retrievability-cache.damaged-2026-09-26-101500.sqlite`, with
/// its companion files. Never deletes anything.
fn move_aside(path: &Path) -> std::io::Result<PathBuf> {
    let stem = path
        .file_stem()
        .map(|stem| stem.to_string_lossy().into_owned())
        .unwrap_or_default();
    let extension = path
        .extension()
        .map(|ext| ext.to_string_lossy().into_owned())
        .unwrap_or_default();
    let stamp = chrono::Local::now().format("%Y-%m-%d-%H%M%S");
    let mut aside = path.with_file_name(format!("{stem}.damaged-{stamp}.{extension}"));
    let mut n = 2;
    while aside.exists() {
        aside = path.with_file_name(format!("{stem}.damaged-{stamp}-{n}.{extension}"));
        n += 1;
    }
    for suffix in COMPANION_SUFFIXES {
        let from = with_suffix(path, suffix);
        if from.exists() {
            let moved = fs::rename(&from, with_suffix(&aside, suffix));
            // a WAL or journal left behind would be read into the new file;
            // the shared-memory index is rebuilt, so it may stay
            if suffix != "-shm" {
                moved?;
            }
        }
    }
    if path.exists() {
        fs::rename(path, &aside)?;
    }
    Ok(aside)
}

/// The scheduler-record rows still readable from a damaged file: an empty
/// list when it holds no such table, None when it cannot be read. The file
/// is opened read-only, so the damaged copy stays as it was found.
fn salvage_scheduler_records(path: &Path) -> Option<Vec<SchedulerRecordRow>> {
    let read = |db: Connection| -> rusqlite::Result<Vec<SchedulerRecordRow>> {
        // a file shorter than its header says is otherwise refused as a
        // whole; this reads the pages that are still there
        db.pragma_update(None, "writable_schema", true)?;
        let exists: bool = db.query_row(
            "select exists(select 1 from sqlite_master
             where type = 'table' and name = ?1)",
            [REVIEW_SCHEDULER_TABLE],
            |row| row.get(0),
        )?;
        if !exists {
            return Ok(vec![]);
        }
        db.prepare(&format!(
            "select revlog_id, algorithm, recorded_at from {REVIEW_SCHEDULER_TABLE}"
        ))?
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))?
        .collect()
    };
    let read_only = OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let direct = Connection::open_with_flags(path, read_only).and_then(read);
    // a WAL database without its shared-memory file cannot be opened
    // read-only; immutable reads the main file alone
    let rows = direct.or_else(|_| {
        let uri = format!(
            "file:{}?immutable=1",
            path.to_string_lossy()
                .replace('\\', "/")
                .replace('%', "%25")
                .replace('?', "%3f")
                .replace('#', "%23")
        );
        Connection::open_with_flags(uri, read_only | OpenFlags::SQLITE_OPEN_URI).and_then(read)
    });
    match rows {
        Ok(rows) => Some(rows),
        Err(err) => {
            tracing::warn!(file = ?path, ?err, "nothing could be read from a damaged sidecar");
            None
        }
    }
}

fn create_scheduler_record_table(db: &Connection, schema: &str) -> rusqlite::Result<()> {
    db.execute_batch(&format!(
        "CREATE TABLE IF NOT EXISTS {schema}.{REVIEW_SCHEDULER_TABLE} (
            revlog_id INTEGER NOT NULL PRIMARY KEY,
            algorithm TEXT NOT NULL,
            recorded_at INTEGER NOT NULL
        );"
    ))
}

/// Makes both copies of `review_scheduler` hold the same rows, plus any rows
/// salvaged from a damaged file. A row a file already holds keeps its
/// value. The two files are compared by row count
/// and largest review id, which reads two index pages when they agree.
fn bring_scheduler_records_into_step(
    db: &Connection,
    salvaged: &[SchedulerRecordRow],
) -> Result<()> {
    let [copy, cache] = SIDECARS;
    create_scheduler_record_table(db, copy)?;
    create_scheduler_record_table(db, cache)?;
    let summary = |schema: &str| -> Result<(i64, Option<i64>)> {
        Ok(db.query_row(
            &format!("select count(*), max(revlog_id) from {schema}.{REVIEW_SCHEDULER_TABLE}"),
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )?)
    };
    if salvaged.is_empty() && summary(copy)? == summary(cache)? {
        return Ok(());
    }
    db.execute_batch("begin")?;
    let merged = (|| -> Result<()> {
        for schema in SIDECARS {
            let mut insert = db.prepare(&format!(
                "insert or ignore into {schema}.{REVIEW_SCHEDULER_TABLE}
                 (revlog_id, algorithm, recorded_at) values (?1, ?2, ?3)"
            ))?;
            for (revlog_id, algorithm, recorded_at) in salvaged {
                insert.execute(rusqlite::params![revlog_id, algorithm, recorded_at])?;
            }
        }
        for (to, from) in [(copy, cache), (cache, copy)] {
            db.execute(
                &format!(
                    "insert or ignore into {to}.{REVIEW_SCHEDULER_TABLE}
                     select revlog_id, algorithm, recorded_at
                     from {from}.{REVIEW_SCHEDULER_TABLE}"
                ),
                [],
            )?;
        }
        Ok(())
    })();
    match merged {
        Ok(()) => db.execute_batch("commit")?,
        Err(err) => {
            let _ = db.execute_batch("rollback");
            return Err(err);
        }
    }
    Ok(())
}

#[cfg(test)]
mod test {
    use std::fs;
    use std::path::Path;

    use tempfile::tempdir;
    use tempfile::TempDir;

    use super::*;
    use crate::collection::Collection;
    use crate::collection::CollectionBuilder;
    use crate::storage::RwkvReviewRetrievabilityCacheRow;
    use crate::storage::RwkvReviewRetrievabilitySampleRole;

    const RECORDED: [(i64, &str); 3] = [(1, "fsrs7"), (2, "rwkv_curve"), (3, "rwkv_instant")];

    fn open(path: &Path) -> Collection {
        CollectionBuilder::new(path)
            .with_desktop_media_paths()
            .build()
            .unwrap()
    }

    pub(crate) fn records(col: &Collection, schema: &str) -> Vec<(i64, String)> {
        col.storage
            .db
            .prepare(&format!(
                "select revlog_id, algorithm from {schema}.{REVIEW_SCHEDULER_TABLE}
                 order by revlog_id"
            ))
            .unwrap()
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
            .unwrap()
            .map(|row| row.unwrap())
            .collect()
    }

    fn recorded() -> Vec<(i64, String)> {
        RECORDED
            .iter()
            .map(|(id, algorithm)| (*id, algorithm.to_string()))
            .collect()
    }

    /// A closed collection on disk whose cache holds the three records and
    /// enough prediction rows to span many pages.
    fn collection_with_records() -> (TempDir, PathBuf) {
        let dir = tempdir().unwrap();
        let path = dir.path().join("collection.anki2");
        let col = open(&path);
        for (id, algorithm) in RECORDED {
            col.storage
                .set_review_scheduler(RevlogId(id), algorithm)
                .unwrap();
        }
        let rows: Vec<_> = (1..=20_000)
            .map(|id| RwkvReviewRetrievabilityCacheRow {
                revlog_id: RevlogId(id),
                prediction: 0.5,
                sample_role: RwkvReviewRetrievabilitySampleRole::FinalFit,
                fold_index: -1,
            })
            .collect();
        col.storage
            .set_rwkv_review_retrievability_predictions(&rows, "test")
            .unwrap();
        col.close(None).unwrap();
        (dir, path)
    }

    fn cache_file(path: &Path) -> PathBuf {
        retrievability_cache_path(path)
    }

    fn copy_file(path: &Path) -> PathBuf {
        scheduler_record_path(path)
    }

    fn garbage(file: &Path) {
        fs::write(file, "not a database, ".repeat(64)).unwrap();
    }

    fn truncate(file: &Path) {
        let len = fs::metadata(file).unwrap().len();
        fs::OpenOptions::new()
            .write(true)
            .open(file)
            .unwrap()
            .set_len(len / 2)
            .unwrap();
    }

    fn moved_aside(dir: &TempDir) -> Vec<String> {
        let mut names: Vec<_> = fs::read_dir(dir.path())
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
            .filter(|name| name.contains(".damaged-") && name.ends_with(".sqlite"))
            .collect();
        names.sort();
        names
    }

    fn reopen_and_take(path: &Path) -> (Collection, SidecarRecovery) {
        let mut col = open(path);
        let recovery = std::mem::take(&mut col.storage.sidecar_recovery);
        (col, recovery)
    }

    /// Pins spec/database.md#database.sidecar-recovery: a cache that is not
    /// a database is moved aside, the collection opens, and the record comes
    /// back from the copy.
    #[test]
    fn a_garbage_cache_is_replaced_and_keeps_the_record() {
        let (dir, path) = collection_with_records();
        garbage(&cache_file(&path));

        let (col, recovery) = reopen_and_take(&path);

        assert!(recovery.cache_replaced);
        assert!(!recovery.record_copy_replaced);
        assert!(!recovery.records_lost);
        assert_eq!(records(&col, RETRIEVABILITY_CACHE_DB_SCHEMA), recorded());
        assert_eq!(records(&col, SCHEDULER_RECORD_DB_SCHEMA), recorded());
        // the recomputable rows are gone, to be rebuilt by their passes
        let predictions: i64 = col
            .storage
            .db
            .query_row(
                "select count(*) from retrievability_cache.search_stats_rwkv_review_retrievability",
                [],
                |row| row.get(0),
            )
            .unwrap_or(0);
        assert_eq!(predictions, 0);
        let aside = moved_aside(&dir);
        assert_eq!(aside.len(), 1, "{aside:?}");
        assert!(aside[0].starts_with("collection.retrievability-cache.damaged-"));
        // the damaged file is kept as it was
        assert_eq!(
            fs::read(dir.path().join(&aside[0])).unwrap(),
            "not a database, ".repeat(64).into_bytes()
        );
        assert_eq!(recovery.moved_aside, vec![dir.path().join(&aside[0])]);
    }

    /// Pins spec/database.md#database.sidecar-recovery: a cache shorter
    /// than its header says is caught before it is attached.
    #[test]
    fn a_truncated_cache_is_replaced_and_keeps_the_record() {
        let (dir, path) = collection_with_records();
        assert!(file_damage(&cache_file(&path)).is_none());
        truncate(&cache_file(&path));
        assert!(file_damage(&cache_file(&path)).is_some());

        let (col, recovery) = reopen_and_take(&path);

        assert!(recovery.cache_replaced);
        assert!(!recovery.records_lost);
        assert_eq!(records(&col, RETRIEVABILITY_CACHE_DB_SCHEMA), recorded());
        assert_eq!(moved_aside(&dir).len(), 1);
    }

    /// Pins spec/database.md#database.sidecar-recovery: a missing cache is
    /// made again, and the record comes back from the copy.
    #[test]
    fn a_missing_cache_gets_the_record_back() {
        let (dir, path) = collection_with_records();
        fs::rename(cache_file(&path), dir.path().join("elsewhere.sqlite")).unwrap();

        let (col, recovery) = reopen_and_take(&path);

        assert_eq!(recovery, SidecarRecovery::default());
        assert_eq!(records(&col, RETRIEVABILITY_CACHE_DB_SCHEMA), recorded());
    }

    /// Pins spec/database.md#database.sidecar-recovery: a damaged copy is
    /// made again from the cache, and nothing is lost.
    #[test]
    fn a_damaged_copy_is_made_again_from_the_cache() {
        for truncated in [false, true] {
            let (dir, path) = collection_with_records();
            if truncated {
                // pad the copy, so that half of it cuts into its pages
                let col = open(&path);
                col.storage
                    .db
                    .execute_batch(
                        "create table scheduler_record.padding (x);
                         insert into scheduler_record.padding
                         select zeroblob(3000) from pragma_table_info('revlog');",
                    )
                    .unwrap();
                col.close(None).unwrap();
                truncate(&copy_file(&path));
            } else {
                garbage(&copy_file(&path));
            }

            let (col, recovery) = reopen_and_take(&path);

            assert!(recovery.record_copy_replaced, "truncated: {truncated}");
            assert!(!recovery.cache_replaced);
            assert!(!recovery.records_lost);
            assert_eq!(records(&col, SCHEDULER_RECORD_DB_SCHEMA), recorded());
            assert_eq!(moved_aside(&dir).len(), 1);
        }
    }

    /// Pins spec/database.md#database.sidecar-recovery: records are lost,
    /// and said to be, only when neither file can give them back.
    #[test]
    fn records_are_lost_only_when_neither_file_holds_them() {
        // both damaged
        let (dir, path) = collection_with_records();
        garbage(&cache_file(&path));
        garbage(&copy_file(&path));
        let (col, recovery) = reopen_and_take(&path);
        assert!(recovery.cache_replaced && recovery.record_copy_replaced);
        assert!(recovery.records_lost);
        assert!(records(&col, RETRIEVABILITY_CACHE_DB_SCHEMA).is_empty());
        assert_eq!(moved_aside(&dir).len(), 2);
        col.close(None).unwrap();

        // a damaged cache before any copy existed (the first open of this
        // version)
        let (_dir, path) = collection_with_records();
        fs::rename(copy_file(&path), path.with_extension("old")).unwrap();
        garbage(&cache_file(&path));
        let (_col, recovery) = reopen_and_take(&path);
        assert!(recovery.records_lost);

        // a truncated cache whose record pages survived gives them back
        let (_dir, path) = collection_with_records();
        fs::rename(copy_file(&path), path.with_extension("old")).unwrap();
        truncate(&cache_file(&path));
        let (col, recovery) = reopen_and_take(&path);
        assert!(recovery.cache_replaced);
        assert!(!recovery.records_lost);
        assert_eq!(records(&col, SCHEDULER_RECORD_DB_SCHEMA), recorded());
        assert_eq!(records(&col, RETRIEVABILITY_CACHE_DB_SCHEMA), recorded());
    }

    /// Pins spec/database.md#database.sidecar-recovery: the first open of a
    /// collection whose cache already holds records fills the new copy.
    #[test]
    fn a_new_copy_is_filled_from_the_cache() {
        let (_dir, path) = collection_with_records();
        fs::rename(copy_file(&path), path.with_extension("old")).unwrap();

        let (col, recovery) = reopen_and_take(&path);

        assert_eq!(recovery, SidecarRecovery::default());
        assert_eq!(records(&col, SCHEDULER_RECORD_DB_SCHEMA), recorded());
    }

    /// Pins spec/database.md#database.sidecar-recovery: a row one file
    /// lacks, as a crash between the two commits leaves it, is added at the
    /// next open; neither file loses a row.
    #[test]
    fn the_two_files_are_brought_into_step_at_open() {
        let (_dir, path) = collection_with_records();
        let col = open(&path);
        col.storage
            .db
            .execute_batch(
                "insert into scheduler_record.review_scheduler values (4, 'fsrs7', 0);
                 delete from retrievability_cache.review_scheduler where revlog_id = 1;",
            )
            .unwrap();
        col.close(None).unwrap();

        let (col, recovery) = reopen_and_take(&path);

        assert_eq!(recovery, SidecarRecovery::default());
        let mut expected = recorded();
        expected.push((4, "fsrs7".into()));
        assert_eq!(records(&col, SCHEDULER_RECORD_DB_SCHEMA), expected);
        assert_eq!(records(&col, RETRIEVABILITY_CACHE_DB_SCHEMA), expected);
    }

    /// Pins spec/database.md#database.sidecar-recovery: the copy is
    /// attached before the cache, which makes its commit the first.
    #[test]
    fn the_copy_is_attached_before_the_cache() {
        let (_dir, path) = collection_with_records();
        let col = open(&path);
        let order: Vec<String> = col
            .storage
            .db
            .prepare("select name from pragma_database_list order by seq")
            .unwrap()
            .query_map([], |row| row.get(0))
            .unwrap()
            .map(|name| name.unwrap())
            .filter(|name: &String| name != "temp")
            .collect();
        assert_eq!(
            order,
            [
                "main",
                SCHEDULER_RECORD_DB_SCHEMA,
                RETRIEVABILITY_CACHE_DB_SCHEMA
            ]
        );
    }

    /// Pins spec/database.md#database.sidecar-recovery: damage inside a
    /// page passes the open, and Check Database replaces the cache instead
    /// of calling the collection corrupt.
    #[test]
    fn check_database_replaces_a_damaged_cache_and_keeps_the_record() {
        let (dir, path) = collection_with_records();
        // spoil the last page, where the newest rows are
        let file = cache_file(&path);
        let mut bytes = fs::read(&file).unwrap();
        let len = bytes.len();
        bytes[len - 4096..].fill(0xAB);
        fs::write(&file, bytes).unwrap();

        let (mut col, recovery) = reopen_and_take(&path);
        assert_eq!(recovery, SidecarRecovery::default(), "the open passes");
        assert!(col
            .storage
            .quick_check_damage(RETRIEVABILITY_CACHE_DB_SCHEMA)
            .is_some());

        let out = col.check_database().unwrap();

        let problems = out.to_i18n_strings(&col.tr);
        assert_eq!(
            problems,
            vec![col.tr.database_check_sidecar_replaced().to_string()]
        );
        assert_eq!(records(&col, RETRIEVABILITY_CACHE_DB_SCHEMA), recorded());
        assert_eq!(records(&col, SCHEDULER_RECORD_DB_SCHEMA), recorded());
        assert!(col
            .storage
            .quick_check_damage(RETRIEVABILITY_CACHE_DB_SCHEMA)
            .is_none());
        assert_eq!(moved_aside(&dir).len(), 1);
        let recovery = std::mem::take(&mut col.storage.sidecar_recovery);
        assert!(recovery.cache_replaced && !recovery.records_lost);
        // both files take the next record
        col.storage
            .set_review_scheduler(RevlogId(9), "fsrs7")
            .unwrap();
        assert_eq!(records(&col, SCHEDULER_RECORD_DB_SCHEMA).len(), 4);
        assert_eq!(records(&col, RETRIEVABILITY_CACHE_DB_SCHEMA).len(), 4);
    }

    #[test]
    fn the_cheap_check_reads_the_header_and_the_length() {
        let (dir, path) = collection_with_records();
        let file = cache_file(&path);
        assert_eq!(file_damage(&file), None);
        assert_eq!(file_damage(&dir.path().join("absent.sqlite")), None);
        let empty = dir.path().join("empty.sqlite");
        fs::write(&empty, b"").unwrap();
        assert_eq!(file_damage(&empty), None);
        let short = dir.path().join("short.sqlite");
        fs::write(&short, b"SQLite format 3\0").unwrap();
        assert!(file_damage(&short).is_some());

        // a WAL that holds pages can make the file legitimately shorter
        truncate(&file);
        assert!(file_damage(&file).is_some());
        fs::write(with_suffix(&file, "-wal"), b"wal").unwrap();
        assert_eq!(file_damage(&file), None);
    }

    /// What the open pays for the checks and the copy, on a copy of a real
    /// profile: `SIDECAR_BENCH_COLLECTION=<path to collection.anki2> cargo
    /// test -p anki --release --lib bench_sidecar_attach -- --ignored
    /// --nocapture`. The earlier open attached the cache alone.
    #[test]
    #[ignore]
    fn bench_sidecar_attach() {
        let path = PathBuf::from(std::env::var("SIDECAR_BENCH_COLLECTION").unwrap());
        let (mut before, mut after) = (vec![], vec![]);
        for round in 0..240 {
            let db = Connection::open(&path).unwrap();
            let started = std::time::Instant::now();
            if round % 2 == 0 {
                attach(
                    &db,
                    RETRIEVABILITY_CACHE_DB_SCHEMA,
                    &retrievability_cache_path(&path).to_string_lossy(),
                )
                .unwrap();
                before.push(started.elapsed().as_secs_f64() * 1000.0);
            } else {
                let recovery = attach_sidecars(&db, Some(&path)).unwrap();
                after.push(started.elapsed().as_secs_f64() * 1000.0);
                assert!(!recovery.anything_replaced());
            }
        }
        let median = |values: &mut Vec<f64>| {
            values.sort_by(|a, b| a.partial_cmp(b).unwrap());
            values[values.len() / 2]
        };
        println!(
            "attach median: cache alone {:.3} ms, checks + copy + cache + step {:.3} ms",
            median(&mut before),
            median(&mut after)
        );
    }
}
