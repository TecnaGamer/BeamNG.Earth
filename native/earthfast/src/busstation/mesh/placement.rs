//! Select sign, ticket-kiosk and bollard positions from the same walk field as the kerbs.
use super::*;
type Spot = (f64, f64, f64, f64);
fn spot(p: Point, u: Point) -> Spot {
    (p.0, p.1, u.0, u.1)
}
#[pyfunction]
fn bus_board(hall: XY<'_>, benches: Vec<(f64, f64)>, sign: Option<Spot>) -> PyResult<Option<Spot>> {
    let hall = points(hall)?;
    let hall = open(&hall);
    let mut best = None;
    for p in pairs(benches) {
        if sign.is_some_and(|s| (s.0 - p.0).abs() < 0.01 && (s.1 - p.1).abs() < 0.01) {
            continue;
        }
        let d = distance(hall, p, true);
        if best.is_none_or(|(b, _)| d < b) {
            best = Some((d, p))
        }
    }
    Ok(best.filter(|(d, _)| *d <= 40.0).map(|(_, p)| {
        let u = p - centroid(hall);
        let len = u.norm();
        spot(p, if len > 1e-6 { u / len } else { Point(1.0, 0.0) })
    }))
}
#[pyfunction]
fn bus_facade(hall: XY<'_>, yards: Vec<XY<'_>>) -> PyResult<Option<(f64, f64, f64, f64, f64)>> {
    let h = points(hall)?;
    let hall = open(&h);
    if hall.len() < 3 {
        return Ok(None);
    }
    let ccw = signed(hall) >= 0.0;
    let yards: Vec<_> = rings(yards)?
        .iter()
        .filter(|r| r.len() >= 3)
        .map(|r| Sdf::new(r))
        .collect();
    let mut best = None;
    for (a, b) in edges(hall) {
        let len = (b - a).norm();
        if len < 6.0 {
            continue;
        }
        let t = (b - a) / len;
        let out = if ccw { Point(t.1, -t.0) } else { t.left() };
        let mid = (a + b) / 2.0;
        if yards.iter().any(|y| y.at(mid + out * 3.0) > 0.0) && best.is_none_or(|(_, _, _, _, d)| len > d) {
            let p = mid + out * 0.12;
            best = Some((p.0, p.1, -out.0, -out.1, len));
        }
    }
    Ok(best)
}
#[pyfunction]
fn bus_both_sides(sign: Spot, paths: Vec<XY<'_>>) -> PyResult<bool> {
    let p = Point(sign.0, sign.1);
    let u = Point(sign.2, sign.3);
    Ok(rings(paths)?
        .iter()
        .any(|r| r.len() >= 2 && (0..19).any(|i| distance(r, p + u * (3.0 + i as f64 * 1.5), false) < 3.0)))
}
#[pyfunction]
fn bus_sign(
    benches: Vec<(f64, f64)>,
    roofs: Vec<XY<'_>>,
    streets: Vec<XY<'_>>,
    paths: Vec<(XY<'_>, f64)>,
    mouths: Vec<(f64, f64)>,
    x: f64,
    y: f64,
    f: XY<'_>,
) -> PyResult<Option<Spot>> {
    let walk = Walk { x, y, f: f.as_array() };
    let paths = lanes(paths)?;
    let mouths = pairs(mouths);
    let roofs = rings(roofs)?;
    let streets = rings(streets)?;
    let benches = pairs(benches);
    let clear: Vec<_> = benches
        .iter()
        .copied()
        .filter(|&p| !roofs.iter().any(|r| inside(r, p) || distance(r, p, true) < 1.5))
        .collect();
    let mut best = None;
    for p in if clear.is_empty() { benches } else { clear } {
        let mut near = None;
        for r in &streets {
            for s in r.windows(2) {
                let (d, q) = foot(s[0], s[1], p);
                if near.is_none_or(|(b, _)| d < b) {
                    near = Some((d, q))
                }
            }
        }
        let Some((d, q)) = near else { continue };
        let u = if d > 1e-6 { (p - q) / d } else { Point(1.0, 0.0) };
        let mut nblocked = 0;
        let mut nout = 0;
        let mut middle = false;
        for (i, s) in [-1.6, -0.8, 0.0, 0.8, 1.6].into_iter().enumerate() {
            let q = p + u.left() * s;
            if blocked(&paths, &mouths, q, 0.3) {
                nblocked += 1;
                middle |= (1..4).contains(&i);
            }
            nout += (!walk.has(q)) as usize;
        }
        let key = (nblocked, nout, d);
        if !middle && best.is_none_or(|(k, _)| key < k) {
            best = Some((key, spot(p, u)))
        }
    }
    Ok(best.map(|(_, s)| s))
}
const DEFAULT_PLATFORM_WIDTH: f64 = 3.2;
type Platform<'p> = (String, XY<'p>, Bound<'p, PyAny>);

fn platform_width(tags: &Bound<'_, PyAny>) -> PyResult<f64> {
    let py = tags.py();
    let width = tags.call_method1("get", ("width", ""))?;
    match py.get_type::<pyo3::types::PyFloat>().call1((width,)) {
        Ok(value) => value.extract(),
        Err(err) if err.is_instance_of::<PyValueError>(py) => Ok(DEFAULT_PLATFORM_WIDTH),
        Err(err) => Err(err),
    }
}
fn tips(input: Vec<Platform<'_>>) -> PyResult<Vec<(Point, Point, f64)>> {
    let mut out = Vec::new();
    for (kind, xy, tags) in input {
        let r = points(xy)?;
        if kind == "line" && r.len() >= 2 {
            let width = platform_width(&tags)?;
            let width = if width == 0.0 { DEFAULT_PLATFORM_WIDTH } else { width };
            for (a, b) in [(r[0], r[1]), (r[r.len() - 1], r[r.len() - 2])] {
                let d = a - b;
                let len = d.norm();
                if len > 1e-6 {
                    out.push((a, d / len, width))
                }
            }
        } else if kind == "polygon" && open(&r).len() >= 3 {
            if let Some(b) = obb(open(&r)) {
                if b.u1 - b.u0 >= 2.0 * b.width() {
                    for (u, s) in [(b.u0, -1.0), (b.u1, 1.0)] {
                        out.push((b.t * u + b.t.left() * ((b.v0 + b.v1) / 2.0), b.t * s, b.width()));
                    }
                }
            }
        }
    }
    Ok(out)
}
#[pyfunction]
fn bus_platforms<'p>(py: Python<'p>, platforms: Vec<Platform<'p>>) -> PyResult<Vec<Array<'p>>> {
    let mut out = vec![];
    for (kind, xy, tags) in platforms {
        let r = points(xy)?;
        if kind == "polygon" && area(&r) >= 4.0 {
            out.push(array(py, &ccw(&r)))
        } else if kind == "line" {
            out.extend(
                seg_quads(&r, (platform_width(&tags)? / 2.0).clamp(1.0, 4.0))
                    .iter()
                    .map(|r| array(py, r)),
            )
        }
    }
    Ok(out)
}
#[pyfunction]
fn bus_bollards(
    x: f64,
    y: f64,
    f: XY<'_>,
    platforms: Vec<Platform<'_>>,
    paths: Vec<(XY<'_>, f64)>,
) -> PyResult<Vec<(f64, f64)>> {
    let walk = Walk { x, y, f: f.as_array() };
    let paths = lanes(paths)?;
    let mut out = Vec::new();
    if paths.is_empty() {
        return Ok(out);
    }
    for (tip, u, w) in tips(platforms)? {
        if w < 1.6 || !blocked(&paths, &[], tip + u * 2.0, 6.0) {
            continue;
        }
        let k = (crate::py_floordiv(w - 0.6, 1.1) as usize + 1).max(2);
        let span = (k - 1) as f64 * 1.1;
        for i in 0..k {
            let q = tip - u * 0.45 + u.left() * (-span / 2.0 + i as f64 * 1.1);
            if walk.has(q) && !blocked(&paths, &[], q, 0.2) {
                out.push(q.pair())
            }
        }
    }
    Ok(out)
}
pub(super) fn rectangle(c: Point, a: Point, b: Point) -> Ring {
    vec![c + a + b, c - a + b, c - a - b, c + a - b]
}
#[pyfunction]
fn bus_kiosk(
    x: f64,
    y: f64,
    f: XY<'_>,
    platforms: Vec<XY<'_>>,
    roofs: Vec<XY<'_>>,
    paths: Vec<(XY<'_>, f64)>,
    mouths: Vec<(f64, f64)>,
) -> PyResult<Option<Spot>> {
    let walk = Walk { x, y, f: f.as_array() };
    let paths = lanes(paths)?;
    let mouths = pairs(mouths);
    let roofs = rings(roofs)?;
    let platforms = rings(platforms)?;
    let mut best = None;
    for r in &platforms {
        let a = area(r);
        if best.is_none_or(|(s, _)| a > s) {
            best = Some((a, r))
        }
    }
    let Some(b) = best.and_then(|(_, r)| obb(r)) else {
        return Ok(None);
    };
    if b.width() >= 3.6 {
        for frac in [0.5, 0.3, 0.7] {
            let c = b.centre(frac);
            if roofs.iter().any(|r| inside(r, c)) {
                continue;
            }
            if rectangle(c, b.t * (3.2 / 2.0 + 0.6), b.t.left() * (2.6 / 2.0 + 0.6))
                .iter()
                .all(|&p| walk.has(p) && !blocked(&paths, &mouths, p, 0.3))
            {
                return Ok(Some(spot(c, b.t)));
            }
        }
    }
    Ok(None)
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(bus_board, m)?)?;
    m.add_function(wrap_pyfunction!(bus_facade, m)?)?;
    m.add_function(wrap_pyfunction!(bus_both_sides, m)?)?;
    m.add_function(wrap_pyfunction!(bus_sign, m)?)?;
    m.add_function(wrap_pyfunction!(bus_platforms, m)?)?;
    m.add_function(wrap_pyfunction!(bus_bollards, m)?)?;
    m.add_function(wrap_pyfunction!(bus_kiosk, m)?)
}
