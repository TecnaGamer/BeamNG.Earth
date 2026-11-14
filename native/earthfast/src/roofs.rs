//! Pitched roofs on a footprint's own outline (buildings._roof's heightfield roofs, research 2026-11-10: the roofs were
//! drawn on the footprint's oriented box only and a footprint filling less than 80 % of it went flat: 11-13 % of
//! houses, every L / T / U house). The idea is OSM2World's (MIT; no code taken): a roof is a heightfield over the
//! outline, here the lowest of a few planes (a gable's two slopes, a hipped roof's four, a half-hipped roof's two
//! slopes and two hips raised to its gables' tops, a lean-to's one). The outline is ear clipped, every piece whose
//! corners take different planes is cut along the line where two of them meet until each piece lies on one plane
//! (a concave piecewise-planar surface is that plane on a convex piece whose corners all take it), and each edge of
//! the outline is cut the same way for the wall up to the roof.
use crate::dm::Dm;
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2};
use pyo3::prelude::*;

#[derive(Clone, Copy)]
struct Plane {
    z0: f64,
    gx: f64,
    gy: f64,
}

impl Plane {
    fn at(&self, p: (f64, f64)) -> f64 {
        self.z0 + self.gx * p.0 + self.gy * p.1
    }
}

const EPS: f64 = 1e-7;
const MAX_DEPTH: u32 = 12;

/// The planes of roof kind (0 gabled, 1 hipped / pyramidal, 2 half-hipped, 3 lean-to) over ring, its ridge along u
/// through c, slope k (rise per m), eave z e; hip: a half-hipped roof's hip share of the rise; hi: a lean-to's high
/// side (+1 / -1 along v = (-u.y, u.x)). The extents are the ring's own: its farthest corners take the eave.
fn planes(ring: &[(f64, f64)], kind: i64, c: (f64, f64), u: (f64, f64), k: f64, e: f64, hip: f64, hi: f64)
          -> Vec<Plane> {
    let v = (-u.1, u.0);
    let st = |p: &(f64, f64)| {
        let d = (p.0 - c.0, p.1 - c.1);
        (d.0 * u.0 + d.1 * u.1, d.0 * v.0 + d.1 * v.1)
    };
    let mut tmax = 0.0f64;
    let mut lmax = 0.0f64;
    let mut lean = f64::NEG_INFINITY;
    for p in ring {
        let (s, t) = st(p);
        tmax = tmax.max(t.abs());
        lmax = lmax.max(s.abs());
        lean = lean.max(-hi * t);
    }
    // z = a + b s + c t in the roof's frame -> world: gx = b ux + c vx, gy = b uy + c vy, z0 = a - gx cx - gy cy
    let mk = |a: f64, b: f64, cc: f64| {
        let gx = b * u.0 + cc * v.0;
        let gy = b * u.1 + cc * v.1;
        Plane { z0: a - gx * c.0 - gy * c.1, gx, gy }
    };
    let top = e + k * tmax;
    let mut out = Vec::new();
    match kind {
        3 => out.push(mk(e + k * lean, 0.0, k * hi)),
        _ => {
            out.push(mk(top, 0.0, -k));
            out.push(mk(top, 0.0, k));
            if kind == 1 {
                out.push(mk(e + k * lmax, -k, 0.0));
                out.push(mk(e + k * lmax, k, 0.0));
            } else if kind == 2 {
                let base = e + k * tmax * (1.0 - hip);
                out.push(mk(base + k * lmax, -k, 0.0));
                out.push(mk(base + k * lmax, k, 0.0));
            }
        }
    }
    out
}

/// The planes lowest at p (within EPS).
fn lowest(pl: &[Plane], p: (f64, f64)) -> (f64, Vec<usize>) {
    let z = pl.iter().map(|q| q.at(p)).fold(f64::INFINITY, f64::min);
    (z, (0..pl.len()).filter(|&i| pl[i].at(p) <= z + EPS).collect())
}

/// A convex piece cut by f(p) = a.at(p) - b.at(p) = 0: (f <= 0 side, f >= 0 side).
fn split(poly: &[(f64, f64)], a: &Plane, b: &Plane) -> (Vec<(f64, f64)>, Vec<(f64, f64)>) {
    let f = |p: (f64, f64)| a.at(p) - b.at(p);
    let (mut lo, mut hi) = (Vec::new(), Vec::new());
    let n = poly.len();
    for i in 0..n {
        let p = poly[i];
        let q = poly[(i + 1) % n];
        let (fp, fq) = (f(p), f(q));
        if fp <= 0.0 { lo.push(p); }
        if fp >= 0.0 { hi.push(p); }
        if (fp < 0.0 && fq > 0.0) || (fp > 0.0 && fq < 0.0) {
            let t = fp / (fp - fq);
            let x = (p.0 + (q.0 - p.0) * t, p.1 + (q.1 - p.1) * t);
            lo.push(x);
            hi.push(x);
        }
    }
    (lo, hi)
}

fn area2(p: &[(f64, f64)]) -> f64 {
    let n = p.len();
    (0..n).map(|i| p[i].0 * p[(i + 1) % n].1 - p[(i + 1) % n].0 * p[i].1).sum()
}

/// The piece's plane, or the two planes to cut it by.
fn uniform(pl: &[Plane], poly: &[(f64, f64)]) -> Result<usize, (usize, usize)> {
    let sets: Vec<Vec<usize>> = poly.iter().map(|&p| lowest(pl, p).1).collect();
    if let Some(&c) = sets[0].iter().find(|&&c| sets.iter().all(|s| s.contains(&c))) {
        return Ok(c);
    }
    let a = sets[0][0];
    for s in &sets {
        if !s.contains(&a) {
            return Err((a, s[0]));
        }
    }
    Ok(a)
}

fn cut(pl: &[Plane], poly: Vec<(f64, f64)>, depth: u32, out: &mut Vec<(Vec<(f64, f64)>, usize)>) {
    if poly.len() < 3 || area2(&poly).abs() < 1e-9 {
        return;
    }
    match uniform(pl, &poly) {
        Ok(c) => out.push((poly, c)),
        Err((a, b)) => {
            if depth >= MAX_DEPTH {
                let n = poly.len() as f64;
                let m = poly.iter().fold((0.0, 0.0), |s, p| (s.0 + p.0 / n, s.1 + p.1 / n));
                let c = lowest(pl, m).1[0];
                out.push((poly, c));
                return;
            }
            let (lo, hi) = split(&poly, &pl[a], &pl[b]);
            cut(pl, lo, depth + 1, out);
            cut(pl, hi, depth + 1, out);
        }
    }
}

fn cut_edge(pl: &[Plane], a: (f64, f64), b: (f64, f64), depth: u32, out: &mut Vec<[f64; 6]>) {
    let (za, sa) = lowest(pl, a);
    let (zb, sb) = lowest(pl, b);
    if depth >= MAX_DEPTH || sa.iter().any(|c| sb.contains(c)) {
        out.push([a.0, a.1, za, b.0, b.1, zb]);
        return;
    }
    let (pa, pb) = (&pl[sa[0]], &pl[sb[0]]);
    let f = |p: (f64, f64)| pa.at(p) - pb.at(p);
    let (fa, fb) = (f(a), f(b));
    if (fa - fb).abs() < 1e-12 {
        out.push([a.0, a.1, za, b.0, b.1, zb]);
        return;
    }
    let t = (fa / (fa - fb)).clamp(0.0, 1.0);
    let x = (a.0 + (b.0 - a.0) * t, a.1 + (b.1 - a.1) * t);
    cut_edge(pl, a, x, depth + 1, out);
    cut_edge(pl, x, b, depth + 1, out);
}

/// The walls above a heightfield roof's eave (heightfield_roof's walls (w, 6): ax ay za bx by zb) cut into floor bands
/// of the window sheet, as the floors under the eave are drawn (streets.gl study 2026-11-11, proposal 3: One World
/// Trade Center's sloped corner walls rise 360 m over their eave as a plain sliver). Each wall piece rising at least
/// min_rise: the trapezoid between the eave and its top cut by each floor's slab [e + f h, e + (f + 1) h] (a convex
/// polygon, fanned), its uv: along the edge from the edge's start / sheet_m (a sheet of bays per sheet_m; pieces of
/// one straight edge carry on), up the slab over band band_k of bands (rows from the sheet's top, the floors' v).
/// Returns (triangles (m * 3, 3) xyz, counter-clockwise seen from outside as the walls' quads; their uvs (m * 3, 2)).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn slope_wall_floors<'py>(py: Python<'py>, walls: PyReadonlyArray2<'py, f64>, e: f64, floor_h: f64, sheet_m: f64,
                              band_k: i64, bands: f64, min_rise: f64)
                              -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>) {
    let w = walls.as_array();
    let rows: Vec<[f64; 6]> = (0..w.nrows()).map(|i| [w[[i, 0]], w[[i, 1]], w[[i, 2]], w[[i, 3]], w[[i, 4]], w[[i, 5]]])
        .collect();
    let (pts, uvs) = py.detach(|| slope_wall_floors_core(&rows, e, floor_h, sheet_m, band_k, bands, min_rise));
    let n = pts.len();
    let p = numpy::ndarray::Array2::from_shape_vec((n, 3), pts.into_iter().flat_map(|q| [q.0, q.1, q.2]).collect())
        .expect("shape");
    let t = numpy::ndarray::Array2::from_shape_vec((n, 2), uvs.into_iter().flat_map(|q| [q.0, q.1]).collect()).expect("shape");
    (p.into_pyarray(py), t.into_pyarray(py))
}

#[allow(clippy::type_complexity)]
pub fn slope_wall_floors_core(rows: &[[f64; 6]], e: f64, floor_h: f64, sheet_m: f64, band_k: i64, bands: f64,
                              min_rise: f64) -> (Vec<(f64, f64, f64)>, Vec<(f64, f64)>) {
    let mut pts = Vec::new();
    let mut uvs = Vec::new();
    if floor_h <= 0.1 || sheet_m <= 0.0 || bands <= 0.0 {
        return (pts, uvs);
    }
    let eps = 1.5 / 1024.0;
    let (v0, v1) = (1.0 - (band_k + 1) as f64 / bands + eps, 1.0 - band_k as f64 / bands - eps);
    let mut s0 = 0.0f64;                    // (the edge's length before this piece)
    let mut prev: Option<(f64, f64, f64, f64)> = None;    // (the last piece's end and direction)
    for r in rows {
        let [ax, ay, za, bx, by, zb] = *r;
        let l = (bx - ax).dhypot(by - ay);
        if l < 1e-6 {
            continue;
        }
        let d = ((bx - ax) / l, (by - ay) / l);
        s0 = match prev {
            Some((px, py_, dx, dy)) if (px - ax).dhypot(py_ - ay) < 1e-6 && dx * d.0 + dy * d.1 > 0.9999 => s0,
            _ => 0.0,
        };
        prev = Some((bx, by, d.0, d.1));
        let s_here = s0;
        s0 += l;
        if za.max(zb) - e < min_rise {
            continue;
        }
        // (the trapezoid in (t, z), counter-clockwise: t along the piece)
        let quad = [(0.0, e), (1.0, e), (1.0, zb.max(e)), (0.0, za.max(e))];
        let floors = ((za.max(zb) - e) / floor_h).ceil() as i64;
        for f in 0..floors {
            let (z0, z1) = (e + f as f64 * floor_h, e + (f + 1) as f64 * floor_h);
            let poly = clip_floor_z(&clip_floor_z(&quad, z0, true), z1, false);
            if poly.len() < 3 {
                continue;
            }
            let corner = |q: (f64, f64)| {
                ((ax + (bx - ax) * q.0, ay + (by - ay) * q.0, q.1),
                 ((s_here + q.0 * l) / sheet_m, v0 + (q.1 - z0) / floor_h * (v1 - v0)))
            };
            let c0 = corner(poly[0]);
            for j in 1..poly.len() - 1 {
                let (c1, c2) = (corner(poly[j]), corner(poly[j + 1]));
                // (a sliver left by the slope: no triangle)
                let area = (poly[j].0 - poly[0].0) * (poly[j + 1].1 - poly[0].1)
                    - (poly[j + 1].0 - poly[0].0) * (poly[j].1 - poly[0].1);
                if area.abs() * l < 1e-4 {
                    continue;
                }
                for c in [c0, c1, c2] {
                    pts.push(c.0);
                    uvs.push(c.1);
                }
            }
        }
    }
    (pts, uvs)
}

/// The part of the polygon (t, z) above (keep_above) or below the level z (Sutherland-Hodgman, one plane).
fn clip_floor_z(poly: &[(f64, f64)], z: f64, keep_above: bool) -> Vec<(f64, f64)> {
    let inside = |p: &(f64, f64)| if keep_above { p.1 >= z } else { p.1 <= z };
    let mut out = Vec::new();
    let n = poly.len();
    for i in 0..n {
        let (p, q) = (poly[i], poly[(i + 1) % n]);
        let (ip, iq) = (inside(&p), inside(&q));
        if ip {
            out.push(p);
        }
        if ip != iq {
            let s = (z - p.1) / (q.1 - p.1);
            out.push((p.0 + (q.0 - p.0) * s, z));
        }
    }
    out
}

/// buildings._roof's heightfield roof over the counter-clockwise ring (n, 2): kind 0 gabled, 1 hipped / pyramidal,
/// 2 half-hipped (hip: its hip share of the rise), 3 lean-to (hi: its high side along v = (-uy, ux)); the ridge along
/// (ux, uy) through (cx, cy), slope k (m per m), eave e. Returns (triangles (m * 3, 3) xyz, counter-clockwise from
/// above; their planes' slopes (m, 2) gx gy; the walls' tops (w, 6): ax ay za bx by zb along each edge in ring order,
/// the edge cut where the roof over it bends).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn heightfield_roof<'py>(py: Python<'py>, ring: PyReadonlyArray2<'py, f64>, kind: i64, cx: f64, cy: f64, ux: f64,
                             uy: f64, k: f64, e: f64, hip: f64, hi: f64)
                             -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>) {
    let a = ring.as_array();
    let pts: Vec<(f64, f64)> = (0..a.nrows()).map(|i| (a[[i, 0]], a[[i, 1]])).collect();
    let (tri, grad, walls) = py.detach(|| {
        let pl = planes(&pts, kind, (cx, cy), (ux, uy), k, e, hip, if hi < 0.0 { -1.0 } else { 1.0 });
        let mut pieces = Vec::new();
        for t in crate::ear_clip(&pts) {
            let poly = vec![pts[t[0] as usize], pts[t[1] as usize], pts[t[2] as usize]];
            cut(&pl, poly, 0, &mut pieces);
        }
        let mut tri: Vec<f64> = Vec::new();
        let mut grad: Vec<f64> = Vec::new();
        for (poly, c) in &pieces {
            let p = &pl[*c];
            for i in 1..poly.len() - 1 {
                for q in [poly[0], poly[i], poly[i + 1]] {
                    tri.extend_from_slice(&[q.0, q.1, p.at(q)]);
                }
                grad.extend_from_slice(&[p.gx, p.gy]);
            }
        }
        let mut walls: Vec<[f64; 6]> = Vec::new();
        let n = pts.len();
        for i in 0..n {
            cut_edge(&pl, pts[i], pts[(i + 1) % n], 0, &mut walls);
        }
        (tri, grad, walls)
    });
    let m = grad.len() / 2;
    let w = walls.len();
    let flat: Vec<f64> = walls.iter().flat_map(|r| r.iter().copied()).collect();
    (numpy::ndarray::Array2::from_shape_vec((m * 3, 3), tri).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((m, 2), grad).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((w, 6), flat).unwrap().into_pyarray(py))
}

type P = (f64, f64);

fn orient(a: P, b: P, c: P) -> f64 {
    (b.0 - a.0) * (c.1 - a.1) - (b.1 - a.1) * (c.0 - a.0)
}

/// Do segments ab and cd cross at a point inside both (not at an end)?
fn proper(a: P, b: P, c: P, d: P) -> bool {
    let (o1, o2, o3, o4) = (orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b));
    o1 * o2 < -1e-12 && o3 * o4 < -1e-12
}

fn inside(ring: &[P], p: P) -> bool {
    let mut c = false;
    let n = ring.len();
    for i in 0..n {
        let (a, b) = (ring[i], ring[(i + 1) % n]);
        if (a.1 > p.1) != (b.1 > p.1) && p.0 < a.0 + (p.1 - a.1) / (b.1 - a.1) * (b.0 - a.0) {
            c = !c;
        }
    }
    c
}

/// The outer ring (counter-clockwise) with each hole (clockwise) joined to it by a bridge to a vertex it sees, holes in
/// the order of their largest x (Eberly's hole elimination, the nearest visible vertex rather than the ray's).
fn bridge_holes(outer: Vec<P>, mut holes: Vec<Vec<P>>) -> Vec<P> {
    holes.sort_by(|a, b| {
        let ma = a.iter().fold(f64::NEG_INFINITY, |m, p| m.max(p.0));
        let mb = b.iter().fold(f64::NEG_INFINITY, |m, p| m.max(p.0));
        mb.partial_cmp(&ma).unwrap_or(std::cmp::Ordering::Equal)
    });
    let mut ring = outer;
    for h in 0..holes.len() {
        let hole = holes[h].clone();
        let mi = (0..hole.len()).max_by(|&i, &j| hole[i].0.partial_cmp(&hole[j].0).unwrap()).unwrap();
        let m = hole[mi];
        let mut order: Vec<usize> = (0..ring.len()).collect();
        order.sort_by(|&i, &j| {
            let di = (ring[i].0 - m.0).powi(2) + (ring[i].1 - m.1).powi(2);
            let dj = (ring[j].0 - m.0).powi(2) + (ring[j].1 - m.1).powi(2);
            di.partial_cmp(&dj).unwrap()
        });
        let edges_ok = |v: P| {
            let blocked = |r: &[P]| (0..r.len()).any(|i| proper(m, v, r[i], r[(i + 1) % r.len()]));
            !(blocked(&ring) || holes[h..].iter().any(|o| blocked(o)))
        };
        let pick = order.into_iter().find(|&vi| {
            let v = ring[vi];
            let mid = ((m.0 + v.0) / 2.0, (m.1 + v.1) / 2.0);
            edges_ok(v) && inside(&ring, mid) && !inside(&hole, mid)
        });
        let Some(vi) = pick else { continue };      // (no bridge found: that hole is left filled)
        let mut out = Vec::with_capacity(ring.len() + hole.len() + 2);
        out.extend_from_slice(&ring[..=vi]);
        out.extend(hole[mi..].iter().copied());
        out.extend(hole[..=mi].iter().copied());
        out.extend_from_slice(&ring[vi..]);
        ring = out;
    }
    ring
}

/// ear_clip for a bridged ring: a vertex at the same place as a corner of the ear (a bridge's two copies) does not
/// block it.
fn ear_clip_bridged(pts: &[P]) -> Vec<[i64; 3]> {
    let n = pts.len();
    let same = |a: P, b: P| (a.0 - b.0).abs() < 1e-9 && (a.1 - b.1).abs() < 1e-9;
    let mut idx: Vec<usize> = (0..n).collect();
    let mut out: Vec<[i64; 3]> = Vec::new();
    let mut guard = 0usize;
    while idx.len() > 3 && guard < 4 * n * n {
        guard += 1;
        let m = idx.len();
        let mut found = false;
        for k in 0..m {
            let (i0, i1, i2) = (idx[(k + m - 1) % m], idx[k], idx[(k + 1) % m]);
            let (pa, pb, pc) = (pts[i0], pts[i1], pts[i2]);
            if orient(pa, pb, pc) <= 1e-12 {
                continue;
            }
            let ear = idx.iter().all(|&j| {
                if j == i0 || j == i1 || j == i2 { return true; }
                let q = pts[j];
                if same(q, pa) || same(q, pb) || same(q, pc) { return true; }
                !(orient(pa, pb, q) >= 0.0 && orient(pb, pc, q) >= 0.0 && orient(pc, pa, q) >= 0.0)
            });
            if ear {
                out.push([i0 as i64, i1 as i64, i2 as i64]);
                idx.remove(k);
                found = true;
                break;
            }
        }
        if !found {
            break;
        }
    }
    if idx.len() == 3 {
        out.push([idx[0] as i64, idx[1] as i64, idx[2] as i64]);
    }
    out
}

/// buildings' flat roof over a courtyard building: the outer ring (n, 2) and its holes (each (k, 2), any winding),
/// triangulated with the holes left open: (points (m, 2), triangles (t, 3) counter-clockwise from above). A hole no
/// bridge reaches stays filled.
#[pyfunction]
pub fn triangulate_holes<'py>(py: Python<'py>, outer: PyReadonlyArray2<'py, f64>, holes: Vec<PyReadonlyArray2<'py, f64>>)
                              -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>) {
    let rd = |a: &PyReadonlyArray2<'py, f64>| -> Vec<P> {
        let v = a.as_array();
        (0..v.nrows()).map(|i| (v[[i, 0]], v[[i, 1]])).collect()
    };
    let area = |r: &[P]| -> f64 { (0..r.len()).map(|i| r[i].0 * r[(i + 1) % r.len()].1 - r[(i + 1) % r.len()].0 * r[i].1).sum() };
    let mut o = rd(&outer);
    let hs: Vec<Vec<P>> = holes.iter().map(|h| rd(h)).collect();
    let (pts, tris) = py.detach(move || {
        if area(&o) < 0.0 { o.reverse(); }
        let hs: Vec<Vec<P>> = hs.into_iter().filter(|h| h.len() >= 3).map(|mut h| {
            if area(&h) > 0.0 { h.reverse(); }
            h
        }).collect();
        let ring = bridge_holes(o, hs);
        let tris = ear_clip_bridged(&ring);
        (ring, tris)
    });
    let flat: Vec<f64> = pts.iter().flat_map(|p| [p.0, p.1]).collect();
    let ti: Vec<i64> = tris.iter().flat_map(|t| t.iter().copied()).collect();
    (numpy::ndarray::Array2::from_shape_vec((pts.len(), 2), flat).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((tris.len(), 3), ti).unwrap().into_pyarray(py))
}

// ------------------------------------------------------------------------------------------------ straight skeleton
// A hipped roof over any outline (and its courtyards): the straight skeleton's wavefront (Felkel & Obdrzalek 1998,
// written from the paper: every edge moving inward at unit speed, vertices along their bisectors; edge events where an
// edge shrinks to nothing, split events where a reflex vertex runs into another edge). Each original edge's face is the
// roof's slope over it; a point's height is the time the wavefront passed it times the slope. Small outlines only
// (buildings): every candidate event is computed again each step (O(n^2) per event). Checked: the faces must cover the
// outline's area, else None (the caller keeps its other roof).

#[derive(Clone)]
struct WV {
    p: P,          // position at time t
    t: f64,
    v: P,          // velocity
    prev: usize,
    next: usize,
    e_in: usize,   // original edge arriving (from prev)
    e_out: usize,  // original edge leaving (to next)
    node: usize,   // skeleton node it started from
    alive: bool,
}

fn at_t(w: &WV, t: f64) -> P {
    (w.p.0 + w.v.0 * (t - w.t), w.p.1 + w.v.1 * (t - w.t))
}

fn bisector(n1: P, n2: P) -> Option<P> {
    let d = 1.0 + n1.0 * n2.0 + n1.1 * n2.1;
    if d < 1e-9 {
        return None;
    }
    Some(((n1.0 + n2.0) / d, (n1.1 + n2.1) / d))
}

/// The faces of the skeleton roof: per original edge a polygon of (x, y, time) points; None when it fails.
type Faces = (Vec<Vec<(f64, f64, f64)>>, Vec<[(f64, f64, f64); 3]>);

/// gable: each hip end (a face of three corners whose top is where a ridge ends) turned into a gable: its top moved
/// onto its wall (the ridge runs on to it; Streets GL's idea), the face a wall, returned apart (the gables).
fn skeleton_faces(loops: &[Vec<P>], gable: bool) -> Option<Faces> {
    // original edges: (a, b, inward normal); the region is on each directed edge's left
    let mut edges: Vec<(P, P, P)> = Vec::new();
    let mut verts: Vec<WV> = Vec::new();
    let mut nodes: Vec<(f64, f64, f64)> = Vec::new();
    let mut arcs: Vec<(usize, usize, usize, usize)> = Vec::new(); // (node from, node to, edge left, edge right)
    for lp in loops {
        let n = lp.len();
        if n < 3 { return None; }
        let base = edges.len();
        for i in 0..n {
            let (a, b) = (lp[i], lp[(i + 1) % n]);
            let (dx, dy) = (b.0 - a.0, b.1 - a.1);
            let l = (dx * dx + dy * dy).sqrt();
            if l < 1e-6 { return None; }
            edges.push((a, b, (-dy / l, dx / l)));
        }
        let vbase = verts.len();
        for i in 0..n {
            let e_in = base + (i + n - 1) % n;
            let e_out = base + i;
            let v = bisector(edges[e_in].2, edges[e_out].2)?;
            nodes.push((lp[i].0, lp[i].1, 0.0));
            verts.push(WV { p: lp[i], t: 0.0, v, prev: vbase + (i + n - 1) % n, next: vbase + (i + 1) % n,
                            e_in, e_out, node: nodes.len() - 1, alive: true });
        }
    }
    let ne = edges.len();
    let mut now = 0.0f64;
    let mut guard = 0;
    loop {
        guard += 1;
        if guard > 20 * ne + 50 { return None; }
        let alive: Vec<usize> = (0..verts.len()).filter(|&i| verts[i].alive).collect();
        if alive.is_empty() { break; }
        // a loop of two: the vertices meet where they are heading (a ridge's end)
        let mut done2 = false;
        for &i in &alive {
            let w = &verts[i];
            if verts[w.next].next == i && w.next != i {
                // a loop of two: the wavefront is a doubled segment, the ridge between its ends
                let j = w.next;
                let mut ends = [0usize; 2];
                for (q, k) in [i, j].into_iter().enumerate() {
                    let pk = at_t(&verts[k], now);
                    let nk = verts[k].node;
                    let (x, y, _) = nodes[nk];
                    if (x - pk.0).abs() > 1e-7 || (y - pk.1).abs() > 1e-7 {
                        nodes.push((pk.0, pk.1, now));
                        arcs.push((nk, nodes.len() - 1, verts[k].e_in, verts[k].e_out));
                        ends[q] = nodes.len() - 1;
                    } else {
                        ends[q] = nk;
                    }
                    verts[k].alive = false;
                }
                if ends[0] != ends[1] {
                    arcs.push((ends[0], ends[1], verts[i].e_out, verts[j].e_out));
                }
                done2 = true;
                break;
            }
            if w.next == i { verts[i].alive = false; done2 = true; break; }
        }
        if done2 { continue; }
        // the earliest event
        let mut best: Option<(f64, u8, usize, usize, P)> = None;  // (time, kind 0 edge / 1 split, vertex, edge-vertex, point)
        for &i in &alive {
            let a = &verts[i];
            let b = &verts[a.next];
            let e = a.e_out;
            let d = { let (p, q, _) = edges[e]; let l = ((q.0 - p.0).powi(2) + (q.1 - p.1).powi(2)).sqrt(); ((q.0 - p.0) / l, (q.1 - p.1) / l) };
            let (pa, pb) = (at_t(a, now), at_t(b, now));
            let len = (pb.0 - pa.0) * d.0 + (pb.1 - pa.1) * d.1;
            let rate = (b.v.0 - a.v.0) * d.0 + (b.v.1 - a.v.1) * d.1;
            if rate < -1e-9 {
                let dt = (-len / rate).max(0.0);
                let t = now + dt;
                if best.map_or(true, |bb| t < bb.0 - 1e-9) {
                    best = Some((t, 0, i, a.next, at_t(a, t)));
                }
            }
            // a reflex vertex: its wavefront runs into another edge
            let n1 = edges[a.e_in].2;
            let n2 = edges[a.e_out].2;
            let reflex = n1.0 * n2.1 - n1.1 * n2.0 < -1e-9;
            if !reflex { continue; }
            for &j in &alive {
                let f = verts[j].e_out;
                if j == i || verts[j].next == i || f == a.e_in || f == a.e_out { continue; }
                let nf = edges[f].2;
                let pj = at_t(&verts[j], now);
                let s = (pa.0 - pj.0) * nf.0 + (pa.1 - pj.1) * nf.1;
                let den = 1.0 - (a.v.0 * nf.0 + a.v.1 * nf.1);
                if s < -1e-7 || den <= 1e-9 { continue; }
                let dt = s / den;
                let t = now + dt;
                if best.map_or(false, |bb| t >= bb.0 - 1e-9) { continue; }
                let hit = at_t(a, t);
                let (qa, qb) = (at_t(&verts[j], t), at_t(&verts[verts[j].next], t));
                let ed = (qb.0 - qa.0, qb.1 - qa.1);
                let l2 = ed.0 * ed.0 + ed.1 * ed.1;
                if l2 < 1e-12 { continue; }
                let u = ((hit.0 - qa.0) * ed.0 + (hit.1 - qa.1) * ed.1) / l2;
                if u <= 1e-6 || u >= 1.0 - 1e-6 { continue; }
                best = Some((t, 1, i, j, hit));
            }
        }
        let Some((t, kind, i, j, p)) = best else { return None };
        now = t;
        nodes.push((p.0, p.1, t));
        let nn = nodes.len() - 1;
        if kind == 0 {
            // i and j (= i.next) meet: one vertex in their place
            let (a, b) = (verts[i].clone(), verts[j].clone());
            arcs.push((a.node, nn, a.e_in, a.e_out));
            arcs.push((b.node, nn, b.e_in, b.e_out));
            verts[i].alive = false;
            verts[j].alive = false;
            if a.prev == j {
                continue;           // (a loop of two collapsed)
            }
            let v = bisector(edges[a.e_in].2, edges[b.e_out].2).unwrap_or((0.0, 0.0));
            let k = verts.len();
            verts.push(WV { p, t, v, prev: a.prev, next: b.next, e_in: a.e_in, e_out: b.e_out, node: nn, alive: true });
            verts[a.prev].next = k;
            verts[b.next].prev = k;
            if a.prev == b.next {
                // (a triangle: the last vertex arrives here too)
                let c = verts[a.prev].clone();
                arcs.push((c.node, nn, c.e_in, c.e_out));
                verts[a.prev].alive = false;
                verts[k].alive = false;
            }
        } else {
            // i (reflex) splits edge j -> j.next
            let r = verts[i].clone();
            let (ja, jb) = (j, verts[j].next);
            let f = verts[ja].e_out;
            arcs.push((r.node, nn, r.e_in, r.e_out));
            verts[i].alive = false;
            // (between two opposite edges a vertex stays where it is: that arm has collapsed to a ridge, its loop
            // closes as a pair below)
            let v1 = bisector(edges[r.e_in].2, edges[f].2).unwrap_or((0.0, 0.0));
            let v2 = bisector(edges[f].2, edges[r.e_out].2).unwrap_or((0.0, 0.0));
            let c1 = verts.len();
            verts.push(WV { p, t, v: v1, prev: r.prev, next: jb, e_in: r.e_in, e_out: f, node: nn, alive: true });
            let c2 = verts.len();
            verts.push(WV { p, t, v: v2, prev: ja, next: r.next, e_in: f, e_out: r.e_out, node: nn, alive: true });
            verts[r.prev].next = c1;
            verts[jb].prev = c1;
            verts[ja].next = c2;
            verts[r.next].prev = c2;
        }
    }
    // each original edge's face: its base, then the arcs bounding it from its end back to its start
    let mut faces_i: Vec<Vec<usize>> = Vec::with_capacity(ne);
    for e in 0..ne {
        let mut adj: std::collections::HashMap<usize, Vec<usize>> = std::collections::HashMap::new();
        for (k, &(a, b, l, r)) in arcs.iter().enumerate() {
            if l == e || r == e {
                adj.entry(a).or_default().push(k);
                adj.entry(b).or_default().push(k);
            }
        }
        // the base's start and end nodes: the original vertices whose e_out / e_in is e
        let start = (0..verts.len()).find(|&k| verts[k].t == 0.0 && verts[k].e_out == e).map(|k| verts[k].node)?;
        let end = (0..verts.len()).find(|&k| verts[k].t == 0.0 && verts[k].e_in == e).map(|k| verts[k].node)?;
        let mut poly = vec![start, end];
        let mut used = vec![false; arcs.len()];
        let mut cur = end;
        let mut steps = 0;
        while cur != start {
            steps += 1;
            if steps > arcs.len() + 2 { return None; }
            let nxt = adj.get(&cur).and_then(|v| v.iter().copied().find(|&k| !used[k]))?;
            used[nxt] = true;
            let (a, b, _, _) = arcs[nxt];
            cur = if a == cur { b } else { a };
            if cur != start {
                poly.push(cur);
            }
        }
        faces_i.push(poly);
    }
    let mut gables: Vec<[(f64, f64, f64); 3]> = Vec::new();
    if gable {
        let mut degree = vec![0usize; nodes.len()];
        for &(a, b, _, _) in &arcs {
            if a != b {
                degree[a] += 1;
                degree[b] += 1;
            }
        }
        let mut moved = vec![false; nodes.len()];
        for f in faces_i.iter_mut() {
            if f.len() != 3 || degree[f[2]] != 3 || moved[f[2]] { continue; }
            let (a, b, top) = (nodes[f[0]], nodes[f[1]], nodes[f[2]]);
            let (dx, dy) = (b.0 - a.0, b.1 - a.1);
            let l2 = dx * dx + dy * dy;
            if l2 < 1e-9 { continue; }
            let u = (((top.0 - a.0) * dx + (top.1 - a.1) * dy) / l2).clamp(0.0, 1.0);
            let q = (a.0 + dx * u, a.1 + dy * u, top.2);
            nodes[f[2]] = q;
            moved[f[2]] = true;
            gables.push([a, b, q]);
            f.clear();                          // (now a wall: no roof face)
        }
    }
    let faces = faces_i.iter().filter(|f| !f.is_empty()).map(|f| f.iter().map(|&k| nodes[k]).collect()).collect();
    Some((faces, gables))
}

/// buildings._sky_mesh's walls of one building: a quad per edge of ring (n, 2) from z0 to its corners' tops zt (n,)
/// (P 12 numbers and UV 8 per quad, the block Mesh.quads takes): with windows, u along the walls from u0 in bays of bw
/// m (CPython's math.hypot: py_hypot), v up from v0 in storeys of fh m; without, every corner at uv. Returns (P, UV)
/// (version 242: the per-wall loop in Python was ~1/10 of a skyline tile).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn sky_walls<'py>(py: Python<'py>, ring: PyReadonlyArray2<'py, f64>, zt: numpy::PyReadonlyArray1<'py, f64>, z0: f64,
                      u0: f64, v0: f64, fh: f64, bw: f64, windows: bool, uv: (f64, f64))
                      -> PyResult<(Bound<'py, numpy::PyArray1<f64>>, Bound<'py, numpy::PyArray1<f64>>)> {
    let (r, zt) = (ring.as_array(), zt.as_array());
    let n = r.nrows();
    if zt.len() != n || r.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("sky_walls: a top per corner"));
    }
    let (mut p, mut t) = (Vec::with_capacity(12 * n), Vec::with_capacity(8 * n));
    let mut u = u0;
    for i in 0..n {
        let j = (i + 1) % n;
        let (ax, ay, bx, by) = (r[[i, 0]], r[[i, 1]], r[[j, 0]], r[[j, 1]]);
        let (za, zb) = (zt[i], zt[j]);
        p.extend_from_slice(&[ax, ay, z0, bx, by, z0, bx, by, zb, ax, ay, za]);
        if windows {
            let du = crate::py_hypot(bx - ax, by - ay) / bw;
            t.extend_from_slice(&[u, v0, u + du, v0, u + du, v0 + (zb - z0) / fh, u, v0 + (za - z0) / fh]);
            u += du;
        } else {
            t.extend_from_slice(&[uv.0, uv.1, uv.0, uv.1, uv.0, uv.1, uv.0, uv.1]);
        }
    }
    Ok((numpy::IntoPyArray::into_pyarray(p, py), numpy::IntoPyArray::into_pyarray(t, py)))
}

/// buildings._skel_roof's texture coordinates of skeleton_roof's triangles (tri (m * 3, 3), grad (m, 2)): per corner
/// (along the slope's level line, up the slope stretched by its length) / uv_m, as the per-triangle Python loop made
/// them (CPython's math.hypot: py_hypot). Returns (m * 3, 2) (version 242).
#[pyfunction]
pub fn skeleton_roof_uv<'py>(py: Python<'py>, tri: PyReadonlyArray2<'py, f64>, grad: PyReadonlyArray2<'py, f64>,
                             uv_m: f64) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (t, g) = (tri.as_array(), grad.as_array());
    if t.nrows() != 3 * g.nrows() || t.ncols() < 2 || g.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("skeleton_roof_uv: three corners per slope"));
    }
    let mut out = numpy::ndarray::Array2::<f64>::zeros((t.nrows(), 2));
    for j in 0..g.nrows() {
        let (gx, gy) = (g[[j, 0]], g[[j, 1]]);
        let gm = crate::py_hypot(gx, gy);
        let (d, a, f) = if gm < 1e-9 {
            ((0.0, 1.0), (1.0, 0.0), 1.0)
        } else {
            let d = (gx / gm, gy / gm);
            (d, (-d.1, d.0), (1.0 + gm * gm).sqrt())
        };
        for i in 0..3 {
            let (px, py_) = (t[[3 * j + i, 0]], t[[3 * j + i, 1]]);
            out[[3 * j + i, 0]] = (px * a.0 + py_ * a.1) / uv_m;
            out[[3 * j + i, 1]] = (px * d.0 + py_ * d.1) * f / uv_m;
        }
    }
    Ok(out.into_pyarray(py))
}

/// buildings._roof's hipped roof over any outline (and its courtyards) by the straight skeleton: outer (n, 2), holes
/// [(k, 2)] (any winding), slope k, eave e, cap: the most the roof rises (a flat top over it; <= 0: none). Returns
/// (triangles (m * 3, 3) xyz, counter-clockwise from above; their slopes (m, 2)), or empty arrays when the skeleton
/// failed (the caller's other roof).
#[pyfunction]
pub fn skeleton_roof<'py>(py: Python<'py>, outer: PyReadonlyArray2<'py, f64>, holes: Vec<PyReadonlyArray2<'py, f64>>,
                          k: f64, e: f64, cap: f64, gable: bool)
                          -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>) {
    let rd = |a: &PyReadonlyArray2<'py, f64>| -> Vec<P> {
        let v = a.as_array();
        (0..v.nrows()).map(|i| (v[[i, 0]], v[[i, 1]])).collect()
    };
    let area = |r: &[P]| -> f64 { (0..r.len()).map(|i| r[i].0 * r[(i + 1) % r.len()].1 - r[(i + 1) % r.len()].0 * r[i].1).sum::<f64>() / 2.0 };
    let mut o = rd(&outer);
    let hs: Vec<Vec<P>> = holes.iter().map(|h| rd(h)).collect();
    let (tri, grad, gab) = py.detach(move || {
        if area(&o) < 0.0 { o.reverse(); }
        let mut loops = vec![o.clone()];
        let mut want = area(&o);
        for mut h in hs {
            if h.len() < 3 { continue; }
            if area(&h) > 0.0 { h.reverse(); }
            want += area(&h);
            loops.push(h);
        }
        let mut tri: Vec<f64> = Vec::new();
        let mut grad: Vec<f64> = Vec::new();
        let mut gab: Vec<f64> = Vec::new();
        let orig: Vec<P> = loops.iter().flatten().copied().collect();
        // (a tenth of a millimetre's nudge per corner, the same every build: an L's arm walls meet at the very time
        // its reflex corner reaches the far wall, and the vertex left between two opposite edges has no bisector;
        // the eave's corners are put back on the outline below)
        for (li, lp) in loops.iter_mut().enumerate() {
            for (i, q) in lp.iter_mut().enumerate() {
                let h = ((li * 7919 + i * 104729) as f64 * 12.9898).dsin() * 43758.5453;
                let (a, b) = (h.fract(), (h * 1.618).fract());
                q.0 += (a - 0.5) * 2e-4;
                q.1 += (b - 0.5) * 2e-4;
            }
        }
        let Some((faces, gables)) = skeleton_faces(&loops, gable) else { return (tri, grad, gab) };
        let zcap = if cap > 0.0 { e + cap } else { f64::INFINITY };
        let mut got = 0.0;
        let mut out: Vec<[(f64, f64, f64); 3]> = Vec::new();
        for f0 in &faces {
            // (simultaneous events leave a node twice: consecutive copies dropped)
            let mut f: Vec<(f64, f64, f64)> = Vec::with_capacity(f0.len());
            for q in f0 {
                if f.last().map_or(true, |l: &(f64, f64, f64)| (l.0 - q.0).abs() > 1e-7 || (l.1 - q.1).abs() > 1e-7) {
                    f.push(*q);
                }
            }
            while f.len() > 1 && (f[0].0 - f[f.len() - 1].0).abs() <= 1e-7 && (f[0].1 - f[f.len() - 1].1).abs() <= 1e-7 {
                f.pop();
            }
            if f.len() < 3 { continue; }
            let xy: Vec<P> = f.iter().map(|q| (q.0, q.1)).collect();
            if area(&xy) <= 1e-9 { continue; }
            for t in ear_clip_bridged(&xy) {
                let pts = [f[t[0] as usize], f[t[1] as usize], f[t[2] as usize]];
                got += area(&[(pts[0].0, pts[0].1), (pts[1].0, pts[1].1), (pts[2].0, pts[2].1)]);
                out.push([(pts[0].0, pts[0].1, e + k * pts[0].2), (pts[1].0, pts[1].1, e + k * pts[1].2),
                          (pts[2].0, pts[2].1, e + k * pts[2].2)]);
            }
        }
        if (got - want).abs() > 0.01 * want.abs() + 0.5 {
            return (Vec::new(), Vec::new(), Vec::new());   // (the skeleton did not cover the outline: the other roof)
        }
        for t3 in out.iter_mut() {
            for q in t3.iter_mut() {
                if (q.2 - e).abs() < 1e-9 {
                    if let Some(o) = orig.iter().find(|o| (o.0 - q.0).abs() < 5e-4 && (o.1 - q.1).abs() < 5e-4) {
                        q.0 = o.0;
                        q.1 = o.1;
                    }
                }
            }
        }
        for t3 in out {
            // (cut at the cap: the part over it flat at zcap)
            let below: Vec<(f64, f64, f64)> = clip_z(&t3, zcap, true);
            let above: Vec<(f64, f64, f64)> = clip_z(&t3, zcap, false);
            for (poly, flat) in [(below, false), (above, true)] {
                if poly.len() < 3 { continue; }
                for i in 1..poly.len() - 1 {
                    let mut tr = [poly[0], poly[i], poly[i + 1]];
                    if flat { for q in tr.iter_mut() { q.2 = zcap; } }
                    // the triangle's slope: z = z0 + gx x + gy y
                    let (a, b, c) = (tr[0], tr[1], tr[2]);
                    let (ux, uy, uz) = (b.0 - a.0, b.1 - a.1, b.2 - a.2);
                    let (vx, vy, vz) = (c.0 - a.0, c.1 - a.1, c.2 - a.2);
                    let nz = ux * vy - uy * vx;
                    if nz.abs() < 1e-9 { continue; }
                    let gx = -(uy * vz - uz * vy) / nz;
                    let gy = -(uz * vx - ux * vz) / nz;
                    for q in tr { tri.extend_from_slice(&[q.0, q.1, q.2]); }
                    grad.extend_from_slice(&[gx, gy]);
                }
            }
        }
        // the gables' walls, under the cap (over it the roof is flat: the wall stops there too)
        for g in gables {
            let mut t3 = [(g[0].0, g[0].1, e), (g[1].0, g[1].1, e), (g[2].0, g[2].1, e + k * g[2].2)];
            for q in t3.iter_mut().take(2) {
                if let Some(o) = orig.iter().find(|o| (o.0 - q.0).abs() < 5e-4 && (o.1 - q.1).abs() < 5e-4) {
                    q.0 = o.0;
                    q.1 = o.1;
                }
            }
            let below = clip_z(&t3, zcap, true);
            for i in 1..below.len().saturating_sub(1) {
                for q in [below[0], below[i], below[i + 1]] { gab.extend_from_slice(&[q.0, q.1, q.2]); }
            }
        }
        (tri, grad, gab)
    });
    let m = grad.len() / 2;
    let g = gab.len() / 3;
    (numpy::ndarray::Array2::from_shape_vec((m * 3, 3), tri).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((m, 2), grad).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((g, 3), gab).unwrap().into_pyarray(py))
}

/// A triangle's part under (below: true) or over z.
fn clip_z(t: &[(f64, f64, f64); 3], z: f64, below: bool) -> Vec<(f64, f64, f64)> {
    let inside = |q: &(f64, f64, f64)| if below { q.2 <= z } else { q.2 >= z };
    let mut out = Vec::new();
    for i in 0..3 {
        let (p, q) = (t[i], t[(i + 1) % 3]);
        if inside(&p) { out.push(p); }
        if inside(&p) != inside(&q) {
            let s = (z - p.2) / (q.2 - p.2);
            out.push((p.0 + (q.0 - p.0) * s, p.1 + (q.1 - p.1) * s, z));
        }
    }
    out
}

#[cfg(test)]
mod slope_wall_tests {
    use super::*;

    #[test]
    fn slope_wall_floors_cut_by_floor() {
        // a 30 m wall rising from the eave (10 m) at a to 10 + 9 m at b, 3 m floors: 3 floors, the slab triangles
        // under the slope; a low wall (rise 1 m) left out
        let rows = [[0.0, 0.0, 10.0, 30.0, 0.0, 19.0], [30.0, 0.0, 10.5, 30.0, 10.0, 11.0]];
        let (p, uv) = slope_wall_floors_core(&rows, 10.0, 3.0, 12.0, 1, 4.0, 2.0);
        assert!(!p.is_empty() && p.len() % 3 == 0);
        assert!(p.iter().all(|q| q.1 == 0.0), "only the tall wall");
        let top = p.iter().map(|q| q.2).fold(f64::MIN, f64::max);
        assert!((top - 19.0).abs() < 1e-9);
        // every vertex under the slope line
        assert!(p.iter().all(|q| q.2 <= 10.0 + 9.0 * q.0 / 30.0 + 1e-9));
        // area: the triangle 30 x 9 / 2
        let mut area = 0.0;
        for t in p.chunks(3) {
            let (a, b, c) = (t[0], t[1], t[2]);
            area += ((b.0 - a.0) * (c.2 - a.2) - (c.0 - a.0) * (b.2 - a.2)) / 2.0;
        }
        assert!((area - 135.0).abs() < 1e-6, "{area}");      // (counter-clockwise from outside: positive)
        // u along the wall per 12 m, v in band 1's rows
        let eps = 1.5 / 1024.0;
        assert!(uv.iter().all(|q| q.0 >= -1e-9 && q.0 <= 2.5 + 1e-9));
        assert!(uv.iter().all(|q| q.1 >= 0.5 + eps - 1e-9 && q.1 <= 0.75 - eps + 1e-9));
    }
}
