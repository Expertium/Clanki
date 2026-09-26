// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The packed warm-up request of a batch of RWKV review inputs, for
//! aqt.rwkv_srs_benchmark's `_packed_warm_up_reviews`: the bytes the RWKV
//! replay reads (`ARWKVWU2`, then one 95-byte row per review). Every
//! replay packs its batches this way: the start-up build, the rebuild after
//! a delete or a preset change, and the recording pass. Python packed one
//! row at a time, ~3 us a row with the GIL held (50 ms for a batch of
//! 16,384 reviews), while the user worked; here a row takes a fraction of
//! that, and the GIL is offered to another thread between two blocks of
//! rows.

use pyo3::exceptions::PyOverflowError;
use pyo3::exceptions::PyTypeError;
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyo3::types::PyFloat;
use pyo3::types::PyInt;

const MAGIC: &[u8; 8] = b"ARWKVWU2";
/// `struct.Struct("<IqqqqBBqqqqqffffB").size`
const ROW_BYTES: usize = 95;
/// Rows packed between two offers of the GIL to another thread: about the
/// work Python does in one switch interval (1 ms).
const ROWS_PER_BLOCK: usize = 1024;

/// The bytes of `_packed_warm_up_reviews(reviews)`: the header (magic and
/// row count), then per review the row of `_packed_review_input_row(review)`
/// with no query day. Each value is read and converted as that function
/// does: `int()` of an optional integer field, `float()` of a target
/// retention, the truth of `is_query` and `enforce_grade_order`; the errors
/// `struct.pack` raises for a value its field cannot hold are raised too.
#[pyfunction]
pub(crate) fn packed_warm_up_reviews<'py>(
    py: Python<'py>,
    reviews: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyBytes>> {
    let count = reviews.len()?;
    let packer = RowPacker::new(py)?;
    let header_count = u32::try_from(count)
        .map_err(|_| packer.error("'I' format requires 0 <= number <= 4294967295"))?;
    let mut out = Vec::with_capacity(12 + count * ROW_BYTES);
    out.extend_from_slice(MAGIC);
    out.extend_from_slice(&header_count.to_le_bytes());
    for (index, review) in reviews.try_iter()?.enumerate() {
        if index > 0 && index % ROWS_PER_BLOCK == 0 {
            // a thread waiting for the GIL takes it here, as it would
            // between two of Python's bytecodes
            py.detach(|| ());
        }
        packer.pack(&review?, &mut out)?;
    }
    Ok(PyBytes::new(py, &out))
}

struct RowPacker<'py> {
    py: Python<'py>,
    int_type: Bound<'py, PyAny>,
    float_type: Bound<'py, PyAny>,
    struct_error: Bound<'py, PyAny>,
}

impl<'py> RowPacker<'py> {
    fn new(py: Python<'py>) -> PyResult<Self> {
        Ok(Self {
            py,
            int_type: py.get_type::<PyInt>().into_any(),
            float_type: py.get_type::<PyFloat>().into_any(),
            struct_error: py.import("struct")?.getattr("error")?,
        })
    }

    fn pack(&self, review: &Bound<'py, PyAny>, out: &mut Vec<u8>) -> PyResult<()> {
        let py = self.py;
        let identity = review.getattr(intern!(py, "identity"))?;
        let mut presence: u32 = 0;
        let note_id = self.optional_i64(&identity, intern!(py, "note_id"), 0, &mut presence)?;
        let deck_id = self.optional_i64(&identity, intern!(py, "deck_id"), 1, &mut presence)?;
        let preset_id = self.optional_i64(&identity, intern!(py, "preset_id"), 2, &mut presence)?;
        let is_query = review.getattr(intern!(py, "is_query"))?.is_truthy()?;
        let ease = self.optional_i64(review, intern!(py, "ease"), 3, &mut presence)?;
        let duration_millis =
            self.optional_i64(review, intern!(py, "duration_millis"), 4, &mut presence)?;
        let card_type = self.optional_i64(review, intern!(py, "card_type"), 5, &mut presence)?;
        let day_offset = self.optional_i64(review, intern!(py, "day_offset"), 6, &mut presence)?;
        let elapsed_days = self.optional_i64(
            review,
            intern!(py, "current_elapsed_days"),
            7,
            &mut presence,
        )?;
        let elapsed_seconds = self.optional_i64(
            review,
            intern!(py, "current_elapsed_seconds"),
            8,
            &mut presence,
        )?;
        let retentions = review.getattr(intern!(py, "target_retentions"))?;
        let mut retention = [0f32; 4];
        for (index, slot) in retention.iter_mut().enumerate() {
            *slot = self.optional_f32(
                &retentions.get_item(index)?,
                9 + index as u32,
                &mut presence,
            )?;
        }
        // the card id is packed as it is, without int(), as "q" takes it
        let card_id = self.q(&identity.getattr(intern!(py, "card_id"))?)?;
        let enforce_grade_order = review
            .getattr(intern!(py, "enforce_grade_order"))?
            .is_truthy()?;
        let ease = u8::try_from(ease)
            .map_err(|_| self.error("ubyte format requires 0 <= number <= 255"))?;

        out.extend_from_slice(&presence.to_le_bytes());
        for value in [card_id, note_id, deck_id, preset_id] {
            out.extend_from_slice(&value.to_le_bytes());
        }
        out.push(is_query as u8);
        out.push(ease);
        for value in [
            duration_millis,
            card_type,
            day_offset,
            elapsed_days,
            elapsed_seconds,
        ] {
            out.extend_from_slice(&value.to_le_bytes());
        }
        for value in retention {
            out.extend_from_slice(&value.to_le_bytes());
        }
        out.push(enforce_grade_order as u8);
        Ok(())
    }

    /// `optional_i64(owner.name, bit)`: 0 for None, else `int(value)` and
    /// the presence bit.
    fn optional_i64(
        &self,
        owner: &Bound<'py, PyAny>,
        name: &Bound<'py, pyo3::types::PyString>,
        bit: u32,
        presence: &mut u32,
    ) -> PyResult<i64> {
        let value = owner.getattr(name)?;
        if value.is_none() {
            return Ok(0);
        }
        *presence |= 1 << bit;
        if value.is_exact_instance_of::<PyInt>() {
            self.q(&value)
        } else {
            self.q(&self.int_type.call1((value,))?)
        }
    }

    /// `optional_f32(value, bit)`: 0.0 for None, else `float(value)` packed
    /// as "f" packs it (a finite value too large for a float raises).
    fn optional_f32(
        &self,
        value: &Bound<'py, PyAny>,
        bit: u32,
        presence: &mut u32,
    ) -> PyResult<f32> {
        if value.is_none() {
            return Ok(0.0);
        }
        *presence |= 1 << bit;
        let value: f64 = if value.is_exact_instance_of::<PyFloat>() {
            value.extract()?
        } else {
            self.float_type.call1((value,))?.extract()?
        };
        let packed = value as f32;
        if packed.is_infinite() && !value.is_infinite() {
            return Err(PyOverflowError::new_err(
                "float too large to pack with f format",
            ));
        }
        Ok(packed)
    }

    /// An integer as "q" packs it: an int in the i64 range, else
    /// struct.error.
    fn q(&self, value: &Bound<'py, PyAny>) -> PyResult<i64> {
        match value.extract::<i64>() {
            Ok(value) => Ok(value),
            Err(err) if err.is_instance_of::<PyOverflowError>(self.py) => {
                Err(self.error("argument out of range"))
            }
            Err(err) if err.is_instance_of::<PyTypeError>(self.py) => {
                Err(self.error("required argument is not an integer"))
            }
            Err(err) => Err(err),
        }
    }

    fn error(&self, message: &str) -> PyErr {
        match self.struct_error.call1((message,)) {
            Ok(instance) => PyErr::from_value(instance),
            Err(err) => err,
        }
    }
}
