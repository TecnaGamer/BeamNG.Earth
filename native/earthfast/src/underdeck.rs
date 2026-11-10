//! A road's ground where bridge decks cross over it (grading.deck_capped): every crossing of the road's polyline with
//! a near deck's segments, the windows round them, and the ground there capped at the straight line between the
//! ground beside the deck. Was numpy per road part (a box test of every deck, a pair test of the near ones' segments,
//! np.interp per window): ~half of grading.plan_roads on a dense tile, and each call handed the GIL over a few times
//! under the road workers' threads (2026-11-08). The same products and comparisons in the same order as the numpy
//! version (elementwise: bit for bit); the segment lengths come from numpy (np.hypot), the arcs are summed in
//! order as np.cumsum does.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// numpy's binary_search_with_guess (numpy/_core/src/multiarray/compiled_base.c), as np.interp calls it.
fn search_with_guess(key: f64, arr: &[f64], guess: isize) -> isize {
    const LIKELY_IN_CACHE_SIZE: isize = 8;
    let len = arr.len() as isize;
    let at = |i: isize| arr[i as usize];
    let (mut imin, mut imax) = (0isize, len);
    if key > at(len - 1) {
        return len;
    } else if key < at(0) {
        return -1;
    }
    if len <= 4 {
        let mut i = 1;
        while i < len && key >= at(i) {
            i += 1;
        }
        return i - 1;
    }
    let mut guess = guess;
    if guess > len - 3 {
        guess = len - 3;
    }
    if guess < 1 {
        guess = 1;
    }
    if key < at(guess) {
        if key < at(guess - 1) {
            imax = guess - 1;
            if guess > LIKELY_IN_CACHE_SIZE && key >= at(guess - LIKELY_IN_CACHE_SIZE) {
                imin = guess - LIKELY_IN_CACHE_SIZE;
            }
        } else {
            return guess - 1;
        }
    } else if key < at(guess + 1) {
        return guess;
    } else if key < at(guess + 2) {
        return guess + 1;
    } else {
        imin = guess + 2;
        if guess < len - LIKELY_IN_CACHE_SIZE - 1 && key < at(guess + LIKELY_IN_CACHE_SIZE) {
            imax = guess + LIKELY_IN_CACHE_SIZE;
        }
    }
    while imin < imax {
        let imid = imin + ((imax - imin) >> 1);
        if key >= at(imid) {
            imin = imid + 1;
        } else {
            imax = imid;
        }
    }
    imin - 1
}

/// np.interp(x, xp, fp) of a scalar x (xp, fp of two or more: no precomputed slopes, as numpy for one x).
fn interp(x: f64, xp: &[f64], fp: &[f64]) -> f64 {
    let n = xp.len();
    if x.is_nan() {
        return x;
    }
    let j = search_with_guess(x, xp, 0);
    if j == -1 {
        return fp[0];
    }
    if j == n as isize {
        return fp[n - 1];
    }
    let j = j as usize;
    if j == n - 1 || xp[j] == x {
        return fp[j];
    }
    let slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
    let mut d = slope * (x - xp[j]) + fp[j];
    if d.is_nan() {
        d = slope * (x - xp[j + 1]) + fp[j + 1];
        if d.is_nan() && fp[j] == fp[j + 1] {
            d = fp[j];
        }
    }
    d
}

/// numpy's min of a column (NaN if any is NaN).
fn col_min_max(p: &numpy::ndarray::ArrayView2<f64>, c: usize) -> (f64, f64) {
    let (mut lo, mut hi) = (f64::INFINITY, f64::NEG_INFINITY);
    let mut nan = false;
    for k in 0..p.nrows() {
        let v = p[[k, c]];
        if v.is_nan() {
            nan = true;
        }
        if v < lo {
            lo = v;
        }
        if v > hi {
            hi = v;
        }
    }
    if nan { (f64::NAN, f64::NAN) } else { (lo, hi) }
}

/// grading.deck_capped's work for road pts (n, 2) with ground z (n,), la (n - 1,) its segment lengths (numpy's
/// np.hypot; its arcs summed in order, as np.cumsum), against the decks: box (d, 4) x0 y0 x1 y1 each (test_box false: every deck is near), their
/// segments off[k]..off[k + 1] of q (starts), sv (vectors), lb (lengths: numpy's np.hypot) and half[k] (half width)
/// each; a deck's arcs summed here as np.cumsum sums them (in order, from its first segment). (code, out): code 0 nothing capped before the windows (z itself), 1 windows but nothing
/// lowered (z as float64), 2 out (the capped ground).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn deck_capped<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                        la: PyReadonlyArray1<'py, f64>,
                        box_: PyReadonlyArray2<'py, f64>, test_box: bool, off: PyReadonlyArray1<'py, i64>,
                        q: PyReadonlyArray2<'py, f64>, sv: PyReadonlyArray2<'py, f64>,
                        lb: PyReadonlyArray1<'py, f64>, half: PyReadonlyArray1<'py, f64>,
                        max_m: f64, side_m: f64, min_cross: f64)
                        -> PyResult<(i32, Option<Bound<'py, PyArray1<f64>>>)> {
    let (p, z, la) = (pts.as_array(), z.as_array(), la.as_array());
    let (bx, off, q, sv, lb, half) = (box_.as_array(), off.as_array(), q.as_array(), sv.as_array(), lb.as_array(),
                                      half.as_array());
    let n = p.nrows();
    let nd = half.len();
    let m = lb.len();
    if n < 2 || z.len() != n || la.len() + 1 != n || off.len() != nd + 1
        || (test_box && bx.nrows() != nd) || q.nrows() != m || sv.nrows() != m
        || off.iter().any(|&o| o < 0 || o as usize > m) {
        return Err(pyo3::exceptions::PyValueError::new_err("deck_capped: inconsistent arrays"));
    }
    // (np.r_[0.0, np.cumsum(la)]: the road's arcs)
    let mut sa = Vec::with_capacity(n);
    let mut acc = 0.0f64;
    sa.push(acc);
    for i in 0..n - 1 {
        acc += la[i];
        sa.push(acc);
    }
    // the near decks (grading._Decks' box test)
    let (x0, x1) = col_min_max(&p, 0);
    let (y0, y1) = col_min_max(&p, 1);
    let (lo0, lo1, hi0, hi1) = (x0 - max_m, y0 - max_m, x1 + max_m, y1 + max_m);
    let mut wins: Vec<(f64, f64)> = Vec::new();
    let mut sb: Vec<f64> = Vec::new();
    for k in 0..nd {
        if test_box && !(bx[[k, 2]] >= lo0 && bx[[k, 3]] >= lo1 && bx[[k, 0]] <= hi0 && bx[[k, 1]] <= hi1) {
            continue;
        }
        let (g0, g1) = (off[k] as usize, off[k + 1] as usize);
        if g1 <= g0 {
            continue;
        }
        // (np.r_[0.0, np.cumsum(lb)] of the deck: its segments' start arcs, and its length the last)
        sb.clear();
        let mut acc = 0.0f64;
        for g in g0..g1 {
            sb.push(acc);
            acc += lb[g];
        }
        let length = acc;
        for i in 0..n - 1 {
            let (p0, p1) = (p[[i, 0]], p[[i, 1]]);
            let (r0, r1) = (p[[i + 1, 0]] - p0, p[[i + 1, 1]] - p1);
            for g in g0..g1 {
                let (s0, s1) = (sv[[g, 0]], sv[[g, 1]]);
                let rxs = r0 * s1 - r1 * s0;
                if !(rxs.abs() > 1e-12) {
                    continue;
                }
                let (qp0, qp1) = (q[[g, 0]] - p0, q[[g, 1]] - p1);
                let t = (qp0 * s1 - qp1 * s0) / rxs;
                if !(t >= 0.0 && t <= 1.0) {
                    continue;
                }
                let u = (qp0 * r1 - qp1 * r0) / rxs;
                if !(u >= 0.0 && u <= 1.0) {
                    continue;
                }
                let prod = la[i] * lb[g];
                let sn = rxs.abs() / if prod >= 1e-12 || prod.is_nan() { prod } else { 1e-12 };
                let a = sa[i] + t * la[i];
                let b = sb[g - g0] + u * lb[g];
                if min_cross <= b && b <= length - min_cross {
                    let w = half[k] / if 0.2 > sn { 0.2 } else { sn };
                    let h = (if max_m < w { max_m } else { w }) + side_m;
                    wins.push((a - h, a + h));
                }
            }
        }
    }
    if wins.is_empty() {
        return Ok((0, None));
    }
    // (Python's sort of the (start, end) tuples, then merged where they overlap)
    wins.sort_by(|x, y| x.0.partial_cmp(&y.0).unwrap_or(std::cmp::Ordering::Equal)
        .then(x.1.partial_cmp(&y.1).unwrap_or(std::cmp::Ordering::Equal)));
    let mut merged: Vec<(f64, f64)> = vec![wins[0]];
    for &(a, b) in &wins[1..] {
        let last = merged.last_mut().unwrap();
        if a <= last.1 {
            if b > last.1 {
                last.1 = b;
            }
        } else {
            merged.push((a, b));
        }
    }
    let s = sa;
    let zv: Vec<f64> = z.to_vec();
    let mut out = zv.clone();
    let s_last = s[n - 1];
    for &(a, b) in &merged {
        let a = if 0.0 > a { 0.0 } else { a };
        let b = if s_last < b { s_last } else { b };
        if b - a < 1e-6 || !s.iter().any(|&v| v > a && v < b) {
            continue;
        }
        let (za, zb) = (interp(a, &s, &zv), interp(b, &s, &zv));
        for k in 0..n {
            if s[k] > a && s[k] < b {
                let line = za + (zb - za) * (s[k] - a) / (b - a);
                let cur = out[k];
                out[k] = if cur <= line || cur.is_nan() { cur } else { line };
            }
        }
    }
    if out.iter().zip(zv.iter()).any(|(o, v)| o < v) {
        Ok((2, Some(out.into_pyarray(py))))
    } else {
        Ok((1, None))
    }
}
