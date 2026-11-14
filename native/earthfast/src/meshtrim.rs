//! roadmesh.py's small per-ribbon numpy rounds, which a road tile's mesh ran hundreds of times (~1.3 CPU-s of a
//! 16-tile I-83 Baltimore probe, 2026-11-12), with the numpy version's numbers (operation order, first minimum, NaN
//! where numpy has one):
//! - deck_trim_overlaps: roadmesh.trim_overlaps' cut of each deck ribbon's edges where another deck overlaps it in plan
//!   at another height;
//! - keep_up_mask: roadmesh._keep_up's triangles facing the ribbon's way;
//! - shared_points: roadmesh._shared's equal points (position and uv to 0.1 mm) made one.
use crate::dm::Dm;

use numpy::ndarray::Array1;
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

fn nmax(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else { a.max(b) }
}

fn nmin(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else { a.min(b) }
}

/// np.min / np.max of values (NaN if any is; +/- inf when empty, never asked).
fn amin(v: impl Iterator<Item = f64>) -> f64 {
    v.fold(f64::INFINITY, nmin)
}

fn amax(v: impl Iterator<Item = f64>) -> f64 {
    v.fold(f64::NEG_INFINITY, nmax)
}

type Trim<'py> = (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>);

/// roadmesh.trim_overlaps' cuts: the ribbons (left, right (N, 2) and z (N,) one after another, off (R + 1)), each
/// with its owner (R,) (equal: one road), its shared group (R,) (roadmesh's `shared` dict's value, -1 for none) and
/// whether its owner is in it (R,); targets (T,) the ribbons trimmed (indices). params: TRIM_DZ low, high, TRIM_GAP_M,
/// and whether `shared` is given (non-empty). Returns (tl, tr): each target's left and right cut per point, one after
/// another (before the 0.9 half-width cap).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn deck_trim_overlaps<'py>(
    py: Python<'py>,
    left: PyReadonlyArray2<'py, f64>,
    right: PyReadonlyArray2<'py, f64>,
    z: PyReadonlyArray1<'py, f64>,
    off: PyReadonlyArray1<'py, i64>,
    owner: PyReadonlyArray1<'py, i64>,
    group: PyReadonlyArray1<'py, i64>,
    in_shared: PyReadonlyArray1<'py, bool>,
    targets: PyReadonlyArray1<'py, i64>,
    params: PyReadonlyArray1<'py, f64>,
) -> PyResult<Trim<'py>> {
    let bad = |m: &str| pyo3::exceptions::PyValueError::new_err(format!("deck_trim_overlaps: {m}"));
    let (l, r, z, off) = (left.as_array(), right.as_array(), z.as_array(), off.as_array());
    let (owner, group, in_shared, targets) = (owner.as_array(), group.as_array(), in_shared.as_array(),
                                              targets.as_array());
    let pr = params.as_slice().map_err(|_| bad("params must be contiguous"))?;
    if pr.len() != 4 {
        return Err(bad("params needs 4 values"));
    }
    let (dz_lo, dz_hi, gap, shared) = (pr[0], pr[1], pr[2], pr[3] != 0.0);
    let nr = off.len().saturating_sub(1);
    let np_ = l.nrows();
    if r.nrows() != np_ || z.len() != np_ || l.ncols() < 2 || r.ncols() < 2 || owner.len() != nr || group.len() != nr
        || in_shared.len() != nr {
        return Err(bad("array sizes"));
    }
    for k in 0..nr {
        if off[k] < 0 || off[k + 1] < off[k] || off[k + 1] as usize > np_ {
            return Err(bad("bad off"));
        }
    }
    // every ribbon's centre line and half width (numpy: (left + right) / 2, hypot(*(left - right).T) / 2)
    let mid: Vec<[f64; 2]> = (0..np_).map(|k| [(l[[k, 0]] + r[[k, 0]]) / 2.0, (l[[k, 1]] + r[[k, 1]]) / 2.0]).collect();
    let hw: Vec<f64> = (0..np_).map(|k| (l[[k, 0]] - r[[k, 0]]).dhypot(l[[k, 1]] - r[[k, 1]]) / 2.0).collect();
    let span = |k: usize| (off[k] as usize, off[k + 1] as usize);
    // each ribbon's box (om.min(0), om.max(0))
    let boxes: Vec<[f64; 4]> = (0..nr)
        .map(|k| {
            let (a, b) = span(k);
            [amin(mid[a..b].iter().map(|p| p[0])), amin(mid[a..b].iter().map(|p| p[1])),
             amax(mid[a..b].iter().map(|p| p[0])), amax(mid[a..b].iter().map(|p| p[1]))]
        })
        .collect();
    let (mut tl_out, mut tr_out) = (Vec::new(), Vec::new());
    for &t in targets.iter() {
        if t < 0 || t as usize >= nr {
            return Err(bad("bad target"));
        }
        let own = t as usize;
        let (a0, b0) = span(own);
        let n = b0 - a0;
        let m = &mid[a0..b0];
        let h = &hw[a0..b0];
        let u: Vec<[f64; 2]> = (a0..b0)
            .map(|k| {
                let den = nmax(2.0 * hw[k], 1e-9);
                [(l[[k, 0]] - r[[k, 0]]) / den, (l[[k, 1]] - r[[k, 1]]) / den]
            })
            .collect();
        let (mut tl, mut tr) = (vec![0.0f64; n], vec![0.0f64; n]);
        if n > 0 {
            let hmax = amax(h.iter().copied());
            let lo = [boxes[own][0] - hmax - 30.0, boxes[own][1] - hmax - 30.0];
            let hi = [boxes[own][2] + hmax + 30.0, boxes[own][3] + hmax + 30.0];
            for o in 0..nr {
                let (oa, ob) = span(o);
                if owner[o] == owner[own] || ob - oa < 2 {
                    continue;
                }
                if shared && in_shared[own] && group[o] == group[own] {
                    continue;
                }
                let bx = boxes[o];
                if bx[2] < lo[0] || bx[3] < lo[1] || bx[0] > hi[0] || bx[1] > hi[1] {
                    continue;
                }
                let om = &mid[oa..ob];
                let (oz, ohw) = (&z.as_slice().ok_or_else(|| bad("z must be contiguous"))?[oa..ob], &hw[oa..ob]);
                let segs: Vec<([f64; 2], [f64; 2], f64)> = om
                    .windows(2)
                    .map(|w| {
                        let d = [w[1][0] - w[0][0], w[1][1] - w[0][1]];
                        (w[0], d, nmax(d[0] * d[0] + d[1] * d[1], 1e-12))
                    })
                    .collect();
                for (i, p) in m.iter().enumerate() {
                    // the nearest piece (np.argmin: the first of the smallest, a NaN at once)
                    let (mut j, mut best, mut bt, mut bq) = (0usize, f64::NAN, 0.0, [0.0, 0.0]);
                    for (s, &(a, d, l2)) in segs.iter().enumerate() {
                        let tt = nmin(nmax(((p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1]) / l2, 0.0), 1.0);
                        let q = [a[0] + tt * d[0], a[1] + tt * d[1]];
                        let dist = (q[0] - p[0]).dhypot(q[1] - p[1]);
                        if s == 0 || dist < best || (dist.is_nan() && !best.is_nan()) {
                            j = s;
                            best = dist;
                            bt = tt;
                            bq = q;
                            if dist.is_nan() {
                                break;
                            }
                        }
                    }
                    let j1 = (j + 1).min(oz.len() - 1);
                    let zo = oz[j] + bt * (oz[j1] - oz[j]);
                    let wo = ohw[j] + bt * (ohw[j1] - ohw[j]);
                    let dz = (zo - z[a0 + i]).abs();
                    let hit = best < h[i] + wo && dz >= dz_lo && dz < dz_hi && best > 1e-3;
                    if !hit {
                        continue;
                    }
                    let cut = nmax(h[i] - (best / 2.0 - gap), 0.0);
                    let on_left = (bq[0] - p[0]) * u[i][0] + (bq[1] - p[1]) * u[i][1] > 0.0;
                    if on_left {
                        tl[i] = nmax(tl[i], cut);
                    } else {
                        tr[i] = nmax(tr[i], cut);
                    }
                }
            }
        }
        tl_out.extend(tl);
        tr_out.extend(tr);
    }
    Ok((Array1::from_vec(tl_out).into_pyarray(py), Array1::from_vec(tr_out).into_pyarray(py)))
}

/// roadmesh._keep_up's mask: the triangles tri (m, 3) (points xy (n, 2), indices less base) whose turn z (e1 x e2)
/// times the sign of their median (1 where the median is 0) is over 1e-6.
#[pyfunction]
pub fn keep_up_mask<'py>(
    py: Python<'py>,
    tri: PyReadonlyArray2<'py, i64>,
    xy: PyReadonlyArray2<'py, f64>,
    base: i64,
) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let (tri, xy) = (tri.as_array(), xy.as_array());
    let bad = |m: &str| pyo3::exceptions::PyValueError::new_err(format!("keep_up_mask: {m}"));
    if tri.ncols() != 3 || xy.ncols() < 2 {
        return Err(bad("tri (m, 3), xy (n, 2)"));
    }
    let n = xy.nrows() as i64;
    let mut cz = Vec::with_capacity(tri.nrows());
    for t in tri.rows() {
        let idx = [t[0] - base, t[1] - base, t[2] - base];
        if idx.iter().any(|&v| v < -n || v >= n) {
            return Err(bad("index out of range"));
        }
        // (numpy's negative indices count from the end)
        let at = |v: i64| if v < 0 { (v + n) as usize } else { v as usize };
        let (p0, p1, p2) = (at(idx[0]), at(idx[1]), at(idx[2]));
        let e1 = [xy[[p1, 0]] - xy[[p0, 0]], xy[[p1, 1]] - xy[[p0, 1]]];
        let e2 = [xy[[p2, 0]] - xy[[p0, 0]], xy[[p2, 1]] - xy[[p0, 1]]];
        cz.push(e1[0] * e2[1] - e1[1] * e2[0]);
    }
    // np.median: NaN if any is; the middle value, or the mean of the middle two
    let med = if cz.is_empty() {
        f64::NAN
    } else if cz.iter().any(|v| v.is_nan()) {
        f64::NAN
    } else {
        let mut s = cz.clone();
        s.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let k = s.len();
        if k % 2 == 1 { s[k / 2] } else { (s[k / 2 - 1] + s[k / 2]) / 2.0 }
    };
    let m = if med == 0.0 { 1.0 } else { med };       // (`np.median(cz) or 1.0`: a NaN is true)
    let sign = if m.is_nan() { f64::NAN } else if m > 0.0 { 1.0 } else if m < 0.0 { -1.0 } else { 0.0 };
    let out: Vec<bool> = cz.iter().map(|&c| c * sign > 1e-6).collect();
    Ok(Array1::from_vec(out).into_pyarray(py))
}

type Shared<'py> = (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>);

/// roadmesh._shared's index: the rows of key (n, c) (the points' rounded position and uv), as np.unique(key, axis=0,
/// return_index=True, return_inverse=True) gives them: (first (u,) the first row of each distinct key in the keys'
/// sorted order, inverse (n,) each row's distinct key).
#[pyfunction]
pub fn shared_points<'py>(py: Python<'py>, key: PyReadonlyArray2<'py, i64>) -> PyResult<Shared<'py>> {
    let k = key.as_array();
    let (n, c) = (k.nrows(), k.ncols());
    let rows: Vec<Vec<i64>> = (0..n).map(|i| (0..c).map(|j| k[[i, j]]).collect()).collect();
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&a, &b| rows[a].cmp(&rows[b]));     // (stable: equal rows keep their order, the first first)
    let mut first = Vec::new();
    let mut inv = vec![0i64; n];
    for (pos, &i) in order.iter().enumerate() {
        if pos == 0 || rows[i] != rows[order[pos - 1]] {
            first.push(i as i64);
        }
        inv[i] = first.len() as i64 - 1;
    }
    Ok((Array1::from_vec(first).into_pyarray(py), Array1::from_vec(inv).into_pyarray(py)))
}
