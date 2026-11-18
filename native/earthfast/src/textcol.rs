//! mapgeom.text_column in Rust (the full Rust port: 2.2 CPU-s of Python self-time over 7.7k calls in a cold first
//! visit's profile, the decode's text attributes), byte for byte the numpy array the Python made:
//! np.array(["" if x is None else str(x) for x in values], dtype=f"<U{width}"), width the longest's code points (1 at
//! least). An exact str is read as it is; anything else through str() (its own __str__, as the list comprehension
//! asked it), one value at a time in order. Text with lone surrogates (no UTF-8) is read through Python's UTF-32
//! encoding (surrogatepass), as numpy stores its code points.

use numpy::IntoPyArray;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyString};

/// The code points of s.
fn code_points(s: &Bound<'_, PyString>, out: &mut Vec<u32>) -> PyResult<usize> {
    match s.to_str() {
        Ok(v) => {
            let n0 = out.len();
            out.extend(v.chars().map(|c| c as u32));
            Ok(out.len() - n0)
        }
        Err(_) => {
            let b = s.call_method1("encode", ("utf-32-le", "surrogatepass"))?;
            let b = b.cast::<PyBytes>()?.as_bytes();
            let n = b.len() / 4;
            out.extend(b.chunks_exact(4).map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]])));
            Ok(n)
        }
    }
}

#[pyfunction]
pub fn text_column<'py>(py: Python<'py>, values: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let mut cps: Vec<u32> = Vec::new();
    let mut lens: Vec<usize> = Vec::new();
    for x in values.try_iter()? {
        let x = x?;
        let n = if x.is_none() {
            0
        } else if let Ok(s) = x.cast_exact::<PyString>() {
            code_points(s, &mut cps)?
        } else {
            let s = x.str()?;
            code_points(&s, &mut cps)?
        };
        lens.push(n);
    }
    let width = lens.iter().copied().max().unwrap_or(0).max(1);
    let mut buf = vec![0u32; lens.len() * width];
    let mut at = 0usize;
    for (k, &n) in lens.iter().enumerate() {
        buf[k * width..k * width + n].copy_from_slice(&cps[at..at + n]);
        at += n;
    }
    let arr = numpy::ndarray::Array2::from_shape_vec((lens.len(), width), buf).expect("text column").into_pyarray(py);
    let np = py.import("numpy")?;
    let dt = np.call_method1("dtype", (format!("<U{width}"),))?;
    // (a (n, width) uint32 buffer viewed as n "<U{width}" values, copied: an array of its own, as np.array made)
    let n = lens.len();
    let u = arr.as_any().call_method1("view", (dt,))?.call_method1("reshape", (n,))?;
    u.call_method0("copy")
}
