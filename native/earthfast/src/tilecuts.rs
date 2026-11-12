//! roads.build_tile's per-road cut lists and retaining_walls' deck test, packed once per tile:
//! * road_cut_boxes / inside_road_cuts: every road of the tile as a blocker once (roads._RoadCuts); one road's cut
//!   list is a mask of them with its own half widths. Built per road as Python lists of every other road (8 lists a
//!   road, a numpy view each) and packed again per list (_blocker_segments), they were O(roads^2) a tile, ~3 % of a
//!   16-tile I-83 Baltimore probe's CPU, 2026-11-12. The test is inside_blockers' (the same box, the same distances).
//! * deck_floor: roads._deck_floor, its numpy round per deck chunk (~1.5 % of the same probe), the same numbers
//!   (operation order, numpy's first minimum).

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// Each blocker's box (B, 4) x0 y0 x1 y1 of its segments (segs (m, 4) ax ay bx by, starts (B + 1,)): inside_blockers'
/// own min / max fold (+inf / -inf for a blocker without a segment).
#[pyfunction]
pub fn road_cut_boxes<'py>(py: Python<'py>, segs: PyReadonlyArray2<'py, f64>, starts: PyReadonlyArray1<'py, i64>)
                           -> PyResult<Bound<'py, PyArray2<f64>>> {
    let sg = segs.as_array();
    let st = starts.as_array();
    if sg.ncols() < 4 || st.is_empty() {
        return Err(pyo3::exceptions::PyValueError::new_err("road_cut_boxes: segs (m, 4), starts (B + 1,)"));
    }
    let nb = st.len() - 1;
    let mut out = Array2::<f64>::zeros((nb, 4));
    for b in 0..nb {
        let (s0, s1) = (st[b] as usize, st[b + 1] as usize);
        if s1 > sg.nrows() || s0 > s1 {
            return Err(pyo3::exceptions::PyValueError::new_err("road_cut_boxes: starts out of range"));
        }
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for k in s0..s1 {
            x0 = x0.min(sg[[k, 0]]).min(sg[[k, 2]]);
            x1 = x1.max(sg[[k, 0]]).max(sg[[k, 2]]);
            y0 = y0.min(sg[[k, 1]]).min(sg[[k, 3]]);
            y1 = y1.max(sg[[k, 1]]).max(sg[[k, 3]]);
        }
        out[[b, 0]] = x0;
        out[[b, 1]] = y0;
        out[[b, 2]] = x1;
        out[[b, 3]] = y1;
    }
    Ok(out.into_pyarray(py))
}

/// inside_blockers over the blockers b with keep[b] only, half width hw[b], their boxes (road_cut_boxes) given:
/// which points (n, 2) lie within hw[b] of a segment of a kept blocker.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn inside_road_cuts<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, segs: PyReadonlyArray2<'py, f64>,
                             starts: PyReadonlyArray1<'py, i64>, boxes: PyReadonlyArray2<'py, f64>,
                             keep: PyReadonlyArray1<'py, bool>, hw: PyReadonlyArray1<'py, f64>)
                             -> PyResult<Bound<'py, PyArray1<bool>>> {
    let p = pts.as_array();
    let sg = segs.as_array();
    let st = starts.as_array();
    let bx = boxes.as_array();
    let kp = keep.as_array();
    let h = hw.as_array();
    let nb = h.len();
    if kp.len() != nb || st.len() != nb + 1 || bx.nrows() != nb || bx.ncols() < 4 || p.ncols() < 2
        || (nb > 0 && st[nb] as usize > sg.nrows()) {
        return Err(pyo3::exceptions::PyValueError::new_err("inside_road_cuts: mismatched arrays"));
    }
    let kept: Vec<(usize, f64, f64, f64, f64)> = (0..nb).filter(|&b| kp[b])
        .map(|b| (b, bx[[b, 0]] - h[b], bx[[b, 1]] - h[b], bx[[b, 2]] + h[b], bx[[b, 3]] + h[b])).collect();
    let n = p.nrows();
    let mut out = vec![false; n];
    for i in 0..n {
        let (x, y) = (p[[i, 0]], p[[i, 1]]);
        'blk: for &(b, bx0, by0, bx1, by1) in &kept {
            if x < bx0 || x > bx1 || y < by0 || y > by1 {
                continue;
            }
            for k in (st[b] as usize)..(st[b + 1] as usize) {
                let (ax, ay) = (sg[[k, 0]], sg[[k, 1]]);
                let (dx, dy) = (sg[[k, 2]] - ax, sg[[k, 3]] - ay);
                let l2 = (dx * dx + dy * dy).max(1e-12);
                let t = (((x - ax) * dx + (y - ay) * dy) / l2).clamp(0.0, 1.0);
                if (x - (ax + t * dx)).hypot(y - (ay + t * dy)) < h[b] {
                    out[i] = true;
                    break 'blk;
                }
            }
        }
    }
    Ok(out.into_pyarray(py))
}

/// numpy's clip(v, 0, 1): NaN stays NaN.
fn clip01(v: f64) -> f64 {
    if v.is_nan() { v } else { v.max(0.0).min(1.0) }
}

/// roads._deck_floor: per point xy (k, 2) at road heights z (k,), the underside (deck height at its nearest segment
/// less under) of the lowest deck chunk over it: within reach[c] (half its width + WALL_DECK_REACH_M) of that segment
/// and over or more above z; +inf where none. Chunks: points (N, 3) one after another, starts (C + 1,).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn deck_floor<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                       pts: PyReadonlyArray2<'py, f64>, starts: PyReadonlyArray1<'py, i64>,
                       reach: PyReadonlyArray1<'py, f64>, over: f64, under: f64)
                       -> PyResult<Bound<'py, PyArray1<f64>>> {
    let q = xy.as_array();
    let zz = z.as_array();
    let n = pts.as_array();
    let st = starts.as_array();
    let r = reach.as_array();
    let nc = r.len();
    let k = q.nrows();
    if zz.len() != k || st.len() != nc + 1 || q.ncols() < 2 || n.ncols() < 3
        || (nc > 0 && st[nc] as usize > n.nrows()) {
        return Err(pyo3::exceptions::PyValueError::new_err("deck_floor: mismatched arrays"));
    }
    let mut floor = vec![f64::INFINITY; k];
    for c in 0..nc {
        let (s0, s1) = (st[c] as usize, st[c + 1] as usize);
        if s1 < s0 + 2 || k == 0 {
            continue;
        }
        // (a, ab and L2 per segment, as numpy's n[:-1], n[1:] - n[:-1], maximum((ab[:, :2] ** 2).sum(1), 1e-12))
        let segs: Vec<[f64; 6]> = (s0..s1 - 1).map(|j| {
            let (ax, ay, az) = (n[[j, 0]], n[[j, 1]], n[[j, 2]]);
            let (dx, dy) = (n[[j + 1, 0]] - ax, n[[j + 1, 1]] - ay);
            let l2 = dx * dx + dy * dy;
            [ax, ay, az, dx, dy, if l2.is_nan() { l2 } else { l2.max(1e-12) }]
        }).collect();
        let dzs: Vec<f64> = (s0..s1 - 1).map(|j| n[[j + 1, 2]] - n[[j, 2]]).collect();
        for i in 0..k {
            let (x, y) = (q[[i, 0]], q[[i, 1]]);
            let (mut best, mut bd, mut bt) = (usize::MAX, f64::INFINITY, 0.0);
            for (j, s) in segs.iter().enumerate() {
                let t = clip01(((x - s[0]) * s[3] + (y - s[1]) * s[4]) / s[5]);
                let d = (s[0] + t * s[3] - x).hypot(s[1] + t * s[4] - y);
                if d.is_nan() {             // (numpy's argmin: the first NaN)
                    best = j;
                    bd = d;
                    bt = t;
                    break;
                }
                if best == usize::MAX || d < bd {
                    best = j;
                    bd = d;
                    bt = t;
                }
            }
            let zd = segs[best][2] + bt * dzs[best];
            if bd <= r[c] && zd - zz[i] >= over {
                let v = zd - under;
                floor[i] = if v.is_nan() || floor[i].is_nan() { f64::NAN } else { floor[i].min(v) };
            }
        }
    }
    Ok(floor.into_pyarray(py))
}

/// roads.offset_polyline: the polyline pts (k >= 2, D >= 2) moved d m (d (1,): every point; (k,): d[i] at point i) to
/// its left in xy (the other columns kept), with mitred joints (the miter's cos(half angle) clipped to [0.5, 1]: at most
/// 2x), numpy's arithmetic in its order (a numpy round per call: ~1 % of a road tile set's CPU, I-83 2026-11-12).
#[pyfunction]
pub fn offset_polyline<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, d: PyReadonlyArray1<'py, f64>)
                            -> PyResult<Bound<'py, PyArray2<f64>>> {
    let p = pts.as_array();
    let dd = d.as_array();
    let k = p.nrows();
    if p.ncols() < 2 || k < 2 || !(dd.len() == 1 || dd.len() == k) {
        return Err(pyo3::exceptions::PyValueError::new_err("offset_polyline: pts (k >= 2, >= 2), d (1,) or (k,)"));
    }
    // (the unit left normal of each segment: t / max(|t|, 1e-9) turned left)
    let nrm: Vec<(f64, f64)> = (0..k - 1).map(|i| {
        let (tx, ty) = (p[[i + 1, 0]] - p[[i, 0]], p[[i + 1, 1]] - p[[i, 1]]);
        let mut l = tx.hypot(ty);
        if l < 1e-9 {
            l = 1e-9;
        }
        let (tx, ty) = (tx / l, ty / l);
        (-ty, tx)
    }).collect();
    let mut out = p.to_owned();
    for i in 0..k {
        let (vx, vy) = if i == 0 {
            nrm[0]
        } else if i == k - 1 {
            nrm[k - 2]
        } else {
            let (a, b) = (nrm[i - 1], nrm[i]);
            let (mut mx, mut my) = (a.0 + b.0, a.1 + b.1);
            let mut ml = mx.hypot(my);
            if ml < 1e-9 {
                ml = 1e-9;
            }
            mx /= ml;
            my /= ml;
            let c = clip(mx * b.0 + my * b.1, 0.5, 1.0);
            (mx / c, my / c)
        };
        let di = if dd.len() == 1 { dd[0] } else { dd[i] };
        out[[i, 0]] += vx * di;
        out[[i, 1]] += vy * di;
    }
    Ok(out.into_pyarray(py))
}

/// railways' ground memo (point_ground / grade_plans): heights by point key (x, y in cm, int64 pairs), the dict
/// {(x, y): z} it was without a Python tuple and dict lookup per point (~1.6 % of a 16-tile Fells Point probe's CPU,
/// 2026-11-12). Shared by threads: a lock round each call.
#[pyclass(module = "earthfast")]
pub struct PointMemo {
    map: std::sync::Mutex<std::collections::HashMap<(i64, i64), f64>>,
}

#[pymethods]
impl PointMemo {
    #[new]
    fn new() -> Self {
        PointMemo { map: std::sync::Mutex::new(std::collections::HashMap::new()) }
    }

    /// (n,) the heights of keys (n, 2), NaN for a key not kept.
    fn get<'py>(&self, py: Python<'py>, keys: PyReadonlyArray2<'py, i64>) -> PyResult<Bound<'py, PyArray1<f64>>> {
        let k = keys.as_array();
        if k.ncols() < 2 {
            return Err(pyo3::exceptions::PyValueError::new_err("PointMemo.get: keys (n, 2)"));
        }
        let m = self.map.lock().unwrap_or_else(|e| e.into_inner());
        let out: Vec<f64> = (0..k.nrows()).map(|i| *m.get(&(k[[i, 0]], k[[i, 1]])).unwrap_or(&f64::NAN)).collect();
        Ok(out.into_pyarray(py))
    }

    /// keys (n, 2) given heights z (n,), in order (a later one of the same key wins, as a dict's assignments).
    fn put(&self, keys: PyReadonlyArray2<'_, i64>, z: PyReadonlyArray1<'_, f64>) -> PyResult<()> {
        let (k, z) = (keys.as_array(), z.as_array());
        if k.ncols() < 2 || k.nrows() != z.len() {
            return Err(pyo3::exceptions::PyValueError::new_err("PointMemo.put: keys (n, 2), z (n,)"));
        }
        let mut m = self.map.lock().unwrap_or_else(|e| e.into_inner());
        for i in 0..k.nrows() {
            m.insert((k[[i, 0]], k[[i, 1]]), z[i]);
        }
        Ok(())
    }

    fn clear(&self) {
        self.map.lock().unwrap_or_else(|e| e.into_inner()).clear();
    }

    fn __len__(&self) -> usize {
        self.map.lock().unwrap_or_else(|e| e.into_inner()).len()
    }
}

/// buildings.doors' walls against the road segments: for each point p (n, 2) (a wall's middle), its nearest segment
/// k (numpy's argmin of hypot(Q - p): the first minimum, the first NaN), that distance and the nearest point Q on it,
/// with Q = a + clip(((p - a) * d).sum() / l2, 0, 1) * d per segment a (S, 2), d (S, 2), l2 (S,) (the numpy's
/// numbers; its (walls, segments) arrays per building were ~1.5 % of a road tile set's CPU, Fells Point 2026-11-12).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn nearest_segments<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, a: PyReadonlyArray2<'py, f64>,
                             d: PyReadonlyArray2<'py, f64>, l2: PyReadonlyArray1<'py, f64>)
                             -> PyResult<(Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray2<f64>>)> {
    let (p, a, d, l2) = (pts.as_array(), a.as_array(), d.as_array(), l2.as_array());
    let s = l2.len();
    if p.ncols() < 2 || a.ncols() < 2 || d.ncols() < 2 || a.nrows() != s || d.nrows() != s || s == 0 {
        return Err(pyo3::exceptions::PyValueError::new_err("nearest_segments: pts (n, 2), a, d (S >= 1, 2), l2 (S,)"));
    }
    let n = p.nrows();
    let mut kk = vec![0i64; n];
    let mut dd = vec![0.0f64; n];
    let mut q = Array2::<f64>::zeros((n, 2));
    for i in 0..n {
        let (x, y) = (p[[i, 0]], p[[i, 1]]);
        let (mut best, mut bd, mut bq) = (usize::MAX, 0.0, (0.0, 0.0));
        for j in 0..s {
            let t = clip(((x - a[[j, 0]]) * d[[j, 0]] + (y - a[[j, 1]]) * d[[j, 1]]) / l2[j], 0.0, 1.0);
            let (qx, qy) = (a[[j, 0]] + t * d[[j, 0]], a[[j, 1]] + t * d[[j, 1]]);
            let dist = (qx - x).hypot(qy - y);
            if dist.is_nan() {
                best = j;
                bd = dist;
                bq = (qx, qy);
                break;
            }
            if best == usize::MAX || dist < bd {
                best = j;
                bd = dist;
                bq = (qx, qy);
            }
        }
        kk[i] = best as i64;
        dd[i] = bd;
        q[[i, 0]] = bq.0;
        q[[i, 1]] = bq.1;
    }
    Ok((kk.into_pyarray(py), dd.into_pyarray(py), q.into_pyarray(py)))
}

/// numpy's clip(v, lo, hi): NaN stays NaN.
fn clip(v: f64, lo: f64, hi: f64) -> f64 {
    if v.is_nan() { v } else { v.max(lo).min(hi) }
}
