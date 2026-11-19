//! fork_records (earthfast 510, road revamp v2 phase P2a, BNE_ROADSHAPE=sheet; SHADOW ONLY: nothing built reads it):
//! the Fork records of the road surface model (.claude/work/road-revamp-v2-road-shape-first-2026-11-19.md §1.1, §1.2,
//! §1.5) where one one-way carriageway divides in two (a diverge: an exit, a ramp splitting) or two join (a merge: an
//! entrance), from the map alone (roadshape.cell_forks: one z14 cell's nodes, its halo's roads).
//!
//! Per connector of the home cell with exactly three one-way arms (any levels: a shared connector is one point; diverge: one in, two out; merge: two
//! in, one out), every arm walked away from the node through plain continuations (a connector of two arms) up to
//! REACH_M (a function of the map within REACH_M of the node: order-free by construction):
//! - TRUNK: the single arm (the road before a diverge, after a merge). THROUGH (the main carriageway going on) of the
//!   other two by the design's local rule: same class as the trunk and not a ramp, then a ramp last, then the smaller
//!   turn from the trunk (read over DIR_M), then more lanes, then the lower segment id. BRANCH: the other. A fork only
//!   where both turn no more than FORK_DEG from the trunk and one of the pair is a ramp or both are motorway / trunk
//!   (a one-way street grid's T is a junction, not a fork).
//! - Stations s along the main path: + along the through arm from the node, - along the trunk; integer mm out.
//! - The branch's mapped line, sampled every STEP_M and projected on the through arm, gives the PAINTED NOSE (the two
//!   lanes' edges part: offset >= (wM + wB) / 2, w = mapped lanes x LANE_W, else the driving width) and the PHYSICAL
//!   NOSE (offset >= (wM + wB) / 2 + NOSE_GAP: a real gore's width at the island's end, fitted on the aerial; P2a2), each
//!   the first sample after which the offset stays at or above it (read until the lines have stayed clearly apart
//!   for NOSE_HOLD_M). d: driving width, h: one side's shoulder ((pavement - d) / 2), M through, B branch. At least
//!   GORE_MIN_M of painted gore.
//! - AT GRADE: any arm stops within AT_GRADE_M at a junction with crossing traffic (four or more arms, one of them
//!   leaving at more than 45 degrees to the arm; or a two-way road of the fork classes) and the main line is no
//!   motorway: a slip lane of a junction zone (P5), flagged "at_grade", no record.
//! - TAPER START s_t, the start of the shared length where the branch is an extra lane of the main carriageway, from
//!   the lanes: the trunk has the through's and branch's lanes (lT >= lM + lB, a lane drop / lane add): back along the
//!   trunk to the first piece with fewer (its connector), "lanes"; else (a diverge) the trunk's turn:lanes give the
//!   branch side's outer lanes to the branch alone (no through), "turn_lanes", from the trunk way's start; else the
//!   design length for the speed (a parallel deceleration lane DEC_K V + DEC_C, acceleration ACC_K V + ACC_C, clamped,
//!   plus the taper), "design".
//! - WIDTHS, quintic pieces (q(u) = u^3 (10 - 15 u + 6 u^2), C2 at both ends): the branch lane wb 0 -> dB over the
//!   taper (TAP_FAST_M at TAP_KMH and over, else TAP_SLOW_M) from s_t, then dB to the painted nose; the painted gap gp
//!   0 -> G = hM + hB + PHYS_M from the painted to the physical nose; the carriageway width from the trunk's mapped
//!   pavement to the model's over one taper before s_t (the seam piece; just after s_t where the lane is there from
//!   the record's start, or where the trunk ends at s_t: then the lane's own taper starts after the seam). Gores at
//!   least GORE_MIN_M long and opening no faster than 1 m per GORE_RATE m (GORE_RATE 0: off); a gore the through arm
//!   cannot hold is no complete record ("gore_beyond_arm").
//! - ROWS every OUT_M from the seam to the blend's end: the main line's point and normal (toward the branch), the
//!   lateral offsets of the far pavement edge, the main's near driving edge, the branch's inner and outer driving
//!   edges and the near pavement edge. BRANCH LINE from the physical nose: the offset of the main line (its centre at
//!   dM / 2 + G + dB / 2) blended onto the mapped ramp over max(BLEND_MIN_M, BLEND_K V) (q again).
//!
//! The record is one JSON object per fork, sorted by connector id; integers (mm, 1e-6 for unit normals) only.
//! No FMA, no libm, no hash maps: sqrt (correctly rounded) and + - * / only, in a fixed order.
use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

// params (roadshape.FORK_PARAMS, in this order)
const P_REACH: usize = 0;
const P_DIR: usize = 1;
const P_FORK_COS: usize = 2;
const P_PHYS: usize = 3;
const P_STEP: usize = 4;
const P_OUT: usize = 5;
const P_TAP_FAST: usize = 6;
const P_TAP_SLOW: usize = 7;
const P_TAP_KMH: usize = 8;
const P_DEC_K: usize = 9;
const P_DEC_C: usize = 10;
const P_DEC_MIN: usize = 11;
const P_DEC_MAX: usize = 12;
const P_ACC_K: usize = 13;
const P_ACC_C: usize = 14;
const P_ACC_MIN: usize = 15;
const P_ACC_MAX: usize = 16;
const P_BLEND_MIN: usize = 17;
const P_BLEND_K: usize = 18;
const P_GORE_MIN: usize = 19;
const P_GORE_RATE: usize = 20;
const P_LANE_W: usize = 21;
const P_NOSE_GAP: usize = 22;
const P_AT_GRADE: usize = 23;
const NPARAMS: usize = 24;

const NOSE_HOLD_M: f64 = 20.0;    // m of branch the lines stay clearly apart before the noses' scan stops
const CROSS_COS: f64 = 0.7071067811865476;    // cos 45 deg: an arm leaving a junction more across than this crosses
const MAX_PIECES: usize = 64;       // pieces of one arm's walk at most (a loop of continuations)
const CLS_TRUNK: i64 = 2;           // class codes (roadshape.FORK_CLASSES): 1 motorway, 2 trunk, 3.. lower; 0 none

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

/// The quintic smoothstep: 0 at 0, 1 at 1, first and second derivatives 0 at both ends.
pub(crate) fn quintic(u: f64) -> f64 {
    let u = if u < 0.0 { 0.0 } else if u > 1.0 { 1.0 } else { u };
    u * u * u * (10.0 + u * (-15.0 + u * 6.0))
}

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
    pave: f64,
    drive: f64,
    lanes: i64,
    kmh: f64,
    level: i64,
    turn: String,
    fid: String,
    nodes: Vec<(usize, usize)>,     // (vertex, node), by vertex
}

/// A walk away from a node: its polyline (from the node), arc lengths, and (segment, s0, s1) per piece.
struct Arm {
    seg: usize,
    flow: i64,                      // +1 traffic leaves the node along it, -1 comes in
    p: Vec<P2>,
    s: Vec<f64>,
    pieces: Vec<(usize, f64, f64)>,
    junction: bool,                 // stopped at a connector of three or more arms (else at REACH_M or a dead end)
    end_node: Option<usize>,        // that connector
}

impl Arm {
    fn len(&self) -> f64 { *self.s.last().unwrap_or(&0.0) }
    /// (point, unit tangent) at arc length t (clamped).
    fn at(&self, t: f64) -> (P2, P2) {
        let n = self.p.len();
        if n < 2 { return (self.p[0], [1.0, 0.0]); }
        let t = if t < 0.0 { 0.0 } else if t > self.len() { self.len() } else { t };
        let mut k = 0;
        while k + 2 < n && self.s[k + 1] < t { k += 1; }
        let l = self.s[k + 1] - self.s[k];
        let u = if l > 0.0 { (t - self.s[k]) / l } else { 0.0 };
        (lerp(self.p[k], self.p[k + 1], u), unit(sub(self.p[k + 1], self.p[k])).unwrap_or([1.0, 0.0]))
    }
    /// (arc length, signed offset, + left of the walk) of the nearest point to q; ties: the first piece.
    fn project(&self, q: P2) -> (f64, f64) {
        let mut best = (f64::INFINITY, 0.0, 0.0);
        for k in 0..self.p.len().saturating_sub(1) {
            let (a, b) = (self.p[k], self.p[k + 1]);
            let ab = sub(b, a);
            let l2 = dot(ab, ab);
            let u = if l2 > 0.0 { let v = dot(sub(q, a), ab) / l2; if v < 0.0 { 0.0 } else if v > 1.0 { 1.0 } else { v } }
                    else { 0.0 };
            let m = lerp(a, b, u);
            let d = norm(sub(q, m));
            if d < best.0 {
                let side = if cross(ab, sub(q, a)) < 0.0 { -1.0 } else { 1.0 };
                best = (d, self.s[k] + (self.s[k + 1] - self.s[k]) * u, side * d);
            }
        }
        (best.1, best.2)
    }
    fn seg_at(&self, t: f64) -> usize {
        for &(g, _s0, s1) in &self.pieces { if t <= s1 { return g; } }
        self.pieces.last().map(|v| v.0).unwrap_or(self.seg)
    }
}

/// Arms of node n: (segment, vertex, forward) per arm, in the node's row order.
fn arms_of(segs: &[Seg], rows: &[(usize, usize)]) -> Vec<(usize, usize, bool)> {
    let mut out = Vec::new();
    for &(g, v) in rows {
        if v > 0 { out.push((g, v, false)); }
        if v + 1 < segs[g].pts.len() { out.push((g, v, true)); }
    }
    out
}

fn flow_of(seg: &Seg, fwd: bool) -> i64 {
    match (seg.dir, fwd) { (1, true) | (-1, false) => 1, (1, false) | (-1, true) => -1, _ => 0 }
}

fn walk(segs: &[Seg], node_rows: &[Vec<(usize, usize)>], start: usize, g0: usize, v0: usize, fwd0: bool,
        reach: f64) -> Arm {
    let flow = flow_of(&segs[g0], fwd0);
    let mut arm = Arm { seg: g0, flow, p: vec![segs[g0].pts[v0]], s: vec![0.0], pieces: Vec::new(), junction: false,
                       end_node: None };
    let (mut g, mut v, mut fwd) = (g0, v0, fwd0);
    'outer: loop {
        let seg = &segs[g];
        let s_start = arm.len();
        let mut i = v;
        loop {
            let j = if fwd { i + 1 } else { i.wrapping_sub(1) };
            if j >= seg.pts.len() { break; }
            let (a, b) = (seg.pts[i], seg.pts[j]);
            let l = norm(sub(b, a));
            let s0 = arm.len();
            if s0 + l >= reach {
                let u = if l > 0.0 { (reach - s0) / l } else { 0.0 };
                arm.p.push(lerp(a, b, u));
                arm.s.push(reach);
                arm.pieces.push((g, s_start, reach));
                break 'outer;
            }
            if l > 0.0 { arm.p.push(b); arm.s.push(s0 + l); }
            i = j;
            // a connector at vertex i: go on through a plain continuation, stop at a junction or back at the start
            if let Ok(k) = seg.nodes.binary_search_by(|e| e.0.cmp(&i)) {
                let n = seg.nodes[k].1;
                arm.pieces.push((g, s_start, arm.len()));
                if n == start || arm.pieces.len() >= MAX_PIECES { break 'outer; }
                let others: Vec<(usize, usize, bool)> = arms_of(segs, &node_rows[n]).into_iter()
                    .filter(|&(h, w, f)| !(h == g && w == i && f != fwd)).collect();
                if others.len() != 1 {
                    arm.junction = others.len() > 1;
                    if arm.junction { arm.end_node = Some(n); }
                    break 'outer;
                }
                let (h, w, f) = others[0];
                if flow_of(&segs[h], f) != flow { break 'outer; }
                g = h; v = w; fwd = f;
                continue 'outer;
            }
        }
        arm.pieces.push((g, s_start, arm.len()));
        break;
    }
    arm
}

/// The branch side's lanes of a turn:lanes value given to the branch alone (no through / none), from that edge.
fn exit_lanes(turn: &str, left: bool) -> i64 {
    if turn.is_empty() { return 0; }
    let lanes: Vec<&str> = turn.split('|').collect();
    let want: &[&str] = if left { &["slight_left", "left", "sharp_left"] } else { &["slight_right", "right", "sharp_right"] };
    let mut k = 0;
    let n = lanes.len();
    for idx in 0..n {
        let lane = if left { lanes[idx] } else { lanes[n - 1 - idx] };
        let vals: Vec<&str> = lane.split(';').map(|v| v.trim()).collect();
        let turns = vals.iter().any(|v| want.contains(v));
        let other = vals.iter().any(|v| !want.contains(v));
        if turns && !other { k += 1; } else { break; }
    }
    k
}

fn json_str(s: &str) -> String { serde_json::to_string(s).unwrap_or_else(|_| "\"\"".to_string()) }

struct Main<'a> { t: &'a Arm, m: &'a Arm }

impl Main<'_> {
    /// (point, unit tangent along +s) of the main path at station s (- along the trunk, + along the through arm).
    fn at(&self, s: f64) -> (P2, P2) {
        if s < 0.0 { let (p, t) = self.t.at(-s); (p, [-t[0], -t[1]]) } else { self.m.at(s) }
    }
}

#[allow(clippy::too_many_arguments)]
fn one_fork(segs: &[Seg], node_rows: &[Vec<(usize, usize)>], n: usize, prm: &[f64]) -> Option<String> {
    let arms_rows = arms_of(segs, &node_rows[n]);
    if arms_rows.len() != 3 { return None; }
    let arms: Vec<Arm> = arms_rows.iter().map(|&(g, v, f)| walk(segs, node_rows, n, g, v, f, prm[P_REACH])).collect();
    if arms.iter().any(|a| a.flow == 0 || segs[a.seg].cls <= 0 || a.len() <= 0.0) {
        return None;
    }
    let n_out = arms.iter().filter(|a| a.flow > 0).count();
    let diverge = match n_out { 2 => true, 1 => false, _ => return None };
    let ti = arms.iter().position(|a| (a.flow > 0) != diverge)?;
    let others: Vec<usize> = (0..3).filter(|&k| k != ti).collect();
    let node = arms[ti].p[0];
    let dir = |a: &Arm| unit(sub(a.at(if prm[P_DIR] < a.len() { prm[P_DIR] } else { a.len() }).0, node));
    let dt = dir(&arms[ti])?;
    let cont = [-dt[0], -dt[1]];
    let st = &segs[arms[ti].seg];
    let key = |k: usize| {
        let a = &arms[k];
        let s = &segs[a.seg];
        let c = dir(a).map(|d| dot(cont, d)).unwrap_or(-2.0);
        (s.cls == st.cls && !s.link, !s.link, c, s.lanes)
    };
    let (ka, kc) = (key(others[0]), key(others[1]));
    // the through arm: the greater key (same class not a ramp, not a ramp, smaller turn, more lanes), ties the lower id
    let a_first = if ka.0 != kc.0 { ka.0 } else if ka.1 != kc.1 { ka.1 } else if ka.2 != kc.2 { ka.2 > kc.2 }
                  else if ka.3 != kc.3 { ka.3 > kc.3 } else { segs[arms[others[0]].seg].fid <= segs[arms[others[1]].seg].fid };
    let (mi, bi) = if a_first { (others[0], others[1]) } else { (others[1], others[0]) };
    let (ma, ba, ta) = (&arms[mi], &arms[bi], &arms[ti]);
    let (sm, sb) = (&segs[ma.seg], &segs[ba.seg]);
    if ka.2 < prm[P_FORK_COS] || kc.2 < prm[P_FORK_COS] { return None; }
    if !(sm.link || sb.link || (sm.cls <= CLS_TRUNK && sb.cls <= CLS_TRUNK)) { return None; }
    let dm = sm.drive;
    let hm = { let v = (sm.pave - sm.drive) / 2.0; if v > 0.0 { v } else { 0.0 } };
    let db = sb.drive;
    let hb = { let v = (sb.pave - sb.drive) / 2.0; if v > 0.0 { v } else { 0.0 } };
    let phys = prm[P_PHYS];
    let g_n = hm + hb + phys;
    // the noses from the lanes the map gives (lanes x LANE_W; no count: the driving width): the painted nose where the
    // two centrelines are half the two widths apart, the physical nose NOSE_GAP further (a real gore's width there,
    // shoulders and the island's end; fitted on the aerial, forkprobe)
    let wm = if sm.lanes >= 1 { sm.lanes as f64 * prm[P_LANE_W] } else { dm };
    let wb_n = if sb.lanes >= 1 { sb.lanes as f64 * prm[P_LANE_W] } else { db };
    let painted_off = (wm + wb_n) / 2.0;
    let phys_off = painted_off + prm[P_NOSE_GAP];
    // the branch's mapped line on the through arm: (s on the branch, s on the main, offset toward the branch)
    let mut samples: Vec<(f64, f64, f64)> = Vec::new();
    let mut side = 0.0;
    let mut t = prm[P_STEP];
    while t <= ba.len() {
        let (p, _) = ba.at(t);
        let (s, d) = ma.project(p);
        if s >= ma.len() - 1e-6 { break; }
        if side == 0.0 && (if d < 0.0 { -d } else { d }) >= 1.0 { side = if d < 0.0 { -1.0 } else { 1.0 }; }
        samples.push((t, s, d));
        t += prm[P_STEP];
    }
    let mut flags: Vec<&str> = Vec::new();
    if side == 0.0 {
        let c = cross(dir(ma)?, dir(ba)?);
        side = if c < 0.0 { -1.0 } else { 1.0 };
    }
    // each nose: the first sample after which the separation stays at or above its offset (an early bump of the mapped
    // line toward or away from the main does not place it), read along the branch until the lines have stayed clearly
    // apart (phys_off + NOSE_GAP) for NOSE_HOLD_M of the branch, or the through arm ends
    let mut sp: Option<f64> = None;
    let mut sn: Option<f64> = None;
    let mut apart: Option<f64> = None;
    for &(tb, s, d) in &samples {
        let dd = d * side;
        if dd < painted_off { sp = None; } else if sp.is_none() { sp = Some(s); }
        if dd < phys_off { sn = None; } else if sn.is_none() { sn = Some(s); }
        if dd < phys_off + prm[P_NOSE_GAP] { apart = None; } else if apart.is_none() { apart = Some(tb); }
        if let Some(a) = apart { if tb - a >= NOSE_HOLD_M { break; } }
    }
    if let (Some(a), Some(b)) = (sp, sn) { if a > b { sp = Some(b); } }
    let kmh = sm.kmh;
    let left_driver = (side > 0.0) == diverge;      // the side as the driver sees it (merge: the walk runs upstream)
    let kind = if diverge { "diverge" } else { "merge" };
    let head = format!("{{\"node_xy\":[{},{}],\"kind\":\"{}\",\"side\":\"{}\",\"trunk\":{},\"through\":{},\"branch\":{},\
                        \"lanes\":[{},{},{}],\"levels\":[{},{},{}],\"kmh\":{},\"w\":[{},{},{},{},{}]",
                       mm(node[0]), mm(node[1]), kind, if left_driver { "left" } else { "right" }, json_str(&st.fid),
                       json_str(&sm.fid), json_str(&sb.fid), st.lanes, sm.lanes, sb.lanes, st.level, sm.level, sb.level,
                       kmh.round() as i64,
                       mm(dm), mm(hm), mm(db), mm(hb), mm(st.pave));
    // an at-grade slip lane (any arm stops within AT_GRADE_M at a junction with crossing traffic: four or more arms,
    // or a two-way road of the fork classes; a service road's access to a motorway is none): a junction zone's (P5),
    // no fork record. A motorway's own exit / entrance never (a short ramp ending at a street is still a freeway gore)
    // (four or more arms: crossing only where one of them leaves at more than CROSS_DEG to the arm's own line, so a
    // same-direction lane merge and split is none)
    let crossing = |a: &Arm, n: usize| {
        if node_rows[n].iter().any(|&(g, _)| segs[g].dir == 0 && segs[g].cls > 0) { return true; }
        let arms = arms_of(segs, &node_rows[n]);
        if arms.len() < 4 { return false; }
        let (_, tg) = a.at(a.len());
        arms.iter().any(|&(g, v, f)| {
            let w = if f { v + 1 } else { v - 1 };
            match unit(sub(segs[g].pts[w], segs[g].pts[v])) {
                Some(u) => { let c = dot(u, tg); (if c < 0.0 { -c } else { c }) < CROSS_COS }
                None => false,
            }
        })
    };
    let motorway = |s: &Seg| s.cls == 1 && !s.link;
    if !(motorway(st) && motorway(sm))
       && [ta, ma, ba].iter().any(|a| a.len() <= prm[P_AT_GRADE] && a.end_node.map(|n| crossing(a, n)).unwrap_or(false)) {
        return Some(format!("{},\"flags\":[\"at_grade\"]}}", head));
    }
    let (sp, mut sn) = match (sp, sn) {
        (Some(a), Some(b)) => (a, b),
        _ => {
            flags.push(if sp.is_none() { "no_separation" } else { "no_physical_nose" });
            return Some(format!("{},\"flags\":[\"{}\"]}}", head, flags.join("\",\"")));
        }
    };
    // at least GORE_MIN_M of painted gore, opening no faster than 1 m per GORE_RATE m (a ramp drawn at a sharp angle
    // from the node would otherwise open the whole gore in a few metres)
    let gore_min = { let v = prm[P_GORE_RATE] * g_n; if v > prm[P_GORE_MIN] { v } else { prm[P_GORE_MIN] } };
    if sn < sp + gore_min { sn = sp + gore_min; flags.push("gore_min"); }
    if sn > ma.len() {      // (the through arm ends at a junction before that: no complete record)
        flags.push("gore_beyond_arm");
        return Some(format!("{},\"flags\":[\"{}\"]}}", head, flags.join("\",\"")));
    }
    let tap = if kmh >= prm[P_TAP_KMH] { prm[P_TAP_FAST] } else { prm[P_TAP_SLOW] };
    // the shared length's start
    let (lt, lm, lb) = (st.lanes, sm.lanes, sb.lanes);
    let mut evidence = "design";
    let mut s_t: f64;
    let mut full_at_start = false;
    if lt >= 1 && lm >= 1 && lb >= 1 && lt >= lm + lb {
        evidence = "lanes";
        let mut found = None;
        for &(g, s0, _s1) in &ta.pieces {
            let l = segs[g].lanes;
            if l >= 1 && l < lm + lb { found = Some(s0); break; }
        }
        match found {
            Some(s0) => s_t = -s0,
            None => { s_t = -ta.len(); full_at_start = true; flags.push(if ta.junction { "lane_from_junction" } else { "lane_beyond_reach" }); }
        }
    } else if diverge && st.dir == 1 && exit_lanes(&st.turn, left_driver) >= 1 {
        evidence = "turn_lanes";
        let s1 = ta.pieces.first().map(|v| v.2).unwrap_or(0.0);
        s_t = -s1;
        if s1 >= ta.len() && ta.junction { full_at_start = true; flags.push("lane_from_junction"); }
        else if s1 >= ta.len() { flags.push("lane_beyond_reach"); full_at_start = true; }
    } else {
        let (k, c, lo, hi) = if diverge { (prm[P_DEC_K], prm[P_DEC_C], prm[P_DEC_MIN], prm[P_DEC_MAX]) }
                             else { (prm[P_ACC_K], prm[P_ACC_C], prm[P_ACC_MIN], prm[P_ACC_MAX]) };
        let par = { let v = k * kmh + c; if v < lo { lo } else if v > hi { hi } else { v } };
        s_t = sp - (par + tap);
    }
    if s_t < -ta.len() { s_t = -ta.len(); flags.push("short_trunk"); }
    if s_t > sp { s_t = sp; }
    // the seam piece: the carriageway's width from the trunk's mapped pavement to the model's over one taper, before
    // the taper start; just after it where the branch lane is there from the record's start (full_at_start) or the
    // trunk's walk ends at the taper start (no room before: then the branch lane's own taper starts after the seam, so
    // the seam always blends onto one width and the rows are the pieces)
    let after = full_at_start || s_t <= -ta.len();
    let (seam0, seam1) = if !after { (if s_t - tap < -ta.len() { -ta.len() } else { s_t - tap }, s_t) }
                         else if full_at_start { (s_t, if s_t + tap < sp { s_t + tap } else { sp }) }
                         else { let h = (sp - s_t) / 2.0; (s_t, if tap < h { s_t + tap } else { s_t + h }) };
    let s_lane = if after && !full_at_start { seam1 } else { s_t };     // the branch lane's taper start
    let tap_eff = if full_at_start { 0.0 } else if sp - s_lane < tap { sp - s_lane } else { tap };
    let s_full = s_lane + tap_eff;
    let blend = { let v = prm[P_BLEND_K] * kmh; if v > prm[P_BLEND_MIN] { v } else { prm[P_BLEND_MIN] } };
    let s_end = { let v = sn + blend; if v > ma.len() { ma.len() } else { v } };
    let w_model_t = hm + dm + (if full_at_start { db } else { 0.0 }) + hb;
    let pave_seam = segs[ta.seg_at(-seam0)].pave;
    let wb = |s: f64| if s < s_lane { if full_at_start { db } else { 0.0 } } else { piece(s, s_lane, s_full, 0.0, db) };
    let gp = |s: f64| piece(s, sp, sn, 0.0, g_n);
    let main = Main { t: ta, m: ma };
    let pt = |s: f64, off: f64| { let (p, tg) = main.at(s); [p[0] - side * tg[1] * off, p[1] + side * tg[0] * off] };
    // rows
    let mut rows = String::new();
    let mut s = seam0;
    let step = prm[P_OUT];
    let mut first = true;
    loop {
        let (p, tg) = main.at(s);
        let nrm = [-side * tg[1], side * tg[0]];
        let (w_b, g_p) = (wb(s), gp(s));
        // the far pavement edge (-), the main's near driving edge, the branch's driving edges, the near pavement edge;
        // before the taper start the carriageway's width blends from the trunk's mapped pavement (the seam piece)
        let far = -(dm / 2.0 + hm);
        let near = if s < seam1 { far + piece(s, seam0, seam1, pave_seam, w_model_t) } else { dm / 2.0 + g_p + w_b + hb };
        if !first { rows.push(','); }
        first = false;
        rows.push_str(&format!("[{},{},{},{},{},{},{},{},{},{}]", mm(s), mm(p[0]), mm(p[1]), (nrm[0] * 1e6).round() as i64,
                               (nrm[1] * 1e6).round() as i64, mm(far), mm(dm / 2.0), mm(dm / 2.0 + g_p),
                               mm(dm / 2.0 + g_p + w_b), mm(near)));
        if s >= s_end { break; }
        s += step;
        if s > s_end { s = s_end; }
    }
    // the branch's line from the physical nose: the main's offset blended onto the mapped ramp
    let mut bl = String::new();
    let mut first = true;
    let c_off = dm / 2.0 + g_n + db / 2.0;
    let mut last_s = f64::NEG_INFINITY;
    for &(tb, s, _d) in &samples {
        if s < sn || s > s_end || s < last_s + step - 1e-9 { continue; }
        last_s = s;
        let o = pt(s, c_off);
        let (b, _) = ba.at(tb);
        let q = quintic((s - sn) / blend);
        let p = lerp(o, b, q);
        if !first { bl.push(','); }
        first = false;
        bl.push_str(&format!("[{},{}]", mm(p[0]), mm(p[1])));
    }
    let (p_t, p_p, p_n) = (pt(s_lane, dm / 2.0), pt(sp, dm / 2.0), pt(sn, dm / 2.0 + hm + phys / 2.0));
    Some(format!("{},\"evidence\":\"{}\",\"s\":{{\"seam\":{},\"taper\":{},\"full\":{},\"painted\":{},\"physical\":{},\"end\":{}}},\
                  \"pt\":{{\"taper\":[{},{}],\"painted\":[{},{}],\"physical\":[{},{}]}},\
                  \"pieces\":[[\"w_seam\",{},{},{},{}],[\"wb\",{},{},0,{}],[\"gp\",{},{},0,{}]],\"rows\":[{}],\"branch_line\":[{}],\
                  \"flags\":[{}]}}",
                 head, evidence, mm(seam0), mm(s_lane), mm(s_full), mm(sp), mm(sn), mm(s_end),
                 mm(p_t[0]), mm(p_t[1]), mm(p_p[0]), mm(p_p[1]), mm(p_n[0]), mm(p_n[1]),
                 mm(seam0), mm(seam1), mm(pave_seam), mm(w_model_t), mm(s_lane), mm(s_full), mm(db), mm(sp), mm(sn), mm(g_n),
                 rows, bl, flags.iter().map(|f| format!("\"{}\"", f)).collect::<Vec<_>>().join(",")))
}

/// roadshape.cell_forks: the fork records of the home nodes, one JSON array (module doc). Segments in fid order (xy
/// rows off[k]..off[k+1], the home cell's frame, m); connector rows (id, segment, vertex, home) in any order.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn fork_records<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: Vec<i64>, fid: Vec<String>,
                         cls: Vec<i64>, link: Vec<bool>, dir: Vec<i64>, pave: Vec<f64>, drive: Vec<f64>,
                         lanes: Vec<i64>, kmh: Vec<f64>, level: Vec<i64>, turn: Vec<String>,
                         con_id: Vec<String>, con_seg: Vec<i64>, con_vtx: Vec<i64>, con_home: Vec<bool>,
                         params: PyReadonlyArray1<'py, f64>) -> PyResult<String> {
    let (xy, prm) = (xy.as_array(), params.as_array().to_vec());
    let ns = off.len().saturating_sub(1);
    let bad = xy.ncols() != 2 || prm.len() != NPARAMS || off.first().copied().unwrap_or(0) != 0
        || off.windows(2).any(|w| w[1] < w[0]) || off.last().map(|&v| v as usize != xy.nrows()).unwrap_or(true)
        || [fid.len(), cls.len(), link.len(), dir.len(), pave.len(), drive.len(), lanes.len(), kmh.len(), level.len(),
            turn.len()].iter().any(|&l| l != ns)
        || [con_seg.len(), con_vtx.len(), con_home.len()].iter().any(|&l| l != con_id.len())
        || xy.iter().chain(pave.iter()).chain(drive.iter()).chain(kmh.iter()).any(|v| !v.is_finite())
        || fid.windows(2).any(|w| w[0] >= w[1]);
    if bad { return Err(PyValueError::new_err("fork_records: shapes, offsets, order or values do not agree")); }
    let mut segs: Vec<Seg> = (0..ns).map(|k| {
        // inputs to 1 mm (the record's own grid), consecutive duplicates dropped
        let mut pts: Vec<P2> = Vec::new();
        for r in off[k] as usize..off[k + 1] as usize {
            let p = [(xy[[r, 0]] * 1000.0).round() / 1000.0, (xy[[r, 1]] * 1000.0).round() / 1000.0];
            if pts.last().map(|q| q[0] != p[0] || q[1] != p[1]).unwrap_or(true) { pts.push(p); }
        }
        Seg { pts, cls: cls[k], link: link[k], dir: dir[k], pave: pave[k], drive: drive[k], lanes: lanes[k], kmh: kmh[k],
              level: level[k], turn: turn[k].clone(), fid: fid[k].clone(), nodes: Vec::new() }
    }).collect();
    // vertex indices after dropping duplicates: the first kept vertex at or after each original one
    let mut remap: Vec<Vec<usize>> = Vec::with_capacity(ns);
    for k in 0..ns {
        let mut m = Vec::new();
        let mut kept = 0usize;
        let mut last: Option<P2> = None;
        for r in off[k] as usize..off[k + 1] as usize {
            let p = [(xy[[r, 0]] * 1000.0).round() / 1000.0, (xy[[r, 1]] * 1000.0).round() / 1000.0];
            if let Some(q) = last { if q[0] != p[0] || q[1] != p[1] { kept += 1; } }
            last = Some(p);
            m.push(kept);
        }
        remap.push(m);
    }
    let mut rows: Vec<(String, usize, usize, bool)> = Vec::new();
    for i in 0..con_id.len() {
        let (g, v) = (con_seg[i], con_vtx[i]);
        if g < 0 || g as usize >= ns || v < 0 || v as usize >= remap[g as usize].len() {
            return Err(PyValueError::new_err("fork_records: a connector row's segment or vertex is out of range"));
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
        let mut parts: Vec<String> = Vec::new();
        for n in 0..node_id.len() {
            if !node_home[n] { continue; }
            if let Some(r) = one_fork(&segs, &node_rows, n, &prm) {
                parts.push(format!("{{\"node\":{},{}", json_str(&node_id[n]), &r[1..]));
            }
        }
        format!("[{}]", parts.join(","))
    });
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quintic_ends() {
        assert_eq!(quintic(0.0), 0.0);
        assert_eq!(quintic(1.0), 1.0);
        assert_eq!(quintic(0.5), 0.5);
        assert_eq!(exit_lanes("none|none|slight_right", false), 1);
        assert_eq!(exit_lanes("through|through;slight_right|slight_right", false), 1);
        assert_eq!(exit_lanes("slight_left|through", true), 1);
        assert_eq!(exit_lanes("", false), 0);
    }
}
