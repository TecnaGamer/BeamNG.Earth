//! Lane flow's counts from each road's home cell (roads._lane_flow_home) and the ramp lane rule's overrides
//! (roads._lane_overrides), byte for byte the Python they replaced. The neighbour layers' reading (mapdata's hook,
//! _near_layers) and each layer's own solve (_lane_flow_local, remembered on it) stay Python callbacks.

use std::collections::HashMap;

use pyo3::exceptions::PyIndexError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet};

use super::pyobj::items;
use super::pyobj::CArr2;

/// roads._lane_flow_home: list(local) (this cell's solve), then each counted road (counted[i]) whose home cell
/// (homes()[i]: asked only when the layer knows its cell and neighbours, homes_fn given) is not this cell (me[0])
/// takes its home cell's count: near(away) -> {cell: layer} of the away cells (the set of them, asked only when there
/// is one), each home layer's solve local_of(layer) (in order of the homes' first road), the road found there by its
/// id (other.ids, the last of a repeated id).
#[pyfunction]
#[pyo3(signature = (local, counted, homes_fn, me, near, ids, local_of))]
#[allow(clippy::too_many_arguments)]
pub fn lane_flow_home<'py>(py: Python<'py>, local: Bound<'py, PyAny>, counted: Vec<bool>,
                           homes_fn: Option<Bound<'py, PyAny>>, me: Bound<'py, PyAny>, near: Bound<'py, PyAny>,
                           ids: Bound<'py, PyAny>, local_of: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
    let out = PyList::new(py, local.try_iter()?.collect::<PyResult<Vec<_>>>()?)?;
    let homes: Vec<Bound<'py, PyAny>> = match homes_fn {
        Some(f) => f.call0()?.try_iter()?.collect::<PyResult<_>>()?,
        None => Vec::new(),
    };
    let flag = |i: usize| -> PyResult<bool> {
        counted.get(i).copied().ok_or_else(|| PyIndexError::new_err("list index out of range"))
    };
    // away = {home for i, home in enumerate(homes) if counted[i] and home != me[0]} (me[0] read where first needed)
    let mut me0: Option<Bound<'py, PyAny>> = None;
    let mut is_away = Vec::with_capacity(homes.len());
    let away = PySet::empty(py)?;
    for (i, home) in homes.iter().enumerate() {
        let a = if flag(i)? {
            if me0.is_none() {
                me0 = Some(me.get_item(0)?);
            }
            home.ne(me0.as_ref().unwrap())?
        } else {
            false
        };
        if a {
            away.add(home)?;
        }
        is_away.push(a);
    }
    if away.is_empty() {
        return Ok(out);
    }
    let near = near.call1((away,))?.cast_into::<PyDict>()
        .map_err(|_| pyo3::exceptions::PyTypeError::new_err("lane_flow_home: near() is not a dict"))?;
    // rows: {home: [i, ...]} in the order of each home's first road
    let mut rows: Vec<(Bound<'py, PyAny>, Vec<usize>)> = Vec::new();
    for (i, home) in homes.iter().enumerate() {
        if is_away[i] && near.contains(home)? {
            let mut found = None;
            for (k, (h, _)) in rows.iter().enumerate() {
                if h.eq(home)? {
                    found = Some(k);
                    break;
                }
            }
            match found {
                Some(k) => rows[k].1.push(i),
                None => rows.push((home.clone(), vec![i])),
            }
        }
    }
    for (home, idx) in rows {
        let other = near.get_item(&home)?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(home.clone().unbind()))?;
        let res = local_of.call1((&other,))?;
        let mut pos: HashMap<String, usize> = HashMap::new();
        for (j, fid) in items(Some(&other.getattr("ids")?))?.unwrap_or_default().iter().enumerate() {
            pos.insert(fid.extract::<String>()?, j);
        }
        for i in idx {
            if let Some(&j) = pos.get(&ids.get_item(i)?.extract::<String>()?) {
                out.set_item(i, res.get_item(j)?)?;
            }
        }
    }
    Ok(out)
}

/// roads._lane_overrides' making with the ramp lane rule on: {i: int(v)} of home() (the home cells' counts, asked
/// only where a road of the layer is one the rule checks: attrs' column 4 > 0.5, _lane_flow_rows), v not None; {}
/// otherwise.
#[pyfunction]
pub fn lane_overrides<'py>(py: Python<'py>, attrs: CArr2<'py, f64>, home: Bound<'py, PyAny>)
                           -> PyResult<Bound<'py, PyDict>> {
    let a = attrs.as_array();
    let d = PyDict::new(py);
    if a.ncols() < 5 {
        return Err(PyIndexError::new_err(format!("index 4 is out of bounds for axis 1 with size {}", a.ncols())));
    }
    if !(0..a.nrows()).any(|i| a[[i, 4]] > 0.5) {
        return Ok(d);
    }
    let int = py.get_type::<pyo3::types::PyInt>();
    for (i, v) in home.call0()?.try_iter()?.enumerate() {
        let v = v?;
        if !v.is_none() {
            d.set_item(i, int.call1((v,))?)?;
        }
    }
    Ok(d)
}
