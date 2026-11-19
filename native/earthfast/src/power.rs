//! High-voltage power lines (the user, 2026-10-26: "Is there a map of all power lines and how they connect? Currently
//! we've been placing them almost everywhere" / "start with 1"): OpenStreetMap's power=line ways (Overture class
//! power_line) as lattice towers at their vertices (the mapped towers) with the conductors hung between them, ending
//! at gantries inside the substations they reach; the substations' yards filled with switchgear and fenced.
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use std::collections::{BTreeMap, BTreeSet, HashSet};

#[derive(Clone, Copy)]
struct RoadSegment {
    a: [f64; 2],
    d: [f64; 2],
    half: f64,
    length2: f64,
    last: bool,
}

impl RoadSegment {
    fn t(&self, p: [f64; 2]) -> f64 {
        (((p[0] - self.a[0]) * self.d[0] + (p[1] - self.a[1]) * self.d[1]) / self.length2).clamp(0.0, 1.0)
    }
}

fn road_margin(p: [f64; 2], roads: &[RoadSegment], clear: f64) -> f64 {
    let mut best = f64::INFINITY;
    for s in roads {
        let t = s.t(p);
        let x = (p[0] - s.a[0]) - t * s.d[0];
        let y = (p[1] - s.a[1]) - t * s.d[1];
        best = best.min((x.dhypot(y) - s.half) - clear);
    }
    best
}

/// First try directly away from the closest carriageway edge, as powerlines._off_roads did.
/// Ordered segments preserve both the first nearest segment and the first road on a tie.
fn road_candidate(p: [f64; 2], roads: &[RoadSegment], clear: f64) -> [f64; 2] {
    let mut best = f64::INFINITY;
    let mut out = p;
    let mut nearest = f64::INFINITY;
    let mut near_foot = p;
    let mut near_direction = [0.0, 0.0];
    for s in roads {
        let t = s.t(p);
        let foot = [s.a[0] + t * s.d[0], s.a[1] + t * s.d[1]];
        let away = [p[0] - foot[0], p[1] - foot[1]];
        let distance = away[0].dhypot(away[1]);
        if distance < nearest {
            nearest = distance;
            near_foot = foot;
            near_direction = s.d;
        }
        if s.last {
            if nearest - s.half < best {
                best = nearest - s.half;
                let direction = if nearest > 1e-6 {
                    [(p[0] - near_foot[0]) / nearest, (p[1] - near_foot[1]) / nearest]
                } else {
                    let norm = near_direction[0].dhypot(near_direction[1]).max(1e-9);
                    [-near_direction[1] / norm, near_direction[0] / norm]
                };
                out = [near_foot[0] + direction[0] * (s.half + clear),
                       near_foot[1] + direction[1] * (s.half + clear)];
            }
            nearest = f64::INFINITY;
        }
    }
    out
}

/// Move one tower/pole off all nearby roads in one native call. roads: (n, 5), ax/ay/dx/dy/half-width;
/// ends: exclusive segment end of each road (each road has one half-width); around: (k, 2), the caller's
/// ordered search offsets, retained exactly (including its trig rounding).
/// Avoid point-by-segment NumPy matrices and stop at the first acceptable candidate. Most points need no search.
#[pyfunction]
pub fn power_off_roads(py: Python<'_>, roads: PyReadonlyArray2<'_, f64>, ends: PyReadonlyArray1<'_, i64>, x: f64, y: f64,
                       around: PyReadonlyArray2<'_, f64>, clear: f64) -> PyResult<(f64, f64)> {
    let (r, a) = (roads.as_array(), around.as_array());
    if r.ncols() != 5 || a.ncols() != 2 || !x.is_finite() || !y.is_finite() || !clear.is_finite()
        || r.iter().chain(a.iter()).any(|v| !v.is_finite()) {
        return Err(pyo3::exceptions::PyValueError::new_err("power_off_roads expects finite roads (n, 5), offsets (k, 2), point and clearance"));
    }
    let ends = ends.as_array();
    let mut previous = 0;
    let mut segments = Vec::with_capacity(r.nrows());
    for &end in ends {
        if end <= previous || end as usize > r.nrows() {
            return Err(pyo3::exceptions::PyValueError::new_err("power_off_roads road ends must increase and cover the segments"));
        }
        let half = r[[previous as usize, 4]];
        for i in previous as usize..end as usize {
            let s = r.row(i);
            if s[4] != half {
                return Err(pyo3::exceptions::PyValueError::new_err("power_off_roads requires one half-width per road"));
            }
            segments.push(RoadSegment { a: [s[0], s[1]], d: [s[2], s[3]], half,
                length2: (s[2] * s[2] + s[3] * s[3]).max(1e-12), last: i + 1 == end as usize });
        }
        previous = end;
    }
    if previous as usize != r.nrows() {
        return Err(pyo3::exceptions::PyValueError::new_err("power_off_roads road ends must cover the segments"));
    }
    let p = [x, y];
    if road_margin(p, &segments, clear) >= 0.0 { return Ok((x, y)); }
    let first = road_candidate(p, &segments, clear);
    let first_margin = road_margin(first, &segments, clear);
    if first_margin >= -1e-6 { return Ok((first[0], first[1])); }
    // Only the search can be substantial: ordinary unchanged points and straight moves retain the GIL.
    let work = segments.len().saturating_mul(a.nrows()).saturating_mul(20);
    let best = crate::release(py, work, || {
        let mut best = first;
        let mut margin = first_margin;
        for delta in a.rows() {
            let candidate = [p[0] + delta[0], p[1] + delta[1]];
            let value = road_margin(candidate, &segments, clear);
            if value >= -1e-6 { return candidate; }
            if value > margin { best = candidate; margin = value; }
        }
        best
    });
    Ok((best[0], best[1]))
}

/// m: a gantry's footprint round its origin, across the line (its truss, 8 m) and along it (its legs): West Coast
/// USA's substation_truss, read 2026-11-18 (x -4 .. 4, y -2.87 .. 2.87; its busbars run on along -y over the kit)
const GANTRY_HALF_W: f64 = 4.0;
const GANTRY_HALF_D: f64 = 2.87;
/// m: the farthest a line end moves (across the line, and into the yard along it) to get its gantries inside
const END_SHIFT_MAX_M: f64 = 12.0;
const END_SHIFT_STEP_M: f64 = 0.5;

/// Where a part (lx, ly in the kit's frame) of the kit anchored at a line end (ex, ey heading hd into the yard: its
/// point anchor_at on the end, its +y toward the line) stands: substation_yard's placement, the same sums.
fn kit_part(ex: f64, ey: f64, hd: f64, anchor_at: [f64; 2], lx: f64, ly: f64) -> (f64, f64) {
    let ky = (-hd.dcos(), -hd.dsin());
    let kx = (ky.1, -ky.0);
    let (cx, cy) = (ex - anchor_at[0] * kx.0 - anchor_at[1] * ky.0, ey - anchor_at[0] * kx.1 - anchor_at[1] * ky.1);
    (cx + lx * kx.0 - ly * kx.1, cy + lx * kx.1 + ly * kx.0)
}

/// Do segments a-b and c-d cross (touching counts)?
fn segments_cross(a: (f64, f64), b: (f64, f64), c: (f64, f64), d: (f64, f64)) -> bool {
    let side = |p: (f64, f64), q: (f64, f64), r: (f64, f64)| (q.0 - p.0) * (r.1 - p.1) - (q.1 - p.1) * (r.0 - p.0);
    // (a touch or a collinear edge counts: the footprint is then not clear of the ring)
    side(c, d, a) * side(c, d, b) <= 0.0 && side(a, b, c) * side(a, b, d) <= 0.0
}

/// Does a gantry of the kit anchored at a line end (its place lx, ly in the kit's frame) stand with its whole footprint
/// (GANTRY_HALF_W across, GANTRY_HALF_D along) inside the rings: its corners inside, no ring edge across its outline
/// (an outline wall, a hole: Codex's review of 0cf9fd9b, the centre and four points 1 m off missed both), no ring
/// vertex under it (a hole wholly inside it)?
fn gantry_fits(rings: &[Vec<(f64, f64)>], ex: f64, ey: f64, hd: f64, anchor_at: [f64; 2], lx: f64, ly: f64) -> bool {
    let c: Vec<(f64, f64)> = [(-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)].iter()
        .map(|&(a, b)| kit_part(ex, ey, hd, anchor_at, lx + a * GANTRY_HALF_W, ly + b * GANTRY_HALF_D)).collect();
    if !c.iter().all(|&(x, y)| in_rings(rings, x, y)) {
        return false;
    }
    // (no ring's vertex under it: a hole wholly inside the footprint crosses none of its edges; Codex's review of
    // 1ac6e583)
    let side = |p: (f64, f64), q: (f64, f64), r: (f64, f64)| (q.0 - p.0) * (r.1 - p.1) - (q.1 - p.1) * (r.0 - p.0);
    let under = |r: (f64, f64)| {
        let s: Vec<f64> = (0..4).map(|k| side(c[k], c[(k + 1) % 4], r)).collect();
        s.iter().all(|&v| v >= 0.0) || s.iter().all(|&v| v <= 0.0)
    };
    if rings.iter().any(|r| r.iter().any(|&q| under(q))) {
        return false;
    }
    !rings.iter().any(|r| {
        (0..r.len()).any(|i| {
            let (p, q) = (r[i], r[(i + 1) % r.len()]);
            (0..4).any(|k| segments_cross(p, q, c[k], c[(k + 1) % 4]))
        })
    })
}

/// Do a kit part's place and the four points m along the kit's axes round it lie inside the rings?
#[allow(clippy::too_many_arguments)]
fn part_fits(rings: &[Vec<(f64, f64)>], ex: f64, ey: f64, hd: f64, anchor_at: [f64; 2], lx: f64, ly: f64, m: f64)
             -> bool {
    [(0.0, 0.0), (-m, 0.0), (m, 0.0), (0.0, -m), (0.0, m)].iter().all(|&(dx, dy)| {
        let (x, y) = kit_part(ex, ey, hd, anchor_at, lx + dx, ly + dy);
        in_rings(rings, x, y)
    })
}

/// A line end at (x, y) heading w into its substation (yard: that one yard's rings, even-odd; None: in no yard):
/// Some((x, y)) itself when its gantries (gantries: their places in the kit's frame, the kit anchored at the end at
/// anchor_at) stand with their whole footprints inside the yard (gantry_fits), or
/// when it is in no yard; else the nearest end (moved across the line or into the yard along it, END_SHIFT_STEP_M
/// steps, END_SHIFT_MAX_M at most; nearest first, then across before along, left before right) that is itself in the
/// same yard (substation_yard anchors only those: not in a hole, not in the next yard) with both gantries in it; None
/// where none is (the line ends at its last tower: no gantries, no span into the yard; Codex's reviews of report 95's
/// fix).
fn gantries_inside(yard: Option<&[Vec<(f64, f64)>]>, x: f64, y: f64, w: f64, anchor_at: [f64; 2],
                   gantries: &[[f64; 2]]) -> Option<(f64, f64)> {
    let yard = match yard {
        Some(r) if !gantries.is_empty() => r,
        _ => return Some((x, y)),
    };
    let fits = |ex: f64, ey: f64| {
        in_rings(yard, ex, ey) && gantries.iter().all(|g| gantry_fits(yard, ex, ey, w, anchor_at, g[0], g[1]))
    };
    if fits(x, y) {
        return Some((x, y));
    }
    let (ax, ay) = (w.dcos(), w.dsin());           // along the line, into the yard
    let (cx, cy) = (-ay, ax);                       // across it
    let steps = (END_SHIFT_MAX_M / END_SHIFT_STEP_M).round() as i64;
    let mut cand: Vec<(i64, i64, i64)> = Vec::new();    // (squared steps, along, across)
    for t in 0..=steps {
        for s in -steps..=steps {
            if (t, s) != (0, 0) && t * t + s * s <= steps * steps {
                cand.push((t * t + s * s, t, s));
            }
        }
    }
    cand.sort_by_key(|&(d, t, s)| (d, t, -s));
    for (_, t, s) in cand {
        let (ex, ey) = (x + ax * t as f64 * END_SHIFT_STEP_M + cx * s as f64 * END_SHIFT_STEP_M,
                        y + ay * t as f64 * END_SHIFT_STEP_M + cy * s as f64 * END_SHIFT_STEP_M);
        if fits(ex, ey) {
            return Some((ex, ey));
        }
    }
    None
}

/// A line end: its place and heading [x, y, z, w], whether its gantries stand (gantries_inside found them a place),
/// the ground under its left and right gantries (the conductors' heights there and the yard's gantry rows').
struct LineEnd {
    e: [f64; 4],
    fits: bool,
    zl: f64,
    zr: f64,
    yard: i64,
}

/// The yard a point is in: the first (by id) whose rings hold it (even-odd: not in its holes), -1 for none.
fn yard_of(yards: &[(i64, Vec<Vec<(f64, f64)>>)], x: f64, y: f64) -> i64 {
    yards.iter().find(|(_, r)| in_rings(r, x, y)).map(|(k, _)| *k).unwrap_or(-1)
}

/// The towers, line ends and conductors of lines given as their vertices (n, 3) (x, y in the anchor frame, z the
/// ground) with offsets (m + 1) and stop (n,): the vertex lies in a substation. Consecutive vertices closer than
/// min_span merged. A tower at every vertex not in a substation, turned along the line (the bisector of its two spans;
/// its local x along the line, its arms along local y), once per spot (towers within dedupe_m of one made earlier:
/// none, the lines meeting there share it). Where a span reaches a substation, a line end at its vertex inside
/// (substation_yard puts a yard's gantries there, facing the line), heading the span's way into the substation; an
/// end within end_merge_m of an earlier one is that one (the lines share its gantries); no span on between substation
/// vertices. The conductors: per span (min_span .. max_span long) one from each arm (arms (r, 4): lateral offset and
/// height over the ground at a tower; lateral offset and height at a line end, mirrored when the span comes in from
/// the end's other side, gantry_front out of the gantries toward the line) to the same arm of the next tower or end, a
/// parabola sagging sag x the span at its middle, seg_m apart. Only the towers in the box x0, y0, x1, y1 and the spans
/// whose middle is in it; every end. An end whose gantries (gantries (2, 2): the line-end kit's two gantries in its
/// frame, the kit anchored at anchor_at) would stand past its yard (yard_pts (q, 2), yard_off: ring offsets, yard_id:
/// each ring's yard: a yard is its rings, even-odd) moves with them to where they fit in that yard (gantries_inside),
/// its conductors with it; one whose gantries fit
/// nowhere is no end: no span on to it (the line stops at its last tower). The conductors at the gantries hang at the
/// ground under each gantry (ground(x (n,), y (n,)) -> z, None: the end's own ground) plus the arm's height.
/// (towers (t, 4) [x, y, z, yaw degrees counter-clockwise from +x], ends (e, 7) [x, y, z, heading radians, ground
/// under the left gantry, under the right one, its yard's id (-1: none)], wire points (w, 3), wire offsets (k + 1),
/// each wire's supports (k, 10) [kind, x, y, z, yaw degrees] at its first point then at its last: kind 1 a tower (its
/// row's values), 2 the line end's gantry it hangs on (the yard's gantry row's), 0 none: a span is drawn by one tile,
/// its supports by theirs). span_ground (x (n,), y (n,)) -> z, None: none) with span_clear > 0: the spans' clearance
/// (span_clearance: each tower's scale its towers' 5th column and its supports' 6th, each span's sag cut). 496: towers
/// (t, 5) [x, y, z, yaw, scale], supports (k, 12) [kind, x, y, z, yaw, scale] at each end.
#[pyfunction]
#[pyo3(signature = (pts, off, stop, arms, sag, seg_m, min_span, max_span, dedupe_m, end_merge_m, gantry_front, bx,
                    yard_pts, yard_off, yard_id, anchor_at, gantries, ground=None, span_ground=None, span_clear=0.0))]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn power_line_layout<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                              stop: PyReadonlyArray1<'py, bool>, arms: PyReadonlyArray2<'py, f64>, sag: f64,
                              seg_m: f64, min_span: f64, max_span: f64, dedupe_m: f64, end_merge_m: f64,
                              gantry_front: f64, bx: [f64; 4], yard_pts: PyReadonlyArray2<'py, f64>,
                              yard_off: PyReadonlyArray1<'py, i64>, yard_id: PyReadonlyArray1<'py, i64>,
                              anchor_at: [f64; 2], gantries: Vec<[f64; 2]>,
                              ground: Option<Bound<'py, PyAny>>, span_ground: Option<Bound<'py, PyAny>>,
                              span_clear: f64)
                              -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>,
                                           Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>,
                                           Bound<'py, PyArray2<f64>>)> {
    // (left, right: the kit's gantries by their x)
    let mut gantries = gantries;
    gantries.sort_by(|a, b| a[0].partial_cmp(&b[0]).unwrap_or(std::cmp::Ordering::Equal));
    let (pts, off, stop, arms) = (pts.as_array(), off.as_array(), stop.as_array(), arms.as_array());
    let (yp, yo, yi) = (yard_pts.as_array(), yard_off.as_array(), yard_id.as_array());
    // (each yard its own rings: its outline and holes, even-odd)
    let mut by_id: std::collections::BTreeMap<i64, Vec<Vec<(f64, f64)>>> = std::collections::BTreeMap::new();
    for k in 0..yo.len().saturating_sub(1) {
        let ring: Vec<(f64, f64)> = (yo[k] as usize..yo[k + 1] as usize).map(|i| (yp[[i, 0]], yp[[i, 1]])).collect();
        if ring.len() >= 3 {
            by_id.entry(yi.get(k).copied().unwrap_or(k as i64)).or_default().push(ring);
        }
    }
    let yards: Vec<(i64, Vec<Vec<(f64, f64)>>)> = by_id.into_iter().collect();
    let yard_rings = |id: i64| yards.iter().find(|(k, _)| *k == id).map(|(_, r)| r.as_slice());
    let inside = |x: f64, y: f64| x >= bx[0] && x < bx[2] && y >= bx[1] && y < bx[3];
    let mut towers: Vec<f64> = Vec::new();
    let mut ends: Vec<LineEnd> = Vec::new();
    let mut mapped: Vec<[f64; 3]> = Vec::new();     // (each end's own vertex: the merge reads the mapped places)
    let mut sup: Vec<f64> = Vec::new();
    let mut taken: HashSet<(i64, i64)> = HashSet::new();
    let cell = |x: f64, y: f64| ((x / dedupe_m).round() as i64, (y / dedupe_m).round() as i64);
    // a new spot (none within dedupe_m made before)
    let claim = |set: &mut HashSet<(i64, i64)>, x: f64, y: f64| {
        let key = cell(x, y);
        let near = (-1..=1).any(|a| (-1..=1).any(|b| set.contains(&(key.0 + a, key.1 + b))));
        if !near {
            set.insert(key);
        }
        !near
    };
    let (mut wp, mut wo): (Vec<f64>, Vec<i64>) = (Vec::new(), vec![0]);
    // every line's vertices, those closer than min_span to the last kept one left out, but never one either side of a
    // yard's edge (its mapped approach: a line end, its heading and the gantries it merges onto read the same in every
    // tile, however far back the tile's run of the line starts; Codex's review of 58788443)
    let lines: Vec<(Vec<[f64; 3]>, Vec<bool>)> = (0..off.len().saturating_sub(1)).map(|l| {
        let (mut v, mut st): (Vec<[f64; 3]>, Vec<bool>) = (Vec::new(), Vec::new());
        let (i0, i1) = (off[l] as usize, off[l + 1] as usize);
        for i in i0..i1 {
            let p = [pts[[i, 0]], pts[[i, 1]], pts[[i, 2]]];
            // (only with the yards given: BNE_POWER_YARDS; without, every vertex as before)
            let edge = !yards.is_empty()
                && ((i > i0 && stop[i] != stop[i - 1]) || (i + 1 < i1 && stop[i] != stop[i + 1]));
            if let Some(q) = v.last() {
                if !edge && (p[0] - q[0]).dhypot(p[1] - q[1]) < min_span {
                    continue;
                }
            }
            v.push(p);
            st.push(stop[i]);
        }
        (v, st)
    }).collect();
    // the spans' clearance over the ground between their towers: a support's lift the most any span of any line at it
    // asks (a tower shared by two ways, or the same tower of the next tile's run: one scale for its model and every
    // conductor on it; Codex's review), each span's share of its sag from its supports' lifts
    let key = |p: [f64; 3]| (p[0].to_bits(), p[1].to_bits());
    let hmin = (0..arms.nrows()).map(|r| arms[[r, 1]]).fold(f64::INFINITY, f64::min);
    let mut lift_at: BTreeMap<(u64, u64), f64> = BTreeMap::new();
    let mut samples_of: BTreeMap<(usize, usize), Vec<(f64, f64)>> = BTreeMap::new();
    if let (Some(g), true) = (span_ground.as_ref(), span_clear > 0.0 && hmin > 0.0 && hmin.is_finite()) {
        let span_len = |v: &[[f64; 3]], i: usize| (v[i + 1][0] - v[i][0]).dhypot(v[i + 1][1] - v[i][1]);
        // (the supports that matter here: the towers the tile stands, the ends of the spans it draws)
        let mut keys: BTreeSet<(u64, u64)> = BTreeSet::new();
        for (v, st) in &lines {
            for i in 0..v.len() {
                if !st[i] && inside(v[i][0], v[i][1]) {
                    keys.insert(key(v[i]));
                }
            }
            for i in 0..v.len().saturating_sub(1) {
                if !(st[i] && st[i + 1]) && span_len(v, i) <= max_span
                    && inside((v[i][0] + v[i + 1][0]) / 2.0, (v[i][1] + v[i + 1][1]) / 2.0) {
                    keys.insert(key(v[i]));
                    keys.insert(key(v[i + 1]));
                }
            }
        }
        // (every tower-to-tower span of every line at those supports)
        let mut want: Vec<(usize, usize)> = Vec::new();
        for (l, (v, st)) in lines.iter().enumerate() {
            for i in 0..v.len().saturating_sub(1) {
                let len = span_len(v, i);
                if !st[i] && !st[i + 1] && len <= max_span && len > 1e-6
                    && (keys.contains(&key(v[i])) || keys.contains(&key(v[i + 1]))) {
                    want.push((l, i));
                }
            }
        }
        let got = span_samples(g, &lines, &want, &arms, seg_m)?;
        let most = hmin * (SPAN_MAX_SCALE - 1.0);
        for (&(l, i), sm) in want.iter().zip(got) {
            let v = &lines[l].0;
            let need = span_need(v[i], v[i + 1], &sm, hmin, span_clear).clamp(0.0, most);
            for k in [key(v[i]), key(v[i + 1])] {
                let e = lift_at.entry(k).or_insert(0.0);
                *e = e.max(need);
            }
            samples_of.insert((l, i), sm);
        }
    }
    for (l, (v, st)) in lines.iter().enumerate() {
        let (v, st) = (v.clone(), st.clone());
        if v.len() < 2 {
            continue;
        }
        let n = v.len();
        let unit = |a: [f64; 3], b: [f64; 3]| {
            let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
            let d = dx.dhypot(dy).max(1e-9);
            (dx / d, dy / d)
        };
        let yaw: Vec<f64> = (0..n).map(|i| {
            let (mut dx, mut dy) = (0.0, 0.0);
            if i > 0 { let (a, b) = unit(v[i - 1], v[i]); dx += a; dy += b; }
            if i + 1 < n { let (a, b) = unit(v[i], v[i + 1]); dx += a; dy += b; }
            dy.datan2(dx)
        }).collect();
        let lift: Vec<f64> = v.iter().map(|p| lift_at.get(&key(*p)).copied().unwrap_or(0.0)).collect();
        let scale: Vec<f64> = lift.iter().map(|q| if *q > 0.0 { 1.0 + q / hmin } else { 1.0 }).collect();
        let sag_f: Vec<f64> = (0..n - 1).map(|i| match samples_of.get(&(l, i)) {
            Some(sm) => span_share(v[i], v[i + 1], sm, lift[i], lift[i + 1], hmin, sag, span_clear),
            None => 1.0,
        }).collect();
        for i in 0..n {
            if !st[i] && claim(&mut taken, v[i][0], v[i][1]) && inside(v[i][0], v[i][1]) {
                towers.extend_from_slice(&[v[i][0], v[i][1], v[i][2], yaw[i].to_degrees().rem_euclid(360.0), scale[i]]);
            }
        }
        for i in 0..n - 1 {
            if st[i] && st[i + 1] {
                continue;
            }
            let (a, b) = (v[i], v[i + 1]);
            let span = (b[0] - a[0]).dhypot(b[1] - a[1]);
            if span > max_span {
                continue;
            }
            let head = (b[1] - a[1]).datan2(b[0] - a[0]);
            // the span's line end (heading into the substation), a nearby earlier one if any
            let end = if st[i] { Some((a, head + std::f64::consts::PI)) } else if st[i + 1] { Some((b, head)) } else { None };
            let end = match end {
                None => None,
                // (into an earlier end of the same yard whose gantries stand; one that found no place is no end to
                // share: a nearby line tries its own; Codex's review of 26580e0c)
                Some((p, w)) => Some(match mapped.iter().zip(ends.iter()).position(|(e, x)| {
                    x.fits && x.yard == yard_of(&yards, p[0], p[1]) && (e[0] - p[0]).dhypot(e[1] - p[1]) < end_merge_m
                }) {
                    Some(k) => k,
                    None => {
                        // (its gantries inside the yard: the end moved with them where one stood past the fence)
                        let yard = yard_of(&yards, p[0], p[1]);
                        let got = gantries_inside(yard_rings(yard), p[0], p[1], w, anchor_at, &gantries);
                        let (x, y) = got.unwrap_or((p[0], p[1]));
                        let (mut zl, mut zr) = (p[2], p[2]);
                        if let (Some(g), Some(_), true) = (ground.as_ref(), got, gantries.len() >= 2) {
                            let at: Vec<(f64, f64)> = [gantries[0], gantries[gantries.len() - 1]].iter()
                                .map(|q| kit_part(x, y, w, anchor_at, q[0], q[1])).collect();
                            let xs: Vec<f64> = at.iter().map(|q| q.0).collect();
                            let ys: Vec<f64> = at.iter().map(|q| q.1).collect();
                            let z: Vec<f64> = g.call1((xs.into_pyarray(py), ys.into_pyarray(py)))?.extract()?;
                            if z.len() != 2 {
                                return Err(pyo3::exceptions::PyValueError::new_err(
                                    "power_line_layout: a height per gantry"));
                            }
                            zl = z[0];
                            zr = z[1];
                        }
                        mapped.push(p);
                        ends.push(LineEnd { e: [x, y, p[2], w], fits: got.is_some(), zl, zr, yard });
                        ends.len() - 1
                    }
                }),
            };
            if let Some(k) = end {
                if !ends[k].fits {
                    continue;           // (no gantries fit: no span into the yard)
                }
            }
            if !inside((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0) {
                continue;
            }
            let k = ((span / seg_m).ceil() as usize).max(2);
            for r in 0..arms.nrows() {
                // (a tower scaled up for its spans' clearance: its arms out and up with it)
                let tower = |p: [f64; 3], w: f64, s: f64| [p[0] - w.dsin() * arms[[r, 0]] * s,
                                                           p[1] + w.dcos() * arms[[r, 0]] * s, p[2] + arms[[r, 1]] * s];
                // at the end: its lateral axis, mirrored when it points against the span's (sides kept in the world)
                let gantry = |k: usize| {
                    let (e, w) = (ends[k].e, ends[k].e[3]);
                    let sign = if (-w.dsin()) * (-head.dsin()) + w.dcos() * head.dcos() >= 0.0 { 1.0 } else { -1.0 };
                    let lat = arms[[r, 2]] * sign;
                    // (on the gantry on its side: the ground under that one)
                    let zg = if lat > 0.0 { ends[k].zr } else { ends[k].zl };
                    [e[0] - w.dsin() * lat - w.dcos() * gantry_front, e[1] + w.dcos() * lat - w.dsin() * gantry_front,
                     zg + arms[[r, 3]]]
                };
                let p0 = match (st[i], end) { (true, Some(k)) => gantry(k), _ => tower(a, yaw[i], scale[i]) };
                let p1 = match (st[i + 1], end) { (true, Some(k)) => gantry(k), _ => tower(b, yaw[i + 1], scale[i + 1]) };
                for (on_end, q, qy, qs) in [(st[i], a, yaw[i], scale[i]), (st[i + 1], b, yaw[i + 1], scale[i + 1])] {
                    if let (true, Some(k)) = (on_end, end) {
                        // (its gantry's own row: where the yard stands it, at the ground it is stood on; a raceway's
                        // band culling it there takes the conductor here too: Codex's review of 119bb4d7)
                        let (e, w) = (ends[k].e, ends[k].e[3]);
                        let sign = if (-w.dsin()) * (-head.dsin()) + w.dcos() * head.dcos() >= 0.0 { 1.0 } else { -1.0 };
                        let right = arms[[r, 2]] * sign > 0.0;
                        match (gantries.first(), gantries.last()) {
                            (Some(gl), Some(gr)) => {
                                let g = if right { gr } else { gl };
                                let (gx, gy) = kit_part(e[0], e[1], w, anchor_at, g[0], g[1]);
                                let kx = (-w.dsin(), w.dcos());
                                sup.extend_from_slice(&[2.0, gx, gy, if right { ends[k].zr } else { ends[k].zl },
                                                        kx.1.datan2(kx.0).to_degrees().rem_euclid(360.0), 1.0]);
                            }
                            _ => sup.extend_from_slice(&[0.0, 0.0, 0.0, 0.0, 0.0, 1.0]),
                        }
                    } else {
                        sup.extend_from_slice(&[1.0, q[0], q[1], q[2], qy.to_degrees().rem_euclid(360.0), qs]);
                    }
                }
                let sag_i = sag * sag_f[i];
                for s in 0..=k {
                    let t = s as f64 / k as f64;
                    wp.extend_from_slice(&[p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t,
                                           p0[2] + (p1[2] - p0[2]) * t - 4.0 * sag_i * span * t * (1.0 - t)]);
                }
                wo.push((wp.len() / 3) as i64);
            }
        }
    }
    let ends: Vec<f64> = ends.iter().filter(|e| e.fits)
        .flat_map(|e| [e.e[0], e.e[1], e.e[2], e.e[3], e.zl, e.zr, e.yard as f64]).collect();
    let (t, e, w, k) = (towers.len() / 5, ends.len() / 7, wp.len() / 3, sup.len() / 12);
    Ok((numpy::ndarray::Array2::from_shape_vec((t, 5), towers).unwrap().into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((e, 7), ends).unwrap().into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((w, 3), wp).unwrap().into_pyarray(py), wo.into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((k, 12), sup).unwrap().into_pyarray(py)))
}

/// A tower's scale for its spans' clearance at most this (its lowest conductor raised by up to SPAN_MAX_SCALE - 1 of
/// its height, the model with it).
const SPAN_MAX_SCALE: f64 = 1.75;

/// The ground samples [(t along the span, ground z)] of spans want [(line, span)] of lines [(vertices, at a yard)]: every
/// seg_m along each, under the line and under its outermost conductors either side (arms' widest lateral offset at the
/// most a tower is scaled), the highest of the three (a conductor out over a side slope clears it too); one call of
/// span_ground (x (n,), y (n,)) -> z for them all.
fn span_samples<'py>(g: &Bound<'py, PyAny>, lines: &[(Vec<[f64; 3]>, Vec<bool>)], want: &[(usize, usize)],
                     arms: &numpy::ndarray::ArrayView2<'_, f64>, seg_m: f64) -> PyResult<Vec<Vec<(f64, f64)>>> {
    let side = (0..arms.nrows()).map(|r| arms[[r, 0]].abs()).fold(0.0, f64::max) * SPAN_MAX_SCALE;
    let (mut xs, mut ys, mut ts, mut of) = (Vec::new(), Vec::new(), Vec::new(), vec![0usize]);
    for &(l, i) in want {
        let v = &lines[l].0;
        let len = (v[i + 1][0] - v[i][0]).dhypot(v[i + 1][1] - v[i][1]);
        let k = ((len / seg_m).ceil() as usize).max(2);
        let (ux, uy) = ((v[i + 1][0] - v[i][0]) / len, (v[i + 1][1] - v[i][1]) / len);
        for s in 1..k {
            let t = s as f64 / k as f64;
            let (cx, cy) = (v[i][0] + (v[i + 1][0] - v[i][0]) * t, v[i][1] + (v[i + 1][1] - v[i][1]) * t);
            for off in [0.0, side, -side] {
                xs.push(cx - uy * off);
                ys.push(cy + ux * off);
            }
            ts.push(t);
        }
        of.push(ts.len());
    }
    if ts.is_empty() {
        return Ok(vec![Vec::new(); want.len()]);
    }
    let m = xs.len();
    let z: Vec<f64> = g.call1((xs.into_pyarray(g.py()), ys.into_pyarray(g.py())))?.extract()?;
    if z.len() != m {
        return Err(pyo3::exceptions::PyValueError::new_err("power_line_layout: a ground height per span sample"));
    }
    let high = |q: usize| {
        [z[3 * q], z[3 * q + 1], z[3 * q + 2]].into_iter().filter(|h| h.is_finite())
            .fold(f64::NAN, |acc: f64, h| if acc.is_nan() { h } else { acc.max(h) })
    };
    Ok((0..want.len()).map(|w| (of[w]..of[w + 1]).map(|q| (ts[q], high(q))).collect()).collect())
}

/// m the chord of span a -> b's lowest conductor (hmin over its towers' feet) must rise to be clear over its ground
/// samples (<= 0: clear already). Pure.
fn span_need(a: [f64; 3], b: [f64; 3], sm: &[(f64, f64)], hmin: f64, clear: f64) -> f64 {
    sm.iter().filter(|q| q.1.is_finite())
        .fold(f64::NEG_INFINITY, |acc, &(t, gz)| acc.max(gz + clear - (a[2] + hmin + (b[2] - a[2]) * t)))
}

/// The share (0..1) of sag span a -> b keeps with its towers' lowest conductors lifted la, lb so that it stays clear
/// over its ground samples. Pure.
#[allow(clippy::too_many_arguments)]
fn span_share(a: [f64; 3], b: [f64; 3], sm: &[(f64, f64)], la: f64, lb: f64, hmin: f64, sag: f64, clear: f64) -> f64 {
    let l = (b[0] - a[0]).dhypot(b[1] - a[1]);
    let (za, zb) = (a[2] + la + hmin, b[2] + lb + hmin);
    let mut f = 1.0f64;
    for &(t, gz) in sm {
        let w = 4.0 * sag * l * t * (1.0 - t);
        if gz.is_finite() && w > 1e-9 {
            f = f.min((za + (zb - za) * t - gz - clear) / w);
        }
    }
    f.clamp(0.0, 1.0)
}

/// Each span's lowest conductor (hmin over its towers' feet v, the chord between them less sag x span at its middle)
/// at least clear over the ground samples [(t along the span, ground z)]: where the chord itself is under that, both its
/// towers scaled up by the deficit (a tower the most either of its spans asks, SPAN_MAX_SCALE at most); then the
/// span's sag cut to what the ground under it leaves (a share 0..1 of sag). Spans over a crest between their towers
/// buried their conductors in the hill (Breno, 45.7981,10.4253: a 418 m span climbing 142 m over a ridge, its lowest
/// conductor 3.6 m under the ground: the player's report 0XESWVG2, 2026-11-17). Returns (scale per vertex, sag share
/// per span). Pure.
#[cfg(test)]
fn span_clearance_core(v: &[[f64; 3]], samples: &[Vec<(f64, f64)>], hmin: f64, sag: f64, clear: f64)
                       -> (Vec<f64>, Vec<f64>) {
    let n = v.len();
    let need: Vec<f64> = (0..n.saturating_sub(1)).map(|i| span_need(v[i], v[i + 1], &samples[i], hmin, clear)).collect();
    let most = hmin * (SPAN_MAX_SCALE - 1.0);
    let lift: Vec<f64> = (0..n).map(|i| {
        let a = if i > 0 { need[i - 1] } else { 0.0 };
        let b = if i + 1 < n { need[i] } else { 0.0 };
        a.max(b).clamp(0.0, most)
    }).collect();
    let scale: Vec<f64> = lift.iter().map(|l| 1.0 + l / hmin).collect();
    let share: Vec<f64> = (0..n.saturating_sub(1))
        .map(|i| span_share(v[i], v[i + 1], &samples[i], lift[i], lift[i + 1], hmin, sag, clear)).collect();
    (scale, share)
}

#[cfg(test)]
mod span_tests {
    use super::*;

    #[test]
    fn a_span_over_a_crest_keeps_its_clearance() {
        // report 0XESWVG2's span: 418 m, its towers' feet 1356 and 1498 m, the ground over the chord near the top
        let v = [[0.0, 0.0, 1356.0], [418.0, 0.0, 1498.0], [618.0, 0.0, 1396.0]];
        let crest = |t: f64| 1356.0 + 142.0 * t + 20.0 * (std::f64::consts::PI * t).dsin();
        let s0: Vec<(f64, f64)> = (1..42).map(|k| { let t = k as f64 / 42.0; (t, crest(t)) }).collect();
        let s1: Vec<(f64, f64)> = (1..20).map(|k| { let t = k as f64 / 20.0; (t, 1498.0 - 102.0 * t - 20.0) }).collect();
        let (hmin, sag, clear) = (15.65, 0.03, 3.0);
        let (scale, share) = span_clearance_core(&v, &[s0.clone(), s1.clone()], hmin, sag, clear);
        assert!(scale[0] > 1.0 && scale[1] > 1.0, "{scale:?}");
        assert!(scale[2] == 1.0);
        // every sample of the first span at least clear under its lowest conductor
        for (t, gz) in &s0 {
            let (za, zb) = (v[0][2] + hmin * scale[0], v[1][2] + hmin * scale[1]);
            let z = za + (zb - za) * t - 4.0 * sag * share[0] * 418.0 * t * (1.0 - t);
            assert!(z - gz >= clear - 1e-9, "{t}: {}", z - gz);
        }
        // the open span down the other side keeps its towers' scale and its whole sag where the ground leaves room
        assert_eq!(share[1], 1.0);
        // flat open ground: nothing changes
        let flat: Vec<(f64, f64)> = (1..20).map(|k| (k as f64 / 20.0, 1356.0)).collect();
        let (s2, f2) = span_clearance_core(&v[..2], &[flat], hmin, sag, clear);
        assert_eq!((s2, f2), (vec![1.0, 1.0], vec![1.0]));
    }
}

/// Even-odd: (x, y) inside the rings (all of an area's, outer and holes alike).
pub(crate) fn in_rings(rings: &[Vec<(f64, f64)>], x: f64, y: f64) -> bool {
    let mut c = false;
    for r in rings {
        let n = r.len();
        let mut j = n.wrapping_sub(1);
        for i in 0..n {
            let ((xi, yi), (xj, yj)) = (r[i], r[j]);
            if (yi > y) != (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi {
                c = !c;
            }
            j = i;
        }
    }
    c
}

pub(crate) fn ring_area(r: &[(f64, f64)]) -> f64 {
    let n = r.len();
    (0..n).map(|i| r[i].0 * r[(i + 1) % n].1 - r[(i + 1) % n].0 * r[i].1).sum::<f64>().abs() / 2.0
}

/// The smallest rectangle around the ring along one of its edges: (u, v axes, a range along u, b range along v).
pub(crate) fn min_box(r: &[(f64, f64)]) -> Option<((f64, f64), (f64, f64), (f64, f64), (f64, f64))> {
    let n = r.len();
    let mut best: Option<(f64, ((f64, f64), (f64, f64), (f64, f64), (f64, f64)))> = None;
    for i in 0..n {
        let (ex, ey) = (r[(i + 1) % n].0 - r[i].0, r[(i + 1) % n].1 - r[i].1);
        let l = ex.dhypot(ey);
        if l < 1e-6 {
            continue;
        }
        let (u, v) = ((ex / l, ey / l), (-ey / l, ex / l));
        let (mut a, mut b) = ((f64::INFINITY, f64::NEG_INFINITY), (f64::INFINITY, f64::NEG_INFINITY));
        for &(x, y) in r {
            let (pa, pb) = (x * u.0 + y * u.1, x * v.0 + y * v.1);
            a = (a.0.min(pa), a.1.max(pa));
            b = (b.0.min(pb), b.1.max(pb));
        }
        let area = (a.1 - a.0) * (b.1 - b.0);
        if best.map_or(true, |q| area < q.0 - 1e-9) {
            best = Some((area, (u, v, a, b)));
        }
    }
    best.map(|q| q.1)
}

/// Two rectangles (centre, unit x axis, half sizes) overlap (separating axes).
fn rects_overlap(a: ((f64, f64), (f64, f64), f64, f64), b: ((f64, f64), (f64, f64), f64, f64)) -> bool {
    let axes = |r: &((f64, f64), (f64, f64), f64, f64)| [r.1, (-r.1 .1, r.1 .0)];
    let reach = |r: &((f64, f64), (f64, f64), f64, f64), ax: (f64, f64)| {
        let (x, y) = (r.1, (-r.1 .1, r.1 .0));
        r.2 * (x.0 * ax.0 + x.1 * ax.1).abs() + r.3 * (y.0 * ax.0 + y.1 * ax.1).abs()
    };
    let d = (b.0 .0 - a.0 .0, b.0 .1 - a.0 .1);
    axes(&a).iter().chain(axes(&b).iter()).all(|&ax| (d.0 * ax.0 + d.1 * ax.1).abs() < reach(&a, ax) + reach(&b, ax))
}

/// m round an anchored kit's part (along the kit's axes) that must be inside the rings for it to stand
const ANCHOR_PART_M: f64 = 1.0;
/// m off the anchor's line (the kit's y of anchor_at) a part of an anchored kit stands whatever the rings: the gantries
const ANCHOR_LINE_M: f64 = 0.5;

/// A substation's yard: its rings (pts (n, 2) in the anchor frame, ring offsets (k + 1); even-odd). First a kit
/// anchor_kit at every line end in anchors (a, 3 or 5) [x, y, heading into the substation radians, (the ground under
/// its left gantry, under its right one)]: its point anchor_at (its frame) on the end, its +y toward the line; of it
/// only the parts with ANCHOR_PART_M round them inside the rings, and those on the anchor's line (ANCHOR_LINE_M: the
/// gantries the conductors end on). Then the rest filled with the first kit (kits (m, 2): footprint [w along its x, h
/// along its y] around its frame's origin, in order of preference) of which at least one fits, on a grid along the
/// sides of the smallest rectangle around its largest ring (the kit's x along the longer rectangle side or across it,
/// whichever fits more), gap apart and gap / 2 inside the rings, gap clear of the anchored kits, at most max_kits
/// (those nearest the rectangle's middle). Every kit's parts (parts (p, 4) [kit, part, x, y] in the kit's frame) at
/// the kit's yaw. The fence: along every ring, panels fence_len long end to end (each placed at its end, its x along
/// the ring), the last one of a side overlapping its corner. Only the parts and panels in the box x0, y0, x1, y1.
/// (parts (q, 5) [part, x, y, yaw degrees counter-clockwise from +x, z: an anchored gantry's ground from anchors
/// (the conductors' own), NaN for the others], fence (f, 3) [x, y, yaw degrees]).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn substation_yard<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, ring_off: PyReadonlyArray1<'py, i64>,
                            kits: PyReadonlyArray2<'py, f64>, parts: PyReadonlyArray2<'py, f64>, gap: f64,
                            anchors: PyReadonlyArray2<'py, f64>, anchor_kit: usize, anchor_at: [f64; 2],
                            max_kits: usize, fence_len: f64, bx: [f64; 4])
                            -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>) {
    let (pts, ro, kits, parts, an) = (pts.as_array(), ring_off.as_array(), kits.as_array(), parts.as_array(),
                                      anchors.as_array());
    let inside = |x: f64, y: f64| x >= bx[0] && x < bx[2] && y >= bx[1] && y < bx[3];
    let rings: Vec<Vec<(f64, f64)>> = (0..ro.len().saturating_sub(1))
        .map(|k| (ro[k] as usize..ro[k + 1] as usize).map(|i| (pts[[i, 0]], pts[[i, 1]])).collect())
        .filter(|r: &Vec<(f64, f64)>| r.len() >= 3)
        .collect();
    let (mut out, mut fence): (Vec<f64>, Vec<f64>) = (Vec::new(), Vec::new());
    let mut place = |k: usize, cx: f64, cy: f64, kx: (f64, f64), out: &mut Vec<f64>| {
        let yaw = kx.1.datan2(kx.0).to_degrees().rem_euclid(360.0);
        for p in 0..parts.nrows() {
            if parts[[p, 0]] as usize != k {
                continue;
            }
            let (lx, ly) = (parts[[p, 2]], parts[[p, 3]]);
            let (x, y) = (cx + lx * kx.0 - ly * kx.1, cy + lx * kx.1 + ly * kx.0);
            if inside(x, y) {
                out.extend_from_slice(&[parts[[p, 1]], x, y, yaw, f64::NAN]);
            }
        }
    };
    // the kits at the line ends: of each only the parts standing inside the rings (ANCHOR_PART_M of each inside; the
    // user's report 95, a 25 m wide Tennessee substation 2026-11-17: a line entering along its short side laid the
    // 38 m kit's masts, transformers and supports out past the fence), but always those on the anchor's line (the
    // gantries the conductors end on)
    let mut held: Vec<((f64, f64), (f64, f64), f64, f64)> = Vec::new();
    if anchor_kit < kits.nrows() {
        let (w, h) = (kits[[anchor_kit, 0]], kits[[anchor_kit, 1]]);
        for e in 0..an.nrows() {
            let hd = an[[e, 2]];
            let ky = (-hd.dcos(), -hd.dsin());
            let kx = (ky.1, -ky.0);
            let (cx, cy) = (an[[e, 0]] - anchor_at[0] * kx.0 - anchor_at[1] * ky.0,
                            an[[e, 1]] - anchor_at[0] * kx.1 - anchor_at[1] * ky.1);
            let yaw = kx.1.datan2(kx.0).to_degrees().rem_euclid(360.0);
            // (the ground under its left and right gantries, as the conductors hang there: power_line_layout's ends;
            // anchors (a, 3): every part of the kit, as before BNE_POWER_YARDS)
            let clip = an.ncols() >= 5;
            let (zl, zr) = if clip { (an[[e, 3]], an[[e, 4]]) } else { (f64::NAN, f64::NAN) };
            for p in 0..parts.nrows() {
                if parts[[p, 0]] as usize != anchor_kit {
                    continue;
                }
                let (lx, ly) = (parts[[p, 2]], parts[[p, 3]]);
                let on_line = (ly - anchor_at[1]).abs() <= ANCHOR_LINE_M;
                let fits = part_fits(&rings, an[[e, 0]], an[[e, 1]], hd, anchor_at, lx, ly, ANCHOR_PART_M);
                let (x, y) = kit_part(an[[e, 0]], an[[e, 1]], hd, anchor_at, lx, ly);
                if (!clip || on_line || fits) && inside(x, y) {
                    let z = if !on_line { f64::NAN } else if lx < anchor_at[0] { zl } else { zr };
                    out.extend_from_slice(&[parts[[p, 1]], x, y, yaw, z]);
                }
            }
            held.push(((cx, cy), kx, (w + gap) / 2.0, (h + gap) / 2.0));
        }
    }
    let big = rings.iter().max_by(|a, b| ring_area(a).partial_cmp(&ring_area(b)).unwrap());
    if let Some((u, v, ar, br)) = big.and_then(|r| min_box(r)) {
        // the rectangle's longer side as u
        let (u, v, ar, br) = if ar.1 - ar.0 >= br.1 - br.0 { (u, v, ar, br) } else { (v, (-u.0, -u.1), br, (-ar.1, -ar.0)) };
        let (ac, bc) = ((ar.0 + ar.1) / 2.0, (br.0 + br.1) / 2.0);
        let world = |a: f64, b: f64| (a * u.0 + b * v.0, a * u.1 + b * v.1);
        let clear = |cx: f64, cy: f64, kx: (f64, f64), w: f64, h: f64| {
            // the rectangle (centre, its x axis, size) inside the rings with the margin, clear of the anchored kits
            let ky = (-kx.1, kx.0);
            let (hw, hh) = ((w + gap) / 2.0, (h + gap) / 2.0);
            let corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh), (0.0, -hh), (0.0, hh), (-hw, 0.0), (hw, 0.0),
                           (0.0, 0.0)];
            corners.iter().all(|&(p, q)| in_rings(&rings, cx + p * kx.0 + q * ky.0, cy + p * kx.1 + q * ky.1))
                && held.iter().all(|&r| !rects_overlap(r, ((cx, cy), kx, hw, hh)))
        };
        for k in 0..kits.nrows() {
            let (w, h) = (kits[[k, 0]], kits[[k, 1]]);
            let mut best: Vec<(f64, f64, (f64, f64))> = Vec::new();
            for turned in [false, true] {
                // the kit's x along u, or along v
                let (kx, sa, sb) = if turned { (v, h, w) } else { (u, w, h) };
                let na = ((ar.1 - ar.0 + gap) / (sa + gap)).floor().max(0.0) as i64;
                let nb = ((br.1 - br.0 + gap) / (sb + gap)).floor().max(0.0) as i64;
                let mut got = Vec::new();
                for i in 0..na {
                    for j in 0..nb {
                        let a = ac + (i as f64 - (na - 1) as f64 / 2.0) * (sa + gap);
                        let b = bc + (j as f64 - (nb - 1) as f64 / 2.0) * (sb + gap);
                        let (cx, cy) = world(a, b);
                        if clear(cx, cy, kx, w, h) {
                            got.push(((a - ac).dhypot(b - bc), cx, cy, kx));
                        }
                    }
                }
                if got.len() > best.len() {
                    got.sort_by(|p, q| p.0.partial_cmp(&q.0).unwrap());
                    best = got.into_iter().map(|g| (g.1, g.2, g.3)).collect();
                }
            }
            if best.is_empty() {
                continue;
            }
            for &(cx, cy, kx) in best.iter().take(max_kits) {
                place(k, cx, cy, kx, &mut out);
            }
            break;
        }
    }
    if fence_len > 0.0 {
        for r in &rings {
            let n = r.len();
            for i in 0..n {
                let (p, q) = (r[i], r[(i + 1) % n]);
                let l = (q.0 - p.0).dhypot(q.1 - p.1);
                if l < 1e-6 {
                    continue;
                }
                let (dx, dy) = ((q.0 - p.0) / l, (q.1 - p.1) / l);
                let yaw = dy.datan2(dx).to_degrees().rem_euclid(360.0);
                let m = (l / fence_len).ceil() as usize;
                for s in 1..=m {
                    let t = (s as f64 * fence_len).min(l.max(fence_len));
                    let (x, y) = (p.0 + dx * t, p.1 + dy * t);
                    let (x, y) = if s == m { (q.0, q.1) } else { (x, y) };
                    if inside(x, y) {
                        fence.extend_from_slice(&[x, y, yaw]);
                    }
                }
            }
        }
    }
    let (q, f) = (out.len() / 5, fence.len() / 3);
    (numpy::ndarray::Array2::from_shape_vec((q, 5), out).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((f, 3), fence).unwrap().into_pyarray(py))
}
