//! Group station seeds and classify the mapped platforms, stops, roofs, halls and roads.
//! Keep the original item dictionaries: railways and street callers share their cached geometry.
use super::*;
use blake2::{
    digest::{Update, VariableOutput},
    Blake2bVar,
};
use pyo3::types::{PyDict, PyList, PySet, PySlice};
use std::collections::{BTreeMap, BTreeSet};

type Dict<'p> = Bound<'p, PyDict>;
const GENERIC: &[&str] = &[
    "автовокзал",
    "автостанц",
    "автовозал",
    "autobusov",
    "autobus",
    "terminál",
    "terminal",
    "nádraží",
    "bus station",
    "busbahnhof",
    "dworzec",
    "otogar",
    "estación",
    "estacion",
    "bus terminal",
    "coach station",
    "busstation",
    "autostazione",
    "stazione",
];
fn name_matches(name: &str, names: &[String]) -> bool {
    let n = name.to_lowercase();
    !n.is_empty()
        && (GENERIC.iter().any(|s| n.contains(s))
            || n.replace('-', " ").split_whitespace().any(|w| w == "zob")
            || names.iter().any(|s| {
                s.to_lowercase()
                    .replace(',', " ")
                    .split_whitespace()
                    .filter(|w| w.chars().count() >= 5)
                    .map(|w| w.trim_matches(['"', '«', '»', '(', ')']))
                    .any(|w| !w.is_empty() && n.contains(w))
            }))
}
#[pyfunction]
fn bus_name_matches(name: &str, names: Vec<String>) -> bool {
    name_matches(name, &names)
}
#[pyfunction]
fn bus_bay_label(py: Python<'_>, sources: Vec<Option<String>>) -> PyResult<String> {
    for s in sources.into_iter().flatten() {
        let w = s
            .trim()
            .split(' ')
            .next()
            .unwrap_or("")
            .trim_matches(['(', ')', '[', ']', '"', '\'', '«', '»', ',', ';']);
        let n = w.chars().count();
        if n > 0 && n <= 4 {
            let digit = |c: char| -> PyResult<bool> {
                pyo3::types::PyString::new(py, &c.to_string())
                    .call_method0("isdigit")?
                    .extract()
            };
            if n == 1
                || w.chars()
                    .any(|c| c.is_ascii_digit() || (!c.is_ascii() && digit(c).unwrap_or(false)))
            {
                return Ok(w.to_uppercase());
            }
        }
    }
    Ok(String::new())
}
fn text(d: &Dict<'_>, k: &str) -> PyResult<String> {
    Ok(d.get_item(k)?
        .filter(|v| !v.is_none())
        .map(|v| v.extract())
        .transpose()?
        .unwrap_or_default())
}
pub(super) fn optional_float(v: Option<Bound<'_, PyAny>>) -> PyResult<f64> {
    match v {
        Some(v) if v.is_truthy()? => v.py().get_type::<pyo3::types::PyFloat>().call1((v,))?.extract(),
        _ => Ok(0.0),
    }
}
struct Item<'p> {
    obj: Dict<'p>,
    xy: Bound<'p, PyAny>,
    ring: Ring,
    layer: String,
    kind: String,
    cls: String,
    sub: String,
    name: String,
    id: String,
    tags: Dict<'p>,
}
impl<'p> Item<'p> {
    fn new(d: Dict<'p>) -> PyResult<Self> {
        let xy = d.as_any().get_item("xy")?;
        let ring = points(xy.extract()?)?;
        Ok(Self {
            layer: text(&d, "layer")?,
            kind: text(&d, "kind")?,
            cls: text(&d, "cls")?,
            sub: text(&d, "sub")?,
            name: text(&d, "name")?,
            id: d.as_any().get_item("id")?.str()?.extract()?,
            tags: d.as_any().get_item("tags")?.cast_into()?,
            obj: d,
            xy,
            ring,
        })
    }
    fn tag(&self, k: &str) -> PyResult<String> {
        text(&self.tags, k)
    }
    fn seed(&self) -> PyResult<bool> {
        Ok((self.layer == "infrastructure" && self.cls == "bus_station")
            || (self.layer == "buildings" && self.tag("amenity")? == "bus_station"))
    }
    fn polygon(&self) -> bool {
        self.kind == "polygon"
    }
    fn centre(&self) -> PyResult<Point> {
        if self.kind == "point" {
            return self
                .ring
                .first()
                .copied()
                .ok_or_else(|| PyValueError::new_err("empty station point"));
        }
        if let Some(p) = self.obj.get_item("_c")?.filter(|v| !v.is_none()) {
            return point(&p);
        }
        let p = centroid(&self.ring);
        self.obj.set_item("_c", p.arr(self.obj.py()))?;
        Ok(p)
    }
    fn area(&self) -> PyResult<f64> {
        if let Some(a) = self.obj.get_item("_a")?.filter(|v| !v.is_none()) {
            return a.extract();
        }
        let a = area(&self.ring);
        self.obj.set_item("_a", a)?;
        Ok(a)
    }
    fn opened(&self) -> PyResult<Bound<'p, PyAny>> {
        let n = open(&self.ring).len();
        if n == self.ring.len() {
            Ok(self.xy.clone())
        } else {
            self.xy.get_item(PySlice::new(self.xy.py(), 0, n as isize, 1))
        }
    }
    fn canopy(&self, hall: bool) -> PyResult<Dict<'p>> {
        let h = self.obj.get_item("height")?;
        let mut mapped = h.as_ref().map(|v| v.is_truthy()).transpose()?.unwrap_or(false);
        let height = optional_float(h)?;
        mapped &= !hall || height <= 8.0;
        let d = PyDict::new(self.obj.py());
        d.set_item("ring", self.opened()?)?;
        d.set_item("height", if mapped { height } else { 3.8 })?;
        d.set_item("src", self.obj.as_any().get_item("id")?)?;
        d.set_item("area", self.area()?)?;
        d.set_item("mapped", mapped)?;
        Ok(d)
    }
}
fn roof(s: &str) -> bool {
    matches!(s, "roof" | "shelter" | "carport" | "canopy")
}
fn street(s: &str) -> bool {
    matches!(
        s,
        "motorway"
            | "trunk"
            | "primary"
            | "secondary"
            | "tertiary"
            | "residential"
            | "unclassified"
            | "living_street"
    )
}
fn green(s: &str) -> bool {
    matches!(
        s,
        "grass" | "park" | "forest" | "meadow" | "garden" | "wood" | "shrub" | "flowerbed" | "scrub"
    )
}
pub(super) fn point(v: &Bound<'_, PyAny>) -> PyResult<Point> {
    Ok(Point(v.get_item(0)?.extract()?, v.get_item(1)?.extract()?))
}
fn root(parents: &mut [usize], mut i: usize) -> usize {
    while parents[i] != i {
        parents[i] = parents[parents[i]];
        i = parents[i];
    }
    i
}
fn assemble<'p>(
    py: Python<'p>,
    items: &[Item<'p>],
    seeds: &[&Item<'p>],
    factory: &Bound<'p, PyAny>,
    passages: &Bound<'p, PySet>,
) -> PyResult<Bound<'p, PyAny>> {
    let st = factory.call0()?;
    let mut biggest = None;
    let mut names = Vec::new();
    let mut centres = Vec::new();
    for &s in seeds {
        if s.polygon() {
            let a = s.area()?;
            if a >= 400.0 && biggest.is_none_or(|(b, _)| a > b) {
                biggest = Some((a, s));
            }
        }
        centres.push(s.centre()?);
        if !s.name.is_empty() && (s.seed()? || name_matches(&s.name, &[])) {
            names.push(s.name.clone())
        }
    }
    st.setattr("name", names.first().map(String::as_str).unwrap_or(""))?;
    let mut evidence = Vec::new();
    let mut boundary = None;
    let hub = if let Some((_, s)) = biggest {
        let ring = open(&s.ring);
        let ring_area = area(ring);
        let mut own = false;
        for it in items {
            if it.layer == "buildings"
                && it.polygon()
                && it.area()? >= 0.7 * ring_area
                && inside(ring, it.centre()?)
            {
                own = true;
                break;
            }
        }
        if !own {
            boundary = Some(ring);
            st.setattr("boundary", s.opened()?)?;
            st.setattr("surface", text(&s.obj, "surface")?)?;
        }
        evidence.push(("boundary polygon", 3));
        centroid(ring)
    } else {
        centres.iter().copied().fold(Point::default(), |a, b| a + b) / centres.len() as f64
    };
    st.setattr("hub", hub.arr(py))?;
    if seeds.iter().any(|s| s.layer == "places") {
        evidence.push(("place", 1))
    }
    if !names.is_empty() {
        evidence.push(("name", 1))
    }
    let in_region = |p: Point, pad: f64| {
        boundary.map_or_else(
            || (p - hub).norm() <= 70.0,
            |r| inside(r, p) || distance(r, p, true) <= pad,
        )
    };
    let taken: BTreeSet<_> = seeds.iter().map(|s| s.obj.as_ptr()).collect();
    let mut platforms = Vec::new();
    let mut roofs = Vec::new();
    let claimed = st.getattr("claimed")?.cast_into::<PySet>()?;
    for it in items {
        if taken.contains(&it.obj.as_ptr())
            || !matches!(it.layer.as_str(), "buildings" | "infrastructure")
            || !in_region(it.centre()?, 12.0)
        {
            continue;
        }
        if it.layer == "infrastructure" && it.cls == "platform" && it.kind != "point" {
            let rail = it.tag("railway")? == "platform" || it.tag("train")? == "yes" || it.tag("tram")? == "yes";
            if !rail || it.tag("bus")? == "yes" || matches!(it.tag("highway")?.as_str(), "platform" | "bus_stop") {
                platforms.push(it);
            }
        }
        let canopy = roof(&it.cls)
            || roof(&it.tag("building")?)
            || (it.layer == "infrastructure"
                && it.cls == "platform"
                && it.polygon()
                && it.tag("covered")? == "yes");
        if canopy && it.layer == "buildings" {
            claimed.add(it.obj.as_any().get_item("id")?)?;
        }
        if canopy && it.polygon() && it.area()? >= 14.0 {
            roofs.push((it.area()?, it, it.canopy(false)?));
        }
    }
    st.setattr("platforms", PyList::new(py, platforms.iter().map(|s| &s.obj))?)?;
    let mut stops = Vec::new();
    let mut close = 0;
    for it in items {
        if it.layer == "infrastructure" && it.cls == "bus_stop" && it.kind == "point" {
            let p = it.centre()?;
            if in_region(p, 37.0) {
                let near = (p - hub).norm() <= 70.0;
                close += near as usize;
                stops.push((it, p, near));
            }
        }
    }
    let mut bays = Vec::new();
    let bay_list = st.getattr("bays")?.cast_into::<PyList>()?;
    for (s, p, near) in stops {
        if !(boundary.is_some() && in_region(p, 2.0) || name_matches(&s.name, &names) || close >= 3 && near) {
            continue;
        }
        let label = bus_bay_label(
            py,
            vec![Some(s.tag("local_ref")?), Some(s.tag("ref")?), Some(s.name.clone())],
        )?;
        let d = PyDict::new(py);
        d.set_item("xy", s.xy.get_item(0)?)?;
        d.set_item("label", label)?;
        d.set_item("tags", &s.tags)?;
        d.set_item("name", &s.name)?;
        d.set_item("id", s.obj.as_any().get_item("id")?)?;
        bay_list.append(d)?;
        bays.push(p);
    }
    let mut cand = Vec::new();
    for it in items {
        if it.layer != "buildings" || !it.polygon() || roof(&it.cls) {
            continue;
        }
        let a = it.area()?;
        let c = it.centre()?;
        let d = (c - hub).norm();
        let own = it.cls == "transportation" || it.sub == "transportation" || it.tag("amenity")? == "bus_station";
        let plain = !matches!(it.sub.as_str(), "residential" | "garage")
            && !matches!(
                it.cls.as_str(),
                "garage" | "garages" | "house" | "apartments" | "residential"
            );
        if own && in_region(c, 12.0) || a >= 150.0 && d <= 45.0 && plain {
            cand.push((!own, -a, it));
        }
    }
    cand.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.total_cmp(&b.1)));
    let mut buildings: Vec<_> = cand.iter().take_while(|r| !r.0).map(|r| r.2).collect();
    if buildings.is_empty() {
        buildings.extend(cand.first().map(|r| r.2))
    }
    for it in items {
        if it.layer != "buildings"
            || !it.polygon()
            || it.area()? < 200.0
            || roof(&it.cls)
            || claimed.contains(it.obj.as_any().get_item("id")?)?
            || (it.centre()? - hub).norm() > 150.0
        {
            continue;
        }
        let count = bays.iter().filter(|&&p| inside(&it.ring, p)).count();
        let mut hall = count >= 2;
        if count == 1 {
            for pl in &platforms {
                if inside(&it.ring, pl.centre()?) {
                    hall = true;
                    break;
                }
            }
        }
        if hall {
            buildings.retain(|b| !b.obj.is(&it.obj));
            claimed.add(it.obj.as_any().get_item("id")?)?;
            roofs.push((it.area()?, it, it.canopy(true)?));
        }
    }
    st.setattr("buildings", PyList::new(py, buildings.iter().map(|b| &b.obj))?)?;
    if let Some(b) = buildings.first() {
        evidence.push(("building", if b.cls == "transportation" { 2 } else { 1 }));
    }
    if !platforms.is_empty() {
        evidence.push(("platforms", 2))
    }
    if !bays.is_empty() {
        evidence.push(("bays", 2))
    }
    if !roofs.is_empty() {
        evidence.push(("canopy", 1))
    }
    evidence.push(("seed", 1));
    st.setattr("confidence", evidence.iter().map(|e| e.1).sum::<i32>())?;
    st.setattr("evidence", evidence)?;
    let mut roads = Vec::new();
    let mut streets = Vec::new();
    for it in items {
        if it.layer != "roads" || it.kind != "line" {
            continue;
        }
        let service = matches!(it.cls.as_str(), "service" | "living_street" | "unclassified");
        if !service && !street(&it.cls) {
            continue;
        }
        let d = distance(&it.ring, hub, false);
        if service && d <= 100.0 {
            roads.push(it)
        }
        if street(&it.cls) && d <= 160.0 {
            streets.push(it)
        }
    }
    st.setattr("roads", PyList::new(py, roads.iter().map(|r| &r.obj))?)?;
    st.setattr("streets", PyList::new(py, streets.iter().map(|r| &r.obj))?)?;
    let flat = streets
        .into_iter()
        .map(|r| Ok((optional_float(r.obj.get_item("level")?)?, r)))
        .collect::<PyResult<Vec<_>>>()?;
    let lanes = st.getattr("lanes")?.cast_into::<PyList>()?;
    let mouths = st.getattr("mouths")?.cast_into::<PyList>()?;
    let placed = st.getattr("passages")?.cast_into::<PyList>()?;
    let passage_ids = PyList::empty(py);
    for r in roads {
        if r.ring.len() < 2 || boundary.is_some_and(|b| !inside(b, r.ring[r.ring.len() / 2])) {
            continue;
        }
        placed.append(&r.obj)?;
        let ow = optional_float(r.obj.get_item("oneway")?)?;
        let xy = if ow < 0.0 {
            r.xy.get_item(PySlice::new(py, isize::MAX, isize::MIN, -1))?
        } else {
            r.xy.clone()
        };
        let d = PyDict::new(py);
        d.set_item("xy", xy)?;
        d.set_item("half", if ow != 0.0 { 3.4 } else { 4.0 })?;
        d.set_item("oneway", ow != 0.0)?;
        d.set_item("core", if ow != 0.0 { 3.25 } else { 3.75 })?;
        let id = r.obj.as_any().get_item("id")?;
        d.set_item("id", &id)?;
        lanes.append(d)?;
        passage_ids.append(id)?;
        let mut ends = [r.ring[0], r.ring[r.ring.len() - 1]];
        if ow < 0.0 {
            ends.reverse()
        }
        for end in ends {
            if flat
                .iter()
                .any(|(lv, s)| *lv == 0.0 && distance(&s.ring, end, false) < 8.0)
            {
                mouths.append(end.pair())?
            }
        }
    }
    passages.call_method1("update", (passage_ids,))?;
    let obstacles = st.getattr("obstacles")?.cast_into::<PyList>()?;
    let greens = st.getattr("greens")?.cast_into::<PyList>()?;
    for it in items {
        if !it.polygon() {
            continue;
        }
        if it.layer == "buildings"
            && !roof(&it.cls)
            && !claimed.contains(it.obj.as_any().get_item("id")?)?
            && it.area()? >= 15.0
            && (it.centre()? - hub).norm() <= 140.0
        {
            obstacles.append(it.opened()?)?
        }
        if it.layer == "land_use" && green(&it.cls) && (it.centre()? - hub).norm() <= 200.0 {
            greens.append(it.opened()?)?
        }
    }
    roofs.sort_by(|a, b| b.0.total_cmp(&a.0));
    let mut kept: Vec<(f64, &[Point], Point)> = Vec::new();
    let canopies = st.getattr("canopies")?.cast_into::<PyList>()?;
    for (a, it, d) in roofs {
        let r = open(&it.ring);
        let ctr = centroid(r);
        if kept
            .iter()
            .any(|(b, o, c)| (inside(o, ctr) || (ctr - *c).norm() < 3.0) && a >= 0.6 * b)
        {
            continue;
        }
        kept.push((a, r, ctr));
        canopies.append(d)?;
    }
    if boundary.is_some() {
        st.setattr("region", st.getattr("boundary")?)?;
    } else {
        let mut pts = vec![hub];
        pts.extend(bays.into_iter().filter(|&p| (p - hub).norm() <= 40.0));
        for b in buildings {
            pts.extend_from_slice(open(&b.ring))
        }
        for p in platforms {
            pts.extend_from_slice(&p.ring)
        }
        st.setattr("region", array(py, &pad_ring(&hull(&pts), 5.0)))?;
    }
    let mut ids: Vec<_> = seeds.iter().map(|s| s.id.as_str()).collect();
    ids.sort();
    let mut hash = Blake2bVar::new(6).unwrap();
    hash.update(ids.join("|").as_bytes());
    let mut digest = [0u8; 6];
    hash.finalize_variable(&mut digest).unwrap();
    st.setattr("fid", digest.iter().map(|v| format!("{v:02x}")).collect::<String>())?;
    Ok(st)
}
#[pyfunction]
fn bus_assemble<'p>(
    py: Python<'p>,
    items: Vec<Dict<'p>>,
    seeds: Vec<Dict<'p>>,
    factory: &Bound<'p, PyAny>,
    passages: &Bound<'p, PySet>,
) -> PyResult<Bound<'p, PyAny>> {
    let items = items.into_iter().map(Item::new).collect::<PyResult<Vec<_>>>()?;
    let seeds = seeds.into_iter().map(Item::new).collect::<PyResult<Vec<_>>>()?;
    assemble(py, &items, &seeds.iter().collect::<Vec<_>>(), factory, passages)
}
#[pyfunction]
fn bus_find<'p>(
    py: Python<'p>,
    items: Vec<Dict<'p>>,
    factory: &Bound<'p, PyAny>,
    passages: &Bound<'p, PySet>,
) -> PyResult<Vec<Bound<'p, PyAny>>> {
    // Most cells are station-free. Look at their layer and class before reading any geometry.
    let mut candidates = Vec::new();
    for (i, d) in items.iter().enumerate() {
        let layer = text(d, "layer")?;
        if layer == "infrastructure" && text(d, "cls")? == "bus_station"
            || layer == "buildings"
                && d.as_any()
                    .get_item("tags")?
                    .call_method1("get", ("amenity",))?
                    .eq("bus_station")?
        {
            candidates.push(i)
        }
    }
    if candidates.is_empty() {
        return Ok(vec![]);
    }
    let items = items.into_iter().map(Item::new).collect::<PyResult<Vec<_>>>()?;
    let centres = candidates
        .iter()
        .map(|&i| items[i].centre())
        .collect::<PyResult<Vec<_>>>()?;
    let mut parents: Vec<_> = (0..candidates.len()).collect();
    for i in 0..centres.len() {
        for j in i + 1..centres.len() {
            if (centres[i] - centres[j]).norm() <= 120.0 {
                let a = root(&mut parents, i);
                parents[a] = root(&mut parents, j);
            }
        }
    }
    let mut positions = BTreeMap::new();
    let mut groups: Vec<Vec<usize>> = Vec::new();
    for (i, &s) in candidates.iter().enumerate() {
        let g = *positions.entry(root(&mut parents, i)).or_insert_with(|| {
            groups.push(vec![]);
            groups.len() - 1
        });
        groups[g].push(s);
    }
    let mut found = Vec::new();
    for group in groups {
        let mut real = false;
        for &i in &group {
            let s = &items[i];
            if s.polygon() && s.area()? >= 400.0 {
                real = true;
                break;
            }
        }
        if !real
            && !group
                .iter()
                .any(|&i| items[i].name.is_empty() || name_matches(&items[i].name, &[]))
        {
            continue;
        }
        let mut seeds: Vec<_> = group.iter().map(|&i| &items[i]).collect();
        let centres = seeds.iter().map(|s| s.centre()).collect::<PyResult<Vec<_>>>()?;
        for p in &items {
            if p.layer == "places" && text(&p.obj, "taxonomy")? == "bus_station" {
                let c = p.centre()?;
                if centres.iter().any(|&q| (q - c).norm() <= 120.0) {
                    seeds.push(p)
                }
            }
        }
        found.push(assemble(py, &items, &seeds, factory, passages)?)
    }
    Ok(found)
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(bus_bay_label, m)?)?;
    m.add_function(wrap_pyfunction!(bus_name_matches, m)?)?;
    m.add_function(wrap_pyfunction!(bus_assemble, m)?)?;
    m.add_function(wrap_pyfunction!(bus_find, m)?)
}
