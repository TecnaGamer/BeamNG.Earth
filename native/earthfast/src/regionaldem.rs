//! Coordinate systems, sampling grids and masks of the regional elevation models.
use crate::dm::Dm;
use numpy::{
    ndarray::{Array2, ArrayD, ArrayViewD},
    IntoPyArray, PyArray1, PyArray2, PyArrayDyn, PyReadonlyArray1, PyReadonlyArray2,
    PyReadonlyArrayDyn,
};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::f64::consts::FRAC_PI_4;

type Pair<'py> = (Bound<'py, PyArrayDyn<f64>>, Bound<'py, PyArrayDyn<f64>>);

fn project<'py>(
    py: Python<'py>,
    lat: ArrayViewD<'_, f64>,
    lon: ArrayViewD<'_, f64>,
    f: impl Fn(f64, f64) -> (f64, f64) + Sync + Send,
) -> PyResult<Pair<'py>> {
    if lat.shape() != lon.shape() {
        return Err(PyValueError::new_err(
            "projection needs broadcast coordinates",
        ));
    }
    let shape = lat.raw_dim();
    let (x, y): (Vec<_>, Vec<_>) =
        py.detach(|| lat.iter().zip(lon.iter()).map(|(&a, &b)| f(a, b)).unzip());
    Ok((
        ArrayD::from_shape_vec(shape.clone(), x)
            .unwrap()
            .into_pyarray(py),
        ArrayD::from_shape_vec(shape, y).unwrap().into_pyarray(py),
    ))
}

fn transverse(phi: f64, lam: f64, ecc: f64, alpha: &[f64]) -> (f64, f64) {
    let sin = phi.dsin();
    let t = (sin.datanh() - ecc * (ecc * sin).datanh()).dsinh();
    let xi = t.datan2(lam.dcos());
    let eta = (lam.dsin() / (1. + t * t).sqrt()).datanh();
    let (mut x, mut y) = (eta, xi);
    for (j, &a) in alpha.iter().enumerate() {
        let k = (2 * (j + 1)) as f64;
        x += a * (k * xi).dcos() * (k * eta).dsinh();
        y += a * (k * xi).dsin() * (k * eta).dcosh();
    }
    (x, y)
}

#[pyfunction]
fn dem_utm<'py>(
    py: Python<'py>,
    lat: PyReadonlyArrayDyn<'py, f64>,
    lon: PyReadonlyArrayDyn<'py, f64>,
    lon0: f64,
    scale: f64,
    fe: f64,
    north: f64,
    ecc: f64,
    alpha: Vec<f64>,
) -> PyResult<Pair<'py>> {
    project(py, lat.as_array(), lon.as_array(), |lat, lon| {
        let (x, y) = transverse(lat.to_radians(), (lon - lon0).to_radians(), ecc, &alpha);
        (fe + scale * x, north + scale * y)
    })
}

#[pyfunction]
fn dem_conic<'py>(
    py: Python<'py>,
    lat: PyReadonlyArrayDyn<'py, f64>,
    lon: PyReadonlyArrayDyn<'py, f64>,
    parallels: (f64, f64),
    origin: (f64, f64),
    offset: (f64, f64),
    albers: bool,
    hrdem: bool,
) -> PyResult<Pair<'py>> {
    let a = 6_378_137.;
    let f = 1. / 298.257222101;
    let e2: f64 = f * (2. - f);
    let e = e2.sqrt();
    let m = |p: f64| {
        let s = p.dsin();
        let term = if hrdem { (e * s).sq() } else { e2 * s.sq() };
        p.dcos() / (1. - term).sqrt()
    };
    let q = |p: f64| {
        let s = p.dsin();
        (1. - e2) * (s / (1. - e2 * s * s) - (1. / (2. * e)) * ((1. - e * s) / (1. + e * s)).dln())
    };
    let t = |p: f64| {
        let s = p.dsin();
        (FRAC_PI_4 - p / 2.).dtan() / ((1. - e * s) / (1. + e * s)).dpowf(e / 2.)
    };
    let (p1, p2, p0, lon0) = (
        parallels.0.to_radians(),
        parallels.1.to_radians(),
        origin.0.to_radians(),
        origin.1.to_radians(),
    );
    let (m1, m2) = (m(p1), m(p2));
    let (n, c) = if albers {
        let n = (m1 * m1 - m2 * m2) / (q(p2) - q(p1));
        (n, m1 * m1 + n * q(p1))
    } else {
        let n = (m1.dln() - m2.dln()) / (t(p1).dln() - t(p2).dln());
        (n, m1 / (n * t(p1).dpowf(n)))
    };
    let radius = |p| {
        if albers {
            a * (c - n * q(p)).sqrt() / n
        } else {
            a * c * t(p).dpowf(n)
        }
    };
    let r0 = radius(p0);
    project(py, lat.as_array(), lon.as_array(), |lat, lon| {
        let r = radius(lat.to_radians());
        let th = n * (lon.to_radians() - lon0);
        let ex = r * th.dsin();
        (
            if hrdem { ex } else { offset.0 + ex },
            offset.1 + r0 - r * th.dcos(),
        )
    })
}

#[pyfunction]
fn dem_hk_grid<'py>(
    py: Python<'py>,
    lat: PyReadonlyArrayDyn<'py, f64>,
    lon: PyReadonlyArrayDyn<'py, f64>,
    inv: PyReadonlyArray2<'py, f64>,
    alpha: Vec<f64>,
    scale: f64,
    origin: (f64, f64),
) -> PyResult<Pair<'py>> {
    let inv = inv.as_array();
    if inv.dim() != (3, 3) {
        return Err(PyValueError::new_err("expected a 3 by 3 datum transform"));
    }
    let wf = 1. / 298.257223563;
    let we2 = wf * (2. - wf);
    let f = 1. / 297.;
    let e2: f64 = f * (2. - f);
    let e = e2.sqrt();
    let m0 = scale * transverse(origin.0, 0., e, &alpha).1;
    project(py, lat.as_array(), lon.as_array(), |lat, lon| {
        let (phi, lam) = (lat.to_radians(), lon.to_radians());
        let (s, c) = (phi.dsin(), phi.dcos());
        let nu = 6378137. / (1. - we2 * s.sq()).sqrt();
        let (x, y, z) = (
            nu * c * lam.dcos() + 162.619,
            nu * c * lam.dsin() + 276.959,
            nu * (1. - we2) * s + 161.764,
        );
        let row = |i: usize| inv[[i, 0]] * x + inv[[i, 1]] * y + inv[[i, 2]] * z;
        let (x, y, z) = (row(0), row(1), row(2));
        let p = x.dhypot(y);
        let mut phi = z.datan2(p * (1. - e2));
        for _ in 0..4 {
            let s = phi.dsin();
            let nu = 6378388. / (1. - e2 * s.sq()).sqrt();
            phi = (z + e2 * nu * s).datan2(p);
        }
        let (ex, ny) = transverse(phi, y.datan2(x) - origin.1, e, &alpha);
        (836694.05 + scale * ex, 819069.80 + scale * ny - m0)
    })
}

#[pyfunction]
fn dem_tile_axes<'py>(
    py: Python<'py>,
    z: u32,
    x: i64,
    y: i64,
    size: usize,
) -> (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>) {
    let n = 2f64.dpowf(z as f64);
    let (lat, lon): (Vec<_>, Vec<_>) = (0..size)
        .map(|j| {
            let i = (j as f64 + 0.5) / size as f64;
            (
                (std::f64::consts::PI * (1. - 2. * (y as f64 + i) / n))
                    .dsinh()
                    .datan()
                    .to_degrees(),
                (x as f64 + i) / n * 360. - 180.,
            )
        })
        .unzip();
    (lat.into_pyarray(py), lon.into_pyarray(py))
}

#[pyfunction]
fn dem_feather<'py>(
    py: Python<'py>,
    valid: PyReadonlyArray2<'py, bool>,
    px: usize,
) -> Bound<'py, PyArray2<f32>> {
    let a = valid.as_array();
    py.detach(|| feather(a, px)).into_pyarray(py)
}

fn feather(a: numpy::ndarray::ArrayView2<bool>, px: usize) -> Array2<f32> {
    // Manhattan distance to the closest invalid sample. Beyond the tile each
    // edge pixel repeats, so both passes stay inside.
    let px = px.max(1);
    let (h, w) = a.dim();
    let mut d = a.mapv(|v| if v { px } else { 0 });
    for r in 0..h {
        for c in 0..w {
            if r > 0 {
                d[[r, c]] = d[[r, c]].min(d[[r - 1, c]].saturating_add(1));
            }
            if c > 0 {
                d[[r, c]] = d[[r, c]].min(d[[r, c - 1]].saturating_add(1));
            }
        }
    }
    for r in (0..h).rev() {
        for c in (0..w).rev() {
            if r + 1 < h {
                d[[r, c]] = d[[r, c]].min(d[[r + 1, c]].saturating_add(1));
            }
            if c + 1 < w {
                d[[r, c]] = d[[r, c]].min(d[[r, c + 1]].saturating_add(1));
            }
        }
    }
    d.mapv(|v| v as f32 / px as f32)
}

#[pyfunction]
fn dem_projected_grid<'py>(
    py: Python<'py>,
    samples: PyReadonlyArray2<'py, f64>,
    rows: Vec<f64>,
    cols: Vec<f64>,
    h: usize,
    w: usize,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let a = samples.as_array();
    if rows.is_empty() || cols.is_empty() || a.dim() != (rows.len(), cols.len()) {
        return Err(PyValueError::new_err("invalid projection grid"));
    }
    Ok(py
        .detach(|| {
            let iy = (0..h).map(|i| i as f64).collect::<Vec<_>>();
            let ix = (0..w).map(|i| i as f64).collect::<Vec<_>>();
            let mut mid = Array2::zeros((h, cols.len()));
            for j in 0..cols.len() {
                let v = crate::dm::interp(&iy, &rows, &a.column(j).to_vec(), None, None);
                for (i, value) in v.into_iter().enumerate() {
                    mid[[i, j]] = value;
                }
            }
            let mut out = Vec::with_capacity(h * w);
            for row in mid.rows() {
                out.extend(crate::dm::interp(
                    &ix,
                    &cols,
                    row.as_slice().unwrap(),
                    None,
                    None,
                ));
            }
            Array2::from_shape_vec((h, w), out).unwrap()
        })
        .into_pyarray(py))
}

#[pyfunction]
fn dem_hr_window<'py>(
    py: Python<'py>,
    grid: PyReadonlyArray2<'py, f32>,
    cols: PyReadonlyArray1<'py, f64>,
    rows: PyReadonlyArray1<'py, f64>,
    offset: (i64, i64),
    limit: f32,
    nodata: Option<f32>,
) -> PyResult<(Bound<'py, PyArray1<f32>>, Bound<'py, PyArray1<bool>>)> {
    let (grid, x, y) = (grid.as_array(), cols.as_array(), rows.as_array());
    if x.len() != y.len() {
        return Err(PyValueError::new_err("coordinate lengths differ"));
    }
    let ((h, w), (cx, cy)) = (grid.dim(), offset);
    let result = py
        .detach(|| {
            let mut heights = vec![0.; x.len()];
            let mut valid = vec![false; x.len()];
            for (i, (&x, &y)) in x.iter().zip(y.iter()).enumerate() {
                let (u, v) = (x - cx as f64, y - cy as f64);
                if !(u >= 0. && v >= 0. && u + 1. < w as f64 && v + 1. < h as f64) {
                    return Err("coordinate outside HRDEM window");
                }
                let (c, r) = (u as usize, v as usize);
                let (fu, fv) = ((u - c as f64) as f32, (v - r as f64) as f32);
                let [a, b, d, e] = [
                    grid[[r, c]],
                    grid[[r, c + 1]],
                    grid[[r + 1, c]],
                    grid[[r + 1, c + 1]],
                ];
                if [a, b, d, e]
                    .iter()
                    .all(|&z| z.is_finite() && z >= limit && Some(z) != nodata)
                {
                    heights[i] =
                        (a * (1. - fu) + b * fu) * (1. - fv) + (d * (1. - fu) + e * fu) * fv;
                    valid[i] = true;
                }
            }
            Ok((heights, valid))
        })
        .map_err(PyValueError::new_err)?;
    Ok((result.0.into_pyarray(py), result.1.into_pyarray(py)))
}

#[pyfunction]
fn dem_despike<'py>(
    py: Python<'py>,
    heights: &Bound<'py, PyAny>,
    ok: PyReadonlyArray2<'py, bool>,
    spike: f64,
    cap: f64,
) -> PyResult<Bound<'py, PyArray2<f32>>> {
    // Surface minus bare earth is float32. Keep the input precision for other callers.
    let ok = ok.as_array();
    macro_rules! filter {
        ($a:expr,$t:ty) => {{
            let src = $a;
            let a = src.as_array();
            let (h, w) = a.dim();
            if ok.dim() != (h, w) || h < 3 || w < 3 {
                return Err(PyValueError::new_err("invalid canopy grid"));
            }
            let (cap, spike) = (cap as $t, spike as $t);
            py.detach(|| {
                Array2::from_shape_fn((h, w), |(r, c)| {
                    let mut v = [0 as $t; 9];
                    let mut n = 0;
                    for dy in [-1isize, 0, 1] {
                        for dx in [-1isize, 0, 1] {
                            let k = [
                                r.saturating_add_signed(dy).min(h - 1),
                                c.saturating_add_signed(dx).min(w - 1),
                            ];
                            if ok[k] && !a[k].is_nan() {
                                v[n] = a[k];
                                n += 1;
                            }
                        }
                    }
                    v[..n].sort_unstable_by(<$t>::total_cmp);
                    let z = a[[r, c]];
                    let med = if n == 0 {
                        z
                    } else if n % 2 == 1 {
                        v[n / 2]
                    } else {
                        (v[n / 2 - 1] + v[n / 2]) / 2.
                    };
                    let med = if med.is_finite() { med } else { z };
                    (if z - med > spike || z > cap {
                        med.min(cap)
                    } else {
                        z
                    }) as f32
                })
            })
            .into_pyarray(py)
        }};
    }
    Ok(
        if let Ok(a) = heights.extract::<PyReadonlyArray2<'_, f32>>() {
            filter!(a, f32)
        } else {
            filter!(heights.extract::<PyReadonlyArray2<'_, f64>>()?, f64)
        },
    )
}

#[pyfunction]
fn dem_hr_tile<'py>(
    py: Python<'py>,
    ex: PyReadonlyArray2<'py, f64>,
    ny: PyReadonlyArray2<'py, f64>,
    center: f64,
    z: u32,
    min_px: f64,
    item_m: f64,
    max_level: i32,
    limit: f32,
    cog: &Bound<'py, PyAny>,
) -> PyResult<Option<(Bound<'py, PyArray2<f32>>, Bound<'py, PyArray2<bool>>)>> {
    use std::collections::BTreeMap;
    let (x, y) = (ex.as_array(), ny.as_array());
    let (h, w) = x.dim();
    if h == 0 || x.dim() != y.dim() {
        return Err(PyValueError::new_err("invalid HRDEM tile shape"));
    }
    let px = 40075016.686 * center.to_radians().dcos() / 2f64.dpowf(z as f64) / h as f64;
    let level = (px.max(min_px).max(1.).dlog2().floor() as i32).clamp(0, max_level);
    let mut groups = BTreeMap::<i64, Vec<(usize, f64, f64)>>::new();
    for (i, (&x, &y)) in x.iter().zip(y.iter()).enumerate() {
        let code = (((x / item_m).floor() as i64) + 64) * 4096 + ((y / item_m).floor() as i64 + 64);
        groups.entry(code).or_default().push((i, x, y));
    }
    let (mut out, mut ok) = (vec![0.; h * w], vec![false; h * w]);
    for (code, pts) in groups {
        let item = format!(
            "{}_{}",
            code.div_euclid(4096) - 64 + 6,
            code.rem_euclid(4096) - 64 + 3
        );
        for lv in (0..=level).rev() {
            let c = cog.call1((&item, lv))?;
            if c.is_none() {
                continue;
            }
            let xs = pts.iter().map(|p| p.1).collect::<Vec<_>>().into_pyarray(py);
            let ys = pts.iter().map(|p| p.2).collect::<Vec<_>>().into_pyarray(py);
            let (cols, rows) = c
                .call_method1("pixel_of", (xs, ys))?
                .extract::<(PyReadonlyArray1<f64>, PyReadonlyArray1<f64>)>()?;
            let coord = |a: PyReadonlyArray1<f64>| {
                let v = a.as_array().iter().map(|x| x - 0.5).collect::<Vec<_>>();
                let low = v.iter().copied().fold(f64::INFINITY, f64::min).floor() as i64 - 1;
                let high = v.iter().copied().fold(f64::NEG_INFINITY, f64::max).floor() as i64 + 3;
                (v.into_pyarray(py), low, high - low)
            };
            let (col, c0, width) = coord(cols);
            let (row, r0, height) = coord(rows);
            let win = c
                .call_method1("read_window", (c0, r0, width, height))?
                .call_method1("astype", ("float32",))?;
            let nodata = c.getattr("nodata")?.extract()?;
            use numpy::PyArrayMethods;
            let (heights, valid) = dem_hr_window(
                py,
                win.extract()?,
                col.readonly(),
                row.readonly(),
                (c0, r0),
                limit,
                nodata,
            )?;
            let (v, m) = (heights.readonly(), valid.readonly());
            for ((p, &v), &m) in pts.iter().zip(v.as_array()).zip(m.as_array()) {
                out[p.0] = v;
                ok[p.0] = m;
            }
            break;
        }
    }
    if !ok.iter().any(|&v| v) {
        return Ok(None);
    }
    Ok(Some((
        Array2::from_shape_vec((h, w), out)
            .unwrap()
            .into_pyarray(py),
        Array2::from_shape_vec((h, w), ok).unwrap().into_pyarray(py),
    )))
}

#[pyfunction]
fn dem_hk_sheets<'py>(
    py: Python<'py>,
    ex: PyReadonlyArray2<'py, f64>,
    ny: PyReadonlyArray2<'py, f64>,
    meeting: Vec<(String, String, (f64, f64, f64, f64))>,
    sheet: &Bound<'py, PyAny>,
    delta: f64,
) -> PyResult<(Bound<'py, PyArray2<f32>>, Bound<'py, PyArray2<bool>>, bool)> {
    let (x, y) = (ex.as_array(), ny.as_array());
    if x.dim() != y.dim() {
        return Err(PyValueError::new_err("invalid HK1980 coordinate grid"));
    }
    let (mut out, mut ok, mut taken) = (
        vec![0.; x.len()],
        vec![false; x.len()],
        vec![false; x.len()],
    );
    let mut short = false;
    for (name, url, (x0, y0, x1, y1)) in meeting {
        let mut ids = Vec::new();
        let mut xs = Vec::new();
        let mut ys = Vec::new();
        for (i, (&x, &y)) in x.iter().zip(y.iter()).enumerate() {
            if !taken[i] && x >= x0 && x < x1 && y >= y0 && y < y1 {
                taken[i] = true;
                ids.push(i);
                xs.push(x);
                ys.push(y);
            }
        }
        if ids.is_empty() {
            continue;
        }
        let s = sheet.call1((name, url))?;
        if !s.is_truthy()? {
            short |= s.is_none();
            continue;
        }
        let v = s.call_method1("bilinear", (xs.into_pyarray(py), ys.into_pyarray(py)))?;
        // Production sheets interpolate in float64; some memory-backed readers use float32.
        macro_rules! store {
            ($v:expr,$t:ty) => {{
                let a = $v;
                let a = a.as_array();
                if a.len() != ids.len() {
                    return Err(PyValueError::new_err("wrong Hong Kong sheet point count"));
                }
                for (&i, &z) in ids.iter().zip(a) {
                    if z.is_finite() {
                        out[i] = (z + delta as $t) as f32;
                        ok[i] = true;
                    }
                }
            }};
        }
        if let Ok(a) = v.extract::<PyReadonlyArray1<f32>>() {
            store!(a, f32);
        } else {
            store!(v.extract::<PyReadonlyArray1<f64>>()?, f64);
        }
    }
    Ok((
        Array2::from_shape_vec(x.dim(), out)
            .unwrap()
            .into_pyarray(py),
        Array2::from_shape_vec(x.dim(), ok)
            .unwrap()
            .into_pyarray(py),
        short,
    ))
}

#[pyfunction]
fn dem_blend<'py>(
    py: Python<'py>,
    key: (u32, i64, i64),
    a: PyReadonlyArray2<'py, f32>,
    h: PyReadonlyArray2<'py, f32>,
    valid: PyReadonlyArray2<'py, bool>,
    metres: f64,
    cap: usize,
) -> PyResult<Option<Bound<'py, PyArray2<f32>>>> {
    let (a, h, valid) = (a.as_array(), h.as_array(), valid.as_array());
    if a.dim() != h.dim() || h.dim() != valid.dim() {
        return Err(PyValueError::new_err("invalid lidar blend grid"));
    }
    let px = crate::demtiles::pixel_m(key.0, key.2, a.dim().0);
    let rounds = ((metres / px).round_ties_even() as usize).max(2).min(cap);
    let out = py.detach(|| {
        let ok = Array2::from_shape_fn(a.dim(), |i| valid[i] && a[i] != 0.);
        if !ok.iter().any(|&v| v) {
            return None;
        }
        let weights = feather(ok.view(), rounds);
        Some(Array2::from_shape_fn(a.dim(), |i| {
            a[i] * (1. - weights[i]) + h[i] * weights[i]
        }))
    });
    Ok(out.map(|v| v.into_pyarray(py)))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(dem_despike, m)?)?;
    m.add_function(wrap_pyfunction!(dem_hr_tile, m)?)?;
    m.add_function(wrap_pyfunction!(dem_hk_sheets, m)?)?;
    m.add_function(wrap_pyfunction!(dem_blend, m)?)?;
    m.add_function(wrap_pyfunction!(dem_utm, m)?)?;
    m.add_function(wrap_pyfunction!(dem_conic, m)?)?;
    m.add_function(wrap_pyfunction!(dem_hk_grid, m)?)?;
    m.add_function(wrap_pyfunction!(dem_tile_axes, m)?)?;
    m.add_function(wrap_pyfunction!(dem_feather, m)?)?;
    m.add_function(wrap_pyfunction!(dem_projected_grid, m)?)?;
    Ok(())
}
