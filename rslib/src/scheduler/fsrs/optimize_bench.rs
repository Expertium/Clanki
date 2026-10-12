// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! A measurement harness, not a test: the time of an FSRS-7 optimization on
//! a COPY of a collection, as the app runs it. Run
//! `cargo test -p anki --release --lib bench_fsrs_optimize -- --ignored
//! --nocapture` with `ANKI_OPTIMIZE_BENCH_COL` set to the copy and
//! `ANKI_OPTIMIZE_BENCH_MODE` set to one of:
//! - `setup`: make every preset due for the automatic optimization (writes the
//!   copy; run it once, before the timed runs);
//! - `preset`: one preset (`ANKI_BENCH_PRESET`), read and trained as the Deck
//!   Options optimize does;
//! - `auto`: the same preset through the automatic optimization's job (read in
//!   parts, then trained); the save is not timed;
//! - `all`: every preset with reviews, trained together as "Optimize All
//!   Presets" does, while a probe thread measures how long a small fixed piece
//!   of work waits (the UI thread's view of the machine);
//! - `params`: every preset's trained and kept parameters, with their log loss
//!   on the preset's reviews, and the log loss and RMSE(bins) of a 5-split
//!   time-series evaluation (trained on earlier reviews, tested on later ones).
//!
//! With `ANKI_BENCH_FROM_DEFAULTS` set, every preset starts from the default
//! parameters, so that the trained parameters are the ones kept.
//!
//! Each run prints one `BENCH {json}` line.

use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;
use std::time::Instant;

use fsrs::compute_parameters;
use fsrs::evaluate_with_time_series_splits;
use fsrs::ComputeParametersInput;
use fsrs::ComputeParametersVersion;
use fsrs::FSRS;

use crate::collection::CollectionBuilder;
use crate::deckconfig::effective_fsrs7_params;
use crate::prelude::*;
use crate::scheduler::fsrs::auto_optimize::fsrs_auto_optimize_job_in_parts;
use crate::scheduler::fsrs::auto_optimize::AUTO_OPTIMIZE_READ_PART_CARDS;
use crate::scheduler::fsrs::batch::ComputeParamsBatchInput;
use crate::scheduler::fsrs::params::compute_params_from_prepared;
use crate::scheduler::fsrs::params::fsrs_optimizer_search;
use crate::scheduler::fsrs::params::ignore_revlogs_before_ms_from_config;
use crate::scheduler::fsrs::params::PrepareComputeParamsInput;
use crate::scheduler::fsrs::params::PreparedComputeParams;

#[repr(C)]
#[derive(Default)]
struct FileTime {
    low: u32,
    high: u32,
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn GetCurrentProcess() -> isize;
    fn GetCurrentThread() -> isize;
    fn GetProcessTimes(
        h: isize,
        created: *mut FileTime,
        exited: *mut FileTime,
        kernel: *mut FileTime,
        user: *mut FileTime,
    ) -> i32;
    fn GetThreadTimes(
        h: isize,
        created: *mut FileTime,
        exited: *mut FileTime,
        kernel: *mut FileTime,
        user: *mut FileTime,
    ) -> i32;
}

fn ms(time: &FileTime) -> f64 {
    ((u64::from(time.high) << 32) | u64::from(time.low)) as f64 / 10_000.0
}

/// CPU time (user + kernel) of the process so far, in ms.
fn process_cpu_ms() -> f64 {
    let (mut c, mut e, mut k, mut u) = Default::default();
    unsafe { GetProcessTimes(GetCurrentProcess(), &mut c, &mut e, &mut k, &mut u) };
    ms(&k) + ms(&u)
}

/// CPU time (user + kernel) of the calling thread so far, in ms.
fn thread_cpu_ms() -> f64 {
    let (mut c, mut e, mut k, mut u) = Default::default();
    unsafe { GetThreadTimes(GetCurrentThread(), &mut c, &mut e, &mut k, &mut u) };
    ms(&k) + ms(&u)
}

fn elapsed_ms(at: Instant) -> f64 {
    at.elapsed().as_secs_f64() * 1000.0
}

/// A fixed piece of CPU work, about 1 ms on an idle core.
fn probe_work() -> f64 {
    let mut x = 1.000_1f64;
    for i in 0..400_000 {
        x = (x * 1.000_000_1 + (i & 7) as f64 * 1e-9).sqrt() + 0.5;
    }
    x
}

fn percentile(sorted: &[f64], p: f64) -> f64 {
    if sorted.is_empty() {
        return f64::NAN;
    }
    sorted[((sorted.len() - 1) as f64 * p).round() as usize]
}

fn prepare(col: &mut Collection, config: &DeckConfig) -> Result<PreparedComputeParams> {
    let search = fsrs_optimizer_search(config)?;
    let current_params = if std::env::var("ANKI_BENCH_FROM_DEFAULTS").is_ok() {
        fsrs::DEFAULT_PARAMETERS.to_vec()
    } else {
        effective_fsrs7_params(config.fsrs_params()).to_vec()
    };
    col.prepare_compute_params(PrepareComputeParamsInput {
        search: &search,
        ignore_revlogs_before: ignore_revlogs_before_ms_from_config(config)?,
        current_params: &current_params,
        num_of_relearning_steps: config.inner.relearn_steps.len(),
        enable_scheduling_penalties: true,
    })
}

fn log_loss(params: &[f32], prepared: &PreparedComputeParams) -> f64 {
    FSRS::new(params)
        .and_then(|fsrs| {
            fsrs.evaluate_with_card_ids(
                prepared.items.clone(),
                prepared.item_card_ids.clone(),
                |_| true,
            )
        })
        .map(|eval| eval.log_loss as f64)
        .unwrap_or(f64::NAN)
}

#[test]
#[ignore]
fn bench_fsrs_optimize() -> Result<()> {
    let path = std::env::var("ANKI_OPTIMIZE_BENCH_COL")
        .expect("set ANKI_OPTIMIZE_BENCH_COL to a copy of a collection");
    let mode = std::env::var("ANKI_OPTIMIZE_BENCH_MODE").expect("set ANKI_OPTIMIZE_BENCH_MODE");
    let preset = || {
        DeckConfigId(
            std::env::var("ANKI_BENCH_PRESET")
                .expect("set ANKI_BENCH_PRESET")
                .parse()
                .unwrap(),
        )
    };
    let mut col = CollectionBuilder::new(path).build()?;
    let line = match mode.as_str() {
        "setup" => {
            let mut due = 0;
            for mut config in col.storage.all_deck_config()? {
                config.inner.fsrs_last_optimized_day = Some(1);
                col.storage.update_deck_conf(&config)?;
                due += 1;
            }
            serde_json::json!({ "mode": mode, "presets_made_due": due })
        }
        "preset" => {
            let config = col.storage.get_deck_config(preset())?.unwrap();
            let (wall, cpu) = (Instant::now(), process_cpu_ms());
            let prepared = prepare(&mut col, &config)?;
            let read_ms = elapsed_ms(wall);
            let items = prepared.items.len();
            let response = compute_params_from_prepared(prepared, None, false)?;
            serde_json::json!({
                "mode": mode, "preset": config.name, "items": items,
                "wall_ms": elapsed_ms(wall), "read_ms": read_ms,
                "cpu_ms": process_cpu_ms() - cpu,
                "params_hash": format!("{:016x}", super::params_fingerprint(&response.params)),
            })
        }
        "auto" => {
            let (wall, cpu) = (Instant::now(), process_cpu_ms());
            let job = fsrs_auto_optimize_job_in_parts(
                preset(),
                AUTO_OPTIMIZE_READ_PART_CARDS,
                &mut |step| step(&mut col),
            )?
            .expect("the preset is due: run the setup mode first");
            let read_ms = elapsed_ms(wall);
            let (_key, params, items) = job.params()?;
            serde_json::json!({
                "mode": mode, "items": items,
                "wall_ms": elapsed_ms(wall), "read_ms": read_ms,
                "cpu_ms": process_cpu_ms() - cpu,
                "params_hash": format!("{:016x}", super::params_fingerprint(&params)),
            })
        }
        "all" => {
            let configs = col.storage.all_deck_config()?;
            let (wall, cpu) = (Instant::now(), process_cpu_ms());
            let mut inputs = Vec::new();
            for (index, config) in configs.iter().enumerate() {
                if let Ok(prepared) = prepare(&mut col, config) {
                    inputs.push(ComputeParamsBatchInput {
                        index,
                        name: config.name.clone(),
                        prepared,
                    });
                }
            }
            let read_ms = elapsed_ms(wall);
            let jobs = inputs
                .iter()
                .filter(|input| input.prepared.target_counts.total_targets > 0)
                .count();
            // the probe: every 16 ms (a frame), a fixed ~1 ms piece of work;
            // its wall time is what a click on the UI thread would wait for
            let stop = Arc::new(AtomicBool::new(false));
            let probe = {
                let stop = stop.clone();
                std::thread::spawn(move || {
                    let (mut waits, mut sink) = (Vec::new(), 0.0);
                    while !stop.load(Ordering::Acquire) {
                        std::thread::sleep(Duration::from_millis(16));
                        let at = Instant::now();
                        sink += probe_work();
                        waits.push(elapsed_ms(at));
                    }
                    (waits, thread_cpu_ms(), sink)
                })
            };
            let train = Instant::now();
            let outputs = col.compute_params_batch(inputs)?;
            let train_ms = elapsed_ms(train);
            stop.store(true, Ordering::Release);
            let (mut waits, probe_cpu, _) = probe.join().unwrap();
            waits.sort_by(|a, b| a.partial_cmp(b).unwrap());
            let hashes: Vec<_> = outputs
                .iter()
                .map(|output| match &output.result {
                    Ok(response) => format!("{:016x}", super::params_fingerprint(&response.params)),
                    Err(err) => format!("error: {err}"),
                })
                .collect();
            serde_json::json!({
                "mode": mode, "jobs": jobs, "rayon_threads": rayon::current_num_threads(),
                "wall_ms": elapsed_ms(wall), "read_ms": read_ms, "train_ms": train_ms,
                "cpu_ms": process_cpu_ms() - cpu - probe_cpu,
                "probe_n": waits.len(), "probe_p50_ms": percentile(&waits, 0.5),
                "probe_p95_ms": percentile(&waits, 0.95), "probe_max_ms": percentile(&waits, 1.0),
                "params_hashes": hashes,
            })
        }
        "params" => {
            let mut presets = Vec::new();
            for config in col.storage.all_deck_config()? {
                let Ok(prepared) = prepare(&mut col, &config) else {
                    continue;
                };
                if prepared.items.is_empty() {
                    continue;
                }
                let current = prepared.current_params.clone();
                let trained = compute_parameters(ComputeParametersInput {
                    training_config: None,
                    train_set: prepared.items.clone(),
                    card_ids: Some(prepared.item_card_ids.clone()),
                    progress: None,
                    enable_short_term: true,
                    enable_sched_penalties: prepared.enable_scheduling_penalties,
                    model_version: ComputeParametersVersion::Fsrs7,
                    num_relearning_steps: Some(prepared.num_of_relearning_steps),
                })?;
                let (items, current_loss, trained_loss) = (
                    prepared.items.len(),
                    log_loss(&current, &prepared),
                    log_loss(&trained, &prepared),
                );
                let splits = evaluate_with_time_series_splits(
                    ComputeParametersInput {
                        training_config: None,
                        train_set: prepared.items.clone(),
                        card_ids: Some(prepared.item_card_ids.clone()),
                        progress: None,
                        enable_short_term: true,
                        enable_sched_penalties: prepared.enable_scheduling_penalties,
                        model_version: ComputeParametersVersion::Fsrs7,
                        num_relearning_steps: Some(prepared.num_of_relearning_steps),
                    },
                    |_| true,
                )
                .ok();
                let kept = compute_params_from_prepared(prepared, None, false)?.params;
                presets.push(serde_json::json!({
                    "preset": config.name, "items": items,
                    "current_log_loss": current_loss, "trained_log_loss": trained_loss,
                    "kept_is_trained": kept == trained,
                    "split_log_loss": splits.as_ref().map(|e| e.log_loss),
                    "split_rmse_bins": splits.as_ref().map(|e| e.rmse_bins),
                    "trained": trained, "kept": kept,
                }));
            }
            serde_json::json!({ "mode": mode, "presets": presets })
        }
        other => panic!("unknown mode {other}"),
    };
    println!("BENCH {line}");
    Ok(())
}
