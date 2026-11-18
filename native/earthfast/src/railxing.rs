//! railxing.corridor_ground (version 372): the ground heights of a window's ground track lines with their parallel
//! neighbours' mixed in (a double track's beds smoothed from one profile, BNE_RAIL_XING). The numpy version measured
//! every point of a line against every point of every other line whose box met its own (a full distance matrix per
//! pair): a rail yard of hundreds of tracks every 2 m never finished a cell's plans (Fells Point, 2026-11-17: 2,105 CPU-s
//! in 20 min against ~165 without the switch). Here a uniform grid over all the points looks only at the points that can
//! be within PARALLEL_M, with the same numbers as the numpy version, bit for bit:
//!
//! - the distance is np.hypot as dnp has it (the libm crate's, `dhypot`), of p_i - p_j;
//! - the nearest point of line j is np.argmin's: the smallest distance, the first index on a tie. It only matters when
//!   its distance is <= parallel_m (else j adds nothing), and then every point of j at that distance is within
//!   parallel_m too: the smallest distance and first index among j's points within parallel_m are the argmin's;
//! - a point within parallel_m of p lies in p's grid cell or one of its 8 neighbours: the cells are CELL wide, a power
//!   of two over parallel_m * 1.0001 (x / CELL exact), and hypot(dx, dy) <= parallel_m means |dx|, |dy| <= parallel_m
//!   within an ulp or two (hypot >= max(|dx|, |dy|) up to libm's < 1 ulp error; dx the rounded difference); a point
//!   whose dx * dx + dy * dy passes (parallel_m * 1.0001)^2 is surely past parallel_m and skipped before hypot;
//! - numpy's box pre-filter (each line's box widened by parallel_m) only skipped pairs of lines more than 2 *
//!   parallel_m apart along x or y (rounding is monotonic: fl(a - m) > fl(b + m) needs a - b > 2 m), none of whose
//!   points are within parallel_m: it never changed a result and the grid needs no such step;
//! - the tangents are np.gradient's (one-sided at the ends, central / 2.0 inside), over np.maximum(hypot,
//!   min_step) (a NaN stays NaN); a one-point line's are zero; |cos| the products summed in numpy's order;
//! - each point's sum starts at its own height and adds the neighbours' in increasing line order, then / (1 + their
//!   count), as numpy's acc[ok] += ... per line and acc / cnt.
//!
//! The grid needs finite coordinates of a sane size (|v| < 1e15); otherwise (a NaN or inf point: numpy's argmin takes the
//! first NaN, its boxes compare false) the kernel does numpy's full version as it was, box filter and all.
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// np.maximum: a NaN either side wins.
fn nmax(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b > a { b } else { a }
}

/// railxing._tangents of a line's points (len >= 2), zeros for a one-point line.
fn tangents(p: &[[f64; 2]], min_step: f64) -> Vec<[f64; 2]> {
    let n = p.len();
    if n < 2 {
        return vec![[0.0, 0.0]; n];
    }
    let mut d = vec![[0.0f64; 2]; n];
    for c in 0..2 {
        d[0][c] = (p[1][c] - p[0][c]) / 1.0;
        for k in 1..n - 1 {
            d[k][c] = (p[k + 1][c] - p[k - 1][c]) / 2.0;
        }
        d[n - 1][c] = (p[n - 1][c] - p[n - 2][c]) / 1.0;
    }
    d.iter()
        .map(|v| {
            let m = nmax(v[0].dhypot(v[1]), min_step);
            [v[0] / m, v[1] / m]
        })
        .collect()
}

/// The lines' points packed (pts, off: line l is pts[off[l]..off[l + 1]]) and their heights (zs the same).
pub struct Lines {
    pub pts: Vec<[f64; 2]>,
    pub off: Vec<usize>,
    pub zs: Vec<f64>,
}

/// corridor_ground on packed lines: the heights end to end.
pub fn corridor(l: &Lines, parallel_m: f64, parallel_cos: f64, min_step: f64) -> Vec<f64> {
    let nl = l.off.len() - 1;
    let mut tan = Vec::with_capacity(l.pts.len());
    for i in 0..nl {
        tan.extend(tangents(&l.pts[l.off[i]..l.off[i + 1]], min_step));
    }
    let gridded = parallel_m.is_finite() && parallel_m >= 1e-3 && parallel_m < 1e6
        && l.pts.iter().all(|p| p[0].abs() < 1e15 && p[1].abs() < 1e15);
    if gridded { by_grid(l, &tan, parallel_m, parallel_cos) } else { full(l, &tan, parallel_m, parallel_cos) }
}

fn by_grid(l: &Lines, tan: &[[f64; 2]], pm: f64, pc: f64) -> Vec<f64> {
    let nl = l.off.len() - 1;
    let n = l.pts.len();
    let mut cell = 1.0f64;
    while cell <= pm * 1.0001 {
        cell *= 2.0;
    }
    while cell / 2.0 > pm * 1.0001 {
        cell /= 2.0;
    }
    // a cheap test before hypot: dx * dx + dy * dy (a few ulps off the true square) over (pm * 1.0001)^2 only where
    // the true distance, and so libm's hypot (< 1 ulp off), is past pm
    let far2 = (pm * 1.0001) * (pm * 1.0001);
    let key = |p: &[f64; 2]| ((p[0] / cell).floor() as i64, (p[1] / cell).floor() as i64);
    let mut line_of = vec![0u32; n];
    for i in 0..nl {
        for q in l.off[i]..l.off[i + 1] {
            line_of[q] = i as u32;
        }
    }
    // the points sorted by their cell (then index): each cell a run of `order`
    let keys: Vec<(i64, i64)> = l.pts.iter().map(key).collect();
    let mut order: Vec<u32> = (0..n as u32).collect();
    order.sort_unstable_by_key(|&q| (keys[q as usize], q));
    let mut cells: Vec<((i64, i64), u32, u32)> = Vec::new();
    let mut s = 0usize;
    while s < n {
        let k = keys[order[s] as usize];
        let mut e = s + 1;
        while e < n && keys[order[e] as usize] == k {
            e += 1;
        }
        cells.push((k, s as u32, e as u32));
        s = e;
    }
    let mut out = vec![0.0f64; n];
    let mut stamp = vec![usize::MAX; nl];
    let mut best: Vec<(f64, usize)> = vec![(0.0, 0); nl];
    let mut touched: Vec<usize> = Vec::new();
    for i in 0..nl {
        for p in l.off[i]..l.off[i + 1] {
            let (px, py) = (l.pts[p][0], l.pts[p][1]);
            let (gx, gy) = keys[p];
            touched.clear();
            for cx in gx - 1..=gx + 1 {
                for cy in gy - 1..=gy + 1 {
                    let Ok(c) = cells.binary_search_by(|e| e.0.cmp(&(cx, cy))) else { continue };
                    let (_, a, b) = cells[c];
                    for &q in &order[a as usize..b as usize] {
                        let q = q as usize;
                        let j = line_of[q] as usize;
                        if j == i {
                            continue;
                        }
                        let (dx, dy) = (px - l.pts[q][0], py - l.pts[q][1]);
                        if dx * dx + dy * dy > far2 {
                            continue;       // (surely past pm: hypot needs not be asked)
                        }
                        let d = dx.dhypot(dy);
                        if !(d <= pm) {
                            continue;
                        }
                        let k = q - l.off[j];
                        if stamp[j] != p {
                            stamp[j] = p;
                            best[j] = (d, k);
                            touched.push(j);
                        } else if d < best[j].0 || (d == best[j].0 && k < best[j].1) {
                            best[j] = (d, k);
                        }
                    }
                }
            }
            touched.sort_unstable();
            let (mut acc, mut cnt) = (l.zs[p], 1.0f64);
            for &j in &touched {
                let q = l.off[j] + best[j].1;
                let cos = (tan[p][0] * tan[q][0] + tan[p][1] * tan[q][1]).abs();
                if cos >= pc {
                    acc += l.zs[q];
                    cnt += 1.0;
                }
            }
            out[p] = acc / cnt;
        }
    }
    out
}

/// numpy's version as it was (any coordinates: NaN and inf as numpy has them).
fn full(l: &Lines, tan: &[[f64; 2]], pm: f64, pc: f64) -> Vec<f64> {
    let nl = l.off.len() - 1;
    let nmin = |a: f64, b: f64| if a.is_nan() || b.is_nan() { f64::NAN } else if b < a { b } else { a };
    let boxes: Vec<[f64; 4]> = (0..nl)
        .map(|i| {
            let s = &l.pts[l.off[i]..l.off[i + 1]];
            let (mut x0, mut y0, mut x1, mut y1) = (s[0][0], s[0][1], s[0][0], s[0][1]);
            for v in &s[1..] {
                x0 = nmin(x0, v[0]);
                y0 = nmin(y0, v[1]);
                x1 = nmax(x1, v[0]);
                y1 = nmax(y1, v[1]);
            }
            [x0 - pm, y0 - pm, x1 + pm, y1 + pm]
        })
        .collect();
    let mut out = vec![0.0f64; l.pts.len()];
    for i in 0..nl {
        let (a, b) = (l.off[i], l.off[i + 1]);
        let mut acc: Vec<f64> = l.zs[a..b].to_vec();
        let mut cnt = vec![1.0f64; b - a];
        for j in 0..nl {
            let (bi, bj) = (&boxes[i], &boxes[j]);
            if i == j || bi[0] > bj[2] || bj[0] > bi[2] || bi[1] > bj[3] || bj[1] > bi[3] {
                continue;
            }
            let (ja, jb) = (l.off[j], l.off[j + 1]);
            for p in a..b {
                // np.argmin: the first NaN, else the first of the smallest
                let (mut k, mut near) = (ja, f64::INFINITY);
                let mut first = true;
                for q in ja..jb {
                    let d = (l.pts[p][0] - l.pts[q][0]).dhypot(l.pts[p][1] - l.pts[q][1]);
                    if d.is_nan() {
                        k = q;
                        near = d;
                        break;
                    }
                    if first || d < near {
                        k = q;
                        near = d;
                        first = false;
                    }
                }
                let cos = (tan[p][0] * tan[k][0] + tan[p][1] * tan[k][1]).abs();
                if near <= pm && cos >= pc {
                    acc[p - a] += l.zs[k];
                    cnt[p - a] += 1.0;
                }
            }
        }
        for p in a..b {
            out[p] = acc[p - a] / cnt[p - a];
        }
    }
    out
}

/// railxing.corridor_ground of the lines packed: xy (n, 2+) their points end to end, off (L + 1,) where each starts
/// (every line one point or more), z (n,) their ground heights. The heights with the parallel neighbours' mixed in, end
/// to end.
#[pyfunction]
#[pyo3(signature = (xy, off, z, parallel_m, parallel_cos, min_step))]
pub fn corridor_ground<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                            z: PyReadonlyArray1<'py, f64>, parallel_m: f64, parallel_cos: f64, min_step: f64)
                            -> PyResult<Bound<'py, PyArray1<f64>>> {
    let xyv = xy.as_array();
    let zs: Vec<f64> = z.as_array().iter().copied().collect();
    if xyv.ncols() < 2 || xyv.nrows() != zs.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("corridor_ground: xy needs (n, 2), as many rows as z"));
    }
    let o: Vec<i64> = off.as_array().iter().copied().collect();
    if o.is_empty() || o[0] != 0 || *o.last().unwrap() as usize != zs.len() || o.windows(2).any(|w| w[1] <= w[0]) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "corridor_ground: inconsistent line offsets (every line needs a point)"));
    }
    let l = Lines {
        pts: xyv.rows().into_iter().map(|r| [r[0], r[1]]).collect(),
        off: o.iter().map(|&v| v as usize).collect(),
        zs,
    };
    let out = py.detach(|| corridor(&l, parallel_m, parallel_cos, min_step));
    Ok(out.into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lines(ls: &[Vec<[f64; 2]>]) -> Lines {
        let mut l = Lines { pts: Vec::new(), off: vec![0], zs: Vec::new() };
        for (i, v) in ls.iter().enumerate() {
            for (k, p) in v.iter().enumerate() {
                l.pts.push(*p);
                l.zs.push((i * 10 + k) as f64 * 0.37);
            }
            l.off.push(l.pts.len());
        }
        l
    }

    #[test]
    fn grid_matches_full() {
        let mut s = 12345u64;
        let mut r = || {
            s ^= s << 13;
            s ^= s >> 7;
            s ^= s << 17;
            (s >> 11) as f64 / (1u64 << 53) as f64
        };
        for _ in 0..50 {
            let mut ls = Vec::new();
            for _ in 0..8 {
                let (x0, y0, a) = (r() * 80.0, r() * 80.0, r() * 0.3);
                let n = 1 + (r() * 30.0) as usize;
                ls.push((0..n).map(|k| [((x0 + k as f64 * 2.0 * a.dcos()) * 4.0).round() / 4.0,
                                        ((y0 + k as f64 * 2.0 * a.dsin()) * 4.0).round() / 4.0]).collect());
            }
            let l = lines(&ls);
            let mut tan = Vec::new();
            for i in 0..ls.len() {
                tan.extend(tangents(&l.pts[l.off[i]..l.off[i + 1]], 1e-6));
            }
            let (a, b) = (by_grid(&l, &tan, 14.0, 0.9), full(&l, &tan, 14.0, 0.9));
            assert!(a.iter().zip(&b).all(|(x, y)| x.to_bits() == y.to_bits()));
        }
    }
}
