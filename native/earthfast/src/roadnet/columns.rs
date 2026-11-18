//! A road layer's columns as the spec kernels read them (roads._spec_inputs' per-feature columns, roads._rule_cols,
//! roads._osm_values, roads._lengths, roads.cycle_streets), byte for byte the numpy / Python they replaced.

use std::collections::BTreeMap;

use numpy::{IntoPyArray, PyArray1};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use super::pyobj::{items, lower_is, py_float, py_float_strict, py_str, py_strip};
use crate::dm::Dm;
use super::pyobj::{CArr1, CArr2};

/// roads.SIDEWALK_TAG's keys by the kernels' side code (roads._SIDES_OF: both, left, right, none).
fn sidewalk_code(v: &Bound<'_, PyAny>) -> PyResult<Option<i8>> {
    let s = py_str(v)?;
    let s = py_strip(&s);
    Ok(if lower_is(s, "both") {
        Some(0)
    } else if lower_is(s, "left") {
        Some(1)
    } else if lower_is(s, "right") {
        Some(2)
    } else if lower_is(s, "no") || lower_is(s, "none") || lower_is(s, "separate") {
        Some(3)
    } else {
        None
    })
}

/// roads.cycle_streets: a cycleway with its pavements mapped on it (SIDEWALK_TAG both / left / right).
fn cycle_street_flags(n: usize, cls: Option<&Bound<'_, PyAny>>, tags: Option<&Bound<'_, PyAny>>) -> PyResult<Vec<bool>> {
    let mut out = vec![false; n];
    let (Some(cls), Some(tags)) = (items(cls)?, items(tags)?) else { return Ok(out) };
    for (i, (c, v)) in cls.iter().zip(tags.iter()).enumerate() {
        if i >= n {
            return Err(pyo3::exceptions::PyIndexError::new_err(format!("index {i} is out of bounds for axis 0 with size {n}")));
        }
        if py_str(c)? == "cycleway" && matches!(sidewalk_code(v)?, Some(0..=2)) {
            out[i] = true;
        }
    }
    Ok(out)
}

/// roads.cycle_streets(feats) given its class and osm_sidewalk columns (None without): (n,) bool.
#[pyfunction]
#[pyo3(signature = (n, cls, sidewalk))]
pub fn cycle_streets<'py>(py: Python<'py>, n: usize, cls: Option<Bound<'py, PyAny>>, sidewalk: Option<Bound<'py, PyAny>>)
                          -> PyResult<Bound<'py, PyArray1<bool>>> {
    Ok(cycle_street_flags(n, cls.as_ref(), sidewalk.as_ref())?.into_pyarray(py))
}

/// roads._rule_cols(table, n): (has (n,) bool, value (n,) float64, NaN without) of the rule row covering the most of
/// each feature (table.majors(n)), float() of its value (any error raised, as there).
#[pyfunction]
pub fn rule_cols<'py>(py: Python<'py>, table: Option<Bound<'py, PyAny>>, n: usize)
                      -> PyResult<(Bound<'py, PyArray1<bool>>, Bound<'py, PyArray1<f64>>)> {
    let (has, val) = rule_cols_of(table.as_ref(), n)?;
    Ok((has.into_pyarray(py), val.into_pyarray(py)))
}

pub fn rule_cols_of(table: Option<&Bound<'_, PyAny>>, n: usize) -> PyResult<(Vec<bool>, Vec<f64>)> {
    let mut has = vec![false; n];
    let mut val = vec![f64::NAN; n];
    let Some(table) = table.filter(|t| !t.is_none()) else { return Ok((has, val)) };
    if table.getattr("feature")?.len()? == 0 || n == 0 {
        return Ok((has, val));
    }
    let maj: CArr1<i64> = table.call_method1("majors", (n,))?.extract()?;
    let maj = maj.as_slice()?;
    let value = table.getattr("value")?;
    for i in 0..n {
        if maj[i] >= 0 {
            has[i] = true;
            val[i] = py_float_strict(&value.get_item(maj[i])?)?;
        }
    }
    Ok((has, val))
}

/// roads._osm_values: float() of each feature's value of an osm_ column (NaN: no column, not a number (TypeError /
/// ValueError), not finite, under lo or over hi); the column's first n values.
#[pyfunction]
#[pyo3(signature = (col, n, lo, hi))]
pub fn osm_values<'py>(py: Python<'py>, col: Option<Bound<'py, PyAny>>, n: usize, lo: Option<f64>, hi: Option<f64>)
                       -> PyResult<Bound<'py, PyArray1<f64>>> {
    Ok(osm_values_of(col.as_ref(), n, lo, hi)?.into_pyarray(py))
}

pub fn osm_values_of(col: Option<&Bound<'_, PyAny>>, n: usize, lo: Option<f64>, hi: Option<f64>) -> PyResult<Vec<f64>> {
    let Some(vals) = items(col)? else { return Ok(vec![f64::NAN; n]) };
    let mut out = Vec::with_capacity(n.min(vals.len()));
    for v in vals.iter().take(n) {
        let x = py_float(v)?.unwrap_or(f64::NAN);
        let bad = !x.is_finite() || lo.is_some_and(|l| x < l) || hi.is_some_and(|h| x > h);
        out.push(if bad { f64::NAN } else { x });
    }
    Ok(out)
}

/// roads._lengths(feats): each feature's length (m; lon scaled by the cos of its first point's latitude), NaN under
/// two points: numpy's arithmetic over the layer's points in order (each feature's segments summed in order, as
/// np.bincount's weights; segments across its parts' gaps counted, as there).
#[pyfunction]
pub fn layer_lengths<'py>(py: Python<'py>, part_off: CArr1<'py, i64>, ring_off: CArr1<'py, i64>,
                          coord_off: CArr1<'py, i64>, lonlat: CArr2<'py, f64>)
                          -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (po, ro, co) = (part_off.as_slice()?, ring_off.as_slice()?, coord_off.as_slice()?);
    let ll = lonlat.as_array();
    let n = po.len().saturating_sub(1);
    let mut out = vec![f64::NAN; n];
    if n == 0 {
        return Ok(out.into_pyarray(py));
    }
    super::lanesplit::check_lonlat(ll.ncols())?;
    super::lanesplit::check_offsets(n, po, ro, co, ll.nrows())?;
    let a: Vec<i64> = (0..n).map(|i| co[ro[po[i] as usize] as usize]).collect();
    let b: Vec<i64> = (0..n).map(|i| co[ro[po[i + 1] as usize] as usize]).collect();
    let cnt: Vec<i64> = a.iter().zip(&b).map(|(x, y)| y - x).collect();
    if cnt.iter().sum::<i64>() < 2 {
        return Ok(out.into_pyarray(py));
    }
    let m = 111320.0f64;
    let np_ = ll.nrows() as i64;
    let c: Vec<f64> = a.iter().map(|&x| ll[[x.min(np_ - 1) as usize, 1]].to_radians().dcos()).collect();
    // fid = repeat(arange(n), cnt); pos = arange(a[0], b[-1])
    let mut fid: Vec<usize> = Vec::new();
    for (i, &k) in cnt.iter().enumerate() {
        for _ in 0..k.max(0) {
            fid.push(i);
        }
    }
    let p0 = a[0];
    let len = (b[n - 1] - p0).max(0) as usize;
    if fid.len() != len {
        return Err(pyo3::exceptions::PyValueError::new_err("layer_lengths: the features' points are not contiguous"));
    }
    let mut l = vec![0.0f64; n];
    for k in 0..len.saturating_sub(1) {
        if fid[k + 1] != fid[k] {
            continue;
        }
        let (q0, q1) = ((p0 as usize) + k, (p0 as usize) + k + 1);
        let dx = (ll[[q1, 0]] - ll[[q0, 0]]) * m * c[fid[k]];
        let dy = (ll[[q1, 1]] - ll[[q0, 1]]) * m;
        l[fid[k]] += dx.dhypot(dy);
    }
    for i in 0..n {
        if cnt[i] >= 2 {
            out[i] = l[i];
        }
    }
    Ok(out.into_pyarray(py))
}

/// roads._spec_inputs' per-feature columns from the layer's attrs: {"cls" int32 (STYLE's order, -1 not a style),
/// "elig" bool (a road subtype of a known class, built (not unbuilt), a cycleway only as a street; "elig_pre": before
/// the cycleway test, as the ring radii and the photo surfaces read it), "ring" bool,
/// "good" bool (smoothness excellent / good / intermediate), "name" int64 codes by first appearance (-1 unnamed) or
/// None without the column, "sides" int8 (SIDEWALK_TAG's code, -1), "ncon" int64 (connector rows per feature) or None}.
#[pyfunction]
#[pyo3(signature = (n, names, unbuilt, cls, subtype, junction, smoothness, name, sidewalk, connectors))]
#[allow(clippy::too_many_arguments)]
pub fn spec_columns<'py>(py: Python<'py>, n: usize, names: Vec<String>, unbuilt: CArr1<'py, bool>,
                         cls: Option<Bound<'py, PyAny>>, subtype: Option<Bound<'py, PyAny>>,
                         junction: Option<Bound<'py, PyAny>>, smoothness: Option<Bound<'py, PyAny>>,
                         name: Option<Bound<'py, PyAny>>, sidewalk: Option<Bound<'py, PyAny>>,
                         connectors: Option<CArr1<'py, i64>>) -> PyResult<Bound<'py, PyDict>> {
    let pos = |c: &str| names.iter().position(|x| x == c).map(|k| k as i32);
    let unbuilt = unbuilt.as_slice()?;
    let code: Vec<i32> = match items(cls.as_ref())? {
        Some(v) => v.iter().take(n).map(|x| Ok(pos(&py_str(x)?).unwrap_or(-1))).collect::<PyResult<_>>()?,
        None => vec![pos("unknown").unwrap_or(-1); n],
    };
    let short = |what: &str| -> PyResult<()> {
        Err(pyo3::exceptions::PyValueError::new_err(format!("spec_columns: a {what} column shorter than the layer")))
    };
    if unbuilt.len() < n {
        short("unbuilt")?;
    }
    if code.len() != n {
        return Err(pyo3::exceptions::PyValueError::new_err("spec_columns: a class column shorter than the layer"));
    }
    let road: Vec<bool> = match items(subtype.as_ref())? {
        Some(v) => v.iter().take(n).map(|x| Ok(py_str(x)? == "road")).collect::<PyResult<_>>()?,
        None => vec![true; n],
    };
    if road.len() != n {
        short("subtype")?;
    }
    let mut elig: Vec<bool> = (0..n).map(|i| road[i] && code[i] >= 0 && !unbuilt[i]).collect();
    let ring: Vec<bool> = match items(junction.as_ref())? {
        Some(v) => v.iter().take(n).map(|x| { let s = py_str(x)?; Ok(s == "roundabout" || s == "circular") })
            .collect::<PyResult<_>>()?,
        None => vec![false; n],
    };
    let good: Vec<bool> = match items(smoothness.as_ref())? {
        Some(v) => v.iter().take(n).map(|x| {
            let s = py_str(x)?;
            let s = py_strip(&s);
            Ok(lower_is(s, "excellent") || lower_is(s, "good") || lower_is(s, "intermediate"))
        }).collect::<PyResult<_>>()?,
        None => vec![false; n],
    };
    // (np.fromiter(..., n): a column shorter than the layer is a ValueError)
    if ring.len() != n {
        short("osm_junction")?;
    }
    if good.len() != n {
        short("osm_smoothness")?;
    }
    // (name[i] = / sides[i] = for every value of the column: one past the layer that would be set is numpy's
    // IndexError)
    let past = |i: usize| -> PyResult<()> {
        Err(pyo3::exceptions::PyIndexError::new_err(format!("index {i} is out of bounds for axis 0 with size {n}")))
    };
    let names_code = match items(name.as_ref())? {
        Some(v) => {
            let mut codes: BTreeMap<String, i64> = BTreeMap::new();
            let mut out = vec![-1i64; n];
            for (i, x) in v.iter().enumerate() {
                // str(v or "").strip()
                let s = if x.is_truthy()? { py_str(x)? } else { String::new() };
                let s = py_strip(&s);
                if !s.is_empty() && s != "None" {
                    if i >= n {
                        past(i)?;
                    }
                    let k = codes.len() as i64;
                    out[i] = *codes.entry(s.to_owned()).or_insert(k);
                }
            }
            Some(out)
        }
        None => None,
    };
    let mut sides = vec![-1i8; n];
    if let Some(v) = items(sidewalk.as_ref())? {
        for (i, x) in v.iter().enumerate() {
            if let Some(c) = sidewalk_code(x)? {
                if i >= n {
                    past(i)?;
                }
                sides[i] = c;
            }
        }
    }
    let elig_pre = elig.clone();
    // (a cycleway is a street only with its pavements mapped on it: STYLE's "cycleway")
    if let Some(cyc) = pos("cycleway") {
        if code.contains(&cyc) {
            let streets = cycle_street_flags(n, cls.as_ref(), sidewalk.as_ref())?;
            for i in 0..n {
                elig[i] &= code[i] != cyc || streets[i];
            }
        }
    }
    let ncon = match connectors {
        Some(f) if !f.as_slice()?.is_empty() => {
            let f = f.as_slice()?;
            let mut out = vec![0i64; n];
            for &k in f {
                if k < 0 {
                    return Err(pyo3::exceptions::PyValueError::new_err("spec_columns: a negative connector row"));
                }
                if (k as usize) < n {
                    out[k as usize] += 1;
                }
            }
            Some(out)
        }
        _ => None,
    };
    let d = PyDict::new(py);
    d.set_item("cls", code.into_pyarray(py))?;
    d.set_item("elig_pre", elig_pre.into_pyarray(py))?;
    d.set_item("elig", elig.into_pyarray(py))?;
    d.set_item("ring", ring.into_pyarray(py))?;
    d.set_item("good", good.into_pyarray(py))?;
    d.set_item("name", names_code.map(|v| v.into_pyarray(py)))?;
    d.set_item("sides", sides.into_pyarray(py))?;
    d.set_item("ncon", ncon.map(|v| v.into_pyarray(py)))?;
    Ok(d)
}
