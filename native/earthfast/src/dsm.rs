//! dsmground.ground in Rust (2026-10-23: the Deck's server spent ~38 CPU-s of a teleport outside the lidar areas in
//! its numpy passes, on the main interpreter's lock): the ground under a surface model, buildings and tree stands
//! taken out by a morphological opening. The same maths as the numpy code, step by step in float32 (the box mean's
//! summed-area table in float64, as numpy's cumsum), so the result is the same to the bit.
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2};
use pyo3::prelude::*;

/// Running min (max: false) / max over a centred window of w (odd) samples along the rows (axis 1: within a row) or
/// the columns (axis 0), edges extended with the edge values (numpy's pad mode "edge").
fn run(a: &[f32], rows: usize, cols: usize, w: usize, along_rows: bool, max: bool) -> Vec<f32> {
    if w <= 1 {
        return a.to_vec();
    }
    let h = w / 2;
    let (lines, n) = if along_rows { (rows, cols) } else { (cols, rows) };
    let at = |line: usize, i: usize| -> f32 {
        if along_rows { a[line * cols + i] } else { a[i * cols + line] }
    };
    let op = |x: f32, y: f32| -> f32 { if max { if y > x { y } else { x } } else if y < x { y } else { x } };
    let mut out = vec![0f32; rows * cols];
    // van Herk / Gil-Werman over the padded line p (length n + 2h rounded up to a multiple of w)
    let m = n + 2 * h + ((w - (n + 2 * h) % w) % w);
    let mut p = vec![0f32; m];
    let mut g = vec![0f32; m];
    let mut s = vec![0f32; m];
    for line in 0..lines {
        for (j, v) in p.iter_mut().enumerate() {
            let i = j as isize - h as isize;
            let i = i.clamp(0, n as isize - 1) as usize;
            *v = at(line, i);
        }
        for b in (0..m).step_by(w) {
            g[b] = p[b];
            for j in b + 1..b + w {
                g[j] = op(g[j - 1], p[j]);
            }
            s[b + w - 1] = p[b + w - 1];
            for j in (b..b + w - 1).rev() {
                s[j] = op(s[j + 1], p[j]);
            }
        }
        for i in 0..n {
            let v = op(s[i], g[i + w - 1]);
            if along_rows { out[line * cols + i] = v } else { out[i * cols + line] = v }
        }
    }
    out
}

fn erode(a: &[f32], rows: usize, cols: usize, w: usize) -> Vec<f32> {
    let t = run(a, rows, cols, w, false, false);
    run(&t, rows, cols, w, true, false)
}

fn dilate(a: &[f32], rows: usize, cols: usize, w: usize) -> Vec<f32> {
    let t = run(a, rows, cols, w, false, true);
    run(&t, rows, cols, w, true, true)
}

fn opening(a: &[f32], rows: usize, cols: usize, w: usize) -> Vec<f32> {
    let e = erode(a, rows, cols, w);
    dilate(&e, rows, cols, w)
}

/// Mean over a centred w x w window, edges replicated: numpy's padded double cumsum in float64, the four-corner
/// difference in its order, divided by w * w, to float32.
fn box_mean(a: &[f32], rows: usize, cols: usize, w: usize) -> Vec<f32> {
    if w <= 1 {
        return a.to_vec();
    }
    let h = w / 2;
    let (pr, pc) = (rows + 2 * h, cols + 2 * h);
    // c: (pr + 1) x (pc + 1), first row and column 0 (np.pad((1, 0), (1, 0)))
    let mut c = vec![0f64; (pr + 1) * (pc + 1)];
    let px = |i: usize, j: usize| -> f64 {
        let r = (i as isize - h as isize).clamp(0, rows as isize - 1) as usize;
        let q = (j as isize - h as isize).clamp(0, cols as isize - 1) as usize;
        a[r * cols + q] as f64
    };
    // cumsum along axis 0, then along axis 1 (numpy's order of additions)
    let mut col0 = vec![0f64; pr * pc];
    for j in 0..pc {
        let mut acc = 0f64;
        for i in 0..pr {
            acc += px(i, j);
            col0[i * pc + j] = acc;
        }
    }
    for i in 0..pr {
        let mut acc = 0f64;
        for j in 0..pc {
            acc += col0[i * pc + j];
            c[(i + 1) * (pc + 1) + (j + 1)] = acc;
        }
    }
    let cc = |i: usize, j: usize| c[i * (pc + 1) + j];
    let ww = (w * w) as f64;
    let mut out = vec![0f32; rows * cols];
    for i in 0..rows {
        for j in 0..cols {
            let s = cc(i + w, j + w) - cc(i, j + w) - cc(i + w, j) + cc(i, j);
            out[i * cols + j] = (s / ww) as f32;
        }
    }
    out
}

/// np.gradient of a 2D float32 array with uniform spacing dx: (d/d rows, d/d cols), central differences inside,
/// first-order at the edges, all in float32.
fn gradient(a: &[f32], rows: usize, cols: usize, dx: f32) -> (Vec<f32>, Vec<f32>) {
    let mut gy = vec![0f32; rows * cols];
    let mut gx = vec![0f32; rows * cols];
    let two = 2.0f32 * dx;
    for i in 0..rows {
        for j in 0..cols {
            let k = i * cols + j;
            gy[k] = if rows < 2 {
                0.0
            } else if i == 0 {
                (a[k + cols] - a[k]) / dx
            } else if i == rows - 1 {
                (a[k] - a[k - cols]) / dx
            } else {
                (a[k + cols] - a[k - cols]) / two
            };
            gx[k] = if cols < 2 {
                0.0
            } else if j == 0 {
                (a[k + 1] - a[k]) / dx
            } else if j == cols - 1 {
                (a[k] - a[k - 1]) / dx
            } else {
                (a[k + 1] - a[k - 1]) / two
            };
        }
    }
    (gy, gx)
}

fn clip01(v: f32) -> f32 {
    if v < 0.0 { 0.0 } else if v > 1.0 { 1.0 } else { v }
}

/// dsmground.ground(a, px_m, ...): a [rows, cols] float32 elevation with the surface objects narrower than window_m
/// taken down to the ground around them. w, w_half, wr, w_small: the odd window sizes in samples (computed in Python,
/// as before); the thresholds as dsmground's constants.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn dsm_ground<'py>(py: Python<'py>, a: PyReadonlyArray2<'py, f32>, px_m: f32, window_m: f32, threshold_m: f32,
                       slope_k: f32, max_cut_m: f32, w: usize, w_half: usize, wr: usize, w_small: usize,
                       hills_m: f32, flat_m: f32, cut_lo: f32, cut_hi: f32) -> Bound<'py, PyArray2<f32>> {
    let arr = a.as_array();
    let (rows, cols) = (arr.shape()[0], arr.shape()[1]);
    let src: Vec<f32> = arr.iter().copied().collect();
    let out = py.detach(|| {
        let o = opening(&src, rows, cols, w);
        let cut: Vec<f32> = src.iter().zip(&o).map(|(x, y)| x - y).collect();
        let bm = box_mean(&o, rows, cols, w_half);
        let (gy, gx) = gradient(&bm, rows, cols, px_m);
        let half = window_m / 2.0;
        let soft = if threshold_m > 0.5 { threshold_m } else { 0.5 };
        let o_small = opening(&src, rows, cols, w_small);
        let dil = dilate(&o_small, rows, cols, wr);
        let ero = erode(&o_small, rows, cols, wr);
        let deep = dilate(&cut, rows, cols, w);
        let mut res = vec![0f32; rows * cols];
        for k in 0..rows * cols {
            let rise = gx[k].dhypot(gy[k]) * half;
            let thr = threshold_m + slope_k * rise;
            let mut kk = clip01((cut[k] - thr) / soft);
            let relief = dil[k] - ero[k];
            kk *= clip01((hills_m - relief) / (hills_m - flat_m));
            kk *= clip01((cut_hi - deep[k]) / (cut_hi - cut_lo));
            let c = cut[k] * kk;
            res[k] = src[k] - if c < max_cut_m { c } else { max_cut_m };
        }
        res
    });
    numpy::ndarray::Array2::from_shape_vec((rows, cols), out).unwrap().into_pyarray(py)
}

/// dem.fill_holes in Rust (2026-10-23: its numpy rounds, a pad and a full-tile pass each, were seconds of a road
/// plan's DEM reads in lidar areas): a lidar tile's exact-0 holes filled from their surroundings. Each round (k at
/// most), every still-empty cell with a filled 4-neighbour takes the mean of those (edge cells' outside neighbours:
/// the edge, numpy's pad "edge"), all from the round's start (float32, numpy's order of additions). What is left
/// is a wide zero area: it and the band of holes within k cells of it (zero padding outside) are set to 0.
#[pyfunction]
pub fn fill_holes<'py>(py: Python<'py>, a: PyReadonlyArray2<'py, f32>, k: usize) -> Bound<'py, PyArray2<f32>> {
    let arr = a.as_array();
    let (n, m) = (arr.shape()[0], arr.shape()[1]);
    let src: Vec<f32> = arr.iter().copied().collect();
    let out = py.detach(|| {
        let hole: Vec<bool> = src.iter().map(|&v| v == 0.0).collect();
        let mut out = src.clone();
        if !hole.iter().any(|&h| h) || hole.iter().all(|&h| h) {
            return out;
        }
        let mut todo = hole.clone();
        let idx = |i: isize, j: isize| -> usize {
            (i.clamp(0, n as isize - 1) as usize) * m + j.clamp(0, m as isize - 1) as usize
        };
        for _ in 0..k {
            if !todo.iter().any(|&t| t) {
                break;
            }
            let snap = out.clone();
            let mut got: Vec<usize> = Vec::new();
            let mut vals: Vec<f32> = Vec::new();
            for i in 0..n as isize {
                for j in 0..m as isize {
                    let c = i as usize * m + j as usize;
                    if !todo[c] {
                        continue;
                    }
                    let (u, d, l, r) = (idx(i - 1, j), idx(i + 1, j), idx(i, j - 1), idx(i, j + 1));
                    let v = |q: usize| -> f32 { if todo[q] { 0.0 } else { 1.0 } };
                    let s = snap[u] * v(u) + snap[d] * v(d) + snap[l] * v(l) + snap[r] * v(r);
                    let w = v(u) + v(d) + v(l) + v(r);
                    if w > 0.0 {
                        got.push(c);
                        vals.push(s / w);
                    }
                }
            }
            for (c, val) in got.iter().zip(vals) {
                out[*c] = val;
                todo[*c] = false;
            }
        }
        if todo.iter().any(|&t| t) {
            let mut wide = todo.clone();
            for _ in 0..k {
                let prev = wide.clone();
                let at = |i: isize, j: isize| -> bool {
                    i >= 0 && j >= 0 && i < n as isize && j < m as isize && prev[i as usize * m + j as usize]
                };
                for i in 0..n as isize {
                    for j in 0..m as isize {
                        let c = i as usize * m + j as usize;
                        if !wide[c] && hole[c] && (at(i - 1, j) || at(i + 1, j) || at(i, j - 1) || at(i, j + 1)) {
                            wide[c] = true;
                        }
                    }
                }
            }
            for (c, w) in wide.iter().enumerate() {
                if *w {
                    out[c] = 0.0;
                }
            }
        }
        out
    });
    numpy::ndarray::Array2::from_shape_vec((n, m), out).unwrap().into_pyarray(py)
}
