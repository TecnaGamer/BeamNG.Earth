//! deck_shape / deck_simple (earthfast 498, road revamp v2 phase P1b, BNE_ROADSHAPE=decks): a simple structure's deck
//! line from the road's own smooth line, its skewed end lines and its buried ends (roads.BridgeProfiles._chain_solve,
//! roadshape.shape_chain).
//!
//! Why: a deck kept the mapped line between its ends. Where the map's bridge is one straight chord and its end vertex
//! stays a corner (roadsmooth pins both vertices of a piece holding a connector: a side road joining 6 m before the
//! deck), the slab ran straight across a curving road with a 9.6 degree corner at its end (KGBXY1DG, Baring Creek on
//! the Going-to-the-Sun Road, 48.67835 -113.59579; the aerial shows one continuous curve).
//!
//! deck_shape, per deck (its polyline xy, heights z) with the lines round it (the chain's ground roads and railways,
//! the chain's other decks):
//! - APPROACH at each end: the line with an end within JOIN_M of the deck's end, going on within APPROACH_DEG of the
//!   deck (its own feature's ground piece first, then the straightest; another line within AMBIG_DEG of the chosen
//!   one's turn, not the deck's own feature: a fork, none). Its direction is read over DIR_M of it. A ground road
//!   gives the deck's tangent there; another deck (a joint) the mean of both decks' directions, the same from either.
//! - BLEND: the first b m of the deck from an end with an approach (b = BLEND_MAX_M at most, half the deck when both
//!   ends blend, all of it when one does) replaced by the cubic Hermite from the end (the approach's tangent) to the
//!   deck's point at b (its own direction there, read +-2 m), both tangents the chord's length. Not where it would
//!   leave the deck's line by more than max(DEV_MIN_M, DEV_SHARE x b) (the approach is another road's).
//! - ROWS by the chord rule: the Hermite pieces split until no piece leaves the curve by more than CHORD_TOL_M and none
//!   is longer than MAX_STEP_M (2.8 m at R 50 m); the deck's own vertices kept between. Heights from the deck's
//!   profile at the same share of its length (the ends exact).
//! - SKEW: at each end where no other deck goes on or ends (along the deck and the decks going on from it end to
//!   end: a short bridge's pieces), the nearest road or railway crossing under the deck within SKEW_REACH_M of the end (not the
//!   deck's own feature, not an approach, crossing at SKEW_MIN_CROSS_DEG or more, the deck turning no more than
//!   SKEW_BEND_DEG between the crossing and its end): the end line runs along the crossed line, at most SKEW_MAX_DEG
//!   off square (clamped), pointing to the deck's left.
//! - OVERLAP: at such an end, a ground approach's first OVERLAP_M m from the deck's end, the buried end's line (roadmesh.deck skirts).
//!
//! deck_simple: per structure_groups member, whether its group is simple: no END_ON / OVERLAP_ALONG / OVERLAP_CROSS
//! edge inside it, every member a road, and its JOIN edges a path (no member joined to three or more, fewer JOINs than
//! members): one deck, or twins, each perhaps in pieces end to end. The design's "one or two members" counted a short
//! bridge's own pieces: roads._skew_decks carries a bridge that is a segment of its own onto the roads at its ends,
//! so a plain overpass is three members joined end to end (structprobe reports both shares).
//!
//! No FMA, no libm, no hash maps: sqrt (correctly rounded) and + - * / only, in a fixed order.
use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::structures::groups::{END_ON, JOIN, OVERLAP_ALONG, OVERLAP_CROSS, ROAD};

// params (roadshape.PARAMS, in this order)
const P_CHORD_TOL: usize = 0;       // m: a row piece's largest departure from the curve (chord rule)
const P_MAX_STEP: usize = 1;        // m between rows at most
const P_BLEND_MAX: usize = 2;       // m of a deck re-shaped from an end at most
const P_APPROACH_COS: usize = 3;    // cos of APPROACH_DEG
const P_JOIN: usize = 4;            // m: a line's end this near the deck's end goes on from it
const P_OVERLAP: usize = 5;         // m of the approach a buried end runs over
const P_SKEW_REACH: usize = 6;      // m along the deck from its end to the crossing it is the end of
const P_SKEW_MIN_CROSS_SIN: usize = 7;  // sin of SKEW_MIN_CROSS_DEG
const P_SKEW_MAX_COS: usize = 8;    // cos of SKEW_MAX_DEG
const P_SKEW_BEND_COS: usize = 9;   // cos of SKEW_BEND_DEG
const P_DEV_MIN: usize = 10;        // m a blend may leave the deck's line in any case ...
const P_DEV_SHARE: usize = 11;      // ... or this share of its length
const P_AMBIG_COS: usize = 12;      // cos of AMBIG_DEG: two approaches turning within this of each other: a fork
const P_DIR: usize = 13;            // m of an approach its direction is read over
const P_SKEW_NEAR: usize = 14;      // m: a crossing nearer the end than this joins it, not crossed
const NPARAMS: usize = 15;

const KIND_ROAD: i64 = 0;           // line kinds: a ground road, a ground railway, another deck of the chain
const KIND_RAIL: i64 = 1;
const KIND_DECK: i64 = 2;

type P2 = [f64; 2];

fn sub(a: P2, b: P2) -> P2 { [a[0] - b[0], a[1] - b[1]] }
fn add(a: P2, b: P2) -> P2 { [a[0] + b[0], a[1] + b[1]] }
fn mul(a: P2, k: f64) -> P2 { [a[0] * k, a[1] * k] }
fn dot(a: P2, b: P2) -> f64 { a[0] * b[0] + a[1] * b[1] }
fn cross(a: P2, b: P2) -> f64 { a[0] * b[1] - a[1] * b[0] }
fn norm(a: P2) -> f64 { (a[0] * a[0] + a[1] * a[1]).sqrt() }
fn unit(a: P2) -> Option<P2> {
    let n = norm(a);
    if n > 1e-9 { Some([a[0] / n, a[1] / n]) } else { None }
}

/// A polyline with its arc lengths (consecutive duplicates dropped).
struct Line {
    p: Vec<P2>,
    s: Vec<f64>,
}

impl Line {
    fn new(pts: &[P2]) -> Line {
        let mut p: Vec<P2> = Vec::with_capacity(pts.len());
        for &q in pts {
            if p.last().is_none_or(|&l| norm(sub(q, l)) > 1e-6) {
                p.push(q);
            }
        }
        let mut s = vec![0.0; p.len()];
        for i in 1..p.len() {
            s[i] = s[i - 1] + norm(sub(p[i], p[i - 1]));
        }
        Line { p, s }
    }
    fn len(&self) -> f64 { *self.s.last().unwrap_or(&0.0) }
    /// The point at arc a (clamped).
    fn at(&self, a: f64) -> P2 {
        let n = self.p.len();
        if n == 1 || a <= 0.0 { return self.p[0]; }
        if a >= self.len() { return self.p[n - 1]; }
        let mut i = self.s.partition_point(|&v| v <= a).max(1) - 1;
        if i >= n - 1 { i = n - 2; }
        let d = self.s[i + 1] - self.s[i];
        let t = if d > 0.0 { (a - self.s[i]) / d } else { 0.0 };
        add(self.p[i], mul(sub(self.p[i + 1], self.p[i]), t))
    }
    /// The unit direction round arc a, read from a - h to a + h (clamped to the line).
    fn dir(&self, a: f64, h: f64) -> Option<P2> {
        let lo = (a - h).max(0.0);
        let hi = (a + h).min(self.len());
        unit(sub(self.at(hi), self.at(lo)))
    }
    /// The distance from q to the stretch of the line between arcs a0 and a1.
    fn dist(&self, q: P2, a0: f64, a1: f64) -> f64 {
        let mut best = f64::INFINITY;
        let n = self.p.len();
        for i in 0..n.saturating_sub(1) {
            if self.s[i + 1] < a0 || self.s[i] > a1 { continue; }
            best = best.min(seg_dist(q, self.p[i], self.p[i + 1]));
        }
        if n == 1 { best = norm(sub(q, self.p[0])); }
        best
    }
}

fn seg_dist(q: P2, a: P2, b: P2) -> f64 {
    let ab = sub(b, a);
    let l2 = dot(ab, ab);
    let t = if l2 > 0.0 { (dot(sub(q, a), ab) / l2).clamp(0.0, 1.0) } else { 0.0 };
    norm(sub(q, add(a, mul(ab, t))))
}

/// The cubic Hermite from a (tangent ta) to b (tangent tb) at u.
fn hermite(a: P2, ta: P2, b: P2, tb: P2, u: f64) -> P2 {
    let u2 = u * u;
    let u3 = u2 * u;
    let h00 = 2.0 * u3 - 3.0 * u2 + 1.0;
    let h10 = u3 - 2.0 * u2 + u;
    let h01 = -2.0 * u3 + 3.0 * u2;
    let h11 = u3 - u2;
    [h00 * a[0] + h10 * ta[0] + h01 * b[0] + h11 * tb[0], h00 * a[1] + h10 * ta[1] + h01 * b[1] + h11 * tb[1]]
}

/// The chord-rule samples of a Hermite piece after u0 up to u1 (u1's point last), in order.
#[allow(clippy::too_many_arguments)]
fn sample(h: &dyn Fn(f64) -> P2, u0: f64, p0: P2, u1: f64, p1: P2, tol: f64, step: f64, depth: u32, out: &mut Vec<P2>) {
    let um = 0.5 * (u0 + u1);
    let pm = h(um);
    let q1 = h(0.5 * (u0 + um));
    let q3 = h(0.5 * (um + u1));
    let dev = seg_dist(pm, p0, p1).max(seg_dist(q1, p0, p1)).max(seg_dist(q3, p0, p1));
    if depth >= 24 || (dev <= tol && norm(sub(p1, p0)) <= step) {
        out.push(p1);
        return;
    }
    sample(h, u0, p0, um, pm, tol, step, depth + 1, out);
    sample(h, um, pm, u1, p1, tol, step, depth + 1, out);
}

/// A line of the kernel's input: its points, kind, whether it is the deck's own feature.
struct Other {
    line: Line,
    kind: i64,
    own: bool,
}

/// The approach at deck end `end` (0 start, 1 end): (kind, its outward unit direction from the deck's end, the line's
/// index, which end of it) or None.
fn approach(deck: &Line, end: usize, others: &[Other], prm: &[f64]) -> Option<(i64, P2, usize, usize)> {
    let p = if end == 0 { deck.p[0] } else { deck.p[deck.p.len() - 1] };
    let l = deck.len();
    let h = prm[P_DIR].min(0.5 * l);
    // the deck's outward direction at that end
    let out = if end == 0 { mul(deck.dir(h * 0.5, h * 0.5)?, -1.0) } else { deck.dir(l - h * 0.5, h * 0.5)? };
    let mut cands: Vec<(bool, f64, P2, usize, usize, i64)> = Vec::new();
    for (k, o) in others.iter().enumerate() {
        if o.kind == KIND_RAIL || o.line.p.len() < 2 { continue; }
        let ol = o.line.len();
        for oe in 0..2 {
            let q = if oe == 0 { o.line.p[0] } else { o.line.p[o.line.p.len() - 1] };
            if norm(sub(q, p)) > prm[P_JOIN] { continue; }
            let hd = prm[P_DIR].min(ol);
            let far = if oe == 0 { o.line.at(hd) } else { o.line.at(ol - hd) };
            let Some(v) = unit(sub(far, q)) else { continue };
            let c = dot(v, out);
            if c < prm[P_APPROACH_COS] { continue; }
            cands.push((o.own, c, v, k, oe, o.kind));
        }
    }
    if cands.is_empty() { return None; }
    // the own feature's piece first, then the straightest; the first of equals in the lines' order
    let mut best = 0;
    for i in 1..cands.len() {
        let (a, b) = (&cands[i], &cands[best]);
        if (a.0 && !b.0) || (a.0 == b.0 && a.1 > b.1) { best = i; }
    }
    let b = cands[best];
    if !b.0 {
        for (i, c) in cands.iter().enumerate() {
            if i != best && c.3 != b.3 && dot(c.2, b.2) >= prm[P_AMBIG_COS] {
                return None;            // (two lines going on alike: a fork, no tangent to take)
            }
        }
    }
    Some((b.5, b.2, b.3, b.4))
}

/// The skew end direction at deck end `end`: (unit end line pointing to the deck's left, crossing angle deg-free
/// sine) or None (square).
fn skew(deck: &Line, end: usize, others: &[Other], skip: &[usize], prm: &[f64]) -> Option<P2> {
    let l = deck.len();
    let n = deck.p.len();
    if n < 2 || l < 1.0 { return None; }
    // the line from the free end inward: the deck, then the decks going on end to end from its far end (a short
    // bridge's pieces: the crossing may lie on the next piece), up to SKEW_REACH_M
    let mut pts: Vec<P2> = if end == 0 { deck.p.clone() } else { deck.p.iter().rev().copied().collect() };
    let mut used: Vec<usize> = Vec::new();
    for _hop in 0..8 {
        let inward = Line::new(&pts);
        if inward.len() >= prm[P_SKEW_REACH] { break; }
        let tail = pts[pts.len() - 1];
        let hh = 1.0f64.min(0.5 * inward.len());
        let Some(tdir) = inward.dir(inward.len() - hh, hh) else { break };
        let mut next: Option<(f64, usize, bool)> = None;
        for (k, o) in others.iter().enumerate() {
            if o.kind != KIND_DECK || o.line.p.len() < 2 || used.contains(&k) { continue; }
            for oe in 0..2 {
                let q = if oe == 0 { o.line.p[0] } else { o.line.p[o.line.p.len() - 1] };
                if norm(sub(q, tail)) > prm[P_JOIN] { continue; }
                let ol = o.line.len();
                let h = 1.0f64.min(0.5 * ol);
                let v = if oe == 0 { o.line.dir(0.5 * h, 0.5 * h) }
                        else { o.line.dir(ol - 0.5 * h, 0.5 * h).map(|d| mul(d, -1.0)) };
                let Some(v) = v else { continue };
                let c = dot(v, tdir);
                if c >= prm[P_APPROACH_COS] && next.is_none_or(|nx| c > nx.0) { next = Some((c, k, oe == 1)); }
            }
        }
        let Some((_c, k, rev)) = next else { break };
        used.push(k);
        let o = &others[k].line.p;
        if rev { pts.extend(o.iter().rev().skip(1)); } else { pts.extend(o.iter().skip(1)); }
    }
    let inward = Line::new(&pts);
    let m = inward.p.len();
    let mut best: Option<(f64, P2, P2)> = None;     // (reach, inward dir there, crossed dir)
    for (k, o) in others.iter().enumerate() {
        if o.own || o.kind == KIND_DECK || skip.contains(&k) || o.line.p.len() < 2 { continue; }
        for i in 0..m - 1 {
            if inward.s[i] > prm[P_SKEW_REACH] { break; }
            let (a, r) = (inward.p[i], sub(inward.p[i + 1], inward.p[i]));
            for j in 0..o.line.p.len() - 1 {
                let (q, sv) = (o.line.p[j], sub(o.line.p[j + 1], o.line.p[j]));
                let den = cross(r, sv);
                if den.abs() < 1e-12 { continue; }
                let qp = sub(q, a);
                let t = cross(qp, sv) / den;
                let u = cross(qp, r) / den;
                if !(0.0..=1.0).contains(&t) || !(0.0..=1.0).contains(&u) { continue; }
                let reach = inward.s[i] + t * (inward.s[i + 1] - inward.s[i]);
                if reach <= prm[P_SKEW_NEAR] || reach > prm[P_SKEW_REACH] { continue; }
                let (Some(md), Some(cd)) = (unit(r), unit(sv)) else { continue };
                if cross(md, cd).abs() < prm[P_SKEW_MIN_CROSS_SIN] { continue; }     // (along it, not crossed)
                if best.is_none_or(|b| reach < b.0) { best = Some((reach, md, cd)); }
            }
        }
    }
    let (_reach, md, cd) = best?;
    let h = 2.0f64.min(0.5 * l);
    let t_in = if end == 0 { deck.dir(h, h)? } else { mul(deck.dir(l - h, h)?, -1.0) };
    if dot(t_in, md) < prm[P_SKEW_BEND_COS] { return None; }     // (the deck turns between: not that crossing's end)
    let t = if end == 0 { t_in } else { mul(t_in, -1.0) };      // (the deck's own way)
    let left = [-t[1], t[0]];
    let mut d = if dot(cd, left) >= 0.0 { cd } else { mul(cd, -1.0) };
    let c = dot(d, left);
    if c < prm[P_SKEW_MAX_COS] {
        // clamped SKEW_MAX_DEG off square, to the crossed line's side
        let sn = (1.0 - prm[P_SKEW_MAX_COS] * prm[P_SKEW_MAX_COS]).max(0.0).sqrt();
        let side = if dot(d, t) >= 0.0 { 1.0 } else { -1.0 };
        d = add(mul(left, prm[P_SKEW_MAX_COS]), mul(t, side * sn));
    }
    unit(d)
}

/// The shape of one deck (module doc). Returns (xy, z, ends (2 x 8: approach kind or -1, tangent x y, skew 0 / 1,
/// skew dir x y, blended 0 / 1, approach line index or -1), overlap polylines of both ends (flat x y), their offsets).
#[allow(clippy::type_complexity)]
fn shape_core(xy: &[P2], z: &[f64], others: &[Other], prm: &[f64])
              -> (Vec<P2>, Vec<f64>, [[f64; 8]; 2], Vec<P2>, [usize; 3]) {
    let deck = Line::new(xy);
    let mut ends = [[-1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0]; 2];
    let l = deck.len();
    // the input profile by arc (the input's own points: duplicates kept in z's order)
    let mut s_in = vec![0.0; xy.len()];
    for i in 1..xy.len() {
        s_in[i] = s_in[i - 1] + norm(sub(xy[i], xy[i - 1]));
    }
    if deck.p.len() < 2 || l < 1.0 {
        return (xy.to_vec(), z.to_vec(), ends, Vec::new(), [0, 0, 0]);
    }
    let mut tang: [Option<P2>; 2] = [None, None];
    let mut appr: [Option<(i64, P2, usize, usize)>; 2] = [None, None];
    for e in 0..2 {
        let Some(a) = approach(&deck, e, others, prm) else { continue };
        appr[e] = Some(a);
        let (kind, v, k, _oe) = a;
        // the deck's direction along itself at that end
        let along = if e == 0 { mul(v, -1.0) } else { v };
        let t = if kind == KIND_DECK {
            let h = prm[P_DIR].min(0.5 * l);
            let own = if e == 0 { deck.dir(0.5 * h, 0.5 * h) } else { deck.dir(l - 0.5 * h, 0.5 * h) };
            own.and_then(|o| unit(add(o, along)))
        } else {
            Some(along)
        };
        tang[e] = t;
        ends[e][0] = kind as f64;
        ends[e][7] = k as f64;
        if let Some(t) = t {
            ends[e][1] = t[0];
            ends[e][2] = t[1];
        }
    }
    let both = tang[0].is_some() && tang[1].is_some();
    let bmax = prm[P_BLEND_MAX];
    let blen = if both { bmax.min(0.5 * l) } else { bmax.min(l) };
    // the blends: (a, ta, b, tb) of each end, in the deck's direction
    let mut blend: [Option<(P2, P2, P2, P2)>; 2] = [None, None];
    for e in 0..2 {
        let Some(t) = tang[e] else { continue };
        let (a_arc, b_arc) = if e == 0 { (0.0, blen) } else { (l - blen, l) };
        let hd = 2.0f64.min(0.25 * blen);
        let (p_a, p_b) = (deck.at(a_arc), deck.at(b_arc));
        let scale = norm(sub(p_b, p_a));
        if scale < 1e-6 { continue; }
        let (ta, tb) = if e == 0 {
            (t, match deck.dir(b_arc, hd) { Some(d) => d, None => continue })
        } else {
            (match deck.dir(a_arc, hd) { Some(d) => d, None => continue }, t)
        };
        let (ta, tb) = (mul(ta, scale), mul(tb, scale));
        let dev_max = prm[P_DEV_MIN].max(prm[P_DEV_SHARE] * blen);
        let mut ok = true;
        for i in 1..16 {
            let q = hermite(p_a, ta, p_b, tb, i as f64 / 16.0);
            if deck.dist(q, a_arc, b_arc) > dev_max {
                ok = false;
                break;
            }
        }
        if ok {
            blend[e] = Some((p_a, ta, p_b, tb));
            ends[e][6] = 1.0;
        }
    }
    // the rows
    let (tol, step) = (prm[P_CHORD_TOL], prm[P_MAX_STEP]);
    let mut out: Vec<P2> = vec![deck.p[0]];
    let mid0 = if blend[0].is_some() { blen } else { 0.0 };
    let mid1 = if blend[1].is_some() { l - blen } else { l };
    if let Some((a, ta, b, tb)) = blend[0] {
        let h = move |u: f64| hermite(a, ta, b, tb, u);
        sample(&h, 0.0, a, 1.0, b, tol, step, 0, &mut out);
    }
    for i in 0..deck.p.len() {
        if deck.s[i] > mid0 + 1e-6 && deck.s[i] < mid1 - 1e-6 {
            out.push(deck.p[i]);
        }
    }
    if let Some((a, ta, b, tb)) = blend[1] {
        if norm(sub(a, *out.last().unwrap())) > 1e-6 { out.push(a); }
        let h = move |u: f64| hermite(a, ta, b, tb, u);
        sample(&h, 0.0, a, 1.0, b, tol, step, 0, &mut out);
    } else if norm(sub(deck.p[deck.p.len() - 1], *out.last().unwrap())) > 1e-6 {
        out.push(deck.p[deck.p.len() - 1]);
    }
    // a straight stretch between kept vertices longer than MAX_STEP split (rows every MAX_STEP at most)
    let mut rows: Vec<P2> = vec![out[0]];
    for w in out.windows(2) {
        let d = norm(sub(w[1], w[0]));
        let k = (d / step).ceil().max(1.0) as usize;
        for j in 1..k {
            rows.push(add(w[0], mul(sub(w[1], w[0]), j as f64 / k as f64)));
        }
        rows.push(w[1]);
    }
    let out = rows;
    // heights at the same share of the length
    let mut so = vec![0.0; out.len()];
    for i in 1..out.len() {
        so[i] = so[i - 1] + norm(sub(out[i], out[i - 1]));
    }
    let lo = so[so.len() - 1];
    let lin = s_in[s_in.len() - 1];
    let zo: Vec<f64> = so.iter().enumerate().map(|(i, &a)| {
        if i == 0 { return z[0]; }
        if i == so.len() - 1 { return z[z.len() - 1]; }
        let t = if lo > 0.0 { a / lo * lin } else { 0.0 };
        interp1(t, &s_in, z)
    }).collect();
    // skew and the overlaps on the new line
    let shaped = Line::new(&out);
    let mut ov: Vec<P2> = Vec::new();
    let mut ooff = [0usize; 3];
    for e in 0..2 {
        let skip: Vec<usize> = appr.iter().flatten().map(|a| a.2).collect();
        // (no skew where another deck goes on or ends: the joint's rows are both decks', square)
        let p_end = if e == 0 { shaped.p[0] } else { shaped.p[shaped.p.len() - 1] };
        let joint = others.iter().any(|o| o.kind == KIND_DECK && o.line.p.len() >= 2
            && [o.line.p[0], o.line.p[o.line.p.len() - 1]].iter().any(|&q| norm(sub(q, p_end)) <= prm[P_JOIN]));
        if joint { ooff[e + 1] = ov.len(); }
        if !joint { if let Some(d) = skew(&shaped, e, others, &skip, prm) {
            ends[e][3] = 1.0;
            ends[e][4] = d[0];
            ends[e][5] = d[1];
        } }
        if joint { continue; }
        if let Some((kind, _v, k, oe)) = appr[e] {
            if kind == KIND_ROAD {
                let o = &others[k].line;
                let ol = o.len();
                let reach = prm[P_OVERLAP].min(ol);
                let start = if e == 0 { shaped.p[0] } else { shaped.p[shaped.p.len() - 1] };
                ov.push(start);
                let stepn = (reach / 2.0).ceil().max(1.0) as usize;
                for j in 1..=stepn {
                    let a = reach * j as f64 / stepn as f64;
                    ov.push(if oe == 0 { o.at(a) } else { o.at(ol - a) });
                }
            }
        }
        ooff[e + 1] = ov.len();
    }
    (out, zo, ends, ov, ooff)
}

fn interp1(t: f64, xs: &[f64], ys: &[f64]) -> f64 {
    let n = xs.len();
    if t <= xs[0] { return ys[0]; }
    if t >= xs[n - 1] { return ys[n - 1]; }
    let i = xs.partition_point(|&v| v <= t).max(1) - 1;
    let d = xs[i + 1] - xs[i];
    if d <= 0.0 { return ys[i + 1]; }
    ys[i] + (ys[i + 1] - ys[i]) * ((t - xs[i]) / d)
}

/// roadshape.shape_chain: one deck's shape (module doc). xy (n, 2), z (n,); lines (m, 2) of the lines round it, line k
/// rows loff[k]..loff[k + 1]; lkind (L,) 0 ground road / 1 ground railway / 2 another deck of the chain; lown (L,) 1
/// for the deck's own feature; params (roadshape.PARAMS). Returns (xy flat (2 k), z (k,), ends flat (16: per end
/// approach kind or -1, tangent x y, skew 0 / 1, skew dir x y, blended 0 / 1, approach line or -1), the overlap
/// polylines flat (x y), their offsets (3,): end 0's points off[0]..off[1], end 1's off[1]..off[2]).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn deck_shape<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                       lines: PyReadonlyArray2<'py, f64>, loff: PyReadonlyArray1<'py, i64>,
                       lkind: PyReadonlyArray1<'py, i64>, lown: PyReadonlyArray1<'py, i64>,
                       params: PyReadonlyArray1<'py, f64>)
                       -> PyResult<(Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>, Vec<i64>)> {
    let (xy, z, lines, loff, lkind, lown, prm) = (xy.as_array(), z.as_array(), lines.as_array(),
        loff.as_array().to_vec(), lkind.as_array(), lown.as_array(), params.as_array().to_vec());
    let nl = loff.len().saturating_sub(1);
    if xy.ncols() != 2 || z.len() != xy.nrows() || xy.nrows() == 0 || lines.ncols() != 2 || prm.len() != NPARAMS
        || lkind.len() != nl || lown.len() != nl || loff.iter().any(|&v| v < 0 || v as usize > lines.nrows())
        || loff.windows(2).any(|w| w[1] < w[0]) || xy.iter().chain(z.iter()).any(|v| !v.is_finite()) {
        return Err(PyValueError::new_err("deck_shape: shapes, offsets or values do not agree"));
    }
    let pts: Vec<P2> = (0..xy.nrows()).map(|i| [xy[[i, 0]], xy[[i, 1]]]).collect();
    let zz: Vec<f64> = z.to_vec();
    let others: Vec<Other> = (0..nl).map(|k| {
        let rows: Vec<P2> = (loff[k] as usize..loff[k + 1] as usize).map(|r| [lines[[r, 0]], lines[[r, 1]]]).collect();
        Other { line: Line::new(&rows), kind: lkind[k], own: lown[k] != 0 }
    }).collect();
    let (out, zo, ends, ov, ooff) = py.detach(|| shape_core(&pts, &zz, &others, &prm));
    Ok((out.iter().flat_map(|p| [p[0], p[1]]).collect(), zo, ends.iter().flat_map(|e| e.iter().copied()).collect(),
        ov.iter().flat_map(|p| [p[0], p[1]]).collect(), ooff.iter().map(|&v| v as i64).collect()))
}

/// roadshape.simple_members: per member of structure_groups' output (root per member, dup per member, edges as
/// (a, b, kind) rows, kind per member), 1 when its group is simple (module doc), else 0.
#[pyfunction]
pub fn deck_simple(root: Vec<i64>, dup: Vec<i64>, edges: Vec<(i64, i64, i64)>, kind: Vec<i64>) -> PyResult<Vec<i64>> {
    let n = root.len();
    if dup.len() != n || kind.len() != n || root.iter().chain(dup.iter()).any(|&r| r < 0 || r as usize >= n)
        || edges.iter().any(|e| e.0 < 0 || e.1 < 0 || e.0 as usize >= n || e.1 as usize >= n) {
        return Err(PyValueError::new_err("deck_simple: shapes or indices do not agree"));
    }
    let mut size = vec![0usize; n];         // per group root: its members (duplicates not counted)
    let mut joins = vec![0usize; n];        // ... its JOIN edges
    let mut deg = vec![0usize; n];          // per member: its JOIN edges
    let mut bad = vec![false; n];
    for k in 0..n {
        let g = root[k] as usize;
        if dup[k] as usize == k { size[g] += 1; }
        if kind[k] != ROAD { bad[g] = true; }
    }
    for &(a, b, kd) in &edges {
        let (ga, gb) = (root[a as usize] as usize, root[b as usize] as usize);
        if ga != gb { continue; }
        if kd == END_ON as i64 || kd == OVERLAP_ALONG as i64 || kd == OVERLAP_CROSS as i64 {
            bad[ga] = true;
        } else if kd == JOIN as i64 {
            joins[ga] += 1;
            deg[a as usize] += 1;
            deg[b as usize] += 1;
        }
    }
    for k in 0..n {
        if deg[k] > 2 { bad[root[k] as usize] = true; }        // (a member joined at three ends or more: a fork)
    }
    Ok((0..n).map(|k| {
        let g = root[k] as usize;
        i64::from(!bad[g] && joins[g] < size[g].max(1))         // (its joints a path: no fork, no ring)
    }).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn prm() -> Vec<f64> {
        let c = |d: f64| { let r = d / 180.0 * std::f64::consts::PI; 1.0 - r * r / 2.0 + r * r * r * r / 24.0 };
        let s = |d: f64| { let r = d / 180.0 * std::f64::consts::PI; r - r * r * r / 6.0 + r * r * r * r * r / 120.0 };
        vec![0.02, 4.0, 30.0, c(25.0), 0.75, 10.0, 40.0, s(15.0), c(60.0), c(30.0), 0.5, 0.1, c(10.0), 10.0, 1.0]
    }

    #[test]
    fn chord_deck_takes_the_approach_tangent() {
        // a straight 30 m deck whose approach arrives turned 10 degrees: the deck leaves along it, no corner
        let deck: Vec<P2> = vec![[0.0, 0.0], [30.0, 0.0]];
        let a = 10.0f64 / 180.0 * std::f64::consts::PI;
        let (ca, sa) = (1.0 - a * a / 2.0, a - a * a * a / 6.0);
        let appr = Other { line: Line::new(&[[0.0, 0.0], [-20.0 * ca, 20.0 * sa]]), kind: KIND_ROAD, own: true };
        let (out, z, ends, ov, ooff) = shape_core(&deck, &[1.0, 2.0], &[appr], &prm());
        assert_eq!(ends[0][6], 1.0);
        let d0 = unit(sub(out[1], out[0])).unwrap();
        assert!(dot(d0, [ca, -sa]) > 0.9995, "start direction {:?}", d0);
        assert_eq!(out[0], [0.0, 0.0]);
        assert_eq!(*out.last().unwrap(), [30.0, 0.0]);
        assert_eq!(z[0], 1.0);
        assert_eq!(*z.last().unwrap(), 2.0);
        assert!(out.windows(2).all(|w| norm(sub(w[1], w[0])) <= 4.0 + 1e-9));
        assert_eq!(ooff[1], ov.len());
        assert!(ooff[1] >= 2);
    }

    #[test]
    fn simple_groups() {
        // 0-1 JOIN (simple), 2-3 END_ON (not), 4 alone (simple), 5 a walk (not)
        let root = vec![0, 0, 2, 2, 4, 5];
        let dup = vec![0, 1, 2, 3, 4, 5];
        let edges = vec![(0, 1, 0), (2, 3, END_ON as i64)];
        let kind = vec![ROAD, ROAD, ROAD, ROAD, ROAD, 1];
        assert_eq!(deck_simple(root, dup, edges, kind).unwrap(), vec![1, 1, 0, 0, 1, 0]);
        // a fork: three members joined pairwise (not)
        assert_eq!(deck_simple(vec![0, 0, 0], vec![0, 1, 2], vec![(0, 1, 0), (0, 2, 0), (1, 2, 0)],
                               vec![ROAD, ROAD, ROAD]).unwrap(), vec![0, 0, 0]);
    }
}
