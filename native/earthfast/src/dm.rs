//! Deterministic maths: the transcendental functions from the `libm` crate (pure Rust, the same bits on Linux,
//! Windows (zig / mingw and MSVC builds) and every glibc), never the platform's C maths library.
//!
//! `f64::sin`, `hypot`, `atan2`, `exp`, `powf`, ... call the C library of the platform the module runs on: glibc on
//! Linux (and 2.35 and 2.44 differ in hypot's last bit), mingw's or the UCRT's on Windows. Their last bits differ on a
//! few percent of the inputs, and the planners' decisions (a crossing found or not, the nearer of two parts, a sort by
//! a computed key) turn such a bit into another road: GitHub #1169, Windows built a 0.78 g crest and a 1.41 m step where
//! Linux had 0.26 g and 0.96 m from the same map data. Every kernel calls these instead (`x.dsin()` for `x.sin()`);
//! tests/test_determinism.py fails on a platform call in native/earthfast/src. sqrt, the arithmetic operators,
//! floor / ceil / round / trunc, abs, copysign, `%` and powi are exact or the same code everywhere and stay.

#[allow(dead_code)]
pub trait Dm: Sized {
    fn dsin(self) -> Self;
    fn dcos(self) -> Self;
    fn dtan(self) -> Self;
    fn dasin(self) -> Self;
    fn dacos(self) -> Self;
    fn datan(self) -> Self;
    fn datan2(self, x: Self) -> Self;
    fn dsin_cos(self) -> (Self, Self);
    fn dhypot(self, y: Self) -> Self;
    fn dexp(self) -> Self;
    fn dexp2(self) -> Self;
    fn dexp_m1(self) -> Self;
    fn dln(self) -> Self;
    fn dln_1p(self) -> Self;
    fn dlog2(self) -> Self;
    fn dlog10(self) -> Self;
    fn dpowf(self, y: Self) -> Self;
    fn dcbrt(self) -> Self;
    fn dsinh(self) -> Self;
    fn dcosh(self) -> Self;
    fn dtanh(self) -> Self;
    fn dasinh(self) -> Self;
    fn dacosh(self) -> Self;
    fn datanh(self) -> Self;
    fn dmul_add(self, a: Self, b: Self) -> Self;
}

macro_rules! dm_impl {
    ($t:ty, $sin:ident, $cos:ident, $tan:ident, $asin:ident, $acos:ident, $atan:ident, $atan2:ident, $sincos:ident,
     $hypot:ident, $exp:ident, $exp2:ident, $expm1:ident, $log:ident, $log1p:ident, $log2:ident, $log10:ident,
     $pow:ident, $cbrt:ident, $sinh:ident, $cosh:ident, $tanh:ident, $asinh:ident, $acosh:ident, $atanh:ident,
     $fma:ident) => {
        impl Dm for $t {
            #[inline] fn dsin(self) -> Self { libm::$sin(self) }
            #[inline] fn dcos(self) -> Self { libm::$cos(self) }
            #[inline] fn dtan(self) -> Self { libm::$tan(self) }
            #[inline] fn dasin(self) -> Self { libm::$asin(self) }
            #[inline] fn dacos(self) -> Self { libm::$acos(self) }
            #[inline] fn datan(self) -> Self { libm::$atan(self) }
            #[inline] fn datan2(self, x: Self) -> Self { libm::$atan2(self, x) }
            #[inline] fn dsin_cos(self) -> (Self, Self) { libm::$sincos(self) }
            #[inline] fn dhypot(self, y: Self) -> Self { libm::$hypot(self, y) }
            #[inline] fn dexp(self) -> Self { libm::$exp(self) }
            #[inline] fn dexp2(self) -> Self { libm::$exp2(self) }
            #[inline] fn dexp_m1(self) -> Self { libm::$expm1(self) }
            #[inline] fn dln(self) -> Self { libm::$log(self) }
            #[inline] fn dln_1p(self) -> Self { libm::$log1p(self) }
            #[inline] fn dlog2(self) -> Self { libm::$log2(self) }
            #[inline] fn dlog10(self) -> Self { libm::$log10(self) }
            #[inline] fn dpowf(self, y: Self) -> Self { libm::$pow(self, y) }
            #[inline] fn dcbrt(self) -> Self { libm::$cbrt(self) }
            #[inline] fn dsinh(self) -> Self { libm::$sinh(self) }
            #[inline] fn dcosh(self) -> Self { libm::$cosh(self) }
            #[inline] fn dtanh(self) -> Self { libm::$tanh(self) }
            #[inline] fn dasinh(self) -> Self { libm::$asinh(self) }
            #[inline] fn dacosh(self) -> Self { libm::$acosh(self) }
            #[inline] fn datanh(self) -> Self { libm::$atanh(self) }
            #[inline] fn dmul_add(self, a: Self, b: Self) -> Self { libm::$fma(self, a, b) }
        }
    };
}

dm_impl!(f64, sin, cos, tan, asin, acos, atan, atan2, sincos, hypot, exp, exp2, expm1, log, log1p, log2, log10, pow,
         cbrt, sinh, cosh, tanh, asinh, acosh, atanh, fma);
dm_impl!(f32, sinf, cosf, tanf, asinf, acosf, atanf, atan2f, sincosf, hypotf, expf, exp2f, expm1f, logf, log1pf, log2f,
         log10f, powf, cbrtf, sinhf, coshf, tanhf, asinhf, acoshf, atanhf, fmaf);

// ---- the server's Python maths (server/dmath.py scalars, server/dnp.py arrays): the same functions for numpy and
// math calls, so that a projection or a planner in Python gets the same bits on every platform too (numpy's and
// CPython's transcendental functions are the platform's C library or numpy's own CPU-dispatched SIMD code)

use numpy::{PyArray1, PyReadonlyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

fn unary(op: u8) -> Option<fn(f64) -> f64> {
    Some(match op {
        0 => libm::sin, 1 => libm::cos, 2 => libm::tan, 3 => libm::asin, 4 => libm::acos, 5 => libm::atan,
        6 => libm::sinh, 7 => libm::cosh, 8 => libm::tanh, 9 => libm::asinh, 10 => libm::atanh, 11 => libm::exp,
        12 => libm::log, 13 => libm::log2, 14 => libm::log10, 15 => libm::cbrt, 16 => libm::expm1, 17 => libm::log1p,
        18 => libm::acosh,
        _ => return None,
    })
}

fn binary(op: u8) -> Option<fn(f64, f64) -> f64> {
    Some(match op {
        32 => libm::atan2, 33 => libm::pow, 34 => libm::hypot,
        _ => return None,
    })
}

/// dnp.py: op (unary() / binary() codes) on flat float64 arrays (y: the second operand, x's length, for a binary op).
#[pyfunction]
#[pyo3(signature = (op, x, y=None))]
pub fn dm_vec<'py>(py: Python<'py>, op: u8, x: PyReadonlyArray1<'py, f64>, y: Option<PyReadonlyArray1<'py, f64>>)
    -> PyResult<Bound<'py, PyArray1<f64>>> {
    let x = x.as_slice()?;
    let out: Vec<f64> = match (unary(op), binary(op), y) {
        (Some(f), _, None) => py.detach(|| x.iter().map(|&v| f(v)).collect()),
        (_, Some(f), Some(y)) => {
            let y = y.as_slice()?;
            if y.len() != x.len() {
                return Err(PyValueError::new_err("dm_vec: x and y differ in length"));
            }
            py.detach(|| x.iter().zip(y).map(|(&a, &b)| f(a, b)).collect())
        }
        _ => return Err(PyValueError::new_err(format!("dm_vec: op {op} with the wrong number of operands"))),
    };
    Ok(PyArray1::from_vec(py, out))
}

macro_rules! dm_scalar1 {
    ($($name:ident => $f:path),* $(,)?) => { $(
        /// dmath.py: the libm crate's function of a Python float.
        #[pyfunction]
        pub fn $name(x: f64) -> f64 { $f(x) }
    )* };
}
macro_rules! dm_scalar2 {
    ($($name:ident => $f:path),* $(,)?) => { $(
        /// dmath.py: the libm crate's function of two Python floats.
        #[pyfunction]
        pub fn $name(x: f64, y: f64) -> f64 { $f(x, y) }
    )* };
}
dm_scalar1!(dm_sin => libm::sin, dm_cos => libm::cos, dm_tan => libm::tan, dm_asin => libm::asin, dm_acos => libm::acos,
            dm_atan => libm::atan, dm_sinh => libm::sinh, dm_cosh => libm::cosh, dm_tanh => libm::tanh,
            dm_asinh => libm::asinh, dm_acosh => libm::acosh, dm_atanh => libm::atanh, dm_exp => libm::exp,
            dm_expm1 => libm::expm1, dm_log => libm::log, dm_log1p => libm::log1p, dm_log2 => libm::log2,
            dm_log10 => libm::log10, dm_cbrt => libm::cbrt);
dm_scalar2!(dm_atan2 => libm::atan2, dm_pow => libm::pow, dm_hypot => libm::hypot);

// ---- small dense linear algebra for dnp.py (numpy's linalg is LAPACK / BLAS: CPU-specific kernels, FMA or not)

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2, PyUntypedArrayMethods};

/// x of a x = b (a (n, n), b (n, m)): Gaussian elimination with partial pivoting (the first row of the largest
/// |pivot| on a tie), row by row in a fixed order. None when a is singular (a zero pivot).
pub fn solve(mut a: Vec<f64>, n: usize, mut b: Vec<f64>, m: usize) -> Option<Vec<f64>> {
    for c in 0..n {
        let mut p = c;
        for r in c + 1..n {
            if a[r * n + c].abs() > a[p * n + c].abs() {
                p = r;
            }
        }
        if a[p * n + c] == 0.0 || !a[p * n + c].is_finite() {
            return None;
        }
        if p != c {
            for j in 0..n { a.swap(c * n + j, p * n + j); }
            for j in 0..m { b.swap(c * m + j, p * m + j); }
        }
        let d = a[c * n + c];
        for r in c + 1..n {
            let f = a[r * n + c] / d;
            if f == 0.0 { continue; }
            for j in c..n { a[r * n + j] -= f * a[c * n + j]; }
            for j in 0..m { b[r * m + j] -= f * b[c * m + j]; }
        }
    }
    for c in (0..n).rev() {
        for j in 0..m {
            let mut s = b[c * m + j];
            for k in c + 1..n { s -= a[c * n + k] * b[k * m + j]; }
            b[c * m + j] = s / a[c * n + c];
        }
    }
    Some(b)
}

/// The least-squares x of a x = b (a (r, c), r >= c, b (r, m)) by Householder QR, column by column; None when a
/// column is (numerically) dependent on the ones before it (|R_jj| <= rcond * max |R_ii|).
pub fn lstsq(mut a: Vec<f64>, r: usize, c: usize, mut b: Vec<f64>, m: usize, rcond: f64) -> Option<Vec<f64>> {
    if r < c { return None; }
    let mut rmax = 0.0f64;
    for j in 0..c {
        let mut norm = 0.0;
        for i in j..r { norm += a[i * c + j] * a[i * c + j]; }
        let norm = norm.sqrt();
        if norm == 0.0 { return None; }
        let alpha = if a[j * c + j] > 0.0 { -norm } else { norm };
        let mut v: Vec<f64> = (j..r).map(|i| a[i * c + j]).collect();
        v[0] -= alpha;
        let vv: f64 = v.iter().map(|x| x * x).sum();
        if vv > 0.0 {
            for k in j..c {
                let s: f64 = (j..r).map(|i| v[i - j] * a[i * c + k]).sum::<f64>() * 2.0 / vv;
                for i in j..r { a[i * c + k] -= s * v[i - j]; }
            }
            for k in 0..m {
                let s: f64 = (j..r).map(|i| v[i - j] * b[i * m + k]).sum::<f64>() * 2.0 / vv;
                for i in j..r { b[i * m + k] -= s * v[i - j]; }
            }
        }
        rmax = rmax.max(a[j * c + j].abs());
    }
    for j in 0..c {
        if a[j * c + j].abs() <= rcond * rmax { return None; }
    }
    let mut x = vec![0.0; c * m];
    for j in (0..c).rev() {
        for k in 0..m {
            let mut s = b[j * m + k];
            for t in j + 1..c { s -= a[j * c + t] * x[t * m + k]; }
            x[j * m + k] = s / a[j * c + j];
        }
    }
    Some(x)
}

/// Eigenvalues (ascending) and unit eigenvectors (columns, each with its largest |component| positive) of a symmetric
/// (n, n) matrix by cyclic Jacobi rotations in a fixed order.
pub fn sym_eig(mut a: Vec<f64>, n: usize) -> (Vec<f64>, Vec<f64>) {
    let mut v = vec![0.0; n * n];
    for i in 0..n { v[i * n + i] = 1.0; }
    for _sweep in 0..64 {
        let mut off = 0.0;
        for p in 0..n { for q in p + 1..n { off += a[p * n + q] * a[p * n + q]; } }
        if off == 0.0 { break; }
        for p in 0..n {
            for q in p + 1..n {
                let apq = a[p * n + q];
                if apq == 0.0 { continue; }
                let theta = (a[q * n + q] - a[p * n + p]) / (2.0 * apq);
                let t = theta.signum() / (theta.abs() + (theta * theta + 1.0).sqrt());
                let t = if theta == 0.0 { 1.0 } else { t };
                let cs = 1.0 / (t * t + 1.0).sqrt();
                let sn = t * cs;
                for k in 0..n {
                    let akp = a[k * n + p];
                    let akq = a[k * n + q];
                    a[k * n + p] = cs * akp - sn * akq;
                    a[k * n + q] = sn * akp + cs * akq;
                }
                for k in 0..n {
                    let apk = a[p * n + k];
                    let aqk = a[q * n + k];
                    a[p * n + k] = cs * apk - sn * aqk;
                    a[q * n + k] = sn * apk + cs * aqk;
                }
                for k in 0..n {
                    let vkp = v[k * n + p];
                    let vkq = v[k * n + q];
                    v[k * n + p] = cs * vkp - sn * vkq;
                    v[k * n + q] = sn * vkp + cs * vkq;
                }
            }
        }
    }
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by(|&i, &j| a[i * n + i].total_cmp(&a[j * n + j]).then(i.cmp(&j)));
    let w: Vec<f64> = order.iter().map(|&i| a[i * n + i]).collect();
    let mut vs = vec![0.0; n * n];
    for (c, &i) in order.iter().enumerate() {
        let mut big = 0usize;
        for k in 1..n { if v[k * n + i].abs() > v[big * n + i].abs() { big = k; } }
        let s = if v[big * n + i] < 0.0 { -1.0 } else { 1.0 };
        for k in 0..n { vs[k * n + c] = s * v[k * n + i]; }
    }
    (w, vs)
}

fn mat(a: &PyReadonlyArray2<f64>) -> (Vec<f64>, usize, usize) {
    let s = a.shape();
    (a.as_array().iter().copied().collect(), s[0], s[1])
}

/// dnp.solve: x of a x = b (b (n, m)); ValueError when a is singular.
#[pyfunction]
pub fn dm_solve<'py>(py: Python<'py>, a: PyReadonlyArray2<'py, f64>, b: PyReadonlyArray2<'py, f64>)
    -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (av, n, n2) = mat(&a);
    let (bv, nb, m) = mat(&b);
    if n != n2 || nb != n { return Err(PyValueError::new_err("dm_solve: a must be (n, n) and b (n, m)")); }
    let x = solve(av, n, bv, m).ok_or_else(|| PyValueError::new_err("dm_solve: singular matrix"))?;
    Ok(numpy::ndarray::Array2::from_shape_vec((n, m), x).unwrap().into_pyarray(py))
}

/// dnp.lstsq: the least-squares x of a x = b (b (r, m)), None when a's columns are dependent (rcond).
#[pyfunction]
pub fn dm_lstsq<'py>(py: Python<'py>, a: PyReadonlyArray2<'py, f64>, b: PyReadonlyArray2<'py, f64>, rcond: f64)
    -> PyResult<Option<Bound<'py, PyArray2<f64>>>> {
    let (av, r, c) = mat(&a);
    let (bv, rb, m) = mat(&b);
    if rb != r { return Err(PyValueError::new_err("dm_lstsq: a and b differ in rows")); }
    Ok(lstsq(av, r, c, bv, m, rcond).map(|x| numpy::ndarray::Array2::from_shape_vec((c, m), x).unwrap().into_pyarray(py)))
}

/// dnp.eigh: (eigenvalues ascending (n,), eigenvectors as columns (n, n)) of a symmetric matrix.
#[pyfunction]
pub fn dm_eigh<'py>(py: Python<'py>, a: PyReadonlyArray2<'py, f64>)
    -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray2<f64>>)> {
    let (av, n, n2) = mat(&a);
    if n != n2 { return Err(PyValueError::new_err("dm_eigh: a must be square")); }
    let (w, v) = sym_eig(av, n);
    Ok((PyArray1::from_vec(py, w), numpy::ndarray::Array2::from_shape_vec((n, n), v).unwrap().into_pyarray(py)))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(dm_vec, m)?)?;
    m.add_function(wrap_pyfunction!(dm_solve, m)?)?;
    m.add_function(wrap_pyfunction!(dm_lstsq, m)?)?;
    m.add_function(wrap_pyfunction!(dm_eigh, m)?)?;
    for f in [wrap_pyfunction!(dm_sin, m)?, wrap_pyfunction!(dm_cos, m)?, wrap_pyfunction!(dm_tan, m)?,
              wrap_pyfunction!(dm_asin, m)?, wrap_pyfunction!(dm_acos, m)?, wrap_pyfunction!(dm_atan, m)?,
              wrap_pyfunction!(dm_sinh, m)?, wrap_pyfunction!(dm_cosh, m)?, wrap_pyfunction!(dm_tanh, m)?,
              wrap_pyfunction!(dm_asinh, m)?, wrap_pyfunction!(dm_acosh, m)?, wrap_pyfunction!(dm_atanh, m)?,
              wrap_pyfunction!(dm_exp, m)?, wrap_pyfunction!(dm_expm1, m)?, wrap_pyfunction!(dm_log, m)?,
              wrap_pyfunction!(dm_log1p, m)?, wrap_pyfunction!(dm_log2, m)?, wrap_pyfunction!(dm_log10, m)?,
              wrap_pyfunction!(dm_cbrt, m)?, wrap_pyfunction!(dm_atan2, m)?, wrap_pyfunction!(dm_pow, m)?,
              wrap_pyfunction!(dm_hypot, m)?] {
        m.add_function(f)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::Dm;

    #[test]
    fn known_bits() {
        // a few values pinned to their libm-crate bits: a change of the crate's algorithms (or a platform call
        // creeping back in) shows here first
        assert_eq!(1.0f64.dsin().to_bits(), 0x3FEAED548F090CEE);
        assert_eq!(0.5f64.dhypot(0.25), 0.5590169943749475);
        assert_eq!(1.0f64.datan2(2.0), 0.4636476090008061);
        assert_eq!(2.0f64.dln(), std::f64::consts::LN_2);
    }
}
