//! The junction owns its tangent cross section. Fit the end of the cut sidewalk
//! to that section before either tile takes its portion of the same street.
use crate::dm::Dm;
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

type XY = [f64; 2];
fn sub(a: XY, b: XY) -> XY {
    [a[0] - b[0], a[1] - b[1]]
}
fn dot(a: XY, b: XY) -> f64 {
    a[0] * b[0] + a[1] * b[1]
}
fn cross(a: XY, b: XY) -> f64 {
    a[0] * b[1] - a[1] * b[0]
}
fn len(d: XY) -> f64 {
    d[0].dhypot(d[1])
}
fn mix(a: XY, b: XY, t: f64) -> XY {
    [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]
}

#[pyfunction]
pub fn sidewalk_join_run<'py>(
    py: Python<'py>,
    pts: PyReadonlyArray2<'py, f64>,
    width: f64,
    side: f64,
    cuts: (bool, bool),
    ends: Vec<(XY, XY)>,
) -> PyResult<(Bound<'py, PyArray2<f64>>, Vec<Option<XY>>)> {
    let mut p = pts.as_array().to_owned();
    let n = p.nrows();
    if n < 2 || p.ncols() < 2 {
        return Err(PyValueError::new_err("expected a sidewalk polyline"));
    }
    let mut arc = vec![0.0; n];
    for i in 1..n {
        arc[i] = arc[i - 1] + (p[[i, 0]] - p[[i - 1, 0]]).dhypot(p[[i, 1]] - p[[i - 1, 1]]);
    }
    let reach = 4.0_f64.min(arc[n - 1] * 0.45);
    let mut normals = vec![None; 2];
    for (e, active, i) in [(0, cuts.0, 0), (1, cuts.1, n - 1)] {
        if !active || reach < 0.1 {
            continue;
        }
        let q = [p[[i, 0]], p[[i, 1]]];
        let mut best = None;
        let mut distance = reach.min(2.5);
        for &(curb, normal) in &ends {
            let mid = [
                curb[0] + width * 0.5 * normal[0],
                curb[1] + width * 0.5 * normal[1],
            ];
            let shift = sub(mid, q);
            if len(shift) < distance {
                distance = len(shift);
                best = Some((shift, normal));
            }
        }
        if let Some((shift, normal)) = best {
            for j in 0..n {
                let t = (1.0 - (arc[j] - arc[i]).abs() / reach).max(0.0);
                let wt = t * t * (3.0 - 2.0 * t);
                p[[j, 0]] += wt * shift[0];
                p[[j, 1]] += wt * shift[1];
            }
            normals[e] = Some([side * normal[0], side * normal[1]]);
        }
    }
    Ok((p.into_pyarray(py), normals))
}

// Closed mapped footprints and barrier lines. Bound the working set per run;
// classification and nearest-hit ties follow input order.
struct Boundary {
    pts: Vec<XY>,
    target: bool,
    closed: bool,
    lo: XY,
    hi: XY,
}

#[pyclass(module = "earthfast")]
pub struct TownFrontage {
    walls: Vec<Boundary>,
}

fn inside(q: XY, ring: &[XY]) -> bool {
    let mut hit = false;
    for (i, &a) in ring.iter().enumerate() {
        let b = ring[(i + 1) % ring.len()];
        if (a[1] > q[1]) != (b[1] > q[1])
            && q[0] < a[0] + (q[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
        {
            hit = !hit;
        }
    }
    hit
}

#[pymethods]
impl TownFrontage {
    #[new]
    fn new(shapes: Vec<(PyReadonlyArray2<'_, f64>, u8, bool)>) -> PyResult<Self> {
        let mut walls = Vec::new();
        let mut party_walls = std::collections::BTreeMap::new();
        let mut attached = std::collections::BTreeSet::new();
        for (ring, kind, closed) in shapes {
            let r = ring.as_array();
            if r.ncols() < 2 {
                return Err(PyValueError::new_err("expected xy boundaries"));
            }
            let pts: Vec<XY> = r.rows().into_iter().map(|q| [q[0], q[1]]).collect();
            if pts.len() < 2 {
                continue;
            }
            let mut lo = [f64::INFINITY; 2];
            let mut hi = [f64::NEG_INFINITY; 2];
            for p in &pts {
                for k in 0..2 {
                    lo[k] = lo[k].min(p[k]);
                    hi[k] = hi[k].max(p[k]);
                }
            }
            if kind == 2 {
                // Shared party walls identify terraced blocks even when the
                // building centres belong to the neighbouring road tile.
                let key = |p: XY| p.map(|v| (v * 20.0).round() as i64);
                for i in 0..pts.len() {
                    let (a, b) = (pts[i], pts[(i + 1) % pts.len()]);
                    if len(sub(a, b)) < 1.0 {
                        continue;
                    }
                    let (a, b) = (key(a), key(b));
                    if let Some(old) = party_walls.insert((a.min(b), a.max(b)), walls.len()) {
                        if old != walls.len() {
                            attached.insert(old);
                            attached.insert(walls.len());
                        }
                    }
                }
            }
            walls.push(Boundary {
                pts,
                target: kind == 1,
                closed,
                lo,
                hi,
            });
        }
        for i in attached {
            walls[i].target = true;
        }
        Ok(Self { walls })
    }

    /// Widths at curb stations, stored as a fraction of total length so that
    /// re-draping a geotile in another ENU frame preserves its facade breaks.
    fn profile(
        &self,
        curb: PyReadonlyArray2<'_, f64>,
        outward: PyReadonlyArray2<'_, f64>,
        width: f64,
        drives: Vec<(f64, f64)>,
        junction: (bool, bool),
    ) -> PyResult<Vec<(f64, f64)>> {
        let (c, n) = (curb.as_array(), outward.as_array());
        if c.ncols() < 2 || n.ncols() != 2 || c.nrows() != n.nrows() {
            return Err(PyValueError::new_err(
                "expected one outward vector per curb station",
            ));
        }
        let mut profile = Vec::new();
        let mut s = 0.0;
        let max_w = 8.0_f64.min(width + 6.0);
        for i in 1..c.nrows() {
            let a = [c[[i - 1, 0]], c[[i - 1, 1]]];
            let b = [c[[i, 0]], c[[i, 1]]];
            let delta = sub(b, a);
            let l = len(delta);
            if l < 1e-7 {
                continue;
            }
            let tangent = [delta[0] / l, delta[1] / l];
            let near: Vec<_> = self
                .walls
                .iter()
                .filter(|r| {
                    (0..2).all(|k| {
                        r.hi[k] >= a[k].min(b[k]) - 2.0 * max_w
                            && r.lo[k] <= a[k].max(b[k]) + 2.0 * max_w
                    })
                })
                .collect();
            let mut stations = vec![0.0, l];
            // At wall ends and driveway aprons the width changes on the
            // developed side of the boundary. Never interpolate across a yard.
            for wall in &near {
                for &p in &wall.pts {
                    let along = dot(sub(p, a), tangent);
                    if along > 0.0 && along < l && cross(tangent, sub(p, a)).abs() < 2.0 * max_w {
                        stations.push(along);
                    }
                }
            }
            for &(a, b) in &drives {
                for d in [a - 0.6 - s, b + 0.6 - s] {
                    if d > 0.0 && d < l {
                        stations.push(d);
                    }
                }
            }
            stations.sort_by(f64::total_cmp);
            stations.dedup_by(|a, b| (*a - *b).abs() < 1e-7);
            let query = |d: f64| {
                if drives
                    .iter()
                    .any(|&(a, b)| s + d >= a - 0.6 && s + d <= b + 0.6)
                {
                    return width;
                }
                let t = d / l;
                let q = mix(a, b, t);
                let dir = mix([n[[i - 1, 0]], n[[i - 1, 1]]], [n[[i, 0]], n[[i, 1]]], t);
                let back = [q[0] + width * dir[0], q[1] + width * dir[1]];
                let mut target = f64::INFINITY;
                let mut stop = f64::INFINITY;
                for wall in &near {
                    // A protected polygon already under the walk blocks growth.
                    if !wall.target && wall.closed && inside(back, &wall.pts) {
                        stop = stop.min(width);
                    }
                    let count = wall.pts.len();
                    for j in 0..if wall.closed { count } else { count - 1 } {
                        let p = wall.pts[j];
                        let edge = sub(wall.pts[(j + 1) % count], p);
                        let den = cross(dir, edge);
                        if den.abs() < 1e-9 {
                            continue;
                        }
                        let rel = sub(p, q);
                        let h = cross(rel, edge) / den;
                        let u = cross(rel, dir) / den;
                        if h <= 0.05 || h > max_w || !(0.0..=1.0).contains(&u) {
                            continue;
                        }
                        if wall.target && dot(tangent, edge).abs() >= 0.5 * len(edge) {
                            target = target.min(h);
                        } else {
                            stop = stop.min(h);
                        }
                    }
                }
                if target.is_finite() && target <= stop + 1e-6 {
                    target.max(0.4)
                } else {
                    width
                }
            };
            for ab in stations.windows(2) {
                let inset = 0.04_f64.min((ab[1] - ab[0]) * 0.25);
                for d in [
                    ab[0],
                    ab[0] + inset,
                    (ab[0] + ab[1]) * 0.5,
                    ab[1] - inset,
                    ab[1],
                ] {
                    let w = query(d);
                    let pos = s + d;
                    if let Some(last) = profile.last_mut() {
                        let (x, v): &mut (f64, f64) = last;
                        if (pos - *x).abs() < 1e-7 {
                            *v = w.min(*v);
                            continue;
                        }
                    }
                    profile.push((pos, w));
                }
            }
            s += l;
        }
        if !profile.iter().any(|&(_, w)| (w - width).abs() > 0.02) {
            return Ok(Vec::new());
        }
        let mut kept: Vec<(f64, f64)> = Vec::new();
        for (station, w) in profile {
            let t = (if junction.0 { station } else { 2.0 })
                .min(if junction.1 { s - station } else { 2.0 })
                .clamp(0.0, 1.0);
            let p = (
                (station / s).clamp(0.0, 1.0),
                (width + (w - width) * t).max(0.4),
            );
            // Drop redundant constant or collinear samples.
            while kept.len() >= 2 {
                let b = kept[kept.len() - 1];
                let a = kept[kept.len() - 2];
                if (b.1 - a.1 - (p.1 - a.1) * (b.0 - a.0) / (p.0 - a.0)).abs() > 1e-6 {
                    break;
                }
                kept.pop();
            }
            kept.push(p);
        }
        Ok(kept)
    }
}

/// Stations along the original road, before the tile or the junction trimmed it.
/// Both sidewalk sides together get WCUSA's point rates per km of street.
#[pyfunction]
pub fn town_marks(
    parent: PyReadonlyArray2<'_, f64>,
    run: PyReadonlyArray2<'_, f64>,
    fid: &str,
    side: i64,
) -> PyResult<Vec<[f64; 5]>> {
    let (p, r) = (parent.as_array(), run.as_array());
    if p.ncols() < 2 || r.ncols() < 2 || p.nrows() < 2 || r.nrows() < 2 {
        return Ok(vec![]);
    }
    let project = |i: usize| {
        let q = [r[[i, 0]], r[[i, 1]]];
        let (mut best, mut station, mut base) = (f64::INFINITY, 0., 0.);
        for j in 1..p.nrows() {
            let a = [p[[j - 1, 0]], p[[j - 1, 1]]];
            let b = [p[[j, 0]], p[[j, 1]]];
            let d = sub(b, a);
            let l = len(d);
            if l == 0. {
                continue;
            }
            let t = (dot(sub(q, a), d) / (l * l)).clamp(0., 1.);
            let diff = sub(q, mix(a, b, t));
            let dist = dot(diff, diff);
            if dist < best {
                best = dist;
                station = base + t * l;
            }
            base += l;
        }
        station
    };
    let (start, end) = (project(0), project(r.nrows() - 1));
    if (end - start).abs() < 0.1 {
        return Ok(vec![]);
    }
    let mut seed = 0xcbf29ce484222325u64;
    for b in fid.bytes() {
        seed = (seed ^ u64::from(b)).wrapping_mul(0x100000001b3);
    }
    if side < 0 {
        seed ^= 0xa74332a958da36b1;
    }
    let mut marks = Vec::new();
    for (kind, (rate, size)) in [(11.8, 1.4), (1.3, 1.8), (3.4, 1.3)]
        .into_iter()
        .enumerate()
    {
        let step = 2000. / rate;
        for bin in ((start.min(end) - size) / step).floor() as i64
            ..=((start.max(end) + size) / step).floor() as i64
        {
            let mut h = seed
                ^ ((bin as u64).wrapping_mul(0x9e3779b97f4a7c15))
                ^ (kind as u64 + 1).wrapping_mul(0x94d049bb133111eb);
            h = (h ^ (h >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
            h = (h ^ (h >> 27)).wrapping_mul(0x94d049bb133111eb);
            h ^= h >> 31;
            let middle = (bin as f64 + (h >> 11) as f64 / 9007199254740992.) * step;
            let a = (middle - size * 0.5 - start) / (end - start);
            let b = (middle + size * 0.5 - start) / (end - start);
            let lo = a.min(b).max(0.);
            let hi = a.max(b).min(1.);
            if hi > lo {
                marks.push([lo, hi, kind as f64, (lo - a) / (b - a), (hi - a) / (b - a)]);
            }
        }
    }
    Ok(marks)
}

/// Historic brick seen under shade or in faded aerials reads muted red-purple.
/// Only use this weaker colour signal in a region with established brick paving.
#[pyfunction]
pub fn town_brick(rgb: [f64; 3], regional: bool) -> bool {
    let [r, g, b] = rgb;
    regional && (0.18..0.85).contains(&r) && r - g > 0.04 && r - b > 0.024 && b - g < 0.055
}

/// A segment's own walking-surface tags take precedence over inference from
/// its streetscape. A road's setts only imply brick walks in historic brick regions.
#[pyfunction]
pub fn town_slab(tag: &str, road_material: &str, brick_region: bool) -> i64 {
    match tag {
        "bricks" | "brick" | "clinker" => 3,
        "paving_stones" | "pavers" | "pavers_mixed" | "slabs" | "concrete:plates" => 19,
        "sett" | "cobblestone" | "stone" | "unhewn_cobblestone" => 20,
        "" if brick_region
            && matches!(road_material, "sett" | "bricks" | "cobblestone" | "cobble") =>
        {
            3
        }
        _ => 1,
    }
}
