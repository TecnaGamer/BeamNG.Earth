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
    fit_to_road: bool,
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
        let ctx = Ctx { bl: &bl, bx: bxa, fit_to_road: false };
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

// ---- the road revamp v2's surface overlays (P1c, BNE_SURFACE_V2: roadwear.py surface_chunks) -------------------------
// The stock maps lay a second layer of DecalRoads over the terrain's asphalt on 72-93 % of it (the second vanilla study,
// 2026-11-19): long faint surface-variation strips, tyre wear on the lane centres and repair patch strips, under the
// markings. Each from the road's own seed and its distance along its whole centre line, as wear_plan's pieces.

pub const K_VAR1: i64 = 10;     // surface variation, the mottled stock texture (strip, fades, not cut at junctions)
pub const K_VAR2: i64 = 11;     // ... the streaked one
pub const K_TREAD2: i64 = 12;   // tyre wear on a lane centre (strip, fades)
pub const K_PATCH2: i64 = 13;   // a repair patch strip (strip, short fades)

const VAR_W: (f64, f64) = (5.0, 9.0);       // m: a variation strip's width
const VAR_TRACK_M: f64 = 9.0;               // m of carriageway per parallel variation track
const VAR_RUN: (f64, f64) = (35.0, 65.0);   // m: a strip's length (about 50)
const VAR_GAP: (f64, f64) = (5.0, 35.0);    // m between strips (about 20: ~70 % of the length covered)
const VAR_TL: (f64, f64) = (80.0, 120.0);   // m: texture length (about 100)
const VAR_FADE_M: f64 = 10.0;               // m: end fades
const TREAD_W: (f64, f64) = (4.0, 5.0);     // m: a wear strip's width
const TREAD_RUN: (f64, f64) = (40.0, 60.0);
const TREAD_FADE: (f64, f64) = (5.0, 20.0);
const PATCH_EVERY: (f64, f64) = (60.0, 190.0);   // m between patches (about 8 a km)
const PATCH_LEN: (f64, f64) = (15.0, 40.0);
const PATCH_W: (f64, f64) = (1.8, 3.6);
const PATCH_FADE_M: f64 = 1.5;

/// A v2 strip of road r: its path cut where it leaves the box (and, with `cut`, where it lies on another
/// carriageway), its runs (at least min_m long) out with (kind, width, texture length, whole length, -1, end fade).
#[allow(clippy::too_many_arguments)]
fn emit_strip2(ctx: &Ctx, pts: &mut Vec<[f64; 4]>, off: &mut Vec<i64>, attrs: &mut Vec<[f64; 6]>, r: usize,
               path: Vec<[f64; 4]>, kind: i64, width: f64, tl: f64, min_m: f64, cut: bool, fend: f64) {
    if path.len() < 2 { return; }
    let slen = path[path.len() - 1][3];
    if slen < min_m.max(0.05) { return; }
    let pad = BOX_PAD_M + width / 2.0;
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for q in &path {
        x0 = x0.min(q[0]); x1 = x1.max(q[0]); y0 = y0.min(q[1]); y1 = y1.max(q[1]);
    }
    if x1 < ctx.bx[0] - pad || x0 > ctx.bx[2] + pad || y1 < ctx.bx[1] - pad || y0 > ctx.bx[3] + pad { return; }
    let dense = densify(&path);
    // Station each strip between physical obstructions before clipping it to a tile.
    // A crossing creates a visible end that needs a fade. A tile boundary only clips
    // the draw buffer and must preserve the same stations as the neighbouring tile.
    for clear in dense.split(|q| cut && ctx.bl.blocked(r, q[0], q[1])) {
        if clear.len() < 2 { continue; }
        let (start, length) = if ctx.fit_to_road && cut {
            (clear[0][3], clear[clear.len() - 1][3] - clear[0][3])
        } else { (0.0, slen) };
        for visible in clear.split(|q| !ctx.in_box(q[0], q[1], pad)) {
            if visible.len() < 2 || visible[visible.len() - 1][3] - visible[0][3] < min_m { continue; }
            let mut run = simplify(visible);
            for q in &mut run { q[3] -= start; }
            pts.extend_from_slice(&run);
            off.push(pts.len() as i64);
            attrs.push([kind as f64, width, tl, length, -1.0, fend]);
        }
    }
}

/// One road's v2 surface pieces (tier >= 1: variation and tyre wear; tier 2: patches and their joints).
#[allow(clippy::too_many_arguments)]
fn road_surface(ctx: &Ctx, pts: &mut Vec<[f64; 4]>, off: &mut Vec<i64>, attrs: &mut Vec<[f64; 6]>, r: usize,
                ln: &Line, a: &[f64], tier: i64) {
    let len = ln.len();
    if len < 2.0 { return; }
    let seed = a[A_SEED] as u64;
    let cls = (a[A_CLS] as i64).clamp(0, 7);
    let traffic = TRAFFIC[cls as usize];
    let lanes = (a[A_LANES].round() as i64).max(1);
    let hw = a[A_HW].max(0.5);
    let drive = a[A_DRIVE].max(1.0).min(2.0 * hw);
    let doff = a[A_DOFF];
    let lw = drive / lanes as f64;
    let aisle = a[A_AISLE] > 0.5;
    let win = windows(ln, &ctx.bx, hw + 12.0);
    if win.is_empty() { return; }
    let lane_c = |k: i64| doff - drive / 2.0 + (k as f64 + 0.5) * lw;
    // surface variation: parallel tracks over the whole carriageway, each a run of strips and gaps; not cut at the
    // other carriageways (the stock maps' strips run on through junction mouths and overlap there)
    if tier >= 1 {
        let full = 2.0 * hw;
        let tracks = ((full / VAR_TRACK_M).round() as i64).max(1);
        let band = full / tracks as f64;
        for k in 0..tracks {
            let mut rng = Rng::new(seed, 0x2000 + k as u64);
            let mut s = rng.range(-30.0, 20.0);
            while s < len {
                // (every draw made whatever the box: the next strips' numbers are the same in every tile)
                let run = rng.range(VAR_RUN.0, VAR_RUN.1);
                let w = rng.range(VAR_W.0, VAR_W.1).min(band + 1.0).min(full);
                let jit = rng.range(-1.0, 1.0);
                let tl = rng.range(VAR_TL.0, VAR_TL.1);
                let kind = if rng.f() < 0.5 { K_VAR1 } else { K_VAR2 };
                let gap = rng.range(VAR_GAP.0, VAR_GAP.1);
                let c = -hw + (k as f64 + 0.5) * band;
                let reach = (hw - w / 2.0).max(0.0);
                let d = (c + jit * ((band - w) / 2.0).max(0.0)).clamp(-reach, reach);
                let (s0, s1) = (s.max(0.0), (s + run).min(len));
                if s1 - s0 > 6.0 && overlaps(&win, s0, s1) {
                    emit_strip2(ctx, pts, off, attrs, r, ln.path(s0, s1, d), kind, w, tl, 3.0, false, VAR_FADE_M);
                }
                s += run + gap;
            }
        }
    }
    // tyre wear: a strip on each lane's centre in runs (tertiary roads and up)
    if tier >= 1 && traffic >= 0.4 && !aisle {
        // One tyre-wear decal describes one lane. Stock-width strips on narrow city
        // lanes otherwise overlap their neighbours and darken the asphalt twice.
        let tread_width = if ctx.fit_to_road && a[A_URBAN] > 0.5 && cls != CLS_MOTORWAY { lw } else { drive };
        for k in 0..lanes {
            let mut rng = Rng::new(seed, 0x2100 + k as u64);
            let mut s = rng.range(-40.0, 30.0);
            while s < len {
                let run = rng.range(TREAD_RUN.0, TREAD_RUN.1);
                let w = rng.range(TREAD_W.0, TREAD_W.1).min(tread_width);
                let fend = rng.range(TREAD_FADE.0, TREAD_FADE.1);
                let gap = rng.range(10.0, 30.0 + 120.0 * (1.0 - traffic));
                let (s0, s1) = (s.max(0.0), (s + run).min(len));
                if s1 - s0 > 8.0 && overlaps(&win, s0, s1) {
                    emit_strip2(ctx, pts, off, attrs, r, ln.path(s0, s1, lane_c(k)), K_TREAD2, w, w * 4.0, 2.0, true,
                                fend);
                }
                s += run + gap;
            }
        }
    }
    // repair patch strips (about 8 a km), a joint line across each end now and then
    if tier >= 2 {
        let mut rng = Rng::new(seed, 0x2200);
        let mut s = rng.range(0.0, PATCH_EVERY.1);
        while s < len {
            let plen = rng.range(PATCH_LEN.0, PATCH_LEN.1);
            let w = rng.range(PATCH_W.0, PATCH_W.1).min(drive);
            let k = rng.pick(lanes as usize) as i64;
            let jit = rng.range(-0.5, 0.5);
            let joints = rng.f();
            let reach = (hw - w / 2.0).max(0.0);
            let d = (lane_c(k) + jit).clamp(-reach, reach);
            let (s0, s1) = (s, (s + plen).min(len));
            if s1 - s0 > 4.0 && overlaps(&win, s0 - 2.0, s1 + 2.0) {
                emit_strip2(ctx, pts, off, attrs, r, ln.path(s0, s1, d), K_PATCH2, w, w * 8.0, 2.0, true, PATCH_FADE_M);
                if joints < 0.5 {
                    for &sj in &[s0, s1] {
                        let (x, y, z, tx, ty) = ln.at(sj);
                        let (nx, ny) = (-ty, tx);
                        let (l, rr) = (d + w / 2.0, d - w / 2.0);
                        let p = vec![[x + nx * l, y + ny * l, z, 0.0], [x + nx * rr, y + ny * rr, z, l - rr]];
                        emit_strip2(ctx, pts, off, attrs, r, p, K_ACROSS, 0.2, 6.4, 0.5, true, -1.0);
                    }
                }
            }
            s += plen + rng.range(PATCH_EVERY.0, PATCH_EVERY.1);
        }
    }
}

/// roadwear.surface_chunks (BNE_SURFACE_V2): the v2 surface pieces of a tile's roads, wear_plan's inputs (attrs NA
/// columns, tier). Returns (points (M, 4) x y z and the distance along its piece, offsets (P + 1,), per piece (P, 6):
/// kind, width, texture length, the whole piece's length, -1, end fade m (-1: none)).
#[pyfunction]
#[pyo3(signature = (pts, off, attrs, excl, bx, tier, fit_to_road=false))]
#[allow(clippy::type_complexity)]
pub fn surface_plan<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                         attrs: PyReadonlyArray2<'py, f64>, excl: PyReadonlyArray2<'py, i64>,
                         bx: PyReadonlyArray1<'py, f64>, tier: i64, fit_to_road: bool)
                         -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<f64>>)> {
    let (p, o, a, e, b) = (pts.as_array(), off.as_array(), attrs.as_array(), excl.as_array(), bx.as_array());
    let nr = o.len().saturating_sub(1);
    if a.nrows() != nr || (nr > 0 && a.ncols() < NA) || b.len() < 4 || p.ncols() < 3 || (e.nrows() > 0 && e.ncols() < 2) {
        return Err(pyo3::exceptions::PyValueError::new_err("surface_plan: pts (N, 3), off (R + 1), attrs (R, 15), excl (E, 2), box (4)"));
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
    let (op, oo, oa) = crate::release(py, work, move || {
        let bl = Blockers::new(&lines, &block, ex);
        let ctx = Ctx { bl: &bl, bx: bxa, fit_to_road };
        let (mut op, mut oo, mut oa) = (Vec::new(), vec![0i64], Vec::new());
        if tier > 0 {
            for r in 0..nr {
                if rows[r][A_WEAR] > 0.5 && lines[r].x.len() >= 2 {
                    road_surface(&ctx, &mut op, &mut oo, &mut oa, r, &lines[r], &rows[r], tier);
                }
            }
        }
        (op, oo, oa)
    });
    let m = op.len();
    let flat: Vec<f64> = op.iter().flat_map(|q| q.iter().copied()).collect();
    let at: Vec<f64> = oa.iter().flat_map(|q| q.iter().copied()).collect();
    let np = oa.len();
    let pts_arr = numpy::ndarray::Array2::from_shape_vec((m, 4), flat)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let at_arr = numpy::ndarray::Array2::from_shape_vec((np, 6), at)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok((pts_arr.into_pyarray(py), oo.into_pyarray(py), at_arr.into_pyarray(py)))
}

// ---- puddles (the 2026-11-26 playtest: "theres no puddles either. remember this game has physicly based textures") ---
// Where rain water stands on a paved road: the low points of its drive surface (a sag of the centre line's profile:
// the water runs down both ways to it, along the edges a crown drains to), the gutters by a kerb (between the drains)
// and the wheel paths' ruts (more on a worn surface). Each a quad of the puddle atlas (roadwear.py PUDDLE_CELLS: 4
// shapes a row, the rows' masks ever shallower: sags deepest, then gutters, then the ruts of poor and of fair roads),
// shown by the mod only while the ground is wet (wetlook.lua: the atlas's alpha threshold falls as the rain fills them,
// rises as they dry). As wear_plan's pieces: from the road's seed and its distance along its whole centre line (the
// same puddles in every tile that builds the road), cut at the other carriageways and the box.

pub const K_PUDDLE_SAG: i64 = 20;
pub const K_PUDDLE_GUTTER: i64 = 21;
pub const K_PUDDLE_RUT: i64 = 22;

const SAG_STEP_M: f64 = 2.0;        // m between the profile's samples
const SAG_LOW_M: f64 = 12.0;        // a sag is the lowest sample this far either way ...
const SAG_RISE_M: f64 = 30.0;       // ... and the road rises at least SAG_MIN_M within this far on both sides
const SAG_MIN_M: f64 = 0.04;
const SAG_MIN_MOTORWAY_M: f64 = 0.08;   // (a motorway's drainage: only marked sags)
const GUTTER_EVERY: (f64, f64) = (8.0, 22.0);
const RUT_EVERY: (f64, f64) = (12.0, 40.0);
const WHEEL_M: f64 = 0.85;          // m from a lane's centre to a wheel path
const FIT_TRIES: usize = 4;         // a puddle shrunk this many times to stay on the carriageway (road_puddles)

/// Puddles of the road's sags: (distance along, depth m) of every low point of the centre line's profile within the
/// windows (both sides SAG_RISE_M long within the line: a sag at its end is unknown, its next road decides).
fn sags(ln: &Line, win: &[(f64, f64)], min_depth: f64) -> Vec<(f64, f64)> {
    let len = ln.len();
    let n = (len / SAG_STEP_M).floor() as usize;
    if n < 4 { return Vec::new(); }
    let z: Vec<f64> = (0..=n).map(|k| ln.at(k as f64 * SAG_STEP_M).2).collect();
    let lo = (SAG_LOW_M / SAG_STEP_M).round() as usize;
    let rise = (SAG_RISE_M / SAG_STEP_M).round() as usize;
    let mut out = Vec::new();
    for k in rise..=n.saturating_sub(rise) {
        let s = k as f64 * SAG_STEP_M;
        if !overlaps(win, s - 10.0, s + 10.0) { continue; }
        let (a, b) = (k.saturating_sub(lo), (k + lo).min(n));
        // the lowest within lo either way (ties: the first)
        if (a..=b).any(|j| z[j] < z[k] || (z[j] == z[k] && j < k)) { continue; }
        let left = (k - rise..k).map(|j| z[j]).fold(f64::NEG_INFINITY, f64::max);
        let right = (k + 1..=k + rise).map(|j| z[j]).fold(f64::NEG_INFINITY, f64::max);
        let depth = (left - z[k]).min(right - z[k]);
        if depth >= min_depth { out.push((s, depth)); }
    }
    out
}

/// One road's puddles (wear_plan's road attrs; gutter: kerbs along its edges).
fn road_puddles(ctx: &Ctx, out: &mut Out, r: usize, ln: &Line, a: &[f64], gutter: bool) {
    let len = ln.len();
    if len < 4.0 { return; }
    let seed = a[A_SEED] as u64;
    let cls = (a[A_CLS] as i64).clamp(0, 7);
    let hw = a[A_HW];
    if !(hw > 0.8) { return; }
    let lanes = (a[A_LANES].round() as i64).max(1);
    let drive = a[A_DRIVE].max(1.0).min(2.0 * hw);
    let doff = a[A_DOFF];
    let lw = drive / lanes as f64;
    let oneway = a[A_ONEWAY] > 0.5;
    let mapped = a[A_COND] >= 0.0;
    let base = if mapped { a[A_COND] } else {
        let mut rng = Rng::new(seed, 1);
        (BASE_COND[cls as usize] + rng.range(-0.25, 0.25)).clamp(0.02, 0.98)
    };
    let win = windows(ln, &ctx.bx, hw + 8.0);
    if win.is_empty() { return; }
    // A puddle quad kept on the carriageway: each corner (emit_quad's, turned by `turn`) within hw of the centre line,
    // measured at the corner's own distance along it (a turned or long quad on a bend reached past the edge: the sag
    // puddles of a 7 m road out to 3.8 m). edge (side, inset): the quad hugs that edge, its offset taken again from
    // its width as it shrinks. Too big: shorter and narrower (FIT_TRIES steps), then the turn dropped; never fits:
    // none.
    let fits = |s: f64, d: f64, along: f64, across: f64, turn: f64| -> bool {
        let (x, y, _z, tx, ty) = ln.at(s);
        let (cx, cy) = (x - ty * d, y + tx * d);
        let (cs, sn) = (turn.dcos(), turn.dsin());
        let (ux, uy) = (tx * cs - ty * sn, tx * sn + ty * cs);
        let (vx, vy) = (-uy, ux);
        let (a, b) = (along / 2.0, across / 2.0);
        [(a, b), (a, -b), (-a, -b), (-a, b)].iter().all(|&(fa, fb)| {
            let (px, py) = (cx + ux * fa + vx * fb, cy + uy * fa + vy * fb);
            let sa = (s + (px - x) * tx + (py - y) * ty).clamp(0.0, len);
            let (qx, qy, _qz, qtx, qty) = ln.at(sa);
            ((px - qx) * -qty + (py - qy) * qtx).abs() <= hw + 1e-9
        })
    };
    let quad = |out: &mut Out, s: f64, d: f64, along: f64, across: f64, turn: f64, kind: i64, cell: i64,
                edge: Option<(f64, f64)>| {
        let (mut along, mut across, mut turn) = (along, across, turn);
        let offset = |across: f64| match edge { Some((side, inset)) => side * (hw - across / 2.0 - inset), None => d };
        for k in 0..=FIT_TRIES {
            if k == FIT_TRIES { turn = 0.0; }
            let dd = offset(across);
            if fits(s, dd, along, across, turn) {
                let (x, y, z, tx, ty) = ln.at(s);
                emit_quad(ctx, out, r, (x - ty * dd, y + tx * dd, z), (tx, ty), turn, along, across, kind, cell);
                return;
            }
            if k < FIT_TRIES {
                along *= 0.8;
                across *= 0.85;
            }
        }
    };
    // sags: along the edges the crown drains to (a one-way road's cross-fall: its right edge; a two-way crown both),
    // bigger with the sag's depth; a deep one on a narrow road across its middle too
    let min_depth = if cls == CLS_MOTORWAY { SAG_MIN_MOTORWAY_M } else { SAG_MIN_M };
    for (i, (s, depth)) in sags(ln, &win, min_depth).into_iter().enumerate() {
        let mut rng = Rng::new(seed, 0x3000 + i as u64 * 7 + (s / SAG_STEP_M) as u64);
        let along = (5.0 + 30.0 * depth).clamp(4.0, 12.0) * rng.range(0.8, 1.2);
        let across = (0.9 + 5.0 * depth).clamp(0.8, (0.8 * hw).min(2.6));
        let sides: &[f64] = if oneway { &[-1.0] } else { &[1.0, -1.0] };
        for &side in sides {
            let d = side * (hw - across / 2.0 - 0.1);
            quad(out, s + rng.range(-1.5, 1.5), d, along, across, rng.range(-0.06, 0.06), K_PUDDLE_SAG,
                 rng.pick(4) as i64, Some((side, 0.1)));
        }
        if depth >= 0.15 && hw < 4.0 {
            quad(out, s, doff, along * 0.8, (2.0 * hw - 2.0 * across).max(1.0), rng.range(-0.05, 0.05), K_PUDDLE_SAG,
                 rng.pick(4) as i64, None);
        }
    }
    // gutters: by a kerb, between the drains, more on a worn surface
    if gutter && cls != CLS_MOTORWAY {
        for (side_i, side) in [1.0f64, -1.0].iter().enumerate() {
            let mut rng = Rng::new(seed, 0x3100 + side_i as u64);
            let mut s = rng.range(0.0, GUTTER_EVERY.1);
            while s < len {
                let cond = condition(base, mapped, seed, s);
                let along = rng.range(2.0, 5.0);
                let across = rng.range(0.6, 1.2).min(0.5 * hw);
                let (turn, cell, keep) = (rng.range(-0.08, 0.08), rng.pick(4) as i64, rng.f());
                if keep < (0.6 * (1.3 - cond)).clamp(0.3, 0.75) && overlaps(&win, s - along, s + along) {
                    quad(out, s, side * (hw - across / 2.0 - 0.05), along, across, turn, K_PUDDLE_GUTTER, 4 + cell,
                         Some((*side, 0.05)));
                }
                s += rng.range(GUTTER_EVERY.0, GUTTER_EVERY.1);
            }
        }
    }
    // ruts: in the wheel paths of each lane, the more and the deeper the worse the surface
    if cls != CLS_SERVICE || base < 0.5 {
        for k in 0..lanes {
            let c = doff - drive / 2.0 + (k as f64 + 0.5) * lw;
            for (w_i, w) in [-WHEEL_M, WHEEL_M].iter().enumerate() {
                let mut rng = Rng::new(seed, 0x3200 + 2 * k as u64 + w_i as u64);
                let mut s = rng.range(0.0, RUT_EVERY.1);
                while s < len {
                    let cond = condition(base, mapped, seed, s);
                    let along = rng.range(2.5, 7.0);
                    let across = rng.range(0.35, 0.6);
                    let (turn, cell, keep) = (rng.range(-0.04, 0.04), rng.pick(4) as i64, rng.f());
                    let row = if cond < 0.5 { 8 } else { 12 };
                    if keep < (0.25 + 0.55 * (1.0 - cond)) && overlaps(&win, s - along, s + along) {
                        quad(out, s, c + w, along, across, turn, K_PUDDLE_RUT, row + cell, None);
                    }
                    s += rng.range(RUT_EVERY.0, RUT_EVERY.1) / (1.3 - cond).max(0.5);
                }
            }
        }
    }
}

/// roadwear.puddle_chunks: the puddles of a tile's roads, wear_plan's arguments (pts, off, attrs, excl, bx) and gutter
/// (R,): 1 where the road has kerbs along its edges. Returns as wear_plan (points (M, 4): a quad's corners, the corner
/// number; offsets (P + 1,); per piece (P, 5): kind K_PUDDLE_*, across, along, -1, atlas cell 0..15).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn puddle_plan<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                        attrs: PyReadonlyArray2<'py, f64>, excl: PyReadonlyArray2<'py, i64>,
                        bx: PyReadonlyArray1<'py, f64>, gutter: PyReadonlyArray1<'py, u8>)
                        -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<f64>>)> {
    let (p, o, a, e, b, g) = (pts.as_array(), off.as_array(), attrs.as_array(), excl.as_array(), bx.as_array(),
                              gutter.as_array());
    let nr = o.len().saturating_sub(1);
    if a.nrows() != nr || (nr > 0 && a.ncols() < NA) || b.len() < 4 || p.ncols() < 3 || (e.nrows() > 0 && e.ncols() < 2)
        || g.len() != nr {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "puddle_plan: pts (N, 3), off (R + 1), attrs (R, 15), excl (E, 2), box (4), gutter (R)"));
    }
    let lines: Vec<Line> = (0..nr).map(|r| {
        let (s, t) = (o[r] as usize, (o[r + 1] as usize).min(p.nrows()));
        let s = s.min(t);
        Line::new((s..t).map(|i| p[[i, 0]]).collect(), (s..t).map(|i| p[[i, 1]]).collect(),
                  (s..t).map(|i| p[[i, 2]]).collect())
    }).collect();
    let rows: Vec<Vec<f64>> = (0..nr).map(|r| (0..NA).map(|c| a[[r, c]]).collect()).collect();
    let gut: Vec<bool> = g.iter().map(|&v| v != 0).collect();
    let block: Vec<f64> = rows.iter().map(|v| v[A_BLOCK]).collect();
    let ex: HashSet<(usize, usize)> = (0..e.nrows()).filter_map(|k| {
        let (x, y) = (e[[k, 0]], e[[k, 1]]);
        if x >= 0 && y >= 0 { Some((x as usize, y as usize)) } else { None }
    }).collect();
    let bxa = [b[0], b[1], b[2], b[3]];
    let work = p.nrows() * 64;
    let out = crate::release(py, work, move || {
        let bl = Blockers::new(&lines, &block, ex);
        let ctx = Ctx { bl: &bl, bx: bxa, fit_to_road: false };
        let mut out = Out { off: vec![0], ..Default::default() };
        for r in 0..nr {
            if rows[r][A_WEAR] > 0.5 && rows[r][A_AISLE] < 0.5 && lines[r].x.len() >= 2 {
                road_puddles(&ctx, &mut out, r, &lines[r], &rows[r], gut[r]);
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

// ---- the puddle atlas's masks (tools/make_puddle_atlas.py writes them as the mod's texture source) -----------------

/// The puddle atlas's alpha (size x size, row 0 at the top; 4 x 4 cells of size / 4): cell (row, col) a puddle of
/// shape col whose water depth (0..255: the material's alpha test threshold falls as the rain fills them) peaks at
/// PEAK[row] in its middle and falls to 0 at its rim. Its outline: an ellipse filling the cell with a ragged edge
/// (seeded value noise of three octaves, the same on every CPU: integer hashes, no transcendental functions).
pub fn puddle_atlas(size: usize) -> Vec<u8> {
    const PEAK: [f64; 4] = [1.0, 0.85, 0.72, 0.6];
    let size = size.max(16) / 16 * 16;
    let cs = size / 4;
    let mut out = vec![0u8; size * size];
    // value noise on a lattice of `cells` per unit, smooth-stepped, seeded
    let noise = |seed: u64, u: f64, v: f64, cells: f64| -> f64 {
        let (x, y) = (u * cells, v * cells);
        let (ix, iy) = (x.floor(), y.floor());
        let (fx, fy) = (x - ix, y - iy);
        let h = |i: f64, j: f64| -> f64 {
            let k = mix(seed ^ mix((i as i64 as u64).wrapping_mul(0x9E37) ^ (j as i64 as u64).wrapping_mul(0x85EB_CA6B)));
            (k >> 11) as f64 / (1u64 << 53) as f64
        };
        let (sx, sy) = (fx * fx * (3.0 - 2.0 * fx), fy * fy * (3.0 - 2.0 * fy));
        let a = h(ix, iy) + (h(ix + 1.0, iy) - h(ix, iy)) * sx;
        let b = h(ix, iy + 1.0) + (h(ix + 1.0, iy + 1.0) - h(ix, iy + 1.0)) * sx;
        a + (b - a) * sy
    };
    for row in 0..4 {
        for col in 0..4 {
            let seed = 0x5EED_0000 + (row * 4 + col) as u64;
            // per shape: the ellipse's squash and a lobe (col 3: two pools joined)
            let (ax, ay) = [(0.80, 0.92), (0.62, 0.95), (0.88, 0.88), (0.70, 0.95)][col];   // (across u, along v)
            for py_ in 0..cs {
                for px in 0..cs {
                    // u, v in -1..1 across the cell (the quad's uv: u across the road, v along it: roadwear.py)
                    let u = (px as f64 + 0.5) / cs as f64 * 2.0 - 1.0;
                    let v = (py_ as f64 + 0.5) / cs as f64 * 2.0 - 1.0;
                    let mut r = ((u / ax) * (u / ax) + (v / ay) * (v / ay)).sqrt();
                    if col == 3 {               // two pools: the narrower waist between them
                        let w = 1.0 - 0.35 * (1.0 - (v * 2.2).abs().min(1.0));
                        r = ((u / (ax * w)) * (u / (ax * w)) + (v / ay) * (v / ay)).sqrt();
                    }
                    let (uu, vv) = ((u + 1.0) / 2.0, (v + 1.0) / 2.0);
                    let n = 0.55 * noise(seed, uu, vv, 3.0) + 0.3 * noise(seed ^ 1, uu, vv, 7.0)
                        + 0.15 * noise(seed ^ 2, uu, vv, 17.0);
                    // the rim pushed in and out by the noise; the depth falls from the middle to it
                    let rim = (0.86 + 0.45 * (n - 0.5)).min(0.97);       // (never past the cell: its neighbours in the mips)
                    let d = (1.0 - r / rim.max(0.2)).max(0.0);
                    let depth = (d.min(0.5) / 0.5 * PEAK[row]).clamp(0.0, 1.0);
                    let (x, y) = (col * cs + px, row * cs + py_);
                    out[y * size + x] = (depth * 255.0).round() as u8;
                }
            }
        }
    }
    out
}

/// roadwear.puddle_atlas: the atlas's alpha (size x size u8, row 0 the top).
#[pyfunction]
pub fn puddle_atlas_px<'py>(py: Python<'py>, size: usize) -> Bound<'py, PyArray1<u8>> {
    puddle_atlas(size).into_pyarray(py)
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

    fn attrs_row(hw: f64, lanes: f64, cls: f64, seed: f64) -> Vec<f64> {
        let mut a = vec![0.0; NA];
        a[A_HW] = hw; a[A_DRIVE] = 2.0 * hw; a[A_LANES] = lanes; a[A_CLS] = cls; a[A_COND] = -1.0;
        a[A_SEED] = seed; a[A_WEAR] = 1.0; a[A_BLOCK] = hw + 0.5;
        a
    }

    fn surface(ln: &Line, a: &[f64], bx: [f64; 4], tier: i64) -> (Vec<[f64; 4]>, Vec<i64>, Vec<[f64; 6]>) {
        let bl = Blockers::new(std::slice::from_ref(ln), &[a[A_BLOCK]], HashSet::new());
        let ctx = Ctx { bl: &bl, bx, fit_to_road: true };
        let (mut p, mut o, mut at) = (Vec::new(), vec![0i64], Vec::new());
        road_surface(&ctx, &mut p, &mut o, &mut at, 0, ln, a, tier);
        (p, o, at)
    }

    fn is_var(q: &[f64; 6]) -> bool { q[0] as i64 == K_VAR1 || q[0] as i64 == K_VAR2 }

    #[test]
    fn surface_variation_covers_about_seventy_percent() {
        let ln = straight(3000.0);
        let a = attrs_row(3.7, 2.0, 4.0, 12345.0);
        let (_p, _o, at) = surface(&ln, &a, [-10.0, -50.0, 3010.0, 50.0], 2);
        let var: f64 = at.iter().filter(|q| is_var(q)).map(|q| q[3]).sum();
        let share = var / 3000.0;
        assert!(share > 0.6 && share < 0.8, "variation share {share}");
        assert!(at.iter().filter(|q| is_var(q))
                  .all(|q| q[1] >= 5.0 - 1e-9 && q[1] <= 7.4 + 1e-9 && q[2] >= 80.0 && q[2] <= 120.0 && q[5] == 10.0));
        let patches = at.iter().filter(|q| q[0] as i64 == K_PATCH2).count() as f64 / 3.0;
        assert!(patches > 5.0 && patches < 11.0, "patches a km {patches}");
        assert!(at.iter().any(|q| q[0] as i64 == K_TREAD2 && q[1] >= 4.0 && q[1] <= 5.0 && q[5] >= 5.0 && q[5] <= 20.0));
    }

    #[test]
    fn surface_pieces_agree_across_tiles() {
        // the same road built twice and for a smaller box: the same pieces where they share it
        let ln = straight(800.0);
        let a = attrs_row(3.5, 2.0, 2.0, 777.0);
        let (p1, o1, a1) = surface(&ln, &a, [-10.0, -50.0, 810.0, 50.0], 2);
        let (p2, o2, a2) = surface(&ln, &a, [-10.0, -50.0, 810.0, 50.0], 2);
        assert_eq!(o1, o2);
        assert_eq!(a1.len(), a2.len());
        assert!(p1.iter().zip(&p2).all(|(x, y)| x == y));
        let (_p3, _o3, a3) = surface(&ln, &a, [300.0, -50.0, 500.0, 50.0], 2);
        assert!(a3.iter().any(is_var));
        for q in a3.iter().filter(|q| is_var(q)) {
            assert!(a1.iter().any(|w| w[0] == q[0] && w[1] == q[1] && w[3] == q[3]));
        }
    }

    fn puddles(ln: &Line, a: &[f64], bx: [f64; 4], gutter: bool) -> Out {
        let bl = Blockers::new(std::slice::from_ref(ln), &[a[A_BLOCK]], HashSet::new());
        let ctx = Ctx { bl: &bl, bx, fit_to_road: false };
        let mut out = Out { off: vec![0], ..Default::default() };
        road_puddles(&ctx, &mut out, 0, ln, a, gutter);
        out
    }

    fn vee(len: f64, grade: f64) -> Line {
        let n = (len / 2.0) as usize + 1;
        Line::new((0..n).map(|i| i as f64 * 2.0).collect(), vec![0.0; n],
                  (0..n).map(|i| (i as f64 * 2.0 - len / 2.0).abs() * grade).collect())
    }

    #[test]
    fn puddles_stand_in_the_sag() {
        let a = attrs_row(3.5, 2.0, 4.0, 4242.0);
        let out = puddles(&vee(400.0, 0.01), &a, [-10.0, -50.0, 410.0, 50.0], false);
        let sag: Vec<usize> = (0..out.attrs.len()).filter(|&k| out.attrs[k][0] as i64 == K_PUDDLE_SAG).collect();
        assert_eq!(sag.len(), 3, "both edges and the middle of a 0.3 m sag on a 7 m road");
        for k in sag {
            let q = &out.pts[out.off[k] as usize..out.off[k + 1] as usize];
            let cx = q.iter().map(|p| p[0]).sum::<f64>() / 4.0;
            assert!((cx - 200.0).abs() < 2.0, "at the low point: {cx}");
            assert!(q.iter().all(|p| p[1].abs() <= 3.5 + 1e-9), "on the carriageway");
            assert!((0..4).contains(&(out.attrs[k][4] as i64)), "the deepest row's cells");
        }
        // a flat road and a steady grade: no sag
        let flat = puddles(&straight(400.0), &a, [-10.0, -50.0, 410.0, 50.0], false);
        assert!(flat.attrs.iter().all(|q| q[0] as i64 != K_PUDDLE_SAG));
        let tilt = Line::new(vec![0.0, 400.0], vec![0.0, 0.0], vec![0.0, 8.0]);
        assert!(puddles(&tilt, &a, [-10.0, -50.0, 410.0, 50.0], false).attrs.iter().all(|q| q[0] as i64 != K_PUDDLE_SAG));
        // a shallow dip under SAG_MIN_M: none
        let dip = puddles(&vee(400.0, 0.001), &a, [-10.0, -50.0, 410.0, 50.0], false);
        assert!(dip.attrs.iter().all(|q| q[0] as i64 != K_PUDDLE_SAG));
    }

    #[test]
    fn puddles_by_kerbs_and_in_the_wheel_paths() {
        let mut a = attrs_row(4.0, 2.0, 4.0, 99.0);
        a[A_COND] = 0.2;                   // a worn street
        let ln = straight(1000.0);
        let bx = [-10.0, -50.0, 1010.0, 50.0];
        let no_kerb = puddles(&ln, &a, bx, false);
        assert!(no_kerb.attrs.iter().all(|q| q[0] as i64 != K_PUDDLE_GUTTER));
        let kerb = puddles(&ln, &a, bx, true);
        let g = kerb.attrs.iter().filter(|q| q[0] as i64 == K_PUDDLE_GUTTER).count();
        assert!(g > 30 && g < 220, "gutter puddles a km on both sides: {g}");
        let ruts = kerb.attrs.iter().filter(|q| q[0] as i64 == K_PUDDLE_RUT).count();
        assert!(ruts > 20, "a worn street's ruts: {ruts}");
        // a new surface: fewer ruts
        a[A_COND] = 0.95;
        let newer = puddles(&ln, &a, bx, true).attrs.iter().filter(|q| q[0] as i64 == K_PUDDLE_RUT).count();
        assert!(newer < ruts / 2, "new {newer} vs worn {ruts}");
        // the same road twice, and a smaller box: the same puddles where they share it
        a[A_COND] = 0.2;
        let (p1, p2) = (puddles(&ln, &a, bx, true), puddles(&ln, &a, bx, true));
        assert_eq!(p1.off, p2.off);
        assert!(p1.pts.iter().zip(&p2.pts).all(|(x, y)| x == y));
        let part = puddles(&ln, &a, [300.0, -50.0, 500.0, 50.0], true);
        assert!(!part.attrs.is_empty());
        for k in 0..part.attrs.len() {
            let q = &part.pts[part.off[k] as usize..part.off[k + 1] as usize];
            assert!((0..p1.attrs.len()).any(|j| p1.attrs[j] == part.attrs[k]
                && p1.pts[p1.off[j] as usize..p1.off[j + 1] as usize] == *q), "a part tile's puddle is the whole road's");
        }
    }

    #[test]
    fn puddles_stay_on_a_bending_carriageway() {
        // a worn kerbed street round a 40 m radius bend, dipping in the middle: every corner of every puddle within
        // the half width of the centre line (its distance to the arc)
        let (rad, n) = (40.0f64, 160usize);
        let pts: Vec<(f64, f64, f64)> = (0..=n).map(|i| {
            let t = i as f64 / n as f64 * 2.0;          // 2 rad of arc, 80 m
            ((t.dcos()) * rad, (t.dsin()) * rad, (t - 1.0).abs() * 0.4)
        }).collect();
        let ln = Line::new(pts.iter().map(|p| p.0).collect(), pts.iter().map(|p| p.1).collect(),
                           pts.iter().map(|p| p.2).collect());
        let mut a = attrs_row(3.5, 2.0, 5.0, 31337.0);
        a[A_COND] = 0.15;
        let out = puddles(&ln, &a, [-60.0, -60.0, 60.0, 60.0], true);
        assert!(out.attrs.len() > 5, "puddles laid: {}", out.attrs.len());
        assert!(out.attrs.iter().any(|q| q[0] as i64 == K_PUDDLE_SAG), "the dip's sag puddles");
        for p in &out.pts {
            let r = p[0].dhypot(p[1]);
            assert!((r - rad).abs() <= 3.5 + 1e-6, "a corner {:.3} m off the centre line", (r - rad).abs());
        }
    }

    #[test]
    fn puddle_atlas_rows_ever_shallower() {
        let n = 256;
        let px = puddle_atlas(n);
        assert_eq!(px.len(), n * n);
        let cs = n / 4;
        let peak = |row: usize, col: usize| (0..cs).flat_map(|y| (0..cs).map(move |x| (x, y)))
            .map(|(x, y)| px[(row * cs + y) * n + col * cs + x]).max().unwrap();
        for col in 0..4 {
            assert_eq!(peak(0, col), 255);
            assert!(peak(1, col) < 255 && peak(2, col) < peak(1, col) && peak(3, col) < peak(2, col));
        }
        // a cell's rim and corners dry; the same pixels every time
        assert_eq!(px[0], 0);
        assert_eq!(px[cs - 1], 0);
        assert_eq!(puddle_atlas(n), px);
    }

    #[test]
    fn rng_is_deterministic() {
        let (mut a, mut b) = (Rng::new(7, 3), Rng::new(7, 3));
        for _ in 0..10 { assert_eq!(a.next(), b.next()); }
        assert_ne!(Rng::new(7, 3).next(), Rng::new(7, 4).next());
    }
}
