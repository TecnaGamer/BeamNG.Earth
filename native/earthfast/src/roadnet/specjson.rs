//! The road specs' stored form (roads._spec_json: the cell facts' "specs", the plans' openings) and its reading back
//! (roads.spec_from_json / _specs_from) in Rust, byte for byte the Python they replaced, under the R2a input contract
//! (roadnet's module notes): specs are exact roads.Spec tuples whose fields are None, exact bool / int / float / str,
//! exact numpy scalars, exact tuples or an exact europewalks.Street; stored data is what json.loads gives (exact list,
//! dict with str keys, str, int, float, bool, None). Anything else raises TypeError.

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple, PyType};

use super::pyobj::Contract;

/// _spec_json of each spec of specs (None for None): {field: value} in the fields' order, an exact bool / int / float
/// / str / None as it is, a numpy scalar as its item(), a tuple (sides, a Street) as list(v); "street" left out where
/// it is None.
#[pyfunction]
pub fn specs_json<'py>(py: Python<'py>, specs: Bound<'py, PyAny>, spec: Bound<'py, PyType>, street: Bound<'py, PyType>)
                       -> PyResult<Bound<'py, PyList>> {
    let k = Contract::new(py)?;
    let specs = specs.cast_exact::<PyList>().map_err(|_| pyo3::exceptions::PyTypeError::new_err(
        "specs_json: the specs are no exact list (the road spec contract)"))?;
    let fields: Vec<Bound<'py, PyString>> = spec.getattr("_fields")?.cast_into::<PyTuple>()?.iter()
        .map(|f| f.cast_into::<PyString>().map_err(PyErr::from)).collect::<PyResult<_>>()?;
    let street_at = fields.iter().position(|f| f.to_str().map(|s| s == "street").unwrap_or(false));
    let out = PyList::empty(py);
    for sp in specs.iter() {
        if sp.is_none() {
            out.append(&sp)?;
            continue;
        }
        if !sp.get_type().is(&spec) {
            return Err(refuse("a spec", &sp));
        }
        let t = sp.cast::<PyTuple>()?;
        let d = PyDict::new(py);
        for (j, (f, v)) in fields.iter().zip(t.iter()).enumerate() {
            let plain = v.is_none() || v.is_exact_instance_of::<pyo3::types::PyBool>()
                || v.is_exact_instance_of::<pyo3::types::PyInt>() || v.is_exact_instance_of::<pyo3::types::PyFloat>()
                || v.is_exact_instance_of::<PyString>();
            if plain {
                if Some(j) != street_at || !v.is_none() {
                    d.set_item(f, &v)?;
                }
            } else if k.scalar(&v) {
                d.set_item(f, v.call_method0("item")?)?;      // (a numpy scalar's item(): its Python number)
            } else if v.is_exact_instance_of::<PyTuple>() || v.get_type().is(&street) {
                d.set_item(f, PyList::new(py, v.cast::<PyTuple>()?.iter())?)?;
            } else {
                return Err(refuse("a spec's field", &v));
            }
        }
        out.append(d)?;
    }
    Ok(out)
}

fn refuse(what: &str, v: &Bound<'_, PyAny>) -> PyErr {
    let name = v.get_type().name().map(|s| s.to_string()).unwrap_or_else(|_| "?".to_owned());
    pyo3::exceptions::PyTypeError::new_err(format!("{what}: a {name} is outside the road spec contract"))
}

/// Whether v is of what json.loads gives: None, exact bool / int / float / str, and exact lists and dicts with str keys
/// of such, at any depth (an iterative walk: no depth cut-off, Codex on 7d348090; a container met twice, a cycle or a
/// shared one json.loads never makes, is refused).
fn json_value(v: &Bound<'_, PyAny>) -> bool {
    let mut stack = vec![v.clone()];
    let mut seen: std::collections::BTreeSet<usize> = std::collections::BTreeSet::new();
    while let Some(x) = stack.pop() {
        if x.is_none() || x.is_exact_instance_of::<pyo3::types::PyBool>() || x.is_exact_instance_of::<pyo3::types::PyInt>()
            || x.is_exact_instance_of::<pyo3::types::PyFloat>() || x.is_exact_instance_of::<PyString>()
        {
            continue;
        }
        if !seen.insert(x.as_ptr() as usize) {
            return false;
        }
        if let Ok(l) = x.cast_exact::<PyList>() {
            stack.extend(l.iter());
        } else if let Ok(d) = x.cast_exact::<PyDict>() {
            for (k, y) in d.iter() {
                if !k.is_exact_instance_of::<PyString>() {
                    return false;
                }
                stack.push(y);
            }
        } else {
            return false;
        }
    }
    true
}

/// spec_from_json of each stored spec of data (None for None): Spec(**{**d, "sides": tuple(d["sides"]) or None,
/// "street": Street.from_json(d["street"]) or None}): an unknown field or a missing one without a default
/// TypeError, a street not of 4 values ValueError, a walk of the wrong arity TypeError, as the Python raised them.
#[pyfunction]
pub fn specs_from_json<'py>(py: Python<'py>, data: Bound<'py, PyAny>, spec: Bound<'py, PyType>,
                            street: Bound<'py, PyType>, walk: Bound<'py, PyType>) -> PyResult<Bound<'py, PyList>> {
    let data = data.cast_exact::<PyList>().map_err(|_| pyo3::exceptions::PyTypeError::new_err(
        "specs_from_json: the stored specs are no exact list (the road spec contract)"))?;
    let fields: Vec<String> = spec.getattr("_fields")?.extract()?;
    let defaults = spec.getattr("_field_defaults")?.cast_into::<PyDict>()?;
    let tuple_new = py.get_type::<PyTuple>().getattr("__new__")?;
    let out = PyList::empty(py);
    for d in data.iter() {
        if d.is_none() {
            out.append(&d)?;
            continue;
        }
        if !d.is_exact_instance_of::<PyDict>() || !json_value(&d) {
            return Err(refuse("a stored spec", &d));
        }
        let d = d.cast_into::<PyDict>()?;
        // ({**d, "sides": ..., "street": ...}: the sides' tuple, then the street, made before Spec(**kw)'s checks)
        let sides = match d.get_item("sides")? {
            Some(s) if !s.is_none() => PyTuple::new(py, s.try_iter()?.collect::<PyResult<Vec<_>>>()?)?.into_any(),
            _ => py.None().into_bound(py),
        };
        let st = match d.get_item("street")? {
            Some(s) if !s.is_none() => street_from(py, &s, &street, &walk)?,
            _ => py.None().into_bound(py),
        };
        // (Spec(**kw)'s own checks: every key a field, every field without a default given)
        for (key, _) in d.iter() {
            let key = key.cast_into::<PyString>()?;
            if !fields.iter().any(|f| key.to_str().map(|k| k == f).unwrap_or(false)) {
                return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                    "Spec.__new__() got an unexpected keyword argument {}", key.repr()?)));
            }
        }
        let vals = PyList::empty(py);
        for f in &fields {
            let v = match f.as_str() {
                "sides" => sides.clone(),
                "street" => st.clone(),
                _ => match d.get_item(f)? {
                    Some(v) => v,
                    None => match defaults.get_item(f)? {
                        Some(v) => v,
                        None => {
                            return Err(pyo3::exceptions::PyTypeError::new_err(format!(
                                "Spec.__new__() missing required argument: '{f}'")));
                        }
                    },
                },
            };
            vals.append(v)?;
        }
        out.append(tuple_new.call1((&spec, vals))?)?;
    }
    Ok(out)
}

/// europewalks.Street.from_json(value): left, right, parking, village = value (exactly four: ValueError), Walk(*left)
/// / Walk(*right) where truthy, else None.
fn street_from<'py>(py: Python<'py>, value: &Bound<'py, PyAny>, street: &Bound<'py, PyType>, walk: &Bound<'py, PyType>)
                    -> PyResult<Bound<'py, PyAny>> {
    let v = super::pyobj::unpack(value, 4)?;
    let side = |w: &Bound<'py, PyAny>| -> PyResult<Bound<'py, PyAny>> {
        if w.is_truthy()? {
            walk.call1(PyTuple::new(py, w.try_iter()?.collect::<PyResult<Vec<_>>>()?)?)
        } else {
            Ok(py.None().into_bound(py))
        }
    };
    let (l, r) = (side(&v[0])?, side(&v[1])?);
    street.call1((l, r, &v[2], &v[3]))
}
