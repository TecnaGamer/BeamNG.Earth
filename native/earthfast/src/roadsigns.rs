//! Ordinary road signs from what the road data states (task 017): render-only signs in the sign tiles, one shared mesh
//! on one opaque atlas (server/roadsigns.py is the glue, signstream.py sends them). A wrong sign is worse than a missing
//! one, so a rule stands only where the facts imply the sign, and a doubtful case gets none.
//!
//! Where (road_sign_place), per direction of travel on the road pieces between junctions:
//! - NO_ENTRY: where a one-way road ends and traffic could turn into it the wrong way: facing that traffic, just inside
//!   the road. Not on a road that carries straight on as one-way (a junction along it), nor where a divided road splits
//!   (the carriageway leaving is nearly opposite: there the layout says it).
//! - DEAD_END: facing traffic that turns into a two-way minor road leading only to its own end within DEAD_END_MAX_M.
//!   A track at the end (the road goes on, unpaved) is the caller's `open_ends`: no sign.
//! - CROSSING: at a marked crossing without signals (the caller's `crossings`, points on the road's centre line), each
//!   way of travel, at the crossing's near side.
//! A rule's signs are thinned per direction; signs stand beside the carriageway on the driving side, clear of every
//! road's pavement, never on a bridge or in a tunnel.
//!
//! How (road_sign_mesh): the sign's outline (outline(), the atlas cell's: tools/build_sign_atlas.py) cut as geometry, a
//! metal back, a round pole.

use std::collections::HashMap;

use numpy::ndarray::{ArrayView1, ArrayView2};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

use crate::speedsigns::{clear, splitmix, Mesh};

pub const NO_ENTRY: u8 = 0;
pub const DEAD_END: u8 = 1;
pub const CROSSING: u8 = 2;

const PRIMARY: i64 = 5;         // ranks (speedsigns.RANK)
const TRUNK: i64 = 6;
const TERTIARY: i64 = 3;

const NO_ENTRY_IN_M: f64 = 6.0;         // m inside the road's end
const DEAD_END_IN_M: f64 = 10.0;        // m past the junction
const DEAD_END_MIN_M: f64 = 40.0;       // a stub shorter than this is a driveway to the eye
const DEAD_END_MAX_M: f64 = 400.0;      // (well within the glue's margin: a road's end there is the map's, not the box's)
const DEAD_END_HOPS: usize = 4;
const OPEN_END_M: f64 = 4.0;            // an `open_ends` point this near the end: the road goes on
const CROSSING_ON_M: f64 = 2.0;         // a crossing this far from a centre line at most is on that road
const CROSSING_BACK_M: f64 = 1.5;       // m before the crossing
const STRAIGHT_COS: f64 = 0.7;
const SPLIT_COS: f64 = -0.85;           // a one-way leaving nearly opposite to the one arriving: a divided road's split
const TANGENT_M: f64 = 10.0;
const SAME_WAY_COS: f64 = 0.7;
const GAPS_M: [f64; 3] = [15.0, 30.0, 12.0];        // the same rule, the same way, within this: one sign
const ALONG_STEPS_M: [f64; 5] = [0.0, 2.0, 4.0, 6.0, 8.0];  // a blocked spot: tried farther along
const OUT_STEP_M: f64 = 0.5;
const OUT_MAX_M: f64 = 2.0;
const CLEAR_M: f64 = 0.25;

struct Road {
    a: usize,
    b: usize,
    s: Vec<f64>,
    hw: f64,
    dir: i64,
    rank: i64,
    urban: bool,
    link: bool,
}

struct Edge {
    road: usize,
    piece: usize,
    sa: f64,
    sb: f64,
    fwd: bool,
    from: i64,
    to: i64,
}

struct Net<'a> {
    pts: ArrayView2<'a, f64>,
    ll: ArrayView2<'a, f64>,
    roads: Vec<Road>,
    edges: Vec<Edge>,
}

impl Net<'_> {
    fn len(&self, e: usize) -> f64 { self.edges[e].sb - self.edges[e].sa }

    fn at(&self, r: usize, s: f64) -> ([f64; 2], [f64; 2]) {
        let rd = &self.roads[r];
        let n = rd.b - rd.a;
        let s = s.clamp(0.0, rd.s[n - 1]);
        let mut k = match rd.s.binary_search_by(|v| v.partial_cmp(&s).unwrap()) { Ok(k) => k, Err(k) => k.max(1) - 1 };
        k = k.min(n - 2);
        let l = rd.s[k + 1] - rd.s[k];
        let t = if l > 1e-9 { (s - rd.s[k]) / l } else { 0.0 };
        let (i, j) = (rd.a + k, rd.a + k + 1);
        let lerp = |m: &ArrayView2<f64>, c: usize| m[[i, c]] + t * (m[[j, c]] - m[[i, c]]);
        ([lerp(&self.pts, 0), lerp(&self.pts, 1)], [lerp(&self.ll, 0), lerp(&self.ll, 1)])
    }

    /// The direction of travel on road r over arc lengths [s0, s1], fwd: along the geometry (unit).
    fn chord(&self, r: usize, s0: f64, s1: f64, fwd: bool) -> [f64; 2] {
        let (p, _) = self.at(r, s0);
        let (q, _) = self.at(r, s1);
        let (dx, dy) = (q[0] - p[0], q[1] - p[1]);
        let l = (dx * dx + dy * dy).sqrt().max(1e-9);
        if fwd { [dx / l, dy / l] } else { [-dx / l, -dy / l] }
    }

    fn end_dir(&self, e: usize) -> [f64; 2] {
        let ed = &self.edges[e];
        let d = TANGENT_M.min(self.len(e));
        if ed.fwd { self.chord(ed.road, ed.sb - d, ed.sb, true) } else { self.chord(ed.road, ed.sa, ed.sa + d, false) }
    }

    fn start_dir(&self, e: usize) -> [f64; 2] {
        let ed = &self.edges[e];
        let d = TANGENT_M.min(self.len(e));
        if ed.fwd { self.chord(ed.road, ed.sa, ed.sa + d, true) } else { self.chord(ed.road, ed.sb - d, ed.sb, false) }
    }
}

fn dot(a: [f64; 2], b: [f64; 2]) -> f64 { a[0] * b[0] + a[1] * b[1] }

/// A rule's sign before its spot: on road `road` at arc length s, for traffic travelling fwd along the geometry or
/// against it; back: the spot moves towards the traffic when blocked, else away from it.
struct Cand {
    rule: u8,
    road: usize,
    s: f64,
    fwd: bool,
    back: bool,
    key: u64,
    xy: [f64; 2],
    dir: [f64; 2],
}

/// One sign: its spot, the direction of the traffic it faces, its rule, size class (0 town, 1 country, 2 major road),
/// the road point beside it, whether in town.
#[derive(Clone, Debug)]
pub struct Sign {
    pub xy: [f64; 2],
    pub dir: [f64; 2],
    pub rule: u8,
    pub size: usize,
    pub road: [f64; 2],
    pub urban: bool,
}

pub struct Input<'a> {
    pub pts: ArrayView2<'a, f64>,
    pub ll: ArrayView2<'a, f64>,
    pub off: ArrayView1<'a, i64>,
    pub meta: ArrayView2<'a, f64>,
    pub con_at: ArrayView1<'a, f64>,
    pub con_id: ArrayView1<'a, i64>,
    pub con_off: ArrayView1<'a, i64>,
    pub skip: ArrayView2<'a, f64>,
    pub skip_off: ArrayView1<'a, i64>,
    pub crossings: ArrayView2<'a, f64>,
    pub open_ends: ArrayView2<'a, f64>,
    pub poly: ArrayView2<'a, f64>,
    pub poly_off: ArrayView1<'a, i64>,
    pub bx: [f64; 4],
    pub left_hand: bool,
    pub lateral: [f64; 2],      // m from the carriageway's edge to the sign's near edge: town, country
    pub widest: f64,            // m: the widest sign, for the clearance of a spot
}

fn no_entry(net: &Net, incoming: &HashMap<i64, Vec<usize>>, outgoing: &HashMap<i64, Vec<usize>>, out: &mut Vec<Cand>) {
    let none: Vec<usize> = Vec::new();
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        if rd.dir == 0 || rd.link || rd.rank < 1 || rd.rank >= TRUNK { continue; }
        let d = net.end_dir(e);
        let mut onward = false;
        for &o in outgoing.get(&ed.to).unwrap_or(&none) {
            let oe = &net.edges[o];
            if oe.road == ed.road && oe.piece == ed.piece { continue; }
            let c = dot(net.start_dir(o), d);
            if net.roads[oe.road].dir != 0 && (c > STRAIGHT_COS || c < SPLIT_COS) { onward = true; }
        }
        if onward { continue; }
        // someone must be able to come here other than along this road
        let others = incoming.get(&ed.to).unwrap_or(&none).iter().any(|&i| {
            let o = &net.edges[i];
            o.road != ed.road && net.roads[o.road].rank >= 1
        });
        if !others { continue; }
        let inside = NO_ENTRY_IN_M.min(net.len(e) * 0.5);
        let s = if ed.fwd { ed.sb - inside } else { ed.sa + inside };
        out.push(cand(net, NO_ENTRY, ed.road, s, !ed.fwd, false));
    }
}

/// Whether travel on along e reaches only a dead end within the limit: its length, or None.
fn dead_length(net: &Net, e: usize, ends: &HashMap<i64, usize>, outgoing: &HashMap<i64, Vec<usize>>,
               open: &[[f64; 2]]) -> Option<f64> {
    let none: Vec<usize> = Vec::new();
    let (mut cur, mut total) = (e, 0.0);
    for _ in 0..DEAD_END_HOPS {
        let ed = &net.edges[cur];
        total += net.len(cur);
        if total > DEAD_END_MAX_M { return None; }
        let arms = *ends.get(&ed.to).unwrap_or(&0);
        if arms == 1 {
            let (p, _) = net.at(ed.road, if ed.fwd { ed.sb } else { ed.sa });
            let shut = open.iter().any(|q| (q[0] - p[0]).powi(2) + (q[1] - p[1]).powi(2) < OPEN_END_M * OPEN_END_M);
            return if shut { None } else { Some(total) };
        }
        if arms != 2 { return None; }
        let next: Vec<usize> = outgoing.get(&ed.to).unwrap_or(&none).iter().copied().filter(|&o| {
            let oe = &net.edges[o];
            !(oe.road == ed.road && oe.piece == ed.piece)
        }).collect();
        if next.len() != 1 { return None; }
        cur = next[0];
    }
    None
}

fn dead_ends(net: &Net, ends: &HashMap<i64, usize>, outgoing: &HashMap<i64, Vec<usize>>, open: &[[f64; 2]],
             out: &mut Vec<Cand>) {
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        if rd.dir != 0 || rd.link || rd.rank < 1 || rd.rank > TERTIARY { continue; }
        if *ends.get(&ed.from).unwrap_or(&0) < 3 { continue; }
        let Some(total) = dead_length(net, e, ends, outgoing, open) else { continue };
        if total < DEAD_END_MIN_M { continue; }
        let inside = DEAD_END_IN_M.min(net.len(e) * 0.5);
        let s = if ed.fwd { ed.sa + inside } else { ed.sb - inside };
        out.push(cand(net, DEAD_END, ed.road, s, ed.fwd, false));
    }
}

/// The road a crossing point lies on: (road, arc length), the nearest within CROSSING_ON_M.
fn on_road(net: &Net, q: [f64; 2]) -> Option<(usize, f64)> {
    let mut best: Option<(usize, f64, f64)> = None;
    for (r, rd) in net.roads.iter().enumerate() {
        if rd.rank < 1 || rd.rank > TRUNK || rd.link { continue; }
        for k in rd.a..rd.b.saturating_sub(1) {
            let (ax, ay, bx, by) = (net.pts[[k, 0]], net.pts[[k, 1]], net.pts[[k + 1, 0]], net.pts[[k + 1, 1]]);
            let (dx, dy) = (bx - ax, by - ay);
            let l2 = (dx * dx + dy * dy).max(1e-12);
            let t = (((q[0] - ax) * dx + (q[1] - ay) * dy) / l2).clamp(0.0, 1.0);
            let d2 = (ax + t * dx - q[0]).powi(2) + (ay + t * dy - q[1]).powi(2);
            if d2 <= CROSSING_ON_M * CROSSING_ON_M && best.map_or(true, |b| d2 < b.2) {
                best = Some((r, rd.s[k - rd.a] + t * l2.sqrt(), d2));
            }
        }
    }
    best.map(|b| (b.0, b.1))
}

fn crossings(net: &Net, nodes: &ArrayView2<f64>, out: &mut Vec<Cand>) {
    for k in 0..nodes.nrows() {
        let q = [nodes[[k, 0]], nodes[[k, 1]]];
        let Some((r, s)) = on_road(net, q) else { continue };
        let (dir, total) = (net.roads[r].dir, net.roads[r].s[net.roads[r].b - net.roads[r].a - 1]);
        if total < 2.0 * CROSSING_BACK_M { continue; }
        for fwd in [true, false] {
            if (dir > 0 && !fwd) || (dir < 0 && fwd) { continue; }
            let at = if fwd { s - CROSSING_BACK_M } else { s + CROSSING_BACK_M };
            if at <= 0.0 || at >= total { continue; }
            out.push(cand(net, CROSSING, r, at, fwd, true));
        }
    }
}

fn cand(net: &Net, rule: u8, road: usize, s: f64, fwd: bool, back: bool) -> Cand {
    let (xy, lonlat) = net.at(road, s);
    let key = splitmix(splitmix(((lonlat[0] * 1e6).round() as i64) as u64) ^ ((lonlat[1] * 1e6).round() as i64) as u64)
        ^ (rule as u64) ^ ((fwd as u64) << 8);
    let total = net.roads[road].s[net.roads[road].b - net.roads[road].a - 1];
    let dir = net.chord(road, (s - 3.0).max(0.0), (s + 3.0).min(total), fwd);
    Cand { rule, road, s, fwd, back, key, xy, dir }
}

/// The signs of a box. Arrays as speed_sign_place's; see Input.
pub fn plan(a: &Input) -> Vec<Sign> {
    let mut net = Net { pts: a.pts, ll: a.ll, roads: Vec::new(), edges: Vec::new() };
    let nr = a.off.len().saturating_sub(1);
    let mut ends: HashMap<i64, usize> = HashMap::new();
    for r in 0..nr {
        let (p, q) = (a.off[r] as usize, a.off[r + 1] as usize);
        let mut s = vec![0.0f64; q.saturating_sub(p)];
        for k in p + 1..q {
            s[k - p] = s[k - p - 1] + ((a.pts[[k, 0]] - a.pts[[k - 1, 0]]).powi(2) + (a.pts[[k, 1]] - a.pts[[k - 1, 1]]).powi(2)).sqrt();
        }
        let road = Road { a: p, b: q, s, hw: a.meta[[r, 0]], dir: a.meta[[r, 1]] as i64, rank: a.meta[[r, 2]] as i64,
                          urban: a.meta[[r, 3]] != 0.0, link: a.meta[[r, 4]] != 0.0 };
        let total = if q >= p + 2 { road.s[q - p - 1] } else { 0.0 };
        net.roads.push(road);
        if total < 1.0 { continue; }
        let mut nodes: Vec<(f64, i64)> = Vec::new();
        for k in a.con_off[r] as usize..a.con_off[r + 1] as usize {
            let f = a.con_at[k];
            nodes.push((if f < 1e-3 { 0.0 } else if f > 1.0 - 1e-3 { 1.0 } else { f }, a.con_id[k]));
        }
        nodes.sort_by(|x, y| x.partial_cmp(y).unwrap());
        nodes.dedup_by(|x, y| (x.0 - y.0).abs() < 1e-9);
        if nodes.first().map_or(true, |n| n.0 > 0.0) { nodes.insert(0, (0.0, -(2 * r as i64) - 1)); }
        if nodes.last().map_or(true, |n| n.0 < 1.0) { nodes.push((1.0, -(2 * r as i64) - 2)); }
        let dir = net.roads[r].dir;
        for (piece, w) in nodes.windows(2).enumerate() {
            let (sa, sb) = (w[0].0 * total, w[1].0 * total);
            if sb - sa < 0.5 { continue; }
            *ends.entry(w[0].1).or_default() += 1;
            *ends.entry(w[1].1).or_default() += 1;
            for fwd in [true, false] {
                if (dir > 0 && !fwd) || (dir < 0 && fwd) { continue; }
                let (from, to) = if fwd { (w[0].1, w[1].1) } else { (w[1].1, w[0].1) };
                net.edges.push(Edge { road: r, piece, sa, sb, fwd, from, to });
            }
        }
    }
    let mut outgoing: HashMap<i64, Vec<usize>> = HashMap::new();
    let mut incoming: HashMap<i64, Vec<usize>> = HashMap::new();
    for (i, e) in net.edges.iter().enumerate() {
        outgoing.entry(e.from).or_default().push(i);
        incoming.entry(e.to).or_default().push(i);
    }
    let open: Vec<[f64; 2]> = (0..a.open_ends.nrows()).map(|k| [a.open_ends[[k, 0]], a.open_ends[[k, 1]]]).collect();

    let mut cands = Vec::new();
    no_entry(&net, &incoming, &outgoing, &mut cands);
    dead_ends(&net, &ends, &outgoing, &open, &mut cands);
    crossings(&net, &a.crossings, &mut cands);
    cands.sort_by(|x, y| (x.rule, x.key).cmp(&(y.rule, y.key)));

    // one a place, per rule and direction, in a fixed order
    let mut kept: Vec<usize> = Vec::new();
    let mut grid: HashMap<(u8, i64, i64), Vec<usize>> = HashMap::new();
    let cell = 40.0;
    for (i, c) in cands.iter().enumerate() {
        let (gx, gy) = ((c.xy[0] / cell).floor() as i64, (c.xy[1] / cell).floor() as i64);
        let gap = GAPS_M[c.rule as usize];
        let mut near = false;
        'scan: for dx in -1..=1 {
            for dy in -1..=1 {
                for &j in grid.get(&(c.rule, gx + dx, gy + dy)).map_or(&[][..], |v| &v[..]) {
                    let o = &cands[j];
                    if dot(o.dir, c.dir) > SAME_WAY_COS
                        && (o.xy[0] - c.xy[0]).powi(2) + (o.xy[1] - c.xy[1]).powi(2) < gap * gap {
                        near = true;
                        break 'scan;
                    }
                }
            }
        }
        if !near {
            grid.entry((c.rule, gx, gy)).or_default().push(i);
            kept.push(i);
        }
    }

    let boxes: Vec<[f64; 4]> = (0..a.poly_off.len().saturating_sub(1)).map(|q| {
        let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for k in a.poly_off[q] as usize..a.poly_off[q + 1] as usize {
            b[0] = b[0].min(a.poly[[k, 0]]); b[1] = b[1].min(a.poly[[k, 1]]);
            b[2] = b[2].max(a.poly[[k, 0]]); b[3] = b[3].max(a.poly[[k, 1]]);
        }
        b
    }).collect();
    let side = if a.left_hand { -1.0 } else { 1.0 };
    let mut out = Vec::new();
    for &i in &kept {
        let c = &cands[i];
        let rd = &net.roads[c.road];
        let total = rd.s[rd.b - rd.a - 1];
        let f = c.s / total.max(1e-9);
        let (sa, sb) = (a.skip_off[c.road] as usize, a.skip_off[c.road + 1] as usize);
        if (sa..sb).any(|k| a.skip[[k, 0]] - 0.02 <= f && f <= a.skip[[k, 1]] + 0.02) { continue; }
        let base = rd.hw + a.lateral[if rd.urban { 0 } else { 1 }] + a.widest / 2.0;
        let along = if c.fwd != c.back { 1.0 } else { -1.0 };   // (the spot moves with the traffic, or back towards it)
        let mut found = None;
        'spot: for du in ALONG_STEPS_M {
            let s = c.s + along * du;
            if s <= 0.0 || s >= total { break; }
            let (road_xy, _) = net.at(c.road, s);
            let t = net.chord(c.road, (s - 3.0).max(0.0), (s + 3.0).min(total), c.fwd);
            let right = [t[1] * side, -t[0] * side];
            let mut extra = 0.0;
            while extra <= OUT_MAX_M + 1e-9 {
                let q = [road_xy[0] + right[0] * (base + extra), road_xy[1] + right[1] * (base + extra)];
                if clear(q, a.widest / 2.0 + CLEAR_M, &a.poly, &a.poly_off, &boxes) {
                    found = Some((q, t, road_xy));
                    break 'spot;
                }
                extra += OUT_STEP_M;
            }
        }
        let Some((q, t, road_xy)) = found else { continue };
        if !(q[0] >= a.bx[0] && q[0] < a.bx[2] && q[1] >= a.bx[1] && q[1] < a.bx[3]) { continue; }
        let size = if rd.urban { 0 } else if rd.rank >= PRIMARY { 2 } else { 1 };
        out.push(Sign { xy: q, dir: t, rule: c.rule, size, road: road_xy, urban: rd.urban });
    }
    out
}

/// The road signs of a box (x0, y0, x1, y1). Roads, junctions and bridge / tunnel ranges as speed_sign_place's; crossings
/// (c, 2) points on the roads' centre lines; open_ends (o, 2) where a road goes on unpaved. lateral: m from the
/// carriageway's edge to the sign's near edge [town, country]; widest: m, the widest sign's width.
/// Returns (k, 9) [x, y, travel x, travel y, rule, size class, road x, road y, urban].
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn road_sign_place<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, ll: PyReadonlyArray2<'py, f64>,
                            off: PyReadonlyArray1<'py, i64>, meta: PyReadonlyArray2<'py, f64>,
                            con_at: PyReadonlyArray1<'py, f64>, con_id: PyReadonlyArray1<'py, i64>,
                            con_off: PyReadonlyArray1<'py, i64>, skip: PyReadonlyArray2<'py, f64>,
                            skip_off: PyReadonlyArray1<'py, i64>, crossings: PyReadonlyArray2<'py, f64>,
                            open_ends: PyReadonlyArray2<'py, f64>, poly: PyReadonlyArray2<'py, f64>,
                            poly_off: PyReadonlyArray1<'py, i64>, bx: [f64; 4], left_hand: bool, lateral: [f64; 2],
                            widest: f64) -> Bound<'py, PyArray2<f64>> {
    let signs = plan(&Input { pts: pts.as_array(), ll: ll.as_array(), off: off.as_array(), meta: meta.as_array(),
                              con_at: con_at.as_array(), con_id: con_id.as_array(), con_off: con_off.as_array(),
                              skip: skip.as_array(), skip_off: skip_off.as_array(), crossings: crossings.as_array(),
                              open_ends: open_ends.as_array(), poly: poly.as_array(), poly_off: poly_off.as_array(),
                              bx, left_hand, lateral, widest });
    let n = signs.len();
    let rows: Vec<f64> = signs.iter().flat_map(|s| [s.xy[0], s.xy[1], s.dir[0], s.dir[1], s.rule as f64, s.size as f64,
                                                    s.road[0], s.road[1], if s.urban { 1.0 } else { 0.0 }]).collect();
    numpy::ndarray::Array2::from_shape_vec((n, 9), rows).unwrap().into_pyarray(py)
}

// ---- the mesh ----

const M_ATLAS: i64 = 0;
const M_METAL: i64 = 1;
const TRI_TOP: f64 = 0.90;      // tools/build_sign_atlas.py
const TRI_BOTTOM: f64 = -0.83;
const CORNER: f64 = 0.12;
const BACK_M: f64 = 0.004;

/// A convex outline's corners rounded by r (tools/build_sign_atlas.py rounded()).
fn round_poly(verts: &[[f64; 2]], r: f64) -> Vec<[f64; 2]> {
    const STEPS: usize = 4;
    let n = verts.len();
    let mut out = Vec::with_capacity(n * (STEPS + 1));
    for i in 0..n {
        let (p, a, b) = (verts[(i + n - 1) % n], verts[i], verts[(i + 1) % n]);
        let (ux, uy) = (p[0] - a[0], p[1] - a[1]);
        let (vx, vy) = (b[0] - a[0], b[1] - a[1]);
        let (lu, lv) = ((ux * ux + uy * uy).sqrt(), (vx * vx + vy * vy).sqrt());
        let (ux, uy, vx, vy) = (ux / lu, uy / lu, vx / lv, vy / lv);
        let half = (ux * vx + uy * vy).clamp(-1.0, 1.0).acos() / 2.0;
        let d = r / half.tan();
        let (bx, by) = (ux + vx, uy + vy);
        let lb = (bx * bx + by * by).sqrt();
        let c = [a[0] + bx / lb * r / half.sin(), a[1] + by / lb * r / half.sin()];
        let a0 = (a[1] + uy * d - c[1]).atan2(a[0] + ux * d - c[0]);
        let a1 = (a[1] + vy * d - c[1]).atan2(a[0] + vx * d - c[0]);
        let tau = std::f64::consts::TAU;
        let da = (a1 - a0 + std::f64::consts::PI).rem_euclid(tau) - std::f64::consts::PI;
        for k in 0..=STEPS {
            let t = a0 + da * k as f64 / STEPS as f64;
            out.push([c[0] + r * t.cos(), c[1] + r * t.sin()]);
        }
    }
    out
}

/// A shape's outline in cell units ([-1, 1]^2, y up), counter-clockwise as the driver sees it: 0 circle, 1 triangle,
/// 2 inverted triangle, 3 diamond, 4 octagon, 5 square.
pub fn outline(shape: i64) -> Vec<[f64; 2]> {
    match shape {
        1 => round_poly(&[[0.0, TRI_TOP], [-1.0, TRI_BOTTOM], [1.0, TRI_BOTTOM]], CORNER),
        2 => round_poly(&[[0.0, -TRI_TOP], [1.0, -TRI_BOTTOM], [-1.0, -TRI_BOTTOM]], CORNER),
        3 => round_poly(&[[0.0, 0.98], [-0.98, 0.0], [0.0, -0.98], [0.98, 0.0]], CORNER),
        4 => {
            let t = 0.98 * (std::f64::consts::PI / 8.0).tan();
            vec![[t, 0.98], [-t, 0.98], [-0.98, t], [-0.98, -t], [-t, -0.98], [t, -0.98], [0.98, -t], [0.98, t]]
        }
        5 => round_poly(&[[0.97, 0.97], [-0.97, 0.97], [-0.97, -0.97], [0.97, -0.97]], 0.10),
        _ => (0..32).map(|k| {
            let a = k as f64 * std::f64::consts::TAU / 32.0;
            [0.99 * a.cos(), 0.99 * a.sin()]
        }).collect(),
    }
}

/// The mesh of signs (k, 10) [x, y, ground z, road z, travel x, travel y, atlas cell, shape, width m, lower edge above
/// the road m] on an atlas of cols x rows cells, relative to origin. Returns (vertices, triangles, uv, material of
/// each triangle): 0 the atlas, 1 metal.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn road_sign_mesh<'py>(py: Python<'py>, signs: PyReadonlyArray2<'py, f64>, cols: i64, rows: i64, origin: [f64; 3])
                           -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>,
                               Bound<'py, PyArray1<i64>>) {
    let signs = signs.as_array();
    let mut m = Mesh { p: Vec::new(), t: Vec::new(), uv: Vec::new(), mi: Vec::new(), centre: [0.0; 3], face: [0.0; 2],
                       right: [0.0; 2] };
    let mut shapes: HashMap<i64, Vec<[f64; 2]>> = HashMap::new();
    for k in 0..signs.nrows() {
        let r = signs.row(k);
        let (x, y, zg, zr, tx, ty) = (r[0], r[1], r[2], r[3], r[4], r[5]);
        if !(x.is_finite() && y.is_finite() && zg.is_finite() && zr.is_finite()) { continue; }
        let (cell, shape, w, mount) = (r[6] as i64, r[7] as i64, r[8], r[9]);
        let tl = (tx * tx + ty * ty).sqrt().max(1e-9);
        m.centre = [x - origin[0], y - origin[1], zg - origin[2]];
        m.face = [-tx / tl, -ty / tl];
        m.right = [ty / tl, -tx / tl];
        let bottom = (zr + mount).max(zg + 1.2) - zg;
        let (half, zc) = (w / 2.0, bottom + w / 2.0);
        m.pole(-0.05, 0.03, -0.3, zc + 0.3 * half, M_METAL);
        let out = shapes.entry(shape).or_insert_with(|| outline(shape));
        let (cu, cv) = ((cell % cols) as f64, (cell / cols) as f64);
        let n = out.len();
        let i = m.p.len() as i64;
        for q in out.iter() {
            let w3 = m.world([0.0, q[0] * half, zc + q[1] * half]);
            m.p.push(w3);
            m.uv.push([(cu + (1.0 + q[0]) / 2.0) / cols as f64, 1.0 - (cv + (1.0 - q[1]) / 2.0) / rows as f64]);
        }
        for q in out.iter() {
            let w3 = m.world([-BACK_M, q[0] * half, zc + q[1] * half]);
            m.p.push(w3);
            m.uv.push([0.0, 0.0]);
        }
        for j in 1..n as i64 - 1 {
            m.t.push([i, i + j, i + j + 1]);
            m.mi.push(M_ATLAS);
            let b = i + n as i64;
            m.t.push([b, b + j + 1, b + j]);
            m.mi.push(M_METAL);
        }
    }
    let (n, t) = (m.p.len(), m.t.len());
    let p = numpy::ndarray::Array2::from_shape_vec((n, 3), m.p.into_iter().flatten().collect()).unwrap();
    let tri = numpy::ndarray::Array2::from_shape_vec((t, 3), m.t.into_iter().flatten().collect()).unwrap();
    let uv = numpy::ndarray::Array2::from_shape_vec((n, 2), m.uv.into_iter().flatten().collect()).unwrap();
    (p.into_pyarray(py), tri.into_pyarray(py), uv.into_pyarray(py), m.mi.into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;
    use numpy::ndarray::{arr1, arr2, Array1, Array2};

    /// Roads as (points, direction, rank, junction ids at their two ends), half width 4 m, in town.
    struct T {
        pts: Array2<f64>,
        off: Array1<i64>,
        meta: Array2<f64>,
        con_at: Array1<f64>,
        con_id: Array1<i64>,
        con_off: Array1<i64>,
    }

    fn roads(list: &[(&[[f64; 2]], f64, f64, [i64; 2])]) -> T {
        let (mut p, mut off, mut meta, mut at, mut id, mut co) = (vec![], vec![0i64], vec![], vec![], vec![], vec![0i64]);
        for (line, dir, rank, ends) in list {
            p.extend(line.iter().flatten().copied());
            off.push(p.len() as i64 / 2);
            meta.extend([4.0, *dir, *rank, 1.0, 0.0]);
            for (f, c) in [(0.0, ends[0]), (1.0, ends[1])] {
                if c >= 0 { at.push(f); id.push(c); }
            }
            co.push(at.len() as i64);
        }
        let n = p.len() / 2;
        T { pts: Array2::from_shape_vec((n, 2), p).unwrap(), off: Array1::from(off),
            meta: Array2::from_shape_vec((list.len(), 5), meta).unwrap(), con_at: Array1::from(at),
            con_id: Array1::from(id), con_off: Array1::from(co) }
    }

    fn run(t: &T, cross: Array2<f64>, open: Array2<f64>, left: bool) -> Vec<Sign> {
        let skip = Array2::<f64>::zeros((0, 2));
        let skip_off = Array1::<i64>::zeros(t.off.len());
        let poly = Array2::<f64>::zeros((0, 2));
        let poly_off = arr1(&[0i64]);
        plan(&Input { pts: t.pts.view(), ll: t.pts.view(), off: t.off.view(), meta: t.meta.view(), con_at: t.con_at.view(),
                      con_id: t.con_id.view(), con_off: t.con_off.view(), skip: skip.view(), skip_off: skip_off.view(),
                      crossings: cross.view(), open_ends: open.view(), poly: poly.view(), poly_off: poly_off.view(),
                      bx: [-1e4, -1e4, 1e4, 1e4], left_hand: left, lateral: [0.5, 1.2], widest: 0.75 })
    }

    fn none() -> Array2<f64> { Array2::zeros((0, 2)) }

    fn t_junction(oneway: f64) -> T {
        // an east-west two-way street, and a one-way street coming south into it at (0, 0)
        roads(&[(&[[-1000.0, 0.0], [0.0, 0.0]], 0.0, 3.0, [1, 2]), (&[[0.0, 0.0], [1000.0, 0.0]], 0.0, 3.0, [2, 3]),
                (&[[0.0, 100.0], [0.0, 0.0]], oneway, 1.0, [4, 2])])
    }

    #[test]
    fn no_entry_faces_wrong_way_traffic_at_the_end_of_a_one_way() {
        let s = run(&t_junction(1.0), none(), none(), false);
        let e: Vec<_> = s.iter().filter(|x| x.rule == NO_ENTRY).collect();
        assert_eq!(e.len(), 1, "{s:?}");
        assert!(e[0].dir[1] > 0.99, "faces traffic going north: {:?}", e[0].dir);
        assert!(e[0].xy[0] > 4.0, "on that traffic's right: {:?}", e[0].xy);
        assert!(e[0].xy[1] > 0.0 && e[0].xy[1] < 20.0);
    }

    #[test]
    fn left_hand_traffic_has_it_on_the_left() {
        let s = run(&t_junction(1.0), none(), none(), true);
        let e = s.iter().find(|x| x.rule == NO_ENTRY).unwrap();
        assert!(e.xy[0] < -4.0, "{:?}", e.xy);
    }

    #[test]
    fn no_entry_not_where_the_one_way_carries_on() {
        let t = roads(&[(&[[0.0, 100.0], [0.0, 0.0]], 1.0, 1.0, [1, 2]), (&[[0.0, 0.0], [0.0, -100.0]], 1.0, 1.0, [2, 3]),
                        (&[[-100.0, 0.0], [0.0, 0.0]], 0.0, 1.0, [4, 2])]);
        assert!(run(&t, none(), none(), false).iter().all(|x| x.rule != NO_ENTRY));
    }

    #[test]
    fn no_entry_not_where_a_divided_road_splits() {
        // a two-way road from the west splits at (0, 0): the two carriageways nearly parallel
        let t = roads(&[(&[[-1000.0, 0.0], [0.0, 0.0]], 0.0, 3.0, [1, 2]),
                        (&[[0.0, 0.0], [100.0, 8.0]], 1.0, 3.0, [2, 3]), (&[[100.0, -8.0], [0.0, 0.0]], 1.0, 3.0, [4, 2])]);
        assert!(run(&t, none(), none(), false).iter().all(|x| x.rule != NO_ENTRY));
    }

    #[test]
    fn no_entry_needs_someone_to_come_from_elsewhere() {
        let t = roads(&[(&[[0.0, 100.0], [0.0, 0.0]], 1.0, 1.0, [1, 2])]);
        assert!(run(&t, none(), none(), false).is_empty());
    }

    #[test]
    fn dead_end_only_on_entering_a_road_that_ends() {
        let s = run(&t_junction(0.0), none(), none(), false);
        let d: Vec<_> = s.iter().filter(|x| x.rule == DEAD_END).collect();
        assert_eq!(d.len(), 1, "{s:?}");
        assert!(d[0].dir[1] > 0.99, "faces traffic turning in: {:?}", d[0].dir);
    }

    #[test]
    fn dead_end_not_where_a_track_goes_on() {
        let open = arr2(&[[0.0, 100.0]]);
        assert!(run(&t_junction(0.0), none(), open, false).iter().all(|x| x.rule != DEAD_END));
    }

    #[test]
    fn dead_end_not_on_a_long_road() {
        let t = roads(&[(&[[-1000.0, 0.0], [0.0, 0.0]], 0.0, 3.0, [1, 2]), (&[[0.0, 0.0], [1000.0, 0.0]], 0.0, 3.0, [2, 3]),
                        (&[[0.0, 600.0], [0.0, 0.0]], 0.0, 1.0, [4, 2])]);
        assert!(run(&t, none(), none(), false).iter().all(|x| x.rule != DEAD_END));
    }

    #[test]
    fn crossing_gets_one_sign_each_way_on_a_two_way_and_one_on_a_one_way() {
        let line: &[[f64; 2]] = &[[0.0, 0.0], [200.0, 0.0]];
        let two = roads(&[(line, 0.0, 3.0, [1, 2])]);
        let at = arr2(&[[100.0, 0.5]]);
        let s = run(&two, at.clone(), none(), false);
        assert_eq!(s.iter().filter(|x| x.rule == CROSSING).count(), 2, "{s:?}");
        let one = roads(&[(line, 1.0, 3.0, [1, 2])]);
        let s = run(&one, at, none(), false);
        assert_eq!(s.len(), 1, "{s:?}");
        assert!(s[0].dir[0] > 0.99 && s[0].xy[0] < 100.0 && s[0].xy[1] < -4.0, "{s:?}");
    }

    #[test]
    fn crossing_away_from_every_road_gets_none() {
        let t = roads(&[(&[[0.0, 0.0], [200.0, 0.0]], 0.0, 3.0, [1, 2])]);
        assert!(run(&t, arr2(&[[100.0, 9.0]]), none(), false).is_empty());
    }

    #[test]
    fn the_same_every_time() {
        let t = t_junction(1.0);
        let a: Vec<String> = run(&t, arr2(&[[50.0, 0.0]]), none(), false).iter().map(|s| format!("{s:?}")).collect();
        let b: Vec<String> = run(&t, arr2(&[[50.0, 0.0]]), none(), false).iter().map(|s| format!("{s:?}")).collect();
        assert_eq!(a, b);
    }

    #[test]
    fn outlines_are_counter_clockwise_and_in_the_cell() {
        for shape in 0..6 {
            let o = outline(shape);
            let area: f64 = (0..o.len()).map(|i| {
                let (a, b) = (o[i], o[(i + 1) % o.len()]);
                a[0] * b[1] - b[0] * a[1]
            }).sum();
            assert!(area > 0.0, "shape {shape}");
            assert!(o.iter().all(|p| p[0].abs() <= 1.0 && p[1].abs() <= 1.0), "shape {shape}");
        }
    }
}
