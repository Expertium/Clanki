// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The request that publishes the Stats graph's RWKV scores
//! (`RwkvStatsGraphScoresRequest`), for aqt.rwkv_scheduler's
//! `_set_rwkv_stats_graph_scores`. Python built one `Score` message per card
//! and then the request from them, ~3 us a card with the GIL held (7 ms for
//! 3,000 cards); here the request is encoded from Python's maps directly.

use anki_proto::scheduler::rwkv_stats_graph_scores_request::Score;
use anki_proto::scheduler::RwkvStatsGraphScoresRequest;
use prost::Message;
use pyo3::exceptions::PyOverflowError;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBool;
use pyo3::types::PyBytes;
use pyo3::types::PyDict;
use pyo3::types::PyFloat;
use pyo3::types::PyInt;

/// The serialized request for `search`: one score per entry of
/// `retrievabilities` (card id -> RWKV-Instant's value or None), in its
/// order, as Python's encoding built it:
/// - the value when not None;
/// - the card's `target_retentions` and `curve_retrievabilities` values when
///   they are probabilities (an int or float, not a bool, finite, in [0, 1]);
/// - its `intervening_reviews` when an int of 0 or more;
/// - `curve_due` when the card is in `curve_due_card_ids`.
///
/// Values go into the f32 fields as the protobuf library stores a Python
/// number (a double, cast). A card id outside int64, or a review count
/// outside uint32, raises ValueError, as setting the field did.
#[pyfunction]
pub(crate) fn rwkv_stats_graph_scores_request<'py>(
    py: Python<'py>,
    search: String,
    retrievabilities: &Bound<'py, PyDict>,
    target_retentions: &Bound<'py, PyDict>,
    intervening_reviews: &Bound<'py, PyDict>,
    curve_due_card_ids: &Bound<'py, PyAny>,
    curve_retrievabilities: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyBytes>> {
    let mut scores = Vec::with_capacity(retrievabilities.len());
    for (card_id, retrievability) in retrievabilities.iter() {
        let mut score = Score {
            card_id: in_range(&card_id, card_id.extract::<i64>())?,
            ..Default::default()
        };
        if !retrievability.is_none() {
            score.retrievability = Some(retrievability.extract::<f64>()? as f32);
        }
        if let Some(value) = target_retentions.get_item(&card_id)? {
            score.target_retention = probability(&value)?;
        }
        if let Some(value) = intervening_reviews.get_item(&card_id)? {
            if value.is_instance_of::<PyInt>() && value.ge(0)? {
                score.intervening_reviews = Some(in_range(&value, value.extract::<u32>())?);
            }
        }
        if curve_due_card_ids.contains(&card_id)? {
            score.curve_due = Some(true);
        }
        if let Some(value) = curve_retrievabilities.get_item(&card_id)? {
            score.curve_retrievability = probability(&value)?;
        }
        scores.push(score);
    }
    let request = RwkvStatsGraphScoresRequest { search, scores };
    let bytes = py.detach(|| request.encode_to_vec());
    Ok(PyBytes::new(py, &bytes))
}

/// `value` as an f32 field when aqt's `_valid_probability` accepts it.
fn probability(value: &Bound<'_, PyAny>) -> PyResult<Option<f32>> {
    if value.is_instance_of::<PyBool>()
        || !(value.is_instance_of::<PyFloat>() || value.is_instance_of::<PyInt>())
    {
        return Ok(None);
    }
    // an int too large for a double raises, as math.isfinite does
    let probability = value.extract::<f64>()?;
    Ok(
        (probability.is_finite() && (0.0..=1.0).contains(&probability))
            .then_some(probability as f32),
    )
}

/// The protobuf library's error for an int its field cannot hold.
fn in_range<T>(value: &Bound<'_, PyAny>, extracted: PyResult<T>) -> PyResult<T> {
    extracted.map_err(|err| {
        if err.is_instance_of::<PyOverflowError>(value.py()) {
            PyValueError::new_err(format!("Value out of range: {value}"))
        } else {
            err
        }
    })
}
