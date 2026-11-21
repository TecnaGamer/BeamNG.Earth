//! Smooth the bed through crossings, insert the exact deck edges, and lay sleepers
//! at stations measured from the beginning of the feature.
use super::*;
use numpy::{PyArray1, PyReadonlyArray2};
use std::collections::BTreeMap;
fn check(ok: bool) -> PyResult<()> {
    if ok {
        Ok(())
    } else {
        Err(PyValueError::new_err(
            "rail profile and sample lengths differ",
        ))
    }
}
#[pyfunction]
fn rail_flush<'p>(
    py: Python<'p>,
    s: Vec<f64>,
    zb: Vec<f64>,
    blk: Vec<bool>,
    road: Vec<f64>,
) -> PyResult<Bound<'p, PyArray1<f64>>> {
    check(
        s.len() == zb.len()
            && s.len() == blk.len()
            && blk.iter().filter(|&&b| b).count() == road.len(),
    )?;
    if road.is_empty() {
        return Err(PyValueError::new_err(
            "zero-size array to reduction operation minimum which has no identity",
        ));
    }
    let head = 0.3 - 0.08 + 0.2 + 0.16;
    let hits: Vec<_> = blk
        .iter()
        .enumerate()
        .filter(|(_, b)| **b)
        .zip(road)
        .map(|((i, _), z)| {
            let target = z + 0.03 - head;
            let delta = target - zb[i];
            let grade = f64::max(1. / 60., delta.abs() / 40.);
            (s[i], target, delta, grade)
        })
        .collect();
    let result = s
        .iter()
        .zip(zb)
        .map(|(s, z)| {
            let mut low = f64::INFINITY;
            let mut high = f64::NEG_INFINITY;
            for &(a, t, d, g) in &hits {
                let dist = (s - a).abs() * g;
                if d < 0. {
                    low = low.min(t + dist)
                } else if d > 0. {
                    high = high.max(t - dist)
                }
            }
            if z.is_nan() {
                z
            } else {
                z.max(high).min(low)
            }
        })
        .collect::<Vec<_>>();
    Ok(result.into_pyarray(py))
}
#[pyfunction]
fn rail_insert_edges<'p>(
    py: Python<'p>,
    xy: Obj<'p>,
    zb: Obj<'p>,
    rec: &Obj<'p>,
) -> PyResult<(Obj<'p>, Obj<'p>, Obj<'p>, Obj<'p>)> {
    let p = points(xy.extract()?)?;
    let z: Vec<f64> = zb.extract()?;
    let mut mask: Vec<bool> = rec.get_item("blk")?.extract()?;
    let n = p.len();
    check(z.len() == n && mask.len() == n)?;
    let mut add = BTreeMap::new();
    let runs: Vec<[usize; 2]> = rec.get_item("runs")?.extract()?;
    let start: Vec<f64> = rec.get_item("start_t")?.extract()?;
    let end: Vec<f64> = rec.get_item("end_t")?.extract()?;
    for (([a, b], t0), t1) in runs.into_iter().zip(start).zip(end) {
        check(a < n && b < n)?;
        if a > 0 {
            if t0 > 0. && t0 < 1. {
                add.insert(a - 1, t0);
            } else if t0 >= 1. {
                mask[a] = false;
            }
        }
        if b + 1 < n {
            if t1 > 0. && t1 < 1. {
                add.insert(b, t1);
            } else if t1 <= 0. {
                mask[b] = false;
            }
        }
    }
    if add.is_empty() {
        return Ok((
            xy,
            zb,
            mask.into_pyarray(py).into_any(),
            (0..n as i64)
                .collect::<Vec<_>>()
                .into_pyarray(py)
                .into_any(),
        ));
    }
    let (mut q, mut zz, mut b, mut src) = (vec![], vec![], vec![], vec![]);
    for i in 0..n {
        q.push(p[i]);
        zz.push(z[i]);
        b.push(mask[i]);
        src.push(i as i64);
        if let Some(&t) = add.get(&i) {
            q.push(p[i] + (p[i + 1] - p[i]) * t);
            zz.push(z[i] + (z[i + 1] - z[i]) * t);
            b.push(false);
            src.push(-1);
        }
    }
    Ok((
        array(py, &q).into_any(),
        zz.into_pyarray(py).into_any(),
        b.into_pyarray(py).into_any(),
        src.into_pyarray(py).into_any(),
    ))
}
#[pyfunction]
fn rail_sleepers<'p>(
    py: Python<'p>,
    s: Vec<f64>,
    xy: PyReadonlyArray2<'p, f64>,
    u: PyReadonlyArray2<'p, f64>,
    top: Vec<f64>,
    i: usize,
    j: usize,
    gone: f64,
) -> PyResult<Option<Obj<'p>>> {
    let p = points(xy)?;
    let u = points(u)?;
    check(
        i < s.len()
            && j < s.len()
            && p.len() == s.len()
            && u.len() == s.len()
            && top.len() == s.len(),
    )?;
    let mut ss = crate::railfit::np_arange((s[i] / 0.6).ceil() * 0.6, s[j], 0.6);
    if gone > 0. {
        ss.retain(|t| {
            let k = (t / 0.6).round_ties_even() as u32;
            k.wrapping_mul(2654435761) as f64 / 4294967296. >= gone
        });
    }
    if ss.is_empty() {
        return Ok(None);
    }
    let interp = |v: &[f64]| crate::dm::interp(&ss, &s, v, None, None);
    let sample = |line: &[Point]| {
        let x = interp(&line.iter().map(|p| p.0).collect::<Vec<_>>());
        let y = interp(&line.iter().map(|p| p.1).collect::<Vec<_>>());
        x.into_iter()
            .zip(y)
            .map(|(x, y)| Point(x, y))
            .collect::<Vec<_>>()
    };
    let c = sample(&p);
    let across: Vec<_> = sample(&u)
        .into_iter()
        .map(|n| n / n.norm().max(1e-9))
        .collect();
    let along: Vec<_> = across.iter().map(|p| Point(p.1, -p.0)).collect();
    let z0: Vec<_> = interp(&top).iter().map(|z| z - 0.08).collect();
    let z1 = z0.iter().map(|z| z + 0.2).collect::<Vec<_>>();
    Ok(Some(
        (
            array(py, &c),
            array(py, &along),
            array(py, &across),
            z0.into_pyarray(py),
            z1.into_pyarray(py),
        )
            .into_pyobject(py)?
            .into_any(),
    ))
}
#[pyfunction]
fn rail_switch_masks<'p>(
    py: Python<'p>,
    rails: &Obj<'p>,
) -> PyResult<Vec<Bound<'p, PyArray1<bool>>>> {
    let mut tracks = vec![];
    let mut grid: BTreeMap<(i64, i64), Vec<(usize, usize)>> = BTreeMap::new();
    let cell = |p: Point| ((p.0 / 1.7).floor() as i64, (p.1 / 1.7).floor() as i64);
    for (i, r) in rails.try_iter()?.enumerate() {
        let r = r?;
        let p = points(r.get_item(3)?.extract()?)?;
        let tram = r.get_item(1)?.eq("tram")?;
        if p.len() < 2 {
            return Err(PyValueError::new_err(
                "Shape of array too small to calculate a numerical gradient",
            ));
        }
        let (normals, _) = line_axes(&p);
        if !tram {
            for (k, &pt) in p.iter().enumerate() {
                grid.entry(cell(pt)).or_default().push((i, k));
            }
        }
        tracks.push((p, normals, tram));
    }
    let mut out = vec![];
    for (i, (p, dirs, tram)) in tracks.iter().enumerate() {
        let mut mask = vec![false; p.len()];
        if !tram {
            for (j, &pt) in p.iter().enumerate() {
                let (x, y) = cell(pt);
                'near: for dx in -1..=1 {
                    for dy in -1..=1 {
                        if let Some(others) =
                            grid.get(&(x.saturating_add(dx), y.saturating_add(dy)))
                        {
                            for &(k, l) in others {
                                if i == k {
                                    continue;
                                }
                                let (pts, other, _) = &tracks[k];
                                let d = pts[l] - pt;
                                if crate::py_hypot(d.0, d.1) < 1.7
                                    && (dirs[j].1 * -other[l].0 - (-dirs[j].0) * other[l].1).abs()
                                        >= 0.087
                                {
                                    mask[j] = true;
                                    break 'near;
                                }
                            }
                        }
                    }
                }
            }
        }
        out.push(mask.into_pyarray(py));
    }
    Ok(out)
}
#[pyfunction]
fn rail_foot_limits<'p>(
    py: Python<'p>,
    xy: PyReadonlyArray2<'p, f64>,
    side: PyReadonlyArray2<'p, f64>,
    foot: Vec<f64>,
    others: &Obj<'p>,
    crossers: &Obj<'p>,
) -> PyResult<Bound<'p, PyArray1<f64>>> {
    let p = points(xy)?;
    let side = points(side)?;
    let mut lim = foot;
    check(lim.len() == p.len() && side.len() == p.len())?;
    let mut clip = |line: Vec<Point>, extra: f64| {
        if line.len() < 2 {
            return;
        }
        let bbox = line.iter().fold(
            [
                f64::INFINITY,
                f64::INFINITY,
                f64::NEG_INFINITY,
                f64::NEG_INFINITY,
            ],
            |[a, b, c, d], q| [a.min(q.0), b.min(q.1), c.max(q.0), d.max(q.1)],
        );
        for (i, &pt) in p.iter().enumerate() {
            if !contains(expanded(bbox, lim[i] + (extra + 1.)), pt) {
                continue;
            }
            let mut best = f64::INFINITY;
            let mut near = Point::default();
            for w in line.windows(2) {
                let d = w[1] - w[0];
                let t = ((pt - w[0]).dot(d) / d.dot(d).max(1e-9)).clamp(0., 1.);
                let q = w[0] + d * t;
                let v = (q - pt).norm();
                if v < best {
                    best = v;
                    near = q;
                }
            }
            let edge = best - extra;
            if (near - pt).dot(side[i]) > 0. && edge > 0. && edge < lim[i] {
                lim[i] = edge;
            }
        }
    };
    for c in crossers.try_iter()? {
        let c = c?;
        clip(
            points(c.get_item(0)?.extract()?)?,
            c.get_item(1)?.extract::<f64>()? + 0.5,
        );
    }
    for t in others.try_iter()? {
        clip(points(t?.get_item(1)?.extract()?)?, 2.4 + 1.);
    }
    for v in &mut lim {
        if *v < 1.8 + 0.01 {
            *v = 1.8 + 0.01
        }
    }
    Ok(lim.into_pyarray(py))
}
pub(super) fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(rail_sleepers, m)?)?;
    m.add_function(wrap_pyfunction!(rail_flush, m)?)?;
    m.add_function(wrap_pyfunction!(rail_insert_edges, m)?)?;
    m.add_function(wrap_pyfunction!(rail_switch_masks, m)?)?;
    m.add_function(wrap_pyfunction!(rail_foot_limits, m)?)
}
