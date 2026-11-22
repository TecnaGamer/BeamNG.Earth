//! lane_surface (earthfast 542, road surface v3 phase S2, BNE_ROADSURF_V3=surface): the GROUND road surface of the
//! motorway / trunk roads round one road tile as ONE continuous mesh (.claude/work/road-surface-v3-plan-2026-11-19.md
//! §2-3, road-surface-architecture-research-2026-11-19.md §2.3-2.4, §2.7), from S1's lane graph (lanes.rs) and the
//! roads' ground lines (server/roadsurf3.py gathers both from the map cells round the tile, never the tile window).
//!
//! PIECES (one per lane event: a diverge, merge, add or drop; lanes.rs's rows: the main line's point and the ordered
//! lane boundaries t_0 >= t_1 >= ... every 2 m) are lane STRIPS sharing their boundary rows: lanes can never overlap or
//! gap, a taper is the smoothstep width S1 gives (a lane under 1 mm wide is one vertex: triangles fan out of a point,
//! no step), the painted gore is a strip between the through and branch lanes, a shoulder strip on each side. Normals
//! from the row points (central differences: the mapped line's corners eased over a row).
//!
//! ROADS (each v3-class ground road part, its line as the road tile draws it) are strips of their own: rows at every
//! vertex and every ROAD_STEP from the part's start (canonical: the same rows in every tile), the edges at +/- the half
//! width (a vertex's normal mitred, a row inside a segment the segment's).
//!
//! ONE SURFACE, by construction (in the pieces' given order, the caller's priority: forks first, then node id):
//! - a piece's rows whose main point lies inside an earlier piece's outline, or on a structure (a bridge / tunnel of
//!   the same direction), are trimmed (the longest run of rows outside kept; under 3 rows: the piece is dropped);
//! - a road is cut where it crosses a piece's end row (carried END_EXT m past the piece's edges; a road of the piece's
//!   direction) and where it enters the piece's final outline widened by ROAD_CLEAR x its half width; its stretches
//!   inside left out; the cut is a row of the road's own (its point, the segment's normal); two roads meeting end to
//!   end share a mitred row;
//! - each piece END is welded to what goes on: the cut rows of the roads crossing it (each road's edges to the strips
//!   it holds there; strips no road holds narrow to its nearest edge: the lane fans out of a point), an earlier
//!   piece's end row (each boundary to its nearest point on that row), or a structure's end (its edges, recorded: a
//!   deck's gap is S3's). The end row becomes the target exactly (a shared row: identical points), the rows before it
//!   eased toward it by a quintic over BLEND = clamp(BLEND_K x the largest move, BLEND_MIN, BLEND_MAX) m (at most 45 %
//!   of the piece), so the width changes no faster than a taper.
//!
//! MARKINGS from the boundaries (lines: kind, points, the main station at the start for a dash's phase): an edge line
//! on each shoulder boundary (the left one its own kind: the Americas' yellow), lane lines between through lanes
//! (through-lane dashes go on past an exit), exit / auxiliary dashes between a through lane and a branch / added /
//! dropped lane where it is at least LINE_W_MIN of a lane wide, the gore's channelizing lines where it is wider than
//! GORE_MIN, and its stripes every STRIPE_STEP m: chevrons (CHEVRON 1: a V from both sides to the middle, pointing
//! upstream) or hatching (0: one diagonal each).
//!
//! Output (flat arrays, roadsurf3.py reshapes them): the mesh's vertices (x, y), triangles, each triangle's owner
//! (piece index, or -1 - road index) and strip kind, each vertex's (u across the owner 0 left .. 1 right, v m along);
//! the lines; each kept piece's outline and its two removal outlines (lateral margins CUT_M / CUT_WIDE_M: the road
//! tile's own lines and overlays inside them are left out); a JSON status per piece. Triangles wholly outside the box
//! (the tile and a margin) are left out; the rest is the same in every tile that builds the same pieces and roads.
//! No FMA, no libm, no hash maps: sqrt (correctly rounded) and + - * / only, in a fixed order.
mod gore;
pub mod grade;
mod junction;
pub mod paint;
mod partition;
pub mod deck;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use super::forks::quintic;

// strip kinds (roadsurf3.STRIP)
pub const K_SH: i64 = 0;
pub const K_TH: i64 = 1;
pub const K_GO: i64 = 2;
pub const K_BR: i64 = 3;
pub const K_AD: i64 = 4;

// line kinds (roadsurf3.LINE)
pub const L_EDGE: i64 = 0;
pub const L_EDGE_LEFT: i64 = 1;
pub const L_LANE: i64 = 2;
pub const L_EXIT: i64 = 3;
pub const L_CHANNEL: i64 = 4;
pub const L_STRIPE: i64 = 5;

// params (roadsurf3.SURF_PARAMS, in this order)
const P_BLEND_MIN: usize = 0;
const P_BLEND_K: usize = 1;
const P_BLEND_MAX: usize = 2;
const P_LINE_W_MIN: usize = 3;
const P_GORE_MIN: usize = 4;
const P_STRIPE_STEP: usize = 5;
const P_CHEVRON: usize = 6;
const P_ROAD_STEP: usize = 7;
const P_SNAP: usize = 8;
const P_DECK_JOIN: usize = 9;
const P_CROSS_COS: usize = 10;
const P_STRUCT_COS: usize = 11;
const P_CUT_M: usize = 12;
const P_CUT_WIDE_M: usize = 13;
const P_END_EXT: usize = 14;
const P_ROAD_CLEAR: usize = 15;
const NPARAMS: usize = 16;

const COLLAPSE_M: f64 = 0.001;      // m: a strip this narrow at a row is one vertex there
const ABUT_M: f64 = 4.0;            // m: a piece trimmed by an earlier one welds onto its end row when this near it

type P2 = [f64; 2];

fn snapped(p: P2) -> P2 { p.map(|v| (v * 1000.0).round() / 1000.0) }
fn sub(a: P2, b: P2) -> P2 { [a[0] - b[0], a[1] - b[1]] }
fn add(a: P2, b: P2) -> P2 { [a[0] + b[0], a[1] + b[1]] }
fn mul(a: P2, k: f64) -> P2 { [a[0] * k, a[1] * k] }
fn dot(a: P2, b: P2) -> f64 { a[0] * b[0] + a[1] * b[1] }
fn cross(a: P2, b: P2) -> f64 { a[0] * b[1] - a[1] * b[0] }
fn norm(a: P2) -> f64 { (a[0] * a[0] + a[1] * a[1]).sqrt() }
fn unit(a: P2) -> P2 {
    let n = norm(a);
    if n > 1e-12 { [a[0] / n, a[1] / n] } else { [1.0, 0.0] }
}
fn left(a: P2) -> P2 { [-a[1], a[0]] }
fn lerp(a: P2, b: P2, u: f64) -> P2 { [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u] }
fn absf(v: f64) -> f64 { if v < 0.0 { -v } else { v } }
fn minf(a: f64, b: f64) -> f64 { if a < b { a } else { b } }
fn maxf(a: f64, b: f64) -> f64 { if a > b { a } else { b } }
fn clampf(v: f64, lo: f64, hi: f64) -> f64 { if v < lo { lo } else if v > hi { hi } else { v } }

/// Point in polygon (even-odd; a point on an edge counts by the half-open rule, the same for every caller).
pub fn inside(poly: &[P2], q: P2) -> bool {
    let n = poly.len();
    if n < 3 { return false; }
    let mut c = false;
    let mut j = n - 1;
    for i in 0..n {
        let (a, b) = (poly[i], poly[j]);
        if (a[1] > q[1]) != (b[1] > q[1]) {
            let x = a[0] + (q[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1]);
            if q[0] < x { c = !c; }
        }
        j = i;
    }
    c
}

/// Segment p0-p1 against segment q0-q1: (u along p, v along q) of the crossing, or None (parallel or apart).
fn seg_cross(p0: P2, p1: P2, q0: P2, q1: P2) -> Option<(f64, f64)> {
    let r = sub(p1, p0);
    let s = sub(q1, q0);
    let den = cross(r, s);
    if absf(den) < 1e-12 { return None; }
    let qp = sub(q0, p0);
    let u = cross(qp, s) / den;
    let v = cross(qp, r) / den;
    if (0.0..=1.0).contains(&u) && (0.0..=1.0).contains(&v) { Some((u, v)) } else { None }
}

/// A polyline with its arc lengths.
struct Line { p: Vec<P2>, s: Vec<f64>, section: Vec<P2>, hw: f64, ends: [Option<P2>; 2], oneway: bool }

impl Line {
    fn new(p: Vec<P2>, hw: f64) -> Line {
        let mut s = vec![0.0; p.len()];
        for k in 1..p.len() { s[k] = s[k - 1] + norm(sub(p[k], p[k - 1])); }
        Line { p, s, section: Vec::new(), hw, ends: [None, None], oneway: false }
    }
    fn len(&self) -> f64 { *self.s.last().unwrap_or(&0.0) }
    /// The segment index holding station t (the last segment for t past the end).
    fn seg_at(&self, t: f64) -> usize {
        let n = self.p.len();
        let mut k = 0;
        while k + 2 < n && self.s[k + 1] <= t { k += 1; }
        k
    }
    fn dir(&self, k: usize) -> P2 { unit(sub(self.p[k + 1], self.p[k])) }
    /// The row at station t: (point, left unit normal scaled by the mitre at a vertex).
    fn row(&self, t: f64) -> (P2, P2) {
        let n = self.p.len();
        if !self.section.is_empty() {
            let i = self.s.partition_point(|&s| s < t);
            if i == 0 || i == n || self.s[i] == t {
                let j = i.min(n-1);
                return (self.p[j], self.section[j]);
            }
            let u = (t-self.s[i-1])/(self.s[i]-self.s[i-1]);
            return (lerp(self.p[i-1],self.p[i],u), lerp(self.section[i-1],self.section[i],u));
        }
        for v in 0..n {
            if self.s[v] == t {
                let p = self.p[v];
                if v == 0 { return (p, self.ends[0].unwrap_or_else(|| left(self.dir(0)))); }
                if v == n - 1 { return (p, self.ends[1].unwrap_or_else(|| left(self.dir(n - 2)))); }
                let (a, b) = (left(self.dir(v - 1)), left(self.dir(v)));
                let m = unit(add(a, b));
                let c = clampf(dot(m, b), 0.5, 1.0);
                return (p, mul(m, 1.0 / c));
            }
        }
        let k = self.seg_at(t);
        let l = self.s[k + 1] - self.s[k];
        let u = if l > 0.0 { (t - self.s[k]) / l } else { 0.0 };
        (lerp(self.p[k], self.p[k + 1], u), left(self.dir(k)))
    }
    /// Distance from q to the line and the direction of its nearest segment.
    fn near(&self, q: P2) -> (f64, P2) {
        let mut best = (f64::INFINITY, [1.0, 0.0]);
        for k in 0..self.p.len().saturating_sub(1) {
            let (a, b) = (self.p[k], self.p[k + 1]);
            let ab = sub(b, a);
            let l2 = dot(ab, ab);
            let u = if l2 > 0.0 { clampf(dot(sub(q, a), ab) / l2, 0.0, 1.0) } else { 0.0 };
            let d = norm(sub(q, lerp(a, b, u)));
            if d < best.0 { best = (d, unit(ab)); }
        }
        best
    }
}

struct Piece {
    s: Vec<f64>,
    p: Vec<P2>,
    t: Vec<Vec<f64>>,       // per row: boundaries (left first), shoulders included
    kinds: Vec<i64>,        // per strip
    lane_w: f64,
}

impl Piece {
    fn nb(&self) -> usize { self.kinds.len() + 1 }
    /// Left unit normals from the row points (central differences).
    fn normals(&self, a: usize, b: usize) -> Vec<P2> {
        let mut out = Vec::with_capacity(b + 1 - a);
        for i in a..=b {
            let (i0, i1) = (if i > a { i - 1 } else { i }, if i < b { i + 1 } else { i });
            out.push(left(unit(sub(self.p[i1], self.p[i0]))));
        }
        out
    }
}

/// A kept piece: its rows a..=b, the boundary points per row (after the welds), status.
struct Kept {
    idx: usize,
    a: usize,
    b: usize,
    pts: Vec<Vec<P2>>,      // [row - a][boundary]
    raw_outline: Vec<P2>,
    flags: Vec<String>,
}

impl Kept {
    fn end_row(&self, hi: bool) -> &Vec<P2> { if hi { self.pts.last().unwrap() } else { &self.pts[0] } }
}

fn outline(pts: &[Vec<P2>]) -> Vec<P2> {
    let mut o: Vec<P2> = pts.iter().map(|r| r[0]).collect();
    for r in pts.iter().rev() { o.push(*r.last().unwrap()); }
    o
}

/// The outline pushed out by m on both sides (the end rows as they are): the removal outline.
fn widened(pts: &[Vec<P2>], m: f64) -> Vec<P2> {
    let n = pts.len();
    let mut l = Vec::with_capacity(n);
    let mut r = Vec::with_capacity(n);
    for i in 0..n {
        let (i0, i1) = (if i > 0 { i - 1 } else { i }, if i + 1 < n { i + 1 } else { i });
        let (a0, a1) = (pts[i0][0], pts[i1][0]);
        let (b0, b1) = (*pts[i0].last().unwrap(), *pts[i1].last().unwrap());
        let (mut nl, mut nr) = (left(unit(sub(a1, a0))), left(unit(sub(b1, b0))));
        if i == 0 || i + 1 == n {       // (an end row carried on along itself: the outline ends exactly on it)
            let ax = unit(sub(pts[i][0], *pts[i].last().unwrap()));
            nl = ax;
            nr = ax;
        }
        l.push(add(pts[i][0], mul(nl, m)));
        r.push(sub(*pts[i].last().unwrap(), mul(nr, m)));
    }
    let mut o = l;
    for q in r.into_iter().rev() { o.push(q); }
    o
}

/// A target at a piece end: one road (or structure) holding the strips between its edges there.
struct Hold { left: P2, right: P2, centre: f64 }

/// The targets of a piece's end row (raw points `row`, its raw lateral offsets `tau`) from the holds there: one point
/// per boundary, or None (a free end). The row's RUNS are its strips between gores at least RUN_GORE_M wide (a gore
/// narrower joins its sides: the trunk at a diverge's start holds the lanes on both sides of it); each run of at least
/// RUN_MIN_M takes the nearest hold (the nearest pair first, each hold once): its outer boundaries go to the hold's
/// edges, its inner ones move with them (the move interpolated by t). Between two runs' targets (a gore) the move is
/// interpolated; a run under RUN_MIN_M wide collapses onto the nearest target (a lane fanning out of a point); a run
/// with no hold left keeps its place.
fn targets_from_holds(row: &[P2], tau: &[f64], kinds: &[i64], holds: &[(Hold, f64, f64)]) -> Option<Vec<P2>> {
    const RUN_GORE_M: f64 = 0.5;
    const RUN_MIN_M: f64 = 0.5;
    const HOLD_REACH_M: f64 = 20.0;     // (a road further from a run's middle is another road: a loop ramp crossing)
    if holds.is_empty() { return None; }
    let nb = row.len();
    let ns = nb - 1;
    let w = |k: usize| tau[k] - tau[k + 1];
    // runs [k0, k1) of strips split at the wide gores (the gores themselves in none)
    let mut runs: Vec<(usize, usize)> = Vec::new();
    let mut k = 0;
    while k < ns {
        if kinds[k] == K_GO && w(k) >= RUN_GORE_M { k += 1; continue; }
        let k0 = k;
        while k < ns && !(kinds[k] == K_GO && w(k) >= RUN_GORE_M) { k += 1; }
        runs.push((k0, k));
    }
    let live: Vec<bool> = runs.iter().map(|&(a, b)| tau[a] - tau[b] >= RUN_MIN_M).collect();
    // the nearest (run, hold) pairs first, each once
    let mut pairs: Vec<(f64, usize, usize)> = Vec::new();
    for (ri, &(a, b)) in runs.iter().enumerate() {
        if !live[ri] { continue; }
        let c = 0.5 * (tau[a] + tau[b]);
        for (h, (hold, _tl, _tr)) in holds.iter().enumerate() {
            let d = absf(c - hold.centre);
            if d <= HOLD_REACH_M { pairs.push((d, ri, h)); }
        }
    }
    pairs.sort_by(|x, y| x.0.partial_cmp(&y.0).unwrap().then(x.1.cmp(&y.1)).then(x.2.cmp(&y.2)));
    let mut run_hold: Vec<i64> = vec![-1; runs.len()];
    let mut used = vec![false; holds.len()];
    for (_d, ri, h) in pairs {
        if run_hold[ri] >= 0 || used[h] { continue; }
        run_hold[ri] = h as i64;
        used[h] = true;
    }
    let mut tgt: Vec<Option<P2>> = vec![None; nb];
    let mut keep: Vec<bool> = vec![false; nb];       // a live run with no hold: its boundaries stay
    for (ri, &(a, b)) in runs.iter().enumerate() {
        if run_hold[ri] >= 0 {
            let hold = &holds[run_hold[ri] as usize].0;
            tgt[a] = Some(hold.left);
            tgt[b] = Some(hold.right);
        } else if live[ri] {
            for kk in a..=b { keep[kk] = true; }
        }
    }
    if tgt.iter().all(|t| t.is_none()) { return None; }
    // the rest: between two known moves (a target's, a kept boundary's 0), interpolated by t; outside them all, onto
    // the nearest target (collapsed)
    let delta: Vec<Option<P2>> = (0..nb).map(|k| match tgt[k] {
        Some(x) => Some(sub(x, row[k])),
        None => if keep[k] { Some([0.0, 0.0]) } else { None },
    }).collect();
    let first = (0..nb).find(|&k| tgt[k].is_some()).unwrap();
    let last = (0..nb).rev().find(|&k| tgt[k].is_some()).unwrap();
    let mut out = vec![[0.0, 0.0]; nb];
    for k in 0..nb {
        if let Some(d) = delta[k] { out[k] = add(row[k], d); continue; }
        let lo = (0..k).rev().find(|&j| delta[j].is_some());
        let hi = (k + 1..nb).find(|&j| delta[j].is_some());
        match (lo, hi) {
            (Some(lo), Some(hi)) => {
                let span = tau[lo] - tau[hi];
                let u = if absf(span) > 1e-9 { (tau[lo] - tau[k]) / span } else { 0.5 };
                out[k] = add(row[k], lerp(delta[lo].unwrap(), delta[hi].unwrap(), u));
            }
            (None, _) => { out[k] = if k < first { tgt[first].unwrap() } else { row[k] }; }
            (_, None) => { out[k] = if k > last { tgt[last].unwrap() } else { row[k] }; }
        }
    }
    Some(out)
}

/// The nearest point on polyline `poly` to q (and whether it is within snap of a vertex: then that vertex).
fn onto_row(poly: &[P2], q: P2, snap: f64) -> P2 {
    for v in poly { if norm(sub(*v, q)) <= snap { return *v; } }
    let mut best = (f64::INFINITY, poly[0]);
    for k in 0..poly.len().saturating_sub(1) {
        let (a, b) = (poly[k], poly[k + 1]);
        let ab = sub(b, a);
        let l2 = dot(ab, ab);
        let u = if l2 > 0.0 { clampf(dot(sub(q, a), ab) / l2, 0.0, 1.0) } else { 0.0 };
        let m = lerp(a, b, u);
        let d = norm(sub(q, m));
        if d < best.0 { best = (d, m); }
    }
    best.1
}

struct Mesh { v: Vec<P2>, uv: Vec<P2>, t: Vec<[i64; 3]>, owner: Vec<i64>, kind: Vec<i64> }

impl Mesh {
    fn quad(&mut self, q: [usize; 4], owner: i64, kind: i64, boxm: &[f64; 4]) {
        // q: a0 (row i, left), b0 (row i, right), b1 (row i+1, right), a1 (row i+1, left)
        let p = [self.v[q[0]], self.v[q[1]], self.v[q[2]], self.v[q[3]]];
        let (mut x0, mut y0, mut x1, mut y1) = (p[0][0], p[0][1], p[0][0], p[0][1]);
        for v in &p[1..] { x0 = minf(x0, v[0]); y0 = minf(y0, v[1]); x1 = maxf(x1, v[0]); y1 = maxf(y1, v[1]); }
        if x1 < boxm[0] || x0 > boxm[2] || y1 < boxm[1] || y0 > boxm[3] { return; }
        // the shorter diagonal (a tie: a0-b1)
        let d1 = norm(sub(p[0], p[2]));
        let d2 = norm(sub(p[1], p[3]));
        let tris: [[usize; 3]; 2] = if d2 < d1 { [[q[0], q[1], q[3]], [q[1], q[2], q[3]]] }
                                    else { [[q[0], q[1], q[2]], [q[0], q[2], q[3]]] };
        for tr in tris.iter() {
            let (a, b, c) = (self.v[tr[0]], self.v[tr[1]], self.v[tr[2]]);
            let area = cross(sub(b, a), sub(c, a));
            // (counter-clockwise from above as the quad runs; one turned over is a fold at a bend's inner edge:
            // left out, its neighbours cover it, as roadmesh._keep_up)
            if area < 1e-8 { continue; }
            self.t.push([tr[0] as i64, tr[1] as i64, tr[2] as i64]);
            self.owner.push(owner);
            self.kind.push(kind);
        }
    }
}

struct Lines { xy: Vec<P2>, off: Vec<i64>, kind: Vec<i64>, phase: Vec<f64>, piece: Vec<i64> }

impl Lines {
    fn push(&mut self, pts: &[P2], kind: i64, phase: f64, piece: usize) {
        if pts.len() < 2 { return; }
        self.xy.extend_from_slice(pts);
        self.off.push(self.xy.len() as i64);
        self.kind.push(kind);
        self.phase.push(phase);
        self.piece.push(piece as i64);
    }
}

/// The line kind on boundary k at a row (None: no line there), from the strips beside it and their widths; a gore
/// narrower than gore_min is passed over (its far side's strip is the neighbour).
fn line_kind(kinds: &[i64], w: &[f64], k: usize, lane_w: f64, prm: &[f64]) -> Option<i64> {
    let ns = kinds.len();
    if k == 0 || k == ns { return None; }
    let (a, b) = (kinds[k - 1], kinds[k]);
    let wl = |j: usize| w[j] >= prm[P_LINE_W_MIN] * lane_w;
    let gmin = prm[P_GORE_MIN];
    // a shoulder on one side: an edge line (the left shoulder's its own kind) where the lane beside is there
    // (always: a lane under it collapsed puts its edge where the next lane's is)
    if a == K_SH { return Some(L_EDGE_LEFT); }
    if b == K_SH { return Some(L_EDGE); }
    if a == K_GO || b == K_GO {
        let g = if a == K_GO { k - 1 } else { k };
        if w[g] >= gmin { return Some(L_CHANNEL); }
        // a gore too narrow: one line for both sides, drawn on its left boundary
        if b != K_GO { return None; }
        if g + 1 >= ns || k == 0 { return None; }
        let (l, r) = (k - 1, g + 1);
        return pair(kinds[l], kinds[r], wl(l), wl(r));
    }
    pair(a, b, wl(k - 1), wl(k))
}

fn pair(a: i64, b: i64, wa: bool, wb: bool) -> Option<i64> {
    if !(wa && wb) { return None; }
    if a == K_TH && b == K_TH { return Some(L_LANE); }
    if (a == K_BR && b == K_BR) || (a == K_AD && b == K_AD) { return Some(L_LANE); }
    if a == K_TH || b == K_TH { return Some(L_EXIT); }
    None
}

/// lane_surface (module doc). rows (R, 3): s, x, y of every piece's rows (row_off per piece); t: each row's boundaries
/// (t_off per piece, nb per row: its strips + 1, shoulders not included); kinds (k_off per piece); pmeta (P, 3): left
/// and right shoulder width, lane width; roads rxy / r_off / r_hw / r_oneway (a one-way road's points in its travel
/// direction: only one going the piece's way goes on from its end); structures sxy / s_off / s_hw; boxm: x0 y0 x1 y1.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
#[pyo3(signature = (rows, row_off, t, t_off, kinds, k_off, pmeta, rxy, r_off, r_hw, r_oneway, sxy, s_off, s_hw, boxm, params, outlines_only=false))]
pub fn lane_surface<'py>(py: Python<'py>, rows: PyReadonlyArray2<'py, f64>, row_off: Vec<i64>, t: PyReadonlyArray1<'py, f64>,
                         t_off: Vec<i64>, kinds: Vec<i64>, k_off: Vec<i64>, pmeta: PyReadonlyArray2<'py, f64>,
                         rxy: PyReadonlyArray2<'py, f64>, r_off: Vec<i64>, r_hw: Vec<f64>, r_oneway: Vec<bool>,
                         sxy: PyReadonlyArray2<'py, f64>, s_off: Vec<i64>, s_hw: Vec<f64>,
                         boxm: Vec<f64>, params: PyReadonlyArray1<'py, f64>, outlines_only: bool)
                         -> PyResult<((Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<i64>>,
                                       Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>),
                                      (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>,
                                       Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<i64>>),
                                      (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<i64>>), String)> {
    let (rows, t, pmeta, rxy, sxy, prm) = (rows.as_array(), t.as_array(), pmeta.as_array(), rxy.as_array(),
                                           sxy.as_array(), params.as_array().to_vec());
    let np_ = row_off.len().saturating_sub(1);
    let ok_off = |o: &Vec<i64>, n: usize| o.first() == Some(&0) && o.windows(2).all(|w| w[1] >= w[0])
        && o.last().map(|&v| v as usize == n).unwrap_or(false);
    let bad = rows.ncols() != 3 || pmeta.ncols() != 3 || !matches!(rxy.ncols(), 2 | 4) || sxy.ncols() != 2 || prm.len() != NPARAMS
        || boxm.len() != 4 || t_off.len() != np_ + 1 || k_off.len() != np_ + 1 || pmeta.nrows() != np_
        || !ok_off(&row_off, rows.nrows()) || !ok_off(&t_off, t.len()) || !ok_off(&k_off, kinds.len())
        || !ok_off(&r_off, rxy.nrows()) || r_hw.len() + 1 != r_off.len() || r_oneway.len() != r_hw.len()
        || !ok_off(&s_off, sxy.nrows())
        || s_hw.len() + 1 != s_off.len()
        || rows.iter().chain(t.iter()).chain(pmeta.iter()).chain(rxy.iter()).chain(sxy.iter()).chain(prm.iter())
            .chain(r_hw.iter()).chain(s_hw.iter()).chain(boxm.iter()).any(|v| !v.is_finite());
    if bad { return Err(PyValueError::new_err("lane_surface: shapes, offsets or values do not agree")); }
    let mut pieces: Vec<Piece> = Vec::with_capacity(np_);
    for i in 0..np_ {
        let (r0, r1) = (row_off[i] as usize, row_off[i + 1] as usize);
        let nr = r1 - r0;
        let ks: Vec<i64> = kinds[k_off[i] as usize..k_off[i + 1] as usize].to_vec();
        let nb = ks.len() + 1;
        if nr < 2 || ks.is_empty() || (t_off[i + 1] - t_off[i]) as usize != nr * nb
            || ks.iter().any(|&k| !(K_SH..=K_AD).contains(&k)) {
            return Err(PyValueError::new_err("lane_surface: a piece's rows, boundaries and strips do not agree"));
        }
        let (shl, shr) = (maxf(pmeta[[i, 0]], 0.0), maxf(pmeta[[i, 1]], 0.0));
        let mut s = Vec::with_capacity(nr);
        let mut p = Vec::with_capacity(nr);
        let mut tt = Vec::with_capacity(nr);
        for r in 0..nr {
            s.push(rows[[r0 + r, 0]]);
            p.push([rows[[r0 + r, 1]], rows[[r0 + r, 2]]]);
            let base = t_off[i] as usize + r * nb;
            let mut row = Vec::with_capacity(nb + 2);
            row.push(t[base] + shl);
            for k in 0..nb { row.push(t[base + k]); }
            row.push(t[base + nb - 1] - shr);
            tt.push(row);
        }
        if s.windows(2).any(|w| w[1] <= w[0]) {
            return Err(PyValueError::new_err("lane_surface: a piece's stations do not increase"));
        }
        let mut kk = vec![K_SH];
        kk.extend(ks);
        kk.push(K_SH);
        pieces.push(Piece { s, p, t: tt, kinds: kk, lane_w: pmeta[[i, 2]] });
    }
    let lines_of = |xy: &numpy::ndarray::ArrayView2<f64>, off: &Vec<i64>, hw: &Vec<f64>| -> Vec<Line> {
        (0..hw.len()).map(|k| {
            let mut p: Vec<P2> = Vec::new();
            let mut section = Vec::new();
            for r in off[k] as usize..off[k + 1] as usize {
                let q = [xy[[r, 0]], xy[[r, 1]]];
                if p.last() != Some(&q) {
                    p.push(q);
                    if xy.ncols() == 4 { section.push([xy[[r,2]],xy[[r,3]]]); }
                }
            }
            let mut line = Line::new(p, hw[k]);
            line.section = section;
            line
        }).collect()
    };
    let mut roads = lines_of(&rxy, &r_off, &r_hw);
    for (r, &o) in roads.iter_mut().zip(r_oneway.iter()) { r.oneway = o; }
    joints(&mut roads);
    let structs = lines_of(&sxy, &s_off, &s_hw);
    let boxa = [boxm[0], boxm[1], boxm[2], boxm[3]];
    let out = py.detach(|| build(&pieces, &roads, &structs, &boxa, &prm, outlines_only));
    let (mesh, lines, outl, cut, cutw, status) = out;
    let flat2 = |v: &Vec<P2>| -> Vec<f64> { let mut o = Vec::with_capacity(v.len() * 2); for p in v { o.push(p[0]); o.push(p[1]); } o };
    let tri: Vec<i64> = mesh.t.iter().flat_map(|t| t.iter().copied()).collect();
    // outlines: three per kept piece, one flat list with offsets
    let mut oxy: Vec<P2> = Vec::new();
    let mut ooff: Vec<i64> = vec![0];
    for set in [&outl, &cut, &cutw] {
        for o in set.iter() { oxy.extend_from_slice(o); ooff.push(oxy.len() as i64); }
    }
    let mut loff = vec![0i64];
    loff.extend(lines.off.iter().copied());
    Ok(((flat2(&mesh.v).into_pyarray(py), flat2(&mesh.uv).into_pyarray(py), tri.into_pyarray(py),
         mesh.owner.into_pyarray(py), mesh.kind.into_pyarray(py)),
        (flat2(&lines.xy).into_pyarray(py), loff.into_pyarray(py), lines.kind.into_pyarray(py),
         lines.phase.into_pyarray(py), lines.piece.into_pyarray(py)),
        (flat2(&oxy).into_pyarray(py), ooff.into_pyarray(py)), status))
}

/// Two roads meeting end to end (one point, within JOINT_M; going on within 45 degrees, the only such pair there) share
/// their end row: the mitre of their end segments (no wedge of overlap on one side and of gap on the other).
fn joints(roads: &mut [Line]) {
    const JOINT_M: f64 = 0.01;
    let n = roads.len();
    let mut ends: Vec<(usize, usize, P2, P2)> = Vec::new();     // (road, end, point, direction into the road)
    for (i, r) in roads.iter().enumerate() {
        let m = r.p.len();
        if m < 2 { continue; }
        ends.push((i, 0, r.p[0], r.dir(0)));
        ends.push((i, 1, r.p[m - 1], mul(r.dir(m - 2), -1.0)));
    }
    let mut set: Vec<(usize, usize, P2)> = Vec::new();
    for a in 0..ends.len() {
        let (i, e, p, d) = ends[a];
        let mut hits = Vec::new();
        for (b, &(j, _f, q, dq)) in ends.iter().enumerate() {
            if b == a || j == i { continue; }
            if norm(sub(p, q)) <= JOINT_M && dot(d, dq) < -0.7 { hits.push(b); }
        }
        // (and no third road leaving that point: a fork without an event keeps its square ends)
        let others = ends.iter().enumerate().filter(|&(b, &(j, _, q, _))| b != a && j != i && norm(sub(p, q)) <= JOINT_M).count();
        if hits.len() != 1 || others != 1 { continue; }
        let (_j, _f, _q, dq) = ends[hits[0]];
        // the travel direction in road i's sense through the joint: the mean of both
        let di = if e == 0 { d } else { mul(d, -1.0) };
        let dj = if e == 0 { mul(dq, -1.0) } else { dq };
        let avg = unit(add(di, dj));
        let m = left(avg);
        let c = clampf(dot(m, left(di)), 0.5, 1.0);
        set.push((i, e, mul(m, 1.0 / c)));
    }
    let _ = n;
    for (i, e, nn) in set { roads[i].ends[e] = Some(nn); }
}

#[allow(clippy::type_complexity)]
fn build(pieces: &[Piece], roads: &[Line], structs: &[Line], boxm: &[f64; 4], prm: &[f64], outlines_only: bool)
         -> (Mesh, Lines, Vec<Vec<P2>>, Vec<Vec<P2>>, Vec<Vec<P2>>, String) {
    let mut kept: Vec<Kept> = Vec::new();
    let mut status: Vec<String> = Vec::new();
    // road crossings: (road, station, piece slot in kept, hi end)
    let mut crossings: Vec<(usize, f64)> = Vec::new();
    // (road, nose station, outward sign, easing length, shift at the nose).
    let mut ramp_blends: Vec<(usize, f64, f64, f64, P2)> = Vec::new();
    for (pi, pc) in pieces.iter().enumerate() {
        let nr = pc.s.len();
        let nb = pc.nb();
        let nrm = pc.normals(0, nr - 1);
        let raw: Vec<Vec<P2>> = (0..nr).map(|i| (0..nb).map(|k| add(pc.p[i], mul(nrm[i], pc.t[i][k]))).collect()).collect();
        // rows inside an earlier piece or on a structure of this direction
        let mut blocked: Vec<i64> = vec![-1; nr];     // -1 free, j: inside kept[j], -2: on a structure
        for i in 0..nr {
            // (on it: the main point or any lane boundary, a lane's middle included: a lane on the earlier piece is no
            // shrinking matter; only a shoulder is drawn in, below)
            let mut probes: Vec<P2> = vec![pc.p[i]];
            for k in 1..nb - 1 {
                probes.push(raw[i][k]);
                if k + 1 < nb - 1 { probes.push(lerp(raw[i][k], raw[i][k + 1], 0.5)); }
            }
            for (j, kp) in kept.iter().enumerate() {
                if probes.iter().any(|q| inside(&kp.raw_outline, *q)) { blocked[i] = j as i64; break; }
            }
            if blocked[i] != -1 { continue; }
            let along = unit(sub(pc.p[if i + 1 < nr { i + 1 } else { i }], pc.p[if i > 0 { i - 1 } else { i }]));
            for st in structs {
                let (d, sd) = st.near(pc.p[i]);
                if d < st.hw && absf(dot(sd, along)) >= prm[P_STRUCT_COS] { blocked[i] = -2; break; }
            }
        }
        // the longest run of free rows (the first on a tie)
        let (mut best, mut cur) = ((0usize, 0usize, 0usize), None::<usize>);
        for i in 0..=nr {
            let free = i < nr && blocked[i] == -1;
            match (free, cur) {
                (true, None) => cur = Some(i),
                (false, Some(a)) => { if i - a > best.2 { best = (a, i - 1, i - a); } cur = None; }
                _ => {}
            }
        }
        if best.2 < 3 {
            status.push(format!("{{\"piece\":{},\"kept\":false,\"why\":\"{}\"}}", pi,
                                if blocked.iter().any(|&b| b >= 0) { "inside_earlier" } else { "on_structure" }));
            continue;
        }
        let (a, b) = (best.0, best.1);
        let mut flags: Vec<String> = Vec::new();
        if a > 0 { flags.push(format!("trim_lo:{}", if blocked[a - 1] >= 0 { "piece" } else { "structure" })); }
        if b + 1 < nr { flags.push(format!("trim_hi:{}", if blocked[b + 1] >= 0 { "piece" } else { "structure" })); }
        let mut pts: Vec<Vec<P2>> = raw[a..=b].to_vec();
        // an outer edge lying on an earlier piece (two carriageways side by side whose lanes S1 made wider than the map
        // has room for: bad data, osm2streets' last resort) drawn in until it is off it, the move eased along (a tent
        // over SHRINK_ROWS rows either side) and the lanes it passes collapsed onto it; recorded
        const SHRINK_ROWS: usize = 16;
        if !kept.is_empty() {
            let n_ = pts.len();
            let mut need = [vec![0.0f64; n_], vec![0.0f64; n_]];
            for r in 0..n_ {
                let c = pc.p[a + r];
                for (side, k) in [(0usize, 0usize), (1usize, nb - 1)] {
                    let q = pts[r][k];
                    let on = |x: P2| kept.iter().any(|kp| inside(&kp.raw_outline, x));
                    if !on(q) { continue; }
                    let span = norm(sub(c, q));
                    let (mut lo, mut hi) = (0.0, span);
                    if on(c) { need[side][r] = span; continue; }
                    for _ in 0..14 {
                        let m = 0.5 * (lo + hi);
                        if on(lerp(q, c, if span > 0.0 { m / span } else { 0.0 })) { lo = m; } else { hi = m; }
                    }
                    need[side][r] = hi + 0.01;
                }
            }
            let mut any = false;
            for side in 0..2 {
                let eased: Vec<f64> = (0..n_).map(|r| {
                    let mut m = 0.0f64;
                    let (r0, r1) = (r.saturating_sub(SHRINK_ROWS), (r + SHRINK_ROWS).min(n_ - 1));
                    for j in r0..=r1 {
                        let d = if j > r { j - r } else { r - j } as f64;
                        m = maxf(m, need[side][j] * (1.0 - d / (SHRINK_ROWS as f64 + 1.0)));
                    }
                    m
                }).collect();
                for r in 0..n_ {
                    let d = eased[r];
                    if d <= 0.0 { continue; }
                    any = true;
                    let ax = unit(sub(pts[r][nb - 1], pts[r][0]));     // left -> right
                    if side == 0 {
                        let q = add(pts[r][0], mul(ax, d));
                        let lim = dot(sub(q, pts[r][0]), ax);
                        for k in 0..nb {
                            if dot(sub(pts[r][k], pts[r][0]), ax) < lim { pts[r][k] = q; }
                        }
                        pts[r][0] = q;
                    } else {
                        let e = nb - 1;
                        let q = sub(pts[r][e], mul(ax, d));
                        for k in (0..nb).rev() {
                            if dot(sub(pts[r][k], q), ax) > 0.0 { pts[r][k] = q; }
                        }
                        pts[r][e] = q;
                    }
                }
            }
            if any { flags.push("shrunk".to_string()); }
        }
        let raw_outline = outline(&pts);
        // the welds of both ends
        let mut targets: [Option<Vec<P2>>; 2] = [None, None];
        for (e, hi) in [(0usize, false), (1usize, true)] {
            let i = if hi { b } else { a };
            let row = &raw[i];
            let n = nrm[i];
            let tau: Vec<f64> = row.iter().map(|q| dot(sub(*q, pc.p[i]), n)).collect();
            let along = unit(sub(pc.p[if hi { b } else { a + 1 }], pc.p[if hi { b - 1 } else { a }]));
            let nb_ = row.len();
            let (e0, e1) = (add(row[0], mul(n, prm[P_END_EXT])), sub(row[nb_ - 1], mul(n, prm[P_END_EXT])));
            // an earlier piece beyond this end: onto its nearer end row
            let beyond = if hi { b + 1 < nr && blocked[b + 1] >= 0 } else { a > 0 && blocked[a - 1] >= 0 };
            if beyond {
                let j = if hi { blocked[b + 1] } else { blocked[a - 1] } as usize;
                let kp = &kept[j];
                let c = pc.p[i];
                let (r0, r1) = (kp.end_row(false), kp.end_row(true));
                let dist = |r: &Vec<P2>| norm(sub(onto_row(r, c, 0.0), c));
                let er = if dist(r1) < dist(r0) { r1 } else { r0 };
                // (only through that end row: one met from its side is no continuation of it, a free end there)
                let ax = unit(sub(*er.last().unwrap(), er[0]));
                if dist(er) <= ABUT_M && absf(dot(ax, along)) <= 0.5 {
                    targets[e] = Some(row.iter().map(|q| onto_row(er, *q, prm[P_SNAP])).collect());
                    flags.push(format!("{}:piece", if hi { "weld_hi" } else { "weld_lo" }));
                } else {
                    flags.push(format!("{}", if hi { "side_hi" } else { "side_lo" }));
                }
                continue;
            }
            let mut holds: Vec<(Hold, f64, f64)> = Vec::new();
            for (ri, rd) in roads.iter().enumerate() {
                for k in 0..rd.p.len().saturating_sub(1) {
                    if let Some((u, _v)) = seg_cross(rd.p[k], rd.p[k + 1], e0, e1) {
                        let dir = rd.dir(k);
                        let c = dot(dir, along);
                        if (if rd.oneway { c } else { absf(c) }) < prm[P_CROSS_COS] { continue; }
                        let st = rd.s[k] + (rd.s[k + 1] - rd.s[k]) * u;
                        let (q, nn) = rd.row(st);
                        let (mut l, mut r) = (add(q, mul(nn, rd.hw)), sub(q, mul(nn, rd.hw)));
                        if dot(nn, n) < 0.0 { std::mem::swap(&mut l, &mut r); }     // (a road drawn against travel)
                        let (tl, tr) = (dot(sub(l, pc.p[i]), n), dot(sub(r, pc.p[i]), n));
                        holds.push((Hold { left: l, right: r, centre: dot(sub(q, pc.p[i]), n) }, tl, tr));
                        crossings.push((ri, st));
                    }
                }
            }
            if holds.is_empty() {
                // a structure starting within DECK_JOIN of this end: its end's edges (a deck's gap: S3's)
                for st in structs {
                    let m = st.p.len();
                    if m < 2 { continue; }
                    for (q, dir) in [(st.p[0], st.dir(0)), (st.p[m - 1], st.dir(m - 2))] {
                        if absf(dot(dir, along)) < prm[P_STRUCT_COS] { continue; }
                        let c = pc.p[i];
                        let off = dot(sub(q, c), along);
                        let lat = dot(sub(q, c), n);
                        let reach = if hi { off } else { -off };
                        if reach < -1.0 || reach > prm[P_DECK_JOIN] || lat > tau[0] + 1.0 || lat < tau[nb_ - 1] - 1.0 { continue; }
                        let nn = left(dir);
                        let (l, r) = if dot(nn, n) >= 0.0 { (add(q, mul(nn, st.hw)), sub(q, mul(nn, st.hw))) }
                                     else { (sub(q, mul(nn, st.hw)), add(q, mul(nn, st.hw))) };
                        let (tl, tr) = (dot(sub(l, c), n), dot(sub(r, c), n));
                        holds.push((Hold { left: l, right: r, centre: lat }, tl, tr));
                        flags.push(format!("{}:{}", if hi { "deck_hi" } else { "deck_lo" }, (reach * 100.0).round() / 100.0));
                    }
                }
            }
            targets[e] = targets_from_holds(row, &tau, &pc.kinds, &holds);
            if targets[e].is_none() { flags.push(format!("{}", if hi { "free_hi" } else { "free_lo" })); }
            else if !holds.is_empty() && !flags.iter().any(|f| f.starts_with(if hi { "deck_hi" } else { "deck_lo" })) {
                flags.push(format!("{}:road{}", if hi { "weld_hi" } else { "weld_lo" }, holds.len()));
            }
        }
        // ease the rows toward each end's targets
        let len = pc.s[b] - pc.s[a];
        for (e, hi) in [(0usize, false), (1usize, true)] {
            let mut tg = match &targets[e] { Some(v) => v.clone(), None => continue };
            let i_end = if hi { b - a } else { 0 };
            // the roads beyond overlapping at a gore (the map's ramp line too near the through road): the branch side's
            // targets moved off by the overlap (the gore closed there, no lane squeezed; the branch road's own strip
            // overlaps the through road's by that much beyond the nose: the map's, recorded)
            for g in 0..nb - 1 {
                if pc.kinds[g] != K_GO { continue; }
                let ax = unit(sub(tg[nb - 1], tg[0]));
                let gwt = dot(sub(tg[g + 1], tg[g]), ax);
                let through_left = pc.kinds[..g].contains(&K_TH);
                let (l, r) = if through_left { (g + 1, nb - 1) } else { (0, g) };
                let mut gap = gwt.max(0.0);
                // Keep the designed physical nose open. Carry the matching ramp's cut
                // row with it and ease back to that road's mapped line downstream.
                let raw_gap = dot(sub(pts[i_end][g + 1], pts[i_end][g]), ax);
                if raw_gap >= 0.5 {
                    for &(ri, station) in &crossings {
                        let rd = &roads[ri];
                        let (q, normal) = rd.row(station);
                        let (a, b) = (add(q, mul(normal, rd.hw)), sub(q, mul(normal, rd.hw)));
                        let same = norm(sub(tg[l], a)) + norm(sub(tg[r], b)) < 1e-5;
                        let reverse = norm(sub(tg[l], b)) + norm(sub(tg[r], a)) < 1e-5;
                        if !same && !reverse { continue; }
                        let sign = if hi == same { 1.0 } else { -1.0 };
                        let avail = if sign > 0.0 { rd.len() - station } else { station };
                        if avail < prm[P_BLEND_MIN] { continue; }
                        gap = raw_gap;
                        let d = (gap - gwt) * if through_left { 1.0 } else { -1.0 };
                        let len = clampf(d.abs() * prm[P_BLEND_K], prm[P_BLEND_MIN], prm[P_BLEND_MAX]).min(avail);
                        ramp_blends.push((ri, station, sign, len, mul(ax, d)));
                        flags.push(format!("ramp_ease:{}", (len * 10.0).round() / 10.0));
                        break;
                    }
                }
                if gwt < 0.0 { flags.push(format!("nose_overlap:{}", (-gwt * 100.0).round() / 100.0)); }
                let d = mul(ax, gap - gwt);
                if through_left { for p in &mut tg[g + 1..] { *p = add(*p, d); } }
                else { for p in &mut tg[..=g] { *p = sub(*p, d); } }
            }
            let d: Vec<P2> = (0..nb).map(|k| sub(tg[k], pts[i_end][k])).collect();
            let mut dmax = 0.0;
            for v in &d { dmax = maxf(dmax, norm(*v)); }
            let lb = minf(clampf(dmax * prm[P_BLEND_K], prm[P_BLEND_MIN], prm[P_BLEND_MAX]), 0.45 * len);
            let s_end = if hi { pc.s[b] } else { pc.s[a] };
            let gw = |row: &Vec<P2>, g: usize| -> f64 { dot(sub(row[g + 1], row[g]), unit(sub(row[nb - 1], row[0]))) };
            let gores: Vec<usize> = (0..nb - 1).filter(|&g| pc.kinds[g] == K_GO).collect();
            let g_raw: Vec<Vec<f64>> = pts.iter().map(|row| gores.iter().map(|&g| gw(row, g)).collect()).collect();
            let mut zone: Vec<(usize, f64)> = Vec::new();
            for r in 0..pts.len() {
                let dist = absf(pc.s[a + r] - s_end);
                if r != i_end && (lb <= 0.0 || dist >= lb) { continue; }
                let w = if r == i_end { 1.0 } else { quintic(1.0 - dist / lb) };
                for k in 0..nb { pts[r][k] = add(pts[r][k], mul(d[k], w)); }
                if r != i_end { zone.push((r, w)); }
            }
            // a gore eased by scaling, not by the move of its sides: where the end's gore is narrower than the
            // piece's (the roads beyond closer than S1's nose), it narrows in proportion along the zone, so it never
            // turns over where it is closing anyway (the branch side moved, the through side's lanes kept)
            for (gi, &g) in gores.iter().enumerate() {
                let (g_er, g_t) = (g_raw[i_end][gi], maxf(0.0, gw(&tg, g)));
                if g_er <= 1e-3 { continue; }
                let ratio = g_t / g_er;
                let through_left = (0..g).any(|k| pc.kinds[k] == K_TH);
                for &(r, w) in &zone {
                    let want = g_raw[r][gi] * (1.0 + (ratio - 1.0) * w);
                    let ax = unit(sub(pts[r][nb - 1], pts[r][0]));
                    let shift = want - dot(sub(pts[r][g + 1], pts[r][g]), ax);
                    if through_left { for k in g + 1..nb { pts[r][k] = add(pts[r][k], mul(ax, shift)); } }
                    else { for k in 0..=g { pts[r][k] = sub(pts[r][k], mul(ax, shift)); } }
                }
            }
            for k in 0..nb { pts[i_end][k] = tg[k]; }       // (the shared row exactly)
            flags.push(format!("blend_{}:{}", if hi { "hi" } else { "lo" }, (lb * 10.0).round() / 10.0));
        }
        // boundaries kept in order across each row (a crossed one onto its left neighbour)
        let mut reordered = false;
        for row in pts.iter_mut() {
            let ax = unit(sub(row[nb - 1], row[0]));
            for k in 1..nb {
                if dot(sub(row[k], row[k - 1]), ax) < -1e-6 { row[k] = row[k - 1]; reordered = true; }
            }
        }
        if reordered { flags.push("reordered".to_string()); }
        kept.push(Kept { idx: pi, a, b, pts, raw_outline, flags });
    }
    // Partition complete faces across the crop line. A face in the seam band
    // can meet the next face beyond the requested box near a diagonal edge.
    let crop = boxm;
    let context = [crop[0] - 64.0, crop[1] - 64.0, crop[2] + 64.0, crop[3] + 64.0];
    let boxm = &context;
    let mut mesh = Mesh { v: Vec::new(), uv: Vec::new(), t: Vec::new(), owner: Vec::new(), kind: Vec::new() };
    let mut lines = Lines { xy: Vec::new(), off: Vec::new(), kind: Vec::new(), phase: Vec::new(), piece: Vec::new() };
    if outlines_only {
        let outlines = kept.iter().map(|kp| outline(&kp.pts)).collect();
        let status = kept.iter().map(|kp| format!(r#"{{"piece":{},"kept":true}}"#, kp.idx))
            .collect::<Vec<_>>().join(",");
        return (mesh, lines, outlines, vec![], vec![], format!("[{}]", status));
    }
    // pieces: strips and markings
    for kp in &kept {
        let pc = &pieces[kp.idx];
        let nb = pc.nb();
        let ns = nb - 1;
        let nr = kp.pts.len();
        let base = mesh.v.len();
        for (r, row) in kp.pts.iter().enumerate() {
            let tot = norm(sub(row[nb - 1], row[0]));
            for k in 0..nb {
                mesh.v.push(snapped(row[k]));
                let u = if tot > 1e-9 { norm(sub(row[k], row[0])) / tot } else { 0.0 };
                mesh.uv.push([u, pc.s[kp.a + r]]);
            }
        }
        let width = |r: usize, k: usize| norm(sub(kp.pts[r][k + 1], kp.pts[r][k]));
        for k in 0..ns {
            if pc.kinds[k] == K_GO { gore::zone(&mut mesh, base, nb, nr, k, kp.idx as i64, boxm); }
        }
        for r in 0..nr - 1 {
            for k in 0..ns {
                if pc.kinds[k] == K_GO { continue; }
                if width(r, k) < COLLAPSE_M && width(r + 1, k) < COLLAPSE_M { continue; }
                let id = |rr: usize, kk: usize| base + rr * nb + kk;
                mesh.quad([id(r, k), id(r, k + 1), id(r + 1, k + 1), id(r + 1, k)], kp.idx as i64, pc.kinds[k], boxm);
            }
        }
        // markings: runs of one line kind per boundary
        let ws: Vec<Vec<f64>> = (0..nr).map(|r| (0..ns).map(|k| width(r, k)).collect()).collect();
        for k in 1..ns {
            let mut run: Vec<P2> = Vec::new();
            let mut run_kind = -1i64;
            let mut run_s = 0.0;
            for r in 0..=nr {
                let kd = if r < nr { line_kind(&pc.kinds, &ws[r], k, pc.lane_w, prm).unwrap_or(-1) } else { -1 };
                if kd != run_kind {
                    if run_kind >= 0 { lines.push(&run, run_kind, run_s, kp.idx); }
                    run.clear();
                    run_kind = kd;
                    if r < nr { run_s = pc.s[kp.a + r]; }
                }
                if r < nr && kd >= 0 { run.push(kp.pts[r][k]); }
            }
        }
        // gore stripes: every STRIPE_STEP from the station 0 grid, where the gore is GORE_MIN wide or more
        let step = prm[P_STRIPE_STEP];
        if step > 0.0 {
            for g in 0..ns {
                if pc.kinds[g] != K_GO { continue; }
                let mut r = 0;
                while r + 1 < nr {
                    let (s0, s1) = (pc.s[kp.a + r], pc.s[kp.a + r + 1]);
                    let q = (s0 / step).ceil() * step;
                    if q >= s0 && q < s1 && ws[r][g] >= 2.0 * prm[P_GORE_MIN] {
                        let u = (q - s0) / (s1 - s0);
                        let l = lerp(kp.pts[r][g], kp.pts[r + 1][g], u);
                        let rr = lerp(kp.pts[r][g + 1], kp.pts[r + 1][g + 1], u);
                        let w = norm(sub(rr, l));
                        // the stripe's far end w further downstream (a 45 degree diagonal), clipped to the gore
                        let ahead = |k: usize| -> P2 {
                            let mut rj = r;
                            let target = q + w;
                            while rj + 1 < nr && pc.s[kp.a + rj + 1] < target { rj += 1; }
                            if rj + 1 >= nr { return kp.pts[nr - 1][k]; }
                            let (t0, t1) = (pc.s[kp.a + rj], pc.s[kp.a + rj + 1]);
                            lerp(kp.pts[rj][k], kp.pts[rj + 1][k], clampf((target - t0) / (t1 - t0), 0.0, 1.0))
                        };
                        if prm[P_CHEVRON] > 0.5 {
                            let mid = lerp(l, rr, 0.5);
                            let (la, ra) = (ahead(g), ahead(g + 1));
                            // a V pointing upstream: both arms from the middle at q to the sides w / 2 downstream
                            let ml = lerp(l, la, 0.5);
                            let mr = lerp(rr, ra, 0.5);
                            lines.push(&[mid, ml], L_STRIPE, q, kp.idx);
                            lines.push(&[mid, mr], L_STRIPE, q, kp.idx);
                        } else {
                            let ra = ahead(g + 1);
                            lines.push(&[l, ra], L_STRIPE, q, kp.idx);
                        }
                    }
                    r += 1;
                }
            }
        }
    }
    // roads: their stretches outside every kept piece, rows canonical (every vertex and every ROAD_STEP from 0, plus
    // the cuts)
    let spans = junction::build(roads, &kept, &crossings, &mut mesh, boxm);
    let step = prm[P_ROAD_STEP];
    for (ri, rd) in roads.iter().enumerate() {
        if rd.p.len() < 2 { continue; }
        // Structural cross-sections carry pavement under a lane event all
        // the way to its surveyed edge. Let partition subtract the actual
        // event faces, including a partial lane, from the supporting slab.
        if !rd.section.is_empty() {
            let mut prev = None;
            for (j, (&p, &axis)) in rd.p.iter().zip(&rd.section).enumerate() {
                let il = mesh.v.len();
                mesh.v.extend([snapped(add(p,mul(axis,rd.hw))),snapped(sub(p,mul(axis,rd.hw)))]);
                mesh.uv.extend([[0.0,rd.s[j]], [1.0,rd.s[j]]]);
                if let Some(pl) = prev {
                    mesh.quad([pl,pl+1,il+1,il], -1-ri as i64,K_TH,boxm);
                }
                prev = Some(il);
            }
            continue;
        }
        let mut cuts: Vec<f64> = crossings.iter().filter(|c| c.0 == ri).map(|c| c.1).collect();
        // the kept pieces' outlines widened by most of this road's half width: a road running beside a piece's edge
        // (a ramp's mapped line outside the lane S1 gives it) is cut where its strip would lie on the piece
        let mut rb = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for q in &rd.p { rb[0] = minf(rb[0], q[0]); rb[1] = minf(rb[1], q[1]); rb[2] = maxf(rb[2], q[0]); rb[3] = maxf(rb[3], q[1]); }
        let wids: Vec<Vec<P2>> = kept.iter().filter_map(|kp| {
            let w = widened(&kp.pts, prm[P_ROAD_CLEAR] * rd.hw);
            let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
            for q in &w { b[0] = minf(b[0], q[0]); b[1] = minf(b[1], q[1]); b[2] = maxf(b[2], q[0]); b[3] = maxf(b[3], q[1]); }
            if b[2] < rb[0] || b[0] > rb[2] || b[3] < rb[1] || b[1] > rb[3] { None } else { Some(w) }
        }).collect();
        for w in &wids {
            for k in 0..rd.p.len() - 1 {
                for e in 0..w.len() {
                    if let Some((u, _v)) = seg_cross(rd.p[k], rd.p[k + 1], w[e], w[(e + 1) % w.len()]) {
                        cuts.push(rd.s[k] + (rd.s[k + 1] - rd.s[k]) * u);
                    }
                }
            }
        }
        cuts.retain(|s| *s > spans[ri][0] && *s < spans[ri][1]);
        cuts.extend(spans[ri]);
        cuts.sort_by(|a, b| a.partial_cmp(b).unwrap());
        cuts.dedup();
        for w in cuts.windows(2) {
            let (s0, s1) = (w[0], w[1]);
            if s1 - s0 < 1e-6 { continue; }
            let (mid, _) = rd.row(0.5 * (s0 + s1));
            if wids.iter().any(|w| inside(w, mid)) { continue; }
            // the rows of [s0, s1]
            let mut st: Vec<f64> = vec![s0];
            let mut q = (s0 / step).floor() * step + step;
            let mut v = 0;
            while v < rd.s.len() && rd.s[v] <= s0 { v += 1; }
            loop {
                let nv = if v < rd.s.len() { rd.s[v] } else { f64::INFINITY };
                let nx = minf(q, nv);
                if nx >= s1 { break; }
                if nx > *st.last().unwrap() + 1e-9 { st.push(nx); }
                if nx == q { q += step; }
                if nx == nv { v += 1; }
            }
            st.push(s1);
            // skip rows far from the box
            let near = |p: P2| p[0] >= boxm[0] - rd.hw - step && p[0] <= boxm[2] + rd.hw + step
                && p[1] >= boxm[1] - rd.hw - step && p[1] <= boxm[3] + rd.hw + step;
            let rws: Vec<(P2, P2)> = st.iter().map(|&x| {
                let (mut p, nn) = rd.row(x);
                for &(road, nose, sign, length, delta) in &ramp_blends {
                    let dist = (x - nose) * sign;
                    if road == ri && dist >= 0.0 && dist < length {
                        p = add(p, mul(delta, quintic(1.0 - dist / length)));
                    }
                }
                (p, nn)
            }).collect();
            let mut prev: Option<(usize, usize)> = None;
            for (j, (p, nn)) in rws.iter().enumerate() {
                let here = near(*p) || (j + 1 < rws.len() && near(rws[j + 1].0)) || (j > 0 && near(rws[j - 1].0));
                if !here { prev = None; continue; }
                let (l, r) = (add(*p, mul(*nn, rd.hw)), sub(*p, mul(*nn, rd.hw)));
                let il = mesh.v.len();
                mesh.v.push(snapped(l));
                mesh.uv.push([0.0, st[j]]);
                mesh.v.push(snapped(r));
                mesh.uv.push([1.0, st[j]]);
                if let Some((pl, pr)) = prev {
                    mesh.quad([pl, pr, il + 1, il], -1 - ri as i64, K_TH, boxm);
                }
                prev = Some((il, il + 1));
            }
        }
    }
    partition::apply(&mut mesh);
    let mut n = 0;
    for i in 0..mesh.t.len() {
        let pts = mesh.t[i].map(|j| mesh.v[j as usize]);
        if (0..2).any(|a| pts.iter().all(|p| p[a] < crop[a]) || pts.iter().all(|p| p[a] > crop[a+2])) {continue;}
        mesh.t[n] = mesh.t[i];
        mesh.owner[n] = mesh.owner[i];
        mesh.kind[n] = mesh.kind[i];
        n += 1;
    }
    mesh.t.truncate(n);
    mesh.owner.truncate(n);
    mesh.kind.truncate(n);
    let outl: Vec<Vec<P2>> = kept.iter().map(|kp| outline(&kp.pts)).collect();
    let cut: Vec<Vec<P2>> = kept.iter().map(|kp| widened(&kp.pts, prm[P_CUT_M])).collect();
    let cutw: Vec<Vec<P2>> = kept.iter().map(|kp| widened(&kp.pts, prm[P_CUT_WIDE_M])).collect();
    for kp in &kept {
        let fl = kp.flags.iter().map(|f| format!("\"{}\"", f)).collect::<Vec<_>>().join(",");
        // the steepest width change per m of a lane strip and of a shoulder between rows (the gore opens as it will)
        let pc = &pieces[kp.idx];
        let (mut lane_rate, mut sh_rate, mut at) = (0.0f64, 0.0f64, 0.0f64);
        for r in 0..kp.pts.len() - 1 {
            let ds = pc.s[kp.a + r + 1] - pc.s[kp.a + r];
            if ds <= 0.2 { continue; }
            for k in 0..pc.kinds.len() {
                let w0 = norm(sub(kp.pts[r][k + 1], kp.pts[r][k]));
                let w1 = norm(sub(kp.pts[r + 1][k + 1], kp.pts[r + 1][k]));
                let rate = absf(w1 - w0) / ds;
                match pc.kinds[k] {
                    K_SH => { sh_rate = maxf(sh_rate, rate); }
                    K_GO => {}
                    _ => { if rate > lane_rate { lane_rate = rate; at = pc.s[kp.a + r]; } }
                }
            }
        }
        status.push(format!("{{\"piece\":{},\"kept\":true,\"rows\":[{},{}],\"flags\":[{}],\"lane_rate\":{},\
                             \"lane_rate_s\":{},\"shoulder_rate\":{}}}", kp.idx, kp.a, kp.b, fl,
                            (lane_rate * 1e4).round() / 1e4, at.round(), (sh_rate * 1e4).round() / 1e4));
    }
    status.sort_by_key(|s| {
        let k = s.find(':').map(|i| i + 1).unwrap_or(0);
        s[k..].split(|c: char| c == ',' || c == '}').next().unwrap_or("0").parse::<i64>().unwrap_or(0)
    });
    (mesh, lines, outl, cut, cutw, format!("[{}]", status.join(",")))
}

/// cut_lines (earthfast 542, v3 S2): the pieces of polylines outside every polygon. xy (n, c) (c >= 2: x, y, then any
/// columns carried, interpolated at a cut), off per line; polys (m, 2), p_off per polygon. Returns (points (k, c) flat,
/// offsets, each piece's line, each piece's start's arc length along its line). A point on a polygon's boundary is a
/// cut; pieces under min_len m are dropped.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn cut_lines<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: Vec<i64>, polys: PyReadonlyArray2<'py, f64>,
                      p_off: Vec<i64>, min_len: f64)
                      -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>,
                                   Bound<'py, PyArray1<f64>>)> {
    let (xy, pg) = (xy.as_array(), polys.as_array());
    let c = xy.ncols();
    let okoff = |o: &Vec<i64>, n: usize| o.first() == Some(&0) && o.windows(2).all(|w| w[1] >= w[0])
        && o.last().map(|&v| v as usize == n).unwrap_or(false);
    if c < 2 || pg.ncols() != 2 || !okoff(&off, xy.nrows()) || !okoff(&p_off, pg.nrows()) || !min_len.is_finite()
        || xy.iter().chain(pg.iter()).any(|v| !v.is_finite()) {
        return Err(PyValueError::new_err("cut_lines: shapes, offsets or values do not agree"));
    }
    let polys_v: Vec<Vec<P2>> = (0..p_off.len() - 1)
        .map(|k| (p_off[k] as usize..p_off[k + 1] as usize).map(|r| [pg[[r, 0]], pg[[r, 1]]]).collect()).collect();
    let boxes: Vec<[f64; 4]> = polys_v.iter().map(|p| {
        let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for q in p { b[0] = minf(b[0], q[0]); b[1] = minf(b[1], q[1]); b[2] = maxf(b[2], q[0]); b[3] = maxf(b[3], q[1]); }
        b
    }).collect();
    let mut out: Vec<f64> = Vec::new();
    let mut ooff: Vec<i64> = vec![0];
    let mut oline: Vec<i64> = Vec::new();
    let mut os: Vec<f64> = Vec::new();
    for li in 0..off.len() - 1 {
        let (r0, r1) = (off[li] as usize, off[li + 1] as usize);
        if r1 - r0 < 2 { continue; }
        let row = |r: usize| -> Vec<f64> { (0..c).map(|j| xy[[r, j]]).collect() };
        let pt = |v: &Vec<f64>| -> P2 { [v[0], v[1]] };
        let mut lb = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for r in r0..r1 { lb[0] = minf(lb[0], xy[[r, 0]]); lb[1] = minf(lb[1], xy[[r, 1]]); lb[2] = maxf(lb[2], xy[[r, 0]]); lb[3] = maxf(lb[3], xy[[r, 1]]); }
        let near: Vec<usize> = (0..polys_v.len()).filter(|&k| {
            let b = &boxes[k];
            !(b[2] < lb[0] || b[0] > lb[2] || b[3] < lb[1] || b[1] > lb[3])
        }).collect();
        // densified with the cut points: (point, arc length, inside)
        let mut seq: Vec<(Vec<f64>, f64)> = Vec::new();
        let mut s = 0.0;
        for r in r0..r1 - 1 {
            let (a, b) = (row(r), row(r + 1));
            let (pa, pb) = (pt(&a), pt(&b));
            let l = norm(sub(pb, pa));
            if seq.is_empty() { seq.push((a.clone(), s)); }
            let mut us: Vec<f64> = Vec::new();
            for &k in &near {
                let p = &polys_v[k];
                for e in 0..p.len() {
                    let (q0, q1) = (p[e], p[(e + 1) % p.len()]);
                    if let Some((u, _v)) = seg_cross(pa, pb, q0, q1) { if u > 1e-9 && u < 1.0 - 1e-9 { us.push(u); } }
                }
            }
            us.sort_by(|x, y| x.partial_cmp(y).unwrap());
            for u in us {
                let v: Vec<f64> = (0..c).map(|j| a[j] + (b[j] - a[j]) * u).collect();
                seq.push((v, s + l * u));
            }
            s += l;
            seq.push((b.clone(), s));
        }
        let ins = |p: P2| near.iter().any(|&k| inside(&polys_v[k], p));
        // each sub-segment kept when its middle is outside
        let mut cur: Vec<&(Vec<f64>, f64)> = Vec::new();
        let mut flush = |cur: &mut Vec<&(Vec<f64>, f64)>, out: &mut Vec<f64>, ooff: &mut Vec<i64>, oline: &mut Vec<i64>,
                         os: &mut Vec<f64>| {
            if cur.len() >= 2 && cur.last().unwrap().1 - cur[0].1 >= min_len {
                for q in cur.iter() { out.extend_from_slice(&q.0); }
                ooff.push((out.len() / c) as i64);
                oline.push(li as i64);
                os.push(cur[0].1);
            }
            cur.clear();
        };
        for k in 0..seq.len() - 1 {
            let (a, b) = (&seq[k], &seq[k + 1]);
            if b.1 - a.1 <= 1e-12 { continue; }
            let m = lerp(pt(&a.0), pt(&b.0), 0.5);
            if ins(m) {
                flush(&mut cur, &mut out, &mut ooff, &mut oline, &mut os);
            } else {
                if cur.is_empty() { cur.push(a); }
                cur.push(b);
            }
        }
        flush(&mut cur, &mut out, &mut ooff, &mut oline, &mut os);
    }
    Ok((out.into_pyarray(py), ooff.into_pyarray(py), oline.into_pyarray(py), os.into_pyarray(py)))
}

// (tests: server/tests/test_roadsurf3.py, through the module: the crate's own test binary does not link without
// Python)
