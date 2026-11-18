//! Lane flow's per-layer rows (roads._lane_flow_rows, _ramp_candidates) and its counts handed back
//! (roads._lane_flow_local's lists), byte for byte the numpy / Python they replaced.

use std::collections::BTreeMap;

use numpy::{IntoPyArray, PyArray1, PyArray2};
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};

use super::columns::rule_cols_of;
use super::pyobj::py_str;
use super::pyobj::{CArr1, CArr2};

/// np.asarray(col).astype(str), as Python strings: an object or str array's str() of each, any other dtype numpy's own
/// text of its numbers.
fn str_col(col: &Bound<'_, PyAny>) -> PyResult<Vec<String>> {
    let py = col.py();
    let arr = py.import("numpy")?.getattr("asarray")?.call1((col,))?;
    let kind: String = arr.getattr("dtype")?.getattr("kind")?.extract()?;
    let arr = if kind == "U" || kind == "O" { arr } else { arr.call_method1("astype", ("U",))? };
    let list = arr.call_method0("tolist")?;
    list.cast_into::<PyList>().map_err(|_| pyo3::exceptions::PyTypeError::new_err("lane_flow_rows: a column"))?
        .iter().map(|v| py_str(&v)).collect()
}

/// roads._lane_flow_rows(f): (first and last coordinate (n, 4: lon, lat, lon, lat; 0 without any), coordinate count
/// (n,), attrs (n, 5): one-way sign or 0, the map's lanes or NaN, link, the class's default lanes or NaN, 1 for a
/// road the ramp lane rule checks). counts: roadlanes.generated_counts(f); links: roadlanes.links(f); defaults: the
/// LANE_FLOW_CLASSES' default lanes; the ramp lane rule's columns as _ramp_candidates reads them: ramp_widths() ->
/// (the width rule table, OSM's widths (n,) or fewer), roads.width_table / _osm_widths (R1j's width repair: a
/// right-of-way is no mapped width), asked only when a road is a candidate (width_rejected may read the home
/// cells); the turn:lanes columns, the lane tag parts.
#[pyfunction]
#[pyo3(signature = (part_off, ring_off, coord_off, lonlat, cls, subtype, counts, oneway, links, defaults, ramp_rule,
                    ramp_widths, turn_cols, parts))]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn lane_flow_rows<'py>(py: Python<'py>, part_off: CArr1<'py, i64>, ring_off: CArr1<'py, i64>,
                           coord_off: CArr1<'py, i64>, lonlat: CArr2<'py, f64>,
                           cls: Option<Bound<'py, PyAny>>, subtype: Option<Bound<'py, PyAny>>,
                           counts: Vec<Option<Bound<'py, PyAny>>>, oneway: Option<Bound<'py, PyAny>>,
                           links: CArr1<'py, bool>, defaults: BTreeMap<String, f64>, ramp_rule: bool,
                           ramp_widths: Bound<'py, PyAny>,
                           turn_cols: Vec<Option<Bound<'py, PyAny>>>, parts: Option<Bound<'py, PyAny>>)
                           -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<f64>>)> {
    let (po, ro, co) = (part_off.as_slice()?, ring_off.as_slice()?, coord_off.as_slice()?);
    let ll = lonlat.as_array();
    let n = po.len().saturating_sub(1);
    super::lanesplit::check_lonlat(ll.ncols())?;
    super::lanesplit::check_offsets(n, po, ro, co, ll.nrows())?;
    let mut pts = vec![0.0f64; n * 4];
    let mut npts = vec![0i64; n];
    for i in 0..n {
        let s = co[ro[po[i] as usize] as usize];
        let e = co[ro[po[i + 1] as usize] as usize];
        npts[i] = e - s;
        if e - s > 0 {
            pts[i * 4] = ll[[s as usize, 0]];
            pts[i * 4 + 1] = ll[[s as usize, 1]];
            pts[i * 4 + 2] = ll[[(e - 1) as usize, 0]];
            pts[i * 4 + 3] = ll[[(e - 1) as usize, 1]];
        }
    }
    let mut attrs = vec![0.0f64; n * 5];
    for i in 0..n {
        attrs[i * 5 + 1] = f64::NAN;
        attrs[i * 5 + 3] = f64::NAN;
    }
    let table = oneway.filter(|t| !t.is_none());
    if let (Some(cls_a), Some(table)) = (cls.as_ref(), table.as_ref()) {
        if n > 0 && table.getattr("feature")?.len()? > 0 {
            let cl = str_col(cls_a)?;
            let sub = match subtype.as_ref() {
                Some(s) if !s.is_none() => Some(str_col(s)?),
                _ => None,
            };
            // (numpy's shapes there: `sel &= subtype == "road"` broadcasts a one-value column and refuses another
            // length, `sel &= maj >= 0` needs the class column as long as the layer: ValueError)
            if let Some(s) = &sub {
                if s.len() != cl.len() && s.len() != 1 {
                    return Err(pyo3::exceptions::PyValueError::new_err(format!("operands could not be broadcast together with shapes ({},) ({},)",
                                             cl.len(), s.len())));
                }
            }
            if cl.len() != n {
                return Err(pyo3::exceptions::PyValueError::new_err(format!("operands could not be broadcast together with shapes ({},) ({},)",
                                         cl.len(), n)));
            }
            let road = |i: usize| -> bool {
                sub.as_ref().is_none_or(|s| s[if s.len() == 1 { 0 } else { i }] == "road")
            };
            let maj: CArr1<i64> = table.call_method1("majors", (n,))?.extract()?;
            let maj = maj.as_slice()?;
            let idx: Vec<usize> = (0..n).filter(|&i| defaults.contains_key(&cl[i]) && road(i) && maj[i] >= 0)
                .collect();
            if !idx.is_empty() {
                // (np.asarray(table.value)[maj[idx]].astype(np.float64): numpy's own conversion of any dtype)
                let np = py.import("numpy")?;
                let rows: Vec<i64> = idx.iter().map(|&i| maj[i]).collect();
                let v: Vec<f64> = np.getattr("asarray")?.call1((table.getattr("value")?,))?
                    .get_item(rows.into_pyarray(py))?.call_method1("astype", (np.getattr("float64")?,))?
                    .extract::<CArr1<f64>>()?.as_array().iter().copied().collect();
                let lk = links.as_slice()?;
                // (counts[i] for the counted roads in order, then links(f)[idx]: an IndexError past either)
                if let Some(&i) = idx.iter().find(|&&i| i >= counts.len()) {
                    return Err(pyo3::exceptions::PyIndexError::new_err(format!("list index out of range ({i})")));
                }
                if let Some(&i) = idx.iter().find(|&&i| i >= lk.len()) {
                    return Err(pyo3::exceptions::PyIndexError::new_err(format!("index {i} is out of bounds for axis 0 with size {}", lk.len())));
                }
                for (k, &i) in idx.iter().enumerate() {
                    attrs[i * 5] = if v[k] < 0.0 { -1.0 } else { 1.0 };
                    attrs[i * 5 + 1] = match &counts[i] {
                        Some(c) if !c.is_none() => {
                            let int = py.get_type::<pyo3::types::PyInt>().call1((c,))?;
                            int.extract::<f64>()?
                        }
                        _ => f64::NAN,
                    };
                    attrs[i * 5 + 2] = if lk[i] { 1.0 } else { 0.0 };
                    attrs[i * 5 + 3] = defaults[&cl[i]];
                }
                if ramp_rule {
                    let cand: Vec<usize> = idx.iter().copied().filter(|&i| cl[i] == "motorway" && lk[i]
                        && (if attrs[i * 5 + 1].is_nan() { 0.0 } else { attrs[i * 5 + 1] }) >= 3.0).collect();
                    if !cand.is_empty() {
                        // (width_table(f), then _osm_widths(f, None, None): the old order)
                        let got = ramp_widths.call0()?;
                        let wt = got.get_item(0)?;
                        let (has_w, _) = rule_cols_of(Some(&wt), n)?;
                        let ow: CArr1<f64> = got.get_item(1)?.extract()?;
                        let osm_w: Vec<f64> = ow.as_array().iter().copied().collect();
                        let parts = parts.filter(|p| !p.is_none());
                        let empty = PyString::new(py, "");
                        'cand: for &i in &cand {
                            // (_osm_values' first n values of a column that may be shorter: osm_w[i] past it is
                            // numpy's IndexError)
                            if i >= osm_w.len() && !has_w[i] {
                                return Err(pyo3::exceptions::PyIndexError::new_err(format!("index {i} is out of bounds for axis 0 with size {}",
                                                         osm_w.len())));
                            }
                            if has_w[i] || osm_w[i].is_finite() {
                                continue;
                            }
                            for c in turn_cols.iter().flatten() {
                                if c.is_none() {
                                    continue;
                                }
                                let v = c.get_item(i)?;
                                if !(v.is_none() || v.eq(&empty)?) {
                                    continue 'cand;
                                }
                            }
                            if let Some(parts) = parts.as_ref() {
                                let pi = parts.get_item(i)?;
                                if pi.is_truthy()? {
                                    for row in pi.try_iter()? {
                                        let t = row?.get_item(3)?;
                                        if t.is_truthy()? {
                                            for key in t.try_iter()? {
                                                if py_str(&key?)?.starts_with("turn:lanes") {
                                                    continue 'cand;
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                            attrs[i * 5 + 4] = 1.0;
                        }
                    }
                }
            }
        }
    }
    Ok((numpy::ndarray::Array2::from_shape_vec((n, 4), pts).unwrap().into_pyarray(py),
        npts.into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((n, 5), attrs).unwrap().into_pyarray(py)))
}

/// roads._lane_flow_local's lists from earthfast.lane_flow_cells' (got, ramp) of this cell's roads: (each road's
/// count, int(), None where not finite or a link (attrs' column 2); the ramp rule's count, None where not finite).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn lane_flow_lists<'py>(got: CArr1<'py, f64>, ramp: CArr1<'py, f64>,
                            attrs: CArr2<'py, f64>) -> PyResult<(Vec<Option<i64>>, Vec<Option<i64>>)> {
    let (g, r, a) = (got.as_slice()?, ramp.as_slice()?, attrs.as_array());
    // (isfinite(got) & ~(attrs[:, 2] > 0.5): one shape, ValueError for another)
    if a.ncols() < 3 {
        return Err(pyo3::exceptions::PyIndexError::new_err("lane_flow_lists: attrs of fewer than 3 columns"));
    }
    if a.nrows() != g.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "operands could not be broadcast together with shapes ({},) ({},)", g.len(), a.nrows())));
    }
    let out = (0..g.len()).map(|i| if g[i].is_finite() && !(a[[i, 2]] > 0.5) { Some(g[i] as i64) } else { None })
        .collect();
    let fixed = r.iter().map(|&v| if v.is_finite() { Some(v as i64) } else { None }).collect();
    Ok((out, fixed))
}
