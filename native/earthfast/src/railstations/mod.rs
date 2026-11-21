//! Station query and layout. Map loading and its per-frame LRU remain with the worker.
//! Groups are insertion ordered. A point joins the first group whose first seed is near it.
mod canopies;
mod layout;
mod links;
use crate::busstation::{array, points, Point};
use crate::roadnet::pyfloat::{np_round, py_round};
use numpy::IntoPyArray;
use pyo3::{
    prelude::*,
    types::{PyDict, PyList, PyTuple},
};
type Obj<'py> = Bound<'py, PyAny>;
type List<'py> = Bound<'py, PyList>;

fn point(v: &Obj) -> PyResult<Point> {
    Ok(Point(v.get_item(0)?.extract()?, v.get_item(1)?.extract()?))
}

#[pyfunction]
fn rail_station_groups<'py>(
    py: Python<'py>,
    md: &Obj<'py>,
    frame: &Obj<'py>,
    bbox: [f64; 4],
    shared: bool,
    group: &Obj<'py>,
) -> PyResult<List<'py>> {
    let [x0, y0, x1, y1] = bbox;
    let ll = frame.call_method1(
        "enu_to_geodetic",
        (
            vec![x0, x0, x1, x1].into_pyarray(py),
            vec![y0, y1, y0, y1].into_pyarray(py),
            vec![0.; 4].into_pyarray(py),
        ),
    )?;
    let cells = py.import("mapcells")?;
    let kw = PyDict::new(py);
    kw.set_item("pad_lat", 0.005)?;
    kw.set_item("pad_lon", 0.005)?;
    kw.set_item("max_cells", 64)?;
    let keys = cells.call_method(
        "cells_for_points",
        (ll.get_item(0)?, ll.get_item(1)?),
        Some(&kw),
    )?;
    let cds = cells.call_method1("load_cells", (md, keys, ("infrastructure",)))?;
    let mut seeds = Vec::new();
    for cd in cds.try_iter()? {
        let feats = cd?
            .getattr("layers")?
            .call_method1("get", ("infrastructure",))?;
        if feats.is_none() || feats.len()? == 0 {
            continue;
        }
        let cls = feats.getattr("attrs")?.call_method1("get", ("class",))?;
        if cls.is_none() {
            continue;
        }
        for (i, c) in cls.try_iter()?.enumerate() {
            let c = c?;
            if !c.eq("railway_station")? && !c.eq("railway_halt")? {
                continue;
            }
            let parts = feats.call_method1("parts", (i,))?;
            if parts.len()? == 0 {
                continue;
            }
            let rings = parts.get_item(0)?;
            if rings.len()? == 0 {
                continue;
            }
            let ll = rings.get_item(0)?;
            if ll.len()? == 0 {
                continue;
            }
            let ll = py
                .import("numpy")?
                .call_method1("asarray", (ll, "float64"))?;
            let p = points(ll.extract()?)?;
            let mean = |f: fn(&Point) -> f64| {
                crate::levels::np_sum(&p.iter().map(f).collect::<Vec<_>>()) / p.len() as f64
            };
            seeds.push((py_round(mean(|p| p.0), 5), py_round(mean(|p| p.1), 5)));
        }
    }
    seeds.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.1.total_cmp(&b.1)));
    seeds.dedup();
    let mut groups: Vec<Vec<(f64, f64)>> = Vec::new();
    for s in seeds {
        if let Some(g) = groups
            .iter_mut()
            .find(|g| (g[0].0 - s.0).abs() < 0.006 && (g[0].1 - s.1).abs() < 0.0045)
        {
            g.push(s)
        } else {
            groups.push(vec![s]);
        }
    }
    let found = PyList::empty(py);
    for g in groups {
        let gf = if shared {
            py.import("busstations")?
                .call_method1("group_frame", (&g,))?
        } else {
            frame.clone()
        };
        let sts = group.call1((md, &gf, PyTuple::new(py, g)?))?;
        for st in sts.try_iter()? {
            let st = st?;
            if shared {
                st.setattr("group_frame", &gf)?;
            }
            found.append((st, &gf))?;
        }
    }
    Ok(found)
}

#[pyfunction]
fn rail_stations_near<'py>(
    py: Python<'py>,
    found: Vec<(Obj<'py>, Obj<'py>)>,
    frame: &Obj<'py>,
    bbox: [f64; 4],
) -> PyResult<List<'py>> {
    let transform = py.import("busstations")?.getattr("to_frame")?;
    let mut seen = vec![];
    let out = PyList::empty(py);
    for (st, gf) in found {
        let hub = st.getattr("hub")?;
        let q = point(&transform.call1((&gf, frame, vec![&hub]))?.get_item(0)?)?;
        let key = (np_round(q.0, 1), np_round(q.1, 1));
        if seen.contains(&key) {
            continue;
        }
        seen.push(key);
        let mut pts = vec![];
        for pl in st.getattr("platforms")?.try_iter()? {
            pts.extend(points(pl?.getattr("ring")?.extract()?)?);
        }
        pts.push(point(&hub)?);
        let corners = transform.call1((&gf, frame, array(py, &pts)))?;
        let pts = points(corners.extract()?)?;
        let mut lo = Point(f64::INFINITY, f64::INFINITY);
        let mut hi = Point(f64::NEG_INFINITY, f64::NEG_INFINITY);
        let mut valid = true;
        for p in pts {
            valid &= !p.0.is_nan() && !p.1.is_nan();
            lo = Point(lo.0.min(p.0), lo.1.min(p.1));
            hi = Point(hi.0.max(p.0), hi.1.max(p.1));
        }
        if valid
            && lo.0 - 420. <= bbox[2]
            && hi.0 + 420. >= bbox[0]
            && lo.1 - 420. <= bbox[3]
            && hi.1 + 420. >= bbox[1]
        {
            out.append(st)?;
        }
    }
    Ok(out)
}
pub(crate) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rail_station_groups, m)?)?;
    m.add_function(wrap_pyfunction!(rail_stations_near, m)?)?;
    links::register(m)?;
    canopies::register(m)?;
    layout::register(m)
}
