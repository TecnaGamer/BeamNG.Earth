//! Source pixel coordinates of an imagery square. Keep both the global mesh (tile selection)
//! and the mosaic-relative mesh (warp interpolation) at their original precision.
use crate::{
    dm::Dm,
    fartex::{footprint_tiles, TILE},
};
use numpy::{ndarray::Array2, IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::f64::consts::PI;

#[pyclass(frozen)]
pub struct PhotoGrid {
    lat: Array2<f64>,
    lon: Array2<f64>,
}

fn tile(p: f64) -> i64 {
    (p.floor() as i64).div_euclid(TILE as i64)
}

fn build_grid(n: usize, point: impl Fn(usize, usize) -> (f64, f64)) -> PyResult<PhotoGrid> {
    if n == 0 || n.checked_add(1).and_then(|s| s.checked_mul(s)).is_none() {
        return Err(PyValueError::new_err("invalid photo grid size"));
    }
    let mut lat = Array2::zeros((n + 1, n + 1));
    let mut lon = lat.clone();
    for ((r, c), v) in lat.indexed_iter_mut() {
        (*v, lon[[r, c]]) = point(r, c);
    }
    Ok(PhotoGrid { lat, lon })
}

#[pymethods]
impl PhotoGrid {
    #[new]
    fn new(lat: PyReadonlyArray2<'_, f64>, lon: PyReadonlyArray2<'_, f64>) -> PyResult<Self> {
        let lat = lat.as_array();
        let lon = lon.as_array();
        if lat.nrows() < 2 || lat.nrows() != lat.ncols() || lat.dim() != lon.dim() {
            return Err(PyValueError::new_err("expected square lat/lon arrays"));
        }
        Ok(Self {
            lat: lat.as_standard_layout().into_owned(),
            lon: lon.as_standard_layout().into_owned(),
        })
    }

    #[staticmethod]
    fn mercator(
        py: Python<'_>,
        n: usize,
        z: u8,
        x: i64,
        y: i64,
        f0: f64,
        f1: f64,
    ) -> PyResult<Self> {
        let scale = (f1 - f0) / n as f64;
        let world = 2f64.dpowf(z as f64);
        py.detach(|| {
            build_grid(n, |r, c| {
                let fx = f0 + c as f64 * scale;
                let fy = f0 + r as f64 * scale;
                let lon = (x as f64 + fx) / world * 360.0 - 180.0;
                let lat = (PI * (1.0 - 2.0 * (y as f64 + fy) / world))
                    .dsinh()
                    .datan()
                    .to_degrees();
                (lat, lon)
            })
        })
    }

    #[staticmethod]
    fn enu(
        py: Python<'_>,
        n: usize,
        x0: f64,
        y0: f64,
        size: f64,
        origin: PyReadonlyArray1<'_, f64>,
        rot: PyReadonlyArray2<'_, f64>,
    ) -> PyResult<Self> {
        let (o, r) = crate::frame_of(&origin, &rot)?;
        let step = size / n as f64;
        py.detach(|| {
            build_grid(n, |y, x| {
                let (lat, lon, _) = crate::e2g_one(
                    x0 + x as f64 * step,
                    y0 + size - y as f64 * step,
                    0.,
                    &o,
                    &r,
                );
                (lat, lon)
            })
        })
    }

    #[staticmethod]
    fn world(
        py: Python<'_>,
        n: usize,
        x0: f64,
        y0: f64,
        size: f64,
        lat: f64,
        lon: f64,
    ) -> PyResult<Self> {
        let frame = crate::geotiles::Frame::new(lat, lon);
        let step = size / n as f64;
        py.detach(|| {
            build_grid(n, |y, x| {
                frame.world_to_latlon(x0 + x as f64 * step, y0 + size - y as f64 * step)
            })
        })
    }

    fn arrays<'py>(
        &self,
        py: Python<'py>,
    ) -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>) {
        (
            self.lat.clone().into_pyarray(py),
            self.lon.clone().into_pyarray(py),
        )
    }

    /// World-space canopy coordinates and the latitude used for date-dependent season selection.
    fn season_grid<'py>(
        &self,
        py: Python<'py>,
    ) -> (f64, Bound<'py, PyArray2<f32>>, Bound<'py, PyArray2<f32>>) {
        let (mean, east, north) = py.detach(|| {
            let mean = crate::levels::np_sum(self.lat.as_slice().unwrap()) / self.lat.len() as f64;
            let east =
                Array2::from_shape_fn(self.lat.dim(), |i| east_metres(self.lat[i], self.lon[i]));
            (mean, east, self.lat.mapv(north_metres))
        });
        (mean, east.into_pyarray(py), north.into_pyarray(py))
    }

    fn bounds(&self) -> (f64, f64, f64, f64, f64) {
        let center = self.lat.nrows().saturating_sub(1) / 2;
        let (south, north) = extrema(&self.lat);
        let (west, east) = extrema(&self.lon);
        (south, west, north, east, self.lat[[center, center]])
    }

    fn project(&self, py: Python<'_>, z: u8, max_tiles: usize) -> PyResult<Option<SourceMesh>> {
        let n = 1i64
            .checked_shl(z.into())
            .filter(|&n| n > 0 && n <= i64::MAX / TILE as i64)
            .ok_or_else(|| PyValueError::new_err("invalid source zoom"))?;
        let w = n as f64 * TILE as f64;
        py.detach(|| {
            let x0 = (self.lon[[0, 0]] / 360.0 + 0.5) * w;
            let x = self.lon.mapv(|p| {
                let x = (p / 360.0 + 0.5) * w;
                x - w * ((x - x0) / w).round_ties_even()
            });
            let y = self.lat.mapv(|p| {
                (0.5 - p.clamp(-89.9, 89.9).to_radians().dtan().dasinh() / (2.0 * PI)) * w
            });
            let (minx, maxx) = extrema(&x);
            let (miny, maxy) = extrema(&y);
            let (tx, ty) = (tile(minx - 1.0), tile(miny - 1.0).max(0));
            let (nx, ny) = (
                tile(maxx + 1.0) - tx + 1,
                tile(maxy + 1.0).min(n - 1) - ty + 1,
            );
            if nx <= 0 || ny <= 0 || nx.saturating_mul(ny) as usize > max_tiles {
                return Ok(None);
            }
            let qx = x.mapv(|v| v - (tx * TILE as i64) as f64);
            let qy = y.mapv(|v| v - (ty * TILE as i64) as f64);
            Ok(Some(SourceMesh {
                x,
                y,
                qx,
                qy,
                tiles: (tx, ty, nx as usize, ny as usize),
            }))
        })
    }
}

fn extrema(a: &Array2<f64>) -> (f64, f64) {
    a.iter()
        .fold((f64::INFINITY, f64::NEG_INFINITY), |(lo, hi), &v| {
            (lo.min(v), hi.max(v))
        })
}

#[pyclass(frozen)]
pub struct SourceMesh {
    pub x: Array2<f64>,
    pub y: Array2<f64>,
    pub qx: Array2<f64>,
    pub qy: Array2<f64>,
    #[pyo3(get)]
    pub tiles: (i64, i64, usize, usize),
}

#[pymethods]
impl SourceMesh {
    fn core_tiles(
        &self,
        cells: usize,
        cw: usize,
        g: usize,
        px: usize,
    ) -> PyResult<(i64, i64, i64, i64)> {
        footprint_tiles(self.x.view(), self.y.view(), cells, cw, g, px)
    }
}

fn east_metres(lat: f64, lon: f64) -> f32 {
    (lon * 111320.0 * lat.to_radians().dcos()) as f32
}
fn north_metres(lat: f64) -> f32 {
    (lat * 111320.0) as f32
}

#[pyfunction]
pub fn season_metres<'py>(
    py: Python<'py>,
    lat: numpy::PyReadonlyArrayDyn<'py, f64>,
    lon: numpy::PyReadonlyArrayDyn<'py, f64>,
    north_lat: numpy::PyReadonlyArrayDyn<'py, f64>,
) -> PyResult<(
    Bound<'py, numpy::PyArrayDyn<f32>>,
    Bound<'py, numpy::PyArrayDyn<f32>>,
)> {
    let (lat, lon, n) = (lat.as_array(), lon.as_array(), north_lat.as_array());
    if lat.shape() != lon.shape() {
        return Err(PyValueError::new_err(
            "season_metres: latitude and longitude shapes differ",
        ));
    }
    let (east, north) = py.detach(|| {
        (
            numpy::ndarray::Zip::from(lat)
                .and(lon)
                .map_collect(|&la, &lo| east_metres(la, lo)),
            n.mapv(north_metres),
        )
    });
    Ok((east.into_pyarray(py), north.into_pyarray(py)))
}
