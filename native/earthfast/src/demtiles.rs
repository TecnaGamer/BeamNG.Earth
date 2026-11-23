//! Tile selection and decoding for the elevation providers. Download and cache effects
//! stay on the calling worker, including failures that mark an entire ancestor chain short.
use crate::dm::Dm;
use numpy::{ndarray::Array2, IntoPyArray, PyArray2, PyReadonlyArray2};
use pyo3::{
    exceptions::PyValueError,
    prelude::*,
    types::{PyBytes, PyDict, PyList},
};

fn native_size(data: &[u8]) -> Option<(usize, usize)> {
    // The providers publish 8-bit RGB(A) PNG and single-frame lossless WebP.
    // Other modes use Pillow's colour conversion and alpha-aware resampling.
    if data.starts_with(b"\x89PNG\r\n\x1a\n")
        && data.get(12..16) == Some(b"IHDR")
        && data.get(24) == Some(&8)
        && matches!(data.get(25), Some(2 | 6))
    {
        let width = u32::from_be_bytes(data.get(16..20)?.try_into().ok()?);
        let height = u32::from_be_bytes(data.get(20..24)?.try_into().ok()?);
        return Some((width as usize, height as usize));
    }
    if data.starts_with(b"RIFF")
        && data.get(8..16) == Some(b"WEBPVP8L")
        && data.get(20) == Some(&0x2f)
    {
        // The final RIFF pad is part of the advertised download. A VP8L
        // decoder can recover all pixels even when that byte never arrived.
        let length = u32::from_le_bytes(data.get(4..8)?.try_into().ok()?) as usize;
        if length > data.len() - 8 {
            return None;
        }
        let header = u32::from_le_bytes(data.get(21..25)?.try_into().ok()?);
        return Some((
            ((header & 0x3fff) + 1) as usize,
            (((header >> 14) & 0x3fff) + 1) as usize,
        ));
    }
    None
}

#[pyfunction]
fn dem_decode<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyBytes>,
    size: usize,
    other_image: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyArray2<f32>>> {
    let bytes = data.as_bytes();
    if size > 0 && size <= 4096 && native_size(bytes) == Some((size, size)) {
        let array = py.detach(|| -> Result<_, String> {
            let (w, h, ch, pixels) = crate::texdds::decode_image(bytes)?;
            if (w, h) != (size, size) || ch < 3 {
                return Err("unexpected DEM pixel layout".into());
            }
            let heights = pixels
                .chunks_exact(ch)
                .map(|p| crate::terrarium_value(p[0], p[1], p[2]))
                .collect();
            Ok(Array2::from_shape_vec((h, w), heights).expect("decoded pixel count"))
        });
        if let Ok(array) = array {
            return Ok(array.into_pyarray(py));
        }
        // A partial download must follow the same Pillow acceptance/error policy
        // as cached tiles from earlier releases.
    }
    let image = other_image.call1((data, size))?;
    Ok(crate::terrarium_heights(py, image.extract()?))
}

#[pyfunction]
fn dem_ancestor<'py>(
    py: Python<'py>,
    tile: PyReadonlyArray2<'py, f32>,
    depth: u32,
    x: i64,
    y: i64,
) -> PyResult<Bound<'py, PyArray2<f32>>> {
    let a = tile.as_array();
    let (n, m) = a.dim();
    if n < 2 || n != m || depth > 52 {
        return Err(PyValueError::new_err("invalid DEM ancestor"));
    }
    let scale = 1i64 << depth;
    let axis = |q: i64| {
        let start = ((q.rem_euclid(scale) as f64) * n as f64) / scale as f64;
        (0..n)
            .map(|i| {
                let v = (start + ((i as f64 + 0.5) / scale as f64 - 0.5)).clamp(0., (n - 1) as f64);
                let k = (v as usize).min(n - 2);
                (k, v - k as f64)
            })
            .collect::<Vec<_>>()
    };
    Ok(py
        .detach(|| {
            let (cols, rows) = (axis(x), axis(y));
            Array2::from_shape_fn((n, n), |(r, c)| {
                let (u, fu) = cols[c];
                let (v, fv) = rows[r];
                let top = a[[v, u]] as f64 * (1. - fu) + a[[v, u + 1]] as f64 * fu;
                let bottom = a[[v + 1, u]] as f64 * (1. - fu) + a[[v + 1, u + 1]] as f64 * fu;
                (top * (1. - fv) + bottom * fv) as f32
            })
        })
        .into_pyarray(py))
}

#[pyfunction]
fn dem_coverage<'py>(
    py: Python<'py>,
    items: &Bound<'py, PyAny>,
    zoom: u32,
) -> PyResult<Bound<'py, PyDict>> {
    let out = PyDict::new(py);
    let int = py.import("builtins")?.getattr("int")?;
    let prefix = zoom.to_string();
    for item in items.try_iter()? {
        let item = item?;
        let name = item.call_method1("get", ("name", ""))?.str()?.to_string();
        let mut parts = name.split('.').next().unwrap_or("").split('-');
        if parts.next() != Some(prefix.as_str()) {
            continue;
        }
        let (Some(x), Some(y), None) = (parts.next(), parts.next(), parts.next()) else {
            continue;
        };
        let key_zoom = (|| -> PyResult<_> {
            let depth = int.call1((item.get_item("max_zoom")?,))?;
            Ok(((int.call1((x,))?, int.call1((y,))?), depth))
        })();
        match key_zoom {
            Ok((key, depth)) => out.set_item(key, depth)?,
            Err(e)
                if e.is_instance_of::<pyo3::exceptions::PyKeyError>(py)
                    || e.is_instance_of::<pyo3::exceptions::PyTypeError>(py)
                    || e.is_instance_of::<PyValueError>(py) =>
            {
                ()
            }
            Err(e) => return Err(e),
        }
    }
    Ok(out)
}

#[pyfunction]
fn dem_listed(
    index: &Bound<'_, PyAny>,
    z: u32,
    x: i64,
    y: i64,
    coverage_zoom: u32,
    global_zoom: u32,
) -> PyResult<bool> {
    if index.is_none() {
        return Ok(true);
    }
    let shift = z
        .checked_sub(coverage_zoom)
        .ok_or_else(|| PyValueError::new_err("negative shift count"))?;
    let parent = |v: i64| v >> shift.min(63);
    index
        .call_method1("get", ((parent(x), parent(y)), global_zoom))?
        .ge(z)
}

#[pyfunction]
fn dem_deepest(
    py: Python<'_>,
    dem: &Bound<'_, PyAny>,
    source: &Bound<'_, PyAny>,
    error: &Bound<'_, PyAny>,
    z: u32,
    x: i64,
    y: i64,
    global_zoom: u32,
) -> PyResult<Option<u32>> {
    let keys: Vec<_> = (global_zoom + 1..z)
        .rev()
        .map(|a| (a, x >> (z - a).min(63), y >> (z - a).min(63)))
        .collect();
    let futures = dem.call_method1("_ahead", (PyList::new(py, &keys)?,))?;
    dem.call_method1("_wait", (futures,))?;
    for (a, px, py_) in keys {
        match dem.call_method1("_download", (source, a, px, py_)) {
            Ok(data) if !data.is_none() => return Ok(Some(a)),
            Ok(_) => (),
            Err(e) if e.matches(py, error)? => {
                dem.call_method0("_partial")?;
                break;
            }
            Err(e) => return Err(e),
        }
    }
    Ok(None)
}

/// Select the first available source at the requested depth. Cache faults and
/// the "served" field must be updated before and after the same provider calls.
#[pyfunction]
fn dem_source<'py>(
    py: Python<'py>,
    reader: &Bound<'py, PyAny>,
    module: &Bound<'py, PyAny>,
    key: (u32, i64, i64),
) -> PyResult<Bound<'py, PyAny>> {
    let (z, x, y) = key;
    let mt = module.getattr("MAPTERHORN")?;
    let global: u32 = module.getattr("GLOBAL_ZOOM")?.extract()?;
    let max: u32 = module.getattr("MAX_ZOOM")?.extract()?;
    let err = module.getattr("netfetch")?.getattr("FetchError")?;
    let served = reader.getattr("served")?;
    let data = reader
        .call_method1("_download", (&mt, z, x, y))
        .and_then(|b| {
            if b.is_none() {
                Ok(b)
            } else {
                reader.call_method1("_decode", (&mt, z, x, y, b))
            }
        });
    let failure = match data {
        Ok(a) if !a.is_none() => {
            served.set_item(key, mt.get_item("name")?)?;
            if !reader.getattr("ground_filter")?.is_truthy()? {
                return Ok(a);
            }
            let lidar = if z >= global {
                z > global || reader.call_method1("_lidar", (x, y))?.is_truthy()?
            } else {
                let shift = global - z;
                let half = (1i64 << shift) / 2;
                reader
                    .call_method1("_lidar", ((x << shift) + half, (y << shift) + half))?
                    .is_truthy()?
            };
            if lidar {
                let rounds = module.call_method1("hole_px", (z, y))?;
                return module.call_method1("fill_holes", (a, rounds));
            }
            return if z == global {
                reader.call_method1("_ground12", (x, y, a))
            } else {
                Ok(a)
            };
        }
        Ok(_) => None,
        Err(e) if e.matches(py, &err)? => {
            reader.call_method1("_count", ("mapterhorn:failover",))?;
            reader.call_method0("_partial")?;
            Some(e)
        }
        Err(e) => return Err(e),
    };
    if z > max
        || (failure.is_none() && z > global && reader.getattr("ground_filter")?.is_truthy()?)
    {
        let a = if z > max {
            reader.call_method1("_from_ancestor", (z, x, y, max))?
        } else {
            let ancestor = reader.call_method1("_deepest_lidar", key)?;
            if ancestor.is_truthy()? {
                reader.call_method1("_from_ancestor", (z, x, y, ancestor))?
            } else {
                reader.call_method1("_from_ground12", key)?
            }
        };
        served.set_item(key, mt.get_item("name")?)?;
        return Ok(a);
    }
    let a = match reader.call_method1("_aws_tile", key) {
        Ok(a) => a,
        Err(e) if e.matches(py, &err)? => {
            let reason = match failure {
                Some(mt) => format!(
                    "Mapterhorn: {}; AWS: {}",
                    mt.value(py).str()?,
                    e.value(py).str()?
                ),
                None => format!("AWS: {}", e.value(py).str()?),
            };
            return Err(PyErr::from_value(
                module
                    .getattr("DemUnavailable")?
                    .call1((format!("DEM tile {z}/{x}/{y}: {reason}"),))?,
            ));
        }
        Err(e) => return Err(e),
    };
    if a.is_none() {
        served.set_item(key, py.None())?;
        let n: usize = module.getattr("TILE")?.extract()?;
        return Ok(Array2::<f32>::zeros((n, n)).into_pyarray(py).into_any());
    }
    served.set_item(key, module.getattr("AWS")?.get_item("name")?)?;
    if failure.is_none() && z <= global {
        // The AWS fallback on a failed land request retains its bathymetry.
        return py.import("numpy")?.call_method1("maximum", (a, 0.));
    }
    Ok(a)
}

#[pyfunction]
fn dem_lidar(
    reader: &Bound<'_, PyAny>,
    module: &Bound<'_, PyAny>,
    x: i64,
    y: i64,
) -> PyResult<bool> {
    let py = reader.py();
    let z = module.getattr("GLOBAL_ZOOM")?.extract::<u32>()? + 1;
    let mt = module.getattr("MAPTERHORN")?;
    let kids = [(z, 2 * x, 2 * y), (z, 2 * x + 1, 2 * y + 1)];
    let exists = module.getattr("os")?.getattr("path")?.getattr("exists")?;
    for &(z, x, y) in &kids {
        let path = reader.call_method1("_path", (&mt, z, x, y))?;
        if exists.call1((path,))?.is_truthy()?
            && !reader.call_method1("_download", (&mt, z, x, y))?.is_none()
        {
            return Ok(true);
        }
    }
    if !reader.getattr("archives")?.is_none() {
        let got = reader.call_method1("_archive_lidar", (kids,))?;
        if !got.is_none() {
            return got.is_truthy();
        }
    }
    let error = module.getattr("netfetch")?.getattr("FetchError")?;
    for (z, x, y) in kids {
        match reader.call_method1("_download", (&mt, z, x, y)) {
            Ok(tile) if !tile.is_none() => return Ok(true),
            Ok(_) => (),
            Err(e) if e.matches(py, &error)? => {
                reader.call_method0("_partial")?;
            }
            Err(e) => return Err(e),
        }
    }
    Ok(false)
}

pub fn pixel_m(z: u32, y: i64, tile: usize) -> f64 {
    let n = 2f64.dpowf(z as f64);
    let lat = (std::f64::consts::PI * (1. - 2. * (y as f64 + 0.5) / n))
        .dsinh()
        .datan()
        .to_degrees();
    40075016.686 * lat.to_radians().dcos() / n / tile as f64
}

#[pyfunction]
fn dem_hole_px(z: u32, y: i64, tile: usize, metres: f64, cap: usize) -> usize {
    ((metres / pixel_m(z, y, tile)).round_ties_even() as usize)
        .max(2)
        .min(cap)
}

#[pyfunction]
fn dem_xy(lat: f64, lon: f64, z: u32, max_lat: f64) -> PyResult<(i64, i64)> {
    if z > 52 || !lon.is_finite() {
        return Err(PyValueError::new_err("invalid DEM coordinates"));
    }
    let n = 1i64 << z;
    let x = (((lon + 180.) / 360. * n as f64) as i64).rem_euclid(n);
    let phi = lat.min(max_lat).max(-max_lat).to_radians();
    let y = ((1. - phi.dtan().dasinh() / std::f64::consts::PI) / 2. * n as f64) as i64;
    Ok((x, y.clamp(0, n - 1)))
}

#[pyfunction]
fn dem_aws<'py>(
    py: Python<'py>,
    reader: &Bound<'py, PyAny>,
    source: &Bound<'py, PyAny>,
    key: (u32, i64, i64),
    max_zoom: u32,
    tile: usize,
) -> PyResult<Option<Bound<'py, PyArray2<f32>>>> {
    let (z, x, y) = key;
    if z > max_zoom || tile == 0 {
        return Err(PyValueError::new_err("invalid AWS tile request"));
    }
    let za = (z + 1).min(max_zoom);
    let n = 1usize << (za - z);
    let side = source.get_item("size")?.extract::<usize>()?;
    let mut mosaic = Array2::<f32>::zeros((n * side, n * side));
    let mut found = false;
    for j in 0..n {
        for i in 0..n {
            let (x, y) = (x * n as i64 + i as i64, y * n as i64 + j as i64);
            let data = reader.call_method1("_download", (source, za, x, y))?;
            if data.is_none() {
                continue;
            }
            let a = reader
                .call_method1("_decode", (source, za, x, y, data))?
                .extract::<PyReadonlyArray2<f32>>()?;
            if a.as_array().dim() != (side, side) {
                return Err(PyValueError::new_err("invalid AWS tile shape"));
            }
            mosaic
                .slice_mut(numpy::ndarray::s![
                    j * side..(j + 1) * side,
                    i * side..(i + 1) * side
                ])
                .assign(&a.as_array());
            found = true;
        }
    }
    if !found {
        return Ok(None);
    }
    if n * side == tile {
        return Ok(Some(mosaic.into_pyarray(py)));
    }
    let out = py.detach(|| crate::fartex::resize_f32(mosaic.view(), tile, tile)).into_pyarray(py);
    // A resized float image was exposed as a read-only array by Pillow.
    out.call_method1("setflags", (false,))?;
    Ok(Some(out))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(dem_xy, m)?)?;
    m.add_function(wrap_pyfunction!(dem_aws, m)?)?;
    m.add_function(wrap_pyfunction!(dem_hole_px, m)?)?;
    m.add_function(wrap_pyfunction!(dem_source, m)?)?;
    m.add_function(wrap_pyfunction!(dem_lidar, m)?)?;
    m.add_function(wrap_pyfunction!(dem_decode, m)?)?;
    m.add_function(wrap_pyfunction!(dem_ancestor, m)?)?;
    m.add_function(wrap_pyfunction!(dem_coverage, m)?)?;
    m.add_function(wrap_pyfunction!(dem_listed, m)?)?;
    m.add_function(wrap_pyfunction!(dem_deepest, m)?)?;
    Ok(())
}
