//! mapgeom.text_column in Rust (the full Rust port: 2.2 CPU-s of Python self-time over 7.7k calls in a cold first
//! visit's profile, the decode's text attributes), byte for byte the numpy array the Python made:
//! np.array(["" if x is None else str(x) for x in values], dtype=f"<U{width}"), width the longest's code points (1 at
//! least). An exact str is read as it is; anything else through str() (its own __str__, as the list comprehension
//! asked it), one value at a time in order, every str() before any length; a __str__ returning a str subclass takes
//! the Python's own len() and np.array steps. Text with lone surrogates (no UTF-8) is read through Python's UTF-32
//! encoding (surrogatepass), as numpy stores its code points.

use numpy::PyArrayMethods;
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
    // v = ["" if x is None else str(x) for x in values]: every str() asked first, in order
    let mut strs: Vec<Option<Bound<'py, PyString>>> = Vec::new();
    let mut exotic = false;
    for x in values.try_iter()? {
        let x = x?;
        strs.push(if x.is_none() {
            None
        } else if let Ok(s) = x.cast_exact::<PyString>() {
            Some(s.clone())
        } else {
            let s = x.str()?;
            exotic |= !s.is_exact_instance_of::<PyString>();
            Some(s)
        });
    }
    if exotic {
        // a __str__ returned a str subclass (its own len(), numpy's conversion of it): the Python's own steps,
        // width = max([1] + [len(x) for x in v]), np.array(v, dtype=f"<U{width}")
        let empty = PyString::new(py, "");
        let v = pyo3::types::PyList::empty(py);
        for s in &strs {
            v.append(s.as_ref().unwrap_or(&empty))?;
        }
        let mut width = 1usize;
        for s in v.iter() {
            width = width.max(s.len()?);
        }
        let np = py.import("numpy")?;
        let kw = pyo3::types::PyDict::new(py);
        kw.set_item("dtype", format!("<U{width}"))?;
        return np.call_method("array", (v,), Some(&kw));
    }
    let mut cps: Vec<u32> = Vec::new();
    let mut lens: Vec<usize> = Vec::with_capacity(strs.len());
    for s in &strs {
        lens.push(match s { Some(s) => code_points(s, &mut cps)?, None => 0 });
    }
    let width = lens.iter().copied().max().unwrap_or(0).max(1);
    // np.zeros(n, "<U{width}"): numpy's own allocation (MemoryError, ValueError for a size past its limits), the array
    // the Python's np.array made (its own data), its code points written through a uint32 view of it
    let n = lens.len();
    let np = py.import("numpy")?;
    let out = np.call_method1("zeros", (n, format!("<U{width}")))?;
    let view = out.call_method1("view", (np.getattr("uint32")?,))?;
    let view = view.cast::<numpy::PyArray1<u32>>()?;
    let mut rw = view.try_readwrite().map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
    let buf = rw.as_slice_mut()?;
    let mut at = 0usize;
    for (k, &m) in lens.iter().enumerate() {
        buf[k * width..k * width + m].copy_from_slice(&cps[at..at + m]);
        at += m;
    }
    drop(rw);
    Ok(out)
}
