use super::*;

/// Ready road tiles open the sign queue. A missing road during a refresh keeps its
/// visible signs until it leaves the unload radius.
#[pyfunction]
pub(super) fn sign_tiles<'py>(
    py: Python<'py>,
    tr: &Obj<'py>,
    aid: i64,
    pos: Point,
    ready: &Obj<'py>,
    version: &Obj<'py>,
    tile: f64,
    drop_m: f64,
    workers: usize,
    now: f64,
) -> PyResult<(Vec<Key>, Vec<Key>)> {
    if tr.getattr("closed")?.is_truthy()? {
        return Ok((vec![], vec![]));
    }
    if !tr.getattr("version")?.eq(version)? {
        tr.call_method0("refresh")?;
        tr.setattr("version", version)?;
    }
    let sent = set(tr, "sent")?;
    let stale = set(tr, "stale")?;
    let retry = dict(tr, "retry")?;
    if !tr.getattr("aid")?.eq(aid)? {
        tr.setattr("aid", aid)?;
        sent.clear();
        stale.clear();
        retry.clear();
    }
    let wanted = py
        .import("builtins")?
        .getattr("set")?
        .call1((ready,))?
        .cast_into::<PySet>()?;
    for k in sent.iter() {
        if distance(k.extract()?, pos, tile, 1) <= drop_m {
            wanted.add(k)?;
        }
    }
    tr.setattr("wanted", &wanted)?;
    let drops = sent
        .call_method1("difference", (&wanted,))?
        .cast_into::<PySet>()?
        .iter()
        .map(|k| k.extract())
        .collect::<PyResult<_>>()?;
    sent.call_method1("intersection_update", (&wanted,))?;
    stale.call_method1("intersection_update", (&wanted,))?;
    let keep = PyDict::new(py);
    for (k, t) in retry.iter() {
        if wanted.contains(&k)? {
            keep.set_item(k, t)?;
        }
    }
    tr.setattr("retry", &keep)?;
    let jobs = dict(tr, "jobs")?;
    let mut want = Vec::new();
    for key in wanted.iter() {
        if (!sent.contains(&key)? || stale.contains(&key)?)
            && !jobs.contains((aid, &key))?
            && keep
                .get_item(&key)?
                .map(|t| t.extract::<f64>())
                .transpose()?
                .unwrap_or(0.)
                <= now
        {
            let k = key.extract()?;
            want.push((distance(k, pos, tile, 1), k));
        }
    }
    want.sort_by(|a, b| a.0.total_cmp(&b.0));
    Ok((
        want.into_iter()
            .take(workers.saturating_sub(jobs.len()))
            .map(|(_, k)| k)
            .collect(),
        drops,
    ))
}

/// The completion belongs to a specific anchor and sign-data generation. An empty first
/// tile counts as sent, an empty replacement must reach the client to remove old shapes.
#[pyfunction]
pub(super) fn sign_result(
    tr: &Obj<'_>,
    aid: i64,
    key: Key,
    revision: i64,
    failed: bool,
    n: Option<i64>,
    now: f64,
) -> PyResult<bool> {
    if tr.getattr("closed")?.is_truthy()? || !tr.getattr("aid")?.eq(aid)? || !set(tr, "wanted")?.contains(key)? {
        return Ok(false);
    }
    if !failed && !tr.getattr("revision")?.eq(revision)? {
        set(tr, "stale")?.add(key)?;
        return Ok(false);
    }
    if n.is_none() {
        dict(tr, "retry")?.set_item(key, now + 10.)?;
        return Ok(failed); // report the builder exception
    }
    if n == Some(0) && !set(tr, "stale")?.contains(key)? {
        set(tr, "sent")?.add(key)?;
        return Ok(false);
    }
    Ok(true)
}
