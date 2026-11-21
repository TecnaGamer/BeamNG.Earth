//! Station layout and geometry. Coordinates are frame metres. Keep the operation order of
//! the server's float64 array expressions (including division before multiplication).
mod features;
mod layout;
mod mesh;

use crate::{dm::Dm, dmmatmul::dot1, roadnet::pyfloat::py_round};
use numpy::{ndarray::Array2, Element, IntoPyArray, PyArray1, PyArray2, PyReadonlyArray2};
use pyo3::{exceptions::PyValueError, prelude::*};
use std::ops::{Add, Div, Mul, Neg, Sub};

#[derive(Clone, Copy, Debug, Default, PartialEq)]
struct Point(f64, f64);

impl Add for Point {
    type Output = Self;
    fn add(self, b: Self) -> Self {
        Self(self.0 + b.0, self.1 + b.1)
    }
}
impl Sub for Point {
    type Output = Self;
    fn sub(self, b: Self) -> Self {
        Self(self.0 - b.0, self.1 - b.1)
    }
}
impl Mul<f64> for Point {
    type Output = Self;
    fn mul(self, s: f64) -> Self {
        Self(self.0 * s, self.1 * s)
    }
}
impl Div<f64> for Point {
    type Output = Self;
    fn div(self, s: f64) -> Self {
        Self(self.0 / s, self.1 / s)
    }
}
impl Neg for Point {
    type Output = Self;
    fn neg(self) -> Self {
        Self(-self.0, -self.1)
    }
}
impl Point {
    fn norm(self) -> f64 {
        self.0.dhypot(self.1)
    }
    fn dot(self, b: Self) -> f64 {
        (self.0 * b.0) + (self.1 * b.1)
    }
    fn left(self) -> Self {
        Self(-self.1, self.0)
    }
    fn arr(self, py: Python<'_>) -> Bound<'_, PyArray1<f64>> {
        vec![self.0, self.1].into_pyarray(py)
    }
    fn pair(self) -> (f64, f64) {
        (self.0, self.1)
    }
}
type Ring = Vec<Point>;
type Array<'py> = Bound<'py, PyArray2<f64>>;
type XY<'py> = PyReadonlyArray2<'py, f64>;
fn points(a: XY<'_>) -> PyResult<Ring> {
    let a = a.as_array();
    if a.ncols() < 2 {
        return Err(PyValueError::new_err("expected x and y columns"));
    }
    Ok(a.rows().into_iter().map(|r| Point(r[0], r[1])).collect())
}
fn rows<T: Element, const N: usize>(py: Python<'_>, data: Vec<[T; N]>) -> Bound<'_, PyArray2<T>> {
    Array2::from_shape_vec((data.len(), N), data.into_flattened())
        .unwrap()
        .into_pyarray(py)
}
fn array<'p>(py: Python<'p>, pts: &[Point]) -> Array<'p> {
    rows(py, pts.iter().map(|p| [p.0, p.1]).collect())
}
fn open(r: &[Point]) -> &[Point] {
    if r.len() >= 2 {
        let d = r[0] - r[r.len() - 1];
        if d.0.abs() < 1e-6 && d.1.abs() < 1e-6 {
            return &r[..r.len() - 1];
        }
    }
    r
}
fn edges(r: &[Point]) -> impl Iterator<Item = (Point, Point)> + '_ {
    r.iter().copied().zip(r.iter().copied().cycle().skip(1))
}
fn signed(r: &[Point]) -> f64 {
    let x: Vec<_> = r.iter().map(|p| p.0).collect();
    let y: Vec<_> = r.iter().map(|p| p.1).collect();
    let mut nx = x.clone();
    let mut ny = y.clone();
    if !r.is_empty() {
        nx.rotate_left(1);
        ny.rotate_left(1);
    }
    (dot1(&x, &ny) - dot1(&nx, &y)) * 0.5
}
fn area(r: &[Point]) -> f64 {
    let r = open(r);
    if r.len() < 3 {
        0.0
    } else {
        signed(r).abs()
    }
}
fn ccw(r: &[Point]) -> Ring {
    let mut r = open(r).to_vec();
    if !(signed(&r) >= 0.0) {
        r.reverse();
    }
    r
}
fn centroid(r: &[Point]) -> Point {
    let r = open(r);
    if let Some((x, y)) = crate::stationgeom::area_centroid(r.len(), |i| r[i].pair()) {
        return Point(x, y);
    }
    r.iter().copied().fold(Point::default(), |a, b| a + b) / r.len() as f64
}
fn inside(r: &[Point], p: Point) -> bool {
    edges(open(r))
        .filter(|&(a, b)| (b.1 > p.1) != (a.1 > p.1) && p.0 < (a.0 - b.0) * (p.1 - b.1) / (a.1 - b.1 + 1e-12) + b.0)
        .count()
        % 2
        == 1
}
fn foot(a: Point, b: Point, p: Point) -> (f64, Point) {
    let (d, x, y) = crate::stationgeom::seg_foot(a.0, a.1, b.0 - a.0, b.1 - a.1, p.0, p.1);
    (d, Point(x, y))
}
fn distance(r: &[Point], p: Point, closed: bool) -> f64 {
    let r = if closed { open(r) } else { r };
    if r.len() == 1 {
        return (r[0] - p).norm();
    }
    let mut best = f64::INFINITY;
    for (a, b) in edges(r).take(if closed { r.len() } else { r.len().saturating_sub(1) }) {
        let d = foot(a, b, p).0;
        if d.is_nan() {
            return d;
        }
        best = best.min(d);
    }
    if r.is_empty() {
        f64::NAN
    } else {
        best
    }
}
fn hull(r: &[Point]) -> Ring {
    let mut pts: Vec<_> = r.iter().map(|p| Point(py_round(p.0, 3), py_round(p.1, 3))).collect();
    pts.sort_by(|a, b| {
        a.0.partial_cmp(&b.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal))
    });
    pts.dedup();
    if pts.len() < 3 {
        return pts;
    }
    let chain = |it: &mut dyn Iterator<Item = Point>| {
        let mut out: Ring = Vec::new();
        for p in it {
            while out.len() >= 2 {
                let o = out[out.len() - 2];
                let a = out[out.len() - 1] - o;
                let b = p - o;
                if !(a.0 * b.1 - a.1 * b.0 <= 0.0) {
                    break;
                }
                out.pop();
            }
            out.push(p);
        }
        out.pop();
        out
    };
    let mut out = chain(&mut pts.iter().copied());
    out.extend(chain(&mut pts.iter().rev().copied()));
    out
}
fn pad_ring(r: &[Point], pad: f64) -> Ring {
    let r = open(r);
    let n = r.len();
    if n < 3 {
        return r.to_vec();
    }
    (0..n)
        .map(|i| {
            let p = r[i];
            let a = p - r[(i + n - 1) % n];
            let b = r[(i + 1) % n] - p;
            let n1 = Point(a.1, -a.0) / a.norm().max(1e-9);
            let n2 = Point(b.1, -b.0) / b.norm().max(1e-9);
            let v = n1 + n2;
            let norm = v.norm();
            if norm > 1e-9 {
                p + v / norm * pad / (norm / 2.0).max(0.35)
            } else {
                p + n1 * pad
            }
        })
        .collect()
}
#[pyfunction]
fn bus_open_len(xy: XY<'_>) -> usize {
    let a = xy.as_array();
    let n = a.nrows();
    if n >= 2 && a.ncols() > 0 && (0..a.ncols()).all(|j| (a[[0, j]] - a[[n - 1, j]]).abs() < 1e-6) {
        n - 1
    } else {
        n
    }
}
#[pyfunction]
fn bus_signed(xy: XY<'_>) -> PyResult<f64> {
    Ok(signed(&points(xy)?))
}
#[pyfunction]
fn bus_hull<'p>(py: Python<'p>, xy: XY<'p>) -> PyResult<Array<'p>> {
    Ok(array(py, &hull(&points(xy)?)))
}
#[pyfunction]
fn bus_pad<'p>(py: Python<'p>, xy: XY<'p>, pad: f64) -> PyResult<Array<'p>> {
    Ok(array(py, &pad_ring(&points(xy)?, pad)))
}
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(bus_open_len, m)?)?;
    m.add_function(wrap_pyfunction!(bus_signed, m)?)?;
    m.add_function(wrap_pyfunction!(bus_hull, m)?)?;
    m.add_function(wrap_pyfunction!(bus_pad, m)?)?;
    layout::register(m)?;
    mesh::register(m)?;
    features::register(m)
}
