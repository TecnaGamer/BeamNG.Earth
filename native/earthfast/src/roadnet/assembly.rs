//! The Spec tuples from the spec kernels' columns and back (roads._specs_of, dual_carriageways' and _bridge_specs'
//! columns of the ground specs), byte for byte the Python they replaced: the same values of the same Python types
//! (a kernel's float32 width as numpy.float32, the gap as numpy.float64, as list(array) handed them out).

use numpy::{IntoPyArray, PyArray1};
use super::pyobj::CArr1;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyTuple};

/// The kinds (roads._KINDS) and the sidewalk sides by the kernels' code (roads._SIDES_OF).
const KINDS: [&str; 4] = ["paved", "cobble", "gravel", "dirt"];

fn sides_of<'py>(py: Python<'py>, sd: i8) -> PyResult<Bound<'py, PyTuple>> {
    // ((1, -1), (1,), (-1,), ()), Python's negative index for -2 .. -4 (sd < 0 is None before this)
    let k = sd.rem_euclid(4) as usize;
    let v: &[i64] = [&[1, -1][..], &[1][..], &[-1][..], &[][..]][k];
    PyTuple::new(py, v)
}

/// Python's seq[k] for a k that may be negative (from the end); IndexError past either end.
fn pyidx<T: Clone>(seq: &[T], k: i64) -> PyResult<T> {
    let n = seq.len() as i64;
    let j = if k < 0 { n + k } else { k };
    if j < 0 || j >= n {
        return Err(pyo3::exceptions::PyIndexError::new_err("list index out of range"));
    }
    Ok(seq[j as usize].clone())
}

fn col<'py, T: numpy::Element + Copy>(d: &Bound<'py, PyDict>, k: &str) -> PyResult<Vec<T>> {
    let a: CArr1<T> = d.get_item(k)?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(k.to_owned()))?
        .extract()?;
    Ok(a.as_array().iter().copied().collect())
}

/// roads._specs_of(d, inp, states): each feature's Spec (spec, the namedtuple class) from the kernels' columns d, or
/// None where not present: width (numpy.float32 where w32), kind, urban, the class name, one-way, walk 2.5, dir,
/// kmh / gap (numpy.float64) / median (None for NaN), lanes (None < 0), the surface token (None < 0), link, sidewalk
/// sides (None < 0), parking, mountain, france False, the state.
#[pyfunction]
pub fn specs_of<'py>(py: Python<'py>, d: Bound<'py, PyDict>, names: Vec<String>, tokens: Vec<String>,
                     states: Bound<'py, PyAny>, spec: Bound<'py, PyAny>) -> PyResult<Bound<'py, PyList>> {
    let np = py.import("numpy")?;
    let (f32t, f64t) = (np.getattr("float32")?, np.getattr("float64")?);
    let present: Vec<bool> = col(&d, "present")?;
    let width: Vec<f64> = col(&d, "width")?;
    let w32: Vec<bool> = col(&d, "w32")?;
    let kind: Vec<i8> = col(&d, "kind")?;
    let urban: Vec<bool> = col(&d, "urban")?;
    let cls: Vec<i32> = col(&d, "cls")?;
    let oneway: Vec<bool> = col(&d, "oneway")?;
    let dir: Vec<i64> = col(&d, "dir")?;
    let kmh: Vec<f64> = col(&d, "kmh")?;
    let gap: Vec<f64> = col(&d, "gap")?;
    let median: Vec<f64> = col(&d, "median")?;
    let lanes: Vec<i64> = col(&d, "lanes")?;
    let surf: Vec<i32> = col(&d, "surf")?;
    let link: Vec<bool> = col(&d, "link")?;
    let sides: Vec<i8> = col(&d, "sides")?;
    let parking: Vec<bool> = col(&d, "parking")?;
    let mountain: Vec<bool> = col(&d, "mountain")?;
    let states: Vec<Bound<'py, PyAny>> = states.call_method0("tolist")?.cast_into::<PyList>()
        .map_err(|_| pyo3::exceptions::PyTypeError::new_err("specs_of: states"))?.iter().collect();
    // (zip: the shortest column)
    let n = [present.len(), width.len(), w32.len(), kind.len(), urban.len(), cls.len(), oneway.len(), dir.len(),
             kmh.len(), gap.len(), median.len(), lanes.len(), surf.len(), link.len(), sides.len(), parking.len(),
             mountain.len(), states.len()].into_iter().min().unwrap_or(0);
    let mut out: Vec<Bound<'py, PyAny>> = Vec::with_capacity(n);
    let none = py.None().into_bound(py);
    let nan_none = |v: f64| -> PyResult<Bound<'py, PyAny>> {
        Ok(if v.is_nan() { none.clone() } else { v.into_pyobject(py)?.into_any() })
    };
    for i in 0..n {
        if !present[i] {
            out.push(none.clone());
            continue;
        }
        let w = if w32[i] { f32t.call1((width[i],))? } else { width[i].into_pyobject(py)?.into_any() };
        let args: [Bound<'py, PyAny>; 18] = [
            w,
            pyidx(&KINDS, kind[i] as i64)?.into_pyobject(py)?.into_any(),
            urban[i].into_pyobject(py)?.to_owned().into_any(),
            pyidx(&names, cls[i] as i64)?.into_pyobject(py)?.into_any(),
            oneway[i].into_pyobject(py)?.to_owned().into_any(),
            2.5f64.into_pyobject(py)?.into_any(),
            dir[i].into_pyobject(py)?.into_any(),
            nan_none(kmh[i])?,
            if gap[i].is_nan() { none.clone() } else { f64t.call1((gap[i],))? },
            nan_none(median[i])?,
            if lanes[i] < 0 { none.clone() } else { lanes[i].into_pyobject(py)?.into_any() },
            if surf[i] < 0 { none.clone() } else { pyidx(&tokens, surf[i] as i64)?.into_pyobject(py)?.into_any() },
            link[i].into_pyobject(py)?.to_owned().into_any(),
            if sides[i] < 0 { none.clone() } else { sides_of(py, sides[i])?.into_any() },
            parking[i].into_pyobject(py)?.to_owned().into_any(),
            mountain[i].into_pyobject(py)?.to_owned().into_any(),
            false.into_pyobject(py)?.to_owned().into_any(),
            states[i].clone(),
        ];
        out.push(spec.call1(PyTuple::new(py, args)?)?);
    }
    PyList::new(py, out)
}

fn attr<'py>(sp: &Bound<'py, PyAny>, k: &str) -> PyResult<Bound<'py, PyAny>> {
    sp.getattr(k)
}

/// dual_carriageways' columns of the ground specs (each a Spec or None): {present, width (float(), NaN), kind (the
/// kind's code, -1), urban, cls (pos[sp.cls], -1), oneway, lanes (int(), -1), link, parking, median (float(), NaN),
/// dir (int(), 0)}.
#[pyfunction]
pub fn dual_cols<'py>(py: Python<'py>, specs: Vec<Bound<'py, PyAny>>, pos: Bound<'py, PyDict>)
                      -> PyResult<Bound<'py, PyDict>> {
    let n = specs.len();
    let mut present = vec![false; n];
    let mut width = vec![f64::NAN; n];
    let mut kind = vec![-1i8; n];
    let mut urban = vec![false; n];
    let mut cls = vec![-1i32; n];
    let mut oneway = vec![false; n];
    let mut lanes = vec![-1i64; n];
    let mut link = vec![false; n];
    let mut parking = vec![false; n];
    let mut median = vec![f64::NAN; n];
    let mut dir = vec![0i64; n];
    let fl = |v: Bound<'py, PyAny>| -> PyResult<f64> { super::pyobj::py_float_strict(&v) };
    let int = |v: Bound<'py, PyAny>| -> PyResult<i64> {
        py.get_type::<pyo3::types::PyInt>().call1((v,))?.extract::<i64>()
    };
    for (i, sp) in specs.iter().enumerate() {
        if sp.is_none() {
            continue;
        }
        present[i] = true;
        width[i] = fl(attr(sp, "width")?)?;
        let k = attr(sp, "kind")?;
        kind[i] = KINDS.iter().position(|x| k.eq(*x).unwrap_or(false)).map_or(-1, |p| p as i8);
        urban[i] = attr(sp, "urban")?.is_truthy()?;
        cls[i] = match pos.get_item(attr(sp, "cls")?)? {
            Some(v) => v.extract::<i32>()?,
            None => -1,
        };
        oneway[i] = attr(sp, "oneway")?.is_truthy()?;
        let ln = attr(sp, "lanes")?;
        lanes[i] = if ln.is_none() { -1 } else { int(ln)? };
        link[i] = attr(sp, "link")?.is_truthy()?;
        parking[i] = attr(sp, "parking")?.is_truthy()?;
        let md = attr(sp, "median")?;
        median[i] = if md.is_none() { f64::NAN } else { fl(md)? };
        dir[i] = int(attr(sp, "dir")?)?;
    }
    let d = PyDict::new(py);
    d.set_item("present", present.into_pyarray(py))?;
    d.set_item("width", width.into_pyarray(py))?;
    d.set_item("kind", kind.into_pyarray(py))?;
    d.set_item("urban", urban.into_pyarray(py))?;
    d.set_item("cls", cls.into_pyarray(py))?;
    d.set_item("oneway", oneway.into_pyarray(py))?;
    d.set_item("lanes", lanes.into_pyarray(py))?;
    d.set_item("link", link.into_pyarray(py))?;
    d.set_item("parking", parking.into_pyarray(py))?;
    d.set_item("median", median.into_pyarray(py))?;
    d.set_item("dir", dir.into_pyarray(py))?;
    Ok(d)
}

/// dual_carriageways' result on the specs: each changed one (d["changed"] 1: its width; 2: its median and width; 3:
/// one-way, its direction, width and median) _replace'd, its width the kernel's (d["width"]) unless d["w32"] keeps
/// its own.
#[pyfunction]
pub fn dual_apply<'py>(py: Python<'py>, specs: Vec<Bound<'py, PyAny>>, d: Bound<'py, PyDict>)
                       -> PyResult<Bound<'py, PyList>> {
    let changed: Vec<u8> = col(&d, "changed")?;
    let width: Vec<f64> = col(&d, "width")?;
    let kept: Vec<bool> = col(&d, "w32")?;
    let dirs: Vec<i64> = col(&d, "dir")?;
    let med: Vec<f64> = col(&d, "median")?;
    let m = changed.len();
    if specs.len() < m || width.len() < m || kept.len() < m || dirs.len() < m || med.len() < m {
        return Err(pyo3::exceptions::PyIndexError::new_err("dual_apply: the kernel's columns and the specs of other lengths"));
    }
    let mut out = specs.clone();
    for (i, &c) in changed.iter().enumerate() {
        if c == 0 {
            continue;
        }
        let sp = &specs[i];
        let w = if kept[i] { attr(sp, "width")? } else { width[i].into_pyobject(py)?.into_any() };
        let kw = PyDict::new(py);
        if c == 1 {
            kw.set_item("width", w)?;
        } else if c == 2 {
            kw.set_item("median", med[i])?;
            kw.set_item("width", w)?;
        } else {
            kw.set_item("oneway", true)?;
            kw.set_item("dir", dirs[i])?;
            kw.set_item("width", w)?;
            kw.set_item("median", med[i])?;
        }
        out[i] = sp.call_method("_replace", (), Some(&kw))?;
    }
    PyList::new(py, out)
}

/// _bridge_specs' columns of the ground specs: (gmed: the median where truthy, else NaN; gdir: dir, 0 for None).
#[pyfunction]
pub fn ground_med_dir<'py>(py: Python<'py>, ground: Vec<Bound<'py, PyAny>>)
                           -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<i64>>)> {
    let n = ground.len();
    let mut gmed = vec![f64::NAN; n];
    let mut gdir = vec![0i64; n];
    for (i, g) in ground.iter().enumerate() {
        if g.is_none() {
            continue;
        }
        let m = g.getattr("median")?;
        if m.is_truthy()? {
            gmed[i] = super::pyobj::py_float_strict(&m)?;
        }
        gdir[i] = py.get_type::<pyo3::types::PyInt>().call1((g.getattr("dir")?,))?.extract::<i64>()?;
    }
    Ok((gmed.into_pyarray(py), gdir.into_pyarray(py)))
}
