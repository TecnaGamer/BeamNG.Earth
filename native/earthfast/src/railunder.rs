//! railways.road_underpasses (the full port, 491): the short road tunnels near a window. Every node window of
//! railprofile's lattice (~800 a Manhattan visit's 25 road tiles) went through every road of its cells in Python: the
//! layer's tunnel mask made again, then for each road with a tunnel near the window its tunnel parts, the map's ranges
//! against the raw ones, each ring into the frame, its arc, its rounded ends (~2.5 of the rail stack's ~18 CPU-s). Now
//! a layer's roads with a tunnel (a spec or the map's) sit in an UnderpassIndex per frame and road revision, each one's
//! short tunnels made once when first asked (railways._underpass_entry, the Python's calls), and a window is one call:
//! box test, arc test (numpy's cumsum of the segments' hypot), np.round's ends and the dedupe by (id, ends).
use crate::dm::Dm;
use crate::railindex::{box_hit, code_points, offsets, P};

use std::collections::HashSet;
use std::sync::{Arc, Mutex};

use numpy::{IntoPyArray, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};

/// A road's short tunnels in the frame: each ring's points, the tunnel's half width (the Python's number) and its dedupe key (the ends
/// rounded to the cm), and whether a map tunnel was missing from its ranges (railways then asks the layer's road_specs
/// on that call, as the Python did).
struct Under {
    missing: bool,
    key: Vec<u32>,
    rings: Vec<(Arc<Vec<P>>, Py<PyAny>, [f64; 4])>,
}

/// A layer's roads with a tunnel spec or a tunnel among the map's ranges (rows ascending, their boxes in the frame) and
/// each one's short tunnels once asked.
#[pyclass(module = "earthfast", frozen)]
pub struct UnderpassIndex {
    rows: Vec<(usize, [f64; 4])>,
    max_m: f64,
    got: Mutex<Vec<Option<Arc<Under>>>>,
}

#[pymethods]
impl UnderpassIndex {
    /// rows: the layer's roads with a tunnel (ascending), boxes (k, 4) theirs in the frame; max_m: roads.UNDERPASS_MAX_M.
    #[new]
    fn new(rows: PyReadonlyArray1<'_, i64>, boxes: PyReadonlyArray2<'_, f64>, max_m: f64) -> PyResult<Self> {
        let rows = offsets(&rows, "row")?;
        let b = boxes.as_array();
        if b.nrows() != rows.len() || (!rows.is_empty() && b.ncols() != 4) {
            return Err(PyValueError::new_err("UnderpassIndex: boxes (k, 4) per row"));
        }
        let n = rows.len();
        Ok(UnderpassIndex {
            rows: rows.into_iter().enumerate().map(|(k, i)| (i, [b[[k, 0]], b[[k, 1]], b[[k, 2]], b[[k, 3]]])).collect(),
            max_m,
            got: Mutex::new(vec![None; n]),
        })
    }

    fn __len__(&self) -> usize {
        self.rows.len()
    }
}

/// entry()'s answer: (missing, id, [(xy (k, 2), half width)]).
type Entry<'py> = (bool, Bound<'py, PyString>, Vec<(PyReadonlyArray2<'py, f64>, Bound<'py, PyAny>)>);

/// np.round(v, 2): v * 100 to the nearest even, / 100.
fn round2(v: f64) -> f64 {
    (v * 100.0).round_ties_even() / 100.0
}

impl UnderpassIndex {
    /// Row k's tunnels, made by entry(layer, i) when first asked (no lock held while the Python runs).
    fn under(&self, k: usize, layer: usize, entry: &Bound<'_, PyAny>) -> PyResult<Arc<Under>> {
        if let Some(u) = &self.got.lock().unwrap_or_else(|e| e.into_inner())[k] {
            return Ok(u.clone());
        }
        let v = entry.call1((layer, self.rows[k].0))?;
        let (missing, fid, rings): Entry<'_> = v.extract()?;
        let mut out = Vec::new();
        for (xy, half) in rings {
            let a = xy.as_array();
            if a.nrows() < 2 {
                continue;
            }
            if a.ncols() < 2 {
                return Err(PyValueError::new_err("rail_underpass_select: a tunnel ring is (k, 2)"));
            }
            let p: Vec<P> = (0..a.nrows()).map(|j| [a[[j, 0]], a[[j, 1]]]).collect();
            let mut s = 0.0f64;
            for w in p.windows(2) {
                s += (w[1][0] - w[0][0]).dhypot(w[1][1] - w[0][1]);
            }
            if s > self.max_m {
                continue;
            }
            let (f, l) = (p[0], p[p.len() - 1]);
            out.push((Arc::new(p), half.unbind(), [round2(f[0]), round2(f[1]), round2(l[0]), round2(l[1])]));
        }
        let u = Arc::new(Under { missing, key: code_points(&fid)?, rings: out });
        let mut got = self.got.lock().unwrap_or_else(|e| e.into_inner());
        Ok(got[k].get_or_insert(u).clone())
    }
}

/// railways.road_underpasses over the layers' UnderpassIndex within near: [(xy (k, 2), half width)] of every short
/// tunnel of the roads whose box meets near, layer by layer, row by row, ring by ring, each once by (id, its ends to the
/// cm; Python's tuple equality: -0.0 == 0.0, a NaN equal to nothing); entry(layer, i) -> (missing, id, [(xy, half)])
/// makes a road's when first asked. Also the layers (their positions) one of whose roads taken had a map tunnel missing
/// from its ranges.
#[pyfunction]
#[pyo3(signature = (indexes, near, entry))]
pub fn rail_underpass_select<'py>(py: Python<'py>, indexes: Vec<Bound<'py, UnderpassIndex>>,
                                  near: (f64, f64, f64, f64), entry: Bound<'py, PyAny>)
                                  -> PyResult<(Bound<'py, PyList>, Vec<usize>)> {
    let near = [near.0, near.1, near.2, near.3];
    let out = PyList::empty(py);
    let mut seen: HashSet<(Vec<u32>, [u64; 4])> = HashSet::new();
    let mut ground = Vec::new();
    for (li, ix) in indexes.iter().enumerate() {
        let ix = ix.get();
        let mut asked = false;
        for k in 0..ix.rows.len() {
            if !box_hit(&ix.rows[k].1, &near) {
                continue;
            }
            let u = ix.under(k, li, &entry)?;
            asked |= u.missing;
            for (p, half, key) in &u.rings {
                if !key.iter().any(|v| v.is_nan()) {
                    let bits = key.map(|v| if v == 0.0 { 0u64 } else { v.to_bits() });
                    if !seen.insert((u.key.clone(), bits)) {
                        continue;
                    }
                }
                let flat: Vec<f64> = p.iter().flat_map(|q| [q[0], q[1]]).collect();
                let arr = numpy::ndarray::Array2::from_shape_vec((p.len(), 2), flat)
                    .map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py);
                out.append((arr, half.bind(py)))?;
            }
        }
        if asked {
            ground.push(li);
        }
    }
    Ok((out, ground))
}
