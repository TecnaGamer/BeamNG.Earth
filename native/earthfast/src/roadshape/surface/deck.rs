//! Bridge carriageways are partitioned by elevation before lane_surface owns their faces.
//! Never partition a stacked crossing in the XY plane.
use super::{add, cross, dot, lerp, mul, norm, sub, unit, P2};
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::BTreeMap;

type P3 = [f64; 3];
fn xy(p: P3) -> P2 {
    [p[0], p[1]]
}

struct Profile {
    p: Vec<P3>,
    width: f64,
    section: Vec<P3>,
    bounds: [f64; 4],
    nearby: BTreeMap<(i64, i64), Vec<usize>>,
}

impl Profile {
    /// Invert a ribbon segment's actual cross-sections. A point on a skew
    /// end can project metres along the centreline, even onto another segment.
    fn section_sample(&self, i: usize, q: P2) -> Option<(f64, f64)> {
        let (a, b) = (self.p[i], self.p[i + 1]);
        let (l, r) = (self.section[i], self.section[i + 1]);
        let offset = |t| sub(q, lerp(xy(a), xy(b), t));
        let axis = |t| lerp(xy(l), xy(r), t);
        let f0 = cross(offset(0.0), xy(l));
        let f1 = cross(offset(1.0), xy(r));
        let t = if f0.abs() <= 1e-10 {
            0.0
        } else if f1.abs() <= 1e-10 {
            1.0
        } else if f0.is_sign_negative() == f1.is_sign_negative() {
            return None;
        } else if xy(l) == xy(r) {
            f0 / (f0 - f1)
        } else {
            let (mut lo, mut hi) = (0.0, 1.0);
            for _ in 0..36 {
                let mid = (lo + hi) * 0.5;
                if cross(offset(mid), axis(mid)).is_sign_negative() == f0.is_sign_negative() {
                    lo = mid;
                } else {
                    hi = mid;
                }
            }
            (lo + hi) * 0.5
        };
        let v = offset(t);
        let ax = axis(t);
        let along = dot(v, ax) / dot(ax, ax).max(1e-20);
        Some((
            dot(v, v),
            a[2] + t * (b[2] - a[2]) + along * (l[2] + t * (r[2] - l[2])),
        ))
    }

    fn sample(&self, q: P2) -> (f64, f64) {
        let mut best = (f64::INFINITY, 0.0);
        let mut on_row = (f64::INFINITY, 0.0);
        let key = grid(q);
        let mut sample = |i: usize| {
            let seg = &self.p[i..=i + 1];
            let (a, b) = (xy(seg[0]), xy(seg[1]));
            let d = sub(b, a);
            let t = (dot(sub(q, a), d) / dot(d, d).max(1e-20)).clamp(0.0, 1.0);
            let v = sub(q, lerp(a, b, t));
            let d2 = dot(v, v);
            if d2 < best.0 {
                let mut z = seg[0][2] + t * (seg[1][2] - seg[0][2]);
                if !self.section.is_empty() {
                    let (s0, s1) = (self.section[i], self.section[i + 1]);
                    let ax = lerp(xy(s0), xy(s1), t);
                    z += dot(v, ax) / dot(ax, ax).max(1e-20) * (s0[2] + t * (s1[2] - s0[2]));
                }
                best = (d2, z);
            }
            if !self.section.is_empty() && d2 <= self.width * self.width * 2.0 {
                if let Some(hit) = self.section_sample(i, q) {
                    if hit.0 < on_row.0 {
                        on_row = hit;
                    }
                }
            }
        };
        if let Some(ids) = self.nearby.get(&key) {
            for &i in ids {
                sample(i);
            }
        } else {
            for i in 0..self.p.len() - 1 {
                sample(i);
            }
        }
        (
            best.0,
            if on_row.0.is_finite() {
                on_row.1
            } else {
                best.1
            },
        )
    }
}

fn grid(p: P2) -> (i64, i64) {
    ((p[0] / 32.0).floor() as i64, (p[1] / 32.0).floor() as i64)
}

fn bins(a: P2, b: P2, pad: f64) -> impl Iterator<Item = (i64, i64)> {
    let (x0, y0) = grid([a[0].min(b[0]) - pad, a[1].min(b[1]) - pad]);
    let (x1, y1) = grid([a[0].max(b[0]) + pad, a[1].max(b[1]) + pad]);
    (x0..=x1).flat_map(move |x| (y0..=y1).map(move |y| (x, y)))
}

fn profiles(lines: Vec<PyReadonlyArray2<'_, f64>>, widths: &[f64]) -> PyResult<Vec<Profile>> {
    if widths.len() != lines.len() {
        return Err(PyValueError::new_err("deck profiles: width count"));
    }
    lines
        .into_iter()
        .zip(widths)
        .map(|(line, &width)| {
            let v = line.as_array();
            if !matches!(v.ncols(), 3 | 6)
                || v.nrows() < 2
                || !width.is_finite()
                || width <= 0.0
                || !v.iter().all(|f| f.is_finite())
            {
                return Err(PyValueError::new_err(
                    "deck profiles: expected finite XYZ and positive width",
                ));
            }
            let p: Vec<_> = v.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect();
            let mut bounds = [p[0][0], p[0][1], p[0][0], p[0][1]];
            for q in &p {
                for k in 0..2 {
                    bounds[k] = bounds[k].min(q[k] - width / 2.0);
                    bounds[k + 2] = bounds[k + 2].max(q[k] + width / 2.0);
                }
            }
            let section = if v.ncols() == 6 {
                v.rows().into_iter().map(|r| [r[3], r[4], r[5]]).collect()
            } else {
                Vec::new()
            };
            let mut nearby = BTreeMap::<_, Vec<_>>::new();
            for (i, seg) in p.windows(2).enumerate() {
                for cell in bins(xy(seg[0]), xy(seg[1]), width + 8.0) {
                    nearby.entry(cell).or_default().push(i);
                }
            }
            Ok(Profile {
                nearby,
                p,
                width,
                bounds,
                section,
            })
        })
        .collect()
}

fn apart(a: [f64; 4], b: [f64; 4], gap: f64) -> bool {
    (0..2).any(|k| a[k + 2] + gap < b[k] || b[k + 2] + gap < a[k])
}

/// Join only roads that can share pavement. Check every pair in a proposed
/// assembly: a ramp reaching both levels must not join the levels transitively.
#[pyfunction]
pub fn deck_surface_groups(
    lines: Vec<PyReadonlyArray2<'_, f64>>,
    widths: Vec<f64>,
    eligible: Vec<bool>,
    tile: [f64; 4],
) -> PyResult<Vec<Vec<usize>>> {
    let src = profiles(lines, &widths)?;
    if eligible.len() != src.len() {
        return Err(PyValueError::new_err("deck groups: eligibility count"));
    }
    let mut groups: Vec<Vec<usize>> = (0..src.len()).map(|i| vec![i]).collect();
    let mut conflicts = vec![vec![false; src.len()]; src.len()];
    let mut joins = Vec::new();
    for (i, a) in src.iter().enumerate().filter(|(i, _)| eligible[*i]) {
        for (j, b) in src[..i].iter().enumerate().filter(|(j, _)| eligible[*j]) {
            if apart(a.bounds, b.bounds, 3.0) {
                continue;
            }
            let mut gap = f64::INFINITY;
            let mut incompatible = false;
            let mut joined = false;
            let half = 0.5 * (a.width + b.width);
            for (road, other) in [(a, b), (b, a)] {
                let shared = |p: P3| {
                    [other.p[0], *other.p.last().unwrap()]
                        .iter()
                        .any(|q| norm(sub(xy(p), xy(*q))) < 0.6 && (p[2] - q[2]).abs() < 0.35)
                };
                let shared_start = shared(road.p[0]);
                let shared_end = shared(*road.p.last().unwrap());
                joined |= shared_start || shared_end;
                // Only the contiguous approach to a shared bearing can have
                // different source grades. A later crossing is a separate level.
                let mut at_joint = shared_start;
                let mut difference = 0.0_f64;
                let mut check = |row: P3| {
                    if !apart(
                        [row[0], row[1], row[0], row[1]],
                        other.bounds,
                        road.width * 0.5 + 3.0,
                    ) {
                        let (d2, z) = other.sample(xy(row));
                        let d = d2.sqrt();
                        if d < half + 3.0 {
                            gap = gap.min(d);
                            difference = difference.max((z - row[2]).abs());
                            return;
                        }
                    }
                    incompatible |= difference > if at_joint { 4.5 } else { 0.55 };
                    at_joint = false;
                    difference = 0.0;
                };
                check(road.p[0]);
                // Input may contain a two-point span. Its crossing is as
                // important as one on a densely sampled curved approach.
                for seg in road.p.windows(2) {
                    let length = norm(sub(xy(seg[1]), xy(seg[0])));
                    let steps = (length / half.max(2.0)).ceil() as usize;
                    for k in 1..steps {
                        let u = k as f64 / steps as f64;
                        check(std::array::from_fn(|n| {
                            seg[0][n] + (seg[1][n] - seg[0][n]) * u
                        }));
                    }
                    check(seg[1]);
                }
                incompatible |= difference > if at_joint || shared_end { 4.5 } else { 0.55 };
            }
            if incompatible {
                conflicts[i][j] = true;
                conflicts[j][i] = true;
            } else if gap.is_finite() {
                joins.push((!joined, i, j));
            }
        }
    }
    joins.sort_unstable();
    let mut owner: Vec<_> = (0..src.len()).collect();
    for (_, i, j) in joins {
        let (a, b) = (owner[i], owner[j]);
        if a == b
            || groups[a]
                .iter()
                .any(|&u| groups[b].iter().any(|&v| conflicts[u][v]))
        {
            continue;
        }
        for k in std::mem::take(&mut groups[b]) {
            owner[k] = a;
            groups[a].push(k);
        }
    }
    groups.retain(|g| {
        g.iter()
            .any(|&i| eligible[i] && !apart(src[i].bounds, tile, 12.0))
    });
    for g in &mut groups {
        g.sort_unstable();
    }
    groups.sort();
    Ok(groups)
}

/// Carry the through grade along a ramp starting at the same bearing.
/// Follow only the contiguous overlap from that bearing. A later crossing
/// or another deck directly over the ramp has no say in its profile.
#[pyfunction]
pub fn deck_ramp_offsets(
    lines: Vec<PyReadonlyArray2<'_, f64>>,
    widths: Vec<f64>,
    ramps: Vec<bool>,
) -> PyResult<Vec<Vec<f64>>> {
    let mut src = profiles(lines, &widths)?;
    if ramps.len() != src.len() {
        return Err(PyValueError::new_err("deck ramps: flag count"));
    }
    let mut result = Vec::new();
    // Chain stores can disagree by centimetres at the same internal bearing.
    // Use the median incident height and carry the correction through the
    // junction, easing back to each surveyed grade beyond the approach.
    let mut joints = Vec::new();
    for r in &src {
        let ends = [r.p[0], *r.p.last().unwrap()].map(|p| {
            let mut zs = Vec::new();
            for q in &src {
                for e in [q.p[0], *q.p.last().unwrap()] {
                    if norm(sub(xy(e), xy(p))) < 0.60 && (e[2] - p[2]).abs() < 0.50 {
                        zs.push(e[2]);
                    }
                }
            }
            zs.sort_by(f64::total_cmp);
            (zs[(zs.len() - 1) / 2] + zs[zs.len() / 2]) * 0.5 - p[2]
        });
        let arc = super::Line::new(r.p.iter().copied().map(xy).collect(), r.width).s;
        let reach = (3.0 * r.width).min(*arc.last().unwrap() * 0.5).max(0.001);
        let correction: Vec<_> = arc
            .iter()
            .map(|&s| {
                ends[0] * super::quintic((2.0 - s / reach).clamp(0.0, 1.0))
                    + ends[1]
                        * super::quintic((2.0 - (arc.last().unwrap() - s) / reach).clamp(0.0, 1.0))
            })
            .collect();
        joints.push(correction);
    }
    for (r, dzs) in src.iter_mut().zip(&joints) {
        for (p, dz) in r.p.iter_mut().zip(dzs) {
            p[2] += dz;
        }
    }
    for (i, r) in src.iter().enumerate() {
        let mut offsets = vec![0.0; r.p.len()];
        if ramps[i] {
            for rev in [false, true] {
                let idx = |k| if rev { r.p.len() - 1 - k } else { k };
                let end = r.p[idx(0)];
                let tang = unit(sub(xy(r.p[idx(1)]), xy(end)));
                let mut candidates = Vec::new();
                for (j, m) in src.iter().enumerate().filter(|(j, _)| !ramps[*j]) {
                    for (a, b) in [(0, 1), (m.p.len() - 1, m.p.len() - 2)] {
                        if norm(sub(xy(end), xy(m.p[a]))) > 0.6 || (end[2] - m.p[a][2]).abs() > 0.35
                        {
                            continue;
                        }
                        let alignment = dot(tang, unit(sub(xy(m.p[b]), xy(m.p[a]))));
                        if alignment > 0.65 {
                            candidates.push((alignment, j));
                        }
                    }
                }
                let Some((_, j)) = candidates.into_iter().max_by(|a, b| a.0.total_cmp(&b.0)) else {
                    continue;
                };
                let main = &src[j];
                let reach = (main.width + r.width) * 0.5;
                let far = r.p[idx(r.p.len() - 1)];
                let mut since_split = 0.0;
                let mut split_dz = None;
                for k in 1..r.p.len() - 1 {
                    let n = idx(k);
                    let (d2, z) = main.sample(xy(r.p[n]));
                    let span = (3.0 * r.width).max(20.0);
                    let dz = if d2.sqrt() <= reach + 1.0 && split_dz.is_none() {
                        z - r.p[n][2]
                    } else {
                        since_split += norm(sub(xy(r.p[n]), xy(r.p[idx(k - 1)])));
                        if since_split >= span {
                            break;
                        }
                        let last = *split_dz.get_or_insert(offsets[idx(k - 1)]);
                        last * super::quintic(1.0 - since_split / span)
                    };
                    // The abutment belongs to the ground approach. Leave its
                    // height unchanged even if the spans still overlap here.
                    let fade = (norm(sub(xy(far), xy(r.p[n]))) / r.width).min(1.0);
                    let dz = dz * super::quintic(fade);
                    if dz.abs() > f64::abs(offsets[n]) {
                        offsets[n] = dz;
                    }
                }
            }
        }
        for (v, j) in offsets.iter_mut().zip(&joints[i]) {
            *v += j;
        }
        result.push(offsets);
    }
    Ok(result)
}

// Every owner evaluates a common height at shared vertices. Compact transverse
// weights approach zero at the outer boundary of a neighbouring carriageway.
fn height(p: P2, src: &[Profile]) -> f64 {
    if src.len() == 1 {
        return src[0].sample(p).1;
    }
    let mut best = (f64::INFINITY, 0.0);
    let (mut weights, mut heights) = (0.0, 0.0);
    for road in src {
        if apart([p[0], p[1], p[0], p[1]], road.bounds, 4.0) {
            continue;
        }
        let (d2, z) = road.sample(p);
        if d2 < best.0 {
            best = (d2, z);
        }
        let reach = road.width * 0.5 + 2.0;
        let w = (1.0 - d2 / (reach * reach)).max(0.0);
        weights += w * w;
        heights += w * w * z;
    }
    if !best.0.is_finite() {
        src.iter()
            .map(|r| r.sample(p))
            .min_by(|a, b| a.0.total_cmp(&b.0))
            .unwrap()
            .1
    } else if weights == 0.0 {
        best.1
    } else {
        heights / weights
    }
}

#[pyfunction]
pub fn deck_surface_z<'py>(
    py: Python<'py>,
    points: PyReadonlyArray2<'py, f64>,
    lines: Vec<PyReadonlyArray2<'py, f64>>,
    widths: Vec<f64>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let src = profiles(lines, &widths)?;
    let p = points.as_array();
    if p.ncols() != 2 || src.is_empty() || !p.iter().all(|f| f.is_finite()) {
        return Err(PyValueError::new_err(
            "deck heights: invalid coordinates or empty profiles",
        ));
    }
    Ok(p.rows()
        .into_iter()
        .map(|r| height([r[0], r[1]], &src))
        .collect::<Vec<_>>()
        .into_pyarray(py))
}

/// The concrete lip supports the parapet outside the carriageway. Where
/// two distinct edges face each other across a narrow gore, reserve the
/// gap between their slabs. Each side can use at most a fifth of it.
#[pyfunction]
pub fn deck_slab_rows<'py>(
    py: Python<'py>,
    lines: Vec<PyReadonlyArray2<'_, f64>>,
    widths: Vec<f64>,
    lip: f64,
) -> PyResult<Vec<Bound<'py, PyArray1<f64>>>> {
    let src = profiles(lines, &widths)?;
    if lip < 0.0 || !lip.is_finite() || src.iter().any(|r| r.section.is_empty()) {
        return Err(PyValueError::new_err(
            "deck slab: expected cross-sections and a finite lip",
        ));
    }
    let mut result = Vec::new();
    for (i, r) in src.iter().enumerate() {
        let mut rows = Vec::new();
        for (&p, &section) in r.p.iter().zip(&r.section) {
            let axis = xy(section);
            let n = unit(axis);
            let extension = |side: f64| {
                let q = add(xy(p), mul(axis, side));
                let mut extra = lip;
                for (j, other) in src.iter().enumerate() {
                    if j == i || apart([q[0], q[1], q[0], q[1]], other.bounds, 5.0 * lip) {
                        continue;
                    }
                    let (d2, z) = other.sample(q);
                    let gap = d2.sqrt() - other.width * 0.5;
                    if gap >= 0.0 && gap < 5.0 * lip && (z - p[2]).abs() < 1.0 {
                        extra = extra.min(gap * 0.20);
                    }
                }
                extra
            };
            let (l, h) = (extension(1.0), extension(-1.0));
            let mid = add(xy(p), mul(n, (l - h) * 0.5));
            let normal = mul(add(axis, mul(n, (l + h) * 0.5)), 2.0 / r.width);
            rows.extend([mid[0], mid[1], normal[0], normal[1]]);
        }
        result.push(rows.into_pyarray(py));
    }
    Ok(result)
}

/// Find free half-edges before the tile cut. At a welded lane row several short
/// half-edges can face one long one. Subtract the covered intervals; an edge
/// across a terminal profile row is an abutment and carries no barrier.
#[pyfunction]
pub fn deck_surface_edges<'py>(
    py: Python<'py>,
    points: PyReadonlyArray2<'py, f64>,
    tris: PyReadonlyArray2<'py, i64>,
    lines: Vec<PyReadonlyArray2<'py, f64>>,
    widths: Vec<f64>,
) -> PyResult<(Bound<'py, PyArray1<f64>>, Vec<usize>)> {
    let src = profiles(lines, &widths)?;
    let (p, t) = (points.as_array(), tris.as_array());
    if p.ncols() != 2
        || t.ncols() != 3
        || !p.iter().all(|v| v.is_finite())
        || t.iter().any(|&i| i < 0 || i as usize >= p.nrows())
    {
        return Err(PyValueError::new_err("deck edges: invalid mesh"));
    }
    let pos = |i: i64| [p[[i as usize, 0]], p[[i as usize, 1]]];
    let ends: Vec<_> = src
        .iter()
        .flat_map(|r| {
            let n = r.p.len();
            [(0, 1), (n - 1, n - 2)].map(|(a, b)| {
                let d = if r.section.is_empty() {
                    unit(sub(xy(r.p[b]), xy(r.p[a])))
                } else {
                    super::left(unit(xy(r.section[a])))
                };
                let w = if r.section.is_empty() {
                    r.width
                } else {
                    2.0 * norm(xy(r.section[a]))
                };
                (xy(r.p[a]), d, w)
            })
        })
        .collect();
    // Cancel identical directed geometric edges (the surface may duplicate
    // vertices at material or texture boundaries).
    let key = |v: P2| v.map(|q| (q * 1e6).round() as i64);
    let mut open = BTreeMap::new();
    for tri in t.rows() {
        for k in 0..3 {
            let (a, b) = (pos(tri[k]), pos(tri[(k + 1) % 3]));
            if key(a) != key(b) && open.remove(&(key(b), key(a))).is_none() {
                open.insert((key(a), key(b)), (a, b));
            }
        }
    }
    let boundary: Vec<_> = open.into_values().collect();
    let mut adjacent = BTreeMap::<_, Vec<_>>::new();
    for (i, &(a, b)) in boundary.iter().enumerate() {
        for cell in bins(a, b, 0.002) {
            adjacent.entry(cell).or_default().push(i);
        }
    }
    let mut neighbours = Vec::new();
    let mut free = Vec::new();
    for &(a, b) in &boundary {
        let dir = unit(sub(b, a));
        let len = norm(sub(b, a));
        if ends.iter().any(|&(q, d, w)| {
            dot(dir, d).abs() < 0.01
                && dot(sub(a, q), d).abs() < 0.002
                && norm(sub(lerp(a, b, 0.5), q)) <= w * 0.5 + 1.0
        }) {
            continue;
        }
        let mut covered = Vec::new();
        neighbours.clear();
        for cell in bins(a, b, 0.0) {
            neighbours.extend_from_slice(&adjacent[&cell]);
        }
        neighbours.sort_unstable();
        neighbours.dedup();
        for &i in &neighbours {
            let (c, d) = boundary[i];
            if dot(sub(d, c), dir) >= 0.0
                || cross(sub(c, a), dir).abs() > 0.0015
                || cross(sub(d, a), dir).abs() > 0.0015
            {
                continue;
            }
            let (lo, hi) = (dot(sub(d, a), dir).max(0.0), dot(sub(c, a), dir).min(len));
            if hi > lo {
                covered.push((lo, hi));
            }
        }
        covered.sort_by(|a, b| a.0.total_cmp(&b.0));
        let mut start = 0.0_f64;
        covered.push((len, len));
        for (lo, hi) in covered {
            if lo - start > 0.002 {
                let (u, v) = (add(a, mul(dir, start)), add(a, mul(dir, lo)));
                free.push((u, v));
            }
            start = start.max(hi);
        }
    }
    let mut by_start = BTreeMap::new();
    let mut by_end = BTreeMap::new();
    for (i, &(a, b)) in free.iter().enumerate() {
        by_start.entry(key(a)).or_insert_with(Vec::new).push(i);
        by_end.entry(key(b)).or_insert_with(Vec::new).push(i);
    }
    let mut used = vec![false; free.len()];
    let mut out = Vec::new();
    let mut offsets = vec![0];
    let starts = (0..free.len()).filter(|&i| !by_end.contains_key(&key(free[i].0)));
    for i in starts.chain(0..free.len()) {
        if used[i] {
            continue;
        }
        let mut chain = vec![free[i].0];
        let mut j = i;
        loop {
            used[j] = true;
            let q = free[j].1;
            chain.push(q);
            match by_start
                .get(&key(q))
                .and_then(|ids| ids.iter().find(|&&k| !used[k]))
            {
                Some(&k) => j = k,
                None => break,
            }
        }
        for (k, &p) in chain.iter().enumerate() {
            let tang = unit(sub(
                chain[(k + 1).min(chain.len() - 1)],
                chain[k.saturating_sub(1)],
            ));
            out.extend([p[0], p[1], height(p, &src), tang[1], -tang[0]]);
        }
        offsets.push(out.len() / 5);
    }
    Ok((out.into_pyarray(py), offsets))
}

/// Clip the lane graph rows to the stations carried by the through profiles.
/// Retain exact abutment rows even when a short span falls between S1 samples.
#[pyfunction]
pub fn deck_event_rows<'py>(
    py: Python<'py>,
    rows: PyReadonlyArray2<'py, f64>,
    offsets: PyReadonlyArray2<'py, f64>,
    lines: Vec<PyReadonlyArray2<'py, f64>>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (r, t) = (rows.as_array(), offsets.as_array());
    if r.ncols() != 3
        || r.nrows() < 2
        || t.nrows() != r.nrows()
        || t.ncols() < 2
        || !r.iter().chain(t.iter()).all(|v| v.is_finite())
    {
        return Err(PyValueError::new_err("deck event: invalid lane rows"));
    }
    let n = r.nrows();
    let pt = |i: usize| [r[[i, 1]], r[[i, 2]]];
    let station = |p: P2| {
        let mut best = (f64::INFINITY, 0.0);
        for i in 1..n {
            let d = sub(pt(i), pt(i - 1));
            let denom = dot(d, d);
            if denom < 1e-12 {
                continue;
            }
            let u = dot(sub(p, pt(i - 1)), d) / denom;
            let u = if i == 1 { u } else { u.max(0.0) };
            let u = if i + 1 == n { u } else { u.min(1.0) };
            let v = sub(p, lerp(pt(i - 1), pt(i), u));
            if dot(v, v) < best.0 {
                best = (dot(v, v), r[[i - 1, 0]] + u * (r[[i, 0]] - r[[i - 1, 0]]));
            }
        }
        best
    };
    let widths = vec![1.0; lines.len()];
    let src = profiles(lines, &widths)?;
    let mut spans = Vec::new();
    for road in &src {
        let a = station(xy(road.p[0]));
        let b = station(xy(*road.p.last().unwrap()));
        if a.0 > 64.0 || b.0 > 64.0 {
            continue;
        }
        let (lo, hi) = (a.1.min(b.1).max(r[[0, 0]]), a.1.max(b.1).min(r[[n - 1, 0]]));
        if lo < hi {
            spans.push((lo, hi));
        }
    }
    let mut out = Vec::new();
    spans.sort_by(|a, b| a.0.total_cmp(&b.0));
    if let Some(&(lo, mut hi)) = spans.first() {
        for &(a, b) in &spans[1..] {
            if a > hi + 0.10 {
                return Ok(out.into_pyarray(py));
            }
            hi = hi.max(b);
        }
        let mut st = vec![lo, hi];
        st.extend((0..n).map(|i| r[[i, 0]]).filter(|&s| s > lo && s < hi));
        st.sort_by(f64::total_cmp);
        st.dedup_by(|a, b| (*a - *b).abs() < 1e-6);
        for s in st {
            let i = (1..n).find(|&i| r[[i, 0]] >= s).unwrap_or(n - 1);
            let u = (s - r[[i - 1, 0]]) / (r[[i, 0]] - r[[i - 1, 0]]).max(1e-9);
            let p = lerp(pt(i - 1), pt(i), u);
            if !src.iter().any(|road| road.sample(p).0 < 64.0) {
                out.clear();
                break;
            }
            out.extend([s, p[0], p[1]]);
            for j in 0..t.ncols() {
                out.push(t[[i - 1, j]] + u * (t[[i, j]] - t[[i - 1, j]]));
            }
        }
    }
    Ok(out.into_pyarray(py))
}
