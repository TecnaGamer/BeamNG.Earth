//! Reading a road layer's Python columns with Python's own semantics: str(v), float(v), str.strip(), str.lower(). The
//! columns are numpy arrays of objects, strings or numbers (mapgeom.Features.attrs); the ported glue read them with
//! these, so the kernels read them the same way (CPython's own calls where its rules are not plain ASCII).

use numpy::ndarray::Dimension;
use numpy::{Element, PyReadonlyArray, PyUntypedArray, PyUntypedArrayMethods};
use pyo3::prelude::*;
use pyo3::types::{PyFloat, PyList, PyString};
use pyo3::Borrowed;

/// col.tolist() (a numpy array's elements as Python objects, a list's own), None for no column.
pub fn items<'py>(col: Option<&Bound<'py, PyAny>>) -> PyResult<Option<Vec<Bound<'py, PyAny>>>> {
    let Some(col) = col else { return Ok(None) };
    if col.is_none() {
        return Ok(None);
    }
    let list = if col.hasattr("tolist")? { col.call_method0("tolist")? } else { col.clone() };
    let list = list.cast_into::<PyList>().map_err(|_| {
        pyo3::exceptions::PyTypeError::new_err("roadnet: a column that is not a sequence")
    })?;
    Ok(Some(list.iter().collect()))
}

/// str(v).
pub fn py_str(v: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(s) = v.cast::<PyString>() {
        return Ok(s.to_str()?.to_owned());
    }
    Ok(v.str()?.to_str()?.to_owned())
}

/// float(v): Some(value), None where Python raised TypeError or ValueError (not a number); any other error raised.
pub fn py_float(v: &Bound<'_, PyAny>) -> PyResult<Option<f64>> {
    if let Ok(f) = v.cast::<PyFloat>() {
        return Ok(Some(f.value()));
    }
    let py = v.py();
    match py.get_type::<PyFloat>().call1((v,)) {
        Ok(f) => Ok(Some(f.extract::<f64>()?)),
        Err(e) if e.is_instance_of::<pyo3::exceptions::PyTypeError>(py)
            || e.is_instance_of::<pyo3::exceptions::PyValueError>(py) => Ok(None),
        Err(e) => Err(e),
    }
}

/// float(v), every error raised.
pub fn py_float_strict(v: &Bound<'_, PyAny>) -> PyResult<f64> {
    if let Ok(f) = v.cast::<PyFloat>() {
        return Ok(f.value());
    }
    v.py().get_type::<PyFloat>().call1((v,))?.extract::<f64>()
}

/// Python's str.isspace() of one character: Unicode White_Space, and the separators U+001C-U+001F (bidirectional
/// class B / S), which Python counts and Rust's char::is_whitespace does not.
pub fn py_isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python's str.strip() (no argument).
pub fn py_strip(s: &str) -> &str {
    s.trim_matches(py_isspace)
}

/// Whether Python's s.lower() == key, for an ASCII key: only an ASCII s can lower to it (no other character lowers to
/// an ASCII letter but the Kelvin sign's "k", which no key here holds; checked by the callers' keys).
pub fn lower_is(s: &str, key: &str) -> bool {
    debug_assert!(key.is_ascii() && !key.contains('k'));
    s.is_ascii() && s.len() == key.len() && s.eq_ignore_ascii_case(key)
}

/// Python's unpacking of x into exactly n names (`a, b, c = x`, `for a, b, c in ...`): its values; ValueError unless it
/// holds n (its iterator asked for at most n + 1, as CPython's unpacking does), a non-iterable's TypeError.
pub fn unpack<'py>(x: &Bound<'py, PyAny>, n: usize) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let mut it = x.try_iter()?;
    let mut got = Vec::with_capacity(n);
    while got.len() < n {
        match it.next() {
            Some(v) => got.push(v?),
            None => {
                return Err(pyo3::exceptions::PyValueError::new_err(format!(
                    "not enough values to unpack (expected {n}, got {})", got.len())));
            }
        }
    }
    if let Some(v) = it.next() {
        v?;
        return Err(pyo3::exceptions::PyValueError::new_err(format!("too many values to unpack (expected {n})")));
    }
    Ok(got)
}

/// A numpy array argument as rust-numpy reads it rightly: C-contiguous and aligned (np.require(x, requirements="CA"),
/// numpy's own copy, where it is not). rust-numpy's views divide numpy's byte
/// strides by the item size, so a byte-strided view (a float64 (2, 2) of strides (24, 12)) was read at other bytes than
/// numpy's indexing reads (Codex on a6b93359); the copy holds the values numpy read. The dtype and dimension checks are
/// PyReadonlyArray's own.
pub struct CArr<'py, T: Element, D: Dimension>(pub PyReadonlyArray<'py, T, D>);
pub type CArr1<'py, T> = CArr<'py, T, numpy::Ix1>;
pub type CArr2<'py, T> = CArr<'py, T, numpy::Ix2>;

/// Whether u is C-contiguous and aligned (numpy's own flags), as np.require(x, requirements="CA") leaves it: the
/// kernels' as_slice reads it whole, as_array at numpy's offsets (a byte-strided view's strides, divided by the item
/// size, pointed elsewhere; a misaligned one rust-numpy refused).
fn sound(u: &Bound<'_, PyUntypedArray>) -> bool {
    // SAFETY: as_array_ptr is the live array object u holds; its flags field is read, nothing written.
    let aligned = unsafe { (*u.as_array_ptr()).fields.flags & numpy::npyffi::NPY_ARRAY_ALIGNED != 0 };
    aligned && u.is_c_contiguous()
}

impl<'a, 'py, T: Element + 'a, D: Dimension + 'a> FromPyObject<'a, 'py> for CArr<'py, T, D> {
    type Error = PyErr;

    fn extract(obj: Borrowed<'a, 'py, PyAny>) -> Result<Self, Self::Error> {
        let b = obj.to_owned();
        let b = match b.cast::<PyUntypedArray>() {
            Ok(u) if !sound(u) => {
                let py = b.py();
                let kw = pyo3::types::PyDict::new(py);
                kw.set_item("requirements", "CA")?;
                py.import("numpy")?.getattr("require")?.call((&b,), Some(&kw))?
            }
            _ => b,
        };
        Ok(CArr(b.extract()?))
    }
}

impl<'py, T: Element, D: Dimension> std::ops::Deref for CArr<'py, T, D> {
    type Target = PyReadonlyArray<'py, T, D>;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}
