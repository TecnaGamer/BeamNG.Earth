//! Bridge chain solving's pair kernels (roads.BridgeProfiles._solve): _merge_overlaps' per-round pull of every
//! related pair of deck parts toward each other (a numpy (len(A), len(B)) projection per pair and round, 0.75 s of a
//! cold Manhattan road tile, 2026-11-08) and _ground_pins' joints a ground road goes on from. The numpy versions'
//! operations in their order, so the heights come out bit for bit the same (server/tests/deckmerge_reference.py).
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

// np.clip's float loop (umath/clip.cpp): _NPY_MIN(_NPY_MAX(x, lo), hi), NaN in x kept, PyArray_MAX / MIN otherwise
fn clip_max(a: f64, b: f64) -> f64 {
    if a.is_nan() {
        a
    } else if a > b {
        a
    } else {
        b
    }
}

fn clip_min(a: f64, b: f64) -> f64 {
    if a.is_nan() {
        a
    } else if a < b {
        a
    } else {
        b
    }
}

fn np_clip(x: f64, lo: f64, hi: f64) -> f64 {
    clip_min(clip_max(x, lo), hi)
}

// np.maximum's loop: (a >= b || isnan(a)) ? a : b (a NaN operand propagates as it is, its sign too)
fn np_maximum(a: f64, b: f64) -> f64 {
    if a >= b || a.is_nan() {
        a
    } else {
        b
    }
}

// np.nan_to_num
fn nan_to_num(x: f64) -> f64 {
    if x.is_nan() {
        0.0
    } else if x == f64::INFINITY {
        f64::MAX
    } else if x == f64::NEG_INFINITY {
        -f64::MAX
    } else {
        x
    }
}

/// part j's segment s (rows b0 + s, b0 + s + 1): (distance of (ax, ay) to it, the projection's t), as the numpy rows
fn seg_dt(xy: &numpy::ndarray::ArrayView2<f64>, b0: usize, s: usize, ax: f64, ay: f64) -> (f64, f64) {
    let (px, py_) = (xy[[b0 + s, 0]], xy[[b0 + s, 1]]);
    let (abx, aby) = (xy[[b0 + s + 1, 0]] - px, xy[[b0 + s + 1, 1]] - py_);
    let l2 = np_maximum(abx * abx + aby * aby, 1e-12);
    let tt = np_clip(((ax - px) * abx + (ay - py_) * aby) / l2, 0.0, 1.0);
    let (nx, ny) = (px + tt * abx, py_ + tt * aby);
    ((ax - nx).dhypot(ay - ny), tt)
}

const BLOCK: usize = 16;            // segments per box (Blocks)
const PRUNE_M: f64 = 1e-6;          // m a box must lie past the best distance to be left out: far over the rounding

/// Each part's segments in boxes of BLOCK, so the nearest segment of a long part is found without measuring the far
/// ones: a box farther than the best distance so far (+ PRUNE_M) holds no segment as near, nor one tying it (every
/// projection lies within its segment's box, to rounding). A part with a non-finite coordinate is scanned whole.
struct Blocks {
    first: Vec<usize>,              // part k's boxes: boxes[first[k]..first[k + 1]]
    boxes: Vec<[f64; 4]>,           // x0 y0 x1 y1
    finite: Vec<bool>,
    off: Vec<usize>,
}

impl Blocks {
    fn of(xy: &numpy::ndarray::ArrayView2<f64>, off: &numpy::ndarray::ArrayView1<i64>) -> Blocks {
        let n = off.len().saturating_sub(1);
        let (mut first, mut boxes, mut finite) = (Vec::with_capacity(n + 1), Vec::new(), Vec::with_capacity(n));
        for k in 0..n {
            first.push(boxes.len());
            let (a, b) = (off[k] as usize, off[k + 1] as usize);
            finite.push((a..b).all(|v| xy[[v, 0]].is_finite() && xy[[v, 1]].is_finite()));
            let ns = (b - a).saturating_sub(1);
            let mut s0 = 0;
            while s0 < ns {
                let s1 = (s0 + BLOCK).min(ns);
                let mut r = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
                for v in a + s0..=a + s1 {
                    r[0] = r[0].min(xy[[v, 0]]);
                    r[1] = r[1].min(xy[[v, 1]]);
                    r[2] = r[2].max(xy[[v, 0]]);
                    r[3] = r[3].max(xy[[v, 1]]);
                }
                boxes.push(r);
                s0 = s1;
            }
        }
        first.push(boxes.len());
        Blocks { first, boxes, finite, off: off.iter().map(|&v| v as usize).collect() }
    }

    /// (segment, distance, t) of part j nearest (ax, ay): numpy's argmin of the distances (the first least; the first
    /// NaN, scanning whole). hint: a segment to measure first (the last point's nearest).
    fn nearest(&self, xy: &numpy::ndarray::ArrayView2<f64>, j: usize, ax: f64, ay: f64, hint: usize)
               -> (usize, f64, f64) {
        let b0 = self.off[j];
        let ns = self.off[j + 1] - b0 - 1;
        if !self.finite[j] || !(ax.is_finite() && ay.is_finite()) {
            let (mut m, (mut best, mut bt)) = (0usize, seg_dt(xy, b0, 0, ax, ay));
            if !best.is_nan() {
                for s in 1..ns {
                    let (d, t) = seg_dt(xy, b0, s, ax, ay);
                    if d.is_nan() || d < best {
                        (m, best, bt) = (s, d, t);
                        if d.is_nan() {
                            break;
                        }
                    }
                }
            }
            return (m, best, bt);
        }
        // (finite: no NaN distance; the least (distance, segment) whatever the order measured)
        let (mut m, mut best, mut bt) = (usize::MAX, f64::INFINITY, 0.0);
        let take = |s: usize, m: &mut usize, best: &mut f64, bt: &mut f64| {
            let (d, t) = seg_dt(xy, b0, s, ax, ay);
            if d < *best || (d == *best && s < *m) {
                (*m, *best, *bt) = (s, d, t);
            }
        };
        let hb = hint.min(ns - 1) / BLOCK;
        let (f0, f1) = (self.first[j], self.first[j + 1]);
        let span = |b: usize| (b * BLOCK, ((b + 1) * BLOCK).min(ns));
        let (h0, h1) = span(hb);
        for s in h0..h1 {
            take(s, &mut m, &mut best, &mut bt);
        }
        for b in 0..f1 - f0 {
            if b == hb {
                continue;
            }
            let r = self.boxes[f0 + b];
            let gx = (r[0] - ax).max(ax - r[2]).max(0.0);
            let gy = (r[1] - ay).max(ay - r[3]).max(0.0);
            if gx.dhypot(gy) > best + PRUNE_M {
                continue;
            }
            let (s0, s1) = span(b);
            for s in s0..s1 {
                take(s, &mut m, &mut best, &mut bt);
            }
        }
        (m, best, bt)
    }
}

/// One round of roads._merge_overlaps' pair pulls: (acc, cnt, up), each one value per height of zs (part k's heights
/// zs[zoff[k]..zoff[k + 1]], its line xy[off[k]..off[k + 1]]), summed over pairs (i, j) in their order into part i's
/// rows: every point of i projected onto j's nearest segment, weighted by how deep the surfaces overlap (widths, touch
/// m) and how parallel they run; dual[p]: the pair is one level's (its lift up to the higher, fading out from dual_m
/// apart). ValueError where a pair's part has under 2 points (numpy raised too) or heights are not one per point.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn deck_merge_round<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                             zs: PyReadonlyArray1<'py, f64>, zoff: PyReadonlyArray1<'py, i64>,
                             widths: PyReadonlyArray1<'py, f64>, pairs: PyReadonlyArray2<'py, i64>,
                             dual: PyReadonlyArray1<'py, u8>, touch: f64, dual_m: f64)
                             -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    let (xy, off, zs, zoff, widths, pairs, dual) = (xy.as_array(), off.as_array(), zs.as_array(), zoff.as_array(),
                                                    widths.as_array(), pairs.as_array(), dual.as_array());
    let nz = zs.len();
    let got = py.detach(|| -> Result<(Vec<f64>, Vec<f64>, Vec<f64>), String> {
        let mut acc = vec![0.0f64; nz];
        let mut cnt = vec![0.0f64; nz];
        let mut up = vec![0.0f64; nz];
        let blocks = Blocks::of(&xy, &off);
        for p in 0..pairs.nrows() {
            let (i, j) = (pairs[[p, 0]] as usize, pairs[[p, 1]] as usize);
            let (a0, a1) = (off[i] as usize, off[i + 1] as usize);
            let (b0, b1) = (off[j] as usize, off[j + 1] as usize);
            let (zi0, zi1) = (zoff[i] as usize, zoff[i + 1] as usize);
            let (zj0, zj1) = (zoff[j] as usize, zoff[j + 1] as usize);
            let (na, nb) = (a1 - a0, b1 - b0);
            if nb < 2 {
                return Err("attempt to get argmin of an empty sequence".into());
            }
            if na < 2 {
                return Err("Shape of array too small to calculate a numerical gradient".into());
            }
            if zi1 - zi0 != na || zj1 - zj0 != nb {
                return Err("operands could not be broadcast together".into());
            }
            let nzj = zj1 - zj0;
            let c = (widths[i] + widths[j]) / 2.0 + touch;
            let mut hint = 0usize;
            for a in 0..na {
                let (ax, ay) = (xy[[a0 + a, 0]], xy[[a0 + a, 1]]);
                let (m, dmin, tj) = blocks.nearest(&xy, j, ax, ay, hint);
                hint = m;
                let zm = zs[zj0 + m];
                let zj = zm + tj * (zs[zj0 + (m + 1).min(nzj - 1)] - zm);
                // np.gradient(A, axis=0): one-sided at the ends, central between
                let (tax, tay) = if a == 0 {
                    ((xy[[a0 + 1, 0]] - xy[[a0, 0]]) / 1.0, (xy[[a0 + 1, 1]] - xy[[a0, 1]]) / 1.0)
                } else if a == na - 1 {
                    ((xy[[a1 - 1, 0]] - xy[[a1 - 2, 0]]) / 1.0, (xy[[a1 - 1, 1]] - xy[[a1 - 2, 1]]) / 1.0)
                } else {
                    ((xy[[a0 + a + 1, 0]] - xy[[a0 + a - 1, 0]]) / 2.0,
                     (xy[[a0 + a + 1, 1]] - xy[[a0 + a - 1, 1]]) / 2.0)
                };
                let (px, py_) = (xy[[b0 + m, 0]], xy[[b0 + m, 1]]);
                let (tbx, tby) = (xy[[b0 + m + 1, 0]] - px, xy[[b0 + m + 1, 1]] - py_);
                let cos = (tax * tbx + tay * tby).abs() / (tax.dhypot(tay) * tbx.dhypot(tby) + 1e-12);
                // soft: full weight 2 m into the overlap and when parallel, none at its edge (a hard mask made 4 m
                // steps mid-viaduct where a neighbour's footprint started)
                let depth = np_clip((c - dmin) / 2.0, 0.0, 1.0);
                let par = np_clip((cos - 0.85) / 0.1, 0.0, 1.0);
                let fin = if zj.is_finite() { 1.0 } else { 0.0 };
                let w = depth * par * fin;
                let r = zi0 + a;
                acc[r] += w * nan_to_num(zj);
                cnt[r] += w;
                if dual[p] != 0 {
                    // one level's carriageways side by side are one deck (the Tsing Ma Bridge's two directions stood
                    // 15 m apart in height, the one over the lower deck lifted by the stacking, 2026-10-18): the lower
                    // lifted to the higher, fading out from dual_m apart
                    let w2 = np_clip((dual_m - dmin) / 10.0, 0.0, 1.0) * par;
                    let lift = if zj.is_finite() { w2 * np_maximum(nan_to_num(zj) - zs[r], 0.0) } else { 0.0 };
                    up[r] = np_maximum(up[r], lift);
                }
            }
        }
        Ok((acc, cnt, up))
    });
    let (acc, cnt, up) = got.map_err(PyValueError::new_err)?;
    Ok((acc.into_pyarray(py), cnt.into_pyarray(py), up.into_pyarray(py)))
}

/// roads._ground_pins: of the deck ends (e, 2) those another end lies within 0.5 m of (shared joints), each one within
/// pin_m of an end of a ground line (gl rows goff[k]..goff[k + 1]) pin_road_m or longer, per ground line and its first
/// then last end, in the ends' order (repeats kept). (m, 2).
#[pyfunction]
pub fn deck_ground_pins<'py>(py: Python<'py>, ends: PyReadonlyArray2<'py, f64>, gl: PyReadonlyArray2<'py, f64>,
                             goff: PyReadonlyArray1<'py, i64>, pin_road_m: f64, pin_m: f64)
                             -> Bound<'py, PyArray2<f64>> {
    let (ends, gl, goff) = (ends.as_array(), gl.as_array(), goff.as_array());
    let out = py.detach(|| {
        let ne = ends.nrows();
        let mut shared: Vec<(f64, f64)> = Vec::new();
        for r in 0..ne {
            let (bx, by) = (ends[[r, 0]], ends[[r, 1]]);
            let mut n = 0;
            for c in 0..ne {
                if (ends[[c, 0]] - bx).dhypot(ends[[c, 1]] - by) < 0.5 {
                    n += 1;
                }
            }
            if n > 1 {
                shared.push((bx, by));
            }
        }
        let mut out: Vec<f64> = Vec::new();
        if shared.is_empty() {
            return out;
        }
        let mut seg = Vec::new();
        for k in 0..goff.len().saturating_sub(1) {
            let (a, b) = (goff[k] as usize, goff[k + 1] as usize);
            if b < a + 2 {
                continue;
            }
            seg.clear();
            for v in a..b - 1 {
                seg.push((gl[[v + 1, 0]] - gl[[v, 0]]).dhypot(gl[[v + 1, 1]] - gl[[v, 1]]));
            }
            if crate::levels::np_sum(&seg) < pin_road_m {
                continue;
            }
            for e in [a, b - 1] {
                let (ex, ey) = (gl[[e, 0]], gl[[e, 1]]);
                for &(sx, sy) in &shared {
                    if (ex - sx).dhypot(ey - sy) < pin_m {
                        out.push(sx);
                        out.push(sy);
                    }
                }
            }
        }
        out
    });
    let n = out.len() / 2;
    numpy::ndarray::Array2::from_shape_vec((n, 2), out).unwrap().into_pyarray(py)
}
