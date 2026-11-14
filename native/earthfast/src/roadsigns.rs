//! Ordinary road signs from what the road data states (task 017): render-only signs in the sign tiles, one shared mesh
//! on one opaque atlas (server/roadsigns.py is the glue, signstream.py sends them). A wrong sign is worse than a missing
//! one, so a rule stands only where the facts imply the sign, and a doubtful case gets none.
//!
//! Where (road_sign_place), per direction of travel on the road pieces between junctions:
//! - NO_ENTRY: where a one-way road ends and traffic could turn into it the wrong way: facing that traffic, just inside
//!   the road. Not on a road that carries straight on as one-way (a junction along it), nor where a divided road splits
//!   (the carriageway leaving is nearly opposite: there the layout says it).
//! - DEAD_END: facing traffic that turns into a two-way minor road leading only to its own end within DEAD_END_MAX_M.
//!   A track at the end (the road goes on, unpaved) is the caller's `open_ends`: no sign.
//! - CROSSING: at a marked crossing without signals (the caller's `crossings`, points on the road's centre line), each
//!   way of travel, at the crossing's near side.
//! - CURVE, DOUBLE: a sharp bend (or two opposite ones close together) on a country road of tertiary rank or better, far from
//!   any junction: the warning `warn_m` before it, in the lane's direction of travel, left or right by the turn's sign.
//! - ROUNDABOUT: on every road entering a ring (the caller's geometric rings): the mandatory sign at the entry, and in the
//!   country the warning `warn_m` before it.
//! - SIGNALS: on a country road of secondary rank or better, `warn_m` before the signals the map has on it.
//! - MOTORWAY, MOTORWAY_END: where a motorway carriageway begins (no motorway before it, traffic joining) and `motorway_end`
//!   m before it ends (traffic leaves onto a lesser road).
//! - LIVING, LIVING_END: entering and leaving a living street from a road that is not one.
//! - ONEWAY: at the start of a town one-way street where traffic can join and none comes along it as a one-way.
//! Signs that would stand within `join` m of each other, facing the same way, share one pole (the country's `order` of groups
//! from the top down, at most `max_on_pole`; a rule whose group is not in the order stands alone); signs near a speed-limit
//! sign of speedsigns.rs (the caller's `avoid`) give way to it.
//! A rule's signs are thinned per direction; signs stand beside the carriageway on the driving side, clear of every
//! road's pavement, never on a bridge or in a tunnel.
//!
//! How (road_sign_mesh): the sign's outline (outline(), the atlas cell's: tools/build_sign_atlas.py) cut as geometry, a
//! metal back, a round pole.

use std::collections::HashMap;

use numpy::ndarray::{ArrayView1, ArrayView2};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

use crate::speedsigns::{clear, splitmix, Mesh};

pub const NO_ENTRY: u8 = 0;
pub const DEAD_END: u8 = 1;
pub const CROSSING: u8 = 2;
pub const CURVE_RIGHT: u8 = 3;
pub const CURVE_LEFT: u8 = 4;
pub const DOUBLE_RIGHT: u8 = 5;
pub const DOUBLE_LEFT: u8 = 6;
pub const ROUNDABOUT_WARN: u8 = 7;
pub const ROUNDABOUT: u8 = 8;
pub const SIGNALS: u8 = 9;
pub const MOTORWAY: u8 = 10;
pub const MOTORWAY_END: u8 = 11;
pub const LIVING: u8 = 12;
pub const LIVING_END: u8 = 13;
pub const ONEWAY: u8 = 14;
pub const NARROW: u8 = 15;
pub const CHEVRON_RIGHT: u8 = 16;
pub const CHEVRON_LEFT: u8 = 17;
pub const SIDE_RIGHT: u8 = 18;
pub const SIDE_LEFT: u8 = 19;
pub const GIVE_WAY: u8 = 20;
pub const STOP: u8 = 21;
pub const TUNNEL: u8 = 22;
pub const PED_ZONE: u8 = 23;
pub const BICYCLE: u8 = 24;
pub const BUS_STOP: u8 = 25;
pub const PARKING: u8 = 26;
pub const RULES: usize = 27;

/// The kinds of Input::pois (x, y, kind, a, b): give way and stop nodes (a: +1 along the way's geometry, -1 against, 0 not
/// stated), bus stops, car park entrances, tunnel portals (a, b: the travel into the tunnel).
pub const POI_GIVE_WAY: i64 = 0;
pub const POI_STOP: i64 = 1;
pub const POI_BUS_STOP: i64 = 2;
pub const POI_PARKING: i64 = 3;
pub const POI_TUNNEL: i64 = 4;

mod more;

/// What the caller says about a road beside its rank (meta column 5).
pub const FLAG_RING: i64 = 1;
pub const FLAG_LIVING: i64 = 2;
pub const FLAG_FOOT: i64 = 4;       // a pedestrian zone
pub const FLAG_CYCLE: i64 = 8;      // a cycle path

/// The thresholds (Input::params, sign_systems.json "params"): [warn m, curve turn deg, curve window m, curve min rank,
/// double curve gap m, motorway end m, living street min m, min m before the hazard, chevron turn deg, chevron window m,
/// narrowing: the narrower road's share of the wider one's half width at most].
pub const N_PARAMS: usize = 11;

/// The pole settings (Input::pole): per rule its place in the country's order of groups from the top of a pole (a negative
/// number: the rule stands alone), then the most signs on a pole and the distance within which signs share one.
pub const N_POLE: usize = RULES + 2;

const PRIMARY: i64 = 5;         // ranks (speedsigns.RANK)
const TRUNK: i64 = 6;
const TERTIARY: i64 = 3;
const SECONDARY: i64 = 4;
const MOTORWAY_RANK: i64 = 7;
const BEND_STEP_M: f64 = 5.0;            // the heading is sampled this often along a piece
const BEND_EDGE_M: f64 = 15.0;           // a bend ending this near a piece's end is a junction's corner, not a curve
const MIN_PIECE_M: f64 = 80.0;           // a piece shorter than this holds no curve sign
const SIGN_AFTER_M: f64 = 15.0;          // a sign stands at least this far past its piece's start
const ROUNDABOUT_IN_M: f64 = 8.0;        // the mandatory sign this far before the ring
const MOTORWAY_IN_M: f64 = 20.0;
const LIVING_IN_M: f64 = 6.0;
const ONEWAY_IN_M: f64 = 8.0;
const AVOID_M: f64 = 7.0;               // a spot this near a speed-limit sign facing the same way is dropped
const PANEL_GAP_M: f64 = 5.0;            // no two signs facing the same way closer than this, whatever their rules

const NO_ENTRY_IN_M: f64 = 6.0;         // m inside the road's end
const DEAD_END_IN_M: f64 = 10.0;        // m past the junction
const DEAD_END_MIN_M: f64 = 40.0;       // a stub shorter than this is a driveway to the eye
const DEAD_END_MAX_M: f64 = 400.0;      // (well within the glue's margin: a road's end there is the map's, not the box's)
const DEAD_END_HOPS: usize = 4;
const OPEN_END_M: f64 = 4.0;            // an `open_ends` point this near the end: the road goes on
const CROSSING_ON_M: f64 = 2.0;         // a crossing this far from a centre line at most is on that road
const CROSSING_BACK_M: f64 = 1.5;       // m before the crossing
const STRAIGHT_COS: f64 = 0.7;
const SPLIT_COS: f64 = -0.85;           // a one-way leaving nearly opposite to the one arriving: a divided road's split
const TANGENT_M: f64 = 10.0;
const SAME_WAY_COS: f64 = 0.7;
const GAPS_M: [f64; RULES] = [15.0, 30.0, 12.0, 150.0, 150.0, 150.0, 150.0, 100.0, 20.0, 150.0, 200.0, 200.0, 25.0, 25.0, 60.0,
                                   150.0, 8.0, 8.0, 100.0, 100.0, 10.0, 10.0, 100.0, 30.0, 30.0, 25.0, 40.0];        // the same rule, the same way, within this: one sign
const ALONG_STEPS_M: [f64; 5] = [0.0, 2.0, 4.0, 6.0, 8.0];  // a blocked spot: tried farther along
const OUT_STEP_M: f64 = 0.5;
const OUT_MAX_M: f64 = 2.0;
const CLEAR_M: f64 = 0.25;

struct Road {
    a: usize,
    b: usize,
    s: Vec<f64>,
    hw: f64,
    dir: i64,
    rank: i64,
    urban: bool,
    link: bool,
    ring: bool,
    living: bool,
    foot: bool,
    cycle: bool,
}

struct Edge {
    road: usize,
    piece: usize,
    sa: f64,
    sb: f64,
    fwd: bool,
    from: i64,
    to: i64,
}

struct Net<'a> {
    pts: ArrayView2<'a, f64>,
    ll: ArrayView2<'a, f64>,
    roads: Vec<Road>,
    edges: Vec<Edge>,
}

impl Net<'_> {
    fn len(&self, e: usize) -> f64 { self.edges[e].sb - self.edges[e].sa }

    fn at(&self, r: usize, s: f64) -> ([f64; 2], [f64; 2]) {
        let rd = &self.roads[r];
        let n = rd.b - rd.a;
        let s = s.clamp(0.0, rd.s[n - 1]);
        let mut k = match rd.s.binary_search_by(|v| v.partial_cmp(&s).unwrap()) { Ok(k) => k, Err(k) => k.max(1) - 1 };
        k = k.min(n - 2);
        let l = rd.s[k + 1] - rd.s[k];
        let t = if l > 1e-9 { (s - rd.s[k]) / l } else { 0.0 };
        let (i, j) = (rd.a + k, rd.a + k + 1);
        let lerp = |m: &ArrayView2<f64>, c: usize| m[[i, c]] + t * (m[[j, c]] - m[[i, c]]);
        ([lerp(&self.pts, 0), lerp(&self.pts, 1)], [lerp(&self.ll, 0), lerp(&self.ll, 1)])
    }

    /// The direction of travel on road r over arc lengths [s0, s1], fwd: along the geometry (unit).
    fn chord(&self, r: usize, s0: f64, s1: f64, fwd: bool) -> [f64; 2] {
        let (p, _) = self.at(r, s0);
        let (q, _) = self.at(r, s1);
        let (dx, dy) = (q[0] - p[0], q[1] - p[1]);
        let l = (dx * dx + dy * dy).sqrt().max(1e-9);
        if fwd { [dx / l, dy / l] } else { [-dx / l, -dy / l] }
    }

    fn end_dir(&self, e: usize) -> [f64; 2] {
        let ed = &self.edges[e];
        let d = TANGENT_M.min(self.len(e));
        if ed.fwd { self.chord(ed.road, ed.sb - d, ed.sb, true) } else { self.chord(ed.road, ed.sa, ed.sa + d, false) }
    }

    fn start_dir(&self, e: usize) -> [f64; 2] {
        let ed = &self.edges[e];
        let d = TANGENT_M.min(self.len(e));
        if ed.fwd { self.chord(ed.road, ed.sa, ed.sa + d, true) } else { self.chord(ed.road, ed.sb - d, ed.sb, false) }
    }
}

fn dot(a: [f64; 2], b: [f64; 2]) -> f64 { a[0] * b[0] + a[1] * b[1] }

/// A rule's sign before its spot: on road `road` at arc length s, for traffic travelling fwd along the geometry or
/// against it; back: the spot moves towards the traffic when blocked, else away from it.
struct Cand {
    rule: u8,
    road: usize,
    s: f64,
    fwd: bool,
    back: bool,
    key: u64,
    xy: [f64; 2],
    dir: [f64; 2],
    side: Option<f64>,      // None: the kerb side; Some(1.0) the right of the travel, Some(-1.0) the left
}

/// One sign: its spot, the direction of the traffic it faces, its rule, size class (0 town, 1 country, 2 major road),
/// the road point beside it, whether in town.
#[derive(Clone, Debug)]
pub struct Sign {
    pub xy: [f64; 2],
    pub dir: [f64; 2],
    pub rule: u8,
    pub size: usize,
    pub road: [f64; 2],
    pub urban: bool,
    pub pole: usize,
    pub slot: usize,        // 0: the top of its pole
}

pub struct Input<'a> {
    pub pts: ArrayView2<'a, f64>,
    pub ll: ArrayView2<'a, f64>,
    pub off: ArrayView1<'a, i64>,
    pub meta: ArrayView2<'a, f64>,
    pub con_at: ArrayView1<'a, f64>,
    pub con_id: ArrayView1<'a, i64>,
    pub con_off: ArrayView1<'a, i64>,
    pub skip: ArrayView2<'a, f64>,
    pub skip_off: ArrayView1<'a, i64>,
    pub crossings: ArrayView2<'a, f64>,
    pub signals: ArrayView2<'a, f64>,
    pub open_ends: ArrayView2<'a, f64>,
    pub pois: ArrayView2<'a, f64>,          // (n, 5) x, y, kind (POI_*), a, b
    pub avoid: ArrayView2<'a, f64>,         // (n, 4) x, y, travel x, travel y of the speed-limit signs round the box
    pub poly: ArrayView2<'a, f64>,
    pub poly_off: ArrayView1<'a, i64>,
    pub bx: [f64; 4],
    pub left_hand: bool,
    pub lateral: [f64; 2],      // m from the carriageway's edge to the sign's near edge: town, country
    pub widest: f64,            // m: the widest sign, for the clearance of a spot
    pub params: [f64; N_PARAMS],
    pub pole: [f64; N_POLE],
}

fn no_entry(net: &Net, incoming: &HashMap<i64, Vec<usize>>, outgoing: &HashMap<i64, Vec<usize>>, out: &mut Vec<Cand>) {
    let none: Vec<usize> = Vec::new();
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        if rd.dir == 0 || rd.link || rd.ring || rd.rank < 1 || rd.rank >= TRUNK { continue; }
        let d = net.end_dir(e);
        let mut onward = false;
        for &o in outgoing.get(&ed.to).unwrap_or(&none) {
            let oe = &net.edges[o];
            if oe.road == ed.road && oe.piece == ed.piece { continue; }
            let c = dot(net.start_dir(o), d);
            if net.roads[oe.road].dir != 0 && (c > STRAIGHT_COS || c < SPLIT_COS) { onward = true; }
        }
        if onward { continue; }
        // someone must be able to come here other than along this road
        let others = incoming.get(&ed.to).unwrap_or(&none).iter().any(|&i| {
            let o = &net.edges[i];
            o.road != ed.road && net.roads[o.road].rank >= 1
        });
        if !others { continue; }
        let inside = NO_ENTRY_IN_M.min(net.len(e) * 0.5);
        let s = if ed.fwd { ed.sb - inside } else { ed.sa + inside };
        out.push(cand(net, NO_ENTRY, ed.road, s, !ed.fwd, false));
    }
}

/// Whether travel on along e reaches only a dead end within the limit: its length, or None.
fn dead_length(net: &Net, e: usize, ends: &HashMap<i64, usize>, outgoing: &HashMap<i64, Vec<usize>>,
               open: &[[f64; 2]]) -> Option<f64> {
    let none: Vec<usize> = Vec::new();
    let (mut cur, mut total) = (e, 0.0);
    for _ in 0..DEAD_END_HOPS {
        let ed = &net.edges[cur];
        total += net.len(cur);
        if total > DEAD_END_MAX_M { return None; }
        let arms = *ends.get(&ed.to).unwrap_or(&0);
        if arms == 1 {
            let (p, _) = net.at(ed.road, if ed.fwd { ed.sb } else { ed.sa });
            let shut = open.iter().any(|q| (q[0] - p[0]).powi(2) + (q[1] - p[1]).powi(2) < OPEN_END_M * OPEN_END_M);
            return if shut { None } else { Some(total) };
        }
        if arms != 2 { return None; }
        let next: Vec<usize> = outgoing.get(&ed.to).unwrap_or(&none).iter().copied().filter(|&o| {
            let oe = &net.edges[o];
            !(oe.road == ed.road && oe.piece == ed.piece)
        }).collect();
        if next.len() != 1 { return None; }
        cur = next[0];
    }
    None
}

fn dead_ends(net: &Net, ends: &HashMap<i64, usize>, outgoing: &HashMap<i64, Vec<usize>>, open: &[[f64; 2]],
             out: &mut Vec<Cand>) {
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        if rd.dir != 0 || rd.link || rd.ring || rd.rank < 1 || rd.rank > TERTIARY { continue; }
        if *ends.get(&ed.from).unwrap_or(&0) < 3 { continue; }
        let Some(total) = dead_length(net, e, ends, outgoing, open) else { continue };
        if total < DEAD_END_MIN_M { continue; }
        let inside = DEAD_END_IN_M.min(net.len(e) * 0.5);
        let s = if ed.fwd { ed.sa + inside } else { ed.sb - inside };
        out.push(cand(net, DEAD_END, ed.road, s, ed.fwd, false));
    }
}

/// The road a crossing point lies on: (road, arc length), the nearest within CROSSING_ON_M.
fn on_road(net: &Net, q: [f64; 2]) -> Option<(usize, f64)> {
    let mut best: Option<(usize, f64, f64)> = None;
    for (r, rd) in net.roads.iter().enumerate() {
        if rd.rank < 1 || rd.rank > TRUNK || rd.link || rd.ring { continue; }
        for k in rd.a..rd.b.saturating_sub(1) {
            let (ax, ay, bx, by) = (net.pts[[k, 0]], net.pts[[k, 1]], net.pts[[k + 1, 0]], net.pts[[k + 1, 1]]);
            let (dx, dy) = (bx - ax, by - ay);
            let l2 = (dx * dx + dy * dy).max(1e-12);
            let t = (((q[0] - ax) * dx + (q[1] - ay) * dy) / l2).clamp(0.0, 1.0);
            let d2 = (ax + t * dx - q[0]).powi(2) + (ay + t * dy - q[1]).powi(2);
            if d2 <= CROSSING_ON_M * CROSSING_ON_M && best.map_or(true, |b| d2 < b.2) {
                best = Some((r, rd.s[k - rd.a] + t * l2.sqrt(), d2));
            }
        }
    }
    best.map(|b| (b.0, b.1))
}

fn crossings(net: &Net, nodes: &ArrayView2<f64>, out: &mut Vec<Cand>) {
    for k in 0..nodes.nrows() {
        let q = [nodes[[k, 0]], nodes[[k, 1]]];
        let Some((r, s)) = on_road(net, q) else { continue };
        let (dir, total) = (net.roads[r].dir, net.roads[r].s[net.roads[r].b - net.roads[r].a - 1]);
        if total < 2.0 * CROSSING_BACK_M { continue; }
        for fwd in [true, false] {
            if (dir > 0 && !fwd) || (dir < 0 && fwd) { continue; }
            let at = if fwd { s - CROSSING_BACK_M } else { s + CROSSING_BACK_M };
            if at <= 0.0 || at >= total { continue; }
            out.push(cand(net, CROSSING, r, at, fwd, true));
        }
    }
}

fn cand(net: &Net, rule: u8, road: usize, s: f64, fwd: bool, back: bool) -> Cand {
    let (xy, lonlat) = net.at(road, s);
    let key = splitmix(splitmix(((lonlat[0] * 1e6).round() as i64) as u64) ^ ((lonlat[1] * 1e6).round() as i64) as u64)
        ^ (rule as u64) ^ ((fwd as u64) << 8);
    let total = net.roads[road].s[net.roads[road].b - net.roads[road].a - 1];
    let dir = net.chord(road, (s - 3.0).max(0.0), (s + 3.0).min(total), fwd);
    Cand { rule, road, s, fwd, back, key, xy, dir, side: None }
}

/// The arc length on edge e's road at u m from the edge's start in its direction of travel.
fn arc_at(net: &Net, e: usize, u: f64) -> f64 {
    let ed = &net.edges[e];
    if ed.fwd { ed.sa + u } else { ed.sb - u }
}

/// The unit direction of travel on edge e between u0 and u1.
fn travel(net: &Net, e: usize, u0: f64, u1: f64) -> [f64; 2] {
    let r = net.edges[e].road;
    let (p, _) = net.at(r, arc_at(net, e, u0));
    let (q, _) = net.at(r, arc_at(net, e, u1));
    let (dx, dy) = (q[0] - p[0], q[1] - p[1]);
    let l = (dx * dx + dy * dy).sqrt().max(1e-9);
    [dx / l, dy / l]
}

/// A warning for a hazard at u_hazard m along edge e: `warn` m before it, but past the edge's start by SIGN_AFTER_M, and
/// at least min_before m before it; nothing where the piece leaves no room.
fn before(net: &Net, e: usize, u_hazard: f64, p: &[f64; N_PARAMS], out: &mut Vec<Cand>, rule: u8) {
    let u = (u_hazard - p[0]).max(SIGN_AFTER_M);
    if u_hazard - u < p[7] { return; }
    let ed = &net.edges[e];
    out.push(cand(net, rule, ed.road, arc_at(net, e, u), ed.fwd, false));
}

struct Bend {
    from: f64,      // u of its first sample
    to: f64,
    turn: f64,      // radians, positive to the left
}

/// The bends of edge e: windows of `window` m turning at least `min_turn` radians, the sharpest of each run, in travel order.
fn bends(net: &Net, e: usize, window: f64, min_turn: f64) -> Vec<Bend> {
    let len = net.len(e);
    let steps = (len / BEND_STEP_M).floor() as usize;
    let w = ((window / BEND_STEP_M).round() as usize).max(2);
    if steps < w + 2 { return Vec::new(); }
    let head: Vec<f64> = (0..steps).map(|k| {
        let d = travel(net, e, k as f64 * BEND_STEP_M, (k + 1) as f64 * BEND_STEP_M);
        d[1].atan2(d[0])
    }).collect();
    let tau = std::f64::consts::TAU;
    let dh: Vec<f64> = head.windows(2).map(|h| (h[1] - h[0] + std::f64::consts::PI).rem_euclid(tau) - std::f64::consts::PI).collect();
    if dh.len() < w { return Vec::new(); }
    let sums: Vec<f64> = (0..=dh.len() - w).map(|i| dh[i..i + w].iter().sum()).collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < sums.len() {
        if sums[i].abs() < min_turn { i += 1; continue; }
        let sign = sums[i].signum();
        let (mut best, mut k) = (i, i);
        while k < sums.len() && sums[k].abs() >= min_turn && sums[k].signum() == sign {
            if sums[k].abs() > sums[best].abs() { best = k; }
            k += 1;
        }
        out.push(Bend { from: best as f64 * BEND_STEP_M, to: (best + w + 1) as f64 * BEND_STEP_M, turn: sums[best] });
        i = best + w;
    }
    out
}

fn curves(net: &Net, p: &[f64; N_PARAMS], out: &mut Vec<Cand>) {
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        if rd.urban || rd.link || rd.ring || rd.rank < p[3] as i64 { continue; }
        let len = net.len(e);
        if len < MIN_PIECE_M { continue; }
        let list: Vec<Bend> = bends(net, e, p[2], p[1].to_radians()).into_iter()
            .filter(|b| b.from >= BEND_EDGE_M && b.to <= len - BEND_EDGE_M).collect();
        let mut k = 0;
        while k < list.len() {
            let b = &list[k];
            let double = list.get(k + 1).map_or(false, |n| n.turn * b.turn < 0.0 && n.from - b.to <= p[4]);
            let rule = match (b.turn > 0.0, double) {
                (false, false) => CURVE_RIGHT,
                (true, false) => CURVE_LEFT,
                (false, true) => DOUBLE_RIGHT,
                (true, true) => DOUBLE_LEFT,
            };
            before(net, e, b.from, p, out, rule);
            k += if double { 2 } else { 1 };
        }
    }
}

fn roundabouts(net: &Net, p: &[f64; N_PARAMS], out: &mut Vec<Cand>) {
    let mut ring_at: HashMap<i64, bool> = HashMap::new();
    for ed in &net.edges {
        if net.roads[ed.road].ring {
            ring_at.insert(ed.from, true);
            ring_at.insert(ed.to, true);
        }
    }
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        if rd.ring || rd.link || rd.rank < 1 || !ring_at.contains_key(&ed.to) { continue; }
        let len = net.len(e);
        let u = (len - ROUNDABOUT_IN_M).max(len * 0.5);
        out.push(cand(net, ROUNDABOUT, ed.road, arc_at(net, e, u), ed.fwd, true));
        if !rd.urban && rd.rank >= TERTIARY && len >= MIN_PIECE_M {
            before(net, e, len, p, out, ROUNDABOUT_WARN);
        }
    }
}

fn signal_warnings(net: &Net, nodes: &ArrayView2<f64>, p: &[f64; N_PARAMS], out: &mut Vec<Cand>) {
    for k in 0..nodes.nrows() {
        let Some((r, s)) = on_road(net, [nodes[[k, 0]], nodes[[k, 1]]]) else { continue };
        let rd = &net.roads[r];
        if rd.urban || rd.ring || rd.rank < SECONDARY { continue; }
        let total = rd.s[rd.b - rd.a - 1];
        for fwd in [true, false] {
            if (rd.dir > 0 && !fwd) || (rd.dir < 0 && fwd) { continue; }
            let at = if fwd { (s - p[0]).max(SIGN_AFTER_M) } else { (s + p[0]).min(total - SIGN_AFTER_M) };
            if (s - at).abs() < p[7] { continue; }
            out.push(cand(net, SIGNALS, r, at, fwd, false));
        }
    }
}

fn motorways(net: &Net, ends: &HashMap<i64, usize>, incoming: &HashMap<i64, Vec<usize>>,
             outgoing: &HashMap<i64, Vec<usize>>, p: &[f64; N_PARAMS], out: &mut Vec<Cand>) {
    let none: Vec<usize> = Vec::new();
    let is_mw = |i: usize| { let r = &net.roads[net.edges[i].road]; r.rank == MOTORWAY_RANK && !r.link };
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        if !is_mw(e) || rd.dir == 0 { continue; }
        let len = net.len(e);
        let same = |i: usize| net.edges[i].road == ed.road && net.edges[i].piece == ed.piece;
        let inn = incoming.get(&ed.from).unwrap_or(&none);
        if *ends.get(&ed.from).unwrap_or(&0) >= 2 && !inn.is_empty() && !inn.iter().any(|&i| !same(i) && is_mw(i)) {
            out.push(cand(net, MOTORWAY, ed.road, arc_at(net, e, MOTORWAY_IN_M.min(len * 0.5)), ed.fwd, false));
        }
        let outg = outgoing.get(&ed.to).unwrap_or(&none);
        if *ends.get(&ed.to).unwrap_or(&0) >= 2 && !outg.is_empty() && !outg.iter().any(|&i| !same(i) && is_mw(i)) && len >= 60.0 {
            out.push(cand(net, MOTORWAY_END, ed.road, arc_at(net, e, (len - p[5]).max(10.0)), ed.fwd, false));
        }
    }
}

fn living_streets(net: &Net, incoming: &HashMap<i64, Vec<usize>>, outgoing: &HashMap<i64, Vec<usize>>,
                  p: &[f64; N_PARAMS], out: &mut Vec<Cand>) {
    let none: Vec<usize> = Vec::new();
    let plain = |i: usize| { let r = &net.roads[net.edges[i].road]; !r.living && !r.link && !r.ring && r.rank >= 1 };
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        if !net.roads[ed.road].living { continue; }
        let len = net.len(e);
        if len < p[6] { continue; }
        if incoming.get(&ed.from).unwrap_or(&none).iter().any(|&i| plain(i)) {
            out.push(cand(net, LIVING, ed.road, arc_at(net, e, LIVING_IN_M.min(len * 0.5)), ed.fwd, false));
        }
        if outgoing.get(&ed.to).unwrap_or(&none).iter().any(|&i| plain(i)) {
            out.push(cand(net, LIVING_END, ed.road, arc_at(net, e, (len - LIVING_IN_M).max(len * 0.5)), ed.fwd, true));
        }
    }
}

fn one_ways(net: &Net, ends: &HashMap<i64, usize>, incoming: &HashMap<i64, Vec<usize>>, out: &mut Vec<Cand>) {
    let none: Vec<usize> = Vec::new();
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        if rd.dir == 0 || rd.link || rd.ring || rd.living || !rd.urban || rd.rank < 1 || rd.rank > SECONDARY { continue; }
        if *ends.get(&ed.from).unwrap_or(&0) < 3 { continue; }
        let inn = incoming.get(&ed.from).unwrap_or(&none);
        let d = net.start_dir(e);
        let joins = inn.iter().any(|&i| { let o = &net.edges[i]; o.road != ed.road && net.roads[o.road].rank >= 1 });
        let carries = inn.iter().any(|&i| {
            let o = &net.edges[i];
            o.road != ed.road && net.roads[o.road].dir != 0 && dot(net.end_dir(i), d) > STRAIGHT_COS
        });
        if joins && !carries {
            out.push(cand(net, ONEWAY, ed.road, arc_at(net, e, ONEWAY_IN_M.min(net.len(e) * 0.5)), ed.fwd, false));
        }
    }
}

/// Signs sharing poles: the greedy groups of signs of the same size class facing the same way within `join` m of the first of
/// them, at most `max` to a pole, ordered from the top by the rule's place in the country's order; each group moves to its first
/// sign's spot. The rest keep PANEL_GAP_M from every pole and from each other. Poles are numbered in the order they are made.
fn poles(signs: Vec<Sign>, pole: &[f64; N_POLE]) -> Vec<Sign> {
    let (max, join) = (pole[RULES].max(1.0) as usize, pole[RULES + 1]);
    let n = signs.len();
    let rank = |i: usize| pole[signs[i].rule as usize];
    let mut taken = vec![false; n];
    let mut groups: Vec<Vec<usize>> = Vec::new();
    for i in 0..n {
        if taken[i] || rank(i) < 0.0 { continue; }
        let mut g = vec![i];
        for j in i + 1..n {
            if g.len() >= max { break; }
            if taken[j] || rank(j) < 0.0 || signs[j].size != signs[i].size || dot(signs[j].dir, signs[i].dir) < 0.95 { continue; }
            let (dx, dy) = (signs[j].xy[0] - signs[i].xy[0], signs[j].xy[1] - signs[i].xy[1]);
            if dx * dx + dy * dy <= join * join { g.push(j); }
        }
        if g.len() > 1 {
            for &m in &g { taken[m] = true; }
            groups.push(g);
        }
    }
    let mut out: Vec<Sign> = Vec::with_capacity(n);
    for g in &groups {
        let mut order = g.clone();
        order.sort_by(|&x, &y| rank(x).partial_cmp(&rank(y)).unwrap().then(signs[x].rule.cmp(&signs[y].rule)));
        let first = &signs[g[0]];
        for (slot, &m) in order.iter().enumerate() {
            let mut s = signs[m].clone();
            s.xy = first.xy;
            s.dir = first.dir;
            s.road = first.road;
            s.slot = slot;
            out.push(s);
        }
        let base = out.len() - order.len();
        for k in 0..order.len() { out[base + k].pole = base; }
    }
    let mut singles: Vec<Sign> = Vec::new();
    for i in 0..n {
        if taken[i] { continue; }
        let s = &signs[i];
        let near = |o: &Sign| dot(o.dir, s.dir) > SAME_WAY_COS && (o.xy[0] - s.xy[0]).powi(2) + (o.xy[1] - s.xy[1]).powi(2) < PANEL_GAP_M * PANEL_GAP_M;
        if out.iter().any(near) || singles.iter().any(near) { continue; }
        singles.push(s.clone());
    }
    for mut s in singles {
        s.pole = out.len();
        s.slot = 0;
        out.push(s);
    }
    out
}

/// The signs of a box. Arrays as speed_sign_place's; see Input.
pub fn plan(a: &Input) -> Vec<Sign> {
    let mut net = Net { pts: a.pts, ll: a.ll, roads: Vec::new(), edges: Vec::new() };
    let nr = a.off.len().saturating_sub(1);
    let mut ends: HashMap<i64, usize> = HashMap::new();
    for r in 0..nr {
        let (p, q) = (a.off[r] as usize, a.off[r + 1] as usize);
        let mut s = vec![0.0f64; q.saturating_sub(p)];
        for k in p + 1..q {
            s[k - p] = s[k - p - 1] + ((a.pts[[k, 0]] - a.pts[[k - 1, 0]]).powi(2) + (a.pts[[k, 1]] - a.pts[[k - 1, 1]]).powi(2)).sqrt();
        }
        let flags = if a.meta.ncols() > 5 { a.meta[[r, 5]] as i64 } else { 0 };
        let road = Road { a: p, b: q, s, hw: a.meta[[r, 0]], dir: a.meta[[r, 1]] as i64, rank: a.meta[[r, 2]] as i64,
                          urban: a.meta[[r, 3]] != 0.0, link: a.meta[[r, 4]] != 0.0, ring: flags & FLAG_RING != 0,
                          living: flags & FLAG_LIVING != 0, foot: flags & FLAG_FOOT != 0, cycle: flags & FLAG_CYCLE != 0 };
        let total = if q >= p + 2 { road.s[q - p - 1] } else { 0.0 };
        net.roads.push(road);
        if total < 1.0 { continue; }
        let mut nodes: Vec<(f64, i64)> = Vec::new();
        for k in a.con_off[r] as usize..a.con_off[r + 1] as usize {
            let f = a.con_at[k];
            nodes.push((if f < 1e-3 { 0.0 } else if f > 1.0 - 1e-3 { 1.0 } else { f }, a.con_id[k]));
        }
        nodes.sort_by(|x, y| x.partial_cmp(y).unwrap());
        nodes.dedup_by(|x, y| (x.0 - y.0).abs() < 1e-9);
        if nodes.first().map_or(true, |n| n.0 > 0.0) { nodes.insert(0, (0.0, -(2 * r as i64) - 1)); }
        if nodes.last().map_or(true, |n| n.0 < 1.0) { nodes.push((1.0, -(2 * r as i64) - 2)); }
        let dir = net.roads[r].dir;
        for (piece, w) in nodes.windows(2).enumerate() {
            let (sa, sb) = (w[0].0 * total, w[1].0 * total);
            if sb - sa < 0.5 { continue; }
            *ends.entry(w[0].1).or_default() += 1;
            *ends.entry(w[1].1).or_default() += 1;
            for fwd in [true, false] {
                if (dir > 0 && !fwd) || (dir < 0 && fwd) { continue; }
                let (from, to) = if fwd { (w[0].1, w[1].1) } else { (w[1].1, w[0].1) };
                net.edges.push(Edge { road: r, piece, sa, sb, fwd, from, to });
            }
        }
    }
    let mut outgoing: HashMap<i64, Vec<usize>> = HashMap::new();
    let mut incoming: HashMap<i64, Vec<usize>> = HashMap::new();
    for (i, e) in net.edges.iter().enumerate() {
        outgoing.entry(e.from).or_default().push(i);
        incoming.entry(e.to).or_default().push(i);
    }
    let open: Vec<[f64; 2]> = (0..a.open_ends.nrows()).map(|k| [a.open_ends[[k, 0]], a.open_ends[[k, 1]]]).collect();

    let mut cands = Vec::new();
    no_entry(&net, &incoming, &outgoing, &mut cands);
    dead_ends(&net, &ends, &outgoing, &open, &mut cands);
    crossings(&net, &a.crossings, &mut cands);
    curves(&net, &a.params, &mut cands);
    roundabouts(&net, &a.params, &mut cands);
    signal_warnings(&net, &a.signals, &a.params, &mut cands);
    motorways(&net, &ends, &incoming, &outgoing, &a.params, &mut cands);
    living_streets(&net, &incoming, &outgoing, &a.params, &mut cands);
    one_ways(&net, &ends, &incoming, &mut cands);
    more::all(&net, &ends, &incoming, &outgoing, a, &mut cands);
    cands.sort_by(|x, y| (x.rule, x.key).cmp(&(y.rule, y.key)));

    // one a place, per rule and direction, in a fixed order
    let mut kept: Vec<usize> = Vec::new();
    let mut grid: HashMap<(u8, i64, i64), Vec<usize>> = HashMap::new();
    let cell = 40.0;
    for (i, c) in cands.iter().enumerate() {
        let (gx, gy) = ((c.xy[0] / cell).floor() as i64, (c.xy[1] / cell).floor() as i64);
        let gap = GAPS_M[c.rule as usize];
        let mut near = false;
        'scan: for dx in -1..=1 {
            for dy in -1..=1 {
                for &j in grid.get(&(c.rule, gx + dx, gy + dy)).map_or(&[][..], |v| &v[..]) {
                    let o = &cands[j];
                    if dot(o.dir, c.dir) > SAME_WAY_COS
                        && (o.xy[0] - c.xy[0]).powi(2) + (o.xy[1] - c.xy[1]).powi(2) < gap * gap {
                        near = true;
                        break 'scan;
                    }
                }
            }
        }
        if !near {
            grid.entry((c.rule, gx, gy)).or_default().push(i);
            kept.push(i);
        }
    }

    let boxes: Vec<[f64; 4]> = (0..a.poly_off.len().saturating_sub(1)).map(|q| {
        let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for k in a.poly_off[q] as usize..a.poly_off[q + 1] as usize {
            b[0] = b[0].min(a.poly[[k, 0]]); b[1] = b[1].min(a.poly[[k, 1]]);
            b[2] = b[2].max(a.poly[[k, 0]]); b[3] = b[3].max(a.poly[[k, 1]]);
        }
        b
    }).collect();
    let side = if a.left_hand { -1.0 } else { 1.0 };
    let mut out: Vec<Sign> = Vec::new();
    for &i in &kept {
        let c = &cands[i];
        let rd = &net.roads[c.road];
        let total = rd.s[rd.b - rd.a - 1];
        let f = c.s / total.max(1e-9);
        let (sa, sb) = (a.skip_off[c.road] as usize, a.skip_off[c.road + 1] as usize);
        if (sa..sb).any(|k| a.skip[[k, 0]] - 0.02 <= f && f <= a.skip[[k, 1]] + 0.02) { continue; }
        let base = rd.hw + a.lateral[if rd.urban { 0 } else { 1 }] + a.widest / 2.0;
        let along = if c.fwd != c.back { 1.0 } else { -1.0 };   // (the spot moves with the traffic, or back towards it)
        let mut found = None;
        'spot: for du in ALONG_STEPS_M {
            let s = c.s + along * du;
            if s <= 0.0 || s >= total { break; }
            let (road_xy, _) = net.at(c.road, s);
            let t = net.chord(c.road, (s - 3.0).max(0.0), (s + 3.0).min(total), c.fwd);
            let sd = c.side.unwrap_or(side);
            let right = [t[1] * sd, -t[0] * sd];
            let mut extra = 0.0;
            while extra <= OUT_MAX_M + 1e-9 {
                let q = [road_xy[0] + right[0] * (base + extra), road_xy[1] + right[1] * (base + extra)];
                if clear(q, a.widest / 2.0 + CLEAR_M, &a.poly, &a.poly_off, &boxes) {
                    found = Some((q, t, road_xy));
                    break 'spot;
                }
                extra += OUT_STEP_M;
            }
        }
        let Some((q, t, road_xy)) = found else { continue };
        if (0..a.avoid.nrows()).any(|k| dot([a.avoid[[k, 2]], a.avoid[[k, 3]]], t) > SAME_WAY_COS
                                    && (a.avoid[[k, 0]] - q[0]).powi(2) + (a.avoid[[k, 1]] - q[1]).powi(2) < AVOID_M * AVOID_M) {
            continue;
        }
        let size = if rd.urban { 0 } else if rd.rank >= PRIMARY { 2 } else { 1 };
        out.push(Sign { xy: q, dir: t, rule: c.rule, size, road: road_xy, urban: rd.urban, pole: 0, slot: 0 });
    }
    let out = poles(out, &a.pole);
    out.into_iter().filter(|o| o.xy[0] >= a.bx[0] && o.xy[0] < a.bx[2] && o.xy[1] >= a.bx[1] && o.xy[1] < a.bx[3]).collect()
}

/// The road signs of a box (x0, y0, x1, y1). Roads, junctions and bridge / tunnel ranges as speed_sign_place's; crossings
/// (c, 2) points on the roads' centre lines, signals the same for traffic signals; meta (r, 6) as speed_sign_place's with the flags
/// (FLAG_RING, FLAG_LIVING, FLAG_FOOT, FLAG_CYCLE) in column 5; params: see N_PARAMS; open_ends (o, 2) where a road goes on unpaved. lateral: m from the
/// carriageway's edge to the sign's near edge [town, country]; widest: m, the widest sign's width.
/// avoid (n, 4): the speed-limit signs (x, y, travel x, travel y); pole: see N_POLE; pois (n, 5): see POI_GIVE_WAY.
/// Returns (k, 11) [x, y, travel x, travel y, rule, size class, road x, road y, urban, pole, slot].
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn road_sign_place<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, ll: PyReadonlyArray2<'py, f64>,
                            off: PyReadonlyArray1<'py, i64>, meta: PyReadonlyArray2<'py, f64>,
                            con_at: PyReadonlyArray1<'py, f64>, con_id: PyReadonlyArray1<'py, i64>,
                            con_off: PyReadonlyArray1<'py, i64>, skip: PyReadonlyArray2<'py, f64>,
                            skip_off: PyReadonlyArray1<'py, i64>, crossings: PyReadonlyArray2<'py, f64>,
                            signals: PyReadonlyArray2<'py, f64>, open_ends: PyReadonlyArray2<'py, f64>,
                            poly: PyReadonlyArray2<'py, f64>, poly_off: PyReadonlyArray1<'py, i64>, bx: [f64; 4],
                            left_hand: bool, lateral: [f64; 2], widest: f64, params: [f64; N_PARAMS],
                            avoid: PyReadonlyArray2<'py, f64>, pole: [f64; N_POLE], pois: PyReadonlyArray2<'py, f64>)
                            -> Bound<'py, PyArray2<f64>> {
    let signs = plan(&Input { pts: pts.as_array(), ll: ll.as_array(), off: off.as_array(), meta: meta.as_array(),
                              con_at: con_at.as_array(), con_id: con_id.as_array(), con_off: con_off.as_array(),
                              skip: skip.as_array(), skip_off: skip_off.as_array(), crossings: crossings.as_array(),
                              signals: signals.as_array(), open_ends: open_ends.as_array(), pois: pois.as_array(), poly: poly.as_array(),
                              poly_off: poly_off.as_array(), bx, left_hand, lateral, widest, params,
                              avoid: avoid.as_array(), pole });
    let n = signs.len();
    let rows: Vec<f64> = signs.iter().flat_map(|s| [s.xy[0], s.xy[1], s.dir[0], s.dir[1], s.rule as f64, s.size as f64,
                                                    s.road[0], s.road[1], if s.urban { 1.0 } else { 0.0 },
                                                    s.pole as f64, s.slot as f64]).collect();
    numpy::ndarray::Array2::from_shape_vec((n, 11), rows).unwrap().into_pyarray(py)
}

// ---- the mesh ----

const M_ATLAS: i64 = 0;
const M_METAL: i64 = 1;
const TRI_TOP: f64 = 0.90;      // tools/build_sign_atlas.py
const TRI_BOTTOM: f64 = -0.83;
const CORNER: f64 = 0.12;
const BACK_M: f64 = 0.004;

/// A convex outline's corners rounded by r (tools/build_sign_atlas.py rounded()).
fn round_poly(verts: &[[f64; 2]], r: f64) -> Vec<[f64; 2]> {
    const STEPS: usize = 4;
    let n = verts.len();
    let mut out = Vec::with_capacity(n * (STEPS + 1));
    for i in 0..n {
        let (p, a, b) = (verts[(i + n - 1) % n], verts[i], verts[(i + 1) % n]);
        let (ux, uy) = (p[0] - a[0], p[1] - a[1]);
        let (vx, vy) = (b[0] - a[0], b[1] - a[1]);
        let (lu, lv) = ((ux * ux + uy * uy).sqrt(), (vx * vx + vy * vy).sqrt());
        let (ux, uy, vx, vy) = (ux / lu, uy / lu, vx / lv, vy / lv);
        let half = (ux * vx + uy * vy).clamp(-1.0, 1.0).acos() / 2.0;
        let d = r / half.tan();
        let (bx, by) = (ux + vx, uy + vy);
        let lb = (bx * bx + by * by).sqrt();
        let c = [a[0] + bx / lb * r / half.sin(), a[1] + by / lb * r / half.sin()];
        let a0 = (a[1] + uy * d - c[1]).atan2(a[0] + ux * d - c[0]);
        let a1 = (a[1] + vy * d - c[1]).atan2(a[0] + vx * d - c[0]);
        let tau = std::f64::consts::TAU;
        let da = (a1 - a0 + std::f64::consts::PI).rem_euclid(tau) - std::f64::consts::PI;
        for k in 0..=STEPS {
            let t = a0 + da * k as f64 / STEPS as f64;
            out.push([c[0] + r * t.cos(), c[1] + r * t.sin()]);
        }
    }
    out
}

/// A shape's outline in cell units ([-1, 1]^2, y up), counter-clockwise as the driver sees it: 0 circle, 1 triangle,
/// 2 inverted triangle, 3 diamond, 4 octagon, 5 square.
pub fn outline(shape: i64) -> Vec<[f64; 2]> {
    match shape {
        1 => round_poly(&[[0.0, TRI_TOP], [-1.0, TRI_BOTTOM], [1.0, TRI_BOTTOM]], CORNER),
        2 => round_poly(&[[0.0, -TRI_TOP], [1.0, -TRI_BOTTOM], [-1.0, -TRI_BOTTOM]], CORNER),
        3 => round_poly(&[[0.0, 0.98], [-0.98, 0.0], [0.0, -0.98], [0.98, 0.0]], CORNER),
        4 => {
            let t = 0.98 * (std::f64::consts::PI / 8.0).tan();
            vec![[t, 0.98], [-t, 0.98], [-0.98, t], [-0.98, -t], [-t, -0.98], [t, -0.98], [0.98, -t], [0.98, t]]
        }
        5 => round_poly(&[[0.97, 0.97], [-0.97, 0.97], [-0.97, -0.97], [0.97, -0.97]], 0.10),
        6 => round_poly(&[[0.97, 0.25], [-0.97, 0.25], [-0.97, -0.25], [0.97, -0.25]], 0.04),     // a long board, four times as wide as high
        _ => (0..32).map(|k| {
            let a = k as f64 * std::f64::consts::TAU / 32.0;
            [0.99 * a.cos(), 0.99 * a.sin()]
        }).collect(),
    }
}

/// The mesh of signs (k, 10) [x, y, ground z, road z, travel x, travel y, atlas cell, shape, width m, lower edge above
/// the road m] on an atlas of cols x rows cells, relative to origin. Returns (vertices, triangles, uv, material of
/// each triangle): 0 the atlas, 1 metal.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn road_sign_mesh<'py>(py: Python<'py>, signs: PyReadonlyArray2<'py, f64>, cols: i64, rows: i64, origin: [f64; 3])
                           -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>,
                               Bound<'py, PyArray1<i64>>) {
    let signs = signs.as_array();
    let mut m = Mesh { p: Vec::new(), t: Vec::new(), uv: Vec::new(), mi: Vec::new(), centre: [0.0; 3], face: [0.0; 2],
                       right: [0.0; 2] };
    let mut shapes: HashMap<i64, Vec<[f64; 2]>> = HashMap::new();
    for k in 0..signs.nrows() {
        let r = signs.row(k);
        let (x, y, zg, zr, tx, ty) = (r[0], r[1], r[2], r[3], r[4], r[5]);
        if !(x.is_finite() && y.is_finite() && zg.is_finite() && zr.is_finite()) { continue; }
        let (cell, shape, w, mount) = (r[6] as i64, r[7] as i64, r[8], r[9]);
        let tl = (tx * tx + ty * ty).sqrt().max(1e-9);
        m.centre = [x - origin[0], y - origin[1], zg - origin[2]];
        m.face = [-tx / tl, -ty / tl];
        m.right = [ty / tl, -tx / tl];
        let bottom = (zr + mount).max(zg + 1.2) - zg;
        let out = shapes.entry(shape).or_insert_with(|| outline(shape));
        let half = w / 2.0;
        let high = out.iter().fold(0.0f64, |a, q| a.max(q[1].abs()));     // (a wide board is lower than it is wide)
        let zc = bottom + high * half;
        m.pole(-0.05, 0.03, -0.3, zc + 0.3 * half * high, M_METAL);
        let (cu, cv) = ((cell % cols) as f64, (cell / cols) as f64);
        let n = out.len();
        let i = m.p.len() as i64;
        for q in out.iter() {
            let w3 = m.world([0.0, q[0] * half, zc + q[1] * half]);
            m.p.push(w3);
            m.uv.push([(cu + (1.0 + q[0]) / 2.0) / cols as f64, 1.0 - (cv + (1.0 - q[1]) / 2.0) / rows as f64]);
        }
        for q in out.iter() {
            let w3 = m.world([-BACK_M, q[0] * half, zc + q[1] * half]);
            m.p.push(w3);
            m.uv.push([0.0, 0.0]);
        }
        for j in 1..n as i64 - 1 {
            m.t.push([i, i + j, i + j + 1]);
            m.mi.push(M_ATLAS);
            let b = i + n as i64;
            m.t.push([b, b + j + 1, b + j]);
            m.mi.push(M_METAL);
        }
    }
    let (n, t) = (m.p.len(), m.t.len());
    let p = numpy::ndarray::Array2::from_shape_vec((n, 3), m.p.into_iter().flatten().collect()).unwrap();
    let tri = numpy::ndarray::Array2::from_shape_vec((t, 3), m.t.into_iter().flatten().collect()).unwrap();
    let uv = numpy::ndarray::Array2::from_shape_vec((n, 2), m.uv.into_iter().flatten().collect()).unwrap();
    (p.into_pyarray(py), tri.into_pyarray(py), uv.into_pyarray(py), m.mi.into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;
    use numpy::ndarray::{arr1, arr2, Array1, Array2};

    /// Roads as (points, direction, rank, junction ids at their two ends), half width 4 m, in town.
    struct T {
        pts: Array2<f64>,
        off: Array1<i64>,
        meta: Array2<f64>,
        con_at: Array1<f64>,
        con_id: Array1<i64>,
        con_off: Array1<i64>,
    }

    type Line<'a> = (&'a [[f64; 2]], f64, f64, [i64; 2]);

    fn roads(list: &[Line]) -> T {
        let x: Vec<_> = list.iter().map(|(l, d, r, e)| (*l, *d, *r, *e, 1.0, 0, 0.0)).collect();
        roads_x(&x)
    }

    /// Roads as (points, direction, rank, junction ids at their two ends, urban, flags, link), half width 4 m.
    #[allow(clippy::type_complexity)]
    fn roads_x(list: &[(&[[f64; 2]], f64, f64, [i64; 2], f64, i64, f64)]) -> T {
        let (mut p, mut off, mut meta, mut at, mut id, mut co) = (vec![], vec![0i64], vec![], vec![], vec![], vec![0i64]);
        for (line, dir, rank, ends, urban, flags, link) in list {
            p.extend(line.iter().flatten().copied());
            off.push(p.len() as i64 / 2);
            meta.extend([4.0, *dir, *rank, *urban, *link, *flags as f64]);
            for (f, c) in [(0.0, ends[0]), (1.0, ends[1])] {
                if c >= 0 { at.push(f); id.push(c); }
            }
            co.push(at.len() as i64);
        }
        let n = p.len() / 2;
        T { pts: Array2::from_shape_vec((n, 2), p).unwrap(), off: Array1::from(off),
            meta: Array2::from_shape_vec((list.len(), 6), meta).unwrap(), con_at: Array1::from(at),
            con_id: Array1::from(id), con_off: Array1::from(co) }
    }

    const PARAMS: [f64; N_PARAMS] = [150.0, 55.0, 60.0, 3.0, 100.0, 150.0, 40.0, 50.0, 70.0, 40.0, 0.8];

    fn run(t: &T, cross: Array2<f64>, open: Array2<f64>, left: bool) -> Vec<Sign> {
        run_x(t, cross, none(), open, left)
    }

    /// Poles as the Czech order has them: priority 1, warning 0 ... every rule given its group's place; none alone.
    fn all_alone() -> [f64; N_POLE] {
        let mut p = [-1.0; N_POLE];
        p[RULES] = 3.0;
        p[RULES + 1] = 12.0;
        p
    }

    fn run_x(t: &T, cross: Array2<f64>, signals: Array2<f64>, open: Array2<f64>, left: bool) -> Vec<Sign> {
        run_p(t, cross, signals, open, none4(), all_alone(), left)
    }

    fn none4() -> Array2<f64> { Array2::zeros((0, 4)) }

    fn run_p(t: &T, cross: Array2<f64>, signals: Array2<f64>, open: Array2<f64>, avoid: Array2<f64>, pole: [f64; N_POLE],
             left: bool) -> Vec<Sign> {
        run_q(t, cross, signals, open, avoid, pole, Array2::zeros((0, 5)), left)
    }

    /// The road signs of the roads and the points of interest (x, y, kind, a, b) alone.
    fn with_pois(t: &T, pois: Array2<f64>, left: bool) -> Vec<Sign> {
        run_q(t, none(), none(), none(), none4(), all_alone(), pois, left)
    }

    #[allow(clippy::too_many_arguments)]
    fn run_q(t: &T, cross: Array2<f64>, signals: Array2<f64>, open: Array2<f64>, avoid: Array2<f64>, pole: [f64; N_POLE],
             pois: Array2<f64>, left: bool) -> Vec<Sign> {
        let skip = Array2::<f64>::zeros((0, 2));
        let skip_off = Array1::<i64>::zeros(t.off.len());
        let poly = Array2::<f64>::zeros((0, 2));
        let poly_off = arr1(&[0i64]);
        plan(&Input { pts: t.pts.view(), ll: t.pts.view(), off: t.off.view(), meta: t.meta.view(), con_at: t.con_at.view(),
                      con_id: t.con_id.view(), con_off: t.con_off.view(), skip: skip.view(), skip_off: skip_off.view(),
                      crossings: cross.view(), signals: signals.view(), open_ends: open.view(), pois: pois.view(), avoid: avoid.view(), poly: poly.view(),
                      poly_off: poly_off.view(), bx: [-1e4, -1e4, 1e4, 1e4], left_hand: left, lateral: [0.5, 1.2],
                      widest: 0.75, params: PARAMS, pole: pole })
    }

    fn none() -> Array2<f64> { Array2::zeros((0, 2)) }

    fn t_junction(oneway: f64) -> T {
        // an east-west two-way street, and a one-way street coming south into it at (0, 0)
        roads(&[(&[[-1000.0, 0.0], [0.0, 0.0]], 0.0, 3.0, [1, 2]), (&[[0.0, 0.0], [1000.0, 0.0]], 0.0, 3.0, [2, 3]),
                (&[[0.0, 100.0], [0.0, 0.0]], oneway, 1.0, [4, 2])])
    }

    #[test]
    fn no_entry_faces_wrong_way_traffic_at_the_end_of_a_one_way() {
        let s = run(&t_junction(1.0), none(), none(), false);
        let e: Vec<_> = s.iter().filter(|x| x.rule == NO_ENTRY).collect();
        assert_eq!(e.len(), 1, "{s:?}");
        assert!(e[0].dir[1] > 0.99, "faces traffic going north: {:?}", e[0].dir);
        assert!(e[0].xy[0] > 4.0, "on that traffic's right: {:?}", e[0].xy);
        assert!(e[0].xy[1] > 0.0 && e[0].xy[1] < 20.0);
    }

    #[test]
    fn left_hand_traffic_has_it_on_the_left() {
        let s = run(&t_junction(1.0), none(), none(), true);
        let e = s.iter().find(|x| x.rule == NO_ENTRY).unwrap();
        assert!(e.xy[0] < -4.0, "{:?}", e.xy);
    }

    #[test]
    fn no_entry_not_where_the_one_way_carries_on() {
        let t = roads(&[(&[[0.0, 100.0], [0.0, 0.0]], 1.0, 1.0, [1, 2]), (&[[0.0, 0.0], [0.0, -100.0]], 1.0, 1.0, [2, 3]),
                        (&[[-100.0, 0.0], [0.0, 0.0]], 0.0, 1.0, [4, 2])]);
        assert!(run(&t, none(), none(), false).iter().all(|x| x.rule != NO_ENTRY));
    }

    #[test]
    fn no_entry_not_where_a_divided_road_splits() {
        // a two-way road from the west splits at (0, 0): the two carriageways nearly parallel
        let t = roads(&[(&[[-1000.0, 0.0], [0.0, 0.0]], 0.0, 3.0, [1, 2]),
                        (&[[0.0, 0.0], [100.0, 8.0]], 1.0, 3.0, [2, 3]), (&[[100.0, -8.0], [0.0, 0.0]], 1.0, 3.0, [4, 2])]);
        assert!(run(&t, none(), none(), false).iter().all(|x| x.rule != NO_ENTRY));
    }

    #[test]
    fn no_entry_needs_someone_to_come_from_elsewhere() {
        let t = roads(&[(&[[0.0, 100.0], [0.0, 0.0]], 1.0, 1.0, [1, 2])]);
        assert!(run(&t, none(), none(), false).is_empty());
    }

    #[test]
    fn dead_end_only_on_entering_a_road_that_ends() {
        let s = run(&t_junction(0.0), none(), none(), false);
        let d: Vec<_> = s.iter().filter(|x| x.rule == DEAD_END).collect();
        assert_eq!(d.len(), 1, "{s:?}");
        assert!(d[0].dir[1] > 0.99, "faces traffic turning in: {:?}", d[0].dir);
    }

    #[test]
    fn dead_end_not_where_a_track_goes_on() {
        let open = arr2(&[[0.0, 100.0]]);
        assert!(run(&t_junction(0.0), none(), open, false).iter().all(|x| x.rule != DEAD_END));
    }

    #[test]
    fn dead_end_not_on_a_long_road() {
        let t = roads(&[(&[[-1000.0, 0.0], [0.0, 0.0]], 0.0, 3.0, [1, 2]), (&[[0.0, 0.0], [1000.0, 0.0]], 0.0, 3.0, [2, 3]),
                        (&[[0.0, 600.0], [0.0, 0.0]], 0.0, 1.0, [4, 2])]);
        assert!(run(&t, none(), none(), false).iter().all(|x| x.rule != DEAD_END));
    }

    #[test]
    fn crossing_gets_one_sign_each_way_on_a_two_way_and_one_on_a_one_way() {
        let line: &[[f64; 2]] = &[[0.0, 0.0], [200.0, 0.0]];
        let two = roads(&[(line, 0.0, 3.0, [1, 2])]);
        let at = arr2(&[[100.0, 0.5]]);
        let s = run(&two, at.clone(), none(), false);
        assert_eq!(s.iter().filter(|x| x.rule == CROSSING).count(), 2, "{s:?}");
        let one = roads(&[(line, 1.0, 3.0, [1, 2])]);
        let s = run(&one, at, none(), false);
        assert_eq!(s.len(), 1, "{s:?}");
        assert!(s[0].dir[0] > 0.99 && s[0].xy[0] < 100.0 && s[0].xy[1] < -4.0, "{s:?}");
    }

    #[test]
    fn crossing_away_from_every_road_gets_none() {
        let t = roads(&[(&[[0.0, 0.0], [200.0, 0.0]], 0.0, 3.0, [1, 2])]);
        assert!(run(&t, arr2(&[[100.0, 9.0]]), none(), false).is_empty());
    }

    #[test]
    fn the_same_every_time() {
        let t = t_junction(1.0);
        let a: Vec<String> = run(&t, arr2(&[[50.0, 0.0]]), none(), false).iter().map(|s| format!("{s:?}")).collect();
        let b: Vec<String> = run(&t, arr2(&[[50.0, 0.0]]), none(), false).iter().map(|s| format!("{s:?}")).collect();
        assert_eq!(a, b);
    }

    /// Points of a circular arc round (cx, cy) of radius r from angle a0 to a1 (degrees).
    fn arc(cx: f64, cy: f64, r: f64, a0: f64, a1: f64) -> Vec<[f64; 2]> {
        let n = ((a1 - a0).abs() / 5.0).ceil().max(2.0) as usize;
        (0..=n).map(|k| {
            let a = (a0 + (a1 - a0) * k as f64 / n as f64).to_radians();
            [cx + r * a.cos(), cy + r * a.sin()]
        }).collect()
    }

    fn of(s: &[Sign], rule: u8) -> Vec<&Sign> { s.iter().filter(|x| x.rule == rule).collect() }

    fn country(line: &[[f64; 2]], dir: f64, rank: f64) -> T {
        roads_x(&[(line, dir, rank, [1, 2], 0.0, 0, 0.0)])
    }

    #[test]
    fn a_sharp_bend_gets_a_warning_each_way_with_the_turns_own_side() {
        // east 250 m, a left arc of radius 50 m (90 degrees), north 250 m
        let mut line: Vec<[f64; 2]> = vec![[-300.0, 0.0]];
        line.extend(arc(-50.0, 50.0, 50.0, -90.0, 0.0));
        line.push([0.0, 300.0]);
        let s = run(&country(&line, 0.0, 4.0), none(), none(), false);
        let l = of(&s, CURVE_LEFT);
        assert_eq!(l.len(), 1, "{s:?}");
        assert!(l[0].dir[0] > 0.99 && l[0].xy[0] > -215.0 && l[0].xy[0] < -185.0, "150 m before the bend: {:?}", l[0]);
        let r = of(&s, CURVE_RIGHT);
        assert_eq!(r.len(), 1, "{s:?}");
        assert!(r[0].dir[1] < -0.99, "coming back it turns right: {:?}", r[0]);
        assert!(s.iter().all(|x| x.rule == CURVE_LEFT || x.rule == CURVE_RIGHT), "{s:?}");
    }

    #[test]
    fn gentle_bends_town_roads_small_roads_and_junction_corners_get_none() {
        let mut gentle: Vec<[f64; 2]> = vec![[-300.0, 0.0]];
        gentle.extend(arc(-50.0, 300.0, 300.0, -90.0, -70.0));
        gentle.push([200.0, 90.0]);
        assert!(run(&country(&gentle, 0.0, 4.0), none(), none(), false).is_empty());
        let mut sharp: Vec<[f64; 2]> = vec![[-300.0, 0.0]];
        sharp.extend(arc(-50.0, 50.0, 50.0, -90.0, 0.0));
        sharp.push([0.0, 300.0]);
        assert!(run(&roads_x(&[(&sharp, 0.0, 4.0, [1, 2], 1.0, 0, 0.0)]), none(), none(), false).is_empty(), "in town");
        assert!(run(&country(&sharp, 0.0, 2.0), none(), none(), false).is_empty(), "a residential road");
        let corner = roads_x(&[(&[[-300.0, 0.0], [0.0, 0.0]], 0.0, 4.0, [1, 2], 0.0, 0, 0.0),
                               (&[[0.0, 0.0], [0.0, 300.0]], 0.0, 4.0, [2, 3], 0.0, 0, 0.0)]);
        assert!(run(&corner, none(), none(), false).is_empty(), "a junction's corner");
    }

    #[test]
    fn a_real_s_bend_is_a_double_curve_not_two_curves() {
        let mut line: Vec<[f64; 2]> = vec![[-300.0, 0.0]];
        line.extend(arc(-50.0, 50.0, 50.0, -90.0, 0.0));            // left, ends at (0, 50) heading north
        line.extend(arc(50.0, 50.0, 50.0, 180.0, 90.0));            // then right, back to heading east
        line.push([400.0, 100.0]);
        let s = run(&country(&line, 1.0, 4.0), none(), none(), false);
        let d = of(&s, DOUBLE_LEFT);
        assert_eq!(d.len(), 1, "{s:?}");
        assert!(of(&s, CURVE_LEFT).is_empty() && of(&s, CURVE_RIGHT).is_empty() && of(&s, DOUBLE_RIGHT).is_empty(), "{s:?}");
    }

    #[test]
    fn signals_in_the_country_are_warned_of_before_and_in_town_not() {
        let line: &[[f64; 2]] = &[[0.0, 0.0], [800.0, 0.0]];
        let at = arr2(&[[500.0, 0.5]]);
        let s = run_x(&country(line, 0.0, 4.0), none(), at.clone(), none(), false);
        let w = of(&s, SIGNALS);
        assert_eq!(w.len(), 2, "{s:?}");
        let east = w.iter().find(|x| x.dir[0] > 0.0).unwrap();
        let west = w.iter().find(|x| x.dir[0] < 0.0).unwrap();
        assert!(east.xy[0] > 335.0 && east.xy[0] < 365.0 && west.xy[0] > 635.0 && west.xy[0] < 665.0, "{s:?}");
        let town = roads_x(&[(line, 0.0, 4.0, [1, 2], 1.0, 0, 0.0)]);
        assert!(run_x(&town, none(), at.clone(), none(), false).is_empty());
        assert!(run_x(&country(line, 0.0, 3.0), none(), at, none(), false).is_empty(), "a tertiary road");
    }

    #[test]
    fn a_roundabout_gets_its_entry_sign_and_in_the_country_a_warning() {
        let ring = arc(0.0, 0.0, 30.0, 180.0, 540.0);
        let t = roads_x(&[(&ring, 0.0, 4.0, [10, 10], 0.0, FLAG_RING, 0.0),
                          (&[[-330.0, 0.0], [-30.0, 0.0]], 0.0, 4.0, [11, 10], 0.0, 0, 0.0)]);
        let s = run(&t, none(), none(), false);
        let m = of(&s, ROUNDABOUT);
        assert_eq!(m.len(), 1, "{s:?}");
        assert!(m[0].dir[0] > 0.99 && m[0].xy[0] < -30.0 && m[0].xy[0] > -45.0, "{:?}", m[0]);
        let w = of(&s, ROUNDABOUT_WARN);
        assert_eq!(w.len(), 1, "{s:?}");
        assert!(w[0].xy[0] > -195.0 && w[0].xy[0] < -165.0, "{:?}", w[0]);
        assert!(s.iter().all(|x| x.rule == ROUNDABOUT || x.rule == ROUNDABOUT_WARN), "no sign on the ring: {s:?}");
        let town = roads_x(&[(&ring, 0.0, 4.0, [10, 10], 1.0, FLAG_RING, 0.0),
                             (&[[-330.0, 0.0], [-30.0, 0.0]], 0.0, 4.0, [11, 10], 1.0, 0, 0.0)]);
        assert!(of(&run(&town, none(), none(), false), ROUNDABOUT_WARN).is_empty(), "no warning in town");
    }

    #[test]
    fn a_motorway_is_signed_where_it_begins_and_before_it_ends() {
        let t = roads_x(&[(&[[-300.0, 0.0], [0.0, 0.0]], 1.0, 3.0, [5, 1], 0.0, 0, 0.0),
                          (&[[0.0, 0.0], [1000.0, 0.0]], 1.0, 7.0, [1, 6], 0.0, 0, 0.0),
                          (&[[1000.0, 0.0], [1300.0, 0.0]], 1.0, 3.0, [6, 7], 0.0, 0, 0.0)]);
        let s = run(&t, none(), none(), false);
        let a = of(&s, MOTORWAY);
        let b = of(&s, MOTORWAY_END);
        assert_eq!((a.len(), b.len()), (1, 1), "{s:?}");
        assert!(a[0].xy[0] > 5.0 && a[0].xy[0] < 40.0 && a[0].dir[0] > 0.99, "{:?}", a[0]);
        assert!(b[0].xy[0] > 830.0 && b[0].xy[0] < 870.0, "{:?}", b[0]);
    }

    #[test]
    fn a_motorway_that_carries_on_as_one_has_no_signs_at_the_join() {
        let t = roads_x(&[(&[[0.0, 0.0], [500.0, 0.0]], 1.0, 7.0, [1, 2], 0.0, 0, 0.0),
                          (&[[500.0, 0.0], [1000.0, 0.0]], 1.0, 7.0, [2, 3], 0.0, 0, 0.0)]);
        assert!(run(&t, none(), none(), false).iter().all(|x| x.rule != MOTORWAY && x.rule != MOTORWAY_END));
    }

    #[test]
    fn a_living_street_is_signed_at_both_ends_of_its_junction() {
        let t = roads_x(&[(&[[-200.0, 0.0], [0.0, 0.0]], 0.0, 2.0, [1, 2], 1.0, 0, 0.0),
                          (&[[0.0, 0.0], [200.0, 0.0]], 0.0, 1.0, [2, 3], 1.0, FLAG_LIVING, 0.0)]);
        let s = run(&t, none(), none(), false);
        let (e, x) = (of(&s, LIVING), of(&s, LIVING_END));
        assert_eq!((e.len(), x.len()), (1, 1), "{s:?}");
        assert!(e[0].dir[0] > 0.99 && x[0].dir[0] < -0.99, "{s:?}");
        assert!(e[0].xy[0] < 15.0 && x[0].xy[0] < 15.0, "{s:?}");
    }

    #[test]
    fn a_one_way_street_is_signed_where_it_starts_but_not_where_one_way_runs_on() {
        let cross = [(&[[-200.0, 0.0], [0.0, 0.0]][..], 0.0, 2.0, [1, 2], 1.0, 0, 0.0),
                     (&[[0.0, 0.0], [200.0, 0.0]][..], 0.0, 2.0, [2, 3], 1.0, 0, 0.0),
                     (&[[0.0, 0.0], [0.0, 200.0]][..], 1.0, 2.0, [2, 4], 1.0, 0, 0.0)];
        let s = run(&roads_x(&cross), none(), none(), false);
        let o = of(&s, ONEWAY);
        assert_eq!(o.len(), 1, "{s:?}");
        assert!(o[0].dir[1] > 0.99 && o[0].xy[1] < 15.0, "{:?}", o[0]);
        let mut with_feeder = cross.to_vec();
        with_feeder.push((&[[0.0, -200.0], [0.0, 0.0]][..], 1.0, 2.0, [8, 2], 1.0, 0, 0.0));
        assert!(of(&run(&roads_x(&with_feeder), none(), none(), false), ONEWAY).is_empty(), "one-way carried straight on");
    }

    #[test]
    fn signs_of_different_rules_keep_apart() {
        let line: &[[f64; 2]] = &[[0.0, 0.0], [800.0, 0.0]];
        let t = country(line, 0.0, 4.0);
        let s = run_x(&t, arr2(&[[350.0, 0.5], [352.0, 0.5]]), arr2(&[[500.0, 0.5]]), none(), false);
        for (i, a) in s.iter().enumerate() {
            for b in &s[i + 1..] {
                if dot(a.dir, b.dir) > SAME_WAY_COS {
                    assert!((a.xy[0] - b.xy[0]).powi(2) + (a.xy[1] - b.xy[1]).powi(2) >= PANEL_GAP_M * PANEL_GAP_M, "{s:?}");
                }
            }
        }
    }

    /// Czech-like pole settings: priority group 1, warning group 0 (top), the rest below; `alone` rules get -1.
    fn grouped(max: f64, alone: &[u8]) -> [f64; N_POLE] {
        let mut p = [2.0; N_POLE];
        p[SIGNALS as usize] = 0.0;
        p[CROSSING as usize] = 3.0;
        p[NO_ENTRY as usize] = 1.0;
        for &r in alone { p[r as usize] = -1.0; }
        p[RULES] = max;
        p[RULES + 1] = 12.0;
        p
    }

    fn poles_of(s: &[Sign]) -> Vec<Vec<u8>> {
        let mut by: std::collections::BTreeMap<usize, Vec<(usize, u8)>> = Default::default();
        for x in s { by.entry(x.pole).or_default().push((x.slot, x.rule)); }
        by.into_values().map(|mut v| { v.sort(); v.into_iter().map(|(_, r)| r).collect() }).collect()
    }

    fn road800() -> T { country(&[[0.0, 0.0], [800.0, 0.0]], 0.0, 4.0) }

    #[test]
    fn signs_close_together_share_a_pole_ordered_by_group() {
        // signals warned of at 350 and a crossing at 360: one pole, the warning on top
        let s = run_p(&road800(), arr2(&[[360.0, 0.5]]), arr2(&[[500.0, 0.5]]), none(), none4(), grouped(3.0, &[]), false);
        let on: Vec<_> = poles_of(&s).into_iter().filter(|p| p.len() > 1).collect();
        assert_eq!(on, vec![vec![SIGNALS, CROSSING]], "{s:?}");
        let (a, b) = (s.iter().find(|x| x.rule == SIGNALS).unwrap(), s.iter().find(|x| x.rule == CROSSING).unwrap());
        assert_eq!((a.xy, a.dir, a.pole), (b.xy, b.dir, b.pole), "one spot");
        assert_eq!((a.slot, b.slot), (0, 1));
    }

    #[test]
    fn a_rule_that_stands_alone_is_not_put_on_a_pole() {
        let s = run_p(&road800(), arr2(&[[360.0, 0.5]]), arr2(&[[500.0, 0.5]]), none(), none4(), grouped(3.0, &[SIGNALS]), false);
        assert!(poles_of(&s).iter().all(|p| p.len() == 1), "{s:?}");
        // and the two keep apart
        for (i, a) in s.iter().enumerate() {
            for b in &s[i + 1..] {
                if dot(a.dir, b.dir) > SAME_WAY_COS {
                    assert!((a.xy[0] - b.xy[0]).powi(2) + (a.xy[1] - b.xy[1]).powi(2) >= PANEL_GAP_M * PANEL_GAP_M, "{s:?}");
                }
            }
        }
    }

    #[test]
    fn a_pole_holds_no_more_than_the_limit() {
        let marks = arr2(&[[352.0, 0.5], [358.0, 0.5], [364.0, 0.5]]);
        let s = run_p(&road800(), marks, arr2(&[[500.0, 0.5]]), none(), none4(), grouped(2.0, &[]), false);
        assert!(poles_of(&s).iter().all(|p| p.len() <= 2), "{s:?}");
    }

    #[test]
    fn signs_facing_the_other_way_never_share_a_pole() {
        let two_way = road800();
        let s = run_p(&two_way, arr2(&[[360.0, 0.5]]), none(), none(), none4(), grouped(3.0, &[]), false);
        assert_eq!(s.len(), 2, "{s:?}");
        assert!(poles_of(&s).iter().all(|p| p.len() == 1));
    }

    #[test]
    fn a_spot_by_a_speed_limit_sign_is_given_up() {
        let at = arr2(&[[360.0, 0.5]]);
        let free = run_p(&road800(), at.clone(), none(), none(), none4(), grouped(3.0, &[]), false);
        let east = free.iter().find(|x| x.dir[0] > 0.0).unwrap();
        let avoid = arr2(&[[east.xy[0] + 2.0, east.xy[1], 1.0, 0.0]]);
        let s = run_p(&road800(), at, none(), none(), avoid, grouped(3.0, &[]), false);
        assert!(s.iter().all(|x| x.dir[0] < 0.0), "the eastbound sign next to the speed sign is gone: {s:?}");
        assert_eq!(s.len(), 1);
    }

    #[test]
    fn a_tight_bend_gets_three_chevrons_on_its_outside_pointing_the_way_it_turns() {
        // east, a left arc of radius 25 m (90 degrees, centre (-25, 25)), north
        let mut line: Vec<[f64; 2]> = vec![[-300.0, 0.0]];
        line.extend(arc(-25.0, 25.0, 25.0, -90.0, 0.0));
        line.push([0.0, 300.0]);
        let s = run(&country(&line, 0.0, 4.0), none(), none(), false);
        let (l, r) = (of(&s, CHEVRON_LEFT), of(&s, CHEVRON_RIGHT));
        assert_eq!((l.len(), r.len()), (3, 3), "{s:?}");
        for c in l.iter().chain(r.iter()) {
            let d = ((c.xy[0] + 25.0).powi(2) + (c.xy[1] - 25.0).powi(2)).sqrt();
            assert!(d > 25.0 + 4.0, "on the outside of the bend: {c:?} is {d} m from its centre");
        }
    }

    #[test]
    fn a_bend_a_curve_sign_is_enough_for_has_no_chevrons() {
        let mut line: Vec<[f64; 2]> = vec![[-300.0, 0.0]];
        line.extend(arc(-50.0, 50.0, 50.0, -90.0, 0.0));
        line.push([0.0, 300.0]);
        let s = run(&country(&line, 0.0, 4.0), none(), none(), false);
        assert!(of(&s, CHEVRON_LEFT).is_empty() && of(&s, CHEVRON_RIGHT).is_empty(), "{s:?}");
    }

    fn t_main(urban: f64, side_rank: f64) -> T {
        // an east-west main road (rank 4) through (0, 0) and a road joining it from the north
        roads_x(&[(&[[-1000.0, 0.0], [0.0, 0.0]], 0.0, 4.0, [1, 2], urban, 0, 0.0), (&[[0.0, 0.0], [1000.0, 0.0]], 0.0, 4.0, [2, 3], urban, 0, 0.0),
                  (&[[0.0, 100.0], [0.0, 0.0]], 0.0, side_rank, [4, 2], urban, 0, 0.0)])
    }

    #[test]
    fn a_side_road_is_warned_of_on_the_main_road_on_the_side_it_joins_from() {
        let s = run(&t_main(0.0, 2.0), none(), none(), false);
        let (l, r) = (of(&s, SIDE_LEFT), of(&s, SIDE_RIGHT));
        assert_eq!((l.len(), r.len()), (1, 1), "{s:?}");
        assert!(l[0].dir[0] > 0.99 && l[0].xy[0] < -100.0, "going east it joins from the left: {:?}", l[0]);
        assert!(r[0].dir[0] < -0.99 && r[0].xy[0] > 100.0, "going west from the right: {:?}", r[0]);
        let town = run(&t_main(1.0, 2.0), none(), none(), false);
        assert!(town.iter().all(|x| x.rule != SIDE_LEFT && x.rule != SIDE_RIGHT), "in town: none");
        let main = run(&t_main(0.0, 4.0), none(), none(), false);
        assert!(main.iter().all(|x| x.rule != SIDE_LEFT && x.rule != SIDE_RIGHT), "a road as big as the main road: none");
    }

    #[test]
    fn give_way_and_stop_stand_where_the_map_has_them_facing_the_way_it_states() {
        let t = road800();
        let s = with_pois(&t, arr2(&[[300.0, 0.0, 0.0, 1.0, 0.0], [500.0, 0.0, 1.0, -1.0, 0.0]]), false);
        let (g, p) = (of(&s, GIVE_WAY), of(&s, STOP));
        assert_eq!((g.len(), p.len()), (1, 1), "{s:?}");
        assert!(g[0].dir[0] > 0.99 && (g[0].xy[0] - 298.5).abs() < 6.0, "{:?}", g[0]);
        assert!(p[0].dir[0] < -0.99, "{:?}", p[0]);
        assert!(with_pois(&t, arr2(&[[300.0, 0.0, 0.0, 0.0, 0.0]]), false).is_empty(), "no stated way on a two-way road: none");
    }

    #[test]
    fn a_node_at_a_junction_belongs_to_the_minor_road() {
        // the node lies at the crossing of a main road and a rank 1 street: the sign goes to the street
        let t = roads_x(&[(&[[-500.0, 0.0], [500.0, 0.0]], 0.0, 4.0, [1, 2], 0.0, 0, 0.0), (&[[0.0, 200.0], [0.0, 0.0]], 1.0, 1.0, [3, 4], 0.0, 0, 0.0)]);
        let s = with_pois(&t, arr2(&[[0.0, 1.0, 0.0, 0.0, 0.0]]), false);
        let g = of(&s, GIVE_WAY);
        assert_eq!(g.len(), 1, "{s:?}");
        assert!(g[0].dir[1] < -0.99, "facing the traffic coming south along the street: {:?}", g[0]);
    }

    #[test]
    fn a_narrowing_road_is_warned_of_before_it() {
        let mut t = roads_x(&[(&[[-1000.0, 0.0], [0.0, 0.0]], 0.0, 3.0, [1, 2], 0.0, 0, 0.0), (&[[0.0, 0.0], [1000.0, 0.0]], 0.0, 3.0, [2, 3], 0.0, 0, 0.0)]);
        t.meta[[1, 0]] = 2.5;
        let s = run(&t, none(), none(), false);
        let n = of(&s, NARROW);
        assert_eq!(n.len(), 1, "only the way it narrows: {s:?}");
        assert!(n[0].dir[0] > 0.99 && n[0].xy[0] < -100.0, "{:?}", n[0]);
        t.meta[[1, 0]] = 3.8;
        assert!(of(&run(&t, none(), none(), false), NARROW).is_empty(), "a hair narrower is the same road");
    }

    #[test]
    fn a_tunnel_is_signed_before_its_portal() {
        let t = road800();
        let s = with_pois(&t, arr2(&[[500.0, 0.0, 4.0, 1.0, 0.0], [700.0, 0.0, 4.0, -1.0, 0.0]]), false);
        let u = of(&s, TUNNEL);
        assert_eq!(u.len(), 2, "{s:?}");
        assert!(u.iter().any(|x| x.dir[0] > 0.99 && x.xy[0] < 495.0) && u.iter().any(|x| x.dir[0] < -0.99 && x.xy[0] > 705.0), "{u:?}");
    }

    #[test]
    fn a_bus_stop_is_signed_for_the_traffic_on_its_side() {
        let t = road800();
        let at = arr2(&[[300.0, -6.0, 2.0, 0.0, 0.0]]);
        let s = of(&with_pois(&t, at.clone(), false), BUS_STOP).iter().map(|x| x.dir[0]).collect::<Vec<_>>();
        assert_eq!(s.len(), 1);
        assert!(s[0] > 0.99, "south of the road: the traffic going east (right-hand): {s:?}");
        let l = of(&with_pois(&t, at, true), BUS_STOP).iter().map(|x| x.dir[0]).collect::<Vec<_>>();
        assert!(l.len() == 1 && l[0] < -0.99, "left-hand traffic: the other way: {l:?}");
        assert!(with_pois(&t, arr2(&[[300.0, -60.0, 2.0, 0.0, 0.0]]), false).is_empty(), "a stop far from every road: none");
    }

    #[test]
    fn pedestrian_zones_and_cycle_paths_are_signed_where_traffic_enters_them() {
        // an ordinary road, then a pedestrian zone straight on; a cycle path leaving to the side
        let t = roads_x(&[(&[[-300.0, 0.0], [0.0, 0.0]], 0.0, 3.0, [1, 2], 0.0, 0, 0.0),
                          (&[[0.0, 0.0], [200.0, 0.0]], 0.0, 0.0, [2, 3], 0.0, FLAG_FOOT, 0.0)]);
        let s = run(&t, none(), none(), false);
        let p = of(&s, PED_ZONE);
        assert_eq!(p.len(), 1, "{s:?}");
        assert!(p[0].dir[0] > 0.99 && p[0].xy[0] > 0.0, "{:?}", p[0]);
        let straight = roads_x(&[(&[[-300.0, 0.0], [0.0, 0.0]], 0.0, 3.0, [1, 2], 0.0, 0, 0.0),
                                 (&[[0.0, 0.0], [200.0, 0.0]], 0.0, 0.0, [2, 3], 0.0, FLAG_CYCLE, 0.0)]);
        assert_eq!(of(&run(&straight, none(), none(), false), BICYCLE).len(), 1);
        let side = roads_x(&[(&[[-300.0, 0.0], [0.0, 0.0]], 0.0, 3.0, [1, 2], 0.0, 0, 0.0),
                             (&[[0.0, 0.0], [0.0, 200.0]], 0.0, 0.0, [2, 3], 0.0, FLAG_CYCLE, 0.0)]);
        assert!(of(&run(&side, none(), none(), false), BICYCLE).is_empty(), "a path branching off is not announced");
    }

    #[test]
    fn outlines_are_counter_clockwise_and_in_the_cell() {
        for shape in 0..7 {
            let o = outline(shape);
            let area: f64 = (0..o.len()).map(|i| {
                let (a, b) = (o[i], o[(i + 1) % o.len()]);
                a[0] * b[1] - b[0] * a[1]
            }).sum();
            assert!(area > 0.0, "shape {shape}");
            assert!(o.iter().all(|p| p[0].abs() <= 1.0 && p[1].abs() <= 1.0), "shape {shape}");
        }
    }
}
