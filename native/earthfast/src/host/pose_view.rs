//! The visual focus can follow the camera. Other vehicles only add collision foci.
use pyo3::exceptions::{PyKeyError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyTuple};

type Obj<'py> = Bound<'py, PyAny>;

fn finite(value: Option<Obj<'_>>) -> PyResult<Option<f64>> {
    let Some(value) = value else { return Ok(None) };
    if value.is_instance_of::<PyBool>()
        || !(value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>())
    {
        return Ok(None);
    }
    Ok(Some(value.extract::<f64>()?).filter(|n| n.is_finite()))
}

#[pyfunction]
pub(super) fn pose_camera(
    cam: &Obj<'_>,
    focus: &Obj<'_>,
    away: f64,
) -> PyResult<Option<(f64, f64)>> {
    let Ok(cam) = cam.cast::<PyDict>() else {
        return Ok(None);
    };
    let mut x = cam.get_item("tx")?;
    let mut y = cam.get_item("ty")?;
    if x.as_ref().is_none_or(|v| v.is_none()) || y.as_ref().is_none_or(|v| v.is_none()) {
        x = cam.get_item("x")?;
        y = cam.get_item("y")?;
    }
    let (Some(x), Some(y)) = (finite(x)?, finite(y)?) else {
        return Ok(None);
    };
    let (fx, fy) = focus.extract::<(f64, f64)>()?;
    Ok((crate::py_hypot(x - fx, y - fy) > away).then_some((x, y)))
}

#[pyfunction]
pub(super) fn pose_foci<'py>(
    raw: &Obj<'py>,
    limit: usize,
    range: f64,
    max_radius: f64,
) -> PyResult<Bound<'py, PyTuple>> {
    let py = raw.py();
    let mut out = Vec::new();
    if let Ok(list) = raw.cast::<PyList>() {
        let float = py.get_type::<PyFloat>();
        for f in list.iter().take(limit) {
            let num = |key| float.call1((f.get_item(key)?,))?.extract::<f64>();
            let coords = || -> PyResult<_> { Ok((num("x")?, num("y")?, num("r")?)) };
            let (x, y, r) = match coords() {
                Ok(v) => v,
                Err(e)
                    if e.is_instance_of::<PyTypeError>(py)
                        || e.is_instance_of::<PyKeyError>(py)
                        || e.is_instance_of::<PyValueError>(py) =>
                {
                    continue
                }
                Err(e) => return Err(e),
            };
            if x.is_finite()
                && y.is_finite()
                && r.is_finite()
                && x.abs().max(y.abs()) <= range
                && r > 0.
            {
                out.push((
                    x,
                    y,
                    r.min(max_radius),
                    f.call_method1("get", ("trees",))?.is_truthy()?,
                ));
            }
        }
    }
    PyTuple::new(py, out)
}

#[pyfunction]
pub(super) fn pose_eye(
    c: &Obj<'_>,
    frame: &Obj<'_>,
    x: f64,
    y: f64,
    msg: &Bound<'_, PyDict>,
    globals: &Bound<'_, PyDict>,
) -> PyResult<()> {
    let global = globals.as_any();
    let cam = msg
        .get_item("cam")?
        .and_then(|v| v.cast_into::<PyDict>().ok());
    let cam_z = match cam {
        Some(cam) => cam.get_item("z")?,
        None => None,
    };
    let z = match finite(cam_z)? {
        Some(z) => z,
        None => {
            global
                .get_item("_num")?
                .call1((msg, "z", 0.0))?
                .extract::<f64>()?
                + global.get_item("EYE_ABOVE_PLAYER")?.extract::<f64>()?
        }
    };
    let h: f64 = frame
        .call_method1("enu_to_geodetic", (x, y, z))?
        .get_item(2)?
        .extract()?;
    c.setattr("eye_now", h)?;
    let old = c.getattr("eye")?.extract::<Option<f64>>()?;
    let step = global.get_item("EYE_STEP")?.extract::<f64>()?;
    let rel = global.get_item("EYE_STEP_REL")?.extract::<f64>()?;
    if old.is_none_or(|eye| (h - eye).abs() > step.max(rel * eye.abs())) {
        c.setattr("eye", h)?;
    }
    Ok(())
}
