//! Filters and the terrain horizon used by the regional sun fit and per-tile relighting.
use crate::dm::Dm;
use numpy::ndarray::{Array2, ArrayView2};
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2};
use pyo3::{exceptions::PyValueError, prelude::*};

fn box_mean(src: ArrayView2<f32>, radius: usize) -> Array2<f32> {
    let (h, w) = src.dim();
    // The original first summed down each column, then across the integral image in float64.
    let mut prefix = Array2::<f64>::zeros((h + 1, w + 1));
    let mut cols = vec![0.; w];
    for y in 0..h {
        let mut row = 0.;
        for x in 0..w {
            cols[x] += src[[y, x]] as f64;
            row += cols[x];
            prefix[[y + 1, x + 1]] = row;
        }
    }
    Array2::from_shape_fn((h, w), |(y, x)| {
        let y0 = y.saturating_sub(radius);
        let x0 = x.saturating_sub(radius);
        let y1 = (y + 1).saturating_add(radius).min(h);
        let x1 = (x + 1).saturating_add(radius).min(w);
        let sum = prefix[[y1, x1]] - prefix[[y0, x1]] - prefix[[y1, x0]] + prefix[[y0, x0]];
        (sum * (1.0 / ((y1 - y0) * (x1 - x0)) as f64)) as f32
    })
}

#[pyfunction]
pub fn shadow_box<'py>(
    py: Python<'py>,
    src: PyReadonlyArray2<'py, f32>,
    radius: usize,
) -> Bound<'py, PyArray2<f32>> {
    let src = src.as_array();
    py.detach(|| box_mean(src, radius)).into_pyarray(py)
}

#[pyfunction]
pub fn shadow_normals<'py>(
    py: Python<'py>,
    heights: PyReadonlyArray2<'py, f64>,
    res: f64,
) -> PyResult<(
    Bound<'py, PyArray2<f32>>,
    Bound<'py, PyArray2<f32>>,
    Bound<'py, PyArray2<f32>>,
)> {
    let a = heights.as_array();
    let (h, w) = a.dim();
    if h < 2 || w < 2 {
        return Err(PyValueError::new_err(
            "normals: both axes need at least two heights",
        ));
    }
    let (x, y, z) = py.detach(|| {
        let mut x = Array2::zeros((h, w));
        let mut y = x.clone();
        let mut z = x.clone();
        for r in 0..h {
            let (top, bot) = (r.saturating_sub(1), (r + 1).min(h - 1));
            for c in 0..w {
                let (left, right) = (c.saturating_sub(1), (c + 1).min(w - 1));
                let dx = -(a[[r, right]] - a[[r, left]]) / ((right - left) as f64 * res);
                let dy = (a[[bot, c]] - a[[top, c]]) / ((bot - top) as f64 * res);
                let norm = (dx * dx + dy * dy + 1.0).sqrt();
                x[[r, c]] = (dx / norm) as f32;
                y[[r, c]] = (dy / norm) as f32;
                z[[r, c]] = (1.0 / norm) as f32;
            }
        }
        (x, y, z)
    });
    Ok((x.into_pyarray(py), y.into_pyarray(py), z.into_pyarray(py)))
}

#[pyfunction]
pub fn shadow_horizon<'py>(
    py: Python<'py>,
    heights: PyReadonlyArray2<'py, f32>,
    res: f64,
    pad: usize,
    n: usize,
    az: f64,
    stop: f64,
    growth: f64,
    batch: usize,
    pace: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyArray2<f32>>> {
    let view = heights.as_array();
    let (h, w) = view.dim();
    if !(res > 0.0)
        || !res.is_finite()
        || pad.checked_add(n).is_none_or(|end| end > h || end > w)
        || batch == 0
    {
        return Err(PyValueError::new_err(
            "horizon: invalid geometry or batch size",
        ));
    }
    let hp = heights.as_slice()?;
    let (sin, cos) = (az.to_radians().dsin(), az.to_radians().dcos());
    let mut best = Array2::from_elem((n, n), -1e9f32);
    let mut d = res;
    let stop = stop.min((pad as f64 - 1.5) * res);
    while d <= stop {
        pace.call0()?;
        let mut steps = Vec::new();
        while d <= stop && steps.len() < batch {
            let (dy, dx) = (-cos * d / res, sin * d / res);
            let (iy, ix) = (dy.floor(), dx.floor());
            let y = pad as i64 + iy as i64;
            let x = pad as i64 + ix as i64;
            if x < 0
                || y < 0
                || (x as usize).saturating_add(n) >= w
                || (y as usize).saturating_add(n) >= h
            {
                return Err(PyValueError::new_err("horizon: sample outside padded grid"));
            }
            steps.push([iy, ix, dy - iy, dx - ix, d]);
            d = (d + res).max(d * growth);
        }
        py.detach(|| crate::apply_horizon(hp, w, best.as_slice_mut().unwrap(), n, pad, &steps));
    }
    Ok(best.into_pyarray(py))
}

/// Score every candidate elevation for one azimuth. Centered covariance uses the same
/// product reduction as dnp.matmul. The luminance threshold and variance are shared by all azimuths.
#[pyfunction]
pub fn sun_scores<'py>(
    py: Python<'py>,
    horizontal: PyReadonlyArray2<'py, f32>,
    up: PyReadonlyArray2<'py, f32>,
    horizon: PyReadonlyArray2<'py, f32>,
    centered: numpy::PyReadonlyArray1<'py, f32>,
    dark: numpy::PyReadonlyArray1<'py, bool>,
    variance: f64,
    elevations: Vec<f64>,
    pace: &Bound<'py, PyAny>,
) -> PyResult<(Vec<f64>, Vec<f64>)> {
    let (a, z, ht) = (horizontal.as_array(), up.as_array(), horizon.as_array());
    let (lc, dark) = (centered.as_array(), dark.as_array());
    if a.is_empty()
        || a.dim() != z.dim()
        || a.dim() != ht.dim()
        || a.len() != lc.len()
        || a.len() != dark.len()
    {
        return Err(PyValueError::new_err(
            "sun_scores: inconsistent sample counts",
        ));
    }
    let mut r2 = Vec::with_capacity(elevations.len());
    let mut mcc = Vec::with_capacity(elevations.len());
    let mut direct = vec![0.0f32; a.len()];
    let nd = dark.iter().filter(|&&v| v).count();
    for elevation in elevations {
        pace.call0()?;
        let (sq, score) = py.detach(|| {
            let el = elevation.to_radians();
            let (cos, sin, tan) = (el.dcos() as f32, el.dsin() as f32, el.dtan() as f32);
            let (mut ns, mut tp) = (0, 0);
            for (i, ((&xy, &up), &h)) in a.iter().zip(z.iter()).zip(ht.iter()).enumerate() {
                let lit = h < tan;
                let value = (xy * cos + up * sin).max(0.) * u8::from(lit) as f32;
                direct[i] = value;
                if !lit || value < 0.15 {
                    ns += 1;
                    tp += usize::from(dark[i]);
                }
            }
            let n = direct.len();
            let mean = crate::dmmatmul::sum1_f32(n, |i| direct[i]) / n as f32;
            for v in &mut direct {
                *v -= mean;
            }
            let cov = crate::dmmatmul::dot1_f32(n, |i| direct[i] * lc[i]) as f64;
            let var = crate::dmmatmul::dot1_f32(n, |i| direct[i] * direct[i]) as f64 + 1e-12;
            let r = cov / (var * variance).sqrt();
            let (fp, miss) = (ns - tp, nd - tp);
            let tn = n - tp - fp - miss;
            let denom =
                ((tp + fp) as f64 * (tp + miss) as f64 * (tn + fp) as f64 * (tn + miss) as f64)
                    .sqrt();
            let numer = tp as i64 * tn as i64 - fp as i64 * miss as i64;
            (
                r * r.abs(),
                if denom > 0.0 {
                    numer as f64 / denom
                } else {
                    0.0
                },
            )
        });
        r2.push(sq);
        mcc.push(score);
    }
    Ok((r2, mcc))
}
