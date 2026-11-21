//! railways.parts' feature loop (the full port, 491): a layer's rail features kept in a Rust-owned index per frame
//! (RailIndex: each one's row, id, box, length, drawn class and wear, heritage flag and OSM gauge, its rings in lon / lat
//! and in the frame), and a window's selection in one call (rail_parts_select): the box test, the longest copy of a
//! feature over the cells, the drawn class, the pieces by its level ranges (railways._pieces: short tunnels on the
//! ground, long ones left out or kept with BNE_RAIL_TUNNELS), each piece's ring in the frame, its box and clip test
//! against the window and its STEP_M resampling, sorted by (id, kind, first point). Every node window of railprofile's
//! lattice (~800 a Manhattan visit's 25 road tiles) went through every rail of its cells feature by feature in Python
//! (~4.5 of the rail stack's ~18 CPU-s). The level ranges stay the caller's (roads.level_ranges, asked for each feature
//! taken, in the Python's order: the lidar rule's revisit fault comes with them); a feature's pieces are kept for its
//! last ranges. The Python's numbers: sub_polyline's mean latitude (numpy's pairwise sum), cos and hypot (dm), the
//! frame's geodetic_to_enu (g2e_one; a whole ring's points are the layer's xy_in), np.arange's i * step and np.interp
//! (dm::interp, numpy's algorithm), numpy's min / max (a NaN wins), Python's min / max, its sort's order.
use crate::dm::Dm;
use crate::levels::np_sum;

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Mutex};

use numpy::{IntoPyArray, PyArray2, PyArrayMethods, PyReadonlyArray1, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::exceptions::{PyMemoryError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyString, PyTuple};

pub(crate) type P = [f64; 2];

const GROUND: u8 = 0;
const BRIDGE: u8 = 1;
const TUNNEL: u8 = 2;
const OTHER: u8 = 3;
const KINDS: [&str; 3] = ["ground", "bridge", "tunnel"];
/// raildrawn's class code of "tram" (only its bridges are taken).
const TRAM: i8 = 3;

/// Points of a resampling past which numpy's arange would not allocate (MemoryError; a line of 2e9 m).
const RESAMPLE_MAX: f64 = 1e9;

/// A ring of 2 points or more of a feature: its first coordinate's row in the layer, lon / lat and the same points in
/// the frame (xy_in's).
struct Ring {
    start: usize,
    ll: Vec<P>,
    xy: Vec<P>,
}

/// A rail feature of the layer.
struct Row {
    i: usize,
    fid: Py<PyString>,
    key: Vec<u32>,
    bbox: [f64; 4],
    len_m: f64,
    cls: i8,
    wear: i64,
    heritage: bool,
    gauge: f64,
    rings: Vec<Ring>,
}

#[derive(Clone, Copy)]
enum ResErr {
    Value(&'static str),
    Memory,
}

/// A piece of a feature: its kind, its points in the frame, their box and (once asked) their resampling.
struct Piece {
    kind: u8,
    xy: Vec<P>,
    bbox: [f64; 4],
    res: Option<Result<Arc<Vec<P>>, ResErr>>,
}

/// A feature's pieces for one list of level ranges (each (start, end, kind code)) and BNE_RAIL_TUNNELS.
struct Cached {
    rngs: Vec<(f64, f64, u8)>,
    tunnels: bool,
    pieces: Vec<Piece>,
}

/// The pieces' memo entries kept per feature (the latest first).
const CACHED_MAX: usize = 2;

/// A feature's ground pieces with no level ranges: roads.ground_parts' rings (the objects, held: their ids the key)
/// in the frame.
struct GroundCached {
    ids: Vec<usize>,
    _keep: Vec<Py<PyAny>>,
    pieces: Vec<Piece>,
}

/// A ground_parts ring as enu_xy reads it: a view of the layer's lonlat (its first row, its length) or other points.
enum GRing {
    View(usize, usize, Vec<P>),
    Pts(Vec<P>),
}

/// The layer's lonlat buffer (enu_xy's view test): its address, strides (bytes) and shape.
struct LonLat {
    ptr: usize,
    strides: [isize; 2],
    rows: usize,
}

#[pyclass(module = "earthfast", frozen)]
pub struct RailIndex {
    rows: Vec<Row>,
    origin: [f64; 3],
    rot: [[f64; 3]; 3],
    step: f64,
    min_part: f64,
    min_part_m: f64,
    short_tunnel_m: f64,
    lonlat: LonLat,
    cache: Mutex<Vec<Vec<Cached>>>,
    ground: Mutex<Vec<Option<GroundCached>>>,
}

/// The code points of a str (a lone surrogate too: Python's order of str).
pub(crate) fn code_points(s: &Bound<'_, PyString>) -> PyResult<Vec<u32>> {
    if let Ok(t) = s.to_str() {
        return Ok(t.chars().map(|c| c as u32).collect());
    }
    let b = s.call_method1("encode", ("utf-32-le", "surrogatepass"))?;
    let b: Vec<u8> = b.extract()?;
    Ok(b.as_chunks::<4>().0.iter().map(|c| u32::from_le_bytes(*c)).collect())
}

pub(crate) fn offsets(a: &PyReadonlyArray1<'_, i64>, what: &str) -> PyResult<Vec<usize>> {
    a.as_array().iter().map(|&v| usize::try_from(v)
        .map_err(|_| PyValueError::new_err(format!("RailIndex: negative {what}")))).collect()
}

fn points(a: &PyReadonlyArray2<'_, f64>, what: &str) -> PyResult<Vec<P>> {
    let v = a.as_array();
    if v.nrows() > 0 && v.ncols() < 2 {
        return Err(PyValueError::new_err(format!("RailIndex: {what} must be (N, 2)")));
    }
    Ok((0..v.nrows()).map(|k| [v[[k, 0]], v[[k, 1]]]).collect())
}

#[pymethods]
impl RailIndex {
    /// rows: the layer's rail features (ascending); part_off / ring_off / coord_off / lonlat: the layer's geometry;
    /// xy: its xy_in(frame); boxes, cls, wear, len_m, fids, heritage, gauge: per rail feature (gauge NaN: none);
    /// origin / rot: the frame's; step, min_part, min_part_m, short_tunnel_m: railways.STEP_M, roads.MIN_PART,
    /// railways.MIN_PART_M, railways.SHORT_TUNNEL_M.
    #[new]
    #[pyo3(signature = (rows, part_off, ring_off, coord_off, lonlat, xy, boxes, cls, wear, len_m, fids, heritage, gauge,
                        origin, rot, step, min_part, min_part_m, short_tunnel_m))]
    #[allow(clippy::too_many_arguments)]
    fn new(rows: PyReadonlyArray1<'_, i64>, part_off: PyReadonlyArray1<'_, i64>, ring_off: PyReadonlyArray1<'_, i64>,
           coord_off: PyReadonlyArray1<'_, i64>, lonlat: Bound<'_, PyArray2<f64>>, xy: PyReadonlyArray2<'_, f64>,
           boxes: PyReadonlyArray2<'_, f64>, cls: PyReadonlyArray1<'_, i64>, wear: PyReadonlyArray1<'_, i64>,
           len_m: PyReadonlyArray1<'_, f64>, fids: Vec<Bound<'_, PyString>>, heritage: Vec<bool>,
           gauge: PyReadonlyArray1<'_, f64>, origin: PyReadonlyArray1<'_, f64>, rot: PyReadonlyArray2<'_, f64>,
           step: f64, min_part: f64, min_part_m: f64, short_tunnel_m: f64) -> PyResult<Self> {
        let rows = offsets(&rows, "row")?;
        let (po, ro, co) = (offsets(&part_off, "part_off")?, offsets(&ring_off, "ring_off")?,
                            offsets(&coord_off, "coord_off")?);
        let lonlat_ro = lonlat.readonly();
        let (ll, xyv) = (points(&lonlat_ro, "lonlat")?, points(&xy, "xy")?);
        let st = lonlat.strides();
        let lonlat = LonLat { ptr: lonlat.data() as usize, strides: [st[0], st.get(1).copied().unwrap_or(0)],
                              rows: ll.len() };
        if xyv.len() != ll.len() {
            return Err(PyValueError::new_err("RailIndex: xy needs a point per lonlat point"));
        }
        let n = rows.len();
        let (bx, cl, we, lm, ga) = (boxes.as_array(), cls.as_array(), wear.as_array(), len_m.as_array(), gauge.as_array());
        if bx.nrows() != n || (n > 0 && bx.ncols() != 4) || cl.len() != n || we.len() != n || lm.len() != n
            || fids.len() != n || heritage.len() != n || ga.len() != n {
            return Err(PyValueError::new_err("RailIndex: boxes (k, 4), cls, wear, len_m, fids, heritage, gauge per row"));
        }
        let (o, r) = (origin.as_array(), rot.as_array());
        if o.len() < 3 || r.nrows() < 3 || r.ncols() < 3 {
            return Err(PyValueError::new_err("RailIndex: a frame: origin (3,), rot (3, 3)"));
        }
        let bad = || PyValueError::new_err("RailIndex: offsets out of range");
        let mut out = Vec::with_capacity(n);
        for (k, &i) in rows.iter().enumerate() {
            if i + 1 >= po.len() || po[i] > po[i + 1] {
                return Err(bad());
            }
            let mut rings = Vec::new();
            for p in po[i]..po[i + 1] {
                if p + 1 >= ro.len() || ro[p] > ro[p + 1] {
                    return Err(bad());
                }
                for g in ro[p]..ro[p + 1] {
                    if g + 1 >= co.len() || co[g] > co[g + 1] || co[g + 1] > ll.len() {
                        return Err(bad());
                    }
                    let (a, b) = (co[g], co[g + 1]);
                    if b - a >= 2 {
                        rings.push(Ring { start: a, ll: ll[a..b].to_vec(), xy: xyv[a..b].to_vec() });
                    }
                }
            }
            let c = cl[k];
            out.push(Row {
                i,
                key: code_points(&fids[k])?,
                fid: fids[k].clone().unbind(),
                bbox: [bx[[k, 0]], bx[[k, 1]], bx[[k, 2]], bx[[k, 3]]],
                len_m: lm[k],
                cls: if (0..=3).contains(&c) { c as i8 } else { -1 },
                wear: we[k],
                heritage: heritage[k],
                gauge: ga[k],
                rings,
            });
        }
        let cache = Mutex::new((0..n).map(|_| Vec::new()).collect());
        let ground = Mutex::new((0..n).map(|_| None).collect());
        Ok(RailIndex {
            rows: out,
            origin: [o[0], o[1], o[2]],
            rot: [[r[[0, 0]], r[[0, 1]], r[[0, 2]]], [r[[1, 0]], r[[1, 1]], r[[1, 2]]],
                  [r[[2, 0]], r[[2, 1]], r[[2, 2]]]],
            step,
            min_part,
            min_part_m,
            short_tunnel_m,
            lonlat,
            cache,
            ground,
        })
    }

    fn __len__(&self) -> usize {
        self.rows.len()
    }
}

/// railways._box_hit
pub(crate) fn box_hit(b: &[f64; 4], n: &[f64; 4]) -> bool {
    !(b[2] < n[0] || b[0] > n[2] || b[3] < n[1] || b[1] > n[3])
}

/// (x0, y0, x1, y1): numpy's min / max of each column (a NaN in a column is that column's).
fn bbox(p: &[P]) -> [f64; 4] {
    let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    let mut nan = [false; 2];
    for q in p {
        for c in 0..2 {
            if q[c].is_nan() {
                nan[c] = true;
            } else {
                if q[c] < b[c] {
                    b[c] = q[c];
                }
                if q[c] > b[c + 2] {
                    b[c + 2] = q[c];
                }
            }
        }
    }
    for c in 0..2 {
        if nan[c] {
            b[c] = f64::NAN;
            b[c + 2] = f64::NAN;
        }
    }
    b
}

/// Python's max(a, b) / min(a, b): the first unless the second is greater / less.
fn pmax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

fn pmin(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

/// roads.sub_polyline of a lon / lat ring (>= 2 points): the part between fractions a < b of its length (lon scaled
/// by the cos of its mean latitude), np.interp's ends.
fn sub_polyline(r: &[P], a: f64, b: f64) -> Vec<P> {
    let lat: Vec<f64> = r.iter().map(|p| p[1]).collect();
    let c = (np_sum(&lat) / r.len() as f64).to_radians().dcos();
    let mut s = vec![0.0f64; r.len()];
    for v in 1..r.len() {
        s[v] = s[v - 1] + ((r[v][0] - r[v - 1][0]) * c).dhypot(r[v][1] - r[v - 1][1]);
    }
    let tot = s[r.len() - 1];
    if tot <= 0.0 {
        return vec![r[0]];
    }
    let (sa, sb) = (a * tot, b * tot);
    let lon: Vec<f64> = r.iter().map(|p| p[0]).collect();
    let at = |x: f64| [crate::dm::interp(&[x], &s, &lon, None, None)[0], crate::dm::interp(&[x], &s, &lat, None, None)[0]];
    let mut out = vec![at(sa)];
    for v in 0..r.len() {
        if s[v] > sa && s[v] < sb {
            out.push(r[v]);
        }
    }
    out.push(at(sb));
    out
}

/// railways._resample(xy, step): points every step m from the start (np.arange's i * step), the end kept (a sliver
/// under a quarter step before it dropped), np.interp along the arc; xy[:1] for a line of 1e-6 m or less.
fn resample(xy: &[P], step: f64) -> Result<Vec<P>, ResErr> {
    let k = xy.len();
    let mut s = vec![0.0f64; k];
    for i in 1..k {
        s[i] = s[i - 1] + (xy[i][0] - xy[i - 1][0]).dhypot(xy[i][1] - xy[i - 1][1]);
    }
    let tot = s[k - 1];
    if tot <= 1e-6 {
        return Ok(vec![xy[0]]);
    }
    let q = tot / step;
    if q.is_nan() {
        return Err(ResErr::Value("arange: cannot compute length"));
    }
    let len = q.ceil();
    if len >= 9.2e18 {
        return Err(ResErr::Value("Maximum allowed size exceeded"));
    }
    if len > RESAMPLE_MAX {
        return Err(ResErr::Memory);
    }
    let mut t: Vec<f64> = (0..len as usize).map(|i| i as f64 * step).collect();
    t.push(tot);
    let m = t.len();
    if m > 2 && t[m - 1] - t[m - 2] < 0.25 * step {
        t.remove(m - 2);
    }
    let xs: Vec<f64> = xy.iter().map(|p| p[0]).collect();
    let ys: Vec<f64> = xy.iter().map(|p| p[1]).collect();
    let (rx, ry) = (crate::dm::interp(&t, &s, &xs, None, None), crate::dm::interp(&t, &s, &ys, None, None));
    Ok(rx.into_iter().zip(ry).map(|(x, y)| [x, y]).collect())
}

/// Stations of the rendered ground rail on either side of a profile crossing.
#[pyfunction]
pub fn surface_rail_samples(lines: Vec<Vec<P>>, points: Vec<(usize, P)>, step: f64)
    -> PyResult<Vec<(P, P, f64)>> {
    if !step.is_finite() || step <= 0. { return Err(PyValueError::new_err("invalid rail step")); }
    let mut sampled = vec![None; lines.len()];
    let mut out = Vec::with_capacity(points.len());
    for (i, p) in points {
        if i >= lines.len() || lines[i].len() < 2 {
            return Err(PyValueError::new_err("invalid rail line"));
        }
        if sampled[i].is_none() {
            sampled[i] = Some(resample(&lines[i], step).map_err(|_| PyValueError::new_err("invalid rail span"))?);
        }
        let rail = sampled[i].as_ref().unwrap();
        let mut best = f64::INFINITY;
        let mut hit = (rail[0], rail[0], 0.);
        for w in rail.windows(2) {
            let d = [w[1][0] - w[0][0], w[1][1] - w[0][1]];
            let v = [p[0] - w[0][0], p[1] - w[0][1]];
            let norm = d[0] * d[0] + d[1] * d[1];
            if norm == 0. { continue; }
            let u = ((d[0] * v[0] + d[1] * v[1]) / norm).clamp(0., 1.);
            let (x,y) = (v[0] - u*d[0], v[1] - u*d[1]);
            let dist = x*x + y*y;
            if dist < best { best = dist; hit = (w[0], w[1], u); }
        }
        out.push(hit);
    }
    Ok(out)
}

/// roads.clip_polyline(xy, *near) is not empty: a segment of xy reaches into the box (Liang-Barsky, the kernel's).
fn clips(p: &[P], n: &[f64; 4]) -> bool {
    let [x0, y0, x1, y1] = *n;
    for w in p.windows(2) {
        let (ax, ay) = (w[0][0], w[0][1]);
        let (dx, dy) = (w[1][0] - ax, w[1][1] - ay);
        let (mut t0, mut t1, mut ok) = (0.0f64, 1.0f64, true);
        for (pp, q) in [(-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)] {
            if pp == 0.0 {
                if q < 0.0 {
                    ok = false;
                    break;
                }
            } else {
                let t = q / pp;
                if pp < 0.0 {
                    if t > t0 {
                        t0 = t;
                    }
                } else if t < t1 {
                    t1 = t;
                }
            }
        }
        if !ok || t0 > t1 {
            continue;
        }
        return true;
    }
    false
}

impl RailIndex {
    /// railways._pieces_of with these ranges: [(kind, its rings)], ground, bridge then tunnel, each ring by ring and
    /// range by range (every ring of 2 points or more; the whole ring for (0, 1), else sub_polyline's), in the frame.
    fn pieces_of(&self, r: &Row, rngs: &[(f64, f64, u8)], tunnels: bool) -> Vec<Piece> {
        let l = r.len_m;
        let min_part = pmin(self.min_part, self.min_part_m / pmax(l, 1.0));
        let off: Vec<(f64, f64, u8)> = rngs.iter().copied()
            .filter(|&(a, b, k)| !(k == TUNNEL && (b - a) * l < self.short_tunnel_m)).collect();
        let (mut ground, mut at) = (Vec::new(), 0.0f64);
        for &(a, b, _) in &off {
            if a - at > min_part {
                ground.push((at, a));
            }
            at = pmax(at, b);
        }
        if 1.0 - at > min_part {
            ground.push((at, 1.0));
        }
        let of = |want: u8| -> Vec<(f64, f64)> { off.iter().filter(|x| x.2 == want).map(|x| (x.0, x.1)).collect() };
        let spans = [ground, of(BRIDGE), if tunnels { of(TUNNEL) } else { Vec::new() }];
        let mut out: [Vec<Vec<P>>; 3] = [Vec::new(), Vec::new(), Vec::new()];
        for ring in &r.rings {
            for (kind, sp) in spans.iter().enumerate() {
                for &(a, b) in sp {
                    if b - a > min_part {
                        if a == 0.0 && b == 1.0 {
                            out[kind].push(ring.xy.clone());
                        } else {
                            let sub = sub_polyline(&ring.ll, a, b);
                            if sub.len() >= 2 {
                                out[kind].push(sub.iter().map(|p| {
                                    let (e, n, _u) = crate::g2e_one(p[1], p[0], 0.0, &self.origin, &self.rot);
                                    [e, n]
                                }).collect());
                            }
                        }
                    }
                }
            }
        }
        let mut pieces = Vec::new();
        for (kind, rings) in out.into_iter().enumerate() {
            for xy in rings {
                pieces.push(Piece { kind: kind as u8, bbox: bbox(&xy), xy, res: None });
            }
        }
        pieces
    }

    /// The parts of row k within near (its pieces for rngs: kept for its latest ranges), as (kind, resampling).
    fn take(&self, k: usize, rngs: Vec<(f64, f64, u8)>, tunnels: bool, near: &[f64; 4], tram: bool)
            -> PyResult<Vec<(u8, Arc<Vec<P>>)>> {
        let mut cache = self.cache.lock().unwrap_or_else(|e| e.into_inner());
        let slot = &mut cache[k];
        let same = |c: &Cached| c.tunnels == tunnels && c.rngs.len() == rngs.len()
            && c.rngs.iter().zip(&rngs).all(|(x, y)| x.0 == y.0 && x.1 == y.1 && x.2 == y.2);
        let pos = match slot.iter().position(same) {
            Some(p) => p,
            None => {
                let pieces = self.pieces_of(&self.rows[k], &rngs, tunnels);
                slot.insert(0, Cached { rngs, tunnels, pieces });
                slot.truncate(CACHED_MAX);
                0
            }
        };
        within(&mut slot[pos].pieces, near, tram, self.step)
    }

    /// The parts of row k within near when it has no level ranges: roads.ground_parts' rings (_pieces' path for
    /// them: that memo's rings, whatever ranges it was made with), kept while they are the same objects.
    fn take_ground(&self, k: usize, rings: Vec<(Py<PyAny>, GRing)>, near: &[f64; 4], tram: bool)
                   -> PyResult<Vec<(u8, Arc<Vec<P>>)>> {
        let ids: Vec<usize> = rings.iter().map(|(o, _)| o.as_ptr() as usize).collect();
        // (the entry replaced, and its rings, let go only after the lock: their last reference can run Python)
        let mut old: Option<GroundCached> = None;
        let mut cache = self.ground.lock().unwrap_or_else(|e| e.into_inner());
        let slot = &mut cache[k];
        if slot.as_ref().is_none_or(|g| g.ids != ids) {
            let row = &self.rows[k];
            let mut keep = Vec::with_capacity(rings.len());
            let mut pieces = Vec::with_capacity(rings.len());
            for (o, g) in rings {
                let xy: Vec<P> = match g {
                    GRing::View(start, n, pts) => match row.rings.iter().find(|r| r.start == start && r.xy.len() == n) {
                        Some(r) => r.xy.clone(),
                        None => self.enu(&pts),
                    },
                    GRing::Pts(pts) => self.enu(&pts),
                };
                pieces.push(Piece { kind: GROUND, bbox: bbox(&xy), xy, res: None });
                keep.push(o);
            }
            old = slot.replace(GroundCached { ids, _keep: keep, pieces });
        }
        let got = within(&mut slot.as_mut().expect("ground pieces").pieces, near, tram, self.step);
        drop(cache);
        drop(old);
        got
    }

    /// The frame's (e, n) of lon / lat points (geodetic_to_enu at h 0).
    fn enu(&self, p: &[P]) -> Vec<P> {
        p.iter().map(|q| {
            let (e, n, _u) = crate::g2e_one(q[1], q[0], 0.0, &self.origin, &self.rot);
            [e, n]
        }).collect()
    }

    /// A ground_parts ring as mapgeom.enu_xy takes it: a view of the layer's lonlat (the same dtype, strides and
    /// columns, inside it) is sliced from xy_in, anything else converted.
    fn ground_ring(&self, o: &Bound<'_, PyAny>) -> PyResult<GRing> {
        let a = o.cast::<PyArray2<f64>>().map_err(|_| {
            pyo3::exceptions::PyTypeError::new_err("rail_parts_select: a ground part is a float64 (k, 2) array")
        })?;
        let pts = points(&a.readonly(), "a ground part")?;
        let st = a.strides();
        let (ptr, l) = (a.data() as usize as isize, &self.lonlat);
        if !pts.is_empty() && st.len() == 2 && st[0] == l.strides[0] && st[1] == l.strides[1] && a.shape()[1] == 2
            && l.strides[0] > 0 {
            let d = ptr - l.ptr as isize;
            let (off, rem) = (d.div_euclid(l.strides[0]), d.rem_euclid(l.strides[0]));
            if rem == 0 && off >= 0 && off as usize + pts.len() <= l.rows {
                return Ok(GRing::View(off as usize, pts.len(), pts));
            }
        }
        Ok(GRing::Pts(pts))
    }
}

/// The pieces within near (box, clip test) resampled, in order (a tram's ground pieces left out: its street track
/// stays draped in rails.py); a resampling's error raised.
fn within(pieces: &mut [Piece], near: &[f64; 4], tram: bool, step: f64) -> PyResult<Vec<(u8, Arc<Vec<P>>)>> {
    let mut out = Vec::new();
    for pc in pieces.iter_mut() {
        if tram && pc.kind != BRIDGE {
            continue;
        }
        if !box_hit(&pc.bbox, near) || !clips(&pc.xy, near) {
            continue;
        }
        if pc.res.is_none() {
            pc.res = Some(resample(&pc.xy, step).map(Arc::new));
        }
        match pc.res.as_ref().expect("resampled") {
            Ok(v) => {
                if v.len() >= 2 {
                    out.push((pc.kind, v.clone()));
                }
            }
            Err(ResErr::Memory) => return Err(PyMemoryError::new_err("rail part resampling: too many points")),
            Err(ResErr::Value(m)) => return Err(PyValueError::new_err(*m)),
        }
    }
    Ok(out)
}

/// A level range list as railways._pieces_of reads it: (start, end, kind) triples, kind "bridge" / "tunnel" / other.
fn ranges_of(v: &Bound<'_, PyAny>) -> PyResult<Vec<(f64, f64, u8)>> {
    let mut out = Vec::new();
    for item in v.try_iter()? {
        let item = item?;
        let t: Vec<Bound<'_, PyAny>> = item.try_iter()?.collect::<PyResult<_>>()?;
        if t.len() != 3 {
            return Err(PyValueError::new_err(format!("a level range unpacks to 3 values, not {}", t.len())));
        }
        let kind = match t[2].extract::<&str>() {
            Ok("bridge") => BRIDGE,
            Ok("tunnel") => TUNNEL,
            _ => OTHER,
        };
        out.push((t[0].extract::<f64>()?, t[1].extract::<f64>()?, kind));
    }
    Ok(out)
}

/// railways.parts' feature loop over cells (each layer's RailIndex for the frame, None for a layer without rails or a
/// subtype column) within near (x0, y0, x1, y1): [(fid, class, kind, xy (k, 2))] sorted by (fid, kind, x0, y0). Each
/// feature once, from the cell holding its longest copy (1 m longer than the first); its wear into wear_of (a heritage
/// line's id into kept with lifecycle), its OSM gauge into gauges (when given); level_ranges(cells[c], i) asked for
/// each feature taken, in the Python's order, and ground_parts(cells[c], i) for one without any (railways._pieces);
/// trams' bridges only.
#[pyfunction]
#[pyo3(signature = (indexes, cells, near, level_ranges, ground_parts, wear_of, kept, gauges, tunnels, lifecycle))]
#[allow(clippy::too_many_arguments)]
pub fn rail_parts_select<'py>(py: Python<'py>, indexes: Vec<Option<Bound<'py, RailIndex>>>,
                              cells: Vec<Bound<'py, PyAny>>, near: (f64, f64, f64, f64),
                              level_ranges: Bound<'py, PyAny>, ground_parts: Bound<'py, PyAny>, wear_of: Bound<'py, PyDict>, kept: Bound<'py, PySet>,
                              gauges: Option<Bound<'py, PyDict>>, tunnels: bool, lifecycle: bool)
                              -> PyResult<Bound<'py, PyList>> {
    if cells.len() != indexes.len() {
        return Err(PyValueError::new_err("rail_parts_select: a cell per index"));
    }
    let near = [near.0, near.1, near.2, near.3];
    let ix: Vec<Option<&RailIndex>> = indexes.iter().map(|x| x.as_ref().map(|b| b.get())).collect();
    // (the longest copy of each feature over the cells: a later one replaces it only 1 m longer)
    let mut longest: HashMap<&[u32], (f64, usize)> = HashMap::new();
    for (ci, x) in ix.iter().enumerate() {
        let Some(x) = x else { continue };
        for r in &x.rows {
            if !box_hit(&r.bbox, &near) {
                continue;
            }
            match longest.get_mut(r.key.as_slice()) {
                None => {
                    longest.insert(r.key.as_slice(), (r.len_m, ci));
                }
                Some(v) => {
                    if r.len_m > v.0 + 1.0 {
                        *v = (r.len_m, ci);
                    }
                }
            }
        }
    }
    let mut seen: HashSet<&[u32]> = HashSet::new();
    let mut taken: Vec<(usize, usize)> = Vec::new();
    for (ci, x) in ix.iter().enumerate() {
        let Some(x) = x else { continue };
        for (k, r) in x.rows.iter().enumerate() {
            if !box_hit(&r.bbox, &near) || seen.contains(r.key.as_slice())
                || longest.get(r.key.as_slice()).map_or(ci, |v| v.1) != ci || r.cls < 0 {
                continue;
            }
            seen.insert(r.key.as_slice());
            taken.push((ci, k));
        }
    }
    // (each in turn as the Python went: its wear and gauge, its level ranges (ground_parts' rings without any), its
    // pieces in the window; a failure stops there)
    let mut got: Vec<(usize, usize, u8, Arc<Vec<P>>)> = Vec::new();
    for &(ci, k) in &taken {
        let x = ix[ci].expect("an index");
        let r = &x.rows[k];
        let fid = r.fid.bind(py);
        if r.wear != 0 {
            wear_of.set_item(fid, r.wear)?;
            if lifecycle && r.heritage {
                kept.add(fid)?;
            }
        }
        if let Some(g) = &gauges {
            if !r.gauge.is_nan() && r.gauge != 0.0 {
                g.set_item(fid, r.gauge)?;
            }
        }
        let rr = ranges_of(&level_ranges.call1((&cells[ci], r.i))?)?;
        let parts = if rr.is_empty() {
            let mut rings = Vec::new();
            for o in ground_parts.call1((&cells[ci], r.i))?.try_iter()? {
                let o = o?;
                let g = x.ground_ring(&o)?;
                rings.push((o.unbind(), g));
            }
            x.take_ground(k, rings, &near, r.cls == TRAM)?
        } else {
            x.take(k, rr, tunnels, &near, r.cls == TRAM)?
        };
        for (kind, v) in parts {
            got.push((ci, k, kind, v));
        }
    }
    // (Python's sort of (fid, kind, x0, y0): stable; the first points are finite, every point of a resampled line is)
    let key = |g: &(usize, usize, u8, Arc<Vec<P>>)| -> (&[u32], &str, f64, f64) {
        (ix[g.0].expect("an index").rows[g.1].key.as_slice(), KINDS[g.2 as usize], g.3[0][0], g.3[0][1])
    };
    got.sort_by(|a, b| {
        let (ka, kb) = (key(a), key(b));
        ka.0.cmp(kb.0).then(ka.1.cmp(kb.1))
            .then(ka.2.partial_cmp(&kb.2).unwrap_or(std::cmp::Ordering::Equal))
            .then(ka.3.partial_cmp(&kb.3).unwrap_or(std::cmp::Ordering::Equal))
    });
    let out = PyList::empty(py);
    for (ci, k, kind, v) in got {
        let r = &ix[ci].expect("an index").rows[k];
        let cls = crate::raildrawn::CLASSES[r.cls as usize];
        let flat: Vec<f64> = v.iter().flat_map(|p| [p[0], p[1]]).collect();
        let arr = numpy::ndarray::Array2::from_shape_vec((v.len(), 2), flat)
            .map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py);
        out.append(PyTuple::new(py, [r.fid.bind(py).clone().into_any(), PyString::intern(py, cls).into_any(),
                                     PyString::intern(py, KINDS[kind as usize]).into_any(), arr.into_any()])?)?;
    }
    Ok(out)
}
