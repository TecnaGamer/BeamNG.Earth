//! The tram's corridor (plan 046, "objects on the rails"): where nothing may stand along a ground tram track. A tram's
//! structure gauge is about 1.3 m either side of its axis (a 2.4-2.65 m wide body); a pole, a sign post, a bench, a building's
//! wall or a tree inside it is on the rails. ONE definition for every consumer of obstacles (furniture.place's poles, lamps,
//! benches and bins, the bus shelters, the signs' posts, the buildings' footprints): the ground tram parts, ways that meet end
//! to end one line (so a joint is no gap), sampled every 2 m. The tram's own poles, lamps and signals are exempt: they are
//! placed relative to the track by design (tramcat, tramsignals).
use super::lines::{densify, P2};
use super::switches::near_line;
use super::track::chain_parts;

use numpy::{IntoPyArray, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// m from the axis nothing may stand: the structure gauge's half and a margin (furniture.place adds its own CLEAR_OTHER_M, so
/// its obstacles are given a half width of KEEP_M - 1.5 there: railxing.rail_obstacles).
pub const KEEP_M: f64 = 1.6;
/// m from the axis a building's footprint may not reach (its wall is a tram's side clearance and more).
pub const BUILDING_KEEP_M: f64 = 1.8;
/// m between the samples of a corridor line.
pub const STEP_M: f64 = 2.0;

/// The corridor lines of the ground tram parts [(line, gauge)]: ways meeting end to end are one line, sampled every STEP_M.
pub fn lines(parts: &[(Vec<P2>, f64)]) -> Vec<Vec<P2>> {
    chain_parts(parts).into_iter().map(|(l, _g)| densify(&l, STEP_M)).filter(|l| l.len() >= 2).collect()
}

/// Which points lie in the corridor (within `half` of a line).
pub fn hits(pts: &[P2], lines: &[Vec<P2>], half: f64) -> Vec<bool> {
    pts.iter().map(|&p| lines.iter().any(|l| near_line(p, l).0 < half)).collect()
}

fn pts2(a: &PyReadonlyArray2<f64>) -> PyResult<Vec<P2>> {
    let v = a.as_array();
    if v.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("tram corridor: points need two columns"));
    }
    Ok((0..v.nrows()).map(|i| [v[[i, 0]], v[[i, 1]]]).collect())
}

fn split(xy: &PyReadonlyArray2<f64>, off: &PyReadonlyArray1<i64>, g: &PyReadonlyArray1<f64>) -> PyResult<Vec<(Vec<P2>, f64)>> {
    let p = pts2(xy)?;
    let (o, gg) = (off.as_array().to_vec(), g.as_array().to_vec());
    if o.is_empty() || o.len() != gg.len() + 1 || o[o.len() - 1] as usize != p.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("tram corridor: parts and offsets differ"));
    }
    Ok((0..gg.len()).map(|k| (p[o[k] as usize..o[k + 1] as usize].to_vec(), gg[k])).collect())
}

/// The corridor lines of ground tram parts (end to end: xy (n, 2), part_off (p + 1,), gauges (p,)): [(k, 2)].
#[pyfunction]
pub fn tram_corridor_lines<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, part_off: PyReadonlyArray1<'py, i64>,
                                gauges: PyReadonlyArray1<'py, f64>) -> PyResult<Vec<Bound<'py, numpy::PyArray2<f64>>>> {
    let parts = split(&xy, &part_off, &gauges)?;
    let out = py.detach(|| lines(&parts));
    Ok(out
        .into_iter()
        .map(|l| {
            let flat: Vec<f64> = l.iter().flat_map(|q| [q[0], q[1]]).collect();
            numpy::ndarray::Array2::from_shape_vec((l.len(), 2), flat).unwrap().into_pyarray(py)
        })
        .collect())
}

/// Which points (n, 2) lie within `half` of the ground tram parts' corridor lines: [bool].
#[pyfunction]
pub fn tram_corridor_hits<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, xy: PyReadonlyArray2<'py, f64>,
                               part_off: PyReadonlyArray1<'py, i64>, gauges: PyReadonlyArray1<'py, f64>, half: f64)
                               -> PyResult<Vec<bool>> {
    let (p, parts) = (pts2(&pts)?, split(&xy, &part_off, &gauges)?);
    Ok(py.detach(|| hits(&p, &lines(&parts), half)))
}

/// The numbers of the corridor.
#[pyfunction]
pub fn tram_corridor_consts(py: Python<'_>) -> PyResult<Bound<'_, pyo3::types::PyDict>> {
    let d = pyo3::types::PyDict::new(py);
    for (k, v) in [("KEEP_M", KEEP_M), ("BUILDING_KEEP_M", BUILDING_KEEP_M), ("STEP_M", STEP_M)] {
        d.set_item(k, v)?;
    }
    Ok(d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_pole_on_the_axis_is_in_the_corridor_and_one_two_metres_off_is_not() {
        let a: Vec<P2> = (0..=10).map(|i| [i as f64 * 10.0, 0.0]).collect();
        let b: Vec<P2> = (0..=10).map(|i| [100.0 + i as f64 * 10.0, 0.0]).collect();
        let l = lines(&[(a, 1.435), (b, 1.435)]);
        assert_eq!(l.len(), 1, "two ways end to end: one line");
        let h = hits(&[[50.0, 0.0], [100.0, 0.3], [50.0, 1.0], [50.0, 2.0], [50.0, -2.5], [250.0, 0.0]], &l, KEEP_M);
        assert_eq!(h, vec![true, true, true, false, false, false]);
    }
}
