//! Bilinear lookup of two fields sampled on a regular grid (grading._add_cuts' lat / lon of its cutting probes: the
//! frame's exact conversion at the grid's nodes, interpolated between them; the conversion is smooth to well under a
//! millimetre over a 32 m cell, and per point it was most of the cuttings' time: 3.7 million probes per 4 cells).

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// a, b: (ny, nx) values at nodes (x0 + i * step, y0 + j * step); per point (x, y) both fields interpolated (points
/// outside the grid take the nearest cell's plane).
pub fn bilinear2(a: &[f64], b: &[f64], nx: usize, ny: usize, x0: f64, y0: f64, step: f64, x: &[f64], y: &[f64])
                 -> (Vec<f64>, Vec<f64>) {
    let n = x.len().min(y.len());
    let (mut oa, mut ob) = (Vec::with_capacity(n), Vec::with_capacity(n));
    if nx < 2 || ny < 2 || !(step > 0.0) {
        return (vec![f64::NAN; n], vec![f64::NAN; n]);
    }
    for k in 0..n {
        let fx = (x[k] - x0) / step;
        let fy = (y[k] - y0) / step;
        let i = (fx.floor() as isize).clamp(0, nx as isize - 2) as usize;
        let j = (fy.floor() as isize).clamp(0, ny as isize - 2) as usize;
        let (t, u) = (fx - i as f64, fy - j as f64);
        let (p00, p10, p01, p11) = (j * nx + i, j * nx + i + 1, (j + 1) * nx + i, (j + 1) * nx + i + 1);
        let w00 = (1.0 - t) * (1.0 - u);
        let w10 = t * (1.0 - u);
        let w01 = (1.0 - t) * u;
        let w11 = t * u;
        oa.push(a[p00] * w00 + a[p10] * w10 + a[p01] * w01 + a[p11] * w11);
        ob.push(b[p00] * w00 + b[p10] * w10 + b[p01] * w01 + b[p11] * w11);
    }
    (oa, ob)
}

#[pyfunction]
pub fn grid_bilinear2<'py>(py: Python<'py>, a: PyReadonlyArray2<'py, f64>, b: PyReadonlyArray2<'py, f64>, x0: f64,
                           y0: f64, step: f64, x: PyReadonlyArray1<'py, f64>, y: PyReadonlyArray1<'py, f64>)
                           -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    let (av, bv) = (a.as_array(), b.as_array());
    let (ny, nx) = (av.shape()[0], av.shape()[1]);
    if bv.shape() != av.shape() {
        return Err(pyo3::exceptions::PyValueError::new_err("grid_bilinear2: a and b differ in shape"));
    }
    let (a_s, b_s) = (av.iter().copied().collect::<Vec<f64>>(), bv.iter().copied().collect::<Vec<f64>>());
    let (xs, ys) = (x.as_array().to_vec(), y.as_array().to_vec());
    let (oa, ob) = py.allow_threads(|| bilinear2(&a_s, &b_s, nx, ny, x0, y0, step, &xs, &ys));
    Ok((oa.into_pyarray(py), ob.into_pyarray(py)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exact_on_a_plane() {
        // f = 2x + 3y + 1 on a 3 x 4 grid, step 10 from (5, -5): bilinear reproduces a plane exactly, also outside
        let (nx, ny, x0, y0, s) = (3usize, 4usize, 5.0, -5.0, 10.0);
        let mut a = vec![0.0; nx * ny];
        let mut b = vec![0.0; nx * ny];
        for j in 0..ny {
            for i in 0..nx {
                let (x, y) = (x0 + i as f64 * s, y0 + j as f64 * s);
                a[j * nx + i] = 2.0 * x + 3.0 * y + 1.0;
                b[j * nx + i] = -x + 0.5 * y;
            }
        }
        let xs = [5.0, 12.5, 24.9, 30.0, 0.0];
        let ys = [-5.0, 7.0, 24.0, 3.0, -9.0];
        let (oa, ob) = bilinear2(&a, &b, nx, ny, x0, y0, s, &xs, &ys);
        for k in 0..xs.len() {
            assert!((oa[k] - (2.0 * xs[k] + 3.0 * ys[k] + 1.0)).abs() < 1e-9);
            assert!((ob[k] - (-xs[k] + 0.5 * ys[k])).abs() < 1e-9);
        }
    }
}
