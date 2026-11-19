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
/// whether `shared` is given (non-empty) and optionally (310) TRIM_ALONG_COS: another deck's piece counts only where it
/// runs within that cosine of the ribbon's own way (a ramp beside its mainline); one crossing it (a track's deck 4.1 m
/// under the B 254 at Trutzhain) cut both edges to the centre round the crossing point, a hole across the carriageway
/// that a car at 80 km/h fell into. Four values: every piece counts (the version before). Returns (tl, tr): each
/// target's left and right cut per point, one after another (before the 0.9 half-width cap).
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
    if pr.len() != 4 && pr.len() != 5 && pr.len() != 6 {
        return Err(bad("params needs 4, 5 or 6 values"));
    }
    let (dz_lo, dz_hi, gap, shared) = (pr[0], pr[1], pr[2], pr[3] != 0.0);
    let along_cos = if pr.len() >= 5 { pr[4] } else { f64::NEG_INFINITY };
    // (534, road revamp v2 P3d: a sixth value non-zero, roadmesh's coplanar trim: a point whose nearest point on the
    // other deck is that deck's very end lies past it, not over it: no cut (a deck cut halfway toward the end of a deck
    // merging into it left a hole past that end); and the cut line at least at the other's near edge)
    let past_end = pr.len() >= 6 && pr[5] != 0.0;
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
                    if past_end && ((j == 0 && bt <= 0.0) || (j + 1 == segs.len() && bt >= 1.0)) {
                        continue;
                    }
                    let j1 = (j + 1).min(oz.len() - 1);
                    let zo = oz[j] + bt * (oz[j1] - oz[j]);
                    let wo = ohw[j] + bt * (ohw[j1] - ohw[j]);
                    let dz = (zo - z[a0 + i]).abs();
                    let hit = best < h[i] + wo && dz >= dz_lo && dz < dz_hi && best > 1e-3;
                    if !hit {
                        continue;
                    }
                    // a deck crossing this one (its piece far off the ribbon's way) passes over or under it: no gore
                    if along_cos > f64::NEG_INFINITY {
                        let (_, d, l2) = segs[j];
                        let cos = (d[0] * u[i][1] - d[1] * u[i][0]).abs() / l2.sqrt();
                        if cos < along_cos {
                            continue;
                        }
                    }
                    // (534 coplanar mode: the cut line inside both footprints, at least to the other's near edge: a
                    // narrow deck's edge short of the midpoint left a gap to the wide one's, Codex review of P3d)
                    let keep = if past_end { nmax(best / 2.0, best - wo) } else { best / 2.0 - gap };
                    let cut = nmax(h[i] - keep, 0.0);
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

/// One coplanar cut (deck_trim_coplanar): a target row's cut (m in from its edge, >= 0) and its side (true: left)
/// against the ribbon `o`, or None (not a coplanar neighbour there).
#[allow(clippy::too_many_arguments)]
fn cop_cut(p: [f64; 2], h: f64, u: [f64; 2], za: f64, ba: f64, om: &[[f64; 2]], oz: &[f64], ob: &[f64],
           ohw: &[f64], ends: (bool, bool), tol: f64, along_cos: f64, hide_dz: f64) -> Option<(f64, bool)> {
    // the nearest piece (the first of the smallest; its unclamped projection kept)
    let (mut j, mut best, mut bt, mut raw, mut bq) = (usize::MAX, f64::INFINITY, 0.0, 0.0, [0.0, 0.0]);
    for s in 0..om.len().saturating_sub(1) {
        let (a, b) = (om[s], om[s + 1]);
        let d = [b[0] - a[0], b[1] - a[1]];
        let l2 = nmax(d[0] * d[0] + d[1] * d[1], 1e-12);
        let tr = ((p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1]) / l2;
        let tt = tr.clamp(0.0, 1.0);
        let q = [a[0] + tt * d[0], a[1] + tt * d[1]];
        let dist = (q[0] - p[0]).dhypot(q[1] - p[1]);
        if !dist.is_finite() {
            return None;
        }
        if dist < best {
            j = s;
            best = dist;
            bt = tt;
            raw = tr;
            bq = q;
        }
    }
    if j == usize::MAX {
        return None;
    }
    let last = om.len() - 2;
    // (a point strictly past the other deck's own end (its part's, never a tile's or a split's chunk end) lies past it,
    // not beside it; at a chunk end inside its part the next chunk goes on: the cut as the whole line gives it)
    if (j == 0 && raw < 0.0 && ends.0) || (j == last && raw > 1.0 && ends.1) {
        return None;
    }
    let wo = ohw[j] + bt * (ohw[j + 1] - ohw[j]);
    if !(best < h + wo && best > 1e-3) {
        return None;
    }
    let d = [om[j + 1][0] - om[j][0], om[j + 1][1] - om[j][1]];
    let ld = nmax(d[0].dhypot(d[1]), 1e-9);
    let cos = (d[0] * u[1] - d[1] * u[0]).abs() / ld;
    if cos < along_cos || cos <= 0.0 {
        return None;
    }
    // the shared seam: the line both centre lines are equally far from (the bisector), reached along this row (best /
    // (1 + cos): each deck's cut on it whatever their angle; halfway, best / 2, for parallel decks); not short of the
    // other's near edge ((best - wo) / cos): a narrow deck short of the bisector is met at its own edge
    let keep = nmax(best / (1.0 + cos), (best - wo) / cos);
    let on_left = (bq[0] - p[0]) * u[0] + (bq[1] - p[1]) * u[1] > 0.0;
    let sg = if on_left { 1.0 } else { -1.0 };
    let q = [p[0] + sg * keep * u[0], p[1] + sg * keep * u[1]];
    // both surfaces at the seam (each one's centre height, its cross slope x the seam's offset to its left): one
    // surface there within tol, or no cut (two decks apart in height, or banked apart, are not butted into a step)
    let zq_a = za + ba * sg * keep;
    // (the other surface at the seam point's own foot on its line: on angled decks the row centre's foot is another
    // station, its grade a false step; Codex P3e)
    let (mut jq, mut dq, mut tq, mut rq) = (j, f64::INFINITY, bt, bt);
    for s in 0..om.len() - 1 {
        let (a, b) = (om[s], om[s + 1]);
        let e = [b[0] - a[0], b[1] - a[1]];
        let l2 = nmax(e[0] * e[0] + e[1] * e[1], 1e-12);
        let tr = ((q[0] - a[0]) * e[0] + (q[1] - a[1]) * e[1]) / l2;
        let tt = tr.clamp(0.0, 1.0);
        let dist = (a[0] + tt * e[0] - q[0]).dhypot(a[1] + tt * e[1] - q[1]);
        if dist < dq {
            jq = s;
            dq = dist;
            tq = tt;
            rq = tr;
        }
    }
    // (the seam point itself strictly past the other deck's own end: the other cannot cover the cut there; Codex P3e)
    if (jq == 0 && rq < 0.0 && ends.0) || (jq == last && rq > 1.0 && ends.1) {
        return None;
    }
    let fq = [om[jq][0] + tq * (om[jq + 1][0] - om[jq][0]), om[jq][1] + tq * (om[jq + 1][1] - om[jq][1])];
    let eq = [om[jq + 1][0] - om[jq][0], om[jq + 1][1] - om[jq][1]];
    let lq = nmax(eq[0].dhypot(eq[1]), 1e-9);
    let zo = oz[jq] + tq * (oz[jq + 1] - oz[jq]);
    let bo = ob[jq] + tq * (ob[jq + 1] - ob[jq]);
    let off_o = (eq[0] * (q[1] - fq[1]) - eq[1] * (q[0] - fq[0])) / lq;
    let zq_o = zo + bo * off_o;
    let dzs = zq_a - zq_o;
    if (dzs).abs() <= tol {
        return Some((nmax(h - keep, 0.0), on_left));
    }
    // (the lower of two decks apart by less than hide_dz at the seam: cut back to the upper one's near edge only, the
    // part the upper slab hides (it stands over it, its side face deeper than the step): no surface drawn twice, no
    // step made, the upper deck left whole)
    if dzs < 0.0 && -dzs < hide_dz {
        let hide = (best - wo) / cos;
        return Some((nmax(h - hide.max(0.0), 0.0), on_left));
    }
    None
}

type CopTrim<'py> = (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>);

/// roadmesh.trim_overlaps' coplanar cuts (BNE_ROADSHAPE=profile; 536, road revamp v2 P3e): two decks running along
/// each other (within along_cos) whose footprints overlap end on ONE seam, the line both centre lines are equally far
/// from (not each at half the distance along its own normal: angled decks left a ~0.23 m gap, Codex P3d), at least at
/// the other's near edge; only where both surfaces meet at that seam within tol (centre heights and cross slopes:
/// bank); never past the other deck's own end (ends (2R,): its part's start / end is that ribbon's first / last point)
/// but on through a chunk end inside its part. Ribbons as deck_trim_overlaps' (left, right, z, off, owner, group,
/// in_shared, targets) and bank (N,) each point's cross slope (m per m toward its left). params: tol, along_cos,
/// shared (non-zero: a target in a shared group not cut by that group), optionally hide_dz (the lower of two decks
/// apart by less than it at the seam cut back under the upper's near edge only). Returns (tl, tr) per target point.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn deck_trim_coplanar<'py>(
    py: Python<'py>,
    left: PyReadonlyArray2<'py, f64>,
    right: PyReadonlyArray2<'py, f64>,
    z: PyReadonlyArray1<'py, f64>,
    bank: PyReadonlyArray1<'py, f64>,
    off: PyReadonlyArray1<'py, i64>,
    owner: PyReadonlyArray1<'py, i64>,
    group: PyReadonlyArray1<'py, i64>,
    in_shared: PyReadonlyArray1<'py, bool>,
    ends: PyReadonlyArray1<'py, bool>,
    targets: PyReadonlyArray1<'py, i64>,
    params: PyReadonlyArray1<'py, f64>,
) -> PyResult<CopTrim<'py>> {
    let bad = |m: &str| pyo3::exceptions::PyValueError::new_err(format!("deck_trim_coplanar: {m}"));
    let (l, r, off) = (left.as_array(), right.as_array(), off.as_array());
    let z = z.as_slice().map_err(|_| bad("z must be contiguous"))?;
    let bk = bank.as_slice().map_err(|_| bad("bank must be contiguous"))?;
    let (owner, group, in_shared, ends, targets) = (owner.as_array(), group.as_array(), in_shared.as_array(),
                                                    ends.as_array(), targets.as_array());
    let pr = params.as_slice().map_err(|_| bad("params must be contiguous"))?;
    if pr.len() != 3 && pr.len() != 4 {
        return Err(bad("params needs 3 or 4 values"));
    }
    let (tol, along_cos, shared) = (pr[0], pr[1], pr[2] != 0.0);
    let hide_dz = if pr.len() > 3 { pr[3] } else { 0.0 };
    let nr = off.len().saturating_sub(1);
    let np_ = l.nrows();
    if r.nrows() != np_ || z.len() != np_ || bk.len() != np_ || l.ncols() < 2 || r.ncols() < 2 || owner.len() != nr
        || group.len() != nr || in_shared.len() != nr || ends.len() != 2 * nr {
        return Err(bad("array sizes"));
    }
    for k in 0..nr {
        if off[k] < 0 || off[k + 1] < off[k] || off[k + 1] as usize > np_ {
            return Err(bad("bad off"));
        }
    }
    let mid: Vec<[f64; 2]> = (0..np_).map(|k| [(l[[k, 0]] + r[[k, 0]]) / 2.0, (l[[k, 1]] + r[[k, 1]]) / 2.0]).collect();
    let hw: Vec<f64> = (0..np_).map(|k| (l[[k, 0]] - r[[k, 0]]).dhypot(l[[k, 1]] - r[[k, 1]]) / 2.0).collect();
    let span = |k: usize| (off[k] as usize, off[k + 1] as usize);
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
        let (mut tl, mut tr) = (vec![0.0f64; n], vec![0.0f64; n]);
        if n > 0 {
            let hmax = amax(hw[a0..b0].iter().copied());
            let lo = [boxes[own][0] - hmax - 30.0, boxes[own][1] - hmax - 30.0];
            let hi = [boxes[own][2] + hmax + 30.0, boxes[own][3] + hmax + 30.0];
            for o in 0..nr {
                let (oa, ob) = span(o);
                if owner[o] == owner[own] || ob - oa < 2 || (shared && in_shared[own] && group[o] == group[own]) {
                    continue;
                }
                let bx = boxes[o];
                if !(bx[2] >= lo[0] && bx[3] >= lo[1] && bx[0] <= hi[0] && bx[1] <= hi[1]) {
                    continue;
                }
                for i in 0..n {
                    let k = a0 + i;
                    let den = nmax(2.0 * hw[k], 1e-9);
                    let u = [(l[[k, 0]] - r[[k, 0]]) / den, (l[[k, 1]] - r[[k, 1]]) / den];
                    if let Some((cut, on_left)) = cop_cut(mid[k], hw[k], u, z[k], bk[k], &mid[oa..ob], &z[oa..ob],
                                                          &bk[oa..ob], &hw[oa..ob], (ends[2 * o], ends[2 * o + 1]),
                                                          tol, along_cos, hide_dz) {
                        if on_left {
                            tl[i] = nmax(tl[i], cut);
                        } else {
                            tr[i] = nmax(tr[i], cut);
                        }
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
