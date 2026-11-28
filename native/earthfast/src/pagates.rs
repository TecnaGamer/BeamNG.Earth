//! The parked aircraft's hottest geometry (server/parkedaircraft.py _area, _pip, _seg_dist, _bridge): called tens of thousands
//! of times a big airport on a few points each (an airport's gates fitted: ~7 s of numpy's per-call overhead at Atlanta, the
//! user, 2026-11-17). Ports of the Python as it is, in its evaluation order, to the bit: dnp.dot's sum (in order up to 8
//! products, numpy's pairwise sum above), the libm hypot of dm.rs.

use crate::dm::Dm;
use crate::standpaths::pysum;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// dnp.dot of two 1-D arrays: the products summed in order up to 8, pairwise above (dnp.matmul's k > 8 add.reduce).
fn ddot(a: &[f64], b: &[f64]) -> f64 {
    let n = a.len();
    if n == 0 { return 0.0; }
    if n <= 8 {
        let mut s = a[0] * b[0];
        for i in 1..n { s = s + a[i] * b[i]; }
        return s;
    }
    let p: Vec<f64> = (0..n).map(|i| a[i] * b[i]).collect();
    pysum(&p)
}

/// parkedaircraft._area: the shoelace area of the ring (k, 2).
pub(crate) fn area(x: &[f64], y: &[f64]) -> f64 {
    let n = x.len();
    let yr: Vec<f64> = (0..n).map(|i| y[(i + 1) % n]).collect();
    let xr: Vec<f64> = (0..n).map(|i| x[(i + 1) % n]).collect();
    0.5 * (ddot(x, &yr) - ddot(&xr, y)).abs()
}

/// parkedaircraft._pip: which points lie inside the ring (even-odd).
pub(crate) fn pip(rx: &[f64], ry: &[f64], px: &[f64], py: &[f64]) -> Vec<bool> {
    let n = rx.len();
    (0..px.len()).map(|j| {
        let (x, y) = (px[j], py[j]);
        let mut c = 0u32;
        for i in 0..n {
            let (a0, a1, b0, b1) = (rx[i], ry[i], rx[(i + 1) % n], ry[(i + 1) % n]);
            let cross = (a1 <= y) != (b1 <= y);
            let dy = if b1 == a1 { 1.0 } else { b1 - a1 };
            let xi = a0 + (y - a1) * (b0 - a0) / dy;
            if cross && x < xi { c += 1; }
        }
        c % 2 == 1
    }).collect()
}

fn cols(a: &numpy::ndarray::ArrayView2<f64>) -> (Vec<f64>, Vec<f64>) {
    ((0..a.nrows()).map(|i| a[[i, 0]]).collect(), (0..a.nrows()).map(|i| a[[i, 1]]).collect())
}

fn two(a: &PyReadonlyArray2<'_, f64>, what: &str) -> PyResult<(Vec<f64>, Vec<f64>)> {
    let v = a.as_array();
    if v.nrows() > 0 && v.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err(format!("{what}: (k, 2) points")));
    }
    Ok(cols(&v))
}

#[pyfunction]
fn pa_area(ring: PyReadonlyArray2<'_, f64>) -> PyResult<f64> {
    let (x, y) = two(&ring, "pa_area")?;
    Ok(area(&x, &y))
}

#[pyfunction]
fn pa_pip<'py>(py: Python<'py>, ring: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>)
               -> PyResult<Bound<'py, PyArray1<bool>>> {
    let (rx, ry) = two(&ring, "pa_pip ring")?;
    let (px, pyv) = two(&pts, "pa_pip pts")?;
    Ok(pip(&rx, &ry, &px, &pyv).into_pyarray(py))
}

/// parkedaircraft._seg_dist: (m, k) distances from pts (m, 2) to the segments a -> b (k, 2).
#[pyfunction]
fn pa_seg_dist<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, a: PyReadonlyArray2<'py, f64>,
                    b: PyReadonlyArray2<'py, f64>) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (px, pyv) = two(&pts, "pa_seg_dist pts")?;
    let (ax, ay) = two(&a, "pa_seg_dist a")?;
    let (bx, by) = two(&b, "pa_seg_dist b")?;
    if ax.len() != bx.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("pa_seg_dist: a, b (k, 2)"));
    }
    let (m, k) = (px.len(), ax.len());
    let mut out = vec![0.0f64; m * k];
    for i in 0..m {
        for j in 0..k {
            let (abx, aby) = (bx[j] - ax[j], by[j] - ay[j]);
            let (apx, apy) = (px[i] - ax[j], pyv[i] - ay[j]);
            let den = abx * abx + aby * aby;
            let t = (apx * abx + apy * aby) / if den > 1e-12 { den } else { 1e-12 };
            let t = if t < 0.0 { 0.0 } else if t > 1.0 { 1.0 } else { t };
            out[i * k + j] = (apx - t * abx).dhypot(apy - t * aby);
        }
    }
    Ok(numpy::ndarray::Array2::from_shape_vec((m, k), out).unwrap().into_pyarray(py))
}

/// parkedaircraft._bridge: the small buildings (rings: points (n, 2), starts (S + 1,), their obstacle indexes (S,)) touching
/// the gate (gx, gy) within attach_m; of those reaching min_m or more along `out`, (the farthest one's tip capped at cap_m,
/// lateral centre, half width) and all their indexes; None without.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn pa_bridge(pts: PyReadonlyArray2<'_, f64>, starts: PyReadonlyArray1<'_, i64>, oi: PyReadonlyArray1<'_, i64>, gx: f64, gy: f64,
             out: (f64, f64), attach_m: f64, min_m: f64, cap_m: f64) -> PyResult<Option<(f64, f64, f64, Vec<i64>)>> {
    let (x, y) = two(&pts, "pa_bridge")?;
    let (s, o) = (starts.as_array(), oi.as_array());
    if s.len() != o.len() + 1 {
        return Err(pyo3::exceptions::PyValueError::new_err("pa_bridge: starts (S + 1,), indexes (S,)"));
    }
    let tan = (-out.1, out.0);
    let mut best: Option<(f64, f64, f64)> = None;
    let mut ids = vec![];
    for r in 0..o.len() {
        let (a, b) = (s[r], s[r + 1]);
        if a < 0 || b < a || b as usize > x.len() {
            return Err(pyo3::exceptions::PyValueError::new_err("pa_bridge: starts rising within the points"));
        }
        let (a, b) = (a as usize, b as usize);
        if a == b { continue; }
        let mut near = f64::INFINITY;
        for i in a..b {
            let d = (x[i] - gx).dhypot(y[i] - gy);
            if d < near { near = d; }
        }
        if near > attach_m { continue; }
        let (mut tmax, mut lmin, mut lmax) = (f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
        for i in a..b {
            let (dx, dy) = (x[i] - gx, y[i] - gy);
            let tip = dx * out.0 + dy * out.1;
            let lat = dx * tan.0 + dy * tan.1;
            if tip > tmax { tmax = tip; }
            if lat < lmin { lmin = lat; }
            if lat > lmax { lmax = lat; }
        }
        if tmax >= min_m {
            ids.push(o[r]);
            if best.map_or(true, |bb| tmax > bb.0) {
                best = Some((if cap_m < tmax { cap_m } else { tmax }, (lmin + lmax) / 2.0, (lmax - lmin) / 2.0));
            }
        }
    }
    Ok(best.map(|(t, c, h)| (t, c, h, ids)))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(pa_area, m)?)?;
    m.add_function(wrap_pyfunction!(pa_pip, m)?)?;
    m.add_function(wrap_pyfunction!(pa_seg_dist, m)?)?;
    m.add_function(wrap_pyfunction!(pa_bridge, m)?)?;
    Ok(())
}
