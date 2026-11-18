//! Australian road scenery's distance tests (aurails, kantstolpar, warnsigns): motesfri._distance's numpy
//! broadcast per point and segment (the squared length floored at 1e-12, the clipped projection, np.hypot of
//! p - (a + t ab)), bit for bit, in loops instead of one (points x segments) array per line.
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray2};
use numpy::ndarray::ArrayView2;
use pyo3::prelude::*;

use crate::dm::Dm;

/// (distance, t, l2) of (px, py) to the segment a + t ab, numpy's order of operations.
#[inline]
fn seg(ax: f64, ay: f64, bx: f64, by: f64, px: f64, py: f64) -> (f64, f64, f64) {
    let (dx, dy) = (bx - ax, by - ay);
    let l2 = (dx * dx + dy * dy).max(1e-12);
    let t = (((px - ax) * dx + (py - ay) * dy) / l2).clamp(0.0, 1.0);
    ((px - (ax + t * dx)).dhypot(py - (ay + t * dy)), t, l2)
}

/// motesfri._distance for one point: the least over the polyline's segments (inf for fewer than 2 points).
#[inline]
fn line_min(l: &ArrayView2<f64>, px: f64, py: f64) -> f64 {
    let mut best = f64::INFINITY;
    for i in 0..l.nrows().saturating_sub(1) {
        let d = seg(l[[i, 0]], l[[i, 1]], l[[i + 1, 0]], l[[i + 1, 1]], px, py).0;
        best = best.min(d);
    }
    best
}

/// motesfri._distance(points, line): each point's (n, 2) distance to the polyline line (k >= 2, 2).
#[pyfunction]
pub fn line_dists<'py>(py: Python<'py>, points: PyReadonlyArray2<'py, f64>, line: PyReadonlyArray2<'py, f64>)
                       -> Bound<'py, PyArray1<f64>> {
    let (p, l) = (points.as_array(), line.as_array());
    let v: Vec<f64> = (0..p.nrows()).map(|i| line_min(&l, p[[i, 0]], p[[i, 1]])).collect();
    v.into_pyarray(py)
}

/// Per point (n, 2): farther than reaches[j] from every polyline lines[j], and farther than rail_reach from every
/// segment of rails (m, 4: x1 y1 x2 y2), as kantstolpar's and warnsigns' `motesfri._distance(q, line) > reach` tests.
#[pyfunction]
pub fn lines_clear<'py>(py: Python<'py>, points: PyReadonlyArray2<'py, f64>, lines: Vec<PyReadonlyArray2<'py, f64>>,
                        reaches: Vec<f64>, rails: PyReadonlyArray2<'py, f64>, rail_reach: f64)
                        -> PyResult<Bound<'py, PyArray1<bool>>> {
    if lines.len() != reaches.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("lines_clear: a reach per line"));
    }
    let (p, r) = (points.as_array(), rails.as_array());
    let lv: Vec<ArrayView2<f64>> = lines.iter().map(|l| l.as_array()).collect();
    let v: Vec<bool> = (0..p.nrows()).map(|i| {
        let (px, py) = (p[[i, 0]], p[[i, 1]]);
        lv.iter().zip(&reaches).all(|(l, &reach)| line_min(l, px, py) > reach)
            && (0..r.nrows()).all(|k| seg(r[[k, 0]], r[[k, 1]], r[[k, 2]], r[[k, 3]], px, py).0 > rail_reach)
    }).collect();
    Ok(v.into_pyarray(py))
}

/// aurails._nearest: (distance (n,), nearest point (n, 2), unit direction of that segment (n, 2)) of points (n, 2)
/// to the polyline line (k >= 2, >= 2); the first segment of the least distance.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn line_nearest<'py>(py: Python<'py>, points: PyReadonlyArray2<'py, f64>, line: PyReadonlyArray2<'py, f64>)
                         -> (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>) {
    let (p, l) = (points.as_array(), line.as_array());
    let n = p.nrows();
    let (mut dist, mut q, mut dir) = (vec![0.0; n], vec![[0.0; 2]; n], vec![[0.0; 2]; n]);
    for i in 0..n {
        let (px, py) = (p[[i, 0]], p[[i, 1]]);
        let mut best = (f64::INFINITY, 0usize, 0.0, 1e-12);
        for k in 0..l.nrows().saturating_sub(1) {
            let (d, t, l2) = seg(l[[k, 0]], l[[k, 1]], l[[k + 1, 0]], l[[k + 1, 1]], px, py);
            if d < best.0 || k == 0 {
                best = (d, k, t, l2);
            }
        }
        let k = best.1;
        let (ax, ay, dx, dy) = (l[[k, 0]], l[[k, 1]], l[[k + 1, 0]] - l[[k, 0]], l[[k + 1, 1]] - l[[k, 1]]);
        dist[i] = best.0;
        q[i] = [ax + best.2 * dx, ay + best.2 * dy];
        let s = best.3.sqrt();
        dir[i] = [dx / s, dy / s];
    }
    let to2 = |v: Vec<[f64; 2]>| numpy::ndarray::Array2::from_shape_vec((n, 2), v.concat()).unwrap().into_pyarray(py);
    (dist.into_pyarray(py), to2(q), to2(dir))
}

/// aurails._mapped_dist: each point's (n, 2) distance to the nearest of the segments rails (m, 4) (inf if none).
#[pyfunction]
pub fn segs_min_dist<'py>(py: Python<'py>, points: PyReadonlyArray2<'py, f64>, rails: PyReadonlyArray2<'py, f64>)
                          -> Bound<'py, PyArray1<f64>> {
    let (p, r) = (points.as_array(), rails.as_array());
    let v: Vec<f64> = (0..p.nrows()).map(|i| {
        (0..r.nrows()).fold(f64::INFINITY, |m, k| m.min(seg(r[[k, 0]], r[[k, 1]], r[[k, 2]], r[[k, 3]], p[[i, 0]], p[[i, 1]]).0))
    }).collect();
    v.into_pyarray(py)
}
