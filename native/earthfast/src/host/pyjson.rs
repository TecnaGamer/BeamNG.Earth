//! pyjson (the Rust port's phase 2a, plan section 2.3): JSON text with exactly the bytes of Python's
//! `json.dumps(v, separators=(",", ":"), ensure_ascii=False)`, so that messages made in Rust and those made in Python are
//! the same frames:
//!   - keys in insertion order (a dict's own order; a message built in Rust: its fields' order);
//!   - floats as Python's repr: the shortest digits that read back the same, written as Python writes them (`1e-05`,
//!     `1e+16`, `1.0`, `-0.0`), `NaN` / `Infinity` / `-Infinity`;
//!   - strings as real UTF-8 with `"`, `\`, `\n`, `\r`, `\t`, `\b`, `\f` escaped and the other characters below 0x20
//!     as `\u00xx`.
//! What this does not handle itself (a dict whose keys are not all str, a dict subclass, an int past i64, a value of
//! another type, a nesting past MAX_DEPTH) is handed to Python's json.dumps for that value, so the text and the errors
//! stay Python's own.
//!
//! `round_py` is Python's round(x, n) for a float (correctly rounded, ties to even: Rust's fixed formatting rounds the
//! same way, and the text reads back to the nearest double as Python's strtod does).
//!
//! `J` is a message value built in Rust: encoded straight to frame bytes (phase 2b: the far state machine) or turned
//! into the Python objects Python's `send` takes now (phase 2a: Connection.send, its lease / closing / buffer checks and
//! the tests' captured messages unchanged).
use std::fmt::Write as _;

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

/// nesting handled here; deeper values go to json.dumps (which finds a circular reference, or hits Python's limit)
const MAX_DEPTH: usize = 64;

/// A float as Python's repr writes it (float_repr_style "short": PyOS_double_to_string(x, 'r', 0, ADD_DOT_0)).
pub fn push_f64(out: &mut Vec<u8>, x: f64) {
    if x.is_nan() {
        out.extend_from_slice(b"NaN");
        return;
    }
    if x.is_infinite() {
        out.extend_from_slice(if x > 0.0 { b"Infinity" } else { b"-Infinity" });
        return;
    }
    if x == 0.0 {
        out.extend_from_slice(if x.is_sign_negative() { b"-0.0" } else { b"0.0" });
        return;
    }
    // Rust's LowerExp without a precision is the shortest round-trip digits (as Python's repr: David Gay's mode 0)
    let mut s = String::with_capacity(32);
    let _ = write!(s, "{:e}", x.abs());
    // ... but its last digit is not always the closest one (Grisu): Python's is the correctly rounded value at that
    // length whenever that reads back the same (at a power of two's lopsided interval it may not: the shortest then)
    let n = s.find('e').unwrap_or(s.len()) - usize::from(s.contains('.'));
    let mut t = String::with_capacity(32);
    let _ = write!(t, "{:.*e}", n.saturating_sub(1), x.abs());
    if t.parse::<f64>() == Ok(x.abs()) {
        s = t;
    }
    let (mant, exp) = s.split_once('e').unwrap_or((&s, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    let digits: Vec<u8> = mant.bytes().filter(|&b| b != b'.').collect();
    let n = digits.len() as i32;
    let decpt = exp + 1; // the decimal point's place after the first digit
    if x < 0.0 {
        out.push(b'-');
    }
    if decpt <= -4 || decpt > 16 {
        out.push(digits[0]);
        if n > 1 {
            out.push(b'.');
            out.extend_from_slice(&digits[1..]);
        }
        out.push(b'e');
        out.push(if exp < 0 { b'-' } else { b'+' });
        let e = exp.unsigned_abs();
        if e < 10 {
            out.push(b'0');
        }
        out.extend_from_slice(e.to_string().as_bytes());
    } else if decpt <= 0 {
        out.extend_from_slice(b"0.");
        out.extend(std::iter::repeat(b'0').take((-decpt) as usize));
        out.extend_from_slice(&digits);
    } else if decpt >= n {
        out.extend_from_slice(&digits);
        out.extend(std::iter::repeat(b'0').take((decpt - n) as usize));
        out.extend_from_slice(b".0");
    } else {
        out.extend_from_slice(&digits[..decpt as usize]);
        out.push(b'.');
        out.extend_from_slice(&digits[decpt as usize..]);
    }
}

pub fn push_i64(out: &mut Vec<u8>, v: i64) {
    out.extend_from_slice(v.to_string().as_bytes());
}

/// A string as json.dumps(ensure_ascii=False) writes it (py_encode_basestring).
pub fn push_str(out: &mut Vec<u8>, s: &str) {
    out.push(b'"');
    let b = s.as_bytes();
    let mut start = 0;
    for (i, &c) in b.iter().enumerate() {
        let esc: &[u8] = match c {
            b'"' => b"\\\"",
            b'\\' => b"\\\\",
            b'\n' => b"\\n",
            b'\r' => b"\\r",
            b'\t' => b"\\t",
            0x08 => b"\\b",
            0x0c => b"\\f",
            0..=0x1f => b"",
            _ => continue,
        };
        out.extend_from_slice(&b[start..i]);
        if esc.is_empty() {
            const HEX: &[u8; 16] = b"0123456789abcdef";
            out.extend_from_slice(b"\\u00");
            out.push(HEX[(c >> 4) as usize]);
            out.push(HEX[(c & 15) as usize]);
        } else {
            out.extend_from_slice(esc);
        }
        start = i + 1;
    }
    out.extend_from_slice(&b[start..]);
    out.push(b'"');
}

/// Python's round(x, n) for a float, n >= 0 (the message fields' 1-3 digits).
pub fn round_py(x: f64, n: usize) -> f64 {
    if !x.is_finite() || n > 300 {
        return x;
    }
    let s = format!("{:.*}", n, x);
    s.parse().unwrap_or(x)
}

/// Python's round(x) (no digits) for a float: an int, ties to even; None past i64 (Python's own call then).
pub fn round_int_py(x: f64) -> Option<i64> {
    let r = x.round_ties_even();
    if r.is_finite() && r.abs() < 9.0e18 {
        Some(r as i64)
    } else {
        None
    }
}

/// Python's round(v, n): a float in Rust; anything else (an int, numpy's float64 with its own __round__) by Python.
pub fn round_obj<'py>(v: &Bound<'py, PyAny>, n: usize) -> PyResult<Bound<'py, PyAny>> {
    let py = v.py();
    if v.is_exact_instance_of::<PyFloat>() {
        let x: f64 = v.cast::<PyFloat>()?.value();
        return Ok(PyFloat::new(py, round_py(x, n)).into_any());
    }
    py.import("builtins")?.getattr("round")?.call1((v, n))
}

/// Python's round(v) (an int for a float).
pub fn round0_obj<'py>(v: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
    let py = v.py();
    if v.is_exact_instance_of::<PyFloat>() {
        if let Some(i) = round_int_py(v.cast::<PyFloat>()?.value()) {
            return Ok(i.into_pyobject(py)?.into_any());
        }
    }
    py.import("builtins")?.getattr("round")?.call1((v,))
}

/// json.dumps's own text for v (what this encoder leaves to Python).
fn dumps_py(out: &mut Vec<u8>, v: &Bound<'_, PyAny>) -> PyResult<()> {
    let py = v.py();
    let kw = PyDict::new(py);
    kw.set_item("separators", (",", ":"))?;
    kw.set_item("ensure_ascii", false)?;
    let s = py.import("json")?.getattr("dumps")?.call((v,), Some(&kw))?;
    out.extend_from_slice(s.cast::<PyString>()?.to_str()?.as_bytes());
    Ok(())
}

/// v as json.dumps writes it, appended to out.
pub fn push_py(out: &mut Vec<u8>, v: &Bound<'_, PyAny>, depth: usize) -> PyResult<()> {
    if v.is_none() {
        out.extend_from_slice(b"null");
        return Ok(());
    }
    if let Ok(b) = v.cast::<PyBool>() {
        out.extend_from_slice(if b.is_true() { b"true" } else { b"false" });
        return Ok(());
    }
    if let Ok(s) = v.cast::<PyString>() {
        push_str(out, s.to_str()?);
        return Ok(());
    }
    if v.cast::<PyInt>().is_ok() {
        return match v.extract::<i64>() {
            Ok(i) => {
                push_i64(out, i);
                Ok(())
            }
            Err(_) => dumps_py(out, v),
        };
    }
    if let Ok(f) = v.cast::<PyFloat>() {
        push_f64(out, f.value());
        return Ok(());
    }
    if depth >= MAX_DEPTH {
        return dumps_py(out, v);
    }
    // (only the built-in containers themselves: a subclass may iterate or order differently, json.dumps's own then)
    if v.is_exact_instance_of::<PyList>() {
        let l = v.cast::<PyList>()?;
        out.push(b'[');
        // (by index, as json's encoder reads a list: an item the list loses meanwhile cannot happen under the GIL)
        for (i, item) in l.iter().enumerate() {
            if i > 0 {
                out.push(b',');
            }
            push_py(out, &item, depth + 1)?;
        }
        out.push(b']');
        return Ok(());
    }
    if v.is_exact_instance_of::<PyTuple>() {
        let t = v.cast::<PyTuple>()?;
        out.push(b'[');
        for (i, item) in t.iter().enumerate() {
            if i > 0 {
                out.push(b',');
            }
            push_py(out, &item, depth + 1)?;
        }
        out.push(b']');
        return Ok(());
    }
    if v.is_exact_instance_of::<PyDict>() {
        let d = v.cast::<PyDict>()?;
        if !d.keys().iter().all(|k| k.is_exact_instance_of::<PyString>()) {
            return dumps_py(out, v);
        }
        out.push(b'{');
        for (i, (k, item)) in d.iter().enumerate() {
            if i > 0 {
                out.push(b',');
            }
            push_str(out, k.cast::<PyString>()?.to_str()?);
            out.push(b':');
            push_py(out, &item, depth + 1)?;
        }
        out.push(b'}');
        return Ok(());
    }
    dumps_py(out, v)
}

/// A message's frame: the 4-byte LE length, then the payload as protocol.encode made it: json.dumps of the message
/// with the values of type `raw` (protocol.RawJson: a file's JSON text already) spliced in after the other fields, as
/// they are.
#[pyfunction]
#[pyo3(signature = (msg, raw = None))]
pub fn encode_frame<'py>(py: Python<'py>, msg: &Bound<'py, PyAny>, raw: Option<&Bound<'py, PyAny>>)
    -> PyResult<Bound<'py, PyBytes>> {
    let mut out = vec![0u8; 4];
    let is_raw = |v: &Bound<'py, PyAny>| raw.is_some_and(|r| v.get_type().is(r));
    // (a dict subclass with RawJson values: an exact dict of its items() in their order, as Python's encode read it)
    let copy;
    let dict = if msg.is_exact_instance_of::<PyDict>() {
        msg.cast::<PyDict>().ok()
    } else if let Some(d) = msg.cast::<PyDict>().ok().filter(|d| d.values().iter().any(|v| is_raw(&v))) {
        copy = PyDict::new(py);
        for kv in d.call_method0("items")?.try_iter()? {
            let (k, v): (Bound<'py, PyAny>, Bound<'py, PyAny>) = kv?.extract()?;
            copy.set_item(k, v)?;
        }
        Some(&copy)
    } else {
        None
    };
    match dict {
        Some(d) if d.values().iter().any(|v| is_raw(&v)) => {
            if !d.keys().iter().all(|k| k.is_exact_instance_of::<PyString>()) {
                // (the rest as Python's own encode: json.dumps of a dict of the other fields)
                let rest = PyDict::new(py);
                for (k, v) in d.iter() {
                    if !is_raw(&v) {
                        rest.set_item(k, v)?;
                    }
                }
                let mut head = Vec::new();
                dumps_py(&mut head, rest.as_any())?;
                head.pop();
                out.extend_from_slice(&head);
                let mut first = rest.is_empty();
                for (k, v) in d.iter() {
                    if is_raw(&v) {
                        if !first {
                            out.push(b',');
                        }
                        first = false;
                        dumps_py(&mut out, &k)?;
                        out.push(b':');
                        out.extend_from_slice(v.getattr("text")?.cast::<PyString>()?.to_str()?.as_bytes());
                    }
                }
                out.push(b'}');
            } else {
                out.push(b'{');
                let mut first = true;
                for pass_raw in [false, true] {
                    for (k, v) in d.iter() {
                        if is_raw(&v) != pass_raw {
                            continue;
                        }
                        if !first {
                            out.push(b',');
                        }
                        first = false;
                        push_str(&mut out, k.cast::<PyString>()?.to_str()?);
                        out.push(b':');
                        if pass_raw {
                            out.extend_from_slice(v.getattr("text")?.cast::<PyString>()?.to_str()?.as_bytes());
                        } else {
                            push_py(&mut out, &v, 1)?;
                        }
                    }
                }
                out.push(b'}');
            }
        }
        _ => push_py(&mut out, msg, 0)?,
    }
    let n = (out.len() - 4) as u32;
    out[..4].copy_from_slice(&n.to_le_bytes());
    Ok(PyBytes::new(py, &out))
}

/// json.dumps(v, separators=(",", ":"), ensure_ascii=False) (the conformance tests; tile JSON files later).
#[pyfunction]
pub fn pyjson_dumps(v: &Bound<'_, PyAny>) -> PyResult<String> {
    let mut out = Vec::new();
    push_py(&mut out, v, 0)?;
    // (only valid UTF-8 went in: str's text, ASCII, json.dumps's own str)
    String::from_utf8(out).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
}

/// Python's round(x, n) for a float (the conformance tests).
#[pyfunction]
pub fn pyjson_round(x: f64, n: usize) -> f64 {
    round_py(x, n)
}

/// A message value built in Rust.
pub enum J {
    Null,
    Bool(bool),
    Int(i64),
    F(f64),
    S(String),
    A(Vec<J>),
    O(Vec<(&'static str, J)>),
    /// a Python value passed through as it is (encoded as json.dumps would, or handed back unchanged)
    Py(Py<PyAny>),
}

impl J {
    pub fn of(v: &Bound<'_, PyAny>) -> J {
        J::Py(v.clone().unbind())
    }

    /// The frame payload's bytes (phase 2b's messages straight to the Wire).
    pub fn encode(&self, py: Python<'_>, out: &mut Vec<u8>) -> PyResult<()> {
        match self {
            J::Null => out.extend_from_slice(b"null"),
            J::Bool(b) => out.extend_from_slice(if *b { b"true" } else { b"false" }),
            J::Int(i) => push_i64(out, *i),
            J::F(x) => push_f64(out, *x),
            J::S(s) => push_str(out, s),
            J::A(a) => {
                out.push(b'[');
                for (i, v) in a.iter().enumerate() {
                    if i > 0 {
                        out.push(b',');
                    }
                    v.encode(py, out)?;
                }
                out.push(b']');
            }
            J::O(o) => {
                out.push(b'{');
                for (i, (k, v)) in o.iter().enumerate() {
                    if i > 0 {
                        out.push(b',');
                    }
                    push_str(out, k);
                    out.push(b':');
                    v.encode(py, out)?;
                }
                out.push(b'}');
            }
            J::Py(v) => push_py(out, v.bind(py), 1)?,
        }
        Ok(())
    }

    /// The Python value (a dict in the fields' order for an object).
    pub fn to_py<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyAny>> {
        Ok(match self {
            J::Null => py.None().into_bound(py),
            J::Bool(b) => PyBool::new(py, *b).to_owned().into_any(),
            J::Int(i) => i.into_pyobject(py)?.into_any(),
            J::F(x) => PyFloat::new(py, *x).into_any(),
            J::S(s) => PyString::new(py, s).into_any(),
            J::A(a) => {
                let l = PyList::empty(py);
                for v in a {
                    l.append(v.to_py(py)?)?;
                }
                l.into_any()
            }
            J::O(o) => {
                let d = PyDict::new(py);
                for (k, v) in o {
                    d.set_item(*k, v.to_py(py)?)?;
                }
                d.into_any()
            }
            J::Py(v) => v.bind(py).clone(),
        })
    }
}

/// J's encoding of a message built in Python's objects (the conformance tests: J::encode against json.dumps).
#[pyfunction]
pub fn pyjson_dumps_j(v: &Bound<'_, PyAny>) -> PyResult<String> {
    fn to_j(v: &Bound<'_, PyAny>) -> PyResult<J> {
        if v.is_exact_instance_of::<PyDict>() {
            let d = v.cast::<PyDict>()?;
            let mut o = Vec::new();
            for (k, item) in d.iter() {
                // (a test's keys: leaked to the 'static fields a message's builder names)
                let k: &'static str = Box::leak(k.extract::<String>()?.into_boxed_str());
                o.push((k, to_j(&item)?));
            }
            return Ok(J::O(o));
        }
        if v.is_exact_instance_of::<PyList>() {
            return Ok(J::A(v.cast::<PyList>()?.iter().map(|x| to_j(&x)).collect::<PyResult<_>>()?));
        }
        if v.is_exact_instance_of::<PyFloat>() {
            return Ok(J::F(v.cast::<PyFloat>()?.value()));
        }
        if v.is_exact_instance_of::<PyString>() {
            return Ok(J::S(v.extract()?));
        }
        Ok(J::of(v))
    }
    let py = v.py();
    let mut out = Vec::new();
    to_j(v)?.encode(py, &mut out)?;
    String::from_utf8(out).map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn f(x: f64) -> String {
        let mut o = Vec::new();
        push_f64(&mut o, x);
        String::from_utf8(o).unwrap()
    }

    #[test]
    fn floats_as_python_repr() {
        assert_eq!(f(1.0), "1.0");
        assert_eq!(f(-0.0), "-0.0");
        assert_eq!(f(1e16), "1e+16");
        assert_eq!(f(1e15), "1000000000000000.0");
        assert_eq!(f(1.5e-5), "1.5e-05");
        assert_eq!(f(0.0001), "0.0001");
        assert_eq!(f(0.1 + 0.2), "0.30000000000000004");
        assert_eq!(f(123.456), "123.456");
        assert_eq!(f(-2.5e100), "-2.5e+100");
        assert_eq!(f(5e-324), "5e-324");
        assert_eq!(f(f64::NAN), "NaN");
        assert_eq!(f(f64::NEG_INFINITY), "-Infinity");
        assert_eq!(f(1234567890123456.7), "1234567890123456.8");
    }

    #[test]
    fn strings_as_python_escapes() {
        let mut o = Vec::new();
        push_str(&mut o, "a\"b\\c\nd\u{1}\u{7f}é\u{2028}");
        assert_eq!(String::from_utf8(o).unwrap(), "\"a\\\"b\\\\c\\nd\\u0001\u{7f}é\u{2028}\"");
    }

    #[test]
    fn round_ties_to_even_on_the_exact_value() {
        assert_eq!(round_py(0.125, 2), 0.12);
        assert_eq!(round_py(2.675, 2), 2.67);
        assert_eq!(round_py(-0.0001, 2).to_bits(), (-0.0f64).to_bits());
        assert_eq!(round_int_py(2.5), Some(2));
        assert_eq!(round_int_py(-3.5), Some(-4));
    }
}
