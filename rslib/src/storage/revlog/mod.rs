// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;
use std::collections::HashSet;
use std::convert::TryFrom;

use fnv::FnvHashMap;
use rusqlite::params;
use rusqlite::types::FromSql;
use rusqlite::types::FromSqlError;
use rusqlite::types::ValueRef;
use rusqlite::OptionalExtension;
use rusqlite::Row;

use super::ids_to_string;
use super::sqlite::RETRIEVABILITY_CACHE_DB_SCHEMA;
use super::write_comma_separated_ids;
use super::SqliteStorage;
use crate::config::ConfigEntry;
use crate::error::Result;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::revlog::RevlogReviewKind;

pub(crate) const FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE: &str =
    "search_stats_fsrs_review_retrievability";
/// How many review ids one delete statement names.
const DELETE_REVIEW_PREDICTIONS_CHUNK: usize = 5_000;
/// Every algorithm's per-review predictions, addressed BY ALGORITHM, so a
/// query cannot reach a row without saying whose it is (spec
/// ui.stats-model-metrics). FSRS-7 and RWKV-Instant still have tables of
/// their own; moving them here is a separate, bit-identical refactor.
pub(crate) const REVIEW_PREDICTIONS_TABLE: &str = "review_predictions";
pub(crate) const RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE: &str =
    "search_stats_rwkv_review_retrievability";
/// Which algorithm scheduled a review, one row per review, written when the
/// review is answered (spec sched.review-scheduler-record).
pub(crate) const REVIEW_SCHEDULER_TABLE: &str = "review_scheduler";
/// RWKV-Curve's per-review curve sources (spec ui.card-info-rwkv-curve), one
/// row per review, and the tags that say which model wrote them.
const RWKV_CURVE_SOURCES_TABLE: &str = "rwkv_curve_sources";
const RWKV_CURVE_SOURCE_TAGS_TABLE: &str = "rwkv_curve_source_tags";
/// Lets the check that the recordings are all still there count a tag's
/// sources without reading them: every row of the table holds a 256-byte
/// source, so a count over the table read ~170 MB, and over this index 28 ms
/// against 149 ms warm (656,445 sources). Built once, on the first write or
/// read of the sources after an update.
const RWKV_CURVE_SOURCES_TAG_INDEX: &str = "ix_rwkv_curve_sources_tag";
const REVIEW_RETRIEVABILITY_CACHE_CLEANUP_FULL_SYNC_MARKER: &str =
    "reviewRetrievabilityCacheCleanupFullSync";
const REVIEW_RETRIEVABILITY_CACHE_WRITE_SAVEPOINT: &str = "review_retrievability_cache_write";
const FSRS_REVIEW_RETRIEVABILITY_SAMPLE_ROLES: &str =
    "'final_fit', 'validation_fold', 'post_optimization'";
const RWKV_REVIEW_RETRIEVABILITY_SAMPLE_ROLES: &str =
    "'final_fit', 'test_fold', 'validation_fold', 'post_optimization'";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum FsrsReviewRetrievabilitySampleRole {
    FinalFit,
    ValidationFold,
    PostOptimization,
}

impl FsrsReviewRetrievabilitySampleRole {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::FinalFit => "final_fit",
            Self::ValidationFold => "validation_fold",
            Self::PostOptimization => "post_optimization",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct FsrsReviewRetrievabilityCacheRow {
    pub revlog_id: RevlogId,
    pub prediction: f32,
    pub sample_role: FsrsReviewRetrievabilitySampleRole,
    pub fold_index: i32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RwkvReviewRetrievabilitySampleRole {
    FinalFit,
    TestFold,
    PostOptimization,
}

impl RwkvReviewRetrievabilitySampleRole {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::FinalFit => "final_fit",
            Self::TestFold => "test_fold",
            Self::PostOptimization => "post_optimization",
        }
    }

    pub(crate) fn from_str(value: &str) -> Option<Self> {
        match value {
            "" | "final_fit" => Some(Self::FinalFit),
            "test_fold" | "validation_fold" => Some(Self::TestFold),
            "post_optimization" => Some(Self::PostOptimization),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct RwkvReviewRetrievabilityCacheRow {
    pub revlog_id: RevlogId,
    pub prediction: f32,
    pub sample_role: RwkvReviewRetrievabilitySampleRole,
    pub fold_index: i32,
}

/// One row of the generic prediction table, for one algorithm.
#[derive(Debug, Clone, Copy)]
pub(crate) struct ReviewPredictionRow {
    pub revlog_id: RevlogId,
    pub prediction: f32,
    pub sample_role: &'static str,
    pub fold_index: i32,
}

/// Who saved an RWKV curve source: the model's identity and the source's
/// format and kernel versions (spec ui.card-info-rwkv-curve).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct RwkvCurveSourceTag {
    pub model: String,
    pub format: u32,
    pub kernel: u32,
}

pub(crate) struct StudiedToday {
    pub cards: u32,
    pub seconds: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct RwkvHistoricalReviewRow {
    pub(crate) review_id: i64,
    pub(crate) card_id: i64,
    pub(crate) note_id: i64,
    pub(crate) deck_id: i64,
    pub(crate) ease: i64,
    pub(crate) duration_millis: i64,
    pub(crate) review_kind: i64,
    pub(crate) interval_days: i64,
    pub(crate) ease_factor: i64,
    pub(crate) is_learning_start: bool,
}

/// How far back one card's replay history reaches. Built while the review log
/// is read in id order, so that the start row costs no extra pass.
#[derive(Default)]
struct RwkvHistoricalReviewStart {
    /// The kind of the card's previous rated row, to find the row that starts
    /// a run of learning rows.
    previous_rated_kind: Option<i64>,
    /// The latest row that starts a run of learning rows.
    learning_start: Option<i64>,
    /// The card's first rated row after its last Forget row.
    first_rated_after_forget: Option<i64>,
}

impl RwkvHistoricalReviewStart {
    fn id(&self) -> Option<i64> {
        self.learning_start.or(self.first_rated_after_forget)
    }
}

/// A read of the RWKV replay's rows in progress; see
/// `SqliteStorage::rwkv_historical_review_reader`.
pub(crate) struct RwkvHistoricalReviewReader {
    ignored: Vec<i64>,
    active_ignored_review_ids: Vec<i64>,
    cards: FnvHashMap<i64, (i64, i64)>,
    rows: Vec<RwkvHistoricalReviewRow>,
    starts: FnvHashMap<i64, RwkvHistoricalReviewStart>,
    /// The last card id read, while the cards are read; None after them.
    after_card_id: Option<i64>,
    /// The last review-log id read; the next part starts after it.
    after_review_id: i64,
}

impl RwkvHistoricalReviewReader {
    /// The rows and the active ignored reviews, once every part is read. A
    /// card's rows before its start row are dropped only now: a later Forget
    /// or learning run moves the start.
    pub(crate) fn finish(self) -> (Vec<RwkvHistoricalReviewRow>, Vec<i64>) {
        let Self {
            mut rows,
            starts,
            active_ignored_review_ids,
            ..
        } = self;
        rows.retain_mut(|row| {
            let Some(start_id) = starts
                .get(&row.card_id)
                .and_then(RwkvHistoricalReviewStart::id)
            else {
                return false;
            };
            row.is_learning_start = row.review_id == start_id;
            row.review_id >= start_id
        });
        (rows, active_ignored_review_ids)
    }
}

impl FromSql for RevlogReviewKind {
    fn column_result(value: ValueRef<'_>) -> std::result::Result<Self, FromSqlError> {
        if let ValueRef::Integer(i) = value {
            Ok(Self::try_from(i as u8).map_err(|_| FromSqlError::InvalidType)?)
        } else {
            Err(FromSqlError::InvalidType)
        }
    }
}

/// One rating of the search, as the model-quality graphs read it.
pub(crate) struct SearchedRating {
    pub id: RevlogId,
    pub cid: CardId,
    pub button_chosen: u8,
}

/// `roles` as an SQL list of string literals. The roles are the algorithms'
/// own constant names, never user input; a quote in one is doubled anyway.
fn role_list(roles: &[&str]) -> String {
    roles
        .iter()
        .map(|role| format!("'{}'", role.replace('\'', "''")))
        .collect::<Vec<_>>()
        .join(", ")
}

/// One prediction per review, from rows that arrive in review order.
///
/// This is the row `row_number() over (partition by revlog_id order by
/// updated_at desc, fold_index desc, source)` numbered 1, picked while the
/// rows stream instead of by sorting them: the ordering index already
/// groups a review's rows together, so the sorter the window function
/// needed was the whole extra cost. Two rows of one review differ in
/// `fold_index` or in `source`, because both are part of the key, so the
/// order is total and the winner is the same row either way.
fn newest_prediction_of_each_review(mut rows: rusqlite::Rows<'_>) -> Result<Vec<(RevlogId, f32)>> {
    let mut newest: Vec<(RevlogId, f32)> = vec![];
    // the winning row's ordering columns; `best_source` is one buffer that
    // is written over, so a review with a single row allocates nothing
    let (mut best_updated_at, mut best_fold_index) = (0i64, 0i64);
    let mut best_source = String::new();
    while let Some(row) = rows.next()? {
        let review: RevlogId = row.get(0)?;
        let prediction: f32 = row.get(1)?;
        let updated_at: i64 = row.get(2)?;
        let fold_index: i64 = row.get(3)?;
        let source = row.get_ref(4)?.as_str().map_err(|error| {
            rusqlite::Error::FromSqlConversionFailure(4, rusqlite::types::Type::Text, error.into())
        })?;
        if newest.last().is_some_and(|(last, _)| *last == review) {
            // a later row of the review being read wins only if it sorts
            // before the winner so far: newer first, then the higher fold,
            // then the lower source
            let (key, best) = ((updated_at, fold_index), (best_updated_at, best_fold_index));
            if key < best || (key == best && source >= best_source.as_str()) {
                continue;
            }
            newest.last_mut().expect("just read").1 = prediction;
        } else {
            newest.push((review, prediction));
        }
        (best_updated_at, best_fold_index) = (updated_at, fold_index);
        best_source.clear();
        best_source.push_str(source);
    }
    Ok(newest)
}

fn row_to_revlog_entry(row: &Row) -> Result<RevlogEntry> {
    Ok(RevlogEntry {
        id: row.get(0)?,
        cid: row.get(1)?,
        usn: row.get(2)?,
        button_chosen: row.get(3)?,
        interval: row.get(4)?,
        last_interval: row.get(5)?,
        ease_factor: row.get(6)?,
        taken_millis: row.get(7).unwrap_or_default(),
        review_kind: row.get(8).unwrap_or_default(),
    })
}

/// One part of the uncovered-review count: each deck with its uncovered
/// reviews, and the review id the next part starts after (None at the end).
pub(crate) type UncoveredReviewsPart = (Vec<(DeckId, u32)>, Option<i64>);

impl SqliteStorage {
    fn qualified_retrievability_cache_table(table: &str) -> String {
        format!("{RETRIEVABILITY_CACHE_DB_SCHEMA}.{table}")
    }

    fn table_columns(&self, schema: &str, table: &str) -> Result<Vec<String>> {
        self.db
            .prepare(&format!("PRAGMA {schema}.table_info({table})"))?
            .query_map([], |row| row.get::<_, String>(1))?
            .collect::<std::result::Result<Vec<_>, _>>()
            .map_err(Into::into)
    }

    fn main_table_exists(&self, table: &str) -> Result<bool> {
        self.db
            .prepare("SELECT null FROM main.sqlite_master WHERE type = 'table' AND name = ?")?
            .exists([table])
            .map_err(Into::into)
    }

    fn migrate_legacy_retrievability_cache_table(
        &self,
        table: &str,
        valid_sample_roles: &str,
    ) -> Result<bool> {
        if !self.main_table_exists(table)? {
            return Ok(false);
        }

        let columns = self.table_columns("main", table)?;
        let has_required_columns = ["revlog_id", "prediction", "source", "updated_at"]
            .iter()
            .all(|required| columns.iter().any(|column| column == required));
        if has_required_columns {
            let has_sample_role = columns.iter().any(|column| column == "sample_role");
            let has_fold_index = columns.iter().any(|column| column == "fold_index");
            let sample_role = if has_sample_role {
                "sample_role"
            } else {
                "'final_fit'"
            };
            let fold_index = if has_fold_index { "fold_index" } else { "-1" };
            let sample_role_filter = if has_sample_role {
                format!("AND sample_role IN ({valid_sample_roles})")
            } else {
                String::new()
            };
            let target = Self::qualified_retrievability_cache_table(table);
            self.db.execute_batch(&format!(
                "
                INSERT OR REPLACE INTO {target}
                    (revlog_id, prediction, source, updated_at, sample_role, fold_index)
                SELECT revlog_id, prediction, source, updated_at, {sample_role}, {fold_index}
                FROM main.{table}
                WHERE revlog_id > 0
                  AND prediction BETWEEN 0 AND 1
                  AND source IS NOT NULL
                  AND updated_at IS NOT NULL
                  {sample_role_filter};
                "
            ))?;
        }

        self.db
            .execute_batch(&format!("DROP TABLE IF EXISTS main.{table};"))?;
        Ok(true)
    }

    pub(crate) fn migrate_review_retrievability_cache_to_sidecar(&self) -> Result<usize> {
        self.ensure_fsrs_review_retrievability_cache_schema()?;
        let mut dropped = 0;
        if self.migrate_legacy_retrievability_cache_table(
            FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE,
            FSRS_REVIEW_RETRIEVABILITY_SAMPLE_ROLES,
        )? {
            dropped += 1;
        }
        self.ensure_rwkv_review_retrievability_cache_schema()?;
        if self.migrate_legacy_retrievability_cache_table(
            RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE,
            RWKV_REVIEW_RETRIEVABILITY_SAMPLE_ROLES,
        )? {
            dropped += 1;
        }
        Ok(dropped)
    }

    pub(crate) fn review_retrievability_cache_cleanup_full_sync_marked(&self) -> Result<bool> {
        Ok(self
            .get_config_value::<bool>(REVIEW_RETRIEVABILITY_CACHE_CLEANUP_FULL_SYNC_MARKER)?
            .unwrap_or(false))
    }

    pub(crate) fn mark_review_retrievability_cache_cleanup_full_sync(&self) -> Result<()> {
        self.set_config_entry(&ConfigEntry::boxed(
            REVIEW_RETRIEVABILITY_CACHE_CLEANUP_FULL_SYNC_MARKER,
            serde_json::to_vec(&true)?,
            Usn(0),
            TimestampSecs::now(),
        ))
    }

    fn with_retrievability_cache_write_batch(
        &self,
        op: impl FnOnce() -> Result<usize>,
    ) -> Result<usize> {
        let started_savepoint = self.db.is_autocommit();
        if started_savepoint {
            self.db.execute_batch(&format!(
                "SAVEPOINT {REVIEW_RETRIEVABILITY_CACHE_WRITE_SAVEPOINT};"
            ))?;
        }

        let result = op();
        if !started_savepoint {
            return result;
        }

        match result {
            Ok(stored) => {
                self.db.execute_batch(&format!(
                    "RELEASE {REVIEW_RETRIEVABILITY_CACHE_WRITE_SAVEPOINT};"
                ))?;
                Ok(stored)
            }
            Err(err) => {
                if let Err(rollback_err) = self.db.execute_batch(&format!(
                    "ROLLBACK TO {REVIEW_RETRIEVABILITY_CACHE_WRITE_SAVEPOINT};
                     RELEASE {REVIEW_RETRIEVABILITY_CACHE_WRITE_SAVEPOINT};"
                )) {
                    tracing::warn!(
                        ?rollback_err,
                        "failed to roll back retrievability cache write batch"
                    );
                }
                Err(err)
            }
        }
    }

    fn ensure_fsrs_review_retrievability_cache_schema(&self) -> Result<()> {
        let table_info = self.table_columns(
            RETRIEVABILITY_CACHE_DB_SCHEMA,
            FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE,
        )?;
        let has_sample_role = table_info.iter().any(|col| col == "sample_role");
        let has_fold_index = table_info.iter().any(|col| col == "fold_index");
        if !table_info.is_empty() && (!has_sample_role || !has_fold_index) {
            let table =
                Self::qualified_retrievability_cache_table(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE);
            self.db
                .execute_batch(&format!("DROP TABLE IF EXISTS {table};"))?;
        }

        let table =
            Self::qualified_retrievability_cache_table(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE);
        self.db.execute_batch(&format!(
            "
            CREATE TABLE IF NOT EXISTS {table} (
                revlog_id INTEGER NOT NULL,
                prediction REAL NOT NULL CHECK(prediction >= 0 AND prediction <= 1),
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                sample_role TEXT NOT NULL DEFAULT 'final_fit'
                    CHECK(sample_role IN ('final_fit', 'validation_fold', 'post_optimization')),
                fold_index INTEGER NOT NULL DEFAULT -1,
                PRIMARY KEY (revlog_id, sample_role, fold_index, source)
            );
            CREATE INDEX IF NOT EXISTS {RETRIEVABILITY_CACHE_DB_SCHEMA}.ix_fsrs_review_retrievability_covering
                ON {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                (sample_role, revlog_id, prediction, updated_at, fold_index, source);
            DROP INDEX IF EXISTS {RETRIEVABILITY_CACHE_DB_SCHEMA}.ix_fsrs_review_retrievability_role_revlog;
            "
        ))?;
        Ok(())
    }

    pub(crate) fn set_fsrs_review_retrievability_predictions(
        &self,
        rows: &[FsrsReviewRetrievabilityCacheRow],
        source: &str,
    ) -> Result<usize> {
        if rows.is_empty() {
            return Ok(0);
        }

        self.ensure_fsrs_review_retrievability_cache_schema()?;

        self.with_retrievability_cache_write_batch(|| {
            let updated_at = TimestampMillis::now().0;
            let mut stored = 0;
            let table =
                Self::qualified_retrievability_cache_table(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE);
            let mut stmt = self.db.prepare_cached(&format!(
                "
                INSERT INTO {table}
                    (revlog_id, prediction, source, updated_at, sample_role, fold_index)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(revlog_id, sample_role, fold_index, source) DO UPDATE SET
                    prediction = excluded.prediction,
                    updated_at = excluded.updated_at
                WHERE {table}.prediction IS NOT excluded.prediction
                "
            ))?;

            for row in rows {
                if row.revlog_id.0 > 0
                    && row.prediction.is_finite()
                    && (0.0..=1.0).contains(&row.prediction)
                {
                    stmt.execute(params![
                        row.revlog_id,
                        row.prediction,
                        source,
                        updated_at,
                        row.sample_role.as_str(),
                        row.fold_index
                    ])?;
                    stored += 1;
                }
            }

            Ok(stored)
        })
    }

    fn ensure_review_scheduler_schema(&self) -> Result<()> {
        let table = Self::qualified_retrievability_cache_table(REVIEW_SCHEDULER_TABLE);
        self.db.execute_batch(&format!(
            "
            CREATE TABLE IF NOT EXISTS {table} (
                revlog_id INTEGER NOT NULL PRIMARY KEY,
                algorithm TEXT NOT NULL,
                recorded_at INTEGER NOT NULL
            );
            "
        ))?;
        Ok(())
    }

    /// Records which algorithm scheduled the review, once, at the moment it
    /// is answered (spec sched.review-scheduler-record). A review already
    /// recorded keeps its first answer, because the algorithm that scheduled
    /// it cannot change afterwards.
    pub(crate) fn set_review_scheduler(&self, revlog_id: RevlogId, algorithm: &str) -> Result<()> {
        self.ensure_review_scheduler_schema()?;
        let table = Self::qualified_retrievability_cache_table(REVIEW_SCHEDULER_TABLE);
        self.db
            .prepare_cached(&format!(
                "insert or ignore into {table} (revlog_id, algorithm, recorded_at)
                 values (?1, ?2, ?3)"
            ))?
            .execute(params![revlog_id.0, algorithm, TimestampSecs::now().0])?;
        Ok(())
    }

    fn ensure_rwkv_review_retrievability_cache_schema(&self) -> Result<()> {
        let table_info = self.table_columns(
            RETRIEVABILITY_CACHE_DB_SCHEMA,
            RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE,
        )?;
        let required_columns = ["revlog_id", "prediction", "source", "updated_at"];
        let has_sample_role = table_info.iter().any(|col| col == "sample_role");
        let has_fold_index = table_info.iter().any(|col| col == "fold_index");
        if !table_info.is_empty()
            && (!required_columns
                .iter()
                .all(|required| table_info.iter().any(|column| column == required))
                || !has_sample_role
                || !has_fold_index)
        {
            let table =
                Self::qualified_retrievability_cache_table(RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE);
            self.db
                .execute_batch(&format!("DROP TABLE IF EXISTS {table};"))?;
        }

        let table =
            Self::qualified_retrievability_cache_table(RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE);
        self.db.execute_batch(&format!(
            "
            CREATE TABLE IF NOT EXISTS {table} (
                revlog_id INTEGER NOT NULL,
                prediction REAL NOT NULL CHECK(prediction >= 0 AND prediction <= 1),
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                sample_role TEXT NOT NULL DEFAULT 'final_fit'
                    CHECK(sample_role IN ('final_fit', 'test_fold', 'validation_fold', 'post_optimization')),
                fold_index INTEGER NOT NULL DEFAULT -1,
                PRIMARY KEY (revlog_id, sample_role, fold_index, source)
            );
            CREATE INDEX IF NOT EXISTS {RETRIEVABILITY_CACHE_DB_SCHEMA}.ix_rwkv_review_retrievability_covering
                ON {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                (sample_role, revlog_id, prediction, updated_at, fold_index, source);
            DROP INDEX IF EXISTS {RETRIEVABILITY_CACHE_DB_SCHEMA}.ix_rwkv_review_retrievability_role_revlog;
            "
        ))?;
        Ok(())
    }

    pub(crate) fn set_rwkv_review_retrievability_predictions(
        &self,
        rows: &[RwkvReviewRetrievabilityCacheRow],
        source: &str,
    ) -> Result<usize> {
        if rows.is_empty() {
            return Ok(0);
        }

        self.ensure_rwkv_review_retrievability_cache_schema()?;

        self.with_retrievability_cache_write_batch(|| {
            let updated_at = TimestampMillis::now().0;
            let mut stored = 0;
            let table =
                Self::qualified_retrievability_cache_table(RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE);
            let mut stmt = self.db.prepare_cached(&format!(
                "
                INSERT INTO {table}
                    (revlog_id, prediction, source, updated_at, sample_role, fold_index)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(revlog_id, sample_role, fold_index, source) DO UPDATE SET
                    prediction = excluded.prediction,
                    updated_at = excluded.updated_at
                WHERE {table}.prediction IS NOT excluded.prediction
                "
            ))?;
            for row in rows {
                if row.revlog_id.0 > 0
                    && row.prediction.is_finite()
                    && (0.0..=1.0).contains(&row.prediction)
                {
                    stmt.execute(params![
                        row.revlog_id,
                        row.prediction,
                        source,
                        updated_at,
                        row.sample_role.as_str(),
                        row.fold_index,
                    ])?;
                    stored += 1;
                }
            }
            Ok(stored)
        })
    }

    /// The generic prediction table. A collection that has never recorded
    /// an algorithm's predictions gets it empty: the algorithm's series
    /// then says it has no rows yet, and whatever produces them fills it.
    /// Nothing has to be deleted by hand.
    ///
    /// `sample_role` carries no CHECK here. Which roles are legitimate is a
    /// fact about one algorithm, and it belongs to that algorithm's
    /// contract, not to a table every algorithm shares.
    fn ensure_review_predictions_schema(&self) -> Result<()> {
        let table = Self::qualified_retrievability_cache_table(REVIEW_PREDICTIONS_TABLE);
        self.db.execute_batch(&format!(
            "
            CREATE TABLE IF NOT EXISTS {table} (
                algorithm INTEGER NOT NULL,
                revlog_id INTEGER NOT NULL,
                prediction REAL NOT NULL CHECK(prediction >= 0 AND prediction <= 1),
                sample_role TEXT NOT NULL DEFAULT 'final_fit',
                fold_index INTEGER NOT NULL DEFAULT -1,
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (algorithm, revlog_id, sample_role, fold_index, source)
            );
            CREATE INDEX IF NOT EXISTS {RETRIEVABILITY_CACHE_DB_SCHEMA}.ix_review_predictions_algorithm_role_revlog
                ON {REVIEW_PREDICTIONS_TABLE} (algorithm, sample_role, revlog_id);
            "
        ))?;
        Ok(())
    }

    /// Stores one algorithm's predictions. The key holds the role, the fold
    /// and the source as well as the algorithm and the review, so two roles
    /// of the same review coexist: 362 of Andrew's reviews hold both a
    /// `final_fit` row and a `post_optimization` row, and the role counts
    /// and the staleness query both depend on that.
    pub(crate) fn set_review_predictions(
        &self,
        algorithm: i32,
        rows: &[ReviewPredictionRow],
        source: &str,
    ) -> Result<usize> {
        if rows.is_empty() {
            return Ok(0);
        }
        self.ensure_review_predictions_schema()?;
        self.with_retrievability_cache_write_batch(|| {
            let updated_at = TimestampMillis::now().0;
            let mut stored = 0;
            let table = Self::qualified_retrievability_cache_table(REVIEW_PREDICTIONS_TABLE);
            let mut stmt = self.db.prepare_cached(&format!(
                "
                INSERT INTO {table}
                    (algorithm, revlog_id, prediction, sample_role, fold_index, source, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(algorithm, revlog_id, sample_role, fold_index, source) DO UPDATE SET
                    prediction = excluded.prediction,
                    updated_at = excluded.updated_at
                WHERE {table}.prediction IS NOT excluded.prediction
                "
            ))?;
            for row in rows {
                if row.revlog_id.0 > 0
                    && row.prediction.is_finite()
                    && (0.0..=1.0).contains(&row.prediction)
                {
                    stmt.execute(params![
                        algorithm,
                        row.revlog_id,
                        row.prediction,
                        row.sample_role,
                        row.fold_index,
                        source,
                        updated_at
                    ])?;
                    stored += 1;
                }
            }
            Ok(stored)
        })
    }

    /// Whether ONE algorithm and ONE role have a row at all.
    ///
    /// An algorithm that takes the first role it has rows for asks only
    /// this. Counting the rows of every role instead reads the whole index
    /// of a table that holds a row per review.
    pub(crate) fn review_prediction_role_exists(&self, algorithm: i32, role: &str) -> Result<bool> {
        self.ensure_review_predictions_schema()?;
        let table = Self::qualified_retrievability_cache_table(REVIEW_PREDICTIONS_TABLE);
        self.db
            .prepare_cached(&format!(
                "select exists(select 1 from {table} where algorithm = ?1 and sample_role = ?2)"
            ))?
            .query_row((algorithm, role), |row| row.get(0))
            .map_err(Into::into)
    }

    /// One algorithm's newest prediction of each review, from one role, by
    /// ascending review id.
    pub(crate) fn review_predictions_of(
        &self,
        algorithm: i32,
        sample_role: &str,
        after: TimestampMillis,
    ) -> Result<Vec<(RevlogId, f32)>> {
        self.ensure_review_predictions_schema()?;
        let table = Self::qualified_retrievability_cache_table(REVIEW_PREDICTIONS_TABLE);
        let mut statement = self.db.prepare_cached(&format!(
            "select revlog_id, prediction, updated_at, fold_index, source
             from {table}
             where algorithm = ?1 and sample_role = ?2 and revlog_id > ?3
             order by revlog_id"
        ))?;
        let rows = statement.query((algorithm, sample_role, after.0))?;
        newest_prediction_of_each_review(rows)
    }

    /// One algorithm's newest prediction of each review across `roles`, by
    /// ascending review id: the row written last wins, whatever its role.
    pub(crate) fn review_predictions_newest_of(
        &self,
        algorithm: i32,
        roles: &[&str],
        after: TimestampMillis,
    ) -> Result<Vec<(RevlogId, f32)>> {
        self.ensure_review_predictions_schema()?;
        let table = Self::qualified_retrievability_cache_table(REVIEW_PREDICTIONS_TABLE);
        let mut statement = self.db.prepare(&format!(
            "select revlog_id, prediction, updated_at, fold_index, source
             from {table}
             where algorithm = ?1 and revlog_id > ?2 and sample_role in ({})
             order by revlog_id",
            role_list(roles)
        ))?;
        let rows = statement.query((algorithm, after.0))?;
        newest_prediction_of_each_review(rows)
    }

    /// The curve-source tables live in the cache file beside the
    /// collection, like the other per-review recordings, so official Anki,
    /// AnkiDroid and sync never see them.
    fn ensure_rwkv_curve_sources_schema(&self) -> Result<()> {
        let tags = Self::qualified_retrievability_cache_table(RWKV_CURVE_SOURCE_TAGS_TABLE);
        let sources = Self::qualified_retrievability_cache_table(RWKV_CURVE_SOURCES_TABLE);
        self.db.execute_batch(&format!(
            "
            CREATE TABLE IF NOT EXISTS {tags} (
                id INTEGER PRIMARY KEY,
                model TEXT NOT NULL,
                format INTEGER NOT NULL,
                kernel INTEGER NOT NULL,
                UNIQUE (model, format, kernel)
            );
            CREATE TABLE IF NOT EXISTS {sources} (
                revlog_id INTEGER PRIMARY KEY,
                tag INTEGER NOT NULL,
                source BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS {RETRIEVABILITY_CACHE_DB_SCHEMA}.{RWKV_CURVE_SOURCES_TAG_INDEX}
                ON {RWKV_CURVE_SOURCES_TABLE} (tag, revlog_id);
            "
        ))?;
        Ok(())
    }

    fn rwkv_curve_source_tag_id(&self, tag: &RwkvCurveSourceTag) -> Result<Option<i64>> {
        let tags = Self::qualified_retrievability_cache_table(RWKV_CURVE_SOURCE_TAGS_TABLE);
        Ok(self
            .db
            .prepare_cached(&format!(
                "select id from {tags} where model = ? and format = ? and kernel = ?"
            ))?
            .query_row(params![tag.model, tag.format, tag.kernel], |row| row.get(0))
            .optional()?)
    }

    /// Saves one curve source per review, `width` bytes each, in the order
    /// of `revlog_ids`. Sources saved under any other tag are stale for
    /// every reader, so they are dropped first rather than kept beside the
    /// new ones.
    pub(crate) fn set_rwkv_curve_sources(
        &self,
        tag: &RwkvCurveSourceTag,
        revlog_ids: &[i64],
        sources: &[u8],
        width: usize,
    ) -> Result<usize> {
        if revlog_ids.is_empty() {
            return Ok(0);
        }
        self.ensure_rwkv_curve_sources_schema()?;
        let tags = Self::qualified_retrievability_cache_table(RWKV_CURVE_SOURCE_TAGS_TABLE);
        let table = Self::qualified_retrievability_cache_table(RWKV_CURVE_SOURCES_TABLE);
        self.with_retrievability_cache_write_batch(|| {
            let tag_id = match self.rwkv_curve_source_tag_id(tag)? {
                Some(id) => id,
                None => {
                    self.db.execute(
                        &format!("insert into {tags} (model, format, kernel) values (?, ?, ?)"),
                        params![tag.model, tag.format, tag.kernel],
                    )?;
                    self.db.last_insert_rowid()
                }
            };
            let other_tags: bool = self.db.query_row(
                &format!("select exists(select 1 from {tags} where id != ?)"),
                [tag_id],
                |row| row.get(0),
            )?;
            if other_tags {
                self.db
                    .execute(&format!("delete from {table} where tag != ?"), [tag_id])?;
                self.db
                    .execute(&format!("delete from {tags} where id != ?"), [tag_id])?;
            }
            let mut stmt = self.db.prepare_cached(&format!(
                "insert or replace into {table} (revlog_id, tag, source) values (?, ?, ?)"
            ))?;
            for (revlog_id, source) in revlog_ids.iter().zip(sources.chunks_exact(width)) {
                stmt.execute(params![revlog_id, tag_id, source])?;
            }
            Ok(revlog_ids.len())
        })
    }

    /// The sources saved under `tag` for the reviews of one card, oldest
    /// first: (review ids, bytes per source, the sources). A source of
    /// another length than the first is left out.
    pub(crate) fn rwkv_curve_sources_for_card(
        &self,
        card_id: CardId,
        tag: &RwkvCurveSourceTag,
    ) -> Result<(Vec<i64>, usize, Vec<u8>)> {
        self.ensure_rwkv_curve_sources_schema()?;
        let Some(tag_id) = self.rwkv_curve_source_tag_id(tag)? else {
            return Ok((vec![], 0, vec![]));
        };
        let table = Self::qualified_retrievability_cache_table(RWKV_CURVE_SOURCES_TABLE);
        let mut ids = vec![];
        let mut width = 0;
        let mut bytes = vec![];
        let mut stmt = self.db.prepare_cached(&format!(
            "select s.revlog_id, s.source from main.revlog r
             join {table} s on s.revlog_id = r.id
             where r.cid = ? and s.tag = ?
             order by r.id"
        ))?;
        let mut rows = stmt.query(params![card_id, tag_id])?;
        while let Some(row) = rows.next()? {
            let source = row.get_ref(1)?.as_blob()?;
            if width == 0 {
                width = source.len();
            }
            if source.len() == width && width > 0 {
                ids.push(row.get(0)?);
                bytes.extend_from_slice(source);
            }
        }
        Ok((ids, width, bytes))
    }

    pub(crate) fn set_rwkv_review_retrievability_prediction(
        &self,
        revlog_id: RevlogId,
        prediction: f32,
        source: &str,
    ) -> Result<()> {
        self.set_rwkv_review_retrievability_predictions(
            &[RwkvReviewRetrievabilityCacheRow {
                revlog_id,
                prediction,
                sample_role: RwkvReviewRetrievabilitySampleRole::PostOptimization,
                fold_index: -1,
            }],
            source,
        )
        .map(|_| ())
    }

    /// The cached per-review predictions of one model and one sample role,
    /// from `after` on: the newest row of each review, by ascending review
    /// id (spec ui.stats-model-metrics). The caller picks the role; rows of
    /// other roles are never mixed in.
    ///
    /// This asks the retrievability-cache sidecar and nothing else. It does
    /// not name the search's `search_cids` table - the caller joins the two
    /// lists, which are both in review order - so it can run on a
    /// connection that has only the sidecar attached.
    pub(crate) fn cached_review_predictions(
        &self,
        table: &str,
        sample_role: &str,
        after: TimestampMillis,
    ) -> Result<Vec<(RevlogId, f32)>> {
        let table = Self::qualified_retrievability_cache_table(table);
        let mut statement = self.db.prepare_cached(&format!(
            "select revlog_id, prediction, updated_at, fold_index, source
             from {table}
             where sample_role = ?1 and revlog_id > ?2
             order by revlog_id"
        ))?;
        let rows = statement.query((sample_role, after.0))?;
        newest_prediction_of_each_review(rows)
    }

    /// `cached_review_predictions` across `roles`: the row written last of
    /// each review wins, whatever its role.
    pub(crate) fn cached_review_predictions_newest_of(
        &self,
        table: &str,
        roles: &[&str],
        after: TimestampMillis,
    ) -> Result<Vec<(RevlogId, f32)>> {
        let table = Self::qualified_retrievability_cache_table(table);
        let mut statement = self.db.prepare(&format!(
            "select revlog_id, prediction, updated_at, fold_index, source
             from {table}
             where revlog_id > ?1 and sample_role in ({})
             order by revlog_id",
            role_list(roles)
        ))?;
        let rows = statement.query((after.0,))?;
        newest_prediction_of_each_review(rows)
    }

    /// The decks whose cards hold rated reviews that no `validation_fold`
    /// row of this model covers, with how many such reviews each holds
    /// (spec ui.stats-fsrs-predictions-ready). The caller maps the decks to
    /// their presets; the query is collection-wide and never scoped to a
    /// search, because a pass that filled only the deck on screen would
    /// leave the same fault everywhere else.
    ///
    /// One part of that count: the reviews with an id above `after`, over
    /// `part_rows` rows of the review log, or over the rest of it when None.
    /// Also returns the id the next part starts after, None when this part
    /// reached the end. The parts' counts, added up, are the whole count.
    ///
    /// The review log leads the join (`cross join`), so the cache is
    /// searched in review-id order: 0.7 s against 5.0 s for the same count
    /// led by the cards, on a collection of 910,750 rated reviews.
    pub(crate) fn decks_with_uncovered_fsrs_review_predictions_part(
        &self,
        after: i64,
        part_rows: Option<usize>,
    ) -> Result<UncoveredReviewsPart> {
        let last = match part_rows {
            Some(rows) => self
                .db
                .prepare_cached(
                    "select id from revlog where id > ?1 order by id limit 1 offset ?2",
                )?
                .query_row((after, rows.max(1) as i64 - 1), |row| row.get(0))
                .optional()?,
            None => None,
        };
        let table =
            Self::qualified_retrievability_cache_table(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE);
        let counts = self
            .db
            .prepare_cached(&format!(
                "select c.did, count(*) from revlog r
                 cross join cards c on c.id = r.cid
                 where r.id > ?1 and r.id <= ?2 and r.ease > 0
                   and not exists (
                       select 1 from {table} t
                       where t.revlog_id = r.id and t.sample_role = 'validation_fold'
                   )
                 group by c.did"
            ))?
            .query_and_then((after, last.unwrap_or(i64::MAX)), |row| {
                Ok((row.get(0)?, row.get(1)?))
            })?
            .collect::<Result<_>>()?;
        Ok((counts, last))
    }

    /// Deletes every stored FSRS prediction of the cards of these decks.
    /// Parameters are per preset, so a preset's own rows go when its
    /// parameters change and no superseded value survives to be drawn.
    pub(crate) fn clear_fsrs_review_predictions_for_decks(
        &self,
        decks: &[DeckId],
    ) -> Result<usize> {
        if decks.is_empty() {
            return Ok(0);
        }
        let table =
            Self::qualified_retrievability_cache_table(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE);
        let mut ids = String::new();
        write_comma_separated_ids(&mut ids, decks.iter().map(|deck| deck.0));
        self.db.execute_batch(&format!(
            "delete from {table} where revlog_id in (
                     select r.id from revlog r join cards c on c.id = r.cid
                     where c.did in ({ids}) or c.odid in ({ids})
                 );"
        ))?;
        Ok(self.db.changes() as usize)
    }

    /// Whether ONE role of a legacy table has a row at all; see
    /// `review_prediction_role_exists`.
    pub(crate) fn cached_review_prediction_role_exists(
        &self,
        table: &str,
        role: &str,
    ) -> Result<bool> {
        let table = Self::qualified_retrievability_cache_table(table);
        self.db
            .prepare_cached(&format!(
                "select exists(select 1 from {table} where sample_role = ?1)"
            ))?
            .query_row((role,), |row| row.get(0))
            .map_err(Into::into)
    }

    /// Deletes the rows this source stored for these reviews. The
    /// prediction pass writes one preset a batch of rows at a time, and
    /// uses this to take back the batches it had already written when the
    /// preset is saved under it, so the cache never holds rows that two
    /// different sets of parameters produced. A review belongs to one card
    /// and a card to one deck, so this can only reach the preset's own
    /// rows.
    pub(crate) fn clear_fsrs_review_predictions_of_reviews(
        &self,
        revlogs: &[RevlogId],
        source: &str,
    ) -> Result<usize> {
        if revlogs.is_empty() {
            return Ok(0);
        }
        let table =
            Self::qualified_retrievability_cache_table(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE);
        let mut deleted = 0;
        // in pieces, so that the statement stays a sane length however many
        // reviews a preset holds
        for chunk in revlogs.chunks(DELETE_REVIEW_PREDICTIONS_CHUNK) {
            let mut ids = String::new();
            write_comma_separated_ids(&mut ids, chunk.iter().map(|id| id.0));
            deleted += self.db.execute(
                &format!("delete from {table} where source = ?1 and revlog_id in ({ids})"),
                params![source],
            )?;
        }
        Ok(deleted)
    }

    pub(crate) fn fix_revlog_properties(&self) -> Result<usize> {
        self.db
            .prepare(include_str!("fix_props.sql"))?
            .execute([])
            .map_err(Into::into)
    }

    pub(crate) fn clear_pending_revlog_usns(&self) -> Result<()> {
        self.db
            .prepare("update revlog set usn = 0 where usn = -1")?
            .execute([])?;
        Ok(())
    }

    /// Adds the entry, if its id is unique. If it is not, and `uniquify` is
    /// true, adds it with a new id. Returns the added id.
    /// (I.e., the option is safe to unwrap, if `uniquify` is true.)
    pub(crate) fn add_revlog_entry(
        &self,
        entry: &RevlogEntry,
        uniquify: bool,
    ) -> Result<Option<RevlogId>> {
        let added = self
            .db
            .prepare_cached(include_str!("add.sql"))?
            .execute(params![
                uniquify,
                entry.id,
                entry.cid,
                entry.usn,
                entry.button_chosen,
                entry.interval,
                entry.last_interval,
                entry.ease_factor,
                entry.taken_millis,
                entry.review_kind as u8
            ])?;
        Ok((added > 0).then(|| RevlogId(self.db.last_insert_rowid())))
    }

    pub(crate) fn get_revlog_entry(&self, id: RevlogId) -> Result<Option<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(include_str!("get.sql"), " where id=?"))?
            .query_and_then([id], row_to_revlog_entry)?
            .next()
            .transpose()
    }

    /// Determine the the last review time based on the revlog.
    pub(crate) fn time_of_last_review(&self, card_id: CardId) -> Result<Option<TimestampSecs>> {
        self.db
            .prepare_cached(include_str!("time_of_last_review.sql"))?
            .query_row([card_id], |row| row.get(0))
            .optional()
            .map_err(Into::into)
    }

    /// The button (1-4) of the card's last rated review, leaving out
    /// preview answers in a filtered deck, as `time_of_last_review` does.
    pub(crate) fn last_review_rating(&self, card_id: CardId) -> Result<Option<u8>> {
        self.db
            .prepare_cached(
                "select ease from revlog where cid = ? and ease between 1 and 4                  and (type != 3 or factor != 0) order by id desc limit 1",
            )?
            .query_row([card_id], |row| row.get(0))
            .optional()
            .map_err(Into::into)
    }

    pub(crate) fn times_of_last_review(
        &self,
        card_ids: &[CardId],
    ) -> Result<HashMap<CardId, TimestampSecs>> {
        if card_ids.is_empty() {
            return Ok(HashMap::new());
        }

        let mut ids = String::new();
        ids_to_string(&mut ids, card_ids);
        let sql = format!(
            "select id, (select revlog.id / 1000 from revlog \
             where cid = cards.id and ease between 1 and 4 \
             and (type != 3 or factor != 0) \
             order by revlog.id desc limit 1) from cards where id in {ids}"
        );
        let mut review_times = HashMap::new();
        let mut stmt = self.db.prepare(&sql)?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            let cid: CardId = row.get(0)?;
            let last_review_time: Option<TimestampSecs> = row.get(1)?;
            if let Some(time) = last_review_time {
                review_times.insert(cid, time);
            }
        }

        Ok(review_times)
    }

    /// The reviews of a card logged at or after `since`, counted the way
    /// `time_of_last_review` finds the last one: answers 1-4, without
    /// filtered-deck reviews that did not reschedule.
    pub(crate) fn review_count_since(
        &self,
        card_id: CardId,
        since: TimestampMillis,
    ) -> Result<u32> {
        self.db
            .prepare_cached(
                "select count() from revlog where cid = ? and id >= ? \
                 and ease between 1 and 4 and (type != 3 or factor != 0)",
            )?
            .query_row(params![card_id, since.0], |row| row.get(0))
            .map_err(Into::into)
    }

    /// Only intended to be used by the undo code, as Anki can not sync revlog
    /// deletions.
    pub(crate) fn remove_revlog_entry(&self, id: RevlogId) -> Result<()> {
        self.db
            .prepare_cached("delete from revlog where id = ?")?
            .execute([id])?;
        Ok(())
    }

    pub(crate) fn get_revlog_entries_for_card(&self, cid: CardId) -> Result<Vec<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(include_str!("get.sql"), " where cid=?"))?
            .query_and_then([cid], row_to_revlog_entry)?
            .collect()
    }

    /// The rated review rows the RWKV replay reads, each card from its start
    /// row onwards.
    ///
    /// The start row is the card's latest learning start. A card with no
    /// learning row at all (an import, another app, an old scheduler) starts at
    /// its first rated row after its last Forget row, or at its first rated row
    /// when it has no Forget row. Manual rows (Forget, Set Due Date) are never
    /// rated, so they never enter the sequence themselves; only the last Forget
    /// cuts the history. The start row always reports
    /// `is_learning_start = true`, so the replay gives it first-row treatment.
    ///
    /// The review log is read in ONE sequential pass over the table, in id
    /// order, and each card's start row is folded into that pass. The SQL this
    /// replaced expressed the same rule with a window function over the rows
    /// sorted by card, which sent SQLite through the card index -- one
    /// scattered row read per review -- and then read the whole history three
    /// more times to group, join and sort it.
    pub(crate) fn rwkv_historical_review_rows(
        &self,
        ignored_review_ids: &[RevlogId],
    ) -> Result<(Vec<RwkvHistoricalReviewRow>, Vec<i64>)> {
        let mut reader = self.rwkv_historical_review_reader(ignored_review_ids)?;
        self.rwkv_historical_review_rows_part(&mut reader, None)?;
        Ok(reader.finish())
    }

    /// A read of `rwkv_historical_review_rows` that can be done in parts:
    /// the ignored reviews are read here, the cards and then the review log
    /// by `rwkv_historical_review_rows_part`. The parts give the rows of one
    /// read only while the collection does not change between them; see
    /// `change_stamp`.
    pub(crate) fn rwkv_historical_review_reader(
        &self,
        ignored_review_ids: &[RevlogId],
    ) -> Result<RwkvHistoricalReviewReader> {
        let active_ignored_review_ids = self.rwkv_active_ignored_review_ids(ignored_review_ids)?;
        let mut ignored: Vec<i64> = ignored_review_ids.iter().map(|id| id.0).collect();
        ignored.sort_unstable();
        Ok(RwkvHistoricalReviewReader {
            ignored,
            active_ignored_review_ids,
            cards: FnvHashMap::default(),
            rows: Vec::new(),
            starts: FnvHashMap::default(),
            after_card_id: Some(i64::MIN),
            after_review_id: i64::MIN,
        })
    }

    /// Reads up to `max_rows` more rows (all of them for None), in id order
    /// after the last part: first the cards, then the review log. True when
    /// the review log is done.
    pub(crate) fn rwkv_historical_review_rows_part(
        &self,
        reader: &mut RwkvHistoricalReviewReader,
        max_rows: Option<usize>,
    ) -> Result<bool> {
        // a negative limit is none
        let limit = max_rows.map_or(-1, |rows| rows as i64);
        let RwkvHistoricalReviewReader {
            ignored,
            cards,
            rows,
            starts,
            after_card_id,
            after_review_id,
            ..
        } = reader;
        if let Some(after) = after_card_id {
            let read = self.rwkv_historical_review_cards_part(cards, after, limit)?;
            if max_rows == Some(read) {
                return Ok(false);
            }
            *after_card_id = None;
            if max_rows.is_some() {
                return Ok(false);
            }
        }
        let mut statement = self.db.prepare_cached(concat!(
            "select r.id, r.cid, r.ease, r.time, r.type, ",
            "cast(r.ivl as integer), cast(r.factor as integer), r.factor = 0 ",
            "from revlog r ",
            // the rated rows the replay reads, plus the Forget rows that cut a
            // card's history. SQLite decides both, exactly as the query this
            // replaced wrote them, so that a column holding something other
            // than a whole number still compares as it did before. Both
            // conditions live in `where`, where a row that fails them costs
            // nothing more: reading the columns first and deciding afterwards
            // measured 270ms slower over 1.3M rows.
            "where ((r.ease between 1 and 4 and r.type in (0, 1, 2, 3, 4, 5) ",
            "        and not (r.type = 3 and r.factor = 0)) ",
            "    or (r.type = 4 and r.factor = 0)) ",
            "  and r.id > ?1 ",
            "order by r.id limit ?2"
        ))?;
        let mut query = statement.query(params![*after_review_id, limit])?;
        let mut read = 0;
        while let Some(row) = query.next()? {
            let review_id: i64 = row.get(0)?;
            *after_review_id = review_id;
            read += 1;
            let card_id: i64 = row.get(1)?;
            // a review whose card is gone belongs to no history: the query
            // this replaced joined `cards`, which dropped it
            let Some(&(note_id, deck_id)) = cards.get(&card_id) else {
                continue;
            };
            let ease: i64 = row.get(2)?;
            let review_kind: i64 = row.get(4)?;
            let ease_factor_is_zero: bool = row.get(7)?;
            let is_forget = review_kind == 4 && ease_factor_is_zero;
            // an ignored review leaves the rated history, but a Forget row
            // still cuts the history even when it is ignored
            let is_rated = (1..=4).contains(&ease)
                && (0..=5).contains(&review_kind)
                && !(review_kind == 3 && ease_factor_is_zero)
                && ignored.binary_search(&review_id).is_err();
            if !is_rated && !is_forget {
                continue;
            }
            let start = starts.entry(card_id).or_default();
            if is_forget {
                start.first_rated_after_forget = None;
            }
            if !is_rated {
                continue;
            }
            if review_kind == 0 && start.previous_rated_kind != Some(0) {
                start.learning_start = Some(review_id);
            }
            start.previous_rated_kind = Some(review_kind);
            if !is_forget && start.first_rated_after_forget.is_none() {
                start.first_rated_after_forget = Some(review_id);
            }
            rows.push(RwkvHistoricalReviewRow {
                review_id,
                card_id,
                note_id,
                deck_id,
                ease,
                duration_millis: row.get(3)?,
                review_kind,
                interval_days: row.get(5)?,
                ease_factor: row.get(6)?,
                is_learning_start: false,
            });
        }
        Ok(max_rows.map_or(true, |max_rows| read < max_rows))
    }

    /// Every reset's review id and card (a Forget row: `type` 4 with no ease
    /// factor), as Total Knowledge reads them (spec ui.stats-total-knowledge).
    pub(crate) fn rwkv_reset_review_ids_and_cards(&self) -> Result<Vec<(i64, i64)>> {
        self.db
            .prepare("select id, cid from revlog where type = 4 and factor = 0")?
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?
            .collect::<std::result::Result<_, _>>()
            .map_err(Into::into)
    }

    /// Which of the reviews the caller asks to ignore still belong to the
    /// rated history, in id order.
    fn rwkv_active_ignored_review_ids(&self, ignored_review_ids: &[RevlogId]) -> Result<Vec<i64>> {
        if ignored_review_ids.is_empty() {
            return Ok(Vec::new());
        }
        let mut ids = String::new();
        ids_to_string(&mut ids, ignored_review_ids);
        let sql = format!(
            "select r.id
             from revlog r
             join cards c on c.id = r.cid
             where r.ease between 1 and 4
               and r.type in (0, 1, 2, 3, 4, 5)
               and not (r.type = 3 and r.factor = 0)
               and r.id in {ids}
             order by r.id"
        );
        self.db
            .prepare(&sql)?
            .query_map([], |row| row.get(0))?
            .collect::<std::result::Result<Vec<i64>, _>>()
            .map_err(Into::into)
    }

    /// Each card's note and home deck, for the replay's rows: up to `limit`
    /// cards (all for a negative limit) in id order after `after`, which
    /// moves to the last card read. Returns how many were read.
    fn rwkv_historical_review_cards_part(
        &self,
        cards: &mut FnvHashMap<i64, (i64, i64)>,
        after: &mut i64,
        limit: i64,
    ) -> Result<usize> {
        let mut statement = self.db.prepare_cached(concat!(
            "select id, nid, case when odid != 0 then odid else did end from cards ",
            "where id > ?1 order by id limit ?2"
        ))?;
        let mut query = statement.query(params![*after, limit])?;
        let mut read = 0;
        while let Some(row) = query.next()? {
            *after = row.get(0)?;
            cards.insert(*after, (row.get(1)?, row.get(2)?));
            read += 1;
        }
        Ok(read)
    }

    /// The searched cards' reviews since `after`, in no particular order.
    /// `many_cards`: the search matched a large part of the collection. The
    /// review log is then read in id order, keeping the searched cards' rows
    /// (`+cid` keeps SQLite off the cid index), instead of one index lookup
    /// per card, whose scattered reads cost several times more per row once
    /// most rows match.
    pub(crate) fn get_revlog_entries_for_searched_cards_after_stamp(
        &self,
        after: TimestampSecs,
        many_cards: bool,
    ) -> Result<Vec<RevlogEntry>> {
        let sql = if many_cards {
            concat!(
                include_str!("get.sql"),
                " where id >= ? and +cid in (select cid from search_cids)"
            )
        } else {
            concat!(
                include_str!("get.sql"),
                " where cid in (select cid from search_cids) and id >= ?"
            )
        };
        self.db
            .prepare_cached(sql)?
            .query_and_then([after.0 * 1000], row_to_revlog_entry)?
            .collect()
    }

    /// The searched cards' ratings that affect scheduling, from `after` on,
    /// in no particular order (spec ui.stats-model-metrics).
    ///
    /// The model-quality graphs read three columns of a review and test two
    /// more; the full review-log row carries four the graphs never look at,
    /// and a large collection decodes 800000 of them.
    ///
    /// `many_cards`: the search matched a large part of the collection, so
    /// the review log is read in id order and the searched cards' rows are
    /// kept, as `get_revlog_entries_for_searched_cards_after_stamp` explains.
    pub(crate) fn searched_ratings_that_affect_scheduling(
        &self,
        after: TimestampMillis,
        many_cards: bool,
    ) -> Result<Vec<SearchedRating>> {
        let sql = if many_cards {
            "select id, cid, ease, factor, type from revlog
             where id > ?1 and +cid in (select cid from search_cids)"
        } else {
            "select id, cid, ease, factor, type from revlog
             where cid in (select cid from search_cids) and id > ?1"
        };
        let mut statement = self.db.prepare_cached(sql)?;
        let mut rows = statement.query([after.0])?;
        let mut ratings = vec![];
        while let Some(row) = rows.next()? {
            // `factor` and `type` only decide whether the rating counts;
            // the kept row carries the answer alone
            let entry = RevlogEntry {
                id: row.get(0)?,
                cid: row.get(1)?,
                button_chosen: row.get(2)?,
                ease_factor: row.get(3)?,
                review_kind: row.get(4).unwrap_or_default(),
                ..Default::default()
            };
            if entry.has_rating_and_affects_scheduling() {
                ratings.push(SearchedRating {
                    id: entry.id,
                    cid: entry.cid,
                    button_chosen: entry.button_chosen,
                });
            }
        }
        Ok(ratings)
    }

    /// The first rating of each searched card over its whole history, the
    /// period aside: a rating as `searched_ratings_that_affect_scheduling`
    /// counts one (spec ui.stats-model-metrics).
    pub(crate) fn first_ratings_of_searched_cards(&self) -> Result<Vec<RevlogId>> {
        self.db
            .prepare_cached(
                "select min(id) from revlog
                 where cid in (select cid from search_cids)
                   and ease > 0 and not (type = 3 and factor = 0)
                 group by cid",
            )?
            .query_and_then([], |row| -> Result<RevlogId> { Ok(row.get(0)?) })?
            .collect()
    }

    pub(crate) fn get_revlog_entries_for_searched_cards(&self) -> Result<Vec<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(
                include_str!("get.sql"),
                " where cid in (select cid from search_cids)"
            ))?
            .query_and_then([], row_to_revlog_entry)?
            .collect()
    }

    pub(crate) fn get_revlog_entries_for_searched_cards_in_card_order(
        &self,
    ) -> Result<Vec<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(
                include_str!("get.sql"),
                " where cid in (select cid from search_cids) order by cid, id"
            ))?
            .query_and_then([], row_to_revlog_entry)?
            .collect()
    }

    /// The review log entries of `cids`, oldest first. One pass over the
    /// whole table: for many cards, far faster than looking each card's
    /// entries up in the cid index.
    pub(crate) fn get_revlog_entries_of_cards_by_scan(
        &self,
        cids: &HashSet<CardId>,
    ) -> Result<Vec<RevlogEntry>> {
        let mut stmt = self.db.prepare_cached(include_str!("get.sql"))?;
        let mut rows = stmt.query([])?;
        let mut entries = vec![];
        while let Some(row) = rows.next()? {
            if cids.contains(&row.get(1)?) {
                entries.push(row_to_revlog_entry(row)?);
            }
        }
        Ok(entries)
    }

    pub(crate) fn get_revlog_entries_for_export_dataset(&self) -> Result<Vec<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(
                include_str!("get.sql"),
                " where (ease between 1 and 4) or (ease = 0 and factor = 0)",
                " order by cid, id"
            ))?
            .query_and_then([], row_to_revlog_entry)?
            .collect()
    }

    pub(crate) fn get_all_revlog_entries_in_card_order(&self) -> Result<Vec<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(include_str!("get.sql"), " order by cid, id"))?
            .query_and_then([], row_to_revlog_entry)?
            .collect()
    }

    pub(crate) fn get_all_revlog_entries(&self, after: TimestampSecs) -> Result<Vec<RevlogEntry>> {
        self.db
            .prepare_cached(concat!(include_str!("get.sql"), " where id >= ?"))?
            .query_and_then([after.0 * 1000], row_to_revlog_entry)?
            .collect()
    }

    pub(crate) fn studied_today(&self, day_cutoff: TimestampSecs) -> Result<StudiedToday> {
        let start = day_cutoff.adding_secs(-86_400).as_millis();
        self.db
            .prepare_cached(include_str!("studied_today.sql"))?
            .query_map(
                [
                    start.0,
                    RevlogReviewKind::Manual as i64,
                    RevlogReviewKind::Rescheduled as i64,
                ],
                |row| {
                    Ok(StudiedToday {
                        cards: row.get(0)?,
                        seconds: row.get(1)?,
                    })
                },
            )?
            .next()
            .unwrap()
            .map_err(Into::into)
    }

    pub(crate) fn studied_today_by_deck(
        &self,
        day_cutoff: TimestampSecs,
    ) -> Result<Vec<(DeckId, usize)>> {
        let start = day_cutoff.adding_secs(-86_400).as_millis();
        self.db
            .prepare_cached(include_str!("studied_today_by_deck.sql"))?
            .query_and_then([start.0], |row| -> Result<_> {
                Ok((DeckId(row.get(0)?), row.get(1)?))
            })?
            .collect()
    }
    pub(crate) fn upgrade_revlog_to_v2(&self) -> Result<()> {
        self.db
            .execute_batch(include_str!("v2_upgrade.sql"))
            .map_err(Into::into)
    }
}

#[cfg(test)]
mod tests {
    use std::path::Path;
    use std::path::PathBuf;
    use std::time::Instant;

    use rusqlite::params;
    use tempfile::tempdir;
    use tempfile::TempDir;

    use super::*;
    use crate::collection::Collection;
    use crate::collection::CollectionBuilder;

    fn temp_collection(name: &str) -> Result<(Collection, TempDir, PathBuf)> {
        let tempdir = tempdir()?;
        let col_path = tempdir.path().join(format!("{name}.anki2"));
        let mut builder = CollectionBuilder::new(&col_path);
        builder.with_desktop_media_paths();
        let col = builder.build()?;
        Ok((col, tempdir, col_path))
    }

    /// Pins spec/ui.md#ui.stats-model-metrics: one prediction per review,
    /// the newest row of the role, and the reviews in ascending order.
    ///
    /// Of several rows of one review the newest wins, then the higher fold,
    /// then the source whose name sorts first. This writes rows that make
    /// each of the three ordering columns decide on its own.
    #[test]
    fn the_newest_row_of_each_review_is_the_one_read() -> Result<()> {
        let (col, _dir, _path) = temp_collection("newest_prediction")?;
        // creates the table, and leaves a row of another role behind
        col.storage.set_fsrs_review_retrievability_predictions(
            &[FsrsReviewRetrievabilityCacheRow {
                revlog_id: RevlogId(5),
                prediction: 0.11,
                sample_role: FsrsReviewRetrievabilitySampleRole::FinalFit,
                fold_index: -1,
            }],
            "other_role",
        )?;
        let table = SqliteStorage::qualified_retrievability_cache_table(
            FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE,
        );
        let write = |revlog_id: i64,
                     prediction: f64,
                     fold_index: i64,
                     source: &str,
                     updated_at: i64|
         -> Result<()> {
            col.storage.db.execute(
                &format!(
                    "insert into {table}
                     (revlog_id, prediction, source, updated_at, sample_role, fold_index)
                     values (?1, ?2, ?3, ?4, 'validation_fold', ?5)"
                ),
                params![revlog_id, prediction, source, updated_at, fold_index],
            )?;
            Ok(())
        };
        // review 20: the newest row wins, whatever its fold or source
        write(20, 0.20, 9, "aaa", 100)?;
        write(20, 0.21, 0, "zzz", 200)?;
        // review 10: same moment, so the higher fold wins
        write(10, 0.10, 1, "zzz", 100)?;
        write(10, 0.11, 7, "aaa", 100)?;
        // review 30: same moment and fold, so the lower source wins
        write(30, 0.30, 3, "bbb", 100)?;
        write(30, 0.31, 3, "aaa", 100)?;
        // review 40: one row, and it is below the cutoff of the read below
        write(40, 0.40, 0, "aaa", 100)?;

        let read = col.storage.cached_review_predictions(
            FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE,
            "validation_fold",
            0.into(),
        )?;
        assert_eq!(
            read,
            vec![
                (RevlogId(10), 0.11),
                (RevlogId(20), 0.21),
                (RevlogId(30), 0.31),
                (RevlogId(40), 0.40),
            ]
        );
        // `after` is exclusive, and the other role is never mixed in
        let later = col.storage.cached_review_predictions(
            FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE,
            "validation_fold",
            30.into(),
        )?;
        assert_eq!(later, vec![(RevlogId(40), 0.40)]);
        Ok(())
    }

    fn curve_tag(model: &str) -> RwkvCurveSourceTag {
        RwkvCurveSourceTag {
            model: model.into(),
            format: 1,
            kernel: 1,
        }
    }

    /// The recordings check in qt/aqt/rwkv_scheduler.py
    /// (`_rwkv_recorded_row_counts`) counts a tag's sources with this query;
    /// it must be answered from the index, not by reading every source.
    #[test]
    fn the_recordings_check_counts_curve_sources_from_the_index() -> Result<()> {
        let (col, _tempdir, _path) = temp_collection("curve_sources_index")?;
        col.storage
            .set_rwkv_curve_sources(&curve_tag("model-a"), &[10, 20], &[1, 1, 2, 2], 2)?;
        let plan: Vec<String> = col
            .storage
            .db
            .prepare(&format!(
                "explain query plan
                 select count(*) from {RETRIEVABILITY_CACHE_DB_SCHEMA}.{RWKV_CURVE_SOURCES_TABLE} s
                 join {RETRIEVABILITY_CACHE_DB_SCHEMA}.{RWKV_CURVE_SOURCE_TAGS_TABLE} t
                   on t.id = s.tag
                 where t.model = ? and t.format = ? and t.kernel = ? and s.revlog_id <= ?"
            ))?
            .query_map(params!["model-a", 1, 1, 20], |row| row.get(3))?
            .collect::<std::result::Result<_, _>>()?;
        assert!(
            plan.iter().any(
                |step| step.contains(&format!("COVERING INDEX {RWKV_CURVE_SOURCES_TAG_INDEX}"))
            ),
            "{plan:?}"
        );
        Ok(())
    }

    // Pins spec/ui.md#ui.card-info-rwkv-curve: curve sources are saved in
    // the cache file beside the collection, never in the collection itself;
    // a card gets its own reviews' sources, oldest first; a source is read
    // only under the tag that saved it; and saving under a new tag drops the
    // stale ones.
    #[test]
    fn rwkv_curve_sources_are_per_review_cache_rows_read_by_tag() -> Result<()> {
        let (col, _tempdir, _path) = temp_collection("curve_sources")?;
        for (id, cid) in [(10, 1), (20, 2), (30, 1)] {
            col.storage.db.execute(
                "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type)
                 values (?, ?, 0, 3, 1, 0, 2500, 1000, 1)",
                params![id, cid],
            )?;
        }
        let tag = curve_tag("model-a");
        let stored =
            col.storage
                .set_rwkv_curve_sources(&tag, &[30, 20, 10], &[3, 3, 2, 2, 1, 1], 2)?;
        assert_eq!(stored, 3);

        assert_eq!(
            col.storage.rwkv_curve_sources_for_card(CardId(1), &tag)?,
            (vec![10, 30], 2, vec![1, 1, 3, 3])
        );
        let empty = (vec![], 0, vec![]);
        assert_eq!(
            col.storage
                .rwkv_curve_sources_for_card(CardId(1), &curve_tag("model-b"))?,
            empty
        );
        let other_kernel = RwkvCurveSourceTag {
            kernel: 2,
            ..tag.clone()
        };
        assert_eq!(
            col.storage
                .rwkv_curve_sources_for_card(CardId(1), &other_kernel)?,
            empty
        );
        let in_collection: i64 = col.storage.db.query_row(
            "select count() from main.sqlite_master where name like 'rwkv_curve_source%'",
            [],
            |row| row.get(0),
        )?;
        assert_eq!(in_collection, 0);

        // a new model's sources replace the old model's
        col.storage
            .set_rwkv_curve_sources(&curve_tag("model-b"), &[20], &[9, 9, 9], 3)?;
        let rows: i64 = col.storage.db.query_row(
            &format!(
                "select count() from {RETRIEVABILITY_CACHE_DB_SCHEMA}.{RWKV_CURVE_SOURCES_TABLE}"
            ),
            [],
            |row| row.get(0),
        )?;
        assert_eq!(rows, 1);
        assert_eq!(
            col.storage.rwkv_curve_sources_for_card(CardId(1), &tag)?,
            empty
        );
        assert_eq!(
            col.storage
                .rwkv_curve_sources_for_card(CardId(2), &curve_tag("model-b"))?,
            (vec![20], 3, vec![9, 9, 9])
        );
        Ok(())
    }

    fn fsrs_cache_rows(count: usize, offset: i64) -> Vec<FsrsReviewRetrievabilityCacheRow> {
        (0..count)
            .map(|index| FsrsReviewRetrievabilityCacheRow {
                revlog_id: RevlogId(offset + index as i64 + 1),
                prediction: (index % 100) as f32 / 100.0,
                sample_role: FsrsReviewRetrievabilitySampleRole::FinalFit,
                fold_index: -1,
            })
            .collect()
    }

    fn count_fsrs_cache_rows(storage: &SqliteStorage) -> Result<usize> {
        storage
            .db
            .query_row(
                &format!("SELECT count() FROM {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE}"),
                [],
                |row| row.get::<_, usize>(0),
            )
            .map_err(Into::into)
    }

    fn clear_fsrs_cache_rows(storage: &SqliteStorage) -> Result<()> {
        storage.db.execute_batch(&format!(
            "DELETE FROM {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE};"
        ))?;
        Ok(())
    }

    fn checkpoint_truncate(storage: &SqliteStorage) -> Result<()> {
        storage
            .db
            .execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")?;
        Ok(())
    }

    fn wal_path(path: &Path) -> PathBuf {
        let mut wal = path.as_os_str().to_os_string();
        wal.push("-wal");
        wal.into()
    }

    fn file_size(path: &Path) -> u64 {
        std::fs::metadata(path)
            .map(|metadata| metadata.len())
            .unwrap_or(0)
    }

    fn sidecar_table_exists(storage: &SqliteStorage, table: &str) -> Result<bool> {
        storage
            .db
            .prepare(&format!(
                "SELECT null FROM {RETRIEVABILITY_CACHE_DB_SCHEMA}.sqlite_master \
                 WHERE type = 'table' AND name = ?"
            ))?
            .exists([table])
            .map_err(Into::into)
    }

    #[test]
    fn retrievability_cache_tables_are_external_to_collection_db() -> Result<()> {
        let (col, _tempdir, col_path) = temp_collection("external-retrievability-cache")?;
        let rows = fsrs_cache_rows(1, 0);

        col.storage
            .set_fsrs_review_retrievability_predictions(&rows, "test")?;
        col.storage
            .set_rwkv_review_retrievability_prediction(RevlogId(1), 0.42, "rwkv_review")?;

        assert!(!col
            .storage
            .main_table_exists(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE)?);
        assert!(!col
            .storage
            .main_table_exists(RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE)?);
        assert!(sidecar_table_exists(
            &col.storage,
            FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE
        )?);
        assert!(sidecar_table_exists(
            &col.storage,
            RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE
        )?);
        assert!(super::super::sqlite::retrievability_cache_path(&col_path).exists());
        assert_eq!(count_fsrs_cache_rows(&col.storage)?, 1);

        let rwkv_count: usize = col.storage.db.query_row(
            &format!("SELECT count() FROM {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE}"),
            [],
            |row| row.get(0),
        )?;
        assert_eq!(rwkv_count, 1);
        Ok(())
    }

    #[test]
    fn legacy_main_retrievability_cache_tables_migrate_to_sidecar() -> Result<()> {
        let (col, _tempdir, col_path) = temp_collection("migrate-retrievability-cache")?;
        col.storage.db.execute_batch(&format!(
            "
            CREATE TABLE main.{FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE} (
                revlog_id INTEGER NOT NULL,
                prediction REAL NOT NULL,
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                sample_role TEXT NOT NULL DEFAULT 'final_fit',
                fold_index INTEGER NOT NULL DEFAULT -1,
                PRIMARY KEY (revlog_id, sample_role, fold_index, source)
            );
            INSERT INTO main.{FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                (revlog_id, prediction, source, updated_at, sample_role, fold_index)
            VALUES (1, 0.25, 'legacy_fsrs', 123, 'validation_fold', 2);
            CREATE TABLE main.{RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} (
                revlog_id INTEGER NOT NULL,
                prediction REAL NOT NULL,
                source TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                sample_role TEXT NOT NULL DEFAULT 'final_fit',
                fold_index INTEGER NOT NULL DEFAULT -1,
                PRIMARY KEY (revlog_id, sample_role, fold_index, source)
            );
            INSERT INTO main.{RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                (revlog_id, prediction, source, updated_at, sample_role, fold_index)
            VALUES (2, 0.75, 'legacy_rwkv', 456, 'test_fold', 0);
            "
        ))?;
        col.storage
            .set_schema_modified_time(TimestampMillis(1_000))?;
        col.storage.set_last_sync(TimestampMillis(1_000))?;
        col.close(None)?;

        let mut builder = CollectionBuilder::new(&col_path);
        builder.with_desktop_media_paths();
        let col = builder.build()?;

        assert!(!col
            .storage
            .main_table_exists(FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE)?);
        assert!(!col
            .storage
            .main_table_exists(RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE)?);
        assert!(col
            .storage
            .get_collection_timestamps()?
            .schema_changed_since_sync());
        assert!(col
            .storage
            .review_retrievability_cache_cleanup_full_sync_marked()?);

        let fsrs: (f64, String, i64) = col.storage.db.query_row(
            &format!(
                "SELECT prediction, sample_role, fold_index \
                 FROM {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE} WHERE revlog_id = 1"
            ),
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )?;
        assert_eq!(fsrs, (0.25, "validation_fold".to_string(), 2));

        let rwkv: (f64, String, i64) = col.storage.db.query_row(
            &format!(
                "SELECT prediction, sample_role, fold_index \
                 FROM {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} WHERE revlog_id = 2"
            ),
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )?;
        assert_eq!(rwkv, (0.75, "test_fold".to_string(), 0));
        Ok(())
    }

    fn legacy_autocommit_fsrs_insert(
        storage: &SqliteStorage,
        rows: &[FsrsReviewRetrievabilityCacheRow],
        source: &str,
    ) -> Result<usize> {
        storage.ensure_fsrs_review_retrievability_cache_schema()?;
        let updated_at = TimestampMillis::now().0;
        let mut stored = 0;
        let mut stmt = storage.db.prepare_cached(&format!(
            "
            INSERT OR REPLACE INTO {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                (revlog_id, prediction, source, updated_at, sample_role, fold_index)
            VALUES (?, ?, ?, ?, ?, ?)
            "
        ))?;

        for row in rows {
            if row.revlog_id.0 > 0
                && row.prediction.is_finite()
                && (0.0..=1.0).contains(&row.prediction)
            {
                stmt.execute(params![
                    row.revlog_id,
                    row.prediction,
                    source,
                    updated_at,
                    row.sample_role.as_str(),
                    row.fold_index
                ])?;
                stored += 1;
            }
        }

        Ok(stored)
    }

    #[test]
    fn fsrs_retrievability_cache_batch_works_inside_transaction() -> Result<()> {
        let (col, _tempdir, _col_path) = temp_collection("fsrs-cache-transaction")?;
        let rows = fsrs_cache_rows(3, 0);

        col.storage.begin_trx()?;
        let stored = col
            .storage
            .set_fsrs_review_retrievability_predictions(&rows, "test")?;
        col.storage.commit_trx()?;

        assert_eq!(stored, 3);
        assert_eq!(count_fsrs_cache_rows(&col.storage)?, 3);
        Ok(())
    }

    #[test]
    fn fsrs_retrievability_cache_skips_unchanged_rebuild_rows() -> Result<()> {
        let (col, _tempdir, _col_path) = temp_collection("fsrs-cache-skip-unchanged")?;
        let mut rows = fsrs_cache_rows(1, 0);

        col.storage
            .set_fsrs_review_retrievability_predictions(&rows, "test")?;
        col.storage.db.execute(
            &format!(
                "
                UPDATE {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                SET updated_at = 123
                WHERE revlog_id = ? AND sample_role = ? AND fold_index = ? AND source = ?
                "
            ),
            params![
                rows[0].revlog_id,
                rows[0].sample_role.as_str(),
                rows[0].fold_index,
                "test",
            ],
        )?;

        assert_eq!(
            col.storage
                .set_fsrs_review_retrievability_predictions(&rows, "test")?,
            1
        );
        let unchanged_updated_at: i64 = col.storage.db.query_row(
            &format!(
                "SELECT updated_at FROM {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE} WHERE revlog_id = ?"
            ),
            [rows[0].revlog_id],
            |row| row.get(0),
        )?;
        assert_eq!(unchanged_updated_at, 123);

        rows[0].prediction = 0.99;
        col.storage
            .set_fsrs_review_retrievability_predictions(&rows, "test")?;
        let (changed_prediction, changed_updated_at): (f64, i64) = col.storage.db.query_row(
            &format!(
                "SELECT prediction, updated_at FROM {FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE} WHERE revlog_id = ?"
            ),
            [rows[0].revlog_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )?;
        assert!((changed_prediction - 0.99).abs() < 1e-6);
        assert_ne!(changed_updated_at, 123);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_cache_batch_works_inside_transaction() -> Result<()> {
        let (col, _tempdir, _col_path) = temp_collection("rwkv-cache-transaction")?;
        let rows = [
            RwkvReviewRetrievabilityCacheRow {
                revlog_id: RevlogId(1),
                prediction: 0.25,
                sample_role: RwkvReviewRetrievabilitySampleRole::FinalFit,
                fold_index: -1,
            },
            RwkvReviewRetrievabilityCacheRow {
                revlog_id: RevlogId(2),
                prediction: 0.75,
                sample_role: RwkvReviewRetrievabilitySampleRole::PostOptimization,
                fold_index: -1,
            },
        ];

        col.storage.begin_trx()?;
        let stored = col
            .storage
            .set_rwkv_review_retrievability_predictions(&rows, "test")?;
        col.storage.commit_trx()?;

        let count: usize = col.storage.db.query_row(
            &format!("SELECT count() FROM {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE}"),
            [],
            |row| row.get(0),
        )?;
        assert_eq!(stored, 2);
        assert_eq!(count, 2);
        Ok(())
    }

    #[test]
    fn rwkv_retrievability_cache_skips_unchanged_rebuild_rows() -> Result<()> {
        let (col, _tempdir, _col_path) = temp_collection("rwkv-cache-skip-unchanged")?;
        let mut rows = [RwkvReviewRetrievabilityCacheRow {
            revlog_id: RevlogId(1),
            prediction: 0.25,
            sample_role: RwkvReviewRetrievabilitySampleRole::FinalFit,
            fold_index: -1,
        }];

        col.storage
            .set_rwkv_review_retrievability_predictions(&rows, "test")?;
        col.storage.db.execute(
            &format!(
                "
                UPDATE {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE}
                SET updated_at = 123
                WHERE revlog_id = ? AND sample_role = ? AND fold_index = ? AND source = ?
                "
            ),
            params![
                rows[0].revlog_id,
                rows[0].sample_role.as_str(),
                rows[0].fold_index,
                "test",
            ],
        )?;

        assert_eq!(
            col.storage
                .set_rwkv_review_retrievability_predictions(&rows, "test")?,
            1
        );
        let unchanged_updated_at: i64 = col.storage.db.query_row(
            &format!(
                "SELECT updated_at FROM {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} WHERE revlog_id = ?"
            ),
            [rows[0].revlog_id],
            |row| row.get(0),
        )?;
        assert_eq!(unchanged_updated_at, 123);

        rows[0].prediction = 0.75;
        col.storage
            .set_rwkv_review_retrievability_predictions(&rows, "test")?;
        let (changed_prediction, changed_updated_at): (f64, i64) = col.storage.db.query_row(
            &format!(
                "SELECT prediction, updated_at FROM {RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE} WHERE revlog_id = ?"
            ),
            [rows[0].revlog_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )?;
        assert!((changed_prediction - 0.75).abs() < 1e-6);
        assert_ne!(changed_updated_at, 123);
        Ok(())
    }

    #[test]
    #[ignore]
    fn retrievability_cache_insert_benchmark() -> Result<()> {
        let row_count = std::env::var("ANKI_RETRIEVABILITY_CACHE_BENCH_ROWS")
            .ok()
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(10_000);
        let repeated_runs = std::env::var("ANKI_RETRIEVABILITY_CACHE_BENCH_REPEATS")
            .ok()
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(3);
        let (col, _tempdir, col_path) =
            if let Ok(source) = std::env::var("ANKI_RETRIEVABILITY_CACHE_BENCH_COLLECTION") {
                let tempdir = tempdir()?;
                let col_path = tempdir.path().join("bench.anki2");
                std::fs::copy(source, &col_path)?;
                let mut builder = CollectionBuilder::new(&col_path);
                builder.with_desktop_media_paths();
                (builder.build()?, tempdir, col_path)
            } else {
                temp_collection("retrievability-cache-bench")?
            };
        let wal_path = wal_path(&col_path);
        let rows = fsrs_cache_rows(row_count, 1_000_000_000);

        col.storage
            .set_fsrs_review_retrievability_predictions(&rows[0..1], "setup")?;
        clear_fsrs_cache_rows(&col.storage)?;
        checkpoint_truncate(&col.storage)?;

        let legacy_started = Instant::now();
        let legacy_stored = legacy_autocommit_fsrs_insert(&col.storage, &rows, "legacy")?;
        let legacy_elapsed = legacy_started.elapsed();
        let legacy_wal_bytes = file_size(&wal_path);
        clear_fsrs_cache_rows(&col.storage)?;
        checkpoint_truncate(&col.storage)?;

        let batched_started = Instant::now();
        let batched_stored = col
            .storage
            .set_fsrs_review_retrievability_predictions(&rows, "batched")?;
        let batched_elapsed = batched_started.elapsed();
        let batched_wal_bytes = file_size(&wal_path);

        let mut repeated_stored = 0;
        let mut repeated_total_ms = 0.0;
        let mut repeated_wal_bytes = 0;
        for _ in 0..repeated_runs {
            checkpoint_truncate(&col.storage)?;
            let repeated_started = Instant::now();
            repeated_stored += col
                .storage
                .set_fsrs_review_retrievability_predictions(&rows, "batched")?;
            repeated_total_ms += repeated_started.elapsed().as_secs_f64() * 1000.0;
            repeated_wal_bytes += file_size(&wal_path);
        }

        println!(
            "retrievability_cache_insert_benchmark rows={row_count} repeated_runs={repeated_runs} \
             legacy_stored={legacy_stored} legacy_ms={:.3} legacy_wal_bytes={} \
             batched_stored={batched_stored} batched_ms={:.3} batched_wal_bytes={} \
             repeated_stored={repeated_stored} repeated_total_ms={repeated_total_ms:.3} \
             repeated_wal_bytes={repeated_wal_bytes}",
            legacy_elapsed.as_secs_f64() * 1000.0,
            legacy_wal_bytes,
            batched_elapsed.as_secs_f64() * 1000.0,
            batched_wal_bytes,
        );

        assert_eq!(legacy_stored, row_count);
        assert_eq!(batched_stored, row_count);
        assert_eq!(repeated_stored, row_count * repeated_runs);
        Ok(())
    }

    #[test]
    #[ignore]
    fn times_of_last_review_benchmark() -> Result<()> {
        let card_count: usize = std::env::var("ANKI_TIMES_BENCH_CARDS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(2048);
        let reviews_per_card: usize = std::env::var("ANKI_TIMES_BENCH_REVIEWS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(20);
        let repeats: usize = std::env::var("ANKI_TIMES_BENCH_REPEATS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(5);

        let (col, _tempdir, _col_path) = if let Ok(source) =
            std::env::var("ANKI_TIMES_BENCH_COLLECTION")
        {
            let tempdir = tempdir()?;
            let col_path = tempdir.path().join("bench.anki2");
            std::fs::copy(&source, &col_path)?;
            let mut builder = CollectionBuilder::new(&col_path);
            builder.with_desktop_media_paths();
            (builder.build()?, tempdir, col_path)
        } else {
            let (col, tempdir, col_path) = temp_collection("times-bench")?;
            let base_ts: i64 = 1_700_000_000_000;
            col.storage.db.execute_batch("BEGIN")?;
            for card_idx in 0..card_count {
                let card_id = (card_idx + 1) as i64;
                col.storage.db.execute(
                    "INSERT INTO cards (id, nid, did, ord, mod, usn, type, queue, due, ivl, \
                         factor, reps, lapses, left, odue, odid, flags, data) \
                         VALUES (?1, 1, 1, 0, 0, 0, 2, 2, 0, 30, 2500, ?2, 0, 0, 0, 0, 0, '')",
                    params![card_id, reviews_per_card],
                )?;
                for rev_idx in 0..reviews_per_card {
                    let revlog_id = base_ts + (card_idx * reviews_per_card + rev_idx) as i64;
                    col.storage.db.execute(
                            "INSERT INTO revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type) \
                             VALUES (?1, ?2, 0, 2, 30, 1, 2500, 10000, 1)",
                            params![revlog_id, card_id],
                        )?;
                }
            }
            col.storage.db.execute_batch("COMMIT")?;
            (col, tempdir, col_path)
        };

        let card_ids: Vec<CardId> = if std::env::var("ANKI_TIMES_BENCH_COLLECTION").is_ok() {
            let mut stmt = col
                .storage
                .db
                .prepare("SELECT id FROM cards WHERE queue = 2 LIMIT ?1")?;
            let ids: Vec<CardId> = stmt
                .query_map([card_count as i64], |row| row.get(0))?
                .collect::<std::result::Result<Vec<_>, _>>()?;
            ids
        } else {
            (1..=card_count as i64).map(CardId).collect()
        };

        println!(
            "times_of_last_review_benchmark: cards={} reviews_per_card={} repeats={}",
            card_ids.len(),
            reviews_per_card,
            repeats,
        );

        // Benchmark: new GROUP BY approach (current)
        let mut group_by_total_ms = 0.0;
        let mut group_by_result = HashMap::new();
        for _ in 0..repeats {
            let start = Instant::now();
            group_by_result = col.storage.times_of_last_review(&card_ids)?;
            group_by_total_ms += start.elapsed().as_secs_f64() * 1000.0;
        }

        // Benchmark: old correlated subquery approach
        let mut correlated_total_ms = 0.0;
        let mut correlated_result = HashMap::new();
        for _ in 0..repeats {
            let start = Instant::now();
            correlated_result = times_of_last_review_correlated(&col.storage, &card_ids)?;
            correlated_total_ms += start.elapsed().as_secs_f64() * 1000.0;
        }

        println!(
            "  GROUP BY:   total={:.1}ms avg={:.2}ms results={}",
            group_by_total_ms,
            group_by_total_ms / repeats as f64,
            group_by_result.len(),
        );
        println!(
            "  CORRELATED: total={:.1}ms avg={:.2}ms results={}",
            correlated_total_ms,
            correlated_total_ms / repeats as f64,
            correlated_result.len(),
        );
        println!(
            "  Speedup: {:.1}x",
            correlated_total_ms / group_by_total_ms.max(0.001),
        );

        assert_eq!(group_by_result.len(), correlated_result.len());
        for (card_id, group_by_time) in &group_by_result {
            assert_eq!(
                correlated_result.get(card_id),
                Some(group_by_time),
                "mismatch for card_id={card_id:?}",
            );
        }
        Ok(())
    }

    fn times_of_last_review_correlated(
        storage: &SqliteStorage,
        card_ids: &[CardId],
    ) -> Result<HashMap<CardId, TimestampSecs>> {
        if card_ids.is_empty() {
            return Ok(HashMap::new());
        }
        let mut ids = String::new();
        ids_to_string(&mut ids, card_ids);
        let sql = format!(
            "select id, (select revlog.id / 1000 from revlog \
             where cid = cards.id and ease between 1 and 4 \
             and (type != 3 or factor != 0) \
             order by revlog.id desc limit 1) from cards where id in {ids}"
        );
        let mut review_times = HashMap::new();
        let mut stmt = storage.db.prepare(&sql)?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            let cid: CardId = row.get(0)?;
            let last_review_time: Option<TimestampSecs> = row.get(1)?;
            if let Some(time) = last_review_time {
                review_times.insert(cid, time);
            }
        }
        Ok(review_times)
    }

    /// Review kinds and buttons used by the RWKV replay start-row tests.
    const RATED_LEARNING: (i64, i64, i64) = (3, 0, 2500);
    const RATED_REVIEW: (i64, i64, i64) = (3, 1, 2500);
    const RATED_RELEARNING: (i64, i64, i64) = (1, 2, 2500);
    /// Reset: manual row with a zero ease factor.
    const FORGET: (i64, i64, i64) = (0, 4, 0);
    /// Set Due Date, as old Anki wrote it: a Manual row with a non-zero ease
    /// factor.
    const SET_DUE_DATE_AS_MANUAL: (i64, i64, i64) = (0, 4, 2500);
    /// Set Due Date, as modern Anki writes it: `RevlogReviewKind::Rescheduled`
    /// with a zero ease.
    const SET_DUE_DATE_AS_RESCHEDULED: (i64, i64, i64) = (0, 5, 2500);

    fn add_replay_card(col: &Collection, card_id: i64) -> Result<()> {
        col.storage.db.execute(
            "insert into cards (id, nid, did, ord, mod, usn, type, queue, due, ivl, \
             factor, reps, lapses, left, odue, odid, flags, data) \
             values (?, 1, 1, 0, 0, -1, 2, 2, 1, 10, 2500, 0, 0, 0, 0, 0, 0, '')",
            [card_id],
        )?;
        Ok(())
    }

    fn add_replay_revlog(
        col: &Collection,
        review_id: i64,
        card_id: i64,
        row: (i64, i64, i64),
    ) -> Result<()> {
        let (ease, kind, factor) = row;
        col.storage.db.execute(
            "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type) \
             values (?, ?, -1, ?, 10, 5, ?, 1000, ?)",
            [review_id, card_id, ease, factor, kind],
        )?;
        Ok(())
    }

    /// `(review id, is_learning_start)` of every replayed row of `card_id`.
    fn replay_start_rows(col: &Collection, card_id: i64) -> Result<Vec<(i64, bool)>> {
        let (rows, _) = col.storage.rwkv_historical_review_rows(&[])?;
        Ok(rows
            .into_iter()
            .filter(|row| row.card_id == card_id)
            .map(|row| (row.review_id, row.is_learning_start))
            .collect())
    }

    #[test]
    fn rwkv_replay_keeps_the_latest_learning_start() -> Result<()> {
        let (col, _tempdir, _path) = temp_collection("rwkv-replay-learning-start")?;
        add_replay_card(&col, 100)?;
        add_replay_revlog(&col, 1000, 100, RATED_LEARNING)?;
        add_replay_revlog(&col, 2000, 100, RATED_REVIEW)?;
        // A Forget followed by a new learning start: the learning start wins.
        add_replay_revlog(&col, 3000, 100, FORGET)?;
        add_replay_revlog(&col, 4000, 100, RATED_LEARNING)?;
        add_replay_revlog(&col, 5000, 100, RATED_REVIEW)?;

        assert_eq!(
            replay_start_rows(&col, 100)?,
            vec![(4000, true), (5000, false)]
        );
        Ok(())
    }

    #[test]
    fn rwkv_replay_card_without_a_learning_row_starts_at_its_first_rated_row() -> Result<()> {
        let (col, _tempdir, _path) = temp_collection("rwkv-replay-no-learning-row")?;
        add_replay_card(&col, 200)?;
        add_replay_revlog(&col, 1000, 200, RATED_REVIEW)?;
        add_replay_revlog(&col, 2000, 200, RATED_RELEARNING)?;
        add_replay_revlog(&col, 3000, 200, RATED_REVIEW)?;

        assert_eq!(
            replay_start_rows(&col, 200)?,
            vec![(1000, true), (2000, false), (3000, false)]
        );
        Ok(())
    }

    #[test]
    fn rwkv_replay_card_without_a_learning_row_starts_after_its_forget() -> Result<()> {
        let (col, _tempdir, _path) = temp_collection("rwkv-replay-forget-then-reviews")?;
        add_replay_card(&col, 300)?;
        add_replay_revlog(&col, 1000, 300, RATED_REVIEW)?;
        add_replay_revlog(&col, 2000, 300, FORGET)?;
        add_replay_revlog(&col, 3000, 300, RATED_REVIEW)?;
        add_replay_revlog(&col, 4000, 300, RATED_REVIEW)?;

        // The rows before the Forget are dropped, not merged.
        assert_eq!(
            replay_start_rows(&col, 300)?,
            vec![(3000, true), (4000, false)]
        );
        Ok(())
    }

    #[test]
    fn rwkv_replay_set_due_date_does_not_cut_the_history() -> Result<()> {
        let (col, _tempdir, _path) = temp_collection("rwkv-replay-set-due-date")?;
        add_replay_card(&col, 400)?;
        // Both encodings: old Anki wrote a Manual row, modern Anki writes a
        // Rescheduled row.
        add_replay_revlog(&col, 1000, 400, SET_DUE_DATE_AS_MANUAL)?;
        add_replay_revlog(&col, 2000, 400, RATED_REVIEW)?;
        add_replay_revlog(&col, 3000, 400, SET_DUE_DATE_AS_MANUAL)?;
        add_replay_revlog(&col, 4000, 400, RATED_REVIEW)?;
        add_replay_revlog(&col, 5000, 400, SET_DUE_DATE_AS_RESCHEDULED)?;
        add_replay_revlog(&col, 6000, 400, RATED_REVIEW)?;

        assert_eq!(
            replay_start_rows(&col, 400)?,
            vec![(2000, true), (4000, false), (6000, false)]
        );
        Ok(())
    }

    #[test]
    fn rwkv_replay_uses_only_the_last_forget() -> Result<()> {
        let (col, _tempdir, _path) = temp_collection("rwkv-replay-several-forgets")?;
        add_replay_card(&col, 500)?;
        add_replay_revlog(&col, 1000, 500, RATED_REVIEW)?;
        add_replay_revlog(&col, 2000, 500, FORGET)?;
        add_replay_revlog(&col, 3000, 500, RATED_REVIEW)?;
        add_replay_revlog(&col, 4000, 500, FORGET)?;
        add_replay_revlog(&col, 5000, 500, RATED_REVIEW)?;
        add_replay_revlog(&col, 6000, 500, RATED_RELEARNING)?;

        assert_eq!(
            replay_start_rows(&col, 500)?,
            vec![(5000, true), (6000, false)]
        );
        Ok(())
    }

    #[test]
    fn rwkv_replay_learning_start_wins_over_a_later_forget() -> Result<()> {
        let (col, _tempdir, _path) = temp_collection("rwkv-replay-learning-start-wins")?;
        add_replay_card(&col, 600)?;
        add_replay_revlog(&col, 1000, 600, RATED_LEARNING)?;
        add_replay_revlog(&col, 2000, 600, RATED_REVIEW)?;
        add_replay_revlog(&col, 3000, 600, FORGET)?;
        // No Learning row follows the Forget, only Review rows.
        add_replay_revlog(&col, 4000, 600, RATED_REVIEW)?;
        add_replay_revlog(&col, 5000, 600, RATED_REVIEW)?;

        // Rule 1 wins: the card keeps its learning start, so the rows from
        // before the Forget stay and the Forget is ignored. This matches the
        // training dataset builder, which drops manual rows before it masks, so
        // a Forget is invisible there unless a Learning row follows it.
        assert_eq!(
            replay_start_rows(&col, 600)?,
            vec![(1000, true), (2000, false), (4000, false), (5000, false)]
        );
        Ok(())
    }

    #[test]
    fn rwkv_replay_drops_a_card_whose_last_row_is_a_forget() -> Result<()> {
        let (col, _tempdir, _path) = temp_collection("rwkv-replay-trailing-forget")?;
        add_replay_card(&col, 700)?;
        add_replay_revlog(&col, 1000, 700, RATED_REVIEW)?;
        add_replay_revlog(&col, 2000, 700, RATED_REVIEW)?;
        add_replay_revlog(&col, 3000, 700, FORGET)?;

        // The card has no learning start and no rated row after its Forget, so
        // it gets no start row and leaves the replay. The Forget reset it, so
        // there is no memory left to replay. This is asymmetric with
        // `rwkv_replay_learning_start_wins_over_a_later_forget` on purpose: see
        // the spec entry.
        assert_eq!(replay_start_rows(&col, 700)?, vec![]);
        Ok(())
    }

    /// The query `rwkv_historical_review_rows` replaced, kept as the oracle of
    /// the one-pass read. Every rule of `sched.rwkv-replay-start-row` is
    /// written here in SQL, so a test that compares the two proves the rewrite
    /// changed nothing.
    const REPLACED_HISTORICAL_ROWS_SQL: &str = "
with eligible as (
  select
    r.id,
    r.cid,
    c.nid,
    case when c.odid != 0 then c.odid else c.did end as deck_id,
    r.ease,
    r.time,
    r.type,
    cast(r.ivl as integer) as interval_days,
    cast(r.factor as integer) as ease_factor,
    lag(r.type) over (partition by r.cid order by r.id) as previous_type
  from revlog r
  join cards c on c.id = r.cid
  where r.ease between 1 and 4
    and r.type in (0, 1, 2, 3, 4, 5)
    and not (r.type = 3 and r.factor = 0)
    IGNORED_CLAUSE
), learning_starts as (
  select cid, max(id) as start_id
  from eligible
  where type = 0 and (previous_type is null or previous_type != 0)
  group by cid
), last_forgets as (
  select cid, max(id) as forget_id
  from revlog
  where type = 4 and factor = 0
  group by cid
), fallback_starts as (
  select e.cid as cid, min(e.id) as start_id
  from eligible e
  left join learning_starts l on l.cid = e.cid
  left join last_forgets f on f.cid = e.cid
  where l.cid is null
    and (f.forget_id is null or e.id > f.forget_id)
  group by e.cid
), retained_starts as (
  select cid, start_id from learning_starts
  union all
  select cid, start_id from fallback_starts
)
select
  e.id,
  e.cid,
  e.nid,
  e.deck_id,
  e.ease,
  e.time,
  e.type,
  e.interval_days,
  e.ease_factor,
  e.id = s.start_id
from eligible e
join retained_starts s on s.cid = e.cid
where e.id >= s.start_id
order by e.id, e.cid";

    /// One replayed row, as both reads describe it.
    type ReplayedRow = (i64, i64, i64, i64, i64, i64, i64, i64, i64, bool);

    fn replayed_rows_from_replaced_sql(
        col: &Collection,
        ignored_review_ids: &[RevlogId],
    ) -> Result<Vec<ReplayedRow>> {
        let clause = if ignored_review_ids.is_empty() {
            String::new()
        } else {
            let mut ids = String::new();
            ids_to_string(&mut ids, ignored_review_ids);
            format!("and r.id not in {ids}")
        };
        let sql = REPLACED_HISTORICAL_ROWS_SQL.replace("IGNORED_CLAUSE", &clause);
        let mut statement = col.storage.db.prepare(&sql)?;
        let mut query = statement.query([])?;
        let mut rows = Vec::new();
        while let Some(row) = query.next()? {
            rows.push((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
                row.get(5)?,
                row.get(6)?,
                row.get(7)?,
                row.get(8)?,
                row.get(9)?,
            ));
        }
        Ok(rows)
    }

    fn replayed_rows_from_one_pass_read(
        col: &Collection,
        ignored_review_ids: &[RevlogId],
    ) -> Result<Vec<ReplayedRow>> {
        let (rows, _) = col
            .storage
            .rwkv_historical_review_rows(ignored_review_ids)?;
        Ok(rows
            .into_iter()
            .map(|row| {
                (
                    row.review_id,
                    row.card_id,
                    row.note_id,
                    row.deck_id,
                    row.ease,
                    row.duration_millis,
                    row.review_kind,
                    row.interval_days,
                    row.ease_factor,
                    row.is_learning_start,
                )
            })
            .collect())
    }

    /// A repeatable pseudo-random source, so that the histories below are the
    /// same on every machine and on every run.
    struct ReplayNoise(u64);

    impl ReplayNoise {
        fn below(&mut self, limit: u64) -> u64 {
            self.0 = self
                .0
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            (self.0 >> 33) % limit
        }
    }

    fn add_replay_card_in_deck(
        col: &Collection,
        card_id: i64,
        note_id: i64,
        deck_id: i64,
        original_deck_id: i64,
    ) -> Result<()> {
        col.storage.db.execute(
            "insert into cards (id, nid, did, ord, mod, usn, type, queue, due, ivl, \
             factor, reps, lapses, left, odue, odid, flags, data) \
             values (?, ?, ?, 0, 0, -1, 2, 2, 1, 10, 2500, 0, 0, 0, 0, ?, 0, '')",
            [card_id, note_id, deck_id, original_deck_id],
        )?;
        Ok(())
    }

    /// The one-pass read must return exactly the rows the SQL it replaced
    /// returned, over histories that reach every branch of the start-row rule:
    /// learning runs, Forget cuts, Set Due Date rows, filtered reviews with and
    /// without an ease factor, cards in a filtered deck, reviews of a card that
    /// no longer exists, and ignored reviews.
    #[test]
    fn rwkv_replay_read_matches_the_query_it_replaced() -> Result<()> {
        let (col, _tempdir, _path) = temp_collection("rwkv-replay-read-equivalence")?;
        // a rated filtered review keeps its ease factor; one without an ease
        // factor is a reschedule and never enters the history
        let rated_filtered: (i64, i64, i64) = (2, 3, 2500);
        let unrated_filtered: (i64, i64, i64) = (2, 3, 0);
        let rescheduled: (i64, i64, i64) = (0, 5, 0);
        let palette = [
            RATED_LEARNING,
            RATED_REVIEW,
            RATED_RELEARNING,
            FORGET,
            SET_DUE_DATE_AS_MANUAL,
            SET_DUE_DATE_AS_RESCHEDULED,
            rated_filtered,
            unrated_filtered,
            rescheduled,
        ];

        let mut noise = ReplayNoise(20_260_921);
        let mut review_id = 1_000_000;
        let mut interesting_review_ids = Vec::new();
        for index in 0..40i64 {
            let card_id = 1_000 + index;
            // every fourth card sits in a filtered deck, so `odid` decides the
            // deck the rows report
            let original_deck_id = if index % 4 == 0 { 7 } else { 0 };
            add_replay_card_in_deck(&col, card_id, 500 + index, 1 + index % 3, original_deck_id)?;
            for _ in 0..noise.below(9) {
                review_id += 1 + noise.below(5_000) as i64;
                let row = palette[noise.below(palette.len() as u64) as usize];
                add_replay_revlog(&col, review_id, card_id, row)?;
                if noise.below(6) == 0 {
                    interesting_review_ids.push(RevlogId(review_id));
                }
            }
        }
        // reviews of a card that was deleted: no read may return them
        for _ in 0..5 {
            review_id += 1 + noise.below(5_000) as i64;
            add_replay_revlog(&col, review_id, 999_999, RATED_REVIEW)?;
        }

        let replaced = replayed_rows_from_replaced_sql(&col, &[])?;
        assert!(
            replaced.len() > 40,
            "the generated history is too small to be a test: {} rows",
            replaced.len()
        );
        assert_eq!(replayed_rows_from_one_pass_read(&col, &[])?, replaced);

        // the same, with reviews the caller asks the replay to ignore
        assert!(!interesting_review_ids.is_empty());
        for ignored in [
            &interesting_review_ids[..1],
            &interesting_review_ids[..interesting_review_ids.len().min(7)],
            &interesting_review_ids[..],
        ] {
            let mut ignored = ignored.to_vec();
            // an id that is in no review log, as a stale cache passes
            ignored.push(RevlogId(review_id + 10_000));
            ignored.sort_unstable();
            assert_eq!(
                replayed_rows_from_one_pass_read(&col, &ignored)?,
                replayed_rows_from_replaced_sql(&col, &ignored)?,
                "ignoring {} reviews",
                ignored.len()
            );
        }

        Ok(())
    }

    /// The read the stats pages make: the same columns, the same filter and
    /// the same order as `cached_review_predictions`.
    const PREDICTION_READ: &str = "select revlog_id, prediction, updated_at, fold_index, source
         from {table} where sample_role = ?1 and revlog_id > ?2 order by revlog_id";

    fn index_names(storage: &SqliteStorage, table: &str) -> Result<Vec<String>> {
        let mut statement = storage.db.prepare(
            "select name from retrievability_cache.sqlite_master
             where type = 'index' and tbl_name = ? and name not like 'sqlite_%'
             order by name",
        )?;
        let names = statement
            .query_map([table], |row| row.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        Ok(names)
    }

    fn read_plan(storage: &SqliteStorage, table: &str) -> Result<String> {
        let plan: String = storage.db.query_row(
            &format!(
                "explain query plan {}",
                PREDICTION_READ.replace("{table}", table)
            ),
            params!["final_fit", 0i64],
            |row| row.get(3),
        )?;
        Ok(plan)
    }

    #[test]
    fn the_prediction_read_uses_a_covering_index() -> Result<()> {
        let (col, _tempdir, _path) = temp_collection("prediction-read-covering-index")?;
        col.storage
            .ensure_fsrs_review_retrievability_cache_schema()?;
        col.storage
            .ensure_rwkv_review_retrievability_cache_schema()?;

        for table in [
            FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE,
            RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE,
        ] {
            let plan = read_plan(&col.storage, table)?;
            // "COVERING" is the word that says the read never touches the
            // table: without it every row costs a separate seek, which is the
            // whole point of the index (spec database.prediction-read-index)
            assert!(
                plan.contains("USING COVERING INDEX"),
                "{table} does not read from a covering index: {plan}"
            );
        }
        Ok(())
    }

    #[test]
    fn the_narrow_prediction_index_is_replaced_not_joined() -> Result<()> {
        let (col, _tempdir, _path) = temp_collection("prediction-read-one-index")?;
        col.storage
            .ensure_fsrs_review_retrievability_cache_schema()?;

        // the covering index starts with the narrow one's columns, so keeping
        // both would cost disk and buy nothing
        assert_eq!(
            index_names(&col.storage, FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE)?,
            vec!["ix_fsrs_review_retrievability_covering".to_string()]
        );
        Ok(())
    }

    #[test]
    fn a_collection_that_arrives_with_the_narrow_index_is_upgraded() -> Result<()> {
        let (col, _tempdir, _path) = temp_collection("prediction-read-upgrade")?;
        col.storage
            .ensure_fsrs_review_retrievability_cache_schema()?;

        // what an existing cache looks like: the narrow index, and no covering
        // one. The next open must replace it, once.
        col.storage.db.execute_batch(
            "drop index retrievability_cache.ix_fsrs_review_retrievability_covering;
             create index retrievability_cache.ix_fsrs_review_retrievability_role_revlog
                 on search_stats_fsrs_review_retrievability (sample_role, revlog_id);",
        )?;
        col.storage
            .ensure_fsrs_review_retrievability_cache_schema()?;

        assert_eq!(
            index_names(&col.storage, FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE)?,
            vec!["ix_fsrs_review_retrievability_covering".to_string()]
        );
        assert!(
            read_plan(&col.storage, FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE)?
                .contains("USING COVERING INDEX")
        );
        Ok(())
    }
}
