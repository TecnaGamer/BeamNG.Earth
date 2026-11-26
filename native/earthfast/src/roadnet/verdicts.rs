//! The aerial photo's surface verdicts as _spec_inputs reads them, byte for byte the Python they replaced: which roads
//! roadsurf.py's store may judge (roadsurf.candidates) and its verdicts looked up for them (roadsurf.layer_verdicts),
//! which roads photosurface.py measures (photosurface.verdicts' selection) and its verdicts filled in for the map's
//! missing values (_spec_inputs). The store's files and the photo's tiles stay with the Python (reading them is I/O;
//! photosurface's measuring decodes JPEGs through PIL, whose pixels are its input).

use std::collections::BTreeSet;

use numpy::{IntoPyArray, PyArray1};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use super::pyobj::{items, lower_is, py_str, py_strip};
use super::pyobj::CArr1;

/// np.fromiter(..., bool, n) of an iterator that gave k < n items.
fn too_short(n: usize, k: usize) -> PyErr {
    pyo3::exceptions::PyValueError::new_err(format!("iterator too short: Expected {n} but iterator had only {k} items."))
}

/// roadsurf.candidates: bool (n,) of the roads the photo may judge, n the layer's length: a class of `classes` (str of
/// the class column's value) whose map surface value (vals, in order; zipped with the classes) is None, with no
/// mapped lane count (lanes_fn(): roads._osm_values of "lanes", NaN for none, asked after that) and no smoothness of
/// `good` (str(v).strip().lower()); all False without a class column or features.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn roadsurf_candidates<'py>(py: Python<'py>, n: usize, cls: Option<Bound<'py, PyAny>>, vals: Bound<'py, PyAny>,
                                lanes_fn: Bound<'py, PyAny>, smoothness: Option<Bound<'py, PyAny>>,
                                classes: Vec<String>, good: Vec<String>) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let Some(cls) = items(cls.as_ref())?.filter(|_| n > 0) else {
        return Ok(vec![false; n].into_pyarray(py));
    };
    let classes: BTreeSet<String> = classes.into_iter().collect();
    // (vals is iterated, not indexed: zip(cls.tolist(), vals))
    let vals: Vec<Bound<'py, PyAny>> = vals.try_iter()?.take(n.min(cls.len())).collect::<PyResult<_>>()?;
    let mut out = Vec::with_capacity(n);
    for (c, v) in cls.iter().zip(&vals).take(n) {
        out.push(classes.contains(&py_str(c)?) && v.is_none());
    }
    if out.len() < n {
        return Err(too_short(n, out.len()));
    }
    let lanes: CArr1<f64> = lanes_fn.call0()?.extract()?;
    let lanes = lanes.as_slice()?;
    if let Some(sm) = items(smoothness.as_ref())? {
        let mut k = 0;
        for (i, v) in sm.iter().take(n).enumerate() {
            let s = py_str(v)?;
            let s = py_strip(&s);
            if good.iter().any(|g| lower_is(s, g)) {
                out[i] = false;
            }
            k += 1;
        }
        if k < n {
            return Err(too_short(n, k));
        }
    }
    // (out & np.isnan(lanes) & ~good)
    if lanes.len() != n {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "operands could not be broadcast together with shapes ({n},) ({},)", lanes.len())));
    }
    for (o, l) in out.iter_mut().zip(lanes) {
        *o = *o && l.is_nan();
    }
    Ok(out.into_pyarray(py))
}

/// roadsurf.layer_verdicts' lookup: {i: verdict} of the candidates (in order) whose home cell's entry (entry(home),
/// homes_fn()[i]: asked only when there is a candidate) has a true verdict under str(ids[i]).
#[pyfunction]
pub fn roadsurf_lookup<'py>(py: Python<'py>, cand: CArr1<'py, bool>, homes_fn: Bound<'py, PyAny>,
                            ids: Bound<'py, PyAny>, entry: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyDict>> {
    let out = PyDict::new(py);
    let cand = cand.as_slice()?;
    if !cand.iter().any(|&c| c) {
        return Ok(out);
    }
    let homes = homes_fn.call0()?;
    for (i, _) in cand.iter().enumerate().filter(|(_, &c)| c) {
        let e = entry.call1((homes.get_item(i)?,))?;
        let v = e.call_method1("get", (py_str(&ids.get_item(i)?)?,))?;
        if v.is_truthy()? {
            out.set_item(i, v)?;
        }
    }
    Ok(out)
}

/// photosurface.verdicts' selection: the features i < n (in order) whose class (str(cls[i])) is one of `classes` and
/// whose map surface value (surface_of(i), asked only for those) is None.
#[pyfunction]
pub fn photo_candidates<'py>(n: usize, cls: Bound<'py, PyAny>, surface_of: Bound<'py, PyAny>, classes: Vec<String>)
                             -> PyResult<Vec<usize>> {
    let classes: BTreeSet<String> = classes.into_iter().collect();
    let mut out = Vec::new();
    for i in 0..n {
        if classes.contains(&py_str(&cls.get_item(i)?)?) && surface_of.call1((i,))?.is_none() {
            out.push(i);
        }
    }
    Ok(out)
}

/// _spec_inputs' surface values with the photo's: list(vals); where an eligible feature (elig) has none, the photo's
/// verdicts (photo_fn(): {i: value}, asked only then) fill in every feature that has none (ph.get(i)).
#[pyfunction]
pub fn photo_values<'py>(py: Python<'py>, vals: Bound<'py, PyAny>, elig: CArr1<'py, bool>,
                         photo_fn: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
    let vals: Vec<Bound<'py, PyAny>> = vals.try_iter()?.collect::<PyResult<_>>()?;
    let mut ask = false;
    for (i, &e) in elig.as_slice()?.iter().enumerate() {
        // (vals[i] for i of np.flatnonzero(elig): an index past vals is the list's IndexError)
        if e {
            let Some(v) = vals.get(i) else {
                return Err(pyo3::exceptions::PyIndexError::new_err("list index out of range"));
            };
            if v.is_none() {
                ask = true;
                break;
            }
        }
    }
    if !ask {
        return PyList::new(py, vals);
    }
    let ph = photo_fn.call0()?;
    let out: Vec<Bound<'py, PyAny>> = vals.into_iter().enumerate()
        .map(|(i, v)| if v.is_none() { ph.call_method1("get", (i,)) } else { Ok(v) })
        .collect::<PyResult<_>>()?;
    PyList::new(py, out)
}
