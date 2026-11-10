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
