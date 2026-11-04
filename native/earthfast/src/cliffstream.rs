//! Spatial batches for cliff streaming: preserve every input triangle and vertex attribute.
use numpy::{PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::{exceptions::PyValueError, prelude::*};

fn partition(
    centers: &[[f64; 3]],
    mut ids: Vec<usize>,
    limit: usize,
    slots: usize,
    out: &mut Vec<Vec<usize>>,
) {
    if ids.len() <= limit || slots <= 1 {
        out.push(ids);
        return;
    }
    let mut lo = [f64::INFINITY; 3];
    let mut hi = [f64::NEG_INFINITY; 3];
    for &i in &ids {
        for axis in 0..3 {
            lo[axis] = lo[axis].min(centers[i][axis]);
            hi[axis] = hi[axis].max(centers[i][axis]);
        }
    }
    let axis = (0..3)
        .max_by(|&a, &b| (hi[a] - lo[a]).total_cmp(&(hi[b] - lo[b])))
        .unwrap();
    // Allocate just the pieces required by the triangle budget. Equal halving
    // doubles every leaf when a contour adds a few faces over a power of two.
    let slots = slots.min(ids.len().div_ceil(limit));
    let left_slots = slots / 2;
    let mid = ids.len() * left_slots / slots;
    ids.select_nth_unstable_by(mid, |&a, &b| {
        centers[a][axis]
            .total_cmp(&centers[b][axis])
            .then(a.cmp(&b))
    });
    let right = ids.split_off(mid);
    partition(centers, ids, limit, left_slots, out);
    partition(centers, right, limit, slots - left_slots, out);
}

/// Deterministic spatial batches of triangle indices. Shared vertices are copied verbatim by the caller.
#[pyfunction]
pub fn cliff_stream_parts(
    py: Python<'_>,
    positions: PyReadonlyArray2<'_, f64>,
    tris: PyReadonlyArray2<'_, i64>,
    limit: usize,
    max_parts: usize,
) -> PyResult<Vec<Vec<usize>>> {
    if positions.shape()[1] != 3 || tris.shape()[1] != 3 || limit == 0 || max_parts == 0 {
        return Err(PyValueError::new_err(
            "expected Nx3 positions/triangles and positive limits",
        ));
    }
    let p = positions.as_array();
    let t = tris.as_array();
    py.allow_threads(move || {
        let mut centers = Vec::with_capacity(t.nrows());
        for row in t.rows() {
            let mut c = [0.; 3];
            for &v in row {
                if v < 0 || v as usize >= p.nrows() {
                    return Err(PyValueError::new_err("triangle index out of range"));
                }
                for axis in 0..3 {
                    c[axis] += p[[v as usize, axis]] / 3.;
                }
            }
            if c.iter().any(|v| !v.is_finite()) {
                return Err(PyValueError::new_err("nonfinite vertex"));
            }
            centers.push(c);
        }
        let mut out = Vec::new();
        if !centers.is_empty() {
            partition(
                &centers,
                (0..centers.len()).collect(),
                limit,
                max_parts,
                &mut out,
            );
        }
        Ok(out)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn batches_are_bounded_complete_and_deterministic() {
        let centers: Vec<_> = (0..1003)
            .map(|i| [(i % 37) as f64, (i / 37) as f64, 0.])
            .collect();
        let mut a = Vec::new();
        let mut b = Vec::new();
        partition(&centers, (0..centers.len()).collect(), 64, 64, &mut a);
        partition(&centers, (0..centers.len()).collect(), 64, 64, &mut b);
        assert_eq!(a, b);
        assert!(a.iter().all(|v| v.len() <= 64));
        let mut all: Vec<_> = a.into_iter().flatten().collect();
        all.sort_unstable();
        assert_eq!(all, (0..1003).collect::<Vec<_>>());
        let mut capped = Vec::new();
        partition(&centers, (0..1003).collect(), 1, 4, &mut capped);
        assert_eq!(capped.len(), 4);
    }
}
