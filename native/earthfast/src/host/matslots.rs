//! A material and its shape path share a slot. Reuse requires the tile owner and every
//! game lease to have released it. Workers can allocate while the loop reconciles a hello.
use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};

use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::exceptions::{PyIndexError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::MutexExt;
use pyo3::types::{PyBytes, PyDict, PyList, PySet};

const LOW: usize = 32;
const TARGET: usize = 128;

#[derive(Default)]
struct Numbers {
    made: usize,
    free: Vec<usize>,
    waiting: BTreeSet<usize>,
}

#[pyclass(frozen, module = "earthfast")]
pub(super) struct MatSlots {
    pub(super) returned: Arc<Mutex<Vec<usize>>>,
    #[pyo3(get)]
    prefix: String,
    held: Option<Py<PyAny>>,
    // Python keys include both (anchor, tile key) and ("shape", serial). Their insertion
    // order breaks ties when reclaiming owners without a tile record.
    slots: Py<PyDict>,
    numbers: Mutex<Numbers>,
}

impl MatSlots {
    fn reclaim_from<'py>(
        &self,
        py: Python<'py>,
        idle: &Bound<'py, PyAny>,
        eligible: impl Fn(&Bound<'py, PyAny>) -> PyResult<bool>,
    ) -> PyResult<usize> {
        let slots = self.slots.bind(py);
        let mut freed = 0;
        for ck in idle.try_iter()? {
            let ck = ck?;
            let mut state = self.state(py);
            if state.free.len() >= TARGET {
                break;
            }
            if !eligible(&ck)? {
                continue;
            }
            if let Some(n) = slots.get_item(&ck)? {
                let n = n.extract()?;
                if !self.held(py, n)? {
                    slots.del_item(ck)?;
                    state.free.push(n);
                    freed += 1;
                }
            }
        }
        Ok(freed)
    }

    fn state(&self, py: Python<'_>) -> std::sync::MutexGuard<'_, Numbers> {
        let mut state = self.numbers.lock_py_attached(py).unwrap();
        state.free.append(&mut self.returned.lock().unwrap());
        state
    }

    fn allocate(&self, py: Python<'_>, state: &mut Numbers) -> PyResult<usize> {
        let n = loop {
            let Some(&n) = state.free.last() else {
                let n = state.made;
                state.made += 1;
                break n;
            };
            let held = self.held(py, n)?;
            state.free.pop();
            if !held {
                break n;
            }
            state.waiting.insert(n);
        };
        Ok(n)
    }

    pub(super) fn reserve(&self, py: Python<'_>) -> PyResult<usize> {
        self.allocate(py, &mut self.state(py))
    }

    pub(super) fn published(&self, py: Python<'_>, n: usize) {
        self.state(py).waiting.insert(n);
    }

    pub(super) fn held(&self, py: Python<'_>, n: usize) -> PyResult<bool> {
        match &self.held {
            Some(f) => f.bind(py).call1((n,))?.is_truthy(),
            None => Ok(false),
        }
    }
}

#[pymethods]
impl MatSlots {
    #[new]
    #[pyo3(signature=(session, held=None))]
    fn new(py: Python<'_>, session: &str, held: Option<Py<PyAny>>) -> Self {
        Self {
            returned: Arc::default(),
            prefix: format!("bngEarthFar_{session}_"),
            held,
            slots: PyDict::new(py).unbind(),
            numbers: Mutex::default(),
        }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.held)?;
        visit.call(&self.slots)
    }

    fn name(&self, n: usize) -> String {
        format!("{}{n:05}", self.prefix)
    }

    #[staticmethod]
    fn number(name: &str) -> PyResult<usize> {
        name.rsplit_once('_')
            .ok_or_else(|| PyIndexError::new_err("list index out of range"))?
            .1
            .parse()
            .map_err(|_| PyValueError::new_err("invalid slot number"))
    }

    #[staticmethod]
    #[pyo3(signature=(name, ext=".cdae"))]
    fn file(name: &str, ext: &str) -> PyResult<String> {
        let (_, suffix) = name
            .rsplit_once('_')
            .ok_or_else(|| PyIndexError::new_err("list index out of range"))?;
        Ok(format!("slot_{suffix}{ext}"))
    }

    fn name_in(&self, data: &[u8]) -> Option<String> {
        let prefix = self.prefix.as_bytes();
        data.windows(prefix.len() + 5)
            .find(|w| w.starts_with(prefix) && w[prefix.len()..].iter().all(u8::is_ascii_digit))
            .and_then(|w| std::str::from_utf8(w).ok())
            .map(str::to_owned)
    }

    /// Worker only. The tile's own file can carry an earlier name after several renewals.
    fn write_reslot(
        &self,
        py: Python<'_>,
        src: std::path::PathBuf,
        dst: &Bound<'_, PyAny>,
        old: &str,
        new: &str,
        pooled: bool,
        compress: bool,
    ) -> PyResult<()> {
        let staging = py.import("staging")?;
        let tmp = staging.call_method1("tmp_for", (dst,))?;
        let tmp_path = tmp.extract::<std::path::PathBuf>()?;
        py.detach(|| -> PyResult<()> {
            let raw = std::fs::read(&src)?;
            let packed = pooled && crate::cdaez::is_packed(&raw).unwrap_or(false);
            let mut data = if packed {
                crate::cdaez::plain(&raw)
                    .map_err(PyValueError::new_err)?
                    .into_owned()
            } else {
                raw
            };
            let have = if !old.is_empty() && data.windows(old.len()).any(|w| w == old.as_bytes()) {
                old.to_owned()
            } else {
                self.name_in(&data).ok_or_else(|| {
                    PyValueError::new_err(format!(
                        "no pooled name in {}",
                        src.file_name().unwrap_or_default().to_string_lossy()
                    ))
                })?
            };
            if have != new {
                data = crate::farrestore::restamp(&data, have.as_bytes(), new.as_bytes())
                    .map_err(PyValueError::new_err)?;
            }
            if packed && compress {
                data = crate::cdaez::packed(&data, crate::cdaez::LEVEL)
                    .map_err(PyValueError::new_err)?;
            }
            std::fs::write(tmp_path, data)?;
            Ok(())
        })?;
        staging.call_method1("replace", (tmp, dst))?;
        Ok(())
    }

    #[getter]
    fn made(&self, py: Python<'_>) -> usize {
        self.state(py).made
    }

    #[getter]
    fn free(&self, py: Python<'_>) -> Vec<usize> {
        self.state(py).free.clone()
    }

    #[getter]
    fn waiting(&self, py: Python<'_>) -> BTreeSet<usize> {
        self.state(py).waiting.clone()
    }

    fn take(&self, py: Python<'_>, ck: &Bound<'_, PyAny>) -> PyResult<String> {
        let mut state = self.state(py);
        let slots = self.slots.bind(py);
        if let Some(n) = slots.get_item(ck)? {
            return Ok(self.name(n.extract()?));
        }
        let n = self.allocate(py, &mut state)?;
        slots.set_item(ck, n)?;
        Ok(self.name(n))
    }

    fn owns(&self, py: Python<'_>, ck: &Bound<'_, PyAny>, name: &str) -> PyResult<bool> {
        let _guard = self.state(py);
        match self.slots.bind(py).get_item(ck)? {
            Some(n) => Ok(self.name(n.extract()?) == name),
            None => Ok(false),
        }
    }

    fn release(&self, py: Python<'_>, ck: &Bound<'_, PyAny>) -> PyResult<()> {
        let mut state = self.state(py);
        let slots = self.slots.bind(py);
        if let Some(n) = slots.get_item(ck)? {
            let n = n.extract()?;
            let held = self.held(py, n)?;
            slots.del_item(ck)?;
            if held {
                state.waiting.insert(n);
            } else {
                state.free.push(n);
            }
        }
        Ok(())
    }

    fn unheld(&self, py: Python<'_>, n: usize) {
        let mut state = self.state(py);
        if state.waiting.remove(&n) {
            state.free.push(n);
        }
    }

    fn short(&self, py: Python<'_>) -> bool {
        self.state(py).free.len() < LOW
    }

    fn reclaim(&self, py: Python<'_>, idle: &Bound<'_, PyAny>) -> PyResult<usize> {
        self.reclaim_from(py, idle, |_| Ok(true))
    }

    /// Lost build owners first, then cached records in the server's LRU order.
    fn reclaim_server(&self, py: Python<'_>, s: &Bound<'_, PyAny>) -> PyResult<usize> {
        let used = s.call_method0("names_in_use")?;
        let tiles = s.getattr("tiles")?;
        let building = s.getattr("tile_inflight")?;
        let idle = |k: &Bound<'_, PyAny>| -> PyResult<bool> {
            Ok(!used.contains(k)? && !building.contains(k)?)
        };
        let lost = self.reclaim_from(py, self.holders(py).as_any(), |k| {
            Ok(!tiles.contains(k)? && idle(k)?)
        })?;
        Ok(lost + self.reclaim_from(py, &tiles, idle)?)
    }

    fn holders<'py>(&self, py: Python<'py>) -> Bound<'py, PyList> {
        let _guard = self.state(py);
        self.slots.bind(py).keys()
    }

    fn held_owners<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PySet>> {
        let _guard = self.state(py);
        let out = PySet::empty(py)?;
        for (ck, n) in self.slots.bind(py).iter() {
            if self.held(py, n.extract()?)? {
                out.add(ck)?;
            }
        }
        Ok(out)
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let state = self.state(py);
        let d = PyDict::new(py);
        d.set_item("made", state.made)?;
        d.set_item("owned", self.slots.bind(py).len())?;
        d.set_item("free", state.free.len())?;
        d.set_item("waiting", state.waiting.len())?;
        Ok(d)
    }
}

#[pyfunction]
fn slot_restamp<'py>(
    py: Python<'py>,
    data: &Bound<'py, PyBytes>,
    old: &str,
    new: &str,
) -> PyResult<Bound<'py, PyBytes>> {
    if old.len() != new.len() {
        return Err(PyValueError::new_err("names differ in length"));
    }
    if old.is_empty() {
        return Ok(data.clone());
    }
    let out = crate::farrestore::restamp(data.as_bytes(), old.as_bytes(), new.as_bytes())
        .map_err(|_| PyValueError::new_err(format!("{old} not in the shape")))?;
    Ok(PyBytes::new(py, &out))
}

pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<MatSlots>()?;
    m.add("SLOT_TARGET", TARGET)?;
    m.add_function(wrap_pyfunction!(slot_restamp, m)?)
}
