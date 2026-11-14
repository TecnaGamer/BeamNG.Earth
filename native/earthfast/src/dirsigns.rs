//! Direction signs before junctions (the user, 2026-11-02: advance direction boards, a blue or green rectangle with
//! the road numbers, the places ahead in the local language and in English, and their distances). The glue is
//! server/dirsigns.py; signstream.py sends the meshes as sign tiles (render-only, under the sign switch).
//!
//! Where (dirsign_place): at every junction of major roads (>= 3 arms: primary or better, or a link of one), a board on
//! each approach (an arm traffic can arrive by) ADVANCE_M before the junction, on the driving side, clear of every
//! pavement, off bridges and tunnels. The sources of the distances: UK TSRGD / StVO RWB 2000 / the Polish E-1a
//! (1500 m before an exit) / Czech and Ukrainian practice vary; the values are the style table's (server data), the
//! kernel takes them as parameters.
//!
//! What it says: per arm leaving the junction (straight, left, right; at most MAX_ROWS), the road numbers of the
//! road it follows (the route memberships the server joined to the segments) and up to two places ahead: a near one
//! (the closest significant place in a cone round the arm's bearing) and a far one (the biggest place in the cone).
//! The bearing is that of the arm followed through the next junctions to about WALK_M (a ramp's own bearing says
//! nothing; where it leads does). The places are the server's compact snapshot (Overture localities); the distance
//! is the straight one (the server scales it to a road's).
//!
//! Everything is a function of the data (sorted, hashed ties): the same board from every tile and anchor.
use crate::dm::Dm;

use std::collections::{HashMap, HashSet};

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

const PRIMARY: i64 = 5;
const SECONDARY: i64 = 4;
const TRUNK: i64 = 6;
const MOTORWAY: i64 = 7;

const MAX_ROWS: usize = 4;          // arms on a board (StVO RWB: at most 4 a direction)
const MAX_ROUTES: usize = 3;
const WALK_M: f64 = 800.0;          // m an arm is followed to find where it leads
const WALK_HOPS: usize = 5;
const WALK_COS: f64 = 0.5;          // a continuation within ~60 degrees of the way we came
const TANGENT_M: f64 = 10.0;
const MIN_PIECE_M: f64 = 40.0;      // an approach shorter than this gets no board
const STRAIGHT_DEG: f64 = 30.0;     // an arm within this of the approach's heading is "straight"
const BEHIND_COS: f64 = -0.2;       // a place this far behind the approach is no destination
const ALONG_STEPS_M: [f64; 7] = [0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0];
const OUT_STEP_M: f64 = 0.5;
const OUT_MAX_M: f64 = 3.0;
const CLEAR_M: f64 = 0.3;           // a post's centre from any pavement (plus its radius)
const POST_R: f64 = 0.35;
const BOARD_GAP_M: f64 = 60.0;      // no two boards for one direction closer than this
const TOWN_PANEL_W: f64 = 1.6;      // m: a town board is small (the panel width the spot search reserves; the face is its own)
const URBAN_GAP_M: f64 = 600.0;     // ... in a town: a board only where one is wanted (CITY BOARDS below), and far apart
const SKIP_PAST_M: f64 = 60.0;
const SAME_WAY_COS: f64 = 0.7;
const M_PER_DEG: f64 = 111_320.0;

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

/// One end of a piece of road at a junction, looking away from it.
#[derive(Clone, Copy)]
struct Arm {
    road: usize,
    piece: usize,
    sa: f64,            // arc lengths of the piece's ends
    sb: f64,
    fwd: bool,          // leaves the junction along the geometry (from sa) / against it (from sb)
    node: i64,
}

impl Arm {
    fn len(&self) -> f64 { self.sb - self.sa }
    /// Arc length of the arm's junction end.
    fn s0(&self) -> f64 { if self.fwd { self.sa } else { self.sb } }
    /// Arc length at distance u from the junction along the arm.
    fn s_at(&self, u: f64) -> f64 { if self.fwd { self.sa + u } else { self.sb - u } }
}

struct Net<'a> {
    pts: numpy::ndarray::ArrayView2<'a, f64>,
    ll: numpy::ndarray::ArrayView2<'a, f64>,
    roads: Vec<Road>,
}

impl<'a> Net<'a> {
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

    fn total(&self, r: usize) -> f64 { let rd = &self.roads[r]; rd.s[rd.b - rd.a - 1] }

    fn frac(&self, r: usize, s: f64) -> f64 {
        let t = self.total(r);
        if t > 1e-9 { s / t } else { 0.0 }
    }

    /// The unit chord of arm `m` between distances u0 and u1 from its junction, in the direction leaving it.
    fn chord(&self, m: &Arm, u0: f64, u1: f64) -> [f64; 2] {
        let (p, _) = self.at(m.road, m.s_at(u0));
        let (q, _) = self.at(m.road, m.s_at(u1));
        unit([q[0] - p[0], q[1] - p[1]])
    }

    /// The direction leaving the junction along the arm.
    fn leave_dir(&self, m: &Arm) -> [f64; 2] { self.chord(m, 0.0, TANGENT_M.min(m.len())) }

    /// The direction travelling along the arm into its junction (what a board's approach looks like).
    fn arrive_dir(&self, m: &Arm, u: f64) -> [f64; 2] {
        let (a, b) = ((u - 3.0).max(0.0), (u + 3.0).min(m.len()));
        let d = self.chord(m, a, b.max(a + 1e-3));
        [-d[0], -d[1]]
    }
}

fn unit(v: [f64; 2]) -> [f64; 2] {
    let l = (v[0] * v[0] + v[1] * v[1]).sqrt().max(1e-9);
    [v[0] / l, v[1] / l]
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

/// A place the server's snapshot has: lon, lat, population, class (0 village, 1 town, 2 city).
struct Places<'a> {
    v: numpy::ndarray::ArrayView2<'a, f64>,
}

struct Pick {
    idx: usize,
    d: f64,
    off: f64,
}

impl<'a> Places<'a> {
    /// The places a bearing (unit vector, east/north) from (lon, lat) leads to: within `cone` degrees of it, d metres
    /// away (min_d ..= max_d), not behind `heading`, within a cross-track of max(2.5 km, a quarter of the distance).
    fn ahead(&self, lon: f64, lat: f64, bearing: [f64; 2], heading: [f64; 2], cone: f64, min_d: f64, max_d: f64,
             used: &HashSet<usize>) -> Vec<Pick> {
        let c = lat.to_radians().dcos();
        let mut out = Vec::new();
        for i in 0..self.v.nrows() {
            if used.contains(&i) { continue; }
            let (dx, dy) = ((self.v[[i, 0]] - lon) * M_PER_DEG * c, (self.v[[i, 1]] - lat) * M_PER_DEG);
            let d = (dx * dx + dy * dy).sqrt();
            if d < min_d || d > max_d { continue; }
            let u = [dx / d, dy / d];
            if dot(u, heading) < BEHIND_COS { continue; }
            let cos = dot(u, bearing).clamp(-1.0, 1.0);
            let off = cos.dacos().to_degrees();
            if off > cone { continue; }
            if d * (1.0 - cos * cos).sqrt() > 2500.0f64.max(0.25 * d) { continue; }
            out.push(Pick { idx: i, d, off });
        }
        out
    }
}

/// The compass-free angle of a unit vector (degrees counter-clockwise from east), 0..360.
fn angle(v: [f64; 2]) -> f64 {
    let a = v[1].datan2(v[0]).to_degrees();
    if a < 0.0 { a + 360.0 } else { a }
}

fn signed_diff(a: f64, b: f64) -> f64 { ((a - b + 540.0) % 360.0) - 180.0 }

/// The direction boards of a box (x0, y0, x1, y1).
///
/// Roads: vertices pts (n, 2) and their lon/lat ll (n, 2), offsets off (r + 1); meta (r, 5) [half width, direction
/// (+1 / -1 one way along / against the geometry, 0 both), rank (motorway 7 ... residential 1, 0 none), urban, link];
/// junctions (connector ids con_id at geometry fractions con_at, offsets con_off (r + 1)); route memberships
/// (rt_id: the server's route number ids, from rt_from to rt_to as fractions of the segment, offsets rt_off (r + 1));
/// bridge / tunnel ranges (skip (k, 2) fractions, offsets skip_off (r + 1)). Pavements: outlines poly (m, 2), offsets
/// poly_off. places (p, 4) [lon, lat, population, class]. params: [advance urban, advance rural, advance freeway,
/// lateral urban, lateral rural, lateral freeway, panel width, cone degrees, near min m, near max m, far max m,
/// near min population, far min population, mount height] (the style table's).
///
/// Returns (boards (k, 12) [x, y, travel x, travel y, road x, road y, urban, size class (0 town, 1 road, 2 freeway),
/// junction lon, junction lat, first row, rows], rows (m, 11) [turn (-1 left, 0 straight, 1 right), bearing,
/// route 0, route 1, route 2 (ids or -1), near place (index or -1), its distance m, far place, its distance m,
/// the arm's rank, 0]).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn dirsign_place<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, ll: PyReadonlyArray2<'py, f64>,
                          off: PyReadonlyArray1<'py, i64>, meta: PyReadonlyArray2<'py, f64>,
                          con_at: PyReadonlyArray1<'py, f64>, con_id: PyReadonlyArray1<'py, i64>,
                          con_off: PyReadonlyArray1<'py, i64>, rt_id: PyReadonlyArray1<'py, i64>,
                          rt_from: PyReadonlyArray1<'py, f64>, rt_to: PyReadonlyArray1<'py, f64>,
                          rt_off: PyReadonlyArray1<'py, i64>, skip: PyReadonlyArray2<'py, f64>,
                          skip_off: PyReadonlyArray1<'py, i64>, poly: PyReadonlyArray2<'py, f64>,
                          poly_off: PyReadonlyArray1<'py, i64>, places: PyReadonlyArray2<'py, f64>, bx: [f64; 4],
                          left_hand: bool, params: [f64; 15])
                          -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>) {
    let (pts, ll, off, meta) = (pts.as_array(), ll.as_array(), off.as_array(), meta.as_array());
    let (con_at, con_id, con_off) = (con_at.as_array(), con_id.as_array(), con_off.as_array());
    let (rt_id, rt_from, rt_to, rt_off) = (rt_id.as_array(), rt_from.as_array(), rt_to.as_array(), rt_off.as_array());
    let (skip, skip_off, poly, poly_off) = (skip.as_array(), skip_off.as_array(), poly.as_array(), poly_off.as_array());
    let places = Places { v: places.as_array() };
    let [adv_urban, adv_rural, adv_freeway, lat_urban, lat_rural, lat_freeway, panel_w, cone, near_min, near_max,
         far_max, near_pop, far_pop, _mount, repeat_m] = params;
    let mut net = Net { pts, ll, roads: Vec::new() };
    let nr = off.len().saturating_sub(1);

    // the roads, their pieces between junctions, and each piece's two arms
    let mut arms: Vec<Arm> = Vec::new();
    let mut at_node: HashMap<i64, Vec<usize>> = HashMap::new();
    for r in 0..nr {
        let (a, b) = (off[r] as usize, off[r + 1] as usize);
        let mut s = vec![0.0f64; b.saturating_sub(a)];
        for k in a + 1..b {
            s[k - a] = s[k - a - 1] + ((pts[[k, 0]] - pts[[k - 1, 0]]).powi(2) + (pts[[k, 1]] - pts[[k - 1, 1]]).powi(2)).sqrt();
        }
        net.roads.push(Road { a, b, s, hw: meta[[r, 0]], dir: meta[[r, 1]] as i64, rank: meta[[r, 2]] as i64,
                              urban: meta[[r, 3]] != 0.0, link: meta[[r, 4]] != 0.0 });
        let total = if b >= a + 2 { net.roads[r].s[b - a - 1] } else { 0.0 };
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
        for (piece, w) in nodes.windows(2).enumerate() {
            let (sa, sb) = (w[0].0 * total, w[1].0 * total);
            if sb - sa < 0.5 { continue; }
            for (fwd, node) in [(true, w[0].1), (false, w[1].1)] {
                at_node.entry(node).or_default().push(arms.len());
                arms.push(Arm { road: r, piece, sa, sb, fwd, node });
            }
        }
    }
    let net = net;
    let road_of = |m: &Arm| &net.roads[m.road];
    // traffic may leave the junction along the arm / arrive by it
    let can_out = |m: &Arm| { let d = road_of(m).dir; d == 0 || (d > 0) == m.fwd };
    let can_in = |m: &Arm| { let d = road_of(m).dir; d == 0 || (d > 0) != m.fwd };
    let routes_at = |r: usize, f: f64, out: &mut Vec<i64>| {
        for k in rt_off[r] as usize..rt_off[r + 1] as usize {
            if rt_from[k] - 1e-6 <= f && f <= rt_to[k] + 1e-6 && !out.contains(&rt_id[k]) { out.push(rt_id[k]); }
        }
    };
    // the arm followed through the next junctions: (where it leads: the chord from the junction, the route ids met)
    let walk = |m: &Arm| -> ([f64; 2], Vec<i64>) {
        let (start, _) = net.at(m.road, m.s0());
        let mut routes: Vec<i64> = Vec::new();
        let mut cur = *m;
        let mut done = 0.0;
        let mut end = start;
        for hop in 0..WALK_HOPS {
            let u1 = cur.len().min(WALK_M - done).max(0.0);
            routes_at(cur.road, net.frac(cur.road, cur.s_at(u1 * 0.5)), &mut routes);
            let (q, _) = net.at(cur.road, cur.s_at(u1));
            end = q;
            done += u1;
            if done >= WALK_M - 1.0 || u1 < cur.len() - 1e-6 { break; }
            // at the far end: the straightest way on that traffic can take
            // (the node at the piece's other end)
            let far_node = arms.iter().find(|o| o.road == cur.road && o.piece == cur.piece && o.fwd != cur.fwd).map(|o| o.node);
            let Some(fnode) = far_node else { break };
            let back = net.chord(&cur, (cur.len() - TANGENT_M).max(0.0), cur.len());
            let mut best: Option<(Arm, f64)> = None;
            for &i in at_node.get(&fnode).map(|v| v.as_slice()).unwrap_or(&[]) {
                let o = arms[i];
                if o.road == cur.road && o.piece == cur.piece { continue; }
                if !can_out(&o) { continue; }
                let c = dot(net.leave_dir(&o), back);
                if c >= WALK_COS && best.map_or(true, |(_, bc)| c > bc) { best = Some((o, c)); }
            }
            match best { Some((o, _)) if hop + 1 < WALK_HOPS => cur = o, _ => break }
        }
        (unit([end[0] - start[0], end[1] - start[1]]), routes)
    };

    let boxes: Vec<[f64; 4]> = (0..poly_off.len().saturating_sub(1)).map(|q| {
        let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for k in poly_off[q] as usize..poly_off[q + 1] as usize {
            b[0] = b[0].min(poly[[k, 0]]); b[1] = b[1].min(poly[[k, 1]]);
            b[2] = b[2].max(poly[[k, 0]]); b[3] = b[3].max(poly[[k, 1]]);
        }
        b
    }).collect();

    let mut nodes_sorted: Vec<i64> = at_node.keys().copied().collect();
    nodes_sorted.sort();
    let mut pend: Vec<Pending> = Vec::new();
    let mut placed: Vec<([f64; 2], [f64; 2])> = Vec::new();
    for node in nodes_sorted {
        let here = &at_node[&node];
        if here.len() < 3 { continue; }
        let major = |m: &Arm| { let r = road_of(m); !r.link && r.rank >= PRIMARY };
        // the choices a driver has: other arms worth a name
        let worth = |m: &Arm| { let r = road_of(m); r.rank >= SECONDARY || (r.link && r.rank >= PRIMARY) };
        let ns = here.iter().filter(|&&i| major(&arms[i])).count();
        if ns < 2 { continue; }
        for &ai in here {
            let a = arms[ai];
            if !major(&a) || !can_in(&a) { continue; }
            let rd = road_of(&a);
            if a.len() < MIN_PIECE_M { continue; }
            // the junction's own place (lon/lat) from the arm's end
            let (jxy, jll) = net.at(a.road, a.s0());
            // the arms to name
            let heading_probe = net.arrive_dir(&a, a.len().min(30.0));
            let mut cand: Vec<([f64; 2], Vec<i64>, f64, i64, usize)> = Vec::new();   // (bearing, routes, angle, rank, arm)
            for &bi in here {
                if bi == ai { continue; }
                let b = arms[bi];
                if !can_out(&b) || !worth(&b) { continue; }
                if b.road == a.road && b.piece == a.piece { continue; }
                let (bearing, routes) = walk(&b);
                let ang = signed_diff(angle(bearing), angle(heading_probe));
                // a way back the way we came is no choice
                if ang.abs() > 150.0 { continue; }
                cand.push((bearing, routes, ang, net.roads[b.road].rank, bi));
            }
            if cand.is_empty() { continue; }
            // straight first, then left, then right (the top-down order of a German board), the nearer to straight first
            let key = |ang: f64| -> (i64, i64) {
                if ang.abs() <= STRAIGHT_DEG { (0, (ang.abs() * 100.0) as i64) } else if ang > 0.0 { (1, (ang * 100.0) as i64) } else { (2, (-ang * 100.0) as i64) }
            };
            cand.sort_by_key(|c| (key(c.2), net.arms_key(c.4, &arms)));
            cand.truncate(MAX_ROWS);
            // A turn: no arm goes straight on along the road the board stands on (it ends here or bends), or the road it
            // goes on along carries other route numbers: always a board (not thinned below).
            let mut here_routes: Vec<i64> = Vec::new();
            routes_at(a.road, net.frac(a.road, a.s_at(a.len() * 0.5)), &mut here_routes);
            let straight = cand.iter().find(|c| c.2.abs() <= STRAIGHT_DEG);
            let turn = match straight {
                None => true,
                Some(c) => !here_routes.is_empty() && !c.1.is_empty() && !c.1.iter().any(|r| here_routes.contains(r)),
            };
            // CITY BOARDS: in a town a board stands only at the junctions a driver needs one at (real towns: a few
            // boards at the big junctions and at the ways out, the user, 2026-11-07), not at every crossing of two main
            // streets: an interchange (a link among the arms), a way out of the built-up land (an arm that leaves
            // it), a fork of numbered routes (two different route numbers among the arms) or a turn of a numbered road.
            if rd.urban {
                let interchange = here.iter().any(|&i| road_of(&arms[i]).link);
                let way_out = cand.iter().any(|c| !net.roads[arms[c.4].road].urban);
                let mut ids: Vec<i64> = Vec::new();
                for c in &cand { for r in &c.1 { if !ids.contains(r) { ids.push(*r); } } }
                let numbered_turn = turn && !here_routes.is_empty();      // (a plain T of unnumbered streets is no turn worth a board)
                if !(interchange || way_out || ids.len() >= 2 || numbered_turn) { continue; }
            }
            // the board's spot
            let freeway = rd.rank >= MOTORWAY || (rd.rank >= TRUNK && rd.dir != 0 && !rd.urban);
            let adv = if freeway { adv_freeway } else if rd.urban { adv_urban } else { adv_rural };
            let lateral = if freeway { lat_freeway } else if rd.urban { lat_urban } else { lat_rural };
            let size = if freeway { 2.0 } else if rd.urban { 0.0 } else { 1.0 };
            let (sa_, sb_) = (skip_off[a.road] as usize, skip_off[a.road + 1] as usize);
            let mut u = adv.min(a.len() - 10.0);
            if u < 5.0 { continue; }
            let mut moved = 0.0;
            for _ in 0..4 {
                let f = net.frac(a.road, a.s_at(u));
                let mut hit = None;
                for k in sa_..sb_ { if skip[[k, 0]] - 1e-6 <= f && f <= skip[[k, 1]] + 1e-6 { hit = Some(k); break; } }
                match hit {
                    None => break,
                    Some(k) => {
                        // move toward the junction (a.s_at decreasing u) past the range's end nearer to it
                        let near_end = if a.fwd { skip[[k, 0]] } else { skip[[k, 1]] };
                        let du = (f - near_end).abs() * net.total(a.road) + 8.0;
                        u -= du;
                        moved += du;
                    }
                }
            }
            if moved > SKIP_PAST_M || u < 5.0 { continue; }
            let side = if left_hand { -1.0 } else { 1.0 };
            // (a town board: a small one on a single stand or a pole: one spot to keep clear, not two posts)
            let pw = if rd.urban && !freeway { TOWN_PANEL_W } else { panel_w };
            let base = rd.hw + lateral + pw / 2.0;
            let mut found: Option<([f64; 2], [f64; 2], [f64; 2])> = None;
            'spot: for du in ALONG_STEPS_M {
                let uu = u - du;                       // (farther along: toward the junction)
                if uu < 5.0 { break; }
                let (road_xy, _) = net.at(a.road, a.s_at(uu));
                let t = net.arrive_dir(&a, uu);
                let right = [t[1] * side, -t[0] * side];
                let mut extra = 0.0;
                while extra <= OUT_MAX_M + 1e-9 {
                    let q = [road_xy[0] + right[0] * (base + extra), road_xy[1] + right[1] * (base + extra)];
                    // the two posts (near and far edge of the panel) clear of the pavements
                    let p1 = [q[0] - right[0] * (pw / 2.0 - 0.3), q[1] - right[1] * (pw / 2.0 - 0.3)];
                    let p2 = [q[0] + right[0] * (pw / 2.0 - 0.3), q[1] + right[1] * (pw / 2.0 - 0.3)];
                    let town = pw < panel_w;
                    if (town || (clear(p1, POST_R + CLEAR_M, &poly, &poly_off, &boxes) && clear(p2, POST_R + CLEAR_M, &poly, &poly_off, &boxes)))
                        && clear(q, POST_R + CLEAR_M, &poly, &poly_off, &boxes) {
                        found = Some((q, t, road_xy));
                        break 'spot;
                    }
                    extra += OUT_STEP_M;
                }
            }
            let Some((q, t, road_xy)) = found else { continue };
            let gap = if rd.urban { URBAN_GAP_M } else { BOARD_GAP_M };
            if placed.iter().any(|(p, d)| dot(*d, t) > SAME_WAY_COS && (p[0] - q[0]).powi(2) + (p[1] - q[1]).powi(2) < gap * gap) {
                continue;
            }
            placed.push((q, t));
            // (the repeats of a board are thinned over every board of the loaded area: those outside the box are
            // worked out too, but only when there is a thinning)
            let inside = q[0] >= bx[0] && q[0] < bx[2] && q[1] >= bx[1] && q[1] < bx[3];
            if repeat_m <= 0.0 && !inside { continue; }
            // the places for each row, none twice on a board
            let mut used: HashSet<usize> = HashSet::new();
            let mut rows: Vec<[f64; 11]> = Vec::new();
            let heading = t;                // (the approach's heading: the board's travel direction)
            let mut made = 0usize;
            for (bearing, routes, ang, rank, _bi) in &cand {
                let near_ok = |p: &Pick| -> bool { let c = places.v[[p.idx, 3]]; c >= 1.0 || places.v[[p.idx, 2]] >= near_pop };
                let picks = places.ahead(jll[0], jll[1], *bearing, heading, cone, near_min, far_max, &used);
                let near = picks.iter().filter(|p| p.d <= near_max && near_ok(p))
                    .min_by(|p, q| (p.d * (1.0 + p.off / 45.0)).partial_cmp(&(q.d * (1.0 + q.off / 45.0))).unwrap()
                        .then(p.idx.cmp(&q.idx)));
                let near_idx = near.map(|p| p.idx);
                let near_d = near.map_or(0.0, |p| p.d);
                let far = picks.iter().filter(|p| places.v[[p.idx, 2]] >= far_pop && Some(p.idx) != near_idx
                                              && (near_idx.is_none() || p.d >= near_d * 1.3))
                    .max_by(|p, q| (places.v[[p.idx, 2]] / (1.0 + p.d / 40_000.0) / (1.0 + p.off / 60.0))
                        .partial_cmp(&(places.v[[q.idx, 2]] / (1.0 + q.d / 40_000.0) / (1.0 + q.off / 60.0))).unwrap()
                        .then(q.idx.cmp(&p.idx)));
                if near_idx.is_none() && far.is_none() && routes.is_empty() { continue; }
                if let Some(i) = near_idx { used.insert(i); }
                if let Some(f) = far { used.insert(f.idx); }
                // (angle > 0: the arm turns counter-clockwise: left, whichever side drives)
                let turn = if ang.abs() <= STRAIGHT_DEG { 0.0 } else if *ang > 0.0 { -1.0 } else { 1.0 };
                let mut rr = [-1.0f64; MAX_ROUTES];
                for (k, id) in routes.iter().take(MAX_ROUTES).enumerate() { rr[k] = *id as f64; }
                rows.push([turn, angle(*bearing), rr[0], rr[1], rr[2],
                    near_idx.map_or(-1.0, |i| i as f64), near_d,
                    far.map_or(-1.0, |f| f.idx as f64), far.map_or(0.0, |f| f.d), *rank as f64, 0.0]);
                made += 1;
            }
            if made == 0 { continue; }
            // what it says, without the distances: the same board from every junction that has it
            let sector = ((angle(t) / 45.0).round() as i64).rem_euclid(8) as u64;
            let mut sig = splitmix(sector ^ 0xD1B5_4A32_D192_ED03);
            for r in &rows {
                for &v in &[r[0], r[2], r[3], r[4], r[5], r[7]] { sig = splitmix(sig ^ (v as i64 as u64)); }
            }
            let key = splitmix(splitmix(((jll[0] * 1e5).round() as i64) as u64) ^ ((jll[1] * 1e5).round() as i64) as u64 ^ sector);
            // (a city board is a wanted one already: no thinning of repeats)
            pend.push(Pending { q, t, road_xy, urban: rd.urban, size, jll, rows, sig, turn: turn || rd.urban, key, inside });
            let _ = jxy;
        }
    }
    // The repeats of a board: one in each cell (about repeat_m a side) of a fixed lat/lon grid, the lowest key of those
    // that say the same; a turn is always kept. (A fixed grid, no walking along the roads: the same boards whichever
    // tile asks, once its margin covers the cell.)
    let mut best: HashMap<(u64, i64, i64), u64> = HashMap::new();
    let cell_of = |p: &Pending| -> (i64, i64) {
        let dlat = repeat_m / M_PER_DEG;
        let band = (p.jll[1].floor() + 0.5).to_radians().dcos().max(0.05);
        ((p.jll[0] / (dlat / band)).floor() as i64, (p.jll[1] / dlat).floor() as i64)
    };
    if repeat_m > 0.0 {
        for p in pend.iter().filter(|p| !p.turn) {
            let (cx, cy) = cell_of(p);
            let e = best.entry((p.sig, cx, cy)).or_insert(p.key);
            if p.key < *e { *e = p.key; }
        }
    }
    let mut boards: Vec<f64> = Vec::new();
    let mut rows_out: Vec<f64> = Vec::new();
    for p in &pend {
        if !p.inside { continue; }
        if repeat_m > 0.0 && !p.turn {
            let (cx, cy) = cell_of(p);
            if best.get(&(p.sig, cx, cy)) != Some(&p.key) { continue; }
        }
        let first = rows_out.len() / 11;
        for r in &p.rows { rows_out.extend_from_slice(r); }
        boards.extend_from_slice(&[p.q[0], p.q[1], p.t[0], p.t[1], p.road_xy[0], p.road_xy[1],
                                   if p.urban { 1.0 } else { 0.0 }, p.size, p.jll[0], p.jll[1], first as f64, p.rows.len() as f64]);
    }
    let nb = boards.len() / 12;
    let nrw = rows_out.len() / 11;
    (numpy::ndarray::Array2::from_shape_vec((nb, 12), boards).unwrap().into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((nrw, 11), rows_out).unwrap().into_pyarray(py))
}

/// A board worked out, before the repeats are thinned.
struct Pending {
    q: [f64; 2],
    t: [f64; 2],
    road_xy: [f64; 2],
    urban: bool,
    size: f64,
    jll: [f64; 2],
    rows: Vec<[f64; 11]>,
    sig: u64,
    turn: bool,
    key: u64,
    inside: bool,
}

fn splitmix(mut z: u64) -> u64 {
    z = z.wrapping_add(0x9E3779B97F4A7C15);
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
    z ^ (z >> 31)
}

impl<'a> Net<'a> {
    /// A stable tie-break for arms (their road's first vertex): the same order in every tile.
    fn arms_key(&self, i: usize, arms: &[Arm]) -> (i64, i64) {
        let rd = &self.roads[arms[i].road];
        (((self.pts[[rd.a, 0]] * 100.0).round()) as i64, ((self.pts[[rd.a, 1]] * 100.0).round()) as i64)
    }
}

// ---- the mesh ----

const M_METAL: i64 = 0;             // material 0: bare metal (the back and the posts); the faces' are the caller's, 1..
const POST_HALF_W: f64 = 0.04;
const BACK_T: f64 = 0.05;           // the board's depth behind its face
const POLE_R: f64 = 0.17;           // m: the radius a board's clamps go round on an existing pole
const STAND_R: f64 = 0.06;          // m: a town board's own round steel stand
const CLAMP_H: f64 = 0.06;

/// The meshes of direction boards: signs (k, 10) [x, y, ground z, road z, travel x, travel y, width, height, material
/// (the face's, >= 1), mount (m the board's bottom stands above the road's edge)] and, in a 12 column array, mode (-1
/// the board's own two posts, 0 hung on an existing pole at (x, y) by two clamps, the board reaching out from the pole
/// to the side away from the road, 1 on its own round stand at (x, y), the same way) and side (+1 right-hand traffic,
/// -1 left-hand), relative to origin. A board is its face (a quad textured 0..1, the lower left of the picture at u = 0,
/// v = 0), a metal back and its posts or clamps (render-only).
/// Returns (vertices (n, 3), triangles (m, 3), uv (n, 2), material per triangle (m,)).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn dirsign_mesh<'py>(py: Python<'py>, signs: PyReadonlyArray2<'py, f64>, origin: [f64; 3])
                         -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>,
                             Bound<'py, numpy::PyArray1<i64>>) {
    use crate::speedsigns::Mesh;
    let signs = signs.as_array();
    let mut m = Mesh { p: Vec::new(), t: Vec::new(), uv: Vec::new(), mi: Vec::new(), centre: [0.0; 3],
                       face: [0.0; 2], right: [0.0; 2] };
    for k in 0..signs.nrows() {
        let (x, y, zg, zr, tx, ty) = (signs[[k, 0]], signs[[k, 1]], signs[[k, 2]], signs[[k, 3]], signs[[k, 4]], signs[[k, 5]]);
        let (w, h, mat, mount) = (signs[[k, 6]], signs[[k, 7]], signs[[k, 8]].round() as i64, signs[[k, 9]]);
        if !(x.is_finite() && y.is_finite() && zg.is_finite() && zr.is_finite()) || w <= 0.0 || h <= 0.0 || mat < 1 { continue; }
        let tl = (tx * tx + ty * ty).sqrt().max(1e-9);
        let (tx, ty) = (tx / tl, ty / tl);
        m.centre = [x - origin[0], y - origin[1], zg - origin[2]];
        m.face = [-tx, -ty];
        m.right = [ty, -tx];
        let bottom = (zr + mount).max(zg + 1.2) - zg;
        let top = bottom + h;
        let hw = w / 2.0;
        let mode = if signs.ncols() >= 12 { signs[[k, 10]].round() as i64 } else { -1 };
        if mode >= 0 {
            // a town board by a pole (mode 0) or on its own stand (mode 1): the axis is at (x, y), the board's near edge
            // 0.2 m inside it, reaching out to the side away from the road (a driver's right in right-hand traffic)
            let side = if signs[[k, 11]] < 0.0 { -1.0 } else { 1.0 };
            let r = if mode == 1 { STAND_R } else { POLE_R };
            let (ylo, yhi) = if side > 0.0 { (-0.2, w - 0.2) } else { (-(w - 0.2), 0.2) };
            let xf = r + BACK_T;
            m.quad([[xf, ylo, bottom], [xf, yhi, bottom], [xf, yhi, top], [xf, ylo, top]], mat,
                   [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]);
            m.cuboid([r, ylo, bottom], [xf - 0.004, yhi, top], M_METAL);
            if mode == 1 {
                m.pole(0.0, STAND_R, -0.3, top - 0.05, M_METAL);
            } else {
                for zc in [bottom + 0.12, top - 0.12 - CLAMP_H] {
                    m.cuboid([-r - 0.015, -r - 0.015, zc], [r, r + 0.015, zc + CLAMP_H], M_METAL);
                }
            }
            continue;
        }
        // the face, then the back (a slab) and the posts
        m.quad([[0.0, -hw, bottom], [0.0, hw, bottom], [0.0, hw, top], [0.0, -hw, top]], mat,
               [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]);
        m.cuboid([-BACK_T, -hw, bottom], [-0.004, hw, top], M_METAL);
        let off = (hw - 0.3).max(0.0);
        let posts: &[f64] = if w > 1.8 { &[-1.0, 1.0] } else { &[0.0] };
        for &o in posts {
            m.cuboid([-BACK_T - 0.07, o * off - POST_HALF_W, -0.3], [-BACK_T - 0.005, o * off + POST_HALF_W, top - 0.1], M_METAL);
        }
    }
    let (n, t) = (m.p.len(), m.t.len());
    let p = numpy::ndarray::Array2::from_shape_vec((n, 3), m.p.into_iter().flatten().collect()).unwrap();
    let tri = numpy::ndarray::Array2::from_shape_vec((t, 3), m.t.into_iter().flatten().collect()).unwrap();
    let uv = numpy::ndarray::Array2::from_shape_vec((n, 2), m.uv.into_iter().flatten().collect()).unwrap();
    (p.into_pyarray(py), tri.into_pyarray(py), uv.into_pyarray(py), m.mi.into_pyarray(py))
}
