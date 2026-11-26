//! vegetation.py's kernels for the stock clump trees and planted rows (the asset survey, 2026-11-10; GitHub #1074:
//! "trees cost too much"):
//! - veg_clumps: a forest's canopy trees gathered on a world-fixed grid; a square with enough of them gets one item of
//!   a stock clump shape (East Coast USA's tree_douglasfir_group_low, tree_beech_forest_group ...: one item for 4-8
//!   trees), shown beyond the near ring in their place (trees.lua CLUMP_M), and its trees are flagged.
//! - field_lattice: the points of a lattice along a field's axis inside its rings (orchards' trees, vineyards' rows,
//!   hay bales), i-major (numpy's meshgrid 'ij' order, the orchard rows' Python before it).
use crate::dm::Dm;

use std::collections::BTreeMap;

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

const NONE: u8 = 255;
const HALF_WORLD: f64 = 20037508.342789244;      // Web Mercator's half extent (m)

#[inline]
pub(crate) fn mix(i: i64, j: i64, salt: u64) -> f64 {
    let mut x = (i as u64).wrapping_mul(0x9E3779B97F4A7C15) ^ (j as u64).wrapping_add(salt).wrapping_mul(0xC2B2AE3D27D4EB4F);
    x ^= x >> 31;
    x = x.wrapping_mul(0xBF58476D1CE4E5B9);
    x ^= x >> 29;
    (x >> 11) as f64 / (1u64 << 53) as f64
}

/// The roads a far clump keeps its footprint off (veg_clumps): segments in Web Mercator m, each with its reach (the
/// carriageway's half width + a margin by class), in a bucket grid over the trees' extent.
struct ClumpRoads {
    s: Vec<[f64; 5]>,           // ax, ay, bx, by, reach
    x0: f64,
    y0: f64,
    size: f64,
    nx: i64,
    ny: i64,
    cells: Vec<Vec<u32>>,
}

impl ClumpRoads {
    /// segs: (ax, ay, bx, by, reach); the grid covers [lo, hi] (the trees' extent with the clumps' reach); a segment
    /// goes in every bucket its capsule's box overlaps (clamped to the grid).
    fn new(s: Vec<[f64; 5]>, lo: (f64, f64), hi: (f64, f64), size: f64) -> Self {
        let nx = (((hi.0 - lo.0) / size).floor() as i64 + 1).clamp(1, 4096);
        let ny = (((hi.1 - lo.1) / size).floor() as i64 + 1).clamp(1, 4096);
        let mut cells: Vec<Vec<u32>> = vec![Vec::new(); (nx * ny) as usize];
        for (k, g) in s.iter().enumerate() {
            let (sx0, sx1) = (g[0].min(g[2]) - g[4], g[0].max(g[2]) + g[4]);
            let (sy0, sy1) = (g[1].min(g[3]) - g[4], g[1].max(g[3]) + g[4]);
            if !(sx1 >= lo.0 && sx0 <= hi.0 && sy1 >= lo.1 && sy0 <= hi.1) {
                continue;
            }
            let i0 = (((sx0 - lo.0) / size).floor() as i64).clamp(0, nx - 1);
            let i1 = (((sx1 - lo.0) / size).floor() as i64).clamp(0, nx - 1);
            let j0 = (((sy0 - lo.1) / size).floor() as i64).clamp(0, ny - 1);
            let j1 = (((sy1 - lo.1) / size).floor() as i64).clamp(0, ny - 1);
            for j in j0..=j1 {
                for i in i0..=i1 {
                    cells[(j * nx + i) as usize].push(k as u32);
                }
            }
        }
        ClumpRoads { s, x0: lo.0, y0: lo.1, size, nx, ny, cells }
    }

    /// The road a disc (cx, cy, r) reaches into deepest: (segment, depth = reach + r - distance > 0, the nearest point
    /// on it), ties to the lower segment; None when it is clear of all of them.
    fn worst(&self, cx: f64, cy: f64, r: f64) -> Option<(usize, f64, f64, f64)> {
        if self.s.is_empty() {
            return None;
        }
        let i0 = (((cx - r - self.x0) / self.size).floor() as i64).clamp(0, self.nx - 1);
        let i1 = (((cx + r - self.x0) / self.size).floor() as i64).clamp(0, self.nx - 1);
        let j0 = (((cy - r - self.y0) / self.size).floor() as i64).clamp(0, self.ny - 1);
        let j1 = (((cy + r - self.y0) / self.size).floor() as i64).clamp(0, self.ny - 1);
        let mut best: Option<(usize, f64, f64, f64)> = None;
        for j in j0..=j1 {
            for i in i0..=i1 {
                for &k in &self.cells[(j * self.nx + i) as usize] {
                    let k = k as usize;
                    let g = &self.s[k];
                    let (dx, dy) = (g[2] - g[0], g[3] - g[1]);
                    let l2 = dx * dx + dy * dy;
                    let t = if l2 > 0.0 { (((cx - g[0]) * dx + (cy - g[1]) * dy) / l2).clamp(0.0, 1.0) } else { 0.0 };
                    let (qx, qy) = (g[0] + t * dx, g[1] + t * dy);
                    let d = ((cx - qx) * (cx - qx) + (cy - qy) * (cy - qy)).sqrt();
                    let depth = g[4] + r - d;
                    if depth > 0.0 {
                        let better = match best {
                            None => true,
                            Some((bk, bd, _, _)) => depth > bd || (depth == bd && k < bk),
                        };
                        if better {
                            best = Some((k, depth, qx, qy));
                        }
                    }
                }
            }
        }
        best
    }

    /// Where a clump of radius r at (cx, cy) stands clear of every road: there, or moved straight off the road it
    /// reaches deepest into (a few times: a bend), by max_shift at most in all; None when that is not enough.
    fn place(&self, cx: f64, cy: f64, r: f64, max_shift: f64) -> Option<(f64, f64)> {
        let (mut px, mut py, mut moved) = (cx, cy, 0.0f64);
        for _ in 0..4 {
            let Some((k, depth, qx, qy)) = self.worst(px, py, r) else {
                return Some((px, py));
            };
            let (mut nx, mut ny) = (px - qx, py - qy);
            let d = (nx * nx + ny * ny).sqrt();
            if d > 1e-9 {
                nx /= d;
                ny /= d;
            } else {                // (on the road's line: its left)
                let g = &self.s[k];
                let (dx, dy) = (g[2] - g[0], g[3] - g[1]);
                let l = (dx * dx + dy * dy).sqrt();
                if !(l > 0.0) {
                    return None;
                }
                nx = -dy / l;
                ny = dx / l;
            }
            let step = depth + CLUMP_STEP_EPS;
            moved += step;
            if moved > max_shift {
                return None;
            }
            px += nx * step;
            py += ny * step;
        }
        if self.worst(px, py, r).is_none() { Some((px, py)) } else { None }
    }
}

const CLUMP_STEP_EPS: f64 = 0.01;       // Web Mercator m a moved clump stands past the reach (no rounding left in it)
const CLUMP_BUCKET: f64 = 32.0;         // the roads' bucket grid (Web Mercator m)

/// One clump of the trees ids: (centroid x, y, height (mean of the taller half), lowest rank, the group most of them
/// map to (ties: the lower id)).
fn clump_of_ids(ids: &[usize], x: &[f64], y: &[f64], group: &[u8], height: &[f32], rank: &[f32], clump_of: &[u8])
                -> (f64, f64, f32, f32, u8) {
    let mut votes = [0usize; 256];
    let (mut sx, mut sy, mut rmin) = (0.0f64, 0.0f64, f32::INFINITY);
    let mut hs: Vec<f32> = Vec::with_capacity(ids.len());
    for &k in ids {
        votes[clump_of[group[k] as usize] as usize] += 1;
        sx += x[k];
        sy += y[k];
        rmin = rmin.min(rank[k]);
        hs.push(height[k]);
    }
    let mut best = 0usize;
    for g in 0..256 {
        if votes[g] > votes[best] {
            best = g;
        }
    }
    hs.sort_by(|a, b| b.partial_cmp(a).unwrap_or(std::cmp::Ordering::Equal));
    let top = hs.len().div_ceil(2);
    let h = hs[..top].iter().map(|&v| v as f64).sum::<f64>() / top as f64;
    let m = ids.len() as f64;
    (sx / m, sy / m, h as f32, rmin, best as u8)
}

/// What clumps_core keeps the clumps off: the roads (Web Mercator m: ax, ay, bx, by, reach), the clump's ground radius
/// rad_k * height + rad_c, Web Mercator m per ground m, the most a clump may move off a road (ground m). A moved clump
/// must also stay in its own map cell (own: x0, y0, x1, y1 with x0 <= x < x1, y0 < y <= y1, the cell's trees' rule: a
/// neighbour's tiles never show it) and off the blocked pixels (buildings, water, decks: blocked (rows * cols, row
/// major) on the grid (x0, y0, dx, dy); off the grid counts as blocked; the tiles drop a clump in water), else its
/// trees stay single (Codex review of cff64d91).
pub(crate) struct ClumpRoadArgs {
    pub segs: Vec<[f64; 5]>,
    pub merc_per_m: f64,
    pub rad_k: f64,
    pub rad_c: f64,
    pub max_shift_m: f64,
    pub own: Option<[f64; 4]>,
    pub blocked: Option<(Vec<u8>, usize, usize, [f64; 4])>,
}

impl ClumpRoadArgs {
    /// May a clump moved off a road stand at (x, y)?
    fn free(&self, x: f64, y: f64) -> bool {
        if let Some(o) = self.own {
            if !(x >= o[0] && x < o[2] && y > o[1] && y <= o[3]) {
                return false;
            }
        }
        if let Some((m, rows, cols, g)) = &self.blocked {
            let c = ((x - g[0]) / g[2]).floor();
            let r = ((g[1] - y) / g[3]).floor();
            if !(c >= 0.0 && r >= 0.0 && c < *cols as f64 && r < *rows as f64) {
                return false;
            }
            if m[r as usize * cols + c as usize] != 0 {
                return false;
            }
        }
        true
    }
}

/// veg_clumps' work: (member, cx, cy, height, rank, group, u, stats [clumps clear of the roads as they were, squares
/// split at a road, clumps moved off one, clumps left out (their trees single)]).
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub(crate) fn clumps_core(x: &[f64], y: &[f64], group: &[u8], kind: &[u8], height: &[f32], rank: &[f32], lcls: &[u8],
                          clump_of: &[u8], tree_kind: u8, skip_class: u8, cell: f64, min_members: usize,
                          roads: &ClumpRoadArgs)
                          -> (Vec<bool>, Vec<f64>, Vec<f64>, Vec<f32>, Vec<f32>, Vec<u8>, Vec<f64>, [u64; 4]) {
    let n = x.len();
    let mut squares: BTreeMap<(i64, i64), Vec<usize>> = BTreeMap::new();
    let (mut lo, mut hi) = ((f64::INFINITY, f64::INFINITY), (f64::NEG_INFINITY, f64::NEG_INFINITY));
    let mut hmax = 0.0f64;
    for i in 0..n {
        let g = group[i] as usize;
        if kind[i] != tree_kind || lcls[i] == skip_class || g >= clump_of.len() || clump_of[g] == NONE {
            continue;
        }
        if !(x[i].is_finite() && y[i].is_finite()) {
            continue;
        }
        let key = (((x[i] + HALF_WORLD) / cell).floor() as i64, ((y[i] + HALF_WORLD) / cell).floor() as i64);
        squares.entry(key).or_default().push(i);
        lo = (lo.0.min(x[i]), lo.1.min(y[i]));
        hi = (hi.0.max(x[i]), hi.1.max(y[i]));
        if height[i].is_finite() {
            hmax = hmax.max(height[i] as f64);
        }
    }
    let k = roads.merc_per_m;
    let max_shift = roads.max_shift_m.max(0.0) * k;
    let rad = |h: f32| (roads.rad_k * h as f64 + roads.rad_c).max(0.0) * k;
    let index = if roads.segs.is_empty() || squares.is_empty() {
        None
    } else {
        let pad = (roads.rad_k * hmax + roads.rad_c).max(0.0) * k + max_shift + 1.0;
        Some(ClumpRoads::new(roads.segs.clone(), (lo.0 - pad, lo.1 - pad), (hi.0 + pad, hi.1 + pad), CLUMP_BUCKET))
    };
    let mut member = vec![false; n];
    let mut stats = [0u64; 4];
    let (mut cx, mut cy, mut ch, mut cr, mut cg, mut cu) = (vec![], vec![], vec![], vec![], vec![], vec![]);
    for (&(i, j), ids) in &squares {
        if ids.len() < min_members {
            continue;
        }
        let (mx, my, h, rmin, best) = clump_of_ids(ids, x, y, group, height, rank, clump_of);
        let hit = index.as_ref().and_then(|ix| ix.worst(mx, my, rad(h)));
        let (Some(ix), Some((seg, _, _, _))) = (index.as_ref(), hit) else {
            for &t in ids {
                member[t] = true;
            }
            cx.push(mx);
            cy.push(my);
            ch.push(h);
            cr.push(rmin);
            cg.push(best);
            cu.push(mix(i, j, 0x5eed));
            stats[0] += 1;
            continue;
        };
        // its footprint reaches into a road: the square's trees each side of that road's line a clump of their own
        // (a forest road through the square had one clump over its lanes), each moved off the roads where it must be
        let g = ix.s[seg];
        let (dx, dy) = (g[2] - g[0], g[3] - g[1]);
        let (left, right): (Vec<usize>, Vec<usize>) =
            ids.iter().copied().partition(|&t| dx * (y[t] - g[1]) - dy * (x[t] - g[0]) >= 0.0);
        stats[1] += 1;
        for (side, sub) in [(0u64, left), (1u64, right)] {
            if sub.len() < min_members {
                continue;
            }
            let (sx, sy, sh, srmin, sbest) = clump_of_ids(&sub, x, y, group, height, rank, clump_of);
            match ix.place(sx, sy, rad(sh), max_shift)
                .filter(|&(px, py)| (px == sx && py == sy) || roads.free(px, py)) {
                Some((px, py)) => {
                    for &t in &sub {
                        member[t] = true;
                    }
                    if px != sx || py != sy {
                        stats[2] += 1;
                    }
                    cx.push(px);
                    cy.push(py);
                    ch.push(sh);
                    cr.push(srmin);
                    cg.push(sbest);
                    cu.push(mix(i, j, 0x5eed + 1 + side));
                }
                None => stats[3] += 1,
            }
        }
    }
    (member, cx, cy, ch, cr, cg, cu, stats)
}

/// The clumps of a cell's canopy trees. x, y: Web Mercator m; group / kind / height / rank / lcls per item; clump_of:
/// a group's clump group (NONE: none); tree_kind: the kind of a tree; skip_class: a land cover class whose trees stay
/// single (built-up land: street trees); cell: the grid's square (Web Mercator m, from the world's edge: a whole number
/// of them a geotile, so a square never straddles two); min_members: trees a square needs. A square's clump stands at
/// its trees' centroid, as tall as the mean of their taller half, with the lowest of their ranks (it shows wherever
/// one of them would), the group most of them map to (ties: the lower id).
/// Roads (the user, 2026-11-16: "take into consideration if the road is blocked by other buildings or trees"): a
/// clump's footprint (radius rad = (k, c): k * height + c ground m) keeps off the roads segs ((m, 4) c0 r0 c1 r1 on the
/// grid = (x0, y0, dx, dy): x = x0 + c dx, y = y0 - r dy) by their reach hws ((m,) grid px: the carriageway's half
/// width + a margin); merc_per_m: Web Mercator m per ground m. A square whose clump would reach into one is split along
/// that road's line, each side's trees (min_members of them) a clump of their own, moved straight off the roads by up
/// to max_shift_m where it still reaches in; one that cannot be cleared is left out and its trees stay single (a forest
/// road through a square stood under a 6-10 m clump seen from afar; stock East Coast USA keeps its group shapes 5-10 m
/// off the road's edge). Returns (member (n,), cx, cy, height, rank, group, u (a hashed [0, 1) per clump), stats
/// (clear, split, moved, left out)). own (x0, y0, x1, y1; Web Mercator m): the map cell's box (x0 <= x < x1,
/// y0 < y <= y1); blocked (rows, cols) on grid: buildings, water, decks. A moved clump stays in its cell and off them,
/// else its trees stay single. Deterministic: squares in key order, the left side's clump first.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn veg_clumps<'py>(py: Python<'py>, x: PyReadonlyArray1<'py, f64>, y: PyReadonlyArray1<'py, f64>,
                       group: PyReadonlyArray1<'py, u8>, kind: PyReadonlyArray1<'py, u8>,
                       height: PyReadonlyArray1<'py, f32>, rank: PyReadonlyArray1<'py, f32>,
                       lcls: PyReadonlyArray1<'py, u8>, clump_of: Vec<u8>, tree_kind: u8, skip_class: u8, cell: f64,
                       min_members: usize, segs: PyReadonlyArray2<'py, f64>, hws: PyReadonlyArray1<'py, f64>,
                       grid: (f64, f64, f64, f64), merc_per_m: f64, rad: (f64, f64), max_shift_m: f64,
                       own: (f64, f64, f64, f64), blocked: PyReadonlyArray2<'py, bool>)
                       -> PyResult<(Bound<'py, PyArray1<bool>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>,
                                    Bound<'py, PyArray1<f32>>, Bound<'py, PyArray1<f32>>, Bound<'py, PyArray1<u8>>,
                                    Bound<'py, PyArray1<f64>>, (u64, u64, u64, u64))> {
    let (x, y, group, kind, height, rank, lcls) = (x.as_array(), y.as_array(), group.as_array(), kind.as_array(),
                                                   height.as_array(), rank.as_array(), lcls.as_array());
    let n = x.len();
    if [y.len(), group.len(), kind.len(), height.len(), rank.len(), lcls.len()].iter().any(|&l| l != n) {
        return Err(PyValueError::new_err("veg_clumps: a value per item"));
    }
    if !(cell > 0.0) || min_members < 1 {
        return Err(PyValueError::new_err("veg_clumps: cell > 0, min_members >= 1"));
    }
    let (sg, hw) = (segs.as_array(), hws.as_array());
    if (sg.nrows() > 0 && sg.ncols() != 4) || sg.nrows() != hw.len() {
        return Err(PyValueError::new_err("veg_clumps: segs (m, 4) and a reach per segment"));
    }
    let (gx0, gy0, gdx, gdy) = grid;
    if !(gdx > 0.0 && gdy > 0.0 && merc_per_m > 0.0) {
        return Err(PyValueError::new_err("veg_clumps: grid steps and merc_per_m > 0"));
    }
    let seg_m: Vec<[f64; 5]> = (0..sg.nrows())
        .map(|q| [gx0 + sg[[q, 0]] * gdx, gy0 - sg[[q, 1]] * gdy, gx0 + sg[[q, 2]] * gdx, gy0 - sg[[q, 3]] * gdy,
                  hw[q] * gdx])
        .filter(|g| g.iter().all(|v| v.is_finite()))
        .collect();
    let bl = blocked.as_array();
    let (rows, cols) = bl.dim();
    let mask: Vec<u8> = bl.iter().map(|&b| b as u8).collect();     // (ndarray's logical order: row major)
    let roads = ClumpRoadArgs { segs: seg_m, merc_per_m, rad_k: rad.0, rad_c: rad.1, max_shift_m,
                                own: Some([own.0, own.1, own.2, own.3]),
                                blocked: if rows * cols > 0 { Some((mask, rows, cols, [gx0, gy0, gdx, gdy])) }
                                         else { None } };   // ((0, 0): none)
    let x = x.to_vec();
    let y = y.to_vec();
    let group = group.to_vec();
    let kind = kind.to_vec();
    let height = height.to_vec();
    let rank = rank.to_vec();
    let lcls = lcls.to_vec();
    let out = py.detach(move || {
        clumps_core(&x, &y, &group, &kind, &height, &rank, &lcls, &clump_of, tree_kind, skip_class, cell, min_members,
                    &roads)
    });
    let (member, cx, cy, ch, cr, cg, cu, st) = out;
    Ok((member.into_pyarray(py), cx.into_pyarray(py), cy.into_pyarray(py), ch.into_pyarray(py), cr.into_pyarray(py),
        cg.into_pyarray(py), cu.into_pyarray(py), (st[0], st[1], st[2], st[3])))
}

/// polygon.contains_points' test (even-odd crossings of a ray toward +x: an edge counts when lo <= y < hi and it
/// crosses y right of the point) for one point and one ring.
fn inside_ring(r: &[[f64; 2]], px: f64, py_: f64) -> bool {
    let n = r.len();
    if n < 3 {
        return false;
    }
    let mut c = 0u32;
    for i in 0..n {
        let (x0, y0) = (r[i][0], r[i][1]);
        let (x1, y1) = (r[(i + 1) % n][0], r[(i + 1) % n][1]);
        if y0 == y1 {
            continue;
        }
        let (lo, hi) = (y0.min(y1), y0.max(y1));
        if py_ >= lo && py_ < hi && x0 + (py_ - y0) * ((x1 - x0) / (y1 - y0)) > px {
            c += 1;
        }
    }
    c & 1 == 1
}

/// The lattice points (i * along + phase_a) u + (j * across + phase_b) v (v = u turned left) over the rings' extent
/// along u and v, inside the first ring and outside the others (holes), i-major. rings: (k, 2) vertices (open), ring_off
/// (r + 1,). None when the lattice would be over max_steps along either axis. Returns (pts (m, 2), i (m,), j (m,)).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn field_lattice<'py>(py: Python<'py>, rings: PyReadonlyArray2<'py, f64>, ring_off: PyReadonlyArray1<'py, i64>,
                          u: (f64, f64), along: f64, across: f64, phase_a: f64, phase_b: f64, max_steps: i64)
                          -> PyResult<Option<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>,
                                              Bound<'py, PyArray1<i64>>)>> {
    let (r, off) = (rings.as_array(), ring_off.as_array());
    if off.len() < 2 || !(along > 0.0 && across > 0.0) {
        return Err(PyValueError::new_err("field_lattice: one ring at least, along / across > 0"));
    }
    let rs: Vec<Vec<[f64; 2]>> = (0..off.len() - 1).map(|q| {
        (off[q] as usize..off[q + 1] as usize).map(|k| [r[[k, 0]], r[[k, 1]]]).collect()
    }).collect();
    let (ux, uy) = u;
    let (vx, vy) = (-uy, ux);
    let (mut amin, mut amax, mut bmin, mut bmax) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
    for p in &rs[0] {
        let (a, b) = (p[0] * ux + p[1] * uy, p[0] * vx + p[1] * vy);
        amin = amin.min(a); amax = amax.max(a);
        bmin = bmin.min(b); bmax = bmax.max(b);
    }
    if rs[0].len() < 3 {
        return Ok(None);
    }
    let (i0, i1) = (((amin - phase_a) / along).floor() as i64, ((amax - phase_a) / along).ceil() as i64);
    let (j0, j1) = (((bmin - phase_b) / across).floor() as i64, ((bmax - phase_b) / across).ceil() as i64);
    if i1 - i0 > max_steps || j1 - j0 > max_steps {
        return Ok(None);
    }
    let (pts, ii, jj) = py.detach(|| {
        let (mut pts, mut ii, mut jj) = (Vec::new(), Vec::new(), Vec::new());
        for i in i0..=i1 {
            let a = i as f64 * along + phase_a;
            for j in j0..=j1 {
                let b = j as f64 * across + phase_b;
                let (px, py_) = (a * ux + b * vx, a * uy + b * vy);
                if !inside_ring(&rs[0], px, py_) || rs[1..].iter().any(|h| h.len() >= 3 && inside_ring(h, px, py_)) {
                    continue;
                }
                pts.push(px);
                pts.push(py_);
                ii.push(i);
                jj.push(j);
            }
        }
        (pts, ii, jj)
    });
    let m = ii.len();
    Ok(Some((Array2::from_shape_vec((m, 2), pts).unwrap().into_pyarray(py), ii.into_pyarray(py),
             jj.into_pyarray(py))))
}

/// The direction a field's rows run along (vineyards, hay windrows): its dominant edge direction, the angle (mod 180
/// degrees, 1-degree bins) whose edges within +-window_deg add up to the most length, refined to their length-weighted
/// mean (doubled angles); a rectangle's long side, a trapezoid's parallel sides, where the principal axis of an odd
/// shape ran across its rows (Saint-Emilion's parcels, 2026-11-10 aerial). ring: (k, 2) vertices (open or closed).
/// Returns a unit (ux, uy) pointing east, or north when it runs north-south; (1, 0) for a degenerate ring.
#[pyfunction]
pub fn edge_axis(ring: PyReadonlyArray2<'_, f64>, window_deg: f64) -> (f64, f64) {
    let r = ring.as_array();
    let pts: Vec<[f64; 2]> = (0..r.nrows()).map(|i| [r[[i, 0]], r[[i, 1]]]).collect();
    edge_axis_core(&pts, window_deg)
}

pub(crate) fn edge_axis_core(r: &[[f64; 2]], window_deg: f64) -> (f64, f64) {
    let k = r.len();
    let mut bins = [0.0f64; 180];
    let mut edges: Vec<(f64, f64)> = Vec::with_capacity(k);       // (angle deg in [0, 180), length)
    for i in 0..k {
        let j = (i + 1) % k;
        let (dx, dy) = (r[j][0] - r[i][0], r[j][1] - r[i][1]);
        let l = dx.dhypot(dy);
        if !(l > 0.0) || !l.is_finite() {
            continue;
        }
        let a = dy.datan2(dx).to_degrees().rem_euclid(180.0);
        edges.push((a, l));
        bins[(a as usize).min(179)] += l;
    }
    if edges.is_empty() {
        return (1.0, 0.0);
    }
    let w = window_deg.max(0.0).round() as i64;
    let (mut best, mut best_l) = (0i64, f64::NEG_INFINITY);
    for b in 0..180i64 {
        let s: f64 = (-w..=w).map(|d| bins[(b + d).rem_euclid(180) as usize]).sum();
        if s > best_l {
            best_l = s;
            best = b;
        }
    }
    let centre = best as f64 + 0.5;
    let (mut sx, mut sy) = (0.0f64, 0.0f64);
    for &(a, l) in &edges {
        let d = (a - centre + 90.0).rem_euclid(180.0) - 90.0;      // signed difference mod 180
        if d.abs() <= w as f64 + 0.5 {
            let t = (2.0 * (centre + d)).to_radians();              // doubled: a direction, not a heading
            sx += l * t.dcos();
            sy += l * t.dsin();
        }
    }
    let a = if sx == 0.0 && sy == 0.0 { centre.to_radians() } else { 0.5 * sy.datan2(sx) };
    let (mut ux, mut uy) = (a.dcos(), a.dsin());
    if ux < -1e-9 || (ux.abs() <= 1e-9 && uy < 0.0) {
        ux = -ux;
        uy = -uy;
    }
    (ux, uy)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ring_test_is_even_odd() {
        let sq = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]];
        assert!(inside_ring(&sq, 5.0, 5.0));
        assert!(!inside_ring(&sq, 15.0, 5.0));
        assert!(!inside_ring(&sq, 5.0, 10.0));       // (the top edge: hi excluded)
        assert!(inside_ring(&sq, 5.0, 0.0));         // (the bottom edge: lo included)
    }

    #[test]
    fn edge_axis_of_a_skewed_parcel_is_its_long_sides() {
        // a parallelogram: long sides along +x (100 m), short ones slanted 45 degrees: its principal axis is skewed
        let (ux, uy) = edge_axis_core(&[[0.0, 0.0], [100.0, 0.0], [160.0, 60.0], [60.0, 60.0]], 8.0);
        assert!((ux - 1.0).abs() < 1e-9 && uy.abs() < 1e-9, "{ux} {uy}");
        let (ux, uy) = edge_axis_core(&[[0.0, 0.0], [0.0, 100.0], [-10.0, 100.0], [-10.0, 0.0]], 8.0);
        assert!(ux.abs() < 1e-9 && (uy - 1.0).abs() < 1e-9, "north-south: {ux} {uy}");
        assert_eq!(edge_axis_core(&[[1.0, 1.0]], 8.0), (1.0, 0.0));
    }

    // a square of trees (all conifers, group 1 -> clump 12) from (x0, y0) with offsets pts, height h
    fn trees(pts: &[(f64, f64)], h: f32) -> (Vec<f64>, Vec<f64>, Vec<u8>, Vec<u8>, Vec<f32>, Vec<f32>, Vec<u8>) {
        let n = pts.len();
        (pts.iter().map(|p| p.0).collect(), pts.iter().map(|p| p.1).collect(), vec![1; n], vec![0; n], vec![h; n],
         (0..n).map(|k| k as f32 * 0.1).collect(), vec![0; n])
    }

    fn table() -> Vec<u8> {
        let mut t = vec![NONE; 25];
        t[1] = 12;
        t
    }

    fn run(pts: &[(f64, f64)], h: f32, segs: Vec<[f64; 5]>, shift: f64)
           -> (Vec<bool>, Vec<f64>, Vec<f64>, Vec<f32>, Vec<f32>, Vec<u8>, Vec<f64>, [u64; 4]) {
        run_in(pts, h, segs, shift, None, None)
    }

    #[allow(clippy::type_complexity)]
    fn run_in(pts: &[(f64, f64)], h: f32, segs: Vec<[f64; 5]>, shift: f64, own: Option<[f64; 4]>,
              blocked: Option<(Vec<u8>, usize, usize, [f64; 4])>)
              -> (Vec<bool>, Vec<f64>, Vec<f64>, Vec<f32>, Vec<f32>, Vec<u8>, Vec<f64>, [u64; 4]) {
        let (x, y, g, k, hh, r, l) = trees(pts, h);
        let roads = ClumpRoadArgs { segs, merc_per_m: 1.0, rad_k: 0.3, rad_c: 2.0, max_shift_m: shift, own, blocked };
        clumps_core(&x, &y, &g, &k, &hh, &r, &l, &table(), 0, 9, 18.0, 3, &roads)
    }

    // the forest road through a square (a_clump_over_a_forest_road_splits_into_one_each_side's): its upper side's
    // clump moves up to ~SY + 17.6, its lower side's down to ~SY + 0.4
    const SPLIT_PTS: [(f64, f64); 6] = [(SX + 2.0, SY + 2.0), (SX + 9.0, SY + 1.0), (SX + 15.0, SY + 3.0),
                                        (SX + 3.0, SY + 16.0), (SX + 10.0, SY + 17.0), (SX + 16.0, SY + 15.0)];

    #[test]
    fn a_clump_moved_out_of_its_cell_leaves_its_trees_single() {
        // the cell's box ends at SY + 17: the next cell's tiles would never show the moved clump (Codex review)
        let road = vec![[SX - 50.0, SY + 9.0, SX + 70.0, SY + 9.0, 3.0]];
        let own = Some([SX - 100.0, SY - 100.0, SX + 100.0, SY + 17.0]);
        let (m, x, y, _h, _r, _g, _u, st) = run_in(&SPLIT_PTS, 12.0, road, 4.0, own, None);
        assert_eq!(x.len(), 1, "the lower side's only");
        assert!(y[0] < SY + 9.0);
        assert_eq!(st, [0, 1, 1, 1]);
        assert_eq!(m, vec![true, true, true, false, false, false], "the upper side's trees single");
    }

    #[test]
    fn a_clump_moved_onto_water_or_a_building_leaves_its_trees_single() {
        // a blocked strip (water, a building) over y in (SY + 17, SY + 18], on a 1 m grid from (SX - 10, SY + 30)
        let (rows, cols) = (40usize, 40usize);
        let mut mask = vec![0u8; rows * cols];
        for c in 0..cols {
            mask[12 * cols + c] = 1;
        }
        let road = vec![[SX - 50.0, SY + 9.0, SX + 70.0, SY + 9.0, 3.0]];
        let bl = Some((mask, rows, cols, [SX - 10.0, SY + 30.0, 1.0, 1.0]));
        let (m, x, y, _h, _r, _g, _u, st) = run_in(&SPLIT_PTS, 12.0, road, 4.0, None, bl);
        assert_eq!(x.len(), 1);
        assert!(y[0] < SY + 9.0);
        assert_eq!(st, [0, 1, 1, 1]);
        assert_eq!(m, vec![true, true, true, false, false, false]);
    }

    // a square's origin on the world grid (cell 18 m): its trees inside one square
    const SX: f64 = 18.0 * 1000.0 - HALF_WORLD;
    const SY: f64 = 18.0 * 2000.0 - HALF_WORLD;

    #[test]
    fn a_clump_over_a_forest_road_splits_into_one_each_side() {
        // a road along y = SY + 9 (the square's middle), carriageway half width 2.5 m + 0.5 margin; three trees each
        // side 4-8 m off its line: the one clump stood at its centroid on the lanes (radius 0.3 * 12 + 2 = 5.6 m)
        let pts = [(SX + 2.0, SY + 2.0), (SX + 9.0, SY + 1.0), (SX + 15.0, SY + 3.0),
                   (SX + 3.0, SY + 16.0), (SX + 10.0, SY + 17.0), (SX + 16.0, SY + 15.0)];
        let road = vec![[SX - 50.0, SY + 9.0, SX + 70.0, SY + 9.0, 3.0]];
        let (m0, x0, y0, ..) = run(&pts, 12.0, vec![], 4.0);
        assert_eq!(x0.len(), 1);
        assert!(m0.iter().all(|&v| v));
        assert!((y0[0] - (SY + 9.0)).abs() < 0.01, "without the road: on its line ({})", y0[0] - SY);
        let (m, x, y, h, _r, g, u, st) = run(&pts, 12.0, road.clone(), 4.0);
        assert_eq!(x.len(), 2, "one clump each side");
        assert_eq!(st, [0, 1, 2, 0]);
        assert!(m.iter().all(|&v| v));
        for k in 0..2 {
            let d = (y[k] - (SY + 9.0)).abs();
            let rad = 0.3 * h[k] as f64 + 2.0;
            assert!(d >= 3.0 + rad, "clump {k} {d} m off the line, reach {}", 3.0 + rad);
            assert!(x[k] > SX && x[k] < SX + 18.0);
            assert_eq!(g[k], 12);
        }
        assert!(y[0] > SY + 9.0 && y[1] < SY + 9.0, "the left side's first");
        assert_ne!(u[0], u[1]);
        // the same again: bit for bit
        let again = run(&pts, 12.0, road, 4.0);
        assert_eq!(again.1, x);
        assert_eq!(again.2, y);
    }

    #[test]
    fn clumps_clear_of_the_roads_are_as_before() {
        let pts = [(SX + 2.0, SY + 2.0), (SX + 9.0, SY + 4.0), (SX + 15.0, SY + 3.0), (SX + 5.0, SY + 12.0)];
        let far = vec![[SX - 50.0, SY + 40.0, SX + 70.0, SY + 40.0, 3.0]];
        let a = run(&pts, 12.0, vec![], 4.0);
        let b = run(&pts, 12.0, far, 4.0);
        assert_eq!((a.0, a.1, a.2, a.3, a.4, a.5, a.6), (b.0, b.1, b.2, b.3, b.4, b.5, b.6));
        assert_eq!(b.7, [1, 0, 0, 0]);
    }

    #[test]
    fn a_clump_that_cannot_clear_the_road_leaves_its_trees_single() {
        // trees 2-3 m off a wide road's line on one side only: the clump would have to move ~8 m (more than 4)
        let pts = [(SX + 2.0, SY + 11.0), (SX + 8.0, SY + 12.0), (SX + 14.0, SY + 11.5)];
        let road = vec![[SX - 50.0, SY + 9.0, SX + 70.0, SY + 9.0, 6.0]];
        let (m, x, _y, _h, _r, _g, _u, st) = run(&pts, 12.0, road.clone(), 4.0);
        assert!(x.is_empty());
        assert!(m.iter().all(|&v| !v), "its trees stay single (shown far as before the clumps)");
        assert_eq!(st, [0, 1, 0, 1]);
        // allowed to move far enough: moved off the road
        let (m, x, y, h, ..) = run(&pts, 12.0, road, 20.0);
        assert_eq!(x.len(), 1);
        assert!(m.iter().all(|&v| v));
        assert!((y[0] - (SY + 9.0)) >= 6.0 + 0.3 * h[0] as f64 + 2.0);
    }

    #[test]
    fn mix_is_uniformish_and_stable() {
        let a = mix(3, 4, 1);
        assert_eq!(a, mix(3, 4, 1));
        assert!(a >= 0.0 && a < 1.0);
        assert_ne!(a, mix(4, 3, 1));
    }
}
