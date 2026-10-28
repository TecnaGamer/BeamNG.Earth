//! Street lamps past the road tiles (the user, 2026-10-27: "extend the street and window lights as far as the buildings
//! render"): the far and skyline building tiles carry the lamps of their streets as glow points only (no posts, no
//! light), placed by a simple rule along the mapped road lines.

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// Lamp stations along lines given as their vertices pts (n, 2) with offsets off (m + 1): per line every spacing[i] m
/// of arc length from phase[i] on, side[i] m to the left of the line's direction (negative: to its right). Only the
/// stations in the box x0, y0, x1, y1. (stations (k, 3) [x, y, line index]).
#[pyfunction]
pub fn lamp_stations<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                          spacing: PyReadonlyArray1<'py, f64>, phase: PyReadonlyArray1<'py, f64>,
                          side: PyReadonlyArray1<'py, f64>, bx: [f64; 4]) -> Bound<'py, PyArray2<f64>> {
    let (pts, off, spacing, phase, side) = (pts.as_array(), off.as_array(), spacing.as_array(), phase.as_array(),
                                            side.as_array());
    let mut out: Vec<f64> = Vec::new();
    for i in 0..off.len().saturating_sub(1) {
        let (a, b) = (off[i] as usize, off[i + 1] as usize);
        let sp = spacing[i];
        if b < a + 2 || !(sp > 0.0) {
            continue;
        }
        let mut next = phase[i].max(0.0);     // the arc length of the next station
        let mut s0 = 0.0;                      // the arc length at the segment's start
        for j in a..b - 1 {
            let (x0, y0, x1, y1) = (pts[[j, 0]], pts[[j, 1]], pts[[j + 1, 0]], pts[[j + 1, 1]]);
            let len = ((x1 - x0).powi(2) + (y1 - y0).powi(2)).sqrt();
            if len <= 1e-9 {
                continue;
            }
            let (dx, dy) = ((x1 - x0) / len, (y1 - y0) / len);
            while next <= s0 + len {
                let t = next - s0;
                let x = x0 + dx * t - dy * side[i];
                let y = y0 + dy * t + dx * side[i];
                if x >= bx[0] && x < bx[2] && y >= bx[1] && y < bx[3] {
                    out.extend_from_slice(&[x, y, i as f64]);
                }
                next += sp;
            }
            s0 += len;
        }
    }
    let n = out.len() / 3;
    numpy::ndarray::Array2::from_shape_vec((n, 3), out).unwrap().into_pyarray(py)
}
