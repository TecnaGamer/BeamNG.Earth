//! Reading a road layer's Python columns with Python's own semantics: str(v), float(v), str.strip(), str.lower(). The
//! columns are numpy arrays of objects, strings or numbers (mapgeom.Features.attrs); the ported glue read them with
//! these, so the kernels read them the same way (CPython's own calls where its rules are not plain ASCII).

use numpy::ndarray::Dimension;
use numpy::{Element, PyReadonlyArray, PyUntypedArray, PyUntypedArrayMethods};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFloat, PyList, PyString};
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

/// str(v) (an exact str read as it is; a subclass, which may override __str__, through str(): Codex on bc34313f).
pub fn py_str(v: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(s) = v.cast_exact::<PyString>() {
        return Ok(s.to_str()?.to_owned());
    }
    Ok(v.str()?.to_str()?.to_owned())
}

/// float(v): Some(value), None where Python raised TypeError or ValueError (not a number); any other error raised. An
/// exact float read as it is; anything else, a float subclass too (an overridden __float__, one that raises), through
/// float() (Codex on bc34313f).
pub fn py_float(v: &Bound<'_, PyAny>) -> PyResult<Option<f64>> {
    if let Ok(f) = v.cast_exact::<PyFloat>() {
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

/// float(v), every error raised (an exact float read as it is, anything else through float()).
pub fn py_float_strict(v: &Bound<'_, PyAny>) -> PyResult<f64> {
    if let Ok(f) = v.cast_exact::<PyFloat>() {
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

/// What a generator expression's body raised, as the generator raises it: a StopIteration becomes RuntimeError
/// ("generator raised StopIteration", its cause the StopIteration: PEP 479); anything else as it was.
pub fn in_generator<T>(py: Python<'_>, r: PyResult<T>) -> PyResult<T> {
    match r {
        Err(e) if e.is_instance_of::<pyo3::exceptions::PyStopIteration>(py) => {
            let re = pyo3::exceptions::PyRuntimeError::new_err("generator raised StopIteration");
            re.set_cause(py, Some(e));
            Err(re)
        }
        other => other,
    }
}

/// The R2a input contract (the road revamp's port of the spec glue; roadnet's module notes): a kernel that takes a
/// road layer's columns takes them as the pipeline makes them, and refuses anything else with a TypeError before it
/// reads a row, rather than following every object protocol a Python value could override. A column is None, an exact
/// list or tuple, or an exact 1-D numpy.ndarray of a number, text or bytes dtype ("biufUS") or of objects ("O"); each
/// item of a list, tuple or object array is None, an exact bool / int / float / str, or an exact numpy scalar (bool_,
/// int8 .. uint64, float16 .. float64, str_, bytes_). Subclasses, proxies and other objects are refused.
pub struct Contract<'py> {
    ndarray: Bound<'py, pyo3::types::PyType>,
    scalars: Vec<Bound<'py, pyo3::types::PyType>>,
}

impl<'py> Contract<'py> {
    pub fn new(py: Python<'py>) -> PyResult<Self> {
        let np = py.import("numpy")?;
        let mut scalars = Vec::new();
        for t in ["bool_", "int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64", "float16",
                  "float32", "float64", "str_", "bytes_"] {
            scalars.push(np.getattr(t)?.cast_into::<pyo3::types::PyType>()?);
        }
        Ok(Contract { ndarray: np.getattr("ndarray")?.cast_into::<pyo3::types::PyType>()?, scalars })
    }

    /// Whether v is one of the contract's scalars.
    pub fn scalar(&self, v: &Bound<'py, PyAny>) -> bool {
        v.is_none() || v.is_exact_instance_of::<pyo3::types::PyBool>() || v.is_exact_instance_of::<pyo3::types::PyInt>()
            || v.is_exact_instance_of::<PyFloat>() || v.is_exact_instance_of::<PyString>()
            || self.scalars.iter().any(|t| v.get_type().is(t))
    }

    fn refuse(what: &str, got: &Bound<'py, PyAny>) -> PyErr {
        let name = got.get_type().name().map(|s| s.to_string()).unwrap_or_else(|_| "?".to_owned());
        pyo3::exceptions::PyTypeError::new_err(format!(
            "{what}: a {name} is outside the road layer contract (None, an exact list / tuple or 1-D numpy array of \
             numbers, text or objects whose items are None, bool, int, float, str or numpy scalars)"))
    }

    /// The rows of a list, tuple or 1-D numpy array of objects (None for a number / text array: no item to check), or
    /// a TypeError.
    fn rows(&self, col: &Bound<'py, PyAny>, what: &str, objects_only: bool) -> PyResult<Option<Vec<Bound<'py, PyAny>>>> {
        if let Ok(l) = col.cast_exact::<PyList>() {
            return Ok(Some(l.iter().collect()));
        }
        if let Ok(t) = col.cast_exact::<pyo3::types::PyTuple>() {
            return Ok(Some(t.iter().collect()));
        }
        if col.get_type().is(&self.ndarray) {
            use numpy::{PyArrayDescrMethods, PyUntypedArrayMethods};
            let a = col.cast::<PyUntypedArray>()?;
            let kind = a.dtype().kind() as char;
            if a.ndim() == 1 && kind == 'O' {
                return Ok(Some(col.call_method0("tolist")?.cast_into::<PyList>()?.iter().collect()));
            }
            if a.ndim() == 1 && !objects_only && "biufUS".contains(kind) {
                return Ok(None);
            }
        }
        Err(Self::refuse(what, col))
    }

    /// A column of the contract (None too), else TypeError.
    pub fn column(&self, col: &Bound<'py, PyAny>, what: &str) -> PyResult<()> {
        if col.is_none() {
            return Ok(());
        }
        for v in self.rows(col, what, false)?.unwrap_or_default() {
            if !self.scalar(&v) {
                return Err(Self::refuse(what, &v));
            }
        }
        Ok(())
    }

    /// An exact dict whose keys are exact str and values the contract's scalars, else TypeError.
    fn tags(&self, d: &Bound<'py, PyAny>, what: &str) -> PyResult<()> {
        let d = d.cast_exact::<PyDict>().map_err(|_| Self::refuse(what, d))?;
        for (k, v) in d.iter() {
            if !k.is_exact_instance_of::<PyString>() {
                return Err(Self::refuse(what, &k));
            }
            if !self.scalar(&v) {
                return Err(Self::refuse(what, &v));
            }
        }
        Ok(())
    }

    /// The lane parts column (LANE_PARTS): None, or a list / tuple / 1-D object array whose rows are None, a scalar, an
    /// exact dict (not read), or an exact list / tuple of parts; a part None, a scalar, an exact dict (not read), or an
    /// exact list / tuple of scalars and exact dicts of str keys and scalar values. Else TypeError.
    pub fn parts(&self, col: &Bound<'py, PyAny>, what: &str) -> PyResult<()> {
        if col.is_none() {
            return Ok(());
        }
        let seq = |v: &Bound<'py, PyAny>| v.is_exact_instance_of::<PyList>() || v.is_exact_instance_of::<pyo3::types::PyTuple>();
        for row in self.rows(col, what, true)?.unwrap_or_default() {
            if self.scalar(&row) || row.is_exact_instance_of::<PyDict>() {
                continue;
            }
            if !seq(&row) {
                return Err(Self::refuse(what, &row));
            }
            for p in row.try_iter()? {
                let p = p?;
                if self.scalar(&p) || p.is_exact_instance_of::<PyDict>() {
                    continue;
                }
                if !seq(&p) {
                    return Err(Self::refuse(what, &p));
                }
                for x in p.try_iter()? {
                    let x = x?;
                    if x.is_exact_instance_of::<PyDict>() {
                        self.tags(&x, what)?;
                    } else if !self.scalar(&x) {
                        return Err(Self::refuse(what, &x));
                    }
                }
            }
        }
        Ok(())
    }
}

/// A str the contract allows, as text to compare with ASCII keys: str(v) with a lone surrogate replaced (U+FFFD:
/// such a text equals no ASCII key either way).
pub fn key_str(v: &Bound<'_, PyAny>) -> PyResult<String> {
    if let Ok(s) = v.cast_exact::<PyString>() {
        return Ok(s.to_string_lossy().into_owned());
    }
    Ok(v.str()?.to_string_lossy().into_owned())
}
