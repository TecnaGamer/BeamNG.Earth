//! Exact orthographic receiver clipping for bounded cliff contact strips.
use numpy::{PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::collections::{BTreeSet, HashMap};
type Point = [f64; 4];
type Poly = Vec<Point>;
const EPS: f64 = 1e-8;
fn area(p: &[Point]) -> f64 {
    if p.len() < 3 {
        return 0.;
    }
    (0..p.len())
        .map(|i| {
            let j = (i + 1) % p.len();
            p[i][0] * p[j][1] - p[i][1] * p[j][0]
        })
        .sum::<f64>()
        .abs()
        * 0.5
}
fn clip(p: &[Point], h: [f64; 5]) -> Poly {
    let mut out = Vec::new();
    if p.is_empty() {
        return out;
    }
    let distance = |v: &Point| h[4] + (0..4).map(|i| v[i] * h[i]).sum::<f64>();
    let mut a = p[p.len() - 1];
    let mut da = distance(&a);
    for &b in p {
        let db = distance(&b);
        if (da >= -EPS) != (db >= -EPS) {
            let t = da / (da - db);
            let mut v = [0.; 4];
            for k in 0..4 {
                v[k] = a[k] + t * (b[k] - a[k]);
            }
            out.push(v);
        }
        if db >= -EPS {
            out.push(b);
        }
        a = b;
        da = db;
    }
    out
}
fn edges(p: &[Point]) -> Vec<[f64; 5]> {
    let signed = (0..p.len())
        .map(|i| {
            let j = (i + 1) % p.len();
            p[i][0] * p[j][1] - p[i][1] * p[j][0]
        })
        .sum::<f64>();
    let s = if signed >= 0. { 1. } else { -1. };
    (0..p.len())
        .map(|i| {
            let a = p[i];
            let b = p[(i + 1) % p.len()];
            let dx = b[0] - a[0];
            let dy = b[1] - a[1];
            (dx.abs() + dy.abs() > EPS)
                .then_some([-dy * s, dx * s, 0., 0., (dy * a[0] - dx * a[1]) * s])
        })
        .flatten()
        .collect()
}
fn intersect(mut p: Poly, b: &[Point]) -> Poly {
    for h in edges(b) {
        p = clip(&p, h);
        if p.len() < 3 {
            break;
        }
    }
    p
}
fn subtract(p: Poly, b: &[Point]) -> Vec<Poly> {
    let mut r = p;
    let mut out = Vec::new();
    for h in edges(b) {
        let q = clip(&r, h.map(|v| -v));
        if area(&q) > EPS {
            out.push(q);
        }
        r = clip(&r, h);
        if area(&r) <= EPS {
            break;
        }
    }
    out
}
fn bounds(p: &[Point]) -> [f64; 4] {
    let mut b = [
        f64::INFINITY,
        f64::INFINITY,
        f64::NEG_INFINITY,
        f64::NEG_INFINITY,
    ];
    for v in p {
        for k in 0..2 {
            b[k] = b[k].min(v[k]);
            b[k + 2] = b[k + 2].max(v[k]);
        }
    }
    b
}
fn overlap(a: [f64; 4], b: [f64; 4]) -> bool {
    a[2] > b[0] + EPS && b[2] > a[0] + EPS && a[3] > b[1] + EPS && b[3] > a[1] + EPS
}
/// Return independent triangle corners [x,y,depth,threshold,receiver]. All clipping runs outside the GIL.
#[pyfunction]
pub fn cliff_strip_project(
    py: Python<'_>,
    receivers: PyReadonlyArray2<'_, f64>,
    domains: PyReadonlyArray2<'_, f64>,
    limit: usize,
) -> PyResult<Vec<[f64; 5]>> {
    if receivers.shape()[1] != 9 || domains.shape()[1] != 12 || limit == 0 {
        return Err(PyValueError::new_err(
            "expected Nx9 receivers, Nx12 domains and positive limit",
        ));
    }
    let r = receivers.as_array();
    let d = domains.as_array();
    py.allow_threads(move || {
        let mut shapes: Vec<Poly> = Vec::new();
        let mut planes = Vec::new();
        let mut bins: HashMap<(i32, i32), Vec<usize>> = HashMap::new();
        for (id, row) in r.rows().into_iter().enumerate() {
            if row.iter().any(|v| !v.is_finite()) {
                return Err(PyValueError::new_err("nonfinite receiver"));
            }
            let p: Poly = (0..3)
                .map(|k| [row[k * 3], row[k * 3 + 1], row[k * 3 + 2], 0.])
                .collect();
            let a = [p[1][0] - p[0][0], p[1][1] - p[0][1], p[1][2] - p[0][2]];
            let b = [p[2][0] - p[0][0], p[2][1] - p[0][1], p[2][2] - p[0][2]];
            let det = a[0] * b[1] - a[1] * b[0];
            let mut h = [0.; 3];
            if det.abs() > 1e-7 {
                h[0] = (a[2] * b[1] - a[1] * b[2]) / det;
                h[1] = (a[0] * b[2] - a[2] * b[0]) / det;
                h[2] = p[0][2] - h[0] * p[0][0] - h[1] * p[0][1];
                let bb = bounds(&p);
                let x0 = (bb[0] / 2.).floor() as i32;
                let x1 = (bb[2] / 2.).floor() as i32;
                let y0 = (bb[1] / 2.).floor() as i32;
                let y1 = (bb[3] / 2.).floor() as i32;
                if (x1 as i64 - x0 as i64 + 1) * (y1 as i64 - y0 as i64 + 1) > 100000 {
                    return Err(PyValueError::new_err("receiver exceeds projection bounds"));
                }
                for x in x0..=x1 {
                    for y in y0..=y1 {
                        bins.entry((x, y)).or_default().push(id);
                    }
                }
            }
            shapes.push(p);
            planes.push(h);
        }
        let mut out = Vec::new();
        for row in d.rows() {
            if row.iter().any(|v| !v.is_finite()) {
                return Err(PyValueError::new_err("nonfinite domain"));
            }
            let domain: Poly = (0..3)
                .map(|k| [row[k * 4], row[k * 4 + 1], row[k * 4 + 2], row[k * 4 + 3]])
                .collect();
            let bb = bounds(&domain);
            let mut ids = BTreeSet::new();
            let x0 = (bb[0] / 2.).floor() as i32;
            let x1 = (bb[2] / 2.).floor() as i32;
            let y0 = (bb[1] / 2.).floor() as i32;
            let y1 = (bb[3] / 2.).floor() as i32;
            if (x1 as i64 - x0 as i64 + 1) * (y1 as i64 - y0 as i64 + 1) > 100000 {
                return Err(PyValueError::new_err("domain exceeds projection bounds"));
            }
            for x in x0..=x1 {
                for y in y0..=y1 {
                    if let Some(v) = bins.get(&(x, y)) {
                        ids.extend(v.iter().copied());
                    }
                }
            }
            let mut regions = Vec::new();
            for id in ids {
                let h = planes[id];
                let p = intersect(domain.clone(), &shapes[id]);
                let p = clip(&p, [h[0], h[1], 0., -1., h[2] + 12.]);
                let p = clip(&p, [-h[0], -h[1], 0., 1., 12. - h[2]]);
                if area(&p) > EPS {
                    let b = bounds(&p);
                    regions.push((id, p, b));
                }
            }
            for (id, p, bb) in &regions {
                let mut fragments = vec![p.clone()];
                for (other, b, bounds_other) in &regions {
                    if other == id || !overlap(*bb, *bounds_other) {
                        continue;
                    }
                    let delta = [
                        planes[*other][0] - planes[*id][0],
                        planes[*other][1] - planes[*id][1],
                        planes[*other][2] - planes[*id][2],
                    ];
                    let occluder = if delta.iter().all(|v| v.abs() < EPS) {
                        if other > id {
                            continue;
                        }
                        b.clone()
                    } else {
                        clip(b, [delta[0], delta[1], 0., 0., delta[2]])
                    };
                    if area(&occluder) <= EPS {
                        continue;
                    }
                    let mut visible = Vec::new();
                    for fragment in fragments {
                        if area(&intersect(fragment.clone(), &occluder)) <= EPS {
                            visible.push(fragment);
                        } else {
                            visible.extend(subtract(fragment, &occluder));
                        }
                    }
                    fragments = visible;
                    if fragments.is_empty() {
                        break;
                    }
                }
                let h = planes[*id];
                for p in fragments {
                    for k in 1..p.len() - 1 {
                        let mut corners = [p[0], p[k], p[k + 1]];
                        let signed = (corners[1][0] - corners[0][0])
                            * (corners[2][1] - corners[0][1])
                            - (corners[1][1] - corners[0][1]) * (corners[2][0] - corners[0][0]);
                        if signed.abs() <= EPS {
                            continue;
                        }
                        if signed < 0. {
                            corners.swap(1, 2);
                        }
                        if out.len() / 3 >= limit {
                            return Err(PyValueError::new_err("strip triangle budget exceeded"));
                        }
                        for v in corners {
                            out.push([
                                v[0],
                                v[1],
                                v[0] * h[0] + v[1] * h[1] + h[2],
                                v[2].clamp(0., 1.),
                                *id as f64,
                            ]);
                        }
                    }
                }
            }
        }
        Ok(out)
    })
}
