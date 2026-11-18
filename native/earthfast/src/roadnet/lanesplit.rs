//! Lane sections of merged highways (lanesections.split's plan: _sections, _joint_specs, _ramp_mouths, _sample, _ease,
//! _offset, _placement, _outer_lanes, _grid and the job loop; from PR #880 by DerEifrige-f), byte for byte the
//! numpy / Python they replaced. The new layer's arrays stay lanesections.split's (numpy gathers, earthfast's
//! lane_split_coords / lane_split_rules).

use std::collections::BTreeMap;

use numpy::IntoPyArray;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};

use super::pyfloat::np_round;
use super::pyobj::{py_float, py_str};
use crate::dm::Dm;
use super::pyobj::{CArr1, CArr2};

const LINE: u8 = 2;
const STEP_M: f64 = 4.0;
const MAX_LANES: f64 = 12.0;

/// roadlanes.count(value): a whole lane count 1..12 (float() of it), None for anything else.
fn count(v: Option<&Bound<'_, PyAny>>) -> PyResult<Option<i64>> {
    let Some(v) = v else { return Ok(None) };
    match py_float(v)? {
        Some(n) if n.is_finite() && n.fract() == 0.0 && (1.0..=MAX_LANES).contains(&n) => Ok(Some(n as i64)),
        _ => Ok(None),
    }
}

/// A road layer's columns as the lane sections read them.
pub struct Cols<'py> {
    py: Python<'py>,
    ids: Vec<String>,
    kind: Vec<u8>,
    po: Vec<i64>,
    ro: Vec<i64>,
    co: Vec<i64>,
    ll: Vec<[f64; 2]>,
    cls: Option<Bound<'py, PyAny>>,
    oneway: Option<(Vec<i64>, Bound<'py, PyAny>)>,
    subclass: Option<(Vec<i64>, Bound<'py, PyAny>)>,
    subclass_attr: Option<Bound<'py, PyAny>>,
    osm_lanes: Option<Bound<'py, PyAny>>,
    lane_total: Option<Bound<'py, PyAny>>,
    parts: Option<Bound<'py, PyAny>>,
    lane_w: BTreeMap<String, f64>,
    lane_w_default: f64,
    shoulder: (f64, f64, i64),
}

/// The layer's offsets fit together (n features, parts, rings, points): IndexError where one points past its array,
/// is negative or runs backwards (numpy's index error; never a panic on a malformed layer). Every value read is
/// checked on its own (a lone offset has no neighbour to compare with: Codex round 2 on fde0e52d, ring offsets [-1]):
/// part_off[0..=n] index ring_off, ring_off[0..=part_off[n]] index coord_off, coord_off[0..=ring_off[part_off[n]]]
/// are at most the point count; each run non-decreasing.
pub(crate) fn check_offsets(n: usize, po: &[i64], ro: &[i64], co: &[i64], points: usize) -> PyResult<()> {
    fn run(what: &str, v: &[i64], bound: usize, inclusive: bool) -> PyResult<usize> {
        let mut prev = 0usize;
        for (k, &x) in v.iter().enumerate() {
            let ok = usize::try_from(x).ok().filter(|&u| if inclusive { u <= bound } else { u < bound });
            match ok {
                Some(u) if k == 0 || u >= prev => prev = u,
                _ => return Err(pyo3::exceptions::PyIndexError::new_err(format!("a road layer's {what} out of range"))),
            }
        }
        Ok(prev)
    }
    if po.len() < n + 1 {
        return Err(pyo3::exceptions::PyIndexError::new_err("a road layer's part offsets out of range"));
    }
    let parts = run("part offsets", &po[..n + 1], ro.len(), false)?;
    let rings = run("ring offsets", &ro[..parts + 1], co.len(), false)?;
    run("coordinate offsets", &co[..rings + 1], points, true)?;
    Ok(())
}

/// A layer's lon / lat points are (k, 2): IndexError otherwise (numpy's error reading column 1 of a one-column array;
/// never a panic on a malformed array handed to a kernel directly).
pub(crate) fn check_lonlat(cols: usize) -> PyResult<()> {
    if cols != 2 {
        return Err(pyo3::exceptions::PyIndexError::new_err(format!(
            "a road layer's lon / lat points have {cols} columns, not 2")));
    }
    Ok(())
}

fn table<'py>(t: Option<Bound<'py, PyAny>>, n: usize) -> PyResult<Option<(Vec<i64>, Bound<'py, PyAny>)>> {
    let Some(t) = t.filter(|t| !t.is_none()) else { return Ok(None) };
    let maj: CArr1<i64> = t.call_method1("majors", (n,))?.extract()?;
    Ok(Some((maj.as_slice()?.to_vec(), t.getattr("value")?)))
}

fn opt(v: Option<Bound<'_, PyAny>>) -> Option<Bound<'_, PyAny>> {
    v.filter(|c| !c.is_none())
}

impl<'py> Cols<'py> {
    #[allow(clippy::too_many_arguments)]
    fn new(py: Python<'py>, ids: Vec<String>, kind: CArr1<'py, u8>, po: CArr1<'py, i64>,
           ro: CArr1<'py, i64>, co: CArr1<'py, i64>, lonlat: CArr2<'py, f64>,
           cls: Option<Bound<'py, PyAny>>, oneway: Option<Bound<'py, PyAny>>, subclass: Option<Bound<'py, PyAny>>,
           subclass_attr: Option<Bound<'py, PyAny>>, osm_lanes: Option<Bound<'py, PyAny>>,
           lane_total: Option<Bound<'py, PyAny>>, parts: Option<Bound<'py, PyAny>>, lane_w: BTreeMap<String, f64>,
           lane_w_default: f64, shoulder: (f64, f64, i64)) -> PyResult<Self> {
        let n = ids.len();
        let a = lonlat.as_array();
        check_lonlat(a.ncols())?;
        check_offsets(n, po.as_slice()?, ro.as_slice()?, co.as_slice()?, a.nrows())?;
        if kind.len()? < n {
            return Err(pyo3::exceptions::PyIndexError::new_err("lane sections: fewer kinds than features"));
        }
        Ok(Cols {
            py,
            ids,
            kind: kind.as_slice()?.to_vec(),
            po: po.as_slice()?.to_vec(),
            ro: ro.as_slice()?.to_vec(),
            co: co.as_slice()?.to_vec(),
            ll: (0..a.nrows()).map(|k| [a[[k, 0]], a[[k, 1]]]).collect(),
            cls: opt(cls),
            oneway: table(oneway, n)?,
            subclass: table(subclass, n)?,
            subclass_attr: opt(subclass_attr),
            osm_lanes: opt(osm_lanes),
            lane_total: opt(lane_total),
            parts: opt(parts),
            lane_w,
            lane_w_default,
            shoulder,
        })
    }

    fn n(&self) -> usize {
        self.ids.len()
    }

    /// _col(f, name, i): the column's element i.
    fn col(c: &Option<Bound<'py, PyAny>>, i: usize) -> PyResult<Option<Bound<'py, PyAny>>> {
        match c {
            Some(c) => Ok(Some(c.get_item(i)?)),
            None => Ok(None),
        }
    }

    /// _col(f, "class", i) in roadlanes.HIGHWAYS.
    fn highway(&self, i: usize) -> PyResult<bool> {
        let Some(v) = Self::col(&self.cls, i)? else { return Ok(false) };
        if let Ok(s) = v.cast::<PyString>() {
            let s = s.to_str()?;
            return Ok(s == "motorway" || s == "trunk");
        }
        Ok(false)
    }

    /// str(f.attrs["class"][i]).
    fn cls_str(&self, i: usize) -> PyResult<String> {
        match &self.cls {
            Some(c) => py_str(&c.get_item(i)?),
            None => Err(pyo3::exceptions::PyKeyError::new_err("class")),
        }
    }

    /// roadlanes.rule(f, "oneway", i): the majority row's value, None without.
    fn direction(&self, i: usize) -> PyResult<Option<Bound<'py, PyAny>>> {
        match &self.oneway {
            Some((maj, v)) if i < maj.len() && maj[i] >= 0 => Ok(Some(v.get_item(maj[i])?)),
            _ => Ok(None),
        }
    }

    /// roadlanes.is_link(f, i).
    fn is_link(&self, i: usize) -> PyResult<bool> {
        if let Some((maj, v)) = &self.subclass {
            if i < maj.len() && maj[i] >= 0 && v.get_item(maj[i])?.eq("link")? {
                return Ok(true);
            }
        }
        if let Some(s) = &self.subclass_attr {
            return Ok(py_str(&s.get_item(i)?)? == "link");
        }
        Ok(false)
    }

    /// f.parts(i): [part [ring (start, end) into ll]].
    fn rings(&self, i: usize) -> Vec<Vec<(usize, usize)>> {
        let mut out = Vec::new();
        for p in self.po[i]..self.po[i + 1] {
            let mut part = Vec::new();
            for r in self.ro[p as usize]..self.ro[p as usize + 1] {
                part.push((self.co[r as usize] as usize, self.co[r as usize + 1] as usize));
            }
            out.push(part);
        }
        out
    }

    fn lane_m(&self, cls: &str) -> f64 {
        *self.lane_w.get(cls).unwrap_or(&self.lane_w_default)
    }

    /// _grid(cls, n, lane_m): a motorway's travel lanes stand left of its pavement's centre by its shoulders' half
    /// difference.
    fn grid(&self, cls: &str, n: i64) -> f64 {
        if cls != "motorway" {
            return 0.0;
        }
        let cap = if n >= self.shoulder.2 { self.shoulder.1 } else { self.shoulder.0 };
        cap / 2.0
    }
}

/// _arc(ll): the stations along a lon/lat line (0, then the geodesic lengths summed in order).
fn arc(ll: &[[f64; 2]]) -> Vec<f64> {
    let mut s = Vec::with_capacity(ll.len());
    s.push(0.0);
    let mut acc = 0.0;
    for l in crate::lanesections::lengths(ll) {
        acc += l;
        s.push(acc);
    }
    s
}

/// np.arange(start, stop, step) of floats: ceil((stop - start) / step) values, start + i * ((start + step) - start);
/// numpy's errors: ValueError for a length that is NaN, OverflowError past the index range, MemoryError past what
/// could be allocated.
fn arange(start: f64, stop: f64, step: f64) -> PyResult<Vec<f64>> {
    let len = ((stop - start) / step).ceil();
    if len.is_nan() {
        return Err(pyo3::exceptions::PyValueError::new_err("arange: cannot compute length"));
    }
    if !(len >= i64::MIN as f64 && len <= i64::MAX as f64) {
        return Err(pyo3::exceptions::PyOverflowError::new_err("arange: overflow while computing length"));
    }
    if !(len > 0.0) {
        return Ok(Vec::new());
    }
    if len > (1u64 << 36) as f64 {
        return Err(pyo3::exceptions::PyMemoryError::new_err("arange: unable to allocate"));
    }
    let len = len as usize;
    let mut out = Vec::with_capacity(len);
    out.push(start);
    if len > 1 {
        let second = start + step;
        out.push(second);
        let delta = second - start;
        for i in 2..len {
            out.push(start + (i as f64) * delta);
        }
    }
    Ok(out)
}

/// numpy's binary_search_with_guess (compiled_base.c): the index of the last xp <= key from a guess, -1 / len outside;
/// with numpy's comparisons in its order, so a NaN in xp lands where numpy's does.
fn search_with_guess(key: f64, arr: &[f64], guess: i64) -> i64 {
    const CACHE: i64 = 8;
    let len = arr.len() as i64;
    let at = |k: i64| arr[k as usize];
    let (mut imin, mut imax) = (0i64, len);
    if key > at(len - 1) {
        return len;
    } else if key < at(0) {
        return -1;
    }
    if len <= 4 {
        let mut i = 1;
        while i < len && key >= at(i) {
            i += 1;
        }
        return i - 1;
    }
    let mut guess = guess;
    if guess > len - 3 {
        guess = len - 3;
    }
    if guess < 1 {
        guess = 1;
    }
    if key < at(guess) {
        if key < at(guess - 1) {
            imax = guess - 1;
            if guess > CACHE && key >= at(guess - CACHE) {
                imin = guess - CACHE;
            }
        } else {
            return guess - 1;
        }
    } else if key < at(guess + 1) {
        return guess;
    } else if key < at(guess + 2) {
        return guess + 1;
    } else {
        imin = guess + 2;
        if guess < len - CACHE - 1 && key < at(guess + CACHE) {
            imax = guess + CACHE;
        }
    }
    while imin < imax {
        let imid = imin + ((imax - imin) >> 1);
        if key >= at(imid) {
            imin = imid + 1;
        } else {
            imax = imid;
        }
    }
    imin - 1
}

/// np.interp(x, xp, fp) (numpy's arr_interp, its search carrying its guess from one x to the next): NaN for a NaN x,
/// fp[0] / fp[-1] outside, the sample's own value on it, the slope's other side where one side gives NaN. xp, fp
/// non-empty and of one length.
fn np_interp_all(x: &[f64], xp: &[f64], fp: &[f64]) -> Vec<f64> {
    let n = xp.len();
    let (lval, rval) = (fp[0], fp[n - 1]);
    if n == 1 {
        return x.iter().map(|&v| if v < xp[0] { lval } else if v > xp[0] { rval } else { fp[0] }).collect();
    }
    let mut j: i64 = 0;
    x.iter().map(|&v| {
        if v.is_nan() {
            return v;
        }
        j = search_with_guess(v, xp, j);
        if j == -1 {
            lval
        } else if j == n as i64 {
            rval
        } else if j == n as i64 - 1 {
            fp[j as usize]
        } else {
            let k = j as usize;
            if xp[k] == v {
                fp[k]
            } else {
                let slope = (fp[k + 1] - fp[k]) / (xp[k + 1] - xp[k]);
                let mut r = slope * (v - xp[k]) + fp[k];
                if r.is_nan() {
                    r = slope * (v - xp[k + 1]) + fp[k + 1];
                    if r.is_nan() && fp[k] == fp[k + 1] {
                        r = fp[k];
                    }
                }
                r
            }
        }
    }).collect()
}

/// _sample(ll, a, b, dense): the line between fractions a and b of its length (its own vertices more than 1 cm in,
/// and dense: every STEP_M not within 1 cm of one), np.interp'd; and the points' stations from its start.
fn sample(ll: &[[f64; 2]], a: f64, b: f64, dense: bool) -> PyResult<(Vec<[f64; 2]>, Vec<f64>)> {
    let s = arc(ll);
    let total = s[s.len() - 1];
    let (u, v) = (a * total, b * total);
    let inner0: Vec<f64> = s.iter().copied().filter(|&x| x > u + 0.01 && x < v - 0.01).collect();
    let mut inner = inner0.clone();
    if dense {
        let mut stations = arange(u + STEP_M, v - 0.01, STEP_M)?;
        if !inner0.is_empty() {
            let m = inner0.len();
            stations.retain(|&st| {
                // searchsorted (left): the first k with inner[k] >= st
                let k = inner0.partition_point(|&x| x < st);
                let hi = inner0[k.min(m - 1)];
                let lo = inner0[if k == 0 { 0 } else { k - 1 }];
                let d1 = (st - hi).abs();
                let d2 = (st - lo).abs();
                // np.minimum (NaN propagates) > .01
                let mn = if d1.is_nan() || d2.is_nan() { f64::NAN } else { d1.min(d2) };
                mn > 0.01
            });
        }
        inner.extend(stations);
        // np.sort: NaN last
        inner.sort_by(|x, y| x.partial_cmp(y).unwrap_or_else(|| x.is_nan().cmp(&y.is_nan())));
    }
    if !inner.is_empty() {
        let mut keep = Vec::with_capacity(inner.len());
        keep.push(inner[0]);
        for k in 1..inner.len() {
            if inner[k] - inner[k - 1] > 0.01 {
                keep.push(inner[k]);
            }
        }
        inner = keep;
    }
    let mut at = Vec::with_capacity(inner.len() + 2);
    at.push(u);
    at.extend(inner);
    at.push(v);
    let lon: Vec<f64> = ll.iter().map(|p| p[0]).collect();
    let lat: Vec<f64> = ll.iter().map(|p| p[1]).collect();
    let (plon, plat) = (np_interp_all(&at, &s, &lon), np_interp_all(&at, &s, &lat));
    let pts = plon.iter().zip(&plat).map(|(&x, &y)| [x, y]).collect();
    let st = at.iter().map(|&x| x - u).collect();
    Ok((pts, st))
}

/// _ease(s, length, reach, ends): the eased share of the change at each station.
fn ease(s: &[f64], length: f64, reach: [f64; 2], ends: [f64; 2]) -> Vec<f64> {
    let mut g = vec![1.0f64; s.len()];
    for end in 0..2 {
        let span = reach[end];
        if span > 0.0 {
            for (k, &x) in s.iter().enumerate() {
                let t = clip((if end == 0 { x } else { length - x }) / span, 0.0, 1.0);
                // ((1 - e) * t) * t * (3 - 2t): numpy's left-to-right products
                let v = ends[end] + (1.0 - ends[end]) * t * t * (3.0 - 2.0 * t);
                g[k] = npmin(g[k], v);
            }
        }
    }
    g
}

/// numpy's clip: NaN stays NaN.
fn clip(v: f64, lo: f64, hi: f64) -> f64 {
    if v.is_nan() { v } else { v.max(lo).min(hi) }
}

/// np.minimum / np.maximum: NaN propagates.
fn npmin(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else { a.min(b) }
}

fn npmax(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else { a.max(b) }
}

/// np.sign.
fn npsign(v: f64) -> f64 {
    if v.is_nan() { v } else if v > 0.0 { 1.0 } else if v < 0.0 { -1.0 } else { 0.0 }
}

/// _offset(ll, offsets): the line moved offsets m to its left in the ENU frame at its first point (geodesy.EnuFrame,
/// roads.offset_polyline), back to lon / lat.
fn offset(ll: &[[f64; 2]], d: &[f64]) -> Vec<[f64; 2]> {
    let (lat0, lon0) = (ll[0][1], ll[0][0]);
    let zero = [0.0f64; 3];
    let eye = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];
    let (ox, oy, oz) = crate::g2e_one(lat0, lon0, 0.0, &zero, &eye);
    let (la, lo) = (lat0.to_radians(), lon0.to_radians());
    let (sla, cla, slo, clo) = (la.dsin(), la.dcos(), lo.dsin(), lo.dcos());
    let rot = [[-slo, clo, 0.0], [-sla * clo, -sla * slo, cla], [cla * clo, cla * slo, sla]];
    let origin = [ox, oy, oz];
    let mut xy: Vec<[f64; 2]> = ll.iter().map(|p| {
        let (e, n, _) = crate::g2e_one(p[1], p[0], 0.0, &origin, &rot);
        [e, n]
    }).collect();
    if xy.len() >= 2 {
        crate::tilecuts::offset_xy(&mut xy, d);
    }
    xy.iter().map(|p| {
        let (la, lo, _) = crate::e2g_one(p[0], p[1], 0.0, &origin, &rot);
        [lo, la]
    }).collect()
}

/// _placement(tags, n, lane_m, direction): m its lanes stand left of the line by placement=*, None without.
fn placement(py: Python<'_>, tags: &Bound<'_, PyDict>, n: i64, lane_m: f64, direction: i64) -> PyResult<Option<f64>> {
    let v = match tags.get_item("placement")? {
        Some(v) => py_str(&v)?,
        None => String::new(),
    };
    let (kind, digits) = match v.split_once(':') {
        Some(x) => x,
        None => return Ok(None),
    };
    let sub = match kind {
        "left_of" => 1.0,
        "middle_of" => 0.5,
        "right_of" => 0.0,
        _ => return Ok(None),
    };
    // re.fullmatch's (\d+) and int(): ASCII digits here, any other text Python's own (\d is every Unicode digit)
    let k: i64 = if !digits.is_empty() && digits.bytes().all(|c| c.is_ascii_digit()) {
        match digits.parse::<i64>() {
            Ok(k) => k,
            Err(_) => return Ok(None),
        }
    } else {
        let re = py.import("re")?;
        let m = re.call_method1("fullmatch", (r"(\d+)", digits))?;
        if m.is_none() {
            return Ok(None);
        }
        match py.get_type::<pyo3::types::PyInt>().call1((m.call_method1("group", (1,))?,))?.extract::<i64>() {
            Ok(k) => k,
            Err(_) => return Ok(None),
        }
    };
    if !(1 <= k && k <= n) {
        return Ok(None);
    }
    let kf = k as f64 - sub;
    Ok(Some((kf - n as f64 / 2.0) * lane_m * direction as f64))
}

/// _outer_lanes(tags, n): (travel side, count) of a contiguous group of exit or merge lanes, (0, 0) none.
fn outer_lanes(tags: &Bound<'_, PyDict>, n: i64) -> PyResult<(i64, i64)> {
    let v = match tags.get_item("turn:lanes")? {
        Some(v) => py_str(&v)?,
        None => String::new(),
    };
    let vals: Vec<Vec<&str>> = v.split('|').map(|x| x.split(';').collect()).collect();
    if vals.len() as i64 != n {
        return Ok((0, 0));
    }
    let has = |set: &Vec<&str>, w: &str| set.contains(&w);
    for (side, choices) in [(-1i64, ["merge_to_left", "slight_right", "right"]), (1, ["merge_to_right", "slight_left", "left"])] {
        let ordered: Vec<&Vec<&str>> = if side < 0 { vals.iter().rev().collect() } else { vals.iter().collect() };
        let mut cnt = 0i64;
        for value in &ordered {
            if !choices.iter().any(|c| has(value, c)) || has(value, "through") {
                break;
            }
            cnt += 1;
        }
        if 0 < cnt && cnt < n && ordered[cnt as usize..].iter().all(|v| v.iter().all(|x| *x == "" || *x == "none" || *x == "through")) {
            return Ok((side, cnt));
        }
    }
    Ok((0, 0))
}

/// One merged section row (a, b, tags, n): the stretch, its tags (a dict of its own), its count (its source way unread).
pub struct Row<'py> {
    a: f64,
    b: f64,
    tags: Bound<'py, PyDict>,
    n: i64,
}

/// _sections(f, i): (rows, base count, direction +1 / -1), None where the feature is no merged highway of other
/// counts.
fn sections<'py>(c: &Cols<'py>, i: usize) -> PyResult<Option<(Vec<Row<'py>>, i64, i64)>> {
    let py = c.py;
    let Some(parts) = &c.parts else { return Ok(None) };
    let pi = parts.get_item(i)?;
    if !(pi.is_instance_of::<PyList>() || pi.is_instance_of::<PyTuple>()) || pi.len()? < 2 || c.kind[i] != LINE {
        return Ok(None);
    }
    if !c.highway(i)? {
        return Ok(None);
    }
    let dir = c.direction(i)?;
    let dir = match dir {
        Some(d) if d.is_truthy()? => d,
        _ => return Ok(None),
    };
    if c.is_link(i)? {
        return Ok(None);
    }
    let rings = c.rings(i);
    if rings.len() != 1 || rings[0].len() != 1 || rings[0][0].1 - rings[0][0].0 < 2 {
        return Ok(None);
    }
    let (r0, r1) = rings[0][0];
    let mut primary = count(Cols::col(&c.osm_lanes, i)?.as_ref())?;
    if primary.is_none() {
        primary = count(Cols::col(&c.lane_total, i)?.as_ref())?;
    }
    // sorted(parts[i], key=lambda r: r[0]): Python's own sort
    let key = py.import("operator")?.call_method1("itemgetter", (0,))?;
    let kw = PyDict::new(py);
    kw.set_item("key", key)?;
    let rows = py.import("builtins")?.getattr("sorted")?.call((pi,), Some(&kw))?.cast_into::<PyList>()
        .map_err(|_| pyo3::exceptions::PyTypeError::new_err("lane parts"))?;
    let rows: Vec<Bound<'py, PyAny>> = rows.iter().collect();
    let mut explicit = Vec::with_capacity(rows.len());
    let mut tag_of = Vec::with_capacity(rows.len());
    for r in &rows {
        let t = r.get_item(3)?;
        let lanes = if t.is_truthy()? { t.call_method1("get", ("lanes",))? } else { py.None().into_bound(py) };
        explicit.push(count(if lanes.is_none() { None } else { Some(&lanes) })?);
        tag_of.push(t);
    }
    let fl = |r: &Bound<'py, PyAny>, k: usize| -> PyResult<f64> { r.get_item(k)?.extract::<f64>() };
    if primary.is_none() && explicit.iter().all(|n| n.is_some()) {
        let mut best = 0usize;
        let mut bk = fl(&rows[0], 1)? - fl(&rows[0], 0)?;
        for k in 1..rows.len() {
            let v = fl(&rows[k], 1)? - fl(&rows[k], 0)?;
            if v > bk {
                best = k;
                bk = v;
            }
        }
        primary = explicit[best];
    }
    let counts: Vec<Option<i64>> = explicit.iter().map(|n| n.or(primary)).collect();
    let line = &c.ll[r0..r1];
    let s = arc(line);
    if primary.is_none() || counts.iter().any(|n| n.is_none()) || s[s.len() - 1] < 1.0 {
        return Ok(None);
    }
    let counts: Vec<i64> = counts.into_iter().map(|n| n.unwrap()).collect();
    let mut distinct = counts.clone();
    distinct.sort();
    distinct.dedup();
    if distinct.len() < 2 {
        return Ok(None);
    }
    let first_a = fl(&rows[0], 0)?;
    let last_b = fl(&rows[rows.len() - 1], 1)?;
    if first_a.abs() > 1e-6 || (last_b - 1.0).abs() > 1e-6 {
        return Ok(None);
    }
    for j in 0..rows.len() {
        let (a, b) = (fl(&rows[j], 0)?, fl(&rows[j], 1)?);
        if b <= a || (j > 0 && (a - fl(&rows[j - 1], 1)?).abs() > 1e-6) {
            return Ok(None);
        }
    }
    let mut out: Vec<Row<'py>> = Vec::new();
    for (k, r) in rows.iter().enumerate() {
        let t = &tag_of[k];
        let tags = PyDict::new(py);
        if t.is_truthy()? {
            tags.update(t.cast::<pyo3::types::PyMapping>()?)?;
        }
        let n = counts[k];
        let (a, b) = (fl(r, 0)?, fl(r, 1)?);
        if let Some(last) = out.last_mut() {
            if last.n == n && last.tags.eq(&tags)? {
                last.b = b;
                last.tags = tags;
                continue;
            }
        }
        out.push(Row { a, b, tags, n });
    }
    let base = *counts.iter().min().unwrap();
    let d = if dir.gt(0)? { 1 } else { -1 };
    Ok(Some((out, base, d)))
}

/// One joint spec: (tangent, target, tail, class, turning).
type Joint = ([f64; 2], f64, bool, String, i64);

/// A joint's key: (lon, lat) by numpy's round(x, 6), -0.0 as 0.0; None for a NaN one (Python's key tuples of NaN
/// equal nothing: such a joint is never found, as there).
fn key_of(p: [f64; 2]) -> Option<(u64, u64)> {
    let z = |v: f64| if v == 0.0 { 0.0f64 } else { v };
    let (x, y) = (np_round(p[0], 6), np_round(p[1], 6));
    if x.is_nan() || y.is_nan() {
        return None;
    }
    Some((z(x).to_bits(), z(y).to_bits()))
}

/// _joint_specs(f): {joint key: [specs]} of every lane section's auxiliary lane at the section's own ends.
fn joint_specs(c: &Cols<'_>) -> PyResult<BTreeMap<(u64, u64), Vec<Joint>>> {
    let mut joints: BTreeMap<(u64, u64), Vec<Joint>> = BTreeMap::new();
    for i in 0..c.n() {
        let Some((rows, base, direction)) = sections(c, i)? else { continue };
        let (r0, r1) = c.rings(i)[0][0];
        let raw = &c.ll[r0..r1];
        let cls = c.cls_str(i)?;
        let lane_m = c.lane_m(&cls);
        for row in &rows {
            let n = row.n;
            let (side, turning) = outer_lanes(&row.tags, n)?;
            let off = placement(c.py, &row.tags, n, lane_m, direction)?;
            let Some(off) = off else { continue };
            if side == 0 || !(0 < turning && turning <= n - base) {
                continue;
            }
            if (off - (side * direction * (n - base)) as f64 * lane_m / 2.0).abs() > 0.05 {
                continue;
            }
            for (end, at) in [(0usize, row.a), (1, row.b)] {
                if !(at == 0.0 || at == 1.0) {
                    continue;
                }
                let k = raw.len();
                let e = if end == 0 { raw[0] } else { raw[k - 1] };
                let mut t = if end == 0 {
                    [raw[1][0] - raw[0][0], raw[1][1] - raw[0][1]]
                } else {
                    [raw[k - 1][0] - raw[k - 2][0], raw[k - 1][1] - raw[k - 2][1]]
                };
                t = [t[0] * e[1].to_radians().dcos(), t[1] * 1.0];
                let norm = (t[0] * t[0] + t[1] * t[1]).sqrt();
                let m = if norm > 1e-12 || norm.is_nan() { norm } else { 1e-12 };
                let t = [t[0] / m, t[1] / m];
                let target = off + (side * direction * (n - turning)) as f64 * lane_m / 2.0
                    + c.grid(&cls, n) * direction as f64;
                let tail = (end == 0) == (direction > 0);
                if let Some(key) = key_of(e) {
                    joints.entry(key).or_default().push((t, target, tail, cls.clone(), turning));
                }
            }
        }
    }
    Ok(joints)
}

fn joints_to_py<'py>(py: Python<'py>, j: &BTreeMap<(u64, u64), Vec<Joint>>) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    for (k, v) in j {
        let key = PyTuple::new(py, [f64::from_bits(k.0), f64::from_bits(k.1)])?;
        let list = PyList::empty(py);
        for (t, target, tail, cls, turning) in v {
            list.append((t[0], t[1], *target, *tail, cls.as_str(), *turning))?;
        }
        d.set_item(key, list)?;
    }
    Ok(d)
}

fn joints_from_py(d: &Bound<'_, PyAny>) -> PyResult<BTreeMap<(u64, u64), Vec<Joint>>> {
    let mut out = BTreeMap::new();
    for item in d.call_method0("items")?.try_iter()? {
        let item = item?;
        let k = item.get_item(0)?;
        let key = (k.get_item(0)?.extract::<f64>()?.to_bits(), k.get_item(1)?.extract::<f64>()?.to_bits());
        let mut v = Vec::new();
        for e in item.get_item(1)?.try_iter()? {
            let e = e?;
            v.push(([e.get_item(0)?.extract()?, e.get_item(1)?.extract()?], e.get_item(2)?.extract()?,
                    e.get_item(3)?.extract()?, e.get_item(4)?.extract()?, e.get_item(5)?.extract()?));
        }
        out.insert(key, v);
    }
    Ok(out)
}

/// lanesections._joint_specs(f)'s table ({(lon, lat) rounded: [(tx, ty, target, tail, class, turning)]}), given the
/// layer's columns (lanesections._cols).
#[pyfunction]
#[pyo3(signature = (ids, kind, po, ro, co, lonlat, cls, oneway, subclass, subclass_attr, osm_lanes, lane_total, parts,
                    lane_w, lane_w_default, shoulder))]
#[allow(clippy::too_many_arguments)]
pub fn lane_joint_specs<'py>(py: Python<'py>, ids: Vec<String>, kind: CArr1<'py, u8>,
                             po: CArr1<'py, i64>, ro: CArr1<'py, i64>,
                             co: CArr1<'py, i64>, lonlat: CArr2<'py, f64>,
                             cls: Option<Bound<'py, PyAny>>, oneway: Option<Bound<'py, PyAny>>,
                             subclass: Option<Bound<'py, PyAny>>, subclass_attr: Option<Bound<'py, PyAny>>,
                             osm_lanes: Option<Bound<'py, PyAny>>, lane_total: Option<Bound<'py, PyAny>>,
                             parts: Option<Bound<'py, PyAny>>, lane_w: BTreeMap<String, f64>, lane_w_default: f64,
                             shoulder: (f64, f64, i64)) -> PyResult<Bound<'py, PyDict>> {
    let c = Cols::new(py, ids, kind, po, ro, co, lonlat, cls, oneway, subclass, subclass_attr, osm_lanes, lane_total,
                      parts, lane_w, lane_w_default, shoulder)?;
    joints_to_py(py, &joint_specs(&c)?)
}

fn line_py<'py>(py: Python<'py>, p: &[[f64; 2]]) -> PyResult<Bound<'py, PyAny>> {
    let flat: Vec<f64> = p.iter().flat_map(|q| [q[0], q[1]]).collect();
    Ok(numpy::ndarray::Array2::from_shape_vec((p.len(), 2), flat).unwrap().into_pyarray(py).into_any())
}

/// A job of the split: (fid, feature, a, b, its line (moved) or None, tags, count, profile) as lanesections.split
/// made them.
struct Job<'py> {
    fid: String,
    i: usize,
    a: f64,
    b: f64,
    line: Option<Vec<[f64; 2]>>,
    tags: Option<Bound<'py, PyDict>>,
    n: Option<i64>,
    prof: Option<Bound<'py, PyDict>>,
}

/// Python's round(x) of a float to an int (ties to even): ValueError for NaN, OverflowError for an infinity.
fn pyround_int(x: f64) -> PyResult<i128> {
    if x.is_nan() {
        return Err(pyo3::exceptions::PyValueError::new_err("cannot convert float NaN to integer"));
    }
    if x.is_infinite() {
        return Err(pyo3::exceptions::PyOverflowError::new_err("cannot convert float infinity to integer"));
    }
    let r = x.round_ties_even();
    if r.abs() >= 1e38 {
        return Err(pyo3::exceptions::PyOverflowError::new_err("a lane section's fraction past any id"));
    }
    Ok(r as i128)
}

/// lanesections.split's plan: None where the layer is kept as it is, else its jobs sorted by id [(fid, feature, a, b,
/// line (k, 2) or None, tags, count, profile)]. context(lat, lon): the layer of the cell holding a mouth (or None);
/// joint_specs(layer): lanesections._joint_specs; structure_mouth(i, end): lanesections._structure_mouth(f, i, end).
#[pyfunction]
#[pyo3(signature = (ids, kind, po, ro, co, lonlat, cls, oneway, subclass, subclass_attr, osm_lanes, lane_total, parts,
                    lane_w, lane_w_default, shoulder, layer, context, joint_specs, structure_mouth))]
#[allow(clippy::too_many_arguments)]
pub fn lane_split_jobs<'py>(py: Python<'py>, ids: Vec<String>, kind: CArr1<'py, u8>,
                            po: CArr1<'py, i64>, ro: CArr1<'py, i64>,
                            co: CArr1<'py, i64>, lonlat: CArr2<'py, f64>,
                            cls: Option<Bound<'py, PyAny>>, oneway: Option<Bound<'py, PyAny>>,
                            subclass: Option<Bound<'py, PyAny>>, subclass_attr: Option<Bound<'py, PyAny>>,
                            osm_lanes: Option<Bound<'py, PyAny>>, lane_total: Option<Bound<'py, PyAny>>,
                            parts: Option<Bound<'py, PyAny>>, lane_w: BTreeMap<String, f64>, lane_w_default: f64,
                            shoulder: (f64, f64, i64), layer: Bound<'py, PyAny>,
                            context: Option<Bound<'py, PyAny>>, joint_specs: Bound<'py, PyAny>,
                            structure_mouth: Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyList>>> {
    let c = Cols::new(py, ids, kind, po, ro, co, lonlat, cls, oneway, subclass, subclass_attr, osm_lanes, lane_total,
                      parts, lane_w, lane_w_default, shoulder)?;
    let n = c.n();
    let mut plans: BTreeMap<usize, (Vec<Row<'py>>, i64, i64)> = BTreeMap::new();
    for i in 0..n {
        if let Some(got) = sections(&c, i)? {
            plans.insert(i, got);
        }
    }
    let context = context.filter(|x| !x.is_none());
    if plans.is_empty() && context.is_none() {
        return Ok(None);
    }
    let mut jobs: Vec<Job<'py>> = Vec::new();
    for i in 0..n {
        let Some((rows, base, direction)) = plans.get(&i) else {
            jobs.push(Job { fid: c.ids[i].clone(), i, a: 0.0, b: 1.0, line: None, tags: None, n: None, prof: None });
            continue;
        };
        let (base, direction) = (*base, *direction);
        let (r0, r1) = c.rings(i)[0][0];
        let ll = &c.ll[r0..r1];
        let cls = c.cls_str(i)?;
        let lane_m = c.lane_m(&cls);
        let mut offsets = Vec::with_capacity(rows.len());
        for r in rows {
            // (`or 0.`: None and -0.0 alike)
            let o = placement(py, &r.tags, r.n, lane_m, direction)?;
            offsets.push(match o { Some(v) if v != 0.0 => v, _ => 0.0 });
        }
        let base_offsets: Vec<f64> = {
            let v: Vec<f64> = offsets.iter().zip(rows).filter(|(_, r)| r.n == base).map(|(o, _)| *o).collect();
            if v.is_empty() { vec![0.0] } else { v }
        };
        let reference = base_offsets[0];
        let pmax = base_offsets.iter().copied().fold(None, |m: Option<f64>, x| Some(match m { None => x, Some(m) => if x > m { x } else { m } })).unwrap();
        let pmin = base_offsets.iter().copied().fold(None, |m: Option<f64>, x| Some(match m { None => x, Some(m) => if x < m { x } else { m } })).unwrap();
        let common_grid = pmax - pmin < 0.05;
        let full = arc(ll);
        let full_len = full[full.len() - 1];
        for (j, r) in rows.iter().enumerate() {
            let off = offsets[j];
            let shift = off - reference;
            let mut extra = (r.n - base) as f64 * lane_m;
            if !common_grid || (shift.abs() - extra / 2.0).abs() > 0.05 {
                extra = 0.0;
            }
            let (mut p, s) = sample(ll, r.a, r.b, extra != 0.0)?;
            let length = s[s.len() - 1];
            let mut reach = [0.0f64, 0.0];
            let mut ends = [1.0f64, 1.0];
            let turning;
            let placed: Vec<f64>;
            if extra != 0.0 {
                for (end, nb) in [(0usize, j as i64 - 1), (1, j as i64 + 1)] {
                    if !(0 <= nb && (nb as usize) < rows.len()) {
                        continue;
                    }
                    let other = rows[nb as usize].n;
                    let change = (r.n - other) as f64 * lane_m;
                    if change > 0.0 && (off - offsets[nb as usize] - npsign(shift) * change / 2.0).abs() < 0.05 {
                        // min(90., change * 20., length * .45): the first of the least
                        let mut m = 90.0;
                        for v in [change * 20.0, length * 0.45] {
                            if v < m {
                                m = v;
                            }
                        }
                        reach[end] = m;
                        ends[end] = 1.0 - change / extra;
                    }
                }
                let (side, t) = outer_lanes(&r.tags, r.n)?;
                turning = if (side * direction) as f64 != npsign(shift) { 0 } else { t };
                placed = ease(&s, length, reach, ends).iter().map(|g| reference + shift * g).collect();
            } else {
                placed = vec![off];
                turning = 0;
            }
            let moved = placed.iter().any(|v| v.abs() > 1e-9);
            if moved {
                p = offset(&p, &placed);
            }
            let ends_off = if moved { (placed[0], placed[placed.len() - 1]) } else { (0.0, 0.0) };
            let prof = PyDict::new(py);
            prof.set_item("offset", off)?;
            prof.set_item("shift", shift)?;
            prof.set_item("extra", extra)?;
            prof.set_item("turning", turning)?;
            prof.set_item("end_off", ends_off)?;
            prof.set_item("reach", PyList::new(py, reach)?)?;
            prof.set_item("ends", PyList::new(py, ends)?)?;
            prof.set_item("length", length)?;
            prof.set_item("phase", full_len * r.a)?;
            prof.set_item("stations", PyList::new(py, &s)?)?;
            let pl = PyList::empty(py);
            for q in &p {
                pl.append(PyList::new(py, [q[0], q[1]])?)?;
            }
            prof.set_item("line", pl)?;
            let fid = format!("{}~lanes{:09}-{:09}", c.ids[i], pyround_int(r.a * 1e9)?, pyround_int(r.b * 1e9)?);
            jobs.push(Job { fid, i, a: r.a, b: r.b, line: Some(p), tags: Some(r.tags.clone()), n: Some(r.n),
                            prof: Some(prof) });
        }
    }
    ramp_mouths(&c, &mut jobs, &layer, context.as_ref(), &joint_specs, &structure_mouth)?;
    if plans.is_empty() && !jobs.iter().any(|j| j.line.is_some()) {
        return Ok(None);
    }
    jobs.sort_by(|x, y| x.fid.cmp(&y.fid));
    let out = PyList::empty(py);
    for j in jobs {
        let line = match &j.line { Some(p) => line_py(py, p)?, None => py.None().into_bound(py) };
        out.append((j.fid, j.i, j.a, j.b, line, j.tags, j.n, j.prof))?;
    }
    Ok(Some(out))
}

/// _ramp_mouths(f, jobs, context): each one-way highway link whose mouth is the one auxiliary lane's joint of a lane
/// section (its own cell's specs: context's) moves its line out to that lane, eased off along it.
fn ramp_mouths<'py>(c: &Cols<'py>, jobs: &mut [Job<'py>], layer: &Bound<'py, PyAny>,
                    context: Option<&Bound<'py, PyAny>>,
                    joint_specs: &Bound<'py, PyAny>, structure_mouth: &Bound<'py, PyAny>) -> PyResult<()> {
    let mut joints = joints_from_py(&joint_specs.call1((layer,))?)?;
    let candidate = |job: &Job<'py>| -> PyResult<bool> {
        Ok(job.line.is_none() && c.highway(job.i)? && c.kind[job.i] == LINE)
    };
    if let Some(ctx) = context {
        for job in jobs.iter() {
            if !candidate(job)? || !c.is_link(job.i)? {
                continue;
            }
            let rings = c.rings(job.i);
            if rings.len() != 1 || rings[0].len() != 1 {
                continue;
            }
            let (r0, r1) = rings[0][0];
            if r1 <= r0 {
                // (parts[0][0][[0, -1]] of an empty ring: numpy's IndexError)
                return Err(pyo3::exceptions::PyIndexError::new_err("index 0 is out of bounds for axis 0 with size 0"));
            }
            for node in [c.ll[r0], c.ll[r1 - 1]] {
                let source = ctx.call1((node[1], node[0]))?;
                if !source.is_none() {
                    let theirs = joints_from_py(&joint_specs.call1((source,))?)?;
                    if let Some(key) = key_of(node) {
                        joints.insert(key, theirs.get(&key).cloned().unwrap_or_default());
                    }
                }
            }
        }
    }
    if joints.is_empty() {
        return Ok(());
    }
    for job in jobs.iter_mut() {
        if !candidate(job)? {
            continue;
        }
        let i = job.i;
        let Some(direction) = c.direction(i)? else { continue };
        if !direction.is_truthy()? || !c.is_link(i)? {
            continue;
        }
        let rings = c.rings(i);
        if rings.len() != 1 || rings[0].len() != 1 {
            continue;
        }
        let (r0, r1) = rings[0][0];
        let raw = &c.ll[r0..r1];
        if raw.len() < 2 {
            continue;
        }
        let dpos = direction.gt(0)?;
        let mut moved: Option<Vec<[f64; 2]>> = None;
        for end in [0usize, 1] {
            let k = raw.len();
            let node = if end == 0 { raw[0] } else { raw[k - 1] };
            let Some(cands) = key_of(node).and_then(|k| joints.get(&k)) else { continue };
            if cands.len() != 1 {
                continue;
            }
            let (tangent, target, main_tail, ref cls, turning) = cands[0];
            if structure_mouth.call1((i, if end == 0 { 0i64 } else { -1i64 }))?.is_truthy()? {
                continue;
            }
            let mut ramp_lanes = count(Cols::col(&c.osm_lanes, i)?.as_ref())?;
            if ramp_lanes.is_none() {
                ramp_lanes = count(Cols::col(&c.lane_total, i)?.as_ref())?;
            }
            let ramp_lanes = ramp_lanes.unwrap_or(1);
            if *cls != c.cls_str(i)? || turning != ramp_lanes || main_tail == ((end == 0) == dpos) {
                continue;
            }
            let (line, stations) = sample(raw, 0.0, 1.0, true)?;
            let sc = [111320.0 * node[1].to_radians().dcos(), 110540.0];
            let xy: Vec<[f64; 2]> = line.iter().map(|q| [(q[0] - node[0]) * sc[0], (q[1] - node[1]) * sc[1]]).collect();
            let mut dr = if end == 0 {
                [(raw[1][0] - raw[0][0]) * sc[0], (raw[1][1] - raw[0][1]) * sc[1]]
            } else {
                [(raw[k - 1][0] - raw[k - 2][0]) * sc[0], (raw[k - 1][1] - raw[k - 2][1]) * sc[1]]
            };
            let norm = (dr[0] * dr[0] + dr[1] * dr[1]).sqrt();
            let m = if norm > 1e-12 || norm.is_nan() { norm } else { 1e-12 };
            dr = [dr[0] / m, dr[1] / m];
            let dot = dr[0] * tangent[0] + dr[1] * tangent[1];
            if dot.abs() < 0.85 {
                continue;
            }
            let last = stations[stations.len() - 1];
            let reach = { let r = last * 0.45; if r < 160.0 { r } else { 160.0 } };
            let rm = if reach > 1e-9 || reach.is_nan() { reach } else { 1e-9 };
            let sg = npsign(target);
            let shift: Vec<f64> = (0..line.len()).map(|q| {
                let lateral = tangent[0] * xy[q][1] - tangent[1] * xy[q][0];
                let from_end = if end == 0 { stations[q] } else { last - stations[q] };
                let t = clip(from_end / rm, 0.0, 1.0);
                let fade = 1.0 - t * t * (3.0 - 2.0 * t);
                sg * npmax(target.abs() - lateral * sg, 0.0) * fade / dot
            }).collect();
            let base = moved.take().unwrap_or_else(|| line.clone());
            moved = Some(offset(&base, &shift));
        }
        if moved.is_some() {
            job.line = moved;
        }
    }
    Ok(())
}

/// lanesections._ease(s, length, reach, ends) of stations s.
#[pyfunction]
pub fn lane_ease<'py>(py: Python<'py>, s: CArr1<'py, f64>, length: f64, reach: (f64, f64), ends: (f64, f64))
                      -> PyResult<Bound<'py, numpy::PyArray1<f64>>> {
    Ok(ease(s.as_slice()?, length, [reach.0, reach.1], [ends.0, ends.1]).into_pyarray(py))
}

/// lanesections._offset(ll, offsets): the lon / lat line moved offsets m (one, or one per point) to its left.
#[pyfunction]
pub fn lane_offset<'py>(py: Python<'py>, ll: CArr2<'py, f64>, d: CArr1<'py, f64>)
                        -> PyResult<Bound<'py, numpy::PyArray2<f64>>> {
    let a = ll.as_array();
    check_lonlat(a.ncols())?;
    let p: Vec<[f64; 2]> = (0..a.nrows()).map(|k| [a[[k, 0]], a[[k, 1]]]).collect();
    let d = d.as_slice()?;
    if p.is_empty() || !(d.len() == 1 || d.len() == p.len()) {
        return Err(pyo3::exceptions::PyValueError::new_err("lane_offset: ll (k >= 1, 2), d (1,) or (k,)"));
    }
    let out = offset(&p, d);
    let flat: Vec<f64> = out.iter().flat_map(|q| [q[0], q[1]]).collect();
    Ok(numpy::ndarray::Array2::from_shape_vec((out.len(), 2), flat).unwrap().into_pyarray(py))
}

/// roads.lane_tag_parts' stretches from a feature's lane tag parts ([(start, end, way, tags or None)]), None where it
/// has none (the caller makes the whole-feature stretch).
#[pyfunction]
pub fn lane_tag_parts<'py>(py: Python<'py>, parts: Bound<'py, PyAny>) -> PyResult<Option<Bound<'py, PyList>>> {
    if parts.is_none() || !parts.is_truthy()? {
        return Ok(None);
    }
    let out = PyList::empty(py);
    for r in parts.try_iter()? {
        let r = r?;
        // (for a, b, w, t in parts[i]: exactly four values each)
        let vals: Vec<Bound<'py, PyAny>> = r.try_iter()?.collect::<PyResult<_>>()?;
        if vals.len() != 4 {
            return Err(pyo3::exceptions::PyValueError::new_err(format!(
                "{} values to unpack (expected 4)", if vals.len() > 4 { "too many" } else { "not enough" })));
        }
        let t = if vals[3].is_truthy()? { vals[3].clone() } else { py.None().into_bound(py) };
        out.append(PyTuple::new(py, [vals[0].clone(), vals[1].clone(), vals[2].clone(), t])?)?;
    }
    Ok(Some(out))
}
