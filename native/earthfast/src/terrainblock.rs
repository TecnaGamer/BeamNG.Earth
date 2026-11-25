//! The physics block's sample lattice and Torque v9 terrain file.
use numpy::{
    ndarray::Array2, IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyUntypedArray,
    PyUntypedArrayMethods,
};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::{
    fs::File,
    io::{BufWriter, Write},
    path::PathBuf,
};

#[pyfunction]
pub fn block_coordinates<'py>(
    py: Python<'py>,
    xs: PyReadonlyArray1<'py, f64>,
    ys: PyReadonlyArray1<'py, f64>,
    origin: PyReadonlyArray1<'py, f64>,
    rot: PyReadonlyArray2<'py, f64>,
) -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>)> {
    let (o, r) = crate::frame_of(&origin, &rot)?;
    let (xs, ys) = (xs.as_array(), ys.as_array());
    let (lat, lon) = py.detach(|| {
        let mut lat = Array2::zeros((ys.len(), xs.len()));
        let mut lon = lat.clone();
        for ((row, col), la) in lat.indexed_iter_mut() {
            let (a, b, _) = crate::e2g_one(xs[col], ys[row], 0., &o, &r);
            *la = a;
            lon[[row, col]] = b;
        }
        (lat, lon)
    });
    Ok((lat.into_pyarray(py), lon.into_pyarray(py)))
}

#[pyfunction]
pub fn block_up<'py>(
    py: Python<'py>,
    lat: PyReadonlyArray2<'py, f64>,
    lon: PyReadonlyArray2<'py, f64>,
    elev: PyReadonlyArray2<'py, f64>,
    origin: PyReadonlyArray1<'py, f64>,
    rot: PyReadonlyArray2<'py, f64>,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (o, r) = crate::frame_of(&origin, &rot)?;
    let (lat, lon, elev) = (lat.as_array(), lon.as_array(), elev.as_array());
    if lat.dim() != lon.dim() || lat.dim() != elev.dim() {
        return Err(PyValueError::new_err(
            "block_up needs matching elevation and position grids",
        ));
    }
    Ok(py
        .detach(|| {
            Array2::from_shape_fn(lat.dim(), |i| {
                crate::g2e_one(lat[i], lon[i], elev[i], &o, &r).2
            })
        })
        .into_pyarray(py))
}

#[pyfunction]
pub fn block_quantize<'py>(
    py: Python<'py>,
    u: PyReadonlyArray2<'py, f64>,
    q: f64,
    z_base: f64,
) -> Bound<'py, PyArray2<u16>> {
    let u = u.as_array();
    let scale = 1. / q;
    let offset = (z_base * scale).round_ties_even();
    py.detach(|| u.mapv(|v| ((v * scale).round_ties_even() - offset) as u16))
        .into_pyarray(py)
}

#[pyfunction]
pub fn block_mask<'py>(
    py: Python<'py>,
    idx: PyReadonlyArray2<'py, u8>,
    material: u8,
) -> Bound<'py, PyArray2<u8>> {
    let idx = idx.as_array();
    py.detach(|| Array2::from_shape_fn(idx.dim(), |i| if idx[i] == material { 255 } else { 0 }))
        .into_pyarray(py)
}

/// The Python caller owns the staging path and the Windows replace/retry semantics.
#[pyfunction]
#[pyo3(signature=(path, grid, layers, materials, source_ids=None))]
pub fn block_write_ter(
    py: Python<'_>,
    path: PathBuf,
    grid: PyReadonlyArray2<'_, u16>,
    layers: Option<PyReadonlyArray2<'_, u8>>,
    materials: Vec<String>,
    source_ids: Option<Vec<u8>>,
) -> PyResult<()> {
    let grid = grid.as_array();
    let layers = layers.as_ref().map(|v| v.as_array());
    let n = grid.nrows();
    if n == 0
        || n > u32::MAX as usize
        || grid.ncols() != n
        || layers.as_ref().is_some_and(|v| v.dim() != grid.dim())
    {
        return Err(PyValueError::new_err(
            "terrain heights and layers must be matching square grids",
        ));
    }
    if materials.iter().any(|s| s.len() > 255) {
        return Err(PyValueError::new_err(
            "terrain material name exceeds 255 bytes",
        ));
    }
    let mut local_id: [u8; 256] = std::array::from_fn(|i| i as u8);
    if let Some(ids) = source_ids {
        if ids.len() != materials.len() || ids.len() > 255 {
            return Err(PyValueError::new_err(
                "material IDs must match the terrain file listing",
            ));
        }
        local_id.fill(255);
        for (local, id) in ids.into_iter().enumerate() {
            local_id[id as usize] = local as u8;
        }
    }
    py.detach(|| -> std::io::Result<()> {
        let mut f = BufWriter::new(File::create(path)?);
        f.write_all(&[9])?;
        f.write_all(&(n as u32).to_le_bytes())?;
        let mut row = vec![0; 2 * n];
        for heights in grid.rows() {
            for (dst, &v) in row.chunks_exact_mut(2).zip(heights) {
                dst.copy_from_slice(&v.to_le_bytes());
            }
            f.write_all(&row)?;
        }
        let row = &mut row[..n];
        for y in 0..n {
            for (x, v) in row.iter_mut().enumerate() {
                *v = if y == 0 || y + 1 == n || x == 0 || x + 1 == n {
                    255
                } else {
                    layers
                        .as_ref()
                        .map_or(0, |idx| local_id[idx[[y, x]] as usize])
                };
            }
            f.write_all(row)?;
        }
        f.write_all(&(materials.len() as u32).to_le_bytes())?;
        for name in materials {
            f.write_all(&[name.len() as u8])?;
            f.write_all(name.as_bytes())?;
        }
        f.flush()
    })?;
    Ok(())
}

/// Depth and surface level are f32 in the raster. Their difference rounds to f32 before the
/// minimum with the f64 terrain height. None preserves the untouched DEM on dry ground.
#[pyfunction]
pub fn block_carve<'py>(
    py: Python<'py>,
    heights: PyReadonlyArray1<'py, f64>,
    depth: PyReadonlyArray1<'py, f32>,
    level: PyReadonlyArray1<'py, f32>,
) -> PyResult<Option<Bound<'py, numpy::PyArray1<f64>>>> {
    let (z, d, lv) = (heights.as_array(), depth.as_array(), level.as_array());
    if z.len() != d.len() || d.len() != lv.len() {
        return Err(PyValueError::new_err(
            "water and elevation samples must match",
        ));
    }
    let result = py.detach(|| {
        let mut out = None;
        for i in 0..d.len() {
            if d[i] != 0. && lv[i].is_finite() {
                let bed = (lv[i] - d[i]) as f64;
                out.get_or_insert_with(|| z.to_vec())[i] = if z[i].is_nan() || z[i] < bed {
                    z[i]
                } else {
                    bed
                };
            }
        }
        out
    });
    Ok(result.map(|v| v.into_pyarray(py)))
}

/// Build on the worker's calling thread. Provider/cache callbacks must see the same
/// thread-local cancellation and short-input scopes as the request that entered here.
#[pyfunction]
#[pyo3(signature=(height_job, paint_job, holes_job, frame, center, surface, sea, cache, grader,
                  keep, out_dir, name, ter_name, square, bed_depth, worker))]
pub fn build_block<'py>(
    py: Python<'py>,
    height_job: &Bound<'py, PyAny>,
    paint_job: &Bound<'py, PyAny>,
    holes_job: Option<&Bound<'py, PyAny>>,
    frame: &Bound<'py, PyAny>,
    center: (f64, f64),
    surface: Option<&Bound<'py, PyAny>>,
    sea: Option<&Bound<'py, PyAny>>,
    cache: Option<&Bound<'py, PyAny>>,
    grader: Option<&Bound<'py, PyAny>>,
    keep: &Bound<'py, PyAny>,
    out_dir: &Bound<'py, PyAny>,
    name: &str,
    ter_name: Option<&str>,
    square: f64,
    bed_depth: f64,
    worker: bool,
) -> PyResult<Bound<'py, PyAny>> {
    use std::time::Instant;
    let sf = py.import("surface")?;
    let (info, stages): (Bound<'py, PyAny>, Bound<'py, PyAny>) = height_job.call0()?.extract()?;
    let mut idx = py.None().into_bound(py);
    if let Some(surface) = surface {
        let (lat, lon) = (keep.get_item("lat")?, keep.get_item("lon")?);
        let start = Instant::now();
        let (classes, complete): (Bound<'py, PyAny>, bool) = surface
            .call_method1("block_classes", (frame, center.0, center.1, &lat, &lon))?
            .extract()?;
        stages.set_item("classes", start.elapsed().as_secs_f64())?;
        idx = sf.call_method1(
            "classify",
            (classes, keep.get_item("u")?, square, &lat, &lon),
        )?;
        if let Some(sea) = sea {
            let depth = sea.call_method1("depth", (&lat, &lon))?;
            idx.set_item(
                depth.rich_compare(bed_depth, pyo3::basic::CompareOp::Gt)?,
                sf.getattr("MUD")?,
            )?;
        }
        let land = idx.call_method0("copy")?;
        let (roads, ready): (Bound<'py, PyAny>, bool) =
            surface.call_method1("roads", (&lat, &lon))?.extract()?;
        stages.set_item("landcover", start.elapsed().as_secs_f64())?;
        let paint = Instant::now();
        idx = paint_job.call1((idx, roads))?;
        stages.set_item("roadPaint", paint.elapsed().as_secs_f64())?;
        idx = sf.call_method1("fold_layers", (idx, land, square, &stages))?;
        let stem = name
            .chars()
            .take(name.chars().count().saturating_sub(4))
            .collect::<String>();
        info.set_item(
            "layers",
            sf.call_method1("write_masks", (&idx, out_dir, stem, true))?,
        )?;
        info.set_item("surfaceComplete", complete && ready)?;
        info.set_item("roadsComplete", ready)?;
    }
    if let Some(ter) = ter_name {
        let start = Instant::now();
        let pre = if cache.is_some() && !idx.is_none() {
            idx.call_method0("copy")?
        } else {
            py.None().into_bound(py)
        };
        if let Some(holes) = holes_job {
            idx = holes.call1((idx, &info))?;
        }
        let (written, mats): (Bound<'py, PyAny>, Bound<'py, PyAny>) = sf
            .call_method1("write_ter", (out_dir, ter, keep.get_item("qgrid")?, &idx))?
            .extract()?;
        info.set_item("ter", written)?;
        if !mats.is_none() {
            info.set_item("mats", mats)?;
        }
        if worker {
            stages.set_item("ter", start.elapsed().as_secs_f64())?;
        }
        if let Some(cache) = cache {
            let start = Instant::now();
            cache.call_method1("finish", (&info, keep, pre, idx, grader))?;
            stages.set_item("blockCache", start.elapsed().as_secs_f64())?;
            stages.set_item(
                "chunksRead",
                cache.getattr("disk")?.getattr("read")?.extract::<f64>()?,
            )?;
        }
    }
    info.set_item("stages", stages)?;
    Ok(info)
}

#[pyfunction]
pub fn block_params(
    lo: f64,
    hi: f64,
    q: f64,
    ladder: Vec<f64>,
    step: f64,
    margin: f64,
) -> PyResult<(f64, f64, f64)> {
    let last = ladder
        .last()
        .copied()
        .ok_or_else(|| PyValueError::new_err("empty terrain quantum ladder"))?;
    if !lo.is_finite() {
        return Err(PyValueError::new_err("terrain minimum must be finite"));
    }
    let base = ((lo - margin) / step).floor() * step;
    let q = ladder
        .into_iter()
        .find(|&v| v >= q && hi - base <= 65534. * v)
        .unwrap_or(last);
    Ok((q, base, q * 65536.))
}

#[pyfunction]
pub fn block_height_at(
    grid: &Bound<'_, PyUntypedArray>,
    base: f64,
    max_height: f64,
    center: (f64, f64),
    point: (f64, f64),
    square: f64,
) -> PyResult<Option<f64>> {
    let shape = grid.shape();
    if shape.len() != 2 || shape[0] == 0 || shape[0] != shape[1] {
        return Err(PyValueError::new_err("invalid terrain grid"));
    }
    let size = shape[0];
    let half = size as f64 * square / 2.;
    let (fx, fy) = (
        (point.0 - (center.0 - half)) / square,
        (point.1 - (center.1 - half)) / square,
    );
    let n = size - 1;
    if !(0. <= fx && fx <= n as f64 && 0. <= fy && fy <= n as f64) {
        return Ok(None);
    }
    if size < 2 {
        return Err(pyo3::exceptions::PyIndexError::new_err(
            "terrain interpolation needs two samples",
        ));
    }
    let (ix, iy) = ((fx as usize).min(n - 1), (fy as usize).min(n - 1));
    let (wx, wy) = (fx - ix as f64, fy - iy as f64);
    let h = |y: usize, x: usize| -> PyResult<f64> {
        Ok(base + grid.get_item((y, x))?.extract::<f64>()? * (max_height / 65536.))
    };
    Ok(Some(
        (h(iy, ix)? * (1. - wx) + h(iy, ix + 1)? * wx) * (1. - wy)
            + (h(iy + 1, ix)? * (1. - wx) + h(iy + 1, ix + 1)? * wx) * wy,
    ))
}
