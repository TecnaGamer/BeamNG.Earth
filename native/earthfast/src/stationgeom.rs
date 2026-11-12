//! The bus and railway stations' small geometry (server busstations.contains / dist_to_path, railstations._crossings):
//! a station group's layout called them thousands of times on numpy rows of a few points each, ~3 CPU-s of a Dijon
//! group per process (2026-11-11). The numbers are the Python steps' to the bit, in their order: even-odd crossings
//! with the same expression, numpy's distance to a polyline (squares summed, np.maximum, np.clip, libm hypot, the
//! first minimum or NaN), the track crossings' parameters and arc lengths (hypot per segment, summed in order) sorted
//! stably by arc.
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// busstations._open: the ring's rows without a closing copy of its first vertex (within 1e-6 on both axes).
fn open_len(r: &numpy::ndarray::ArrayView2<f64>) -> usize {
    let n = r.nrows();
    if n >= 2 {
        let d = (r[[0, 0]] - r[[n - 1, 0]]).abs().max((r[[0, 1]] - r[[n - 1, 1]]).abs());
        // (float(np.max(np.abs(...))) < 1e-6: np.max of a NaN is NaN, and NaN < 1e-6 is false)
        let d = if (r[[0, 0]] - r[[n - 1, 0]]).is_nan() || (r[[0, 1]] - r[[n - 1, 1]]).is_nan() { f64::NAN } else { d };
        if d < 1e-6 {
            return n - 1;
        }
    }
    n
}

/// busstations.centroid's area centroid of a ring xy (k, c >= 2): opened (its last row dropped when every column is
/// within 1e-6 of the first's), then with 3 or more points and an area over 1e-6 the shoelace centroid, numpy's
/// pairwise sums of cr = x * roll(y, -1) - roll(x, -1) * y; None where the caller takes the mean (kernel 235: ~35,000
/// numpy centroids of a station group's items, 2.7 % of an I-83 Baltimore road probe, 2026-11-12).
#[pyfunction]
pub fn ring_centroid(xy: PyReadonlyArray2<'_, f64>) -> PyResult<Option<(f64, f64)>> {
    let r = xy.as_array();
    let (mut n, c) = (r.nrows(), r.ncols());
    if c < 2 {
        return Ok(None);
    }
    if n >= 2 {
        let mut d = f64::NEG_INFINITY;
        for j in 0..c {
            let v = (r[[0, j]] - r[[n - 1, j]]).abs();
            d = if d.is_nan() || v.is_nan() { f64::NAN } else { d.max(v) };
        }
        if d < 1e-6 {
            n -= 1;
        }
    }
    if n < 3 {
        return Ok(None);
    }
    let nx = |i: usize| (i + 1) % n;
    let cr: Vec<f64> = (0..n).map(|i| r[[i, 0]] * r[[nx(i), 1]] - r[[nx(i), 0]] * r[[i, 1]]).collect();
    let a = crate::junctionsnap::np_sum(&cr) * 0.5;
    if !(a.abs() > 1e-6) {
        return Ok(None);
    }
    let sx: Vec<f64> = (0..n).map(|i| (r[[i, 0]] + r[[nx(i), 0]]) * cr[i]).collect();
    let sy: Vec<f64> = (0..n).map(|i| (r[[i, 1]] + r[[nx(i), 1]]) * cr[i]).collect();
    Ok(Some((crate::junctionsnap::np_sum(&sx) / (6.0 * a), crate::junctionsnap::np_sum(&sy) / (6.0 * a))))
}

/// busstations.contains(ring, p): p inside the polygon (even-odd), ring opened first.
fn contains(r: &numpy::ndarray::ArrayView2<f64>, n: usize, x: f64, y: f64) -> bool {
    let mut inside = false;
    if n == 0 {
        return false;
    }
    let mut j = n - 1;
    for i in 0..n {
        let (xi, yi, xj, yj) = (r[[i, 0]], r[[i, 1]], r[[j, 0]], r[[j, 1]]);
        if (yi > y) != (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi {
            inside = !inside;
        }
        j = i;
    }
    inside
}

/// [busstations.contains(ring, p) for p in pts] (pts (k, 2)).
#[pyfunction]
pub fn station_contains<'py>(py: Python<'py>, ring: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>)
                             -> Bound<'py, PyArray1<bool>> {
    let (r, p) = (ring.as_array(), pts.as_array());
    let n = open_len(&r);
    let out: Vec<bool> = (0..p.nrows()).map(|k| contains(&r, n, p[[k, 0]], p[[k, 1]])).collect();
    out.into_pyarray(py)
}

/// np.clip(x, 0, 1) (NaN through).
fn clip01(x: f64) -> f64 {
    if x.is_nan() { x } else if x < 0.0 { 0.0 } else if x > 1.0 { 1.0 } else { x }
}

/// busstations.dist_to_path(path, p, closed) for one point, path's rows (closed: its opened rows and the first again).
fn dist(px: &[f64], py_: &[f64], x: f64, y: f64) -> f64 {
    let n = px.len();
    if n == 1 {
        return (px[0] - x).hypot(py_[0] - y);
    }
    let mut best = f64::NAN;
    for k in 0..n - 1 {
        let (ax, ay) = (px[k], py_[k]);
        let (abx, aby) = (px[k + 1] - ax, py_[k + 1] - ay);
        let s2 = abx * abx + aby * aby;
        let ab2 = if s2.is_nan() || s2 > 1e-9 { s2 } else { 1e-9 };     // (np.maximum: NaN through)
        let t = clip01(((x - ax) * abx + (y - ay) * aby) / ab2);
        let d = (ax + t * abx - x).hypot(ay + t * aby - y);
        if k == 0 || d.is_nan() || d < best {
            best = d;
            if d.is_nan() {
                return d;       // (.min(): NaN wins)
            }
        }
    }
    best
}

/// [busstations.dist_to_path(path, p, closed) for p in pts] (pts (k, 2)); an empty path gives NaN (numpy raised).
#[pyfunction]
pub fn station_path_dist<'py>(py: Python<'py>, path: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>,
                              closed: bool) -> Bound<'py, PyArray1<f64>> {
    let (a, p) = (path.as_array(), pts.as_array());
    let n = if closed { open_len(&a) } else { a.nrows() };
    let mut xs: Vec<f64> = (0..n).map(|i| a[[i, 0]]).collect();
    let mut ys: Vec<f64> = (0..n).map(|i| a[[i, 1]]).collect();
    if closed && n > 0 {
        xs.push(a[[0, 0]]);
        ys.push(a[[0, 1]]);
    }
    let out: Vec<f64> = (0..p.nrows())
        .map(|k| if xs.is_empty() { f64::NAN } else { dist(&xs, &ys, p[[k, 0]], p[[k, 1]]) })
        .collect();
    out.into_pyarray(py)
}

/// railstations._crossings(path, tracks): [(track index, arc length m along path)] where the path crosses a track, in
/// order of arc (stable: path segment, then track, then track segment). tracks: their rows one after another (m, 2),
/// track k's rows off[k]..off[k + 1].
#[pyfunction]
pub fn station_crossings(path: PyReadonlyArray2<'_, f64>, tracks: PyReadonlyArray2<'_, f64>,
                         off: PyReadonlyArray1<'_, i64>) -> Vec<(usize, f64)> {
    let (p, t, o) = (path.as_array(), tracks.as_array(), off.as_array());
    let nt = o.len().saturating_sub(1);
    let mut out: Vec<(usize, f64)> = Vec::new();
    let mut s0 = 0.0f64;
    for i in 0..p.nrows().saturating_sub(1) {
        let (a0x, a0y, a1x, a1y) = (p[[i, 0]], p[[i, 1]], p[[i + 1, 0]], p[[i + 1, 1]]);
        let seglen = (a1x - a0x).hypot(a1y - a0y);
        let (rx, ry) = (a1x - a0x, a1y - a0y);
        for k in 0..nt {
            let (j0, j1) = (o[k] as usize, o[k + 1] as usize);
            for j in j0..j1.saturating_sub(1) {
                let (b0x, b0y) = (t[[j, 0]], t[[j, 1]]);
                let (sx, sy) = (t[[j + 1, 0]] - b0x, t[[j + 1, 1]] - b0y);
                let den = rx * sy - ry * sx;
                if den.abs() < 1e-9 || den.is_nan() {
                    continue;   // (abs(den) < 1e-9: None; a NaN den gives NaN t, u: not in [0, 1])
                }
                let (qx, qy) = (b0x - a0x, b0y - a0y);
                let tt = (qx * sy - qy * sx) / den;
                let uu = (qx * ry - qy * rx) / den;
                if (0.0..=1.0).contains(&tt) && (0.0..=1.0).contains(&uu) {
                    out.push((k, s0 + tt * seglen));
                }
            }
        }
        s0 += seglen;
    }
    // (sorted(..., key=arc): stable; arcs of a NaN path keep their order as Python's comparisons would not move them)
    out.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal));
    out
}
