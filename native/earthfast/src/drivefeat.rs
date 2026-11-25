//! Mapped driving features in the road's graded profile (server/calming.py, BNE_CALMING; server/drivefeat.py):
//! speed bumps, humps, tables and cushions (and fords' dips, BNE_UNPAVED) added to a road plan's heights, the plan
//! densified round each so the physics ground (grading.grade_points' lattice, 1 m) and everything drawn on it take the
//! feature's shape.
//!
//! A feature is a point on the road (an OpenStreetMap node of its way) with a profile along the road: up a ramp of
//! length `ramp`, a flat top of length `top`, down again, `height` m at the top (negative: a dip). The ramps are
//! half-cosines (a hump of top 0 is the sinusoidal UK / US road hump). The profile is a pure function of the distance
//! along the plan from the feature's foot (its nearest point on the plan's line): two plans of one road (a cell's
//! pieces, overlapping) give the same heights where they overlap.
//!
//! A plan takes a feature whose foot lies within `reach` m of the point (the node sits on its way's line; a crossing
//! road's plan passes farther off except at the shared node). Vertices are added every `step` m over the feature's
//! length and one `step` past either end, each at the plan's own linear height there (grade_points interpolates the
//! same), its bank and widen interpolated and its cut row its segment's.

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

use crate::dm::Dm;

/// A level feature (a ford) is held level only where the plan rises at most this much over its flat top.
pub const LEVEL_MAX_RISE_M: f64 = 0.15;

/// One feature: position (x, y) in the plan's frame and its profile.
#[derive(Clone, Copy, Debug)]
pub struct Feature {
    pub x: f64,
    pub y: f64,
    pub height: f64,
    pub ramp: f64,
    pub top: f64,
    /// a level bottom (a ford): the top held at the lowest planned height over it less |height|, the ramps blending
    /// the plan into it; else the profile is added to the plan's heights
    pub level: bool,
}

/// The profile's offset at signed distance `d` from the feature's centre (0 outside it).
pub fn profile(f: &Feature, d: f64) -> f64 {
    let half_top = 0.5 * f.top;
    let a = d.abs();
    if a <= half_top {
        return f.height;
    }
    let r = a - half_top;
    if f.ramp <= 0.0 || r >= f.ramp {
        return 0.0;
    }
    // half-cosine ramp: 1 at the top's edge, 0 at the foot, level tangents at both
    let t = r / f.ramp;
    f.height * 0.5 * (1.0 + (std::f64::consts::PI * t).dcos())
}

/// Cumulative distance of each vertex along the polyline.
fn chain(xy: &[[f64; 2]]) -> Vec<f64> {
    let mut s = Vec::with_capacity(xy.len());
    let mut acc = 0.0;
    for i in 0..xy.len() {
        if i > 0 {
            acc += (xy[i][0] - xy[i - 1][0]).dhypot(xy[i][1] - xy[i - 1][1]);
        }
        s.push(acc);
    }
    s
}

/// (distance along, distance off) of point p's foot on the polyline (the nearest; the first of equals). Past an end
/// the foot is on the end segment's line carried on (a distance along below 0 or past the length): a feature just
/// past a plan's end still reaches into it with its ramp.
fn foot(xy: &[[f64; 2]], s: &[f64], p: [f64; 2]) -> Option<(f64, f64)> {
    let nseg = xy.len().saturating_sub(1);
    let mut best: Option<(f64, f64)> = None;
    for i in 0..nseg {
        let (ax, ay) = (xy[i][0], xy[i][1]);
        let (dx, dy) = (xy[i + 1][0] - ax, xy[i + 1][1] - ay);
        let l2 = dx * dx + dy * dy;
        if !(l2 > 0.0) {
            continue;
        }
        let mut t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2;
        let lo = if i == 0 { f64::NEG_INFINITY } else { 0.0 };
        let hi = if i + 1 == nseg { f64::INFINITY } else { 1.0 };
        t = t.clamp(lo, hi);
        let (qx, qy) = (ax + t * dx, ay + t * dy);
        let off = (p[0] - qx).dhypot(p[1] - qy);
        if best.map_or(true, |b| off < b.1) {
            best = Some((s[i] + t * l2.sqrt(), off));
        }
    }
    best
}

/// The plan's vertices with the features added: (xy, z, bank, widen, the source segment of each new segment, the
/// features taken). bank / widen empty: none. Pure.
#[allow(clippy::type_complexity)]
pub fn apply(xy: &[[f64; 2]], z: &[f64], bank: &[f64], widen: &[f64], feats: &[Feature], reach: f64, step: f64)
             -> (Vec<[f64; 2]>, Vec<f64>, Vec<f64>, Vec<f64>, Vec<usize>, Vec<usize>) {
    let n = xy.len();
    let s = chain(xy);
    let total = *s.last().unwrap_or(&0.0);
    // the features on this plan: (centre along, feature)
    let mut on: Vec<(f64, usize)> = Vec::new();
    if n >= 2 && step > 0.0 {
        for (k, f) in feats.iter().enumerate() {
            if let Some((sa, off)) = foot(xy, &s, [f.x, f.y]) {
                let half = 0.5 * f.top + f.ramp;
                if off <= reach && sa + half > 0.0 && sa - half < total {
                    on.push((sa, k));
                }
            }
        }
    }
    if on.is_empty() {
        return (xy.to_vec(), z.to_vec(), bank.to_vec(), widen.to_vec(), (0..n.saturating_sub(1)).collect(), vec![]);
    }
    // the stations to add (distances along), each feature's span and one step past its ends
    let mut add: Vec<f64> = Vec::new();
    for &(c, k) in &on {
        let f = &feats[k];
        let half = 0.5 * f.top + f.ramp;
        let m = (half / step).ceil() as i64 + 1;
        for j in -m..=m {
            let d = j as f64 * step;
            let at = c + d.clamp(-half - step, half + step);
            if at > 0.0 && at < total {
                add.push(at);
            }
        }
        for at in [c - half, c + half, c - 0.5 * f.top, c + 0.5 * f.top] {
            if at > 0.0 && at < total {
                add.push(at);
            }
        }
    }
    // merged with the plan's own vertices (theirs kept exactly), in order; stations within 1 mm of another dropped
    let mut st: Vec<(f64, Option<usize>)> = s.iter().enumerate().map(|(i, &v)| (v, Some(i))).collect();
    st.extend(add.into_iter().map(|v| (v, None)));
    st.sort_by(|a, b| a.0.total_cmp(&b.0).then_with(|| b.1.is_some().cmp(&a.1.is_some())));
    let mut merged: Vec<(f64, Option<usize>)> = Vec::with_capacity(st.len());
    for e in st {
        if let Some(last) = merged.last() {
            if e.0 - last.0 < 1e-3 {
                if e.1.is_some() && last.1.is_none() {
                    merged.pop();
                } else {
                    continue;
                }
            }
        }
        merged.push(e);
    }
    let lerp = |v: &[f64], i: usize, t: f64| v[i] + t * (v[i + 1] - v[i]);
    let mut oxy = Vec::with_capacity(merged.len());
    let mut oz = Vec::with_capacity(merged.len());
    let mut ob = Vec::new();
    let mut ow = Vec::new();
    let mut seg_of = Vec::with_capacity(merged.len());     // the source segment each station lies in
    for &(at, orig) in &merged {
        if let Some(o) = orig {
            oxy.push(xy[o]);
            oz.push(z[o]);
            if !bank.is_empty() {
                ob.push(bank[o]);
            }
            if !widen.is_empty() {
                ow.push(widen[o]);
            }
            seg_of.push(o.min(n - 2));
            continue;
        }
        // (the segment holding it: the last vertex at or before it, at most the last segment)
        let seg = (s.partition_point(|&v| v <= at).max(1) - 1).min(n - 2);
        let len = s[seg + 1] - s[seg];
        let t = if len > 0.0 { ((at - s[seg]) / len).clamp(0.0, 1.0) } else { 0.0 };
        oxy.push([xy[seg][0] + t * (xy[seg + 1][0] - xy[seg][0]), xy[seg][1] + t * (xy[seg + 1][1] - xy[seg][1])]);
        oz.push(lerp(z, seg, t));
        if !bank.is_empty() {
            ob.push(lerp(bank, seg, t));
        }
        if !widen.is_empty() {
            ow.push(lerp(widen, seg, t));
        }
        seg_of.push(seg);
    }
    // a level feature's bottom: the lowest of the plan's own heights over its top (the stations there), less |height|
    let base = oz.clone();
    let bottoms: Vec<f64> = on.iter().map(|&(c, k)| {
        let f = &feats[k];
        if !f.level {
            return 0.0;
        }
        let over: Vec<f64> = merged.iter().zip(&base).filter(|((at, _), _)| (at - c).abs() <= 0.5 * f.top + 1e-9)
            .map(|(_, z)| *z).collect();
        let lo = over.iter().cloned().fold(f64::INFINITY, f64::min);
        let hi = over.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
        // (on a road climbing more than LEVEL_MAX_RISE_M over the top, not level: the dip follows the slope, NaN)
        if !lo.is_finite() || hi - lo > LEVEL_MAX_RISE_M { f64::NAN } else { lo - f.height.abs() }
    }).collect();
    // the features' offsets: where several reach a station, the highest rise and the deepest dip (not their sum: one
    // hump mapped twice, on a node of each of two ways or as two close nodes, is one hump, not a double one)
    for (i, &(at, _)) in merged.iter().enumerate() {
        let (mut up, mut down) = (0.0f64, 0.0f64);
        for (j, &(c, k)) in on.iter().enumerate() {
            let f = &feats[k];
            let d = if f.level && bottoms[j].is_finite() {
                // (the weight of the bottom: 1 on the top, the half-cosine down the ramps)
                let w = if f.height != 0.0 { profile(f, at - c) / f.height } else { 0.0 };
                (w * (bottoms[j] - base[i])).min(0.0)
            } else {
                profile(f, at - c)
            };
            up = up.max(d);
            down = down.min(d);
        }
        oz[i] += up + down;
    }
    // each new segment's source segment: the one its first station lies in
    let segs: Vec<usize> = (0..merged.len().saturating_sub(1)).map(|i| seg_of[i]).collect();
    let mut taken: Vec<usize> = on.iter().map(|&(_, k)| k).collect();
    taken.sort_unstable();
    taken.dedup();
    (oxy, oz, ob, ow, segs, taken)
}

/// calming.apply's kernel: (xy (n2, 2), z (n2,), bank (n2,) or empty, widen (n2,) or empty, segs (n2 - 1,) the source
/// segment of each, taken: the features' indices on this plan). feats (m, 5 or 6): x, y, height, ramp, top[, level].
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn calming_apply<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                          bank: PyReadonlyArray1<'py, f64>, widen: PyReadonlyArray1<'py, f64>,
                          feats: PyReadonlyArray2<'py, f64>, reach: f64, step: f64)
                          -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>,
                                       Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>)> {
    let xa = xy.as_array();
    let fa = feats.as_array();
    if xa.ncols() != 2 || (fa.nrows() > 0 && fa.ncols() != 5 && fa.ncols() != 6) {
        return Err(pyo3::exceptions::PyValueError::new_err("xy (n, 2), feats (m, 5 or 6)"));
    }
    let pts: Vec<[f64; 2]> = xa.rows().into_iter().map(|r| [r[0], r[1]]).collect();
    let zv = z.as_array().to_vec();
    let bv = bank.as_array().to_vec();
    let wv = widen.as_array().to_vec();
    if zv.len() != pts.len() || (!bv.is_empty() && bv.len() != pts.len()) || (!wv.is_empty() && wv.len() != pts.len()) {
        return Err(pyo3::exceptions::PyValueError::new_err("z / bank / widen not one per vertex"));
    }
    let fs: Vec<Feature> = fa.rows().into_iter()
        .map(|r| Feature { x: r[0], y: r[1], height: r[2], ramp: r[3], top: r[4], level: r.len() > 5 && r[5] != 0.0 })
        .collect();
    let (oxy, oz, ob, ow, segs, taken) = apply(&pts, &zv, &bv, &wv, &fs, reach, step);
    let flat: Vec<f64> = oxy.iter().flat_map(|p| [p[0], p[1]]).collect();
    let arr = numpy::ndarray::Array2::from_shape_vec((oxy.len(), 2), flat)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok((arr.into_pyarray(py), oz.into_pyarray(py), ob.into_pyarray(py), ow.into_pyarray(py),
        segs.into_iter().map(|v| v as i64).collect::<Vec<_>>().into_pyarray(py),
        taken.into_iter().map(|v| v as i64).collect::<Vec<_>>().into_pyarray(py)))
}

/// A road-end / roadside pad of a mapped node (server/roadends.py, BNE_ROAD_ENDS): its outline (closed, counter-clockwise,
/// the first point not repeated), the road it belongs to and its centre.
#[derive(Clone, Debug)]
pub struct Pad {
    pub poly: Vec<[f64; 2]>,
    pub line: usize,
    pub centre: [f64; 2],
}

/// The nearest point of the lines to p: (line, segment, t, distance, point).
fn nearest(lines: &[Vec<[f64; 2]>], p: [f64; 2]) -> Option<(usize, usize, f64, f64, [f64; 2])> {
    let mut best: Option<(usize, usize, f64, f64, [f64; 2])> = None;
    for (li, l) in lines.iter().enumerate() {
        for i in 0..l.len().saturating_sub(1) {
            let (a, b) = (l[i], l[i + 1]);
            let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
            let l2 = dx * dx + dy * dy;
            let t = if l2 > 0.0 { (((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2).clamp(0.0, 1.0) } else { 0.0 };
            let q = [a[0] + t * dx, a[1] + t * dy];
            let d = (p[0] - q[0]).dhypot(p[1] - q[1]);
            if best.map_or(true, |bb| d < bb.3) {
                best = Some((li, i, t, d, q));
            }
        }
    }
    best
}

/// The pads of nodes (x, y, kind, radius, side): kind 1 a turning circle / loop (a disc of `radius` m round the node,
/// on a road whose end lies within `reach` m of it: a dead end; none on a through road), kind 2 a passing place (a
/// lay-by on the road's `side` (+1 left of its direction, -1 right), `radius` m wide, PASS_LEN_M long with
/// PASS_TAPER_M tapers, its inner edge the carriageway's edge (half[line])). `segs`: the disc's sides. Pure; a node on
/// no road within reach has none.
pub const PASS_LEN_M: f64 = 15.0;
pub const PASS_TAPER_M: f64 = 6.0;

pub fn pads(lines: &[Vec<[f64; 2]>], half: &[f64], nodes: &[[f64; 5]], reach: f64, segs: usize) -> Vec<Pad> {
    let mut out = Vec::new();
    for nd in nodes {
        let p = [nd[0], nd[1]];
        let kind = nd[2] as i64;
        let radius = nd[3];
        let Some((li, si, t, d, q)) = nearest(lines, p) else { continue };
        if !(d <= reach + half[li]) || !(radius > 0.0) {
            continue;
        }
        let l = &lines[li];
        if kind == 1 {
            // a dead end: one of the road's two ends within reach of the node
            let ends = [l[0], l[l.len() - 1]];
            if !ends.iter().any(|e| (e[0] - p[0]).dhypot(e[1] - p[1]) <= reach) {
                continue;
            }
            let n = segs.max(8);
            let poly = (0..n).map(|k| {
                let a = 2.0 * std::f64::consts::PI * k as f64 / n as f64;
                let (sa, ca) = a.dsin_cos();
                [p[0] + radius * ca, p[1] + radius * sa]
            }).collect();
            out.push(Pad { poly, line: li, centre: p });
        } else if kind == 2 {
            let (a, b) = (l[si], l[si + 1]);
            let len = (b[0] - a[0]).dhypot(b[1] - a[1]);
            if !(len > 0.0) {
                continue;
            }
            let u = [(b[0] - a[0]) / len, (b[1] - a[1]) / len];
            let side = if nd[4] < 0.0 { -1.0 } else { 1.0 };
            let nrm = [-u[1] * side, u[0] * side];
            let e0 = half[li];
            let e1 = half[li] + radius;
            let h = 0.5 * PASS_LEN_M;
            let tp = PASS_TAPER_M;
            let at = |s: f64, off: f64| [q[0] + u[0] * s + nrm[0] * off, q[1] + u[1] * s + nrm[1] * off];
            let _ = t;
            let mut poly = vec![at(-h - tp, e0 - 0.05), at(h + tp, e0 - 0.05), at(h, e1), at(-h, e1)];
            if side < 0.0 {
                poly.reverse();     // (counter-clockwise either side)
            }
            out.push(Pad { poly, line: li, centre: at(0.0, 0.5 * (e0 + e1)) });
        }
    }
    out
}

/// roadends.pads' kernel: [(outline (k, 2), road index, centre (2,))] of nodes (m, 5) on lines.
#[pyfunction]
pub fn drive_pads<'py>(py: Python<'py>, lines: Vec<PyReadonlyArray2<'py, f64>>, half: Vec<f64>,
                       nodes: PyReadonlyArray2<'py, f64>, reach: f64, segs: usize)
                       -> PyResult<Vec<(Bound<'py, PyArray2<f64>>, usize, (f64, f64))>> {
    if half.len() != lines.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("one half width per line"));
    }
    let ls: Vec<Vec<[f64; 2]>> = lines.iter().map(|a| a.as_array().rows().into_iter().map(|r| [r[0], r[1]]).collect())
        .collect();
    let na = nodes.as_array();
    if na.nrows() > 0 && na.ncols() != 5 {
        return Err(pyo3::exceptions::PyValueError::new_err("nodes (m, 5)"));
    }
    let ns: Vec<[f64; 5]> = na.rows().into_iter().map(|r| [r[0], r[1], r[2], r[3], r[4]]).collect();
    let mut out = Vec::new();
    for pd in pads(&ls, &half, &ns, reach, segs) {
        let flat: Vec<f64> = pd.poly.iter().flat_map(|q| [q[0], q[1]]).collect();
        let arr = numpy::ndarray::Array2::from_shape_vec((pd.poly.len(), 2), flat)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        out.push((arr.into_pyarray(py), pd.line, (pd.centre[0], pd.centre[1])));
    }
    Ok(out)
}

/// The paint of mapped nodes on roads (server/drivefeat.tile_teeth): convex white polygons. nodes (m, 6): x, y, kind,
/// a, b, c. kind 1: a hump / table's triangles (a = ramp m, b = flat top m; TSRGD diagram 1062, MUTCD 3B-29's shark's
/// teeth option): on each lane coming in, on its up-ramp, apex toward the crest, TRI_BASE_M across and TRI_LEN_M along,
/// as many as fit across the lane (at most 3). kind 2: a mini roundabout's disc of a m radius round the node. A node
/// on no line within `reach` m (+ the road's half width for a disc) has none. dir: per line, 0 two-way, 1 one-way along
/// its points, -1 against; lanes: per line, lanes each way (at least 1). left_hand: traffic keeps left.
pub const TRI_BASE_M: f64 = 0.9;
/// kinds 3-5's sizes: a build-out OUT_LEN_M along the kerb with OUT_TAPER_M tapers, at most OUT_DEPTH_M into the road,
/// leaving a choker's CHOKE_GAP_M between its two (one lane's width) and a chicane's CHICANE_GAP_M past each, the
/// chicane's two CHICANE_SPACING_M apart; a refuge island ISLAND_LEN_M x ISLAND_W_M on a road at least
/// ISLAND_MIN_ROAD_M wide.
pub const OUT_LEN_M: f64 = 8.0;
pub const OUT_TAPER_M: f64 = 2.0;
pub const OUT_DEPTH_M: f64 = 1.8;
pub const CHOKE_GAP_M: f64 = 3.6;
pub const CHICANE_GAP_M: f64 = 3.3;
pub const CHICANE_SPACING_M: f64 = 14.0;
pub const ISLAND_LEN_M: f64 = 8.0;
pub const ISLAND_W_M: f64 = 1.6;
pub const ISLAND_MIN_ROAD_M: f64 = 7.0;
pub const TRI_LEN_M: f64 = 1.2;

#[allow(clippy::too_many_arguments)]
pub fn marks(lines: &[Vec<[f64; 2]>], half: &[f64], dir: &[i64], lanes: &[i64], nodes: &[[f64; 6]], reach: f64,
             left_hand: bool, segs: usize) -> Vec<Vec<[f64; 2]>> {
    let mut out = Vec::new();
    for nd in nodes {
        let p = [nd[0], nd[1]];
        let Some((li, si, _t, d, q)) = nearest(lines, p) else { continue };
        let kind = nd[2] as i64;
        if kind == 2 {
            if d > reach + half[li] {
                continue;
            }
            let r = nd[3];
            let n = segs.max(8);
            out.push((0..n).map(|k| {
                let a = 2.0 * std::f64::consts::PI * k as f64 / n as f64;
                let (sa, ca) = a.dsin_cos();
                [p[0] + r * ca, p[1] + r * sa]
            }).collect());
            continue;
        }
        if !(1..=5).contains(&kind) || d > reach {
            continue;
        }
        let l = &lines[li];
        let (a, b) = (l[si], l[si + 1]);
        let len = (b[0] - a[0]).dhypot(b[1] - a[1]);
        if !(len > 0.0) {
            continue;
        }
        let u = [(b[0] - a[0]) / len, (b[1] - a[1]) / len];
        let left = [-u[1], u[0]];
        let h = half[li];
        if kind >= 3 {
            // kerbed shapes (outlines, counter-clockwise): 3 a refuge island on the centre line, 4 a choker's two
            // build-outs, 5 a chicane's two (left before, right after); none where the road is too narrow for them
            let at = |s: f64, off: f64| [q[0] + u[0] * s + left[0] * off, q[1] + u[1] * s + left[1] * off];
            let shape = |s0: f64, side: f64, depth: f64| -> Vec<[f64; 2]> {
                let l2 = 0.5 * OUT_LEN_M;
                let mut p = vec![at(s0 - l2, side * (h + 0.3)), at(s0 + l2, side * (h + 0.3)),
                                 at(s0 + l2 - OUT_TAPER_M, side * (h - depth)), at(s0 - l2 + OUT_TAPER_M, side * (h - depth))];
                if side > 0.0 {
                    p.reverse();
                }
                p
            };
            if kind == 3 {
                if 2.0 * h < ISLAND_MIN_ROAD_M {
                    continue;
                }
                let (l2, w2) = (0.5 * ISLAND_LEN_M, 0.5 * ISLAND_W_M);
                out.push(vec![at(-l2, 0.0), at(-l2 + 1.5, -w2), at(l2 - 1.5, -w2), at(l2, 0.0), at(l2 - 1.5, w2),
                              at(-l2 + 1.5, w2)]);
            } else if kind == 4 {
                let depth = (0.5 * (2.0 * h - CHOKE_GAP_M)).min(OUT_DEPTH_M);
                if depth < 0.4 {
                    continue;
                }
                out.push(shape(0.0, -1.0, depth));
                out.push(shape(0.0, 1.0, depth));
            } else {
                let depth = (2.0 * h - CHICANE_GAP_M).min(OUT_DEPTH_M);
                if depth < 0.4 {
                    continue;
                }
                out.push(shape(-0.5 * CHICANE_SPACING_M, 1.0, depth));
                out.push(shape(0.5 * CHICANE_SPACING_M, -1.0, depth));
            }
            continue;
        }
        let (ramp, top) = (nd[3], nd[4]);
        let each = lanes[li].max(1) as f64;
        // the ways in: (direction of travel sign along u, the band across the road it uses: lo..hi m to the left)
        let ways: Vec<(f64, f64, f64)> = match dir[li] {
            1 => vec![(1.0, -h, h)],
            -1 => vec![(-1.0, -h, h)],
            _ => {
                // two-way: traffic along u keeps right (left_hand: left) of the centre line
                if left_hand { vec![(1.0, 0.0, h), (-1.0, -h, 0.0)] } else { vec![(1.0, -h, 0.0), (-1.0, 0.0, h)] }
            }
        };
        for (sg, lo, hi) in ways {
            let t = [u[0] * sg, u[1] * sg];
            // the up-ramp's middle, coming in: before the crest by the top's half and half the ramp
            let at = -(0.5 * top + 0.5 * ramp);
            let lane_w = (hi - lo) / each;
            for k in 0..(each as usize) {
                let l0 = lo + lane_w * k as f64;
                let count = ((lane_w - 0.3) / (TRI_BASE_M + 0.3)).floor().clamp(1.0, 3.0) as usize;
                for j in 0..count {
                    let off = l0 + lane_w * (j as f64 + 0.5) / count as f64;
                    let c = [q[0] + t[0] * at + left[0] * off, q[1] + t[1] * at + left[1] * off];
                    let base0 = [c[0] - t[0] * TRI_LEN_M / 2.0 + left[0] * TRI_BASE_M / 2.0,
                                 c[1] - t[1] * TRI_LEN_M / 2.0 + left[1] * TRI_BASE_M / 2.0];
                    let base1 = [c[0] - t[0] * TRI_LEN_M / 2.0 - left[0] * TRI_BASE_M / 2.0,
                                 c[1] - t[1] * TRI_LEN_M / 2.0 - left[1] * TRI_BASE_M / 2.0];
                    let apex = [c[0] + t[0] * TRI_LEN_M / 2.0, c[1] + t[1] * TRI_LEN_M / 2.0];
                    out.push(vec![base0, base1, apex]);
                }
            }
        }
    }
    out
}

/// drivefeat.tile_teeth's kernel: the polygons (k, 2) of marks().
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn drive_marks<'py>(py: Python<'py>, lines: Vec<PyReadonlyArray2<'py, f64>>, half: Vec<f64>, dir: Vec<i64>,
                        lanes: Vec<i64>, nodes: PyReadonlyArray2<'py, f64>, reach: f64, left_hand: bool, segs: usize)
                        -> PyResult<Vec<Bound<'py, PyArray2<f64>>>> {
    if half.len() != lines.len() || dir.len() != lines.len() || lanes.len() != lines.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("one half width, direction and lane count per line"));
    }
    let ls: Vec<Vec<[f64; 2]>> = lines.iter().map(|a| a.as_array().rows().into_iter().map(|r| [r[0], r[1]]).collect())
        .collect();
    let na = nodes.as_array();
    if na.nrows() > 0 && na.ncols() != 6 {
        return Err(pyo3::exceptions::PyValueError::new_err("nodes (m, 6)"));
    }
    let ns: Vec<[f64; 6]> = na.rows().into_iter().map(|r| [r[0], r[1], r[2], r[3], r[4], r[5]]).collect();
    let mut out = Vec::new();
    for poly in marks(&ls, &half, &dir, &lanes, &ns, reach, left_hand, segs) {
        let flat: Vec<f64> = poly.iter().flat_map(|q| [q[0], q[1]]).collect();
        out.push(numpy::ndarray::Array2::from_shape_vec((poly.len(), 2), flat)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?.into_pyarray(py));
    }
    Ok(out)
}

/// drivefeat.barrier_rows' / rail_rows' kernel: per point (m, 2) its foot on the nearest of lines: (m, 6) rows [line
/// index, foot x, foot y, unit tangent x, y (along the line's points), distance]; a row of NaN (line -1) where there is
/// no line. Pure.
#[pyfunction]
pub fn line_feet<'py>(py: Python<'py>, lines: Vec<PyReadonlyArray2<'py, f64>>, pts: PyReadonlyArray2<'py, f64>)
                      -> PyResult<Bound<'py, PyArray2<f64>>> {
    let ls: Vec<Vec<[f64; 2]>> = lines.iter().map(|a| a.as_array().rows().into_iter().map(|r| [r[0], r[1]]).collect())
        .collect();
    let pa = pts.as_array();
    if pa.nrows() > 0 && pa.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("pts (m, 2)"));
    }
    let mut flat = Vec::with_capacity(pa.nrows() * 6);
    for r in pa.rows() {
        match nearest(&ls, [r[0], r[1]]) {
            Some((li, si, _t, d, q)) => {
                let (a, b) = (ls[li][si], ls[li][si + 1]);
                let len = (b[0] - a[0]).dhypot(b[1] - a[1]);
                let (ux, uy) = if len > 0.0 { ((b[0] - a[0]) / len, (b[1] - a[1]) / len) } else { (1.0, 0.0) };
                flat.extend_from_slice(&[li as f64, q[0], q[1], ux, uy, d]);
            }
            None => flat.extend_from_slice(&[-1.0, f64::NAN, f64::NAN, f64::NAN, f64::NAN, f64::NAN]),
        }
    }
    numpy::ndarray::Array2::from_shape_vec((pa.nrows(), 6), flat)
        .map(|a| a.into_pyarray(py))
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(calming_apply, m)?)?;
    m.add_function(wrap_pyfunction!(drive_pads, m)?)?;
    m.add_function(wrap_pyfunction!(drive_marks, m)?)?;
    m.add_function(wrap_pyfunction!(line_feet, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(n: usize, step: f64) -> (Vec<[f64; 2]>, Vec<f64>) {
        ((0..n).map(|i| [i as f64 * step, 0.0]).collect(), (0..n).map(|i| 0.01 * i as f64 * step).collect())
    }

    #[test]
    fn hump_profile_shape() {
        let f = Feature { x: 0.0, y: 0.0, height: 0.075, ramp: 1.85, top: 0.0, level: false };
        assert!((profile(&f, 0.0) - 0.075).abs() < 1e-12);
        assert_eq!(profile(&f, 1.85), 0.0);
        assert_eq!(profile(&f, -2.0), 0.0);
        assert!((profile(&f, 0.925) - 0.0375).abs() < 1e-9);
        assert_eq!(profile(&f, 0.5), profile(&f, -0.5));
    }

    #[test]
    fn table_adds_vertices_and_height() {
        let (xy, z) = line(6, 4.0);
        let f = [Feature { x: 10.0, y: 0.5, height: 0.08, ramp: 1.5, top: 4.0, level: false }];
        let (oxy, oz, _b, _w, segs, taken) = apply(&xy, &z, &[], &[], &f, 1.5, 0.25);
        assert_eq!(taken, vec![0]);
        assert!(oxy.len() > xy.len() + 20);
        assert_eq!(segs.len(), oxy.len() - 1);
        for (p, h) in oxy.iter().zip(&oz) {
            let base = 0.01 * p[0];
            let d = p[0] - 10.0;
            let want = base + profile(&f[0], d);
            assert!((h - want).abs() < 1e-9, "{} {} {}", p[0], h, want);
        }
        // the plan's own vertices kept exactly (their heights outside the feature too)
        for (i, q) in xy.iter().enumerate() {
            let j = oxy.iter().position(|p| p == q).unwrap();
            if (q[0] - 10.0).abs() > 0.5 * 4.0 + 1.5 {
                assert_eq!(oz[j], z[i]);
            }
        }
        // increasing stations
        for w in oxy.windows(2) {
            assert!(w[1][0] > w[0][0]);
        }
    }

    #[test]
    fn far_feature_untouched() {
        let (xy, z) = line(4, 4.0);
        let f = [Feature { x: 6.0, y: 3.0, height: 0.08, ramp: 1.5, top: 4.0, level: false }];
        let (oxy, oz, _b, _w, segs, taken) = apply(&xy, &z, &[], &[], &f, 1.5, 0.25);
        assert!(taken.is_empty());
        assert_eq!(oxy, xy);
        assert_eq!(oz, z);
        assert_eq!(segs, vec![0, 1, 2]);
    }

    #[test]
    fn bulb_only_at_dead_ends() {
        let lines = vec![vec![[0.0, 0.0], [50.0, 0.0]]];
        let at_end = pads(&lines, &[3.0], &[[50.5, 0.0, 1.0, 12.0, 0.0]], 4.0, 24);
        assert_eq!(at_end.len(), 1);
        assert_eq!(at_end[0].poly.len(), 24);
        let mid = pads(&lines, &[3.0], &[[25.0, 0.0, 1.0, 12.0, 0.0]], 4.0, 24);
        assert!(mid.is_empty());
        let pass = pads(&lines, &[2.0], &[[25.0, 0.3, 2.0, 2.5, -1.0]], 4.0, 24);
        assert_eq!(pass.len(), 1);
        assert!(pass[0].centre[1] < -2.0);
    }

    #[test]
    fn hump_triangles_per_lane() {
        let lines = vec![vec![[0.0, 0.0], [50.0, 0.0]]];
        let two = marks(&lines, &[3.0], &[0], &[1], &[[25.0, 0.0, 1.0, 1.85, 0.0, 0.0]], 1.5, false, 16);
        assert_eq!(two.len(), 4);       // (3 m lanes: two triangles each way)
        let one = marks(&lines, &[3.0], &[1], &[1], &[[25.0, 0.0, 1.0, 1.85, 0.0, 0.0]], 1.5, false, 16);
        // one-way: one lane of 6 m, three triangles, all before the crest
        assert_eq!(one.len(), 3);
        assert!(one.iter().all(|t| t.iter().all(|p| p[0] < 25.0)));
        let isl = marks(&lines, &[4.0], &[0], &[1], &[[25.0, 0.0, 3.0, 0.0, 0.0, 0.0]], 1.5, false, 16);
        assert_eq!(isl.len(), 1);
        assert!(marks(&lines, &[3.0], &[0], &[1], &[[25.0, 0.0, 3.0, 0.0, 0.0, 0.0]], 1.5, false, 16).is_empty());
        let ch = marks(&lines, &[3.0], &[0], &[1], &[[25.0, 0.0, 4.0, 0.0, 0.0, 0.0]], 1.5, false, 16);
        assert_eq!(ch.len(), 2);       // (6 m road: 1.2 m build-outs each side, 3.6 m left)
        let disc = marks(&lines, &[3.0], &[0], &[1], &[[10.0, 1.0, 2.0, 1.5, 0.0, 0.0]], 1.5, false, 16);
        assert_eq!(disc.len(), 1);
        assert_eq!(disc[0].len(), 16);
    }

    #[test]
    fn twice_mapped_is_one_hump() {
        let (xy, z) = line(11, 2.0);
        let f = Feature { x: 10.0, y: 0.0, height: 0.075, ramp: 1.85, top: 0.0, level: false };
        let g = Feature { x: 10.4, ..f };
        let (oxy, oz, ..) = apply(&xy, &z, &[], &[], &[f, g], 1.5, 0.25);
        let top = oxy.iter().zip(&oz).map(|(p, h)| h - 0.01 * p[0]).fold(0.0f64, f64::max);
        assert!(top <= 0.075 + 1e-9, "{top}");
    }

    #[test]
    fn ford_bottom_is_level() {
        let (xy, z) = (vec![[0.0, 0.0], [40.0, 0.0]], vec![0.0, 1.0]);     // (a 2.5 % slope)
        let f = Feature { x: 20.0, y: 0.0, height: -0.3, ramp: 4.0, top: 4.0, level: true };
        let (oxy, oz, ..) = apply(&xy, &z, &[], &[], &[f], 1.5, 0.25);
        let bottom: Vec<f64> = oxy.iter().zip(&oz).filter(|(p, _)| (p[0] - 20.0).abs() <= 2.0).map(|(_, h)| *h).collect();
        let lo = bottom.iter().cloned().fold(f64::INFINITY, f64::min);
        assert!(bottom.iter().all(|h| (h - lo).abs() < 1e-9), "{bottom:?}");
        assert!((lo - (0.45 - 0.3)).abs() < 1e-9, "{lo}");
        // never above the plan
        for (p, h) in oxy.iter().zip(&oz) {
            assert!(*h <= 0.025 * p[0] + 1e-9);
        }
        // a steep road: the dip follows it
        let (oxy, oz, ..) = apply(&xy, &[0.0, 4.0], &[], &[], &[f], 1.5, 0.25);
        let j = oxy.iter().position(|p| (p[0] - 20.0).abs() < 1e-9).unwrap();
        assert!((oz[j] - (2.0 - 0.3)).abs() < 1e-9);
    }

    #[test]
    fn pieces_agree() {
        // two overlapping pieces of one line, the same heights where they overlap
        let (xy, z) = line(11, 2.0);
        let f = [Feature { x: 9.3, y: 0.0, height: 0.07, ramp: 0.5, top: 1.0, level: false }];
        let (a, az, ..) = apply(&xy[..8], &z[..8], &[], &[], &f, 1.5, 0.25);
        let (b, bz, ..) = apply(&xy[3..], &z[3..], &[], &[], &f, 1.5, 0.25);
        for (p, h) in a.iter().zip(&az) {
            if let Some(j) = b.iter().position(|q| (q[0] - p[0]).abs() < 1e-9) {
                assert!((bz[j] - h).abs() < 1e-9);
            }
        }
    }
}
