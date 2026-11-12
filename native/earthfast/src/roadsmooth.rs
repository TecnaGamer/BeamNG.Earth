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

// ---- curvature-continuous lines (roadsmooth.py G2_*, kernel 230) --------------------------------------------------
//
// The arcs above are tangent to the pieces (G1): the curvature steps from none to the arc's at each tangent point and
// from one arc's to the next where a mapped curve's vertices turn unevenly (a car's lateral pull jumps there; a banked
// oval's chords made a series of bumps, the user 2026-11-12). The second pass spreads each vertex's turn of the arcs'
// line over a triangle of half width w along it (the sum of triangles is a curvature that is continuous and linear
// between the points: transition spirals, the clothoids of road design), integrates the headings back to points, and
// moves each stretch between two anchors (the line's ends, the vertices where another road joins: its connectors)
// by the linear share of the anchors' misses, so the anchors stay where the map has them bit for bit and a straight
// between them stays straight. w is the class's (window_m), at most what moves the line `shift_m` at a curve's end
// (a spiral of length 2w shifts an arc of curvature k by (2w)^2 k / 24) or at a corner (a turn d spread over w cuts the
// corner by w d / 6). Where the result strays more than max_dev_m from the arcs' line the windows are halved (twice),
// then the arcs' line stands.

/// The G2 pass's tunables, in roadsmooth.G2_PARAMS's order.
#[derive(Clone, Copy, Debug)]
pub struct G2 {
    pub shift_m: f64,   // m: the most the spreading moves the line at a curve's end or a corner
    pub cell_m: f64,    // m: the integration step
    pub out_m: f64,     // m: the longest piece of the result on a curve ...
    pub out_turn: f64,  // radians: ... and the most it turns between two of its points
    pub max_dev_m: f64, // m: the result stays this close to the arcs' line, else smaller windows
    pub out_tol: f64,   // m: a kept piece passes this close to the curve's nodes it skips
}

pub const G2_COUNT: usize = 6;

impl G2 {
    pub fn from_slice(p: &[f64]) -> Option<G2> {
        if p.len() != G2_COUNT {
            return None;
        }
        Some(G2 { shift_m: p[0], cell_m: p[1], out_m: p[2], out_turn: p[3].to_radians(), max_dev_m: p[4], out_tol: p[5] })
    }
}

/// The arcs (smooth_core) of each stretch of xy between the pins (interior vertex indices kept where they are) and the
/// ends: (points, old distances, new distances, the points' indices of the ends and pins), closed lines included.
/// None when no stretch changes.
fn g1_pinned(xy: &[Pt], radius: f64, pins: &[usize], p: &Params) -> Option<(Vec<Pt>, Vec<f64>, Vec<f64>, Vec<usize>)> {
    let k = xy.len();
    let mut cuts = vec![0usize];
    for &i in pins {
        if i > *cuts.last().unwrap() && i < k - 1 {
            cuts.push(i);
        }
    }
    cuts.push(k - 1);
    let mut p1 = *p;
    p1.closed = -1.0; // (a ring's ends are its anchors: it is rounded all round but there)
    let (mut pts, mut old_s, mut new_s, mut anchors) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    let (mut o_off, mut n_off, mut any) = (0.0, 0.0, false);
    for w in cuts.windows(2) {
        let seg = &xy[w[0]..=w[1]];
        let got = if seg.len() >= 3 { smooth_core(seg, radius, &p1) } else { None };
        let (sp, so, sn) = match got {
            Some(g) => {
                any = true;
                g
            }
            None => {
                let mut d = vec![0.0];
                for q in seg.windows(2) {
                    d.push(d.last().unwrap() + dist(q[0], q[1]));
                }
                (seg.to_vec(), d.clone(), d)
            }
        };
        let first = if pts.is_empty() { 0 } else { 1 };
        if first == 0 {
            anchors.push(0);
        }
        pts.extend_from_slice(&sp[first..]);
        old_s.extend(so[first..].iter().map(|v| v + o_off));
        new_s.extend(sn[first..].iter().map(|v| v + n_off));
        o_off += *so.last().unwrap();
        n_off += *sn.last().unwrap();
        anchors.push(pts.len() - 1);
    }
    if any {
        Some((pts, old_s, new_s, anchors))
    } else {
        None
    }
}

/// The distance of point v from the segment a b.
fn seg_dist(v: Pt, a: Pt, b: Pt) -> f64 {
    let ab = [b[0] - a[0], b[1] - a[1]];
    let l2 = ab[0] * ab[0] + ab[1] * ab[1];
    let t = if l2 > 0.0 { (((v[0] - a[0]) * ab[0] + (v[1] - a[1]) * ab[1]) / l2).clamp(0.0, 1.0) } else { 0.0 };
    dist([a[0] + ab[0] * t, a[1] + ab[1] * t], v)
}

/// The second derivatives at the knots xs (increasing) of the clamped cubic spline through ys with no slope at either
/// end (the line's end headings stay the map's: the roads meeting there keep their tangent).
fn clamped_spline(xs: &[f64], ys: &[f64]) -> Vec<f64> {
    let n = xs.len();
    let mut m = vec![0.0; n];
    if n < 2 {
        return m;
    }
    let h: Vec<f64> = (0..n - 1).map(|i| (xs[i + 1] - xs[i]).max(1e-9)).collect();
    // rows (a, b, c, d): a m[i-1] + b m[i] + c m[i+1] = d (Thomas)
    let row = |i: usize| -> (f64, f64, f64, f64) {
        if i == 0 {
            (0.0, h[0] / 3.0, h[0] / 6.0, (ys[1] - ys[0]) / h[0])
        } else if i == n - 1 {
            (h[n - 2] / 6.0, h[n - 2] / 3.0, 0.0, -(ys[n - 1] - ys[n - 2]) / h[n - 2])
        } else {
            (h[i - 1] / 6.0, (h[i - 1] + h[i]) / 3.0, h[i] / 6.0, (ys[i + 1] - ys[i]) / h[i] - (ys[i] - ys[i - 1]) / h[i - 1])
        }
    };
    let mut cp = vec![0.0; n];
    let mut dp = vec![0.0; n];
    for i in 0..n {
        let (a, b, c, d) = row(i);
        let (cpp, dpp) = if i > 0 { (cp[i - 1], dp[i - 1]) } else { (0.0, 0.0) };
        let den = b - a * cpp;
        cp[i] = c / den;
        dp[i] = (d - a * dpp) / den;
    }
    m[n - 1] = dp[n - 1];
    for i in (0..n - 1).rev() {
        m[i] = dp[i] - cp[i] * m[i + 1];
    }
    m
}

/// The G2 pass over the line pts (the arcs' line) with the anchors (indices into pts, the first and the last among
/// them), each vertex's spread at most window_m: (points, the new distance along them of each of pts) or None where
/// it strays more than g.max_dev_m or the line is degenerate.
fn g2_pass(pts: &[Pt], anchors: &[usize], window_m: f64, g: &G2) -> Option<(Vec<Pt>, Vec<f64>)> {
    let m = pts.len();
    if m < 3 || window_m <= 0.0 {
        return None;
    }
    let mut l = Vec::with_capacity(m - 1);
    for w in pts.windows(2) {
        let d = dist(w[0], w[1]);
        if !(d > 1e-6) {
            return None;
        }
        l.push(d);
    }
    let mut s = vec![0.0; m];
    for i in 1..m {
        s[i] = s[i - 1] + l[i - 1];
    }
    // the nodes: every point of pts (node[i]) and the cells between, at most cell_m long
    let mut sig: Vec<f64> = Vec::new();
    let mut node = vec![0usize; m];
    for i in 0..m - 1 {
        node[i] = sig.len();
        let n = (l[i] / g.cell_m).ceil().max(1.0) as usize;
        for c in 0..n {
            sig.push(s[i] + l[i] * c as f64 / n as f64);
        }
    }
    node[m - 1] = sig.len();
    sig.push(s[m - 1]);
    let nn = sig.len();
    let last = nn - 1;
    // the turns of pts' vertices, spread
    let mut tau = vec![0.0f64; nn];
    let mut wts: Vec<f64> = Vec::new();
    let mut is_anchor = vec![false; m];
    for &a in anchors {
        is_anchor[a] = true;
    }
    for i in 1..m - 1 {
        let a = [pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]];
        let b = [pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]];
        let d = (a[0] * b[1] - a[1] * b[0]).atan2(a[0] * b[0] + a[1] * b[1]);
        if d.abs() < 1e-12 {
            continue;
        }
        let kap = d.abs() / (0.5 * (l[i - 1] + l[i]));
        let w = window_m.min((6.0 * g.shift_m / kap).sqrt()).min(6.0 * g.shift_m / d.abs());
        let k0 = node[i];
        if w < g.cell_m {
            tau[k0] += d;
            continue;
        }
        let lo = sig.partition_point(|&x| x <= s[i] - w).max(1);
        let hi = sig.partition_point(|&x| x < s[i] + w).min(last);
        wts.clear();
        let mut sum = 0.0;
        for k in lo..hi {
            let v = (1.0 - (sig[k] - s[i]).abs() / w).max(0.0);
            wts.push(v);
            sum += v;
        }
        if !(sum > 1e-12) {
            tau[k0] += d;
            continue;
        }
        for (j, k) in (lo..hi).enumerate() {
            tau[k] += d * wts[j] / sum;
        }
    }
    // headings integrated back to points
    let h0 = (pts[1][1] - pts[0][1]).atan2(pts[1][0] - pts[0][0]);
    let mut q = vec![pts[0]; nn];
    let mut th = h0;
    for k in 0..last {
        th += tau[k];
        let ds = sig[k + 1] - sig[k];
        q[k + 1] = [q[k][0] + ds * th.cos(), q[k][1] + ds * th.sin()];
    }
    // the line moved by its anchors' misses: a cubic spline of them along it, curvature continuous through the anchors
    // and with no slope at the line's ends (their headings stay; the misses are millimetres to decimetres)
    let ka: Vec<usize> = anchors.iter().map(|&a| node[a]).collect();
    let xs: Vec<f64> = ka.iter().map(|&k| sig[k]).collect();
    for c in 0..2 {
        let ys: Vec<f64> = anchors.iter().zip(ka.iter()).map(|(&a, &k)| pts[a][c] - q[k][c]).collect();
        let mm = clamped_spline(&xs, &ys);
        let mut j = 0usize;
        for k in 0..nn {
            while j + 2 < xs.len() && sig[k] > xs[j + 1] {
                j += 1;
            }
            let h = xs[j + 1] - xs[j];
            if !(h > 0.0) {
                continue;
            }
            let (a, b) = ((xs[j + 1] - sig[k]) / h, (sig[k] - xs[j]) / h);
            q[k][c] += a * ys[j] + b * ys[j + 1] + ((a * a * a - a) * mm[j] + (b * b * b - b) * mm[j + 1]) * h * h / 6.0;
        }
    }
    for (&a, &k) in anchors.iter().zip(ka.iter()) {
        q[k] = pts[a];
    }
    // how far from the arcs' line (at the same distance along)
    let mut seg = 0usize;
    for k in 0..nn {
        while seg + 1 < m - 1 && s[seg + 1] <= sig[k] {
            seg += 1;
        }
        let t = ((sig[k] - s[seg]) / l[seg]).clamp(0.0, 1.0);
        let r = [pts[seg][0] + (pts[seg + 1][0] - pts[seg][0]) * t, pts[seg][1] + (pts[seg + 1][1] - pts[seg][1]) * t];
        if !(dist(q[k], r) <= g.max_dev_m) {
            return None;
        }
    }
    // the points kept: the ends, the anchors, and on the curves (where the nodes turn) as few as keep within out_tol of
    // the nodes, out_m and out_turn apart at most, and the first and last node of each curved run (a straight runs
    // exactly to where its curve begins)
    let mut keep = vec![false; nn];
    keep[0] = true;
    keep[last] = true;
    for (i, &k) in node.iter().enumerate() {
        if is_anchor[i] {
            keep[k] = true;
        }
    }
    let nz = |k: usize| tau[k].abs() > 1e-12;
    let mut lk = 0usize;
    let mut turn = 0.0f64;
    for k in 1..last {
        if !keep[k] && nz(k) {
            // (would the chord from the last point kept to the next node still do: short enough, turning little
            // enough, every node it skips within out_tol of it)
            let stop = !nz(k - 1)
                || !nz(k + 1)
                || sig[k + 1] - sig[lk] > g.out_m + 1e-9
                || (turn + tau[k] + tau[k + 1]).abs() > g.out_turn
                || (lk + 1..=k).any(|j| seg_dist(q[j], q[lk], q[k + 1]) > g.out_tol);
            if stop {
                keep[k] = true;
            }
        }
        if keep[k] {
            lk = k;
            turn = 0.0;
        } else {
            turn += tau[k];
        }
    }
    // the distances along the result of each of pts (along the nodes, scaled to the kept points' length)
    let mut sn = vec![0.0f64; nn];
    for k in 1..nn {
        sn[k] = sn[k - 1] + dist(q[k - 1], q[k]);
    }
    let mut out: Vec<Pt> = Vec::new();
    let mut total = 0.0;
    for k in 0..nn {
        if keep[k] {
            if let Some(&pv) = out.last() {
                total += dist(pv, q[k]);
            }
            out.push(q[k]);
        }
    }
    let scale = if sn[last] > 0.0 { total / sn[last] } else { 1.0 };
    let at: Vec<f64> = node.iter().map(|&k| sn[k] * scale).collect();
    Some((out, at))
}

/// The line xy (k, 2) with its bends rounded and then curvature continuous (the notes above): (points, old distances,
/// new distances at the knots), or None when nothing changes. pins: interior vertices that stay (sorted).
pub fn smooth_g2(xy: &[Pt], radius: f64, pins: &[usize], window_m: f64, p: &Params, g: &G2)
    -> Option<(Vec<Pt>, Vec<f64>, Vec<f64>)> {
    if xy.len() < 3 {
        return None;
    }
    let (pts, old_s, new_s, anchors) = g1_pinned(xy, radius, pins, p)?;
    let mut w = window_m;
    for _ in 0..3 {
        if let Some((out, at)) = g2_pass(&pts, &anchors, w, g) {
            // the knots' distances along the arcs' line (new_s) carried to the result's (at: per point of pts)
            let mut s1 = vec![0.0; pts.len()];
            for i in 1..pts.len() {
                s1[i] = s1[i - 1] + dist(pts[i - 1], pts[i]);
            }
            let mut j = 0usize;
            let ns: Vec<f64> = new_s
                .iter()
                .map(|&v| {
                    while j + 2 < s1.len() && s1[j + 1] < v {
                        j += 1;
                    }
                    let span = s1[j + 1] - s1[j];
                    let t = if span > 0.0 { ((v - s1[j]) / span).clamp(0.0, 1.0) } else { 0.0 };
                    at[j] + (at[j + 1] - at[j]) * t
                })
                .collect();
            return Some((out, old_s, ns));
        }
        w *= 0.5;
    }
    Some((pts, old_s, new_s))
}

type G2Out<'py> = (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>,
                   Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>);

/// roadsmooth.smooth_lines_g2: a layer's lines at once. xy (N, 2) the lines' points, off (n + 1) into it; radius (n)
/// each line's arc radius (NaN: left as it is), window (n) its spiral half length and tol (n) the tolerance of the points
/// kept (NaN: g2's); pin_off (n + 1) into pins, each
/// line's interior vertices kept where they are (indices into its own points); params: roadsmooth.PARAMS, g2:
/// roadsmooth.G2_PARAMS. Returns (points (M, 2), their offsets (n + 1), knot offsets (n + 1), the knots' old and new
/// distances along the line): a line that does not change keeps its points and has no knots.
#[pyfunction]
pub fn road_smooth_g2<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>,
    off: PyReadonlyArray1<'py, i64>,
    radius: PyReadonlyArray1<'py, f64>,
    window: PyReadonlyArray1<'py, f64>,
    tol: PyReadonlyArray1<'py, f64>,
    pin_off: PyReadonlyArray1<'py, i64>,
    pins: PyReadonlyArray1<'py, i64>,
    params: PyReadonlyArray1<'py, f64>,
    g2: PyReadonlyArray1<'py, f64>,
) -> PyResult<G2Out<'py>> {
    let (a, off, rad, win, poff, pins) =
        (xy.as_array(), off.as_array(), radius.as_array(), window.as_array(), pin_off.as_array(), pins.as_array());
    let tol = tol.as_array();
    let bad = |m: &str| pyo3::exceptions::PyValueError::new_err(m.to_string());
    if a.ndim() != 2 || a.shape()[1] < 2 {
        return Err(bad("xy must be (N, 2)"));
    }
    let n = off.len().saturating_sub(1);
    if rad.len() != n || win.len() != n || tol.len() != n || poff.len() != n + 1 {
        return Err(bad("radius / window / tol / pin_off must match off"));
    }
    let p = Params::from_slice(params.as_slice().map_err(|_| bad("params must be contiguous"))?)
        .ok_or_else(|| bad("params needs 8 values"))?;
    let g = G2::from_slice(g2.as_slice().map_err(|_| bad("g2 must be contiguous"))?)
        .ok_or_else(|| bad("g2 needs 6 values"))?;
    let rows = a.shape()[0] as i64;
    for i in 0..n {
        if off[i] < 0 || off[i + 1] < off[i] || off[i + 1] > rows || poff[i + 1] < poff[i] || poff[i + 1] as usize > pins.len() {
            return Err(bad("bad offsets"));
        }
    }
    let lines: Vec<(Vec<Pt>, f64, f64, f64, Vec<usize>)> = (0..n)
        .map(|i| {
            let (s, e) = (off[i] as usize, off[i + 1] as usize);
            let pts: Vec<Pt> = (s..e).map(|k| [a[[k, 0]], a[[k, 1]]]).collect();
            let mut pv: Vec<usize> = (poff[i] as usize..poff[i + 1] as usize)
                .map(|k| pins[k])
                .filter(|&v| v > 0 && (v as usize) + 1 < pts.len())
                .map(|v| v as usize)
                .collect();
            pv.sort_unstable();
            pv.dedup();
            (pts, rad[i], win[i], tol[i], pv)
        })
        .collect();
    let (flat, out_off, knot_off, old_s, new_s) = py.detach(move || {
        let mut flat: Vec<f64> = Vec::new();
        let (mut out_off, mut knot_off) = (vec![0i64], vec![0i64]);
        let (mut old_s, mut new_s) = (Vec::new(), Vec::new());
        for (pts, r, w, t, pv) in &lines {
            let gl = if t.is_finite() { G2 { out_tol: *t, ..g } } else { g };
            let got = if r.is_finite() { smooth_g2(pts, *r, pv, *w, &p, &gl) } else { None };
            match got {
                Some((out, o, ns)) => {
                    flat.extend(out.iter().flatten());
                    old_s.extend(o);
                    new_s.extend(ns);
                }
                None => flat.extend(pts.iter().flatten()),
            }
            out_off.push((flat.len() / 2) as i64);
            knot_off.push(old_s.len() as i64);
        }
        (flat, out_off, knot_off, old_s, new_s)
    });
    let m = flat.len() / 2;
    Ok((
        Array2::from_shape_vec((m, 2), flat).expect("points").into_pyarray(py),
        Array1::from_vec(out_off).into_pyarray(py),
        Array1::from_vec(knot_off).into_pyarray(py),
        Array1::from_vec(old_s).into_pyarray(py),
        Array1::from_vec(new_s).into_pyarray(py),
    ))
}

// ---- a whole layer at once (roadsmooth.smooth, kernel 235) ---------------------------------------------------------
//
// roadsmooth.smooth's Python round the line kernels (its interior connectors' pins, the layer's coordinates rebuilt
// with the new lines, the rules' fractions carried over) was ~2.3 of the 2.8 CPU-s a 16-tile road probe spent
// smoothing (I-83 Baltimore, 2026-11-12): the same steps here, with the same arithmetic as the numpy they replace.

/// The cumulative lengths of a line (numpy's r_[0, cumsum(hypot(*diff(xy).T))]: summed in order).
fn cum_len(xy: &[Pt]) -> Vec<f64> {
    let mut s = Vec::with_capacity(xy.len());
    s.push(0.0f64);
    for k in 1..xy.len() {
        let d = (xy[k][0] - xy[k - 1][0]).hypot(xy[k][1] - xy[k - 1][1]);
        s.push(s[k - 1] + d);
    }
    s
}

/// roadsmooth.connector_pins: the interior vertex indices of line xy where its interior connectors are (at: their
/// fractions of its length): the vertex within pin_m of one, else both vertices of the piece it lies on; sorted, unique.
pub fn connector_pins(xy: &[Pt], at: &[f64], pin_m: f64) -> Vec<usize> {
    let k = xy.len();
    if at.is_empty() || k < 3 {
        return Vec::new();
    }
    let s = cum_len(xy);
    let total = s[k - 1];
    let mut pins: Vec<usize> = Vec::new();
    for &f in at {
        let t = f * total;
        // (numpy's argmin: the first of the smallest; a NaN wins at once)
        let mut v = 0usize;
        let mut best = (s[0] - t).abs();
        if !best.is_nan() {
            for (j, &sj) in s.iter().enumerate().skip(1) {
                let d = (sj - t).abs();
                if d.is_nan() {
                    v = j;
                    break;
                }
                if d < best {
                    best = d;
                    v = j;
                }
            }
        }
        if (s[v] - t).abs() <= pin_m {
            pins.push(v);
        } else {
            // np.clip(np.searchsorted(s, t) - 1, 0, k - 2): the first index with s[i] >= t (left), less one
            let left = s.partition_point(|&x| x < t) as i64;
            let piece = (left - 1).clamp(0, k as i64 - 2) as usize;
            pins.push(piece);
            pins.push(piece + 1);
        }
    }
    pins.retain(|&v| v > 0 && v + 1 < k);
    pins.sort_unstable();
    pins.dedup();
    pins
}

/// roadsmooth.smooth's pins of its G2 lines: xy (N, 2) the lines' points, off (n + 1) into it; at (m,) the interior
/// connectors' fractions, at_off (n + 1) into it, per line. Returns (pin_off (n + 1), pins): each line's interior
/// vertex indices to keep.
#[pyfunction]
pub fn road_connector_pins<'py>(
    py: Python<'py>,
    xy: PyReadonlyArray2<'py, f64>,
    off: PyReadonlyArray1<'py, i64>,
    at_off: PyReadonlyArray1<'py, i64>,
    at: PyReadonlyArray1<'py, f64>,
    pin_m: f64,
) -> PyResult<(Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>)> {
    let (a, off, aoff, at) = (xy.as_array(), off.as_array(), at_off.as_array(), at.as_array());
    let bad = |m: &str| pyo3::exceptions::PyValueError::new_err(m.to_string());
    if a.ndim() != 2 || a.shape()[1] < 2 {
        return Err(bad("xy must be (N, 2)"));
    }
    let n = off.len().saturating_sub(1);
    if aoff.len() != n + 1 {
        return Err(bad("at_off must match off"));
    }
    let rows = a.shape()[0] as i64;
    for i in 0..n {
        if off[i] < 0 || off[i + 1] < off[i] || off[i + 1] > rows || aoff[i] < 0 || aoff[i + 1] < aoff[i]
            || aoff[i + 1] as usize > at.len() {
            return Err(bad("bad offsets"));
        }
    }
    let mut pin_off = vec![0i64];
    let mut pins: Vec<i64> = Vec::new();
    for i in 0..n {
        let (a0, a1) = (aoff[i] as usize, aoff[i + 1] as usize);
        if a1 > a0 {
            let (s, e) = (off[i] as usize, off[i + 1] as usize);
            let pts: Vec<Pt> = (s..e).map(|k| [a[[k, 0]], a[[k, 1]]]).collect();
            let fr: Vec<f64> = (a0..a1).map(|k| at[k]).collect();
            pins.extend(connector_pins(&pts, &fr, pin_m).into_iter().map(|v| v as i64));
        }
        pin_off.push(pins.len() as i64);
    }
    Ok((Array1::from_vec(pin_off).into_pyarray(py), Array1::from_vec(pins).into_pyarray(py)))
}

/// Equality as Python's float / complex comparisons have it (-0.0 is 0.0), as a hashable key; None for a NaN.
fn eq_key(p: Pt) -> Option<(u64, u64)> {
    if p[0].is_nan() || p[1].is_nan() {
        return None;
    }
    let norm = |v: f64| if v == 0.0 { 0u64 } else { v.to_bits() };
    Some((norm(p[0]), norm(p[1])))
}

type Rebuilt<'py> = (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>,
                     Bound<'py, PyArray1<bool>>);

/// roadsmooth.smooth's new coordinates: a layer's en / lonlat (N, 2) by rings (coord_off (R + 1)) with the rings
/// `rings` (k,) replaced by the points pts (M, 2), pts_off (k + 1). Returns (new coord_off, en, lonlat, new_pt): a
/// replaced ring's points that are its old vertices (the same en exactly; the last such old vertex) and its ends keep
/// their lon / lat; the others (new_pt) are left 0 for the caller's one conversion.
#[pyfunction]
pub fn road_smooth_rebuild<'py>(
    py: Python<'py>,
    en: PyReadonlyArray2<'py, f64>,
    lonlat: PyReadonlyArray2<'py, f64>,
    coord_off: PyReadonlyArray1<'py, i64>,
    rings: PyReadonlyArray1<'py, i64>,
    pts: PyReadonlyArray2<'py, f64>,
    pts_off: PyReadonlyArray1<'py, i64>,
) -> PyResult<Rebuilt<'py>> {
    let (en, ll, co, rings, pts, poff) =
        (en.as_array(), lonlat.as_array(), coord_off.as_array(), rings.as_array(), pts.as_array(), pts_off.as_array());
    let bad = |m: &str| pyo3::exceptions::PyValueError::new_err(m.to_string());
    let nr = co.len().saturating_sub(1);
    let npts = en.shape()[0];
    if en.ndim() != 2 || en.shape()[1] < 2 || ll.shape()[0] != npts || ll.shape()[1] < 2 || pts.shape()[1] < 2 {
        return Err(bad("en / lonlat / pts must be (N, 2)"));
    }
    if poff.len() != rings.len() + 1 || co.is_empty() || co[0] != 0 || co[nr] as usize > npts {
        return Err(bad("bad offsets"));
    }
    for r in 0..nr {
        if co[r + 1] < co[r] {
            return Err(bad("bad coord_off"));
        }
    }
    let mut repl: Vec<Option<usize>> = vec![None; nr];
    for (j, &r) in rings.iter().enumerate() {
        if r < 0 || r as usize >= nr || poff[j] < 0 || poff[j + 1] < poff[j] || poff[j + 1] as usize > pts.shape()[0] {
            return Err(bad("bad ring / pts_off"));
        }
        repl[r as usize] = Some(j);
    }
    let mut new_co = vec![0i64; nr + 1];
    for r in 0..nr {
        let len = match repl[r] {
            Some(j) => poff[j + 1] - poff[j],
            None => co[r + 1] - co[r],
        };
        new_co[r + 1] = new_co[r] + len;
    }
    let total = new_co[nr] as usize;
    let mut out_en = vec![0.0f64; total * 2];
    let mut out_ll = vec![0.0f64; total * 2];
    let mut new_pt = vec![false; total];
    let mut index: std::collections::HashMap<(u64, u64), usize> = std::collections::HashMap::new();
    for r in 0..nr {
        let (a0, b0) = (co[r] as usize, co[r + 1] as usize);
        let a1 = new_co[r] as usize;
        match repl[r] {
            None => {
                for k in 0..b0 - a0 {
                    out_en[2 * (a1 + k)] = en[[a0 + k, 0]];
                    out_en[2 * (a1 + k) + 1] = en[[a0 + k, 1]];
                    out_ll[2 * (a1 + k)] = ll[[a0 + k, 0]];
                    out_ll[2 * (a1 + k) + 1] = ll[[a0 + k, 1]];
                }
            }
            Some(j) => {
                let (p0, p1) = (poff[j] as usize, poff[j + 1] as usize);
                let m = p1 - p0;
                index.clear();
                for k in a0..b0 {
                    if let Some(key) = eq_key([en[[k, 0]], en[[k, 1]]]) {
                        index.insert(key, k);       // (the last vertex of these coordinates, as a dict keeps it)
                    }
                }
                for q in 0..m {
                    let p = [pts[[p0 + q, 0]], pts[[p0 + q, 1]]];
                    out_en[2 * (a1 + q)] = p[0];
                    out_en[2 * (a1 + q) + 1] = p[1];
                    new_pt[a1 + q] = true;
                    if q > 0 && q + 1 < m {
                        if let Some(&k) = eq_key(p).and_then(|key| index.get(&key)) {
                            out_ll[2 * (a1 + q)] = ll[[k, 0]];
                            out_ll[2 * (a1 + q) + 1] = ll[[k, 1]];
                            new_pt[a1 + q] = false;
                        }
                    }
                }
                if m > 0 && b0 > a0 {
                    for (q, k) in [(0usize, a0), (m - 1, b0 - 1)] {
                        out_ll[2 * (a1 + q)] = ll[[k, 0]];
                        out_ll[2 * (a1 + q) + 1] = ll[[k, 1]];
                        new_pt[a1 + q] = false;
                    }
                }
            }
        }
    }
    Ok((
        Array1::from_vec(new_co).into_pyarray(py),
        Array2::from_shape_vec((total, 2), out_en).expect("en").into_pyarray(py),
        Array2::from_shape_vec((total, 2), out_ll).expect("lonlat").into_pyarray(py),
        Array1::from_vec(new_pt).into_pyarray(py),
    ))
}

/// roadsmooth.smooth's rule fractions: frac (m,) of the rows of features feature (m,), each row of a changed line
/// (lines (k,) the features, knot_off (k + 1) into old_s / new_s: its knots) carried over by the length at the knots
/// (np.interp(f * old[-1], old, new) / new[-1], f where the new length is 0); the other rows as they are.
#[pyfunction]
pub fn road_smooth_fractions<'py>(
    py: Python<'py>,
    feature: PyReadonlyArray1<'py, i64>,
    frac: PyReadonlyArray1<'py, f64>,
    lines: PyReadonlyArray1<'py, i64>,
    knot_off: PyReadonlyArray1<'py, i64>,
    old_s: PyReadonlyArray1<'py, f64>,
    new_s: PyReadonlyArray1<'py, f64>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (feat, frac, lines, koff) = (feature.as_array(), frac.as_array(), lines.as_array(), knot_off.as_array());
    let bad = |m: &str| pyo3::exceptions::PyValueError::new_err(m.to_string());
    let old = old_s.as_slice().map_err(|_| bad("old_s must be contiguous"))?;
    let new = new_s.as_slice().map_err(|_| bad("new_s must be contiguous"))?;
    if feat.len() != frac.len() || koff.len() != lines.len() + 1 || old.len() != new.len() {
        return Err(bad("feature / frac, lines / knot_off and old_s / new_s must match"));
    }
    let mut of: std::collections::HashMap<i64, (usize, usize)> = std::collections::HashMap::new();
    for (j, &f) in lines.iter().enumerate() {
        let (a, b) = (koff[j], koff[j + 1]);
        if a < 0 || b <= a || b as usize > old.len() {
            return Err(bad("bad knot_off"));
        }
        of.insert(f, (a as usize, b as usize));
    }
    let out: Vec<f64> = feat
        .iter()
        .zip(frac.iter())
        .map(|(f, &v)| match of.get(f) {
            Some(&(a, b)) => {
                let (o, nw) = (&old[a..b], &new[a..b]);
                let last = nw[nw.len() - 1];
                if last > 0.0 {
                    crate::gradesep::np_interp(v * o[o.len() - 1], o, nw) / last
                } else {
                    v
                }
            }
            None => v,
        })
        .collect();
    Ok(Array1::from_vec(out).into_pyarray(py))
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

    fn g2() -> G2 {
        G2::from_slice(&[0.4, 1.0, 20.0, 3.0, 1.0, 0.02]).unwrap()
    }

    /// curvature (turn / mean of the two pieces) at each interior point
    fn curv(pts: &[Pt]) -> Vec<f64> {
        (1..pts.len() - 1)
            .map(|i| {
                let a = [pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]];
                let b = [pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]];
                let d = (a[0] * b[1] - a[1] * b[0]).atan2(a[0] * b[0] + a[1] * b[1]);
                d / (0.5 * (dist(pts[i - 1], pts[i]) + dist(pts[i], pts[i + 1])))
            })
            .collect()
    }

    /// the biggest change of the curvature (heading change over 10 m / 10 m) between two windows 10 m apart, the line
    /// resampled every metre (what a car's lateral pull sees, whatever the points' spacing)
    fn wjump(pts: &[Pt]) -> f64 {
        let mut s = vec![0.0];
        for w in pts.windows(2) {
            s.push(s.last().unwrap() + dist(w[0], w[1]));
        }
        let total = *s.last().unwrap();
        let mut h = Vec::new();
        let mut j = 0usize;
        let mut t = 0.0;
        while t < total {
            while j + 2 < s.len() && s[j + 1] <= t {
                j += 1;
            }
            h.push((pts[j + 1][1] - pts[j][1]).atan2(pts[j + 1][0] - pts[j][0]));
            t += 1.0;
        }
        let k: Vec<f64> = (0..h.len().saturating_sub(10)).map(|i| {
            let mut d = h[i + 10] - h[i];
            while d > std::f64::consts::PI { d -= std::f64::consts::TAU; }
            while d < -std::f64::consts::PI { d += std::f64::consts::TAU; }
            d / 10.0
        }).collect();
        (0..k.len().saturating_sub(10)).map(|i| (k[i + 10] - k[i]).abs()).fold(0.0, f64::max)
    }

    fn near(pts: &[Pt], v: Pt) -> f64 {
        pts.windows(2)
            .map(|w| {
                let ab = [w[1][0] - w[0][0], w[1][1] - w[0][1]];
                let l2 = (ab[0] * ab[0] + ab[1] * ab[1]).max(1e-12);
                let t = (((v[0] - w[0][0]) * ab[0] + (v[1] - w[0][1]) * ab[1]) / l2).clamp(0.0, 1.0);
                dist([w[0][0] + ab[0] * t, w[0][1] + ab[1] * t], v)
            })
            .fold(f64::MAX, f64::min)
    }

    /// a chorded oval curve: 19 m pieces turning unevenly (Daytona's mapped banking), between two straights
    fn chorded() -> Vec<Pt> {
        let mut line = vec![[-300.0, 0.0], [0.0, 0.0]];
        let mut ang = 0.0f64;
        for (i, t) in [2.0, 4.5, 2.5, 5.2, 3.0, 3.8, 2.2, 4.9, 3.5, 2.8].iter().enumerate() {
            ang += (*t as f64).to_radians();
            let l = *line.last().unwrap();
            let len = if i % 2 == 0 { 19.0 } else { 17.5 };
            line.push([l[0] + len * ang.cos(), l[1] + len * ang.sin()]);
        }
        let l = *line.last().unwrap();
        line.push([l[0] + 300.0 * ang.cos(), l[1] + 300.0 * ang.sin()]);
        line
    }

    #[test]
    fn g2_curvature_has_no_steps_and_ends_stay() {
        let line = chorded();
        let (g1, _, _) = smooth_core(&line, 30.0, &params()).unwrap();
        let (pts, old_s, new_s) = smooth_g2(&line, 30.0, &[], 40.0, &params(), &g2()).unwrap();
        assert_eq!(pts[0], line[0]);
        assert_eq!(*pts.last().unwrap(), *line.last().unwrap());
        // the curve itself (kept densely: the points kept by out_tol turn up to a degree or two, which a 10 m window
        // reads as a step of its own)
        let dense = G2 { out_tol: 0.0005, out_m: 3.0, ..g2() };
        let (fine, _, _) = smooth_g2(&line, 30.0, &[], 40.0, &params(), &dense).unwrap();
        let (j1, j2) = (wjump(&g1), wjump(&fine));
        assert!(j2 < 0.45 * j1, "curvature steps {j1} -> {j2}");
        // still on the mapped curve
        for v in &line[1..line.len() - 1] {
            assert!(near(&pts, *v) < 1.2, "{}", near(&pts, *v));
        }
        // knots: monotone, ending at the new length
        assert!(new_s.windows(2).all(|w| w[1] >= w[0] - 1e-9));
        let total: f64 = pts.windows(2).map(|w| dist(w[0], w[1])).sum();
        assert!((new_s.last().unwrap() - total).abs() < 1e-6);
        assert_eq!(old_s.len(), new_s.len());
    }

    #[test]
    fn g2_straights_stay_straight_and_sparse() {
        let line = [[0.0, 0.0], [400.0, 0.0], [400.0 + 400.0 * 0.5f64.cos(), 400.0 * 0.5f64.sin()]];
        let (pts, _, _) = smooth_g2(&line, 300.0, &[], 40.0, &params(), &g2()).unwrap();
        // the first straight: one piece up to where the spiral begins, on y = 0
        assert!(pts[1][1].abs() < 0.02, "{:?}", pts[1]);  // (the closing shear: mm)
        assert!(pts[1][0] > 200.0, "{:?}", pts[1]);
        // no turn above out_turn between two points kept
        for t in turns(&pts) {
            assert!(t <= 3.0 + 0.6, "{t}");
        }
    }

    #[test]
    fn g2_points_kept_stay_on_the_curve() {
        let line = chorded();
        let dense = G2 { out_tol: 0.0005, out_m: 1.0, ..g2() };
        let (fine, _, _) = smooth_g2(&line, 30.0, &[], 40.0, &params(), &dense).unwrap();
        let (pts, _, _) = smooth_g2(&line, 30.0, &[], 40.0, &params(), &g2()).unwrap();
        assert!(pts.len() * 2 < fine.len(), "{} {}", pts.len(), fine.len());
        for v in &fine {
            assert!(near(&pts, *v) < 0.02 + 1e-3, "{}", near(&pts, *v));
        }
    }

    #[test]
    fn g2_pins_stay_bit_for_bit() {
        let line = chorded();
        let pins = [4usize, 7];
        let (pts, _, _) = smooth_g2(&line, 30.0, &pins, 40.0, &params(), &g2()).unwrap();
        for &i in &pins {
            assert!(pts.contains(&line[i]), "pin {i} moved");
        }
        assert_eq!(pts[0], line[0]);
        // no curvature spike at the pins (the misses carried by a spline, not straight lines between the anchors)
        let dense = G2 { out_tol: 0.0005, out_m: 3.0, ..g2() };
        let (free, _, _) = smooth_g2(&line, 30.0, &[], 40.0, &params(), &dense).unwrap();
        let (pinned, _, _) = smooth_g2(&line, 30.0, &pins, 40.0, &params(), &dense).unwrap();
        // (a pin holds the line at its vertex: the misses bend it a little round there, well under the corner itself)
        assert!(wjump(&pinned) < 2.5 * wjump(&free) + 2e-4, "{} {}", wjump(&pinned), wjump(&free));
    }

    #[test]
    fn g2_a_ring_is_rounded_but_its_ends_stay() {
        let mut ring: Vec<Pt> = (0..12).map(|i| {
            let a = i as f64 * std::f64::consts::TAU / 12.0;
            [20.0 * a.cos(), 20.0 * a.sin()]
        }).collect();
        ring.push(ring[0]);
        let (pts, _, _) = smooth_g2(&ring, 12.0, &[], 10.0, &params(), &g2()).unwrap();
        assert_eq!(pts[0], ring[0]);
        assert_eq!(*pts.last().unwrap(), ring[0]);
        assert!(pts.len() > ring.len());
    }

    #[test]
    fn g2_hairpin_corner_keeps_its_place() {
        let line = [[0.0, 0.0], [50.0, 0.0], [0.0, 5.0], [-30.0, 40.0]];
        if let Some((pts, _, _)) = smooth_g2(&line, 20.0, &[], 20.0, &params(), &g2()) {
            assert!(near(&pts, line[1]) < 1.0);
            assert_eq!(*pts.last().unwrap(), line[3]);
        }
    }
}
