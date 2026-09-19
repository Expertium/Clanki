// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

pub(crate) mod auto_optimize;
pub(crate) mod batch;
pub(crate) mod curve;
mod error;
pub mod memory_state;
pub mod params;
pub(crate) mod predictions;
pub(crate) mod preset;
pub mod rescheduler;
pub mod retention;
pub(crate) mod review_time_model;
pub mod simulator;
pub mod try_collect;

/// Historical retention is fixed (spec
/// deck-options.historical-retention-fixed). The stored `historical_retention`
/// of a preset, and the value an add-on preset or a simulator request carries,
/// are ignored in favour of this.
pub(crate) const HISTORICAL_RETENTION: f32 = 0.9;

pub(crate) fn params_fingerprint(params: &[f32]) -> u64 {
    params.iter().fold(0xcbf29ce484222325, |hash, param| {
        let hash = hash ^ u64::from(param.to_bits());
        hash.wrapping_mul(0x100000001b3)
    })
}

pub(crate) fn round_to_two_decimals(value: f32) -> f32 {
    (value * 100.0).round() / 100.0
}
