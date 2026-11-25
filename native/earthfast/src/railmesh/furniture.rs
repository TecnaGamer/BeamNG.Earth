//! Pedestrian panels, warning signs, and buffers on ground tracks.
use super::*;
use crate::{dm::Dm, roadnet::pyfloat::py_round};
use std::cmp::Ordering;
const HEAD: f64 = 0.3 - 0.08 + 0.2 + 0.16;
fn point(a: &Obj) -> PyResult<Point> {
    Ok(Point(a.get_item(0)?.extract()?, a.get_item(1)?.extract()?))
}
fn line(a: &Obj) -> PyResult<Vec<Point>> {
    points(a.extract()?)
}
fn direction(v: Point) -> Point {
    v / v.norm().max(1e-9)
}
fn nearest(p: Point, line: &[Point]) -> (usize, f64) {
    line.iter()
        .enumerate()
        .map(|(i, q)| (i, (*q - p).norm()))
        .min_by(|a, b| a.1.total_cmp(&b.1))
        .unwrap_or((0, f64::INFINITY))
}
fn dense(py: Python, xy: &Obj, step: f64) -> PyResult<Vec<Point>> {
    line(&py.import("roads")?.call_method1("densify", (xy, step))?)
}
fn leaves(py: Python, way: &[Point], pt: Point, track: &Obj) -> PyResult<bool> {
    if way.len() < 2 {
        return Err(PyValueError::new_err("empty path"));
    }
    let (_, s) = line_axes(way);
    let mut best = f64::INFINITY;
    let mut at = 0.;
    for (j, w) in way.windows(2).enumerate() {
        let d = w[1] - w[0];
        let t = ((pt - w[0]).dot(d) / d.dot(d).max(1e-12)).clamp(0., 1.);
        let v = (w[0] + d * t - pt).norm();
        if v < best {
            best = v;
            at = s[j] + t * (s[j + 1] - s[j]);
        }
    }
    let xy = dense(py, track, 1.)?;
    let a = [(at - 12.).max(0.), (at + 12.).min(*s.last().unwrap())];
    let x = crate::dm::interp(
        &a,
        &s,
        &way.iter().map(|v| v.0).collect::<Vec<_>>(),
        None,
        None,
    );
    let y = crate::dm::interp(
        &a,
        &s,
        &way.iter().map(|v| v.1).collect::<Vec<_>>(),
        None,
        None,
    );
    Ok((0..2).all(|i| nearest(Point(x[i], y[i]), &xy).1 >= 5.))
}
#[pyfunction]
fn rail_leaves_track(py: Python, way: &Obj, pt: &Obj, track: &Obj) -> PyResult<bool> {
    leaves(py, &line(way)?, point(pt)?, track)
}
struct Way<'p> {
    xy: Obj<'p>,
    p: Vec<Point>,
    width: f64,
}
fn ways<'p>(ls: &Obj<'p>) -> PyResult<Vec<Way<'p>>> {
    ls.try_iter()?
        .map(|v| {
            let v = v?;
            let xy = v.get_item(0)?;
            Ok(Way {
                p: line(&xy)?,
                xy,
                width: v.get_item(1)?.extract()?,
            })
        })
        .collect()
}
#[derive(Clone, Copy)]
struct Crossing {
    p: Point,
    arc: f64,
    part: usize,
    t: Point,
    f: Point,
    w: f64,
}
impl Crossing {
    fn row<'p>(&self, py: Python<'p>) -> PyResult<Obj<'p>> {
        Ok((
            self.p.arr(py),
            self.arc,
            self.part,
            self.t.arr(py),
            self.f.arr(py),
            self.w,
        )
            .into_pyobject(py)?
            .into_any())
    }
    fn read(v: &Obj) -> PyResult<Self> {
        Ok(Self {
            p: point(&v.get_item(0)?)?,
            arc: v.get_item(1)?.extract()?,
            part: v.get_item(2)?.extract()?,
            t: point(&v.get_item(3)?)?,
            f: point(&v.get_item(4)?)?,
            w: v.get_item(5)?.extract()?,
        })
    }
    fn bed(&self, finals: &Obj) -> PyResult<f64> {
        let z = finals.get_item(self.part)?;
        let i = ((self.arc / 2.).round_ties_even() as isize).min(z.len()? as isize - 1);
        z.get_item(i)?.extract()
    }
}
fn sine(t: Point, f: Point) -> f64 {
    (t.0 * f.1 - t.1 * f.0).abs()
}
fn strict(py: Python) -> PyResult<bool> {
    py.import("railways")?
        .getattr("WALK_STRICT_ON")?
        .is_truthy()
}
#[pyfunction]
fn rail_walk_crossings<'p>(
    py: Python<'p>,
    rails: &Obj<'p>,
    paths: &Obj<'p>,
    nodes: &Obj<'p>,
    carriage: &Obj<'p>,
    ex: Bounds,
) -> PyResult<Vec<Obj<'p>>> {
    let ways = ways(paths)?;
    let carriage = self::ways(carriage)?;
    let on_road = |p| carriage.iter().any(|w| nearest(p, &w.p).1 < w.width + 2.);
    let strict = strict(py)?;
    let roads = py.import("roads")?;
    let mut out = Vec::new();
    let mut tracks = vec![];
    for (part, r) in rails.try_iter()?.enumerate() {
        let r = r?;
        let xy = r.get_item(3)?;
        let pts = line(&xy)?;
        if !r.get_item(2)?.eq("ground")? || r.get_item(1)?.eq("tram")? || pts.len() < 2 {
            continue;
        }
        let (_, s) = line_axes(&pts);
        let bounds = expanded(
            pts.iter().fold(
                [
                    f64::INFINITY,
                    f64::INFINITY,
                    f64::NEG_INFINITY,
                    f64::NEG_INFINITY,
                ],
                |[a, b, c, d], p| [a.min(p.0), b.min(p.1), c.max(p.0), d.max(p.1)],
            ),
            8.,
        );
        for way in &ways {
            if !bbox_hits(&way.xy, bounds)? {
                continue;
            }
            let hits = roads.call_method1("_crossings", (vec![&xy], vec![&way.xy]))?;
            for h in hits.try_iter()? {
                let h = h?;
                let p = point(&h.get_item(2)?)?;
                if !contains(ex, p) {
                    continue;
                }
                let arc = h.get_item(1)?.extract()?;
                let k = s.partition_point(|&v| v < arc).clamp(1, s.len() - 1);
                let t = direction(pts[k] - pts[k - 1]);
                let (j, _) = way
                    .p
                    .windows(2)
                    .enumerate()
                    .map(|(j, w)| (j, ((w[0] + w[1]) / 2. - p).norm()))
                    .min_by(|a, b| a.1.total_cmp(&b.1))
                    .ok_or_else(|| PyValueError::new_err("empty way"))?;
                let f = direction(way.p[j + 1] - way.p[j]);
                if sine(t, f) < if strict { 0.6 } else { 0.35 }
                    || on_road(p)
                    || strict && !leaves(py, &way.p, p, &xy)?
                {
                    continue;
                }
                out.push(Crossing {
                    p,
                    arc,
                    part,
                    t,
                    f,
                    w: way.width,
                });
            }
        }
        tracks.push((part, pts, s));
    }
    for n in nodes.try_iter()? {
        let p = point(&n?)?;
        if !contains(ex, p)
            || out
                .iter()
                .any(|c| crate::py_hypot(p.0 - c.p.0, p.1 - c.p.1) < 6.)
        {
            continue;
        }
        let best = tracks
            .iter()
            .map(|r| (r, nearest(p, &r.1)))
            .filter(|(_, (_, d))| *d < 2.5)
            .min_by(|a, b| a.1 .1.total_cmp(&b.1 .1));
        if let Some(((part, xy, s), (k, _))) = best {
            let p = xy[k];
            if on_road(p) {
                continue;
            }
            let t = direction(xy[(k + 1).min(xy.len() - 1)] - xy[k.saturating_sub(1)]);
            out.push(Crossing {
                p,
                arc: s[k],
                part: *part,
                t,
                f: t.left(),
                w: 2.,
            });
        }
    }
    out.iter().map(|c| c.row(py)).collect()
}
fn groups(crossings: &Obj) -> PyResult<Vec<Vec<Crossing>>> {
    let mut cs = crossings
        .try_iter()?
        .map(|c| Crossing::read(&c?))
        .collect::<PyResult<Vec<_>>>()?;
    let cmp = |a: f64, b: f64| a.partial_cmp(&b).unwrap_or(Ordering::Equal);
    cs.sort_by(|a, b| {
        cmp(py_round(a.f.0, 2), py_round(b.f.0, 2))
            .then_with(|| cmp(py_round(a.f.1, 2), py_round(b.f.1, 2)))
            .then_with(|| cmp(a.p.0, b.p.0))
            .then_with(|| cmp(a.p.1, b.p.1))
    });
    let mut groups: Vec<Vec<Crossing>> = vec![];
    for c in cs {
        if let Some(group) = groups.iter_mut().find(|g| {
            let h = g.last().unwrap();
            h.f.dot(c.f).abs() > 0.97
                && (h.w - c.w).abs() < 0.01
                && g.iter().any(|q| (q.p - c.p).norm() < 14.)
        }) {
            group.push(c);
        } else {
            groups.push(vec![c]);
        }
    }
    for g in &mut groups {
        let f = g[0].f;
        g.sort_by(|a, b| cmp(a.p.dot(f), b.p.dot(f)));
    }
    Ok(groups)
}
#[pyfunction]
fn rail_walk_groups<'p>(py: Python<'p>, c: &Obj<'p>) -> PyResult<Vec<Vec<Obj<'p>>>> {
    groups(c)?
        .into_iter()
        .map(|g| g.iter().map(|c| c.row(py)).collect())
        .collect()
}
#[pyfunction]
fn rail_walk_panels<'p>(
    py: Python<'p>,
    crossings: &Obj<'p>,
    finals: &Obj<'p>,
) -> PyResult<List<'p>> {
    let strict = strict(py)?;
    let out = PyList::empty(py);
    let panel = |p: Point, t: Point, n: Point, b: f64, h: f64, hx: f64, hy: f64| {
        out.append((
            (
                array(py, &[p]),
                array(py, &[t]),
                array(py, &[n]),
                vec![b].into_pyarray(py),
                vec![h].into_pyarray(py),
            ),
            hx,
            hy,
        ))
    };
    let half = 1.435 / 2.;
    let edge = half + 0.07 + 2. * 0.45;
    for g in groups(crossings)? {
        for c in &g {
            let z = c.bed(finals)?;
            let n = c.t.left();
            let hx = (if strict { 4.0f64.max(c.w) } else { 8. })
                .min((c.w / 2.) / sine(c.t, c.f).max(0.35) + 0.2);
            for (off, hy) in [
                (0., half - 0.07),
                (half + 0.07 + 0.45, 0.45),
                (-(half + 0.07 + 0.45), 0.45),
            ] {
                panel(
                    c.p + n * off,
                    c.t,
                    n,
                    z + 0.3 - 0.15,
                    z + HEAD - 0.012,
                    hx,
                    hy,
                )?;
            }
        }
        for w in g.windows(2) {
            let (a, b) = (w[0], w[1]);
            let f = a.f;
            let d = (b.p - a.p).dot(f);
            let g0 = edge / sine(a.t, f).max(0.35);
            let gap = d - g0 - edge / sine(b.t, f).max(0.35);
            if gap < 0.05 || d > 14. {
                continue;
            }
            let (z0, z1) = (a.bed(finals)? + HEAD - 0.012, b.bed(finals)? + HEAD - 0.012);
            panel(
                a.p + f * (g0 + gap / 2.),
                f.left(),
                f,
                z0.min(z1) - 0.45,
                (z0 + z1) / 2.,
                a.w / 2. + 0.2,
                gap / 2. + 0.02,
            )?;
        }
    }
    Ok(out)
}
fn prop<'p>(
    py: Python<'p>,
    name: &str,
    p: Point,
    z: f64,
    side: Point,
    height: f64,
) -> PyResult<List<'p>> {
    let r = PyList::empty(py);
    r.append(name)?;
    let a = side.1.datan2(side.0).to_degrees();
    for v in [
        py_round(p.0, 2),
        py_round(p.1, 2),
        py_round(z, 2),
        py_round(if a < 0. { a + 360. } else { a + 0. }, 1),
        1.,
        height,
        0.,
    ] {
        r.append(v)?;
    }
    Ok(r)
}
#[pyfunction]
fn rail_walk_signs<'p>(
    py: Python<'p>,
    crossings: &Obj<'p>,
    ground: &Obj<'p>,
    finals: &Obj<'p>,
    rails: &Obj<'p>,
    streets: &Obj<'p>,
) -> PyResult<List<'p>> {
    let streets = streets
        .try_iter()?
        .map(|q| point(&q?))
        .collect::<PyResult<Vec<_>>>()?;
    let mut track = vec![];
    if !rails.is_none() {
        for r in rails.try_iter()? {
            let xy = r?.get_item(3)?;
            if xy.len()? >= 2 {
                track.push(xy);
            }
        }
    }
    let mut pts = vec![];
    let mut meta = vec![];
    let mut dense_lines = vec![];
    for g in groups(crossings)? {
        if streets
            .iter()
            .any(|q| g.iter().any(|c| (c.p - *q).norm() < 10.))
        {
            continue;
        }
        for (sg, c) in [(1., g[0]), (-1., *g.last().unwrap())] {
            let sin = sine(c.t, c.f).max(0.35);
            let before = 6.0f64.min((1.8 + 1.5 * 0.3 + 1.) / sin);
            let floor = if finals.is_none() {
                f64::NEG_INFINITY
            } else {
                c.bed(finals)? + 0.3 - (before * sin - 1.8).max(0.) / 1.5
            };
            let a = c.f * sg;
            let side = Point(a.1, -a.0);
            let q = c.p - a * before + side * (c.w / 2. + 0.5);
            if dense_lines.is_empty() {
                for xy in &track {
                    dense_lines.push(dense(py, xy, 1.)?);
                }
            }
            if dense_lines.iter().any(|v| nearest(q, v).1 < 2.6) {
                continue;
            }
            pts.push(q);
            meta.push((floor, side));
        }
    }
    let out = PyList::empty(py);
    if !pts.is_empty() {
        for ((q, z), (floor, side)) in pts.iter().zip(heights(py, ground, &pts)?).zip(meta) {
            out.append(prop(py, "rrwalk", *q, z.max(floor), side, 2.52)?)?;
        }
    }
    Ok(out)
}
#[pyfunction]
fn rail_buffer_rows<'p>(py: Python<'p>, ends: &Obj<'p>) -> PyResult<List<'p>> {
    let out = PyList::empty(py);
    for e in ends.try_iter()? {
        let e = e?;
        let p = point(&e.get_item(0)?)?;
        let d = point(&e.get_item(1)?)?;
        out.append(prop(
            py,
            "rrbuffer",
            p - d,
            e.get_item(2)?.extract()?,
            Point(d.1, -d.0),
            1.8,
        )?)?;
    }
    Ok(out)
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rail_dead_ends, m)?)?;
    m.add_function(wrap_pyfunction!(rail_leaves_track, m)?)?;
    m.add_function(wrap_pyfunction!(rail_walk_crossings, m)?)?;
    m.add_function(wrap_pyfunction!(rail_walk_groups, m)?)?;
    m.add_function(wrap_pyfunction!(rail_walk_panels, m)?)?;
    m.add_function(wrap_pyfunction!(rail_walk_signs, m)?)?;
    m.add_function(wrap_pyfunction!(rail_buffer_rows, m)?)
}
#[pyfunction]
fn rail_dead_ends<'p>(
    py: Python<'p>,
    rails: &Obj<'p>,
    finals: &Obj<'p>,
    worn: &Obj<'p>,
    near: Bounds,
    carriage: &Obj<'p>,
    cut: &Obj<'p>,
    gauges: &Obj<'p>,
) -> PyResult<List<'p>> {
    let roads = py.import("roads")?;
    let rw = py.import("railways")?;
    let ends = PyList::empty(py);
    if rails.len()? == 0 {
        return Ok(ends);
    }
    let xys = rails
        .try_iter()?
        .map(|r| r?.get_item(3))
        .collect::<PyResult<Vec<_>>>()?;
    let pts = xys.iter().map(line).collect::<PyResult<Vec<_>>>()?;
    let graph = roads.call_method1("_chain_graph", (&xys,))?;
    let mut deg = vec![0; graph.get_item(0)?.len()?];
    let mut edges = vec![];
    for e in graph.get_item(1)?.try_iter()? {
        let e = e?;
        let a: usize = e.get_item(0)?.extract()?;
        let b: usize = e.get_item(1)?.extract()?;
        deg[a] += 1;
        deg[b] += 1;
        edges.push((a, b));
    }
    let lanes = PyDict::new(py);
    if carriage.is_truthy()? {
        for (i, w) in ways(carriage)?.into_iter().enumerate() {
            if w.p.len() >= 2 {
                lanes.set_item(i, (array(py, &w.p), w.width))?;
            }
        }
    }
    let inner = expanded(near, -6.);
    let fit = rw.getattr("RAIL_ROAD_FIT")?.is_truthy()?;
    for (n, (a, b)) in edges
        .into_iter()
        .take(rails.len()?.min(finals.len()?))
        .enumerate()
    {
        let r = rails.get_item(n)?;
        let xy = &pts[n];
        if !r.get_item(2)?.eq("ground")?
            || r.get_item(1)?.eq("tram")?
            || worn
                .call_method1("get", (r.get_item(0)?, 0))?
                .extract::<i64>()?
                >= 2
            || a == b
            || xy.len() < 4
        {
            continue;
        }
        for (node, i, j) in [(a, 0, 2), (b, xy.len() - 1, xy.len() - 3)] {
            let p = xy[i];
            if deg[node] != 1
                || !contains(inner, p)
                || pts
                    .iter()
                    .enumerate()
                    .any(|(k, other)| k != n && !xys[k].is(&xys[n]) && nearest(p, other).1 < 2.3)
            {
                continue;
            }
            if !lanes.is_empty()
                && roads
                    .call_method1("_on_carriageways", (array(py, &[p]), &lanes, 0.))?
                    .call_method0("any")?
                    .is_truthy()?
            {
                continue;
            }
            if fit
                && cut.is_truthy()?
                && cut.contains((
                    (p.0 * 1000.).round_ties_even() as i64,
                    (p.1 * 1000.).round_ties_even() as i64,
                ))?
            {
                continue;
            }
            ends.append((
                p.arr(py),
                direction(p - xy[j]).arr(py),
                finals.get_item(n)?.get_item(i)?.extract::<f64>()? + HEAD,
                n,
            ))?;
        }
    }
    let remove = if fit && carriage.is_truthy()? && !ends.is_empty() {
        rw.call_method1("_road_ends", (&ends, rails, finals, carriage, gauges))?
            .extract()?
    } else {
        vec![false; ends.len()]
    };
    let out = PyList::empty(py);
    for (e, skip) in ends.iter().zip(remove) {
        if !skip {
            out.append(e.get_item(PySlice::new(py, 0, 3, 1))?)?;
        }
    }
    Ok(out)
}
// Project the two tag families separately, preserving the feature/source order.
fn node_points<'p>(py: Python<'p>, frame: &Obj<'p>, ll: &[(f64, f64)]) -> PyResult<Vec<Point>> {
    if ll.is_empty() {
        return Ok(vec![]);
    }
    let coords = frame.call_method1(
        "geodetic_to_enu",
        (
            ll.iter().map(|a| a.1).collect::<Vec<_>>().into_pyarray(py),
            ll.iter().map(|a| a.0).collect::<Vec<_>>().into_pyarray(py),
            0.,
        ),
    )?;
    let x: Vec<f64> = coords.get_item(0)?.extract()?;
    let y: Vec<f64> = coords.get_item(1)?.extract()?;
    Ok(x.into_iter().zip(y).map(|(a, b)| Point(a, b)).collect())
}
pub(super) fn tile<'p>(
    py: Python<'p>,
    tile: &Tile<'p>,
    ex: Bounds,
    all_cr: &Obj<'p>,
    cut: &Obj<'p>,
    want_props: bool,
) -> PyResult<Obj<'p>> {
    let rw = &tile.rw;
    let carriage = PyList::empty(py);
    for c in tile.crossers.try_iter()? {
        let c = c?;
        let xy = c.get_item(0)?;
        if !xy.is_none() {
            carriage.append((xy, c.get_item(1)?))?;
        }
    }
    let mut ll = [vec![], vec![]];
    for f in tile.cells.iter() {
        if f.is_none() {
            continue;
        }
        for n in f
            .getattr("__dict__")?
            .call_method1("get", ("_rail_nodes", PyTuple::empty(py)))?
            .try_iter()?
        {
            let n = n?;
            let kind = n.get_item(2)?.call_method1("get", ("railway",))?;
            let i = if kind.eq("crossing")? {
                0
            } else if kind.eq("level_crossing")? {
                1
            } else {
                continue;
            };
            ll[i].push((n.get_item(0)?.extract()?, n.get_item(1)?.extract()?));
        }
    }
    let nodes = node_points(py, &tile.frame, &ll[0])?;
    let levels = node_points(py, &tile.frame, &ll[1])?;
    let paths = PyList::empty(py);
    for mut w in ways(&rw.call_method1("walk_ways", (&tile.frame, &tile.cells, &tile.bbox, 60.))?)?
    {
        if !levels.is_empty() {
            let xy = dense(py, &w.xy, 2.)?;
            if levels.iter().any(|p| nearest(*p, &xy).1 < 10.) {
                w.width = w.width.max(6.5);
            }
        }
        paths.append((w.xy, w.width))?;
    }
    let cs = PyList::new(
        py,
        rail_walk_crossings(py, &tile.rails, &paths, &array(py, &nodes), &carriage, ex)?,
    )?;
    for p in rail_walk_panels(py, &cs, &tile.finals)?.iter() {
        tile.small.append((
            p.get_item(0)?,
            p.get_item(1)?,
            p.get_item(2)?,
            tile.concrete,
        ))?;
    }
    let out = PyList::empty(py);
    if want_props {
        let signs = rail_walk_signs(
            py,
            &cs,
            &tile.ground,
            &tile.finals,
            &tile.rails,
            &array(py, &levels),
        )?;
        let ends = rail_dead_ends(
            py,
            &tile.rails,
            &tile.finals,
            &tile.worn,
            expanded(tile.bbox.extract()?, 200.),
            all_cr,
            cut,
            &tile.gauges,
        )?;
        let stops = rail_buffer_rows(py, &ends)?;
        let roads = py.import("roads")?;
        for row in signs.iter().chain(stops.iter()) {
            if roads
                .call_method1("box_owns", (&tile.bbox, row.get_item(1)?, row.get_item(2)?))?
                .is_truthy()?
            {
                out.append(row)?;
            }
        }
    }
    Ok(out.into_any())
}
