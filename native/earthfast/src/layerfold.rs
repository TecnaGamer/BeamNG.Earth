//! Fewer ground layers per terrain block (surface.fold_layers): every distinct TerrainMaterial a block's .ter lists
//! costs the engine's texture-array rebuild at the swap (~20 ms each, in-game probes 2026-10-18), so a material
//! only a few scattered samples use is folded into the materials around it. Two kernels: the per-material statistics
//! the policy reads (samples, painted samples, core samples: those with nothing else within a radius, the part of a
//! layer seen as an area of its own rather than as specks and lines) and the fill (each sample of a dropped material
//! takes the material of the nearest kept sample, 8-connected breadth first).

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::prelude::*;
use std::collections::VecDeque;

const HOLE: u8 = 255;

/// The statistics of a block's layer map (pure): samples and painted samples per material, and the core samples of
/// each material with at most `max_count` samples (the others: 0): a sample whose (2 r + 1)² neighbourhood (Chebyshev
/// radius r, clipped at the map's edge) is all its own material. Only the rare materials' samples are looked at.
fn stats(idx: &[u8], painted: &[u8], n: usize, max_count: u64, r: usize) -> (Vec<u64>, Vec<u64>, Vec<u64>) {
    let mut counts = vec![0u64; 256];
    let mut paint = vec![0u64; 256];
    for i in 0..n * n {
        let m = idx[i] as usize;
        counts[m] += 1;
        if painted[i] != 0 {
            paint[m] += 1;
        }
    }
    let mut want = [false; 256];
    for m in 0..255 {
        want[m] = counts[m] > 0 && counts[m] <= max_count;
    }
    let mut core = vec![0u64; 256];
    if !want.iter().any(|w| *w) {
        return (counts, paint, core);
    }
    for i in 0..n * n {
        let m = idx[i];
        if !want[m as usize] {
            continue;
        }
        let (row, col) = (i / n, i % n);
        let (r0, r1) = (row.saturating_sub(r), (row + r).min(n - 1));
        let (c0, c1) = (col.saturating_sub(r), (col + r).min(n - 1));
        let inside = (r0..=r1).all(|rr| idx[rr * n + c0..=rr * n + c1].iter().all(|&v| v == m));
        if inside {
            core[m as usize] += 1;
        }
    }
    (counts, paint, core)
}

/// The fill (pure): each sample whose material is dropped takes the material of the nearest kept sample (breadth
/// first over the 8 neighbours through the dropped samples, sources in scan order: the same result every time), the
/// kept samples whose `painted` is 0 first (the land cover around a speck, not the road beside it), then any kept
/// sample for what those do not reach. Holes stay holes and pass nothing on. A dropped sample no kept sample reaches
/// keeps its material.
fn fold(idx: &[u8], drop: &[bool; 256], painted: &[u8], n: usize) -> Vec<u8> {
    let mut out = idx.to_vec();
    let dropped: Vec<usize> = (0..n * n).filter(|&i| idx[i] != HOLE && drop[idx[i] as usize]).collect();
    if dropped.is_empty() {
        return out;
    }
    let mut done = vec![true; n * n];
    for &i in &dropped {
        done[i] = false;
    }
    const NB: [(isize, isize); 8] = [(0, 1), (1, 0), (0, -1), (-1, 0), (1, 1), (1, -1), (-1, 1), (-1, -1)];
    let near = |i: usize| {
        let (r, c) = ((i / n) as isize, (i % n) as isize);
        NB.iter().filter_map(move |(dr, dc)| {
            let (rr, cc) = (r + dr, c + dc);
            (rr >= 0 && cc >= 0 && rr < n as isize && cc < n as isize).then(|| rr as usize * n + cc as usize)
        })
    };
    // the kept samples next to a dropped one, in scan order (the only ones a breadth-first fill from every kept
    // sample would start from: the same result)
    let mut front: Vec<usize> = dropped.iter().flat_map(|&i| near(i))
        .filter(|&j| idx[j] != HOLE && !drop[idx[j] as usize]).collect();
    front.sort_unstable();
    front.dedup();
    for pass in 0..2 {
        let mut q: VecDeque<usize> = front.iter().copied().filter(|&j| pass == 1 || painted[j] == 0).collect();
        while let Some(i) = q.pop_front() {
            let m = out[i];
            for j in near(i) {
                if done[j] {
                    continue;
                }
                done[j] = true;
                out[j] = m;
                q.push_back(j);
            }
        }
    }
    out
}

/// The array's bytes in row order: borrowed when contiguous (a 1024² map's copy was a few ms of each call), else copied.
fn bytes<'a>(a: &'a PyReadonlyArray2<'_, u8>) -> std::borrow::Cow<'a, [u8]> {
    match a.as_slice() {
        Ok(s) => std::borrow::Cow::Borrowed(s),
        Err(_) => std::borrow::Cow::Owned(a.as_array().iter().copied().collect()),
    }
}

fn square(shape: &[usize], what: &str) -> PyResult<usize> {
    if shape.len() != 2 || shape[0] != shape[1] || shape[0] == 0 {
        return Err(pyo3::exceptions::PyValueError::new_err(format!("{what}: a square layer map is needed")));
    }
    Ok(shape[0])
}

/// idx (n, n) u8: the block's material index per sample (255: a hole); painted (n, n) u8: nonzero where a sample's
/// material was painted over the land cover (roads, lots, fields, pads: never folded); max_count: the cores are
/// counted for the materials with at most this many samples; radius: the core's Chebyshev radius in samples. Returns
/// (samples, painted samples, core samples: 0 for the others) per material index, each (256,) u64.
#[pyfunction]
pub fn layer_stats<'py>(py: Python<'py>, idx: PyReadonlyArray2<'py, u8>, painted: PyReadonlyArray2<'py, u8>,
                        max_count: u64, radius: usize)
                        -> PyResult<(Bound<'py, PyArray1<u64>>, Bound<'py, PyArray1<u64>>, Bound<'py, PyArray1<u64>>)> {
    let n = square(idx.shape(), "layer_stats")?;
    if painted.shape() != idx.shape() {
        return Err(pyo3::exceptions::PyValueError::new_err("layer_stats: painted is not the layer map's shape"));
    }
    let (a, p) = (bytes(&idx), bytes(&painted));
    let (a, p): (&[u8], &[u8]) = (&a, &p);
    let (c, pa, pt) = py.detach(move || stats(a, p, n, max_count, radius));
    Ok((c.into_pyarray(py), pa.into_pyarray(py), pt.into_pyarray(py)))
}

/// idx (n, n) u8 and painted (n, n) u8 as layer_stats'; drop (256,) u8: nonzero for the materials to remove. Returns
/// the new layer map (the dropped materials' samples given their nearest kept neighbour's material, the land cover's
/// before a painted one: fold above).
#[pyfunction]
pub fn layer_fold<'py>(py: Python<'py>, idx: PyReadonlyArray2<'py, u8>, drop: PyReadonlyArray1<'py, u8>,
                       painted: PyReadonlyArray2<'py, u8>) -> PyResult<Bound<'py, PyArray2<u8>>> {
    let n = square(idx.shape(), "layer_fold")?;
    if painted.shape() != idx.shape() {
        return Err(pyo3::exceptions::PyValueError::new_err("layer_fold: painted is not the layer map's shape"));
    }
    let d = drop.as_array();
    if d.len() != 256 {
        return Err(pyo3::exceptions::PyValueError::new_err("layer_fold: drop must have 256 entries"));
    }
    let mut dm = [false; 256];
    for (k, v) in d.iter().enumerate() {
        dm[k] = *v != 0;
    }
    dm[HOLE as usize] = false;
    let (a, p) = (bytes(&idx), bytes(&painted));
    let (a, p): (&[u8], &[u8]) = (&a, &p);
    let out = py.detach(move || fold(a, &dm, p, n));
    let arr = numpy::ndarray::Array2::from_shape_vec((n, n), out)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok(arr.into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stats_cores() {
        // 5x5: material 1 a 3x3 block (one core sample at radius 1) and a speck, 2 elsewhere, one hole
        let idx = [1, 1, 1, 2, 2,
                   1, 1, 1, 2, 2,
                   1, 1, 1, 2, 1,
                   2, 2, 2, 2, 2,
                   2, 2, 2, 2, HOLE];
        let mut painted = [0u8; 25];
        painted[14] = 1;
        let (c, p, k) = stats(&idx, &painted, 5, 100, 1);
        assert_eq!((c[1], c[2], c[255]), (10, 14, 1));
        assert_eq!((p[1], p[2]), (1, 0));
        // material 1: (1, 1) and the clipped corner (0, 0)'s neighbourhood {(0..1), (0..1)} are all 1; (0, 1), (1, 0)
        // too: four; material 2's corner (4, 0) too and more
        assert_eq!(k[1], 4);
        assert_eq!(k[255], 0);
        // cores counted only up to max_count samples
        let (_, _, k) = stats(&idx, &painted, 5, 12, 1);
        assert_eq!((k[1], k[2]), (4, 0));
        // radius 0: every sample is its own core
        let (_, _, k) = stats(&idx, &painted, 5, 100, 0);
        assert_eq!(k[1], 10);
    }

    #[test]
    fn fold_nearest() {
        let idx = [3, 3, 3, 4,
                   3, 9, 4, 4,
                   3, 4, 4, 4,
                   HOLE, 4, 4, 9];
        let mut drop = [false; 256];
        drop[9] = true;
        let out = fold(&idx, &drop, &[0u8; 16], 4);
        assert!(!out.contains(&9));
        assert_eq!(out[15], 4);
        assert_eq!(out[12], HOLE);
        // (1, 1): the first source in scan order reaching it is (0, 0)'s 3
        assert_eq!(out[5], 3);
    }

    #[test]
    fn fold_prefers_land() {
        // a speck (9) between a painted road (1, left column) and grass (2): it takes the grass
        let idx = [1, 9, 2,
                   1, 2, 2,
                   1, 2, 2];
        let painted = [1, 0, 0, 1, 0, 0, 1, 0, 0];
        let mut drop = [false; 256];
        drop[9] = true;
        assert_eq!(fold(&idx, &drop, &painted, 3)[1], 2);
        // nothing but road around it: the road
        let idx2 = [1, 9, 1, 1];
        assert_eq!(fold(&idx2, &drop, &[1, 0, 1, 1], 2)[1], 1);
    }
}
