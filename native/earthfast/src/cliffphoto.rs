//! Shared geographic mineral-colour field. Python reads cached imagery for the
//! returned probes; native code rejects canopy, water and shadows and interpolates
//! only reliable nodes. No image downloads or image cache lives in this module.
use crate::dm::Dm;
use numpy::{
    ndarray::{Array2, Array3},
    IntoPyArray, PyArray2, PyArray3, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3,
};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::collections::BTreeMap;

const METRES_PER_DEGREE: f64 = 111320.0;
const PROBE_SIDE: usize = 9;
const PROBES: usize = PROBE_SIDE * PROBE_SIDE;
const LUMA: [f64; 3] = [0.2126, 0.7152, 0.0722];
// Canopy can be pale gray-green or olive (red slightly above green). Require
// green to fall toward blue along a warm mineral hue, at both sampling scales.
// Dry tan remains valid; the regional fallback must not reintroduce tree tint.
const MINERAL_GREEN_FRACTION: f64 = 0.20;

/// Plan shared lattice samples for flat (N,) latitude/longitude arrays. Returns
/// coordinates (nodes, 81, 2) in latitude/longitude order, node indices (N, 4),
/// and bilinear weights (N, 4). Sparse node ownership avoids allocating a huge
/// rectangle when callers pass disjoint locations.
#[pyfunction]
pub fn cliff_photo_queries<'py>(
    py: Python<'py>,
    lat: PyReadonlyArray1<'py, f64>,
    lon: PyReadonlyArray1<'py, f64>,
    grid_m: f64,
    radius_m: f64,
) -> PyResult<(
    Bound<'py, PyArray3<f64>>,
    Bound<'py, PyArray2<i64>>,
    Bound<'py, PyArray2<f64>>,
)> {
    let (lat, lon) = (lat.as_array(), lon.as_array());
    if lat.len() != lon.len()
        || !grid_m.is_finite()
        || grid_m <= 0.0
        || !radius_m.is_finite()
        || radius_m < 0.0
        || grid_m < 1e-6
        || lat.iter().any(|v| !v.is_finite() || v.abs() > 90.0)
        || lon.iter().any(|v| !v.is_finite() || v.abs() > 180.0)
    {
        return Err(PyValueError::new_err(
            "finite geographic coordinates, positive grid and nonnegative radius required",
        ));
    }
    let (coords, indices, weights) = py.detach(|| {
        let mut nodes = BTreeMap::new();
        let mut indices = Vec::with_capacity(lat.len() * 4);
        let mut weights = Vec::with_capacity(lat.len() * 4);
        for (&la, &lo) in lat.iter().zip(lon.iter()) {
            let (x, y) = (lo * METRES_PER_DEGREE / grid_m, la * METRES_PER_DEGREE / grid_m);
            let (ix, iy) = (x.floor() as i64, y.floor() as i64);
            let (fx, fy) = (x - x.floor(), y - y.floor());
            for (di, dj, weight) in [
                (0, 0, (1.0 - fx) * (1.0 - fy)),
                (1, 0, fx * (1.0 - fy)),
                (0, 1, (1.0 - fx) * fy),
                (1, 1, fx * fy),
            ] {
                let next = nodes.len();
                indices.push(*nodes.entry((iy + dj, ix + di)).or_insert(next) as i64);
                weights.push(weight);
            }
        }
        let mut coords = vec![0.0; nodes.len() * PROBES * 2];
        for ((iy, ix), id) in nodes {
            for j in 0..PROBE_SIDE {
                let oy = -radius_m + j as f64 * (2.0 * radius_m / (PROBE_SIDE - 1) as f64);
                let la = (iy as f64 * grid_m + oy) / METRES_PER_DEGREE;
                for i in 0..PROBE_SIDE {
                    let ox = -radius_m + i as f64 * (2.0 * radius_m / (PROBE_SIDE - 1) as f64);
                    let lo = (ix as f64 * grid_m + ox / la.to_radians().dcos().max(0.1)) / METRES_PER_DEGREE;
                    let at = (id * PROBES + j * PROBE_SIDE + i) * 2;
                    coords[at] = la;
                    coords[at + 1] = lo;
                }
            }
        }
        (coords, indices, weights)
    });
    Ok((
        Array3::from_shape_vec((coords.len() / (PROBES * 2), PROBES, 2), coords)
            .unwrap()
            .into_pyarray(py),
        Array2::from_shape_vec((lat.len(), 4), indices)
            .unwrap()
            .into_pyarray(py),
        Array2::from_shape_vec((lat.len(), 4), weights)
            .unwrap()
            .into_pyarray(py),
    ))
}

fn mineral(samples: impl Iterator<Item = [f64; 3]>, regional: bool) -> Option<([f64; 3], f64)> {
    let mut valid: Vec<_> = samples
        .filter_map(|rgb| {
            let [r, g, b] = rgb;
            let light = r * LUMA[0] + g * LUMA[1] + b * LUMA[2];
            (light > 55.0 && light < 235.0 && g <= r - MINERAL_GREEN_FRACTION * (r - b).max(0.0) && b < r * 1.08 && (regional || g - (r + b) * 0.5 < light * 0.045))
                .then_some((light, rgb))
        })
        .collect();
    if valid.is_empty() {
        return None;
    }
    valid.sort_by(|a, b| a.0.total_cmp(&b.0));
    let threshold = valid[(valid.len() as f64 * 0.6) as usize].0;
    let bright: Vec<_> = valid.iter().filter(|v| v.0 >= threshold).map(|v| v.1).collect();
    if bright.len() < 3 {
        return None;
    }
    let color = std::array::from_fn(|k| {
        let mut channel: Vec<_> = bright.iter().map(|v| v[k]).collect();
        channel.sort_by(f64::total_cmp);
        let mid = channel.len() / 2;
        if channel.len() % 2 == 0 {
            (channel[mid - 1] + channel[mid]) * 0.5
        } else {
            channel[mid]
        }
    });
    // A handful of acceptable pixels must not replace a reliable regional hue.
    // Regional samples describe the broad ground hue, including dry tan terrain.
    // Once supported by several bright samples, do not dilute it with neutral gray.
    let support = if regional { 1.0 } else { (bright.len() as f64 / 12.0).min(1.0) };
    Some((color, support * support * (3.0 - 2.0 * support)))
}

fn resolve(sample: Option<([f64; 3], f64)>, fallback: Option<[f64; 3]>) -> Option<[f64; 3]> {
    match (sample, fallback) {
        (Some((color, confidence)), Some(base)) =>
            Some(std::array::from_fn(|k| base[k] + confidence * (color[k] - base[k]))),
        (Some((color, _)), None) => Some(color),
        (None, base) => base,
    }
}

/// Reduce cached RGB samples (nodes, 81, 3) to mineral medians and interpolate
/// the query plan. Optional per-node regional colours fill rejected evidence
/// before interpolation, so the last reliable node cannot snap to a climate tint.
#[pyfunction]
#[pyo3(signature = (rgb, indices, weights, fallback=None, regional=false))]
pub fn cliff_photo_tints<'py>(
    py: Python<'py>,
    rgb: PyReadonlyArray3<'py, f64>,
    indices: PyReadonlyArray2<'py, i64>,
    weights: PyReadonlyArray2<'py, f64>,
    fallback: Option<PyReadonlyArray2<'py, f64>>,
    regional: bool,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (rgb, indices, weights) = (rgb.as_array(), indices.as_array(), weights.as_array());
    let fallback = fallback.as_ref().map(|v| v.as_array());
    if rgb.shape()[1..] != [PROBES, 3]
        || fallback.as_ref().is_some_and(|v| v.shape() != [rgb.shape()[0], 3] || v.iter().any(|c| !c.is_finite()))
        || indices.ncols() != 4
        || weights.shape() != indices.shape()
        || indices.iter().any(|&i| i < 0 || i as usize >= rgb.shape()[0])
        || weights.iter().any(|v| !v.is_finite() || *v < 0.0)
    {
        return Err(PyValueError::new_err(
            "matching colour probes and valid four-node interpolation plan required",
        ));
    }
    let out = py.detach(|| {
        let field: Vec<_> = (0..rgb.shape()[0])
            .map(|n| resolve(
                mineral((0..PROBES).map(|p| [rgb[[n, p, 0]], rgb[[n, p, 1]], rgb[[n, p, 2]]]), regional),
                fallback.as_ref().map(|v| [v[[n, 0]], v[[n, 1]], v[[n, 2]]])))
            .collect();
        let mut out = vec![0.0; indices.nrows() * 3];
        for i in 0..indices.nrows() {
            let mut total = 0.0;
            for j in 0..4 {
                if let Some(color) = field[indices[[i, j]] as usize] {
                    let w = weights[[i, j]];
                    for k in 0..3 {
                        out[i * 3 + k] += w * color[k];
                    }
                    total += w;
                }
            }
            for k in 0..3 {
                out[i * 3 + k] /= total.max(1e-6);
            }
        }
        out
    });
    Ok(Array2::from_shape_vec((indices.nrows(), 3), out)
        .unwrap()
        .into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn regional_colour_survives_missing_and_sparse_local_evidence() {
        let base = [190., 175., 150.];
        assert_eq!(resolve(None, Some(base)), Some(base));
        assert_eq!(resolve(Some(([100.; 3], 0.0)), Some(base)), Some(base));
        assert_eq!(resolve(Some(([100.; 3], 1.0)), Some(base)), Some([100.; 3]));
        assert_eq!(resolve(Some(([100.; 3], 0.5)), Some(base)), Some([145., 137.5, 125.]));
    }

    #[test]
    fn regional_ground_hue_retains_tan_but_rejects_green_water_and_shadow() {
        let tan = [174., 155., 113.5];
        assert_eq!(mineral(std::iter::repeat_n(tan, PROBES), true), Some((tan, 1.0)));
        for rgb in [[30., 120., 20.], [30., 40., 100.], [10.; 3]] {
            assert_eq!(mineral(std::iter::repeat_n(rgb, PROBES), true), None);
        }
    }

    #[test]
    fn reject_canopy_water_shadows_and_sparse_evidence() {
        for rgb in [[30., 120., 20.], [30., 40., 100.], [10.; 3]] {
            assert_eq!(mineral(std::iter::repeat_n(rgb, PROBES), false), None);
        }
        assert_eq!(mineral(std::iter::repeat_n([190., 175., 150.], 2), false), None);
        assert_eq!(
            mineral(std::iter::repeat_n([190., 175., 150.], PROBES), false),
            Some(([190., 175., 150.], 1.0))
        );
    }
}
