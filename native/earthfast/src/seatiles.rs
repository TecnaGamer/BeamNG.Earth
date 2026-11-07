//! Decisions for one batch of sea squares. A small low coastal town must not disappear in a tile-wide
//! percentage: near squares split for any sampled dry ground below the sea, down to the existing floor.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray2};
use pyo3::{exceptions::PyValueError, prelude::*};

fn action(x0: f64, y0: f64, size: f64, x: f64, y: f64, wet: usize, low: usize, n: usize,
          cfg: (f64, f64, f64, f64, f64, f64, f64)) -> u8 {
    let (_, poly_max, _, _, far_share, floor, floor_k) = cfg;
    // Keep the distant DEM-band policy. Near the player, even <5% can be a whole flooded town (#812).
    let allowed = if size <= poly_max { 0.0 } else { far_share };
    if low as f64 <= allowed * n as f64 {
        return 1; // keep
    }
    let dx = (x0 - x).max(0.0).max(x - (x0 + size));
    let dy = (y0 - y).max(0.0).max(y - (y0 + size));
    if size / 2.0 >= floor.max(floor_k * dx.hypot(dy)) {
        2 // split
    } else if wet * 2 >= n {
        1 // the existing shore approximation at the subdivision floor
    } else {
        0 // drop
    }
}

/// 0 drop / 1 keep / 2 split for each square's sampled wet mask and raw ground heights.
#[pyfunction]
pub fn sea_tile_actions<'py>(py: Python<'py>, corners: PyReadonlyArray2<'py, f64>,
    wet: PyReadonlyArray2<'py, bool>, ground: PyReadonlyArray2<'py, f64>, size: f64, x: f64, y: f64,
    cfg: (f64, f64, f64, f64, f64, f64, f64)) -> PyResult<Bound<'py, PyArray1<u8>>> {
    let c = corners.as_array();
    let w = wet.as_array();
    let g = ground.as_array();
    if c.ncols() != 2 || c.nrows() != w.nrows() || w.dim() != g.dim() || w.ncols() == 0 {
        return Err(PyValueError::new_err("sea tile samples and corners have incompatible shapes"));
    }
    let (sea, poly_max, near_m, far_m, _, _, _) = cfg;
    let limit = sea - if size <= poly_max { near_m } else { far_m };
    let result: Vec<u8> = (0..c.nrows()).map(|i| {
        let nw = w.row(i).iter().filter(|&&v| v).count();
        let nl = w.row(i).iter().zip(g.row(i).iter()).filter(|&(&wet, &z)| !wet && z < limit).count();
        action(c[[i, 0]], c[[i, 1]], size, x, y, nw, nl, w.ncols(), cfg)
    }).collect();
    Ok(result.into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;
    const CFG: (f64, f64, f64, f64, f64, f64, f64) = (0., 2048., 0.3, 2., 0.05, 64., 0.0625);

    #[test]
    fn small_low_coastal_area_splits_near_only() {
        assert_eq!(action(0., 0., 2048., 0., 0., 1700, 39, 4096, CFG), 2);
        assert_eq!(action(0., 0., 8192., 0., 0., 1700, 39, 4096, CFG), 1);
        assert_eq!(action(0., 0., 2048., 0., 0., 1700, 0, 4096, CFG), 1);
    }

    #[test]
    fn floor_and_distance_remain_bounded() {
        assert_eq!(action(0., 0., 64., 0., 0., 10, 39, 256, CFG), 0);
        assert_eq!(action(0., 0., 64., 0., 0., 200, 39, 256, CFG), 1);
        assert_eq!(action(6000., 0., 128., 0., 0., 200, 39, 256, CFG), 1);
    }
}
