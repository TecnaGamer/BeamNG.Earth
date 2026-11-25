//! roadlanes.count and roadlanes.join in Rust (the full Rust port: the OpenStreetMap tag join's last Python, after
//! osmrows / textcol / osmjoin), byte for byte the Python they replaced (tests/test_lanesjoin_native.py keeps it as the
//! reference):
//! - lane_count = roadlanes.count(value, minimum=1);
//! - lanes_need = join's boxes of the motorways / trunks whose own way is not among the tags (asked of the geometry
//!   callback, which stays the facade's: its errors are logged there);
//! - lanes_join = join's lane_count / lane_source / lane_osm_way columns: each road's OSM count, a motorway's or
//!   trunk's from its own way, else from a tagged highway way along the same line (the geometry match: numpy's
//!   arithmetic step by step, unfused; np.linspace as junctions' linspace, the samples through dnp.interp's own
//!   dm::interp, numpy's pairwise mean).
//!
//! Input contract (R2a, roadnet::pyobj::Contract): attrs an exact dict of contract columns; tags osm_tags' exact dict of
//! exact int way ids -> exact dicts of str keys and contract scalars ("_" keys any value, read where used: "_coords" an
//! exact float64 (k, 2) ndarray, "_highway" a str); ids an exact list of n exact ints / None; the layer's offsets int64,
//! lonlat a float64 (k, 2) array; the coordinates the match computes with finite lon / lat (|v| <= 1e6: no overflow,
//! no invalid operation, so numpy's floating-point warnings cannot differ). Anything else TypeError.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyInt, PyList, PySet, PyString};

use crate::dm::Dm;
use crate::roadnet::countries::np_index;
use crate::roadnet::pyobj::{py_float, CArr1, CArr2, Contract};

const MAX_LANES: f64 = 12.0;
const COORD_MAX: f64 = 1e6;

fn refuse(what: &str) -> PyErr {
    pyo3::exceptions::PyTypeError::new_err(format!("roadlanes: {what} (the R2a input contract)"))
}

/// roadlanes.count(v, minimum): float(v) (TypeError / ValueError: None; any other error raised), a finite whole
/// number with minimum <= n <= 12 as int(n), else None.
pub(crate) fn count_of(v: &Bound<'_, PyAny>, minimum: i64) -> PyResult<Option<i64>> {
    let n = if v.is_none() {
        return Ok(None);    // (float(None): TypeError)
    } else if let Ok(f) = v.cast_exact::<PyFloat>() {
        f.value()
    } else if v.is_exact_instance_of::<PyInt>() {
        match v.extract::<i64>() {
            Ok(i) => i as f64,              // (float(int): correctly rounded, as `as` is)
            Err(_) => match py_float(v)? { Some(n) => n, None => return Ok(None) },
        }
    } else {
        match py_float(v)? { Some(n) => n, None => return Ok(None) }
    };
    // minimum <= n <= 12 compared exactly (Python's int / float comparison): n is whole and at most 12 here
    Ok(if n.is_finite() && n.fract() == 0.0 && n <= MAX_LANES && n >= -1.7e38 && (minimum as i128) <= (n as i128) {
        Some(n as i64)
    } else {
        None
    })
}

/// roadlanes.count(value, minimum=1); minimum an exact int (the contract).
#[pyfunction]
#[pyo3(signature = (value, minimum = None))]
pub fn lane_count<'py>(py: Python<'py>, value: Bound<'py, PyAny>, minimum: Option<Bound<'py, PyAny>>)
                       -> PyResult<Bound<'py, PyAny>> {
    let m = match minimum {
        None => 1,
        Some(m) => {
            if !m.is_exact_instance_of::<PyInt>() {
                return Err(refuse("count's minimum is no int"));
            }
            m.extract::<i64>().map_err(|_| refuse("count's minimum is past int64"))?
        }
    };
    Ok(match count_of(&value, m)? {
        Some(c) => c.into_pyobject(py)?.into_any(),
        None => py.None().into_bound(py),
    })
}

/// The layer's offsets and coordinates.
struct Geo<'a> {
    po: &'a [i64],
    ro: &'a [i64],
    co: &'a [i64],
    ll: numpy::ndarray::ArrayView2<'a, f64>,
}

/// Python's slice bounds [a:b] of a sequence of len n.
fn slice_bounds(n: usize, a: i64, b: i64) -> (usize, usize) {
    let n = n as i64;
    let fix = |v: i64| -> i64 { (if v < 0 { v.saturating_add(n) } else { v }).clamp(0, n) };
    let (a, b) = (fix(a), fix(b));
    (a as usize, if b > a { b as usize } else { a as usize })
}

impl Geo<'_> {
    /// feats.parts(i) walked as the Python built it (every index numpy's rules: IndexError where it raised): (the
    /// number of parts, the first part's number of rings, the first ring's rows of lonlat).
    fn parts(&self, i: usize) -> PyResult<(usize, usize, (usize, usize))> {
        let p0 = self.po[np_index(self.po.len(), i as i64)?];
        let p1 = self.po[np_index(self.po.len(), i as i64 + 1)?];
        let (mut nparts, mut rings0, mut ring0) = (0usize, 0usize, (0usize, 0usize));
        let mut p = p0;
        while p < p1 {
            let r0 = self.ro[np_index(self.ro.len(), p)?];
            let r1 = self.ro[np_index(self.ro.len(), p.wrapping_add(1))?];
            let mut r = r0;
            let mut k = 0usize;
            while r < r1 {
                let a = self.co[np_index(self.co.len(), r)?];
                let b = self.co[np_index(self.co.len(), r.wrapping_add(1))?];
                if nparts == 0 && k == 0 {
                    ring0 = slice_bounds(self.ll.nrows(), a, b);
                }
                k += 1;
                r += 1;
            }
            if nparts == 0 {
                rings0 = k;
            }
            nparts += 1;
            p += 1;
        }
        Ok((nparts, rings0, ring0))
    }
}

/// The text of a str / numpy.str_ (a contract column's text item), else None.
fn text_of<'py>(v: &Bound<'py, PyAny>, np_str: &Bound<'py, PyAny>) -> Option<Bound<'py, PyString>> {
    if v.is_exact_instance_of::<PyString>() || v.get_type().is(np_str) {
        v.cast::<PyString>().ok().cloned()
    } else {
        None
    }
}

/// The shared reading of the inputs (the contract checked), for lanes_need and lanes_join.
struct Inputs<'py> {
    attrs: Bound<'py, PyDict>,
    tags: Bound<'py, PyDict>,
    ids: Vec<Bound<'py, PyAny>>,
    np_str: Bound<'py, PyAny>,
    hw_set: Bound<'py, PySet>,
    highways: Vec<String>,
}

impl<'py> Inputs<'py> {
    fn read(py: Python<'py>, attrs: &Bound<'py, PyAny>, tags: &Bound<'py, PyAny>, ids: &Bound<'py, PyAny>, n: usize,
            lonlat: &CArr2<'py, f64>, highways: Vec<String>) -> PyResult<Inputs<'py>> {
        let k = Contract::new(py)?;
        let attrs = attrs.cast_exact::<PyDict>().map_err(|_| refuse("attrs is no exact dict"))?.clone();
        let tags = tags.cast_exact::<PyDict>().map_err(|_| refuse("tags is no exact dict"))?.clone();
        // (the tag values are read through Python's own float() / truthiness / str(), "_coords" checked where read)
        for (w, t) in tags.iter() {
            if !w.is_exact_instance_of::<PyInt>() {
                return Err(refuse("a way id is no int"));
            }
            t.cast_exact::<PyDict>().map_err(|_| refuse("a way's tags are no exact dict"))?;
        }
        let ids = ids.cast_exact::<PyList>().map_err(|_| refuse("ids is no exact list"))?;
        if ids.len() != n {
            return Err(refuse("ids is not one per road"));
        }
        let ids: Vec<Bound<'py, PyAny>> = ids.iter().collect();
        for w in &ids {
            if !(w.is_none() || w.is_exact_instance_of::<PyInt>()) {
                return Err(refuse("a way id is no int or None"));
            }
        }
        for col in ["class", "osm_lanes"] {
            if let Some(c) = attrs.get_item(col)? {
                k.column(&c, col)?;
            }
        }
        if lonlat.as_array().ncols() != 2 {
            return Err(refuse("lonlat is no (k, 2) array"));
        }
        let np = py.import("numpy")?;
        Ok(Inputs { attrs, tags, ids, np_str: np.getattr("str_")?, hw_set: PySet::new(py, &highways)?, highways })
    }

    /// classes[i] in roadlanes.HIGHWAYS (a set of str: str / numpy.str_ compared here, anything else asked of the set).
    fn hw(&self, c: &Bound<'py, PyAny>) -> PyResult<bool> {
        match text_of(c, &self.np_str) {
            Some(s) => Ok(match s.to_str() { Ok(v) => self.highways.iter().any(|h| h == v), Err(_) => false }),
            None => self.hw_set.contains(c),
        }
    }

    /// feats.attrs.get("class", [""] * n): the column (None where present as None: its indexing raises there).
    fn classes(&self) -> PyResult<Option<Bound<'py, PyAny>>> {
        self.attrs.get_item("class")
    }

    /// tags.get(w): the way's tag dict.
    fn tag(&self, w: &Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyDict>>> {
        Ok(match self.tags.get_item(w)? { Some(t) => Some(t.cast_into::<PyDict>()?), None => None })
    }
}

fn class_at<'py>(py: Python<'py>, classes: &Option<Bound<'py, PyAny>>, i: usize) -> PyResult<Bound<'py, PyAny>> {
    match classes {
        Some(c) => c.get_item(i),
        None => Ok(PyString::new(py, "").into_any()),
    }
}

/// numpy's min of finite-or-NaN values (NaN propagated, the first of equal ones kept).
fn np_min(v: impl Iterator<Item = f64>) -> f64 {
    let mut r = f64::NAN;
    let mut first = true;
    for x in v {
        if first {
            r = x;
            first = false;
        } else if r.is_nan() {
            break;
        } else if x.is_nan() || x < r {
            r = x;
        }
    }
    r
}

/// numpy's max (NaN propagated, the first of equal ones kept).
fn np_max(v: impl Iterator<Item = f64>) -> f64 {
    let mut r = f64::NAN;
    let mut first = true;
    for x in v {
        if first {
            r = x;
            first = false;
        } else if r.is_nan() {
            break;
        } else if x.is_nan() || x > r {
            r = x;
        }
    }
    r
}

/// join's need: (lon0, lat0, lon1, lat1) of each motorway / trunk whose own way is not among the tags and which is one
/// line of two points or more (min / max of its lon / lat, float()).
#[pyfunction]
#[pyo3(signature = (attrs, tags, ids, n, part_off, ring_off, coord_off, lonlat, highways))]
#[allow(clippy::too_many_arguments)]
pub fn lanes_need<'py>(py: Python<'py>, attrs: Bound<'py, PyAny>, tags: Bound<'py, PyAny>, ids: Bound<'py, PyAny>,
                       n: usize, part_off: CArr1<'py, i64>, ring_off: CArr1<'py, i64>, coord_off: CArr1<'py, i64>,
                       lonlat: CArr2<'py, f64>, highways: Vec<String>) -> PyResult<Bound<'py, PyList>> {
    let inp = Inputs::read(py, &attrs, &tags, &ids, n, &lonlat, highways)?;
    let g = Geo { po: part_off.as_slice()?, ro: ring_off.as_slice()?, co: coord_off.as_slice()?, ll: lonlat.as_array() };
    let classes = inp.classes()?;
    let need = PyList::empty(py);
    for i in 0..n {
        if !inp.hw(&class_at(py, &classes, i)?)? || inp.tags.contains(&inp.ids[i])? {
            continue;
        }
        let (np_, nr, (a, b)) = g.parts(i)?;
        if np_ == 1 && nr == 1 && b - a >= 2 {
            let col = |d: usize| (a..b).map(move |r| g.ll[[r, d]]);
            need.append((np_min(col(0)), np_min(col(1)), np_max(col(0)), np_max(col(1))))?;
        }
    }
    Ok(need)
}

type P2 = [f64; 2];

/// sqrt(add.reduce(d * d)) of a 2-vector (dnp.norm over its axis of two).
fn norm2(d: P2) -> f64 {
    (d[0] * d[0] + d[1] * d[1]).sqrt()
}

fn sub(a: P2, b: P2) -> P2 {
    [a[0] - b[0], a[1] - b[1]]
}

/// line[np.r_[True, dnp.norm(np.diff(line, axis=0), axis=1) > .01]]
fn thin(line: &[P2]) -> Vec<P2> {
    let mut out = Vec::with_capacity(line.len());
    for (j, &p) in line.iter().enumerate() {
        if j == 0 || norm2(sub(p, line[j - 1])) > 0.01 {
            out.push(p);
        }
    }
    out
}

/// numpy's clip(v, 0, 1): _NPY_MIN(_NPY_MAX(v, 0), 1), NaN kept.
fn clip01(v: f64) -> f64 {
    let v = if v.is_nan() { v } else if v > 0.0 { v } else { 0.0 };
    if v.is_nan() { v } else if v < 1.0 { v } else { 1.0 }
}

/// roadlanes._project(line, points): each point's distance to line and the unit heading of the nearest segment
/// (numpy's steps: maximum(norm, 1e-9), the clipped projection, argmin's first minimum or first NaN).
fn project(line: &[P2], pts: &[P2]) -> (Vec<f64>, Vec<P2>) {
    let m = line.len() - 1;
    let delta: Vec<P2> = (0..m).map(|j| sub(line[j + 1], line[j])).collect();
    let length: Vec<f64> = delta.iter().map(|&d| {
        let l = norm2(d);
        if l.is_nan() || l >= 1e-9 { l } else { 1e-9 }
    }).collect();
    let mut dist = Vec::with_capacity(pts.len());
    let mut head = Vec::with_capacity(pts.len());
    for &p in pts {
        let mut best = 0usize;
        let mut bd = f64::NAN;
        for j in 0..m {
            let (l, d) = (line[j], delta[j]);
            let w = sub(p, l);
            let t = clip01((w[0] * d[0] + w[1] * d[1]) / (length[j] * length[j]));
            let q = [l[0] + t * d[0], l[1] + t * d[1]];
            let dj = norm2(sub(p, q));
            if j == 0 {
                best = 0;
                bd = dj;
            } else if !bd.is_nan() && (dj.is_nan() || dj < bd) {
                best = j;
                bd = dj;
            }
        }
        dist.push(bd);
        head.push([delta[best][0] / length[best], delta[best][1] / length[best]]);
    }
    (dist, head)
}

/// One tagged highway way a road can match (join's candidates, in the tags' order).
struct Cand<'py> {
    wid: Bound<'py, PyAny>,
    lanes: i64,
    cls: String,
    link: bool,
    dir: i64,
    pts: Vec<P2>,
    lo: P2,
    hi: P2,
}

/// The rows of lonlat (or of a way's "_coords") moved to metres about origin: (p - origin) * scale, each coordinate
/// the contract's (finite, |v| <= 1e6).
fn to_metres(rows: impl Iterator<Item = P2>, origin: P2, scale: P2) -> PyResult<Vec<P2>> {
    let ok = |v: f64| v.is_finite() && v.abs() <= COORD_MAX;
    if !(ok(origin[0]) && ok(origin[1])) {
        return Err(refuse("the layer's first coordinate is no finite lon / lat"));
    }
    rows.map(|p| {
        if ok(p[0]) && ok(p[1]) {
            Ok([(p[0] - origin[0]) * scale[0], (p[1] - origin[1]) * scale[1]])
        } else {
            Err(refuse("a coordinate is no finite lon / lat"))
        }
    }).collect()
}

/// join's columns: lane_count (int64 where every road has an OSM count, else float64 with NaN), lane_source ("<U16":
/// "osm-way", "osm-geometry" or ""), lane_osm_way (int64), set on attrs in that order. direction_of(i): the road's
/// one-way sign (0 none); is_link_of(i): roadlanes.is_link (both the facade's, over feats' rule tables).
#[pyfunction]
#[pyo3(signature = (attrs, tags, ids, n, part_off, ring_off, coord_off, lonlat, highways, direction_of, is_link_of))]
#[allow(clippy::too_many_arguments)]
pub fn lanes_join<'py>(py: Python<'py>, attrs: Bound<'py, PyAny>, tags: Bound<'py, PyAny>, ids: Bound<'py, PyAny>,
                       n: usize, part_off: CArr1<'py, i64>, ring_off: CArr1<'py, i64>, coord_off: CArr1<'py, i64>,
                       lonlat: CArr2<'py, f64>, highways: Vec<String>, direction_of: Bound<'py, PyAny>,
                       is_link_of: Bound<'py, PyAny>) -> PyResult<()> {
    let inp = Inputs::read(py, &attrs, &tags, &ids, n, &lonlat, highways)?;
    // Rust-owned copies of the geometry: the direction / link callbacks run Python, which could resize or free a
    // caller-owned array under a borrowed slice (Codex on d4bd7eb4). A copy per layer is a memcpy.
    let (po_v, ro_v, co_v) = (part_off.as_slice()?.to_vec(), ring_off.as_slice()?.to_vec(), coord_off.as_slice()?.to_vec());
    let ll_v = lonlat.as_array().to_owned();
    let g = Geo { po: &po_v, ro: &ro_v, co: &co_v, ll: ll_v.view() };
    let np = py.import("numpy")?;

    // counts = np.array([count(v) or np.nan for v in attrs.get("osm_lanes", [None] * n)])
    let mut got: Vec<Option<i64>> = Vec::new();
    match inp.attrs.get_item("osm_lanes")? {
        None => got.resize(n, None),
        Some(col) => {
            let mut fast = false;
            if let Ok(a) = col.extract::<CArr1<'py, f64>>() {
                if col.get_type().is(&np.getattr("ndarray")?) {
                    for &v in a.as_slice()? {
                        got.push(if v.is_finite() && v.fract() == 0.0 && (1.0..=MAX_LANES).contains(&v) { Some(v as i64) } else { None });
                    }
                    fast = true;
                }
            } else if let Ok(a) = col.extract::<CArr1<'py, i64>>() {
                if col.get_type().is(&np.getattr("ndarray")?) {
                    for &v in a.as_slice()? {
                        got.push(if (1..=12).contains(&v) { Some(v) } else { None });
                    }
                    fast = true;
                }
            }
            if !fast {
                for v in col.try_iter()? {
                    got.push(count_of(&v?, 1)?);
                }
            }
        }
    }
    let ints = !got.is_empty() && got.iter().all(|c| c.is_some());
    let mut cf: Vec<f64> = got.iter().map(|c| c.map(|v| v as f64).unwrap_or(f64::NAN)).collect();
    let mut ci: Vec<i64> = if ints { got.iter().map(|c| c.unwrap_or(0)).collect() } else { Vec::new() };
    // 0 none, 1 osm-way, 2 osm-geometry
    let mut sources: Vec<u8> = cf.iter().map(|v| if v.is_finite() { 1 } else { 0 }).collect();
    let mut matched: Vec<i64> = Vec::with_capacity(n);
    for w in &inp.ids {
        matched.push(if w.is_none() { 0 } else {
            w.extract::<i64>().map_err(|_| pyo3::exceptions::PyOverflowError::new_err("Python int too large to convert to C long"))?
        });
    }
    let classes = inp.classes()?;
    let origin: P2 = if g.ll.nrows() > 0 { [g.ll[[0, 0]], g.ll[[0, 1]]] } else { [0.0, 0.0] };
    let scale: P2 = [111320.0 * origin[1].to_radians().dcos(), 110574.0];

    let mut cands: Vec<Cand<'py>> = Vec::new();
    let ndarray = np.getattr("ndarray")?;
    let f64_dt = np.getattr("dtype")?.call1(("float64",))?;
    for (w, t) in inp.tags.iter() {
        let t = t.cast_into::<PyDict>()?;
        let p = t.get_item("_coords")?;
        let lanes = match t.get_item("lanes")? { Some(v) => count_of(&v, 1)?, None => None };
        // cls = tag.get("lane_hw") or tag.get("_highway") or ""
        let mut cls: Option<Bound<'py, PyAny>> = None;
        for key in ["lane_hw", "_highway"] {
            if let Some(v) = t.get_item(key)? {
                if v.is_truthy()? {
                    cls = Some(v);
                    break;
                }
            }
        }
        let (Some(lanes), Some(p)) = (lanes, p) else { continue };
        if p.is_none() {
            continue;
        }
        if !p.get_type().is(&ndarray) {
            return Err(refuse("a way's _coords is no ndarray"));
        }
        let pa = p.extract::<CArr2<'py, f64>>().map_err(|_| refuse("a way's _coords is no float64 (k, 2) array"))?;
        if !p.getattr("dtype")?.eq(&f64_dt)? || pa.as_array().ncols() != 2 {
            return Err(refuse("a way's _coords is no float64 (k, 2) array"));
        }
        let pv = pa.as_array();
        if pv.nrows() < 2 {
            continue;
        }
        let cls = match &cls {
            None => String::new(),
            Some(c) => match text_of(c, &inp.np_str) {
                Some(s) => match s.to_str() {
                    Ok(v) => v.to_owned(),
                    Err(_) => continue,         // (lone surrogates: no highway class)
                },
                None => return Err(refuse("a way's highway class is no str")),
            },
        };
        let base = cls.strip_suffix("_link").unwrap_or(&cls).to_owned();
        if !inp.highways.iter().any(|h| *h == base) {
            continue;
        }
        let pts = thin(&to_metres((0..pv.nrows()).map(|r| [pv[[r, 0]], pv[[r, 1]]]), origin, scale)?);
        if pts.len() < 2 {
            continue;
        }
        let ow = match t.get_item("osm_oneway")? {
            Some(v) => {
                let s = if v.is_exact_instance_of::<PyString>() { v.cast_into::<PyString>()? } else { v.str()? };
                s.to_str().map(|x| x.to_owned()).unwrap_or_default()
            }
            None => String::new(),
        };
        let dir = if ow == "-1" { -1 } else if ow == "yes" || ow == "1" || ow == "true" { 1 } else { 0 };
        let lo = [np_min(pts.iter().map(|q| q[0])) - 6.0, np_min(pts.iter().map(|q| q[1])) - 6.0];
        let hi = [np_max(pts.iter().map(|q| q[0])) + 6.0, np_max(pts.iter().map(|q| q[1])) + 6.0];
        cands.push(Cand { wid: w.clone(), lanes, cls: base, link: cls.ends_with("_link"), dir, pts, lo, hi });
    }

    for i in 0..n {
        let c = class_at(py, &classes, i)?;
        if !inp.hw(&c)? {
            continue;
        }
        // counts[i] = count(tags.get(ids[i], {}).get("lanes")) or np.nan
        if i >= cf.len() {
            return Err(pyo3::exceptions::PyIndexError::new_err(format!(
                "index {i} is out of bounds for axis 0 with size {}", cf.len())));
        }
        let own = match inp.tag(&inp.ids[i])? {
            Some(t) => match t.get_item("lanes")? { Some(v) => count_of(&v, 1)?, None => None },
            None => None,
        };
        match own {
            Some(v) => {
                cf[i] = v as f64;
                if ints {
                    ci[i] = v;
                }
            }
            None => {
                if ints {
                    return Err(pyo3::exceptions::PyValueError::new_err("cannot convert float NaN to integer"));
                }
                cf[i] = f64::NAN;
            }
        }
        sources[i] = if cf[i].is_finite() { 1 } else { 0 };
        if sources[i] != 0 || cands.is_empty() {
            continue;
        }
        let (nparts, nrings, (a, b)) = g.parts(i)?;
        if nparts != 1 || nrings != 1 {
            continue;
        }
        let line = to_metres((a..b).map(|r| [g.ll[[r, 0]], g.ll[[r, 1]]]), origin, scale)?;
        if line.len() < 2 {
            continue;
        }
        let line = thin(&line);
        if line.len() < 2 {
            continue;
        }
        // s = np.r_[0., np.cumsum(norms)]; samples at np.linspace(0., s[-1], 9) through dnp.interp (dm::interp)
        let mut s = Vec::with_capacity(line.len());
        s.push(0.0);
        let mut acc = 0.0;
        for j in 1..line.len() {
            let d = norm2(sub(line[j], line[j - 1]));
            acc = if j == 1 { d } else { acc + d };
            s.push(acc);
        }
        let xs = crate::junctions::linspace(0.0, s[s.len() - 1], 9);
        let lx: Vec<f64> = line.iter().map(|q| q[0]).collect();
        let ly: Vec<f64> = line.iter().map(|q| q[1]).collect();
        let sx = crate::dm::interp(&xs, &s, &lx, None, None);
        let sy = crate::dm::interp(&xs, &s, &ly, None, None);
        let samples: Vec<P2> = (0..9).map(|k| [sx[k], sy[k]]).collect();
        let (_, tangent) = project(&line, &samples);
        let direction: i64 = direction_of.call1((i,))?.extract()?;
        let ctext = text_of(&c, &inp.np_str);
        let mut matches: Vec<(f64, Bound<'py, PyAny>, i64)> = Vec::new();
        for cd in &cands {
            let same_class = match &ctext {
                Some(s) => s.to_str().map(|v| v == cd.cls).unwrap_or(false),
                None => !PyString::new(py, &cd.cls).as_any().ne(&c)?,
            };
            if !same_class || cd.link != is_link_of.call1((i,))?.is_truthy()? || (cd.dir != 0) != (direction != 0) {
                continue;
            }
            if samples.iter().any(|q| q[0] < cd.lo[0] || q[1] < cd.lo[1]) || samples.iter().any(|q| q[0] > cd.hi[0] || q[1] > cd.hi[1]) {
                continue;
            }
            let (distance, heading) = project(&cd.pts, &samples);
            let align: Vec<f64> = (0..9).map(|k| {
                let a = tangent[k][0] * heading[k][0] + tangent[k][1] * heading[k][1];
                if direction != 0 { (a * direction as f64) * cd.dir as f64 } else { a.abs() }
            }).collect();
            let mean = crate::levels::np_pairwise(&distance) / 9.0;
            if np_max(distance.iter().copied()) <= 6.0 && mean <= 3.0 && np_min(align.iter().copied()) >= 0.9 {
                matches.push((mean, cd.wid.clone(), cd.lanes));
            }
        }
        // matches.sort(): (mean, way id, lanes) tuples (the means never NaN here, the way ids distinct)
        let mut err: Option<PyErr> = None;
        matches.sort_by(|x, y| {
            x.0.partial_cmp(&y.0).unwrap_or(std::cmp::Ordering::Equal).then_with(|| {
                match x.1.lt(&y.1) {
                    Ok(true) => std::cmp::Ordering::Less,
                    Ok(false) => match x.1.gt(&y.1) {
                        Ok(true) => std::cmp::Ordering::Greater,
                        Ok(false) => x.2.cmp(&y.2),
                        Err(e) => { err.get_or_insert(e); std::cmp::Ordering::Equal }
                    },
                    Err(e) => { err.get_or_insert(e); std::cmp::Ordering::Equal }
                }
            })
        });
        if let Some(e) = err {
            return Err(e);
        }
        if let Some(best) = matches.first() {
            if !matches[1..].iter().any(|m| m.2 != best.2 && m.0 < best.0 + 1.0) {
                matched[i] = best.1.extract::<i64>().map_err(|_| pyo3::exceptions::PyOverflowError::new_err(
                    "Python int too large to convert to C long"))?;
                cf[i] = best.2 as f64;
                if ints {
                    ci[i] = best.2;
                }
                sources[i] = 2;
            }
        }
    }
    let counts: Bound<'py, PyAny> = if ints {
        numpy::PyArray1::from_vec(py, ci).into_any()
    } else {
        numpy::PyArray1::from_vec(py, cf).into_any()
    };
    inp.attrs.set_item("lane_count", counts)?;
    let names = [PyString::new(py, ""), PyString::new(py, "osm-way"), PyString::new(py, "osm-geometry")];
    let src = PyList::empty(py);
    for &s in &sources {
        src.append(&names[s as usize])?;
    }
    let kw = PyDict::new(py);
    kw.set_item("dtype", "<U16")?;
    inp.attrs.set_item("lane_source", np.call_method("array", (src,), Some(&kw))?)?;
    inp.attrs.set_item("lane_osm_way", numpy::PyArray1::from_vec(py, matched))?;
    Ok(())
}
