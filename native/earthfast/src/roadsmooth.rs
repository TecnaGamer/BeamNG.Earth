//! Rounded road bends (roadsmooth.py, BNE_ROAD_SMOOTH=on): the arcs that replace a polyline's corners.
//!
//! One line at a time, pure (its own coordinates and radius), so two cells that share a road agree. At each interior
//! vertex turning by `min_deflect` .. `max_deflect` an arc tangent to both pieces replaces the corner. Its radius is
//! the class's, or a larger one when that fills the pieces (a gentle bend of short pieces becomes one round curve)
//! and moves the line by less than `max_shift`. The tangent points stay within half of each neighbouring piece (most
//! of it, `free_share`, when the vertex at its other end is not rounded). The ends and the vertices that are not
//! rounded keep their coordinates bit for bit.
//!
//! The tunables come from roadsmooth.py (`PARAMS`), so the numbers live in one place.

use numpy::ndarray::{Array1, Array2};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// The tunables, in the order roadsmooth.PARAMS lists them.
#[derive(Clone, Copy, Debug)]
pub struct Params {
    pub min_deflect: f64, // radians: a vertex turning less is left alone
    pub max_deflect: f64, // radians: ... and one turning more (a hairpin's tip, a doubling back)
    pub arc_step_m: f64,  // m between the points of an arc ...
    pub arc_step: f64,    // ... and at most this many radians
    pub free_share: f64,  // share of a piece a rounded vertex may take beside one that is not rounded
    pub max_shift: f64,   // m a corner may move to fill its pieces with a radius above the class's
    pub min_piece: f64,   // a piece shorter than this is a duplicate vertex: the line is left alone
    pub closed: f64,      // ends this close are one point: a closed line
}

pub const PARAM_COUNT: usize = 8;

impl Params {
    fn from_slice(p: &[f64]) -> Option<Params> {
        if p.len() != PARAM_COUNT {
            return None;
        }
        Some(Params {
            min_deflect: p[0].to_radians(),
            max_deflect: p[1].to_radians(),
            arc_step_m: p[2],
            arc_step: p[3].to_radians(),
            free_share: p[4],
            max_shift: p[5],
            min_piece: p[6],
            closed: p[7],
        })
    }
}

type Pt = [f64; 2];

fn dist(a: Pt, b: Pt) -> f64 {
    (a[0] - b[0]).hypot(a[1] - b[1])
}

/// The points of the arc replacing vertex v where the unit direction u turns to w by delta (signed) with radius r,
/// tangent points t from the vertex: the first and the last are the tangents.
fn arc(v: Pt, u: Pt, w: Pt, delta: f64, r: f64, t: f64, p: &Params) -> Vec<Pt> {
    let a = [v[0] - u[0] * t, v[1] - u[1] * t];
    let sgn = if delta > 0.0 { 1.0 } else { -1.0 };
    let n = [-u[1] * sgn, u[0] * sgn]; // towards the centre (left of the line for a left turn)
    let c = [a[0] + n[0] * r, a[1] + n[1] * r];
    let a0 = (a[1] - c[1]).atan2(a[0] - c[0]);
    let mag = delta.abs();
    let steps = (r * mag / p.arc_step_m).max(mag / p.arc_step).ceil().max(1.0) as usize;
    let mut pts = Vec::with_capacity(steps + 1);
    pts.push(a);
    for k in 1..steps {
        let ang = a0 + sgn * mag * k as f64 / steps as f64;
        pts.push([c[0] + r * ang.cos(), c[1] + r * ang.sin()]);
    }
    pts.push([v[0] + w[0] * t, v[1] + w[1] * t]);
    pts
}

/// (new points, old distances, new distances) of the line `xy` with its bends rounded: the distances along the line
/// at its vertices and tangent points, before and after. None when nothing changes.
pub fn smooth_core(xy: &[Pt], radius: f64, p: &Params) -> Option<(Vec<Pt>, Vec<f64>, Vec<f64>)> {
    let k = xy.len();
    if k < 3 {
        return None;
    }
    let mut len = Vec::with_capacity(k - 1);
    let mut u: Vec<Pt> = Vec::with_capacity(k - 1);
    for i in 0..k - 1 {
        let dx = xy[i + 1][0] - xy[i][0];
        let dy = xy[i + 1][1] - xy[i][1];
        let l = dx.hypot(dy);
        // (a NaN piece is as unusable as a duplicate one)
        if !(l >= p.min_piece) {
            return None;
        }
        len.push(l);
        u.push([dx / l, dy / l]);
    }
    if dist(xy[0], xy[k - 1]) < p.closed {
        return None;
    }
    // the turn at the interior vertices 1 .. k-2 (index j - 1)
    let n = k - 2;
    let mut delta = vec![0.0; n];
    let mut rounded = vec![false; n];
    for j in 0..n {
        let cross = u[j][0] * u[j + 1][1] - u[j][1] * u[j + 1][0];
        let dot = u[j][0] * u[j + 1][0] + u[j][1] * u[j + 1][1];
        delta[j] = cross.atan2(dot);
        let m = delta[j].abs();
        rounded[j] = m >= p.min_deflect && m <= p.max_deflect;
    }
    if !rounded.iter().any(|&r| r) {
        return None;
    }
    // the tangent length at each rounded vertex: a piece between two rounded vertices gives each half, a rounded vertex
    // beside one that is not may take most of it (not all: its arc would end on that vertex, a duplicate point)
    let mut tang = vec![0.0; n];
    for j in 0..n {
        if !rounded[j] {
            continue;
        }
        let w_prev = if j > 0 && rounded[j - 1] { 0.5 } else { p.free_share };
        let w_next = if j + 1 < n && rounded[j + 1] { 0.5 } else { p.free_share };
        let share = (w_prev * len[j]).min(w_next * len[j + 1]);
        let mag = delta[j].abs();
        let half = (mag / 2.0).tan();
        // the class's radius, or a larger one when it fills the pieces and moves the line less than max_shift (a
        // corner's distance to its arc is r * (1 / cos(delta / 2) - 1))
        let r_fill = (share / half).min(p.max_shift / (1.0 / (mag / 2.0).cos() - 1.0));
        let r_fill = if r_fill.is_finite() { r_fill } else { 0.0 };
        tang[j] = (radius.max(r_fill) * half).min(share);
    }
    let mut s_old = Vec::with_capacity(k);
    s_old.push(0.0);
    for l in &len {
        s_old.push(s_old.last().unwrap() + l);
    }
    let mut out: Vec<Pt> = vec![xy[0]];
    let mut old_s = vec![0.0];
    let mut new_s = vec![0.0];
    let mut s_new = 0.0;
    let mut last = xy[0];
    for j in 1..k - 1 {
        let v = xy[j];
        let t = tang[j - 1];
        if !rounded[j - 1] || t <= 1e-6 {
            s_new += dist(v, last);
            out.push(v);
            last = v;
            old_s.push(s_old[j]);
            new_s.push(s_new);
            continue;
        }
        let dj = delta[j - 1];
        let r = t / (dj.abs() / 2.0).tan();
        let mut pts = arc(v, u[j - 1], u[j], dj, r, t, p);
        s_new += dist(pts[0], last);
        old_s.push(s_old[j] - t);
        new_s.push(s_new);
        for w in pts.windows(2) {
            s_new += dist(w[0], w[1]);
        }
        if dist(pts[0], last) < p.min_piece {
            pts.remove(0); // (the arc before took half of the same piece: this one starts where it ended)
        }
        last = *pts.last().unwrap();
        out.extend(pts);
        old_s.push(s_old[j] + t);
        new_s.push(s_new);
    }
    let e = xy[k - 1];
    s_new += dist(e, last);
    out.push(e);
    old_s.push(s_old[k - 1]);
    new_s.push(s_new);
    Some((out, old_s, new_s))
}

/// roadsmooth.smooth_line: (points (m, 2), old distances, new distances) of the line xy (k, 2) with its bends rounded
/// with `radius`, or None when nothing changes. `params`: roadsmooth.PARAMS.
#[pyfunction]
pub fn road_smooth_line<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>,
    radius: f64,
    params: PyReadonlyArray1<'py, f64>,
) -> PyResult<Option<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)>> {
    let a = xy.as_array();
    if a.shape()[1] < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("xy must be (k, 2)"));
    }
    let ps = params.as_slice().map_err(|_| pyo3::exceptions::PyValueError::new_err("params must be contiguous"))?;
    let p = Params::from_slice(ps)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err(format!("params needs {PARAM_COUNT} values")))?;
    let pts: Vec<Pt> = (0..a.shape()[0]).map(|i| [a[[i, 0]], a[[i, 1]]]).collect();
    Ok(smooth_core(&pts, radius, &p).map(|(out, old_s, new_s)| {
        let m = out.len();
        let flat: Vec<f64> = out.into_iter().flatten().collect();
        (
            Array2::from_shape_vec((m, 2), flat).expect("points").into_pyarray(py),
            Array1::from_vec(old_s).into_pyarray(py),
            Array1::from_vec(new_s).into_pyarray(py),
        )
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn params() -> Params {
        Params::from_slice(&[0.5, 150.0, 3.0, 6.0, 0.9, 0.75, 0.05, 0.01]).unwrap()
    }

    /// The turn at each interior vertex, in degrees.
    fn turns(pts: &[Pt]) -> Vec<f64> {
        (1..pts.len() - 1)
            .map(|i| {
                let a = [pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]];
                let b = [pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]];
                (a[0] * b[1] - a[1] * b[0]).atan2(a[0] * b[0] + a[1] * b[1]).abs().to_degrees()
            })
            .collect()
    }

    #[test]
    fn a_right_angle_becomes_an_arc_of_the_radius() {
        let line = [[0.0, 0.0], [100.0, 0.0], [100.0, 100.0]];
        let (pts, old_s, new_s) = smooth_core(&line, 25.0, &params()).unwrap();
        assert_eq!(pts[0], line[0]);
        assert_eq!(*pts.last().unwrap(), line[2]);
        assert!(turns(&pts).iter().all(|&t| t <= 6.0 + 1e-6));
        // tangent points 25 m from the corner; the arc's quarter circle is 25 * pi / 2 long
        assert!((pts[1][0] - 75.0).abs() < 1e-9 && pts[1][1].abs() < 1e-9);
        assert!((old_s[1] - 75.0).abs() < 1e-9 && (old_s[2] - 125.0).abs() < 1e-9);
        assert!((new_s[2] - new_s[1] - 25.0 * std::f64::consts::FRAC_PI_2).abs() < 0.1);
        assert!((old_s[3] - 200.0).abs() < 1e-9 && new_s[3] < 200.0);
    }

    #[test]
    fn nothing_to_round_is_none() {
        let p = params();
        assert!(smooth_core(&[[0.0, 0.0], [10.0, 0.0]], 20.0, &p).is_none());
        assert!(smooth_core(&[[0.0, 0.0], [10.0, 0.0], [20.0, 0.0]], 20.0, &p).is_none());
        // closed: a roundabout / a ring
        assert!(smooth_core(&[[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 0.0]], 20.0, &p).is_none());
        // a duplicate vertex
        assert!(smooth_core(&[[0.0, 0.0], [10.0, 0.0], [10.0, 0.0], [10.0, 9.0]], 20.0, &p).is_none());
        // a hairpin's tip
        assert!(smooth_core(&[[0.0, 0.0], [50.0, 0.0], [0.0, 5.0]], 20.0, &p).is_none());
        // NaN
        assert!(smooth_core(&[[0.0, 0.0], [f64::NAN, 0.0], [10.0, 10.0]], 20.0, &p).is_none());
    }

    #[test]
    fn a_gentle_bend_of_short_pieces_is_one_curve() {
        // 16 m pieces turning 6 degrees each: the class radius (25 m) would leave straight stretches between the arcs
        let mut line = vec![[0.0, 0.0]];
        let mut ang = 0.0f64;
        for _ in 0..10 {
            let l = *line.last().unwrap();
            line.push([l[0] + 16.0 * ang.cos(), l[1] + 16.0 * ang.sin()]);
            ang += 6.0f64.to_radians();
        }
        let (pts, _, _) = smooth_core(&line, 25.0, &params()).unwrap();
        let t = turns(&pts);
        assert!(t.iter().cloned().fold(0.0, f64::max) <= 6.0 + 1e-6);
        // no vertex of the original is further than max_shift from the curve
        for v in &line[1..line.len() - 1] {
            let d = pts.iter().map(|q| dist(*q, *v)).fold(f64::MAX, f64::min);
            assert!(d <= 0.75 + 0.3, "{d}");
        }
    }

    #[test]
    fn unrounded_vertices_and_ends_keep_their_bits() {
        let line = [[0.0, 0.0], [50.0, 0.0], [100.0, 0.0], [100.0, 60.0], [160.0, 60.0]];
        let (pts, _, _) = smooth_core(&line, 20.0, &params()).unwrap();
        assert!(pts.contains(&line[1]));
        assert_eq!(pts[0], line[0]);
        assert_eq!(*pts.last().unwrap(), line[4]);
    }

    #[test]
    fn the_params_must_be_eight() {
        assert!(Params::from_slice(&[1.0, 2.0]).is_none());
    }
}
