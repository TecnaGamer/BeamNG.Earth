//! Map ground-way segments used by walkway panels and bridge pier placement.
use super::*;
type Lines = Vec<(Vec<Point>, f64)>;

fn nearby(py: Python, frame: &Obj, cells: &Obj, b: Bounds, walks: bool) -> PyResult<Lines> {
    let rd = py.import("roads")?;
    let geo = py.import("mapgeom")?;
    let widths = py.import("railways")?.getattr("WALK_CLASSES")?;
    let mut seen = BTreeSet::new();
    let mut out = vec![];
    for f in cells.try_iter()? {
        let f = f?;
        if f.is_none() || f.len()? == 0 {
            continue;
        }
        let attrs = f.getattr("attrs")?;
        if walks && (!attrs.contains("subtype")? || !attrs.contains("class")?) {
            continue;
        }
        let specs = if walks {
            None
        } else {
            Some(rd.call_method1("road_specs", (&f,))?)
        };
        let boxes = f.call_method1("boxes", (frame,))?;
        let ids = f.getattr("ids")?;
        for i in 0..f.len()? {
            let [x0, y0, x1, y1]: Bounds = boxes.get_item(i)?.extract()?;
            if x1 < b[0] || x0 > b[2] || y1 < b[1] || y0 > b[3] {
                continue;
            }
            let w = if let Some(specs) = &specs {
                let spec = specs.get_item(i)?;
                if spec.is_none() {
                    continue;
                }
                let walk = spec.getattr("walk")?;
                let extra = if rd.call_method1("has_sidewalks", (&spec,))?.is_truthy()?
                    && walk.is_truthy()?
                {
                    walk.extract()?
                } else {
                    0.
                };
                spec.getattr("width")?.extract::<f64>()? / 2. + extra
            } else {
                if !attrs.get_item("subtype")?.get_item(i)?.eq("road")? {
                    continue;
                }
                let w =
                    widths.call_method1("get", (attrs.get_item("class")?.get_item(i)?.str()?,))?;
                if w.is_none() {
                    continue;
                }
                w.extract()?
            };
            if !seen.insert(ids.get_item(i)?.str()?.extract::<String>()?) {
                continue;
            }
            for r in rd.call_method1("ground_parts", (&f, i))?.try_iter()? {
                let q = geo.call_method1("enu_xy", (&f, frame, r?))?;
                let x: Vec<f64> = q.get_item(0)?.extract()?;
                let y: Vec<f64> = q.get_item(1)?.extract()?;
                if x.len() >= 2 {
                    out.push((x.into_iter().zip(y).map(|(x, y)| Point(x, y)).collect(), w));
                }
            }
        }
    }
    Ok(out)
}
#[pyfunction]
fn rail_walk_ways<'p>(
    py: Python<'p>,
    frame: &Obj,
    cells: &Obj,
    bbox: Bounds,
    reach: f64,
) -> PyResult<List<'p>> {
    PyList::new(
        py,
        nearby(py, frame, cells, expanded(bbox, reach), true)?
            .into_iter()
            .map(|(p, w)| (array(py, &p), w)),
    )
}
#[pyfunction]
fn rail_pier_obstacles<'p>(
    py: Python<'p>,
    frame: &Obj,
    cells: &Obj,
    bbox: Bounds,
    reach: f64,
) -> PyResult<Obj<'p>> {
    let b = expanded(bbox, reach);
    let mut ls = nearby(py, frame, cells, b, false)?;
    ls.extend(
        nearby(py, frame, cells, b, true)?
            .into_iter()
            .map(|(p, w)| (p, w / 2.)),
    );
    let (mut a, mut b, mut hw) = (vec![], vec![], vec![]);
    for (p, w) in ls {
        for q in p.windows(2) {
            a.push(q[0]);
            b.push(q[1]);
            hw.push(w);
        }
    }
    Ok((array(py, &a), array(py, &b), hw.into_pyarray(py))
        .into_pyobject(py)?
        .into_any())
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rail_walk_ways, m)?)?;
    m.add_function(wrap_pyfunction!(rail_pier_obstacles, m)?)
}
