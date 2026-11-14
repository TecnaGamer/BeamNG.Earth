//! roads.road_specs / bridge_specs / tunnel_specs / _structure_width, a layer at a time: what width, surface, town
//! test, direction and lanes each road of a map cell is drawn with (the road-semantics port's step 5,
//! .claude/work/road_semantics_rust_plan.md). The Python made them feature by feature (rule lookups, small numpy arrays,
//! namedtuple replaces: ~5 % of a cold Manhattan road tile's CPU, with the per-feature level-range calls under it);
//! here one call makes a layer's, the heavy parts (the land cover's sample points, the divided-road pairing, the pass
//! roads' bends) without the interpreter lock.
//!
//! Python keeps the glue: the layer's columns made once (roads._spec_inputs: classes, rules' values, OpenStreetMap
//! tags, surface kinds by roads.surface_kind, ring radii), the lazy lane flow / regional surface / land cover (asked
//! back through callbacks only when a road needs them, as the Python did), the opt-in French and European passes, and
//! the Spec namedtuples. The numbers are the Python's bit for bit: numpy's pairwise sums, linspace and interp, libm's
//! cos / hypot, Python's min / max (the first of equals) and round (half to even).

use std::collections::HashMap;

use numpy::ndarray::ArrayView2;
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::gradesep::np_interp;
use crate::levels::np_sum;
use crate::{dual_side_core, polyline_dist_core, tight_bends_core};

type P = [f64; 2];

// kinds: roads._KINDS' order
const PAVED: i8 = 0;
const GRAVEL: i8 = 2;

// class flags (RoadRules: from the Python's class sets and names)
const F_HW: u32 = 1;             // roadlanes.HIGHWAYS
const F_PARK: u32 = 2;           // PARKING_CLASSES
const F_DUAL: u32 = 4;           // DUAL_CLASSES
const F_MTN: u32 = 8;            // MOUNTAIN_CLASSES
const F_REG: u32 = 16;           // REGIONAL_CLASSES (lazily: regional_unpaved's)
const F_SEG: u32 = 32;           // SE_GRAVEL_CLASSES
const F_SENS: u32 = 64;          // SE_NO_STRIPS
const F_MOTORWAY: u32 = 128;
const F_TRUNK: u32 = 256;
const F_TRACK: u32 = 512;
const F_SERVICE: u32 = 1024;
const F_PRIMARY: u32 = 2048;
const F_TERTIARY: u32 = 4096;
const F_RCAND: u32 = 8192;       // _rural_connectors' links: trunk, primary, secondary, tertiary
const F_RDIV: u32 = 16384;       // ... the divided roads they cross: motorway, trunk, primary

/// Python's max(a, b): a unless b > a.
#[inline]
fn pmax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// Python's min(a, b): a unless b < a.
#[inline]
fn pmin(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// Python's truth of a float (NaN is true).
#[inline]
fn truthy(v: f64) -> bool {
    v != 0.0
}

#[derive(Clone)]
struct ClassRow {
    w: f64,           // STYLE width
    ow: f64,          // ONEWAY_STYLE (NaN: none)
    lw: f64,          // LANE_W (NaN: LANE_W_DEFAULT)
    ua: f64,          // URBAN_ARTERIAL (NaN: none)
    es: f64,          // EDGE_STRIP_CLASS_M (NaN: EDGE_STRIP_M)
    flags: u32,
}

/// The constants the width rules read (roads.py's), in one object made once per process (roads._road_rules).
#[pyclass(module = "earthfast", frozen)]
pub struct RoadRules {
    names: Vec<String>,
    index: HashMap<String, usize>,
    rows: Vec<ClassRow>,
    other: ClassRow,            // a class not in STYLE: STYLE["unknown"]'s width, in no set
    lane_w_default: f64,
    link_shoulder: f64,
    link_lane: f64,
    link_m: Vec<(i64, f64)>,
    shoulder: f64,
    shoulder_wide: f64,
    shoulder_wide_lanes: f64,
    parking: f64,
    motorway_rural: f64,
    edge_strip: f64,
    ring_lane: f64,
    ring_extra_lane: f64,
    ring_small_r: f64,
    ring_small_per: f64,
    ring_small_max: f64,
    dual_min_median: f64,
    dual_min_w: f64,
    urban_median: f64,
    urban_median_gap: f64,
    urban_dual_max_w: f64,
    ramp_deg: f64,
}

fn prm_get(prm: &HashMap<String, f64>, k: &str) -> PyResult<f64> {
    prm.get(k).copied().ok_or_else(|| PyValueError::new_err(format!("RoadRules: no {k}")))
}

impl RoadRules {
    fn row(&self, c: usize) -> &ClassRow {
        self.rows.get(c).unwrap_or(&self.other)
    }

    fn has(&self, c: usize, f: u32) -> bool {
        self.row(c).flags & f != 0
    }

    /// roads.carriageway (cls: a class index, or rows.len() for one not in STYLE): (width, whether it is rule_width
    /// itself: the caller hands back its own object, an np.float32 stays one).
    #[allow(clippy::too_many_arguments)]
    fn cw(&self, c: usize, built: bool, urban: bool, rule_w: Option<f64>, oneway: bool, lanes: Option<f64>, link: bool)
          -> (f64, bool) {
        let r = self.row(c);
        let fl = r.flags;
        let lt = |l: Option<f64>| l.filter(|v| truthy(*v));        // (lanes or ...)
        if built && link && fl & (F_MOTORWAY | F_TRUNK) != 0 {
            let x = lt(lanes).unwrap_or(if oneway { 1.0 } else { 2.0 }) * self.link_lane + 2.0 * self.link_shoulder;
            return match rule_w.filter(|v| truthy(*v)) {
                Some(v) => if x > v { (x, false) } else { (v, true) },
                None => (pmax(0.0, x), false),
            };
        }
        if built && fl & F_MOTORWAY != 0 && oneway {
            let x = lt(lanes).unwrap_or(2.0) * r.lw_or(self.lane_w_default) + 2.0 * self.shoulder;
            return match rule_w.filter(|v| truthy(*v)) {
                Some(v) => if x > v { (x, false) } else { (v, true) },
                None => (pmax(0.0, x), false),
            };
        }
        if let Some(v) = rule_w {
            return (v, true);
        }
        let mut lanes = lanes;
        if lanes.is_none() && fl & (F_MOTORWAY | F_TRUNK) != 0 {
            lanes = Some(2.0);
        }
        let mut w = r.w;
        if oneway && !r.ow.is_nan() {
            w = r.ow;
        }
        let lanes_t = lanes.filter(|v| truthy(*v));
        if let Some(l) = lanes_t {
            w = l * r.lw_or(self.lane_w_default);
        }
        if !built {
            return (w, false);
        }
        if let Some(l) = lanes_t {
            if !urban && oneway && fl & F_MOTORWAY != 0 {
                return (w + 2.0 * (if l >= self.shoulder_wide_lanes { self.shoulder_wide } else { self.shoulder }), false);
            }
        }
        if urban && !oneway && !r.ua.is_nan() && lanes_t.is_none() {
            return (r.ua, false);
        }
        if urban && fl & F_PARK != 0 {
            return (w + (if oneway { 1.0 } else { 2.0 }) * self.parking, false);
        }
        if !urban && fl & F_SERVICE == 0 {
            if oneway && fl & F_MOTORWAY != 0 {
                return (self.motorway_rural + 2.0 * self.shoulder, false);
            }
            return (w + 2.0 * (if r.es.is_nan() { self.edge_strip } else { r.es }), false);
        }
        (w, false)
    }

    /// roads._link_width.
    fn link_width(&self, lanes: i64) -> f64 {
        for &(k, v) in &self.link_m {
            if k == lanes {
                return v;
            }
        }
        let two = self.link_m.iter().find(|kv| kv.0 == 2).map(|kv| kv.1).unwrap_or(f64::NAN);
        two + ((lanes - 2) as f64) * self.link_lane
    }

    /// roads.ring_width.
    fn ring_w(&self, lanes: Option<f64>, radius: Option<f64>) -> f64 {
        let l = match lanes.filter(|v| truthy(*v)) { Some(v) => v.trunc(), None => 1.0 };
        let mut w = self.ring_lane + self.ring_extra_lane * (if l > 1.0 { l } else { 1.0 } - 1.0);
        if let Some(r) = radius.filter(|v| truthy(*v)) {
            if 3.0 <= r && r < self.ring_small_r {
                w += pmin(self.ring_small_max, self.ring_small_per * (self.ring_small_r - r));
            }
        }
        w
    }

    /// roads.dual_cap.
    fn cap(&self, gap: f64, w: f64, lanes: Option<f64>, other: Option<f64>) -> f64 {
        let share = match (lanes.filter(|v| truthy(*v)), other.filter(|v| truthy(*v))) {
            (Some(a), Some(b)) => a / (a + b),
            _ => 0.5,
        };
        pmin(w, pmax(2.0 * (gap - self.dual_min_median) * share, self.dual_min_w))
    }

    fn class_of(&self, cls: &str) -> usize {
        self.index.get(cls).copied().unwrap_or(self.rows.len())
    }
}

impl ClassRow {
    fn lw_or(&self, d: f64) -> f64 {
        if self.lw.is_nan() { d } else { self.lw }
    }
}

/// One road's spec as the kernels carry it (roads.Spec's fields; walk is always 2.5 here, france / state / street
/// are the Python's).
#[derive(Clone)]
struct Sp {
    width: f64,
    w32: bool,              // width is the width rule's np.float32 itself
    kind: i8,
    urban: bool,
    cls: usize,
    oneway: bool,
    dir: i64,
    kmh: Option<f64>,
    gap: Option<f64>,
    median: Option<f64>,
    lanes: Option<i64>,
    surf: i32,
    link: bool,
    sides: i8,
    parking: bool,
    mountain: bool,
}

fn med_truthy(m: Option<f64>) -> bool {
    m.is_some_and(truthy)
}

/// A layer's columns (roads._spec_inputs) and geometry.
struct Layer<'a> {
    n: usize,
    ll: ArrayView2<'a, f64>,
    co: Vec<usize>,
    ro: Vec<usize>,
    po: Vec<usize>,
    cls: Vec<i32>,          // class index in STYLE, -1 none (any subtype: _ramp_junction reads it)
    elig: Vec<bool>,        // a road (subtype road, class in STYLE, built)
    has_w: Vec<bool>,
    wv: Vec<f64>,
    osm_w: Vec<f64>,
    osm_lanes: Vec<f64>,
    osm_ms: Vec<f64>,
    has_ow: Vec<bool>,
    owv: Vec<f64>,
    has_k: Vec<bool>,
    kv: Vec<f64>,
    ncon: Option<Vec<i64>>,
    lens: Vec<f64>,
    over: Vec<i64>,
    gen: Vec<i64>,
    ring: Vec<bool>,
    radius: Vec<f64>,
    link: Vec<bool>,
    skind: Vec<i8>,
    stok: Vec<i32>,
    reg_pre: Vec<bool>,
    name: Option<Vec<i64>>,
    eu_country: Vec<bool>,
    sides: Vec<i8>,
    sweden: Vec<bool>,
    se_tag: Vec<bool>,
    se_kind: Vec<i8>,
    se_tok: Vec<i32>,
    aisle: Vec<bool>,
    built: [bool; 4],
}

fn col<'py, T: numpy::Element + Copy>(d: &Bound<'py, PyDict>, k: &str, n: usize) -> PyResult<Vec<T>> {
    let v = d.get_item(k)?.ok_or_else(|| PyValueError::new_err(format!("spec inputs: no {k}")))?;
    let a: PyReadonlyArray1<T> = v.extract().map_err(|e| PyValueError::new_err(format!("spec inputs: {k}: {e}")))?;
    let out = a.as_array().to_vec();
    if out.len() != n {
        return Err(PyValueError::new_err(format!("spec inputs: {k}: {} values for {n} features", out.len())));
    }
    Ok(out)
}

fn opt_col<'py, T: numpy::Element + Copy>(d: &Bound<'py, PyDict>, k: &str, n: usize) -> PyResult<Option<Vec<T>>> {
    match d.get_item(k)? {
        Some(v) if !v.is_none() => col(d, k, n).map(Some),
        _ => Ok(None),
    }
}

impl<'a> Layer<'a> {
    #[allow(clippy::too_many_arguments)]
    fn new(ll: ArrayView2<'a, f64>, co: &PyReadonlyArray1<i64>, ro: &PyReadonlyArray1<i64>,
           po: &PyReadonlyArray1<i64>, d: &Bound<'_, PyDict>, built: [bool; 4]) -> PyResult<Layer<'a>> {
        let us = |a: &PyReadonlyArray1<i64>| a.as_array().iter().map(|&v| v.max(0) as usize).collect::<Vec<_>>();
        let po = us(po);
        let n = po.len().saturating_sub(1);
        if ll.ncols() != 2 && ll.nrows() > 0 {
            return Err(PyValueError::new_err("spec inputs: lonlat (k, 2)"));
        }
        Ok(Layer {
            n, ll, co: us(co), ro: us(ro), po,
            cls: col(d, "cls", n)?, elig: col(d, "elig", n)?, has_w: col(d, "has_w", n)?, wv: col(d, "wv", n)?,
            osm_w: col(d, "osm_w", n)?, osm_lanes: col(d, "osm_lanes", n)?, osm_ms: col(d, "osm_ms", n)?,
            has_ow: col(d, "has_ow", n)?, owv: col(d, "owv", n)?, has_k: col(d, "has_k", n)?, kv: col(d, "kv", n)?,
            ncon: opt_col(d, "ncon", n)?, lens: col(d, "lens", n)?, over: col(d, "over", n)?, gen: col(d, "gen", n)?,
            ring: col(d, "ring", n)?, radius: col(d, "radius", n)?, link: col(d, "link", n)?,
            skind: col(d, "skind", n)?, stok: col(d, "stok", n)?, reg_pre: col(d, "reg_pre", n)?,
            name: opt_col(d, "name", n)?, eu_country: col(d, "eu_country", n)?, sides: col(d, "sides", n)?,
            sweden: col(d, "sweden", n)?, se_tag: col(d, "se_tag", n)?, se_kind: col(d, "se_kind", n)?,
            se_tok: col(d, "se_tok", n)?, aisle: col(d, "aisle", n)?, built,
        })
    }

    /// The coordinates co[ro[po[i]]]..co[ro[po[i + 1]]] (all of feature i's).
    fn all_pts(&self, i: usize) -> (usize, usize) {
        (self.co[self.ro[self.po[i]]], self.co[self.ro[self.po[i + 1]]])
    }

    fn pt(&self, k: usize) -> P {
        [self.ll[[k, 0]], self.ll[[k, 1]]]
    }

    fn rule_w(&self, i: usize) -> Option<f64> {
        if self.has_w[i] && 1.0 <= self.wv[i] && self.wv[i] <= 40.0 {
            return Some(self.wv[i]);
        }
        let o = self.osm_w[i];
        if o.is_finite() { Some(o) } else { None }
    }

    /// _rule_value(width) is not None or _osm(width) is not None.
    fn mapped_w(&self, i: usize) -> bool {
        self.has_w[i] || self.osm_w[i].is_finite()
    }

    /// The width rule's raw value (np.float32: flag) or OSM's, as _rural_connectors and the primary rule pass it.
    fn raw_w(&self, i: usize) -> (Option<f64>, bool) {
        if self.has_w[i] {
            (Some(self.wv[i]), true)
        } else if self.osm_w[i].is_finite() {
            (Some(self.osm_w[i]), false)
        } else {
            (None, false)
        }
    }

    fn dir(&self, i: usize) -> i64 {
        if !self.has_ow[i] { 0 } else if self.owv[i] < 0.0 { -1 } else { 1 }
    }

    fn built(&self, k: i8) -> bool {
        k >= 0 && (k as usize) < 4 && self.built[k as usize]
    }
}

/// roads._ramp_junction: per feature, a motorway / trunk (not a ramp) with an end meeting both a ramp's end and
/// another through motorway / trunk's.
fn ramp_junction(l: &Layer, rules: &RoadRules) -> Vec<bool> {
    let n = l.n;
    let hw = |i: usize| l.cls[i] >= 0 && rules.has(l.cls[i] as usize, F_HW);
    let mut ends: HashMap<(i64, i64), Vec<(usize, bool)>> = HashMap::new();
    let mut mine: Vec<Vec<(i64, i64)>> = vec![Vec::new(); n];
    for i in 0..n {
        if !hw(i) || l.po[i + 1] <= l.po[i] {
            continue;
        }
        let p = l.po[i];
        if l.ro[p + 1] <= l.ro[p] {
            continue;
        }
        let r = l.ro[p];
        let (a, b) = (l.co[r], l.co[r + 1]);
        if b < a + 2 {
            continue;
        }
        for k in [a, b - 1] {
            let q = l.pt(k);
            let key = ((q[0] / rules.ramp_deg).round_ties_even() as i64, (q[1] / rules.ramp_deg).round_ties_even() as i64);
            ends.entry(key).or_default().push((i, l.link[i]));
            mine[i].push(key);
        }
    }
    let mut out = vec![false; n];
    for i in 0..n {
        if !hw(i) || l.link[i] {
            continue;
        }
        for k in &mine[i] {
            let (mut lk, mut nl) = (false, false);
            for dk in [(0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)] {
                if let Some(v) = ends.get(&(k.0 + dk.0, k.1 + dk.1)) {
                    for &(j, link) in v {
                        if j != i {
                            if link { lk = true } else { nl = true }
                        }
                    }
                }
            }
            if lk && nl {
                out[i] = true;
                break;
            }
        }
    }
    out
}

/// The lane counts of features idx (roads._lane_count), the lane flow asked of flow_fn only when one needs it.
fn lane_counts(py: Python<'_>, l: &Layer, rules: &RoadRules, idx: &[usize], flow_fn: &Bound<'_, PyAny>)
               -> PyResult<Vec<Option<i64>>> {
    let mut rj: Option<Vec<bool>> = None;
    let mut out: Vec<Option<i64>> = Vec::with_capacity(idx.len());
    let mut need = Vec::new();
    for (t, &i) in idx.iter().enumerate() {
        if l.over[i] >= 0 {
            out.push(Some(l.over[i]));
            continue;
        }
        if l.osm_lanes[i].is_finite() {
            out.push(Some(l.osm_lanes[i].trunc() as i64));
            continue;
        }
        let g = if l.gen[i] >= 0 { Some(l.gen[i]) } else { None };
        let c = l.cls[i].max(0) as usize;
        if g.is_none() {
            let hw = rules.has(c, F_HW);
            let ask = !hw || rj.get_or_insert_with(|| ramp_junction(l, rules))[i];
            if ask {
                need.push(t);
            }
        }
        out.push(g);
    }
    if !need.is_empty() {
        let flow: Vec<Option<i64>> = flow_fn.call0()?.extract()?;
        if flow.len() != l.n {
            return Err(PyValueError::new_err("lane flow: one value per feature"));
        }
        for &t in &need {
            out[t] = flow[idx[t]];
        }
    }
    let _ = py;
    for (t, &i) in idx.iter().enumerate() {
        if l.over[i] >= 0 || l.osm_lanes[i].is_finite() {
            continue;
        }
        if out[t].is_none() && !l.ring[i] {
            let c = l.cls[i].max(0) as usize;
            if l.link[i] {
                out[t] = Some(if l.has_ow[i] { 1 } else { 2 });
            } else if rules.has(c, F_HW) {
                out[t] = Some(2);
            }
        }
    }
    Ok(out)
}

/// roads._urban_samples: the line's points every URBAN_SAMPLE_M at most (endpoints kept), by distance.
fn urban_samples(ll: &[P], step_m: f64) -> Vec<P> {
    if ll.len() < 2 {
        return ll.to_vec();
    }
    let (mut mn, mut mx) = (ll[0][1], ll[0][1]);
    for p in ll {
        if p[1] < mn || mn.is_nan() { mn = p[1] }
        if p[1] > mx || mx.is_nan() { mx = p[1] }
    }
    let lat = 0.5 * (mn + mx);
    let sx = 111320.0 * lat.to_radians().cos();
    let mut dist = vec![0.0f64; ll.len()];
    for k in 1..ll.len() {
        let (dx, dy) = ((ll[k][0] - ll[k - 1][0]) * sx, (ll[k][1] - ll[k - 1][1]) * 110540.0);
        dist[k] = dist[k - 1] + (dx * dx + dy * dy).sqrt();
    }
    let total = dist[ll.len() - 1];
    if total < 1e-8 {
        return vec![ll[0]];
    }
    let count = std::cmp::max(2, (total / step_m).ceil() as usize + 1);
    let step = total / ((count - 1) as f64);
    let xs: Vec<f64> = ll.iter().map(|p| p[0]).collect();
    let ys: Vec<f64> = ll.iter().map(|p| p[1]).collect();
    (0..count).map(|k| {
        let s = if k == count - 1 { total } else { (k as f64) * step };
        [np_interp(s, &dist, &xs), np_interp(s, &dist, &ys)]
    }).collect()
}

/// roads._beside: the points d m left and right of each vertex of each line (counts of pts in turn).
fn beside(pts: &[P], counts: &[usize], d: f64) -> (Vec<P>, Vec<P>) {
    let mut left = pts.to_vec();
    let mut right = pts.to_vec();
    let mut k = 0;
    for &c in counts {
        let p = &pts[k..k + c];
        if c >= 2 {
            let lat: Vec<f64> = p.iter().map(|q| q[1]).collect();
            let mx = 111320.0 * (np_sum(&lat) / (c as f64)).to_radians().cos();
            let m = [mx, 110540.0];
            let q: Vec<P> = p.iter().map(|v| [v[0] * m[0], v[1] * m[1]]).collect();
            for j in 0..c {
                let t = if j == 0 {
                    [(q[1][0] - q[0][0]) / 1.0, (q[1][1] - q[0][1]) / 1.0]
                } else if j == c - 1 {
                    [(q[c - 1][0] - q[c - 2][0]) / 1.0, (q[c - 1][1] - q[c - 2][1]) / 1.0]
                } else {
                    [(q[j + 1][0] - q[j - 1][0]) / 2.0, (q[j + 1][1] - q[j - 1][1]) / 2.0]
                };
                let h = t[0].hypot(t[1]);
                let h = if h.is_nan() { h } else if h > 1e-9 { h } else { 1e-9 };
                let t = [t[0] / h, t[1] / h];
                let off = [(-t[1] * d) / m[0], (t[0] * d) / m[1]];
                left[k + j] = [p[j][0] + off[0], p[j][1] + off[1]];
                right[k + j] = [p[j][0] - off[0], p[j][1] - off[1]];
            }
        }
        k += c;
    }
    (left, right)
}

/// numpy's mean of a float column (pairwise sum / n).
fn np_mean(v: &[f64]) -> f64 {
    np_sum(v) / (v.len() as f64)
}

/// np.median (the two middle values' mean for an even count).
fn np_median(v: &[f64]) -> f64 {
    let n = v.len();
    if n == 0 {
        return f64::NAN;
    }
    if v.iter().any(|x| x.is_nan()) {
        return f64::NAN;
    }
    let mut s = v.to_vec();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap());
    if n % 2 == 1 { s[n / 2] } else { (s[n / 2 - 1] + s[n / 2]) / 2.0 }
}

/// Bool mean (np.mean of a bool slice).
fn bmean(v: &[bool]) -> f64 {
    (v.iter().filter(|&&b| b).count() as f64) / (v.len() as f64)
}

/// The kernels' scalar constants read at each call (roads._spec_prm: tests change some).
struct Prm {
    min_part: f64,
    urban_sample: f64,
    urban_share: f64,
    urban_beside: f64,
    urban_beside_share: f64,
    urban_both_share: f64,
    dual_gap0: f64,
    dual_gap1: f64,
    dual_share: f64,
    dual_cos: f64,
    mountain_on: bool,
    mountain_switchbacks: f64,
    mountain_span: f64,
    mountain_turn: f64,
    mountain_road: f64,
    mountain_max_lanes: f64,
    se_town_kmh: f64,
    edge_m: f64,
}

impl Prm {
    fn new(p: &HashMap<String, f64>) -> PyResult<Prm> {
        let g = |k: &str| prm_get(p, k);
        Ok(Prm {
            min_part: g("MIN_PART")?, urban_sample: g("URBAN_SAMPLE_M")?, urban_share: g("URBAN_SHARE")?,
            urban_beside: g("URBAN_BESIDE_M")?, urban_beside_share: g("URBAN_BESIDE_SHARE")?,
            urban_both_share: g("URBAN_BOTH_SHARE")?, dual_gap0: g("DUAL_GAP_M0")?, dual_gap1: g("DUAL_GAP_M1")?,
            dual_share: g("DUAL_SHARE")?, dual_cos: g("DUAL_COS")?, mountain_on: g("MOUNTAIN_ON")? != 0.0,
            mountain_switchbacks: g("MOUNTAIN_SWITCHBACKS")?, mountain_span: g("MOUNTAIN_SPAN_M")?,
            mountain_turn: g("MOUNTAIN_TURN_DEG")?, mountain_road: g("MOUNTAIN_ROAD_M")?,
            mountain_max_lanes: g("MOUNTAIN_MAX_LANES")?, se_town_kmh: g("SE_TOWN_MAX_KMH")?, edge_m: g("EDGE_M")?,
        })
    }
}

/// roads.switchbacks of lon/lat points.
fn switchbacks(ll: &[P], span: f64, turn: f64) -> usize {
    if ll.len() < 3 {
        return 0;
    }
    let lat: Vec<f64> = ll.iter().map(|p| p[1]).collect();
    let kx = 111320.0 * (np_mean(&lat)).to_radians().cos();
    let mut flat = Vec::with_capacity(ll.len() * 2);
    for p in ll {
        flat.push((p[0] - ll[0][0]) * kx);
        flat.push((p[1] - ll[0][1]) * 111320.0);
    }
    let xy = ArrayView2::from_shape((ll.len(), 2), &flat).unwrap();
    let m = tight_bends_core(xy, span, turn);
    let mut c = if m[0] { 1 } else { 0 };
    for k in 1..m.len() {
        if m[k] && !m[k - 1] {
            c += 1;
        }
    }
    c
}

/// The Swedish reading (roads._swedish_specs) of the specs in place.
fn swedish(l: &Layer, rules: &RoadRules, prm: &Prm, out: &mut [Option<Sp>], widths_only: bool) {
    if l.n == 0 || !l.sweden.iter().any(|&b| b) {
        return;
    }
    for j in 0..l.n {
        if !l.sweden[j] {
            continue;
        }
        let Some(sp) = out[j].as_mut() else { continue };
        let mapped = sp.link || l.mapped_w(j);
        let urban = sp.urban && !(sp.kmh.unwrap_or(0.0) >= prm.se_town_kmh);
        let (mut kind, mut surf) = (sp.kind, sp.surf);
        if !widths_only {
            if surf < 0 && l.se_tag[j] {
                kind = l.se_kind[j];
                surf = l.se_tok[j];
            } else if surf < 0 && !urban && rules.has(sp.cls, F_SEG) && sp.lanes.is_none_or(|v| v == 0) && !mapped
                && !l.aisle[j] {
                kind = GRAVEL;
            }
        }
        if !mapped && !rules.has(sp.cls, F_MOTORWAY) && !rules.has(sp.cls, F_TRACK) {
            let (w, _) = rules.cw(sp.cls, !rules.has(sp.cls, F_SENS), false, None, sp.oneway,
                                  sp.lanes.map(|v| v as f64), false);
            sp.width = w;
            sp.w32 = false;
        }
        sp.urban = urban;
        sp.kind = kind;
        sp.surf = surf;
        sp.parking = false;
    }
}

/// _dual_width: a divided road carriageway's width (keep: w itself, its type kept).
fn dual_width(l: &Layer, rules: &RoadRules, sp: &Sp, w: f64, gap: f64, i: usize) -> (f64, bool) {
    if l.mapped_w(i) {
        return (w, true);
    }
    let mut w = w;
    if sp.urban && sp.parking && gap <= rules.urban_median_gap {
        // np.clip(gap - URBAN_MEDIAN_M, w, URBAN_DUAL_MAX_W)
        let x = gap - rules.urban_median;
        let a = if x.is_nan() { x } else if x > w { x } else { w };
        w = if a.is_nan() { a } else if a < rules.urban_dual_max_w { a } else { rules.urban_dual_max_w };
    }
    (rules.cap(gap, w, None, None), false)
}

/// _centre_gap: the median distance from a's points to b where b is beside it.
fn centre_gap(a: ArrayView2<f64>, b: ArrayView2<f64>, gap1: f64) -> f64 {
    let d = polyline_dist_core(b, a);
    let beside: Vec<f64> = d.iter().copied().filter(|&v| v <= gap1).collect();
    if beside.is_empty() { np_median(&d) } else { np_median(&beside) }
}

/// dual_carriageways on specs (in place): returns, per feature, what changed: 0 nothing, 1 the width (a nameless
/// pair capped), 2 the median and width (a one-way carriageway), 3 one-way, dir, width and median.
fn dual(l: &Layer, rules: &RoadRules, prm: &Prm, specs: &mut [Option<Sp>], european: bool, left_hand: bool)
        -> Vec<u8> {
    let n = l.n;
    let mut changed = vec![0u8; n];
    let Some(names) = l.name.as_ref() else { return changed };
    let mut order: Vec<(i64, usize)> = Vec::new();
    let mut groups: HashMap<(i64, usize), Vec<usize>> = HashMap::new();
    for i in 0..n {
        let Some(sp) = specs[i].as_ref() else { continue };
        if !rules.has(sp.cls, F_DUAL) {
            continue;
        }
        let key = if names[i] >= 0 {
            (names[i], sp.cls)
        } else if sp.oneway && !sp.link && !rules.has(sp.cls, F_MOTORWAY) {
            (-1, sp.cls)
        } else {
            continue;
        };
        groups.entry(key).or_insert_with(|| {
            order.push(key);
            Vec::new()
        }).push(i);
    }
    let input: Vec<Option<Sp>> = specs.to_vec();
    for key in order {
        let idx = &groups[&key];
        if idx.len() < 2 {
            continue;
        }
        // the lines in metres from the group's first point, at the group's mean latitude
        let lines: Vec<(usize, usize)> = idx.iter().map(|&i| l.all_pts(i)).collect();
        let lat: Vec<f64> = lines.iter().flat_map(|&(a, b)| (a..b).map(|k| l.ll[[k, 1]])).collect();
        let lat0 = np_mean(&lat);
        let m = [111320.0 * lat0.to_radians().cos(), 110540.0];
        let r0 = l.pt(lines[0].0);
        let xy: Vec<Vec<f64>> = lines.iter().map(|&(a, b)| {
            let mut v = Vec::with_capacity(2 * (b - a));
            for k in a..b {
                v.push((l.ll[[k, 0]] - r0[0]) * m[0]);
                v.push((l.ll[[k, 1]] - r0[1]) * m[1]);
            }
            v
        }).collect();
        let view = |t: usize| ArrayView2::from_shape((xy[t].len() / 2, 2), &xy[t][..]).unwrap();
        for (ti, &i) in idx.iter().enumerate() {
            let si = input[i].as_ref().unwrap();
            let mut best: Option<(f64, usize, f64)> = None;      // (side, j, gap)
            for (tj, &j) in idx.iter().enumerate() {
                if i == j {
                    continue;
                }
                let (share, side) = dual_side_core(view(ti), view(tj), prm.dual_gap0, prm.dual_gap1, prm.dual_cos);
                if share >= prm.dual_share && side.abs() > 0.5 {
                    let sj = input[j].as_ref().unwrap();
                    if european && rules.has(si.cls, F_TERTIARY) && !(si.oneway && sj.oneway) && l.eu_country[i] {
                        continue;
                    }
                    let gap = centre_gap(view(ti), view(tj), prm.dual_gap1);
                    if best.is_none() || gap < best.unwrap().2 {
                        best = Some((side, j, gap));
                    }
                }
            }
            let Some((side, bj, gap)) = best else { continue };
            let sp = specs[i].clone().unwrap();
            if key.0 < 0 {
                let other = input[bj].as_ref().unwrap();
                if (sp.width + other.width) / 2.0 + rules.dual_min_median > gap {
                    let s = specs[i].as_mut().unwrap();
                    s.width = rules.cap(gap, sp.width, sp.lanes.map(|v| v as f64), other.lanes.map(|v| v as f64));
                    s.w32 = false;
                    changed[i] = 1;
                }
                continue;
            }
            if sp.oneway {
                let (w, keep) = dual_width(l, rules, &sp, sp.width, gap, i);
                let s = specs[i].as_mut().unwrap();
                s.median = Some(gap);
                if !keep {
                    s.width = w;
                    s.w32 = false;
                }
                changed[i] = 2;
                continue;
            }
            if l.gen[i] > 0 {
                continue;
            }
            let d = (if side > 0.0 { 1 } else { -1 }) * (if left_hand { -1 } else { 1 });
            let (w, w32) = if !sp.parking || l.mapped_w(i) {
                (sp.width, sp.w32)
            } else {
                (rules.cw(sp.cls, l.built(sp.kind), sp.urban, None, true, sp.lanes.map(|v| v as f64), sp.link).0, false)
            };
            let (w2, keep) = dual_width(l, rules, &sp, w, gap, i);
            let s = specs[i].as_mut().unwrap();
            s.oneway = true;
            s.dir = d;
            s.width = w2;
            s.w32 = keep && w32;
            s.median = Some(gap);
            changed[i] = 3;
        }
    }
    changed
}

/// The spec columns handed back (roads._specs_of makes the Spec tuples).
fn spec_cols<'py>(py: Python<'py>, d: &Bound<'py, PyDict>, specs: &[Option<Sp>], names: &[String]) -> PyResult<()> {
    let n = specs.len();
    macro_rules! put {
        ($k:expr, $t:ty, $f:expr, $dflt:expr) => {{
            let v: Vec<$t> = specs.iter().map(|s| s.as_ref().map($f).unwrap_or($dflt)).collect();
            d.set_item($k, v.into_pyarray(py))?;
        }};
    }
    put!("present", bool, |_s: &Sp| true, false);
    put!("width", f64, |s: &Sp| s.width, f64::NAN);
    put!("w32", bool, |s: &Sp| s.w32, false);
    put!("kind", i8, |s: &Sp| s.kind, -1);
    put!("urban", bool, |s: &Sp| s.urban, false);
    put!("cls", i32, |s: &Sp| s.cls as i32, -1);
    put!("oneway", bool, |s: &Sp| s.oneway, false);
    put!("dir", i64, |s: &Sp| s.dir, 0);
    put!("kmh", f64, |s: &Sp| s.kmh.unwrap_or(f64::NAN), f64::NAN);
    put!("gap", f64, |s: &Sp| s.gap.unwrap_or(f64::NAN), f64::NAN);
    put!("median", f64, |s: &Sp| s.median.unwrap_or(f64::NAN), f64::NAN);
    put!("lanes", i64, |s: &Sp| s.lanes.unwrap_or(-1), -1);
    put!("surf", i32, |s: &Sp| s.surf, -1);
    put!("link", bool, |s: &Sp| s.link, false);
    put!("sides", i8, |s: &Sp| s.sides, -1);
    put!("parking", bool, |s: &Sp| s.parking, false);
    put!("mountain", bool, |s: &Sp| s.mountain, false);
    let _ = (n, names);
    Ok(())
}

/// The base spec of a road (no level ranges, rural: _bridge_specs' / _structure_width's), with free.
fn base_bridge(l: &Layer, rules: &RoadRules, i: usize, lanes: Option<i64>) -> (Sp, bool) {
    let c = l.cls[i] as usize;
    let mut rule_w = l.rule_w(i);
    let kind = l.skind[i];
    let ow = l.has_ow[i];
    let link = l.link[i];
    if rule_w.is_none() && link {
        rule_w = Some(rules.link_width(lanes.filter(|&v| v != 0).unwrap_or(if ow { 1 } else { 2 })));
    }
    let (w, _) = rules.cw(c, l.built(kind), false, rule_w, ow, lanes.map(|v| v as f64), link);
    (Sp { width: w, w32: false, kind, urban: false, cls: c, oneway: ow, dir: l.dir(i), kmh: None, gap: None,
          median: None, lanes, surf: l.stok[i], link, sides: -1, parking: true, mountain: false },
     ow || rule_w.is_some())
}

type Geom<'py> = (PyReadonlyArray2<'py, f64>, PyReadonlyArray1<'py, i64>, PyReadonlyArray1<'py, i64>,
                  PyReadonlyArray1<'py, i64>);

fn built_of(b: &[bool]) -> PyResult<[bool; 4]> {
    if b.len() != 4 {
        return Err(PyValueError::new_err("spec kernels: a built flag per kind"));
    }
    Ok([b[0], b[1], b[2], b[3]])
}

#[pymethods]
impl RoadRules {
    /// names: STYLE's classes in order; tab (len(names), 12): width, oneway width, lane width, urban arterial, edge
    /// strip (NaN: none), then 0 / 1 in HIGHWAYS, PARKING_CLASSES, DUAL_CLASSES, MOUNTAIN_CLASSES, REGIONAL_CLASSES,
    /// SE_GRAVEL_CLASSES, SE_NO_STRIPS; other_w: STYLE["unknown"]'s width; prm: the scalars by name; link_m: LINK_M.
    #[new]
    fn new(names: Vec<String>, tab: PyReadonlyArray2<'_, f64>, other_w: f64, prm: HashMap<String, f64>,
           link_m: Vec<(i64, f64)>) -> PyResult<RoadRules> {
        let t = tab.as_array();
        if t.nrows() != names.len() || t.ncols() != 12 {
            return Err(PyValueError::new_err("RoadRules: a row of 12 per class"));
        }
        let mut rows = Vec::with_capacity(names.len());
        for (c, nm) in names.iter().enumerate() {
            let b = |k: usize| t[[c, k]] != 0.0;
            let mut fl = 0u32;
            for (k, f) in [(5, F_HW), (6, F_PARK), (7, F_DUAL), (8, F_MTN), (9, F_REG), (10, F_SEG), (11, F_SENS)] {
                if b(k) {
                    fl |= f;
                }
            }
            fl |= match nm.as_str() {
                "motorway" => F_MOTORWAY | F_RDIV,
                "trunk" => F_TRUNK | F_RCAND | F_RDIV,
                "primary" => F_PRIMARY | F_RCAND | F_RDIV,
                "secondary" => F_RCAND,
                "tertiary" => F_TERTIARY | F_RCAND,
                "track" => F_TRACK,
                "service" => F_SERVICE,
                _ => 0,
            };
            rows.push(ClassRow { w: t[[c, 0]], ow: t[[c, 1]], lw: t[[c, 2]], ua: t[[c, 3]], es: t[[c, 4]], flags: fl });
        }
        let g = |k: &str| prm_get(&prm, k);
        Ok(RoadRules {
            index: names.iter().enumerate().map(|(k, v)| (v.clone(), k)).collect(), names, rows,
            other: ClassRow { w: other_w, ow: f64::NAN, lw: f64::NAN, ua: f64::NAN, es: f64::NAN, flags: 0 },
            lane_w_default: g("LANE_W_DEFAULT")?, link_shoulder: g("LINK_SHOULDER_M")?, link_lane: g("LINK_LANE_M")?,
            link_m, shoulder: g("SHOULDER_M")?, shoulder_wide: g("SHOULDER_WIDE_M")?,
            shoulder_wide_lanes: g("SHOULDER_WIDE_LANES")?, parking: g("PARKING_M")?,
            motorway_rural: g("MOTORWAY_RURAL_M")?, edge_strip: g("EDGE_STRIP_M")?, ring_lane: g("RING_LANE_M")?,
            ring_extra_lane: g("RING_EXTRA_LANE_M")?, ring_small_r: g("RING_SMALL_R_M")?,
            ring_small_per: g("RING_SMALL_PER_M")?, ring_small_max: g("RING_SMALL_MAX_M")?,
            dual_min_median: g("DUAL_MIN_MEDIAN_M")?, dual_min_w: g("DUAL_MIN_W")?, urban_median: g("URBAN_MEDIAN_M")?,
            urban_median_gap: g("URBAN_MEDIAN_GAP_M")?, urban_dual_max_w: g("URBAN_DUAL_MAX_W")?,
            ramp_deg: g("RAMP_JOIN_DEG")?,
        })
    }

    /// roads.carriageway: (width, whether it is rule_width itself).
    #[pyo3(signature = (cls, built, urban, rule_width, oneway, lanes, link))]
    #[allow(clippy::too_many_arguments)]
    fn carriageway(&self, cls: &str, built: bool, urban: bool, rule_width: Option<f64>, oneway: bool,
                   lanes: Option<f64>, link: bool) -> (f64, bool) {
        self.cw(self.class_of(cls), built, urban, rule_width, oneway, lanes, link)
    }

    /// roads.ring_width.
    #[pyo3(signature = (lanes, radius))]
    fn ring_width(&self, lanes: Option<f64>, radius: Option<f64>) -> f64 {
        self.ring_w(lanes, radius)
    }

    /// roads.dual_cap.
    #[pyo3(signature = (gap, w, lanes, other))]
    fn dual_cap(&self, gap: f64, w: f64, lanes: Option<f64>, other: Option<f64>) -> f64 {
        self.cap(gap, w, lanes, other)
    }

    /// roads.switchbacks of a lon/lat polyline (k, 2).
    fn switchbacks(&self, ll: PyReadonlyArray2<'_, f64>, span: f64, turn: f64) -> usize {
        let a = ll.as_array();
        let pts: Vec<P> = (0..a.nrows()).map(|k| [a[[k, 0]], a[[k, 1]]]).collect();
        switchbacks(&pts, span, turn)
    }

    /// roads._ramp_junction of a layer (cols: roads._spec_inputs').
    fn ramp_junction<'py>(&self, py: Python<'py>, geo: Geom<'py>, cols: Bound<'py, PyDict>, built: Vec<bool>)
                          -> PyResult<Bound<'py, PyArray1<bool>>> {
        let l = Layer::new(geo.0.as_array(), &geo.1, &geo.2, &geo.3, &cols, built_of(&built)?)?;
        Ok(ramp_junction(&l, self).into_pyarray(py))
    }

    /// roads._road_specs up to the opt-in French / European passes, for a layer: geo (lonlat, coord_off, ring_off,
    /// part_off); cols: roads._spec_inputs; raw ranges (off, (m, 2)) for the off-ground test; flow_fn() -> the lane
    /// flow (asked once, when a road needs it); regional_fn() -> regional_unpaved's classes (likewise); urban_fn(lat,
    /// lon) -> bool (None: rural everywhere); prm: roads._spec_prm. Returns a dict of columns (spec_cols, "towns",
    /// "frontage" (n, 2), NaN where not asked).
    #[allow(clippy::too_many_arguments)]
    #[pyo3(signature = (geo, cols, built, raw_off, raw_rng, flow_fn, regional_fn, urban_fn, prm, european, left_hand))]
    fn road_specs<'py>(&self, py: Python<'py>, geo: Geom<'py>, cols: Bound<'py, PyDict>, built: Vec<bool>,
                       raw_off: PyReadonlyArray1<'py, i64>, raw_rng: PyReadonlyArray2<'py, f64>,
                       flow_fn: Bound<'py, PyAny>, regional_fn: Bound<'py, PyAny>, urban_fn: Option<Bound<'py, PyAny>>,
                       prm: HashMap<String, f64>, european: bool, left_hand: bool) -> PyResult<Bound<'py, PyDict>> {
        let prm = Prm::new(&prm)?;
        let l = Layer::new(geo.0.as_array(), &geo.1, &geo.2, &geo.3, &cols, built_of(&built)?)?;
        let n = l.n;
        let (ro, rr) = (raw_off.as_array(), raw_rng.as_array());
        if ro.len() != n + 1 {
            return Err(PyValueError::new_err("road_specs: raw ranges per feature"));
        }
        // the roads drawn on the ground at all
        let mut idx = Vec::new();
        for i in 0..n {
            if !l.elig[i] {
                continue;
            }
            let (a, b) = (ro[i] as usize, ro[i + 1] as usize);
            if b > a {
                let mut s = 0.0f64;
                for r in a..b {
                    s += rr[[r, 1]] - rr[[r, 0]];
                }
                if s > 1.0 - prm.min_part {
                    continue;
                }
            }
            idx.push(i);
        }
        let lanes = lane_counts(py, &l, self, &idx, &flow_fn)?;
        let mut regional = vec![false; n];
        if idx.iter().any(|&i| l.reg_pre[i] && self.has(l.cls[i] as usize, F_REG)) {
            let got: Vec<String> = regional_fn.call0()?.extract()?;
            let set: Vec<usize> = got.iter().map(|c| self.class_of(c)).collect();
            for &i in &idx {
                regional[i] = l.reg_pre[i] && set.contains(&(l.cls[i] as usize));
            }
        }
        let mut out: Vec<Option<Sp>> = vec![None; n];
        struct Ask { i: usize, rule_w: Option<f64>, lanes: Option<i64>, dir: i64, kmh: Option<f64>, gap: Option<f64> }
        let mut asks: Vec<Ask> = Vec::new();
        for (t, &i) in idx.iter().enumerate() {
            let c = l.cls[i] as usize;
            let mut rule_w = l.rule_w(i);
            let ln = lanes[t];
            let mut kind = l.skind[i];
            let ow = l.has_ow[i];
            let link = l.link[i];
            if rule_w.is_none() && link {
                rule_w = Some(self.link_width(ln.filter(|&v| v != 0).unwrap_or(if ow { 1 } else { 2 })));
            }
            let dir = l.dir(i);
            let mut kmh = if l.has_k[i] && l.kv[i] > 0.0 { Some(l.kv[i]) } else { None };
            let mut gap = None;
            if let Some(nc) = l.ncon.as_ref() {
                if nc[i] >= 2 && !l.lens[i].is_nan() {
                    gap = Some(l.lens[i] / ((nc[i] - 1) as f64));
                }
            }
            if kmh.is_none() && l.osm_ms[i].is_finite() {
                kmh = Some(l.osm_ms[i]);
            }
            if urban_fn.is_some() && l.built(kind) {
                let (a, b) = l.all_pts(i);
                if b > a {
                    asks.push(Ask { i, rule_w, lanes: ln, dir, kmh, gap });
                    continue;
                }
            }
            if regional[i] {
                kind = GRAVEL;
            }
            let (w, _) = self.cw(c, l.built(kind), false, rule_w, ow, ln.map(|v| v as f64), link);
            out[i] = Some(Sp { width: w, w32: false, kind, urban: false, cls: c, oneway: ow, dir, kmh, gap,
                               median: None, lanes: ln, surf: l.stok[i], link, sides: -1, parking: true,
                               mountain: false });
        }
        let mut frontage = vec![[f64::NAN; 2]; n];
        let mut developed_both: HashMap<usize, bool> = HashMap::new();
        if !asks.is_empty() {
            let urban_fn = urban_fn.unwrap();
            let lines: Vec<(usize, usize)> = asks.iter().map(|q| l.all_pts(q.i)).collect();
            let (pts, counts) = py.detach(|| {
                let mut pts: Vec<P> = Vec::new();
                let mut counts = Vec::with_capacity(lines.len());
                for &(a, b) in &lines {
                    let ll: Vec<P> = (a..b).map(|k| l.pt(k)).collect();
                    let s = urban_samples(&ll, prm.urban_sample);
                    counts.push(s.len());
                    pts.extend(s);
                }
                let (lf, rt) = beside(&pts, &counts, prm.urban_beside);
                let mut all = pts.clone();
                all.extend(lf);
                all.extend(rt);
                if european {
                    let (el, er) = beside(&pts, &counts, prm.edge_m);
                    all.extend(el);
                    all.extend(er);
                }
                (all, counts)
            });
            let lat: Vec<f64> = pts.iter().map(|p| p[1]).collect();
            let lon: Vec<f64> = pts.iter().map(|p| p[0]).collect();
            let got = urban_fn.call1((lat.into_pyarray(py), lon.into_pyarray(py)))?;
            let ua: PyReadonlyArray1<bool> = got.extract()?;
            let u_all = ua.as_array().to_vec();
            let m: usize = counts.iter().sum();
            if u_all.len() != pts.len() {
                return Err(PyValueError::new_err("road_specs: urban_fn: one value per point"));
            }
            let (u, ul, ur) = (&u_all[..m], &u_all[m..2 * m], &u_all[2 * m..3 * m]);
            let by: Vec<bool> = ul.iter().zip(ur).map(|(a, b)| *a || *b).collect();
            let both: Vec<bool> = ul.iter().zip(ur).map(|(a, b)| *a && *b).collect();
            let mut k = 0;
            for (q, &count) in asks.iter().zip(&counts) {
                let i = q.i;
                let c = l.cls[i] as usize;
                let sl = k..k + count;
                if european {
                    frontage[i] = [bmean(&u_all[3 * m + sl.start..3 * m + sl.end]),
                                   bmean(&u_all[4 * m + sl.start..4 * m + sl.end])];
                }
                let mb = bmean(&both[sl.clone()]);
                developed_both.insert(i, mb >= prm.urban_both_share);
                let urban = bmean(&u[sl.clone()]) >= prm.urban_share && bmean(&by[sl.clone()]) >= prm.urban_beside_share
                    && (!self.has(c, F_MOTORWAY | F_TRUNK) || mb >= prm.urban_both_share);
                k += count;
                let mut kind = l.skind[i];
                if regional[i] && !urban {
                    kind = GRAVEL;
                }
                let ow = l.has_ow[i];
                let (w, _) = self.cw(c, l.built(kind), urban, q.rule_w, ow, q.lanes.map(|v| v as f64), l.link[i]);
                out[i] = Some(Sp { width: w, w32: false, kind, urban, cls: c, oneway: ow, dir: q.dir, kmh: q.kmh,
                                   gap: q.gap, median: None, lanes: q.lanes, surf: l.stok[i], link: l.link[i],
                                   sides: -1, parking: true, mountain: false });
            }
        }
        let towns: Vec<bool> = out.iter().map(|s| s.as_ref().is_some_and(|s| s.urban)).collect();
        let out = py.detach(|| {
            let mut out = out;
            swedish(&l, self, &prm, &mut out, false);
            // _rings
            for i in 0..n {
                let Some(sp) = out[i].as_mut() else { continue };
                if !l.built(sp.kind) || !l.ring[i] {
                    continue;
                }
                let mapped = if l.has_w[i] && 1.0 <= l.wv[i] && l.wv[i] <= 40.0 { Some(l.wv[i]) }
                             else if l.osm_w[i].is_finite() { Some(l.osm_w[i]) } else { None };
                let r = if l.radius[i].is_nan() { None } else { Some(l.radius[i]) };
                sp.width = pmax(mapped.unwrap_or(0.0), self.ring_w(sp.lanes.map(|v| v as f64), r));
                sp.w32 = false;
                sp.urban = false;
                sp.median = None;
            }
            dual(&l, self, &prm, &mut out, european, left_hand);
            // a primary's divided carriageways built up on one side only: rural (after the pairing)
            for j in 0..n {
                let Some(sp) = out[j].as_ref() else { continue };
                if !(self.has(sp.cls, F_PRIMARY) && med_truthy(sp.median) && sp.urban
                     && !developed_both.get(&j).copied().unwrap_or(true)) {
                    continue;
                }
                let (raw, is32) = l.raw_w(j);
                let (w, own) = self.cw(sp.cls, l.built(sp.kind), false, raw, sp.oneway, sp.lanes.map(|v| v as f64),
                                       sp.link);
                let mut s2 = sp.clone();
                s2.urban = false;
                let (w2, keep) = dual_width(&l, self, &s2, w, sp.median.unwrap(), j);
                let sp = out[j].as_mut().unwrap();
                sp.urban = false;
                sp.width = w2;
                sp.w32 = keep && own && is32;
            }
            rural_connectors(&l, self, &prm, &mut out);
            // _osm_sidewalks
            for i in 0..n {
                if let Some(sp) = out[i].as_mut() {
                    if l.built(sp.kind) && l.sides[i] >= 0 {
                        sp.sides = l.sides[i];
                    }
                }
            }
            if prm.mountain_on {
                for i in 0..n {
                    let Some(sp) = out[i].as_mut() else { continue };
                    if sp.urban || !self.has(sp.cls, F_MTN) || sp.kind != PAVED || sp.oneway || med_truthy(sp.median)
                        || (sp.lanes.unwrap_or(0) as f64) > prm.mountain_max_lanes {
                        continue;
                    }
                    if l.mapped_w(i) {
                        continue;
                    }
                    let a = l.co[l.ro[l.po[i]]];
                    let b = l.co[l.ro[l.po[i] + 1]];
                    if b < a + 3 {
                        continue;
                    }
                    let ll: Vec<P> = (a..b).map(|k| l.pt(k)).collect();
                    if (switchbacks(&ll, prm.mountain_span, prm.mountain_turn) as f64) < prm.mountain_switchbacks {
                        continue;
                    }
                    if prm.mountain_road < sp.width {
                        sp.width = prm.mountain_road;
                        sp.w32 = false;
                    }
                    sp.mountain = true;
                }
            }
            out
        });
        let d = PyDict::new(py);
        spec_cols(py, &d, &out, &self.names)?;
        d.set_item("towns", towns.into_pyarray(py))?;
        let fl: Vec<f64> = frontage.iter().flat_map(|v| v.iter().copied()).collect();
        d.set_item("frontage", numpy::ndarray::Array2::from_shape_vec((n, 2), fl).unwrap().into_pyarray(py))?;
        Ok(d)
    }

    /// roads._bridge_specs' specs for a layer: want (n,) the features with a range of that kind; gmed / gdir: the
    /// ground specs' median (NaN: none or 0) and dir.
    #[allow(clippy::too_many_arguments)]
    fn bridge_specs<'py>(&self, py: Python<'py>, geo: Geom<'py>, cols: Bound<'py, PyDict>, built: Vec<bool>,
                         want: PyReadonlyArray1<'py, bool>, gmed: PyReadonlyArray1<'py, f64>,
                         gdir: PyReadonlyArray1<'py, i64>, flow_fn: Bound<'py, PyAny>, prm: HashMap<String, f64>)
                         -> PyResult<Bound<'py, PyDict>> {
        let prm = Prm::new(&prm)?;
        let l = Layer::new(geo.0.as_array(), &geo.1, &geo.2, &geo.3, &cols, built_of(&built)?)?;
        let n = l.n;
        let (want, gmed, gdir) = (want.as_array(), gmed.as_array(), gdir.as_array());
        if want.len() != n || gmed.len() != n || gdir.len() != n {
            return Err(PyValueError::new_err("bridge_specs: a value per feature"));
        }
        let idx: Vec<usize> = (0..n).filter(|&i| l.elig[i] && want[i]).collect();
        let lanes = lane_counts(py, &l, self, &idx, &flow_fn)?;
        let mut out: Vec<Option<Sp>> = vec![None; n];
        let mut free = vec![false; n];
        for (t, &i) in idx.iter().enumerate() {
            let (sp, f) = base_bridge(&l, self, i, lanes[t]);
            out[i] = Some(sp);
            free[i] = f;
        }
        swedish(&l, self, &prm, &mut out, true);
        for i in 0..n {
            let Some(sp) = out[i].as_mut() else { continue };
            let gm = gmed[i];
            if gm.is_nan() {
                continue;
            }
            let w = if free[i] { sp.width } else {
                self.cw(sp.cls, l.built(sp.kind), false, None, true, sp.lanes.map(|v| v as f64), sp.link).0
            };
            sp.oneway = true;
            sp.dir = gdir[i];
            sp.median = Some(gm);
            sp.width = self.cap(gm, w, None, None);
            sp.w32 = false;
        }
        let d = PyDict::new(py);
        spec_cols(py, &d, &out, &self.names)?;
        Ok(d)
    }

    /// roads._structure_width of features idx (NaN: None).
    fn structure_widths<'py>(&self, py: Python<'py>, geo: Geom<'py>, cols: Bound<'py, PyDict>, built: Vec<bool>,
                             idx: PyReadonlyArray1<'py, i64>, flow_fn: Bound<'py, PyAny>)
                             -> PyResult<Bound<'py, PyArray1<f64>>> {
        let l = Layer::new(geo.0.as_array(), &geo.1, &geo.2, &geo.3, &cols, built_of(&built)?)?;
        let idx: Vec<usize> = idx.as_array().iter().map(|&v| v as usize).collect();
        if idx.iter().any(|&i| i >= l.n) {
            return Err(PyValueError::new_err("structure_widths: a feature out of range"));
        }
        let elig: Vec<usize> = idx.iter().copied().filter(|&i| l.elig[i]).collect();
        let lanes = lane_counts(py, &l, self, &elig, &flow_fn)?;
        let mut w = HashMap::new();
        for (t, &i) in elig.iter().enumerate() {
            w.insert(i, base_bridge(&l, self, i, lanes[t]).0.width);
        }
        Ok(idx.iter().map(|i| w.get(i).copied().unwrap_or(f64::NAN)).collect::<Vec<f64>>().into_pyarray(py))
    }

    /// dual_carriageways on given specs (the Python's public entry: traffic.py, tests): spec columns in (present,
    /// width, kind, urban, cls, oneway, lanes, link, parking, median: as spec_cols'), the changed ones out: (changed,
    /// oneway, dir, width (NaN: kept), median).
    #[allow(clippy::too_many_arguments)]
    fn dual_carriageways<'py>(&self, py: Python<'py>, geo: Geom<'py>, cols: Bound<'py, PyDict>, built: Vec<bool>,
                              specs: Bound<'py, PyDict>, prm: HashMap<String, f64>, european: bool, left_hand: bool)
                              -> PyResult<Bound<'py, PyDict>> {
        let prm = Prm::new(&prm)?;
        let l = Layer::new(geo.0.as_array(), &geo.1, &geo.2, &geo.3, &cols, built_of(&built)?)?;
        let n = l.n;
        let pres: Vec<bool> = col(&specs, "present", n)?;
        let width: Vec<f64> = col(&specs, "width", n)?;
        let kind: Vec<i8> = col(&specs, "kind", n)?;
        let urban: Vec<bool> = col(&specs, "urban", n)?;
        let cls: Vec<i32> = col(&specs, "cls", n)?;
        let oneway: Vec<bool> = col(&specs, "oneway", n)?;
        let lanes: Vec<i64> = col(&specs, "lanes", n)?;
        let link: Vec<bool> = col(&specs, "link", n)?;
        let parking: Vec<bool> = col(&specs, "parking", n)?;
        let median: Vec<f64> = col(&specs, "median", n)?;
        let dir: Vec<i64> = col(&specs, "dir", n)?;
        let mut sp: Vec<Option<Sp>> = (0..n).map(|i| if !pres[i] { None } else {
            Some(Sp { width: width[i], w32: false, kind: kind[i], urban: urban[i],
                      cls: if cls[i] < 0 { self.rows.len() } else { cls[i] as usize }, oneway: oneway[i], dir: dir[i],
                      kmh: None, gap: None, median: if median[i].is_nan() { None } else { Some(median[i]) },
                      lanes: if lanes[i] < 0 { None } else { Some(lanes[i]) }, surf: -1, link: link[i], sides: -1,
                      parking: parking[i], mountain: false })
        }).collect();
        // (w32 marks the widths dual kept: the caller keeps its own object there)
        for s in sp.iter_mut().flatten() {
            s.w32 = true;
        }
        let changed = py.detach(|| dual(&l, self, &prm, &mut sp, european, left_hand));
        let d = PyDict::new(py);
        d.set_item("changed", changed.into_pyarray(py))?;
        spec_cols(py, &d, &sp, &self.names)?;
        Ok(d)
    }
}

/// _rural_connectors on the specs in place.
fn rural_connectors(l: &Layer, rules: &RoadRules, prm: &Prm, out: &mut [Option<Sp>]) {
    let n = l.n;
    let cand: Vec<usize> = (0..n).filter(|&i| out[i].as_ref().is_some_and(|s| s.link && s.urban
        && rules.has(s.cls, F_RCAND))).collect();
    let rural: Vec<usize> = (0..n).filter(|&i| out[i].as_ref().is_some_and(|s| !s.urban && s.oneway
        && med_truthy(s.median) && rules.has(s.cls, F_RDIV))).collect();
    if cand.is_empty() || rural.len() < 2 {
        return;
    }
    let ends = |i: usize| -> Option<[P; 2]> {
        let (a, b) = l.all_pts(i);
        if b > a { Some([l.pt(a), l.pt(b - 1)]) } else { None }
    };
    let mut points: Vec<P> = Vec::new();
    let mut owners: Vec<usize> = Vec::new();
    for &i in &rural {
        if let Some(e) = ends(i) {
            points.extend(e);
            owners.push(i);
            owners.push(i);
        }
    }
    if points.is_empty() {
        return;
    }
    let lat: Vec<f64> = points.iter().map(|p| p[1]).collect();
    let scale = [111320.0 * np_mean(&lat).to_radians().cos(), 110540.0];
    for &i in &cand {
        let Some(e) = ends(i) else { continue };
        if l.lens[i] > 2.0 * prm.urban_beside {
            continue;
        }
        let mut att: [Vec<usize>; 2] = [Vec::new(), Vec::new()];
        for (j, ej) in e.iter().enumerate() {
            for (p, q) in points.iter().enumerate() {
                let (dx, dy) = ((ej[0] - q[0]) * scale[0], (ej[1] - q[1]) * scale[1]);
                if (dx * dx + dy * dy).sqrt() < 2.5 && !att[j].contains(&owners[p]) {
                    att[j].push(owners[p]);
                }
            }
        }
        if att[0].is_empty() || att[1].is_empty() {
            continue;
        }
        let mut all = att[0].clone();
        for o in &att[1] {
            if !all.contains(o) {
                all.push(*o);
            }
        }
        if all.len() < 2 {
            continue;
        }
        let sp = out[i].as_mut().unwrap();
        let (raw, is32) = l.raw_w(i);
        let (w, own) = rules.cw(sp.cls, l.built(sp.kind), false, raw, sp.oneway, sp.lanes.map(|v| v as f64), sp.link);
        sp.urban = false;
        sp.width = w;
        sp.w32 = own && is32;
    }
}
