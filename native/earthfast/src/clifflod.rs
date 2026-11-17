//! Connected spatial clustering for cliff LODs. Never weld disconnected faces.
use numpy::{
    ndarray::{Array2, ArrayView2},
    IntoPyArray, PyArray1, PyArray2, PyArrayMethods, PyReadonlyArray1, PyReadonlyArray2,
};
use pyo3::{exceptions::PyValueError, prelude::*, types::PyDict};

pub(crate) fn face_normals(p: ArrayView2<f64>, t: ArrayView2<i64>) -> Array2<f64> {
    Array2::from_shape_fn((t.nrows(), 3), |(i, axis)| {
        let (a, b, c) = (t[[i, 0]] as usize, t[[i, 1]] as usize, t[[i, 2]] as usize);
        let (j, k) = ((axis + 1) % 3, (axis + 2) % 3);
        (p[[b, j]] - p[[a, j]]) * (p[[c, k]] - p[[a, k]]) - (p[[b, k]] - p[[a, k]]) * (p[[c, j]] - p[[a, j]])
    })
}

pub(crate) fn grouped_edges(t: ArrayView2<i64>) -> (Array2<i64>, Vec<i64>, Vec<i64>) {
    let mut ordered = Vec::with_capacity(t.nrows() * 3);
    for (a, b) in [(0, 1), (1, 2), (0, 2)] {
        for row in t.rows() {
            let edge = [row[a].min(row[b]), row[a].max(row[b])];
            ordered.push((edge, ordered.len()));
        }
    }
    ordered.sort_unstable_by_key(|&(edge, index)| (edge, index));
    let mut edges: Vec<i64> = Vec::new();
    let mut counts: Vec<i64> = Vec::new();
    let mut inverse = vec![0_i64; ordered.len()];
    let mut previous = None;
    for (edge, index) in ordered {
        if previous != Some(edge) {
            edges.extend(edge);
            counts.push(0);
            previous = Some(edge);
        }
        let group = counts.len() - 1;
        inverse[index] = group as i64;
        counts[group] += 1;
    }
    (
        Array2::from_shape_vec((counts.len(), 2), edges).unwrap(),
        inverse,
        counts,
    )
}

/// Group sorted undirected edges, retaining the source order (01, 12, 02).
#[pyfunction]
pub fn cliff_lod_edges<'py>(
    py: Python<'py>,
    triangles: PyReadonlyArray2<'py, i64>,
    vertices: usize,
) -> PyResult<(
    Bound<'py, PyArray2<i64>>,
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<i64>>,
)> {
    let t = triangles.as_array();
    if t.ncols() != 3 || t.iter().any(|&i| i < 0 || i as usize >= vertices) {
        return Err(PyValueError::new_err("valid (M,3) triangle indices required"));
    }
    let (edges, inverse, counts) = py.detach(|| grouped_edges(t));
    Ok((
        edges.into_pyarray(py),
        inverse.into_pyarray(py),
        counts.into_pyarray(py),
    ))
}

/// Topology shared by every LOD trial: open/nonmanifold and material edges
/// are locked, while original face normals remain the reference for fold tests.
#[pyfunction]
pub fn cliff_lod_prepare<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    material: PyReadonlyArray1<'py, i32>,
) -> PyResult<(
    Bound<'py, PyArray2<i64>>,
    Bound<'py, PyArray1<bool>>,
    Bound<'py, PyArray2<f64>>,
)> {
    let (p, t, material) = (positions.as_array(), triangles.as_array(), material.as_array());
    if p.ncols() != 3
        || t.ncols() != 3
        || material.len() != t.nrows()
        || p.iter().any(|v| !v.is_finite())
        || material.iter().any(|&v| v < 0)
        || t.iter().any(|&v| v < 0 || v as usize >= p.nrows())
    {
        return Err(PyValueError::new_err(
            "finite Nx3 positions, valid Mx3 triangles and M nonnegative materials required",
        ));
    }
    let (edges, locked, normal) = py.detach(|| {
        let (edges, inverse, counts) = grouped_edges(t);
        let mut first = vec![None; edges.nrows()];
        let mut seam = vec![false; edges.nrows()];
        for (source, &group) in inverse.iter().enumerate() {
            let group = group as usize;
            let value = material[source % t.nrows()];
            match first[group] {
                Some(previous) => seam[group] |= previous != value,
                None => first[group] = Some(value),
            }
        }
        let mut locked = vec![false; p.nrows()];
        for (i, edge) in edges.rows().into_iter().enumerate() {
            if counts[i] != 2 || seam[i] {
                locked[edge[0] as usize] = true;
                locked[edge[1] as usize] = true;
            }
        }
        let normal = face_normals(p, t);
        (edges, locked, normal)
    });
    Ok((edges.into_pyarray(py), locked.into_pyarray(py), normal.into_pyarray(py)))
}

fn root(parent: &mut [i64], mut i: usize) -> usize {
    while parent[i] as usize != i {
        parent[i] = parent[parent[i] as usize];
        i = parent[i] as usize;
    }
    i
}

/// Union only edge-connected, unlocked vertices within a cell. Earlier accepted
/// clusters stay indivisible, even when they cross the next level's cell borders.
#[pyfunction]
pub fn cliff_lod_clusters<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    edges: PyReadonlyArray2<'py, i64>,
    locked: PyReadonlyArray1<'py, bool>,
    baseline: PyReadonlyArray1<'py, i64>,
    cell_m: f64,
) -> PyResult<Bound<'py, PyArray1<i64>>> {
    let (p, e, locked, base) = (
        positions.as_array(),
        edges.as_array(),
        locked.as_array(),
        baseline.as_array(),
    );
    let n = p.nrows();
    if p.ncols() != 3
        || e.ncols() != 2
        || locked.len() != n
        || base.len() != n
        || !cell_m.is_finite()
        || cell_m <= 0.0
        || p.iter().any(|v| !v.is_finite())
        || e.iter().chain(base.iter()).any(|&v| v < 0 || v as usize >= n)
    {
        return Err(PyValueError::new_err(
            "finite Nx3 positions, valid edges/root labels, locks and positive cell size required",
        ));
    }
    if base.iter().any(|&v| base[v as usize] != v) {
        return Err(PyValueError::new_err("baseline labels must reference roots"));
    }
    let labels = py.detach(|| {
        let mut origin = [f64::INFINITY; 3];
        for point in p.rows() {
            for axis in 0..3 {
                origin[axis] = origin[axis].min(point[axis]);
            }
        }
        let cells: Vec<[f64; 3]> = p
            .rows()
            .into_iter()
            .map(|point| std::array::from_fn(|axis| ((point[axis] - origin[axis]) / cell_m).floor()))
            .collect();
        let mut labels: Vec<i64> = base.iter().copied().collect();
        for edge in e.rows() {
            let (a, b) = (edge[0] as usize, edge[1] as usize);
            if !locked[a] && !locked[b] && cells[a] == cells[b] {
                let (ra, rb) = (root(&mut labels, a), root(&mut labels, b));
                labels[ra.max(rb)] = ra.min(rb) as i64;
            }
        }
        for i in 0..n {
            labels[i] = root(&mut labels, i) as i64;
        }
        labels
    });
    Ok(labels.into_pyarray(py))
}
/// Average arbitrary vertex channels using a compact mapping. Iteration order
/// matches the source vertices, preserving reproducible sums for every channel.
fn average(values: ArrayView2<f64>, mapping: &[usize], groups: usize) -> Array2<f64> {
    let mut result = Array2::zeros((groups, values.ncols()));
    let mut counts = vec![0_usize; groups];
    for (i, row) in values.rows().into_iter().enumerate() {
        let group = mapping[i];
        counts[group] += 1;
        for (c, &value) in row.iter().enumerate() {
            result[[group, c]] += value;
        }
    }
    for (group, mut row) in result.rows_mut().into_iter().enumerate() {
        for value in row.iter_mut() {
            *value /= counts[group] as f64;
        }
    }
    result
}

/// Construct a trial's compact geometry before clearance and topology checks.
/// Groups follow ascending source label order; collapsed faces remain in place
/// with a false keep flag so material and source-normal indices stay aligned.
#[pyfunction]
pub fn cliff_lod_trial<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    labels: PyReadonlyArray1<'py, i64>,
) -> PyResult<(
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<i64>>,
    Bound<'py, PyArray1<bool>>,
    Bound<'py, PyArray2<f64>>,
)> {
    let (p, t, labels) = (positions.as_array(), triangles.as_array(), labels.as_array());
    let n = p.nrows();
    if p.ncols() != 3
        || t.ncols() != 3
        || labels.len() != n
        || p.iter().any(|v| !v.is_finite())
        || t.iter().chain(labels.iter()).any(|&v| v < 0 || v as usize >= n)
    {
        return Err(PyValueError::new_err(
            "finite Nx3 positions, valid Mx3 triangles and N source labels required",
        ));
    }
    let (mapping, weights, pos, tri, keep, normal) = py.detach(|| {
        let mut counts = vec![0_i64; n];
        for &label in &labels {
            counts[label as usize] += 1;
        }
        let mut compact = vec![0_usize; n];
        let mut weights = Vec::new();
        for (label, &count) in counts.iter().enumerate() {
            if count > 0 {
                compact[label] = weights.len();
                weights.push(count);
            }
        }
        let mapping: Vec<usize> = labels.iter().map(|&label| compact[label as usize]).collect();
        let pos = average(p, &mapping, weights.len());
        let tri = Array2::from_shape_fn(t.dim(), |(i, c)| mapping[t[[i, c]] as usize] as i64);
        let keep: Vec<bool> = tri
            .rows()
            .into_iter()
            .map(|r| r[0] != r[1] && r[1] != r[2] && r[0] != r[2])
            .collect();
        let normal = face_normals(pos.view(), tri.view());
        (
            mapping.into_iter().map(|v| v as i64).collect::<Vec<_>>(),
            weights,
            pos,
            tri,
            keep,
            normal,
        )
    });
    Ok((
        mapping.into_pyarray(py),
        weights.into_pyarray(py),
        pos.into_pyarray(py),
        tri.into_pyarray(py),
        keep.into_pyarray(py),
        normal.into_pyarray(py),
    ))
}

/// Reject changed faces that fold, duplicate another face, or touch a newly
/// nonmanifold edge. Unchanged source slivers must not veto a useful LOD.
#[pyfunction]
pub fn cliff_lod_validate<'py>(
    py: Python<'py>,
    triangles: PyReadonlyArray2<'py, i64>,
    weights: PyReadonlyArray1<'py, i64>,
    normal: PyReadonlyArray2<'py, f64>,
    source_normal: PyReadonlyArray2<'py, f64>,
    min_normal_cos: f64,
) -> PyResult<(Bound<'py, PyArray1<bool>>, Bound<'py, PyArray1<bool>>)> {
    use std::collections::BTreeMap;
    let (t, weights, normal, source) = (
        triangles.as_array(),
        weights.as_array(),
        normal.as_array(),
        source_normal.as_array(),
    );
    if t.ncols() != 3
        || normal.dim() != t.dim()
        || source.dim() != t.dim()
        || t.iter().any(|&v| v < 0 || v as usize >= weights.len())
        || weights.iter().any(|&v| v <= 0)
        || !min_normal_cos.is_finite()
        || !(-1.0..=1.0).contains(&min_normal_cos)
    {
        return Err(PyValueError::new_err(
            "aligned triangle normals, valid indices and positive group weights required",
        ));
    }
    let (changed, bad) = py.detach(|| {
        let mut faces = BTreeMap::<[i64; 3], usize>::new();
        let mut edges = BTreeMap::<[i64; 2], usize>::new();
        let mut keep = vec![false; t.nrows()];
        for (i, row) in t.rows().into_iter().enumerate() {
            if row[0] == row[1] || row[1] == row[2] || row[0] == row[2] {
                continue;
            }
            keep[i] = true;
            let mut face = [row[0], row[1], row[2]];
            face.sort_unstable();
            *faces.entry(face).or_default() += 1;
            for (a, b) in [(0, 1), (1, 2), (0, 2)] {
                *edges.entry([row[a].min(row[b]), row[a].max(row[b])]).or_default() += 1;
            }
        }
        let mut nonmanifold = vec![false; weights.len()];
        for (edge, count) in edges {
            if count > 2 {
                for v in edge {
                    nonmanifold[v as usize] = true;
                }
            }
        }
        let mut changed = vec![false; t.nrows()];
        let mut bad = vec![false; t.nrows()];
        for (i, row) in t.rows().into_iter().enumerate() {
            changed[i] = keep[i] && row.iter().any(|&v| weights[v as usize] > 1);
            if !changed[i] {
                continue;
            }
            let mut face = [row[0], row[1], row[2]];
            face.sort_unstable();
            let dot: f64 = (0..3).map(|c| normal[[i, c]] * source[[i, c]]).sum();
            let n2: f64 = (0..3).map(|c| normal[[i, c]] * normal[[i, c]]).sum();
            let s2: f64 = (0..3).map(|c| source[[i, c]] * source[[i, c]]).sum();
            let product = n2.sqrt() * s2.sqrt();
            bad[i] = dot <= min_normal_cos * product
                || product < 1e-12
                || !product.is_finite()
                || faces[&face] > 1
                || row.iter().any(|&v| nonmanifold[v as usize]);
        }
        (changed, bad)
    });
    Ok((changed.into_pyarray(py), bad.into_pyarray(py)))
}

/// Conservative surface retreat check for standalone skins without a terrain
/// sampler: test both the new face plane and the original vertex normals.
#[pyfunction]
pub fn cliff_lod_retreat<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    reduced: PyReadonlyArray2<'py, f64>,
    mapping: PyReadonlyArray1<'py, i64>,
    normals: PyReadonlyArray2<'py, f64>,
    max_retreat: f64,
) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let (p, t, pos, map, normals) = (
        positions.as_array(),
        triangles.as_array(),
        reduced.as_array(),
        mapping.as_array(),
        normals.as_array(),
    );
    let have_normals = normals.nrows() != 0;
    if p.ncols() != 3
        || t.ncols() != 3
        || pos.ncols() != 3
        || normals.ncols() != 3
        || map.len() != p.nrows()
        || (have_normals && normals.nrows() != p.nrows())
        || !max_retreat.is_finite()
        || max_retreat < 0.0
        || t.iter().any(|&i| i < 0 || i as usize >= p.nrows())
        || map.iter().any(|&i| i < 0 || i as usize >= pos.nrows())
    {
        return Err(PyValueError::new_err(
            "aligned source/reduced positions, valid indices and nonnegative retreat required",
        ));
    }
    let bad = py.detach(|| {
        let tri = Array2::from_shape_fn(t.dim(), |(i, c)| map[t[[i, c]] as usize]);
        let nn = face_normals(pos, tri.view());
        let mut vertex_bad = vec![false; p.nrows()];
        if have_normals {
            for i in 0..p.nrows() {
                let retreat: f64 = (0..3)
                    .map(|c| (p[[i, c]] - pos[[map[i] as usize, c]]) * normals[[i, c]])
                    .sum();
                vertex_bad[i] = retreat > max_retreat;
            }
        }
        let mut bad = vec![false; t.nrows()];
        for (i, row) in t.rows().into_iter().enumerate() {
            bad[i] = row.iter().any(|&v| vertex_bad[v as usize]);
            let tr = tri.row(i);
            if tr[0] == tr[1] || tr[1] == tr[2] || tr[0] == tr[2] {
                continue;
            }
            let length = (0..3).map(|c| nn[[i, c]] * nn[[i, c]]).sum::<f64>().sqrt().max(1e-12);
            for &v in row {
                let retreat: f64 = (0..3)
                    .map(|c| (p[[v as usize, c]] - pos[[tr[0] as usize, c]]) * (nn[[i, c]] / length))
                    .sum();
                bad[i] |= retreat > max_retreat;
            }
        }
        bad
    });
    Ok(bad.into_pyarray(py))
}

/// Sample the terrain envelope beneath local mesh points using the same ENU
/// kernels as upstream geodesy. Missing or out-of-grid terrain stays invalid.
#[pyfunction]
pub fn cliff_lod_ground<'py>(
    py: Python<'py>,
    points: PyReadonlyArray2<'py, f64>,
    heights: PyReadonlyArray2<'py, f64>,
    origin: PyReadonlyArray1<'py, f64>,
    rotation: PyReadonlyArray2<'py, f64>,
    offset: (f64, f64),
    grid: (f64, f64, f64, f64),
    longitude: f64,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (o, r) = crate::frame_of(&origin, &rotation)?;
    let (points, heights) = (points.as_array(), heights.as_array());
    let (x0, y0, dx, dy) = grid;
    if points.ncols() != 3
        || heights.nrows() < 2
        || heights.ncols() < 2
        || ![x0, y0, dx, dy, offset.0, offset.1, longitude]
            .iter()
            .all(|v| v.is_finite())
        || dx <= 0.0
        || dy <= 0.0
    {
        return Err(PyValueError::new_err(
            "Nx3 points, at least 2x2 terrain and finite positive grid spacing required",
        ));
    }
    let z = heights
        .as_slice()
        .ok_or_else(|| PyValueError::new_err("contiguous terrain required"))?;
    let out = py.detach(|| {
        points
            .rows()
            .into_iter()
            .map(|p| {
                if !p.iter().all(|v| v.is_finite()) {
                    return f64::NAN;
                }
                // These are full ENU points, not zero-height world coordinates.
                let (lat, lon, _) = crate::e2g_one(p[0] + offset.0, p[1] + offset.1, p[2], &o, &r);
                let lon = longitude + (lon - longitude + 180.0).rem_euclid(360.0) - 180.0;
                const METRES_PER_DEGREE: f64 = 111320.0;
                let height = crate::cliffselect::sample(
                    z,
                    heights.ncols(),
                    heights.nrows(),
                    (lon * METRES_PER_DEGREE - x0) / dx,
                    (lat * METRES_PER_DEGREE - y0) / dy,
                );
                crate::g2e_one(lat, lon, height, &o, &r).2
            })
            .collect::<Vec<_>>()
    });
    Ok(out.into_pyarray(py))
}

const CLEARANCE_PROBES: [[f64; 3]; 7] = [
    [1.0, 0.0, 0.0],
    [0.0, 1.0, 0.0],
    [0.0, 0.0, 1.0],
    [0.5, 0.5, 0.0],
    [0.0, 0.5, 0.5],
    [0.5, 0.0, 0.5],
    [1.0 / 3.0; 3],
];

fn probes(p: ArrayView2<f64>, t: ArrayView2<i64>, ids: &[usize], bary: [f64; 3]) -> Array2<f64> {
    Array2::from_shape_fn((ids.len(), 3), |(i, c)| {
        let row = t.row(ids[i]);
        p[[row[0] as usize, c]] * bary[0] + p[[row[1] as usize, c]] * bary[1] + p[[row[2] as usize, c]] * bary[2]
    })
}

/// Invoke the supplied terrain provider only at the boundary. Probe generation
/// and clearance decisions stay native; production's provider samples the DEM
/// envelope natively, while tests can supply synthetic terrain profiles.
pub(crate) fn terrain_clearance(py: Python<'_>, points: Array2<f64>, ground: &Bound<'_, PyAny>) -> PyResult<Vec<f64>> {
    let z: Vec<_> = points.column(2).iter().copied().collect();
    let heights = ground.call1((points.into_pyarray(py),))?;
    let heights = heights.extract::<PyReadonlyArray1<f64>>()?;
    let heights = heights.as_array();
    if heights.len() != z.len() {
        return Err(PyValueError::new_err(
            "terrain sampler must return one height per probe",
        ));
    }
    Ok(py.detach(|| (0..z.len()).map(|i| z[i] - heights[i]).collect()))
}

/// Check face interiors as well as corners: a chord between clear endpoints
/// can cross a ridge. Source clearances are cached across all LOD trials so
/// sunken boundary faces cannot progressively widen their existing blend.
#[pyfunction]
pub fn cliff_lod_clearance<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    reduced: PyReadonlyArray2<'py, f64>,
    reduced_triangles: PyReadonlyArray2<'py, i64>,
    changed: PyReadonlyArray1<'py, bool>,
    ground: &Bound<'py, PyAny>,
    cache: &Bound<'py, PyDict>,
    max_inward: f64,
) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let (p, t, pos, tri, changed) = (
        positions.as_array(),
        triangles.as_array(),
        reduced.as_array(),
        reduced_triangles.as_array(),
        changed.as_array(),
    );
    if p.ncols() != 3
        || pos.ncols() != 3
        || t.ncols() != 3
        || tri.dim() != t.dim()
        || changed.len() != t.nrows()
        || !max_inward.is_finite()
        || max_inward < 0.0
        || t.iter().any(|&v| v < 0 || v as usize >= p.nrows())
        || tri.iter().any(|&v| v < 0 || v as usize >= pos.nrows())
    {
        return Err(PyValueError::new_err(
            "aligned source/trial triangles and nonnegative clearance tolerance required",
        ));
    }
    let ids: Vec<_> = (0..t.nrows()).filter(|&i| changed[i]).collect();
    let all: Vec<_> = (0..t.nrows()).collect();
    let mut bad = vec![false; t.nrows()];
    for bary in CLEARANCE_PROBES {
        let q = py.detach(|| probes(pos, tri, &ids, bary));
        let clearance = terrain_clearance(py, q, ground)?;
        let key = (bary[0], bary[1], bary[2]);
        let previous = match cache.get_item(key)? {
            Some(value) => value,
            None => {
                let old = py.detach(|| probes(p, t, &all, bary));
                let values = terrain_clearance(py, old, ground)?.into_pyarray(py);
                cache.set_item(key, &values)?;
                values.into_any()
            }
        };
        let previous = previous.extract::<PyReadonlyArray1<f64>>()?;
        let previous = previous.as_array();
        if previous.len() != t.nrows() {
            return Err(PyValueError::new_err(
                "source clearance cache must match source triangle count",
            ));
        }
        py.detach(|| {
            for (j, &i) in ids.iter().enumerate() {
                // Preserve missing source samples as NaN, like numpy.minimum.
                let floor = if previous[i].is_nan() {
                    f64::NAN
                } else {
                    previous[i].min(0.0)
                };
                bad[i] |= !clearance[j].is_finite() || clearance[j] < floor - max_inward;
            }
        });
    }
    Ok(bad.into_pyarray(py))
}

/// Compact an accepted trial once and carry every vertex channel through the
/// same mapping. Keeping this together prevents UV/color/normal misalignment.
#[pyfunction]
pub fn cliff_lod_output<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    keep: PyReadonlyArray1<'py, bool>,
    mapping: PyReadonlyArray1<'py, i64>,
    uvs: PyReadonlyArray2<'py, f64>,
    colors: PyReadonlyArray2<'py, f64>,
    materials: PyReadonlyArray1<'py, i32>,
    terrain_blend: bool,
    normals: Option<PyReadonlyArray2<'py, f64>>,
    uvs2: Option<PyReadonlyArray2<'py, f64>>,
) -> PyResult<Bound<'py, PyDict>> {
    let (p, t, keep, map, uv, color, material) = (
        positions.as_array(),
        triangles.as_array(),
        keep.as_array(),
        mapping.as_array(),
        uvs.as_array(),
        colors.as_array(),
        materials.as_array(),
    );
    let normal = normals.as_ref().map(|a| a.as_array());
    let uv2 = uvs2.as_ref().map(|a| a.as_array());
    let n = map.len();
    if p.ncols() != 3
        || t.ncols() != 3
        || keep.len() != t.nrows()
        || material.len() != t.nrows()
        || uv.dim() != (n, 2)
        || color.dim() != (n, 4)
        || normal.is_some_and(|a| a.dim() != (n, 3))
        || uv2.is_some_and(|a| a.dim() != (n, 2))
        || map.iter().chain(t.iter()).any(|&v| v < 0 || v as usize >= p.nrows())
        || color.iter().any(|v| !v.is_finite() || !(0.0..=255.0).contains(v))
    {
        return Err(PyValueError::new_err(
            "aligned trial geometry, vertex channels and byte-range colors required",
        ));
    }
    let (pos, tri, uv, color, normal, material, uv2) = py.detach(|| {
        let map: Vec<usize> = map.iter().map(|&v| v as usize).collect();
        let mut populated = vec![false; p.nrows()];
        for &v in &map {
            populated[v] = true;
        }
        if populated.iter().any(|&v| !v) {
            return Err(PyValueError::new_err("all trial groups must contain a source vertex"));
        }
        let faces: Vec<_> = (0..t.nrows()).filter(|&i| keep[i]).collect();
        let mut referenced = vec![false; p.nrows()];
        for &i in &faces {
            for &v in t.row(i) {
                referenced[v as usize] = true;
            }
        }
        let used: Vec<_> = (0..p.nrows()).filter(|&v| referenced[v]).collect();
        let mut remap = vec![0; p.nrows()];
        for (i, &v) in used.iter().enumerate() {
            remap[v] = i;
        }
        let select = |a: ArrayView2<f64>| Array2::from_shape_fn((used.len(), a.ncols()), |(i, c)| a[[used[i], c]]);
        let compact = |a: ArrayView2<f64>| select(average(a, &map, p.nrows()).view());
        let normal = normal.map(|a| {
            let mut out = compact(a);
            for mut row in out.rows_mut() {
                let length = row.iter().map(|v| v * v).sum::<f64>().sqrt().max(1e-12);
                for v in &mut row {
                    *v /= length;
                }
            }
            out
        });
        Ok((
            select(p),
            Array2::from_shape_fn((faces.len(), 3), |(i, c)| remap[t[[faces[i], c]] as usize] as i64),
            compact(uv),
            compact(color).mapv(|v| v.round_ties_even() as u8),
            normal,
            faces.iter().map(|&i| material[i]).collect::<Vec<_>>(),
            uv2.map(compact),
        ))
    })?;
    let result = PyDict::new(py);
    result.set_item("positions", pos.into_pyarray(py))?;
    result.set_item("tris", tri.into_pyarray(py))?;
    let uv = uv.into_pyarray(py);
    let uv2 = uv2.map(|a| a.into_pyarray(py));
    result.set_item("uvs", &uv)?;
    result.set_item("colors", color.into_pyarray(py))?;
    result.set_item("normals", normal.map(|a| a.into_pyarray(py)))?;
    result.set_item("tri_materials", material.into_pyarray(py))?;
    result.set_item("uvs2", &uv2)?;
    result.set_item("tangent_uvs", uv2.as_ref().map(|a| if terrain_blend { &uv } else { a }))?;
    Ok(result)
}

/// Undo every source vertex in a rejected cluster, retaining the previously
/// accepted finer mapping. A partial undo could tear adjacent faces apart.
#[pyfunction]
pub fn cliff_lod_restore<'py>(
    py: Python<'py>,
    triangles: PyReadonlyArray2<'py, i64>,
    bad: PyReadonlyArray1<'py, bool>,
    mapping: PyReadonlyArray1<'py, i64>,
    labels: PyReadonlyArray1<'py, i64>,
    baseline: PyReadonlyArray1<'py, i64>,
) -> PyResult<Bound<'py, PyArray1<i64>>> {
    let (t, bad, map, labels, baseline) = (
        triangles.as_array(),
        bad.as_array(),
        mapping.as_array(),
        labels.as_array(),
        baseline.as_array(),
    );
    let n = map.len();
    if t.ncols() != 3
        || bad.len() != t.nrows()
        || labels.len() != n
        || baseline.len() != n
        || t.iter()
            .chain(map.iter())
            .chain(labels.iter())
            .chain(baseline.iter())
            .any(|&v| v < 0 || v as usize >= n)
    {
        return Err(PyValueError::new_err(
            "aligned face flags and valid vertex mappings required",
        ));
    }
    let restored = py.detach(|| {
        let mut rejected = vec![false; n];
        for (i, row) in t.rows().into_iter().enumerate() {
            if bad[i] {
                for &v in row {
                    rejected[v as usize] = true;
                }
            }
        }
        (0..n)
            .map(|i| {
                if rejected[map[i] as usize] {
                    baseline[i]
                } else {
                    labels[i]
                }
            })
            .collect::<Vec<_>>()
    });
    Ok(restored.into_pyarray(py))
}

const MAX_TRIALS: usize = 8;
const MIN_NORMAL_COS: f64 = 0.15;
// Retreat must remain a small fraction of the skin's terrain clearance.
const MAX_INWARD_ERROR_M: f64 = 0.03;
const MAX_SURFACE_RETREAT_M: f64 = 0.12;

type Reduction<'py> = (
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<i64>>,
    Bound<'py, PyArray1<bool>>,
    Bound<'py, PyArray1<i64>>,
);

/// Retry a connected-cell reduction against the original surface. Each rejected
/// group falls back to the last accepted finer grouping, never a partially
/// validated trial. Exhaustion returns None so the caller retains the source.
#[pyfunction]
pub fn cliff_lod_reduce<'py>(
    py: Python<'py>,
    positions: &Bound<'py, PyArray2<f64>>,
    triangles: &Bound<'py, PyArray2<i64>>,
    edges: &Bound<'py, PyArray2<i64>>,
    locked: &Bound<'py, PyArray1<bool>>,
    source_normal: &Bound<'py, PyArray2<f64>>,
    normals: &Bound<'py, PyArray2<f64>>,
    baseline: &Bound<'py, PyArray1<i64>>,
    cell_m: f64,
    ground: &Bound<'py, PyAny>,
    cache: &Bound<'py, PyDict>,
) -> PyResult<Option<Reduction<'py>>> {
    let mut labels = cliff_lod_clusters(
        py,
        positions.readonly(),
        edges.readonly(),
        locked.readonly(),
        baseline.readonly(),
        cell_m,
    )?;
    for _ in 0..MAX_TRIALS {
        let (mapping, weights, pos, tri, keep, normal) =
            cliff_lod_trial(py, positions.readonly(), triangles.readonly(), labels.readonly())?;
        let (changed, invalid) = cliff_lod_validate(
            py,
            tri.readonly(),
            weights.readonly(),
            normal.readonly(),
            source_normal.readonly(),
            MIN_NORMAL_COS,
        )?;
        let clearance = if ground.is_none() {
            cliff_lod_retreat(
                py,
                positions.readonly(),
                triangles.readonly(),
                pos.readonly(),
                mapping.readonly(),
                normals.readonly(),
                MAX_SURFACE_RETREAT_M,
            )?
        } else {
            cliff_lod_clearance(
                py,
                positions.readonly(),
                triangles.readonly(),
                pos.readonly(),
                tri.readonly(),
                changed.readonly(),
                ground,
                cache,
                MAX_INWARD_ERROR_M,
            )?
        };
        let bad = {
            let (clearance, invalid, changed) = (clearance.readonly(), invalid.readonly(), changed.readonly());
            let (clearance, invalid, changed) = (clearance.as_array(), invalid.as_array(), changed.as_array());
            py.detach(|| {
                (0..changed.len())
                    .map(|i| changed[i] && (clearance[i] || invalid[i]))
                    .collect::<Vec<_>>()
            })
        };
        if !bad.iter().any(|&v| v) {
            return Ok(Some((mapping, pos, tri, keep, labels)));
        }
        labels = cliff_lod_restore(
            py,
            tri.readonly(),
            bad.into_pyarray(py).readonly(),
            mapping.readonly(),
            labels.readonly(),
            baseline.readonly(),
        )?;
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;
    use numpy::ndarray::array;

    #[test]
    fn edge_groups_preserve_source_order_and_shared_face_counts() {
        let triangles = array![[0, 1, 2], [2, 1, 3]];
        let (edges, inverse, counts) = grouped_edges(triangles.view());
        assert_eq!(edges, array![[0, 1], [0, 2], [1, 2], [1, 3], [2, 3]]);
        assert_eq!(inverse, vec![0, 2, 2, 3, 1, 4]);
        assert_eq!(counts, vec![1, 1, 2, 1, 1]);
    }

    #[test]
    fn empty_topology_has_no_edges() {
        let triangles = Array2::zeros((0, 3));
        let (edges, inverse, counts) = grouped_edges(triangles.view());
        assert_eq!(edges.dim(), (0, 2));
        assert!(inverse.is_empty() && counts.is_empty());
    }
}
