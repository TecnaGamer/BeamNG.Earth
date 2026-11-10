//! Speed-limit signs along the roads (the user, 2026-10-28: "speed limit panels could be a good fallback in case osm
//! is missing data"): render-only panels in the sign tiles (server/speedsigns.py is the glue, signstream.py sends them).
//! The posted value is the one the AI drives (navgraph.road_mph: the mapped limit, else the class default), so a
//! sign never disagrees with the traffic.
//!
//! Where (speed_sign_place), per direction of travel on the road pieces between junctions:
//! - CHANGE: where the limit changes along the road (MUTCD 2009 2B.13: "shall be located at the points of change from
//!   one speed limit to another"; Vienna Convention / StVO Zeichen 274 the same): just past a junction whose straight
//!   on continuation had another limit, at the change inside a road, and on a road entered by turning when it has
//!   another limit than the road turned from is not known (no straight on continuation: a turn-in).
//! - JUNCTION: past a major junction (MUTCD 2B.13: "Additional Speed Limit signs should be installed beyond major
//!   intersections"; VwV-StVO zu Zeichen 274: repeated after junctions): a tertiary-or-better road leaving a junction
//!   that another secondary-or-better road meets.
//! - MERGE: on a motorway / trunk carriageway past an entrance ramp's merge (state practice: beyond each
//!   interchange's entrance), MERGE_M after the ramp joins.
//! - REPEAT: a reminder on long stretches (PR #363's 2 mile repeaters; MUTCD leaves the interval to the agencies):
//!   where the road crosses a fixed lat/lon grid of about the spacing (frame free: the same in every anchor), dropped
//!   near another sign of the same value.
//! A candidate is dropped near a kept one of the same value facing the same way (greedy in a fixed order: changes,
//! merges, junctions, repeats; then a hash of the position), so a dense grid of arterials does not get a sign every
//! block. Signs stand beside the carriageway on the driving side, clear of every road's pavement (moved out, then
//! along the road), never on a bridge or in a tunnel.
//!
//! How (speed_sign_mesh): US: MUTCD R2-1 (white rectangle, "SPEED LIMIT", the number), its sizes by road (Table
//! 2B-1: 24x30 in, 30x36 multi-lane, 36x48 expressway, 48x60 freeway); Canada: Rb-1 ("MAXIMUM", the number, "km/h");
//! elsewhere the Vienna Convention's C14 (white disc, red ring, the number; 600 / 750 / 900 mm). Lettering: the sign
//! atlas (server/sign_glyphs.json, Roadgeek 2014 Series EEM), condensed for the round signs.

use std::collections::HashMap;

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

const CHANGE: u8 = 0;
const MERGE: u8 = 1;
const JUNCTION: u8 = 2;
const REPEAT: u8 = 3;

const TERTIARY: i64 = 3;    // ranks (speedsigns.RANK): residential 1, unclassified 2, tertiary 3 ... motorway 7
const SECONDARY: i64 = 4;
const PRIMARY: i64 = 5;
const TRUNK: i64 = 6;
const MOTORWAY: i64 = 7;

const STRAIGHT_COS: f64 = 0.75;     // a junction's straight on continuation: within ~41 degrees ...
const STRAIGHT_MARGIN: f64 = 0.1;   // ... and clearly straighter than the next best
const TANGENT_M: f64 = 10.0;        // m of a piece its direction at an end is taken over
const CHANGE_IN_M: f64 = 5.0;       // m past a change inside a road / at a junction of two pieces only
const JUNCTION_IN_URBAN_M: f64 = 30.0;  // m past a junction (in town)
const JUNCTION_IN_RURAL_M: f64 = 60.0;  // m past a junction (out of town)
const MERGE_M: f64 = 300.0;         // m past an entrance ramp's join (beyond the acceleration lane, ~1000 ft)
const GAP_CHANGE_M: f64 = 60.0;     // a candidate is dropped within this of a kept one (same value, same way) ...
const GAP_MERGE_M: f64 = 800.0;
const GAP_JUNCTION_URBAN_M: f64 = 400.0;
const GAP_JUNCTION_RURAL_M: f64 = 1500.0;
const GAP_REPEAT_SHARE: f64 = 0.6;  // ... a repeat within this share of its spacing
const SAME_WAY_COS: f64 = 0.7;
const ACROSS_M: f64 = 20.0;         // ... and no farther than this (plus a share of the distance) to its side
const ACROSS_SHARE: f64 = 0.15;
const ALONG_STEPS_M: [f64; 7] = [0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0];  // a blocked spot: tried farther on
const OUT_STEP_M: f64 = 0.5;        // ... and farther out, up to OUT_MAX_M
const OUT_MAX_M: f64 = 3.0;
const PANEL_GAP_M: f64 = 6.0;      // no two panels facing the same way closer than this
const CLEAR_M: f64 = 0.25;          // the panel's edge from any pavement
const SKIP_PAST_M: f64 = 60.0;      // a sign on a bridge / in a tunnel moves past its end if that is this near

/// The panel (width, height) of a style and size class (0 town / single lane, 1 multi-lane, 2 expressway, 3 freeway).
fn dims(style: i64, size: usize) -> (f64, f64) {
    let s = size.min(3);
    match style {
        0 => [(0.61, 0.76), (0.76, 0.91), (0.91, 1.22), (1.22, 1.52)][s],      // MUTCD R2-1 (Table 2B-1)
        2 => [(0.6, 0.75), (0.6, 0.75), (0.9, 1.2), (0.9, 1.2)][s],            // Canada Rb-1
        _ => { let d = [0.6, 0.75, 0.9, 0.9][s]; (d, d) }                   // Vienna C14 (StVO / TSRGD sizes)
    }
}

/// m from the carriageway's edge to the panel's near edge (urban, rural, freeway): MUTCD 2A.19 (2 ft from a curb in
/// town; 6-12 ft out of town), StVO VwV (0.5 m in town, 1.5 m out of it); the photos (2026-10-28: Baltimore's arterials
/// at the curb, rural Maryland 2-3 m out, Oxfordshire and Stuttgart about a metre out).
fn lateral(style: i64, urban: bool, freeway: bool) -> f64 {
    if freeway { return if style == 0 { 2.0 } else { 1.5 }; }
    if urban { 0.5 } else if style == 0 { 2.5 } else { 1.2 }
}

/// The bottom of the panel above the road's edge (urban, rural): MUTCD 2A.18 (7 ft in town and on freeways, 5 ft out
/// of town); StVO VwV (2.0 m in town over a footway, 1.5 m out of it).
fn mount(style: i64, urban: bool, freeway: bool) -> f64 {
    if urban || freeway { if style == 0 { 2.13 } else { 2.0 } } else { 1.52 }
}

fn splitmix(mut z: u64) -> u64 {
    z = z.wrapping_add(0x9E3779B97F4A7C15);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
    z ^ (z >> 31)
}

struct Road {
    a: usize,
    b: usize,
    s: Vec<f64>,        // arc length at each vertex
    hw: f64,
    dir: i64,           // +1 / -1 one way along / against the geometry, 0 both ways
    rank: i64,
    urban: bool,
    link: bool,
}

struct Edge {
    road: usize,
    piece: usize,       // (road, piece) is the stretch between two junctions
    sa: f64,            // arc length of the piece's ends along the geometry
    sb: f64,
    fwd: bool,          // travel along the geometry
    from: i64,
    to: i64,
}

struct Net<'a> {
    pts: numpy::ndarray::ArrayView2<'a, f64>,
    ll: numpy::ndarray::ArrayView2<'a, f64>,
    roads: Vec<Road>,
    edges: Vec<Edge>,
    lim_at: numpy::ndarray::ArrayView1<'a, f64>,
    lim_val: numpy::ndarray::ArrayView1<'a, f64>,
    lim_off: numpy::ndarray::ArrayView1<'a, i64>,
}

impl<'a> Net<'a> {
    fn len(&self, e: usize) -> f64 { self.edges[e].sb - self.edges[e].sa }

    /// The geometry's arc length of station u (m from the edge's start in its travel direction).
    fn arc(&self, e: usize, u: f64) -> f64 {
        let ed = &self.edges[e];
        if ed.fwd { ed.sa + u } else { ed.sb - u }
    }

    /// (x, y) and (lon, lat) of road r at arc length s.
    fn at(&self, r: usize, s: f64) -> ([f64; 2], [f64; 2]) {
        let rd = &self.roads[r];
        let n = rd.b - rd.a;
        let s = s.clamp(0.0, rd.s[n - 1]);
        let mut k = match rd.s.binary_search_by(|v| v.partial_cmp(&s).unwrap()) { Ok(k) => k, Err(k) => k.max(1) - 1 };
        k = k.min(n - 2);
        let l = rd.s[k + 1] - rd.s[k];
        let t = if l > 1e-9 { (s - rd.s[k]) / l } else { 0.0 };
        let (i, j) = (rd.a + k, rd.a + k + 1);
        let lerp = |m: &numpy::ndarray::ArrayView2<f64>, c: usize| m[[i, c]] + t * (m[[j, c]] - m[[i, c]]);
        ([lerp(&self.pts, 0), lerp(&self.pts, 1)], [lerp(&self.ll, 0), lerp(&self.ll, 1)])
    }

    /// The travel direction of edge e over [u0, u1] (the chord, unit).
    fn chord(&self, e: usize, u0: f64, u1: f64) -> [f64; 2] {
        let r = self.edges[e].road;
        let (p, _) = self.at(r, self.arc(e, u0));
        let (q, _) = self.at(r, self.arc(e, u1));
        let (dx, dy) = (q[0] - p[0], q[1] - p[1]);
        let l = (dx * dx + dy * dy).sqrt().max(1e-9);
        [dx / l, dy / l]
    }

    fn start_dir(&self, e: usize) -> [f64; 2] { self.chord(e, 0.0, TANGENT_M.min(self.len(e))) }

    fn end_dir(&self, e: usize) -> [f64; 2] {
        let l = self.len(e);
        self.chord(e, (l - TANGENT_M).max(0.0), l)
    }

    fn tangent(&self, e: usize, u: f64) -> [f64; 2] {
        let l = self.len(e);
        let (a, b) = ((u - 3.0).max(0.0), (u + 3.0).min(l));
        self.chord(e, a, b.max(a + 1e-3))
    }

    /// The posted limit of road r at geometry fraction f, travelling along (fwd) or against it: the piece just ahead.
    fn limit(&self, r: usize, f: f64, fwd: bool) -> f64 {
        let (a, b) = (self.lim_off[r] as usize, self.lim_off[r + 1] as usize);
        if b <= a { return 0.0; }
        let probe = if fwd { f + 1e-6 } else { f - 1e-6 };
        let mut v = self.lim_val[a];
        for k in a..b {
            if self.lim_at[k] <= probe { v = self.lim_val[k]; } else { break; }
        }
        v
    }

    fn frac(&self, r: usize, s: f64) -> f64 {
        let rd = &self.roads[r];
        let total = rd.s[rd.b - rd.a - 1];
        if total > 1e-9 { s / total } else { 0.0 }
    }

    fn limit_at(&self, e: usize, u: f64) -> f64 {
        let ed = &self.edges[e];
        self.limit(ed.road, self.frac(ed.road, self.arc(e, u)), ed.fwd)
    }
}

/// The straight on continuation among cands (edges ending / starting at a junction of `arms` pieces): with two pieces
/// the other one at any angle, else the best aligned within STRAIGHT_COS and clearly ahead of the next best.
fn straight(cands: &[(usize, f64)], arms: usize) -> Option<usize> {
    let mut best: Option<(usize, f64)> = None;
    let mut second = -2.0f64;
    for &(e, c) in cands {
        match best {
            Some((_, bc)) if c <= bc => { second = second.max(c); }
            Some((_, bc)) => { second = second.max(bc); best = Some((e, c)); }
            None => best = Some((e, c)),
        }
    }
    let (e, c) = best?;
    if arms == 2 || (c >= STRAIGHT_COS && c - second >= STRAIGHT_MARGIN) { Some(e) } else { None }
}

fn dot(a: [f64; 2], b: [f64; 2]) -> f64 { a[0] * b[0] + a[1] * b[1] }

/// Whether a disc (c, r) is clear of every polygon (outside it and r from its edges).
fn clear(c: [f64; 2], r: f64, poly: &numpy::ndarray::ArrayView2<f64>, off: &numpy::ndarray::ArrayView1<i64>,
         boxes: &[[f64; 4]]) -> bool {
    for (q, bx) in boxes.iter().enumerate() {
        if c[0] + r < bx[0] || c[0] - r > bx[2] || c[1] + r < bx[1] || c[1] - r > bx[3] { continue; }
        let (a, b) = (off[q] as usize, off[q + 1] as usize);
        if b < a + 3 { continue; }
        let mut inside = false;
        for k in a..b {
            let j = if k + 1 < b { k + 1 } else { a };
            let (ax, ay, bx2, by) = (poly[[k, 0]], poly[[k, 1]], poly[[j, 0]], poly[[j, 1]]);
            let (dx, dy) = (bx2 - ax, by - ay);
            let l2 = (dx * dx + dy * dy).max(1e-12);
            let t = (((c[0] - ax) * dx + (c[1] - ay) * dy) / l2).clamp(0.0, 1.0);
            let (px, py) = (ax + t * dx - c[0], ay + t * dy - c[1]);
            if px * px + py * py < r * r { return false; }
            if (ay > c[1]) != (by > c[1]) && c[0] < ax + (c[1] - ay) * dx / (by - ay) { inside = !inside; }
        }
        if inside { return false; }
    }
    true
}

struct Cand {
    e: usize,
    u: f64,
    kind: u8,
    key: u64,
    xy: [f64; 2],
    dir: [f64; 2],
    value: f64,
    gap: f64,
}

/// The speed-limit signs of a box (x0, y0, x1, y1). Roads: vertices pts (n, 2) and their lon/lat ll (n, 2) with
/// offsets off (r + 1); meta (r, 5) [half width, direction (+1 / -1 one way along / against the geometry, 0 both),
/// rank (motorway 7 ... residential 1, 0 none), urban, link]; their junctions (connector ids con_id at geometry
/// fractions con_at, offsets con_off (r + 1)); their posted limits (lim_val from fraction lim_at on, offsets lim_off
/// (r + 1); 0: none); their bridge / tunnel ranges (skip (k, 2) fractions, offsets skip_off (r + 1)). Pavements:
/// outlines poly (m, 2), offsets poly_off. style: 0 MUTCD, 1 Vienna, 2 Canada; repeat: the reminders' spacing
/// [town, country] (m). Returns (k, 9) [x, y, travel x, travel y, value, size class, road x, road y, urban].
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn speed_sign_place<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, ll: PyReadonlyArray2<'py, f64>,
                             off: PyReadonlyArray1<'py, i64>, meta: PyReadonlyArray2<'py, f64>,
                             con_at: PyReadonlyArray1<'py, f64>, con_id: PyReadonlyArray1<'py, i64>,
                             con_off: PyReadonlyArray1<'py, i64>, lim_at: PyReadonlyArray1<'py, f64>,
                             lim_val: PyReadonlyArray1<'py, f64>, lim_off: PyReadonlyArray1<'py, i64>,
                             skip: PyReadonlyArray2<'py, f64>, skip_off: PyReadonlyArray1<'py, i64>,
                             poly: PyReadonlyArray2<'py, f64>, poly_off: PyReadonlyArray1<'py, i64>,
                             bx: [f64; 4], style: i64, left_hand: bool, repeat: [f64; 2])
                             -> Bound<'py, PyArray2<f64>> {
    let (pts, ll, off, meta) = (pts.as_array(), ll.as_array(), off.as_array(), meta.as_array());
    let (con_at, con_id, con_off) = (con_at.as_array(), con_id.as_array(), con_off.as_array());
    let (skip, skip_off, poly, poly_off) = (skip.as_array(), skip_off.as_array(), poly.as_array(), poly_off.as_array());
    let mut net = Net { pts, ll, roads: Vec::new(), edges: Vec::new(), lim_at: lim_at.as_array(),
                        lim_val: lim_val.as_array(), lim_off: lim_off.as_array() };
    let nr = off.len().saturating_sub(1);
    // the roads and their pieces between junctions, both ways where traffic goes both ways
    let mut ends: HashMap<i64, usize> = HashMap::new();     // junction -> pieces meeting there
    for r in 0..nr {
        let (a, b) = (off[r] as usize, off[r + 1] as usize);
        let mut s = vec![0.0f64; b.saturating_sub(a)];
        for k in a + 1..b {
            s[k - a] = s[k - a - 1] + ((pts[[k, 0]] - pts[[k - 1, 0]]).powi(2) + (pts[[k, 1]] - pts[[k - 1, 1]]).powi(2)).sqrt();
        }
        let road = Road { a, b, s, hw: meta[[r, 0]], dir: meta[[r, 1]] as i64, rank: meta[[r, 2]] as i64,
                          urban: meta[[r, 3]] != 0.0, link: meta[[r, 4]] != 0.0 };
        let total = if b >= a + 2 { road.s[b - a - 1] } else { 0.0 };
        net.roads.push(road);
        if total < 1.0 { continue; }
        let mut nodes: Vec<(f64, i64)> = Vec::new();
        for k in con_off[r] as usize..con_off[r + 1] as usize {
            let f = con_at[k];
            let f = if f < 1e-3 { 0.0 } else if f > 1.0 - 1e-3 { 1.0 } else { f };
            nodes.push((f, con_id[k]));
        }
        nodes.sort_by(|p, q| p.partial_cmp(q).unwrap());
        nodes.dedup_by(|p, q| (p.0 - q.0).abs() < 1e-9);
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
    let same_piece = |a: &Edge, b: &Edge| a.road == b.road && a.piece == b.piece;
    let none: Vec<usize> = Vec::new();
    // the straight on continuation into edge e (pred) and out of it (succ)
    let pred = |net: &Net, e: usize| -> Option<usize> {
        let ed = &net.edges[e];
        let arms = *ends.get(&ed.from).unwrap_or(&0);
        if arms < 2 { return None; }
        let d = net.start_dir(e);
        let c: Vec<(usize, f64)> = incoming.get(&ed.from).unwrap_or(&none).iter()
            .filter(|&&i| !same_piece(&net.edges[i], ed)).map(|&i| (i, dot(net.end_dir(i), d))).collect();
        straight(&c, arms)
    };
    let succ = |net: &Net, e: usize| -> Option<usize> {
        let ed = &net.edges[e];
        let arms = *ends.get(&ed.to).unwrap_or(&0);
        if arms < 2 { return None; }
        let d = net.end_dir(e);
        let c: Vec<(usize, f64)> = outgoing.get(&ed.to).unwrap_or(&none).iter()
            .filter(|&&i| !same_piece(&net.edges[i], ed)).map(|&i| (i, dot(net.start_dir(i), d))).collect();
        straight(&c, arms)
    };

    // candidates
    let mut cands: Vec<(usize, f64, u8)> = Vec::new();
    for e in 0..net.edges.len() {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        if rd.rank < 1 || rd.link { continue; }
        let len = net.len(e);
        let v = net.limit_at(e, 0.0);
        if v <= 0.0 { continue; }
        let arms = *ends.get(&ed.from).unwrap_or(&0);
        let p = pred(&net, e);
        let mut changed = false;
        if let Some(p) = p {
            let pv = net.limit_at(p, (net.len(p) - 0.5).max(0.0));
            changed = (pv - v).abs() > 0.5;
        }
        let into = if arms <= 2 { CHANGE_IN_M } else if rd.urban { JUNCTION_IN_URBAN_M } else { JUNCTION_IN_RURAL_M };
        // the roads meeting here besides this one and its straight on continuation
        let mut major = false;
        let mut ramp_in = false;
        if arms >= 3 {
            let pr = p.map(|p| net.edges[p].road);
            for &i in incoming.get(&ed.from).unwrap_or(&none).iter().chain(outgoing.get(&ed.from).unwrap_or(&none)) {
                let o = &net.edges[i];
                if o.road == ed.road || Some(o.road) == pr { continue; }
                let or = &net.roads[o.road];
                if !or.link && or.rank >= SECONDARY { major = true; }
                if or.link && o.to == ed.from { ramp_in = true; }
            }
            // a turn-in (no straight on continuation) onto a road whose limit the driver cannot know
            if p.is_none() && rd.rank >= TERTIARY { changed = changed || major; }
        }
        if changed {
            cands.push((e, into.min(len * 0.5), CHANGE));
        } else if major && rd.rank >= TERTIARY {
            cands.push((e, into.min(len * 0.5), JUNCTION));
        }
        if ramp_in && rd.rank >= TRUNK && rd.dir != 0 {
            // MERGE_M on, along the straight on continuation
            let (mut cur, mut left, mut hops) = (e, MERGE_M, 0);
            loop {
                let l = net.len(cur);
                if left <= l - 1.0 { cands.push((cur, left, MERGE)); break; }
                left -= l;
                hops += 1;
                match succ(&net, cur) { Some(n) if hops < 12 => cur = n, _ => break }
            }
        }
        // a change inside the piece
        let (la, lb) = (net.lim_off[ed.road] as usize, net.lim_off[ed.road + 1] as usize);
        let (fa, fb) = (net.frac(ed.road, ed.sa), net.frac(ed.road, ed.sb));
        for k in la..lb {
            let f = net.lim_at[k];
            if f <= fa + 1e-6 || f >= fb - 1e-6 { continue; }
            let before = net.limit(ed.road, f, !ed.fwd);
            let after = net.limit(ed.road, f, ed.fwd);
            if (before - after).abs() > 0.5 && after > 0.0 {
                let u = (if ed.fwd { f - fa } else { fb - f }) * (rd.s[rd.b - rd.a - 1]) + CHANGE_IN_M;
                if u < len - 1.0 { cands.push((e, u, CHANGE)); }
            }
        }
        // reminders where the road crosses the lat/lon grid
        if rd.rank >= TERTIARY {
            let spacing = if rd.urban { repeat[0] } else { repeat[1] };
            if spacing > 0.0 {
                let dlat = spacing / 111_320.0;
                let (ka, kb) = (rd.a, rd.b);
                let base = ed.sa;
                for k in ka..kb - 1 {
                    let (sk, sk1) = (rd.s[k - ka], rd.s[k + 1 - ka]);
                    if sk1 <= ed.sa || sk >= ed.sb { continue; }
                    let (p, q) = ([ll[[k, 0]], ll[[k, 1]]], [ll[[k + 1, 0]], ll[[k + 1, 1]]]);
                    let band = (((p[1] + q[1]) * 0.5).floor() + 0.5).to_radians().cos().max(0.05);
                    let dlon = dlat / band;
                    let mut ts: Vec<f64> = Vec::new();
                    for (c, step) in [(0usize, dlon), (1usize, dlat)] {
                        let (a, b) = (p[c] / step, q[c] / step);
                        let (lo, hi) = (a.min(b).floor() as i64 + 1, a.max(b).floor() as i64);
                        for g in lo..=hi {
                            if (b - a).abs() > 1e-12 { ts.push((g as f64 - a) / (b - a)); }
                        }
                    }
                    for t in ts {
                        let s = sk + t * (sk1 - sk);
                        if s <= ed.sa || s >= ed.sb { continue; }
                        let u = if ed.fwd { s - base } else { ed.sb - s };
                        cands.push((e, u, REPEAT));
                    }
                }
            }
        }
    }

    // keep the first of each cluster (same value, same way), in a fixed order
    let mut full: Vec<Cand> = cands.into_iter().filter_map(|(e, u, kind)| {
        let ed = &net.edges[e];
        let rd = &net.roads[ed.road];
        let u = u.clamp(0.0, net.len(e));
        let value = net.limit_at(e, u);
        if value <= 0.0 { return None; }
        let (xy, lonlat) = net.at(ed.road, net.arc(e, u));
        let key = splitmix(splitmix(((lonlat[0] * 1e5).round() as i64) as u64)
            ^ ((lonlat[1] * 1e5).round() as i64) as u64) ^ (kind as u64);
        let gap = match kind {
            CHANGE => GAP_CHANGE_M,
            MERGE => GAP_MERGE_M,
            JUNCTION => if rd.urban { GAP_JUNCTION_URBAN_M } else { GAP_JUNCTION_RURAL_M },
            _ => GAP_REPEAT_SHARE * if rd.urban { repeat[0] } else { repeat[1] },
        };
        Some(Cand { e, u, kind, key, xy, dir: net.tangent(e, u), value, gap })
    }).collect();
    full.sort_by(|a, b| (a.kind, a.key).cmp(&(b.kind, b.key)));
    let cell = 250.0;
    let mut grid: HashMap<(i64, i64), Vec<usize>> = HashMap::new();
    let mut kept: Vec<usize> = Vec::new();
    for (i, c) in full.iter().enumerate() {
        let (gx, gy) = ((c.xy[0] / cell).floor() as i64, (c.xy[1] / cell).floor() as i64);
        let reach = (c.gap / cell).ceil() as i64;
        let mut near = false;
        'scan: for dx in -reach..=reach {
            for dy in -reach..=reach {
                if let Some(list) = grid.get(&(gx + dx, gy + dy)) {
                    for &j in list {
                        let o = &full[j];
                        let (ddx, ddy) = (o.xy[0] - c.xy[0], o.xy[1] - c.xy[1]);
                        let d2 = ddx * ddx + ddy * ddy;
                        // (along the road: not a parallel street a block away)
                        let across = (ddx * c.dir[1] - ddy * c.dir[0]).abs();
                        if (o.value - c.value).abs() < 0.5 && dot(o.dir, c.dir) > SAME_WAY_COS && d2 < c.gap * c.gap
                            && across < ACROSS_M + ACROSS_SHARE * d2.sqrt() {
                            near = true;
                            break 'scan;
                        }
                    }
                }
            }
        }
        if !near {
            grid.entry((gx, gy)).or_default().push(i);
            kept.push(i);
        }
    }

    // the spots of the kept ones in the box: beside the road, clear of every pavement, off bridges and tunnels
    let boxes: Vec<[f64; 4]> = (0..poly_off.len().saturating_sub(1)).map(|q| {
        let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for k in poly_off[q] as usize..poly_off[q + 1] as usize {
            b[0] = b[0].min(poly[[k, 0]]); b[1] = b[1].min(poly[[k, 1]]);
            b[2] = b[2].max(poly[[k, 0]]); b[3] = b[3].max(poly[[k, 1]]);
        }
        b
    }).collect();
    let margin = 80.0;
    let mut out: Vec<f64> = Vec::new();
    let mut placed: Vec<([f64; 2], [f64; 2])> = Vec::new();
    for &i in &kept {
        let c = &full[i];
        if c.xy[0] < bx[0] - margin || c.xy[0] >= bx[2] + margin || c.xy[1] < bx[1] - margin || c.xy[1] >= bx[3] + margin {
            continue;
        }
        let ed = &net.edges[c.e];
        let rd = &net.roads[ed.road];
        let len = net.len(c.e);
        let total = rd.s[rd.b - rd.a - 1];
        // off a bridge / out of a tunnel
        let mut u = c.u;
        let (sa, sb) = (skip_off[ed.road] as usize, skip_off[ed.road + 1] as usize);
        let mut moved = 0.0;
        for _ in 0..4 {
            let f = net.frac(ed.road, net.arc(c.e, u));
            let mut hit = None;
            for k in sa..sb {
                if skip[[k, 0]] - 1e-6 <= f && f <= skip[[k, 1]] + 1e-6 { hit = Some(k); break; }
            }
            match hit {
                None => break,
                Some(k) => {
                    let exit = if ed.fwd { skip[[k, 1]] } else { skip[[k, 0]] };
                    let du = (exit - f).abs() * total + 8.0;
                    u += du;
                    moved += du;
                }
            }
        }
        if moved > SKIP_PAST_M || u > len - 1.0 { continue; }
        let freeway = rd.rank >= MOTORWAY || (rd.rank >= TRUNK && rd.dir != 0 && !rd.urban);
        // (multi-lane: the arterials, and a wide tertiary; a town street's width is mostly its parking lanes)
        let size = if freeway && rd.rank >= MOTORWAY { 3 } else if freeway { 2 }
                   else if rd.rank >= PRIMARY || (rd.rank >= TERTIARY && rd.hw * 2.0 >= 13.0) { 1 } else { 0 };
        let size = if !rd.urban && size == 0 && style != 0 { 1 } else { size };     // (out of town: the 750 mm disc)
        let (w, _) = dims(style, size);
        let side = if left_hand { -1.0 } else { 1.0 };
        let base = rd.hw + lateral(style, rd.urban, freeway) + w / 2.0;
        let mut found: Option<([f64; 2], [f64; 2], [f64; 2])> = None;
        'spot: for du in ALONG_STEPS_M {
            let uu = u + du;
            if uu > len - 1.0 { break; }
            let (road_xy, _) = net.at(ed.road, net.arc(c.e, uu));
            let t = net.tangent(c.e, uu);
            let right = [t[1] * side, -t[0] * side];
            let mut extra = 0.0;
            while extra <= OUT_MAX_M + 1e-9 {
                let q = [road_xy[0] + right[0] * (base + extra), road_xy[1] + right[1] * (base + extra)];
                if clear(q, w / 2.0 + CLEAR_M, &poly, &poly_off, &boxes) {
                    found = Some((q, t, road_xy));
                    break 'spot;
                }
                extra += OUT_STEP_M;
            }
        }
        let Some((q, t, road_xy)) = found else { continue };
        // one panel a spot: overlapping map lines (two pieces of one road) put a second one there
        if placed.iter().any(|(p, d)| dot(*d, t) > SAME_WAY_COS
                             && (p[0] - q[0]).powi(2) + (p[1] - q[1]).powi(2) < PANEL_GAP_M * PANEL_GAP_M) {
            continue;
        }
        placed.push((q, t));
        if !(q[0] >= bx[0] && q[0] < bx[2] && q[1] >= bx[1] && q[1] < bx[3]) { continue; }
        out.extend_from_slice(&[q[0], q[1], t[0], t[1], c.value, size as f64, road_xy[0], road_xy[1],
                                if rd.urban { 1.0 } else { 0.0 }]);
    }
    let n = out.len() / 9;
    numpy::ndarray::Array2::from_shape_vec((n, 9), out).unwrap().into_pyarray(py)
}

// ---- the mesh ----

const M_WHITE: i64 = 0;
const M_RED: i64 = 1;
const M_METAL: i64 = 2;
const M_LETTERS: i64 = 3;
const M_BLACK: i64 = 4;
const TEXT_DEPTH: f64 = 0.02;       // the lettering this far in front of the face (no flicker at distance)

pub(crate) struct Mesh {
    pub(crate) p: Vec<[f64; 3]>,
    pub(crate) t: Vec<[i64; 3]>,
    pub(crate) uv: Vec<[f64; 2]>,
    pub(crate) mi: Vec<i64>,
    pub(crate) centre: [f64; 3],
    pub(crate) face: [f64; 2],
    pub(crate) right: [f64; 2],
}

impl Mesh {
    fn world(&self, q: [f64; 3]) -> [f64; 3] {
        [self.centre[0] + q[0] * self.face[0] + q[1] * self.right[0],
         self.centre[1] + q[0] * self.face[1] + q[1] * self.right[1],
         self.centre[2] + q[2]]
    }

    pub(crate) fn quad(&mut self, q: [[f64; 3]; 4], m: i64, uv: [[f64; 2]; 4]) {
        let i = self.p.len() as i64;
        for (k, v) in q.iter().enumerate() {
            let w = self.world(*v);
            self.p.push(w);
            self.uv.push(uv[k]);
        }
        self.t.push([i, i + 1, i + 2]);
        self.t.push([i, i + 2, i + 3]);
        self.mi.push(m);
        self.mi.push(m);
    }

    fn tri(&mut self, q: [[f64; 3]; 3], m: i64) {
        let i = self.p.len() as i64;
        for v in q.iter() {
            let w = self.world(*v);
            self.p.push(w);
            self.uv.push([0.0, 0.0]);
        }
        self.t.push([i, i + 1, i + 2]);
        self.mi.push(m);
    }

    /// A box (x: depth toward the driver, y: to the driver's right, z: up), as exitsigns.Mesh.box.
    pub(crate) fn cuboid(&mut self, lo: [f64; 3], hi: [f64; 3], m: i64) {
        let ([x, y, z], [xx, yy, zz]) = (lo, hi);
        let uv = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]];
        for q in [[[x, y, z], [x, y, zz], [x, yy, zz], [x, yy, z]], [[xx, y, z], [xx, yy, z], [xx, yy, zz], [xx, y, zz]],
                  [[x, y, z], [xx, y, z], [xx, y, zz], [x, y, zz]], [[x, yy, z], [x, yy, zz], [xx, yy, zz], [xx, yy, z]],
                  [[x, y, z], [x, yy, z], [xx, yy, z], [xx, y, z]], [[x, y, zz], [xx, y, zz], [xx, yy, zz], [x, yy, zz]]] {
            self.quad(q, m, uv);
        }
    }

    /// A round pole (octagonal) of radius r at depth x0 from z0 to z1.
    fn pole(&mut self, x0: f64, r: f64, z0: f64, z1: f64, m: i64) {
        let uv = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]];
        for k in 0..8 {
            let (a, b) = (k as f64 * std::f64::consts::TAU / 8.0, (k + 1) as f64 * std::f64::consts::TAU / 8.0);
            let (pa, pb) = ([x0 + r * a.cos(), r * a.sin()], [x0 + r * b.cos(), r * b.sin()]);
            self.quad([[pa[0], pa[1], z0], [pb[0], pb[1], z0], [pb[0], pb[1], z1], [pa[0], pa[1], z1]], m, uv);
        }
    }

    /// The ring between two outlines of as many points (counter-clockwise as the driver sees them) at depth x.
    fn ring(&mut self, outer: &[[f64; 2]], inner: &[[f64; 2]], x: f64, m: i64) {
        let n = outer.len();
        let uv = [[0.0, 0.0]; 4];
        for i in 0..n {
            let j = (i + 1) % n;
            self.quad([[x, outer[i][0], outer[i][1]], [x, outer[j][0], outer[j][1]],
                       [x, inner[j][0], inner[j][1]], [x, inner[i][0], inner[i][1]]], m, uv);
        }
    }

    /// A convex outline filled (facing the driver, or away with back).
    fn fill(&mut self, poly: &[[f64; 2]], x: f64, m: i64, back: bool) {
        for i in 1..poly.len() - 1 {
            let (a, b, c) = (poly[0], poly[i], poly[i + 1]);
            if back {
                self.tri([[x, a[0], a[1]], [x, c[0], c[1]], [x, b[0], b[1]]], m);
            } else {
                self.tri([[x, a[0], a[1]], [x, b[0], b[1]], [x, c[0], c[1]]], m);
            }
        }
    }
}

/// A rounded rectangle's outline (counter-clockwise as the driver sees it): width w, bottom z0, height h, radius r.
pub(crate) fn rounded(w: f64, z0: f64, h: f64, r: f64) -> Vec<[f64; 2]> {
    let mut out = Vec::with_capacity(28);
    for (cy, cz, start) in [(w / 2.0 - r, z0 + r, -90.0f64), (w / 2.0 - r, z0 + h - r, 0.0),
                            (-w / 2.0 + r, z0 + h - r, 90.0), (-w / 2.0 + r, z0 + r, 180.0)] {
        for k in 0..7 {
            let a = (start + 15.0 * k as f64).to_radians();
            out.push([cy + r * a.cos(), cz + r * a.sin()]);
        }
    }
    out
}

fn circle(r: f64, zc: f64) -> Vec<[f64; 2]> {
    (0..48).map(|k| {
        let a = k as f64 * std::f64::consts::TAU / 48.0;
        [r * a.cos(), zc + r * a.sin()]
    }).collect()
}

struct Font {
    glyphs: HashMap<u32, [f64; 9]>,     // advance, bbox l t r b, uv u0 v0 u1 v1
    cap: f64,
}

impl Font {
    fn width(&self, s: &str) -> f64 {
        s.chars().filter_map(|c| self.glyphs.get(&(c as u32))).map(|g| g[0]).sum()
    }

    /// Lettering centred on y with its baseline at z: cap height h, at most max_w wide, condensed horizontally by
    /// cond (1: the font's own width).
    fn text(&self, mesh: &mut Mesh, s: &str, y: f64, z: f64, h: f64, max_w: f64, cond: f64) {
        let scale = h / self.cap;
        let raw = self.width(s) * scale;
        let sx = scale * cond.min(max_w / raw.max(1e-6));
        let mut cursor = y - self.width(s) * sx / 2.0;
        for c in s.chars() {
            let Some(g) = self.glyphs.get(&(c as u32)) else { continue };
            let [adv, l, t, r, b, u0, v0, u1, v1] = *g;
            if r > l && b > t {
                mesh.quad([[TEXT_DEPTH, cursor + l * sx, z - b * scale], [TEXT_DEPTH, cursor + r * sx, z - b * scale],
                           [TEXT_DEPTH, cursor + r * sx, z - t * scale], [TEXT_DEPTH, cursor + l * sx, z - t * scale]],
                          M_LETTERS, [[u0, v0], [u1, v0], [u1, v1], [u0, v1]]);
            }
            cursor += adv * sx;
        }
    }
}

/// The meshes of signs (k, 9) [x, y, ground z, road z, travel x, travel y, value, size class, urban] in one style
/// (0 MUTCD R2-1, 1 Vienna C14, 2 Canada Rb-1), relative to origin. glyph_cp (g,) code points with glyph (g, 9)
/// [advance, left, top, right, bottom, u0, v0, u1, v1] in font units of cap height cap (exitsigns.lettering()).
/// Returns (vertices (n, 3), triangles (m, 3), uv (n, 2), material per triangle (m,)): 0 white, 1 red, 2 metal,
/// 3 lettering, 4 black.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn speed_sign_mesh<'py>(py: Python<'py>, signs: PyReadonlyArray2<'py, f64>, style: i64,
                            glyph_cp: PyReadonlyArray1<'py, i64>, glyph: PyReadonlyArray2<'py, f64>, cap: f64,
                            origin: [f64; 3])
                            -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>,
                                Bound<'py, PyArray1<i64>>) {
    let (signs, cp, g) = (signs.as_array(), glyph_cp.as_array(), glyph.as_array());
    let mut glyphs = HashMap::new();
    for k in 0..cp.len() {
        let mut row = [0.0; 9];
        for c in 0..9 { row[c] = g[[k, c]]; }
        glyphs.insert(cp[k] as u32, row);
    }
    let font = Font { glyphs, cap };
    let mut m = Mesh { p: Vec::new(), t: Vec::new(), uv: Vec::new(), mi: Vec::new(), centre: [0.0; 3],
                       face: [0.0; 2], right: [0.0; 2] };
    for k in 0..signs.nrows() {
        let (x, y, zg, zr, tx, ty) = (signs[[k, 0]], signs[[k, 1]], signs[[k, 2]], signs[[k, 3]], signs[[k, 4]],
                                      signs[[k, 5]]);
        let value = signs[[k, 6]].round() as i64;
        let size = signs[[k, 7]].max(0.0) as usize;
        let urban = signs[[k, 8]] != 0.0;
        if value <= 0 || !(x.is_finite() && y.is_finite() && zg.is_finite() && zr.is_finite()) { continue; }
        let tl = (tx * tx + ty * ty).sqrt().max(1e-9);
        let (tx, ty) = (tx / tl, ty / tl);
        m.centre = [x - origin[0], y - origin[1], zg - origin[2]];
        m.face = [-tx, -ty];
        m.right = [ty, -tx];
        let freeway = size >= 2;
        let (w, h) = dims(style, size);
        let bottom = (zr + mount(style, urban, freeway)).max(zg + 1.2) - zg;
        let top = bottom + h;
        let digits = value.to_string();
        match style {
            1 => {
                // Vienna C14: a white rim, the red ring, the white disc, the number; a round pole behind
                let (r, zc) = (w / 2.0, bottom + h / 2.0);
                m.pole(-0.05, 0.03, -0.3, top - 0.05, M_METAL);
                let rim = circle(r, zc);
                let red = circle(r * 0.97, zc);
                let inner = circle(r * 0.80, zc);
                m.ring(&rim, &red, 0.0, M_WHITE);
                m.ring(&red, &inner, 0.0, M_RED);
                m.fill(&inner, 0.0, M_WHITE, false);
                m.fill(&rim, -0.004, M_METAL, true);
                font.text(&mut m, &digits, 0.0, zc - 0.19 * w, 0.38 * w, 0.62 * w, 0.7);
            }
            _ => {
                // a white rectangle with a black border (MUTCD R2-1 / Rb-1): margin 3/8 in and border 5/8 in at 30 in
                let posts: &[f64] = if size >= 2 { &[-0.3, 0.3] } else { &[0.0] };
                for &o in posts {
                    m.cuboid([-0.07, o * w - 0.032, -0.3], [-0.006, o * w + 0.032, top - 0.08], M_METAL);
                }
                let r = 0.05 * h;
                let (mg, bd) = (0.0125 * h, 0.021 * h);
                let outer = rounded(w, bottom, h, r);
                let b0 = rounded(w - 2.0 * mg, bottom + mg, h - 2.0 * mg, (r - mg).max(0.005));
                let b1 = rounded(w - 2.0 * (mg + bd), bottom + mg + bd, h - 2.0 * (mg + bd), (r - mg - bd).max(0.004));
                m.ring(&outer, &b0, 0.0, M_WHITE);
                m.ring(&b0, &b1, 0.0, M_BLACK);
                m.fill(&b1, 0.0, M_WHITE, false);
                m.fill(&outer, -0.004, M_METAL, true);
                let inner_w = w - 2.0 * (mg + bd) - 0.06 * w;
                if style == 2 {
                    font.text(&mut m, "MAXIMUM", 0.0, bottom + 0.80 * h, 0.10 * h, inner_w, 0.85);
                    font.text(&mut m, &digits, 0.0, bottom + 0.30 * h, 0.36 * h, inner_w, 0.9);
                    font.text(&mut m, "km/h", 0.0, bottom + 0.09 * h, 0.10 * h, inner_w, 1.0);
                } else {
                    font.text(&mut m, "SPEED", 0.0, bottom + 0.80 * h, 0.125 * h, inner_w, 0.8);
                    font.text(&mut m, "LIMIT", 0.0, bottom + 0.62 * h, 0.125 * h, inner_w, 0.8);
                    font.text(&mut m, &digits, 0.0, bottom + 0.12 * h, 0.36 * h, inner_w, 0.8);
                }
            }
        }
    }
    let (n, t) = (m.p.len(), m.t.len());
    let p = numpy::ndarray::Array2::from_shape_vec((n, 3), m.p.into_iter().flatten().collect()).unwrap();
    let tri = numpy::ndarray::Array2::from_shape_vec((t, 3), m.t.into_iter().flatten().collect()).unwrap();
    let uv = numpy::ndarray::Array2::from_shape_vec((n, 2), m.uv.into_iter().flatten().collect()).unwrap();
    (p.into_pyarray(py), tri.into_pyarray(py), uv.into_pyarray(py), m.mi.into_pyarray(py))
}
