//! Opaque cliff rims: filtered coverage contours and shared material blend coordinates.
use numpy::{ndarray::Array2, IntoPyArray, PyArray2, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::collections::HashMap;

// Smooth at the detection-cell scale, independent of render subdivisions.
const CONTOUR_SIGMA_M: f64 = 2.0;
const CONTOUR_RADIUS_M: f64 = 4.0;
const COVERAGE_THRESHOLD: f64 = 0.60;
const EXCLUDED_MARGIN: f64 = 0.01;
const MASK_HALF_TEXEL: f64 = 0.5 / 16.0;

fn blur_axis(input: &[f64], w: usize, h: usize, step: f64, vertical: bool) -> Vec<f64> {
    let radius = (CONTOUR_RADIUS_M / step).ceil().min(32.) as isize;
    let kernel: Vec<_> = (-radius..=radius)
        .map(|i| {
            let d = i as f64 * step / CONTOUR_SIGMA_M;
            (-0.5 * d * d).exp()
        })
        .collect();
    let total: f64 = kernel.iter().sum();
    let mut out = vec![0.; input.len()];
    for y in 0..h {
        for x in 0..w {
            out[y * w + x] = kernel
                .iter()
                .enumerate()
                .map(|(i, k)| {
                    let offset = i as isize - radius;
                    let xx = if vertical {
                        x
                    } else {
                        (x as isize + offset).clamp(0, w as isize - 1) as usize
                    };
                    let yy = if vertical {
                        (y as isize + offset).clamp(0, h as isize - 1) as usize
                    } else {
                        y
                    };
                    input[yy * w + xx] * k
                })
                .sum::<f64>()
                / total;
        }
    }
    out
}

fn prepare(
    surface: &[[f64; 3]],
    heights: &[f64],
    keep: &[bool],
    w: usize,
    h: usize,
    dx: f64,
    dy: f64,
    scale: f64,
    sink: f64,
) -> (Vec<[f64; 3]>, Vec<f64>, Vec<f64>) {
    let mut touch = vec![0u8; w * h];
    for y in 0..h - 1 {
        for x in 0..w - 1 {
            if keep[y * (w - 1) + x] {
                for v in [
                    y * w + x,
                    y * w + x + 1,
                    (y + 1) * w + x,
                    (y + 1) * w + x + 1,
                ] {
                    touch[v] += 1;
                }
            }
        }
    }
    // Halo edges are sampling boundaries, not evidence of absent cliffs.
    let coverage: Vec<_> = touch
        .iter()
        .enumerate()
        .map(|(v, t)| {
            let nx = if v % w == 0 || v % w == w - 1 { 1. } else { 2. };
            let ny = if v / w == 0 || v / w == h - 1 { 1. } else { 2. };
            *t as f64 / (nx * ny)
        })
        .collect();
    let blurred = blur_axis(
        &blur_axis(&coverage, w, h, dx * scale, false),
        w,
        h,
        dy,
        true,
    );
    let field: Vec<_> = blurred
        .iter()
        .enumerate()
        .map(|(v, b)| {
            let d = b - COVERAGE_THRESHOLD;
            // Never expand across the original exclusion mask, including concave holes.
            if coverage[v] < 1. {
                d.min(-EXCLUDED_MARGIN)
            } else {
                d
            }
        })
        .collect();
    let mut out = surface.to_vec();
    let mut weights = Vec::with_capacity(w * h);
    for v in 0..w * h {
        let t = (field[v] / (1. - COVERAGE_THRESHOLD)).clamp(0., 1.);
        let fade = t * t * (3. - 2. * t);
        weights.push(if touch[v] == 0 || fade > 1. - 1e-12 {
            0.
        } else {
            1. - fade
        });
        if touch[v] > 0 && fade < 1. - 1e-12 {
            let ground = [
                surface[0][0] + (v % w) as f64 * dx,
                surface[0][1] + (v / w) as f64 * dy,
                heights[v] - sink,
            ];
            for axis in 0..3 {
                out[v][axis] = ground[axis] + (surface[v][axis] - ground[axis]) * fade;
            }
        }
    }
    (out, weights, field)
}

/// Smooth coverage on the source halo before chart splitting or tile clipping.
#[pyfunction]
pub fn cliff_transition_surface<'py>(
    py: Python<'py>,
    surface: PyReadonlyArray2<'py, f64>,
    heights: PyReadonlyArray2<'py, f64>,
    keep: PyReadonlyArray2<'py, bool>,
    dx: f64,
    dy: f64,
    scale: f64,
    sink: f64,
) -> PyResult<(
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<f64>>,
)> {
    let (h, w) = (heights.shape()[0], heights.shape()[1]);
    if h < 2
        || w < 2
        || surface.shape() != [h * w, 3]
        || keep.shape() != [h - 1, w - 1]
        || ![dx, dy, scale].iter().all(|v| v.is_finite() && *v > 0.)
        || !sink.is_finite()
        || sink < 0.
    {
        return Err(PyValueError::new_err("invalid cliff transition grid"));
    }
    let p: Vec<_> = surface
        .as_array()
        .rows()
        .into_iter()
        .map(|r| [r[0], r[1], r[2]])
        .collect();
    let z: Vec<_> = heights.as_array().iter().copied().collect();
    let k: Vec<_> = keep.as_array().iter().copied().collect();
    if p.iter().flatten().chain(z.iter()).any(|v| !v.is_finite()) {
        return Err(PyValueError::new_err("nonfinite cliff transition grid"));
    }
    let (p, weights, field) = py.allow_threads(|| prepare(&p, &z, &k, w, h, dx, dy, scale, sink));
    Ok((
        Array2::from_shape_vec((h * w, 3), p.into_iter().flatten().collect())
            .unwrap()
            .into_pyarray(py),
        Array2::from_shape_vec((h * w, 1), weights)
            .unwrap()
            .into_pyarray(py),
        Array2::from_shape_vec((h * w, 1), field)
            .unwrap()
            .into_pyarray(py),
    ))
}

/// Cut only crossing triangles; share edge intersections and retain attribute charts.
/// New boundary vertices use the sampled ground, avoiding a raised rectangular lip.
#[pyfunction]
pub fn cliff_transition_clip<'py>(
    py: Python<'py>,
    points: PyReadonlyArray2<'py, f64>,
    triangles: PyReadonlyArray2<'py, i32>,
    attributes: PyReadonlyArray2<'py, f64>,
    field: PyReadonlyArray2<'py, f64>,
    ground: PyReadonlyArray2<'py, f64>,
) -> PyResult<(
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray2<i32>>,
    Bound<'py, PyArray2<f64>>,
)> {
    let n = points.shape()[0];
    let width = attributes.shape()[1];
    if points.shape() != [n, 3]
        || ground.shape() != [n, 3]
        || field.shape() != [n, 1]
        || attributes.shape()[0] != n
        || triangles.shape()[1] != 3
    {
        return Err(PyValueError::new_err("invalid cliff contour arrays"));
    }
    let p = points.as_array();
    let g = ground.as_array();
    let a = attributes.as_array();
    let f = field.as_array();
    let tris = triangles.as_array();
    if tris.iter().any(|v| *v < 0 || *v as usize >= n)
        || p.iter()
            .chain(g.iter())
            .chain(a.iter())
            .chain(f.iter())
            .any(|v| !v.is_finite())
    {
        return Err(PyValueError::new_err("invalid cliff contour values"));
    }
    let (out_p, out_t, out_a) = py.allow_threads(|| {
        let mut out_p = Vec::new();
        let mut out_a = Vec::new();
        let mut out_t = Vec::new();
        let mut vertices = HashMap::<(usize, usize), i32>::new();
        for tri in tris.rows() {
            let ids = [tri[0] as usize, tri[1] as usize, tri[2] as usize];
            let mut poly = Vec::with_capacity(4);
            for i in 0..3 {
                let (u, v) = (ids[i], ids[(i + 1) % 3]);
                if f[[u, 0]] >= 0. {
                    let id = *vertices.entry((u, u)).or_insert_with(|| {
                        let id = (out_p.len() / 3) as i32;
                        out_p.extend(p.row(u));
                        out_a.extend(a.row(u));
                        id
                    });
                    poly.push(id);
                }
                // Exact contour endpoints are already emitted as source vertices.
                // Do not duplicate them as edge intersections (zero-area faces).
                if (f[[u, 0]] >= 0.) != (f[[v, 0]] >= 0.) && f[[u, 0]] != 0. && f[[v, 0]] != 0. {
                    let (lo, hi) = (u.min(v), u.max(v));
                    let id = *vertices.entry((lo, hi)).or_insert_with(|| {
                        let t = f[[lo, 0]] / (f[[lo, 0]] - f[[hi, 0]]);
                        let id = (out_p.len() / 3) as i32;
                        for axis in 0..3 {
                            out_p.push(g[[lo, axis]] + (g[[hi, axis]] - g[[lo, axis]]) * t);
                        }
                        for c in 0..width {
                            out_a.push(a[[lo, c]] + (a[[hi, c]] - a[[lo, c]]) * t);
                        }
                        id
                    });
                    poly.push(id);
                }
            }
            for i in 1..poly.len().saturating_sub(1) {
                out_t.extend([poly[0], poly[i], poly[i + 1]]);
            }
        }
        (out_p, out_t, out_a)
    });
    let count = out_p.len() / 3;
    Ok((
        Array2::from_shape_vec((count, 3), out_p)
            .unwrap()
            .into_pyarray(py),
        Array2::from_shape_vec((out_t.len() / 3, 3), out_t)
            .unwrap()
            .into_pyarray(py),
        Array2::from_shape_vec((count, width), out_a)
            .unwrap()
            .into_pyarray(py),
    ))
}

fn linear(v: f64) -> f64 {
    if v <= 0.04045 {
        v / 12.92
    } else {
        ((v + 0.055) / 1.055).powf(2.4)
    }
}
fn srgb(v: f64) -> f64 {
    if v <= 0.0031308 {
        v * 12.92
    } else {
        1.055 * v.powf(1. / 2.4) - 0.055
    }
}

/// Mean-preserving tint compensation: the rock keeps its interior colour while
/// the opaque rim tends to the cached satellite colour, not a white overlay.
#[pyfunction]
pub fn cliff_transition_attributes<'py>(
    py: Python<'py>,
    colors: PyReadonlyArray2<'py, f64>,
    rgb: PyReadonlyArray2<'py, f64>,
    weights: PyReadonlyArray2<'py, f64>,
    reference: [f64; 3],
) -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>)> {
    let n = colors.shape()[0];
    if colors.shape() != [n, 4]
        || rgb.shape() != [n, 3]
        || weights.shape() != [n, 1]
        || reference.iter().any(|x| !x.is_finite() || *x <= 0.)
    {
        return Err(PyValueError::new_err("invalid cliff transition attributes"));
    }
    let (c, r, b) = (colors.as_array(), rgb.as_array(), weights.as_array());
    if c.iter()
        .chain(r.iter())
        .chain(b.iter())
        .any(|x| !x.is_finite())
    {
        return Err(PyValueError::new_err(
            "nonfinite cliff transition attributes",
        ));
    }
    let (out, uv) = py.allow_threads(|| {
        let mut out = Vec::with_capacity(n * 4);
        let mut uv = Vec::with_capacity(n * 2);
        for i in 0..n {
            let w = b[[i, 0]].clamp(0., 1.);
            let valid = (0..3).any(|ch| r[[i, ch]] > 8.);
            for ch in 0..3 {
                let old = linear((c[[i, ch]] / 255.).clamp(0., 1.));
                let mean = reference[ch];
                let target = if valid {
                    linear((r[[i, ch]] / 255.).clamp(0., 1.))
                } else {
                    old * mean
                };
                let tint = ((1. - w) * old * mean + w * target) / ((1. - w) * mean + w);
                out.push(if w == 0. {
                    c[[i, ch]]
                } else {
                    (srgb(tint.clamp(0., 1.)) * 255.).round()
                });
            }
            out.push(c[[i, 3]]);
            uv.extend([MASK_HALF_TEXEL + w * (1. - 2. * MASK_HALF_TEXEL), 0.5]);
        }
        (out, uv)
    });
    Ok((
        Array2::from_shape_vec((n, 4), out)
            .unwrap()
            .into_pyarray(py),
        Array2::from_shape_vec((n, 2), uv).unwrap().into_pyarray(py),
    ))
}

/// Keep subsequent heightmap relief inside the same geometric rim taper.
/// UV1 stores the shared mask coordinate; endpoints remain completely buried.
#[pyfunction]
pub fn cliff_transition_relief_weights<'py>(
    py: Python<'py>,
    weights: PyReadonlyArray2<'py, f64>,
    uv: PyReadonlyArray2<'py, f64>,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let n = weights.shape()[0];
    if weights.shape() != [n, 1] || uv.shape() != [n, 2] {
        return Err(PyValueError::new_err("invalid cliff relief taper arrays"));
    }
    let w = weights.as_array();
    let u = uv.as_array();
    let out: Vec<_> = py.allow_threads(|| {
        (0..n)
            .map(|i| {
                let blend =
                    ((u[[i, 0]] - MASK_HALF_TEXEL) / (1. - 2. * MASK_HALF_TEXEL)).clamp(0., 1.);
                w[[i, 0]] * (1. - blend)
            })
            .collect()
    });
    Ok(Array2::from_shape_vec((n, 1), out)
        .unwrap()
        .into_pyarray(py))
}
