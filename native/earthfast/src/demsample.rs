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
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray2, PyUntypedArrayMethods};
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
    from_lat: f64,
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

#[inline]
fn floor_i(v: f64) -> i64 {
    v.floor() as i64
}

/// Work on this many points or more runs with the GIL released (below it, releasing costs about what the work does).
const DETACH_MIN: usize = 4096;
/// A box's tiles are copied out (and the bilinear run without the GIL) when it has at least this many points per tile
/// read (the copy then costs at most about what the bilinear does) and at least DETACH_MIN: a call below holds the
/// GIL for well under a millisecond (deshadow's padded 1250 x 1250 grid: ~1.6 M points on a few tiles, copied).
const SNAPSHOT_POINTS_PER_TILE: usize = 1024;
/// ... and when the copies take at most this many bytes (by the tiles' area: a box of many tiles, or a huge tile that
/// is one broadcast scalar, is read in place with the GIL held instead).
const SNAPSHOT_MAX_BYTES: usize = 64 << 20;

/// Whether the tiles of a box are copied out for a bilinear without the GIL (see the constants above).
fn snapshot(points: usize, tiles: usize, tile: usize) -> bool {
    let bytes = tile.checked_mul(tile).and_then(|a| a.checked_mul(4)).and_then(|b| b.checked_mul(tiles));
    points >= DETACH_MIN && points >= SNAPSHOT_POINTS_PER_TILE.saturating_mul(tiles)
        && bytes.is_some_and(|b| b <= SNAPSHOT_MAX_BYTES)
}

static BORROW_API: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// rust-numpy's shared borrow checking set up (its first borrow imports numpy's multiarray module and looks its capsule
/// up: Python that could run hooks), on a private array, before any caller array is checked and borrowed.
fn borrow_api_ready(py: Python<'_>) -> PyResult<()> {
    use numpy::PyArrayMethods;
    use std::sync::atomic::Ordering;
    if !BORROW_API.load(Ordering::Acquire) {
        let a = PyArray1::<f64>::zeros(py, 1, false);
        drop(a.try_readonly().map_err(|e| PyValueError::new_err(format!("dem_sample: {e}")))?);
        BORROW_API.store(true, Ordering::Release);
    }
    Ok(())
}

/// f() with the GIL released when n points make it worth it (f touches no Python object).
fn gil_free<T: Send>(py: Python<'_>, n: usize, f: impl FnOnce() -> T + Send) -> T {
    if n >= DETACH_MIN { py.detach(f) } else { f() }
}

/// obj as a numpy array of T, D (a type check: no borrow taken), copied first when its data are not aligned (a view
/// into a packed buffer may not be; the views read here need aligned elements) by numpy.array (a base-class copy: no
/// subclass's own copy method).
fn normalised<'py, T: numpy::Element, D: numpy::ndarray::Dimension>(obj: Bound<'py, PyAny>)
    -> PyResult<Bound<'py, numpy::PyArray<T, D>>> {
    let a = obj.cast_into::<numpy::PyArray<T, D>>()?;
    if a.is_aligned() {
        return Ok(a);
    }
    let py = a.py();
    let kw = pyo3::types::PyDict::new(py);
    kw.set_item("copy", true)?;
    Ok(py.import("numpy")?.getattr("array")?.call((a,), Some(&kw))?.cast_into::<numpy::PyArray<T, D>>()?)
}

/// A read-only borrow of a normalised array, taken after every Python step and released before the next: its type,
/// rank and alignment checked again first (Python run since its normalising could have changed them).
fn borrowed<'py, T: numpy::Element, D: numpy::ndarray::Dimension>(a: &Bound<'py, numpy::PyArray<T, D>>)
    -> PyResult<numpy::PyReadonlyArray<'py, T, D>> {
    use numpy::PyArrayMethods;
    a.as_any().cast::<numpy::PyArray<T, D>>()?;
    if !a.is_aligned() {
        return Err(PyValueError::new_err("dem_sample: an array's data are not aligned"));
    }
    a.try_readonly().map_err(|e| PyValueError::new_err(format!("dem_sample: {e}")))
}

/// The coordinates of a 1-D float64 array, copied into Rust (no borrow left behind).
fn coords(obj: &Bound<'_, PyAny>) -> PyResult<Vec<f64>> {
    let a = normalised::<f64, numpy::Ix1>(obj.clone())?;
    let r = borrowed(&a)?;
    Ok(r.as_array().to_vec())
}

/// polar(lat, lon) of the points outside `valid` (the polar caps: the Copernicus DEM, polardem.py), called; its result
/// (None: sea level) is put in by `put` once the caller has read what it reads after the call.
fn polar_call<'py>(polar: &Bound<'py, PyAny>, la: &[f64], lo: &[f64], valid: &[bool]) -> PyResult<Bound<'py, PyAny>> {
    let py = polar.py();
    let pla: Vec<f64> = la.iter().zip(valid).filter(|p| !*p.1).map(|p| *p.0).collect();
    let plo: Vec<f64> = lo.iter().zip(valid).filter(|p| !*p.1).map(|p| *p.0).collect();
    polar.call1((PyArray1::from_vec(py, pla), PyArray1::from_vec(py, plo)))
}

fn polar_put(r: &Bound<'_, PyAny>, valid: &[bool], out: &mut [f64]) -> PyResult<()> {
    if r.is_none() {
        return Ok(());
    }
    let rv = coords(r)?;
    if rv.len() != valid.iter().filter(|&&v| !v).count() {
        return Err(PyValueError::new_err("dem_sample: polar returned another number of heights"));
    }
    let mut j = 0;
    for (o, &v) in out.iter_mut().zip(valid) {
        if !v {
            *o = rv[j];
            j += 1;
        }
    }
    Ok(())
}

/// out's points in `valid` set from the box sample of those points of la / lo.
#[allow(clippy::too_many_arguments)]
fn put_valid(cfg: &Cfg, la: &[f64], lo: &[f64], valid: &[bool], out: &mut [f64], fetch: &Bound<'_, PyAny>,
             polar: &Bound<'_, PyAny>, depth: u32) -> PyResult<()> {
    let vla: Vec<f64> = la.iter().zip(valid).filter(|p| *p.1).map(|p| *p.0).collect();
    let vlo: Vec<f64> = lo.iter().zip(valid).filter(|p| *p.1).map(|p| *p.0).collect();
    let sub = sample_box(cfg, &vla, &vlo, fetch, polar, depth)?;
    let mut j = 0;
    for (o, &v) in out.iter_mut().zip(valid) {
        if v {
            *o = sub[j];
            j += 1;
        }
    }
    Ok(())
}

/// The Python's whole sample of Rust-owned points (a split group's: the polar caps looked at again, as its recursive
/// call did).
fn sample_points(cfg: &Cfg, la: &[f64], lo: &[f64], fetch: &Bound<'_, PyAny>, polar: &Bound<'_, PyAny>, depth: u32)
    -> PyResult<Vec<f64>> {
    let np = la.len();
    let valid: Vec<bool> = gil_free(fetch.py(), np, || la.iter().map(|&a| a.abs() < cfg.from_lat).collect());
    let nvalid = valid.iter().filter(|&&v| v).count();
    if nvalid == np {
        return sample_box(cfg, la, lo, fetch, polar, depth);
    }
    let mut out = vec![0.0f64; np];
    let r = polar_call(polar, la, lo, &valid)?;
    polar_put(&r, &valid, &mut out)?;
    if nvalid > 0 {
        put_valid(cfg, la, lo, &valid, &mut out, fetch, polar, depth)?;
    }
    Ok(out)
}

enum PlanErr {
    NotFinite,
    Range,
    Group,
    Fill,
}

impl From<PlanErr> for PyErr {
    fn from(e: PlanErr) -> PyErr {
        PyValueError::new_err(match e {
            PlanErr::NotFinite => "cannot convert float NaN to integer",
            PlanErr::Range => "dem.sample: a longitude too far out for the tile arithmetic",
            PlanErr::Group => "dem_sample: sparse_group must be positive",
            PlanErr::Fill => "bilinear_tiles: tiles must fill ny x nx",
        })
    }
}

/// One sample's plan: its groups (each its points' indices and coordinates, in the groups' order), or the box (pixel
/// coordinates in it, its first tile, size and the tiles the bilinear reads, row-major).
enum Plan {
    Split(Vec<(Vec<usize>, Vec<f64>, Vec<f64>)>),
    Box { fx: Vec<f64>, fy: Vec<f64>, tx0: i64, ty0: i64, nx: i64, ny: i64, read: Vec<bool> },
}

/// The Python's sample up to its tile read, on points all inside the caps (no Python object touched).
fn plan(cfg: &Cfg, lat: &[f64], lon: &[f64]) -> Result<Plan, PlanErr> {
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
            return Err(PlanErr::NotFinite);
        }
        if x.abs() >= PIXEL_LIMIT || y.abs() >= PIXEL_LIMIT {
            return Err(PlanErr::Range);
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
        let g = if sparse { cfg.sparse_group } else { (isqrt(cfg.max_tiles) - 1).max(1) };
        if g <= 0 {
            return Err(PlanErr::Group);
        }
        let mut order: Vec<(i64, i64, usize)> = fx.iter().zip(&fy).enumerate()
            .map(|(i, (&x, &y))| (floor_i(x).div_euclid(t).div_euclid(g), floor_i(y).div_euclid(t).div_euclid(g), i))
            .collect();
        order.sort_unstable();
        let mut groups = Vec::new();
        let mut s = 0;
        while s < order.len() {
            let mut e = s + 1;
            while e < order.len() && order[e].0 == order[s].0 && order[e].1 == order[s].1 {
                e += 1;
            }
            let idx: Vec<usize> = order[s..e].iter().map(|o| o.2).collect();
            let sla: Vec<f64> = idx.iter().map(|&i| lat[i]).collect();
            let slo: Vec<f64> = idx.iter().map(|&i| lon[i]).collect();
            groups.push((idx, sla, slo));
            s = e;
        }
        return Ok(Plan::Split(groups));
    }
    if nx <= 0 || ny <= 0 {
        return Err(PlanErr::Fill);
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
    let nxu = nx as usize;
    let (wpx, hpx) = (nx * t, ny * t);
    let mut read = vec![false; nxu * ny as usize];
    for (&x, &y) in fx.iter().zip(&fy) {
        let ix = floor_i(x).clamp(0, wpx - 2);
        let iy = floor_i(y).clamp(0, hpx - 2);
        for dx in 0..2 {
            for dy in 0..2 {
                read[((iy + dy) / t) as usize * nxu + ((ix + dx) / t) as usize] = true;
            }
        }
    }
    Ok(Plan::Box { fx, fy, tx0, ty0, nx, ny, read })
}

/// The heights of points that are all inside the caps (|lat| < from_lat), each call of the Python's sample on them.
fn sample_box(cfg: &Cfg, lat: &[f64], lon: &[f64], fetch: &Bound<'_, PyAny>, polar: &Bound<'_, PyAny>, depth: u32)
    -> PyResult<Vec<f64>> {
    if lat.is_empty() {
        return Ok(Vec::new());
    }
    let py = fetch.py();
    let np = lat.len();
    let (fx, fy, tx0, ty0, nx, ny, read) = match gil_free(py, np, || plan(cfg, lat, lon))? {
        Plan::Split(groups) => {
            if depth >= MAX_DEPTH {
                return Err(PyRecursionError::new_err("dem.sample: maximum recursion depth exceeded"));
            }
            let mut out = vec![0.0f64; np];
            for (idx, sla, slo) in groups {
                let sub = sample_points(cfg, &sla, &slo, fetch, polar, depth + 1)?;
                for (&i, v) in idx.iter().zip(sub) {
                    out[i] = v;
                }
            }
            return Ok(out);
        }
        Plan::Box { fx, fy, tx0, ty0, nx, ny, read } => (fx, fy, tx0, ty0, nx, ny, read),
    };
    let n: i64 = 1i64 << cfg.zoom;
    let (nxu, tu) = (nx as usize, cfg.tile as usize);
    let keys = PyList::empty(py);
    for ty in ty0..ty0 + ny {
        for tx in tx0..tx0 + nx {
            if read[(ty - ty0) as usize * nxu + (tx - tx0) as usize] {
                keys.append(PyTuple::new(py, [cfg.zoom as i64, tx.rem_euclid(n), ty])?)?;
            }
        }
    }
    let nkeys = keys.len();
    // Every Python step first: the callback, its whole result materialised, each tile type-checked and copied when not
    // aligned. Only then the read-only borrows, with no Python run until they are released (a generator advanced, or a
    // copy made, while a borrow was held could change a borrowed array under it).
    let got = fetch.call1((keys,))?;
    let items: Vec<Bound<'_, PyAny>> = got.try_iter()?.collect::<PyResult<_>>()?;
    if items.len() != nkeys {
        return Err(PyValueError::new_err("dem_sample: fetch returned another number of tiles than asked"));
    }
    let arrs: Vec<Bound<'_, numpy::PyArray2<f32>>> =
        items.into_iter().map(normalised::<f32, numpy::Ix2>).collect::<PyResult<_>>()?;
    if arrs.iter().any(|a| a.shape() != [tu, tu]) {
        return Err(PyValueError::new_err("bilinear_tiles: every tile must be tile x tile"));
    }
    let guards: Vec<PyReadonlyArray2<'_, f32>> = arrs.iter().map(borrowed).collect::<PyResult<_>>()?;
    let bad = |_| PyValueError::new_err("dem_sample: a pixel outside the tiles read");
    let out = if snapshot(np, nkeys, tu) {
        // (copies of the tiles: read with the GIL released, no Python thread can write them meanwhile)
        let owned: Vec<numpy::ndarray::Array2<f32>> = guards.iter().map(|g| g.as_array().to_owned()).collect();
        drop(guards);
        py.detach(|| {
            let mut it = owned.iter();
            let ts: Vec<Option<ArrayView2<'_, f32>>> =
                read.iter().map(|&r| if r { it.next().map(|a| a.view()) } else { None }).collect();
            bilinear_mosaic(&ts, nxu, tu, ArrayView1::from(&fx[..]), ArrayView1::from(&fy[..]))
        }).map_err(bad)?
    } else {
        // (in place, with the GIL held: no Python thread can write a tile while it is read)
        let mut it = guards.iter();
        let ts: Vec<Option<ArrayView2<'_, f32>>> =
            read.iter().map(|&r| if r { it.next().map(|a| a.as_array()) } else { None }).collect();
        bilinear_mosaic(&ts, nxu, tu, ArrayView1::from(&fx[..]), ArrayView1::from(&fy[..])).map_err(bad)?
    };
    Ok(out.into_raw_vec_and_offset().0)
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
    borrow_api_ready(py)?;
    let (mut la, mut lo) = (coords(&lat)?, coords(&lon)?);
    if la.len() != lo.len() {
        return Err(PyValueError::new_err("dem_sample: lat and lon differ in length"));
    }
    let cfg = Cfg { zoom, from_lat, tile, max_tiles, sparse_min, sparse_ratio, sparse_group };
    let np = la.len();
    let valid: Vec<bool> = gil_free(py, np, || la.iter().map(|&a| a.abs() < from_lat).collect());
    let nvalid = valid.iter().filter(|&&v| v).count();
    if nvalid == np {
        let out = if np > 0 { sample_box(&cfg, &la, &lo, &fetch, &polar, 0)? } else { Vec::new() };
        return Ok(out.into_pyarray(py));
    }
    let mut out = vec![0.0f64; np];
    let r = polar_call(&polar, &la, &lo, &valid)?;
    if nvalid > 0 {
        // (the other points read again, as the Python took them after the polar DEM: by the first mask)
        (la, lo) = (coords(&lat)?, coords(&lon)?);
        if la.len() != np || lo.len() != np {
            return Err(PyValueError::new_err("dem_sample: lat / lon changed length"));
        }
    }
    polar_put(&r, &valid, &mut out)?;
    if nvalid > 0 {
        put_valid(&cfg, &la, &lo, &valid, &mut out, &fetch, &polar, 0)?;
    }
    Ok(out.into_pyarray(py))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(dem_sample, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::snapshot;

    #[test]
    fn snapshot_budget() {
        assert!(snapshot(1 << 20, 4, 512));                 // 4 MiB of copies
        assert!(!snapshot(1 << 30, 1024, 512));             // 1 GiB: read in place
        assert!(!snapshot(1 << 20, 1, 1 << 20));            // a 4 TiB tile (a broadcast scalar): in place
        assert!(!snapshot(usize::MAX, usize::MAX, usize::MAX));
        assert!(!snapshot(4095, 1, 512));                   // too few points to detach
    }
}
