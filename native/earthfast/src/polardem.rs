//! Copernicus cap band selection, wraparound mosaics, and point sampling.
use crate::dm::Dm;
use numpy::{ndarray::ArrayD, IntoPyArray, PyArrayDyn, PyArrayMethods, PyReadonlyArrayDyn};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::collections::{BTreeMap, BTreeSet};

fn file_lat(lat: f64) -> i64 {
    89 - (((90. - lat) * 3600.).floor() as i64).clamp(0, 180 * 3600 - 1) / 3600
}

#[pyfunction]
fn polar_file_lat<'py>(
    py: Python<'py>,
    lat: PyReadonlyArrayDyn<'py, f64>,
) -> Bound<'py, PyArrayDyn<i64>> {
    lat.as_array().mapv(file_lat).into_pyarray(py)
}

#[pyfunction]
fn polar_band_cols(sw: i64) -> PyResult<i64> {
    if sw >= 85 || sw <= -86 {
        Ok(360)
    } else if (80..=84).contains(&sw) || (-85..=-81).contains(&sw) {
        Ok(720)
    } else {
        Err(PyValueError::new_err(format!(
            "latitude {sw} is outside the polar bands"
        )))
    }
}

fn level_for(pixel_m: Option<f64>, pixel: f64, levels: u32) -> u32 {
    let m = pixel_m.unwrap_or(0.);
    if m <= pixel {
        0
    } else {
        ((m / pixel).dlog2().floor() as u32).min(levels - 1)
    }
}

#[pyfunction]
fn polar_level(pixel_m: Option<f64>, pixel: f64, levels: u32) -> PyResult<u32> {
    if levels == 0 {
        return Err(PyValueError::new_err("no polar overviews"));
    }
    Ok(level_for(pixel_m, pixel, levels))
}

fn coord(degrees: f64, per_degree: i64, f: f64) -> f64 {
    (degrees * per_degree as f64 - (f - 1.) / 2.) / f
}

#[pyfunction]
fn polar_pixels<'py>(
    py: Python<'py>,
    lat: PyReadonlyArrayDyn<'py, f64>,
    lon: PyReadonlyArrayDyn<'py, f64>,
    level: u32,
    cpd: i64,
) -> (Bound<'py, PyArrayDyn<f64>>, Bound<'py, PyArrayDyn<f64>>) {
    let f = 2f64.dpowf(level as f64);
    (
        lon.as_array()
            .mapv(|v| coord(v + 180., cpd, f))
            .into_pyarray(py),
        lat.as_array()
            .mapv(|v| coord(90. - v, 3600, f))
            .into_pyarray(py),
    )
}

#[derive(Clone, Copy)]
struct Point {
    index: usize,
    x: f64,
    y: f64,
}

struct Band {
    level: u32,
    cpd: i64,
    cols: i64,
    first: i64,
    last: i64,
    max_pixels: i64,
}

fn sample_band(
    py: Python<'_>,
    band: &Band,
    pts: &[Point],
    read: &Bound<'_, PyAny>,
    out: &mut [f64],
) -> PyResult<()> {
    let span = |get: fn(&Point) -> f64| {
        let mut lo = f64::INFINITY;
        let mut hi = f64::NEG_INFINITY;
        for p in pts {
            let v = get(p);
            lo = lo.min(v);
            hi = hi.max(v);
        }
        (lo.floor() as i64, hi.floor() as i64 + 1)
    };
    let (low, high) = span(|p| p.y);
    let r0 = low.clamp(band.first, band.last - 1);
    let r1 = high.clamp(r0 + 1, band.last);
    let (mut c0, mut c1) = span(|p| p.x);
    if c1 - c0 + 1 > band.cols / 2 {
        (c0, c1) = (0, band.cols);
    }
    if (r1 - r0 + 1) * (c1 - c0 + 1) > band.max_pixels {
        let mut ys = pts.iter().map(|p| p.y).collect::<Vec<_>>();
        ys.sort_unstable_by(f64::total_cmp);
        let median = (ys[(ys.len() - 1) / 2] + ys[ys.len() / 2]) / 2.;
        let (lower, upper): (Vec<_>, Vec<_>) = pts.iter().copied().partition(|p| p.y <= median);
        if !lower.is_empty() && !upper.is_empty() {
            sample_band(py, band, &lower, read, out)?;
            return sample_band(py, band, &upper, read, out);
        }
    }
    let mosaic = read.call1((band.level, band.cpd, r0, r1, c0, c1.max(c0 + 1), band.cols))?;
    if !mosaic.is_none() {
        let x = pts
            .iter()
            .map(|p| p.x - c0 as f64)
            .collect::<Vec<_>>()
            .into_pyarray(py);
        let y = pts
            .iter()
            .map(|p| p.y - r0 as f64)
            .collect::<Vec<_>>()
            .into_pyarray(py);
        let z = crate::bilinear_grid(py, mosaic.extract()?, x.readonly(), y.readonly())?;
        for (p, &v) in pts.iter().zip(z.readonly().as_array()) {
            out[p.index] = v;
        }
    }
    Ok(())
}

#[pyfunction]
fn polar_sample<'py>(
    py: Python<'py>,
    lat: PyReadonlyArrayDyn<'py, f64>,
    lon: PyReadonlyArrayDyn<'py, f64>,
    pixel_m: Option<f64>,
    pixel: f64,
    levels: u32,
    max_files: usize,
    max_pixels: i64,
    read: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyArrayDyn<f64>>> {
    let (lat, lon) = (lat.as_array(), lon.as_array());
    if lat.shape() != lon.shape() || !(1..=4).contains(&levels) {
        return Err(PyValueError::new_err("invalid polar sample grid"));
    }
    let mut groups = BTreeMap::<(i64, bool), Vec<Point>>::new();
    let mut files = BTreeSet::new();
    for (index, (&a, &o)) in lat.iter().zip(lon.iter()).enumerate() {
        let o = (o + 180.).rem_euclid(360.) - 180.;
        let sw = file_lat(a);
        files.insert(a.floor() as i64 * 1000 + o.floor() as i64);
        groups
            .entry((polar_band_cols(sw)?, sw < 0))
            .or_default()
            .push(Point { index, x: o, y: a });
    }
    let level = if files.len() > max_files {
        levels - 1
    } else {
        level_for(pixel_m, pixel, levels)
    };
    let f = 1i64 << level;
    let mut out = vec![0.; lat.len()];
    for ((cpd, south), mut pts) in groups {
        let (top, bottom) = match (cpd, south) {
            (360, false) => (90, 85),
            (360, true) => (-85, -90),
            (720, false) => (85, 80),
            _ => (-80, -85),
        };
        let band = Band {
            level,
            cpd,
            cols: 360 * cpd / f,
            first: (90 - top) * 3600 / f,
            last: (90 - bottom) * 3600 / f - 1,
            max_pixels,
        };
        for p in &mut pts {
            p.x = coord(p.x + 180., cpd, f as f64).rem_euclid(band.cols as f64);
            p.y = coord(90. - p.y, 3600, f as f64);
        }
        sample_band(py, &band, &pts, read, &mut out)?;
    }
    Ok(ArrayD::from_shape_vec(lat.raw_dim(), out)
        .unwrap()
        .into_pyarray(py))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(polar_file_lat, m)?)?;
    m.add_function(wrap_pyfunction!(polar_band_cols, m)?)?;
    m.add_function(wrap_pyfunction!(polar_level, m)?)?;
    m.add_function(wrap_pyfunction!(polar_pixels, m)?)?;
    m.add_function(wrap_pyfunction!(polar_sample, m)?)?;
    Ok(())
}
