//! Time-to-reach along the road graph (server/reach.py; the retro optimization study's section J: "stream by
//! time-to-reach along the road graph", Crash's rails, GTA's window, Just Cause's queues).
//!
//! RoadReach keeps the drivable roads of the map cells near a fast car as a graph: Overture's segments split at their
//! connectors (the nodes, by connector id: every cell that holds a segment names the same ones), each piece an edge
//! with its polyline in the anchor's frame, its road class rank and one-way direction. Cells come and go
//! (add_cell / remove_cell; a segment held by several cells stays while any of them is in).
//!
//! grow() walks the graph from where the car is, the way it is going, and returns the tree of where it can get
//! within a horizon: points along the roads every step with their earliest arrival (ETA: the car's speed along the
//! road, plus the time a turn costs to brake for and speed up from) and the chance the driver goes that way (at each
//! junction: by the time the turn would cost and by the road class, a GPS route's way strongly preferred). Where the
//! loaded cells end the tree goes on straight (the frontier: the next map data to fetch). The most probable path (at
//! each junction the likeliest way) is returned too (the terrain blocks and data downloads ahead follow it).

use numpy::{ndarray::Array2, IntoPyArray, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::{exceptions::PyValueError, prelude::*, types::PyDict};
use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};

const GRID_M: f64 = 100.0;          // the snap / route grid's cell
const TANGENT_M: f64 = 15.0;        // an edge's direction at its ends: over this much of it
const TURN_M: f64 = 60.0;           // a way's turn at a junction: to its direction this far on (a ramp's curve)

struct Edge {
    a: u32,
    b: u32,
    pts: Vec<[f64; 2]>,
    cum: Vec<f64>,
    len: f64,
    rank: u8,
    dir: i8,            // 0 both ways, 1 a -> b only, -1 b -> a only
    alive: bool,
}

struct Cell {
    feats: Vec<u32>,
    quad: [[f64; 2]; 4],
}

#[pyclass(module = "earthfast")]
pub struct RoadReach {
    node_ids: HashMap<String, u32>,
    nodes: Vec<[f64; 2]>,
    edges: Vec<Edge>,
    feat_ids: HashMap<String, u32>,
    feats: Vec<(u32, Vec<u32>)>,     // (cells holding it, its edges)
    cells: HashMap<String, Cell>,
    adj: Vec<Vec<(u32, bool)>>,      // node -> (edge, leaves it a -> b)
    grid: HashMap<(i64, i64), Vec<u32>>,
    dirty: bool,
    dead: usize,
}

fn gkey(x: f64, y: f64) -> (i64, i64) {
    ((x / GRID_M).floor() as i64, (y / GRID_M).floor() as i64)
}

fn norm(v: [f64; 2]) -> [f64; 2] {
    let l = (v[0] * v[0] + v[1] * v[1]).sqrt();
    if l < 1e-9 { [0.0, 0.0] } else { [v[0] / l, v[1] / l] }
}

fn at_len(pts: &[[f64; 2]], cum: &[f64], d: f64) -> [f64; 2] {
    let d = d.clamp(0.0, *cum.last().unwrap_or(&0.0));
    let j = match cum.binary_search_by(|c| c.partial_cmp(&d).unwrap_or(Ordering::Less)) {
        Ok(j) => return pts[j],
        Err(j) => j.max(1).min(pts.len() - 1),
    };
    let (c0, c1) = (cum[j - 1], cum[j]);
    let t = if c1 - c0 > 1e-12 { (d - c0) / (c1 - c0) } else { 0.0 };
    [pts[j - 1][0] + (pts[j][0] - pts[j - 1][0]) * t, pts[j - 1][1] + (pts[j][1] - pts[j - 1][1]) * t]
}

fn cumlen(pts: &[[f64; 2]]) -> Vec<f64> {
    let mut c = Vec::with_capacity(pts.len());
    let mut s = 0.0;
    for (i, p) in pts.iter().enumerate() {
        if i > 0 {
            s += ((p[0] - pts[i - 1][0]).powi(2) + (p[1] - pts[i - 1][1]).powi(2)).sqrt();
        }
        c.push(s);
    }
    c
}

fn in_quad(q: &[[f64; 2]; 4], x: f64, y: f64) -> bool {
    // (a convex quad in either winding: the point on one side of every edge)
    let mut pos = false;
    let mut neg = false;
    for i in 0..4 {
        let (a, b) = (q[i], q[(i + 1) % 4]);
        let c = (b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0]);
        if c > 1e-9 { pos = true; }
        if c < -1e-9 { neg = true; }
    }
    !(pos && neg)
}

impl Edge {
    /// The point d along the edge travelled forward (fwd) or backward.
    fn point(&self, fwd: bool, d: f64) -> [f64; 2] {
        at_len(&self.pts, &self.cum, if fwd { d } else { self.len - d })
    }
    /// The way the edge leaves its start (fwd: a, else b), over its first TANGENT_M.
    fn out_dir(&self, fwd: bool) -> [f64; 2] {
        let d = TANGENT_M.min(self.len);
        let p0 = self.point(fwd, 0.0);
        let p1 = self.point(fwd, d);
        norm([p1[0] - p0[0], p1[1] - p0[1]])
    }
    /// The way it goes d along it (over the TANGENT_M before; its last TANGENT_M when it is shorter).
    fn dir_at(&self, fwd: bool, d: f64) -> [f64; 2] {
        let d1 = d.min(self.len);
        let d0 = (d1 - TANGENT_M).max(0.0);
        let p0 = self.point(fwd, d0);
        let p1 = self.point(fwd, d1);
        norm([p1[0] - p0[0], p1[1] - p0[1]])
    }
    /// The way it arrives at its end (fwd: b, else a), over its last TANGENT_M.
    fn in_dir(&self, fwd: bool) -> [f64; 2] {
        let d = TANGENT_M.min(self.len);
        let p0 = self.point(fwd, self.len - d);
        let p1 = self.point(fwd, self.len);
        norm([p1[0] - p0[0], p1[1] - p0[1]])
    }
}

#[derive(Clone, Copy)]
struct Entry {
    eta: f64,
    de: u32,            // directed edge: edge * 2 + (0 forward, 1 backward)
    p: f64,
    parent: i32,        // index in the processed list (-1: a root)
    from: f64,          // m along the directed edge it starts at (a root: the car's spot)
}

impl PartialEq for Entry {
    fn eq(&self, o: &Self) -> bool { self.eta == o.eta }
}
impl Eq for Entry {}
impl PartialOrd for Entry {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> { Some(self.cmp(o)) }
}
impl Ord for Entry {
    fn cmp(&self, o: &Self) -> Ordering {
        // (a min-heap on eta; the likelier first on a tie)
        o.eta.partial_cmp(&self.eta).unwrap_or(Ordering::Equal)
            .then(self.p.partial_cmp(&o.p).unwrap_or(Ordering::Equal))
    }
}

struct Done {
    p: f64,
    children: Vec<usize>,
    range: (usize, usize),
}

pub struct Params {
    pub horizon_s: f64,
    pub p_min: f64,
    pub step_m: f64,
    pub snap_m: f64,
    pub snap_cos: f64,
    pub a_turn: f64,
    pub v90: f64,
    pub t_p: f64,
    pub class_k: f64,
    pub link_k: f64,
    pub max_states: usize,
    pub route_m: f64,
    pub route_share: f64,
}

struct Route {
    segs: Vec<([f64; 2], [f64; 2])>,
    grid: HashMap<(i64, i64), Vec<u32>>,
}

impl Route {
    fn new(xy: &[[f64; 2]]) -> Route {
        let mut segs = Vec::new();
        let mut grid: HashMap<(i64, i64), Vec<u32>> = HashMap::new();
        for w in xy.windows(2) {
            let (a, b) = (w[0], w[1]);
            let i = segs.len() as u32;
            segs.push((a, b));
            let (g0, g1) = (gkey(a[0].min(b[0]), a[1].min(b[1])), gkey(a[0].max(b[0]), a[1].max(b[1])));
            for gx in g0.0..=g1.0 {
                for gy in g0.1..=g1.1 {
                    grid.entry((gx, gy)).or_default().push(i);
                }
            }
        }
        Route { segs, grid }
    }

    /// Does the route pass within r of p, going the way of dir (cos >= 0.7)?
    fn follows(&self, p: [f64; 2], dir: [f64; 2], r: f64) -> bool {
        let (g0, g1) = (gkey(p[0] - r, p[1] - r), gkey(p[0] + r, p[1] + r));
        for gx in g0.0..=g1.0 {
            for gy in g0.1..=g1.1 {
                let Some(v) = self.grid.get(&(gx, gy)) else { continue };
                for &i in v {
                    let (a, b) = self.segs[i as usize];
                    let d = [b[0] - a[0], b[1] - a[1]];
                    let l2 = d[0] * d[0] + d[1] * d[1];
                    if l2 < 1e-9 { continue; }
                    let t = (((p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1]) / l2).clamp(0.0, 1.0);
                    let q = [a[0] + d[0] * t, a[1] + d[1] * t];
                    if (q[0] - p[0]).hypot(q[1] - p[1]) <= r {
                        let u = norm(d);
                        if u[0] * dir[0] + u[1] * dir[1] >= 0.7 {
                            return true;
                        }
                    }
                }
            }
        }
        false
    }
}

impl RoadReach {
    fn node(&mut self, id: &str, p: [f64; 2]) -> u32 {
        if let Some(&n) = self.node_ids.get(id) {
            return n;
        }
        let n = self.nodes.len() as u32;
        self.nodes.push(p);
        self.node_ids.insert(id.to_string(), n);
        n
    }

    fn rebuild(&mut self) {
        if !self.dirty {
            return;
        }
        self.adj = vec![Vec::new(); self.nodes.len()];
        self.grid.clear();
        for (i, e) in self.edges.iter().enumerate() {
            if !e.alive {
                continue;
            }
            if e.dir >= 0 {
                self.adj[e.a as usize].push((i as u32, true));
            }
            if e.dir <= 0 {
                self.adj[e.b as usize].push((i as u32, false));
            }
            let mut last = None;
            for w in e.pts.windows(2) {
                let (a, b) = (w[0], w[1]);
                let (g0, g1) = (gkey(a[0].min(b[0]), a[1].min(b[1])), gkey(a[0].max(b[0]), a[1].max(b[1])));
                for gx in g0.0..=g1.0 {
                    for gy in g0.1..=g1.1 {
                        if last == Some((gx, gy)) {
                            continue;
                        }
                        let v = self.grid.entry((gx, gy)).or_default();
                        if v.last() != Some(&(i as u32)) {
                            v.push(i as u32);
                        }
                        last = Some((gx, gy));
                    }
                }
            }
        }
        self.dirty = false;
    }

    fn loaded(&self, p: [f64; 2]) -> bool {
        self.cells.values().any(|c| in_quad(&c.quad, p[0], p[1]))
    }

    /// The turn's time cost (s) at speed s through angle theta (rad): braking to the turn's speed and back up.
    fn turn_delay(theta: f64, s: f64, pr: &Params) -> f64 {
        if theta <= 1e-3 {
            return 0.0;
        }
        let vt = s.min(pr.v90 * std::f64::consts::FRAC_PI_2 / theta);
        (s - vt) * (s - vt) / (pr.a_turn * s)
    }

    fn grow_impl(&mut self, x: f64, y: f64, vx: f64, vy: f64, pr: &Params, route: Option<&Route>)
                     -> Option<(Vec<[f64; 4]>, Vec<[f64; 3]>, Vec<[f64; 4]>, usize)> {
        let s = vx.hypot(vy);
        if s < 0.5 || !(x.is_finite() && y.is_finite()) {
            return None;
        }
        self.rebuild();
        let u = [vx / s, vy / s];
        // the edges the car is on: within snap_m, its way along them (one-way roads only forward)
        let mut cands: HashMap<u32, (f64, f64)> = HashMap::new();      // de -> (score, m along it)
        let (g0, g1) = (gkey(x - pr.snap_m, y - pr.snap_m), gkey(x + pr.snap_m, y + pr.snap_m));
        for gx in g0.0..=g1.0 {
            for gy in g0.1..=g1.1 {
                let Some(v) = self.grid.get(&(gx, gy)) else { continue };
                for &ei in v {
                    let e = &self.edges[ei as usize];
                    for j in 0..e.pts.len().saturating_sub(1) {
                        let (a, b) = (e.pts[j], e.pts[j + 1]);
                        let d = [b[0] - a[0], b[1] - a[1]];
                        let l2 = d[0] * d[0] + d[1] * d[1];
                        if l2 < 1e-9 { continue; }
                        let t = (((x - a[0]) * d[0] + (y - a[1]) * d[1]) / l2).clamp(0.0, 1.0);
                        let q = [a[0] + d[0] * t, a[1] + d[1] * t];
                        let dist = (q[0] - x).hypot(q[1] - y);
                        if dist > pr.snap_m { continue; }
                        let tl = l2.sqrt();
                        let cf = (d[0] * u[0] + d[1] * u[1]) / tl;
                        let along = e.cum[j] + t * tl;
                        for (fwd, c) in [(true, cf), (false, -cf)] {
                            if c < pr.snap_cos || (fwd && e.dir < 0) || (!fwd && e.dir > 0) { continue; }
                            let de = ei * 2 + if fwd { 0 } else { 1 };
                            let score = dist + 40.0 * (1.0 - c);
                            let m = if fwd { along } else { e.len - along };
                            let cur = cands.entry(de).or_insert((f64::INFINITY, 0.0));
                            if score < cur.0 {
                                *cur = (score, m);
                            }
                        }
                    }
                }
            }
        }
        if cands.is_empty() {
            return None;
        }
        let mut roots: Vec<(u32, f64, f64)> = cands.into_iter().map(|(de, (sc, m))| (de, sc, m)).collect();
        roots.sort_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(Ordering::Equal).then(a.0.cmp(&b.0)));
        let best = roots[0].1;
        roots.retain(|r| r.1 <= best + 12.0);
        roots.truncate(3);
        // (a road the car is nearer to and going along more: likelier; a lower class beside a motorway (a frontage
        // road, a ramp) less likely)
        let rank_of = |de: u32| (self.edges[(de / 2) as usize].rank & 0x7f) as i32;
        let top = roots.iter().map(|r| rank_of(r.0)).max().unwrap_or(0);
        let mut ws: Vec<f64> = roots.iter().map(|r| (-(r.1 - best) / 3.0).exp()
            * pr.class_k.powi((top - rank_of(r.0)).clamp(0, 4))).collect();
        if let Some(r) = route {
            // (on a GPS route: the edge along it the likeliest)
            let on: Vec<bool> = roots.iter().map(|&(de, _, m)| {
                let e = &self.edges[(de / 2) as usize];
                let fwd = de % 2 == 0;
                let (p0, p1) = (e.point(fwd, m), e.point(fwd, (m + TANGENT_M).min(e.len)));
                r.follows(p0, norm([p1[0] - p0[0], p1[1] - p0[1]]), pr.route_m)
            }).collect();
            route_shares(&mut ws, &on, pr.route_share);
        }
        let wsum: f64 = ws.iter().sum();

        let mut heap = BinaryHeap::new();
        for (r, w) in roots.iter().zip(&ws) {
            heap.push(Entry { eta: 0.0, de: r.0, p: w / wsum, parent: -1, from: r.2 });
        }
        let mut visited: HashMap<u32, usize> = HashMap::new();
        let mut done: Vec<Done> = Vec::new();
        let mut pts: Vec<[f64; 4]> = Vec::new();
        let mut frontier: Vec<[f64; 4]> = Vec::new();
        let mut root_done: Vec<usize> = Vec::new();
        while let Some(en) = heap.pop() {
            if let Some(&k) = visited.get(&en.de) {
                // (reached again, later: its chance grows; walked again from here when the new way is much likelier
                // than the one it was walked with, so what lies beyond gets that chance: a root on the next piece of
                // the car's own road, a low-chance guess, took the road's whole way on with it)
                if en.p <= 2.0 * done[k].p {
                    done[k].p = (done[k].p + en.p).min(1.0);
                    continue;
                }
            }
            if done.len() >= pr.max_states {
                break;
            }
            let ei = (en.de / 2) as usize;
            let fwd = en.de % 2 == 0;
            let e = &self.edges[ei];
            let idx = done.len();
            visited.insert(en.de, idx);
            if en.parent >= 0 {
                done[en.parent as usize].children.push(idx);
            } else {
                root_done.push(idx);
            }
            // its points: every step from where it starts, to its end or the horizon
            let left = e.len - en.from;
            let room = ((pr.horizon_s - en.eta) * s).max(0.0);
            let go = left.min(room);
            let i0 = pts.len();
            if en.parent < 0 {
                let p = e.point(fwd, en.from);
                pts.push([p[0], p[1], en.eta, en.p]);
            }
            let mut d = pr.step_m;
            while d < go {
                let p = e.point(fwd, en.from + d);
                pts.push([p[0], p[1], en.eta + d / s, en.p]);
                d += pr.step_m;
            }
            let p = e.point(fwd, en.from + go);
            pts.push([p[0], p[1], en.eta + go / s, en.p]);
            let eta_end = en.eta + left / s;
            done.push(Done { p: en.p, children: Vec::new(), range: (i0, pts.len()) });
            if go < left || eta_end >= pr.horizon_s {
                continue;               // (cut at the horizon)
            }
            let node = if fwd { e.b } else { e.a };
            let at = self.nodes[node as usize];
            let t_in = e.in_dir(fwd);
            let end = e.point(fwd, e.len);
            if !self.loaded(at) {
                // the map data ends here: on straight for the rest of the horizon (the frontier)
                frontier.push([end[0], end[1], eta_end, en.p]);
                let rest = (pr.horizon_s - eta_end) * s;
                let mut d = pr.step_m;
                while d <= rest + 1e-9 {
                    pts.push([end[0] + t_in[0] * d, end[1] + t_in[1] * d, eta_end + d / s, en.p]);
                    d += pr.step_m;
                }
                done[idx].range.1 = pts.len();
                continue;
            }
            let rank_in = e.rank;
            let mut opts: Vec<(u32, f64)> = Vec::new();         // (de, weight)
            let mut on_route: Vec<bool> = Vec::new();
            for &(oi, ofwd) in &self.adj[node as usize] {
                if oi as usize == ei {
                    continue;           // (no U-turn back along the same piece)
                }
                let o = &self.edges[oi as usize];
                let t_out = o.out_dir(ofwd);
                let c = (t_in[0] * t_out[0] + t_in[1] * t_out[1]).clamp(-1.0, 1.0);
                // (and the way it goes TURN_M on: a ramp curving away from the road it leaves turns more than its
                // first metres show)
                let t_on = o.dir_at(ofwd, TURN_M);
                let c2 = (t_in[0] * t_on[0] + t_in[1] * t_on[1]).clamp(-1.0, 1.0);
                let theta = c.acos().max(c2.acos());
                if theta > 2.6 {
                    continue;           // (~150 deg: a U-turn)
                }
                let delay = Self::turn_delay(theta, s, pr);
                let mut w = (-delay / pr.t_p).exp();
                let (ri, ro) = (rank_in & 0x7f, o.rank & 0x7f);
                if ro < ri {
                    w *= pr.class_k.powi((ri - ro).min(4) as i32);
                }
                if o.rank & 0x80 != 0 && rank_in & 0x80 == 0 {
                    w *= pr.link_k;     // (off the road onto a ramp)
                }
                on_route.push(route.is_some_and(|r| r.follows(o.point(ofwd, (2.0 * TANGENT_M).min(o.len)), t_out,
                                                                 pr.route_m)));
                opts.push((oi * 2 + if ofwd { 0 } else { 1 }, w));
            }
            if route.is_some() {
                let mut ws: Vec<f64> = opts.iter().map(|o| o.1).collect();
                route_shares(&mut ws, &on_route, pr.route_share);
                for (o, w) in opts.iter_mut().zip(ws) {
                    o.1 = w;
                }
            }
            let wsum: f64 = opts.iter().map(|o| o.1).sum();
            if wsum <= 0.0 {
                continue;               // (a dead end)
            }
            // (a turn's time cost makes it less likely, not later: the ETA is the earliest the car can be there at
            // its speed, by the road's length alone. Counted into the ETA, a canyon motorway's own curves put the
            // road 1.5 km on 15 s later at 500 mph, and every turn of a GPS route through Manhattan ~18 s later at
            // 300 mph, though a car on it took them at speed: tools/eta_sim.py)
            for (de, w) in opts {
                let p = en.p * w / wsum;
                let eta = eta_end;
                if p < pr.p_min || eta >= pr.horizon_s {
                    continue;
                }
                heap.push(Entry { eta, de, p, parent: idx as i32, from: 0.0 });
            }
        }
        // the most probable path: the likeliest root, then at each junction the likeliest way on
        let mut mpp: Vec<[f64; 3]> = Vec::new();
        let mut cur = root_done.iter().copied().max_by(|&a, &b| done[a].p.partial_cmp(&done[b].p)
            .unwrap_or(Ordering::Equal).then(b.cmp(&a)));
        while let Some(k) = cur {
            let (i0, i1) = done[k].range;
            for q in &pts[i0..i1] {
                mpp.push([q[0], q[1], q[2]]);
            }
            cur = done[k].children.iter().copied().max_by(|&a, &b| done[a].p.partial_cmp(&done[b].p)
                .unwrap_or(Ordering::Equal).then(b.cmp(&a)));
        }
        Some((pts, mpp, frontier, done.len()))
    }
}

/// The zoom-z Web Mercator tiles within corridor_m of a tree's points (lat, lon, eta, chance; reach.Tree.geotile_keys):
/// for each tile the point with the smallest priority (eta / max(chance, p_min) ^ gamma + gap / side_speed, gap: the
/// point's distance to the tile on the ground) and whether a point with a chance of p_want or more reaches it.
/// Returns (gx, gy, priority, wanted, eta, chance, gap) arrays.
#[pyfunction]
#[pyo3(signature = (lat, lon, eta, chance, z, corridor_m, p_want, side_speed, gamma, p_min))]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn reach_tile_keys<'py>(py: Python<'py>, lat: PyReadonlyArray1<'py, f64>, lon: PyReadonlyArray1<'py, f64>,
                            eta: PyReadonlyArray1<'py, f64>, chance: PyReadonlyArray1<'py, f64>, z: u8, corridor_m: f64,
                            p_want: f64, side_speed: f64, gamma: f64, p_min: f64)
                            -> PyResult<(Py<PyAny>, Py<PyAny>, Py<PyAny>, Py<PyAny>, Py<PyAny>, Py<PyAny>, Py<PyAny>)> {
    let (la, lo, et, ch) = (lat.as_array(), lon.as_array(), eta.as_array(), chance.as_array());
    let n = la.len();
    if lo.len() != n || et.len() != n || ch.len() != n {
        return Err(PyValueError::new_err("lat, lon, eta and chance must have the same length"));
    }
    if z > 30 || !(side_speed > 0.0) {
        return Err(PyValueError::new_err("z must be 0..30 and side_speed positive"));
    }
    let (la, lo, et, ch) = (la.to_vec(), lo.to_vec(), et.to_vec(), ch.to_vec());
    let got = py.detach(|| {
        let nt = (1u64 << z) as f64;
        let mut best: HashMap<(i64, i64), (f64, bool, f64, f64, f64)> = HashMap::new();
        for i in 0..n {
            if !(la[i].is_finite() && lo[i].is_finite() && et[i].is_finite()) {
                continue;
            }
            let fx = (lo[i] + 180.0) / 360.0 * nt;
            let lr = la[i].to_radians();
            let fy = (1.0 - (lr.tan() + 1.0 / lr.cos()).ln() / std::f64::consts::PI) / 2.0 * nt;
            let m_tile = 40075016.686 * lr.cos() / nt;
            if !(m_tile > 0.0) {
                continue;
            }
            let reach = (corridor_m / m_tile).ceil() as i64;
            let (tx0, ty0) = (fx.floor() as i64, fy.floor() as i64);
            let pri = et[i] / ch[i].max(p_min).powf(gamma);
            let want = ch[i] >= p_want;
            for dx in -reach..=reach {
                for dy in -reach..=reach {
                    let (gx, gy) = (tx0 + dx, ty0 + dy);
                    let ex = ((gx as f64 - fx).max(fx - (gx + 1) as f64)).max(0.0);
                    let ey = ((gy as f64 - fy).max(fy - (gy + 1) as f64)).max(0.0);
                    let gap = ex.hypot(ey) * m_tile;
                    if gap > corridor_m {
                        continue;
                    }
                    let k = pri + gap / side_speed;
                    let e = best.entry((gx, gy)).or_insert((f64::INFINITY, false, 0.0, 0.0, 0.0));
                    let w = e.1 || want;
                    if k < e.0 {
                        *e = (k, w, et[i], ch[i], gap);
                    }
                    e.1 = w;
                }
            }
        }
        let mut v: Vec<((i64, i64), (f64, bool, f64, f64, f64))> = best.into_iter().collect();
        v.sort_by(|a, b| a.0.cmp(&b.0));
        v
    });
    let gx: Vec<i64> = got.iter().map(|r| r.0 .0).collect();
    let gy: Vec<i64> = got.iter().map(|r| r.0 .1).collect();
    let key: Vec<f64> = got.iter().map(|r| r.1 .0).collect();
    let want: Vec<bool> = got.iter().map(|r| r.1 .1).collect();
    let e: Vec<f64> = got.iter().map(|r| r.1 .2).collect();
    let c: Vec<f64> = got.iter().map(|r| r.1 .3).collect();
    let g: Vec<f64> = got.iter().map(|r| r.1 .4).collect();
    Ok((gx.into_pyarray(py).into_any().unbind(), gy.into_pyarray(py).into_any().unbind(),
        key.into_pyarray(py).into_any().unbind(), want.into_pyarray(py).into_any().unbind(),
        e.into_pyarray(py).into_any().unbind(), c.into_pyarray(py).into_any().unbind(),
        g.into_pyarray(py).into_any().unbind()))
}

/// A GPS route's ways (on) take `share` of the chance between them (by their own weights), the others the rest;
/// unchanged when none or all of them follow it.
fn route_shares(ws: &mut [f64], on: &[bool], share: f64) {
    let (mut a, mut b) = (0.0, 0.0);
    for (w, &o) in ws.iter().zip(on) {
        if o { a += *w } else { b += *w }
    }
    let n_on = on.iter().filter(|&&o| o).count();
    if n_on == 0 || n_on == on.len() {
        return;
    }
    for (w, &o) in ws.iter_mut().zip(on) {
        *w = if o {
            share * if a > 0.0 { *w / a } else { 1.0 / n_on as f64 }
        } else if b > 0.0 {
            (1.0 - share) * *w / b
        } else {
            0.0
        };
    }
}

fn arr2<const N: usize>(py: Python<'_>, v: &[[f64; N]]) -> Py<PyAny> {
    Array2::from_shape_vec((v.len(), N), v.iter().flatten().copied().collect()).unwrap().into_pyarray(py)
        .into_any().unbind()
}

#[pymethods]
impl RoadReach {
    #[new]
    fn new() -> Self {
        RoadReach {
            node_ids: HashMap::new(), nodes: Vec::new(), edges: Vec::new(), feat_ids: HashMap::new(),
            feats: Vec::new(), cells: HashMap::new(), adj: Vec::new(), grid: HashMap::new(), dirty: true, dead: 0,
        }
    }

    /// Add a cell's drivable segments (already in the frame): feature ids, xy (n, 2) with each feature's
    /// [start, end) rows, its rank (class rank, | 0x80 for a ramp), its connectors (feature index, fraction along,
    /// id) and its one-way rules (feature index, value: > 0 along, < 0 against, fraction range). quad: the cell's
    /// outline in the frame (where the map data is loaded). Returns the segments new to the graph.
    #[allow(clippy::too_many_arguments)]
    fn add_cell(&mut self, key: String, quad: [[f64; 2]; 4], feat_ids: Vec<String>,
                xy: PyReadonlyArray2<'_, f64>, start: PyReadonlyArray1<'_, i64>, end: PyReadonlyArray1<'_, i64>,
                rank: PyReadonlyArray1<'_, u8>, conn_feat: PyReadonlyArray1<'_, i32>,
                conn_at: PyReadonlyArray1<'_, f32>, conn_ids: Vec<String>, ow_feat: PyReadonlyArray1<'_, i32>,
                ow_val: PyReadonlyArray1<'_, f32>, ow_start: PyReadonlyArray1<'_, f32>,
                ow_end: PyReadonlyArray1<'_, f32>) -> PyResult<usize> {
        let xy = xy.as_array();
        if xy.ncols() != 2 {
            return Err(PyValueError::new_err("xy must have two columns"));
        }
        let (start, end, rank) = (start.as_array(), end.as_array(), rank.as_array());
        let n = feat_ids.len();
        if start.len() != n || end.len() != n || rank.len() != n {
            return Err(PyValueError::new_err("start, end and rank must have one value per feature"));
        }
        let (cf, ca) = (conn_feat.as_array(), conn_at.as_array());
        if cf.len() != ca.len() || cf.len() != conn_ids.len() {
            return Err(PyValueError::new_err("the connector arrays differ in length"));
        }
        let (of, ov, os, oe) = (ow_feat.as_array(), ow_val.as_array(), ow_start.as_array(), ow_end.as_array());
        if of.len() != ov.len() || of.len() != os.len() || of.len() != oe.len() {
            return Err(PyValueError::new_err("the one-way arrays differ in length"));
        }
        if self.cells.contains_key(&key) {
            return Ok(0);
        }
        let pts_all: Vec<[f64; 2]> = xy.rows().into_iter().map(|r| [r[0], r[1]]).collect();
        let mut conns: Vec<Vec<(f64, usize)>> = vec![Vec::new(); n];
        for (k, (&f, &a)) in cf.iter().zip(ca.iter()).enumerate() {
            if f >= 0 && (f as usize) < n && a.is_finite() {
                conns[f as usize].push((a.clamp(0.0, 1.0) as f64, k));
            }
        }
        let mut ows: Vec<Vec<(f64, f64, f64)>> = vec![Vec::new(); n];
        for i in 0..of.len() {
            let f = of[i];
            if f >= 0 && (f as usize) < n {
                ows[f as usize].push((os[i] as f64, oe[i] as f64, ov[i] as f64));
            }
        }
        let mut cell_feats = Vec::new();
        let mut added = 0;
        for i in 0..n {
            if let Some(&fi) = self.feat_ids.get(&feat_ids[i]) {
                self.feats[fi as usize].0 += 1;
                cell_feats.push(fi);
                continue;
            }
            let (s0, s1) = (start[i].max(0) as usize, (end[i].max(0) as usize).min(pts_all.len()));
            if s1 < s0 + 2 {
                continue;
            }
            let pts = &pts_all[s0..s1];
            let cum = cumlen(pts);
            let total = *cum.last().unwrap();
            if total < 0.5 {
                continue;
            }
            let mut cs = conns[i].clone();
            cs.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(Ordering::Equal).then(a.1.cmp(&b.1)));
            let mut stops: Vec<(f64, String)> = Vec::new();
            if cs.first().map(|c| c.0 > 1e-4).unwrap_or(true) {
                stops.push((0.0, format!("{}#0", feat_ids[i])));
            }
            for (a, k) in cs {
                stops.push((a, conn_ids[k].clone()));
            }
            if stops.last().map(|c| c.0 < 1.0 - 1e-4).unwrap_or(true) {
                stops.push((1.0, format!("{}#1", feat_ids[i])));
            }
            let fi = self.feats.len() as u32;
            let mut fe = Vec::new();
            for w in stops.windows(2) {
                let ((fa, ida), (fb, idb)) = (&w[0], &w[1]);
                let (da, db) = (fa * total, fb * total);
                if db - da < 0.05 {
                    continue;
                }
                let mut ep = vec![at_len(pts, &cum, da)];
                for (j, &c) in cum.iter().enumerate() {
                    if c > da + 1e-6 && c < db - 1e-6 {
                        ep.push(pts[j]);
                    }
                }
                ep.push(at_len(pts, &cum, db));
                let a = self.node(ida, ep[0]);
                let b = self.node(idb, *ep.last().unwrap());
                if a == b && ep.len() < 3 {
                    continue;
                }
                let mid = (fa + fb) / 2.0;
                let ov = ows[i].iter().find(|r| r.0 <= mid && mid <= r.1).map(|r| r.2).unwrap_or(0.0);
                let ecum = cumlen(&ep);
                let len = *ecum.last().unwrap();
                fe.push(self.edges.len() as u32);
                self.edges.push(Edge { a, b, pts: ep, cum: ecum, len, rank: rank[i],
                                       dir: if ov > 0.0 { 1 } else if ov < 0.0 { -1 } else { 0 }, alive: true });
            }
            self.feat_ids.insert(feat_ids[i].clone(), fi);
            self.feats.push((1, fe));
            cell_feats.push(fi);
            added += 1;
        }
        self.cells.insert(key, Cell { feats: cell_feats, quad });
        self.dirty = true;
        Ok(added)
    }

    /// Drop a cell: the segments no other loaded cell holds leave the graph.
    fn remove_cell(&mut self, key: String) -> bool {
        let Some(c) = self.cells.remove(&key) else { return false };
        for fi in c.feats {
            let f = &mut self.feats[fi as usize];
            f.0 = f.0.saturating_sub(1);
            if f.0 == 0 {
                for &ei in &f.1 {
                    self.edges[ei as usize].alive = false;
                    self.dead += 1;
                }
                f.1.clear();
            }
        }
        self.feat_ids.retain(|_, fi| self.feats[*fi as usize].0 > 0);
        self.dirty = true;
        true
    }

    /// Build the graph's ways out of each node and its spatial grid after cells came or went (grow does it too when
    /// it has not been done: call this where the time is cheaper than in grow's caller).
    fn prepare(&mut self, py: Python<'_>) {
        py.detach(|| self.rebuild());
    }

    fn has_cell(&self, key: String) -> bool {
        self.cells.contains_key(&key)
    }

    fn cell_keys(&self) -> Vec<String> {
        let mut v: Vec<String> = self.cells.keys().cloned().collect();
        v.sort();
        v
    }

    /// (nodes, live edges, dead edges, cells)
    fn stats(&self) -> (usize, usize, usize, usize) {
        (self.nodes.len(), self.edges.len() - self.dead, self.dead, self.cells.len())
    }

    /// The live edges with a point within r of (x, y): [(edge, node a, node b, length, rank, one-way, ways out of a,
    /// ways out of b, first point, last point)] (tests and debugging).
    fn edges_near(&mut self, x: f64, y: f64, r: f64) -> Vec<(usize, u32, u32, f64, u8, i8, usize, usize, [f64; 2], [f64; 2])> {
        self.rebuild();
        let mut out = Vec::new();
        for (i, e) in self.edges.iter().enumerate() {
            if !e.alive || !e.pts.iter().any(|p| (p[0] - x).hypot(p[1] - y) <= r) {
                continue;
            }
            out.push((i, e.a, e.b, e.len, e.rank, e.dir, self.adj[e.a as usize].len(), self.adj[e.b as usize].len(),
                      e.pts[0], *e.pts.last().unwrap()));
        }
        out
    }

    /// Is (x, y) inside a loaded cell?
    fn is_loaded(&self, x: f64, y: f64) -> bool {
        self.loaded([x, y])
    }

    /// The tree of where a car at (x, y) going (vx, vy) can get within horizon_s (module doc), or None when it is on
    /// no road of the graph (none within snap_m going its way). {"pts": (n, 4) x, y, eta s, chance; "mpp": (k, 3)
    /// x, y, eta of the most probable path; "frontier": (m, 4) x, y, eta, chance where the loaded data ends;
    /// "states": directed edges walked}. route: a GPS route's line (k, 2) in the frame: at a junction the ways
    /// along it (within route_m) take route_share of the chance.
    #[pyo3(signature = (x, y, vx, vy, horizon_s, p_min=0.03, step_m=32.0, snap_m=30.0, snap_cos=0.7, a_turn=6.0,
                        v90=12.0, t_p=2.0, class_k=0.6, link_k=0.5, max_states=20000, route=None, route_m=20.0,
                        route_share=0.9))]
    #[allow(clippy::too_many_arguments)]
    fn grow(&mut self, py: Python<'_>, x: f64, y: f64, vx: f64, vy: f64, horizon_s: f64, p_min: f64, step_m: f64,
            snap_m: f64, snap_cos: f64, a_turn: f64, v90: f64, t_p: f64, class_k: f64, link_k: f64,
            max_states: usize, route: Option<PyReadonlyArray2<'_, f64>>, route_m: f64, route_share: f64)
            -> PyResult<Option<Py<PyDict>>> {
        if !(step_m > 0.1) || !(horizon_s >= 0.0) || !(a_turn > 0.0) || !(t_p > 0.0) {
            return Err(PyValueError::new_err("step_m, a_turn and t_p must be positive, horizon_s not negative"));
        }
        let pr = Params { horizon_s, p_min, step_m, snap_m, snap_cos, a_turn, v90, t_p, class_k, link_k, max_states,
                          route_m, route_share };
        let rxy: Option<Vec<[f64; 2]>> = match route {
            Some(r) => {
                let a = r.as_array();
                if a.ncols() != 2 {
                    return Err(PyValueError::new_err("route must have two columns"));
                }
                Some(a.rows().into_iter().map(|q| [q[0], q[1]]).collect())
            }
            None => None,
        };
        let got = py.detach(|| {
            let rt = rxy.as_ref().filter(|v| v.len() >= 2).map(|v| Route::new(v));
            self.grow_impl(x, y, vx, vy, &pr, rt.as_ref())
        });
        let Some((pts, mpp, frontier, states)) = got else { return Ok(None) };
        let d = PyDict::new(py);
        d.set_item("pts", arr2(py, &pts))?;
        d.set_item("mpp", arr2(py, &mpp))?;
        d.set_item("frontier", arr2(py, &frontier))?;
        d.set_item("states", states)?;
        Ok(Some(d.unbind()))
    }
}
