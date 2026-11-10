//! Pitched roofs on a footprint's own outline (buildings._roof's heightfield roofs, research 2026-11-10: the roofs were
//! drawn on the footprint's oriented box only and a footprint filling less than 80 % of it went flat: 11-13 % of
//! houses, every L / T / U house). The idea is OSM2World's (MIT; no code taken): a roof is a heightfield over the
//! outline, here the lowest of a few planes (a gable's two slopes, a hipped roof's four, a half-hipped roof's two
//! slopes and two hips raised to its gables' tops, a lean-to's one). The outline is ear clipped, every piece whose
//! corners take different planes is cut along the line where two of them meet until each piece lies on one plane
//! (a concave piecewise-planar surface is that plane on a convex piece whose corners all take it), and each edge of
//! the outline is cut the same way for the wall up to the roof.
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
