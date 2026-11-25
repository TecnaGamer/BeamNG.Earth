//! Exact orthographic receiver clipping for bounded cliff contact strips.
use crate::dm::Dm;
use numpy::{
    ndarray::{Array1, Array2},
    IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyUntypedArrayMethods,
};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::collections::{BTreeMap, BTreeSet, HashMap};
type Point = [f64; 4];
type Poly = Vec<Point>;
const EPS: f64 = 1e-8;
const RIM_HALF_TEXEL: f64 = 0.5 / 16.0;
const MAX_MASK_VERTICES: usize = 100_000;
const CONTACT_WELD_M: f64 = 0.03;
const MIN_CONTACT_RUN_M: f64 = 4.0;
const MAX_CONTACT_RUN_M: f64 = 48.0;
const MAX_CONTACT_RUNS: usize = 100_000;

/// Receiver patches repeat shared triangle corners. Map each exact position
/// once per patch, preserving corner order and the upstream world transform.
#[pyfunction]
pub fn cliff_contact_map<'py>(
    py: Python<'py>,
    points: PyReadonlyArray2<'py, f64>,
    source: (f64, f64),
    target: (f64, f64),
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let points = points.as_array();
    if points.ncols() != 3
        || points.iter().any(|v| !v.is_finite())
        || ![source.0, source.1, target.0, target.1]
            .iter()
            .all(|v| v.is_finite())
    {
        return Err(PyValueError::new_err(
            "finite Nx3 contact points and frame coordinates required",
        ));
    }
    let mapped = py.detach(|| {
        let src = crate::geotiles::Frame::new(source.0, source.1);
        let dst = crate::geotiles::Frame::new(target.0, target.1);
        let mut cache = HashMap::new();
        let mut out = Vec::with_capacity(points.len());
        for row in points.rows() {
            let p = [row[0], row[1], row[2]];
            // Bit keys also preserve signed zero; no geometric welding or
            // quantization may move a contact away from its receiver.
            let key = p.map(f64::to_bits);
            let mapped = cache.entry(key).or_insert_with(|| {
                if source == target {
                    p
                } else {
                    crate::geotiles::world_map_pt(&src, &dst, p)
                }
            });
            out.extend_from_slice(mapped);
        }
        Array2::from_shape_vec((points.nrows(), 3), out).unwrap()
    });
    Ok(mapped.into_pyarray(py))
}

/// Select nearby receivers within a shared tile budget. Preserve contour order
/// and skip oversized patches so smaller contacts can still use the remainder.
#[pyfunction]
#[pyo3(signature = (positions, triangles, colors, families, means, runs, offsets, margin, budget, byte_colors, normals=None, canyon_rim=None))]
pub fn cliff_contact_patches<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    colors: PyReadonlyArray2<'py, f64>,
    families: PyReadonlyArray1<'py, i64>,
    means: PyReadonlyArray2<'py, f64>,
    runs: PyReadonlyArray2<'py, f64>,
    offsets: PyReadonlyArray1<'py, i64>,
    margin: f64,
    budget: usize,
    byte_colors: bool,
    normals: Option<PyReadonlyArray2<'py, f64>>,
    canyon_rim: Option<(PyReadonlyArray2<'py, f64>, PyReadonlyArray2<'py, f64>)>,
) -> PyResult<
    Vec<(
        usize,
        Bound<'py, PyArray2<f64>>,
        Bound<'py, PyArray2<f32>>,
        Bound<'py, PyArray2<f32>>,
    )>,
> {
    let (p, tri, color, families, means, runs, offsets) = (
        positions.as_array(),
        triangles.as_array(),
        colors.as_array(),
        families.as_array(),
        means.as_array(),
        runs.as_array(),
        offsets.as_array(),
    );
    let normals = normals.as_ref().map(|n| n.as_array());
    let canyon_rim = canyon_rim
        .as_ref()
        .map(|(colors, uv)| (colors.as_array(), uv.as_array()));
    if p.ncols() != 3
        || tri.ncols() != 3
        || color.dim() != p.dim()
        || means.ncols() != 3
        || families.len() != tri.nrows()
        || runs.ncols() != 3
        || normals
            .as_ref()
            .is_some_and(|n| n.dim() != p.dim() || n.iter().any(|v| !v.is_finite()))
        || canyon_rim.as_ref().is_some_and(|(c, uv)| {
            c.dim() != p.dim()
                || uv.dim() != (p.nrows(), 2)
                || c.iter().chain(uv.iter()).any(|v| !v.is_finite())
        })
        || tri.iter().any(|i| *i < 0 || *i as usize >= p.nrows())
        || families
            .iter()
            .any(|i| *i < 0 || *i as usize >= means.nrows())
        || p.iter()
            .chain(color.iter())
            .chain(means.iter())
            .chain(runs.iter())
            .any(|v| !v.is_finite())
        || !margin.is_finite()
        || margin < 0.0
        || offsets.first() != Some(&0)
        || offsets.last() != Some(&(runs.nrows() as i64))
        || offsets
            .iter()
            .zip(offsets.iter().skip(1))
            .any(|(a, b)| a >= b)
    {
        return Err(PyValueError::new_err(
            "finite contact geometry, matching colors/normals, valid indices and run offsets required",
        ));
    }
    let patches = py.detach(|| {
        let divisor = if byte_colors || color.iter().any(|v| *v > 1.0) {
            255.0
        } else {
            1.0
        };
        // Compute face bounds once; only selected faces need normal/color assembly.
        let bounds: Vec<_> = tri
            .rows()
            .into_iter()
            .map(|t| {
                let low: [f64; 3] = std::array::from_fn(|k| {
                    (0..3)
                        .map(|v| p[[t[v] as usize, k]])
                        .fold(f64::INFINITY, f64::min)
                });
                let high: [f64; 3] = std::array::from_fn(|k| {
                    (0..3)
                        .map(|v| p[[t[v] as usize, k]])
                        .fold(f64::NEG_INFINITY, f64::max)
                });
                (low, high)
            })
            .collect();
        let mut remaining = budget;
        let mut patches = Vec::new();
        for run in 0..offsets.len() - 1 {
            let range = offsets[run] as usize..offsets[run + 1] as usize;
            let low: [f64; 3] = std::array::from_fn(|k| {
                range
                    .clone()
                    .map(|i| runs[[i, k]])
                    .fold(f64::INFINITY, f64::min)
                    - margin
            });
            let high: [f64; 3] = std::array::from_fn(|k| {
                range
                    .clone()
                    .map(|i| runs[[i, k]])
                    .fold(f64::NEG_INFINITY, f64::max)
                    + margin
            });
            let ids: Vec<_> = bounds
                .iter()
                .enumerate()
                .filter_map(|(i, (a, b))| {
                    (0..3)
                        .all(|k| b[k] >= low[k] && a[k] <= high[k])
                        .then_some(i)
                })
                .collect();
            if ids.is_empty() || ids.len() > remaining {
                continue;
            }
            remaining -= ids.len();
            let faces = Array2::from_shape_fn((ids.len(), 9), |(i, k)| {
                p[[tri[[ids[i], k / 3]] as usize, k % 3]]
            });
            let mut ns = Array2::<f32>::zeros((ids.len(), 9));
            let mut albedo = Array2::<f32>::zeros((ids.len(), 9));
            for (i, &id) in ids.iter().enumerate() {
                let a: [f64; 3] = std::array::from_fn(|k| faces[[i, k + 3]] - faces[[i, k]]);
                let b: [f64; 3] = std::array::from_fn(|k| faces[[i, k + 6]] - faces[[i, k]]);
                let n = [
                    a[1] * b[2] - a[2] * b[1],
                    a[2] * b[0] - a[0] * b[2],
                    a[0] * b[1] - a[1] * b[0],
                ];
                let length = n.iter().map(|v| v * v).sum::<f64>().sqrt().max(1e-12);
                let sign = if n[2] < 0.0 { -1.0 } else { 1.0 };
                for k in 0..9 {
                    let vertex = tri[[id, k / 3]] as usize;
                    ns[[i, k]] = normals
                        .as_ref()
                        .map_or(n[k % 3] / length * sign, |n| n[[vertex, k % 3]])
                        as f32;
                    let family = families[id] as usize;
                    let channel = k % 3;
                    let mean = means[[family, channel]];
                    // All cliff rims blend the rock with a vertex-coloured terrain layer.
                    // Match that visible surface, not the mineral tint from before blending.
                    albedo[[i, k]] = canyon_rim
                        .as_ref()
                        .map(|(c, uv)| {
                            let half_texel = RIM_HALF_TEXEL;
                            let blend = ((uv[[vertex, 0]] - half_texel) / (1.0 - 2.0 * half_texel))
                                .clamp(0.0, 1.0);
                            crate::cliffappearance::linear(c[[vertex, channel]] / 255.0)
                                * ((1.0 - blend) * mean + blend)
                        })
                        .unwrap_or_else(|| {
                            crate::cliffappearance::linear(color[[vertex, channel]] / divisor) * mean
                        }) as f32;
                }
            }
            patches.push((run, faces, ns, albedo));
        }
        patches
    });
    Ok(patches
        .into_iter()
        .map(|(run, f, n, a)| {
            (
                run,
                f.into_pyarray(py),
                n.into_pyarray(py),
                a.into_pyarray(py),
            )
        })
        .collect())
}

/// Ground contacts inherit a broad nearby rock color; rock contacts retain
/// their receiver's barycentric color. A sorted 2 m palette gives stable ties
/// without allocating a query-by-vertex distance matrix.
#[pyfunction]
pub fn cliff_strip_tint<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    owners: PyReadonlyArray1<'py, i64>,
    faces: PyReadonlyArray2<'py, f64>,
    albedo: PyReadonlyArray2<'py, f64>,
    axes: PyReadonlyArray2<'py, f64>,
    origin: [f64; 3],
    ground_count: usize,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    const COLOR_GRID_M: f64 = 2.0;
    let (positions, owners, faces, albedo, axes) = (
        positions.as_array(),
        owners.as_array(),
        faces.as_array(),
        albedo.as_array(),
        axes.as_array(),
    );
    if positions.ncols() != 3
        || owners.len() != positions.nrows()
        || faces.ncols() != 9
        || faces.nrows() == 0
        || albedo.dim() != faces.dim()
        || axes.dim() != (3, 3)
        || positions
            .iter()
            .chain(faces.iter())
            .chain(albedo.iter())
            .chain(axes.iter())
            .chain(origin.iter())
            .any(|v| !v.is_finite())
        || faces
            .iter()
            .any(|v| (v / COLOR_GRID_M).abs() >= i64::MAX as f64 / 2.0)
        || owners
            .iter()
            .any(|v| *v < 0 || (*v as usize).saturating_sub(ground_count) >= faces.nrows())
    {
        return Err(PyValueError::new_err(
            "finite strip geometry, matching rock colors and valid receiver IDs required",
        ));
    }
    let output = py
        .detach(|| -> Result<_, &'static str> {
            let mut groups: BTreeMap<[i64; 3], ([f64; 3], [f64; 3], usize)> = BTreeMap::new();
            for n in 0..faces.nrows() {
                for corner in 0..3 {
                    let key = std::array::from_fn(|k| {
                        (faces[[n, 3 * corner + k]] / COLOR_GRID_M).floor() as i64
                    });
                    let (center, color, count) =
                        groups.entry(key).or_insert(([0.0; 3], [0.0; 3], 0));
                    for k in 0..3 {
                        center[k] += faces[[n, 3 * corner + k]];
                        color[k] += albedo[[n, 3 * corner + k]];
                    }
                    *count += 1;
                }
            }
            let palette: Vec<_> = groups
                .into_values()
                .map(|(center, color, count)| {
                    (
                        center.map(|v| v / count as f64),
                        color.map(|v| v / count as f64),
                    )
                })
                .collect();
            let project = |p: [f64; 3]| -> [f64; 2] {
                std::array::from_fn(|axis| {
                    (0..3).map(|k| (p[k] - origin[k]) * axes[[axis, k]]).sum()
                })
            };
            let mut output = Array2::zeros((positions.nrows(), 3));
            for (n, point) in positions.rows().into_iter().enumerate() {
                let point = [point[0], point[1], point[2]];
                let color = if (owners[n] as usize) < ground_count {
                    let mut best = 0;
                    let mut distance = f64::INFINITY;
                    for (i, (center, _)) in palette.iter().enumerate() {
                        let d = (0..3).map(|k| (point[k] - center[k]).sq()).sum::<f64>();
                        if d < distance {
                            best = i;
                            distance = d;
                        }
                    }
                    palette[best].1
                } else {
                    let face = owners[n] as usize - ground_count;
                    let [a, b, c] = std::array::from_fn::<_, 3, _>(|corner| {
                        project(std::array::from_fn(|k| faces[[face, corner * 3 + k]]))
                    });
                    let p = project(point);
                    let (u, v, q) = (
                        [b[0] - a[0], b[1] - a[1]],
                        [c[0] - a[0], c[1] - a[1]],
                        [p[0] - a[0], p[1] - a[1]],
                    );
                    let det = u[0] * v[1] - u[1] * v[0];
                    if det == 0.0 || !det.is_finite() {
                        return Err("degenerate projected rock receiver");
                    }
                    let x = (q[0] * v[1] - q[1] * v[0]) / det;
                    let y = (u[0] * q[1] - u[1] * q[0]) / det;
                    std::array::from_fn(|k| {
                        (1.0 - x - y) * albedo[[face, k]]
                            + x * albedo[[face, 3 + k]]
                            + y * albedo[[face, 6 + k]]
                    })
                };
                for k in 0..3 {
                    output[[n, k]] = color[k];
                }
            }
            Ok(output)
        })
        .map_err(PyValueError::new_err)?;
    Ok(output.into_pyarray(py))
}

/// Finish clipped corners as a shared-vertex mesh. Attribute columns are
/// position(3), normal(3), UV0(2), UV1(2), erosion threshold(1); receiver IDs stay
/// integer. Quantized keys weld fragments without merging shading boundaries.
#[pyfunction]
#[pyo3(signature = (rows, faces, local, axes, origin, repeat, offset, lookup_width, normals=None))]
pub fn cliff_strip_mesh<'py>(
    py: Python<'py>,
    rows: PyReadonlyArray2<'py, f64>,
    faces: PyReadonlyArray2<'py, f64>,
    local: PyReadonlyArray2<'py, f64>,
    axes: PyReadonlyArray2<'py, f64>,
    origin: [f64; 3],
    repeat: f64,
    offset: f64,
    lookup_width: usize,
    normals: Option<PyReadonlyArray2<'py, f64>>,
) -> PyResult<(
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray2<i64>>,
)> {
    let (rows, faces, local, axes) = (
        rows.as_array(),
        faces.as_array(),
        local.as_array(),
        axes.as_array(),
    );
    let normals = normals.as_ref().map(|n| n.as_array());
    if rows.ncols() != 5
        || rows.nrows() % 3 != 0
        || faces.ncols() != 9
        || local.dim() != faces.dim()
        || axes.dim() != (3, 3)
        || normals.as_ref().is_some_and(|n| n.dim() != faces.dim())
        || !repeat.is_finite()
        || repeat <= 0.0
        || !offset.is_finite()
        || lookup_width == 0
        || rows
            .iter()
            .chain(faces.iter())
            .chain(local.iter())
            .chain(axes.iter())
            .chain(origin.iter())
            .any(|v| !v.is_finite())
        || normals
            .as_ref()
            .is_some_and(|n| n.iter().any(|v| !v.is_finite()))
        || rows
            .rows()
            .into_iter()
            .any(|r| r[4] < 0.0 || r[4].fract() != 0.0 || r[4] >= faces.nrows() as f64)
    {
        return Err(PyValueError::new_err(
            "finite strip triangles, valid receivers and positive texture dimensions required",
        ));
    }
    let (attributes, owners, triangles) = py
        .detach(|| -> Result<_, &'static str> {
            let normalize = |v: [f64; 3]| {
                let length = v.iter().map(|v| v * v).sum::<f64>().sqrt().max(1e-12);
                v.map(|v| v / length)
            };
            let mut corners = Vec::with_capacity(rows.nrows());
            let mut first = BTreeMap::new();
            let mut keys = Vec::with_capacity(rows.nrows());
            for (i, row) in rows.rows().into_iter().enumerate() {
                let owner = row[4] as usize;
                let normal = if let Some(normals) = normals.as_ref() {
                    let face = local.row(owner);
                    let (ux, uy, vx, vy) = (
                        face[3] - face[0],
                        face[4] - face[1],
                        face[6] - face[0],
                        face[7] - face[1],
                    );
                    let (qx, qy) = (row[0] - face[0], row[1] - face[1]);
                    let det = ux * vy - uy * vx;
                    if det == 0.0 {
                        return Err("degenerate strip shading receiver");
                    }
                    let (u, v) = ((qx * vy - qy * vx) / det, (ux * qy - uy * qx) / det);
                    normalize(std::array::from_fn(|k| {
                        (1.0 - u - v) * normals[[owner, k]]
                            + u * normals[[owner, 3 + k]]
                            + v * normals[[owner, 6 + k]]
                    }))
                } else {
                    let face = faces.row(owner);
                    let a: [f64; 3] = std::array::from_fn(|k| face[3 + k] - face[k]);
                    let b: [f64; 3] = std::array::from_fn(|k| face[6 + k] - face[k]);
                    let n = normalize([
                        a[1] * b[2] - a[2] * b[1],
                        a[2] * b[0] - a[0] * b[2],
                        a[0] * b[1] - a[1] * b[0],
                    ]);
                    let direction = if (0..3).map(|k| n[k] * axes[[2, k]]).sum::<f64>() < 0.0 {
                        -1.0
                    } else {
                        1.0
                    };
                    n.map(|v| v * direction)
                };
                let position: [f64; 3] = std::array::from_fn(|k| {
                    (0..3).map(|axis| row[axis] * axes[[axis, k]]).sum::<f64>()
                        + origin[k]
                        + axes[[2, k]] * offset
                });
                let mut attributes = [0.0; 11];
                attributes[..3].copy_from_slice(&position);
                attributes[3..6].copy_from_slice(&normal);
                attributes[6] = (0..3).map(|k| position[k] * axes[[0, k]]).sum::<f64>() / repeat;
                attributes[7] = -(0..3).map(|k| position[k] * axes[[1, k]]).sum::<f64>() / repeat;
                attributes[8] = (0.5 + row[3] * (lookup_width - 1) as f64) / lookup_width as f64;
                attributes[9] = 0.5;
                attributes[10] = row[3];
                if attributes.iter().any(|v| !v.is_finite())
                    || position.iter().any(|v| v.abs() >= i64::MAX as f64 / 1e7)
                {
                    return Err("strip attributes exceed finite weld range");
                }
                let mut key = [0i64; 8];
                for k in 0..3 {
                    key[k] = (position[k] * 1e7).round_ties_even() as i64;
                    key[3 + k] = (normal[k] * 1e6).round_ties_even() as i64;
                }
                key[6] = (row[3] * 1e7).round_ties_even() as i64;
                key[7] = owner as i64;
                first.entry(key).or_insert(i);
                keys.push(key);
                corners.push(attributes);
            }
            let mut ids = BTreeMap::new();
            let mut attributes = Vec::with_capacity(first.len() * 11);
            let mut owners = Vec::with_capacity(first.len());
            for (id, (key, i)) in first.into_iter().enumerate() {
                ids.insert(key, id as i64);
                attributes.extend(corners[i]);
                owners.push(key[7]);
            }
            let triangles = Array2::from_shape_vec(
                (rows.nrows() / 3, 3),
                keys.iter().map(|k| ids[k]).collect(),
            )
            .unwrap();
            Ok((
                Array2::from_shape_vec((owners.len(), 11), attributes).unwrap(),
                Array1::from_vec(owners),
                triangles,
            ))
        })
        .map_err(PyValueError::new_err)?;
    Ok((
        attributes.into_pyarray(py),
        owners.into_pyarray(py),
        triangles.into_pyarray(py),
    ))
}

fn terrain_height([a, b, c, d]: [f64; 4], x: f64, y: f64, even: bool) -> f64 {
    if even {
        if x > y {
            a + x * (b - a) + y * (d - b)
        } else {
            a + y * (c - a) + x * (d - c)
        }
    } else if x + y < 1.0 {
        a + x * (b - a) + y * (c - a)
    } else {
        d + (1.0 - y) * (b - d) + (1.0 - x) * (c - d)
    }
}

/// Query only unique corners of the active TerrainBlock lattice. Python owns
/// elevation access; corner selection and triangle interpolation run natively.
#[pyfunction]
pub fn cliff_terrain_sample<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>,
    spacing: f64,
    ground: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let xy = xy.as_array();
    if xy.ncols() != 2
        || !spacing.is_finite()
        || spacing <= 0.0
        || xy
            .iter()
            .any(|v| !v.is_finite() || (v / spacing).abs() >= i64::MAX as f64 / 2.0)
    {
        return Err(PyValueError::new_err(
            "finite Nx2 terrain queries and positive spacing required",
        ));
    }
    if xy.nrows() == 0 {
        return Ok(Array1::<f64>::zeros(0).into_pyarray(py));
    }
    let (queries, nodes, indices) = py.detach(|| {
        let mut nodes = BTreeSet::new();
        let mut queries = Vec::with_capacity(xy.nrows());
        for row in xy.rows() {
            let (x, y) = (row[0] / spacing, row[1] / spacing);
            let (i, j) = (x.floor() as i64, y.floor() as i64);
            let corners = [[i, j], [i + 1, j], [i, j + 1], [i + 1, j + 1]];
            nodes.extend(corners);
            queries.push((corners, x - i as f64, y - j as f64, (i ^ j) & 1 == 0));
        }
        let indices: BTreeMap<_, _> = nodes.iter().enumerate().map(|(i, p)| (*p, i)).collect();
        let nodes = Array2::from_shape_vec(
            (nodes.len(), 2),
            nodes
                .into_iter()
                .flat_map(|p| p.map(|c| c as f64 * spacing))
                .collect(),
        )
        .unwrap();
        (queries, nodes, indices)
    });
    let count = nodes.nrows();
    let values = ground.call1((nodes.into_pyarray(py),))?;
    let values = values.extract::<PyReadonlyArray1<f64>>()?;
    let values = values.as_array();
    if values.len() != count {
        return Err(PyValueError::new_err(
            "terrain provider must return one height per corner",
        ));
    }
    let out = py.detach(|| {
        queries
            .iter()
            .map(|(corners, x, y, even)| {
                terrain_height(corners.map(|p| values[indices[&p]]), *x, *y, *even)
            })
            .collect::<Vec<_>>()
    });
    Ok(Array1::from_vec(out).into_pyarray(py))
}

/// Build the receiver on the same global lattice and alternating diagonals as
/// TerrainBlock. Elevations are fetched once through the Python I/O callback.
#[pyfunction]
pub fn cliff_terrain_faces<'py>(
    py: Python<'py>,
    lo: [f64; 2],
    hi: [f64; 2],
    spacing: f64,
    ground: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    const MAX_RECEIVER_FACES: usize = 128_000;
    if !spacing.is_finite()
        || spacing <= 0.0
        || (0..2).any(|k| {
            !lo[k].is_finite()
                || !hi[k].is_finite()
                || hi[k] < lo[k]
                || (lo[k] / spacing).abs() >= i64::MAX as f64 / 2.0
                || (hi[k] / spacing).abs() >= i64::MAX as f64 / 2.0
        })
    {
        return Err(PyValueError::new_err(
            "ordered finite terrain bounds and positive spacing required",
        ));
    }
    let start = lo.map(|v| (v / spacing).floor() as i64);
    // Match the exclusive upper endpoint of arange(start, hi + spacing).
    let size: [usize; 2] = std::array::from_fn(|k| {
        ((hi[k] + spacing - start[k] as f64 * spacing) / spacing).ceil() as usize
    });
    let cells = size[0]
        .saturating_sub(1)
        .saturating_mul(size[1].saturating_sub(1));
    if cells > MAX_RECEIVER_FACES / 2 || size[0].saturating_mul(size[1]) > MAX_RECEIVER_FACES {
        return Err(PyValueError::new_err(
            "terrain receiver exceeds face budget",
        ));
    }
    if cells == 0 {
        return Ok(Array2::<f64>::zeros((0, 9)).into_pyarray(py));
    }
    let nodes = py.detach(|| {
        Array2::from_shape_fn((size[0] * size[1], 2), |(n, k)| {
            let offset = if k == 0 { n % size[0] } else { n / size[0] };
            (start[k] + offset as i64) as f64 * spacing
        })
    });
    let values = ground.call1((nodes.clone().into_pyarray(py),))?;
    let values = values.extract::<PyReadonlyArray1<f64>>()?;
    let values = values.as_array();
    if values.len() != nodes.nrows() {
        return Err(PyValueError::new_err(
            "terrain provider must return one height per corner",
        ));
    }
    let output = py.detach(|| {
        let mut output = Vec::with_capacity(cells * 18);
        // Preserve receiver IDs: even first triangles, even second, then odd.
        for group in 0..4 {
            for j in 0..size[1] - 1 {
                for i in 0..size[0] - 1 {
                    let even = ((start[0] + i as i64) ^ (start[1] + j as i64)) & 1 == 0;
                    if even != (group < 2) {
                        continue;
                    }
                    let a = j * size[0] + i;
                    let (b, c, d) = (a + 1, a + size[0], a + size[0] + 1);
                    let triangle = match group {
                        0 => [a, b, d],
                        1 => [a, d, c],
                        2 => [a, b, c],
                        _ => [b, d, c],
                    };
                    for n in triangle {
                        output.extend([nodes[[n, 0]], nodes[[n, 1]], values[n]]);
                    }
                }
            }
        }
        Array2::from_shape_vec((cells * 2, 9), output).unwrap()
    });
    Ok(output.into_pyarray(py))
}

/// Prefer the longest contact, then the contact farthest from those already
/// selected. Strict comparisons keep the first input on ties.
fn spread_runs(points: &[[f64; 3]], offsets: &[usize], limit: usize) -> Vec<usize> {
    let n = offsets.len() - 1;
    if n == 0 || limit == 0 {
        return Vec::new();
    }
    let mut centers = Vec::with_capacity(n);
    let mut lengths = Vec::with_capacity(n);
    for bounds in offsets.windows(2) {
        let run = &points[bounds[0]..bounds[1]];
        centers.push(std::array::from_fn::<_, 3, _>(|k| {
            run.iter().map(|p| p[k]).sum::<f64>() / run.len() as f64
        }));
        lengths.push(
            run.windows(2)
                .map(|p| {
                    (0..3)
                        .map(|k| (p[1][k] - p[0][k]).sq())
                        .sum::<f64>()
                        .sqrt()
                })
                .sum::<f64>(),
        );
    }
    let mut selected = 0;
    for i in 1..n {
        if lengths[i] > lengths[selected] {
            selected = i;
        }
    }
    let mut distances = vec![f64::INFINITY; n];
    let mut remaining = vec![true; n];
    let mut out = Vec::with_capacity(n.min(limit));
    for _ in 0..n.min(limit) {
        out.push(selected);
        remaining[selected] = false;
        let mut next = None;
        for i in 0..n {
            let distance = (0..3)
                .map(|k| (centers[i][k] - centers[selected][k]).sq())
                .sum::<f64>();
            distances[i] = distances[i].min(distance);
            if remaining[i] && next.is_none_or(|j| distances[i] > distances[j]) {
                next = Some(i);
            }
        }
        match next {
            Some(i) => selected = i,
            None => break,
        }
    }
    out
}

#[pyfunction]
pub fn cliff_contact_order(
    py: Python<'_>,
    positions: PyReadonlyArray2<'_, f64>,
    offsets: PyReadonlyArray1<'_, i64>,
    limit: usize,
) -> PyResult<Vec<usize>> {
    let (p, offsets) = (positions.as_array(), offsets.as_array());
    let offsets: Vec<_> = offsets.iter().copied().collect();
    if p.ncols() != 3
        || p.iter().any(|v| !v.is_finite())
        || offsets.first() != Some(&0)
        || offsets.last() != Some(&(p.nrows() as i64))
        || offsets.windows(2).any(|w| w[0] >= w[1])
    {
        return Err(PyValueError::new_err(
            "finite Nx3 contact positions and strictly increasing run offsets required",
        ));
    }
    let points: Vec<_> = p.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect();
    let offsets: Vec<_> = offsets.into_iter().map(|i| i as usize).collect();
    Ok(py.detach(|| spread_runs(&points, &offsets, limit)))
}

/// Trace terrain intersections through the displaced mesh, welding only nearby
/// endpoints. Split at branches and fixed physical lengths, never by vertex count.
#[pyfunction]
pub fn cliff_contact_runs<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    clearance: PyReadonlyArray1<'py, f64>,
) -> PyResult<Vec<Bound<'py, PyArray2<f64>>>> {
    let (p, tris, signed) = (
        positions.as_array(),
        triangles.as_array(),
        clearance.as_array(),
    );
    if p.ncols() != 3
        || tris.ncols() != 3
        || signed.len() != p.nrows()
        || tris.iter().any(|&v| v < 0 || v as usize >= p.nrows())
    {
        return Err(PyValueError::new_err(
            "Nx3 positions, Mx3 triangle indices and N clearances required",
        ));
    }
    let runs = py
        .detach(|| -> Result<Vec<Array2<f64>>, &'static str> {
            let key = |v: &[f64; 3]| v.map(|c| (c / CONTACT_WELD_M).round_ties_even() as i64);
            let mut segments = Vec::new();
            let mut vertices = BTreeMap::new();
            for tri in tris.rows() {
                let ids = [tri[0] as usize, tri[1] as usize, tri[2] as usize];
                let d = ids.map(|i| signed[i]);
                if d.iter().any(|v| !v.is_finite())
                    || !d.iter().any(|v| *v < 0.0)
                    || !d.iter().any(|v| *v >= 0.0)
                {
                    continue;
                }
                let mut ends = Vec::with_capacity(2);
                for (a, b) in [(0, 1), (1, 2), (2, 0)] {
                    if (d[a] < 0.0) != (d[b] < 0.0) {
                        let t = d[a] / (d[a] - d[b]);
                        ends.push(std::array::from_fn::<_, 3, _>(|k| {
                            p[[ids[a], k]] + t * (p[[ids[b], k]] - p[[ids[a], k]])
                        }));
                    }
                }
                if ends.len() != 2 || ends.iter().flatten().any(|v| !v.is_finite()) {
                    continue;
                }
                for v in &ends {
                    vertices.entry(key(v)).or_insert(*v);
                }
                segments.push([key(&ends[0]), key(&ends[1])]);
            }
            let nodes: Vec<_> = vertices.values().copied().collect();
            let indices: BTreeMap<_, _> =
                vertices.keys().enumerate().map(|(i, k)| (*k, i)).collect();
            let edges: Vec<_> = segments
                .iter()
                .map(|e| [indices[&e[0]], indices[&e[1]]])
                .collect();
            let mut adjacent = vec![Vec::new(); nodes.len()];
            let mut order = Vec::new();
            for (i, &[a, b]) in edges.iter().enumerate() {
                if a == b {
                    continue;
                }
                for v in [a, b] {
                    if adjacent[v].is_empty() {
                        order.push(v);
                    }
                    adjacent[v].push(i);
                }
            }
            // Stable ordering retains first encounter order within each group.
            order.sort_by_key(|&v| adjacent[v].len() == 2);
            let mut used = vec![false; edges.len()];
            let mut runs = Vec::new();
            for start in order {
                for &first in &adjacent[start] {
                    if used[first] {
                        continue;
                    }
                    let (mut v, mut edge) = (start, first);
                    let mut run = vec![nodes[v]];
                    while !used[edge] {
                        used[edge] = true;
                        let [a, b] = edges[edge];
                        v = if a == v { b } else { a };
                        run.push(nodes[v]);
                        if v == start || adjacent[v].len() != 2 {
                            break;
                        }
                        if let Some(&next) = adjacent[v].iter().find(|&&e| !used[e]) {
                            edge = next;
                        } else {
                            break;
                        }
                    }
                    let mut arc = vec![0.0];
                    for pair in run.windows(2) {
                        let distance = (0..3)
                            .map(|k| (pair[1][k] - pair[0][k]).sq())
                            .sum::<f64>()
                            .sqrt();
                        arc.push(arc.last().unwrap() + distance);
                    }
                    let length = *arc.last().unwrap();
                    if length < MIN_CONTACT_RUN_M {
                        continue;
                    }
                    let parts = (length / MAX_CONTACT_RUN_M).ceil();
                    if !parts.is_finite()
                        || parts > MAX_CONTACT_RUNS.saturating_sub(runs.len()) as f64
                    {
                        return Err("contact contour exceeds run budget");
                    }
                    for part in 0..parts as usize {
                        let lo = part as f64 * MAX_CONTACT_RUN_M;
                        let hi = (lo + MAX_CONTACT_RUN_M).min(length);
                        if hi - lo < MIN_CONTACT_RUN_M {
                            continue;
                        }
                        let mut at = vec![lo];
                        at.extend(arc.iter().copied().filter(|d| *d > lo && *d < hi));
                        at.push(hi);
                        let mut points = Vec::with_capacity(at.len() * 3);
                        for d in at {
                            let j = arc
                                .partition_point(|&x| x <= d)
                                .saturating_sub(1)
                                .min(run.len() - 2);
                            for k in 0..3 {
                                let value = if d == length {
                                    run.last().unwrap()[k]
                                } else {
                                    run[j][k]
                                        + (run[j + 1][k] - run[j][k]) / (arc[j + 1] - arc[j])
                                            * (d - arc[j])
                                };
                                points.push(value);
                            }
                        }
                        runs.push(Array2::from_shape_vec((points.len() / 3, 3), points).unwrap());
                    }
                }
            }
            Ok(runs)
        })
        .map_err(PyValueError::new_err)?;
    Ok(runs.into_iter().map(|r| r.into_pyarray(py)).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terrain_interpolation_follows_both_triangle_diagonals() {
        let corners = [0.0, 0.0, 0.0, 4.0];
        assert_eq!(terrain_height(corners, 0.75, 0.25, true), 1.0);
        assert_eq!(terrain_height(corners, 0.25, 0.75, true), 1.0);
        assert_eq!(terrain_height(corners, 0.75, 0.25, false), 0.0);
        assert_eq!(terrain_height(corners, 0.75, 0.75, false), 2.0);
        assert_eq!(terrain_height(corners, 1.0, 1.0, false), 4.0);
        assert!(terrain_height([f64::NAN; 4], 0.5, 0.5, true).is_nan());
    }

    #[test]
    fn contact_order_spreads_budget_and_preserves_ties() {
        let points = [
            [0.0, 0.0, 0.0],
            [0.0, 10.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 10.0, 0.0],
            [200.0, 0.0, 0.0],
            [200.0, 10.0, 0.0],
        ];
        assert_eq!(spread_runs(&points, &[0, 2, 4, 6], 2), vec![0, 2]);
        assert_eq!(spread_runs(&[[0.0; 3]; 3], &[0, 1, 2, 3], 9), vec![0, 1, 2]);
        assert!(spread_runs(&[], &[0], 128).is_empty());
    }

    #[test]
    fn mask_has_eroded_edges_and_dense_center() {
        let rows = mask_cells(&[[0.0, 0.0, 0.0], [0.0, 4.0, 0.0]], 1.0, 1.0, 1.0).unwrap();
        let mut dense = false;
        for p in rows.chunks_exact(4) {
            if p[0].abs() == 1.0 || p[1] == 0.0 || p[1] == 4.0 {
                assert_eq!(p[2], 1.0);
            }
            dense |= p[0] == 0.0 && p[1] == 2.0 && p[2] == 0.0;
        }
        assert!(dense);
    }

    #[test]
    fn mask_skips_degenerate_runs_and_bounds_allocation() {
        assert!(mask_cells(&[[0.0; 3]; 3], 1.0, 1.0, 1.0)
            .unwrap()
            .is_empty());
        assert!(mask_cells(&[[0.0; 3], [1e12, 0.0, 0.0]], 1.0, 0.1, 1.0).is_err());
    }
}

/// Sample a curved centerline in the projector's local frame. Every mask corner
/// carries XY, erosion threshold and expected depth for receiver clipping.
fn mask_cells(
    line: &[[f64; 3]],
    half_width: f64,
    spacing: f64,
    end_fade: f64,
) -> Result<Vec<f64>, &'static str> {
    let mut local = Vec::new();
    for (i, &p) in line.iter().enumerate() {
        if i == 0
            || ((p[0] - line[i - 1][0]).sq() + (p[1] - line[i - 1][1]).sq()).sqrt() > 1e-5
        {
            local.push(p);
        }
    }
    if local.len() < 2 {
        return Ok(Vec::new());
    }
    let mut arc = vec![0.0];
    for pair in local.windows(2) {
        arc.push(
            arc.last().unwrap()
                + ((pair[1][0] - pair[0][0]).sq() + (pair[1][1] - pair[0][1]).sq()).sqrt(),
        );
    }
    let length = *arc.last().unwrap();
    if length < 1.0 {
        return Ok(Vec::new());
    }
    let along_steps = (length / spacing).ceil().max(2.0);
    // An even cross section always samples the dense center of the mask.
    let across_steps = (2.0 * (half_width / spacing).ceil()).max(2.0);
    if !length.is_finite() || (along_steps + 1.0) * (across_steps + 1.0) > MAX_MASK_VERTICES as f64
    {
        return Err("strip mask exceeds vertex budget");
    }
    let (along_steps, across_steps) = (along_steps as usize, across_steps as usize);
    let mut centers = Vec::with_capacity(along_steps + 1);
    let mut along = Vec::with_capacity(along_steps + 1);
    let mut segment = 0;
    for i in 0..=along_steps {
        let distance = if i == along_steps {
            length
        } else {
            i as f64 * (length / along_steps as f64)
        };
        along.push(distance);
        while segment + 2 < arc.len() && arc[segment + 1] < distance {
            segment += 1;
        }
        let mut p = local[segment];
        for k in 0..3 {
            p[k] += (local[segment + 1][k] - p[k]) / (arc[segment + 1] - arc[segment])
                * (distance - arc[segment]);
        }
        if i == along_steps {
            p = *local.last().unwrap();
        }
        centers.push(p);
    }
    let mut points = Vec::with_capacity(centers.len() * (across_steps + 1));
    for (i, p) in centers.iter().enumerate() {
        let (l, r) = (i.saturating_sub(1), (i + 1).min(along_steps));
        let tx = (centers[r][0] - centers[l][0]) / (r - l) as f64;
        let ty = (centers[r][1] - centers[l][1]) / (r - l) as f64;
        let norm = (tx * tx + ty * ty).sqrt().max(1e-12);
        let cap = (along[i].min(length - along[i]) / end_fade.min(length / 2.0)).clamp(0.0, 1.0);
        let cap = cap * cap * (3.0 - 2.0 * cap);
        for j in 0..=across_steps {
            let across = if j == across_steps {
                half_width
            } else {
                -half_width + j as f64 * (2.0 * half_width / across_steps as f64)
            };
            let side = (across.abs() / half_width).clamp(0.0, 1.0).dpowf(1.35);
            points.push([
                p[0] - ty / norm * across,
                p[1] + tx / norm * across,
                1.0 - (1.0 - side) * cap,
                p[2],
            ]);
        }
    }
    let mut out = Vec::new();
    let stride = across_steps + 1;
    for y in 0..along_steps {
        for x in 0..across_steps {
            let a = y * stride + x;
            for ids in [[a, a + 1, a + stride], [a + 1, a + stride + 1, a + stride]] {
                let triangle = ids.map(|i| points[i]);
                if area(&triangle) > EPS {
                    for p in triangle {
                        out.extend_from_slice(&p);
                    }
                }
            }
        }
    }
    Ok(out)
}

#[pyfunction]
pub fn cliff_strip_domains<'py>(
    py: Python<'py>,
    centerline: PyReadonlyArray2<'py, f64>,
    half_width: f64,
    spacing: f64,
    end_fade: f64,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    if centerline.shape()[1] != 3
        || [half_width, spacing, end_fade]
            .iter()
            .any(|v| !v.is_finite() || *v <= 0.0)
    {
        return Err(PyValueError::new_err(
            "Nx3 centerline and finite positive strip dimensions required",
        ));
    }
    let view = centerline.as_array();
    if view.iter().any(|v| !v.is_finite()) {
        return Err(PyValueError::new_err("nonfinite strip centerline"));
    }
    let line: Vec<_> = view
        .rows()
        .into_iter()
        .map(|r| [r[0], r[1], r[2]])
        .collect();
    let out = py
        .detach(|| mask_cells(&line, half_width, spacing, end_fade))
        .map_err(PyValueError::new_err)?;
    Ok(Array2::from_shape_vec((out.len() / 12, 12), out)
        .unwrap()
        .into_pyarray(py))
}

fn area(p: &[Point]) -> f64 {
    if p.len() < 3 {
        return 0.;
    }
    (0..p.len())
        .map(|i| {
            let j = (i + 1) % p.len();
            p[i][0] * p[j][1] - p[i][1] * p[j][0]
        })
        .sum::<f64>()
        .abs()
        * 0.5
}
fn clip(p: &[Point], h: [f64; 5]) -> Poly {
    let mut out = Vec::new();
    if p.is_empty() {
        return out;
    }
    let distance = |v: &Point| h[4] + (0..4).map(|i| v[i] * h[i]).sum::<f64>();
    let mut a = p[p.len() - 1];
    let mut da = distance(&a);
    for &b in p {
        let db = distance(&b);
        if (da >= -EPS) != (db >= -EPS) {
            let t = da / (da - db);
            let mut v = [0.; 4];
            for k in 0..4 {
                v[k] = a[k] + t * (b[k] - a[k]);
            }
            out.push(v);
        }
        if db >= -EPS {
            out.push(b);
        }
        a = b;
        da = db;
    }
    out
}
fn edges(p: &[Point]) -> Vec<[f64; 5]> {
    let signed = (0..p.len())
        .map(|i| {
            let j = (i + 1) % p.len();
            p[i][0] * p[j][1] - p[i][1] * p[j][0]
        })
        .sum::<f64>();
    let s = if signed >= 0. { 1. } else { -1. };
    (0..p.len())
        .map(|i| {
            let a = p[i];
            let b = p[(i + 1) % p.len()];
            let dx = b[0] - a[0];
            let dy = b[1] - a[1];
            (dx.abs() + dy.abs() > EPS).then_some([
                -dy * s,
                dx * s,
                0.,
                0.,
                (dy * a[0] - dx * a[1]) * s,
            ])
        })
        .flatten()
        .collect()
}
fn intersect(mut p: Poly, b: &[Point]) -> Poly {
    for h in edges(b) {
        p = clip(&p, h);
        if p.len() < 3 {
            break;
        }
    }
    p
}
fn subtract(p: Poly, b: &[Point]) -> Vec<Poly> {
    let mut r = p;
    let mut out = Vec::new();
    for h in edges(b) {
        let q = clip(&r, h.map(|v| -v));
        if area(&q) > EPS {
            out.push(q);
        }
        r = clip(&r, h);
        if area(&r) <= EPS {
            break;
        }
    }
    out
}
fn bounds(p: &[Point]) -> [f64; 4] {
    let mut b = [
        f64::INFINITY,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::NEG_INFINITY,
    ];
    for v in p {
        for k in 0..2 {
            b[k] = b[k].min(v[k]);
            b[k + 2] = b[k + 2].max(v[k]);
        }
    }
    b
}
fn overlap(a: [f64; 4], b: [f64; 4]) -> bool {
    a[2] > b[0] + EPS && b[2] > a[0] + EPS && a[3] > b[1] + EPS && b[3] > a[1] + EPS
}
/// Return independent triangle corners [x,y,depth,threshold,receiver]. All clipping runs outside the GIL.
#[pyfunction]
pub fn cliff_strip_project(
    py: Python<'_>,
    receivers: PyReadonlyArray2<'_, f64>,
    domains: PyReadonlyArray2<'_, f64>,
    limit: usize,
) -> PyResult<Vec<[f64; 5]>> {
    if receivers.shape()[1] != 9 || domains.shape()[1] != 12 || limit == 0 {
        return Err(PyValueError::new_err(
            "expected Nx9 receivers, Nx12 domains and positive limit",
        ));
    }
    let r = receivers.as_array();
    let d = domains.as_array();
    py.detach(move || {
        let mut shapes: Vec<Poly> = Vec::new();
        let mut planes = Vec::new();
        let mut bins: HashMap<(i32, i32), Vec<usize>> = HashMap::new();
        for (id, row) in r.rows().into_iter().enumerate() {
            if row.iter().any(|v| !v.is_finite()) {
                return Err(PyValueError::new_err("nonfinite receiver"));
            }
            let p: Poly = (0..3)
                .map(|k| [row[k * 3], row[k * 3 + 1], row[k * 3 + 2], 0.])
                .collect();
            let a = [p[1][0] - p[0][0], p[1][1] - p[0][1], p[1][2] - p[0][2]];
            let b = [p[2][0] - p[0][0], p[2][1] - p[0][1], p[2][2] - p[0][2]];
            let det = a[0] * b[1] - a[1] * b[0];
            let mut h = [0.; 3];
            if det.abs() > 1e-7 {
                h[0] = (a[2] * b[1] - a[1] * b[2]) / det;
                h[1] = (a[0] * b[2] - a[2] * b[0]) / det;
                h[2] = p[0][2] - h[0] * p[0][0] - h[1] * p[0][1];
                let bb = bounds(&p);
                let x0 = (bb[0] / 2.).floor() as i32;
                let x1 = (bb[2] / 2.).floor() as i32;
                let y0 = (bb[1] / 2.).floor() as i32;
                let y1 = (bb[3] / 2.).floor() as i32;
                if (x1 as i64 - x0 as i64 + 1) * (y1 as i64 - y0 as i64 + 1) > 100000 {
                    return Err(PyValueError::new_err("receiver exceeds projection bounds"));
                }
                for x in x0..=x1 {
                    for y in y0..=y1 {
                        bins.entry((x, y)).or_default().push(id);
                    }
                }
            }
            shapes.push(p);
            planes.push(h);
        }
        let mut out = Vec::new();
        for row in d.rows() {
            if row.iter().any(|v| !v.is_finite()) {
                return Err(PyValueError::new_err("nonfinite domain"));
            }
            let domain: Poly = (0..3)
                .map(|k| [row[k * 4], row[k * 4 + 1], row[k * 4 + 2], row[k * 4 + 3]])
                .collect();
            let bb = bounds(&domain);
            let mut ids = BTreeSet::new();
            let x0 = (bb[0] / 2.).floor() as i32;
            let x1 = (bb[2] / 2.).floor() as i32;
            let y0 = (bb[1] / 2.).floor() as i32;
            let y1 = (bb[3] / 2.).floor() as i32;
            if (x1 as i64 - x0 as i64 + 1) * (y1 as i64 - y0 as i64 + 1) > 100000 {
                return Err(PyValueError::new_err("domain exceeds projection bounds"));
            }
            for x in x0..=x1 {
                for y in y0..=y1 {
                    if let Some(v) = bins.get(&(x, y)) {
                        ids.extend(v.iter().copied());
                    }
                }
            }
            let mut regions = Vec::new();
            for id in ids {
                let h = planes[id];
                let p = intersect(domain.clone(), &shapes[id]);
                let p = clip(&p, [h[0], h[1], 0., -1., h[2] + 12.]);
                let p = clip(&p, [-h[0], -h[1], 0., 1., 12. - h[2]]);
                if area(&p) > EPS {
                    let b = bounds(&p);
                    regions.push((id, p, b));
                }
            }
            for (id, p, bb) in &regions {
                let mut fragments = vec![p.clone()];
                for (other, b, bounds_other) in &regions {
                    if other == id || !overlap(*bb, *bounds_other) {
                        continue;
                    }
                    let delta = [
                        planes[*other][0] - planes[*id][0],
                        planes[*other][1] - planes[*id][1],
                        planes[*other][2] - planes[*id][2],
                    ];
                    let occluder = if delta.iter().all(|v| v.abs() < EPS) {
                        if other > id {
                            continue;
                        }
                        b.clone()
                    } else {
                        clip(b, [delta[0], delta[1], 0., 0., delta[2]])
                    };
                    if area(&occluder) <= EPS {
                        continue;
                    }
                    let mut visible = Vec::new();
                    for fragment in fragments {
                        if area(&intersect(fragment.clone(), &occluder)) <= EPS {
                            visible.push(fragment);
                        } else {
                            visible.extend(subtract(fragment, &occluder));
                        }
                    }
                    fragments = visible;
                    if fragments.is_empty() {
                        break;
                    }
                }
                let h = planes[*id];
                for p in fragments {
                    for k in 1..p.len() - 1 {
                        let mut corners = [p[0], p[k], p[k + 1]];
                        let signed = (corners[1][0] - corners[0][0])
                            * (corners[2][1] - corners[0][1])
                            - (corners[1][1] - corners[0][1]) * (corners[2][0] - corners[0][0]);
                        if signed.abs() <= EPS {
                            continue;
                        }
                        if signed < 0. {
                            corners.swap(1, 2);
                        }
                        if out.len() / 3 >= limit {
                            return Err(PyValueError::new_err("strip triangle budget exceeded"));
                        }
                        for v in corners {
                            out.push([
                                v[0],
                                v[1],
                                v[0] * h[0] + v[1] * h[1] + h[2],
                                v[2].clamp(0., 1.),
                                *id as f64,
                            ]);
                        }
                    }
                }
            }
        }
        Ok(out)
    })
}
