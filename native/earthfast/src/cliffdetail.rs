//! Conforming surface-space subdivision for tall canyon faces.
use crate::clifflod::{face_normals, grouped_edges, terrain_clearance};
use numpy::{ndarray::Array2, IntoPyArray, PyArray1, PyArray2, PyArrayMethods, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::{exceptions::PyValueError, prelude::*};

const VERTICAL_STEP_M: f64 = 2.0;
const MAX_SPLITS: usize = 8;
const MAX_ADDED_VERTICES: usize = 200_000;
// a,b,c, midpoint(ab), midpoint(bc), midpoint(ca). Every pattern preserves winding.
const SPLITS: [&[[usize; 3]]; 8] = [
    &[[0, 1, 2]],
    &[[0, 3, 2], [3, 1, 2]],
    &[[1, 4, 0], [4, 2, 0]],
    &[[1, 4, 3], [0, 3, 2], [3, 4, 2]],
    &[[2, 5, 1], [5, 0, 1]],
    &[[0, 3, 5], [3, 1, 2], [3, 2, 5]],
    &[[2, 5, 4], [0, 1, 5], [1, 4, 5]],
    &[[0, 3, 5], [3, 1, 4], [5, 4, 2], [3, 4, 5]],
];

fn append_midpoints(values: &Array2<f64>, pairs: &[[usize; 2]]) -> Array2<f64> {
    Array2::from_shape_fn((values.nrows() + pairs.len(), values.ncols()), |(i, c)| {
        if i < values.nrows() {
            values[[i, c]]
        } else {
            let [a, b] = pairs[i - values.nrows()];
            (values[[a, c]] + values[[b, c]]) * 0.5
        }
    })
}

fn subdivide(
    mut p: Array2<f64>,
    mut t: Array2<i64>,
    mut attributes: Array2<f64>,
    mut material: Vec<i32>,
    family: i32,
) -> (Array2<f64>, Array2<i64>, Array2<f64>, Vec<i32>) {
    let start = p.nrows();
    for _ in 0..MAX_SPLITS {
        let (edges, inverse, _) = grouped_edges(t.view());
        let face_count = t.nrows();
        // grouped_edges stores all ab, then bc, then ac edges. Resolve the
        // three per-face IDs without another edge table or sort.
        let edge_ids = |i: usize| {
            [
                inverse[i] as usize,
                inverse[face_count + i] as usize,
                inverse[2 * face_count + i] as usize,
            ]
        };
        let mut selected = vec![false; edges.nrows()];
        for (i, &m) in material.iter().enumerate() {
            if m == family {
                for e in edge_ids(i) {
                    selected[e] = true;
                }
            }
        }
        let mut pairs = Vec::new();
        let mut midpoint = vec![None; edges.nrows()];
        for (i, edge) in edges.rows().into_iter().enumerate() {
            let (a, b) = (edge[0] as usize, edge[1] as usize);
            if selected[i] && (p[[a, 2]] - p[[b, 2]]).abs() > VERTICAL_STEP_M {
                midpoint[i] = Some(p.nrows() + pairs.len());
                pairs.push([a, b]);
            }
        }
        if pairs.is_empty() || p.nrows() + pairs.len() > start + MAX_ADDED_VERTICES {
            break;
        }
        p = append_midpoints(&p, &pairs);
        attributes = append_midpoints(&attributes, &pairs);
        let masks: Vec<_> = (0..face_count)
            .map(|i| {
                edge_ids(i)
                    .iter()
                    .enumerate()
                    .fold(0, |mask, (bit, &e)| mask | ((midpoint[e].is_some() as usize) << bit))
            })
            .collect();
        let mut faces = Vec::new();
        let mut materials = Vec::new();
        // Stable pattern/source-face order matches the original authoring path.
        // Neighbouring non-canyon faces split too when they share a chosen edge.
        for (code, pattern) in SPLITS.iter().enumerate() {
            for i in 0..face_count {
                if masks[i] != code {
                    continue;
                }
                let e = edge_ids(i);
                let nodes = [
                    t[[i, 0]] as usize,
                    t[[i, 1]] as usize,
                    t[[i, 2]] as usize,
                    midpoint[e[0]].unwrap_or(0),
                    midpoint[e[1]].unwrap_or(0),
                    midpoint[e[2]].unwrap_or(0),
                ];
                for face in *pattern {
                    faces.extend(face.iter().map(|&v| nodes[v] as i64));
                    materials.push(material[i]);
                }
            }
        }
        t = Array2::from_shape_vec((materials.len(), 3), faces).unwrap();
        material = materials;
    }
    (p, t, attributes, material)
}

/// Subdivide selected-family vertical edges once for both incident faces.
/// Attributes are uninterpreted vertex channels (UVs, normals, colors, weights,
/// source relief); interpolating them together keeps every chart aligned.
#[pyfunction]
pub fn cliff_detail_subdivide<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    attributes: PyReadonlyArray2<'py, f64>,
    materials: PyReadonlyArray1<'py, i32>,
    family: i32,
) -> PyResult<(
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<i64>>,
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray1<i32>>,
)> {
    let (p, t, a, m) = (
        positions.as_array(),
        triangles.as_array(),
        attributes.as_array(),
        materials.as_array(),
    );
    if p.ncols() != 3
        || t.ncols() != 3
        || a.nrows() != p.nrows()
        || m.len() != t.nrows()
        || family < 0
        || m.iter().any(|&v| v < 0)
        || p.iter().any(|v| !v.is_finite())
        || t.iter().any(|&v| v < 0 || v as usize >= p.nrows())
    {
        return Err(PyValueError::new_err(
            "finite Nx3 positions, valid triangles and aligned attributes/materials required",
        ));
    }
    let (p, t, a, m) = py.detach(|| subdivide(p.to_owned(), t.to_owned(), a.to_owned(), m.to_vec(), family));
    Ok((
        p.into_pyarray(py),
        t.into_pyarray(py),
        a.into_pyarray(py),
        m.into_pyarray(py),
    ))
}

/// Keep original outlines, holes and non-target material vertices fixed.
#[pyfunction]
pub fn cliff_detail_weights<'py>(
    py: Python<'py>,
    triangles: PyReadonlyArray2<'py, i64>,
    materials: PyReadonlyArray1<'py, i32>,
    vertices: usize,
    family: i32,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (t, material) = (triangles.as_array(), materials.as_array());
    if t.ncols() != 3 || material.len() != t.nrows() || t.iter().any(|&v| v < 0 || v as usize >= vertices) {
        return Err(PyValueError::new_err(
            "valid triangles and one material per face required",
        ));
    }
    let weights = py.detach(|| {
        let (edges, _, counts) = grouped_edges(t);
        let mut weights = Array2::ones((vertices, 1));
        for (i, edge) in edges.rows().into_iter().enumerate() {
            if counts[i] != 2 {
                for &v in edge {
                    weights[[v as usize, 0]] = 0.0;
                }
            }
        }
        for (i, face) in t.rows().into_iter().enumerate() {
            if material[i] != family {
                for &v in face {
                    weights[[v as usize, 0]] = 0.0;
                }
            }
        }
        weights
    });
    Ok(weights.into_pyarray(py))
}

const MAX_RESIDUAL_M: f64 = 0.75;
const MAX_FULL_RELIEF_M: f64 = 1.5;
const GROUND_BISECTION_STEPS: usize = 6;
const FOLD_REFINE_STEPS: usize = 12;
const MIN_FACE_AREA_RATIO: f64 = 0.2;

/// Place sampled relief along vertex normals. Terrain intersections shorten
/// the displacement instead of lifting vertices vertically into texture fins.
/// Folded faces shorten all incident offsets together, preserving shared edges.
#[pyfunction]
pub fn cliff_detail_displace<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    normals: PyReadonlyArray2<'py, f64>,
    weights: PyReadonlyArray1<'py, f64>,
    sampled: PyReadonlyArray1<'py, f64>,
    coarse: PyReadonlyArray1<'py, f64>,
    full_relief: bool,
    ground: &Bound<'py, PyAny>,
) -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>)> {
    let (p, t, n, w, s, c) = (
        positions.as_array(),
        triangles.as_array(),
        normals.as_array(),
        weights.as_array(),
        sampled.as_array(),
        coarse.as_array(),
    );
    if p.ncols() != 3
        || t.ncols() != 3
        || n.dim() != p.dim()
        || w.len() != p.nrows()
        || s.len() != p.nrows()
        || c.len() != p.nrows()
        || t.iter().any(|&v| v < 0 || v as usize >= p.nrows())
        || p.iter()
            .chain(n.iter())
            .chain(w.iter())
            .chain(s.iter())
            .chain(c.iter())
            .any(|v| !v.is_finite())
    {
        return Err(PyValueError::new_err(
            "finite aligned positions/normals/relief channels and valid triangles required",
        ));
    }
    let (normal, residual, mut candidate) = py.detach(|| {
        let mut normal = n.to_owned();
        for mut row in normal.rows_mut() {
            let length = row.iter().map(|v| v * v).sum::<f64>().sqrt().max(1e-12);
            for v in &mut row {
                *v /= length;
            }
        }
        let residual: Vec<_> = (0..p.nrows())
            .map(|i| {
                let r = if full_relief { s[i] } else { s[i] - c[i] };
                r.clamp(
                    -MAX_RESIDUAL_M,
                    if full_relief { MAX_FULL_RELIEF_M } else { MAX_RESIDUAL_M },
                ) * w[i]
            })
            .collect();
        let candidate = Array2::from_shape_fn(p.dim(), |(i, c)| p[[i, c]] + normal[[i, c]] * residual[i]);
        (normal, residual, candidate)
    });
    if !ground.is_none() {
        let old_gap = terrain_clearance(py, p.to_owned(), ground)?;
        let gap = terrain_clearance(py, candidate.clone(), ground)?;
        let floor = |i: usize| {
            if old_gap[i].is_nan() {
                f64::NAN
            } else {
                old_gap[i].min(0.0)
            }
        };
        let ids: Vec<_> = (0..p.nrows())
            .filter(|&i| !gap[i].is_finite() || gap[i] < floor(i))
            .collect();
        let (mut lo, mut hi) = (vec![0.0; ids.len()], vec![1.0; ids.len()]);
        for _ in 0..GROUND_BISECTION_STEPS {
            let alpha: Vec<_> = lo.iter().zip(&hi).map(|(&l, &h)| (l + h) * 0.5).collect();
            let q = py.detach(|| {
                Array2::from_shape_fn((ids.len(), 3), |(j, c)| {
                    let i = ids[j];
                    p[[i, c]] + normal[[i, c]] * (residual[i] * alpha[j])
                })
            });
            let gap = terrain_clearance(py, q, ground)?;
            for (j, &i) in ids.iter().enumerate() {
                if gap[j].is_finite() && gap[j] >= floor(i) {
                    lo[j] = alpha[j];
                } else {
                    hi[j] = alpha[j];
                }
            }
        }
        py.detach(|| {
            for (j, &i) in ids.iter().enumerate() {
                for c in 0..3 {
                    candidate[[i, c]] = p[[i, c]] + normal[[i, c]] * (residual[i] * lo[j]);
                }
            }
        });
    }
    py.detach(|| {
        let base = face_normals(p, t);
        for _ in 0..FOLD_REFINE_STEPS {
            let cross = face_normals(candidate.view(), t);
            let mut bad = vec![false; p.nrows()];
            for (i, face) in t.rows().into_iter().enumerate() {
                let dot: f64 = (0..3).map(|c| cross[[i, c]] * base[[i, c]]).sum();
                let area: f64 = (0..3).map(|c| base[[i, c]] * base[[i, c]]).sum();
                if dot < MIN_FACE_AREA_RATIO * area {
                    for &v in face {
                        bad[v as usize] = true;
                    }
                }
            }
            if !bad.iter().any(|&v| v) {
                break;
            }
            for (i, &bad) in bad.iter().enumerate() {
                if bad {
                    for c in 0..3 {
                        candidate[[i, c]] = (candidate[[i, c]] + p[[i, c]]) * 0.5;
                    }
                }
            }
        }
    });
    let candidate = candidate.into_pyarray(py);
    let shaded = crate::face_normal_sum(py, candidate.readonly(), triangles);
    {
        let mut shaded = shaded.readwrite();
        let mut shaded = shaded.as_array_mut();
        for i in 0..p.nrows() {
            if w[i] == 0.0 {
                for c in 0..3 {
                    shaded[[i, c]] = normal[[i, c]];
                }
            }
        }
    }
    Ok((candidate, shaded))
}

#[cfg(test)]
mod tests {
    use super::*;
    use numpy::ndarray::array;

    #[test]
    fn split_patterns_cover_the_original_face_without_reversing_winding() {
        let nodes = [[0.0, 0.0], [2.0, 0.0], [0.0, 2.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]];
        for (code, pattern) in SPLITS.iter().enumerate() {
            let mut area = 0.0;
            for face in *pattern {
                for &node in face {
                    assert!(node < 3 || code & (1 << (node - 3)) != 0);
                }
                let [a, b, c] = face.map(|i| nodes[i]);
                let cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
                assert!(cross > 0.0);
                area += cross;
            }
            assert_eq!(area, 4.0);
        }
    }

    #[test]
    fn shared_material_edge_remains_conforming_and_channels_remain_aligned() {
        let p = array![[0., 0., 0.], [2., 0., 0.], [0., 1., 8.], [2., 1., 8.]];
        let t = array![[0, 1, 2], [1, 3, 2]];
        let attrs = Array2::from_shape_fn(
            (4, 2),
            |(i, c)| if c == 0 { p[[i, 0]] + 2. * p[[i, 1]] } else { p[[i, 2]] },
        );
        let (p, t, a, m) = subdivide(p, t, attrs, vec![5, 0], 5);
        assert!(p.nrows() > 4 && m.contains(&0) && m.contains(&5));
        let (edges, _, counts) = grouped_edges(t.view());
        for (i, edge) in edges.rows().into_iter().enumerate() {
            assert!(counts[i] <= 2);
            let (a, b) = (edge[0] as usize, edge[1] as usize);
            if counts[i] == 1 {
                // Only the original rectangle outline may be an open edge.
                assert!(
                    (p[[a, 0]] == p[[b, 0]] && (p[[a, 0]] == 0. || p[[a, 0]] == 2.))
                        || (p[[a, 1]] == p[[b, 1]] && (p[[a, 1]] == 0. || p[[a, 1]] == 1.))
                );
            }
        }
        for i in 0..p.nrows() {
            assert_eq!(a[[i, 0]], p[[i, 0]] + 2. * p[[i, 1]]);
            assert_eq!(a[[i, 1]], p[[i, 2]]);
        }
    }
}
