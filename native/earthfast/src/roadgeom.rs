//! Road geometry helpers off the worker processes' Python loops (2026-10-23: a Steam Deck's road, block and far
//! building processes each spent 14-21 CPU-s of a teleport in roads._dedupe alone).

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// roads._dedupe: the indices of a polyline's points kept when every point closer than tol to the previous kept one
/// is dropped (the first and the last kept; a last kept point within tol of the end is replaced by the end).
#[pyfunction]
pub fn dedupe_polyline<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, tol: f64) -> Bound<'py, PyArray1<i64>> {
    let a = xy.as_array();
    let n = a.shape()[0];
    if n < 3 {
        return (0..n as i64).collect::<Vec<_>>().into_pyarray(py);
    }
    let d = |i: usize, j: usize| -> f64 { (a[[i, 0]] - a[[j, 0]]).hypot(a[[i, 1]] - a[[j, 1]]) };
    let mut keep: Vec<usize> = vec![0];
    for k in 1..n - 1 {
        if d(k, *keep.last().unwrap()) >= tol {
            keep.push(k);
        }
    }
    if d(n - 1, *keep.last().unwrap()) < tol && keep.len() > 1 {
        *keep.last_mut().unwrap() = n - 1;
    } else {
        keep.push(n - 1);
    }
    keep.into_iter().map(|k| k as i64).collect::<Vec<_>>().into_pyarray(py)
}

// numpy's maximum / minimum: NaN propagates
fn np_max(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else if a >= b {
        a
    } else {
        b
    }
}

fn np_min(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() {
        f64::NAN
    } else if a <= b {
        a
    } else {
        b
    }
}

fn np_clip(v: f64, lo: f64, hi: f64) -> f64 {
    np_min(np_max(v, lo), hi)
}

/// roads._curve_deck: a bridge deck's profile z (at arcs s) projected onto its bounds [min(floor, cap), cap] and a
/// change of grade of at most 1/300 per metre (a 300 m vertical radius), by Dykstra's alternating projections (at most
/// 2000 rounds), its ends pinned. z itself when they do not meet (map constraints that cannot all hold) or when s does
/// not increase. (After wyo-backroads' PR #374; in Python each deck took ~170 ms of a road tile set: downtown Los
/// Angeles' 36 tiles 45 -> 64 s.)
#[pyfunction]
pub fn curve_deck<'py>(
    py: Python<'py>,
    s: PyReadonlyArray1<'py, f64>,
    z: PyReadonlyArray1<'py, f64>,
    floor: PyReadonlyArray1<'py, f64>,
    cap: PyReadonlyArray1<'py, f64>,
) -> Bound<'py, PyArray1<f64>> {
    let (s, z0, floor, cap) = (s.as_array(), z.as_array(), floor.as_array(), cap.as_array());
    let n = z0.len();
    let z: Vec<f64> = z0.iter().copied().collect();
    if n < 3 {
        return z.into_pyarray(py);
    }
    let ds: Vec<f64> = (0..n - 1).map(|i| s[i + 1] - s[i]).collect();
    if ds.iter().any(|d| *d <= 0.0) {
        return z.into_pyarray(py);
    }
    let mut lo: Vec<f64> = (0..n).map(|i| np_min(floor[i], cap[i])).collect();
    let mut hi: Vec<f64> = (0..n).map(|i| cap[i]).collect();
    for e in [0, n - 1] {
        hi[e] = z[e];
        lo[e] = z[e];
    }
    let m = n - 2;
    let limit: Vec<f64> = (0..m).map(|i| (ds[i] + ds[i + 1]) / 600.0).collect();
    let w: Vec<[f64; 3]> = (0..m).map(|i| [1.0 / ds[i], -1.0 / ds[i] - 1.0 / ds[i + 1], 1.0 / ds[i + 1]]).collect();
    let norm: Vec<f64> = w.iter().map(|r| r[0] * r[0] + r[1] * r[1] + r[2] * r[2]).collect();
    let mut x = z.clone();
    let mut bc = vec![0.0f64; n];
    let mut corr = vec![[0.0f64; 3]; m];
    for _ in 0..2000 {
        for i in 0..n {
            let y = x[i] + bc[i];
            x[i] = np_clip(y, lo[i], hi[i]);
            bc[i] = y - x[i];
        }
        for colour in 0..3 {
            let mut k = colour;
            while k < m {
                let r = &w[k];
                let y = [x[k] + corr[k][0], x[k + 1] + corr[k][1], x[k + 2] + corr[k][2]];
                let bend = y[0] * r[0] + y[1] * r[1] + y[2] * r[2];
                let delta = (bend - np_clip(bend, -limit[k], limit[k])) / norm[k];
                for j in 0..3 {
                    let p = y[j] - delta * r[j];
                    corr[k][j] = y[j] - p;
                    x[k + j] = p;
                }
                k += 3;
            }
        }
        let inside = (0..n).all(|i| x[i] >= lo[i] - 1e-7 && x[i] <= hi[i] + 1e-7);
        if inside
            && (0..m).all(|k| (x[k] * w[k][0] + x[k + 1] * w[k][1] + x[k + 2] * w[k][2]).abs() <= limit[k] + 1e-7)
        {
            x[0] = z[0];
            x[n - 1] = z[n - 1];
            return x.into_pyarray(py);
        }
    }
    z.into_pyarray(py)
}
