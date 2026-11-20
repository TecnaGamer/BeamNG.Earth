//! A lease outlives its socket. A successful hello swaps a complete slot dictionary in
//! before releasing obsolete holds. The connection and lease must alias that dictionary.
use pyo3::class::gc::{PyTraverseError, PyVisit};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PySet, PyString, PyTuple};

type Obj<'py> = Bound<'py, PyAny>;

#[pyfunction]
fn lease_integer(value: &Obj<'_>) -> bool {
    if value.is_instance_of::<PyFloat>() {
        return value
            .extract::<f64>()
            .is_ok_and(|v| v.is_finite() && v.trunc() == v);
    }
    value.is_instance_of::<PyInt>() && !value.is_instance_of::<PyBool>()
}

fn invalid(msg: &str) -> PyErr {
    PyValueError::new_err(msg.to_owned())
}

#[pyfunction]
fn lease_snapshot<'py>(
    py: Python<'py>,
    value: &Obj<'py>,
    made: usize,
) -> PyResult<Bound<'py, PyDict>> {
    let out = PyDict::new(py);
    if value.cast::<PyDict>().is_ok_and(|d| d.is_empty()) {
        return Ok(out);
    }
    let list = value
        .cast::<PyList>()
        .map_err(|_| invalid("slotsHeld must be a complete array"))?;
    for pair in list {
        let pair = pair
            .cast::<PyList>()
            .ok()
            .filter(|p| p.len() == 2)
            .ok_or_else(|| invalid("invalid held-slot pair"))?;
        let n = pair.get_item(0)?;
        let serial = pair.get_item(1)?;
        if !lease_integer(&n)
            || !lease_integer(&serial)
            || n.lt(0)?
            || n.ge(made)?
            || serial.le(0)?
            || serial.gt((1u64 << 53) - 1)?
        {
            return Err(invalid("invalid held-slot number or serial"));
        }
        let n = n.call_method0("__int__")?;
        let serial = serial.call_method0("__int__")?;
        if out
            .get_item(&n)?
            .is_none_or(|old| old.lt(&serial).unwrap_or(false))
        {
            out.set_item(n, serial)?;
        }
    }
    Ok(out)
}

#[pyclass(frozen, module = "earthfast")]
struct StreamLeases {
    #[pyo3(get)]
    entries: Py<PyDict>,
}

#[pymethods]
impl StreamLeases {
    #[new]
    fn new(py: Python<'_>) -> Self {
        Self {
            entries: PyDict::new(py).unbind(),
        }
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.entries)
    }

    fn attach<'py>(
        &self,
        py: Python<'py>,
        connection: &Obj<'py>,
        token: &Obj<'py>,
        slots: &Obj<'py>,
        anchors: &Obj<'py>,
        resumable: bool,
    ) -> PyResult<(Obj<'py>, Obj<'py>, Obj<'py>)> {
        let released = PySet::empty(py)?.into_any();
        if let Some(lease) = connection.getattr("lease").ok().filter(|v| !v.is_none()) {
            return Ok((lease, released, py.None().into_bound(py)));
        }
        let entries = self.entries.bind(py);
        let existing = if resumable && token.is_instance_of::<PyString>() {
            entries.get_item(token)?
        } else {
            None
        };
        let lease = match existing {
            Some(l) if l.getattr("resumable")?.is_truthy()? => l,
            _ => {
                let l = py
                    .import("streamleases")?
                    .getattr("Lease")?
                    .call1((connection, resumable))?;
                entries.set_item(l.getattr("token")?, &l)?;
                l
            }
        };
        let previous = lease.getattr("connection")?;
        let released = if slots.is_none() {
            released
        } else {
            let old = lease.getattr("slots")?;
            let set = py.get_type::<PySet>();
            let diff = set
                .call1((old,))?
                .call_method1("__sub__", (set.call1((slots,))?,))?;
            lease.setattr("slots", slots)?;
            diff
        };
        lease.setattr("anchors", anchors)?;
        lease.setattr("connection", connection)?;
        lease.setattr("detached_at", py.None())?;
        connection.setattr("lease", &lease)?;
        connection.setattr("slot_holds", lease.getattr("slots")?)?;
        connection.setattr("held", anchors.call_method0("copy")?)?;
        connection.setattr("sessions", lease.getattr("sessions")?.call_method0("copy")?)?;
        Ok((
            lease,
            released,
            if previous.is(connection) {
                py.None().into_bound(py)
            } else {
                previous
            },
        ))
    }

    fn detach(&self, py: Python<'_>, connection: &Obj<'_>) -> PyResult<bool> {
        let Some(lease) = connection.getattr("lease").ok().filter(|v| !v.is_none()) else {
            return Ok(false);
        };
        if lease.getattr("connection")?.is(connection) {
            let anchors = connection.getattr("held")?.call_method0("copy")?;
            let active = connection.getattr("active")?;
            if !active.is_none() {
                anchors.call_method1("add", (active,))?;
            }
            lease.setattr("anchors", &anchors)?;
            let sessions = connection.getattr("sessions")?.call_method0("copy")?;
            lease.setattr("sessions", &sessions)?;
            lease.setattr("connection", py.None())?;
            lease.setattr("detached_at", py.import("time")?.call_method0("monotonic")?)?;
            if !lease.getattr("slots")?.is_truthy()?
                && !anchors.is_truthy()?
                && !sessions.is_truthy()?
            {
                self.entries
                    .bind(py)
                    .call_method1("pop", (lease.getattr("token")?, py.None()))?;
            }
        }
        Ok(true)
    }

    fn expire<'py>(&self, py: Python<'py>, now: f64, grace: f64) -> PyResult<Bound<'py, PyList>> {
        let entries = self.entries.bind(py);
        let gone = PyList::empty(py);
        for l in entries.values() {
            if l.getattr("connection")?.is_none()
                && l.getattr("detached_at")?
                    .extract::<Option<f64>>()?
                    .is_some_and(|t| now - t >= grace)
            {
                entries.del_item(l.getattr("token")?)?;
                gone.append(l)?;
            }
        }
        Ok(gone)
    }

    fn held(&self, py: Python<'_>, n: &Obj<'_>) -> PyResult<bool> {
        for l in self.entries.bind(py).values() {
            if l.getattr("slots")?.contains(n)? {
                return Ok(true);
            }
        }
        Ok(false)
    }

    fn anchors<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PySet>> {
        let out = PySet::empty(py)?;
        for l in self.entries.bind(py).values() {
            if l.getattr("connection")?.is_none() {
                out.call_method1("update", (l.getattr("anchors")?,))?;
            }
        }
        Ok(out)
    }

    fn sessions<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PySet>> {
        let out = PySet::empty(py)?;
        for l in self.entries.bind(py).values() {
            let c = l.getattr("connection")?;
            let owner = if c.is_none() { l } else { c };
            out.call_method1("update", (owner.getattr("sessions")?,))?;
        }
        Ok(out)
    }
}

#[pyfunction]
fn slots_gone(py: Python<'_>, conn: &Obj<'_>, gone: &Obj<'_>) -> PyResult<()> {
    if let Some(lease) = conn.getattr("lease").ok().filter(|v| !v.is_none()) {
        if !lease.getattr("connection")?.is(conn) {
            return Ok(());
        }
    }
    let list = if let Ok(d) = gone.cast::<PyDict>() {
        d.values()
    } else if let Ok(l) = gone.cast::<PyList>() {
        l.clone()
    } else {
        return Ok(());
    };
    let holds = conn.getattr("slot_holds")?.cast_into::<PyDict>()?;
    let released = PyList::empty(py);
    for g in list {
        let (n, serial) = if let Ok(d) = g.cast::<PyDict>() {
            let (Some(n), Some(serial)) = (d.get_item("1")?, d.get_item("2")?) else {
                continue;
            };
            (n, serial)
        } else if (g.is_instance_of::<PyList>() || g.is_instance_of::<PyTuple>()) && g.len()? == 2 {
            (g.get_item(0)?, g.get_item(1)?)
        } else {
            continue;
        };
        if lease_integer(&n) && lease_integer(&serial) {
            let n = n.call_method0("__int__")?;
            if let Some(held) = holds.get_item(&n)? {
                if held.eq(serial)? {
                    holds.del_item(&n)?;
                    released.append(n)?;
                }
            }
        }
    }
    conn.getattr("s")?
        .call_method1("slots_let_go", (released,))?;
    Ok(())
}

#[pyfunction]
fn claim_sessions(s: &Obj<'_>, conn: &Obj<'_>, raw: &Obj<'_>, log: &Obj<'_>) -> PyResult<()> {
    let py = s.py();
    let values = if let Ok(d) = raw.cast::<PyDict>() {
        d.values()
    } else if let Ok(l) = raw.cast::<PyList>() {
        l.clone()
    } else {
        return Ok(());
    };
    let orphans = s.getattr("orphans")?;
    let mine = PySet::empty(py)?;
    for sid in values {
        if sid.is_instance_of::<PyString>() && orphans.contains(&sid)? {
            mine.add(sid)?;
        }
    }
    let old = conn.getattr("sessions")?;
    let sorted = py.import("builtins")?.getattr("sorted")?;
    for sid in sorted
        .call1((mine.call_method1("difference", (&old,))?,))?
        .try_iter()?
    {
        let sid = sid?;
        log.call_method1("info",("adopting session %s: the game still shows its tiles, its files stay until it lets go",&sid))?;
        s.call_method1("_touch", (sid,))?;
    }
    let gone = old.call_method1("difference", (&mine,))?;
    conn.setattr("sessions", mine)?;
    let others = s.call_method0("claimed_sessions")?;
    for sid in sorted
        .call1((gone.call_method1("difference", (others,))?,))?
        .try_iter()?
    {
        s.call_method1("release_session", (sid?,))?;
    }
    Ok(())
}

#[pyfunction]
fn attach_stream<'py>(
    s: &Obj<'py>,
    conn: &Obj<'py>,
    msg: &Bound<'py, PyDict>,
    log: &Obj<'py>,
) -> PyResult<Obj<'py>> {
    let py = s.py();
    let field = |k| -> PyResult<Obj<'py>> {
        Ok(msg.get_item(k)?.unwrap_or_else(|| py.None().into_bound(py)))
    };
    let resumable = field("streamLease")?.eq(1)?;
    let sid = s.getattr("sid")?;
    let registry = s.getattr("stream_leases")?;
    let request = || -> PyResult<_> {
        let mut token = field("leaseId")?;
        if !field("leaseSession")?.eq(&sid)? || !token.is_instance_of::<PyString>() {
            token = py.None().into_bound(py);
        }
        let anchors = PySet::empty(py)?;
        if !field("slotSession")?.eq(&sid)? {
            if resumable && registry.getattr("entries")?.contains(&token)? {
                return Err(invalid(
                    "resuming a stream requires its complete slot snapshot",
                ));
            }
            return Ok((token, py.None().into_bound(py), anchors));
        }
        let slots = lease_snapshot(
            py,
            &field("slotsHeld")?,
            s.getattr("matslots")?.getattr("made")?.extract()?,
        )?;
        if let Some(held) = msg.get_item("held")? {
            if !held.cast::<PyDict>().is_ok_and(|d| d.is_empty()) {
                let list = held
                    .cast::<PyList>()
                    .map_err(|_| invalid("held anchors must be an array of integers"))?;
                for n in list {
                    if !lease_integer(&n) {
                        return Err(invalid("held anchors must be an array of integers"));
                    }
                    if s.getattr("anchors")?.contains(&n)? {
                        anchors.add(n.call_method0("__int__")?)?;
                    }
                }
            }
        }
        Ok((token, slots.into_any(), anchors))
    };
    let (token, slots, anchors) = match request() {
        Err(e) if e.is_instance_of::<PyValueError>(py) => {
            let peer = conn
                .getattr("peer")
                .unwrap_or_else(|_| py.None().into_bound(py));
            log.call_method1(
                "warning",
                (
                    "client %s: stream snapshot not resumed (%s): a new lease",
                    peer,
                    e.value(py),
                ),
            )?;
            (
                py.None().into_bound(py),
                py.None().into_bound(py),
                PySet::empty(py)?,
            )
        }
        r => r?,
    };
    let result = registry.call_method1("attach", (conn, token, slots, anchors, resumable))?;
    let previous = result.get_item(2)?;
    if !previous.is_none() {
        previous.call_method1("abort_stream", ("replaced by this game's reconnect",))?;
    }
    s.call_method1("slots_let_go", (result.get_item(1)?,))?;
    result.get_item(0)?.getattr("token")
}

#[pyfunction]
fn slot_held(s: &Obj<'_>, n: &Obj<'_>) -> PyResult<bool> {
    if s.getattr("stream_leases")?
        .call_method1("held", (n,))?
        .is_truthy()?
    {
        return Ok(true);
    }
    // Worker reads can interleave with a new accept or a disconnect.
    let connections = s
        .py()
        .get_type::<PyTuple>()
        .call1((s.getattr("connections")?,))?;
    for c in connections.try_iter()? {
        let c = c?;
        if c.getattr("lease").map_or(true, |l| l.is_none())
            && c.getattr("slot_holds")?.contains(n)?
        {
            return Ok(true);
        }
    }
    Ok(false)
}

#[pyfunction]
fn slots_let_go(s: &Obj<'_>, slots: &Obj<'_>) -> PyResult<()> {
    let pool = s.getattr("matslots")?;
    for n in slots.try_iter()? {
        let n = n?;
        if !s.call_method1("slot_held", (&n,))?.is_truthy()? {
            pool.call_method1("unheld", (n,))?;
        }
    }
    Ok(())
}

#[pyfunction]
fn held_anchors<'py>(s: &Obj<'py>, global: &Obj<'py>) -> PyResult<Obj<'py>> {
    let all = s.getattr("stream_leases")?.call_method0("anchors")?;
    for c in s.getattr("connections")?.try_iter()? {
        let c = c?;
        if let Some(lease) = c.getattr("lease").ok().filter(|v| !v.is_none()) {
            if !lease.getattr("connection")?.is(&c) {
                continue;
            }
        }
        all.call_method1("update", (c.getattr("held")?,))?;
    }
    all.call_method1("discard", (global,))?;
    Ok(all)
}

pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<StreamLeases>()?;
    m.add_function(wrap_pyfunction!(slots_let_go, m)?)?;
    m.add_function(wrap_pyfunction!(held_anchors, m)?)?;
    m.add_function(wrap_pyfunction!(attach_stream, m)?)?;
    m.add_function(wrap_pyfunction!(claim_sessions, m)?)?;
    m.add_function(wrap_pyfunction!(slot_held, m)?)?;
    m.add_function(wrap_pyfunction!(lease_integer, m)?)?;
    m.add_function(wrap_pyfunction!(lease_snapshot, m)?)?;
    m.add_function(wrap_pyfunction!(slots_gone, m)?)
}
