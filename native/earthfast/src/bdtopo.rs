//! France's measured buildings (server/bdtopo.py): IGN BD TOPO `batiment` records joined to the map's footprints.
//!
//! - `bdtopo_points`: one point inside each BD TOPO outline (its area centroid, or where that falls outside an L / U
//!   shaped outline the middle of the widest span across it at the centroid's latitude) and the outline's area in m2.
//!   Run once per downloaded map cell (~5 400 outlines in a central Paris cell), off the tiles' path.
//! - `bdtopo_match`: for each map footprint (its outer ring), the record whose point stands inside it, the largest
//!   record when several do (one OSM outline over two cadastre buildings); -1 none. Run once per decoded buildings
//!   layer of a French cell, on the tiles' path: a grid of the records (~40 m bins), then an even-odd test against the
//!   few records under each footprint's box.
//! Both pure functions of their inputs.

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use numpy::ndarray::Array2;
use pyo3::prelude::*;
use std::collections::HashMap;

const M_PER_DEG: f64 = 111_320.0;
const BIN_DEG: f64 = 0.0005;

/// Even-odd crossings of a ray toward +x (an edge counts when lo <= y < hi), over the ring's vertices (closed or not).
fn inside(xs: &[f64], ys: &[f64], x: f64, y: f64) -> bool {
    let n = xs.len();
    if n < 3 {
        return false;
    }
    let mut c = false;
    let mut j = n - 1;
    for i in 0..n {
        let (xi, yi, xj, yj) = (xs[i], ys[i], xs[j], ys[j]);
        if (yi > y) != (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi {
            c = !c;
        }
        j = i;
    }
    c
}

/// (point inside, area m2) of one ring in lon / lat degrees.
fn interior(xs: &[f64], ys: &[f64]) -> (f64, f64, f64) {
    let n = xs.len();
    if n == 0 {
        return (f64::NAN, f64::NAN, 0.0);
    }
    let (x0, y0) = (xs[0], ys[0]);
    let kx = M_PER_DEG * y0.to_radians().cos().max(0.01);
    // the area centroid in metres round the first vertex (shoelace)
    let (mut a2, mut cx, mut cy) = (0.0f64, 0.0f64, 0.0f64);
    for i in 0..n {
        let j = (i + 1) % n;
        let (ax, ay) = ((xs[i] - x0) * kx, (ys[i] - y0) * M_PER_DEG);
        let (bx, by) = ((xs[j] - x0) * kx, (ys[j] - y0) * M_PER_DEG);
        let cr = ax * by - bx * ay;
        a2 += cr;
        cx += (ax + bx) * cr;
        cy += (ay + by) * cr;
    }
    let area = (a2 * 0.5).abs();
    let (mut px, mut py) = if a2.abs() > 1e-9 {
        (x0 + cx / (3.0 * a2) / kx, y0 + cy / (3.0 * a2) / M_PER_DEG)
    } else {
        let m = n as f64;
        (xs.iter().sum::<f64>() / m, ys.iter().sum::<f64>() / m)
    };
    if !inside(xs, ys, px, py) {
        // the widest span across the ring at the centroid's latitude (nudged off a vertex's exact latitude)
        let y = py + 1e-9;
        let mut cuts: Vec<f64> = Vec::new();
        let mut j = n - 1;
        for i in 0..n {
            let (xi, yi, xj, yj) = (xs[i], ys[i], xs[j], ys[j]);
            if (yi > y) != (yj > y) {
                cuts.push((xj - xi) * (y - yi) / (yj - yi) + xi);
            }
            j = i;
        }
        cuts.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let mut best = -1.0;
        let mut k = 0;
        while k + 1 < cuts.len() {
            let w = cuts[k + 1] - cuts[k];
            if w > best {
                best = w;
                px = 0.5 * (cuts[k] + cuts[k + 1]);
                py = y;
            }
            k += 2;
        }
    }
    (px, py, area)
}

/// (points (r, 2) lon / lat, areas (r,) m2) of the rings coord_off (r + 1,) delimits in lonlat (n, 2).
#[pyfunction]
pub fn bdtopo_points<'py>(py: Python<'py>, coord_off: PyReadonlyArray1<'py, i64>, lonlat: PyReadonlyArray2<'py, f64>)
                          -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>) {
    let off = coord_off.as_slice().unwrap_or(&[]).to_vec();
    let ll = lonlat.as_array();
    let r = off.len().saturating_sub(1);
    let mut pts = Array2::<f64>::from_elem((r, 2), f64::NAN);
    let mut areas = vec![0.0f64; r];
    for k in 0..r {
        let (a, b) = (off[k].max(0) as usize, (off[k + 1].max(0) as usize).min(ll.nrows()));
        if b <= a + 2 {
            continue;
        }
        let xs: Vec<f64> = (a..b).map(|i| ll[[i, 0]]).collect();
        let ys: Vec<f64> = (a..b).map(|i| ll[[i, 1]]).collect();
        let (px, py_, area) = interior(&xs, &ys);
        pts[[k, 0]] = px;
        pts[[k, 1]] = py_;
        areas[k] = area;
    }
    (pts.into_pyarray(py), areas.into_pyarray(py))
}

/// For each footprint ring lonlat[start[i]:end[i]], the index of the record (rec_lon, rec_lat) inside it with the
/// largest rec_area; -1 none.
#[pyfunction]
pub fn bdtopo_match<'py>(py: Python<'py>, start: PyReadonlyArray1<'py, i64>, end: PyReadonlyArray1<'py, i64>,
                         lonlat: PyReadonlyArray2<'py, f64>, rec_lon: PyReadonlyArray1<'py, f64>,
                         rec_lat: PyReadonlyArray1<'py, f64>, rec_area: PyReadonlyArray1<'py, f64>)
                         -> Bound<'py, PyArray1<i32>> {
    let (s, e) = (start.as_slice().unwrap_or(&[]), end.as_slice().unwrap_or(&[]));
    let ll = lonlat.as_array();
    let (rx, ry, ra) = (rec_lon.as_array(), rec_lat.as_array(), rec_area.as_array());
    let nr = rx.len().min(ry.len()).min(ra.len());
    let mut grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    for k in 0..nr {
        if rx[k].is_finite() && ry[k].is_finite() {
            grid.entry(((rx[k] / BIN_DEG).floor() as i64, (ry[k] / BIN_DEG).floor() as i64)).or_default().push(k);
        }
    }
    let n = s.len().min(e.len());
    let mut out = vec![-1i32; n];
    if nr == 0 {
        return out.into_pyarray(py);
    }
    let mut xs: Vec<f64> = Vec::new();
    let mut ys: Vec<f64> = Vec::new();
    for i in 0..n {
        let (a, b) = (s[i].max(0) as usize, (e[i].max(0) as usize).min(ll.nrows()));
        if b <= a + 2 {
            continue;
        }
        xs.clear();
        ys.clear();
        let (mut x0, mut x1, mut y0, mut y1) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
        for p in a..b {
            let (x, y) = (ll[[p, 0]], ll[[p, 1]]);
            xs.push(x);
            ys.push(y);
            x0 = x0.min(x); x1 = x1.max(x); y0 = y0.min(y); y1 = y1.max(y);
        }
        let finite = x0.is_finite() && y0.is_finite() && x1.is_finite() && y1.is_finite();
        if !finite || (x1 - x0) > 0.05 || (y1 - y0) > 0.05 {
            continue;       // (a broken ring, or one kilometres across: not a building to match)
        }
        let mut best = -1i64;
        let mut best_a = f64::NEG_INFINITY;
        for gx in (x0 / BIN_DEG).floor() as i64..=(x1 / BIN_DEG).floor() as i64 {
            for gy in (y0 / BIN_DEG).floor() as i64..=(y1 / BIN_DEG).floor() as i64 {
                if let Some(v) = grid.get(&(gx, gy)) {
                    for &k in v {
                        let (px, py_) = (rx[k], ry[k]);
                        if px < x0 || px > x1 || py_ < y0 || py_ > y1 {
                            continue;
                        }
                        // the larger record wins; the lower index on a tie (deterministic)
                        if (ra[k] > best_a || (ra[k] == best_a && (k as i64) < best)) && inside(&xs, &ys, px, py_) {
                            best = k as i64;
                            best_a = ra[k];
                        }
                    }
                }
            }
        }
        out[i] = best as i32;
    }
    out.into_pyarray(py)
}
