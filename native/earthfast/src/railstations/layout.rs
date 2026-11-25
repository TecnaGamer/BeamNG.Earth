//! Assign map items to the nearest station hub, snap mapped platforms, and collect roofs and halls.
use super::*;
use crate::busstation::{area, ccw, distance, inside, open};
use pyo3::types::PySet;
type Track<'p> = (String, Obj<'p>);
fn field(d: &Obj, k: &str) -> PyResult<String> {
    d.get_item(k)?.extract()
}
pub(super) fn val(d: &Obj, k: &str) -> PyResult<f64> {
    let v = d.call_method1("get", (k,))?;
    if v.is_truthy()? {
        v.py()
            .get_type::<pyo3::types::PyFloat>()
            .call1((v,))?
            .extract()
    } else {
        Ok(0.)
    }
}
fn centre(d: &Obj) -> PyResult<Point> {
    point(
        &d.py()
            .import("busstations")?
            .call_method1("item_centroid", (d,))?,
    )
}
fn xy<'py>(d: &Obj<'py>) -> PyResult<Obj<'py>> {
    d.py()
        .import("numpy")?
        .call_method1("asarray", (d.get_item("xy")?, "float64"))
}
#[pyfunction]
fn rail_find<'py>(py: Python<'py>, items: Vec<Obj<'py>>) -> PyResult<Vec<Vec<Obj<'py>>>> {
    let mut seeds = vec![];
    let mut halls = vec![];
    for it in items {
        let layer = field(&it, "layer")?;
        let cls = field(&it, "cls")?;
        if layer == "infrastructure"
            && matches!(cls.as_str(), "railway_station" | "railway_halt")
            && field(&it, "kind")? == "point"
        {
            seeds.push(it);
        } else if layer == "buildings" && cls == "train_station" {
            halls.push(it);
        }
    }
    let n = seeds.len();
    for h in halls {
        let mut alone = true;
        for p in &seeds[..n] {
            if (centre(&h)? - centre(p)?).norm() < 450. {
                alone = false;
                break;
            }
        }
        if alone {
            seeds.push(h);
        }
    }
    let mut sorted = vec![];
    for it in seeds {
        sorted.push((field(&it, "id")?, it));
    }
    sorted.sort_by(|a, b| a.0.cmp(&b.0));
    let mut groups: Vec<Vec<(Point, Obj<'py>)>> = vec![];
    for (_, it) in sorted {
        let c = centre(&it)?;
        if let Some(g) = groups
            .iter_mut()
            .find(|g| g.iter().any(|(p, _)| (c - *p).norm() < 450.))
        {
            g.push((c, it))
        } else {
            groups.push(vec![(c, it)])
        }
    }
    let _ = py;
    Ok(groups
        .into_iter()
        .map(|g| g.into_iter().map(|(_, it)| it).collect())
        .collect())
}

#[pyfunction]
fn rail_train_platform(d: &Obj) -> PyResult<bool> {
    if field(d, "layer")? != "infrastructure"
        || field(d, "cls")? != "platform"
        || field(d, "kind")? == "point"
    {
        return Ok(false);
    }
    let tags = d.get_item("tags")?;
    let has = |k: &str, v: &str| tags.call_method1("get", (k,))?.eq(v);
    if has("train", "yes")? {
        return Ok(true);
    }
    for key in ["subway", "light_rail", "tram", "trolleybus", "bus"] {
        if has(key, "yes")? {
            return Ok(false);
        }
    }
    Ok(!has("highway", "bus_stop")? && has("railway", "platform")?)
}
fn bbox(pts: &[Point]) -> [f64; 4] {
    pts.iter().fold(
        [
            f64::INFINITY,
            f64::INFINITY,
            f64::NEG_INFINITY,
            f64::NEG_INFINITY,
        ],
        |[x, y, r, t], p| [x.min(p.0), y.min(p.1), r.max(p.0), t.max(p.1)],
    )
}
pub(super) fn foot(path: &[Point], p: Point) -> (Point, Point, f64) {
    let mut result = (Point::default(), Point::default(), f64::INFINITY);
    for w in path.windows(2) {
        let d = w[1] - w[0];
        let t = ((p - w[0]).dot(d) / d.dot(d).max(1e-9)).clamp(0., 1.);
        let q = w[0] + d * t;
        let dist = (q - p).norm();
        if dist < result.2 {
            result = (q, d / d.norm().max(1e-9), dist);
        }
    }
    result
}
pub(super) fn normals(xy: &[Point]) -> Vec<Point> {
    (0..xy.len())
        .map(|i| {
            let d = xy[(i + 1).min(xy.len() - 1)] - xy[i.saturating_sub(1)];
            (d / d.norm().max(1e-9)).left()
        })
        .collect()
}
fn strip(line: &[Point], tracks: &[&TrackData], width: f64) -> Option<Vec<Point>> {
    if line.len() < 2 {
        return None;
    }
    let mid = line.len() / 2;
    let d = line[(mid + 1).min(line.len() - 1)] - line[mid.saturating_sub(1)];
    if d.norm() < 1e-6 {
        return None;
    }
    let t = d / d.norm();
    let n = t.left();
    let mut gaps = [None, None];
    for (k, side) in [1., -1.].into_iter().enumerate() {
        for tr in tracks {
            let (f, tan, dist) = foot(&tr.pts, line[mid] + n * side * 0.01);
            if (f - line[mid]).dot(n) * side > 0. && tan.dot(t).abs() > 0.8 && dist < 14. {
                gaps[k] = Some(dist.min(gaps[k].unwrap_or(f64::INFINITY)));
            }
        }
    }
    let (left, right) = match gaps {
        [Some(a), Some(b)] => {
            let l = (a * 0.5).min(width);
            let r = (b * 0.5).min(width);
            if l + r < 1.5 {
                (1.5, 1.5)
            } else {
                (l, r)
            }
        }
        [None, Some(_)] => (width, 0.),
        _ => (0., width),
    };
    let normals = normals(line);
    let edge = |i: usize, side: f64| line[i] + normals[i] * side;
    Some(
        (0..line.len())
            .map(|i| edge(i, left))
            .chain((0..line.len()).rev().map(|i| line[i] - normals[i] * right))
            .collect(),
    )
}
struct TrackData<'py> {
    row: Track<'py>,
    pts: Vec<Point>,
    bbox: [f64; 4],
}
fn tracks<'py>(near: &[Obj<'py>]) -> PyResult<Vec<TrackData<'py>>> {
    let mut tracks = vec![];
    for it in near {
        if field(it, "layer")? == "roads"
            && field(it, "sub")? == "rail"
            && matches!(
                field(it, "cls")?.as_str(),
                "standard_gauge" | "narrow_gauge"
            )
            && val(it, "level")? == 0.
            && it.get_item("xy")?.len()? >= 2
        {
            let xy = xy(it)?;
            let pts = points(xy.extract()?)?;
            tracks.push(TrackData {
                row: (field(it, "id")?, xy),
                bbox: bbox(&pts),
                pts,
            });
        }
    }
    tracks.sort_by(|a, b| a.row.0.cmp(&b.row.0));
    Ok(tracks)
}
fn mapped<'py>(
    rs: &Bound<'py, PyModule>,
    it: &Obj<'py>,
    tracks: &[TrackData<'py>],
    hub: Point,
    edge: f64,
) -> PyResult<Option<(Obj<'py>, bool)>> {
    let py = it.py();
    let c = centre(it)?;
    if (c - hub).norm() > 380. {
        return Ok(None);
    }
    let line = points(xy(it)?.extract()?)?;
    let mut local = vec![];
    let mut nearest = f64::INFINITY;
    for t in tracks {
        let d = line
            .iter()
            .map(|p| distance(&t.pts, *p, false))
            .fold(f64::INFINITY, f64::min);
        if distance(&t.pts, c, false) < 90. || d < 60. {
            local.push(t);
            nearest = nearest.min(d);
        }
    }
    if local.is_empty() || nearest > 12. {
        return Ok(None);
    }
    let ring = if field(it, "kind")? == "polygon" {
        ccw(&line)
    } else {
        let width = rs
            .call_method1(
                "_num",
                (it.get_item("tags")?.call_method1("get", ("width",))?,),
            )?
            .extract::<Option<f64>>()?
            .filter(|v| *v != 0.)
            .unwrap_or(4.);
        let Some(r) = strip(&line, &local, width) else {
            return Ok(None);
        };
        ccw(&r)
    };
    if area(&ring) < 8. {
        return Ok(None);
    }
    let b = bbox(&ring);
    let beside: Vec<_> = local
        .iter()
        .filter(|t| meets(b, t.bbox, 7.5))
        .map(|t| t.row.clone())
        .collect();
    let (mut r, mut snapped, mut indices): (Obj, Obj, Obj) = rs
        .call_method1("snap_edges", (array(py, &ring), &beside, edge))?
        .extract()?;
    if r.len()? < 3 || area(&points(r.extract()?)?) < 8. {
        r = array(py, &ring).into_any();
        snapped = vec![false; ring.len()].into_pyarray(py).into_any();
        indices = vec![-1i64; ring.len()].into_pyarray(py).into_any();
    }
    let used: std::collections::BTreeSet<i64> = indices
        .get_item(&snapped)?
        .extract::<Vec<_>>()?
        .into_iter()
        .collect();
    let pl = rs.call_method1("Platform", (it.get_item("id")?, r, it.get_item("tags")?))?;
    pl.setattr("snapped", snapped)?;
    pl.setattr("tidx", indices)?;
    pl.setattr("tracks", beside)?;
    pl.setattr("island", used.len() >= 2)?;
    Ok(Some((pl, !used.is_empty())))
}
#[pyfunction]
fn rail_assemble<'py>(
    py: Python<'py>,
    items: Vec<Obj<'py>>,
    seeds: Vec<Obj<'py>>,
    country: &Obj<'py>,
    hubs: Vec<Obj<'py>>,
    me: usize,
) -> PyResult<Option<Obj<'py>>> {
    let rs = py.import("railstations")?;
    let bs = py.import("busstations")?;
    let st = rs.call_method0("RailStation")?;
    let centres = seeds.iter().map(centre).collect::<PyResult<Vec<_>>>()?;
    let hub_obj = array(py, &centres).call_method1("mean", (0,))?;
    let hub = point(&hub_obj)?;
    st.setattr("hub", &hub_obj)?;
    let mut name = String::new();
    let mut infra_name = String::new();
    let mut station = false;
    for s in &seeds {
        let n = field(s, "name")?;
        if !n.is_empty() {
            if name.is_empty() {
                name = n.clone();
            }
            if infra_name.is_empty() && field(s, "layer")? == "infrastructure" {
                infra_name = n;
            }
        }
        station |= field(s, "cls")? == "railway_station";
    }
    st.setattr(
        "name",
        if infra_name.is_empty() {
            name
        } else {
            infra_name
        },
    )?;
    st.setattr("kind", if station { "station" } else { "halt" })?;
    let others = hubs
        .iter()
        .enumerate()
        .filter(|(i, _)| *i != me)
        .map(|(_, p)| point(p))
        .collect::<PyResult<Vec<_>>>()?;
    let mut near = vec![];
    for it in items {
        let c = centre(&it)?;
        let d = (c - hub).norm();
        if d < 460.
            && (others.is_empty()
                || field(&it, "layer")? == "roads" && field(&it, "sub")? == "rail"
                || others.iter().all(|p| (c - *p).norm() >= d))
        {
            near.push(it);
        }
    }
    let tracks = tracks(&near)?;
    let edge = rs
        .call_method1("norm", (country,))?
        .get_item(0)?
        .extract()?;
    let mut plats = vec![];
    let mut roofs = vec![];
    let mut buildings = vec![];
    for it in &near {
        if rail_train_platform(it)? {
            plats.push((field(it, "id")?, it));
        }
        if field(it, "layer")? != "buildings" || field(it, "kind")? != "polygon" {
            continue;
        }
        match field(it, "cls")?.as_str() {
            "train_station" | "transportation"
                if (centre(it)? - hub).norm() < 160.
                    && bs.call_method1("item_area", (it,))?.extract::<f64>()? > 60. =>
            {
                buildings.push((
                    it.get_item("id")?,
                    bs.call_method1("_open", (it.get_item("xy")?,))?,
                    val(it, "height")?,
                ));
            }
            "roof" | "shelter" | "canopy" => roofs.push((field(it, "id")?, it)),
            _ => {}
        }
    }
    plats.sort_by(|a, b| a.0.cmp(&b.0));
    roofs.sort_by(|a, b| a.0.cmp(&b.0));
    let mut platforms = vec![];
    let mut used = false;
    for (_, it) in plats {
        if let Some((p, has)) = mapped(&rs, it, &tracks, hub, edge)? {
            platforms.push(p);
            used |= has;
        }
    }
    st.setattr("platforms", &platforms)?;
    let near_tracks = tracks
        .iter()
        .filter(|t| distance(&t.pts, hub, false) < 380.)
        .map(|t| t.row.clone())
        .collect::<Vec<_>>();
    st.setattr("tracks", &near_tracks)?;
    st.setattr("buildings", &buildings)?;
    let mut roof_ids = std::collections::BTreeSet::new();
    for (id, it) in roofs {
        let ring = bs.call_method1("_open", (it.get_item("xy")?,))?;
        let r = points(ring.extract()?)?;
        for p in &platforms {
            let pl = points(p.getattr("ring")?.extract()?)?;
            let a = open(&r);
            let b = open(&pl);
            let covered = a
                .iter()
                .step_by((a.len() / 12).max(1))
                .any(|&q| inside(b, q))
                || b.iter()
                    .step_by((b.len() / 12).max(1))
                    .any(|&q| inside(a, q))
                || r.iter().any(|&q| distance(&pl, q, true) < 6.);
            if !covered {
                continue;
            }
            p.getattr("roofs")?.call_method1("append", (&id,))?;
            if roof_ids.insert(id.clone()) {
                st.getattr("roofs")?
                    .call_method1("append", ((&id, &ring),))?;
                st.getattr("claimed")?.cast::<PySet>()?.add(&id)?;
                let (look, known): (Obj, bool) = canopies::item_look(it)?;
                st.getattr("roof_looks")?.set_item(&id, &look)?;
                if known {
                    st.getattr("__dict__")?
                        .call_method1("setdefault", ("_known_looks", PyList::empty(py)))?
                        .call_method1("append", (look,))?;
                }
            }
        }
    }
    st.setattr(
        "links",
        rs.call_method1("_links", (&near, near_tracks, &platforms, hub_obj))?,
    )?;
    rs.call_method1("synth_canopies", (&st,))?;
    let mapped = !platforms.is_empty();
    if !mapped && !(rs.getattr("ROAD_FIT")?.is_truthy()? && freight_only(&seeds)?) {
        let all = tracks.iter().map(|t| t.row.clone()).collect::<Vec<_>>();
        let pl = rs.call_method1("_synth", (&st, &near, all, edge))?;
        if !pl.is_none() {
            used = pl.getattr("snapped")?.call_method0("any")?.is_truthy()?;
            st.setattr("platforms", vec![pl])?;
            st.setattr("synth", true)?;
        }
    }
    let near_track = tracks.iter().any(|t| distance(&t.pts, hub, false) < 60.);
    let confidence = 3 * mapped as usize
        + 2 * near_track as usize
        + 1
        + (station || seeds.len() > 1) as usize
        + (!buildings.is_empty()) as usize
        + used as usize;
    st.setattr("confidence", confidence)?;
    Ok((confidence >= 4 && st.getattr("platforms")?.len()? > 0).then_some(st))
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rail_hubs, m)?)?;
    m.add_function(wrap_pyfunction!(rail_stations, m)?)?;
    m.add_function(wrap_pyfunction!(rail_tracks_beside, m)?)?;
    m.add_function(wrap_pyfunction!(rail_platform_obstacles, m)?)?;
    m.add_function(wrap_pyfunction!(rail_find, m)?)?;
    m.add_function(wrap_pyfunction!(rail_train_platform, m)?)?;
    m.add_function(wrap_pyfunction!(rail_assemble, m)?)
}

fn meets(b: [f64; 4], track: [f64; 4], m: f64) -> bool {
    !(track[2] < b[0] - m || track[0] > b[2] + m || track[3] < b[1] - m || track[1] > b[3] + m)
}
#[pyfunction]
fn rail_tracks_beside<'p>(
    ring: &Obj<'p>,
    tracks: Vec<Obj<'p>>,
    margin: f64,
) -> PyResult<Vec<Obj<'p>>> {
    let b = bbox(&points(ring.extract()?)?);
    let mut out = vec![];
    for t in tracks {
        if meets(b, bbox(&points(t.get_item(1)?.extract()?)?), margin) {
            out.push(t);
        }
    }
    Ok(out)
}
#[pyfunction]
fn rail_hubs<'p>(py: Python<'p>, groups: Vec<Vec<Obj<'p>>>) -> PyResult<Vec<Obj<'p>>> {
    hubs(py, &groups)
}
fn hubs<'p>(py: Python<'p>, groups: &[Vec<Obj<'p>>]) -> PyResult<Vec<Obj<'p>>> {
    groups
        .iter()
        .map(|g| {
            array(py, &g.iter().map(centre).collect::<PyResult<Vec<_>>>()?)
                .call_method1("mean", (0,))
        })
        .collect()
}
#[pyfunction]
fn rail_stations<'p>(
    py: Python<'p>,
    items: Vec<Obj<'p>>,
    country: &Obj<'p>,
) -> PyResult<Vec<Obj<'p>>> {
    let groups = rail_find(py, items.clone())?;
    let hubs = hubs(py, &groups)?;
    let mut out = vec![];
    for (k, g) in groups.into_iter().enumerate() {
        if let Some(st) = rail_assemble(py, items.clone(), g, country, hubs.clone(), k)? {
            out.push(st)
        }
    }
    Ok(out)
}
fn freight_only(seeds: &[Obj]) -> PyResult<bool> {
    let mut any = false;
    for it in seeds {
        if field(it, "layer")? == "infrastructure" {
            any = true;
            let tags = it.call_method1("get", ("tags",))?;
            if !tags.is_truthy()?
                || !(tags.call_method1("get", ("public_transport",))?.eq("no")?
                    || tags.call_method1("get", ("passenger",))?.eq("no")?)
            {
                return Ok(false);
            }
        }
    }
    Ok(any)
}
#[pyfunction]
fn rail_platform_obstacles<'p>(
    py: Python<'p>,
    near: Vec<Obj<'p>>,
    walk: &Obj<'p>,
    styles: &Obj<'p>,
) -> PyResult<(Obj<'p>, Obj<'p>)> {
    let rs = py.import("railstations")?;
    let cars = PyDict::new(py);
    let walks = PyDict::new(py);
    for it in near {
        if field(&it, "layer")? != "roads"
            || field(&it, "sub")? == "rail"
            || it.get_item("xy")?.len()? < 2
            || val(&it, "level")? != 0.
            || links::rail_span_kind(&it)?.is_some()
        {
            continue;
        }
        let cls = it.get_item("cls")?.str()?;
        let tags = it.call_method1("get", ("tags",))?;
        let w: Option<f64> = if tags.is_truthy()? {
            rs.call_method1("_num", (tags.call_method1("get", ("width",))?,))?
                .extract()?
        } else {
            None
        };
        let (out, width) = if walk.contains(&cls)? {
            (&walks, walk.get_item(&cls)?)
        } else if styles.contains(&cls)? {
            (&cars, styles.get_item(&cls)?.get_item(0)?)
        } else {
            continue;
        };
        out.set_item(
            it.get_item("id")?.str()?,
            (xy(&it)?, w.unwrap_or(0.).max(width.extract()?) / 2.),
        )?;
    }
    Ok((cars.into_any(), walks.into_any()))
}
