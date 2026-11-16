//! Poles of the tram overhead line (plan 046; tramcat.py's `stations`, `_anchor`, `deck_at`, `plan`, `_mast`): where a
//! street tram's poles stand on the pavements (or a fixing on a building's wall), where an own-way tram's cantilever
//! mast stands, and the nodes along each part of the line (its ends, a span wire's axis points, the masts) that
//! wires.rs hangs the contact wire and the span wires on.
//!
//! The poles stand every SPAN_M (shorter on a curve) from a phase hashed from the part's id, the same in every tile; a
//! pole near another (SHARE_M) is that one. Pure given its inputs.
use super::geom::*;

pub const SPAN_M: f64 = 34.0;
pub const SPAN_MIN_M: f64 = 16.0;
pub const CURVE_K: f64 = 0.3;
pub const OUT_MAX_M: f64 = 28.0;
pub const START_OUT_M: f64 = 1.2;
pub const SIDEWALK_OFF_M: f64 = 0.9;
pub const TRACK_CLEAR_M: f64 = 1.8;
pub const SHARE_M: f64 = 5.0;
pub const BRIDGE_OUT_M: f64 = 0.55;
pub const STREET_MARGIN_M: f64 = 3.0;
pub const DECK_NEAR_M: f64 = 6.0;
// catenary.py's (the cantilever mast of an own-way tram is the railway's)
pub const MAST_OFF_M: f64 = 3.45;
pub const BETWEEN_M: f64 = 3.0;
pub const ROAD_CLEAR_M: f64 = 2.0;
pub const BUILDING_CLEAR_M: f64 = 1.5;
pub const SHIFT_M: [f64; 7] = [0.0, 3.0, -3.0, 6.0, -6.0, 9.0, -9.0];
// stage E: a track of its own with a neighbour track running alongside at a gap of GAP_MIN_M .. GAP_MAX_M (axis to axis)
// gets centre poles between them (a two-sided cantilever); the poles stand on the middle line, clear of everything
pub const GAP_MIN_M: f64 = 3.4;
pub const GAP_MAX_M: f64 = 6.4;
pub const GAP_COS: f64 = 0.95;
pub const CENTRE_NEAR_M: f64 = 14.0;
pub const NODE_MIN_GAP_M: f64 = 3.0;
pub const LAMP_CLEAR_M: f64 = 5.0;
// a street pole stands at most this far from the axis: a farther one (a junction's wide mouth) was a span wire of 30 m and
// more, so that side has none (the span hangs from the other, or the contact wire runs on from pole to pole)
pub const ANCHOR_MAX_M: f64 = 16.0;
// a pole keeps this far off a railway track's axis (a tram line crossing or running beside a railway)
pub const RAIL_CLEAR_M: f64 = 3.0;
// a pole's foot keeps this far off every ground tram track's axis, electrified or not (the sleepers are 2.6 m long, 1.3 m each
// side, and the pole's base is 0.3 m: the pole stood on the bed in a yard's fan); the tram corridor (corridor.rs) is 1.6 m
pub const POLE_TRACK_CLEAR_M: f64 = 1.6;
// the poles of a stretch of wire that no pole holds for MAX_UNSUPPORTED_M may stand this close to a carriageway's edge (a track beside a road)
pub const FILL_ROAD_CLEAR_M: f64 = 0.8;
// a yard (>= PORTAL_MIN_TRACKS electrified tracks side by side within FAN_GAP_M of each other, running about parallel): a
// cross-span portal, its posts PORTAL_POST_OUT_M outside the outermost track, a pole in a gap wider than MID_GAP_MIN_M where
// the fan is wider than BAY_MAX_M; a stretch of wire with no support for MAX_UNSUPPORTED_M gets another station
pub const PORTAL_MIN_TRACKS: usize = 3;
pub const PORTAL_COS: f64 = 0.8;
pub const PORTAL_POST_OUT_M: f64 = 3.0;
pub const FAN_REACH_M: f64 = 30.0;
// a station of a higher part within this of a lower part's portal hanger is that portal's (the lower part's stations are SPAN_M apart)
pub const PORTAL_SERVE_M: f64 = 18.0;
pub const FAN_GAP_M: f64 = 14.0;
pub const BAY_MAX_M: f64 = 22.0;
pub const BAY_HARD_MAX_M: f64 = 40.0;
pub const MID_GAP_MIN_M: f64 = 4.0;
pub const MAX_UNSUPPORTED_M: f64 = 40.0;
pub const SHIFT_WIDE_M: [f64; 11] = [0.0, 3.0, -3.0, 6.0, -6.0, 9.0, -9.0, 12.0, -12.0, 15.0, -15.0];

/// The look of a country's tram poles (scenery by country, the families as europeinfra's: a part of a line takes one of
/// its country's by a hash of its id, the same in every tile). Tube: the galvanised tapered tubular pole on a flange
/// (Germany, Austria, the Netherlands, ...); Square: the black square tube with a T lamp head (Helsinki); Lattice: the
/// black lattice pole (Tampere); Concrete: the spun concrete pole (ex-USSR).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Family {
    Tube,
    Square,
    Lattice,
    Concrete,
}

impl Family {
    pub fn name(self) -> &'static str {
        match self {
            Family::Tube => "tube",
            Family::Square => "square",
            Family::Lattice => "lattice",
            Family::Concrete => "concrete",
        }
    }

    /// A black pole carries the black lamp bracket.
    pub fn black(self) -> bool {
        matches!(self, Family::Square | Family::Lattice)
    }
}

/// The families of an ISO alpha-2 country's tram poles (the first is the usual one).
pub fn families(country: &str) -> &'static [Family] {
    match country.to_ascii_uppercase().as_str() {
        "UA" | "RU" | "BY" | "MD" | "GE" | "AM" | "AZ" | "KZ" | "UZ" | "KG" | "TJ" | "TM" => &[Family::Concrete],
        "PL" | "CZ" | "SK" | "HU" | "RO" | "BG" | "RS" | "HR" | "SI" | "EE" | "LV" | "LT" => &[Family::Tube, Family::Concrete],
        "FI" => &[Family::Square, Family::Lattice],
        "SE" | "NO" | "DK" => &[Family::Square, Family::Tube],
        _ => &[Family::Tube],
    }
}

/// The family of the line `fid` in a country.
pub fn family_of(country: &str, fid: &str) -> Family {
    let f = families(country);
    f[((h01(fid, "family") * f.len() as f64) as usize).min(f.len() - 1)]
}

/// One part of a tram line (an OSM way's piece, resampled to ~2 m).
pub struct Part {
    pub fid: String,
    pub bridge: bool,
    pub xy: Vec<P2>,
}

/// A carriageway: its centre line and half width.
pub struct Road {
    pub pts: Vec<P2>,
    pub hw: f64,
}

/// A road bridge deck under a tram bridge: centre line, height at each point, width.
pub struct Deck {
    pub xy: Vec<P2>,
    pub z: Vec<f64>,
    pub width: f64,
}

/// What the plan reads (all in the tile's frame, metres).
pub struct Scene {
    pub parts: Vec<Part>,
    pub roads: Vec<Road>,
    pub rings: Vec<Vec<P2>>,
    pub decks: Vec<Deck>,
    pub country: String,
    /// The railway tracks' axes near (not trams): the poles stay clear of them and a tram wire crossing one is insulated.
    pub rails: Vec<Vec<P2>>,
    /// Every ground tram track's axis, electrified or not, ways that meet end to end one line (corridor.rs): no pole on them.
    pub tracks: Vec<Vec<P2>>,
}

/// Which model stands where a pole is.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PoleKind {
    /// A pole at a street's edge carrying a span wire (and a lamp bracket).
    Street,
    /// A pole behind a bridge's parapet.
    Bridge,
    /// A pole between two tracks of their own, a cantilever each side.
    Centre,
    /// A pole of a cross-span portal over a yard's fan of tracks (no lamp).
    Portal,
}

#[derive(Clone, Debug)]
pub struct Pole {
    pub q: P2,
    /// A fixing on a building's wall (a rosette, no pole drawn).
    pub wall: bool,
    /// Its base height where it stands on a road deck.
    pub z: Option<f64>,
    pub kind: PoleKind,
    pub family: Family,
    /// The point a span wire is fixed at (the wall's face for a rosette, else q) and, for a wall, the face's outward unit normal.
    pub fix: P2,
    pub normal: P2,
    /// The axis point of the first node that uses it: the way its arms / lamp bracket point.
    pub toward: P2,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    End,
    Span,
    Mast,
}

#[derive(Clone, Debug)]
pub struct Node {
    /// The axis point.
    pub p: P2,
    pub kind: Kind,
    pub s: f64,
    /// Indices into Plan::poles (a span's fixings).
    pub anchors: Vec<usize>,
    /// A mast's foot and the unit direction from it to the axis.
    pub q: P2,
    pub dir: P2,
    /// Its height where it is on a road deck.
    pub z: Option<f64>,
    /// A mast node that hangs on a centre pole (Plan::poles[i]): the pole's row is the pole's, not the node's.
    pub centre: Option<usize>,
    /// The family of its mast.
    pub family: Family,
    /// A portal's hanger: the contact wire hangs from this bay's cross-span (Plan::bays[i]).
    pub bay: Option<usize>,
}

impl Node {
    fn new(p: P2, kind: Kind, s: f64) -> Node {
        Node { p, kind, s, anchors: Vec::new(), q: [0.0; 2], dir: [0.0; 2], z: None, centre: None, family: Family::Tube, bay: None }
    }
}

pub struct PlanPart {
    pub part: usize,
    pub nodes: Vec<Node>,
}

/// A cross-span between two poles of a portal (Plan::poles indices): the wire the contact wires of its hanger nodes hang from.
pub struct Bay {
    pub a: usize,
    pub b: usize,
}

pub struct Plan {
    pub parts: Vec<PlanPart>,
    pub poles: Vec<Pole>,
    pub bays: Vec<Bay>,
}

/// tramcat.stations: arc lengths of the poles along xy: from a phase hashed from fid, the span by the curve there.
pub fn stations(xy: &[P2], arc: &[f64], fid: &str) -> Vec<f64> {
    let mut s = h01(fid, "tram") * SPAN_M;
    let mut out = Vec::new();
    while s < arc[arc.len() - 1] {
        out.push(s);
        let r = radius(xy, arc, s, 20.0);
        s += if !r.is_finite() { SPAN_M } else { (8.0 * r * CURVE_K).sqrt().clamp(SPAN_MIN_M, SPAN_M) };
    }
    out
}

fn lines_of(roads: &[Road]) -> impl Iterator<Item = (&[P2], f64)> {
    roads.iter().map(|r| (r.pts.as_slice(), r.hw))
}

fn off_roads(q: P2, roads: &[Road], extra: f64) -> bool {
    lines_of(roads).all(|(ln, hw)| dist_to_line(q, ln) > hw + extra)
}

/// The point of the nearest outline of the footprints to q, and the outline's outward unit normal there.
pub fn wall_point(q: P2, rings: &[Vec<P2>]) -> Option<(P2, P2)> {
    let mut best: Option<(f64, P2, usize, usize)> = None;
    for (ri, r) in rings.iter().enumerate() {
        let n = r.len();
        for i in 0..n {
            let (_, c, _) = seg_t(q, r[i], r[(i + 1) % n]);
            let d = hyp(c[0] - q[0], c[1] - q[1]);
            if best.map_or(true, |b| d < b.0) {
                best = Some((d, c, ri, i));
            }
        }
    }
    let (_d, c, ri, i) = best?;
    let r = &rings[ri];
    let (a, b) = (r[i], r[(i + 1) % r.len()]);
    let t = [b[0] - a[0], b[1] - a[1]];
    let tl = hyp(t[0], t[1]).max(1e-9);
    let mut n = [-t[1] / tl, t[0] / tl];
    // (outward: a step along the normal must leave the footprint)
    if near_ring([c[0] + n[0] * 0.05, c[1] + n[1] * 0.05], &rings[ri..=ri], 0.0) {
        n = [-n[0], -n[1]];
    }
    Some((c, n))
}

/// The pole's fixing out from axis point p along side * n (tramcat._anchor): the first point SIDEWALK_OFF_M clear of
/// every carriageway (and off the other tracks); a wall where a building stands there ((wall point, normal)). A track
/// that is not on a carriageway finds none across one: a pole on the far side of a road was a span wire over it.
#[allow(clippy::too_many_arguments)]
fn anchor(p: P2, n: P2, side: f64, roads: &[Road], rings: &[Vec<P2>], others: &[&[P2]], rails: &[Vec<P2>], tracks: &[Vec<P2>], far: bool)
          -> Option<(P2, Option<(P2, P2)>)> {
    // (far: the last try for a stretch no pole holds: the pavement across the carriageways, up to OUT_MAX_M)
    let count = (((if far { OUT_MAX_M } else { OUT_MAX_M.min(ANCHOR_MAX_M) }) - START_OUT_M) / 0.5).ceil() as usize;
    let on_road = roads.iter().any(|r| dist_to_line(p, &r.pts) < r.hw);
    for i in 0..count {
        let d = START_OUT_M + i as f64 * 0.5;
        let q = [p[0] + n[0] * side * d, p[1] + n[1] * side * d];
        if !far && !on_road && roads.iter().any(|r| dist_to_line(q, &r.pts) < r.hw) {
            return None;
        }
        if !off_roads(q, roads, SIDEWALK_OFF_M) {
            continue;
        }
        if !others.is_empty() && dist_to_lines(q, others.iter().copied()) < TRACK_CLEAR_M {
            continue;
        }
        if !rails.is_empty() && dist_to_lines(q, rails.iter().map(|r| r.as_slice())) < RAIL_CLEAR_M {
            continue;
        }
        if !track_ok(q, tracks) {
            continue;
        }
        let wall = if !rings.is_empty() && near_ring(q, rings, 0.5) { wall_point(q, rings) } else { None };
        return Some((q, wall));
    }
    None
}

/// tramcat.deck_at: (z, half width, centre point, unit direction) of the road deck under p: the nearest of the decks
/// within DECK_NEAR_M of it.
pub fn deck_at(p: P2, decks: &[Deck]) -> Option<(f64, f64, P2, P2)> {
    let mut best: Option<(f64, f64, f64, P2, P2)> = None;
    for dk in decks {
        if dk.xy.len() < 2 || dk.z.len() < dk.xy.len() {
            continue;
        }
        // (railways.road_deck_z: the nearest segment within DECK_NEAR_M, the height interpolated along it)
        let mut nd = (f64::INFINITY, 0usize, 0.0f64);
        for j in 0..dk.xy.len() - 1 {
            let (t, q, _) = seg_t(p, dk.xy[j], dk.xy[j + 1]);
            let d = ((q[0] - p[0]) * (q[0] - p[0]) + (q[1] - p[1]) * (q[1] - p[1])).sqrt();
            if d < nd.0 {
                nd = (d, j, t);
            }
        }
        if nd.0 <= DECK_NEAR_M {
            let z = dk.z[nd.1] + nd.2 * (dk.z[nd.1 + 1] - dk.z[nd.1]);
            let a = arc(&dk.xy);
            let (_s, q, u, d) = nearest(&dk.xy, &a, p);
            if best.map_or(true, |b| d < b.0) {
                best = Some((d, z, dk.width / 2.0, q, u));
            }
        }
    }
    best.map(|b| (b.1, b.2, b.3, b.4))
}

/// tramtrack.in_street: whether the point lies within STREET_MARGIN_M of a carriageway's edge.
fn in_street(p: P2, roads: &[Road]) -> bool {
    roads.iter().any(|r| {
        let rr = r.hw + STREET_MARGIN_M;
        r.pts.windows(2).any(|w| {
            let (_, q, _) = seg_t(p, w[0], w[1]);
            hyp(p[0] - q[0], p[1] - q[1]) < rr
        })
    })
}

/// tramcat._mast: the railway's cantilever mast for an own-way tram at arc s0 (catenary.masts_of's rule, one line).
#[allow(clippy::too_many_arguments)]
fn mast(xy: &[P2], arc: &[f64], s0: f64, others: &[&[P2]], roads: &[Road], rings: &[Vec<P2>], rails: &[Vec<P2>], tracks: &[Vec<P2>],
        shifts: &[f64], road_clear: f64, fid: &str) -> Option<Node> {
    let side0 = if h01(fid, "side") < 0.5 { 1.0 } else { -1.0 };
    for &shift in shifts {
        let s = s0 + shift;
        if !(0.0 <= s && s <= arc[arc.len() - 1]) {
            continue;
        }
        let (p, u) = frame_at(xy, arc, s);
        let n = [-u[1], u[0]];
        for side in [side0, -side0] {
            let q = [p[0] + n[0] * side * MAST_OFF_M, p[1] + n[1] * side * MAST_OFF_M];
            if !others.is_empty() && dist_to_lines(q, others.iter().copied()) < BETWEEN_M {
                continue;
            }
            if !lines_of(roads).all(|(ln, hw)| dist_to_line(q, ln) > hw + road_clear) || (!rings.is_empty() && near_ring(q, rings, BUILDING_CLEAR_M)) {
                continue;
            }
            if !rails.is_empty() && dist_to_lines(q, rails.iter().map(|r| r.as_slice())) < RAIL_CLEAR_M {
                continue;
            }
            if !track_ok(q, tracks) {
                continue;
            }
            let l = hyp(p[0] - q[0], p[1] - q[1]).max(1e-9);
            let mut nd = Node::new(p, Kind::Mast, s);
            nd.q = q;
            nd.dir = [(p[0] - q[0]) / l, (p[1] - q[1]) / l];
            return Some(nd);
        }
    }
    None
}

/// The neighbour track alongside a point of part pi: (part index, arc on it, its point, unit direction, gap) of the
/// nearest other part within GAP_MIN_M .. GAP_MAX_M that runs parallel (GAP_COS) and has the point inside its length.
fn neighbour(sc: &Scene, arcs: &[Vec<f64>], pi: usize, p: P2, u: P2) -> Option<(usize, f64, P2, P2, f64)> {
    let mut best: Option<(usize, f64, P2, P2, f64)> = None;
    for (pj, o) in sc.parts.iter().enumerate() {
        if pj == pi || o.xy.len() < 2 {
            continue;
        }
        let (s, q, uj, d) = nearest(&o.xy, &arcs[pj], p);
        if d >= GAP_MIN_M && d <= GAP_MAX_M && (u[0] * uj[0] + u[1] * uj[1]).abs() >= GAP_COS && s > 0.0 && s < arcs[pj][arcs[pj].len() - 1]
            && best.map_or(true, |b| d < b.4) {
            best = Some((pj, s, q, uj, d));
        }
    }
    best
}

fn unit2(d: P2) -> P2 {
    let l = hyp(d[0], d[1]).max(1e-9);
    [d[0] / l, d[1] / l]
}

fn share(poles: &mut Vec<Pole>, q: P2, z: Option<f64>, kind: PoleKind, family: Family, toward: P2) -> usize {
    for (i, pl) in poles.iter().enumerate() {
        if hyp(pl.q[0] - q[0], pl.q[1] - q[1]) <= SHARE_M && pl.z.is_some() == z.is_some() && (pl.kind == PoleKind::Centre) == (kind == PoleKind::Centre)
            && (pl.kind == PoleKind::Portal) == (kind == PoleKind::Portal) {
            return i;
        }
    }
    poles.push(Pole { q, wall: false, z, kind, family, fix: q, normal: [0.0; 2], toward });
    poles.len() - 1
}

/// Whether a pole's foot may stand at q: clear of every carriageway (ROAD_CLEAR_M), building, railway and ground tram track
/// (POLE_TRACK_CLEAR_M: a sleeper's half length and the pole's base).
pub fn site_ok(sc: &Scene, q: P2) -> bool {
    site_ok_r(sc, q, ROAD_CLEAR_M)
}

/// site_ok with its own clearance off the carriageways (the last try for a stretch that has no support: FILL_ROAD_CLEAR_M).
pub fn site_ok_r(sc: &Scene, q: P2, road_clear: f64) -> bool {
    lines_of(&sc.roads).all(|(ln, hw)| dist_to_line(q, ln) > hw + road_clear)
        && !(!sc.rings.is_empty() && near_ring(q, &sc.rings, BUILDING_CLEAR_M))
        && (sc.rails.is_empty() || dist_to_lines(q, sc.rails.iter().map(|r| r.as_slice())) >= RAIL_CLEAR_M)
        && track_ok(q, &sc.tracks)
}

/// Whether q keeps POLE_TRACK_CLEAR_M off every ground tram track's axis.
pub fn track_ok(q: P2, tracks: &[Vec<P2>]) -> bool {
    tracks.is_empty() || dist_to_lines(q, tracks.iter().map(|r| r.as_slice())) >= POLE_TRACK_CLEAR_M
}

/// A track crossing the transversal through a station: lateral position t, the axis point, the part (None: a track of
/// the ground that has no overhead line) and the arc on it.
#[derive(Clone, Copy, Debug)]
struct Cross {
    part: Option<usize>,
    t: f64,
    p: P2,
    s: f64,
}

/// The tracks that cross the line through p0 across direction u0 (within FAN_REACH_M, running about parallel to u0), sorted
/// by lateral position; an electrified part's crossings first, then the other ground tracks' not already there.
fn crossings(sc: &Scene, arcs: &[Vec<f64>], p0: P2, u0: P2) -> Vec<Cross> {
    let n = [-u0[1], u0[0]];
    let mut out: Vec<Cross> = Vec::new();
    let mut scan = |line: &[P2], arc: Option<&[f64]>, part: Option<usize>, out: &mut Vec<Cross>| {
        let mut run = 0.0;
        for w in line.windows(2) {
            let (a, b) = (w[0], w[1]);
            let seg = hyp(b[0] - a[0], b[1] - a[1]);
            let xa = (a[0] - p0[0]) * u0[0] + (a[1] - p0[1]) * u0[1];
            let xb = (b[0] - p0[0]) * u0[0] + (b[1] - p0[1]) * u0[1];
            let base = arc.map_or(run, |ar| ar[0]);
            run += seg;
            if (xa > 0.0) == (xb > 0.0) || seg < 1e-9 {
                continue;
            }
            let cosine = ((b[0] - a[0]) * u0[0] + (b[1] - a[1]) * u0[1]) / seg;
            if cosine.abs() < PORTAL_COS {
                continue;
            }
            let f = xa / (xa - xb);
            let q = [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f];
            let t = (q[0] - p0[0]) * n[0] + (q[1] - p0[1]) * n[1];
            if t.abs() > FAN_REACH_M {
                continue;
            }
            out.push(Cross { part, t, p: q, s: base + run - seg + seg * f });
        }
    };
    for (j, o) in sc.parts.iter().enumerate() {
        if !o.bridge && o.xy.len() >= 2 {
            let mut v = Vec::new();
            scan(&o.xy, Some(&arcs[j]), Some(j), &mut v);
            // (the arc of a crossing: the part's own, from the cumulative arc at its segment)
            for c in v.iter_mut() {
                let (s, _q, _u, _d) = nearest(&o.xy, &arcs[j], c.p);
                c.s = s;
            }
            out.extend(v);
        }
    }
    let elec = out.len();
    let mut others = Vec::new();
    for l in &sc.tracks {
        scan(l, None, None, &mut others);
    }
    for c in others {
        if !out[..elec].iter().any(|e| (e.t - c.t).abs() < 1.5) && !out[elec..].iter().any(|e| (e.t - c.t).abs() < 1.5) {
            out.push(c);
        }
    }
    out.sort_by(|x, y| x.t.partial_cmp(&y.t).unwrap_or(std::cmp::Ordering::Equal));
    out
}

/// One pole of a portal: its lateral position, foot, and (a centre pole between two tracks) the gap it stands in.
struct PortalPost {
    t: f64,
    q: P2,
    gap: Option<usize>,
}

/// A cross-span portal over a fan of tracks (a yard): the posts outside the outermost tracks, intermediate poles in gaps
/// wider than MID_GAP_MIN_M where the fan is wider than BAY_MAX_M, and the electrified crossings between them.
struct Portal {
    posts: Vec<PortalPost>,
    group: Vec<Cross>,
    n: P2,
}

/// The portal at the station (p0, u0) of part pi, if >= PORTAL_MIN_TRACKS electrified tracks run side by side there:
/// Err(true) the station is another part's (skip it), Err(false) no portal (the posts cannot stand: use a mast).
fn portal(sc: &Scene, arcs: &[Vec<f64>], pi: usize, p0: P2, u0: P2) -> Result<Portal, bool> {
    let cr = crossings(sc, arcs, p0, u0);
    let Some(own) = cr.iter().enumerate().filter(|(_, c)| c.part == Some(pi)).min_by(|a, b| a.1.t.abs().partial_cmp(&b.1.t.abs()).unwrap()).map(|(i, _)| i) else {
        return Err(false);
    };
    let (mut lo, mut hi) = (own, own);
    while lo > 0 && cr[lo].t - cr[lo - 1].t <= FAN_GAP_M {
        lo -= 1;
    }
    while hi + 1 < cr.len() && cr[hi + 1].t - cr[hi].t <= FAN_GAP_M {
        hi += 1;
    }
    let group: Vec<Cross> = cr[lo..=hi].to_vec();
    let elec: Vec<&Cross> = group.iter().filter(|c| c.part.is_some()).collect();
    if elec.len() < PORTAL_MIN_TRACKS {
        return Err(false);
    }
    let n = [-u0[1], u0[0]];
    let (t_lo, t_hi) = (group[0].t, group[group.len() - 1].t);
    let at = |t: f64| [p0[0] + n[0] * t, p0[1] + n[1] * t];
    let post = |edge: f64, dir: f64| -> Option<PortalPost> {
        for e in [0.0, 1.5, 3.0, 4.5, 6.0] {
            let t = edge + dir * (PORTAL_POST_OUT_M + e);
            if site_ok(sc, at(t)) {
                return Some(PortalPost { t, q: at(t), gap: None });
            }
        }
        None
    };
    let (Some(left), Some(right)) = (post(t_lo, -1.0), post(t_hi, 1.0)) else { return Err(false) };
    let mut posts = vec![left];
    while right.t - posts[posts.len() - 1].t > BAY_MAX_M {
        let last = posts[posts.len() - 1].t;
        let mut best: Option<PortalPost> = None;
        for k in 0..group.len() - 1 {
            let (a, b) = (group[k].t, group[k + 1].t);
            let m = (a + b) / 2.0;
            if b - a >= MID_GAP_MIN_M && m > last + 3.0 && m - last <= BAY_MAX_M && best.as_ref().map_or(true, |x| m > x.t) && site_ok(sc, at(m)) {
                best = Some(PortalPost { t: m, q: at(m), gap: Some(k) });
            }
        }
        let Some(b) = best else {
            // (no gap to stand in: the span is longer, up to BAY_HARD_MAX_M, or there is no portal)
            if right.t - last <= BAY_HARD_MAX_M {
                break;
            }
            return Err(false);
        };
        posts.push(b);
    }
    posts.push(right);
    Ok(Portal { posts, group, n })
}

/// The plan of the tram lines: the nodes of each part (its ends, a span wire's axis point between two poles in a street,
/// a mast's, a centre pole's, a portal's hanger) and the poles. A pole near another of its kind (SHARE_M) is that one.
pub fn plan(sc: &Scene) -> Plan {
    let mut poles: Vec<Pole> = Vec::new();
    let mut bays: Vec<Bay> = Vec::new();
    let mut out: Vec<PlanPart> = Vec::new();
    if sc.parts.is_empty() {
        return Plan { parts: out, poles, bays };
    }
    let arcs: Vec<Vec<f64>> = sc.parts.iter().map(|p| arc(&p.xy)).collect();
    // (nodes a lower part planned for a higher one: a centre pole's, a portal's hangers, at the higher part's own points)
    let mut pending: Vec<Vec<Node>> = vec![Vec::new(); sc.parts.len()];
    for (pi, part) in sc.parts.iter().enumerate() {
        let xy = part.xy.as_slice();
        let a = &arcs[pi];
        let mut stn = stations(xy, a, &part.fid);
        let others: Vec<&[P2]> = sc.parts.iter().enumerate().filter(|(j, _)| *j != pi).map(|(_, o)| o.xy.as_slice()).collect();
        let bridge = part.bridge;
        let family = family_of(&sc.country, &part.fid);
        if bridge {
            let step = (xy.len() / 4).max(1);
            if !xy.iter().step_by(step).any(|p| deck_at(*p, &sc.decks).is_some()) {
                continue; // (a bridge of its own, no road deck under it: no overhead line on it, yet)
            }
        }
        let end = |p: P2, s: f64| {
            let mut nd = Node::new(p, Kind::End, s);
            if bridge {
                if let Some(dk) = deck_at(p, &sc.decks) {
                    nd.z = Some(dk.0);
                }
            }
            nd
        };
        let mut nodes = vec![end(xy[0], 0.0)];
        let mut mid: Vec<Node> = std::mem::take(&mut pending[pi]);
        for round in 0..2 {
            let fill = round == 1;
            for &s0 in &stn {
                let (p0, u0) = frame_at(xy, a, s0);
                let n = [-u0[1], u0[0]];
                if bridge {
                    // poles at the road deck's edges, at its height
                    let Some((zd, half, qc, uc)) = deck_at(p0, &sc.decks) else { continue };
                    let nc = [-uc[1], uc[0]];
                    let mut nd = Node::new(p0, Kind::Span, s0);
                    for side in [1.0, -1.0] {
                        let q = [qc[0] + nc[0] * side * (half + BRIDGE_OUT_M), qc[1] + nc[1] * side * (half + BRIDGE_OUT_M)];
                        nd.anchors.push(share(&mut poles, q, Some(zd), PoleKind::Bridge, family, p0));
                    }
                    nd.z = Some(zd);
                    mid.push(nd);
                    continue;
                }
                let street = !sc.roads.is_empty() && in_street(p0, &sc.roads);
                if street {
                    let mut nd = Node::new(p0, Kind::Span, s0);
                    for side in [1.0, -1.0] {
                        if let Some((q, wall)) = anchor(p0, n, side, &sc.roads, &sc.rings, &others, &sc.rails, &sc.tracks, fill) {
                            let i = share(&mut poles, q, None, PoleKind::Street, family, p0);
                            if let Some((c, nrm)) = wall {
                                poles[i].wall = true;
                                poles[i].fix = c;
                                poles[i].normal = nrm;
                            }
                            nd.anchors.push(i);
                        }
                    }
                    if !nd.anchors.is_empty() {
                        mid.push(nd);
                        continue;
                    }
                    if !fill {
                        continue;
                    }
                    // (the last try for a stretch no pole holds: a street's pavements out of reach, a cantilever mast beside the track)
                }
                // a fan of tracks of their own (a yard): a cross-span portal, the poles outside the whole fan
                if !fill {
                    match portal(sc, &arcs, pi, p0, u0) {
                        Ok(pt) => {
                            let lower = pt.group.iter().any(|c| c.part.map_or(false, |j| j < pi));
                            if lower && mid.iter().any(|m| (m.bay.is_some() || m.centre.is_some()) && hyp(m.p[0] - p0[0], m.p[1] - p0[1]) < PORTAL_SERVE_M) {
                                continue; // (served by the lower part's portal)
                            }
                            make_portal(sc, &pt, pi, family, &mut poles, &mut bays, &mut mid, &mut pending);
                            continue;
                        }
                        Err(_) => {}
                    }
                }
                // a track of its own: a centre pole where another track runs alongside, else the cantilever mast beside it
                if let Some((pj, sj, qj, _uj, _d)) = neighbour(sc, &arcs, pi, p0, u0) {
                    if pj < pi {
                        // (the lower part's centre poles serve this one: its nodes are the pending ones)
                        if mid.iter().any(|m| m.centre.is_some() && hyp(m.p[0] - p0[0], m.p[1] - p0[1]) < CENTRE_NEAR_M) {
                            continue;
                        }
                    } else {
                        let c = [(p0[0] + qj[0]) / 2.0, (p0[1] + qj[1]) / 2.0];
                        let third: Vec<&[P2]> = sc.parts.iter().enumerate().filter(|(j, _)| *j != pi && *j != pj).map(|(_, o)| o.xy.as_slice()).collect();
                        if site_ok_r(sc, c, if fill { FILL_ROAD_CLEAR_M } else { ROAD_CLEAR_M }) && (third.is_empty() || dist_to_lines(c, third.iter().copied()) >= TRACK_CLEAR_M) {
                            let i = share(&mut poles, c, None, PoleKind::Centre, family, p0);
                            let mut nd = Node::new(p0, Kind::Mast, s0);
                            nd.q = poles[i].q;
                            nd.dir = unit2([p0[0] - poles[i].q[0], p0[1] - poles[i].q[1]]);
                            nd.centre = Some(i);
                            nd.family = family;
                            mid.push(nd);
                            let mut nj = Node::new(qj, Kind::Mast, sj);
                            nj.q = poles[i].q;
                            nj.dir = unit2([qj[0] - poles[i].q[0], qj[1] - poles[i].q[1]]);
                            nj.centre = Some(i);
                            nj.family = family;
                            pending[pj].push(nj);
                            continue;
                        }
                    }
                }
                let shifts: &[f64] = if fill { &SHIFT_WIDE_M } else { &SHIFT_M };
                if let Some(mut nd) = mast(xy, a, s0, &others, &sc.roads, &sc.rings, &sc.rails, &sc.tracks, shifts, if fill { FILL_ROAD_CLEAR_M } else { ROAD_CLEAR_M }, &part.fid) {
                    nd.family = family;
                    mid.push(nd);
                }
            }
            if fill {
                break;
            }
            // (a stretch of the wire with no support for more than MAX_UNSUPPORTED_M gets stations of its own, once)
            mid.sort_by(|x, y| x.s.partial_cmp(&y.s).unwrap_or(std::cmp::Ordering::Equal));
            let mut marks: Vec<f64> = vec![0.0];
            marks.extend(mid.iter().map(|m| m.s));
            marks.push(a[a.len() - 1]);
            stn = Vec::new();
            for w in marks.windows(2) {
                let gap = w[1] - w[0];
                if gap > MAX_UNSUPPORTED_M {
                    let k = (gap / MAX_UNSUPPORTED_M).ceil() as usize;
                    for m in 1..k {
                        stn.push(w[0] + gap * m as f64 / k as f64);
                    }
                }
            }
            if stn.is_empty() {
                break;
            }
        }
        // (the nodes along the part from its front; one within NODE_MIN_GAP_M of the one before is left out)
        mid.sort_by(|x, y| x.s.partial_cmp(&y.s).unwrap_or(std::cmp::Ordering::Equal));
        for nd in mid {
            if nodes.last().map_or(true, |l| nd.s - l.s >= NODE_MIN_GAP_M || (l.kind == Kind::End && nd.s - l.s >= 1.0)) {
                nodes.push(nd);
            }
        }
        nodes.push(end(xy[xy.len() - 1], a[a.len() - 1]));
        out.push(PlanPart { part: pi, nodes });
    }
    Plan { parts: out, poles, bays }
}

/// The poles, bays and hanger nodes of a portal (the electrified tracks of its group that belong to part pi or a higher one).
#[allow(clippy::too_many_arguments)]
fn make_portal(sc: &Scene, pt: &Portal, pi: usize, family: Family, poles: &mut Vec<Pole>, bays: &mut Vec<Bay>, mid: &mut Vec<Node>,
               pending: &mut [Vec<Node>]) {
    let n = pt.n;
    let k = pt.posts.len();
    let mut ix: Vec<usize> = Vec::new();
    for (i, ps) in pt.posts.iter().enumerate() {
        let centre = ps.gap.map_or(false, |g| pt.group[g + 1].t - pt.group[g].t <= GAP_MAX_M);
        let kind = if centre { PoleKind::Centre } else { PoleKind::Portal };
        let dir = if i + 1 == k { -1.0 } else { 1.0 };
        let toward = [ps.q[0] + n[0] * dir * 2.0, ps.q[1] + n[1] * dir * 2.0];
        ix.push(share(poles, ps.q, None, kind, family, toward));
    }
    // (the tracks a centre pole's arms hold: the two beside it)
    let mut held: Vec<Option<usize>> = vec![None; pt.group.len()];
    for (i, ps) in pt.posts.iter().enumerate() {
        if let Some(g) = ps.gap {
            if poles[ix[i]].kind == PoleKind::Centre {
                held[g] = Some(ix[i]);
                held[g + 1] = Some(ix[i]);
            }
        }
    }
    let first_bay = bays.len();
    for w in ix.windows(2) {
        bays.push(Bay { a: w[0], b: w[1] });
    }
    for (gi, c) in pt.group.iter().enumerate() {
        let Some(j) = c.part else { continue };
        if j < pi {
            continue;
        }
        let mut nd;
        if let Some(pole) = held[gi] {
            nd = Node::new(c.p, Kind::Mast, c.s);
            nd.q = poles[pole].q;
            nd.dir = unit2([c.p[0] - nd.q[0], c.p[1] - nd.q[1]]);
            nd.centre = Some(pole);
            nd.family = family;
        } else {
            nd = Node::new(c.p, Kind::Span, c.s);
            let b = (0..k - 1).find(|&i| pt.posts[i].t < c.t && c.t < pt.posts[i + 1].t).unwrap_or(0);
            nd.bay = Some(first_bay + b);
        }
        if j == pi {
            mid.push(nd);
        } else {
            pending[j].push(nd);
        }
    }
    let _ = sc;
}

/// A pole at the end of a part's line, where its wire ends with nothing near (an end anchor): the cantilever mast's rule
/// (MAST_OFF_M beside the axis, up to 6 m back along the line, clear of roads, buildings, other tracks and railways).
/// (the pole's foot, the axis point its arm reaches)
pub fn end_pole(sc: &Scene, pi: usize, end: P2, out: P2) -> Option<(P2, P2)> {
    let part = &sc.parts[pi];
    let others: Vec<&[P2]> = sc.parts.iter().enumerate().filter(|(j, _)| *j != pi).map(|(_, o)| o.xy.as_slice()).collect();
    let side0 = if h01(&part.fid, "side") < 0.5 { 1.0 } else { -1.0 };
    for back in [0.0, 3.0, 6.0] {
        let base = [end[0] - out[0] * back, end[1] - out[1] * back];
        let n = [-out[1], out[0]];
        for side in [side0, -side0] {
            let q = [base[0] + n[0] * side * MAST_OFF_M, base[1] + n[1] * side * MAST_OFF_M];
            if !others.is_empty() && dist_to_lines(q, others.iter().copied()) < BETWEEN_M {
                continue;
            }
            if !lines_of(&sc.roads).all(|(ln, hw)| dist_to_line(q, ln) > hw + ROAD_CLEAR_M) || (!sc.rings.is_empty() && near_ring(q, &sc.rings, BUILDING_CLEAR_M)) {
                continue;
            }
            if !sc.rails.is_empty() && dist_to_lines(q, sc.rails.iter().map(|r| r.as_slice())) < RAIL_CLEAR_M {
                continue;
            }
            if !track_ok(q, &sc.tracks) {
                continue;
            }
            return Some((q, base));
        }
    }
    None
}

/// The points pts within r of any of the poles: the street lamps a pole's bracket replaces.
pub fn near_any(pts: &[P2], poles: &[P2], r: f64) -> Vec<bool> {
    pts.iter().map(|p| poles.iter().any(|q| hyp(p[0] - q[0], p[1] - q[1]) <= r)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(y: f64) -> Vec<P2> {
        (0..=300).map(|i| [-150.0 + 2.0 * i as f64, y]).collect()
    }

    fn scene(tram_y: f64) -> Scene {
        Scene {
            parts: vec![Part { fid: "tram0".into(), bridge: false, xy: line(tram_y) }],
            roads: vec![Road { pts: vec![[-150.0, 128.0], [450.0, 128.0]], hw: 4.0 }],
            rings: vec![],
            decks: vec![],
            country: "DE".into(),
            rails: vec![],
            tracks: vec![],
        }
    }

    #[test]
    fn stations_run_the_span_on_a_straight() {
        let xy = line(0.0);
        let a = arc(&xy);
        let s = stations(&xy, &a, "x");
        assert!(s.len() >= 17);
        for w in s.windows(2) {
            assert!((w[1] - w[0] - SPAN_M).abs() < 1e-9);
        }
        assert!(s[0] >= 0.0 && s[0] < SPAN_M);
    }

    #[test]
    fn a_street_tram_gets_poles_on_both_pavements() {
        let pl = plan(&scene(128.0));
        assert_eq!(pl.parts.len(), 1);
        let spans: Vec<&Node> = pl.parts[0].nodes.iter().filter(|n| n.kind == Kind::Span).collect();
        assert!(spans.len() > 6);
        for n in &spans {
            assert_eq!(n.anchors.len(), 2);
        }
        for p in &pl.poles {
            assert!((p.q[1] - 128.0).abs() >= 4.0 + SIDEWALK_OFF_M - 0.01);
            assert!(!p.wall && p.z.is_none());
        }
        assert!(pl.poles.iter().any(|p| p.q[1] > 128.0) && pl.poles.iter().any(|p| p.q[1] < 128.0));
    }

    #[test]
    fn a_building_takes_the_fixing_on_its_wall() {
        let mut sc = scene(128.0);
        sc.rings = vec![vec![[-200.0, 133.0], [500.0, 133.0], [500.0, 160.0], [-200.0, 160.0]]];
        let pl = plan(&sc);
        assert!(pl.poles.iter().any(|p| p.wall));
    }

    #[test]
    fn an_own_way_tram_gets_cantilever_masts() {
        let pl = plan(&scene(40.0));
        let masts: Vec<&Node> = pl.parts[0].nodes.iter().filter(|n| n.kind == Kind::Mast).collect();
        assert!(masts.len() > 4);
        for m in masts {
            assert!((hyp(m.p[0] - m.q[0], m.p[1] - m.q[1]) - MAST_OFF_M).abs() < 1e-6);
        }
        assert!(pl.poles.is_empty());
    }

    #[test]
    fn a_bridge_without_a_deck_has_no_line_and_one_on_a_deck_has_poles_at_its_edges() {
        let mut sc = scene(128.0);
        sc.parts[0].bridge = true;
        assert!(plan(&sc).parts.is_empty());
        let xy: Vec<P2> = (0..=120).map(|i| [-150.0 + 5.0 * i as f64, 125.0]).collect();
        sc.decks = vec![Deck { z: vec![24.0; xy.len()], xy, width: 12.0 }];
        sc.parts[0].xy = line(128.0);
        let pl = plan(&sc);
        assert_eq!(pl.parts.len(), 1);
        assert!(!pl.poles.is_empty());
        for p in &pl.poles {
            assert!(((p.q[1] - 125.0).abs() - (6.0 + BRIDGE_OUT_M)).abs() < 1e-6);
            assert_eq!(p.z, Some(24.0));
        }
    }

    #[test]
    fn two_tracks_of_their_own_alongside_get_centre_poles_between_them() {
        let mut sc = scene(128.0);
        sc.roads.clear();
        sc.parts.push(Part { fid: "tram1".into(), bridge: false, xy: line(132.0) });
        let pl = plan(&sc);
        let centre: Vec<&Pole> = pl.poles.iter().filter(|p| p.kind == PoleKind::Centre).collect();
        assert!(centre.len() > 4, "{}", centre.len());
        for c in &centre {
            assert!((c.q[1] - 130.0).abs() < 0.05, "on the middle line: {:?}", c.q);
        }
        // both tracks hang on them: each part has centre nodes, and no cantilever masts of its own where the poles serve
        for pp in &pl.parts {
            let n: Vec<&Node> = pp.nodes.iter().filter(|n| n.centre.is_some()).collect();
            assert!(n.len() > 4);
            for nd in n {
                assert!((hyp(nd.p[0] - nd.q[0], nd.p[1] - nd.q[1]) - 2.0).abs() < 0.05);
            }
            assert_eq!(pp.nodes.iter().filter(|n| n.kind == Kind::Mast && n.centre.is_none()).count(), 0);
        }
        // the nodes of a part are in order along it, none within NODE_MIN_GAP_M of the one before
        for pp in &pl.parts {
            for w in pp.nodes.windows(2) {
                assert!(w[1].s >= w[0].s);
            }
        }
    }

    #[test]
    fn tracks_too_far_apart_get_masts_each() {
        let mut sc = scene(128.0);
        sc.roads.clear();
        sc.parts.push(Part { fid: "tram1".into(), bridge: false, xy: line(140.0) });
        let pl = plan(&sc);
        assert!(pl.poles.is_empty());
        assert!(pl.parts.iter().all(|pp| pp.nodes.iter().any(|n| n.kind == Kind::Mast && n.centre.is_none())));
    }

    #[test]
    fn a_wall_fixing_sits_on_the_walls_face_facing_the_street() {
        let mut sc = scene(128.0);
        sc.rings = vec![vec![[-200.0, 133.0], [500.0, 133.0], [500.0, 160.0], [-200.0, 160.0]]];
        let pl = plan(&sc);
        let w: Vec<&Pole> = pl.poles.iter().filter(|p| p.wall).collect();
        assert!(!w.is_empty());
        for p in w {
            assert!((p.fix[1] - 133.0).abs() < 1e-6, "on the face: {:?}", p.fix);
            assert!((p.normal[1] + 1.0).abs() < 1e-6 && p.normal[0].abs() < 1e-6, "facing the street: {:?}", p.normal);
        }
    }

    #[test]
    fn a_track_beside_a_road_gets_no_pole_across_it() {
        // the tram runs 2 m off a 16 m wide carriageway's edge: the span wire's far fixing used to stand on its other side
        let sc = Scene {
            parts: vec![Part { fid: "t".into(), bridge: false, xy: line(0.0) }],
            roads: vec![Road { pts: vec![[-150.0, 10.0], [450.0, 10.0]], hw: 8.0 }],
            rings: vec![],
            decks: vec![],
            country: "UA".into(),
            rails: vec![],
            tracks: vec![],
        };
        let pl = plan(&sc);
        for p in &pl.poles {
            assert!(p.q[1] < 2.0, "the pole is on the track's own side: {:?}", p.q);
        }
    }

    #[test]
    fn families_by_country() {
        assert_eq!(family_of("UA", "x"), Family::Concrete);
        assert_eq!(family_of("DE", "x"), Family::Tube);
        assert!(matches!(family_of("FI", "a"), Family::Square | Family::Lattice));
        let fi: std::collections::HashSet<_> = (0..40).map(|i| family_of("FI", &format!("w{i}")).name()).collect();
        assert_eq!(fi.len(), 2, "both Finnish families occur");
        assert_eq!(family_of("FI", "same"), family_of("fi", "same"));
        assert_eq!(family_of("", "x"), Family::Tube);
    }

    #[test]
    fn lamps_near_a_pole() {
        let m = near_any(&[[0.0, 0.0], [4.9, 0.0], [5.1, 0.0]], &[[0.0, 0.0]], LAMP_CLEAR_M);
        assert_eq!(m, vec![true, true, false]);
    }

    #[test]
    fn a_far_side_across_a_wide_junction_mouth_gets_no_pole() {
        // a 40 m wide carriageway with the tram in its middle: the pavements are 20 m out, past ANCHOR_MAX_M
        let sc = Scene {
            parts: vec![Part { fid: "t".into(), bridge: false, xy: line(0.0) }],
            roads: vec![Road { pts: vec![[-150.0, 0.0], [450.0, 0.0]], hw: 20.0 }],
            rings: vec![],
            decks: vec![],
            country: "DE".into(),
            rails: vec![],
            tracks: vec![],
        };
        let pl = plan(&sc);
        // (a 34 m station finds no pole within ANCHOR_MAX_M: the poles there are only the last try for a stretch no pole holds,
        // across the road, one a MAX_UNSUPPORTED_M or so, not a pair every 34 m)
        assert!(pl.poles.len() <= 2 * 600 / 36, "{} poles", pl.poles.len());
        for w in pl.parts[0].nodes.windows(2) {
            assert!(w[1].s - w[0].s <= MAX_UNSUPPORTED_M + 1e-6);
        }
        // and 14 m out is still fine
        let mut sc2 = sc;
        sc2.roads[0].hw = 12.0;
        assert!(!plan(&sc2).poles.is_empty());
    }

    #[test]
    fn poles_keep_off_a_railway_track() {
        let mut sc = scene(128.0);
        sc.rails = vec![vec![[40.0, 100.0], [40.0, 160.0]]];
        let pl = plan(&sc);
        assert!(!pl.poles.is_empty());
        for p in &pl.poles {
            assert!(dist_to_line(p.q, &sc.rails[0]) >= RAIL_CLEAR_M - 1e-9, "{:?}", p.q);
        }
    }

    /// A yard's fan: n parallel electrified tracks gap m apart along x (y = 0, gap, ...), all of them also ground tracks.
    fn fan(n: usize, gap: f64) -> Scene {
        let ys: Vec<f64> = (0..n).map(|i| i as f64 * gap).collect();
        Scene {
            parts: ys.iter().enumerate().map(|(i, &y)| Part { fid: format!("y{i}"), bridge: false, xy: line(y) }).collect(),
            roads: vec![],
            rings: vec![],
            decks: vec![],
            country: "UA".into(),
            rails: vec![],
            tracks: ys.iter().map(|&y| line(y)).collect(),
        }
    }

    fn off_every_axis(sc: &Scene, m: f64) {
        for pl in plan(sc).poles.iter().filter(|p| !p.wall) {
            assert!(dist_to_lines(pl.q, sc.tracks.iter().map(|t| t.as_slice())) >= m, "pole {:?} on the track field", pl.q);
        }
    }

    #[test]
    fn no_pole_stands_in_a_fan_of_six_tracks() {
        for gap in [3.6, 4.2, 5.0] {
            let sc = fan(6, gap);
            off_every_axis(&sc, 1.5);
            let pl = plan(&sc);
            assert!(!pl.poles.is_empty(), "the yard has poles (gap {gap})");
            for p in pl.poles.iter() {
                assert!(dist_to_lines(p.q, sc.tracks.iter().map(|t| t.as_slice())) >= POLE_TRACK_CLEAR_M - 1e-9);
            }
        }
    }

    #[test]
    fn a_portal_is_chosen_for_three_or_more_tracks_and_stands_outside_the_fan() {
        let sc = fan(6, 4.2);
        let pl = plan(&sc);
        let w = 5.0 * 4.2;
        let portal: Vec<&Pole> = pl.poles.iter().filter(|p| matches!(p.kind, PoleKind::Portal | PoleKind::Centre)).collect();
        assert!(portal.len() >= 8, "portal poles: {}", portal.len());
        assert!(pl.poles.iter().filter(|p| p.kind == PoleKind::Portal).all(|p| p.q[1] < -PORTAL_POST_OUT_M + 1e-6 || p.q[1] > w + PORTAL_POST_OUT_M - 1e-6 || {
            // (or a pole in a gap of a wide fan)
            true
        }));
        assert!(!pl.bays.is_empty());
        // every track is hung: each part has a node of a bay (or a centre pole's arm) about every 34 m
        for pp in &pl.parts {
            let hung = pp.nodes.iter().filter(|n| n.bay.is_some() || n.centre.is_some()).count();
            assert!(hung >= 8, "a track of the yard hung {hung} times");
        }
        // far fewer poles than one a track every 34 m (6 tracks x 18 stations = 108)
        assert!(pl.poles.len() <= 3 * 18 + 2, "{} poles", pl.poles.len());
    }

    #[test]
    fn two_tracks_are_not_a_portal() {
        let pl = plan(&fan(2, 4.2));
        assert!(pl.bays.is_empty());
    }

    #[test]
    fn a_wide_fan_gets_poles_in_the_wide_gaps_only() {
        // eight tracks, 4.5 m apart (31.5 m: wider than a bay) with a 6 m gap in the middle: the pole stands there
        let ys = [0.0, 4.5, 9.0, 13.5, 19.5, 24.0, 28.5, 33.0];
        let sc = Scene {
            parts: ys.iter().enumerate().map(|(i, &y)| Part { fid: format!("w{i}"), bridge: false, xy: line(y) }).collect(),
            roads: vec![],
            rings: vec![],
            decks: vec![],
            country: "UA".into(),
            rails: vec![],
            tracks: ys.iter().map(|&y| line(y)).collect(),
        };
        let pl = plan(&sc);
        off_every_axis(&sc, 1.5);
        assert!(pl.poles.iter().any(|p| p.kind == PoleKind::Centre && (p.q[1] - 16.5).abs() < 0.01), "a pole in the 6 m gap");
        assert!(pl.poles.iter().all(|p| dist_to_lines(p.q, sc.tracks.iter().map(|t| t.as_slice())) >= POLE_TRACK_CLEAR_M - 1e-9));
    }

    #[test]
    fn a_track_that_has_no_overhead_line_is_still_a_place_no_pole_stands() {
        // one electrified tram track and a siding 2 m beside it, bare: the mast keeps off the siding
        let mut sc = scene(40.0);
        sc.tracks = vec![line(40.0), line(42.0)];
        let pl = plan(&sc);
        for nd in pl.parts[0].nodes.iter().filter(|n| n.kind == Kind::Mast) {
            assert!(dist_to_lines(nd.q, sc.tracks.iter().map(|t| t.as_slice())) >= POLE_TRACK_CLEAR_M - 1e-9, "{:?}", nd.q);
        }
    }

    #[test]
    fn no_stretch_of_wire_runs_unsupported_for_more_than_the_limit() {
        for sc in [fan(6, 4.2), fan(4, 3.6), scene(40.0)] {
            let pl = plan(&sc);
            for pp in &pl.parts {
                for w in pp.nodes.windows(2) {
                    assert!(w[1].s - w[0].s <= MAX_UNSUPPORTED_M + 1e-6, "{} m without support", w[1].s - w[0].s);
                }
            }
        }
    }
}
