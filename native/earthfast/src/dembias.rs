//! Align the low-resolution Canadian elevation tile with sparse survey heights.
use crate::dm::Dm;
use numpy::{ndarray::Array2, IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::{exceptions::PyValueError, prelude::*};

fn blur(a: &Array2<f64>, k: &[f64]) -> Array2<f64> {
    let (h, w) = a.dim();
    let half = (k.len() / 2) as isize;
    let mid = Array2::from_shape_fn((h, w), |(r, c)| {
        k.iter().enumerate().fold(0., |s, (j, &v)| {
            s + v * a[[
                (r as isize + j as isize - half).clamp(0, h as isize - 1) as usize,
                c,
            ]]
        })
    });
    Array2::from_shape_fn((h, w), |(r, c)| {
        k.iter().enumerate().fold(0., |s, (j, &v)| {
            s + v * mid[[
                r,
                (c as isize + j as isize - half).clamp(0, w as isize - 1) as usize,
            ]]
        })
    })
}

#[pyfunction]
pub fn dem_bias<'py>(
    py: Python<'py>,
    key: (u32, i64, i64),
    tile: PyReadonlyArray2<'py, f32>,
    lat: PyReadonlyArray1<'py, f64>,
    lon: PyReadonlyArray1<'py, f64>,
    heights: PyReadonlyArray1<'py, f64>,
    g: usize,
    sigma: f64,
) -> PyResult<Option<Bound<'py, PyArray2<f32>>>> {
    let (a, lat, lon, hs) = (
        tile.as_array(),
        lat.as_array(),
        lon.as_array(),
        heights.as_array(),
    );
    let (n, w) = a.dim();
    if n != w || n < 2 || g < 2 || hs.len() != lat.len() || hs.len() != lon.len() || !(sigma > 0.) {
        return Err(PyValueError::new_err("invalid DEM bias grid"));
    }
    let (z, x, y) = key;
    let scale = 2f64.dpowf(z as f64);
    let field = py.detach(|| {
        if hs.len() < 16 {
            return None;
        }
        let mut d = Array2::<f64>::zeros((g, g));
        let mut weight = Array2::<f64>::zeros((g, g));
        for ((&la, &lo), &h) in lat.iter().zip(lon.iter()).zip(hs.iter()) {
            let fx = ((lo + 180.) / 360. * scale - x as f64) * n as f64 - 0.5;
            let fy = ((1. - la.to_radians().dtan().dasinh() / std::f64::consts::PI) / 2. * scale
                - y as f64)
                * n as f64
                - 0.5;
            let (c, r) = (
                (fx.floor() as usize).min(n - 2),
                (fy.floor() as usize).min(n - 2),
            );
            if a[[r, c]] == 0. || a[[r + 1, c + 1]] == 0. {
                continue;
            }
            let (u, v) = ((fx - c as f64).clamp(0., 1.), (fy - r as f64).clamp(0., 1.));
            let top = a[[r, c]] as f64 * (1. - u) + a[[r, c + 1]] as f64 * u;
            let bottom = a[[r + 1, c]] as f64 * (1. - u) + a[[r + 1, c + 1]] as f64 * u;
            let bin = |p: f64| (((p + 0.5) / n as f64 * g as f64) as usize).min(g - 1);
            let k = [bin(fy), bin(fx)];
            d[k] += top * (1. - v) + bottom * v - h;
            weight[k] += 1.;
        }
        let mut occupied = 0;
        for (d, v) in d.iter_mut().zip(weight.iter_mut()) {
            if *v > 0. {
                *d /= *v;
                *v = 1.;
                occupied += 1;
            }
        }
        if occupied < 4 {
            return None;
        }
        let r = ((3. * sigma).round_ties_even() as isize).max(1);
        let mut k = (-r..=r)
            .map(|j| (-0.5 * (j as f64 / sigma).sq()).dexp())
            .collect::<Vec<_>>();
        let sum = crate::levels::np_pairwise(&k);
        for t in &mut k {
            *t /= sum;
        }
        let (mut off, sw) = (blur(&d, &k), blur(&weight, &k));
        let mx = sw.iter().copied().fold(1e-6, f64::max);
        for (o, &s) in off.iter_mut().zip(sw.iter()) {
            *o = if s > 1e-6 { *o / s } else { 0. } * (s / mx * 2.).clamp(0., 1.);
        }
        let axis = (0..n)
            .map(|i| {
                let p = (i as f64 + 0.5) / n as f64 * g as f64 - 0.5;
                let j = (p.floor() as usize).min(g - 2);
                (j, (p - j as f64).clamp(0., 1.))
            })
            .collect::<Vec<_>>();
        let mut changed = false;
        let out = Array2::from_shape_fn((n, n), |(y, x)| {
            let (r, v) = axis[y];
            let (c, u) = axis[x];
            let left = off[[r, c]] * (1. - v) + off[[r + 1, c]] * v;
            let right = off[[r, c + 1]] * (1. - v) + off[[r + 1, c + 1]] * v;
            let f = left * (1. - u) + right * u;
            changed |= !(f.abs() < 0.25);
            if a[[y, x]] == 0. {
                0.
            } else {
                (a[[y, x]] as f64 - f) as f32
            }
        });
        changed.then_some(out)
    });
    Ok(field.map(|v| v.into_pyarray(py)))
}
