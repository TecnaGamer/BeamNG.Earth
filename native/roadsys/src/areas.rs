//! OpenStreetMap's mapped road areas (area:highway=* polygons, tools/build_osm_road_areas.py, server/osmareas.py) in
//! the road system: validated (`prepare`), the M0 audit of a generated surface against them (`audit`: edge distance,
//! area IoU, width error, missed and extra splits, island position) and, behind BNE_ROADSYS_AREAS=on, the carriageway
//! edges' soft targets (`fit`: per stroke a program of lane-width factor, outer shoulder widths and lateral shift).
//!
//! The lane rules still decide the lanes (their count, kinds, events, tapers, gores): the fit only scales the lanes'
//! widths within LANE_MIN..LANE_MAX, widens or narrows the outer shoulders and shifts the whole carriageway, smoothly
//! (5 m samples, outliers out by a running median, slopes at most 1:20 per edge), so each edge lies on the mapped one.
//! A polygon that is not a clean ring (open, too few points, self-crossing, tiny, non-finite) is never used; a station
//! whose mapped section is implausible (no area round the line, a width under 2 m or far past the generated one, a
//! junction mouth, a fork's gore window) takes no target; a stroke with too few good stations keeps today's widths.
//! Every rejection is counted (FitStats), never silent.
//!
//! Determinism: polygons in key order, strokes independent of each other, fixed sample stations, IEEE + - * / and
//! sqrt only, BTreeMap only; the raster audit is integer cells.
use crate::geom::*;
use crate::net::{is_strip, Net, Stroke, K_GORE, K_LANE, K_SHOULDER, SUP_STRUCT, SUP_TUNNEL};
use std::collections::BTreeMap;
use std::fmt::Write as _;

/// The areas' meaning in the keys (product.rs): bump when the fit or the classes change.
pub const AREA_FORMAT: u64 = 1;

pub const A_NONE: u8 = 0;
pub const A_MAJOR: u8 = 1;      // motorway, trunk, primary and their links
pub const A_SECONDARY: u8 = 2;
pub const A_TERTIARY: u8 = 3;
pub const A_MINOR: u8 = 4;      // residential, unclassified, living_street, road
pub const A_SERVICE: u8 = 5;
pub const A_PAVED: u8 = 6;      // other paved parts of a carriageway: hatched (prohibited), shoulders, bays, junctions
pub const A_ISLAND: u8 = 7;     // traffic islands, central islands, medians
pub const A_WALK: u8 = 8;       // footways, cycleways, pedestrian areas, steps
pub const A_OTHER: u8 = 9;      // parking spaces and lots, tracks, "yes" and any other value: neither right nor wrong
pub const CLASS_NAMES: [&str; 10] = ["none", "major", "secondary", "tertiary", "minor", "service", "paved", "island", "walk", "other"];

/// The class of an area:highway value (lower case, trimmed; the common misspellings OSM has of the road classes).
pub fn class_of(v: &str) -> u8 {
    let v = v.trim().to_ascii_lowercase();
    match v.as_str() {
        "motorway" | "motorway_link" | "trunk" | "trunk_link" | "primary" | "primary_link" => A_MAJOR,
        "secondary" | "secondary_link" => A_SECONDARY,
        "tertiary" | "tertiary_link" | "tetriary" | "teritary" | "teriary" => A_TERTIARY,
        "residential" | "unclassified" | "living_street" | "road" | "residental" | "residetial" | "residencial"
        | "street" | "roadway" | "carriageway" => A_MINOR,
        "service" | "driveway" | "access_road" | "alley" | "parking_aisle" | "servise" | "sevice" => A_SERVICE,
        "prohibited" | "hatched" | "shoulder" | "hard_shoulder" | "emergency" | "emergency_bay" | "parking_lane"
        | "bus_bay" | "bus" | "busway" | "bus_stop" | "junction" | "intersection" | "crossing" | "turning_circle"
        | "turning_loop" | "passing_place" | "marking" | "painted_island" | "buffer" | "side_strip" | "traffic_calming"
        | "taxi_stop" | "roundabout" | "mini_roundabout" | "lane" | "traffic_lane" => A_PAVED,
        "traffic_island" | "central_island" | "calming_island" | "island" | "median" | "central_reservation"
        | "central_barrier" | "separator" | "divider" | "traversable_island" | "kerb_top" | "verge" => A_ISLAND,
        "footway" | "cycleway" | "pedestrian" | "path" | "steps" | "sidewalk" | "platform" | "stairs" | "footpath"
        | "bridleway" | "corridor" | "elevator" | "courtyard" => A_WALK,
        "" => A_NONE,
        _ => A_OTHER,
    }
}

#[inline] pub fn drivable(c: u8) -> bool { (A_MAJOR..=A_PAVED).contains(&c) }

/// A mapped area as read (frame metres): its key (2 x the OSM id, + 1 for a relation), area:highway value, OSM layer,
/// flags (1 bridge, 2 tunnel), tagged street ways and rings (0 outer, 1 inner).
#[derive(Clone, Debug, Default)]
pub struct Area { pub key: u64, pub value: String, pub layer: i64, pub flags: u8, pub links: Vec<u64>, pub rings: Vec<(u8, Vec<P2>)> }

/// FNV-1a 64 of an area's whole record (its identity in the keys).
pub fn area_digest(a: &Area) -> u64 {
    let mut b: Vec<u8> = Vec::with_capacity(64 + a.rings.iter().map(|r| r.1.len() * 16).sum::<usize>());
    b.extend_from_slice(&a.key.to_le_bytes());
    b.extend_from_slice(&(a.value.len() as u64).to_le_bytes());
    b.extend_from_slice(a.value.as_bytes());
    b.extend_from_slice(&a.layer.to_le_bytes());
    b.push(a.flags);
    b.extend_from_slice(&(a.links.len() as u64).to_le_bytes());
    for l in &a.links { b.extend_from_slice(&l.to_le_bytes()); }
    b.extend_from_slice(&(a.rings.len() as u64).to_le_bytes());
    for (role, r) in &a.rings {
        b.push(*role);
        b.extend_from_slice(&(r.len() as u64).to_le_bytes());
        for p in r { b.extend_from_slice(&p[0].to_bits().to_le_bytes()); b.extend_from_slice(&p[1].to_bits().to_le_bytes()); }
    }
    fnv(&b)
}

pub fn bbox_of(p: &[P2]) -> [f64; 4] {
    let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    for q in p { b[0] = b[0].min(q[0]); b[1] = b[1].min(q[1]); b[2] = b[2].max(q[0]); b[3] = b[3].max(q[1]); }
    b
}

// ------------------------------------------------------------------------------------------- validation, index

pub const MAX_POINTS: usize = 6000;     // a polygon's points at most (beyond: a mapping error, not used)
pub const MIN_AREA_M2: f64 = 1.0;
const GRID_M: f64 = 50.0;

pub const BAD_SHORT: usize = 0;         // a ring under 4 points / not closable
pub const BAD_NONFINITE: usize = 1;
pub const BAD_LONG: usize = 2;
pub const BAD_TINY: usize = 3;
pub const BAD_SELF_X: usize = 4;        // a ring crossing itself or another ring of the polygon
pub const BAD_NO_OUTER: usize = 5;
pub const BAD_CLASS: usize = 6;         // a value of no class
pub const BAD_NAMES: [&str; 7] = ["short", "nonfinite", "long", "tiny", "self_crossing", "no_outer", "no_class"];

/// A polygon ready to use: its class, rings closed (first == last), bbox, area.
#[derive(Clone, Debug)]
pub struct Poly { pub key: u64, pub cls: u8, pub layer: i64, pub flags: u8, pub rings: Vec<(u8, Vec<P2>)>, pub bb: [f64; 4], pub area: f64 }

impl Poly {
    /// Even-odd over all its rings (an inner ring is a hole; a point on an edge counts on one side, consistently).
    pub fn contains(&self, p: P2) -> bool {
        if p[0] < self.bb[0] || p[0] > self.bb[2] || p[1] < self.bb[1] || p[1] > self.bb[3] { return false; }
        let mut inside = false;
        for (_role, r) in &self.rings {
            for w in r.windows(2) {
                let (a, b) = (w[0], w[1]);
                if (a[1] > p[1]) != (b[1] > p[1]) {
                    let x = a[0] + (p[1] - a[1]) / (b[1] - a[1]) * (b[0] - a[0]);
                    if p[0] < x { inside = !inside; }
                }
            }
        }
        inside
    }
    /// Is this polygon at the support level kind (SUP_*)? Bridges (the bridge flag or an OSM layer above 0) for a
    /// structure, tunnels for a tunnel, the rest on the ground.
    pub fn at_level(&self, sup: u8) -> bool {
        let bridge = self.flags & 1 != 0 || self.layer > 0;
        let tunnel = self.flags & 2 != 0 || self.layer < 0;
        match sup { SUP_STRUCT => bridge, SUP_TUNNEL => tunnel, _ => !bridge && !tunnel }
    }
}

fn ring_area(r: &[P2]) -> f64 {
    let mut a = 0.0;
    for w in r.windows(2) { a += cross(w[0], w[1]); }
    a / 2.0
}

/// Do any two non-adjacent edges of the rings (closed: first == last) cross or touch (a proper crossing, or a vertex
/// lying on another edge)? O(n^2) with a bbox test; n <= MAX_POINTS.
fn self_crossing(rings: &[(u8, Vec<P2>)]) -> bool {
    let mut e: Vec<(P2, P2, usize, usize, usize)> = Vec::new();   // (a, b, ring, index, ring length in edges)
    for (ri, (_role, r)) in rings.iter().enumerate() {
        let n = r.len() - 1;
        for i in 0..n { e.push((r[i], r[i + 1], ri, i, n)); }
    }
    let mut order: Vec<usize> = (0..e.len()).collect();
    order.sort_by(|&a, &b| e[a].0[0].min(e[a].1[0]).partial_cmp(&e[b].0[0].min(e[b].1[0])).unwrap().then(a.cmp(&b)));
    for (k, &i) in order.iter().enumerate() {
        let (a, b, ri, ii, n) = e[i];
        let xmax = a[0].max(b[0]);
        for &j in &order[k + 1..] {
            let (c, d, rj, jj, _m) = e[j];
            if c[0].min(d[0]) > xmax { break; }
            if c[1].min(d[1]) > a[1].max(b[1]) || c[1].max(d[1]) < a[1].min(b[1]) { continue; }
            if ri == rj && (ii.abs_diff(jj) <= 1 || ii.abs_diff(jj) == n - 1) { continue; }
            let o1 = cross(sub(b, a), sub(c, a));
            let o2 = cross(sub(b, a), sub(d, a));
            let o3 = cross(sub(d, c), sub(a, c));
            let o4 = cross(sub(d, c), sub(b, c));
            if ((o1 > 0.0 && o2 < 0.0) || (o1 < 0.0 && o2 > 0.0)) && ((o3 > 0.0 && o4 < 0.0) || (o3 < 0.0 && o4 > 0.0)) { return true; }
        }
    }
    false
}

/// The areas validated and indexed.
pub struct Prep { pub p: Vec<Poly>, pub grid: BTreeMap<(i64, i64), Vec<u32>>, pub bad: [usize; 7], pub by_class: [usize; 10] }

impl Prep {
    pub fn near(&self, b: [f64; 4], out: &mut Vec<u32>) {
        out.clear();
        let (x0, y0) = ((b[0] / GRID_M).floor() as i64, (b[1] / GRID_M).floor() as i64);
        let (x1, y1) = ((b[2] / GRID_M).floor() as i64, (b[3] / GRID_M).floor() as i64);
        if (x1 - x0 + 1) * (y1 - y0 + 1) > 4096 {
            out.extend(0..self.p.len() as u32);
        } else {
            for gx in x0..=x1 { for gy in y0..=y1 { if let Some(v) = self.grid.get(&(gx, gy)) { out.extend_from_slice(v); } } }
            out.sort_unstable();
            out.dedup();
        }
        out.retain(|&i| { let q = &self.p[i as usize].bb; q[0] <= b[2] && b[0] <= q[2] && q[1] <= b[3] && b[1] <= q[3] });
    }
}

/// The areas usable: one record per key (the first in key order), rings closed, every check passed.
pub fn prepare(areas: &[Area]) -> Prep {
    let mut idx: Vec<usize> = (0..areas.len()).collect();
    idx.sort_by_key(|&i| (areas[i].key, i));
    idx.dedup_by_key(|i| areas[*i].key);
    let mut bad = [0usize; 7];
    let mut by_class = [0usize; 10];
    let mut p: Vec<Poly> = Vec::new();
    'next: for i in idx {
        let a = &areas[i];
        let cls = class_of(&a.value);
        if cls == A_NONE { bad[BAD_CLASS] += 1; continue; }
        let mut rings: Vec<(u8, Vec<P2>)> = Vec::with_capacity(a.rings.len());
        let mut npts = 0;
        for (role, r) in &a.rings {
            if r.iter().any(|q| !q[0].is_finite() || !q[1].is_finite()) { bad[BAD_NONFINITE] += 1; continue 'next; }
            let mut r = r.clone();
            if r.len() >= 2 && r[0] != r[r.len() - 1] { r.push(r[0]); }
            r.dedup();
            if r.len() < 4 { bad[BAD_SHORT] += 1; continue 'next; }
            npts += r.len();
            rings.push((*role, r));
        }
        if npts > MAX_POINTS { bad[BAD_LONG] += 1; continue; }
        if !rings.iter().any(|r| r.0 == 0) { bad[BAD_NO_OUTER] += 1; continue; }
        if self_crossing(&rings) { bad[BAD_SELF_X] += 1; continue; }
        let outer: f64 = rings.iter().filter(|r| r.0 == 0).map(|r| absf(ring_area(&r.1))).sum();
        let inner: f64 = rings.iter().filter(|r| r.0 != 0).map(|r| absf(ring_area(&r.1))).sum();
        let area = outer - inner;
        if !(area >= MIN_AREA_M2) { bad[BAD_TINY] += 1; continue; }
        let mut bb = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
        for (_r, r) in &rings { let q = bbox_of(r); bb = [bb[0].min(q[0]), bb[1].min(q[1]), bb[2].max(q[2]), bb[3].max(q[3])]; }
        by_class[cls as usize] += 1;
        p.push(Poly { key: a.key, cls, layer: a.layer, flags: a.flags, rings, bb, area });
    }
    let mut grid: BTreeMap<(i64, i64), Vec<u32>> = BTreeMap::new();
    for (k, q) in p.iter().enumerate() {
        let (x0, y0) = ((q.bb[0] / GRID_M).floor() as i64, (q.bb[1] / GRID_M).floor() as i64);
        let (x1, y1) = ((q.bb[2] / GRID_M).floor() as i64, (q.bb[3] / GRID_M).floor() as i64);
        if (x1 - x0 + 1) * (y1 - y0 + 1) > 4096 { continue; }       // (a giant: found by the full scan only)
        for gx in x0..=x1 { for gy in y0..=y1 { grid.entry((gx, gy)).or_default().push(k as u32); } }
    }
    Prep { p, grid, bad, by_class }
}

// ------------------------------------------------------------------------------------------------- the fit

pub const STEP_M: f64 = 5.0;            // sample stations along a stroke
pub const RAY_M: f64 = 40.0;            // a section's half reach at most
pub const LANE_MIN: f64 = 2.6;
pub const LANE_MAX: f64 = 3.75;
pub const LANE_MAX_FAST: f64 = 3.9;     // motorways and trunks
pub const SHOULDER_MIN: f64 = 0.2;
pub const SHIFT_MAX: f64 = 3.0;
pub const SLOPE: f64 = 0.05;            // m of edge movement per m along the road at most (1:20)
pub const JUNCTION_M: f64 = 12.0;       // m past the carriageway's half width round a junction node: no targets
pub const GAP_M: f64 = 60.0;            // a run of stations without a target this long fades to today's widths
pub const MIN_STATIONS: usize = 4;
pub const MIN_SHARE: f64 = 0.3;
pub const STRUCT_HOLD_M: f64 = 30.0;    // (flat_struct) m round a structure the lane rules' widths are kept

/// The fit's counts (FitStats; the product's JSON "areas_fit").
#[derive(Default, Debug, Clone)]
pub struct FitStats {
    pub strokes: usize,
    pub fitted: usize,
    pub stations: usize,
    pub valid: usize,
    pub no_area: usize,
    pub junction: usize,
    pub fork: usize,
    pub no_edge: usize,
    pub implausible: usize,
    pub outlier: usize,
    pub few: usize,
    pub clamped: usize,
    pub knots: usize,
    pub failed: usize,
}

impl FitStats {
    fn add(&mut self, o: &FitStats) {
        self.stations += o.stations; self.valid += o.valid; self.no_area += o.no_area; self.junction += o.junction;
        self.fork += o.fork; self.no_edge += o.no_edge; self.implausible += o.implausible; self.outlier += o.outlier;
        self.few += o.few; self.clamped += o.clamped;
    }
}

/// The paved union's exit along the ray p + t d (t in (0, RAY_M]) over the polygons cand at level sup: paved = inside
/// a drivable polygon and inside no island. None when it does not leave within RAY_M or p is not paved.
fn exit(prep: &Prep, cand: &[u32], p: P2, d: P2) -> Option<f64> {
    let mut inside: Vec<bool> = Vec::with_capacity(cand.len());
    let mut ev: Vec<(f64, u32)> = Vec::new();
    let q = add(p, scale(d, RAY_M));
    for (ci, &i) in cand.iter().enumerate() {
        let pg = &prep.p[i as usize];
        inside.push(pg.contains(p));
        for (_role, r) in &pg.rings {
            for w in r.windows(2) {
                if let Some((t, _u)) = seg_x(p, q, w[0], w[1]) {
                    if t > 0.0 { ev.push((t * RAY_M, ci as u32)); }
                }
            }
        }
    }
    let paved = |inside: &[bool]| -> bool {
        let mut d = false;
        for (ci, &i) in cand.iter().enumerate() {
            if !inside[ci] { continue; }
            let c = prep.p[i as usize].cls;
            if c == A_ISLAND { return false; }
            if drivable(c) { d = true; }
        }
        d
    };
    if !paved(&inside) { return None; }
    ev.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap().then(a.1.cmp(&b.1)));
    let mut k = 0;
    while k < ev.len() {
        let t = ev[k].0;
        // every crossing within 5 cm of this one at once (two polygons sharing an edge: out of one, into the other)
        while k < ev.len() && ev[k].0 <= t + 0.05 { let ci = ev[k].1 as usize; inside[ci] = !inside[ci]; k += 1; }
        if !paved(&inside) { return Some(t); }
    }
    None
}

fn lane_max(st: &Stroke) -> f64 { if st.cls >= 1 && st.cls <= 2 { LANE_MAX_FAST } else { LANE_MAX } }

/// The fit parameters at a station: lane factor f, extra width of the outer left / right shoulder, shift, strip factor
/// g (BNE_ROADSYS_XSEC's parking, cycle lanes and buffers: 1 unless the mapped width leaves them no room beside the
/// lanes at their least; always 1 on a stroke without strips).
pub type Knot = (f64, [f64; 5]);

/// The parameters that change nothing.
pub const NEUTRAL: [f64; 5] = [1.0, 0.0, 0.0, 0.0, 1.0];

/// The program at s (linear between knots, held past the ends); NEUTRAL without knots.
pub fn knot_at(k: &[Knot], s: f64) -> [f64; 5] {
    if k.is_empty() { return NEUTRAL; }
    let i = k.partition_point(|x| x.0 <= s);
    if i == 0 { return k[0].1; }
    if i >= k.len() { return k[k.len() - 1].1; }
    let (a, b) = (k[i - 1], k[i]);
    let u = if b.0 > a.0 { (s - a.0) / (b.0 - a.0) } else { 0.0 };
    [a.1[0] + (b.1[0] - a.1[0]) * u, a.1[1] + (b.1[1] - a.1[1]) * u, a.1[2] + (b.1[2] - a.1[2]) * u, a.1[3] + (b.1[3] - a.1[3]) * u,
     a.1[4] + (b.1[4] - a.1[4]) * u]
}

/// Does a node end a carriageway at a junction (three or more arms)?
fn junction(net: &Net, n: usize) -> bool { net.nodes[n].arms.len() >= 3 }

/// Every stroke's area program (Stroke.area) where its mapped areas give one (BNE_ROADSYS_AREAS=on).
pub fn fit(net: &mut Net, prep: &Prep) -> FitStats {
    let mut stats = FitStats::default();
    if prep.p.is_empty() { return stats; }
    let mut out: Vec<(usize, Vec<Knot>, Vec<f64>)> = Vec::new();
    for si in 0..net.strokes.len() {
        if net.strokes[si].absorbed { continue; }
        stats.strokes += 1;
        // (a stroke the fit cannot take keeps the lane rules' widths: never a road lost to a polygon)
        let mut one = FitStats::default();
        let got = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| fit_stroke(net, si, prep, &mut one)));
        stats.add(&one);
        let got = match got { Ok(g) => g, Err(_) => { stats.failed += 1; None } };
        if let Some((k, rows)) = got {
            stats.fitted += 1;
            stats.knots += k.len();
            out.push((si, k, rows));
        }
    }
    for (si, k, rows) in out {
        let st = &mut net.strokes[si];
        st.area = k;
        st.stations.extend(rows);
    }
    stats
}

fn fit_stroke(net: &Net, si: usize, prep: &Prep, stats: &mut FitStats) -> Option<(Vec<Knot>, Vec<f64>)> {
    let st = &net.strokes[si];
    let len = st.len();
    if len < 2.0 * STEP_M { return None; }
    let n = (len / STEP_M).floor() as usize;
    let stations: Vec<f64> = (0..=n).map(|k| (k as f64 * STEP_M).min(len)).collect();
    let (p0, p1) = (net.nodes[st.n0].p, net.nodes[st.n1].p);
    let (j0, j1) = (junction(net, st.n0), junction(net, st.n1));
    let mut off: Vec<f64> = Vec::new();
    let mut cand: Vec<u32> = Vec::new();
    // per station: Some((s, mapped left edge, mapped right edge)) or None
    let mut got: Vec<Option<(f64, f64, f64)>> = Vec::with_capacity(stations.len());
    for &s in &stations {
        stats.stations += 1;
        st.offsets_raw(s, None, &mut off);
        let (t0, ts) = (off[0], off[off.len() - 1]);
        let hw = t0.max(-ts);
        let wg = t0 - ts;
        let (p, nrm, _k) = st.row_frame(s, hw);
        if st.dense.iter().any(|&(a, b)| s >= a - 10.0 && s <= b + 10.0) { stats.fork += 1; got.push(None); continue; }
        if net.opts.flat_struct && st.sup.iter().any(|u| u.kind == SUP_STRUCT && s >= u.s0 - STRUCT_HOLD_M && s <= u.s1 + STRUCT_HOLD_M) {
            stats.fork += 1; got.push(None); continue;
        }
        let jr = JUNCTION_M + hw;
        if (j0 && norm(sub(p, p0)) < jr) || (j1 && norm(sub(p, p1)) < jr) { stats.junction += 1; got.push(None); continue; }
        let sup = st.support_at(s).kind;
        let b = [p[0] - RAY_M, p[1] - RAY_M, p[0] + RAY_M, p[1] + RAY_M];
        prep.near(b, &mut cand);
        cand.retain(|&i| prep.p[i as usize].at_level(sup) && (drivable(prep.p[i as usize].cls) || prep.p[i as usize].cls == A_ISLAND));
        if cand.is_empty() { stats.no_area += 1; got.push(None); continue; }
        let tl = exit(prep, &cand, p, nrm);
        let tr = exit(prep, &cand, p, scale(nrm, -1.0));
        let (tl, tr) = match (tl, tr) {
            (Some(a), Some(b)) => (a, b),
            (None, None) if !cand.iter().any(|&i| prep.p[i as usize].contains(p)) => { stats.no_area += 1; got.push(None); continue; }
            _ => { stats.no_edge += 1; got.push(None); continue; }
        };
        let wm = tl + tr;
        // (the lower bound against the width without BNE_ROADSYS_XSEC's strips: the fit squeezes them, so perpendicular
        // parking or a cycle lane the outline leaves out is no reason to drop the station; wx 0 without strips)
        let mut wx = 0.0;
        for (k, sl) in st.slots.iter().enumerate() { if is_strip(sl.kind) { wx += (off[k] - off[k + 1]).max(0.0); } }
        if !(wm >= 2.0 && wm <= 40.0 && wm <= (2.2 * wg).max(wg + 10.0) && wm >= 0.5 * (wg - wx)) { stats.implausible += 1; got.push(None); continue; }
        got.push(Some((s, tl, -tr)));
    }
    // outliers: a station's width or centre far from the median of it and its two valid neighbours each side
    let valid: Vec<usize> = (0..got.len()).filter(|&i| got[i].is_some()).collect();
    let mut keep: Vec<(f64, f64, f64)> = Vec::new();
    for (k, &i) in valid.iter().enumerate() {
        let lo = k.saturating_sub(2);
        let hi = (k + 3).min(valid.len());
        let mut ws: Vec<f64> = valid[lo..hi].iter().map(|&j| { let g = got[j].unwrap(); g.1 - g.2 }).collect();
        let mut cs: Vec<f64> = valid[lo..hi].iter().map(|&j| { let g = got[j].unwrap(); (g.1 + g.2) / 2.0 }).collect();
        ws.sort_by(|a, b| a.partial_cmp(b).unwrap());
        cs.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let (mw, mc) = (ws[ws.len() / 2], cs[cs.len() / 2]);
        let g = got[i].unwrap();
        if absf((g.1 - g.2) - mw) > 0.2 * mw + 0.3 || absf((g.1 + g.2) / 2.0 - mc) > 0.75 { stats.outlier += 1; continue; }
        keep.push(g);
    }
    stats.valid += keep.len();
    if keep.len() < MIN_STATIONS || (keep.len() as f64) < MIN_SHARE * stations.len() as f64 { stats.few += 1; return None; }
    // the parameters at each kept station
    let lmax = lane_max(st);
    let mut knots: Vec<Knot> = Vec::with_capacity(keep.len());
    for &(s, ml, mr) in &keep {
        st.offsets_raw(s, None, &mut off);
        let wg = off[0] - off[off.len() - 1];
        let (mut wl, mut nl, mut wx) = (0.0, 0usize, 0.0);
        for (k, sl) in st.slots.iter().enumerate() {
            if sl.kind == K_LANE { let w = off[k] - off[k + 1]; if w > 1e-6 { wl += w; nl += 1; } }
            if is_strip(sl.kind) { wx += (off[k] - off[k + 1]).max(0.0); }
        }
        let (shl, shr) = outer_shoulders(st, &off);
        let wm = ml - mr;
        let mut f = 1.0;
        let mut el = 0.0;
        let mut er = 0.0;
        let mut g = 1.0;
        if nl > 0 && wl > 1e-3 {
            let per = wl / nl as f64;
            let (flo, fhi) = ((LANE_MIN / per).min(1.0), (lmax / per).max(1.0));
            let want = (wm - (wg - wl)) / wl;
            f = clampf(want, flo, fhi);
            if f != want { stats.clamped += 1; }
            // (BNE_ROADSYS_XSEC) the mapped width is the carriageway's whole: lanes at their least and the outer
            // shoulders at SHOULDER_MIN still too wide, the strips are squeezed into the rest (as xsec.rs squeezes them
            // into a too narrow width:carriageway); none on a stroke without strips (wx 0)
            if wx > 1e-6 && want < flo {
                let sh_min = shl.map(|a| a.min(SHOULDER_MIN)).unwrap_or(0.0) + shr.map(|b| b.min(SHOULDER_MIN)).unwrap_or(0.0);
                let sh_now = shl.unwrap_or(0.0) + shr.unwrap_or(0.0);
                let room = wm - (wg - wl - wx - sh_now + sh_min) - f * wl;
                g = clampf(room / wx, 0.0, 1.0);
            }
        }
        let rem = wm - (wg - wl - wx) - g * wx - f * wl;
        // the rest into the outer shoulders, half each, none under SHOULDER_MIN
        if let (Some(a), Some(b)) = (shl, shr) {
            el = (rem / 2.0).max(SHOULDER_MIN - a);
            er = (rem - el).max(SHOULDER_MIN - b);
        }
        st.offsets_raw(s, Some([f, el, er, 0.0, g]), &mut off);
        let (gl, gr) = (off[0], off[off.len() - 1]);
        let dt = clampf((ml + mr) / 2.0 - (gl + gr) / 2.0, -SHIFT_MAX.min(0.5 * wg), SHIFT_MAX.min(0.5 * wg));
        knots.push((s, [f, el, er, dt, g]));
    }
    // a 3-tap average (the 5 m samples' jitter), then gaps and ends
    let raw = knots.clone();
    for k in 1..knots.len().saturating_sub(1) {
        if raw[k + 1].0 - raw[k - 1].0 > 2.5 * STEP_M { continue; }
        for c in 0..5 { knots[k].1[c] = (raw[k - 1].1[c] + raw[k].1[c] + raw[k + 1].1[c]) / 3.0; }
    }
    let neutral = NEUTRAL;
    let mut prog: Vec<Knot> = Vec::with_capacity(knots.len() + 8);
    let fade = 30.0;
    let first = knots[0].0;
    // before the first kept station: held to a junction's mouth (the zone takes the mapped width), faded elsewhere
    // (a snapped branch end, a fork's window, an open end)
    let hold0 = j0 && st.snap0.is_none();
    if !hold0 && first > 0.0 { prog.push(((first - fade).max(0.0).min(first - 1e-3), neutral)); }
    for k in 0..knots.len() {
        if k > 0 && knots[k].0 - knots[k - 1].0 > GAP_M {
            prog.push((knots[k - 1].0 + fade / 2.0, neutral));
            prog.push((knots[k].0 - fade / 2.0, neutral));
        }
        prog.push(knots[k]);
    }
    let last = knots[knots.len() - 1].0;
    let hold1 = j1 && st.snap1.is_none();
    if !hold1 && last < len { prog.push(((last + fade).min(len).max(last + 1e-3), neutral)); }
    // forks' windows: neutral inside (the snaps and gores keep the lane rules' widths); with flat_struct (M4's decks
    // between today's ground roads, deck.rs) the structures and STRUCT_HOLD_M round them too: a deck keeps the
    // width of the approaches it joins (a fitted deck between unfitted ground roads made steps and cracks at its
    // joints, the gate at Wroclaw's Grunwaldzki bridge)
    let mut hold: Vec<(f64, f64)> = st.dense.clone();
    if net.opts.flat_struct {
        for u in &st.sup { if u.kind == SUP_STRUCT { hold.push((u.s0 - STRUCT_HOLD_M + 10.0, u.s1 + STRUCT_HOLD_M - 10.0)); } }
    }
    for &(a, b) in &hold {
        prog.retain(|x| !(x.0 > a - 10.0 && x.0 < b + 10.0));
        prog.push((a - 10.0, neutral));
        prog.push((b + 10.0, neutral));
    }
    prog.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap());
    prog.dedup_by(|a, b| absf(a.0 - b.0) < 1e-6);
    // slopes: each edge moves at most SLOPE m per m (forward then backward passes on the edge-moving parameters)
    let wl_of = |s: f64, off: &mut Vec<f64>| -> (f64, f64) {
        st.offsets_raw(s, None, off);
        let (mut w, mut x) = (0.0, 0.0);
        for (k, sl) in st.slots.iter().enumerate() {
            if sl.kind == K_LANE { w += (off[k] - off[k + 1]).max(0.0); }
            if is_strip(sl.kind) { x += (off[k] - off[k + 1]).max(0.0); }
        }
        (w.max(1.0), x.max(1.0))
    };
    // (the neutral knots are fixed: a fork's or a deck's window keeps exactly the lane rules' widths, its neighbours
    // bend toward it; a few rounds settle where a fixed knot sits between moved ones)
    let fixed: Vec<bool> = prog.iter().map(|k| k.1 == neutral).collect();
    for pass in 0..8 {
        let idx: Vec<usize> = if pass % 2 == 0 { (1..prog.len()).collect() } else { (0..prog.len().saturating_sub(1)).rev().collect() };
        for k in idx {
            if fixed[k] { continue; }
            let j = if pass % 2 == 0 { k - 1 } else { k + 1 };
            let ds = absf(prog[k].0 - prog[j].0);
            let lim = SLOPE * ds;
            let (wl, wx) = wl_of(prog[k].0, &mut off);
            let flim = 2.0 * lim / wl;
            let glim = 2.0 * lim / wx;
            let pj = prog[j].1;
            let v = &mut prog[k].1;
            v[0] = clampf(v[0], pj[0] - flim, pj[0] + flim);
            for c in 1..4 { v[c] = clampf(v[c], pj[c] - lim, pj[c] + lim); }
            v[4] = clampf(v[4], pj[4] - glim, pj[4] + glim);
        }
    }
    // rows: where the program bends by more than 1 cm (a station rows must include)
    let mut rows = Vec::new();
    for k in 0..prog.len() {
        let bend = if k == 0 || k + 1 == prog.len() { true } else {
            let (a, b, c) = (prog[k - 1], prog[k], prog[k + 1]);
            let u = if c.0 > a.0 { (b.0 - a.0) / (c.0 - a.0) } else { 0.5 };
            (0..5).any(|i| absf(b.1[i] - (a.1[i] + (c.1[i] - a.1[i]) * u)) * if i == 0 || i == 4 { 3.0 } else { 1.0 } > 0.01)
        };
        if bend && prog[k].0 > 0.0 && prog[k].0 < len { rows.push(prog[k].0); }
    }
    Some((prog, rows))
}

/// The outer shoulders' widths at a row's offsets: (left, right), None where the edge slot is not a shoulder.
pub fn outer_shoulders(st: &Stroke, off: &[f64]) -> (Option<f64>, Option<f64>) {
    let n = st.slots.len();
    let first = (0..n).find(|&k| off[k] - off[k + 1] > 1e-6);
    let last = (0..n).rev().find(|&k| off[k] - off[k + 1] > 1e-6);
    let l = first.filter(|&k| st.slots[k].kind == K_SHOULDER).map(|k| off[k] - off[k + 1]);
    let r = last.filter(|&k| st.slots[k].kind == K_SHOULDER).map(|k| off[k] - off[k + 1]);
    (l, r)
}

// ------------------------------------------------------------------------------------------------- the audit

pub const CELL: f64 = 0.25;             // raster cells (m)
pub const DOMAIN_M: f64 = 4.0;          // the evaluation domain: the mapped areas grown this much
pub const EDGE_STEP: f64 = 0.5;
pub const EDGE_MAX: f64 = 10.0;         // a mapped edge farther than this from any generated edge: unmatched
pub const WIDTH_STEP: f64 = 4.0;
pub const WIDTH_END_M: f64 = 12.0;
pub const HIST_BINS: usize = 100;       // 0.1 m bins up to EDGE_MAX (the last one holds the rest)

/// A generated surface triangle: its corners and whether it is hatched paving (a gore: not a lane, for the islands);
/// minus: not road surface (a sidewalk drawn over the terrain's paint: taken out of the generated surface).
#[derive(Clone, Copy)]
pub struct STri { pub p: [P2; 3], pub gore: bool, pub minus: bool }

/// A road centre line of the scene (the width check): its points, nominal width and a tag naming where that width
/// came from, for the per-line width rows (labels: tools' width_labels): the product's strokes "segment id|class|mapped
/// lanes|layout lanes|urban|country|paved width|driving width|one-way"; today's lines (checks.py) "segment id|class|
/// lanes|urban|width|driving width|one-way".
pub struct CLine { pub p: Vec<P2>, pub width: f64, pub tag: String }

const B_G: u8 = 1;      // generated paved
const B_GORE: u8 = 2;   // generated hatched
const B_M: u8 = 4;      // mapped paved (drivable polygons)
const B_I: u8 = 8;      // mapped island
const B_W: u8 = 16;     // mapped walk
const B_Q: u8 = 32;     // in the quad
const B_D: u8 = 64;     // in the evaluation domain
const B_X: u8 = 128;    // mapped "other" (parking, tracks, unknown values): left out of the domain

struct Grid { x0: f64, y0: f64, nx: usize, ny: usize, b: Vec<u8>, poly: Vec<u32> }

impl Grid {
    fn cell(&self, p: P2) -> Option<usize> {
        let fx = ((p[0] - self.x0) / CELL).floor();
        let fy = ((p[1] - self.y0) / CELL).floor();
        if !(fx >= 0.0 && fy >= 0.0 && fx < self.nx as f64 && fy < self.ny as f64) { return None; }
        Some(fy as usize * self.nx + fx as usize)
    }
    fn has(&self, p: P2, bit: u8) -> bool { self.cell(p).map(|c| self.b[c] & bit != 0).unwrap_or(false) }
    fn centre(&self, c: usize) -> P2 { [self.x0 + ((c % self.nx) as f64 + 0.5) * CELL, self.y0 + ((c / self.nx) as f64 + 0.5) * CELL] }
    /// Every cell whose centre lies inside the rings (even-odd), each once.
    fn fill_rings(&self, rings: &[&[P2]], f: &mut dyn FnMut(usize)) {
        let bb = rings.iter().fold([f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY], |b, r| {
            let q = bbox_of(r);
            [b[0].min(q[0]), b[1].min(q[1]), b[2].max(q[2]), b[3].max(q[3])]
        });
        let j0 = (((bb[1] - self.y0) / CELL - 0.5).floor().max(0.0)) as usize;
        let j1f = ((bb[3] - self.y0) / CELL - 0.5).ceil();
        if j1f < 0.0 { return; }
        let j1 = (j1f as usize).min(self.ny.saturating_sub(1));
        let mut xs: Vec<f64> = Vec::new();
        for j in j0..=j1 {
            let y = self.y0 + (j as f64 + 0.5) * CELL;
            xs.clear();
            for r in rings {
                for w in r.windows(2) {
                    let (a, b) = (w[0], w[1]);
                    if (a[1] > y) != (b[1] > y) { xs.push(a[0] + (y - a[1]) / (b[1] - a[1]) * (b[0] - a[0])); }
                }
            }
            xs.sort_by(|a, b| a.partial_cmp(b).unwrap());
            for pr in xs.chunks(2) {
                if pr.len() < 2 { break; }
                // cells with centre x in [pr0, pr1)
                let i0 = ((pr[0] - self.x0) / CELL - 0.5).ceil().max(0.0);
                let i1 = ((pr[1] - self.x0) / CELL - 0.5).ceil().min(self.nx as f64);
                if i1 <= i0 { continue; }
                for i in i0 as usize..i1 as usize { f(j * self.nx + i); }
            }
        }
    }
    fn fill_tri(&mut self, t: &[P2; 3], bits: u8) {
        let bb = bbox_of(t);
        let i0 = (((bb[0] - self.x0) / CELL - 0.5).ceil().max(0.0)) as i64;
        let i1 = (((bb[2] - self.x0) / CELL - 0.5).floor()).min(self.nx as f64 - 1.0) as i64;
        let j0 = (((bb[1] - self.y0) / CELL - 0.5).ceil().max(0.0)) as i64;
        let j1 = (((bb[3] - self.y0) / CELL - 0.5).floor()).min(self.ny as f64 - 1.0) as i64;
        if i1 < i0 || j1 < j0 { return; }
        let o = cross(sub(t[1], t[0]), sub(t[2], t[0]));
        if o == 0.0 { return; }
        for j in j0..=j1 {
            for i in i0..=i1 {
                let c = [self.x0 + (i as f64 + 0.5) * CELL, self.y0 + (j as f64 + 0.5) * CELL];
                let a = cross(sub(t[1], t[0]), sub(c, t[0]));
                let b = cross(sub(t[2], t[1]), sub(c, t[1]));
                let d = cross(sub(t[0], t[2]), sub(c, t[2]));
                let ins = if o > 0.0 { a >= 0.0 && b >= 0.0 && d >= 0.0 } else { a <= 0.0 && b <= 0.0 && d <= 0.0 };
                if ins { self.b[j as usize * self.nx + i as usize] |= bits; }
            }
        }
    }
}

/// Squared Euclidean distance (cells) to the nearest seed cell (Felzenszwalb-Huttenlocher, exact; integer inputs,
/// so the same values on any platform).
fn edt(nx: usize, ny: usize, seed: &[bool]) -> Vec<f64> {
    let inf = 1e20;
    let mut g: Vec<f64> = seed.iter().map(|&s| if s { 0.0 } else { inf }).collect();
    let mut f = vec![0.0; nx.max(ny)];
    let mut d = vec![0.0; nx.max(ny)];
    let mut v = vec![0usize; nx.max(ny)];
    let mut z = vec![0.0; nx.max(ny) + 1];
    let pass = |f: &[f64], n: usize, d: &mut [f64], v: &mut [usize], z: &mut [f64]| {
        // the lower envelope of the parabolas (q - p)^2 + f[p] (Felzenszwalb-Huttenlocher)
        let mut k = 0usize;
        v[0] = 0;
        z[0] = -inf;
        z[1] = inf;
        for q in 1..n {
            let sec = |p: usize| ((f[q] + (q * q) as f64) - (f[p] + (p * p) as f64)) / (2.0 * q as f64 - 2.0 * p as f64);
            let mut s = sec(v[k]);
            while s <= z[k] { k -= 1; s = sec(v[k]); }
            k += 1;
            v[k] = q;
            z[k] = s;
            z[k + 1] = inf;
        }
        k = 0;
        for q in 0..n {
            while z[k + 1] < q as f64 { k += 1; }
            let p = v[k];
            let dq = q as f64 - p as f64;
            d[q] = dq * dq + f[p];
        }
    };
    for x in 0..nx {
        for y in 0..ny { f[y] = g[y * nx + x]; }
        pass(&f, ny, &mut d, &mut v, &mut z);
        for y in 0..ny { g[y * nx + x] = d[y]; }
    }
    for y in 0..ny {
        f[..nx].copy_from_slice(&g[y * nx..(y + 1) * nx]);
        pass(&f, nx, &mut d, &mut v, &mut z);
        g[y * nx..(y + 1) * nx].copy_from_slice(&d[..nx]);
    }
    g
}

#[derive(Clone, Default)]
pub struct Hist { pub n: u64, pub bins: Vec<u64>, pub signed: f64, pub over: u64 }

impl Hist {
    fn new() -> Hist { Hist { n: 0, bins: vec![0; HIST_BINS], signed: 0.0, over: 0 } }
    fn add(&mut self, v: f64) {
        let a = absf(v);
        if a > EDGE_MAX { self.over += 1; return; }
        self.n += 1;
        self.signed += v;
        let b = ((a / EDGE_MAX * HIST_BINS as f64).floor() as usize).min(HIST_BINS - 1);
        self.bins[b] += 1;
    }
    /// The q-quantile (bin upper edge, m) of the values within EDGE_MAX.
    pub fn q(&self, q: f64) -> f64 {
        if self.n == 0 { return 0.0; }
        let want = (q * self.n as f64).ceil().max(1.0) as u64;
        let mut acc = 0;
        for (b, &c) in self.bins.iter().enumerate() {
            acc += c;
            if acc >= want { return (b + 1) as f64 * EDGE_MAX / HIST_BINS as f64; }
        }
        EDGE_MAX
    }
    fn json(&self, w: &mut String) {
        let _ = write!(w, "{{\"n\":{},\"over\":{},\"p50\":{:.2},\"p95\":{:.2},\"mean_signed\":{:.3},\"bins\":[", self.n, self.over, self.q(0.5), self.q(0.95),
                       if self.n > 0 { self.signed / self.n as f64 } else { 0.0 });
        for (i, b) in self.bins.iter().enumerate() { let _ = write!(w, "{}{}", if i > 0 { "," } else { "" }, b); }
        w.push_str("]}");
    }
}

/// The audit's sums (additive over tiles) and histograms.
pub struct Audit {
    pub polys: usize,
    pub bad: [usize; 7],
    pub by_class: [usize; 10],
    pub eval_m2: f64,
    pub mapped_m2: f64,
    pub gen_m2: f64,
    pub inter_m2: f64,
    pub union_m2: f64,
    pub missing_m2: f64,
    pub extra_m2: f64,
    pub edge: Vec<Hist>,            // per class (index A_*), and [0]: all
    pub width: Vec<Hist>,
    pub islands: usize,
    pub islands_missed: usize,
    pub island_off: Hist,
    pub extra_splits: usize,
    pub extra_split_m2: f64,
    pub lines: usize,
    pub lines_mapped: usize,
    /// per centre line with mapped stations: (its tag, the mapped class most of its stations lie in, our chord's
    /// median, the mapped chord's median, its nominal width, stations): the material for width priors by class and
    /// region where nothing is mapped
    pub line_rows: Vec<(String, u8, f64, f64, f64, usize)>,
}

impl Audit {
    pub fn json(&self) -> String {
        let mut w = String::new();
        let iou = if self.union_m2 > 0.0 { self.inter_m2 / self.union_m2 } else { 0.0 };
        let _ = write!(w, "{{\"format\":{},\"polygons\":{},\"bad\":{{", AREA_FORMAT, self.polys);
        for (i, n) in BAD_NAMES.iter().enumerate() { let _ = write!(w, "{}\"{}\":{}", if i > 0 { "," } else { "" }, n, self.bad[i]); }
        w.push_str("},\"by_class\":{");
        for i in 1..10 { let _ = write!(w, "{}\"{}\":{}", if i > 1 { "," } else { "" }, CLASS_NAMES[i], self.by_class[i]); }
        let _ = write!(w, "}},\"eval_m2\":{:.2},\"mapped_m2\":{:.2},\"gen_m2\":{:.2},\"inter_m2\":{:.2},\"union_m2\":{:.2},\"missing_m2\":{:.2},\"extra_m2\":{:.2},\"iou\":{:.4},",
                       self.eval_m2, self.mapped_m2, self.gen_m2, self.inter_m2, self.union_m2, self.missing_m2, self.extra_m2, iou);
        for (name, h) in [("edge", &self.edge), ("width", &self.width)] {
            let _ = write!(w, "\"{}\":{{", name);
            for i in 0..7 {
                let _ = write!(w, "{}\"{}\":", if i > 0 { "," } else { "" }, if i == 0 { "all" } else { CLASS_NAMES[i] });
                h[i].json(&mut w);
            }
            w.push_str("},");
        }
        let _ = write!(w, "\"islands\":{},\"islands_missed\":{},\"island_offset\":", self.islands, self.islands_missed);
        self.island_off.json(&mut w);
        let _ = write!(w, ",\"extra_splits\":{},\"extra_split_m2\":{:.2},\"lines\":{},\"lines_mapped\":{},\"line_rows\":[", self.extra_splits, self.extra_split_m2, self.lines, self.lines_mapped);
        for (i, r) in self.line_rows.iter().enumerate() {
            let tag: String = r.0.chars().filter(|c| *c != '"' && *c != '\\' && !c.is_control()).collect();
            let _ = write!(w, "{}[\"{}\",\"{}\",{:.2},{:.2},{:.2},{}]", if i > 0 { "," } else { "" }, tag, CLASS_NAMES[r.1 as usize], r.2, r.3, r.4, r.5);
        }
        w.push_str("]}");
        w
    }
}

/// The audit of a generated surface (tris, in the frame of the areas) inside quad against the mapped areas, with the
/// scene's centre lines for the width check. See the module docs; the metrics:
/// - domain: the quad's cells within DOMAIN_M of a mapped drivable or island polygon, less the cells round centre
///   lines that are mostly unmapped (their own width + 2 m): the roads someone mapped areas for;
/// - IoU, missing and extra m2 of the mapped paved area (drivable less islands) against the generated in the domain;
/// - edge: each mapped carriageway edge (a ring's sample whose two sides differ in mapped pavement: shared edges
///   between abutting polygons are not edges) to the nearest generated edge, signed + where the generated surface
///   reaches past it; per class of the paved side's polygon; over EDGE_MAX unmatched;
/// - width: across each mapped centre line every WIDTH_STEP (not within WIDTH_END_M of its ends), the generated chord
///   minus the mapped one (the paved run through the centre), stations whose mapped chord is a junction area's
///   (over 2.5 x the line's width and 12 m more) left out;
/// - islands: each island polygon and hole of a drivable one (over 1 m2 in the quad): missed when the generated
///   lanes cover over half of it (a generated gore over it is a kept split); kept ones' offset: the island's centroid
///   to the centroid of the generated non-lane cells round it (its box grown 2 m);
/// - extra splits: holes in the generated surface inside mapped pavement, enclosed by generated surface (>= 2 m2).
pub fn audit(prep: &Prep, quad: [P2; 4], tris: &[STri], lines: &[CLine]) -> Audit {
    let qb = bbox_of(&quad);
    let (x0, y0) = (((qb[0] - 2.0) / CELL).floor() * CELL, ((qb[1] - 2.0) / CELL).floor() * CELL);
    let nx = (((qb[2] + 2.0 - x0) / CELL).ceil() as usize).max(1);
    let ny = (((qb[3] + 2.0 - y0) / CELL).ceil() as usize).max(1);
    let mut a = Audit { polys: prep.p.len(), bad: prep.bad, by_class: prep.by_class, eval_m2: 0.0, mapped_m2: 0.0, gen_m2: 0.0, inter_m2: 0.0,
                        union_m2: 0.0, missing_m2: 0.0, extra_m2: 0.0, edge: (0..7).map(|_| Hist::new()).collect(),
                        width: (0..7).map(|_| Hist::new()).collect(), islands: 0, islands_missed: 0, island_off: Hist::new(),
                        extra_splits: 0, extra_split_m2: 0.0, lines: 0, lines_mapped: 0, line_rows: Vec::new() };
    if nx * ny > 40_000_000 { return a; }
    let mut g = Grid { x0, y0, nx, ny, b: vec![0; nx * ny], poly: vec![u32::MAX; nx * ny] };
    // the quad
    let qr: Vec<P2> = vec![quad[0], quad[1], quad[2], quad[3], quad[0]];
    let mut cells: Vec<usize> = Vec::new();
    g.fill_rings(&[&qr], &mut |c| cells.push(c));
    for &c in &cells { g.b[c] |= B_Q; }
    // the mapped areas near the quad
    let mut cand = Vec::new();
    prep.near([qb[0] - 2.0, qb[1] - 2.0, qb[2] + 2.0, qb[3] + 2.0], &mut cand);
    for &i in &cand {
        let p = &prep.p[i as usize];
        let rings: Vec<&[P2]> = p.rings.iter().map(|r| r.1.as_slice()).collect();
        cells.clear();
        g.fill_rings(&rings, &mut |c| cells.push(c));
        let bit = if drivable(p.cls) { B_M } else if p.cls == A_ISLAND { B_I } else if p.cls == A_WALK { B_W } else { B_X };
        for &c in &cells {
            g.b[c] |= bit;
            if drivable(p.cls) { g.poly[c] = i; }
        }
    }
    // the generated surface
    for t in tris { if !t.minus { g.fill_tri(&t.p, if t.gore { B_G | B_GORE } else { B_G }); } }
    if tris.iter().any(|t| t.minus) {
        let mut m = Grid { x0, y0, nx, ny, b: vec![0; nx * ny], poly: Vec::new() };
        for t in tris { if t.minus { m.fill_tri(&t.p, 1); } }
        for c in 0..nx * ny { if m.b[c] != 0 { g.b[c] &= !(B_G | B_GORE); } }
    }
    // the domain: mapped drivable / island grown DOMAIN_M (a square), less the unmapped roads' bands
    let r = (DOMAIN_M / CELL).round() as usize;
    let base: Vec<bool> = g.b.iter().map(|&b| b & (B_M | B_I) != 0).collect();
    let grown = dilate(nx, ny, &base, r);
    for c in 0..nx * ny { if grown[c] && g.b[c] & B_Q != 0 && (g.b[c] & B_X == 0 || g.b[c] & B_M != 0) { g.b[c] |= B_D; } }
    let mapped_at = |g: &Grid, p: P2| g.has(p, B_M) && !g.has(p, B_I);
    for l in lines {
        let ln = Line::new(l.p.clone());
        let len = ln.len();
        if len < 1.0 { continue; }
        let n = (len / 2.0).ceil() as usize;
        let (mut inm, mut tot) = (0, 0);
        for k in 0..=n {
            let (p, _t) = ln.at(len * k as f64 / n as f64);
            if g.cell(p).map(|c| g.b[c] & B_Q != 0).unwrap_or(false) { tot += 1; if mapped_at(&g, p) || g.has(p, B_I) { inm += 1; } }
        }
        if tot == 0 { continue; }
        if 2 * inm < tot {
            // mostly unmapped: its band leaves the domain
            let hw = l.width / 2.0 + 2.0;
            for w in l.p.windows(2) {
                let bb = [w[0][0].min(w[1][0]) - hw, w[0][1].min(w[1][1]) - hw, w[0][0].max(w[1][0]) + hw, w[0][1].max(w[1][1]) + hw];
                let i0 = (((bb[0] - x0) / CELL).floor().max(0.0)) as usize;
                let i1 = (((bb[2] - x0) / CELL).ceil().min(nx as f64)) as usize;
                let j0 = (((bb[1] - y0) / CELL).floor().max(0.0)) as usize;
                let j1 = (((bb[3] - y0) / CELL).ceil().min(ny as f64)) as usize;
                let (a0, b0) = (w[0], w[1]);
                let ab = sub(b0, a0);
                let l2 = dot(ab, ab);
                for j in j0..j1 {
                    for i in i0..i1 {
                        let c = j * nx + i;
                        if g.b[c] & B_D == 0 { continue; }
                        let q = g.centre(c);
                        let u = if l2 > 0.0 { clampf(dot(sub(q, a0), ab) / l2, 0.0, 1.0) } else { 0.0 };
                        if norm(sub(q, lerp(a0, b0, u))) <= hw { g.b[c] &= !B_D; }
                    }
                }
            }
        }
    }
    // areas
    let ca = CELL * CELL;
    for c in 0..nx * ny {
        let b = g.b[c];
        if b & B_D == 0 { continue; }
        a.eval_m2 += ca;
        let m = b & B_M != 0 && b & B_I == 0;
        let gg = b & B_G != 0;
        if m { a.mapped_m2 += ca; }
        if gg { a.gen_m2 += ca; }
        if m && gg { a.inter_m2 += ca; }
        if m || gg { a.union_m2 += ca; }
        if m && !gg { a.missing_m2 += ca; }
        if gg && !m { a.extra_m2 += ca; }
    }
    // edges: distance to the generated surface's boundary cells
    let mut seed = vec![false; nx * ny];
    for j in 0..ny {
        for i in 0..nx {
            let c = j * nx + i;
            if g.b[c] & B_G == 0 { continue; }
            let edge = i == 0 || j == 0 || i + 1 == nx || j + 1 == ny || g.b[c - 1] & B_G == 0 || g.b[c + 1] & B_G == 0
                || g.b[c - nx] & B_G == 0 || g.b[c + nx] & B_G == 0;
            if edge { seed[c] = true; }
        }
    }
    let dist = edt(nx, ny, &seed);
    for &i in &cand {
        let p = &prep.p[i as usize];
        if !(drivable(p.cls) || p.cls == A_ISLAND) { continue; }
        for (_role, r) in &p.rings {
            for w in r.windows(2) {
                let (s0, s1) = (w[0], w[1]);
                let d = sub(s1, s0);
                let l = norm(d);
                if l < 1e-6 { continue; }
                let u = scale(d, 1.0 / l);
                let nrm = [-u[1], u[0]];
                let n = (l / EDGE_STEP).ceil() as usize;
                for k in 0..n {
                    let q = add(s0, scale(d, (k as f64 + 0.5) / n as f64));
                    let Some(c) = g.cell(q) else { continue };
                    if g.b[c] & B_D == 0 { continue; }
                    let pa = add(q, scale(nrm, 0.5));
                    let pb = sub(q, scale(nrm, 0.5));
                    let (ma, mb) = (mapped_at(&g, pa), mapped_at(&g, pb));
                    if ma == mb { continue; }
                    let paved = if ma { pa } else { pb };
                    let cls = g.cell(paved).map(|cc| g.poly[cc]).filter(|&x| x != u32::MAX).map(|x| prep.p[x as usize].cls).unwrap_or(p.cls);
                    let dd = dist[c].sqrt() * CELL;
                    let v = if g.b[c] & B_G != 0 { dd } else { -dd };
                    a.edge[0].add(v);
                    if drivable(cls) { a.edge[cls as usize].add(v); }
                }
            }
        }
    }
    // widths across the centre lines
    for l in lines {
        let ln = Line::new(l.p.clone());
        let len = ln.len();
        a.lines += 1;
        if len < 2.0 * WIDTH_END_M + 1.0 { continue; }
        let mut any = false;
        let (mut gws, mut mws, mut cls_n) = (Vec::new(), Vec::new(), [0usize; 10]);
        let n = ((len - 2.0 * WIDTH_END_M) / WIDTH_STEP).floor() as usize;
        for k in 0..=n {
            let s = WIDTH_END_M + k as f64 * WIDTH_STEP;
            let (p, t) = ln.at(s);
            let Some(c) = g.cell(p) else { continue };
            if g.b[c] & B_Q == 0 || g.b[c] & B_D == 0 || !mapped_at(&g, p) { continue; }
            let nrm = [-t[1], t[0]];
            let chord = |g: &Grid, bit: u8, mapped: bool| -> f64 {
                let mut w = 0.0;
                for sg in [1.0, -1.0] {
                    let mut x = 0.0;
                    while x < RAY_M {
                        let q = add(p, scale(nrm, sg * (x + 0.05)));
                        let ok = if mapped { mapped_at(g, q) } else { g.has(q, bit) };
                        if !ok { break; }
                        x += 0.1;
                    }
                    w += x;
                }
                w
            };
            let mw = chord(&g, B_M, true);
            if mw > (2.5 * l.width).max(l.width + 12.0) || mw >= 2.0 * RAY_M - 0.2 { continue; }
            let gw = if g.b[c] & B_G != 0 { chord(&g, B_G, false) } else { 0.0 };
            let cls = if g.poly[c] != u32::MAX { prep.p[g.poly[c] as usize].cls } else { A_NONE };
            a.width[0].add(gw - mw);
            if drivable(cls) { a.width[cls as usize].add(gw - mw); }
            gws.push(gw);
            mws.push(mw);
            cls_n[cls as usize] += 1;
            any = true;
        }
        if any {
            a.lines_mapped += 1;
            let med = |v: &mut Vec<f64>| { v.sort_by(|x, y| x.partial_cmp(y).unwrap()); v[v.len() / 2] };
            let cls = (0..10).max_by_key(|&c| (cls_n[c], std::cmp::Reverse(c))).unwrap_or(0) as u8;
            let k = gws.len();
            a.line_rows.push((l.tag.clone(), cls, med(&mut gws), med(&mut mws), l.width, k));
        }
    }
    // islands
    for &i in &cand {
        let p = &prep.p[i as usize];
        let objs: Vec<Vec<&[P2]>> = if p.cls == A_ISLAND { vec![p.rings.iter().map(|r| r.1.as_slice()).collect()] }
            else if drivable(p.cls) { p.rings.iter().filter(|r| r.0 != 0).map(|r| vec![r.1.as_slice()]).collect() } else { Vec::new() };
        for rings in objs {
            cells.clear();
            g.fill_rings(&rings, &mut |c| cells.push(c));
            cells.retain(|&c| g.b[c] & B_Q != 0);
            if (cells.len() as f64) * ca < 1.0 { continue; }
            a.islands += 1;
            let lane = |b: u8| b & B_G != 0 && b & B_GORE == 0;
            let cov = cells.iter().filter(|&&c| lane(g.b[c])).count();
            if 2 * cov > cells.len() { a.islands_missed += 1; continue; }
            let mut cx = [0.0, 0.0];
            for &c in &cells { let q = g.centre(c); cx = add(cx, q); }
            cx = scale(cx, 1.0 / cells.len() as f64);
            let bb = rings.iter().fold([f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY], |b, r| {
                let q = bbox_of(r);
                [b[0].min(q[0]), b[1].min(q[1]), b[2].max(q[2]), b[3].max(q[3])]
            });
            let (mut gx, mut gn) = ([0.0, 0.0], 0usize);
            let i0 = (((bb[0] - 2.0 - x0) / CELL).floor().max(0.0)) as usize;
            let i1 = (((bb[2] + 2.0 - x0) / CELL).ceil().min(nx as f64)) as usize;
            let j0 = (((bb[1] - 2.0 - y0) / CELL).floor().max(0.0)) as usize;
            let j1 = (((bb[3] + 2.0 - y0) / CELL).ceil().min(ny as f64)) as usize;
            for jj in j0..j1 { for ii in i0..i1 {
                let c = jj * nx + ii;
                if !lane(g.b[c]) && g.b[c] & (B_M | B_I) != 0 { gx = add(gx, g.centre(c)); gn += 1; }
            } }
            if gn == 0 { continue; }
            let d = norm(sub(scale(gx, 1.0 / gn as f64), cx));
            a.island_off.add(d);
        }
    }
    // extra splits: 4-connected holes of the generated surface in mapped pavement, enclosed by generated surface
    let mut seen = vec![false; nx * ny];
    let mut stack: Vec<usize> = Vec::new();
    for c0 in 0..nx * ny {
        let b = g.b[c0];
        if seen[c0] || b & B_D == 0 || b & B_G != 0 || !(b & B_M != 0 && b & B_I == 0) { continue; }
        let mut n = 0usize;
        let mut closed = true;
        stack.push(c0);
        seen[c0] = true;
        while let Some(c) = stack.pop() {
            n += 1;
            let (i, j) = (c % nx, c / nx);
            let nb = [(i > 0).then(|| c - 1), (i + 1 < nx).then(|| c + 1), (j > 0).then(|| c - nx), (j + 1 < ny).then(|| c + nx)];
            if nb.iter().any(|x| x.is_none()) { closed = false; }
            for x in nb.into_iter().flatten() {
                let bx = g.b[x];
                if bx & B_G != 0 { continue; }
                if bx & B_Q == 0 { closed = false; continue; }
                if !(bx & B_M != 0 && bx & B_I == 0) { closed = false; continue; }
                if !seen[x] { seen[x] = true; stack.push(x); }
            }
        }
        if closed && n as f64 * ca >= 2.0 { a.extra_splits += 1; a.extra_split_m2 += n as f64 * ca; }
    }
    a
}

/// Chebyshev dilation by r cells (separable running max).
fn dilate(nx: usize, ny: usize, m: &[bool], r: usize) -> Vec<bool> {
    let mut h = vec![false; nx * ny];
    for j in 0..ny {
        let row = &m[j * nx..(j + 1) * nx];
        let mut last: i64 = -1_000_000;
        // forward: nearest set at or before i; backward: at or after
        let mut fw = vec![i64::MIN / 2; nx];
        for i in 0..nx { if row[i] { last = i as i64; } fw[i] = last; }
        let mut next: i64 = 1_000_000_000;
        for i in (0..nx).rev() {
            if row[i] { next = i as i64; }
            let ii = i as i64;
            h[j * nx + i] = ii - fw[i] <= r as i64 || next - ii <= r as i64;
        }
    }
    let mut out = vec![false; nx * ny];
    for i in 0..nx {
        let mut last: i64 = -1_000_000;
        let mut fw = vec![0i64; ny];
        for j in 0..ny { if h[j * nx + i] { last = j as i64; } fw[j] = last; }
        let mut next: i64 = 1_000_000_000;
        for j in (0..ny).rev() {
            if h[j * nx + i] { next = j as i64; }
            let jj = j as i64;
            out[j * nx + i] = jj - fw[j] <= r as i64 || next - jj <= r as i64;
        }
    }
    out
}

// ----------------------------------------------------------------------------- the CLI's audit input (default path)

/// tools/widecorpus/checks.py's areas input ("RSAREA01"; "RSAREA02": each line first its tag, u32 + UTF-8; little endian): quad 8 x f64; u32 triangles, each u8 code (0
/// paved, 1 hatched, 2 not road surface: a sidewalk over the paint) + 6 x f64 (x, y of 3 corners); u32 lines, each f64 width, u32 points, points x 2 f64; u32 areas, each u64 key,
/// u32 + value UTF-8, i64 layer, u8 flags, u32 rings, each u8 role, u32 points, points x 2 f64. Output: the audit's
/// JSON.
pub fn run_input(b: &[u8]) -> Result<String, String> {
    struct R<'a> { b: &'a [u8], i: usize }
    impl R<'_> {
        fn take(&mut self, n: usize) -> Result<&[u8], String> {
            if self.i + n > self.b.len() { return Err("areas input: truncated".into()); }
            let s = &self.b[self.i..self.i + n];
            self.i += n;
            Ok(s)
        }
        fn u8(&mut self) -> Result<u8, String> { Ok(self.take(1)?[0]) }
        fn u32(&mut self) -> Result<usize, String> { Ok(u32::from_le_bytes(self.take(4)?.try_into().unwrap()) as usize) }
        fn u64(&mut self) -> Result<u64, String> { Ok(u64::from_le_bytes(self.take(8)?.try_into().unwrap())) }
        fn f64(&mut self) -> Result<f64, String> { Ok(f64::from_le_bytes(self.take(8)?.try_into().unwrap())) }
        fn count(&mut self, each: usize) -> Result<usize, String> {
            let n = self.u32()?;
            if n.saturating_mul(each) > self.b.len() - self.i { return Err("areas input: a count past its bytes".into()); }
            Ok(n)
        }
    }
    let mut r = R { b, i: 0 };
    let magic = r.take(8)?.to_vec();
    let v2 = magic == b"RSAREA02";
    if !v2 && magic != b"RSAREA01" { return Err("areas input: bad magic".into()); }
    let mut quad = [[0.0; 2]; 4];
    for q in quad.iter_mut() { q[0] = r.f64()?; q[1] = r.f64()?; }
    let nt = r.count(49)?;
    let mut tris = Vec::with_capacity(nt);
    for _ in 0..nt {
        let code = r.u8()?;
        let mut p = [[0.0; 2]; 3];
        for v in p.iter_mut() { v[0] = r.f64()?; v[1] = r.f64()?; }
        if p.iter().any(|v| !v[0].is_finite() || !v[1].is_finite()) { return Err("areas input: non-finite vertex".into()); }
        tris.push(STri { p, gore: code == 1, minus: code == 2 });
    }
    let nl = r.count(12)?;
    let mut lines = Vec::with_capacity(nl);
    for _ in 0..nl {
        let tag = if v2 { let n = r.count(1)?; String::from_utf8_lossy(r.take(n)?).into_owned() } else { String::new() };
        let width = r.f64()?;
        let np = r.count(16)?;
        let mut p = Vec::with_capacity(np);
        for _ in 0..np { p.push([r.f64()?, r.f64()?]); }
        if p.iter().any(|v: &P2| !v[0].is_finite() || !v[1].is_finite()) || !width.is_finite() { return Err("areas input: non-finite line".into()); }
        lines.push(CLine { p, width, tag: tag.clone() });
    }
    let na = r.count(25)?;
    let mut areas = Vec::with_capacity(na);
    for _ in 0..na {
        let key = r.u64()?;
        let nv = r.count(1)?;
        let value = String::from_utf8_lossy(r.take(nv)?).into_owned();
        let layer = r.u64()? as i64;
        let flags = r.u8()?;
        let nr = r.count(5)?;
        let mut rings = Vec::with_capacity(nr);
        for _ in 0..nr {
            let role = r.u8()?;
            let np = r.count(16)?;
            let mut p = Vec::with_capacity(np);
            for _ in 0..np { p.push([r.f64()?, r.f64()?]); }
            rings.push((role, p));
        }
        areas.push(Area { key, value, layer, flags, links: Vec::new(), rings });
    }
    let prep = prepare(&areas);
    Ok(audit(&prep, quad, &tris, &lines).json())
}

/// The product's faces as the audit's triangles: every face in the quad's box (kinds lane, shoulder, zone paved;
/// gores hatched).
pub fn faces_of(mesh: &crate::part::Mesh, quad: &[P2; 4]) -> Vec<STri> {
    let b = bbox_of(quad);
    let mut out = Vec::new();
    for (t, g) in mesh.t.iter().zip(&mesh.tag) {
        let p = [mesh.v[t[0] as usize], mesh.v[t[1] as usize], mesh.v[t[2] as usize]];
        let q = [[p[0][0] as f64 / 1000.0, p[0][1] as f64 / 1000.0], [p[1][0] as f64 / 1000.0, p[1][1] as f64 / 1000.0], [p[2][0] as f64 / 1000.0, p[2][1] as f64 / 1000.0]];
        let tb = bbox_of(&q);
        if tb[2] < b[0] - 1.0 || tb[0] > b[2] + 1.0 || tb[3] < b[1] - 1.0 || tb[1] > b[3] + 1.0 { continue; }
        out.push(STri { p: q, gore: g.kind == K_GORE, minus: false });
    }
    out
}

/// The strokes' centre lines (with their carriageway width at the middle) for the width check.
pub fn lines_of(net: &Net, d: Option<&[bool]>) -> Vec<CLine> {
    let mut out = Vec::new();
    let mut off = Vec::new();
    for (si, st) in net.strokes.iter().enumerate() {
        if st.absorbed || d.map(|v| !v[si]).unwrap_or(false) { continue; }
        st.offsets(st.len() / 2.0, &mut off);
        // (the stroke's middle part's map segment: what the labels name)
        let mid = st.len() / 2.0;
        let part = st.parts.iter().find(|q| mid >= q.s0 && mid <= q.s1).unwrap_or(&st.parts[0]);
        let seg = &net.segs[net.edges[part.edge].seg];
        let tag = format!("{}|{}|{}|{}|{}|{}|{:.2}|{:.2}|{}", seg.fid.split('|').next().unwrap_or(""), st.cls_s, seg.lanes,
                          seg.inferred, if seg.urban { 1 } else { 0 }, seg.code, seg.pave, seg.drive, if st.oneway { 1 } else { 0 });
        out.push(CLine { p: st.line.p.clone(), width: off[0] - off[off.len() - 1], tag });
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rect(key: u64, v: &str, x0: f64, y0: f64, x1: f64, y1: f64) -> Area {
        Area { key, value: v.into(), rings: vec![(0, vec![[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]])], ..Default::default() }
    }

    #[test]
    fn classes() {
        assert_eq!(class_of("Primary "), A_MAJOR);
        assert_eq!(class_of("traffic_island"), A_ISLAND);
        assert_eq!(class_of("prohibited"), A_PAVED);
        assert_eq!(class_of("footway"), A_WALK);
        assert_eq!(class_of("yes"), A_OTHER);
        assert_eq!(class_of(" "), A_NONE);
    }

    #[test]
    fn bad_polygons_are_left_out() {
        let bow = Area { key: 2, value: "residential".into(), rings: vec![(0, vec![[0.0, 0.0], [10.0, 10.0], [10.0, 0.0], [0.0, 10.0], [0.0, 0.0]])], ..Default::default() };
        let tiny = rect(4, "residential", 0.0, 0.0, 0.5, 0.5);
        let open = Area { key: 6, value: "residential".into(), rings: vec![(0, vec![[0.0, 0.0], [1.0, 0.0]])], ..Default::default() };
        let nan = Area { key: 8, value: "residential".into(), rings: vec![(0, vec![[0.0, 0.0], [f64::NAN, 0.0], [1.0, 1.0], [0.0, 0.0]])], ..Default::default() };
        let ok = rect(10, "residential", 0.0, 0.0, 10.0, 5.0);
        let p = prepare(&[bow, tiny, open, nan, ok.clone(), ok]);
        assert_eq!(p.p.len(), 1);
        assert_eq!((p.bad[BAD_SELF_X], p.bad[BAD_TINY], p.bad[BAD_SHORT], p.bad[BAD_NONFINITE]), (1, 1, 1, 1));
    }

    #[test]
    fn the_exit_leaves_the_union_not_a_shared_edge() {
        // two abutting polygons (a street and its junction area) and an island in the first
        let a = rect(2, "residential", 0.0, -5.0, 20.0, 5.0);
        let b = rect(4, "junction", 20.0, -5.0, 40.0, 5.0);
        let i = rect(6, "traffic_island", 10.0, 2.0, 12.0, 4.0);
        let p = prepare(&[a, b, i]);
        let cand: Vec<u32> = (0..p.p.len() as u32).collect();
        let t = exit(&p, &cand, [5.0, 0.0], [1.0, 0.0]).unwrap();
        assert!((t - 35.0).abs() < 1e-9, "{t}");
        let t = exit(&p, &cand, [11.0, 0.0], [0.0, 1.0]).unwrap();
        assert!((t - 2.0).abs() < 1e-9, "{t}");
    }

    #[test]
    fn audit_of_a_perfect_and_a_narrow_surface() {
        let area = rect(2, "secondary", 0.0, -6.0, 100.0, 6.0);
        let p = prepare(&[area]);
        let quad = [[0.0, -50.0], [100.0, -50.0], [100.0, 50.0], [0.0, 50.0]];
        let strip = |h: f64| vec![STri { p: [[0.0, -h], [100.0, -h], [100.0, h]], gore: false, minus: false }, STri { p: [[0.0, -h], [100.0, h], [0.0, h]], gore: false, minus: false }];
        let line = vec![CLine { p: vec![[-10.0, 0.0], [110.0, 0.0]], width: 12.0, tag: String::new() }];
        let a = audit(&p, quad, &strip(6.0), &line);
        assert!(a.inter_m2 / a.union_m2 > 0.99);
        assert!(a.edge[A_SECONDARY as usize].q(0.95) <= 0.3, "{}", a.json());
        assert!(a.width[0].q(0.95) <= 0.3);
        let b = audit(&p, quad, &strip(4.5), &line);
        let e = &b.edge[A_SECONDARY as usize];
        assert!((e.q(0.5) - 1.5).abs() <= 0.3 && e.signed < 0.0, "{}", b.json());
        assert!((b.width[0].signed / b.width[0].n as f64 + 3.0).abs() < 0.3, "{}", b.json());
    }

    #[test]
    fn an_island_paved_over_is_missed_and_a_hole_is_an_extra_split() {
        let area = rect(2, "secondary", 0.0, -6.0, 100.0, 6.0);
        let isl = rect(4, "traffic_island", 40.0, -1.0, 60.0, 1.0);
        let p = prepare(&[area, isl]);
        let quad = [[0.0, -50.0], [100.0, -50.0], [100.0, 50.0], [0.0, 50.0]];
        let full = vec![STri { p: [[0.0, -6.0], [100.0, -6.0], [100.0, 6.0]], gore: false, minus: false }, STri { p: [[0.0, -6.0], [100.0, 6.0], [0.0, 6.0]], gore: false, minus: false }];
        let a = audit(&p, quad, &full, &[]);
        assert_eq!((a.islands, a.islands_missed), (1, 1));
        // two strips with a 4 m gap from x 10 to 30 closed at both ends by the surface: an extra split
        let mut holed = Vec::new();
        for (x0, x1, y0, y1) in [(0.0, 100.0, 2.0, 6.0), (0.0, 100.0, -6.0, -2.0), (0.0, 10.0, -2.0, 2.0), (30.0, 100.0, -2.0, 2.0)] {
            holed.push(STri { p: [[x0, y0], [x1, y0], [x1, y1]], gore: false, minus: false });
            holed.push(STri { p: [[x0, y0], [x1, y1], [x0, y1]], gore: false, minus: false });
        }
        let p2 = prepare(&[rect(2, "secondary", 0.0, -6.0, 100.0, 6.0)]);
        let b = audit(&p2, quad, &holed, &[]);
        assert_eq!(b.extra_splits, 1, "{}", b.json());
    }

    fn road(areas: Vec<Area>, rev: bool) -> crate::io::Place {
        let mk = |fid: &str, a: [f64; 2], b: [f64; 2], con: &str, v: usize| crate::io::Seg {
            fid: fid.into(), code: "DE".into(), cls_s: "secondary".into(), pave: 9.0, drive: 7.0, inferred: 2, urban: true, cls: 4,
            dir: 0, lanes: 2, lfw: -1, lbw: -1, kmh: 50.0, lane_w: 3.5, pts: vec![a, b], cons: vec![(con.into(), v)], ..Default::default() };
        let mut segs = vec![mk("a", [-300.0, 0.0], [0.0, 0.0], "n0", 1), mk("b", [0.0, 0.0], [300.0, 0.0], "n0", 0)];
        let mut areas = areas;
        if rev { segs.reverse(); areas.reverse(); }
        crate::io::Place { segs, areas, area_build: 7, ..Default::default() }
    }

    fn edges_at(net: &Net, x: f64) -> (f64, f64) {
        let st = &net.strokes[0];
        let (s, _) = st.line.project([x, 0.0], 0.0, f64::INFINITY);
        let mut off = Vec::new();
        st.offsets(s, &mut off);
        let sgn = if st.line.p[0][0] < st.line.p[st.line.p.len() - 1][0] { 1.0 } else { -1.0 };
        let (l, r) = (off[0] * sgn, off[off.len() - 1] * sgn);
        if l > r { (l, r) } else { (r, l) }
    }

    #[test]
    fn the_fit_puts_the_edges_on_the_mapped_ones() {
        let area = rect(2, "secondary", -300.0, -4.0, 300.0, 6.0);
        let p = road(vec![area.clone()], false);
        let order: Vec<usize> = (0..p.segs.len()).collect();
        let on = crate::net::build_opts(&p, &order, crate::net::Opts { areas: true, ..Default::default() });
        let off = crate::net::build_opts(&p, &order, crate::net::Opts::default());
        assert!(on.area_fit.fitted >= 1, "{:?}", on.area_fit);
        let (l, r) = edges_at(&on, 0.0);
        assert!((l - 6.0).abs() < 0.15 && (r + 4.0).abs() < 0.15, "{l} {r}");
        let (l0, r0) = edges_at(&off, 0.0);
        assert!((l0 - 4.5).abs() < 0.01 && (r0 + 4.5).abs() < 0.01, "{l0} {r0}");
        // far ends (no area past them, open ends): today's edges again
        // a bow-tie polygon: no fit
        let bow = Area { key: 2, value: "secondary".into(), rings: vec![(0, vec![[-300.0, -4.0], [300.0, 6.0], [300.0, -4.0], [-300.0, 6.0], [-300.0, -4.0]])], ..Default::default() };
        let bad = crate::net::build_opts(&road(vec![bow], false), &order, crate::net::Opts { areas: true, ..Default::default() });
        assert_eq!(bad.area_fit.fitted, 0);
        // way off the centre line (a polygon 30 m north): no target
        let far = crate::net::build_opts(&road(vec![rect(2, "secondary", -300.0, 30.0, 300.0, 40.0)], false), &order, crate::net::Opts { areas: true, ..Default::default() });
        assert_eq!(far.area_fit.fitted, 0);
        // a polygon 40 m wide round a 9 m road (a square): implausible, no fit
        let wide = crate::net::build_opts(&road(vec![rect(2, "secondary", -300.0, -20.0, 300.0, 20.0)], false), &order, crate::net::Opts { areas: true, ..Default::default() });
        assert_eq!(wide.area_fit.fitted, 0);
    }

    #[test]
    fn with_cross_sections_the_area_is_the_whole_and_the_strips_divide_it() {
        // (BNE_ROADSYS_XSEC with the fit) parking on both sides: the mapped area sets the carriageway's edges, the
        // strips keep their widths while the lanes can take the rest, and are squeezed once the lanes are at their least
        let order: Vec<usize> = vec![0, 1];
        let with = |xs: &str, half: f64| -> (Net, Net) {
            let mut p = road(vec![rect(2, "secondary", -300.0, -half, 300.0, half)], false);
            for s in p.segs.iter_mut() { s.xsec = xs.replace(';', "\u{1f}"); }
            p.xsec = true;
            (crate::net::build_opts(&p, &order, crate::net::Opts { areas: true, ..Default::default() }),
             crate::net::build_opts(&p, &order, crate::net::Opts::default()))
        };
        let strips = |n: &Net| -> f64 {
            let st = &n.strokes[0];
            let (s, _) = st.line.project([0.0, 0.0], 0.0, f64::INFINITY);
            let mut off = Vec::new();
            st.offsets(s, &mut off);
            st.slots.iter().enumerate().filter(|(_, k)| is_strip(k.kind)).map(|(k, _)| off[k] - off[k + 1]).sum()
        };
        let (on, off) = with("parking:both=lane", 5.0);
        assert!(on.area_fit.fitted >= 1, "{:?}", on.area_fit);
        let (l, r) = edges_at(&on, 0.0);
        assert!((l - 5.0).abs() < 0.15 && (r + 5.0).abs() < 0.15, "{l} {r}");
        assert!(strips(&off) > 3.0 && (strips(&on) - strips(&off)).abs() < 1e-9, "{} {}", strips(&on), strips(&off));
        // perpendicular parking the outline leaves out (mapped as parking spaces): the station is no outlier, the
        // edges still the mapped ones, the strips squeezed into what the lanes at their least leave
        let (on, off) = with("parking:both=lane;parking:both:orientation=perpendicular", 4.0);
        assert!(on.area_fit.fitted >= 1, "{:?}", on.area_fit);
        let (l, r) = edges_at(&on, 0.0);
        assert!((l - 4.0).abs() < 0.15 && (r + 4.0).abs() < 0.15, "{l} {r}");
        assert!(strips(&on) > 0.5 && strips(&on) < 0.5 * strips(&off), "{} {}", strips(&on), strips(&off));
    }

    #[test]
    fn a_fit_held_to_junction_ends_settles() {
        // both ends at junctions (held, not faded: the last knots are not neutral, every slope pass reaches them)
        let mk = |fid: &str, a: [f64; 2], b: [f64; 2], c0: &str, c1: &str| crate::io::Seg {
            fid: fid.into(), code: "DE".into(), cls_s: "secondary".into(), pave: 9.0, drive: 7.0, inferred: 2, urban: true, cls: 4,
            dir: 0, lanes: 2, lfw: -1, lbw: -1, kmh: 50.0, lane_w: 3.5, pts: vec![a, b], cons: vec![(c0.into(), 0), (c1.into(), 1)], ..Default::default() };
        let segs = vec![mk("m", [-300.0, 0.0], [300.0, 0.0], "j0", "j1"), mk("n0", [-300.0, 0.0], [-300.0, 200.0], "j0", "x0"),
                        mk("s0", [-300.0, 0.0], [-300.0, -200.0], "j0", "x1"), mk("n1", [300.0, 0.0], [300.0, 200.0], "j1", "x2"),
                        mk("s1", [300.0, 0.0], [300.0, -200.0], "j1", "x3")];
        let p = crate::io::Place { segs, areas: vec![rect(2, "secondary", -300.0, -4.0, 300.0, 6.0)], ..Default::default() };
        let order: Vec<usize> = (0..p.segs.len()).collect();
        let on = crate::net::build_opts(&p, &order, crate::net::Opts { areas: true, ..Default::default() });
        assert_eq!(on.area_fit.failed, 0);
        assert!(on.area_fit.fitted >= 1, "{:?}", on.area_fit);
    }

    #[test]
    fn decks_between_todays_roads_keep_todays_widths() {
        // (flat_struct: M4's decks, whose approaches are today's ground roads) a structure on "b" from x 90 to 210
        let area = rect(2, "secondary", -300.0, -4.0, 300.0, 6.0);
        let mut p = road(vec![area], false);
        p.segs[1].bridge = vec![(0.3, 0.7)];
        p.segs[1].level = vec![(0.3, 0.7, 1.0)];
        p.areas[0].flags = 0;
        let mut bridge = rect(4, "secondary", 60.0, -4.0, 240.0, 6.0);
        bridge.flags = 1;
        p.areas.push(bridge);
        let order: Vec<usize> = (0..p.segs.len()).collect();
        let on = crate::net::build_opts(&p, &order, crate::net::Opts { areas: true, flat_struct: true, ..Default::default() });
        let (l, r) = edges_at(&on, 150.0);
        assert!((l - 4.5).abs() < 1e-9 && (r + 4.5).abs() < 1e-9, "{l} {r}");
        let (l, r) = edges_at(&on, -150.0);
        assert!((l - 6.0).abs() < 0.15 && (r + 4.0).abs() < 0.15, "{l} {r}");
        // the shadow partition (no flat_struct) fits the deck too
        let sh = crate::net::build_opts(&p, &order, crate::net::Opts { areas: true, ..Default::default() });
        let (l, r) = edges_at(&sh, 150.0);
        assert!((l - 6.0).abs() < 0.15 && (r + 4.0).abs() < 0.15, "{l} {r}");
    }

    #[test]
    fn the_fit_whatever_the_input_order() {
        let areas = vec![rect(2, "secondary", -300.0, -4.0, 0.0, 6.0), rect(4, "secondary", 0.0, -4.5, 300.0, 5.5), rect(6, "traffic_island", 100.0, -0.5, 120.0, 0.5)];
        let q = [[-128.0, -128.0], [128.0, -128.0], [128.0, 128.0], [-128.0, 128.0]];
        let g = |p: &[P2]| -> Vec<f64> { p.iter().map(|q| 10.0 + 0.001 * q[0]).collect() };
        crate::prof::store_clear();
        let a = crate::product::build(&road(areas.clone(), false), q, (0, 0), 1, &g, crate::net::Opts { areas: true, ..Default::default() });
        crate::prof::store_clear();
        let b = crate::product::build(&road(areas.clone(), true), q, (0, 0), 1, &g, crate::net::Opts { areas: true, ..Default::default() });
        assert_eq!(a.bytes, b.bytes);
        assert_eq!(a.piece_keys, b.piece_keys);
        crate::prof::store_clear();
        let c = crate::product::build(&road(areas.clone(), false), q, (0, 0), 1, &g, crate::net::Opts::default());
        assert_ne!(a.bytes, c.bytes);
        assert_ne!(a.piece_keys, c.piece_keys);
        assert!(a.json.contains("\"areas\":{") && c.json.contains("\"areas_on\":false"), "{}", c.json);
        // no areas: the product as before (no areas section)
        crate::prof::store_clear();
        let d = crate::product::build(&road(Vec::new(), false), q, (0, 0), 1, &g, crate::net::Opts { areas: true, ..Default::default() });
        assert_eq!(c.bytes, d.bytes);
        assert!(!d.json.contains("\"areas\""));
        // another area somewhere else in the read: another key only where the fit reads it
        let mut more = areas.clone();
        more.push(rect(8, "residential", 2000.0, 2000.0, 2010.0, 2010.0));
        crate::prof::store_clear();
        let e = crate::product::build(&road(more, false), q, (0, 0), 1, &g, crate::net::Opts { areas: true, ..Default::default() });
        assert_eq!(a.bytes, e.bytes);
        assert_eq!(a.piece_keys, e.piece_keys);
    }

    #[test]
    fn edt_is_exact() {
        let (nx, ny) = (7, 5);
        let mut s = vec![false; nx * ny];
        s[2 * nx + 3] = true;
        let d = edt(nx, ny, &s);
        assert_eq!(d[0], 9.0 + 4.0);
        assert_eq!(d[2 * nx + 6], 9.0);
    }
}
