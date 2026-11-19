//! deck_sheets (earthfast 514, road revamp v2 phase P2b, BNE_ROADSHAPE=sheet): an exit or entrance leaving or joining
//! a carriageway ON a bridge built as ONE deck (.claude/work/road-revamp-v2-road-shape-first-2026-11-19.md §1.2, §1.5;
//! roads._deck_chunks, roadmesh.deck).
//!
//! Why: each mapped way had its own deck: where a ramp left a carriageway on a bridge the two slabs overlapped in the
//! gore (z-fighting), their parapets ran across the lanes and their heights disagreed (a lip across the lanes); the
//! user's top complaint (t4 Ottawa 45.38521 -75.73824, r87 Springfield, Fort Pitt's ramps).
//!
//! Input: the deck parts round a tile (their drawn centre lines, heights, half widths per vertex), each with its
//! traffic's direction (+1 along its line, -1 against, 0 two-way) and a rank (0 a main road, 1 a link / ramp).
//! Per part end B0 of a one-way part B (the BRANCH) lying within JOIN_M of another one-way part T (the THROUGH, the
//! carriageway going on; at its end or inside it):
//! - the FORK: B leaves the node (a diverge) and T's traffic leaves it too, or both come in (a merge); B's direction
//!   over DIR_M from the node within FORK_DEG of T's going on from it; their heights there within NODE_DZ. Where both
//!   only start (or end) at the node, the pair counts once: T is the more main of the two (rank, then the wider, then
//!   the earlier in the input's order: the caller sorts the parts by id). Of several T, the first by the same order.
//!   The map's nose is NOT used (P2a2: up to 23 m off on shallow diverges): the split comes from the deck geometry.
//! - the SPLIT: walking B away from the node every STEP_M (up to REACH_M, B's end, or T's end), the gap between the
//!   two carriageways' facing edges (|offset| - hB - hT, B's centre projected on T). The union holds up to where that
//!   gap last is SPLIT_GAP_M or less (interpolated) before it stays over STOP_GAP_M for HOLD_M (the strips clearly
//!   apart), or to B's / T's end when they never part. At least MIN_UNION_M, and B never crossing T's line.
//! - LAMBDA, the split line: T's normal at the foot of B's centre at the split (sT), crossed with B's centre line (sB:
//!   B's slab starts or ends on it, skewed; T's union piece ends on it; roads._deck_chunks).
//! - B's heights PINNED to T's within the union (the design's gore owned by the main surface: no step between the
//!   through and branch lanes at the shared edge, one slab top), then back onto B's own profile by a quintic over
//!   min(PIN_BLEND_M, PIN_SHARE x the rest of B) from Lambda (B's far end keeps its height: the abutment).
//! - CHAINS (CHAIN 1): each arm followed through its plain continuations (an arm in pieces end to end). Off in the
//!   server for now (roadshape.SHEET_CHAIN): a tile's deck parts are those round it, so a chain's far pieces can be
//!   missing in one tile and present in the next (window dependence: two tiles drawing one fork two ways). With
//!   CHAIN 0 a record is a function of its two parts alone, whole in every tile that holds either.
//! - NODE (optional): only the branch ends at that point (roads._sheet_records: one call per fork node on the parts
//!   within SHEET_NODE_R_M of it, the record a function of those alone, whichever tile asks).
//! - NO RECORD where the branch never parts from the through deck (it lies inside it to its end); nor where the two disagree in height by more than DZ_MAX anywhere in the union (a flyover over the deck, a
//!   profile fault: one slab would put the ramp on the wrong level; P3's joint profile), nor where the blend back would
//!   be steeper than PIN_GRADE on average (|dz at the split| > PIN_GRADE x the blend's length: a branch ending inside
//!   the union at another height keeps its own heights and deck).
//!
//! Out: per record (t, b, b_end, side: B on T's left (+1, of T's point order) or right (-1)), its numbers
//! (REC_F below), and per input vertex the change of its height (the pins and blends of every record on that part).
//! No FMA, no libm, no hash maps: sqrt (correctly rounded) and + - * / only, in a fixed order.
use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

// params (roadshape.SHEET_PARAMS, in this order)
const P_JOIN: usize = 0;
const P_FORK_COS: usize = 1;
const P_NODE_DZ: usize = 2;
const P_SPLIT_GAP: usize = 3;
const P_STOP_GAP: usize = 4;
const P_HOLD: usize = 5;
const P_REACH: usize = 6;
const P_STEP: usize = 7;
const P_DIR: usize = 8;
const P_MIN_UNION: usize = 9;
const P_PIN_BLEND: usize = 10;
const P_PIN_SHARE: usize = 11;
const P_DZ_MAX: usize = 12;
const P_PIN_GRADE: usize = 13;
const P_CHAIN: usize = 14;          // 1: each arm followed through its plain continuations; 0: the two parts alone
const NPARAMS: usize = 15;

/// The numbers of a record: the own arc of Lambda on the branch part holding it, the through part's arc of the node
/// (on the first through part), the own arc of Lambda on the through part holding it, Lambda's point on the through
/// line, that part's left unit normal there (of its point order), Lambda's point on the branch line, the union's height
/// there, the gap and the height difference (T - B, before the pin) at the split, and 1 when the strips parted (0: the
/// union runs to the branch chain's or the through chain's end).
pub const REC_F: usize = 13;

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
fn quintic(u: f64) -> f64 {
    let u = if u < 0.0 { 0.0 } else if u > 1.0 { 1.0 } else { u };
    u * u * u * (10.0 - 15.0 * u + 6.0 * u * u)
}

/// A part's line with its heights and half widths, in some order, with its arc lengths.
struct Line {
    p: Vec<P2>,
    z: Vec<f64>,
    hw: Vec<f64>,
    s: Vec<f64>,
}

impl Line {
    fn new(p: Vec<P2>, z: Vec<f64>, hw: Vec<f64>) -> Line {
        let mut s = vec![0.0; p.len()];
        for i in 1..p.len() {
            s[i] = s[i - 1] + norm(sub(p[i], p[i - 1]));
        }
        Line { p, z, hw, s }
    }
    fn len(&self) -> f64 { *self.s.last().unwrap_or(&0.0) }
    /// The piece holding arc a (clamped) and the share along it.
    fn locate(&self, a: f64) -> (usize, f64) {
        let n = self.p.len();
        if n < 2 { return (0, 0.0); }
        let a = if a < 0.0 { 0.0 } else if a > self.len() { self.len() } else { a };
        let mut i = self.s.partition_point(|&v| v <= a).max(1) - 1;
        if i >= n - 1 { i = n - 2; }
        let d = self.s[i + 1] - self.s[i];
        (i, if d > 0.0 { (a - self.s[i]) / d } else { 0.0 })
    }
    fn at(&self, a: f64) -> P2 {
        let (i, u) = self.locate(a);
        if self.p.len() < 2 { return self.p[0]; }
        add(self.p[i], mul(sub(self.p[i + 1], self.p[i]), u))
    }
    fn zat(&self, a: f64) -> f64 {
        let (i, u) = self.locate(a);
        if self.p.len() < 2 { return self.z[0]; }
        self.z[i] + (self.z[i + 1] - self.z[i]) * u
    }
    fn hwat(&self, a: f64) -> f64 {
        let (i, u) = self.locate(a);
        if self.p.len() < 2 { return self.hw[0]; }
        self.hw[i] + (self.hw[i + 1] - self.hw[i]) * u
    }
    /// (distance, arc length, signed offset + left, the piece's left unit normal) of the nearest point to q; ties:
    /// the first piece.
    fn project(&self, q: P2) -> (f64, f64, f64, P2) {
        let mut best = (f64::INFINITY, 0.0, 0.0, [0.0, 1.0]);
        for k in 0..self.p.len().saturating_sub(1) {
            let (a, b) = (self.p[k], self.p[k + 1]);
            let ab = sub(b, a);
            let l2 = dot(ab, ab);
            if l2 <= 0.0 { continue; }
            let v = dot(sub(q, a), ab) / l2;
            let u = if v < 0.0 { 0.0 } else if v > 1.0 { 1.0 } else { v };
            let m = add(a, mul(ab, u));
            let d = norm(sub(q, m));
            if d < best.0 {
                let l = l2.sqrt();
                let side = if cross(ab, sub(q, a)) < 0.0 { -1.0 } else { 1.0 };
                best = (d, self.s[k] + (self.s[k + 1] - self.s[k]) * u, side * d, [-ab[1] / l, ab[0] / l]);
            }
        }
        best
    }
    /// The arc length where the line through o along d first crosses this line (None: it does not).
    fn cross_line(&self, o: P2, d: P2) -> Option<f64> {
        let mut best: Option<(f64, f64)> = None;          // (|distance from o along d|, arc)
        for k in 0..self.p.len().saturating_sub(1) {
            let (a, b) = (self.p[k], self.p[k + 1]);
            let ab = sub(b, a);
            let den = cross(d, ab);
            if den.abs() <= 1e-12 { continue; }
            let ao = sub(a, o);
            let u = cross(ao, d) / den;             // along ab
            if !(-1e-9..=1.0 + 1e-9).contains(&u) { continue; }
            let t = cross(ao, ab) / den;            // along d
            let u = if u < 0.0 { 0.0 } else if u > 1.0 { 1.0 } else { u };
            let arc = self.s[k] + (self.s[k + 1] - self.s[k]) * u;
            if best.is_none_or(|(bt, _)| t.abs() < bt) { best = Some((t.abs(), arc)); }
        }
        best.map(|v| v.1)
    }
}

struct Part {
    line: Line,
    tdir: i64,
    rank: i64,
    hwm: f64,       // its median-ish half width (the first vertex's and the last's mean): the "wider" tie rule
}

/// T is more main than B (the pair counted once where both only meet at the node).
fn more_main(t: usize, b: usize, parts: &[Part]) -> bool {
    let (pt, pb) = (&parts[t], &parts[b]);
    if pt.rank != pb.rank { return pt.rank < pb.rank; }
    if (pt.hwm - pb.hwm).abs() > 0.05 { return pt.hwm > pb.hwm; }
    t < b
}

/// A part walked in one direction (rev: against its point order) as a piece of a chain, from chain arc a0 to a1, its
/// first chain point at its own arc s0.
#[derive(Clone, Copy)]
struct Piece {
    part: usize,
    rev: bool,
    a0: f64,
    a1: f64,
    s0: f64,
}

impl Piece {
    /// The part's own arc of chain arc a.
    fn own(&self, a: f64) -> f64 {
        let u = a - self.a0;
        if self.rev { self.s0 - u } else { self.s0 + u }
    }
}

/// A chain: a part from a point of it (its end, or inside it) on through its plain continuations (the only part whose
/// end meets the chain's end there, its traffic the same way, turning no more than FORK_DEG, within NODE_DZ), up to
/// REACH_M + HOLD_M. A function of the parts within that reach alone.
struct Chain {
    line: Line,
    pieces: Vec<Piece>,
}

impl Chain {
    /// (piece index, the part's own arc) of chain arc a.
    fn own(&self, a: f64) -> (usize, f64) {
        let mut k = 0;
        while k + 1 < self.pieces.len() && a > self.pieces[k].a1 { k += 1; }
        (k, self.pieces[k].own(a))
    }
    /// Per piece holding chain arcs [0, cut]: (part, rev, own arc lo, own arc hi).
    fn cover(&self, cut: f64) -> Vec<(usize, bool, f64, f64)> {
        let mut out = Vec::new();
        for pc in &self.pieces {
            let a1 = if pc.a1 < cut { pc.a1 } else { cut };
            if a1 - pc.a0 <= 1e-6 { continue; }
            let (u0, u1) = (pc.own(pc.a0), pc.own(a1));
            out.push((pc.part, pc.rev, u0.min(u1), u0.max(u1)));
        }
        out
    }
}

/// part p walked from its own arc s0 (rev: toward its start), then its plain continuations. flow: the traffic's
/// direction along the walk (+1 / -1).
fn chain(parts: &[Part], p: usize, rev: bool, s0: f64, flow: i64, prm: &[f64]) -> Chain {
    let mut pts: Vec<P2> = Vec::new();
    let mut zs: Vec<f64> = Vec::new();
    let mut hs: Vec<f64> = Vec::new();
    let mut pieces: Vec<Piece> = Vec::new();
    let (mut cur, mut crev, mut cs0) = (p, rev, s0);
    let mut a0 = 0.0;
    let limit = prm[P_REACH] + prm[P_HOLD];
    loop {
        let ln = &parts[cur].line;
        let l = ln.len();
        let start = ln.at(cs0);
        if pts.is_empty() {
            pts.push(start);
            zs.push(ln.zat(cs0));
            hs.push(ln.hwat(cs0));
        }
        let mut idx: Vec<usize> = (0..ln.p.len()).filter(|&i| if crev { ln.s[i] < cs0 } else { ln.s[i] > cs0 }).collect();
        if crev { idx.reverse(); }
        for &i in &idx {
            let last = *pts.last().unwrap_or(&start);
            if norm(sub(ln.p[i], last)) <= 1e-9 { continue; }
            pts.push(ln.p[i]);
            zs.push(ln.z[i]);
            hs.push(ln.hw[i]);
        }
        let walked = if crev { cs0 } else { l - cs0 };
        pieces.push(Piece { part: cur, rev: crev, a0, a1: a0 + walked, s0: cs0 });
        a0 += walked;
        if prm[P_CHAIN] <= 0.0 || a0 >= limit || pts.len() < 2 { break; }
        // the plain continuation at the chain's end
        let q = *pts.last().unwrap_or(&start);
        let n = pts.len();
        let Some(dir) = unit(sub(q, pts[n - 2])) else { break };
        let mut found: Option<(usize, bool)> = None;
        let mut meeting = 0;
        for (k, pk) in parts.iter().enumerate() {
            if pk.line.p.len() < 2 || pieces.iter().any(|pc| pc.part == k) { continue; }
            for e in 0..2 {
                let at = if e == 0 { pk.line.p[0] } else { pk.line.p[pk.line.p.len() - 1] };
                if norm(sub(at, q)) > prm[P_JOIN] { continue; }
                meeting += 1;
                let krev = e == 1;
                let walk_flow = if krev { -pk.tdir } else { pk.tdir };
                let lk = pk.line.len();
                let far = pk.line.at(if krev { lk - prm[P_DIR].min(lk) } else { prm[P_DIR].min(lk) });
                let z_at = pk.line.zat(if krev { lk } else { 0.0 });
                if walk_flow == flow && (z_at - *zs.last().unwrap_or(&z_at)).abs() <= prm[P_NODE_DZ]
                    && unit(sub(far, at)).is_some_and(|v| dot(v, dir) >= prm[P_FORK_COS]) {
                    found = Some((k, krev));
                }
            }
        }
        match found {
            Some((k, krev)) if meeting == 1 => {
                cur = k;
                crev = krev;
                cs0 = if krev { parts[k].line.len() } else { 0.0 };
            }
            _ => break,
        }
    }
    Chain { line: Line::new(pts, zs, hs), pieces }
}

/// One record: (t: the through part holding the split, b: the branch part holding it, side: the branch on t's left
/// (+1, of its point order) or right), REC_F numbers, the cover ((part, rev, role 0 through / 1 branch, side on that
/// part, own arcs lo, hi), in chain order), the height changes ((part, vertex, dz)).
type Cover = (usize, bool, i64, i64, f64, f64);
type Rec = ((usize, usize, i64), [f64; REC_F], Vec<Cover>, Vec<(usize, usize, f64)>);

fn fork_of(parts: &[Part], b: usize, e: usize, prm: &[f64]) -> Option<Rec> {
    let pb = &parts[b];
    if pb.tdir == 0 || pb.line.p.len() < 2 || pb.line.len() < prm[P_MIN_UNION] { return None; }
    let leaves = (e == 0 && pb.tdir > 0) || (e == 1 && pb.tdir < 0);
    let flow = if leaves { 1 } else { -1 };
    let lb0 = pb.line.len();
    let q = if e == 0 { pb.line.p[0] } else { pb.line.p[pb.line.p.len() - 1] };
    let far0 = pb.line.at(if e == 0 { prm[P_DIR].min(lb0) } else { lb0 - prm[P_DIR].min(lb0) });
    let o_b = unit(sub(far0, q))?;
    let z_b0 = if e == 0 { pb.line.z[0] } else { pb.line.z[pb.line.z.len() - 1] };
    // the through part
    let mut pick: Option<(usize, f64, f64)> = None;      // (t, its arc of the node, its walking sign)
    for (t, pt) in parts.iter().enumerate() {
        if t == b || pt.tdir == 0 || pt.line.p.len() < 2 { continue; }
        let (d, s0, _off, _nrm) = pt.line.project(q);
        if d > prm[P_JOIN] { continue; }
        let sgn = if leaves { pt.tdir as f64 } else { -(pt.tdir as f64) };
        let lt = pt.line.len();
        let room = if sgn > 0.0 { lt - s0 } else { s0 };
        if room < prm[P_MIN_UNION] { continue; }
        let Some(d_t) = unit(sub(pt.line.at(s0 + sgn * prm[P_DIR].min(room)), pt.line.at(s0))) else { continue };
        if dot(o_b, d_t) < prm[P_FORK_COS] { continue; }
        if (z_b0 - pt.line.zat(s0)).abs() > prm[P_NODE_DZ] { continue; }
        let at_end = s0 <= prm[P_JOIN] || s0 >= lt - prm[P_JOIN];
        if at_end && !more_main(t, b, parts) { continue; }
        let better = match pick {
            None => true,
            Some((t0, _, _)) => more_main(t, t0, parts),
        };
        if better { pick = Some((t, s0, sgn)); }
    }
    let (t, s_t0, sgn) = pick?;
    let ob = chain(parts, b, e == 1, if e == 0 { 0.0 } else { lb0 }, flow, prm);
    let tc = chain(parts, t, sgn < 0.0, s_t0, flow, prm);
    if tc.pieces.iter().any(|pc| ob.pieces.iter().any(|x| x.part == pc.part)) { return None; }
    let (lb, lt) = (&ob.line, &tc.line);
    // the walk along the branch's chain, measured against the through chain (both from the node)
    let step = prm[P_STEP];
    let reach = prm[P_REACH].min(lb.len());
    let mut side = 0.0;
    let mut last_le: Option<(f64, f64)> = None;          // (s, gap) of the last sample at or under SPLIT_GAP
    let mut next_gt: Option<(f64, f64)> = None;          // the sample after it
    let mut far = 0.0;
    let mut k = 0usize;
    let mut dzs: Vec<(f64, f64)> = Vec::new();          // (s, |T - B|) per sample
    loop {
        let s = (k as f64) * step;
        let s = if s > reach { reach } else { s };
        let p = lb.at(s);
        let (_d, st, off, _nrm) = lt.project(p);
        dzs.push((s, (lt.zat(st) - lb.zat(s)).abs()));
        if k > 0 && st >= lt.len() - 1e-6 { break; }         // (the through chain ends: no more alongside)
        if off.abs() > 0.5 {
            let sd = if off > 0.0 { 1.0 } else { -1.0 };
            if side == 0.0 { side = sd; } else if sd != side { return None; }       // (B crossing T: no fork)
        }
        let gap = off.abs() - lb.hwat(s) - lt.hwat(st);
        if gap <= prm[P_SPLIT_GAP] {
            last_le = Some((s, gap));
            next_gt = None;
            far = 0.0;
        } else {
            if next_gt.is_none() { next_gt = Some((s, gap)); }
            if gap > prm[P_STOP_GAP] {
                far += step;
                if far >= prm[P_HOLD] { break; }
            } else {
                far = 0.0;
            }
        }
        if s >= reach { break; }
        k += 1;
    }
    let (s_le, g_le) = last_le?;
    let (s_split, parted) = match next_gt {
        Some((s2, g2)) if s2 > s_le && g2 > g_le => (s_le + (s2 - s_le) * (prm[P_SPLIT_GAP] - g_le) / (g2 - g_le), true),
        _ => (s_le, false),
    };
    // (a branch that never parts from the through deck lies inside it to its end: an overlay or a mapping fault, not
    // an exit; its own deck and heights stay. SD Fwy's I-605 connector: 2,553 -> 3,307 m2 overlap with it)
    if !parted || s_split < prm[P_MIN_UNION] || side == 0.0 { return None; }
    // Lambda: the through chain's normal at the foot of the branch's centre there, crossed with the branch's line
    let (_d, s_t, _off, nrm) = lt.project(lb.at(s_split));
    let pt_ = lt.at(s_t);
    let s_b = lb.cross_line(pt_, nrm).unwrap_or(s_split);
    let pb_ = lb.at(s_b);
    let z_t = lt.zat(s_t);
    let gap = norm(sub(pb_, pt_)) - lb.hwat(s_b) - lt.hwat(s_t);
    let dz_split = z_t - lb.zat(s_b);
    if dzs.iter().any(|&(s, d)| s <= s_b && d > prm[P_DZ_MAX]) { return None; }
    let lblend0 = prm[P_PIN_BLEND].min(prm[P_PIN_SHARE] * (lb.len() - s_b));
    if dz_split.abs() > prm[P_PIN_GRADE] * lblend0 + 0.02 { return None; }
    // the pins: in the union the branch's heights are the through chain's at its foot; then back to its own
    let mut dz: Vec<(usize, usize, f64)> = Vec::new();
    let lblend = prm[P_PIN_BLEND].min(prm[P_PIN_SHARE] * (lb.len() - s_b));
    for pc in &ob.pieces {
        let ln = &parts[pc.part].line;
        for i in 0..ln.p.len() {
            let a = pc.a0 + if pc.rev { pc.s0 - ln.s[i] } else { ln.s[i] - pc.s0 };
            if a < pc.a0 - 1e-9 || a > pc.a1 + 1e-9 { continue; }
            let v = if a <= s_b {
                let (_d2, st2, _o2, _n2) = lt.project(ln.p[i]);
                lt.zat(st2) - ln.z[i]
            } else if lblend > 1e-9 && a < s_b + lblend {
                dz_split * (1.0 - quintic((a - s_b) / lblend))
            } else {
                continue;
            };
            dz.push((pc.part, i, v));
        }
    }
    // the cover: the through chain's pieces from the node to Lambda, the branch's
    let mut cov: Vec<Cover> = Vec::new();
    for (p, rev, u0, u1) in tc.cover(s_t) {
        cov.push((p, rev, 0, if rev { -side as i64 } else { side as i64 }, u0, u1));
    }
    for (p, rev, u0, u1) in ob.cover(s_b) {
        cov.push((p, rev, 1, 0, u0, u1));
    }
    let (kt, st_own) = tc.own(s_t);
    let (kb, sb_own) = ob.own(s_b);
    let (tp, bp) = (tc.pieces[kt].part, ob.pieces[kb].part);
    let trev = tc.pieces[kt].rev;
    let n_own = if trev { [-nrm[0], -nrm[1]] } else { nrm };
    let side_own = if trev { -side } else { side };
    let rec = [sb_own, s_t0, st_own, pt_[0], pt_[1], n_own[0], n_own[1], pb_[0], pb_[1], z_t, gap, dz_split,
               if parted { 1.0 } else { 0.0 }];
    Some(((tp, bp, side_own as i64), rec, cov, dz))
}

/// (records, 4 per record: the through part holding the split, the branch part holding it, side (the branch on that
/// through part's left, +1, of its point order), its cover's length; numbers, REC_F per record; the covers, 4 per
/// entry: part, reversed, role (0 through, 1 branch), side on that part; their own arcs, 2 per entry; the height change
/// per input vertex (n)) of the forks among the parts (xy (n, 2), z (n,), hw (n,) per vertex; off: each
/// part's first vertex, the end last; tdir, rank per part).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn deck_sheets<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                        hw: PyReadonlyArray1<'py, f64>, off: Vec<i64>, tdir: Vec<i64>, rank: Vec<i64>,
                        params: PyReadonlyArray1<'py, f64>, node: Vec<f64>)
                        -> PyResult<(Vec<i64>, Vec<f64>, Vec<i64>, Vec<f64>, Vec<f64>)> {
    let (xy, z, hw, prm) = (xy.as_array(), z.as_array(), hw.as_array(), params.as_array().to_vec());
    let np = off.len().saturating_sub(1);
    let n = xy.nrows();
    if xy.ncols() != 2 || z.len() != n || hw.len() != n || prm.len() != NPARAMS || tdir.len() != np
        || rank.len() != np || off.first().copied().unwrap_or(0) != 0 || off.windows(2).any(|w| w[1] < w[0])
        || off.last().map(|&v| v as usize != n).unwrap_or(n != 0) || !(node.is_empty() || node.len() == 2) {
        return Err(PyValueError::new_err("deck_sheets: bad shapes"));
    }
    let mut parts = Vec::with_capacity(np);
    for k in 0..np {
        let (a, b) = (off[k] as usize, off[k + 1] as usize);
        let p: Vec<P2> = (a..b).map(|i| [xy[[i, 0]], xy[[i, 1]]]).collect();
        let zz: Vec<f64> = (a..b).map(|i| z[i]).collect();
        let hh: Vec<f64> = (a..b).map(|i| hw[i]).collect();
        let hwm = if b > a { 0.5 * (hh[0] + hh[hh.len() - 1]) } else { 0.0 };
        parts.push(Part { line: Line::new(p, zz, hh), tdir: tdir[k].signum(), rank: rank[k], hwm });
    }
    let recs = py.detach(|| {
        let mut out: Vec<Rec> = Vec::new();
        for b in 0..np {
            for e in 0..2 {
                if node.len() == 2 && !parts[b].line.p.is_empty() {
                    let q = if e == 0 { parts[b].line.p[0] } else { parts[b].line.p[parts[b].line.p.len() - 1] };
                    if q[0] != node[0] || q[1] != node[1] { continue; }      // (only the branch ends exactly at node)
                }
                if let Some(r) = fork_of(&parts, b, e, &prm) { out.push(r); }
            }
        }
        out
    });
    let mut ri: Vec<i64> = Vec::new();
    let mut rf: Vec<f64> = Vec::new();
    let mut ci: Vec<i64> = Vec::new();
    let mut cf: Vec<f64> = Vec::new();
    let mut dz = vec![0.0; n];
    for ((t, b, side), f, cov, d) in &recs {
        ri.extend_from_slice(&[*t as i64, *b as i64, *side, cov.len() as i64]);
        rf.extend_from_slice(f);
        for &(p, rev, role, sd, u0, u1) in cov {
            ci.extend_from_slice(&[p as i64, rev as i64, role, sd]);
            cf.extend_from_slice(&[u0, u1]);
        }
        for &(p, i, v) in d { dz[off[p] as usize + i] += v; }
    }
    Ok((ri, rf, ci, cf, dz))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn prm() -> Vec<f64> { vec![1.0, 0.766, 0.5, 1.2, 6.0, 20.0, 300.0, 0.5, 10.0, 2.0, 120.0, 0.5, 3.0, 0.03, 1.0] }

    fn part(p: Vec<P2>, z: f64, hw: f64, tdir: i64, rank: i64) -> Part {
        let n = p.len();
        Part { line: Line::new(p, vec![z; n], vec![hw; n]), tdir, rank, hwm: hw }
    }

    #[test]
    fn diverge_splits_where_edges_part() {
        // T straight east, B leaving at 1:10 to the north, both from the node at the origin, B 0.3 m higher
        let t = part(vec![[0.0, 0.0], [300.0, 0.0]], 10.0, 7.0, 1, 0);
        let mut b = part(vec![[0.0, 0.0], [300.0, 30.0]], 10.3, 3.5, 1, 1);
        b.line.z = vec![10.3, 11.5];
        let parts = vec![t, b];
        let r = fork_of(&parts, 1, 0, &prm()).expect("a fork");
        assert_eq!((r.0).0, 0);
        assert_eq!((r.0).2, 1);             // B on T's left
        // gap 1.2 m where the offset is 7 + 3.5 + 1.2 = 11.7 m: x = 117 m along T
        let f = r.1;
        assert!((f[2] - 117.0).abs() < 1.0, "sT {}", f[2]);
        assert_eq!(f[12], 1.0);
        // the node's height pinned to T's
        assert!(r.3.iter().any(|&(p, i, v)| p == 1 && i == 0 && (v + 0.3).abs() < 1e-9), "{:?}", r.3);
        // the reverse pair (T as the branch of B) is not counted: B is a ramp
        assert!(fork_of(&parts, 0, 0, &prm()).is_none());
    }

    #[test]
    fn heights_apart_are_no_union() {
        // the ramp climbing 10 m over its 300 m: 3.9 m above the deck at the split (over DZ_MAX): no record; a short
        // ramp ending 1 m off the deck's height inside the union: no record either (no room to blend back)
        let t = part(vec![[0.0, 0.0], [300.0, 0.0]], 10.0, 7.0, 1, 0);
        let mut b = part(vec![[0.0, 0.0], [300.0, 30.0]], 10.0, 3.5, 1, 1);
        b.line.z = vec![10.0, 20.0];
        assert!(fork_of(&[t, b], 1, 0, &prm()).is_none());
        let t = part(vec![[0.0, 0.0], [300.0, 0.0]], 10.0, 7.0, 1, 0);
        let mut b = part(vec![[0.0, 0.0], [40.0, 2.0]], 10.0, 3.5, 1, 1);
        b.line.z = vec![10.0, 11.0];
        assert!(fork_of(&[t, b], 1, 0, &prm()).is_none());
    }

    #[test]
    fn crossing_and_two_way_are_no_forks() {
        let t = part(vec![[0.0, 0.0], [300.0, 0.0]], 10.0, 7.0, 1, 0);
        let b = part(vec![[0.0, 0.0], [0.0, 300.0]], 10.0, 3.5, 1, 1);
        assert!(fork_of(&[t, b], 1, 0, &prm()).is_none());
        let t = part(vec![[0.0, 0.0], [300.0, 0.0]], 10.0, 7.0, 0, 0);
        let b = part(vec![[0.0, 0.0], [300.0, 30.0]], 10.0, 3.5, 1, 1);
        assert!(fork_of(&[t, b], 1, 0, &prm()).is_none());
        // a level apart
        let t = part(vec![[0.0, 0.0], [300.0, 0.0]], 10.0, 7.0, 1, 0);
        let b = part(vec![[0.0, 0.0], [300.0, 30.0]], 16.0, 3.5, 1, 1);
        assert!(fork_of(&[t, b], 1, 0, &prm()).is_none());
    }

    #[test]
    fn chains_follow_plain_continuations() {
        // the same diverge with the through deck and the ramp each in two pieces end to end: one record, split on the
        // second pieces, covering the first pieces whole
        let t1 = part(vec![[0.0, 0.0], [50.0, 0.0]], 10.0, 7.0, 1, 0);
        let t2 = part(vec![[50.0, 0.0], [300.0, 0.0]], 10.0, 7.0, 1, 0);
        let b1 = part(vec![[0.0, 0.0], [40.0, 4.0]], 10.0, 3.5, 1, 1);
        let b2 = part(vec![[300.0, 30.0], [40.0, 4.0]], 10.0, 3.5, -1, 1);
        let parts = vec![t1, t2, b1, b2];
        let r = fork_of(&parts, 2, 0, &prm()).expect("a fork");
        assert_eq!(((r.0).0, (r.0).1), (1, 3));
        assert!((r.1[2] - 67.0).abs() < 1.0, "sT {}", r.1[2]);
        assert!((r.1[0] - 183.9).abs() < 1.5, "sB {}", r.1[0]);
        let roles: Vec<(usize, i64)> = r.2.iter().map(|c| (c.0, c.2)).collect();
        assert_eq!(roles, vec![(0, 0), (1, 0), (2, 1), (3, 1)]);
    }

    #[test]
    fn merge_into_the_middle_of_a_through_part() {
        // T west to east through the node at x 100; B comes in from the south-west and ends at the node
        let t = part(vec![[-300.0, 0.0], [400.0, 0.0]], 10.0, 7.0, 1, 0);
        let b = part(vec![[-150.0, -15.0], [100.0, 0.0]], 10.0, 3.5, 1, 1);
        let parts = vec![t, b];
        let r = fork_of(&parts, 1, 1, &prm()).expect("a merge");
        assert_eq!((r.0).2, -1);            // B on T's right
        assert!(r.1[2] < 400.0 && r.1[2] > 150.0, "sT {}", r.1[2]);
    }
}
