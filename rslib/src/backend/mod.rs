// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod adding;
mod ankidroid;
mod ankihub;
mod ankiweb;
mod card_rendering;
mod collection;
mod config;
pub(crate) mod dbproxy;
mod error;
mod github;
mod i18n;
mod import_export;
mod media;
mod ops;
mod sync;

use std::ops::Deref;
use std::result;
use std::sync::atomic::AtomicUsize;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::OnceLock;
use std::thread::JoinHandle;
use std::time::Duration;
use std::time::Instant;

use futures::future::AbortHandle;
use prost::Message;
use reqwest::Client;
use tokio::runtime;
use tokio::runtime::Runtime;

use crate::backend::dbproxy::db_command_bytes;
use crate::backend::sync::SyncState;
use crate::prelude::*;
use crate::progress::Progress;
use crate::progress::ProgressState;
use crate::progress::ThrottlingProgressHandler;

#[derive(Clone)]
#[repr(transparent)]
pub struct Backend(Arc<BackendInner>);

impl Deref for Backend {
    type Target = BackendInner;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

pub struct BackendInner {
    col: Mutex<Option<Collection>>,
    /// Calls of `with_col` that wait for the collection now; a step of a
    /// read in parts lets them go first (`with_col_for_a_part`).
    col_waiters: AtomicUsize,
    pub(crate) tr: I18n,
    server: bool,
    sync_abort: Mutex<Option<AbortHandle>>,
    progress_state: Arc<Mutex<ProgressState>>,
    runtime: OnceLock<Runtime>,
    state: Mutex<BackendState>,
    backup_task: Mutex<Option<JoinHandle<Result<()>>>>,
    media_sync_task: Mutex<Option<JoinHandle<Result<()>>>>,
    web_client: Mutex<Option<Client>>,
}

/// How long a step of a read in parts lets waiting calls go first at most, so
/// that a stream of calls cannot stop the read.
const MAX_WAIT_FOR_WAITING_CALLS: Duration = Duration::from_secs(1);

#[derive(Default)]
struct BackendState {
    sync: SyncState,
}

pub fn init_backend(init_msg: &[u8]) -> result::Result<Backend, String> {
    let input: anki_proto::backend::BackendInit =
        match anki_proto::backend::BackendInit::decode(init_msg) {
            Ok(req) => req,
            Err(_) => return Err("couldn't decode init request".into()),
        };

    let tr = I18n::new(&input.preferred_langs);

    Ok(Backend::new(tr, input.server))
}

impl Backend {
    pub fn new(tr: I18n, server: bool) -> Backend {
        Backend(Arc::new(BackendInner {
            col: Mutex::new(None),
            col_waiters: AtomicUsize::new(0),
            tr,
            server,
            sync_abort: Mutex::new(None),
            progress_state: Arc::new(Mutex::new(ProgressState {
                want_abort: false,
                last_progress: None,
            })),
            runtime: OnceLock::new(),
            state: Mutex::new(BackendState::default()),
            backup_task: Mutex::new(None),
            media_sync_task: Mutex::new(None),
            web_client: Mutex::new(None),
        }))
    }

    pub fn i18n(&self) -> &I18n {
        &self.tr
    }

    pub fn run_db_command_bytes(&self, input: &[u8]) -> result::Result<Vec<u8>, Vec<u8>> {
        self.db_command(input).map_err(|err| {
            let backend_err = err.into_protobuf(&self.tr);
            let mut bytes = Vec::new();
            backend_err.encode(&mut bytes).unwrap();
            bytes
        })
    }

    /// If collection is open, run the provided closure while holding
    /// the mutex.
    /// If collection is not open, return an error.
    pub(crate) fn with_col<F, T>(&self, func: F) -> Result<T>
    where
        F: FnOnce(&mut Collection) -> Result<T>,
    {
        self.col_waiters.fetch_add(1, Ordering::SeqCst);
        let guard = self.col.lock();
        self.col_waiters.fetch_sub(1, Ordering::SeqCst);
        func(
            guard
                .unwrap()
                .as_mut()
                .ok_or(AnkiError::CollectionNotOpen)?,
        )
    }

    /// `with_col` for one step of a read in parts: a `with_col` call that
    /// already waits for the collection takes it first.
    ///
    /// The parts exist so that a click waits for one part at most, but the
    /// lock is not fair: the thread that has just released it takes it
    /// again before a waiting thread wakes up, so a click waited for all
    /// the parts. A fingerprint of 656k reviews kept a call on the main
    /// thread waiting 720-740 ms, with parts of about 25 ms.
    pub(crate) fn with_col_for_a_part<F, T>(&self, func: F) -> Result<T>
    where
        F: FnOnce(&mut Collection) -> Result<T>,
    {
        let started = Instant::now();
        let mut tries = 0;
        while self.col_waiters.load(Ordering::SeqCst) > 0
            && started.elapsed() < MAX_WAIT_FOR_WAITING_CALLS
        {
            // a waiting call usually takes the collection within microseconds;
            // one that waits behind a long operation is not spun for
            if tries < 100 {
                tries += 1;
                std::thread::yield_now();
            } else {
                std::thread::sleep(Duration::from_millis(1));
            }
        }
        func(
            self.col
                .lock()
                .unwrap()
                .as_mut()
                .ok_or(AnkiError::CollectionNotOpen)?,
        )
    }

    fn runtime_handle(&self) -> runtime::Handle {
        self.runtime
            .get_or_init(|| {
                runtime::Builder::new_multi_thread()
                    .worker_threads(1)
                    .enable_all()
                    .build()
                    .unwrap()
            })
            .handle()
            .clone()
    }

    #[cfg(feature = "rustls")]
    fn set_custom_certificate_inner(&self, cert_str: String) -> Result<()> {
        use std::io::Cursor;
        use std::io::Read;

        use reqwest::Certificate;

        let mut web_client = self.web_client.lock().unwrap();

        if cert_str.is_empty() {
            let _ = web_client.insert(Client::builder().http1_only().build().unwrap());
            return Ok(());
        }

        if rustls_pemfile::read_all(Cursor::new(cert_str.as_bytes()).by_ref()).count() != 1 {
            return Err(AnkiError::InvalidCertificateFormat);
        }

        if let Ok(certificate) = Certificate::from_pem(cert_str.as_bytes()) {
            if let Ok(new_client) = Client::builder()
                .use_rustls_tls()
                .add_root_certificate(certificate)
                .http1_only()
                .build()
            {
                let _ = web_client.insert(new_client);
                return Ok(());
            }
        }

        Err(AnkiError::InvalidCertificateFormat)
    }

    fn web_client(&self) -> Client {
        // currently limited to http1, as nginx doesn't support http2 proxies
        let mut web_client = self.web_client.lock().unwrap();

        web_client
            .get_or_insert_with(|| Client::builder().http1_only().build().unwrap())
            .clone()
    }

    fn db_command(&self, input: &[u8]) -> Result<Vec<u8>> {
        self.with_col(|col| db_command_bytes(col, input))
    }

    /// Useful for operations that function with a closed collection, such as
    /// a colpkg import. For collection operations, you can use
    /// [Collection::new_progress_handler] instead.
    pub(crate) fn new_progress_handler<P: Into<Progress> + Default + Clone>(
        &self,
    ) -> ThrottlingProgressHandler<P> {
        ThrottlingProgressHandler::new(self.progress_state.clone())
    }
}

#[cfg(test)]
mod test {
    use std::thread;

    use super::*;

    /// A call that waits for the collection while a read in parts runs gets
    /// it after the part that holds it now, not after the whole read: the
    /// read's thread no longer takes the lock back before the call wakes.
    #[test]
    fn a_waiting_call_takes_the_collection_between_two_parts() -> Result<()> {
        const PARTS: usize = 40;
        let backend = Backend::new(I18n::template_only(), false);
        *backend.col.lock().unwrap() = Some(Collection::new());
        let parts_done = Arc::new(AtomicUsize::new(0));
        let read = {
            let backend = backend.clone();
            let parts_done = parts_done.clone();
            thread::spawn(move || -> Result<()> {
                for _ in 0..PARTS {
                    backend.with_col_for_a_part(|_| {
                        thread::sleep(Duration::from_millis(5));
                        parts_done.fetch_add(1, Ordering::SeqCst);
                        Ok(())
                    })?;
                }
                Ok(())
            })
        };
        while parts_done.load(Ordering::SeqCst) < 3 {
            thread::sleep(Duration::from_millis(1));
        }
        let before = parts_done.load(Ordering::SeqCst);
        let at_call = backend.with_col(|_| Ok(parts_done.load(Ordering::SeqCst)))?;
        read.join().unwrap()?;
        assert!(
            at_call - before <= 2,
            "the call waited for {} parts",
            at_call - before
        );
        assert!(at_call < PARTS);
        assert_eq!(backend.col_waiters.load(Ordering::SeqCst), 0);
        Ok(())
    }
}
