//! A road layer's lane evidence (roads._lane_evidence) and its mapped widths' repair (roads.width_rejected: the
//! road revamp's WIDTH repair, section 2.2) in Rust, byte for byte the Python they replaced. The rule kernel
//! (RoadRules.width_rejects), the home copies (roads.home_copy: mapdata's neighbour hook), the layer's oneway / link /
//! OSM width columns and each layer's lane evidence (its memo) stay callbacks, asked where the Python asked them.

use std::collections::HashMap;

use numpy::{IntoPyArray, PyArray1};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use super::countries::np_index;
use super::pyobj::{in_generator, py_float, py_float_strict, py_str, CArr1};

const MAX_LANES: f64 = 12.0;

/// roadlanes.count(v, minimum): a whole lane count minimum..12 (float() of it: TypeError / ValueError none), None for
/// anything else.
fn count(v: &Bound<'_, PyAny>, minimum: f64) -> PyResult<Option<i64>> {
    Ok(match py_float(v)? {
        Some(n) if n.is_finite() && n.fract() == 0.0 && minimum <= n && n <= MAX_LANES => Some(n as i64),
        _ => None,
    })
}

/// A column read as Python's c[i], at the point the Python read it (no snapshot: an element's __float__ or a
/// subclass's __getitem__ may change what a later read gives, as it did there; Codex on bc34313f): an exact list's or
/// tuple's item directly (their indexing calls nothing back), c[i] of anything else.
enum Col<'py> {
    List(Bound<'py, PyList>),
    Tuple(Bound<'py, PyTuple>),
    Obj(Bound<'py, PyAny>),
}

impl<'py> Col<'py> {
    fn new(c: &Bound<'py, PyAny>) -> PyResult<Col<'py>> {
        if let Ok(l) = c.cast_exact::<PyList>() {
            return Ok(Col::List(l.clone()));
        }
        if let Ok(t) = c.cast_exact::<PyTuple>() {
            return Ok(Col::Tuple(t.clone()));
        }
        Ok(Col::Obj(c.clone()))
    }

    fn get(&self, i: usize) -> PyResult<Bound<'py, PyAny>> {
        let past = || pyo3::exceptions::PyIndexError::new_err("list index out of range");
        match self {
            Col::List(l) => if i < l.len() { l.get_item(i) } else { Err(past()) },
            Col::Tuple(t) => if i < t.len() { t.get_item(i) } else { Err(past()) },
            Col::Obj(c) => c.get_item(i),
        }
    }
}

fn cols<'py>(v: Vec<Option<Bound<'py, PyAny>>>) -> PyResult<Vec<Option<Col<'py>>>> {
    v.into_iter().map(|c| c.filter(|c| !c.is_none()).map(|c| Col::new(&c)).transpose()).collect()
}

/// roads._lane_evidence's loop: (n,) int64, the most lanes any of the map's lane data gives each feature (-1: none):
/// counts (osm_lanes, the lane tags' total, lane_count, LANE_MAX: count(c[i])), gen[i] (roadlanes.generated_counts:
/// None or a count), the directions' sum (lanes:forward + backward + both_ways, each count(d[i], 0), where any is
/// non-zero), and each lane part's (parts[i]: a list / tuple of (a, b, way, tags) 4-sequences whose tags is a
/// non-empty dict: count(tags.get("lanes")), the directions' sum alike), in that order per feature.
#[pyfunction]
pub fn lane_evidence<'py>(py: Python<'py>, n: usize, counts: Vec<Option<Bound<'py, PyAny>>>, gen: Bound<'py, PyAny>,
                          dirs: Vec<Option<Bound<'py, PyAny>>>, parts: Option<Bound<'py, PyAny>>)
                          -> PyResult<Bound<'py, PyArray1<i64>>> {
    if dirs.len() != 3 {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "lane_evidence: three direction columns (lanes:forward, lanes:backward, lanes:both_ways), not {}",
            dirs.len())));
    }
    let cs = cols(counts)?;
    let ds = cols(dirs)?;
    let gen = Col::new(&gen)?;
    let parts = parts.filter(|p| !p.is_none()).map(|p| Col::new(&p)).transpose()?;
    let keys = ["lanes:forward", "lanes:backward", "lanes:both_ways"];
    let mut ev = vec![-1i64; n];
    for i in 0..n {
        let mut take = |c: Option<i64>| {
            if let Some(c) = c {
                if c > ev[i] {
                    ev[i] = c;
                }
            }
        };
        for c in cs.iter().flatten() {
            take(count(&c.get(i)?, 1.0)?);
        }
        let g = gen.get(i)?;
        if !g.is_none() {
            take(count(&g, 1.0)?);
        }
        let mut tot = [None; 3];
        for (k, d) in ds.iter().enumerate() {
            if let Some(d) = d {
                tot[k] = count(&d.get(i)?, 0.0)?;
            }
        }
        if tot.iter().any(|t| matches!(t, Some(v) if *v != 0)) {
            take(count(&tot.iter().map(|t| t.unwrap_or(0)).sum::<i64>().into_pyobject(py)?.into_any(), 1.0)?);
        }
        let Some(parts) = &parts else { continue };
        let ps = parts.get(i)?;
        if !(ps.is_instance_of::<PyList>() || ps.is_instance_of::<PyTuple>()) {
            continue;
        }
        for p in ps.try_iter()? {
            let p = p?;
            let seq = p.is_instance_of::<PyList>() || p.is_instance_of::<PyTuple>();
            if !(seq && p.len()? == 4 && p.get_item(3)?.is_instance_of::<PyDict>()) {
                continue;
            }
            let tags = p.get_item(3)?;
            if !tags.is_truthy()? {
                continue;
            }
            take(count(&tags.call_method1("get", ("lanes",))?, 1.0)?);
            let mut t = [None; 3];
            for (k, key) in keys.iter().enumerate() {
                t[k] = count(&tags.call_method1("get", (*key,))?, 0.0)?;
            }
            if t.iter().any(|x| matches!(x, Some(v) if *v != 0)) {
                take(count(&t.iter().map(|x| x.unwrap_or(0)).sum::<i64>().into_pyobject(py)?.into_any(), 1.0)?);
            }
        }
    }
    Ok(ev.into_pyarray(py))
}

/// numpy's fromiter(..., np.int32, n) of an iterator of `len` items: ValueError when it is shorter.
fn too_short() -> PyErr {
    pyo3::exceptions::PyValueError::new_err("iterator too short")
}

/// width_rejected's body past its early returns (roads.width_rejected): (rule rows (nw,) bool, OSM (n,) bool). cls:
/// STYLE's index of str(class) (names: STYLE's keys, the last of a repeated one) where there is no subtype or
/// str(subtype) == "road", else -1 (has_class false: "class" not in attrs, every road "unknown"); none of STYLE: none
/// rejected. Then oneway_fn() (the oneway rule's has mask), links_fn() (as bool), rules_fn() (RoadRules) and the
/// values: the width rule's (wt: its features, float64 values: astype of a number dtype, float() of each other one),
/// OSM's (osm_fn(), where has_osm). A value is a candidate where the rule rejects it with one lane (maybe); with none,
/// no lanes asked. Each candidate's lanes: lane_evidence(home)[j] of home_copy(feats, i) -> (home, j), else
/// lane_evidence(feats)[i] (asked once, before the first). Tested: candidates with lanes, or trunks (not links) without,
/// not one-way; with none left, none rejected; else each source's rule rejects with those lanes, among them.
#[pyfunction]
#[pyo3(signature = (feats, n, names, has_class, cls_col, sub_col, oneway_fn, links_fn, rules_fn, wt, has_osm, osm_fn, factor,
                    lane_evidence, home_copy))]
#[allow(clippy::too_many_arguments)]
pub fn width_rejected<'py>(py: Python<'py>, feats: Bound<'py, PyAny>, n: usize, names: Vec<String>,
                           has_class: bool, cls_col: Bound<'py, PyAny>, sub_col: Option<Bound<'py, PyAny>>,
                           oneway_fn: Bound<'py, PyAny>, links_fn: Bound<'py, PyAny>, rules_fn: Bound<'py, PyAny>,
                           wt: Option<Bound<'py, PyAny>>, has_osm: bool, osm_fn: Bound<'py, PyAny>, factor: f64,
                           lane_evidence: Bound<'py, PyAny>, home_copy: Bound<'py, PyAny>)
                           -> PyResult<(Bound<'py, PyArray1<bool>>, Bound<'py, PyArray1<bool>>)> {
    let np = py.import("numpy")?;
    let nw = match &wt { Some(t) => t.getattr("feature")?.len()?, None => 0 };
    let zeros = |k: usize| vec![false; k].into_pyarray(py);
    let out = || (zeros(nw), zeros(n));
    let mut pos: HashMap<&str, i32> = HashMap::new();
    for (k, c) in names.iter().enumerate() {
        pos.insert(c.as_str(), k as i32);
    }
    // cls_s = [str(v) for v in a["class"].tolist()] if "class" in a else ["unknown"] * n
    let cls_s: Vec<String> = if has_class {
        cls_col.call_method0("tolist")?.try_iter()?.map(|v| py_str(&v?)).collect::<PyResult<_>>()?
    } else {
        vec!["unknown".to_owned(); n]
    };
    let sub: Option<Vec<Bound<'py, PyAny>>> = match sub_col.filter(|s| !s.is_none()) {
        Some(s) => Some(s.call_method0("tolist")?.try_iter()?.collect::<PyResult<_>>()?),
        None => None,
    };
    let pairs = match &sub { Some(s) => cls_s.len().min(s.len()), None => cls_s.len() };
    let mut cls = Vec::with_capacity(n);
    for k in 0..n {
        if k >= pairs {
            return Err(too_short());
        }
        // (str(s) in fromiter's generator: a StopIteration from it the generator's RuntimeError)
        let road = match &sub { Some(s) => in_generator(py, py_str(&s[k]))? == "road", None => true };
        cls.push(if road { pos.get(cls_s[k].as_str()).copied().unwrap_or(-1) } else { -1 });
    }
    if !cls.iter().any(|&c| c >= 0) {
        return Ok(out());
    }
    let cls = cls.into_pyarray(py);
    let oneway = oneway_fn.call0()?;
    let link = np.call_method1("asarray", (links_fn.call0()?, py.get_type::<pyo3::types::PyBool>()))?;
    let rr = rules_fn.call0()?;
    let int64 = np.getattr("int64")?;
    let float64 = np.getattr("float64")?;
    let mut srcs: Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)> = Vec::new();
    if nw > 0 {
        let t = wt.as_ref().expect("wt");
        let wv = np.call_method1("asarray", (t.getattr("value")?,))?;
        let kind: String = wv.getattr("dtype")?.getattr("kind")?.extract()?;
        let wv = if "fiub".contains(kind.as_str()) && kind.len() == 1 {
            wv.call_method1("astype", (&float64,))?
        } else {
            let v: Vec<f64> = wv.call_method0("tolist")?.try_iter()?.map(|x| py_float_strict(&x?))
                .collect::<PyResult<_>>()?;
            np.call_method1("array", (v, &float64))?
        };
        srcs.push((np.call_method1("ascontiguousarray", (t.getattr("feature")?, &int64))?,
                   np.call_method1("ascontiguousarray", (wv,))?));
    }
    if has_osm {
        let f = (0..n as i64).collect::<Vec<i64>>().into_pyarray(py).into_any();
        srcs.push((f, np.call_method1("ascontiguousarray", (osm_fn.call0()?,))?));
    }
    // (a width the fewest lanes allow is kept whatever the lanes: only the rest ask for lane evidence)
    let reject = |lanes: &Bound<'py, PyAny>, f: &Bound<'py, PyAny>, v: &Bound<'py, PyAny>| -> PyResult<Vec<bool>> {
        let got = rr.call_method1("width_rejects", (&cls, &oneway, &link, lanes, f, v, factor))?;
        let got: CArr1<'py, bool> = np.call_method1("asarray", (got, py.get_type::<pyo3::types::PyBool>()))?.extract()?;
        Ok(got.as_slice()?.to_vec())
    };
    let feat_of = |f: &Bound<'py, PyAny>| -> PyResult<Vec<i64>> {
        let a: CArr1<'py, i64> = f.extract()?;
        Ok(a.as_slice()?.to_vec())
    };
    let one = vec![1i64; n].into_pyarray(py).into_any();
    let mut maybe = vec![false; n];
    for (f, v) in &srcs {
        let hit = reject(&one, f, v)?;
        let fv = feat_of(f)?;
        for (k, &h) in hit.iter().enumerate() {
            if h {
                maybe[np_index(n, *fv.get(k).ok_or_else(|| pyo3::exceptions::PyIndexError::new_err(
                    "boolean index did not match indexed array"))?)?] = true;
            }
        }
    }
    if !maybe.iter().any(|&m| m) {
        return Ok(out());
    }
    let mut lanes = vec![-1i64; n];
    let own = lane_evidence.call1((&feats,))?;
    let int = py.get_type::<pyo3::types::PyInt>();
    for i in 0..n {
        if !maybe[i] {
            continue;
        }
        let h = home_copy.call1((&feats, i))?;
        let v = if h.is_none() {
            int.call1((own.get_item(i)?,))?
        } else {
            int.call1((lane_evidence.call1((h.get_item(0)?,))?.get_item(h.get_item(1)?)?,))?
        };
        lanes[i] = v.extract()?;
    }
    // (no lane data anywhere on the road: only a trunk is tested, as two lanes; a one-way carriageway keeps its width)
    let link_v: CArr1<'py, bool> = link.extract()?;
    let ow_v: CArr1<'py, bool> = np.call_method1("asarray", (&oneway, py.get_type::<pyo3::types::PyBool>()))?.extract()?;
    let (lk, ow) = (link_v.as_slice()?, ow_v.as_slice()?);
    if lk.len() != n || ow.len() != n {
        return Err(pyo3::exceptions::PyValueError::new_err("operands could not be broadcast together"));
    }
    for i in 0..n {
        let guess = cls_s[i] == "trunk" && !lk[i];
        maybe[i] = maybe[i] && (lanes[i] > 0 || guess) && !ow[i];
    }
    if !maybe.iter().any(|&m| m) {
        return Ok(out());
    }
    let lanes = lanes.into_pyarray(py).into_any();
    let mut res = Vec::new();
    for (f, v) in &srcs {
        let bad = reject(&lanes, f, v)?;
        let fv = feat_of(f)?;
        let mut r = Vec::with_capacity(bad.len());
        for (k, &b) in bad.iter().enumerate() {
            let m = maybe[np_index(n, fv[k])?];
            r.push(b && m);
        }
        res.push(r.into_pyarray(py));
    }
    let mut it = res.into_iter();
    let rule_bad = if nw > 0 { it.next().expect("rule") } else { zeros(nw) };
    let osm_bad = if has_osm { it.next().expect("osm") } else { zeros(n) };
    Ok((rule_bad, osm_bad))
}
