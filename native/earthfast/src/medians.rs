//! Median barriers on divided highways (server/medians.py): where a motorway's or trunk's two one-way carriageways run
//! side by side, a barrier down the median between them by its width (a concrete wall, a cable barrier or a steel
//! guard rail; the style table is the caller's), as 3 m Forest modules the road tiles' guard rails are drawn with.
//!
//! Deterministic and tile-independent: modules lie on a grid of arc length along one carriageway (the one whose key is
//! the lower of the pair), and a tile keeps the modules whose middle is in it, so neighbouring tiles agree.

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use std::collections::HashMap;

/// Flags per line (`flags`): bit 0 a candidate (a motorway / trunk one-way carriageway, no ramp), bit 1 a ramp (link),
/// bit 2 travel against the geometry, bit 3 urban, bit 4 three lanes or more, bit 5 a motorway (else a trunk), bit 6
/// a bridge deck (its vertices' heights given: decks pair with decks at their level only, ground with ground).
const F_CAND: i32 = 1;
#[allow(dead_code)]
const F_LINK: i32 = 2;      // (set by the caller; a ramp is no candidate, so it gets the opening)
const F_REV: i32 = 4;
const F_URBAN: i32 = 8;
const F_WIDE: i32 = 16;
const F_MOTORWAY: i32 = 32;
const F_DECK: i32 = 64;
const DECK_DZ: f64 = 1.0;   // m: two decks' surfaces this near in height are one level
const TYPE_SPAN: f64 = 12.0; // m either side of a module whose widest median picks its type
const MIN_RUN: usize = 10;   // modules a run has at least (shorter: none: a 12 m stub of cable stood in a grass gore
                             // between two of I-95's roadways in Baltimore; real runs are far longer)

/// Barrier types (the style table's codes): none, a concrete (New Jersey) wall, a cable barrier, a double-sided steel
/// guard rail (two rails back to back, STEEL_SPREAD m either side of the middle), two single steel rails, one along
/// each carriageway's edge (RAILS_EDGE m outside it, their backs to the median). Their half widths (m) for the clearance
/// test (the two rails': their spread and a rail's depth).
pub const T_NONE: i32 = 0;
pub const T_JERSEY: i32 = 1;
pub const T_CABLE: i32 = 2;
pub const T_STEEL: i32 = 3;
pub const T_RAILS: i32 = 4;
const STEEL_SPREAD: f64 = 0.25;
const RAILS_EDGE: f64 = 0.75;
const RAIL_DEPTH: f64 = 0.2;
fn half_width(t: i32, spread: f64) -> f64 {
    match t {
        T_JERSEY => 0.25,
        T_CABLE => 0.08,
        T_STEEL | T_RAILS => spread + RAIL_DEPTH,
        _ => 0.0,
    }
}

/// The two rails' spread (m either side of the middle) in a median gap m wide.
fn spread_of(t: i32, gap: f64) -> f64 {
    if t == T_RAILS { (gap / 2.0 - RAILS_EDGE).max(STEEL_SPREAD) } else { STEEL_SPREAD }
}

struct Net<'a> {
    pts: &'a [[f64; 2]],
    z: &'a [f64],
    // segments: (line, first vertex) and a bucket grid over them
    seg_line: Vec<u32>,
    seg_v: Vec<u32>,
    cell: f64,
    grid: HashMap<(i32, i32), Vec<u32>>,
}

impl<'a> Net<'a> {
    fn new(pts: &'a [[f64; 2]], z: &'a [f64], off: &'a [usize], cell: f64) -> Self {
        let mut seg_line = Vec::new();
        let mut seg_v = Vec::new();
        let mut grid: HashMap<(i32, i32), Vec<u32>> = HashMap::new();
        for l in 0..off.len().saturating_sub(1) {
            for v in off[l]..off[l + 1].saturating_sub(1) {
                let (a, b) = (pts[v], pts[v + 1]);
                let id = seg_line.len() as u32;
                seg_line.push(l as u32);
                seg_v.push(v as u32);
                let (x0, x1) = (a[0].min(b[0]), a[0].max(b[0]));
                let (y0, y1) = (a[1].min(b[1]), a[1].max(b[1]));
                for gx in (x0 / cell).floor() as i32..=(x1 / cell).floor() as i32 {
                    for gy in (y0 / cell).floor() as i32..=(y1 / cell).floor() as i32 {
                        grid.entry((gx, gy)).or_default().push(id);
                    }
                }
            }
        }
        Net { pts, z, seg_line, seg_v, cell, grid }
    }

    /// Per line within r of p: (distance, nearest point, the segment's unit direction, the segment's vertex,
    /// the nearest point's parameter on it), the nearest segment of each line.
    fn near(&self, p: [f64; 2], r: f64, stamp: &mut Vec<u32>, tick: &mut u32) -> Vec<(usize, f64, [f64; 2], [f64; 2], usize, f64)> {
        *tick = tick.wrapping_add(1);
        if *tick == 0 {
            stamp.iter_mut().for_each(|s| *s = 0);
            *tick = 1;
        }
        let mut best: Vec<(usize, f64, [f64; 2], [f64; 2], usize, f64)> = Vec::new();
        let c = self.cell;
        for gx in ((p[0] - r) / c).floor() as i32..=((p[0] + r) / c).floor() as i32 {
            for gy in ((p[1] - r) / c).floor() as i32..=((p[1] + r) / c).floor() as i32 {
                let Some(list) = self.grid.get(&(gx, gy)) else { continue };
                for &s in list {
                    let si = s as usize;
                    if stamp[si] == *tick {
                        continue;
                    }
                    stamp[si] = *tick;
                    let v = self.seg_v[si] as usize;
                    let (a, b) = (self.pts[v], self.pts[v + 1]);
                    let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
                    let l2 = dx * dx + dy * dy;
                    if l2 < 1e-12 {
                        continue;
                    }
                    let t = (((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2).clamp(0.0, 1.0);
                    let q = [a[0] + t * dx, a[1] + t * dy];
                    let d = (p[0] - q[0]).hypot(p[1] - q[1]);
                    if d > r {
                        continue;
                    }
                    let ln = l2.sqrt();
                    let u = [dx / ln, dy / ln];
                    let line = self.seg_line[si] as usize;
                    match best.iter_mut().find(|e| e.0 == line) {
                        Some(e) => {
                            if d < e.1 {
                                *e = (line, d, q, u, v, t);
                            }
                        }
                        None => best.push((line, d, q, u, v, t)),
                    }
                }
            }
        }
        best
    }
}

fn dot(a: [f64; 2], b: [f64; 2]) -> f64 {
    a[0] * b[0] + a[1] * b[1]
}

/// The point and unit direction at arc length s along line l (cum: its cumulative arc lengths), extrapolated past
/// its ends along the end segments. The direction is smooth: the vertices' (their two segments' mean) blended along
/// each segment, so an offset from the line does not jump at a vertex (19 m out a 4 degree bend moved the median's
/// middle 1.3 m: a gap in the wall, I-10 in Phoenix).
fn at(pts: &[[f64; 2]], a: usize, b: usize, cum: &[f64], s: f64) -> ([f64; 2], [f64; 2]) {
    let n = b - a;
    let mut k = match cum.binary_search_by(|x| x.partial_cmp(&s).unwrap_or(std::cmp::Ordering::Less)) {
        Ok(i) => i,
        Err(i) => i.saturating_sub(1),
    };
    k = k.min(n - 2);
    // a zero-length segment: the next real one
    let mut k2 = k;
    while k2 + 1 < n - 1 && cum[k2 + 1] - cum[k2] < 1e-9 {
        k2 += 1;
    }
    let seg = |j: usize| -> Option<[f64; 2]> {
        let (p, q) = (pts[a + j], pts[a + j + 1]);
        let l = (q[0] - p[0]).hypot(q[1] - p[1]);
        if l < 1e-9 { None } else { Some([(q[0] - p[0]) / l, (q[1] - p[1]) / l]) }
    };
    let (p, q) = (pts[a + k2], pts[a + k2 + 1]);
    let len = (cum[k2 + 1] - cum[k2]).max(1e-9);
    let u = [(q[0] - p[0]) / len, (q[1] - p[1]) / len];
    let t = s - cum[k2];
    // the vertices' directions: the mean of the segments either side (the nearest real ones)
    let vdir = |v: usize| -> [f64; 2] {
        let before = (0..v).rev().find_map(seg);
        let after = (v..n - 1).find_map(seg);
        match (before, after) {
            (Some(x), Some(y)) => {
                let m = [x[0] + y[0], x[1] + y[1]];
                let l = m[0].hypot(m[1]);
                if l < 1e-6 { u } else { [m[0] / l, m[1] / l] }
            }
            (Some(x), None) | (None, Some(x)) => x,
            _ => u,
        }
    };
    let f = (t / len).clamp(0.0, 1.0);
    let (d0, d1) = (vdir(k2), vdir(k2 + 1));
    let m = [d0[0] * (1.0 - f) + d1[0] * f, d0[1] * (1.0 - f) + d1[1] * f];
    let ml = m[0].hypot(m[1]);
    let sm = if ml < 1e-6 { u } else { [m[0] / ml, m[1] / ml] };
    ([p[0] + u[0] * t, p[1] + u[1] * t], sm)
}

/// One station's barrier: Some((type, centre, median width)) or why none: Err(true) where the pair goes on but another
/// carriageway owns it (no end there), Err(false) where there is no barrier.
struct Ctx<'a> {
    net: Net<'a>,
    half: &'a [f64],
    flags: &'a [i32],
    keys: &'a [i64],
    allow: &'a [f64],
    mapped: Vec<[f64; 4]>,
    mapped_grid: HashMap<(i32, i32), Vec<u32>>,
    style: Vec<[f64; 6]>,
    right_hand: bool,
    max_gap: f64,
    max_deck_gap: f64,
    opening: f64,
    reach: f64,
}

impl<'a> Ctx<'a> {
    fn travel(&self, l: usize, u: [f64; 2]) -> [f64; 2] {
        if self.flags[l] & F_REV != 0 { [-u[0], -u[1]] } else { u }
    }

    /// The type for a median w m wide between carriageways with flags fl (both's, or'ed).
    fn style_of(&self, w: f64, fl: i32) -> i32 {
        for r in &self.style {
            if w <= r[0] {
                let col = if fl & F_MOTORWAY != 0 {
                    if fl & F_URBAN != 0 { 2 } else if fl & F_WIDE != 0 { 3 } else { 1 }
                } else if fl & F_URBAN != 0 { 5 } else { 4 };
                return r[col] as i32;
            }
        }
        T_NONE
    }

    fn mapped_near(&self, c: [f64; 2], r: f64, t: [f64; 2]) -> bool {
        if self.mapped.is_empty() {
            return false;
        }
        let cell = 16.0;
        for gx in ((c[0] - r) / cell).floor() as i32..=((c[0] + r) / cell).floor() as i32 {
            for gy in ((c[1] - r) / cell).floor() as i32..=((c[1] + r) / cell).floor() as i32 {
                let Some(list) = self.mapped_grid.get(&(gx, gy)) else { continue };
                for &k in list {
                    let m = self.mapped[k as usize];
                    let (dx, dy) = (m[2] - m[0], m[3] - m[1]);
                    let l2 = dx * dx + dy * dy;
                    if l2 < 1e-9 {
                        continue;
                    }
                    let tt = (((c[0] - m[0]) * dx + (c[1] - m[1]) * dy) / l2).clamp(0.0, 1.0);
                    let d = (c[0] - m[0] - tt * dx).hypot(c[1] - m[1] - tt * dy);
                    let ln = l2.sqrt();
                    if d <= r && (dot([dx / ln, dy / ln], t)).abs() > 0.8 {
                        return true;
                    }
                }
            }
        }
        false
    }

    /// Whether line l's point (segment v at t) is at a's level: both on the ground, or decks at one height (za).
    fn level(&self, a_line: usize, za: f64, l: usize, v: usize, t: f64) -> bool {
        let deck = self.flags[a_line] & F_DECK;
        if self.flags[l] & F_DECK != deck {
            return false;
        }
        deck == 0 || (self.net.z[v] * (1.0 - t) + self.net.z[v + 1] * t - za).abs() <= DECK_DZ
    }

    /// The other carriageway beside line a_line's point p (direction u; za: a deck's height there, NaN on the ground):
    /// (its line, the median's width m between their edges, its height there).
    fn partner(&self, a_line: usize, p: [f64; 2], u: [f64; 2], za: f64, stamp: &mut Vec<u32>, tick: &mut u32)
               -> Option<(usize, f64, f64)> {
        let ta = self.travel(a_line, u);
        let deck = self.flags[a_line] & F_DECK;
        // the median's side: the driver's left (right-hand traffic), else the right
        let n = if self.right_hand { [-ta[1], ta[0]] } else { [ta[1], -ta[0]] };
        let ha = self.half[a_line];
        let reach = ha + self.max_gap + 16.0;
        let near = self.net.near(p, reach, stamp, tick);
        let mut best: Option<(f64, usize, f64, f64)> = None;     // (centre lines apart, line, gap, its height)
        for &(l, _d, q, ub, v, t) in &near {
            if l == a_line || self.flags[l] & F_CAND == 0 || !self.level(a_line, za, l, v, t) {
                continue;
            }
            let tb = self.travel(l, ub);
            if dot(tb, ta) > -0.9 {
                continue;
            }
            let dq = [q[0] - p[0], q[1] - p[1]];
            let lat = dot(dq, n);
            let along = dot(dq, ta).abs();
            if lat <= 0.0 || along > 0.5 + 0.15 * lat {
                continue;
            }
            // (apart: to the other's nearest point, a distance that changes smoothly where the nearest point jumps)
            let apart = dq[0].hypot(dq[1]);
            let gap = apart - ha - self.half[l];
            // (decks further apart than the bridges' median join have their own parapets: none between them)
            if gap > if deck != 0 { self.max_deck_gap } else { self.max_gap } {
                continue;
            }
            if best.map_or(true, |b| apart < b.0) {
                let zb = if deck != 0 { self.net.z[v] * (1.0 - t) + self.net.z[v + 1] * t } else { f64::NAN };
                best = Some((apart, l, gap, zb));
            }
        }
        best.map(|(_apart, l, gap, zb)| (l, gap, zb))
    }

    /// One station's barrier. type_gap: the median width its type is chosen by (the widest near it: no short
    /// stretch of another type where the median narrows for a few metres, A-4 near Almuradiel), NaN: its own.
    /// Ok: (type, centre, gap, spread, the centre's height: the decks' mean, NaN on the ground).
    #[allow(clippy::too_many_arguments)]
    fn station(&self, a_line: usize, p: [f64; 2], u: [f64; 2], za: f64, type_gap: f64, stamp: &mut Vec<u32>,
               tick: &mut u32) -> Result<(i32, [f64; 2], f64, f64, f64), bool> {
        let ta = self.travel(a_line, u);
        let deck = self.flags[a_line] & F_DECK;
        let n = if self.right_hand { [-ta[1], ta[0]] } else { [ta[1], -ta[0]] };
        let ha = self.half[a_line];
        let Some((b_line, gap, zb)) = self.partner(a_line, p, u, za, stamp, tick) else { return Err(false) };
        let tg = if type_gap.is_finite() { type_gap.max(gap) } else { gap };
        let ty = self.style_of(tg, self.flags[a_line] | self.flags[b_line]);
        if ty == T_NONE {
            return Err(false);
        }
        if self.keys[a_line] > self.keys[b_line] {
            return Err(true);                       // the other carriageway's
        }
        let spread = spread_of(ty, gap);
        let hw = half_width(ty, spread);
        let c = [p[0] + n[0] * (ha + gap / 2.0), p[1] + n[1] * (ha + gap / 2.0)];
        // clear of every carriageway: the pair's (and their pieces running alongside) by its width less their edge
        // allowance; any other road (a ramp, a crossover, a crossing road) by an opening
        let r = hw + self.opening + 16.0;
        for &(l, d, _q, ul, v, t) in &self.net.near(c, r, stamp, tick) {
            if !self.level(a_line, za, l, v, t) {
                continue;                           // (a bridge over the median, the road under a viaduct)
            }
            let par = self.flags[l] & F_CAND != 0 && dot(ul, ta).abs() > 0.9;
            let need = if l == a_line || l == b_line || par {
                hw - self.allow[l]
            } else {
                hw + self.opening
            };
            if d - self.half[l] < need {
                return Err(false);
            }
        }
        // a mapped barrier along the median stands for it
        if self.mapped_near(c, (gap / 2.0 + 0.5).max(self.reach), ta) {
            return Err(false);
        }
        Ok((ty, c, gap, spread, if deck != 0 { (za + zb) / 2.0 } else { f64::NAN }))
    }
}

/// Median barrier modules of the road tile box bx (x0, y0, x1, y1).
/// Lines: every carriageway near the tile, vertices pts (n, 2) with offsets off (m + 1); per line its half width, flags
/// (F_*), key (a tile-independent order: the lower key of a pair carries its barrier), allow (m its pavement's edge may
/// be under the barrier: the shoulder). mapped (k, 4): mapped barrier segments [ax, ay, bx, by] (none of ours along
/// them). style (s, 6): rows [max median width m, then the type of a rural motorway, an urban motorway, a rural motorway
/// of three lanes or more, a rural trunk, an urban trunk] by width (the first row the median is not wider than; past
/// the last none). params: [right_hand, max_gap, opening m around other roads, mapped reach m, module length m, max
/// gap m between two decks (wider: each has its parapet)]. z (n,): the vertices' heights (the decks'; anything on the
/// ground). Returns (k, 11): [type, cap (0, 1 the run's start, 2 its end), ax, ay, bx, by (the median's middle line),
/// rank 0..1, filler (1: the piece's end-aligned last module, overlapping the one before), spread (m: the rails'
/// either side), za, zb (on a deck its height at a and b, NaN on the ground)].
#[pyfunction]
pub fn median_modules<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                           off: PyReadonlyArray1<'py, i64>,
                           half: PyReadonlyArray1<'py, f64>, flags: PyReadonlyArray1<'py, i32>,
                           keys: PyReadonlyArray1<'py, i64>, allow: PyReadonlyArray1<'py, f64>,
                           mapped: PyReadonlyArray2<'py, f64>, style: PyReadonlyArray2<'py, f64>,
                           params: Vec<f64>, bx: [f64; 4]) -> Bound<'py, PyArray2<f64>> {
    let pa = pts.as_array();
    let pts_v: Vec<[f64; 2]> = (0..pa.nrows()).map(|i| [pa[[i, 0]], pa[[i, 1]]]).collect();
    let off_v: Vec<usize> = off.as_array().iter().map(|&x| x as usize).collect();
    let z_v: Vec<f64> = z.as_array().to_vec();
    let half_v: Vec<f64> = half.as_array().to_vec();
    let flags_v: Vec<i32> = flags.as_array().to_vec();
    let keys_v: Vec<i64> = keys.as_array().to_vec();
    let allow_v: Vec<f64> = allow.as_array().to_vec();
    let ma = mapped.as_array();
    let mapped_v: Vec<[f64; 4]> = (0..ma.nrows()).map(|i| [ma[[i, 0]], ma[[i, 1]], ma[[i, 2]], ma[[i, 3]]]).collect();
    let sa = style.as_array();
    let style_v: Vec<[f64; 6]> = (0..sa.nrows())
        .map(|i| [sa[[i, 0]], sa[[i, 1]], sa[[i, 2]], sa[[i, 3]], sa[[i, 4]], sa[[i, 5]]]).collect();
    let out = py.detach(|| {
        modules(&pts_v, &z_v, &off_v, &half_v, &flags_v, &keys_v, &allow_v, mapped_v, style_v, &params, bx)
    });
    let k = out.len() / 11;
    numpy::ndarray::Array2::from_shape_vec((k, 11), out).expect("modules").into_pyarray(py)
}

fn hash01(a: i64, b: i64) -> f64 {
    let mut x = (a as u64) ^ (b as u64).wrapping_mul(0x9E3779B97F4A7C15);
    x ^= x >> 30;
    x = x.wrapping_mul(0xBF58476D1CE4E5B9);
    x ^= x >> 27;
    x = x.wrapping_mul(0x94D049BB133111EB);
    x ^= x >> 31;
    (x >> 11) as f64 / (1u64 << 53) as f64
}

#[allow(clippy::too_many_arguments)]
pub fn modules(pts: &[[f64; 2]], z: &[f64], off: &[usize], half: &[f64], flags: &[i32], keys: &[i64], allow: &[f64],
               mapped: Vec<[f64; 4]>, style: Vec<[f64; 6]>, params: &[f64], bx: [f64; 4]) -> Vec<f64> {
    let nl = off.len().saturating_sub(1);
    let mut out = Vec::new();
    if nl == 0 || style.is_empty() {
        return out;
    }
    let right_hand = params.first().copied().unwrap_or(1.0) > 0.5;
    let max_gap = params.get(1).copied().unwrap_or(20.0);
    let opening = params.get(2).copied().unwrap_or(4.0);
    let reach = params.get(3).copied().unwrap_or(1.5);
    let ml = params.get(4).copied().unwrap_or(3.0);
    let max_deck_gap = params.get(5).copied().unwrap_or(6.0);
    let mut mapped_grid: HashMap<(i32, i32), Vec<u32>> = HashMap::new();
    for (k, m) in mapped.iter().enumerate() {
        let (x0, x1) = (m[0].min(m[2]), m[0].max(m[2]));
        let (y0, y1) = (m[1].min(m[3]), m[1].max(m[3]));
        for gx in (x0 / 16.0).floor() as i32..=(x1 / 16.0).floor() as i32 {
            for gy in (y0 / 16.0).floor() as i32..=(y1 / 16.0).floor() as i32 {
                mapped_grid.entry((gx, gy)).or_default().push(k as u32);
            }
        }
    }
    let ctx = Ctx { net: Net::new(pts, z, off, 24.0), half, flags, keys, allow, mapped, mapped_grid, style, right_hand,
                    max_gap, max_deck_gap, opening, reach };
    let mut stamp = vec![0u32; ctx.net.seg_line.len()];
    let mut tick = 0u32;
    let pad = 40.0;
    for a_line in 0..nl {
        if flags[a_line] & F_CAND == 0 {
            continue;
        }
        let (a, b) = (off[a_line], off[a_line + 1]);
        if b < a + 2 {
            continue;
        }
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for p in &pts[a..b] {
            x0 = x0.min(p[0]);
            y0 = y0.min(p[1]);
            x1 = x1.max(p[0]);
            y1 = y1.max(p[1]);
        }
        if x1 < bx[0] - pad || x0 > bx[2] + pad || y1 < bx[1] - pad || y0 > bx[3] + pad {
            continue;
        }
        let mut cum = vec![0.0f64; b - a];
        for i in 1..b - a {
            cum[i] = cum[i - 1] + (pts[a + i][0] - pts[a + i - 1][0]).hypot(pts[a + i][1] - pts[a + i - 1][1]);
        }
        let len = cum[b - a - 1];
        if len < ml {
            continue;
        }
        // the modules' starts: a grid of ml from the line's start, the last one aligned to its end (a filler)
        let nfull = (len / ml).floor() as usize;
        let mut starts: Vec<(f64, bool)> = (0..nfull).map(|k| (k as f64 * ml, false)).collect();
        if len - nfull as f64 * ml > 0.3 {
            starts.push((len - ml, true));
        }
        // whether the line goes on into another carriageway of the same direction at its start / end (no cap there)
        let joins = |p: [f64; 2], t: [f64; 2]| -> bool {
            for l in 0..nl {
                if l == a_line || flags[l] & F_CAND == 0 || off[l + 1] < off[l] + 2 {
                    continue;
                }
                let (s, e) = (off[l], off[l + 1] - 1);
                for (q, u) in [(pts[s], [pts[s + 1][0] - pts[s][0], pts[s + 1][1] - pts[s][1]]),
                               (pts[e], [pts[e][0] - pts[e - 1][0], pts[e][1] - pts[e - 1][1]])] {
                    let ln = u[0].hypot(u[1]).max(1e-9);
                    let tl = ctx.travel(l, [u[0] / ln, u[1] / ln]);
                    if (p[0] - q[0]).hypot(p[1] - q[1]) < 0.5 && dot(tl, t) > 0.9 {
                        return true;
                    }
                }
            }
            false
        };
        // a deck's height at arc length s (NaN on the ground)
        let z_at = |s: f64| -> f64 {
            if flags[a_line] & F_DECK == 0 {
                return f64::NAN;
            }
            let s = s.clamp(0.0, len);
            let k = cum.partition_point(|&x| x <= s).clamp(1, b - a - 1);
            let span = (cum[k] - cum[k - 1]).max(1e-9);
            let f = ((s - cum[k - 1]) / span).clamp(0.0, 1.0);
            z[a + k - 1] * (1.0 - f) + z[a + k] * f
        };
        // per module: Ok((type, ca, cb, spread, za, zb)), Err(continues: the barrier goes on, not ours)
        let mut evals: Vec<Result<(i32, [f64; 2], [f64; 2], f64, f64, f64), bool>> = Vec::with_capacity(starts.len());
        let mut near_box: Vec<bool> = Vec::with_capacity(starts.len());
        for &(s0, _) in &starts {
            let (pm, _) = at(pts, a, b, &cum, s0 + ml / 2.0);
            let inb = pm[0] >= bx[0] - pad && pm[0] <= bx[2] + pad && pm[1] >= bx[1] - pad && pm[1] <= bx[3] + pad;
            near_box.push(inb);
            if !inb {
                evals.push(Err(false));
                continue;
            }
            let mut res: Result<(i32, [f64; 2], [f64; 2], f64, f64, f64), bool> = Err(false);
            let mut cs = [[0.0; 2]; 3];
            let mut zs = [f64::NAN; 3];
            let mut spread = STEEL_SPREAD;
            let mut ty0 = T_NONE;
            let mut ok = true;
            let mut cont = false;
            // the type by the widest median within TYPE_SPAN of the module
            let mut type_gap = f64::NAN;
            for s in [s0 - TYPE_SPAN, s0 + ml / 2.0, s0 + ml + TYPE_SPAN] {
                let sc = s.clamp(0.0, len);
                let (p, u) = at(pts, a, b, &cum, sc);
                if let Some((_, g, _)) = ctx.partner(a_line, p, u, z_at(sc), &mut stamp, &mut tick) {
                    type_gap = if type_gap.is_finite() { type_gap.max(g) } else { g };
                }
            }
            for (j, s) in [s0, s0 + ml / 2.0, s0 + ml].into_iter().enumerate() {
                let (p, u) = at(pts, a, b, &cum, s.clamp(0.0, len));
                match ctx.station(a_line, p, u, z_at(s), type_gap, &mut stamp, &mut tick) {
                    Ok((ty, c, _w, sp, zc)) => {
                        zs[j] = zc;
                        if j > 0 && ty != ty0 {
                            ok = false;
                        }
                        ty0 = ty;
                        cs[j] = c;
                        if j == 1 {
                            spread = sp;
                        }
                    }
                    Err(c) => {
                        ok = false;
                        cont |= c;
                    }
                }
            }
            if ok {
                let mid = [(cs[0][0] + cs[2][0]) / 2.0, (cs[0][1] + cs[2][1]) / 2.0];
                let chord = (cs[2][0] - cs[0][0]).hypot(cs[2][1] - cs[0][1]);
                if (cs[1][0] - mid[0]).hypot(cs[1][1] - mid[1]) < 0.5 && chord > 0.8 * ml && chord < 1.2 * ml {
                    res = Ok((ty0, cs[0], cs[2], spread, zs[0], zs[2]));
                }
            } else if cont {
                res = Err(true);
            }
            evals.push(res);
        }
        let ends = (at(pts, a, b, &cum, 0.0), at(pts, a, b, &cum, len));
        for (k, &(_s0, filler)) in starts.iter().enumerate() {
            let Ok((ty, ca, cb, spread, za, zb)) = evals[k] else { continue };
            let mid = [(ca[0] + cb[0]) / 2.0, (ca[1] + cb[1]) / 2.0];
            if !(mid[0] >= bx[0] && mid[0] < bx[2] && mid[1] >= bx[1] && mid[1] < bx[3]) {
                continue;
            }
            // a run's end: the module before / after it has no barrier (not another carriageway's, not the next
            // piece of this one)
            let gone = |j: Option<usize>, at_end: bool| -> bool {
                match j {
                    Some(j) if j < evals.len() => match evals[j] {
                        Ok((t2, _, _, _, _, _)) => t2 != ty,
                        Err(cont) => !cont && near_box[j],
                    },
                    _ => {
                        let (p, u) = if at_end { ends.1 } else { ends.0 };
                        !joins(p, ctx.travel(a_line, u))
                    }
                }
            };
            let prev = k.checked_sub(1);
            let next = if k + 1 < starts.len() { Some(k + 1) } else { None };
            let (g0, g1) = (gone(prev, false), gone(next, true));
            // the run's modules from k on in a direction, up to MIN_RUN (one going on past what is known: long)
            let run = |dir: i64| -> usize {
                let mut j = k as i64;
                for n in 0..MIN_RUN {
                    let nj = j + dir;
                    let nxt = if nj >= 0 && (nj as usize) < starts.len() { Some(nj as usize) } else { None };
                    if gone(nxt, dir > 0) {
                        return n;
                    }
                    match nxt {
                        Some(x) if evals[x].is_ok() => j = nj,
                        _ => return MIN_RUN,
                    }
                }
                MIN_RUN
            };
            if 1 + run(-1) + run(1) < MIN_RUN {
                continue;                           // a stub (a lone module or a few): no run
            }
            let cap = if g0 { 1.0 } else if g1 { 2.0 } else { 0.0 };
            out.extend_from_slice(&[ty as f64, cap, ca[0], ca[1], cb[0], cb[1], hash01(keys[a_line], k as i64),
                                    if filler { 1.0 } else { 0.0 }, spread, za, zb]);
        }
    }
    out
}

/// Forest rows (trees.lua's 17 numbers, as guardrails.forest_rows makes them) for median modules m (k, >= 9) with their
/// base heights za, zb (k,) at their ends: [group, x, y, z, yaw, 0, height, rank, col0 (3), col1 (3), col2 (3)], the
/// columns the model's axes in the world. Groups (guardrails.GROUPS): 0 / 3 a steel rail facing right / left of the
/// line (the two faces of a double-sided rail, or the two edge rails: the spread either side), 6 a concrete module, 7
/// its sloped end, 8 a cable module. The concrete models run along their y axis, the rest
/// along x. A filler module sits 1 cm aside and up (no flicker where it overlaps the one before).
#[pyfunction]
pub fn median_rows<'py>(py: Python<'py>, m: PyReadonlyArray2<'py, f64>, za: PyReadonlyArray1<'py, f64>,
                        zb: PyReadonlyArray1<'py, f64>) -> Bound<'py, PyArray2<f64>> {
    let (m, za, zb) = (m.as_array(), za.as_array(), zb.as_array());
    let mut out: Vec<f64> = Vec::new();
    for i in 0..m.nrows() {
        let ty = m[[i, 0]] as i32;
        let cap = m[[i, 1]] as i32;
        let (ax, ay, bx_, by) = (m[[i, 2]], m[[i, 3]], m[[i, 4]], m[[i, 5]]);
        let rank = m[[i, 6]];
        let filler = m[[i, 7]] > 0.5;
        let spread = m[[i, 8]];
        let (z0, z1) = (za[i], zb[i]);
        if !z0.is_finite() || !z1.is_finite() {
            continue;
        }
        let f = [bx_ - ax, by - ay, z1 - z0];
        let fl = (f[0] * f[0] + f[1] * f[1] + f[2] * f[2]).sqrt();
        if fl < 1e-6 {
            continue;
        }
        let fw = [f[0] / fl, f[1] / fl, f[2] / fl];
        let mut up = [-fw[2] * fw[0], -fw[2] * fw[1], 1.0 - fw[2] * fw[2]];
        let ul = (up[0] * up[0] + up[1] * up[1] + up[2] * up[2]).sqrt();
        if ul < 1e-6 {
            continue;
        }
        up = [up[0] / ul, up[1] / ul, up[2] / ul];
        // back = up x forward (the rail's back: its left)
        let back = [up[1] * fw[2] - up[2] * fw[1], up[2] * fw[0] - up[0] * fw[2], up[0] * fw[1] - up[1] * fw[0]];
        let (mut px, mut py_, mut pz) = ((ax + bx_) / 2.0, (ay + by) / 2.0, (z0 + z1) / 2.0);
        if filler {
            px += back[0] * 0.01;
            py_ += back[1] * 0.01;
            pz += 0.01;
        }
        let yaw = fw[1].atan2(fw[0]).to_degrees();
        let neg = |v: [f64; 3]| [-v[0], -v[1], -v[2]];
        let mut row = |g: f64, x: f64, y: f64, h: f64, c0: [f64; 3], c1: [f64; 3]| {
            out.extend_from_slice(&[g, x, y, pz, yaw, 0.0, h, rank, c0[0], c0[1], c0[2], c1[0], c1[1], c1[2],
                                    up[0], up[1], up[2]]);
        };
        match ty {
            T_JERSEY => {
                // along the model's y: col0 = -back, col1 = forward; its sloped end's nose at +y: a run's start
                // turned round (col1 = -forward, col0 = back)
                if cap == 1 {
                    row(7.0, px, py_, 0.88, back, neg(fw));
                } else if cap == 2 {
                    row(7.0, px, py_, 0.88, neg(back), fw);
                } else {
                    row(6.0, px, py_, 0.88, neg(back), fw);
                }
            }
            T_CABLE => row(8.0, px, py_, 1.0, fw, back),
            T_STEEL | T_RAILS => {
                // two faced rails back to back (guardrails' two_sided): the plain one to the right, its face out;
                // the reversed one to the left
                let s = spread;
                row(0.0, px - back[0] * s, py_ - back[1] * s, 0.9, fw, back);
                row(3.0, px + back[0] * s, py_ + back[1] * s, 0.9, neg(fw), neg(back));
            }
            _ => {}
        }
    }
    let k = out.len() / 17;
    numpy::ndarray::Array2::from_shape_vec((k, 17), out).expect("rows").into_pyarray(py)
}
