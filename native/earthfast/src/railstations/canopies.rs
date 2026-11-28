//! Fit an inferred roof to the snapped platform edge or a missing platform to a nearby track.
use super::{
    layout::{foot, normals},
    *,
};
use crate::busstation::{area, ccw, distance, open};
use crate::{dm::interp, railfit::np_arange};
fn arc(line: &[Point]) -> Vec<f64> {
    let mut s = vec![0.];
    for w in line.windows(2) {
        s.push(s.last().unwrap() + (w[1] - w[0]).norm());
    }
    s
}
fn length(line: &[Point]) -> f64 {
    crate::levels::np_sum(
        &line
            .windows(2)
            .map(|w| (w[1] - w[0]).norm())
            .collect::<Vec<_>>(),
    )
}
fn sample(line: &[Point], s: &[f64], ss: &[f64]) -> Vec<Point> {
    let x = interp(
        ss,
        s,
        &line.iter().map(|p| p.0).collect::<Vec<_>>(),
        None,
        None,
    );
    let y = interp(
        ss,
        s,
        &line.iter().map(|p| p.1).collect::<Vec<_>>(),
        None,
        None,
    );
    x.into_iter().zip(y).map(|(x, y)| Point(x, y)).collect()
}
fn median(a: &[f64]) -> f64 {
    let mut v = a.to_vec();
    v.sort_by(f64::total_cmp);
    if v.len() % 2 == 0 {
        (v[v.len() / 2 - 1] + v[v.len() / 2]) / 2.
    } else {
        v[v.len() / 2]
    }
}
fn ray(ring: &[Point], p: Point, d: Point) -> Option<f64> {
    let r = open(ring);
    let mut hit = None;
    for (&a, &b) in r.iter().zip(r.iter().cycle().skip(1)) {
        let e = b - a;
        let q = a - p;
        let den = d.0 * e.1 - d.1 * e.0;
        if den.abs() <= 1e-9 {
            continue;
        }
        let t = (q.0 * e.1 - q.1 * e.0) / den;
        let u = (q.0 * d.1 - q.1 * d.0) / den;
        if t > 1e-6 && (0. ..=1.).contains(&u) && t < 60. {
            hit = Some(t.min(hit.unwrap_or(f64::INFINITY)));
        }
    }
    hit
}
fn roof_ring(
    ring: &[Point],
    edge: &[Point],
    s: &[f64],
    centre: f64,
    len: f64,
) -> Option<Vec<Point>> {
    let lo = (centre - len / 2.).max(0.);
    let hi = (centre + len / 2.).min(*s.last()?);
    if hi - lo < 20. {
        return None;
    }
    let line = sample(edge, s, &np_arange(lo, hi + 1e-6, 2.));
    let n = normals(&line);
    let width: Vec<_> = line
        .iter()
        .zip(&n)
        .map(|(&p, &n)| ray(ring, p + n * 0.05, n).unwrap_or(7.5 + 1.2 + 0.8))
        .collect();
    let far: Vec<_> = (0..width.len())
        .map(|i| {
            let w: Vec<_> = (-5isize..=5)
                .map(|d| width[(i as isize + d).clamp(0, width.len() as isize - 1) as usize])
                .collect();
            (median(&w) - 0.8).min(1.2 + 7.5)
        })
        .collect();
    if median(&far.iter().map(|w| w - 1.2).collect::<Vec<_>>()) < 2.5 {
        return None;
    }
    let mut out: Vec<_> = line.iter().zip(&n).map(|(&p, &n)| p + n * 1.2).collect();
    out.extend(
        (0..line.len())
            .rev()
            .map(|i| line[i] + n[i] * far[i].max(1.2 + 2.5 * 0.5)),
    );
    Some(ccw(&out))
}
#[pyfunction]
fn rail_canopies(st: &Obj) -> PyResult<()> {
    let py = st.py();
    let rs = py.import("railstations")?;
    let bs = py.import("busstations")?;
    let platforms = st.getattr("platforms")?;
    let main = st.getattr("kind")?.eq("station")? && platforms.len()? >= 3;
    let mut hall = None;
    let mut best = f64::NEG_INFINITY;
    for b in st.getattr("buildings")?.try_iter()? {
        let r = b?.get_item(1)?;
        let a = area(&points(r.extract()?)?);
        if a > best {
            best = a;
            hall = Some(r);
        }
    }
    let hall = hall
        .map(|r| point(&bs.call_method1("centroid", (r,))?))
        .transpose()?;
    for pl in platforms.try_iter()? {
        let pl = pl?;
        if pl.getattr("roofs")?.len()? > 0 || !pl.getattr("mapped")?.is_truthy()? {
            continue;
        }
        let shelter = pl.call_method1("tag", ("shelter",))?;
        let covered = pl.call_method1("tag", ("covered",))?;
        if !(main || shelter.eq("yes")? || covered.eq("yes")?)
            || covered.eq("no")?
            || shelter.eq("no")?
        {
            continue;
        }
        let r = pl.getattr("ring")?;
        let mut best: Option<(f64, Vec<Point>)> = None;
        for run in rs.call_method1("_runs_of", (&pl,))?.try_iter()? {
            let pts = points(r.get_item(run?)?.extract()?)?;
            let len = length(&pts);
            if len >= 60. && best.as_ref().is_none_or(|b| len > b.0) {
                best = Some((len, pts));
            }
        }
        let Some((len, pts)) = best else { continue };
        let s = arc(&pts);
        let roof = (0.6 * len).min(110.);
        let mid = hall
            .map(|p| {
                let i = (0..pts.len())
                    .min_by(|&a, &b| (pts[a] - p).norm().total_cmp(&(pts[b] - p).norm()))
                    .unwrap();
                s[i]
            })
            .unwrap_or(len / 2.);
        let mid = mid.max(roof / 2.).min(len - roof / 2.);
        let Some(r) = roof_ring(&points(r.extract()?)?, &pts, &s, mid, roof) else {
            continue;
        };
        if area(&r) < 60. {
            continue;
        }
        let rid = format!("synth:{}", pl.getattr("id")?.str()?);
        st.getattr("roofs")?
            .call_method1("append", ((&rid, array(py, &r)),))?;
        pl.getattr("roofs")?.call_method1("append", (&rid,))?;
        let known = st
            .getattr("__dict__")?
            .call_method1("get", ("_known_looks",))?;
        if known.is_truthy()? {
            st.getattr("roof_looks")?
                .set_item(rid, known.get_item(0)?)?;
        } else {
            st.getattr("roof_looks")?.set_item(rid, "grey")?;
        }
    }
    Ok(())
}
#[pyfunction]
fn rail_synth<'p>(
    py: Python<'p>,
    st: &Obj<'p>,
    near: &Obj<'p>,
    tracks: &Obj<'p>,
    edge: f64,
    fit: bool,
) -> PyResult<Option<Obj<'p>>> {
    let rs = py.import("railstations")?;
    let bs = py.import("busstations")?;
    let hub = point(&st.getattr("hub")?)?;
    let mut best = None;
    let mut longest = f64::NEG_INFINITY;
    for tr in tracks.try_iter()? {
        let tr = tr?;
        let path = points(tr.get_item(1)?.extract()?)?;
        if distance(&path, hub, false) < 60. {
            let d = length(&path);
            if d > longest {
                longest = d;
                best = Some((tr, path));
            }
        }
    }
    let Some((tr, path)) = best else {
        return Ok(None);
    };
    let (f, _, _) = foot(&path, hub);
    let s = arc(&path);
    let mut at = 0.;
    let mut dist = 1e18;
    for (i, w) in path.windows(2).enumerate() {
        let d = w[1] - w[0];
        let t = ((f - w[0]).dot(d) / d.dot(d).max(1e-9)).clamp(0., 1.);
        let gap = (w[0] + d * t - f).norm();
        if gap < dist {
            dist = gap;
            at = s[i] + t * d.norm();
        }
    }
    let len = if st.getattr("kind")?.eq("station")? {
        250.
    } else {
        150.
    };
    let lo = (at - len / 2.).max(0.);
    let hi = (at + len / 2.).min(*s.last().unwrap());
    if hi - lo < 30. {
        return Ok(None);
    }
    let ss = np_arange(lo, hi + 1e-6, 2.);
    let mut line = sample(&path, &s, &ss);
    let mut normal = normals(&line);
    let mut side = 1.;
    let mut closest = f64::INFINITY;
    for b in st.getattr("buildings")?.try_iter()? {
        let bc = point(&bs.call_method1("centroid", (b?.get_item(1)?,))?)?;
        let d = (bc - hub).norm();
        if d < closest {
            closest = d;
            let i = line.len() / 2;
            side = if (bc - line[i]).dot(normal[i]) >= 0. {
                1.
            } else {
                -1.
            };
        }
    }
    if fit {
        let n: Vec<_> = normal.iter().map(|v| *v * side).collect();
        let keep = rs.call_method1(
            "_off_roads",
            (
                array(py, &line),
                array(py, &n),
                edge,
                near,
                ss.into_pyarray(py),
                at,
            ),
        )?;
        if keep.is_none() {
            return Ok(None);
        }
        let subset = |v: &[Point]| -> PyResult<Vec<Point>> {
            points(array(py, v).get_item(&keep)?.extract()?)
        };
        line = subset(&line)?;
        normal = subset(&normal)?;
    }
    let mut ring: Vec<_> = line
        .iter()
        .zip(&normal)
        .map(|(&p, &n)| p + n * side * edge)
        .collect();
    ring.extend(
        line.iter()
            .zip(&normal)
            .rev()
            .map(|(&p, &n)| p + n * side * (edge + 4.)),
    );
    let ring = ccw(&ring);
    let pl = rs.call_method1(
        "Platform",
        (
            format!("synth:{}", tr.get_item(0)?.str()?),
            array(py, &ring),
            PyDict::new(py),
            false,
        ),
    )?;
    let snapped: Vec<_> = ring
        .iter()
        .map(|&p| (distance(&path, p, false) - edge).abs() < 0.05)
        .collect();
    pl.setattr(
        "tidx",
        snapped
            .iter()
            .map(|&b| if b { 0i64 } else { -1 })
            .collect::<Vec<_>>()
            .into_pyarray(py),
    )?;
    pl.setattr("snapped", snapped.into_pyarray(py))?;
    pl.setattr("tracks", vec![tr])?;
    Ok(Some(pl))
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rail_canopy_look, m)?)?;
    m.add_function(wrap_pyfunction!(rail_canopies, m)?)?;
    m.add_function(wrap_pyfunction!(rail_synth, m)?)
}

#[pyfunction]
fn rail_canopy_look<'p>(py: Python<'p>, colour: &Obj<'p>, material: &Obj<'p>) -> PyResult<Obj<'p>> {
    let rs = py.import("railstations")?;
    if colour.is_truthy()? {
        let rgb = py
            .import("buildings")?
            .call_method1("parse_color", (colour.str()?,))?;
        if !rgb.is_none() {
            let rgb: Vec<f64> = rgb.extract()?;
            let palette = rs.getattr("CANOPY_PALETTE")?;
            let mut best = None;
            let mut min = f64::INFINITY;
            for key in palette.try_iter()? {
                let key = key?;
                let v: Vec<f64> = palette.get_item(&key)?.extract()?;
                let mut distance = 0.;
                for (a, b) in v.iter().zip(&rgb) {
                    let d = a - b;
                    distance += d * d;
                }
                if best.is_none() || distance < min {
                    best = Some(key);
                    min = distance;
                }
            }
            return best
                .ok_or_else(|| pyo3::exceptions::PyValueError::new_err("empty canopy palette"));
        }
    }
    let m = if material.is_truthy()? {
        material.str()?.extract::<String>()?.trim().to_lowercase()
    } else {
        String::new()
    };
    rs.getattr("ROOF_MATERIAL_LOOK")?
        .call_method1("get", (m, rs.getattr("CANOPY_DEFAULT")?))
}

pub(super) fn item_look<'p>(it: &Obj<'p>) -> PyResult<(Obj<'p>, bool)> {
    let tags = it.call_method1("get", ("tags",))?;
    let first = |field: &str, keys: &[&str]| -> PyResult<Obj<'p>> {
        let mut v = it.call_method1("get", (field,))?;
        if !v.is_truthy()? && tags.is_truthy()? {
            for key in keys {
                v = tags.call_method1("get", (*key,))?;
                if v.is_truthy()? {
                    break;
                }
            }
        }
        Ok(v)
    };
    let colour = first("roof_color", &["roof:colour", "colour", "building:colour"])?;
    let material = first("roof_material", &["roof:material", "material"])?;
    Ok((
        rail_canopy_look(it.py(), &colour, &material)?,
        colour.is_truthy()? || material.is_truthy()?,
    ))
}
