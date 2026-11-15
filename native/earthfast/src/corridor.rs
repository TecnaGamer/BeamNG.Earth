//! grading.share_corridors: roads whose carriageways touch or overlap side by side (a fork's two branches, a slip
//! road along its motorway or along the road it joins, a dual carriageway's halves, a road dipped under a deck and
//! the ramp beside it) agree in height there. Each plan was smoothed and pinned on its own, so two branches leaving a
//! fork at a few degrees climbed and fell apart while their pavements still overlapped, and grade_points' blend gave
//! the overlap to one of them: a 0.4-0.8 m ramp within 3 m across the lanes (GitHub #1043 Wesley Chapel FL, #1045
//! the CR 99 diverging diamond on I-75 in Ohio: ramp IRI 19-64 m/km, 4-7 g jolts at 45 mph).
//!
//! For every vertex of a plan, each other plan near and roughly parallel (within ang of its direction) whose
//! carriageway is within gap_m of its own asks for the height difference between them to fit their gap at bank_s
//! (m per m; none where the pavements overlap). Who moves (key): a road dipped under a deck holds and a slip road
//! beside it follows; a higher key holds (a through road over a slip road, then grade_rank); of two equal slip roads
//! the lower rises; two roads both keyed equal_hold or more (through roads, or one dipped) keep their own (a dual
//! carriageway's halves on a hillside). A vertex beside a cutting's wall neither asks nor answers (two levels). A
//! point within abut_hold m of the plan's own bridge end and the plan's two ends hold (the junctions there, welded and
//! snapped after). The corrections asked are made smooth along the road: a smoothing spline (weights wreq where asked,
//! w0 elsewhere, lam on the curvature: (lam / w0)^(1/4) ~ 47 m of easing), solved per plan (a banded LDL^T). All
//! corrections come from the plans as given (no order dependence).
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

struct Seg {
    plan: usize,
    i: usize,
}

/// A grid over the eligible plans' segments.
struct SegGrid {
    x0: f64,
    y0: f64,
    cell: f64,
    nx: usize,
    ny: usize,
    cells: Vec<Vec<u32>>,
    segs: Vec<Seg>,
}

impl SegGrid {
    fn key(&self, x: f64, y: f64) -> (i64, i64) {
        (((x - self.x0) / self.cell).floor() as i64, ((y - self.y0) / self.cell).floor() as i64)
    }
}

#[allow(clippy::too_many_arguments)]
pub fn corridor_core(xy: &[[f64; 2]], off: &[usize], z: &[f64], wall: &[bool], rh: &[f64], key: &[i64],
                     abut: &[[bool; 2]],
                     cos_ang: f64, bank_s: f64, gap_m: f64, max_diff: f64, abut_hold: f64, wreq: f64, w0: f64,
                     lam: f64, equal_hold: i64) -> (Vec<f64>, Vec<bool>) {
    let n = off.len() - 1;
    let mut zout = z.to_vec();
    let mut changed = vec![false; n];
    let ok = |j: usize| key[j] >= 0 && off[j + 1] - off[j] >= 2 && rh[j].is_finite();
    // the grid
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    let mut max_rh = 0.0f64;
    for j in 0..n {
        if !ok(j) {
            continue;
        }
        max_rh = max_rh.max(rh[j]);
        for p in &xy[off[j]..off[j + 1]] {
            if p[0].is_finite() && p[1].is_finite() {
                x0 = x0.min(p[0]);
                y0 = y0.min(p[1]);
                x1 = x1.max(p[0]);
                y1 = y1.max(p[1]);
            }
        }
    }
    if !x0.is_finite() {
        return (zout, changed);
    }
    let cell = 16.0f64;
    let nx = (((x1 - x0) / cell).floor() as usize) + 1;
    let ny = (((y1 - y0) / cell).floor() as usize) + 1;
    if nx.saturating_mul(ny) > 50_000_000 {
        return (zout, changed); // (absurd extents: nothing rather than a huge grid)
    }
    let mut g = SegGrid { x0, y0, cell, nx, ny, cells: vec![Vec::new(); nx * ny], segs: Vec::new() };
    for j in 0..n {
        if !ok(j) {
            continue;
        }
        let q = &xy[off[j]..off[j + 1]];
        for i in 0..q.len() - 1 {
            let (a, b) = (q[i], q[i + 1]);
            if !(a[0].is_finite() && a[1].is_finite() && b[0].is_finite() && b[1].is_finite()) {
                continue;
            }
            let id = g.segs.len() as u32;
            g.segs.push(Seg { plan: j, i });
            let (ka, kb) = (g.key(a[0].min(b[0]), a[1].min(b[1])), g.key(a[0].max(b[0]), a[1].max(b[1])));
            for cx in ka.0.max(0)..=kb.0.min(g.nx as i64 - 1) {
                for cy in ka.1.max(0)..=kb.1.min(g.ny as i64 - 1) {
                    g.cells[cy as usize * g.nx + cx as usize].push(id);
                }
            }
        }
    }
    // per plan: the corrections asked, then the spline
    let mut best: Vec<(f64, usize, f64)> = Vec::new(); // per other plan near a vertex: (distance, segment, t)
    let mut seen: Vec<usize> = Vec::new();
    let mut mark = vec![usize::MAX; n];
    for j in 0..n {
        if !ok(j) {
            continue;
        }
        let (o, m) = (off[j], off[j + 1] - off[j]);
        if m < 3 {
            continue;
        }
        let p = &xy[o..o + m];
        let zp = &z[o..o + m];
        let mut s = vec![0.0f64; m];
        for k in 1..m {
            s[k] = s[k - 1] + (p[k][0] - p[k - 1][0]).dhypot(p[k][1] - p[k - 1][1]);
        }
        let last = s[m - 1];
        let mut up = vec![0.0f64; m];
        let mut dn = vec![0.0f64; m];
        let mut any = false;
        let reach = rh[j] + max_rh + gap_m;
        for i in 0..m {
            let (px, py) = (p[i][0], p[i][1]);
            if !(px.is_finite() && py.is_finite() && zp[i].is_finite()) {
                continue;
            }
            if (abut[j][0] && s[i] <= abut_hold) || (abut[j][1] && s[i] >= last - abut_hold) || wall[o + i] {
                continue;       // (held, or a cutting's wall beside it: two levels, not one pavement)
            }
            // the direction (np.gradient: central, one-sided at the ends)
            let (a, b) = if i == 0 { (0, 1) } else if i == m - 1 { (m - 2, m - 1) } else { (i - 1, i + 1) };
            let (tx, ty) = (p[b][0] - p[a][0], p[b][1] - p[a][1]);
            let tl = tx.dhypot(ty);
            if tl < 1e-9 {
                continue;
            }
            let (tx, ty) = (tx / tl, ty / tl);
            // the other plans' nearest segments within reach
            let ka = g.key(px - reach, py - reach);
            let kb = g.key(px + reach, py + reach);
            best.clear();
            for &k in &seen {
                mark[k] = usize::MAX;
            }
            seen.clear();
            for cx in ka.0.max(0)..=kb.0.min(g.nx as i64 - 1) {
                for cy in ka.1.max(0)..=kb.1.min(g.ny as i64 - 1) {
                    for &id in &g.cells[cy as usize * g.nx + cx as usize] {
                        let sg = &g.segs[id as usize];
                        if sg.plan == j {
                            continue;
                        }
                        let q = &xy[off[sg.plan]..off[sg.plan + 1]];
                        let (qa, qb) = (q[sg.i], q[sg.i + 1]);
                        let (dx, dy) = (qb[0] - qa[0], qb[1] - qa[1]);
                        let ll = (dx * dx + dy * dy).max(1e-12);
                        let t = (((px - qa[0]) * dx + (py - qa[1]) * dy) / ll).clamp(0.0, 1.0);
                        let d = (qa[0] + dx * t - px).dhypot(qa[1] + dy * t - py);
                        let slot = mark[sg.plan];
                        if slot == usize::MAX {
                            mark[sg.plan] = best.len();
                            seen.push(sg.plan);
                            best.push((d, sg.i, t));
                        } else if d < best[slot].0 || (d == best[slot].0 && sg.i < best[slot].1) {
                            best[slot] = (d, sg.i, t);
                        }
                    }
                }
            }
            for (bi, &k) in seen.iter().enumerate() {
                let (d, si, t) = best[bi];
                let q = &xy[off[k]..off[k + 1]];
                let zq = &z[off[k]..off[k + 1]];
                let nseg = q.len() - 1;
                // (not past the other's end: its end the nearest point, this one beyond it)
                if (si == 0 && t <= 0.0) || (si == nseg - 1 && t >= 1.0) {
                    continue;
                }
                let gap = d - rh[k] - rh[j];
                if gap > gap_m || wall[off[k] + si] || wall[off[k] + si + 1] {
                    continue;   // (the other in a cutting there: Porte Maillot's ramps beside the walled trench)
                }
                let (dx, dy) = (q[si + 1][0] - q[si][0], q[si + 1][1] - q[si][1]);
                let dl = dx.dhypot(dy);
                if dl < 1e-9 || ((tx * dx + ty * dy) / dl).abs() < cos_ang {
                    continue;
                }
                let zk = zq[si] + (zq[si + 1] - zq[si]) * t;
                let diff = zk - zp[i];
                if !diff.is_finite() || diff.abs() > max_diff {
                    continue;
                }
                let need = (diff.abs() - gap.max(0.0) * bank_s).max(0.0);
                if need <= 0.0 {
                    continue;
                }
                if key[j] >= equal_hold && key[k] >= equal_hold {
                    continue; // (two through roads hold their own: a dual carriageway's halves on a hillside, one
                              // dipped under a deck and the other not: BR-381's 2 m notch at a junction between)
                }
                let moves = if key[j] != key[k] { key[j] < key[k] } else { diff > 0.0 };
                if !moves {
                    continue;
                }
                if diff > 0.0 {
                    up[i] = up[i].max(need);
                } else {
                    dn[i] = dn[i].max(need);
                }
                any = true;
            }
        }
        if !any {
            continue;
        }
        // the spline: minimise sum W (d - c)^2 + lam sum h_mid (d'')^2; the ends and the held points fixed at 0 (left
        // free, the corrections moved the junctions and every road meeting there: Wesley Chapel's ramps worse)
        let mut w = vec![0.0f64; m];
        let mut c = vec![0.0f64; m];
        for i in 0..m {
            let held = (abut[j][0] && s[i] <= abut_hold) || (abut[j][1] && s[i] >= last - abut_hold);
            if i == 0 || i == m - 1 || held {
                w[i] = 1e6;
                c[i] = 0.0;
            } else if up[i] > 0.0 || dn[i] > 0.0 {
                w[i] = wreq;
                c[i] = if up[i] >= dn[i] { up[i] } else { -dn[i] };
            } else {
                w[i] = w0;
            }
        }
        // A = diag(w) + lam D^T D, D (m - 2, m): row r = sqrt(hm) * [1/(h0 hm), -(1/h0 + 1/h1)/hm, 1/(h1 hm)]
        let mut a0 = w.clone(); // diagonal
        let mut a1 = vec![0.0f64; m]; // (i, i + 1)
        let mut a2 = vec![0.0f64; m]; // (i, i + 2)
        for r in 0..m - 2 {
            let h0 = (s[r + 1] - s[r]).max(1e-3);
            let h1 = (s[r + 2] - s[r + 1]).max(1e-3);
            let hm = 0.5 * (h0 + h1);
            let sq = hm.sqrt();
            let d = [sq / (h0 * hm), -sq * (1.0 / h0 + 1.0 / h1) / hm, sq / (h1 * hm)];
            for u in 0..3 {
                a0[r + u] += lam * d[u] * d[u];
            }
            a1[r] += lam * d[0] * d[1];
            a1[r + 1] += lam * d[1] * d[2];
            a2[r] += lam * d[0] * d[2];
        }
        let rhs: Vec<f64> = (0..m).map(|i| w[i] * c[i]).collect();
        let Some(mut dz) = solve_penta(&a0, &a1, &a2, &rhs) else { continue };
        for i in 0..m {
            if w[i] >= 1e6 {
                dz[i] = 0.0; // (held: exactly)
            }
        }
        let mut big = false;
        for i in 0..m {
            if dz[i].is_finite() && dz[i].abs() >= 0.01 {
                big = true;
            }
        }
        if !big {
            continue;
        }
        for i in 0..m {
            if dz[i].is_finite() {
                zout[o + i] = z[o + i] + dz[i];
            }
        }
        changed[j] = true;
    }
    (zout, changed)
}

/// The symmetric positive definite pentadiagonal system (diagonal a0, first a1, second a2 off-diagonals) by banded
/// LDL^T; None if it is not positive definite.
fn solve_penta(a0: &[f64], a1: &[f64], a2: &[f64], b: &[f64]) -> Option<Vec<f64>> {
    let m = a0.len();
    // L unit lower with bands l1 (i, i-1), l2 (i, i-2); D diagonal
    let mut d = vec![0.0f64; m];
    let mut l1 = vec![0.0f64; m];
    let mut l2 = vec![0.0f64; m];
    for i in 0..m {
        if i >= 2 {
            l2[i] = a2[i - 2] / d[i - 2];
        }
        if i >= 1 {
            let mut v = a1[i - 1];
            if i >= 2 {
                v -= l2[i] * d[i - 2] * l1[i - 1];
            }
            l1[i] = v / d[i - 1];
        }
        let mut v = a0[i];
        if i >= 1 {
            v -= l1[i] * l1[i] * d[i - 1];
        }
        if i >= 2 {
            v -= l2[i] * l2[i] * d[i - 2];
        }
        if !(v > 0.0) || !v.is_finite() {
            return None;
        }
        d[i] = v;
    }
    let mut y = b.to_vec();
    for i in 0..m {
        if i >= 1 {
            y[i] -= l1[i] * y[i - 1];
        }
        if i >= 2 {
            y[i] -= l2[i] * y[i - 2];
        }
    }
    for i in 0..m {
        y[i] /= d[i];
    }
    for i in (0..m).rev() {
        if i + 1 < m {
            y[i] -= l1[i + 1] * y[i + 1];
        }
        if i + 2 < m {
            y[i] -= l2[i + 2] * y[i + 2];
        }
    }
    Some(y)
}

/// grading.share_corridors (above): plans (xy (n, 2), off, z), wall (n,) a cutting's wall beside the vertex (the
/// plan's cut: a vertex there neither asks nor answers), rh (plans,) carriageway half widths, key (plans,)
/// who holds (below 0: not part of it), abut (plans, 2) bridge ends; equal_hold: two plans both keyed at least this
/// hold (neither moves). Returns (z, changed per plan).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn plan_share_corridors<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                                 z: PyReadonlyArray1<'py, f64>, wall: PyReadonlyArray1<'py, bool>,
                                 rh: PyReadonlyArray1<'py, f64>,
                                 key: PyReadonlyArray1<'py, i64>, abut: PyReadonlyArray2<'py, bool>, ang_deg: f64,
                                 bank_s: f64, gap_m: f64, max_diff: f64, abut_hold: f64, wreq: f64, w0: f64, lam: f64,
                                 equal_hold: i64)
                                 -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<bool>>)> {
    let (a, o, zz, rh, key, ab) = (xy.as_array(), off.as_array(), z.as_array(), rh.as_array(), key.as_array(),
                                   abut.as_array());
    let wv: Vec<bool> = wall.as_array().iter().copied().collect();
    let n = if o.is_empty() { 0 } else { o.len() - 1 };
    if a.ncols() < 2 || zz.len() != a.nrows() || o.is_empty() || o[0] != 0 || o[o.len() - 1] as usize != a.nrows()
        || (1..o.len()).any(|i| o[i] < o[i - 1]) || wv.len() != a.nrows() || rh.len() != n || key.len() != n || ab.nrows() != n
        || ab.ncols() != 2 {
        return Err(PyValueError::new_err("plan_share_corridors: mismatched plan arrays"));
    }
    let xyv: Vec<[f64; 2]> = (0..a.nrows()).map(|i| [a[[i, 0]], a[[i, 1]]]).collect();
    let offv: Vec<usize> = o.iter().map(|&v| v as usize).collect();
    let zv: Vec<f64> = zz.iter().copied().collect();
    let rhv: Vec<f64> = rh.iter().copied().collect();
    let keyv: Vec<i64> = key.iter().copied().collect();
    let abv: Vec<[bool; 2]> = (0..n).map(|j| [ab[[j, 0]], ab[[j, 1]]]).collect();
    let cos_ang = ang_deg.to_radians().dcos();
    let (zo, ch) = py.detach(|| corridor_core(&xyv, &offv, &zv, &wv, &rhv, &keyv, &abv, cos_ang, bank_s, gap_m, max_diff,
                                              abut_hold, wreq, w0, lam, equal_hold));
    Ok((zo.into_pyarray(py), ch.into_pyarray(py)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn penta_solves() {
        // A = tridiagonal-ish SPD; check A x = b
        let m = 7;
        let a0 = vec![6.0; m];
        let a1 = vec![-2.0; m];
        let a2 = vec![0.5; m];
        let b: Vec<f64> = (0..m).map(|i| i as f64).collect();
        let x = solve_penta(&a0, &a1, &a2, &b).unwrap();
        for i in 0..m {
            let mut v = a0[i] * x[i];
            if i >= 1 {
                v += a1[i - 1] * x[i - 1];
            }
            if i + 1 < m {
                v += a1[i] * x[i + 1];
            }
            if i >= 2 {
                v += a2[i - 2] * x[i - 2];
            }
            if i + 2 < m {
                v += a2[i] * x[i + 2];
            }
            assert!((v - b[i]).abs() < 1e-9, "{i}: {v} vs {}", b[i]);
        }
    }

    #[test]
    fn fork_branches_meet() {
        // two branches leaving (0, 0) at 10 degrees, one climbing 6 %, one falling 6 %, carriageways 7 m half
        let mut xy = Vec::new();
        let mut z = Vec::new();
        let mut off = vec![0usize];
        for sgn in [1.0f64, -1.0] {
            let a = (5.0f64 * sgn).to_radians();
            for i in 0..60 {
                let s = i as f64 * 2.0;
                xy.push([s * a.dcos(), s * a.dsin()]);
                z.push(sgn * 0.03 * s);
            }
            off.push(xy.len());
        }
        let (zo, ch) = corridor_core(&xy, &off, &z, &vec![false; xy.len()], &[7.0, 7.0], &[5, 5], &[[false, false]; 2], 45f64.to_radians().dcos(),
                                     0.4, 6.0, 10.0, 25.0, 10.0, 0.01, 50000.0, i64::MAX);
        assert!(!ch[0] && ch[1], "only the lower branch rises: {ch:?}");
        // at 40 m along, the pavements still overlap (gap = 40 sin 10 - 14 < 0): most of the 2.4 m taken out
        let (i0, i1) = (20, 60 + 20);
        assert!((zo[i0] - zo[i1]).abs() < 0.8, "{} vs {}", zo[i0], zo[i1]);
        assert!((zo[i0] - z[i0]).abs() < 1e-12);
        assert!(zo[60] == z[60] && zo[119] == z[119], "the ends hold");
    }
}
