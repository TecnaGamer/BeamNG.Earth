//! Partition in triangle order. Attribute computation and shell closure precede this step,
//! so a vertex referenced by two objects has exactly the same attributes in both.
use numpy::{ndarray::Array2, IntoPyArray, PyArray1, PyArray2, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

pub const MAX_VERTICES: usize = u16::MAX as usize;
type Part<'py> = (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<i64>>);

#[pyfunction]
#[pyo3(signature = (tris, nverts, limit = MAX_VERTICES))]
pub fn mesh_parts<'py>(
    py: Python<'py>,
    tris: PyReadonlyArray2<'py, i64>,
    nverts: usize,
    limit: usize,
) -> PyResult<Vec<Part<'py>>> {
    let tris = tris.as_array();
    if !(3..=MAX_VERTICES).contains(&limit) {
        return Err(PyValueError::new_err("invalid mesh vertex limit"));
    }
    if tris.ncols() != 3 {
        return Err(PyValueError::new_err("expected triangles"));
    }
    let chunks = py.detach(|| {
        let mut chunks = Vec::new();
        let mut local = vec![-1; nverts];
        let mut order = Vec::new();
        let mut indices = Vec::new();
        for row in tris.rows() {
            let triangle = [row[0] as usize, row[1] as usize, row[2] as usize];
            let mut needed = 0;
            for (k, &v) in triangle.iter().enumerate() {
                if v >= nverts {
                    return Err(PyValueError::new_err("triangle index outside vertex array"));
                }
                if local[v] == -1 && !triangle[..k].contains(&v) {
                    needed += 1;
                }
            }
            if order.len() + needed > limit {
                for &v in &order {
                    local[v as usize] = -1;
                }
                chunks.push((std::mem::take(&mut order), std::mem::take(&mut indices)));
            }
            for v in triangle {
                if local[v] == -1 {
                    local[v] = order.len() as i64;
                    order.push(v as i64);
                }
                indices.push(local[v]);
            }
        }
        if !indices.is_empty() {
            chunks.push((order, indices));
        }
        Ok(chunks)
    })?;
    Ok(chunks
        .into_iter()
        .map(|(order, indices)| {
            let tris = Array2::from_shape_vec((indices.len() / 3, 3), indices).unwrap();
            (order.into_pyarray(py), tris.into_pyarray(py))
        })
        .collect())
}

/// Input bodies are detail-major (all visible details, then explicit collision bodies).
/// TSShape stores an object's meshes consecutively, one slot per detail. Give extra chunks
/// separate objects with null meshes in any detail where that object has no triangles.
#[pyfunction]
pub fn mesh_slots(
    name: &str,
    counts: Vec<usize>,
    collisions: usize,
    collision_only: bool,
) -> (Vec<String>, Vec<Option<usize>>) {
    let mut names = Vec::new();
    let mut slots = Vec::new();
    let width = counts.iter().copied().max().unwrap_or(0);
    for i in 0..width {
        names.push(if i == 0 {
            name.to_owned()
        } else {
            format!("{name}{}", i + 1)
        });
        if collision_only {
            slots.push(None);
        }
        let mut offset = 0;
        for &count in &counts {
            slots.push((i < count).then_some(offset + i));
            offset += count;
        }
        if collisions > 0 {
            slots.push(None);
        }
    }
    let offset: usize = counts.iter().sum();
    for i in 0..collisions {
        names.push(format!("Colmesh{i}"));
        slots.extend(std::iter::repeat_n(None, counts.len()));
        slots.push(Some(offset + i));
    }
    (names, slots)
}
