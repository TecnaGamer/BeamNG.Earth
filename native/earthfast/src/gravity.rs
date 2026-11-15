//! gravity.EGM2008._row_sums: the per-degree sums over the order m of the fully normalised associated Legendre
//! functions (recursion with column rescaling) times the coefficient rows, for one direction (t = sin, u = cos of the
//! geocentric latitude, lam the longitude). The Python loop over EGM2008's 2,191 degrees took 37.8 CPU-s of the server
//! process per 15 minutes, under its one interpreter lock (.claude/work/rust_port_plan.md 6a, 2026-11-07). Same
//! recursion, rescale points and zonal terms; the row products summed in f64 (Python: in the coefficients' dtype).
use crate::dm::Dm;
use numpy::{IntoPyArray, PyArray2, PyArray3, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

#[allow(clippy::too_many_arguments)]
fn row_sums<C: Copy + Into<f64>>(t: f64, u: f64, lam: f64, ra: &[f64], rb: &[f64], coef: &[C], ncol: usize,
                                 off: &[i64], sect_next: &[f64], log_sect0: &[f64], every: usize, rescale: f64,
                                 nzonal: usize, nmax: usize) -> (Vec<f64>, Vec<f64>) {
    let n1 = nmax + 1;
    let lu = u.dln();
    let mut log_s: Vec<f64> = (0..n1).map(|m| log_sect0[m] + m as f64 * lu).collect();
    let w_of = |ls: f64| if ls > -650.0 { ls.max(-650.0).dexp() } else { 0.0 };
    let mut w: Vec<f64> = log_s.iter().map(|&ls| w_of(ls)).collect();
    let cm: Vec<f64> = (0..n1).map(|m| (m as f64 * lam).dcos()).collect();
    let sm: Vec<f64> = (0..n1).map(|m| (m as f64 * lam).dsin()).collect();
    let mut wc: Vec<f64> = (0..n1).map(|m| w[m] * cm[m]).collect();
    let mut wsn: Vec<f64> = (0..n1).map(|m| w[m] * sm[m]).collect();
    let mut out = vec![0.0f64; n1 * 2 * ncol];
    let nzon = nzonal.min(nmax);
    let mut zon = vec![0.0f64; (nzon + 1) * 2];
    let (mut p2, mut p1, mut pn) = (vec![0.0f64; n1], vec![0.0f64; n1], vec![0.0f64; n1]);
    let mut acc = vec![0.0f64; 2 * ncol];
    for n in 0..n1 {
        let o = off[n] as usize;
        let k = n + 1;
        if n >= 2 {
            let j = n - 1;
            for m in 0..j {
                pn[m] = ra[o + m] * p1[m] * t - rb[o + m] * p2[m];
            }
        }
        if n >= 1 {
            pn[n - 1] = sect_next[n - 1] * t * p1[n - 1];
        }
        pn[n] = 1.0;
        if n > 0 && n % every == 0 {
            for m in 0..k {
                let mut mag = pn[m].abs();
                if m < n {
                    mag = mag.max(p1[m].abs());
                }
                if mag > rescale {
                    pn[m] /= mag;
                    if m < n {
                        p1[m] /= mag;
                    }
                    log_s[m] += mag.dln();
                    w[m] = w_of(log_s[m]);
                    wc[m] = w[m] * cm[m];
                    wsn[m] = w[m] * sm[m];
                }
            }
        }
        acc.iter_mut().for_each(|a| *a = 0.0);
        for m in 0..k {
            let yc = pn[m] * wc[m];
            let ys = pn[m] * wsn[m];
            if yc == 0.0 && ys == 0.0 {
                continue;
            }
            let row = &coef[(o + m) * ncol..(o + m + 1) * ncol];
            for c in 0..ncol {
                let v: f64 = row[c].into();
                acc[c] += yc * v;
                acc[ncol + c] += ys * v;
            }
        }
        out[n * 2 * ncol..(n + 1) * 2 * ncol].copy_from_slice(&acc);
        if n <= nzon {
            zon[n * 2] = pn[0] * w[0];
            zon[n * 2 + 1] = if n > 0 { pn[1] * w[1] } else { 0.0 };
        }
        std::mem::swap(&mut p2, &mut p1);
        std::mem::swap(&mut p1, &mut pn);
    }
    (out, zon)
}

macro_rules! row_sums_py {
    ($name:ident, $t:ty) => {
        /// gravity.EGM2008._row_sums in Rust: (out (nmax+1, 2, ncol) f64, zon (min(nmax, nzonal)+1, 2) f64).
        #[pyfunction]
        #[allow(clippy::too_many_arguments)]
        pub fn $name<'py>(py: Python<'py>, t: f64, u: f64, lam: f64, ra: PyReadonlyArray1<'py, f64>,
                          rb: PyReadonlyArray1<'py, f64>, coef: PyReadonlyArray2<'py, $t>,
                          off: PyReadonlyArray1<'py, i64>, sect_next: PyReadonlyArray1<'py, f64>,
                          log_sect: PyReadonlyArray1<'py, f64>, every: usize, rescale: f64, nzonal: usize,
                          nmax: usize) -> PyResult<(Bound<'py, PyArray3<f64>>, Bound<'py, PyArray2<f64>>)> {
            let (ra, rb, off) = (ra.as_slice()?, rb.as_slice()?, off.as_slice()?);
            let (sn, ls) = (sect_next.as_slice()?, log_sect.as_slice()?);
            let ca = coef.as_array();
            let ncol = ca.ncols();
            let cs = coef.as_slice()?;
            if off.len() < nmax + 2 || ra.len() < off[nmax + 1] as usize || cs.len() < off[nmax + 1] as usize * ncol
                || sn.len() < nmax + 1 || ls.len() < nmax + 1 || every == 0 {
                return Err(pyo3::exceptions::PyValueError::new_err("gravity_row_sums: inconsistent tables"));
            }
            let (out, zon) = py.detach(|| row_sums(t, u, lam, ra, rb, cs, ncol, off, sn, ls, every, rescale,
                                                          nzonal, nmax));
            let nz = zon.len() / 2;
            let a3 = numpy::ndarray::Array3::from_shape_vec((nmax + 1, 2, ncol), out).unwrap();
            let a2 = numpy::ndarray::Array2::from_shape_vec((nz, 2), zon).unwrap();
            Ok((a3.into_pyarray(py), a2.into_pyarray(py)))
        }
    };
}

row_sums_py!(gravity_row_sums_f32, f32);
row_sums_py!(gravity_row_sums_f64, f64);
