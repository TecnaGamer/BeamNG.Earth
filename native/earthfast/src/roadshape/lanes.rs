//! lane_graph (earthfast 538, road surface v3 phase S1, BNE_ROADSURF_V3=shadow; SHADOW ONLY: nothing built reads it):
//! the lane graph of the one continuous road surface (.claude/work/road-surface-v3-plan-2026-11-19.md §2,
//! road-surface-architecture-research-2026-11-19.md §2.1-2.2) from the map alone, for one z14 cell's home nodes and
//! segments (lanegraph.cell_lanes: the cell's nodes, the roads within LANE_REACH of them).
//!
//! STROKES (lane identity, never a frame): a walk from a node goes on through plain continuations (a connector of two
//! arms of one flow) and through the THROUGH arm of a one-way fork (forks.rs's local rule: same class not a ramp, then
//! not a ramp, then the smaller turn, then more lanes, then the lower segment id), up to REACH_M: a function of the map
//! within REACH_M of the node (order-free by construction). Each home node's links name, per way through it, the
//! segment before and after and the lane shift across the node (lane i before is lane i + shift after, lanes counted
//! from the left in travel), so one lane's identity runs through a whole road without a global walk.
//!
//! ANCHOR: the boundary that stays put is the through lanes' edge away from the event (a right-side ramp: the left
//! edge). `placement=*` says where the mapped line lies in the lanes (left_of:k, middle_of:k, right_of:k; none or
//! transition: the centre), so each piece's anchor offset from its mapped line is known; where it changes at a joint
//! it is blended over SHIFT_M (quintic). Boundary offsets t_k(s) = anchor(s) -/+ the sum of the lane widths between
//! the anchor and k: ORDERED BY CONSTRUCTION (every lane width >= 0), so lanes never overlap.
//!
//! EVENTS at home connectors (stations s in metres along the main path in the direction of travel, the node at 0):
//! - DIVERGE / MERGE: a connector of three one-way arms of the fork classes, both of the pair within FORK_DEG of the
//!   trunk and a ramp among them or both motorway / trunk (forks.rs's test; at-grade slip lanes skipped).
//!   GORE FROM GEOMETRY: the branch's mapped line projected on the through path gives d(s); the THEORETICAL GORE s_g is
//!   where |d| first stays at or above the near through edge's offset plus half the branch's lanes (the ramp's lane
//!   wholly outside the main's lanes). Until there the ramp is a lane of the main road, wherever OSM put the node. The
//!   PAINTED GORE runs from s_g to the PHYSICAL NOSE s_g +/- gore_m (the TaperRule table by region and branch lanes).
//!   The branch lane's other end, by evidence:
//!   * "lanes": the trunk carries the branch's lanes (lT >= lM + lB): the lane runs through the trunk's pieces to
//!     where a piece has fewer (an add: its own taper, add_place of it before that joint; a merge: an auxiliary
//!     lane from an entrance); beyond REACH_M: "exit_only" (the lane runs on);
//!   * "turn_lanes": the trunk's turn:lanes give its outer lanes to the branch alone: the same walk on that evidence;
//!   * "lane_sum" (merges only): a ramp of two or more lanes whose lanes the road after it does not carry, and a
//!     diverge on the same side within AUX_MAX_M after it: one auxiliary lane from the gore to that diverge;
//!   * "design": the region's parallel speed-change lane (dec_par / acc_par) and its taper; a ramp splitting from a
//!     ramp: one taper-type divergence (div_k).
//! - ADD / DROP: a connector of two arms of one flow whose lane counts differ (one-way: the carriageway; two-way: each
//!   direction). The side: placement, then turn:lanes (merge_to_left / merge_to_right; a turn bay's left / right),
//!   then the side of a ramp event on the stroke within FEED_M, then the outer side (right; left in left-hand
//!   traffic). Length: the TaperRule's (an exit's entry taper when a diverge on that side follows within FEED_M, an
//!   entrance's merging taper when a merge precedes, else the lane drop rule: MUTCD L = W S (>= 45 mph), W S^2 / 60).
//!
//! TaperRule table (lanegraph.TAPER_TABLE, flat, REGIONS x T_N): per region (0 default, 1 US, 2 DE, 3 GB, 4 NL, 5 CA)
//! the lane width, exit taper (k per m of lateral change, at least min), parallel deceleration / acceleration lane,
//! entrance taper, taper-type divergence, painted gore (one lane, each further lane), the drop rule, where an add's
//! taper lies about its joint, left-hand traffic and a taper-type entrance's length.
//!
//! Output: one JSON object {"events": [...], "segs": [...]}, events sorted by connector id, segments by id; integers
//! (mm, 1e-6 for unit normals) only. Each event's rows (every OUT_M from its first to its last station): s, the main
//! line's point, its left unit normal, then the boundary offsets t_0 >= t_1 >= ... (left edge first), with "strips"
//! naming the lanes between them. No FMA, no libm, no hash maps: sqrt (correctly rounded) and + - * / only, in a
//! fixed order.
use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use super::forks::{exit_lanes, json_str, quintic};

// params (lanegraph.LANE_PARAMS, in this order)
const P_REACH: usize = 0;
const P_DIR: usize = 1;
const P_FORK_COS: usize = 2;
const P_STEP: usize = 3;
const P_OUT: usize = 4;
const P_SHIFT: usize = 5;
const P_AUX_MAX: usize = 6;
const P_FEED: usize = 7;
const P_NOSE_W: usize = 8;
const P_AT_GRADE: usize = 9;
const P_MARGIN: usize = 10;
const NPARAMS: usize = 11;

// the TaperRule table's fields (lanegraph.TAPER_FIELDS, in this order)
const T_LANE_W: usize = 0;
const T_EXIT_K: usize = 1;
const T_EXIT_MIN: usize = 2;
const T_DEC_PAR: usize = 3;
const T_ACC_PAR: usize = 4;
const T_ENTRY_K: usize = 5;
const T_ENTRY_MIN: usize = 6;
const T_DIV_K: usize = 7;
const T_GORE_1: usize = 8;
const T_GORE_X: usize = 9;
const T_DROP_K: usize = 10;
const T_ADD_PLACE: usize = 11;
const T_LHT: usize = 12;
const T_TAPER_MERGE: usize = 13;
const T_GORE_IN: usize = 14;
const T_N: usize = 15;

const NOSE_HOLD_M: f64 = 20.0;      // m of branch the lines stay clearly apart before the gore's scan stops
const MAX_PIECES: usize = 64;
const CLS_TRUNK: i64 = 2;
const MPH: f64 = 1.609344;          // km per mile

type P2 = [f64; 2];

fn sub(a: P2, b: P2) -> P2 { [a[0] - b[0], a[1] - b[1]] }
fn dot(a: P2, b: P2) -> f64 { a[0] * b[0] + a[1] * b[1] }
fn cross(a: P2, b: P2) -> f64 { a[0] * b[1] - a[1] * b[0] }
fn norm(a: P2) -> f64 { (a[0] * a[0] + a[1] * a[1]).sqrt() }
fn unit(a: P2) -> Option<P2> {
    let n = norm(a);
    if n > 1e-9 { Some([a[0] / n, a[1] / n]) } else { None }
}
fn lerp(a: P2, b: P2, u: f64) -> P2 { [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u] }
fn mm(v: f64) -> i64 { (v * 1000.0).round() as i64 }
fn clampf(v: f64, lo: f64, hi: f64) -> f64 { if v < lo { lo } else if v > hi { hi } else { v } }

/// A quintic piece's value at s: v0 before s0, v1 after s1, the smoothstep between.
fn piece(s: f64, s0: f64, s1: f64, v0: f64, v1: f64) -> f64 {
    if s1 <= s0 { return if s < s0 { v0 } else { v1 }; }
    v0 + (v1 - v0) * quintic((s - s0) / (s1 - s0))
}

struct Seg {
    pts: Vec<P2>,
    cls: i64,
    link: bool,
    dir: i64,
    lanes: i64,
    lfw: i64,
    lbw: i64,
    kmh: f64,
    lane_w: f64,       // the lane width of a road below the region table's classes (Python: drive / lanes)
    turn: String,
    turn_bw: String,
    place: String,
    region: usize,
    fid: String,
    nodes: Vec<(usize, usize)>,
}

impl Seg {
    /// Lanes in the direction of travel along (fwd) or against the points; -1: unknown.
    fn lanes_dir(&self, fwd: bool) -> i64 {
        if self.dir != 0 { return self.lanes; }
        let (a, b) = if fwd { (self.lfw, self.lbw) } else { (self.lbw, self.lfw) };
        if a >= 1 { return a; }
        if self.lanes >= 2 && b < 1 { return self.lanes / 2 + if fwd { self.lanes % 2 } else { 0 }; }
        if self.lanes >= 1 && b >= 1 && self.lanes > b { return self.lanes - b; }
        -1
    }
    fn turn_dir(&self, fwd: bool) -> &str { if self.dir != 0 || fwd { &self.turn } else { &self.turn_bw } }
}

struct Ctx<'a> {
    segs: &'a [Seg],
    rows: &'a [Vec<(usize, usize)>],
    prm: &'a [f64],
    tab: &'a [f64],
}

impl Ctx<'_> {
    fn t(&self, region: usize, f: usize) -> f64 { self.tab[region * T_N + f] }
    /// A lane's width on segment g (the region's for motorway / trunk, else the road's own).
    fn lane_w(&self, g: usize) -> f64 {
        let s = &self.segs[g];
        if s.cls >= 1 && s.cls <= CLS_TRUNK { self.t(s.region, T_LANE_W) } else { s.lane_w }
    }
}

#[derive(Clone, Copy)]
struct Piece { seg: usize, fwd: bool, s0: f64, s1: f64, end_node: Option<usize> }

/// A walk away from a node along the stroke: its polyline (from the node), arc lengths and pieces.
struct Path { p: Vec<P2>, s: Vec<f64>, pieces: Vec<Piece> }

impl Path {
    fn len(&self) -> f64 { *self.s.last().unwrap_or(&0.0) }
    fn at(&self, t: f64) -> (P2, P2) {
        let n = self.p.len();
        if n < 2 { return (self.p[0], [1.0, 0.0]); }
        let t = clampf(t, 0.0, self.len());
        let mut k = 0;
        while k + 2 < n && self.s[k + 1] < t { k += 1; }
        let l = self.s[k + 1] - self.s[k];
        let u = if l > 0.0 { (t - self.s[k]) / l } else { 0.0 };
        (lerp(self.p[k], self.p[k + 1], u), unit(sub(self.p[k + 1], self.p[k])).unwrap_or([1.0, 0.0]))
    }
    /// (arc length, signed offset + left of the walk) of the nearest point to q; ties: the first piece.
    fn project(&self, q: P2) -> (f64, f64) {
        let mut best = (f64::INFINITY, 0.0, 0.0);
        for k in 0..self.p.len().saturating_sub(1) {
            let (a, b) = (self.p[k], self.p[k + 1]);
            let ab = sub(b, a);
            let l2 = dot(ab, ab);
            let u = if l2 > 0.0 { clampf(dot(sub(q, a), ab) / l2, 0.0, 1.0) } else { 0.0 };
            let m = lerp(a, b, u);
            let d = norm(sub(q, m));
            if d < best.0 {
                let side = if cross(ab, sub(q, a)) < 0.0 { -1.0 } else { 1.0 };
                best = (d, self.s[k] + (self.s[k + 1] - self.s[k]) * u, side * d);
            }
        }
        (best.1, best.2)
    }
}

/// Arms of node n: (segment, vertex, forward) per arm, in the node's row order; roads below the fork classes (cls 0:
/// paths, service roads, unpaved) are left out (a footway's end on a motorway's split node kept Utrecht's A27 exit
/// from being a fork).
fn arms_of(segs: &[Seg], rows: &[(usize, usize)]) -> Vec<(usize, usize, bool)> {
    let mut out = Vec::new();
    for &(g, v) in rows {
        if segs[g].cls <= 0 { continue; }
        if v > 0 { out.push((g, v, false)); }
        if v + 1 < segs[g].pts.len() { out.push((g, v, true)); }
    }
    out
}

fn flow_of(seg: &Seg, fwd: bool) -> i64 {
    match (seg.dir, fwd) { (1, true) | (-1, false) => 1, (1, false) | (-1, true) => -1, _ => 0 }
}

/// The unit direction of an arm from its node, read over at most dir_m of its own segment.
fn arm_dir(seg: &Seg, v: usize, fwd: bool, dir_m: f64) -> Option<P2> {
    let p0 = seg.pts[v];
    let mut i = v;
    let mut q = p0;
    let mut got = 0.0;
    loop {
        let j = if fwd { i + 1 } else { i.wrapping_sub(1) };
        if j >= seg.pts.len() { break; }
        let l = norm(sub(seg.pts[j], seg.pts[i]));
        if got + l >= dir_m {
            let u = if l > 0.0 { (dir_m - got) / l } else { 0.0 };
            q = lerp(seg.pts[i], seg.pts[j], u);
            break;
        }
        got += l;
        q = seg.pts[j];
        i = j;
    }
    unit(sub(q, p0))
}

/// A one-way fork's roles at node n: (trunk, through, branch) arm indices into arms_of and whether it diverges; None:
/// not three one-way arms of the fork classes, or the pair not within FORK_DEG of the trunk (forks.rs's rule).
fn fork_roles(c: &Ctx, n: usize) -> Option<(Vec<(usize, usize, bool)>, usize, usize, usize, bool)> {
    let arms = arms_of(c.segs, &c.rows[n]);
    if arms.len() != 3 { return None; }
    if arms.iter().any(|&(g, _, f)| flow_of(&c.segs[g], f) == 0 || c.segs[g].cls <= 0) { return None; }
    let n_out = arms.iter().filter(|&&(g, _, f)| flow_of(&c.segs[g], f) > 0).count();
    let diverge = match n_out { 2 => true, 1 => false, _ => return None };
    let ti = arms.iter().position(|&(g, _, f)| (flow_of(&c.segs[g], f) > 0) != diverge)?;
    let others: Vec<usize> = (0..3).filter(|&k| k != ti).collect();
    let dir = |k: usize| { let (g, v, f) = arms[k]; arm_dir(&c.segs[g], v, f, c.prm[P_DIR]) };
    let dt = dir(ti)?;
    let cont = [-dt[0], -dt[1]];
    let st = &c.segs[arms[ti].0];
    let key = |k: usize| {
        let s = &c.segs[arms[k].0];
        let cs = dir(k).map(|d| dot(cont, d)).unwrap_or(-2.0);
        (s.cls == st.cls && !s.link, !s.link, cs, s.lanes)
    };
    let (ka, kc) = (key(others[0]), key(others[1]));
    if ka.2 < c.prm[P_FORK_COS] || kc.2 < c.prm[P_FORK_COS] { return None; }
    let a_first = if ka.0 != kc.0 { ka.0 } else if ka.1 != kc.1 { ka.1 } else if ka.2 != kc.2 { ka.2 > kc.2 }
                  else if ka.3 != kc.3 { ka.3 > kc.3 } else { c.segs[arms[others[0]].0].fid <= c.segs[arms[others[1]].0].fid };
    let (mi, bi) = if a_first { (others[0], others[1]) } else { (others[1], others[0]) };
    Some((arms, ti, mi, bi, diverge))
}

/// The stroke from node `start` along arm (g0, v0, fwd0) up to reach: through plain continuations of one flow and
/// through a one-way fork's through arm (arriving on its trunk: on along the through; arriving on the through: on
/// along the trunk; arriving on the branch: stop).
fn walk(c: &Ctx, start: usize, g0: usize, v0: usize, fwd0: bool, reach: f64) -> Path {
    let segs = c.segs;
    let flow = flow_of(&segs[g0], fwd0);
    let mut path = Path { p: vec![segs[g0].pts[v0]], s: vec![0.0], pieces: Vec::new() };
    let (mut g, mut v, mut fwd) = (g0, v0, fwd0);
    let mut seen: Vec<usize> = vec![start];
    'outer: loop {
        let seg = &segs[g];
        let s_start = path.len();
        let mut i = v;
        loop {
            let j = if fwd { i + 1 } else { i.wrapping_sub(1) };
            if j >= seg.pts.len() { break; }
            let (a, b) = (seg.pts[i], seg.pts[j]);
            let l = norm(sub(b, a));
            let s0 = path.len();
            if s0 + l >= reach {
                let u = if l > 0.0 { (reach - s0) / l } else { 0.0 };
                path.p.push(lerp(a, b, u));
                path.s.push(reach);
                path.pieces.push(Piece { seg: g, fwd, s0: s_start, s1: reach, end_node: None });
                break 'outer;
            }
            if l > 0.0 { path.p.push(b); path.s.push(s0 + l); }
            i = j;
            if let Ok(k) = seg.nodes.binary_search_by(|e| e.0.cmp(&i)) {
                let n = seg.nodes[k].1;
                path.pieces.push(Piece { seg: g, fwd, s0: s_start, s1: path.len(), end_node: Some(n) });
                if seen.contains(&n) || path.pieces.len() >= MAX_PIECES { break 'outer; }
                seen.push(n);
                let all = arms_of(segs, &c.rows[n]);
                let others: Vec<(usize, usize, bool)> = all.iter().copied()
                    .filter(|&(h, w, f)| !(h == g && w == i && f != fwd)).collect();
                let next = if others.len() == 1 {
                    let (h, w, f) = others[0];
                    if flow_of(&segs[h], f) != flow || (segs[h].dir == 0) != (seg.dir == 0) { break 'outer; }
                    Some((h, w, f))
                } else if others.len() == 2 {
                    match fork_roles(c, n) {
                        Some((arms, ti, mi, _bi, _dv)) => {
                            let me = all.iter().position(|&(h, w, f)| h == g && w == i && f != fwd);
                            match me {
                                Some(k) if k == ti => Some(arms[mi]),
                                Some(k) if k == mi => Some(arms[ti]),
                                _ => None,
                            }
                        }
                        None => None,
                    }
                } else { None };
                match next {
                    Some((h, w, f)) => { g = h; v = w; fwd = f; continue 'outer; }
                    None => break 'outer,
                }
            }
        }
        path.pieces.push(Piece { seg: g, fwd, s0: s_start, s1: path.len(), end_node: None });
        break;
    }
    path
}

/// The main path through an event, in the direction of travel: `up` walked upstream from the node, `dn` downstream.
struct Main<'a> { up: &'a Path, dn: &'a Path }

impl Main<'_> {
    fn at(&self, s: f64) -> (P2, P2) {
        if s < 0.0 { let (p, t) = self.up.at(-s); (p, [-t[0], -t[1]]) } else { self.dn.at(s) }
    }
    fn lo(&self) -> f64 { -self.up.len() }
    fn hi(&self) -> f64 { self.dn.len() }
}

/// The left edge's offset from a piece's mapped line (+ left of travel) for n lanes of width w: placement=left_of:k,
/// middle_of:k, right_of:k; none (or transition, or a count it cannot hold): the centre.
fn left_off(place: &str, n: i64, w: f64) -> f64 {
    let n = if n < 1 { 1 } else { n };
    let centre = n as f64 * w / 2.0;
    let mut it = place.splitn(2, ':');
    let (kind, k) = (it.next().unwrap_or(""), it.next().and_then(|v| v.trim().parse::<i64>().ok()));
    match (kind, k) {
        ("left_of", Some(k)) if k >= 1 && k <= n + 1 => (k - 1) as f64 * w,
        ("middle_of", Some(k)) if k >= 1 && k <= n => (k as f64 - 0.5) * w,
        ("right_of", Some(k)) if k >= 1 && k <= n => k as f64 * w,
        _ => centre,
    }
}

/// The anchor's offset from the main line along s: per piece the anchored edge (left: the left edge; right: the
/// right edge) for that piece's lanes, blended over SHIFT_M about each joint where it changes.
struct Anchor { a0: f64, steps: Vec<(f64, f64)> }

impl Anchor {
    fn new(c: &Ctx, m: &Main, left: bool, w: f64) -> Anchor {
        let edge = |pc: &Piece| {
            let s = &c.segs[pc.seg];
            let n = s.lanes_dir(pc.fwd);
            let n = if n >= 1 { n } else { 1 };
            if s.dir == 0 { return 0.0; }   // (a two-way road: its centre, the mapped line)
            let l = left_off(&s.place, n, w);
            if left { l } else { l - n as f64 * w }
        };
        // pieces in travel order: the upstream walk reversed, then the downstream
        let mut seq: Vec<(f64, f64)> = Vec::new();     // (piece start s, edge)
        for pc in m.up.pieces.iter().rev() { seq.push((-pc.s1, edge(pc))); }
        for pc in m.dn.pieces.iter() { seq.push((pc.s0, edge(pc))); }
        let a0 = seq.first().map(|v| v.1).unwrap_or(0.0);
        let mut steps = Vec::new();
        for k in 1..seq.len() {
            let d = seq[k].1 - seq[k - 1].1;
            if d != 0.0 { steps.push((seq[k].0, d)); }
        }
        Anchor { a0, steps }
    }
    fn at(&self, s: f64, shift: f64) -> f64 {
        let mut a = self.a0;
        for &(sb, d) in &self.steps { a += d * quintic((s - (sb - shift / 2.0)) / shift); }
        a
    }
}

/// The lane widths of an event's strips at s (left to right) and its rows.
struct Strips { names: Vec<&'static str>, w: Vec<Box<dyn Fn(f64) -> f64>> }

fn rows_json(m: &Main, an: &Anchor, left_anchor: bool, strips: &Strips, s_lo: f64, s_hi: f64, step: f64, shift: f64,
             flags: &mut Vec<&'static str>) -> String {
    let mut out = String::new();
    let mut s = s_lo;
    let mut first = true;
    let mut unordered = false;
    loop {
        let (p, tg) = m.at(s);
        let nrm = [-tg[1], tg[0]];
        let a = an.at(s, shift);
        let ws: Vec<f64> = strips.w.iter().map(|f| { let v = f(s); if v > 0.0 { v } else { 0.0 } }).collect();
        let mut t = vec![0.0; ws.len() + 1];
        if left_anchor {
            t[0] = a;
            for k in 0..ws.len() { t[k + 1] = t[k] - ws[k]; }
        } else {
            let n = ws.len();
            t[n] = a;
            for k in (0..n).rev() { t[k] = t[k + 1] + ws[k]; }
        }
        let tm: Vec<i64> = t.iter().map(|&v| mm(v)).collect();
        if tm.windows(2).any(|x| x[0] < x[1]) { unordered = true; }
        if !first { out.push(','); }
        first = false;
        out.push_str(&format!("[{},{},{},{},{}", mm(s), mm(p[0]), mm(p[1]), (nrm[0] * 1e6).round() as i64,
                              (nrm[1] * 1e6).round() as i64));
        for v in tm { out.push_str(&format!(",{}", v)); }
        out.push(']');
        if s >= s_hi { break; }
        s += step;
        if s > s_hi { s = s_hi; }
    }
    if unordered { flags.push("unordered"); }
    out
}

/// The MUTCD lane reduction taper (m) for a lateral change of w m at v km/h: L = W S (S >= 45 mph), W S^2 / 60 below.
fn mutcd_m(w: f64, kmh: f64) -> f64 {
    let s = kmh / MPH;
    if s >= 45.0 { w * s } else { w * s * s / 60.0 }
}

fn speed_share(kmh: f64) -> f64 { clampf(kmh / 80.0, 0.5, 1.0) }

/// The event finder of the add / drop and fork kinds within FEED_M along a path from its start: (station, node,
/// diverge, side +1 right / -1 left as the driver sees it) of the first one-way fork on it.
fn fork_along(c: &Ctx, p: &Path, max_s: f64) -> Option<(f64, usize, bool, f64)> {
    for pc in &p.pieces {
        if pc.s1 > max_s { break; }
        if let Some(n) = pc.end_node {
            if let Some((arms, ti, mi, bi, dv)) = fork_roles(c, n) {
                let _ = ti;
                let (gm, vm, fm) = arms[mi];
                let (gb, vb, fb) = arms[bi];
                let (dm, db) = (arm_dir(&c.segs[gm], vm, fm, c.prm[P_DIR])?, arm_dir(&c.segs[gb], vb, fb, c.prm[P_DIR])?);
                // the branch's side seen along the through arm's walk (a merge's arms point upstream: flip)
                let x = cross(dm, db);
                let right_of_walk = x < 0.0;
                let right = if dv { right_of_walk } else { !right_of_walk };
                return Some((pc.s1, n, dv, if right { 1.0 } else { -1.0 }));
            }
        }
    }
    None
}

fn region_of(c: &Ctx, g: usize) -> usize { c.segs[g].region }

#[allow(clippy::too_many_arguments)]
fn head_json(node_xy: P2, kind: &str, side_right: bool, region: usize, kmh: f64, lanes: [i64; 3], w: f64,
             fids: [&str; 3]) -> String {
    format!("\"node_xy\":[{},{}],\"kind\":\"{}\",\"side\":\"{}\",\"region\":{},\"kmh\":{},\"lanes\":[{},{},{}],\
             \"lane_w\":{},\"segs\":[{},{},{}]",
            mm(node_xy[0]), mm(node_xy[1]), kind, if side_right { "right" } else { "left" }, region,
            kmh.round() as i64, lanes[0], lanes[1], lanes[2], mm(w), json_str(fids[0]), json_str(fids[1]),
            json_str(fids[2]))
}

fn stations_json(st: &[(&str, f64)]) -> String {
    st.iter().map(|(k, v)| format!("\"{}\":{}", k, mm(*v))).collect::<Vec<_>>().join(",")
}

fn flags_json(f: &[&str]) -> String { f.iter().map(|v| format!("\"{}\"", v)).collect::<Vec<_>>().join(",") }

/// The fork event at node n (module doc), or None.
fn fork_event(c: &Ctx, n: usize) -> Option<String> {
    let (arms, ti, mi, bi, diverge) = fork_roles(c, n)?;
    let prm = c.prm;
    let reach = prm[P_REACH];
    let wk = |k: usize| { let (g, v, f) = arms[k]; walk(c, n, g, v, f, reach) };
    let (pt_, pm_, pb_) = (wk(ti), wk(mi), wk(bi));
    let (st, sm, sb) = (&c.segs[arms[ti].0], &c.segs[arms[mi].0], &c.segs[arms[bi].0]);
    if !(sm.link || sb.link || (sm.cls <= CLS_TRUNK && sb.cls <= CLS_TRUNK)) { return None; }
    let main = if diverge { Main { up: &pt_, dn: &pm_ } } else { Main { up: &pm_, dn: &pt_ } };
    let node = c.segs[arms[ti].0].pts[arms[ti].1];
    let region = region_of(c, arms[ti].0);
    let w = c.lane_w(arms[mi].0);
    let (lt, lm, lb0) = (st.lanes, sm.lanes, sb.lanes);
    let lb = if lb0 >= 1 { lb0 } else { 1 };
    let lm_e = if lm >= 1 { lm } else if lt >= 1 && lt > lb { lt - lb } else { 2 };
    // the branch's mapped line on the through path: (s on the branch, s along the through path, offset)
    let half = &pm_;
    let mut samples: Vec<(f64, f64, f64)> = Vec::new();
    let mut t = prm[P_STEP];
    let mut side = 0.0;
    while t <= pb_.len() {
        let (p, _) = pb_.at(t);
        let (s, d) = half.project(p);
        if s >= half.len() - 1e-6 { break; }
        if side == 0.0 && (if d < 0.0 { -d } else { d }) >= 1.0 { side = if d < 0.0 { -1.0 } else { 1.0 }; }
        samples.push((t, s, d));
        t += prm[P_STEP];
    }
    if side == 0.0 {
        let (gm, vm, fm) = arms[mi];
        let (gb, vb, fb) = arms[bi];
        let x = cross(arm_dir(&c.segs[gm], vm, fm, prm[P_DIR])?, arm_dir(&c.segs[gb], vb, fb, prm[P_DIR])?);
        side = if x < 0.0 { -1.0 } else { 1.0 };
    }
    // the side as the driver sees it (a merge's walk runs upstream: its left is the driver's right)
    let right = (side < 0.0) == diverge;
    let kmh = sm.kmh;
    let mut flags: Vec<&'static str> = Vec::new();
    let kind = if diverge { "diverge" } else { "merge" };
    let head = head_json(node, kind, right, region, kmh, [lt, lm, lb0], w, [&st.fid, &sm.fid, &sb.fid]);
    // at grade: a slip lane of a junction zone (forks.rs), no record
    let motorway = |s: &Seg| s.cls == 1 && !s.link;
    if !(motorway(st) && motorway(sm)) {
        for p in [&pt_, &pm_, &pb_] {
            if p.len() <= prm[P_AT_GRADE] {
                if let Some(e) = p.pieces.last().and_then(|pc| pc.end_node) {
                    if c.rows[e].iter().any(|&(g, _)| c.segs[g].dir == 0 && c.segs[g].cls > 0) {
                        return Some(format!("{},\"flags\":[\"at_grade\"]}}", head));
                    }
                }
            }
        }
    }
    // the near through edge's offset from the through path's line (its first piece's placement), the gore's offset
    let lo = left_off(&sm.place, lm_e, w);
    let near = if right { lm_e as f64 * w - lo } else { lo };    // (toward the branch, from the through line)
    let thr = near + lb as f64 * w / 2.0;
    let mut sg: Option<f64> = None;
    let mut apart: Option<f64> = None;
    for &(tb, s, d) in &samples {
        let dd = d * side;
        if dd < thr { sg = None; } else if sg.is_none() { sg = Some(s); }
        if dd < thr + prm[P_NOSE_W] + 3.0 { apart = None; } else if apart.is_none() { apart = Some(tb); }
        if let Some(a) = apart { if tb - a >= NOSE_HOLD_M { break; } }
    }
    let sg = match sg { Some(v) => v, None => return Some(format!("{},\"flags\":[\"no_separation\"]}}", head)) };
    // (an entrance's painted gore, nose to merge point, is shorter than an exit's: measured 15-60 m vs 85-180 m)
    let gore_m = if diverge { c.t(region, T_GORE_1) } else { c.t(region, T_GORE_IN) } + (lb - 1) as f64 * c.t(region, T_GORE_X);
    // travel stations: a diverge's gore after the node, a merge's before it
    let s_g = if diverge { sg } else { -sg };
    let mut s_n = if diverge { s_g + gore_m } else { s_g - gore_m };
    if s_n > main.hi() { s_n = main.hi(); flags.push("nose_beyond_reach"); }
    if s_n < main.lo() { s_n = main.lo(); flags.push("nose_beyond_reach"); }
    // the branch lane's other end
    let share = speed_share(kmh);
    let exit_taper = { let v = c.t(region, T_EXIT_K) * w; let m0 = c.t(region, T_EXIT_MIN); if v > m0 { v } else { m0 } };
    let entry_taper = { let v = c.t(region, T_ENTRY_K) * w; let m0 = c.t(region, T_ENTRY_MIN); if v > m0 { v } else { m0 } };
    let mut evidence = "design";
    let mut typ = "parallel";
    let (mut taper0, mut taper1, mut par0, mut par1);
    let need = lm_e + lb;
    let tl = exit_lanes(st.turn_dir(true), !right);
    if lt >= 1 && lm >= 1 && lt >= need || (diverge && st.dir != 0 && tl >= 1 && tl >= lb) {
        evidence = if lt >= 1 && lm >= 1 && lt >= need { "lanes" } else { "turn_lanes" };
        // the trunk's pieces away from the node until one lacks the lane
        let tp = &pt_;
        let has = |g: usize| {
            let s = &c.segs[g];
            s.lanes < 1 || s.lanes >= need || (evidence == "turn_lanes" && exit_lanes(&s.turn, !right) >= lb)
        };
        let mut found: Option<(f64, Option<usize>)> = None;
        for (i, pc) in tp.pieces.iter().enumerate() {
            if !has(pc.seg) { found = Some((pc.s0, None)); break; }
            // a fork at its far joint where the lane came from (diverge) or goes to (merge) a ramp: an auxiliary lane
            if let (Some(nn), Some(q)) = (pc.end_node, tp.pieces.get(i + 1)) {
                if !has(q.seg) && fork_roles(c, nn).is_some() { found = Some((pc.s1, Some(nn))); break; }
            }
        }
        if diverge {
            match found {
                Some((s0, via_fork)) => {
                    let s_add = -s0;
                    if via_fork.is_some() {
                        typ = "aux";
                        taper0 = s_add; taper1 = s_add;
                        flags.push("lane_from_entrance");
                    } else {
                        typ = "parallel";
                        let pl = c.t(region, T_ADD_PLACE);
                        taper0 = s_add - pl * exit_taper; taper1 = taper0 + exit_taper;
                    }
                    par0 = taper1; par1 = s_g;
                }
                None => {
                    typ = "exit_only";
                    taper0 = main.lo(); taper1 = main.lo(); par0 = main.lo(); par1 = s_g;
                    flags.push("lane_beyond_reach");
                }
            }
        } else {
            match found {
                Some((s0, via_fork)) => {
                    let s_drop = s0;
                    if via_fork.is_some() {
                        typ = "aux";
                        taper0 = s_drop; taper1 = s_drop;
                        flags.push("lane_to_exit");
                    } else {
                        typ = "parallel";
                        taper0 = s_drop; taper1 = s_drop + entry_taper;
                    }
                    par0 = s_g; par1 = taper0;
                }
                None => {
                    typ = "lane_add";
                    taper0 = main.hi(); taper1 = main.hi(); par0 = s_g; par1 = main.hi();
                    flags.push("lane_beyond_reach");
                }
            }
        }
    } else if !diverge && lb >= 2 && lt >= 1 && lm >= 1 && lt < need {
        // lane sum: a ramp of two or more lanes the road after it does not carry, a diverge on its side soon after
        match fork_along(c, &pt_, prm[P_AUX_MAX]) {
            Some((s, _nn, true, sd)) if (sd > 0.0) == right => {
                evidence = "lane_sum"; typ = "aux";
                par0 = s_g; par1 = s; taper0 = s; taper1 = s;
            }
            _ => {
                let acc = c.t(region, T_ACC_PAR) * share;
                par0 = s_g; par1 = s_g + acc; taper0 = par1; taper1 = par1 + entry_taper;
            }
        }
    } else if diverge {
        if sm.link && sb.link {
            typ = "taper";
            let l = c.t(region, T_DIV_K) * w;
            taper0 = s_g - l; taper1 = s_g; par0 = s_g; par1 = s_g;
        } else {
            let dec = c.t(region, T_DEC_PAR) * share;
            par1 = s_g; par0 = s_g - dec; taper1 = par0; taper0 = par0 - exit_taper;
        }
    } else if sm.link && sb.link {
        typ = "taper";
        let l = c.t(region, T_TAPER_MERGE);
        taper0 = s_g; taper1 = s_g + l; par0 = s_g; par1 = s_g;
    } else {
        let acc = c.t(region, T_ACC_PAR) * share;
        par0 = s_g; par1 = s_g + acc; taper0 = par1; taper1 = par1 + entry_taper;
    }
    // (a lane whose evidence starts it too near the gore: its taper ends at the gore, no parallel part; a merge's
    // mirrored)
    if diverge && taper1 > s_g {
        taper1 = s_g; if taper0 > s_g - exit_taper { taper0 = s_g - exit_taper; } par0 = s_g; flags.push("no_parallel");
    }
    if !diverge && taper0 < s_g {
        taper0 = s_g; if taper1 < s_g + entry_taper { taper1 = s_g + entry_taper; } par1 = s_g; flags.push("no_parallel");
    }
    if diverge {
        if taper0 < main.lo() { taper0 = main.lo(); flags.push("short_trunk"); }
        if taper1 < taper0 { taper1 = taper0; }
        if par0 < taper1 { par0 = taper1; }
    } else {
        if taper1 > main.hi() { taper1 = main.hi(); flags.push("short_trunk"); }
        if taper0 > taper1 { taper0 = taper1; }
        if par1 > taper0 { par1 = taper0; }
    }
    // strips, left to right: [through x lM][gore][branch x lB] on the right, mirrored on the left
    let nose_w = prm[P_NOSE_W];
    let (sg_, sn_, t0, t1) = (s_g, s_n, taper0, taper1);
    let exit_only = typ == "exit_only" || typ == "lane_add";
    let aux = typ == "aux";
    let branch_w: std::rc::Rc<dyn Fn(f64) -> f64> = if diverge {
        std::rc::Rc::new(move |s: f64| if exit_only || aux { if s < t0 && aux { 0.0 } else { w } } else { piece(s, t0, t1, 0.0, w) })
    } else {
        std::rc::Rc::new(move |s: f64| if exit_only || aux { if s > t1 && aux { 0.0 } else { w } } else { piece(s, t0, t1, w, 0.0) })
    };
    let gore_w: Box<dyn Fn(f64) -> f64> = if diverge { Box::new(move |s: f64| piece(s, sg_, sn_, 0.0, nose_w)) }
                                          else { Box::new(move |s: f64| piece(s, sn_, sg_, nose_w, 0.0)) };
    let mut names: Vec<&'static str> = Vec::new();
    let mut ws: Vec<Box<dyn Fn(f64) -> f64>> = Vec::new();
    let push_through = |names: &mut Vec<&'static str>, ws: &mut Vec<Box<dyn Fn(f64) -> f64>>| {
        for _ in 0..lm_e { names.push("through"); ws.push(Box::new(move |_s: f64| w)); }
    };
    // (a lane-sum auxiliary lane carries one of the ramp's lanes to the exit: its others merge over the entrance
    // taper after the gore; the carried one is the lane next to the through lanes)
    let lane_sum = evidence == "lane_sum";
    // (the merging lanes close before the exit the carried lane runs to, however near it is)
    let merge_end = { let e = sg_ + entry_taper; if e > par1 && par1 > sg_ { par1 } else { e } };
    let push_branch = |names: &mut Vec<&'static str>, ws: &mut Vec<Box<dyn Fn(f64) -> f64>>| {
        for k in 0..lb {
            let carried = !lane_sum || (if right { k == 0 } else { k == lb - 1 });
            if carried { names.push("branch"); let b = branch_w.clone(); ws.push(Box::new(move |s: f64| b(s))); }
            else { names.push("branch_merging"); ws.push(Box::new(move |s: f64| piece(s, sg_, merge_end, w, 0.0))); }
        }
    };
    if right {
        push_through(&mut names, &mut ws);
        names.push("gore"); ws.push(gore_w);
        push_branch(&mut names, &mut ws);
    } else {
        push_branch(&mut names, &mut ws);
        names.push("gore"); ws.push(gore_w);
        push_through(&mut names, &mut ws);
    }
    let strips = Strips { names, w: ws };
    let an = Anchor::new(c, &main, right, w);
    let margin = prm[P_MARGIN];
    // (an auxiliary lane is there from its entrance's gore / to its exit's: no margin past that end)
    let mg = if aux { 0.0 } else { margin };
    let (lo_s, hi_s) = if diverge {
        let a = if t0 > main.lo() { t0 } else { main.lo() };
        ({ let v = a - mg; if v < main.lo() { main.lo() } else { v } }, s_n)
    } else {
        let b = if t1 < main.hi() { t1 } else { main.hi() };
        (s_n, { let v = b + mg; if v > main.hi() { main.hi() } else { v } })
    };
    let rows = rows_json(&main, &an, right, &strips, lo_s, hi_s, prm[P_OUT], prm[P_SHIFT], &mut flags);
    // the branch's line past the nose (its own ribbon from there): its mapped points
    let mut bl = String::new();
    for &(tb, s, _d) in &samples {
        let ts = if diverge { s } else { -s };
        let past = if diverge { ts >= s_n } else { ts <= s_n };
        if !past { continue; }
        let (b, _) = pb_.at(tb);
        if !bl.is_empty() { bl.push(','); }
        bl.push_str(&format!("[{},{}]", mm(b[0]), mm(b[1])));
        if bl.len() > 4000 { break; }
    }
    let rule = format!("{{\"exit_taper\":{},\"entry_taper\":{},\"gore\":{},\"dec_par\":{},\"acc_par\":{},\"nose_w\":{}}}",
                       mm(exit_taper), mm(entry_taper), mm(gore_m), mm(c.t(region, T_DEC_PAR) * share),
                       mm(c.t(region, T_ACC_PAR) * share), mm(nose_w));
    let names_json = strips.names.iter().map(|v| format!("\"{}\"", v)).collect::<Vec<_>>().join(",");
    let pt = |s: f64| main.at(s).0;
    let (pg, pn) = (pt(s_g), pt(s_n));
    Some(format!("{},\"evidence\":\"{}\",\"type\":\"{}\",\"s\":{{{}}},\"pt\":{{\"gore\":[{},{}],\"nose\":[{},{}]}},\
                  \"rule\":{},\"strips\":[{}],\"rows\":[{}],\"branch_line\":[{}],\"flags\":[{}]}}",
                 head, evidence, typ,
                 stations_json(&[("taper0", taper0), ("taper1", taper1), ("par0", par0), ("par1", par1), ("gore", s_g),
                                 ("nose", s_n), ("lo", lo_s), ("hi", hi_s)]),
                 mm(pg[0]), mm(pg[1]), mm(pn[0]), mm(pn[1]), rule, names_json, rows, bl, flags_json(&flags)))
}

/// The side a lane is added or dropped on at a two-arm joint (true: right as the driver sees it): placement, then
/// turn:lanes, then a ramp event on the stroke within FEED_M, then the outer side.
fn add_side(c: &Ctx, wide: &Seg, wide_fwd: bool, narrow: &Seg, down: &Path, up: &Path, add: bool, region: usize)
            -> (bool, &'static str, Option<(f64, bool)>) {
    let lht = c.t(region, T_LHT) > 0.5;
    if wide.dir != 0 {
        let (pw, pn) = (wide.place.as_str(), narrow.place.as_str());
        if pw.starts_with("right_of") || pw.starts_with("left_of") || pn.starts_with("right_of") || pn.starts_with("left_of") {
            let nw = wide.lanes_dir(wide_fwd);
            let a = left_off(pw, nw, 1.0);
            let b = left_off(pn, narrow.lanes, 1.0);
            // the mapped line keeps its lanes on the left: the left edge's offset in lanes is the same on both
            if (a - b) * (a - b) < 0.01 { return (true, "placement", None); }
            let r = (a - nw as f64) - (b - narrow.lanes as f64);
            if r * r < 0.01 { return (false, "placement", None); }
        }
    }
    let turn = wide.turn_dir(wide_fwd);
    if !turn.is_empty() {
        let lanes: Vec<&str> = turn.split('|').collect();
        let last = lanes.last().copied().unwrap_or("");
        let first = lanes.first().copied().unwrap_or("");
        if !add && last.contains("merge_to_left") { return (true, "turn_lanes", None); }
        if !add && first.contains("merge_to_right") { return (false, "turn_lanes", None); }
        if wide.dir == 0 {
            // a turn bay of a two-way road: a left turn lane is added on the inner side
            let t0: Vec<&str> = first.split(';').collect();
            if t0.iter().all(|v| *v == "left" || *v == "sharp_left" || *v == "slight_left") { return (lht, "bay", None); }
            let t1: Vec<&str> = last.split(';').collect();
            if t1.iter().all(|v| *v == "right" || *v == "sharp_right" || *v == "slight_right") { return (!lht, "bay", None); }
        }
        if add && last.split(';').all(|v| v == "slight_right") { return (true, "turn_lanes", None); }
        if add && first.split(';').all(|v| v == "slight_left") { return (false, "turn_lanes", None); }
    }
    let feed = if add { fork_along(c, down, c.prm[P_FEED]) } else { fork_along(c, up, c.prm[P_FEED]) };
    if let Some((s, _n, dv, sd)) = feed {
        if dv == add { return (sd > 0.0, if add { "exit_after" } else { "entrance_before" }, Some((s, dv))); }
    }
    (!lht, "outer", None)
}

/// The add / drop events at a two-arm node n (one per direction whose lane count changes), or none.
fn add_drop_events(c: &Ctx, n: usize) -> Vec<String> {
    let mut out = Vec::new();
    let arms = arms_of(c.segs, &c.rows[n]);
    if arms.len() != 2 { return out; }
    let (a, b) = (arms[0], arms[1]);
    let (sa, sb) = (&c.segs[a.0], &c.segs[b.0]);
    if sa.cls <= 0 || sb.cls <= 0 || (sa.dir == 0) != (sb.dir == 0) { return out; }
    // each direction of travel through the node: (incoming arm, outgoing arm)
    let mut dirs: Vec<((usize, usize, bool), (usize, usize, bool))> = Vec::new();
    if sa.dir != 0 {
        let (fa, fb) = (flow_of(sa, a.2), flow_of(sb, b.2));
        if fa == fb || fa == 0 || fb == 0 { return out; }
        if fa < 0 { dirs.push((a, b)); } else { dirs.push((b, a)); }
    } else {
        dirs.push((a, b));
        dirs.push((b, a));
    }
    let reach = c.prm[P_REACH];
    for (inc, outg) in dirs {
        let (si, so) = (&c.segs[inc.0], &c.segs[outg.0]);
        // travel along the incoming arm toward the node: along its points when the arm leaves the node backwards
        let li = si.lanes_dir(!inc.2);
        let lo = so.lanes_dir(outg.2);
        if li < 1 || lo < 1 || li == lo { continue; }
        let add = lo > li;
        let up = walk(c, n, inc.0, inc.1, inc.2, reach);
        let dn = walk(c, n, outg.0, outg.1, outg.2, reach);
        let main = Main { up: &up, dn: &dn };
        let region = region_of(c, outg.0);
        let w = c.lane_w(if add { outg.0 } else { inc.0 });
        let (wide, wide_fwd, narrow) = if add { (so, outg.2, si) } else { (si, !inc.2, so) };
        let (right, why, feed) = add_side(c, wide, wide_fwd, narrow, &dn, &up, add, region);
        let kmh = if si.kmh > so.kmh { si.kmh } else { so.kmh };
        let dl = if add { lo - li } else { li - lo };
        let mut flags: Vec<&'static str> = Vec::new();
        let (len, rule) = match (why, feed) {
            ("bay", _) => (mutcd_m(w * dl as f64, kmh), "bay"),
            (_, Some((_s, true))) => {
                let v = c.t(region, T_EXIT_K) * w; let m0 = c.t(region, T_EXIT_MIN);
                (if v > m0 { v } else { m0 }, "exit_taper")
            }
            (_, Some((_s, false))) => {
                let v = c.t(region, T_ENTRY_K) * w; let m0 = c.t(region, T_ENTRY_MIN);
                (if v > m0 { v } else { m0 }, "entry_taper")
            }
            _ => {
                let k = c.t(region, T_DROP_K);
                (if k > 0.0 { k * w * dl as f64 } else { mutcd_m(w * dl as f64, kmh) }, "drop")
            }
        };
        // where the taper lies about the joint: an add add_place of it before; a drop after; a bay centred
        let (mut t0, mut t1) = if why == "bay" { (-len / 2.0, len / 2.0) }
                               else if add { let pl = c.t(region, T_ADD_PLACE); (-pl * len, (1.0 - pl) * len) }
                               else { (0.0, len) };
        if t0 < main.lo() { t0 = main.lo(); flags.push("short_before"); }
        if t1 > main.hi() { t1 = main.hi(); flags.push("short_after"); }
        let base = if add { li } else { lo };
        let two_way = si.dir == 0;
        let mut names: Vec<&'static str> = Vec::new();
        let mut ws: Vec<Box<dyn Fn(f64) -> f64>> = Vec::new();
        let lane_w = move |s: f64| if add { piece(s, t0, t1, 0.0, w) } else { piece(s, t0, t1, w, 0.0) };
        let lane_w = std::rc::Rc::new(lane_w);
        let nb = if two_way { so.lanes_dir(!outg.2).max(si.lanes_dir(inc.2)) } else { 0 };
        // (a two-way road: the other direction's lanes first, on the left, then this direction's)
        for _ in 0..(if two_way && nb > 0 { nb } else { 0 }) { names.push("opposite"); ws.push(Box::new(move |_s: f64| w)); }
        let added_left = !right;
        if added_left { for _ in 0..dl { names.push(if add { "added" } else { "dropped" }); let f = lane_w.clone(); ws.push(Box::new(move |s: f64| f(s))); } }
        for _ in 0..base { names.push("through"); ws.push(Box::new(move |_s: f64| w)); }
        if !added_left { for _ in 0..dl { names.push(if add { "added" } else { "dropped" }); let f = lane_w.clone(); ws.push(Box::new(move |s: f64| f(s))); } }
        let strips = Strips { names, w: ws };
        // a one-way carriageway anchored on the side away from the change; a two-way road on its mapped centre
        let an = Anchor::new(c, &main, right, w);
        let margin = c.prm[P_MARGIN];
        let lo_s = { let v = t0 - margin; if v < main.lo() { main.lo() } else { v } };
        let hi_s = { let v = t1 + margin; if v > main.hi() { main.hi() } else { v } };
        let rows = if two_way {
            two_way_rows(&main, &strips, lo_s, hi_s, c.prm[P_OUT], &mut flags)
        } else {
            rows_json(&main, &an, right, &strips, lo_s, hi_s, c.prm[P_OUT], c.prm[P_SHIFT], &mut flags)
        };
        let node = c.segs[n_vertex(c, n).0].pts[n_vertex(c, n).1];
        let head = head_json(node, if add { "add" } else { "drop" }, right, region, kmh, [li, lo, dl], w,
                             [&si.fid, &so.fid, ""]);
        let names_json = strips.names.iter().map(|v| format!("\"{}\"", v)).collect::<Vec<_>>().join(",");
        let feed_s = feed.map(|v| v.0).unwrap_or(0.0);
        out.push(format!("{},\"evidence\":\"{}\",\"type\":\"{}\",\"s\":{{{}}},\"rule\":{{\"taper\":{}}},\"strips\":[{}],\
                          \"rows\":[{}],\"flags\":[{}]}}",
                         head, why, rule,
                         stations_json(&[("taper0", t0), ("taper1", t1), ("feed", feed_s), ("lo", lo_s), ("hi", hi_s)]),
                         mm(len), names_json, rows, flags_json(&flags)));
    }
    out
}

fn n_vertex(c: &Ctx, n: usize) -> (usize, usize) { c.rows[n][0] }

/// A two-way road's rows: its boundaries centred on its mapped line (t_0 = W / 2, then down by each lane).
fn two_way_rows(m: &Main, strips: &Strips, s_lo: f64, s_hi: f64, step: f64, flags: &mut Vec<&'static str>) -> String {
    let mut out = String::new();
    let mut s = s_lo;
    let mut first = true;
    let mut unordered = false;
    loop {
        let (p, tg) = m.at(s);
        let nrm = [-tg[1], tg[0]];
        let ws: Vec<f64> = strips.w.iter().map(|f| { let v = f(s); if v > 0.0 { v } else { 0.0 } }).collect();
        let mut tot = 0.0;
        for v in &ws { tot += *v; }
        let mut t = vec![tot / 2.0];
        for k in 0..ws.len() { let v = t[k] - ws[k]; t.push(v); }
        let tm: Vec<i64> = t.iter().map(|&v| mm(v)).collect();
        if tm.windows(2).any(|x| x[0] < x[1]) { unordered = true; }
        if !first { out.push(','); }
        first = false;
        out.push_str(&format!("[{},{},{},{},{}", mm(s), mm(p[0]), mm(p[1]), (nrm[0] * 1e6).round() as i64,
                              (nrm[1] * 1e6).round() as i64));
        for v in tm { out.push_str(&format!(",{}", v)); }
        out.push(']');
        if s >= s_hi { break; }
        s += step;
        if s > s_hi { s = s_hi; }
    }
    if unordered { flags.push("unordered"); }
    out
}

/// A home segment's lane record: its lanes per direction, lane width and the left edge's offset from its line.
fn seg_record(c: &Ctx, g: usize) -> String {
    let s = &c.segs[g];
    let w = c.lane_w(g);
    let (lf, lb) = (s.lanes_dir(true), s.lanes_dir(false));
    let left = if s.dir != 0 { left_off(&s.place, if s.lanes >= 1 { s.lanes } else { 1 }, w) } else { f64::NAN };
    format!("{{\"fid\":{},\"lanes\":[{},{}],\"lane_w\":{},\"left\":{},\"region\":{}}}",
            json_str(&s.fid), lf, lb, mm(w), if left.is_finite() { mm(left).to_string() } else { "null".to_string() },
            s.region)
}

/// A home node's stroke links (one-way carriageways): [from, to, role, shift] per way through it, lane i of `from` is
/// lane i + shift of `to` (lanes counted from the left in travel). A plain continuation keeps the left lanes (right-
/// hand traffic; left-hand: the right ones); a fork's branch takes the trunk's lanes on its side. Written by the node's
/// home cell, so every arm there is read whatever the segments' lengths.
fn node_links(c: &Ctx, n: usize) -> Vec<String> {
    let mut out = Vec::new();
    let all = arms_of(c.segs, &c.rows[n]);
    let link = |a: usize, b: usize, role: &str, shift: i64| {
        format!("[{},{},\"{}\",{}]", json_str(&c.segs[all[a].0].fid), json_str(&c.segs[all[b].0].fid), role, shift)
    };
    if all.len() == 2 {
        let (fa, fb) = (flow_of(&c.segs[all[0].0], all[0].2), flow_of(&c.segs[all[1].0], all[1].2));
        if fa != 0 && fa == -fb {
            let (i, o) = if fa < 0 { (0, 1) } else { (1, 0) };
            let (li, lo) = (c.segs[all[i].0].lanes, c.segs[all[o].0].lanes);
            // (the side the lane is added or dropped on, as the add / drop event resolves it: on the left the left
            // lanes' numbers move)
            let mut shift = 0;
            if li >= 1 && lo >= 1 && li != lo {
                let add = lo > li;
                let reach = c.prm[P_REACH];
                let up = walk(c, n, all[i].0, all[i].1, all[i].2, reach);
                let dn = walk(c, n, all[o].0, all[o].1, all[o].2, reach);
                let (si, so) = (&c.segs[all[i].0], &c.segs[all[o].0]);
                let (wide, wide_fwd, narrow) = if add { (so, all[o].2, si) } else { (si, !all[i].2, so) };
                let (right, _why, _feed) = add_side(c, wide, wide_fwd, narrow, &dn, &up, add, so.region);
                if !right { shift = lo - li; }
            }
            out.push(link(i, o, "continue", shift));
        }
    } else if let Some((arms, ti, mi, bi, dv)) = fork_roles(c, n) {
        let (gm, vm, fm) = arms[mi];
        let (gb, vb, fb) = arms[bi];
        let right = match (arm_dir(&c.segs[gm], vm, fm, c.prm[P_DIR]), arm_dir(&c.segs[gb], vb, fb, c.prm[P_DIR])) {
            (Some(dm), Some(db)) => (cross(dm, db) < 0.0) == dv,
            _ => true,
        };
        let (lm, lb) = (c.segs[gm].lanes.max(1), c.segs[gb].lanes.max(1));
        let (sm, sb) = if right { (0, lm) } else { (lb, 0) };
        if dv {
            out.push(link(ti, mi, "through", -sm));
            out.push(link(ti, bi, "branch", -sb));
        } else {
            out.push(link(mi, ti, "through", sm));
            out.push(link(bi, ti, "branch", sb));
        }
    }
    out
}

/// Directional counts, turn tags and placement for an entire decoded layer.
/// Missing or non-string values carry the same defaults as absent OSM tags.
#[pyfunction]
pub fn lane_tag_columns(
    py: Python<'_>, n: usize, cols: Vec<Option<Vec<Py<PyAny>>>>,
) -> PyResult<Vec<(i64, i64, String, String, String)>> {
    if cols.len() != 6 || cols.iter().flatten().any(|c| c.len() != n) {
        return Err(PyValueError::new_err("lane_tag_columns: invalid tag columns"));
    }
    let tag = |col: usize, i: usize| -> &str {
        cols[col].as_ref().and_then(|c| c[i].bind(py).cast::<pyo3::types::PyString>().ok())
            .and_then(|s| s.to_str().ok()).unwrap_or("")
    };
    let int = py.get_type::<pyo3::types::PyInt>();
    let mut parsed = std::collections::BTreeMap::new();
    let mut count = |s: &str| -> i64 {
        if s.is_empty() { return -1; }
        if let Some(&v) = parsed.get(s) { return v; }
        let token = s.split(';').next().unwrap_or("").trim_matches(|c: char|
            c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c));
        let v = int.call1((token,)).and_then(|x| x.extract::<i64>()).ok()
            .filter(|v| (0..=16).contains(v)).unwrap_or(-1);
        parsed.insert(s.to_owned(), v);
        v
    };
    Ok((0..n).map(|i| {
        let p = tag(4, i);
        (count(tag(0,i)), count(tag(1,i)), tag(2,i).to_owned(), tag(3,i).to_owned(),
         if p.is_empty() { tag(5,i) } else { p }.to_owned())
    }).collect())
}

/// Build S1's per-feature columns in one pass over a decoded cell. The
/// Python caller supplies the driving width/count from the shared lane layout
/// rule. Connector stations use the same first-nearest rule as the fork graph.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn lane_cell_rows<'py>(
    py: Python<'py>,
    ll: PyReadonlyArray2<'py, f64>,
    parts: Vec<usize>,
    rings: Vec<usize>,
    coords: Vec<usize>,
    ids: Vec<String>,
    specs: Vec<Option<(i64, bool, i64, i64, f64, f64, f64, i64)>>,
    tags: Vec<Option<Vec<Py<PyAny>>>>,
    turns: Vec<Py<PyAny>>,
    features: Vec<usize>,
    starts: Vec<f64>,
    connectors: Vec<String>,
) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
    use numpy::ndarray::s;
    use pyo3::types::{PyDict, PySlice, PyString};
    let n = ids.len();
    let geom = ll.as_array();
    let offsets = |v: &[usize], total: usize| {
        v.first() == Some(&0) && v.windows(2).all(|w| w[0] <= w[1]) && v.last() == Some(&total)
    };
    if specs.len() != n
        || !turns.is_empty() && turns.len() != n
        || parts.len() != n + 1
        || rings.is_empty()
        || coords.is_empty()
        || !offsets(&parts, rings.len() - 1)
        || !offsets(&rings, coords.len() - 1)
        || !offsets(&coords, geom.nrows())
        || geom.ncols() != 2
        || features.len() != starts.len()
        || starts.len() != connectors.len()
        || features.iter().any(|&i| i >= n)
    {
        return Err(PyValueError::new_err(
            "lane_cell_rows: inconsistent geometry or rule columns",
        ));
    }
    let tags = lane_tag_columns(py, n, tags)?;
    let mut rules = vec![Vec::new(); n];
    for (j, &i) in features.iter().enumerate() {
        rules[i].push(j);
    }
    let out = PyDict::new(py);
    for (i, spec) in specs.into_iter().enumerate() {
        let Some((cls, link, dir, lanes, kmh, pave, drive, inferred)) = spec else {
            continue;
        };
        let p = parts[i];
        if p == parts[i + 1] || rings[p] == rings[p + 1] {
            continue;
        }
        let k = rings[p];
        let (a, b) = (coords[k], coords[k + 1]);
        if b - a < 2 {
            continue;
        }
        let line = geom.slice(s![a..b, ..]);
        let key = format!("{}|{:.7},{:.7}", ids[i], line[[0, 0]], line[[0, 1]]);
        let row = PyDict::new(py);
        row.set_item(
            "ll",
            ll.get_item(PySlice::new(py, a as isize, b as isize, 1))?,
        )?;
        let st: Vec<_> = rules[i].iter().map(|&j| starts[j]).collect();
        let verts = super::segments::vertices(line, &st);
        let con: Vec<_> = rules[i]
            .iter()
            .zip(verts)
            .map(|(&j, v)| (&connectors[j], v))
            .collect();
        row.set_item("cons", con)?;
        let (fw, bw, ref tf, ref tb, ref place) = tags[i];
        let fallback = turns
            .get(i)
            .and_then(|v| v.bind(py).cast::<PyString>().ok())
            .and_then(|s| s.to_str().ok())
            .unwrap_or("");
        row.set_item("cls", cls)?;
        row.set_item("link", link)?;
        row.set_item("dir", dir)?;
        row.set_item("lanes", lanes)?;
        row.set_item("kmh", kmh)?;
        row.set_item("lfw", if dir == 0 { fw } else { -1 })?;
        row.set_item("lbw", if dir == 0 { bw } else { -1 })?;
        row.set_item(
            "turn",
            if dir == 0 && !tf.is_empty() {
                tf.as_str()
            } else {
                fallback
            },
        )?;
        row.set_item("turn_bw", if dir == 0 { tb.as_str() } else { "" })?;
        row.set_item("place", place)?;
        let lane_w = if lanes > 0 {
            (if cls > 0 { drive } else { pave }) / lanes as f64
        } else {
            drive / inferred.max(1) as f64
        };
        row.set_item("lane_w", lane_w)?;
        out.set_item(key, row)?;
    }
    Ok(out)
}

/// Assemble the lane graph connector columns in sorted segment order. The home
/// cell is west/south inclusive, east/north exclusive, in unprojected lon/lat.
#[pyfunction]
pub fn lane_connectors(
    ll: PyReadonlyArray2<'_, f64>,
    off: Vec<usize>,
    rows: Vec<Vec<(String, usize)>>,
    bounds: [f64; 4],
) -> PyResult<(Vec<bool>, Vec<String>, Vec<usize>, Vec<usize>, Vec<bool>)> {
    let ll = ll.as_array();
    if ll.ncols() != 2 || off.len() != rows.len() + 1 || off.first() != Some(&0)
        || off.last() != Some(&ll.nrows()) || off.windows(2).any(|r| r[1] <= r[0]) {
        return Err(PyValueError::new_err("lane_connectors: invalid segment offsets"));
    }
    let inside = |i: usize| {
        let (x, y) = (ll[[i, 0]], ll[[i, 1]]);
        bounds[0] <= x && x < bounds[2] && bounds[1] <= y && y < bounds[3]
    };
    let (mut home, mut ids, mut seg, mut vertex, mut con_home) =
        (Vec::new(), Vec::new(), Vec::new(), Vec::new(), Vec::new());
    for (g, cons) in rows.into_iter().enumerate() {
        home.push(inside(off[g]));
        for (id, v) in cons {
            if v >= off[g + 1] - off[g] {
                return Err(PyValueError::new_err("lane_connectors: vertex outside segment"));
            }
            ids.push(id);
            seg.push(g);
            vertex.push(v);
            con_home.push(inside(off[g] + v));
        }
    }
    Ok((home, ids, seg, vertex, con_home))
}


/// lanegraph.cell_lanes: the lane graph of the home nodes and segments, one JSON object (module doc). Segments in fid
/// order (xy rows off[k]..off[k+1], the home cell's frame, m); connector rows (id, segment, vertex, home) in any
/// order; seg_home: the segments whose record the cell writes; table: the TaperRule table, REGIONS x T_N.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (xy, off, fid, cls, link, dir, lanes, lanes_fw, lanes_bw, kmh, lane_w, turn, turn_bw,
                    place, region, seg_home, con_id, con_seg, con_vtx, con_home, params, table, events_only=false))]
pub fn lane_graph<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: Vec<i64>, fid: Vec<String>,
                       cls: Vec<i64>, link: Vec<bool>, dir: Vec<i64>, lanes: Vec<i64>, lanes_fw: Vec<i64>,
                       lanes_bw: Vec<i64>, kmh: Vec<f64>, lane_w: Vec<f64>, turn: Vec<String>, turn_bw: Vec<String>,
                       place: Vec<String>, region: Vec<i64>, seg_home: Vec<bool>,
                       con_id: Vec<String>, con_seg: Vec<i64>, con_vtx: Vec<i64>, con_home: Vec<bool>,
                       params: PyReadonlyArray1<'py, f64>, table: PyReadonlyArray1<'py, f64>, events_only: bool) -> PyResult<String> {
    let (xy, prm, tab) = (xy.as_array(), params.as_array().to_vec(), table.as_array().to_vec());
    let ns = off.len().saturating_sub(1);
    let regions = tab.len() / T_N;
    let bad = xy.ncols() != 2 || prm.len() != NPARAMS || tab.len() % T_N != 0 || regions == 0
        || off.first().copied().unwrap_or(0) != 0
        || off.windows(2).any(|w| w[1] < w[0]) || off.last().map(|&v| v as usize != xy.nrows()).unwrap_or(true)
        || [fid.len(), cls.len(), link.len(), dir.len(), lanes.len(), lanes_fw.len(), lanes_bw.len(), kmh.len(),
            lane_w.len(), turn.len(), turn_bw.len(), place.len(), region.len(), seg_home.len()].iter().any(|&l| l != ns)
        || [con_seg.len(), con_vtx.len(), con_home.len()].iter().any(|&l| l != con_id.len())
        || xy.iter().chain(kmh.iter()).chain(lane_w.iter()).chain(prm.iter()).chain(tab.iter()).any(|v| !v.is_finite())
        || region.iter().any(|&r| r < 0 || r as usize >= regions)
        || fid.windows(2).any(|w| w[0] >= w[1]);
    if bad { return Err(PyValueError::new_err("lane_graph: shapes, offsets, order or values do not agree")); }
    let q = |v: f64| (v * 1000.0).round() / 1000.0;
    let mut segs: Vec<Seg> = Vec::with_capacity(ns);
    let mut remap: Vec<Vec<usize>> = Vec::with_capacity(ns);
    for k in 0..ns {
        let mut pts: Vec<P2> = Vec::new();
        let mut m = Vec::new();
        for r in off[k] as usize..off[k + 1] as usize {
            let p = [q(xy[[r, 0]]), q(xy[[r, 1]])];
            if pts.last().map(|o: &P2| o[0] != p[0] || o[1] != p[1]).unwrap_or(true) { pts.push(p); }
            m.push(pts.len().saturating_sub(1));
        }
        remap.push(m);
        segs.push(Seg { pts, cls: cls[k], link: link[k], dir: dir[k], lanes: lanes[k], lfw: lanes_fw[k], lbw: lanes_bw[k],
                        kmh: kmh[k], lane_w: lane_w[k], turn: turn[k].clone(), turn_bw: turn_bw[k].clone(),
                        place: place[k].clone(), region: region[k] as usize, fid: fid[k].clone(), nodes: Vec::new() });
    }
    let mut rows: Vec<(String, usize, usize, bool)> = Vec::new();
    for i in 0..con_id.len() {
        let (g, v) = (con_seg[i], con_vtx[i]);
        if g < 0 || g as usize >= ns || v < 0 || v as usize >= remap[g as usize].len() {
            return Err(PyValueError::new_err("lane_graph: a connector row's segment or vertex is out of range"));
        }
        rows.push((con_id[i].clone(), g as usize, remap[g as usize][v as usize], con_home[i]));
    }
    rows.sort_by(|a, b| a.0.cmp(&b.0).then(a.1.cmp(&b.1)).then(a.2.cmp(&b.2)));
    rows.dedup_by(|a, b| a.0 == b.0 && a.1 == b.1 && a.2 == b.2);
    let mut node_id: Vec<String> = Vec::new();
    let mut node_home: Vec<bool> = Vec::new();
    let mut node_rows: Vec<Vec<(usize, usize)>> = Vec::new();
    for (id, g, v, h) in &rows {
        if node_id.last() != Some(id) { node_id.push(id.clone()); node_home.push(*h); node_rows.push(Vec::new()); }
        let n = node_id.len() - 1;
        node_rows[n].push((*g, *v));
        segs[*g].nodes.push((*v, n));
    }
    for s in segs.iter_mut() { s.nodes.sort(); s.nodes.dedup_by(|a, b| a.0 == b.0); }
    let out = py.detach(|| {
        let c = Ctx { segs: &segs, rows: &node_rows, prm: &prm, tab: &tab };
        let mut ev: Vec<String> = Vec::new();
        for n in 0..node_id.len() {
            if !node_home[n] { continue; }
            let arms = arms_of(&segs, &node_rows[n]);
            let recs: Vec<String> = if arms.len() == 3 { fork_event(&c, n).into_iter().collect() }
                                    else if arms.len() == 2 { add_drop_events(&c, n) } else { Vec::new() };
            for r in recs { ev.push(format!("{{\"node\":{},{}", json_str(&node_id[n]), r)); }
        }
        if events_only {
            return format!("{{\"events\":[{}]}}", ev.join(","));
        }
        let sr: Vec<String> = (0..ns).filter(|&g| seg_home[g] && segs[g].cls > 0 && segs[g].pts.len() >= 2)
            .map(|g| seg_record(&c, g)).collect();
        let mut lk: Vec<String> = Vec::new();
        for n in 0..node_id.len() { if node_home[n] { lk.extend(node_links(&c, n)); } }
        format!("{{\"events\":[{}],\"segs\":[{}],\"links\":[{}]}}", ev.join(","), sr.join(","), lk.join(","))
    });
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn placement_offsets() {
        assert_eq!(left_off("", 3, 3.5), 5.25);
        assert_eq!(left_off("right_of:2", 3, 3.5), 7.0);
        assert_eq!(left_off("middle_of:1", 3, 3.5), 1.75);
        assert_eq!(left_off("left_of:1", 3, 3.5), 0.0);
        assert_eq!(left_off("transition", 2, 3.5), 3.5);
        assert_eq!(left_off("right_of:9", 3, 3.5), 5.25);
    }

    #[test]
    fn mutcd() {
        // 3.6 m at 65 mph: 234 m; at 30 mph: 3.6 x 900 / 60 = 54 m
        assert!((mutcd_m(3.6, 65.0 * MPH) - 234.0).abs() < 1e-9);
        assert!((mutcd_m(3.6, 30.0 * MPH) - 54.0).abs() < 1e-9);
    }
}
