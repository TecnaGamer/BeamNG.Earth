//! grading.snap_junctions: a side road's end at a junction takes the height of the road it joins, eased in along the
//! side road (each road is smoothed on its own: a side road met the main road up to a metre off), and (coarse: the
//! BNE_COARSE_EASE crossings, GitHub #414) a road running over a higher-ranked road's carriageway away from its ends
//! takes that road's height there. Higher ranks first: a lower road meets its neighbour's already adjusted height.
//! The numpy version's numbers (its pairwise sums, first minimum, operation order), bit for bit.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// numpy's sum of a float64 array (pairwise: blocks of 8 accumulators up to 128 values, halves above).
fn np_sum(a: &[f64]) -> f64 {
    fn pw(a: &[f64]) -> f64 {
        let n = a.len();
        if n < 8 {
            let mut r = 0.0;
            for &x in a {
                r += x;
            }
            r
        } else if n <= 128 {
            let mut r = [a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7]];
            let mut i = 8;
            while i < n - (n % 8) {
                for j in 0..8 {
                    r[j] += a[i + j];
                }
                i += 8;
            }
            let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
            while i < n {
                res += a[i];
                i += 1;
            }
            res
        } else {
            let mut n2 = n / 2;
            n2 -= n2 % 8;
            pw(&a[..n2]) + pw(&a[n2..])
        }
    }
    0.0 + pw(a)
}

/// The eased weight of the change at u (0 at the end, 1 TAPER away): 0.5 (1 + cos(pi u)) below 1, else 0.
fn ease(u: f64) -> f64 {
    if u < 1.0 {
        0.5 * (1.0 + (std::f64::consts::PI * u.clamp(0.0, 1.0)).cos())
    } else {
        0.0
    }
}

/// (distance, height, arc position from the nearer end) of the point of polyline xy / z nearest (x, y)
/// (grading._nearest_on).
fn nearest_on(xy: &[[f64; 2]], z: &[f64], x: f64, y: f64) -> (f64, f64, f64) {
    let m = xy.len() - 1;
    let mut seg = Vec::with_capacity(m);
    let (mut bi, mut bd, mut bt) = (0usize, f64::INFINITY, 0.0);
    for i in 0..m {
        let (a, b) = (xy[i], xy[i + 1]);
        let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
        let ll = (0.0 + dx * dx + dy * dy).max(1e-12);
        let t = (((x - a[0]) * dx + (y - a[1]) * dy) / ll).clamp(0.0, 1.0);
        let (qx, qy) = (a[0] + dx * t, a[1] + dy * t);
        let d = (qx - x).hypot(qy - y);
        if d < bd || (i == 0 && d.is_nan()) {
            bi = i;
            bd = d;
            bt = t;
        }
        seg.push(ll.sqrt());
    }
    let s = np_sum(&seg[..bi]) + seg[bi] * bt;
    (bd, z[bi] + (z[bi + 1] - z[bi]) * bt, s.min(np_sum(&seg) - s))
}

/// New heights for the plans' vertices. xy (n, 2), z (n): every plan's vertices one after another, plan j's from
/// off[j] to off[j + 1]; rank: its road rank; half: its carriageway's half width; abut (plans, 2): an end that is a
/// bridge abutment (left as it is); bbox (plans, 4): its x0, y0, x1, y1. reach, end_m, taper, max_m: grading's
/// JUNCTION_* constants; coarse: the crossings too (COARSE_EASE_ON). Returns (z, changed per plan). Pure.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn snap_junctions<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>,
    z: PyReadonlyArray1<'py, f64>,
    off: PyReadonlyArray1<'py, i64>,
    rank: PyReadonlyArray1<'py, i64>,
    half: PyReadonlyArray1<'py, f64>,
    abut: PyReadonlyArray2<'py, u8>,
    bbox: PyReadonlyArray2<'py, f64>,
    reach: f64,
    end_m: f64,
    taper_m: f64,
    max_m: f64,
    coarse: bool,
) -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<u8>>)> {
    let xa = xy.as_array();
    let n = xa.nrows();
    let pts: Vec<[f64; 2]> = (0..n).map(|i| [xa[[i, 0]], xa[[i, 1]]]).collect();
    let mut zz: Vec<f64> = z.as_array().to_vec();
    let off: Vec<usize> = off.as_array().iter().map(|&v| v.max(0) as usize).collect();
    let rank: Vec<i64> = rank.as_array().to_vec();
    let half: Vec<f64> = half.as_array().to_vec();
    let ab = abut.as_array();
    let bb = bbox.as_array();
    let np = rank.len();
    if off.len() != np + 1 || zz.len() != n || half.len() != np || ab.nrows() != np || bb.nrows() != np
        || off[np] > n
    {
        return Err(pyo3::exceptions::PyValueError::new_err("snap_junctions: array sizes differ"));
    }
    let boxes: Vec<[f64; 4]> = (0..np).map(|k| [bb[[k, 0]], bb[[k, 1]], bb[[k, 2]], bb[[k, 3]]]).collect();
    let mut order: Vec<usize> = (0..np).collect();
    order.sort_by_key(|&j| -rank[j]); // (stable, as Python's sorted)
    let mut changed = vec![0u8; np];
    for &j in &order {
        let (o0, o1) = (off[j], off[j + 1]);
        let len = o1 - o0;
        if len < 2 {
            continue;
        }
        let p = &pts[o0..o1];
        let mut s = vec![0.0; len];
        for i in 1..len {
            s[i] = s[i - 1] + (p[i][0] - p[i - 1][0]).hypot(p[i][1] - p[i - 1][1]);
        }
        let length = s[len - 1];
        let mut dz = vec![0.0; len];
        for end in 0..2 {
            if ab[[j, end]] != 0 {
                continue; // a bridge abutment's end: the deck's height
            }
            let e = if end == 0 { 0 } else { len - 1 };
            let (x, y) = (p[e][0], p[e][1]);
            let mut best: Option<(f64, f64)> = None;
            for k in 0..np {
                let b = boxes[k];
                if k == j || rank[k] < rank[j] || !(b[0] <= x && x <= b[2] && b[1] <= y && y <= b[3]) {
                    continue;
                }
                let (q0, q1) = (off[k], off[k + 1]);
                if q1 - q0 < 2 {
                    continue; // (numpy: an empty argmin raised; plans have 2 points or more)
                }
                let (dist, zq, from_end) = nearest_on(&pts[q0..q1], &zz[q0..q1], x, y);
                if dist > half[k] + reach {
                    continue;
                }
                if rank[k] == rank[j] && from_end < end_m {
                    continue; // the same class end to end: a road's continuation, not a junction
                }
                if best.map_or(true, |bst| dist < bst.0) {
                    best = Some((dist, zq));
                }
            }
            let Some((_, zq)) = best else { continue };
            let delta = zq - zz[o0 + e];
            if delta.abs() > max_m {
                continue; // a ramp up to an overpass, or bad data: as it was
            }
            let taper = taper_m.min(0.5 * length).max(1e-6);
            for i in 0..len {
                let u = (if end == 0 { s[i] } else { length - s[i] }) / taper;
                dz[i] += delta * ease(u);
            }
        }
        if coarse {
            let pb = boxes[j];
            for k in 0..np {
                let b = boxes[k];
                if !(rank[k] > rank[j] && b[0] <= pb[2] && b[2] >= pb[0] && b[1] <= pb[3] && b[3] >= pb[1]) {
                    continue;
                }
                let (q0, q1) = (off[k], off[k + 1]);
                if q1 - q0 < 2 {
                    continue;
                }
                let q = &pts[q0..q1];
                let qz = &zz[q0..q1];
                let inner: Vec<usize> = (0..len).filter(|&i| s[i] > end_m && s[i] < length - end_m).collect();
                if inner.is_empty() {
                    continue;
                }
                // per inner point: its nearest segment of the other road (first minimum), and the nearest hit overall
                let mut pick: Option<(f64, usize, usize, f64)> = None; // (dmin, h, segment, t)
                for (h, &i) in inner.iter().enumerate() {
                    let (px, py) = (p[i][0], p[i][1]);
                    let (mut sd, mut ss, mut st) = (f64::INFINITY, 0usize, 0.0);
                    for g in 0..q.len() - 1 {
                        let (a, bq) = (q[g], q[g + 1]);
                        let (dx, dy) = (bq[0] - a[0], bq[1] - a[1]);
                        let ll = (0.0 + dx * dx + dy * dy).max(1e-12);
                        let t = (((px - a[0]) * dx + (py - a[1]) * dy) / ll).clamp(0.0, 1.0);
                        let d = (a[0] + t * dx - px).hypot(a[1] + t * dy - py);
                        if d < sd || (g == 0 && d.is_nan()) {
                            sd = d;
                            ss = g;
                            st = t;
                        }
                    }
                    if sd <= half[k] && pick.map_or(true, |pk| sd < pk.0) {
                        pick = Some((sd, h, ss, st));
                    }
                }
                let Some((_, h, sg, t)) = pick else { continue };
                let i_ = inner[h];
                let zq = qz[sg] + (qz[sg + 1] - qz[sg]) * t;
                let delta = zq - (zz[o0 + i_] + dz[i_]);
                if delta.abs() > max_m || delta.abs() < 0.05 {
                    continue;
                }
                let taper = taper_m.min(0.5 * length).max(1e-6);
                for i in 0..len {
                    dz[i] += delta * ease((s[i] - s[i_]).abs() / taper);
                }
            }
        }
        if dz.iter().any(|&v| v != 0.0) {
            for i in 0..len {
                zz[o0 + i] += dz[i];
            }
            changed[j] = 1;
        }
    }
    Ok((zz.into_pyarray(py), changed.into_pyarray(py)))
}
