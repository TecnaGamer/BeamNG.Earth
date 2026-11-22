//! Predict the mod's next terrain requests on the anchor lattice. The request
//! point and requested centre are distinct when a vehicle crosses an edge at speed.
use crate::dm::Dm;
use numpy::PyReadonlyArray2;
use pyo3::{exceptions::PyValueError, prelude::*};

type Point = (f64, f64);
type Mode = (f64, f64, f64);
type Step = (Point, Point);

#[derive(Clone, FromPyObject)]
struct Rule {
    grid: f64,
    prepare: f64,
    lead: f64,
    reach: f64,
    fast_v: f64,
    fast_lead: f64,
    fast_reach: f64,
    fast_ahead: f64,
    min_lead: f64,
    min_lead_v: f64,
}

fn norm2(p: Point) -> f64 {
    p.0 * p.0 + p.1 * p.1
}
fn sup(p: Point) -> f64 {
    p.0.abs().max(p.1.abs())
}
fn offset(p: Point, v: Point, t: f64) -> Point {
    (p.0 + v.0 * t, p.1 + v.1 * t)
}
fn relative(p: Point, c: Point) -> Point {
    (p.0 - c.0, p.1 - c.1)
}
fn dot(p: Point, q: Point) -> f64 {
    p.0 * q.0 + p.1 * q.1
}

impl Rule {
    fn mode(&self, v: Point) -> Mode {
        if norm2(v) > self.fast_v * self.fast_v {
            (self.fast_lead, self.fast_reach, self.fast_ahead)
        } else {
            (self.lead, self.reach, 0.)
        }
    }

    fn request(&self, mut p: Point, v: Point, centre: Point, frame_s: f64) -> Step {
        let (lead, reach, ahead) = self.mode(v);
        let v2 = norm2(v);
        let rel = relative(centre, p);
        if self.min_lead > 0.
            && self.min_lead_v * self.min_lead_v < v2
            && v2 * lead * lead < self.min_lead * self.min_lead
            && dot(rel, v) > 0.
            && sup(rel) <= self.min_lead + self.grid
        {
            p = offset(p, v, dot(rel, v) / v2 + frame_s);
        }
        let q = offset(p, v, ahead);
        let mut t = 0.;
        if sup(relative(q, centre)) <= self.prepare {
            let mut exit = f64::INFINITY;
            for (q, v, c) in [(q.0, v.0, centre.0), (q.1, v.1, centre.1)] {
                if v.abs() > 0.5 {
                    exit = exit.min(((c + self.prepare.copysign(v)) - q) / v);
                }
            }
            if exit.is_finite() {
                t = exit + frame_s;
            }
        }
        let asked = offset(p, v, t);
        (
            centre_for(
                asked,
                v,
                lead,
                reach,
                self.grid,
                self.min_lead,
                self.min_lead_v,
            ),
            asked,
        )
    }
}

fn centre_for(
    p: Point,
    v: Point,
    lead: f64,
    reach: f64,
    grid: f64,
    min_lead: f64,
    min_v: f64,
) -> Point {
    let mut d = (v.0 * lead, v.1 * lead);
    let v2 = norm2(v);
    if min_lead > 0. && v2 > min_v * min_v && norm2(d) < min_lead * min_lead {
        let k = min_lead / v2.sqrt();
        d = (v.0 * k, v.1 * k);
    }
    let axis = |p: f64, d: f64| {
        let c = ((p + d) / grid + 0.5).floor() * grid + 0.;
        c.max(((p - reach) / grid).ceil() * grid)
            .min(((p + reach) / grid).floor() * grid)
            + 0.
    };
    (axis(p.0, d.0), axis(p.1, d.1))
}

#[pyfunction]
fn follow_mode(rule: Rule, v: Point) -> Mode {
    rule.mode(v)
}

#[pyfunction]
fn follow_centre(
    p: Point,
    v: Point,
    lead: f64,
    reach: f64,
    grid: f64,
    min_lead: f64,
    min_v: f64,
) -> Point {
    centre_for(p, v, lead, reach, grid, min_lead, min_v)
}

#[pyfunction]
fn follow_request(
    rule: Rule,
    p: Point,
    v: Point,
    centre: Point,
    frame_s: f64,
    at: Option<&Bound<'_, PyAny>>,
) -> PyResult<Point> {
    let (c, asked) = rule.request(p, v, centre, frame_s);
    if let Some(out) = at {
        out.call_method1("append", (asked,))?;
    }
    Ok(c)
}

#[pyfunction]
fn follow_chain(
    rule: Rule,
    mut p: Point,
    v: Point,
    mut centre: Point,
    n: isize,
    frame_s: f64,
    at: Option<&Bound<'_, PyAny>>,
) -> PyResult<Vec<Point>> {
    let mut out = Vec::new();
    let v2 = norm2(v);
    if v2 >= 1. {
        for _ in 0..n {
            let (c, asked) = rule.request(p, v, centre, frame_s);
            if c == centre || out.contains(&c) {
                break;
            }
            out.push(c);
            if let Some(at) = at {
                at.set_item(c, asked)?;
            }
            p = offset(p, v, (dot(relative(c, p), v) / v2).max(0.));
            centre = c;
        }
    }
    Ok(out)
}

#[pyfunction]
fn follow_route(
    rule: Rule,
    xy: Option<PyReadonlyArray2<'_, f64>>,
    p: Point,
    v: Point,
    mut centre: Point,
    n: isize,
    frame_s: f64,
    limits: (f64, f64, f64, f64, f64),
    at: Option<&Bound<'_, PyAny>>,
) -> PyResult<Option<Vec<Point>>> {
    let speed = crate::py_hypot(v.0, v.1);
    let Some(xy) = xy else {
        return Ok(None);
    };
    let a = xy.as_array();
    if speed < 1. || a.nrows() < 2 {
        return Ok(None);
    }
    if a.ncols() != 2 {
        return Err(PyValueError::new_err(
            "expected route coordinates in two columns",
        ));
    }
    let point = |i| (a[[i, 0]], a[[i, 1]]);
    let seg = |i| relative(point(i + 1), point(i));
    let (mut j, mut closest, mut q) = (0, f64::INFINITY, p);
    for k in 0..a.nrows() - 1 {
        let d = seg(k);
        let t = (dot(relative(p, point(k)), d) / norm2(d).max(1e-9)).clamp(0., 1.);
        let candidate = offset(point(k), d, t);
        let dx = relative(candidate, p);
        let distance = dx.0.dhypot(dx.1);
        if distance < closest {
            j = k;
            closest = distance;
            q = candidate;
        }
    }
    let (near, align, step, seconds, max_m) = limits;
    let d = seg(j);
    if closest > near || dot(d, v) / (norm2(d).max(1e-9).sqrt() * speed) < align {
        return Ok(None);
    }
    let mut out = Vec::new();
    let mut gone = 0.;
    let reach = max_m.min(speed * seconds);
    while gone < reach && j < a.nrows() - 1 && (out.len() as isize) < n {
        let d = seg(j);
        let length = norm2(d).max(1e-9).sqrt();
        let u = (d.0 / length, d.1 / length);
        let left = relative(point(j + 1), q);
        let left = crate::py_hypot(left.0, left.1);
        if left <= 1e-6 {
            j += 1;
            continue;
        }
        let advance = step.min(left);
        q = offset(q, u, advance);
        gone += advance;
        let vel = (u.0 * speed, u.1 * speed);
        let ahead = rule.mode(vel).2;
        if sup(relative(offset(q, vel, ahead), centre)) > rule.prepare {
            let (c, asked) = rule.request(q, vel, centre, frame_s);
            if c != centre && !out.contains(&c) {
                out.push(c);
                centre = c;
                if let Some(at) = at {
                    at.set_item(c, asked)?;
                }
            }
        }
    }
    Ok(Some(out))
}

/// Choose the pending or committed block exactly as the mod will after commit.
/// Try the GPS path, then the reach tree, then the straight velocity chain.
#[pyfunction]
fn follow_pose<'py>(
    py: Python<'py>,
    c: &Bound<'py, PyAny>,
    aid: &Bound<'py, PyAny>,
    frame_s: f64,
    fast_speed: f64,
    chain: isize,
    limits: (f64, f64, f64, f64, f64),
) -> PyResult<Bound<'py, PyAny>> {
    use pyo3::types::{PyDict, PyList};
    c.setattr("predicted_block", py.None())?;
    let tree = if aid.eq(c.getattr("active")?)? {
        c.call_method1("reach_tree", (aid,))?
    } else {
        py.None().into_bound(py)
    };
    c.setattr("tree", &tree)?;
    c.setattr("tree_led", false)?;
    let pending = c.getattr("pending_block")?;
    let base = if pending.is_truthy()? {
        pending
    } else {
        c.getattr("block")?
    };
    if base.is_none() || !c.getattr("following")?.is_truthy()? {
        return Ok(tree);
    }
    let centre = (
        base.getattr("cx")?.extract()?,
        base.getattr("cy")?.extract()?,
    );
    let s = c.getattr("s")?;
    let follow = s.getattr("follow")?;
    let rule: Rule = follow.extract()?;
    let p: Point = c.getattr("focus")?.extract()?;
    let v: Point = c.getattr("vel")?.extract()?;
    let q = offset(p, v, rule.mode(v).2);
    let speed = crate::py_hypot(v.0, v.1);
    let photos =
        speed >= 25. && s.getattr("near_visual")?.eq("detail")? && !s.getattr("imagery")?.is_none();
    let photo_request = photos.then(|| rule.request(p, v, centre, frame_s));
    // A first photo can take a whole block crossing. Predict from the point where
    // the mod will request it, even when the car is still behind the current centre.
    let photo_due =
        photo_request.is_some_and(|(_, asked)| norm2(relative(asked, p)) <= (speed * 14.).powi(2));
    let layout = s.getattr("layout")?;
    let at = PyDict::new(py);
    c.setattr("predicted_at", &at)?;
    if photo_due
        || sup(relative(q, centre)) >= follow.getattr("prefetch_start")?.extract::<f64>()?
    {
        let (next, asked) = photo_request.unwrap_or_else(|| rule.request(p, v, centre, frame_s));
        if next != centre
            && !layout
                .call_method1("block_at", next)?
                .eq(c.getattr("block")?)?
        {
            c.setattr("predicted_block", layout.call_method1("block_at", next)?)?;
            at.set_item(next, asked)?;
        }
    }
    c.setattr("predicted_chain", PyList::empty(py))?;
    if speed > fast_speed || photo_due {
        let chain = if speed > fast_speed {
            chain
        } else {
            chain.min(2)
        };
        let along = |xy: Bound<'py, PyAny>| -> PyResult<_> {
            let where_ = PyDict::new(py);
            let points = if xy.is_none() {
                None
            } else {
                Some(xy.extract()?)
            };
            let centres = follow_route(
                rule.clone(),
                points,
                p,
                v,
                centre,
                chain + 1,
                frame_s,
                limits,
                Some(where_.as_any()),
            )?;
            Ok((centres.unwrap_or_default(), where_))
        };
        let (mut points, mut asked) = along(c.call_method1("route_xy", (aid,))?)?;
        if points.is_empty() && !tree.is_none() && tree.getattr("mpp")?.len()? >= 2 {
            (points, asked) = along(tree.call_method0("mpp_xy")?)?;
            c.setattr("tree_led", !points.is_empty())?;
        }
        let blocks = PyList::empty(py);
        let points = if let Some((first, rest)) = points.split_first() {
            at.call_method1("update", (asked,))?;
            if !c.getattr("predicted_block")?.is_none() {
                c.setattr("predicted_block", layout.call_method1("block_at", *first)?)?;
            }
            rest.to_vec()
        } else {
            follow_chain(rule, p, v, centre, chain, frame_s, Some(at.as_any()))?
        };
        for next in points {
            blocks.append(layout.call_method1("block_at", next)?)?;
        }
        c.setattr("predicted_chain", blocks)?;
    }
    Ok(tree)
}

/// A fast car can leave before a full photo of the committed block finishes.
/// Only refine blocks already built, one per refresh pass on the existing pool.
#[pyfunction]
fn photo_refresh_key<'py>(
    c: &Bound<'py, PyAny>,
    aid: i64,
    anchor: &Bound<'py, PyAny>,
    now: f64,
) -> PyResult<Option<(i64, f64, f64)>> {
    type Key = (i64, f64, f64);
    let s = c.getattr("s")?;
    let hm = s.getattr("hm")?;
    let stale = s.getattr("base_stale")?;
    let skipped = s.getattr("base_skipped")?;
    let mut keys = Vec::new();
    for name in ["hm_committed", "hm_sent"] {
        let history = s.getattr(name)?;
        if history.len()? > 0 {
            keys.push(history.get_item(-1)?.extract::<Key>()?);
        }
    }
    let v: Point = c.getattr("vel")?.extract()?;
    if norm2(v) >= 25. * 25. {
        let p: Point = c.getattr("focus")?.extract()?;
        let predicted = c.getattr("predicted_block")?;
        let chain = c.getattr("predicted_chain")?;
        let mut ahead = vec![predicted];
        for b in chain.try_iter()? {
            ahead.push(b?);
        }
        // Serve a missing current photo first. With a stand-in already installed,
        // give the next block its final photo before committing it.
        let mut insert = 0;
        for k in &keys {
            let info = hm.call_method1("get", (*k,))?;
            if !info.is_none() && !info.call_method1("get", ("base",))?.is_truthy()? {
                insert += 1;
            } else {
                break;
            }
        }
        for b in ahead {
            if b.is_none() {
                continue;
            }
            let xy = (b.getattr("cx")?.extract()?, b.getattr("cy")?.extract()?);
            let rel = relative(xy, p);
            let k = (aid, xy.0, xy.1);
            if dot(rel, v) > 0.
                && norm2(rel) <= norm2(v) * 14. * 14.
                && skipped.contains(k)?
                && !keys.contains(&k)
            {
                keys.insert(insert, k);
                insert += 1;
            }
        }
    }
    for k in keys {
        if k.0 == aid
            && stale.contains(k)?
            && hm.contains(k)?
            && hm
                .get_item(k)?
                .call_method1("get", ("baseRetryAt", 0.))?
                .extract::<f64>()?
                <= now
            && (skipped.contains(k)?
                || s.call_method1("base_ready", (anchor, k.1, k.2))?
                    .is_truthy()?)
        {
            return Ok(Some(k));
        }
    }
    Ok(None)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(photo_refresh_key, m)?)?;
    m.add_function(wrap_pyfunction!(follow_pose, m)?)?;
    m.add_function(wrap_pyfunction!(follow_mode, m)?)?;
    m.add_function(wrap_pyfunction!(follow_centre, m)?)?;
    m.add_function(wrap_pyfunction!(follow_request, m)?)?;
    m.add_function(wrap_pyfunction!(follow_chain, m)?)?;
    m.add_function(wrap_pyfunction!(follow_route, m)?)?;
    Ok(())
}
