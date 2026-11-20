//! A road's copies in the cells at hand (a road is in every z14 cell it touches): what is resolved from the copy in one
//! chosen cell (roads.joint_desc).

use std::collections::BTreeMap;

use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyTuple};

use super::pyfloat::{cell_of, np_round, py_round, ZOOM};

/// A joint's key: (lon, lat) rounded to 6 places, -0.0 as 0.0 (Python's dict keys: -0.0 == 0.0), compared as the
/// tuples sort (both finite: a total order).
#[derive(Clone, Copy, PartialEq)]
struct Key(f64, f64);

impl Eq for Key {}

impl PartialOrd for Key {
    fn partial_cmp(&self, o: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(o))
    }
}

impl Ord for Key {
    fn cmp(&self, o: &Self) -> std::cmp::Ordering {
        self.0.total_cmp(&o.0).then(self.1.total_cmp(&o.1))
    }
}

fn zero(v: f64) -> f64 {
    if v == 0.0 { 0.0 } else { v }
}

/// Python's round(v, 6) of a row's coordinate: a Python float's correctly rounded decimal rounding, a numpy float's
/// np.round (multiply, rint, divide), an int itself.
fn round6(v: &Bound<'_, PyAny>) -> PyResult<f64> {
    if v.is_exact_instance_of::<PyFloat>() {
        return Ok(py_round(v.extract::<f64>()?, 6));
    }
    if v.is_instance_of::<PyFloat>() {
        return Ok(np_round(v.extract::<f64>()?, 6));
    }
    if v.is_instance_of::<pyo3::types::PyInt>() {
        return v.extract::<f64>();
    }
    Err(pyo3::exceptions::PyTypeError::new_err(format!("joint_desc: a joint's coordinate is a {}",
                                                       v.get_type().name()?)))
}

/// roads.joint_desc(copies, fn) given each copy's cell ("z/x/y", None without one) and rows (fn(feats)[i] or (): the
/// tapers / shifts ((lon, lat, ...), ...) that copy found): every joint once, each from the copy in the cell the joint
/// lies in (that cell holds every road meeting there), the last such copy's; a joint whose cell is not at hand: the
/// first copy's that has it. The rows themselves, sorted by their (lon, lat) rounded to 6 places.
///
/// The rows' coordinates must be finite where the copy has a cell (mapcells.cell_of raised for any other); a copy
/// without a cell with a non-finite coordinate is refused (Python kept every NaN key apart and sorted them in an order
/// its sort's inconsistent comparisons made: no road row has one).
#[pyfunction]
pub fn joint_desc<'py>(py: Python<'py>, cells: Vec<Option<String>>, rows: Vec<Bound<'py, PyAny>>)
                       -> PyResult<Bound<'py, PyTuple>> {
    if cells.len() != rows.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("joint_desc: one cell per copy"));
    }
    let mut out: BTreeMap<Key, Bound<'py, PyAny>> = BTreeMap::new();
    let mut other: BTreeMap<Key, Bound<'py, PyAny>> = BTreeMap::new();
    for (cid, row) in cells.iter().zip(rows.iter()) {
        if row.is_none() {
            continue;
        }
        for d in row.try_iter()? {
            let d = d?;
            let lon = d.get_item(0)?;
            let lat = d.get_item(1)?;
            let (klon, klat) = (round6(&lon)?, round6(&lat)?);
            let key = Key(zero(klon), zero(klat));
            let own = match cid {
                None => true,
                Some(c) => {
                    let (x, y) = cell_of(lat.extract::<f64>()?, lon.extract::<f64>()?, ZOOM)
                        .map_err(pyo3::exceptions::PyValueError::new_err)?;
                    format!("{ZOOM}/{x}/{y}") == *c
                }
            };
            if !(klon.is_finite() && klat.is_finite()) {
                return Err(pyo3::exceptions::PyValueError::new_err("joint_desc: a joint's coordinate is not finite"));
            }
            if own {
                out.insert(key, d);
            } else {
                other.entry(key).or_insert(d);
            }
        }
    }
    for (k, d) in other {
        out.entry(k).or_insert(d);
    }
    PyTuple::new(py, out.into_values())
}
