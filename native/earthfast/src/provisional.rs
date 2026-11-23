//! Provisional block scheduling and ownership of a block's stand-in photograph.
use pyo3::{
    exceptions::{PyAttributeError, PyException},
    prelude::*,
    types::{PyDict, PyList, PySet},
};
type Obj<'py> = Bound<'py, PyAny>;

fn optional<'py>(obj: &Obj<'py>, attr: &str) -> PyResult<Obj<'py>> {
    match obj.getattr(attr) {
        Err(e) if e.is_instance_of::<PyAttributeError>(obj.py()) => {
            Ok(obj.py().None().into_bound(obj.py()))
        }
        v => v,
    }
}

#[pyfunction]
fn provisional_eligible(c: &Obj<'_>, anchor: &Obj<'_>, delay: f64, first: bool) -> PyResult<bool> {
    Ok(delay > 0.
        && first
        && c.getattr("s")?.getattr("grading")?.is_truthy()?
        && anchor.getattr("id")?.eq(c.getattr("active")?)?
        && !optional(anchor, "stale")?.is_truthy()?
        && !optional(anchor, "stale_why")?.is_truthy()?)
}

/// Time allowed to the graded job after the planner returned. A slow or failed
/// planner is treated as planned, so the elapsed time always counts.
#[pyfunction]
fn provisional_wait(
    planner: &Obj<'_>,
    complete: bool,
    delay: f64,
    elapsed: f64,
) -> PyResult<(bool, f64)> {
    let planned = if complete {
        match planner.call_method0("result") {
            Ok(result) => result.is_truthy()?,
            Err(e) if e.is_instance_of::<PyException>(planner.py()) => true,
            Err(e) => return Err(e),
        }
    } else {
        true
    };
    Ok((
        planned,
        if planned {
            (delay - elapsed).max(0.)
        } else {
            0.
        },
    ))
}

#[pyfunction]
fn provisional_running(
    s: &Obj<'_>,
    key: &Obj<'_>,
    info: &Obj<'_>,
    planned: bool,
    delay: f64,
    elapsed: f64,
) -> PyResult<f64> {
    let left = delay - elapsed;
    if info.is_none() && planned {
        let proc = s.getattr("hm_proc")?.call_method1("get", (key,))?;
        if !proc.is_none() && proc.call_method0("running")?.is_truthy()? && left > 0. {
            return Ok(left);
        }
    }
    Ok(0.)
}

#[pyfunction]
fn provisional_sent(
    c: &Obj<'_>,
    key: &Obj<'_>,
    info: &Obj<'_>,
    refresh: bool,
    block_files: &Obj<'_>,
    logger: &Obj<'_>,
) -> PyResult<()> {
    let py = c.py();
    if !refresh || c.getattr("s")?.getattr("mapdata")?.is_none() || !info.is_truthy()? {
        return Ok(());
    }
    let roads = info.call_method1("get", ("roadsComplete",))?;
    let surface = info.call_method1("get", ("surfaceComplete",))?;
    let false_ = pyo3::types::PyBool::new(py, false);
    if roads.is(&false_) || surface.is(&false_) {
        let e = PyDict::new(py);
        e.set_item("files", PySet::empty(py)?)?;
        e.set_item("since", py.import("time")?.call_method0("monotonic")?)?;
        let entry = c
            .getattr("prov_live")?
            .call_method1("setdefault", (key, e))?;
        entry
            .get_item("files")?
            .call_method1("update", (block_files.call1((info,))?,))?;
        c.setattr("prov_last", key)?;
        logger.call_method1("info",("provisional block %s sent before its map cells (roads %s, surface %s): built again when they come",key,roads,surface))?;
    }
    Ok(())
}

#[pyfunction]
fn provisional_graded<'py>(c: &Obj<'py>, key: &Obj<'py>) -> PyResult<Bound<'py, PyList>> {
    let py = c.py();
    let e = c
        .getattr("prov_live")?
        .call_method1("pop", (key, py.None()))?;
    if e.is_none() {
        Ok(PyList::empty(py))
    } else {
        Ok(py
            .import("builtins")?
            .getattr("sorted")?
            .call1((e.get_item("files")?,))?
            .cast_into()?)
    }
}

// The server already shares this lock with its photo workers. Never run the
// photo renderer, wait on a future, or complete a future under the lock.
fn locked<T>(lock: &Obj<'_>, f: impl FnOnce() -> PyResult<T>) -> PyResult<T> {
    lock.call_method0("acquire")?;
    let result = f();
    lock.call_method0("release")?;
    result
}
enum Owner<'py> {
    Cached(Obj<'py>),
    Waiting(Obj<'py>),
    Building(Obj<'py>),
}

#[pyfunction]
fn standin_once<'py>(
    py: Python<'py>,
    s: &Obj<'py>,
    key: &Obj<'py>,
    make: &Obj<'py>,
    timeout: f64,
    keep: usize,
) -> PyResult<Obj<'py>> {
    let lock = s.getattr("quick_lock")?;
    let running = s.getattr("quick_running")?;
    let made = s.getattr("quick_made")?;
    let owner = locked(&lock, || {
        let name = made.call_method1("get", (key,))?;
        if name.is_truthy()? {
            let path = py.import("os")?.getattr("path")?;
            if path
                .call_method1(
                    "exists",
                    (path.call_method1("join", (s.getattr("out_dir")?, &name))?,),
                )?
                .is_truthy()?
            {
                return Ok(Owner::Cached(name));
            }
        }
        let fut = running.call_method1("get", (key,))?;
        if !fut.is_none() {
            return Ok(Owner::Waiting(fut));
        }
        let fut = py
            .import("concurrent.futures")?
            .getattr("Future")?
            .call0()?;
        running.set_item(key, &fut)?;
        Ok(Owner::Building(fut))
    })?;
    let fut = match owner {
        Owner::Cached(name) => return Ok(name),
        Owner::Waiting(fut) => {
            return match fut.call_method1("result", (timeout,)) {
                Err(e) if e.is_instance_of::<PyException>(py) => Ok(py.None().into_bound(py)),
                answer => answer,
            }
        }
        Owner::Building(fut) => fut,
    };
    let name = make.call0();
    let completion = match &name {
        Ok(name) => fut.call_method1("set_result", (name,)),
        Err(e) => fut.call_method1("set_exception", (e.value(py),)),
    };
    locked(&lock, || {
        running.call_method1("pop", (key, py.None()))?;
        if fut.call_method0("done")?.is_truthy()? && !fut.call_method0("exception")?.is_truthy()? {
            let name = fut.call_method0("result")?;
            if name.is_truthy()? {
                if made.len()? >= keep {
                    made.call_method0("clear")?;
                }
                made.set_item(key, name)?;
            }
        }
        Ok(())
    })?;
    completion?;
    name
}

fn provisional_pool<'py>(py: Python<'py>, s: &Obj<'py>, lane: &Obj<'py>) -> PyResult<Obj<'py>> {
    let photo = s.getattr("photo_pool")?;
    if photo.is_truthy()? {
        return Ok(photo);
    }
    let pools = s.getattr("block_pools")?;
    if pools.is_truthy()? {
        pools.get_item(lane)?.get_item(0)
    } else {
        Ok(py.None().into_bound(py))
    }
}

fn report_stages(
    info: &Obj<'_>,
    logger: &Obj<'_>,
    key: (&Obj<'_>, f64, f64),
    dt: f64,
    worker: bool,
) -> PyResult<()> {
    let message = if worker {
        "provisional block %s in %.2f s in a worker (%s)"
    } else {
        "provisional block %s in %.2f s (%s)"
    };
    log_stages(info, logger, key, dt, message)
}

fn log_stages(
    info: &Obj<'_>,
    logger: &Obj<'_>,
    key: (&Obj<'_>, f64, f64),
    dt: f64,
    message: &str,
) -> PyResult<()> {
    let py = info.py();
    let stages = info.call_method1("get", ("stages", PyDict::new(py)))?;
    let text = stages
        .call_method0("items")?
        .try_iter()?
        .map(|item| -> PyResult<String> {
            let item = item?;
            Ok(format!(
                "{} {}",
                item.get_item(0)?.str()?,
                item.get_item(1)?
                    .call_method1("__format__", (".2f",))?
                    .str()?
            ))
        })
        .collect::<PyResult<Vec<_>>>()?
        .join(" ");
    logger.call_method1("info", (message, key, dt, text))?;
    Ok(())
}

// Keep the private revision alive through finalization. The context manager
// also removes files from a build that failed after writing only some of them.
fn writing_block<'py, V>(
    s: &Obj<'py>,
    key: (&Obj<'py>, f64, f64),
    work: impl FnOnce(Obj<'py>) -> PyResult<V>,
) -> PyResult<V> {
    let py = s.py();
    let ctx = s.call_method1("writing_block", (key,))?;
    let names = ctx.call_method0("__enter__")?;
    let result = work(names);
    match &result {
        Ok(_) => ctx.call_method1("__exit__", (py.None(), py.None(), py.None()))?,
        Err(err) => ctx.call_method1(
            "__exit__",
            (err.get_type(py), err.value(py), err.traceback(py)),
        )?,
    };
    result
}

fn worker_heights<'py>(
    py: Python<'py>,
    pool: &Obj<'py>,
    s: &Obj<'py>,
    builder: &Obj<'py>,
    anchor: &Obj<'py>,
    (cx, cy): (f64, f64),
    q: &Obj<'py>,
    names: &Obj<'py>,
    ground_at: Option<&Obj<'py>>,
) -> PyResult<Obj<'py>> {
    let aid = anchor.getattr("id")?;
    let kw = PyDict::new(py);
    kw.set_item("provisional", true)?;
    if let Some(at) = ground_at {
        kw.set_item(
            "ground_at",
            py.import("builtins")?.getattr("tuple")?.call1((at,))?,
        )?;
    }
    let args = (
        builder,
        &aid,
        anchor.getattr("lat")?,
        anchor.getattr("lon")?,
        anchor.getattr("hm_zoom")?,
        q,
        cx,
        cy,
        s.getattr("layout")?.getattr("block")?,
        s.getattr("out_dir")?,
        names.get_item(0)?,
        names.get_item(1)?,
    );
    pool.call_method("submit", args, Some(&kw))?
        .call_method0("result")
}

/// A teleport uses the anchor-wide quantum, even for the provisional terrain.
/// Python's callbacks enter the DEM provider's urgent context while reading it.
#[pyfunction]
fn provisional_first<'py>(
    py: Python<'py>,
    s: &Obj<'py>,
    anchor: &Obj<'py>,
    p: (f64, f64),
    ground_at: &Obj<'py>,
    quantum: &Obj<'py>,
    local: &Obj<'py>,
    quick: &Obj<'py>,
    builder: &Obj<'py>,
    lane: &Obj<'py>,
    limits: (bool, f64, f64),
    logger: &Obj<'py>,
) -> PyResult<Obj<'py>> {
    let (cx, cy) = p;
    let (elsewhere, quick_speed, wait) = limits;
    let time = py.import("time")?;
    let now = || time.call_method0("perf_counter")?.extract::<f64>();
    let t0 = now()?;
    let q = quantum.call0()?;
    let base = if s.getattr("near_visual")?.eq("detail")? && !s.getattr("imagery")?.is_none() {
        let base = s.getattr("urgent_base_pool")?.call_method1(
            "submit",
            (
                s.getattr("_base_photo")?,
                anchor,
                cx,
                cy,
                py.None(),
                true,
                true,
            ),
        )?;
        anchor.setattr("first_base", (p, &base))?;
        base
    } else {
        py.None().into_bound(py)
    };
    let pool = provisional_pool(py, s, lane)?;
    let worker = !pool.is_none() && elsewhere;
    let aid = anchor.getattr("id")?;
    let key = (&aid, cx, cy);
    let info = if worker {
        if !base.is_none()
            && py
                .import("builtins")?
                .getattr("getattr")?
                .call1((s, "speed", 0.))?
                .lt(quick_speed)?
        {
            let fut = s
                .getattr("quick_early")?
                .call_method1("submit", (s.getattr("_quick_once")?, anchor, cx, cy, quick))?;
            s.getattr("quick_futs")?.set_item(key, fut)?;
        }
        let ts = time.call_method0("time")?.extract::<f64>()?;
        return writing_block(s, key, |names| {
            let info = worker_heights(
                py,
                &pool,
                s,
                builder,
                anchor,
                p,
                &q,
                &names,
                Some(ground_at),
            )?;
            let started = info.call_method1("pop", ("started", py.None()))?;
            let finished = info.call_method1("pop", ("finished", py.None()))?;
            if !started.is_none() && !finished.is_none() {
                let (started, finished) = (started.extract::<f64>()?, finished.extract::<f64>()?);
                let stages = info.call_method1("setdefault", ("stages", PyDict::new(py)))?;
                stages.set_item("queue", (started - ts).max(0.))?;
                stages.set_item("work", (finished - started).max(0.))?;
                stages.set_item(
                    "back",
                    (time.call_method0("time")?.extract::<f64>()? - finished).max(0.),
                )?;
            }
            let kw = PyDict::new(py);
            kw.set_item("urgent", true)?;
            kw.set_item("base_wait", wait)?;
            kw.set_item("ground", true)?;
            let info = s.call_method(
                "_finish_heightmap",
                (anchor, cx, cy, info, t0, base),
                Some(&kw),
            )?;
            report_stages(&info, logger, key, now()? - t0, true)?;
            Ok(info)
        });
    } else {
        local.call1((t0, q, base))?
    };
    report_stages(&info, logger, key, now()? - t0, worker)?;
    Ok(info)
}

/// The follow worker and its early photo run in parallel. Starting the photo
/// first preserves the stand-in deadline when both lanes are busy.
#[pyfunction]
fn provisional_build<'py>(
    py: Python<'py>,
    s: &Obj<'py>,
    anchor: &Obj<'py>,
    p: (f64, f64),
    lane: &Obj<'py>,
    builder: &Obj<'py>,
    wait: f64,
    logger: &Obj<'py>,
) -> PyResult<Obj<'py>> {
    let (cx, cy) = p;
    let pool = provisional_pool(py, s, lane)?;
    if pool.is_none() {
        return s.call_method1("make_provisional", (anchor, cx, cy, py.None()));
    }
    let perf = py.import("time")?.getattr("perf_counter")?;
    let t0 = perf.call0()?.extract::<f64>()?;
    let aid = anchor.getattr("id")?;
    let key = (&aid, cx, cy);
    let early = if s.getattr("near_visual")?.eq("detail")? && !s.getattr("imagery")?.is_none() {
        Some(
            s.getattr("quick_early")?
                .call_method1("submit", (s.getattr("_stand_in")?, anchor, cx, cy))?,
        )
    } else {
        None
    };
    writing_block(s, key, |names| {
        let q = s.call_method1("anchor_q", (anchor,))?;
        let info = worker_heights(py, &pool, s, builder, anchor, p, &q, &names, None)?;
        for k in ["started", "finished"] {
            info.call_method1("pop", (k, py.None()))?;
        }
        if let Some(f) = early {
            let kw = PyDict::new(py);
            kw.set_item("timeout", wait)?;
            let base = match f.call_method("result", (), Some(&kw)) {
                Ok(v) => v,
                Err(e) if e.is_instance_of::<PyException>(py) => py.None().into_bound(py),
                Err(e) => return Err(e),
            };
            info.set_item("base", &base)?;
            if !base.is_truthy()? {
                s.getattr("quick_pool")?
                    .call_method1("submit", (s.getattr("_quick_later")?, anchor, cx, cy))?;
            }
        }
        let dt = perf.call0()?.extract::<f64>()? - t0;
        info.set_item("seconds", dt)?;
        info.set_item("zoom", anchor.getattr("hm_zoom")?)?;
        report_stages(&info, logger, key, dt, true)?;
        Ok(info)
    })
}

// The follow and repaint workers both consume a private revision. A repaint has
// no photo job; the connection will attach the photo it last showed for this key.
#[pyfunction]
fn provisional_rebuild<'py>(
    py: Python<'py>,
    s: &Obj<'py>,
    anchor: &Obj<'py>,
    p: (f64, f64),
    lane: &Obj<'py>,
    builder: &Obj<'py>,
    logger: &Obj<'py>,
) -> PyResult<Obj<'py>> {
    let (cx, cy) = p;
    let aid = anchor.getattr("id")?;
    let key = (&aid, cx, cy);
    let perf = py.import("time")?.getattr("perf_counter")?;
    let t0 = perf.call0()?.extract::<f64>()?;
    let pool = provisional_pool(py, s, lane)?;
    let info = if pool.is_none() {
        let kw = PyDict::new(py);
        kw.set_item("provisional", true)?;
        let q = s.call_method1("anchor_q", (anchor,))?;
        s.call_method(
            "_make_heightmap_here",
            (anchor, cx, cy, py.None(), t0, q, py.None()),
            Some(&kw),
        )?
    } else {
        let info = writing_block(s, key, |names| {
            let q = s.call_method1("anchor_q", (anchor,))?;
            worker_heights(py, &pool, s, builder, anchor, p, &q, &names, None)
        })?;
        for field in ["started", "finished"] {
            info.call_method1("pop", (field, py.None()))?;
        }
        info
    };
    info.call_method1("pop", ("base", py.None()))?;
    let dt = perf.call0()?.extract::<f64>()? - t0;
    info.set_item("seconds", dt)?;
    info.set_item("zoom", anchor.getattr("hm_zoom")?)?;
    log_stages(
        &info,
        logger,
        key,
        dt,
        "provisional block %s built again with its roads in %.2f s (%s)",
    )?;
    Ok(info)
}

#[pyfunction]
fn provisional_refresh(
    c: &Obj<'_>,
    aid: &Obj<'_>,
    now: &Obj<'_>,
    max_age: f64,
    files_of: &Obj<'_>,
    cells_ready: &Obj<'_>,
) -> PyResult<()> {
    let py = c.py();
    let live = c.getattr("prov_live")?;
    let s = c.getattr("s")?;
    if c.getattr("prov_busy")?.is_truthy()? || !live.is_truthy()? || s.getattr("mapdata")?.is_none()
    {
        return Ok(());
    }
    let now = if now.is_none() {
        py.import("time")?
            .call_method0("monotonic")?
            .extract::<f64>()?
    } else {
        now.extract()?
    };
    let builtins = py.import("builtins")?;
    let list = builtins.getattr("list")?;
    let pairs = list.call1((live.call_method0("items")?,))?;
    for pair in pairs.try_iter()? {
        let pair = pair?;
        let (key, entry) = (pair.get_item(0)?, pair.get_item(1)?);
        if !key.get_item(0)?.eq(aid)? || now - entry.get_item("since")?.extract::<f64>()? > max_age
        {
            live.call_method1("pop", (&key, py.None()))?;
            let cached = s.getattr("prov_info")?.call_method1("get", (&key,))?;
            let cached = if cached.is_truthy()? {
                cached
            } else {
                PyDict::new(py).into_any()
            };
            let obsolete = entry
                .get_item("files")?
                .call_method1("difference", (files_of.call1((cached,))?,))?;
            s.call_method1(
                "retire_terrain",
                (builtins.getattr("sorted")?.call1((obsolete,))?,),
            )?;
            continue;
        }
        if entry.call_method1("get", ("refreshed",))?.is_truthy()? {
            continue;
        }
        let committed = list.call1((s.getattr("hm_committed")?,))?;
        if !(committed.len()? > 0 && committed.get_item(-1)?.eq(&key)?)
            && !key.eq(c.getattr("prov_last")?)?
        {
            continue;
        }
        let anchor = s.getattr("anchors")?.call_method1("get", (aid,))?;
        if anchor.is_none() {
            return Ok(());
        }
        if cells_ready.call1((&s, &anchor, &key))?.is_truthy()? {
            c.setattr("prov_busy", true)?;
            entry.set_item("refreshed", true)?;
            c.call_method1(
                "spawn",
                (c.call_method1("_prov_refresh_run", (anchor, key, entry))?,),
            )?;
            return Ok(());
        }
    }
    Ok(())
}

#[pyfunction]
fn provisional_publish(
    c: &Obj<'_>,
    key: &Obj<'_>,
    entry: &Obj<'_>,
    info: &Obj<'_>,
    base: &Obj<'_>,
    files_of: &Obj<'_>,
    logger: &Obj<'_>,
) -> PyResult<()> {
    let py = c.py();
    let s = c.getattr("s")?;
    if base.is_truthy()? {
        info.set_item("base", base)?;
    }
    let files = entry.get_item("files")?;
    let builtins = py.import("builtins")?;
    s.call_method1(
        "retire_terrain",
        (builtins.getattr("sorted")?.call1((&files,))?,),
    )?;
    files.call_method0("clear")?;
    files.call_method1("update", (files_of.call1((info,))?,))?;
    let cache = s.getattr("prov_info")?;
    if cache.contains(key)? {
        cache.set_item(key, info)?;
    }
    let age = py
        .import("time")?
        .call_method0("monotonic")?
        .extract::<f64>()?
        - entry.get_item("since")?.extract::<f64>()?;
    let stages = info.call_method1("get", ("stages", PyDict::new(py)))?;
    let samples = builtins
        .getattr("int")?
        .call1((stages.call_method1("get", ("l0Samples", 0))?,))?;
    let ms = 1000.
        * stages
            .call_method1("get", ("l0roads", 0.0))?
            .extract::<f64>()?;
    logger.call_method1("info", ("provisional block %s sent again with its roads, %.1f s after it was first sent (L0 roads: %s samples in %.0f ms)",key,age,samples,ms))?;
    let m = PyDict::new(py);
    m.set_item("type", "terrain_refresh")?;
    m.set_item("anchorId", key.get_item(0)?)?;
    m.call_method1("update", (c.call_method1("terrain_msg", (key, info))?,))?;
    c.call_method1("send", (m,))?;
    Ok(())
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(provisional_rebuild, m)?)?;
    m.add_function(wrap_pyfunction!(provisional_refresh, m)?)?;
    m.add_function(wrap_pyfunction!(provisional_publish, m)?)?;
    m.add_function(wrap_pyfunction!(provisional_first, m)?)?;
    m.add_function(wrap_pyfunction!(provisional_build, m)?)?;
    m.add_function(wrap_pyfunction!(provisional_eligible, m)?)?;
    m.add_function(wrap_pyfunction!(provisional_wait, m)?)?;
    m.add_function(wrap_pyfunction!(provisional_running, m)?)?;
    m.add_function(wrap_pyfunction!(provisional_sent, m)?)?;
    m.add_function(wrap_pyfunction!(provisional_graded, m)?)?;
    m.add_function(wrap_pyfunction!(standin_once, m)?)?;
    Ok(())
}
