//! Raised platform geometry, the signed walkable field, lane markings and roof supports.
use super::*;
mod paint;
mod placement;
mod solids;
use numpy::{PyArray1, PyReadonlyArray1};
use pyo3::types::{PyDict, PyList};

fn bounds(r: &[Point]) -> (Point, Point) {
    let mut lo = Point(f64::INFINITY, f64::INFINITY);
    let mut hi = -lo;
    for &p in r {
        lo = Point(lo.0.min(p.0), lo.1.min(p.1));
        hi = Point(hi.0.max(p.0), hi.1.max(p.1));
    }
    (lo, hi)
}
fn seg_quads(path: &[Point], half: f64) -> Vec<Ring> {
    path.windows(2)
        .filter_map(|s| {
            let d = s[1] - s[0];
            let len = d.norm();
            if len < 0.5 {
                return None;
            }
            let n = d.left() / len * half;
            Some(vec![s[0] - n, s[1] - n, s[1] + n, s[0] + n])
        })
        .collect()
}
#[pyfunction]
fn bus_segment_quads<'p>(py: Python<'p>, xy: XY<'p>, half: f64) -> PyResult<Vec<Array<'p>>> {
    Ok(seg_quads(&points(xy)?, half).iter().map(|r| array(py, r)).collect())
}
fn densify(r: &[Point], step: f64) -> Ring {
    let mut out = Vec::new();
    for (a, b) in edges(r) {
        let k = ((b - a).norm() / step).ceil().max(1.0) as usize;
        for i in 0..k {
            out.push(a + (b - a) * (i as f64 / k as f64))
        }
    }
    out
}
#[pyfunction]
fn bus_densify<'p>(py: Python<'p>, xy: XY<'p>, step: f64) -> PyResult<Array<'p>> {
    if !(step > 0.0) {
        return Err(PyValueError::new_err("expected a positive edge step"));
    }
    Ok(array(py, &densify(&ccw(&points(xy)?), step)))
}
#[derive(Clone, Copy)]
struct BoxAxes {
    t: Point,
    u0: f64,
    u1: f64,
    v0: f64,
    v1: f64,
}
impl BoxAxes {
    fn width(self) -> f64 {
        self.v1 - self.v0
    }
    fn centre(self, frac: f64) -> Point {
        self.t * (self.u0 + (self.u1 - self.u0) * frac) + self.t.left() * (self.v0 + self.v1) / 2.0
    }
}
fn obb(r: &[Point]) -> Option<BoxAxes> {
    let r = open(r);
    let mut best = None;
    for (a, b) in edges(r) {
        let d = b - a;
        let len = d.norm();
        if len < 0.5 {
            continue;
        }
        let t = d / len;
        let n = t.left();
        let (mut u0, mut u1, mut v0, mut v1) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
        for &p in r {
            let u = p.dot(t);
            let v = p.dot(n);
            u0 = u0.min(u);
            u1 = u1.max(u);
            v0 = v0.min(v);
            v1 = v1.max(v);
        }
        let area = (u1 - u0) * (v1 - v0);
        if best.is_none_or(|(s, _)| area < s) {
            best = Some((area, BoxAxes { t, u0, u1, v0, v1 }));
        }
    }
    best.map(|(_, b)| {
        if b.u1 - b.u0 < b.width() {
            BoxAxes {
                t: b.t.left(),
                u0: b.v0,
                u1: b.v1,
                v0: -b.u1,
                v1: -b.u0,
            }
        } else {
            b
        }
    })
}
type ObbResult<'p> = (Bound<'p, PyArray1<f64>>, (f64, f64), (f64, f64), f64);
#[pyfunction]
fn bus_obb<'p>(py: Python<'p>, xy: XY<'p>) -> PyResult<Option<ObbResult<'p>>> {
    Ok(obb(&points(xy)?).map(|b| (b.t.arr(py), (b.u0, b.u1), (b.v0, b.v1), b.width())))
}
fn ridge_fraction(p: Point, n: Point, mid: f64, w: f64) -> f64 {
    1.0 - ((p.dot(n) - mid).abs() / (w / 2.0)).min(1.0)
}
type Gable = (Ring, Vec<f64>, Vec<Vec<usize>>, f64);
fn gable(r: &[Point]) -> Option<Gable> {
    let r = ccw(r);
    let b = obb(&r)?;
    let w = b.width();
    if !(4.0..=24.0).contains(&w) || b.u1 - b.u0 < 1.4 * w || area(&r) < 0.85 * (b.u1 - b.u0) * w {
        return None;
    }
    let mid = (b.v0 + b.v1) / 2.0;
    let n = b.t.left();
    let mut pts = Vec::new();
    for (a, b) in edges(&r) {
        pts.push(a);
        let la = a.dot(n) - mid;
        let lb = b.dot(n) - mid;
        if la * lb < 0.0 {
            pts.push(a + (b - a) * (la / (la - lb)))
        }
    }
    let frac = pts.iter().map(|&p| ridge_fraction(p, n, mid, w)).collect();
    let mut halves = Vec::new();
    for sign in [1.0, -1.0] {
        let sel: Vec<_> = (0..pts.len())
            .filter(|&i| sign * (pts[i].dot(n) - mid) >= -1e-6)
            .collect();
        if sel.len() >= 3 {
            halves.push(sel)
        }
    }
    Some((pts, frac, halves, w))
}
#[pyfunction]
fn bus_gable<'p>(
    py: Python<'p>,
    xy: XY<'p>,
) -> PyResult<Option<(Array<'p>, Bound<'p, PyArray1<f64>>, Vec<Vec<usize>>, f64)>> {
    Ok(gable(&points(xy)?).map(|(p, f, h, w)| (array(py, &p), f.into_pyarray(py), h, w)))
}

// polygon.contains_points uses an edge slope first, distinct from station_contains.
struct Sdf {
    ring: Ring,
    slopes: Vec<f64>,
    lo: Point,
    hi: Point,
}
impl Sdf {
    fn new(r: &[Point]) -> Self {
        let r = open(r);
        let (lo, hi) = bounds(r);
        Self {
            ring: r.to_vec(),
            lo,
            hi,
            slopes: edges(r).map(|(a, b)| (b.0 - a.0) / (b.1 - a.1)).collect(),
        }
    }
    fn at(&self, p: Point) -> f64 {
        let mut best = f64::INFINITY;
        let mut inside = false;
        for ((a, b), slope) in edges(&self.ring).zip(&self.slopes) {
            if (a.1 > p.1) != (b.1 > p.1) && a.0 + (p.1 - a.1) * slope > p.0 {
                inside = !inside
            }
            let d = foot(a, b, p).0;
            if d.is_nan() {
                return d;
            }
            best = best.min(d);
        }
        // polygon.contains_points treats every ring with fewer than three vertices as outside.
        if self.ring.len() >= 3 && inside && p.0 >= self.lo.0 && p.0 <= self.hi.0 && p.1 >= self.lo.1 && p.1 <= self.hi.1 {
            best
        } else {
            -best
        }
    }
}
#[pyfunction]
fn bus_sdf<'p>(py: Python<'p>, ring: XY<'p>, xy: XY<'p>) -> PyResult<Bound<'p, PyArray1<f64>>> {
    let sdf = Sdf::new(&points(ring)?);
    let q = points(xy)?;
    if sdf.ring.is_empty() && !q.is_empty() {
        return Err(PyValueError::new_err(
            "zero-size array to reduction operation minimum which has no identity",
        ));
    }
    Ok(q.into_iter().map(|p| sdf.at(p)).collect::<Vec<_>>().into_pyarray(py))
}
struct Lane {
    xy: Ring,
    half: f64,
}
fn lanes(input: Vec<(XY<'_>, f64)>) -> PyResult<Vec<Lane>> {
    input
        .into_iter()
        .map(|(r, half)| Ok(Lane { xy: points(r)?, half }))
        .collect()
}
fn rings(input: Vec<XY<'_>>) -> PyResult<Vec<Ring>> {
    input.into_iter().map(points).collect()
}
fn blocked(lanes: &[Lane], mouths: &[Point], p: Point, pad: f64) -> bool {
    lanes.iter().any(|l| distance(&l.xy, p, false) < l.half + pad)
        || mouths.iter().any(|&m| (p - m).norm() < 12.0 + pad)
}
fn pairs(input: Vec<(f64, f64)>) -> Ring {
    input.into_iter().map(|(x, y)| Point(x, y)).collect()
}
#[pyfunction]
fn bus_blocked<'p>(
    py: Python<'p>,
    xy: XY<'p>,
    paths: Vec<(XY<'p>, f64)>,
    mouths: Vec<(f64, f64)>,
    pad: f64,
) -> PyResult<Bound<'p, PyArray1<bool>>> {
    let paths = lanes(paths)?;
    let mouths = pairs(mouths);
    Ok(points(xy)?
        .iter()
        .map(|&p| blocked(&paths, &mouths, p, pad))
        .collect::<Vec<_>>()
        .into_pyarray(py))
}
#[pyfunction]
fn bus_field<'p>(
    py: Python<'p>,
    plat: Vec<XY<'p>>,
    roofs: Vec<XY<'p>>,
    paths: Vec<(XY<'p>, f64)>,
    mouths: Vec<(f64, f64)>,
) -> PyResult<(i64, i64, Array<'p>)> {
    let plat = rings(plat)?;
    let roofs = rings(roofs)?;
    let paths = lanes(paths)?;
    let mouths = pairs(mouths);
    let all: Vec<_> = plat.iter().chain(&roofs).flatten().copied().collect();
    let (lo, hi) = bounds(&all);
    let (x0, y0) = (lo.0.floor() - 1.0, lo.1.floor() - 1.0);
    let (w, h) = (hi.0.ceil() + 1.0 - x0 + 1.0, hi.1.ceil() + 1.0 - y0 + 1.0);
    if all.is_empty() || w * h > 600000.0 || w < 2.0 || h < 2.0 {
        return Ok((0, 0, Array2::zeros((0, 0)).into_pyarray(py)));
    }
    let ps: Vec<_> = plat.iter().map(|r| Sdf::new(r)).collect();
    let rs: Vec<_> = roofs.iter().map(|r| Sdf::new(r)).collect();
    let field = Array2::from_shape_fn((h as usize, w as usize), |(j, i)| {
        let p = Point(x0 + i as f64, y0 + j as f64);
        let sdf = |rings: &[Sdf]| rings.iter().fold(-1e9_f64, |v, r| npmax(v, r.at(p)));
        let mut ex = 1e9;
        for l in &paths {
            ex = npmin(ex, distance(&l.xy, p, false) - l.half)
        }
        for &m in &mouths {
            ex = npmin(ex, (p - m).norm() - 12.0)
        }
        npmax(sdf(&ps), npmin(sdf(&rs), ex))
    });
    Ok((x0 as i64, y0 as i64, field.into_pyarray(py)))
}
pub(super) fn npmin(a: f64, b: f64) -> f64 {
    if a.is_nan() || a < b {
        a
    } else {
        b
    }
}
pub(super) fn npmax(a: f64, b: f64) -> f64 {
    if a.is_nan() || a > b {
        a
    } else {
        b
    }
}
struct Walk<'a> {
    x: f64,
    y: f64,
    f: numpy::ndarray::ArrayView2<'a, f64>,
}
impl Walk<'_> {
    fn has(&self, p: Point) -> bool {
        let i = (p.0 - self.x).round_ties_even();
        let j = (p.1 - self.y).round_ties_even();
        i >= 0.0
            && j >= 0.0
            && i < (self.f.ncols() as f64)
            && j < (self.f.nrows() as f64)
            && self.f[[j as usize, i as usize]] >= 0.3
    }
}
#[pyfunction]
fn bus_walk_ok<'p>(py: Python<'p>, x: f64, y: f64, f: XY<'p>, xy: XY<'p>) -> PyResult<Bound<'p, PyArray1<bool>>> {
    let field = Walk { x, y, f: f.as_array() };
    Ok(points(xy)?
        .into_iter()
        .map(|p| field.has(p))
        .collect::<Vec<_>>()
        .into_pyarray(py))
}
type Crossing = (Point, Point, f64);
fn crossings_in(c: Vec<(Bound<'_, PyAny>, Bound<'_, PyAny>, f64)>) -> PyResult<Vec<Crossing>> {
    c.into_iter()
        .map(|(p, t, w)| Ok((super::layout::point(&p)?, super::layout::point(&t)?, w)))
        .collect()
}
#[pyfunction]
fn bus_kerbs<'p>(
    py: Python<'p>,
    xy: XY<'p>,
    crossings: Vec<(Bound<'p, PyAny>, Bound<'p, PyAny>, f64)>,
) -> PyResult<Bound<'p, PyArray1<f64>>> {
    let crossings = crossings_in(crossings)?;
    Ok(points(xy)?
        .into_iter()
        .map(|p| {
            let mut h = 0.18;
            for &(c, t, w) in &crossings {
                let d = p - c;
                let a = d.dot(t.left()).abs();
                if d.dot(t).abs() <= 1.8 && a <= w + 0.8 + 1.5 + 1.0 {
                    h = npmin(h, 0.18 * ((a - w - 0.8) / 1.5).clamp(0.0, 1.0));
                }
            }
            h
        })
        .collect::<Vec<_>>()
        .into_pyarray(py))
}
struct Path<'a> {
    xy: &'a [Point],
    seg: Vec<f64>,
    cum: Vec<f64>,
    total: f64,
}
impl<'a> Path<'a> {
    fn new(xy: &'a [Point]) -> Self {
        let seg: Vec<_> = xy.windows(2).map(|s| (s[1] - s[0]).norm()).collect();
        let mut cum = vec![0.0];
        for &s in &seg {
            cum.push(cum[cum.len() - 1] + s)
        }
        let total = crate::junctionsnap::np_sum(&seg);
        Self { xy, seg, cum, total }
    }
    fn at(&self, d: f64) -> (Point, Point) {
        let k = self
            .cum
            .partition_point(|&v| v <= d)
            .saturating_sub(1)
            .min(self.seg.len() - 1);
        let t = (self.xy[k + 1] - self.xy[k]) / self.seg[k].max(1e-9);
        (self.xy[k] + t * (d - self.cum[k]), t)
    }
    fn frames(&self, step: f64) -> Vec<(Point, Point, f64)> {
        if self.seg.is_empty() || self.total < step {
            return vec![];
        }
        let n = ((self.cum[self.cum.len() - 1] + 1e-9) / step).ceil() as usize;
        (0..n)
            .map(|i| {
                let d = i as f64 * step;
                let (c, t) = self.at(d);
                (c, t, d)
            })
            .collect()
    }
}
fn runs(flags: &[bool]) -> Vec<(usize, usize)> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < flags.len() {
        let a = i;
        while i < flags.len() && flags[i] {
            i += 1
        }
        if i > a {
            out.push((a, i - 1))
        }
        i += 1
    }
    out
}
#[pyfunction]
fn bus_frames<'p>(
    py: Python<'p>,
    xy: XY<'p>,
    step: f64,
) -> PyResult<(Array<'p>, Array<'p>, Bound<'p, PyArray1<f64>>)> {
    if !(step > 0.0) {
        return Err(PyValueError::new_err("lane frame step must be positive"));
    }
    let v = Path::new(&points(xy)?).frames(step);
    Ok((
        array(py, &v.iter().map(|f| f.0).collect::<Vec<_>>()),
        array(py, &v.iter().map(|f| f.1).collect::<Vec<_>>()),
        v.iter().map(|f| f.2).collect::<Vec<_>>().into_pyarray(py),
    ))
}
struct BayRect {
    r: Ring,
    lo: Point,
    hi: Point,
}
impl BayRect {
    fn near(&self, p: Point, pad: f64, d: f64) -> bool {
        p.0 >= self.lo.0 - pad
            && p.0 <= self.hi.0 + pad
            && p.1 >= self.lo.1 - pad
            && p.1 <= self.hi.1 + pad
            && (inside(&self.r, p) || distance(&self.r, p, true) < d)
    }
}
fn rects(r: Vec<XY<'_>>) -> PyResult<Vec<BayRect>> {
    Ok(rings(r)?
        .into_iter()
        .map(|r| {
            let (lo, hi) = bounds(&r);
            BayRect { r, lo, hi }
        })
        .collect())
}
type CrossOut<'p> = (Bound<'p, PyArray1<f64>>, Bound<'p, PyArray1<f64>>, f64);
#[pyfunction]
fn bus_crossings<'p>(
    py: Python<'p>,
    x: f64,
    y: f64,
    f: XY<'p>,
    paths: Vec<(XY<'p>, f64)>,
    mouths: Vec<(f64, f64)>,
    bays: Vec<XY<'p>>,
) -> PyResult<Vec<CrossOut<'p>>> {
    let walk = Walk { x, y, f: f.as_array() };
    let mouths = pairs(mouths);
    let rects = rects(bays)?;
    let mut out = vec![];
    for lane in lanes(paths)? {
        let frames = Path::new(&lane.xy).frames(1.0);
        if frames.len() < 8 {
            continue;
        }
        let end = frames[frames.len() - 1].2;
        let mut last = -1e9;
        let ok: Vec<_> = frames
            .iter()
            .map(|&(c, t, d)| {
                let n = t.left() * (lane.half + 1.4);
                d > 6.0
                    && d < end - 6.0
                    && walk.has(c + n)
                    && walk.has(c - n)
                    && mouths.iter().all(|&m| (c - m).norm() > 15.0)
                    && !rects.iter().any(|r| r.near(c, 3.0, 3.0))
            })
            .collect();
        for (i, j) in runs(&ok) {
            let (c, t, d) = frames[(i + j) / 2];
            if frames[j].2 - frames[i].2 >= 3.0 && d - last >= 45.0 {
                out.push((c.arr(py), t.arr(py), lane.half));
                last = d;
            }
        }
    }
    Ok(out)
}
#[pyfunction]
fn bus_edges<'p>(
    py: Python<'p>,
    x: f64,
    y: f64,
    f: XY<'p>,
    paths: Vec<(XY<'p>, f64)>,
    mouths: Vec<(f64, f64)>,
    bays: Vec<XY<'p>>,
    zebras: Vec<(Bound<'p, PyAny>, Bound<'p, PyAny>, f64)>,
) -> PyResult<Vec<Array<'p>>> {
    let walk = Walk { x, y, f: f.as_array() };
    let mouths = pairs(mouths);
    let rects = rects(bays)?;
    let lanes = lanes(paths)?;
    let zebras = crossings_in(zebras)?;
    let mut out = vec![];
    for (li, l) in lanes.iter().enumerate() {
        let frames = Path::new(&l.xy).frames(1.0);
        if frames.len() < 4 {
            continue;
        }
        for side in [1.0, -1.0] {
            let pts: Vec<_> = frames
                .iter()
                .map(|&(c, t, _)| c + t.left() * side * (l.half - 0.2))
                .collect();
            let ok: Vec<_> = pts
                .iter()
                .map(|&p| {
                    !walk.has(p)
                        && lanes
                            .iter()
                            .enumerate()
                            .all(|(j, o)| j == li || distance(&o.xy, p, false) > o.half + 0.3)
                        && mouths.iter().all(|&m| (p - m).norm() > 12.0)
                        && !rects.iter().any(|r| r.near(p, 1.0, 0.8))
                        && zebras.iter().all(|&(c, _, h)| (p - c).norm() > 1.5 + h + 0.5)
                })
                .collect();
            for (i, j) in runs(&ok) {
                if frames[j].2 - frames[i].2 >= 4.0 {
                    out.push(array(py, &pts[i..=j]))
                }
            }
        }
    }
    Ok(out)
}
fn wall_rings<'a>(r: &[Point], buildings: &'a [Ring]) -> Vec<&'a [Point]> {
    let area0 = area(r);
    buildings
        .iter()
        .map(|b| open(b))
        .filter(|&b| b.len() >= 3 && !(inside(r, centroid(b)) && area(b) >= 0.6 * area0))
        .collect()
}
fn in_wall(walls: &[&[Point]], p: Point) -> bool {
    walls.iter().any(|r| inside(r, p) || distance(r, p, true) < 0.8)
}
#[pyfunction]
fn bus_in_wall(ring: XY<'_>, buildings: Vec<XY<'_>>, p: (f64, f64)) -> PyResult<bool> {
    Ok(in_wall(
        &wall_rings(&points(ring)?, &rings(buildings)?),
        Point(p.0, p.1),
    ))
}
fn column_points(r: &[Point], lanes: &[Lane], mouths: &[Point], buildings: &[Ring]) -> Ring {
    let r = open(r);
    let c = centroid(r);
    let mut pts = Vec::new();
    for p in densify(r, 8.0) {
        let v = c - p;
        let d = v.norm();
        pts.push(if d > 1e-6 { p + v / d * 0.5f64.min(d * 0.2) } else { p });
    }
    let (lo, hi) = bounds(r);
    if (hi.0 - lo.0).min(hi.1 - lo.1) > 12.0 {
        for x in arange(lo.0 + 6.0, hi.0, 12.0) {
            for y in arange(lo.1 + 6.0, hi.1, 12.0) {
                let p = Point(x, y);
                if inside(r, p) && distance(r, p, true) > 2.0 {
                    pts.push(p)
                }
            }
        }
    }
    let walls = wall_rings(r, buildings);
    pts.retain(|&p| !blocked(lanes, mouths, p, 0.5) && !in_wall(&walls, p));
    pts
}
fn arange(a: f64, b: f64, step: f64) -> impl Iterator<Item = f64> {
    let n = ((b - a) / step).ceil() as usize;
    let next = a + step;
    let stride = next - a;
    (0..n).map(move |i| match i {
        0 => a,
        1 => next,
        _ => a + i as f64 * stride,
    })
}
#[pyfunction]
fn bus_columns<'p>(
    py: Python<'p>,
    ring: XY<'p>,
    paths: Vec<(XY<'p>, f64)>,
    mouths: Vec<(f64, f64)>,
    buildings: Vec<XY<'p>>,
) -> PyResult<Vec<Bound<'p, PyArray1<f64>>>> {
    Ok(
        column_points(&points(ring)?, &lanes(paths)?, &pairs(mouths), &rings(buildings)?)
            .into_iter()
            .map(|p| p.arr(py))
            .collect(),
    )
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(bus_walkable, m)?)?;
    m.add_function(wrap_pyfunction!(bus_lattice, m)?)?;
    placement::register(m)?;
    paint::register(m)?;
    solids::register(m)?;
    m.add_function(wrap_pyfunction!(bus_segment_quads, m)?)?;
    m.add_function(wrap_pyfunction!(bus_densify, m)?)?;
    m.add_function(wrap_pyfunction!(bus_obb, m)?)?;
    m.add_function(wrap_pyfunction!(bus_gable, m)?)?;
    m.add_function(wrap_pyfunction!(bus_sdf, m)?)?;
    m.add_function(wrap_pyfunction!(bus_blocked, m)?)?;
    m.add_function(wrap_pyfunction!(bus_field, m)?)?;
    m.add_function(wrap_pyfunction!(bus_walk_ok, m)?)?;
    m.add_function(wrap_pyfunction!(bus_kerbs, m)?)?;
    m.add_function(wrap_pyfunction!(bus_frames, m)?)?;
    m.add_function(wrap_pyfunction!(bus_crossings, m)?)?;
    m.add_function(wrap_pyfunction!(bus_edges, m)?)?;
    m.add_function(wrap_pyfunction!(bus_in_wall, m)?)?;
    m.add_function(wrap_pyfunction!(bus_columns, m)?)
}

#[pyfunction]
fn bus_walkable<'p>(py: Python<'p>, f: XY<'p>) -> Bound<'p, PyArray2<bool>> {
    let v = f.as_array();
    Array2::from_shape_fn((v.nrows().saturating_sub(1), v.ncols().saturating_sub(1)), |(j, i)| {
        v[[j, i]] + v[[j, i + 1]] + v[[j + 1, i]] + v[[j + 1, i + 1]] >= 0.0
    })
    .into_pyarray(py)
}
#[pyfunction]
fn bus_lattice<'p>(py: Python<'p>, x: f64, y: f64, f: XY<'p>) -> Option<Array<'p>> {
    let v = f.as_array();
    if !v.iter().any(|&z| z >= 0.0) {
        return None;
    }
    Some(rows(
        py,
        (0..v.nrows())
            .flat_map(|j| (0..v.ncols()).map(move |i| [x + i as f64, y + j as f64]))
            .collect(),
    ))
}
