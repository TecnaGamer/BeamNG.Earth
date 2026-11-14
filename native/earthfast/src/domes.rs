//! Domes and onion domes over any outline (buildings._dome; streets.gl comparison 2026-11-14).
//!
//! The dome's centre and its radial build are ported from Streets GL's CurvedRoofBuilder (getCenter, getRoofPartPoints):
//! the outline's area centroid when it lies inside the outline, else its pole of inaccessibility, and every outline
//! vertex joined to that centre by the profile's levels (scale toward the centre, rise). Our dome was scaled toward the
//! mean of the outline's vertices, which on an L, a crescent or a long apse lies outside the outline (the dome's rings
//! crossed its walls). The pole of inaccessibility is found by the usual quadtree cell search (Garcia-Castellanos and
//! Lombardo 2007, as Mapbox polylabel does it); written here, no code taken.
//!
//! Streets GL: https://github.com/StrandedKitty/streets-gl
//!   MIT License
//!
//!   Copyright (c) 2020-2023 StrandedKitty
//!
//!   Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
//!   documentation files (the "Software"), to deal in the Software without restriction, including without limitation
//!   the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and
//!   to permit persons to whom the Software is furnished to do so, subject to the following conditions:
//!
//!   The above copyright notice and this permission notice shall be included in all copies or substantial portions of
//!   the Software.
//!
//!   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
//!   THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
//!   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
//!   TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
//!   SOFTWARE.
//!
//! Determinism: only + - * / and sqrt (IEEE exact on every CPU), sums in the outline's order, the cell queue ordered
//! by its bound and then by the order cells were made: the same dome on Linux and Windows.
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2};
use pyo3::prelude::*;
use std::cmp::Ordering;
use std::collections::BinaryHeap;

type P = (f64, f64);

const MAX_CELLS: usize = 20_000; // the pole search's budget (a footprint's outline: a few hundred at most)

fn area2(r: &[P]) -> f64 {
    let n = r.len();
    let mut s = 0.0;
    for i in 0..n {
        let (a, b) = (r[i], r[(i + 1) % n]);
        s += a.0 * b.1 - b.0 * a.1;
    }
    s
}

fn inside(r: &[P], p: P) -> bool {
    let n = r.len();
    let mut ins = false;
    let mut j = n - 1;
    for i in 0..n {
        let (a, b) = (r[i], r[j]);
        if (a.1 > p.1) != (b.1 > p.1) && p.0 < (b.0 - a.0) * (p.1 - a.1) / (b.1 - a.1) + a.0 {
            ins = !ins;
        }
        j = i;
    }
    ins
}

fn seg_dist2(p: P, a: P, b: P) -> f64 {
    let (dx, dy) = (b.0 - a.0, b.1 - a.1);
    let l2 = dx * dx + dy * dy;
    let t = if l2 > 0.0 { (((p.0 - a.0) * dx + (p.1 - a.1) * dy) / l2).clamp(0.0, 1.0) } else { 0.0 };
    let (qx, qy) = (a.0 + t * dx - p.0, a.1 + t * dy - p.1);
    qx * qx + qy * qy
}

/// The signed distance from p to the outline: positive inside.
fn signed_dist(r: &[P], p: P) -> f64 {
    let n = r.len();
    let mut d2 = f64::INFINITY;
    for i in 0..n {
        let v = seg_dist2(p, r[i], r[(i + 1) % n]);
        if v < d2 {
            d2 = v;
        }
    }
    let d = d2.sqrt();
    if inside(r, p) { d } else { -d }
}

struct Cell {
    x: f64,
    y: f64,
    h: f64,
    d: f64,
    max: f64,
    seq: usize,
}

impl Cell {
    fn new(r: &[P], x: f64, y: f64, h: f64, seq: usize) -> Cell {
        let d = signed_dist(r, (x, y));
        Cell { x, y, h, d, max: d + h * std::f64::consts::SQRT_2, seq }
    }
}

impl PartialEq for Cell {
    fn eq(&self, o: &Self) -> bool {
        self.cmp(o) == Ordering::Equal
    }
}
impl Eq for Cell {}
impl PartialOrd for Cell {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}
impl Ord for Cell {
    // (the largest bound first; of equal bounds the cell made first)
    fn cmp(&self, o: &Self) -> Ordering {
        self.max.total_cmp(&o.max).then_with(|| o.seq.cmp(&self.seq))
    }
}

/// The pole of inaccessibility of the outline r (the inside point farthest from its edges), to within precision m.
pub fn pole(r: &[P], precision: f64) -> P {
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for p in r {
        x0 = x0.min(p.0);
        y0 = y0.min(p.1);
        x1 = x1.max(p.0);
        y1 = y1.max(p.1);
    }
    let (w, h) = (x1 - x0, y1 - y0);
    let size = w.min(h);
    if !(size > 0.0) {
        return r[0];
    }
    let half = size / 2.0;
    let mut seq = 0usize;
    let mut heap = BinaryHeap::new();
    let mut x = x0;
    while x < x1 {
        let mut y = y0;
        while y < y1 {
            heap.push(Cell::new(r, x + half, y + half, half, seq));
            seq += 1;
            y += size;
        }
        x += size;
    }
    let mut best = Cell::new(r, x0 + w / 2.0, y0 + h / 2.0, 0.0, seq);
    seq += 1;
    let mut made = seq;
    while let Some(c) = heap.pop() {
        if c.d > best.d {
            best = Cell { x: c.x, y: c.y, h: c.h, d: c.d, max: c.max, seq: c.seq };
        }
        if c.max - best.d <= precision || made >= MAX_CELLS {
            continue;
        }
        let q = c.h / 2.0;
        for (sx, sy) in [(-1.0, -1.0), (1.0, -1.0), (-1.0, 1.0), (1.0, 1.0)] {
            heap.push(Cell::new(r, c.x + sx * q, c.y + sy * q, q, seq));
            seq += 1;
            made += 1;
        }
    }
    (best.x, best.y)
}

/// The dome's centre on the outline r (counter-clockwise): its area centroid when that lies inside it, else its pole
/// of inaccessibility (Streets GL's CurvedRoofBuilder.getCenter).
pub fn dome_centre(r: &[P]) -> P {
    let o = r[0];
    let n = r.len();
    let (mut a, mut cx, mut cy) = (0.0, 0.0, 0.0);
    for i in 0..n {
        let (p, q) = ((r[i].0 - o.0, r[i].1 - o.1), (r[(i + 1) % n].0 - o.0, r[(i + 1) % n].1 - o.1));
        let c = p.0 * q.1 - q.0 * p.1;
        a += c;
        cx += (p.0 + q.0) * c;
        cy += (p.1 + q.1) * c;
    }
    if a.abs() > 1e-9 {
        let c = (o.0 + cx / (3.0 * a), o.1 + cy / (3.0 * a));
        if inside(r, c) {
            return c;
        }
    }
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for p in r {
        x0 = x0.min(p.0);
        y0 = y0.min(p.1);
        x1 = x1.max(p.0);
        y1 = y1.max(p.1);
    }
    pole(r, ((x1 - x0).min(y1 - y0) / 100.0).max(0.02))
}

/// The dome's faces: (quads [12 numbers, counter-clockwise from outside], their texture coordinates [8], apex
/// triangles [9], theirs [6]). prof: the levels (scale toward the centre, share of the rise), from the eave (1, 0)
/// to the top (0, 1).
pub fn dome_faces(ring: &[P], z: f64, rise: f64, prof: &[P], uv_m: f64)
                  -> (Vec<[f64; 12]>, Vec<[f64; 8]>, Vec<[f64; 9]>, Vec<[f64; 6]>) {
    let (mut q, mut qu, mut t, mut tu) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    if ring.len() < 3 || !(rise > 0.0) || prof.len() < 2 {
        return (q, qu, t, tu);
    }
    let mut r: Vec<P> = ring.to_vec();
    if area2(&r) < 0.0 {
        r.reverse();
    }
    let c = dome_centre(&r);
    let n = r.len();
    let levels: Vec<Vec<[f64; 3]>> = prof
        .iter()
        .map(|&(s, k)| r.iter().map(|p| [c.0 + (p.0 - c.0) * s, c.1 + (p.1 - c.1) * s, z + rise * k]).collect())
        .collect();
    let uv = |p: &[f64; 3]| (p[0] / uv_m, p[1] / uv_m);
    for w in levels.windows(2) {
        let (lo, hi) = (&w[0], &w[1]);
        for i in 0..n {
            let (a, b, bu, au) = (lo[i], lo[(i + 1) % n], hi[(i + 1) % n], hi[i]);
            let (ua, ub, ubu, uau) = (uv(&a), uv(&b), uv(&bu), uv(&au));
            if au == bu {
                // (the apex)
                t.push([a[0], a[1], a[2], b[0], b[1], b[2], au[0], au[1], au[2]]);
                tu.push([ua.0, ua.1, ub.0, ub.1, uau.0, uau.1]);
            } else {
                q.push([a[0], a[1], a[2], b[0], b[1], b[2], bu[0], bu[1], bu[2], au[0], au[1], au[2]]);
                qu.push([ua.0, ua.1, ub.0, ub.1, ubu.0, ubu.1, uau.0, uau.1]);
            }
        }
    }
    (q, qu, t, tu)
}

fn rows<const K: usize>(v: Vec<[f64; K]>) -> numpy::ndarray::Array2<f64> {
    let n = v.len();
    numpy::ndarray::Array2::from_shape_vec((n, K), v.into_iter().flatten().collect()).unwrap()
}

/// buildings._dome: a dome (or onion dome) of `rise` over the outline ring (n, 2) at eave z, its levels prof (k, 2)
/// (scale, share of the rise); texture coordinates the plan position / uv_m. Returns (quads (q, 12), their uvs (q, 8),
/// apex triangles (t, 9), theirs (t, 6)) (version 297).
#[pyfunction]
pub fn dome_roof<'py>(py: Python<'py>, ring: PyReadonlyArray2<'py, f64>, z: f64, rise: f64,
                      prof: PyReadonlyArray2<'py, f64>, uv_m: f64)
                      -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>,
                          Bound<'py, PyArray2<f64>>) {
    let rv = ring.as_array();
    let r: Vec<P> = (0..rv.nrows()).map(|i| (rv[[i, 0]], rv[[i, 1]])).collect();
    let pv = prof.as_array();
    let pr: Vec<P> = (0..pv.nrows()).map(|i| (pv[[i, 0]], pv[[i, 1]])).collect();
    let (q, qu, t, tu) = py.detach(move || dome_faces(&r, z, rise, &pr, uv_m));
    (rows(q).into_pyarray(py), rows(qu).into_pyarray(py), rows(t).into_pyarray(py), rows(tu).into_pyarray(py))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(dome_roof, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn convex_centre_is_the_centroid() {
        let sq = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)];
        let c = dome_centre(&sq);
        assert!((c.0 - 5.0).abs() < 1e-12 && (c.1 - 5.0).abs() < 1e-12);
    }

    #[test]
    fn crescent_centre_lies_inside() {
        // a U whose centroid falls in its notch: the pole of inaccessibility instead
        let u = [(0.0, 0.0), (30.0, 0.0), (30.0, 30.0), (20.0, 30.0), (20.0, 4.0), (10.0, 4.0), (10.0, 30.0), (0.0, 30.0)];
        let c = dome_centre(&u);
        assert!(inside(&u, c), "{c:?}");
        assert!(signed_dist(&u, c) > 3.0, "{c:?}");
    }

    #[test]
    fn faces_reach_the_apex_and_the_eave() {
        let n = 12;
        let ring: Vec<P> = (0..n)
            .map(|i| {
                let a = i as f64 * std::f64::consts::TAU / n as f64;
                (5.0 * a.cos(), 5.0 * a.sin())
            })
            .collect();
        let prof = [(1.0, 0.0), (0.7, 0.7), (0.0, 1.0)];
        let (q, _, t, _) = dome_faces(&ring, 10.0, 4.0, &prof, 6.0);
        assert_eq!(q.len(), n, "one ring of quads");
        assert_eq!(t.len(), n, "one ring of apex triangles");
        assert!(t.iter().all(|f| (f[8] - 14.0).abs() < 1e-12));
        assert!(q.iter().all(|f| (f[2] - 10.0).abs() < 1e-12));
    }

    #[test]
    fn same_dome_for_either_winding() {
        let sq = vec![(0.0, 0.0), (8.0, 0.0), (8.0, 6.0), (0.0, 6.0)];
        let mut cw = sq.clone();
        cw.reverse();
        let prof = [(1.0, 0.0), (0.5, 0.8), (0.0, 1.0)];
        assert_eq!(dome_faces(&sq, 0.0, 3.0, &prof, 6.0).0, dome_faces(&cw, 0.0, 3.0, &prof, 6.0).0);
    }
}
