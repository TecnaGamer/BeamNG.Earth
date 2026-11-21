//! Where a road is, for its spec rules (sweden.contains: Sweden's outline; signstates.states: the US states' outlines),
//! byte for byte the numpy they replaced: the even-odd crossing test, the intercept a.x + ((y - a.y) * (b.x - a.x)) /
//! (b.y - a.y) compared with x (a NaN intercept never counts), the points outside a polygon's box never tested.

use numpy::{IntoPyArray, PyArray1};
use pyo3::prelude::*;
use super::pyobj::{CArr1, CArr2};

/// sweden.contains' test of one point: edges (a[i], a[(i + 1) % m]) of the closed outline crossed to the point's right,
/// an odd count.
fn inside_outline(outline: &[[f64; 2]], x: f64, y: f64) -> bool {
    let m = outline.len();
    let mut count = 0u64;
    for i in 0..m {
        let (a, b) = (outline[i], outline[(i + 1) % m]);
        let crosses = (a[1] > y) != (b[1] > y);
        let at = a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]);
        if crosses && x < at {
            count += 1;
        }
    }
    count % 2 == 1
}

fn points(a: &CArr2<'_, f64>, what: &str) -> PyResult<Vec<[f64; 2]>> {
    let v = a.as_array();
    if v.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err(format!("{what}: (n, 2) points")));
    }
    Ok((0..v.nrows()).map(|k| [v[[k, 0]], v[[k, 1]]]).collect())
}

/// sweden.contains(lon, lat) for 1-D lon and lat of one length, given the outline (m, 2: lon, lat).
#[pyfunction]
pub fn outline_contains<'py>(py: Python<'py>, outline: CArr2<'py, f64>, lon: CArr1<'py, f64>,
                             lat: CArr1<'py, f64>) -> PyResult<Bound<'py, PyArray1<bool>>> {
    let o = points(&outline, "outline_contains")?;
    let (x, y) = (lon.as_slice()?, lat.as_slice()?);
    if x.len() != y.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "operands could not be broadcast together with shapes ({},) ({},)", x.len(), y.len())));
    }
    let out: Vec<bool> = x.iter().zip(y).map(|(&px, &py_)| inside_outline(&o, px, py_)).collect();
    Ok(out.into_pyarray(py))
}

/// The US states' polygons (signstates._polygons), in their order: each one's box (lower, upper) and rings (closed,
/// the GeoJSON's: the last point the first).
#[pyclass(frozen, module = "earthfast")]
pub struct PolygonIndex {
    lower: Vec<[f64; 2]>,
    upper: Vec<[f64; 2]>,
    rings: Vec<Vec<Vec<[f64; 2]>>>,
}

#[pymethods]
impl PolygonIndex {
    /// PolygonIndex(lower (p, 2), upper (p, 2), [[ring (k, 2), ...] per polygon]).
    #[new]
    fn new(lower: CArr2<'_, f64>, upper: CArr2<'_, f64>,
           rings: Vec<Vec<CArr2<'_, f64>>>) -> PyResult<Self> {
        let lower = points(&lower, "PolygonIndex")?;
        let upper = points(&upper, "PolygonIndex")?;
        if lower.len() != rings.len() || upper.len() != rings.len() {
            return Err(pyo3::exceptions::PyValueError::new_err("PolygonIndex: one box per polygon"));
        }
        let rings = rings.iter().map(|p| p.iter().map(|r| points(r, "PolygonIndex")).collect::<PyResult<Vec<_>>>())
            .collect::<PyResult<Vec<_>>>()?;
        Ok(PolygonIndex { lower, upper, rings })
    }

    /// signstates.states' lookup of 1-D x (lon), y (lat) of one length: each point's first polygon (in order) whose
    /// box holds it and whose rings, by the even-odd count of all their edges (ring[:-1], ring[1:]), hold it; -1 for
    /// none (a NaN point none).
    fn lookup<'py>(&self, py: Python<'py>, x: CArr1<'py, f64>, y: CArr1<'py, f64>)
                   -> PyResult<Bound<'py, PyArray1<i64>>> {
        let (x, y) = (x.as_slice()?, y.as_slice()?);
        if x.len() != y.len() {
            return Err(pyo3::exceptions::PyValueError::new_err("PolygonIndex.lookup: x and y of other lengths"));
        }
        let out: Vec<i64> = x.iter().zip(y).map(|(&px, &py_)| {
            for (p, rings) in self.rings.iter().enumerate() {
                let (lo, hi) = (self.lower[p], self.upper[p]);
                if !(px >= lo[0] && px <= hi[0] && py_ >= lo[1] && py_ <= hi[1]) {
                    continue;
                }
                let mut inside = false;
                for ring in rings {
                    let mut count = 0u64;
                    for w in ring.windows(2) {
                        let (a, b) = (w[0], w[1]);
                        let dy = b[1] - a[1];
                        let d = if dy == 0.0 { 1.0 } else { dy };
                        let intercept = a[0] + (py_ - a[1]) * (b[0] - a[0]) / d;
                        if ((a[1] > py_) != (b[1] > py_)) && px < intercept {
                            count += 1;
                        }
                    }
                    inside ^= count % 2 != 0;
                }
                if inside {
                    return p as i64;
                }
            }
            -1
        }).collect();
        Ok(out.into_pyarray(py))
    }
}

/// roads._width_regions: each feature's WIDTH_REGIONS code: `us` where its state is known (us_mask: _marking_states
/// != ''), then `jp` where it has a point and its middle point ((first + max(last, first)) // 2 of its points, clipped
/// to the layer's) lies in the Japan box (lat0, lon0, lat1, lon1, bounds included), else 0.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn width_regions<'py>(py: Python<'py>, us_mask: CArr1<'py, bool>, part_off: CArr1<'py, i64>,
                          ring_off: CArr1<'py, i64>, coord_off: CArr1<'py, i64>,
                          lonlat: CArr2<'py, f64>, japan: (f64, f64, f64, f64), us: i64, jp: i64)
                          -> PyResult<Bound<'py, PyArray1<i64>>> {
    let (po, ro, co) = (part_off.as_slice()?, ring_off.as_slice()?, coord_off.as_slice()?);
    let n = po.len().saturating_sub(1);
    let ll = lonlat.as_array();
    let mut out = vec![0i64; n];
    if n == 0 {
        return Ok(out.into_pyarray(py));
    }
    super::lanesplit::check_lonlat(ll.ncols())?;
    super::lanesplit::check_offsets(n, po, ro, co, ll.nrows())?;
    let m = us_mask.as_slice()?;
    if m.len() != n {
        return Err(pyo3::exceptions::PyIndexError::new_err(format!(
            "boolean index did not match indexed array along axis 0; size of axis is {n} but size of corresponding \
             boolean axis is {}", m.len())));
    }
    for i in 0..n {
        if m[i] {
            out[i] = us;
        }
    }
    let k = ll.nrows() as i64;
    if k > 0 {
        let (la0, lo0, la1, lo1) = japan;
        for i in 0..n {
            let s = co[ro[po[i] as usize] as usize];
            let e = co[ro[po[i + 1] as usize] as usize];
            if e <= s {
                continue;
            }
            let mid = ((s + (e - 1).max(s)) / 2).clamp(0, k - 1) as usize;
            let (lon, lat) = (ll[[mid, 0]], ll[[mid, 1]]);
            if lat >= la0 && lat <= la1 && lon >= lo0 && lon <= lo1 {
                out[i] = jp;
            }
        }
    }
    Ok(out.into_pyarray(py))
}
