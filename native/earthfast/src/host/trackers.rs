//! Per-pose tile scheduling. Python holds the externally visible tile sets, asyncio futures,
//! and builder callbacks. Keep the previous set operations where their iteration order breaks
//! ties in a stable sort (including tiles exactly the same distance from the camera).
use std::collections::HashSet;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PySet};

use crate::geotiles::{quad, quad_dist, Frame};

pub(super) mod pose;
mod roads;
mod scenery;
mod signs;
use scenery::TileFrame;

type Key = (i64, i64);
type Point = (f64, f64);
type Obj<'py> = Bound<'py, PyAny>;

fn set<'py>(tr: &Obj<'py>, name: &str) -> PyResult<Bound<'py, PySet>> {
    Ok(tr.getattr(name)?.cast_into()?)
}

fn dict<'py>(tr: &Obj<'py>, name: &str) -> PyResult<Bound<'py, PyDict>> {
    Ok(tr.getattr(name)?.cast_into()?)
}

fn frame(f: &Obj<'_>) -> PyResult<Frame> {
    Ok(Frame::new(f.getattr("lat0")?.extract()?, f.getattr("lon0")?.extract()?))
}

struct Focus {
    at: Point,
    radius: f64,
}

fn focuses(foci: &Obj<'_>) -> PyResult<Vec<Focus>> {
    foci.try_iter()?
        .map(|f| {
            let f = f?;
            Ok(Focus {
                at: (f.get_item(0)?.extract()?, f.get_item(1)?.extract()?),
                radius: f.get_item(2)?.extract()?,
            })
        })
        .collect()
}

fn distance(k: Key, p: Point, size: f64, per: i64) -> f64 {
    let x = k.0.div_euclid(per) as f64 * size;
    let y = k.1.div_euclid(per) as f64 * size;
    crate::py_hypot(
        (x - p.0).max(p.0 - (x + size)).max(0.),
        (y - p.1).max(p.1 - (y + size)).max(0.),
    )
}

#[pyfunction]
fn tracker_near(x: f64, y: f64, r: f64, tile: f64, unit: i64, group: Option<i64>) -> PyResult<Vec<Key>> {
    let group = group.filter(|g| *g != 0);
    if unit <= 0 || !tile.is_finite() || tile <= 0. || !x.is_finite() || !y.is_finite() || !r.is_finite() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "tracker_near requires finite coordinates and a positive tile size",
        ));
    }
    let size = tile * group.unwrap_or(unit) as f64;
    let per = group.unwrap_or(unit) / unit;
    let mut out = Vec::new();
    for i in ((x - r) / size).floor() as i64..=((x + r) / size).floor() as i64 {
        let dx = (i as f64 * size - x).max(x - (i + 1) as f64 * size).max(0.);
        for j in ((y - r) / size).floor() as i64..=((y + r) / size).floor() as i64 {
            let dy = (j as f64 * size - y).max(y - (j + 1) as f64 * size).max(0.);
            if dx * dx + dy * dy <= r * r {
                for a in 0..per {
                    for b in 0..per {
                        out.push((i * per + a, j * per + b));
                    }
                }
            }
        }
    }
    Ok(out)
}

#[pyfunction]
fn tracker_distance(k: Key, x: f64, y: f64, tile: f64, unit: i64, group: Option<i64>) -> PyResult<f64> {
    let group = group.filter(|g| *g != 0);
    if unit <= 0 || group.is_some_and(|g| g < unit) {
        return Err(pyo3::exceptions::PyValueError::new_err("invalid tracker group size"));
    }
    Ok(distance(
        k,
        (x, y),
        tile * group.unwrap_or(unit) as f64,
        group.unwrap_or(unit) / unit,
    ))
}

/// Anchor-local tiles (the legacy road or scenery protocol). The car has priority over the
/// detached view, then other vehicles. Protect the ground queue while terrain is building.
#[pyfunction]
#[pyo3(signature=(tr, conn, aid, pos, view, foci, vel, ground_busy, now, submit, stuck))]
fn anchor_tiles<'py>(
    py: Python<'py>,
    tr: &Obj<'py>,
    conn: &Obj<'py>,
    aid: i64,
    pos: Point,
    view: Option<Point>,
    foci: &Obj<'py>,
    vel: Option<Point>,
    ground_busy: bool,
    now: f64,
    submit: &Obj<'py>,
    stuck: Option<&Obj<'py>>,
) -> PyResult<()> {
    let roads = py.import("roads")?;
    let road = vel.is_some();
    let cfg = |r: &str, v: &str| if road { roads.getattr(r) } else { tr.getattr(v) };
    let tile: f64 = roads.getattr("TILE")?.extract()?;
    let send: f64 = cfg("SEND_M", "send_m")?.extract()?;
    let drop_m: f64 = cfg("DROP_M", "drop_m")?.extract()?;
    let limit: usize = cfg("BUILD_PER_POSE", "per_pose")?.extract()?;
    let (unit, group) = if road {
        (1, None)
    } else {
        (tr.getattr("unit")?.extract()?, tr.getattr("group")?.extract()?)
    };
    let foci = focuses(foci)?;
    let spots: Vec<_> = std::iter::once(pos).chain(view).collect();
    let near = |p: Point, r| tracker_near(p.0, p.1, r, tile, unit, group);
    let group = group.filter(|g| *g != 0);
    if unit <= 0 || group.is_some_and(|g| g < unit) {
        return Err(pyo3::exceptions::PyValueError::new_err("invalid tracker group size"));
    }
    let dist = |k, p: Point| distance(k, p, tile * group.unwrap_or(unit) as f64, group.unwrap_or(unit) / unit);
    let skip = PySet::empty(py)?;
    if !road {
        let inner: Option<(f64, i64)> = tr.getattr("inner")?.extract()?;
        let skip_m: Option<f64> = tr.getattr("skip_m")?.extract()?;
        for &p in &spots {
            if let Some((r, g)) = inner {
                skip.call_method1("update", (tracker_near(p.0, p.1, r, tile, unit, Some(g))?,))?;
            } else if let Some(r) = skip_m.filter(|r| *r != 0.) {
                skip.call_method1("update", (tracker_near(p.0, p.1, r, tile, 1, None)?,))?;
            }
        }
    }
    let car = match vel {
        Some(v) => roads::anchor_wanted(py, pos, v)?,
        None => PySet::new(py, near(pos, send)?)?,
    };
    let car_progress = if road {
        car.clone().into_any()
    } else {
        car.call_method1("difference", (&skip,))?
    };
    tr.setattr("car", car_progress)?;
    let seen = if let Some(p) = view {
        PySet::new(py, near(p, send)?)?
            .call_method1("difference", (&car,))?
            .cast_into::<PySet>()?
    } else {
        PySet::empty(py)?
    };
    let others = PySet::empty(py)?;
    for f in &foci {
        others.call_method1("update", (near(f.at, send.min(f.radius + tile / 2.))?,))?;
    }
    let wanted = car.call_method1("union", (&seen,))?;
    others.call_method1("difference_update", (&wanted,))?;
    let wanted = wanted.call_method1("union", (&others,))?.cast_into::<PySet>()?;
    let start = if ground_busy {
        PySet::new(py, near(pos, send)?)?
    } else {
        wanted.clone()
    };
    let ff = py.import("farfield")?;
    let cone: Vec<f64> = if unit == 1 {
        roads.call_method1("_tracker_cone", (conn, aid))?.extract()?
    } else {
        vec![]
    };
    let view_penalty: f64 = ff.getattr("VIEW_PENALTY")?.extract()?;
    let side: f64 = roads.getattr("SIDE_PENALTY")?.extract()?;
    let lead: f64 = roads.getattr("LEAD_MAX_M")?.extract()?;
    let fast: f64 = roads.getattr("FAST_MS")?.extract()?;
    let path = vel.and_then(|(vx, vy)| {
        let s = crate::py_hypot(vx, vy);
        (s >= fast).then(|| (vx / s, vy / s))
    });
    let view_rank = |k, p| {
        if unit == 1 {
            roads::view_rank(k, p, tile, &cone, view_penalty)
        } else {
            dist(k, p)
        }
    };
    let sent = set(tr, "sent")?;
    let building = set(tr, "building")?;
    let empty = road.then(|| set(tr, "empty")).transpose()?;
    let retry = road.then(|| dict(tr, "retry")).transpose()?;
    let mut jobs = Vec::new();
    for key in start.iter() {
        let k: Key = key.extract()?;
        if sent.contains(&key)? || building.contains(&key)? || skip.contains(&key)? {
            continue;
        }
        if let (Some(empty), Some(retry)) = (&empty, &retry) {
            if empty.contains(&key)?
                || retry
                    .get_item(&key)?
                    .map(|t| t.extract::<f64>())
                    .transpose()?
                    .unwrap_or(0.)
                    > now
            {
                continue;
            }
        }
        let rank = if others.contains(&key)? {
            (2, foci.iter().map(|f| dist(k, f.at)).fold(f64::INFINITY, f64::min))
        } else if seen.contains(&key)? {
            (1, view_rank(k, view.unwrap()))
        } else {
            (
                0,
                match path {
                    Some(u) => roads::road_path_rank(k, pos, u, tile, side, lead),
                    None => view_rank(k, pos),
                },
            )
        };
        jobs.push((rank, k));
    }
    jobs.sort_by(|(a, _), (b, _)| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    for (_, key) in jobs.into_iter().take(limit.saturating_sub(building.len())) {
        submit.call1((key,))?;
    }
    if let Some(stuck) = stuck {
        stuck.call0()?;
    }
    let far = |k| spots.iter().all(|&p| dist(k, p) > drop_m) && foci.iter().all(|f| dist(k, f.at) > f.radius + tile);
    let mut drop = Vec::new();
    for key in sent.iter() {
        let k = key.extract()?;
        if (!road || !wanted.contains(&key)?) && far(k) {
            drop.push(k);
        }
    }
    let kind = if road {
        "roads".to_owned()
    } else {
        tr.getattr("kind")?.extract()?
    };
    for (x, y) in drop {
        sent.discard((x, y))?;
        let m = PyDict::new(py);
        m.set_item("type", format!("{kind}_drop"))?;
        m.set_item("anchorId", aid)?;
        m.set_item("key", format!("{x}_{y}"))?;
        conn.call_method1("send", (m,))?;
    }
    if let Some(empty) = empty.filter(|s| !s.is_empty()) {
        let keep = PySet::empty(py)?;
        for key in empty.iter() {
            if wanted.contains(&key)? || !far(key.extract()?) {
                keep.add(key)?;
            }
        }
        tr.setattr("empty", keep)?;
    }
    if let Some(retry) = retry {
        expire(tr, &retry, now)?;
    }
    Ok(())
}

fn expire(tr: &Obj<'_>, retry: &Bound<'_, PyDict>, now: f64) -> PyResult<()> {
    if !retry.is_empty() {
        let keep = PyDict::new(tr.py());
        for (k, t) in retry.iter() {
            if t.extract::<f64>()? > now {
                keep.set_item(k, t)?;
            }
        }
        tr.setattr("retry", keep)?;
    }
    Ok(())
}

/// Keep the shared snapped-point memo in geotiles.tiles_near. Its projection uses the
/// caller's actual frame (including the frame origin), which matters at grid boundaries.
fn geo_near(tiles: &Obj<'_>, frame: &Obj<'_>, p: Point, r: f64, z: u32) -> PyResult<Vec<(i64, i64, f64)>> {
    tiles.call1((frame, p.0, p.1, r, z))?.extract()
}

fn geo_keys(rows: Vec<(i64, i64, f64)>, group: Option<u32>, z: u32, f: &TileFrame, p: Point) -> Vec<Key> {
    let Some(g) = group else {
        return rows.into_iter().map(|(x, y, _)| (x, y)).collect();
    };
    let n = 1_i64 << (z - g);
    let mut keys = Vec::new();
    for (gx, gy, _) in rows {
        for j in 0..n {
            for i in 0..n {
                let k = (gx * n + i, gy * n + j);
                keys.push((quad_dist(&f.quad(z, k), p.0, p.1), k));
            }
        }
    }
    keys.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    keys.into_iter().map(|(_, k)| k).collect()
}

/// The gate is recorded *after* submission and drops: simulated executors (and already
/// completed futures) can run a completion inside the pass itself.
#[pyfunction]
#[pyo3(signature=(tr, aid, fr, pos, view, foci, tree, z, now, max_s, move_m, start, drop_tile))]
fn scenery_geo(
    py: Python<'_>,
    tr: &Obj<'_>,
    aid: i64,
    fr: &Obj<'_>,
    pos: Point,
    view: Option<Point>,
    foci: &Obj<'_>,
    tree: &Obj<'_>,
    z: u32,
    now: f64,
    max_s: f64,
    move_m: f64,
    start: &Obj<'_>,
    drop_tile: &Obj<'_>,
) -> PyResult<()> {
    let sent = set(tr, "sent")?;
    let building = set(tr, "building")?;
    let redo = set(tr, "redo")?;
    let limit: usize = tr.getattr("per_pose")?.extract()?;
    let send: f64 = tr.getattr("send_m")?.extract()?;
    let drop_m: f64 = tr.getattr("drop_m")?.extract()?;
    let inner = tr.getattr("inner")?;
    let group: Option<u32> = tr.getattr("group_z")?.extract()?;
    if z > 31 || group.is_some_and(|g| g > z) {
        return Err(pyo3::exceptions::PyValueError::new_err("invalid tracker zoom levels"));
    }
    let foci = focuses(foci)?;
    let spots: Vec<_> = std::iter::once(pos).chain(view).collect();
    let cont: Vec<_> = spots
        .iter()
        .flat_map(|p| [p.0, p.1])
        .chain(foci.iter().flat_map(|f| [f.at.0, f.at.1, f.radius]))
        .collect();
    let gate_key = || -> PyResult<_> {
        Ok((
            (
                aid,
                fr.as_ptr() as usize,
                view.is_none(),
                foci.len(),
                tree.as_ptr() as usize,
            ),
            (send, drop_m, &inner, group, z),
            (sent.len(), building.len(), redo.len(), tr.getattr("epoch")?, limit),
        )
            .into_pyobject(py)?)
    };
    let gate = tr.getattr("_gate")?;
    if !gate.is_none()
        && gate.get_item(0)?.eq(gate_key()?)?
        && now - gate.get_item(2)?.extract::<f64>()? < max_s
        && !(gate.get_item(3)?.is_truthy()? && building.len() < limit)
    {
        let last: Vec<f64> = gate.get_item(1)?.extract()?;
        if last.len() == cont.len() && last.iter().zip(&cont).all(|(a, b)| (a - b).abs() <= move_m) {
            return Ok(());
        }
    }
    let tiles = py.import("geotiles")?.getattr("tiles_near")?;
    let f = TileFrame::new(&tr.getattr("frame")?)?;
    let near = |p, r| {
        Ok::<_, PyErr>(geo_keys(
            geo_near(&tiles, fr, p, r, group.unwrap_or(z))?,
            group,
            z,
            &f,
            p,
        ))
    };
    let mut skip = HashSet::new();
    if !inner.is_none() {
        let r: f64 = inner.get_item(0)?.extract()?;
        for &p in &spots {
            skip.extend(geo_near(&tiles, fr, p, r, z)?.into_iter().map(|(x, y, _)| (x, y)));
        }
    }
    let mut car = near(pos, send)?;
    car.retain(|k| !skip.contains(k));
    if !tree.is_none() && !car.is_empty() {
        match py
            .import("reach")?
            .call_method1("order_geotiles", (tree, fr, &car, pos.0, pos.1, z))
        {
            Ok(ordered) => car = ordered.extract()?,
            Err(err) => {
                py.import("vegetation")?
                    .getattr("log")?
                    .call_method1("debug", ("geotiles in tree order: %s", err.to_string()))?;
            }
        }
    }
    tr.setattr("car", PySet::new(py, &car)?)?;
    if let Some(p) = view {
        car.extend(near(p, send)?);
    }
    for focus in &foci {
        car.extend(near(focus.at, send.min(focus.radius + 128.))?);
    }
    redo.call_method1("intersection_update", (&sent,))?;
    let mut done = skip;
    let room = limit.saturating_sub(building.len());
    let mut jobs = Vec::new();
    for k in car {
        if done.insert(k) && (!sent.contains(k)? || redo.contains(k)?) && !building.contains(k)? {
            jobs.push(k);
            if jobs.len() > room {
                break;
            }
        }
    }
    for k in jobs.iter().take(room) {
        start.call1((k,))?;
    }
    let gz = group.unwrap_or(z);
    let mut drops = Vec::new();
    for key in sent.iter() {
        let k: Key = key.extract()?;
        let q = f.quad(gz, (k.0 >> (z - gz), k.1 >> (z - gz)));
        if spots.iter().all(|p| quad_dist(&q, p.0, p.1) > drop_m)
            && foci.iter().all(|f| quad_dist(&q, f.at.0, f.at.1) > f.radius + 256.)
        {
            drops.push(k);
        }
    }
    for k in drops {
        drop_tile.call1((k,))?;
    }
    tr.setattr("_gate", (gate_key()?, cont, now, jobs.len() > room))
}

/// The loading line uses the same tile sets as the scheduler. Catch-up counts only the
/// road geotiles within its near radius. Waiting for a retry satisfies a road tile.
#[pyfunction]
#[pyo3(signature=(car,sent,empty,retry,skins,dist,radius))]
fn tracker_progress(
    car: &Obj<'_>,
    sent: &Bound<'_, PySet>,
    empty: Option<&Bound<'_, PySet>>,
    retry: Option<&Bound<'_, PyDict>>,
    skins: Option<&Bound<'_, PySet>>,
    dist: Option<&Bound<'_, PyDict>>,
    radius: Option<f64>,
) -> PyResult<(usize, usize)> {
    let (mut done, mut wanted) = (0, 0);
    for k in car.try_iter()? {
        let k = k?;
        if let (Some(dist), Some(r)) = (dist, radius) {
            if dist
                .get_item(&k)?
                .map(|d| d.extract::<f64>())
                .transpose()?
                .unwrap_or(0.)
                > r
            {
                continue;
            }
        }
        wanted += 1;
        if (sent.contains(&k)? && skins.map(|s| s.contains(&k)).transpose()?.unwrap_or(true))
            || empty.map(|s| s.contains(&k)).transpose()?.unwrap_or(false)
            || retry.map(|s| s.contains(&k)).transpose()?.unwrap_or(false)
        {
            done += 1
        }
    }
    Ok((done, wanted))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(pose::tracker_pose, m)?)?;
    m.add_function(wrap_pyfunction!(pose::traffic_pose, m)?)?;
    m.add_function(wrap_pyfunction!(tracker_progress, m)?)?;
    m.add_function(wrap_pyfunction!(scenery::scenery_result, m)?)?;
    m.add_function(wrap_pyfunction!(scenery::road_eta_rank, m)?)?;
    m.add_function(wrap_pyfunction!(scenery::detail_order, m)?)?;
    m.add_function(wrap_pyfunction!(roads::road_exact, m)?)?;
    m.add_function(wrap_pyfunction!(roads::road_unwanted, m)?)?;
    m.add_function(wrap_pyfunction!(roads::road_view_rank, m)?)?;
    m.add_function(wrap_pyfunction!(roads::road_path_rank, m)?)?;
    m.add_function(wrap_pyfunction!(roads::anchor_wanted, m)?)?;
    m.add_function(wrap_pyfunction!(roads::road_geo_wanted, m)?)?;
    m.add_function(wrap_pyfunction!(roads::road_geo, m)?)?;
    m.add_function(wrap_pyfunction!(roads::road_ready, m)?)?;
    m.add_function(wrap_pyfunction!(signs::sign_tiles, m)?)?;
    m.add_function(wrap_pyfunction!(signs::sign_result, m)?)?;
    m.add_function(wrap_pyfunction!(tracker_near, m)?)?;
    m.add_function(wrap_pyfunction!(tracker_distance, m)?)?;
    m.add_function(wrap_pyfunction!(anchor_tiles, m)?)?;
    m.add_function(wrap_pyfunction!(scenery_geo, m)?)?;
    Ok(())
}
