//! roadwidth.Walls' bucket index (the building walls a street's width is measured between, and the other users of
//! Walls: furniture, streetfront): one (segment, bucket) pair per bucket a segment's box touches, sorted by the bucket's
//! key. numpy made it in ~15 whole-array passes (repeat, argsort, ...) for every set of cells a road near a cell's
//! edge reads: ~1.7 % of a cold road tile's samples (Manhattan, Fells Point, 2026-11-10). The same buckets and keys;
//! within a bucket the segments in their own order (numpy's argsort left them in any: every reader takes the nearest
//! hit or the set).

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// (keys (B,), first (B,), counts (B,), seg_of (P,)) of segments segs (n, 4) x0 y0 x1 y1 in buckets of `bucket` m:
/// a bucket's key (bx + 2^30) * 2^31 + (by + 2^30), its segments seg_of[first..first + count].
#[pyfunction]
pub fn walls_index<'py>(py: Python<'py>, segs: PyReadonlyArray2<'py, f64>, bucket: f64)
                        -> PyResult<(Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>,
                                     Bound<'py, PyArray1<i64>>)> {
    let s = segs.as_array();
    let n = s.nrows();
    if n > 0 && s.ncols() < 4 {
        return Err(pyo3::exceptions::PyValueError::new_err("walls_index: segments (n, 4)"));
    }
    let rows: Vec<[f64; 4]> = (0..n).map(|i| [s[[i, 0]], s[[i, 1]], s[[i, 2]], s[[i, 3]]]).collect();
    let (keys, first, counts, seg_of) = crate::release(py, n * 8, move || {
        let key = |bx: i64, by: i64| (bx + (1i64 << 30)) * (1i64 << 31) + (by + (1i64 << 30));
        // (np.minimum / np.maximum: a NaN either way is NaN; floor of NaN cast to int64 is numpy's i64::MIN)
        let mn = |a: f64, b: f64| if a.is_nan() || b.is_nan() { f64::NAN } else { a.min(b) };
        let mx = |a: f64, b: f64| if a.is_nan() || b.is_nan() { f64::NAN } else { a.max(b) };
        let cast = |v: f64| if v.is_nan() { i64::MIN } else { v as i64 };
        let mut pairs: Vec<(i64, i64)> = Vec::with_capacity(n * 2);
        for (i, r) in rows.iter().enumerate() {
            let bx0 = cast((mn(r[0], r[2]) / bucket).floor());
            let bx1 = cast((mx(r[0], r[2]) / bucket).floor());
            let by0 = cast((mn(r[1], r[3]) / bucket).floor());
            let by1 = cast((mx(r[1], r[3]) / bucket).floor());
            if bx1 < bx0 || by1 < by0 || bx1 - bx0 > 1 << 20 || by1 - by0 > 1 << 20 {
                continue;
            }
            // (numpy's order of a segment's buckets: x fastest, then y)
            for by in by0..=by1 {
                for bx in bx0..=bx1 {
                    pairs.push((key(bx, by), i as i64));
                }
            }
        }
        pairs.sort_by_key(|p| p.0);
        let (mut keys, mut first, mut counts) = (Vec::new(), Vec::new(), Vec::new());
        let mut seg_of = Vec::with_capacity(pairs.len());
        for (j, p) in pairs.iter().enumerate() {
            if j == 0 || p.0 != pairs[j - 1].0 {
                keys.push(p.0);
                first.push(j as i64);
                counts.push(0i64);
            }
            *counts.last_mut().unwrap() += 1;
            seg_of.push(p.1);
        }
        (keys, first, counts, seg_of)
    });
    Ok((keys.into_pyarray(py), first.into_pyarray(py), counts.into_pyarray(py), seg_of.into_pyarray(py)))
}
