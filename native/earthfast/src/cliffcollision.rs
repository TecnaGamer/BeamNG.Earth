//! Collision-only simplification. Output references original vertices; borders never move.
use numpy::{PyReadonlyArray1, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::{exceptions::PyValueError, prelude::*};

/// Reduce a single material's faces with locked boundaries and an absolute quadric error.
/// The caller keeps original f64 positions and calls separately for each collision material.
#[pyfunction]
pub fn cliff_collision_indices(
    py: Python<'_>,
    positions: PyReadonlyArray2<'_, f64>,
    tris: PyReadonlyArray2<'_, i64>,
    locks: PyReadonlyArray1<'_, u8>,
    target_triangles: usize,
    error_m: f32,
) -> PyResult<(Vec<u32>, f32)> {
    if positions.shape()[1] != 3 || tris.shape()[1] != 3
        || locks.len() != positions.shape()[0]
        || !error_m.is_finite() || error_m < 0.0
    {
        return Err(PyValueError::new_err("expected Nx3 arrays and a finite nonnegative error"));
    }
    let p = positions.as_array();
    let t = tris.as_array();
    let locked: Vec<bool> = locks.as_array().iter().map(|&v| v != 0).collect();
    py.allow_threads(move || {
        if p.nrows() > u32::MAX as usize {
            return Err(PyValueError::new_err("too many vertices"));
        }
        let origin = if p.nrows() == 0 { [0.0; 3] } else { [p[[0, 0]], p[[0, 1]], p[[0, 2]]] };
        let mut vertices = Vec::with_capacity(p.nrows());
        for row in p.rows() {
            let q = [(row[0]-origin[0]) as f32, (row[1]-origin[1]) as f32, (row[2]-origin[2]) as f32];
            if q.iter().any(|v| !v.is_finite()) {
                return Err(PyValueError::new_err("nonfinite position"));
            }
            vertices.push(q);
        }
        let mut indices = Vec::with_capacity(t.len());
        for &v in t.iter() {
            if v < 0 || v as usize >= vertices.len() {
                return Err(PyValueError::new_err("triangle index out of range"));
            }
            indices.push(v as u32);
        }
        if indices.is_empty() { return Ok((indices, 0.0)); }
        let adapter = meshopt::VertexDataAdapter::new(bytemuck::cast_slice(&vertices), 12, 0)
            .map_err(|e| PyValueError::new_err(e.to_string()))?;
        let mut error = 0.0;
        let result = meshopt::simplify_with_locks(&indices, &adapter, &locked,
            target_triangles.min(indices.len()/3)*3, error_m,
            meshopt::SimplifyOptions::LockBorder | meshopt::SimplifyOptions::ErrorAbsolute
                | meshopt::SimplifyOptions::PreserveFolds,
            Some(&mut error));
        Ok((result, error))
    })
}
