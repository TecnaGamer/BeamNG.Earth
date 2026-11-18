//! dem.Dem.sample after its zoom check, in one call: the points' Web-Mercator pixel coordinates, the polar caps handed
//! to polardem, the box of tiles they fall in (split into groups of nearby tiles when it is too large or the points are
//! sparse in it), the tiles whose pixels the bilinear reads (only those are asked for: a failed download of a box tile
//! no point reads makes no build short), and the bilinear itself on the tiles in place.
//!
//! The Python glue keeps the tile store (Dem.tiles: the LRU, downloads, stand-ins and their marks) and the polar DEM:
//! `fetch(keys)` returns the (tile, tile) float32 heights of the (zoom, x, y) keys in their order, `polar(lat, lon)`
//! the float64 heights of the points at or past `from_lat` (None: sea level there). Contract: lat and lon are 1-D
//! float64 of one length; the result is 1-D float64 of that length. A longitude that is not finite raises ValueError
//! (as the Python's math.floor did); so does one so far out that its pixel passes 2^53 (|lon| beyond ~10^11 degrees at
//! zoom 16: no tile arithmetic is exact there).
//!
//! Every value is numpy's: the same operations in the same order (radians as x * (pi / 180), tan and asinh from the
//! libm crate as dnp's, round half to even as np.round), no fused multiply-add.

use crate::dm::Dm;
use numpy::ndarray::{Array1, ArrayView1, ArrayView2};
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::exceptions::{PyRecursionError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyList, PyTuple};

/// Pixels at or past this are refused: floats there are all integers, a tile's offset is not exact.
const PIXEL_LIMIT: f64 = 9007199254740992.0; // 2^53
/// Group splits nested deeper than this raise RecursionError (the Python recursed; the splits end within 2 or 3).
const MAX_DEPTH: u32 = 64;

/// The bilinear of dem.sample (bilinear_grid's clamps and arithmetic) on a mosaic of ny x nx tiles (row-major), each
/// corner pixel read from its own tile. Err(()) when a pixel of a tile that is None is read (a tile left out of the
/// read mask: never, by construction).
pub(crate) fn bilinear_mosaic(ts: &[Option<ArrayView2<'_, f32>>], nx: usize, tile: usize, xs: ArrayView1<'_, f64>,
                              ys: ArrayView1<'_, f64>) -> Result<Array1<f64>, ()> {
    let ny = ts.len() / nx;
    let (h, w) = (ny * tile, nx * tile);
    let mut bad = false;
    let px = |y: usize, x: usize, bad: &mut bool| match &ts[(y / tile) * nx + x / tile] {
        Some(t) => t[[y % tile, x % tile]] as f64,
        None => {
            *bad = true;
            0.0
        }
    };
    let out = Array1::from_shape_fn(xs.len(), |i| {
        let (x, y) = (xs[i], ys[i]);
        let ix = (x.floor() as i64).clamp(0, w as i64 - 2) as usize;
        let iy = (y.floor() as i64).clamp(0, h as i64 - 2) as usize;
        let wx = (x - ix as f64).clamp(0.0, 1.0);
        let wy = (y - iy as f64).clamp(0.0, 1.0);
        let (v00, v10) = (px(iy, ix, &mut bad), px(iy, ix + 1, &mut bad));
        let (v01, v11) = (px(iy + 1, ix, &mut bad), px(iy + 1, ix + 1, &mut bad));
        (v00 * (1.0 - wx) + v10 * wx) * (1.0 - wy) + (v01 * (1.0 - wx) + v11 * wx) * wy
    });
    if bad { Err(()) } else { Ok(out) }
}

struct Cfg {
    zoom: u32,
    tile: i64,
    max_tiles: i64,
    sparse_min: i64,
    sparse_ratio: i64,
    sparse_group: i64,
}

fn isqrt(v: i64) -> i64 {
    let mut r = 0i64;
    while (r + 1) * (r + 1) <= v {
        r += 1;
    }
    r
}

/// obj as a read-only array, copied first (by numpy) when its data are not aligned: a view into a packed buffer may
/// be, and the views read here need aligned elements.
fn aligned_f64<'py>(obj: &Bound<'py, PyAny>) -> PyResult<PyReadonlyArray1<'py, f64>> {
    let a = obj.extract::<PyReadonlyArray1<'py, f64>>()?;
    if a.is_aligned() {
        return Ok(a);
    }
    drop(a);
    obj.call_method0("copy")?.extract::<PyReadonlyArray1<'py, f64>>()
}

fn aligned_tile<'py>(obj: &Bound<'py, PyAny>) -> PyResult<PyReadonlyArray2<'py, f32>> {
    let a = obj.extract::<PyReadonlyArray2<'py, f32>>()?;
    if a.is_aligned() {
        return Ok(a);
    }
    drop(a);
    obj.call_method0("copy")?.extract::<PyReadonlyArray2<'py, f32>>()
}

#[inline]
fn floor_i(v: f64) -> i64 {
    v.floor() as i64
}

/// The heights of points that are all inside the caps (|lat| < from_lat), each call of the Python's sample on them.
fn sample_box(cfg: &Cfg, lat: &[f64], lon: &[f64], fetch: &Bound<'_, PyAny>, depth: u32) -> PyResult<Vec<f64>> {
    if lat.is_empty() {
        return Ok(Vec::new());
    }
    let py = fetch.py();
    let n: i64 = 1i64 << cfg.zoom;
    let t = cfg.tile;
    let w = (n * t) as f64;
    // Global pixel coordinates; pixel centres sit at +0.5. Unwrapped across the antimeridian by the first point.
    let fx0 = (lon[0] / 360.0 + 0.5) * w;
    let deg = std::f64::consts::PI / 180.0;
    let tau = 2.0 * std::f64::consts::PI;
    let mut fx: Vec<f64> = lon.iter().map(|&l| {
        let f = (l / 360.0 + 0.5) * w;
        f - w * ((f - fx0) / w).round_ties_even() - 0.5
    }).collect();
    let mut fy: Vec<f64> = lat.iter().map(|&a| (0.5 - (a * deg).dtan().dasinh() / tau) * w - 0.5).collect();
    let (mut xmin, mut xmax, mut ymin, mut ymax) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
    for (&x, &y) in fx.iter().zip(&fy) {
        if !(x.is_finite() && y.is_finite()) {
            return Err(PyValueError::new_err("cannot convert float NaN to integer"));
        }
        if x.abs() >= PIXEL_LIMIT || y.abs() >= PIXEL_LIMIT {
            return Err(PyValueError::new_err("dem.sample: a longitude too far out for the tile arithmetic"));
        }
        xmin = xmin.min(x);
        xmax = xmax.max(x);
        ymin = ymin.min(y);
        ymax = ymax.max(y);
    }
    let tx0 = floor_i(xmin).div_euclid(t);
    let tx1 = (floor_i(xmax) + 1).div_euclid(t);
    let ty0 = floor_i(ymin).div_euclid(t).max(0);
    let ty1 = (floor_i(ymax) + 1).div_euclid(t).min(n - 1);
    let (nx, ny) = (tx1 - tx0 + 1, ty1 - ty0 + 1);
    let boxn = nx.saturating_mul(ny);
    let mut sparse = false;
    if cfg.sparse_min < boxn && boxn <= cfg.max_tiles {
        // points along a line (a road's profile) in a large box: only the tiles near points are asked
        let mut tid: Vec<i64> = fx.iter().zip(&fy)
            .map(|(&x, &y)| (floor_i(x).div_euclid(t) - tx0) * ny + (floor_i(y).div_euclid(t) - ty0))
            .collect();
        tid.sort_unstable();
        tid.dedup();
        sparse = boxn > cfg.sparse_ratio.saturating_mul(tid.len() as i64);
    }
    if boxn > cfg.max_tiles || sparse {
        // spread-out points: groups of nearby tiles sampled separately (the same values), in the order of their
        // (column, row) group, each as a sample of its own points in their order
        if depth >= MAX_DEPTH {
            return Err(PyRecursionError::new_err("dem.sample: maximum recursion depth exceeded"));
        }
        let g = if sparse { cfg.sparse_group } else { (isqrt(cfg.max_tiles) - 1).max(1) };
        if g <= 0 {
            return Err(PyValueError::new_err("dem_sample: sparse_group must be positive"));
        }
        let mut order: Vec<(i64, i64, usize)> = fx.iter().zip(&fy).enumerate()
            .map(|(i, (&x, &y))| (floor_i(x).div_euclid(t).div_euclid(g), floor_i(y).div_euclid(t).div_euclid(g), i))
            .collect();
        order.sort_unstable();
        let mut out = vec![0.0f64; lat.len()];
        let mut s = 0;
        while s < order.len() {
            let mut e = s + 1;
            while e < order.len() && order[e].0 == order[s].0 && order[e].1 == order[s].1 {
                e += 1;
            }
            let idx: Vec<usize> = order[s..e].iter().map(|o| o.2).collect();
            let sla: Vec<f64> = idx.iter().map(|&i| lat[i]).collect();
            let slo: Vec<f64> = idx.iter().map(|&i| lon[i]).collect();
            let sub = sample_box(cfg, &sla, &slo, fetch, depth + 1)?;
            for (&i, v) in idx.iter().zip(sub) {
                out[i] = v;
            }
            s = e;
        }
        return Ok(out);
    }
    if nx <= 0 || ny <= 0 {
        return Err(PyValueError::new_err("bilinear_tiles: tiles must fill ny x nx"));
    }
    let (ox, oy) = ((tx0 * t) as f64, (ty0 * t) as f64);
    for v in fx.iter_mut() {
        *v -= ox;
    }
    for v in fy.iter_mut() {
        *v -= oy;
    }
    // The tiles whose pixels the bilinear reads: each point's pixel (floor, clamped to the box as the kernel clamps
    // it) and the next one right and down, whatever their weights.
    let (nxu, nyu, tu) = (nx as usize, ny as usize, t as usize);
    let (wpx, hpx) = (nx * t, ny * t);
    let mut read = vec![false; nxu * nyu];
    for (&x, &y) in fx.iter().zip(&fy) {
        let ix = floor_i(x).clamp(0, wpx - 2);
        let iy = floor_i(y).clamp(0, hpx - 2);
        for dx in 0..2 {
            for dy in 0..2 {
                read[((iy + dy) / t) as usize * nxu + ((ix + dx) / t) as usize] = true;
            }
        }
    }
    let keys = PyList::empty(py);
    for ty in ty0..=ty1 {
        for tx in tx0..=tx1 {
            if read[(ty - ty0) as usize * nxu + (tx - tx0) as usize] {
                keys.append(PyTuple::new(py, [cfg.zoom as i64, tx.rem_euclid(n), ty])?)?;
            }
        }
    }
    let nkeys = keys.len();
    let got = fetch.call1((keys,))?;
    let arrs: Vec<PyReadonlyArray2<'_, f32>> = got.try_iter()?
        .map(|a| a.and_then(|a| aligned_tile(&a)))
        .collect::<PyResult<_>>()?;
    if arrs.len() != nkeys {
        return Err(PyValueError::new_err("dem_sample: fetch returned another number of tiles than asked"));
    }
    if arrs.iter().any(|a| a.shape() != [tu, tu]) {
        return Err(PyValueError::new_err("bilinear_tiles: every tile must be tile x tile"));
    }
    let mut it = arrs.iter();
    let ts: Vec<Option<ArrayView2<'_, f32>>> =
        read.iter().map(|&r| if r { it.next().map(|a| a.as_array()) } else { None }).collect();
    // (with the GIL held: no Python thread can write a tile while it is read; ~10 us for a call's ~1,000 points)
    let out = bilinear_mosaic(&ts, nxu, tu, ArrayView1::from(&fx[..]), ArrayView1::from(&fy[..]))
        .map_err(|_| PyValueError::new_err("dem_sample: a pixel outside the tiles read"))?;
    Ok(out.to_vec())
}

/// dem.Dem.sample's heights (m, float64) at lat / lon (1-D float64, one length) from zoom-level tiles of tile x tile
/// pixels: fetch(keys) -> the tiles' float32 heights in the keys' order, polar(lat, lon) -> float64 heights of the points
/// with |lat| >= from_lat (or None: 0). The box limits: dem.MAX_TILES_PER_SAMPLE, SPARSE_MIN_TILES, SPARSE_RATIO,
/// SPARSE_GROUP. The module docs give the rest.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (lat, lon, zoom, tile, from_lat, max_tiles, sparse_min, sparse_ratio, sparse_group, fetch, polar))]
pub fn dem_sample<'py>(py: Python<'py>, lat: Bound<'py, PyAny>, lon: Bound<'py, PyAny>, zoom: u32,
                       tile: i64, from_lat: f64, max_tiles: i64, sparse_min: i64, sparse_ratio: i64, sparse_group: i64,
                       fetch: Bound<'py, PyAny>, polar: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyArray1<f64>>> {
    if zoom > 30 || !(2..=1 << 20).contains(&tile) || !(1..=1 << 20).contains(&max_tiles) {
        return Err(PyValueError::new_err("dem_sample: zoom up to 30, tiles of 2 to 2^20 pixels, up to 2^20 a box"));
    }
    // (the coordinates copied out: no borrow of the caller's arrays is held while Python runs a callback)
    let read = |lat: &Bound<'py, PyAny>, lon: &Bound<'py, PyAny>| -> PyResult<(Vec<f64>, Vec<f64>)> {
        let (a, b) = (aligned_f64(lat)?, aligned_f64(lon)?);
        Ok((a.as_array().to_vec(), b.as_array().to_vec()))
    };
    let (mut la, mut lo) = read(&lat, &lon)?;
    if la.len() != lo.len() {
        return Err(PyValueError::new_err("dem_sample: lat and lon differ in length"));
    }
    let cfg = Cfg { zoom, tile, max_tiles, sparse_min, sparse_ratio, sparse_group };
    let mut out = vec![0.0f64; la.len()];
    let valid: Vec<bool> = la.iter().map(|&a| a.abs() < from_lat).collect();
    let nvalid = valid.iter().filter(|&&v| v).count();
    if nvalid < la.len() {
        // the polar caps: the Copernicus DEM (polardem.py)
        let pla: Vec<f64> = la.iter().zip(&valid).filter(|p| !*p.1).map(|p| *p.0).collect();
        let plo: Vec<f64> = lo.iter().zip(&valid).filter(|p| !*p.1).map(|p| *p.0).collect();
        let k = pla.len();
        let r = polar.call1((PyArray1::from_vec(py, pla), PyArray1::from_vec(py, plo)))?;
        if nvalid > 0 {
            // (the other points read again, as the Python took them after the polar DEM: by the first mask)
            (la, lo) = read(&lat, &lon)?;
            if la.len() != valid.len() || lo.len() != valid.len() {
                return Err(PyValueError::new_err("dem_sample: lat / lon changed length"));
            }
        }
        if !r.is_none() {
            let r = aligned_f64(&r)?;
            let rv = r.as_array();
            if rv.len() != k {
                return Err(PyValueError::new_err("dem_sample: polar returned another number of heights"));
            }
            let mut j = 0;
            for (o, &v) in out.iter_mut().zip(&valid) {
                if !v {
                    *o = rv[j];
                    j += 1;
                }
            }
        }
    }
    if nvalid == la.len() && nvalid > 0 {
        out = sample_box(&cfg, &la, &lo, &fetch, 0)?;
    } else if nvalid > 0 {
        let vla: Vec<f64> = la.iter().zip(&valid).filter(|p| *p.1).map(|p| *p.0).collect();
        let vlo: Vec<f64> = lo.iter().zip(&valid).filter(|p| *p.1).map(|p| *p.0).collect();
        let sub = sample_box(&cfg, &vla, &vlo, &fetch, 0)?;
        let mut j = 0;
        for (o, &v) in out.iter_mut().zip(&valid) {
            if v {
                *o = sub[j];
                j += 1;
            }
        }
    }
    Ok(out.into_pyarray(py))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(dem_sample, m)?)?;
    Ok(())
}
