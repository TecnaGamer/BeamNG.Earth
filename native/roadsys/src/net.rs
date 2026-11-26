//! G1 NETWORK + G2 SUPPORT: strokes -> carriageways -> lane slots with width functions w_k(s), events (add, drop,
//! diverge, merge, bay), gores from geometry; support intervals and the crossing graph.
//!
//! v3's measured rules are kept as they are in native/earthfast/src/roadshape/lanes.rs (ported, not re-tuned): the
//! TaperRule table (lanegraph.TAPER_TABLE, plus a JP row: the default values with left-hand traffic, a PT3
//! addition), the fork test (fork_roles), the stroke walk (walk), the at-grade rule and the divided-crossing rule
//! (at_grade_end), the gore from geometry (the branch's mapped line projected on the through path; the theoretical
//! gore where it stays outside the near through edge + half the branch's lanes), the painted gore and nose, the
//! exit / entrance / taper-type tapers and parallel lanes, the add / drop side rule (add_side: placement, turn lanes,
//! bays, feed from a ramp within FEED_M, the outer side) and the MUTCD lane drop taper. What is new: every road gets
//! a lane graph (not only near events), each event's lanes are slots of one carriageway with width programs, so the
//! strips of G4 are built from one ordered set of boundaries (no pieces to trim).
use crate::geom::*;
use crate::io::{Place, Seg};
use crate::xsec::{self, Lay, SideW};

pub const T_LANE_W: usize = 0;
pub const T_EXIT_K: usize = 1;
pub const T_EXIT_MIN: usize = 2;
pub const T_DEC_PAR: usize = 3;
pub const T_ACC_PAR: usize = 4;
pub const T_ENTRY_K: usize = 5;
pub const T_ENTRY_MIN: usize = 6;
pub const T_DIV_K: usize = 7;
pub const T_GORE_1: usize = 8;
pub const T_GORE_X: usize = 9;
pub const T_DROP_K: usize = 10;
pub const T_ADD_PLACE: usize = 11;
pub const T_LHT: usize = 12;
pub const T_TAPER_MERGE: usize = 13;
pub const T_GORE_IN: usize = 14;
pub const TAPER: [[f64; 15]; 7] = [
    [3.65, 21.0, 76.0, 150.0, 160.0, 30.0, 91.0, 20.0, 100.0, 70.0, 0.0, 0.2, 0.0, 80.0, 50.0],
    [3.65, 21.0, 76.0, 150.0, 160.0, 30.0, 91.0, 20.0, 100.0, 70.0, 0.0, 0.2, 0.0, 80.0, 50.0],
    [3.75, 0.0, 50.0, 250.0, 250.0, 0.0, 60.0, 20.0, 50.0, 30.0, 0.0, 0.2, 0.0, 60.0, 30.0],
    [3.65, 25.0, 75.0, 200.0, 230.0, 0.0, 75.0, 46.0, 120.0, 30.0, 0.0, 0.2, 1.0, 75.0, 115.0],
    [3.5, 22.0, 75.0, 200.0, 200.0, 25.0, 75.0, 20.0, 95.0, 40.0, 0.0, 0.2, 0.0, 75.0, 50.0],
    [3.5, 30.0, 100.0, 140.0, 120.0, 34.0, 100.0, 20.0, 130.0, 40.0, 0.0, 0.2, 0.0, 80.0, 30.0],
    [3.5, 21.0, 76.0, 150.0, 160.0, 30.0, 91.0, 20.0, 100.0, 70.0, 0.0, 0.2, 1.0, 80.0, 50.0],
];
// lanegraph.LANE_PARAMS
pub const REACH_M: f64 = 900.0;
pub const DIR_M: f64 = 20.0;
pub const FORK_COS: f64 = 0.766044443118978;   // cos 40 deg (dmath.cos, as a literal)
pub const STEP_M: f64 = 1.0;
pub const SHIFT_M: f64 = 40.0;
pub const FEED_M: f64 = 600.0;
pub const NOSE_W_M: f64 = 4.0;
pub const AT_GRADE_M: f64 = 60.0;
pub const MARGIN_M: f64 = 20.0;
const NOSE_HOLD_M: f64 = 20.0;
const MAX_PIECES: usize = 64;
const CLS_TRUNK: i64 = 2;
const CROSS_COS: f64 = 0.7071067811865476;
const ALONG_NEAR_M: f64 = 60.0;   // (M3) m from a lower road's own structure at the upper's level within which no along floor holds
const SHALLOW_SIN: f64 = 0.5735764363510461;   // (M3) sin 35 deg: a crossing this shallow may be two decks side by side
/// m a floor adds under the clearance (M2: the deck today's builder draws: its slab and girders, roads.DECK_FULL_DEPTH
/// 2.2 m, + roads.LEVEL_MARGIN_M; M3's kits will bring each deck's own depth)
pub const DECK_FULL_M: f64 = 2.3;
pub const LEVEL_MARGIN_M: f64 = 0.1;
pub const TODAY_HEADROOM_M: f64 = 6.15;  // (roads.MIN_BRIDGE_HEADROOM_M 6.0 + BRIDGE_HEADROOM_MARGIN_M 0.15)    // (roads.LEVEL_MARGIN_M)
const MPH: f64 = 1.609344;

pub const K_LANE: u8 = 0;
pub const K_SHOULDER: u8 = 1;
pub const K_GORE: u8 = 2;
pub const K_ZONE: u8 = 3;

pub const SUP_GROUND: u8 = 0;
pub const SUP_STRUCT: u8 = 1;
pub const SUP_TUNNEL: u8 = 2;
/// (T1) a crossing over a tunnel: its floor's parts (the record's lids: roads.TUNNEL_LOW_H, roads.TUNNEL_SOIL_M)
pub const TUNNEL_LIGHTS_M: f64 = 0.3;
pub const TUNNEL_LID_MIN_M: f64 = 3.6;
pub const TUNNEL_ROOF_M: f64 = 1.0;

pub struct Edge { pub seg: usize, pub va: usize, pub vb: usize, pub na: usize, pub nb: usize, pub len: f64 }

#[derive(Default)]
pub struct Node { pub p: P2, pub arms: Vec<(usize, bool)> }

/// A width (or offset) program: w0, then per step (a, b, dv) a quintic blend of dv over [a, b] (a step at a if b == a).
#[derive(Clone, Debug, Default)]
pub struct Prog { pub w0: f64, pub steps: Vec<(f64, f64, f64)> }

impl Prog {
    pub fn at(&self, s: f64) -> f64 {
        let mut v = self.w0;
        for &(a, b, dv) in &self.steps {
            if s < a { continue; }
            if b <= a || s >= b { v += dv; } else { v += dv * quintic((s - a) / (b - a)); }
        }
        v
    }
}

#[derive(Clone, Debug)]
pub struct Slot { pub kind: u8, pub group: u8, pub prog: Prog, pub alive: (f64, f64), pub cap: Vec<(f64, f64)> }

impl Slot {
    pub fn w(&self, s: f64) -> f64 {
        if s < self.alive.0 || s > self.alive.1 { return 0.0; }
        let mut v = self.prog.at(s);
        if !self.cap.is_empty() {
            // a measured cap (the gap to a parallel carriageway), linear between its samples
            let c = &self.cap;
            let k = c.partition_point(|x| x.0 <= s);
            let cv = if k == 0 { c[0].1 } else if k >= c.len() { c[c.len() - 1].1 } else {
                let (a, b) = (c[k - 1], c[k]);
                let u = if b.0 > a.0 { (s - a.0) / (b.0 - a.0) } else { 0.0 };
                a.1 + (b.1 - a.1) * u
            };
            if cv < v { v = cv; }
        }
        if v > 0.0 { v } else { 0.0 }
    }
}

#[derive(Clone, Copy, Debug)]
pub struct Part { pub edge: usize, pub fwd: bool, pub s0: f64, pub s1: f64 }

#[derive(Clone, Copy, Debug)]
pub struct Sup { pub s0: f64, pub s1: f64, pub kind: u8, pub sid: u32, pub level: f64, pub depth: f64 }

/// A branch stroke's first (or last) row taken from a trunk stroke's row: trunk slots [k0, k1] at station s (their
/// left boundary of k0 to the right boundary of k1) are this stroke's boundaries from its first lane-ward one.
#[derive(Clone, Copy, Debug)]
pub struct Snap { pub trunk: usize, pub s: f64, pub k0: usize, pub k1: usize, pub tb: f64, pub joint: bool }

pub struct Stroke {
    pub uid: u64,                  // stable id: its first part's segment and vertex (never its index in a build)
    pub parts: Vec<Part>,
    pub line: Line,
    pub n0: usize,
    pub n1: usize,
    pub oneway: bool,
    pub lht: bool,
    pub slots: Vec<Slot>,          // left to right
    pub anchor_l: Prog,
    pub anchor_r: Prog,
    pub right_windows: Vec<(f64, f64)>,
    pub stations: Vec<f64>,        // stations rows must include
    pub dense: Vec<(f64, f64)>,    // intervals with rows every 2 m
    pub sup: Vec<Sup>,
    pub cls: i64,
    pub cls_s: String,
    pub kmh: f64,
    pub region: usize,
    pub snap0: Option<Snap>,
    pub snap1: Option<Snap>,
    pub cut0: f64,
    pub cut1: f64,
    pub absorbed: bool,
    pub lane_scale: Vec<(f64, f64)>,   // (station, factor): lane widths from the measured gap to a parallel carriageway
    pub cap_nbrs: Vec<usize>,          // the parallel carriageways whose gap capped it (part.rs caps): its read list
    pub reach: f64,                    // m round it its lane program reads (the lane graph's walks; product.rs halos)
    pub over: Vec<(usize, f64, f64, u8)>, // (BNE_ROADSYS_XSEC) (slot, s0, s1, what): a bus lane / unpaved shoulder face kind
                                       // (xsec::K_BUS, K_SOFT) or an advisory cycle lane's dashed line (OVER_ADV)
    pub area: Vec<crate::areas::Knot>, // (BNE_ROADSYS_AREAS) its mapped road areas' program: lane factor, outer shoulders,
                                       // shift, strip factor (the cross-section's strips, BNE_ROADSYS_XSEC)
    pub skews: Vec<(f64, f64)>,        // (627) (station, slope): a skewed end row there, its boundary at offset t on
                                       // station s + t * slope (deck.rs set_skews; part.rs rows)
}

/// Stroke.over's mark for an advisory cycle lane (markings only, never a face kind).
pub const OVER_ADV: u8 = 0x40;

/// (BNE_ROADSYS_XSEC) a cross-section strip slot between the lanes and a shoulder: parking, a cycle lane, a buffer.
pub fn is_strip(kind: u8) -> bool { kind == xsec::K_PARK || kind == xsec::K_CYCLE || kind == xsec::K_BUFFER }

impl Stroke {
    pub fn len(&self) -> f64 { self.line.len() }
    /// (BNE_ROADSYS_XSEC) the override `what` of slot k at station s, if any.
    pub fn over_at(&self, k: usize, s: f64, what: u8) -> bool {
        self.over.iter().any(|o| o.0 == k && o.3 == what && s >= o.1 && s <= o.2)
    }
    /// The face kind of slot k at station s (its slot's, or an override's).
    pub fn face_kind(&self, k: usize, s: f64) -> u8 {
        for o in &self.over { if o.0 == k && o.3 < OVER_ADV && s >= o.1 && s <= o.2 { return o.3; } }
        self.slots[k].kind
    }
    pub fn support_at(&self, s: f64) -> Sup {
        for u in &self.sup { if s >= u.s0 && s <= u.s1 { return *u; } }
        Sup { s0: 0.0, s1: self.len(), kind: SUP_GROUND, sid: 0, level: 0.0, depth: 0.0 }
    }
    /// The row frame at station s (PT3 change 3, no folds on tight curves): the point on the mapped line, the unit
    /// left normal of the line smoothed over +-W (W = the carriageway's half width, 4..15 m: a polyline corner turns
    /// the rows over 2 W instead of at one vertex) and the smoothed line's signed curvature (+ turning left).
    pub fn row_frame(&self, s: f64, half_w: f64) -> (P2, P2, f64) {
        let len = self.len();
        let w = clampf(half_w, 4.0, 15.0);
        let (p, t0) = self.line.at(s);
        let (a, _) = self.line.at((s - w).max(0.0));
        let (b, _) = self.line.at((s + w).min(len));
        let t = unit(sub(b, a)).unwrap_or(t0);
        let k = match (unit(sub(p, a)), unit(sub(b, p))) {
            (Some(u), Some(v)) => { let d = norm(sub(b, a)); if d > 1e-6 { 2.0 * cross(u, v) / d } else { 0.0 } }
            _ => 0.0,
        };
        (p, [-t[1], t[0]], k)
    }

    /// The row at station s for offsets off: (point, normal) of the smoothed frame and the offsets used, those on
    /// the inner side of a curve held to 0.9 of its radius (none passes the curve's centre) and kept in order.
    pub fn row_offsets(&self, s: f64, off: &[f64], ts: &mut Vec<f64>) -> (P2, P2) {
        let hw = off.first().copied().unwrap_or(0.0).max(-off.last().copied().unwrap_or(0.0));
        let (p, n, k) = self.row_frame(s, hw);
        let lim = if absf(k) > 1e-9 { 0.9 / absf(k) } else { f64::INFINITY };
        ts.clear();
        ts.extend_from_slice(off);
        if k > 0.0 { for t in ts.iter_mut() { if *t > lim { *t = lim; } } }
        if k < 0.0 { for t in ts.iter_mut() { if *t < -lim { *t = -lim; } } }
        order_offsets(off, ts);
        (p, n)
    }

    /// The row's boundary points at station s (row_offsets).
    pub fn row_points(&self, s: f64, off: &[f64], out: &mut Vec<P2>) {
        let mut ts = Vec::new();
        let (p, n) = self.row_offsets(s, off, &mut ts);
        out.clear();
        for &t in &ts { out.push(add(p, scale(n, t))); }
    }

    /// Boundary offsets t_0 >= t_1 >= ... >= t_S (m, + left of the stroke) at station s (with its mapped areas'
    /// program, areas.rs, where it has one).
    pub fn offsets(&self, s: f64, out: &mut Vec<f64>) {
        if self.area.is_empty() { self.offsets_raw(s, None, out); } else { self.offsets_raw(s, Some(crate::areas::knot_at(&self.area, s)), out); }
    }

    /// offsets() with the areas' parameters area ([lane factor, left / right outer shoulder extra, shift, strip factor])
    /// or none. (The strip factor scales the cross-section's parking, cycle lanes and buffers, BNE_ROADSYS_XSEC: a
    /// stroke without them is untouched by it.)
    pub fn offsets_raw(&self, s: f64, area: Option<[f64; 5]>, out: &mut Vec<f64>) {
        out.clear();
        let n = self.slots.len();
        let mut ws: Vec<f64> = self.slots.iter().map(|k| k.w(s)).collect();
        if let Some(a) = area {
            for k in 0..n { if self.slots[k].kind == K_LANE { ws[k] *= a[0]; } }
            if a[4] != 1.0 { for k in 0..n { if is_strip(self.slots[k].kind) { ws[k] *= a[4]; } } }
            if let Some(k) = (0..n).find(|&k| ws[k] > 1e-6) { if self.slots[k].kind == K_SHOULDER { ws[k] = (ws[k] + a[1]).max(0.0); } }
            if let Some(k) = (0..n).rev().find(|&k| ws[k] > 1e-6) { if self.slots[k].kind == K_SHOULDER { ws[k] = (ws[k] + a[2]).max(0.0); } }
        }
        let dt = area.map(|a| a[3]).unwrap_or(0.0);
        self.offsets_ws(s, &ws, out);
        if dt != 0.0 { for t in out.iter_mut() { *t += dt; } }
    }

    fn offsets_ws(&self, s: f64, ws0: &[f64], out: &mut Vec<f64>) {
        let n = self.slots.len();
        let mut ws: Vec<f64> = ws0.to_vec();
        if !self.lane_scale.is_empty() {
            let c = &self.lane_scale;
            let k = c.partition_point(|x| x.0 <= s);
            let f = if k == 0 { c[0].1 } else if k >= c.len() { c[c.len() - 1].1 } else {
                let (a, b) = (c[k - 1], c[k]);
                let u = if b.0 > a.0 { (s - a.0) / (b.0 - a.0) } else { 0.0 };
                a.1 + (b.1 - a.1) * u
            };
            for k in 0..n { if self.slots[k].kind == K_LANE { ws[k] *= f; } }
        }
        if !self.oneway {
            // centred on the mapped line: the lanes (both directions) about it, shoulders outside
            let mut wl = 0.0;
            for k in 0..n { if self.slots[k].kind != K_SHOULDER { wl += ws[k]; } }
            let first_lane = (0..n).find(|&k| self.slots[k].kind != K_SHOULDER).unwrap_or(0);
            let mut t0 = wl / 2.0;
            for k in 0..first_lane { t0 += ws[k]; }
            out.push(t0);
            for k in 0..n { let v = out[k] - ws[k]; out.push(v); }
            return;
        }
        let right = self.right_windows.iter().any(|&(a, b)| s >= a && s <= b);
        if !right {
            let first_lane = (0..n).find(|&k| self.slots[k].kind == K_LANE).unwrap_or(0);
            let mut t0 = self.anchor_l.at(s);
            for k in 0..first_lane { t0 += ws[k]; }
            out.push(t0);
            for k in 0..n { let v = out[k] - ws[k]; out.push(v); }
        } else {
            let last_lane = (0..n).rev().find(|&k| self.slots[k].kind == K_LANE).unwrap_or(n - 1);
            let mut tr = self.anchor_r.at(s);
            for k in (last_lane + 1)..n { tr -= ws[k]; }
            out.resize(n + 1, 0.0);
            out[n] = tr;
            for k in (0..n).rev() { out[k] = out[k + 1] + ws[k]; }
        }
    }
}

/// Offsets ts (moved toward the line from orig) put back in order t_0 >= t_1 >= ...: a boundary pulled in on the
/// left pulls the ones inside it along (lanes narrow from the outside, never cross); likewise on the right.
pub fn order_offsets(orig: &[f64], ts: &mut [f64]) {
    let n = ts.len();
    let left = |i: usize| orig[i] > 0.0;
    for i in 1..n { if left(i) && ts[i] > ts[i - 1] { ts[i] = ts[i - 1]; } }
    for i in (0..n.saturating_sub(1)).rev() { if !left(i) && ts[i] < ts[i + 1] { ts[i] = ts[i + 1]; } }
}

pub struct Fork {
    pub node: usize,
    pub trunk: (usize, bool),
    pub through: (usize, bool),
    pub branch: (usize, bool),
    pub diverge: bool,
    pub right: bool,
    pub lb: i64,
    pub lm: i64,
    pub w: f64,
    pub s_g: f64,
    pub s_n: f64,
    pub tb_n: f64,          // the branch's own arc from the node at the nose (from the gore-from-geometry samples)
    pub sep: Vec<(f64, f64)>, // (through arc from the node, the branch line's offset beyond the threshold) every ~5 m
    pub taper0: f64,
    pub taper1: f64,
    pub typ: &'static str,
    pub evidence: &'static str,
    pub region: usize,
}

/// A grade separation: strokes a and b at stations sa / sb, the upper one and its floor (clearance + deck depth over the
/// lower road); along: a structure running above a lower road (a floor every 10 m), stacked: the lower one is a
/// structure too (a double deck).
pub struct Crossing { pub a: usize, pub sa: f64, pub b: usize, pub sb: f64, pub p: P2, pub upper: usize, pub floor: f64, pub along: bool, pub stacked: bool }

/// (M5) A structure of stroke si over a railway line at station s, point p: the deck's road at least floor (the line's
/// clearance + the structure's depth) over the ground at p.
#[derive(Clone, Copy, Debug)]
pub struct RailFloor { pub si: usize, pub s: f64, pub p: P2, pub floor: f64 }

#[derive(Default, Debug, Clone)]
pub struct NetStats {
    pub segs: usize,
    pub nodes: usize,
    pub edges: usize,
    pub strokes: usize,
    pub forks: usize,
    pub forks_at_grade: usize,
    pub forks_no_sep: usize,
    pub adds: usize,
    pub drops: usize,
    pub structures: usize,
    pub tunnels: usize,
    pub crossings: usize,
    pub rail_floors: usize,
    pub unbridged: usize,
    pub nose_beyond: usize,
    pub along: usize,
    pub cuts: usize,
    pub cut_unjoined: usize,
    pub xsec_segs: usize,              // (BNE_ROADSYS_XSEC) segments with cross-section tags
    pub xsec_shaped: usize,            // ... whose tags change their strips or lanes
    pub xsec_width: usize,             // ... whose lanes come from a mapped width
    pub xsec_reject: usize,            // ... whose mapped width gave no plausible lanes (today's lanes kept)
    pub xsec_fork_skip: usize,         // strokes with a fork event whose strips stay empty
}

/// Build options. cut_grid: strokes are cut at a node past each line of a fixed grid of this size (m, in the frame;
/// 0: never), so a motorway's lane program and Tier A profile read a bounded stretch (the read list of a product is
/// its component's, never the whole road's); cut_offset: the grid's lines at offset + k * cut_grid.
#[derive(Clone, Copy, Debug)]
/// flat_struct (M4, deck.rs): no crossfall on structure faces (today's decks are flat across: the terrain and the
/// hand-over were made for them).
/// areas (BNE_ROADSYS_AREAS=on): the place's mapped road areas are the carriageway edges' soft targets (areas::fit).
/// tunnels (T1, BNE_ROADSYS_TUNNELS=on): the profile squares make the tunnel record (tunnel.rs) with their profiles.
pub struct Opts { pub cut_grid: f64, pub cut_offset: f64, pub flat_struct: bool, pub areas: bool, pub tunnels: bool }

impl Default for Opts { fn default() -> Opts { Opts { cut_grid: 4000.0, cut_offset: 2000.0, flat_struct: false, areas: false, tunnels: false } } }

pub struct Net {
    pub opts: Opts,
    pub segs: Vec<Seg>,
    pub node_ids: Vec<String>,          // node -> connector id (synthetic ends: "~<fid>#0" / "#1")
    pub cut: Vec<bool>,                 // node -> a stroke cut (Opts.cut_grid)
    pub region: Vec<usize>,
    pub edges: Vec<Edge>,
    pub nodes: Vec<Node>,
    pub forks: Vec<Fork>,
    pub fork_at: Vec<Option<usize>>,    // node -> fork index (events only)
    pub strokes: Vec<Stroke>,
    pub edge_stroke: Vec<(usize, usize)>, // edge -> (stroke, part)
    pub crossings: Vec<Crossing>,
    pub rail_floors: Vec<RailFloor>,    // (M5) a structure stroke over a railway line: (stroke, station, point, floor)
    pub rails: Vec<(Vec<P2>, f64)>,     // (M5) the railway lines round the read (place.rails): (points, clearance m)
    pub stats: NetStats,
    pub xsec_on: bool,                  // (BNE_ROADSYS_XSEC) the cross-section model: every stroke gets its strip slots
    pub lays: Vec<Option<Lay>>,         // seg -> its cross-section's strips (sides in the segment's direction)
    pub xs_tags: Vec<u16>,              // seg -> what its cross-section took from tags (xsec::T_*; 0: all inferred)
    pub area_fit: crate::areas::FitStats,
}

// ---------------------------------------------------------------------------------------------- v3 helpers (ported)

fn lanes_dir(s: &Seg, fwd: bool) -> i64 {
    if s.dir != 0 { return s.lanes; }
    let (a, b) = if fwd { (s.lfw, s.lbw) } else { (s.lbw, s.lfw) };
    if a >= 1 { return a; }
    if s.lanes >= 2 && b < 1 { return s.lanes / 2 + if fwd { s.lanes % 2 } else { 0 }; }
    if s.lanes >= 1 && b >= 1 && s.lanes > b { return s.lanes - b; }
    -1
}

/// Lanes in one direction with the painted-count fallback (roads.lane_layout's count), at least 1.
fn lanes_eff(s: &Seg, fwd: bool) -> i64 {
    let v = lanes_dir(s, fwd);
    if v >= 1 { return v; }
    if s.dir != 0 { return if s.lanes >= 1 { s.lanes } else { s.inferred.max(1) }; }
    let n = if s.lanes >= 1 { s.lanes } else { s.inferred };
    if n >= 2 { if fwd { n / 2 + n % 2 } else { n / 2 } } else { 1 }
}

fn turn_dir(s: &Seg, fwd: bool) -> &str { if s.dir != 0 || fwd { &s.turn } else { &s.turn_bw } }

fn flow_of(s: &Seg, fwd: bool) -> i64 {
    match (s.dir, fwd) { (1, true) | (-1, false) => 1, (1, false) | (-1, true) => -1, _ => 0 }
}

pub fn exit_lanes(turn: &str, left: bool) -> i64 {
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

fn mutcd_m(w: f64, kmh: f64) -> f64 {
    let s = kmh / MPH;
    if s >= 45.0 { w * s } else { w * s * s / 60.0 }
}

fn speed_share(kmh: f64) -> f64 { clampf(kmh / 80.0, 0.5, 1.0) }

#[derive(Clone, Copy)]
#[allow(dead_code)]
struct Piece { edge: usize, fwd: bool, s0: f64, s1: f64, end_node: Option<usize> }

struct Path { line: Line, pieces: Vec<Piece> }

impl Path {
    fn len(&self) -> f64 { self.line.len() }
}

impl Net {
    pub fn t(&self, region: usize, f: usize) -> f64 { TAPER[region][f] }

    fn seg_of_edge(&self, e: usize) -> &Seg { &self.segs[self.edges[e].seg] }

    /// Lane width on edge e: the region's for motorway / trunk, else the road's own (v3 Ctx::lane_w), sane bounds;
    /// (BNE_ROADSYS_XSEC) its cross-section's where a mapped width gives them.
    fn lane_w(&self, e: usize) -> f64 {
        let g = self.edges[e].seg;
        if let Some(Some(l)) = self.lays.get(g) { if l.lane_w > 0.0 { return clampf(l.lane_w, 2.2, 5.0); } }
        self.lane_w_today(g)
    }

    fn lane_w_today(&self, g: usize) -> f64 { lane_w_of(&self.segs[g], self.region[g]) }
}

/// Today's lane width of segment s in TAPER row region (Net::lane_w without a cross-section).
fn lane_w_of(s: &Seg, region: usize) -> f64 {
    {
        let w = if s.cls >= 1 && s.cls <= CLS_TRUNK { TAPER[region][T_LANE_W] } else {
            let n = if s.dir != 0 { lanes_eff(s, true) } else { lanes_eff(s, true) + lanes_eff(s, false) };
            if s.lanes < 1 && s.dir == 0 && s.inferred <= 1 { s.drive / 2.0 } else if s.lane_w > 0.0 { s.lane_w } else { s.drive / n.max(1) as f64 }
        };
        clampf(w, 2.2, 5.0)
    }
}

impl Net {
    /// Shoulder / margin width on the (right, left) side of edge e's carriageway.
    fn shoulder_w(&self, e: usize) -> (f64, f64) { shoulder_of(self.seg_of_edge(e)) }

    /// (BNE_ROADSYS_XSEC) part p's strips in stroke terms (left, right), None without a cross-section.
    fn xs_part(&self, p: &Part) -> Option<(SideW, SideW)> {
        let l = self.lays[self.edges[p.edge].seg].as_ref()?;
        Some(if p.fwd { (l.left, l.right) } else { (l.right, l.left) })
    }
}

fn shoulder_of(s: &Seg) -> (f64, f64) {
    {
        let street = clampf((s.pave - s.drive) / 2.0, 0.3, 2.6);
        if s.cls == 1 && !s.link { return (3.0, 1.0); }
        if s.urban && !(s.cls == 1) { return (street, street); }
        if s.cls >= 1 && s.cls <= CLS_TRUNK { return if s.link { (2.4, 1.0) } else { (2.5, 1.0) }; }
        if s.link { return (1.5, 0.6); }
        let m = clampf((s.pave - s.drive) / 2.0, 0.3, 2.6);
        (m, m)
    }
}

impl Net {
    /// v3 arms_of: arms of node n in the fork classes (cls > 0).
    fn arms_fc(&self, n: usize) -> Vec<(usize, bool)> {
        self.nodes[n].arms.iter().copied().filter(|&(e, _)| self.seg_of_edge(e).cls > 0).collect()
    }

    fn arm_flow(&self, a: (usize, bool)) -> i64 { flow_of(self.seg_of_edge(a.0), a.1) }

    /// The arm's other node.
    #[allow(dead_code)]
    fn arm_far(&self, a: (usize, bool)) -> usize { let e = &self.edges[a.0]; if a.1 { e.nb } else { e.na } }

    /// The points of arm a leaving its node (along its edge, then on along its segment's points).
    fn arm_dir(&self, a: (usize, bool), dir_m: f64) -> Option<P2> {
        let e = &self.edges[a.0];
        let seg = &self.segs[e.seg];
        let v = if a.1 { e.va } else { e.vb };
        let p0 = seg.pts[v];
        let mut i = v;
        let mut q = p0;
        let mut got = 0.0;
        loop {
            let j = if a.1 { i + 1 } else { i.wrapping_sub(1) };
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

    /// v3 fork_roles: (arms, trunk, through, branch, diverge).
    fn fork_roles(&self, n: usize) -> Option<(Vec<(usize, bool)>, usize, usize, usize, bool)> {
        let arms = self.arms_fc(n);
        if arms.len() != 3 { return None; }
        if arms.iter().any(|&a| self.arm_flow(a) == 0) { return None; }
        let n_out = arms.iter().filter(|&&a| self.arm_flow(a) > 0).count();
        let diverge = match n_out { 2 => true, 1 => false, _ => return None };
        let ti = arms.iter().position(|&a| (self.arm_flow(a) > 0) != diverge)?;
        let others: Vec<usize> = (0..3).filter(|&k| k != ti).collect();
        let dt = self.arm_dir(arms[ti], DIR_M)?;
        let cont = [-dt[0], -dt[1]];
        let st = self.seg_of_edge(arms[ti].0);
        let key = |k: usize| {
            let s = self.seg_of_edge(arms[k].0);
            let cs = self.arm_dir(arms[k], DIR_M).map(|d| dot(cont, d)).unwrap_or(-2.0);
            (s.cls == st.cls && !s.link, !s.link, cs, s.lanes)
        };
        let (ka, kc) = (key(others[0]), key(others[1]));
        if ka.2 < FORK_COS || kc.2 < FORK_COS { return None; }
        let a_first = if ka.0 != kc.0 { ka.0 } else if ka.1 != kc.1 { ka.1 } else if ka.2 != kc.2 { ka.2 > kc.2 }
                      else if ka.3 != kc.3 { ka.3 > kc.3 } else { self.seg_of_edge(arms[others[0]].0).fid <= self.seg_of_edge(arms[others[1]].0).fid };
        let (mi, bi) = if a_first { (others[0], others[1]) } else { (others[1], others[0]) };
        Some((arms, ti, mi, bi, diverge))
    }

    /// v3 walk, over edges.
    fn walk(&self, start: usize, a0: (usize, bool), reach: f64) -> Path {
        let flow = self.arm_flow(a0);
        let e0 = &self.edges[a0.0];
        let seg0 = &self.segs[e0.seg];
        let mut pts = vec![seg0.pts[if a0.1 { e0.va } else { e0.vb }]];
        let mut ss = vec![0.0];
        let mut pieces = Vec::new();
        let mut a = a0;
        let mut seen = vec![start];
        'outer: loop {
            let e = &self.edges[a.0];
            let seg = &self.segs[e.seg];
            let s_start = *ss.last().unwrap();
            let (v_from, v_to) = if a.1 { (e.va, e.vb) } else { (e.vb, e.va) };
            let mut i = v_from;
            while i != v_to {
                let j = if a.1 { i + 1 } else { i - 1 };
                let (pa, pb) = (seg.pts[i], seg.pts[j]);
                let l = norm(sub(pb, pa));
                let s0 = *ss.last().unwrap();
                if s0 + l >= reach {
                    let u = if l > 0.0 { (reach - s0) / l } else { 0.0 };
                    pts.push(lerp(pa, pb, u));
                    ss.push(reach);
                    pieces.push(Piece { edge: a.0, fwd: a.1, s0: s_start, s1: reach, end_node: None });
                    break 'outer;
                }
                if l > 0.0 { pts.push(pb); ss.push(s0 + l); }
                i = j;
            }
            let n = if a.1 { e.nb } else { e.na };
            let s_now = *ss.last().unwrap();
            pieces.push(Piece { edge: a.0, fwd: a.1, s0: s_start, s1: s_now, end_node: Some(n) });
            if seen.contains(&n) || pieces.len() >= MAX_PIECES { break; }
            seen.push(n);
            let all = self.arms_fc(n);
            let me = (a.0, !a.1);
            let others: Vec<(usize, bool)> = all.iter().copied().filter(|&x| x != me).collect();
            let next = if others.len() == 1 {
                let h = others[0];
                if self.arm_flow(h) != flow || (self.seg_of_edge(h.0).dir == 0) != (seg.dir == 0) { break; }
                Some(h)
            } else if others.len() == 2 {
                match self.fork_roles(n) {
                    Some((arms, ti, mi, _bi, _dv)) => {
                        let k = arms.iter().position(|&x| x == me);
                        match k { Some(k) if k == ti => Some(arms[mi]), Some(k) if k == mi => Some(arms[ti]), _ => None }
                    }
                    None => None,
                }
            } else { None };
            match next { Some(h) => { a = h; } None => break }
        }
        Path { line: Line { p: pts, s: ss }, pieces }
    }

    /// v3 at_grade_end (divided_crossings on, as BNE_ROADSURF_V3=surface).
    fn at_grade_end(&self, p: &Path, reach: f64) -> bool {
        if p.len() > reach { return false; }
        let Some(node) = p.pieces.last().and_then(|x| x.end_node) else { return false; };
        let arms_all = &self.nodes[node].arms;
        if arms_all.iter().any(|&(e, _)| { let s = self.seg_of_edge(e); s.dir == 0 && s.cls > 0 }) { return true; }
        let arms = self.arms_fc(node);
        if arms.len() < 3 { return false; }
        let (_, tangent) = p.line.at(p.len());
        arms.iter().any(|&a| self.arm_dir(a, 1e-3).is_some_and(|u| absf(dot(u, tangent)) < CROSS_COS))
    }

    /// v3 fork_along: the first one-way fork on path within max_s: (station, node, diverge, side +1 right).
    fn fork_along(&self, p: &Path, max_s: f64) -> Option<(f64, usize, bool, f64)> {
        for pc in &p.pieces {
            if pc.s1 > max_s { break; }
            if let Some(n) = pc.end_node {
                if let Some((arms, _ti, mi, bi, dv)) = self.fork_roles(n) {
                    let (dm, db) = (self.arm_dir(arms[mi], DIR_M)?, self.arm_dir(arms[bi], DIR_M)?);
                    let right_of_walk = cross(dm, db) < 0.0;
                    let right = if dv { right_of_walk } else { !right_of_walk };
                    return Some((pc.s1, n, dv, if right { 1.0 } else { -1.0 }));
                }
            }
        }
        None
    }

    /// v3 add_side: (right as the driver sees it, why, feed).
    fn add_side(&self, wide: &Seg, wide_fwd: bool, narrow: &Seg, down: &Path, up: &Path, add: bool, region: usize)
                -> (bool, &'static str, Option<(f64, bool)>) {
        let lht = self.t(region, T_LHT) > 0.5;
        if wide.dir != 0 {
            let (pw, pn) = (wide.place.as_str(), narrow.place.as_str());
            if pw.starts_with("right_of") || pw.starts_with("left_of") || pn.starts_with("right_of") || pn.starts_with("left_of") {
                let nw = lanes_dir(wide, wide_fwd);
                let a = left_off(pw, nw, 1.0);
                let b = left_off(pn, narrow.lanes, 1.0);
                if (a - b) * (a - b) < 0.01 { return (true, "placement", None); }
                let r = (a - nw as f64) - (b - narrow.lanes as f64);
                if r * r < 0.01 { return (false, "placement", None); }
            }
        }
        let turn = turn_dir(wide, wide_fwd);
        if !turn.is_empty() {
            let lanes: Vec<&str> = turn.split('|').collect();
            let last = lanes.last().copied().unwrap_or("");
            let first = lanes.first().copied().unwrap_or("");
            if !add && last.contains("merge_to_left") { return (true, "turn_lanes", None); }
            if !add && first.contains("merge_to_right") { return (false, "turn_lanes", None); }
            if wide.dir == 0 {
                let t0: Vec<&str> = first.split(';').collect();
                if t0.iter().all(|v| *v == "left" || *v == "sharp_left" || *v == "slight_left") { return (lht, "bay", None); }
                let t1: Vec<&str> = last.split(';').collect();
                if t1.iter().all(|v| *v == "right" || *v == "sharp_right" || *v == "slight_right") { return (!lht, "bay", None); }
            }
            if add && last.split(';').all(|v| v == "slight_right") { return (true, "turn_lanes", None); }
            if add && first.split(';').all(|v| v == "slight_left") { return (false, "turn_lanes", None); }
        }
        let feed = if add { self.fork_along(down, FEED_M) } else { self.fork_along(up, FEED_M) };
        if let Some((s, _n, dv, sd)) = feed {
            if dv == add { return (sd > 0.0, if add { "exit_after" } else { "entrance_before" }, Some((s, dv))); }
        }
        (!lht, "outer", None)
    }

    /// v3 fork_event's decision and stations (no rows): Err(reason) when the node is no fork event.
    fn fork_event(&self, n: usize) -> Result<Fork, &'static str> {
        let (arms, ti, mi, bi, diverge) = self.fork_roles(n).ok_or("roles")?;
        let wk = |k: usize| self.walk(n, arms[k], REACH_M);
        let (pt_, pm_, pb_) = (wk(ti), wk(mi), wk(bi));
        let (st, sm, sb) = (self.seg_of_edge(arms[ti].0), self.seg_of_edge(arms[mi].0), self.seg_of_edge(arms[bi].0));
        if !(sm.link || sb.link || (sm.cls <= CLS_TRUNK && sb.cls <= CLS_TRUNK)) { return Err("classes"); }
        let region = self.region[self.edges[arms[ti].0].seg];
        let w = self.lane_w(arms[mi].0);
        let (lt, lm, lb0) = (st.lanes, sm.lanes, sb.lanes);
        let lb = if lb0 >= 1 { lb0 } else { 1 };
        let lm_e = if lm >= 1 { lm } else if lt >= 1 && lt > lb { lt - lb } else { 2 };
        let half = &pm_;
        let mut samples: Vec<(f64, f64, f64)> = Vec::new();
        let mut t = STEP_M;
        let mut side = 0.0;
        while t <= pb_.len() {
            let (p, _) = pb_.line.at(t);
            let (s, d) = half.line.project(p, 0.0, f64::INFINITY);
            if s >= half.len() - 1e-6 { break; }
            if side == 0.0 && absf(d) >= 1.0 { side = if d < 0.0 { -1.0 } else { 1.0 }; }
            samples.push((t, s, d));
            t += STEP_M;
        }
        if side == 0.0 {
            let x = cross(self.arm_dir(arms[mi], DIR_M).ok_or("dir")?, self.arm_dir(arms[bi], DIR_M).ok_or("dir")?);
            side = if x < 0.0 { -1.0 } else { 1.0 };
        }
        let right = (side < 0.0) == diverge;
        let kmh = sm.kmh;
        let motorway = |s: &Seg| s.cls == 1 && !s.link;
        let street = !(motorway(st) && motorway(sm));
        if street && [&pt_, &pm_, &pb_].iter().any(|p| self.at_grade_end(p, AT_GRADE_M)) { return Err("at_grade"); }
        let lo = left_off(&sm.place, lm_e, w);
        let near = if right { lm_e as f64 * w - lo } else { lo };
        let thr = near + lb as f64 * w / 2.0;
        let mut sg: Option<f64> = None;
        let mut apart: Option<f64> = None;
        for &(tb, s, d) in &samples {
            let dd = d * side;
            if dd < thr { sg = None; } else if sg.is_none() { sg = Some(s); }
            if dd < thr + NOSE_W_M + 3.0 { apart = None; } else if apart.is_none() { apart = Some(tb); }
            if let Some(a) = apart { if tb - a >= NOSE_HOLD_M { break; } }
        }
        let sg = sg.ok_or("no_separation")?;
        let gore_m = if diverge { self.t(region, T_GORE_1) } else { self.t(region, T_GORE_IN) } + (lb - 1) as f64 * self.t(region, T_GORE_X);
        let (lo_main, hi_main) = if diverge { (-pt_.len(), pm_.len()) } else { (-pm_.len(), pt_.len()) };
        let s_g = if diverge { sg } else { -sg };
        let mut s_n = if diverge { s_g + gore_m } else { s_g - gore_m };
        if s_n > hi_main { s_n = hi_main; }
        if s_n < lo_main { s_n = lo_main; }
        let share = speed_share(kmh);
        let exit_taper = { let v = self.t(region, T_EXIT_K) * w; let m0 = self.t(region, T_EXIT_MIN); if v > m0 { v } else { m0 } };
        let entry_taper = { let v = self.t(region, T_ENTRY_K) * w; let m0 = self.t(region, T_ENTRY_MIN); if v > m0 { v } else { m0 } };
        let need = lm_e + lb;
        let tl = exit_lanes(turn_dir(st, true), !right);
        let (mut taper0, mut taper1, mut par0, mut par1);
        let mut evidence = "design";
        let mut typ = "parallel";
        if lt >= 1 && lm >= 1 && lt >= need || (diverge && st.dir != 0 && tl >= 1 && tl >= lb) {
            evidence = if lt >= 1 && lm >= 1 && lt >= need { "lanes" } else { "turn_lanes" };
            // the lanes exist on the trunk: they are its slots (their start is that joint's own add, G1 below)
            taper0 = if diverge { lo_main } else { hi_main };
            taper1 = taper0;
            par0 = taper0; par1 = s_g;
            typ = "existing";
        } else if diverge {
            if sm.link && sb.link {
                typ = "taper";
                let l = self.t(region, T_DIV_K) * w;
                taper0 = s_g - l; taper1 = s_g; par0 = s_g; par1 = s_g;
            } else {
                let dec = self.t(region, T_DEC_PAR) * share;
                par1 = s_g; par0 = s_g - dec; taper1 = par0; taper0 = par0 - exit_taper;
            }
        } else if sm.link && sb.link {
            typ = "taper";
            let l = self.t(region, T_TAPER_MERGE);
            taper0 = s_g; taper1 = s_g + l; par0 = s_g; par1 = s_g;
        } else {
            let acc = self.t(region, T_ACC_PAR) * share;
            par0 = s_g; par1 = s_g + acc; taper0 = par1; taper1 = par1 + entry_taper;
        }
        if evidence == "design" {
            if diverge && taper1 > s_g { taper1 = s_g; if taper0 > s_g - exit_taper { taper0 = s_g - exit_taper; } par0 = s_g; }
            if !diverge && taper0 < s_g { taper0 = s_g; if taper1 < s_g + entry_taper { taper1 = s_g + entry_taper; } par1 = s_g; }
            if diverge {
                if taper0 < lo_main { taper0 = lo_main; }
                if taper1 < taper0 { taper1 = taper0; }
            } else {
                if taper1 > hi_main { taper1 = hi_main; }
                if taper0 > taper1 { taper0 = taper1; }
            }
        }
        let _ = (par0, par1);
        // (v3's second at-grade test on the painted interval)
        let (lo_s, hi_s) = if diverge { (taper0 - MARGIN_M, s_n) } else { (s_n, taper1 + MARGIN_M) };
        if street {
            let (up, dn) = if diverge { (&pt_, &pm_) } else { (&pm_, &pt_) };
            if self.at_grade_end(up, AT_GRADE_M - lo_s) || self.at_grade_end(dn, AT_GRADE_M + hi_s) { return Err("at_grade"); }
        }
        // the physical nose: where the measured gore reaches the nose width, at most v3's painted gore length past s_g
        let sg_abs = absf(s_g);
        if let Some(&(_tb, s_w, _d)) = samples.iter().find(|x| x.1 > sg_abs && x.2 * side - thr >= NOSE_W_M) {
            if s_w < absf(s_n) { s_n = if diverge { s_w } else { -s_w }; }
        }
        let sn_abs = absf(s_n);
        let tb_n = samples.iter().find(|x| x.1 >= sn_abs).map(|x| x.0).unwrap_or(sn_abs);
        let mut sep: Vec<(f64, f64)> = Vec::new();
        for &(_tb, s, d) in &samples {
            if s > sn_abs + 5.0 { break; }
            if sep.last().map(|x| s - x.0 >= 5.0).unwrap_or(true) { sep.push((s, (d * side - thr).max(0.0))); }
        }
        Ok(Fork { node: n, trunk: arms[ti], through: arms[mi], branch: arms[bi], diverge, right, lb, lm: lm_e, w, s_g, s_n, tb_n, sep,
                  taper0, taper1, typ, evidence, region })
    }
}

// ------------------------------------------------------------------------------------------------------- building

/// The structure support kind and fraction intervals of a segment, as stations along it.
fn seg_ranges(s: &Seg) -> (Vec<(f64, f64, u8)>, Vec<(f64, f64, f64)>) {
    let l = Line::new(s.pts.clone());
    let len = l.len();
    let mut out = Vec::new();
    for &(a, b) in &s.bridge { out.push((a * len, b * len, SUP_STRUCT)); }
    for &(a, b) in &s.tunnel { out.push((a * len, b * len, SUP_TUNNEL)); }
    let lv = s.level.iter().map(|&(a, b, v)| (a * len, b * len, v)).collect();
    (out, lv)
}

pub fn build(place: &Place, input_order: &[usize]) -> Net { build_opts(place, input_order, Opts::default()) }

/// FNV-1a 64 of a string and integers (stable ids).
pub fn uid_of(s: &str, ints: &[u64]) -> u64 {
    let mut h: u64 = 0xcbf29ce484222325;
    for &b in s.as_bytes() { h ^= b as u64; h = h.wrapping_mul(0x100000001b3); }
    for v in ints { for b in v.to_le_bytes() { h ^= b as u64; h = h.wrapping_mul(0x100000001b3); } }
    h
}

/// A stroke's piece label in the tile bytes (31 bits; zones carry the top bit).
pub fn piece_of(uid: u64) -> u32 { ((uid ^ (uid >> 32)) as u32) & 0x7fff_ffff }

pub fn build_opts(place: &Place, input_order: &[usize], opts: Opts) -> Net {
    // canonical order: by fid, whatever the input order (determinism: the reversed-input test); one copy per fid
    let mut idx: Vec<usize> = input_order.to_vec();
    idx.sort_by(|&a, &b| place.segs[a].fid.cmp(&place.segs[b].fid).then(a.cmp(&b)));
    idx.dedup_by(|a, b| place.segs[*a].fid == place.segs[*b].fid);
    // a road split into lane sections ("<id>~lanesA-B", lanesections.split) is read from its sections only (a cell
    // copy can still hold the unsplit road)
    let parents: std::collections::BTreeSet<String> = idx.iter().filter_map(|&i| place.segs[i].fid.split_once("~lanes").map(|x| x.0.to_string())).collect();
    let segs: Vec<Seg> = idx.iter().map(|&i| place.segs[i].clone()).filter(|s| s.pts.len() >= 2 && !parents.contains(&s.fid)).collect();
    let region: Vec<usize> = segs.iter().map(|s| {
        let r = s.region.max(0) as usize;
        if r == 0 && s.code.eq_ignore_ascii_case("JP") { 6 } else if r < 6 { r } else { 0 }
    }).collect();
    // nodes: connector ids (sorted), plus synthetic ends
    let mut ids: Vec<String> = Vec::new();
    for s in segs.iter() {
        for (c, _v) in &s.cons { ids.push(c.clone()); }
        ids.push(format!("~{}#0", s.fid));
        ids.push(format!("~{}#1", s.fid));
    }
    ids.sort();
    ids.dedup();
    let node_of = |id: &str| ids.binary_search_by(|x| x.as_str().cmp(id)).unwrap();
    let mut nodes: Vec<Node> = (0..ids.len()).map(|_| Node::default()).collect();
    let mut node_set = vec![false; ids.len()];
    let mut edges: Vec<Edge> = Vec::new();
    for (g, s) in segs.iter().enumerate() {
        let mut vs: Vec<(usize, usize)> = s.cons.iter().filter(|(_, v)| *v < s.pts.len()).map(|(c, v)| (*v, node_of(c))).collect();
        vs.sort();
        vs.dedup_by(|a, b| a.0 == b.0);
        if vs.first().map(|x| x.0) != Some(0) { vs.insert(0, (0, node_of(&format!("~{}#0", s.fid)))); }
        if vs.last().map(|x| x.0) != Some(s.pts.len() - 1) { vs.push((s.pts.len() - 1, node_of(&format!("~{}#1", s.fid)))); }
        for &(v, n) in &vs {
            if !node_set[n] { nodes[n].p = s.pts[v]; node_set[n] = true; }
        }
        for k in 0..vs.len() - 1 {
            let (va, na) = vs[k];
            let (vb, nb) = vs[k + 1];
            let mut len = 0.0;
            for i in va..vb { len += norm(sub(s.pts[i + 1], s.pts[i])); }
            if len <= 1e-3 || na == nb { continue; }
            let e = edges.len();
            edges.push(Edge { seg: g, va, vb, na, nb, len });
            nodes[na].arms.push((e, true));
            nodes[nb].arms.push((e, false));
        }
    }
    let nn = nodes.len();
    let mut net = Net { opts, segs, node_ids: ids.clone(), cut: vec![false; nn], region, edges, nodes, forks: Vec::new(),
                        fork_at: vec![None; nn], strokes: Vec::new(), edge_stroke: Vec::new(), crossings: Vec::new(),
                        rail_floors: Vec::new(), rails: place.rails.clone(),
                        stats: NetStats::default(), xsec_on: place.xsec, lays: Vec::new(), xs_tags: Vec::new(),
                        area_fit: Default::default() };
    net.lays = vec![None; net.segs.len()];
    net.xs_tags = vec![0; net.segs.len()];
    if net.xsec_on { cross_sections(&mut net); }
    net.stats.segs = net.segs.len();
    net.stats.nodes = net.nodes.iter().filter(|n| !n.arms.is_empty()).count();
    net.stats.edges = net.edges.len();
    // fork events (v3 rules)
    for n in 0..nn {
        if net.nodes[n].arms.len() != 3 { continue; }
        if net.fork_roles(n).is_none() { continue; }
        match net.fork_event(n) {
            Ok(f) => { net.fork_at[n] = Some(net.forks.len()); net.forks.push(f); }
            Err("at_grade") => net.stats.forks_at_grade += 1,
            Err("no_separation") => net.stats.forks_no_sep += 1,
            Err(_) => {}
        }
    }
    net.stats.forks = net.forks.len();
    if opts.cut_grid > 0.0 { cut_nodes(&mut net, opts); }
    strokes(&mut net);
    lanes(&mut net);
    joints(&mut net);
    if opts.areas && !place.areas.is_empty() {
        let prep = crate::areas::prepare(&place.areas);
        net.area_fit = crate::areas::fit(&mut net, &prep);
    }
    crossings(&mut net);
    rail_floors(&mut net);
    net.stats.strokes = net.strokes.len();
    net
}

/// Stroke cuts (Opts.cut_grid): an edge whose end nodes lie in different squares of the grid is cut at one of its
/// ends, a plain joint of two arms of one carriageway (no fork, the same lanes, widths and placement on both
/// sides, not on a structure or in a tunnel), the one with the smaller connector id when both qualify. Data only:
/// the same nodes whatever the build's window.
fn cut_nodes(net: &mut Net, opts: Opts) {
    let sq = |p: P2| (((p[0] + opts.cut_offset) / opts.cut_grid).floor() as i64, ((p[1] + opts.cut_offset) / opts.cut_grid).floor() as i64);
    let on_structure = |net: &Net, e: usize, v: usize| -> bool {
        let s = net.seg_of_edge(e);
        let l = Line::new(s.pts.clone());
        let len = l.len().max(1e-9);
        let f = l.s[v] / len;
        s.bridge.iter().chain(s.tunnel.iter()).any(|&(a, b)| f >= a - 1e-6 && f <= b + 1e-6)
    };
    let qualifies = |net: &Net, n: usize| -> bool {
        let arms = &net.nodes[n].arms;
        if arms.len() != 2 || net.fork_at[n].is_some() { return false; }
        let (a, b) = (arms[0], arms[1]);
        if a.0 == b.0 { return false; }
        let (sa, sb) = (net.seg_of_edge(a.0), net.seg_of_edge(b.0));
        let same = sa.cls == sb.cls && sa.link == sb.link && sa.lanes == sb.lanes && sa.lfw == sb.lfw && sa.lbw == sb.lbw
            && sa.place == sb.place && sa.lane_w == sb.lane_w && sa.turn == sb.turn && sa.turn_bw == sb.turn_bw
            && (sa.dir == 0) == (sb.dir == 0) && sa.pave == sb.pave && sa.drive == sb.drive && sa.urban == sb.urban;
        // (BNE_ROADSYS_XSEC: the same strips on both sides; tags that shape nothing never move a cut)
        if !same || net.lays[net.edges[a.0].seg] != net.lays[net.edges[b.0].seg] { return false; }
        for &(e, fwd) in arms {
            let ed = &net.edges[e];
            if on_structure(net, e, if fwd { ed.va } else { ed.vb }) { return false; }
        }
        joined(net, n, a) == Some(b)
    };
    let mut cut = vec![false; net.nodes.len()];
    for e in 0..net.edges.len() {
        let (na, nb) = (net.edges[e].na, net.edges[e].nb);
        if sq(net.nodes[na].p) == sq(net.nodes[nb].p) { continue; }
        let (qa, qb) = (qualifies(net, na), qualifies(net, nb));
        let pick = match (qa, qb) { (true, true) => Some(na.min(nb)), (true, false) => Some(na), (false, true) => Some(nb), _ => None };
        if let Some(n) = pick { cut[n] = true; }
    }
    net.stats.cuts = cut.iter().filter(|&&c| c).count();
    net.cut = cut;
}

/// The strokes meeting at a cut node continue each other: the later one's first (or last) row is the earlier one's
/// (a joint snap, as a fork's branch takes the trunk's row), so the cut leaves no seam and no zone. Strokes of
/// opposite orientation there keep their own rows and get a zone (counted).
fn joints(net: &mut Net) {
    let mut at: Vec<Vec<(usize, bool)>> = vec![Vec::new(); net.nodes.len()];
    for (si, st) in net.strokes.iter().enumerate() {
        if net.cut[st.n0] { at[st.n0].push((si, true)); }
        if net.cut[st.n1] { at[st.n1].push((si, false)); }
    }
    for n in 0..net.nodes.len() {
        if at[n].len() != 2 { continue; }
        let ((a, a_start), (b, b_start)) = (at[n][0], at[n][1]);
        if a == b || a_start == b_start { net.stats.cut_unjoined += 1; continue; }
        // the one ending at the node is the trunk; the one starting there takes its last row as its first
        let (tr, br) = if !a_start { (a, b) } else { (b, a) };
        let k1 = net.strokes[tr].slots.len().saturating_sub(1);
        if net.strokes[br].snap0.is_some() || net.strokes[br].slots.len() != net.strokes[tr].slots.len() {
            net.stats.cut_unjoined += 1;
            continue;
        }
        let s = net.strokes[tr].len();
        net.strokes[br].snap0 = Some(Snap { trunk: tr, s, k0: 0, k1, tb: 0.0, joint: true });
    }
}

/// The arm joined to arm `a` (arriving at node n along it, as an arm of n) by the stroke rule, if any.
fn joined(net: &Net, n: usize, a: (usize, bool)) -> Option<(usize, bool)> {
    if net.cut[n] { return None; }
    let arms = &net.nodes[n].arms;
    if let Some(fi) = net.fork_at[n] {
        let f = &net.forks[fi];
        if a == f.trunk { return Some(f.through); }
        if a == f.through { return Some(f.trunk); }
        return None;
    }
    if arms.len() != 2 { return None; }
    let other = if arms[0] == a { arms[1] } else if arms[1] == a { arms[0] } else { return None };
    let (sa, sb) = (net.seg_of_edge(a.0), net.seg_of_edge(other.0));
    if (sa.dir == 0) != (sb.dir == 0) { return None; }
    if sa.dir != 0 {
        let (fa, fb) = (flow_of(sa, a.1), flow_of(sb, other.1));
        if fa == fb { return None; }
    }
    if other.0 == a.0 { return None; }
    Some(other)
}

fn strokes(net: &mut Net) {
    let ne = net.edges.len();
    let mut used = vec![false; ne];
    net.edge_stroke = vec![(usize::MAX, 0); ne];
    for e0 in 0..ne {
        if used[e0] { continue; }
        let seg = net.seg_of_edge(e0);
        // travel orientation: a one-way along its flow; a two-way along its points
        let fwd0 = !(seg.dir == -1);
        // extend backwards from the start node
        let mut parts: Vec<(usize, bool)> = vec![(e0, fwd0)];
        used[e0] = true;
        loop {
            let (e, f) = parts[0];
            let ed = &net.edges[e];
            let n = if f { ed.na } else { ed.nb };
            let arm = (e, f);       // leaving n along the stroke
            match joined(net, n, arm) {
                Some((e2, f2)) if !used[e2] => { used[e2] = true; parts.insert(0, (e2, !f2)); }
                _ => break,
            }
            if parts.len() > 10_000 { break; }
        }
        loop {
            let (e, f) = *parts.last().unwrap();
            let ed = &net.edges[e];
            let n = if f { ed.nb } else { ed.na };
            let arm = (e, !f);      // arriving at n: n's arm along this edge
            match joined(net, n, arm) {
                Some((e2, f2)) if !used[e2] => { used[e2] = true; parts.push((e2, f2)); }
                _ => break,
            }
            if parts.len() > 10_000 { break; }
        }
        // polyline
        let mut pts: Vec<P2> = Vec::new();
        let mut ps: Vec<Part> = Vec::new();
        let mut acc = 0.0;
        for &(e, f) in &parts {
            let ed = &net.edges[e];
            let s = &net.segs[ed.seg];
            let vs: Vec<usize> = if f { (ed.va..=ed.vb).collect() } else { (ed.va..=ed.vb).rev().collect() };
            let s0 = acc;
            for (k, &v) in vs.iter().enumerate() {
                let p = s.pts[v];
                if let Some(&q) = pts.last() {
                    let l = norm(sub(p, q));
                    if k == 0 && l < 1e-9 { continue; }
                    if l < 1e-6 { continue; }
                    acc += l;
                }
                pts.push(p);
            }
            ps.push(Part { edge: e, fwd: f, s0, s1: acc });
        }
        if pts.len() < 2 { continue; }
        let first = ps[0];
        let last = *ps.last().unwrap();
        let n0 = if first.fwd { net.edges[first.edge].na } else { net.edges[first.edge].nb };
        let n1 = if last.fwd { net.edges[last.edge].nb } else { net.edges[last.edge].na };
        let sid = net.strokes.len();
        for (k, p) in ps.iter().enumerate() { net.edge_stroke[p.edge] = (sid, k); }
        let g = net.edges[first.edge].seg;
        let seg = &net.segs[g];
        let region = net.region[g];
        let mut cls = i64::MAX;
        let mut kmh: f64 = 0.0;
        for p in &ps { let s = net.seg_of_edge(p.edge); if s.cls > 0 && s.cls < cls { cls = s.cls; } if s.kmh > kmh { kmh = s.kmh; } }
        if cls == i64::MAX { cls = 0; }
        let line = Line::new(pts);
        // support intervals
        let mut sup: Vec<Sup> = Vec::new();
        for p in &ps {
            let ed = &net.edges[p.edge];
            let s = &net.segs[ed.seg];
            let (rs, lv) = seg_ranges(s);
            let l = Line::new(s.pts.clone());
            let (aa, ab) = (l.s[ed.va], l.s[ed.vb]);
            let to_st = |x: f64| if p.fwd { p.s0 + (x - aa) } else { p.s0 + (ab - x) };
            let level_at = |x: f64| lv.iter().find(|r| x >= r.0 && x <= r.1).map(|r| r.2).unwrap_or(0.0);
            for &(a, b, k) in &rs {
                let (a2, b2) = (a.max(aa), b.min(ab));
                if b2 - a2 < 0.5 { continue; }
                let (x0, x1) = (to_st(a2), to_st(b2));
                let (lo, hi) = if x0 < x1 { (x0, x1) } else { (x1, x0) };
                sup.push(Sup { s0: lo, s1: hi, kind: k, sid: 0, level: level_at((a2 + b2) / 2.0), depth: if k == SUP_STRUCT { s.depth } else { 0.0 } });
            }
        }
        sup.sort_by(|a, b| a.s0.partial_cmp(&b.s0).unwrap());
        let mut merged: Vec<Sup> = Vec::new();
        for u in sup {
            if let Some(m) = merged.last_mut() {
                if m.kind == u.kind && u.s0 <= m.s1 + 1.0 { if u.s1 > m.s1 { m.s1 = u.s1; } if u.depth > m.depth { m.depth = u.depth; } continue; }
            }
            merged.push(u);
        }

        let lht = TAPER[region][T_LHT] > 0.5;
        let uid = uid_of(&seg.fid, &[net.edges[first.edge].va as u64, first.fwd as u64]);
        for (k, m) in merged.iter_mut().enumerate() { m.sid = piece_of(uid_of("sup", &[uid, k as u64])); }
        net.strokes.push(Stroke {
            uid, parts: ps, line, n0, n1, oneway: seg.dir != 0, lht, slots: Vec::new(), anchor_l: Prog::default(),
            anchor_r: Prog::default(), right_windows: Vec::new(), stations: Vec::new(), dense: Vec::new(), sup: merged,
            cls, cls_s: seg.cls_s.clone(), kmh: if kmh > 0.0 { kmh } else { 50.0 }, region, snap0: None, snap1: None,
            cut0: 0.0, cut1: 0.0, absorbed: false, lane_scale: Vec::new(), cap_nbrs: Vec::new(), reach: 0.0, over: Vec::new(),
            area: Vec::new(), skews: Vec::new(),
        });
    }
    for s in &net.strokes {
        net.stats.structures += s.sup.iter().filter(|u| u.kind == SUP_STRUCT).count();
        net.stats.tunnels += s.sup.iter().filter(|u| u.kind == SUP_TUNNEL).count();
    }
}

/// One direction group's slot builder (stroke left-to-right order).
struct GroupB { cur: Vec<usize>, group: u8 }

fn insert_after(order: &mut Vec<usize>, after: Option<usize>, before_first: usize, id: usize) {
    match after {
        Some(a) => { let k = order.iter().position(|&x| x == a).unwrap(); order.insert(k + 1, id); }
        None => { let k = order.iter().position(|&x| x == before_first).unwrap(); order.insert(k, id); }
    }
}

fn lanes(net: &mut Net) {
    let ns = net.strokes.len();
    // branch snaps registered by trunk strokes: (branch stroke, at start?, Snap)
    let mut snaps: Vec<(usize, bool, Snap)> = Vec::new();
    for si in 0..ns {
        let (slots, al, ar, rw, stations, dense, snaps_here, over) = build_slots(net, si);
        let st = &mut net.strokes[si];
        st.slots = slots; st.anchor_l = al; st.anchor_r = ar; st.right_windows = rw; st.stations = stations; st.dense = dense;
        st.over = over;
        snaps.extend(snaps_here);
    }
    for (b, at_start, sn) in snaps {
        if at_start { net.strokes[b].snap0 = Some(sn); } else { net.strokes[b].snap1 = Some(sn); }
    }
    // each stroke's program reach: a node of it that the fork rules test walks REACH_M (+ the arms' DIR_M) along the
    // map; a lane count change walks FEED_M for a feeding fork; else its own parts and the arms at its nodes
    for si in 0..ns {
        let st = &net.strokes[si];
        let mut nodes: Vec<usize> = vec![st.n0, st.n1];
        for p in &st.parts { let e = &net.edges[p.edge]; nodes.push(e.na); nodes.push(e.nb); }
        let fork = nodes.iter().any(|&n| net.nodes[n].arms.len() == 3 && net.fork_roles(n).is_some());
        let mut change = false;
        for w in st.parts.windows(2) {
            let (a, b) = (net.seg_of_edge(w[0].edge), net.seg_of_edge(w[1].edge));
            if a.lanes != b.lanes || a.lfw != b.lfw || a.lbw != b.lbw || a.turn != b.turn || a.turn_bw != b.turn_bw { change = true; }
        }
        net.strokes[si].reach = if fork { REACH_M + DIR_M } else if change { FEED_M + DIR_M } else { 3.0 * DIR_M };
    }
}

type SlotsOut = (Vec<Slot>, Prog, Prog, Vec<(f64, f64)>, Vec<f64>, Vec<(f64, f64)>, Vec<(usize, bool, Snap)>, Vec<(usize, f64, f64, u8)>);

fn build_slots(net: &mut Net, si: usize) -> SlotsOut {
    let st = &net.strokes[si];
    let parts = st.parts.clone();
    let oneway = st.oneway;
    let lht = st.lht;
    let len = st.len();
    let mut slots: Vec<Slot> = Vec::new();
    let mut order: Vec<usize> = Vec::new();
    let mut stations: Vec<f64> = Vec::new();
    let mut dense: Vec<(f64, f64)> = Vec::new();
    let mut rwin: Vec<(f64, f64)> = Vec::new();
    let mut snaps_out: Vec<(usize, bool, Snap)> = Vec::new();
    let inf = f64::INFINITY;
    let e0 = parts[0].edge;
    let w0 = net.lane_w(e0);
    let (shr, shl) = net.shoulder_w(e0);
    // shoulders
    let lsh = slots.len();
    slots.push(Slot { kind: K_SHOULDER, group: 9, prog: Prog { w0: if oneway { shl } else { shr }, steps: vec![] }, alive: (-inf, inf), cap: vec![] });
    order.push(lsh);
    let rsh0 = slots.len();
    slots.push(Slot { kind: K_SHOULDER, group: 9, prog: Prog { w0: shr, steps: vec![] }, alive: (-inf, inf), cap: vec![] });
    order.push(rsh0);
    let mut lsh_cur = lsh;
    let mut rsh_cur = rsh0;
    // groups: (travel along the stroke?) fwd always; bwd for two-way
    let mut groups: Vec<(bool, GroupB)> = vec![(true, GroupB { cur: vec![], group: 0 })];
    if !oneway { groups.push((false, GroupB { cur: vec![], group: 1 })); }
    // initial lanes, in stroke left-to-right: two-way RHT [bwd][fwd]; LHT [fwd][bwd]
    let seg0 = net.seg_of_edge(e0).clone();
    let along0 = parts[0].fwd;     // stroke direction vs the segment's points
    let mut gorder: Vec<usize> = (0..groups.len()).collect();
    if !oneway && !lht { gorder.reverse(); }   // bwd first (left)
    for &gi in &gorder {
        let fwd_travel = groups[gi].0;
        let n = if oneway { lanes_eff(&seg0, true) } else { lanes_eff(&seg0, fwd_travel == along0) };
        for _ in 0..n.max(1) {
            let id = slots.len();
            slots.push(Slot { kind: K_LANE, group: groups[gi].1.group, prog: Prog { w0, steps: vec![] }, alive: (-inf, inf), cap: vec![] });
            let k = order.iter().position(|&x| x == rsh0).unwrap();
            order.insert(k, id);
            groups[gi].1.cur.push(id);
        }
    }
    // anchors (one-way): left edge of the lanes and right edge, per part, blended over SHIFT_M at each joint
    let lanes_of_part = |net: &Net, p: &Part, travel: bool| -> i64 {
        let s = net.seg_of_edge(p.edge);
        if s.dir != 0 { lanes_eff(s, true) } else { lanes_eff(s, travel == p.fwd) }
    };
    let place_l = |net: &Net, p: &Part| -> f64 {
        let s = net.seg_of_edge(p.edge);
        let v = left_off(&s.place, lanes_of_part(net, p, true), net.lane_w(p.edge));
        // (BNE_ROADSYS_XSEC) a one-way without a mapped placement: its whole carriageway (lanes and strips) centred
        match net.xs_part(p) { Some((l, r)) if oneway && s.place.is_empty() => v + (r.strips() - l.strips()) / 2.0, _ => v }
    };
    let mut al = Prog { w0: place_l(net, &parts[0]), steps: vec![] };
    let mut ar = Prog { w0: place_l(net, &parts[0]) - lanes_of_part(net, &parts[0], true) as f64 * w0, steps: vec![] };
    let mut cur_w = w0;
    let mut cur_sh = (shr, shl);
    // the stroke's transitions
    for k in 0..parts.len().saturating_sub(1) {
        let (pa, pb) = (parts[k], parts[k + 1]);
        let sigma = pa.s1;
        let node = if pa.fwd { net.edges[pa.edge].nb } else { net.edges[pa.edge].na };
        stations.push(sigma);
        // anchors
        let (la, lb_) = (place_l(net, &pa), place_l(net, &pb));
        let (wa, wb) = (net.lane_w(pa.edge), net.lane_w(pb.edge));
        let (ra, rb) = (la - lanes_of_part(net, &pa, true) as f64 * wa, lb_ - lanes_of_part(net, &pb, true) as f64 * wb);
        if lb_ != la { al.steps.push((sigma - SHIFT_M / 2.0, sigma + SHIFT_M / 2.0, lb_ - la)); }
        if rb != ra { ar.steps.push((sigma - SHIFT_M / 2.0, sigma + SHIFT_M / 2.0, rb - ra)); }
        // shoulders follow the class
        let shb = net.shoulder_w(pb.edge);
        if shb.0 != cur_sh.0 {
            slots[rsh_cur].prog.steps.push((sigma - 15.0, sigma + 15.0, shb.0 - cur_sh.0));
            if !oneway { slots[lsh_cur].prog.steps.push((sigma - 15.0, sigma + 15.0, shb.0 - cur_sh.0)); }
        }
        if oneway && shb.1 != cur_sh.1 { slots[lsh_cur].prog.steps.push((sigma - 15.0, sigma + 15.0, shb.1 - cur_sh.1)); }
        cur_sh = shb;
        // lane width change of kept lanes
        if wb != cur_w {
            for (_t, g) in groups.iter() { for &id in &g.cur { slots[id].prog.steps.push((sigma - SHIFT_M / 2.0, sigma + SHIFT_M / 2.0, wb - cur_w)); } }
            cur_w = wb;
        }
        if let Some(fi) = net.fork_at[node] {
            // a fork on this stroke (it is the trunk + through); the branch is another stroke
            let f = &net.forks[fi];
            let (diverge, right, lb, lm, w, tb_n) = (f.diverge, f.right, f.lb as usize, f.lm as usize, f.w, f.tb_n);
            // the gore's width from the geometry: the branch line's separation beyond the threshold (stations along the stroke)
            let mut gcap: Vec<(f64, f64)> = f.sep.iter().map(|&(x, v)| (if f.diverge { sigma + x } else { sigma - x }, v)).collect();
            gcap.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
            let (s_g, s_n, t0, t1) = (sigma + f.s_g, sigma + f.s_n, sigma + f.taper0, sigma + f.taper1);
            let bstroke = net.edge_stroke[f.branch.0].0;
            let g = &mut groups[0].1;
            stations.extend([s_g, s_n, t0, t1, (s_n + 30.0).min(len)]);
            let lo = if diverge { t0.min(s_g) - MARGIN_M } else { s_n };
            let hi = if diverge { s_n } else { t1.max(s_g) + MARGIN_M };
            dense.push((lo, hi));
            if !right { rwin.push((lo - MARGIN_M, hi + MARGIN_M)); }
            let (sh_r, _sh_l) = net.shoulder_w(pb.edge);
            if diverge {
                let lt = g.cur.len();
                let existing = if lt > lm { (lt - lm).min(lb) } else { 0 };
                let mut branch: Vec<usize> = if right { g.cur[lt - existing..].to_vec() } else { g.cur[..existing].to_vec() };
                for _ in existing..lb {
                    let id = slots.len();
                    slots.push(Slot { kind: K_LANE, group: 0, prog: Prog { w0: 0.0, steps: vec![(t0, t1, w)] }, alive: (t0, inf), cap: vec![] });
                    if right { let a = *branch.last().or(g.cur.last()).unwrap(); insert_after(&mut order, Some(a), 0, id); branch.push(id); }
                    else { let b = *branch.first().or(g.cur.first()).unwrap(); insert_after(&mut order, None, b, id); branch.insert(0, id); }
                }
                for &id in &branch { if slots[id].alive.1 > s_n { slots[id].alive.1 = s_n; } }
                let through: Vec<usize> = g.cur.iter().copied().filter(|x| !branch.contains(x)).collect();
                // gore between the through lanes and the branch; a new shoulder beyond the nose
                let gore = slots.len();
                slots.push(Slot { kind: K_GORE, group: 0, prog: Prog { w0: 60.0, steps: vec![] }, alive: (s_g, s_n), cap: gcap.clone() });
                let sh2 = slots.len();
                slots.push(Slot { kind: K_SHOULDER, group: 9, prog: Prog { w0: 0.0, steps: vec![(s_n, (s_n + 30.0).min(len), if right { sh_r } else { 1.0 })] }, alive: (s_n, inf), cap: vec![] });
                let old_sh = if right { rsh_cur } else { lsh_cur };
                if slots[old_sh].alive.1 > s_n { slots[old_sh].alive.1 = s_n; }
                if right {
                    let a = *through.last().unwrap_or(&lsh_cur);
                    insert_after(&mut order, Some(a), 0, sh2);
                    insert_after(&mut order, Some(sh2), 0, gore);
                    rsh_cur = sh2;
                } else {
                    let b = *through.first().unwrap_or(&rsh_cur);
                    insert_after(&mut order, None, b, sh2);
                    insert_after(&mut order, None, sh2, gore);
                    lsh_cur = sh2;
                }
                // the branch's first row: branch lanes + the old outer shoulder (right) / old shoulder + lanes (left)
                let (k0, k1) = if right { (branch[0], old_sh) } else { (old_sh, *branch.last().unwrap()) };
                if s_n < len - 0.5 {
                    snaps_out.push((bstroke, true, Snap { trunk: si, s: s_n, k0, k1, tb: tb_n, joint: false }));
                } else { net.stats.nose_beyond += 1; }
                g.cur = through;
            } else {
                // merge: branch lanes from the nose, the gore from the nose to the theoretical gore
                let mut branch: Vec<usize> = Vec::new();
                for _ in 0..lb {
                    let id = slots.len();
                    slots.push(Slot { kind: K_LANE, group: 0, prog: Prog { w0: w, steps: vec![] }, alive: (s_n, inf), cap: vec![] });
                    branch.push(id);
                }
                let gore = slots.len();
                slots.push(Slot { kind: K_GORE, group: 0, prog: Prog { w0: 60.0, steps: vec![] }, alive: (s_n, s_g), cap: gcap.clone() });
                let sh2 = slots.len();
                slots.push(Slot { kind: K_SHOULDER, group: 9, prog: Prog { w0: if right { sh_r } else { 1.0 }, steps: vec![] }, alive: (s_n, inf), cap: vec![] });
                let old_sh = if right { rsh_cur } else { lsh_cur };
                if slots[old_sh].alive.1 > s_n { slots[old_sh].alive.1 = s_n; }
                if right {
                    let a = old_sh;
                    insert_after(&mut order, Some(a), 0, gore);
                    let mut prev = gore;
                    for &id in &branch { insert_after(&mut order, Some(prev), 0, id); prev = id; }
                    insert_after(&mut order, Some(prev), 0, sh2);
                    rsh_cur = sh2;
                } else {
                    let b = old_sh;
                    insert_after(&mut order, None, b, gore);
                    let mut nxt = gore;
                    for &id in branch.iter().rev() { insert_after(&mut order, None, nxt, id); nxt = id; }
                    insert_after(&mut order, None, nxt, sh2);
                    lsh_cur = sh2;
                }
                let (k0, k1) = if right { (branch[0], sh2) } else { (sh2, *branch.last().unwrap()) };
                if s_n > 0.5 { snaps_out.push((bstroke, false, Snap { trunk: si, s: s_n, k0, k1, tb: tb_n, joint: false })); } else { net.stats.nose_beyond += 1; }
                // after the node: lt lanes; the branch lanes it carries go on, the rest taper out
                let lt = lanes_of_part(net, &pb, true).max(1) as usize;
                let keep = if lt > lm { (lt - lm).min(lb) } else { 0 };
                let (kept, ends): (Vec<usize>, Vec<usize>) = if right {
                    (branch[..keep].to_vec(), branch[keep..].to_vec())
                } else { (branch[lb - keep..].to_vec(), branch[..lb - keep].to_vec()) };
                for &id in &ends {
                    slots[id].prog.steps.push((t0, t1, -w));
                    slots[id].alive.1 = t1;
                }
                if right { g.cur.extend(kept); } else { let mut v = kept; v.extend(g.cur.iter().copied()); g.cur = v; }
            }
            continue;
        }
        // a plain joint: lane count changes per direction group
        for gi in 0..groups.len() {
            let fwd_travel = groups[gi].0;
            // travel order through the node: incoming part / outgoing part
            let (pin, pout) = if fwd_travel { (pa, pb) } else { (pb, pa) };
            let li = lanes_of_part(net, &pin, fwd_travel).max(1);
            let lo = lanes_of_part(net, &pout, fwd_travel).max(1);
            if li == lo { continue; }
            let add = lo > li;
            let dl = (if add { lo - li } else { li - lo }) as usize;
            let (si_, so_) = (net.seg_of_edge(pin.edge).clone(), net.seg_of_edge(pout.edge).clone());
            // the v3 walks from the node (travel): incoming arm upstream, outgoing downstream
            let arm_in = (pin.edge, if fwd_travel { !pin.fwd } else { pin.fwd });
            let arm_out = (pout.edge, if fwd_travel { pout.fwd } else { !pout.fwd });
            let region = net.region[net.edges[pout.edge].seg];
            let (wide, wide_fwd, narrow) = if add { (&so_, arm_out.1, &si_) } else { (&si_, !arm_in.1, &so_) };
            let need_walk = wide.place.is_empty() && turn_dir(wide, wide_fwd).is_empty();
            let (up, dn) = if need_walk { (net.walk(node, arm_in, FEED_M + 10.0), net.walk(node, arm_out, FEED_M + 10.0)) }
                           else { (Path { line: Line::new(vec![[0.0, 0.0]]), pieces: vec![] }, Path { line: Line::new(vec![[0.0, 0.0]]), pieces: vec![] }) };
            let (right_drv, why, feed) = net.add_side(wide, wide_fwd, narrow, &dn, &up, add, region);
            let kmh = si_.kmh.max(so_.kmh);
            let w = net.lane_w(if add { pout.edge } else { pin.edge });
            let len_t = match (why, feed) {
                ("bay", _) => mutcd_m(w * dl as f64, kmh),
                (_, Some((_s, true))) => { let v = net.t(region, T_EXIT_K) * w; v.max(net.t(region, T_EXIT_MIN)) }
                (_, Some((_s, false))) => { let v = net.t(region, T_ENTRY_K) * w; v.max(net.t(region, T_ENTRY_MIN)) }
                _ => { let kk = net.t(region, T_DROP_K); if kk > 0.0 { kk * w * dl as f64 } else { mutcd_m(w * dl as f64, kmh) } }
            };
            let (tt0, tt1) = if why == "bay" { (-len_t / 2.0, len_t / 2.0) }
                             else if add { let pl = net.t(region, T_ADD_PLACE); (-pl * len_t, (1.0 - pl) * len_t) } else { (0.0, len_t) };
            // travel -> stations; the driver's right -> the stroke's right for the forward group, left for the other
            let (a, b) = if fwd_travel { (sigma + tt0, sigma + tt1) } else { (sigma - tt1, sigma - tt0) };
            let (a, b) = (a.max(-1.0), b.min(len + 1.0));
            let stroke_right = if fwd_travel { right_drv } else { !right_drv };
            // in station order the group gains lanes when (add and forward) or (drop and backward)
            let gains = add == fwd_travel;
            if add { net.stats.adds += 1; } else { net.stats.drops += 1; }
            stations.extend([a.max(0.0), b.min(len)]);
            dense.push((a - MARGIN_M, b + MARGIN_M));
            if oneway && !stroke_right { rwin.push((a - MARGIN_M, b + MARGIN_M)); }
            let g = &mut groups[gi].1;
            if gains {
                for _ in 0..dl {
                    let id = slots.len();
                    slots.push(Slot { kind: K_LANE, group: g.group, prog: Prog { w0: 0.0, steps: vec![(a, b, w)] }, alive: (a, inf), cap: vec![] });
                    if stroke_right {
                        let at = *g.cur.last().unwrap();
                        insert_after(&mut order, Some(at), 0, id);
                        g.cur.push(id);
                    } else {
                        let at = *g.cur.first().unwrap();
                        insert_after(&mut order, None, at, id);
                        g.cur.insert(0, id);
                    }
                }
            } else {
                let n = g.cur.len();
                let dl = dl.min(n.saturating_sub(1));
                let gone: Vec<usize> = if stroke_right { g.cur.drain(n - dl..).collect() } else { g.cur.drain(..dl).collect() };
                for id in gone {
                    let wa = slots[id].prog.at(a).max(0.0);
                    slots[id].prog.steps.push((a, b, -wa));
                    slots[id].alive.1 = b;
                }
            }
        }
    }
    let mut over: Vec<(usize, f64, f64, u8)> = Vec::new();
    if net.xsec_on { xsec_slots(net, &parts, oneway, &mut slots, &mut order, &mut over); }
    // the stroke's own slots in order (re-indexed), snaps re-indexed below
    let remap: Vec<usize> = { let mut r = vec![0; slots.len()]; for (k, &id) in order.iter().enumerate() { r[id] = k; } r };
    let out_slots: Vec<Slot> = order.iter().map(|&id| slots[id].clone()).collect();
    let snaps_out = snaps_out.into_iter().map(|(b, at, s)| (b, at, Snap { k0: remap[s.k0], k1: remap[s.k1], ..s })).collect();
    let over = over.into_iter().map(|(k, a, b, w)| (remap[k], a, b, w)).collect();
    stations.retain(|&s| s >= 0.0 && s <= len);
    (out_slots, al, ar, rwin, stations, dense, snaps_out, over)
}

/// The TAPER row of segment s (build_opts' rule).
fn region_of(s: &Seg) -> usize {
    let r = s.region.max(0) as usize;
    if r == 0 && s.code.eq_ignore_ascii_case("JP") { 6 } else if r < 6 { r } else { 0 }
}

/// (BNE_ROADSYS_XSEC) segment s's layout with its cross-section (sides in its own direction), None without one that
/// shapes it.
pub fn seg_lay(s: &Seg) -> Option<Lay> {
    let region = region_of(s);
    let lht = TAPER[region][T_LHT] > 0.5;
    let c = xsec::Ctx { code: &s.code, cls: &s.cls_s, urban: s.urban, oneway: s.dir != 0 };
    let x = xsec::parse(&s.xsec, &c, s.dir, lht)?;
    if !x.shapes() { return None; }
    let n = if s.dir != 0 { lanes_eff(s, true) } else { lanes_eff(s, true) + lanes_eff(s, false) };
    let (r, l) = shoulder_of(s);
    let sh = if s.dir == 0 { (r, r) } else if s.dir > 0 { (l, r) } else { (r, l) };
    Some(xsec::layout_w(&x, &c, n, lane_w_of(s, region), sh, s.lanes >= 1, s.cls >= 1 && s.cls <= CLS_TRUNK, s.bridge.is_empty()))
}

/// (BNE_ROADSYS_XSEC) the half extent of segment s's carriageway with its cross-section, from its mapped line (lanes
/// and strips centred, the wider shoulder): what a deck drawn from it covers (the deck ends' hand-over reads it).
pub fn xsec_half_extent(s: &Seg) -> Option<f64> {
    let lay = seg_lay(s)?;
    let n = if s.dir != 0 { lanes_eff(s, true) } else { lanes_eff(s, true) + lanes_eff(s, false) };
    let lw = if lay.lane_w > 0.0 { clampf(lay.lane_w, 2.2, 5.0) } else { lane_w_of(s, region_of(s)) };
    let inner = n.max(1) as f64 * lw + lay.left.strips() + lay.right.strips();
    Some(inner / 2.0 + lay.left.shoulder.max(lay.right.shoulder))
}

/// (BNE_ROADSYS_XSEC) every segment's cross-section (xsec.rs): its tags read, its strips and lanes laid out against
/// today's lane width and shoulders.
fn cross_sections(net: &mut Net) {
    for g in 0..net.segs.len() {
        let s = &net.segs[g];
        let lht = TAPER[net.region[g]][T_LHT] > 0.5;
        let c = xsec::Ctx { code: &s.code, cls: &s.cls_s, urban: s.urban, oneway: s.dir != 0 };
        let Some(x) = xsec::parse(&s.xsec, &c, s.dir, lht) else { continue };
        net.stats.xsec_segs += 1;
        let n = if s.dir != 0 { lanes_eff(s, true) } else { lanes_eff(s, true) + lanes_eff(s, false) };
        let (r, l) = shoulder_of(s);
        // (today's shoulders in the segment's direction: a two-way's right one on both sides, build_slots')
        let sh = if s.dir == 0 { (r, r) } else if s.dir > 0 { (l, r) } else { (r, l) };
        // (a segment with a structure never wider than today's: its deck meets today's approaches and abutments)
        let lay = xsec::layout_w(&x, &c, n, net.lane_w_today(g), sh, s.lanes >= 1, s.cls >= 1 && s.cls <= CLS_TRUNK, s.bridge.is_empty());
        net.xs_tags[g] = lay.tags;
        if !x.shapes() { continue; }
        net.stats.xsec_shaped += 1;
        if lay.lane_w > 0.0 { net.stats.xsec_width += 1; }
        if lay.reject > 0 { net.stats.xsec_reject += 1; }
        net.lays[g] = Some(lay);
    }
}

/// (BNE_ROADSYS_XSEC) a stroke's strip slots: on each side between the lanes and the shoulder, from the lanes out,
/// buffer, cycle lane, parking (every stroke has them, zero wide where nothing is mapped, so strokes meeting at a cut
/// keep equal rows); each part's widths, changing at a joint over STRIP_STEP_M; the shoulders the cross-section's
/// where a part has one. A stroke with a fork event keeps empty strips and today's shoulders (recorded).
fn xsec_slots(net: &mut Net, parts: &[Part], oneway: bool, slots: &mut Vec<Slot>, order: &mut Vec<usize>,
              over: &mut Vec<(usize, f64, f64, u8)>) {
    const STRIP_STEP_M: f64 = 15.0;
    let inf = f64::INFINITY;
    let fork = slots.iter().any(|k| k.kind == K_GORE);
    let vals: Vec<Option<(SideW, SideW)>> = parts.iter().map(|p| net.xs_part(p)).collect();
    if fork && vals.iter().any(|v| v.is_some()) { net.stats.xsec_fork_skip += 1; }
    let vals: Vec<Option<(SideW, SideW)>> = if fork { vec![None; parts.len()] } else { vals };
    let today: Vec<(f64, f64)> = parts.iter().map(|p| { let (r, l) = net.shoulder_w(p.edge); if oneway { (l, r) } else { (r, r) } }).collect();
    let val = |k: usize, left: bool, f: &dyn Fn(&SideW) -> f64| -> f64 {
        match vals[k] { Some((l, r)) => f(if left { &l } else { &r }), None => 0.0 }
    };
    let prog = |left: bool, f: &dyn Fn(&SideW) -> f64, half: f64, base: &dyn Fn(usize) -> f64| -> Prog {
        let v = |k: usize| if vals[k].is_some() { val(k, left, f) } else { base(k) };
        let mut pr = Prog { w0: v(0), steps: vec![] };
        for k in 0..parts.len().saturating_sub(1) {
            let (a, b) = (v(k), v(k + 1));
            if b != a { let sg = parts[k].s1; pr.steps.push((sg - half, sg + half, b - a)); }
        }
        pr
    };
    let zero = |_k: usize| 0.0;
    let mk = |slots: &mut Vec<Slot>, kind: u8, pr: Prog| -> usize {
        slots.push(Slot { kind, group: 9, prog: pr, alive: (-inf, inf), cap: vec![] });
        slots.len() - 1
    };
    let fb = |w: &SideW| w.buffer;
    let fc = |w: &SideW| w.cycle;
    let fp = |w: &SideW| w.park;
    let (pl, cl, bl) = (mk(slots, xsec::K_PARK, prog(true, &fp, STRIP_STEP_M / 2.0, &zero)),
                        mk(slots, xsec::K_CYCLE, prog(true, &fc, STRIP_STEP_M / 2.0, &zero)),
                        mk(slots, xsec::K_BUFFER, prog(true, &fb, STRIP_STEP_M / 2.0, &zero)));
    let (br, cr, pr) = (mk(slots, xsec::K_BUFFER, prog(false, &fb, STRIP_STEP_M / 2.0, &zero)),
                        mk(slots, xsec::K_CYCLE, prog(false, &fc, STRIP_STEP_M / 2.0, &zero)),
                        mk(slots, xsec::K_PARK, prog(false, &fp, STRIP_STEP_M / 2.0, &zero)));
    // [left shoulder, park, cycle, buffer, ... lanes ..., buffer, cycle, park, right shoulder]
    for id in [bl, cl, pl] { order.insert(1, id); }
    for id in [br, cr, pr] { let n = order.len(); order.insert(n - 1, id); }
    if vals.iter().all(|v| v.is_none()) { return; }
    // the shoulders (slots 0 and 1: no fork, so the stroke's only ones)
    let fs = |w: &SideW| w.shoulder;
    slots[0].prog = prog(true, &fs, 15.0, &|k| today[k].0);
    slots[1].prog = prog(false, &fs, 15.0, &|k| today[k].1);
    // overrides per part: unpaved shoulders, advisory cycle lanes, bus lanes (the side's outermost lane alive there)
    for (k, p) in parts.iter().enumerate() {
        let Some((l, r)) = vals[k] else { continue };
        let mid = (p.s0 + p.s1) / 2.0;
        for (left, w, sh, cyc) in [(true, l, 0usize, cl), (false, r, 1usize, cr)] {
            if w.soft { over.push((sh, p.s0, p.s1, xsec::K_SOFT)); }
            if w.adv && w.cycle > 0.0 { over.push((cyc, p.s0, p.s1, OVER_ADV)); }
            if w.bus {
                let alive = |id: &usize| slots[*id].kind == K_LANE && mid >= slots[*id].alive.0 && mid <= slots[*id].alive.1;
                let lane = if left { order.iter().find(|id| alive(id)) } else { order.iter().rev().find(|id| alive(id)) };
                if let Some(&id) = lane { over.push((id, p.s0, p.s1, xsec::K_BUS)); }
            }
        }
    }
}

/// m of clearance under a structure over lower road seg at its arc fraction x (M5, the user 2026-11-24: "use real when
/// can"): its height limit there (a maxheight sign, + SIGN_MARGIN_M; within SIGN_MIN_M..SIGN_MAX_M, roads.road_sign_at's
/// range), else the unsigned clearance its class keeps (roads.LOWER_CLEAR_M, the 2026-11-17 clearance study): main
/// roads by country (US 16 ft, UK 16 ft 6 in, Germany 4.50 + 0.20 m, France 4.75 m, Canada 5.0 m, Japan 4.5 m), other
/// public roads 4.5 m (France 4.3 m, the UK and Canada their main roads'), service roads and tracks 4.0 m, foot, cycle
/// and bridle ways 2.5 m. (Was today's 6.15 m headroom over every road: decks lifted over farm tracks 1.7 m too high.)
pub fn clearance_of(seg: &Seg, x: f64) -> f64 {
    if let Some(h) = seg.maxh.iter().filter(|m| x >= m.0 - 1e-6 && x <= m.1 + 1e-6 && m.2 >= SIGN_MIN_M && m.2 <= SIGN_MAX_M)
        .map(|m| m.2).fold(None, |a: Option<f64>, h| Some(a.map_or(h, |a| a.min(h)))) {
        return h + SIGN_MARGIN_M;
    }
    let c = seg.code.to_ascii_uppercase();
    let main = |c: &str| match c { "CA" => 5.0, "DE" => 4.7, "FR" => 4.75, "GB" => 5.03, "JP" => 4.5, "NL" => 4.5, _ => 4.9 };
    let cls = seg.cls_s.as_str();
    let base = cls.strip_suffix("_link").unwrap_or(cls);
    match base {
        "motorway" | "trunk" | "primary" => main(&c),
        "service" | "track" => 4.0,
        "footway" | "path" | "cycleway" | "pedestrian" | "bridleway" | "steps" | "corridor" => 2.5,
        _ => match c.as_str() { "GB" => 5.03, "CA" => 5.0, "DE" => 4.7, "FR" => 4.3, "JP" => 4.5, _ => 4.5 },
    }
}

/// The lower seg and its arc fraction at station s of stroke si (clearance_of's input).
fn seg_at(net: &Net, si: usize, s: f64) -> (&Seg, f64) {
    let st = &net.strokes[si];
    let p = st.parts.iter().find(|p| s >= p.s0 - 1e-6 && s <= p.s1 + 1e-6).unwrap_or(&st.parts[0]);
    let ed = &net.edges[p.edge];
    let seg = &net.segs[ed.seg];
    let l = Line::new(seg.pts.clone());
    let (aa, ab) = (l.s[ed.va], l.s[ed.vb]);
    let x = if p.fwd { aa + (s - p.s0) } else { ab - (s - p.s0) };
    let len = l.len();
    (seg, if len > 0.0 { clampf(x / len, 0.0, 1.0) } else { 0.0 })
}

/// m of clearance under a structure over stroke si at station s (clearance_of).
pub fn clearance_at(net: &Net, si: usize, s: f64) -> f64 { let (g, x) = seg_at(net, si, s); clearance_of(g, x) }

pub const SIGN_MARGIN_M: f64 = 0.25;     // roads.SIGN_MARGIN_M
pub const SIGN_MIN_M: f64 = 2.0;         // roads.SIGN_MIN_M
pub const SIGN_MAX_M: f64 = 8.0;         // roads.SIGN_MAX_M

/// (M5) The structure strokes' crossings of the railway lines round the read (place.rails: lines at ground level),
/// each a floor of the line's clearance + the structure's depth; strokes in index order, crossings along each in
/// station order.
fn rail_floors(net: &mut Net) {
    let mut out = Vec::new();
    for (si, st) in net.strokes.iter().enumerate() {
        if !st.sup.iter().any(|u| u.kind == SUP_STRUCT) { continue; }
        let mut here: Vec<RailFloor> = Vec::new();
        for (rp, clear) in &net.rails {
            for w in rp.windows(2) {
                for k in 0..st.line.p.len().saturating_sub(1) {
                    let (a, b) = (st.line.p[k], st.line.p[k + 1]);
                    let (c, d) = (w[0], w[1]);
                    let r = sub(b, a);
                    let q = sub(d, c);
                    let den = cross(r, q);
                    if absf(den) < 1e-12 { continue; }
                    let t = cross(sub(c, a), q) / den;
                    let u = cross(sub(c, a), r) / den;
                    if !(t >= 0.0 && t <= 1.0 && u >= 0.0 && u <= 1.0) { continue; }
                    let s = st.line.s[k] + (st.line.s[k + 1] - st.line.s[k]) * t;
                    let su = st.support_at(s);
                    if su.kind != SUP_STRUCT { continue; }
                    here.push(RailFloor { si, s, p: lerp(a, b, t), floor: clear + deck_under(su) });
                }
            }
        }
        here.sort_by(|x, y| x.s.partial_cmp(&y.s).unwrap());
        out.extend(here);
    }
    net.stats.rail_floors = out.len();
    net.rail_floors = out;
}

/// m a floor adds under the clearance for a structure: the depth the deck builder draws under it (its segments' own,
/// Seg.depth: the inventory's main span or the part's geometry, roads.deck_depth) + LEVEL_MARGIN_M; DECK_FULL_M when
/// not known (M3; M2 took DECK_FULL_M everywhere).
pub fn deck_under(u: Sup) -> f64 { if u.kind == SUP_STRUCT && u.depth > 0.0 { u.depth + LEVEL_MARGIN_M } else { DECK_FULL_M } }

/// The crossing graph: every pair of strokes crossing in plan without a shared node, with which is above and its
/// floor (clearance of the lower by country and class + deck depth).
fn crossings(net: &mut Net) {
    const CELL: f64 = 32.0;
    let mut grid: std::collections::BTreeMap<(i64, i64), Vec<(usize, usize)>> = std::collections::BTreeMap::new();
    for (si, st) in net.strokes.iter().enumerate() {
        for k in 0..st.line.p.len().saturating_sub(1) {
            let (a, b) = (st.line.p[k], st.line.p[k + 1]);
            let (x0, x1) = ((a[0].min(b[0]) / CELL).floor() as i64, (a[0].max(b[0]) / CELL).floor() as i64);
            let (y0, y1) = ((a[1].min(b[1]) / CELL).floor() as i64, (a[1].max(b[1]) / CELL).floor() as i64);
            for gx in x0..=x1 { for gy in y0..=y1 { grid.entry((gx, gy)).or_default().push((si, k)); } }
        }
    }
    let mut seen = std::collections::BTreeSet::new();
    let mut out = Vec::new();
    let mut unbridged = 0;
    for (_c, list) in &grid {
        for i in 0..list.len() {
            for j in (i + 1)..list.len() {
                let (sa, ka) = list[i];
                let (sb, kb) = list[j];
                if sa == sb { continue; }
                let (sa, ka, sb, kb) = if sa < sb { (sa, ka, sb, kb) } else { (sb, kb, sa, ka) };
                if !seen.insert((sa, ka, sb, kb)) { continue; }
                let (la, lb) = (&net.strokes[sa].line, &net.strokes[sb].line);
                let Some((u, v)) = seg_x(la.p[ka], la.p[ka + 1], lb.p[kb], lb.p[kb + 1]) else { continue };
                let s_a = la.s[ka] + (la.s[ka + 1] - la.s[ka]) * u;
                let s_b = lb.s[kb] + (lb.s[kb + 1] - lb.s[kb]) * v;
                // at a shared node (a stroke end or an inner node): not a crossing
                let p = lerp(la.p[ka], la.p[ka + 1], u);
                let near_end = |st: &Stroke, s: f64| s < 0.5 || s > st.len() - 0.5;
                if near_end(&net.strokes[sa], s_a) || near_end(&net.strokes[sb], s_b) { continue; }
                let (ua, ub) = (net.strokes[sa].support_at(s_a), net.strokes[sb].support_at(s_b));
                let upper = if ua.kind == SUP_STRUCT && ub.kind != SUP_STRUCT { Some(sa) }
                    else if ub.kind == SUP_STRUCT && ua.kind != SUP_STRUCT { Some(sb) }
                    else if ub.kind == SUP_TUNNEL && ua.kind != SUP_TUNNEL { Some(sa) }
                    else if ua.kind == SUP_TUNNEL && ub.kind != SUP_TUNNEL { Some(sb) }
                    else if ua.level > ub.level { Some(sa) } else if ub.level > ua.level { Some(sb) } else { None };
                let Some(up) = upper else { unbridged += 1; continue; };
                let lo = if up == sa { sb } else { sa };
                let lo_sup = if up == sa { ub } else { ua };
                // (M3: two carriageways meeting at a shallow angle where the lower one's own structure, at the upper's
                // level or above, begins within the stretch their paved areas overlap: both are on the deck there (a
                // ramp's deck merging beside the main deck, its bridge tag 2 m short of the crossing), not a grade
                // separation: no floor. A 7.3 m floor between them sank Ottawa 417's ramp 4 m under its ground.)
                if lo_sup.kind == SUP_GROUND {
                    let (da, db) = (sub(la.p[ka + 1], la.p[ka]), sub(lb.p[kb + 1], lb.p[kb]));
                    let (na, nb) = (norm(da), norm(db));
                    let sin = if na > 1e-9 && nb > 1e-9 { absf(cross(da, db)) / (na * nb) } else { 1.0 };
                    if sin < SHALLOW_SIN {
                        let (s_up, s_lo) = if up == sa { (s_a, s_b) } else { (s_b, s_a) };
                        let mut off = Vec::new();
                        net.strokes[up].offsets(s_up, &mut off);
                        let hu = if off.is_empty() { 3.5 } else { off[0].max(-off[off.len() - 1]) };
                        net.strokes[lo].offsets(s_lo, &mut off);
                        let hl = if off.is_empty() { 3.5 } else { off[0].max(-off[off.len() - 1]) };
                        let reach = (hu + hl) / sin.max(0.05) + 5.0;
                        let up_lvl = net.strokes[up].support_at(s_up).level;
                        let beside = net.strokes[lo].sup.iter().any(|u| u.kind == SUP_STRUCT && u.level >= up_lvl
                            && s_lo >= u.s0 - reach && s_lo <= u.s1 + reach);
                        if beside { unbridged += 1; continue; }
                    }
                }
                let clear = clearance_at(net, lo, if up == sa { s_b } else { s_a });
                // (M3: nothing held over a tunnel: today's tunnels own their heights and cover (TunnelProfiles); an 8 m
                // cover floor lifted a speedway's deck over its infield tunnel 12 m)
                // (T1, Opts.tunnels: the tunnel record builds every tagged tunnel covered, so a road crossing over one
                // holds its floor: the tunnel road's clearance + 0.3 m for lights and signs, at least the lowest lid
                // the mesher draws, + its roof and soil (TUNNEL_ROOF_M); the tunnel's knots, smoothness only, take it)
                if lo_sup.kind == SUP_TUNNEL && !net.opts.tunnels { unbridged += 1; continue; }
                let up_sup = if up == sa { ua } else { ub };
                // (631: only a road on the ground holds it: a deck over a tagged tunnel is a covered roadway (section
                // type 2, T4), today's as before; its floor moved Seal Beach's connector decks onto each other)
                if lo_sup.kind == SUP_TUNNEL && up_sup.kind == SUP_STRUCT { unbridged += 1; continue; }
                let floor = if lo_sup.kind == SUP_TUNNEL {
                    if net.opts.tunnels { (clear + TUNNEL_LIGHTS_M).max(TUNNEL_LID_MIN_M) + TUNNEL_ROOF_M } else { 8.0 }
                } else { clear + deck_under(up_sup) };
                let stacked = lo_sup.kind == SUP_STRUCT;
                out.push(Crossing { a: sa, sa: s_a, b: sb, sb: s_b, p, upper: up, floor, along: false, stacked });
            }
        }
    }
    // stacked along: a structure running above a lower road (a viaduct over a street) gets a floor every 10 m
    // where the lower road's line lies within 12 m beside its line and runs within 45 deg of it
    let mut along = 0usize;
    let mut offa: Vec<f64> = Vec::new();
    let mut offb: Vec<f64> = Vec::new();
    for (sa, st) in net.strokes.iter().enumerate() {
        for u in st.sup.iter().filter(|u| u.kind == SUP_STRUCT) {
            let mut s = u.s0 + 15.0;
            while s < u.s1 - 15.0 {
                let (p, t) = st.line.at(s);
                let (gx, gy) = ((p[0] / CELL).floor() as i64, (p[1] / CELL).floor() as i64);
                let mut best: Option<(f64, usize, f64)> = None;
                for dx in -1..=1 { for dy in -1..=1 {
                    let Some(list) = grid.get(&(gx + dx, gy + dy)) else { continue };
                    for &(sb, kb) in list {
                        if sb == sa { continue; }
                        let lb = &net.strokes[sb].line;
                        let (q0, q1) = (lb.p[kb], lb.p[kb + 1]);
                        let ab = sub(q1, q0);
                        let l2 = dot(ab, ab);
                        if l2 <= 1e-9 { continue; }
                        if absf(dot(ab, t)) < 0.7071 * l2.sqrt() { continue; }
                        let uu = clampf(dot(sub(p, q0), ab) / l2, 0.0, 1.0);
                        let q = lerp(q0, q1, uu);
                        let d = norm(sub(q, p));
                        if d > 30.0 { continue; }
                        let s_b = lb.s[kb] + (lb.s[kb + 1] - lb.s[kb]) * uu;
                        // under the deck: the two paved extents overlap in plan by more than 1 m
                        st.offsets(s, &mut offa);
                        net.strokes[sb].offsets(s_b, &mut offb);
                        let left_of_a = cross(t, sub(q, p)) > 0.0;
                        let ea = if left_of_a { offa[0] } else { -offa[offa.len() - 1] };
                        let (_pb, tb) = lb.at(s_b);
                        let a_left_of_b = cross(tb, sub(p, q)) > 0.0;
                        let eb = if a_left_of_b { offb[0] } else { -offb[offb.len() - 1] };
                        // (M2: under it, not beside it: the paved extents overlap in plan by at least half the
                        // narrower one's width; two ramps side by side at two OSM levels, 1-2 m into each other, took a
                        // 5.5 m floor between them, Seal Beach)
                        if ea + eb - d < 0.5 * (2.0 * ea).min(2.0 * eb).max(2.0) { continue; }
                        let ub = net.strokes[sb].support_at(s_b);
                        if ub.kind == SUP_STRUCT && ub.level >= u.level { continue; }
                        if ub.kind == SUP_TUNNEL { continue; }
                        // (M3: over another deck only when its line lies over that deck's pavement, a double deck;
                        // twin viaducts side by side at two OSM layers were held 8.45 m apart, Baltimore's I-95 decks
                        // measured at one height lifted 18 m)
                        if ub.kind == SUP_STRUCT && d >= eb { continue; }
                        // (M3: nor where the lower road's own structure at this level or above begins or ends within
                        // ALONG_NEAR_M: a ramp's deck beside the main deck, its bridge tag a few metres short, is on a
                        // deck too; the 7.3 m floor sank Ottawa 417's ramp 4 m under its ground)
                        if net.strokes[sb].sup.iter().any(|w| w.kind == SUP_STRUCT && w.level >= u.level
                            && s_b >= w.s0 - ALONG_NEAR_M && s_b <= w.s1 + ALONG_NEAR_M) { continue; }
                        if best.map(|x| d < x.0).unwrap_or(true) { best = Some((d, sb, s_b)); }
                    }
                }}
                if let Some((_d, sb, s_b)) = best {
                    let lo_st = &net.strokes[sb];
                    let lo_sup = lo_st.support_at(s_b);
                    let clear = clearance_at(net, sb, s_b);
                    let floor = if lo_sup.kind == SUP_TUNNEL { 8.0 } else { clear + deck_under(*u) };
                    let (a, b2, s1, s2) = if sa < sb { (sa, sb, s, s_b) } else { (sb, sa, s_b, s) };
                    out.push(Crossing { a, sa: s1, b: b2, sb: s2, p, upper: sa, floor, along: true, stacked: lo_sup.kind == SUP_STRUCT });
                    along += 1;
                }
                s += 10.0;
            }
        }
    }
    net.stats.crossings = out.len();
    net.stats.unbridged = unbridged;
    net.stats.along = along;
    net.crossings = out;
}
