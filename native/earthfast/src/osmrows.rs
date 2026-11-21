//! osmtags._rows and tags_and_ways' tag dicts in Rust (the full Rust port: the OpenStreetMap tag join's Python, the
//! largest coherent Python share of a cold first visit after the one-way joins, 2026-11-18), byte for byte the Python
//! they replaced.
//!
//! Input contract (R2a, roadnet::pyobj::Contract): the decoded row group an exact dict of exact numpy arrays as
//! OsmTags._decode / the tag shards make them: "id" / "frn" int64, "race" / "bus" bool, a text column's bytes uint8 with
//! "<c>_off" int32 / int64 offsets and "<c>_ok" bool, a number column float32 / float64; idx an int64 array. Anything
//! else TypeError.

use std::collections::BTreeMap;

use numpy::{PyArrayDescrMethods, PyUntypedArray, PyUntypedArrayMethods};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

use crate::roadnet::countries::np_index;
use crate::roadnet::pyobj::CArr1;

/// One column of the table, as read.
enum Col<'py> {
    U8(CArr1<'py, u8>),
    I32(CArr1<'py, i32>),
    I64(CArr1<'py, i64>),
    F32(CArr1<'py, f32>),
    F64(CArr1<'py, f64>),
    /// a bool column's bytes (a uint8 view of it: numpy reads any nonzero byte as True, a Rust bool must be 0 or 1)
    Bool(CArr1<'py, u8>),
}

impl<'py> Col<'py> {
    fn of(v: &Bound<'py, PyAny>, key: &str) -> PyResult<Col<'py>> {
        let refuse = || pyo3::exceptions::PyTypeError::new_err(format!(
            "osm_rows: the column {key:?} is outside the row group contract"));
        let np = v.py().import("numpy")?;
        if !v.get_type().is(&np.getattr("ndarray")?) {
            return Err(refuse());
        }
        let u = v.cast::<PyUntypedArray>()?;
        if u.ndim() != 1 {
            return Err(refuse());
        }
        let d = u.dtype();
        Ok(match (d.kind() as char, d.itemsize()) {
            ('u', 1) => Col::U8(v.extract()?),
            ('i', 4) => Col::I32(v.extract()?),
            ('i', 8) => Col::I64(v.extract()?),
            ('f', 4) => Col::F32(v.extract()?),
            ('f', 8) => Col::F64(v.extract()?),
            ('b', 1) => Col::Bool(v.call_method1("view", (np.getattr("uint8")?,))?.extract()?),
            _ => return Err(refuse()),
        })
    }

    fn len(&self) -> usize {
        match self {
            Col::U8(a) => a.len(),
            Col::I32(a) => a.len(),
            Col::I64(a) => a.len(),
            Col::F32(a) => a.len(),
            Col::F64(a) => a.len(),
            Col::Bool(a) => a.len(),
        }
    }

    /// int(col[i]) of an integer column (numpy's index rules), TypeError for another dtype.
    fn int(&self, i: i64) -> PyResult<i64> {
        let k = np_index(self.len(), i)?;
        match self {
            Col::I32(a) => Ok(a.as_slice()?[k] as i64),
            Col::I64(a) => Ok(a.as_slice()?[k]),
            _ => Err(pyo3::exceptions::PyTypeError::new_err("osm_rows: an integer column of another dtype")),
        }
    }

    /// int(col[i]) of an int64 column (id, frn), TypeError for another dtype.
    fn int64(&self, i: i64) -> PyResult<i64> {
        let k = np_index(self.len(), i)?;
        match self {
            Col::I64(a) => Ok(a.as_slice()?[k]),
            _ => Err(pyo3::exceptions::PyTypeError::new_err("osm_rows: id / frn of another dtype than int64 (the contract)")),
        }
    }

    /// float(col[i]) of a float column.
    fn float(&self, i: i64) -> PyResult<f64> {
        let k = np_index(self.len(), i)?;
        match self {
            Col::F32(a) => Ok(a.as_slice()?[k] as f64),
            Col::F64(a) => Ok(a.as_slice()?[k]),
            _ => Err(pyo3::exceptions::PyTypeError::new_err("osm_rows: a number column of another dtype")),
        }
    }

    /// bool(col[i]) of a bool column.
    fn flag(&self, i: i64) -> PyResult<bool> {
        let k = np_index(self.len(), i)?;
        match self {
            Col::Bool(a) => Ok(a.as_slice()?[k] != 0),
            _ => Err(pyo3::exceptions::PyTypeError::new_err("osm_rows: a flag column of another dtype")),
        }
    }
}

/// Python's slice bounds of a sequence of len n: [a:b] with a negative counted from the end, both clamped to 0..n.
fn slice_bounds(n: usize, a: i64, b: i64) -> (usize, usize) {
    let n = n as i64;
    let fix = |v: i64| -> i64 {
        let v = if v < 0 { v + n } else { v };
        v.clamp(0, n)
    };
    let (a, b) = (fix(a), fix(b));
    (a as usize, if b > a { b as usize } else { a as usize })
}

/// What a column holds (the contract's dtype of it).
#[derive(Clone, Copy)]
enum Role {
    /// a text column's offsets: int32 / int64
    Off,
    /// race, bus, a text column's _ok: bool
    Flag,
    /// a text column's UTF-8: uint8
    Bytes,
    /// id, frn: int64
    Int64,
    /// a number column: float32 / float64
    Num,
}

/// t[key] looked up at its first use (KeyError where it is missing), its dtype checked against its role there
/// (TypeError, whether or not a value of it is read later), kept.
fn fetch<'py>(t: &Bound<'py, PyDict>, cols: &mut BTreeMap<String, Col<'py>>, key: &str, role: Role) -> PyResult<()> {
    if !cols.contains_key(key) {
        let v = t.get_item(key)?.ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(key.to_owned()))?;
        let c = Col::of(&v, key)?;
        let fits = matches!((role, &c), (Role::Off, Col::I32(_) | Col::I64(_)) | (Role::Flag, Col::Bool(_))
            | (Role::Bytes, Col::U8(_)) | (Role::Int64, Col::I64(_)) | (Role::Num, Col::F32(_) | Col::F64(_)));
        if !fits {
            return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                "osm_rows: the column {key:?} is of another dtype than the row group contract's")));
        }
        cols.insert(key.to_owned(), c);
    }
    Ok(())
}

/// osmtags._rows(t, idx): per row i of idx, (int(id), bool(race), bool(bus), int(frn), *COLUMNS' values): a text
/// column's UTF-8 bytes t[c][off[i]:off[i + 1]] decoded (strict) where t[c + "_ok"][i], else None; a number column's
/// float, None for NaN, int() of it for an INT_COLUMNS one. The columns read as the Python read them (each key looked up
/// at its first use: a missing one KeyError there).
#[pyfunction]
pub fn osm_rows<'py>(py: Python<'py>, t: Bound<'py, PyAny>, idx: Bound<'py, PyAny>, names: Vec<String>,
                     text: Vec<String>, ints: Vec<String>) -> PyResult<Bound<'py, PyList>> {
    if !idx.get_type().is(&py.import("numpy")?.getattr("ndarray")?) {
        return Err(pyo3::exceptions::PyTypeError::new_err("osm_rows: idx is no exact int64 ndarray (the contract)"));
    }
    let idx: CArr1<'py, i64> = idx.extract()?;
    let t = t.cast_exact::<PyDict>().map_err(|_| pyo3::exceptions::PyTypeError::new_err(
        "osm_rows: the row group is no exact dict (the contract)"))?.clone();
    let mut cols: BTreeMap<String, Col<'py>> = BTreeMap::new();
    let is_text: Vec<bool> = names.iter().map(|c| text.contains(c)).collect();
    let is_int: Vec<bool> = names.iter().map(|c| ints.contains(c)).collect();
    let out = PyList::empty(py);
    for &i in idx.as_slice()? {
        let mut vals: Vec<Bound<'py, PyAny>> = Vec::with_capacity(names.len() + 4);
        for (j, c) in names.iter().enumerate() {
            if is_text[j] {
                let off = format!("{c}_off");
                let ok = format!("{c}_ok");
                fetch(&t, &mut cols, &off, Role::Off)?;
                fetch(&t, &mut cols, &ok, Role::Flag)?;
                if cols[&ok].flag(i)? {
                    fetch(&t, &mut cols, c, Role::Bytes)?;
                    let o = &cols[&off];
                    let (a, b) = (o.int(i)?, o.int(i.wrapping_add(1))?);
                    let Col::U8(data) = &cols[c] else {
                        return Err(pyo3::exceptions::PyTypeError::new_err("osm_rows: text bytes of another dtype"));
                    };
                    let bytes = data.as_slice()?;
                    let (a, b) = slice_bounds(bytes.len(), a, b);
                    let s = &bytes[a..b];
                    match std::str::from_utf8(s) {
                        Ok(v) => vals.push(pyo3::types::PyString::new(py, v).into_any()),
                        Err(e) => return Err(PyErr::from_value(
                            pyo3::exceptions::PyUnicodeDecodeError::new_utf8(py, s, e)?.into_any())),
                    }
                } else {
                    vals.push(py.None().into_bound(py));
                }
            } else {
                fetch(&t, &mut cols, c, Role::Num)?;
                let v = cols[c].float(i)?;
                if v.is_nan() {
                    vals.push(py.None().into_bound(py));
                } else if is_int[j] {
                    if v.is_infinite() {
                        return Err(pyo3::exceptions::PyOverflowError::new_err(
                            "cannot convert float infinity to integer"));
                    }
                    // int(v): exact for any finite double (Python's int of it, as an i128 or bigger)
                    vals.push(py.get_type::<pyo3::types::PyInt>().call1((v,))?);
                } else {
                    vals.push(pyo3::types::PyFloat::new(py, v).into_any());
                }
            }
        }
        // (int(t["id"][i]), bool(t["race"][i]), bool(t["bus"][i]), int(t["frn"][i]), *vals): each looked up and read
        // in turn
        let mut row: Vec<Bound<'py, PyAny>> = Vec::with_capacity(names.len() + 4);
        fetch(&t, &mut cols, "id", Role::Int64)?;
        row.push(cols["id"].int64(i)?.into_pyobject(py)?.into_any());
        fetch(&t, &mut cols, "race", Role::Flag)?;
        row.push(pyo3::types::PyBool::new(py, cols["race"].flag(i)?).to_owned().into_any());
        fetch(&t, &mut cols, "bus", Role::Flag)?;
        row.push(pyo3::types::PyBool::new(py, cols["bus"].flag(i)?).to_owned().into_any());
        fetch(&t, &mut cols, "frn", Role::Int64)?;
        row.push(cols["frn"].int64(i)?.into_pyobject(py)?.into_any());
        row.extend(vals);
        out.append(PyTuple::new(py, row)?)?;
    }
    Ok(out)
}

/// tags_and_ways' tag dicts: {int(r[0]): {name: value for name, value in zip(names, r[4:]) if value is not None}} of
/// rows (osm_rows' tuples), in their order (a later row of a repeated id replaces the earlier one in place).
#[pyfunction]
pub fn osm_tags<'py>(py: Python<'py>, rows: Bound<'py, PyList>, names: Vec<String>) -> PyResult<Bound<'py, PyDict>> {
    let keys: Vec<Bound<'py, pyo3::types::PyString>> = names.iter().map(|n| pyo3::types::PyString::new(py, n)).collect();
    let out = PyDict::new(py);
    for r in rows.iter() {
        let r = r.cast_exact::<PyTuple>().map_err(|_| pyo3::exceptions::PyTypeError::new_err(
            "osm_tags: a row is no exact tuple (the contract)"))?.clone();
        let id = py.get_type::<pyo3::types::PyInt>().call1((r.get_item(0)?,))?;
        let d = PyDict::new(py);
        for (k, v) in keys.iter().zip(r.iter().skip(4)) {
            if !v.is_none() {
                d.set_item(k, v)?;
            }
        }
        out.set_item(id, d)?;
    }
    Ok(out)
}
