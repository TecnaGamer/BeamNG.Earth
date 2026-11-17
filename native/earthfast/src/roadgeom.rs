//! Road geometry helpers off the worker processes' Python loops (2026-10-23: a Steam Deck's road, block and far
//! building processes each spent 14-21 CPU-s of a teleport in roads._dedupe alone).
use crate::dm::Dm;

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
    let d = |i: usize, j: usize| -> f64 { (a[[i, 0]] - a[[j, 0]]).dhypot(a[[i, 1]] - a[[j, 1]]) };
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
    let z: Vec<f64> = z0.iter().copied().collect();
    let (sv, fv, cv): (Vec<f64>, Vec<f64>, Vec<f64>) =
        (s.iter().copied().collect(), floor.iter().copied().collect(), cap.iter().copied().collect());
    match curve_deck_radius(&sv, &z, &fv, &cv, 300.0) {
        Some(x) => x.into_pyarray(py),
        None => z.into_pyarray(py),
    }
}

/// curve_deck's projections with the vertical radius r (m): Some(heights) when they meet, None when they do not or
/// when s does not increase (z shorter than 3 points: Some(z)). The slices are of one length (the callers check).
fn curve_deck_radius(s: &[f64], z: &[f64], floor: &[f64], cap: &[f64], r: f64) -> Option<Vec<f64>> {
    let n = z.len();
    if n < 3 {
        return Some(z.to_vec());
    }
    let ds: Vec<f64> = (0..n - 1).map(|i| s[i + 1] - s[i]).collect();
    if ds.iter().any(|d| *d <= 0.0) {
        return None;
    }
    let mut lo: Vec<f64> = (0..n).map(|i| np_min(floor[i], cap[i])).collect();
    let mut hi: Vec<f64> = (0..n).map(|i| cap[i]).collect();
    for e in [0, n - 1] {
        hi[e] = z[e];
        lo[e] = z[e];
    }
    let m = n - 2;
    let two_r = 2.0 * r;        // (600 at 300 m: the same divisor as before, bit for bit)
    let limit: Vec<f64> = (0..m).map(|i| (ds[i] + ds[i + 1]) / two_r).collect();
    let w: Vec<[f64; 3]> = (0..m).map(|i| [1.0 / ds[i], -1.0 / ds[i] - 1.0 / ds[i + 1], 1.0 / ds[i + 1]]).collect();
    let norm: Vec<f64> = w.iter().map(|r| r[0] * r[0] + r[1] * r[1] + r[2] * r[2]).collect();
    let mut x = z.to_vec();
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
            return Some(x);
        }
    }
    None
}

/// roads._curve_deck in speed mode (BNE_DECK_K_SPEED, the road revamp's R1e): curve_deck with each vertical radius of
/// radii (m) in turn, as the caller orders them (longest first); the first that meets its bounds wins. Returns
/// (heights, the index into radii used, -1 none: z itself). ValueError on arrays of different lengths or a radius
/// that is not a positive finite number (map data never reaches a panic).
#[pyfunction]
pub fn curve_deck_radii<'py>(
    py: Python<'py>,
    s: PyReadonlyArray1<'py, f64>,
    z: PyReadonlyArray1<'py, f64>,
    floor: PyReadonlyArray1<'py, f64>,
    cap: PyReadonlyArray1<'py, f64>,
    radii: PyReadonlyArray1<'py, f64>,
) -> PyResult<(Bound<'py, PyArray1<f64>>, i64)> {
    let sv: Vec<f64> = s.as_array().iter().copied().collect();
    let zv: Vec<f64> = z.as_array().iter().copied().collect();
    let fv: Vec<f64> = floor.as_array().iter().copied().collect();
    let cv: Vec<f64> = cap.as_array().iter().copied().collect();
    let n = zv.len();
    if sv.len() != n || fv.len() != n || cv.len() != n {
        return Err(pyo3::exceptions::PyValueError::new_err("curve_deck_radii: arrays of different lengths"));
    }
    let rv: Vec<f64> = radii.as_array().iter().copied().collect();
    if rv.iter().any(|r| !(r.is_finite() && *r > 0.0)) {
        return Err(pyo3::exceptions::PyValueError::new_err("curve_deck_radii: a radius not positive and finite"));
    }
    for (k, r) in rv.iter().enumerate() {
        if let Some(x) = curve_deck_radius(&sv, &zv, &fv, &cv, *r) {
            return Ok((x.into_pyarray(py), k as i64));
        }
    }
    Ok((zv.into_pyarray(py), -1))
}

/// Crest vertical curve radius (m, 100 K) a road needs at its design speed (the road revamp's R1e, plan §2.6; AASHTO
/// stopping sight distance S = 0.278 V 2.5 + 0.039 V^2 / 3.4 m, crest K = S^2 / 658): per row of rows (n, 4): speed
/// limit km/h (NaN none), class rank, urban (0/1), link (0/1), the design speed as the speed tapers take it
/// (tapers::design_speed_kmh). 150 m at 30 km/h, 2,530 m at 80, 9,390 m at 120.
#[pyfunction]
pub fn deck_crest_radii<'py>(py: Python<'py>, rows: PyReadonlyArray2<'py, f64>) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let a = rows.as_array();
    if a.shape()[1] != 4 {
        return Err(pyo3::exceptions::PyValueError::new_err("deck_crest_radii: rows must be (n, 4)"));
    }
    let out: Vec<f64> = (0..a.shape()[0])
        .map(|i| {
            let v = crate::tapers::design_speed_kmh(a[[i, 0]], a[[i, 1]], a[[i, 2]] > 0.5, a[[i, 3]] > 0.5);
            crest_radius_m(v)
        })
        .collect();
    Ok(out.into_pyarray(py))
}

/// 100 x AASHTO's crest K at v km/h (deck_crest_radii). Plain arithmetic: the same on every platform.
pub(crate) fn crest_radius_m(v: f64) -> f64 {
    let s = 0.278 * v * 2.5 + 0.039 * v * v / 3.4;
    100.0 * s * s / 658.0
}

#[cfg(test)]
mod crest_tests {
    use super::*;

    #[test]
    fn crest_radii_match_the_plan_table() {
        for (v, r) in [(30.0, 150.0), (80.0, 2530.0), (120.0, 9390.0)] {
            assert!((crest_radius_m(v) - r).abs() / r < 0.02, "{v}: {}", crest_radius_m(v));
        }
    }

    #[test]
    fn a_radius_too_long_for_the_rise_does_not_meet() {
        let s: Vec<f64> = (0..101).map(|i| i as f64 * 4.0).collect();
        let z: Vec<f64> = s.iter().map(|x| if (160.0..=240.0).contains(x) { 6.0 } else { 0.0 }).collect();
        let fl: Vec<f64> = s.iter().map(|x| if (180.0..=220.0).contains(x) { 6.0 } else { 0.0 }).collect();
        let cap = vec![50.0; 101];
        assert!(curve_deck_radius(&s, &z, &fl, &cap, 100.0).is_some());
        assert!(curve_deck_radius(&s, &z, &fl, &cap, 1.0e6).is_none(), "a 6 m rise in 180 m at R 1000 km cannot");
    }
}
