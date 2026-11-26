//! A reclaimed material is restamped by a tile worker. A single producer owns the
//! cache entry until it publishes, including when a connection cancels its waiter.
use pyo3::exceptions::{PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::path::PathBuf;

type Obj<'py> = Bound<'py, PyAny>;

#[pyfunction]
fn write_renewal<'py>(
    s: &Obj<'py>,
    ck: &Obj<'py>,
    info: &Obj<'py>,
    packed: bool,
) -> PyResult<Obj<'py>> {
    let py = s.py();
    let pool = s.getattr("matslots")?;
    let material = pool.call_method1("take", (ck,))?;
    let mut tile: String = s
        .call_method1("tile_name", (ck.get_item(0)?, ck.get_item(1)?))?
        .extract()?;
    // A photo refresh writes an immutable cliff mesh; retain it when its material slot changes.
    let mesh_rev = info.getattr("mesh_rev").and_then(|v| v.extract::<u64>()).unwrap_or(0);
    if mesh_rev != 0 {
        let stem = tile.rsplit_once('.').map_or(tile.as_str(), |(stem, _)| stem);
        tile = format!("{stem}_r{mesh_rev}.cdae");
    }
    let format = s.getattr("far_format")?;
    let pooled = format.eq("cdae")?;
    let name = if pooled {
        pool.call_method1("file", (&material,))?
            .extract::<String>()?
    } else {
        let stem = tile
            .rsplit_once('.')
            .map_or(tile.as_str(), |(stem, _)| stem);
        let serial = s.getattr("reslot_serial")?.call_method0("__next__")?;
        format!("{stem}_s{serial}.{format}")
    };
    let dir: PathBuf = s.getattr("out_dir")?.extract()?;
    pool.call_method1(
        "write_reslot",
        (
            dir.join(tile),
            dir.join(&name),
            info.getattr("material")?,
            &material,
            pooled,
            packed,
        ),
    )?;
    let kw = PyDict::new(py);
    kw.set_item("material", material)?;
    kw.set_item("shape", name)?;
    py.import("dataclasses")?
        .getattr("replace")?
        .call((info,), Some(&kw))
}

fn publish<'py>(
    s: &Obj<'py>,
    ck: &Obj<'py>,
    old: &Obj<'py>,
    renewed: Obj<'py>,
) -> PyResult<Obj<'py>> {
    if !s.getattr("far_format")?.eq("cdae")? {
        let old_shape = old.getattr("shape")?;
        if !old_shape.is_none() {
            let dir: PathBuf = s.getattr("out_dir")?.extract()?;
            let path = dir.join(old_shape.extract::<PathBuf>()?);
            s.call_method1("queue_delete", (PyList::new(s.py(), [path])?,))?;
        }
    }
    s.getattr("tiles")?.set_item(ck, &renewed)?;
    Ok(renewed)
}

#[pyfunction]
fn reslot_sync<'py>(s: &Obj<'py>, ck: &Obj<'py>, old: &Obj<'py>) -> PyResult<Obj<'py>> {
    match s.call_method1("_write_reslot", (ck, old)) {
        Ok(result) => publish(s, ck, old, result),
        Err(e)
            if e.is_instance_of::<PyOSError>(s.py())
                || e.is_instance_of::<PyValueError>(s.py()) =>
        {
            s.getattr("matslots")?.call_method1("release", (ck,))?;
            Ok(old.clone())
        }
        Err(e) => Err(e),
    }
}

#[pyfunction]
fn start_renewal<'py>(
    s: &Obj<'py>,
    ck: &Obj<'py>,
    info: &Obj<'py>,
    anchor: &Obj<'py>,
    tile: &Obj<'py>,
) -> PyResult<Obj<'py>> {
    let aio = s.py().import("asyncio")?;
    let inflight = s.getattr("tile_inflight")?;
    let existing = inflight.call_method1("get", (ck,))?;
    if !existing.is_none() {
        return aio.call_method1("shield", (existing,));
    }
    let loop_ = aio.call_method0("get_running_loop")?;
    let task = loop_.call_method1(
        "create_task",
        (s.call_method1("_renew_slot", (&loop_, ck, info, anchor, tile))?,),
    )?;
    s.getattr("renewing")?.call_method1("add", (ck,))?;
    inflight.set_item(ck, &task)?;
    aio.call_method1("shield", (task,))
}

#[pyfunction]
fn finish_renewal<'py>(
    s: &Obj<'py>,
    ck: &Obj<'py>,
    old: &Obj<'py>,
    result: Obj<'py>,
) -> PyResult<Obj<'py>> {
    publish(s, ck, old, result)
}

#[pyfunction]
fn rebuild_renewal<'py>(
    s: &Obj<'py>,
    ck: &Obj<'py>,
    anchor: &Obj<'py>,
    tile: &Obj<'py>,
    loop_: &Obj<'py>,
) -> PyResult<Obj<'py>> {
    s.getattr("tiles")?
        .call_method1("pop", (ck, s.py().None()))?;
    s.getattr("matslots")?.call_method1("release", (ck,))?;
    loop_.call_method1(
        "run_in_executor",
        (
            s.getattr("tile_pool")?,
            s.getattr("make_tile")?,
            anchor,
            tile,
            s.py().None(),
        ),
    )
}

pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(write_renewal, m)?)?;
    m.add_function(wrap_pyfunction!(reslot_sync, m)?)?;
    m.add_function(wrap_pyfunction!(start_renewal, m)?)?;
    m.add_function(wrap_pyfunction!(finish_renewal, m)?)?;
    m.add_function(wrap_pyfunction!(rebuild_renewal, m)?)
}
