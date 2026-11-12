//! lanesections.py's numeric kernels (PR #880 by DerEifrige-f, ported 2026-11-12):
//! - geodesic_lengths: WGS84 geodesic metres between consecutive lon/lat vertices (Vincenty's inverse), the metric a
//!   merged highway's per-source lane stretches are located in (their fractions along the feature);
//! - lane_runs: the pieces of a lane separator where the growing carriageway has room for it (room >= 0), each with
//!   the metres along the line before its start (its dashes' phase).
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

const A: f64 = 6378137.0;
const F: f64 = 1.0 / 298.257223563;
const B: f64 = A * (1.0 - F);
const R_MEAN: f64 = 6371008.8;

/// Vincenty's inverse distance of one edge (radians); the mean sphere's haversine when it does not converge (near
/// antipodal: no road edge).
fn vincenty(lon1: f64, lat1: f64, lon2: f64, lat2: f64) -> f64 {
    let u1 = ((1.0 - F) * lat1.tan()).atan();
    let u2 = ((1.0 - F) * lat2.tan()).atan();
    let (s1, c1) = u1.sin_cos();
    let (s2, c2) = u2.sin_cos();
    let pi = std::f64::consts::PI;
    let l = (lon2 - lon1 + pi).rem_euclid(2.0 * pi) - pi;
    let mut lam = l;
    let (mut ss, mut cs, mut sigma, mut ca2, mut mid) = (0.0, 1.0, 0.0, 1.0, 0.0);
    let mut converged = false;
    for _ in 0..200 {
        let (sl, cl) = lam.sin_cos();
        ss = (c2 * sl).hypot(c1 * s2 - s1 * c2 * cl);
        cs = s1 * s2 + c1 * c2 * cl;
        sigma = ss.atan2(cs);
        let sa = if ss > 1e-15 { c1 * c2 * sl / ss } else { 0.0 };
        ca2 = (1.0 - sa * sa).max(0.0);
        mid = if ca2 > 1e-15 { cs - 2.0 * s1 * s2 / ca2 } else { 0.0 };
        let c = F / 16.0 * ca2 * (4.0 + F * (4.0 - 3.0 * ca2));
        let next = l + (1.0 - c) * F * sa * (sigma + c * ss * (mid + c * cs * (-1.0 + 2.0 * mid * mid)));
        let done = (next - lam).abs() <= 1e-12;
        lam = next;
        if done {
            converged = true;
            break;
        }
    }
    if !converged {
        let h = ((lat2 - lat1) / 2.0).sin().powi(2) + lat1.cos() * lat2.cos() * (l / 2.0).sin().powi(2);
        return 2.0 * R_MEAN * h.clamp(0.0, 1.0).sqrt().asin();
    }
    let u2_ = ca2 * (A * A - B * B) / (B * B);
    let aa = 1.0 + u2_ / 16384.0 * (4096.0 + u2_ * (-768.0 + u2_ * (320.0 - 175.0 * u2_)));
    let bb = u2_ / 1024.0 * (256.0 + u2_ * (-128.0 + u2_ * (74.0 - 47.0 * u2_)));
    let ds = bb * ss * (mid + bb / 4.0 * (cs * (-1.0 + 2.0 * mid * mid)
        - bb / 6.0 * mid * (-3.0 + 4.0 * ss * ss) * (-3.0 + 4.0 * mid * mid)));
    B * aa * (sigma - ds)
}

pub fn lengths(ll: &[[f64; 2]]) -> Vec<f64> {
    let r = std::f64::consts::PI / 180.0;
    ll.windows(2).map(|w| vincenty(w[0][0] * r, w[0][1] * r, w[1][0] * r, w[1][1] * r)).collect()
}

/// geodesy.segment_lengths: (k - 1,) metres between the consecutive lon/lat rows of (k, >= 2).
#[pyfunction]
pub fn geodesic_lengths<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let a = lonlat.as_array();
    if a.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("geodesic_lengths: lon/lat columns"));
    }
    let ll: Vec<[f64; 2]> = (0..a.nrows()).map(|k| [a[[k, 0]], a[[k, 1]]]).collect();
    Ok(lengths(&ll).into_pyarray(py))
}

/// The runs of line (k, c >= 2) where room >= 0 (k,), cut where room crosses zero (linear), each as (first row,
/// number of rows) into the stacked points and its metres along line before it (xy distance).
pub fn runs(line: &[f64], c: usize, room: &[f64]) -> (Vec<f64>, Vec<(usize, usize, f64)>) {
    let k = room.len();
    let mut pts: Vec<f64> = Vec::new();
    let mut out = Vec::new();
    if k == 0 {
        return (pts, out);
    }
    let p = |i: usize| &line[i * c..(i + 1) * c];
    let mut dist = vec![0.0; k];
    for i in 1..k {
        let (a, b) = (p(i - 1), p(i));
        dist[i] = dist[i - 1] + (b[0] - a[0]).hypot(b[1] - a[1]);
    }
    if room.iter().all(|r| *r >= -1e-6) {
        pts.extend_from_slice(&line[..k * c]);
        out.push((0, k, 0.0));
        return (pts, out);
    }
    let keep: Vec<bool> = room.iter().map(|r| *r >= 0.0).collect();
    let mut i = 0;
    while i < k {
        if !keep[i] {
            i += 1;
            continue;
        }
        let a = i;
        while i < k && keep[i] {
            i += 1;
        }
        let b = i;
        let start = pts.len() / c;
        let mut before = dist[a];
        if a > 0 {
            let t = -room[a - 1] / (room[a] - room[a - 1]);
            let (q0, q1) = (p(a - 1), p(a));
            for j in 0..c {
                pts.push(q0[j] + t * (q1[j] - q0[j]));
            }
            before = dist[a - 1] + t * (dist[a] - dist[a - 1]);
        }
        pts.extend_from_slice(&line[a * c..b * c]);
        if b < k {
            let t = -room[b - 1] / (room[b] - room[b - 1]);
            let (q0, q1) = (p(b - 1), p(b));
            for j in 0..c {
                pts.push(q0[j] + t * (q1[j] - q0[j]));
            }
        }
        let n = pts.len() / c - start;
        if n >= 2 {
            out.push((start, n, before));
        } else {
            pts.truncate(start * c);
        }
    }
    (pts, out)
}

/// roads._lane_runs: (points (m, c), starts (r + 1,) rows into points, before (r,) m along the line).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn lane_runs<'py>(py: Python<'py>, line: PyReadonlyArray2<'py, f64>, room: PyReadonlyArray1<'py, f64>)
    -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<f64>>)> {
    let la = line.as_array();
    let ra = room.as_array();
    let (k, c) = (la.nrows(), la.ncols());
    if c < 2 || ra.len() != k {
        return Err(pyo3::exceptions::PyValueError::new_err("lane_runs: a line (k, >= 2) and k room values"));
    }
    let lv: Vec<f64> = la.iter().copied().collect();
    let rv: Vec<f64> = ra.iter().copied().collect();
    let (pts, rr) = runs(&lv, c, &rv);
    let m = pts.len() / c;
    let mut starts: Vec<i64> = rr.iter().map(|r| r.0 as i64).collect();
    starts.push(m as i64);
    let before: Vec<f64> = rr.iter().map(|r| r.2).collect();
    let arr = numpy::ndarray::Array2::from_shape_vec((m, c), pts).unwrap();
    Ok((arr.into_pyarray(py), starts.into_pyarray(py), before.into_pyarray(py)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_degree_of_the_equator() {
        let d = lengths(&[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]);
        assert!((d[0] - A * std::f64::consts::PI / 180.0).abs() < 1e-5);
        assert!((d[1] - 110574.3885578).abs() < 1e-5);
        assert_eq!(lengths(&[[10.0, 48.0], [10.0, 48.0]]), vec![0.0]);
    }

    #[test]
    fn runs_cut_at_zero_room() {
        let line = [0.0, 0.0, 10.0, 0.0, 20.0, 0.0];
        let (pts, rr) = runs(&line, 2, &[-1.0, 1.0, 1.0]);
        assert_eq!(rr.len(), 1);
        assert!((pts[0] - 5.0).abs() < 1e-12 && (rr[0].2 - 5.0).abs() < 1e-12);
    }
}
