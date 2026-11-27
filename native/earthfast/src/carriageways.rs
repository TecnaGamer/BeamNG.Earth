//! Python boundary for the coupled profile solver. Validate and own inputs before releasing the GIL.
use crate::carriageway_solver::{self as solver, Coupling, Parameters, Problem};
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// coupling_rows: (n, 5) [chain a, sample, chain b, sample, kind] or (n, 7) with [chain c, sample] after them (chain
/// c -1: none); kind 0: z_a - z_b = 0, 1: >= value, 2: <= value. With c the coupled quantity is
/// z_a - (1 - alpha) z_b - alpha z_c (coupling_alpha, 0 without it): b's height moved alpha of the way to c.
/// parameters: (chains, 5) [grade, K, smoothness, step, length weight], the hard laws; or (chains, 9) with the soft
/// laws after them [comfortable grade, its cost, comfortable K, its cost]; or (chains, 10) with the bounds' cost after
/// those (0: hard; carriageway_solver::Parameters).
#[pyfunction]
#[pyo3(signature = (target, weight, lower, upper, offsets, parameters, coupling_rows, coupling_values, rounds, budget_s,
                    coupling_alpha = None, spacing = None, coupling_soft = None))]
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
    coupling_alpha: Option<PyReadonlyArray1<'py, f64>>,
    spacing: Option<PyReadonlyArray1<'py, f64>>,
    coupling_soft: Option<PyReadonlyArray1<'py, f64>>,
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
    let alpha = match &coupling_alpha {
        Some(a) => a.as_slice()?.to_vec(),
        None => vec![0.; values.len()],
    };
    // (an inequality coupling's softness: 0 hard, > 0 what a metre of its shortfall squared costs, halved)
    let soft = match &coupling_soft {
        Some(a) => a.as_slice()?.to_vec(),
        None => vec![0.; values.len()],
    };
    if soft.len() != values.len() || soft.iter().any(|v| !v.is_finite() || *v < 0.) {
        return Err(bad("carriageway coupling softness must be one finite nonnegative weight per coupling"));
    }
    let n = target.len();
    // (the distance of each sample to the next, per sample: a chain's last one unused; none: each chain's own step)
    let spacing = match &spacing {
        Some(a) => a.as_slice()?.to_vec(),
        None => Vec::new(),
    };
    if !spacing.is_empty() && (spacing.len() != n || spacing.iter().any(|v| !v.is_finite() || *v < 0.)) {
        return Err(bad("carriageway spacing must be one finite nonnegative distance per sample"));
    }
    if weight.len() != n
        || lower.len() != n
        || upper.len() != n
        || offsets.first() != Some(&0)
        || offsets.last().copied() != Some(n as i64)
        || offsets.windows(2).any(|w| w[1] < w[0])
        || offsets.iter().any(|&v| v < 0 || v > n as i64)
        || (params.ncols() != 5 && params.ncols() != 9 && params.ncols() != 10)
        || params.nrows() + 1 != offsets.len()
        || (rows.ncols() != 5 && rows.ncols() != 7)
        || rows.nrows() != values.len()
        || alpha.len() != values.len()
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
        let mut p = Parameters::hard(row[0], row[1], row[2], row[3], row[4]);
        if row.len() >= 9 {
            // (the soft laws: a comfortable grade and its cost, a comfortable curve's K and its cost; 0: none)
            if row[5] < 0. || row[6] < 0. || row[7] < 0. || row[8] < 0. {
                return Err(bad("invalid carriageway soft grade, grade cost, soft K or curve cost"));
            }
            p.soft_grade = row[5];
            p.w_grade = row[6];
            p.soft_k = row[7];
            p.w_curve = row[8];
        }
        if row.len() == 10 {
            // (the bounds' cost: 0 hard, > 0 a sample's shortfall paid for)
            if !(row[9] >= 0.) {
                return Err(bad("invalid carriageway bound cost"));
            }
            p.w_bound = row[9];
        }
        parameters.push(p);
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
        if !(0..=2).contains(&row[4]) || !values[i].is_finite() || !alpha[i].is_finite() {
            return Err(bad("invalid carriageway coupling"));
        }
        let b = sample(row[2], row[3])?;
        let c = if row.len() == 7 && row[5] >= 0 { sample(row[5], row[6])? } else { b };
        couplings.push(Coupling {
            a: sample(row[0], row[1])?,
            b,
            inequality: row[4] != 0,
            value: if row[4] != 0 { values[i] } else { 0. },
            c,
            alpha: if c == b { 0. } else { alpha[i] },
            upper: row[4] == 2,
            soft: if row[4] != 0 { soft[i] } else { 0. },
        });
    }
    let problem = Problem {
        target,
        weight,
        lower,
        upper,
        offsets: offsets.iter().map(|&v| v as usize).collect(),
        parameters,
        spacing,
        couplings,
    };
    let (z, d) = py
        .detach(|| solver::solve(&problem, rounds, budget_s))
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
    diagnostics.set_item("soft_grade_excess", d.soft_grade_excess)?;
    diagnostics.set_item("soft_curve_excess", d.soft_curve_excess)?;
    diagnostics.set_item("soft_cost", d.soft_cost)?;
    diagnostics.set_item("soft_clearance_shortfall_m", d.soft_clearance_shortfall)?;
    diagnostics.set_item("soft_bound_shortfall_m", d.soft_bound_shortfall)?;
    diagnostics.set_item("slack_vars", d.slack_vars)?;
    diagnostics.set_item("elapsed_s", d.elapsed_s)?;
    Ok((z.into_pyarray(py), diagnostics))
}
