//! structure_heights (earthfast 488, road revamp RS step S2b part 1): ONE joint height solve per structure, the
//! single mechanism that will retire, on the switch path, the pairwise ones (roads._merge_overlaps / DeckMerge with its
//! DUAL_DECK_M, roads._tie_beside) and the per-chain clearance quirks. Shadow only at S2b part 1: roads writes its
//! heights into the chain's JSON beside today's; nothing is published from it.
//!
//! Input: one bridge chain's members (lines, the chain profile z_in solved WITHOUT the pairwise mechanisms, half widths,
//! grouping levels, their structure group's root and duplicates from structure_groups, class rank and link flag, the
//! clearance a deck over each one needs), the group edges (a, b, kind), and the chain's crossing floors (a road or a
//! railway under a member: its z there must reach a floor).
//!
//! Couplings (per edge of one group, never across groups: conflicts and rejected pairs stay apart):
//! - JOIN: the two ends; END_ON: the free end and its point on the other deck; OVERLAP_CROSS: where the centre lines
//!   cross; and for every edge but a crossing (side by side: BESIDE / OUTLINE within their gap, the others within
//!   BESIDE_GAP_M, so a ramp beside its deck up to the gore and two copies of one road are held too): each vertex of the follower (the member that is no through one,
//!   else the edge's second) against its nearest point of the leader where that lies beside (not past its ends, within
//!   the edge's gap, within 30 degrees): every DECK_STEP (4 m) or closer. The metrics probe every STEP_M instead.
//! - Each coupled pair is an equality z_a(s_a) = z_b(s_b) (a penalty of MU: residuals far below a millimetre).
//! - Windows: each edge's coupling interval on each of its members plus an ease of max(EASE_MIN_M, |dz| / EASE_GRADE)
//!   (|dz| the edge's largest coupled difference in z_in). Outside the windows every member is pinned to z_in.
//! - Pins: an abutment end (no other member's end at it and no END_ON coupling from it) and a joint shared with a
//!   member it is not JOIN-coupled to keep z_in exactly (the plans' abutments: plan neutral). A coupled pair nearer a
//!   pinned end than its own |dz| / EASE_GRADE is dropped (its ease cannot fit; counted), and an end left with no
//!   live JOIN / END_ON pair is pinned too, until both settle: twins whose abutments differ keep that there.
//!
//! The solve, per structure: the deviation d = z - z_in on each member's vertices (the heights are per vertex: what is
//! solved is what is drawn) minimises sum w h d^2 + LAMBDA sum h (d'')^2 + MU sum (couplings)^2. w is the member's weight inside its
//! coupling intervals (THROUGH_W for the group's through members: not a link, of the group's best class rank; 1 for
//! the others, so ramps bend to the mainline) and EPS_W in the eases (the smoothest ease: a cubic onto the pinned
//! profile). The curvature penalty is on the deviation, so a window with nothing pulling it keeps z_in exactly.
//!
//! Clearances (an active set, at most ROUNDS rounds; a clearance with no window of its own is held at min(its bound,
//! z_in's): never worse than the chain solve): a crossing floor (z >= floor; not within AT_GRADE_REACH_M of the
//! member's ends) and a deck over a deck of another level within the chain (upper - lower >= the lower's clearance, not
//! near the end the two share). A deck-over-deck clearance z_in already misses by more than MARGIN opens a clearance
//! window on the upper member (the constraint +- max(EASE_MIN_M,
//! deficit / EASE_GRADE); not within AT_GRADE_REACH_M of the member's ends: a junction at grade). Each round adds the
//! constraints violated by more than 1 mm as equalities at their bound; after ROUNDS, any still short by more than
//! MARGIN fails. The grade check: no node segment steeper than max(MAX_GRADE, the window's own steepest in z_in) +
//! GRADE_ADD. A failure drops the member's clearance windows (recorded unmet) and solves again; a failure without them
//! falls back to today's heights for the structure (recorded).
//!
//! Solver: a sparse symmetric positive definite system, nodes in a reverse Cuthill-McKee order (ties by index), an
//! envelope LDL^T factorisation: a fixed order of every sum, basic IEEE operations and sqrt only (no FMA, no libm), no
//! HashMap. Output rounded to 1 mm. The same on every platform.
use std::collections::BTreeMap;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use super::groups::{self, BESIDE, BESIDE_OUTLINE, END_ON, JOIN, OVERLAP_ALONG, OVERLAP_CROSS};

pub const STEP_M: f64 = 2.0;            // m between coupled samples and between a window's nodes
pub const EASE_MIN_M: f64 = 20.0;
pub const EASE_GRADE: f64 = 0.025;
pub const THROUGH_W: f64 = 10.0;
pub const LAMBDA: f64 = 4096.0;         // m^4 per unit weight: a deviation pulled by w = 1 bends over (LAMBDA / w)^(1/4) = 8 m
pub const MU: f64 = 1e8;
pub const EPS_W: f64 = 1e-6;
pub const ROUNDS: usize = 8;
pub const MARGIN: f64 = 0.02;           // m: the re-check of couplings and clearances
pub const ACTIVE_TOL: f64 = 0.001;      // m: a constraint short by more joins the active set
pub const RELEASE_TOL: f64 = 1e-9;      // m: a held one its penalty holds down by more is let go
pub const JOIN_END_M: f64 = 0.5;
pub const AT_GRADE_REACH_M: f64 = 12.0; // roads.AT_GRADE_REACH_M
pub const MAX_GRADE: f64 = 0.06;        // roads.DECK_MAX_GRADE
pub const GRADE_ADD: f64 = 0.0375;      // the smoothest ease's steepest added grade (1.5 x EASE_GRADE)
pub const STACK_GRADE: f64 = 0.05;      // roads.DECK_GRADE: a stack requirement falls away from a shared end at this
pub const BESIDE_GAP_M: f64 = 2.5;
pub const OUTLINE_GAP_M: f64 = 6.0;
const TAN_ALONG: f64 = 0.5773502691896257;     // tan(30 degrees)
const GRID_M: f64 = 16.0;

// status per structure
pub const S_SOLVED: i64 = 0;        // solved (something coupled or a clearance window)
pub const S_PINNED: i64 = 1;        // nothing to solve: z_in
pub const S_INFEASIBLE: i64 = 2;    // couplings not met (contradicting pins): today's heights
pub const S_CLEARANCE: i64 = 3;     // a clearance not met after ROUNDS: today's heights
pub const S_GRADE: i64 = 4;         // the grade check failed without clearance windows: today's heights
pub const S_SOLVER: i64 = 5;        // the factorisation failed: today's heights
pub const S_BUDGET: i64 = 6;        // over a memory or work budget (Budget): today's heights, nothing allocated for it

/// The call's memory and work limits, checked BEFORE what they bound is allocated or run (checked integer counts of
/// the input alone: the same answer on every machine, never a timing). Over one, the structure (a factorisation's) or
/// the whole chain (the samples, couplings, clearances, index) keeps today's heights with S_BUDGET. The writer solves
/// one chain at a time in the background, so what a budget buys is a bounded stall of the shadow queue, never a game
/// or build hitch; real chains sit orders of magnitude below each:
/// - factor_bytes 32 MiB: one envelope factor (4 M f64). A structure's free nodes are its coupling and clearance
///   windows' vertices (roads.DECK_STEP, 4 m): a 10 km four-carriageway viaduct coupled end to end is 10 000 nodes,
///   and RCM gives twins a profile of a few nodes (40 000 entries); 32 MiB is 10 000 nodes at a profile of 400.
/// - flops 2e9: every factorisation of the call (each round, attempt and structure; ~3 per inner product term plus
///   the solves), about 1-2 s of the writer's one thread (scalar, no FMA). The 10 km viaduct above: 8 rounds x 1e6.
/// - samples 2e6: the coupling candidates (each edge's follower vertices / probes, its ends, a crossing edge's segment
///   pairs) and the clearance samples (every STEP_M of every member, every floor), each a grid lookup (~1 s at most).
/// - pairs 1e5: couplings (the solve's, and the probes' apart) and clearances each; every pair puts up to 16 entries
///   in the system's maps (~40 MB at the cap). A 10 km twin is 2 500 pairs, a large interchange's chain ~20 000.
/// - grid 2e6: the segment index's cell entries (16 m cells; a 4 m segment is in 1-4).
/// - work 1e9: every other superlinear step, charged (Meter) BEFORE it runs, in units of one bucket entry visited /
///   one distance test / one pair or constraint scanned: the index lookups (the cells' bucket entries), the end
///   search of pins (members^2), each settle pass, each structure's set-up and attempt (its members' nodes, pairs and
///   clearances x the rounds), each pass of the cross-structure clearance re-check. ~1-3 s of the writer at most; a
///   real chain spends ~1e6. Over it the whole chain keeps today's heights (S_BUDGET).
#[derive(Clone, Copy)]
pub struct Budget {
    pub factor_bytes: u64,
    pub flops: u64,
    pub samples: u64,
    pub pairs: usize,
    pub grid: u64,
    pub work: u64,
}

pub const BUDGET: Budget = Budget { factor_bytes: 32 << 20, flops: 2_000_000_000, samples: 2_000_000, pairs: 100_000,
                                    grid: 2_000_000, work: 1_000_000_000 };

/// What is left of Budget::work (charged before the work it pays for; once short, it stays short).
struct Meter(u64);

impl Meter {
    fn take(&mut self, n: u64) -> Option<()> {
        match self.0.checked_sub(n) {
            Some(r) => {
                self.0 = r;
                Some(())
            }
            None => {
                self.0 = 0;
                None
            }
        }
    }
}

pub const CLASS_DECK: i64 = -1;     // the class code of a deck-over-deck clearance (the crossings' own codes are >= 0)

pub struct Line {
    pub xy: Vec<[f64; 2]>,
    pub s: Vec<f64>,
    pub z: Vec<f64>,            // z_in at the vertices
    pub half: f64,
    pub level: f64,
    pub root: usize,
    pub dup: bool,
    pub rank: i64,
    pub link: bool,
    pub under: f64,
}

impl Line {
    fn len(&self) -> f64 {
        self.s.last().copied().unwrap_or(0.0)
    }
}

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

/// f at arc t of (s, f) (np.interp's formula on the bracketing segment; clamped at the ends)
fn at(s: &[f64], f: &[f64], t: f64) -> f64 {
    let n = s.len();
    if n == 0 {
        return f64::NAN;
    }
    if t <= s[0] {
        return f[0];
    }
    if t >= s[n - 1] {
        return f[n - 1];
    }
    let (mut lo, mut hi) = (0usize, n - 1);
    while hi - lo > 1 {
        let m = (lo + hi) / 2;
        if s[m] <= t {
            lo = m;
        } else {
            hi = m;
        }
    }
    let d = s[hi] - s[lo];
    if d <= 0.0 {
        return f[lo];
    }
    (f[hi] - f[lo]) / d * (t - s[lo]) + f[lo]
}

fn point_at(l: &Line, t: f64) -> ([f64; 2], [f64; 2]) {
    // (point, unit direction) at arc t
    // (the first k with s[k + 1] > t, at most n - 2: s never decreases, so the s[j] <= t for j in 1..=n-2 are a
    // prefix and a binary search finds the segment the old linear scan did)
    let n = l.xy.len();
    let k = if n >= 3 { l.s[1..n - 1].partition_point(|&v| v <= t) } else { 0 };
    let (a, b) = (l.xy[k], l.xy[k + 1]);
    let d = (l.s[k + 1] - l.s[k]).max(1e-12);
    let u = (t - l.s[k]) / d;
    ([a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1])], [(b[0] - a[0]) / d, (b[1] - a[1]) / d])
}

struct Seg {
    line: usize,
    k: usize,
}

struct Index {
    segs: Vec<Seg>,
    grid: BTreeMap<(i64, i64), Vec<usize>>,
}

#[derive(Clone, Copy)]
struct Hit {
    dist: f64,
    s: f64,
    past: bool,
    u: [f64; 2],
}

fn gcell(v: f64) -> i64 {
    (v / GRID_M).floor() as i64
}

/// Index::new's cell entries (saturating: over any budget when huge).
fn grid_entries(ls: &[Line]) -> u64 {
    let mut n = 0u64;
    for l in ls {
        for k in 0..l.xy.len().saturating_sub(1) {
            let (a, b) = (l.xy[k], l.xy[k + 1]);
            let gx = (gcell(a[0].max(b[0])) as i128 - gcell(a[0].min(b[0])) as i128 + 1).clamp(0, u64::MAX as i128);
            let gy = (gcell(a[1].max(b[1])) as i128 - gcell(a[1].min(b[1])) as i128 + 1).clamp(0, u64::MAX as i128);
            n = n.saturating_add((gx as u64).saturating_mul(gy as u64));
        }
    }
    n
}

impl Index {
    fn new(ls: &[Line]) -> Index {
        let mut segs = Vec::new();
        let mut grid: BTreeMap<(i64, i64), Vec<usize>> = BTreeMap::new();
        for (li, l) in ls.iter().enumerate() {
            for k in 0..l.xy.len().saturating_sub(1) {
                let (a, b) = (l.xy[k], l.xy[k + 1]);
                let id = segs.len();
                segs.push(Seg { line: li, k });
                for gx in gcell(a[0].min(b[0]))..=gcell(a[0].max(b[0])) {
                    for gy in gcell(a[1].min(b[1]))..=gcell(a[1].max(b[1])) {
                        grid.entry((gx, gy)).or_default().push(id);
                    }
                }
            }
        }
        Index { segs, grid }
    }

    /// near() charged to the meter first: the cells it looks in and every bucket entry there (None: over budget).
    fn near_m(&self, ls: &[Line], p: [f64; 2], r: f64, m: &mut Meter) -> Option<BTreeMap<usize, Hit>> {
        let (x0, x1, y0, y1) = (gcell(p[0] - r), gcell(p[0] + r), gcell(p[1] - r), gcell(p[1] + r));
        let side = |a: i64, b: i64| (b as i128 - a as i128 + 1).clamp(0, u64::MAX as i128) as u64;
        m.take(side(x0, x1).saturating_mul(side(y0, y1)))?;
        let mut n = 0u64;
        for gx in x0..=x1 {
            for gy in y0..=y1 {
                if let Some(v) = self.grid.get(&(gx, gy)) {
                    n = n.saturating_add(v.len() as u64);
                }
            }
        }
        m.take(n)?;
        Some(self.near(ls, p, r))
    }

    /// The nearest point of each line with a segment within r of p: {line: hit} (ties: the lower segment).
    fn near(&self, ls: &[Line], p: [f64; 2], r: f64) -> BTreeMap<usize, Hit> {
        let mut best: BTreeMap<usize, (usize, Hit)> = BTreeMap::new();
        for gx in gcell(p[0] - r)..=gcell(p[0] + r) {
            for gy in gcell(p[1] - r)..=gcell(p[1] + r) {
                let Some(v) = self.grid.get(&(gx, gy)) else { continue };
                for &id in v {
                    let sg = &self.segs[id];
                    let l = &ls[sg.line];
                    let (a, b) = (l.xy[sg.k], l.xy[sg.k + 1]);
                    let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
                    let l2 = dx * dx + dy * dy;
                    if l2 <= 1e-24 {
                        continue;
                    }
                    let raw = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2;
                    let t = raw.clamp(0.0, 1.0);
                    let last = l.xy.len() - 2;
                    let past = (sg.k == 0 && raw < 0.0) || (sg.k == last && raw > 1.0);
                    let (qx, qy) = (a[0] + t * dx, a[1] + t * dy);
                    let d = ((p[0] - qx) * (p[0] - qx) + (p[1] - qy) * (p[1] - qy)).sqrt();
                    if d > r {
                        continue;
                    }
                    let len = l2.sqrt();
                    let h = Hit { dist: d, s: l.s[sg.k] + t * (l.s[sg.k + 1] - l.s[sg.k]), past,
                                  u: [dx / len, dy / len] };
                    let e = best.entry(sg.line).or_insert((id, h));
                    if d < e.1.dist || (d == e.1.dist && id < e.0) {
                        *e = (id, h);
                    }
                }
            }
        }
        best.into_iter().map(|(k, (_, h))| (k, h)).collect()
    }
}

fn within(u: [f64; 2], v: [f64; 2], tan: f64) -> bool {
    let cr = (u[0] * v[1] - u[1] * v[0]).abs();
    let dt = (u[0] * v[0] + u[1] * v[1]).abs();
    cr <= tan * dt
}

/// A coupled pair: z_a(sa) = z_b(sb).
#[derive(Clone, Copy)]
pub struct Pair {
    pub a: usize,
    pub sa: f64,
    pub b: usize,
    pub sb: f64,
    pub kind: u8,
    pub edge: usize,
}

/// A clearance: z_a(sa) - z_b(sb) >= bound (b None: z_a(sa) >= bound). class: CLASS_DECK or the crossing's code.
#[derive(Clone, Copy)]
pub struct Con {
    pub a: usize,
    pub sa: f64,
    pub b: Option<(usize, f64)>,
    pub bound: f64,
    pub class: i64,
}

fn z_of(ls: &[Line], zv: &[Vec<f64>], m: usize, t: f64) -> f64 {
    at(&ls[m].s, &zv[m], t)
}

fn con_value(ls: &[Line], zv: &[Vec<f64>], c: &Con) -> f64 {
    let za = z_of(ls, zv, c.a, c.sa);
    match c.b {
        Some((b, sb)) => za - z_of(ls, zv, b, sb) - c.bound,
        None => za - c.bound,
    }
}

/// The coupled pairs of the group edges (a, b, kind) (both members of one group, neither a duplicate).
/// vertices: the solve's pairs (ALONG / BESIDE / OUTLINE: each vertex of the follower, the edge's member that is not a
/// through one or else its second, against the leader: the heights are per vertex, so an equality between a vertex
/// and the leader's line holds as drawn); else the probes (every STEP_M along the edge's first member: the metrics).
fn couplings(ls: &[Line], edges: &[(usize, usize, u8)], idx_of: &Index, through: &[bool], vertices: bool,
             cap: usize, meter: &mut Meter) -> Option<Vec<Pair>> {
    let mut out = Vec::new();
    for (e, &(a, b, kind)) in edges.iter().enumerate() {
        if a >= ls.len() || b >= ls.len() || a == b || ls[a].root != ls[b].root || ls[a].dup || ls[b].dup
            || ls[a].xy.len() < 2 || ls[b].xy.len() < 2 {
            continue;
        }
        let (la, lb) = (&ls[a], &ls[b]);
        match kind {
            JOIN => {
                for &ea in &[0.0, la.len()] {
                    for &eb in &[0.0, lb.len()] {
                        let (p, _) = point_at(la, ea);
                        let (q, _) = point_at(lb, eb);
                        let d = ((p[0] - q[0]) * (p[0] - q[0]) + (p[1] - q[1]) * (p[1] - q[1])).sqrt();
                        if d <= JOIN_END_M {
                            out.push(Pair { a, sa: ea, b, sb: eb, kind, edge: e });
                            if out.len() > cap {
                                return None;
                            }
                        }
                    }
                }
            }
            END_ON => {
                for (x, y) in [(a, b), (b, a)] {
                    let (lx, ly) = (&ls[x], &ls[y]);
                    for &ex in &[0.0, lx.len()] {
                        let (p, _) = point_at(lx, ex);
                        if let Some(h) = idx_of.near_m(ls, p, ly.half, meter)?.get(&y) {
                            if !h.past && h.s > JOIN_END_M && h.s < ly.len() - JOIN_END_M {
                                out.push(Pair { a: x, sa: ex, b: y, sb: h.s, kind, edge: e });
                                if out.len() > cap {
                                    return None;
                                }
                            }
                        }
                    }
                }
            }
            OVERLAP_CROSS => {
                // where the centre lines cross (each segment pair; the first crossing per segment of a)
                for i in 0..la.xy.len() - 1 {
                    let (p0, p1) = (la.xy[i], la.xy[i + 1]);
                    for j in 0..lb.xy.len() - 1 {
                        let (q0, q1) = (lb.xy[j], lb.xy[j + 1]);
                        let (rx, ry) = (p1[0] - p0[0], p1[1] - p0[1]);
                        let (sx, sy) = (q1[0] - q0[0], q1[1] - q0[1]);
                        let den = rx * sy - ry * sx;
                        if den.abs() <= 1e-12 {
                            continue;
                        }
                        let (wx, wy) = (q0[0] - p0[0], q0[1] - p0[1]);
                        let t = (wx * sy - wy * sx) / den;
                        let u = (wx * ry - wy * rx) / den;
                        if (0.0..=1.0).contains(&t) && (0.0..=1.0).contains(&u) {
                            let sa = la.s[i] + t * (la.s[i + 1] - la.s[i]);
                            let sb = lb.s[j] + u * (lb.s[j + 1] - lb.s[j]);
                            out.push(Pair { a, sa, b, sb, kind, edge: e });
                            if out.len() > cap {
                                return None;
                            }
                        }
                    }
                }
            }
            _ => {}
        }
        // side by side: every edge's members where one runs beside the other (BESIDE / OUTLINE within their gap,
        // the others where the carriageways overlap or within BESIDE_GAP_M: a ramp's end on a deck beside it up to
        // the gore, two copies of one road a JOIN edge pairs only by their ends), never a crossing (over 30 degrees)
        let gap = match kind {
            OVERLAP_ALONG => 0.0,
            BESIDE_OUTLINE => OUTLINE_GAP_M,
            OVERLAP_CROSS => continue,
            _ => BESIDE_GAP_M,
        };
        let reach = la.half + lb.half + gap;
        let (f, g) = if vertices && through[b] && !through[a] { (a, b) } else if vertices { (b, a) } else { (a, b) };
        let lf = &ls[f];
        let ts: Vec<f64> = if vertices {
            lf.s.clone()
        } else {
            let n = (lf.len() / STEP_M).floor() as usize;
            (0..=n).map(|i| (i as f64 * STEP_M).min(lf.len())).collect()
        };
        let mut last = f64::NEG_INFINITY;
        for t in ts {
            if t - last <= 1e-9 {
                continue;
            }
            last = t;
            let (p, u) = point_at(lf, t);
            if let Some(h) = idx_of.near_m(ls, p, reach, meter)?.get(&g) {
                if !h.past && within(u, h.u, TAN_ALONG) {
                    out.push(Pair { a: f, sa: t, b: g, sb: h.s, kind: OVERLAP_ALONG, edge: e });
                    if out.len() > cap {
                        return None;
                    }
                }
            }
        }
    }
    Some(out)
}

/// The samples couplings() looks at (an upper bound, saturating): per edge it would take, its ends' tests, a crossing
/// edge's segment pairs, the side-by-side follower's vertices (both members': whichever follows) or probes.
fn coupling_samples(ls: &[Line], edges: &[(usize, usize, u8)], vertices: bool) -> u64 {
    let mut n = 0u64;
    let probes = |l: &Line| ((l.len() / STEP_M).floor() as u64).saturating_add(1);
    for &(a, b, kind) in edges {
        if a >= ls.len() || b >= ls.len() || a == b || ls[a].root != ls[b].root || ls[a].dup || ls[b].dup
            || ls[a].xy.len() < 2 || ls[b].xy.len() < 2 {
            continue;
        }
        let (la, lb) = (&ls[a], &ls[b]);
        n = n.saturating_add(4);
        if kind == OVERLAP_CROSS {
            n = n.saturating_add(((la.xy.len() - 1) as u64).saturating_mul((lb.xy.len() - 1) as u64));
            continue;
        }
        let f = if vertices { la.s.len().max(lb.s.len()) as u64 } else { probes(la) };
        n = n.saturating_add(f);
    }
    n
}

/// The samples clearances() looks at (saturating): every floor and every STEP_M of every member it samples.
fn clearance_samples(ls: &[Line], floors: usize) -> u64 {
    let mut n = floors as u64;
    for l in ls {
        if !l.dup && l.xy.len() >= 2 && l.level.is_finite() {
            n = n.saturating_add(((l.len() / STEP_M).floor() as u64).saturating_add(1));
        }
    }
    n
}

/// Windows [(s0, s1)] merged and sorted.
fn merge(mut w: Vec<(f64, f64)>) -> Vec<(f64, f64)> {
    w.sort_by(|x, y| x.0.partial_cmp(&y.0).unwrap().then(x.1.partial_cmp(&y.1).unwrap()));
    let mut out: Vec<(f64, f64)> = Vec::new();
    for (a, b) in w {
        if let Some(l) = out.last_mut() {
            if a <= l.1 {
                if b > l.1 {
                    l.1 = b;
                }
                continue;
            }
        }
        out.push((a, b));
    }
    out
}

fn in_any(w: &[(f64, f64)], t: f64) -> bool {
    w.iter().any(|&(a, b)| t >= a && t <= b)
}

fn inside_any(w: &[(f64, f64)], t: f64) -> bool {
    w.iter().any(|&(a, b)| t > a && t < b)
}

/// The envelope LDL^T of the symmetric matrix (rows of (col, value) with col <= row, in the factor's order) and a
/// solve of it for b. None when a pivot is not positive.
pub(crate) fn ldl_solve(n: usize, lower: &[BTreeMap<usize, f64>], rhs: &[f64]) -> Option<Vec<f64>> {
    let first: Vec<usize> = (0..n).map(|i| lower[i].keys().next().copied().unwrap_or(i).min(i)).collect();
    let mut rows: Vec<Vec<f64>> = (0..n).map(|i| vec![0.0; i - first[i] + 1]).collect();
    for i in 0..n {
        for (&j, &v) in &lower[i] {
            rows[i][j - first[i]] = v;
        }
    }
    let mut d = vec![0.0f64; n];
    for i in 0..n {
        let fi = first[i];
        for j in fi..i {
            let fj = first[j];
            let k0 = fi.max(fj);
            let mut acc = rows[i][j - fi];
            for k in k0..j {
                acc -= rows[i][k - fi] * d[k] * rows[j][k - fj];
            }
            rows[i][j - fi] = acc / d[j];
        }
        let mut acc = rows[i][i - fi];
        for k in fi..i {
            let l = rows[i][k - fi];
            acc -= l * l * d[k];
        }
        if !(acc > 0.0) || !acc.is_finite() {
            return None;
        }
        d[i] = acc;
    }
    let mut y = rhs.to_vec();
    for i in 0..n {
        let fi = first[i];
        let mut acc = y[i];
        for k in fi..i {
            acc -= rows[i][k - fi] * y[k];
        }
        y[i] = acc;
    }
    for i in 0..n {
        y[i] /= d[i];
    }
    for i in (0..n).rev() {
        let fi = first[i];
        let yi = y[i];
        for k in fi..i {
            y[k] -= rows[i][k - fi] * yi;
        }
    }
    if y.iter().all(|v| v.is_finite()) {
        Some(y)
    } else {
        None
    }
}

/// (bytes, operations) of one envelope LDL^T (ldl_solve) of the pattern adj (symmetric) in the factor order pos: the
/// envelope's f64 entries x 8, and per row of profile w (its distance to its first entry) 3 w (w + 1) / 2 + 8 w + 4
/// (the inner products' terms, the divisions, the two triangular solves: an upper bound). None: overflow.
pub(crate) fn factor_cost(adj: &[Vec<usize>], pos: &[usize]) -> Option<(u64, u64)> {
    let n = adj.len();
    let mut first: Vec<usize> = (0..n).collect();
    for v in 0..n {
        for &w in &adj[v] {
            let (pv, pw) = (pos[v], pos[w]);
            if pw < first[pv] {
                first[pv] = pw;
            }
        }
    }
    let (mut entries, mut ops) = (0u64, 0u64);
    for (p, &f) in first.iter().enumerate() {
        let w = (p - f) as u64;
        entries = entries.checked_add(w.checked_add(1)?)?;
        let inner = w.checked_mul(w.checked_add(1)?)? / 2;
        ops = ops.checked_add(inner.checked_mul(3)?)?.checked_add(w.checked_mul(8)?)?.checked_add(4)?;
    }
    Some((entries.checked_mul(8)?, ops))
}

/// A reverse Cuthill-McKee order of the graph adj (neighbours sorted), components by their lowest (degree, index).
pub(crate) fn rcm(adj: &[Vec<usize>]) -> Vec<usize> {
    let n = adj.len();
    let mut seen = vec![false; n];
    let mut order = Vec::with_capacity(n);
    let mut by: Vec<usize> = (0..n).collect();
    by.sort_by_key(|&i| (adj[i].len(), i));
    for &s in &by {
        if seen[s] {
            continue;
        }
        seen[s] = true;
        let mut q = std::collections::VecDeque::new();
        q.push_back(s);
        while let Some(v) = q.pop_front() {
            order.push(v);
            let mut nb: Vec<usize> = adj[v].iter().copied().filter(|&w| !seen[w]).collect();
            nb.sort_by_key(|&w| (adj[w].len(), w));
            for w in nb {
                if !seen[w] {
                    seen[w] = true;
                    q.push_back(w);
                }
            }
        }
    }
    order.reverse();
    order
}

/// One member's nodes: arcs, z_in there, free flags, variable ids.
struct Nodes {
    s: Vec<f64>,
    zin: Vec<f64>,
    var: Vec<Option<usize>>,
}

/// (node a, node b, t) bracketing arc t on a member's nodes
fn bracket(s: &[f64], t: f64) -> (usize, usize, f64) {
    let n = s.len();
    if t <= s[0] {
        return (0, 0, 0.0);
    }
    if t >= s[n - 1] {
        return (n - 1, n - 1, 0.0);
    }
    let (mut lo, mut hi) = (0usize, n - 1);
    while hi - lo > 1 {
        let m = (lo + hi) / 2;
        if s[m] <= t {
            lo = m;
        } else {
            hi = m;
        }
    }
    let d = s[hi] - s[lo];
    (lo, hi, if d > 0.0 { (t - s[lo]) / d } else { 0.0 })
}

/// The linear form sum c d (over free nodes) and its constant (the z_in part) of z_m(t).
fn form(nd: &[Nodes], m: usize, t: f64, sign: f64, terms: &mut Vec<(usize, f64)>) -> f64 {
    let (a, b, u) = bracket(&nd[m].s, t);
    let mut cst = 0.0;
    for (k, w) in [(a, 1.0 - u), (b, u)] {
        if w == 0.0 {
            continue;
        }
        cst += sign * w * nd[m].zin[k];
        if let Some(v) = nd[m].var[k] {
            terms.push((v, sign * w));
        }
    }
    cst
}

pub struct Report {
    pub status: i64,
    pub pairs: usize,
    pub pruned: usize,
    pub cwin: usize,
    pub unmet: usize,
    pub rounds: usize,
    pub resid: f64,             // m: the largest coupled pair's |dz| after the solve (before the fallback)
}

/// Per edge: the largest |dz| of its live pairs in the members' z.
fn eases(ls: &[Line], pairs: &[Pair], live: &[bool]) -> BTreeMap<usize, f64> {
    let mut out: BTreeMap<usize, f64> = BTreeMap::new();
    for (k, p) in pairs.iter().enumerate() {
        if !live[k] {
            continue;
        }
        let dz = pair_dz(ls, p);
        let e = out.entry(p.edge).or_insert(0.0);
        if dz > *e {
            *e = dz;
        }
    }
    out
}

fn pair_dz(ls: &[Line], p: &Pair) -> f64 {
    (at(&ls[p.a].s, &ls[p.a].z, p.sa) - at(&ls[p.b].s, &ls[p.b].z, p.sb)).abs()
}

/// m along member m from arc t to its nearest pinned end (infinite: none).
fn pin_dist(ls: &[Line], pinned: &[[bool; 2]], m: usize, t: f64) -> f64 {
    let mut d = f64::INFINITY;
    if pinned[m][0] {
        d = d.min(t);
    }
    if pinned[m][1] {
        d = d.min(ls[m].len() - t);
    }
    d
}

/// The height limit of a coupled pair: a pair of one group farther apart in z_in than a data conflict
/// (groups::CONFLICT_DZ) is not coupled there. The group's own limits (0.3-1.0 m) hold on today's heights, where the
/// pairwise mechanisms already pulled the decks together; z_in, the profile without them, is what the coupling must
/// close (twins solved apart for their own crossings stand up to 1.6 m apart in it: A7 Zillbach).
fn dz_limit(_kind: u8) -> f64 {
    groups::CONFLICT_DZ
}

/// The pinned ends and the live pairs, settled together: a pair lives when its |dz| in z_in is within dz_limit and each of its ends lies at least that |dz| / EASE_GRADE from a pinned end of its member (the ease from that pin fits); an end pins() leaves free that no
/// live JOIN / END_ON pair holds is pinned too (no end drifts off its joint), and so on until nothing changes.
fn settle(ls: &[Line], pairs: &[Pair], meter: &mut Meter) -> Option<(Vec<[bool; 2]>, Vec<bool>)> {
    let mut pinned = pins(ls, pairs, meter)?;
    // (per pair its |dz| in z_in, and per member end the pairs that can hold it: computed once, the same tests as
    // before, so a chain of many members and pairs settles in O(rounds x pairs))
    let dzs: Vec<f64> = pairs.iter().map(|p| pair_dz(ls, p)).collect();
    let mut holds: Vec<[Vec<usize>; 2]> = (0..ls.len()).map(|_| [Vec::new(), Vec::new()]).collect();
    for (k, p) in pairs.iter().enumerate() {
        for e in 0..2 {
            if (p.kind == JOIN || p.kind == END_ON) && p.sa == (if e == 0 { 0.0 } else { ls[p.a].len() }) {
                holds[p.a][e].push(k);
            }
            if p.kind == JOIN && p.sb == (if e == 0 { 0.0 } else { ls[p.b].len() }) {
                holds[p.b][e].push(k);
            }
        }
    }
    loop {
        // (each pass: every pair's test and every member end's holders, at most the pairs again)
        meter.take((pairs.len() as u64).saturating_mul(2).saturating_add(2 * ls.len() as u64))?;
        let live: Vec<bool> = pairs.iter().zip(&dzs).map(|(p, &dz)| {
            let need = dz / EASE_GRADE;
            dz <= dz_limit(p.kind) && pin_dist(ls, &pinned, p.a, p.sa) >= need
                && pin_dist(ls, &pinned, p.b, p.sb) >= need
        }).collect();
        let mut changed = false;
        for m in 0..ls.len() {
            if ls[m].dup || ls[m].xy.len() < 2 {
                continue;
            }
            for e in 0..2 {
                if pinned[m][e] {
                    continue;
                }
                let held = holds[m][e].iter().any(|&k| live[k]);
                if !held {
                    pinned[m][e] = true;
                    changed = true;
                }
            }
        }
        if !changed {
            return Some((pinned, live));
        }
    }
}

/// The solve of one structure's members `mem` (indices into ls): new z per vertex for those members (None: keep
/// fallback heights), its report and (a failure) the clearances short or the steep members.
#[allow(clippy::too_many_arguments)]
fn solve_one(ls: &[Line], mem: &[usize], pairs: &[Pair], live_in: &[bool], cons: &[Con], pinned: &[[bool; 2]],
             cwin_extra: &BTreeMap<usize, Vec<(f64, f64, usize)>>, drop_cwin: &[bool], through: &[bool],
             budget: &Budget, flops_left: &mut u64) -> (Option<BTreeMap<usize, Vec<f64>>>, Report, Vec<usize>) {
    // windows per member: couplings' (with their eases) and clearance windows (not dropped)
    let mut rep = Report { status: S_SOLVED, pairs: pairs.len(), pruned: 0, cwin: 0, unmet: 0, rounds: 0,
                           resid: 0.0 };
    let ease_of = eases(ls, pairs, live_in);
    let ease = |edge: usize| -> f64 {
        let dz = ease_of.get(&edge).copied().unwrap_or(0.0);
        EASE_MIN_M.max(dz / EASE_GRADE)
    };
    let mut live: Vec<Pair> = Vec::new();
    for (k, p) in pairs.iter().enumerate() {
        if live_in[k] {
            live.push(*p);
        } else {
            rep.pruned += 1;
        }
    }
    let mut cint: BTreeMap<usize, Vec<(f64, f64)>> = BTreeMap::new();     // coupling intervals per member
    let mut wins: BTreeMap<usize, Vec<(f64, f64)>> = BTreeMap::new();
    {
        let mut span: BTreeMap<(usize, usize), (f64, f64)> = BTreeMap::new();  // (edge, member) -> arc range
        for p in &live {
            for (m, t) in [(p.a, p.sa), (p.b, p.sb)] {
                let r = span.entry((p.edge, m)).or_insert((t, t));
                if t < r.0 {
                    r.0 = t;
                }
                if t > r.1 {
                    r.1 = t;
                }
            }
        }
        for (&(edge, m), &(a, b)) in &span {
            let e = ease(edge);
            cint.entry(m).or_default().push((a, b));
            wins.entry(m).or_default().push(((a - e).max(0.0), (b + e).min(ls[m].len())));
        }
    }
    let mut used_cwin = Vec::new();
    for &m in mem {
        if let Some(v) = cwin_extra.get(&m) {
            for &(a, b, ci) in v {
                if !drop_cwin[ci] {
                    wins.entry(m).or_default().push((a, b));
                    used_cwin.push(ci);
                    rep.cwin += 1;
                }
            }
        }
    }
    let wins: BTreeMap<usize, Vec<(f64, f64)>> = wins.into_iter().map(|(m, w)| (m, merge(w))).collect();
    let cint: BTreeMap<usize, Vec<(f64, f64)>> = cint.into_iter().map(|(m, w)| (m, merge(w))).collect();
    if wins.is_empty() {
        rep.status = S_PINNED;
        return (None, rep, used_cwin);
    }
    // nodes
    let mut nd: Vec<Nodes> = Vec::with_capacity(ls.len());
    let mut nvar = 0usize;
    let mut owner: Vec<(usize, usize)> = Vec::new();        // var -> (member, node)
    for (m, l) in ls.iter().enumerate() {
        let w = wins.get(&m);
        // (the vertices: the heights are per vertex, so what is solved is what is drawn; the lines come at
        // roads.DECK_STEP)
        let mut s: Vec<f64> = l.s.clone();
        s.sort_by(|x, y| x.partial_cmp(y).unwrap());
        // (a grid node within STEP_M / 4 of a vertex or of the last node kept is left out: no tiny spans)
        let mut kept: Vec<f64> = Vec::with_capacity(s.len());
        let is_vertex = |t: f64| l.s.binary_search_by(|v| v.partial_cmp(&t).unwrap()).is_ok();
        for t in s {
            if let Some(&last) = kept.last() {
                if t - last <= 1e-9 {
                    continue;
                }
                if t - last < STEP_M / 4.0 {
                    if is_vertex(t) && !is_vertex(last) {
                        kept.pop();
                    } else if !is_vertex(t) {
                        continue;
                    }
                }
            }
            kept.push(t);
        }
        let zin: Vec<f64> = kept.iter().map(|&t| at(&l.s, &l.z, t)).collect();
        let mut var = vec![None; kept.len()];
        if let Some(w) = w {
            let len = l.len();
            for (k, &t) in kept.iter().enumerate() {
                let end_pin = (t <= 1e-9 && pinned[m][0]) || (t >= len - 1e-9 && pinned[m][1]);
                let free = !end_pin && (inside_any(w, t) || (t <= 1e-9 && in_any(w, t)) || (t >= len - 1e-9 && in_any(w, t)));
                if free {
                    var[k] = Some(nvar);
                    owner.push((m, k));
                    nvar += 1;
                }
            }
        }
        nd.push(Nodes { s: kept, zin, var });
    }
    if nvar == 0 {
        rep.status = S_PINNED;
        return (None, rep, used_cwin);
    }
    // the fixed part of the matrix: weights, curvature, couplings
    let mut base: BTreeMap<(usize, usize), f64> = BTreeMap::new();
    let mut brhs = vec![0.0f64; nvar];
    let add = |mat: &mut BTreeMap<(usize, usize), f64>, rhs: &mut Vec<f64>, terms: &[(usize, f64)], cst: f64, w: f64| {
        // w (sum c d - cst)^2
        for &(i, ci) in terms {
            rhs[i] += w * ci * cst;
            for &(j, cj) in terms {
                if j <= i {
                    *mat.entry((i, j)).or_insert(0.0) += w * ci * cj;
                }
            }
        }
    };
    for (m, n) in nd.iter().enumerate() {
        let k = n.s.len();
        if k < 2 {
            continue;
        }
        let wm = if through[m] { THROUGH_W } else { 1.0 };
        let ci = cint.get(&m);
        for i in 0..k {
            let Some(v) = n.var[i] else { continue };
            let hl = if i > 0 { n.s[i] - n.s[i - 1] } else { 0.0 };
            let hr = if i + 1 < k { n.s[i + 1] - n.s[i] } else { 0.0 };
            let h = 0.5 * (hl + hr);
            let w = if ci.is_some_and(|c| in_any(c, n.s[i])) { wm } else { EPS_W };
            *base.entry((v, v)).or_insert(0.0) += w * h.max(1e-3);
        }
        for i in 1..k - 1 {
            let (h0, h1) = (n.s[i] - n.s[i - 1], n.s[i + 1] - n.s[i]);
            if !(n.var[i - 1].is_some() || n.var[i].is_some() || n.var[i + 1].is_some()) {
                continue;
            }
            let c = [2.0 / (h0 * (h0 + h1)), -2.0 / (h0 * h1), 2.0 / (h1 * (h0 + h1))];
            let mut terms = Vec::new();
            for (j, cj) in c.iter().enumerate() {
                if let Some(v) = n.var[i - 1 + j] {
                    terms.push((v, *cj));
                }
            }
            add(&mut base, &mut brhs, &terms, 0.0, LAMBDA * 0.5 * (h0 + h1));
        }
    }
    for p in &live {
        let mut terms = Vec::new();
        let ca = form(&nd, p.a, p.sa, 1.0, &mut terms);
        let cb = form(&nd, p.b, p.sb, -1.0, &mut terms);
        // z_a - z_b = 0  <=>  sum c d = -(ca + cb)
        add(&mut base, &mut brhs, &terms, -(ca + cb), MU);
    }
    // the clearances that touch a free node: a clearance window's at its bound, any other never worse than z_in
    // (the chain solve's own deficits stay its own: min(bound, z_in's))
    let mut cl: Vec<(Vec<(usize, f64)>, f64, usize)> = Vec::new();     // (terms, rhs: sum c d >= rhs, con index)
    for (ci, c) in cons.iter().enumerate() {
        let mut terms = Vec::new();
        let ca = form(&nd, c.a, c.sa, 1.0, &mut terms);
        let cb = match c.b {
            Some((b, sb)) => form(&nd, b, sb, -1.0, &mut terms),
            None => 0.0,
        };
        if terms.is_empty() {
            continue;
        }
        let rhs = c.bound - ca - cb;
        cl.push((terms, if used_cwin.contains(&ci) { rhs } else { rhs.min(0.0) }, ci));
    }
    // the order (by the full pattern, every clearance included)
    let mut adj: Vec<Vec<usize>> = vec![Vec::new(); nvar];
    for &(i, j) in base.keys() {
        if i != j {
            adj[i].push(j);
            adj[j].push(i);
        }
    }
    for (terms, _, _) in &cl {
        for &(i, _) in terms {
            for &(j, _) in terms {
                if i != j {
                    adj[i].push(j);
                }
            }
        }
    }
    for a in adj.iter_mut() {
        a.sort_unstable();
        a.dedup();
    }
    let order = rcm(&adj);
    let mut pos = vec![0usize; nvar];
    for (p, &v) in order.iter().enumerate() {
        pos[v] = p;
    }
    // the budgets, before the factor is allocated: its envelope over the full pattern (every clearance in it: each
    // round's lies within) and one factorisation's arithmetic (factor_cost), checked; every round is charged to the
    // call's budget before it runs
    let Some((bytes, per_round)) = factor_cost(&adj, &pos) else {
        rep.status = S_BUDGET;
        return (None, rep, used_cwin);
    };
    if bytes > budget.factor_bytes {
        rep.status = S_BUDGET;
        return (None, rep, used_cwin);
    }
    // (warm start: the clearances z_in already misses, the ones their windows are for, active from the first round)
    let mut active: Vec<bool> = cl.iter().map(|(_t, r, ci)| used_cwin.contains(ci) && *r > ACTIVE_TOL).collect();
    let mut d = vec![0.0f64; nvar];
    for round in 0..ROUNDS {
        rep.rounds = round + 1;
        match flops_left.checked_sub(per_round) {
            Some(r) => *flops_left = r,
            None => {
                rep.status = S_BUDGET;
                return (None, rep, used_cwin);
            }
        }
        let mut mat = base.clone();
        let mut rhs = brhs.clone();
        for (k, (terms, r, _)) in cl.iter().enumerate() {
            if active[k] {
                add(&mut mat, &mut rhs, terms, *r, MU);
            }
        }
        let mut lower: Vec<BTreeMap<usize, f64>> = vec![BTreeMap::new(); nvar];
        for (&(i, j), &v) in &mat {
            let (pi, pj) = (pos[i], pos[j]);
            let (r, c) = if pi >= pj { (pi, pj) } else { (pj, pi) };
            *lower[r].entry(c).or_insert(0.0) += v;
        }
        let mut b = vec![0.0f64; nvar];
        for v in 0..nvar {
            b[pos[v]] = rhs[v];
        }
        let Some(x) = ldl_solve(nvar, &lower, &b) else {
            rep.status = S_SOLVER;
            return (None, rep, used_cwin);
        };
        for v in 0..nvar {
            d[v] = x[pos[v]];
        }
        // (a primal-dual active set on the penalties: a held clearance whose penalty pulls its side DOWN onto the
        // bound (the solution would stand above it) is let go; one short by more than ACTIVE_TOL is held)
        let mut more = false;
        for (k, (terms, r, _)) in cl.iter().enumerate() {
            let lhs: f64 = terms.iter().fold(0.0, |acc, &(i, c)| acc + c * d[i]);
            if !active[k] && lhs < r - ACTIVE_TOL {
                active[k] = true;
                more = true;
            } else if active[k] && lhs > r + RELEASE_TOL {
                active[k] = false;
                more = true;
            }
        }
        if !more {
            break;
        }
    }
    // the new heights (1 mm) and the checks
    let mut zv: Vec<Vec<f64>> = nd.iter().map(|n| n.zin.clone()).collect();
    for (v, &(m, k)) in owner.iter().enumerate() {
        zv[m][k] = ((nd[m].zin[k] + d[v]) * 1000.0).round() / 1000.0;
    }
    let zline = |m: usize, t: f64| at(&nd[m].s, &zv[m], t);
    for p in &live {
        rep.resid = rep.resid.max((zline(p.a, p.sa) - zline(p.b, p.sb)).abs());
    }
    if rep.resid > MARGIN {
        rep.status = S_INFEASIBLE;
        return (None, rep, used_cwin);
    }
    let mut short = Vec::new();
    for (terms, r, ci) in &cl {
        let lhs: f64 = terms.iter().fold(0.0, |acc, &(i, c)| acc + c * d[i]);
        if lhs < r - MARGIN {
            short.push(*ci);
        }
    }
    // (rounds spent with every clearance within MARGIN: accepted)
    if !short.is_empty() {
        rep.status = S_CLEARANCE;
        return (None, rep, short);
    }
    // grade: per member, no segment with a moved end (one straddling a window's edge too, Codex review) steeper than
    // max(MAX_GRADE, the steepest such segment of z_in) + GRADE_ADD, on the final rounded heights
    let mut steep: Vec<usize> = Vec::new();
    for &m in wins.keys() {
        let n = &nd[m];
        let moved = |i: usize| n.var[i].is_some() || n.var[i + 1].is_some();
        let mut gin = 0.0f64;
        for i in 0..n.s.len().saturating_sub(1) {
            let h = n.s[i + 1] - n.s[i];
            if moved(i) && h > 1e-6 {
                gin = gin.max(((n.zin[i + 1] - n.zin[i]) / h).abs());
            }
        }
        let lim = MAX_GRADE.max(gin) + GRADE_ADD + 1e-6;
        for i in 0..n.s.len().saturating_sub(1) {
            let h = n.s[i + 1] - n.s[i];
            if moved(i) && h > 0.25 && ((zv[m][i + 1] - zv[m][i]) / h).abs() > lim {
                steep.push(m);
            }
        }
    }
    if !steep.is_empty() {
        steep.sort_unstable();
        steep.dedup();
        rep.status = S_GRADE;
        return (None, rep, steep);
    }
    // the vertices' heights
    let mut out = BTreeMap::new();
    for &m in mem {
        let l = &ls[m];
        let z: Vec<f64> = l.s.iter().map(|&t| ((at(&nd[m].s, &zv[m], t)) * 1000.0).round() / 1000.0).collect();
        out.insert(m, z);
    }
    (Some(out), rep, used_cwin)
}

/// The pinned ends of every member: [start, end] (an abutment, or a joint with a member it is not JOIN-coupled to).
fn pins(ls: &[Line], pairs: &[Pair], meter: &mut Meter) -> Option<Vec<[bool; 2]>> {
    let n = ls.len();
    // (the end search below tests every end against every other member's: charged first, with the pairs' index)
    meter.take((n as u64).saturating_mul(n as u64).saturating_mul(4).saturating_add(2 * pairs.len() as u64))?;
    let mut out = vec![[true, true]; n];
    // (per member end: the members JOIN-coupled at it, and whether an END_ON pair leaves from it: indexed once)
    let mut joins: Vec<[Vec<usize>; 2]> = (0..n).map(|_| [Vec::new(), Vec::new()]).collect();
    let mut ons = vec![[false, false]; n];
    for pr in pairs {
        for e in 0..2 {
            let ta = if e == 0 { 0.0 } else { ls[pr.a].len() };
            let tb = if e == 0 { 0.0 } else { ls[pr.b].len() };
            if pr.kind == JOIN {
                if pr.sa == ta {
                    joins[pr.a][e].push(pr.b);
                }
                if pr.sb == tb {
                    joins[pr.b][e].push(pr.a);
                }
            } else if pr.kind == END_ON && pr.sa == ta {
                ons[pr.a][e] = true;
            }
        }
    }
    for j in joins.iter_mut() {
        for v in j.iter_mut() {
            v.sort_unstable();
            v.dedup();
        }
    }
    let ends: Vec<[[f64; 2]; 2]> = ls.iter().map(|l| {
        if l.xy.is_empty() { [[f64::NAN; 2]; 2] } else { [l.xy[0], l.xy[l.xy.len() - 1]] }
    }).collect();
    for m in 0..n {
        if ls[m].dup || ls[m].xy.len() < 2 {
            continue;
        }
        for e in 0..2 {
            let p = ends[m][e];
            // the members whose ends meet this one
            let mut at_node: Vec<usize> = Vec::new();
            for (k, ek) in ends.iter().enumerate() {
                if k == m {
                    continue;
                }
                for q in ek {
                    let d = ((p[0] - q[0]) * (p[0] - q[0]) + (p[1] - q[1]) * (p[1] - q[1])).sqrt();
                    if d <= JOIN_END_M {
                        at_node.push(k);
                    }
                }
            }
            at_node.sort_unstable();
            at_node.dedup();
            let joined = |k: usize| joins[m][e].binary_search(&k).is_ok();
            let on = ons[m][e];
            let free = if at_node.is_empty() { on } else { at_node.iter().all(|&k| joined(k)) };
            out[m][e] = !free;
        }
    }
    Some(out)
}

/// The clearances of the chain: crossing floors [(member, x, y, floor, class)] and every deck over a deck of another
/// level (sampled every STEP_M along the upper one).
fn clearances(ls: &[Line], floors: &[(usize, [f64; 2], f64, i64)], idx: &Index, cap: usize, meter: &mut Meter)
               -> Option<Vec<Con>> {
    let mut out = Vec::new();
    for &(m, p, floor, class) in floors {
        if m >= ls.len() || ls[m].xy.len() < 2 || ls[m].dup {
            continue;
        }
        let Some(h) = idx.near_m(ls, p, ls[m].half + 50.0, meter)?.get(&m).copied() else { continue };
        // (a crossing near the member's end: a junction at grade, as roads._clearance_deficits leaves it)
        if h.s <= AT_GRADE_REACH_M || h.s >= ls[m].len() - AT_GRADE_REACH_M {
            continue;
        }
        out.push(Con { a: m, sa: h.s, b: None, bound: floor, class });
        if out.len() > cap {
            return None;
        }
    }
    let maxh = ls.iter().fold(0.0f64, |a, l| a.max(l.half));
    for (i, li) in ls.iter().enumerate() {
        if li.dup || li.xy.len() < 2 || !li.level.is_finite() {
            continue;
        }
        let n = (li.len() / STEP_M).floor() as usize;
        for k in 0..=n {
            let t = (k as f64 * STEP_M).min(li.len());
            let (p, _) = point_at(li, t);
            for (j, h) in idx.near_m(ls, p, li.half.max(maxh), meter)? {
                let lj = &ls[j];
                if j == i || lj.dup || !lj.level.is_finite() || lj.level >= li.level || h.past {
                    continue;
                }
                // over at least half the narrower deck (roads._deck_stacks)
                if h.dist > li.half.max(lj.half) {
                    continue;
                }
                // not where the two part from a shared end (roads._deck_stacks: within the run it takes to fall away)
                let fall = lj.under / STACK_GRADE;
                let mut near_shared = false;
                for (ei, ti) in [(li.xy[0], 0.0), (li.xy[li.xy.len() - 1], li.len())] {
                    for ej in [lj.xy[0], lj.xy[lj.xy.len() - 1]] {
                        let d = ((ei[0] - ej[0]) * (ei[0] - ej[0]) + (ei[1] - ej[1]) * (ei[1] - ej[1])).sqrt();
                        if d < JOIN_END_M && (t - ti).abs() < fall {
                            near_shared = true;
                        }
                    }
                }
                if near_shared {
                    continue;
                }
                out.push(Con { a: i, sa: t, b: Some((j, h.s)), bound: lj.under, class: CLASS_DECK });
                if out.len() > cap {
                    return None;
                }
            }
        }
    }
    Some(out)
}

/// The through members: per group, the members that are no link and of the group's best (lowest) class rank; a group
/// of links alone: its best rank's.
fn through_of(ls: &[Line]) -> Vec<bool> {
    let mut best: BTreeMap<usize, (bool, i64)> = BTreeMap::new();   // root -> (has a non-link, best rank)
    for l in ls {
        if l.dup {
            continue;
        }
        let e = best.entry(l.root).or_insert((!l.link, l.rank));
        if !l.link && !e.0 {
            *e = (true, l.rank);
        } else if l.link == !e.0 && l.rank < e.1 {
            e.1 = l.rank;
        }
    }
    ls.iter().map(|l| {
        let (nl, r) = best.get(&l.root).copied().unwrap_or((false, i64::MAX));
        !l.dup && l.link != nl && l.rank == r
    }).collect()
}

pub struct Solved {
    pub z: Vec<Vec<f64>>,                    // per member: the joint heights (fallback: today's)
    pub reports: Vec<(usize, Report)>,       // per structure root
    pub pairs: Vec<Pair>,
    pub cons: Vec<Con>,
    pub pruned: Vec<bool>,                   // per pair: within its ease of a pinned end (not coupled)
}

/// structure_heights' core: ls (z: z_in), today's heights per member (the fallback), the group edges, the floors.
pub fn structure_heights_core(ls: &[Line], today: &[Vec<f64>], edges: &[(usize, usize, u8)],
                              floors: &[(usize, [f64; 2], f64, i64)]) -> Solved {
    structure_heights_budget(ls, today, edges, floors, &BUDGET)
}

/// The whole chain over a budget (the samples, the couplings, the clearances, the index): today's heights, every
/// structure S_BUDGET, no pairs or clearances (nothing generated past the budget is kept).
fn over_budget(ls: &[Line], today: &[Vec<f64>]) -> Solved {
    let mut roots: Vec<usize> = ls.iter().filter(|l| !l.dup).map(|l| l.root).collect();
    roots.sort_unstable();
    roots.dedup();
    let reports = roots.into_iter().map(|r| (r, Report { status: S_BUDGET, pairs: 0, pruned: 0, cwin: 0, unmet: 0,
                                                          rounds: 0, resid: 0.0 })).collect();
    Solved { z: today.to_vec(), reports, pairs: Vec::new(), cons: Vec::new(), pruned: Vec::new() }
}

/// structure_heights_core under the given budgets.
pub fn structure_heights_budget(ls: &[Line], today: &[Vec<f64>], edges: &[(usize, usize, u8)],
                                floors: &[(usize, [f64; 2], f64, i64)], budget: &Budget) -> Solved {
    // (the counts first: nothing of the index, the couplings or the clearances is made over them)
    let samples = coupling_samples(ls, edges, true).saturating_add(coupling_samples(ls, edges, false))
        .saturating_add(clearance_samples(ls, floors.len()));
    if samples > budget.samples || grid_entries(ls) > budget.grid {
        return over_budget(ls, today);
    }
    let mut meter = Meter(budget.work);
    match core_metered(ls, today, edges, floors, budget, &mut meter) {
        Some(got) => got,
        None => over_budget(ls, today),
    }
}

/// structure_heights_budget's work past the counts; None: over budget (the meter or the caps).
fn core_metered(ls: &[Line], today: &[Vec<f64>], edges: &[(usize, usize, u8)], floors: &[(usize, [f64; 2], f64, i64)],
                budget: &Budget, meter: &mut Meter) -> Option<Solved> {
    let idx = Index::new(ls);
    let through = through_of(ls);
    let pairs = couplings(ls, edges, &idx, &through, true, budget.pairs, meter)?;
    let probes = couplings(ls, edges, &idx, &through, false, budget.pairs, meter)?;
    let cons = clearances(ls, floors, &idx, budget.pairs, meter)?;
    let mut flops_left = budget.flops;
    let nvert: u64 = ls.iter().fold(0u64, |a, l| a.saturating_add(l.xy.len() as u64));
    let (pinned, live) = settle(ls, &pairs, meter)?;
    let zin: Vec<Vec<f64>> = ls.iter().map(|l| l.z.clone()).collect();
    // clearance windows: a deck over a deck of the chain that z_in misses by more than MARGIN (one chain, one
    // clearance: the upper level of a stack), on the upper member, away from its ends. A crossing floor is held only
    // where the members move (inside their windows): the chain solve's own deficits stay its own
    let mut cwin: BTreeMap<usize, Vec<(f64, f64, usize)>> = BTreeMap::new();
    for (ci, c) in cons.iter().enumerate() {
        if c.class != CLASS_DECK {
            continue;
        }
        let v = con_value(ls, &zin, c);
        let l = &ls[c.a];
        if v < -MARGIN && c.sa > AT_GRADE_REACH_M && c.sa < l.len() - AT_GRADE_REACH_M {
            let e = EASE_MIN_M.max(-v / EASE_GRADE);
            cwin.entry(c.a).or_default().push(((c.sa - e).max(0.0), (c.sa + e).min(l.len()), ci));
        }
    }
    let mut roots: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
    for (m, l) in ls.iter().enumerate() {
        if !l.dup {
            roots.entry(l.root).or_default().push(m);
        }
    }
    let mut z: Vec<Vec<f64>> = zin.clone();
    let mut reports = Vec::new();
    // structures in (lowest level, root) order: an upper one's floors are the lower ones' joint heights
    let mut order: Vec<(f64, usize)> = roots.iter().map(|(&r, mem)| {
        (mem.iter().map(|&m| ls[m].level).fold(f64::INFINITY, |a, b| if b < a { b } else { a }), r)
    }).collect();
    order.sort_by(|x, y| x.0.partial_cmp(&y.0).unwrap_or(std::cmp::Ordering::Equal).then(x.1.cmp(&y.1)));
    let mut cur: Vec<Line> = ls.iter().map(|l| Line { xy: l.xy.clone(), s: l.s.clone(), z: l.z.clone(), half: l.half,
        level: l.level, root: l.root, dup: l.dup, rank: l.rank, link: l.link, under: l.under }).collect();
    for &(_lv, r) in &order {
        let mem = &roots[&r];
        // (its set-up scans the chain's members, pairs and clearances: charged)
        meter.take((ls.len() as u64).saturating_add(pairs.len() as u64)
            .saturating_add((cons.len() as u64).saturating_mul(3)))?;
        let inset: Vec<bool> = (0..ls.len()).map(|m| ls[m].root == r && !ls[m].dup).collect();
        let mine: Vec<usize> = (0..pairs.len()).filter(|&k| inset[pairs[k].a] && inset[pairs[k].b]).collect();
        let my_pairs: Vec<Pair> = mine.iter().map(|&k| pairs[k]).collect();
        let my_live: Vec<bool> = mine.iter().map(|&k| live[k]).collect();
        let my_cons: Vec<Con> = cons.iter().copied().filter(|c| inset[c.a] || c.b.is_some_and(|(b, _)| inset[b]))
            .collect();
        // (the con indices of cwin are the chain's; remap to my_cons)
        let mut remap: BTreeMap<usize, usize> = BTreeMap::new();
        {
            let mut k = 0usize;
            for (ci, c) in cons.iter().enumerate() {
                if inset[c.a] || c.b.is_some_and(|(b, _)| inset[b]) {
                    remap.insert(ci, k);
                    k += 1;
                }
            }
        }
        let my_cwin: BTreeMap<usize, Vec<(f64, f64, usize)>> = cwin.iter().filter(|(m, _)| inset[**m])
            .map(|(m, v)| (*m, v.iter().filter_map(|&(a, b, ci)| remap.get(&ci).map(|&k| (a, b, k))).collect()))
            .collect();
        let mut drop = vec![false; my_cons.len()];
        let mut got = None;
        let mut rep = Report { status: S_PINNED, pairs: 0, pruned: 0, cwin: 0, unmet: 0, rounds: 0, resid: 0.0 };
        // the members' fixed neighbours (other structures) at their current heights
        for _attempt in 0..3 {
            // (an attempt: every member's nodes, its pairs and clearances, and those again in each round)
            let scans = ((my_pairs.len() + my_cons.len()) as u64).saturating_mul(ROUNDS as u64 + 2);
            meter.take(nvert.saturating_add(scans))?;
            let (g, rp, info) = solve_one(&cur, mem, &my_pairs, &my_live, &my_cons, &pinned, &my_cwin, &drop,
                                          &through, budget, &mut flops_left);
            rep = rp;
            if g.is_some() || rep.status == S_PINNED {
                got = g;
                break;
            }
            // a failure: drop the clearance windows involved (CLEARANCE: the short constraints'; GRADE: the steep
            // members'), once; a failure without any falls back
            let mut dropped = false;
            if rep.status == S_INFEASIBLE {
                // (a clearance window fighting a coupling: the windows go, the couplings stay)
                for v in my_cwin.values() {
                    for w in v {
                        if !drop[w.2] {
                            drop[w.2] = true;
                            dropped = true;
                        }
                    }
                }
            } else if rep.status == S_CLEARANCE {
                for &ci in &info {
                    if !drop[ci] && my_cwin.values().any(|v| v.iter().any(|w| w.2 == ci)) {
                        drop[ci] = true;
                        dropped = true;
                    }
                }
            } else if rep.status == S_GRADE {
                for &m in &info {
                    if let Some(v) = my_cwin.get(&m) {
                        for w in v {
                            if !drop[w.2] {
                                drop[w.2] = true;
                                dropped = true;
                            }
                        }
                    }
                }
            }
            if !dropped {
                break;
            }
        }
        rep.unmet = drop.iter().filter(|&&x| x).count();
        match got {
            Some(g) => {
                for (m, zz) in g {
                    cur[m].z = zz.clone();
                    z[m] = zz;
                }
            }
            None if rep.status != S_PINNED => {
                for &m in mem {
                    z[m] = today[m].clone();
                    cur[m].z = today[m].clone();
                }
            }
            None => {}
        }
        reports.push((r, rep));
    }
    // the clearances between two structures again on the assembled heights: one solved against a neighbour's
    // z_in that then fell back to today's can be short. Both fall back (today's heights, where nothing is worse
    // than today) until none is (Codex review)
    loop {
        // (a pass: every clearance and the reverted structures' members)
        meter.take((cons.len() as u64).saturating_add(nvert))?;
        let mut bad: Vec<usize> = Vec::new();
        for c in &cons {
            let Some((b, _)) = c.b else { continue };
            let (ra, rb) = (ls[c.a].root, ls[b].root);
            if ra == rb || ls[c.a].dup || ls[b].dup {
                continue;
            }
            let now = con_value(ls, &z, c);
            let was = con_value(ls, today, c);
            if now < was.min(0.0) - MARGIN {
                for r in [ra, rb] {
                    if roots[&r].iter().any(|&m| z[m] != today[m]) {
                        bad.push(r);
                    }
                }
            }
        }
        if bad.is_empty() {
            break;
        }
        bad.sort_unstable();
        bad.dedup();
        for r in bad {
            for &m in &roots[&r] {
                z[m] = today[m].clone();
            }
            for (rr, rep) in reports.iter_mut() {
                if *rr == r {
                    rep.status = S_CLEARANCE;
                }
            }
        }
    }
    // (the probes "free": inside the span the solve couples on their member for their edge, between its first and
    // last live pair; the rest lie in an ease, by a pinned abutment or where the pair is over its height limit)
    let mut span: BTreeMap<(usize, usize), (f64, f64)> = BTreeMap::new();
    for (k, p) in pairs.iter().enumerate() {
        if !live[k] {
            continue;
        }
        for (m, t) in [(p.a, p.sa), (p.b, p.sb)] {
            let r = span.entry((p.edge, m)).or_insert((t, t));
            r.0 = r.0.min(t);
            r.1 = r.1.max(t);
        }
    }
    let pruned = probes.iter().map(|p| {
        let inside = |m: usize, t: f64| span.get(&(p.edge, m)).is_some_and(|&(a, b)| t >= a - 1e-9 && t <= b + 1e-9);
        !(inside(p.a, p.sa) && inside(p.b, p.sb))
    }).collect();
    Some(Solved { z, reports, pairs: probes, cons, pruned })
}

/// The metrics of heights zv (per member) on the coupled pairs and clearances: per structure root (twin_dz: the
/// largest |dz| of BESIDE / ALONG / OUTLINE pairs; twin_free: of those inside the span the solve couples (not in an
/// ease by a pinned end: the abutments the plans keep; not where the pair is over its edge's height limit in z_in); seam: of JOIN / END_ON / CROSS pairs; the largest grade change between consecutive
/// segments of a member (over 0.5 m)), and per (root, class) the least clearance margin.
pub fn metrics(ls: &[Line], zv: &[Vec<f64>], pairs: &[Pair], pruned: &[bool], cons: &[Con])
               -> (Vec<(usize, f64, f64, f64, f64)>, Vec<(usize, i64, f64)>) {
    let mut by: BTreeMap<usize, [f64; 4]> = BTreeMap::new();
    for l in ls.iter() {
        if !l.dup {
            by.entry(l.root).or_insert([0.0; 4]);
        }
    }
    for (k, p) in pairs.iter().enumerate() {
        let dz = (z_of(ls, zv, p.a, p.sa) - z_of(ls, zv, p.b, p.sb)).abs();
        let e = by.entry(ls[p.a].root).or_insert([0.0; 4]);
        match p.kind {
            OVERLAP_ALONG | BESIDE | BESIDE_OUTLINE => {
                e[0] = e[0].max(dz);
                if !pruned.get(k).copied().unwrap_or(false) {
                    e[1] = e[1].max(dz);
                }
            }
            _ => e[2] = e[2].max(dz),
        }
    }
    for (m, l) in ls.iter().enumerate() {
        if l.dup {
            continue;
        }
        let mut gmax = 0.0f64;
        let mut prev: Option<f64> = None;
        for k in 0..l.s.len().saturating_sub(1) {
            let h = l.s[k + 1] - l.s[k];
            if h <= 0.5 {
                continue;
            }
            let g = (zv[m][k + 1] - zv[m][k]) / h;
            if let Some(p) = prev {
                gmax = gmax.max((g - p).abs());
            }
            prev = Some(g);
        }
        let e = by.entry(l.root).or_insert([0.0; 4]);
        e[3] = e[3].max(gmax);
    }
    let mut cm: BTreeMap<(usize, i64), f64> = BTreeMap::new();
    for c in cons {
        let v = con_value(ls, zv, c);
        let e = cm.entry((ls[c.a].root, c.class)).or_insert(f64::INFINITY);
        if v < *e {
            *e = v;
        }
    }
    (by.into_iter().map(|(r, v)| (r, v[0], v[1], v[2], v[3])).collect(), cm.into_iter().map(|((r, c), v)| (r, c, v)).collect())
}

type StructRow = (i64, i64, i64, i64, i64, i64, i64, f64);
type MetricRow = (i64, f64, f64, f64, f64);
type ClearRow = (i64, i64, f64);

/// roads / structures.py (BNE_DECK_STRUCT=shadow): the joint heights of one chain's structures. pts (P, 2) every
/// member's line, member k rows off[k]..off[k + 1]; z_in (P,) the chain profile without the pairwise mechanisms;
/// today (P,) the heights published today (a fallback structure keeps them; the metrics' "today"); half, level (N,);
/// root, dup (N,) from structure_groups (dup[k] != k: a duplicate, left as it is); rank (N,) class rank (lower: more
/// important), link (N,) 0/1; under (N,) m a deck over the member stands above it; edges (E, 3) (a, b, kind) the
/// groups' edges; floors (C, 3) x y floor_z with fmem (C,) the member and fclass (C,) a class code >= 0.
/// Returns (z (P,), [(root, status, pairs, pruned, clearance windows, unmet, rounds, residual m)], today's metrics
/// [(root, twin_dz, seam, grade change)], the joint ones, today's clearance margins [(root, class, m)], the joint
/// ones). Members are the chain's alone (no outside parts).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn structure_heights<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                              z_in: PyReadonlyArray1<'py, f64>, today: PyReadonlyArray1<'py, f64>,
                              half: PyReadonlyArray1<'py, f64>, level: PyReadonlyArray1<'py, f64>,
                              root: PyReadonlyArray1<'py, i64>, dup: PyReadonlyArray1<'py, i64>,
                              rank: PyReadonlyArray1<'py, i64>, link: PyReadonlyArray1<'py, i64>,
                              under: PyReadonlyArray1<'py, f64>, edges: PyReadonlyArray2<'py, i64>,
                              floors: PyReadonlyArray2<'py, f64>, fmem: PyReadonlyArray1<'py, i64>,
                              fclass: PyReadonlyArray1<'py, i64>)
                              -> PyResult<(Bound<'py, PyArray1<f64>>, Vec<StructRow>, Vec<MetricRow>, Vec<MetricRow>,
                                           Vec<ClearRow>, Vec<ClearRow>)> {
    let (pts, off) = (pts.as_array(), off.as_array().to_vec());
    let (z_in, today) = (z_in.as_array(), today.as_array());
    let (half, level, root, dup, rank, link, under) = (half.as_array(), level.as_array(), root.as_array(),
        dup.as_array(), rank.as_array(), link.as_array(), under.as_array());
    let (edges, floors, fmem, fclass) = (edges.as_array(), floors.as_array(), fmem.as_array(), fclass.as_array());
    let n = off.len().saturating_sub(1);
    let np = pts.nrows();
    let bad = pts.ncols() != 2 || z_in.len() != np || today.len() != np || half.len() != n || level.len() != n
        || root.len() != n || dup.len() != n || rank.len() != n || link.len() != n || under.len() != n
        || off.iter().any(|&v| v < 0 || v as usize > np) || off.windows(2).any(|w| w[1] < w[0])
        || off.first().is_some_and(|&v| v != 0) || off.last().is_some_and(|&v| v as usize != np)
        || (edges.nrows() > 0 && edges.ncols() != 3) || (floors.nrows() > 0 && floors.ncols() != 3)
        || fmem.len() != floors.nrows() || fclass.len() != floors.nrows()
        || root.iter().any(|&r| r < 0 || r as usize >= n) || dup.iter().any(|&r| r < 0 || r as usize >= n)
        || edges.iter().any(|&v| v < 0) || fmem.iter().any(|&v| v < 0);
    if bad {
        return Err(PyValueError::new_err("structure_heights: shapes or offsets do not agree"));
    }
    let mut ls = Vec::with_capacity(n);
    let mut tz = Vec::with_capacity(n);
    for k in 0..n {
        let (a, b) = (off[k] as usize, off[k + 1] as usize);
        let xy: Vec<[f64; 2]> = (a..b).map(|r| [pts[[r, 0]], pts[[r, 1]]]).collect();
        let s = arcs(&xy);
        let z: Vec<f64> = (a..b).map(|r| z_in[r]).collect();
        let finite = z.iter().all(|v| v.is_finite()) && xy.iter().all(|p| p[0].is_finite() && p[1].is_finite());
        let t: Vec<f64> = (a..b).map(|r| today[r]).collect();
        ls.push(Line { xy, s, z, half: half[k], level: level[k], root: root[k] as usize,
                       dup: dup[k] as usize != k || !finite, rank: rank[k], link: link[k] != 0, under: under[k] });
        tz.push(t);
    }
    let es: Vec<(usize, usize, u8)> = (0..edges.nrows()).map(|r| (edges[[r, 0]] as usize, edges[[r, 1]] as usize,
        edges[[r, 2]].clamp(0, 255) as u8)).collect();
    let fl: Vec<(usize, [f64; 2], f64, i64)> = (0..floors.nrows()).filter(|&r| {
        floors[[r, 0]].is_finite() && floors[[r, 1]].is_finite() && floors[[r, 2]].is_finite()
    }).map(|r| (fmem[r] as usize, [floors[[r, 0]], floors[[r, 1]]], floors[[r, 2]], fclass[r])).collect();
    let (out, mt, mj, ct, cj) = py.detach(|| {
        let got = structure_heights_core(&ls, &tz, &es, &fl);
        let (mt, ct) = metrics(&ls, &tz, &got.pairs, &got.pruned, &got.cons);
        let (mj, cj) = metrics(&ls, &got.z, &got.pairs, &got.pruned, &got.cons);
        (got, mt, mj, ct, cj)
    });
    let mut z = Vec::with_capacity(np);
    for (k, l) in ls.iter().enumerate() {
        let o = dup[k] as usize;
        if o != k && !ls[o].dup && l.xy.len() >= 2 && ls[o].len() > 0.0 && l.len() > 0.0 {
            // (a duplicate, one way kept under two keys: its original's joint heights, by the share of its length)
            let r = ls[o].len() / l.len();
            z.extend(l.s.iter().map(|&t| at(&ls[o].s, &out.z[o], t * r)));
        } else if l.dup {
            z.extend_from_slice(&tz[k]);
        } else {
            z.extend_from_slice(&out.z[k]);
        }
    }
    let rows: Vec<StructRow> = out.reports.iter().map(|(r, p)| (*r as i64, p.status, p.pairs as i64,
        p.pruned as i64, p.cwin as i64, p.unmet as i64, p.rounds as i64, p.resid)).collect();
    let m = |v: Vec<(usize, f64, f64, f64, f64)>| v.into_iter().map(|(r, a, b, c, d)| (r as i64, a, b, c, d)).collect();
    let c = |v: Vec<(usize, i64, f64)>| v.into_iter().map(|(r, k, x)| (r as i64, k, x)).collect();
    Ok((z.into_pyarray(py), rows, m(mt), m(mj), c(ct), c(cj)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(pts: &[(f64, f64)], z: &[f64], half: f64, root: usize, rank: i64, link: bool) -> Line {
        let xy: Vec<[f64; 2]> = pts.iter().map(|&(x, y)| [x, y]).collect();
        let s = arcs(&xy);
        Line { xy, s, z: z.to_vec(), half, level: 1.0, root, dup: false, rank, link, under: 8.35 }
    }

    fn straight(x0: f64, x1: f64, y: f64, n: usize) -> Vec<(f64, f64)> {
        (0..=n).map(|i| (x0 + (x1 - x0) * i as f64 / n as f64, y)).collect()
    }

    #[test]
    fn twins_meet_in_the_middle_ends_pinned() {
        // two carriageways 600 m long, 9 m apart, one 0.8 m higher in the middle, abutments level with each other
        let pa = straight(0.0, 600.0, 0.0, 150);
        let pb = straight(0.0, 600.0, 9.0, 150);
        let za: Vec<f64> = pa.iter().map(|p| 10.0 + 0.0 * p.0).collect();
        let zb: Vec<f64> = pb.iter().map(|p| {
            let t = p.0 / 600.0;
            10.0 + 0.8 * (4.0 * t * (1.0 - t))
        }).collect();
        let ls = vec![line(&pa, &za, 4.0, 0, 0, false), line(&pb, &zb, 4.0, 0, 0, false)];
        let today = vec![za.clone(), zb.clone()];
        let got = structure_heights_core(&ls, &today, &[(0, 1, BESIDE)], &[]);
        assert_eq!(got.reports[0].1.status, S_SOLVED);
        let (mt, _) = metrics(&ls, &today, &got.pairs, &got.pruned, &got.cons);
        let (mj, _) = metrics(&ls, &got.z, &got.pairs, &got.pruned, &got.cons);
        assert!(mt[0].1 > 0.7 && mt[0].2 > 0.7);
        // the middle: equal within 2 cm; the ends (pinned) unchanged
        let mid = 75;
        assert!((got.z[0][mid] - got.z[1][mid]).abs() <= 0.02, "{} {}", got.z[0][mid], got.z[1][mid]);
        assert!((got.z[0][mid] - 10.4).abs() < 0.05);
        assert_eq!(got.z[0][0], 10.0);
        assert_eq!(got.z[1][150], zb[150]);
        assert!(mj[0].2 <= 0.02 + 1e-9, "{}", mj[0].2);
    }

    #[test]
    fn a_ramp_bends_to_the_mainline() {
        // a mainline 400 m and a ramp (a link) leaving at its 200 m point, its first 60 m beside it 0.5 m lower
        let pm = straight(0.0, 400.0, 0.0, 100);
        let zm: Vec<f64> = vec![12.0; pm.len()];
        let mut pr = straight(200.0, 260.0, 6.0, 15);
        pr.extend((1..=20).map(|i| (260.0 + 10.0 * i as f64, 6.0 + 4.0 * i as f64)));
        let zr: Vec<f64> = pr.iter().map(|p| if p.0 <= 260.0 { 11.5 } else { 11.5 - 0.01 * (p.0 - 260.0) }).collect();
        let ls = vec![line(&pm, &zm, 5.0, 0, 0, false), line(&pr, &zr, 4.0, 0, 0, true)];
        let today = vec![zm.clone(), zr.clone()];
        let got = structure_heights_core(&ls, &today, &[(0, 1, BESIDE)], &[]);
        assert_eq!(got.reports[0].1.status, S_SOLVED, "{}", got.reports[0].1.status);
        // the mainline moves ~1/11 of the gap, the ramp ~10/11
        let zm_mid = got.z[0][58];
        assert!(zm_mid < 12.0 && zm_mid > 11.9, "{zm_mid}");
        assert!((got.z[1][8] - zm_mid).abs() <= 0.02);
    }

    #[test]
    fn deterministic_and_clearance_held() {
        // an upper deck of level 2 over a lower one of level 1 along it, 3 m above: the clearance window lifts it
        let pl = straight(0.0, 400.0, 0.0, 100);
        let pu = straight(0.0, 400.0, 0.5, 100);
        let zl = vec![10.0; pl.len()];
        let zu: Vec<f64> = vec![19.0; pu.len()];
        let mut lo = line(&pl, &zl, 5.0, 0, 0, false);
        lo.level = 1.0;
        lo.under = 8.0;
        let mut up = line(&pu, &zu, 5.0, 1, 0, false);
        up.level = 2.0;
        // its middle sinks to 3 m over the lower deck
        let mut zu2 = zu.clone();
        for (k, p) in pu.iter().enumerate() {
            if (p.0 - 200.0).abs() < 30.0 {
                zu2[k] = 13.0;
            }
        }
        up.z = zu2.clone();
        let ls = vec![lo, up];
        let today = vec![zl.clone(), zu2.clone()];
        let a = structure_heights_core(&ls, &today, &[], &[]);
        let b = structure_heights_core(&ls, &today, &[], &[]);
        assert_eq!(a.z, b.z);
        let (_, ct) = metrics(&ls, &today, &a.pairs, &a.pruned, &a.cons);
        let (_, cj) = metrics(&ls, &a.z, &a.pairs, &a.pruned, &a.cons);
        let deck = |c: &Vec<(usize, i64, f64)>| c.iter().filter(|r| r.1 == CLASS_DECK).fold(f64::INFINITY, |m, r| m.min(r.2));
        assert!(deck(&ct) < -4.0);
        assert!(deck(&cj) >= -MARGIN, "{} {:?}", deck(&cj), a.reports.iter().map(|r| (r.1.status, r.1.cwin, r.1.unmet, r.1.resid)).collect::<Vec<_>>());
    }

    #[test]
    fn over_a_budget_today_s_heights() {
        // the twins of twins_meet_in_the_middle_ends_pinned under budgets too small for each step: today's heights,
        // S_BUDGET; the default budget solves them
        let pa = straight(0.0, 600.0, 0.0, 150);
        let pb = straight(0.0, 600.0, 9.0, 150);
        let za = vec![10.0; pa.len()];
        let zb: Vec<f64> = pb.iter().map(|p| 10.0 + 0.8 * (4.0 * (p.0 / 600.0) * (1.0 - p.0 / 600.0))).collect();
        let ls = vec![line(&pa, &za, 4.0, 0, 0, false), line(&pb, &zb, 4.0, 0, 0, false)];
        let today = vec![vec![11.0; pa.len()], vec![12.0; pb.len()]];
        let e = [(0, 1, BESIDE)];
        assert_eq!(structure_heights_budget(&ls, &today, &e, &[], &BUDGET).reports[0].1.status, S_SOLVED);
        for b in [Budget { flops: 1000, ..BUDGET }, Budget { factor_bytes: 64, ..BUDGET },
                  Budget { pairs: 10, ..BUDGET }, Budget { samples: 10, ..BUDGET }, Budget { grid: 10, ..BUDGET },
                  Budget { work: 1000, ..BUDGET }] {
            let got = structure_heights_budget(&ls, &today, &e, &[], &b);
            assert_eq!(got.reports.len(), 1);
            assert_eq!(got.reports[0].1.status, S_BUDGET);
            assert_eq!(got.z, today);
        }
    }

    #[test]
    fn a_dense_structure_stops_at_the_couplings() {
        // 80 decks 0.2 m apart, 2 km at 4 m, every pair an edge of one group: 3 160 edges x 501 vertices of
        // candidates, over the sample budget before anything is made
        let n = 80;
        let mut ls = Vec::new();
        let mut today = Vec::new();
        for k in 0..n {
            let p = straight(0.0, 2000.0, 0.2 * k as f64, 500);
            let z = vec![10.0 + 0.01 * k as f64; p.len()];
            today.push(z.clone());
            ls.push(line(&p, &z, 4.0, 0, 0, false));
        }
        let mut e = Vec::new();
        for a in 0..n {
            for b in a + 1..n {
                e.push((a, b, BESIDE));
            }
        }
        let got = structure_heights_core(&ls, &today, &e, &[]);
        assert_eq!(got.reports[0].1.status, S_BUDGET);
        assert_eq!(got.z, today);
    }

    #[test]
    fn a_dense_bucket_runs_out_of_work() {
        // 4 000 decks of other levels stacked in one place, no edges: few samples, but every clearance lookup visits
        // every deck's segments: the work meter stops it (the pins' end search is charged too)
        let mut ls = Vec::new();
        let mut today = Vec::new();
        for k in 0..4000 {
            let p = straight(0.0, 20.0, 0.001 * k as f64, 2);
            let z = vec![10.0 + 0.01 * k as f64; p.len()];
            let mut l = line(&p, &z, 4.0, k, 0, false);
            l.level = k as f64;
            today.push(z);
            ls.push(l);
        }
        let b = Budget { work: 50_000_000, ..BUDGET };
        let got = structure_heights_budget(&ls, &today, &[], &[], &b);
        assert!(got.reports.iter().all(|r| r.1.status == S_BUDGET));
        assert_eq!(got.z, today);
    }

    // pins / settle as they were before their per-end indices (1ced286a): the indexed ones must agree
    fn old_settle(ls: &[Line], pairs: &[Pair]) -> (Vec<[bool; 2]>, Vec<bool>) {
        let mut pinned = old_pins(ls, pairs);
        loop {
            let live: Vec<bool> = pairs.iter().map(|p| {
                let dz = pair_dz(ls, p);
                let need = dz / EASE_GRADE;
                dz <= dz_limit(p.kind) && pin_dist(ls, &pinned, p.a, p.sa) >= need
                    && pin_dist(ls, &pinned, p.b, p.sb) >= need
            }).collect();
            let mut changed = false;
            for m in 0..ls.len() {
                if ls[m].dup || ls[m].xy.len() < 2 {
                    continue;
                }
                for e in 0..2 {
                    if pinned[m][e] {
                        continue;
                    }
                    let t = if e == 0 { 0.0 } else { ls[m].len() };
                    let held = pairs.iter().zip(&live).any(|(p, &l)| {
                        let at_a = p.a == m && p.sa == t && (p.kind == JOIN || p.kind == END_ON);
                        let at_b = p.b == m && p.sb == t && p.kind == JOIN;
                        l && (at_a || at_b)
                    });
                    if !held {
                        pinned[m][e] = true;
                        changed = true;
                    }
                }
            }
            if !changed {
                return (pinned, live);
            }
        }
    }

    fn old_pins(ls: &[Line], pairs: &[Pair]) -> Vec<[bool; 2]> {
        let n = ls.len();
        let mut out = vec![[true, true]; n];
        let ends: Vec<[[f64; 2]; 2]> = ls.iter().map(|l| {
            if l.xy.is_empty() { [[f64::NAN; 2]; 2] } else { [l.xy[0], l.xy[l.xy.len() - 1]] }
        }).collect();
        for m in 0..n {
            if ls[m].dup || ls[m].xy.len() < 2 {
                continue;
            }
            for e in 0..2 {
                let t = if e == 0 { 0.0 } else { ls[m].len() };
                let p = ends[m][e];
                // the members whose ends meet this one
                let mut at_node: Vec<usize> = Vec::new();
                for (k, ek) in ends.iter().enumerate() {
                    if k == m {
                        continue;
                    }
                    for q in ek {
                        let d = ((p[0] - q[0]) * (p[0] - q[0]) + (p[1] - q[1]) * (p[1] - q[1])).sqrt();
                        if d <= JOIN_END_M {
                            at_node.push(k);
                        }
                    }
                }
                at_node.sort_unstable();
                at_node.dedup();
                let joined = |k: usize| pairs.iter().any(|pr| pr.kind == JOIN
                    && ((pr.a == m && pr.sa == t && pr.b == k) || (pr.b == m && pr.sb == t && pr.a == k)));
                let on = pairs.iter().any(|pr| pr.kind == END_ON && pr.a == m && pr.sa == t);
                let free = if at_node.is_empty() { on } else { at_node.iter().all(|&k| joined(k)) };
                out[m][e] = !free;
            }
        }
        out
    }

    #[test]
    fn indexed_pins_and_settle_agree_with_the_scans() {
        // a chain of joints, an end on a deck, twins and a cascade of pins (pairs dropped by their |dz| near ends)
        let mut cases: Vec<(Vec<Line>, Vec<(usize, usize, u8)>)> = Vec::new();
        let pa = straight(0.0, 300.0, 0.0, 75);
        let pb = straight(300.0, 600.0, 0.0, 75);
        let pc = straight(600.0, 640.0, 0.0, 10);
        let pt = straight(0.0, 600.0, 9.0, 150);
        let pr: Vec<(f64, f64)> = (0..=20).map(|i| (150.0, 80.0 - 4.0 * i as f64)).collect();
        for dz in [0.0, 0.4, 1.2, 3.0] {
            let z = |p: &[(f64, f64)], c: f64| -> Vec<f64> { p.iter().map(|q| 10.0 + c * (q.0 / 600.0)).collect() };
            let ls = vec![line(&pa, &z(&pa, 0.0), 4.0, 0, 0, false), line(&pb, &z(&pb, dz), 4.0, 0, 0, false),
                          line(&pc, &z(&pc, 2.0 * dz), 4.0, 0, 1, true), line(&pt, &z(&pt, -dz), 4.0, 0, 0, false),
                          line(&pr, &vec![10.0 + dz; pr.len()], 4.0, 0, 2, true)];
            let e = vec![(0, 1, JOIN), (1, 2, JOIN), (0, 3, BESIDE), (1, 3, BESIDE), (4, 0, END_ON), (2, 3, BESIDE)];
            cases.push((ls, e));
        }
        for (ls, e) in &cases {
            let idx = Index::new(ls);
            let through = through_of(ls);
            let mut m = Meter(u64::MAX);
            let pairs = couplings(ls, e, &idx, &through, true, usize::MAX, &mut m).unwrap();
            assert!(!pairs.is_empty());
            assert_eq!(pins(ls, &pairs, &mut m).unwrap(), old_pins(ls, &pairs));
            assert_eq!(settle(ls, &pairs, &mut m).unwrap(), old_settle(ls, &pairs));
        }
    }

    #[test]
    fn point_at_finds_the_scans_segment() {
        let pts = vec![(0.0, 0.0), (1.0, 0.0), (1.0, 0.0), (3.0, 0.0), (3.0, 2.0)];
        let l = line(&pts, &[0.0; 5], 1.0, 0, 0, false);
        for t in [-1.0, 0.0, 0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 9.0] {
            let n = l.xy.len();
            let mut k = 0usize;
            while k + 2 < n && l.s[k + 1] <= t {
                k += 1;
            }
            let (a, b) = (l.xy[k], l.xy[k + 1]);
            let d = (l.s[k + 1] - l.s[k]).max(1e-12);
            let u = (t - l.s[k]) / d;
            let want = ([a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1])], [(b[0] - a[0]) / d, (b[1] - a[1]) / d]);
            assert_eq!(point_at(&l, t), want, "{t}");
        }
    }

    #[test]
    fn ldl_matches_a_small_system() {
        // [[4,1,0],[1,3,1],[0,1,2]] x = [1,2,3]
        let mut lower = vec![BTreeMap::new(); 3];
        lower[0].insert(0, 4.0);
        lower[1].insert(0, 1.0);
        lower[1].insert(1, 3.0);
        lower[2].insert(1, 1.0);
        lower[2].insert(2, 2.0);
        let x = ldl_solve(3, &lower, &[1.0, 2.0, 3.0]).unwrap();
        let r = [4.0 * x[0] + x[1] - 1.0, x[0] + 3.0 * x[1] + x[2] - 2.0, x[1] + 2.0 * x[2] - 3.0];
        assert!(r.iter().all(|v| v.abs() < 1e-12));
    }
}
