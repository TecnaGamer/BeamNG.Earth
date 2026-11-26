//! The map cells' tile arithmetic for arrays of points (server/mapcells.py cells_of / cell_of): each point's
//! Web-Mercator cell (x, y) at a zoom, with cell_of's exact edge rules (a point on an edge belongs to the cell east /
//! south of it, as CellKey.bounds() puts the edge; 180 E wraps to x = 0), as two integer arrays instead of a list of
//! Python CellKeys: the ground under the decks (roadsys.ground_cut) and the profile squares' regions (_regions_of)
//! grouped their points by cell one Python object a point (20,000 calls, 7.5 s of a 20-geotile profile, 2026-11-26).
//! The same numbers as the Python: tile_xy's formula with the deterministic libm (dnp / dmath), the edge rules' own
//! scalar arithmetic.

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

pub const MAX_LAT: f64 = 85.0511287798066; // mapcells.MAX_LAT
pub const EDGE_EPS: f64 = 1e-6; // mapcells.EDGE_EPS (tile units)

/// mapcells.tile_xy for one point at n = 2^z tiles per axis (n as a float, the arrays' formula).
fn tile_xy(lat: f64, lon: f64, n: f64) -> (f64, f64) {
    let lat = lat.max(-MAX_LAT).min(MAX_LAT);
    let x = (lon / 360.0 + 0.5) * n;
    let y = (0.5 - libm::asinh(libm::tan(lat.to_radians())) / (2.0 * std::f64::consts::PI)) * n;
    (x, y)
}

fn lon_of(x: i64, n: i64) -> f64 {
    x as f64 / n as f64 * 360.0 - 180.0
}

fn lat_of(y: i64, n: i64) -> f64 {
    libm::atan(libm::sinh(std::f64::consts::PI * (1.0 - 2.0 * y as f64 / n as f64))).to_degrees()
}

fn exact_col(xf: f64, lon: f64, n: i64) -> i64 {
    let mut x = xf.floor() as i64;
    if xf - (x as f64) < EDGE_EPS && lon < lon_of(x, n) {
        x -= 1;
    } else if (x + 1) as f64 - xf < EDGE_EPS && lon >= lon_of(x + 1, n) {
        x += 1;
    }
    x
}

fn exact_row(yf: f64, lat: f64, n: i64) -> i64 {
    let mut y = (n - 1).min(0i64.max(yf.floor() as i64));
    if yf - (y as f64) < EDGE_EPS && y > 0 && lat > lat_of(y, n) {
        y -= 1;
    } else if (y + 1) as f64 - yf < EDGE_EPS && y < n - 1 && lat <= lat_of(y + 1, n) {
        y += 1;
    }
    y
}

/// One point's cell (x, y) at zoom z (cells_of: the tile formula; cell_of's exact rules within EDGE_EPS of an edge).
pub fn cell_xy(lat: f64, lon: f64, z: u32) -> (i64, i64) {
    let n = 1i64 << z;
    let nf = n as f64;
    let (x, y) = tile_xy(lat, lon, nf);
    let xi = x.floor();
    let yi = y.floor().max(0.0).min(nf - 1.0);
    let edge = x - xi < EDGE_EPS || xi + 1.0 - x < EDGE_EPS || y - yi < EDGE_EPS || yi + 1.0 - y < EDGE_EPS;
    if !edge {
        return ((xi as i64).rem_euclid(n), yi as i64);
    }
    // cell_of(lat, lon): its own tile_xy (the same formula), then the exact column and row
    (exact_col(x, lon, n).rem_euclid(n), exact_row(y, lat, n))
}

/// mapcells.cell_of(lat, lon, z) as (x, y): ValueError beyond MAX_LAT. wrap false: the column before it wraps
/// (mapcells.owner's: a point at 180 E has column 2^z there, the last column's).
#[pyfunction]
#[pyo3(signature = (lat, lon, z, wrap=true))]
pub fn mapcells_cell(lat: f64, lon: f64, z: u32, wrap: bool) -> PyResult<(i64, i64)> {
    if z > 30 {
        return Err(PyValueError::new_err("mapcells_cell: zoom beyond 30"));
    }
    if !(-MAX_LAT..=MAX_LAT).contains(&lat) {
        return Err(PyValueError::new_err(format!("latitude {lat} is beyond the Web-Mercator limit {MAX_LAT}")));
    }
    let n = 1i64 << z;
    let (x, y) = tile_xy(lat, lon, n as f64);
    let col = exact_col(x, lon, n);
    Ok((if wrap { col.rem_euclid(n) } else { col }, exact_row(y, lat, n)))
}

/// (x, y) int64 arrays of mapcells.cells_of(lat, lon, z) (lon finite): ValueError for a latitude beyond MAX_LAT (or NaN), as it.
#[pyfunction]
pub fn mapcells_xy<'py>(py: Python<'py>, lat: PyReadonlyArray1<'py, f64>, lon: PyReadonlyArray1<'py, f64>, z: u32)
    -> PyResult<(Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>)> {
    let la = lat.as_slice()?;
    let lo = lon.as_slice()?;
    if la.len() != lo.len() {
        return Err(PyValueError::new_err("mapcells_xy: lat and lon differ in length"));
    }
    if z > 30 {
        return Err(PyValueError::new_err("mapcells_xy: zoom beyond 30"));
    }
    if let Some(&bad) = la.iter().find(|&&v| !(-MAX_LAT..=MAX_LAT).contains(&v)) {
        return Err(PyValueError::new_err(format!("latitude {bad} is beyond the Web-Mercator limit {MAX_LAT}")));
    }
    let (xs, ys): (Vec<i64>, Vec<i64>) = py.detach(|| la.iter().zip(lo).map(|(&a, &b)| cell_xy(a, b, z)).unzip());
    Ok((xs.into_pyarray(py), ys.into_pyarray(py)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn edges_go_east_and_south() {
        let z = 14;
        let n = 1i64 << z;
        // a point exactly on a column's west edge belongs to that column
        let lon = lon_of(8000, n);
        assert_eq!(cell_xy(10.0, lon, z).0, 8000);
        // 180 E wraps to x = 0
        assert_eq!(cell_xy(10.0, 180.0, z).0, 0);
        // a point on a row's north edge belongs to that row (south of the edge)
        let lat = lat_of(6000, n);
        assert_eq!(cell_xy(lat, 0.0, z).1, 6000);
        // the poles clamp to the last rows
        assert_eq!(cell_xy(MAX_LAT, 0.0, z).1, 0);
        assert_eq!(cell_xy(-MAX_LAT, 0.0, z).1, n - 1);
    }
}
