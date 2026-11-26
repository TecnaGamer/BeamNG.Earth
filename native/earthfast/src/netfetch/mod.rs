//! Host admission state and cross-process request gates.
mod admission;
mod gate;
use pyo3::prelude::*;

const TOKEN_EPS: f64 = 1e-4;

fn token_wait(missing: f64, rate: f64) -> PyResult<f64> {
    if rate == 0.0 {
        return Err(pyo3::exceptions::PyZeroDivisionError::new_err(
            "float division by zero",
        ));
    }
    Ok(missing / rate)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<admission::NetBucket>()?;
    m.add_class::<admission::NetBudget>()?;
    m.add_class::<admission::NetBreaker>()?;
    m.add_class::<gate::NetGate>()?;
    Ok(())
}
