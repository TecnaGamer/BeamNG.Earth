//! junctions.find: the street junctions' corners (curb returns, or the bare pavement's rounded corners), the sidewalk
//! cuts, crosswalks, stop lines and curb ramps of a list of road centrelines (server/junctions.py says what each is).
//! In Python its loops were most of a cold visit's junction CPU: a cold teleport to Santa Fe NM spent ~45 CPU-s of
//! ~885 in find (py-spy, 2026-11-11), most of it testing corners and crosswalks against the carriageways near them
//! (_on_carriageway, whose road boxes were remade for a fresh list at every crosswalk). Bit for bit the Python's
//! numbers: the same products, sums and comparisons in its order, Python's math.hypot where it called that and the C
//! library's hypot where numpy's np.hypot ran, numpy's linspace / gradient / mean formulas, Python's float % and
//! round(), np.round(x, 2) as the junctions' order and np.argmin's first minimum.

use std::collections::{BTreeMap, HashMap};

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet, PyTuple};

use crate::py_hypot;

type P2 = [f64; 2];
type P3 = [f64; 3];

#[inline]
fn add(a: P2, b: P2) -> P2 { [a[0] + b[0], a[1] + b[1]] }
#[inline]
fn sub(a: P2, b: P2) -> P2 { [a[0] - b[0], a[1] - b[1]] }
#[inline]
fn mul(a: P2, s: f64) -> P2 { [a[0] * s, a[1] * s] }
#[inline]
fn neg(a: P2) -> P2 { [-a[0], -a[1]] }
/// numpy's a @ b of two 2-vectors (measured equal to the plain products' sum on every input tried).
#[inline]
fn dot(a: P2, b: P2) -> f64 { a[0] * b[0] + a[1] * b[1] }
#[inline]
fn left(u: P2) -> P2 { [-u[1], u[0]] }
#[inline]
fn xy(p: &P3) -> P2 { [p[0], p[1]] }
/// math.hypot of a vector (Python's: CPython's vector_norm).
#[inline]
fn mhyp(v: P2) -> f64 { py_hypot(v[0], v[1]) }
/// np.hypot of a vector (the C library's hypot).
#[inline]
fn nhyp(v: P2) -> f64 { v[0].hypot(v[1]) }

/// Python's x % m for floats (the sign of m).
fn pmod(x: f64, m: f64) -> f64 {
    let r = x % m;
    if r != 0.0 {
        if (m < 0.0) != (r < 0.0) { r + m } else { r }
    } else {
        0.0f64.copysign(m)
    }
}

const TWO_PI: f64 = 2.0 * std::f64::consts::PI;
const RAD2DEG: f64 = 180.0 / std::f64::consts::PI;     // CPython's math.degrees factor
const DEG2RAD: f64 = std::f64::consts::PI / 180.0;     // ... and math.radians'

/// junctions._unit.
fn unit(v: P2) -> P2 {
    let n = mhyp(v);
    if n > 1e-9 { [v[0] / n, v[1] / n] } else { v }
}

/// junctions._line_x: where lines p + t d and q + s e meet, or None if parallel.
fn line_x(p: P2, d: P2, q: P2, e: P2) -> Option<P2> {
    let den = d[0] * e[1] - d[1] * e[0];
    if den.abs() < 1e-9 {
        return None;
    }
    let w = sub(q, p);
    let t = (w[0] * e[1] - w[1] * e[0]) / den;
    Some(add(p, mul(d, t)))
}

/// np.linspace(start, stop, k) (k >= 2).
fn linspace(start: f64, stop: f64, k: usize) -> Vec<f64> {
    let div = (k - 1) as f64;
    let delta = stop - start;
    let step = delta / div;
    let mut y: Vec<f64> = if step == 0.0 {
        (0..k).map(|i| (i as f64 / div) * delta).collect()
    } else {
        (0..k).map(|i| i as f64 * step).collect()
    };
    for v in y.iter_mut() {
        *v += start;
    }
    y[k - 1] = stop;
    y
}

/// np.gradient(a, axis=0) of points (n >= 2), its unit normals (-g1, g0) / max(hypot, 1e-9).
fn normals(c: &[P2]) -> Vec<P2> {
    let n = c.len();
    (0..n).map(|i| {
        let g = if i == 0 {
            sub(c[1], c[0])
        } else if i == n - 1 {
            sub(c[n - 1], c[n - 2])
        } else {
            let d = sub(c[i + 1], c[i - 1]);
            [d[0] / 2.0, d[1] / 2.0]
        };
        let v = [-g[1], g[0]];
        let h = nhyp(v);
        let m = if h.is_nan() { f64::NAN } else if h > 1e-9 { h } else { 1e-9 };
        [v[0] / m, v[1] / m]
    }).collect()
}

/// [0, cumsum(np.hypot(diff(c)))].
fn arclen(c: &[P2]) -> Vec<f64> {
    let mut s = vec![0.0f64; c.len()];
    for k in 1..c.len() {
        s[k] = s[k - 1] + nhyp(sub(c[k], c[k - 1]));
    }
    s
}

/// junctions._branch_dir.
fn branch_dir(p: &[P3], vi: usize, step: i64, branch_m: f64) -> P2 {
    let p0 = xy(&p[vi]);
    let (mut k, mut acc, mut q) = (vi as i64, 0.0f64, p0);
    while 0 <= k + step && ((k + step) as usize) < p.len() && acc < branch_m {
        k += step;
        acc += mhyp(sub(xy(&p[k as usize]), xy(&p[(k - step) as usize])));
        q = xy(&p[k as usize]);
    }
    unit(sub(q, p0))
}

/// junctions._z_along.
fn z_along(p: &[P3], vi: usize, step: i64, t: f64) -> f64 {
    let (mut k, mut acc) = (vi as i64, 0.0f64);
    while 0 <= k + step && ((k + step) as usize) < p.len() {
        let (a, b) = (&p[k as usize], &p[(k + step) as usize]);
        let seg = mhyp(sub(xy(b), xy(a)));
        if acc + seg >= t {
            let f = if seg > 0.0 { (t - acc) / seg } else { 0.0 };
            return a[2] + f * (b[2] - a[2]);
        }
        acc += seg;
        k += step;
    }
    p[k as usize][2]
}

/// junctions._branch_len.
fn branch_len(p: &[P3], vi: usize, step: i64) -> f64 {
    let (mut k, mut acc) = (vi as i64, 0.0f64);
    while 0 <= k + step && ((k + step) as usize) < p.len() {
        acc += mhyp(sub(xy(&p[(k + step) as usize]), xy(&p[k as usize])));
        k += step;
    }
    acc
}

/// junctions._crosses: whether segment a-b crosses the polyline.
fn crosses<I: Fn(usize) -> P2>(a: P2, b: P2, n: usize, at: I) -> bool {
    let side = |p: P2, q: P2, x: P2| (q[0] - p[0]) * (x[1] - p[1]) - (q[1] - p[1]) * (x[0] - p[0]);
    for i in 0..n.saturating_sub(1) {
        let (pp, qq) = (at(i), at(i + 1));
        let (d1, d2) = (side(pp, qq, a), side(pp, qq, b));
        let (d3, d4) = (side(a, b, pp), side(a, b, qq));
        if d1 * d2 < 0.0 && d3 * d4 < 0.0 {
            return true;
        }
    }
    false
}

/// junctions._seg_dist's distances of points to one segment a-b.
fn seg_dist(p: P2, a: P2, b: P2) -> f64 {
    let ab = sub(b, a);
    let l2 = ab[0] * ab[0] + ab[1] * ab[1];
    let l2 = if l2.is_nan() { l2 } else if l2 > 1e-12 { l2 } else { 1e-12 };
    let t = ((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / l2;
    let t = if t.is_nan() { t } else if t < 0.0 { 0.0 } else if t > 1.0 { 1.0 } else { t };
    let q = [a[0] + t * ab[0], a[1] + t * ab[1]];
    nhyp(sub(p, q))
}

/// junctions._corner_is_simple: the arc moved CURB_W along its inward normals, then the outer corner points, closed:
/// no side crossing another.
fn corner_is_simple(curb: &[P2], inward: &[P2], outer: &[P3], curb_w: f64) -> bool {
    let mut closed: Vec<P2> = curb.iter().zip(inward).map(|(c, i)| add(*c, mul(*i, curb_w))).collect();
    closed.extend(outer.iter().map(xy));
    closed.push(closed[0]);
    let m = closed.len();
    for s in 0..m - 1 {
        if crosses(closed[s], closed[s + 1], m, |i| closed[i]) {
            return false;
        }
    }
    true
}

/// the parameters (junctions.py's constants, in fast's order)
struct K {
    radius: f64, corner_lo: f64, corner_hi: f64, acute_deg: f64, acute_t_m: f64, acute_r_min: f64, branch_m: f64,
    bulb_m: f64, taper_m: f64, nose_deg: f64, nose_gap_m: f64, nose_max_m: f64, cw_cell_m: f64, cw_gap: f64,
    cw_w: f64, stop_gap: f64, stop_w: f64, node_tol: f64, merge_m: f64, connector_m: f64, connector_widths: f64,
    pave_radius: f64, pave_radius_major: f64, service_r: f64, curb_w: f64, radius_tries: Vec<f64>,
}

const HAS_WALK: u8 = 1;
const HAS_PARKING: u8 = 2;
const ROUNDABOUT: u8 = 4;
const SIDEWALKS: u8 = 8;
const SERVICE: u8 = 16;
const MAJOR: u8 = 32;

/// a road side's sidewalk (spec.street.side(...)): no street record, a side without a curbside walk (the corner
/// skipped), or a walk (width, its (width, surface) rank: footway's max)
#[derive(Clone, Copy)]
enum Fw {
    None,
    Skip,
    Walk(f64, i64),
}

struct Net<'a> {
    p: &'a [P3],
    off: Vec<usize>,
    fid: Vec<i64>,
    width: Vec<f64>,
    rad: Vec<f64>,
    walk: Vec<f64>,
    flags: Vec<u8>,
    fw: Vec<[Fw; 2]>,
    boxes: Vec<[f64; 4]>,
}

impl<'a> Net<'a> {
    fn n(&self) -> usize { self.width.len() }
    fn pts(&self, r: usize) -> &'a [P3] { &self.p[self.off[r]..self.off[r + 1]] }
    fn has(&self, r: usize, f: u8) -> bool { self.flags[r] & f != 0 }
    fn side(&self, r: usize, side: i64) -> Fw { self.fw[r][if side > 0 { 0 } else { 1 }] }

    /// whether road r's polyline passes closer than h to (px, py): the any_within test (lib.rs polyline_min_dist's
    /// numbers; its minimum is under h iff one segment's distance is). A segment whose box, grown by h and a
    /// micrometre (the rounding of its nearest point), does not hold the point is not measured: it cannot be nearer.
    fn within(&self, r: usize, px: f64, py: f64, h: f64) -> bool {
        let l = self.pts(r);
        let g = h + 1e-6;
        for i in 0..l.len().saturating_sub(1) {
            let (ax, ay) = (l[i][0], l[i][1]);
            let (bx, by) = (l[i + 1][0], l[i + 1][1]);
            if px < ax.min(bx) - g || px > ax.max(bx) + g || py < ay.min(by) - g || py > ay.max(by) + g {
                continue;
            }
            let (dx, dy) = (bx - ax, by - ay);
            let l2 = (dx * dx + dy * dy).max(1e-12);
            let t = (((px - ax) * dx + (py - ay) * dy) / l2).clamp(0.0, 1.0);
            let d = (ax + dx * t - px).hypot(ay + dy * t - py);
            if d < h {
                return true;
            }
        }
        false
    }

    /// junctions._on_carriageway: whether any of pts lies on a road's carriageway (centreline distance < half width
    /// - margin) among the roads whose box reaches within r of c. half: (fid, half width) overrides (the last one
    /// of a fid counts, as a dict built from them); skip_fid: fids not checked; skip_idx: a road not checked.
    fn on_carriageway(&self, pts: &[P2], c: P2, r: f64, margin: f64, half: &[(i64, f64)], skip_fid: &[i64],
                      skip_idx: Option<usize>) -> bool {
        let (x0, y0, x1, y1) = (c[0] - r, c[1] - r, c[0] + r, c[1] + r);
        for i in 0..self.n() {
            let b = &self.boxes[i];
            if !(b[2] >= x0 && b[3] >= y0 && b[0] <= x1 && b[1] <= y1) {
                continue;
            }
            if skip_idx == Some(i) || skip_fid.contains(&self.fid[i]) {
                continue;
            }
            let mut h = self.width[i] / 2.0;
            for &(f, v) in half {
                if f == self.fid[i] {
                    h = v;
                }
            }
            let h = h - margin;
            if !(h > 0.0) {
                continue;                   // (no distance is under it)
            }
            if pts.iter().any(|q| self.within(i, q[0], q[1], h)) {
                return true;
            }
        }
        false
    }
}

/// a junction branch: (angle round the junction, road, vertex, step along it, direction, the node)
#[derive(Clone, Copy)]
struct Br {
    ang: f64,
    ri: usize,
    vi: usize,
    step: i64,
    u: P2,
    p: P2,
}

#[derive(Default)]
struct Corner {
    arc: Vec<P3>,
    inward: Vec<P2>,
    outer: Vec<P3>,
    fill: Option<Vec<P3>>,
    fills: Option<Vec<Vec<P3>>>,
    footway: Option<(usize, usize)>,     // (road, side index 0 left / 1 right) of the walk object
    sides: Option<(usize, i64, usize, i64)>,
    edge: Option<(P2, P2, f64, P2, P2, f64)>,
    ramps: Vec<(f64, f64)>,
}

/// side_t's entry: (cut t, corner, ("dec" / "inc", s0, tref), bulb)
#[derive(Clone, Copy)]
struct SideT {
    t: f64,
    corner: usize,
    dec: bool,
    s0: Option<f64>,
    tref: f64,
    bulb: f64,
}

#[derive(Default)]
struct Out {
    corners: Vec<Corner>,
    cuts: Vec<(usize, i64, P2, P2, f64)>,
    crosswalks: Vec<(P3, P3)>,
    stops: Vec<(P3, P3)>,
    ramps: Vec<(usize, i64, P2, P2, f64, f64)>,
    line_cuts: Vec<(usize, P2, P2, f64)>,
    internal: Vec<usize>,
}

/// junctions._piece: (curb points, t1, t2, the road edges' meeting point)
type Piece = (Vec<P2>, f64, f64, Option<P2>);

fn piece(k: &K, p: P2, u1: P2, w1: f64, u2: P2, w2: f64, r: f64) -> Option<Piece> {
    let (n1, n2) = (left(u1), left(u2));
    let theta = pmod(u2[1].atan2(u2[0]) - u1[1].atan2(u1[0]), TWO_PI) * RAD2DEG;
    if theta >= k.corner_hi {
        return Some((vec![add(p, mul(n1, w1))], 0.0, 0.0, None));
    }
    let z = line_x(add(p, mul(n1, w1 + r)), u1, sub(p, mul(n2, w2 + r)), u2)?;
    let (t1, t2) = (sub(z, mul(n1, r)), add(z, mul(n2, r)));
    let a0 = (t1[1] - z[1]).atan2(t1[0] - z[0]);
    let a1 = (t2[1] - z[1]).atan2(t2[0] - z[0]);
    let da = pmod(a1 - a0 + std::f64::consts::PI, TWO_PI) - std::f64::consts::PI;
    let kk = 3usize.max((da.abs() * r / 0.75) as usize + 1);
    let arc: Vec<P2> = linspace(0.0, 1.0, kk).iter().map(|&s| {
        let ang = a0 + da * s;
        [z[0] + r * ang.cos(), z[1] + r * ang.sin()]
    }).collect();
    let c = line_x(add(p, mul(n1, w1)), u1, sub(p, mul(n2, w2)), u2);
    Some((arc, dot(sub(t1, p), u1), dot(sub(t2, p), u2), c))
}

fn walk_width(net: &Net, f: Fw, r: usize, walk_w: Option<f64>) -> f64 {
    match f {
        Fw::Walk(w, _) => w,
        _ => walk_w.unwrap_or(net.walk[r]),
    }
}

/// junctions._across: the corner from branch bi (its left side) to bj (its right side) whose nodes the road cr
/// joins: (corner, cut on bi, cut on bj).
#[allow(clippy::too_many_arguments)]
fn across(k: &K, net: &Net, bi: &Br, bj: &Br, cr: usize, x: P2, walk_w: Option<f64>, fi: Fw, fj: Fw)
          -> Option<(Corner, f64, f64)> {
    let (ri, vi, si, ui, pi) = (bi.ri, bi.vi, bi.step, bi.u, bi.p);
    let (rj, vj, sj, uj, pj) = (bj.ri, bj.vi, bj.step, bj.u, bj.p);
    let d = sub(pj, pi);
    let lc = mhyp(d);
    if lc < 1e-6 {
        return None;
    }
    let uc = [d[0] / lc, d[1] / lc];
    let (wi, wj, wc) = (net.width[ri] / 2.0, net.width[rj] / 2.0, net.width[cr] / 2.0);
    let r = net.rad[ri].max(net.rad[rj]).max(net.rad[cr]);
    let p1 = piece(k, pi, ui, wi, uc, wc, r);
    let p2 = piece(k, pj, neg(uc), wc, uj, wj, r);
    let ((mut a1, mut ti, mut tc1, mut c1), (mut a2, mut tc2, mut tj, mut c2)) = (p1?, p2?);
    if a1.len() == 1 && a2.len() > 1 && tc2 > lc {
        a1.clear();
        ti = tc2 - lc;
        tc1 = 0.0;
        c1 = None;
        tc2 = lc;
    } else if a2.len() == 1 && a1.len() > 1 && tc1 > lc {
        a2.clear();
        tj = tc1 - lc;
        tc2 = 0.0;
        c2 = None;
        tc1 = lc;
    }
    if ti < 0.0 || tj < 0.0 || tc1 < 0.0 || tc2 < 0.0 || tc1 + tc2 > lc + 0.25 {
        return None;
    }
    if branch_len(net.pts(ri), vi, si) < ti + 1.0 || branch_len(net.pts(rj), vj, sj) < tj + 1.0 {
        return None;
    }
    let all: Vec<P2> = a1.iter().chain(a2.iter()).copied().collect();
    let mut curb2: Vec<P2> = Vec::with_capacity(all.len());
    for (i, q) in all.iter().enumerate() {
        if i == 0 || nhyp(sub(*q, all[i - 1])) > 0.05 {
            curb2.push(*q);
        }
    }
    if curb2.len() < 2 {
        return None;
    }
    let (zi, zj) = (z_along(net.pts(ri), vi, si, ti), z_along(net.pts(rj), vj, sj, tj));
    let s = arclen(&curb2);
    let last = s[s.len() - 1];
    let den = if 1e-9 > last { 1e-9 } else { last };
    let arc: Vec<P3> = curb2.iter().zip(&s).map(|(c, sv)| [c[0], c[1], zi + (zj - zi) * sv / den]).collect();
    let mut nrm = normals(&curb2);
    for (c, nv) in curb2.iter().zip(nrm.iter_mut()) {
        if (c[0] - x[0]) * nv[0] + (c[1] - x[1]) * nv[1] < 0.0 {
            *nv = neg(*nv);
        }
    }
    let wki = walk_width(net, fi, ri, walk_w);
    let wkj = walk_width(net, fj, rj, walk_w);
    let wkc = wki.max(wkj);
    let (ni, nj, nc) = (left(ui), left(uj), left(uc));
    let oi = line_x(add(pi, mul(ni, wi + wki)), ui, sub(pi, mul(nc, wc + wkc)), uc);
    let oj = line_x(sub(pj, mul(nc, wc + wkc)), neg(uc), sub(pj, mul(nj, wj + wkj)), uj);
    let oi = match oi { Some(o) if a1.len() != 1 => o, _ => add(pi, mul(ni, wi + wki)) };
    let oj = match oj { Some(o) if a2.len() != 1 => o, _ => sub(pj, mul(nj, wj + wkj)) };
    if mhyp(sub(oi, x)) > 20.0 + lc || mhyp(sub(oj, x)) > 20.0 + lc {
        return None;
    }
    let zo = 0.5 * (zi + zj);
    let mids: Vec<P2> = if a1.is_empty() { vec![oj] } else if a2.is_empty() { vec![oi] } else { vec![oj, oi] };
    let e = sub(add(pj, mul(uj, tj)), mul(nj, wj + wkj));
    let mut outer: Vec<P3> = vec![[e[0], e[1], zj]];
    outer.extend(mids.iter().map(|o| [o[0], o[1], zo]));
    let e = add(add(pi, mul(ui, ti)), mul(ni, wi + wki));
    outer.push([e[0], e[1], zi]);
    let has_fw = !matches!(fi, Fw::None) || !matches!(fj, Fw::None);
    if has_fw && !corner_is_simple(&curb2, &nrm, &outer, k.curb_w) {
        return None;
    }
    let mut probe: Vec<P2> = curb2.iter().zip(&nrm).map(|(c, nv)| add(*c, mul(*nv, 0.8))).collect();
    probe.extend(curb2.iter().zip(&nrm).map(|(c, nv)| add(*c, mul(*nv, 2.0))));
    probe.extend(mids.iter().copied());
    let half = [(net.fid[ri], wi), (net.fid[rj], wj), (net.fid[cr], wc)];
    if net.on_carriageway(&probe, x, 60.0, 0.3, &half, &[], None) {
        return None;
    }
    let mut fills = Vec::new();
    for (c, arc_) in [(c1, &a1), (c2, &a2)] {
        if let Some(c) = c {
            if arc_.len() > 1 {
                let mut f = vec![[c[0], c[1], zo]];
                f.extend(arc_.iter().map(|q| [q[0], q[1], zo]));
                fills.push(f);
            }
        }
    }
    Some((Corner { arc, inward: nrm, outer, fill: None, fills: Some(fills), ..Default::default() }, ti, tj))
}

/// union-find root with path halving (junctions.find's root)
fn root(parent: &mut [usize], mut a: usize) -> usize {
    while parent[a] != a {
        parent[a] = parent[parent[a]];
        a = parent[a];
    }
    a
}

fn cmp_f(a: f64, b: f64) -> std::cmp::Ordering {
    a.partial_cmp(&b).unwrap_or(std::cmp::Ordering::Equal)
}

#[allow(clippy::too_many_arguments)]
fn find_core(k: &K, net: &Net, walk_w: Option<f64>, parking_m: f64, pavement: bool, bounds: Option<[f64; 4]>)
             -> Out {
    let n = net.n();
    let mut out = Out::default();
    // nodes: vertices by their NODE_TOL grid key (Python's round: half to even)
    let mut nodes: BTreeMap<(i64, i64), Vec<(usize, usize)>> = BTreeMap::new();
    for ri in 0..n {
        for (vi, p) in net.pts(ri).iter().enumerate() {
            let key = ((p[0] / k.node_tol).round_ties_even() as i64, (p[1] / k.node_tol).round_ties_even() as i64);
            nodes.entry(key).or_default().push((ri, vi));
        }
    }
    let jn: Vec<Vec<(usize, usize)>> = nodes.into_values().filter(|inc| {
        let first = inc[0].0;
        inc.iter().any(|&(ri, _)| ri != first)
    }).collect();
    let pos: Vec<P2> = jn.iter().map(|inc| xy(&net.pts(inc[0].0)[inc[0].1])).collect();
    let nj = jn.len();
    let mut parent: Vec<usize> = (0..nj).collect();
    // node pairs closer than MERGE_M (a grid of MERGE_M bins)
    if k.merge_m > 0.0 {
        let mut bins: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
        let keys: Vec<(i64, i64)> = pos.iter().map(|p| ((p[0] / k.merge_m).floor() as i64,
                                                        (p[1] / k.merge_m).floor() as i64)).collect();
        for (i, key) in keys.iter().enumerate() {
            bins.entry(*key).or_default().push(i);
        }
        for (a, &(gx, gy)) in keys.iter().enumerate() {
            for dx in -1..=1 {
                for dy in -1..=1 {
                    if let Some(v) = bins.get(&(gx + dx, gy + dy)) {
                        for &b in v {
                            if b > a && mhyp(sub(pos[a], pos[b])) < k.merge_m {
                                let ra = root(&mut parent, a);
                                parent[ra] = root(&mut parent, b);
                            }
                        }
                    }
                }
            }
        }
    }
    // junction nodes joined by a short connector
    let mut node_of: Vec<i64> = vec![-1; net.p.len()];
    for (a, inc) in jn.iter().enumerate() {
        for &(ri, vi) in inc {
            node_of[net.off[ri] + vi] = a as i64;
        }
    }
    let node_w: Vec<f64> = jn.iter().map(|inc| {
        let mut m = net.width[inc[0].0];
        for &(ri, _) in &inc[1..] {
            if net.width[ri] > m {
                m = net.width[ri];
            }
        }
        m
    }).collect();
    let mut reach: Vec<f64> = vec![0.0; n];
    let mut connector: HashMap<(usize, usize), usize> = HashMap::new();
    for ri in 0..n {
        let pts = net.pts(ri);
        let mut s_at = vec![0.0f64; pts.len()];
        for v in 1..pts.len() {
            s_at[v] = s_at[v - 1] + nhyp(sub(xy(&pts[v]), xy(&pts[v - 1])));
        }
        let on: Vec<(usize, usize)> = (0..pts.len()).filter(|&vi| node_of[net.off[ri] + vi] >= 0)
            .map(|vi| (vi, node_of[net.off[ri] + vi] as usize)).collect();
        for w in on.windows(2) {
            let ((v0, a), (v1, b)) = (w[0], w[1]);
            let l = s_at[v1] - s_at[v0];
            if !net.has(ri, ROUNDABOUT) && (l < k.connector_m
                                            || l <= k.connector_widths * node_w[a].max(node_w[b])) {
                let ra = root(&mut parent, a);
                parent[ra] = root(&mut parent, b);
                reach[ri] = reach[ri].max(l + 0.5);
                if a != b {
                    connector.entry((a.min(b), a.max(b))).or_insert(ri);
                }
            }
        }
    }
    // groups (in order of their first node), each vertex's group
    let mut gid_of_root: HashMap<usize, usize> = HashMap::new();
    let mut groups: Vec<Vec<usize>> = Vec::new();
    for a in 0..nj {
        let r = root(&mut parent, a);
        let g = *gid_of_root.entry(r).or_insert_with(|| { groups.push(Vec::new()); groups.len() - 1 });
        groups[g].push(a);
    }
    let mut at_cluster: Vec<i64> = vec![-1; net.p.len()];
    for (g, members) in groups.iter().enumerate() {
        for &a in members {
            for &(ri, vi) in &jn[a] {
                at_cluster[net.off[ri] + vi] = g as i64;
            }
        }
    }
    let at = |ri: usize, vi: usize| at_cluster[net.off[ri] + vi];
    let max_fid = net.fid.iter().copied().max().unwrap_or(-1);
    let mut internal = vec![false; (max_fid + 1).max(0) as usize];
    for ri in 0..n {
        let pts = net.pts(ri);
        if pts.is_empty() {
            continue;
        }
        let (c0, c1) = (at(ri, 0), at(ri, pts.len() - 1));
        if c0 >= 0 && c0 == c1 && branch_len(pts, 0, 1) < (3.0 * k.merge_m).max(k.connector_m) {
            if !internal[net.fid[ri] as usize] {
                out.internal.push(ri);
            }
            internal[net.fid[ri] as usize] = true;
        }
    }
    let leads_inside = |ri: usize, vi: usize, step: i64, cid: i64| -> bool {
        let pts = net.pts(ri);
        let lim = (2.0 * k.merge_m).max(k.connector_m).max(reach[ri]);
        let (mut kk, mut acc) = (vi as i64, 0.0f64);
        while 0 <= kk + step && ((kk + step) as usize) < pts.len() && acc < lim {
            acc += mhyp(sub(xy(&pts[(kk + step) as usize]), xy(&pts[kk as usize])));
            kk += step;
            if at(ri, kk as usize) == cid {
                return true;
            }
        }
        false
    };
    let near_c = |c: P2, skip: usize| -> Vec<usize> {
        (0..n).filter(|&r| {
            if r == skip {
                return false;
            }
            let b = &net.boxes[r];
            if !(b[2] >= c[0] - 60.0 && b[3] >= c[1] - 60.0 && b[0] <= c[0] + 60.0 && b[1] <= c[1] + 60.0) {
                return false;      // (no point of it within 60 m: every point is in its box)
            }
            net.pts(r).iter().any(|p| nhyp(sub(xy(p), c)) < 60.0)
        }).collect()
    };
    let mut cw_grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    let mut cw_half = 0.0f64;
    // the junctions in order of their first node's position (np.round(pos, 2)), stable
    let r2 = |v: f64| (v * 100.0).round_ties_even() / 100.0;
    let mut order: Vec<usize> = (0..groups.len()).collect();
    order.sort_by(|&a, &b| {
        let (pa, pb) = (pos[groups[a][0]], pos[groups[b][0]]);
        cmp_f(r2(pa[0]), r2(pb[0])).then(cmp_f(r2(pa[1]), r2(pb[1])))
    });
    for &cid in &order {
        let members = &groups[cid];
        let mut inc: Vec<(usize, usize)> = members.iter().flat_map(|&a| jn[a].iter().copied()).collect();
        inc.sort_unstable();
        inc.dedup();
        let mut x = pos[members[0]];
        for &a in &members[1..] {
            x = add(x, pos[a]);
        }
        let m = members.len() as f64;
        x = [x[0] / m, x[1] / m];
        if let Some(bx) = bounds {
            if pavement && walk_w == Some(0.0) && parking_m == 0.0 && members.len() == 1 {
                let mut radius = k.pave_radius_major.max(k.radius);
                let mut wmax = net.width[inc[0].0];
                for &(ri, _) in &inc {
                    radius = radius.max(net.rad[ri]);
                    if net.width[ri] > wmax {
                        wmax = net.width[ri];
                    }
                }
                let extent = (40.0 + wmax / 2.0 + 2.0 * radius).max(20.0 + k.merge_m) + 2.0 * k.node_tol;
                if x[0] + extent < bx[0] || x[0] - extent > bx[2] || x[1] + extent < bx[1] || x[1] - extent > bx[3] {
                    continue;
                }
            }
        }
        let mut branches: Vec<Br> = Vec::new();
        for &(ri, vi) in &inc {
            let pts = net.pts(ri);
            if internal[net.fid[ri] as usize] {
                continue;
            }
            for step in [1i64, -1] {
                let nv = vi as i64 + step;
                if 0 <= nv && (nv as usize) < pts.len() && !leads_inside(ri, vi, step, cid as i64) {
                    let u = branch_dir(pts, vi, step, k.branch_m);
                    let p = xy(&pts[vi]);
                    let q = sub(add(p, mul(u, k.branch_m)), x);
                    branches.push(Br { ang: q[1].atan2(q[0]), ri, vi, step, u, p });
                }
            }
        }
        branches.sort_by(|a, b| cmp_f(a.ang, b.ang).then(net.fid[a.ri].cmp(&net.fid[b.ri])));
        let nb = branches.len();
        if nb < 2 {
            continue;
        }
        let mut side_t: HashMap<(usize, bool), SideT> = HashMap::new();     // (branch, left side?)
        for i in 0..nb {
            let j = (i + 1) % nb;
            let (bi, bj) = (branches[i], branches[j]);
            let (ri, vi, si, ui, li, pi) = (bi.ri, bi.vi, bi.step, bi.u, bi.step, bi.p);
            let (rj, vj, sj, uj, lj, pj) = (bj.ri, bj.vi, bj.step, bj.u, bj.step, bj.p);
            let theta = pmod(uj[1].atan2(uj[0]) - ui[1].atan2(ui[0]), TWO_PI) * RAD2DEG;
            if ri == rj && nb == 2 {
                continue;
            }
            let (mut fi, mut fj) = (Fw::None, Fw::None);
            if !pavement {
                fi = net.side(ri, li);
                if matches!(fi, Fw::Skip) {
                    continue;
                }
                fj = net.side(rj, -lj);
                if matches!(fj, Fw::Skip) {
                    continue;
                }
            }
            // footway: max of the walks by (width, surface), the first of equals
            let footway: Option<(usize, usize)> = match (fi, fj) {
                (Fw::Walk(_, a), Fw::Walk(_, b)) => Some(if b > a { (rj, if -lj > 0 { 0 } else { 1 }) }
                                                          else { (ri, if li > 0 { 0 } else { 1 }) }),
                (Fw::Walk(..), _) => Some((ri, if li > 0 { 0 } else { 1 })),
                (_, Fw::Walk(..)) => Some((rj, if -lj > 0 { 0 } else { 1 })),
                _ => None,
            };
            let (na, nbn) = (node_of[net.off[ri] + vi] as usize, node_of[net.off[rj] + vj] as usize);
            let cr = if na != nbn { connector.get(&(na.min(nbn), na.max(nbn))).copied() } else { None };
            let (hwi, hwj) = (net.has(ri, HAS_WALK), net.has(rj, HAS_WALK));
            if let Some(cr) = cr {
                if hwi && hwj && !pavement {
                    if let Some((mut corner, ci, cj)) = across(k, net, &bi, &bj, cr, x, walk_w, fi, fj) {
                        corner.footway = footway;
                        let id = out.corners.len();
                        out.corners.push(corner);
                        out.cuts.push((ri, li, pi, ui, ci));
                        out.cuts.push((rj, -lj, pj, uj, cj));
                        side_t.insert((i, true), SideT { t: ci, corner: id, dec: true, s0: None, tref: ci, bulb: 0.0 });
                        side_t.insert((j, false), SideT { t: cj, corner: id, dec: false, s0: None, tref: cj, bulb: 0.0 });
                        let cp = net.pts(cr);
                        let argmin = |q: P2| {      // (np.argmin: the first minimum, a NaN first)
                            let (mut best, mut bd) = (0usize, f64::INFINITY);
                            for (t, p) in cp.iter().enumerate() {
                                let d = nhyp(sub(xy(p), q));
                                if d.is_nan() {
                                    return t;
                                }
                                if t == 0 || d < bd {
                                    best = t;
                                    bd = d;
                                }
                            }
                            best
                        };
                        let (ki, kj) = (argmin(pi), argmin(pj));
                        let uc = unit(sub(pj, pi));
                        out.cuts.push((cr, if kj > ki { -1 } else { 1 }, pi, uc, mhyp(sub(pj, pi))));
                        continue;
                    }
                }
            }
            if !(hwi && hwj) || !(k.corner_lo < theta && theta < k.corner_hi) {
                continue;
            }
            let (ni, nj) = (left(ui), left(uj));
            let (wi, wj) = (net.width[ri] / 2.0, net.width[rj] / 2.0);
            let acute = theta < k.acute_deg;
            let pi_ = if net.has(ri, HAS_PARKING) && !acute { parking_m } else { 0.0 };
            let pj_ = if net.has(rj, HAS_PARKING) && !acute { parking_m } else { 0.0 };
            let (park_i, park_j) = (pi_ != 0.0, pj_ != 0.0);
            let (di, dj) = (wi - pi_, wj - pj_);
            let (bbi, bbj) = (if park_i { k.bulb_m } else { 0.0 }, if park_j { k.bulb_m } else { 0.0 });
            for &rscale in &k.radius_tries {
                let mut radius = net.rad[ri].max(net.rad[rj]) * rscale;
                if pavement {
                    let r0 = radius / rscale;
                    let pr = if net.has(ri, SIDEWALKS) && net.has(rj, SIDEWALKS) {
                        r0
                    } else if net.has(ri, SERVICE) || net.has(rj, SERVICE) {
                        k.service_r
                    } else {
                        let o = if net.has(ri, MAJOR) || net.has(rj, MAJOR) { k.pave_radius_major } else { k.pave_radius };
                        if o > r0 { o } else { r0 }
                    };
                    radius = pr * rscale;
                }
                if acute {
                    // min(radius, max(ACUTE_R_MIN, ACUTE_T_M * tan(radians(theta / 2)))): Python's first of equals
                    let y = k.acute_t_m * (theta / 2.0 * DEG2RAD).tan();
                    let b = if y > k.acute_r_min { y } else { k.acute_r_min };
                    if b < radius {
                        radius = b;
                    }
                }
                let z = match line_x(add(pi, mul(ni, di + radius)), ui, sub(pj, mul(nj, dj + radius)), uj) {
                    Some(z) => z,
                    None => continue,
                };
                let (tti, ttj) = (sub(z, mul(ni, radius)), add(z, mul(nj, radius)));
                let (ti, tj) = (dot(sub(tti, pi), ui), dot(sub(ttj, pj), uj));
                if ti <= 0.0 || tj <= 0.0 || ti > 40.0 || tj > 40.0 {
                    continue;
                }
                let ci = ti + if park_i { bbi + k.taper_m } else { 0.0 };
                let cj = tj + if park_j { bbj + k.taper_m } else { 0.0 };
                let (pts_i, pts_j) = (net.pts(ri), net.pts(rj));
                if branch_len(pts_i, vi, si) < ci + 1.0 || branch_len(pts_j, vj, sj) < cj + 1.0 {
                    continue;
                }
                let wki = walk_width(net, fi, ri, walk_w);
                let wkj = walk_width(net, fj, rj, walk_w);
                let o = match line_x(add(pi, mul(ni, wi + wki)), ui, sub(pj, mul(nj, wj + wkj)), uj) {
                    Some(o) => o,
                    None => continue,
                };
                let a0 = (tti[1] - z[1]).atan2(tti[0] - z[0]);
                let a1 = (ttj[1] - z[1]).atan2(ttj[0] - z[0]);
                let da = pmod(a1 - a0 + std::f64::consts::PI, TWO_PI) - std::f64::consts::PI;
                let kk = 3usize.max((da.abs() * radius / 0.75) as usize + 1);
                let arc2: Vec<P2> = linspace(0.0, 1.0, kk).iter().map(|&s| {
                    let ang = a0 + da * s;
                    [z[0] + radius * ang.cos(), z[1] + radius * ang.sin()]
                }).collect();
                let mut curb2: Vec<P2> = Vec::with_capacity(kk + 4);
                if park_i {
                    curb2.push(add(add(pi, mul(ui, ci)), mul(ni, wi)));
                    curb2.push(add(add(pi, mul(ui, ti + bbi)), mul(ni, di)));
                }
                let npre = curb2.len();
                curb2.extend_from_slice(&arc2);
                if park_j {
                    curb2.push(sub(add(pj, mul(uj, tj + bbj)), mul(nj, dj)));
                    curb2.push(sub(add(pj, mul(uj, cj)), mul(nj, wj)));
                }
                let zi0 = z_along(pts_i, vi, si, ti);
                let zj0 = z_along(pts_j, vj, sj, tj);
                let mut zs: Vec<f64> = Vec::with_capacity(curb2.len());
                if park_i {
                    zs.push(z_along(pts_i, vi, si, ci));
                    zs.push(z_along(pts_i, vi, si, ti + bbi));
                }
                zs.extend(linspace(zi0, zj0, kk));
                if park_j {
                    zs.push(z_along(pts_j, vj, sj, tj + bbj));
                    zs.push(z_along(pts_j, vj, sj, cj));
                }
                let mut nrm = normals(&curb2);
                let mid = npre + kk / 2;
                if dot(nrm[mid], sub(z, curb2[mid])) < 0.0 {
                    for v in nrm.iter_mut() {
                        *v = neg(*v);
                    }
                }
                let zo = 0.5 * (zi0 + zj0);
                let zci = z_along(pts_i, vi, si, ci);
                let zcj = z_along(pts_j, vj, sj, cj);
                let e0 = sub(add(pj, mul(uj, cj)), mul(nj, wj + wkj));
                let e2 = add(add(pi, mul(ui, ci)), mul(ni, wi + wki));
                let mut outer: Vec<P3> = vec![[e0[0], e0[1], zcj], [o[0], o[1], zo], [e2[0], e2[1], zci]];
                if footway.is_some() && !corner_is_simple(&curb2, &nrm, &outer, k.curb_w) {
                    continue;
                }
                if theta < k.nose_deg && footway.is_none() && !pavement {
                    let sn0 = k.nose_gap_m / (2.0 * (theta * DEG2RAD / 2.0).sin());
                    let sn = if sn0 < k.nose_max_m { sn0 } else { k.nose_max_m };
                    if dot(sub(o, pj), uj) + sn > cj + 0.5 && dot(sub(o, pi), ui) + sn > ci + 0.5 {
                        let (pa, pb) = (add(o, mul(uj, sn)), add(o, mul(ui, sn)));
                        let probe = [pa, pb, [(pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0],
                                     [(o[0] + pa[0]) / 2.0, (o[1] + pa[1]) / 2.0],
                                     [(o[0] + pb[0]) / 2.0, (o[1] + pb[1]) / 2.0]];
                        if !net.on_carriageway(&probe, x, 60.0 + sn, 0.3, &[], &[], None) {
                            outer = vec![outer[0], [pa[0], pa[1], zcj], [pb[0], pb[1], zci], outer[2]];
                        }
                    }
                }
                let cc = line_x(add(pi, mul(ni, di)), ui, sub(pj, mul(nj, dj)), uj);
                let fill = cc.map(|c| {
                    let mut f = vec![[c[0], c[1], zo]];
                    f.extend(arc2.iter().zip(&zs[npre..npre + kk]).map(|(q, zz)| [q[0], q[1], *zz]));
                    f
                });
                if let Some(c) = cc {
                    if mhyp(sub(c, x)) > 20.0 + k.merge_m {
                        continue;
                    }
                }
                let mut probe: Vec<P2> = curb2.iter().zip(&nrm).map(|(c, nv)| add(*c, mul(*nv, 0.8))).collect();
                probe.extend(curb2.iter().zip(&nrm).map(|(c, nv)| add(*c, mul(*nv, 2.0))));
                // own = {ri: di, rj: dj} by road, then by fid: the later of equal keys
                let half = [(net.fid[ri], di), (net.fid[rj], dj)];
                let mut hit = net.on_carriageway(&probe, x, 60.0, 0.3, &half, &[], None);
                if !hit {
                    let (q0, q2) = (outer[0], outer[2]);
                    let op = [[(q0[0] + o[0]) / 2.0, (q0[1] + o[1]) / 2.0], [(q2[0] + o[0]) / 2.0, (q2[1] + o[1]) / 2.0], o];
                    hit = net.on_carriageway(&op, x, 60.0, 0.3, &[], &[net.fid[ri], net.fid[rj]], None);
                }
                if hit {
                    continue;
                }
                let arc: Vec<P3> = curb2.iter().zip(&zs).map(|(c, zz)| [c[0], c[1], *zz]).collect();
                let mut corner = Corner { arc, inward: nrm, outer, fill, footway, ..Default::default() };
                if pavement {
                    corner.sides = Some((ri, li, rj, -lj));
                    corner.edge = Some((pi, ui, ci, pj, uj, cj));
                }
                let id = out.corners.len();
                out.corners.push(corner);
                out.cuts.push((ri, li, pi, ui, ci));
                out.cuts.push((rj, -lj, pj, uj, cj));
                let seg = arclen(&curb2);
                side_t.insert((i, true), SideT { t: ti, corner: id, dec: true,
                                                 s0: if park_i { Some(seg[npre - 1]) } else { None },
                                                 tref: ti + bbi, bulb: bbi });
                side_t.insert((j, false), SideT { t: tj, corner: id, dec: false,
                                                  s0: if park_j { Some(seg[npre + kk - 1]) } else { None },
                                                  tref: tj, bulb: bbj });
                break;
            }
        }
        if pavement {
            continue;
        }
        for i in 0..nb {
            let (sl, sr) = match (side_t.get(&(i, true)), side_t.get(&(i, false))) {
                (Some(a), Some(b)) => (*a, *b),
                _ => continue,
            };
            let b = branches[i];
            let (ri, vi, si, ui, li, pi) = (b.ri, b.vi, b.step, b.u, b.step, b.p);
            if net.has(ri, ROUNDABOUT) {
                continue;
            }
            let pts = net.pts(ri);
            let w = net.width[ri] / 2.0;
            let dw = w - if net.has(ri, HAS_PARKING) { parking_m } else { 0.0 };
            let tmax = if sr.t > sl.t { sr.t } else { sl.t };
            let t = tmax + k.cw_gap + k.cw_w / 2.0;
            let c = add(pi, mul(ui, t));
            let z = z_along(pts, vi, si, t);
            let nl = left(ui);
            let (a, bb) = (add(c, mul(nl, dw)), sub(c, mul(nl, dw)));
            if branch_len(pts, vi, si) < t + k.cw_w {
                continue;
            }
            let ends = [add(a, mul(nl, 0.5)), sub(bb, mul(nl, 0.5))];
            if net.on_carriageway(&ends, x, 60.0, 0.3, &[], &[], Some(ri))
                || near_c(c, ri).into_iter().any(|r| { let p = net.pts(r); crosses(a, bb, p.len(), |q| xy(&p[q])) }) {
                continue;
            }
            let hl = 0.5 * nhyp(sub(a, bb));
            let cw_reach = hl + cw_half + 2.0;
            let (gi, gj) = ((c[0] / k.cw_cell_m).floor() as i64, (c[1] / k.cw_cell_m).floor() as i64);
            let kc = (cw_reach / k.cw_cell_m).ceil() as i64;
            let mut touch = false;
            'cells: for di in -kc..=kc {
                for dj in -kc..=kc {
                    if let Some(v) = cw_grid.get(&(gi + di, gj + dj)) {
                        for &q in v {
                            let (qa, qb) = out.crosswalks[q];
                            let (qa, qb) = ([qa[0], qa[1]], [qb[0], qb[1]]);
                            let d = [seg_dist(a, qa, qb), seg_dist(bb, qa, qb), seg_dist(c, qa, qb)];
                            let mn = d[1..].iter().fold(d[0], |m, &v| if v < m { v } else { m });  // (builtin min)
                            if mn < 2.0 {
                                touch = true;
                                break 'cells;
                            }
                        }
                    }
                }
            }
            if touch {
                continue;
            }
            out.crosswalks.push(([a[0], a[1], z], [bb[0], bb[1], z]));
            cw_grid.entry((gi, gj)).or_default().push(out.crosswalks.len() - 1);
            if hl > cw_half {
                cw_half = hl;
            }
            let ts = t + k.cw_w / 2.0 + k.stop_gap + k.stop_w / 2.0;
            if branch_len(pts, vi, si) > ts + 2.0 {
                let cs = add(pi, mul(ui, ts));
                let zs = z_along(pts, vi, si, ts);
                let (sa, sb) = (add(cs, mul(nl, 0.2)), add(cs, mul(nl, dw - 0.3)));
                out.stops.push(([sa[0], sa[1], zs], [sb[0], sb[1], zs]));
            }
            let (t0, t1) = (t - k.cw_w / 2.0, t + k.cw_w / 2.0);
            for (st, side) in [(sl, li), (sr, -li)] {
                if st.bulb > 0.0 && st.s0.is_some() && t1 <= st.t + st.bulb {
                    let s0 = st.s0.unwrap();
                    let (lo, hi) = if st.dec { (s0 + (st.tref - t1), s0 + (st.tref - t0)) }
                                   else { (s0 + (t0 - st.tref), s0 + (t1 - st.tref)) };
                    out.corners[st.corner].ramps.push((lo, hi));
                } else {
                    out.ramps.push((ri, side, pi, ui, t0, t1));
                }
            }
            out.line_cuts.push((ri, pi, ui, t1));
        }
    }
    out
}

fn arr1<'py>(py: Python<'py>, v: P2) -> Bound<'py, PyAny> {
    v.to_vec().into_pyarray(py).into_any()
}

fn arr3<'py>(py: Python<'py>, v: P3) -> Bound<'py, PyAny> {
    v.to_vec().into_pyarray(py).into_any()
}

fn rows3<'py>(py: Python<'py>, v: &[P3]) -> Bound<'py, PyAny> {
    let flat: Vec<f64> = v.iter().flat_map(|p| p.iter().copied()).collect();
    Array2::from_shape_vec((v.len(), 3), flat).unwrap().into_pyarray(py).into_any()
}

fn rows2<'py>(py: Python<'py>, v: &[P2]) -> Bound<'py, PyAny> {
    let flat: Vec<f64> = v.iter().flat_map(|p| p.iter().copied()).collect();
    Array2::from_shape_vec((v.len(), 2), flat).unwrap().into_pyarray(py).into_any()
}

/// junctions.find (server/junctions.py glue): roads' points pts (N, 3) with offsets off (n + 1); per road its fid
/// (fids, the Python objects; fid_rank: their sorted order), width, curb-return radius (RADII by class), sidewalk
/// width (spec.walk), flags (1 has_walk, 2 has_parking, 4 roundabout, 8 has_sidewalks, 16 service, 32 a major
/// class), its two sides' walks (fw_state 0 no street record / 1 none or set back / 2 a walk; fw_width, fw_rank:
/// (width, surface) order; walks: the 2 n objects); consts in junctions._consts' order. Returns find's 6-tuple.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn junctions_find<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                           fids: Bound<'py, PyList>, fid_rank: PyReadonlyArray1<'py, i64>,
                           width: PyReadonlyArray1<'py, f64>, rad: PyReadonlyArray1<'py, f64>,
                           walk: PyReadonlyArray1<'py, f64>, flags: PyReadonlyArray1<'py, u8>,
                           fw_state: PyReadonlyArray2<'py, u8>, fw_width: PyReadonlyArray2<'py, f64>,
                           fw_rank: PyReadonlyArray2<'py, i64>, walks: Bound<'py, PyList>, walk_w: Option<f64>,
                           parking_m: f64, pavement: bool, bounds: Option<(f64, f64, f64, f64)>, consts: Vec<f64>,
                           radius_tries: Vec<f64>, cw_w: f64, stop_w: f64)
                           -> PyResult<Bound<'py, PyTuple>> {
    let pa = pts.as_array();
    let offa = off.as_array();
    let n = offa.len().saturating_sub(1);
    if pa.ncols() < 3 || fids.len() != n || fid_rank.as_array().len() != n || width.as_array().len() != n || rad.as_array().len() != n
        || walk.as_array().len() != n || flags.as_array().len() != n || fw_state.as_array().nrows() != n || walks.len() != 2 * n
        || consts.len() != 23 || (n > 0 && offa[n] as usize != pa.nrows()) {
        return Err(PyValueError::new_err("junctions_find: mismatched road arrays"));
    }
    let p: Vec<P3> = (0..pa.nrows()).map(|i| [pa[[i, 0]], pa[[i, 1]], pa[[i, 2]]]).collect();
    let (fsa, fwa, fra) = (fw_state.as_array(), fw_width.as_array(), fw_rank.as_array());
    let fw: Vec<[Fw; 2]> = (0..n).map(|r| {
        let f = |s: usize| match fsa[[r, s]] { 0 => Fw::None, 1 => Fw::Skip, _ => Fw::Walk(fwa[[r, s]], fra[[r, s]]) };
        [f(0), f(1)]
    }).collect();
    let offv: Vec<usize> = offa.iter().map(|&o| o as usize).collect();
    let boxes: Vec<[f64; 4]> = (0..n).map(|r| {
        let s = &p[offv[r]..offv[r + 1]];
        if s.is_empty() {
            return [f64::NAN; 4];
        }
        // (np.minimum / np.maximum reductions: a NaN wins)
        let mut b = [s[0][0], s[0][1], s[0][0], s[0][1]];
        for q in &s[1..] {
            for (c, v) in [(0usize, q[0]), (1, q[1])] {
                b[c] = if b[c].is_nan() || v.is_nan() { f64::NAN } else if v < b[c] { v } else { b[c] };
                b[c + 2] = if b[c + 2].is_nan() || v.is_nan() { f64::NAN } else if v > b[c + 2] { v } else { b[c + 2] };
            }
        }
        b
    }).collect();
    let net = Net {
        p: &p, off: offv, fid: fid_rank.as_array().to_vec(), width: width.as_array().to_vec(),
        rad: rad.as_array().to_vec(), walk: walk.as_array().to_vec(), flags: flags.as_array().to_vec(), fw, boxes,
    };
    if net.fid.iter().any(|&f| f < 0 || f as usize >= n.max(1)) {
        return Err(PyValueError::new_err("junctions_find: fid ranks out of range"));
    }
    let c = &consts;
    let k = K {
        radius: c[0], corner_lo: c[1], corner_hi: c[2], acute_deg: c[3], acute_t_m: c[4], acute_r_min: c[5],
        branch_m: c[6], bulb_m: c[7], taper_m: c[8], nose_deg: c[9], nose_gap_m: c[10], nose_max_m: c[11],
        cw_cell_m: c[12], cw_gap: c[13], cw_w, stop_gap: c[14], stop_w, node_tol: c[15], merge_m: c[16],
        connector_m: c[17], connector_widths: c[18], pave_radius: c[19], pave_radius_major: c[20], service_r: c[21],
        curb_w: c[22], radius_tries,
    };
    let bounds = bounds.map(|b| [b.0, b.1, b.2, b.3]);
    let o = py.detach(|| find_core(&k, &net, walk_w, parking_m, pavement, bounds));
    // the Python objects, in find's insertion orders
    let fid = |r: usize| fids.get_item(r);
    let corners = PyList::empty(py);
    for cn in &o.corners {
        let d = PyDict::new(py);
        d.set_item("arc", rows3(py, &cn.arc))?;
        d.set_item("inward", rows2(py, &cn.inward))?;
        let outer = PyList::empty(py);
        for q in &cn.outer {
            outer.append(PyTuple::new(py, q.iter())?)?;
        }
        d.set_item("outer", outer)?;
        match &cn.fill {
            Some(f) => d.set_item("fill", rows3(py, f))?,
            None => d.set_item("fill", py.None())?,
        }
        if let Some(fl) = &cn.fills {
            let l = PyList::empty(py);
            for f in fl {
                l.append(rows3(py, f))?;
            }
            d.set_item("fills", l)?;
        }
        let ramps = PyList::empty(py);
        for &(lo, hi) in &cn.ramps {
            ramps.append((lo, hi))?;
        }
        d.set_item("ramps", ramps)?;
        if let Some((r, s)) = cn.footway {
            d.set_item("footway", walks.get_item(2 * r + s)?)?;
        }
        if let Some((ri, li, rj, lj)) = cn.sides {
            d.set_item("road_sides", ((fid(ri)?, li), (fid(rj)?, lj)))?;
        }
        if let Some((pi, ui, ci, pj, uj, cj)) = cn.edge {
            d.set_item("edge_cuts", ((arr1(py, pi), arr1(py, ui), ci), (arr1(py, pj), arr1(py, uj), cj)))?;
        }
        corners.append(d)?;
    }
    let append = |d: &Bound<'py, PyDict>, key: Bound<'py, PyAny>, v: Bound<'py, PyAny>| -> PyResult<()> {
        match d.get_item(&key)? {
            Some(l) => l.cast::<PyList>()?.append(v),
            None => d.set_item(key, PyList::new(py, [v])?),
        }
    };
    let cuts = PyDict::new(py);
    for &(r, side, pp, u, t) in &o.cuts {
        append(&cuts, (fid(r)?, side).into_pyobject(py)?.into_any(),
               (arr1(py, pp), arr1(py, u), t).into_pyobject(py)?.into_any())?;
    }
    let cws = PyList::empty(py);
    for &(a, b) in &o.crosswalks {
        let d = PyDict::new(py);
        d.set_item("a", arr3(py, a))?;
        d.set_item("b", arr3(py, b))?;
        d.set_item("w", cw_w)?;
        cws.append(d)?;
    }
    for &(a, b) in &o.stops {
        let d = PyDict::new(py);
        d.set_item("a", arr3(py, a))?;
        d.set_item("b", arr3(py, b))?;
        d.set_item("w", stop_w)?;
        d.set_item("stop", true)?;
        cws.append(d)?;
    }
    let ramps = PyDict::new(py);
    for &(r, side, pp, u, t0, t1) in &o.ramps {
        append(&ramps, (fid(r)?, side).into_pyobject(py)?.into_any(),
               (arr1(py, pp), arr1(py, u), t0, t1).into_pyobject(py)?.into_any())?;
    }
    let line_cuts = PyDict::new(py);
    for &(r, pp, u, t1) in &o.line_cuts {
        append(&line_cuts, fid(r)?, (arr1(py, pp), arr1(py, u), t1).into_pyobject(py)?.into_any())?;
    }
    let internal = PySet::empty(py)?;
    for &r in &o.internal {
        internal.add(fid(r)?)?;
    }
    PyTuple::new(py, [corners.into_any(), cuts.into_any(), cws.into_any(), ramps.into_any(), line_cuts.into_any(),
                      internal.into_any()])
}
