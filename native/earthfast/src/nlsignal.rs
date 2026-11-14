//! NumPy boundary for local Dutch signal-context matching.
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use crate::nlsignal_core::{self, Line, Query};

#[pyfunction]
pub fn nl_signal_context_match<'py>(
    py: Python<'py>, queries: PyReadonlyArray2<'py, f64>, points: PyReadonlyArray2<'py, f64>,
    offsets: PyReadonlyArray1<'py, i64>, kinds: PyReadonlyArray1<'py, u8>,
    oneways: PyReadonlyArray1<'py, i8>, turns: Vec<[String; 3]>, complete_wkd: bool,
) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (queries, points, offsets, kinds, oneways) =
        (queries.as_array(), points.as_array(), offsets.as_array(), kinds.as_array(), oneways.as_array());
    let n = kinds.len();
    if queries.ncols() != 5 || queries.nrows() > 100_000 || points.ncols() != 2 || points.nrows() > 1_000_000
        || n > 100_000 || oneways.len() != n || turns.len() != n || offsets.len() != n + 1
        || offsets[0] != 0 || offsets[n] != points.nrows() as i64 {
        return Err(PyValueError::new_err("signal context array shapes"));
    }
    let mut lines = Vec::with_capacity(n);
    for (i, text) in turns.into_iter().enumerate() {
        let (a, b) = (offsets[i], offsets[i + 1]);
        if a < 0 || b < a || b > points.nrows() as i64 {
            return Err(PyValueError::new_err("signal context offsets"));
        }
        lines.push(Line { kind: kinds[i], oneway: oneways[i], turns: text,
                          points: (a as usize..b as usize).map(|j| [points[[j,0]], points[[j,1]]]).collect() });
    }
    let queries: Vec<Query> = queries.rows().into_iter().map(|r| Query {
        xy: [r[0], r[1]], travel: [r[2], r[3]],
        lanes: if r[4].is_finite() && r[4].fract() == 0. && (0. ..=16.).contains(&r[4]) { r[4] as usize } else { 17 },
    }).collect();
    let count = queries.len();
    let matches = py.detach(|| nlsignal_core::matches(&queries, &lines, complete_wkd)).map_err(PyValueError::new_err)?;
    let flat: Vec<f64> = matches.into_iter().flat_map(|m| [m.zone as f64, m.urban as f64, m.road as f64,
        m.turns as f64, m.road_forward as f64, m.turns_forward as f64, m.urban_distance2, m.road_distance2]).collect();
    Ok(numpy::ndarray::Array2::from_shape_vec((count, 8), flat).unwrap().into_pyarray(py))
}
