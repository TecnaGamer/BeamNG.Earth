//! Shape links are made alongside each tile build. An unpublished slot belongs to the
//! build result (including while a swap holds it); a published one belongs to the lease.
use pyo3::exceptions::PyOSError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use super::matslots::MatSlots;
type Obj<'py> = Bound<'py, PyAny>;

#[pyclass(module = "earthfast")]
pub(super) struct PreparedShapes {
    files: BTreeMap<String, Vec<usize>>,
    returned: Arc<Mutex<Vec<usize>>>,
}

impl Drop for PreparedShapes {
    fn drop(&mut self) {
        // A cancelled build or a superseded tracker can disappear on any thread. No GIL,
        // server callback or lease mutation in Drop. take() rechecks all recycled numbers.
        let mut returned = self.returned.lock().unwrap();
        for nums in self.files.values_mut() {
            returned.append(nums);
        }
    }
}

impl PreparedShapes {
    fn new(pool: &MatSlots) -> Self {
        Self {
            files: BTreeMap::new(),
            returned: pool.returned.clone(),
        }
    }

    fn stage(&mut self, s: &Obj<'_>, pool: &MatSlots, filename: String) -> PyResult<()> {
        let dir = s.getattr("out_dir")?.extract::<PathBuf>()?;
        let src = dir.join(&filename);
        if !src.exists() {
            return Ok(());
        }
        let py = s.py();
        let place = py.import("matslots")?.getattr("place")?;
        let n = pool.reserve(py)?;
        let dst = dir.join(format!("slot_{n:05}.cdae"));
        if let Err(e) = place.call1((src, dst)) {
            self.returned.lock().unwrap().push(n);
            if !e.is_instance_of::<PyOSError>(py) {
                return Err(e);
            }
        } else {
            self.files.entry(filename).or_default().push(n);
        }
        Ok(())
    }
}

#[pymethods]
impl PreparedShapes {
    /// Called on the loop only after the tracker's epoch, digest and swap checks.
    fn claim<'py>(
        &mut self,
        conn: &Obj<'py>,
        files: &Bound<'py, PyDict>,
    ) -> PyResult<Bound<'py, PyDict>> {
        let py = conn.py();
        let out = PyDict::new(py);
        if conn
            .getattr("closing")
            .is_ok_and(|v| v.is_truthy().unwrap_or(false))
        {
            return Ok(out);
        }
        if let Some(l) = conn.getattr("lease").ok().filter(|v| !v.is_none()) {
            if !l.getattr("connection")?.is(conn) {
                return Ok(out);
            }
        }
        let s = conn.getattr("s")?;
        let pool_obj = s.getattr("matslots")?;
        let pool: PyRef<'_, MatSlots> = pool_obj.extract()?;
        let serials = s.getattr("slot_serial")?;
        let holds = conn.getattr("slot_holds")?;
        let dir = s.getattr("vfs_dir")?.extract::<String>()?;
        for (what, file) in files {
            let Some(nums) = self.files.get_mut(&file.extract::<String>()?) else {
                continue;
            };
            if let Some(&n) = nums.last() {
                // A hello can report a previously unknown hold after the worker reserved this path.
                // Keep that game's hold and let this message use its ordinary shape file.
                if pool.held(py, n)? {
                    pool.published(py, n);
                    nums.pop();
                    continue;
                }
                // The original counter advanced for both the transient owner and the send.
                serials.call_method0("__next__")?;
                let serial = serials.call_method0("__next__")?;
                holds.set_item(n, &serial)?;
                pool.published(py, n);
                nums.pop();
                let pair = PyList::empty(py);
                pair.append(format!("{dir}/slot_{n:05}.cdae"))?;
                pair.append(serial)?;
                out.set_item(what, pair)?;
            }
        }
        Ok(out)
    }
}

#[pyfunction]
pub(super) fn prepare_shapes(
    s: &Obj<'_>,
    kind: &str,
    geo: bool,
    aid: &Obj<'_>,
    key: &Obj<'_>,
    res: &Obj<'_>,
) -> PyResult<PreparedShapes> {
    let py = s.py();
    let pool_obj = s.getattr("matslots")?;
    let pool: PyRef<'_, MatSlots> = pool_obj.extract()?;
    let mut placed = PreparedShapes::new(&pool);
    if res.is_none() || !s.getattr("far_format")?.eq("cdae")? {
        return Ok(placed);
    }
    let x = key.get_item(0)?;
    let y = key.get_item(1)?;
    let mut stage = |name: &Obj<'_>| placed.stage(s, &pool, name.extract()?);
    if kind != "roads" {
        if geo && res.len()? > 4 {
            let shapes = res.get_item(4)?;
            if shapes.is_truthy()? {
                for (_, name) in shapes.cast::<PyDict>()? {
                    stage(&name)?;
                }
            }
        } else if !geo && matches!(kind, "bldfar" | "bldsky" | "brfar" | "bldpack") {
            placed.stage(s, &pool, format!("{kind}_{aid}_{x}_{y}.cdae"))?;
        }
    } else if !geo {
        for (_, name) in py
            .import("roads")?
            .call_method1("shape_files", (aid, x, y))?
            .cast::<PyDict>()?
        {
            stage(&name)?;
        }
    } else {
        let k = py.import("geotiles")?.call_method1("key", (x, y))?;
        let names = py.import("blockworker")?.getattr("geo_names")?;
        let exact = res.call_method1("get", ("exact",))?;
        if res.call_method1("get", ("content",))?.is_truthy()? {
            for (what, name) in names.call1((&k,))?.cast::<PyDict>()? {
                // A late build may still send base content without its previous anchor's exact parts.
                if !what.eq("inputs")? && !what.eq("json")? {
                    stage(&name)?;
                }
            }
        }
        if exact.is_truthy()? {
            for (_, path) in exact.cast::<PyDict>()? {
                let p = path.extract::<PathBuf>()?;
                if let Some(name) = p.file_name() {
                    stage(&name.into_pyobject(py)?.into_any())?;
                }
            }
        }
        if res.call_method1("get", ("skin",))?.is_truthy()? {
            for (what, name) in names.call1((&k, aid))?.cast::<PyDict>()? {
                if !what.eq("json")? {
                    stage(&name)?;
                    // A swap may send the skin twice, with independent game holds.
                    if res.call_method1("get", ("content",))?.is_truthy()? {
                        stage(&name)?;
                    }
                }
            }
        }
    }
    Ok(placed)
}

#[pyfunction]
pub(super) fn prepare_shape_files(
    s: &Obj<'_>,
    files: &Bound<'_, PyDict>,
) -> PyResult<PreparedShapes> {
    let pool_obj = s.getattr("matslots")?;
    let pool: PyRef<'_, MatSlots> = pool_obj.extract()?;
    let mut prepared = PreparedShapes::new(&pool);
    if s.getattr("far_format")?.eq("cdae")? {
        for (_, name) in files {
            prepared.stage(s, &pool, name.extract()?)?;
        }
    }
    Ok(prepared)
}
