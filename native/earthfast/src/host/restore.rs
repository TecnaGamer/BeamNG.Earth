//! Chunked restore on the server's restore executor. One find can overlap one write.
//! All tile-cache publication and streamer wakes run on the loop.
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
type Obj<'py> = Bound<'py, PyAny>;

fn wake(s: &Obj<'_>) -> PyResult<()> {
    for c in s.getattr("connections")?.try_iter()? {
        c?.getattr("wake")?.call_method0("set")?;
    }
    Ok(())
}

#[pyfunction]
pub(super) fn restore_finished(
    s: &Obj<'_>,
    cks: &Obj<'_>,
    future: &Obj<'_>,
    log: &Obj<'_>,
) -> PyResult<()> {
    if !future.call_method0("cancelled")?.is_truthy()? {
        let error = future.call_method0("exception")?;
        if !error.is_none() {
            log.call_method1("warning", ("far restore failed: %r", error))?;
        }
    }
    let pending = s.getattr("restoring")?;
    for ck in cks.try_iter()? {
        if pending.contains(ck?)? {
            pending.call_method1("difference_update", (cks,))?;
            return wake(s);
        }
    }
    Ok(())
}

#[pyfunction]
pub(super) fn restore_publish(
    s: &Obj<'_>,
    cks: &Obj<'_>,
    infos: &Bound<'_, PyDict>,
) -> PyResult<()> {
    let loop_ = s.py().import("asyncio")?.call_method0("get_running_loop")?;
    let pending = s.getattr("restoring")?;
    let tiles = s.getattr("tiles")?;
    let inflight = s.getattr("tile_inflight")?;
    for ck in cks.try_iter()? {
        let ck = ck?;
        pending.call_method1("discard", (&ck,))?;
        if let Some(info) = infos.get_item(&ck)? {
            if !info.is_none() && !tiles.contains(&ck)? && !inflight.contains(&ck)? {
                let f = loop_.call_method0("create_future")?;
                f.call_method1("set_result", (info,))?;
                s.call_method1("_tile_done", (ck, f))?;
            }
        }
    }
    wake(s)
}

#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub(super) fn restore_batches<'py>(
    s: &Obj<'py>,
    anchor: &Obj<'py>,
    tiles: &Bound<'py, PyList>,
    loop_: &Obj<'py>,
    globals: &Bound<'py, PyDict>,
    first: usize,
    chunk: usize,
) -> PyResult<()> {
    let py = s.py();
    let n = s.call_method0("fin_name_len")?;
    if !n.is_truthy()? || tiles.is_empty() {
        return Ok(());
    }
    let im = s.getattr("imagery")?;
    let fin = if !s.getattr("colormatch")?.is_none() || !s.getattr("deshadow")?.is_none() {
        let fin = PyDict::new(py);
        for src in im.getattr("chain")?.try_iter()? {
            let src = src?;
            let name = src.getattr("name")?;
            fin.set_item(
                &name,
                im.getattr("_fin_sigs")?
                    .call_method1("setdefault", (&name, im.call_method1("_fin_sig", (src,))?))?,
            )?;
        }
        fin.into_any()
    } else {
        py.None().into_bound(py)
    };
    let clock = py.import("time")?.getattr("perf_counter")?;
    let now = || clock.call0()?.extract::<f64>();
    let aid = anchor.getattr("id")?;
    let keys = |batch: &Bound<'py, PyList>| -> PyResult<Bound<'py, PyList>> {
        let ks = PyList::empty(py);
        for t in batch {
            ks.append(s.call_method1("far_ck", (&aid, t.getattr("key")?))?)?;
        }
        Ok(ks)
    };
    let find = globals
        .as_any()
        .get_item("blockworker")?
        .getattr("far_restore_find")?;
    let restore = super::restore_tiles::Restore {
        server: s,
        anchor,
        globals,
        name_len: n.extract()?,
        fin,
    };
    let submit = |at: usize, count: usize| -> PyResult<_> {
        let t0 = now()?;
        let batch = tiles.get_slice(at, (at + count).min(tiles.len()));
        let (specs, prep, why) = restore.prepare(&batch)?;
        let future = if specs.is_truthy()? {
            Some(s.getattr("far_pool")?.call_method1(
                "submit",
                (
                    &find,
                    &aid,
                    anchor.getattr("lat")?,
                    anchor.getattr("lon")?,
                    specs,
                ),
            )?)
        } else {
            None
        };
        Ok((batch, prep, why, future, t0))
    };
    let mut job = submit(0usize, first)?;
    let mut at = first;
    let stats = s.getattr("restore_stats")?;
    loop {
        let (batch, prep, why, future, t0) = job;
        let found = match future {
            Some(f) => f.call_method0("result")?,
            None => PyList::empty(py).into_any(),
        };
        let t1 = now()?;
        let mut more = at < tiles.len();
        if more && at == first {
            more = false;
            for f in found.try_iter()? {
                if !f?.is_none() {
                    more = true;
                    break;
                }
            }
        }
        let next = if more { Some(submit(at, chunk)?) } else { None };
        let (infos, t2) = restore.write(prep, &found, &why, t0)?;
        for (k, add) in [
            ("batches", 1),
            ("asked", batch.len()),
            ("restored", infos.len()),
        ] {
            stats.set_item(k, stats.get_item(k)?.extract::<usize>()? + add)?;
        }
        stats.set_item(
            "seconds",
            stats.get_item("seconds")?.extract::<f64>()? + (now()? - t0),
        )?;
        let reasons = if why.is_truthy()? {
            let sorted = py
                .import("builtins")?
                .call_method1("sorted", (why.call_method0("items")?,))?;
            py.get_type::<PyDict>().call1((sorted,))?
        } else {
            "-".into_pyobject(py)?.into_any()
        };
        globals.as_any().get_item("log")?.call_method1(
            "info",
            (
                "far restore: %d of %d tiles in %.0f ms (find %.0f, write %.0f); not kept: %s",
                infos.len(),
                batch.len(),
                1000. * (now()? - t0),
                1000. * (t1 - t0),
                1000. * (t2 - t1),
                reasons,
            ),
        )?;
        loop_.call_method1(
            "call_soon_threadsafe",
            (s.getattr("_restored")?, keys(&batch)?, infos),
        )?;
        let Some(next) = next else {
            if at < tiles.len() {
                loop_.call_method1(
                    "call_soon_threadsafe",
                    (
                        s.getattr("_restored")?,
                        keys(&tiles.get_slice(at, tiles.len()))?,
                        PyDict::new(py),
                    ),
                )?;
            }
            return Ok(());
        };
        at += chunk;
        job = next;
    }
}

pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(restore_finished, m)?)?;
    m.add_function(wrap_pyfunction!(restore_publish, m)?)?;
    m.add_function(wrap_pyfunction!(restore_batches, m)?)
}
