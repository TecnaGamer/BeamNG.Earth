//! The bus and railway stations' small geometry (server busstations.contains / dist_to_path, railstations._crossings):
//! a station group's layout called them thousands of times on numpy rows of a few points each, ~3 CPU-s of a Dijon
//! group per process (2026-11-11). The numbers are the Python steps' to the bit, in their order: even-odd crossings
//! with the same expression, numpy's distance to a polyline (squares summed, np.maximum, np.clip, libm hypot, the
//! first minimum or NaN), the track crossings' parameters and arc lengths (hypot per segment, summed in order) sorted
//! stably by arc.
use crate::dm::Dm;
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// busstations._open: the ring's rows without a closing copy of its first vertex (within 1e-6 on both axes).
fn open_len(r: &numpy::ndarray::ArrayView2<f64>) -> usize {
    let n = r.nrows();
    if n >= 2 {
        let d = (r[[0, 0]] - r[[n - 1, 0]]).abs().max((r[[0, 1]] - r[[n - 1, 1]]).abs());
        // (float(np.max(np.abs(...))) < 1e-6: np.max of a NaN is NaN, and NaN < 1e-6 is false)
        let d = if (r[[0, 0]] - r[[n - 1, 0]]).is_nan() || (r[[0, 1]] - r[[n - 1, 1]]).is_nan() { f64::NAN } else { d };
        if d < 1e-6 {
            return n - 1;
        }
    }
    n
}

/// busstations.centroid's area centroid of a ring xy (k, c >= 2): opened (its last row dropped when every column is
/// within 1e-6 of the first's), then with 3 or more points and an area over 1e-6 the shoelace centroid, numpy's
/// pairwise sums of cr = x * roll(y, -1) - roll(x, -1) * y; None where the caller takes the mean (kernel 235: ~35,000
/// numpy centroids of a station group's items, 2.7 % of an I-83 Baltimore road probe, 2026-11-12).
#[pyfunction]
pub fn ring_centroid(xy: PyReadonlyArray2<'_, f64>) -> PyResult<Option<(f64, f64)>> {
    let r = xy.as_array();
    let (mut n, c) = (r.nrows(), r.ncols());
    if c < 2 {
        return Ok(None);
    }
    if n >= 2 {
        let mut d = f64::NEG_INFINITY;
        for j in 0..c {
            let v = (r[[0, j]] - r[[n - 1, j]]).abs();
            d = if d.is_nan() || v.is_nan() { f64::NAN } else { d.max(v) };
        }
        if d < 1e-6 {
            n -= 1;
        }
    }
    if n < 3 {
        return Ok(None);
    }
    let nx = |i: usize| (i + 1) % n;
    let cr: Vec<f64> = (0..n).map(|i| r[[i, 0]] * r[[nx(i), 1]] - r[[nx(i), 0]] * r[[i, 1]]).collect();
    let a = crate::junctionsnap::np_sum(&cr) * 0.5;
    if !(a.abs() > 1e-6) {
        return Ok(None);
    }
    let sx: Vec<f64> = (0..n).map(|i| (r[[i, 0]] + r[[nx(i), 0]]) * cr[i]).collect();
    let sy: Vec<f64> = (0..n).map(|i| (r[[i, 1]] + r[[nx(i), 1]]) * cr[i]).collect();
    Ok(Some((crate::junctionsnap::np_sum(&sx) / (6.0 * a), crate::junctionsnap::np_sum(&sy) / (6.0 * a))))
}

/// busstations.contains(ring, p): p inside the polygon (even-odd), ring opened first.
fn contains(r: &numpy::ndarray::ArrayView2<f64>, n: usize, x: f64, y: f64) -> bool {
    let mut inside = false;
    if n == 0 {
        return false;
    }
    let mut j = n - 1;
    for i in 0..n {
        let (xi, yi, xj, yj) = (r[[i, 0]], r[[i, 1]], r[[j, 0]], r[[j, 1]]);
        if (yi > y) != (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi {
            inside = !inside;
        }
        j = i;
    }
    inside
}

/// [busstations.contains(ring, p) for p in pts] (pts (k, 2)).
#[pyfunction]
pub fn station_contains<'py>(py: Python<'py>, ring: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>)
                             -> Bound<'py, PyArray1<bool>> {
    let (r, p) = (ring.as_array(), pts.as_array());
    let n = open_len(&r);
    let out: Vec<bool> = (0..p.nrows()).map(|k| contains(&r, n, p[[k, 0]], p[[k, 1]])).collect();
    out.into_pyarray(py)
}

/// np.clip(x, 0, 1) (NaN through).
fn clip01(x: f64) -> f64 {
    if x.is_nan() { x } else if x < 0.0 { 0.0 } else if x > 1.0 { 1.0 } else { x }
}

/// busstations.dist_to_path(path, p, closed) for one point, path's rows (closed: its opened rows and the first again).
fn dist(px: &[f64], py_: &[f64], x: f64, y: f64) -> f64 {
    let n = px.len();
    if n == 1 {
        return (px[0] - x).dhypot(py_[0] - y);
    }
    let mut best = f64::NAN;
    for k in 0..n - 1 {
        let (ax, ay) = (px[k], py_[k]);
        let (abx, aby) = (px[k + 1] - ax, py_[k + 1] - ay);
        let s2 = abx * abx + aby * aby;
        let ab2 = if s2.is_nan() || s2 > 1e-9 { s2 } else { 1e-9 };     // (np.maximum: NaN through)
        let t = clip01(((x - ax) * abx + (y - ay) * aby) / ab2);
        let d = (ax + t * abx - x).dhypot(ay + t * aby - y);
        if k == 0 || d.is_nan() || d < best {
            best = d;
            if d.is_nan() {
                return d;       // (.min(): NaN wins)
            }
        }
    }
    best
}

/// [busstations.dist_to_path(path, p, closed) for p in pts] (pts (k, 2)); an empty path gives NaN (numpy raised).
#[pyfunction]
pub fn station_path_dist<'py>(py: Python<'py>, path: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>,
                              closed: bool) -> Bound<'py, PyArray1<f64>> {
    let (a, p) = (path.as_array(), pts.as_array());
    let n = if closed { open_len(&a) } else { a.nrows() };
    let mut xs: Vec<f64> = (0..n).map(|i| a[[i, 0]]).collect();
    let mut ys: Vec<f64> = (0..n).map(|i| a[[i, 1]]).collect();
    if closed && n > 0 {
        xs.push(a[[0, 0]]);
        ys.push(a[[0, 1]]);
    }
    let out: Vec<f64> = (0..p.nrows())
        .map(|k| if xs.is_empty() { f64::NAN } else { dist(&xs, &ys, p[[k, 0]], p[[k, 1]]) })
        .collect();
    out.into_pyarray(py)
}

/// railstations._crossings(path, tracks): [(track index, arc length m along path)] where the path crosses a track, in
/// order of arc (stable: path segment, then track, then track segment). tracks: their rows one after another (m, 2),
/// track k's rows off[k]..off[k + 1].
#[pyfunction]
pub fn station_crossings(path: PyReadonlyArray2<'_, f64>, tracks: PyReadonlyArray2<'_, f64>,
                         off: PyReadonlyArray1<'_, i64>) -> Vec<(usize, f64)> {
    let (p, t, o) = (path.as_array(), tracks.as_array(), off.as_array());
    let nt = o.len().saturating_sub(1);
    let mut out: Vec<(usize, f64)> = Vec::new();
    let mut s0 = 0.0f64;
    for i in 0..p.nrows().saturating_sub(1) {
        let (a0x, a0y, a1x, a1y) = (p[[i, 0]], p[[i, 1]], p[[i + 1, 0]], p[[i + 1, 1]]);
        let seglen = (a1x - a0x).dhypot(a1y - a0y);
        let (rx, ry) = (a1x - a0x, a1y - a0y);
        for k in 0..nt {
            let (j0, j1) = (o[k] as usize, o[k + 1] as usize);
            for j in j0..j1.saturating_sub(1) {
                let (b0x, b0y) = (t[[j, 0]], t[[j, 1]]);
                let (sx, sy) = (t[[j + 1, 0]] - b0x, t[[j + 1, 1]] - b0y);
                let den = rx * sy - ry * sx;
                if den.abs() < 1e-9 || den.is_nan() {
                    continue;   // (abs(den) < 1e-9: None; a NaN den gives NaN t, u: not in [0, 1])
                }
                let (qx, qy) = (b0x - a0x, b0y - a0y);
                let tt = (qx * sy - qy * sx) / den;
                let uu = (qx * ry - qy * rx) / den;
                if (0.0..=1.0).contains(&tt) && (0.0..=1.0).contains(&uu) {
                    out.push((k, s0 + tt * seglen));
                }
            }
        }
        s0 += seglen;
    }
    // (sorted(..., key=arc): stable; arcs of a NaN path keep their order as Python's comparisons would not move them)
    out.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));
    out
}

/// builtin max(h, 1e-9) of the Python (a NaN h stays: 1e-9 > NaN is false).
fn max_eps(h: f64) -> f64 {
    if 1e-9 > h { 1e-9 } else { h }
}

/// One segment's (distance, foot x, foot y) from point (x, y): the Python's numpy steps (np.clip of the projection
/// over np.maximum(|ab|^2, 1e-9), the foot a + ab * t, libm hypot).
#[inline]
fn seg_foot(ax: f64, ay: f64, abx: f64, aby: f64, x: f64, y: f64) -> (f64, f64, f64) {
    let s2 = abx * abx + aby * aby;
    let ab2 = if s2.is_nan() || s2 > 1e-9 { s2 } else { 1e-9 };
    let t = clip01(((x - ax) * abx + (y - ay) * aby) / ab2);
    let (fx, fy) = (ax + abx * t, ay + aby * t);
    ((fx - x).dhypot(fy - y), fx, fy)
}

/// np.argmin's choice between the best so far (best_j usize::MAX: none) and segment j: the first NaN, else the first
/// smallest in segment order.
#[inline]
fn better(d: f64, j: usize, best_d: f64, best_j: usize) -> bool {
    if best_j == usize::MAX {
        return true;
    }
    if best_d.is_nan() {
        return d.is_nan() && j < best_j;
    }
    d.is_nan() || d < best_d || (d == best_d && j < best_j)
}

/// The tracks' segments (their rows one after another, track k's off[k]..off[k + 1]) in a grid of cells, numbered in
/// the order the Python's concatenation had them (track, then segment): a point asks only the cells within reach.
struct SegGrid {
    ax: Vec<f64>,
    ay: Vec<f64>,
    bx: Vec<f64>,
    by: Vec<f64>,
    track: Vec<usize>,
    x0: f64,
    y0: f64,
    nx: i64,
    ny: i64,
    cells: Vec<Vec<u32>>,
    big: Vec<u32>,
}

const SEG_CELL_M: f64 = 8.0;
const SEG_BIG_CELLS: i64 = 4096;   // (a segment over more cells than this, or with a NaN, is asked by every point)

impl SegGrid {
    fn new(t: &numpy::ndarray::ArrayView2<f64>, o: &numpy::ndarray::ArrayView1<i64>) -> SegGrid {
        let nt = o.len().saturating_sub(1);
        let (mut ax, mut ay, mut bx, mut by, mut track) = (vec![], vec![], vec![], vec![], vec![]);
        for k in 0..nt {
            let (j0, j1) = (o[k] as usize, o[k + 1] as usize);
            for j in j0..j1.saturating_sub(1) {
                ax.push(t[[j, 0]]);
                ay.push(t[[j, 1]]);
                bx.push(t[[j + 1, 0]]);
                by.push(t[[j + 1, 1]]);
                track.push(k);
            }
        }
        let (mut lx, mut ly, mut hx, mut hy) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for s in 0..ax.len() {
            for (x, y) in [(ax[s], ay[s]), (bx[s], by[s])] {
                if x.is_finite() && y.is_finite() {
                    lx = lx.min(x);
                    ly = ly.min(y);
                    hx = hx.max(x);
                    hy = hy.max(y);
                }
            }
        }
        let (nx, ny) = if lx.is_finite() {
            (((hx - lx) / SEG_CELL_M).floor() as i64 + 1, ((hy - ly) / SEG_CELL_M).floor() as i64 + 1)
        } else {
            (0, 0)
        };
        let mut g = SegGrid { ax, ay, bx, by, track, x0: lx, y0: ly, nx, ny,
                              cells: vec![Vec::new(); (nx * ny) as usize], big: vec![] };
        for s in 0..g.ax.len() {
            let (sx0, sx1) = (g.ax[s].min(g.bx[s]), g.ax[s].max(g.bx[s]));
            let (sy0, sy1) = (g.ay[s].min(g.by[s]), g.ay[s].max(g.by[s]));
            let finite = [g.ax[s], g.bx[s], g.ay[s], g.by[s]].iter().all(|v| v.is_finite());
            if !finite {
                g.big.push(s as u32);
                continue;
            }
            let (cx0, cx1, cy0, cy1) = (g.cx(sx0), g.cx(sx1), g.cy(sy0), g.cy(sy1));
            if (cx1 - cx0 + 1) * (cy1 - cy0 + 1) > SEG_BIG_CELLS {
                g.big.push(s as u32);
                continue;
            }
            for cy in cy0..=cy1 {
                for cx in cx0..=cx1 {
                    let i = (cy * g.nx + cx) as usize;
                    g.cells[i].push(s as u32);
                }
            }
        }
        g
    }

    fn cx(&self, x: f64) -> i64 {
        (((x - self.x0) / SEG_CELL_M).floor() as i64).clamp(0, (self.nx - 1).max(0))
    }

    fn cy(&self, y: f64) -> i64 {
        (((y - self.y0) / SEG_CELL_M).floor() as i64).clamp(0, (self.ny - 1).max(0))
    }

    /// (distance, foot x, y, unit tangent x, y, track) of the nearest segment to (x, y) among the ones in the cells
    /// within reach (+ 1 m): the Python's _Segments.nearest whenever that is within reach (every segment as near is
    /// asked), else None or one farther than reach.
    #[allow(clippy::type_complexity)]
    fn nearest(&self, x: f64, y: f64, reach: f64, stamp: &mut [u32], tick: u32)
               -> Option<(f64, f64, f64, f64, f64, usize)> {
        let mut best = (f64::NAN, 0.0, 0.0, usize::MAX);
        let visit = |s: usize, best: &mut (f64, f64, f64, usize)| {
            let (d, fx, fy) = seg_foot(self.ax[s], self.ay[s], self.bx[s] - self.ax[s], self.by[s] - self.ay[s], x, y);
            if better(d, s, best.0, best.3) {
                *best = (d, fx, fy, s);
            }
        };
        for &s in &self.big {
            visit(s as usize, &mut best);
        }
        if self.nx > 0 && x.is_finite() && y.is_finite() {
            let r = reach + 1.0;
            let (hx, hy) = (self.x0 + self.nx as f64 * SEG_CELL_M, self.y0 + self.ny as f64 * SEG_CELL_M);
            if !(x + r < self.x0 || x - r > hx || y + r < self.y0 || y - r > hy) {
                for cy in self.cy(y - r)..=self.cy(y + r) {
                    for cx in self.cx(x - r)..=self.cx(x + r) {
                        for &s in &self.cells[(cy * self.nx + cx) as usize] {
                            if stamp[s as usize] == tick {
                                continue;
                            }
                            stamp[s as usize] = tick;
                            visit(s as usize, &mut best);
                        }
                    }
                }
            }
        }
        if best.3 == usize::MAX {
            return None;
        }
        let s = best.3;
        let (abx, aby) = (self.bx[s] - self.ax[s], self.by[s] - self.ay[s]);
        let h = max_eps(abx.dhypot(aby));
        Some((best.0, best.1, best.2, abx / h, aby / h, self.track[s]))
    }
}

/// railstations._foot on one track (rows j0..j1 of t) for (x, y): (distance, foot x, y, unit tangent x, y) of its
/// first nearest segment; None without a segment.
fn track_foot(t: &numpy::ndarray::ArrayView2<f64>, j0: usize, j1: usize, x: f64, y: f64)
              -> Option<(f64, f64, f64, f64, f64)> {
    let mut best = (f64::NAN, 0.0, 0.0, usize::MAX);
    for j in j0..j1.saturating_sub(1) {
        let (ax, ay) = (t[[j, 0]], t[[j, 1]]);
        let (d, fx, fy) = seg_foot(ax, ay, t[[j + 1, 0]] - ax, t[[j + 1, 1]] - ay, x, y);
        if better(d, j, best.0, best.3) {
            best = (d, fx, fy, j);
        }
    }
    if best.3 == usize::MAX {
        return None;
    }
    let j = best.3;
    let (abx, aby) = (t[[j + 1, 0]] - t[[j, 0]], t[[j + 1, 1]] - t[[j, 1]]);
    let h = max_eps(abx.dhypot(aby));
    Some((best.0, best.1, best.2, abx / h, aby / h))
}

type SnapOut<'py> = (Bound<'py, numpy::PyArray2<f64>>, Bound<'py, PyArray1<bool>>, Bound<'py, PyArray1<i64>>);

/// railstations.snap_edges(ring, tracks, edge_m) (kernel 263): the platform's boundary resampled every `step` m (the
/// ring opened first; each edge, the closing one too, in ceil(len / step) parts a + (b - a) * (k / n)), each point
/// beside a track (within `reach`, the boundary's direction within acos(cos_min) of the track's) put edge_m from that
/// track's axis on its own side, then twice over the unsnapped corner next to a snapped point beside that point's
/// track, then a point the snap brought within 0.25 m of the previous one dropped. The tracks' segments are in a grid:
/// each point asks the few near it, not every one (the Python's numpy pass per point over all the segments in the
/// platform's box was most of a King's Cross / Euston road geotile's build, 2026-11-12). The numbers are the Python's
/// steps to the bit (np.argmin's first minimum in track, segment order). tracks: their rows one after another (m, 2),
/// track k's rows off[k]..off[k + 1]. -> (ring' (n, 2), snapped, track index or -1)
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn station_snap_edges<'py>(py: Python<'py>, ring: PyReadonlyArray2<'py, f64>, tracks: PyReadonlyArray2<'py, f64>,
                               off: PyReadonlyArray1<'py, i64>, edge_m: f64, reach: f64, cos_min: f64, step: f64)
                               -> PyResult<SnapOut<'py>> {
    let (r, t, o) = (ring.as_array(), tracks.as_array(), off.as_array());
    if r.ncols() < 2 || (t.ncols() < 2 && t.nrows() > 0) {
        return Err(pyo3::exceptions::PyValueError::new_err("station_snap_edges: rows of x, y"));
    }
    if o.iter().any(|&v| v < 0 || v as usize > t.nrows()) {
        return Err(pyo3::exceptions::PyValueError::new_err("station_snap_edges: off outside the track rows"));
    }
    let rn = open_len(&r);
    let (mut px, mut py_) = (Vec::new(), Vec::new());
    for i in 0..rn {
        let (ax, ay) = (r[[i, 0]], r[[i, 1]]);
        let (dx, dy) = (r[[(i + 1) % rn, 0]] - ax, r[[(i + 1) % rn, 1]] - ay);
        let q = (dx.dhypot(dy) / step).ceil();
        if !q.is_finite() {
            return Err(pyo3::exceptions::PyValueError::new_err("station_snap_edges: a ring without a length"));
        }
        let parts = (q as i64).max(1);
        for k in 0..parts {
            let f = k as f64 / parts as f64;
            px.push(ax + dx * f);
            py_.push(ay + dy * f);
        }
    }
    let n = px.len();
    let (mut ox, mut oy) = (px.clone(), py_.clone());
    let mut snapped = vec![false; n];
    let mut tidx = vec![-1i64; n];
    let grid = SegGrid::new(&t, &o);
    let mut stamp = vec![0u32; grid.ax.len()];
    for i in 0..n {
        let Some((d, fx, fy, tx, ty, k)) = grid.nearest(px[i], py_[i], reach, &mut stamp, i as u32 + 1) else {
            continue;
        };
        if d > reach {
            continue;
        }
        let (nxt, prv) = ((i + 1) % n, (i + n - 1) % n);
        let (vx, vy) = (px[nxt] - px[prv], py_[nxt] - py_[prv]);
        let tl = vx.dhypot(vy);
        if tl < 1e-6 || ((vx / tl) * tx + (vy / tl) * ty).abs() < cos_min {
            continue;
        }
        let (wx, wy) = (px[i] - fx, py_[i] - fy);
        let side = if (tx * wy - ty * wx) >= 0.0 { 1.0 } else { -1.0 };
        ox[i] = fx + (-ty * side) * edge_m;
        oy[i] = fy + (tx * side) * edge_m;
        snapped[i] = true;
        tidx[i] = k as i64;
    }
    for _ in 0..2 {
        let done = snapped.clone();
        for i in 0..n {
            if done[i] {
                continue;
            }
            for j in [(i + n - 1) % n, (i + 1) % n] {
                if !done[j] {
                    continue;
                }
                let k = tidx[j] as usize;
                let Some((d, fx, fy, tx, ty)) = track_foot(&t, o[k] as usize, o[k + 1] as usize, px[i], py_[i]) else {
                    continue;
                };
                if !(d < 1e9) || d > reach {
                    continue;       // (_nearest_track's 1e9 start: a NaN is never nearer)
                }
                let (wx, wy) = (px[i] - fx, py_[i] - fy);
                let side = if (tx * wy - ty * wx) >= 0.0 { 1.0 } else { -1.0 };
                ox[i] = fx + (-ty * side) * edge_m;
                oy[i] = fy + (tx * side) * edge_m;
                snapped[i] = true;
                tidx[i] = tidx[j];
                break;
            }
        }
    }
    let mut keep = vec![true; n];
    for i in 0..n {
        let j = (i + 1) % n;
        if keep[i] && (ox[i] - ox[j]).dhypot(oy[i] - oy[j]) < 0.25 {
            keep[if j != 0 { j } else { i }] = false;
        }
    }
    let (mut flat, mut sn, mut ti) = (Vec::new(), Vec::new(), Vec::new());
    for i in (0..n).filter(|&i| keep[i]) {
        flat.push(ox[i]);
        flat.push(oy[i]);
        sn.push(snapped[i]);
        ti.push(tidx[i]);
    }
    let m = sn.len();
    Ok((numpy::ndarray::Array2::from_shape_vec((m, 2), flat).unwrap().into_pyarray(py), sn.into_pyarray(py),
        ti.into_pyarray(py)))
}

/// railstations._links' chains of ways (kernel 263): the marked ways (a footbridge's, an underpass's: off level 0 or
/// a bridge / tunnel span) and every eligible way touching a member end to end, grown to a fixed point (the Python's
/// passes until nothing joins), then split into the groups of members whose ends touch. -> each way's chain number
/// (-1: in none), numbered in order of their first (lowest) way. ends (n, 4): each way's first and last point
/// (x0, y0, x1, y1); two ways touch when the least of the 4 end distances (libm hypot) is within join and none is NaN
/// (np.min of a NaN). The ends are in a hash grid of cells over join (the Python's pairwise numpy mins, again every
/// pass, were ~70 % of a central London station group's layout, 2026-11-12).
#[pyfunction]
pub fn station_link_chains<'py>(py: Python<'py>, ends: PyReadonlyArray2<'py, f64>, eligible: PyReadonlyArray1<'py, bool>,
                                marked: PyReadonlyArray1<'py, bool>, join: f64) -> PyResult<Bound<'py, PyArray1<i64>>> {
    let (e, el, mk) = (ends.as_array(), eligible.as_array(), marked.as_array());
    let n = e.nrows();
    if (e.ncols() < 4 && n > 0) || el.len() != n || mk.len() != n {
        return Err(pyo3::exceptions::PyValueError::new_err("station_link_chains: ends (n, 4), eligible / marked (n)"));
    }
    let touch = |a: usize, b: usize| -> bool {
        let mut m = f64::INFINITY;
        for (ia, ib) in [(0, 0), (0, 2), (2, 0), (2, 2)] {
            let d = (e[[a, ia]] - e[[b, ib]]).dhypot(e[[a, ia + 1]] - e[[b, ib + 1]]);
            if d.is_nan() {
                return false;
            }
            m = m.min(d);
        }
        m <= join
    };
    let cell = join.abs() * 1.5 + 0.5;          // (two ends within join: the same or a neighbouring cell)
    let key = |x: f64, y: f64| -> Option<(i64, i64)> {
        if x.is_finite() && y.is_finite() { Some(((x / cell).floor() as i64, (y / cell).floor() as i64)) } else { None }
    };
    let mut grid: std::collections::HashMap<(i64, i64), Vec<usize>> = std::collections::HashMap::new();
    for w in 0..n {
        for c in [0, 2] {
            if let Some(k) = key(e[[w, c]], e[[w, c + 1]]) {
                let v = grid.entry(k).or_default();
                if v.last() != Some(&w) {
                    v.push(w);
                }
            }
        }
    }
    let near = |w: usize, out: &mut Vec<usize>| {
        out.clear();
        for c in [0, 2] {
            if let Some((kx, ky)) = key(e[[w, c]], e[[w, c + 1]]) {
                for dy in -1..=1 {
                    for dx in -1..=1 {
                        if let Some(v) = grid.get(&(kx + dx, ky + dy)) {
                            out.extend(v.iter().copied().filter(|&o| o != w));
                        }
                    }
                }
            }
        }
        out.sort_unstable();
        out.dedup();
    };
    let mut member: Vec<bool> = (0..n).map(|i| mk[i]).collect();
    let mut stack: Vec<usize> = (0..n).filter(|&i| member[i]).collect();
    let mut buf = Vec::new();
    while let Some(w) = stack.pop() {
        near(w, &mut buf);
        for &o in &buf {
            if !member[o] && el[o] && touch(o, w) {
                member[o] = true;
                stack.push(o);
            }
        }
    }
    let mut label = vec![-1i64; n];
    let mut next = 0i64;
    for s in 0..n {
        if !member[s] || label[s] >= 0 {
            continue;
        }
        label[s] = next;
        let mut st = vec![s];
        while let Some(w) = st.pop() {
            near(w, &mut buf);
            for &o in &buf {
                if member[o] && label[o] < 0 && touch(w, o) {
                    label[o] = next;
                    st.push(o);
                }
            }
        }
        next += 1;
    }
    Ok(label.into_pyarray(py))
}
