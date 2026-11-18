//! Road surface wear (roadwear.py; the user, 2026-11-10: "lets use as many assets as needed to give this world detail
//! and variety"): where a tile's paved roads carry the stock maps' wear decals, as pieces laid along their centre
//! lines. Tyre polish in the lanes' wheel paths (busier roads), a concrete road's slab joints (OSM surface=concrete:
//! the transverse joints every few metres, the longitudinal ones between the lanes), sealed and alligator cracks,
//! single crack lines (a paving joint along the middle, transverse thermal cracks out of town), repair patches,
//! potholes (poor roads: OSM smoothness where mapped), manhole covers on town streets and oil spots in a parking lot's
//! stalls. Deterministic: every road's pieces come from its own seed and its distance along its whole centre line, so
//! two tiles that share a road lay the same pieces; a tile only builds the ones near its box.
//!
//! Each piece's polyline is cut where another carriageway lies (its junctions; the caller's excluded pairs are not
//! blockers: a road's straight continuation, a ramp's merge) and where it leaves the box: those cuts are hard, a strip
//! piece fades in and out at its own ends only (roadmesh's fade uv, from each point's distance along the piece and the
//! piece's length), so neighbouring tiles agree.
use crate::dm::Dm;

use std::collections::{HashMap, HashSet};

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

// attrs' columns, per road
const A_HW: usize = 0;          // half the carriageway's width (its blocker: CUT margin added by the caller)
const A_DRIVE: usize = 1;       // the driving width (roads.lane_layout)
const A_DOFF: usize = 2;        // the lanes' offset from the centre line (+ left: roads.driving_offset)
const A_LANES: usize = 3;       // lanes, both ways
const A_ONEWAY: usize = 4;
const A_CLS: usize = 5;         // CLS_*
const A_CONCRETE: usize = 6;
const A_URBAN: usize = 7;
const A_COND: usize = 8;        // the surface's condition 0 (ruined) .. 1 (new) from OSM smoothness, < 0 unknown
const A_SEED: usize = 9;
const A_WEAR: usize = 10;       // 1: the road gets wear (a paved ground road of its own), 0: a blocker only
const A_AISLE: usize = 11;      // 1: a parking lot's aisle (oil in its stalls)
const A_BLOCK: usize = 12;      // its blocker's half width (A_HW + the caller's margin)
const A_LEAF_L: usize = 13;     // fallen leaves along its left edge (the season's fall x the trees beside it, 0..1)
const A_LEAF_R: usize = 14;     // ... and its right edge
pub const NA: usize = 15;

// road classes (attrs[A_CLS])
const CLS_MOTORWAY: i64 = 0;
const CLS_TERTIARY: i64 = 4;
const CLS_MINOR: i64 = 5;
const CLS_SERVICE: i64 = 6;

// piece kinds (the output's column 0; roadwear.py's KINDS)
pub const K_TREAD: i64 = 0;     // tyre polish along a lane (strip, fades)
pub const K_SEALED: i64 = 1;    // tar-sealed crack network (strip, fades)
pub const K_CRACKS: i64 = 2;    // alligator cracking in a wheel path (strip, fades)
pub const K_LINE: i64 = 3;      // a crack / joint line along the road (thin strip)
pub const K_ACROSS: i64 = 4;    // a crack / joint line across the road (thin strip, its line from left to right)
pub const K_PATCH: i64 = 5;     // a repair patch (quad, atlas cell)
pub const K_POTHOLE: i64 = 6;   // (quad, atlas cell)
pub const K_MANHOLE: i64 = 7;   // (quad, atlas cell)
pub const K_OIL: i64 = 8;       // (quad)
pub const K_LEAVES: i64 = 9;    // fallen leaves along an edge (quad)

const TEST_STEP_M: f64 = 1.5;   // m between a piece's points tested against the blockers and the box
const SIMPLIFY_M: f64 = 0.02;   // m a dropped point may lie off its run's kept line
const BUCKET_M: f64 = 32.0;     // the blockers' grid
const BOX_PAD_M: f64 = 3.0;     // m past the box a piece is still laid (the mesh is cut at the box exactly)
const SECTION_M: f64 = 250.0;   // m of road per condition section (a stretch paved at one time)

const TRAFFIC: [f64; 8] = [1.0, 0.9, 0.75, 0.6, 0.45, 0.2, 0.12, 0.3];
const BASE_COND: [f64; 8] = [0.72, 0.66, 0.58, 0.52, 0.47, 0.42, 0.38, 0.5];

#[inline]
fn mix(mut z: u64) -> u64 {
    z = z.wrapping_add(0x9E37_79B9_7F4A_7C15);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    z ^ (z >> 31)
}

/// splitmix64 stream of one road's kind (its seed and a stream number): the same numbers wherever the road is built.
struct Rng(u64);

impl Rng {
    fn new(seed: u64, stream: u64) -> Self { Rng(mix(seed ^ mix(stream.wrapping_add(0x5157)))) }
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        mix(self.0)
    }
    fn f(&mut self) -> f64 { (self.next() >> 11) as f64 / (1u64 << 53) as f64 }
    fn range(&mut self, a: f64, b: f64) -> f64 { a + (b - a) * self.f() }
    fn pick(&mut self, n: usize) -> usize { ((self.f() * n as f64) as usize).min(n.saturating_sub(1)) }
}

/// A road's centre line: points and their distance along it.
struct Line {
    x: Vec<f64>,
    y: Vec<f64>,
    z: Vec<f64>,
    s: Vec<f64>,
}

impl Line {
    fn new(x: Vec<f64>, y: Vec<f64>, z: Vec<f64>) -> Self {
        let mut s = vec![0.0; x.len()];
        for i in 1..x.len() {
            s[i] = s[i - 1] + (x[i] - x[i - 1]).dhypot(y[i] - y[i - 1]);
        }
        Line { x, y, z, s }
    }
    fn len(&self) -> f64 { *self.s.last().unwrap_or(&0.0) }
    /// The segment holding distance s (clamped).
    fn seg(&self, s: f64) -> usize {
        let n = self.s.len();
        if n < 2 { return 0; }
        match self.s.binary_search_by(|v| v.total_cmp(&s)) {
            Ok(i) => i.min(n - 2),
            Err(i) => i.saturating_sub(1).min(n - 2),
        }
    }
    /// (x, y, z, unit tangent x, y) at s.
    fn at(&self, s: f64) -> (f64, f64, f64, f64, f64) {
        let i = self.seg(s);
        let (dx, dy) = (self.x[i + 1] - self.x[i], self.y[i + 1] - self.y[i]);
        let l = dx.dhypot(dy).max(1e-9);
        let t = ((s - self.s[i]) / l).clamp(0.0, 1.0);
        (self.x[i] + t * dx, self.y[i] + t * dy, self.z[i] + t * (self.z[i + 1] - self.z[i]), dx / l, dy / l)
    }
    /// The left normal at vertex i, mitred as roads.offset_polyline (scale 1 / cos of the half turn, at most 2).
    fn vnormal(&self, i: usize) -> (f64, f64) {
        let n = self.x.len();
        let segn = |k: usize| {
            let (dx, dy) = (self.x[k + 1] - self.x[k], self.y[k + 1] - self.y[k]);
            let l = dx.dhypot(dy).max(1e-9);
            (-dy / l, dx / l)
        };
        if i == 0 { return segn(0); }
        if i >= n - 1 { return segn(n - 2); }
        let (a, b) = (segn(i - 1), segn(i));
        let (mx, my) = (a.0 + b.0, a.1 + b.1);
        let ml = mx.dhypot(my);
        if ml < 1e-9 { return b; }
        let (mx, my) = (mx / ml, my / ml);
        let c = (mx * b.0 + my * b.1).clamp(0.5, 1.0);
        (mx / c, my / c)
    }
    /// The line from s0 to s1 moved d m to its left: (x, y, z, distance along this path from its start).
    fn path(&self, s0: f64, s1: f64, d: f64) -> Vec<[f64; 4]> {
        let mut out: Vec<[f64; 3]> = Vec::new();
        let (x, y, z, tx, ty) = self.at(s0);
        out.push([x - ty * d, y + tx * d, z]);
        let (i0, i1) = (self.seg(s0), self.seg(s1));
        for i in (i0 + 1)..=i1 {
            if self.s[i] > s0 + 1e-6 && self.s[i] < s1 - 1e-6 {
                let (nx, ny) = self.vnormal(i);
                out.push([self.x[i] + nx * d, self.y[i] + ny * d, self.z[i]]);
            }
        }
        let (x, y, z, tx, ty) = self.at(s1);
        out.push([x - ty * d, y + tx * d, z]);
        let mut acc = 0.0;
        let mut res = Vec::with_capacity(out.len());
        for (k, p) in out.iter().enumerate() {
            if k > 0 {
                acc += (p[0] - out[k - 1][0]).dhypot(p[1] - out[k - 1][1]);
            }
            res.push([p[0], p[1], p[2], acc]);
        }
        res
    }
}

/// The carriageways as blockers: segments in a grid, each with its road and half width.
struct Blockers {
    segs: Vec<[f64; 4]>,
    owner: Vec<usize>,
    hw: Vec<f64>,
    grid: HashMap<(i64, i64), Vec<u32>>,
    excl: HashSet<(usize, usize)>,
}

impl Blockers {
    fn new(lines: &[Line], half: &[f64], excl: HashSet<(usize, usize)>) -> Self {
        let mut b = Blockers { segs: Vec::new(), owner: Vec::new(), hw: Vec::new(), grid: HashMap::new(), excl };
        for (r, ln) in lines.iter().enumerate() {
            let h = half[r];
            if !(h > 0.0) { continue; }
            for i in 0..ln.x.len().saturating_sub(1) {
                let k = b.segs.len() as u32;
                let sg = [ln.x[i], ln.y[i], ln.x[i + 1], ln.y[i + 1]];
                let (gx0, gx1) = (((sg[0].min(sg[2]) - h) / BUCKET_M).floor() as i64,
                                  ((sg[0].max(sg[2]) + h) / BUCKET_M).floor() as i64);
                let (gy0, gy1) = (((sg[1].min(sg[3]) - h) / BUCKET_M).floor() as i64,
                                  ((sg[1].max(sg[3]) + h) / BUCKET_M).floor() as i64);
                if (gx1 - gx0 + 1) * (gy1 - gy0 + 1) > 4096 { continue; }     // (a broken coordinate)
                for gx in gx0..=gx1 {
                    for gy in gy0..=gy1 {
                        b.grid.entry((gx, gy)).or_default().push(k);
                    }
                }
                b.segs.push(sg);
                b.owner.push(r);
                b.hw.push(h);
            }
        }
        b
    }
    /// Is (x, y) on another road's carriageway (road r's own and its excluded pairs left out)?
    fn blocked(&self, r: usize, x: f64, y: f64) -> bool {
        let key = ((x / BUCKET_M).floor() as i64, (y / BUCKET_M).floor() as i64);
        let Some(list) = self.grid.get(&key) else { return false };
        for &k in list {
            let k = k as usize;
            let o = self.owner[k];
            if o == r || self.excl.contains(&(r, o)) { continue; }
            let sg = &self.segs[k];
            let (dx, dy) = (sg[2] - sg[0], sg[3] - sg[1]);
            let l2 = (dx * dx + dy * dy).max(1e-12);
            let t = (((x - sg[0]) * dx + (y - sg[1]) * dy) / l2).clamp(0.0, 1.0);
            if (x - (sg[0] + t * dx)).dhypot(y - (sg[1] + t * dy)) < self.hw[k] {
                return true;
            }
        }
        false
    }
}

/// The output: points (x, y, z, distance along the piece) and per piece (kind, width, texture length, length of the
/// whole piece (fade; -1: none), atlas cell or -1).
#[derive(Default)]
struct Out {
    pts: Vec<[f64; 4]>,
    off: Vec<i64>,
    attrs: Vec<[f64; 5]>,
}

struct Ctx<'a> {
    bl: &'a Blockers,
    bx: [f64; 4],
}

impl Ctx<'_> {
    fn in_box(&self, x: f64, y: f64, pad: f64) -> bool {
        x >= self.bx[0] - pad && x <= self.bx[2] + pad && y >= self.bx[1] - pad && y <= self.bx[3] + pad
    }
}

/// Points every TEST_STEP_M at most along a path (all four columns interpolated).
fn densify(p: &[[f64; 4]]) -> Vec<[f64; 4]> {
    let mut out = Vec::with_capacity(p.len() * 2);
    for i in 0..p.len() {
        if i > 0 {
            let (a, b) = (p[i - 1], p[i]);
            let d = (b[0] - a[0]).dhypot(b[1] - a[1]);
            let n = (d / TEST_STEP_M).ceil() as usize;
            for k in 1..n {
                let t = k as f64 / n as f64;
                out.push([a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), a[2] + t * (b[2] - a[2]),
                          a[3] + t * (b[3] - a[3])]);
            }
        }
        out.push(p[i]);
    }
    out
}

/// Ramer-Douglas-Peucker on xy, the ends kept.
fn simplify(p: &[[f64; 4]]) -> Vec<[f64; 4]> {
    let n = p.len();
    if n < 3 { return p.to_vec(); }
    let mut keep = vec![false; n];
    keep[0] = true;
    keep[n - 1] = true;
    let mut stack = vec![(0usize, n - 1)];
    while let Some((i, j)) = stack.pop() {
        if j <= i + 1 { continue; }
        let (ax, ay, bx, by) = (p[i][0], p[i][1], p[j][0], p[j][1]);
        let (dx, dy) = (bx - ax, by - ay);
        let l = dx.dhypot(dy);
        let (mut best, mut at) = (-1.0f64, i);
        for k in (i + 1)..j {
            let d = if l < 1e-9 { (p[k][0] - ax).dhypot(p[k][1] - ay) }
                    else { ((p[k][0] - ax) * dy - (p[k][1] - ay) * dx).abs() / l };
            if d > best { best = d; at = k; }
        }
        if best > SIMPLIFY_M {
            keep[at] = true;
            stack.push((i, at));
            stack.push((at, j));
        }
    }
    p.iter().zip(keep).filter(|(_, k)| *k).map(|(q, _)| *q).collect()
}

/// A strip piece of road r: its path, cut where blocked or out of the box, its runs (at least min_m long) out.
fn emit_strip(ctx: &Ctx, out: &mut Out, r: usize, path: Vec<[f64; 4]>, kind: i64, width: f64, tl: f64, fade: bool,
              min_m: f64) {
    if path.len() < 2 { return; }
    let slen = path[path.len() - 1][3];
    if slen < min_m.max(0.05) { return; }
    let pad = BOX_PAD_M + width / 2.0;
    // (a piece entirely away from the box: nothing to test)
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for q in &path {
        x0 = x0.min(q[0]); x1 = x1.max(q[0]); y0 = y0.min(q[1]); y1 = y1.max(q[1]);
    }
    if x1 < ctx.bx[0] - pad || x0 > ctx.bx[2] + pad || y1 < ctx.bx[1] - pad || y0 > ctx.bx[3] + pad { return; }
    let dense = densify(&path);
    let ok: Vec<bool> = dense.iter().map(|q| ctx.in_box(q[0], q[1], pad) && !ctx.bl.blocked(r, q[0], q[1])).collect();
    let mut i = 0;
    while i < dense.len() {
        if !ok[i] { i += 1; continue; }
        let mut j = i;
        while j + 1 < dense.len() && ok[j + 1] { j += 1; }
        if j > i && dense[j][3] - dense[i][3] >= min_m {
            let run = simplify(&dense[i..=j]);
            out.pts.extend_from_slice(&run);
            out.off.push(out.pts.len() as i64);
            out.attrs.push([kind as f64, width, tl, if fade { slen } else { -1.0 }, -1.0]);
        }
        i = j + 1;
    }
}

/// A quad (spot) of road r centred at (x, y) along unit (tx, ty), turned by `turn` rad: kept when its centre is in
/// the box and on no other carriageway (`free`: and not on its own either, a lot's stall beside its aisle).
#[allow(clippy::too_many_arguments)]
fn emit_quad(ctx: &Ctx, out: &mut Out, r: usize, c: (f64, f64, f64), t: (f64, f64), turn: f64, along: f64,
             across: f64, kind: i64, cell: i64) {
    let size = along.max(across);
    if !ctx.in_box(c.0, c.1, size / 2.0) || ctx.bl.blocked(r, c.0, c.1) { return; }
    let (cs, sn) = (turn.dcos(), turn.dsin());
    let (ux, uy) = (t.0 * cs - t.1 * sn, t.0 * sn + t.1 * cs);   // along
    let (vx, vy) = (-uy, ux);                                   // left
    let (a, b) = (along / 2.0, across / 2.0);
    // corners: front left, front right, back right, back left (markings.arrow_quad's order); column 3: corner index
    for (k, (fa, fb)) in [(a, b), (a, -b), (-a, -b), (-a, b)].iter().enumerate() {
        out.pts.push([c.0 + ux * fa + vx * fb, c.1 + uy * fa + vy * fb, c.2, k as f64]);
    }
    out.off.push(out.pts.len() as i64);
    out.attrs.push([kind as f64, across, along, -1.0, cell as f64]);
}

/// The [s0, s1] windows of a line within pad of the box (a long road outside a tile is skipped quickly).
fn windows(ln: &Line, bx: &[f64; 4], pad: f64) -> Vec<(f64, f64)> {
    let mut out: Vec<(f64, f64)> = Vec::new();
    for i in 0..ln.x.len().saturating_sub(1) {
        let (x0, x1) = (ln.x[i].min(ln.x[i + 1]), ln.x[i].max(ln.x[i + 1]));
        let (y0, y1) = (ln.y[i].min(ln.y[i + 1]), ln.y[i].max(ln.y[i + 1]));
        if x1 < bx[0] - pad || x0 > bx[2] + pad || y1 < bx[1] - pad || y0 > bx[3] + pad { continue; }
        let (a, b) = (ln.s[i], ln.s[i + 1]);
        match out.last_mut() {
            Some(w) if w.1 >= a - 1e-9 => w.1 = b,
            _ => out.push((a, b)),
        }
    }
    out
}

fn overlaps(w: &[(f64, f64)], a: f64, b: f64) -> bool { w.iter().any(|&(s0, s1)| b >= s0 && a <= s1) }

/// The road's condition at s (0 ruined .. 1 new): its own (mapped smoothness, else seeded by class) and, per
/// SECTION_M stretch, a seeded jitter (stretches resurfaced at different times).
fn condition(base: f64, mapped: bool, seed: u64, s: f64) -> f64 {
    let k = (s / SECTION_M).floor() as i64;
    let mut rng = Rng::new(seed, 0x1000 + (k as u64));
    let j = if mapped { 0.05 } else { 0.12 };
    (base + rng.range(-j, j)).clamp(0.02, 0.98)
}

/// One road's pieces (tier 1: tyre polish and concrete joints; tier 2: everything).
fn road_pieces(ctx: &Ctx, out: &mut Out, r: usize, ln: &Line, a: &[f64], tier: i64) {
    let len = ln.len();
    if len < 2.0 { return; }
    let seed = a[A_SEED] as u64;
    let cls = (a[A_CLS] as i64).clamp(0, 7);
    let traffic = TRAFFIC[cls as usize];
    let lanes = (a[A_LANES].round() as i64).max(1);
    let drive = a[A_DRIVE].max(1.0).min(2.0 * a[A_HW].max(0.5));
    let doff = a[A_DOFF];
    let hw = a[A_HW];
    let lw = drive / lanes as f64;
    let concrete = a[A_CONCRETE] > 0.5;
    let urban = a[A_URBAN] > 0.5;
    let aisle = a[A_AISLE] > 0.5;
    let mapped = a[A_COND] >= 0.0;
    let base = if mapped { a[A_COND] } else {
        let mut rng = Rng::new(seed, 1);
        (BASE_COND[cls as usize] + rng.range(-0.25, 0.25)).clamp(0.02, 0.98)
    };
    let pad = hw + 6.0;
    let win = windows(ln, &ctx.bx, pad);
    if win.is_empty() { return; }
    let lane_c = |k: i64| doff - drive / 2.0 + (k as f64 + 0.5) * lw;

    // tyre polish: a strip along each lane in runs (the texture's two darker bands: the wheel paths)
    if tier >= 1 && traffic >= 0.4 && !aisle {
        let w = (lw * 0.92).min(3.6);
        for k in 0..lanes {
            let mut rng = Rng::new(seed, 0x100 + k as u64);
            let mut s = rng.range(-40.0, 10.0);
            while s < len {
                let run = if traffic >= 0.75 { rng.range(120.0, 400.0) } else { rng.range(50.0, 220.0) };
                let (s0, s1) = (s.max(0.0), (s + run).min(len));
                if s1 - s0 > 8.0 && overlaps(&win, s0, s1) {
                    emit_strip(ctx, out, r, ln.path(s0, s1, lane_c(k)), K_TREAD, w, w * 4.0, true, 2.0);
                }
                s += run + rng.range(-12.0, 25.0 + 60.0 * (1.0 - traffic));
            }
        }
    }
    if concrete {
        if tier >= 1 {
            // transverse joints every J m (4.5-6 m slabs), the whole carriageway across; the lanes' joints along
            let mut rng = Rng::new(seed, 0x200);
            let j = [4.6, 5.0, 5.5, 6.1][rng.pick(4)];
            let mut s = rng.range(0.3, j);
            let reach = (hw - 0.2).max(0.5);
            while s < len - 0.3 {
                if overlaps(&win, s - 1.0, s + 1.0) {
                    let (x, y, z, tx, ty) = ln.at(s);
                    let (nx, ny) = (-ty, tx);
                    let p = vec![[x + nx * reach, y + ny * reach, z, 0.0], [x - nx * reach, y - ny * reach, z, 2.0 * reach]];
                    emit_strip(ctx, out, r, p, K_ACROSS, 0.2, 6.4, false, 0.8);
                }
                s += j;
            }
            for k in 1..lanes {
                let d = doff - drive / 2.0 + k as f64 * lw;
                for &(w0, w1) in &win {
                    emit_strip(ctx, out, r, ln.path(w0.max(0.0), w1.min(len), d), K_LINE, 0.2, 6.4, false, 1.0);
                }
            }
        }
        if tier >= 2 {
            // a replaced slab here and there: an asphalt repair patch over one lane's slab
            let mut rng = Rng::new(seed, 0x210);
            let mut s = rng.range(20.0, 200.0);
            while s < len {
                let c = condition(base, mapped, seed, s);
                // (every draw made whatever the box: the next pieces' numbers are the same in every tile)
                let (roll, k, cell) = (rng.f(), rng.pick(lanes as usize) as i64, rng.pick(8) as i64);
                if roll < 0.6 * (1.0 - c) && overlaps(&win, s - 4.0, s + 4.0) {
                    let (x, y, z, tx, ty) = ln.at(s);
                    let d = lane_c(k);
                    emit_quad(ctx, out, r, (x - ty * d, y + tx * d, z), (tx, ty), 0.0, lw * 0.9, lw * 0.9, K_PATCH, cell);
                }
                s += rng.range(40.0, 260.0);
            }
        }
    } else if tier >= 2 {
        // sealed crack networks: maintained roads of middling age, the whole width or a lane
        {
            let mut rng = Rng::new(seed, 0x300);
            let mut s = rng.range(0.0, 120.0);
            while s < len {
                let c = condition(base, mapped, seed, s);
                let sealed = (1.0 - ((c - 0.5).abs() * 2.5)).clamp(0.0, 1.0);
                let piece = rng.range(14.0, 70.0);
                let whole = rng.f() < 0.5;
                let k = rng.pick(lanes as usize) as i64;
                let roll = rng.f();
                if sealed > 0.15 && roll < sealed && overlaps(&win, s, s + piece) {
                    let (w, d) = if whole { (drive * 0.95, doff) } else { (lw * 0.95, lane_c(k)) };
                    emit_strip(ctx, out, r, ln.path(s, (s + piece).min(len), d), K_SEALED, w, w * 4.0, true, 2.0);
                }
                s += piece + rng.range(30.0, 220.0) / (0.4 + sealed);
            }
        }
        // alligator cracks in a wheel path: poor surfaces
        {
            let mut rng = Rng::new(seed, 0x400);
            let mut s = rng.range(0.0, 60.0);
            while s < len {
                let c = condition(base, mapped, seed, s);
                let sev = ((0.45 - c) / 0.45).clamp(0.0, 1.0);
                let piece = rng.range(5.0, 22.0) * (0.6 + sev);
                let k = rng.pick(lanes as usize) as i64;
                let wheel = if rng.f() < 0.5 { -lw / 4.0 } else { lw / 4.0 };
                let w = rng.range(1.2, (lw * 0.8).clamp(1.3, 2.6));
                let roll = rng.f();
                if sev > 0.0 && roll < 0.35 + 0.65 * sev && overlaps(&win, s, s + piece) {
                    emit_strip(ctx, out, r, ln.path(s, (s + piece).min(len), lane_c(k) + wheel), K_CRACKS, w, w * 4.0,
                               true, 1.5);
                }
                s += piece + rng.range(20.0, 160.0) * (1.25 - sev);
            }
        }
        // crack lines: the paving joint along the middle of a two-way road; transverse cracks out of town
        if a[A_ONEWAY] < 0.5 && lanes >= 2 {
            let mut rng = Rng::new(seed, 0x500);
            let mut s = rng.range(0.0, 80.0);
            while s < len {
                let c = condition(base, mapped, seed, s);
                let piece = rng.range(8.0, 60.0);
                let d = doff + rng.range(-0.3, 0.3);
                let roll = rng.f();
                if c < 0.6 && roll < 1.2 * (0.6 - c) / 0.6 + 0.2 && overlaps(&win, s, s + piece) {
                    emit_strip(ctx, out, r, ln.path(s, (s + piece).min(len), d), K_LINE, 0.25, 8.0, false, 1.0);
                }
                s += piece + rng.range(15.0, 140.0);
            }
        }
        if !urban && cls >= 2 {
            let mut rng = Rng::new(seed, 0x600);
            let mut s = rng.range(5.0, 40.0);
            while s < len {
                let c = condition(base, mapped, seed, s);
                let reach = drive / 2.0;
                let (a0, a1) = (rng.range(-reach, reach * 0.2), rng.range(-reach * 0.2, reach));
                let roll = rng.f();
                if c < 0.5 && roll < 0.25 + (0.5 - c) && overlaps(&win, s - 1.0, s + 1.0) && a1 - a0 > 0.8 {
                    let (x, y, z, tx, ty) = ln.at(s);
                    let (nx, ny) = (-ty, tx);
                    let (l, rr) = (doff + a1, doff + a0);
                    let p = vec![[x + nx * l, y + ny * l, z, 0.0], [x + nx * rr, y + ny * rr, z, l - rr]];
                    emit_strip(ctx, out, r, p, K_ACROSS, 0.25, 8.0, false, 0.6);
                }
                s += rng.range(8.0, 40.0);
            }
        }
    }
    if tier >= 2 && !concrete {
        // repair patches (utility cuts in town, patched damage everywhere)
        let mut rng = Rng::new(seed, 0x700);
        let mut s = rng.range(0.0, 90.0);
        while s < len {
            let c = condition(base, mapped, seed, s);
            let size = rng.range(1.0, 2.6);
            let off = rng.range(-1.0, 1.0) * (drive / 2.0 - size / 2.0).max(0.0);
            let cell = rng.pick(8) as i64;
            let turn = if rng.f() < 0.5 { 0.0 } else { std::f64::consts::PI };
            let p = if urban { 0.55 } else { 0.35 } * (1.1 - c);
            let roll = rng.f();
            if roll < p && overlaps(&win, s - 3.0, s + 3.0) {
                let (x, y, z, tx, ty) = ln.at(s);
                let d = doff + off;
                emit_quad(ctx, out, r, (x - ty * d, y + tx * d, z), (tx, ty), turn, size, size, K_PATCH, cell);
            }
            s += rng.range(18.0, 120.0);
        }
    }
    if tier >= 2 && (cls >= CLS_TERTIARY) && !aisle {
        // potholes: the worst surfaces only (mapped bad smoothness, or seeded among the minor roads)
        let mut rng = Rng::new(seed, 0x800);
        let mut s = rng.range(0.0, 40.0);
        while s < len {
            let c = condition(base, mapped, seed, s);
            let size = rng.range(0.5, 1.2);
            let k = rng.pick(lanes as usize) as i64;
            let wheel = rng.range(-lw / 3.0, lw / 3.0);
            let cell = rng.pick(4) as i64;
            let turn = rng.range(0.0, std::f64::consts::TAU);
            let roll = rng.f();
            if c < 0.22 && roll < (0.22 - c) / 0.22 + 0.15 && overlaps(&win, s - 2.0, s + 2.0) {
                let (x, y, z, tx, ty) = ln.at(s);
                let d = lane_c(k) + wheel;
                emit_quad(ctx, out, r, (x - ty * d, y + tx * d, z), (tx, ty), turn, size, size, K_POTHOLE, cell);
            }
            s += rng.range(12.0, 70.0);
        }
    }
    if tier >= 2 && urban && (2..=CLS_MINOR).contains(&cls) {
        // manhole covers and small lids in a town street's lanes or its middle
        let mut rng = Rng::new(seed, 0x900);
        let mut s = rng.range(5.0, 60.0);
        while s < len {
            let roll = rng.f();
            // atlas cells: 0 square cover, 1 round cover, 2 plate, 3-6 the small lids and grates
            let (cell, size) = if roll < 0.6 { (1, 0.95) } else if roll < 0.75 { (0, 0.9) } else if roll < 0.85 {
                (2, 1.0) } else { (3 + rng.pick(4) as i64, 0.5) };
            let mid = rng.f() < 0.4;
            let k = rng.pick(lanes as usize) as i64;
            let d = if mid { doff } else { lane_c(k) + rng.range(-0.3, 0.3) };
            if overlaps(&win, s - 1.0, s + 1.0) {
                let (x, y, z, tx, ty) = ln.at(s);
                emit_quad(ctx, out, r, (x - ty * d, y + tx * d, z), (tx, ty), 0.0, size, size, K_MANHOLE, cell);
            }
            s += rng.range(35.0, 95.0);
        }
    }
    if tier >= 2 && aisle {
        // oil spots in a lot's stalls, either side of its aisle (a car's engine ~2 m into a 5.5 m stall)
        let mut rng = Rng::new(seed, 0xA00);
        let stall = 2.7;
        let mut s = rng.range(0.5, stall);
        while s < len {
            for side in [1.0f64, -1.0] {
                let size = rng.range(0.9, 1.7);
                let turn = rng.range(0.0, std::f64::consts::TAU);
                let hit = rng.f();
                if hit < 0.25 && overlaps(&win, s - 2.0, s + 2.0) {
                    let (x, y, z, tx, ty) = ln.at(s);
                    let d = side * (hw + 2.0);
                    emit_quad(ctx, out, r, (x - ty * d, y + tx * d, z), (tx, ty), turn, size, size, K_OIL, -1);
                }
            }
            s += stall;
        }
    } else if tier >= 2 && cls >= CLS_SERVICE {
        // a drip line down the middle of a lot's way or a service road (rarely)
        let mut rng = Rng::new(seed, 0xB00);
        let mut s = rng.range(2.0, 30.0);
        while s < len {
            let (roll, d, size, turn) = (rng.f(), doff + rng.range(-0.5, 0.5), rng.range(0.8, 1.4),
                                         rng.range(0.0, std::f64::consts::TAU));
            if roll < 0.3 && overlaps(&win, s - 2.0, s + 2.0) {
                let (x, y, z, tx, ty) = ln.at(s);
                emit_quad(ctx, out, r, (x - ty * d, y + tx * d, z), (tx, ty), turn, size, size, K_OIL, -1);
            }
            s += rng.range(6.0, 25.0);
        }
    }
    if tier >= 2 {
        // fallen leaves in the autumn under trees: blobs along the edges, denser the more the trees and the season
        for (side, col, stream) in [(1.0f64, A_LEAF_L, 0xC00u64), (-1.0, A_LEAF_R, 0xC01)] {
            let leaf = a[col].clamp(0.0, 1.0);
            if leaf < 0.05 { continue; }
            let mut rng = Rng::new(seed, stream);
            let mut s = rng.range(0.0, 8.0);
            while s < len {
                let (roll, inset, size, turn) = (rng.f(), rng.range(0.2, 1.1), rng.range(1.2, 2.6),
                                                 rng.range(0.0, std::f64::consts::TAU));
                if roll < leaf && overlaps(&win, s - 2.0, s + 2.0) {
                    let (x, y, z, tx, ty) = ln.at(s);
                    let d = side * (hw - inset).max(0.0);
                    emit_quad(ctx, out, r, (x - ty * d, y + tx * d, z), (tx, ty), turn, size, size, K_LEAVES, -1);
                }
                s += rng.range(2.5, 9.0);
            }
        }
    }
    let _ = CLS_MOTORWAY;
}

/// roadwear.plan: the wear pieces of a tile's roads. pts (N, 3) the roads' centre lines one after another, off
/// (R + 1,) where each begins, attrs (R, NA) per road (A_* columns), excl (E, 2) pairs (road, other road) where the
/// other is no blocker of the road, bx (4,) the tile's box x0 y0 x1 y1, tier 0 none / 1 tyre polish and concrete
/// joints / 2 all. Returns (points (M, 4) x y z and the distance along its piece (a quad's: its corner number),
/// piece offsets (P + 1,), per piece (P, 5): kind, width, texture length, the whole piece's length (-1: no fade),
/// atlas cell (-1: none)); a quad's width is across, its texture length along.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn wear_plan<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                      attrs: PyReadonlyArray2<'py, f64>, excl: PyReadonlyArray2<'py, i64>,
                      bx: PyReadonlyArray1<'py, f64>, tier: i64)
                      -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<f64>>)> {
    let (p, o, a, e, b) = (pts.as_array(), off.as_array(), attrs.as_array(), excl.as_array(), bx.as_array());
    let nr = o.len().saturating_sub(1);
    if a.nrows() != nr || (nr > 0 && a.ncols() < NA) || b.len() < 4 || p.ncols() < 3 || (e.nrows() > 0 && e.ncols() < 2) {
        return Err(pyo3::exceptions::PyValueError::new_err("wear_plan: pts (N, 3), off (R + 1), attrs (R, 15), excl (E, 2), box (4)"));
    }
    let lines: Vec<Line> = (0..nr).map(|r| {
        let (s, t) = (o[r] as usize, (o[r + 1] as usize).min(p.nrows()));
        let s = s.min(t);
        Line::new((s..t).map(|i| p[[i, 0]]).collect(), (s..t).map(|i| p[[i, 1]]).collect(),
                  (s..t).map(|i| p[[i, 2]]).collect())
    }).collect();
    let rows: Vec<Vec<f64>> = (0..nr).map(|r| (0..NA).map(|c| a[[r, c]]).collect()).collect();
    let block: Vec<f64> = rows.iter().map(|v| v[A_BLOCK]).collect();
    let ex: HashSet<(usize, usize)> = (0..e.nrows()).filter_map(|k| {
        let (x, y) = (e[[k, 0]], e[[k, 1]]);
        if x >= 0 && y >= 0 { Some((x as usize, y as usize)) } else { None }
    }).collect();
    let bxa = [b[0], b[1], b[2], b[3]];
    let work = p.nrows() * 64;
    let out = crate::release(py, work, move || {
        let bl = Blockers::new(&lines, &block, ex);
        let ctx = Ctx { bl: &bl, bx: bxa };
        let mut out = Out { off: vec![0], ..Default::default() };
        if tier > 0 {
            for r in 0..nr {
                if rows[r][A_WEAR] > 0.5 && lines[r].x.len() >= 2 {
                    road_pieces(&ctx, &mut out, r, &lines[r], &rows[r], tier);
                }
            }
        }
        out
    });
    let m = out.pts.len();
    let flat: Vec<f64> = out.pts.iter().flat_map(|q| q.iter().copied()).collect();
    let at: Vec<f64> = out.attrs.iter().flat_map(|q| q.iter().copied()).collect();
    let np = out.attrs.len();
    let pts_arr = numpy::ndarray::Array2::from_shape_vec((m, 4), flat)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let at_arr = numpy::ndarray::Array2::from_shape_vec((np, 5), at)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok((pts_arr.into_pyarray(py), out.off.into_pyarray(py), at_arr.into_pyarray(py)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn straight(len: f64) -> Line {
        let n = (len / 10.0) as usize + 1;
        Line::new((0..n).map(|i| i as f64 * 10.0).collect(), vec![0.0; n], vec![0.0; n])
    }

    #[test]
    fn path_offsets_left() {
        let ln = straight(100.0);
        let p = ln.path(5.0, 25.0, 2.0);
        assert_eq!(p.first().unwrap()[1], 2.0);
        assert!((p.last().unwrap()[3] - 20.0).abs() < 1e-9);
        assert!(p.iter().all(|q| (q[1] - 2.0).abs() < 1e-9));
    }

    #[test]
    fn blockers_skip_own_and_excluded() {
        let lines = vec![straight(100.0), Line::new(vec![50.0, 50.0], vec![-20.0, 20.0], vec![0.0, 0.0])];
        let bl = Blockers::new(&lines, &[4.0, 4.0], HashSet::new());
        assert!(bl.blocked(0, 50.0, 1.0));       // road 1's carriageway
        assert!(!bl.blocked(0, 20.0, 1.0));      // its own only
        let bl2 = Blockers::new(&lines, &[4.0, 4.0], [(0usize, 1usize)].into_iter().collect());
        assert!(!bl2.blocked(0, 50.0, 1.0));
    }

    #[test]
    fn rng_is_deterministic() {
        let (mut a, mut b) = (Rng::new(7, 3), Rng::new(7, 3));
        for _ in 0..10 { assert_eq!(a.next(), b.next()); }
        assert_ne!(Rng::new(7, 3).next(), Rng::new(7, 4).next());
    }
}
