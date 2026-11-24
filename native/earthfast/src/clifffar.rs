//! Distant cliff selection on the terrain's existing triangles. No geometry is
//! added: Python supplies cached land cover at the returned candidate centres.
use crate::dm::Dm;
use numpy::{
    ndarray::{Array2, Array3, ArrayView1, ArrayView2},
    IntoPyArray, PyArray1, PyArray2, PyArray3, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3,
};
use pyo3::{exceptions::PyValueError, prelude::*};

const MIN_SLOPE_DEG: f64 = 48.0;
const MIN_FACE_DROP_M: f64 = 8.0;
const REFERENCE_GRID_M: f64 = 8.0;
const MAX_GRID_SPACING_M: f64 = 128.0;
const FOREST_WALL_MIN_SLOPE_DEG: f64 = 75.0;
const MIN_PROJECTED_AREA: f64 = 1e-5;

/// Vertices shared by a cliff and real upward-facing terrain, never skirts.
fn boundary_vertices(positions: ArrayView2<f64>, triangles: ArrayView2<i64>, labels: ArrayView1<i8>) -> Vec<i64> {
    let mut membership = vec![0_u8; positions.nrows()];
    for (face, triangle) in triangles.outer_iter().enumerate() {
        let indices = [triangle[0] as usize, triangle[1] as usize, triangle[2] as usize];
        let bit = if labels[face] >= 0 {
            1
        } else {
            let [a, b, c] = indices;
            let area = (positions[[b, 0]] - positions[[a, 0]]) * (positions[[c, 1]] - positions[[a, 1]])
                - (positions[[b, 1]] - positions[[a, 1]]) * (positions[[c, 0]] - positions[[a, 0]]);
            if area > MIN_PROJECTED_AREA {
                2
            } else {
                0
            }
        };
        for index in indices {
            membership[index] |= bit;
        }
    }
    membership
        .iter()
        .enumerate()
        .filter_map(|(i, &bits)| (bits == 3).then_some(i as i64))
        .collect()
}

#[pyfunction]
pub fn cliff_far_boundaries<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    labels: PyReadonlyArray1<'py, i8>,
) -> PyResult<Bound<'py, PyArray1<i64>>> {
    let (positions, triangles, labels) = (positions.as_array(), triangles.as_array(), labels.as_array());
    if positions.ncols() != 3
        || triangles.ncols() != 3
        || labels.len() != triangles.nrows()
        || triangles.iter().any(|&v| v < 0 || v as usize >= positions.nrows())
    {
        return Err(PyValueError::new_err(
            "(N,3) positions and valid (M,3) triangles with M labels required",
        ));
    }
    Ok(py
        .detach(|| boundary_vertices(positions, triangles, labels))
        .into_pyarray(py))
}

/// Compact each material/projection chart in deterministic source-index order.
/// Absolute UV1 is derived before rebasing either chart, preserving tile phase.
#[pyfunction]
pub fn cliff_far_layout<'py>(
    py: Python<'py>,
    triangles: PyReadonlyArray2<'py, i64>,
    labels: PyReadonlyArray1<'py, i8>,
    axes: PyReadonlyArray1<'py, bool>,
    chart: PyReadonlyArray2<'py, f64>,
    terrain_uv: PyReadonlyArray2<'py, f64>,
    terrain_uv2: PyReadonlyArray2<'py, f64>,
    terrain_tangent: PyReadonlyArray2<'py, f64>,
    scales: PyReadonlyArray2<'py, f64>,
) -> PyResult<
    Vec<(
        i32,
        Bound<'py, PyArray1<i64>>,
        Bound<'py, PyArray2<i64>>,
        Bound<'py, PyArray2<f64>>,
        Bound<'py, PyArray2<f64>>,
        Bound<'py, PyArray2<f64>>,
    )>,
> {
    let (triangles, labels, axes, chart) = (
        triangles.as_array(),
        labels.as_array(),
        axes.as_array(),
        chart.as_array(),
    );
    let (uv, uv2, tangent, scales) = (
        terrain_uv.as_array(),
        terrain_uv2.as_array(),
        terrain_tangent.as_array(),
        scales.as_array(),
    );
    let n = chart.nrows();
    if triangles.ncols() != 3
        || labels.len() != triangles.nrows()
        || axes.len() != labels.len()
        || chart.ncols() != 3
        || uv.dim() != (n, 2)
        || uv2.dim() != (n, 2)
        || tangent.dim() != (n, 2)
        || scales.dim() != (3, 4)
        || scales.iter().any(|v| !v.is_finite() || *v <= 0.0)
        || labels.iter().any(|v| !(-1..=2).contains(v))
        || triangles.iter().any(|&v| v < 0 || v as usize >= n)
    {
        return Err(PyValueError::new_err(
            "matching triangle labels/axes, vertex charts and positive (3,4) scales required",
        ));
    }
    let groups = py.detach(|| {
        let mut groups = Vec::new();
        let mut offset = 0_i64;
        for group in 0..7 {
            let material = if group == 0 { 0 } else { (group + 1) / 2 };
            let axis = group > 0 && group % 2 == 0;
            let selected: Vec<usize> = (0..labels.len())
                .filter(|&i| {
                    if group == 0 {
                        labels[i] < 0
                    } else {
                        labels[i] as i32 == material - 1 && axes[i] == axis
                    }
                })
                .collect();
            if selected.is_empty() {
                continue;
            }
            let mut used: Vec<i64> = selected
                .iter()
                .flat_map(|&i| [triangles[[i, 0]], triangles[[i, 1]], triangles[[i, 2]]])
                .collect();
            used.sort_unstable();
            used.dedup();
            let mut remap = vec![0_i64; n];
            for (i, &source) in used.iter().enumerate() {
                remap[source as usize] = offset + i as i64;
            }
            let tris = Array2::from_shape_fn((selected.len(), 3), |(i, c)| {
                remap[triangles[[selected[i], c]] as usize]
            });
            let mut out_uv = Array2::zeros((used.len(), 2));
            let mut out_uv2 = Array2::zeros((used.len(), 2));
            let mut out_tangent = Array2::zeros((used.len(), 2));
            let mut origin = [0.0; 2];
            let mut origin2 = [0.0; 2];
            for (i, &source) in used.iter().enumerate() {
                let source = source as usize;
                for c in 0..2 {
                    if group == 0 {
                        out_uv[[i, c]] = uv[[source, c]];
                        out_uv2[[i, c]] = uv2[[source, c]];
                        out_tangent[[i, c]] = tangent[[source, c]];
                    } else {
                        let family = material as usize - 1;
                        let coordinate = if c == 0 { axis as usize } else { 2 };
                        let value = chart[[source, coordinate]] / scales[[family, c]];
                        let value2 = value / scales[[family, c + 2]];
                        if i == 0 {
                            origin[c] = value.floor();
                            origin2[c] = value2.floor();
                        }
                        out_uv[[i, c]] = value - origin[c];
                        out_uv2[[i, c]] = value2 - origin2[c];
                        out_tangent[[i, c]] = out_uv2[[i, c]];
                    }
                }
            }
            offset += used.len() as i64;
            groups.push((material, used, tris, out_uv, out_uv2, out_tangent));
        }
        groups
    });
    Ok(groups
        .into_iter()
        .map(|(material, used, tris, uv, uv2, tangent)| {
            (
                material,
                used.into_pyarray(py),
                tris.into_pyarray(py),
                uv.into_pyarray(py),
                uv2.into_pyarray(py),
                tangent.into_pyarray(py),
            )
        })
        .collect())
}

/// Filter satellite macro color with the upstream photo filter, then convert to
/// linear light. Keep decoding in Python; no full-sized float temporaries there.
#[pyfunction]
pub fn cliff_far_photo_field<'py>(
    py: Python<'py>,
    pixels: PyReadonlyArray3<'py, u8>,
    radius: f64,
) -> PyResult<Bound<'py, PyArray3<f32>>> {
    let pixels = pixels.as_array();
    let (h, w, channels) = pixels.dim();
    if h == 0 || w == 0 || channels != 3 || !radius.is_finite() || radius < 0.0 || radius > i32::MAX as f64 / 4.0 {
        return Err(PyValueError::new_err(
            "nonempty RGB image and finite nonnegative blur radius required",
        ));
    }
    let result = py.detach(|| {
        let input: Vec<u8> = pixels.iter().copied().collect();
        let filtered = crate::photofill::gaussian_blur(&input, h, w, 3, radius as f32);
        let linear: [f32; 256] = std::array::from_fn(|i| crate::cliffappearance::linear(i as f64 / 255.0) as f32);
        Array3::from_shape_vec((h, w, 3), filtered.into_iter().map(|v| linear[v as usize]).collect()).unwrap()
    });
    Ok(result.into_pyarray(py))
}

/// Reduce one image strip to a row of shader gains, in linear light. Python
/// decodes strips so a 4K source never needs a full-resolution float copy.
#[pyfunction]
pub fn cliff_far_detail_row<'py>(
    py: Python<'py>,
    pixels: PyReadonlyArray3<'py, u8>,
    columns: usize,
    strength: f64,
    factor: [f64; 3],
) -> PyResult<Bound<'py, PyArray2<f32>>> {
    let pixels = pixels.as_array();
    let (h, w, channels) = pixels.dim();
    if columns == 0
        || h == 0
        || w == 0
        || w % columns != 0
        || channels != 3
        || !strength.is_finite()
        || factor.iter().any(|v| !v.is_finite())
    {
        return Err(PyValueError::new_err(
            "RGB strip width must divide into nonzero columns, with finite shader parameters",
        ));
    }
    let result = py.detach(|| {
        let gain: [f64; 256] = std::array::from_fn(|i| {
            (1.0 + (2.0 * crate::cliffappearance::linear(i as f64 / 255.0) - 1.0) * strength).clamp(0.0, 1.0)
        });
        let mut sums = Array2::<f64>::zeros((columns, 3));
        let block = w / columns;
        for y in 0..h {
            for x in 0..w {
                for c in 0..3 {
                    sums[[x / block, c]] += gain[pixels[[y, x, c]] as usize];
                }
            }
        }
        Array2::from_shape_fn((columns, 3), |(x, c)| {
            (sums[[x, c]] / (h * block) as f64 * factor[c]) as f32
        })
    });
    Ok(result.into_pyarray(py))
}

/// Sample linear RGB fields at texel centres. Satellite fields clamp at their
/// gutter and use north-first rows; authored detail fields wrap in wall UV space.
#[pyfunction]
pub fn cliff_far_sample_colors<'py>(
    py: Python<'py>,
    colors: PyReadonlyArray3<'py, f32>,
    uv: PyReadonlyArray2<'py, f64>,
    periodic: bool,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (colors, uv) = (colors.as_array(), uv.as_array());
    let (h, w, channels) = colors.dim();
    if h == 0 || w == 0 || channels != 3 || uv.ncols() != 2 || uv.iter().any(|x| !x.is_finite()) {
        return Err(PyValueError::new_err(
            "nonempty RGB field and finite (N, 2) UV coordinates required",
        ));
    }
    let result = py.detach(|| {
        let mut result = Array2::zeros((uv.nrows(), 3));
        for (i, point) in uv.outer_iter().enumerate() {
            let axis = |value: f64, count: usize| {
                let x = if periodic {
                    value.rem_euclid(1.0)
                } else {
                    value.clamp(0.0, 1.0)
                };
                let x = x * count as f64 - 0.5;
                let x = if periodic { x } else { x.clamp(0.0, (count - 1) as f64) };
                let base = x.floor() as isize;
                let index = |n: isize| {
                    if periodic {
                        n.rem_euclid(count as isize) as usize
                    } else {
                        n.clamp(0, count as isize - 1) as usize
                    }
                };
                (index(base), index(base + 1), x - base as f64)
            };
            let (x0, x1, fx) = axis(point[0], w);
            let (y0, y1, fy) = axis(if periodic { point[1] } else { 1.0 - point[1] }, h);
            for c in 0..3 {
                let top = (1.0 - fx) * colors[[y0, x0, c]] as f64 + fx * colors[[y0, x1, c]] as f64;
                let bottom = (1.0 - fx) * colors[[y1, x0, c]] as f64 + fx * colors[[y1, x1, c]] as f64;
                result[[i, c]] = (1.0 - fy) * top + fy * bottom;
            }
        }
        result
    });
    Ok(result.into_pyarray(py))
}

/// None for terrain/skirt faces; otherwise whether this is a sheer forest rim.
fn eligible(v: [[f64; 3]; 3]) -> Option<bool> {
    if v.iter().flatten().any(|a| !a.is_finite()) {
        return None;
    }
    let a: [f64; 3] = std::array::from_fn(|i| v[1][i] - v[0][i]);
    let b: [f64; 3] = std::array::from_fn(|i| v[2][i] - v[0][i]);
    let normal = [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ];
    let horizontal = normal[0].dhypot(normal[1]);
    let z = normal[2];
    // Vertical stitching skirts have no projected area; reversed faces stay terrain.
    if z <= MIN_PROJECTED_AREA || horizontal <= z * MIN_SLOPE_DEG.to_radians().dtan() {
        return None;
    }
    let mut span = 0.0_f64;
    for i in 0..3 {
        let j = (i + 1) % 3;
        span = span.max(((v[i][0] - v[j][0]).sq() + (v[i][1] - v[j][1]).sq()).sqrt());
    }
    // Scale the relief gate with footprint, so refinement cannot erase a wall.
    let drop =
        v.iter().map(|p| p[2]).fold(f64::NEG_INFINITY, f64::max) - v.iter().map(|p| p[2]).fold(f64::INFINITY, f64::min);
    let minimum = MIN_FACE_DROP_M * (span / (REFERENCE_GRID_M * 2.0_f64.sqrt())).min(1.0);
    (drop >= minimum).then_some(horizontal >= z * FOREST_WALL_MIN_SLOPE_DEG.to_radians().dtan())
}

#[pyfunction]
pub fn cliff_far_candidates<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    grid_spacing: f64,
) -> PyResult<(
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray1<bool>>,
)> {
    let (p, t) = (positions.as_array(), triangles.as_array());
    if p.ncols() != 3
        || t.ncols() != 3
        || !grid_spacing.is_finite()
        || grid_spacing <= 0.0
        || t.iter().any(|&i| i < 0 || i as usize >= p.nrows())
    {
        return Err(PyValueError::new_err(
            "Nx3 vertices, valid triangle indices and positive grid spacing required",
        ));
    }
    let (indices, centers, forest) = py.detach(|| {
        let (mut indices, mut centers, mut forest) = (Vec::new(), Vec::new(), Vec::new());
        if grid_spacing <= MAX_GRID_SPACING_M {
            for (row, tri) in t.rows().into_iter().enumerate() {
                let v: [[f64; 3]; 3] = std::array::from_fn(|i| std::array::from_fn(|a| p[[tri[i] as usize, a]]));
                if let Some(rim) = eligible(v) {
                    indices.push(row as i64);
                    centers.extend((0..3).map(|a| (v[0][a] + v[1][a] + v[2][a]) / 3.0));
                    forest.push(rim);
                }
            }
        }
        (indices, centers, forest)
    });
    let centers = Array2::from_shape_vec((indices.len(), 3), centers).unwrap();
    Ok((
        indices.into_pyarray(py),
        centers.into_pyarray(py),
        forest.into_pyarray(py),
    ))
}

#[cfg(test)]
mod tests {
    #[test]
    fn boundaries_exclude_skirts_reversed_faces_and_unshared_vertices() {
        use numpy::ndarray::array;
        let positions = array![[0., 0., 0.], [1., 0., 0.], [0., 1., 0.], [1., 1., 0.], [0., 0., -2.]];
        let faces = array![[0, 1, 2], [1, 3, 2], [0, 4, 1]];
        let labels = array![1_i8, -1, -1];
        assert_eq!(
            super::boundary_vertices(positions.view(), faces.view(), labels.view()),
            vec![1, 2]
        );
        let reversed = array![[0, 1, 2], [1, 2, 3], [0, 4, 1]];
        assert!(super::boundary_vertices(positions.view(), reversed.view(), labels.view()).is_empty());
        let all_rock = array![0_i8, 1, 2];
        assert!(super::boundary_vertices(positions.view(), faces.view(), all_rock.view()).is_empty());
    }

    use super::*;
    #[test]
    fn refinement_preserves_wall_eligibility() {
        for spacing in [0.5, 1.0, 2.0, 4.0, 8.0, 16.0] {
            assert_eq!(
                eligible([[0., 0., 0.], [spacing, 0., spacing * 1.5], [0., spacing, 0.]]),
                Some(false)
            );
        }
    }
    #[test]
    fn skirts_flat_faces_and_missing_vertices_stay_terrain() {
        assert_eq!(eligible([[0., 0., 0.], [0., 0., -10.], [1., 0., 0.]]), None);
        assert_eq!(eligible([[0., 0., 0.], [16., 0., 2.], [0., 16., 0.]]), None);
        assert_eq!(eligible([[0., 0., 0.], [16., 0., f64::NAN], [0., 16., 0.]]), None);
        assert_eq!(eligible([[0., 0., 0.], [16., 0., 100.], [0., 16., 0.]]), Some(true));
    }
}

/// Connected wall charts stop at material changes, opposing folds or faces
/// poorly aligned with the seed's projection. Confidence ties keep face order.
#[pyfunction]
pub fn cliff_far_projection_axes<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    labels: numpy::PyReadonlyArray1<'py, i8>,
) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let (p, t, labels) = (positions.as_array(), triangles.as_array(), labels.as_array());
    if p.ncols() != 3
        || t.ncols() != 3
        || labels.len() != t.nrows()
        || t.iter().any(|&i| i < 0 || i as usize >= p.nrows())
    {
        return Err(PyValueError::new_err(
            "Nx3 vertices, valid triangle indices and one label per face required",
        ));
    }
    let out = py.detach(|| {
        let mut direction = Vec::with_capacity(t.nrows());
        let mut dominant = Vec::with_capacity(t.nrows());
        let mut active = Vec::new();
        let mut edges = Vec::new();
        for (row, tri) in t.rows().into_iter().enumerate() {
            let a: [f64; 3] = std::array::from_fn(|i| p[[tri[1] as usize, i]] - p[[tri[0] as usize, i]]);
            let b: [f64; 3] = std::array::from_fn(|i| p[[tri[2] as usize, i]] - p[[tri[0] as usize, i]]);
            let (x, y) = (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2]);
            let length = (x * x + y * y).sqrt().max(1e-12);
            let d = [x / length, y / length];
            direction.push(d);
            dominant.push(d[0].abs() >= d[1].abs());
            if labels[row] >= 0 {
                active.push(row);
                for slot in 0..3 {
                    let (a, b) = (tri[slot], tri[(slot + 1) % 3]);
                    edges.push((a.min(b), a.max(b), row, slot));
                }
            }
        }
        edges.sort_by_key(|&(a, b, _, _)| (a, b));
        let mut pairs = Vec::new();
        for pair in edges.windows(2) {
            let (a, b) = (pair[0], pair[1]);
            if (a.0, a.1) == (b.0, b.1)
                && labels[a.2] == labels[b.2]
                && direction[a.2][0] * direction[b.2][0] + direction[a.2][1] * direction[b.2][1] >= 0.0
            {
                pairs.push((a, b));
            }
        }
        let mut neighbours = vec![[None; 3]; t.nrows()];
        // Separate passes preserve deterministic edge-slot precedence even on
        // non-manifold input. Terrain normally has at most two faces per edge.
        for &(a, b) in &pairs {
            neighbours[a.2][a.3] = Some(b.2);
        }
        for &(a, b) in &pairs {
            neighbours[b.2][b.3] = Some(a.2);
        }
        let confidence = |i: usize| (direction[i][0].abs() - direction[i][1].abs()).abs();
        active.sort_by(|&a, &b| confidence(b).total_cmp(&confidence(a)));
        let mut assigned: Vec<bool> = labels.iter().map(|&v| v < 0).collect();
        for seed in active {
            if assigned[seed] {
                continue;
            }
            let axis = dominant[seed];
            let component = if axis { 0 } else { 1 };
            let sign = if direction[seed][component] >= 0.0 { 1.0 } else { -1.0 };
            assigned[seed] = true;
            let mut stack = vec![seed];
            while let Some(face) = stack.pop() {
                dominant[face] = axis;
                for other in neighbours[face].into_iter().flatten() {
                    // At most about 3:1 compression along a coherent wall chart.
                    if !assigned[other] && sign * direction[other][component] >= 0.35 {
                        assigned[other] = true;
                        stack.push(other);
                    }
                }
            }
        }
        dominant
    });
    Ok(out.into_pyarray(py))
}
