//! Per-connection attribution. The label tables stay with their providers in credits.py.
mod features;
mod models;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::{BTreeMap, BTreeSet};

type Obj<'py> = Bound<'py, PyAny>;

fn strings(ids: &Obj<'_>) -> PyResult<Vec<String>> {
    ids.try_iter()?.map(|i| i?.extract()).collect()
}

fn ordered(ids: &mut [String], order: &Obj<'_>) -> PyResult<()> {
    let ranks: BTreeMap<String, usize> = strings(order)?
        .into_iter()
        .enumerate()
        .map(|(n, id)| (id, n))
        .collect();
    ids.sort_by(|a, b| {
        ranks
            .get(a)
            .unwrap_or(&usize::MAX)
            .cmp(ranks.get(b).unwrap_or(&usize::MAX))
            .then(a.cmp(b))
    });
    Ok(())
}

#[pyfunction]
fn credit_ordered(py: Python<'_>, ids: &Obj<'_>) -> PyResult<Vec<String>> {
    let mut ids = strings(ids)?;
    ordered(&mut ids, &py.import("credits")?.getattr("ORDER")?)?;
    Ok(ids)
}

fn line(labels: &Bound<'_, PyModule>, ids: &[String], far: bool) -> PyResult<String> {
    let mut grouped: Vec<(&str, Vec<String>)> = vec![];
    let label = labels.getattr("label")?;
    for id in ids {
        let kind = id.split(':').next().unwrap_or("");
        let pos = grouped
            .iter()
            .position(|(k, _)| *k == kind)
            .unwrap_or_else(|| {
                grouped.push((kind, Vec::new()));
                grouped.len() - 1
            });
        let name = label.call1((id,))?.extract::<String>()?;
        if !grouped[pos].1.contains(&name) {
            grouped[pos].1.push(name);
        }
    }
    let mut parts = vec![];
    for g in labels.getattr("GROUPS")?.try_iter()? {
        let (kind, title, far_title): (String, String, String) = g?.extract()?;
        if let Some(i) = grouped.iter().position(|(k, _)| *k == kind) {
            let (_, names) = grouped.remove(i);
            parts.push(format!(
                "{}: {}",
                if far { far_title } else { title },
                names.join(", ")
            ));
        }
    }
    parts.extend(grouped.iter().map(|(_, names)| names.join(", ")));
    Ok(parts.join(" | "))
}

#[pyfunction]
#[pyo3(signature=(ids, far=false))]
fn credit_line(py: Python<'_>, ids: &Obj<'_>, far: bool) -> PyResult<String> {
    line(&py.import("credits")?, &credit_ordered(py, ids)?, far)
}

#[pyfunction]
fn credit_message<'py>(
    py: Python<'py>,
    conn: &Obj<'py>,
    near_m: f64,
    hyst: f64,
    previous: &Obj<'py>,
) -> PyResult<Option<Bound<'py, PyDict>>> {
    if conn.getattr("active")?.is_none() {
        return Ok(None);
    }
    let (x, y): (f64, f64) = conn.getattr("focus")?.extract()?;
    let tiles = conn.getattr("s")?.getattr("tiles")?;
    let mut known = false;
    let mut near = BTreeSet::new();
    let mut far = BTreeSet::new();
    let keep: BTreeSet<_> = strings(previous)?.into_iter().collect();
    let mut add = |ck: &Obj<'_>, [x0, y0, x1, y1]: [f64; 4]| -> PyResult<()> {
        let info = tiles.call_method1("get", (ck,))?;
        let Some(ids) = info.getattr("credits").ok().filter(|v| !v.is_none()) else {
            return Ok(());
        };
        known = true;
        if !ids.is_truthy()? {
            return Ok(());
        }
        let d = crate::py_hypot((x0 - x).max(0.).max(x - x1), (y0 - y).max(0.).max(y - y1));
        for id in strings(&ids)? {
            if d <= near_m || (d <= near_m + hyst && keep.contains(&id)) {
                near.insert(id);
            } else {
                far.insert(id);
            }
        }
        Ok(())
    };
    if let Some(far) = conn
        .getattr("far")
        .ok()
        .and_then(|v| v.cast_into::<super::farstate::FarState>().ok())
    {
        far.borrow()
            .visit_shown(py, |ck, bbox| add(ck.as_any(), bbox))?;
    } else {
        for (ck, bb) in conn.getattr("shown")?.cast::<PyDict>()?.iter() {
            add(&ck, bb.extract()?)?;
        }
    }
    if !known {
        return Ok(None);
    }
    let labels = py.import("credits")?;
    if !conn.getattr("weather")?.getattr("msg")?.is_none() {
        near.insert("wx:open-meteo".to_owned());
    }
    match features::kept(py, conn) {
        Ok(ids) => near.extend(ids),
        Err(e) => {
            labels
                .getattr("log")?
                .call_method1("error", (format!("credits: feature sources failed: {e}"),))?;
        }
    }
    let mut far: Vec<_> = far.difference(&near).cloned().collect();
    let mut near: Vec<_> = near.into_iter().collect();
    let order = labels.getattr("ORDER")?;
    ordered(&mut near, &order)?;
    ordered(&mut far, &order)?;
    let m = PyDict::new(py);
    m.set_item("type", "credits")?;
    m.set_item("v", labels.getattr("VERSION")?)?;
    m.set_item("near", line(&labels, &near, false)?)?;
    m.set_item("far", line(&labels, &far, true)?)?;
    m.set_item("ids", near)?;
    m.set_item("farIds", far)?;
    Ok(Some(m))
}

#[pyfunction]
fn credit_poke(tr: &Obj<'_>, conn: &Obj<'_>) -> PyResult<()> {
    let cr = conn.getattr("s")?.getattr("credits")?;
    if cr.is_none() || !tr.getattr("timer")?.is_none() {
        return Ok(());
    }
    let delay = tr.getattr("last_check")?.extract::<f64>()?
        + cr.getattr("interval")?.extract::<f64>()?
        - cr.call_method0("clock")?.extract::<f64>()?;
    tr.setattr(
        "timer",
        conn.getattr("loop")?
            .call_method1("call_later", (delay.max(0.), tr.getattr("check")?, conn))?,
    )
}

#[pyfunction]
fn credit_check(py: Python<'_>, tr: &Obj<'_>, conn: &Obj<'_>) -> PyResult<()> {
    tr.setattr("timer", py.None())?;
    let cr = conn.getattr("s")?.getattr("credits")?;
    if cr.is_none() {
        return Ok(());
    }
    tr.setattr("last_check", cr.call_method0("clock")?)?;
    tr.setattr("checks", tr.getattr("checks")?.extract::<usize>()? + 1)?;
    let prev = tr.getattr("msg")?;
    let ids = if prev.is_none() {
        PyList::empty(py).into_any()
    } else {
        prev.get_item("ids")?
    };
    match cr.call_method1("message", (conn, ids)) {
        Ok(msg) if !msg.is_none() && !msg.eq(&prev)? => {
            tr.setattr("msg", &msg)?;
            conn.call_method1("send", (msg,))?;
        }
        Err(e) => {
            py.import("credits")?
                .getattr("log")?
                .call_method1("error", (format!("credits failed: {e}"),))?;
        }
        _ => (),
    }
    Ok(())
}

pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(models::credited_model_near, m)?)?;
    m.add_function(wrap_pyfunction!(features::credit_features, m)?)?;
    m.add_function(wrap_pyfunction!(credit_ordered, m)?)?;
    m.add_function(wrap_pyfunction!(credit_line, m)?)?;
    m.add_function(wrap_pyfunction!(credit_message, m)?)?;
    m.add_function(wrap_pyfunction!(credit_poke, m)?)?;
    m.add_function(wrap_pyfunction!(credit_check, m)?)
}
