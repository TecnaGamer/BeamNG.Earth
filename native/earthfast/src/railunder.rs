//! railways.road_underpasses (the full port, 491): the short road tunnels near a window. Every node window of
//! railprofile's lattice (~800 a Manhattan visit's 25 road tiles) went through every road of its cells in Python: the
//! layer's tunnel mask made again, then for each road with a tunnel near the window its tunnel parts, the map's ranges
//! against the raw ones, each ring into the frame, its arc, its rounded ends (~1.4 CPU-s of the rail stack's ~18). Now
//! a layer's roads with a tunnel (a spec or the map's) sit in an UnderpassIndex per frame and road revision, each one's
//! tunnel geometry made once when first asked (railways._underpass_entry, the Python's calls: its tunnel parts, the map
//! tunnels missing from its ranges as parts), and a window is one call: box test, the spec of each call (the tunnel
//! spec, or the layer's road_specs asked at the first road with a missing tunnel, as the Python asked them: specs made
//! while a download failed are made again), its half width (the Python's own arithmetic on it), the arc test (numpy's
//! cumsum of the segments' hypot), np.round's ends and the dedupe by (id, ends).
use crate::dm::Dm;
use crate::railindex::{box_hit, code_points, P};

use std::collections::HashSet;
use std::sync::{Arc, Mutex};

use numpy::{IntoPyArray, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyList, PyString};

/// A short tunnel ring in the frame and its dedupe key (its ends rounded to the cm).
type URing = (Arc<Vec<P>>, [f64; 4]);

/// A road's tunnel geometry in the frame: its tunnel parts (made when it had a tunnel spec) and the map's tunnels
/// missing from its ranges (gradesep made them ground), each ring of 80 m or less.
struct Under {
    spec: bool,
    missing: bool,
    key: Vec<u32>,
    tunnels: Vec<URing>,
    gone: Vec<URing>,
}

/// A layer's roads (their boxes in the frame, whether the map's ranges have a tunnel) and each one's tunnel geometry
/// once asked. The roads taken each call are those with a tunnel spec in that call's specs or a map tunnel.
#[pyclass(module = "earthfast", frozen)]
pub struct UnderpassIndex {
    boxes: Vec<[f64; 4]>,
    tagged: Vec<bool>,
    max_m: f64,
    got: Mutex<Vec<Option<Arc<Under>>>>,
}

#[pymethods]
impl UnderpassIndex {
    /// boxes (n, 4): the layer's features' boxes in the frame; tagged (n,): a tunnel among the map's ranges
    /// (railways._tunnel_tagged); max_m: roads.UNDERPASS_MAX_M.
    #[new]
    fn new(boxes: PyReadonlyArray2<'_, f64>, tagged: PyReadonlyArray1<'_, bool>, max_m: f64) -> PyResult<Self> {
        let (b, t) = (boxes.as_array(), tagged.as_array());
        let n = b.nrows();
        if t.len() != n || (n > 0 && b.ncols() != 4) {
            return Err(PyValueError::new_err("UnderpassIndex: boxes (n, 4) and tagged (n,)"));
        }
        Ok(UnderpassIndex {
            boxes: (0..n).map(|k| [b[[k, 0]], b[[k, 1]], b[[k, 2]], b[[k, 3]]]).collect(),
            tagged: t.iter().copied().collect(),
            max_m,
            got: Mutex::new(vec![None; n]),
        })
    }

    fn __len__(&self) -> usize {
        self.boxes.len()
    }
}

/// The (id, ends) keys of the tunnels taken so far in one road_underpasses call (its layers one after another).
#[pyclass(module = "earthfast", frozen)]
pub struct UnderpassSeen {
    keys: Mutex<HashSet<(Vec<u32>, [u64; 4])>>,
}

#[pymethods]
impl UnderpassSeen {
    #[new]
    fn new() -> Self {
        UnderpassSeen { keys: Mutex::new(HashSet::new()) }
    }
}

/// entry()'s answer: (spec, missing, id, tunnel parts [xy (k, 2)], missing tunnels [xy (k, 2)]).
type Entry<'py> = (bool, bool, Bound<'py, PyString>, Vec<PyReadonlyArray2<'py, f64>>, Vec<PyReadonlyArray2<'py, f64>>);

/// np.round(v, 2): v * 100 to the nearest even, / 100.
fn round2(v: f64) -> f64 {
    (v * 100.0).round_ties_even() / 100.0
}

impl UnderpassIndex {
    /// The rings of 2 points or more and 80 m (max_m) or less (railways._arc's last) with their keys.
    fn short(&self, rings: Vec<PyReadonlyArray2<'_, f64>>) -> PyResult<Vec<URing>> {
        let mut out = Vec::new();
        for xy in rings {
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
            out.push((Arc::new(p), [round2(f[0]), round2(f[1]), round2(l[0]), round2(l[1])]));
        }
        Ok(out)
    }

    /// Road k's tunnel geometry, made by entry(k) when first asked or when the road's tunnel spec came or went
    /// (no lock held while the Python runs).
    fn under(&self, k: usize, spec: bool, entry: &Bound<'_, PyAny>) -> PyResult<Arc<Under>> {
        if let Some(u) = &self.got.lock().unwrap_or_else(|e| e.into_inner())[k] {
            if u.spec == spec {
                return Ok(u.clone());
            }
        }
        let (spec, missing, fid, tunnels, gone): Entry<'_> = entry.call1((k,))?.extract()?;
        let u = Arc::new(Under { spec, missing, key: code_points(&fid)?, tunnels: self.short(tunnels)?,
                                 gone: self.short(gone)? });
        let old = self.got.lock().unwrap_or_else(|e| e.into_inner())[k].replace(u.clone());
        drop(old);
        Ok(u)
    }
}

/// railways.road_underpasses for one layer (index) within near, its tunnels appended to out: (xy (k, 2), half width)
/// of every short tunnel of the roads with a tunnel spec in specs (this call's roads.tunnel_specs) or a map tunnel whose
/// box meets near, road by road, ring by ring, each once by (id, its ends to the cm) over the call's layers (seen;
/// Python's tuple equality: -0.0 == 0.0, a NaN equal to nothing). ground() -> the layer's roads.road_specs, asked at
/// its first road taken with a map tunnel missing from its ranges; entry(i) -> (spec, missing, id, tunnel parts,
/// missing tunnels) makes a road's geometry; side: railways.UNDERPASS_SIDE_M (half width = spec.width / 2 + side).
#[pyfunction]
#[pyo3(signature = (index, specs, near, entry, ground, side, seen, out))]
#[allow(clippy::too_many_arguments)]
pub fn rail_underpass_select<'py>(py: Python<'py>, index: Bound<'py, UnderpassIndex>, specs: Bound<'py, PyList>,
                                  near: (f64, f64, f64, f64), entry: Bound<'py, PyAny>, ground: Bound<'py, PyAny>,
                                  side: Bound<'py, PyAny>, seen: Bound<'py, UnderpassSeen>, out: Bound<'py, PyList>)
                                  -> PyResult<()> {
    let ix = index.get();
    let n = ix.boxes.len();
    if specs.len() != n {
        return Err(PyValueError::new_err("rail_underpass_select: a spec per feature"));
    }
    let near = [near.0, near.1, near.2, near.3];
    let has: Vec<bool> = specs.iter().zip(&ix.tagged).map(|(sp, &t)| t || !sp.is_none()).collect();
    let mut ground_specs: Option<Bound<'py, PyAny>> = None;
    for (i, &h) in has.iter().enumerate() {
        if !h || !box_hit(&ix.boxes[i], &near) {
            continue;
        }
        let mut sp = specs.get_item(i)?;
        let u = ix.under(i, !sp.is_none(), &entry)?;
        let mut rings: Vec<&URing> = if sp.is_none() { Vec::new() } else { u.tunnels.iter().collect() };
        if u.missing {
            if ground_specs.is_none() {
                ground_specs = Some(ground.call0()?);
            }
            if !sp.is_truthy()? {
                sp = ground_specs.as_ref().expect("road specs").get_item(i)?;
            }
            if !sp.is_none() {
                rings.extend(u.gone.iter());
            }
        }
        if sp.is_none() {
            continue;
        }
        let half = sp.getattr("width")?.div(2)?.add(&side)?;
        for (p, key) in rings {
            if !key.iter().any(|v| v.is_nan()) {
                let bits = key.map(|v| if v == 0.0 { 0u64 } else { v.to_bits() });
                if !seen.get().keys.lock().unwrap_or_else(|e| e.into_inner()).insert((u.key.clone(), bits)) {
                    continue;
                }
            }
            let flat: Vec<f64> = p.iter().flat_map(|q| [q[0], q[1]]).collect();
            let arr = numpy::ndarray::Array2::from_shape_vec((p.len(), 2), flat)
                .map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py);
            out.append((arr, &half))?;
        }
    }
    Ok(())
}
