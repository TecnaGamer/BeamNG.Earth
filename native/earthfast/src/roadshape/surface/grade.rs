//! Place the grading corridor on the two edges of a welded surface piece.
//! Work in cross sections normal to the grading line. A side cannot widen
//! the opposite side of a ramp and a later bend cannot supply a remote edge.
use super::{add, cross, dot, left, mul, norm, sub, unit, P2};
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2};
use pyo3::{exceptions::PyValueError, prelude::*};

/// Signed lateral distance at which a side segment crosses this normal.
fn intercept(p: P2, along: P2, a: P2, b: P2) -> Option<f64> {
    let segment = sub(b, a);
    let run = dot(segment, along);
    if run.abs() < 1e-8 {
        return None;
    }
    let u = dot(sub(p, a), along) / run;
    if !(0.0..=1.0).contains(&u) {
        return None;
    }
    Some(cross(along, sub(add(a, mul(segment, u)), p)))
}

#[pyfunction]
pub fn surface_grade_sections<'py>(
    py: Python<'py>,
    line: PyReadonlyArray2<'py, f64>,
    outlines: Vec<PyReadonlyArray2<'py, f64>>,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let xy = line.as_array();
    if xy.ncols() != 2 || xy.nrows() < 2 {
        return Err(PyValueError::new_err("surface grading needs a plan line"));
    }
    let mut sections = numpy::ndarray::Array2::zeros((xy.nrows(), 4));
    for (i, p) in xy.rows().into_iter().enumerate() {
        sections[[i, 0]] = p[0];
        sections[[i, 1]] = p[1];
    }
    for polygon in outlines {
        let v = polygon.as_array();
        let n = v.nrows() / 2;
        if n < 2 || v.nrows() != 2 * n || v.ncols() != 2 {
            return Err(PyValueError::new_err("expected paired surface edges"));
        }
        let point = |k: usize| [v[[k, 0]], v[[k, 1]]];
        for i in 0..xy.nrows() {
            if sections[[i, 2]] > 0.0 { continue; }
            let p = [xy[[i, 0]], xy[[i, 1]]];
            let a = xy.row(i.saturating_sub(1));
            let b = xy.row((i + 1).min(xy.nrows() - 1));
            let d = [b[0] - a[0], b[1] - a[1]];
            if norm(d) < 1e-8 { continue; }
            let along = unit(d);
            for k in 1..n {
                let l0 = point(k - 1);
                let l1 = point(k);
                let r0 = point(2 * n - k);
                let r1 = point(2 * n - k - 1);
                // A skew row can put the two intersections in different
                // adjacent side segments. Extend the search by one row.
                let Some(l) = intercept(p, along, l0, l1) else { continue };
                let opposite = intercept(p, along, r0, r1)
                    .or_else(|| (k > 1).then(|| intercept(p, along, point(2 * n - k + 1), r0)).flatten())
                    .or_else(|| (k + 1 < n).then(|| intercept(p, along, r1, point(2 * n - k - 2))).flatten());
                if let Some(r) = opposite {
                    if l * r > 0.0 || (l - r).abs() < 0.01 { continue; }
                    let shift = 0.5 * (l + r);
                    let centre = add(p, mul(left(along), shift));
                    sections[[i, 0]] = centre[0];
                    sections[[i, 1]] = centre[1];
                    sections[[i, 2]] = (l - r).abs() * 0.5;
                    sections[[i, 3]] = shift;
                    break;
                }
            }
        }
    }
    Ok(sections.into_pyarray(py))
}
