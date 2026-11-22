//! Insertion order is part of the output. Portal steel precedes spans, joint links follow
//! the per-part spans, and mast props follow the portal posts.
use crate::busstation::{rows, Point};
use crate::dm::Dm;
use crate::roadnet::pyfloat::py_round;
use pyo3::{
    prelude::*,
    types::{PyDict, PyList},
};

type Obj<'py> = Bound<'py, PyAny>;
type List<'py> = Bound<'py, PyList>;
type XYZ = [f64; 3];

fn point(v: &Obj) -> PyResult<Point> {
    let [x, y] = v.extract::<[f64; 2]>()?;
    Ok(Point(x, y))
}
fn xy(obj: &Obj, key: &str) -> PyResult<Point> {
    point(&obj.get_item(key)?)
}
fn number(obj: &Obj, key: &str) -> PyResult<f64> {
    obj.get_item(key)?.extract()
}
fn flag(obj: &Obj, key: &str) -> PyResult<bool> {
    obj.call_method1("get", (key,))?.is_truthy()
}
fn at(p: Point, z: f64) -> XYZ {
    [p.0, p.1, z]
}
fn lerp(a: XYZ, b: XYZ, t: f64) -> XYZ {
    std::array::from_fn(|i| a[i] + (b[i] - a[i]) * t)
}
fn tip(m: &Obj) -> PyResult<XYZ> {
    Ok(at(
        xy(m, "q")? + xy(m, "dir")? * 3.45,
        number(m, "z")? + 6.18,
    ))
}
fn wire(out: &List, points: Vec<XYZ>, radius: f64) -> PyResult<()> {
    out.append((rows(out.py(), points), radius))
}
fn fraction(i: usize, n: usize) -> f64 {
    if i == n {
        1.
    } else {
        i as f64 * (1. / n as f64)
    }
}
fn span(out: &List, a: &Obj, b: &Obj) -> PyResult<()> {
    let c0 = tip(a)?;
    let c1 = tip(b)?;
    let mut m0 = c0;
    let mut m1 = c1;
    if !flag(a, "end")? {
        m0[2] = number(a, "z")? + 7.80;
    }
    if !flag(b, "end")? {
        m1[2] = number(b, "z")? + 7.80;
    }
    let length = (c1[0] - c0[0]).dhypot(c1[1] - c0[1]);
    let n = (length / 10.).ceil().max(2.) as usize;
    let messenger = |t| {
        let mut p = lerp(m0, m1, t);
        p[2] -= 1.12 * 4. * t * (1. - t);
        p
    };
    wire(
        out,
        (0..=n).map(|i| lerp(c0, c1, fraction(i, n))).collect(),
        0.016,
    )?;
    wire(
        out,
        (0..=n).map(|i| messenger(fraction(i, n))).collect(),
        0.013,
    )?;
    let first = (length / 2.).min(3.);
    let k = ((length - 2. * first) / 6.).round_ties_even().max(1.) as usize;
    let last = if length > 4. { k } else { 0 };
    for i in 0..=last {
        let t = if length > 4. {
            first / length + (1. - 2. * first / length) * i as f64 / k as f64
        } else {
            0.5
        };
        let bot = lerp(c0, c1, t);
        let top = messenger(t);
        if top[2] - bot[2] > 0.12 {
            wire(out, vec![bot, top], 0.010)?;
        }
    }
    Ok(())
}
fn posts(m: &Obj) -> PyResult<[(Point, f64); 2]> {
    let fields = m.get_item("posts")?;
    let post = |i| -> PyResult<_> {
        let p = fields.get_item(i)?;
        Ok((point(&p.get_item(0)?)?, p.get_item(1)?.extract::<f64>()?))
    };
    Ok([post(0usize)?, post(1usize)?])
}
fn portal(out: &List, m: &Obj) -> PyResult<()> {
    let [(a, _), (b, _)] = posts(m)?;
    let bot = number(m, "z")? + 8.;
    let top = bot + 1.;
    let d = b - a;
    let len = d.norm();
    let n = (len / 2.).round_ties_even().max(2.) as usize;
    let u = d / len.max(1e-9);
    for z in [bot, top] {
        wire(out, vec![at(a + d * 0., z), at(a + d * 1., z)], 0.05)?;
    }
    for i in 1..n {
        let q = a + d * fraction(i, n);
        wire(out, vec![at(q, bot - 0.05), at(q, top + 0.05)], 0.025)?;
    }
    for track in m.get_item("tracks")?.try_iter()? {
        let track = track?;
        let q = point(&track.get_item(0)?)?;
        let z = track.get_item(1)?.extract::<f64>()? + 6.18;
        let ins = bot - 0.4;
        for (z0, z1, r) in [
            (bot, ins, 0.035),
            (ins, ins - 0.4 * 0.6, 0.035),
            (ins - 0.4 * 0.6, z + 0.12, 0.022),
        ] {
            wire(out, vec![at(q, z0), at(q, z1)], r)?;
        }
        for sg in [-1., 1.] {
            wire(
                out,
                vec![at(q, z + 0.12), at(q + u * (0.3 * sg), z + 0.05)],
                0.022,
            )?;
        }
        wire(out, vec![at(q - u * 0.15, z), at(q + u * 0.15, z)], 0.03)?;
    }
    for p in [a, b] {
        wire(out, vec![at(p, bot - 0.4), at(p, top + 0.3)], 0.05 * 1.6)?;
    }
    Ok(())
}
#[pyfunction]
fn catenary_portal_wires<'py>(m: &Obj<'py>) -> PyResult<List<'py>> {
    let out = PyList::empty(m.py());
    portal(&out, m)?;
    Ok(out)
}
#[pyfunction]
fn catenary_overlaps(new: &Obj, placed: Vec<Obj>) -> PyResult<bool> {
    let mid = |m: &Obj| -> PyResult<Point> {
        let [a, b] = posts(m)?;
        Ok((a.0 + b.0) / 2.)
    };
    let c = mid(new)?;
    for m in placed {
        if (c - mid(&m)?).norm() < 14. {
            return Ok(true);
        }
    }
    Ok(false)
}
fn yaw(d: Point) -> f64 {
    let a = d.1.datan2(d.0).to_degrees() % 360.;
    py_round(
        if a == 0. {
            0.
        } else if a < 0. {
            a + 360.
        } else {
            a
        },
        1,
    )
}
fn prop(out: &List, group: &str, q: Point, z: f64, yaw: f64, h: f64) -> PyResult<()> {
    let row = PyList::empty(out.py());
    row.append(group)?;
    for x in [
        py_round(q.0, 2),
        py_round(q.1, 2),
        py_round(z, 2),
        yaw,
        1.,
        h,
        0.,
    ] {
        row.append(x)?;
    }
    out.append(row)
}
#[pyfunction]
fn catenary_build<'py>(
    py: Python<'py>,
    masts: Vec<Obj<'py>>,
    owns: &Obj,
    groups: (String, String),
) -> PyResult<(List<'py>, List<'py>)> {
    let rows = PyList::empty(py);
    let wires = PyList::empty(py);
    let owned = |p: Point| owns.call1(p.pair())?.is_truthy();
    let by_line = PyDict::new(py);
    let mut links = vec![];
    for m in &masts {
        if m.contains("link")? {
            links.push(m.get_item("link")?);
            continue;
        }
        if flag(m, "portal")? {
            if owned(xy(m, "q")?)? {
                portal(&wires, m)?;
            }
            let posts = posts(m)?;
            let angle = yaw(posts[1].0 - posts[0].0);
            for (p, z) in posts {
                if owned(p)? {
                    prop(&rows, &groups.1, p, z, angle, 10.4)?;
                }
            }
        } else {
            let key = m.call_method1("get", ("part", m.get_item("fid")?))?;
            let ms = by_line.call_method1("setdefault", (key, PyList::empty(py)))?;
            ms.call_method1("append", (m,))?;
        }
    }
    for (_, line) in by_line.iter() {
        let mut ms = vec![];
        for m in line.try_iter()? {
            let m = m?;
            ms.push((number(&m, "s")?, m));
        }
        ms.sort_by(|a, b| a.0.total_cmp(&b.0));
        for w in ms.windows(2) {
            let (a, b) = (&w[0].1, &w[1].1);
            let l = (xy(b, "axis")? - xy(a, "axis")?).norm();
            if (0.3..=200.).contains(&l) && w[1].0 - w[0].0 <= 200. * 1.3 && owned(xy(a, "q")?)? {
                span(&wires, a, b)?;
            }
        }
    }
    for link in links {
        let (a, b) = (link.get_item(0)?, link.get_item(1)?);
        let (qa, qb) = (tip(&a)?, tip(&b)?);
        if (qb[0] - qa[0]).dhypot(qb[1] - qa[1]) <= 150. && owned(xy(&a, "q")?)? {
            span(&wires, &a, &b)?;
        }
    }
    for m in &masts {
        if flag(m, "portal")? || flag(m, "virtual")? || m.contains("link")? {
            continue;
        }
        let q = xy(m, "q")?;
        if owned(q)? {
            prop(&rows, &groups.0, q, number(m, "z")?, yaw(xy(m, "dir")?), 9.)?;
        }
    }
    Ok((rows, wires))
}
#[pyfunction]
fn catenary_span_wires<'p>(a: &Obj<'p>, b: &Obj<'p>) -> PyResult<List<'p>> {
    let out = PyList::empty(a.py());
    span(&out, a, b)?;
    Ok(out)
}
#[pyfunction]
fn catenary_style(country: &Obj, concrete_countries: &Obj) -> PyResult<&'static str> {
    if country.is_truthy()? && concrete_countries.contains(country.str()?.call_method0("upper")?)? {
        Ok("su")
    } else {
        Ok("eu")
    }
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(catenary_portal_wires, m)?)?;
    m.add_function(wrap_pyfunction!(catenary_style, m)?)?;
    m.add_function(wrap_pyfunction!(catenary_span_wires, m)?)?;
    m.add_function(wrap_pyfunction!(catenary_build, m)?)?;
    m.add_function(wrap_pyfunction!(catenary_overlaps, m)?)
}
