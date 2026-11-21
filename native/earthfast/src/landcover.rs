//! WorldCover sampling coordinates and material classification on the block lattice.
use crate::{dm::Dm, groundpaint::value_noise};
use numpy::{
    ndarray::{Array2, ArrayView2},
    IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2,
};
use pyo3::{exceptions::PyValueError, prelude::*};

const M_PER_DEG: f64 = 111320.;

#[pyfunction]
pub fn landcover_warp<'py>(
    py: Python<'py>,
    lat: PyReadonlyArray1<'py, f64>,
    lon: PyReadonlyArray1<'py, f64>,
    octaves: Vec<(f64, f64)>,
) -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    let (lat, lon) = (lat.as_array(), lon.as_array());
    if lat.len() != lon.len() {
        return Err(PyValueError::new_err("land-cover coordinates must match"));
    }
    let (lat, lon) = py.detach(|| {
        let mut pairs = vec![(0., 0.); lat.len()];
        crate::par::rows(
            &mut pairs,
            8192,
            crate::par::threads_for(lat.len(), 32768),
            |part, row| {
                for (j, pos) in row.iter_mut().enumerate() {
                    let i = part * 8192 + j;
                    let m = M_PER_DEG * lat[i].to_radians().dcos();
                    let (xm, ym) = (lon[i] * m, lat[i] * M_PER_DEG);
                    let (mut dx, mut dy) = (0., 0.);
                    for (k, &(size, amp)) in octaves.iter().enumerate() {
                        let (x, y) = (xm / size, ym / size);
                        dx += amp * value_noise(x, y, 2 * k as u64 + 1);
                        dy += amp * value_noise(x, y, 2 * k as u64 + 2);
                    }
                    *pos = (lat[i] + dy / M_PER_DEG, lon[i] + dx / m.max(1e-6));
                }
            },
        );
        pairs.into_iter().unzip::<_, _, Vec<_>, Vec<_>>()
    });
    Ok((lat.into_pyarray(py), lon.into_pyarray(py)))
}

fn dry_west(lat: f64, lon: f64) -> bool {
    lon < -102. && lat > 25. && lat < 46. && !(lon < -121. && lat > 37.)
}

#[pyfunction]
pub fn landcover_dry_west<'py>(
    py: Python<'py>,
    lat: PyReadonlyArray1<'py, f64>,
    lon: PyReadonlyArray1<'py, f64>,
) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let (lat, lon) = (lat.as_array(), lon.as_array());
    if lat.len() != lon.len() {
        return Err(PyValueError::new_err("region coordinates must match"));
    }
    Ok(py
        .detach(|| lat.iter().zip(lon).map(|(&a, &b)| dry_west(a, b)).collect::<Vec<_>>())
        .into_pyarray(py))
}

fn lawn(lat: f64, lon: f64, settings: (f64, f64, f64)) -> bool {
    let (size, dry_share, humid_share) = settings;
    let x = lon * M_PER_DEG * lat.to_radians().dcos();
    let y = lat * M_PER_DEG;
    let n = 0.65 * value_noise(x / size, y / size, 5) + 0.35 * value_noise(x / (size / 3.), y / (size / 3.), 6);
    let dry = dry_west(lat, lon);
    n > (0.5 - if dry { dry_share } else { humid_share }) * 1.1
}

#[pyfunction]
pub fn landcover_lawns<'py>(
    py: Python<'py>,
    lat: PyReadonlyArray1<'py, f64>,
    lon: PyReadonlyArray1<'py, f64>,
    settings: (f64, f64, f64),
) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let (lat, lon) = (lat.as_array(), lon.as_array());
    if lat.len() != lon.len() {
        return Err(PyValueError::new_err("lawn coordinates must match"));
    }
    Ok(py
        .detach(|| {
            lat.iter()
                .zip(lon)
                .map(|(&a, b)| lawn(a, *b, settings))
                .collect::<Vec<_>>()
        })
        .into_pyarray(py))
}

/// Whether any (or every) sample in a clipped square passes. Repeating an edge
/// sample beyond the grid cannot change an all-water test.
fn neighbourhood(mask: &Array2<bool>, r: usize, all: bool) -> Array2<bool> {
    let (h, w) = mask.dim();
    let mut prefix = Array2::<usize>::zeros((h + 1, w + 1));
    for y in 0..h {
        let mut sum = 0;
        for x in 0..w {
            sum += usize::from(mask[[y, x]]);
            prefix[[y + 1, x + 1]] = prefix[[y, x + 1]] + sum;
        }
    }
    Array2::from_shape_fn((h, w), |(y, x)| {
        let (x0, x1) = (x.saturating_sub(r), x.saturating_add(r).saturating_add(1).min(w));
        let (y0, y1) = (y.saturating_sub(r), y.saturating_add(r).saturating_add(1).min(h));
        let count = (prefix[[y1, x1]] - prefix[[y0, x1]]) - (prefix[[y1, x0]] - prefix[[y0, x0]]);
        if all {
            count == (x1 - x0) * (y1 - y0)
        } else {
            count != 0
        }
    })
}

fn beaches(classes: ArrayView2<u8>, square: f64, distances: (f64, f64, f64)) -> Option<Array2<bool>> {
    if classes.nrows().min(classes.ncols()) < 2
        || !classes.iter().any(|&c| c == 30 || c == 60)
        || !classes.iter().any(|&c| c == 80)
    {
        return None;
    }
    let (open, shore, bare) = distances;
    let radius = |metres: f64| (metres / square).round_ties_even().max(1.) as usize;
    let core = neighbourhood(&classes.mapv(|c| c == 80), radius(open / 2.), true);
    let shore = neighbourhood(&core, radius(shore + open / 2.), false);
    let bare = neighbourhood(&classes.mapv(|c| c == 60), radius(bare), false);
    Some(Array2::from_shape_fn(classes.dim(), |i| {
        shore[i] && (classes[i] == 60 || (classes[i] == 30 && bare[i]))
    }))
}

#[pyfunction]
pub fn landcover_sand<'py>(
    py: Python<'py>,
    classes: PyReadonlyArray2<'py, u8>,
    square: f64,
    distances: (f64, f64, f64),
) -> Bound<'py, PyArray2<bool>> {
    let c = classes.as_array();
    py.detach(|| beaches(c, square, distances).unwrap_or_else(|| Array2::from_elem(c.dim(), false)))
        .into_pyarray(py)
}

fn koppen(grid: ArrayView2<u8>, lat: f64, lon: f64, res: f64) -> u8 {
    let y = (((90. - lat) / res) as i64).clamp(0, grid.nrows() as i64 - 1) as usize;
    let x = ((lon + 180.).rem_euclid(360.) / res) as usize % grid.ncols();
    grid[[y, x]]
}

#[pyfunction]
pub fn koppen_samples<'py>(
    py: Python<'py>,
    grid: PyReadonlyArray2<'py, u8>,
    lat: PyReadonlyArray1<'py, f64>,
    lon: PyReadonlyArray1<'py, f64>,
    res: f64,
) -> PyResult<Bound<'py, PyArray1<i64>>> {
    let (g, lat, lon) = (grid.as_array(), lat.as_array(), lon.as_array());
    if g.is_empty() || lat.len() != lon.len() {
        return Err(PyValueError::new_err("invalid climate grid or coordinates"));
    }
    Ok(py
        .detach(|| {
            lat.iter()
                .zip(lon)
                .map(|(&a, &b)| koppen(g, a, b, res) as i64)
                .collect::<Vec<_>>()
        })
        .into_pyarray(py))
}

#[pyfunction]
#[pyo3(signature=(classes, u, square, lat, lon, climate, res, lut, materials, dry, slope, lawn_settings, distances))]
pub fn landcover_classify<'py>(
    py: Python<'py>,
    classes: PyReadonlyArray2<'py, u8>,
    u: PyReadonlyArray2<'py, f64>,
    square: f64,
    lat: Option<PyReadonlyArray2<'py, f64>>,
    lon: Option<PyReadonlyArray2<'py, f64>>,
    climate: Option<PyReadonlyArray2<'py, u8>>,
    res: f64,
    lut: PyReadonlyArray1<'py, u8>,
    materials: [u8; 5],
    dry: Vec<u8>,
    slope: f64,
    lawn_settings: (f64, f64, f64),
    distances: (f64, f64, f64),
) -> PyResult<Bound<'py, PyArray2<u8>>> {
    let (c, u, lut) = (classes.as_array(), u.as_array(), lut.as_array());
    let (lat, lon, grid) = (
        lat.as_ref().map(|a| a.as_array()),
        lon.as_ref().map(|a| a.as_array()),
        climate.as_ref().map(|a| a.as_array()),
    );
    let (h, w) = c.dim();
    if c.dim() != u.dim()
        || lat.iter().chain(lon.iter()).any(|a| a.dim() != c.dim())
        || lut.len() != 256
        || grid.is_some_and(|g| g.is_empty())
    {
        return Err(PyValueError::new_err("invalid land-cover grids"));
    }
    let [lawn_id, dry_id, sand_id, rock, snow] = materials;
    let out = py.detach(|| {
        let sand = beaches(c, square, distances);
        Array2::from_shape_fn((h, w), |(y, x)| {
            let i = [y, x];
            let mut m = lut[c[i] as usize];
            if let (Some(lat), Some(lon)) = (lat, lon) {
                if c[i] == 50 && lawn(lat[i], lon[i], lawn_settings) {
                    m = lawn_id;
                }
                if c[i] == 30 && grid.is_some_and(|g| dry.contains(&koppen(g, lat[i], lon[i], res))) {
                    m = dry_id;
                }
            }
            if sand.as_ref().is_some_and(|s| s[i]) {
                m = sand_id;
            }
            if w > 1 && h > 1 && m != snow {
                let (x0, x1) = (x.saturating_sub(1), (x + 1).min(w - 1));
                let (y0, y1) = (y.saturating_sub(1), (y + 1).min(h - 1));
                let gx = (u[[y, x1]] - u[[y, x0]]) / (square * (x1 - x0) as f64);
                let gy = (u[[y1, x]] - u[[y0, x]]) / (square * (y1 - y0) as f64);
                if gx * gx + gy * gy > slope * slope {
                    m = rock;
                }
            }
            m
        })
    });
    Ok(out.into_pyarray(py))
}

/// A cell's cached raster is requested before checking which points fall in it.
/// This preserves the provider's prefetch and short-input accounting.
#[pyfunction]
pub fn landcover_samples<'py>(
    py: Python<'py>,
    lat: PyReadonlyArray1<'py, f64>,
    lon: PyReadonlyArray1<'py, f64>,
    keys: &Bound<'py, PyAny>,
    source: &Bound<'py, PyAny>,
    queued: &Bound<'py, PyAny>,
    fault: &Bound<'py, PyAny>,
    max_lat: f64,
    polar: u8,
) -> PyResult<(Bound<'py, PyArray1<u8>>, bool)> {
    let (lat, lon) = (lat.as_array(), lon.as_array());
    if lat.len() != lon.len() {
        return Err(PyValueError::new_err("land-cover sample coordinates must match"));
    }
    let mut out = vec![0u8; lat.len()];
    let mut complete = true;
    for key in keys.try_iter()? {
        let key = key?;
        let raster = source.call1((&key,))?;
        let (w, s, e, n): (f64, f64, f64, f64) = key.call_method0("bounds")?.extract()?;
        let indices: Vec<_> = py.detach(|| {
            (0..lat.len())
                .filter(|&i| lat[i] >= s && lat[i] < n && lon[i] >= w && lon[i] < e)
                .collect()
        });
        if indices.is_empty() {
            continue;
        }
        if raster.is_none() {
            if complete {
                complete = !queued.call1((&key,))?.is_truthy()?;
            }
            if !complete {
                fault.call0()?;
            }
            continue;
        }
        let (x, y): (Vec<_>, Vec<_>) = py.detach(|| indices.iter().map(|&i| (lon[i], lat[i])).unzip());
        let values = raster.call_method1("sample", (x.into_pyarray(py), y.into_pyarray(py)))?;
        let np = py.import("numpy")?;
        let values = np.call_method1("asarray", (values, "uint8"))?;
        let values = np.call_method1("broadcast_to", (values, indices.len()))?;
        let values: PyReadonlyArray1<'_, u8> = values.extract()?;
        let values = values.as_array();
        py.detach(|| {
            for (i, &v) in indices.into_iter().zip(values) {
                out[i] = v;
            }
        });
    }
    py.detach(|| {
        for (v, &la) in out.iter_mut().zip(lat) {
            if la.abs() > max_lat {
                *v = polar;
            }
        }
    });
    Ok((out.into_pyarray(py), complete))
}
