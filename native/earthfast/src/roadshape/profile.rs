//! Road revamp v2 phase P3a (earthfast 516, BNE_ROADSHAPE=profile, SHADOW ONLY): one height profile through the
//! abutments (the design's section 1.4, Tier B). Nothing built reads it yet: server/roadshape.py writes the solved
//! profile beside the build (BNE_ROADSHAPE_DEBUG=1) and tools measure it (rideprobe.py).
//!
//! profile_members: the stretches the joint solve uses, from the ground anchor before a structure to the one after it.
//! - The members are a bridge chain's decks plus APPROACHES: from each deck end not on another deck, the ground road
//!   going on from it (an end of a ground line within JOIN_M, its direction within APPROACH_DEG of the deck's outward
//!   one; the straightest, then the nearest, then the lowest line index: the caller sorts the lines by their content),
//!   walked on through the line ends that go on the same way, up to the deck's anchor length (2 x L_vc(V) clamped, the
//!   caller's per deck). Deck ends meeting at one point that go on along the same ground line (a merge whose road goes
//!   on on the ground) get one approach, from the deck running straightest onto it. A walk that reaches another deck
//!   end of the chain stops there (a ground gap between two decks: one member coupled at both ends; found from both
//!   sides it is kept once, from the lower end). A walk with no line going on stops short (a dead end, a junction with
//!   nothing straight on): recorded.
//! - Deck ends within JOIN_M of each other are joined (C0); the pair at a point that runs straight on (mutual best,
//!   within APPROACH_DEG) also C1. A deck end inside another deck's edge is END_ON (the solve's sheet coupling). A
//!   joined end with nothing straight on and no approach at its point keeps its height (kind 4).
//! - Window independence: every value is a function of the decks and of the ground lines within the walk's reach
//!   (anchor length) of the deck ends; the caller passes every line with a vertex within that reach (plus a pad) and
//!   never the tile window. Ties by content order only.
//!
//! profile_solve: one joint solve per chain on the members' knots (STEP_M apart, the member's ends exact):
//!   min  sum w(d) h (z - z_today)^2 + LAMBDA sum h (z'')^2 + MU sum (couplings)^2
//! - z_today: a ground knot's local (Tier A) profile, a deck knot's height as built. w(d): EPS_W within D0 of an
//!   abutment, rising as ((d - D0) / (D1 - D0))^2 to W_G at D1 (d along the member from its abutment ends; a member
//!   with none, W_G): the joint bends, the far ground keeps its local profile and a viaduct's middle its height (with
//!   nothing pulling the deck knots, a long multi-level chain fell to its clearance floors: P3a measured it at I-95
//!   Middle Branch and Fort Pitt; the design's "deck knots only curvature, clearance and grade" holds within D1).
//! - z'' across a C1 joint uses the knots either side (the joint's own second difference): no kink at a through node
//!   nor at an abutment (every approach is C1 to its deck).
//! - Couplings (MU): C0 at joins; the sheet: a deck end inside the edge zone of another deck of the same level (not
//!   one it runs straight on into) is held to it knot by knot while the knot projects inside that deck, the edges stay
//!   within SPLIT_GAP_M and the two run within SHEET_DEG (P2b's union up to its split); today's two heights further
//!   apart than SHEET_DZ_MAX anywhere in it: a flyover, no union (an end held by nothing else keeps its height).
//! - P3d (534): twins (two decks side by side, structprobe's BESIDE rule) held at one height knot by knot, each one's
//!   knots on the other's line; two deck ends at one junction (within the wider one's half width, NODE_DZ_M today)
//!   held at one height; a deck end on a deck of another OSM level a sheet end when within NODE_DZ_M today; an
//!   approach under a deck held its clearance under it (the pins' ground). A grade failure lets go of a coupling
//!   group holding that member first (the last made first), as a clearance conflict does.
//! - P3e (536): a deck over another along it (an overlap, not only side by side) held as twins are; a twin's run
//!   of knots beside the other held whole once one of them is within TWIN_DZ_M today (a ramp parting uphill).
//! - Pins (MU): an approach that reached its anchor keeps z and z' there (its last two knots), one stopped short (a
//!   junction) z; a deck end with nothing on keeps z.
//! - Clearances (an active set, ROUNDS rounds with hysteresis: in past ACTIVE_TOL, out past RELEASE_TOL; each never
//!   worse than today's: min(bound, today's value)): the chain's crossing floors (roads, rails, water as roads._solve
//!   asked them; not within AT_GRADE_M of an abutment), deck over ground (z >= ground under + DECK_GROUND_M away from
//!   the abutments), deck over deck where two deck lines cross more than STACK_MIN_M apart today (the higher today
//!   stays over the lower by min(the lower's clearance need, today's gap): the solve never swaps two decks).
//! - Checks: couplings within MARGIN, clearances within MARGIN, no segment steeper than max(MAX_GRADE, the member's
//!   steepest today) + GRADE_ADD, no C1 joint bending more than SLOPE_TOL + 1.5 x its members' own bend + KINK_STEEP x
//!   the steeper member's steepest grade today (523). A coupling or clearance failure that a sheet union takes part in lets that union go
//!   and solves again (at most ATTEMPTS solves; counted); any other failure keeps today's heights (status recorded).
//! - Budget: structures::heights' (checked counts before allocation; every factorisation charged before it runs):
//!   over it today's heights (S_BUDGET).
//! Solver: structures::heights' RCM order + envelope LDL^T (fixed order of every sum, basic IEEE operations and sqrt
//! only, no FMA, no HashMap). Output rounded to 1 mm. The same on every platform.
use std::collections::{BTreeMap, BTreeSet};

use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::structures::heights::{factor_cost, ldl_solve, rcm, BUDGET};

#[path = "profile_qp.rs"]
mod profile_qp;

pub const S_SOLVED: i64 = 0;
pub const S_PINNED: i64 = 1;
pub const S_INFEASIBLE: i64 = 2;
pub const S_CLEARANCE: i64 = 3;
pub const S_GRADE: i64 = 4;
pub const S_SOLVER: i64 = 5;
pub const S_BUDGET: i64 = 6;

const MEMBER_PARAMS: usize = 5;
const SOLVE_PARAMS: usize = 18;
const GRID_M: f64 = 8.0;
const MAX_KNOTS: usize = 400_000;
const ATTEMPTS: usize = 10;
const JOIN_DZ_M: f64 = 0.5;     // m: two joined deck ends further apart in height today are stacked, not joined
const SLOPE_TOL: f64 = 0.01;    // the grade either side of a C1 joint may differ by this in a solved result (or
                                // by as much as its members bend inside, between two pieces)
const KINK_STEEP: f64 = 0.1;
const GAP_LOOK: f64 = 2.0;      // an approach's walk looks this many anchors on for another deck end (P3c)    // ... plus this share of the steeper member's steepest grade today (P3c)
const NODE_DZ_M: f64 = 0.5;     // m: a deck end on a deck of another OSM level this near in height today is held to
                                // it (roadshape.SHEET_NODE_DZ, P2b's fork test at the node; P3d)
// (P3d, 534) twins: two decks side by side (structprobe's BESIDE: the edges at most TWIN_GAP_M apart, or overlapping,
// running within TWIN_DEG of each other, at most TWIN_DZ_M apart in height today) for at least min(TWIN_MIN_M,
// TWIN_SHORT x the shorter deck) are held at one height knot by knot (the design's equal z across shared pavement);
// a pair in conflict with a clearance or another coupling is let go as a sheet union is
const TWIN_GAP_M: f64 = 2.5;
const TWIN_TOUCH_M: f64 = 0.5;  // m two twins' carriageways may overlap ([18] off: more is an overlap, roadmesh's trim)
const TWIN_HOLD_M: f64 = 1.0;   // m past TWIN_GAP_M a qualifying pair's knots are still held (no step where it parts)
const TWIN_COS: f64 = 0.906_307_787_036_649_9;     // cos(25 degrees)
const TWIN_DZ_M: f64 = 1.0;
const TWIN_MIN_M: f64 = 20.0;
const TWIN_SHORT: f64 = 0.8;
const TWIN_END_M: f64 = 1e-6;   // m: a knot whose nearest point on the other is its end (clamped there) is past it
const APPROACH_PAD_M: f64 = 0.02;   // m an approach under a deck is held under its clearance more (P3d)
const STACK_MIN_M: f64 = 1.0;   // m: two decks crossing closer than this in height today are one junction (no clearance)      // solves at most: each after letting go of a sheet union in conflict

fn arcs(xy: &[[f64; 2]]) -> Vec<f64> {
    let mut s = Vec::with_capacity(xy.len());
    for k in 0..xy.len() {
        if k == 0 {
            s.push(0.0);
        } else {
            let (dx, dy) = (xy[k][0] - xy[k - 1][0], xy[k][1] - xy[k - 1][1]);
            s.push(s[k - 1] + (dx * dx + dy * dy).sqrt());
        }
    }
    s
}

fn d2(a: [f64; 2], b: [f64; 2]) -> f64 {
    let (dx, dy) = (a[0] - b[0], a[1] - b[1]);
    dx * dx + dy * dy
}

/// whether p's foot on polyline xy's end segment at end e (0: its start) lies past that end (beyond it, not on it)
fn past_end(xy: &[[f64; 2]], e: usize, p: [f64; 2]) -> bool {
    let n = xy.len();
    let (q, r) = if e == 0 { (xy[0], xy[1]) } else { (xy[n - 1], xy[n - 2]) };
    let d = [q[0] - r[0], q[1] - r[1]];
    (p[0] - q[0]) * d[0] + (p[1] - q[1]) * d[1] > 1e-9
}

fn unit(v: [f64; 2]) -> [f64; 2] {
    let n = (v[0] * v[0] + v[1] * v[1]).sqrt();
    if n > 1e-12 {
        [v[0] / n, v[1] / n]
    } else {
        [0.0, 0.0]
    }
}

fn dot(a: [f64; 2], b: [f64; 2]) -> f64 {
    a[0] * b[0] + a[1] * b[1]
}

/// point at arc t of (xy, s) (clamped)
fn point_at(xy: &[[f64; 2]], s: &[f64], t: f64) -> [f64; 2] {
    let n = xy.len();
    if n == 1 || t <= 0.0 {
        return xy[0];
    }
    if t >= s[n - 1] {
        return xy[n - 1];
    }
    let k = s[1..n - 1].partition_point(|&v| v <= t);
    let d = s[k + 1] - s[k];
    let u = if d > 0.0 { (t - s[k]) / d } else { 0.0 };
    [xy[k][0] + u * (xy[k + 1][0] - xy[k][0]), xy[k][1] + u * (xy[k + 1][1] - xy[k][1])]
}

/// unit direction OUT of the line at end e (0: its first vertex), over `reach` m
fn out_dir(xy: &[[f64; 2]], s: &[f64], e: usize, reach: f64) -> [f64; 2] {
    let n = xy.len();
    let l = s[n - 1];
    if e == 0 {
        let q = point_at(xy, s, reach.min(l));
        unit([xy[0][0] - q[0], xy[0][1] - q[1]])
    } else {
        let q = point_at(xy, s, (l - reach).max(0.0));
        unit([xy[n - 1][0] - q[0], xy[n - 1][1] - q[1]])
    }
}

/// knots every `step` m at most along (xy, s): n equal pieces, both ends exact
fn resample(xy: &[[f64; 2]], s: &[f64], step: f64) -> (Vec<[f64; 2]>, Vec<f64>) {
    let l = s[s.len() - 1];
    let n = ((l / step).ceil() as usize).max(1);
    let mut out = Vec::with_capacity(n + 1);
    let mut t = Vec::with_capacity(n + 1);
    for i in 0..=n {
        let ti = if i == n { l } else { l * (i as f64) / (n as f64) };
        out.push(if i == n { xy[xy.len() - 1] } else { point_at(xy, s, ti) });
        t.push(ti);
    }
    (out, t)
}

/// (distance^2, arc) of the nearest point of polyline (xy, s) to p (the lowest segment on ties)
fn nearest(xy: &[[f64; 2]], s: &[f64], p: [f64; 2]) -> (f64, f64) {
    let mut best = (f64::INFINITY, 0.0);
    for k in 0..xy.len().saturating_sub(1) {
        let (a, b) = (xy[k], xy[k + 1]);
        let v = [b[0] - a[0], b[1] - a[1]];
        let vv = dot(v, v);
        let u = if vv > 0.0 { (dot([p[0] - a[0], p[1] - a[1]], v) / vv).clamp(0.0, 1.0) } else { 0.0 };
        let q = [a[0] + u * v[0], a[1] + u * v[1]];
        let dd = d2(p, q);
        if dd < best.0 {
            best = (dd, s[k] + u * (s[k + 1] - s[k]));
        }
    }
    best
}

fn lines_of(xy: &numpy::ndarray::ArrayView2<f64>, off: &[i64]) -> Vec<Vec<[f64; 2]>> {
    (0..off.len().saturating_sub(1)).map(|i| {
        let mut v: Vec<[f64; 2]> = Vec::new();
        for k in off[i] as usize..off[i + 1] as usize {
            let p = [xy[[k, 0]], xy[[k, 1]]];
            if v.last().map(|q| d2(*q, p) > 1e-12).unwrap_or(true) {
                v.push(p);
            }
        }
        v
    }).collect()
}

fn check_off(off: &[i64], n: usize) -> bool {
    off.first().copied().unwrap_or(0) == 0 && off.windows(2).all(|w| w[1] >= w[0])
        && off.last().map(|&v| v as usize == n).unwrap_or(n == 0)
}

fn gkey(p: [f64; 2], cell: f64) -> (i64, i64) {
    ((p[0] / cell).floor() as i64, (p[1] / cell).floor() as i64)
}

pub struct Approach {
    pub xy: Vec<[f64; 2]>,
    pub deck: usize,
    pub end: usize,
    pub to: i64,        // deck * 2 + end it reaches, or -1
    pub reach: i64,     // 0 anchor reached, 1 stopped short (nothing going on), 2 reached another deck end
}

pub struct MembersOut {
    pub approaches: Vec<Approach>,
    pub joins: Vec<[i64; 5]>,       // (deck a, end a, deck b, end b, c1)
    pub ends: Vec<[i64; 3]>,        // (deck, end, kind: 0 nothing on, 1 approach, 2 joined, 3 end on a deck,
                                    // 4 joined with nothing going on from the point: kept)
}

/// profile_members' work (pure)
type SourceIds = (Vec<String>, Vec<String>);

pub fn members_core(decks: &[Vec<[f64; 2]>], half: &[f64], anchor: &[f64], ground: &[Vec<[f64; 2]>], prm: &[f64], source: Option<&SourceIds>)
    -> MembersOut {
    let (join_m, dir_m, cos_on, step, max_pts) = (prm[0], prm[1], prm[2], prm[3], prm[4] as usize);
    let ds: Vec<Vec<f64>> = decks.iter().map(|l| arcs(l)).collect();
    let nd = decks.len();
    let ok: Vec<bool> = (0..nd).map(|d| decks[d].len() >= 2 && ds[d][ds[d].len() - 1] > 1e-6).collect();
    let end_pt = |d: usize, e: usize| if e == 0 { decks[d][0] } else { decks[d][decks[d].len() - 1] };
    let end_out: Vec<[[f64; 2]; 2]> = (0..nd).map(|d| if ok[d] {
        [out_dir(&decks[d], &ds[d], 0, dir_m), out_dir(&decks[d], &ds[d], 1, dir_m)]
    } else { [[0.0; 2]; 2] }).collect();
    let jm2 = join_m * join_m;
    // joins between deck ends
    let mut joins = Vec::new();
    let mut joined = vec![[false; 2]; nd];
    let mut best_on: Vec<[Option<(usize, usize)>; 2]> = vec![[None, None]; nd];
    for a in 0..nd {
        if !ok[a] {
            continue;
        }
        for ea in 0..2 {
            let mut best: Option<(f64, usize, usize)> = None;
            for b in 0..nd {
                if !ok[b] || b == a {
                    continue;
                }
                for eb in 0..2 {
                    if d2(end_pt(a, ea), end_pt(b, eb)) <= jm2 {
                        joined[a][ea] = true;
                        let c = -dot(end_out[a][ea], end_out[b][eb]);
                        if c >= cos_on && best.map(|q| c > q.0).unwrap_or(true) {
                            best = Some((c, b, eb));
                        }
                    }
                }
            }
            best_on[a][ea] = best.map(|q| (q.1, q.2));
        }
    }
    for a in 0..nd {
        if !ok[a] {
            continue;
        }
        for ea in 0..2 {
            for b in a + 1..nd {
                if !ok[b] {
                    continue;
                }
                for eb in 0..2 {
                    if d2(end_pt(a, ea), end_pt(b, eb)) <= jm2 {
                        let c1 = best_on[a][ea] == Some((b, eb)) && best_on[b][eb] == Some((a, ea));
                        joins.push([a as i64, ea as i64, b as i64, eb as i64, c1 as i64]);
                    }
                }
            }
        }
    }
    // ends on another deck's interior (inside its edge)
    let mut end_on = vec![[false; 2]; nd];
    for a in 0..nd {
        if !ok[a] {
            continue;
        }
        for ea in 0..2 {
            if joined[a][ea] {
                continue;
            }
            let p = end_pt(a, ea);
            for b in 0..nd {
                if b == a || !ok[b] {
                    continue;
                }
                let (dd, t) = nearest(&decks[b], &ds[b], p);
                let lb = ds[b][ds[b].len() - 1];
                let r = half[b] + join_m;
                if dd <= r * r && t > join_m && t < lb - join_m {
                    end_on[a][ea] = true;
                    break;
                }
            }
        }
    }
    // ground line ends, indexed
    let gs: Vec<Vec<f64>> = ground.iter().map(|l| arcs(l)).collect();
    let gok: Vec<bool> = (0..ground.len()).map(|g| ground[g].len() >= 2 && gs[g][gs[g].len() - 1] > 1e-6).collect();
    let mut grid: BTreeMap<(i64, i64), Vec<(usize, usize)>> = BTreeMap::new();
    let cell = join_m.max(1.0) * 2.0;
    for g in 0..ground.len() {
        if !gok[g] {
            continue;
        }
        for e in 0..2 {
            let p = if e == 0 { ground[g][0] } else { ground[g][ground[g].len() - 1] };
            grid.entry(gkey(p, cell)).or_default().push((g, e));
        }
    }
    let gin: Vec<[[f64; 2]; 2]> = (0..ground.len()).map(|g| if gok[g] {
        let o = [out_dir(&ground[g], &gs[g], 0, dir_m), out_dir(&ground[g], &gs[g], 1, dir_m)];
        [[-o[0][0], -o[0][1]], [-o[1][0], -o[1][1]]]       // (into the line from each end)
    } else { [[0.0; 2]; 2] }).collect();
    let deck_end_near = |p: [f64; 2], skip: (usize, usize)| -> Option<(usize, usize)> {
        for d in 0..nd {
            if !ok[d] {
                continue;
            }
            for e in 0..2 {
                if (d, e) != skip && d2(end_pt(d, e), p) <= jm2 {
                    return Some((d, e));
                }
            }
        }
        None
    };
    let mut approaches = Vec::new();
    let mut walks: Vec<(usize, usize, (usize, usize, f64), Approach)> = Vec::new();
    let mut ends = Vec::new();
    for d in 0..nd {
        for e in 0..2 {
            if !ok[d] {
                ends.push([d as i64, e as i64, 0]);
                continue;
            }
            if end_on[d][e] && source.is_none() {
                ends.push([d as i64, e as i64, 3]);
                continue;
            }
            let mut p = end_pt(d, e);
            let mut u = end_out[d][e];
            let mut path = vec![p];
            let mut len = 0.0f64;
            let lim = anchor[d];
            // (P3c, earthfast 523: the walk looks on to GAP_LOOK x its anchor for another deck's end along the road:
            // found, one member between the two (reach 2); else cut back to its anchor. Two anchors walked from two
            // abutments 135 m apart along one road overlapped, each pinned to today's ground at the other's moved
            // abutment: I-95 Middle Branch's 4.5 g, the two solved 5 m apart on one road)
            let look = lim * GAP_LOOK;
            let mut cut: Option<(usize, [f64; 2])> = None;     // (path length before the anchor point, the point)
            let mut used = BTreeSet::new();
            let (mut reach, mut to) = (1i64, -1i64);
            let mut first: Option<(usize, usize, f64)> = None;
            loop {
                let (cx, cy) = gkey(p, cell);
                let mut best: Option<(f64, f64, usize, usize)> = None;
                for gx in cx - 1..=cx + 1 {
                    for gy in cy - 1..=cy + 1 {
                        if let Some(v) = grid.get(&(gx, gy)) {
                            for &(g, ge) in v {
                                if used.contains(&g) || (first.is_none() && end_on[d][e] &&
                                    source.is_some_and(|(decks, ground)| decks[d] != ground[g])) {
                                    continue;
                                }
                                let q = if ge == 0 { ground[g][0] } else { ground[g][ground[g].len() - 1] };
                                let dd = d2(p, q);
                                if dd > jm2 {
                                    continue;
                                }
                                let c = dot(u, gin[g][ge]);
                                if c < cos_on {
                                    continue;
                                }
                                let better = match best {
                                    None => true,
                                    Some((bc, bd, bg, be)) => c > bc || (c == bc && (dd < bd || (dd == bd && (g, ge) < (bg, be)))),
                                };
                                if better {
                                    best = Some((c, dd, g, ge));
                                }
                            }
                        }
                    }
                }
                let Some((bc, _, g, ge)) = best else { break };
                if first.is_none() {
                    first = Some((g, ge, bc));
                }
                used.insert(g);
                let pts: Vec<[f64; 2]> = if ge == 0 { ground[g].clone() } else { ground[g].iter().rev().copied().collect() };
                let mut done = false;
                for q in pts.into_iter() {
                    let last = path[path.len() - 1];
                    let seg = d2(last, q).sqrt();
                    if seg <= 1e-6 {
                        continue;
                    }
                    if cut.is_none() && len + seg >= lim {
                        let f = (lim - len) / seg;
                        cut = Some((path.len(), [last[0] + f * (q[0] - last[0]), last[1] + f * (q[1] - last[1])]));
                    }
                    if len + seg >= look {
                        reach = 0;
                        done = true;
                        break;
                    }
                    path.push(q);
                    len += seg;
                    if let Some((d2_, e2_)) = deck_end_near(q, (d, e)) {
                        reach = 2;
                        to = (d2_ * 2 + e2_) as i64;
                        done = true;
                        break;
                    }
                }
                if done || path.len() > max_pts {
                    break;
                }
                p = path[path.len() - 1];
                let ps = arcs(&path);
                u = out_dir(&path, &ps, 1, dir_m);
            }
            if reach != 2 {
                if let Some((n, q)) = cut {
                    path.truncate(n);
                    path.push(q);
                    len = lim;
                    reach = 0;
                }
            }
            if path.len() < 2 || len < step {
                ends.push([d as i64, e as i64, if end_on[d][e] { 3 } else if joined[d][e] { 2 } else { 0 }]);
                continue;
            }
            let ps = arcs(&path);
            let (kn, _) = resample(&path, &ps, step);
            walks.push((d, e, first.unwrap_or((usize::MAX, 0, 0.0)), Approach { xy: kn, deck: d, end: e, to, reach }));
            ends.push([d as i64, e as i64, 1]);
        }
    }
    // (deck ends meeting at one point that go on along the same ground line: one approach, from the deck running
    // straightest onto it, then the lowest end; a ground gap between two decks found from both sides: kept from the
    // lower end)
    let mut keep = vec![true; walks.len()];
    for i in 0..walks.len() {
        for j in 0..walks.len() {
            if i == j || !keep[j] {
                continue;
            }
            let (a, b) = (&walks[i], &walks[j]);
            let same_start = a.2 .0 != usize::MAX && (a.2 .0, a.2 .1) == (b.2 .0, b.2 .1);
            let same_gap = a.3.reach == 2 && b.3.reach == 2 && a.3.to == (b.0 * 2 + b.1) as i64
                && b.3.to == (a.0 * 2 + a.1) as i64;
            if (same_start && (b.2 .2 > a.2 .2 || (b.2 .2 == a.2 .2 && (b.0, b.1) < (a.0, a.1))))
                || (same_gap && (b.0, b.1) < (a.0, a.1)) {
                keep[i] = false;
                break;
            }
        }
    }
    // (P3c: an anchor walked from a deck end another walk reached as its gap's far end, found from that side alone
    // (the other's anchor shorter): the gap member covers its road; Codex review of P3c)
    for i in 0..walks.len() {
        if !keep[i] || walks[i].3.reach == 2 {
            continue;
        }
        let me = (walks[i].0 * 2 + walks[i].1) as i64;
        if (0..walks.len()).any(|j| j != i && keep[j] && walks[j].3.reach == 2 && walks[j].3.to == me) {
            keep[i] = false;
        }
    }
    for (i, w) in walks.into_iter().enumerate() {
        if keep[i] {
            approaches.push(w.3);
        } else {
            for en in ends.iter_mut() {
                if en[0] == w.0 as i64 && en[1] == w.1 as i64 {
                    en[2] = if joined[w.0][w.1] { 2 } else { 1 };
                }
            }
        }
    }
    // (a joined end with no approach at its point and no straight-on partner: its height kept, kind 4)
    let has_app: Vec<[f64; 2]> = approaches.iter().map(|a| a.xy[0]).collect();
    for en in ends.iter_mut() {
        let (d, e) = (en[0] as usize, en[1] as usize);
        if en[2] == 2 && best_on[d][e].is_none() && !has_app.iter().any(|q| d2(*q, end_pt(d, e)) <= jm2) {
            en[2] = 4;
        }
    }
    MembersOut { approaches, joins, ends }
}

/// The stretches of one bridge chain from anchor to anchor (module doc). decks: the chain's deck lines (flat xy, off);
/// half: their half widths; anchor: each deck's anchor length (m); ground: the ground road lines within reach (flat,
/// off; sorted by content); params [JOIN_M, DIR_M, cos(APPROACH_DEG), STEP_M, MAX_PTS]. Returns (approach knots flat
/// (k, 2), their off, per approach (deck, end, to, reach) flat, joins flat (da, ea, db, eb, c1), ends flat (d, e,
/// kind)).
#[pyfunction]
#[allow(clippy::type_complexity)]
#[pyo3(signature = (dxy, doff, half, anchor, gxy, goff, params, source=None))]
pub fn profile_members<'py>(_py: Python<'py>, dxy: PyReadonlyArray2<'py, f64>, doff: Vec<i64>, half: Vec<f64>,
                            anchor: Vec<f64>, gxy: PyReadonlyArray2<'py, f64>, goff: Vec<i64>, params: Vec<f64>, source: Option<SourceIds>)
                            -> PyResult<(Vec<f64>, Vec<i64>, Vec<i64>, Vec<i64>, Vec<i64>)> {
    let (dxy, gxy) = (dxy.as_array(), gxy.as_array());
    let nd = doff.len().saturating_sub(1);
    if dxy.ncols() != 2 || gxy.ncols() != 2 || !check_off(&doff, dxy.nrows()) || !check_off(&goff, gxy.nrows())
        || source.as_ref().is_some_and(|(d, g)| d.len() != nd || g.len()+1 != goff.len())
        || half.len() != nd || anchor.len() != nd || params.len() != MEMBER_PARAMS {
        return Err(PyValueError::new_err("profile_members: bad shapes"));
    }
    let decks = lines_of(&dxy, &doff);
    let ground = lines_of(&gxy, &goff);
    let out = members_core(&decks, &half, &anchor, &ground, &params, source.as_ref());
    let mut axy = Vec::new();
    let mut aoff = vec![0i64];
    let mut ameta = Vec::new();
    for a in &out.approaches {
        for p in &a.xy {
            axy.push(p[0]);
            axy.push(p[1]);
        }
        aoff.push(aoff[aoff.len() - 1] + a.xy.len() as i64);
        ameta.extend_from_slice(&[a.deck as i64, a.end as i64, a.to, a.reach]);
    }
    let joins = out.joins.iter().flat_map(|j| j.iter().copied()).collect();
    let ends = out.ends.iter().flat_map(|j| j.iter().copied()).collect();
    Ok((axy, aoff, ameta, joins, ends))
}

// ---- the solve -----------------------------------------------------------------------------------------------------

pub struct Member {
    pub xy: Vec<[f64; 2]>,
    pub s: Vec<f64>,
    pub z: Vec<f64>,        // today's (deck as built; ground: the local profile)
    pub g: Vec<f64>,        // the ground under each knot (NaN: unknown)
    pub deck: bool,
    pub half: f64,
    pub level: f64,
    pub under: f64,         // the clearance a deck over it needs
    pub abut: [bool; 2],    // an approach's ends that meet a deck (ground weight measured from them)
    pub dabut: [bool; 2],   // a deck's ends that are abutments (an approach on, or nothing on)
}

/// One inequality: sum c z >= r (or an equality held by MU when active / always for couplings)
#[derive(Clone)]
struct Lin {
    terms: Vec<(usize, f64)>,
    r: f64,
}

fn lin_at(base: &[usize], m: &Member, mi: usize, t: f64, sign: f64, terms: &mut Vec<(usize, f64)>) {
    let s = &m.s;
    let n = s.len();
    let b = base[mi];
    if t <= s[0] || n == 1 {
        terms.push((b, sign));
        return;
    }
    if t >= s[n - 1] {
        terms.push((b + n - 1, sign));
        return;
    }
    let k = s[1..n - 1].partition_point(|&v| v <= t);
    let d = s[k + 1] - s[k];
    let u = if d > 0.0 { (t - s[k]) / d } else { 0.0 };
    if u < 1.0 {
        terms.push((b + k, sign * (1.0 - u)));
    }
    if u > 0.0 {
        terms.push((b + k + 1, sign * u));
    }
}

fn lin_val(z: &[f64], l: &Lin) -> f64 {
    let mut v = 0.0;
    for &(j, c) in &l.terms {
        v += c * z[j];
    }
    v - l.r
}

fn z_at(m: &Member, t: f64) -> f64 {
    let s = &m.s;
    let n = s.len();
    if t <= s[0] || n == 1 {
        return m.z[0];
    }
    if t >= s[n - 1] {
        return m.z[n - 1];
    }
    let k = s[1..n - 1].partition_point(|&v| v <= t);
    let d = s[k + 1] - s[k];
    let u = if d > 0.0 { (t - s[k]) / d } else { 0.0 };
    m.z[k] + u * (m.z[k + 1] - m.z[k])
}

/// segment-segment crossing: (u on a, v on b) both in [0, 1]
fn seg_cross(a0: [f64; 2], a1: [f64; 2], b0: [f64; 2], b1: [f64; 2]) -> Option<(f64, f64)> {
    let r = [a1[0] - a0[0], a1[1] - a0[1]];
    let q = [b1[0] - b0[0], b1[1] - b0[1]];
    let den = r[0] * q[1] - r[1] * q[0];
    if den.abs() < 1e-12 {
        return None;
    }
    let w = [b0[0] - a0[0], b0[1] - a0[1]];
    let u = (w[0] * q[1] - w[1] * q[0]) / den;
    let v = (w[0] * r[1] - w[1] * r[0]) / den;
    if (0.0..=1.0).contains(&u) && (0.0..=1.0).contains(&v) {
        Some((u, v))
    } else {
        None
    }
}

pub struct SolveOut {
    pub z: Vec<f64>,
    pub tried: Vec<f64>,    // the heights a clearance or grade failure threw away (diagnostics; else empty)
    pub status: i64,
    pub report: Vec<f64>,   // status, knots, rounds, active, sheet pairs, crossings, max coupling residual, unmet, flops,
                            // unions let go, the failing member, the failing clearance's kind, its shortfall (m)
}

fn add_term(rows: &mut [BTreeMap<usize, f64>], rhs: &mut [f64], pos: &[usize], terms: &[(usize, f64)], r: f64, w: f64) {
    for &(i, ci) in terms {
        let pi = pos[i];
        rhs[pi] += w * ci * r;
        for &(j, cj) in terms {
            let pj = pos[j];
            if pj <= pi {
                *rows[pi].entry(pj).or_insert(0.0) += w * ci * cj;
            }
        }
    }
}

// Lane graph event in the chain frame: trunk, through and branch member indices,
// connector XY and distance from the connector to the physical nose on the through arm.
type SurfaceFork = (i64, usize, usize, f64, f64, f64);

fn bearing(m: &Member, node: [f64; 2]) -> Option<usize> {
    let ends = [m.xy[0], *m.xy.last()?];
    let e = usize::from(d2(ends[1], node) < d2(ends[0], node));
    (d2(ends[e], node) <= 0.75 * 0.75).then_some(e)
}

/// Supply the ground endpoints for the legacy check, or the full vertices for
/// S4c. An interior vertex must coincide with the deck bearing itself: a line
/// merely passing under its end remains a crossing.
#[pyfunction]
pub fn surface_road_floors(decks: Vec<[[f64; 3]; 2]>, ground: Vec<Vec<[f64; 2]>>,
                           crossings: Vec<(usize, usize, f64, f64, f64)>) -> PyResult<Vec<bool>> {
    crossings.into_iter().map(|(d, g, x, y, z)| {
        let (Some(deck), Some(road)) = (decks.get(d), ground.get(g)) else {
            return Err(PyValueError::new_err("surface_road_floors: unknown part"));
        };
        let p = [x, y];
        let attached = deck.iter().any(|q| {
            let end = [q[0], q[1]];
            if d2(end, p) > 0.75 * 0.75 || (q[2] - z).abs() >= STACK_MIN_M { return false; }
            road.iter().enumerate().any(|(i, &v)| {
                if i == 0 || i + 1 == road.len() { d2(v, p) <= 0.75 * 0.75 }
                else { d2(v, p) < 0.05 * 0.05 && d2(end, v) < 0.05 * 0.05 }
            })
        });
        Ok(!attached)
    }).collect()
}

/// A pin and the grade check bound every reachable knot on the same member.
/// Ignore sub-0.5 m segments exactly as the grade check does. Interpolation of
/// two upper bounds is an upper bound on the height at a recorded crossing.
fn pinned_ceiling(ms: &[Member], base: &[usize], pins: &[Lin], n: usize,
                  max_grade: f64, grade_add: f64, margin: f64) -> Vec<f64> {
    let mut ceiling = vec![f64::INFINITY; n];
    for p in pins {
        let i = p.terms[0].0;
        ceiling[i] = ceiling[i].min(p.r + margin);
    }
    for (m, &start) in ms.iter().zip(base) {
        let bounds = &mut ceiling[start..start + m.s.len()];
        if bounds.iter().all(|h| !h.is_finite()) { continue; }
        let mut grade = max_grade;
        for (s, z) in m.s.windows(2).zip(m.z.windows(2)) {
            let ds = s[1] - s[0];
            if ds >= 0.5 { grade = grade.max((z[1] - z[0]).abs() / ds); }
        }
        grade += grade_add;
        for k in 1..bounds.len() {
            let ds = m.s[k] - m.s[k - 1];
            if ds >= 0.5 { bounds[k] = bounds[k].min(bounds[k - 1] + ds * grade); }
        }
        for k in (1..bounds.len()).rev() {
            let ds = m.s[k] - m.s[k - 1];
            if ds >= 0.5 { bounds[k - 1] = bounds[k - 1].min(bounds[k] + ds * grade); }
        }
    }
    ceiling
}

/// Follow the through joins from a fork arm, including an approach where the
/// pavement continues on ground. Stop at an ambiguous junction or a cycle.
type ThroughJoins = BTreeMap<(usize, usize), BTreeSet<(usize, usize)>>;

fn fork_arm(ms: &[Member], through: &ThroughJoins, first: (usize, usize), reach: f64)
    -> Vec<(usize, usize, f64)> {
    let mut path = Vec::new();
    let mut used = BTreeSet::new();
    let (mut m, mut e) = first;
    let mut start = 0.0;
    while used.insert(m) {
        path.push((m, e, start));
        start += ms[m].s.last().unwrap();
        if start >= reach { break; }
        let Some(next) = through.get(&(m, 1 - e)) else { break };
        if next.len() != 1 { break; }
        (m, e) = *next.first().unwrap();
    }
    path
}

type GroundFloors = (Vec<String>, Vec<(String, f64)>);

/// floors: (member, x, y, floor m).
fn solve_core(ms: &[Member], joins: &[[i64; 5]], pins: &[[i64; 3]], floors: &[(usize, [f64; 2], f64)], prm: &[f64],
              surface: Option<&[SurfaceFork]>, robust: bool, ground_floors: Option<&GroundFloors>, traffic_gap: f64)
    -> SolveOut {
    let (lambda, w_g, d0, d1, eps_w, mu) = (prm[0], prm[1], prm[2], prm[3], prm[4], prm[5]);
    let (split_gap, sheet_dz, cos_sheet, sheet_reach, at_grade, deck_ground) = (prm[6], prm[7], prm[8], prm[9], prm[10], prm[11]);
    let (max_grade, grade_add, margin, rounds_max, active_tol, release_tol) = (prm[12], prm[13], prm[14], prm[15] as usize, prm[16], prm[17]);
    // (P3e, 536: optional [18] decks over each other along held as twins (1), [19] a twin run held whole once seeded
    // (1), [20] solves at most, [21] a failure lets go of every twin group holding its members at once (1); 18
    // values: P3d's rules)
    let opt = |i: usize, d: f64| if prm.len() > i { prm[i] } else { d };
    let (twin_overlap, twin_flood) = (opt(18, 0.0) != 0.0, opt(19, 0.0) != 0.0);
    let attempts = (opt(20, ATTEMPTS as f64) as usize).clamp(1, 32);
    let drop_all = opt(21, 0.0) != 0.0;
    let mut base = Vec::with_capacity(ms.len());
    let mut n = 0usize;
    for m in ms {
        base.push(n);
        n += m.xy.len();
    }
    let today: Vec<f64> = ms.iter().flat_map(|m| m.z.iter().copied()).collect();
    // (two deck ends near in plan but apart in height today are no joint (stacked ends): not coupled, both kept)
    let end_z = |m: usize, e: usize| if e == 0 { ms[m].z[0] } else { ms[m].z[ms[m].z.len() - 1] };
    let mut joins_ok: Vec<[i64; 5]> = Vec::new();
    let mut pins_v: Vec<[i64; 3]> = pins.to_vec();
    for j in joins {
        let (a, ea, b, eb) = (j[0] as usize, j[1] as usize, j[2] as usize, j[3] as usize);
        if a < ms.len() && b < ms.len() && ms[a].deck && ms[b].deck && (end_z(a, ea) - end_z(b, eb)).abs() > JOIN_DZ_M {
            pins_v.push([a as i64, ea as i64, 1]);
            pins_v.push([b as i64, eb as i64, 1]);
            continue;
        }
        joins_ok.push(*j);
    }
    let mut pavement = BTreeSet::new();
    for &(trunk, main, branch, x, y, reach) in surface.unwrap_or_default() {
        let node = [x, y];
        let (Some(em), Some(eb)) = (bearing(&ms[main], node), bearing(&ms[branch], node)) else { continue };
        if (end_z(main, em) - end_z(branch, eb)).abs() > JOIN_DZ_M { continue; }
        pavement.insert((main, em, branch, eb, reach.to_bits()));
        if let Some(et) = usize::try_from(trunk).ok().and_then(|t| bearing(&ms[t], node)) {
            let thru = [(trunk, et as i64), (main as i64, em as i64)];
            if joins_ok.iter().any(|j| thru.contains(&(j[0], j[1])) && thru.contains(&(j[2], j[3]))) {
                for j in &mut joins_ok {
                    let a = thru.contains(&(j[0], j[1]));
                    let b = thru.contains(&(j[2], j[3]));
                    if a || b { j[4] = i64::from(a && b); }
                }
            }
        }
    }
    let joins: &[[i64; 5]] = &joins_ok;
    let pins: &[[i64; 3]] = &pins_v;
    let fallback = |status: i64, rep: Vec<f64>| SolveOut { z: today.clone(), tried: Vec::new(), status, report: rep };
    let mut rep = vec![0.0; 15];
    rep[1] = n as f64;
    if n == 0 || n > MAX_KNOTS {
        rep[0] = S_BUDGET as f64;
        return fallback(S_BUDGET, rep);
    }
    // equalities (couplings, held by MU) and inequalities (the clearances' candidates)
    let mut eqs: Vec<Lin> = Vec::new();
    let mut eq_group: Vec<i64> = Vec::new();       // (-1: a join; else the sheet union it belongs to)
    let mut twin_groups: BTreeSet<i64> = BTreeSet::new();      // (the groups the twins' pass made)
    let mut extra_groups: BTreeSet<i64> = BTreeSet::new();     // (P3e's additions among them)
    let mut unions = 0i64;
    for j in joins {
        let (a, ea, b, eb) = (j[0] as usize, j[1] as usize, j[2] as usize, j[3] as usize);
        if a >= ms.len() || b >= ms.len() {
            continue;
        }
        let ia = base[a] + if ea == 0 { 0 } else { ms[a].xy.len() - 1 };
        let ib = base[b] + if eb == 0 { 0 } else { ms[b].xy.len() - 1 };
        eqs.push(Lin { terms: vec![(ia, 1.0), (ib, -1.0)], r: 0.0 });
        eq_group.push(-1);
    }
    // the sheet: a deck end inside another deck's edge of the same level, held to it knot by knot to the split
    let mut c1_of: BTreeSet<(usize, usize, usize)> = BTreeSet::new();     // (member, end, member it runs on into)
    let mut joined_end: BTreeSet<(usize, usize)> = BTreeSet::new();
    for j in joins {
        joined_end.insert((j[0] as usize, j[1] as usize));
        joined_end.insert((j[2] as usize, j[3] as usize));
        if j[4] != 0 {
            c1_of.insert((j[0] as usize, j[1] as usize, j[2] as usize));
            c1_of.insert((j[2] as usize, j[3] as usize, j[0] as usize));
        }
    }
    let mut sheet_pairs = 0usize;
    let mut twin_pairs = 0usize;
    let mut node_pairs = 0usize;
    let mut held: BTreeSet<usize> = BTreeSet::new();
    let mut pinned_end: Vec<[bool; 2]> = vec![[false; 2]; ms.len()];
    let mut work: u64 = 0;
    if surface.is_some() || robust {
        rep.extend([0.0, 0.0]); // required surface equalities; events ending before their nose
    }
    let mut through = ThroughJoins::new();
    if !pavement.is_empty() {
        for &[a, ea, b, eb, c1] in joins {
            let (x, y) = ((a as usize, ea as usize), (b as usize, eb as usize));
            if c1 != 0 && x.0 < ms.len() && y.0 < ms.len() && x.1 <= 1 && y.1 <= 1 {
                through.entry(x).or_default().insert(y);
                through.entry(y).or_default().insert(x);
            }
        }
    }
    for (a, ea, b, eb, reach) in pavement {
        let nose = f64::from_bits(reach);
        let main = fork_arm(ms, &through, (a, ea), nose);
        // The outside arm of a curved fork may be longer than the main.
        let branch = fork_arm(ms, &through, (b, eb), f64::INFINITY);
        let mut prev = None;
        let mut reached = false;
        'branch: for (m, e, _) in branch {
            for i in 0..ms[m].xy.len() {
                let k = if e == 0 { i } else { ms[m].xy.len() - 1 - i };
                let p = ms[m].xy[k];
                let mut best = (f64::INFINITY, 0, 0., 0.);
                for &(host, end, offset) in &main {
                    work += ms[host].xy.len() as u64;
                    if work > BUDGET.work {
                        rep[0] = S_BUDGET as f64;
                        return fallback(S_BUDGET, rep);
                    }
                    let (dist, t) = nearest(&ms[host].xy, &ms[host].s, p);
                    if dist < best.0 {
                        let length = *ms[host].s.last().unwrap();
                        best = (dist, host, t, offset + if end == 0 { t } else { length - t });
                    }
                }
                let (_, host, t, station) = best;
                if i > 0 && station <= 0.0 { break 'branch; }
                let last = main.last().unwrap();
                let available = last.2 + ms[last.0].s.last().unwrap();
                if available < nose && station >= available && past_end(&ms[last.0].xy, 1 - last.1, p) {
                    break 'branch;
                }
                let id = base[m] + k;
                let mut terms = Vec::new();
                if station > nose {
                    // The nose falls between two branch knots. Constrain the
                    // interpolated height at that station on both roads.
                    if let Some((prior, s)) = prev {
                        if s < nose {
                            let u = (nose - s) / (station - s);
                            terms.extend([(prior, 1.0 - u), (id, u)]);
                            let &(h, end, offset) = main.iter().rev().find(|row| row.2 <= nose).unwrap();
                            let arc = if end == 0 { nose - offset } else { ms[h].s.last().unwrap() - (nose - offset) };
                            lin_at(&base, &ms[h], h, arc, -1.0, &mut terms);
                        }
                    }
                } else {
                    terms.push((id, 1.0));
                    lin_at(&base, &ms[host], host, t, -1.0, &mut terms);
                }
                if !terms.is_empty() {
                    eqs.push(Lin { terms, r: 0.0 });
                    eq_group.push(-1);
                    rep[15] += 1.0;
                }
                if station >= nose { reached = true; break 'branch; }
                prev = Some((id, station));
            }
        }
        if !reached { rep[16] += 1.0; }
    }
    for b in 0..ms.len() {
        if !ms[b].deck || ms[b].xy.len() < 2 {
            continue;
        }
        for eb in 0..2 {
            let p = if eb == 0 { ms[b].xy[0] } else { ms[b].xy[ms[b].xy.len() - 1] };
            // (the host: a deck of the same level whose edge zone holds the end, not one running straight on from
            // it (a C1 joint); nearest, then the lowest index)
            // (P3d, 534: or of another OSM level whose surface is within NODE_DZ_M of the end today, as P2b's
            // deck_sheets takes a fork by its heights at the node: Fort Pitt's level 2 decks ending on a level 3
            // deck 0.36 m apart were pinned to it at tile time in sheet mode and solved apart here, a 0.4 m step
            // and 270 m2 of holes in the slab)
            let zb_end = if eb == 0 { ms[b].z[0] } else { ms[b].z[ms[b].z.len() - 1] };
            let mut host: Option<(usize, f64)> = None;
            for a in 0..ms.len() {
                if a == b || !ms[a].deck || ms[a].xy.len() < 2 || c1_of.contains(&(b, eb, a)) {
                    continue;
                }
                work += ms[a].xy.len() as u64;
                if work > BUDGET.work {
                    rep[0] = S_BUDGET as f64;
                    return fallback(S_BUDGET, rep);
                }
                let (dd, t) = nearest(&ms[a].xy, &ms[a].s, p);
                let r = ms[a].half + 1.0;
                if ms[a].level != ms[b].level && (z_at(&ms[a], t) - zb_end).abs() > NODE_DZ_M {
                    continue;
                }
                if dd <= r * r && host.map(|h| dd < h.1).unwrap_or(true) {
                    host = Some((a, dd));
                }
            }
            let Some((a, _)) = host else { continue };
            let nb = ms[b].xy.len();
            let mut got = Vec::new();
            let mut flyover = false;
            for step in 0..nb {
                let k = if eb == 0 { step } else { nb - 1 - step };
                let sb = if eb == 0 { ms[b].s[k] } else { ms[b].s[nb - 1] - ms[b].s[k] };
                if sb > sheet_reach {
                    break;
                }
                work += ms[a].xy.len() as u64;
                if work > BUDGET.work {
                    rep[0] = S_BUDGET as f64;
                    return fallback(S_BUDGET, rep);
                }
                let (dd, t) = nearest(&ms[a].xy, &ms[a].s, ms[b].xy[k]);
                let la = ms[a].s[ms[a].s.len() - 1];
                if step > 0 && (t <= 0.5 || t >= la - 0.5) {
                    break;      // (past the host's end: the union is over)
                }
                let gap = dd.sqrt() - ms[a].half - ms[b].half;
                let ub = {
                    let k0 = if k + 1 < nb { k } else { k - 1 };
                    unit([ms[b].xy[k0 + 1][0] - ms[b].xy[k0][0], ms[b].xy[k0 + 1][1] - ms[b].xy[k0][1]])
                };
                let ua = {
                    let pa = point_at(&ms[a].xy, &ms[a].s, (t - 1.0).max(0.0));
                    let qa = point_at(&ms[a].xy, &ms[a].s, (t + 1.0).min(ms[a].s[ms[a].s.len() - 1]));
                    unit([qa[0] - pa[0], qa[1] - pa[1]])
                };
                if gap > split_gap || dot(ua, ub).abs() < cos_sheet {
                    break;
                }
                if (ms[b].z[k] - z_at(&ms[a], t)).abs() > sheet_dz {
                    flyover = true;
                    break;
                }
                got.push((k, t));
            }
            if flyover || got.is_empty() {
                // (an end on another deck with nothing else holding it keeps its height)
                if !joined_end.contains(&(b, eb)) {
                    pinned_end[b][eb] = true;
                }
                continue;
            }
            for (k, t) in got {
                let mut terms = vec![(base[b] + k, 1.0)];
                lin_at(&base, &ms[a], a, t, -1.0, &mut terms);
                eqs.push(Lin { terms, r: 0.0 });
                eq_group.push(unions);
                held.insert(base[b] + k);
                sheet_pairs += 1;
            }
            unions += 1;
        }
    }
    // nodes (P3d): two deck ends of the chain not joined, each inside the other's carriageway (within the wider one's
    // half width) and within NODE_DZ_M in height today, are one junction: held at one height (Fort Pitt's level 2 and
    // level 3 forks, their nodes 3.9 m apart and 0.36 m today, were solved 0.41 m apart: a step through the junction
    // and 270 m2 of holes in its slab); a pair in conflict let go as a union is
    let mut ends_v: Vec<(usize, usize)> = Vec::new();
    for (mi, m) in ms.iter().enumerate() {
        if m.deck && m.xy.len() >= 2 {
            ends_v.push((mi, 0));
            ends_v.push((mi, 1));
        }
    }
    let end_k = |m: usize, e: usize| if e == 0 { 0 } else { ms[m].xy.len() - 1 };
    for i in 0..ends_v.len() {
        for j in i + 1..ends_v.len() {
            let ((ma, ea), (mb, eb)) = (ends_v[i], ends_v[j]);
            let joined = joins.iter().any(|q| {
                let (a, b) = ((q[0] as usize, q[1] as usize), (q[2] as usize, q[3] as usize));
                (a == (ma, ea) && b == (mb, eb)) || (a == (mb, eb) && b == (ma, ea))
            });
            if ma == mb || joined {
                continue;
            }
            let (ka, kb) = (end_k(ma, ea), end_k(mb, eb));
            let r = ms[ma].half.max(ms[mb].half);
            if d2(ms[ma].xy[ka], ms[mb].xy[kb]) > r * r || (ms[ma].z[ka] - ms[mb].z[kb]).abs() > NODE_DZ_M {
                continue;
            }
            eqs.push(Lin { terms: vec![(base[ma] + ka, 1.0), (base[mb] + kb, -1.0)], r: 0.0 });
            eq_group.push(unions);
            unions += 1;
            node_pairs += 1;
        }
    }
    rep[14] = node_pairs as f64;
    // twins (P3d): the later deck's knots held on the earlier's line where the two run side by side (TWIN_*); a knot
    // a sheet union holds already is left to it
    let boxes: Vec<[f64; 4]> = ms.iter().map(|m| {
        let mut bx = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for p in &m.xy {
            bx = [bx[0].min(p[0]), bx[1].min(p[1]), bx[2].max(p[0]), bx[3].max(p[1])];
        }
        bx
    }).collect();
    for b0 in 0..ms.len() {
        if !ms[b0].deck || ms[b0].xy.len() < 2 {
            continue;
        }
        for a0 in 0..b0 {
            if !ms[a0].deck || ms[a0].xy.len() < 2 {
                continue;
            }
            let pad = ms[a0].half + ms[b0].half + TWIN_GAP_M + TWIN_HOLD_M;
            let (ba, bb) = (boxes[a0], boxes[b0]);
            if bb[0] > ba[2] + pad || bb[2] < ba[0] - pad || bb[1] > ba[3] + pad || bb[3] < ba[1] - pad {
                continue;
            }
            // (each one's knots on the other's line: both sides held, no step where one's knots fall between)
            let (la0, lb0) = (ms[a0].s[ms[a0].s.len() - 1], ms[b0].s[ms[b0].s.len() - 1]);
            let mut got = Vec::new();
            let (mut run, mut run_base) = (0.0f64, 0.0f64);
            for (a, b) in [(a0, b0), (b0, a0)] {
            let la = ms[a].s[ms[a].s.len() - 1];
            let lb = ms[b].s[ms[b].s.len() - 1];
            let nb = ms[b].xy.len();
            let (mut side_run, mut side_base) = (0.0f64, 0.0f64);
            let mut ovl = vec![false; nb];
            // (P3e, 536: per knot of b: whether it lies beside or over a (cand), within TWIN_DZ_M of it today (seed),
            // and held by a sheet union already; a candidate run is held whole when one of its knots is a seed: a
            // ramp climbing away from its mainline while still beside it (I-95 Middle Branch: 3.4 m apart today
            // within 2.5 m, solved 0.56 m) stays at the mainline's height until it parts, as a twin does)
            let mut cand = vec![false; nb];
            let mut seed = vec![false; nb];
            let mut tk = vec![0.0f64; nb];
            for k in 0..nb {
                // (a knot a sheet union holds: P3d's rules skip it before any work; P3e's read it as a run's seed)
                let is_held = held.contains(&(base[b] + k));
                if is_held && !twin_flood && !twin_overlap {
                    continue;
                }
                work += ms[a].xy.len() as u64;
                if work > BUDGET.work {
                    rep[0] = S_BUDGET as f64;
                    return fallback(S_BUDGET, rep);
                }
                let (dd, t) = nearest(&ms[a].xy, &ms[a].s, ms[b].xy[k]);
                if (t <= TWIN_END_M && past_end(&ms[a].xy, 0, ms[b].xy[k]))
                    || (t >= la - TWIN_END_M && past_end(&ms[a].xy, 1, ms[b].xy[k])) {
                    continue;
                }
                let gap = dd.sqrt() - ms[a].half - ms[b].half;
                // (P3e: an overlap too, one over the other (OVERLAP_ALONG): one pavement, held at one height, never
                // trimmed into a step (P3d left overlaps past TWIN_TOUCH_M to roadmesh's trim))
                if gap > TWIN_GAP_M + TWIN_HOLD_M || (!twin_overlap && gap < -TWIN_TOUCH_M) {
                    continue;
                }
                let ub = {
                    let k0 = if k + 1 < nb { k } else { k - 1 };
                    unit([ms[b].xy[k0 + 1][0] - ms[b].xy[k0][0], ms[b].xy[k0 + 1][1] - ms[b].xy[k0][1]])
                };
                let ua = {
                    let pa = point_at(&ms[a].xy, &ms[a].s, (t - 1.0).max(0.0));
                    let qa = point_at(&ms[a].xy, &ms[a].s, (t + 1.0).min(la));
                    unit([qa[0] - pa[0], qa[1] - pa[1]])
                };
                if dot(ua, ub).abs() < TWIN_COS {
                    continue;
                }
                cand[k] = true;
                tk[k] = t;
                seed[k] = (ms[b].z[k] - z_at(&ms[a], t)).abs() <= TWIN_DZ_M;
                ovl[k] = gap < -TWIN_TOUCH_M;
                let piece = if k == 0 { ms[b].s[1] / 2.0 } else if k == nb - 1 { (lb - ms[b].s[k - 1]) / 2.0 }
                    else { (ms[b].s[k + 1] - ms[b].s[k - 1]) / 2.0 };
                if gap <= TWIN_GAP_M && (twin_flood || seed[k]) {
                    side_run += piece;
                }
                if gap <= TWIN_GAP_M && seed[k] && !ovl[k] && !is_held {
                    side_base += piece;      // (P3d's rule alone)
                }
            }
            // (a run of candidates holding a seed: held whole)
            let mut k = 0;
            while k < nb {
                if !cand[k] {
                    k += 1;
                    continue;
                }
                let k0 = k;
                while k < nb && cand[k] {
                    k += 1;
                }
                if (k0..k).any(|q| seed[q]) {
                    for q in k0..k {
                        if !held.contains(&(base[b] + q)) && (twin_flood || seed[q]) {
                            // (P3d's knots one group, P3e's additions (a run held whole, an overlap) another: a
                            // failure no group of its members answers lets go of every addition first)
                            got.push((a, b, q, tk[q], !seed[q] || ovl[q]));
                        }
                    }
                }
            }
            run = run.max(side_run);
            run_base = run_base.max(side_base);
            }
            let need = TWIN_MIN_M.min(TWIN_SHORT * la0.min(lb0));
            if got.is_empty() || run < need {
                continue;
            }
            let base_ok = run_base >= need;
            for (a, b, k, t, extra) in got {
                if !extra && !base_ok {
                    continue;
                }
                let g = unions + extra as i64;
                let mut terms = vec![(base[b] + k, 1.0)];
                lin_at(&base, &ms[a], a, t, -1.0, &mut terms);
                eqs.push(Lin { terms, r: 0.0 });
                eq_group.push(g);
                twin_groups.insert(g);
                if extra {
                    extra_groups.insert(g);
                }
                twin_pairs += 1;
            }
            unions += 2;
        }
    }
    if robust {
        // Separate approach walks may use the same ground line and then fork.
        // The terrain can publish just one height on that shared surface. Seed
        // the pairing on a common run, then follow it until the pavements part.
        for b in 0..ms.len() {
            if ms[b].deck { continue; }
            for a in 0..b {
                if ms[a].deck { continue; }
                let reach = ms[a].half + ms[b].half;
                let (ba, bb) = (boxes[a], boxes[b]);
                if bb[0] > ba[2] + reach || ba[0] > bb[2] + reach ||
                   bb[1] > ba[3] + reach || ba[1] > bb[3] + reach { continue; }
                work += ms[a].xy.len() as u64 * ms[b].xy.len() as u64;
                if work > BUDGET.work {
                    rep[0] = S_BUDGET as f64;
                    return fallback(S_BUDGET, rep);
                }
                let mut run = Vec::new();
                let mut shared = 0.;
                for (k, &p) in ms[b].xy.iter().enumerate() {
                    let (dd,t) = nearest(&ms[a].xy, &ms[a].s, p);
                    let h = if k > 0 { ms[b].s[k] - ms[b].s[k-1] } else { 0. };
                    let len = *ms[a].s.last().unwrap();
                    if dd < 0.05 * 0.05 && t > 0. && t < len { shared += h; }
                    run.push((k,t,dd));
                }
                if shared < 10. { continue; }
                for (k,t,dd) in run {
                    if dd >= reach * reach || t <= 0. || t >= *ms[a].s.last().unwrap() { continue; }
                    let mut terms = vec![(base[b] + k, 1.)];
                    lin_at(&base, &ms[a], a, t, -1., &mut terms);
                    eqs.push(Lin { terms, r: 0. });
                    eq_group.push(unions);
                }
                unions += 1;
            }
        }
    }
    rep[4] = sheet_pairs as f64;
    rep[13] = twin_pairs as f64;
    if work > BUDGET.work {
        rep[0] = S_BUDGET as f64;
        return fallback(S_BUDGET, rep);
    }
    // pins (MU on z, and on the next knot for z')
    let mut pin_terms: Vec<Lin> = Vec::new();
    let mut pin_one = |i: usize, z: f64| pin_terms.push(Lin { terms: vec![(i, 1.0)], r: z });
    for p in pins {
        let (m, e, kind) = (p[0] as usize, p[1] as usize, p[2]);
        if m >= ms.len() {
            continue;
        }
        let nm = ms[m].xy.len();
        let k = if e == 0 { 0 } else { nm - 1 };
        pin_one(base[m] + k, ms[m].z[k]);
        if kind == 2 && nm >= 2 {
            let k2 = if e == 0 { 1 } else { nm - 2 };
            pin_one(base[m] + k2, ms[m].z[k2]);
        }
    }
    for (m, pe) in pinned_end.iter().enumerate() {
        for e in 0..2 {
            if pe[e] {
                let k = if e == 0 { 0 } else { ms[m].xy.len() - 1 };
                pin_one(base[m] + k, ms[m].z[k]);
            }
        }
    }
    // inequalities: floors (never worse than today's), deck over ground, deck over deck
    let mut ineq: Vec<Lin> = Vec::new();
    let mut traffic_order = Vec::new();
    let mut ineq_kind: Vec<u8> = Vec::new();        // (0 a crossing floor, 1 deck over ground, 2 deck over deck)
    let abut_dist = |m: &Member, t: f64| -> f64 {
        let l = m.s[m.s.len() - 1];
        let mut d = f64::INFINITY;
        if m.dabut[0] {
            d = d.min(t);
        }
        if m.dabut[1] {
            d = d.min(l - t);
        }
        d
    };
    let ceiling = surface.filter(|_| !robust).map(|_| pinned_ceiling(ms, &base, &pin_terms, n, max_grade, grade_add, margin));
    let approaches: Vec<_> = ms.iter().enumerate().filter(|(_, m)| !m.deck).collect();
    for (fi, &(m, p, f)) in floors.iter().enumerate() {
        if m >= ms.len() || !ms[m].deck {
            continue;
        }
        work += ms[m].xy.len() as u64;
        let (_, t) = nearest(&ms[m].xy, &ms[m].s, p);
        if surface.is_none() && abut_dist(&ms[m], t) < at_grade {
            continue;
        }
        let r = if surface.is_some() { f + margin } else { f.min(z_at(&ms[m], t)) };
        let mut terms = Vec::new();
        lin_at(&base, &ms[m], m, t, 1.0, &mut terms);
        if let Some((ids, sources)) = ground_floors {
            let (id, clearance) = &sources[fi];
            let mut found = None;
            let mut best = 0.25 * 0.25;
            for ((mi, approach), parent) in approaches.iter().zip(ids) {
                if id.is_empty() || parent != id { continue; }
                let (d, s) = nearest(&approach.xy, &approach.s, p);
                if d < best && s > 0.25 && s < approach.s.last().unwrap() - 0.25 {
                    best = d;
                    found = Some((*mi, *approach, s));
                }
            }
            if let Some((mi, approach, s)) = found {
                lin_at(&base, approach, mi, s, -1., &mut terms);
                ineq.push(Lin { terms, r: clearance + margin + APPROACH_PAD_M });
                ineq_kind.push(5);
                continue;
            }
            if !id.is_empty() {
                // A separate ground road supplies an absolute lower surface.
                // Keep its order. Preserve observed passable headroom up to a
                // car plus the depth of the upper deck, even when the full
                // clearance observation competes with pins or parallel decks.
                let lower = f - clearance;
                let gap = (z_at(&ms[m], t) - lower).max(0.02).min(traffic_gap).min(*clearance);
                traffic_order.push(Lin { terms: terms.clone(), r: lower + gap });
            }
        }
        if let Some(bound) = &ceiling {
            let highest: f64 = terms.iter().map(|&(i, w)| w * bound[i]).sum();
            if highest + 1e-6 < f {
                rep[0] = S_CLEARANCE as f64;
                rep[7] = 1.0;
                rep[10] = m as f64;
                rep[12] = f - highest;
                rep.extend([t, f, highest]);
                return fallback(S_CLEARANCE, rep);
            }
        }
        ineq.push(Lin { terms, r });
        ineq_kind.push(0u8);
    }
    for (mi, m) in ms.iter().enumerate() {
        if !m.deck {
            continue;
        }
        for k in 0..m.xy.len() {
            if !m.g[k].is_finite() || abut_dist(m, m.s[k]) < at_grade {
                continue;
            }
            let r = (m.g[k] + deck_ground).min(m.z[k]);
            ineq.push(Lin { terms: vec![(base[mi] + k, 1.0)], r });
            ineq_kind.push(1u8);
        }
    }
    // deck over deck of another level where the lines cross (a grid of segments)
    let mut grid: BTreeMap<(i64, i64), Vec<(usize, usize)>> = BTreeMap::new();
    let mut entries: u64 = 0;
    // (P3d, 534: and an approach under a deck: the ground road the pins raise under one of the chain's decks, held
    // its clearance under it (a Fort Pitt road pinned 0.1-0.35 m up under a deck, one 0.02 m short of C))
    for (mi, m) in ms.iter().enumerate() {
        for k in 0..m.xy.len().saturating_sub(1) {
            let (a, b) = (m.xy[k], m.xy[k + 1]);
            let (x0, y0) = gkey([a[0].min(b[0]), a[1].min(b[1])], GRID_M);
            let (x1, y1) = gkey([a[0].max(b[0]), a[1].max(b[1])], GRID_M);
            for gx in x0..=x1 {
                for gy in y0..=y1 {
                    grid.entry((gx, gy)).or_default().push((mi, k));
                    entries += 1;
                }
            }
        }
    }
    if entries > BUDGET.grid {
        rep[0] = S_BUDGET as f64;
        return fallback(S_BUDGET, rep);
    }
    let mut seen = BTreeSet::new();
    let mut ncross = 0usize;
    let app_joined: BTreeSet<(usize, usize)> = joins.iter().filter_map(|q| {
        let (a, b) = (q[0] as usize, q[2] as usize);
        if a < ms.len() && b < ms.len() && ms[a].deck != ms[b].deck { Some((a.min(b), a.max(b))) } else { None }
    }).collect();
    for v in grid.values() {
        work += (v.len() as u64) * (v.len() as u64);
        if work > BUDGET.work {
            rep[0] = S_BUDGET as f64;
            return fallback(S_BUDGET, rep);
        }
        for i in 0..v.len() {
            for j in i + 1..v.len() {
                let ((ma, ka), (mb, kb)) = (v[i], v[j]);
                if ma == mb || (!ms[ma].deck && !ms[mb].deck) {
                    continue;
                }
                let (ma, ka, mb, kb) = if (ma, ka) < (mb, kb) { (ma, ka, mb, kb) } else { (mb, kb, ma, ka) };
                if !seen.insert((ma, ka, mb, kb)) {
                    continue;
                }
                let (a, b) = (&ms[ma], &ms[mb]);
                let Some((u, w)) = seg_cross(a.xy[ka], a.xy[ka + 1], b.xy[kb], b.xy[kb + 1]) else { continue };
                let ta = a.s[ka] + u * (a.s[ka + 1] - a.s[ka]);
                let tb = b.s[kb] + w * (b.s[kb + 1] - b.s[kb]);
                // (a deck and an approach: only the approach's own joint with that deck is at grade; any other deck
                // over it holds the clearance near its abutment too (never worse than today's: min(need, dz)), Codex
                // review of P3d)
                let near = abut_dist(a, ta) < at_grade || abut_dist(b, tb) < at_grade;
                if near && surface.is_none() && (a.deck && b.deck || app_joined.contains(&(ma.min(mb), ma.max(mb)))) {
                    continue;
                }
                let (za, zb) = (z_at(a, ta), z_at(b, tb));
                let interior = |m: &Member, t: f64| t.min(m.s.last().unwrap() - t) >= at_grade;
                let mapped_crossing = robust && a.deck && b.deck && a.level != b.level
                    && interior(a, ta) && interior(b, tb)
                    && dot(unit([a.xy[ka+1][0]-a.xy[ka][0], a.xy[ka+1][1]-a.xy[ka][1]]),
                           unit([b.xy[kb+1][0]-b.xy[kb][0], b.xy[kb+1][1]-b.xy[kb][1]])).abs() < cos_sheet;
                if (za - zb).abs() < STACK_MIN_M && !mapped_crossing {
                    continue;       // (level with each other today: a junction at grade, nothing to clear)
                }
                // When neither profile has established the crossing clearance, use mapped deck
                // order. An already separated pair retains its built order.
                let existing_clearance = if za >= zb { b.under } else { a.under };
                let a_over = if surface.is_some() && a.deck && b.deck && a.level != b.level
                    && (za - zb).abs() < existing_clearance {
                    a.level > b.level
                } else {
                    za >= zb
                };
                let (up, tu, lo, tl, dz) = if a_over {
                    (ma, ta, mb, tb, za - zb)
                } else {
                    (mb, tb, ma, ta, zb - za)
                };
                if !ms[up].deck {
                    continue;       // (a ground road over a deck: not this solve's to clear)
                }
                // (an approach under: its clearance and the checks' margin, so the pinned ground ends at C or lower)
                let clearance = ms[lo].under + if ms[lo].deck { 0.0 } else { APPROACH_PAD_M };
                let need = if surface.is_some() { clearance + margin } else { clearance.min(dz) };
                let mut terms = Vec::new();
                lin_at(&base, &ms[up], up, tu, 1.0, &mut terms);
                lin_at(&base, &ms[lo], lo, tl, -1.0, &mut terms);
                ineq.push(Lin { terms, r: need });
                ineq_kind.push(2u8);
                ncross += 1;
            }
        }
    }
    rep[5] = ncross as f64;
    if eqs.len() + ineq.len() + pin_terms.len() > BUDGET.pairs * 8 {
        rep[0] = S_BUDGET as f64;
        return fallback(S_BUDGET, rep);
    }
    // the pattern (every term that may enter: the order is fixed for every round)
    let mut adj: Vec<BTreeSet<usize>> = vec![BTreeSet::new(); n];
    let mut link = |t: &[(usize, f64)]| {
        if robust { return; }
        for &(i, _) in t {
            for &(j, _) in t {
                if i != j {
                    adj[i].insert(j);
                }
            }
        }
    };
    for (mi, m) in ms.iter().enumerate() {
        for k in 0..m.xy.len() {
            let i = base[mi] + k;
            for d in 1..=2usize {
                if k + d < m.xy.len() {
                    link(&[(i, 1.0), (i + d, 1.0)]);
                }
            }
        }
    }
    // (the curvature terms across C1 joins: the end knots' neighbours)
    let mut c1_terms: Vec<([usize; 3], f64, f64)> = Vec::new();
    for j in joins {
        if j[4] == 0 {
            continue;
        }
        let (a, ea, b, eb) = (j[0] as usize, j[1] as usize, j[2] as usize, j[3] as usize);
        if a >= ms.len() || b >= ms.len() || ms[a].xy.len() < 2 || ms[b].xy.len() < 2 {
            continue;
        }
        let (na, nb) = (ms[a].xy.len(), ms[b].xy.len());
        let ia_in = base[a] + if ea == 0 { 1 } else { na - 2 };
        let ia = base[a] + if ea == 0 { 0 } else { na - 1 };
        let ib_in = base[b] + if eb == 0 { 1 } else { nb - 2 };
        let ha = if ea == 0 { ms[a].s[1] } else { ms[a].s[na - 1] - ms[a].s[na - 2] };
        let hb = if eb == 0 { ms[b].s[1] } else { ms[b].s[nb - 1] - ms[b].s[nb - 2] };
        c1_terms.push(([ia_in, ia, ib_in], ha, hb));
        link(&[(ia_in, 1.0), (ia, 1.0), (ib_in, 1.0)]);
    }
    for l in eqs.iter().chain(ineq.iter()).chain(pin_terms.iter()) {
        link(&l.terms);
    }
    let adjv: Vec<Vec<usize>> = adj.into_iter().map(|s| s.into_iter().collect()).collect();
    let order = if robust { (0..n).collect() } else { rcm(&adjv) };
    let mut pos = vec![0usize; n];
    for (p, &v) in order.iter().enumerate() {
        pos[v] = p;
    }
    let cost = if robust { Some((0, 0)) } else { factor_cost(&adjv, &pos) };
    let Some((bytes, flops)) = cost else {
        rep[0] = S_BUDGET as f64;
        return fallback(S_BUDGET, rep);
    };
    // (each factorisation charged before it runs: over the work budget, today's heights)
    let mut spent: u64 = 0;
    if bytes > BUDGET.factor_bytes || flops > BUDGET.flops {
        rep[0] = S_BUDGET as f64;
        return fallback(S_BUDGET, rep);
    }
    // the fixed part of the system
    let mut rows0: Vec<BTreeMap<usize, f64>> = vec![BTreeMap::new(); n];
    let mut rhs0 = vec![0.0f64; n];
    let mut free_any = false;
    for (mi, m) in ms.iter().enumerate() {
        let nm = m.xy.len();
        let l = m.s[nm - 1];
        for k in 0..nm {
            let h = if nm == 1 { 1.0 } else if k == 0 { (m.s[1] - m.s[0]) / 2.0 } else if k == nm - 1 {
                (m.s[k] - m.s[k - 1]) / 2.0 } else { (m.s[k + 1] - m.s[k - 1]) / 2.0 };
            // (the weight toward today's: none near an abutment, full from d1; a ground knot toward the local
            // profile, a deck knot toward its height as built: the solve reshapes the joints, not whole viaducts)
            let w = {
                let ab = if m.deck { m.dabut } else { m.abut };
                let mut d = f64::INFINITY;
                if ab[0] {
                    d = d.min(m.s[k]);
                }
                if ab[1] {
                    d = d.min(l - m.s[k]);
                }
                if d <= d0 {
                    eps_w
                } else if d >= d1 {
                    w_g
                } else {
                    let f = (d - d0) / (d1 - d0);
                    (w_g * f * f).max(eps_w)
                }
            };
            if w < w_g {
                free_any = true;
            }
            add_term(&mut rows0, &mut rhs0, &pos, &[(base[mi] + k, 1.0)], m.z[k], w * h.max(1e-3));
            if k >= 1 && k + 1 < nm {
                let (ha, hb) = (m.s[k] - m.s[k - 1], m.s[k + 1] - m.s[k]);
                if ha > 1e-6 && hb > 1e-6 {
                    let c = 2.0 / (ha + hb);
                    let t = [(base[mi] + k - 1, c / ha), (base[mi] + k, -c / ha - c / hb), (base[mi] + k + 1, c / hb)];
                    add_term(&mut rows0, &mut rhs0, &pos, &t, 0.0, lambda * (ha + hb) / 2.0);
                }
            }
        }
    }
    for &(c, ha, hb) in &c1_terms {
        let (ia_in, ia, ib_in) = (c[0], c[1], c[2]);
        let (ha, hb) = (ha.max(1e-3), hb.max(1e-3));
        let cc = 2.0 / (ha + hb);
        let t = [(ia_in, cc / ha), (ia, -cc / ha - cc / hb), (ib_in, cc / hb)];
        add_term(&mut rows0, &mut rhs0, &pos, &t, 0.0, lambda * (ha + hb) / 2.0);
    }
    if robust {
        return profile_qp::solve(ms, &base, rows0, rhs0, &eqs, &eq_group, &c1_terms,
                                 &pin_terms, &ineq, &ineq_kind, &traffic_order, traffic_gap, max_grade, grade_add, rep);
    }
    for l in pin_terms.iter() {
        add_term(&mut rows0, &mut rhs0, &pos, &l.terms, l.r, mu);
    }
    if !free_any && eqs.is_empty() {
        rep[0] = S_PINNED as f64;
        return fallback(S_PINNED, rep);
    }
    let member_of = |i: usize| base.partition_point(|&b| b <= i) - 1;
    let mut dropped: BTreeSet<i64> = BTreeSet::new();
    let mut total_rounds = 0usize;
    // (P3e: a failure no group of its members answers lets go of every addition (extra_groups) at once, if any is left:
    // P3d's couplings alone then)
    let drop_extras = |dropped: &mut BTreeSet<i64>| -> bool {
        let left: Vec<i64> = extra_groups.iter().copied().filter(|g| !dropped.contains(g)).collect();
        for g in &left {
            dropped.insert(*g);
        }
        !left.is_empty()
    };
    // (P3e: a failure no group of its members answers: the last-made group holding a member joined to one of them
    // (one join on), with those members)
    let near_group = |dropped: &BTreeSet<i64>, mm: &BTreeSet<usize>| -> Option<(i64, BTreeSet<usize>)> {
        let mut nb = mm.clone();
        for j in joins {
            let (x, y) = (j[0] as usize, j[2] as usize);
            if mm.contains(&x) {
                nb.insert(y);
            }
            if mm.contains(&y) {
                nb.insert(x);
            }
        }
        for (i, l) in eqs.iter().enumerate().rev() {
            let g = eq_group[i];
            if g >= 0 && !dropped.contains(&g) && l.terms.iter().any(|t| nb.contains(&member_of(t.0))) {
                return Some((g, nb));
            }
        }
        None
    };
    // ([21]: every twin group holding one of the members, let go with the one chosen)
    let drop_with = |dropped: &mut BTreeSet<i64>, mm: &BTreeSet<usize>| {
        if !drop_all {
            return;
        }
        for (i, l) in eqs.iter().enumerate() {
            let g = eq_group[i];
            if g >= 0 && twin_groups.contains(&g) && l.terms.iter().any(|t| mm.contains(&member_of(t.0))) {
                dropped.insert(g);
            }
        }
    };
    for attempt in 0..attempts {
        // (P3e: from half the solves on, P3d's couplings alone: the additions never cost the chain its solve)
        if attempt == attempts / 2 && drop_extras(&mut dropped) {
            rep[9] = dropped.len() as f64;
        }
        // (the couplings per attempt: a sheet union in conflict is let go)
        let mut rows1 = rows0.clone();
        let mut rhs1 = rhs0.clone();
        for (l, g) in eqs.iter().zip(eq_group.iter()) {
            if !dropped.contains(g) {
                add_term(&mut rows1, &mut rhs1, &pos, &l.terms, l.r, mu);
            }
        }
        // the active set
        let mut active: BTreeSet<usize> = BTreeSet::new();
        let mut zsol: Option<Vec<f64>> = None;
        for _ in 0..=rounds_max {
            total_rounds += 1;
            spent = spent.saturating_add(flops);
            rep[8] = spent as f64;
            if spent > BUDGET.flops {
                rep[0] = S_BUDGET as f64;
                rep[2] = total_rounds as f64;
                return fallback(S_BUDGET, rep);
            }
            let mut rows = rows1.clone();
            let mut rhs = rhs1.clone();
            for &i in &active {
                add_term(&mut rows, &mut rhs, &pos, &ineq[i].terms, ineq[i].r, mu);
            }
            let Some(y) = ldl_solve(n, &rows, &rhs) else {
                rep[0] = S_SOLVER as f64;
                rep[2] = total_rounds as f64;
                return fallback(S_SOLVER, rep);
            };
            let z: Vec<f64> = (0..n).map(|v| y[pos[v]]).collect();
            let mut changed = false;
            for (i, l) in ineq.iter().enumerate() {
                let v = lin_val(&z, l);
                if active.contains(&i) {
                    if v > release_tol {
                        active.remove(&i);
                        changed = true;
                    }
                } else if v < -active_tol {
                    active.insert(i);
                    changed = true;
                }
            }
            zsol = Some(z);
            if !changed {
                break;
            }
        }
        rep[2] = total_rounds as f64;
        rep[3] = active.len() as f64;
        let z = zsol.unwrap();
        // the worst coupling and the worst clearance
        let (mut resid, mut worst) = (0.0f64, usize::MAX);
        for (i, l) in eqs.iter().enumerate() {
            if dropped.contains(&eq_group[i]) {
                continue;
            }
            let v = lin_val(&z, l).abs();
            if v > resid {
                resid = v;
                worst = i;
            }
        }
        rep[6] = resid;
        let (mut unmet, mut wv, mut wi) = (0usize, 0.0f64, usize::MAX);
        for (i, l) in ineq.iter().enumerate() {
            let v = lin_val(&z, l);
            if v < -margin {
                unmet += 1;
                if v < wv {
                    wv = v;
                    wi = i;
                }
            }
        }
        rep[7] = unmet as f64;
        if resid > margin || unmet > 0 {
            // (a sheet union in conflict with a clearance or another coupling: let go, solved again; counted)
            let mut cand: Option<i64> = None;
            if resid > margin && eq_group[worst] >= 0 {
                cand = Some(eq_group[worst]);
            } else if unmet > 0 {
                let mms: BTreeSet<usize> = ineq[wi].terms.iter().map(|t| member_of(t.0)).collect();
                // (the last made first: a twin pair before a sheet union, P3d)
                for (i, l) in eqs.iter().enumerate().rev() {
                    let g = eq_group[i];
                    if g >= 0 && !dropped.contains(&g) && l.terms.iter().any(|t| mms.contains(&member_of(t.0))) {
                        cand = Some(g);
                        break;
                    }
                }
            }
            if let Some(g) = cand {
                dropped.insert(g);
                let mm: BTreeSet<usize> = if unmet > 0 && !(resid > margin && eq_group[worst] >= 0) {
                    ineq[wi].terms.iter().map(|t| member_of(t.0)).collect()
                } else {
                    eqs[worst].terms.iter().map(|t| member_of(t.0)).collect()
                };
                drop_with(&mut dropped, &mm);
                rep[9] = dropped.len() as f64;
                continue;
            }
            if drop_extras(&mut dropped) {
                rep[9] = dropped.len() as f64;
                continue;
            }
            if resid > margin {
                rep[10] = member_of(eqs[worst].terms[0].0) as f64;
                rep[0] = S_INFEASIBLE as f64;
                return fallback(S_INFEASIBLE, rep);
            }
            rep[10] = member_of(ineq[wi].terms[0].0) as f64;
            rep[11] = ineq_kind[wi] as f64;
            rep[12] = -wv;
            rep[0] = S_CLEARANCE as f64;
            let mut out = fallback(S_CLEARANCE, rep);
            out.tried = z.iter().map(|v| (v * 1000.0).round() / 1000.0).collect();
            return out;
        }
        // (the pins held and no slope break at a C1 joint, on the solve as it is published)
        let mut drift = 0.0f64;
        for l in &pin_terms {
            drift = drift.max(lin_val(&z, l).abs());
        }
        // (a C1 joint bends no more than SLOPE_TOL + 1.5 x what its two members do inside (their largest slope change
        // between two pieces))
        let bend: Vec<f64> = ms.iter().enumerate().map(|(mi, m)| {
            let mut b = 0.0f64;
            for k in 1..m.xy.len().saturating_sub(1) {
                let (ha, hb) = ((m.s[k] - m.s[k - 1]).max(1e-3), (m.s[k + 1] - m.s[k]).max(1e-3));
                let i = base[mi] + k;
                b = b.max(((z[i + 1] - z[i]) / hb - (z[i] - z[i - 1]) / ha).abs());
            }
            b
        }).collect();
        // (P3c, earthfast 523: and KINK_STEEP x the steeper member's steepest grade today: a steep road is a slow one,
        // the ride of a slope break goes with v^2 (Tromso's 13-23 % streets fell back over a 0.35 % excess))
        let steep: Vec<f64> = ms.iter().map(|m| {
            let mut g = 0.0f64;
            for k in 0..m.xy.len().saturating_sub(1) {
                let h = m.s[k + 1] - m.s[k];
                if h >= 0.5 {
                    g = g.max((m.z[k + 1] - m.z[k]).abs() / h);
                }
            }
            g
        }).collect();
        let mut kink = 0.0f64;
        let mut kink_at: Option<(usize, usize)> = None;
        for &(c, ha, hb) in &c1_terms {
            let (sa, sb) = ((z[c[1]] - z[c[0]]) / ha.max(1e-3), (z[c[2]] - z[c[1]]) / hb.max(1e-3));
            let (ma, mb) = (member_of(c[0]), member_of(c[2]));
            let lim = SLOPE_TOL + 1.5 * bend[ma].max(bend[mb]) + KINK_STEEP * steep[ma].max(steep[mb]);
            let k = (sa - sb).abs() - lim;
            if k > kink {
                kink = k;
                kink_at = Some((ma, mb));
            }
        }
        if drift <= margin && kink > 0.0 {
            // (P3e, 536: a slope break at a C1 joint lets go of a coupling group holding one of its two members
            // first, the last made first, as a grade failure does; solved again; counted)
            let (ma, mb) = kink_at.unwrap_or((usize::MAX, usize::MAX));
            let mut cand: Option<i64> = None;
            for (i, l) in eqs.iter().enumerate().rev() {
                let g = eq_group[i];
                if g >= 0 && !dropped.contains(&g) && l.terms.iter().any(|t| {
                    let m = member_of(t.0);
                    m == ma || m == mb
                }) {
                    cand = Some(g);
                    break;
                }
            }
            if let Some(g) = cand {
                dropped.insert(g);
                drop_with(&mut dropped, &[ma, mb].into_iter().collect());
                rep[9] = dropped.len() as f64;
                continue;
            }
        }
        if drift <= margin && kink > 0.0 {
            let (ma, mb) = kink_at.unwrap_or((usize::MAX, usize::MAX));
            if let Some((g, nb)) = near_group(&dropped, &[ma, mb].into_iter().collect()) {
                dropped.insert(g);
                drop_with(&mut dropped, &nb);
                rep[9] = dropped.len() as f64;
                continue;
            }
        }
        if (drift > margin || kink > 0.0) && drop_extras(&mut dropped) {
            rep[9] = dropped.len() as f64;
            continue;
        }
        if drift > margin || kink > 0.0 {
            rep[12] = drift.max(kink);
            rep[0] = S_INFEASIBLE as f64;
            return fallback(S_INFEASIBLE, rep);
        }
        let mut regrade = false;
        for (mi, m) in ms.iter().enumerate() {
            let nm = m.xy.len();
            let mut today_max: f64 = 0.0;
            let mut new_max: f64 = 0.0;
            for k in 0..nm.saturating_sub(1) {
                let h = m.s[k + 1] - m.s[k];
                if h < 0.5 {
                    continue;
                }
                today_max = today_max.max((m.z[k + 1] - m.z[k]).abs() / h);
                new_max = new_max.max((z[base[mi] + k + 1] - z[base[mi] + k]).abs() / h);
            }
            if new_max > max_grade.max(today_max) + grade_add {
                // (P3d: a twin pair or sheet union holding that member let go first, the last made first (the twins
                // after the unions), solved again; counted)
                let mut cand: Option<i64> = None;
                for (i, l) in eqs.iter().enumerate().rev() {
                    let g = eq_group[i];
                    if g >= 0 && !dropped.contains(&g) && l.terms.iter().any(|t| member_of(t.0) == mi) {
                        cand = Some(g);
                        break;
                    }
                }
                if let Some(g) = cand {
                    dropped.insert(g);
                    drop_with(&mut dropped, &[mi].into_iter().collect());
                    rep[9] = dropped.len() as f64;
                    regrade = true;
                    break;
                }
                if let Some((g, nb)) = near_group(&dropped, &[mi].into_iter().collect()) {
                    dropped.insert(g);
                    drop_with(&mut dropped, &nb);
                    rep[9] = dropped.len() as f64;
                    regrade = true;
                    break;
                }
                if drop_extras(&mut dropped) {
                    rep[9] = dropped.len() as f64;
                    regrade = true;
                    break;
                }
                rep[10] = mi as f64;
                rep[0] = S_GRADE as f64;
                let mut out = fallback(S_GRADE, rep);
                out.tried = z.iter().map(|v| (v * 1000.0).round() / 1000.0).collect();
                return out;
            }
        }
        if regrade {
            continue;
        }
        let out: Vec<f64> = z.iter().map(|v| (v * 1000.0).round() / 1000.0).collect();
        rep[0] = S_SOLVED as f64;
        return SolveOut { z: out, tried: Vec::new(), status: S_SOLVED, report: rep };
    }
    rep[0] = S_INFEASIBLE as f64;
    fallback(S_INFEASIBLE, rep)
}

/// One joint profile solve of a chain's members (module doc). Knots: xy (k, 2) flat by member (off), today's z (deck as
/// built, ground the local profile), the ground under each (NaN: unknown); per member: deck flag, half width, level,
/// the clearance a deck over it needs, abutment flags (bits: 1 start, 2 end; an approach's ends at a deck, a deck's
/// ends that are abutments); joins flat (ma, ea, mb, eb, c1); pins flat (m, e, kind); floors flat (m, x, y, floor);
/// params (SOLVE_PARAMS). Returns (z per knot, report [status, knots, rounds, active, sheet pairs, crossings, max
/// coupling residual, unmet, flops]).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
#[pyo3(signature = (kxy, koff, kz, kg, deck, half, level, under, abut, joins, pins, floors, params, surface=None, robust=false, ground_floors=None, traffic_gap=4.5))]
pub fn profile_solve<'py>(py: Python<'py>, kxy: PyReadonlyArray2<'py, f64>, koff: Vec<i64>,
                          kz: PyReadonlyArray1<'py, f64>, kg: PyReadonlyArray1<'py, f64>, deck: Vec<i64>,
                          half: Vec<f64>, level: Vec<f64>, under: Vec<f64>, abut: Vec<i64>, joins: Vec<i64>,
                          pins: Vec<i64>, floors: Vec<f64>, params: Vec<f64>, surface: Option<Vec<SurfaceFork>>, robust: bool, ground_floors: Option<GroundFloors>, traffic_gap: f64)
                          -> PyResult<(Vec<f64>, Vec<f64>, Vec<f64>)> {
    let (kxy, kz, kg) = (kxy.as_array(), kz.as_array(), kg.as_array());
    let nm = koff.len().saturating_sub(1);
    let n = kxy.nrows();
    if kxy.ncols() != 2 || !check_off(&koff, n) || kz.len() != n || kg.len() != n || deck.len() != nm
        || half.len() != nm || level.len() != nm || under.len() != nm || abut.len() != nm || joins.len() % 5 != 0
        || pins.len() % 3 != 0 || floors.len() % 4 != 0 || (params.len() != SOLVE_PARAMS && params.len() != SOLVE_PARAMS + 4)
        || (0..nm).any(|i| koff[i + 1] - koff[i] < 2) {
        return Err(PyValueError::new_err("profile_solve: bad shapes"));
    }
    if !traffic_gap.is_finite() || traffic_gap < 0.02 || ground_floors.as_ref().is_some_and(|(ids, floors_meta)| !robust || ids.len() != deck.iter().filter(|&&d| d == 0).count()
        || floors_meta.len() != floors.len()/4 || floors.chunks_exact(4).any(|c| c[0] < 0.)) {
        return Err(PyValueError::new_err("profile_solve: invalid ground floors"));
    }
    if surface.as_deref().unwrap_or_default().iter().any(|&(t, a, b, x, y, reach)| {
        t < -1 || t >= nm as i64 || a >= nm || b >= nm || a == b || deck[a] == 0 || deck[b] == 0
            || !x.is_finite() || !y.is_finite() || !reach.is_finite() || reach < 0.0
    }) {
        return Err(PyValueError::new_err("profile_solve: invalid surface fork"));
    }
    let mut ms = Vec::with_capacity(nm);
    for i in 0..nm {
        let (a, b) = (koff[i] as usize, koff[i + 1] as usize);
        let xy: Vec<[f64; 2]> = (a..b).map(|k| [kxy[[k, 0]], kxy[[k, 1]]]).collect();
        let s = arcs(&xy);
        let ab = [abut[i] & 1 != 0, abut[i] & 2 != 0];
        ms.push(Member { xy, s, z: (a..b).map(|k| kz[k]).collect(), g: (a..b).map(|k| kg[k]).collect(),
                         deck: deck[i] != 0, half: half[i], level: level[i], under: under[i],
                         abut: if deck[i] != 0 { [false; 2] } else { ab }, dabut: if deck[i] != 0 { ab } else { [false; 2] } });
    }
    let jn: Vec<[i64; 5]> = joins.chunks(5).map(|c| [c[0], c[1], c[2], c[3], c[4]]).collect();
    let pn: Vec<[i64; 3]> = pins.chunks(3).map(|c| [c[0], c[1], c[2]]).collect();
    let fl: Vec<(usize, [f64; 2], f64)> = floors.chunks(4).filter(|c| c[0] >= 0.0)
        .map(|c| (c[0] as usize, [c[1], c[2]], c[3])).collect();
    let out = py.detach(|| {
        let out = solve_core(&ms, &jn, &pn, &fl, &params, surface.as_deref(), robust, ground_floors.as_ref(), traffic_gap);
        // (P3e: the additions ([18], [19]) over the budget: solved again without them, P3d's couplings alone, never a
        // fallback the baseline would not take; Codex P3e)
        if out.status == S_BUDGET && params.len() > SOLVE_PARAMS && (params[18] != 0.0 || params[19] != 0.0) {
            let mut p2 = params.clone();
            p2[18] = 0.0;
            p2[19] = 0.0;
            return solve_core(&ms, &jn, &pn, &fl, &p2, surface.as_deref(), robust, ground_floors.as_ref(), traffic_gap);
        }
        out
    });
    Ok((out.z, out.report, out.tried))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn prm() -> Vec<f64> {
        vec![160000.0, 1.0, 10.0, 80.0, 1e-6, 1e8, 1.2, 3.0, 0.766, 300.0, 12.0, 0.5, 0.06, 0.0375, 0.02, 8.0, 0.001, 1e-9]
    }

    fn line(x0: f64, x1: f64, step: f64, z: impl Fn(f64) -> f64, deck: bool, abut: [bool; 2]) -> Member {
        let (xy, s) = resample(&[[x0, 0.0], [x1, 0.0]], &[0.0, (x1 - x0).abs()], step);
        let zz = xy.iter().map(|p| z(p[0])).collect();
        Member { g: vec![f64::NAN; xy.len()], xy, s, z: zz, deck, half: 5.0, level: 1.0, under: 5.0,
                 abut: if deck { [false; 2] } else { abut }, dabut: if deck { abut } else { [false; 2] } }
    }

    #[test]
    fn kink_smoothed_and_anchors_kept() {
        // a deck rising 4 % between two flat approaches: today's joints are kinks; the solve rounds them and keeps the
        // anchors' heights
        let a0 = line(0.0, -200.0, 2.0, |_| 0.0, false, [true, false]);
        let dk = line(0.0, 100.0, 2.0, |x| 0.04 * x, true, [true, true]);
        let a1 = line(100.0, 300.0, 2.0, |_| 4.0, false, [true, false]);
        let ms = vec![dk, a0, a1];
        let joins = vec![[1, 0, 0, 0, 1], [2, 0, 0, 1, 1]];
        let pins = vec![[1, 1, 2], [2, 1, 2]];
        let out = solve_core(&ms, &joins, &pins, &[], &prm(), None, false, None, 4.5);
        assert_eq!(out.status, S_SOLVED);
        let n0 = ms[0].xy.len();
        let n1 = ms[1].xy.len();
        assert!((out.z[n0 + n1 - 1] - 0.0).abs() < 0.01);
        // the joint is no longer a kink: its second difference is far below today's 0.04 / 2 m
        let z = &out.z;
        let dd = (z[1] - z[0]) - (z[0] - z[n0 + 1]);
        assert!(dd.abs() < 0.02, "{}", dd);
        // the same twice
        let again = solve_core(&ms, &joins, &pins, &[], &prm(), None, false, None, 4.5);
        assert_eq!(out.z, again.z);
    }

    #[test]
    fn floor_held() {
        let a0 = line(0.0, -150.0, 2.0, |_| 0.0, false, [true, false]);
        let dk = line(0.0, 100.0, 2.0, |x| 6.0 * (1.0 - ((x - 50.0) / 50.0).powi(2)), true, [true, true]);
        let a1 = line(100.0, 250.0, 2.0, |_| 0.0, false, [true, false]);
        let ms = vec![dk, a0, a1];
        let joins = vec![[1, 0, 0, 0, 1], [2, 0, 0, 1, 1]];
        let pins = vec![[1, 1, 2], [2, 1, 2]];
        let floors = vec![(0usize, [50.0, 0.0], 5.5)];
        let out = solve_core(&ms, &joins, &pins, &floors, &prm(), None, false, None, 4.5);
        assert_eq!(out.status, S_SOLVED);
        assert!(out.z[25] >= 5.5 - 0.02, "{}", out.z[25]);
    }

    #[test]
    fn twins_held_level() {
        // (P3d) two parallel decks 1 m apart edge to edge, 0.3 m apart today and rising differently: one height
        let mut a = line(0.0, 100.0, 2.0, |x| 0.02 * x, true, [true, true]);
        let mut b = line(0.0, 100.0, 2.0, |x| 0.1 + 0.025 * x, true, [true, true]);
        for p in b.xy.iter_mut() {
            p[1] = 11.0;
        }
        a.half = 5.0;
        b.half = 5.0;
        let ms = vec![a, b];
        let pins = vec![[0, 0, 1], [0, 1, 1]];
        let out = solve_core(&ms, &[], &pins, &[], &prm(), None, false, None, 4.5);
        assert_eq!(out.status, S_SOLVED);
        assert!(out.report[13] > 0.0);
        let n = ms[0].xy.len();
        for k in 5..n - 5 {
            assert!((out.z[k] - out.z[n + k]).abs() < 0.02, "{} {} {}", k, out.z[k], out.z[n + k]);
        }
    }

    #[test]
    fn members_walk() {
        let decks = vec![vec![[0.0, 0.0], [100.0, 0.0]]];
        let ground = vec![vec![[100.0, 0.0], [150.0, 0.0]], vec![[150.0, 0.0], [400.0, 0.0]],
                          vec![[150.0, 0.0], [150.0, 100.0]], vec![[-300.0, 0.0], [0.0, 0.0]]];
        let out = members_core(&decks, &[5.0], &[200.0], &ground, &[1.0, 10.0, 0.766, 2.0, 100000.0], None);
        assert_eq!(out.approaches.len(), 2);
        for a in &out.approaches {
            let s = arcs(&a.xy);
            assert!((s[s.len() - 1] - 200.0).abs() < 1e-6);
            assert_eq!(a.reach, 0);
        }
        let b = &out.approaches[1];
        assert!((b.xy[b.xy.len() - 1][0] - 300.0).abs() < 1e-6 && b.xy[b.xy.len() - 1][1].abs() < 1e-9);
    }
}
