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

const MAX_CELLS: usize = 20_000; // the pole search's budget: cells made (a footprint's outline: a few hundred at most)
const MAX_POPS: usize = 20_000; // ... and cells looked at
const ROOT_CELLS: usize = 32; // root cells along the outline's long side at most
const MIN_EXTENT_M: f64 = 1e-6; // an outline smaller than this: no dome
const DEGENERATE_SHARE: f64 = 1e-12; // twice the area under this share of the extent's square: collinear, no dome

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
/// Bounded whatever the outline (Codex review of a92a4828: a sliver's grid of square cells was its length over its
/// width cells, ~1e8 for a collinear ring, and float steps that stop advancing looped forever): at most ROOT_CELLS a
/// side of root cells, counted by index; children only while their half size still shrinks and stays over a
/// thousandth of the precision; at most MAX_CELLS cells and MAX_POPS queue pops in all. None for a ring that is not
/// finite or has no extent.
pub fn pole(r: &[P], precision: f64) -> Option<P> {
    if r.len() < 3 || !(precision > 0.0) || !precision.is_finite() || r.iter().any(|p| !p.0.is_finite() || !p.1.is_finite()) {
        return None;
    }
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for p in r {
        x0 = x0.min(p.0);
        y0 = y0.min(p.1);
        x1 = x1.max(p.0);
        y1 = y1.max(p.1);
    }
    let (w, h) = (x1 - x0, y1 - y0);
    let long = w.max(h);
    if !(long > MIN_EXTENT_M) || !long.is_finite() {
        return None;
    }
    // (square root cells of the short side, but at most ROOT_CELLS along the long one)
    let size = w.min(h).max(long / ROOT_CELLS as f64);
    let nx = ((w / size).ceil() as usize).clamp(1, ROOT_CELLS);
    let ny = ((h / size).ceil() as usize).clamp(1, ROOT_CELLS);
    let half = size / 2.0;
    let mut seq = 0usize;
    let mut heap = BinaryHeap::new();
    for i in 0..nx {
        for j in 0..ny {
            let (x, y) = (x0 + (i as f64 + 0.5) * size, y0 + (j as f64 + 0.5) * size);
            heap.push(Cell::new(r, x, y, half, seq));
            seq += 1;
        }
    }
    let mut best = Cell::new(r, x0 + w / 2.0, y0 + h / 2.0, 0.0, seq);
    seq += 1;
    let min_half = precision / 1000.0;
    let mut pops = 0usize;
    while let Some(c) = heap.pop() {
        pops += 1;
        if c.d > best.d {
            best = Cell { x: c.x, y: c.y, h: c.h, d: c.d, max: c.max, seq: c.seq };
        }
        if pops >= MAX_POPS {
            break;
        }
        let q = c.h / 2.0;
        if c.max - best.d <= precision || seq + 4 > MAX_CELLS || !(q < c.h) || !(q > min_half) {
            continue;
        }
        for (sx, sy) in [(-1.0, -1.0), (1.0, -1.0), (-1.0, 1.0), (1.0, 1.0)] {
            heap.push(Cell::new(r, c.x + sx * q, c.y + sy * q, q, seq));
            seq += 1;
        }
    }
    if best.x.is_finite() && best.y.is_finite() { Some((best.x, best.y)) } else { None }
}

/// The dome's centre on the outline r (counter-clockwise, finite, with an area): its area centroid when that lies
/// inside it, else its pole of inaccessibility (Streets GL's CurvedRoofBuilder.getCenter), else its corners' mean.
pub fn dome_centre(r: &[P]) -> P {
    let n = r.len();
    let mean = {
        let (mut sx, mut sy) = (0.0, 0.0);
        for p in r {
            sx += p.0;
            sy += p.1;
        }
        (sx / n.max(1) as f64, sy / n.max(1) as f64)
    };
    if n < 3 {
        return mean;
    }
    let o = r[0];
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
        if c.0.is_finite() && c.1.is_finite() && inside(r, c) {
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
    pole(r, ((x1 - x0).min(y1 - y0) / 100.0).max(0.02)).unwrap_or(mean)
}

/// The outline as the dome builds it: counter-clockwise, without repeated corners (a closing corner, a corner mapped
/// twice), or None for one that is not finite, has under three corners or no area (collinear, a sliver whose area
/// is under DEGENERATE_SHARE of its extent's square): no dome.
pub fn dome_ring(ring: &[P]) -> Option<Vec<P>> {
    if ring.iter().any(|p| !p.0.is_finite() || !p.1.is_finite()) {
        return None;
    }
    let mut r: Vec<P> = Vec::with_capacity(ring.len());
    for &p in ring {
        if r.last() != Some(&p) {
            r.push(p);
        }
    }
    while r.len() > 1 && r.first() == r.last() {
        r.pop();
    }
    if r.len() < 3 {
        return None;
    }
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for p in &r {
        x0 = x0.min(p.0);
        y0 = y0.min(p.1);
        x1 = x1.max(p.0);
        y1 = y1.max(p.1);
    }
    let ext = (x1 - x0).max(y1 - y0);
    let a = area2(&r);
    if !(ext > MIN_EXTENT_M) || !ext.is_finite() || !a.is_finite() || a.abs() <= DEGENERATE_SHARE * ext * ext {
        return None;
    }
    if a < 0.0 {
        r.reverse();
    }
    Some(r)
}

/// The dome's faces: (quads [12 numbers, counter-clockwise from outside], their texture coordinates [8], apex
/// triangles [9], theirs [6]). prof: the levels (scale toward the centre, share of the rise), from the eave (1, 0)
/// to the top (0, 1). Nothing for a degenerate outline (dome_ring) or a rise or profile that is not finite.
pub fn dome_faces(ring: &[P], z: f64, rise: f64, prof: &[P], uv_m: f64)
                  -> (Vec<[f64; 12]>, Vec<[f64; 8]>, Vec<[f64; 9]>, Vec<[f64; 6]>) {
    let (mut q, mut qu, mut t, mut tu) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    if !(rise > 0.0) || !rise.is_finite() || !z.is_finite() || !(uv_m > 0.0) || prof.len() < 2
        || prof.iter().any(|p| !p.0.is_finite() || !p.1.is_finite()) {
        return (q, qu, t, tu);
    }
    let r = match dome_ring(ring) {
        Some(r) => r,
        None => return (q, qu, t, tu),
    };
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
    numpy::ndarray::Array2::from_shape_vec((n, K), v.into_iter().flatten().collect())
        .unwrap_or_else(|_| numpy::ndarray::Array2::zeros((0, K)))
}

/// buildings._dome: a dome (or onion dome) of `rise` over the outline ring (n, 2) at eave z, its levels prof (k, 2)
/// (scale, share of the rise); texture coordinates the plan position / uv_m. Returns (quads (q, 12), their uvs (q, 8),
/// apex triangles (t, 9), theirs (t, 6)), all empty for an outline with no dome (dome_ring) (version 297). Arrays
/// narrower than two columns are a ValueError; a panic inside never crosses into Python (empty arrays).
#[pyfunction]
pub fn dome_roof<'py>(py: Python<'py>, ring: PyReadonlyArray2<'py, f64>, z: f64, rise: f64,
                      prof: PyReadonlyArray2<'py, f64>, uv_m: f64)
                      -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>,
                                   Bound<'py, PyArray2<f64>>)> {
    let rv = ring.as_array();
    let pv = prof.as_array();
    if (rv.nrows() > 0 && rv.ncols() < 2) || (pv.nrows() > 0 && pv.ncols() < 2) {
        return Err(pyo3::exceptions::PyValueError::new_err("dome_roof: (n, 2) rings and profiles"));
    }
    let r: Vec<P> = (0..rv.nrows()).map(|i| (rv[[i, 0]], rv[[i, 1]])).collect();
    let pr: Vec<P> = (0..pv.nrows()).map(|i| (pv[[i, 0]], pv[[i, 1]])).collect();
    let (q, qu, t, tu) = py.detach(move || {
        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| dome_faces(&r, z, rise, &pr, uv_m)))
            .unwrap_or_else(|_| (Vec::new(), Vec::new(), Vec::new(), Vec::new()))
    });
    Ok((rows(q).into_pyarray(py), rows(qu).into_pyarray(py), rows(t).into_pyarray(py), rows(tu).into_pyarray(py)))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(dome_roof, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use crate::dm::Dm;
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
                (5.0 * a.dcos(), 5.0 * a.dsin())     // (dm::Dm, not the platform's C maths: test_determinism)
            })
            .collect();
        let prof = [(1.0, 0.0), (0.7, 0.7), (0.0, 1.0)];
        let (q, _, t, _) = dome_faces(&ring, 10.0, 4.0, &prof, 6.0);
        assert_eq!(q.len(), n, "one ring of quads");
        assert_eq!(t.len(), n, "one ring of apex triangles");
        assert!(t.iter().all(|f| (f[8] - 14.0).abs() < 1e-12));
        assert!(q.iter().all(|f| (f[2] - 10.0).abs() < 1e-12));
    }

    fn quick<F: FnOnce() -> R, R>(f: F) -> R {
        let t0 = std::time::Instant::now();
        let r = f();
        assert!(t0.elapsed().as_millis() < 200, "took {:?}", t0.elapsed());
        r
    }

    const PROF: [P; 3] = [(1.0, 0.0), (0.6, 0.8), (0.0, 1.0)];

    #[test]
    fn collinear_ring_no_dome_fast() {
        let r = [(0.0, 0.0), (1.0, 1e-8), (2.0, 2e-8)];
        let (q, _, t, _) = quick(|| dome_faces(&r, 0.0, 3.0, &PROF, 6.0));
        assert!(q.is_empty() && t.is_empty());
        let _ = quick(|| pole(&r, 0.02));
        let c = quick(|| dome_centre(&r));
        assert!(c.0.is_finite() && c.1.is_finite());
    }

    #[test]
    fn sliver_fast() {
        // 100 m long, 1e-6 m wide (aspect 1e-8), as an L so the centroid is not used
        let r = [(0.0, 0.0), (100.0, 0.0), (100.0, 1e-6), (1e-6, 1e-6), (1e-6, 100.0), (0.0, 100.0)];
        let (q, _, _, _) = quick(|| dome_faces(&r, 0.0, 3.0, &PROF, 6.0));
        assert!(q.len() <= 2 * r.len());
        let p = quick(|| pole(&r, 0.02)).unwrap();
        assert!(p.0.is_finite() && p.1.is_finite());
        let thin = [(0.0, 0.0), (100.0, 0.0), (100.0, 1e-6), (0.0, 1e-6)];
        assert!(quick(|| pole(&thin, 1e-9)).is_some());
    }

    #[test]
    fn non_finite_rings_no_dome() {
        for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
            let r = [(0.0, 0.0), (10.0, 0.0), (10.0, bad), (0.0, 10.0)];
            let (q, _, t, _) = quick(|| dome_faces(&r, 0.0, 3.0, &PROF, 6.0));
            assert!(q.is_empty() && t.is_empty());
            assert!(pole(&r, 0.02).is_none());
        }
        let sq = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)];
        assert!(dome_faces(&sq, 0.0, f64::NAN, &PROF, 6.0).0.is_empty());
        assert!(dome_faces(&sq, 0.0, 3.0, &[(1.0, 0.0), (f64::NAN, 1.0)], 6.0).0.is_empty());
    }

    #[test]
    fn duplicate_points_dropped() {
        let r = [(0.0, 0.0), (0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)];
        let (q, _, t, _) = quick(|| dome_faces(&r, 0.0, 3.0, &PROF, 6.0));
        assert_eq!(q.len(), 4, "the square's four corners");
        assert_eq!(t.len(), 4);
        let same = [(0.0, 0.0); 5];
        assert!(dome_faces(&same, 0.0, 3.0, &PROF, 6.0).0.is_empty());
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
