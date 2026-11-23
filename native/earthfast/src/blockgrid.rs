//! Assemble one block from memory/disk chunks. Preserve the sampler's batch shape:
//! a complete miss calls it on a 2D block; a partial miss on a flat list of whole chunks.
use numpy::{
    ndarray::{s, Array2, ArrayView2},
    IntoPyArray, PyArray2, PyArrayMethods, PyReadonlyArray1, PyReadonlyArray2,
};
use pyo3::{exceptions::PyValueError, prelude::*, types::PyDict};

#[derive(Clone, Copy)]
struct Chunk {
    x: usize,
    y: usize,
    key: (i64, i64),
    side: usize,
}

impl Chunk {
    fn area(
        self,
    ) -> numpy::ndarray::SliceInfo<[numpy::ndarray::SliceInfoElem; 2], numpy::ndarray::Ix2, numpy::ndarray::Ix2>
    {
        let (x, y, s) = (self.x, self.y, self.side);
        s![y..y + s, x..x + s]
    }

    fn paste(self, dst: &mut Array2<f64>, src: &Bound<'_, PyAny>) -> PyResult<()> {
        let src: PyReadonlyArray2<'_, f64> = src.extract()?;
        if src.as_array().dim() != (self.side, self.side) {
            return Err(PyValueError::new_err("terrain cache chunk has the wrong shape"));
        }
        dst.slice_mut(self.area()).assign(&src.as_array());
        Ok(())
    }

    fn take(self, full: ArrayView2<'_, f64>) -> Array2<f64> {
        full.slice(self.area()).to_owned()
    }
}

#[pyfunction]
#[pyo3(signature=(origin, rot, xs, ys, first, side, cache, disk, keep, elev_fn, projection=None))]
pub fn block_chunk_grid<'py>(
    py: Python<'py>,
    origin: Option<PyReadonlyArray1<'py, f64>>,
    rot: Option<PyReadonlyArray2<'py, f64>>,
    xs: PyReadonlyArray1<'py, f64>,
    ys: PyReadonlyArray1<'py, f64>,
    first: (i64, i64),
    side: usize,
    cache: &Bound<'py, PyAny>,
    disk: Option<&Bound<'py, PyAny>>,
    keep: Option<&Bound<'py, PyDict>>,
    elev_fn: &Bound<'py, PyAny>,
    projection: Option<&Bound<'py, PyAny>>,
) -> PyResult<(Bound<'py, PyArray2<f64>>, f64)> {
    let tangent = match (&origin, &rot) {
        (Some(o), Some(r)) => Some(crate::frame_of(o, r)?),
        _ if projection.is_some() => None,
        _ => {
            return Err(PyValueError::new_err(
                "a terrain frame or a custom projection is required",
            ))
        }
    };
    let (xs, ys) = (xs.as_array(), ys.as_array());
    let n = xs.len();
    if n == 0 || ys.len() != n || side == 0 || n % side != 0 {
        return Err(PyValueError::new_err("terrain grid must contain whole square chunks"));
    }
    let mut u = Array2::zeros((n, n));
    let mut lat = Array2::zeros((n, n));
    let mut lon = Array2::zeros((n, n));
    let mut missing = Vec::new();
    let mut need_geo = Vec::new();
    let get_geo = keep.is_some() && cache.hasattr("get_geo")?;
    for y in (0..n).step_by(side) {
        for x in (0..n).step_by(side) {
            let key = (first.0 + (x / side) as i64, first.1 + (y / side) as i64);
            let c = Chunk { x, y, key, side };
            let hit = cache.call_method1("get", (key,))?;
            if hit.is_none() {
                missing.push(c);
                need_geo.push(c);
                continue;
            }
            c.paste(&mut u, &hit)?;
            if keep.is_some() {
                if get_geo {
                    let geo = cache.call_method1("get_geo", (key,))?;
                    if !geo.is_none() {
                        c.paste(&mut lat, &geo.get_item(0)?)?;
                        c.paste(&mut lon, &geo.get_item(1)?)?;
                        continue;
                    }
                }
                need_geo.push(c);
            }
        }
    }
    if let Some(disk) = disk.filter(|_| !missing.is_empty()) {
        let keys: Vec<_> = missing.iter().map(|c| c.key).collect();
        let hits = disk.call_method1("load", (keys,))?;
        let mut rest = Vec::new();
        for c in missing {
            if hits.contains(c.key)? {
                let hit = hits.get_item(c.key)?;
                c.paste(&mut u, &hit)?;
                cache.call_method1("put", (c.key, hit))?;
            } else {
                rest.push(c);
            }
        }
        missing = rest;
    }
    let full = missing.len() == (n / side) * (n / side);
    if let Some((o, r)) = &tangent {
        py.detach(|| {
            for c in &need_geo {
                for y in c.y..c.y + side {
                    for x in c.x..c.x + side {
                        let (la, lo, _) = crate::e2g_one(xs[x], ys[y], 0., o, r);
                        lat[[y, x]] = la;
                        lon[[y, x]] = lo;
                    }
                }
            }
        });
    } else if !need_geo.is_empty() {
        // Override frames (including geotiles.WorldFrame) define their own inverse.
        // Keep placement and grouping in the cache builder, and ask the frame for just those points.
        let mut east = Vec::new();
        let mut north = Vec::new();
        for c in &need_geo {
            for y in c.y..c.y + side {
                for x in c.x..c.x + side {
                    east.push(xs[x]);
                    north.push(ys[y]);
                }
            }
        }
        let frame = projection.unwrap();
        let points = frame.call_method1("enu_to_geodetic", (east.into_pyarray(py), north.into_pyarray(py), 0.))?;
        let np = py.import("numpy")?;
        let coords = |i: usize| -> PyResult<PyReadonlyArray1<'_, f64>> {
            np.call_method1("asarray", (points.get_item(i)?, "float64"))?
                .call_method0("ravel")?
                .extract()
        };
        let (la, lo) = (coords(0)?, coords(1)?);
        let (la, lo) = (la.as_array(), lo.as_array());
        if la.len() != need_geo.len() * side * side || lo.len() != la.len() {
            return Err(PyValueError::new_err("frame returned the wrong number of coordinates"));
        }
        for (j, c) in need_geo.iter().enumerate() {
            for y in 0..side {
                for x in 0..side {
                    let i = (j * side + y) * side + x;
                    lat[[c.y + y, c.x + x]] = la[i];
                    lon[[c.y + y, c.x + x]] = lo[i];
                }
            }
        }
    }
    if !full {
        for c in &need_geo {
            // The get also promotes a disk hit in the LRU, even without put_geo.
            if !cache.call_method1("get", (c.key,))?.is_none() && cache.hasattr("put_geo")? {
                let geo = (c.take(lat.view()).into_pyarray(py), c.take(lon.view()).into_pyarray(py));
                cache.call_method1("put_geo", (c.key, geo))?;
            }
        }
    }
    let share = missing.len() as f64 / ((n / side) * (n / side)) as f64;
    let (lat, lon) = (lat.into_pyarray(py), lon.into_pyarray(py));
    if !missing.is_empty() {
        let (la, lo) = if full {
            (lat.clone(), lon.clone())
        } else {
            let gather = |a: &Bound<'py, PyArray2<f64>>| {
                let a = a.readonly();
                let mut v = Vec::with_capacity(missing.len() * side * side);
                for c in &missing {
                    v.extend(a.as_array().slice(c.area()).iter().copied());
                }
                Array2::from_shape_vec((1, v.len()), v).unwrap().into_pyarray(py)
            };
            (gather(&lat), gather(&lon))
        };
        let z = if full {
            elev_fn.call1((&la, &lo))?
        } else {
            elev_fn.call1((la.reshape([la.len()])?, lo.reshape([lo.len()])?))?
        };
        // Providers return either f32 or f64. Cast before converting geodetic to ENU.
        let np = py.import("numpy")?;
        let z = np
            .call_method1("asarray", (z, "float64"))?
            .call_method1("reshape", (la.shape().to_vec(),))?;
        let projected = if let Some(frame) = projection {
            let points = if full {
                frame.call_method1("geodetic_to_enu", (&la, &lo, &z))?
            } else {
                frame.call_method1(
                    "geodetic_to_enu",
                    (
                        la.reshape([la.len()])?,
                        lo.reshape([lo.len()])?,
                        z.call_method0("ravel")?,
                    ),
                )?
            };
            let a = np
                .call_method1("asarray", (points.get_item(2)?, "float64"))?
                .call_method1("reshape", (la.shape().to_vec(),))?;
            Some(a.extract::<PyReadonlyArray2<'_, f64>>()?)
        } else {
            None
        };
        let projected = projected.as_ref().map(|a| a.as_array());
        let z: PyReadonlyArray2<'_, f64> = z.extract()?;
        let (la, lo) = (la.readonly(), lo.readonly());
        let (la, lo, z) = (la.as_array(), lo.as_array(), z.as_array());
        py.detach(|| {
            for (j, c) in missing.iter().enumerate() {
                for dy in 0..side {
                    for dx in 0..side {
                        let dst = [c.y + dy, c.x + dx];
                        let src = if full {
                            dst
                        } else {
                            [0, j * side * side + dy * side + dx]
                        };
                        u[dst] = match &tangent {
                            Some((o, r)) => crate::g2e_one(la[src], lo[src], z[src], o, r).2,
                            None => projected.unwrap()[src],
                        };
                    }
                }
            }
        });
        for c in &missing {
            let chunk = c.take(u.view()).into_pyarray(py);
            let geo = (
                c.take(lat.readonly().as_array()).into_pyarray(py),
                c.take(lon.readonly().as_array()).into_pyarray(py),
            );
            cache.call_method1("put", (c.key, &chunk, geo))?;
            if let Some(disk) = disk {
                // Keep a separate snapshot: a cache consumer may modify its own chunk.
                disk.getattr("computed")?
                    .call_method1("append", ((c.key, c.take(u.view()).into_pyarray(py)),))?;
            }
        }
    }
    if let Some(keep) = keep {
        keep.set_item("lat", lat)?;
        keep.set_item("lon", lon)?;
    }
    Ok((u.into_pyarray(py), share))
}

use numpy::PyUntypedArrayMethods;

/// Reopen the published block using only clean chunks. Keep any disk hits in the
/// memory LRU even when a later chunk is absent and a build will follow.
#[pyfunction]
#[pyo3(signature=(n, side, first, cache, disk))]
pub fn block_cached_grid<'py>(
    py: Python<'py>,
    n: usize,
    side: usize,
    first: (i64, i64),
    cache: Option<&Bound<'py, PyAny>>,
    disk: Option<&Bound<'py, PyAny>>,
) -> PyResult<Option<Bound<'py, PyArray2<f64>>>> {
    if side == 0 || n % side != 0 {
        return Err(PyValueError::new_err("invalid terrain chunk grid"));
    }
    let mut u = Array2::zeros((n, n));
    let mut missing = Vec::new();
    for y in (0..n).step_by(side) {
        for x in (0..n).step_by(side) {
            let key = (first.0 + (x / side) as i64, first.1 + (y / side) as i64);
            let c = Chunk { x, y, key, side };
            if let Some(cache) = cache {
                if !cache.call_method1("any_dirty", ((key,),))?.is_truthy()? {
                    let got = cache.call_method1("get", (key,))?;
                    if !got.is_none() {
                        c.paste(&mut u, &got)?;
                        continue;
                    }
                }
            }
            missing.push(c);
        }
    }
    if !missing.is_empty() {
        let Some(disk) = disk else { return Ok(None) };
        let keys: Vec<_> = missing.iter().map(|c| c.key).collect();
        let hits = disk.call_method1("load", (keys,))?;
        let mut complete = true;
        for c in missing {
            if hits.contains(c.key)? {
                let got = hits.get_item(c.key)?;
                c.paste(&mut u, &got)?;
                if let Some(cache) = cache {
                    cache.call_method1("put", (c.key, got))?;
                }
            } else {
                complete = false;
            }
        }
        if !complete {
            return Ok(None);
        }
    }
    Ok(Some(u.into_pyarray(py)))
}
