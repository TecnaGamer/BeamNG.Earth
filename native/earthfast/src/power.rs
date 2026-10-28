//! High-voltage power lines (the user, 2026-10-26: "Is there a map of all power lines and how they connect? Currently
//! we've been placing them almost everywhere" / "start with 1"): OpenStreetMap's power=line ways (Overture class
//! power_line) as lattice towers at their vertices (the mapped towers) with the conductors hung between them, ending
//! at gantries inside the substations they reach; the substations' yards filled with switchgear and fenced.

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use std::collections::HashSet;

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
/// whose middle is in it; every end. (towers (t, 4) [x, y, z, yaw degrees counter-clockwise from +x], ends (e, 4)
/// [x, y, z, heading radians], wire points (w, 3), wire offsets (k + 1)).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn power_line_layout<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                              stop: PyReadonlyArray1<'py, bool>, arms: PyReadonlyArray2<'py, f64>, sag: f64,
                              seg_m: f64, min_span: f64, max_span: f64, dedupe_m: f64, end_merge_m: f64,
                              gantry_front: f64, bx: [f64; 4])
                              -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>,
                                  Bound<'py, PyArray1<i64>>) {
    let (pts, off, stop, arms) = (pts.as_array(), off.as_array(), stop.as_array(), arms.as_array());
    let inside = |x: f64, y: f64| x >= bx[0] && x < bx[2] && y >= bx[1] && y < bx[3];
    let mut towers: Vec<f64> = Vec::new();
    let mut ends: Vec<[f64; 4]> = Vec::new();
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
    for l in 0..off.len().saturating_sub(1) {
        // the line's vertices, those closer than min_span to the last kept one left out
        let (mut v, mut st): (Vec<[f64; 3]>, Vec<bool>) = (Vec::new(), Vec::new());
        for i in off[l] as usize..off[l + 1] as usize {
            let p = [pts[[i, 0]], pts[[i, 1]], pts[[i, 2]]];
            if let Some(q) = v.last() {
                if (p[0] - q[0]).hypot(p[1] - q[1]) < min_span {
                    continue;
                }
            }
            v.push(p);
            st.push(stop[i]);
        }
        if v.len() < 2 {
            continue;
        }
        let n = v.len();
        let unit = |a: [f64; 3], b: [f64; 3]| {
            let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
            let d = dx.hypot(dy).max(1e-9);
            (dx / d, dy / d)
        };
        let yaw: Vec<f64> = (0..n).map(|i| {
            let (mut dx, mut dy) = (0.0, 0.0);
            if i > 0 { let (a, b) = unit(v[i - 1], v[i]); dx += a; dy += b; }
            if i + 1 < n { let (a, b) = unit(v[i], v[i + 1]); dx += a; dy += b; }
            dy.atan2(dx)
        }).collect();
        for i in 0..n {
            if !st[i] && claim(&mut taken, v[i][0], v[i][1]) && inside(v[i][0], v[i][1]) {
                towers.extend_from_slice(&[v[i][0], v[i][1], v[i][2], yaw[i].to_degrees().rem_euclid(360.0)]);
            }
        }
        for i in 0..n - 1 {
            if st[i] && st[i + 1] {
                continue;
            }
            let (a, b) = (v[i], v[i + 1]);
            let span = (b[0] - a[0]).hypot(b[1] - a[1]);
            if span > max_span {
                continue;
            }
            let head = (b[1] - a[1]).atan2(b[0] - a[0]);
            // the span's line end (heading into the substation), a nearby earlier one if any
            let end = if st[i] { Some((a, head + std::f64::consts::PI)) } else if st[i + 1] { Some((b, head)) } else { None };
            let end = end.map(|(p, w)| {
                match ends.iter().find(|e| (e[0] - p[0]).hypot(e[1] - p[1]) < end_merge_m) {
                    Some(e) => *e,
                    None => {
                        ends.push([p[0], p[1], p[2], w]);
                        [p[0], p[1], p[2], w]
                    }
                }
            });
            if !inside((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0) {
                continue;
            }
            let k = ((span / seg_m).ceil() as usize).max(2);
            for r in 0..arms.nrows() {
                let tower = |p: [f64; 3], w: f64| [p[0] - w.sin() * arms[[r, 0]], p[1] + w.cos() * arms[[r, 0]], p[2] + arms[[r, 1]]];
                // at the end: its lateral axis, mirrored when it points against the span's (sides kept in the world)
                let gantry = |e: [f64; 4]| {
                    let w = e[3];
                    let sign = if (-w.sin()) * (-head.sin()) + w.cos() * head.cos() >= 0.0 { 1.0 } else { -1.0 };
                    let lat = arms[[r, 2]] * sign;
                    [e[0] - w.sin() * lat - w.cos() * gantry_front, e[1] + w.cos() * lat - w.sin() * gantry_front,
                     e[2] + arms[[r, 3]]]
                };
                let p0 = match (st[i], end) { (true, Some(e)) => gantry(e), _ => tower(a, yaw[i]) };
                let p1 = match (st[i + 1], end) { (true, Some(e)) => gantry(e), _ => tower(b, yaw[i + 1]) };
                for s in 0..=k {
                    let t = s as f64 / k as f64;
                    wp.extend_from_slice(&[p0[0] + (p1[0] - p0[0]) * t, p0[1] + (p1[1] - p0[1]) * t,
                                           p0[2] + (p1[2] - p0[2]) * t - 4.0 * sag * span * t * (1.0 - t)]);
                }
                wo.push((wp.len() / 3) as i64);
            }
        }
    }
    let (t, e, w) = (towers.len() / 4, ends.len(), wp.len() / 3);
    let ends: Vec<f64> = ends.into_iter().flatten().collect();
    (numpy::ndarray::Array2::from_shape_vec((t, 4), towers).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((e, 4), ends).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((w, 3), wp).unwrap().into_pyarray(py), wo.into_pyarray(py))
}

/// Even-odd: (x, y) inside the rings (all of an area's, outer and holes alike).
fn in_rings(rings: &[Vec<(f64, f64)>], x: f64, y: f64) -> bool {
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

fn ring_area(r: &[(f64, f64)]) -> f64 {
    let n = r.len();
    (0..n).map(|i| r[i].0 * r[(i + 1) % n].1 - r[(i + 1) % n].0 * r[i].1).sum::<f64>().abs() / 2.0
}

/// The smallest rectangle around the ring along one of its edges: (u, v axes, a range along u, b range along v).
fn min_box(r: &[(f64, f64)]) -> Option<((f64, f64), (f64, f64), (f64, f64), (f64, f64))> {
    let n = r.len();
    let mut best: Option<(f64, ((f64, f64), (f64, f64), (f64, f64), (f64, f64)))> = None;
    for i in 0..n {
        let (ex, ey) = (r[(i + 1) % n].0 - r[i].0, r[(i + 1) % n].1 - r[i].1);
        let l = ex.hypot(ey);
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

/// A substation's yard: its rings (pts (n, 2) in the anchor frame, ring offsets (k + 1); even-odd). First a kit
/// anchor_kit at every line end in anchors (a, 3) [x, y, heading into the substation, radians]: its point anchor_at
/// (its frame) on the end, its +y toward the line. Then the rest filled with the first kit (kits (m, 2): footprint
/// [w along its x, h along its y] around its frame's origin, in order of preference) of which at least one fits, on a
/// grid along the sides of the smallest rectangle around its largest ring (the kit's x along the longer rectangle side
/// or across it, whichever fits more), gap apart and gap / 2 inside the rings, gap clear of the anchored kits, at most
/// max_kits (those nearest the rectangle's middle). Every kit's parts (parts (p, 4) [kit, part, x, y] in the kit's
/// frame) at the kit's yaw. The fence: along every ring, panels fence_len long end to end (each placed at its end,
/// its x along the ring), the last one of a side overlapping its corner. Only the parts and panels in the box x0, y0,
/// x1, y1. (parts (q, 4) [part, x, y, yaw degrees counter-clockwise from +x], fence (f, 3) [x, y, yaw degrees]).
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
        let yaw = kx.1.atan2(kx.0).to_degrees().rem_euclid(360.0);
        for p in 0..parts.nrows() {
            if parts[[p, 0]] as usize != k {
                continue;
            }
            let (lx, ly) = (parts[[p, 2]], parts[[p, 3]]);
            let (x, y) = (cx + lx * kx.0 - ly * kx.1, cy + lx * kx.1 + ly * kx.0);
            if inside(x, y) {
                out.extend_from_slice(&[parts[[p, 1]], x, y, yaw]);
            }
        }
    };
    // the kits at the line ends
    let mut held: Vec<((f64, f64), (f64, f64), f64, f64)> = Vec::new();
    if anchor_kit < kits.nrows() {
        let (w, h) = (kits[[anchor_kit, 0]], kits[[anchor_kit, 1]]);
        for e in 0..an.nrows() {
            let hd = an[[e, 2]];
            let ky = (-hd.cos(), -hd.sin());
            let kx = (ky.1, -ky.0);
            let (cx, cy) = (an[[e, 0]] - anchor_at[0] * kx.0 - anchor_at[1] * ky.0,
                            an[[e, 1]] - anchor_at[0] * kx.1 - anchor_at[1] * ky.1);
            place(anchor_kit, cx, cy, kx, &mut out);
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
                            got.push(((a - ac).hypot(b - bc), cx, cy, kx));
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
                let l = (q.0 - p.0).hypot(q.1 - p.1);
                if l < 1e-6 {
                    continue;
                }
                let (dx, dy) = ((q.0 - p.0) / l, (q.1 - p.1) / l);
                let yaw = dy.atan2(dx).to_degrees().rem_euclid(360.0);
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
    let (q, f) = (out.len() / 4, fence.len() / 3);
    (numpy::ndarray::Array2::from_shape_vec((q, 4), out).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((f, 3), fence).unwrap().into_pyarray(py))
}
