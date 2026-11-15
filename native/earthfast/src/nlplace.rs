//! Python array boundary for the native source-position roadside planner.
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use crate::nlplace_core::{self, Candidate};

#[pyfunction]
pub fn nl_source_sign_place<'py>(py: Python<'py>, ids: Vec<String>, rows: PyReadonlyArray2<'py, f64>,
    poly: PyReadonlyArray2<'py, f64>, offsets: PyReadonlyArray1<'py, i64>, decks: PyReadonlyArray2<'py, f64>,
    bounds: [f64; 4]) -> PyResult<Bound<'py, PyArray1<i64>>> {
    let (rows, poly, offsets, decks) = (rows.as_array(), poly.as_array(), offsets.as_array(), decks.as_array());
    if rows.ncols() != 7 || rows.nrows() != ids.len() || rows.nrows() > 100_000
        || poly.ncols() != 2 || poly.nrows() > 1_000_000 || decks.ncols() != 5
        || offsets.is_empty() || offsets[0] != 0 || offsets[offsets.len()-1] != poly.nrows() as i64 {
        return Err(PyValueError::new_err("source sign array shapes"));
    }
    let mut polygons = Vec::new();
    for i in 0..offsets.len()-1 {
        let (a,b) = (offsets[i], offsets[i+1]);
        if a < 0 || b < a || b > poly.nrows() as i64 { return Err(PyValueError::new_err("pavement offsets")); }
        polygons.push((a as usize..b as usize).map(|j| [poly[[j,0]],poly[[j,1]]]).collect());
    }
    let candidates: Vec<Candidate> = ids.into_iter().zip(rows.rows()).map(|(id,r)| Candidate {
        id, xy:[r[0],r[1]], z:r[2], travel:[r[3],r[4]], radius:r[5], deck_margin:r[6]
    }).collect();
    let decks: Vec<[f64;5]> = decks.rows().into_iter().map(|r| [r[0],r[1],r[2],r[3],r[4]]).collect();
    let kept = py.detach(|| nlplace_core::place(&candidates,&polygons,&decks,bounds))
        .map_err(PyValueError::new_err)?;
    Ok(kept.into_iter().map(|i| i as i64).collect::<Vec<_>>().into_pyarray(py))
}
