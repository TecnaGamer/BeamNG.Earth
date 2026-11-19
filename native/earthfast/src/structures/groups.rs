//! structure_groups (earthfast 480, road revamp RS step S1; 481 step S2a): which decks of one bridge chain form one
//! structure. structure_touch (481): the pairs a chain's closure must hold so every structure edge lies inside one
//! chain (one owner per structure).
//!
//! Union-find over the members in their given order (the caller's sorted keys; the smaller index is the root), on
//! footprints quantised to 1 mm, with the design's edge table ("Grouping"). Per unordered pair the first edge kind of
//! EDGE_ORDER that holds within its height limit is the pair's edge:
//!
//! | edge           | geometry                                                            | \|dz\| limit |
//! |----------------|---------------------------------------------------------------------|-------------|
//! | JOIN           | an end of each within JOIN_END_M                                     | 0.3 m       |
//! | END_ON         | a free end on the other's carriageway (not near its ends)           | 0.5 m       |
//! | OVERLAP_ALONG  | carriageways overlapping >= 2 m, within 30 degrees                    | 1.0 m       |
//! | OVERLAP_CROSS  | a centre line point on the other's carriageway, crossing > 30 deg   | 0.6 m       |
//! | BESIDE         | gap <= 2.5 m over min(20 m, 80 % of the shorter), within 25 degrees  | 1.0 m       |
//! | BESIDE_OUTLINE | gap 2.5-6 m over that length, both inside one OSM bridge outline    | 1.0 m       |
//!
//! An end is free (END_ON's) unless a JOIN was taken there (an end over another deck's end at another height stays
//! free); the footprint tests count only a's samples whose nearest point of b lies on b's span at least half a piece
//! inside its ends, never past them (two decks end to end are no length side by side; Codex review).
//!
//! Never a union (S0's design changes), recorded as a data conflict instead where the geometry and the height limit
//! held: two members of different OSM levels (a HARD rule: Queensboro's, Fort Pitt's, Brisbane's and Lower Wacker's
//! decks stand ~1 m apart in the game where they cross, so the height limits alone joined them), a walkway with a road,
//! rail with road, a tunnel with anything. A member outside the chain (no height of its own) that could be one
//! structure with a chain member (every never-rule passes, the geometry holds) is an "outside" conflict: the chain's
//! closure missed it (S2a: structure_touch closes it). One OSM level whose decks meet a geometry test farther apart
//! than CONFLICT_DZ is a data conflict too. Pairs whose geometry held but not the height limit are listed as rejected.
//!
//! Members are unique (S2a): a member of the same OSM way, kind and line as an earlier one (each vertex within DUP_M
//! of the other's line, both ways; one way kept under two part keys by two cells' copies, San Diego's c54ad200) is
//! that one's duplicate: in its group, in no test.
//!
//! Deterministic: basic IEEE operations and sqrt only (angles by their tangents, no libm, no FMA), BTreeMaps, every
//! loop in index order; a nearest segment's tie goes to the lower segment index.
use std::collections::BTreeMap;

use numpy::{PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

// edge kinds, in EDGE_ORDER
pub const JOIN: u8 = 0;
pub const END_ON: u8 = 1;
pub const OVERLAP_ALONG: u8 = 2;
pub const OVERLAP_CROSS: u8 = 3;
pub const BESIDE: u8 = 4;
pub const BESIDE_OUTLINE: u8 = 5;
// member kinds (structures.py's ROAD ... TUNNEL; a road and a walkway are told apart by kind != kind alone); a part
// near the chain but not in it (its height not solved here) is flagged outside (Member::outside), its own kind kept
#[allow(dead_code)]
pub const ROAD: i64 = 0;
#[allow(dead_code)]
pub const WALK: i64 = 1;
pub const RAIL: i64 = 2;
pub const TUNNEL: i64 = 3;
// data conflict reasons
pub const C_LEVEL: u8 = 0;
pub const C_DZ: u8 = 1;
pub const C_WALK: u8 = 2;
pub const C_RAIL: u8 = 3;
pub const C_TUNNEL: u8 = 4;
pub const C_OUTSIDE: u8 = 5;

pub const JOIN_END_M: f64 = 0.5;
pub const JOIN_DZ: f64 = 0.3;
pub const END_ON_DZ: f64 = 0.5;
pub const ALONG_MIN_M: f64 = 2.0;
pub const ALONG_DZ: f64 = 1.0;
pub const CROSS_DZ: f64 = 0.6;
pub const BESIDE_GAP_M: f64 = 2.5;
pub const BESIDE_MIN_M: f64 = 20.0;
pub const BESIDE_SHORT: f64 = 0.8;  // ... or this share of the shorter deck (E6 Vist's twins on one ~17 m deck)
pub const BESIDE_DZ: f64 = 1.0;
pub const OUTLINE_GAP_M: f64 = 6.0;
pub const TWIN_TOUCH_M: f64 = 0.5;  // m two BESIDE carriageways may overlap (one deck)
pub const CONFLICT_DZ: f64 = 2.0;   // m: one OSM level farther apart than this where a geometry test held
pub const STEP_M: f64 = 1.0;        // m between a member's samples (each stands for one piece of its length)
pub const DUP_M: f64 = 0.1;         // m: a member this near another of its way (every vertex, both ways): a duplicate
const TAN_ALONG: f64 = 0.5773502691896257;     // tan(30 degrees): OVERLAP_ALONG within, OVERLAP_CROSS over
const TAN_BESIDE: f64 = 0.4663076581549986;    // tan(25 degrees): BESIDE and BESIDE_OUTLINE within
const GRID_M: f64 = 16.0;

/// One member: its line (deduplicated after the 1 mm rounding), heights, arc lengths, carriageway half width.
pub struct Member {
    pub xy: Vec<[f64; 2]>,
    pub z: Vec<f64>,
    pub s: Vec<f64>,
    pub half: f64,
    pub kind: i64,
    pub level: f64,
    pub fid: i64,
    pub outside: bool,
}

fn mm(v: f64) -> f64 {
    (v * 1000.0).round() / 1000.0
}

impl Member {
    pub fn new(pts: &[[f64; 3]], half: f64, kind: i64, level: f64, fid: i64, outside: bool) -> Member {
        let (mut xy, mut z, mut s): (Vec<[f64; 2]>, Vec<f64>, Vec<f64>) = (Vec::new(), Vec::new(), Vec::new());
        for p in pts {
            let q = [mm(p[0]), mm(p[1])];
            if let Some(l) = xy.last() {
                if l[0] == q[0] && l[1] == q[1] {
                    continue;
                }
                let d = ((q[0] - l[0]) * (q[0] - l[0]) + (q[1] - l[1]) * (q[1] - l[1])).sqrt();
                s.push(s[s.len() - 1] + d);
            } else {
                s.push(0.0);
            }
            xy.push(q);
            z.push(mm(p[2]));
        }
        Member { xy, z, s, half: mm(half), kind, level, fid, outside }
    }

    fn len(&self) -> f64 {
        self.s.last().copied().unwrap_or(0.0)
    }
}

struct Seg {
    line: usize,
    a: [f64; 2],
    b: [f64; 2],
    za: f64,
    zb: f64,
    s0: f64,
    len: f64,
    u: [f64; 2],
}

struct Index {
    segs: Vec<Seg>,
    grid: BTreeMap<(i64, i64), Vec<usize>>,
    ends: Vec<(usize, usize)>,      // per line: its first and last segment (usize::MAX: none)
}

/// (segment, distance, t on it) of the nearest segment of one line
#[derive(Clone, Copy)]
struct Hit {
    seg: usize,
    dist: f64,
    t: f64,
    past: bool,         // the point lies past the line's own end (its nearest point is that end, clamped)
}

fn cell(v: f64) -> i64 {
    (v / GRID_M).floor() as i64
}

impl Index {
    fn new(ms: &[Member]) -> Index {
        let mut segs = Vec::new();
        let mut grid: BTreeMap<(i64, i64), Vec<usize>> = BTreeMap::new();
        let mut ends = vec![(usize::MAX, usize::MAX); ms.len()];
        for (li, m) in ms.iter().enumerate() {
            if m.xy.len() >= 2 {
                ends[li] = (segs.len(), segs.len() + m.xy.len() - 2);
            }
            for k in 0..m.xy.len().saturating_sub(1) {
                let (a, b) = (m.xy[k], m.xy[k + 1]);
                let len = m.s[k + 1] - m.s[k];
                let u = [(b[0] - a[0]) / len, (b[1] - a[1]) / len];
                let id = segs.len();
                segs.push(Seg { line: li, a, b, za: m.z[k], zb: m.z[k + 1], s0: m.s[k], len, u });
                for gx in cell(a[0].min(b[0]))..=cell(a[0].max(b[0])) {
                    for gy in cell(a[1].min(b[1]))..=cell(a[1].max(b[1])) {
                        grid.entry((gx, gy)).or_default().push(id);
                    }
                }
            }
        }
        Index { segs, grid, ends }
    }

    /// The nearest segment of every line with a segment within r of p: {line: hit} (ties: the lower segment).
    fn near(&self, p: [f64; 2], r: f64) -> BTreeMap<usize, Hit> {
        let mut best: BTreeMap<usize, Hit> = BTreeMap::new();
        for gx in cell(p[0] - r)..=cell(p[0] + r) {
            for gy in cell(p[1] - r)..=cell(p[1] + r) {
                let Some(v) = self.grid.get(&(gx, gy)) else { continue };
                for &k in v {
                    let sg = &self.segs[k];
                    let (dx, dy) = (sg.b[0] - sg.a[0], sg.b[1] - sg.a[1]);
                    let l2 = (dx * dx + dy * dy).max(1e-24);
                    let raw = ((p[0] - sg.a[0]) * dx + (p[1] - sg.a[1]) * dy) / l2;
                    let t = raw.clamp(0.0, 1.0);
                    let (first, last) = self.ends[sg.line];
                    let past = (k == first && raw < 0.0) || (k == last && raw > 1.0);
                    let (qx, qy) = (sg.a[0] + t * dx, sg.a[1] + t * dy);
                    let d = ((p[0] - qx) * (p[0] - qx) + (p[1] - qy) * (p[1] - qy)).sqrt();
                    if d > r {
                        continue;
                    }
                    let e = best.entry(sg.line).or_insert(Hit { seg: k, dist: d, t, past });
                    if d < e.dist || (d == e.dist && k < e.seg) {
                        *e = Hit { seg: k, dist: d, t, past };
                    }
                }
            }
        }
        best
    }

    fn at(&self, h: &Hit) -> ([f64; 2], f64, f64) {
        // (the point, its height, the line's arc there)
        let sg = &self.segs[h.seg];
        ([sg.a[0] + h.t * (sg.b[0] - sg.a[0]), sg.a[1] + h.t * (sg.b[1] - sg.a[1])], sg.za + h.t * (sg.zb - sg.za),
         sg.s0 + h.t * sg.len)
    }
}

/// np.interp's formula (slope * (x - xp[j]) + fp[j]) on one segment
fn lerp(x: f64, x0: f64, x1: f64, f0: f64, f1: f64) -> f64 {
    let slope = (f1 - f0) / (x1 - x0);
    slope * (x - x0) + f0
}

/// The member's samples every STEP_M (the pieces' middles): (point, height, unit direction), and the piece length.
fn samples(m: &Member) -> (Vec<([f64; 2], f64, [f64; 2])>, f64) {
    let l = m.len();
    if m.xy.len() < 2 || l <= 1e-9 {
        return (Vec::new(), 0.0);
    }
    let n = ((l / STEP_M - 1e-9).ceil() as usize).max(1);
    let piece = l / n as f64;
    let mut out = Vec::with_capacity(n);
    let mut seg = 0usize;
    for i in 0..n {
        let t = (i as f64 + 0.5) * piece;
        while seg + 2 < m.s.len() && m.s[seg + 1] <= t {
            seg += 1;
        }
        let (s0, s1) = (m.s[seg], m.s[seg + 1]);
        let (a, b) = (m.xy[seg], m.xy[seg + 1]);
        let p = [lerp(t, s0, s1, a[0], b[0]), lerp(t, s0, s1, a[1], b[1])];
        let z = lerp(t, s0, s1, m.z[seg], m.z[seg + 1]);
        let len = s1 - s0;
        out.push((p, z, [(b[0] - a[0]) / len, (b[1] - a[1]) / len]));
    }
    (out, piece)
}

fn within(u: [f64; 2], v: [f64; 2], tan: f64) -> bool {
    // the undirected lines u, v within the angle whose tangent is tan
    let cr = (u[0] * v[1] - u[1] * v[0]).abs();
    let dt = (u[0] * v[0] + u[1] * v[1]).abs();
    cr <= tan * dt
}

fn inside(ring: &[[f64; 2]], p: [f64; 2]) -> bool {
    let mut c = false;
    let n = ring.len();
    if n < 3 {
        return false;
    }
    let mut j = n - 1;
    for i in 0..n {
        let (a, b) = (ring[i], ring[j]);
        if (a[1] > p[1]) != (b[1] > p[1]) {
            let x = (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0];
            if p[0] < x {
                c = !c;
            }
        }
        j = i;
    }
    c
}

/// A sample of member A against the nearest segment of member B.
struct Rec {
    gap: f64,
    dist: f64,
    dz: f64,
    along: bool,
    beside: bool,
    p: [f64; 2],
    q: [f64; 2],
}

#[derive(Default)]
pub struct Out {
    pub root: Vec<usize>,
    pub dup: Vec<usize>,                                    // the member each one duplicates (itself: none)
    pub edges: Vec<(usize, usize, u8, bool, f64)>,          // (a, b, kind, united two groups, |dz|)
    pub rejected: Vec<(usize, usize, u8, f64)>,             // (a, b, kind, the least |dz|): over the height limit
    pub conflicts: Vec<(usize, usize, u8, u8, f64)>,        // (a, b, reason, kind, |dz|)
}

struct Acc<'a> {
    ms: &'a [Member],
    edges: BTreeMap<(usize, usize), (u8, f64)>,
    rejected: BTreeMap<(usize, usize, u8), f64>,
    conflicts: BTreeMap<(usize, usize, u8), (u8, f64)>,
}

impl Acc<'_> {
    fn same_level(&self, a: usize, b: usize) -> bool {
        let (la, lb) = (self.ms[a].level, self.ms[b].level);
        la.is_finite() && lb.is_finite() && la == lb
    }

    /// Why a and b may never be one structure (None: they may; C_OUTSIDE: they may, but one is outside the chain).
    fn never(&self, a: usize, b: usize) -> Option<u8> {
        let r = never_rule(&self.ms[a], &self.ms[b]);
        if r.is_none() && (self.ms[a].outside || self.ms[b].outside) {
            Some(C_OUTSIDE)
        } else {
            r
        }
    }

    fn conflict(&mut self, a: usize, b: usize, reason: u8, kind: u8, dz: f64) {
        let e = self.conflicts.entry((a, b, reason)).or_insert((kind, dz));
        if kind < e.0 || (kind == e.0 && dz < e.1) {
            *e = (kind, dz);
        }
    }

    /// The geometry of `kind` held within its height limit (|dz| dz).
    fn hold(&mut self, i: usize, j: usize, kind: u8, dz: f64) {
        let (a, b) = (i.min(j), i.max(j));
        if let Some(r) = self.never(a, b) {
            let dz = if r == C_OUTSIDE { f64::NAN } else { dz };
            self.conflict(a, b, r, kind, dz);
            return;
        }
        let e = self.edges.entry((a, b)).or_insert((kind, dz));
        if kind < e.0 {
            *e = (kind, dz);
        }
    }

    /// The geometry of `kind` held, over its height limit (the least |dz| dz).
    fn over(&mut self, i: usize, j: usize, kind: u8, dz: f64) {
        let (a, b) = (i.min(j), i.max(j));
        if self.ms[a].outside || self.ms[b].outside {
            // (no height of its own: an outside conflict only where nothing else rules the pair out)
            if self.never(a, b) == Some(C_OUTSIDE) {
                self.conflict(a, b, C_OUTSIDE, kind, f64::NAN);
            }
            return;
        }
        let e = self.rejected.entry((a, b, kind)).or_insert(dz);
        if dz < *e {
            *e = dz;
        }
        let same_kind = self.ms[a].kind == self.ms[b].kind && self.ms[a].kind != TUNNEL;
        if same_kind && self.same_level(a, b) && dz > CONFLICT_DZ {
            self.conflict(a, b, C_DZ, kind, dz);
        }
    }
}

/// The never-union rules of two members, outside the chain or not (None: they may be one structure).
fn never_rule(ma: &Member, mb: &Member) -> Option<u8> {
    let (ka, kb) = (ma.kind, mb.kind);
    let (la, lb) = (ma.level, mb.level);
    if ka == TUNNEL || kb == TUNNEL {
        Some(C_TUNNEL)
    } else if ka != kb && (ka == RAIL || kb == RAIL) {
        Some(C_RAIL)
    } else if ka != kb {
        Some(C_WALK)
    } else if la.is_finite() && lb.is_finite() && la != lb {
        Some(C_LEVEL)
    } else {
        None
    }
}

/// Every vertex of member m within DUP_M of line `line`.
fn covers(idx: &Index, line: usize, m: &Member) -> bool {
    m.xy.iter().all(|&p| idx.near(p, DUP_M).contains_key(&line))
}

/// The member each one duplicates (the first of its way, kind and line; itself when none), in the members' order.
fn duplicates(ms: &[Member], idx: &Index) -> Vec<usize> {
    let n = ms.len();
    let mut dup: Vec<usize> = (0..n).collect();
    for i in 0..n {
        if ms[i].xy.len() < 2 {
            continue;
        }
        for j in 0..i {
            // (one way, one kind, one grouping level: copies on two levels stay two members, Codex review)
            if dup[j] != j || ms[j].fid != ms[i].fid || ms[j].kind != ms[i].kind || ms[j].xy.len() < 2
                || never_rule(&ms[i], &ms[j]).is_some() || (ms[j].len() - ms[i].len()).abs() > 2.0 * DUP_M {
                continue;
            }
            if covers(idx, j, &ms[i]) && covers(idx, i, &ms[j]) {
                dup[i] = j;
                break;
            }
        }
    }
    dup
}

/// The member each one duplicates (duplicates', the members' own index), for the abutments' end groups.
pub(crate) fn member_duplicates(ms: &[Member]) -> Vec<usize> {
    duplicates(ms, &Index::new(ms))
}

/// The pairs (i, j), i < j, that could be one structure whatever their heights and outlines: the edges of
/// structure_groups_core's own tests in CLOSURE mode (every height 0, so every height limit holds; one outline round
/// everything, so every BESIDE_OUTLINE candidate holds; every end free for END_ON whatever JOIN took it; no member
/// a duplicate). Every edge structure_groups_core can find with any heights and any outlines is among them (each of its
/// tests only narrows with them), and nothing else: proximity alone (one sample near) is no pair, a sustained run
/// beside or an end on the other is (Codex review: a 6 m sample reach chained decks the grouping never joins).
pub fn structure_touch_core(ms: &[Member]) -> Vec<(usize, usize)> {
    let flat: Vec<Member> = ms.iter().map(|m| Member { xy: m.xy.clone(), z: vec![0.0; m.z.len()], s: m.s.clone(),
        half: m.half, kind: m.kind, level: m.level, fid: m.fid, outside: false }).collect();
    let mut bb = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    for m in &flat {
        for p in &m.xy {
            bb = [bb[0].min(p[0]), bb[1].min(p[1]), bb[2].max(p[0]), bb[3].max(p[1])];
        }
    }
    if !bb[0].is_finite() {
        return Vec::new();
    }
    let pad = 100.0;
    let ring = vec![[bb[0] - pad, bb[1] - pad], [bb[2] + pad, bb[1] - pad], [bb[2] + pad, bb[3] + pad],
                    [bb[0] - pad, bb[3] + pad]];
    let out = groups_core(&flat, &[ring], true);
    out.edges.iter().map(|&(a, b, _k, _u, _dz)| (a, b)).collect()
}

fn sum(recs: &[&Rec], piece: f64) -> f64 {
    recs.len() as f64 * piece
}

fn max_dz(recs: &[&Rec]) -> f64 {
    recs.iter().fold(f64::NEG_INFINITY, |m, r| m.max(r.dz))
}

fn min_dz(recs: &[&Rec]) -> f64 {
    recs.iter().fold(f64::INFINITY, |m, r| m.min(r.dz))
}

/// The footprint tests of member a's samples against member b (OVERLAP_ALONG, OVERLAP_CROSS, BESIDE, BESIDE_OUTLINE).
fn footprint(acc: &mut Acc, a: usize, b: usize, recs: &[Rec], piece: f64, outlines: &[Vec<[f64; 2]>]) {
    let ms = acc.ms;
    let hb = ms[b].half;
    let along: Vec<&Rec> = recs.iter().filter(|r| r.gap < 0.0 && r.along).collect();
    if sum(&along, piece) >= ALONG_MIN_M {
        let good: Vec<&Rec> = along.iter().copied().filter(|r| r.dz <= ALONG_DZ).collect();
        if sum(&good, piece) >= ALONG_MIN_M {
            acc.hold(a, b, OVERLAP_ALONG, max_dz(&good));
        } else {
            acc.over(a, b, OVERLAP_ALONG, min_dz(&along));
        }
    }
    let cross: Vec<&Rec> = recs.iter().filter(|r| r.dist <= hb && !r.along).collect();
    if !cross.is_empty() {
        let good: Vec<&Rec> = cross.iter().copied().filter(|r| r.dz <= CROSS_DZ).collect();
        if !good.is_empty() {
            acc.hold(a, b, OVERLAP_CROSS, min_dz(&good));
        } else {
            acc.over(a, b, OVERLAP_CROSS, min_dz(&cross));
        }
    }
    let need = BESIDE_MIN_M.min(BESIDE_SHORT * ms[a].len().min(ms[b].len()));
    let par: Vec<&Rec> = recs.iter().filter(|r| r.gap >= -TWIN_TOUCH_M && r.beside).collect();
    let near: Vec<&Rec> = par.iter().copied().filter(|r| r.gap <= BESIDE_GAP_M).collect();
    if !near.is_empty() && sum(&near, piece) >= need {
        let good: Vec<&Rec> = near.iter().copied().filter(|r| r.dz <= BESIDE_DZ).collect();
        if sum(&good, piece) >= need {
            acc.hold(a, b, BESIDE, max_dz(&good));
        } else {
            acc.over(a, b, BESIDE, min_dz(&near));
        }
    }
    let wide: Vec<&Rec> = par.iter().copied().filter(|r| r.gap > BESIDE_GAP_M && r.gap <= OUTLINE_GAP_M).collect();
    if !outlines.is_empty() && !wide.is_empty() && sum(&wide, piece) >= need {
        // (one outline holding both: a's samples and b's nearest points in it over the length; every outline tried)
        let (mut held, mut passed): (Option<f64>, Option<f64>) = (None, None);
        for ring in outlines {
            let both: Vec<&Rec> = wide.iter().copied().filter(|r| inside(ring, r.p) && inside(ring, r.q)).collect();
            if both.is_empty() || sum(&both, piece) < need {
                continue;
            }
            if held.is_none() {
                held = Some(min_dz(&both));
            }
            let good: Vec<&Rec> = both.iter().copied().filter(|r| r.dz <= BESIDE_DZ).collect();
            if sum(&good, piece) >= need {
                let d = max_dz(&good);
                if passed.is_none_or(|p| d < p) {
                    passed = Some(d);
                }
            }
        }
        if let Some(d) = passed {
            acc.hold(a, b, BESIDE_OUTLINE, d);
        } else if let Some(d) = held {
            acc.over(a, b, BESIDE_OUTLINE, d);
        }
    }
}

/// The groups of members ms (in their keys' order) with OSM bridge outline rings `outlines` (BESIDE_OUTLINE's
/// evidence).
pub fn structure_groups_core(ms: &[Member], outlines: &[Vec<[f64; 2]>]) -> Out {
    groups_core(ms, outlines, false)
}

/// structure_groups_core; closure: structure_touch_core's mode (no duplicates, every end free for END_ON).
pub(crate) fn groups_core(ms: &[Member], outlines: &[Vec<[f64; 2]>], closure: bool) -> Out {
    let n = ms.len();
    let mut acc = Acc { ms, edges: BTreeMap::new(), rejected: BTreeMap::new(), conflicts: BTreeMap::new() };
    let idx = Index::new(ms);
    let dup = if closure { (0..n).collect() } else { duplicates(ms, &idx) };
    let max_half = ms.iter().fold(0.0f64, |m, x| m.max(x.half));
    let pair_ok = |i: usize, j: usize| {
        i != j && dup[i] == i && dup[j] == j && ms[i].fid != ms[j].fid && !(ms[i].outside && ms[j].outside)
    };
    // JOIN from the ends
    let mut ends: Vec<(usize, [f64; 2], f64)> = Vec::new();
    for (i, m) in ms.iter().enumerate() {
        if m.xy.len() >= 2 && dup[i] == i {
            ends.push((i, m.xy[0], m.z[0]));
            ends.push((i, m.xy[m.xy.len() - 1], m.z[m.z.len() - 1]));
        }
    }
    let mut joined = vec![false; ends.len()];
    for k in 0..ends.len() {
        let (i, p, z) = ends[k];
        for (k2, &(j, q, z2)) in ends.iter().enumerate() {
            if k2 == k || j == i {
                continue;
            }
            let d = ((p[0] - q[0]) * (p[0] - q[0]) + (p[1] - q[1]) * (p[1] - q[1])).sqrt();
            if d > JOIN_END_M {
                continue;
            }
            if !pair_ok(i, j) {
                continue;
            }
            let dz = (z - z2).abs();
            if !closure && dz <= JOIN_DZ && acc.never(i.min(j), i.max(j)).is_none() {
                joined[k] = true;       // (taken: an end over or beside another deck's end stays free for END_ON)
            }
            if dz <= JOIN_DZ {
                acc.hold(i, j, JOIN, dz);
            } else {
                acc.over(i, j, JOIN, dz);
            }
        }
    }
    // END_ON: a free end on another member's carriageway, away from its ends (those are JOIN's)
    for k in 0..ends.len() {
        if joined[k] {
            continue;
        }
        let (i, p, z) = ends[k];
        for (j, h) in idx.near(p, max_half) {
            if !pair_ok(i, j) || h.dist > ms[j].half {
                continue;
            }
            let (_q, zb, sb) = idx.at(&h);
            let lj = ms[j].len();
            if sb <= JOIN_END_M || sb >= lj - JOIN_END_M {
                continue;
            }
            let dz = (z - zb).abs();
            if dz <= END_ON_DZ {
                acc.hold(i, j, END_ON, dz);
            } else {
                acc.over(i, j, END_ON, dz);
            }
        }
    }
    // the footprint tests from every member's samples (a member outside the chain: only as the other one)
    for a in 0..n {
        if ms[a].outside || dup[a] != a {
            continue;
        }
        let (pts, piece) = samples(&ms[a]);
        let ha = ms[a].half;
        let reach = ha + max_half + OUTLINE_GAP_M + 1.0;
        let mut by: BTreeMap<usize, Vec<Rec>> = BTreeMap::new();
        for &(p, za, ua) in &pts {
            for (b, h) in idx.near(p, reach) {
                if !pair_ok(a, b) {
                    continue;
                }
                let hb = ms[b].half;
                if h.dist > ha + hb + OUTLINE_GAP_M + 1.0 {
                    continue;
                }
                let (q, zb, sb) = idx.at(&h);
                if h.past || sb < piece / 2.0 || sb > ms[b].len() - piece / 2.0 {
                    // (beyond b's end, or its piece not wholly beside b: no shared span credited; ends are JOIN's and
                    // END_ON's. Two decks end to end overlapping 1.6 m were credited two pieces, 2 m: Codex round 2)
                    continue;
                }
                let ub = idx.segs[h.seg].u;
                by.entry(b).or_default().push(Rec {
                    gap: h.dist - ha - hb, dist: h.dist, dz: (za - zb).abs(), along: within(ua, ub, TAN_ALONG),
                    beside: within(ua, ub, TAN_BESIDE), p, q,
                });
            }
        }
        for (b, recs) in &by {
            footprint(&mut acc, a, *b, recs, piece, outlines);
        }
    }
    // union-find in the pairs' order, the smaller index the root
    let mut parent: Vec<usize> = (0..n).collect();
    fn find(parent: &mut [usize], mut x: usize) -> usize {
        while parent[x] != x {
            parent[x] = parent[parent[x]];
            x = parent[x];
        }
        x
    }
    let mut out = Out::default();
    for (&(a, b), &(kind, dz)) in &acc.edges {
        let (ra, rb) = (find(&mut parent, a), find(&mut parent, b));
        if ra != rb {
            parent[ra.max(rb)] = ra.min(rb);
        }
        out.edges.push((a, b, kind, ra != rb, dz));
    }
    out.root = (0..n).map(|i| find(&mut parent, dup[i])).collect();
    out.dup = dup;
    out.rejected = acc.rejected.iter().map(|(&(a, b, k), &dz)| (a, b, k, dz)).collect();
    out.conflicts = acc.conflicts.iter().map(|(&(a, b, r), &(k, dz))| (a, b, r, k, dz)).collect();
    out
}

type Edges = Vec<(i64, i64, i64, bool, f64)>;
type Rejected = Vec<(i64, i64, i64, f64)>;
type Conflicts = Vec<(i64, i64, i64, i64, f64)>;

/// roads / structures.py: the structure groups of one chain's members. pts (P, 3) x y z of every member's line,
/// member k rows off[k]..off[k + 1]; half (N,) carriageway half widths; kind (N,) ROAD / WALK / RAIL / TUNNEL;
/// level (N,) the OSM level (NaN: unknown); fid (N,) the OSM way (two parts of one way are never paired); outside (N,)
/// 1 for a part outside the chain (no height of its own); outline rings opts (Q, 2), ring k rows ooff[k]..ooff[k + 1].
/// Members in their keys' order, the chain's first. Returns (root per member, the member each duplicates (itself:
/// none), edges [(a, b, kind, united, |dz|)], rejected [(a, b, kind, |dz|)], conflicts [(a, b, reason, kind, |dz|)]).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn structure_groups<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                             half: PyReadonlyArray1<'py, f64>, kind: PyReadonlyArray1<'py, i64>,
                             level: PyReadonlyArray1<'py, f64>, fid: PyReadonlyArray1<'py, i64>,
                             outside: PyReadonlyArray1<'py, i64>, opts: PyReadonlyArray2<'py, f64>,
                             ooff: PyReadonlyArray1<'py, i64>)
                             -> PyResult<(Vec<i64>, Vec<i64>, Edges, Rejected, Conflicts)> {
    let (opts, ooff) = (opts.as_array(), ooff.as_array().to_vec());
    if opts.ncols() != 2 || ooff.iter().any(|&v| v < 0 || v as usize > opts.nrows())
        || ooff.windows(2).any(|w| w[1] < w[0]) {
        return Err(PyValueError::new_err("structure_groups: shapes or offsets do not agree"));
    }
    let ms = members(&pts, &off, &half, &kind, &level, &fid, Some(&outside), 3)?;
    let outlines: Vec<Vec<[f64; 2]>> = (0..ooff.len().saturating_sub(1)).map(|k| {
        (ooff[k] as usize..ooff[k + 1] as usize).map(|r| [mm(opts[[r, 0]]), mm(opts[[r, 1]])]).collect()
    }).collect();
    let out = py.detach(|| structure_groups_core(&ms, &outlines));
    Ok((out.root.iter().map(|&r| r as i64).collect(), out.dup.iter().map(|&r| r as i64).collect(),
        out.edges.iter().map(|&(a, b, k, u, dz)| (a as i64, b as i64, k as i64, u, dz)).collect(),
        out.rejected.iter().map(|&(a, b, k, dz)| (a as i64, b as i64, k as i64, dz)).collect(),
        out.conflicts.iter().map(|&(a, b, r, k, dz)| (a as i64, b as i64, r as i64, k as i64, dz)).collect()))
}

/// The members of the kernels' arrays (pts (P, 2 or 3): x y [z]; z NaN when 2 columns), checked.
#[allow(clippy::too_many_arguments)]
pub(crate) fn members(pts: &PyReadonlyArray2<f64>, off: &PyReadonlyArray1<i64>, half: &PyReadonlyArray1<f64>,
           kind: &PyReadonlyArray1<i64>, level: &PyReadonlyArray1<f64>, fid: &PyReadonlyArray1<i64>,
           outside: Option<&PyReadonlyArray1<i64>>, cols: usize) -> PyResult<Vec<Member>> {
    let (pts, half, kind, level, fid) = (pts.as_array(), half.as_array(), kind.as_array(), level.as_array(),
        fid.as_array());
    let outside = outside.map(|o| o.as_array().to_vec());
    let off = off.as_array().to_vec();
    let n = off.len().saturating_sub(1);
    if pts.ncols() != cols || half.len() != n || kind.len() != n || level.len() != n || fid.len() != n
        || outside.as_ref().is_some_and(|o| o.len() != n)
        || off.iter().any(|&v| v < 0 || v as usize > pts.nrows()) || off.windows(2).any(|w| w[1] < w[0]) {
        return Err(PyValueError::new_err("structures: shapes or offsets do not agree"));
    }
    Ok((0..n).map(|k| {
        let rows: Vec<[f64; 3]> = (off[k] as usize..off[k + 1] as usize)
            .map(|r| [pts[[r, 0]], pts[[r, 1]], if cols == 3 { pts[[r, 2]] } else { f64::NAN }]).collect();
        Member::new(&rows, half[k], kind[k], level[k], fid[k], outside.as_ref().is_some_and(|o| o[k] != 0))
    }).collect())
}

/// roads.BridgeProfiles._resolve on the switch path (structures.touch): the pairs (i, j), i < j, of deck parts that
/// could be one structure whatever their heights (structure_touch_core). pts (P, 2) x y of every part's line, part k
/// rows off[k]..off[k + 1]; half, kind, level, fid as structure_groups'.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn structure_touch<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                            half: PyReadonlyArray1<'py, f64>, kind: PyReadonlyArray1<'py, i64>,
                            level: PyReadonlyArray1<'py, f64>, fid: PyReadonlyArray1<'py, i64>)
                            -> PyResult<Vec<(i64, i64)>> {
    let ms = members(&pts, &off, &half, &kind, &level, &fid, None, 2)?;
    let out = py.detach(|| structure_touch_core(&ms));
    Ok(out.iter().map(|&(a, b)| (a as i64, b as i64)).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(pts: &[(f64, f64)], z: f64, half: f64, level: f64, fid: i64) -> Member {
        let p: Vec<[f64; 3]> = pts.iter().map(|&(x, y)| [x, y, z]).collect();
        Member::new(&p, half, ROAD, level, fid, false)
    }

    #[test]
    fn twins_on_one_short_deck_are_one_structure() {
        // E6 Vist: two carriageways, 17 m long, 1 m apart edge to edge (BESIDE by 80 % of the shorter)
        let ms = [line(&[(0.0, 0.0), (17.0, 0.0)], 10.0, 4.0, 1.0, 1),
                  line(&[(0.0, 9.0), (17.0, 9.0)], 10.3, 4.0, 1.0, 2)];
        let out = structure_groups_core(&ms, &[]);
        assert_eq!(out.root, vec![0, 0]);
        assert_eq!(out.edges[0].2, BESIDE);
    }

    #[test]
    fn levels_never_join() {
        // Queensboro: two decks over each other ~1 m apart in the game, different OSM levels: a data conflict
        let ms = [line(&[(0.0, 0.0), (100.0, 0.0)], 10.0, 5.0, 1.0, 1),
                  line(&[(0.0, 1.0), (100.0, 1.0)], 10.8, 5.0, 2.0, 2)];
        let out = structure_groups_core(&ms, &[]);
        assert_eq!(out.root, vec![0, 1]);
        assert!(out.edges.is_empty());
        assert!(out.conflicts.iter().any(|c| c.2 == C_LEVEL));
    }

    #[test]
    fn a_t_ending_on_a_deck_and_an_overpass() {
        // Springfield: a ramp's free end on the curved deck (END_ON); a deck crossing 7 m over it: never one
        let ms = [line(&[(0.0, 0.0), (100.0, 0.0)], 10.0, 6.0, 1.0, 1),
                  line(&[(50.0, 3.0), (50.0, 60.0)], 10.2, 4.0, 1.0, 2),
                  line(&[(80.0, -50.0), (80.0, 50.0)], 17.0, 4.0, 2.0, 3)];
        let out = structure_groups_core(&ms, &[]);
        assert_eq!(out.root, vec![0, 0, 2]);
        assert!(out.edges.iter().any(|e| (e.0, e.1, e.2) == (0, 1, END_ON)));
        assert!(out.rejected.iter().any(|r| (r.0, r.1, r.2) == (0, 2, OVERLAP_CROSS)));
    }

    #[test]
    fn walkways_and_one_level_far_apart_are_conflicts() {
        let mut walk = line(&[(0.0, 6.0), (60.0, 6.0)], 10.0, 1.5, 1.0, 2);
        walk.kind = WALK;
        let ms = [line(&[(0.0, 0.0), (60.0, 0.0)], 10.0, 4.0, 1.0, 1), walk,
                  line(&[(30.0, -40.0), (30.0, 40.0)], 15.0, 4.0, 1.0, 3)];
        let out = structure_groups_core(&ms, &[]);
        assert_eq!(out.root, vec![0, 1, 2]);
        assert!(out.conflicts.iter().any(|c| (c.0, c.1, c.2) == (0, 1, C_WALK)));
        assert!(out.conflicts.iter().any(|c| (c.0, c.1, c.2) == (0, 2, C_DZ)));
    }

    #[test]
    fn joins_and_outline_twins() {
        // two parts end to end (JOIN), and a twin 4 m away held by one bridge outline (BESIDE_OUTLINE)
        let ms = [line(&[(0.0, 0.0), (50.0, 0.0)], 10.0, 4.0, 1.0, 1),
                  line(&[(50.0, 0.0), (100.0, 0.0)], 10.1, 4.0, 1.0, 2),
                  line(&[(0.0, 12.0), (100.0, 12.0)], 10.4, 4.0, 1.0, 3)];
        let none = structure_groups_core(&ms, &[]);
        assert_eq!(none.root, vec![0, 0, 2]);
        let ring = vec![[-5.0, -10.0], [105.0, -10.0], [105.0, 20.0], [-5.0, 20.0]];
        let out = structure_groups_core(&ms, &[ring]);
        assert_eq!(out.root, vec![0, 0, 0]);
        assert!(out.edges.iter().any(|e| e.2 == BESIDE_OUTLINE));
        assert!(out.edges.iter().any(|e| (e.0, e.1, e.2) == (0, 1, JOIN)));
    }

    #[test]
    fn staggered_short_decks_are_not_beside() {
        // two 5 m decks end to end along x, 9 m apart across: no length side by side (only past each other's ends)
        let ms = [line(&[(0.0, 0.0), (5.0, 0.0)], 10.0, 4.0, 1.0, 1),
                  line(&[(5.1, 9.0), (10.1, 9.0)], 10.0, 4.0, 1.0, 2)];
        let out = structure_groups_core(&ms, &[]);
        assert_eq!(out.root, vec![0, 1]);
        assert!(out.edges.is_empty());
    }

    #[test]
    fn a_short_overlap_end_to_end_is_not_along() {
        // decks on one axis overlapping 1.6 m end to end, 0.8 m apart in height: under OVERLAP_ALONG's 2 m
        let ms = [line(&[(0.0, 0.0), (100.0, 0.0)], 10.0, 4.0, 1.0, 1),
                  line(&[(98.4, 0.0), (198.4, 0.0)], 10.8, 4.0, 1.0, 2)];
        let out = structure_groups_core(&ms, &[]);
        assert_eq!(out.root, vec![0, 1]);
    }

    #[test]
    fn an_end_over_another_level_keeps_its_end_on() {
        // a ramp's free end on a through deck; another level's deck ends right over it (no JOIN: never one)
        let ms = [line(&[(0.0, 0.0), (100.0, 0.0)], 10.0, 6.0, 1.0, 1),
                  line(&[(50.0, 40.0), (50.0, 2.0)], 10.1, 4.0, 1.0, 2),
                  line(&[(90.0, 2.0), (50.2, 2.0)], 17.0, 4.0, 2.0, 3)];
        let out = structure_groups_core(&ms, &[]);
        assert_eq!(out.root[1], 0);
        assert!(out.edges.iter().any(|e| (e.0, e.1, e.2) == (0, 1, END_ON)));
    }

    #[test]
    fn outside_parts_are_recorded_not_joined() {
        let mut far = line(&[(0.0, 10.0), (100.0, 10.0)], f64::NAN, 4.0, 1.0, 2);
        far.outside = true;
        let mut other = line(&[(0.0, -10.0), (100.0, -10.0)], f64::NAN, 4.0, 2.0, 3);
        other.outside = true;           // (another OSM level: never one structure, so no outside conflict)
        let ms = [line(&[(0.0, 0.0), (100.0, 0.0)], 10.0, 4.0, 1.0, 1), far, other];
        let out = structure_groups_core(&ms, &[]);
        assert_eq!(out.root, vec![0, 1, 2]);
        assert!(out.conflicts.iter().any(|c| (c.0, c.1, c.2) == (0, 1, C_OUTSIDE)));
        assert!(!out.conflicts.iter().any(|c| c.1 == 2));
    }

    #[test]
    fn one_way_under_two_keys_is_one_member() {
        // San Diego's c54ad200: the same way's line twice (1 cm apart), a twin beside it: the copy joins nothing itself
        let ms = [line(&[(0.0, 0.0), (300.0, 0.0)], 10.0, 4.0, 1.0, 1),
                  line(&[(0.0, 0.01), (150.0, 0.01), (300.0, 0.0)], 10.001, 4.0, 1.0, 1),
                  line(&[(0.0, 9.0), (300.0, 9.0)], 10.2, 4.0, 1.0, 2)];
        let out = structure_groups_core(&ms, &[]);
        assert_eq!(out.dup, vec![0, 0, 2]);
        assert_eq!(out.root, vec![0, 0, 0]);
        assert!(out.edges.iter().all(|e| e.0 != 1 && e.1 != 1));
        // another part of the same way (not its line) is no duplicate
        let ms2 = [line(&[(0.0, 0.0), (300.0, 0.0)], 10.0, 4.0, 1.0, 1),
                   line(&[(300.0, 0.0), (600.0, 0.0)], 10.0, 4.0, 1.0, 1)];
        assert_eq!(structure_groups_core(&ms2, &[]).dup, vec![0, 1]);
        // nor a copy on another grouping level (Codex review: kept, its level rule applies)
        let ms3 = [line(&[(0.0, 0.0), (300.0, 0.0)], 10.0, 4.0, 1.0, 1),
                   line(&[(0.0, 0.01), (300.0, 0.01)], 10.0, 4.0, 2.0, 1)];
        assert_eq!(structure_groups_core(&ms3, &[]).dup, vec![0, 1]);
    }

    #[test]
    fn the_closure_holds_every_edge() {
        // structure_touch: every pair structure_groups_core makes an edge of whatever the heights and outlines, never
        // another OSM level, a walkway with a road or two parts of one way, never a deck merely passing near
        let mut walk = line(&[(0.0, 14.0), (100.0, 14.0)], 10.0, 1.5, 1.0, 5);
        walk.kind = WALK;
        let ms = [line(&[(0.0, 0.0), (100.0, 0.0)], 10.0, 4.0, 1.0, 1),
                  line(&[(100.0, 0.3), (200.0, 0.3)], 10.1, 4.0, 1.0, 2),             // JOIN
                  line(&[(0.0, 13.5), (100.0, 13.5)], 10.4, 4.0, 1.0, 3),             // 5.5 m edge to edge
                  line(&[(50.0, 60.0), (50.0, 3.0)], 10.2, 3.0, 1.0, 4),              // END_ON
                  walk,
                  line(&[(0.0, 2.0), (100.0, 2.0)], 17.0, 4.0, 2.0, 6),               // another level over it
                  line(&[(200.0, 0.3), (300.0, 0.3)], 10.1, 4.0, 1.0, 2),             // the same way as 1
                  line(&[(110.0, 8.0), (110.0, 60.0)], 10.0, 4.0, 1.0, 7)];           // its end 3.7 m off 1's edge
        let pairs = structure_touch_core(&ms);
        assert!(pairs.iter().all(|&(a, b)| b != 7), "proximity alone is no pair: {pairs:?}");
        for p in [(0, 1), (0, 2), (0, 3)] {
            assert!(pairs.contains(&p), "{p:?} in {pairs:?}");
        }
        assert!(pairs.iter().all(|&(a, b)| a != 4 && b != 4 && a != 5 && b != 5 && (a, b) != (1, 6)), "{pairs:?}");
        let ring = vec![[-5.0, -10.0], [105.0, -10.0], [105.0, 25.0], [-5.0, 25.0]];
        let out = structure_groups_core(&ms, &[ring]);
        for e in &out.edges {
            assert!(pairs.contains(&(e.0, e.1)), "edge {:?} outside the closure", (e.0, e.1, e.2));
        }
    }
}
