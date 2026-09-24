// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use anki_proto::ankidroid::sql_value::Data;
use anki_proto::ankidroid::DbResponse;
use anki_proto::ankidroid::DbResult as ProtoDbResult;
use anki_proto::ankidroid::SqlValue as pb_SqlValue;
use rusqlite::params_from_iter;
use rusqlite::types::FromSql;
use rusqlite::types::FromSqlError;
use rusqlite::types::ToSql;
use rusqlite::types::ToSqlOutput;
use rusqlite::types::ValueRef;
use rusqlite::OptionalExtension;
use serde::Deserialize;
use serde::Serialize;

use crate::ankidroid::db::next_sequence_number;
use crate::ankidroid::db::trim_and_cache_remaining;
use crate::prelude::*;
use crate::storage::SqliteStorage;

#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub(super) enum DbRequest {
    Query {
        sql: String,
        args: Vec<SqlValue>,
        first_row_only: bool,
    },
    Begin,
    Commit,
    Rollback,
    ExecuteMany {
        sql: String,
        args: Vec<Vec<SqlValue>>,
    },
}

#[derive(Serialize)]
#[serde(untagged)]
pub(super) enum DbResult {
    Rows(Vec<Vec<SqlValue>>),
    None,
}

#[derive(Serialize, Deserialize, Debug)]
#[serde(untagged)]
pub(crate) enum SqlValue {
    Null,
    String(String),
    Int(i64),
    Double(f64),
    Blob(Vec<u8>),
}

impl ToSql for SqlValue {
    fn to_sql(&self) -> std::result::Result<ToSqlOutput<'_>, rusqlite::Error> {
        let val = match self {
            SqlValue::Null => ValueRef::Null,
            SqlValue::String(v) => ValueRef::Text(v.as_bytes()),
            SqlValue::Int(v) => ValueRef::Integer(*v),
            SqlValue::Double(v) => ValueRef::Real(*v),
            SqlValue::Blob(v) => ValueRef::Blob(v),
        };
        Ok(ToSqlOutput::Borrowed(val))
    }
}

impl From<&SqlValue> for anki_proto::ankidroid::SqlValue {
    fn from(item: &SqlValue) -> Self {
        match item {
            SqlValue::Null => pb_SqlValue { data: Option::None },
            SqlValue::String(s) => pb_SqlValue {
                data: Some(Data::StringValue(s.to_string())),
            },
            SqlValue::Int(i) => pb_SqlValue {
                data: Some(Data::LongValue(*i)),
            },
            SqlValue::Double(d) => pb_SqlValue {
                data: Some(Data::DoubleValue(*d)),
            },
            SqlValue::Blob(b) => pb_SqlValue {
                data: Some(Data::BlobValue(b.clone())),
            },
        }
    }
}

fn row_to_proto(row: &[SqlValue]) -> anki_proto::ankidroid::Row {
    anki_proto::ankidroid::Row {
        fields: row
            .iter()
            .map(anki_proto::ankidroid::SqlValue::from)
            .collect(),
    }
}

fn rows_to_proto(rows: &[Vec<SqlValue>]) -> anki_proto::ankidroid::DbResult {
    anki_proto::ankidroid::DbResult {
        rows: rows.iter().map(|r| row_to_proto(r)).collect(),
    }
}

impl FromSql for SqlValue {
    fn column_result(value: ValueRef<'_>) -> std::result::Result<Self, FromSqlError> {
        let val = match value {
            ValueRef::Null => SqlValue::Null,
            ValueRef::Integer(i) => SqlValue::Int(i),
            ValueRef::Real(v) => SqlValue::Double(v),
            ValueRef::Text(v) => SqlValue::String(String::from_utf8_lossy(v).to_string()),
            ValueRef::Blob(v) => SqlValue::Blob(v.to_vec()),
        };
        Ok(val)
    }
}

pub(crate) fn db_command_bytes(col: &mut Collection, input: &[u8]) -> Result<Vec<u8>> {
    serde_json::to_vec(&db_command_bytes_inner(col, input)?).map_err(Into::into)
}

pub(super) fn db_command_bytes_inner(col: &mut Collection, input: &[u8]) -> Result<DbResult> {
    let req: DbRequest = serde_json::from_slice(input)?;
    let resp = match req {
        DbRequest::Query {
            sql,
            args,
            first_row_only,
        } => {
            update_state_after_modification(col, &sql)?;
            if first_row_only {
                db_query_row(&col.storage, &sql, &args)?
            } else {
                db_query(&col.storage, &sql, &args)?
            }
        }
        DbRequest::Begin => {
            col.storage.begin_trx()?;
            DbResult::None
        }
        DbRequest::Commit => {
            if col.state.modified_by_dbproxy {
                col.storage.set_modified_time(TimestampMillis::now())?;
                col.state.modified_by_dbproxy = false;
            }
            col.storage.commit_trx()?;
            DbResult::None
        }
        DbRequest::Rollback => {
            col.clear_caches();
            col.storage.rollback_trx()?;
            DbResult::None
        }
        DbRequest::ExecuteMany { sql, args } => {
            update_state_after_modification(col, &sql)?;
            db_execute_many(&col.storage, &sql, &args)?
        }
    };
    Ok(resp)
}

fn update_state_after_modification(col: &mut Collection, sql: &str) -> Result<()> {
    if !is_read_only(&col.storage, sql)? {
        // println!("clearing undo+study due to {}", sql);
        col.update_state_after_dbproxy_modification();
    }
    Ok(())
}

/// True if the statement only reads: SQLite reports that it changes nothing
/// in the database (`sqlite3_stmt_readonly`), and it returns rows. The second
/// half keeps the statements SQLite also calls read-only but that change what
/// the collection holds or sees (BEGIN, COMMIT, ROLLBACK, SAVEPOINT, RELEASE,
/// ATTACH, DETACH): none of them returns rows. The text of the statement is
/// not looked at, so `WITH ... SELECT` is a read and `WITH ... DELETE` is a
/// write (spec database.dbproxy-read-only).
///
/// The statement goes into the connection's statement cache, where the query
/// that follows finds it, so this adds no second prepare.
fn is_read_only(storage: &SqliteStorage, sql: &str) -> Result<bool> {
    let stmt = storage.db.prepare_cached(sql)?;
    Ok(stmt.readonly() && stmt.column_count() > 0)
}

pub(crate) fn db_command_proto(col: &mut Collection, input: &[u8]) -> Result<DbResponse> {
    let result = db_command_bytes_inner(col, input)?;
    let proto_resp = match result {
        DbResult::None => ProtoDbResult { rows: Vec::new() },
        DbResult::Rows(rows) => rows_to_proto(&rows),
    };
    let trimmed = trim_and_cache_remaining(col, proto_resp, next_sequence_number());
    Ok(trimmed)
}

pub(super) fn db_query_row(ctx: &SqliteStorage, sql: &str, args: &[SqlValue]) -> Result<DbResult> {
    let mut stmt = ctx.db.prepare_cached(sql)?;
    let columns = stmt.column_count();

    let row = stmt
        .query_row(params_from_iter(args), |row| {
            let mut orow = Vec::with_capacity(columns);
            for i in 0..columns {
                let v: SqlValue = row.get(i)?;
                orow.push(v);
            }
            Ok(orow)
        })
        .optional()?;

    let rows = if let Some(row) = row {
        vec![row]
    } else {
        vec![]
    };

    Ok(DbResult::Rows(rows))
}

pub(super) fn db_query(ctx: &SqliteStorage, sql: &str, args: &[SqlValue]) -> Result<DbResult> {
    let mut stmt = ctx.db.prepare_cached(sql)?;
    let columns = stmt.column_count();

    let res: std::result::Result<Vec<Vec<_>>, rusqlite::Error> = stmt
        .query_map(params_from_iter(args), |row| {
            let mut orow = Vec::with_capacity(columns);
            for i in 0..columns {
                let v: SqlValue = row.get(i)?;
                orow.push(v);
            }
            Ok(orow)
        })?
        .collect();

    Ok(DbResult::Rows(res?))
}

pub(super) fn db_execute_many(
    ctx: &SqliteStorage,
    sql: &str,
    args: &[Vec<SqlValue>],
) -> Result<DbResult> {
    let mut stmt = ctx.db.prepare_cached(sql)?;

    for params in args {
        stmt.execute(params_from_iter(params))?;
    }

    Ok(DbResult::None)
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::collection::CollectionBuilder;

    /// A collection with one undoable step (Add Note) and a built study queue.
    fn collection_with_undo_step_and_queue() -> Result<Collection> {
        let mut col = CollectionBuilder::default().build()?;
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "front")?;
        col.add_note(&mut note, DeckId(1))?;
        col.get_queued_cards(1, false, true)?;
        assert_eq!(col.undo_status().undo, Some(Op::AddNote));
        assert!(col.state.card_queues.is_some());
        Ok(col)
    }

    fn run_query(col: &mut Collection, sql: &str) -> Result<()> {
        let request = serde_json::json!({
            "kind": "query",
            "sql": sql,
            "args": [],
            "first_row_only": false,
        });
        db_command_bytes(col, request.to_string().as_bytes())?;
        Ok(())
    }

    /// True if the statement left the undo step, the study queue and the
    /// collection's modified flag as they were.
    fn kept_as_a_read(sql: &str) -> Result<bool> {
        let mut col = collection_with_undo_step_and_queue()?;
        run_query(&mut col, sql)?;
        let undo_kept = col.undo_status().undo == Some(Op::AddNote);
        let queue_kept = col.state.card_queues.is_some();
        let unmodified = !col.state.modified_by_dbproxy;
        assert_eq!(undo_kept, queue_kept, "{sql}");
        assert_eq!(undo_kept, unmodified, "{sql}");
        Ok(undo_kept)
    }

    // Pins spec/database.md#database.dbproxy-read-only.
    #[test]
    fn a_read_keeps_the_undo_step_whatever_its_first_word() -> Result<()> {
        for sql in [
            "select count() from cards",
            "  SELECT id FROM notes",
            "with x as (select id from cards) select count() from x",
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3) \
             SELECT i FROM n",
            "pragma table_info(cards)",
            "values (1), (2)",
        ] {
            assert!(kept_as_a_read(sql)?, "{sql}");
        }
        Ok(())
    }

    // Pins spec/database.md#database.dbproxy-read-only.
    #[test]
    fn a_write_drops_the_undo_step_whatever_its_first_word() -> Result<()> {
        for sql in [
            "update cards set mod = mod",
            "delete from graves where 0",
            "insert into graves (oid, type, usn) select 0, 0, 0 where 0",
            "with x as (select id from cards) delete from cards where id in x and 0",
            "with x as (select 1) update col set mod = mod",
            "WITH x AS (SELECT 0 AS v) INSERT INTO graves (oid, type, usn) \
             SELECT v, v, v FROM x WHERE 0",
            "insert into graves (oid, type, usn) values (0, 0, 0) returning oid",
            // read-only to SQLite, but changes what the connection sees
            "savepoint dbproxy_test",
        ] {
            assert!(!kept_as_a_read(sql)?, "{sql}");
        }
        Ok(())
    }

    // Pins spec/database.md#database.dbproxy-read-only: `executemany` is
    // classified the same way (it cannot run a statement that returns rows).
    #[test]
    fn execute_many_of_a_with_write_drops_the_undo_step() -> Result<()> {
        let mut col = collection_with_undo_step_and_queue()?;
        let request = serde_json::json!({
            "kind": "executemany",
            "sql": "with x as (select ? as v) delete from graves where oid in x and 0",
            "args": [[1], [2]],
        });
        db_command_bytes(&mut col, request.to_string().as_bytes())?;
        assert_eq!(col.undo_status().undo, None);
        assert!(col.state.card_queues.is_none());
        Ok(())
    }

    // Pins spec/database.md#database.dbproxy-read-only: after an answer, the
    // RWKV history read of the answered card (`with eligible as ...`) leaves
    // "Undo Answer Card" and the queue the answer updated in place.
    #[test]
    fn a_history_read_after_an_answer_keeps_undo_answer_card() -> Result<()> {
        let mut col = CollectionBuilder::default().build()?;
        let nt = col.get_notetype_by_name("Basic")?.unwrap();
        for front in ["one", "two"] {
            let mut note = nt.new_note();
            note.set_field(0, front)?;
            col.add_note(&mut note, DeckId(1))?;
        }
        col.answer_good();
        assert!(col.state.card_queues.is_some());
        run_query(
            &mut col,
            "with eligible as (select id, cid from revlog) select id, cid from eligible",
        )?;
        assert_eq!(col.undo_status().undo, Some(Op::AnswerCard));
        assert!(col.state.card_queues.is_some());
        Ok(())
    }

    // A statement that cannot be prepared changes nothing and returns the
    // error; the undo step stays.
    #[test]
    fn a_statement_that_fails_to_prepare_returns_the_error() -> Result<()> {
        let mut col = collection_with_undo_step_and_queue()?;
        assert!(run_query(&mut col, "selec nothing").is_err());
        assert_eq!(col.undo_status().undo, Some(Op::AddNote));
        Ok(())
    }
}
