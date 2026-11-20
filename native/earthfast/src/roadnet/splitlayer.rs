//! lanesections.split's new layer (lanesections._new_layer) in Rust, byte for byte the Python assembly it replaced: the
//! columns gathered to the jobs (every attrs column, the sections' profiles, R1j's LANE_MAX, the lane tag columns as
//! objects), the sections' own values, the geometry and rule rows (lanesections.rs, kernel 235's gathers), the cut
//! points' connector ids merged after each job's own rows, and the new Features. numpy's own conversions
//! (np.asarray / np.array of a column, a gather by `old`) are asked of numpy, so every dtype comes out as it did.

use numpy::IntoPyArray;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyList, PySlice, PyString};

use super::pyobj::unpack;
use crate::lanesections::{split_coords, split_rules};
use super::pyobj::{CArr1, CArr2};

/// lanesections._new_layer(f, jobs): the new layer of split's jobs [(fid, feature, a, b, line or None, tags, count,
/// profile)]. lane_evidence(): roads._lane_evidence(f) (asked where the Python asked it); lane_cols: (tag, attrs column)
/// of railtags.LANE_COLUMNS; lanes / placement: the lane_attr columns of "lanes" / "placement"; parts: LANE_PARTS;
/// features / rule_table: mapgeom.Features / RuleTable.
#[pyfunction]
#[pyo3(signature = (f, jobs, lane_evidence, profile, lane_max, lane_cols, lanes, placement, parts, features,
                    rule_table))]
#[allow(clippy::too_many_arguments)]
pub fn lane_split_layer<'py>(py: Python<'py>, f: Bound<'py, PyAny>, jobs: Bound<'py, PyList>,
                             lane_evidence: Bound<'py, PyAny>, profile: String, lane_max: String,
                             lane_cols: Vec<(String, String)>, lanes: String, placement: String, parts: String,
                             features: Bound<'py, PyAny>, rule_table: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let np = py.import("numpy")?;
    let builtins = py.import("builtins")?;
    let (int64, float64, int32) = (np.getattr("int64")?, np.getattr("float64")?, np.getattr("int32")?);
    let (object, str_t) = (builtins.getattr("object")?, builtins.getattr("str")?);
    let jl: Vec<Bound<'py, PyAny>> = jobs.iter().collect();
    let nj = jl.len();
    let none = py.None().into_bound(py);
    let filled = |v: &Bound<'py, PyAny>| -> PyResult<Bound<'py, PyList>> { PyList::new(py, vec![v.clone(); nj]) };

    // old = np.array([r[1] for r in jobs], np.int64)
    let ol = PyList::empty(py);
    for r in &jl {
        ol.append(r.get_item(1)?)?;
    }
    let old = np.call_method1("array", (ol, &int64))?;
    // attrs = {k: np.asarray(v)[old].copy() for k, v in f.attrs.items()}
    let attrs = PyDict::new(py);
    for kv in f.getattr("attrs")?.call_method0("items")?.try_iter()? {
        let (k, v): (Bound<'py, PyAny>, Bound<'py, PyAny>) = kv?.extract()?;
        attrs.set_item(k, np.call_method1("asarray", (v,))?.get_item(&old)?.call_method0("copy")?)?;
    }
    attrs.set_item(&profile, np.call_method1("full", (nj, &none, &object))?)?;
    // (each section, and every road kept whole, with its road's most lanes before the split clears its lane parts)
    let ev = lane_evidence.call0()?.get_item(&old)?;
    attrs.set_item(&lane_max, np.call_method1("ascontiguousarray", (ev, &int64))?)?;
    // (object columns: a section's tag strings may be longer than its road's)
    let or_default = |key: &str, d: &Bound<'py, PyAny>| -> PyResult<Bound<'py, PyAny>> {
        Ok(match attrs.get_item(key)? { Some(v) => v, None => filled(d)?.into_any() })
    };
    for (_, col) in &lane_cols {
        let v = or_default(col, &none)?;
        attrs.set_item(col, np.call_method1("asarray", (v, &object))?)?;
    }
    let v = or_default("src_record", &PyString::new(py, "").into_any())?;
    attrs.set_item("src_record", np.call_method1("asarray", (v, &object))?)?;
    let v = or_default("osm_lanes", &PyFloat::new(py, f64::NAN).into_any())?;
    attrs.set_item("osm_lanes", np.call_method1("asarray", (v, &float64))?)?;
    let v = or_default(&parts, &none)?;
    attrs.set_item(&parts, np.call_method1("asarray", (v, &object))?)?;

    // the geometry: each job a copy of its feature, or of its section's (or moved mouth's) line
    let mut moved = Vec::with_capacity(nj);
    for r in &jl {
        moved.push(!r.get_item(4)?.is_none());
    }
    let lines = PyList::empty(py);
    for r in &jl {
        let p = r.get_item(4)?;
        if !p.is_none() {
            lines.append(np.call_method1("asarray", (p, &float64))?.call_method1("reshape", (-1, 2))?)?;
        }
    }
    let mut p_off = vec![0i64];
    for r in &jl {
        let p = r.get_item(4)?;
        let k = if p.is_none() { 0 } else { p.len()? as i64 };
        p_off.push(p_off[p_off.len() - 1].wrapping_add(k));
    }
    // np.ascontiguousarray(np.concatenate(lines) if lines else np.zeros((0, 2)), np.float64): numpy's own reading of
    // each line, whatever its strides (rust-numpy's view of a byte-strided line read other bytes: Codex on a6b93359)
    let cat = if lines.is_empty() {
        np.call_method1("zeros", ((0, 2),))?
    } else {
        np.call_method1("concatenate", (lines,))?
    };
    let pts: CArr2<'py, f64> = np.call_method1("ascontiguousarray", (cat, &float64))?.extract()?;
    let i64col = |name: &str| -> PyResult<CArr1<'py, i64>> {
        np.call_method1("ascontiguousarray", (f.getattr(name)?, &int64))?.extract()
    };
    let (po, ro, co) = (i64col("part_off")?, i64col("ring_off")?, i64col("coord_off")?);
    let ll: CArr2<'py, f64> = np.call_method1("ascontiguousarray", (f.getattr("lonlat")?, &float64))?
        .call_method1("reshape", (-1, 2))?.extract()?;
    let old_a: CArr1<'py, i64> = old.extract()?;
    // (with the GIL held, as kernel 235's binding ran it: a copy of the layer's points, well under a millisecond on a
    // city cell, where a GIL handoff costs another thread's switch interval)
    let got = split_coords(po.as_array(), ro.as_array(), co.as_array(), ll.as_array(), old_a.as_array(),
                           numpy::ndarray::ArrayView1::from(&moved[..]), pts.as_array(),
                           numpy::ndarray::ArrayView1::from(&p_off[..]));
    let (npo, nro, nco, coords) = got.map_err(pyo3::exceptions::PyValueError::new_err)?;
    let m = coords.len() / 2;
    let coords = if m == 0 {
        np.call_method1("zeros", (0, &float64))?
    } else {
        numpy::ndarray::Array2::from_shape_vec((m, 2), coords).expect("coords").into_pyarray(py).into_any()
    };
    let fl = |k: usize| -> PyResult<CArr1<'py, f64>> {
        let l = PyList::empty(py);
        for r in &jl {
            l.append(r.get_item(k)?)?;
        }
        np.call_method1("array", (l, &float64))?.extract()
    };
    let (ja, jb) = (fl(2)?, fl(3)?);

    // each section's own values; the cut points' connectors (job, id, at)
    let zero = PyFloat::new(py, 0.0);
    let one = PyFloat::new(py, 1.0);
    let ids = f.getattr("ids")?;
    let format = builtins.getattr("format")?;
    let round = builtins.getattr("round")?;
    let mut extra: Vec<(i64, Bound<'py, PyAny>, f64)> = Vec::new();
    let col = |name: &str| -> PyResult<Bound<'py, PyAny>> {
        attrs.get_item(name)?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(name.to_owned()))
    };
    for (z, r) in jl.iter().enumerate() {
        let t = unpack(r, 8)?;
        let (i, a, b, tags, n, prof) = (&t[1], &t[2], &t[3], &t[5], &t[6], &t[7]);
        if prof.is_none() {
            continue;
        }
        for (at, value) in [(a, 0.0), (b, 1.0)] {
            if zero.lt(at)? && at.lt(&one)? {
                // f"lane:{f.ids[i]}:{round(at * 1e9)}"
                let fid = format.call1((ids.get_item(i)?, ""))?;
                let k = format.call1((round.call1((at.mul(1e9)?,))?, ""))?;
                // (the f-string's join of its pieces: str.join, no operator of a str subclass asked)
                let pieces = PyList::new(py, [PyString::new(py, "lane:").into_any(), fid,
                                              PyString::new(py, ":").into_any(), k])?;
                extra.push((z as i64, PyString::new(py, "").call_method1("join", (pieces,))?, value));
            }
        }
        col(&profile)?.set_item(z, prof)?;
        col("osm_lanes")?.set_item(z, n)?;
        // (the section's tags, rather than its road's turn arrows)
        for (key, c) in &lane_cols {
            col(c)?.set_item(z, tags.call_method1("get", (key,))?)?;
        }
        col(&lanes)?.set_item(z, n)?;
        if attrs.contains(&placement)? {
            col(&placement)?.set_item(z, &none)?;     // (in its line: prof["end_off"])
        }
        col(&parts)?.set_item(z, &none)?;
    }

    // the rule tables
    let rules = f.getattr("rules")?;
    let mut names: Vec<Bound<'py, PyAny>> = rules.try_iter()?.collect::<PyResult<_>>()?;
    if !rules.contains("connectors")? {
        names.push(PyString::new(py, "connectors").into_any());
    }
    let tables = PyDict::new(py);
    let (ov, av, bv) = (old_a.as_array(), ja.as_array(), jb.as_array());
    for name in names {
        let table = rules.call_method1("get", (&name,))?;
        type Taken<'py> = (Vec<i64>, Vec<f64>, Vec<f64>, Option<Bound<'py, PyAny>>);
        let (mut zs, mut us, mut vs, mut values): Taken<'py> = if !table.is_none() && table.len()? > 0 {
            let feat = i64col_of(&np, &table.getattr("feature")?, &int64)?;
            let st = f64col_of(&np, &table.getattr("start")?, &float64)?;
            let en = f64col_of(&np, &table.getattr("end")?, &float64)?;
            let got = split_rules(feat.as_array(), st.as_array(), en.as_array(), ov, av, bv);
            let (rows, z0, u0, v0) = got.map_err(pyo3::exceptions::PyValueError::new_err)?;
            let val = np.call_method1("asarray", (table.getattr("value")?,))?.get_item(rows.into_pyarray(py))?;
            (z0, u0, v0, Some(val))
        } else {
            (Vec::new(), Vec::new(), Vec::new(), None)
        };
        if name.eq("connectors")? {
            if !extra.is_empty() {
                // (after each job's own rows, in job order: np.lexsort's (job, own / cut, position))
                let k = zs.len();
                let mut order: Vec<usize> = (0..k + extra.len()).collect();
                let key = |q: usize| if q < k { (zs[q], 0u8, q) } else { (extra[q - k].0, 1u8, q - k) };
                order.sort_by_key(|&q| key(q));
                let vals = np.call_method1("empty", (k + extra.len(), &object))?;
                if k > 0 {
                    let own = PyList::new(py, values.as_ref().expect("values").try_iter()?.collect::<PyResult<Vec<_>>>()?)?;
                    vals.set_item(PySlice::new(py, 0, k as isize, 1), own)?;
                }
                let cut = PyList::new(py, extra.iter().map(|e| e.1.clone()))?;
                vals.set_item(PySlice::new(py, k as isize, (k + extra.len()) as isize, 1), cut)?;
                let ez: Vec<i64> = extra.iter().map(|e| e.0).collect();
                let ev: Vec<f64> = extra.iter().map(|e| e.2).collect();
                let all_z: Vec<i64> = zs.iter().chain(&ez).copied().collect();
                let all_u: Vec<f64> = us.iter().chain(&ev).copied().collect();
                let all_v: Vec<f64> = vs.iter().chain(&ev).copied().collect();
                zs = order.iter().map(|&q| all_z[q]).collect();
                us = order.iter().map(|&q| all_u[q]).collect();
                vs = order.iter().map(|&q| all_v[q]).collect();
                let ord: Vec<i64> = order.iter().map(|&q| q as i64).collect();
                values = Some(vals.get_item(ord.into_pyarray(py))?);
            }
            if !zs.is_empty() {
                let l = PyList::new(py, values.as_ref().expect("values").try_iter()?.collect::<PyResult<Vec<_>>>()?)?;
                let kw = PyDict::new(py);
                kw.set_item("dtype", &str_t)?;
                values = Some(np.getattr("array")?.call((l,), Some(&kw))?);
            }
        }
        if zs.is_empty() {
            let keep = rules.contains(&name)? && rules.get_item(&name)?.len()? == 0;
            let t = if keep {
                rules.get_item(&name)?
            } else {
                rule_table.call1((np.call_method1("zeros", (0, &int32))?, np.call_method1("zeros", (0, &str_t))?,
                                  np.call_method1("zeros", (0,))?, np.call_method1("zeros", (0,))?))?
            };
            tables.set_item(&name, t)?;
            continue;
        }
        let z32: Vec<i32> = zs.iter().map(|&z| z as i32).collect();
        tables.set_item(&name, rule_table.call1((z32.into_pyarray(py), values.expect("values"), us.into_pyarray(py),
                                                 vs.into_pyarray(py)))?)?;
    }

    // the new layer
    let layer = f.getattr("layer")?;
    let il = PyList::empty(py);
    for r in &jl {
        il.append(r.get_item(0)?)?;
    }
    let new_ids = np.call_method1("array", (il,))?;
    let kind = f.getattr("kind")?.get_item(&old)?;
    let gather = |name: &str| -> PyResult<Bound<'py, PyAny>> {
        let v = f.getattr(name)?;
        if v.is_none() { Ok(v) } else { v.get_item(&old) }
    };
    let (own, clipped) = (gather("own")?, gather("clipped")?);
    let kw = PyDict::new(py);
    kw.set_item("own", own)?;
    kw.set_item("clipped", clipped)?;
    kw.set_item("attrs", attrs)?;
    kw.set_item("rules", tables)?;
    features.call((layer, new_ids, kind, npo.into_pyarray(py), nro.into_pyarray(py), nco.into_pyarray(py), coords),
                  Some(&kw))
}

fn i64col_of<'py>(np: &Bound<'py, PyModule>, v: &Bound<'py, PyAny>, t: &Bound<'py, PyAny>)
                  -> PyResult<CArr1<'py, i64>> {
    np.call_method1("ascontiguousarray", (v, t))?.extract()
}

fn f64col_of<'py>(np: &Bound<'py, PyModule>, v: &Bound<'py, PyAny>, t: &Bound<'py, PyAny>)
                  -> PyResult<CArr1<'py, f64>> {
    np.call_method1("ascontiguousarray", (v, t))?.extract()
}
