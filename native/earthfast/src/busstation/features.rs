//! Overture cell rows -> station items. The cell owns the row memo; each frame projects
//! candidate rings in batches. Repeated IDs are projected one at a time until accepted.
use super::*;
use numpy::PyReadonlyArray1;
use pyo3::types::{PyDict, PyFloat, PyList, PySet, PyString, PyTuple};
use std::collections::BTreeSet;

const EXTRA: [&str; 10] = [
    "height",
    "min_height",
    "num_floors",
    "roof_shape",
    "surface",
    "basic_category",
    "taxonomy",
    "subclass",
    "roof_color",
    "roof_material",
];

fn lower<'p>(s: &Bound<'p, PyAny>) -> PyResult<Bound<'p, PyAny>> {
    s.call_method0("lower")
}

// The common case is an object of strings. Visit it in source order (a JSON tree
// backed by a sorted map would change the iteration order of normalized tags).
struct StringTags(Vec<(String, String)>);
impl<'de> serde::Deserialize<'de> for StringTags {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct TagsVisitor;
        impl<'de> serde::de::Visitor<'de> for TagsVisitor {
            type Value = StringTags;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("a map of string tags")
            }
            fn visit_map<A: serde::de::MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut tags = Vec::new();
                while let Some((key, value)) = map.next_entry::<String, String>()? {
                    // json.loads resolves identical raw keys before normalizing their case.
                    if let Some((_, old)) = tags.iter_mut().find(|(k, _)| k == &key) {
                        *old = value;
                    } else {
                        tags.push((key, value));
                    }
                }
                Ok(StringTags(tags))
            }
        }
        d.deserialize_map(TagsVisitor)
    }
}
fn normalize<'p>(py: Python<'p>, text: &str) -> PyResult<Bound<'p, PyAny>> {
    if text.is_ascii() {
        let s = text.trim_matches(|c: char| c.is_ascii_whitespace() || c == '\x0b' || ('\u{1c}'..='\u{1f}').contains(&c));
        return Ok(PyString::new(py, &s.to_ascii_lowercase()).into_any());
    }
    lower(&PyString::new(py, text).call_method0("strip")?)
}
struct Columns<'p> {
    attrs: Bound<'p, PyDict>,
    floating: Bound<'p, PyAny>,
    loads: Bound<'p, PyAny>,
}
impl<'p> Columns<'p> {
    fn new(feats: &Bound<'p, PyAny>) -> PyResult<Self> {
        let py = feats.py();
        Ok(Self {
            attrs: feats.getattr("attrs")?.cast_into()?,
            floating: py.import("numpy")?.getattr("floating")?,
            loads: py.import("json")?.getattr("loads")?,
        })
    }
    fn attr(&self, name: &str, i: usize) -> PyResult<Option<Bound<'p, PyAny>>> {
        let Some(col) = self.attrs.get_item(name)?.filter(|c| !c.is_none()) else {
            return Ok(None);
        };
        let val = col.get_item(i)?;
        if val.is_instance_of::<PyFloat>() || val.is_instance(&self.floating)? {
            let v: f64 = val.extract()?;
            return Ok(v.is_finite().then(|| PyFloat::new(val.py(), v).into_any()));
        }
        let s = val.str()?.call_method0("strip")?;
        Ok(if matches!(s.extract::<String>()?.as_str(), "" | "None" | "nan") {
            None
        } else {
            Some(s)
        })
    }
    fn tags(&self, i: usize) -> PyResult<Bound<'p, PyDict>> {
        let out = PyDict::new(self.attrs.py());
        let Some(col) = self.attrs.get_item("tags")?.filter(|c| !c.is_none()) else {
            return Ok(out);
        };
        let s = col.get_item(i)?.str()?;
        let raw = s.to_string_lossy();
        if matches!(raw.as_ref(), "" | "{}" | "None" | "null") {
            return Ok(out);
        }
        if let Ok(StringTags(tags)) = serde_json::from_str(&raw) {
            for (key, value) in tags {
                out.set_item(normalize(out.py(), &key)?, normalize(out.py(), &value)?)?;
            }
            return Ok(out);
        }
        // Other JSON values use the Python decoder and str (including nested dict repr).
        if let Ok(value) = self.loads.call1((s,)) {
            if let Ok(d) = value.cast::<PyDict>() {
                for (k, v) in d.iter() {
                    out.set_item(
                        lower(&k.str()?.call_method0("strip")?)?,
                        lower(&v.str()?.call_method0("strip")?)?,
                    )?;
                }
            }
        }
        Ok(out)
    }
}
#[pyfunction]
fn bus_tags<'p>(feats: &Bound<'p, PyAny>, i: usize) -> PyResult<Bound<'p, PyDict>> {
    Columns::new(feats)?.tags(i)
}

fn rule(feats: &Bound<'_, PyAny>, name: &str, i: usize) -> String {
    let get = || -> PyResult<String> {
        let tab = feats.getattr("rules")?.get_item(name)?;
        if tab.getattr("feature")?.len()? == 0 {
            return Ok(String::new());
        }
        let j: isize = tab.call_method1("major", (i,))?.extract()?;
        if j < 0 {
            return Ok(String::new());
        }
        lower(&tab.getattr("value")?.get_item(j)?.str()?.call_method0("strip")?)?.extract()
    };
    get().unwrap_or_default()
}
fn base<'p>(
    feats: &Bound<'p, PyAny>,
    cols: &Columns<'p>,
    name: &str,
    i: usize,
    fid: &str,
) -> PyResult<Bound<'p, PyAny>> {
    let py = feats.py();
    let kind = feats.getattr("kind")?.get_item(i)?.extract::<u8>()?;
    let parts = feats.call_method1("parts", (i,))?;
    if !(1..=3).contains(&kind) || parts.len()? == 0 || parts.get_item(0)?.len()? == 0 {
        return Ok(py.None().into_bound(py));
    }
    let ring = parts.get_item(0)?.get_item(0)?;
    if ring.len()? == 0 {
        return Ok(py.None().into_bound(py));
    }
    let ring = py.import("numpy")?.call_method1("asarray", (ring, "float64"))?;
    let it = PyDict::new(py);
    it.set_item("layer", name)?;
    it.set_item("id", fid)?;
    let empty = PyString::new(py, "").into_any();
    let value = |key| -> PyResult<_> {
        Ok(match cols.attr(key, i)? {
            Some(v) if v.is_truthy()? => v,
            _ => empty.clone(),
        })
    };
    let cls = lower(&value("class")?)?;
    it.set_item("cls", &cls)?;
    it.set_item("sub", lower(&value("subtype")?)?)?;
    it.set_item("name", value("name")?)?;
    it.set_item("tags", cols.tags(i)?)?;
    it.set_item("kind", ["point", "line", "polygon"][(kind - 1) as usize])?;
    it.set_item("xy", py.None())?;
    for key in EXTRA {
        if let Some(v) = cols.attr(key, i)? {
            it.set_item(key, v)?
        }
    }
    if name == "roads" {
        let surface = rule(feats, "surface", i);
        if !surface.is_empty() && !it.contains("surface")? {
            it.set_item("surface", surface)?
        }
        for key in ["level", "oneway"] {
            let s = rule(feats, key, i);
            let v = if s.is_empty() {
                0.0
            } else {
                PyFloat::new(py, 0.0)
                    .get_type()
                    .call1((s,))
                    .and_then(|v| v.extract())
                    .unwrap_or(0.0)
            };
            it.set_item(key, v)?;
        }
        if !it.contains("subclass")? {
            let s = rule(feats, "subclass", i);
            if !s.is_empty() {
                it.set_item("subclass", s)?
            }
        }
        if matches!(
            cls.extract::<String>()?.as_str(),
            "footway" | "steps" | "path" | "pedestrian" | "corridor" | "cycleway"
        ) {
            let spans = PyList::empty(py);
            if let Ok(ranges) = py.import("roads")?.call_method1("level_ranges", (feats, i)) {
                if ranges.is_truthy()? {
                    for row in ranges.try_iter()? {
                        let r = row?;
                        let span = PyList::empty(py);
                        span.append(py_round(r.get_item(0)?.extract()?, 3))?;
                        span.append(py_round(r.get_item(1)?.extract()?, 3))?;
                        span.append(r.get_item(2)?)?;
                        spans.append(span)?;
                    }
                }
            }
            if !spans.is_empty() {
                it.set_item("spans", spans)?
            }
        }
    }
    Ok(PyTuple::new(py, [ring, it.into_any()])?.into_any())
}

type Candidate<'p> = (String, Bound<'p, PyAny>);
fn take(
    frame: &Bound<'_, PyAny>,
    name: &str,
    cand: &[Candidate<'_>],
    hub: Point,
    radius: f64,
    seen: &Bound<'_, PySet>,
    out: &Bound<'_, PyList>,
) -> PyResult<()> {
    if cand.is_empty() {
        return Ok(());
    }
    let py = frame.py();
    let mut offsets = vec![0];
    let mut la = Vec::new();
    let mut lo = Vec::new();
    for (_, row) in cand {
        let ring = row.get_item(0)?;
        let xy = ring.extract::<XY>()?;
        if xy.as_array().ncols() < 2 {
            return Err(PyValueError::new_err("expected longitude and latitude columns"));
        }
        for p in xy.as_array().rows() {
            la.push(p[1]);
            lo.push(p[0]);
        }
        offsets.push(la.len());
    }
    let en = frame.call_method1("geodetic_to_enu", (la.into_pyarray(py), lo.into_pyarray(py), 0.0))?;
    let east = en.get_item(0)?;
    let north = en.get_item(1)?;
    let x = east.extract::<PyReadonlyArray1<f64>>()?;
    let y = north.extract::<PyReadonlyArray1<f64>>()?;
    let x = x.as_array();
    let y = y.as_array();
    if x.len() != offsets[cand.len()] || y.len() != x.len() {
        return Err(PyValueError::new_err("expected one projected point per coordinate"));
    }
    for (i, (fid, row)) in cand.iter().enumerate() {
        let range = offsets[i]..offsets[i + 1];
        if range.clone().all(|j| (x[j] - hub.0).dhypot(y[j] - hub.1) > radius) || seen.contains((name, fid))? {
            continue;
        }
        seen.add((name, fid))?;
        let it = row.get_item(1)?.cast_into::<PyDict>()?.copy()?;
        it.set_item("xy", rows(py, range.map(|j| [x[j], y[j]]).collect()))?;
        out.append(it)?;
    }
    Ok(())
}

#[pyfunction]
fn bus_item_cell(
    feats: &Bound<'_, PyAny>,
    frame: &Bound<'_, PyAny>,
    name: &str,
    memo: &Bound<'_, PyDict>,
    hub: (f64, f64),
    radius: f64,
    bounds: (f64, f64, f64, f64),
    seen: &Bound<'_, PySet>,
    out: &Bound<'_, PyList>,
) -> PyResult<()> {
    let n = feats.len()?;
    let cols = Columns::new(feats)?;
    let ids = feats.getattr("ids")?;
    let boxes = if feats.hasattr("lonlat_boxes")? {
        Some(feats.call_method0("lonlat_boxes")?)
    } else {
        None
    };
    let b = boxes
        .as_ref()
        .filter(|b| !b.is_none())
        .map(|b| b.extract::<XY>())
        .transpose()?;
    if b.as_ref()
        .is_some_and(|v| v.as_array().nrows() == n && v.as_array().ncols() < 4)
    {
        return Err(PyValueError::new_err("feature bounds require four columns"));
    }
    let mut selected = Vec::new();
    let mut unique = BTreeSet::new();
    let mut batch = true;
    for i in 0..n {
        if let Some(b) = &b {
            let v = b.as_array();
            if v.nrows() == n
                && !(v[[i, 0]] <= bounds.2
                    && v[[i, 2]] >= bounds.0
                    && v[[i, 1]] <= bounds.3
                    && v[[i, 3]] >= bounds.1)
            {
                continue;
            }
        }
        let fid = ids.get_item(i)?.str()?.extract::<String>()?;
        batch &= unique.insert(fid.clone());
        selected.push((i, fid));
    }
    let hub = Point(hub.0, hub.1);
    let mut cand = Vec::new();
    for (i, fid) in selected {
        if seen.contains((name, &fid))? {
            continue;
        }
        let row = match memo.get_item(i)? {
            Some(row) => row,
            None => {
                let v = base(feats, &cols, name, i, &fid)?;
                memo.set_item(i, &v)?;
                v
            }
        };
        if row.is_none() {
            continue;
        }
        cand.push((fid, row));
        if !batch {
            take(frame, name, &cand, hub, radius, seen, out)?;
            cand.clear();
        }
    }
    take(frame, name, &cand, hub, radius, seen, out)
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(bus_search_bounds, m)?)?;
    m.add_function(wrap_pyfunction!(bus_seed_points, m)?)?;
    m.add_function(wrap_pyfunction!(bus_seed_groups, m)?)?;
    m.add_function(wrap_pyfunction!(bus_near_tile, m)?)?;
    m.add_function(wrap_pyfunction!(bus_tags, m)?)?;
    m.add_function(wrap_pyfunction!(bus_item_cell, m)?)
}

#[pyfunction]
fn bus_search_bounds(lat: f64, lon: f64, radius: f64) -> (f64, f64, f64, f64) {
    let rla = 1.02 * radius / 110540.0 + 1e-7;
    let rlo = 1.02 * radius / (111320.0 * (lat.abs() + rla).min(89.0).to_radians().dcos().max(1e-3)) + 1e-7;
    (lon - rlo, lat - rla, lon + rlo, lat + rla)
}
#[pyfunction]
fn bus_seed_points(feats: &Bound<'_, PyAny>, indices: Vec<usize>) -> PyResult<Vec<(f64, f64)>> {
    let mut out = Vec::new();
    for i in indices {
        let p = feats.call_method1("parts", (i,))?;
        if p.len()? == 0 || p.get_item(0)?.len()? == 0 {
            continue;
        }
        let r = p.get_item(0)?.get_item(0)?;
        if r.len()? == 0 {
            continue;
        }
        let a = feats.py().import("numpy")?.call_method1("asarray", (r, "float64"))?;
        let r = points(a.extract()?)?;
        let n = r.len() as f64;
        let mean =
            |axis: fn(&Point) -> f64| crate::junctionsnap::np_sum(&r.iter().map(axis).collect::<Vec<_>>()) / n;
        out.push((mean(|p| p.0), mean(|p| p.1)));
    }
    Ok(out)
}
#[pyfunction]
fn bus_seed_groups(seeds: Vec<(f64, f64)>) -> Vec<Vec<(f64, f64)>> {
    let mut seeds: Vec<_> = seeds
        .into_iter()
        .map(|(a, b)| (py_round(a, 5), py_round(b, 5)))
        .collect();
    seeds.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    seeds.dedup();
    let mut groups: Vec<Vec<(f64, f64)>> = Vec::new();
    for s in seeds {
        if let Some(g) = groups
            .iter_mut()
            .find(|g| (g[0].0 - s.0).abs() < 0.004 && (g[0].1 - s.1).abs() < 0.003)
        {
            g.push(s)
        } else {
            groups.push(vec![s])
        }
    }
    groups
}
#[pyfunction]
fn bus_near_tile<'p>(
    found: Vec<(Bound<'p, PyAny>, Bound<'p, PyAny>)>,
    frame: &Bound<'p, PyAny>,
    to_frame: &Bound<'p, PyAny>,
    b: (f64, f64, f64, f64),
    reach: f64,
) -> PyResult<Vec<Bound<'p, PyAny>>> {
    let mut seen = BTreeSet::new();
    let mut out = Vec::new();
    for (st, gf) in found {
        if !seen.insert(st.getattr("fid")?.extract::<String>()?) {
            continue;
        }
        let reg = to_frame.call1((gf, frame, st.getattr("region")?))?;
        let xy = points(reg.extract()?)?;
        if xy.is_empty() {
            return Err(PyValueError::new_err(
                "zero-size array to reduction operation minimum which has no identity",
            ));
        }
        let mut lo = Point(f64::INFINITY, f64::INFINITY);
        let mut hi = -lo;
        for p in xy {
            lo = Point(mesh::npmin(lo.0, p.0), mesh::npmin(lo.1, p.1));
            hi = Point(mesh::npmax(hi.0, p.0), mesh::npmax(hi.1, p.1));
        }
        if lo.0 - reach <= b.2 && hi.0 + reach >= b.0 && lo.1 - reach <= b.3 && hi.1 + reach >= b.1 {
            out.push(st)
        }
    }
    Ok(out)
}
