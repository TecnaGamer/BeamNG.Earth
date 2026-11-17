//! Terrain-following relief. All fitting is done before clipping the shared halo to tile ownership.
use crate::cliffselect::sample;
use crate::dm::Dm;
use numpy::{
    ndarray::{Array1, Array2, Array3},
    IntoPyArray, PyArray1, PyArray2, PyArray3, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3,
    PyUntypedArrayMethods,
};
use pyo3::{exceptions::PyValueError, prelude::*};

const WIDTH: f64 = 91.44;
const CANYON_HEIGHT: f64 = 182.88;
const TERRAIN_SPAN: f64 = 2.0 * std::f64::consts::SQRT_2;
const EDGE_BLEND: f64 = 2.0;
const EDGE_SINK: f64 = 0.35;
const CLEARANCE: f64 = 0.20;
const OUTWARD_STEP: f64 = 0.25;
const BIOME_RELIEF: [f64; 4] = [0.7, 1.0, 0.55, 1.15];

/// Clip cliff triangles to tile ownership, interpolating every appearance channel.
/// Keep original vertices and inside-face order so the existing finish pass can
/// compact them. A running vertex count avoids quadratic boundary bookkeeping.
#[pyfunction]
pub fn cliff_mesh_clip<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    attributes: PyReadonlyArray2<'py, f64>,
    bounds: [f64; 4],
) -> PyResult<(
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<i64>>,
    Bound<'py, PyArray2<f64>>,
)> {
    let (p, t, a) = (positions.as_array(), triangles.as_array(), attributes.as_array());
    if p.ncols() != 3
        || t.ncols() != 3
        || a.nrows() != p.nrows()
        || bounds.iter().any(|v| !v.is_finite())
        || bounds[0] > bounds[2]
        || bounds[1] > bounds[3]
        || t.iter().any(|&i| i < 0 || i as usize >= p.nrows())
    {
        return Err(PyValueError::new_err(
            "cliff clip needs matching positions/attributes, valid triangles and ordered bounds",
        ));
    }
    let (points, faces, attrs) = py.detach(|| {
        let mut points: Vec<f64> = p.iter().copied().collect();
        let mut attrs: Vec<f64> = a.iter().copied().collect();
        let mut faces = Vec::new();
        let mut boundary = Vec::new();
        let planes = [
            (0, bounds[0], 1.0),
            (0, bounds[2], -1.0),
            (1, bounds[1], 1.0),
            (1, bounds[3], -1.0),
        ];
        for tri in t.rows() {
            let inside = planes
                .iter()
                .all(|&(axis, value, sign)| tri.iter().all(|&i| sign * (p[[i as usize, axis]] - value) >= 0.0));
            if inside {
                faces.extend(tri.iter().copied());
                continue;
            }
            if planes
                .iter()
                .any(|&(axis, value, sign)| tri.iter().all(|&i| sign * (p[[i as usize, axis]] - value) < 0.0))
            {
                continue;
            }
            let mut poly: Vec<Vec<f64>> = tri
                .iter()
                .map(|&i| {
                    p.row(i as usize)
                        .iter()
                        .chain(a.row(i as usize).iter())
                        .copied()
                        .collect()
                })
                .collect();
            for &(axis, value, sign) in &planes {
                let mut clipped = Vec::new();
                for j in 0..poly.len() {
                    let (v, w) = (&poly[j], &poly[(j + 1) % poly.len()]);
                    let (dv, dw) = (sign * (v[axis] - value), sign * (w[axis] - value));
                    if dv >= 0.0 {
                        clipped.push(v.clone());
                    }
                    if (dv >= 0.0) != (dw >= 0.0) {
                        let f = dv / (dv - dw);
                        clipped.push(v.iter().zip(w).map(|(&x, &y)| x + (y - x) * f).collect());
                    }
                }
                poly = clipped;
            }
            if poly.len() < 3 {
                continue;
            }
            let base = (points.len() / 3) as i64;
            for j in 1..poly.len() - 1 {
                boundary.extend_from_slice(&[base, base + j as i64, base + j as i64 + 1]);
            }
            for vertex in poly {
                points.extend_from_slice(&vertex[..3]);
                attrs.extend_from_slice(&vertex[3..]);
            }
        }
        faces.extend(boundary);
        let count = points.len() / 3;
        (
            Array2::from_shape_vec((count, 3), points).unwrap(),
            Array2::from_shape_vec((faces.len() / 3, 3), faces).unwrap(),
            Array2::from_shape_vec((count, a.ncols()), attrs).unwrap(),
        )
    });
    Ok((points.into_pyarray(py), faces.into_pyarray(py), attrs.into_pyarray(py)))
}

/// Finish a clipped material group: retain referenced vertices only, localize
/// positions, and renormalize interpolated normals without changing other data.
#[pyfunction]
pub fn cliff_mesh_finish<'py>(
    py: Python<'py>,
    positions: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i64>,
    attributes: PyReadonlyArray2<'py, f64>,
    origin: [f64; 3],
    base: usize,
) -> PyResult<(
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<i64>>,
    Bound<'py, PyArray2<f64>>,
)> {
    let (p, t, a) = (positions.as_array(), triangles.as_array(), attributes.as_array());
    if p.ncols() != 3
        || t.ncols() != 3
        || a.nrows() != p.nrows()
        || a.ncols() < 11
        || origin.iter().any(|v| !v.is_finite())
        || t.iter().any(|v| *v < 0 || *v as usize >= p.nrows())
        || base > i64::MAX as usize - p.nrows()
    {
        return Err(PyValueError::new_err(
            "aligned cliff vertices/attributes, valid triangles and finite origin required",
        ));
    }
    let (p, t, a) = py.detach(|| {
        let (used, mapped) = crate::cliffstream::compact_indices(t, base);
        let points = Array2::from_shape_fn((used.len(), 3), |(i, k)| p[[used[i], k]] - origin[k]);
        let mut attrs = Array2::from_shape_fn((used.len(), a.ncols()), |(i, k)| a[[used[i], k]]);
        if points.iter().chain(attrs.iter()).any(|v| !v.is_finite()) {
            return Err(PyValueError::new_err(
                "referenced cliff vertex attributes must be finite",
            ));
        }
        for mut row in attrs.rows_mut() {
            let length = (row[2] * row[2] + row[3] * row[3] + row[4] * row[4]).sqrt().max(1e-12);
            for k in 2..5 {
                row[k] /= length;
            }
        }
        Ok((points, mapped, attrs))
    })?;
    Ok((p.into_pyarray(py), t.into_pyarray(py), a.into_pyarray(py)))
}

/// Keep the undeformed material chart aligned with relief. Remove only whole
/// repeats before float32 storage, preserving phase across neighboring tiles.
#[pyfunction]
pub fn cliff_surface_uv<'py>(
    py: Python<'py>,
    heights: PyReadonlyArray2<'py, f64>,
    xs: PyReadonlyArray1<'py, f64>,
    ys: PyReadonlyArray1<'py, f64>,
    use_y: bool,
    canyon: bool,
    horizontal_scale: f64,
    width: f64,
    height_span: f64,
    offset: [f64; 2],
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (h, xs, ys) = (heights.as_array(), xs.as_array(), ys.as_array());
    if xs.len() < 2
        || ys.len() < 2
        || h.dim() != (ys.len(), xs.len())
        || xs.iter().chain(ys.iter()).chain(offset.iter()).any(|v| !v.is_finite())
        || [horizontal_scale, width, height_span]
            .iter()
            .any(|v| !v.is_finite() || *v <= 0.0)
        || xs[1] <= xs[0]
        || ys[1] <= ys[0]
    {
        return Err(PyValueError::new_err(
            "matching surface axes and positive finite chart scales required",
        ));
    }
    let z = h
        .as_slice()
        .ok_or_else(|| PyValueError::new_err("contiguous chart heights required"))?;
    let uv = py.detach(|| {
        let fitted = canyon.then(|| envelope(z, xs.len(), ys.len(), (xs[1] - xs[0]) * horizontal_scale, ys[1] - ys[0]));
        let z = fitted.as_deref().unwrap_or(z);
        let u0 = ((if use_y { ys[0] } else { xs[0] }) / width + offset[0]).floor();
        let v0 = (z[0] / height_span + offset[1]).floor();
        Array2::from_shape_fn((z.len(), 2), |(i, k)| {
            if k == 0 {
                (if use_y { ys[i / xs.len()] } else { xs[i % xs.len()] }) / width + offset[0] - u0
            } else {
                z[i] / height_span + offset[1] - v0
            }
        })
    });
    if !uv[[0, 1]].is_finite() {
        return Err(PyValueError::new_err("finite chart origin height required"));
    }
    Ok(uv.into_pyarray(py))
}

/// Refine only the owned tile and its dependency apron. Elevation and imagery
/// callbacks remain Python I/O; lattice construction and classification run here.
#[pyfunction]
#[pyo3(signature = (xs, ys, keep, biomes, bounds, subdivisions, grid, context, horizontal_scale, metres_per_degree, dry_min, elevation, colors=None, shore=None))]
pub fn cliff_refine_grid<'py>(
    py: Python<'py>,
    xs: PyReadonlyArray1<'py, f64>,
    ys: PyReadonlyArray1<'py, f64>,
    keep: PyReadonlyArray2<'py, bool>,
    biomes: PyReadonlyArray2<'py, i64>,
    bounds: [f64; 4],
    subdivisions: usize,
    grid: f64,
    context: f64,
    horizontal_scale: f64,
    metres_per_degree: f64,
    dry_min: f64,
    elevation: &Bound<'py, PyAny>,
    colors: Option<&Bound<'py, PyAny>>,
    shore: Option<PyReadonlyArray2<'py, bool>>,
) -> PyResult<(
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<bool>>,
    Bound<'py, PyArray2<i64>>,
    Option<Py<PyAny>>,
)> {
    const MAX_REFINED_VERTICES: usize = 4_000_000;
    let (xs, ys, keep, biomes) = (xs.as_array(), ys.as_array(), keep.as_array(), biomes.as_array());
    let shore = shore.as_ref().map(|a| a.as_array());
    if xs.len() < 2
        || ys.len() < 2
        || keep.dim() != (ys.len() - 1, xs.len() - 1)
        || biomes.dim() != keep.dim()
        || shore.as_ref().is_some_and(|s| s.dim() != keep.dim())
        || subdivisions == 0
        || [grid, horizontal_scale, metres_per_degree]
            .iter()
            .any(|v| !v.is_finite() || *v <= 0.0)
        || !context.is_finite()
        || context < 0.0
        || !dry_min.is_finite()
        || bounds.iter().any(|v| !v.is_finite())
        || bounds[0] > bounds[2]
        || bounds[1] > bounds[3]
        || xs.iter().chain(ys.iter()).any(|v| !v.is_finite())
        || (1..xs.len()).any(|i| xs[i] <= xs[i - 1])
        || (1..ys.len()).any(|i| ys[i] <= ys[i - 1])
    {
        return Err(PyValueError::new_err(
            "ordered finite refinement axes/bounds, matching cell grids and positive spacing required",
        ));
    }
    let apron = [
        (context / (grid * horizontal_scale)).ceil() as usize,
        (context / grid).ceil() as usize,
    ];
    let window = |axis: numpy::ndarray::ArrayView1<f64>, low: f64, high: f64, pad: usize| {
        let left = axis.iter().position(|v| *v >= low).unwrap_or(axis.len());
        let right = axis.iter().position(|v| *v >= high).unwrap_or(axis.len());
        (left.saturating_sub(pad), right.saturating_add(pad).min(axis.len() - 1))
    };
    let (i0, i1) = window(xs, bounds[0], bounds[2], apron[0]);
    let (j0, j1) = window(ys, bounds[1], bounds[3], apron[1]);
    if i0 >= i1 || j0 >= j1 {
        return Err(PyValueError::new_err("refinement bounds do not overlap coarse cells"));
    }
    let w = (i1 - i0).saturating_mul(subdivisions).saturating_add(1);
    let h = (j1 - j0).saturating_mul(subdivisions).saturating_add(1);
    if w.saturating_mul(h) > MAX_REFINED_VERTICES {
        return Err(PyValueError::new_err("refinement vertex budget exceeded"));
    }
    let (rx, ry, lat, lon) = py.detach(|| {
        let rx = Array1::from_iter((0..w).map(|i| xs[i0] + i as f64 * (grid / subdivisions as f64)));
        let ry = Array1::from_iter((0..h).map(|j| ys[j0] + j as f64 * (grid / subdivisions as f64)));
        let lat = Array2::from_shape_fn((h, w), |(j, _)| ry[j] / metres_per_degree);
        let lon = Array2::from_shape_fn((h, w), |(_, i)| {
            (rx[i] / metres_per_degree + 180.0).rem_euclid(360.0) - 180.0
        });
        (rx, ry, lat, lon)
    });
    let sampled = elevation.call1((lat.into_pyarray(py), lon.into_pyarray(py)))?;
    let heights = sampled.extract::<PyReadonlyArray2<f64>>()?;
    let heights = heights.as_array();
    if heights.dim() != (h, w) {
        return Err(PyValueError::new_err(
            "elevation provider must return the refined vertex grid",
        ));
    }
    let (rk, rb) = py.detach(|| {
        let rk = Array2::from_shape_fn((h - 1, w - 1), |(j, i)| {
            let source = (j0 + j / subdivisions, i0 + i / subdivisions);
            let z = [
                heights[[j, i]],
                heights[[j + 1, i]],
                heights[[j, i + 1]],
                heights[[j + 1, i + 1]],
            ];
            keep[source]
                && z.iter().all(|v| v.is_finite())
                && (!shore.as_ref().is_some_and(|s| s[source]) || z.iter().all(|v| *v > dry_min))
        });
        let rb = Array2::from_shape_fn((h - 1, w - 1), |(j, i)| {
            biomes[[j0 + j / subdivisions, i0 + i / subdivisions]]
        });
        (rk, rb)
    });
    let rgb = if let Some(colors) = colors {
        let (lat, lon) = py.detach(|| {
            (
                Array2::from_shape_fn((h - 1, w - 1), |(j, _)| ((ry[j] + ry[j + 1]) / 2.0) / metres_per_degree),
                Array2::from_shape_fn((h - 1, w - 1), |(_, i)| {
                    (((rx[i] + rx[i + 1]) / 2.0) / metres_per_degree + 180.0).rem_euclid(360.0) - 180.0
                }),
            )
        });
        Some(colors.call1((lat.into_pyarray(py), lon.into_pyarray(py)))?.unbind())
    } else {
        None
    };
    Ok((
        rx.into_pyarray(py),
        ry.into_pyarray(py),
        // Keep the provider's array: a second full height grid adds no information.
        sampled.cast::<PyArray2<f64>>()?.clone(),
        rk.into_pyarray(py),
        rb.into_pyarray(py),
        rgb,
    ))
}

fn envelope(z: &[f64], w: usize, h: usize, dx: f64, dy: f64) -> Vec<f64> {
    let mut out = z.to_vec();
    for (horizontal, spacing) in [(true, dx), (false, dy)] {
        let source = out.clone();
        let span = (TERRAIN_SPAN / spacing).ceil() as usize;
        for y in 0..h {
            for x in 0..w {
                let (at, len, stride) = if horizontal { (x, w, 1) } else { (y, h, w) };
                for left in 1..span.min(at + 1) {
                    for right in 1..=(span - left).min(len - at - 1) {
                        let chord = (right as f64 * source[y * w + x - left * stride]
                            + left as f64 * source[y * w + x + right * stride])
                            / (left + right) as f64;
                        // NumPy maximum propagates missing input instead of authorizing new surface.
                        out[y * w + x] = if chord.is_nan() || out[y * w + x].is_nan() {
                            f64::NAN
                        } else {
                            out[y * w + x].max(chord)
                        };
                    }
                }
            }
        }
    }
    out
}

/// Upper terrain chords shared by surface displacement, road-cut fitting and
/// texture charts. Missing samples propagate rather than inventing a surface.
#[pyfunction]
pub fn cliff_terrain_envelope<'py>(
    py: Python<'py>,
    heights: PyReadonlyArray2<'py, f64>,
    dx: f64,
    dy: f64,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    if [dx, dy].iter().any(|v| !v.is_finite() || *v <= 0.0) {
        return Err(PyValueError::new_err("finite positive terrain spacing required"));
    }
    let heights = heights.as_array();
    let (h, w) = heights.dim();
    let z: Vec<_> = heights.iter().copied().collect();
    let out = py.detach(|| envelope(&z, w, h, dx, dy));
    Ok(Array2::from_shape_vec((h, w), out).unwrap().into_pyarray(py))
}

fn gradient(z: &[f64], w: usize, h: usize, dx: f64, dy: f64) -> (Vec<f64>, Vec<f64>) {
    let mut gx = vec![0.0; w * h];
    let mut gy = gx.clone();
    for y in 0..h {
        for x in 0..w {
            let (l, r, b, t) = (
                x.saturating_sub(1),
                (x + 1).min(w - 1),
                y.saturating_sub(1),
                (y + 1).min(h - 1),
            );
            gx[y * w + x] = (z[y * w + r] - z[y * w + l]) / ((r - l) as f64 * dx);
            gy[y * w + x] = (z[t * w + x] - z[b * w + x]) / ((t - b) as f64 * dy);
        }
    }
    (gx, gy)
}

fn touches(cells: &[bool], w: usize, h: usize) -> Vec<f64> {
    let mut out = vec![0.0; w * h];
    for y in 0..h - 1 {
        for x in 0..w - 1 {
            if cells[y * (w - 1) + x] {
                for i in [y * w + x, y * w + x + 1, (y + 1) * w + x, (y + 1) * w + x + 1] {
                    out[i] += 1.0;
                }
            }
        }
    }
    out
}

fn fade(z: &[f64], count: &[f64], w: usize, h: usize, dx: f64, dy: f64) -> Vec<f64> {
    let mut distance: Vec<_> = count
        .iter()
        .map(|&v| if v == 4.0 { f64::INFINITY } else { 0.0 })
        .collect();
    let steep = 70.0_f64.to_radians().dtan();
    for _ in 0..(EDGE_BLEND / dx.min(dy)).ceil() as usize {
        let old = distance.clone();
        for y in 0..h {
            for x in 0..w {
                let i = y * w + x;
                // Unlike f64::min/max, missing samples must propagate through
                // the blend rather than silently supplying a finite distance.
                let mut visit = |j: usize, spacing: f64| {
                    let rise = (z[i] - z[j]).abs() / steep;
                    let candidate = old[j] + spacing.max(rise);
                    distance[i] = if rise.is_nan() || old[j].is_nan() || distance[i].is_nan() {
                        f64::NAN
                    } else {
                        distance[i].min(candidate)
                    };
                };
                if x > 0 {
                    visit(i - 1, dx);
                }
                if x + 1 < w {
                    visit(i + 1, dx);
                }
                if y > 0 {
                    visit(i - w, dy);
                }
                if y + 1 < h {
                    visit(i + w, dy);
                }
            }
        }
    }
    distance
        .iter()
        .map(|d| {
            let t = if d.is_nan() {
                f64::NAN
            } else {
                (d / EDGE_BLEND).min(1.0)
            };
            t * t * (3.0 - 2.0 * t)
        })
        .collect()
}

/// Lift the cut interior to the terrain envelope while keeping its perimeter
/// on the original ground, preventing exposed teeth at each lattice step.
#[pyfunction]
pub fn cliff_cut_chart<'py>(
    py: Python<'py>,
    heights: PyReadonlyArray2<'py, f64>,
    upper: PyReadonlyArray2<'py, f64>,
    cuts: PyReadonlyArray2<'py, bool>,
    dx: f64,
    dy: f64,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (h, w) = (heights.shape()[0], heights.shape()[1]);
    if h < 2
        || w < 2
        || upper.shape() != heights.shape()
        || cuts.shape() != [h - 1, w - 1]
        || [dx, dy].iter().any(|v| !v.is_finite() || *v <= 0.0)
    {
        return Err(PyValueError::new_err(
            "matching vertex heights, cell cuts and finite positive spacing required",
        ));
    }
    let z: Vec<_> = heights.as_array().iter().copied().collect();
    let upper: Vec<_> = upper.as_array().iter().copied().collect();
    let cuts: Vec<_> = cuts.as_array().iter().copied().collect();
    let out = py.detach(|| {
        let weights = fade(&upper, &touches(&cuts, w, h), w, h, dx, dy);
        (0..z.len()).map(|i| z[i] + (upper[i] - z[i]) * weights[i]).collect()
    });
    Ok(Array2::from_shape_vec((h, w), out).unwrap().into_pyarray(py))
}

fn periodic_height(grid: &[f64], w: usize, h: usize, u: f64, v: f64, amplitude: f64) -> f64 {
    if !u.is_finite() || !v.is_finite() {
        return f64::NAN;
    }
    let x = u.rem_euclid(1.0) * w as f64 - 0.5;
    let y = v.rem_euclid(1.0) * h as f64 - 0.5;
    let (ix, iy) = (x.floor() as i64, y.floor() as i64);
    let (fx, fy) = (x - x.floor(), y - y.floor());
    let get =
        |dx: i64, dy: i64| grid[(iy + dy).rem_euclid(h as i64) as usize * w + (ix + dx).rem_euclid(w as i64) as usize];
    amplitude
        * ((1.0 - fy) * ((1.0 - fx) * get(0, 0) + fx * get(1, 0)) + fy * ((1.0 - fx) * get(0, 1) + fx * get(1, 1)))
}

fn height(maps: &[f64], mw: usize, mh: usize, family: usize, u: f64, v: f64) -> f64 {
    let start = family * mw * mh;
    periodic_height(
        &maps[start..start + mw * mh],
        mw,
        mh,
        u,
        v,
        if family == 0 { 3.0 } else { 0.65 },
    )
}

/// Periodic pixel-centre sampling shared with terrain displacement. The optional
/// scan keeps its own vertical physical scale when blended with broad canyon beds.
#[pyfunction]
pub fn cliff_height_sample<'py>(
    py: Python<'py>,
    u: PyReadonlyArray1<'py, f64>,
    v: PyReadonlyArray1<'py, f64>,
    grid: PyReadonlyArray2<'py, f64>,
    amplitude: f64,
    scan: Option<(PyReadonlyArray2<'py, f64>, f64, f64, f64)>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (u, v, g) = (u.as_array(), v.as_array(), grid.as_array());
    if u.len() != v.len() || g.is_empty() || !amplitude.is_finite() {
        return Err(PyValueError::new_err(
            "aligned UVs, nonempty height map and finite amplitude required",
        ));
    }
    let grid = g
        .as_slice()
        .ok_or_else(|| PyValueError::new_err("contiguous height map required"))?;
    let scan = scan
        .as_ref()
        .map(|(a, amplitude, scale, weight)| (a.as_array(), *amplitude, *scale, *weight));
    let scan = match scan {
        Some((a, amp, scale, weight)) => {
            if a.is_empty() || ![amp, scale, weight].iter().all(|v| v.is_finite()) {
                return Err(PyValueError::new_err(
                    "nonempty scan and finite blend parameters required",
                ));
            }
            Some((
                a.to_slice()
                    .ok_or_else(|| PyValueError::new_err("contiguous scan required"))?,
                a.ncols(),
                a.nrows(),
                amp,
                scale,
                weight,
            ))
        }
        None => None,
    };
    let out = py.detach(|| {
        (0..u.len())
            .map(|i| {
                let base = periodic_height(grid, g.ncols(), g.nrows(), u[i], v[i], amplitude);
                if let Some((grid, w, h, amp, scale, weight)) = scan {
                    base + weight * periodic_height(grid, w, h, u[i], v[i] * scale, amp)
                } else {
                    base
                }
            })
            .collect::<Vec<_>>()
    });
    Ok(out.into_pyarray(py))
}

struct Input<'a> {
    z: &'a [f64],
    keep: &'a [bool],
    cuts: &'a [bool],
    biomes: &'a [i32],
    maps: &'a [f64],
    w: usize,
    h: usize,
    mw: usize,
    mh: usize,
    x0: f64,
    y0: f64,
    dx: f64,
    dy: f64,
    scale: f64,
}

fn surface(a: &Input<'_>, family: usize) -> (Vec<[f64; 3]>, Vec<f64>, Vec<f64>) {
    let (w, h) = (a.w, a.h);
    let count = touches(a.keep, w, h);
    let env = envelope(a.z, w, h, a.dx * a.scale, a.dy);
    let base = if family == 3 { &env } else { a.z };
    let (gx, gy) = gradient(base, w, h, a.dx * a.scale, a.dy);
    let any_cut = a.cuts.iter().any(|v| *v);
    let cut_count = touches(a.cuts, w, h);
    let arid: Vec<_> = a.cuts.iter().zip(a.biomes).map(|(&c, &b)| c && b == 1).collect();
    let arid_count = touches(&arid, w, h);
    let blend = fade(a.z, &count, w, h, a.dx * a.scale, a.dy);
    let mut out = Vec::with_capacity(w * h);
    for y in 0..h {
        for x in 0..w {
            let i = y * w + x;
            let (xx, yy) = (a.x0 + x as f64 * a.dx, a.y0 + y as f64 * a.dy);
            let cut_weight = cut_count[i] / count[i].max(1.0);
            if family == 3 && (!any_cut || cut_weight == 0.0) {
                out.push([xx, yy, base[i] + if count[i] == 4.0 { CLEARANCE } else { -EDGE_SINK }]);
                continue;
            }
            let vb = a.biomes[y.min(h - 2) * (w - 1) + x.min(w - 2)] as usize;
            let across = if gx[i].abs() >= gy[i].abs() { yy } else { xx };
            let v = base[i] / if family == 3 { CANYON_HEIGHT } else { WIDTH };
            let mut relief = height(a.maps, a.mw, a.mh, family, across / WIDTH, v);
            if family == 3 {
                relief += 0.6 * height(a.maps, a.mw, a.mh, 0, across / WIDTH, v * CANYON_HEIGHT / WIDTH);
            }
            relief *= BIOME_RELIEF[vb];
            if any_cut {
                let arid_weight = arid_count[i] / count[i].max(1.0);
                relief = relief * (1.0 - cut_weight)
                    + height(a.maps, a.mw, a.mh, 1, across / WIDTH, a.z[i] / WIDTH) * (cut_weight - arid_weight)
                    + height(a.maps, a.mw, a.mh, 2, across / WIDTH, a.z[i] / WIDTH) * arid_weight;
            }
            let offset = (0.35 + relief) * blend[i];
            let norm = (1.0 + gx[i] * gx[i] + gy[i] * gy[i]).sqrt();
            let direction = [-gx[i] / norm / a.scale, -gy[i] / norm, 1.0 / norm];
            let mut point = [
                xx + direction[0] * offset,
                yy + direction[1] * offset,
                base[i] + direction[2] * offset,
            ];
            let ground = |p: [f64; 3]| sample(&env, w, h, (p[0] - a.x0) / a.dx, (p[1] - a.y0) / a.dy) + CLEARANCE;
            if count[i] == 4.0 {
                for _ in 0..(2.0 * TERRAIN_SPAN / OUTWARD_STEP).ceil() as usize {
                    let g = ground(point);
                    if !(point[2] < g) {
                        break;
                    }
                    let trial = std::array::from_fn(|k| point[k] + direction[k] * OUTWARD_STEP);
                    let tg = ground(trial);
                    if trial[2] - tg > point[2] - g {
                        if trial[2] >= tg {
                            let (mut lo, mut hi) = (point, trial);
                            for _ in 0..5 {
                                let mid = std::array::from_fn(|k| (lo[k] + hi[k]) / 2.0);
                                if mid[2] >= ground(mid) {
                                    hi = mid;
                                } else {
                                    lo = mid;
                                }
                            }
                            point = hi;
                        } else {
                            point = trial;
                        }
                    } else {
                        point[2] = g;
                    }
                }
                let g = ground(point);
                if point[2] < g {
                    point[2] = g;
                }
            }
            if any_cut {
                let cutting = [xx, yy, a.z[i] + offset];
                point = std::array::from_fn(|k| point[k] * (1.0 - cut_weight) + cutting[k] * cut_weight);
            }
            if count[i] != 4.0 {
                point[2] -= EDGE_SINK;
            }
            out.push(point);
        }
    }
    (out, gx, gy)
}

/// Displace an (H, W) vertex grid using matching (H-1, W-1) selection grids and
/// four periodic relief maps. Returns row-major XYZ vertices (H*W, 3) and two
/// (H, W) slope grids. Coordinates and spacing use the caller's projected frame.
#[pyfunction]
pub fn cliff_surface<'py>(
    py: Python<'py>,
    heights: PyReadonlyArray2<'py, f64>,
    keep: PyReadonlyArray2<'py, bool>,
    cuts: PyReadonlyArray2<'py, bool>,
    biomes: PyReadonlyArray2<'py, i32>,
    families: PyReadonlyArray2<'py, i16>,
    maps: PyReadonlyArray3<'py, f64>,
    x0: f64,
    y0: f64,
    dx: f64,
    dy: f64,
    scale: f64,
) -> PyResult<(
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<f64>>,
)> {
    let (h, w) = (heights.shape()[0], heights.shape()[1]);
    if w < 2
        || h < 2
        || [keep.shape(), cuts.shape(), biomes.shape(), families.shape()]
            .iter()
            .any(|s| *s != [h - 1, w - 1])
        || maps.shape()[0] != 4
        || maps.shape()[1] < 2
        || maps.shape()[2] < 2
        || [dx, dy, scale].iter().any(|v| !v.is_finite() || *v <= 0.0)
        || !x0.is_finite()
        || !y0.is_finite()
    {
        return Err(PyValueError::new_err(
            "vertex grid, matching cell grids, four heightmaps and finite positive spacing required",
        ));
    }
    let z: Vec<_> = heights.as_array().iter().copied().collect();
    let k: Vec<_> = keep.as_array().iter().copied().collect();
    let c: Vec<_> = cuts.as_array().iter().copied().collect();
    let b: Vec<_> = biomes.as_array().iter().copied().collect();
    let f: Vec<_> = families.as_array().iter().copied().collect();
    let m: Vec<_> = maps.as_array().iter().copied().collect();
    if b.iter().any(|v| !(0..4).contains(v)) || f.iter().any(|v| !(0..4).contains(v)) {
        return Err(PyValueError::new_err("invalid cliff family or biome"));
    }
    let a = Input {
        z: &z,
        keep: &k,
        cuts: &c,
        biomes: &b,
        maps: &m,
        w,
        h,
        mw: maps.shape()[2],
        mh: maps.shape()[1],
        x0,
        y0,
        dx,
        dy,
        scale,
    };
    let (points, gx, gy) = py.detach(|| {
        let mut points = vec![[0.0; 3]; w * h];
        let mut gx = vec![0.0; w * h];
        let mut gy = gx.clone();
        let mut total = gx.clone();
        for family in 0..4 {
            let cells: Vec<_> = f.iter().map(|&v| v == family as i16).collect();
            if !cells.iter().any(|v| *v) {
                continue;
            }
            let weight = touches(&cells, w, h);
            let (p, px, py) = surface(&a, family);
            for i in 0..w * h {
                for axis in 0..3 {
                    points[i][axis] += p[i][axis] * weight[i];
                }
                gx[i] += px[i] * weight[i];
                gy[i] += py[i] * weight[i];
                total[i] += weight[i];
            }
        }
        for i in 0..w * h {
            for axis in 0..3 {
                points[i][axis] /= total[i];
            }
            gx[i] /= total[i];
            gy[i] /= total[i];
        }
        (points.into_iter().flatten().collect(), gx, gy)
    });
    Ok((
        Array2::from_shape_vec((w * h, 3), points).unwrap().into_pyarray(py),
        Array2::from_shape_vec((h, w), gx).unwrap().into_pyarray(py),
        Array2::from_shape_vec((h, w), gy).unwrap().into_pyarray(py),
    ))
}

/// Normals of the displaced parametric grid, including horizontal motion.
/// Scale physical coordinates before differencing, matching the mesh chart's
/// world-coordinate precision and one-sided derivatives along the outer halo.
#[pyfunction]
pub fn cliff_grid_normals<'py>(
    py: Python<'py>,
    surface: PyReadonlyArray3<'py, f64>,
    dx: f64,
    dy: f64,
    scale: f64,
) -> PyResult<Bound<'py, PyArray3<f64>>> {
    let p = surface.as_array();
    let (h, w, c) = p.dim();
    if h < 2 || w < 2 || c != 3 || [dx, dy, scale].iter().any(|v| !v.is_finite() || *v <= 0.0) {
        return Err(PyValueError::new_err(
            "at least 2x2 XYZ surface and positive spacing/scale required",
        ));
    }
    let out = py.detach(|| {
        let mut out = Array3::zeros((h, w, 3));
        for y in 0..h {
            for x in 0..w {
                let (l, r, b, t) = (
                    x.saturating_sub(1),
                    (x + 1).min(w - 1),
                    y.saturating_sub(1),
                    (y + 1).min(h - 1),
                );
                let (mut ax, mut ay) = ([0.0; 3], [0.0; 3]);
                for c in 0..3 {
                    let factor = if c == 0 { scale } else { 1.0 };
                    ax[c] = (p[[y, r, c]] * factor - p[[y, l, c]] * factor) / ((r - l) as f64 * (dx * scale));
                    ay[c] = (p[[t, x, c]] * factor - p[[b, x, c]] * factor) / ((t - b) as f64 * dy);
                }
                let normal = [
                    ax[1] * ay[2] - ax[2] * ay[1],
                    ax[2] * ay[0] - ax[0] * ay[2],
                    ax[0] * ay[1] - ax[1] * ay[0],
                ];
                let length = normal.iter().map(|v| v * v).sum::<f64>().sqrt();
                for c in 0..3 {
                    out[[y, x, c]] = normal[c] / length;
                }
            }
        }
        out
    });
    Ok(out.into_pyarray(py))
}

/// Two triangles per selected cell, grouped by material and dominant vertical
/// projection plane. Shared positions/normals survive chart duplication.
#[pyfunction]
pub fn cliff_mesh_layout<'py>(
    py: Python<'py>,
    keep: PyReadonlyArray2<'py, bool>,
    gx: PyReadonlyArray2<'py, f64>,
    gy: PyReadonlyArray2<'py, f64>,
    families: PyReadonlyArray2<'py, i16>,
) -> PyResult<Vec<(usize, bool, Bound<'py, PyArray2<i64>>)>> {
    let (keep, gx, gy, family) = (keep.as_array(), gx.as_array(), gy.as_array(), families.as_array());
    let (h, w) = keep.dim();
    if gx.dim() != (h + 1, w + 1)
        || gy.dim() != gx.dim()
        || family.dim() != keep.dim()
        || family.iter().any(|&v| !(0..4).contains(&v))
    {
        return Err(PyValueError::new_err(
            "cell masks/families and matching vertex gradients required",
        ));
    }
    let groups = py.detach(|| {
        let mut groups: Vec<Vec<i64>> = (0..8).map(|_| Vec::new()).collect();
        for y in 0..h {
            for x in 0..w {
                if !keep[[y, x]] {
                    continue;
                }
                let use_y = gx[[y, x]].abs() >= gy[[y, x]].abs();
                let group = family[[y, x]] as usize * 2 + use_y as usize;
                let a = (y * (w + 1) + x) as i64;
                let (b, c, d) = (a + 1, a + w as i64 + 2, a + w as i64 + 1);
                groups[group].extend_from_slice(&[a, b, c, a, c, d]);
            }
        }
        groups
            .into_iter()
            .enumerate()
            .filter(|(_, t)| !t.is_empty())
            .map(|(i, t)| (i / 2, i % 2 == 1, Array2::from_shape_vec((t.len() / 3, 3), t).unwrap()))
            .collect::<Vec<_>>()
    });
    Ok(groups.into_iter().map(|(f, a, t)| (f, a, t.into_pyarray(py))).collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn steep_boundary_fade_preserves_the_wall() {
        let z: Vec<_> = (0..17 * 17).map(|i| (i % 17) as f64 * 10.0).collect();
        let counts: Vec<_> = (0..17 * 17).map(|i| if i % 17 == 0 { 2.0 } else { 4.0 }).collect();
        let steep = fade(&z, &counts, 17, 17, 0.5, 0.5);
        let flat = fade(&vec![0.0; z.len()], &counts, 17, 17, 0.5, 0.5);
        for y in 0..17 {
            assert_eq!(steep[y * 17], 0.0);
            assert_eq!(steep[y * 17 + 1], 1.0);
            assert!(flat[y * 17 + 1] < 0.2);
        }
    }

    #[test]
    fn fade_propagates_missing_neighbours() {
        let mut z = vec![0.0; 25];
        z[12] = f64::NAN;
        let weights = fade(&z, &[4.0; 25], 5, 5, 1.0, 1.0);
        assert!(weights[12].is_nan());
        assert!(weights[10].is_nan());
        assert!(weights[2].is_nan());
        assert_eq!(weights[0], 1.0);
    }

    #[test]
    fn height_wraps_and_preserves_pixel_centres() {
        let maps = vec![0.0, 1.0, 0.5, 0.25].repeat(4);
        assert_eq!(height(&maps, 2, 2, 0, 0.25, 0.25), 0.0);
        assert_eq!(height(&maps, 2, 2, 0, 0.75, 0.25), 3.0);
        assert_eq!(height(&maps, 2, 2, 0, 0.75, 0.25), height(&maps, 2, 2, 0, -0.25, 1.25));
    }
    #[test]
    fn envelope_never_lowers_finite_terrain() {
        let z = vec![0.0, 4.0, 0.0, 4.0, 0.0, 4.0, 0.0, 4.0, 0.0];
        let e = envelope(&z, 3, 3, 1.0, 1.0);
        assert!(e.iter().zip(z).all(|(a, b)| *a >= b));
    }
}
