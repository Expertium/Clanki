// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The RWKV review inputs of the backend's input rows
//! (`RwkvReviewInputRowsForCardsResponse`), for aqt.rwkv_scheduler: the
//! Stats graph's search, the Browser's values, the study queue's deck rows
//! and the rows of given cards. Python built one `RwkvReviewInput` per row
//! from the parsed message, ~8 us a row with the GIL held (24 ms for the
//! 3,038 rows of the Stats graph on Andrew's collection); here the rows are
//! decoded without the GIL and the objects are made without running Python
//! code per row.

use std::collections::hash_map::Entry;
use std::collections::HashMap;

use anki_proto::scheduler::rwkv_review_input_rows_for_cards_response::Row;
use anki_proto::scheduler::RwkvReviewInputRowsForCardsResponse;
use prost::Message;
use pyo3::exceptions::PyTypeError;
use pyo3::exceptions::PyValueError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyBool;
use pyo3::types::PyBytes;
use pyo3::types::PyDict;
use pyo3::types::PyFloat;
use pyo3::types::PyList;
use pyo3::types::PyString;
use pyo3::types::PyTuple;
use pyo3::types::PyType;

/// The fields of `RwkvReviewInput`, in the order the dataclass declares
/// them; `review_inputs` refuses a class with other fields.
const INPUT_FIELDS: [&str; 18] = [
    "identity",
    "is_query",
    "ease",
    "duration_millis",
    "card_type",
    "card_queue",
    "card_due",
    "interval_days",
    "ease_factor",
    "reps",
    "lapses",
    "day_offset",
    "current_state_kind",
    "current_normal_state_kind",
    "current_elapsed_days",
    "current_elapsed_seconds",
    "target_retentions",
    "enforce_grade_order",
];
const IDENTITY_FIELDS: [&str; 4] = ["card_id", "note_id", "deck_id", "preset_id"];

/// The decoded rows of one `RwkvReviewInputRowsForCardsResponse`, with its
/// counts.
#[pyclass(module = "_rsbridge", frozen)]
pub(crate) struct RwkvReviewInputRows {
    rows: Vec<Row>,
    #[pyo3(get)]
    loaded_cards: u32,
    #[pyo3(get)]
    cards_with_supported_state: u32,
    #[pyo3(get)]
    disabled_config_cards: u32,
    #[pyo3(get)]
    deck_configs: u32,
    #[pyo3(get)]
    searched_cards: u32,
}

/// The review states `RwkvReviewInput.card_type` takes from the row's
/// scheduling state, as aqt's `RwkvReviewState` numbers them.
#[derive(FromPyObject)]
struct ReviewStates<'py> {
    #[pyo3(item)]
    filtered: Bound<'py, PyAny>,
    #[pyo3(item)]
    new: Bound<'py, PyAny>,
    #[pyo3(item)]
    learning: Bound<'py, PyAny>,
    #[pyo3(item)]
    review: Bound<'py, PyAny>,
    #[pyo3(item)]
    relearning: Bound<'py, PyAny>,
}

#[pymethods]
impl RwkvReviewInputRows {
    /// Decodes the serialized response, with the GIL released; ValueError
    /// for malformed bytes.
    #[new]
    fn new(py: Python<'_>, data: &Bound<'_, PyBytes>) -> PyResult<Self> {
        let bytes = data.as_bytes();
        let response = py
            .detach(|| RwkvReviewInputRowsForCardsResponse::decode(bytes))
            .map_err(|err| PyValueError::new_err(err.to_string()))?;
        Ok(Self {
            rows: response.rows,
            loaded_cards: response.loaded_cards,
            cards_with_supported_state: response.cards_with_supported_state,
            disabled_config_cards: response.disabled_config_cards,
            deck_configs: response.deck_configs,
            searched_cards: response.searched_cards,
        })
    }

    fn __len__(&self) -> usize {
        self.rows.len()
    }

    /// The rows as `{batch size: [(card id, RwkvReviewInput), ...]}`, the
    /// batch sizes in the order their first row comes, each list in row
    /// order: `batch_size_override` for every row when given, else the
    /// row's batch size when it lies in [min_batch_size, max_batch_size],
    /// else `default_batch_size`.
    ///
    /// Each input is what aqt's Python conversion built from the parsed
    /// row: a query (`is_query` True, no ease or duration); the preset id
    /// from `stable_preset_id(row.preset_id)`, None for an empty one; the
    /// review state from `review_states` for a filtered state or a known
    /// normal state, else the row's card type; empty state kinds as None;
    /// the elapsed times only when the row has them; the row's target
    /// retention four times when it is a probability, else
    /// `default_target_retention`; `enforce_grade_order` True unless the
    /// row says otherwise. The objects are made as `copy` makes a
    /// dataclass: `object.__new__`, then the fields into its `__dict__`.
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (
        input_type,
        identity_type,
        stable_preset_id,
        review_states,
        *,
        batch_size_override,
        default_batch_size,
        min_batch_size,
        max_batch_size,
        default_target_retention,
    ))]
    fn review_inputs<'py>(
        &self,
        py: Python<'py>,
        input_type: &Bound<'py, PyType>,
        identity_type: &Bound<'py, PyType>,
        stable_preset_id: &Bound<'py, PyAny>,
        review_states: ReviewStates<'py>,
        batch_size_override: Option<Bound<'py, PyAny>>,
        default_batch_size: u32,
        min_batch_size: u32,
        max_batch_size: u32,
        default_target_retention: Bound<'py, PyFloat>,
    ) -> PyResult<Bound<'py, PyDict>> {
        require_fields(input_type, &INPUT_FIELDS)?;
        require_fields(identity_type, &IDENTITY_FIELDS)?;

        let object_new = py
            .import(intern!(py, "builtins"))?
            .getattr(intern!(py, "object"))?
            .getattr(intern!(py, "__new__"))?;
        let dict_name = intern!(py, "__dict__");
        let none = py.None().into_bound(py);
        let true_ = PyBool::new(py, true).to_owned().into_any();
        let false_ = PyBool::new(py, false).to_owned().into_any();
        let names = FieldNames::new(py);

        let mut presets: HashMap<&str, Bound<'py, PyAny>> = HashMap::new();
        let mut kinds: HashMap<&str, Bound<'py, PyAny>> = HashMap::new();
        let mut retentions: HashMap<u32, Bound<'py, PyTuple>> = HashMap::new();
        let mut groups: Vec<(u32, Bound<'py, PyList>)> = Vec::new();
        let override_group = match &batch_size_override {
            Some(_) => Some(PyList::empty(py)),
            None => None,
        };

        for row in &self.rows {
            let preset_id = if row.preset_id.is_empty() {
                none.clone()
            } else if let Some(preset_id) = presets.get(row.preset_id.as_str()) {
                preset_id.clone()
            } else {
                let preset_id = stable_preset_id.call1((row.preset_id.as_str(),))?;
                presets.insert(row.preset_id.as_str(), preset_id.clone());
                preset_id
            };
            let state_kind = state_kind_object(py, &mut kinds, &row.current_state_kind, &none);
            let normal_state_kind =
                state_kind_object(py, &mut kinds, &row.current_normal_state_kind, &none);
            let card_type = if row.current_state_kind == "filtered" {
                review_states.filtered.clone()
            } else {
                match row.current_normal_state_kind.as_str() {
                    "new" => review_states.new.clone(),
                    "learning" => review_states.learning.clone(),
                    "review" => review_states.review.clone(),
                    "relearning" => review_states.relearning.clone(),
                    _ => row.card_type.into_pyobject(py)?.into_any(),
                }
            };
            // one tuple per distinct retention: it is immutable
            let target_retentions = match retentions.entry(row.target_retention.to_bits()) {
                Entry::Occupied(entry) => entry.get().clone(),
                Entry::Vacant(entry) => {
                    let target_retention = f64::from(row.target_retention);
                    let target_retention = if target_retention.is_finite()
                        && (0.0..=1.0).contains(&target_retention)
                    {
                        PyFloat::new(py, target_retention)
                    } else {
                        default_target_retention.clone()
                    };
                    entry
                        .insert(PyTuple::new(
                            py,
                            [
                                &target_retention,
                                &target_retention,
                                &target_retention,
                                &target_retention,
                            ],
                        )?)
                        .clone()
                }
            };

            let card_id = row.card_id.into_pyobject(py)?.into_any();

            let identity = object_new.call1((identity_type,))?;
            let fields = identity.getattr(dict_name)?.cast_into::<PyDict>()?;
            fields.set_item(&names.card_id, &card_id)?;
            fields.set_item(&names.note_id, row.note_id)?;
            fields.set_item(&names.deck_id, row.deck_id)?;
            fields.set_item(&names.preset_id, preset_id)?;

            let input = object_new.call1((input_type,))?;
            let fields = input.getattr(dict_name)?.cast_into::<PyDict>()?;
            fields.set_item(&names.identity, identity)?;
            fields.set_item(&names.is_query, &true_)?;
            fields.set_item(&names.ease, &none)?;
            fields.set_item(&names.duration_millis, &none)?;
            fields.set_item(&names.card_type, card_type)?;
            fields.set_item(&names.card_queue, row.card_queue)?;
            fields.set_item(&names.card_due, row.card_due)?;
            fields.set_item(&names.interval_days, row.interval_days)?;
            fields.set_item(&names.ease_factor, row.ease_factor)?;
            fields.set_item(&names.reps, row.reps)?;
            fields.set_item(&names.lapses, row.lapses)?;
            fields.set_item(&names.day_offset, row.day_offset)?;
            fields.set_item(&names.current_state_kind, state_kind)?;
            fields.set_item(&names.current_normal_state_kind, normal_state_kind)?;
            fields.set_item(&names.current_elapsed_days, row.current_elapsed_days)?;
            fields.set_item(&names.current_elapsed_seconds, row.current_elapsed_seconds)?;
            fields.set_item(&names.target_retentions, target_retentions)?;
            fields.set_item(
                &names.enforce_grade_order,
                if row.enforce_grade_order.unwrap_or(true) {
                    &true_
                } else {
                    &false_
                },
            )?;

            let item = PyTuple::new(py, [card_id, input])?;
            let group = match &override_group {
                Some(group) => group,
                None => {
                    let batch_size = if (min_batch_size..=max_batch_size).contains(&row.batch_size)
                    {
                        row.batch_size
                    } else {
                        default_batch_size
                    };
                    match groups.iter().position(|(size, _)| *size == batch_size) {
                        Some(index) => &groups[index].1,
                        None => {
                            groups.push((batch_size, PyList::empty(py)));
                            &groups[groups.len() - 1].1
                        }
                    }
                }
            };
            group.append(item)?;
        }

        let inputs_by_batch_size = PyDict::new(py);
        match (batch_size_override, override_group) {
            (Some(batch_size), Some(group)) => {
                if !group.is_empty() {
                    inputs_by_batch_size.set_item(batch_size, group)?;
                }
            }
            _ => {
                for (batch_size, group) in groups {
                    inputs_by_batch_size.set_item(batch_size, group)?;
                }
            }
        }
        Ok(inputs_by_batch_size)
    }
}

/// The Python objects of the field names, made once per call.
struct FieldNames<'py> {
    card_id: Bound<'py, PyString>,
    note_id: Bound<'py, PyString>,
    deck_id: Bound<'py, PyString>,
    preset_id: Bound<'py, PyString>,
    identity: Bound<'py, PyString>,
    is_query: Bound<'py, PyString>,
    ease: Bound<'py, PyString>,
    duration_millis: Bound<'py, PyString>,
    card_type: Bound<'py, PyString>,
    card_queue: Bound<'py, PyString>,
    card_due: Bound<'py, PyString>,
    interval_days: Bound<'py, PyString>,
    ease_factor: Bound<'py, PyString>,
    reps: Bound<'py, PyString>,
    lapses: Bound<'py, PyString>,
    day_offset: Bound<'py, PyString>,
    current_state_kind: Bound<'py, PyString>,
    current_normal_state_kind: Bound<'py, PyString>,
    current_elapsed_days: Bound<'py, PyString>,
    current_elapsed_seconds: Bound<'py, PyString>,
    target_retentions: Bound<'py, PyString>,
    enforce_grade_order: Bound<'py, PyString>,
}

impl<'py> FieldNames<'py> {
    fn new(py: Python<'py>) -> Self {
        let name = |name: &str| PyString::intern(py, name);
        Self {
            card_id: name("card_id"),
            note_id: name("note_id"),
            deck_id: name("deck_id"),
            preset_id: name("preset_id"),
            identity: name("identity"),
            is_query: name("is_query"),
            ease: name("ease"),
            duration_millis: name("duration_millis"),
            card_type: name("card_type"),
            card_queue: name("card_queue"),
            card_due: name("card_due"),
            interval_days: name("interval_days"),
            ease_factor: name("ease_factor"),
            reps: name("reps"),
            lapses: name("lapses"),
            day_offset: name("day_offset"),
            current_state_kind: name("current_state_kind"),
            current_normal_state_kind: name("current_normal_state_kind"),
            current_elapsed_days: name("current_elapsed_days"),
            current_elapsed_seconds: name("current_elapsed_seconds"),
            target_retentions: name("target_retentions"),
            enforce_grade_order: name("enforce_grade_order"),
        }
    }
}

/// A state kind as Python read it (`row.current_state_kind or None`): the
/// string, or None for an empty one; one object per distinct kind.
fn state_kind_object<'py, 'a>(
    py: Python<'py>,
    kinds: &mut HashMap<&'a str, Bound<'py, PyAny>>,
    kind: &'a str,
    none: &Bound<'py, PyAny>,
) -> Bound<'py, PyAny> {
    if kind.is_empty() {
        return none.clone();
    }
    kinds
        .entry(kind)
        .or_insert_with(|| PyString::new(py, kind).into_any())
        .clone()
}

/// TypeError unless `cls` is a dataclass with exactly `fields`, in order:
/// the objects are filled field by field, so a changed class must not be
/// filled with the old fields.
fn require_fields(cls: &Bound<'_, PyType>, fields: &[&str]) -> PyResult<()> {
    let declared: Vec<String> = cls
        .getattr(intern!(cls.py(), "__dataclass_fields__"))?
        .cast::<PyDict>()?
        .keys()
        .extract()?;
    if declared.iter().map(String::as_str).eq(fields.iter().copied()) {
        Ok(())
    } else {
        Err(PyTypeError::new_err(format!(
            "{} does not have the fields the RWKV input rows fill",
            cls.name()?
        )))
    }
}
