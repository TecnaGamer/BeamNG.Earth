//! Decode the rail, road and mapped-node columns just for the candidate box.
use super::*;
use std::collections::BTreeSet;
type Obj<'p> = Bound<'p, PyAny>;

#[pyfunction]
fn catenary_electrified<'p>(py: Python<'p>, cells: &Obj<'p>) -> PyResult<Bound<'p, PyDict>> {
    let out = PyDict::new(py);
    for f in cells.try_iter()? {
        let f = f?;
        if f.is_none() || f.len()? == 0 {
            continue;
        }
        let tags = f
            .getattr("attrs")?
            .call_method1("get", ("osm_electrified",))?;
        if tags.is_none() {
            continue;
        }
        for i in 0..f.len()? {
            let v = tags.get_item(i)?;
            if v.is_truthy()? {
                let text: String = v.str()?.extract()?;
                if matches!(
                    text.split(';').next().unwrap_or("").trim(),
                    "yes" | "contact_line"
                ) {
                    out.set_item(f.getattr("ids")?.get_item(i)?.str()?, true)?;
                }
            }
        }
    }
    Ok(out)
}
#[pyfunction]
fn catenary_mapped<'p>(
    py: Python<'p>,
    frame: &Obj<'p>,
    cells: &Obj<'p>,
) -> PyResult<Bound<'p, numpy::PyArray2<f64>>> {
    let (mut lat, mut lon) = (vec![], vec![]);
    for f in cells.try_iter()? {
        let f = f?;
        if f.is_none() {
            continue;
        }
        let nodes = f
            .getattr("__dict__")?
            .call_method1("get", ("_rail_nodes", pyo3::types::PyTuple::empty(py)))?;
        for n in nodes.try_iter()? {
            let n = n?;
            let p = n.get_item(2)?.call_method1("get", ("power",))?;
            if p.eq("catenary_mast")? || p.eq("catenary_portal")? {
                lon.push(n.get_item(0)?.extract::<f64>()?);
                lat.push(n.get_item(1)?.extract::<f64>()?);
            }
        }
    }
    if lat.is_empty() {
        return Ok(array(py, &[]));
    }
    let z = vec![0.; lat.len()];
    let en = frame.call_method1(
        "geodetic_to_enu",
        (
            lat.into_pyarray(py),
            lon.into_pyarray(py),
            z.into_pyarray(py),
        ),
    )?;
    let x: Vec<f64> = en.get_item(0)?.extract()?;
    let y: Vec<f64> = en.get_item(1)?.extract()?;
    Ok(crate::busstation::rows(
        py,
        x.into_iter().zip(y).map(|(x, y)| [x, y]).collect(),
    ))
}
#[pyfunction]
fn catenary_layout<'p>(
    py: Python<'p>,
    frame: &Obj<'p>,
    cells: &Obj<'p>,
    bbox: &Obj<'p>,
    ground: Obj<'p>,
    rings: &Obj<'p>,
) -> PyResult<Bound<'p, PyList>> {
    let elec = catenary_electrified(py, cells)?;
    if elec.is_empty() {
        return Ok(PyList::empty(py));
    }
    let mut parts = vec![];
    let rails = py
        .import("railways")?
        .call_method1("parts", (frame, cells, bbox, 130.))?;
    for row in rails.try_iter()? {
        let row = row?;
        if !row.get_item(2)?.eq("ground")? {
            continue;
        }
        let fid = row.get_item(0)?.str()?;
        let xy = row.get_item(3)?;
        if elec.contains(&fid)? && xy.len()? >= 2 {
            parts.push((fid.extract()?, xy.extract()?));
        }
    }
    if parts.is_empty() {
        return Ok(PyList::empty(py));
    }
    let rd = py.import("roads")?;
    let mg = py.import("mapgeom")?;
    let [x0, y0, x1, y1] = bbox.extract::<[f64; 4]>()?;
    let (mut roads, mut seen) = (vec![], BTreeSet::new());
    for f in cells.try_iter()? {
        let f = f?;
        if f.is_none() || f.len()? == 0 {
            continue;
        }
        let specs = rd.call_method1("road_specs", (&f,))?;
        let boxes = f.call_method1("boxes", (frame,))?;
        let ids = f.getattr("ids")?;
        for (i, spec) in specs.try_iter()?.enumerate() {
            let spec = spec?;
            let id: String = ids.get_item(i)?.str()?.extract()?;
            if spec.is_none() || seen.contains(&id) {
                continue;
            }
            let [a, b, c, d] = boxes.get_item(i)?.extract::<[f64; 4]>()?;
            if !(a <= x1 + 130. && c >= x0 - 130. && b <= y1 + 130. && d >= y0 - 130.) {
                continue;
            }
            seen.insert(id);
            for ring in rd.call_method1("ground_parts", (&f, i))?.try_iter()? {
                let q = mg.call_method1("enu_xy", (&f, frame, ring?))?;
                let x: Vec<f64> = q.get_item(0)?.extract()?;
                if x.len() < 2 {
                    continue;
                }
                let y: Vec<f64> = q.get_item(1)?.extract()?;
                let xy = crate::busstation::rows(
                    py,
                    x.into_iter().zip(y).map(|(x, y)| [x, y]).collect(),
                );
                roads.push((
                    xy.into_any().extract()?,
                    spec.getattr("width")?.extract::<f64>()? / 2.,
                ));
            }
        }
    }
    let mapped = catenary_mapped(py, frame, cells)?.into_any().extract()?;
    catenary_masts(py, parts, roads, rings.extract()?, mapped, ground)
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(catenary_electrified, m)?)?;
    m.add_function(wrap_pyfunction!(catenary_mapped, m)?)?;
    m.add_function(wrap_pyfunction!(catenary_layout, m)?)
}
