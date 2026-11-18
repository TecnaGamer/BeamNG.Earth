//! Australian houses' kernels (buildings.py): the side fences', carports' and awnings' clearance tests.
use numpy::ndarray::ArrayView2;
use numpy::{PyReadonlyArray1, PyReadonlyArray2, PyReadwriteArray1};
use pyo3::prelude::*;

use crate::dm::Dm;

/// buildings._seg_cross: do the segments pq and rs cross (parallel within 1e-12: no)?
#[inline]
fn seg_cross(p: [f64; 2], q: [f64; 2], r: [f64; 2], s: [f64; 2]) -> bool {
    let (d1, d2) = ([q[0] - p[0], q[1] - p[1]], [s[0] - r[0], s[1] - r[1]]);
    let den = d1[0] * d2[1] - d1[1] * d2[0];
    if den.abs() < 1e-12 {
        return false;
    }
    let w = [r[0] - p[0], r[1] - p[1]];
    let t = (w[0] * d2[1] - w[1] * d2[0]) / den;
    let u = (w[0] * d1[1] - w[1] * d1[0]) / den;
    (0.0..=1.0).contains(&t) && (0.0..=1.0).contains(&u)
}

/// buildings._rect_hits: does the convex counter-clockwise quad R (4, 2) touch the ring (n, >= 2): a ring vertex
/// inside R, a corner of R inside the ring (even-odd), or an edge of each crossing; the Python's tests in its order.
fn rect_hits_view(r: &ArrayView2<f64>, ring: &ArrayView2<f64>) -> bool {
    let q: [[f64; 2]; 4] = std::array::from_fn(|i| [r[[i, 0]], r[[i, 1]]]);
    let n = ring.nrows();
    for k in 0..n {
        let (px, py) = (ring[[k, 0]], ring[[k, 1]]);
        if (0..4).all(|i| {
            let (a, b) = (q[i], q[(i + 1) % 4]);
            (b[0] - a[0]) * (py - a[1]) - (b[1] - a[1]) * (px - a[0]) >= 0.0
        }) {
            return true;
        }
    }
    if q.iter().any(|c| crate::inside_view(ring, c[0], c[1])) {
        return true;
    }
    (0..4).any(|i| (0..n).any(|j| seg_cross(q[i], q[(i + 1) % 4], [ring[[j, 0]], ring[[j, 1]]],
                                               [ring[[(j + 1) % n, 0]], ring[[(j + 1) % n, 1]]])))
}

/// buildings._rect_hits for one ring.
#[pyfunction]
pub fn rect_hits(r: PyReadonlyArray2<f64>, ring: PyReadonlyArray2<f64>) -> bool {
    rect_hits_view(&r.as_array(), &ring.as_array())
}

/// any(buildings._rect_hits(R, ring) for ring in rings), first to last.
#[pyfunction]
pub fn rect_hits_any(r: PyReadonlyArray2<f64>, rings: Vec<PyReadonlyArray2<f64>>) -> bool {
    let rv = r.as_array();
    rings.iter().any(|g| rect_hits_view(&rv, &g.as_array()))
}

/// Python's float floor division a // b (CPython's float_floor_div: fmod, then the quotient rounded to the nearest
/// integer), for b > 0.
fn py_floordiv(a: f64, b: f64) -> f64 {
    let mut m = a % b;
    let mut div = (a - m) / b;
    if m != 0.0 && (b < 0.0) != (m < 0.0) {
        m += b;
        div -= 1.0;
    }
    let _ = m;
    if div == 0.0 {
        return 0.0f64.copysign(a / b);
    }
    let mut f = div.floor();
    if div - f > 0.5 {
        f += 1.0;
    }
    f
}

/// buildings._qld_brick_fit: (scale, o) for a wall of length l m (bay: m of sheet per window bay), numpy's
/// arithmetic per window middle; lexsort's (bad, then lop, then first index) choice.
fn brick_fit(l: f64, bay: f64, scales: &[f64], win: f64, margin: f64) -> (f64, f64) {
    let mut best: Option<(f64, f64, f64)> = None;
    for &sc in scales {
        let (step, hw) = (bay * sc, win * sc / 2.0);
        let n_o = (step / 0.05).ceil().max(0.0) as usize;
        let hi = py_floordiv(l, step) as i64 + 3;
        let mut pick: Option<(f64, f64, usize)> = None; // (bad, lop, k)
        for i in 0..n_o {
            let o = i as f64 * 0.05;
            let (mut cut_n, mut in_n) = (0u32, 0u32);
            let (mut first, mut last) = (f64::INFINITY, f64::INFINITY);
            for j in -1..hi {
                let x = o + step * j as f64;
                if x + hw <= 0.0 || x - hw >= l {
                    continue; // wholly off the wall
                }
                in_n += 1;
                if (x + hw > 0.0 && x - hw < margin) || (x + hw > l - margin && x - hw < l) {
                    cut_n += 1;
                }
                first = first.min(x - hw);
                last = last.min(l - (x + hw));
            }
            let bad = cut_n as f64 + 0.5 * ((in_n == 0) as i32 as f64) * ((l >= 2.2) as i32 as f64);
            let lop = if first.is_finite() { (first - last).abs() } else { 0.0 };
            if pick.is_none_or(|(b, lp, _)| bad < b || (bad == b && lop < lp)) {
                pick = Some((bad, lop, i));
            }
        }
        let (bad, _, k) = pick.unwrap_or((0.0, 0.0, 0)); // (n_o is at least 1 for a positive step)
        if best.is_none_or(|(b, _, _)| bad < b) {
            best = Some((bad, sc, k as f64 * 0.05));
        }
        if bad == 0.0 {
            break;
        }
    }
    let b = best.unwrap_or((0.0, 1.0, 0.0));
    (b.1, b.2)
}

/// buildings._qld_brick_scale: rewrites the u of each window-sheet wall quad of a QLD brick house in place
/// (UV[8q], UV[8q+6] = ua; UV[8q+2], UV[8q+4] = ub). ring (n, >= 2); p, uv, mid: building_walls_np's arrays (12 and 8
/// numbers and a material per quad); band1 the sheet's bays; skip the wall the door strip splits (-1 none).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn qld_brick_uv(ring: PyReadonlyArray2<f64>, p: PyReadonlyArray1<f64>, mut uv: PyReadwriteArray1<f64>,
                    mid: PyReadonlyArray1<u8>, band1: f64, tile_px_m: f64, skip: i64, scales: Vec<f64>, win: f64,
                    margin: f64) -> PyResult<()> {
    let (r, p, mid) = (ring.as_array(), p.as_array(), mid.as_array());
    let mut uv = uv.as_array_mut();
    if p.len() < 12 * mid.len() || uv.len() < 8 * mid.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("qld_brick_uv: 12 P and 8 UV numbers per quad"));
    }
    let n = r.nrows();
    let pts: Vec<[f64; 2]> = (0..n).map(|i| [r[[i, 0]], r[[i, 1]]]).collect();
    let e: Vec<[f64; 2]> = (0..n).map(|i| {
        let j = (i + 1) % n;
        [pts[j][0] - pts[i][0], pts[j][1] - pts[i][1]]
    }).collect();
    let el: Vec<f64> = e.iter().map(|v| v[0].dhypot(v[1])).collect();
    let bay = tile_px_m / band1;
    let mut fits: Vec<Option<(f64, f64)>> = vec![None; n];
    for (q, &k) in mid.iter().enumerate() {
        if k != 0 || uv[8 * q + 2] - uv[8 * q] < 0.1 {
            continue;
        }
        let (x, y, x1, y1) = (p[12 * q], p[12 * q + 1], p[12 * q + 3], p[12 * q + 4]);
        let mut bi = 0usize;
        let mut bo = f64::INFINITY;
        for i in 0..n {
            let mut off = 0.0f64;
            for (px, py) in [(x, y), (x1, y1)] {
                let t = (((px - pts[i][0]) * e[i][0] + (py - pts[i][1]) * e[i][1]) / (el[i] * el[i]).max(1e-12))
                    .clamp(0.0, 1.0);
                let (wx, wy) = (pts[i][0] + e[i][0] * t, pts[i][1] + e[i][1] * t);
                off += (wx - px).dhypot(wy - py);
            }
            if off < bo {
                bo = off;
                bi = i;
            }
        }
        if n == 0 || bi as i64 == skip || el[bi] < 0.05 {
            continue;
        }
        let l = el[bi];
        let (sc, o) = *fits[bi].get_or_insert_with(|| brick_fit(l, bay, &scales, win, margin));
        let a = ((x - pts[bi][0]) * e[bi][0] + (y - pts[bi][1]) * e[bi][1]) / l;
        let b = ((x1 - pts[bi][0]) * e[bi][0] + (y1 - pts[bi][1]) * e[bi][1]) / l;
        let (ua, ub) = ((a - o) / (tile_px_m * sc) + 0.5 / band1, (b - o) / (tile_px_m * sc) + 0.5 / band1);
        uv[8 * q] = ua;
        uv[8 * q + 2] = ub;
        uv[8 * q + 4] = ub;
        uv[8 * q + 6] = ua;
    }
    Ok(())
}

/// buildings._au_side_fences' tests for each fence piece i (centre p (m, 2), unit direction dr (m, 2), half length
/// half (m,)): kept unless its 0.3 m wide box (rect_hits) touches one of the rings whose centre cents (r, 2) lies
/// within 40 m of p, or one of its ends or its middle is nearer than hw (s,) to a road segment a -> b (s, 2)
/// (points_near_segments' arithmetic); the Python's numpy expressions per element, in its order.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn au_fence_keep<'py>(py: Python<'py>, p: PyReadonlyArray2<'py, f64>, dr: PyReadonlyArray2<'py, f64>,
                          half: PyReadonlyArray1<'py, f64>, rings: Vec<PyReadonlyArray2<'py, f64>>,
                          cents: PyReadonlyArray2<'py, f64>, a: PyReadonlyArray2<'py, f64>,
                          b: PyReadonlyArray2<'py, f64>, hw: PyReadonlyArray1<'py, f64>)
                          -> PyResult<Bound<'py, numpy::PyArray1<bool>>> {
    let (p, dr, half, cents, a, b, hw) = (p.as_array(), dr.as_array(), half.as_array(), cents.as_array(), a.as_array(),
                                          b.as_array(), hw.as_array());
    if dr.nrows() != p.nrows() || half.len() != p.nrows() || cents.nrows() != rings.len() || b.nrows() != a.nrows()
        || hw.len() != a.nrows() {
        return Err(pyo3::exceptions::PyValueError::new_err("au_fence_keep: mismatched lengths"));
    }
    let rv: Vec<ArrayView2<f64>> = rings.iter().map(|r| r.as_array()).collect();
    let seg: Vec<[f64; 6]> = (0..a.nrows()).map(|k| {
        let (a0, a1) = (a[[k, 0]], a[[k, 1]]);
        let (ab0, ab1) = (b[[k, 0]] - a0, b[[k, 1]] - a1);
        [a0, a1, ab0, ab1, (ab0 * ab0 + ab1 * ab1).max(1e-9), hw[k]]
    }).collect();
    let near_road = |x: f64, y: f64| seg.iter().any(|g| {
        let t = (((x - g[0]) * g[2] + (y - g[1]) * g[3]) / g[4]).clamp(0.0, 1.0);
        (x - (g[0] + t * g[2])).dhypot(y - (g[1] + t * g[3])) < g[5]
    });
    let out: Vec<bool> = (0..p.nrows()).map(|i| {
        let (p0, p1, d0, d1, h) = (p[[i, 0]], p[[i, 1]], dr[[i, 0]], dr[[i, 1]], half[i]);
        let (n0, n1) = (-d1, d0);
        let q = numpy::ndarray::arr2(&[[p0 - d0 * h - n0 * 0.15, p1 - d1 * h - n1 * 0.15],
                                       [p0 + d0 * h - n0 * 0.15, p1 + d1 * h - n1 * 0.15],
                                       [p0 + d0 * h + n0 * 0.15, p1 + d1 * h + n1 * 0.15],
                                       [p0 - d0 * h + n0 * 0.15, p1 - d1 * h + n1 * 0.15]]);
        let qv = q.view();
        let hit = (0..rv.len()).any(|k| (cents[[k, 0]] - p0).dhypot(cents[[k, 1]] - p1) < 40.0
                                        && rect_hits_view(&qv, &rv[k]));
        if hit {
            return false;
        }
        !(near_road(p0 - d0 * h, p1 - d1 * h) || near_road(p0, p1) || near_road(p0 + d0 * h, p1 + d1 * h))
    }).collect();
    Ok(numpy::IntoPyArray::into_pyarray(out, py))
}
