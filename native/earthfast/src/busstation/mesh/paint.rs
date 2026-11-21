//! Visible paint chunks in lane order. Arrow artwork and box clipping are the road renderer's.
use super::*;
type Box4 = (f64, f64, f64, f64);
fn visible(b: Box4, p: Point) -> bool {
    b.0 <= p.0 && p.0 < b.2 && b.1 <= p.1 && p.1 < b.3
}
fn mark<'p>(py: Python<'p>, material: &str, width: f64, r: &[Point]) -> PyResult<Bound<'p, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("m", material)?;
    d.set_item("w", width)?;
    d.set_item("tl", 6.4)?;
    d.set_item("p", 2)?;
    d.set_item(
        "n",
        r.iter()
            .map(|p| vec![py_round(p.0, 3), py_round(p.1, 3), 0.0])
            .collect::<Vec<_>>(),
    )?;
    Ok(d)
}
#[pyfunction]
fn bus_lane_marks<'p>(
    py: Python<'p>,
    box_: Box4,
    paths: Vec<(XY<'p>, f64)>,
    mouths: Vec<(f64, f64)>,
    bays: Vec<XY<'p>>,
) -> PyResult<Vec<Bound<'p, PyDict>>> {
    let mut out = Vec::new();
    let mouths = pairs(mouths);
    let bays = rings(bays)?;
    let markings = py.import("markings")?;
    for l in lanes(paths)? {
        let path = Path::new(&l.xy);
        let total = path.total;
        if total < 18.0 {
            continue;
        }
        let k = (crate::py_floordiv(total - 16.0, 25.0) as usize + 1).max(1);
        for i in 0..k {
            let (c, t) = path.at(8.0 + (total - 16.0) * (i as f64 + 0.5) / k as f64);
            if !visible(box_, c) || bays.iter().any(|r| inside(r, c)) {
                continue;
            }
            let quad = markings.call_method1(
                "arrow_quad",
                (vec![c.0, c.1, 0.0].into_pyarray(py), t.arr(py), "through"),
            )?;
            let corners = quad.get_item(0)?;
            let corners = corners.extract::<XY>()?;
            let d = PyDict::new(py);
            d.set_item("m", markings.getattr("ARROW_MATERIAL")?)?;
            d.set_item("w", 0.0)?;
            d.set_item("tl", 4.0)?;
            d.set_item("p", 6)?;
            d.set_item("layer", "mark")?;
            d.set_item(
                "poly",
                corners
                    .as_array()
                    .rows()
                    .into_iter()
                    .map(|r| r.iter().map(|&v| py_round(v, 3)).collect::<Vec<_>>())
                    .collect::<Vec<_>>(),
            )?;
            d.set_item("uvq", quad.get_item(1)?.call_method0("tolist")?)?;
            d.set_item("n", PyList::empty(py))?;
            out.push(d);
        }
        let end = l.xy[l.xy.len() - 1];
        if mouths.iter().any(|m| crate::py_hypot(end.0 - m.0, end.1 - m.1) < 1.0) {
            let (c, t) = path.at((total - 2.5).max(0.0));
            if visible(box_, c) {
                let n = t.left() * l.half * 0.9;
                out.push(mark(py, "line_white", 0.45, &[c - n, c + n])?)
            }
        }
    }
    Ok(out)
}
#[pyfunction]
fn bus_cross_marks<'p>(
    py: Python<'p>,
    box_: Box4,
    crossings: Vec<(Bound<'p, PyAny>, Bound<'p, PyAny>, f64)>,
) -> PyResult<Vec<Bound<'p, PyDict>>> {
    let mut out = Vec::new();
    for (c, t, h) in crossings_in(crossings)? {
        if !visible(box_, c) {
            continue;
        }
        let k = (2.0 * h).floor().max(1.0) as usize;
        let start = -(k as f64) / 2.0 + 0.5;
        for i in 0..k {
            let mid = c + t.left() * (start + i as f64);
            out.push(mark(
                py,
                "line_white",
                0.5,
                &[mid - t * 3.0 / 2.0, mid + t * 3.0 / 2.0],
            )?)
        }
    }
    Ok(out)
}
#[pyfunction]
fn bus_edge_marks<'p>(py: Python<'p>, box_: Box4, runs: Vec<XY<'p>>) -> PyResult<Vec<Bound<'p, PyDict>>> {
    let mut out = Vec::new();
    for r in rings(runs)? {
        if r.is_empty() || !visible(box_, r[r.len() / 2]) {
            continue;
        }
        let mut pts: Vec<_> = r.iter().copied().step_by(3).collect();
        let a = *pts.last().unwrap();
        let b = *r.last().unwrap();
        let close = |x: f64, y: f64| x == y || (x - y).abs() <= 1e-8 + 1e-5 * y.abs();
        if !(close(a.0, b.0) && close(a.1, b.1)) {
            pts.push(b)
        }
        out.push(mark(py, "line_white", 0.12, &pts)?);
    }
    Ok(out)
}
#[pyfunction]
fn bus_zigzags<'p>(py: Python<'p>, box_: Box4, bays: Vec<Bound<'p, PyAny>>) -> PyResult<Vec<Bound<'p, PyDict>>> {
    let mut out = Vec::new();
    for b in bays {
        if !b.getattr("label")?.is_truthy()? && !b.getattr("synth")?.is_truthy()? {
            continue;
        }
        let c = super::super::layout::point(&b.getattr("c")?)?;
        if !visible(box_, c) {
            continue;
        }
        let n = super::super::layout::point(&b.getattr("n")?)?;
        let t = super::super::layout::point(&b.getattr("t")?)?;
        let w: f64 = b.getattr("width")?.extract()?;
        let len: f64 = b.getattr("length")?.extract()?;
        let base = c - n * (w / 2.0 - 0.4);
        let k = (len / 0.5).floor().max(2.0) as usize;
        let pts: Vec<_> = (0..=k)
            .map(|i| {
                base + t * ((i as f64 / k as f64 - 0.5) * (len - 1.0)) + n * if i % 2 == 0 { 0.22 } else { -0.22 }
            })
            .collect();
        out.push(mark(py, "line_yellow", 0.12, &pts)?);
    }
    Ok(out)
}
#[pyfunction]
fn bus_bay_lines(bays: Vec<Bound<'_, PyAny>>) -> PyResult<Vec<((f64, f64, f64), (f64, f64, f64))>> {
    let mut out = Vec::new();
    for b in bays {
        let corners = b.call_method0("corners")?;
        let point = |i| -> PyResult<_> {
            let p = super::super::layout::point(&corners.get_item(i)?)?;
            Ok((p.0, p.1, 0.0))
        };
        let (ns, ne, fs, fe) = (point(0)?, point(1)?, point(2)?, point(3)?);
        out.extend([(ns, fs), (fs, fe), (fe, ne)]);
    }
    Ok(out)
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(bus_lane_marks, m)?)?;
    m.add_function(wrap_pyfunction!(bus_cross_marks, m)?)?;
    m.add_function(wrap_pyfunction!(bus_edge_marks, m)?)?;
    m.add_function(wrap_pyfunction!(bus_zigzags, m)?)?;
    m.add_function(wrap_pyfunction!(bus_bay_lines, m)?)
}
