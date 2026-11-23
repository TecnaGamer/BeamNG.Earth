//! Chain elevated paths and stairs, then add unclaimed crossings at track level.
use super::*;
use crate::busstation::{inside, rows};
use numpy::PyArrayMethods;
use std::collections::{BTreeMap, BTreeSet};

struct Way<'p> {
    obj: Obj<'p>,
    id: String,
    cls: String,
    path: Obj<'p>,
    pts: Vec<Point>,
    level: f64,
    span: Option<&'static str>,
}
fn tag(w: &Obj, key: &str) -> PyResult<Option<String>> {
    let tags = w.call_method1("get", ("tags",))?;
    if !tags.is_truthy()? {
        return Ok(None);
    }
    let v = tags.call_method1("get", (key,))?;
    if v.is_truthy()? {
        Ok(Some(v.str()?.extract()?))
    } else {
        Ok(None)
    }
}
#[pyfunction]
pub(super) fn rail_span_kind(w: &Obj) -> PyResult<Option<&'static str>> {
    let mut share = [0., 0.];
    let spans = w.call_method1("get", ("spans",))?;
    if spans.is_truthy()? {
        for s in spans.try_iter()? {
            let s = s?;
            let kind = s.get_item(2)?;
            for (i, k) in ["bridge", "tunnel"].iter().enumerate() {
                if kind.eq(*k)? {
                    share[i] +=
                        s.get_item(1)?.extract::<f64>()? - s.get_item(0)?.extract::<f64>()?;
                }
            }
        }
    }
    for (i, k) in ["bridge", "tunnel"].iter().enumerate() {
        if tag(w, k)?.is_some_and(|v| v != "no") {
            share[i] = f64::max(share[i], 1.);
        }
    }
    let i = usize::from(share[1] > share[0]);
    Ok((share[i] >= 0.4).then_some(["bridge", "tunnel"][i]))
}
fn width(w: &Obj, rs: &Bound<'_, PyModule>) -> PyResult<f64> {
    Ok(rs
        .call_method1(
            "_num",
            (w.get_item("tags")?.call_method1("get", ("width",))?,),
        )?
        .extract::<Option<f64>>()?
        .unwrap_or(0.))
}
fn all_inside(w: &Way, cross: &[(usize, f64)], rings: &[Vec<Point>]) -> bool {
    let mut s = vec![0.];
    for v in w.pts.windows(2) {
        s.push(s.last().unwrap() + (v[1] - v[0]).norm());
    }
    let a: Vec<_> = cross.iter().map(|c| c.1).collect();
    let xs = crate::dm::interp(
        &a,
        &s,
        &w.pts.iter().map(|p| p.0).collect::<Vec<_>>(),
        None,
        None,
    );
    let ys = crate::dm::interp(
        &a,
        &s,
        &w.pts.iter().map(|p| p.1).collect::<Vec<_>>(),
        None,
        None,
    );
    xs.into_iter()
        .zip(ys)
        .all(|(x, y)| rings.iter().any(|r| inside(r, Point(x, y))))
}
#[pyfunction]
fn rail_links<'p>(
    py: Python<'p>,
    items: Vec<Obj<'p>>,
    tracks: &Obj<'p>,
    platforms: Vec<Obj<'p>>,
    hub: &Obj<'p>,
) -> PyResult<List<'p>> {
    let rs = py.import("railstations")?;
    let hub = point(hub)?;
    let mut ways = vec![];
    for it in items {
        if !it.get_item("layer")?.eq("roads")? {
            continue;
        }
        let cls: String = it.get_item("cls")?.extract()?;
        if !matches!(
            cls.as_str(),
            "footway" | "pedestrian" | "corridor" | "path" | "steps" | "cycleway"
        ) {
            continue;
        }
        let raw = it.get_item("xy")?;
        if raw.len()? < 2 {
            continue;
        }
        let sub = it.call_method1("get", ("subclass",))?;
        if sub.eq("sidewalk")? || sub.eq("crosswalk")? {
            continue;
        }
        let path = py
            .import("numpy")?
            .call_method1("asarray", (raw, "float64"))?;
        if (point(&path.call_method1("mean", (0,))?)? - hub).norm() >= 440. {
            continue;
        }
        let pts = points(path.extract()?)?;
        let id = it.get_item("id")?.extract()?;
        let level = super::layout::val(&it, "level")?;
        let span = rail_span_kind(&it)?;
        ways.push(Way {
            obj: it,
            id,
            cls,
            path,
            pts,
            level,
            span,
        });
    }
    ways.sort_by(|a, b| a.id.cmp(&b.id));
    let mut ends = vec![];
    let mut eligible = vec![];
    let mut marked = vec![];
    for w in &ways {
        let a = w.pts[0];
        let b = *w.pts.last().unwrap();
        ends.push([a.0, a.1, b.0, b.1]);
        let steps = w.cls == "steps";
        marked.push(w.level != 0. && !steps || w.span.is_some());
        eligible.push(
            matches!(
                w.cls.as_str(),
                "steps" | "footway" | "corridor" | "pedestrian" | "path"
            ) && (steps || w.level != 0.),
        );
    }
    let ids = crate::stationgeom::station_link_chains(
        py,
        rows(py, ends).readonly(),
        eligible.into_pyarray(py).readonly(),
        marked.into_pyarray(py).readonly(),
        2.5,
    )?;
    let mut chains: BTreeMap<i64, Vec<usize>> = BTreeMap::new();
    for (i, &k) in ids.readonly().as_slice()?.iter().enumerate() {
        if k >= 0 {
            chains.entry(k).or_default().push(i);
        }
    }
    let mut groups: Vec<_> = chains.into_values().collect();
    groups.sort_by_key(|g| g[0]);
    let mut used = BTreeSet::new();
    let out = PyList::empty(py);
    let crossings = |w: &Way| -> PyResult<Vec<(usize, f64)>> {
        rs.call_method1("_crossings", (&w.path, tracks))?.extract()
    };
    for members in groups {
        let mains: Vec<_> = members
            .iter()
            .map(|&i| &ways[i])
            .filter(|w| w.cls != "steps")
            .collect();
        if mains.is_empty() {
            continue;
        }
        let mut main = mains[0];
        let mut longest = f64::NEG_INFINITY;
        let mut level = 0.0f64;
        for w in &mains {
            let l = crate::levels::np_sum(
                &w.pts
                    .windows(2)
                    .map(|a| (a[1] - a[0]).norm())
                    .collect::<Vec<_>>(),
            );
            if l > longest {
                main = w;
                longest = l;
            }
        }
        for &i in &members {
            if ways[i].level.abs() > level.abs() {
                level = ways[i].level;
            }
        }
        let kind = if main.span == Some("bridge") || main.span.is_none() && level > 0. {
            "over"
        } else {
            "under"
        };
        let mut cross = crossings(main)?;
        if cross.is_empty() {
            for w in mains {
                let c = crossings(w)?;
                if c.len() > cross.len() {
                    cross = c;
                    main = w;
                }
            }
        }
        if cross.is_empty() {
            continue;
        }
        let steps: Vec<_> = members
            .iter()
            .filter(|&&i| ways[i].cls == "steps")
            .map(|&i| &ways[i].path)
            .collect();
        let ids: Vec<_> = members.iter().map(|&i| &ways[i].id).collect();
        used.extend(ids.iter().map(|id| (*id).clone()));
        let covered = tag(&main.obj, "covered")?.as_deref() == Some("yes")
            || tag(&main.obj, "tunnel")?.as_deref() == Some("building_passage")
            || main.cls == "corridor";
        let ln = rs.call_method1(
            "Link",
            (
                kind,
                &main.path,
                steps,
                level,
                ids,
                covered,
                width(&main.obj, &rs)?,
            ),
        )?;
        ln.setattr("crosses", cross)?;
        out.append(ln)?;
    }
    let rings = platforms
        .iter()
        .map(|p| points(p.getattr("ring")?.extract()?))
        .collect::<PyResult<Vec<_>>>()?;
    for w in &ways {
        if used.contains(&w.id)
            || w.level != 0.
            || w.span.is_some()
            || !matches!(
                w.cls.as_str(),
                "footway" | "path" | "pedestrian" | "cycleway"
            )
        {
            continue;
        }
        let cross = crossings(w)?;
        if cross.is_empty() || all_inside(w, &cross, &rings) {
            continue;
        }
        let ln = rs.call_method1(
            "Link",
            (
                "level",
                &w.path,
                PyList::empty(py),
                0.0,
                vec![&w.id],
                false,
                width(&w.obj, &rs)?,
            ),
        )?;
        ln.setattr("crosses", cross)?;
        out.append(ln)?;
    }
    Ok(out)
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rail_span_kind, m)?)?;
    m.add_function(wrap_pyfunction!(rail_links, m)?)
}
