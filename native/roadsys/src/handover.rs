//! M3 structures (BNE_ROADSYS=structures; bridgerewrite/DESIGN.md 6.2): the hand-over between a deck and the ground
//! at its free ends, made on the terrain block's own lattice so the step there is 0 mm by construction, and one owner
//! per drivable point there (the deck over its first metres, the terrain before them).
//!
//! Two halves, each a pure function of the map's parts in one frame, that agree node for node:
//!
//! - `Ends::drop_at`: the ground under the first metres of every ground end of a deck lowered by DROP_M (from
//!   D0_SQUARES lattice squares past the end line to FULL_M along the deck, fading out over TAPER_M), within its half
//!   width (+ PAST_EDGE_M): every evaluator of the physics ground applies it (the terrain blocks, the skins and the
//!   content's drawn ground: roads.block_arch_raise), so a lattice node is dropped or not by its own position alone,
//!   whoever asks. A node on another ground road's surface (a road crossing under the deck, a junction at its end)
//!   keeps its ground. A ground end: a deck part's end that no other deck part (nor tunnel) goes on from and where a
//!   ground part of a road begins (the map's own topology: the same in every process).
//! - `zone`: the deck's exact variant for one anchor rebuilt over the lattice squares round each end (its quantised
//!   node heights, the drop known per node): squares with dropped and undropped corners (H) and the dropped squares
//!   of the first BLEND_M (D near) get the deck's top as lattice polygons whose undropped corners are the terrain's own
//!   nodes (same quantised height: a step of 0 there) and whose dropped corners are the deck's top there plus the end's
//!   mismatch faded out over BLEND_M (quintic: a crown or a quantum's difference becomes a gentle slope change, never
//!   a lip); squares with no dropped corner (U: the approach, up to BACK_M back) lose every deck part lying at ground
//!   level (its slab past the end, its end face, buried girders, pads); the deck beyond is kept as it was. The
//!   polygons share every vertex the kept deck has on their edges (no T-cracks, no lips at the seam).
//!
//! IEEE + - * / and sqrt only, sorted containers (BTreeMap), so the same bytes on every platform.
use crate::geom::*;
use std::collections::{BTreeMap, BTreeSet};

pub const D0_SQUARES: f64 = 1.5;     // the drop starts this many lattice squares past the end line (the base deck,
                                     // shown before the anchor's variant, covers every square it touches)
pub const DROP_M: f64 = 0.3;
pub const FULL_M: f64 = 5.0;         // m along the deck from its end: the full drop up to here ...
pub const TAPER_M: f64 = 3.0;        // ... then fading out over this (only the hand-over's metres: a deck farther on
                                     // keeps the ground the plans give it)
pub const PAST_EDGE_M: f64 = 0.15;   // the drop's half width: the part's half width + this (inside the slab: +0.45)
pub const JOIN_M: f64 = 1.0;         // deck / ground parts' ends this near meet (roads.DECK_JOIN_M)
pub const CLEAR_M: f64 = 0.5;        // a ground road's own surface: its half width + this keeps its ground
pub const BACK_M: f64 = 30.0;        // m before the end line the deck's ground-level parts are cleared
pub const BLEND_M: f64 = 6.0;        // m past the hand-over the end's mismatch fades out over (quintic)
pub const END_TOL_M: f64 = 0.30;     // an end hands over when its deck meets its ground this near (the median at its
                                     // nodes; each hand-over node too)
pub const ZRANGE_M: f64 = 0.60;      // a zone square's deck corners lie within this of each other (one surface)
pub const NEAR_UP_M: f64 = 2.0;      // a deck part over the approach squares within this over the end's ground level ...
pub const NEAR_DOWN_M: f64 = 3.0;    // ... and this under it is the deck's own (its slab past the end, parapet ends) ...
pub const OWN_BAND_M: f64 = 0.6;     // ... in the squares whose centre lies within its drop's half width + this
pub const APPROACH_BAND_M: f64 = 1.2;
pub const NEAR_END_M: f64 = 3.0;     // ... (farther back only along the end's mapped approach)
pub const TOP_ON_GROUND_M: f64 = END_TOL_M; // a top there goes only where it lies this near its square's ground (a deck
                                     // bridging a dip there stays)
pub const REACH_M: f64 = FULL_M + TAPER_M + 2.0;
pub const MIN_PART_M: f64 = 2.0;
/// (630) an approach shorter than this is a stub into a junction: the roads meeting at its far end are the end's own
/// (their surface over the deck's first metres, a roundabout's ring at a deck end, no road under it)
pub const STUB_M: f64 = REACH_M;

#[derive(Clone, Debug)]
pub struct Part { pub pts: Vec<P2>, pub hw: f64 }

#[derive(Clone, Debug)]
pub struct End {
    pub p: P2,
    pub path: Vec<P2>,       // from p into the deck, cut at REACH_M
    pub cum: Vec<f64>,
    pub hw: f64,             // the drop's half width
    pub bb: [f64; 4],
    pub approach: Vec<usize>,
    /// (630) the ground parts meeting a short approach (STUB_M) at its far end: a junction at the end, whose surface
    /// over the deck's first metres is no road under it (on_ground leaves them out, as the approaches)
    pub junction: Vec<usize>,
    /// (M5, 627) the end line (unit, to the left of the way in) when the end is skewed (skew.rs), and its slope against
    /// the way in: a point `lat` to the left of the line in lies on the end line `lat * sk` along it (0: square)
    pub dir: Option<P2>,
    pub sk: f64,
}

#[derive(Clone, Debug, Default)]
pub struct Ends { pub ends: Vec<End>, pub grounds: Vec<Part>, pub gbb: Vec<[f64; 4]>, pub s: f64 }

fn dist(a: P2, b: P2) -> f64 { norm(sub(a, b)) }

fn bbox(p: &[P2], grow: f64) -> [f64; 4] {
    let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    for q in p { b[0] = b[0].min(q[0]); b[1] = b[1].min(q[1]); b[2] = b[2].max(q[0]); b[3] = b[3].max(q[1]); }
    [b[0] - grow, b[1] - grow, b[2] + grow, b[3] + grow]
}

fn inside(b: &[f64; 4], x: f64, y: f64) -> bool { x >= b[0] && x <= b[2] && y >= b[1] && y <= b[3] }

fn length(p: &[P2]) -> f64 { p.windows(2).map(|w| dist(w[0], w[1])).sum() }

/// The polyline from its start, cut at `reach` m (interpolated).
fn head(p: &[P2], reach: f64) -> Vec<P2> {
    let mut out = vec![p[0]];
    let mut acc = 0.0;
    for w in p.windows(2) {
        let l = dist(w[0], w[1]);
        if l <= 0.0 { continue; }
        if acc + l >= reach {
            out.push(lerp(w[0], w[1], (reach - acc) / l));
            return out;
        }
        acc += l;
        out.push(w[1]);
    }
    out
}

fn mmkey(p: &[P2]) -> Vec<I2> { p.iter().map(|&q| mm2(q)).collect() }

/// (627) m a skewed end's tip keeps off another line's surface; the half width taken for a railway or a waterway
pub const TIP_CLEAR_M: f64 = 1.0;
pub const TIP_OTHER_HW_M: f64 = 2.0;
/// the skew's slope eased by these factors in turn until its tip is clear (then square)
pub const TIP_EASE: [f64; 4] = [1.0, 0.75, 0.5, 0.25];

fn seg_seg_dist(a: P2, b: P2, c: P2, d: P2) -> f64 {
    if seg_x(a, b, c, d).is_some() { return 0.0; }
    let pd = |q: P2, x: P2, y: P2| -> f64 {
        let xy = sub(y, x);
        let l2 = dot(xy, xy);
        let u = if l2 > 0.0 { clampf(dot(sub(q, x), xy) / l2, 0.0, 1.0) } else { 0.0 };
        dist(q, add(x, scale(xy, u)))
    };
    pd(a, c, d).min(pd(b, c, d)).min(pd(c, a, b)).min(pd(d, a, b))
}

fn in_tri(q: P2, a: P2, b: P2, c: P2) -> bool {
    let (d1, d2, d3) = (cross(sub(b, a), sub(q, a)), cross(sub(c, b), sub(q, b)), cross(sub(a, c), sub(q, c)));
    !((d1 < 0.0 || d2 < 0.0 || d3 < 0.0) && (d1 > 0.0 || d2 > 0.0 || d3 > 0.0))
}

/// Is the tip of an end at p (t: into the deck, n: its left) skewed by slope k (deck half width hw) clear of the lines'
/// surfaces (each line's half width + TIP_CLEAR_M)? The tip: the triangle past the square end line, between the square
/// corner and the skewed one on the side where the end line runs back over the approach.
fn tip_clear(p: P2, t: P2, n: P2, k: f64, hw: f64, lines: &[(&[P2], u8)], widths: &[f64]) -> bool {
    let side = if k > 0.0 { -1.0 } else { 1.0 };       // (the corner at lateral side * hw lies k * side * hw along t)
    let a = add(p, scale(n, side * hw));
    let c = add(a, scale(t, k * side * hw));
    for (li, (q, _kind)) in lines.iter().enumerate() {
        let r = widths.get(li).copied().unwrap_or(TIP_OTHER_HW_M) + TIP_CLEAR_M;
        for w in q.windows(2) {
            if in_tri(w[0], p, a, c) || in_tri(w[1], p, a, c) { return false; }
            if seg_seg_dist(w[0], w[1], p, a) <= r || seg_seg_dist(w[0], w[1], a, c) <= r || seg_seg_dist(w[0], w[1], c, p) <= r {
                return false;
            }
        }
    }
    true
}

impl Ends {
    /// The ground ends of the deck parts `bridges` (each part's line in the frame, its half width), the ground parts
    /// `grounds` (their lines and half widths: the approaches, and the roads whose surface keeps its ground) and the
    /// tunnel parts' lines; `s` the terrain lattice's square (m). Duplicates (a part read from two cells) count once.
    pub fn new(bridges: &[Part], grounds: Vec<Part>, tunnels: &[Vec<P2>], s: f64) -> Ends {
        Ends::new_skewed(bridges, grounds, tunnels, s, None)
    }

    /// new, the ends skewed by skew.rs's rule when `skew` is given: (the other lines that may pass under a structure
    /// (railways, waterways) with their kinds, per bridge part (bridges' order) the inventory's record or None). The
    /// ground parts are the roads that may pass under (an end's own approaches left out).
    #[allow(clippy::type_complexity)]
    pub fn new_skewed(bridges: &[Part], grounds: Vec<Part>, tunnels: &[Vec<P2>], s: f64,
                      skew: Option<(&[(Vec<P2>, u8)], &[Option<[f64; 4]>], &[bool])>) -> Ends {
        // (one copy of each part, in a canonical order: the same ends whatever order the cells came in)
        let mut seen: BTreeSet<Vec<I2>> = BTreeSet::new();
        let mut br: Vec<(Vec<I2>, &Part, usize)> = Vec::new();
        for (bi, b) in bridges.iter().enumerate() {
            if b.pts.len() < 2 || !(b.hw > 0.0) || b.pts.iter().any(|q| !q.iter().all(|v| v.is_finite())) { continue; }
            let k = mmkey(&b.pts);
            let mut r = k.clone();
            r.reverse();
            let key = if r < k { r } else { k.clone() };
            if seen.insert(key) { br.push((k, b, bi)); }
        }
        // (a record per part: the first copy's, the parts' keys ordering the rest)
        br.sort_by(|a, b| a.0.cmp(&b.0).then(a.2.cmp(&b.2)));
        let mut gs: Vec<(Vec<I2>, Part, bool)> = Vec::new();
        let mut gseen: BTreeMap<Vec<I2>, usize> = BTreeMap::new();
        for (gi, g) in grounds.into_iter().enumerate() {
            if g.pts.len() < 2 || g.pts.iter().any(|q| !q.iter().all(|v| v.is_finite())) { continue; }
            let k = mmkey(&g.pts);
            // (627: a ground part whose surface cannot be cut along a skewed end line: its ends stay square)
            let ns = skew.map(|x| x.2.get(gi).copied().unwrap_or(false)).unwrap_or(false);
            match gseen.get(&k) {
                Some(&j) => { if ns { gs[j].2 = true; } }
                None => { gseen.insert(k.clone(), gs.len()); gs.push((k, g, ns)); }
            }
        }
        gs.sort_by(|a, b| a.0.cmp(&b.0));
        let noskew_g: Vec<bool> = gs.iter().map(|x| x.2).collect();
        let grounds: Vec<Part> = gs.into_iter().map(|x| x.1).collect();
        let gbb: Vec<[f64; 4]> = grounds.iter().map(|g| bbox(&g.pts, g.hw + CLEAR_M)).collect();
        let mut ends = Vec::new();
        let parts: Vec<&[P2]> = br.iter().map(|x| x.1.pts.as_slice()).collect();
        for (bi, (_k, b, src)) in br.iter().enumerate() {
            if length(&b.pts) < MIN_PART_M { continue; }
            for side in 0..2 {
                let line: Vec<P2> = if side == 0 { b.pts.clone() } else { b.pts.iter().rev().cloned().collect() };
                let p = line[0];
                let dir = |q: &[P2]| -> Option<P2> { q.iter().skip(1).find(|v| dist(**v, q[0]) > 0.5).and_then(|v| unit(sub(*v, q[0]))) };
                let own = dir(&line);
                // (another deck part going on from here; a copy of this one, the same way on from the end (a road read
                // whole and as its lane sections), is no other deck)
                let joined = br.iter().enumerate().any(|(oj, (_k2, o, _))| {
                    if oj == bi { return false; }
                    let ol: Vec<P2> = if dist(o.pts[0], p) < JOIN_M { o.pts.clone() }
                        else if dist(*o.pts.last().unwrap(), p) < JOIN_M { o.pts.iter().rev().cloned().collect() }
                        else { return false };
                    match (own, dir(&ol)) { (Some(a), Some(b)) => dot(a, b) < 0.98, _ => true }
                }) || tunnels.iter().any(|t| !t.is_empty() && (dist(t[0], p) < JOIN_M || dist(*t.last().unwrap(), p) < JOIN_M));
                if joined { continue; }
                let approach: Vec<usize> = grounds.iter().enumerate()
                    .filter(|(_, g)| dist(g.pts[0], p) < JOIN_M || dist(*g.pts.last().unwrap(), p) < JOIN_M)
                    .map(|(i, _)| i).collect();
                if approach.is_empty() { continue; }
                let mut junction: Vec<usize> = Vec::new();
                for &a in &approach {
                    let g = &grounds[a];
                    if length(&g.pts) >= STUB_M { continue; }
                    let far = if dist(g.pts[0], p) < JOIN_M { *g.pts.last().unwrap() } else { g.pts[0] };
                    for (gi, o) in grounds.iter().enumerate() {
                        if gi == a || approach.contains(&gi) || junction.contains(&gi) { continue; }
                        if dist(o.pts[0], far) < JOIN_M || dist(*o.pts.last().unwrap(), far) < JOIN_M { junction.push(gi); }
                    }
                }
                junction.sort_unstable();
                let path = head(&line, REACH_M);
                if path.len() < 2 { continue; }
                let mut cum = vec![0.0];
                for w in path.windows(2) { let l = cum[cum.len() - 1] + dist(w[0], w[1]); cum.push(l); }
                let hw = b.hw + PAST_EDGE_M;
                let mut bb = bbox(&path, hw + 1.0);
                // (627) the end's skew: the structure walked in from here, the lines that may pass under it
                let (mut dir_e, mut sk) = (None, 0.0);
                if let Some((crossed, nbi, _)) = skew.filter(|_| !approach.iter().any(|&gi| noskew_g[gi])) {
                    let (inward, slen) = crate::skew::walk(&parts, bi, side);
                    let mut lines: Vec<(&[P2], u8)> = grounds.iter().enumerate().filter(|(gi, _)| !approach.contains(gi))
                        .map(|(_, g)| (g.pts.as_slice(), crate::skew::KIND_ROAD)).collect();
                    lines.extend(crossed.iter().map(|(q, k)| (q.as_slice(), *k)));
                    let rec = nbi.get(*src).copied().flatten();
                    if let (Some(d), Some(t)) = (crate::skew::decide(&inward, slen, &lines, rec), unit(sub(path[1], path[0]))) {
                        let n = [-t[1], t[0]];
                        let (ux, uy) = (dot(d, t), dot(d, n));
                        if uy > 1e-6 {
                            // (the deck's tip past the square end line, over the approach, kept clear of every other
                            // line's surface + TIP_CLEAR_M: the skew eased (TIP_EASE) until it is, else square)
                            let widths: Vec<f64> = grounds.iter().enumerate().filter(|(gi, _)| !approach.contains(gi))
                                .map(|(_, g)| g.hw).chain(crossed.iter().map(|_| TIP_OTHER_HW_M)).collect();
                            for f in TIP_EASE {
                                let k = ux / uy * f;
                                if k == 0.0 { break; }
                                // (and the skewed rows of both ends apart along the part: deck.rs set_skews' span rule)
                                if absf(k) * b.hw * 2.0 + 2.0 * crate::part::SKEW_GAP_M < length(&b.pts)
                                    && tip_clear(p, t, n, k, b.hw, &lines, &widths) {
                                    dir_e = unit(add(n, scale(t, k)));
                                    sk = k;
                                    bb = bbox(&path, hw + 1.0 + absf(sk) * hw);
                                    break;
                                }
                            }
                        }
                    }
                }
                ends.push(End { p, path, cum, hw, bb, approach, junction, dir: dir_e, sk });
            }
        }
        Ends { ends, grounds, gbb, s }
    }

    /// (d, lat) of point q against end k: d the distance along the deck from its end line (negative before it, over
    /// the approach: the first segment carried back), lat the distance off its line.
    pub fn local(&self, k: usize, q: P2) -> (f64, f64) {
        let e = &self.ends[k];
        let mut best = (f64::INFINITY, 0.0, 0.0);
        for j in 0..e.path.len() - 1 {
            let (a, b) = (e.path[j], e.path[j + 1]);
            let ab = sub(b, a);
            let l2 = dot(ab, ab);
            if l2 <= 0.0 { continue; }
            let mut u = dot(sub(q, a), ab) / l2;
            if u > 1.0 { u = 1.0; }
            if u < 0.0 && j > 0 { u = 0.0; }
            let c = add(a, scale(ab, u));
            let dd = dist(q, c);
            if dd < best.0 { best = (dd, e.cum[j] + u * l2.sqrt(), if cross(ab, sub(q, a)) >= 0.0 { dd } else { -dd }); }
        }
        // (627: a skewed end's d from its end line, `lat * sk` along the way in at `lat` to its left)
        if e.sk != 0.0 { return (best.1 - best.2 * e.sk, best.0); }
        (best.1, best.0)
    }

    /// The fraction of DROP_M end k lowers its ground by at q (0 off its band), before the ground roads' exclusion.
    fn weight(&self, k: usize, q: P2) -> f64 {
        let e = &self.ends[k];
        let (d, lat) = self.local(k, q);
        if lat > e.hw { return 0.0; }
        let d0 = D0_SQUARES * self.s;
        if d < d0 { return 0.0; }
        if d <= FULL_M { return 1.0; }
        if d >= FULL_M + TAPER_M { return 0.0; }
        (FULL_M + TAPER_M - d) / TAPER_M
    }

    /// Is q on a ground road's own surface (projecting inside one of its segments within its half width + CLEAR_M),
    /// other than end k's approaches and the junction at a short approach's far end?
    fn on_ground(&self, k: usize, q: P2) -> bool {
        let e = &self.ends[k];
        for (gi, g) in self.grounds.iter().enumerate() {
            if !inside(&self.gbb[gi], q[0], q[1]) || e.approach.contains(&gi) || e.junction.contains(&gi) { continue; }
            for w in g.pts.windows(2) {
                let ab = sub(w[1], w[0]);
                let l2 = dot(ab, ab);
                if l2 <= 0.0 { continue; }
                let u = dot(sub(q, w[0]), ab) / l2;
                if !(u > 0.0 && u < 1.0) { continue; }
                if dist(q, add(w[0], scale(ab, u))) <= g.hw + CLEAR_M { return true; }
            }
        }
        false
    }

    /// Is q on one of end k's approaches (within its half width + APPROACH_BAND_M of its line: the slab past an end
    /// is the road's width and its parapets', a square's centre half a square off)?
    pub fn near_approach(&self, k: usize, q: P2) -> bool {
        for &gi in &self.ends[k].approach {
            let g = &self.grounds[gi];
            for w in g.pts.windows(2) {
                let ab = sub(w[1], w[0]);
                let l2 = dot(ab, ab);
                if l2 <= 0.0 { continue; }
                let u = clampf(dot(sub(q, w[0]), ab) / l2, 0.0, 1.0);
                if dist(q, add(w[0], scale(ab, u))) <= g.hw + APPROACH_BAND_M { return true; }
            }
        }
        false
    }

    /// m the ground at (x, y) is lowered by (0 almost everywhere).
    pub fn drop_at(&self, x: f64, y: f64) -> f64 {
        let q = [x, y];
        let mut best: f64 = 0.0;
        for k in 0..self.ends.len() {
            if !inside(&self.ends[k].bb, x, y) { continue; }
            let w = self.weight(k, q);
            if w <= 0.0 || DROP_M * w <= best { continue; }
            if self.on_ground(k, q) { continue; }
            best = DROP_M * w;
        }
        best
    }

    /// The ends' boxes (x0, y0, x1, y1) grown by m: where a zone may reach.
    pub fn boxes(&self, grow: f64) -> Vec<[f64; 4]> {
        self.ends.iter().map(|e| {
            let back = BACK_M + e.hw * (1.0 + absf(e.sk)) + 2.0 * self.s + grow;
            let mut b = e.bb;
            b = [b[0].min(e.p[0] - back), b[1].min(e.p[1] - back), b[2].max(e.p[0] + back), b[3].max(e.p[1] + back)];
            [b[0] - grow, b[1] - grow, b[2] + grow, b[3] + grow]
        }).collect()
    }
}

// ------------------------------------------------------------------------------------------------------------- zone

/// A triangle soup (the deck's shape, every material): vertex positions and texture coordinates, triangles CCW seen
/// from outside, a material per triangle.
#[derive(Clone, Debug, Default)]
pub struct Soup { pub p: Vec<[f64; 3]>, pub uv: Vec<[f64; 2]>, pub t: Vec<[u32; 3]>, pub m: Vec<u32> }

/// The terrain lattice round the ends: node (i0 + a, j0 + b) at (x, y) = ((i0 + a) s, (j0 + b) s), its quantised
/// height z (with the drop) and its drop (row-major in b, a).
#[derive(Clone, Debug, Default)]
pub struct Grid { pub i0: i64, pub j0: i64, pub nx: usize, pub ny: usize, pub s: f64, pub z: Vec<f64>, pub drop: Vec<f64> }

impl Grid {
    fn node(&self, i: i64, j: i64) -> Option<(f64, f64)> {
        let (a, b) = (i - self.i0, j - self.j0);
        if a < 0 || b < 0 || a as usize >= self.nx || b as usize >= self.ny { return None; }
        let k = b as usize * self.nx + a as usize;
        let (z, d) = (self.z[k], self.drop[k]);
        if !(z.is_finite() && d.is_finite()) { return None; }
        Some((z, d))
    }
    fn xy(&self, i: i64, j: i64) -> P2 { [i as f64 * self.s, j as f64 * self.s] }
}

#[derive(Clone, Debug, Default)]
pub struct ZoneStats {
    pub ends: usize, pub ends_kept: usize, pub squares_u: usize, pub squares_h: usize, pub squares_z: usize,
    pub cleared: usize, pub replaced: usize, pub zone_tris: usize, pub split: usize, pub max_mismatch_mm: f64,
}

#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Debug)]
enum Cls { U, Z, Keep }

type Poly = Vec<([f64; 3], [f64; 2])>;

/// Sutherland-Hodgman of a polygon against one axis-aligned half plane (axis 0 x, 1 y; keep >= c or <= c); points made
/// on the line get exactly c there.
fn clip_axis(poly: &Poly, axis: usize, c: f64, ge: bool) -> Poly {
    let mut out: Poly = Vec::with_capacity(poly.len() + 2);
    let n = poly.len();
    for i in 0..n {
        let (a, b) = (poly[i], poly[(i + 1) % n]);
        let ia = if ge { a.0[axis] >= c } else { a.0[axis] <= c };
        let ib = if ge { b.0[axis] >= c } else { b.0[axis] <= c };
        if ia { out.push(a); }
        if ia != ib {
            let u = (c - a.0[axis]) / (b.0[axis] - a.0[axis]);
            let mut p = [a.0[0] + (b.0[0] - a.0[0]) * u, a.0[1] + (b.0[1] - a.0[1]) * u, a.0[2] + (b.0[2] - a.0[2]) * u];
            p[axis] = c;
            let t = [a.1[0] + (b.1[0] - a.1[0]) * u, a.1[1] + (b.1[1] - a.1[1]) * u];
            out.push((p, t));
        }
    }
    out
}

fn clip_square(poly: &Poly, x0: f64, y0: f64, x1: f64, y1: f64) -> Poly {
    let mut p = clip_axis(poly, 0, x0, true);
    if p.len() < 3 { return Vec::new(); }
    p = clip_axis(&p, 0, x1, false);
    if p.len() < 3 { return Vec::new(); }
    p = clip_axis(&p, 1, y0, true);
    if p.len() < 3 { return Vec::new(); }
    p = clip_axis(&p, 1, y1, false);
    if p.len() < 3 { return Vec::new(); }
    p
}

fn normal3(a: [f64; 3], b: [f64; 3], c: [f64; 3]) -> [f64; 3] {
    let (u, v) = ([b[0] - a[0], b[1] - a[1], b[2] - a[2]], [c[0] - a[0], c[1] - a[1], c[2] - a[2]]);
    [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
}

fn n3(n: [f64; 3]) -> f64 { (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]).sqrt() }

fn poly_normal(p: &Poly) -> [f64; 3] {
    let mut s = [0.0; 3];
    for k in 1..p.len().saturating_sub(1) {
        let n = normal3(p[0].0, p[k].0, p[k + 1].0);
        s = [s[0] + n[0], s[1] + n[1], s[2] + n[2]];
    }
    s
}

fn ray_tri(p: &[[f64; 3]; 3], x: f64, y: f64) -> Option<f64> {
    let d = (p[1][0] - p[0][0]) * (p[2][1] - p[0][1]) - (p[2][0] - p[0][0]) * (p[1][1] - p[0][1]);
    if absf(d) < 1e-12 { return None; }
    let w1 = ((x - p[0][0]) * (p[2][1] - p[0][1]) - (p[2][0] - p[0][0]) * (y - p[0][1])) / d;
    let w2 = ((p[1][0] - p[0][0]) * (y - p[0][1]) - (x - p[0][0]) * (p[1][1] - p[0][1])) / d;
    let e = -1e-9;
    if w1 < e || w2 < e || w1 + w2 > 1.0 - e { return None; }
    Some(p[0][2] + w1 * (p[1][2] - p[0][2]) + w2 * (p[2][2] - p[0][2]))
}

/// The deck rebuilt over the lattice squares round the ground ends (see the module notes). `top`: the material of the
/// deck's drivable top; `tile_uv`: m per texture repeat of the polygons (world xy / it).
pub fn zone(soup: &Soup, top: u32, ends: &Ends, g: &Grid, tile_uv: f64) -> (Soup, ZoneStats) {
    let mut st = ZoneStats { ends: ends.ends.len(), ..Default::default() };
    let s = g.s;
    let d0 = D0_SQUARES * s;
    let corners_of = |i: i64, j: i64| [(i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1)];
    let frames: Vec<Option<(P2, P2)>> = ends.ends.iter().map(|e| unit(sub(e.path[1], e.path[0])).map(|t| (t, [-t[1], t[0]]))).collect();
    // (1) every square round an end (its straight region: BACK_M before it to the blend's end past it, its half width
    // and two squares either side), its corners known; the end it belongs to the one whose line is nearest its centre
    #[derive(Clone, Copy, PartialEq, Debug)]
    enum Raw { U, H, D }
    let mut raw: BTreeMap<(i64, i64), (Raw, usize, f64)> = BTreeMap::new();
    for (k, e) in ends.ends.iter().enumerate() {
        let Some((t0, n0)) = frames[k] else { continue };
        let lat = e.hw + 2.0 * s;
        let skx = absf(e.sk) * lat;          // (627: a skewed end's line reaches this far along either way)
        let far = d0 + BLEND_M + 2.0 * s + skx;
        let back = BACK_M + skx;
        let corners = [add(e.p, add(scale(t0, -back), scale(n0, lat))), add(e.p, add(scale(t0, -back), scale(n0, -lat))),
                       add(e.p, add(scale(t0, far), scale(n0, lat))), add(e.p, add(scale(t0, far), scale(n0, -lat)))];
        let bb = bbox(&corners, s);
        let (i0, i1) = ((bb[0] / s).floor() as i64, (bb[2] / s).floor() as i64);
        let (j0, j1) = ((bb[1] / s).floor() as i64, (bb[3] / s).floor() as i64);
        for j in j0..=j1 {
            for i in i0..=i1 {
                let cs = corners_of(i, j);
                let mut known = true;
                let mut any_in = false;
                let mut nd = 0;
                for &(a, b) in &cs {
                    match g.node(a, b) {
                        None => { known = false; break; }
                        Some((_z, dr)) => { if dr > 0.0 { nd += 1; } }
                    }
                    let q = sub(g.xy(a, b), e.p);
                    let (dd, ll) = (dot(q, t0), absf(dot(q, n0)));
                    if dd >= -back && dd <= far && ll <= lat { any_in = true; }
                }
                if !known || !any_in { continue; }
                let c = [(i as f64 + 0.5) * s, (j as f64 + 0.5) * s];
                let (_d, l) = ends.local(k, c);
                let r = if nd == 0 { Raw::U } else if nd == 4 { Raw::D } else { Raw::H };
                match raw.get(&(i, j)) {
                    Some(&(_, _, l0)) if l0 <= l => {}
                    _ => { raw.insert((i, j), (r, k, l)); }
                }
            }
        }
    }
    // an undropped corner on the end's side of the drop (not beside it): where the deck meets its approach
    let end_side = |k: usize, a: i64, b: i64| -> bool {
        let (d, l) = ends.local(k, g.xy(a, b));
        d < d0 && l <= ends.ends[k].hw
    };
    // (2) the deck's top near each node: the hit nearest the node's own ground (before the drop)
    let mut top_grid: BTreeMap<(i64, i64), Vec<usize>> = BTreeMap::new();
    for k in 0..soup.t.len() {
        if soup.m[k] != top { continue; }
        let t = soup.t[k];
        let pts = [soup.p[t[0] as usize], soup.p[t[1] as usize], soup.p[t[2] as usize]];
        let n = normal3(pts[0], pts[1], pts[2]);
        if !(n[2] > 0.45 * n3(n)) { continue; }
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for v in &pts { x0 = x0.min(v[0]); y0 = y0.min(v[1]); x1 = x1.max(v[0]); y1 = y1.max(v[1]); }
        let (a0, a1, b0, b1) = ((x0 / 4.0).floor() as i64, (x1 / 4.0).floor() as i64, (y0 / 4.0).floor() as i64, (y1 / 4.0).floor() as i64);
        if (a1 - a0 + 1) * (b1 - b0 + 1) > 40000 { continue; }
        for a in a0..=a1 { for b in b0..=b1 { top_grid.entry((a, b)).or_default().push(k); } }
    }
    let deck_at = |x: f64, y: f64, near: f64| -> Option<f64> {
        let mut best: Option<f64> = None;
        if let Some(v) = top_grid.get(&((x / 4.0).floor() as i64, (y / 4.0).floor() as i64)) {
            for &k in v {
                let t = soup.t[k];
                let pts = [soup.p[t[0] as usize], soup.p[t[1] as usize], soup.p[t[2] as usize]];
                if let Some(z) = ray_tri(&pts, x, y) {
                    if absf(z - near) <= 3.0 && best.map_or(true, |b| absf(z - near) < absf(b - near)) { best = Some(z); }
                }
            }
        }
        best
    };
    // each corner's ground before the drop and the deck's top over it (nearest that ground)
    let mut dk: BTreeMap<(i64, i64), Option<f64>> = BTreeMap::new();
    for &(i, j) in raw.keys() {
        for c in corners_of(i, j) {
            if dk.contains_key(&c) { continue; }
            let (z, dr) = g.node(c.0, c.1).unwrap();
            let q = g.xy(c.0, c.1);
            dk.insert(c, deck_at(q[0], q[1], z + dr));
        }
    }
    // (3) per end: the mismatch at its hand-over nodes (end-side undropped corners of its H squares under its deck:
    // ground - deck top); active when their median is within END_TOL_M (else the deck is kept whole: a deck end high
    // over or buried under its ground is the profile's to fix, not a hand-over's)
    let mut mism: BTreeMap<usize, BTreeMap<(i64, i64), (f64, f64)>> = BTreeMap::new();     // end -> node -> (lat, m)
    for (&(i, j), &(r, k, _)) in &raw {
        if r != Raw::H { continue; }
        let Some((_t0, n0)) = frames[k] else { continue };
        for c in corners_of(i, j) {
            let (z, dr) = g.node(c.0, c.1).unwrap();
            if dr > 0.0 || !end_side(k, c.0, c.1) { continue; }
            if let Some(dz) = dk[&c] { mism.entry(k).or_default().insert(c, (dot(sub(g.xy(c.0, c.1), ends.ends[k].p), n0), z - dz)); }
        }
    }
    let mut active: BTreeSet<usize> = BTreeSet::new();
    let mut level: BTreeMap<usize, f64> = BTreeMap::new();         // the end's ground at its hand-over (median)
    for (&k, v) in &mism {
        let mut a: Vec<f64> = v.values().map(|x| absf(x.1)).collect();
        a.sort_by(|p, q| p.partial_cmp(q).unwrap());
        if a.is_empty() { continue; }
        let med = a[a.len() / 2];
        if med <= END_TOL_M {
            active.insert(k);
            let mut zs: Vec<f64> = v.keys().map(|c| g.node(c.0, c.1).unwrap().0).collect();
            zs.sort_by(|p, q| p.partial_cmp(q).unwrap());
            level.insert(k, zs[zs.len() / 2]);
        }
    }
    // (the blend's samples: the hand-over nodes within the tolerance)
    for (k, v) in mism.iter_mut() {
        v.retain(|_c, x| absf(x.1) <= END_TOL_M);
        if active.contains(k) { for x in v.values() { st.max_mismatch_mm = st.max_mismatch_mm.max(absf(x.1) * 1000.0); } }
    }
    st.ends_kept = st.ends - active.len();
    // the classes: Z (the zone's polygons) for an active end's H squares at its side and its D squares of the blend,
    // each only where the deck's top over its dropped corners is one surface (within ZRANGE_M) and its undropped
    // corners hand over (within END_TOL_M); U (cleared) its approach squares; Keep the rest
    let mut cls: BTreeMap<(i64, i64), (Cls, usize)> = BTreeMap::new();
    let mut hand: BTreeSet<(i64, i64)> = BTreeSet::new();
    for (&(i, j), &(r, k, _)) in &raw {
        let cs = corners_of(i, j);
        let mut c = Cls::Keep;
        if active.contains(&k) {
            match r {
                Raw::U => {
                    // (its own band only: the deck's part past its end lies on its own line; a deck beside it is
                    // another's, whatever its level)
                    // (and past the end's first metres only along its mapped approach: a switchback's other leg
                    // behind the end is its deck, not its slab past the end)
                    let cc = [(i as f64 + 0.5) * s, (j as f64 + 0.5) * s];
                    let (dc, lc) = ends.local(k, cc);
                    if dc < d0 && lc <= ends.ends[k].hw + OWN_BAND_M && (dc >= -NEAR_END_M || ends.near_approach(k, cc)) { c = Cls::U; }
                }
                Raw::H | Raw::D => {
                    let side = r == Raw::H && cs.iter().any(|&(a, b)| g.node(a, b).unwrap().1 <= 0.0 && end_side(k, a, b));
                    let dmin = cs.iter().map(|&(a, b)| ends.local(k, g.xy(a, b)).0).fold(f64::INFINITY, f64::min);
                    // (the end side's squares, and every square of the blend whose undropped corners hand over: its
                    // sides too, so the zone does not meet the kept deck beside its hand-over)
                    let want = side || dmin <= d0 + BLEND_M;
                    let mut zs: Vec<f64> = Vec::new();
                    let mut ok = want;
                    for &(a, b) in &cs {
                        if !ok { break; }
                        let (z, dr) = g.node(a, b).unwrap();
                        match dk[&(a, b)] {
                            None => { if dr > 0.0 { ok = false; } }
                            Some(dz) => { if dr > 0.0 { zs.push(dz); } else if absf(z - dz) > END_TOL_M { ok = false; } }
                        }
                    }
                    if ok && !zs.is_empty() {
                        let lo = zs.iter().cloned().fold(f64::INFINITY, f64::min);
                        let hi = zs.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
                        ok = hi - lo <= ZRANGE_M;
                    }
                    if ok && !zs.is_empty() { c = Cls::Z; if r == Raw::H { hand.insert((i, j)); } }
                }
            }
        }
        cls.insert((i, j), (c, k));
    }
    // squares kept whole (their deck as it was) whose corner a zone square shares: there the zone takes the deck's
    // own height (no fade), so the seam is exact
    let kept_corner = |a: i64, b: i64| -> bool {
        [(a - 1, b - 1), (a, b - 1), (a - 1, b), (a, b)].iter().any(|sq| match cls.get(sq) { None => true, Some((Cls::Keep, _)) => true, _ => false })
    };
    let zkeys: Vec<(i64, i64)> = cls.iter().filter(|(_, c)| c.0 == Cls::Z).map(|(k, _)| *k).collect();
    let mut corner_z: BTreeMap<(i64, i64), f64> = BTreeMap::new();     // the zone's height at its nodes
    for &(i, j) in &zkeys {
        let k = cls[&(i, j)].1;
        let (_t0, n0) = frames[k].unwrap();
        for c in corners_of(i, j) {
            if corner_z.contains_key(&c) { continue; }
            let (z, dr) = g.node(c.0, c.1).unwrap();
            let q = g.xy(c.0, c.1);
            let v = if dr <= 0.0 {
                // (an undropped corner a kept square shares: the kept deck's own height there, so the seam is exact;
                // it hands over within END_TOL_M, beside the lanes)
                match dk[&c] { Some(dz) if kept_corner(c.0, c.1) && !end_side(k, c.0, c.1) => dz, _ => z }
            } else {
                let dz = dk[&c].unwrap();
                if kept_corner(c.0, c.1) { dz } else {
                    // the end's mismatch at this lateral offset (its nearest hand-over nodes), faded out (quintic)
                    let l = dot(sub(q, ends.ends[k].p), n0);
                    let m = match mism.get(&k) {
                        None => 0.0,
                        Some(v) => {
                            let (mut sw, mut sm) = (0.0, 0.0);
                            for &(lv, mv) in v.values() { let w = 1.0 / (1.0 + absf(lv - l)); sw += w; sm += w * mv; }
                            if sw > 0.0 { sm / sw } else { 0.0 }
                        }
                    };
                    let (d, _) = ends.local(k, q);
                    dz + m * (1.0 - quintic((d - (d0 - s)) / BLEND_M))
                }
            };
            corner_z.insert(c, v);
        }
    }
    // (4) every triangle meeting a classified square cut into lattice squares; each piece by its square's class
    let zone_rng = |i: i64, j: i64| -> (f64, f64) {
        let v: Vec<f64> = corners_of(i, j).iter().filter_map(|c| corner_z.get(c).cloned()).collect();
        (v.iter().cloned().fold(f64::INFINITY, f64::min), v.iter().cloned().fold(f64::NEG_INFINITY, f64::max))
    };
    let ground_rng = |i: i64, j: i64| -> (f64, f64) {
        let v: Vec<f64> = corners_of(i, j).iter().map(|&(a, b)| g.node(a, b).unwrap().0).collect();
        (v.iter().cloned().fold(f64::INFINITY, f64::min), v.iter().cloned().fold(f64::NEG_INFINITY, f64::max))
    };
    let mut out = Soup::default();
    let mut vmap: BTreeMap<u32, u32> = BTreeMap::new();
    let mut edge_pts: BTreeMap<(i64, i64), Vec<([f64; 3], [f64; 2])>> = BTreeMap::new();   // kept top vertices per square
    let push_poly = |out: &mut Soup, p: &Poly, m: u32| {
        let base = out.p.len() as u32;
        for v in p { out.p.push(v.0); out.uv.push(v.1); }
        for k in 1..p.len() - 1 { out.t.push([base, base + k as u32, base + k as u32 + 1]); out.m.push(m); }
    };
    for (k, t) in soup.t.iter().enumerate() {
        let pts = [soup.p[t[0] as usize], soup.p[t[1] as usize], soup.p[t[2] as usize]];
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for v in &pts { x0 = x0.min(v[0]); y0 = y0.min(v[1]); x1 = x1.max(v[0]); y1 = y1.max(v[1]); }
        let (a0, a1) = ((x0 / s).floor() as i64, (x1 / s).floor() as i64);
        let (b0, b1) = ((y0 / s).floor() as i64, (y1 / s).floor() as i64);
        let mut meets = false;
        if (a1 - a0 + 1) * (b1 - b0 + 1) <= 250000 {
            'm: for b in b0..=b1 { for a in a0..=a1 { if matches!(cls.get(&(a, b)), Some((Cls::U, _)) | Some((Cls::Z, _))) { meets = true; break 'm; } } }
        }
        if !meets {
            let mut idx = [0u32; 3];
            for c in 0..3 {
                let v = t[c];
                idx[c] = *vmap.entry(v).or_insert_with(|| {
                    out.p.push(soup.p[v as usize]); out.uv.push(soup.uv[v as usize]); (out.p.len() - 1) as u32
                });
            }
            out.t.push(idx);
            out.m.push(soup.m[k]);
            continue;
        }
        let is_top = soup.m[k] == top && { let n = normal3(pts[0], pts[1], pts[2]); n[2] > 0.45 * n3(n) };
        let poly: Poly = (0..3).map(|c| (pts[c], soup.uv[t[c] as usize])).collect();
        // its pieces per square and whether each goes; a triangle none of whose pieces goes stays whole (no seams
        // made where nothing changes: a deck over the zone, a girder beside it)
        let mut pieces: Vec<((i64, i64), Poly, bool)> = Vec::new();
        let mut any_gone = false;
        for b in b0..=b1 {
            for a in a0..=a1 {
                let piece = clip_square(&poly, a as f64 * s, b as f64 * s, (a + 1) as f64 * s, (b + 1) as f64 * s);
                if piece.len() < 3 || n3(poly_normal(&piece)) < 1e-10 { continue; }
                let zmin = piece.iter().map(|v| v.0[2]).fold(f64::INFINITY, f64::min);
                let zmax = piece.iter().map(|v| v.0[2]).fold(f64::NEG_INFINITY, f64::max);
                let gone = match cls.get(&(a, b)) {
                    None | Some((Cls::Keep, _)) => false,
                    // (the deck's own parts at its hand-over's level: its slab past the end, its end face, its
                    // parapets' ends, buried girders; another deck's farther up or down stays)
                    Some(&(Cls::U, kk)) => {
                        // (a top only where it lies on its square's own ground: a deck bridging a dip there stays)
                        let lv = level[&kk];
                        let (tmin, tmax) = ground_rng(a, b);
                        zmax <= lv + NEAR_UP_M && zmin >= lv - NEAR_DOWN_M && zmax <= tmax + NEAR_UP_M
                            && (!is_top || (zmin >= tmin - TOP_ON_GROUND_M && zmax <= tmax + TOP_ON_GROUND_M))
                    }
                    Some((Cls::Z, _)) => { let (lo, hi) = zone_rng(a, b); is_top && zmin <= hi + 0.5 && zmax >= lo - 0.5 }
                };
                any_gone |= gone;
                pieces.push(((a, b), piece, gone));
            }
        }
        if !any_gone {
            let mut idx = [0u32; 3];
            for c in 0..3 {
                let v = t[c];
                idx[c] = *vmap.entry(v).or_insert_with(|| {
                    out.p.push(soup.p[v as usize]); out.uv.push(soup.uv[v as usize]); (out.p.len() - 1) as u32
                });
            }
            out.t.push(idx);
            out.m.push(soup.m[k]);
            continue;
        }
        st.split += 1;
        for (sq, piece, gone) in pieces {
            if gone {
                if matches!(cls.get(&sq), Some((Cls::U, _))) { st.cleared += 1; } else { st.replaced += 1; }
                continue;
            }
            if is_top { edge_pts.entry(sq).or_default().extend(piece.iter().cloned()); }
            push_poly(&mut out, &piece, soup.m[k]);
        }
    }
    // (5) the zone's polygons: corners, the kept deck's vertices on their edges, fanned from the centre
    for &(i, j) in &zkeys {
        let (x0, y0, x1, y1) = (i as f64 * s, j as f64 * s, (i + 1) as f64 * s, (j + 1) as f64 * s);
        let cz = |a: i64, b: i64| corner_z[&(a, b)];
        let corners = [((i, j), [x0, y0]), ((i + 1, j), [x1, y0]), ((i + 1, j + 1), [x1, y1]), ((i, j + 1), [x0, y1])];
        // neighbours across edge 0 (south), 1 (east), 2 (north), 3 (west)
        let nb = [(i, j - 1), (i + 1, j), (i, j + 1), (i - 1, j)];
        let mut ring: Vec<[f64; 3]> = Vec::new();
        for e in 0..4 {
            let (ca, pa) = corners[e];
            let (cb, pb) = corners[(e + 1) % 4];
            let (za, zb) = (cz(ca.0, ca.1), cz(cb.0, cb.1));
            ring.push([pa[0], pa[1], za]);
            let kept = match cls.get(&nb[e]) { None | Some((Cls::Keep, _)) => true, _ => false };
            if !kept { continue; }
            let Some(vs) = edge_pts.get(&nb[e]) else { continue };
            let (axis, c) = if e % 2 == 0 { (1usize, pa[1]) } else { (0usize, pa[0]) };
            let other = 1 - axis;
            let (lo, hi) = if pa[other] < pb[other] { (pa[other], pb[other]) } else { (pb[other], pa[other]) };
            let zlo = za.min(zb) - 0.6;
            let zhi = za.max(zb) + 0.6;
            let mut on: Vec<[f64; 3]> = vs.iter().map(|v| v.0)
                .filter(|v| v[axis] == c && v[other] > lo + 1e-9 && v[other] < hi - 1e-9 && v[2] >= zlo && v[2] <= zhi).collect();
            let fwd = pb[other] > pa[other];
            on.sort_by(|p, q| if fwd { p[other].partial_cmp(&q[other]).unwrap() } else { q[other].partial_cmp(&p[other]).unwrap() });
            on.dedup_by(|p, q| p[other] == q[other]);
            ring.extend(on);
        }
        let mut c = [0.0; 3];
        for &(cc, pp) in &corners { c[0] += pp[0]; c[1] += pp[1]; c[2] += cz(cc.0, cc.1); }
        let c = [c[0] / 4.0, c[1] / 4.0, c[2] / 4.0];
        let base = out.p.len() as u32;
        out.p.push(c);
        out.uv.push([c[0] / tile_uv, c[1] / tile_uv]);
        for v in &ring { out.p.push(*v); out.uv.push([v[0] / tile_uv, v[1] / tile_uv]); }
        let n = ring.len() as u32;
        for kk in 0..n {
            out.t.push([base, base + 1 + kk, base + 1 + (kk + 1) % n]);
            out.m.push(top);
            st.zone_tris += 1;
        }
    }
    for c in cls.values() { match c.0 { Cls::U => st.squares_u += 1, Cls::Z => st.squares_z += 1, Cls::Keep => {} } }
    st.squares_h = hand.len();
    (out, st)
}


// ------------------------------------------------------------------------------------------------ probes' input files

/// (627) The skew's own inputs (Ends::new_skewed): the other lines that may pass under a structure with their kinds,
/// per bridge part the inventory's record.
#[derive(Clone, Debug, Default)]
pub struct SkewIn { pub crossed: Vec<(Vec<P2>, u8)>, pub nbi: Vec<Option<[f64; 4]>>, pub noskew: Vec<bool> }

impl Ends {
    /// Ends::new, or new_skewed with sk's lines and records.
    pub fn build(bridges: &[Part], grounds: Vec<Part>, tunnels: &[Vec<P2>], s: f64, sk: Option<&SkewIn>) -> Ends {
        match sk {
            None => Ends::new(bridges, grounds, tunnels, s),
            Some(k) => Ends::new_skewed(bridges, grounds, tunnels, s, Some((&k.crossed, &k.nbi, &k.noskew))),
        }
    }
}

/// A zone's whole input as bytes ("RSHOVER1": the parts the ends are made of, the lattice, the soup; "RSHOVER2" (627)
/// with the skew's inputs), so the same build can be run again by the CLI (`roadsys handover IN`) on another platform
/// and its bytes compared.
#[allow(clippy::too_many_arguments)]
pub fn encode_input(bridges: &[Part], grounds: &[Part], tunnels: &[Vec<P2>], soup: &Soup, top: u32, g: &Grid, tile_uv: f64,
                    sk: Option<&SkewIn>) -> Vec<u8> {
    let mut b: Vec<u8> = if sk.is_some() { b"RSHOVER2".to_vec() } else { b"RSHOVER1".to_vec() };
    let f = |b: &mut Vec<u8>, v: f64| b.extend_from_slice(&v.to_le_bytes());
    let u = |b: &mut Vec<u8>, v: u32| b.extend_from_slice(&v.to_le_bytes());
    f(&mut b, g.s);
    for parts in [bridges, grounds] {
        u(&mut b, parts.len() as u32);
        for p in parts { f(&mut b, p.hw); u(&mut b, p.pts.len() as u32); for q in &p.pts { f(&mut b, q[0]); f(&mut b, q[1]); } }
    }
    u(&mut b, tunnels.len() as u32);
    for t in tunnels { u(&mut b, t.len() as u32); for q in t { f(&mut b, q[0]); f(&mut b, q[1]); } }
    u(&mut b, soup.p.len() as u32);
    for (p, t) in soup.p.iter().zip(soup.uv.iter()) { for c in p { f(&mut b, *c); } f(&mut b, t[0]); f(&mut b, t[1]); }
    u(&mut b, soup.t.len() as u32);
    for (t, m) in soup.t.iter().zip(soup.m.iter()) { for c in t { u(&mut b, *c); } u(&mut b, *m); }
    u(&mut b, top);
    b.extend_from_slice(&g.i0.to_le_bytes());
    b.extend_from_slice(&g.j0.to_le_bytes());
    u(&mut b, g.nx as u32);
    u(&mut b, g.ny as u32);
    for v in &g.z { f(&mut b, *v); }
    for v in &g.drop { f(&mut b, *v); }
    f(&mut b, tile_uv);
    if let Some(k) = sk {
        u(&mut b, k.crossed.len() as u32);
        for (q, kind) in &k.crossed { u(&mut b, *kind as u32); u(&mut b, q.len() as u32); for p in q { f(&mut b, p[0]); f(&mut b, p[1]); } }
        u(&mut b, k.nbi.len() as u32);
        for n in &k.nbi {
            match n { None => u(&mut b, 0), Some(v) => { u(&mut b, 1); for c in v { f(&mut b, *c); } } }
        }
        u(&mut b, k.noskew.len() as u32);
        for &x in &k.noskew { u(&mut b, x as u32); }
    }
    b
}

/// The bytes of a soup (positions, uvs, triangles, materials) and of drops, as compared across platforms.
pub fn output_bytes(drops: &[f64], soup: &Soup) -> Vec<u8> {
    let mut b = Vec::new();
    for v in drops { b.extend_from_slice(&v.to_le_bytes()); }
    for p in &soup.p { for c in p { b.extend_from_slice(&c.to_le_bytes()); } }
    for t in &soup.uv { for c in t { b.extend_from_slice(&c.to_le_bytes()); } }
    for t in &soup.t { for c in t { b.extend_from_slice(&c.to_le_bytes()); } }
    for m in &soup.m { b.extend_from_slice(&m.to_le_bytes()); }
    b
}

/// Run an input file: (the drops at every lattice node, the zone's soup, its stats).
pub fn run_input(b: &[u8]) -> Result<(Vec<f64>, Soup, ZoneStats), String> {
    struct R<'a> { b: &'a [u8], i: usize }
    impl R<'_> {
        fn take(&mut self, n: usize) -> Result<&[u8], String> {
            if self.i + n > self.b.len() { return Err("handover input: truncated".into()); }
            let s = &self.b[self.i..self.i + n]; self.i += n; Ok(s)
        }
        fn u(&mut self) -> Result<u32, String> { Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap())) }
        fn i(&mut self) -> Result<i64, String> { Ok(i64::from_le_bytes(self.take(8)?.try_into().unwrap())) }
        fn f(&mut self) -> Result<f64, String> { Ok(f64::from_le_bytes(self.take(8)?.try_into().unwrap())) }
    }
    let mut r = R { b, i: 0 };
    let tag = r.take(8)?.to_vec();
    if tag != b"RSHOVER1" && tag != b"RSHOVER2" { return Err("handover input: bad magic".into()); }
    let s = r.f()?;
    let mut lists: Vec<Vec<Part>> = Vec::new();
    for _ in 0..2 {
        let n = r.u()? as usize;
        let mut v = Vec::with_capacity(n.min(1 << 20));
        for _ in 0..n {
            let hw = r.f()?;
            let k = r.u()? as usize;
            let mut pts = Vec::with_capacity(k.min(1 << 20));
            for _ in 0..k { pts.push([r.f()?, r.f()?]); }
            v.push(Part { pts, hw });
        }
        lists.push(v);
    }
    let nt = r.u()? as usize;
    let mut tunnels = Vec::new();
    for _ in 0..nt { let k = r.u()? as usize; let mut pts = Vec::new(); for _ in 0..k { pts.push([r.f()?, r.f()?]); } tunnels.push(pts); }
    let mut soup = Soup::default();
    let nv = r.u()? as usize;
    for _ in 0..nv { soup.p.push([r.f()?, r.f()?, r.f()?]); soup.uv.push([r.f()?, r.f()?]); }
    let ntr = r.u()? as usize;
    for _ in 0..ntr {
        let t = [r.u()?, r.u()?, r.u()?];
        if t.iter().any(|&v| v as usize >= nv) { return Err("handover input: index out of range".into()); }
        soup.t.push(t); soup.m.push(r.u()?);
    }
    let top = r.u()?;
    let (i0, j0) = (r.i()?, r.i()?);
    let (nx, ny) = (r.u()? as usize, r.u()? as usize);
    let mut g = Grid { i0, j0, nx, ny, s, z: Vec::new(), drop: Vec::new() };
    for _ in 0..nx * ny { g.z.push(r.f()?); }
    for _ in 0..nx * ny { g.drop.push(r.f()?); }
    let tile_uv = r.f()?;
    let mut sk: Option<SkewIn> = None;
    if tag == b"RSHOVER2" {
        let mut k = SkewIn::default();
        let nc = r.u()? as usize;
        for _ in 0..nc {
            let kind = r.u()? as u8;
            let n = r.u()? as usize;
            let mut q = Vec::with_capacity(n.min(1 << 20));
            for _ in 0..n { q.push([r.f()?, r.f()?]); }
            k.crossed.push((q, kind));
        }
        let nn = r.u()? as usize;
        for _ in 0..nn { k.nbi.push(if r.u()? == 1 { Some([r.f()?, r.f()?, r.f()?, r.f()?]) } else { None }); }
        let nk = r.u()? as usize;
        for _ in 0..nk { k.noskew.push(r.u()? == 1); }
        sk = Some(k);
    }
    let grounds = lists.pop().unwrap();
    let bridges = lists.pop().unwrap();
    let ends = Ends::build(&bridges, grounds, &tunnels, s, sk.as_ref());
    let mut drops = Vec::with_capacity(nx * ny);
    for b in 0..ny { for a in 0..nx { let q = g.xy(i0 + a as i64, j0 + b as i64); drops.push(ends.drop_at(q[0], q[1])); } }
    let (out, st) = zone(&soup, top, &ends, &g, tile_uv);
    Ok((drops, out, st))
}

#[cfg(test)]
mod tests {

    #[test]
    fn a_junction_at_a_short_approachs_far_end_is_the_ends_own() {
        // a deck from x = 0 west to -40, a 3 m stub east of its end into a ring road whose surface (half width 5)
        // reaches over the deck's first metres: dropped there as at a plain end; a road crossing under it is not
        let deck = Part { pts: vec![[0.0, 0.0], [-40.0, 0.0]], hw: 5.0 };
        let stub = Part { pts: vec![[0.0, 0.0], [3.0, 0.0]], hw: 4.0 };
        let ring = Part { pts: vec![[3.0, 0.0], [3.0, 20.0]], hw: 5.0 };
        let e = Ends::new(&[deck.clone()], vec![stub.clone(), ring.clone()], &[], 1.0);
        assert_eq!(e.ends.len(), 1);
        assert!(e.drop_at(-2.0, 1.0) > 0.0, "the ring at the end is the end's junction");
        // (a long approach: the roads at its far end are no junction of the end's)
        let long = Part { pts: vec![[0.0, 0.0], [30.0, 0.0]], hw: 4.0 };
        let ring2 = Part { pts: vec![[-3.0, -20.0], [-3.0, 20.0]], hw: 5.0 };
        let f = Ends::new(&[deck], vec![long, ring2], &[], 1.0);
        assert_eq!(f.drop_at(-2.0, 1.0), 0.0, "a road under the deck keeps its ground");
    }

    use super::*;

    fn flat_grid(i0: i64, j0: i64, n: usize, s: f64, z: f64, ends: &Ends) -> Grid {
        let mut g = Grid { i0, j0, nx: n, ny: n, s, z: vec![0.0; n * n], drop: vec![0.0; n * n] };
        for b in 0..n { for a in 0..n {
            let (x, y) = ((i0 + a as i64) as f64 * s, (j0 + b as i64) as f64 * s);
            let d = ends.drop_at(x, y);
            g.drop[b * n + a] = d;
            g.z[b * n + a] = z - d;
        } }
        g
    }

    fn deck_soup(x0: f64, x1: f64, hw: f64, z: f64) -> Soup {
        // a flat deck top from x0 to x1 (|y| <= hw), split into 2 m rows, material 1
        let mut s = Soup::default();
        let mut x = x0;
        while x < x1 - 1e-9 {
            let xn = (x + 2.0).min(x1);
            let b = s.p.len() as u32;
            for p in [[x, -hw, z], [xn, -hw, z], [xn, hw, z], [x, hw, z]] { s.p.push(p); s.uv.push([0.0, 0.0]); }
            s.t.push([b, b + 1, b + 2]); s.t.push([b, b + 2, b + 3]); s.m.push(1); s.m.push(1);
            x = xn;
        }
        s
    }

    fn road_ends() -> Ends {
        // a deck from x = 0.3 to 60 along y = 0.2, its approach on the ground before it
        let b = Part { pts: vec![[0.3, 0.2], [60.0, 0.2]], hw: 4.0 };
        let gnd = Part { pts: vec![[-40.0, 0.2], [0.3, 0.2]], hw: 4.0 };
        Ends::new(&[b], vec![gnd], &[], 1.0)
    }

    /// (627) A 40 m overpass over a road at 60 degrees ends along it: the drop starts D0 past the skewed end line on
    /// either side (0.577 m along per m across); a waterway crossed the same way skews it too; a viaduct's is square.
    #[test]
    fn a_skewed_end_drops_from_its_own_line() {
        let b = Part { pts: vec![[0.3, 0.2], [40.3, 0.2]], hw: 4.0 };
        let gnd = Part { pts: vec![[-40.0, 0.2], [0.3, 0.2]], hw: 4.0 };
        let road = Part { pts: vec![[20.3 - 25.0, 0.2 - 43.30127018922193], [20.3 + 25.0, 0.2 + 43.30127018922193]], hw: 3.5 };
        let none: Vec<(Vec<P2>, u8)> = Vec::new();
        let e = Ends::new_skewed(&[b.clone()], vec![gnd.clone(), road.clone()], &[], 1.0, Some((&none, &[], &[])));
        let k = e.ends.iter().position(|x| dist(x.p, [0.3, 0.2]) < 1e-9).unwrap();
        assert!((e.ends[k].sk - 0.5773502691896257).abs() < 1e-9, "{:?}", e.ends[k].sk);
        assert_eq!(e.drop_at(3.0, 3.2), 0.0);           // (left: the line 1.73 m in)
        assert!(e.drop_at(4.0, 3.2) > 0.0);
        assert!(e.drop_at(0.5, -2.8) > 0.0);            // (right: the line 1.73 m back)
        let sq = Ends::new(&[b.clone()], vec![gnd.clone(), road.clone()], &[], 1.0);
        assert_eq!(sq.drop_at(0.5, -2.8), 0.0);
        let water = vec![(road.pts.clone(), crate::skew::KIND_WATER)];
        let w = Ends::new_skewed(&[b.clone()], vec![gnd.clone()], &[], 1.0, Some((&water, &[], &[])));
        assert!(w.ends.iter().any(|x| x.sk != 0.0));
        // a side road along the end line 2 m before it (the tip would lie on it): the skew eased until clear, or square
        let side = Part { pts: vec![[-2.0 - 25.0, 0.2 - 43.30127018922193], [-2.0 + 25.0, 0.2 + 43.30127018922193]], hw: 2.0 };
        let t = Ends::new_skewed(&[b.clone()], vec![gnd.clone(), road.clone(), side], &[], 1.0, Some((&none, &[], &[])));
        let kt = t.ends.iter().find(|x| dist(x.p, [0.3, 0.2]) < 1e-9).unwrap().sk;
        assert!(kt.abs() < 0.5773502691896257 * 0.75 + 1e-9, "{kt}");
        // an approach whose surface cannot be cut along the line (noskew): square
        let ns = Ends::new_skewed(&[b.clone()], vec![gnd.clone(), road.clone()], &[], 1.0, Some((&none, &[], &[true, false])));
        assert!(ns.ends.iter().all(|x| x.sk == 0.0));
        let more = Part { pts: vec![[40.3, 0.2], [140.3, 0.2]], hw: 4.0 };
        let v = Ends::new_skewed(&[b, more], vec![gnd, road], &[], 1.0, Some((&none, &[], &[])));
        assert!(v.ends.iter().all(|x| x.sk == 0.0));
    }

    #[test]
    fn ground_ends_only() {
        let e = road_ends();
        assert_eq!(e.ends.len(), 1, "the far end has no approach");
        assert!((e.ends[0].p[0] - 0.3).abs() < 1e-12);
        // dropped past 1.5 m, full to 10 m, gone past 18 m, not beside it
        assert_eq!(e.drop_at(1.0, 0.0), 0.0);
        assert_eq!(e.drop_at(3.0, 0.0), DROP_M);
        assert_eq!(e.drop_at(3.0, 4.0), DROP_M);
        assert_eq!(e.drop_at(3.0, 4.5), 0.0);
        assert!(e.drop_at(6.8, 0.2) > 0.0 && e.drop_at(6.8, 0.2) < DROP_M);
        assert_eq!(e.drop_at(9.0, 0.0), 0.0);
        // a road crossing under it keeps its ground
        let b = Part { pts: vec![[0.3, 0.2], [60.0, 0.2]], hw: 4.0 };
        let gnd = Part { pts: vec![[-40.0, 0.2], [0.3, 0.2]], hw: 4.0 };
        let cross = Part { pts: vec![[1.0, -30.0], [1.0, 30.0]], hw: 1.0 };
        let e2 = Ends::new(&[b.clone()], vec![gnd.clone(), cross], &[], 1.0);
        assert_eq!(e2.drop_at(2.0, 0.0), 0.0);
        assert_eq!(e2.drop_at(4.0, 0.0), DROP_M);
        // a copy of the deck from the same end (a road read whole and as its sections) keeps the end
        let sec = Part { pts: vec![[0.3, 0.2], [30.0, 0.2]], hw: 4.0 };
        let e4 = Ends::new(&[b.clone(), sec], vec![gnd.clone()], &[], 1.0);
        assert!(e4.ends.len() >= 1 && e4.drop_at(3.0, 0.0) == DROP_M);
        // an end another deck goes on from is no ground end
        let b2 = Part { pts: vec![[-20.0, 0.2], [0.3, 0.2]], hw: 4.0 };
        let e3 = Ends::new(&[b, b2], vec![gnd], &[], 1.0);
        assert_eq!(e3.ends.len(), 0);
    }

    #[test]
    fn the_zone_meets_the_ground_on_its_own_nodes() {
        let e = road_ends();
        let g = flat_grid(-45, -20, 90, 1.0, 10.0, &e);
        // the deck 3 cm over the ground at its end (a lip today), on from x = 0.3; and a slab 5 m past it, at ground
        let mut soup = deck_soup(0.3, 40.0, 4.45, 10.03);
        let past = deck_soup(-5.0, 0.3, 4.45, 10.02);
        let b = soup.p.len() as u32;
        soup.p.extend(past.p); soup.uv.extend(past.uv);
        for t in past.t { soup.t.push([t[0] + b, t[1] + b, t[2] + b]); }
        soup.m.extend(past.m);
        let (out, st) = zone(&soup, 1, &e, &g, 4.0);
        assert!(st.squares_z > 0 && st.squares_u > 0, "{:?}", st);
        // nothing of the deck left before its end at ground level
        for t in &out.t {
            for &v in t { let p = out.p[v as usize]; assert!(!(p[0] < 0.0 && p[2] < 10.3), "{:?}", p); }
        }
        // every free edge of the top in the lanes: on the ground's own height where it meets the ground (0 mm)
        let mut ec: BTreeMap<((i64, i64, i64), (i64, i64, i64)), (u32, [f64; 3], [f64; 3])> = BTreeMap::new();
        let q = |v: [f64; 3]| ((v[0] * 1000.0).round() as i64, (v[1] * 1000.0).round() as i64, (v[2] * 1000.0).round() as i64);
        for t in &out.t {
            for k in 0..3 {
                let (a, b) = (out.p[t[k] as usize], out.p[t[(k + 1) % 3] as usize]);
                let (ka, kb) = (q(a), q(b));
                if ka == kb { continue; }
                let key = if ka < kb { (ka, kb) } else { (kb, ka) };
                ec.entry(key).or_insert((0, a, b)).0 += 1;
            }
        }
        let mut free = 0;
        for (_k, (n, a, b)) in &ec {
            if *n != 1 { continue; }
            let m = [(a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0, (a[2] + b[2]) / 2.0];
            if m[1].abs() > 3.0 || m[0] > 3.0 { continue; }   // (the hand-over; the split's own seams farther on are T-joints)
            free += 1;
            // the end's free edges lie on lattice edges at the ground's height
            assert!((m[2] - 10.0).abs() < 1e-9, "a lip at {:?}: {} mm", m, (m[2] - 10.0) * 1000.0);
            assert!(a[0] == a[0].round() || a[1] == a[1].round());
        }
        assert!(free > 0);
        // the deck's height kept past the blend; the mismatch faded in between
        let mut zmax_far: f64 = 0.0;
        for p in &out.p { if p[0] > 9.0 && p[0] < 30.0 && p[1].abs() < 3.0 { zmax_far = zmax_far.max((p[2] - 10.03).abs()); } }
        assert!(zmax_far < 1e-9, "{}", zmax_far);
        // deterministic
        let (out2, _) = zone(&soup, 1, &e, &g, 4.0);
        assert_eq!(out.p.len(), out2.p.len());
        assert!(out.p.iter().zip(out2.p.iter()).all(|(a, b)| a == b));
    }

    #[test]
    fn a_deck_beside_the_end_is_untouched() {
        let e = road_ends();
        let g = flat_grid(-45, -40, 110, 1.0, 10.0, &e);
        let mut soup = deck_soup(0.3, 40.0, 4.45, 10.01);
        // a through deck 10 m beside it at the same level, running past the end line
        let mut other = deck_soup(-30.0, 40.0, 4.45, 10.01);
        for p in other.p.iter_mut() { p[1] += 10.5; }
        let b = soup.p.len() as u32;
        soup.p.extend(other.p.iter().cloned()); soup.uv.extend(other.uv.iter().cloned());
        for t in &other.t { soup.t.push([t[0] + b, t[1] + b, t[2] + b]); }
        soup.m.extend(other.m.iter().cloned());
        let (out, st) = zone(&soup, 1, &e, &g, 4.0);
        assert!(st.squares_h > 0, "{:?}", st);
        // the other deck's top area unchanged
        let area = |s: &Soup, f: &dyn Fn([f64; 3]) -> bool| -> f64 { s.t.iter().filter(|t| f(s.p[t[0] as usize]) && f(s.p[t[1] as usize]) && f(s.p[t[2] as usize]))
            .map(|t| n3(normal3(s.p[t[0] as usize], s.p[t[1] as usize], s.p[t[2] as usize])) / 2.0).sum() };
        let beside = |p: [f64; 3]| p[1] > 5.9;
        assert!((area(&other, &beside) - area(&out, &beside)).abs() < 1e-6, "{} {}", area(&other, &beside), area(&out, &beside));
    }

    #[test]
    fn the_same_bytes_whatever_the_parts_order_and_through_a_file() {
        let b = vec![Part { pts: vec![[0.3, 0.2], [60.0, 0.2]], hw: 4.0 }, Part { pts: vec![[60.0, 0.2], [90.0, 0.2]], hw: 4.0 }];
        let gr = vec![Part { pts: vec![[-40.0, 0.2], [0.3, 0.2]], hw: 4.0 }, Part { pts: vec![[4.0, -30.0], [4.0, 30.0]], hw: 0.4 }];
        let e = Ends::new(&b, gr.clone(), &[], 1.0);
        let g = flat_grid(-45, -20, 90, 1.0, 10.0, &e);
        let soup = deck_soup(0.3, 40.0, 4.45, 10.03);
        let one = encode_input(&b, &gr, &[], &soup, 1, &g, 4.0, None);
        let (mut br, mut grr) = (b.clone(), gr.clone());
        br.reverse(); grr.reverse();
        br.push(b[0].clone());       // (a copy read twice)
        let two = encode_input(&br, &grr, &[], &soup, 1, &g, 4.0, None);
        let (d1, o1, _) = run_input(&one).unwrap();
        let (d2, o2, _) = run_input(&two).unwrap();
        assert_eq!(output_bytes(&d1, &o1), output_bytes(&d2, &o2));
        let (o3, _) = zone(&soup, 1, &e, &g, 4.0);
        assert_eq!(output_bytes(&[], &o1), output_bytes(&[], &o3));
    }

    #[test]
    fn a_deck_high_over_its_ground_is_kept() {
        let e = road_ends();
        let g = flat_grid(-45, -20, 90, 1.0, 10.0, &e);
        let soup = deck_soup(0.3, 40.0, 4.45, 11.5);
        let (out, st) = zone(&soup, 1, &e, &g, 4.0);
        assert_eq!(st.squares_h, 0, "{:?}", st);
        let a0: f64 = soup.t.iter().map(|t| { let n = normal3(soup.p[t[0] as usize], soup.p[t[1] as usize], soup.p[t[2] as usize]); n3(n) }).sum();
        let a1: f64 = out.t.iter().map(|t| { let n = normal3(out.p[t[0] as usize], out.p[t[1] as usize], out.p[t[2] as usize]); n3(n) }).sum();
        assert!((a0 - a1).abs() < 1e-6, "{} {}", a0, a1);
    }
}
