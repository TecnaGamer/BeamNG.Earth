//! A road layer's lane evidence (roads._lane_evidence) and its mapped widths' repair (roads.width_rejected: the
//! road revamp's WIDTH repair, section 2.2) in Rust, byte for byte the Python they replaced. The rule kernel
//! (RoadRules.width_rejects), the home copies (roads.home_copy: mapdata's neighbour hook), the layer's oneway / link /
//! OSM width columns and each layer's lane evidence (its memo) stay callbacks, asked where the Python asked them.

use std::collections::HashMap;

use numpy::{IntoPyArray, PyArray1};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use super::pyobj::{in_generator, key_str, py_float, py_float_strict, Contract};

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
    // (the input contract, checked before any row is read: pyobj::Contract)
    let k = Contract::new(py)?;
    for (j, c) in counts.iter().enumerate() {
        if let Some(c) = c {
            k.column(c, ["osm_lanes", "osm_lane_total", "lane_count", "lane_evidence_max"].get(j).unwrap_or(&"a count"))?;
        }
    }
    for d in dirs.iter().flatten() {
        k.column(d, "a lane direction column")?;
    }
    k.column(&gen, "generated_counts")?;
    if let Some(p) = &parts {
        k.parts(p, "osm_lane_parts")?;
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
        // (`if gen[i] is not None: take(i, gen[i])`: read twice, as the Python did)
        if !gen.get(i)?.is_none() {
            take(count(&gen.get(i)?, 1.0)?);
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
#[pyo3(signature = (feats, n, names, attrs, oneway_fn, links_fn, rules_fn, wt, nw, ow, osm_fn, factor,
                    lane_evidence, home_copy))]
#[allow(clippy::too_many_arguments)]
pub fn width_rejected<'py>(py: Python<'py>, feats: Bound<'py, PyAny>, n: usize, names: Vec<String>,
                           attrs: Bound<'py, PyAny>, oneway_fn: Bound<'py, PyAny>, links_fn: Bound<'py, PyAny>,
                           rules_fn: Bound<'py, PyAny>, wt: Option<Bound<'py, PyAny>>, nw: usize,
                           ow: Option<Bound<'py, PyAny>>, osm_fn: Bound<'py, PyAny>, factor: f64,
                           lane_evidence: Bound<'py, PyAny>, home_copy: Bound<'py, PyAny>)
                           -> PyResult<(Bound<'py, PyAny>, Bound<'py, PyAny>)> {
    let np = py.import("numpy")?;
    // (the input contract: attrs an exact dict, each column read checked where it is read; nw the facade's
    // len(wt.feature), not read again)
    let k = Contract::new(py)?;
    let attrs = attrs.cast_exact::<PyDict>().map_err(|_| pyo3::exceptions::PyTypeError::new_err(
        "width_rejected: the layer's attrs are no exact dict (the road layer contract)"))?.clone();
    let ow = ow.filter(|c| !c.is_none());
    if let Some(c) = &ow {
        k.column(c, "osm_width")?;
    }
    let has_osm = ow.is_some();
    let zeros = |k: usize| vec![false; k].into_pyarray(py);
    let out = || (zeros(nw).into_any(), zeros(n).into_any());
    let mut pos: HashMap<&str, i32> = HashMap::new();
    for (k, c) in names.iter().enumerate() {
        pos.insert(c.as_str(), k as i32);
    }
    // cls_s = [str(v) for v in a["class"].tolist()] if "class" in a else ["unknown"] * n (the class converted before the
    // subtype is looked up, as there: Codex round 3)
    let cls_s: Vec<String> = match attrs.get_item("class")? {
        Some(c) => {
            k.column(&c, "class")?;
            c.call_method0("tolist")?.try_iter()?.map(|v| key_str(&v?)).collect::<PyResult<_>>()?
        }
        None => vec!["unknown".to_owned(); n],
    };
    // np.fromiter((... for c, s in zip(cls_s, sub.tolist() if sub is not None else cls_s)), np.int32, n): sub.tolist()
    // and its iterator made first, then n pairs at most taken one at a time (zip asks the class row first, the
    // subtype's only where there is one), str(s) in the generator (a StopIteration its RuntimeError); fewer: ValueError
    let sub_col = attrs.get_item("subtype")?.filter(|s| !s.is_none());
    if let Some(s) = &sub_col {
        k.column(s, "subtype")?;
    }
    let mut sub_it = match sub_col {
        Some(s) => Some(s.call_method0("tolist")?.try_iter()?),
        None => None,
    };
    let mut cls = Vec::with_capacity(n);
    for k in 0..n {
        let Some(c) = cls_s.get(k) else { return Err(too_short()) };
        let road = match sub_it.as_mut() {
            Some(it) => match it.next() {
                Some(v) => {
                    let v = v?;
                    in_generator(py, key_str(&v))? == "road"
                }
                None => return Err(too_short()),
            },
            None => true,
        };
        cls.push(if road { pos.get(c.as_str()).copied().unwrap_or(-1) } else { -1 });
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
        let t = wt.as_ref().ok_or_else(|| pyo3::exceptions::PyTypeError::new_err("width_rejected: no width table"))?;
        let (tv, tf) = (t.getattr("value")?, t.getattr("feature")?);
        k.column(&tv, "the width rule's values")?;
        k.column(&tf, "the width rule's features")?;
        let wv = np.call_method1("asarray", (tv,))?;
        let kind: String = wv.getattr("dtype")?.getattr("kind")?.extract()?;
        let wv = if "fiub".contains(kind.as_str()) && kind.len() == 1 {
            wv.call_method1("astype", (&float64,))?
        } else {
            let v: Vec<f64> = wv.call_method0("tolist")?.try_iter()?.map(|x| py_float_strict(&x?))
                .collect::<PyResult<_>>()?;
            np.call_method1("array", (v, &float64))?
        };
        srcs.push((np.call_method1("ascontiguousarray", (tf, &int64))?,
                   np.call_method1("ascontiguousarray", (wv,))?));
    }
    if has_osm {
        let f = (0..n as i64).collect::<Vec<i64>>().into_pyarray(py).into_any();
        srcs.push((f, np.call_method1("ascontiguousarray", (osm_fn.call0()?,))?));
    }
    // (a width the fewest lanes allow is kept whatever the lanes: only the rest ask for lane evidence). The masks are
    // numpy's (maybe[f_[hit]] = True, bad & maybe[f_], the &= of the trunk / one-way tests): its indexing,
    // broadcasting and errors for whatever the rule kernel gives (Codex round 2 on ec3397ee)
    let pybool = py.get_type::<pyo3::types::PyBool>();
    let reject = |lanes: &Bound<'py, PyAny>, f: &Bound<'py, PyAny>, v: &Bound<'py, PyAny>| -> PyResult<Bound<'py, PyAny>> {
        let got = rr.call_method1("width_rejects", (&cls, &oneway, &link, lanes, f, v, factor))?;
        np.call_method1("asarray", (got, &pybool))
    };
    let one = np.call_method1("ones", (n, &int64))?;
    let maybe = np.call_method1("zeros", (n, &pybool))?;
    for (f, v) in &srcs {
        let hit = reject(&one, f, v)?;
        maybe.set_item(f.get_item(hit)?, true)?;
    }
    if !maybe.call_method0("any")?.is_truthy()? {
        return Ok(out());
    }
    let lanes = np.call_method1("full", (n, -1i64, &int64))?;
    let own = lane_evidence.call1((&feats,))?;
    let int = py.get_type::<pyo3::types::PyInt>();
    let idx: Vec<i64> = np.call_method1("flatnonzero", (&maybe,))?.call_method0("tolist")?.extract()?;
    for i in idx {
        let h = home_copy.call1((&feats, i))?;
        let v = if h.is_none() {
            int.call1((own.get_item(i)?,))?
        } else {
            int.call1((lane_evidence.call1((h.get_item(0)?,))?.get_item(h.get_item(1)?)?,))?
        };
        lanes.set_item(i, v)?;
    }
    // (no lane data anywhere on the road: only a trunk is tested, as two lanes; a one-way carriageway keeps its width)
    let guess: Vec<bool> = (0..n).map(|k| cls_s[k] == "trunk").collect();
    let guess = guess.into_pyarray(py).into_any().bitand(link.call_method0("__invert__")?)?;
    let lanes_pos = lanes.rich_compare(0i64, pyo3::basic::CompareOp::Gt)?;
    let maybe = maybe.call_method1("__iand__", (lanes_pos.bitor(&guess)?,))?;
    let maybe = maybe.call_method1("__iand__", (oneway.call_method1("astype", (&pybool,))?.call_method0("__invert__")?,))?;
    if !maybe.call_method0("any")?.is_truthy()? {
        return Ok(out());
    }
    let mut res = Vec::new();
    for (f, v) in &srcs {
        let bad = reject(&lanes, f, v)?;
        res.push(bad.bitand(maybe.get_item(f)?)?);
    }
    let mut it = res.into_iter();
    let rule_bad = if nw > 0 { it.next().expect("rule") } else { zeros(nw).into_any() };
    let osm_bad = if has_osm { it.next().expect("osm") } else { zeros(n).into_any() };
    Ok((rule_bad, osm_bad))
}
