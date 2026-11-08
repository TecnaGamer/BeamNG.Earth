//! Bounded, process-local reuse of raw WorldCover classes on the physics block's exact world lattice.
//! Never caches slope/sea/road overrides. Source identity tokens are weak-reference objects for immutable
//! MapData raster snapshots: retaining the tokens prevents pointer reuse without retaining the raster data.
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};

const CHUNK: usize = 64;
const AREA: usize = CHUNK * CHUNK;
static NEXT_OWNER: AtomicU64 = AtomicU64::new(1);
type Sources = Vec<(String, Py<PyAny>)>;
type Key = (i64, i64);

fn clone_sources(py: Python<'_>, src: &Sources) -> Sources {
    src.iter()
        .map(|(k, o)| (k.clone(), o.clone_ref(py)))
        .collect()
}
fn same_sources(a: &Sources, b: &Sources) -> bool {
    a.len() == b.len()
        && a.iter()
            .zip(b)
            .all(|((ka, oa), (kb, ob))| ka == kb && oa.as_ptr() == ob.as_ptr())
}
struct Entry {
    data: Vec<u8>,
    sources: Sources,
    used: u64,
}

#[pyclass]
pub struct ClassGridPlan {
    owner: u64,
    generation: u64,
    n: usize,
    output: Option<Vec<u8>>,
    missing: Vec<usize>,
    chunks: Vec<(Key, usize)>,
    sources: Sources,
}

#[pyclass]
pub struct ClassGridCache {
    owner: u64,
    generation: u64,
    limit: usize,
    tag: Vec<u64>,
    entries: HashMap<Key, Entry>,
    clock: u64,
    hits: u64,
    misses: u64,
}

#[pymethods]
impl ClassGridCache {
    #[new]
    fn new(max_bytes: usize) -> PyResult<Self> {
        if !(AREA..=32 * 1024 * 1024).contains(&max_bytes) {
            return Err(PyValueError::new_err(
                "class grid cache needs 4 KiB..32 MiB",
            ));
        }
        Ok(Self {
            owner: NEXT_OWNER.fetch_add(1, Ordering::Relaxed),
            generation: 0,
            limit: max_bytes / AREA,
            tag: vec![],
            entries: HashMap::new(),
            clock: 0,
            hits: 0,
            misses: 0,
        })
    }
    fn clear(&mut self) {
        self.entries.clear();
        self.generation = self.generation.wrapping_add(1);
    }
    #[getter]
    fn bytes(&self) -> usize {
        self.entries.len() * AREA
    }
    #[getter]
    fn hits(&self) -> u64 {
        self.hits
    }
    #[getter]
    fn misses(&self) -> u64 {
        self.misses
    }

    /// frame = (lat0, lon0, h0, square), origin in sample indices. A plan owns a snapshot, so overlapping requests
    /// cannot overwrite each other's pending state. Sources are weak-reference identity tokens in key order.
    fn lookup<'py>(
        &mut self,
        py: Python<'py>,
        frame: (f64, f64, f64, f64),
        warps: Vec<(f64, f64)>,
        ix0: i64,
        iy0: i64,
        n: usize,
        sources: Sources,
    ) -> PyResult<(ClassGridPlan, Bound<'py, PyArray1<i64>>)> {
        let mut sig = vec![frame.0, frame.1, frame.2, frame.3];
        for (size, amp) in warps {
            sig.extend([size, amp]);
        }
        if n == 0
            || n > 2048
            || sig.iter().any(|x| !x.is_finite())
            || frame.3 <= 0.0
            || ix0.checked_add(n as i64).is_none()
            || iy0.checked_add(n as i64).is_none()
        {
            return Err(PyValueError::new_err("invalid class grid lattice"));
        }
        let tag: Vec<_> = sig.iter().map(|x| x.to_bits()).collect();
        if tag != self.tag {
            self.clear();
            self.tag = tag;
        }
        let mut output = vec![0; n * n];
        let mut missing = Vec::new();
        let mut chunks = Vec::new();
        let aligned = n % CHUNK == 0
            && ix0.rem_euclid(CHUNK as i64) == 0
            && iy0.rem_euclid(CHUNK as i64) == 0;
        if aligned {
            for r in (0..n).step_by(CHUNK) {
                for c in (0..n).step_by(CHUNK) {
                    let key = (ix0 + c as i64, iy0 + r as i64);
                    self.clock = self.clock.wrapping_add(1);
                    let hit = self
                        .entries
                        .get_mut(&key)
                        .filter(|e| same_sources(&e.sources, &sources));
                    if let Some(e) = hit {
                        e.used = self.clock;
                        for y in 0..CHUNK {
                            output[(r + y) * n + c..(r + y) * n + c + CHUNK]
                                .copy_from_slice(&e.data[y * CHUNK..(y + 1) * CHUNK]);
                        }
                        self.hits += 1;
                    } else {
                        self.misses += 1;
                        chunks.push((key, missing.len()));
                        for y in r..r + CHUNK {
                            for x in c..c + CHUNK {
                                missing.push(y * n + x);
                            }
                        }
                    }
                }
            }
        } else {
            // Arbitrary point grids have no block-lattice guarantee: compute every point, retain none.
            missing.extend(0..n * n);
        }
        let indices = missing
            .iter()
            .map(|&x| x as i64)
            .collect::<Vec<_>>()
            .into_pyarray(py);
        Ok((
            ClassGridPlan {
                owner: self.owner,
                generation: self.generation,
                n,
                output: Some(output),
                missing,
                chunks,
                sources,
            },
            indices,
        ))
    }

    fn finish<'py>(
        &mut self,
        py: Python<'py>,
        mut plan: PyRefMut<'_, ClassGridPlan>,
        values: PyReadonlyArray1<'py, u8>,
        complete: bool,
    ) -> PyResult<Bound<'py, PyArray2<u8>>> {
        let v = values
            .as_slice()
            .map_err(|_| PyValueError::new_err("class values must be contiguous"))?;
        if plan.owner != self.owner || v.len() != plan.missing.len() || plan.output.is_none() {
            return Err(PyValueError::new_err(
                "invalid or already consumed class grid plan",
            ));
        }
        let mut out = plan.output.take().unwrap();
        for (&i, &value) in plan.missing.iter().zip(v.iter()) {
            out[i] = value;
        }
        if complete && plan.generation == self.generation {
            for &(key, start) in &plan.chunks {
                if !self.entries.contains_key(&key) && self.entries.len() >= self.limit {
                    if let Some((&old, _)) = self.entries.iter().min_by_key(|(_, e)| e.used) {
                        self.entries.remove(&old);
                    }
                }
                self.clock = self.clock.wrapping_add(1);
                let data = v[start..start + AREA].to_vec();
                self.entries.insert(
                    key,
                    Entry {
                        data,
                        sources: clone_sources(py, &plan.sources),
                        used: self.clock,
                    },
                );
            }
        }
        Ok(
            numpy::ndarray::Array2::from_shape_vec((plan.n, plan.n), out)
                .unwrap()
                .into_pyarray(py),
        )
    }
}

/// Conservative raw-grid bounds including the maximum warp. The extra metre covers libm/NumPy roundoff in
/// the bound only; actual class sampling keeps the existing math. Poles/antimeridian use uncached sampling.
#[pyfunction]
pub fn class_grid_bounds(
    lat: PyReadonlyArray2<'_, f64>,
    lon: PyReadonlyArray2<'_, f64>,
    warps: Vec<(f64, f64)>,
) -> PyResult<Option<(f64, f64, f64, f64)>> {
    let (la, lo) = (lat.as_array(), lon.as_array());
    if la.shape() != lo.shape()
        || la.is_empty()
        || warps.len() > 16
        || warps
            .iter()
            .any(|(s, a)| !s.is_finite() || *s <= 0.0 || !a.is_finite())
    {
        return Err(PyValueError::new_err("invalid class grid bounds"));
    }
    let (mut s, mut n, mut w, mut e) = (
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::INFINITY,
        f64::NEG_INFINITY,
    );
    for (&y, &x) in la.iter().zip(lo.iter()) {
        if !y.is_finite() || !x.is_finite() {
            return Err(PyValueError::new_err("non-finite class grid"));
        }
        s = s.min(y);
        n = n.max(y);
        w = w.min(x);
        e = e.max(x);
    }
    let metres = 1.0 + warps.iter().map(|(_, a)| a.abs()).sum::<f64>();
    let dy = metres / 111320.0;
    if s - dy <= -85.05 || n + dy >= 85.05 || e - w > 180.0 {
        return Ok(None);
    }
    let dx = metres / (111320.0 * s.abs().max(n.abs()).to_radians().cos()).max(1e-6);
    if w - dx < -180.0 || e + dx > 180.0 {
        return Ok(None);
    }
    Ok(Some((s - dy, w - dx, n + dy, e + dx)))
}
