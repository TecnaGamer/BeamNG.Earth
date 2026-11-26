//! M5 (BNE_ROADSYS=surface; bridgerewrite/DESIGN.md G5 / D5): the ground under a deck at most at its soffit less
//! CLEAR_UNDER_M. A pure function of the hand-over's ends (handover.rs `Ends`: its drop, its skewed end lines and its
//! ground roads) and the decks round a place as the bridge chains published them (each part's line in the frame, its
//! top's heights per vertex, its half width and its structure's drawn depth), so every evaluator of the physics ground
//! (the terrain blocks, the skins, the content's drawn ground, the hand-over's lattice) lowers a node by its own
//! position alone, whoever asks: `Ceil::ground` = min(the ground less the hand-over's drop, the ceiling there).
//!
//! The ceiling under a deck part at arc t: its top there less `need`, need = the depth + CLEAR_UNDER_M, eased in from a
//! free end (an end no other deck goes on from): none for the first D0_SQUARES lattice squares (the hand-over's own,
//! where the deck meets its ground on the terrain's nodes), then the hand-over's DROP_M up to FULL_M, then deeper by
//! RAMP per m (a 1:2 abutment slope) until the whole need. A node on another ground road's surface (a road under the
//! deck, a twin beside it) or on a railway keeps its ground, as the drop's does, and the lattice square round
//! it; farther the cut eases in at 1:2 (SIDE_RAMP). Low decks only: a deck high over its
//! ground has its ceiling far over it.
//!
//! IEEE + - * / and sqrt only, BTreeMap / sorted Vec, so the same bytes on every platform.
use crate::geom::*;
use crate::handover::{Ends, Part, SkewIn, CLEAR_M, D0_SQUARES, DROP_M, FULL_M, JOIN_M};
use std::collections::{BTreeMap, BTreeSet};

pub const CLEAR_UNDER_M: f64 = 0.3;  // m under the soffit the ground stays at least (G5)
pub const RAMP: f64 = 0.5;           // m of need per m along the deck past FULL_M from a free end (1:2)
pub const COVER_M: f64 = 0.5;        // a deck end within another deck's half width + this goes on onto it (not free)
pub const RAIL_HW_M: f64 = 4.0;      // a railway's bed (double track and its ballast shoulders): its line + this keeps its ground
pub const KEEP_PAST_M: f64 = 1.0;    // m past a ground road's surface (half width + CLEAR_M) + one lattice square its
                                     // ground is kept whole (its drawn surface wider than the plain width, the
                                     // lattice's squares round it flat: its mesh, draped node by node, is never under
                                     // the terrain's interpolation) ...
pub const SIDE_RAMP: f64 = 0.5;      // ... then cut at most this per m farther (a 1:2 slope down under the deck)
pub const CELL_M: f64 = 8.0;         // the segments' index square
pub const ALLOW_CAP_M: f64 = 8.3;    // (M5 r5) a ground road or railway limits a deck's cut only within the deck's own
                                     // need (its depth + CLEAR_UNDER_M, at most this: the deepest structure's, 8 m +
                                     // 0.3) of its keep (its distance past it * SIDE_RAMP), the reach the cell's deepest
                                     // deck's need gave before: so a node's value is its own decks' whatever other decks
                                     // a caller read

/// One deck part: its line in the frame, its top's height per vertex (the frame's own heights as the evaluators'
/// ground has them), its half width and its structure's drawn depth.
#[derive(Clone, Debug)]
pub struct Deck { pub pts: Vec<P2>, pub h: Vec<f64>, pub hw: f64, pub depth: f64 }

#[derive(Clone, Debug)]
struct DeckC {
    line: Line,
    h: Vec<f64>,
    hw: f64,
    depth: f64,
    free: [bool; 2],
    end_of: [Option<usize>; 2],     // the hand-over's end at each end (its skewed end line), when one lies there
    skip: BTreeSet<usize>,          // the ground parts that are its approaches (their surface goes on from its ends)
}

#[derive(Clone, Debug, Default)]
pub struct Ceil {
    pub ends: Ends,
    decks: Vec<DeckC>,
    rails: Vec<Vec<P2>>,
    seg_grid: BTreeMap<(i64, i64), Vec<(u32, u32)>>,       // (deck, segment) by index square
    gnd_grid: BTreeMap<(i64, i64), Vec<(u32, u32, u8)>>,   // (part, segment, 0 ground / 1 rail) by index square
}

fn cell_of(v: f64) -> i64 { (v / CELL_M).floor() as i64 }

fn seg_box(a: P2, b: P2, grow: f64) -> [f64; 4] {
    [a[0].min(b[0]) - grow, a[1].min(b[1]) - grow, a[0].max(b[0]) + grow, a[1].max(b[1]) + grow]
}

fn put<T: Clone>(g: &mut BTreeMap<(i64, i64), Vec<T>>, bb: [f64; 4], v: T) {
    for j in cell_of(bb[1])..=cell_of(bb[3]) {
        for i in cell_of(bb[0])..=cell_of(bb[2]) { g.entry((i, j)).or_default().push(v.clone()); }
    }
}

/// (distance, clamped u, raw u) of q to segment ab.
fn seg_near(q: P2, a: P2, b: P2) -> (f64, f64, f64) {
    let ab = sub(b, a);
    let l2 = dot(ab, ab);
    let raw = if l2 > 0.0 { dot(sub(q, a), ab) / l2 } else { 0.0 };
    let u = clampf(raw, 0.0, 1.0);
    (norm(sub(q, lerp(a, b, u))), u, raw)
}

fn line_dist(q: P2, p: &[P2]) -> f64 {
    let mut best = f64::INFINITY;
    for w in p.windows(2) { best = best.min(seg_near(q, w[0], w[1]).0); }
    best
}

impl Ceil {
    /// The ceiling of the decks round a place, with its hand-over's ends (their drop, skewed end lines and ground
    /// roads) and the railways there. Each deck once (a part read from two cells), in a canonical order.
    pub fn new(ends: Ends, decks: &[Deck], rails: &[Vec<P2>]) -> Ceil {
        // (each part once, in its canonical direction (the smaller of its key and its reversed key), in that order)
        let mut canon: BTreeMap<(Vec<I2>, Vec<i64>), Deck> = BTreeMap::new();
        for d in decks {
            if d.pts.len() < 2 || d.h.len() != d.pts.len() || !(d.hw > 0.0) || !d.depth.is_finite() || d.depth < 0.0
                || d.pts.iter().any(|q| !q.iter().all(|v| v.is_finite())) || d.h.iter().any(|v| !v.is_finite()) { continue; }
            let key_of = |p: &[P2], h: &[f64]| -> (Vec<I2>, Vec<i64>) {
                (p.iter().map(|&q| mm2(q)).collect(), h.iter().map(|&v| mm(v)).chain([mm(d.hw), mm(d.depth)]).collect())
            };
            let k = key_of(&d.pts, &d.h);
            let mut r = d.clone();
            r.pts.reverse();
            r.h.reverse();
            let rk = key_of(&r.pts, &r.h);
            if rk < k { canon.entry(rk).or_insert(r); } else { canon.entry(k).or_insert_with(|| d.clone()); }
        }
        let ds: Vec<(Vec<I2>, &Deck)> = canon.iter().map(|(k, d)| (k.0.clone(), d)).collect();
        let mut out: Vec<DeckC> = ds.iter().map(|(_k, d)| DeckC {
            line: Line::new(d.pts.clone()), h: d.h.clone(), hw: d.hw, depth: d.depth, free: [true, true],
            end_of: [None, None], skip: BTreeSet::new(),
        }).collect();
        // (an end on another deck's band goes on onto it: not free)
        for i in 0..out.len() {
            for side in 0..2 {
                let p = if side == 0 { out[i].line.p[0] } else { *out[i].line.p.last().unwrap() };
                let covered = (0..out.len()).any(|j| j != i && line_dist(p, &out[j].line.p) <= out[j].hw + COVER_M);
                out[i].free[side] = !covered;
                // the hand-over's end there (its skew; its approaches)
                let mut best: Option<(f64, usize)> = None;
                for (k, e) in ends.ends.iter().enumerate() {
                    let dd = norm(sub(e.p, p));
                    if dd < JOIN_M && best.map_or(true, |b| dd < b.0) { best = Some((dd, k)); }
                }
                out[i].end_of[side] = best.map(|b| b.1);
                if let Some((_, k)) = best {
                    out[i].skip.extend(ends.ends[k].approach.iter().copied());
                    out[i].skip.extend(ends.ends[k].junction.iter().copied());
                }
                for (gi, g) in ends.grounds.iter().enumerate() {
                    if norm(sub(g.pts[0], p)) < JOIN_M || norm(sub(*g.pts.last().unwrap(), p)) < JOIN_M { out[i].skip.insert(gi); }
                }
            }
        }
        let mut seg_grid: BTreeMap<(i64, i64), Vec<(u32, u32)>> = BTreeMap::new();
        let mut all = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for (di, d) in out.iter().enumerate() {
            for s in 0..d.line.p.len() - 1 {
                let bb = seg_box(d.line.p[s], d.line.p[s + 1], d.hw + 0.01);
                all = [all[0].min(bb[0]), all[1].min(bb[1]), all[2].max(bb[2]), all[3].max(bb[3])];
                put(&mut seg_grid, bb, (di as u32, s as u32));
            }
        }
        // the ground roads' and railways' segments near the decks (the only ones a ceiling asks about)
        let mut rl: Vec<Vec<P2>> = rails.iter().filter(|r| r.len() >= 2 && r.iter().all(|q| q.iter().all(|v| v.is_finite())))
            .cloned().collect();
        rl.sort_by(|a, b| a.iter().map(|&q| mm2(q)).cmp(b.iter().map(|&q| mm2(q))));
        rl.dedup_by(|a, b| a.iter().map(|&q| mm2(q)).eq(b.iter().map(|&q| mm2(q))));
        let mut gnd_grid: BTreeMap<(i64, i64), Vec<(u32, u32, u8)>> = BTreeMap::new();
        let near = |bb: &[f64; 4]| bb[2] >= all[0] && bb[0] <= all[2] && bb[3] >= all[1] && bb[1] <= all[3];
        // (M5 r5: a fixed reach, not the read's deepest deck's: every ground part allow() may count at a node lies in
        // the node's index square whichever decks were read; allow() cuts them off at the deck's own need exactly)
        let reach = KEEP_PAST_M + ends.s + ALLOW_CAP_M / SIDE_RAMP + 0.01;
        for (gi, g) in ends.grounds.iter().enumerate() {
            for s in 0..g.pts.len().saturating_sub(1) {
                let bb = seg_box(g.pts[s], g.pts[s + 1], g.hw + CLEAR_M + reach);
                if near(&bb) { put(&mut gnd_grid, bb, (gi as u32, s as u32, 0u8)); }
            }
        }
        for (ri, r) in rl.iter().enumerate() {
            for s in 0..r.len() - 1 {
                let bb = seg_box(r[s], r[s + 1], RAIL_HW_M + reach);
                if near(&bb) { put(&mut gnd_grid, bb, (ri as u32, s as u32, 1u8)); }
            }
        }
        Ceil { ends, decks: out, rails: rl, seg_grid, gnd_grid }
    }

    pub fn count(&self) -> usize { self.decks.len() }

    /// m deck `di`'s ceiling may cut the ground at q by beside the ground roads (other than its approaches) and the
    /// railways round it: none within their surface (half width + CLEAR_M, a railway's bed RAIL_HW_M) + KEEP_PAST_M +
    /// one lattice square, then SIDE_RAMP per m up to the deck's own need (f64::INFINITY: none nearer; M5 r5).
    fn allow(&self, di: usize, q: P2) -> f64 {
        let Some(v) = self.gnd_grid.get(&(cell_of(q[0]), cell_of(q[1]))) else { return f64::INFINITY };
        let keep = KEEP_PAST_M + self.ends.s;
        let cap = (self.decks[di].depth + CLEAR_UNDER_M).min(ALLOW_CAP_M);
        let mut best = f64::INFINITY;
        for &(pi, s, kind) in v {
            let (pi, s) = (pi as usize, s as usize);
            let past = if kind == 0 {
                if self.decks[di].skip.contains(&pi) { continue; }
                let g: &Part = &self.ends.grounds[pi];
                seg_near(q, g.pts[s], g.pts[s + 1]).0 - (g.hw + CLEAR_M + keep)
            } else {
                let r = &self.rails[pi];
                seg_near(q, r[s], r[s + 1]).0 - (RAIL_HW_M + keep)
            };
            let a = if past > 0.0 { past * SIDE_RAMP } else { 0.0 };
            if a > cap { continue; }
            if a < best { best = a; }
        }
        best
    }

    /// The ceiling at (x, y) under the z it would cut (f64::INFINITY: none there): the lowest of the decks over it.
    /// Only ceilings under `z` are tested against the ground roads (the rest cut nothing).
    pub fn ceil_under(&self, x: f64, y: f64, z: f64) -> f64 {
        let q = [x, y];
        let Some(v) = self.seg_grid.get(&(cell_of(x), cell_of(y))) else { return f64::INFINITY };
        let d0 = D0_SQUARES * self.ends.s;
        let mut best = f64::INFINITY;
        let mut i = 0;
        while i < v.len() {
            let di = v[i].0 as usize;
            // (the nearest of the deck's listed segments: ties the first, in segment order)
            let d = &self.decks[di];
            let n = d.line.p.len();
            let mut near: Option<(f64, usize, f64, f64)> = None;
            while i < v.len() && v[i].0 as usize == di {
                let s = v[i].1 as usize;
                let (dist, u, raw) = seg_near(q, d.line.p[s], d.line.p[s + 1]);
                if near.map_or(true, |b| dist < b.0) { near = Some((dist, s, u, raw)); }
                i += 1;
            }
            let Some((dist, s, u, raw)) = near else { continue };
            if dist > d.hw || (s == 0 && raw < 0.0) || (s == n - 2 && raw > 1.0) { continue; }
            let t = d.line.s[s] + (d.line.s[s + 1] - d.line.s[s]) * u;
            let top = d.h[s] + (d.h[s + 1] - d.h[s]) * u;
            let mut sf = f64::INFINITY;
            for side in 0..2 {
                if !d.free[side] { continue; }
                let along = match d.end_of[side] {
                    Some(k) => self.ends.local(k, q).0,
                    None => if side == 0 { t } else { d.line.len() - t },
                };
                if along < sf { sf = along; }
            }
            if sf < d0 { continue; }
            let past = sf - FULL_M;
            let need = (d.depth + CLEAR_UNDER_M).min(DROP_M + if past > 0.0 { past * RAMP } else { 0.0 });
            let mut c = top - need;
            if c >= best || c >= z { continue; }
            let a = self.allow(di, q);
            if a.is_finite() && c < z - a { c = z - a; }
            if c < best { best = c; }
        }
        best
    }

    /// The physics ground at (x, y) from the ground z there: the hand-over's drop, then the ceiling.
    pub fn ground(&self, x: f64, y: f64, z: f64) -> f64 {
        let z = z - self.ends.drop_at(x, y);
        let c = self.ceil_under(x, y, z);
        if c < z { c } else { z }
    }

    /// The decks' boxes (x0, y0, x1, y1) grown by their half widths: where a ceiling may lie (probes).
    pub fn boxes(&self) -> Vec<[f64; 4]> {
        self.decks.iter().map(|d| {
            let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
            for q in &d.line.p { b = [b[0].min(q[0]), b[1].min(q[1]), b[2].max(q[0]), b[3].max(q[1])]; }
            [b[0] - d.hw, b[1] - d.hw, b[2] + d.hw, b[3] + d.hw]
        }).collect()
    }
}

// ------------------------------------------------------------------------------------------------ probes' input files

/// A ceiling's whole input as bytes ("RSCEIL01": the ends' parts and skew inputs as the hand-over's file has them, the
/// decks, the railways), so the CLI (`roadsys ceil IN`) can build it again on another platform and compare.
pub fn encode_input(bridges: &[Part], grounds: &[Part], tunnels: &[Vec<P2>], s: f64, sk: Option<&SkewIn>, decks: &[Deck],
                    rails: &[Vec<P2>]) -> Vec<u8> {
    let mut b: Vec<u8> = b"RSCEIL01".to_vec();
    let f = |b: &mut Vec<u8>, v: f64| b.extend_from_slice(&v.to_le_bytes());
    let u = |b: &mut Vec<u8>, v: u32| b.extend_from_slice(&v.to_le_bytes());
    let line = |b: &mut Vec<u8>, p: &[P2]| { u(b, p.len() as u32); for q in p { f(b, q[0]); f(b, q[1]); } };
    f(&mut b, s);
    for parts in [bridges, grounds] {
        u(&mut b, parts.len() as u32);
        for p in parts { f(&mut b, p.hw); line(&mut b, &p.pts); }
    }
    u(&mut b, tunnels.len() as u32);
    for t in tunnels { line(&mut b, t); }
    match sk {
        None => u(&mut b, 0),
        Some(k) => {
            u(&mut b, 1);
            u(&mut b, k.crossed.len() as u32);
            for (q, kind) in &k.crossed { u(&mut b, *kind as u32); line(&mut b, q); }
            u(&mut b, k.nbi.len() as u32);
            for n in &k.nbi { match n { None => u(&mut b, 0), Some(v) => { u(&mut b, 1); for c in v { f(&mut b, *c); } } } }
            u(&mut b, k.noskew.len() as u32);
            for &x in &k.noskew { u(&mut b, x as u32); }
        }
    }
    u(&mut b, decks.len() as u32);
    for d in decks { f(&mut b, d.hw); f(&mut b, d.depth); line(&mut b, &d.pts); for v in &d.h { f(&mut b, *v); } }
    u(&mut b, rails.len() as u32);
    for r in rails { line(&mut b, r); }
    b
}

/// Run an input file: the ceiling and the drop on a 0.5 m grid over every deck's box (their bytes, as compared across
/// platforms) and (points, points with a ceiling).
pub fn run_input(b: &[u8]) -> Result<(Vec<u8>, usize, usize), String> {
    struct R<'a> { b: &'a [u8], i: usize }
    impl R<'_> {
        fn take(&mut self, n: usize) -> Result<&[u8], String> {
            if self.i + n > self.b.len() { return Err("ceil input: truncated".into()); }
            let s = &self.b[self.i..self.i + n]; self.i += n; Ok(s)
        }
        fn u(&mut self) -> Result<u32, String> { Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap())) }
        fn f(&mut self) -> Result<f64, String> { Ok(f64::from_le_bytes(self.take(8)?.try_into().unwrap())) }
        fn line(&mut self) -> Result<Vec<P2>, String> {
            let k = self.u()? as usize;
            let mut p = Vec::with_capacity(k.min(1 << 20));
            for _ in 0..k { p.push([self.f()?, self.f()?]); }
            Ok(p)
        }
    }
    let mut r = R { b, i: 0 };
    if r.take(8)? != b"RSCEIL01" { return Err("ceil input: bad magic".into()); }
    let s = r.f()?;
    let mut lists: Vec<Vec<Part>> = Vec::new();
    for _ in 0..2 {
        let n = r.u()? as usize;
        let mut v = Vec::with_capacity(n.min(1 << 20));
        for _ in 0..n { let hw = r.f()?; let pts = r.line()?; v.push(Part { pts, hw }); }
        lists.push(v);
    }
    let nt = r.u()? as usize;
    let mut tunnels = Vec::new();
    for _ in 0..nt { tunnels.push(r.line()?); }
    let sk = if r.u()? == 1 {
        let mut k = SkewIn::default();
        let nc = r.u()? as usize;
        for _ in 0..nc { let kind = r.u()? as u8; let q = r.line()?; k.crossed.push((q, kind)); }
        let nn = r.u()? as usize;
        for _ in 0..nn { k.nbi.push(if r.u()? == 1 { Some([r.f()?, r.f()?, r.f()?, r.f()?]) } else { None }); }
        let ns = r.u()? as usize;
        for _ in 0..ns { k.noskew.push(r.u()? != 0); }
        Some(k)
    } else { None };
    let nd = r.u()? as usize;
    let mut decks = Vec::new();
    for _ in 0..nd {
        let hw = r.f()?;
        let depth = r.f()?;
        let pts = r.line()?;
        let mut h = Vec::with_capacity(pts.len());
        for _ in 0..pts.len() { h.push(r.f()?); }
        decks.push(Deck { pts, h, hw, depth });
    }
    let nr = r.u()? as usize;
    let mut rails = Vec::new();
    for _ in 0..nr { rails.push(r.line()?); }
    let grounds = lists.pop().unwrap();
    let bridges = lists.pop().unwrap();
    let ends = Ends::build(&bridges, grounds, &tunnels, s, sk.as_ref());
    let c = Ceil::new(ends, &decks, &rails);
    let mut out = Vec::new();
    let (mut n, mut hit) = (0usize, 0usize);
    let zt = decks.iter().flat_map(|d| d.h.iter().cloned()).fold(0.0, f64::max) + 5.0;
    for bb in c.boxes() {
        let (i0, i1) = ((bb[0] * 2.0).floor() as i64, (bb[2] * 2.0).ceil() as i64);
        let (j0, j1) = ((bb[1] * 2.0).floor() as i64, (bb[3] * 2.0).ceil() as i64);
        if (i1 - i0 + 1) * (j1 - j0 + 1) > 4_000_000 { continue; }
        for j in j0..=j1 {
            for i in i0..=i1 {
                let (x, y) = (i as f64 * 0.5, j as f64 * 0.5);
                let cv = c.ceil_under(x, y, zt);
                let dv = c.ends.drop_at(x, y);
                n += 1;
                if cv < zt { hit += 1; }
                out.extend_from_slice(&cv.to_le_bytes());
                out.extend_from_slice(&dv.to_le_bytes());
            }
        }
    }
    Ok((out, n, hit))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn straight(x0: f64, x1: f64, y: f64) -> Vec<P2> { vec![[x0, y], [(x0 + x1) / 2.0, y], [x1, y]] }

    /// a 60 m low deck (top 10.0, depth 1.2) from x 0 to 60, ground roads on from both ends; and its ceiling
    fn scene(extra_ground: Option<Part>) -> Ceil {
        let br = vec![Part { pts: straight(0.0, 60.0, 0.0), hw: 5.0 }];
        let mut gr = vec![Part { pts: straight(-50.0, 0.0, 0.0), hw: 5.0 }, Part { pts: straight(60.0, 110.0, 0.0), hw: 5.0 }];
        if let Some(g) = extra_ground { gr.push(g); }
        let ends = Ends::new(&br, gr, &[], 1.0);
        Ceil::new(ends, &[Deck { pts: straight(0.0, 60.0, 0.0), h: vec![10.0; 3], hw: 5.0, depth: 1.2 }], &[])
    }

    #[test]
    fn the_ground_under_a_low_deck_lies_under_its_soffit_eased_in_from_its_free_ends() {
        let c = scene(None);
        // mid deck: soffit 8.8 less 0.3
        assert!((c.ground(30.0, 0.0, 10.0) - 8.5).abs() < 1e-12);
        assert!((c.ground(30.0, 4.9, 10.0) - 8.5).abs() < 1e-12);
        // off its width, past its ends and over its approaches: the ground as it was
        assert_eq!(c.ground(30.0, 5.5, 10.0), 10.0);
        assert_eq!(c.ground(-3.0, 0.0, 10.0), 10.0);
        // the hand-over's first squares: the drop alone (none before D0)
        assert_eq!(c.ground(1.0, 0.0, 10.0), 10.0);
        assert!((c.ground(3.0, 0.0, 10.0) - 9.7).abs() < 1e-12);
        // past FULL_M the need grows 1:2 until the whole depth + clearance
        assert!((c.ground(7.0, 0.0, 10.0) - (10.0 - (0.3 + 2.0 * 0.5))).abs() < 1e-12);
        assert!((c.ground(53.0, 0.0, 10.0) - (10.0 - 1.3)).abs() < 1e-12);
        // a ground already deep enough keeps its height
        assert_eq!(c.ground(30.0, 0.0, 5.0), 5.0);
        // a high deck's ceiling over the ground cuts nothing
        let hi = Ceil::new(c.ends.clone(), &[Deck { pts: straight(0.0, 60.0, 0.0), h: vec![30.0; 3], hw: 5.0, depth: 1.2 }], &[]);
        assert_eq!(hi.ground(30.0, 0.0, 10.0), 10.0);
    }

    #[test]
    fn a_road_under_the_deck_and_a_railway_keep_their_ground() {
        let c = scene(Some(Part { pts: vec![[30.0, -40.0], [30.0, 40.0]], hw: 3.0 }));
        assert_eq!(c.ground(30.0, 0.0, 10.0), 10.0);
        // (kept to 3 + 0.5 + 1 + 1 m off its line, then 1:2 down to the soffit's need)
        assert_eq!(c.ground(35.5, 1.0, 10.0), 10.0);
        assert!((c.ground(36.5, 1.0, 10.0) - 9.5).abs() < 1e-12);
        assert!((c.ground(42.0, 1.0, 10.0) - 8.5).abs() < 1e-12);
        let br = vec![Part { pts: straight(0.0, 60.0, 0.0), hw: 5.0 }];
        let ends = Ends::new(&br, vec![], &[], 1.0);
        let r = Ceil::new(ends, &[Deck { pts: straight(0.0, 60.0, 0.0), h: vec![10.0; 3], hw: 5.0, depth: 1.2 }],
                          &[vec![[20.0, -40.0], [20.0, 40.0]]]);
        assert_eq!(r.ground(21.0, 0.0, 10.0), 10.0);
        assert!((r.ground(30.0, 0.0, 10.0) - 8.5).abs() < 1e-12);
        assert!((r.ground(27.0, 0.0, 10.0) - 9.5).abs() < 1e-12);
    }

    #[test]
    fn an_end_going_on_onto_another_deck_is_not_eased() {
        let br = vec![Part { pts: straight(0.0, 60.0, 0.0), hw: 5.0 }, Part { pts: straight(60.0, 120.0, 0.0), hw: 5.0 }];
        let ends = Ends::new(&br, vec![], &[], 1.0);
        let d = |a: f64, b: f64| Deck { pts: straight(a, b, 0.0), h: vec![10.0; 3], hw: 5.0, depth: 1.2 };
        let c = Ceil::new(ends, &[d(0.0, 60.0), d(60.0, 120.0)], &[]);
        assert!((c.ground(59.0, 0.0, 10.0) - 8.5).abs() < 1e-12);
        assert!((c.ground(61.0, 0.0, 10.0) - 8.5).abs() < 1e-12);
    }

    #[test]
    fn a_nodes_value_is_its_own_decks_whatever_other_decks_were_read() {
        // (M5 r5: a caller reads the decks near its own box; a deep deck far off changes no node of the first, even
        // where a ground road beside it limits a deep cut: the ground far over the deck)
        let br = vec![Part { pts: straight(0.0, 60.0, 0.0), hw: 5.0 }];
        let gr = vec![Part { pts: straight(-50.0, 0.0, 0.0), hw: 5.0 }, Part { pts: vec![[30.0, 9.0], [30.0, 60.0]], hw: 3.0 }];
        let a = Deck { pts: straight(0.0, 60.0, 0.0), h: vec![10.0; 3], hw: 5.0, depth: 0.6 };
        let far = Deck { pts: straight(500.0, 560.0, 0.0), h: vec![10.0; 3], hw: 5.0, depth: 8.0 };
        let c1 = Ceil::new(Ends::new(&br, gr.clone(), &[], 1.0), &[a.clone()], &[vec![[45.0, 8.0], [45.0, 80.0]]]);
        let c2 = Ceil::new(Ends::new(&br, gr.clone(), &[], 1.0), &[a, far], &[vec![[45.0, 8.0], [45.0, 80.0]]]);
        let mut cut = 0;
        for i in -20..140 {
            for j in -20..20 {
                let (x, y) = (i as f64 * 0.5, j as f64 * 0.5);
                for z in [10.0, 30.0] {
                    let g = c1.ground(x, y, z);
                    assert_eq!(g.to_bits(), c2.ground(x, y, z).to_bits(), "{x} {y} {z}");
                    if g < z { cut += 1; }
                }
            }
        }
        assert!(cut > 0);
    }

    #[test]
    fn the_order_and_copies_of_the_decks_change_nothing_and_the_input_file_runs_again() {
        let br = vec![Part { pts: straight(0.0, 60.0, 0.0), hw: 5.0 }];
        let gr = vec![Part { pts: straight(-50.0, 0.0, 0.0), hw: 5.0 }];
        let a = Deck { pts: straight(0.0, 60.0, 0.0), h: vec![10.0, 10.5, 11.0], hw: 5.0, depth: 1.2 };
        let mut rv = a.clone();
        rv.pts.reverse();
        rv.h.reverse();
        let b = Deck { pts: vec![[20.0, -20.0], [20.0, 20.0]], h: vec![9.0, 9.0], hw: 4.0, depth: 0.8 };
        let c1 = Ceil::new(Ends::new(&br, gr.clone(), &[], 1.0), &[a.clone(), b.clone()], &[]);
        let c2 = Ceil::new(Ends::new(&br, gr.clone(), &[], 1.0), &[b.clone(), rv, a.clone()], &[]);
        for i in -20..140 {
            for j in -50..50 {
                let (x, y) = (i as f64 * 0.5, j as f64 * 0.5);
                assert_eq!(c1.ground(x, y, 12.0).to_bits(), c2.ground(x, y, 12.0).to_bits());
            }
        }
        let bytes = encode_input(&br, &gr, &[], 1.0, None, &[a, b], &[]);
        let (o1, n, hit) = run_input(&bytes).unwrap();
        let (o2, _, _) = run_input(&bytes).unwrap();
        assert_eq!(o1, o2);
        assert!(n > 0 && hit > 0);
    }
}
