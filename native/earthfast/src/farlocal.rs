//! Far meshes on the anchor's regular plane grid. The DEM provider supplies
//! elevations. The shading normals use the full grid even after simplification.
use crate::{
    dm::Dm,
    farglobal::{arr2, cross, edge_runs, grid_tris, normal_err, surface_at, WATER_U},
};
use numpy::PyReadonlyArray1;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray2};
use pyo3::{exceptions::PyValueError, prelude::*};

type Grid<'py> = Bound<'py, PyArray2<f64>>;

#[pyfunction]
fn far_axes(
    py: Python<'_>,
    origin: (f64, f64),
    cells: usize,
    spacing: f64,
) -> (Bound<'_, PyArray1<f64>>, Bound<'_, PyArray1<f64>>) {
    let axis = |o| {
        (0..=cells)
            .map(|i| o + i as f64 * spacing)
            .collect::<Vec<f64>>()
            .into_pyarray(py)
    };
    (axis(origin.0), axis(origin.1))
}

#[pyfunction]
fn far_global_coordinates(
    py: Python<'_>,
    z: u32,
    x: i64,
    y: i64,
    steps: usize,
) -> PyResult<(Grid<'_>, Grid<'_>)> {
    if steps == 0 || z > 30 {
        return Err(PyValueError::new_err("invalid Mercator grid"));
    }
    let n = steps + 1;
    let w = (1u64 << z) as f64;
    let mut lat = Vec::with_capacity(n * n);
    let mut lon = Vec::with_capacity(n * n);
    for j in 0..n {
        let yf = (y + 1) as f64 - j as f64 / steps as f64;
        // numpy.degrees multiplies by a single precomputed radians-to-degrees factor.
        let la = (std::f64::consts::PI * (1. - 2. * yf / w))
            .dsinh()
            .datan()
            .to_degrees();
        for i in 0..n {
            lat.push(la);
            lon.push((x as f64 + i as f64 / steps as f64) / w * 360. - 180.);
        }
    }
    Ok((arr2(py, lat, n), arr2(py, lon, n)))
}

#[pyfunction]
fn far_grid_triangles(py: Python<'_>, cells: usize) -> Bound<'_, PyArray2<i64>> {
    arr2(py, grid_tris(cells).into_iter().flatten().collect(), 3)
}

// np.gradient preserves a floating elevation array's precision. dnp.hypot
// evaluates in double and rounds back to that same precision before clipping.
fn palette(el: f64, x: (f64, f64), y: (f64, f64), dx: f64, dy: f64, precision: u8) -> [f64; 2] {
    let cast = |v: f64| match precision {
        // Binary16 has ten fraction bits and its least normal exponent is -14.
        16 if v.is_finite() && v.abs() >= 65520. => f64::INFINITY.copysign(v),
        16 if v.is_finite() && v != 0. => {
            let unit = libm::scalbn(1., libm::ilogb(v.abs()).max(-14) - 10);
            (v / unit).round_ties_even() * unit
        }
        32 => v as f32 as f64,
        _ => v,
    };
    let gx = cast(cast(x.1 - x.0) / dx);
    let gy = cast(cast(y.1 - y.0) / dy);
    let slope = cast(gx.dhypot(gy)).clamp(0., 1.);
    let u = if el <= cast(0.5) && slope < cast(0.02) {
        cast(WATER_U)
    } else {
        cast(cast(0.1) + cast(cast(0.88) * cast(el / cast(5000.)).clamp(0., 1.)))
    };
    [u, cast(cast(0.5) - cast(cast(0.47) * slope))]
}

struct Mesh {
    p: Vec<f64>,
    normals: Vec<f64>,
    uv: Vec<f64>,
    planar: Vec<f64>,
    tris: Vec<i64>,
    grid: Vec<f32>,
    pz: f64,
    err: f64,
    err_n: f64,
    sink: Option<f64>,
    skirt: f64,
}

fn build(
    elev: &[f64],
    full: &[f64],
    reference: &[f64],
    n: usize,
    origin: (f64, f64),
    size: f64,
    sp: f64,
    eps: f64,
    fine_limit: Option<usize>,
    precision: u8,
    terrain: Option<(&[f64], usize, f64, f64)>,
) -> Mesh {
    let c = n - 1;
    let (faces, grid, worst) = if eps > 0. || fine_limit.is_some() {
        let mut tolerance = eps;
        loop {
            let (tris, grid, worst) = crate::rtin_core(full, n, tolerance);
            if let Some(limit) = fine_limit {
                let mut used = vec![false; n * n];
                for &i in tris.iter().flatten() {
                    used[i as usize] = true;
                }
                if used.into_iter().filter(|&u| u).count() + 4 * n > limit {
                    tolerance *= 2.;
                    continue;
                }
            }
            break (tris, grid, worst);
        }
    } else {
        (grid_tris(c), full.to_vec(), 0.)
    };
    let (err, err_n) = if fine_limit.is_some() {
        (worst, worst)
    } else {
        let nr = 2 * c + 1;
        let mut model = Vec::with_capacity(nr * nr);
        let mut err = 0f64;
        for (k, &r) in reference.iter().enumerate() {
            let s = surface_at(&grid, n, (k % nr) as f64 / 2., (k / nr) as f64 / 2.);
            let delta = (r - s).abs();
            if !err.is_nan() {
                err = if delta.is_nan() {
                    delta
                } else {
                    err.max(delta)
                };
            }
            model.push(s);
        }
        (err, normal_err(reference, &model, nr, sp / 2.))
    };
    let sink = terrain.map(|(t, nt, sq, margin)| {
        let maximum = t
            .iter()
            .enumerate()
            .map(|(k, z)| {
                surface_at(
                    &grid,
                    n,
                    (k % nt) as f64 * sq / sp,
                    (k / nt) as f64 * sq / sp,
                ) - z
            })
            .fold(f64::NEG_INFINITY, |a, b| {
                if a.is_nan() || b.is_nan() {
                    f64::NAN
                } else {
                    a.max(b)
                }
            });
        0.0f64.max(maximum) + margin
    });
    let skirt = crate::farglobal::far_skirt_depth(size, err);
    // NumPy reduces contiguous arrays in chunks of its default 8192 elements.
    let sum = grid
        .chunks(8192)
        .map(crate::levels::np_pairwise)
        .fold(0., |a, b| a + b);
    let pz = (sum / grid.len() as f64).round_ties_even();
    let mut index = vec![-1i64; n * n];
    for &k in faces.iter().flatten() {
        index[k as usize] = 0;
    }
    let mut p = Vec::new();
    let mut normals = Vec::new();
    let mut uv = Vec::new();
    let mut planar = Vec::new();
    let axis = |start: f64, i: usize| start + i as f64 * sp;
    for k in 0..n * n {
        if index[k] < 0 {
            continue;
        }
        index[k] = (p.len() / 3) as i64;
        let (i, j) = (k % n, k / n);
        let (i0, i1) = (i.saturating_sub(1), (i + 1).min(c));
        let (j0, j1) = (j.saturating_sub(1), (j + 1).min(c));
        let (w, e, s, north) = (j * n + i0, j * n + i1, j0 * n + i, j1 * n + i);
        let (nx, ny) = ((i1 - i0) as f64, (j1 - j0) as f64);
        let di = [
            (axis(origin.0, i1) - axis(origin.0, i0)) / nx,
            0.,
            (full[e] - full[w]) / nx,
        ];
        let dj = [
            0.,
            (axis(origin.1, j1) - axis(origin.1, j0)) / ny,
            (full[north] - full[s]) / ny,
        ];
        let normal = cross(di, dj);
        let length = (normal[0] * normal[0] + normal[1] * normal[1] + normal[2] * normal[2]).sqrt();
        normals.extend(normal.map(|v| v / length));
        uv.extend(palette(
            elev[k],
            (elev[w], elev[e]),
            (elev[s], elev[north]),
            nx * sp,
            ny * sp,
            precision,
        ));
        let (x, y) = (axis(origin.0, i) - origin.0, axis(origin.1, j) - origin.1);
        p.extend([x, y, grid[k] - pz]);
        planar.extend([x / size, y / size]);
    }
    let mut tris: Vec<i64> = faces.iter().flatten().map(|&k| index[k as usize]).collect();
    for run in edge_runs(n) {
        let mut previous = None;
        for k in run {
            let top = index[k];
            if top < 0 {
                continue;
            }
            let low = (p.len() / 3) as i64;
            let a = top as usize;
            p.extend([p[3 * a], p[3 * a + 1], (grid[k] - skirt) - pz]);
            normals.extend_from_within(3 * a..3 * a + 3);
            uv.extend_from_within(2 * a..2 * a + 2);
            planar.extend_from_within(2 * a..2 * a + 2);
            if let Some(last) = previous {
                tris.extend([last, low - 1, top, top, low - 1, low]);
            }
            previous = Some(top);
        }
    }
    Mesh {
        p,
        normals,
        uv,
        planar,
        tris,
        grid: grid.into_iter().map(|x| x as f32).collect(),
        pz,
        err,
        err_n,
        sink,
        skirt,
    }
}

type Local<'py> = (
    Grid<'py>,
    Grid<'py>,
    Bound<'py, PyAny>,
    Grid<'py>,
    Bound<'py, PyArray2<i64>>,
    Bound<'py, PyArray2<f32>>,
    f64,
    f64,
    Option<f64>,
    f64,
    f64,
);
#[pyfunction]
#[pyo3(signature=(elev, full, reference, origin, size, spacing, eps, fine_limit, precision, terrain, terrain_spacing, margin, quantum))]
fn far_local_mesh<'py>(
    py: Python<'py>,
    elev: PyReadonlyArray2<'py, f64>,
    full: PyReadonlyArray2<'py, f64>,
    reference: PyReadonlyArray2<'py, f64>,
    origin: (f64, f64),
    size: f64,
    spacing: f64,
    eps: f64,
    fine_limit: Option<usize>,
    precision: u8,
    terrain: Option<PyReadonlyArray2<'py, f64>>,
    terrain_spacing: f64,
    margin: f64,
    quantum: Option<f64>,
) -> PyResult<Local<'py>> {
    let f = full.as_array();
    let n = f.nrows();
    if n < 2
        || n != f.ncols()
        || f.shape() != elev.as_array().shape()
        || spacing <= 0.
        || size <= 0.
        || (fine_limit.is_none() && reference.as_array().shape() != [2 * n - 1, 2 * n - 1])
        || ((eps > 0. || fine_limit.is_some()) && !(n - 1).is_power_of_two())
        || fine_limit.is_some_and(|v| eps <= 0. || v < 4 * n + 4)
    {
        return Err(PyValueError::new_err(
            "invalid far mesh dimensions or refinement tolerance",
        ));
    }
    let e = elev.as_array().iter().copied().collect::<Vec<_>>();
    let mut f = f.iter().copied().collect::<Vec<_>>();
    if let Some(q) = quantum {
        for h in &mut f {
            *h = (*h / q).round_ties_even() * q;
        }
    }
    let r = reference.as_array().iter().copied().collect::<Vec<_>>();
    let t = terrain.map(|a| {
        let a = a.as_array();
        (a.iter().copied().collect::<Vec<_>>(), a.nrows(), a.ncols())
    });
    if t.as_ref().is_some_and(|(_, ny, nx)| nx != ny || *nx < 2) {
        return Err(PyValueError::new_err("invalid terrain lattice"));
    }
    let m = py.detach(move || {
        build(
            &e,
            &f,
            &r,
            n,
            origin,
            size,
            spacing,
            eps,
            fine_limit,
            precision,
            t.as_ref()
                .map(|(a, n, _)| (a.as_slice(), *n, terrain_spacing, margin)),
        )
    });
    let uv = if precision == 16 {
        arr2(py, m.uv, 2).call_method1("astype", ("float16",))?
    } else if precision == 32 {
        arr2(py, m.uv.into_iter().map(|x| x as f32).collect(), 2).into_any()
    } else {
        arr2(py, m.uv, 2).into_any()
    };
    Ok((
        arr2(py, m.p, 3),
        arr2(py, m.normals, 3),
        uv,
        arr2(py, m.planar, 2),
        arr2(py, m.tris, 3),
        arr2(py, m.grid, n),
        m.pz,
        m.err,
        m.sink,
        m.skirt,
        m.err_n,
    ))
}

/// Select the triangle natively. Evaluate the two edge differences in the
/// source array's own dtype, as required for old float32 grids and integer grids.
#[pyfunction]
fn far_surface_points<'py>(
    py: Python<'py>,
    grid: &Bound<'py, PyAny>,
    n: usize,
    gx: PyReadonlyArray1<'py, f64>,
    gy: PyReadonlyArray1<'py, f64>,
) -> PyResult<Bound<'py, PyAny>> {
    let x = gx.as_array();
    let y = gy.as_array();
    if n < 2 || x.len() != y.len() {
        return Err(PyValueError::new_err("invalid far surface grid or points"));
    }
    // Float grids are the in-game case. Subtract before widening, since the
    // old float32 mesh stores rounded edge differences as well as heights.
    macro_rules! float_grid {
        ($float:ty) => {
            if let Ok(g) = grid.extract::<PyReadonlyArray2<$float>>() {
                let g = g.as_array();
                if g.dim() != (n, n) {
                    return Err(PyValueError::new_err("far surface must be square"));
                }
                let at = |k: usize| g[[k / n, k % n]];
                let values: Vec<f64> = x
                    .iter()
                    .zip(y.iter())
                    .map(|(&x, &y)| {
                        let t = crate::farglobal::triangle_at(n, x, y);
                        at(t.base) as f64
                            + t.a * (at(t.dx.0) - at(t.dx.1)) as f64
                            + t.b * (at(t.dy.0) - at(t.dy.1)) as f64
                    })
                    .collect();
                return Ok(values.into_pyarray(py).into_any());
            }
        };
    }
    float_grid!(f64);
    float_grid!(f32);
    let z = grid.call_method0("ravel")?;
    let mut indices: [Vec<usize>; 5] = std::array::from_fn(|_| Vec::with_capacity(x.len()));
    let (mut a, mut b) = (Vec::with_capacity(x.len()), Vec::with_capacity(x.len()));
    for (&x, &y) in x.iter().zip(y.iter()) {
        let t = crate::farglobal::triangle_at(n, x, y);
        for (v, i) in indices
            .iter_mut()
            .zip([t.base, t.dx.0, t.dx.1, t.dy.0, t.dy.1])
        {
            v.push(i);
        }
        a.push(t.a);
        b.push(t.b);
    }
    let [base, dx1, dx0, dy1, dy0] = indices.map(|i| z.get_item(i.into_pyarray(py)));
    let xterm = a.into_pyarray(py).as_any().mul(dx1?.sub(dx0?)?)?;
    let yterm = b.into_pyarray(py).as_any().mul(dy1?.sub(dy0?)?)?;
    base?.add(xterm)?.add(yterm)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(crate::farglobal::far_skirt_depth, m)?)?;
    m.add_function(wrap_pyfunction!(far_surface_points, m)?)?;
    m.add_function(wrap_pyfunction!(far_axes, m)?)?;
    m.add_function(wrap_pyfunction!(far_global_coordinates, m)?)?;
    m.add_function(wrap_pyfunction!(far_grid_triangles, m)?)?;
    m.add_function(wrap_pyfunction!(far_local_mesh, m)?)?;
    Ok(())
}
