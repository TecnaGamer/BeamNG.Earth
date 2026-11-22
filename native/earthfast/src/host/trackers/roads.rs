//! Road geotiles keep their content across rebases. Skin and exact-part decisions
//! are per anchor. A stale-generation redo can also schedule a tile outside the car set.
use super::*;
use pyo3::types::PyTuple;

struct Horizon {
    tile: f64,
    send: f64,
    fast: f64,
    lead: f64,
    corridor: f64,
}
impl Horizon {
    fn new(roads: &Bound<'_, PyModule>, speed: f64) -> PyResult<Self> {
        let max: f64 = roads.getattr("LEAD_MAX_M")?.extract()?;
        let seconds: f64 = roads.getattr("LEAD_S")?.extract()?;
        Ok(Self {
            tile: roads.getattr("TILE")?.extract()?,
            send: roads.getattr("SEND_M")?.extract()?,
            fast: roads.getattr("FAST_MS")?.extract()?,
            lead: max.min(speed * seconds),
            corridor: roads.getattr("CORRIDOR_M")?.extract()?,
        })
    }
    fn samples(&self, p: Point, v: Point, s: f64) -> Vec<(Point, f64)> {
        let mut samples = Vec::new();
        if s >= self.fast && self.lead > self.send {
            let u = (v.0 / s, v.1 / s);
            let mut d = 0.;
            while d <= self.lead {
                samples.push(((p.0 + u.0 * d, p.1 + u.1 * d), d));
                d += self.tile / 2.;
            }
        }
        samples
    }
}

#[pyfunction]
pub(super) fn anchor_wanted(py: Python<'_>, p: Point, v: Point) -> PyResult<Bound<'_, PySet>> {
    let s = crate::py_hypot(v.0, v.1);
    let h = Horizon::new(&py.import("roads")?, s)?;
    let want = PySet::new(py, tracker_near(p.0, p.1, h.send, h.tile, 1, None)?)?;
    for ((x, y), _) in h.samples(p, v, s) {
        for k in tracker_near(x, y, h.corridor, h.tile, 1, None)? {
            want.add(k)?;
        }
    }
    Ok(want)
}

fn tree_keys<'py>(tree: &Obj<'py>, fr: &Obj<'py>, radius: f64, horizon: f64) -> PyResult<Bound<'py, PyDict>> {
    let kw = PyDict::new(fr.py());
    kw.set_item("horizon", horizon)?;
    Ok(tree.call_method("geotile_keys", (fr, radius), Some(&kw))?.cast_into()?)
}

struct Geo<'py> {
    near: Obj<'py>,
    fr: Obj<'py>,
    z: u32,
}

impl<'py> Geo<'py> {
    fn new(fr: &Obj<'py>) -> PyResult<Self> {
        let geo = fr.py().import("geotiles")?;
        Ok(Self {
            near: geo.getattr("tiles_near")?,
            fr: fr.clone(),
            z: geo.getattr("Z")?.extract()?,
        })
    }
    fn within(&self, p: Point, r: f64) -> PyResult<Vec<(i64, i64, f64)>> {
        geo_near(&self.near, &self.fr, p, r, self.z)
    }
    fn wanted(&self, h: &Horizon, p: Point, v: Point, s: f64, tree: &Obj<'py>) -> PyResult<Bound<'py, PyDict>> {
        let want = PyDict::new(self.fr.py());
        for (a, b, d) in self.within(p, h.send)? {
            want.set_item((a, b), d)?;
        }
        if s >= h.fast && h.lead > h.send {
            if tree.is_none() {
                for (at, d) in h.samples(p, v, s) {
                    for (a, b, _) in self.within(at, h.corridor)? {
                        if !want.contains((a, b))? {
                            want.set_item((a, b), d)?;
                        }
                    }
                }
            } else {
                for (k, got) in tree_keys(tree, &self.fr, h.corridor, h.lead / s)?.iter() {
                    if got.get_item(1)?.is_truthy()? && !want.contains(&k)? {
                        want.set_item(k, got.get_item(2)?.extract::<f64>()? * s)?;
                    }
                }
            }
        }
        Ok(want)
    }
}

#[pyfunction]
pub(super) fn road_geo_wanted<'py>(
    py: Python<'py>,
    fr: &Obj<'py>,
    p: Point,
    v: Point,
    tree: &Obj<'py>,
) -> PyResult<Bound<'py, PyDict>> {
    let s = crate::py_hypot(v.0, v.1);
    Geo::new(fr)?.wanted(&Horizon::new(&py.import("roads")?, s)?, p, v, s, tree)
}

#[pyfunction]
pub(super) fn road_ready<'py>(
    py: Python<'py>,
    tr: &Obj<'py>,
    fr: &Obj<'py>,
    p: Point,
    r: f64,
    tile: f64,
) -> PyResult<Bound<'py, PySet>> {
    let geo = Geo::new(fr)?;
    let sent = set(tr, "sent")?;
    let skins = set(tr, "skins")?;
    let retry = dict(tr, "retry")?;
    let out = PySet::empty(py)?;
    let reach = tile / 2. * 2_f64.sqrt();
    for k in tracker_near(p.0, p.1, r, tile, 1, None)? {
        let rows = geo.near.call1((
            fr,
            (k.0 as f64 + 0.5) * tile,
            (k.1 as f64 + 0.5) * tile,
            reach,
            geo.z,
            true,
        ))?;
        let mut ready = true;
        for row in rows.try_iter()? {
            let (a, b, _): (i64, i64, f64) = row?.extract()?;
            if !(sent.contains((a, b))? && skins.contains((a, b))? || retry.contains((a, b))?) {
                ready = false;
                break;
            }
        }
        if ready {
            out.add(k)?;
        }
    }
    Ok(out)
}

fn pyset<'py>(obj: &Obj<'py>) -> PyResult<Bound<'py, PySet>> {
    Ok(obj
        .py()
        .import("builtins")?
        .getattr("set")?
        .call1((obj,))?
        .cast_into()?)
}

const BUSY_ROUTE_S: f64 = 18.;
const ROADSIDE_M: f64 = 25.;

struct RoadOrder<'a, 'py> {
    h: &'a Horizon,
    f: TileFrame,
    z: u32,
    p: Point,
    v: Point,
    s: f64,
    build_s: f64,
    busy_route: bool,
    max_lead: f64,
    side_penalty: f64,
    insure: f64,
    late: f64,
    car: Bound<'py, PyDict>,
    seen: Bound<'py, PyDict>,
    others: Bound<'py, PyDict>,
    deck: Obj<'py>,
    tree: Option<Bound<'py, PyDict>>,
    eta_params: (f64, f64, f64, f64, f64, f64),
}
impl RoadOrder<'_, '_> {
    fn rank(&self, key: &Obj<'_>) -> PyResult<(i32, f64)> {
        if self.deck.contains(key)? {
            return Ok((-2, 0.));
        }
        let Some(d) = self.car.get_item(key)? else {
            return if let Some(d) = self.seen.get_item(key)? {
                Ok((1, d.extract()?))
            } else {
                Ok((
                    2,
                    self.others
                        .get_item(key)?
                        .map(|d| d.extract())
                        .transpose()?
                        .unwrap_or(1e9),
                ))
            };
        };
        let d: f64 = d.extract()?;
        if self.tree.is_none() && self.s < self.h.fast {
            return Ok((0, d));
        }
        let k: Key = key.extract()?;
        let q = self.f.quad(self.z, k);
        if self.busy_route && quad_dist(&q, self.p.0, self.p.1) <= ROADSIDE_M {
            return Ok((-1, 0.));
        }
        let score = |eta: f64, p: f64, gap: f64| -> PyResult<f64> {
            Ok(scenery::road_eta_rank(eta, p, gap, self.s, self.build_s, self.eta_params) * self.s)
        };
        if let Some(tree) = &self.tree {
            if self.s < self.h.fast {
                let d = quad_dist(&q, self.p.0, self.p.1);
                if d <= self.insure {
                    return Ok((0, d));
                }
            }
            if let Some(got) = tree.get_item(key)? {
                let eta = got.get_item(2)?.extract()?;
                let chance = got.get_item(3)?.extract()?;
                let gap = got.get_item(4)?.extract()?;
                let imminent = self.busy_route && got.get_item(1)?.is_truthy()? && eta <= BUSY_ROUTE_S && gap <= ROADSIDE_M;
                return Ok((if imminent { -1 } else { 0 }, score(eta, chance, gap)?));
            }
        }
        let dx = (q[0][0] + q[1][0] + q[2][0] + q[3][0]) / 4. - self.p.0;
        let dy = (q[0][1] + q[1][1] + q[2][1] + q[3][1]) / 4. - self.p.1;
        let along = (dx * self.v.0 + dy * self.v.1) / self.s;
        let side = ((dx * self.v.1 - dy * self.v.0) / self.s).abs();
        let rank = if along < -self.h.tile / 2. {
            let penalty = d * self.side_penalty + self.max_lead;
            if self.tree.is_some() {
                penalty + self.late * 2. * self.s
            } else {
                penalty
            }
        } else if self.tree.is_some() {
            score(along.max(0.) / self.s, 1., (side - self.h.tile / 2.).max(0.))?
        } else {
            along.max(0.) + (side - self.h.corridor).max(0.) * self.side_penalty
        };
        // With four outstanding builds, a cheap nearby tile off the road can
        // otherwise take the last worker ahead of the tile the car is entering.
        let mut tier = 0;
        if self.busy_route && self.tree.is_none() && along >= -self.h.tile / 2. && along <= self.s * BUSY_ROUTE_S {
            let mut left = f64::INFINITY;
            let mut right = f64::NEG_INFINITY;
            for corner in &q {
                let cross = ((corner[0] - self.p.0) * self.v.1 - (corner[1] - self.p.1) * self.v.0) / self.s;
                left = left.min(cross);
                right = right.max(cross);
            }
            if left <= ROADSIDE_M && right >= -ROADSIDE_M {
                tier = -1;
            }
        }
        Ok((tier, rank))
    }
}

#[pyfunction]
#[pyo3(signature=(tr,conn,aid,fr,p,v,view,foci,tree,ground_busy,now,start,stuck,drop_tile))]
pub(super) fn road_geo(
    py: Python<'_>,
    tr: &Obj<'_>,
    conn: &Obj<'_>,
    aid: i64,
    fr: &Obj<'_>,
    p: Point,
    v: Point,
    view: Option<Point>,
    foci: &Obj<'_>,
    tree: &Obj<'_>,
    ground_busy: bool,
    now: f64,
    start: &Obj<'_>,
    stuck: &Obj<'_>,
    drop_tile: &Obj<'_>,
) -> PyResult<()> {
    let s = crate::py_hypot(v.0, v.1);
    let roads = py.import("roads")?;
    let reach = py.import("reach")?;
    let geo = Geo::new(fr)?;
    let h = Horizon::new(&roads, s)?;
    let none = py.None().into_bound(py);
    let tree = if s < reach.getattr("MIN_V")?.extract()? {
        &none
    } else {
        tree
    };
    let car = geo.wanted(&h, p, v, s, tree)?;
    let tkeys = if tree.is_none() {
        None
    } else {
        Some(tree_keys(
            tree,
            fr,
            h.corridor,
            h.lead.max(reach.getattr("HORIZON_MIN_M")?.extract()?) / s,
        )?)
    };
    let car_set = pyset(&car)?;
    tr.setattr("car", &car_set)?;
    tr.setattr("car_m", &car)?;
    tr.setattr("car_at", p)?;
    tr.setattr("tree_on", !tree.is_none())?;
    let seen = PyDict::new(py);
    if let Some(at) = view {
        for (x, y, d) in geo.within(at, h.send)? {
            seen.set_item((x, y), d)?;
        }
    }
    let others = PyDict::new(py);
    let foci = focuses(foci)?;
    for f in &foci {
        for (x, y, d) in geo.within(f.at, h.send.min(f.radius + h.tile / 2.))? {
            if !others.contains((x, y))? {
                others.set_item((x, y), d)?;
            }
        }
    }
    let wanted = car_set
        .call_method1("union", (pyset(&seen)?,))?
        .call_method1("union", (pyset(&others)?,))?
        .cast_into::<PySet>()?;
    let wa = tr
        .getattr("__dict__")?
        .call_method1("setdefault", ("wanted_at", PyDict::new(py)))?
        .cast_into::<PyDict>()?;
    for k in wanted.iter() {
        wa.set_item(k, now)?;
    }
    if wa.len() > 4 * wanted.len() + 256 {
        let keep = PyDict::new(py);
        for (k, t) in wa.iter() {
            if now - t.extract::<f64>()? <= 60. {
                keep.set_item(k, t)?;
            }
        }
        tr.setattr("wanted_at", keep)?;
    }
    let eligible = if ground_busy {
        let close = PySet::new(py, geo.within(p, h.send)?.into_iter().map(|(a, b, _)| (a, b)))?;
        // Terrain work can last several block crossings. Keep the imminent part of the
        // car's predicted route eligible without admitting distant view or spectator tiles.
        let lead = h.lead.min(s * BUSY_ROUTE_S);
        if s >= h.fast && lead > h.send {
            for (key, distance) in car.iter() {
                if distance.extract::<f64>()? <= lead {
                    close.add(key)?;
                }
            }
        }
        close
    } else {
        wanted.clone()
    };
    // The bridges the mod's deck guard waits on (at most deckspans.WAIT_KEYS, ~900 m ahead): eligible even when
    // terrain work narrows the rest or the predicted corridor misses them, so a deck is never left out of the build.
    let deck = tr
        .getattr("deck_first")
        .ok()
        .filter(|d| !d.is_none())
        .unwrap_or(PyTuple::empty(py).into_any());
    let eligible = if deck.is_truthy()? {
        eligible.call_method1("union", (&deck,))?.cast_into::<PySet>()?
    } else {
        eligible
    };
    let sent = set(tr, "sent")?;
    let building = set(tr, "building")?;
    let skins = set(tr, "skins")?;
    let redo = set(tr, "redo")?;
    let retry = dict(tr, "retry")?;
    let need = dict(tr, "need")?;
    redo.call_method1("intersection_update", (&sent,))?;
    let eligible = if redo.is_empty() {
        eligible
    } else {
        eligible
            .call_method0("copy")?
            .call_method1("union", (&redo,))?
            .cast_into()?
    };
    let build_s = match tr.getattr("build_s").ok().filter(|v| v.is_truthy().unwrap_or(false)) {
        Some(v) => v.extract()?,
        None => roads.call_method0("reach_build_est")?.extract()?,
    };
    let order = RoadOrder {
        h: &h,
        f: TileFrame::new(fr)?,
        z: geo.z,
        p,
        v,
        s,
        build_s,
        busy_route: ground_busy && s >= h.fast,
        max_lead: roads.getattr("LEAD_MAX_M")?.extract()?,
        side_penalty: roads.getattr("SIDE_PENALTY")?.extract()?,
        insure: roads.getattr("INSURE_M")?.extract()?,
        late: reach.getattr("LATE_S")?.extract()?,
        deck,
        car,
        seen,
        others,
        tree: tkeys,
        eta_params: (
            reach.getattr("P_MIN")?.extract()?,
            reach.getattr("GAMMA")?.extract()?,
            reach.getattr("V_SIDE")?.extract()?,
            reach.getattr("WINDOW_M")?.extract()?,
            reach.getattr("LATE_KEEP")?.extract()?,
            reach.getattr("LATE_S")?.extract()?,
        ),
    };
    let mut jobs = Vec::new();
    for k in eligible.iter() {
        if building.contains(&k)?
            || retry
                .get_item(&k)?
                .map(|v| v.extract::<f64>())
                .transpose()?
                .unwrap_or(0.)
                > now
        {
            continue;
        }
        let content = !sent.contains(&k)? || redo.contains(&k)?;
        let skin = content || !skins.contains(&k)?;
        let make = if content {
            pyo3::types::PyString::new(py, "auto").into_any()
        } else {
            if skin || !need.contains((&k, aid))? {
                let res = tr.call_method1("exact_need", (&k,))?;
                need.set_item((&k, aid), res.get_item(0)?)?;
                let base = res.get_item(1)?;
                if base.is_truthy()? {
                    let parts = PyDict::new(py);
                    let shown = dict(tr, "shown")?.get_item(&k)?;
                    for w in base.try_iter()? {
                        let w = w?;
                        if let Some(shown) = &shown {
                            shown.call_method1("pop", (&w, &none))?;
                        }
                        parts.set_item(w, false)?;
                    }
                    let stats = dict(tr, "stats")?;
                    stats.set_item(
                        "exact_base",
                        stats
                            .get_item("exact_base")?
                            .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err("exact_base"))?
                            .extract::<usize>()?
                            + parts.len(),
                    )?;
                    let m = PyDict::new(py);
                    m.set_item("type", "roads_exact")?;
                    m.set_item("anchorId", aid)?;
                    let (a, b): Key = k.extract()?;
                    m.set_item("key", format!("g{}_{a}_{b}", geo.z))?;
                    m.set_item("parts", parts)?;
                    conn.call_method1("send", (m,))?;
                }
            }
            let make = need
                .get_item((&k, aid))?
                .filter(|m| m.is_truthy().unwrap_or(false))
                .unwrap_or(none.clone());
            if !skin && make.is_none() {
                continue;
            }
            make
        };
        let rank = order.rank(&k)?;
        let tie = if order.busy_route { k.extract::<Key>()? } else { (0, 0) };
        jobs.push(((rank, tie), k, content, skin, make));
    }
    jobs.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    let limit: usize = roads.getattr("BUILD_PER_POSE")?.extract()?;
    for (_, k, content, skin, make) in jobs.into_iter().take(limit.saturating_sub(building.len())) {
        start.call1((k, content, skin, make))?;
    }
    stuck.call0()?;
    let drop_m: f64 = roads.getattr("DROP_M")?.extract()?;
    let mut drops = Vec::new();
    for k in sent.iter() {
        if wanted.contains(&k)? || building.contains(&k)? {
            continue;
        }
        let (x, y): Key = k.extract()?;
        let q = order.f.quad(geo.z, (x, y));
        if quad_dist(&q, p.0, p.1) > drop_m
            && view.is_none_or(|v| quad_dist(&q, v.0, v.1) > drop_m)
            && foci.iter().all(|f| quad_dist(&q, f.at.0, f.at.1) > f.radius + h.tile)
        {
            drops.push(k);
        }
    }
    for k in drops {
        drop_tile.call1((k,))?;
    }
    expire(tr, &retry, now)
}

#[pyfunction]
pub(super) fn road_path_rank(k: Key, p: Point, unit: Point, tile: f64, penalty: f64, lead: f64) -> f64 {
    let d = distance(k, p, tile, 1);
    if d <= 0. {
        return 0.;
    }
    let x = k.0 as f64 * tile;
    let y = k.1 as f64 * tile;
    let dx = (x + (k.0 + 1) as f64 * tile) / 2. - p.0;
    let dy = (y + (k.1 + 1) as f64 * tile) / 2. - p.1;
    let along = dx * unit.0 + dy * unit.1;
    if along < -tile / 2. {
        d * penalty + lead
    } else {
        along.max(0.) + penalty * ((dy * unit.0 - dx * unit.1).abs() - tile / 2.).max(0.)
    }
}

#[pyfunction]
pub(super) fn road_view_rank(k: Key, p: Point, tile: f64, cone: Vec<f64>, penalty: f64) -> f64 {
    view_rank(k, p, tile, &cone, penalty)
}
pub(super) fn view_rank(k: Key, p: Point, tile: f64, cone: &[f64], penalty: f64) -> f64 {
    let d = distance(k, p, tile, 1);
    if cone.is_empty() || d <= 0. {
        return d;
    }
    let bb = [
        k.0 as f64 * tile,
        k.1 as f64 * tile,
        (k.0 + 1) as f64 * tile,
        (k.1 + 1) as f64 * tile,
    ];
    crate::view_distance(bb, p.0, p.1, cone, penalty)
}

#[pyfunction]
pub(super) fn road_exact<'py>(
    py: Python<'py>,
    tr: &Obj<'py>,
    k: Key,
    aid: &Obj<'py>,
    fr: &Obj<'py>,
    z: u32,
    tol_col: f64,
    tol_render: f64,
) -> PyResult<(Bound<'py, PyDict>, Vec<String>)> {
    use crate::dm::Dm;
    use crate::geotiles::{centre, placement_of};
    let make = PyDict::new(py);
    let mut base = Vec::new();
    let Some(info) = dict(tr, "info")?.get_item(k)? else {
        return Ok((make, base));
    };
    if fr.is_none() {
        return Ok((make, base));
    }
    let Some(parts) = info.cast::<PyDict>()?.get_item("parts")?.filter(|p| !p.is_none()) else {
        return Ok((make, base));
    };
    let (lat, lon) = centre(z, k.0, k.1);
    let tile = Frame::new(lat, lon);
    let target = frame(fr)?;
    let shown = dict(tr, "shown")?.get_item(k)?;
    let frames = dict(tr, "frames")?;
    for (what, part) in parts.cast::<PyDict>()?.iter() {
        let part = part.cast::<PyDict>()?;
        let src = match &shown {
            Some(s) => s.cast::<PyDict>()?.get_item(&what)?,
            None => None,
        };
        if src
            .as_ref()
            .map(|src| src.eq(aid))
            .transpose()?
            .unwrap_or(aid.is_none())
        {
            continue;
        }
        let tol = if part.get_item("col")?.is_some_and(|v| v.is_truthy().unwrap_or(false)) {
            tol_col
        } else {
            tol_render
        };
        let pivot = part.call_method1("get", ("pivot", 0.))?;
        let piv = [0., 0., pivot.extract()?];
        let relief: f64 = part.call_method1("get", ("relief", 0.))?.extract()?;
        let (rb, _, cb) = placement_of(&tile, &target, piv);
        let base_err = relief * cb[0].dhypot(cb[1]);
        let source = match src {
            Some(src) if !src.is_none() => frames.get_item(src)?,
            _ => None,
        };
        let err = if let Some(source) = source {
            let (ra, _, ca) = placement_of(&tile, &frame(&source)?, piv);
            let mut off = [0.; 2];
            for i in 0..2 {
                for j in 0..3 {
                    let r = rb[i][0] * ra[j][0] + rb[i][1] * ra[j][1] + rb[i][2] * ra[j][2];
                    off[i] += r * ca[j];
                }
                off[i] -= cb[i];
            }
            relief * off[0].dhypot(off[1])
        } else {
            base_err
        };
        if err <= tol {
            continue;
        }
        if base_err <= tol {
            base.push(what.extract()?)
        } else {
            let m = PyDict::new(py);
            m.set_item("pivot", pivot)?;
            make.set_item(what, m)?;
        }
    }
    Ok((make, base))
}

#[pyfunction]
pub(super) fn road_unwanted(tr: &Obj<'_>, k: Key, now: f64, grace: f64, r: f64, z: u32) -> PyResult<bool> {
    let fr = tr.getattr("frame")?;
    if fr.is_none() || !tr.getattr("tree_on").is_ok_and(|v| v.is_truthy().unwrap_or(false)) {
        return Ok(false);
    }
    let last = dict(tr, "wanted_at")?
        .get_item(k)?
        .map(|t| t.extract::<f64>())
        .transpose()?
        .unwrap_or(-1e9);
    if now - last <= grace {
        return Ok(false);
    }
    let Some((x, y)) = tr.getattr("car_at").ok().and_then(|p| p.extract::<Point>().ok()) else {
        return Ok(false);
    };
    Ok(quad_dist(&quad(&frame(&fr)?, z, k.0, k.1), x, y) > r)
}
