//! A track's bed beside a road, without a step between them (railroadseam.couple, PR #990): per bed vertex the
//! band of heights the roads within reach leave it (each road's own height +- room / slope + tol), the bed moved into
//! the strictest, the moves run out along the track at its grade. Was numpy per vertex per road plan of the cell
//! (a foot search of every near vertex on every road: ~5 s of a Manhattan cell's 35 s of planning, 2026-11-09). The
//! same products and comparisons in the same order as the numpy version (elementwise: bit for bit).
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

const CROSS_COS: f64 = 0.7071067811865476; // 45 deg: a road this far from parallel at the bed crosses it
const CROSS_M: f64 = 2.0;                  // m of height between the bed and such a road: it crosses on a bridge

/// (lo, hi) per vertex of xy: the heights the bed may have beside the roads (-inf / inf where none is near).
/// Roads: their points rxy[off[r]..off[r+1]], heights rz, half widths rh (roads under two points are skipped).
#[allow(clippy::too_many_arguments)]
pub fn bounds(xy: &[[f64; 2]], z: &[f64], half: f64, rxy: &[[f64; 2]], rz: &[f64], off: &[i64], rh: &[f64],
              reach: f64, slope: f64, tol: f64) -> (Vec<f64>, Vec<f64>) {
    let n = xy.len();
    let mut lo = vec![f64::NEG_INFINITY; n];
    let mut hi = vec![f64::INFINITY; n];
    if n == 0 {
        return (lo, hi);
    }
    // the track's own direction at each vertex: a road it crosses on a bridge is not one it runs along
    let mut td = vec![[0.0f64, 0.0]; n];
    for i in 0..n {
        let a = xy[i.saturating_sub(1)];
        let b = xy[(i + 1).min(n - 1)];
        let d = [b[0] - a[0], b[1] - a[1]];
        let len = d[0].dhypot(d[1]);
        if len > 1e-9 {
            td[i] = [d[0] / len, d[1] / len];
        }
    }
    let (mut xmin, mut xmax) = ([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]);
    for p in xy {
        for c in 0..2 {
            // (np.min / np.max: a NaN wins)
            if p[c] < xmin[c] || p[c].is_nan() { xmin[c] = p[c]; }
            if p[c] > xmax[c] || p[c].is_nan() { xmax[c] = p[c]; }
        }
    }
    let mut d = Vec::new();
    for r in 0..rh.len() {
        let (s, e) = (off[r] as usize, off[r + 1] as usize);
        if e - s < 2 {
            continue;
        }
        let pts = &rxy[s..e];
        let zs = &rz[s..e];
        let (mut rmin, mut rmax) = ([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]);
        for p in pts {
            for c in 0..2 {
                if p[c] < rmin[c] || p[c].is_nan() { rmin[c] = p[c]; }
                if p[c] > rmax[c] || p[c].is_nan() { rmax[c] = p[c]; }
            }
        }
        if (0..2).any(|c| rmax[c] < xmin[c] - reach) || (0..2).any(|c| rmin[c] > xmax[c] + reach) {
            continue;
        }
        let m = pts.len() - 1;
        d.clear();
        d.resize(m, 0.0);
        for (i, p) in xy.iter().enumerate() {
            if !(0..2).all(|c| p[c] > rmin[c] - reach && p[c] < rmax[c] + reach) {
                continue;
            }
            // the nearest point of the road's polyline (railroadseam._foot: np.argmin, the first of equals)
            let (mut best, mut k, mut tk) = (f64::NAN, 0usize, 0.0f64);
            for j in 0..m {
                let (a, b) = (pts[j], pts[j + 1]);
                let ab = [b[0] - a[0], b[1] - a[1]];
                let ab2 = {
                    let v = ab[0] * ab[0] + ab[1] * ab[1];
                    if v > 1e-9 || v.is_nan() { v } else { 1e-9 }
                };
                let t = ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / ab2;
                let t = if t.is_nan() { t } else if t < 0.0 { 0.0 } else if t > 1.0 { 1.0 } else { t };
                let f = [a[0] + t * ab[0], a[1] + t * ab[1]];
                let dj = (p[0] - f[0]).dhypot(p[1] - f[1]);
                d[j] = dj;
                if j == 0 || (!best.is_nan() && (dj < best || dj.is_nan())) {
                    best = dj;
                    k = j;
                    tk = t;
                }
            }
            if best > reach || best.is_nan() {
                // (NaN: numpy's comparison is False and the vertex is used with a NaN band; skipped here instead)
                continue;
            }
            let zr = zs[k] + (zs[k + 1] - zs[k]) * tk;
            // a road the track crosses on a bridge (a grade separation) is not one beside it: the fill of an
            // approach cannot be taken to the road under it (the deck then fell to the road and the embankment
            // ended mid-span). At grade (within CROSS_M) the flush crossing still takes the road's height.
            let ab = [pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1]];
            let l = ab[0].dhypot(ab[1]);
            if (zr - z[i]).abs() > CROSS_M && l > 1e-9 {
                let cross = (td[i][0] * ab[0] / l + td[i][1] * ab[1] / l).abs();
                if cross < CROSS_COS {
                    continue;
                }
            }
            let room = (best - rh[r] - half).max(0.0);
            let allow = room / slope + tol;
            lo[i] = lo[i].max(zr - allow);
            hi[i] = hi[i].min(zr + allow);
        }
    }
    (lo, hi)
}

/// max over j of v_j - grade * |s - s_j| (railroadseam._tent).
fn tent(s: &[f64], v: &[f64], grade: f64) -> Vec<f64> {
    let mut out = v.to_vec();
    for k in 1..s.len() {
        out[k] = out[k].max(out[k - 1] - grade * (s[k] - s[k - 1]));
    }
    for k in (0..s.len().saturating_sub(1)).rev() {
        out[k] = out[k].max(out[k + 1] - grade * (s[k + 1] - s[k]));
    }
    out
}

/// railroadseam.couple: the moved bed heights, or None when nothing moved (z itself).
#[pyfunction]
#[pyo3(signature = (xy, z, half, rxy, rz, off, rh, reach, slope, tol, max_move, grade))]
#[allow(clippy::too_many_arguments)]
pub fn rail_seam_couple<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>, half: f64,
                             rxy: PyReadonlyArray2<'py, f64>, rz: PyReadonlyArray1<'py, f64>,
                             off: PyReadonlyArray1<'py, i64>, rh: PyReadonlyArray1<'py, f64>, reach: f64, slope: f64,
                             tol: f64, max_move: f64, grade: f64) -> PyResult<Option<Bound<'py, PyArray1<f64>>>> {
    let xyv = xy.as_array();
    let rxyv = rxy.as_array();
    if xyv.ncols() < 2 || rxyv.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("xy and rxy need two columns"));
    }
    let pts: Vec<[f64; 2]> = xyv.rows().into_iter().map(|r| [r[0], r[1]]).collect();
    let rpts: Vec<[f64; 2]> = rxyv.rows().into_iter().map(|r| [r[0], r[1]]).collect();
    let zv: Vec<f64> = z.as_array().iter().copied().collect();
    let rzv: Vec<f64> = rz.as_array().iter().copied().collect();
    let offv: Vec<i64> = off.as_array().iter().copied().collect();
    let rhv: Vec<f64> = rh.as_array().iter().copied().collect();
    let n = zv.len();
    if pts.len() != n || rzv.len() != rpts.len() || offv.len() != rhv.len() + 1
        || offv.iter().any(|&o| o < 0 || o as usize > rpts.len()) || offv.windows(2).any(|w| w[1] < w[0]) {
        return Err(pyo3::exceptions::PyValueError::new_err("rail_seam_couple: inconsistent lengths"));
    }
    if n < 2 {
        return Ok(None);
    }
    let out = py.detach(|| {
        let (lo, hi) = bounds(&pts, &zv, half, &rpts, &rzv, &offv, &rhv, reach, slope, tol);
        let both: Vec<bool> = (0..n).map(|i| lo[i].is_finite() && hi[i].is_finite()).collect();
        if !both.iter().any(|&b| b) {
            return None;
        }
        let mut dv = vec![0.0f64; n];
        for i in 0..n {
            if !both[i] {
                continue;
            }
            let target = if lo[i] > hi[i] {
                0.5 * (lo[i] + hi[i])
            } else {
                // np.clip(z, lo, hi): max then min
                let c = if zv[i] < lo[i] { lo[i] } else { zv[i] };
                if c > hi[i] { hi[i] } else { c }
            };
            let dd = target - zv[i];
            dv[i] = if dd < -max_move { -max_move } else if dd > max_move { max_move } else { dd };
        }
        if !(dv.iter().fold(0.0f64, |m, &x| if x.abs() > m || x.is_nan() { x.abs() } else { m }) > 1e-6) {
            return None;
        }
        let mut s = vec![0.0f64; n];
        for k in 1..n {
            s[k] = s[k - 1] + (pts[k][0] - pts[k - 1][0]).dhypot(pts[k][1] - pts[k - 1][1]);
        }
        let up = tent(&s, &dv.iter().map(|&x| x.max(0.0)).collect::<Vec<_>>(), grade);
        let down = tent(&s, &dv.iter().map(|&x| (-x).max(0.0)).collect::<Vec<_>>(), grade);
        Some((0..n).map(|k| zv[k] + if up[k] >= down[k] { up[k] } else { -down[k] }).collect::<Vec<f64>>())
    });
    Ok(out.map(|v| v.into_pyarray(py)))
}
