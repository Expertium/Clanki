// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

#![cfg(test)]

use std::collections::HashMap;
use std::future::Future;
use std::sync::LazyLock;

use anki_proto::sync::sync_status_response;
use axum::http::StatusCode;
use fsrs::DEFAULT_PARAMETERS;
use reqwest::Client;
use reqwest::Url;
use serde_json::json;
use tempfile::tempdir;
use tempfile::TempDir;
use tokio::sync::Mutex;
use tokio::sync::MutexGuard;
use tracing::Instrument;
use tracing::Span;
use wiremock::matchers::method;
use wiremock::matchers::path;
use wiremock::Mock;
use wiremock::MockServer;
use wiremock::ResponseTemplate;

use crate::card::CardQueue;
use crate::card::CardType;
use crate::card::FsrsMemoryState;
use crate::collection::Collection;
use crate::collection::CollectionBuilder;
use crate::config::BoolKey;
use crate::deckconfig::algorithm::AlgorithmChangeSource;
use crate::deckconfig::algorithm::SchedulingAlgorithm;
use crate::deckconfig::DeckConfig;
use crate::deckconfig::FsrsVersion;
use crate::decks::DeckKind;
use crate::decks::NativeDeckName;
use crate::error::SyncError;
use crate::error::SyncErrorKind;
use crate::log::set_global_logger;
use crate::notetype::all_stock_notetypes;
use crate::ops::Op;
use crate::prelude::*;
use crate::revlog::RevlogEntry;
use crate::revlog::RevlogId;
use crate::revlog::RevlogReviewKind;
use crate::scheduler::fsrs::memory_state::get_decay_from_params;
use crate::scheduler::fsrs::memory_state::UpdateMemoryStateEntry;
use crate::scheduler::fsrs::memory_state::UpdateMemoryStateRequest;
use crate::scheduler::fsrs::params::ignore_revlogs_before_ms_from_config;
use crate::search::SearchNode;
use crate::search::SortMode;
use crate::sync::collection::graves::ApplyGravesRequest;
use crate::sync::collection::meta::MetaRequest;
use crate::sync::collection::normal::AlgorithmChangedBySync;
use crate::sync::collection::normal::NormalSyncer;
use crate::sync::collection::normal::SyncActionRequired;
use crate::sync::collection::normal::SyncOutput;
use crate::sync::collection::protocol::EmptyInput;
use crate::sync::collection::protocol::SyncProtocol;
use crate::sync::collection::start::StartRequest;
use crate::sync::collection::status::online_sync_status_check;
use crate::sync::collection::upload::UploadResponse;
use crate::sync::collection::upload::CORRUPT_MESSAGE;
use crate::sync::http_client::HttpSyncClient;
use crate::sync::http_server::default_ip_header;
use crate::sync::http_server::SimpleServer;
use crate::sync::http_server::SyncServerConfig;
use crate::sync::login::HostKeyRequest;
use crate::sync::login::SyncAuth;
use crate::sync::request::IntoSyncRequest;

const FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE: &str = "search_stats_fsrs_review_retrievability";
const RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE: &str = "search_stats_rwkv_review_retrievability";

struct TestAuth {
    username: String,
    password: String,
    host_key: String,
}

static AUTH: LazyLock<TestAuth> = LazyLock::new(|| {
    if let Ok(auth) = std::env::var("TEST_AUTH") {
        let mut auth = auth.split(':');
        TestAuth {
            username: auth.next().unwrap().into(),
            password: auth.next().unwrap().into(),
            host_key: auth.next().unwrap().into(),
        }
    } else {
        TestAuth {
            username: "user".to_string(),
            password: "pass".to_string(),
            host_key: "b2619aa1529dfdc4248e6edbf3c1b2a2b014cf6d".to_string(),
        }
    }
});

pub(in crate::sync) async fn with_active_server<F, O>(op: F) -> Result<()>
where
    F: FnOnce(HttpSyncClient) -> O,
    O: Future<Output = Result<()>>,
{
    let _ = set_global_logger(None);
    // start server
    let base_folder = tempdir()?;
    std::env::set_var("SYNC_USER1", "user:pass");
    let (addr, server_fut) = SimpleServer::make_server(SyncServerConfig {
        host: "127.0.0.1".parse().unwrap(),
        port: 0,
        base_folder: base_folder.path().into(),
        ip_header: default_ip_header(),
    })
    .await
    .unwrap();
    tokio::spawn(server_fut.instrument(Span::current()));
    // when not using ephemeral servers, tests need to be serialized
    static LOCK: LazyLock<Mutex<()>> = LazyLock::new(|| Mutex::new(()));
    let _lock: MutexGuard<()>;
    // setup client to connect to it
    let endpoint = if let Ok(endpoint) = std::env::var("TEST_ENDPOINT") {
        _lock = LOCK.lock().await;
        endpoint
    } else {
        format!("http://{addr}/")
    };
    let endpoint = Url::try_from(endpoint.as_str()).unwrap();
    let auth = SyncAuth {
        hkey: AUTH.host_key.clone(),
        endpoint: Some(endpoint),
        io_timeout_secs: None,
    };
    let client = HttpSyncClient::new(auth, Client::new());
    op(client).await
}

fn unwrap_sync_err_kind(err: AnkiError) -> SyncErrorKind {
    let AnkiError::SyncError {
        source: SyncError { kind, .. },
    } = err
    else {
        panic!("not sync err: {err:?}");
    };
    kind
}

fn main_table_exists(col: &Collection, table: &str) -> Result<bool> {
    col.storage
        .db
        .prepare("SELECT null FROM main.sqlite_master WHERE type = 'table' AND name = ?")?
        .exists([table])
        .map_err(Into::into)
}

fn add_legacy_retrievability_cache_tables(col: &Collection) -> Result<()> {
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
    Ok(())
}

#[tokio::test]
async fn host_key() -> Result<()> {
    with_active_server(|mut client| async move {
        let err = client
            .host_key(
                HostKeyRequest {
                    username: "bad".to_string(),
                    password: "bad".to_string(),
                }
                .try_into_sync_request()?,
            )
            .await
            .unwrap_err();
        assert_eq!(err.code, StatusCode::FORBIDDEN);
        assert_eq!(
            unwrap_sync_err_kind(AnkiError::from(err)),
            SyncErrorKind::AuthFailed
        );
        // hkey should be automatically set after successful login
        client.sync_key = String::new();
        let resp = client
            .host_key(
                HostKeyRequest {
                    username: AUTH.username.clone(),
                    password: AUTH.password.clone(),
                }
                .try_into_sync_request()?,
            )
            .await?
            .json()?;
        assert_eq!(resp.key, *AUTH.host_key);
        Ok(())
    })
    .await
}

#[tokio::test]
async fn meta() -> Result<()> {
    with_active_server(|client| async move {
        // unsupported sync version
        assert_eq!(
            SyncProtocol::meta(
                &client,
                MetaRequest {
                    sync_version: 0,
                    client_version: "".to_string(),
                }
                .try_into_sync_request()?,
            )
            .await
            .unwrap_err()
            .code,
            StatusCode::NOT_IMPLEMENTED
        );

        Ok(())
    })
    .await
}

#[tokio::test]
async fn aborting_is_idempotent() -> Result<()> {
    with_active_server(|mut client| async move {
        // abort is a no-op if no sync in progress
        client.abort(EmptyInput::request()).await?;

        // start a sync
        let _graves = client
            .start(
                StartRequest {
                    client_usn: Default::default(),
                    local_is_newer: false,
                    deprecated_client_graves: None,
                }
                .try_into_sync_request()?,
            )
            .await?;

        // an abort request with the wrong key is ignored
        let orig_key = client.skey().to_string();
        client.set_skey("aabbccdd".into());
        client.abort(EmptyInput::request()).await?;

        // it should succeed with the correct key
        client.set_skey(orig_key);
        client.abort(EmptyInput::request()).await?;
        Ok(())
    })
    .await
}

#[tokio::test]
async fn new_syncs_cancel_old_ones() -> Result<()> {
    with_active_server(|mut client| async move {
        let ctx = SyncTestContext::new(client.clone());

        // start a sync
        let req = StartRequest {
            client_usn: Default::default(),
            local_is_newer: false,
            deprecated_client_graves: None,
        }
        .try_into_sync_request()?;
        let _ = client.start(req.clone()).await?;

        // a new sync aborts the previous one
        let orig_key = client.skey().to_string();
        client.set_skey("1".into());
        let _ = client.start(req.clone()).await?;

        // old sync can no longer proceed
        client.set_skey(orig_key);
        let graves_req = ApplyGravesRequest::default().try_into_sync_request()?;
        assert_eq!(
            client
                .apply_graves(graves_req.clone())
                .await
                .unwrap_err()
                .code,
            StatusCode::CONFLICT
        );

        // with the correct key, it can continue
        client.set_skey("1".into());
        client.apply_graves(graves_req.clone()).await?;
        // but a full upload will break the lock
        ctx.full_upload(ctx.col1()).await;
        assert_eq!(
            client
                .apply_graves(graves_req.clone())
                .await
                .unwrap_err()
                .code,
            StatusCode::CONFLICT
        );

        // likewise with download
        let _ = client.start(req.clone()).await?;
        ctx.full_download(ctx.col1()).await;
        assert_eq!(
            client
                .apply_graves(graves_req.clone())
                .await
                .unwrap_err()
                .code,
            StatusCode::CONFLICT
        );

        Ok(())
    })
    .await
}

#[tokio::test]
async fn sync_roundtrip() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        upload_download(&ctx).await?;
        regular_sync(&ctx).await?;
        Ok(())
    })
    .await
}

/// See issue #5109
#[tokio::test]
async fn new_empty_collection_should_not_require_full_sync() -> Result<()> {
    with_active_server(|client: HttpSyncClient| async move {
        let ctx = SyncTestContext::new(client);
        let mut col1 = ctx.col1();

        // a new collection reports NoChanges, deferring to the online check
        assert_eq!(
            col1.sync_status_offline()?,
            sync_status_response::Required::NoChanges
        );

        // the online check agrees when the remote account is also new
        let state = online_sync_status_check(col1.sync_meta()?, &mut ctx.cloned_client()).await?;
        assert_eq!(state.required, SyncActionRequired::NoChanges);

        // syncing is a no-op that doesn't stamp last_sync, and the status
        // must remain NoChanges afterwards
        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        assert_eq!(
            col1.sync_status_offline()?,
            sync_status_response::Required::NoChanges
        );

        // once local changes exist, the online check picks them up, even
        // though the offline check still defers
        col1_setup(&mut col1);
        assert_eq!(
            col1.sync_status_offline()?,
            sync_status_response::Required::NoChanges
        );
        let state = online_sync_status_check(col1.sync_meta()?, &mut ctx.cloned_client()).await?;
        assert!(matches!(
            state.required,
            SyncActionRequired::FullSyncRequired { .. }
        ));

        Ok(())
    })
    .await
}

#[tokio::test]
async fn check_database_legacy_retrievability_cache_cleanup_reaches_server() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        upload_download(&ctx).await?;

        let mut col1 = ctx.col1();
        add_legacy_retrievability_cache_tables(&col1)?;
        col1.check_database()?;

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(
            out.required,
            SyncActionRequired::FullSyncRequired {
                upload_ok: true,
                download_ok: true,
            }
        );
        ctx.full_upload(col1).await;

        let col2 = ctx.col2();
        ctx.full_download(col2).await;

        let col2 = ctx.col2();
        assert!(!main_table_exists(
            &col2,
            FSRS_REVIEW_RETRIEVABILITY_CACHE_TABLE
        )?);
        assert!(!main_table_exists(
            &col2,
            RWKV_REVIEW_RETRIEVABILITY_CACHE_TABLE
        )?);

        Ok(())
    })
    .await
}

// FSRS reconciliation after a normal sync (spec/sync.md)
/////////////////////

/// Full-sync `col1` up and `col2` down so both start from the same state.
async fn sync_fsrs_collections(ctx: &SyncTestContext, mut col1: Collection) -> Result<()> {
    let out = ctx.normal_sync(&mut col1).await;
    assert!(matches!(
        out.required,
        SyncActionRequired::FullSyncRequired { .. }
    ));
    ctx.full_upload(col1).await;

    let mut col2 = ctx.col2();
    let out = ctx.normal_sync(&mut col2).await;
    assert_eq!(
        out.required,
        SyncActionRequired::FullSyncRequired {
            upload_ok: false,
            download_ok: true,
        }
    );
    ctx.full_download(col2).await;

    Ok(())
}

/// A new note in `deck` whose card was answered Easy once, so it is a review
/// card with FSRS memory state. Returns the card id.
fn add_reviewed_card(col: &mut Collection, field: &str, deck: DeckId) -> Result<CardId> {
    let nt = col.get_notetype_by_name("Basic")?.unwrap();
    let mut note = nt.new_note();
    note.set_field(0, field)?;
    col.add_note(&mut note, deck)?;
    col.set_current_deck(deck)?;
    Ok(col.answer_easy().card_id)
}

/// Make the card due now and answer Good, as a review on this device.
fn review_card_again(col: &mut Collection, card_id: CardId, deck: DeckId) -> Result<()> {
    col.storage
        .db
        .execute("update cards set due = 0 where id = ?", [card_id])?;
    col.set_current_deck(deck)?;
    col.clear_study_queues();
    col.answer_good();
    Ok(())
}

/// Make the card due now and answer Again, as a lapse on this device.
fn lapse_card(col: &mut Collection, card_id: CardId, deck: DeckId) -> Result<()> {
    col.storage
        .db
        .execute("update cards set due = 0 where id = ?", [card_id])?;
    col.set_current_deck(deck)?;
    col.clear_study_queues();
    col.answer_again();
    Ok(())
}

/// The reconcile rebuilds a memory state from the merged review log, whose
/// times are the moments of answering (milliseconds), while the reviewing
/// device computed its state when the card was shown. FSRS-7 takes the exact
/// elapsed time (spec sched.fsrs7-fractional-elapsed-time), so the two can
/// differ by the seconds spent on the answer: equal within 1%.
fn assert_memory_state_close(a: Option<FsrsMemoryState>, b: Option<FsrsMemoryState>) {
    let close = |x: f32, y: f32| (x - y).abs() <= 0.01 * x.abs().max(y.abs()).max(1.0);
    match (a, b) {
        (Some(a), Some(b)) => assert!(
            close(a.stability, b.stability)
                && close(a.stability_internal, b.stability_internal)
                && close(a.difficulty, b.difficulty)
                && close(
                    a.stability_fast.unwrap_or(a.stability_internal),
                    b.stability_fast.unwrap_or(b.stability_internal)
                ),
            "{a:?} vs {b:?}"
        ),
        (a, b) => assert_eq!(a, b),
    }
}

/// Recompute the FSRS data of `cards` from `config` without rescheduling, the
/// way a deck-options save with "Reschedule cards on change" off does.
fn recompute_memory_state(
    col: &mut Collection,
    config: &DeckConfig,
    deck_desired_retention: HashMap<DeckId, f32>,
    cards: &[CardId],
) -> Result<()> {
    let ignore_before = ignore_revlogs_before_ms_from_config(config)?;
    let review_fuzz_config = col.review_fuzz_config();
    let request = UpdateMemoryStateRequest {
        params: config.fsrs_params().to_vec(),
        preset_desired_retention: config.inner.desired_retention,
        historical_retention: config.inner.historical_retention,
        max_interval: config.inner.maximum_review_interval,
        review_fuzz_config,
        reschedule: false,
        deck_desired_retention,
        keep_stability: false,
    };
    let search = SearchNode::CardIds(
        cards
            .iter()
            .map(|card| card.0.to_string())
            .collect::<Vec<_>>()
            .join(","),
    );
    col.transact(Op::UpdateDeckConfig, |col| {
        col.update_memory_state(vec![UpdateMemoryStateEntry {
            req: Some(request),
            search: search.into(),
            ignore_before,
            preset_name: config.name.clone(),
            current_preset: 1,
            total_presets: 1,
        }])?;
        Ok(())
    })?;
    Ok(())
}

fn revlog_kinds(col: &Collection, card_id: CardId) -> Result<Vec<RevlogReviewKind>> {
    Ok(col
        .storage
        .get_revlog_entries_for_card(card_id)?
        .into_iter()
        .map(|entry| entry.review_kind)
        .collect())
}

fn assert_no_reschedule_rows(col: &Collection, card_id: CardId) -> Result<()> {
    let kinds = revlog_kinds(col, card_id)?;
    assert!(
        !kinds.contains(&RevlogReviewKind::Rescheduled),
        "post-sync reconcile wrote a review log row: {kinds:?}"
    );
    Ok(())
}

#[tokio::test]
async fn fsrs_stale_card_state_is_reconciled_during_sync() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;
        col1.set_config_bool(BoolKey::FsrsReschedule, true, false)?;
        let card_id = add_reviewed_card(&mut col1, "fsrs", DeckId(1))?;
        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        // col1 reviews the card; col2 then recomputes stale FSRS data with a
        // newer mtime, so col2's row wins the merge.
        std::thread::sleep(std::time::Duration::from_millis(1100));
        review_card_again(&mut col1, card_id, DeckId(1))?;
        std::thread::sleep(std::time::Duration::from_millis(1));
        let config = col2.get_deck_config(DeckConfigId(1), false)?.unwrap();
        recompute_memory_state(&mut col2, &config, HashMap::new(), &[card_id])?;

        let stale_card = col2.storage.get_card(card_id)?.unwrap();
        let reviewed_card = col1.storage.get_card(card_id)?.unwrap();
        assert!(
            stale_card.memory_state != reviewed_card.memory_state
                || stale_card.last_review_time != reviewed_card.last_review_time
                || stale_card.interval != reviewed_card.interval
                || stale_card.due != reviewed_card.due
        );

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        // the repaired row must be newer than col1's review (mtime has
        // one-second resolution) to win on the server and reach col1
        std::thread::sleep(std::time::Duration::from_millis(1100));
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        // col2 now holds the schedule the reviewing device produced, and the
        // memory state rebuilt from the merged review log.
        let reconciled_card = col2.storage.get_card(card_id)?.unwrap();
        let reviewed_card = col1.storage.get_card(card_id)?.unwrap();
        assert_memory_state_close(reconciled_card.memory_state, reviewed_card.memory_state);
        assert_eq!(
            reconciled_card.last_review_time,
            reviewed_card.last_review_time
        );
        assert_eq!(reconciled_card.interval, reviewed_card.interval);
        assert_eq!(reconciled_card.due, reviewed_card.due);

        // The reschedule wrote no review log row: col2 has exactly the two
        // real reviews that col1 has.
        assert_eq!(revlog_kinds(&col2, card_id)?, revlog_kinds(&col1, card_id)?);
        assert_eq!(revlog_kinds(&col2, card_id)?.len(), 2);
        assert_no_reschedule_rows(&col2, card_id)?;

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let synced_card = col1.storage.get_card(card_id)?.unwrap();
        assert_eq!(synced_card.memory_state, reconciled_card.memory_state);
        assert_eq!(
            synced_card.last_review_time,
            reconciled_card.last_review_time
        );
        assert_eq!(synced_card.interval, reconciled_card.interval);
        assert_eq!(synced_card.due, reconciled_card.due);
        assert_eq!(revlog_kinds(&col1, card_id)?.len(), 2);

        Ok(())
    })
    .await
}

/// Write the card's data as official Anki and AnkiDroid do: they keep `s`
/// and `d` but drop FSRS-7's `s_int` and `s_fast`. Marked for upload.
fn write_foreign_fsrs_state(
    col: &mut Collection,
    card_id: CardId,
    stability: f32,
    difficulty: f32,
) -> Result<()> {
    let data = json!({"s": stability, "d": difficulty, "dr": 0.9, "decay": 0.1542}).to_string();
    col.storage.db.execute(
        "update cards set data = ?, mod = ?, usn = -1 where id = ?",
        rusqlite::params![data, TimestampSecs::now().0, card_id],
    )?;
    Ok(())
}

fn card_data(col: &Collection, card_id: CardId) -> Result<String> {
    Ok(col
        .storage
        .db
        .query_row("select data from cards where id = ?", [card_id], |row| {
            row.get(0)
        })?)
}

/// FSRS off on this device only (the config row keeps its usn, so no sync
/// sends it): the device then repairs nothing and stands in for another
/// client.
fn turn_fsrs_off_on_this_device_only(col: &Collection) -> Result<()> {
    col.storage.db.execute(
        "update config set val = cast('false' as blob) where key = 'fsrs'",
        [],
    )?;
    assert!(!col.get_config_bool(BoolKey::Fsrs));
    Ok(())
}

// Pins spec/sync.md#sync.fsrs7-state-of-foreign-cards: a card another client
// wrote reaches this device without FSRS-7's internal stability; the same
// sync gives it its FSRS-7 memory state from the review log again and
// uploads it.
#[tokio::test]
async fn fsrs7_state_of_a_foreign_card_is_rebuilt_during_sync() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;
        let card_id = add_reviewed_card(&mut col1, "foreign", DeckId(1))?;
        let fsrs7_state = col1.storage.get_card(card_id)?.unwrap().memory_state;
        assert!(card_data(&col1, card_id)?.contains("\"s_int\""));
        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();
        turn_fsrs_off_on_this_device_only(&col2)?;
        std::thread::sleep(std::time::Duration::from_millis(1100));
        write_foreign_fsrs_state(&mut col2, card_id, 3.0, 7.5)?;
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        assert!(!card_data(&col2, card_id)?.contains("\"s_int\""));

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let repaired = col1.storage.get_card(card_id)?.unwrap();
        assert!(card_data(&col1, card_id)?.contains("\"s_int\""));
        assert_memory_state_close(repaired.memory_state, fsrs7_state);
        // no review log row, no schedule change
        assert_eq!(revlog_kinds(&col1, card_id)?.len(), 1);

        // the same sync uploaded it
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        assert!(card_data(&col2, card_id)?.contains("\"s_int\""));
        assert_eq!(
            col2.storage.get_card(card_id)?.unwrap().memory_state,
            repaired.memory_state
        );

        Ok(())
    })
    .await
}

// Pins spec/sync.md#sync.fsrs7-state-of-foreign-cards: a foreign card
// already in the collection (a full download, a restored backup) is
// repaired when the collection opens.
#[tokio::test]
async fn fsrs7_state_of_a_foreign_card_is_rebuilt_on_open() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;
        let card_id = add_reviewed_card(&mut col1, "foreign-open", DeckId(1))?;
        let fsrs7_state = col1.storage.get_card(card_id)?.unwrap().memory_state;
        write_foreign_fsrs_state(&mut col1, card_id, 3.0, 7.5)?;
        col1.close(None)?;

        let col1 = ctx.col1();
        assert!(card_data(&col1, card_id)?.contains("\"s_int\""));
        assert_memory_state_close(
            col1.storage.get_card(card_id)?.unwrap().memory_state,
            fsrs7_state,
        );

        Ok(())
    })
    .await
}

// Pins spec/sync.md#sync.global-algorithm-mirror: a preset that another
// client (one that knows only the preset flags) switched to another
// algorithm comes back to the collection's algorithm after the sync that
// brings it, and the next sync uploads it.
#[tokio::test]
async fn sync_reverts_a_preset_another_client_gave_another_algorithm() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        add_reviewed_card(&mut col1, "algorithm", DeckId(1))?;
        col1.transact_no_undo(|col| {
            col.change_scheduling_algorithm(SchedulingAlgorithm::RwkvCurve)
        })?;
        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();
        std::thread::sleep(std::time::Duration::from_millis(1100));
        let mut config = col2.get_deck_config(DeckConfigId(1), false)?.unwrap();
        SchedulingAlgorithm::Fsrs7.apply_to(&mut config.inner);
        config.set_modified(Usn(-1));
        col2.storage.update_deck_conf(&config)?;
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        let preset_algorithm = |col: &Collection| {
            SchedulingAlgorithm::of_preset(
                &col.get_deck_config(DeckConfigId(1), false)
                    .unwrap()
                    .unwrap()
                    .inner,
            )
        };
        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        assert_eq!(preset_algorithm(&col1), SchedulingAlgorithm::RwkvCurve);
        assert_eq!(
            col1.scheduling_algorithm(),
            Some(SchedulingAlgorithm::RwkvCurve)
        );

        // the next syncs upload it and bring it to the other device
        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        assert_eq!(preset_algorithm(&col2), SchedulingAlgorithm::RwkvCurve);

        Ok(())
    })
    .await
}

/// Two devices with one reviewed card, both on FSRS-7 by the user's choice
/// on the first, synced; then a pause, so later changes are newer by whole
/// seconds.
async fn two_devices_on_fsrs7(ctx: &SyncTestContext) -> Result<(Collection, Collection)> {
    let mut col1 = ctx.col1();
    add_reviewed_card(&mut col1, "algorithm", DeckId(1))?;
    col1.transact_no_undo(|col| col.change_scheduling_algorithm(SchedulingAlgorithm::Fsrs7))?;
    sync_fsrs_collections(ctx, col1).await?;
    std::thread::sleep(std::time::Duration::from_millis(1100));
    Ok((ctx.col1(), ctx.col2()))
}

/// The other device turns FSRS off (as Anki or AnkiDroid would) and syncs.
async fn turn_fsrs_off_and_sync(ctx: &SyncTestContext, col: &mut Collection) -> Result<()> {
    col.set_config_bool(BoolKey::Fsrs, false, false)?;
    let out = ctx.normal_sync(col).await;
    assert_eq!(out.required, SyncActionRequired::NoChanges);
    Ok(())
}

// Pins spec/scheduling.md#sched.no-sm2 and
// spec/sync.md#sync.algorithm-change-notice: another device turned FSRS
// off after the user chose FSRS-7, so the sync that brings it moves the
// collection to RWKV-Curve and says so.
#[tokio::test]
async fn a_sync_bringing_fsrs_off_newer_than_the_users_choice_moves_to_rwkv_curve() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let (mut col1, mut col2) = two_devices_on_fsrs7(&ctx).await?;
        turn_fsrs_off_and_sync(&ctx, &mut col2).await?;

        let out = ctx.normal_sync(&mut col1).await;

        assert_eq!(out.required, SyncActionRequired::NoChanges);
        assert!(col1.get_config_bool(BoolKey::Fsrs));
        assert_eq!(
            col1.scheduling_algorithm(),
            Some(SchedulingAlgorithm::RwkvCurve)
        );
        assert_eq!(
            out.algorithm_changed,
            Some(AlgorithmChangedBySync {
                algorithm: SchedulingAlgorithm::RwkvCurve,
                fsrs_turned_off: true,
            })
        );
        // a sync that changes nothing says nothing
        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.algorithm_changed, None);
        Ok(())
    })
    .await
}

// Pins spec/scheduling.md#sched.no-sm2 and
// spec/scheduling.md#sched.algorithm-history: the user chose an algorithm
// on this device after another device turned FSRS off, and the other
// device synced last, so its config (with its older algorithm) replaces
// this device's. The user's choice stays, with FSRS on, and the history
// keeps the entries of both devices.
#[tokio::test]
async fn a_users_choice_newer_than_another_devices_fsrs_off_stays_after_sync() -> Result<()> {
    with_active_server(|client| async move {
        for choice in [SchedulingAlgorithm::Fsrs7, SchedulingAlgorithm::RwkvInstant] {
            let ctx = SyncTestContext::new(client.clone());
            let (mut col1, mut col2) = two_devices_on_fsrs7(&ctx).await?;
            col1.transact_no_undo(|col| {
                col.change_scheduling_algorithm(SchedulingAlgorithm::RwkvCurve)
            })?;
            std::thread::sleep(std::time::Duration::from_millis(1100));

            // the other device turns FSRS off, then the user chooses here
            col2.set_config_bool(BoolKey::Fsrs, false, false)?;
            std::thread::sleep(std::time::Duration::from_millis(1100));
            col1.transact_no_undo(|col| col.change_scheduling_algorithm(choice))?;
            std::thread::sleep(std::time::Duration::from_millis(1100));
            let out = ctx.normal_sync(&mut col2).await;
            assert_eq!(out.required, SyncActionRequired::NoChanges);

            let out = ctx.normal_sync(&mut col1).await;

            assert_eq!(out.required, SyncActionRequired::NoChanges);
            assert!(col1.get_config_bool(BoolKey::Fsrs));
            assert_eq!(col1.scheduling_algorithm(), Some(choice));
            assert!(col1
                .storage
                .all_deck_config()?
                .iter()
                .all(|config| SchedulingAlgorithm::of_preset(&config.inner) == choice));
            // unchanged for the user: no notice
            assert_eq!(out.algorithm_changed, None);
            let history: Vec<_> = col1
                .scheduling_algorithm_history()
                .into_iter()
                .map(|entry| (entry.algorithm, entry.source))
                .collect();
            let mut expected = vec![
                (SchedulingAlgorithm::Fsrs7, AlgorithmChangeSource::User),
                (SchedulingAlgorithm::RwkvCurve, AlgorithmChangeSource::User),
                (choice, AlgorithmChangeSource::User),
            ];
            if choice != SchedulingAlgorithm::Fsrs7 {
                // the other device's config held FSRS-7; the sync put the
                // choice back
                expected.push((choice, AlgorithmChangeSource::Sync));
            }
            assert_eq!(history, expected);
        }
        Ok(())
    })
    .await
}

#[tokio::test]
async fn post_sync_reconcile_keeps_stale_schedule_when_reschedule_on_change_is_off() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;
        assert!(!col1.get_config_bool(BoolKey::FsrsReschedule));
        let card_id = add_reviewed_card(&mut col1, "fsrs-no-reschedule", DeckId(1))?;
        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        // a lapse on col1 always changes the schedule (a Good answer seconds
        // after the first review can keep it, with FSRS-7's exact elapsed time)
        std::thread::sleep(std::time::Duration::from_millis(1100));
        lapse_card(&mut col1, card_id, DeckId(1))?;
        std::thread::sleep(std::time::Duration::from_millis(1));
        let config = col2.get_deck_config(DeckConfigId(1), false)?.unwrap();
        recompute_memory_state(&mut col2, &config, HashMap::new(), &[card_id])?;
        let stale_card = col2.storage.get_card(card_id)?.unwrap();
        let reviewed_card = col1.storage.get_card(card_id)?.unwrap();
        assert!(
            stale_card.interval != reviewed_card.interval || stale_card.due != reviewed_card.due
        );

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        // Memory state converges on the merged history, but the schedule is
        // not touched because the user never opted into rescheduling.
        let reconciled_card = col2.storage.get_card(card_id)?.unwrap();
        assert_memory_state_close(reconciled_card.memory_state, reviewed_card.memory_state);
        assert_eq!(reconciled_card.interval, stale_card.interval);
        assert_eq!(reconciled_card.due, stale_card.due);
        assert_eq!(revlog_kinds(&col2, card_id)?.len(), 2);
        assert_no_reschedule_rows(&col2, card_id)?;

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_metadata_conflict_is_reconciled_without_rescheduling() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;
        col1.set_config_bool(BoolKey::FsrsReschedule, true, false)?;
        let card_id = add_reviewed_card(&mut col1, "fsrs-metadata", DeckId(1))?;
        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        // Recompute FSRS metadata on col1 with a different desired retention,
        // but without rescheduling. This gives us a conflict where only
        // FSRS-derived card fields should be reconciled.
        let mut deck = (*col1.get_deck(DeckId(1))?.unwrap()).clone();
        deck.normal_mut().unwrap().desired_retention = Some(0.83);
        col1.add_or_update_deck(&mut deck)?;
        let config = col1.get_deck_config(DeckConfigId(1), false)?.unwrap();
        recompute_memory_state(
            &mut col1,
            &config,
            HashMap::from([(DeckId(1), 0.83)]),
            &[card_id],
        )?;

        // Simulate the other device holding stale FSRS data while the
        // scheduling fields still match the current review history.
        let stale_card = col2.get_and_update_card(card_id, |card| {
            card.memory_state = Some(FsrsMemoryState {
                stability: card.memory_state.unwrap().stability + 1.0,
                ..card.memory_state.unwrap()
            });
            card.desired_retention = Some(0.97);
            card.decay = Some(0.12);
            Ok(())
        })?;
        let updated_card = col1.storage.get_card(card_id)?.unwrap();
        assert_eq!(stale_card.interval, updated_card.interval);
        assert_eq!(stale_card.due, updated_card.due);
        assert_ne!(stale_card.memory_state, updated_card.memory_state);
        assert_ne!(stale_card.desired_retention, updated_card.desired_retention);

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        let reconciled_card = col2.storage.get_card(card_id)?.unwrap();
        let synced_card = col1.storage.get_card(card_id)?.unwrap();
        assert_eq!(reconciled_card.memory_state, synced_card.memory_state);
        assert_eq!(
            reconciled_card.desired_retention,
            synced_card.desired_retention
        );
        assert_eq!(reconciled_card.decay, synced_card.decay);
        assert_eq!(
            reconciled_card.last_review_time,
            synced_card.last_review_time
        );
        // Because only FSRS metadata diverged, reconciliation should not
        // rewrite the schedule.
        assert_eq!(reconciled_card.interval, stale_card.interval);
        assert_eq!(reconciled_card.due, stale_card.due);
        assert_no_reschedule_rows(&col2, card_id)?;

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_itemless_card_state_is_cleared_during_sync() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;
        let nt = col1.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "fsrs-itemless")?;
        col1.add_note(&mut note, DeckId(1))?;
        let card_id = col1.search_cards(note.id, SortMode::NoOrder)?[0];

        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        // A manual-only revlog makes the card show up in the merged history
        // while still producing no FSRS item, which exercises the itemless
        // reconciliation path.
        col1.get_and_update_card(card_id, |card| {
            card.due += 7;
            Ok(())
        })?;
        col1.storage.add_revlog_entry(
            &RevlogEntry {
                id: RevlogId::new(),
                cid: card_id,
                usn: col1.usn()?,
                button_chosen: 0,
                interval: 0,
                last_interval: 0,
                ease_factor: 2500,
                taken_millis: 0,
                review_kind: RevlogReviewKind::Manual,
            },
            true,
        )?;
        col2.get_and_update_card(card_id, |card| {
            card.memory_state = Some(FsrsMemoryState {
                stability: 7.0,
                stability_internal: 7.0,
                stability_fast: None,
                difficulty: 4.0,
            });
            card.desired_retention = Some(0.72);
            card.decay = Some(0.34);
            card.last_review_time = Some(TimestampSecs(123));
            Ok(())
        })?;

        // Confirm the local side really carries stale FSRS state before sync.
        let stale_card = col2.storage.get_card(card_id)?.unwrap();
        assert!(stale_card.memory_state.is_some());
        assert!(stale_card.last_review_time.is_some());

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        let reconciled_card = col2.storage.get_card(card_id)?.unwrap();
        let config = col2.get_deck_config(DeckConfigId(1), false)?.unwrap();
        assert_eq!(reconciled_card.memory_state, None);
        assert_eq!(reconciled_card.last_review_time, None);
        assert_eq!(
            reconciled_card.desired_retention,
            Some(config.inner.desired_retention)
        );
        assert!(
            (reconciled_card.decay.unwrap() - get_decay_from_params(config.fsrs_params())).abs()
                < 0.001
        );
        // Itemless reconciliation clears FSRS-derived fields, but it does not
        // reschedule the card.
        assert_eq!(reconciled_card.due, stale_card.due);
        assert_eq!(reconciled_card.interval, stale_card.interval);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn post_sync_reconcile_keeps_agreed_memory_state_of_itemless_card() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;
        let nt = col1.get_notetype_by_name("Basic")?.unwrap();
        let mut note = nt.new_note();
        note.set_field(0, "fsrs-agreed-itemless")?;
        col1.add_note(&mut note, DeckId(1))?;
        let card_id = col1.search_cards(note.id, SortMode::NoOrder)?[0];
        // A review card with memory state but no review log rows, as an
        // import can produce. Both devices agree on it after the full sync.
        let agreed_state = FsrsMemoryState {
            stability: 12.5,
            stability_internal: 12.5,
            stability_fast: Some(3.0),
            difficulty: 6.0,
        };
        let today = col1.timing_today()?.days_elapsed as i32;
        col1.get_and_update_card(card_id, |card| {
            card.ctype = CardType::Review;
            card.queue = CardQueue::Review;
            card.interval = 10;
            card.due = today + 10;
            card.memory_state = Some(agreed_state);
            card.desired_retention = Some(0.9);
            card.decay = Some(0.2);
            card.last_review_time = Some(TimestampSecs(1_000_000));
            Ok(())
        })?;
        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        // Only the last review time differs momentarily between the devices.
        col2.get_and_update_card(card_id, |card| {
            card.last_review_time = Some(TimestampSecs(1_000_005));
            Ok(())
        })?;
        std::thread::sleep(std::time::Duration::from_millis(1100));
        col1.get_and_update_card(card_id, |card| {
            card.last_review_time = Some(TimestampSecs(1_000_009));
            Ok(())
        })?;

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        // The conflict was flagged (the row was rebuilt with the preset's
        // desired retention), but the agreed memory state survived.
        let reconciled_card = col2.storage.get_card(card_id)?.unwrap();
        let config = col2.get_deck_config(DeckConfigId(1), false)?.unwrap();
        assert_eq!(reconciled_card.memory_state, Some(agreed_state));
        assert_eq!(
            reconciled_card.desired_retention,
            Some(config.inner.desired_retention)
        );
        assert_eq!(reconciled_card.interval, 10);
        assert_eq!(reconciled_card.due, today + 10);

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let synced_card = col1.storage.get_card(card_id)?.unwrap();
        assert_eq!(synced_card.memory_state, Some(agreed_state));

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_conflicts_are_reconciled_per_preset() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;

        let mut config1 = col1.get_deck_config(DeckConfigId(1), false)?.unwrap();
        config1.inner.desired_retention = 0.81;
        config1.inner.fsrs_version = FsrsVersion::Seven as i32;
        config1.inner.fsrs_params_7 = DEFAULT_PARAMETERS.into();
        col1.add_or_update_deck_config(&mut config1)?;

        let mut config2 = DeckConfig {
            name: "fsrs second config".into(),
            ..Default::default()
        };
        config2.inner.desired_retention = 0.93;
        config2.inner.fsrs_version = FsrsVersion::Seven as i32;
        config2.inner.fsrs_params_7 = DEFAULT_PARAMETERS.into();
        // the first decay component, so the two presets store different decays
        config2.inner.fsrs_params_7[23] = 0.2567;
        col1.add_or_update_deck_config(&mut config2)?;

        let mut deck2 = col1.get_or_create_normal_deck("fsrs second deck")?;
        if let DeckKind::Normal(deck) = &mut deck2.kind {
            deck.config_id = config2.id.0;
        }
        col1.add_or_update_deck(&mut deck2)?;

        let card1 = add_reviewed_card(&mut col1, "fsrs-preset-1", DeckId(1))?;
        let card2 = add_reviewed_card(&mut col1, "fsrs-preset-2", deck2.id)?;

        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        // Recompute both cards on the source side using different presets so
        // reconciliation has to group by config instead of treating all cards
        // as if they shared one FSRS setup.
        let config1 = col1.get_deck_config(DeckConfigId(1), false)?.unwrap();
        let config2 = col1.get_deck_config(config2.id, false)?.unwrap();
        recompute_memory_state(&mut col1, &config1, HashMap::new(), &[card1])?;
        recompute_memory_state(&mut col1, &config2, HashMap::new(), &[card2])?;
        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        // Make the destination side newer so the stale cards stay local and
        // must be repaired by the post-sync reconciliation pass.
        col2.get_and_update_card(card1, |card| {
            card.memory_state = Some(FsrsMemoryState {
                stability: card.memory_state.unwrap().stability + 1.0,
                ..card.memory_state.unwrap()
            });
            card.desired_retention = Some(0.99);
            card.decay = Some(0.11);
            Ok(())
        })?;
        col2.get_and_update_card(card2, |card| {
            card.memory_state = Some(FsrsMemoryState {
                stability: card.memory_state.unwrap().stability + 2.0,
                ..card.memory_state.unwrap()
            });
            card.desired_retention = Some(0.77);
            card.decay = Some(0.31);
            Ok(())
        })?;

        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        let synced_card1 = col1.storage.get_card(card1)?.unwrap();
        let synced_card2 = col1.storage.get_card(card2)?.unwrap();
        let reconciled_card1 = col2.storage.get_card(card1)?.unwrap();
        let reconciled_card2 = col2.storage.get_card(card2)?.unwrap();
        assert_eq!(reconciled_card1.memory_state, synced_card1.memory_state);
        assert_eq!(reconciled_card2.memory_state, synced_card2.memory_state);
        assert_eq!(
            reconciled_card1.desired_retention,
            Some(config1.inner.desired_retention)
        );
        assert_eq!(
            reconciled_card2.desired_retention,
            Some(config2.inner.desired_retention)
        );
        assert!(
            (reconciled_card1.decay.unwrap() - get_decay_from_params(config1.fsrs_params())).abs()
                < 0.001
        );
        assert!(
            (reconciled_card2.decay.unwrap() - get_decay_from_params(config2.fsrs_params())).abs()
                < 0.001
        );
        assert_ne!(
            reconciled_card1.desired_retention,
            reconciled_card2.desired_retention
        );
        assert_ne!(reconciled_card1.decay, reconciled_card2.decay);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_reconciliation_uses_original_deck_for_filtered_cards() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;

        let mut home_deck = col1.get_or_create_normal_deck("fsrs home deck")?;
        home_deck.normal_mut().unwrap().desired_retention = Some(0.84);
        col1.add_or_update_deck(&mut home_deck)?;
        let card_id = add_reviewed_card(&mut col1, "fsrs-filtered-home", home_deck.id)?;

        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        // Refresh the source card so the server has a pending card change,
        // then create a newer local filtered-deck variant on the other side.
        let config = col1.get_deck_config(DeckConfigId(1), false)?.unwrap();
        recompute_memory_state(
            &mut col1,
            &config,
            HashMap::from([(home_deck.id, 0.84)]),
            &[card_id],
        )?;
        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        let mut filtered_deck = Deck::new_filtered();
        filtered_deck.name = NativeDeckName::from_native_str("fsrs filtered");
        {
            let filtered = filtered_deck.filtered_mut()?;
            filtered.reschedule = false;
            filtered.search_terms[0].search = format!("cid:{}", card_id.0);
        }
        col2.add_or_update_deck(&mut filtered_deck)?;
        assert_eq!(col2.rebuild_filtered_deck(filtered_deck.id)?.output, 1);
        let stale_card = col2.get_and_update_card(card_id, |card| {
            card.memory_state = Some(FsrsMemoryState {
                stability: card.memory_state.unwrap().stability + 1.5,
                ..card.memory_state.unwrap()
            });
            card.desired_retention = Some(0.99);
            card.decay = Some(0.12);
            Ok(())
        })?;
        assert_eq!(stale_card.deck_id, filtered_deck.id);
        assert_eq!(stale_card.original_deck_id, home_deck.id);

        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        let reconciled_card = col2.storage.get_card(card_id)?.unwrap();
        let synced_card = col1.storage.get_card(card_id)?.unwrap();
        assert_eq!(reconciled_card.deck_id, filtered_deck.id);
        assert_eq!(reconciled_card.original_deck_id, home_deck.id);
        assert_eq!(reconciled_card.memory_state, synced_card.memory_state);
        assert_eq!(reconciled_card.desired_retention, Some(0.84));
        assert_eq!(
            reconciled_card.last_review_time,
            synced_card.last_review_time
        );

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_reconciliation_respects_deck_overrides_within_one_preset() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;

        let mut shared_config = col1.get_deck_config(DeckConfigId(1), false)?.unwrap();
        shared_config.inner.desired_retention = 0.89;
        shared_config.inner.fsrs_version = FsrsVersion::Seven as i32;
        shared_config.inner.fsrs_params_7 = DEFAULT_PARAMETERS.into();
        col1.add_or_update_deck_config(&mut shared_config)?;

        let mut deck1 = col1.get_or_create_normal_deck("fsrs override deck 1")?;
        deck1.normal_mut().unwrap().desired_retention = Some(0.82);
        col1.add_or_update_deck(&mut deck1)?;

        let mut deck2 = col1.get_or_create_normal_deck("fsrs override deck 2")?;
        deck2.normal_mut().unwrap().desired_retention = Some(0.95);
        col1.add_or_update_deck(&mut deck2)?;

        let card1 = add_reviewed_card(&mut col1, "fsrs-override-1", deck1.id)?;
        let card2 = add_reviewed_card(&mut col1, "fsrs-override-2", deck2.id)?;

        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        let config = col1.get_deck_config(DeckConfigId(1), false)?.unwrap();
        // Both cards share the same preset, so reconciliation will process
        // them together and must still apply the deck-level desired retention
        // override for each home deck.
        recompute_memory_state(
            &mut col1,
            &config,
            HashMap::from([(deck1.id, 0.82), (deck2.id, 0.95)]),
            &[card1, card2],
        )?;
        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        col2.get_and_update_card(card1, |card| {
            card.memory_state = Some(FsrsMemoryState {
                stability: card.memory_state.unwrap().stability + 1.0,
                ..card.memory_state.unwrap()
            });
            card.desired_retention = Some(0.99);
            card.decay = Some(0.11);
            Ok(())
        })?;
        col2.get_and_update_card(card2, |card| {
            card.memory_state = Some(FsrsMemoryState {
                stability: card.memory_state.unwrap().stability + 1.5,
                ..card.memory_state.unwrap()
            });
            card.desired_retention = Some(0.77);
            card.decay = Some(0.31);
            Ok(())
        })?;

        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        let reconciled_card1 = col2.storage.get_card(card1)?.unwrap();
        let reconciled_card2 = col2.storage.get_card(card2)?.unwrap();
        let synced_card1 = col1.storage.get_card(card1)?.unwrap();
        let synced_card2 = col1.storage.get_card(card2)?.unwrap();
        assert_eq!(reconciled_card1.memory_state, synced_card1.memory_state);
        assert_eq!(reconciled_card2.memory_state, synced_card2.memory_state);
        assert_eq!(reconciled_card1.desired_retention, Some(0.82));
        assert_eq!(reconciled_card2.desired_retention, Some(0.95));
        assert!(
            (reconciled_card1.decay.unwrap() - get_decay_from_params(config.fsrs_params())).abs()
                < 0.001
        );
        assert!(
            (reconciled_card2.decay.unwrap() - get_decay_from_params(config.fsrs_params())).abs()
                < 0.001
        );

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_mixed_schedule_and_metadata_conflicts_reconcile_selectively() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;
        col1.set_config_bool(BoolKey::FsrsReschedule, true, false)?;

        let mut metadata_deck = col1.get_or_create_normal_deck("fsrs metadata deck")?;
        metadata_deck.normal_mut().unwrap().desired_retention = Some(0.83);
        col1.add_or_update_deck(&mut metadata_deck)?;

        let card1 = add_reviewed_card(&mut col1, "fsrs-mixed-schedule", DeckId(1))?;
        let card2 = add_reviewed_card(&mut col1, "fsrs-mixed-metadata", metadata_deck.id)?;

        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        std::thread::sleep(std::time::Duration::from_millis(1100));

        // Card 1 gets a new review on the source side, so its schedule needs
        // to be recomputed from merged history. Card 2 only gets a metadata
        // refresh using the same preset and should keep its existing schedule.
        review_card_again(&mut col1, card1, DeckId(1))?;
        let config = col1.get_deck_config(DeckConfigId(1), false)?.unwrap();
        recompute_memory_state(
            &mut col1,
            &config,
            HashMap::from([(metadata_deck.id, 0.83)]),
            &[card2],
        )?;

        let stale_card1 = col2.get_and_update_card(card1, |card| {
            card.memory_state = Some(FsrsMemoryState {
                stability: card.memory_state.unwrap().stability + 1.0,
                ..card.memory_state.unwrap()
            });
            card.interval += 3;
            card.due += 3;
            card.desired_retention = Some(0.99);
            card.decay = Some(0.11);
            Ok(())
        })?;
        let stale_card2 = col2.get_and_update_card(card2, |card| {
            card.memory_state = Some(FsrsMemoryState {
                stability: card.memory_state.unwrap().stability + 1.5,
                ..card.memory_state.unwrap()
            });
            card.desired_retention = Some(0.97);
            card.decay = Some(0.12);
            Ok(())
        })?;

        let reviewed_card = col1.storage.get_card(card1)?.unwrap();
        let refreshed_card = col1.storage.get_card(card2)?.unwrap();
        assert!(
            stale_card1.interval != reviewed_card.interval || stale_card1.due != reviewed_card.due
        );
        assert_eq!(stale_card2.interval, refreshed_card.interval);
        assert_eq!(stale_card2.due, refreshed_card.due);

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        let reconciled_card1 = col2.storage.get_card(card1)?.unwrap();
        let reconciled_card2 = col2.storage.get_card(card2)?.unwrap();
        let synced_card1 = col1.storage.get_card(card1)?.unwrap();
        let synced_card2 = col1.storage.get_card(card2)?.unwrap();
        assert_memory_state_close(reconciled_card1.memory_state, synced_card1.memory_state);
        assert_eq!(
            reconciled_card1.last_review_time,
            synced_card1.last_review_time
        );
        assert_eq!(reconciled_card1.interval, synced_card1.interval);
        assert_eq!(reconciled_card1.due, synced_card1.due);

        assert_memory_state_close(reconciled_card2.memory_state, synced_card2.memory_state);
        assert_eq!(
            reconciled_card2.desired_retention,
            synced_card2.desired_retention
        );
        assert_eq!(
            reconciled_card2.last_review_time,
            synced_card2.last_review_time
        );
        // Card 2 was only marked for metadata reconciliation, so its schedule
        // should remain untouched even though it was processed in the same
        // preset batch as card 1.
        assert_eq!(reconciled_card2.interval, stale_card2.interval);
        assert_eq!(reconciled_card2.due, stale_card2.due);
        assert_no_reschedule_rows(&col2, card1)?;
        assert_no_reschedule_rows(&col2, card2)?;

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_filtered_card_schedule_conflict_uses_original_deck() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;
        col1.set_config_bool(BoolKey::FsrsReschedule, true, false)?;

        let mut home_deck = col1.get_or_create_normal_deck("fsrs filtered schedule home")?;
        home_deck.normal_mut().unwrap().desired_retention = Some(0.84);
        col1.add_or_update_deck(&mut home_deck)?;
        let card_id = add_reviewed_card(&mut col1, "fsrs-filtered-schedule", home_deck.id)?;

        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        std::thread::sleep(std::time::Duration::from_millis(1100));
        review_card_again(&mut col1, card_id, home_deck.id)?;

        let mut filtered_deck = Deck::new_filtered();
        filtered_deck.name = NativeDeckName::from_native_str("fsrs filtered schedule");
        {
            let filtered = filtered_deck.filtered_mut()?;
            filtered.reschedule = false;
            filtered.search_terms[0].search = format!("cid:{}", card_id.0);
        }
        col2.add_or_update_deck(&mut filtered_deck)?;
        assert_eq!(col2.rebuild_filtered_deck(filtered_deck.id)?.output, 1);
        let stale_card = col2.get_and_update_card(card_id, |card| {
            card.memory_state = Some(FsrsMemoryState {
                stability: card.memory_state.unwrap().stability + 1.5,
                ..card.memory_state.unwrap()
            });
            card.interval += 3;
            card.original_due += 3;
            card.desired_retention = Some(0.99);
            card.decay = Some(0.12);
            Ok(())
        })?;
        assert_eq!(stale_card.deck_id, filtered_deck.id);
        assert_eq!(stale_card.original_deck_id, home_deck.id);

        let reviewed_card = col1.storage.get_card(card_id)?.unwrap();
        assert!(
            stale_card.interval != reviewed_card.interval
                || stale_card.original_due != reviewed_card.due
        );

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        let reconciled_card = col2.storage.get_card(card_id)?.unwrap();
        let synced_card = col1.storage.get_card(card_id)?.unwrap();
        assert_eq!(reconciled_card.deck_id, filtered_deck.id);
        assert_eq!(reconciled_card.original_deck_id, home_deck.id);
        assert_memory_state_close(reconciled_card.memory_state, synced_card.memory_state);
        assert_eq!(reconciled_card.desired_retention, Some(0.84));
        assert_eq!(reconciled_card.interval, synced_card.interval);
        assert_eq!(reconciled_card.original_due, synced_card.due);
        // The filtered deck position should remain local to the filtered deck;
        // only the home-deck schedule is updated through original_due.
        assert_eq!(reconciled_card.due, stale_card.due);
        assert_no_reschedule_rows(&col2, card_id)?;

        Ok(())
    })
    .await
}

#[tokio::test]
async fn fsrs_state_is_recomputed_from_reviews_on_both_devices() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;
        col1.set_config_bool(BoolKey::FsrsReschedule, true, false)?;
        let card_id = add_reviewed_card(&mut col1, "fsrs-dual-review", DeckId(1))?;

        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        std::thread::sleep(std::time::Duration::from_millis(1100));
        review_card_again(&mut col1, card_id, DeckId(1))?;

        std::thread::sleep(std::time::Duration::from_millis(1100));
        col2.storage
            .db
            .execute("update cards set due = 0 where id = ?", [card_id])?;
        col2.clear_study_queues();
        col2.answer_easy();

        let local_card1 = col1.storage.get_card(card_id)?.unwrap();
        let local_card2 = col2.storage.get_card(card_id)?.unwrap();
        assert!(
            local_card1.memory_state != local_card2.memory_state
                || local_card1.last_review_time != local_card2.last_review_time
                || local_card1.interval != local_card2.interval
                || local_card1.due != local_card2.due
        );
        assert_eq!(col1.storage.get_revlog_entries_for_card(card_id)?.len(), 2);
        assert_eq!(col2.storage.get_revlog_entries_for_card(card_id)?.len(), 2);

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        let merged_card = col2.storage.get_card(card_id)?.unwrap();
        let pre_converged_card = col1.storage.get_card(card_id)?.unwrap();
        assert_eq!(col2.storage.get_revlog_entries_for_card(card_id)?.len(), 3);
        assert_eq!(col1.storage.get_revlog_entries_for_card(card_id)?.len(), 2);
        // After device 2 syncs, it has seen both review streams and should no
        // longer match device 1's still-local-only card state.
        assert!(
            merged_card.memory_state != pre_converged_card.memory_state
                || merged_card.last_review_time != pre_converged_card.last_review_time
                || merged_card.interval != pre_converged_card.interval
                || merged_card.due != pre_converged_card.due
        );

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        let synced_card1 = col1.storage.get_card(card_id)?.unwrap();
        let synced_card2 = col2.storage.get_card(card_id)?.unwrap();
        assert_eq!(col1.storage.get_revlog_entries_for_card(card_id)?.len(), 3);
        assert_eq!(col2.storage.get_revlog_entries_for_card(card_id)?.len(), 3);
        assert_eq!(synced_card1.memory_state, synced_card2.memory_state);
        assert_eq!(synced_card1.last_review_time, synced_card2.last_review_time);
        assert_eq!(synced_card1.interval, synced_card2.interval);
        assert_eq!(synced_card1.due, synced_card2.due);
        assert_no_reschedule_rows(&col1, card_id)?;

        Ok(())
    })
    .await
}

#[tokio::test]
async fn post_sync_reconcile_leaves_schedule_alone_when_card_only_moved_deck() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;
        col1.set_config_bool(BoolKey::FsrsReschedule, true, false)?;
        let other_deck = col1.get_or_create_normal_deck("fsrs other deck")?;
        let card_id = add_reviewed_card(&mut col1, "fsrs-deck-move", DeckId(1))?;
        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();
        let before = col2.storage.get_card(card_id)?.unwrap();

        // col2 changes desired retention locally; col1 then moves the card to
        // another deck with a newer mtime, so the moved row wins the merge.
        col2.get_and_update_card(card_id, |card| {
            card.desired_retention = Some(0.7);
            Ok(())
        })?;
        std::thread::sleep(std::time::Duration::from_millis(1100));
        col1.set_deck(&[card_id], other_deck.id)?;
        let moved_card = col1.storage.get_card(card_id)?.unwrap();
        assert_eq!(moved_card.deck_id, other_deck.id);
        assert_eq!(moved_card.interval, before.interval);
        assert_eq!(moved_card.due, before.due);

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        let reconciled_card = col2.storage.get_card(card_id)?.unwrap();
        assert_eq!(reconciled_card.deck_id, other_deck.id);
        assert_eq!(reconciled_card.memory_state, moved_card.memory_state);
        assert_eq!(reconciled_card.interval, before.interval);
        assert_eq!(reconciled_card.due, before.due);
        assert_eq!(revlog_kinds(&col2, card_id)?, revlog_kinds(&col1, card_id)?);
        assert_no_reschedule_rows(&col2, card_id)?;

        Ok(())
    })
    .await
}

#[tokio::test]
async fn sync_does_not_unforget_a_card() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);

        let mut col1 = ctx.col1();
        col1.set_config_bool(BoolKey::Fsrs, true, false)?;
        col1.set_config_bool(BoolKey::FsrsReschedule, true, false)?;
        let card_id = add_reviewed_card(&mut col1, "fsrs-forget", DeckId(1))?;
        sync_fsrs_collections(&ctx, col1).await?;

        let mut col1 = ctx.col1();
        let mut col2 = ctx.col2();

        // col2 touches the card's FSRS data; col1 then forgets the card with a
        // newer mtime, so the forgotten row wins and the old reviews stay in
        // the merged review log behind the reset entry.
        col2.get_and_update_card(card_id, |card| {
            card.desired_retention = Some(0.7);
            Ok(())
        })?;
        std::thread::sleep(std::time::Duration::from_millis(1100));
        col1.reschedule_cards_as_new(&[card_id], true, false, false, None)?;
        let forgotten_card = col1.storage.get_card(card_id)?.unwrap();
        assert_eq!(forgotten_card.ctype, CardType::New);
        assert_eq!(forgotten_card.memory_state, None);
        // the Easy answer graduated the card from learning; the reset follows
        assert_eq!(
            revlog_kinds(&col1, card_id)?,
            vec![RevlogReviewKind::Learning, RevlogReviewKind::Manual]
        );

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        let reconciled_card = col2.storage.get_card(card_id)?.unwrap();
        assert_eq!(reconciled_card.ctype, CardType::New);
        assert_eq!(reconciled_card.queue, CardQueue::New);
        assert_eq!(reconciled_card.memory_state, None);
        assert_eq!(reconciled_card.interval, 0);
        assert_eq!(reconciled_card.due, forgotten_card.due);
        assert_eq!(
            revlog_kinds(&col2, card_id)?,
            vec![RevlogReviewKind::Learning, RevlogReviewKind::Manual]
        );

        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);
        let synced_card = col1.storage.get_card(card_id)?.unwrap();
        assert_eq!(synced_card.ctype, CardType::New);
        assert_eq!(synced_card.memory_state, None);

        Ok(())
    })
    .await
}

#[tokio::test]
async fn sanity_check_should_roll_back_and_force_full_sync() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        upload_download(&ctx).await?;

        let mut col1 = ctx.col1();

        // add a deck but don't mark it as requiring a sync, which will trigger the
        // sanity check to fail
        let mut deck = col1.get_or_create_normal_deck("unsynced deck")?;
        col1.add_or_update_deck(&mut deck)?;
        col1.storage
            .db
            .execute("update decks set usn=0 where id=?", [deck.id])?;

        // the sync should fail
        let err = NormalSyncer::new(&mut col1, ctx.cloned_client())
            .sync()
            .await
            .unwrap_err();
        assert!(matches!(
            err,
            AnkiError::SyncError {
                source: SyncError {
                    kind: SyncErrorKind::SanityCheckFailed { .. },
                    ..
                }
            }
        ));

        // the server should have rolled back
        let mut col2 = ctx.col2();
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        // and the client should have forced a one-way sync
        let out = ctx.normal_sync(&mut col1).await;
        assert_eq!(
            out.required,
            SyncActionRequired::FullSyncRequired {
                upload_ok: true,
                download_ok: true,
            }
        );

        Ok(())
    })
    .await
}

#[tokio::test]
async fn sync_errors_should_prompt_db_check() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        upload_download(&ctx).await?;

        let mut col1 = ctx.col1();

        // Add a a new notetype, and a note that uses it, but don't mark the notetype as
        // requiring a sync, which will cause the sync to fail as the note is added.
        let mut nt = all_stock_notetypes(&col1.tr).remove(0);
        nt.name = "new".into();
        col1.add_notetype(&mut nt, false)?;
        let mut note = nt.new_note();
        note.set_field(0, "test")?;
        col1.add_note(&mut note, DeckId(1))?;
        col1.storage.db.execute("update notetypes set usn=0", [])?;

        // the sync should fail
        let err = NormalSyncer::new(&mut col1, ctx.cloned_client())
            .sync()
            .await
            .unwrap_err();
        let AnkiError::SyncError {
            source: SyncError { info: _, kind },
        } = err
        else {
            panic!()
        };
        assert_eq!(kind, SyncErrorKind::DatabaseCheckRequired);

        // the server should have rolled back
        let mut col2 = ctx.col2();
        let out = ctx.normal_sync(&mut col2).await;
        assert_eq!(out.required, SyncActionRequired::NoChanges);

        // and the client should be able to sync again without a forced one-way sync
        let err = NormalSyncer::new(&mut col1, ctx.cloned_client())
            .sync()
            .await
            .unwrap_err();
        let AnkiError::SyncError {
            source: SyncError { info: _, kind },
        } = err
        else {
            panic!()
        };
        assert_eq!(kind, SyncErrorKind::DatabaseCheckRequired);

        Ok(())
    })
    .await
}

/// Old AnkiMobile versions sent grave ids as strings
#[tokio::test]
async fn string_grave_ids_are_handled() -> Result<()> {
    with_active_server(|client| async move {
        let req = json!({
            "minUsn": 0,
            "lnewer": false,
            "graves": {
                "cards": vec!["1"],
                "decks": vec!["2", "3"],
                "notes": vec!["4"],
            }
        });
        let req = serde_json::to_vec(&req)
            .unwrap()
            .try_into_sync_request()
            .unwrap();
        // should not return err 400
        client.start(req.into_output_type()).await.unwrap();
        client.abort(EmptyInput::request()).await?;
        Ok(())
    })
    .await?;
    // a missing value should be handled
    with_active_server(|client| async move {
        let req = json!({
            "minUsn": 0,
            "lnewer": false,
        });
        let req = serde_json::to_vec(&req)
            .unwrap()
            .try_into_sync_request()
            .unwrap();
        client.start(req.into_output_type()).await.unwrap();
        client.abort(EmptyInput::request()).await?;
        Ok(())
    })
    .await
}

#[tokio::test]
async fn invalid_uploads_should_be_handled() -> Result<()> {
    with_active_server(|client| async move {
        let ctx = SyncTestContext::new(client);
        let res = ctx
            .client
            .upload(b"fake data".to_vec().try_into_sync_request()?)
            .await?;
        assert_eq!(
            res.upload_response(),
            UploadResponse::Err(CORRUPT_MESSAGE.into())
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn meta_redirect_is_handled() -> Result<()> {
    with_active_server(|client| async move {
        let mock_server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/sync/meta"))
            .respond_with(
                ResponseTemplate::new(308).insert_header("location", client.endpoint.as_str()),
            )
            .mount(&mock_server)
            .await;
        // starting from in-sync state
        let mut ctx = SyncTestContext::new(client);
        upload_download(&ctx).await?;
        // add another note to trigger a normal sync
        let mut col1 = ctx.col1();
        col1_setup(&mut col1);
        // switch to bad endpoint
        let orig_url = ctx.client.endpoint.to_string();
        ctx.client.endpoint = Url::try_from(mock_server.uri().as_str()).unwrap();
        // sync should succeed
        let out = ctx.normal_sync(&mut col1).await;
        // client should have received new endpoint
        assert_eq!(out.new_endpoint, Some(orig_url));
        // client should not have tried the old endpoint more than once
        assert_eq!(mock_server.received_requests().await.unwrap().len(), 1);
        Ok(())
    })
    .await
}

pub(in crate::sync) struct SyncTestContext {
    pub folder: TempDir,
    pub client: HttpSyncClient,
}

impl SyncTestContext {
    pub fn new(client: HttpSyncClient) -> Self {
        Self {
            folder: tempdir().expect("create temp dir"),
            client,
        }
    }

    pub fn col1(&self) -> Collection {
        let base = self.folder.path();
        CollectionBuilder::new(base.join("col1.anki2"))
            .with_desktop_media_paths()
            .build()
            .unwrap()
    }

    pub fn col2(&self) -> Collection {
        let base = self.folder.path();
        CollectionBuilder::new(base.join("col2.anki2"))
            .with_desktop_media_paths()
            .build()
            .unwrap()
    }

    async fn normal_sync(&self, col: &mut Collection) -> SyncOutput {
        NormalSyncer::new(col, self.cloned_client())
            .sync()
            .await
            .unwrap()
    }

    async fn full_upload(&self, col: Collection) {
        col.full_upload_with_server(self.cloned_client())
            .await
            .unwrap()
    }

    async fn full_download(&self, col: Collection) {
        col.full_download_with_server(self.cloned_client())
            .await
            .unwrap()
    }

    fn cloned_client(&self) -> HttpSyncClient {
        self.client.clone()
    }
}

// Setup + full syncs
/////////////////////

fn col1_setup(col: &mut Collection) {
    let nt = col.get_notetype_by_name("Basic").unwrap().unwrap();
    let mut note = nt.new_note();
    note.set_field(0, "1").unwrap();
    col.add_note(&mut note, DeckId(1)).unwrap();
}

async fn upload_download(ctx: &SyncTestContext) -> Result<()> {
    let mut col1 = ctx.col1();
    col1_setup(&mut col1);

    let out = ctx.normal_sync(&mut col1).await;
    assert!(matches!(
        out.required,
        SyncActionRequired::FullSyncRequired { .. }
    ));

    ctx.full_upload(col1).await;

    // another collection
    let mut col2 = ctx.col2();

    // won't allow ankiweb clobber
    let out = ctx.normal_sync(&mut col2).await;
    assert_eq!(
        out.required,
        SyncActionRequired::FullSyncRequired {
            upload_ok: false,
            download_ok: true,
        }
    );

    // fetch so we're in sync
    ctx.full_download(col2).await;

    Ok(())
}

// Regular syncs
/////////////////////

async fn regular_sync(ctx: &SyncTestContext) -> Result<()> {
    // add a deck
    let mut col1 = ctx.col1();
    let mut col2 = ctx.col2();

    let mut deck = col1.get_or_create_normal_deck("new deck")?;

    // give it a new option group
    let mut dconf = DeckConfig {
        name: "new dconf".into(),
        ..Default::default()
    };
    dconf.inner.review_fuzz_base = None;
    dconf.inner.review_fuzz_factor_short = None;
    dconf.inner.review_fuzz_factor_mid = None;
    dconf.inner.review_fuzz_factor_long = None;
    dconf.inner.review_fuzz_enabled = None;
    col1.add_or_update_deck_config(&mut dconf)?;
    if let DeckKind::Normal(deck) = &mut deck.kind {
        deck.config_id = dconf.id.0;
    }
    col1.add_or_update_deck(&mut deck)?;

    // and a new notetype
    let mut nt = all_stock_notetypes(&col1.tr).remove(0);
    nt.name = "new".into();
    col1.add_notetype(&mut nt, false)?;

    // add another note+card+tag
    let mut note = nt.new_note();
    note.set_field(0, "2")?;
    note.tags.push("tag".into());
    col1.add_note(&mut note, deck.id)?;

    // mock revlog entry
    col1.storage.add_revlog_entry(
        &RevlogEntry {
            id: RevlogId(123),
            cid: CardId(456),
            usn: Usn(-1),
            interval: 10,
            ..Default::default()
        },
        true,
    )?;

    // config + creation
    col1.set_config("test", &"test1")?;
    // bumping this will affect 'last studied at' on decks at the moment
    // col1.storage.set_creation_stamp(TimestampSecs(12345))?;

    // and sync our changes
    let remote_meta = ctx
        .client
        .meta(MetaRequest::request())
        .await
        .unwrap()
        .json()
        .unwrap();
    let out = col1.sync_meta()?.compared_to_remote(remote_meta, None);
    assert_eq!(out.required, SyncActionRequired::NormalSyncRequired);

    let out = ctx.normal_sync(&mut col1).await;
    assert_eq!(out.required, SyncActionRequired::NoChanges);
    assert!(!out.remote_collection_changed);
    assert!(out.remote_review_ids.is_empty());
    assert!(!out.remote_non_review_collection_changed);

    // sync the other collection
    let out = ctx.normal_sync(&mut col2).await;
    assert_eq!(out.required, SyncActionRequired::NoChanges);
    assert!(out.remote_collection_changed);
    assert_eq!(out.remote_review_ids, vec![RevlogId(123)]);
    assert!(out.remote_non_review_collection_changed);

    let ntid = nt.id;
    let deckid = deck.id;
    let dconfid = dconf.id;
    let noteid = note.id;
    let cardid = col1.search_cards(note.id, SortMode::NoOrder)?[0];
    let revlogid = RevlogId(123);

    let compare_sides = |col1: &mut Collection, col2: &mut Collection| -> Result<()> {
        assert_eq!(
            col1.get_notetype(ntid)?.unwrap(),
            col2.get_notetype(ntid)?.unwrap()
        );
        assert_eq!(
            col1.get_deck(deckid)?.unwrap(),
            col2.get_deck(deckid)?.unwrap()
        );
        assert_eq!(
            col1.get_deck_config(dconfid, false)?.unwrap(),
            col2.get_deck_config(dconfid, false)?.unwrap()
        );
        assert_eq!(
            col1.storage.get_note(noteid)?.unwrap(),
            col2.storage.get_note(noteid)?.unwrap()
        );
        assert_eq!(
            col1.storage.get_card(cardid)?.unwrap(),
            col2.storage.get_card(cardid)?.unwrap()
        );
        assert_eq!(
            col1.storage.get_revlog_entry(revlogid)?,
            col2.storage.get_revlog_entry(revlogid)?,
        );
        assert_eq!(
            col1.storage.get_all_config()?,
            col2.storage.get_all_config()?
        );
        assert_eq!(
            col1.storage.creation_stamp()?,
            col2.storage.creation_stamp()?
        );

        // server doesn't send tag usns, so we can only compare tags, not usns,
        // as the usns may not match
        assert_eq!(
            col1.storage
                .all_tags()?
                .into_iter()
                .map(|t| t.name)
                .collect::<Vec<_>>(),
            col2.storage
                .all_tags()?
                .into_iter()
                .map(|t| t.name)
                .collect::<Vec<_>>()
        );
        std::thread::sleep(std::time::Duration::from_millis(1));
        Ok(())
    };

    // make sure everything has been transferred across
    compare_sides(&mut col1, &mut col2)?;

    // A review-only download is reported separately from other collection changes.
    col1.storage.add_revlog_entry(
        &RevlogEntry {
            id: RevlogId(124),
            cid: CardId(456),
            usn: Usn(-1),
            interval: 11,
            ..Default::default()
        },
        true,
    )?;
    ctx.normal_sync(&mut col1).await;
    let out = ctx.normal_sync(&mut col2).await;
    assert!(out.remote_collection_changed);
    assert_eq!(out.remote_review_ids, vec![RevlogId(124)]);
    assert!(!out.remote_non_review_collection_changed);

    // make some modifications
    let mut note = col2.storage.get_note(note.id)?.unwrap();
    note.set_field(1, "new")?;
    note.tags.push("tag2".into());
    col2.update_note(&mut note)?;

    col2.get_and_update_card(cardid, |card| {
        card.queue = CardQueue::Review;
        Ok(())
    })?;

    let mut deck = col2.storage.get_deck(deck.id)?.unwrap();
    deck.name = NativeDeckName::from_native_str("newer");
    col2.add_or_update_deck(&mut deck)?;

    let mut nt = col2.storage.get_notetype(nt.id)?.unwrap();
    nt.name = "newer".into();
    col2.update_notetype(&mut nt, false)?;

    // sync the changes back
    let out = ctx.normal_sync(&mut col2).await;
    assert_eq!(out.required, SyncActionRequired::NoChanges);
    let out = ctx.normal_sync(&mut col1).await;
    assert_eq!(out.required, SyncActionRequired::NoChanges);

    // should still match
    compare_sides(&mut col1, &mut col2)?;

    // deletions should sync too
    for table in &["cards", "notes", "decks"] {
        assert_eq!(
            col1.storage
                .db_scalar::<u8>(&format!("select count() from {table}"))?,
            2
        );
    }

    // fixme: inconsistent usn arg
    std::thread::sleep(std::time::Duration::from_millis(1));
    col1.remove_cards_and_orphaned_notes(&[cardid])?;
    let usn = col1.usn()?;
    col1.remove_note_only_undoable(noteid, usn)?;
    col1.remove_decks_and_child_decks(&[deckid])?;

    let out = ctx.normal_sync(&mut col1).await;
    assert_eq!(out.required, SyncActionRequired::NoChanges);
    let out = ctx.normal_sync(&mut col2).await;
    assert_eq!(out.required, SyncActionRequired::NoChanges);

    for table in &["cards", "notes", "decks"] {
        assert_eq!(
            col2.storage
                .db_scalar::<u8>(&format!("select count() from {table}"))?,
            1
        );
    }

    // removing things like a notetype forces a full sync
    std::thread::sleep(std::time::Duration::from_millis(1));
    col2.remove_notetype(ntid)?;
    let out = ctx.normal_sync(&mut col2).await;
    assert!(matches!(
        out.required,
        SyncActionRequired::FullSyncRequired { .. }
    ));
    Ok(())
}
