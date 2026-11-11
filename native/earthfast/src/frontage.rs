//! PR #505's paved frontages and European sidewalks (frontage_geometry.Coverage): disjoint pavement footprints in a
//! tile's frame, surveyed surfaces reserved first, then each new strip claims the free ground. The Python worked on
//! convex polygons of 3-8 points with numpy (np.roll, matrix products, a split per cutting line): ~3 µs of numpy
//! overhead per call over hundreds of thousands of calls, about half of a French road tile's frontage pass (16 Haguenau
//! tiles, 2026-11-10). The same steps and tests here; frontage_geometry._PyCoverage keeps the Python as the reference
//! (tests/test_frontage_native.py).

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2};
use pyo3::prelude::*;
use std::collections::{BTreeSet, HashMap};

const MIN_AREA2: f64 = 0.004;
const CELL_M: f64 = 12.0;
const EPS: f64 = 1e-7;

type Poly = Vec<(f64, f64)>;

fn area2(p: &[(f64, f64)]) -> f64 {
    // buildings._area2: dot(x, roll(y, -1)) - dot(roll(x, -1), y)
    let n = p.len();
    let (mut a, mut b) = (0.0, 0.0);
    for i in 0..n {
        let j = (i + 1) % n;
        a += p[i].0 * p[j].1;
        b += p[j].0 * p[i].1;
    }
    a - b
}

/// frontage_geometry._convex_parts: the ring itself when convex (counter-clockwise), else its ear-clipped triangles;
/// none for a sliver.
fn convex_parts(ring: &[(f64, f64)]) -> Vec<Poly> {
    let n = ring.len();
    let mut p: Poly = Vec::with_capacity(n);
    for i in 0..n {
        let j = (i + 1) % n;
        let (ex, ey) = (ring[j].0 - ring[i].0, ring[j].1 - ring[i].1);
        if ex.abs() > EPS || ey.abs() > EPS {
            p.push(ring[i]);
        }
    }
    let area = area2(&p);
    if p.len() < 3 || area.abs() <= MIN_AREA2 {
        return Vec::new();
    }
    if area < 0.0 {
        p.reverse();
    }
    let m = p.len();
    let convex = (0..m).all(|i| {
        let (a, b, c) = (p[i], p[(i + 1) % m], p[(i + 2) % m]);
        let (ex, ey) = (b.0 - a.0, b.1 - a.1);
        let (fx, fy) = (c.0 - b.0, c.1 - b.1);
        ex * fy - ey * fx >= -EPS
    });
    if convex {
        return vec![p];
    }
    crate::ear_clip(&p)
        .iter()
        .map(|t| t.iter().map(|&k| p[k as usize]).collect())
        .collect()
}

/// _split: the parts of a convex polygon on either side of a cutting line (signed distances d per vertex).
fn split(poly: &[(f64, f64)], d: &[f64]) -> (Poly, Poly) {
    let n = poly.len();
    let (mut left, mut right) = (Vec::new(), Vec::new());
    for i in 0..n {
        let k = (i + n - 1) % n;
        let (a, b, da, db) = (poly[k], poly[i], d[k], d[i]);
        if da.min(db) < -EPS && da.max(db) > EPS {
            let t = da / (da - db);
            let c = (a.0 + (b.0 - a.0) * t, a.1 + (b.1 - a.1) * t);
            left.push(c);
            right.push(c);
        }
        if db >= -EPS {
            left.push(b);
        }
        if db <= EPS {
            right.push(b);
        }
    }
    (left, right)
}

struct Shape {
    lo: (f64, f64),
    hi: (f64, f64),
    planes: Vec<(f64, f64, f64)>, // (nx, ny, offset)
}

fn dists(poly: &[(f64, f64)], pl: &(f64, f64, f64)) -> Vec<f64> {
    poly.iter().map(|q| q.0 * pl.0 + q.1 * pl.1 - pl.2).collect()
}

fn max_of(v: &[f64]) -> f64 {
    v.iter().copied().fold(f64::NEG_INFINITY, f64::max)
}

fn min_of(v: &[f64]) -> f64 {
    v.iter().copied().fold(f64::INFINITY, f64::min)
}

/// _subtract: poly less the convex shape (planes), as convex pieces.
fn subtract(poly: &Poly, planes: &[(f64, f64, f64)]) -> Vec<Poly> {
    let all: Vec<Vec<f64>> = planes.iter().map(|pl| dists(poly, pl)).collect();
    if all.iter().any(|d| max_of(d) <= EPS) {
        return vec![poly.clone()];
    }
    let mut outside = Vec::new();
    let mut inside = poly.clone();
    for (j, pl) in planes.iter().enumerate() {
        if min_of(&all[j]) >= -EPS {
            continue;
        }
        let d = dists(&inside, pl);
        if max_of(&d) <= EPS {
            return vec![poly.clone()]; // (the two shapes only touched or missed each other)
        }
        if min_of(&d) < -EPS {
            let (keep, piece) = split(&inside, &d);
            inside = keep;
            if piece.len() >= 3 && area2(&piece) > MIN_AREA2 {
                outside.push(piece);
            }
        }
    }
    outside
}

/// _intersection: the overlap of poly and the convex shape, or None.
fn intersection(poly: &Poly, planes: &[(f64, f64, f64)]) -> Option<Poly> {
    let all: Vec<Vec<f64>> = planes.iter().map(|pl| dists(poly, pl)).collect();
    if all.iter().any(|d| max_of(d) <= EPS) {
        return None;
    }
    let mut p = poly.clone();
    for (j, pl) in planes.iter().enumerate() {
        if min_of(&all[j]) >= -EPS {
            continue;
        }
        let d = dists(&p, pl);
        if max_of(&d) <= EPS {
            return None;
        }
        if min_of(&d) < -EPS {
            p = split(&p, &d).0;
        }
    }
    if p.len() >= 3 && area2(&p) > MIN_AREA2 {
        Some(p)
    } else {
        None
    }
}

fn ring_of(a: &PyReadonlyArray2<f64>) -> Poly {
    let v = a.as_array();
    (0..v.nrows()).map(|i| (v[[i, 0]], v[[i, 1]])).collect()
}

fn to_py<'py>(py: Python<'py>, polys: Vec<Poly>) -> Vec<Bound<'py, PyArray2<f64>>> {
    polys
        .into_iter()
        .map(|p| {
            let flat: Vec<f64> = p.iter().flat_map(|q| [q.0, q.1]).collect();
            Array2::from_shape_vec((p.len(), 2), flat).unwrap().into_pyarray(py)
        })
        .collect()
}

/// frontage_geometry.Coverage: reserve(ring), overlaps(ring) -> [(k, 2)], claim(ring) -> [(k, 2)] (each claimed
/// piece reserved as it is made), within the bounds (x0, y0, x1, y1) given.
#[pyclass(module = "earthfast")]
pub struct FrontageCoverage {
    lo: (f64, f64),
    hi: (f64, f64),
    cells: HashMap<(i64, i64), Vec<usize>>,
    shapes: Vec<Shape>,
}

impl FrontageCoverage {
    fn keys(&self, lo: (f64, f64), hi: (f64, f64)) -> Vec<(i64, i64)> {
        // (large mapped polygons can run well beyond the region being generated)
        let x0 = (lo.0.max(self.lo.0) / CELL_M).floor() as i64;
        let y0 = (lo.1.max(self.lo.1) / CELL_M).floor() as i64;
        let x1 = (hi.0.min(self.hi.0) / CELL_M).floor() as i64;
        let y1 = (hi.1.min(self.hi.1) / CELL_M).floor() as i64;
        let mut out = Vec::new();
        for x in x0..=x1 {
            for y in y0..=y1 {
                out.push((x, y));
            }
        }
        out
    }

    fn bounds(p: &[(f64, f64)]) -> ((f64, f64), (f64, f64)) {
        let mut lo = (f64::INFINITY, f64::INFINITY);
        let mut hi = (f64::NEG_INFINITY, f64::NEG_INFINITY);
        for q in p {
            lo = (lo.0.min(q.0), lo.1.min(q.1));
            hi = (hi.0.max(q.0), hi.1.max(q.1));
        }
        (lo, hi)
    }

    fn reserve_piece(&mut self, piece: &[(f64, f64)]) {
        let (lo, hi) = Self::bounds(piece);
        let n = piece.len();
        let mut planes = Vec::with_capacity(n);
        for i in 0..n {
            let j = (i + 1) % n;
            let (ex, ey) = (piece[j].0 - piece[i].0, piece[j].1 - piece[i].1);
            let len = (ex * ex + ey * ey).sqrt();
            if len > EPS {
                let (nx, ny) = (-ey / len, ex / len);
                planes.push((nx, ny, piece[i].0 * nx + piece[i].1 * ny));
            }
        }
        let idx = self.shapes.len();
        self.shapes.push(Shape { lo, hi, planes });
        for key in self.keys(lo, hi) {
            self.cells.entry(key).or_default().push(idx);
        }
    }

    fn nearby(&self, piece: &[(f64, f64)]) -> Vec<usize> {
        let (lo, hi) = Self::bounds(piece);
        let mut found = BTreeSet::new();
        for key in self.keys(lo, hi) {
            if let Some(v) = self.cells.get(&key) {
                found.extend(v.iter().copied());
            }
        }
        found
            .into_iter()
            .filter(|&i| {
                let s = &self.shapes[i];
                !(hi.0 <= s.lo.0 + EPS || hi.1 <= s.lo.1 + EPS || lo.0 >= s.hi.0 - EPS || lo.1 >= s.hi.1 - EPS)
            })
            .collect()
    }

    fn overlaps_of(&self, ring: &[(f64, f64)], first: bool) -> Vec<Poly> {
        let mut out = Vec::new();
        for piece in convex_parts(ring) {
            for i in self.nearby(&piece) {
                if let Some(p) = intersection(&piece, &self.shapes[i].planes) {
                    out.push(p);
                    if first {
                        return out;
                    }
                }
            }
        }
        out
    }
}

#[pymethods]
impl FrontageCoverage {
    #[new]
    fn new(x0: f64, y0: f64, x1: f64, y1: f64) -> Self {
        FrontageCoverage { lo: (x0, y0), hi: (x1, y1), cells: HashMap::new(), shapes: Vec::new() }
    }

    fn reserve(&mut self, ring: PyReadonlyArray2<f64>) {
        for piece in convex_parts(&ring_of(&ring)) {
            self.reserve_piece(&piece);
        }
    }

    /// The positive-area intersections of ring with the reserved footprints.
    fn overlaps<'py>(&self, py: Python<'py>, ring: PyReadonlyArray2<f64>) -> Vec<Bound<'py, PyArray2<f64>>> {
        to_py(py, self.overlaps_of(&ring_of(&ring), false))
    }

    /// Whether ring meets a reserved footprint (overlaps' first).
    fn overlaps_any(&self, ring: PyReadonlyArray2<f64>) -> bool {
        !self.overlaps_of(&ring_of(&ring), true).is_empty()
    }

    /// The free parts of ring, each reserved as it is claimed.
    fn claim<'py>(&mut self, py: Python<'py>, ring: PyReadonlyArray2<f64>) -> Vec<Bound<'py, PyArray2<f64>>> {
        let mut out = Vec::new();
        for piece in convex_parts(&ring_of(&ring)) {
            let mut parts = vec![piece.clone()];
            for i in self.nearby(&piece) {
                let planes = &self.shapes[i].planes;
                parts = parts.iter().flat_map(|p| subtract(p, planes)).collect();
                if parts.is_empty() {
                    break;
                }
            }
            for part in parts {
                self.reserve_piece(&part);
                out.push(part);
            }
        }
        to_py(py, out)
    }

    /// The number of reserved convex pieces.
    fn __len__(&self) -> usize {
        self.shapes.len()
    }
}
