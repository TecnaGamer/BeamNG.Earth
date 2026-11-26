//! M2 profile squares (BNE_ROADSYS=heights): the one profile per carriageway as bounded, keyed products.
//!
//! A whole vertical component can span a region (one Newark chain's read reached 125 map cells, 98 s of reading): too
//! much to read on a tile's path. So the profiles are solved per square of a fixed grid of the region frame (SQUARE_M),
//! each from the map inside its window (the square grown by BAND_M + MARGIN_M: its read list is that box, nothing
//! else), every Tier B section cut to the window (prof::plan_w: a cut end held to Tier A, height and slope; a coupling
//! outside the window holds nothing), and the squares' answers blended by a partition of unity: weight
//! phi_i(x) phi_j(y), phi a smoothstep ramp over BAND_M either side of each grid line (overlapping Schwarz: inside its
//! square one square's own answer; the MARGIN_M past its band keeps the cut ends' effect out of it). Linear couplings
//! (noses, floors, deck joints) met in every square stay met in the blend (a convex combination with the same weight
//! at the same point). A square is a function of its window's map data and the ground there alone: the same answer
//! whichever chain, tile or process asks, and a product that can be kept on disk or served by its key.
//!
//! What a square keeps (strokes with a Tier B section or a structure, their knots within its blend reach), what it
//! answers (`sample`: a deck vertex's height on the structure of its own map feature; a road point's height on the
//! stroke holding its feature) and its terrain targets (`pins`: the ground runs of its Tier B sections inside the
//! square itself, as heights.rs made them per chain: stations every PIN_STEP_M, every PIN_FINE_M near a structure end
//! or floor, a fade to 0 over the section's free ends, a share falling from its anchors, the road's half width; their
//! heights from the blend, by the caller).
use crate::geom::*;
use crate::io::Place;
use crate::net::{self, Net, Opts, SUP_GROUND, SUP_STRUCT, SUP_TUNNEL};
use crate::part;
use crate::prof;
use std::collections::BTreeMap;
use std::fmt::Write as _;

pub const SQ_FORMAT: u64 = 2;    // 2 (M3)
pub const SQUARE_M: f64 = 2000.0;
pub const BAND_M: f64 = 250.0;
pub const MARGIN_M: f64 = 850.0;
const KEEP_M: f64 = BAND_M + 15.0;      // knots kept round the square (its weight reaches BAND_M past it)
const GRID_M: f64 = 32.0;
const MATCH_M: f64 = 3.0;               // m a deck vertex may lie off its structure's knots
const ROAD_MATCH_M: f64 = 1.0;          // m a point of a road may lie off the same road's knots in another square
const MATCH_COS: f64 = 0.7071067811865476;
const SUP_TOL_M: f64 = 3.0;
const DECK_PAST_M: f64 = 60.0;          // (M5) m past a mapped structure end a deck of the deck builder still takes its profile
const FEAT_TOL_M: f64 = 2.0;
const PIN_STEP_M: f64 = 2.5;
const PIN_FINE_M: f64 = 0.25;
const PIN_FINE_REACH_M: f64 = 20.0;
const PIN_END_IN_M: f64 = 0.05;
const SHARE_REACH_M: f64 = 200.0;
const SHARE_DROP: f64 = 0.9;
const HALF_MAX_M: f64 = 15.0;
const HAND_MIN_M: f64 = 40.0;           // (M3) the reach round a structure end / floor over which the profile's ground
const HAND_MAX_M: f64 = 200.0;          // goes back to the plans' own: half the approach reach, within these
const TUNNEL_FADE_M: f64 = 120.0;       // m past a tunnel portal its section's targets fade in (today's tunnels own
                                        // their portals' heights: TunnelProfiles)

pub fn sq_of(p: P2) -> (i64, i64) { ((p[0] / SQUARE_M).floor() as i64, (p[1] / SQUARE_M).floor() as i64) }

pub fn bounds(sq: (i64, i64)) -> [f64; 4] {
    let (x0, y0) = (sq.0 as f64 * SQUARE_M, sq.1 as f64 * SQUARE_M);
    [x0, y0, x0 + SQUARE_M, y0 + SQUARE_M]
}

pub fn grow(b: [f64; 4], d: f64) -> [f64; 4] { [b[0] - d, b[1] - d, b[2] + d, b[3] + d] }

/// The square's read box (its window).
pub fn window(sq: (i64, i64)) -> [f64; 4] { grow(bounds(sq), BAND_M + MARGIN_M) }

fn smooth(u: f64) -> f64 { let u = clampf(u, 0.0, 1.0); u * u * (3.0 - 2.0 * u) }

fn phi(x: f64, i: i64) -> f64 {
    let (a, b) = (i as f64 * SQUARE_M - BAND_M, (i + 1) as f64 * SQUARE_M - BAND_M);
    smooth((x - a) / (2.0 * BAND_M)) - smooth((x - b) / (2.0 * BAND_M))
}

/// The squares weighing point p, with their weights (> 0; they sum to 1 up to rounding), in (x, y) order.
pub fn weights(p: P2) -> Vec<((i64, i64), f64)> {
    let (i, j) = sq_of(p);
    let mut out = Vec::new();
    for di in -1..=1 {
        let wx = phi(p[0], i + di);
        if wx <= 0.0 { continue; }
        for dj in -1..=1 {
            let wy = phi(p[1], j + dj);
            if wy > 0.0 { out.push(((i + di, j + dj), wx * wy)); }
        }
    }
    out
}

/// The squares whose weight reaches a box.
pub fn squares_of_box(b: [f64; 4]) -> Vec<(i64, i64)> {
    let g = grow(b, BAND_M);
    let (i0, j0) = sq_of([g[0], g[1]]);
    let (i1, j1) = sq_of([g[2], g[3]]);
    let mut out = Vec::new();
    for i in i0..=i1 { for j in j0..=j1 { out.push((i, j)); } }
    out
}

/// A map feature's id as both the deck builder and the lane graph name it: the row id before its first point, less a
/// lane section's suffix.
pub fn feature_of(fid: &str) -> &str {
    let b = fid.split('|').next().unwrap_or(fid);
    b.split("~lanes").next().unwrap_or(b)
}

#[derive(Clone, Debug, Default)]
pub struct SqStroke {
    pub uid: u64,
    pub s: Vec<f64>,                 // the kept knots' stations
    pub pts: Vec<P2>,
    pub z: Vec<f64>,
    pub half: Vec<f64>,
    pub feats: Vec<(f64, f64, String)>,      // (s0, s1, map feature) of its parts
    pub sup: Vec<(f64, f64, u8, f64)>,      // non-ground intervals (with a structure's drawn depth, M3)
    pub secs: Vec<(f64, f64, bool, bool, f64)>,   // Tier B sections: (s0, s1, free at s0, free at s1, fade length)
    pub anchors: Vec<f64>,                  // structure ends and floors (the shares' anchors)
    pub hand: Vec<f64>,                     // (M3) where the profile owns the ground: structure ends, floors it is the upper road of
    pub lows: Vec<f64>,                     // (M3) floors it is the lower road of (the ground there only dug)
    pub hand_m: f64,                        // (M3) m from those over which the ground goes back to the plans' own
    pub floors: Vec<f64>,                   // (M3) stations of the floors it is the upper road of (chain_heights' holds)
    pub tun: Vec<crate::tunnel::Run>,       // (T1, BNE_ROADSYS_TUNNELS) its tunnel record: covered runs (tunnel.rs)
    pub tsnap: Vec<crate::tunnel::Snap>,    // (T1) rule P's outward snaps of its mapped ranges' ends
}

impl SqStroke {
    fn at(&self, s: f64) -> f64 {
        let n = self.s.len();
        if n == 0 { return f64::NAN; }
        if n == 1 || s <= self.s[0] { return self.z[0]; }
        if s >= self.s[n - 1] { return self.z[n - 1]; }
        let k = self.s.partition_point(|&x| x <= s).saturating_sub(1).min(n - 2);
        let l = self.s[k + 1] - self.s[k];
        let u = if l > 0.0 { (s - self.s[k]) / l } else { 0.0 };
        self.z[k] * (1.0 - u) + self.z[k + 1] * u
    }
    fn feature_at(&self, s: f64) -> Option<&str> {
        self.feats.iter().find(|f| s >= f.0 - 1e-6 && s <= f.1 + 1e-6).map(|f| f.2.as_str())
    }
    /// (M5: within DECK_PAST_M past a structure's mapped end too: the deck builder lengthens decks over roads under
    /// them and over lidar-raised stretches; the profile is the carriageway's there as anywhere)
    fn on_structure(&self, s: f64) -> bool {
        self.sup.iter().any(|u| u.2 == SUP_STRUCT && s >= u.0 - DECK_PAST_M && s <= u.1 + DECK_PAST_M)
    }
    fn depth_at(&self, s: f64) -> Option<f64> {
        self.sup.iter().filter(|u| u.2 == SUP_STRUCT && s >= u.0 - DECK_PAST_M && s <= u.1 + DECK_PAST_M).map(|u| u.3).next()
    }
    /// (station, offset) of q on the kept knots' polyline (nearest; ties the first) and the tangent there.
    fn project(&self, q: P2) -> (f64, f64, P2) {
        let mut best = (f64::INFINITY, 0.0, [1.0, 0.0]);
        for k in 0..self.pts.len().saturating_sub(1) {
            let (a, b) = (self.pts[k], self.pts[k + 1]);
            let ab = sub(b, a);
            let l2 = dot(ab, ab);
            let u = if l2 > 0.0 { clampf(dot(sub(q, a), ab) / l2, 0.0, 1.0) } else { 0.0 };
            let d = norm(sub(q, lerp(a, b, u)));
            if d < best.0 { best = (d, self.s[k] + (self.s[k + 1] - self.s[k]) * u, unit(ab).unwrap_or([1.0, 0.0])); }
        }
        (best.1, best.0, best.2)
    }
}

#[derive(Clone, Debug, Default)]
pub struct Square {
    pub sq: (i64, i64),
    pub strokes: Vec<SqStroke>,
    pub grid: BTreeMap<(i64, i64), Vec<usize>>,
    pub json: String,               // its stats (in its bytes: a function of its inputs)
    pub key: u64,
    pub cpu_json: String,           // its timings (not in its bytes)
    pub tunnels: bool,              // (T1) solved with the tunnel record (BNE_ROADSYS_TUNNELS): its "TUN1" block
}

impl Square {
    fn index(&mut self) {
        self.grid.clear();
        for (k, st) in self.strokes.iter().enumerate() {
            for w in st.pts.windows(2) {
                let (x0, x1) = ((w[0][0].min(w[1][0]) / GRID_M).floor() as i64, (w[0][0].max(w[1][0]) / GRID_M).floor() as i64);
                let (y0, y1) = ((w[0][1].min(w[1][1]) / GRID_M).floor() as i64, (w[0][1].max(w[1][1]) / GRID_M).floor() as i64);
                for gx in x0..=x1 { for gy in y0..=y1 { let v = self.grid.entry((gx, gy)).or_default(); if v.last() != Some(&k) { v.push(k); } } }
            }
        }
        for v in self.grid.values_mut() { v.sort(); v.dedup(); }
    }

    fn near(&self, q: P2, r: f64) -> Vec<usize> {
        let (x0, x1) = (((q[0] - r) / GRID_M).floor() as i64, ((q[0] + r) / GRID_M).floor() as i64);
        let (y0, y1) = (((q[1] - r) / GRID_M).floor() as i64, ((q[1] + r) / GRID_M).floor() as i64);
        let mut out = Vec::new();
        for gx in x0..=x1 { for gy in y0..=y1 { if let Some(v) = self.grid.get(&(gx, gy)) { out.extend_from_slice(v); } } }
        out.sort();
        out.dedup();
        out
    }

    /// This square's height at q (tangent t) on map feature `feat`: on a structure of that feature within MATCH_M
    /// when `deck`, else on a stroke of that feature within ROAD_MATCH_M; the nearest, ties by uid. None: no such.
    pub fn sample(&self, q: P2, t: Option<P2>, feat: &str, deck: bool) -> Option<f64> {
        let r = if deck { MATCH_M } else { ROAD_MATCH_M };
        let mut best: Option<(f64, u64, f64)> = None;
        for k in self.near(q, r) {
            let st = &self.strokes[k];
            let (s, d, ts) = st.project(q);
            if d > r { continue; }
            // (its feature there, give or take FEAT_TOL_M: a deck's end vertex is its feature's end, where the knots'
            // chord may put the foot on the next feature)
            if !st.feats.iter().any(|f| f.2 == feat && s >= f.0 - FEAT_TOL_M && s <= f.1 + FEAT_TOL_M) { continue; }
            if deck && !st.on_structure(s) { continue; }
            if let Some(t) = t { if absf(dot(t, ts)) < MATCH_COS { continue; } }
            if best.map(|b| (d, st.uid) < (b.0, b.1)).unwrap_or(true) { best = Some((d, st.uid, st.at(s))); }
        }
        best.map(|b| b.2)
    }

    /// (T1) The tunnel record at q (tangent t) on map feature `feat`: (kind of the covered run there: 0 daylight gap,
    /// 1 lid, 2 full tube; 255 not covered; its least inner cover) on the nearest stroke of that feature within
    /// ROAD_MATCH_M (ties by uid), as `sample` finds it. None: no such stroke here.
    pub fn tunnel_at(&self, q: P2, t: Option<P2>, feat: &str) -> Option<(u8, f64)> {
        let mut best: Option<(f64, u64, (u8, f64))> = None;
        for k in self.near(q, ROAD_MATCH_M) {
            let st = &self.strokes[k];
            let (s, d, ts) = st.project(q);
            if d > ROAD_MATCH_M { continue; }
            if !st.feats.iter().any(|f| f.2 == feat && s >= f.0 - FEAT_TOL_M && s <= f.1 + FEAT_TOL_M) { continue; }
            if let Some(t) = t { if absf(dot(t, ts)) < MATCH_COS { continue; } }
            let got = st.tun.iter().find(|r| s >= r.s0 - 1e-6 && s <= r.s1 + 1e-6).map(|r| (r.kind, r.cmin)).unwrap_or((255, f64::NAN));
            if best.map(|b| (d, st.uid) < (b.0, b.1)).unwrap_or(true) { best = Some((d, st.uid, got)); }
        }
        best.map(|b| b.2)
    }

    /// (T1) The portals of the record whose point lies in this square itself (each portal in exactly one square):
    /// (point, outward unit direction, hole reach in (m), map feature there, stroke uid), in stroke order.
    pub fn tunnel_mouths(&self) -> Vec<(P2, P2, f64, String, u64)> {
        let b = bounds(self.sq);
        let mut out = Vec::new();
        for st in &self.strokes {
            let n = st.s.len();
            if n < 2 { continue; }
            let at = |s: f64| -> Option<(P2, P2)> {
                if s < st.s[0] - 1e-6 || s > st.s[n - 1] + 1e-6 { return None; }
                let k = st.s.partition_point(|&x| x <= s).saturating_sub(1).min(n - 2);
                let l = st.s[k + 1] - st.s[k];
                let u = if l > 0.0 { clampf((s - st.s[k]) / l, 0.0, 1.0) } else { 0.0 };
                Some((lerp(st.pts[k], st.pts[k + 1], u), unit(sub(st.pts[k + 1], st.pts[k])).unwrap_or([1.0, 0.0])))
            };
            for r in &st.tun {
                for (on, s, hole, sign) in [(r.mouth0, r.s0, r.hole0, -1.0), (r.mouth1, r.s1, r.hole1, 1.0)] {
                    if !on { continue; }
                    let Some((p, t)) = at(s) else { continue };
                    if !(p[0] >= b[0] && p[0] < b[2] && p[1] >= b[1] && p[1] < b[3]) { continue; }
                    let f = st.feature_at(s).unwrap_or("").to_string();
                    out.push((p, [t[0] * sign, t[1] * sign], hole, f, st.uid));
                }
            }
        }
        out
    }

    /// (M3) The drawn depth (m) of the structure of map feature `feat` under q (as `sample` with deck: the nearest
    /// such structure within MATCH_M, ties by uid; the full depth where its segments gave none); None: no such structure.
    pub fn sample_depth(&self, q: P2, t: Option<P2>, feat: &str) -> Option<f64> {
        let mut best: Option<(f64, u64, f64)> = None;
        for k in self.near(q, MATCH_M) {
            let st = &self.strokes[k];
            let (s, d, ts) = st.project(q);
            if d > MATCH_M { continue; }
            if !st.feats.iter().any(|f| f.2 == feat && s >= f.0 - FEAT_TOL_M && s <= f.1 + FEAT_TOL_M) { continue; }
            let Some(dp) = st.depth_at(s) else { continue };
            if let Some(t) = t { if absf(dot(t, ts)) < MATCH_COS { continue; } }
            if best.map(|b| (d, st.uid) < (b.0, b.1)).unwrap_or(true) { best = Some((d, st.uid, dp)); }
        }
        best.map(|b| b.2)
    }

    /// (M3) The floors it holds over other roads inside a box: (point, its map feature) of every station where a stroke
    /// of it is the upper road of a crossing, in stroke order.
    pub fn floor_points(&self, b: [f64; 4]) -> Vec<(P2, String)> {
        let mut out = Vec::new();
        for st in &self.strokes {
            if st.s.len() < 2 { continue; }
            let (s0, s1) = (st.s[0], st.s[st.s.len() - 1]);
            for &x in &st.floors {
                if x < s0 || x > s1 { continue; }
                let n = st.s.len();
                let k = st.s.partition_point(|&v| v <= x).saturating_sub(1).min(n - 2);
                let l = st.s[k + 1] - st.s[k];
                let u = if l > 0.0 { clampf((x - st.s[k]) / l, 0.0, 1.0) } else { 0.0 };
                let p = lerp(st.pts[k], st.pts[k + 1], u);
                if p[0] < b[0] || p[0] > b[2] || p[1] < b[1] || p[1] > b[3] { continue; }
                if let Some(f) = st.feature_at(x) { out.push((p, f.to_string())); }
            }
        }
        out
    }

    // ------------------------------------------------------------------------------- (de)serialisation, own format

    pub fn to_bytes(&self) -> Vec<u8> {
        let mut b: Vec<u8> = Vec::new();
        let u = |b: &mut Vec<u8>, v: u64| b.extend_from_slice(&v.to_le_bytes());
        let f = |b: &mut Vec<u8>, v: f64| b.extend_from_slice(&v.to_bits().to_le_bytes());
        b.extend_from_slice(b"RSSQ");
        u(&mut b, SQ_FORMAT);
        u(&mut b, self.sq.0 as u64);
        u(&mut b, self.sq.1 as u64);
        u(&mut b, self.key);
        u(&mut b, self.json.len() as u64);
        b.extend_from_slice(self.json.as_bytes());
        u(&mut b, self.strokes.len() as u64);
        for st in &self.strokes {
            u(&mut b, st.uid);
            u(&mut b, st.s.len() as u64);
            for i in 0..st.s.len() { f(&mut b, st.s[i]); f(&mut b, st.pts[i][0]); f(&mut b, st.pts[i][1]); f(&mut b, st.z[i]); f(&mut b, st.half[i]); }
            u(&mut b, st.feats.len() as u64);
            for (a, c, n) in &st.feats { f(&mut b, *a); f(&mut b, *c); u(&mut b, n.len() as u64); b.extend_from_slice(n.as_bytes()); }
            u(&mut b, st.sup.len() as u64);
            for (a, c, k, dp) in &st.sup { f(&mut b, *a); f(&mut b, *c); u(&mut b, *k as u64); f(&mut b, *dp); }
            u(&mut b, st.secs.len() as u64);
            for (a, c, f0, f1, fl) in &st.secs { f(&mut b, *a); f(&mut b, *c); u(&mut b, *f0 as u64 | ((*f1 as u64) << 1)); f(&mut b, *fl); }
            u(&mut b, st.anchors.len() as u64);
            for a in &st.anchors { f(&mut b, *a); }
            u(&mut b, st.hand.len() as u64);
            for a in &st.hand { f(&mut b, *a); }
            u(&mut b, st.lows.len() as u64);
            for a in &st.lows { f(&mut b, *a); }
            f(&mut b, st.hand_m);
            u(&mut b, st.floors.len() as u64);
            for a in &st.floors { f(&mut b, *a); }
        }
        // (T1) the tunnel record, only for a square solved with it: the bytes of every other square as before
        if self.tunnels {
            b.extend_from_slice(b"TUN1");
            for (k, st) in self.strokes.iter().enumerate() {
                if st.tun.is_empty() && st.tsnap.is_empty() { continue; }
                u(&mut b, k as u64);
                u(&mut b, st.tun.len() as u64);
                for r in &st.tun {
                    f(&mut b, r.s0); f(&mut b, r.s1);
                    u(&mut b, r.kind as u64 | (r.mouth0 as u64) << 8 | (r.mouth1 as u64) << 9);
                    f(&mut b, r.cmin); f(&mut b, r.hole0); f(&mut b, r.hole1);
                }
                u(&mut b, st.tsnap.len() as u64);
                for q in &st.tsnap { f(&mut b, q.mapped); f(&mut b, q.built); f(&mut b, q.cut); }
            }
            u(&mut b, u64::MAX);
        }
        let h = fnv(&b);
        u(&mut b, h);
        b
    }

    pub fn from_bytes(data: &[u8]) -> Result<Square, String> {
        if data.len() < 12 || &data[..4] != b"RSSQ" { return Err("not a roadsys square".into()); }
        let body = &data[..data.len() - 8];
        let tail = u64::from_le_bytes(data[data.len() - 8..].try_into().unwrap());
        if fnv(body) != tail { return Err("roadsys square: checksum".into()); }
        let mut pos = 4usize;
        let mut u = || -> Result<u64, String> {
            if pos + 8 > body.len() { return Err("roadsys square: short".into()); }
            let v = u64::from_le_bytes(body[pos..pos + 8].try_into().unwrap());
            pos += 8;
            Ok(v)
        };
        // (two readers over one cursor: a closure for u64s, the string reads through it too)
        let fmt = u()?;
        if fmt != SQ_FORMAT { return Err(format!("roadsys square: format {}", fmt)); }
        let sq = (u()? as i64, u()? as i64);
        let key = u()?;
        let mut sqr = Square { sq, key, ..Default::default() };
        let take = |pos: &mut usize, n: usize| -> Result<Vec<u8>, String> {
            if *pos + n > body.len() { return Err("roadsys square: short".into()); }
            let v = body[*pos..*pos + n].to_vec();
            *pos += n;
            Ok(v)
        };
        drop(u);
        let rd = |pos: &mut usize| -> Result<u64, String> {
            if *pos + 8 > body.len() { return Err("roadsys square: short".into()); }
            let v = u64::from_le_bytes(body[*pos..*pos + 8].try_into().unwrap());
            *pos += 8;
            Ok(v)
        };
        let fl = |pos: &mut usize| -> Result<f64, String> { Ok(f64::from_bits(rd(pos)?)) };
        let jn = rd(&mut pos)? as usize;
        sqr.json = String::from_utf8(take(&mut pos, jn)?).map_err(|e| e.to_string())?;
        let ns = rd(&mut pos)? as usize;
        if ns > 10_000_000 { return Err("roadsys square: size".into()); }
        for _ in 0..ns {
            let mut st = SqStroke { uid: rd(&mut pos)?, ..Default::default() };
            let n = rd(&mut pos)? as usize;
            if n > 100_000_000 { return Err("roadsys square: size".into()); }
            for _ in 0..n { st.s.push(fl(&mut pos)?); st.pts.push([fl(&mut pos)?, fl(&mut pos)?]); st.z.push(fl(&mut pos)?); st.half.push(fl(&mut pos)?); }
            let n = rd(&mut pos)? as usize;
            for _ in 0..n { let (a, c) = (fl(&mut pos)?, fl(&mut pos)?); let l = rd(&mut pos)? as usize; let s = String::from_utf8(take(&mut pos, l)?).map_err(|e| e.to_string())?; st.feats.push((a, c, s)); }
            let n = rd(&mut pos)? as usize;
            for _ in 0..n { let (a, c) = (fl(&mut pos)?, fl(&mut pos)?); let k = rd(&mut pos)? as u8; st.sup.push((a, c, k, fl(&mut pos)?)); }
            let n = rd(&mut pos)? as usize;
            for _ in 0..n { let (a, c) = (fl(&mut pos)?, fl(&mut pos)?); let b = rd(&mut pos)?; st.secs.push((a, c, b & 1 != 0, b & 2 != 0, fl(&mut pos)?)); }
            let n = rd(&mut pos)? as usize;
            for _ in 0..n { st.anchors.push(fl(&mut pos)?); }
            let n = rd(&mut pos)? as usize;
            if n > 100_000_000 { return Err("roadsys square: size".into()); }
            for _ in 0..n { st.hand.push(fl(&mut pos)?); }
            let n = rd(&mut pos)? as usize;
            if n > 100_000_000 { return Err("roadsys square: size".into()); }
            for _ in 0..n { st.lows.push(fl(&mut pos)?); }
            st.hand_m = fl(&mut pos)?;
            let n = rd(&mut pos)? as usize;
            if n > 100_000_000 { return Err("roadsys square: size".into()); }
            for _ in 0..n { st.floors.push(fl(&mut pos)?); }
            sqr.strokes.push(st);
        }
        if pos + 4 <= body.len() && &body[pos..pos + 4] == b"TUN1" {
            pos += 4;
            sqr.tunnels = true;
            loop {
                let k = rd(&mut pos)?;
                if k == u64::MAX { break; }
                let k = k as usize;
                if k >= sqr.strokes.len() { return Err("roadsys square: tunnel record stroke".into()); }
                let n = rd(&mut pos)? as usize;
                if n > 1_000_000 { return Err("roadsys square: size".into()); }
                for _ in 0..n {
                    let (s0, s1) = (fl(&mut pos)?, fl(&mut pos)?);
                    let fb = rd(&mut pos)?;
                    let (cmin, hole0, hole1) = (fl(&mut pos)?, fl(&mut pos)?, fl(&mut pos)?);
                    sqr.strokes[k].tun.push(crate::tunnel::Run { s0, s1, kind: (fb & 0xff) as u8, mouth0: fb & 0x100 != 0, mouth1: fb & 0x200 != 0, cmin, hole0, hole1 });
                }
                let n = rd(&mut pos)? as usize;
                if n > 1_000_000 { return Err("roadsys square: size".into()); }
                for _ in 0..n { let (a, b2, c) = (fl(&mut pos)?, fl(&mut pos)?, fl(&mut pos)?); sqr.strokes[k].tsnap.push(crate::tunnel::Snap { mapped: a, built: b2, cut: c }); }
            }
        }
        if pos != body.len() { return Err("roadsys square: trailing bytes".into()); }
        sqr.index();
        Ok(sqr)
    }
}

/// The strokes a square's profiles need: those with a structure, a tunnel, a floor or a deck-level end, the strokes
/// meeting them at a node (secondary sections), and, three times over, the snap trunks and snapped branches of all
/// of them (noses and joints) and the lower roads of their floors.
pub fn need(net: &Net) -> Vec<bool> {
    let ns = net.strokes.len();
    let mut nd = vec![false; ns];
    for (si, st) in net.strokes.iter().enumerate() { if st.sup.iter().any(|u| u.kind != SUP_GROUND) { nd[si] = true; } }
    for c in &net.crossings { nd[c.a] = true; nd[c.b] = true; }
    let mut ends: Vec<Vec<usize>> = vec![Vec::new(); net.nodes.len()];
    for (si, st) in net.strokes.iter().enumerate() { ends[st.n0].push(si); ends[st.n1].push(si); }
    for _round in 0..3 {
        let mut add = nd.clone();
        for si in 0..ns {
            if !nd[si] { continue; }
            let st = &net.strokes[si];
            for n in [st.n0, st.n1] { for &o in &ends[n] { add[o] = true; } }
            for sn in [st.snap0, st.snap1].into_iter().flatten() { add[sn.trunk] = true; }
        }
        for si in 0..ns {
            let st = &net.strokes[si];
            for sn in [st.snap0, st.snap1].into_iter().flatten() { if nd[sn.trunk] { add[si] = true; } }
        }
        if add == nd { break; }
        nd = add;
    }
    nd
}

/// (T1) The tunnel record of stroke si (tunnel.rs) from its profile and ground, the side ground read for the knots
/// round its tunnels (one g_of call, the knots' order: replayed as asked).
fn tunnel_record(net: &Net, si: usize, pr: &prof::Profiles, ends: &[Vec<usize>], g_of: &dyn Fn(&[P2]) -> Vec<f64>)
                 -> (Vec<crate::tunnel::Run>, Vec<crate::tunnel::Snap>) {
    use crate::tunnel::{stroke_record, StrokeIn, SIDE_OFFS, X_MAX};
    let st = &net.strokes[si];
    let (z, g, h) = (&pr.z[si], &pr.g[si], pr.h[si]);
    let m = z.len();
    if m < 2 || g.len() != m { return (Vec::new(), Vec::new()); }
    let len = st.len();
    let s: Vec<f64> = (0..m).map(|i| (i as f64 * h).min(len)).collect();
    let ranges: Vec<(f64, f64)> = st.sup.iter().filter(|u| u.kind == SUP_TUNNEL).map(|u| (u.s0, u.s1)).collect();
    let near = |x: f64| ranges.iter().any(|&(a, b)| x >= a - X_MAX - 10.0 && x <= b + X_MAX + 10.0);
    let mut pts: Vec<P2> = Vec::new();
    let mut at: Vec<usize> = Vec::new();
    let mut offs = Vec::new();
    for (i, &x) in s.iter().enumerate() {
        if !near(x) { continue; }
        let (p, t) = st.line.at(x);
        st.offsets(x, &mut offs);
        let half = if offs.is_empty() { 3.5 } else { offs[0].max(-offs[offs.len() - 1]) };
        let nrm = [-t[1], t[0]];
        for side in [1.0f64, -1.0] { for o in SIDE_OFFS { let d = side * (half + o); pts.push([p[0] + nrm[0] * d, p[1] + nrm[1] * d]); } }
        at.push(i);
    }
    let gz = if pts.is_empty() { Vec::new() } else { g_of(&pts) };
    let mut side_l = vec![[f64::NAN; 3]; m];
    let mut side_r = vec![[f64::NAN; 3]; m];
    for (k, &i) in at.iter().enumerate() {
        if gz.len() < (k + 1) * 6 { break; }
        for j in 0..3 { side_l[i][j] = gz[k * 6 + j]; side_r[i][j] = gz[k * 6 + 3 + j]; }
    }
    // a range's end at a stroke end where another stroke goes on in a tunnel: a joint
    let joined = |node: usize| -> bool {
        ends[node].iter().any(|&o| o != si && {
            let so = &net.strokes[o];
            let e = if so.n0 == node { 0.0 } else { so.len() };
            so.sup.iter().any(|u| u.kind == SUP_TUNNEL && e >= u.s0 - SUP_TOL_M && e <= u.s1 + SUP_TOL_M)
        })
    };
    let inp = StrokeIn { s: &s, z, g, side_l: &side_l, side_r: &side_r, ranges: &ranges, joined0: joined(st.n0), joined1: joined(st.n1), kmh: st.kmh };
    stroke_record(&inp)
}

/// A square solved from its window's segments (the caller reads the map cells covering window(sq)); g_of: ground
/// heights at points (once, prof::knot_points' order).
pub fn solve(place: &Place, sq: (i64, i64), g_of: &dyn Fn(&[P2]) -> Vec<f64>, opts: Opts) -> Square {
    solve_obs(place, sq, g_of, None, opts)
}

/// solve() with measured deck heights (prof::solve_obs).
pub fn solve_obs(place: &Place, sq: (i64, i64), g_of: &dyn Fn(&[P2]) -> Vec<f64>,
                 obs_of: Option<&dyn Fn(&[prof::ObsRun]) -> Vec<Vec<f64>>>, opts: Opts) -> Square {
    let t0 = crate::cpu_now();
    let order: Vec<usize> = (0..place.segs.len()).collect();
    let mut net = net::build_opts(place, &order, opts);
    let _lay = part::layout(&mut net);
    let t1 = crate::cpu_now();
    let nd = need(&net);
    let win = window(sq);
    let pr = prof::solve_obs(&net, g_of, obs_of, Some(&nd), Some(win));
    let t2 = crate::cpu_now();
    let keep = grow(bounds(sq), KEEP_M);
    let inside = |p: P2, b: [f64; 4]| p[0] >= b[0] && p[0] <= b[2] && p[1] >= b[1] && p[1] <= b[3];
    let mut ends: Vec<Vec<usize>> = vec![Vec::new(); net.nodes.len()];
    for (si, st) in net.strokes.iter().enumerate() { ends[st.n0].push(si); ends[st.n1].push(si); }
    // (a section's end at a node is held with it, not free, only when every other arm there is in its component)
    let in_comp_at = |node: usize, me: usize, cid: usize| -> bool {
        let others: Vec<usize> = ends[node].iter().copied().filter(|&o| o != me).collect();
        !others.is_empty() && others.iter().all(|&o| pr.sections[o].iter().any(|&(a, b, c)| {
            let len = net.strokes[o].len();
            c == cid && ((net.strokes[o].n0 == node && a <= 0.5) || (net.strokes[o].n1 == node && b >= len - 0.5))
        }))
    };
    if let Ok(v) = std::env::var("ROADSYS_DEBUG_NEAR") {
        let f: Vec<f64> = v.split(',').filter_map(|x| x.parse().ok()).collect();
        if f.len() == 3 {
            for si in 0..net.strokes.len() {
                if !nd[si] || pr.z[si].is_empty() { continue; }
                let st = &net.strokes[si];
                for i in 0..pr.z[si].len() {
                    let s = (i as f64 * pr.h[si]).min(st.len());
                    let p = st.line.at(s).0;
                    if norm(sub(p, [f[0], f[1]])) > f[2] && std::env::var("ROADSYS_DEBUG_UID").map(|u| u != format!("{:x}", st.uid) || i % 10 != 0).unwrap_or(true) { continue; }
                    eprintln!("near stroke {} uid {:x} cls {} kmh {} s {:.0} sup {} lvl {} g {:.2} ob {:.2} a {:.2} z {:.2} sec {:?}", si, st.uid, st.cls, st.kmh, s, st.support_at(s).kind, st.support_at(s).level,
                              pr.g[si][i], pr.ob[si].get(i).copied().unwrap_or(f64::NAN), pr.tier_a[si][i], pr.z[si][i], pr.sections[si].iter().find(|x| s >= x.0 && s <= x.1));
                    if std::env::var("ROADSYS_DEBUG_SNAP").is_ok() { eprintln!("   n0 {} n1 {} len {:.0} snap0 {:?} snap1 {:?} cut {:.1} {:.1}", st.n0, st.n1, st.len(), st.snap0.map(|x| (x.trunk, x.s, x.joint)), st.snap1.map(|x| (x.trunk, x.s, x.joint)), st.cut0, st.cut1); }
                }
            }
        }
    }
    if let Ok(v) = std::env::var("ROADSYS_DEBUG_NODE") {
        let nn: usize = v.parse().unwrap_or(usize::MAX);
        for (si, st) in net.strokes.iter().enumerate() {
            if st.n0 != nn && st.n1 != nn { continue; }
            let zz = if pr.z[si].is_empty() { (f64::NAN, f64::NAN) } else { (pr.z[si][0], pr.z[si][pr.z[si].len() - 1]) };
            let ta = if pr.tier_a[si].is_empty() { (f64::NAN, f64::NAN) } else { (pr.tier_a[si][0], pr.tier_a[si][pr.tier_a[si].len() - 1]) };
            eprintln!("node {} stroke {} uid {:x} cls {} len {:.0} n0 {} n1 {} need {} sup {:?} z {:?} a {:?} secs {:?} snaps {:?} {:?} p0 {:?} p1 {:?}", nn, si, st.uid, st.cls, st.len(), st.n0, st.n1, nd[si],
                      st.sup.iter().map(|u| (u.s0 as i64, u.s1 as i64, u.kind, u.level)).collect::<Vec<_>>(), zz, ta, pr.sections[si], st.snap0.map(|x| (x.trunk, x.s as i64)), st.snap1.map(|x| (x.trunk, x.s as i64)), st.line.at(0.0).0, st.line.at(st.len()).0);
        }
    }
    let mut out = Square { sq, ..Default::default() };
    let mut offs = Vec::new();
    for si in 0..net.strokes.len() {
        if !nd[si] || pr.z[si].is_empty() { continue; }
        let st = &net.strokes[si];
        let len = st.len();
        let h = pr.h[si];
        let m = pr.z[si].len();
        let ins: Vec<bool> = (0..m).map(|i| inside(st.line.at((i as f64 * h).min(len)).0, keep)).collect();
        let (Some(k0), Some(k1)) = (ins.iter().position(|&x| x), ins.iter().rposition(|&x| x)) else { continue };
        let (k0, k1) = (k0.saturating_sub(1), (k1 + 1).min(m - 1));
        let mut ss = SqStroke { uid: st.uid, ..Default::default() };
        for i in k0..=k1 {
            let s = (i as f64 * h).min(len);
            st.offsets(s, &mut offs);
            ss.s.push(s);
            ss.pts.push(st.line.at(s).0);
            ss.z.push(pr.z[si][i]);
            ss.half.push(if offs.is_empty() { 0.0 } else { offs[0].max(-offs[offs.len() - 1]) });
        }
        for p in &st.parts {
            let fid = &net.segs[net.edges[p.edge].seg].fid;
            ss.feats.push((p.s0, p.s1, feature_of(fid).to_string()));
        }
        for u in &st.sup { if u.kind != SUP_GROUND { ss.sup.push((u.s0, u.s1, u.kind, if u.kind == SUP_STRUCT { net::deck_under(*u) - net::LEVEL_MARGIN_M } else { 0.0 })); } }
        let reach = prof::reach_m(&net, si);
        let fade = clampf(0.5 * reach, 30.0, 150.0);
        for &(s0, s1, cid) in &pr.sections[si] {
            let free0 = s0 > 0.5 || !in_comp_at(st.n0, si, cid);
            let free1 = s1 < len - 0.5 || !in_comp_at(st.n1, si, cid);
            ss.secs.push((s0, s1, free0, free1, fade));
        }
        for u in &st.sup { if u.kind != SUP_GROUND { ss.anchors.push(u.s0); ss.anchors.push(u.s1); } }
        for c in &net.crossings { if c.a == si { ss.anchors.push(c.sa); } if c.b == si { ss.anchors.push(c.sb); } }
        ss.anchors.sort_by(|a, b| a.partial_cmp(b).unwrap());
        // (M3) the ground the profile owns: round its structures' ends and the floors it is the upper road of; round
        // the floors it is the lower road of only its digs; elsewhere the plans' own (pins, pins_near)
        for u in &st.sup { if u.kind == SUP_STRUCT { ss.hand.push(u.s0); ss.hand.push(u.s1); } }
        // (and its ends at a node where another stroke's structure ends: the deck end it hands over to)
        for (start, node) in [(true, st.n0), (false, st.n1)] {
            let deck_there = ends[node].iter().any(|&o| o != si && {
                let so = &net.strokes[o];
                let e = if so.n0 == node { 0.0 } else { so.len() };
                so.sup.iter().any(|u| u.kind == SUP_STRUCT && e >= u.s0 - SUP_TOL_M && e <= u.s1 + SUP_TOL_M)
            });
            if deck_there { ss.hand.push(if start { 0.0 } else { len }); }
        }
        for c in &net.crossings {
            let (sa_, sb_) = (if c.a == si { Some(c.sa) } else { None }, if c.b == si { Some(c.sb) } else { None });
            for x in [sa_, sb_].into_iter().flatten() { if c.upper == si { ss.hand.push(x); ss.floors.push(x); } else { ss.lows.push(x); } }
        }
        // (M5) its floors over railway lines: held too (a deck end dropped onto its approach stops there)
        for r in net.rail_floors.iter().filter(|r| r.si == si) { ss.hand.push(r.s); ss.floors.push(r.s); }
        ss.floors.sort_by(|a, b| a.partial_cmp(b).unwrap());
        ss.hand.sort_by(|a, b| a.partial_cmp(b).unwrap());
        ss.lows.sort_by(|a, b| a.partial_cmp(b).unwrap());
        ss.hand_m = clampf(0.5 * reach, HAND_MIN_M, HAND_MAX_M);
        if opts.tunnels && st.sup.iter().any(|u| u.kind == SUP_TUNNEL) {
            let (runs, snaps) = tunnel_record(&net, si, &pr, &ends, g_of);
            // (the portals are where the profile owns the ground: the approach meets the floor there, dug down to it
            // where the profile lies under the plans' own (a lows entry: pins_near's dig, faded over hand_m). As hand
            // entries they kept the plans' ground at the mouth, the tunnel's floor was then lifted onto it (tunnelrec's
            // hand-over) and the streets over it lost their floors: Moscow's Garden Ring under Taganskaya Square
            // 2.4 m under the streets, its tube through them)
            for r in &runs { if r.mouth0 { ss.lows.push(r.s0); } if r.mouth1 { ss.lows.push(r.s1); } }
            ss.lows.sort_by(|a, b| a.partial_cmp(b).unwrap());
            ss.tun = runs;
            ss.tsnap = snaps;
        }
        out.strokes.push(ss);
    }
    out.tunnels = opts.tunnels;
    out.strokes.sort_by(|a, b| a.uid.cmp(&b.uid).then(a.s.first().partial_cmp(&b.s.first()).unwrap()));
    out.index();
    // steps where strokes meet inside the kept area (nodes: every solved end there; noses and joints: the branch's
    // snapped end against its trunk there): what a built road would show
    let (mut steps, mut step_max, mut step_at) = (0usize, 0.0f64, [0.0f64, 0.0f64]);
    let zend = |si: usize, start: bool| -> f64 { let z = &pr.z[si]; if start { z[0] } else { z[z.len() - 1] } };
    for (n, node) in net.nodes.iter().enumerate() {
        if !inside(node.p, keep) || net.fork_at[n].is_some() { continue; }
        let mut zs: Vec<f64> = Vec::new();
        for &o in &ends[n] {
            if !nd[o] || pr.z[o].is_empty() { continue; }
            let st = &net.strokes[o];
            if st.n0 == n { zs.push(zend(o, true)); }
            if st.n1 == n { zs.push(zend(o, false)); }
        }
        if zs.len() < 2 { continue; }
        let (lo, hi) = zs.iter().fold((f64::INFINITY, f64::NEG_INFINITY), |a, &z| (a.0.min(z), a.1.max(z)));
        let d = hi - lo;
        if d > 0.05 { steps += 1; }
        if d > step_max { step_max = d; step_at = node.p; }
        if d > 0.2 && std::env::var("ROADSYS_DEBUG_STEPS").is_ok() {
            eprintln!("step {:.2} at node {} ({:.0},{:.0}) cut {} arms {}", d, n, node.p[0], node.p[1], net.cut[n], node.arms.len());
            for &o in &ends[n] {
                let st = &net.strokes[o];
                for (start, nn) in [(true, st.n0), (false, st.n1)] {
                    if nn != n { continue; }
                    let e = if start { 0.0 } else { st.len() };
                    let su = st.support_at(e);
                    eprintln!("   stroke {} uid {:x} cls {} len {:.0} {} sup {} lvl {} need {} z {:.2} secs {:?} snap {:?}", o, st.uid, st.cls, st.len(), if start { "start" } else { "end" },
                              su.kind, su.level, nd[o], if pr.z[o].is_empty() { f64::NAN } else { zend(o, start) }, pr.sections[o], if start { st.snap0.map(|x| (x.trunk, x.joint)) } else { st.snap1.map(|x| (x.trunk, x.joint)) });
                }
            }
        }
    }
    for (si, st) in net.strokes.iter().enumerate() {
        if !nd[si] || pr.z[si].is_empty() { continue; }
        for (start, sn) in [(true, st.snap0), (false, st.snap1)] {
            let Some(sn) = sn else { continue };
            if !nd[sn.trunk] || pr.z[sn.trunk].is_empty() { continue; }
            let s_b = if start { st.cut0 } else { st.len() - st.cut1 };
            let p = st.line.at(s_b).0;
            if !inside(p, keep) { continue; }
            let d = absf(pr.at(si, s_b) - pr.at(sn.trunk, sn.s));
            if d > 0.2 && std::env::var("ROADSYS_DEBUG_STEPS").is_ok() {
                eprintln!("nose step {:.2} at ({:.0},{:.0}) branch {} cls {} sup {} secs {:?} | trunk {} cls {} sup {} s {:.0} secs {:?} joint {}", d, p[0], p[1], si, st.cls, st.support_at(s_b).kind, pr.sections[si],
                          sn.trunk, net.strokes[sn.trunk].cls, net.strokes[sn.trunk].support_at(sn.s).kind, sn.s, pr.sections[sn.trunk], sn.joint);
            }
            if d > 0.05 { steps += 1; }
            if d > step_max { step_max = d; step_at = p; }
        }
    }
    let t3 = crate::cpu_now();
    let mut h = net::uid_of("square", &[SQ_FORMAT, sq.0 as u64, sq.1 as u64]);
    for st in &out.strokes { h = net::uid_of("st", &[h, st.uid]); for &z in &st.z { h = net::uid_of("z", &[h, z.to_bits()]); } }
    if out.tunnels {        // (T1: the record in the key; squares without it keyed as before)
        h = net::uid_of("tun1", &[h]);
        for st in &out.strokes { for r in &st.tun { h = net::uid_of("run", &[h, r.s0.to_bits(), r.s1.to_bits(), r.kind as u64, r.mouth0 as u64, r.mouth1 as u64, r.cmin.to_bits(), r.hole0.to_bits(), r.hole1.to_bits()]); } }
    }
    out.key = h;
    let ps = &pr.stats;
    let _ = write!(out.json,
        "{{\"format\":{},\"sq\":[{},{}],\"key\":\"{:016x}\",\"segments\":{},\"strokes\":{},\"need\":{},\"kept\":{},\"steps\":{},\"step_max_m\":{:.3},\"step_at\":[{:.0},{:.0}],\
         \"profile\":{{\"tier_a\":{},\"components\":{},\"sections\":{},\"knots\":{},\"biggest_km\":{:.2},\"cap_cuts\":{},\"solves\":{},\"reused\":{},\"factor_fail\":{},\"floors\":{},\"floors_met\":{},\"floors_relaxed\":{},\"floors_measured_out\":{},\"floor_relax_max_m\":{:.3},\"stacked\":{},\"floor_short_max_m\":{:.3},\"grade_viol\":{},\"comfort_viol\":{},\"couple_max_mm\":{:.2}}}}}",
        SQ_FORMAT, sq.0, sq.1, h, place.segs.len(), net.strokes.len(), nd.iter().filter(|&&x| x).count(), out.strokes.len(), steps, step_max, step_at[0], step_at[1],
        ps.tier_a, ps.components, ps.sections, ps.knots, ps.biggest_component_km, ps.cap_cuts, ps.solves, ps.reused, ps.factor_fail,
        ps.crossings, ps.floors_met, ps.floors_relaxed, ps.floors_measured_out, ps.floor_relax_max_m, ps.stacked, ps.floor_short_max_m, ps.grade_viol, ps.comfort_viol, ps.couple_max_mm);
    let _ = write!(out.cpu_json, "{{\"network\":{:.3},\"profile\":{:.3},\"keep\":{:.3},\"total\":{:.3}}}",
                   (t1 - t0) * 1e3, (t2 - t1) * 1e3, (t3 - t2) * 1e3, (t3 - t0) * 1e3);
    out
}

/// The blended height at q (tangent t, on map feature feat; deck: on its structure) over the squares given (each
/// square's weight there; a square that has no answer is left out and the others' weights renormalised; None: none).
pub fn blend(squares: &BTreeMap<(i64, i64), &Square>, q: P2, t: Option<P2>, feat: &str, deck: bool) -> Option<f64> {
    let (mut sw, mut sz) = (0.0f64, 0.0f64);
    for (sq, w) in weights(q) {
        let Some(s) = squares.get(&sq) else { continue };
        if let Some(z) = s.sample(q, t, feat, deck) { sw += w; sz += w * z; }
    }
    if sw > 0.0 { Some(sz / sw) } else { None }
}

pub struct PinRaw { pub feat: String, pub pts: Vec<P2>, pub tan: Vec<P2>, pub w: Vec<f64>, pub share: Vec<f64>, pub half: f64,
                   pub hand: Vec<f64>, pub low: Vec<f64> }

/// The square's terrain targets (see the module's comment): the ground runs of its Tier B sections whose stations lie
/// in the square (and the first one past it on either side: neighbours' lines meet), each run cut where its map feature
/// changes; heights left to the caller (blend).
pub fn pins(sqr: &Square) -> Vec<PinRaw> {
    let b = bounds(sqr.sq);
    let inside = |p: P2| p[0] >= b[0] && p[0] < b[2] && p[1] >= b[1] && p[1] < b[3];
    let mut out = Vec::new();
    for st in &sqr.strokes {
        if st.s.len() < 2 { continue; }
        let (smin, smax) = (st.s[0], st.s[st.s.len() - 1]);
        let tunnels: Vec<(f64, f64)> = st.sup.iter().filter(|u| u.2 == SUP_TUNNEL).map(|u| (u.0, u.1)).collect();
        let near = |s: f64| -> f64 { st.anchors.iter().fold(f64::INFINITY, |m, &x| m.min(absf(s - x))) };
        let hand_w = |s: f64| -> f64 { let d = st.hand.iter().fold(f64::INFINITY, |m, &x| m.min(absf(s - x))); if d.is_finite() { smooth(1.0 - d / st.hand_m.max(1.0)) } else { 0.0 } };
        let low_w = |s: f64| -> f64 { let d = st.lows.iter().fold(f64::INFINITY, |m, &x| m.min(absf(s - x))); if d.is_finite() { smooth(1.0 - d / st.hand_m.max(1.0)) } else { 0.0 } };
        let line_at = |s: f64| -> (P2, P2) {
            let n = st.s.len();
            let k = st.s.partition_point(|&x| x <= s).saturating_sub(1).min(n - 2);
            let l = st.s[k + 1] - st.s[k];
            let u = if l > 0.0 { clampf((s - st.s[k]) / l, 0.0, 1.0) } else { 0.0 };
            (lerp(st.pts[k], st.pts[k + 1], u), unit(sub(st.pts[k + 1], st.pts[k])).unwrap_or([1.0, 0.0]))
        };
        let half_at = |s: f64| -> f64 {
            let n = st.s.len();
            let k = st.s.partition_point(|&x| x <= s).saturating_sub(1).min(n - 2);
            st.half[k].max(st.half[k + 1])
        };
        for &(s0, s1, free0, free1, fade) in &st.secs {
            let (s0c, s1c) = (s0.max(smin), s1.min(smax));
            if s1c - s0c < 1.0 { continue; }
            let mut cuts: Vec<(f64, f64)> = st.sup.iter().filter(|u| u.1 > s0c && u.0 < s1c).map(|u| (u.0 - PIN_END_IN_M, u.1 + PIN_END_IN_M)).collect();
            cuts.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
            let mut runs: Vec<(f64, f64)> = Vec::new();
            let mut a = s0c;
            for (c0, c1) in cuts { if c0 > a { runs.push((a, c0.min(s1c))); } if c1 > a { a = c1; } }
            if a < s1c { runs.push((a, s1c)); }
            for (r0, r1) in runs {
                if r1 - r0 < 1.0 { continue; }
                let mut ss: Vec<f64> = st.s.iter().copied().filter(|&s| s > r0 && s < r1).collect();
                let mut m = 0.0;
                while m <= PIN_FINE_REACH_M {
                    for e in [r0 + m, r1 - m] { if e >= r0 && e <= r1 && near(e) <= PIN_FINE_REACH_M + 1.0 { ss.push(e); } }
                    m += PIN_FINE_M;
                }
                let mut s = r0;
                while s < r1 { ss.push(s); s += PIN_STEP_M; }
                ss.push(r0);
                ss.push(r1);
                ss.sort_by(|a, b| a.partial_cmp(b).unwrap());
                ss.dedup_by(|a, b| absf(*a - *b) < 0.01);
                // the stations in the square, plus the first one out on either side
                let inn: Vec<bool> = ss.iter().map(|&s| inside(line_at(s).0)).collect();
                let mut cur: Option<PinRaw> = None;
                let flush = |cur: &mut Option<PinRaw>, out: &mut Vec<PinRaw>| {
                    if let Some(mut p) = cur.take() { if p.pts.len() >= 2 { p.half = clampf(p.half, 1.5, HALF_MAX_M); out.push(p); } }
                };
                for (k, &s) in ss.iter().enumerate() {
                    let keep = inn[k] || (k > 0 && inn[k - 1]) || (k + 1 < ss.len() && inn[k + 1]);
                    let feat = st.feature_at(s).unwrap_or("").to_string();
                    if !keep || cur.as_ref().map(|c| c.feat != feat).unwrap_or(false) {
                        flush(&mut cur, &mut out);
                        if !keep { continue; }
                    }
                    let (p, t) = line_at(s);
                    let df = { let mut v = f64::INFINITY; if free0 { v = v.min(s - s0); } if free1 { v = v.min(s1 - s); } v };
                    let wf = if df.is_finite() { smooth(df / fade) } else { 1.0 };
                    let dt = tunnels.iter().fold(f64::INFINITY, |m, &(a, b)| m.min(if s < a { a - s } else if s > b { s - b } else { 0.0 }));
                    // (T1: the record's floor is this profile: the approach takes it right up to the portal, no fade)
                    let wt = if sqr.tunnels { 1.0 } else if dt.is_finite() { smooth(dt / TUNNEL_FADE_M) } else { 1.0 };
                    let c = cur.get_or_insert_with(|| PinRaw { feat: feat.clone(), pts: Vec::new(), tan: Vec::new(), w: Vec::new(), share: Vec::new(), half: 0.0, hand: Vec::new(), low: Vec::new() });
                    c.hand.push(hand_w(s));
                    c.low.push(low_w(s));
                    c.pts.push(p);
                    c.tan.push(t);
                    // (never faded at the deck end it hands over to, M3: a section's free end there left the approach 1 m
                    // over the deck's end, Springfield)
                    c.w.push(wf.max(hand_w(s)) * wt);
                    c.share.push(1.0 - SHARE_DROP * near(s).min(SHARE_REACH_M) / SHARE_REACH_M);
                    let hw = half_at(s);
                    if hw > c.half { c.half = hw; }
                }
                flush(&mut cur, &mut out);
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::io::Seg;

    #[test]
    fn the_weights_are_a_partition_of_unity() {
        for &p in &[[10.0, 10.0], [1999.0, 1.0], [2000.0, 2000.0], [-130.0, 2210.0], [3999.9, -1.0], [-2249.0, 250.0]] {
            let w = weights(p);
            let s: f64 = w.iter().map(|x| x.1).sum();
            assert!(absf(s - 1.0) < 1e-12, "{:?} {:?}", p, w);
            for (sq, _) in &w { let b = grow(bounds(*sq), BAND_M); assert!(p[0] >= b[0] && p[0] <= b[2] && p[1] >= b[1] && p[1] <= b[3]); }
        }
        assert_eq!(weights([1000.0, 1000.0]).len(), 1);
    }

    fn seg(fid: &str, pts: &[[f64; 2]], cls: i64, oneway: i64, kmh: f64, cons: &[(&str, usize)], bridge: Option<(f64, f64)>) -> Seg {
        Seg { fid: fid.into(), code: "US".into(), cls_s: "secondary".into(), pave: 7.0, drive: 7.0, inferred: 2, cls, dir: oneway,
              lanes: 2, lfw: -1, lbw: -1, kmh, lane_w: 3.5, pts: pts.to_vec(), cons: cons.iter().map(|(c, v)| (c.to_string(), *v)).collect(),
              bridge: bridge.into_iter().collect(), level: bridge.map(|b| vec![(b.0, b.1, 1.0)]).unwrap_or_default(), ..Default::default() }
    }

    /// A motorway on a bridge over a street near the corner of four squares.
    fn place(rev: bool, dx: f64) -> Place {
        let mut segs = vec![
            seg("a|0", &[[-600.0 + dx, 0.0], [0.0 + dx, 0.0]], 4, 0, 50.0, &[("n0", 1)], None),
            seg("b|0", &[[0.0 + dx, 0.0], [600.0 + dx, 0.0]], 4, 0, 50.0, &[("n0", 0)], None),
            seg("c|0", &[[dx, -600.0], [dx, 0.0]], 4, 0, 50.0, &[("n0", 1)], None),
            seg("m|0", &[[-300.0 + dx, -900.0], [-300.0 + dx, 900.0]], 1, 1, 100.0, &[], Some((0.48, 0.52))),
        ];
        if rev { segs.reverse(); }
        Place { segs, ..Default::default() }
    }

    fn g(p: &[P2]) -> Vec<f64> { p.iter().map(|q| 10.0 + 0.004 * q[0] + 0.002 * q[1]).collect() }

    #[test]
    fn a_square_round_trips_and_answers_the_same_whatever_the_order() {
        prof::store_clear();
        let a = solve(&place(false, 0.0), (-1, 0), &g, Opts::default());
        prof::store_clear();
        let b = solve(&place(true, 0.0), (-1, 0), &g, Opts::default());
        assert_eq!(a.key, b.key);
        assert!(!a.strokes.is_empty(), "{}", a.json);
        let back = Square::from_bytes(&a.to_bytes()).unwrap();
        assert_eq!(back.to_bytes(), a.to_bytes());
        let z = a.sample([-300.0, 0.0], Some([0.0, 1.0]), "m", true).expect("deck sampled");
        assert_eq!(back.sample([-300.0, 0.0], Some([0.0, 1.0]), "m", true), Some(z));
        assert!(a.sample([-300.0, 0.0], Some([0.0, 1.0]), "a", true).is_none());
        assert!(!pins(&a).is_empty());
    }

    fn crossings_between(p: &Place, a: &str, b: &str) -> usize {
        let order: Vec<usize> = (0..p.segs.len()).collect();
        let n = net::build_opts(p, &order, Opts::default());
        let has = |si: usize, f: &str| n.strokes[si].parts.iter().any(|q| feature_of(&n.segs[n.edges[q.edge].seg].fid) == f);
        n.crossings.iter().filter(|c| (has(c.a, a) && has(c.b, b)) || (has(c.a, b) && has(c.b, a))).count()
    }

    /// (M3) A ramp crossing a deck at a shallow angle where its own deck begins 3 m on is beside it, not under it; a
    /// street under the deck keeps its floor; nothing is held over a tunnel.
    #[test]
    fn decks_side_by_side_and_tunnels_take_no_floor() {
        let mut segs = vec![
            seg("m|0", &[[-300.0, 0.0], [300.0, 0.0]], 1, 1, 100.0, &[], Some((1.0 / 3.0, 2.0 / 3.0))),
            // (crosses y = 0 at x = -40 at ~6 deg, its own deck from x = -37 on)
            seg("r|0", &[[-340.0, -31.5], [-37.0, 0.3], [60.0, 10.5]], 1, 1, 100.0, &[], Some((0.758, 1.0))),
            seg("s|0", &[[0.0, -300.0], [0.0, 300.0]], 4, 0, 50.0, &[], None),
        ];
        let p = Place { segs: segs.clone(), ..Default::default() };
        assert_eq!(crossings_between(&p, "m", "r"), 0, "beside the deck");
        assert!(crossings_between(&p, "m", "s") >= 1, "the street under the deck");
        segs[2].tunnel = vec![(0.4, 0.6)];
        segs[2].level = vec![(0.4, 0.6, -1.0)];
        let p = Place { segs, ..Default::default() };
        assert_eq!(crossings_between(&p, "m", "s"), 0, "over a tunnel");
    }

    /// (M3) Two viaducts side by side at two OSM levels take no floor between them; one over the other's pavement does.
    #[test]
    fn a_deck_beside_another_is_not_over_it() {
        let twin = |dy: f64| {
            let mut a = seg("u|0", &[[-300.0, 0.0], [300.0, 0.0]], 1, 1, 100.0, &[], Some((0.1, 0.9)));
            a.level = vec![(0.1, 0.9, 2.0)];
            let b = seg("l|0", &[[-300.0, dy], [300.0, dy]], 1, 1, 100.0, &[], Some((0.1, 0.9)));
            Place { segs: vec![a, b], ..Default::default() }
        };
        assert_eq!(crossings_between(&twin(9.0), "u", "l"), 0, "side by side");
        assert!(crossings_between(&twin(0.5), "u", "l") > 10, "a double deck");
    }

    /// (M3) A floor takes the structure's drawn depth (+ LEVEL_MARGIN_M) and today's headroom.
    #[test]
    fn a_floor_is_the_lower_roads_clearance_and_the_decks_depth() {
        // (M5) by the lower road's class (a US secondary: 4.5 m), its maxheight sign where it has one (3.8 m + 0.25 m),
        // a footway's 2.5 m
        let floor = |cls: &str, maxh: Vec<(f64, f64, f64)>| {
            let mut m = seg("m|0", &[[-300.0, 0.0], [300.0, 0.0]], 1, 1, 100.0, &[], Some((1.0 / 3.0, 2.0 / 3.0)));
            m.depth = 1.2;
            let mut lo = seg("s|0", &[[0.0, -300.0], [0.0, 300.0]], 4, 0, 50.0, &[], None);
            lo.cls_s = cls.into();
            lo.maxh = maxh;
            let p = Place { segs: vec![m, lo], ..Default::default() };
            let order: Vec<usize> = (0..p.segs.len()).collect();
            let n = net::build_opts(&p, &order, Opts::default());
            n.crossings.iter().find(|c| !c.along).expect("a crossing").floor
        };
        let d = 1.2 + net::LEVEL_MARGIN_M;
        assert!(absf(floor("secondary", vec![]) - (4.5 + d)) < 1e-9);
        assert!(absf(floor("secondary", vec![(0.45, 0.55, 3.8)]) - (3.8 + net::SIGN_MARGIN_M + d)) < 1e-9);
        assert!(absf(floor("secondary", vec![(0.0, 0.1, 3.8)]) - (4.5 + d)) < 1e-9, "a sign elsewhere on the road");
        assert!(absf(floor("footway", vec![]) - (2.5 + d)) < 1e-9);
        assert!(absf(floor("motorway", vec![]) - (4.9 + d)) < 1e-9);
    }

    #[test]
    fn neighbour_squares_agree_inside_their_shared_band() {
        // the bridge 100 m west of the line x = 0 between squares (-1, 0) and (0, 0): both see all of it
        let p = place(false, 200.0);
        let a = solve(&p, (-1, 0), &g, Opts::default());
        let b = solve(&p, (0, 0), &g, Opts::default());
        let (za, zb) = (a.sample([-100.0, 0.0], Some([0.0, 1.0]), "m", true).unwrap(), b.sample([-100.0, 0.0], Some([0.0, 1.0]), "m", true).unwrap());
        assert!(absf(za - zb) < 0.01, "{} {}", za, zb);
        let mut m: BTreeMap<(i64, i64), &Square> = BTreeMap::new();
        m.insert((-1, 0), &a);
        m.insert((0, 0), &b);
        let z = blend(&m, [-100.0, 0.0], Some([0.0, 1.0]), "m", true).unwrap();
        assert!(absf(z - za) < 0.01);
    }

    /// (T1, 631) With the tunnel record a street on the ground over a tunnel holds a floor; a deck over it does not (a
    /// covered roadway, T4: its floor moved Seal Beach's connector decks onto each other).
    #[test]
    fn with_the_record_a_street_over_a_tunnel_holds_a_floor_and_a_deck_does_not() {
        let floors = |deck: bool| {
            let up = seg("m|0", &[[-300.0, 0.0], [300.0, 0.0]], 1, 1, 100.0, &[], if deck { Some((1.0 / 3.0, 2.0 / 3.0)) } else { None });
            let mut lo = seg("s|0", &[[0.0, -300.0], [0.0, 300.0]], 4, 0, 50.0, &[], None);
            lo.tunnel = vec![(0.4, 0.6)];
            lo.level = vec![(0.4, 0.6, -1.0)];
            let p = Place { segs: vec![up, lo], ..Default::default() };
            let order: Vec<usize> = (0..p.segs.len()).collect();
            net::build_opts(&p, &order, Opts { tunnels: true, ..Default::default() }).crossings.iter().filter(|c| !c.along).count()
        };
        assert_eq!(floors(false), 1);
        assert_eq!(floors(true), 0);
    }

    /// (T1, 631) The record's portals are where the profile digs the approach to its floor (lows: pins_near lowers the
    /// plans' ground there), not where the plans' own ground is kept (hand): kept, the hand-over lifted the tunnel onto
    /// the plans' approach and the streets over it lost their floors (Moscow's Garden Ring).
    #[test]
    fn a_tunnels_portals_dig_the_approach() {
        let mut t = seg("t|0", &[[100.0, 1000.0], [1900.0, 1000.0]], 4, 0, 60.0, &[], None);
        t.tunnel = vec![(0.4, 0.6)];
        t.level = vec![(0.4, 0.6, -1.0)];
        let p = Place { segs: vec![t], ..Default::default() };
        let hill = |pts: &[P2]| -> Vec<f64> { pts.iter().map(|q| 10.0 + (40.0 - 0.2 * absf(q[0] - 1000.0)).max(0.0)).collect() };
        prof::store_clear();
        let sq = solve(&p, (0, 0), &hill, Opts { tunnels: true, ..Default::default() });
        let st = sq.strokes.iter().find(|s| !s.tun.is_empty()).expect("a tunnel record");
        let mouths: Vec<f64> = st.tun.iter().flat_map(|r| [(r.mouth0, r.s0), (r.mouth1, r.s1)]).filter(|m| m.0).map(|m| m.1).collect();
        assert!(!mouths.is_empty(), "{:?}", st.tun);
        for m in mouths {
            assert!(st.lows.iter().any(|&x| absf(x - m) < 1e-9), "{} not in lows {:?}", m, st.lows);
            assert!(!st.hand.iter().any(|&x| absf(x - m) < 1e-9), "{} in hand {:?}", m, st.hand);
        }
    }
}
