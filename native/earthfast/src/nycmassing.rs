//! New York City's measured massing (server/nycmassing.py): the city's 2014 3-D Building Model records joined to the
//! map's footprints.
//!
//! `nyc_massing_match`: for each footprint (its outer ring, in a metric frame), the record whose interior point stands
//! inside it and whose footprint area is within [lo, hi] of the footprint's, the closest in area when several do
//! (|ln ratio|); a record claimed by several footprints goes to the closest in area (ties: the first), the others get
//! none. -1 none. Run once per buildings layer of a tile, on the tiles' path: a grid of the records' points (32 m
//! bins), then an even-odd test against the few records under each footprint's box. Pure.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use std::collections::HashMap;

const BIN_M: f64 = 32.0;

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

/// |shoelace area| of a ring (closed or not), round its first vertex.
fn area(xs: &[f64], ys: &[f64]) -> f64 {
    let n = xs.len();
    if n < 3 {
        return 0.0;
    }
    let (x0, y0) = (xs[0], ys[0]);
    let mut a2 = 0.0;
    for i in 0..n {
        let j = (i + 1) % n;
        a2 += (xs[i] - x0) * (ys[j] - y0) - (xs[j] - x0) * (ys[i] - y0);
    }
    (a2 * 0.5).abs()
}

/// The pure join: per footprint xy[start[i]:end[i]] the record index or -1 (see the module comment).
pub fn match_rings(xy: &[[f64; 2]], start: &[i64], end: &[i64], rec: &[[f64; 2]], rec_area: &[f64], lo: f64,
                   hi: f64) -> Vec<i32> {
    let n = start.len().min(end.len());
    let mut out = vec![-1i32; n];
    let nr = rec.len().min(rec_area.len());
    if nr == 0 {
        return out;
    }
    let mut grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    for (k, p) in rec.iter().enumerate().take(nr) {
        if p[0].is_finite() && p[1].is_finite() && rec_area[k] > 0.0 {
            grid.entry(((p[0] / BIN_M).floor() as i64, (p[1] / BIN_M).floor() as i64)).or_default().push(k);
        }
    }
    let mut score = vec![f64::INFINITY; n];
    let (mut xs, mut ys): (Vec<f64>, Vec<f64>) = (Vec::new(), Vec::new());
    for i in 0..n {
        let (a, b) = (start[i].max(0) as usize, (end[i].max(0) as usize).min(xy.len()));
        if b <= a + 2 {
            continue;
        }
        xs.clear();
        ys.clear();
        let (mut x0, mut x1, mut y0, mut y1) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
        for p in &xy[a..b] {
            xs.push(p[0]);
            ys.push(p[1]);
            x0 = x0.min(p[0]);
            x1 = x1.max(p[0]);
            y0 = y0.min(p[1]);
            y1 = y1.max(p[1]);
        }
        if !(x0.is_finite() && x1.is_finite() && y0.is_finite() && y1.is_finite()) {
            continue;
        }
        let fa = area(&xs, &ys);
        if fa <= 0.0 {
            continue;
        }
        let (gx0, gx1) = ((x0 / BIN_M).floor() as i64, (x1 / BIN_M).floor() as i64);
        let (gy0, gy1) = ((y0 / BIN_M).floor() as i64, (y1 / BIN_M).floor() as i64);
        if (gx1 - gx0 + 1) * (gy1 - gy0 + 1) > 4096 {
            continue;               // (a footprint kilometres across: no building record is its own)
        }
        let mut best = (-1i32, f64::INFINITY);
        for gx in gx0..=gx1 {
            for gy in gy0..=gy1 {
                if let Some(ks) = grid.get(&(gx, gy)) {
                    for &k in ks {
                        let p = rec[k];
                        if p[0] < x0 || p[0] > x1 || p[1] < y0 || p[1] > y1 {
                            continue;
                        }
                        let ratio = fa / rec_area[k];
                        if !(lo..=hi).contains(&ratio) || !inside(&xs, &ys, p[0], p[1]) {
                            continue;
                        }
                        let s = ratio.ln().abs();
                        if s < best.1 || (s == best.1 && (k as i32) < best.0) {
                            best = (k as i32, s);
                        }
                    }
                }
            }
        }
        out[i] = best.0;
        score[i] = best.1;
    }
    // (one footprint per record: the closest in area keeps it)
    let mut owner: HashMap<i32, usize> = HashMap::new();
    for i in 0..n {
        let k = out[i];
        if k < 0 {
            continue;
        }
        match owner.get(&k) {
            Some(&j) if score[j] <= score[i] => out[i] = -1,
            Some(&j) => {
                out[j] = -1;
                owner.insert(k, i);
            }
            None => {
                owner.insert(k, i);
            }
        }
    }
    out
}

/// For each footprint ring xy[start[i]:end[i]] (a metric frame), the index of its record (rec (r, 2) interior points in
/// the same frame, rec_area (r,) m2), or -1. lo, hi: the footprint / record area ratios accepted.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn nyc_massing_match<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, start: PyReadonlyArray1<'py, i64>,
                              end: PyReadonlyArray1<'py, i64>, rec: PyReadonlyArray2<'py, f64>,
                              rec_area: PyReadonlyArray1<'py, f64>, lo: f64, hi: f64)
                              -> Bound<'py, PyArray1<i32>> {
    let (xy, rec) = (xy.as_array(), rec.as_array());
    let pts: Vec<[f64; 2]> = if xy.ncols() >= 2 { xy.rows().into_iter().map(|r| [r[0], r[1]]).collect() } else { vec![] };
    let recs: Vec<[f64; 2]> = if rec.ncols() >= 2 { rec.rows().into_iter().map(|r| [r[0], r[1]]).collect() } else { vec![] };
    let ra = rec_area.as_array().to_vec();
    let s = start.as_array().to_vec();
    let e = end.as_array().to_vec();
    let out = py.detach(|| match_rings(&pts, &s, &e, &recs, &ra, lo, hi));
    out.into_pyarray(py)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn square(x: f64, y: f64, s: f64) -> Vec<[f64; 2]> {
        vec![[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]
    }

    #[test]
    fn matches_inside_and_area() {
        let mut xy = square(0.0, 0.0, 10.0);
        xy.extend(square(100.0, 0.0, 10.0));
        let out = match_rings(&xy, &[0, 5], &[5, 10], &[[5.0, 5.0], [105.0, 5.0]], &[100.0, 300.0], 0.8, 1.25);
        assert_eq!(out, vec![0, -1]);      // (the second: area ratio 1/3)
    }

    #[test]
    fn one_footprint_per_record() {
        let mut xy = square(0.0, 0.0, 10.0);
        xy.extend(square(0.0, 0.0, 10.5));
        let out = match_rings(&xy, &[0, 5], &[5, 10], &[[5.0, 5.0]], &[110.25], 0.8, 1.25);
        assert_eq!(out, vec![-1, 0]);      // (the second: equal areas)
    }
}
