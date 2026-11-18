//! seacarve's raster loops in Rust (2026-10-23: a Steam Deck's "sea-warm" thread spent ~23 CPU-s of a fresh place's
//! teleport in shore_steps' whole-raster passes and _carve_line's per-segment numpy, 300 miles from the sea: its rivers,
//! lakes and streams). The same results as the numpy code to the bit (checked on real cells once).
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyReadwriteArray2};
use pyo3::prelude::*;

/// seacarve.shore_steps: for each True pixel of water, the number of erosion steps (4-neighbour on odd steps, 8 on
/// even ones; outside the raster counts as water) until it is reached from a False pixel, capped at kmax; 0 on False
/// pixels. Walks the border (the water pixels with a dry neighbour) instead of passing over the whole raster each step.
#[pyfunction]
pub fn shore_steps<'py>(py: Python<'py>, water: PyReadonlyArray2<'py, bool>, kmax: i64) -> Bound<'py, PyArray2<i16>> {
    let w = water.as_array();
    let (n, m) = (w.shape()[0], w.shape()[1]);
    // padded by one pixel of water all round (outside the raster counts as water): no bounds checks
    let wp = m + 2;
    let mut cur = vec![1u8; (n + 2) * wp];
    match w.as_slice() {
        Some(sl) => {
            for j in 0..n {
                for (c, &v) in cur[(j + 1) * wp + 1..(j + 1) * wp + 1 + m].iter_mut().zip(&sl[j * m..(j + 1) * m]) {
                    *c = v as u8;
                }
            }
        }
        None => {
            for ((j, i), &v) in w.indexed_iter() {
                cur[(j + 1) * wp + i + 1] = v as u8;
            }
        }
    }
    let out = py.detach(|| {
        let mut out = vec![0i16; n * m];
        let four = [1isize, -1, wp as isize, -(wp as isize)];
        let diag = [wp as isize + 1, wp as isize - 1, -(wp as isize) + 1, -(wp as isize) - 1];
        let dry = |cur: &Vec<u8>, p: usize, offs: &[isize]| offs.iter().any(|&o| cur[(p as isize + o) as usize] == 0);
        let mut in_border = vec![false; cur.len()];
        let mut border: Vec<usize> = Vec::new();
        let mut left = 0usize;
        for j in 1..=n {
            let (up, row, dn) = (&cur[(j - 1) * wp..j * wp], &cur[j * wp..(j + 1) * wp], &cur[(j + 1) * wp..(j + 2) * wp]);
            for i in 1..=m {
                if row[i] == 1 {
                    left += 1;
                    let all = row[i - 1] & row[i + 1] & up[i - 1] & up[i] & up[i + 1] & dn[i - 1] & dn[i] & dn[i + 1];
                    if all == 0 {
                        in_border[j * wp + i] = true;
                        border.push(j * wp + i);
                    }
                }
            }
        }
        let mut removed: Vec<usize> = Vec::new();
        for k in 1..=kmax {
            if left == 0 {
                break;
            }
            removed.clear();
            for &p in &border {
                if dry(&cur, p, &four) || (k % 2 == 0 && dry(&cur, p, &diag)) {
                    removed.push(p);
                }
            }
            for &p in &removed {
                let (j, i) = (p / wp - 1, p % wp - 1);
                out[j * m + i] = k as i16;
                cur[p] = 0;
                in_border[p] = false;
                left -= 1;
            }
            border.retain(|&p| cur[p] == 1);
            for &p in &removed {
                for &o in four.iter().chain(diag.iter()) {
                    let q = (p as isize + o) as usize;
                    let (j, i) = (q / wp, q % wp);
                    if cur[q] == 1 && !in_border[q] && j >= 1 && j <= n && i >= 1 && i <= m {
                        in_border[q] = true;
                        border.push(q);
                    }
                }
            }
        }
        if left > 0 {
            for j in 0..n {
                for i in 0..m {
                    if cur[(j + 1) * wp + i + 1] == 1 {
                        out[j * m + i] = kmax as i16;
                    }
                }
            }
        }
        out
    });
    numpy::ndarray::Array2::from_shape_vec((n, m), out).unwrap().into_pyarray(py)
}

/// SeaCarver._carve_line's segments: the line q (k, 2) in raster pixels with per-point half widths hw (pixels), levels
/// lv and centre depths dd (m), carved into depth / level (float32, NaN level: not cut yet) where held is False, the
/// deeper cut winning; drawn marks the pixels cut inside the half width. bank: the bank's reach (pixels), px: m per
/// pixel, slope: the bank's slope.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn carve_segments(py: Python<'_>, q: PyReadonlyArray2<'_, f64>, hw: PyReadonlyArray1<'_, f64>,
                      lv: PyReadonlyArray1<'_, f64>, dd: PyReadonlyArray1<'_, f64>, bank: f64, px: f64, slope: f64,
                      held: PyReadonlyArray2<'_, bool>, mut depth: PyReadwriteArray2<'_, f32>,
                      mut level: PyReadwriteArray2<'_, f32>, mut drawn: PyReadwriteArray2<'_, bool>) {
    let q = q.as_array();
    let hw = hw.as_array();
    let lv = lv.as_array();
    let dd = dd.as_array();
    let held = held.as_array();
    let mut depth = depth.as_array_mut();
    let mut level = level.as_array_mut();
    let mut drawn = drawn.as_array_mut();
    let n = depth.shape()[0] as i64;
    let nc = depth.shape()[1] as i64;
    py.detach(|| {
        for k in 0..q.shape()[0].saturating_sub(1) {
            let (a0, a1, b0, b1) = (q[[k, 0]], q[[k, 1]], q[[k + 1, 0]], q[[k + 1, 1]]);
            let reach = hw[k].max(hw[k + 1]) + bank;
            // (Python's int(): toward zero)
            let i0 = ((a0.min(b0) - reach) as i64).max(0);
            let i1 = (((a0.max(b0) + reach) as i64) + 2).min(nc);
            let j0 = ((a1.min(b1) - reach) as i64).max(0);
            let j1 = (((a1.max(b1) + reach) as i64) + 2).min(n);
            if i1 <= i0 || j1 <= j0 {
                continue;
            }
            let (ab0, ab1) = (b0 - a0, b1 - a1);
            let l2 = ab0 * ab0 + ab1 * ab1;
            let l2 = if l2 == 0.0 { 1e-9 } else { l2 };
            for j in j0..j1 {
                let gj = j as f64 + 0.5;
                for i in i0..i1 {
                    let gi = i as f64 + 0.5;
                    let tt = (((gi - a0) * ab0 + (gj - a1) * ab1) / l2).clamp(0.0, 1.0);
                    let dist = (gi - (a0 + tt * ab0)).dhypot(gj - (a1 + tt * ab1));
                    let h = hw[k] + (hw[k + 1] - hw[k]) * tt;
                    let lvl = lv[k] + (lv[k + 1] - lv[k]) * tt;
                    let d = dd[k] + (dd[k + 1] - dd[k]) * tt;
                    let inside = dist <= h;
                    let want = if inside {
                        let r = dist / h.max(1e-6);
                        d * (1.0 - r * r)
                    } else {
                        -(dist - h) * px * slope
                    };
                    let (ju, iu) = (j as usize, i as usize);
                    if !(dist <= h + bank) || held[[ju, iu]] {
                        continue;
                    }
                    let (cd, cl) = (depth[[ju, iu]], level[[ju, iu]]);
                    if cl.is_nan() || lvl - want < (cl - cd) as f64 {
                        depth[[ju, iu]] = want as f32;
                        level[[ju, iu]] = lvl as f32;
                        if inside {
                            drawn[[ju, iu]] = true;
                        }
                    }
                }
            }
        }
    });
}
