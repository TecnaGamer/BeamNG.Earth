//! Python boundary for the coupled profile solver. Validate and own inputs before releasing the GIL.
use crate::carriageway_solver::{self as solver, Coupling, Parameters, Problem};
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn carriageway_profiles<'py>(
    py: Python<'py>,
    target: PyReadonlyArray1<'py, f64>,
    weight: PyReadonlyArray1<'py, f64>,
    lower: PyReadonlyArray1<'py, f64>,
    upper: PyReadonlyArray1<'py, f64>,
    offsets: PyReadonlyArray1<'py, i64>,
    parameters: PyReadonlyArray2<'py, f64>,
    coupling_rows: PyReadonlyArray2<'py, i64>,
    coupling_values: PyReadonlyArray1<'py, f64>,
    rounds: usize,
    budget_s: f64,
) -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyDict>)> {
    let bad = |message: &str| PyValueError::new_err(message.to_owned());
    if rounds == 0 || budget_s.is_nan() || budget_s < 0. {
        return Err(bad(
            "carriageway rounds must be positive and budget nonnegative",
        ));
    }
    let target = target.as_slice()?.to_vec();
    let weight = weight.as_slice()?.to_vec();
    let mut lower = lower.as_slice()?.to_vec();
    let mut upper = upper.as_slice()?.to_vec();
    let offsets = offsets.as_slice()?;
    let params = parameters.as_array();
    let rows = coupling_rows.as_array();
    let values = coupling_values.as_slice()?;
    let n = target.len();
    if weight.len() != n
        || lower.len() != n
        || upper.len() != n
        || offsets.first() != Some(&0)
        || offsets.last().copied() != Some(n as i64)
        || offsets.windows(2).any(|w| w[1] < w[0])
        || offsets.iter().any(|&v| v < 0 || v > n as i64)
        || params.ncols() != 5
        || params.nrows() + 1 != offsets.len()
        || rows.ncols() != 5
        || rows.nrows() != values.len()
    {
        return Err(bad("inconsistent carriageway array shapes or offsets"));
    }
    if target.iter().chain(&weight).any(|v| !v.is_finite()) {
        return Err(bad("carriageway target heights and weights must be finite"));
    }
    for i in 0..n {
        if lower[i].is_nan() {
            lower[i] = f64::NEG_INFINITY;
        }
        if upper[i].is_nan() {
            upper[i] = f64::INFINITY;
        }
        if lower[i] == f64::INFINITY || upper[i] == f64::NEG_INFINITY {
            return Err(bad("invalid infinite carriageway bound"));
        }
    }
    let mut parameters = Vec::with_capacity(params.nrows());
    for row in params.rows() {
        if row.iter().any(|v| !v.is_finite())
            || row[0] < 0.
            || row[1] <= 0.
            || row[2] < 0.
            || row[3] <= 0.
            || row[4] < 0.
        {
            return Err(bad(
                "invalid carriageway grade, K, smoothness, step or length weight",
            ));
        }
        parameters.push(Parameters {
            grade: row[0],
            k: row[1],
            lam: row[2],
            step: row[3],
            length: row[4],
        });
    }
    let sample = |chain: i64, index: i64| -> PyResult<usize> {
        if chain < 0 || chain as usize >= parameters.len() {
            return Err(bad("invalid carriageway chain index"));
        }
        let a = offsets[chain as usize];
        let len = offsets[chain as usize + 1] - a;
        let i = if index < 0 { len + index } else { index };
        if i < 0 || i >= len {
            return Err(bad("invalid carriageway sample index"));
        }
        Ok((a + i) as usize)
    };
    let mut couplings = Vec::with_capacity(values.len());
    for (i, row) in rows.rows().into_iter().enumerate() {
        if !(0..=1).contains(&row[4]) || !values[i].is_finite() {
            return Err(bad("invalid carriageway coupling"));
        }
        couplings.push(Coupling {
            a: sample(row[0], row[1])?,
            b: sample(row[2], row[3])?,
            inequality: row[4] == 1,
            value: if row[4] == 1 { values[i] } else { 0. },
        });
    }
    let problem = Problem {
        target,
        weight,
        lower,
        upper,
        offsets: offsets.iter().map(|&v| v as usize).collect(),
        parameters,
        couplings,
    };
    let (z, d) = py
        .allow_threads(|| solver::solve(&problem, rounds, budget_s))
        .map_err(PyValueError::new_err)?;
    let diagnostics = PyDict::new(py);
    diagnostics.set_item("rounds", d.rounds)?;
    diagnostics.set_item("timed_out", d.timed_out)?;
    diagnostics.set_item("converged", d.converged)?;
    diagnostics.set_item("status", d.status)?;
    diagnostics.set_item("backend", d.backend)?;
    diagnostics.set_item("infeasible", d.infeasible)?;
    diagnostics.set_item("objective_nnz", d.objective_nnz)?;
    diagnostics.set_item("constraint_nnz", d.constraint_nnz)?;
    diagnostics.set_item("constraint_rows", d.constraint_rows)?;
    diagnostics.set_item("factor_nnz", d.factor_nnz)?;
    diagnostics.set_item("feasible", d.feasible)?;
    diagnostics.set_item("grade_excess", d.grade_excess)?;
    diagnostics.set_item("curve_excess", d.curve_excess)?;
    diagnostics.set_item("lower_shortfall_m", d.lower_shortfall)?;
    diagnostics.set_item("upper_excess_m", d.upper_excess)?;
    diagnostics.set_item("junction_gap_m", d.junction_gap)?;
    diagnostics.set_item("clearance_shortfall_m", d.clearance_shortfall)?;
    diagnostics.set_item("elapsed_s", d.elapsed_s)?;
    Ok((z.into_pyarray(py), diagnostics))
}
