//! Terrain request state on the connection's event-loop thread. Python awaits
//! provider futures; transitions and replies run synchronously between awaits.
use pyo3::{
    exceptions::{PyKeyError, PyTypeError, PyValueError},
    prelude::*,
    types::{PyDict, PyList},
};
type Pt = (f64, f64);

fn active(c: &Bound<'_, PyAny>, aid: &Bound<'_, PyAny>) -> PyResult<bool> {
    aid.eq(c.getattr("active")?)
}
fn layout_block<'py>(c: &Bound<'py, PyAny>, p: Pt) -> PyResult<Bound<'py, PyAny>> {
    c.getattr("s")?
        .getattr("layout")?
        .call_method1("block_at", p)
}
fn pending(c: &Bound<'_, PyAny>, aid: &Bound<'_, PyAny>, p: Pt) -> PyResult<bool> {
    Ok(active(c, aid)? && c.getattr("pending_block")?.eq(layout_block(c, p)?)?)
}
fn coords(s: &Bound<'_, PyAny>, msg: &Bound<'_, PyAny>, num: &Bound<'_, PyAny>) -> PyResult<Pt> {
    let read = |key: &str| {
        s.call_method1("snap", (num.call1((msg, key))?,))?
            .extract::<f64>()
    };
    Ok((read("cx")?, read("cy")?))
}

#[pyfunction]
fn terrain_start<'py>(
    py: Python<'py>,
    c: &Bound<'py, PyAny>,
    msg: &Bound<'py, PyAny>,
    num: &Bound<'py, PyAny>,
    logger: &Bound<'py, PyAny>,
) -> PyResult<Option<(Bound<'py, PyAny>, Bound<'py, PyAny>, f64, f64)>> {
    let s = c.getattr("s")?;
    let got = (|| -> PyResult<_> {
        let aid = msg.get_item("anchorId")?;
        let a = s.getattr("anchors")?.get_item(&aid)?;
        let (x, y) = coords(&s, msg, num)?;
        Ok((aid, a, x, y))
    })();
    let (aid, a, x, y) = match got {
        Ok(v) => v,
        Err(e)
            if e.is_instance_of::<PyKeyError>(py)
                || e.is_instance_of::<PyValueError>(py)
                || e.is_instance_of::<PyTypeError>(py) =>
        {
            let m = PyDict::new(py);
            m.set_item("type", "terrain_error")?;
            m.set_item("requestId", msg.call_method1("get", ("requestId",))?)?;
            m.set_item("anchorId", msg.call_method1("get", ("anchorId",))?)?;
            m.set_item(
                "error",
                format!(
                    "invalid request: {} {}",
                    e.value(py).get_type().name()?,
                    e.value(py).str()?
                ),
            )?;
            c.call_method1("send", (m,))?;
            return Ok(None);
        }
        Err(e) => return Err(e),
    };
    if active(c, &aid)? && !layout_block(c, (x, y))?.eq(c.getattr("block")?)? {
        c.setattr("pending_block", layout_block(c, (x, y))?)?;
        let time = py.import("time")?;
        c.setattr("pending_at", time.call_method0("monotonic")?)?;
        c.setattr("pending_answered", false)?;
        let t = time.call_method0("perf_counter")?.extract::<f64>()?;
        c.call_method0("update_far")?;
        let dt = time.call_method0("perf_counter")?.extract::<f64>()? - t;
        if dt > 0.1 {
            logger.call_method1(
                if dt > 0.5 { "info" } else { "debug" },
                ("terrain request %s: update_far %.2f s", (&aid, x, y), dt),
            )?;
        }
    }
    Ok(Some((aid, a, x, y)))
}

#[pyfunction]
fn terrain_commit(
    c: &Bound<'_, PyAny>,
    msg: &Bound<'_, PyAny>,
    num: &Bound<'_, PyAny>,
    logger: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let aid = msg.call_method1("get", ("anchorId",))?;
    let s = c.getattr("s")?;
    let p = coords(&s, msg, num)?;
    let current = c.getattr("active")?;
    if !aid.eq(&current)? {
        if s.getattr("anchors")?.contains(&aid)? && (current.is_none() || aid.gt(&current)?) {
            c.call_method1("commit_anchor", (&aid,))?;
        } else {
            logger.call_method1(
                "warning",
                (
                    "commit_terrain for anchor %r (active %r) ignored",
                    &aid,
                    current,
                ),
            )?;
            return Ok(());
        }
    }
    let int = c.py().import("builtins")?.getattr("int")?;
    let confirmed = (&aid, int.call1((p.0,))?, int.call1((p.1,))?);
    c.setattr("confirmed_terrain", confirmed.clone())?;
    c.call_method1("release_legacy_terrain", (confirmed,))?;
    if !c.getattr("block")?.eq(layout_block(c, p)?)? {
        c.call_method1("_set_block", (aid, p.0, p.1))?;
        c.call_method0("update_far")?;
    }
    Ok(())
}

/// Send the revision before acknowledging the pending block. Once a later
/// request supersedes it, a legacy client's late reply must not re-pin it.
#[pyfunction]
fn terrain_deliver(
    py: Python<'_>,
    c: &Bound<'_, PyAny>,
    req: &Bound<'_, PyAny>,
    key: &Bound<'_, PyAny>,
    info: &Bound<'_, PyAny>,
) -> PyResult<()> {
    let aid = key.get_item(0)?;
    let message = PyDict::new(py);
    message.set_item("type", "terrain_ready")?;
    message.set_item("requestId", req)?;
    message.set_item("anchorId", &aid)?;
    let fields = c.call_method1("terrain_msg", (key, info))?;
    message.call_method1("update", (fields,))?;
    c.call_method1("send", (message,))?;
    if pending(
        c,
        &aid,
        (key.get_item(1)?.extract()?, key.get_item(2)?.extract()?),
    )? {
        c.setattr("pending_answered", true)?;
    }
    let current = c.getattr("confirmed_terrain")?;
    if current.is_truthy()?
        && active(c, &current.get_item(0)?)?
        && !c
            .getattr_opt("terrain_files")?
            .map_or(Ok(false), |v| v.is_truthy())?
    {
        c.call_method1("release_legacy_terrain", (current,))?;
    }
    Ok(())
}

#[pyfunction]
fn terrain_ready(
    py: Python<'_>,
    c: &Bound<'_, PyAny>,
    req: &Bound<'_, PyAny>,
    aid: &Bound<'_, PyAny>,
    p: Pt,
    info: &Bound<'_, PyAny>,
    provisional: bool,
    logger: &Bound<'_, PyAny>,
    start: f64,
) -> PyResult<()> {
    let key = (&aid, p.0, p.1);
    let wait: i64 = c.getattr("terrain_wait")?.extract()?;
    c.setattr("terrain_wait", wait - 1)?;
    if !provisional {
        c.getattr("s")?
            .getattr("hm_sent")?
            .call_method1("append", (key,))?;
    }
    let ms = (py
        .import("time")?
        .call_method0("perf_counter")?
        .extract::<f64>()?
        - start)
        * 1000.;
    let text = if provisional {
        "terrain_ready %s after %.0f ms (provisional: the graded block follows)"
    } else {
        "terrain_ready %s after %.0f ms"
    };
    logger.call_method1("info", (text, key, ms))?;
    c.call_method1("_send_terrain_ready", (req, key, info))?;
    Ok(())
}

#[pyfunction]
fn terrain_failed(
    py: Python<'_>,
    c: &Bound<'_, PyAny>,
    req: &Bound<'_, PyAny>,
    aid: &Bound<'_, PyAny>,
    p: Pt,
    err: &Bound<'_, PyAny>,
    logger: &Bound<'_, PyAny>,
) -> PyResult<()> {
    c.setattr(
        "terrain_wait",
        c.getattr("terrain_wait")?.extract::<i64>()? - 1,
    )?;
    logger.call_method1("exception", ("terrain request %s failed", req))?;
    let m = PyDict::new(py);
    m.set_item("type", "terrain_error")?;
    m.set_item("requestId", req)?;
    m.set_item("anchorId", aid)?;
    m.set_item(
        "error",
        format!("{}: {}", err.get_type().name()?, err.str()?),
    )?;
    c.call_method1("send", (m,))?;
    if pending(c, aid, p)? {
        c.setattr("pending_block", py.None())?;
        c.call_method0("update_far")?;
    }
    Ok(())
}

#[pyfunction]
fn terrain_message<'py>(
    py: Python<'py>,
    c: &Bound<'py, PyAny>,
    key: &Bound<'py, PyAny>,
    info: &Bound<'py, PyAny>,
    square: &Bound<'py, PyAny>,
    materials: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let s = c.getattr("s")?;
    let out = PyDict::new(py);
    out.set_item("cx", key.get_item(1)?)?;
    out.set_item("cy", key.get_item(2)?)?;
    out.set_item(
        "heightmap",
        format!("{}/{}", s.getattr("vfs_dir")?, info.get_item("heightmap")?),
    )?;
    let size = s.getattr("layout")?.getattr("block")?;
    out.set_item("size", &size)?;
    out.set_item("squareSize", square)?;
    for field in ["zBase", "maxHeight", "q"] {
        out.set_item(field, info.get_item(field)?)?;
    }
    let layers = info.call_method1("get", ("layers",))?;
    if layers.is_truthy()? {
        let mats = info.call_method1("get", ("mats",))?;
        let indices = if mats.is_truthy()? {
            mats
        } else {
            py.import("builtins")?
                .getattr("range")?
                .call1((materials.len()?,))?
        };
        let dir = s.getattr("vfs_dir")?.str()?;
        let names = PyList::empty(py);
        let entries = PyList::empty(py);
        let list = py.import("builtins")?.getattr("list")?;
        for i in indices.try_iter()? {
            let i = i?;
            names.append(format!("{dir}/{}", layers.get_item(&i)?.str()?))?;
            entries.append(list.call1((materials.get_item(i)?,))?)?;
        }
        out.set_item("layers", names)?;
        out.set_item("materials", entries)?;
    }
    let base = info.call_method1("get", ("base",))?;
    if base.is_truthy()? {
        out.set_item(
            "base",
            format!("{}/{}", s.getattr("vfs_dir")?.str()?, base.str()?),
        )?;
        s.call_method1("note_base", (key, base))?;
        out.set_item("baseSize", size.mul(square)?)?;
    }
    let ter = info.call_method1("get", ("ter",))?;
    if ter.is_truthy()? {
        out.set_item(
            "ter",
            format!("{}/{}", s.getattr("vfs_dir")?.str()?, ter.str()?),
        )?;
    }
    Ok(out)
}

#[pyfunction]
fn terrain_ready_set(py: Python<'_>, c: &Bound<'_, PyAny>, radius: f64) -> PyResult<()> {
    let aid = c.getattr("active")?;
    if aid.is_none()
        || !c.getattr("s")?.getattr("fast")?.is_truthy()?
        || !c
            .getattr_opt("ready_set")?
            .map_or(Ok(false), |v| v.is_truthy())?
    {
        return Ok(());
    }
    let (px, py_): Pt = c.getattr("focus")?.extract()?;
    let cs = PyList::empty(py);
    for key in py
        .import("builtins")?
        .getattr("list")?
        .call1((c.getattr("s")?.getattr("hm")?,))?
        .cast::<PyList>()?
        .iter()
    {
        if key.get_item(0)?.eq(&aid)? {
            let x = key.get_item(1)?.extract::<f64>()?;
            let y = key.get_item(2)?.extract::<f64>()?;
            if (x - px).abs().max((y - py_).abs()) <= radius {
                cs.append((key.get_item(1)?, key.get_item(2)?))?;
            }
        }
    }
    cs.call_method0("sort")?;
    if c.getattr_opt("ready_sent")?
        .map_or(Ok(false), |prev| cs.eq(&prev))?
    {
        return Ok(());
    }
    c.setattr("ready_sent", &cs)?;
    let pairs: Vec<Pt> = cs.extract()?;
    let m = PyDict::new(py);
    m.set_item("type", "terrain_ready_set")?;
    m.set_item("anchorId", aid)?;
    m.set_item(
        "centres",
        pairs
            .into_iter()
            .map(|(x, y)| vec![x, y])
            .collect::<Vec<_>>(),
    )?;
    c.call_method1("send", (m,))?;
    Ok(())
}

fn block_centre(block: &Bound<'_, PyAny>) -> PyResult<Pt> {
    Ok((
        block.getattr("cx")?.extract()?,
        block.getattr("cy")?.extract()?,
    ))
}

#[pyfunction]
fn terrain_ready_near(
    c: &Bound<'_, PyAny>,
    anchor: &Bound<'_, PyAny>,
    block: &Bound<'_, PyAny>,
    where_: Option<Pt>,
    busy: &Bound<'_, PyAny>,
    steps: f64,
) -> PyResult<bool> {
    let Some((x, y)) = where_ else {
        return Ok(false);
    };
    let s = c.getattr("s")?;
    let rule = s.getattr("follow")?;
    let (_, reach, _): (f64, f64, f64) = rule
        .call_method1(
            "mode",
            c.getattr("vel")?.cast_into::<pyo3::types::PyTuple>()?,
        )?
        .extract()?;
    let lim = steps * rule.getattr("grid")?.extract::<f64>()?;
    let target = block_centre(block)?;
    let current = c.getattr("block")?;
    let current = if current.is_none() {
        None
    } else {
        Some(block_centre(&current)?)
    };
    let aid = anchor.getattr("id")?;
    let list = c.py().import("builtins")?.getattr("list")?;
    let keys = [
        list.call1((s.getattr("hm")?,))?,
        list.call1((s.getattr("hm_inflight")?,))?,
        list.call1((busy,))?,
    ];
    for table in keys {
        for key in table.cast::<PyList>()?.iter() {
            if !key.get_item(0)?.eq(&aid)? {
                continue;
            }
            let pt: Pt = (key.get_item(1)?.extract()?, key.get_item(2)?.extract()?);
            if pt != target
                && Some(pt) != current
                && (pt.0 - target.0).abs().max((pt.1 - target.1).abs()) <= lim
                && (x - pt.0).abs().max((y - pt.1).abs()) <= reach
            {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(terrain_ready_near, m)?)?;
    m.add_function(wrap_pyfunction!(terrain_start, m)?)?;
    m.add_function(wrap_pyfunction!(terrain_commit, m)?)?;
    m.add_function(wrap_pyfunction!(terrain_deliver, m)?)?;
    m.add_function(wrap_pyfunction!(terrain_ready, m)?)?;
    m.add_function(wrap_pyfunction!(terrain_failed, m)?)?;
    m.add_function(wrap_pyfunction!(terrain_message, m)?)?;
    m.add_function(wrap_pyfunction!(terrain_ready_set, m)?)?;
    Ok(())
}
