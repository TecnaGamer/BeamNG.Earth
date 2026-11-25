//! M4 (BNE_ROADSYS=surface): bridge decks drawn from the road system's own surface faces.
//!
//! The planar partition (part.rs) of a tile's delivered strokes is built in the tile's own frame; its faces on a
//! STRUCTURE support are the deck: its top (drawn road and collider alike, one face set), the slab under it and the
//! parapets on its free edges. Nothing of the per-way deck chain (slabs, pads, welds, merges, trims) takes part.
//!
//! - Heights: every knot of a structure stroke within EXT_M of its structure takes the height today's chain solve
//!   gives that map feature's deck (`lines`: (feature, points (n, 3)) of the deck builder's parts, the same profile the
//!   terrain and the hand-over were made for), the knot projected on the nearest such line of its own feature within
//!   MATCH_OFF_M; past a line's end, its end grade carries on. A structure interval with a knot near the tile that no
//!   line holds is a recorded fallback (its feature listed: the caller keeps today's deck for it); its faces are left
//!   out.
//! - Faces: the structure faces within KEEP_M of the tile's box (strips and zones; per face its kind, map feature and
//!   the structure's drawn depth).
//! - Free edges: an edge of a kept face used by no other face of the partition, not on a row (a deck end or a cut
//!   joint: never a parapet across the road) and with no drivable face at its height just outside it. Chained into runs
//!   (the face on the left), each widened outward by STRIP_W (the parapet's base, part of the slab's top: K_STRIP
//!   faces) and its outer line returned for the parapet.
//! - Markings (mark.rs) whose triangles lie on a kept face at its height.
//!
//! Pure: IEEE + - * / and sqrt only, integer-mm topology from part.rs, BTreeMap / sorted orders only.
use crate::geom::*;
use crate::io::Place;
use crate::net::{self, Net, Opts, K_ZONE, SUP_STRUCT};
use crate::{mark, part, prof, product};
use std::collections::{BTreeMap, BTreeSet};

pub const K_STRIP: u8 = 9;
/// the filled gap to a twin deck (no parapet; the caller gives it a thin slab: no walls between the twins)
pub const K_MEDIAN: u8 = 10;
/// m a parapet's base reaches past the deck's free edge (roadmesh.PARAPET_W)
pub const STRIP_W: f64 = 0.45;
/// m a knot may lie off its feature's deck line and still take its height
pub const MATCH_OFF_M: f64 = 4.0;
/// m past a structure's ends its stroke's knots take the deck's heights (its end grade carried on past the line's end)
pub const EXT_M: f64 = 12.0;
/// m round the tile's box the faces are kept (the caller clips them to the box)
pub const KEEP_M: f64 = 30.0;
/// m outward from a free edge's middle a drivable face at its height (within COVER_DZ_M) makes it no free edge
pub const COVER_OUT_M: f64 = 0.3;
pub const COVER_DZ_M: f64 = 0.5;
/// m outward from a free edge a structure face at its height makes a twin (the gap filled to its middle, no parapet)
pub const TWIN_M: f64 = 3.0;
const TWIN_STEP_M: f64 = 0.25;
/// (M5) m a twin median's probed half gap is refined to
const MEDIAN_TOL_M: f64 = 0.01;
/// (M5) cos 25 deg: a twin found past TWIN_M runs this near parallel to the free edge
const TWIN_PAR_COS: f64 = 0.9063077870366499;
/// m a twin deck's face may lie above or below the free edge (decks side by side parting in height: no parapet
/// between them until they are a level apart)
pub const TWIN_DZ_M: f64 = 1.5;
/// m at most a strip widens to the map's paved half width
pub const WIDEN_MAX_M: f64 = 8.0;
const CELL_M: f64 = 8.0;
/// m past a ground end the ground it is brought onto is read (roadsys.END_PAST_M)
pub const END_PAST_M: f64 = 0.5;
/// the ground ends' blend: m of mismatch at most (more: a profile matter, left as it is), the vertical curve's
/// curvature limit (1/m: 0.25 g at 70 mph), its length's bounds (m; at most half the structure)
pub const BLEND_MAX_M: f64 = 1.5;
pub const BLEND_A: f64 = 0.0025;
pub const BLEND_MIN_M: f64 = 8.0;
pub const BLEND_LEN_MAX_M: f64 = 45.0;
/// m between a structure stroke's knots (at most)
pub const KNOT_M: f64 = 1.0;

pub struct DeckLine { pub feat: String, pub p: Vec<[f64; 3]> }

pub struct Run { pub p: Vec<[f64; 3]>, pub out: Vec<P2>, pub closed: bool, pub feat: i32 }

#[derive(Default)]
pub struct Decks {
    pub v: Vec<[f64; 3]>,
    pub t: Vec<[u32; 3]>,
    pub kind: Vec<u8>,
    pub feat: Vec<i32>,          // index into feats (-1: none)
    pub depth: Vec<f64>,         // the structure's drawn depth (0: unknown)
    pub feats: Vec<String>,
    pub runs: Vec<Run>,
    pub mv: Vec<[f64; 3]>,
    pub mt: Vec<[u32; 3]>,
    pub mc: Vec<u8>,
    pub fallback: Vec<String>,
    pub json: String,
}

fn bbox_of(p: &[P2]) -> [f64; 4] {
    let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    for q in p { b[0] = b[0].min(q[0]); b[1] = b[1].min(q[1]); b[2] = b[2].max(q[0]); b[3] = b[3].max(q[1]); }
    b
}

fn grow(b: [f64; 4], d: f64) -> [f64; 4] { [b[0] - d, b[1] - d, b[2] + d, b[3] + d] }

fn inside(b: [f64; 4], p: P2) -> bool { p[0] >= b[0] && p[0] <= b[2] && p[1] >= b[1] && p[1] <= b[3] }

/// A deck line's height at q: (lateral offset, height) of the nearest point (past an end: the end grade carried on,
/// up to EXT_M), or None.
fn line_z(l: &DeckLine, q: P2) -> Option<(f64, f64)> {
    let n = l.p.len();
    if n < 2 { return None; }
    let xy = |i: usize| [l.p[i][0], l.p[i][1]];
    let mut best: Option<(f64, f64)> = None;
    for k in 0..n - 1 {
        let (a, b) = (xy(k), xy(k + 1));
        let ab = sub(b, a);
        let l2 = dot(ab, ab);
        if !(l2 > 1e-12) { continue; }
        let u = dot(sub(q, a), ab) / l2;
        let (d, z) = if u < 0.0 && k == 0 || u > 1.0 && k == n - 2 {
            // past an end: along the end's own direction, the end grade over its last ~5 m
            let (e, inner) = if u < 0.0 { (0usize, 1usize) } else { (n - 1, n - 2) };
            let pe = xy(e);
            let mut j = inner;
            let mut run = norm(sub(pe, xy(j)));
            while run < 5.0 && ((e == 0 && j + 1 < n) || (e != 0 && j > 0)) {
                j = if e == 0 { j + 1 } else { j - 1 };
                run = norm(sub(pe, xy(j)));
            }
            let Some(tg) = unit(sub(pe, xy(inner))) else { continue };
            let past = dot(sub(q, pe), tg);
            if past > EXT_M { continue; }
            let lat = absf(cross(tg, sub(q, pe)));
            let g = if run > 1e-6 { (l.p[e][2] - l.p[j][2]) / run } else { 0.0 };
            (lat, l.p[e][2] + g * past)
        } else {
            let u = clampf(u, 0.0, 1.0);
            let m = lerp(a, b, u);
            (norm(sub(q, m)), l.p[k][2] + (l.p[k + 1][2] - l.p[k][2]) * u)
        };
        if best.map(|x| d < x.0).unwrap_or(true) { best = Some((d, z)); }
    }
    best
}

/// A uniform grid over triangles' 2D boxes (cell CELL_M): the triangles that may hold a point.
struct Grid { cells: BTreeMap<(i64, i64), Vec<u32>> }

impl Grid {
    fn new(v: &[[i64; 3]], t: &[[u32; 3]], pick: &[u32]) -> Grid {
        let mut cells: BTreeMap<(i64, i64), Vec<u32>> = BTreeMap::new();
        for &ti in pick {
            let tr = t[ti as usize];
            let xs = [v[tr[0] as usize][0], v[tr[1] as usize][0], v[tr[2] as usize][0]];
            let ys = [v[tr[0] as usize][1], v[tr[1] as usize][1], v[tr[2] as usize][1]];
            let c = |w: i64| ((w as f64 / 1000.0) / CELL_M).floor() as i64;
            let (i0, i1) = (c(*xs.iter().min().unwrap()), c(*xs.iter().max().unwrap()));
            let (j0, j1) = (c(*ys.iter().min().unwrap()), c(*ys.iter().max().unwrap()));
            if (i1 - i0) * (j1 - j0) > 400 { continue; }
            for i in i0..=i1 { for j in j0..=j1 { cells.entry((i, j)).or_default().push(ti); } }
        }
        Grid { cells }
    }
    /// The heights at p of the triangles holding it (in order of the triangles' indices).
    fn heights(&self, v: &[[i64; 3]], t: &[[u32; 3]], p: P2, out: &mut Vec<(u32, f64)>) {
        out.clear();
        let key = ((p[0] / CELL_M).floor() as i64, (p[1] / CELL_M).floor() as i64);
        let Some(list) = self.cells.get(&key) else { return };
        let q = mm2(p);
        for &ti in list {
            let tr = t[ti as usize];
            let a = [v[tr[0] as usize][0], v[tr[0] as usize][1]];
            let b = [v[tr[1] as usize][0], v[tr[1] as usize][1]];
            let c = [v[tr[2] as usize][0], v[tr[2] as usize][1]];
            let o = orient(a, b, c);
            if o == 0 { continue; }
            let (w0, w1, w2) = (orient(b, c, q), orient(c, a, q), orient(a, b, q));
            let ins = if o > 0 { w0 >= 0 && w1 >= 0 && w2 >= 0 } else { w0 <= 0 && w1 <= 0 && w2 <= 0 };
            if !ins { continue; }
            let (f0, f1, f2) = (w0 as f64 / o as f64, w1 as f64 / o as f64, w2 as f64 / o as f64);
            let z = (v[tr[0] as usize][2] as f64 * f0 + v[tr[1] as usize][2] as f64 * f1 + v[tr[2] as usize][2] as f64 * f2) / 1000.0;
            out.push((ti, z));
        }
    }
}

/// Is cluster cl's junction on a structure (every arm on one at its end there)? Else it is the ground's: no deck face,
/// and a structure ending at it ends on the ground.
/// The structure strokes and their boxes grown by their widest mapped half width (widen_out's candidates).
fn struct_boxes(net: &Net) -> Vec<(usize, [f64; 4])> {
    let mut out = Vec::new();
    for (si, st) in net.strokes.iter().enumerate() {
        if !st.sup.iter().any(|u| u.kind == SUP_STRUCT) || st.line.p.is_empty() { continue; }
        let half = st.parts.iter().map(|p| net.segs[net.edges[p.edge].seg].pave * 0.5).fold(0.0f64, f64::max);
        out.push((si, grow(bbox_of(&st.line.p), half + 1.0)));
    }
    out
}

/// m a free edge's strip widens at point q, facing out: to the farthest mapped paved edge (line +- the map's paved
/// half width) of any structure stroke whose mapped band holds q, across it (within 60 deg of its normal), past q
/// outward; at most WIDEN_MAX_M. own: the edge's stroke (its map level), usize::MAX for a zone's edge. (M5: any stroke's, not only the edge's own: a ramp mapped beside the main deck
/// before its nose, its line 1.5 m past the main deck's faces, had the parapet across its mapped lanes, I-95)
fn widen_out(net: &Net, sts: &[(usize, [f64; 4])], q: P2, out: P2, own: usize) -> f64 {
    let lvl = if own == usize::MAX { None } else { let st = &net.strokes[own]; Some(st.support_at(st.line.project(q, 0.0, st.len()).0).level) };
    let mut best = 0.0f64;
    for &(sj, b) in sts {
        if !inside(b, q) { continue; }
        let st = &net.strokes[sj];
        let (s_, off) = st.line.project(q, 0.0, st.len());
        if st.support_at(s_).kind != SUP_STRUCT { continue; }
        let part = st.parts.iter().find(|p| s_ >= p.s0 - 1e-6 && s_ <= p.s1 + 1e-6).unwrap_or(&st.parts[0]);
        let half = net.segs[net.edges[part.edge].seg].pave * 0.5;
        if !(half > 0.0) || absf(off) >= half { continue; }
        // (a stroke running along the edge (within 25 deg) at its own map level: never one crossing it or one over it,
        // whose mapped band filled the gap between two carriageways round a ramp crossing them, I-95)
        if let Some(l) = lvl { if st.support_at(s_).level != l { continue; } }
        let t = st.line.at(s_).1;
        let c = dot(out, [-t[1], t[0]]);
        if absf(c) < TWIN_PAR_COS { continue; }
        let ext = if c > 0.0 { half - off } else { half + off };
        if ext > best { best = ext; }
    }
    best.min(WIDEN_MAX_M)
}

fn zone_struct(net: &Net, lay: &part::Layout, cl: usize) -> bool {
    let arms = &lay.arms[cl];
    !arms.is_empty() && arms.iter().all(|a| {
        let st = &net.strokes[a.stroke];
        let s = if a.start { st.cut0 } else { st.len() - st.cut1 };
        st.support_at(s).kind == SUP_STRUCT
    })
}

/// Station s brought inside structure interval u (its feature's, not a neighbour's at a shared end).
fn inner_s(s: f64, u: &net::Sup) -> f64 {
    let m = ((u.s1 - u.s0) * 0.25).min(0.05);
    clampf(s, u.s0 + m, u.s1 - m)
}

fn feature_at(net: &Net, si: usize, s: f64) -> String {
    let st = &net.strokes[si];
    let part = st.parts.iter().find(|q| s >= q.s0 - 1e-6 && s <= q.s1 + 1e-6).unwrap_or(&st.parts[0]);
    crate::square::feature_of(&net.segs[net.edges[part.edge].seg].fid).to_string()
}

/// The tile's decks from a complete read (product::reads_f with field = true), in the tile's own frame: quad its
/// corners, g_of the ground, lines today's deck heights per map feature.
pub fn build(place: &Place, quad: [[f64; 2]; 4], g_of: &dyn Fn(&[P2]) -> Vec<f64>, lines: &[DeckLine], opts: Opts) -> Decks {
    build_skewed(place, quad, g_of, lines, opts, &[])
}

/// m a skewed end (the hand-over's: RoadsysEnds.skews) may lie off a structure interval's end on its stroke
pub const SKEW_MATCH_M: f64 = 1.5;
/// the least |sin| between an end line and its stroke (a flatter one: square)
const SKEW_MIN_UY: f64 = 0.3;

/// (627) Each structure interval end of the strokes with a skewed end (x, y, dx, dy: its point and end line, the
/// hand-over's skew.rs decision) within SKEW_MATCH_M takes a skewed end row (Stroke.skews), unless its span would reach
/// another event of the stroke (a cut-back, a lane's start or end, another support change, a snap): (set, dropped).
pub fn set_skews(net: &mut Net, skews: &[[f64; 4]]) -> (usize, usize) {
    let (mut set, mut dropped) = (0, 0);
    if skews.is_empty() { return (0, 0); }
    let mut off = Vec::new();
    for si in 0..net.strokes.len() {
        let st = &net.strokes[si];
        if st.absorbed { continue; }
        let mut bounds: Vec<f64> = Vec::new();
        for u in &st.sup {
            if u.kind != SUP_STRUCT { continue; }
            for b in [u.s0, u.s1] { if b > 1e-6 && b < st.len() - 1e-6 { bounds.push(b); } }
        }
        let mut found: Vec<(f64, f64)> = Vec::new();
        for &b in &bounds {
            let (p, t) = st.line.at(b);
            let Some(k) = skews.iter().filter(|q| norm(sub([q[0], q[1]], p)) <= SKEW_MATCH_M)
                .min_by(|x, y| norm(sub([x[0], x[1]], p)).partial_cmp(&norm(sub([y[0], y[1]], p))).unwrap()) else { continue };
            let n = [-t[1], t[0]];
            let d = [k[2], k[3]];
            let (ux, uy) = (dot(d, t), dot(d, n));
            if absf(uy) < SKEW_MIN_UY { dropped += 1; continue; }
            let sk = ux / uy;
            st.offsets(b, &mut off);
            let w = off.iter().fold(0.0f64, |m, &x| m.max(absf(x)));
            let (a0, a1) = (b - absf(sk) * w - part::SKEW_GAP_M, b + absf(sk) * w + part::SKEW_GAP_M);
            let clash = a0 <= st.cut0 || a1 >= st.len() - st.cut1
                || st.sup.iter().any(|u| [u.s0, u.s1].iter().any(|&x| absf(x - b) > 1e-6 && x > a0 && x < a1))
                || st.slots.iter().any(|sl| [sl.alive.0, sl.alive.1].iter().any(|&x| x.is_finite() && x > a0 && x < a1))
                || st.stations.iter().any(|&x| absf(x - b) > 1e-6 && x > a0 && x < a1)
                || (st.snap0.is_some() && a0 < st.cut0 + 20.0) || (st.snap1.is_some() && a1 > st.len() - st.cut1 - 20.0);
            if clash { dropped += 1; continue; }
            found.push((b, sk));
        }
        set += found.len();
        net.strokes[si].skews = found;
    }
    (set, dropped)
}

/// build, the structure ends of `skews` (set_skews) cut along their end lines.
pub fn build_skewed(place: &Place, quad: [[f64; 2]; 4], g_of: &dyn Fn(&[P2]) -> Vec<f64>, lines: &[DeckLine], opts: Opts,
                    skews: &[[f64; 4]]) -> Decks {
    let t0 = crate::cpu_now();
    let order: Vec<usize> = (0..place.segs.len()).collect();
    let opts = Opts { flat_struct: true, ..opts };
    let mut net = net::build_opts(place, &order, opts);
    let lay = part::layout(&mut net);
    let (skews_set, skews_dropped) = set_skews(&mut net, skews);
    let plan = prof::plan(&net, None);
    let bxf = bbox_of(&quad);
    let d = product::delivered(&net, bxf);
    let dp = product::deps_f(&net, &lay, &plan, &d, true);
    let ns = net.strokes.len();
    let need: Vec<bool> = (0..ns).map(|si| dp.full[si] || dp.prof[si]).collect();
    let mut pr = prof::solve(&net, g_of, Some(&need));
    let t1 = crate::cpu_now();
    // today's deck heights on the structure strokes' knots
    let mut ls: Vec<&DeckLine> = lines.iter().filter(|l| l.p.len() >= 2 && l.p.iter().all(|q| q[0].is_finite() && q[1].is_finite() && q[2].is_finite())).collect();
    ls.sort_by(|a, b| a.feat.cmp(&b.feat).then_with(|| {
        let ka: Vec<u64> = a.p.iter().flat_map(|q| [q[0].to_bits(), q[1].to_bits(), q[2].to_bits()]).collect();
        let kb: Vec<u64> = b.p.iter().flat_map(|q| [q[0].to_bits(), q[1].to_bits(), q[2].to_bits()]).collect();
        ka.cmp(&kb)
    }));
    let mut by_feat: BTreeMap<&str, Vec<&DeckLine>> = BTreeMap::new();
    for l in &ls { by_feat.entry(l.feat.as_str()).or_default().push(l); }
    let near = grow(bxf, KEEP_M + EXT_M);
    let mut fixed: Vec<Vec<bool>> = (0..ns).map(|si| vec![false; pr.z[si].len()]).collect();
    let mut knots_fixed = 0usize;
    for si in 0..ns {
        if !need[si] || pr.z[si].is_empty() { continue; }
        let st = &net.strokes[si];
        if !st.sup.iter().any(|u| u.kind == SUP_STRUCT) { continue; }
        // (knots every KNOT_M along a structure stroke: its rows follow today's deck line as drawn, no 5 m chords)
        if pr.h[si] > KNOT_M {
            let n = (st.len() / KNOT_M).ceil().max(1.0) as usize;
            let h = st.len() / n as f64;
            let z: Vec<f64> = (0..=n).map(|j| pr.at(si, j as f64 * h)).collect();
            pr.z[si] = z;
            pr.h[si] = h;
            fixed[si] = vec![false; n + 1];
        }
        for i in 0..pr.z[si].len() {
            let s = (i as f64 * pr.h[si]).min(st.len());
            // (past a structure's end: its own feature's deck, carried on)
            let Some(u) = st.sup.iter().find(|u| u.kind == SUP_STRUCT && s >= u.s0 - EXT_M && s <= u.s1 + EXT_M) else { continue };
            let (p, _t) = st.line.at(s);
            if !inside(near, p) { continue; }
            let f = feature_at(&net, si, inner_s(s, u));
            let Some(cands) = by_feat.get(f.as_str()) else { continue };
            let mut best: Option<(f64, f64)> = None;
            for l in cands {
                if let Some((dd, z)) = line_z(l, p) {
                    if dd <= MATCH_OFF_M && best.map(|b| dd < b.0).unwrap_or(true) { best = Some((dd, z)); }
                }
            }
            if let Some((_dd, z)) = best { pr.z[si][i] = z; fixed[si][i] = true; knots_fixed += 1; }
        }
    }
    // the ground ends: the deck's first metres brought onto the ground just past each end (the physics ground the
    // terrain blocks are made of) over a vertical curve of the comfort limit's length, so the hand-over meets the
    // terrain with no mismatch left to fade over its few metres
    let mut ends: Vec<(usize, f64, f64, f64)> = Vec::new();      // (stroke, end station, direction into the deck, other end)
    let mut qs: Vec<P2> = Vec::new();
    for si in 0..ns {
        if !d[si] || pr.z[si].is_empty() { continue; }
        let st = &net.strokes[si];
        for u in st.sup.iter().filter(|u| u.kind == SUP_STRUCT) {
            for (e, dir, other) in [(u.s0, 1.0, u.s1), (u.s1, -1.0, u.s0)] {
                let out_s = e - dir * END_PAST_M;
                let (p, tg) = st.line.at(e);
                let q = if out_s < st.cut0 || out_s > st.len() - st.cut1 {
                    // (the stroke ends here: at a junction, on the ground unless every arm there is on a structure)
                    let node = if dir > 0.0 { st.n0 } else { st.n1 };
                    let cl = lay.cluster_of.get(node).copied().unwrap_or(usize::MAX);
                    if cl == usize::MAX || zone_struct(&net, &lay, cl) { continue; }
                    sub(p, scale(tg, dir * END_PAST_M))
                } else {
                    if st.support_at(out_s).kind != net::SUP_GROUND { continue; }
                    st.line.at(out_s).0
                };
                if !inside(near, p) { continue; }
                let i = ((e / pr.h[si]).round() as i64).clamp(0, pr.z[si].len() as i64 - 1) as usize;
                if !fixed[si][i] { continue; }
                ends.push((si, e, dir, other));
                qs.push(q);
            }
        }
    }
    let mut blends = 0usize;
    let mut blend_max_mm = 0.0f64;
    if !qs.is_empty() {
        let gz = g_of(&qs);
        for (k, &(si, e, dir, other)) in ends.iter().enumerate() {
            let Some(&g) = gz.get(k) else { continue };
            if !g.is_finite() { continue; }
            let mis = g - pr.at(si, e);
            if !(absf(mis) > 0.0005 && absf(mis) <= BLEND_MAX_M) { continue; }
            let span = absf(other - e);
            let len = clampf((5.7735 * absf(mis) / BLEND_A).sqrt(), BLEND_MIN_M, BLEND_LEN_MAX_M).min(span * 0.5);
            if !(len > 1.0) { continue; }
            let h = pr.h[si];
            for i in 0..pr.z[si].len() {
                let s = i as f64 * h;
                let dd = (s - e) * dir;          // (m into the deck; < 0: the approach)
                if dd < -EXT_M || dd > len { continue; }
                let w = if dd <= 0.0 { 1.0 } else { 1.0 - quintic(dd / len) };
                pr.z[si][i] += mis * w;
            }
            blends += 1;
            blend_max_mm = blend_max_mm.max(absf(mis) * 1000.0);
        }
    }
    // a structure interval near the tile with a knot no deck line holds: a fallback (today's deck for its feature)
    let mut matched: BTreeMap<(usize, u32), bool> = BTreeMap::new();
    let mut fallback: BTreeSet<String> = BTreeSet::new();
    for si in 0..ns {
        if !d[si] || pr.z[si].is_empty() { continue; }
        let st = &net.strokes[si];
        let h = pr.h[si];
        for u in st.sup.iter().filter(|u| u.kind == SUP_STRUCT) {
            let i0 = ((u.s0 / h).floor() as i64).max(0) as usize;
            let i1 = (((u.s1 / h).ceil() as i64).max(0) as usize).min(pr.z[si].len() - 1);
            let mut ok = true;
            for i in i0..=i1 {
                let s = (i as f64 * h).min(st.len());
                if !inside(grow(bxf, KEEP_M + h), st.line.at(s).0) { continue; }
                if !fixed[si][i] { ok = false; fallback.insert(feature_at(&net, si, inner_s(s, u))); break; }
            }
            matched.insert((si, u.sid), ok);
        }
    }
    let t2 = crate::cpu_now();
    let built = part::mesh(&net, &lay, &pr, Some(&d));
    let (marks, _mc) = mark::marks(&net, &built.mesh, &built.rows, &place.style, Some(&d));
    let t3 = crate::cpu_now();
    let m = &built.mesh;
    let mut stroke_of: BTreeMap<u32, usize> = BTreeMap::new();
    for (si, st) in net.strokes.iter().enumerate() { stroke_of.entry(net::piece_of(st.uid)).or_insert(si); }
    let mut zone_of: BTreeMap<u32, usize> = BTreeMap::new();
    for (c, &p) in built.zone_piece.iter().enumerate() { if p != 0 { zone_of.entry(p).or_insert(c); } }
    let keep = grow(bxf, KEEP_M);
    let mut feats: Vec<String> = Vec::new();
    let mut feat_ix: BTreeMap<String, i32> = BTreeMap::new();
    let mut fid = |f: String, feats: &mut Vec<String>| -> i32 {
        if let Some(&i) = feat_ix.get(&f) { return i; }
        let i = feats.len() as i32;
        feats.push(f.clone());
        feat_ix.insert(f, i);
        i
    };
    let pv = |i: u32| { let q = m.v[i as usize]; [q[0] as f64 / 1000.0, q[1] as f64 / 1000.0] };
    // the kept structure faces: (triangle, feature, depth)
    let mut kept: Vec<(u32, i32, f64)> = Vec::new();
    let mut dropped = 0usize;
    for (ti, tr) in m.t.iter().enumerate() {
        let tag = m.tag[ti];
        if tag.sup != SUP_STRUCT { continue; }
        if !tr.iter().any(|&i| inside(keep, pv(i))) { continue; }
        let c = scale(add(add(pv(tr[0]), pv(tr[1])), pv(tr[2])), 1.0 / 3.0);
        let (f, dep, ok) = if tag.kind == K_ZONE {
            let Some(&cl) = zone_of.get(&tag.piece) else { continue };
            let mut best: Option<(u64, String)> = None;
            let (mut dep, mut ok) = (0.0f64, true);
            if !zone_struct(&net, &lay, cl) { continue; }      // (a junction off the structure: the ground's)
            for a in &lay.arms[cl] {
                let st = &net.strokes[a.stroke];
                let s = if a.start { st.cut0 } else { st.len() - st.cut1 };
                let u = st.support_at(s);
                if u.kind != SUP_STRUCT { continue; }
                if !matched.get(&(a.stroke, u.sid)).copied().unwrap_or(false) { ok = false; }
                if u.depth > dep { dep = u.depth; }
                if best.as_ref().map(|b| st.uid < b.0).unwrap_or(true) { best = Some((st.uid, feature_at(&net, a.stroke, s))); }
            }
            (best.map(|b| b.1), dep, ok)
        } else {
            let Some(&si) = stroke_of.get(&tag.piece) else { continue };
            let st = &net.strokes[si];
            let (s, _off) = st.line.project(c, 0.0, st.len());
            let u = st.sup.iter().find(|u| u.kind == SUP_STRUCT && u.sid == tag.sid).copied().unwrap_or_else(|| st.support_at(s));
            (Some(feature_at(&net, si, inner_s(s, &u))), u.depth, matched.get(&(si, tag.sid)).copied().unwrap_or(false))
        };
        if !ok { dropped += 1; if let Some(f) = f { fallback.insert(f); } continue; }
        let fi = match f { Some(f) => fid(f, &mut feats), None => -1 };
        kept.push((ti as u32, fi, dep));
    }
    // a fallback feature's faces all go (its deck stays today's whole)
    if !fallback.is_empty() {
        let fb: BTreeSet<i32> = feats.iter().enumerate().filter(|(_, f)| fallback.contains(*f)).map(|(i, _)| i as i32).collect();
        let n0 = kept.len();
        kept.retain(|k| !fb.contains(&k.1));
        dropped += n0 - kept.len();
    }
    // edges: uses over the whole partition, the rows' own edges
    let ekey = |a: u32, b: u32| if a < b { (a, b) } else { (b, a) };
    let mut uses: BTreeMap<(u32, u32), u32> = BTreeMap::new();
    for tr in &m.t {
        for k in 0..3 {
            let (a, b) = (tr[k], tr[(k + 1) % 3]);
            if a != b { *uses.entry(ekey(a, b)).or_insert(0) += 1; }
        }
    }
    let mut row_edges: BTreeSet<(u32, u32)> = BTreeSet::new();
    for rs in &built.rows { for r in rs { for w in r.b.windows(2) { if w[0] != w[1] { row_edges.insert(ekey(w[0], w[1])); } } } }
    let all: Vec<u32> = (0..m.t.len() as u32).collect();
    let grid = Grid::new(&m.v, &m.t, &all);
    let mut hs: Vec<(u32, f64)> = Vec::new();
    // free directed edges (the face on their left): start -> [(end, feature, depth, twin gap)]; a kept face at its
    // height across a gap of at most TWIN_M outward: a twin deck, the gap filled to its middle, no parapet there
    let kept_ids: Vec<u32> = kept.iter().map(|k| k.0).collect();
    let kgrid = Grid::new(&m.v, &m.t, &kept_ids);
    let dbg_free = std::env::var("ROADSYS_DECK_FREE_DEBUG").is_ok();
    let sboxes = struct_boxes(&net);
    type FreeE = (u32, i32, f64, f64, usize);
    let mut free: BTreeMap<u32, Vec<FreeE>> = BTreeMap::new();
    let mut incoming: BTreeMap<u32, usize> = BTreeMap::new();
    for &(ti, fi, dep) in &kept {
        let tr = m.t[ti as usize];
        let i2 = |i: u32| { let q = m.v[i as usize]; [q[0], q[1]] };
        let ccw = orient(i2(tr[0]), i2(tr[1]), i2(tr[2])) > 0;
        let tg = m.tag[ti as usize];
        let si_of = if tg.kind == K_ZONE { usize::MAX } else { stroke_of.get(&tg.piece).copied().unwrap_or(usize::MAX) };
        for k in 0..3 {
            let (mut a, mut b) = (tr[k], tr[(k + 1) % 3]);
            if a == b { continue; }
            if !ccw { std::mem::swap(&mut a, &mut b); }
            let e = ekey(a, b);
            if uses.get(&e).copied().unwrap_or(0) != 1 || row_edges.contains(&e) { continue; }
            let (pa, pb) = (pv(a), pv(b));
            let Some(dir) = unit(sub(pb, pa)) else { continue };
            let out = [dir[1], -dir[0]];
            let mid = scale(add(pa, pb), 0.5);
            let zm = (m.v[a as usize][2] + m.v[b as usize][2]) as f64 / 2000.0;
            grid.heights(&m.v, &m.t, add(mid, scale(out, COVER_OUT_M)), &mut hs);
            if hs.iter().any(|&(_t, z)| absf(z - zm) <= COVER_DZ_M) { continue; }
            // (M5: a twin is a kept face across a gap of at most TWIN_M once both decks are widened to their map's
            // paved half widths (the strips below): two 14 m carriageways 12 m apart are twins, the gap filled, not
            // two parapets each 2.5 m into the other's mapped lanes)
            let r_self = widen_out(&net, &sboxes, mid, out, si_of);
            let mut gap = 0.0;
            let mut dd = COVER_OUT_M + TWIN_STEP_M;
            while dd <= TWIN_M + r_self + WIDEN_MAX_M + 1e-9 {
                kgrid.heights(&m.v, &m.t, add(mid, scale(out, dd)), &mut hs);
                let q = add(mid, scale(out, dd));
                let hit = hs.iter().any(|&(t, z)| {
                    if absf(z - zm) > TWIN_DZ_M { return false; }
                    if dd <= TWIN_M + 1e-9 { return true; }
                    let tg2 = m.tag[t as usize];
                    let sj = if tg2.kind == K_ZONE { usize::MAX } else { stroke_of.get(&tg2.piece).copied().unwrap_or(usize::MAX) };
                    // (past TWIN_M: a parallel deck only (within 25 deg), never a crossing one: a ramp crossing two
                    // twin carriageways at their height filled the gap round it, I-95)
                    if sj == usize::MAX || sj == si_of { return false; }
                    let st2 = &net.strokes[sj];
                    let (s2, _o) = st2.line.project(q, 0.0, st2.len());
                    absf(dot(dir, st2.line.at(s2).1)) >= TWIN_PAR_COS
                        && dd <= TWIN_M + r_self + widen_out(&net, &sboxes, q, [-out[0], -out[1]], sj) + 1e-9
                });
                if hit { gap = dd; break; }
                dd += TWIN_STEP_M;
            }
            if dbg_free {
                let mut hs2: Vec<(u32, f64)> = Vec::new();
                let mut seen: Vec<String> = Vec::new();
                let mut d2 = COVER_OUT_M;
                while d2 <= 12.0 { grid.heights(&m.v, &m.t, add(mid, scale(out, d2)), &mut hs2); for &(t, z) in &hs2 { seen.push(format!("{:.2}:{}:{}:{:.2}", d2, m.tag[t as usize].sup, kept_ids.binary_search(&t).is_ok(), z - zm)); } d2 += 0.5; }
                eprintln!("FREE {:.1} {:.1} zm {:.2} rself {:.2} gap {:.2} si {} seen {}", mid[0], mid[1], zm, r_self, gap, si_of, seen.join(" "));
            }
            free.entry(a).or_default().push((b, fi, dep, gap, si_of));
            *incoming.entry(b).or_insert(0) += 1;
        }
    }
    for v in free.values_mut() { v.sort_by(|x, y| x.0.cmp(&y.0)); }
    // chains: open ones from their starts first, then the loops; per edge its twin gap (0: none)
    let mut chains: Vec<(Vec<u32>, Vec<f64>, Vec<usize>, bool, i32, f64)> = Vec::new();
    let starts: Vec<u32> = free.keys().copied().filter(|v| incoming.get(v).copied().unwrap_or(0) == 0).collect();
    let take = |v: u32, free: &mut BTreeMap<u32, Vec<FreeE>>| -> Option<FreeE> {
        let list = free.get_mut(&v)?;
        if list.is_empty() { return None; }
        let e = list.remove(0);
        if list.is_empty() { free.remove(&v); }
        Some(e)
    };
    for pass in 0..2 {
        loop {
            let st = if pass == 0 {
                match starts.iter().copied().find(|v| free.contains_key(v)) { Some(v) => v, None => break }
            } else {
                match free.keys().next().copied() { Some(v) => v, None => break }
            };
            let mut ch = vec![st];
            let mut gaps = Vec::new();
            let mut sis = Vec::new();
            let (mut fi, mut dep) = (-1, 0.0f64);
            let mut at = st;
            while let Some((b, f, dd, gp, sj)) = take(at, &mut free) {
                if fi < 0 { fi = f; }
                if dd > dep { dep = dd; }
                ch.push(b);
                gaps.push(gp);
                sis.push(sj);
                at = b;
                if b == st { break; }
            }
            let closed = ch.len() > 2 && ch[0] == *ch.last().unwrap();
            if closed { ch.pop(); }
            if ch.len() >= 2 { chains.push((ch, gaps, sis, closed, fi, dep)); }
        }
    }
    // output: the kept faces (their vertices remapped), the strips and runs
    let mut out = Decks { feats, ..Default::default() };
    let mut remap: BTreeMap<u32, u32> = BTreeMap::new();
    let mut vid = |i: u32, out: &mut Decks| -> u32 {
        if let Some(&j) = remap.get(&i) { return j; }
        let q = m.v[i as usize];
        let j = out.v.len() as u32;
        out.v.push([q[0] as f64 / 1000.0, q[1] as f64 / 1000.0, q[2] as f64 / 1000.0]);
        remap.insert(i, j);
        j
    };
    for &(ti, fi, dep) in &kept {
        let tr = m.t[ti as usize];
        let t = [vid(tr[0], &mut out), vid(tr[1], &mut out), vid(tr[2], &mut out)];
        out.t.push(t);
        out.kind.push(m.tag[ti as usize].kind);
        out.feat.push(fi);
        out.depth.push(dep);
    }
    let mut strip_tris = 0usize;
    let mut twin_m = 0.0f64;
    for (ch, gaps, _sis, closed, fi, dep) in &chains {
        let k = ch.len();
        let p: Vec<P2> = ch.iter().map(|&i| pv(i)).collect();
        let nseg = if *closed { k } else { k - 1 };
        let mut rn: Vec<P2> = Vec::with_capacity(nseg);
        for s in 0..nseg {
            let d = unit(sub(p[(s + 1) % k], p[s])).unwrap_or([1.0, 0.0]);
            rn.push([d[1], -d[0]]);
        }
        // (a strip's width per edge: half the gap to a twin deck; else STRIP_W past the map's paved half width from its
        // line where the faces end short of it (a carriageway's lanes on its line, its shoulders apart): the slab and
        // parapet as wide as the map's road, as today's decks)
        let reach = |i: usize, e: usize| -> f64 { widen_out(&net, &sboxes, p[i], rn[e], _sis[e]) };
        // (M5: a widened strip stops short of any kept face it would reach (another deck at its height, a zone): one
        // owner per drivable point, never a strip lying on a neighbour's face)
        let clear = |e: usize, w: f64| -> f64 {
            let (a, b) = (p[e], p[(e + 1) % k]);
            let mid = scale(add(a, b), 0.5);
            let zm = (m.v[ch[e] as usize][2] + m.v[ch[(e + 1) % k] as usize][2]) as f64 / 2000.0;
            let mut hs2: Vec<(u32, f64)> = Vec::new();
            let mut d = COVER_OUT_M + TWIN_STEP_M;
            while d <= w + 1e-9 {
                kgrid.heights(&m.v, &m.t, add(mid, scale(rn[e], d)), &mut hs2);
                // (to the middle of the gap: the deck across widens or fills its half the same way, never over this one's)
                if hs2.iter().any(|&(_t, z)| absf(z - zm) <= COVER_DZ_M) { return (d * 0.5).max(STRIP_W.min(w)); }
                d += TWIN_STEP_M;
            }
            w
        };
        let ew: Vec<f64> = (0..nseg).map(|e| if gaps[e] > 0.0 { gaps[e] * 0.5 } else {
            let w = STRIP_W + reach(e, e).max(reach((e + 1) % k, e));
            if w > STRIP_W { clear(e, w) } else { w }
        }).collect();
        let mut nrm: Vec<P2> = Vec::with_capacity(k);
        let mut wv: Vec<f64> = Vec::with_capacity(k);
        for i in 0..k {
            let (prev, next) = if *closed { (Some((i + nseg - 1) % nseg), Some(i % nseg)) } else {
                (if i > 0 { Some(i - 1) } else { None }, if i < nseg { Some(i) } else { None })
            };
            let (n, c, w) = match (prev, next) {
                (Some(a), Some(b)) => { let n = unit(add(rn[a], rn[b])).unwrap_or(rn[b]); (n, dot(n, rn[b]).max(0.5), ew[a].min(ew[b])) }
                (Some(a), None) => (rn[a], 1.0, ew[a]),
                (None, Some(b)) => (rn[b], 1.0, ew[b]),
                (None, None) => ([0.0, 0.0], 1.0, STRIP_W),
            };
            nrm.push(n);
            wv.push(w / c);
        }
        // (M5: a twin's median fill to the middle of the gap measured at each vertex along its own normal, refined to
        // MEDIAN_TOL_M (the per-edge gaps and the miter left the two halves overlapping by decimetres where the twins
        // are not quite parallel: 46 m2 of coplanar double owners at 973d))
        for i in 0..k {
            let (prev, next) = if *closed { (Some((i + nseg - 1) % nseg), Some(i % nseg)) } else {
                (if i > 0 { Some(i - 1) } else { None }, if i < nseg { Some(i) } else { None })
            };
            let med = prev.map(|a| gaps[a] > 0.0).unwrap_or(false) || next.map(|b| gaps[b] > 0.0).unwrap_or(false);
            if !med { continue; }
            let gmax = prev.map(|a| gaps[a]).unwrap_or(0.0).max(next.map(|b| gaps[b]).unwrap_or(0.0));
            let zi = m.v[ch[i] as usize][2] as f64 / 1000.0;
            let mut hs3: Vec<(u32, f64)> = Vec::new();
            let hit = |d: f64, hs3: &mut Vec<(u32, f64)>| -> bool {
                kgrid.heights(&m.v, &m.t, add(p[i], scale(nrm[i], d)), hs3);
                hs3.iter().any(|&(_t, z)| absf(z - zi) <= TWIN_DZ_M)
            };
            let (mut lo, mut hi) = (COVER_OUT_M, f64::NAN);
            let mut d = COVER_OUT_M + TWIN_STEP_M;
            while d <= gmax * 1.1 + TWIN_STEP_M {
                if hit(d, &mut hs3) { hi = d; break; }
                lo = d;
                d += TWIN_STEP_M;
            }
            if !hi.is_finite() { continue; }
            while hi - lo > MEDIAN_TOL_M {
                let mid = 0.5 * (lo + hi);
                if hit(mid, &mut hs3) { hi = mid; } else { lo = mid; }
            }
            wv[i] = wv[i].min(0.5 * (lo + hi) * 0.5);
        }
        let inner: Vec<u32> = ch.iter().map(|&i| vid(i, &mut out)).collect();
        let mut outer: Vec<u32> = Vec::with_capacity(k);
        let mut pts3: Vec<[f64; 3]> = Vec::with_capacity(k);
        for i in 0..k {
            let z = out.v[inner[i] as usize][2];
            let q = add(p[i], scale(nrm[i], wv[i]));
            let q = [mm(q[0]) as f64 / 1000.0, mm(q[1]) as f64 / 1000.0];
            outer.push(out.v.len() as u32);
            out.v.push([q[0], q[1], z]);
            pts3.push([q[0], q[1], z]);
        }
        for s in 0..nseg {
            let (a, b) = (s, (s + 1) % k);
            for t in [[inner[a], outer[a], outer[b]], [inner[a], outer[b], inner[b]]] {
                out.t.push(t);
                out.kind.push(if gaps[s] > 0.0 { K_MEDIAN } else { K_STRIP });
                out.feat.push(*fi);
                out.depth.push(*dep);
                strip_tris += 1;
            }
            if gaps[s] > 0.0 { twin_m += norm(sub(p[b], p[a])); }
        }
        // the parapet runs: the chain's edges with no twin beside them
        let mut s = 0;
        while s < nseg {
            if gaps[s] > 0.0 { s += 1; continue; }
            let s0 = s;
            while s < nseg && gaps[s] == 0.0 { s += 1; }
            let whole = *closed && s0 == 0 && s == nseg;
            let idx: Vec<usize> = if whole { (0..k).collect() } else { (s0..=s).map(|i| i % k).collect() };
            out.runs.push(Run { p: idx.iter().map(|&i| pts3[i]).collect(), out: idx.iter().map(|&i| nrm[i]).collect(), closed: whole, feat: *fi });
        }
    }
    // markings on the kept faces
    let mut mremap: BTreeMap<u32, u32> = BTreeMap::new();
    for (ti, tr) in marks.t.iter().enumerate() {
        let c = [(marks.v[tr[0] as usize][0] + marks.v[tr[1] as usize][0] + marks.v[tr[2] as usize][0]) as f64 / 3000.0,
                 (marks.v[tr[0] as usize][1] + marks.v[tr[1] as usize][1] + marks.v[tr[2] as usize][1]) as f64 / 3000.0];
        let zc = (marks.v[tr[0] as usize][2] + marks.v[tr[1] as usize][2] + marks.v[tr[2] as usize][2]) as f64 / 3000.0;
        kgrid.heights(&m.v, &m.t, c, &mut hs);
        if !hs.iter().any(|&(_t, z)| absf(z - zc) <= 0.25) { continue; }
        let mut t = [0u32; 3];
        for k in 0..3 {
            let i = tr[k];
            t[k] = *mremap.entry(i).or_insert_with(|| {
                let q = marks.v[i as usize];
                out.mv.push([q[0] as f64 / 1000.0, q[1] as f64 / 1000.0, q[2] as f64 / 1000.0]);
                (out.mv.len() - 1) as u32
            });
        }
        out.mt.push(t);
        out.mc.push(marks.colour[ti]);
    }
    out.fallback = fallback.into_iter().collect();
    let t4 = crate::cpu_now();
    out.json = format!(
        "{{\"strokes\":{},\"skews\":{},\"skews_dropped\":{},\"rail_floors\":{},\"delivered\":{},\"knots_fixed\":{},\"blends\":{},\"blend_max_mm\":{:.1},\"faces\":{},\"dropped\":{},\"fallback\":{},\"runs\":{},\"twin_m\":{:.1},\"strip_tris\":{},\"marks\":{},\"cpu_ms\":{{\"profile\":{:.3},\"heights\":{:.3},\"surface\":{:.3},\"decks\":{:.3},\"total\":{:.3}}}}}",
        ns, skews_set, skews_dropped, net.rail_floors.len(), d.iter().filter(|&&x| x).count(), knots_fixed, blends, blend_max_mm, kept.len(), dropped, out.fallback.len(), out.runs.len(), twin_m, strip_tris, out.mt.len(),
        (t1 - t0) * 1e3, (t2 - t1) * 1e3, (t3 - t2) * 1e3, (t4 - t3) * 1e3, (t4 - t0) * 1e3);
    out
}

/// The decks' whole input as one file (the CLI's `roadsys decks IN` builds it again, on Linux and on Windows, for the
/// byte comparison): the place, quad and cut (io::write_tile, its ground the heights asked, in order) and the lines.
pub fn encode_input(place: &Place, quad: [[f64; 2]; 4], cut: (f64, f64), ground: &[f64], lines: &[DeckLine], areas_on: bool) -> Vec<u8> {
    encode_input_skewed(place, quad, cut, ground, lines, areas_on, &[])
}

/// encode_input with the skewed ends ("RSDECK02" when there are any, after the lines: their count and x y dx dy).
pub fn encode_input_skewed(place: &Place, quad: [[f64; 2]; 4], cut: (f64, f64), ground: &[f64], lines: &[DeckLine], areas_on: bool,
                           skews: &[[f64; 4]]) -> Vec<u8> {
    let t = crate::io::TileInput { place: place.clone(), quad, id: (0, 0), frame: 0, cut, ground: ground.to_vec(), areas_on };
    let tb = crate::io::write_tile(&t);
    let mut b = if skews.is_empty() { b"RSDECK01".to_vec() } else { b"RSDECK02".to_vec() };
    b.extend_from_slice(&(tb.len() as u64).to_le_bytes());
    b.extend_from_slice(&tb);
    b.extend_from_slice(&(lines.len() as u64).to_le_bytes());
    for l in lines {
        b.extend_from_slice(&(l.feat.len() as u64).to_le_bytes());
        b.extend_from_slice(l.feat.as_bytes());
        b.extend_from_slice(&(l.p.len() as u64).to_le_bytes());
        for q in &l.p { for v in q { b.extend_from_slice(&v.to_bits().to_le_bytes()); } }
    }
    if !skews.is_empty() {
        b.extend_from_slice(&(skews.len() as u64).to_le_bytes());
        for k in skews { for v in k { b.extend_from_slice(&v.to_bits().to_le_bytes()); } }
    }
    b
}

/// encode_input's file built again: (the output's bytes, the decks).
pub fn run_input(b: &[u8]) -> Result<(Vec<u8>, Decks), String> {
    let bad = || "not a roadsys decks input".to_string();
    let u64_at = |i: usize| -> Result<u64, String> { Ok(u64::from_le_bytes(b.get(i..i + 8).ok_or_else(bad)?.try_into().unwrap())) };
    if b.len() < 16 || (&b[..8] != b"RSDECK01" && &b[..8] != b"RSDECK02") { return Err(bad()); }
    let with_skews = &b[..8] == b"RSDECK02";
    let n = u64_at(8)? as usize;
    let t = crate::io::read_tile(b.get(16..16 + n).ok_or_else(bad)?)?;
    let mut i = 16 + n;
    let nl = u64_at(i)? as usize;
    i += 8;
    let mut lines = Vec::with_capacity(nl);
    for _ in 0..nl {
        let k = u64_at(i)? as usize;
        i += 8;
        let feat = String::from_utf8(b.get(i..i + k).ok_or_else(bad)?.to_vec()).map_err(|_| bad())?;
        i += k;
        let np = u64_at(i)? as usize;
        i += 8;
        let mut p = Vec::with_capacity(np);
        for _ in 0..np {
            let mut q = [0.0; 3];
            for c in 0..3 { q[c] = f64::from_bits(u64_at(i)?); i += 8; }
            p.push(q);
        }
        lines.push(DeckLine { feat, p });
    }
    let mut skews: Vec<[f64; 4]> = Vec::new();
    if with_skews {
        let ns = u64_at(i)? as usize;
        i += 8;
        for _ in 0..ns.min(1 << 20) {
            let mut q = [0.0; 4];
            for c in 0..4 { q[c] = f64::from_bits(u64_at(i)?); i += 8; }
            skews.push(q);
        }
    }
    let at = std::cell::Cell::new(0usize);
    let g = |pts: &[P2]| -> Vec<f64> {
        let k = at.get();
        let v: Vec<f64> = (0..pts.len()).map(|j| t.ground.get(k + j).copied().unwrap_or(f64::NAN)).collect();
        at.set(k + pts.len());
        v
    };
    let o = build_skewed(&t.place, t.quad, &g, &lines, Opts { cut_grid: t.cut.0, cut_offset: t.cut.1, areas: t.areas_on, ..Default::default() }, &skews);
    Ok((output_bytes(&o), o))
}

/// The decks' output as bytes (everything but the timings).
pub fn output_bytes(o: &Decks) -> Vec<u8> {
    let mut b = Vec::new();
    let mut f = |v: f64| b.extend_from_slice(&v.to_bits().to_le_bytes());
    for q in &o.v { for &v in q { f(v); } }
    for q in &o.mv { for &v in q { f(v); } }
    for &dd in &o.depth { f(dd); }
    for r in &o.runs { for q in &r.p { for &v in q { f(v); } } for q in &r.out { f(q[0]); f(q[1]); } f(if r.closed { 1.0 } else { 0.0 }); f(r.feat as f64); }
    for t in o.t.iter().chain(o.mt.iter()) { for &v in t { b.extend_from_slice(&v.to_le_bytes()); } }
    b.extend_from_slice(&o.kind);
    b.extend_from_slice(&o.mc);
    for &v in &o.feat { b.extend_from_slice(&v.to_le_bytes()); }
    for st in o.feats.iter().chain(o.fallback.iter()) { b.extend_from_slice(st.as_bytes()); b.push(0); }
    b
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::io::Seg;

    fn seg(fid: &str, pts: &[[f64; 2]], cons: &[(&str, usize)], bridge: Option<(f64, f64)>) -> Seg {
        Seg { fid: fid.into(), code: "US".into(), cls_s: "secondary".into(), pave: 7.0, drive: 7.0, inferred: 2, cls: 4, dir: 0,
              lanes: 2, lfw: -1, lbw: -1, kmh: 60.0, lane_w: 3.5, pts: pts.to_vec(), cons: cons.iter().map(|(c, v)| (c.to_string(), *v)).collect(),
              bridge: bridge.into_iter().collect(), level: bridge.map(|b| vec![(b.0, b.1, 1.0)]).unwrap_or_default(), ..Default::default() }
    }

    fn place(rev: bool) -> Place {
        let mut segs = vec![
            seg("a", &[[-400.0, 0.0], [-40.0, 0.0]], &[("n1", 1)], None),
            seg("b", &[[-40.0, 0.0], [40.0, 0.0]], &[("n1", 0), ("n2", 1)], Some((0.0, 1.0))),
            seg("c", &[[40.0, 0.0], [400.0, 0.0]], &[("n2", 0)], None),
            seg("x", &[[0.0, -300.0], [0.0, 300.0]], &[], None),
        ];
        if rev { segs.reverse(); }
        Place { segs, ..Default::default() }
    }

    fn g(p: &[P2]) -> Vec<f64> { p.iter().map(|q| 10.0 + 0.001 * q[0]).collect() }

    const Q: [[f64; 2]; 4] = [[-128.0, -128.0], [128.0, -128.0], [128.0, 128.0], [-128.0, 128.0]];

    fn lines() -> Vec<DeckLine> {
        vec![DeckLine { feat: "b".into(), p: vec![[-40.0, 0.0, 10.0], [0.0, 0.0, 16.0], [40.0, 0.0, 10.0]] }]
    }

    #[test]
    fn decks_take_todays_heights_and_parapets_run_along_both_edges() {
        let o = build(&place(false), Q, &g, &lines(), Opts::default());
        assert!(o.fallback.is_empty(), "{:?}", o.fallback);
        assert!(o.t.iter().zip(o.kind.iter()).filter(|(_, &k)| k < K_STRIP).count() > 10);
        // the deck's middle at today's height (flat across: no crossfall on a structure here)
        let mid = o.t.iter().zip(o.kind.iter()).filter(|(_, &k)| k < K_STRIP).flat_map(|(t, _)| t.iter().map(|&i| o.v[i as usize]))
            .filter(|p| absf(p[0]) < 0.5).collect::<Vec<_>>();
        assert!(!mid.is_empty());
        for p in &mid { assert!(absf(p[2] - 16.0) < 0.05, "{:?}", p); }
        // two parapet runs (the two long edges), none across the ends
        assert_eq!(o.runs.len(), 2, "{}", o.json);
        for r in &o.runs {
            let len: f64 = r.p.windows(2).map(|w| norm(sub([w[1][0], w[1][1]], [w[0][0], w[0][1]]))).sum();
            assert!(len > 70.0 && len < 90.0, "{len}");
            assert!(r.p.iter().all(|q| absf(q[1]) > 3.0));
        }
    }

    /// (627) A skewed end (the hand-over's decision at x = -40, its end line at 60 degrees): the deck's end edge runs
    /// along it, the top's corners at -40 -/+ (half width) * cot 60 deg; the square end stays square; the input file
    /// (RSDECK02) builds it again.
    #[test]
    fn a_skewed_end_row_follows_its_end_line() {
        let sk = [[-40.0, 0.0, 0.5, 0.8660254037844386]];
        let o = build_skewed(&place(false), Q, &g, &lines(), Opts::default(), &sk);
        assert!(o.json.contains("\"skews\":1"), "{}", o.json);
        let tops: Vec<[f64; 3]> = o.t.iter().zip(o.kind.iter()).filter(|(_, &k)| k < K_STRIP)
            .flat_map(|(t, _)| t.iter().map(|&i| o.v[i as usize])).collect();
        let xmin_at = |y: f64| tops.iter().filter(|p| absf(p[1] - y) < 0.01).map(|p| p[0]).fold(f64::INFINITY, f64::min);
        let (yl, yr) = (tops.iter().map(|p| p[1]).fold(f64::NEG_INFINITY, f64::max), tops.iter().map(|p| p[1]).fold(f64::INFINITY, f64::min));
        let k = 0.5 / 0.8660254037844386;
        assert!(absf(xmin_at(yl) - (-40.0 + yl * k)) < 0.02, "{} {}", xmin_at(yl), -40.0 + yl * k);
        assert!(absf(xmin_at(yr) - (-40.0 + yr * k)) < 0.02, "{} {}", xmin_at(yr), -40.0 + yr * k);
        let xmax_at = |y: f64| tops.iter().filter(|p| absf(p[1] - y) < 0.01).map(|p| p[0]).fold(f64::NEG_INFINITY, f64::max);
        assert!(absf(xmax_at(yl) - xmax_at(yr)) < 0.02);
        let file = encode_input_skewed(&place(false), Q, (4000.0, 2000.0), &[], &lines(), false, &sk);
        assert_eq!(&file[..8], b"RSDECK02");
    }

    #[test]
    fn a_ground_end_meets_the_ground_just_past_it() {
        let ls = vec![DeckLine { feat: "b".into(), p: vec![[-40.0, 0.0, 10.3], [0.0, 0.0, 16.0], [40.0, 0.0, 10.3]] }];
        let o = build(&place(false), Q, &g, &ls, Opts::default());
        assert!(o.json.contains("\"blends\":2"), "{}", o.json);
        let tops: Vec<[f64; 3]> = o.t.iter().zip(o.kind.iter()).filter(|(_, &k)| k < K_STRIP)
            .flat_map(|(t, _)| t.iter().map(|&i| o.v[i as usize])).collect();
        let near = |x: f64| tops.iter().filter(|p| absf(p[0] - x) < 0.05 && absf(p[1]) < 0.5).map(|p| p[2]).collect::<Vec<_>>();
        for (x, want) in [(-40.0, 10.0 + 0.001 * -40.5), (40.0, 10.0 + 0.001 * 40.5)] {
            let zs = near(x);
            assert!(!zs.is_empty());
            for z in zs { assert!(absf(z - want) < 0.01, "{x}: {z} vs {want}"); }
        }
        for z in near(0.0) { assert!(absf(z - 16.0) < 0.01, "{z}"); }
    }

    #[test]
    fn the_input_file_builds_the_same_decks() {
        let p = place(false);
        let asked = std::cell::RefCell::new(Vec::new());
        let gr = |pts: &[P2]| { let v = g(pts); asked.borrow_mut().extend_from_slice(&v); v };
        let a = build(&p, Q, &gr, &lines(), Opts::default());
        let file = encode_input(&p, Q, (4000.0, 2000.0), &asked.borrow(), &lines(), false);
        let (bytes, b) = run_input(&file).unwrap();
        assert_eq!(output_bytes(&a), bytes);
        assert_eq!(a.t.len(), b.t.len());
    }

    #[test]
    fn twin_decks_share_a_filled_median_and_no_parapet_between_them() {
        let mut segs = Vec::new();
        for (f, y, dir) in [("p", 4.2, 1i64), ("q", -4.2, 1i64)] {
            let xs: [f64; 2] = if f == "p" { [-400.0, 400.0] } else { [400.0, -400.0] };
            let mut sg = seg(f, &[[xs[0], y], [xs[1], y]], &[], Some((0.45, 0.55)));
            sg.dir = dir;
            segs.push(sg);
        }
        let pl = Place { segs, ..Default::default() };
        let ls = vec![DeckLine { feat: "p".into(), p: vec![[-40.0, 4.2, 12.0], [40.0, 4.2, 12.0]] },
                      DeckLine { feat: "q".into(), p: vec![[40.0, -4.2, 12.0], [-40.0, -4.2, 12.0]] }];
        let o = build(&pl, Q, &g, &ls, Opts::default());
        assert!(o.fallback.is_empty(), "{:?}", o.fallback);
        assert!(o.json.contains("twin_m"), "{}", o.json);
        // two runs: the decks' outer edges; none in the median
        assert_eq!(o.runs.len(), 2, "{}", o.json);
        for r in &o.runs { assert!(r.p.iter().all(|q| absf(q[1]) > 5.0), "{:?}", &r.p[..2]); }
    }

    /// (M5) twins whose own faces are more than TWIN_M apart but whose mapped carriageways (14 m each, lines 12 m apart)
    /// meet: one filled median, no parapet in either's mapped lanes; mapped 7 m wide, the same lines keep their gap
    #[test]
    fn twins_apart_until_widened_to_the_maps_width() {
        let build_pair = |pave: f64| {
            let mut segs = Vec::new();
            for (f, y) in [("p", 6.0), ("q", -6.0)] {
                let xs: [f64; 2] = if f == "p" { [-400.0, 400.0] } else { [400.0, -400.0] };
                let mut sg = seg(f, &[[xs[0], y], [xs[1], y]], &[], Some((0.45, 0.55)));
                sg.dir = 1;
                sg.pave = pave;
                sg.drive = 7.0;
                segs.push(sg);
            }
            let pl = Place { segs, ..Default::default() };
            let ls = vec![DeckLine { feat: "p".into(), p: vec![[-40.0, 6.0, 12.0], [40.0, 6.0, 12.0]] },
                          DeckLine { feat: "q".into(), p: vec![[40.0, -6.0, 12.0], [-40.0, -6.0, 12.0]] }];
            build(&pl, Q, &g, &ls, Opts::default())
        };
        let wide = build_pair(14.0);
        assert!(wide.fallback.is_empty(), "{:?}", wide.fallback);
        assert!(wide.runs.iter().all(|r| r.p.iter().all(|q| absf(q[1]) > 12.0)), "{}", wide.json);
        let narrow = build_pair(7.0);
        assert!(narrow.runs.iter().any(|r| r.p.iter().all(|q| absf(q[1]) < 3.0)), "{}", narrow.json);
    }

    #[test]
    fn a_deck_with_no_heights_is_a_fallback() {
        let o = build(&place(false), Q, &g, &[], Opts::default());
        assert_eq!(o.fallback, vec!["b".to_string()]);
        assert!(o.t.is_empty());
    }

    #[test]
    fn the_same_decks_whatever_the_input_order() {
        let a = build(&place(false), Q, &g, &lines(), Opts::default());
        let b = build(&place(true), Q, &g, &lines(), Opts::default());
        assert_eq!(a.v.len(), b.v.len());
        assert_eq!(a.t, b.t);
        for (p, q) in a.v.iter().zip(b.v.iter()) { assert_eq!(p.map(f64::to_bits), q.map(f64::to_bits)); }
        assert_eq!(a.mt.len(), b.mt.len());
    }
}
