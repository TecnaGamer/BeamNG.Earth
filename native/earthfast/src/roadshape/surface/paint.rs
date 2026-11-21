//! Coverage of the very same ground faces used by the road mesh. Union the
//! subpixel samples before resolving a pixel so lane edges don't leave a seam.
use super::{cross, sub};
use numpy::ndarray::Array3;
use numpy::{IntoPyArray, PyArray3, PyReadonlyArray2};
use pyo3::{exceptions::PyValueError, prelude::*};

#[pyfunction]
pub fn pavement_coverage<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>,
    tri: PyReadonlyArray2<'py, i64>,
    kinds: Vec<usize>,
    w: usize,
    h: usize,
    aa: bool,
) -> PyResult<Bound<'py, PyArray3<u8>>> {
    let (xy, tri) = (xy.as_array(), tri.as_array());
    if xy.ncols() != 2
        || tri.ncols() != 3
        || kinds.len() != tri.nrows()
        || kinds.iter().any(|&k| k >= 2)
        || tri.iter().any(|&i| i < 0 || i as usize >= xy.nrows())
        || !xy.iter().all(|v| v.is_finite())
    {
        return Err(PyValueError::new_err(
            "pavement_coverage: inconsistent mesh",
        ));
    }
    let samples: &[(f64, f64)] = if aa {
        &[(0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)]
    } else {
        &[(0.5, 0.5)]
    };
    let out = py.detach(|| {
        let mut bits = Array3::<u8>::zeros((2, h, w));
        for (t, k) in tri.rows().into_iter().zip(kinds) {
            let point = |i: usize| [xy[[t[i] as usize, 0]], xy[[t[i] as usize, 1]]];
            let p = [point(0), point(1), point(2)];
            let area = cross(sub(p[1], p[0]), sub(p[2], p[0]));
            if area == 0.0 {
                continue;
            }
            let edges = [sub(p[1], p[0]), sub(p[2], p[1]), sub(p[0], p[2])];
            let range = |axis: usize, limit: usize| {
                let q = p.map(|v| v[axis]);
                (q[0].min(q[1]).min(q[2]).floor() as usize).min(limit)
                    ..(q[0].max(q[1]).max(q[2]).ceil() as usize).min(limit)
            };
            for row in range(1, h) {
                for col in range(0, w) {
                    for (i, &(dx, dy)) in samples.iter().enumerate() {
                        let q = [col as f64 + dx, row as f64 + dy];
                        if (0..3).all(|e| cross(edges[e], sub(q, p[e])) * area >= 0.0) {
                            bits[[k, row, col]] |= 1 << i;
                        }
                    }
                }
            }
        }
        bits.mapv_inplace(|v| {
            ((255 * v.count_ones() + samples.len() as u32 / 2) / samples.len() as u32) as u8
        });
        bits
    });
    Ok(out.into_pyarray(py))
}
