//! A mapped roundabout ring's radius (roads.ring_radius: a least-squares circle through its points), byte for byte the
//! numpy / dnp it replaced: the mean latitude by numpy's pairwise sum, dnp.lstsq (the columns scaled by their
//! root summed squares, in row order; Householder QR, dm::lstsq; numpy's minimum-norm answer from the eigen
//! decomposition of a^T a where the QR finds the columns dependent), dnp.matmul's products summed in order.

use pyo3::prelude::*;
use super::pyobj::CArr2;

use crate::dm::Dm;

const LSTSQ_RANK_TOL: f64 = 1e-10;   // dnp.LSTSQ_RANK_TOL

/// np.max: NaN wins.
fn npmax(v: &[f64]) -> f64 {
    let mut m = v[0];
    for &x in &v[1..] {
        if x.is_nan() || m.is_nan() {
            m = f64::NAN;
        } else if x > m {
            m = x;
        }
    }
    m
}

/// dnp.lstsq(a, b)[0] for a (r, 3), b (r,), r >= 3, rcond None.
fn lstsq3(a: &[[f64; 3]], b: &[f64]) -> [f64; 3] {
    let r = a.len();
    // scale: sqrt(np.add.reduce(a * a, axis=0)): each column's squares summed in row order (the first row's first)
    let mut scale = [0.0f64; 3];
    for j in 0..3 {
        let mut s = a[0][j] * a[0][j];
        for row in &a[1..] {
            s += row[j] * row[j];
        }
        let s = s.sqrt();
        scale[j] = if s > 0.0 { s } else { 1.0 };
    }
    let s_: Vec<f64> = a.iter().flat_map(|row| [row[0] / scale[0], row[1] / scale[1], row[2] / scale[2]]).collect();
    if let Some(x) = crate::dm::lstsq(s_, r, 3, b.to_vec(), 1, LSTSQ_RANK_TOL) {
        return [x[0] / scale[0], x[1] / scale[1], x[2] / scale[2]];
    }
    // the minimum-norm x from eigh(a^T a): matmul's products summed in order of k
    let mut ata = vec![0.0f64; 9];
    for j in 0..3 {
        for i in 0..3 {
            let mut s = a[0][j] * a[0][i];
            for row in &a[1..] {
                s += row[j] * row[i];
            }
            ata[j * 3 + i] = s;
        }
    }
    let (w, v) = crate::dm::sym_eig(ata, 3);
    let wmax = npmax(&w);
    let keep: Vec<bool> = w.iter().map(|&x| x > 1e-12 * wmax).collect();
    let inv_w: Vec<f64> = (0..3).map(|i| if keep[i] { 1.0 / w[i] } else { 0.0 }).collect();
    let mut atb = [0.0f64; 3];
    for j in 0..3 {
        let mut s = a[0][j] * b[0];
        for (row, &bv) in a[1..].iter().zip(&b[1..]) {
            s += row[j] * bv;
        }
        atb[j] = s;
    }
    // t = v.T @ atb; x = (v * inv_w) @ t
    let vv = |m: usize, i: usize| v[m * 3 + i];
    let t: Vec<f64> = (0..3).map(|i| vv(0, i) * atb[0] + vv(1, i) * atb[1] + vv(2, i) * atb[2]).collect();
    let x: Vec<f64> = (0..3).map(|m| (vv(m, 0) * inv_w[0]) * t[0] + (vv(m, 1) * inv_w[1]) * t[1]
        + (vv(m, 2) * inv_w[2]) * t[2]).collect();
    [x[0], x[1], x[2]]
}

/// roads.ring_radius's circle of a ring's points ll (k, 2: lon, lat): its radius in m, None for fewer than 5 points, a
/// fit off by more than 15 % of its radius, or a radius outside 3-80 m.
#[pyfunction]
pub fn ring_radius(ll: CArr2<'_, f64>) -> PyResult<Option<f64>> {
    let p = ll.as_array();
    if p.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("ring_radius: (k, 2) lon / lat"));
    }
    let k = p.nrows();
    if k < 5 {
        return Ok(None);
    }
    let lat: Vec<f64> = (0..k).map(|i| p[[i, 1]]).collect();
    let mean = crate::devctx::pairwise(&lat) / k as f64;
    let kk = mean.to_radians().dcos() * 111320.0;
    let xy: Vec<[f64; 2]> = (0..k).map(|i| [(p[[i, 0]] - p[[0, 0]]) * kk, (p[[i, 1]] - p[[0, 1]]) * 110540.0]).collect();
    let a: Vec<[f64; 3]> = xy.iter().map(|q| [2.0 * q[0], 2.0 * q[1], 1.0]).collect();
    let b: Vec<f64> = xy.iter().map(|q| q[0] * q[0] + q[1] * q[1]).collect();
    let sol = lstsq3(&a, &b);
    let r2 = sol[2] + sol[0] * sol[0] + sol[1] * sol[1];
    if r2 <= 0.0 {
        return Ok(None);
    }
    let r = r2.sqrt();
    let dev: Vec<f64> = xy.iter().map(|q| ((q[0] - sol[0]).dhypot(q[1] - sol[1]) - r).abs()).collect();
    let res = npmax(&dev);
    Ok(if (3.0..=80.0).contains(&r) && res <= 0.15 * r { Some(r) } else { None })
}
