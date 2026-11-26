//! Snowy roads, sidewalks and snowbanks (winter W3, BNE_WINTER; ~/bnge-tests/winterplan/PLAN.md 2.7, 2.8, 3.3, 3.4):
//! what the W1 kernel's road state (snow.rs RoadOut per plow class) looks like on our roads.
//!
//! The lane sheet: a road is a centre line with a half width, its plow class P0..P4, its lanes and whether it is one
//! way. From it, per photo texel (the city carriageways are terrain under the ground photo: winter PROBES.md P10), a
//! zone: the lanes, the two wheel tracks of each lane, and the edge (the outer metre each side and the strip between
//! the directions, where the snow is thrown and churned). The ground photo's snow (snowlook.rs) takes the share of the
//! ground's cover each (class, zone) keeps (zone_shares). The same sheet gives the snowbanks along plowed roads (banks:
//! never across junctions, crossings or bridge decks: the lines given are the roads' ground parts) and the class of
//! any point (class_at: the markings', decks' and sidewalks' winter material buckets).
//!
//! The state's look for the mod (look): per class the share of the cover in each zone, how much of the paint still
//! shows (none under a full cover, some under tracks), how white a bridge deck is, the bank bin (height) and dirt bin
//! (white, grey, brown-black), and the sidewalks' states (shovelled, trampled or covered by street kind).
//!
//! Integers where it is state (shares, bins, permille: the same on every machine); the geometry is f64 with plain
//! arithmetic and sqrt only (IEEE exact: no libm), in a fixed order.

use crate::snow::{self, RoadOut, CLASSES};

pub const ZONES: usize = 3;
pub const Z_LANE: usize = 0;
pub const Z_TRACK: usize = 1;
pub const Z_EDGE: usize = 2;
/// (class, zone) slots of the ground photo's road mask: texel value 1 + zone * CLASSES + class (0: not a road)
pub const SLOTS: usize = CLASSES * ZONES;
pub const NO_CLASS: u8 = 255;

/// Plow class P0..P4 of a road class name (roads.Spec.cls: OpenStreetMap's highway value, links folded in) (plan 2.7).
pub fn plow_class(cls: &str) -> u8 {
    let c = cls.strip_suffix("_link").unwrap_or(cls);
    match c {
        "motorway" | "trunk" => 0,
        "primary" | "secondary" => 1,
        "tertiary" | "unclassified" | "road" | "unknown" | "" => 2,
        "residential" | "living_street" => 3,
        _ => 4,
    }
}

/// The shares (eighths of the ground's cover) a road keeps in its lanes, wheel tracks and at its edge, from its row:
/// condition, wheel-track progress (permille) and the edge's condition. Loose snow is white where nobody drives and
/// packed grey in the tracks; packed snow polishes darker in them; a dusting or slush is churned to wet black tracks;
/// the edge holds what the plow and the traffic threw aside, never less than the lanes.
pub fn zone_shares(cond: u8, track: i64, edge_cond: u8) -> [i64; ZONES] {
    let lane = snow::road_share(cond);
    let t = track.clamp(0, 1000);
    let tr = match cond {
        snow::LOOSE => 8 - (t * 4 + 500) / 1000,
        snow::PACKED => 6 - (t * 4 + 500) / 1000,
        snow::PACKED_ICY => 5 - (t * 3 + 500) / 1000,
        snow::DUSTING => 5 - (t * 5 + 500) / 1000,
        snow::SLUSH => 3 - (t * 3 + 500) / 1000,
        _ => 0,
    };
    let edge = match edge_cond {
        snow::LOOSE => 8,
        snow::DUSTING => 6,
        snow::SLUSH => 5,
        snow::PACKED => 6,
        _ => 0,
    };
    [lane, tr.clamp(0, lane), edge.max(lane)]
}

/// Permille of a road's paint that shows (markings, crossings): hidden as the lanes' snow covers it, but the tracks
/// keep it readable (half of what shows there). cover: the ground's cover 0..8 (the photo's shares multiply it).
pub fn marking_vis(shares: &[i64; ZONES], cover: i64) -> i64 {
    let c = cover.clamp(0, 8);
    let lane = shares[Z_LANE] * c; // 0..64
    let track = shares[Z_TRACK] * c;
    let v_lane = (64 - lane) * 1000 / 64;
    let v_track = (64 - track) * 500 / 64;
    v_lane.max(v_track).clamp(0, 1000)
}

/// Typical carriageway width per class (dm): the snow a plow moves comes from this width (banks per side).
pub const ROAD_W_DM: [i64; CLASSES] = [110, 90, 75, 70, 50];
/// Bank bin lower edges (mm of bank height): 1 from 15 cm, 2 from 45 cm, 3 from 80 cm, 4 from 1.25 m; drawn at
/// BANK_DRAW_MM (plan 3.3: 0.3, 0.6, 1.0, 1.5 m).
pub const BANK_EDGE_MM: [i64; 4] = [150, 450, 800, 1250];
pub const BANK_DRAW_MM: [i64; 5] = [0, 300, 600, 1000, 1500];

/// A bank's height (mm) from the kernel's banks (0.01 mm of water plowed off a class's lanes): per side half the
/// width's snow at 400 kg/m3, piled in a rounded ridge of cross-section ~1.5 h^2 (its base 2.4 h).
pub fn bank_height_mm(banks: i64, class: usize) -> i64 {
    let w = ROAD_W_DM[class.min(CLASSES - 1)];
    // volume per metre of edge, mm2: (banks / 100 mm water) * (w / 2 dm * 100 mm) / 0.4
    let vol = banks.max(0) * w * 50 / 100 * 10 / 4;
    snow::isqrt(vol * 2 / 3)
}

/// The bank bin 0..4 of a height (mm), moving from prev (-1: none) only when the height passes an edge by 15 %.
pub fn bank_bin(h_mm: i64, prev: i64) -> i64 {
    let raw = BANK_EDGE_MM.iter().filter(|&&e| h_mm >= e).count() as i64;
    if !(0..=4).contains(&prev) {
        return raw;
    }
    let mut b = prev;
    while b < 4 && h_mm * 100 >= BANK_EDGE_MM[b as usize] * 115 {
        b += 1;
    }
    while b > 0 && h_mm * 100 < BANK_EDGE_MM[(b - 1) as usize] * 85 {
        b -= 1;
    }
    b
}

/// The dirt bin 0..2 (white, grey, brown-black) of a bank by the hours since the last snowfall: full in 3 days beside
/// salted roads, a step cleaner on the unsalted ones [R1].
pub fn dirt_bin(age_h: i64, salted: bool) -> i64 {
    let d = if age_h < 24 { 0 } else if age_h < 72 { 1 } else { 2 };
    if salted { d } else { (d - 1).max(0) }
}

/// Sidewalk states (plan 2.8).
pub const W_COVERED: i64 = 0;
pub const W_TRAMPLED: i64 = 1;
pub const W_SHOVELLED: i64 = 2;
pub const W_CLEAR: i64 = 3;
pub const W_ICY: i64 = 4;
/// Sidewalk slots: S0 town centre / main streets, S1 residential frontages cleared, S1 left, S2 unmaintained.
pub const WALKS: usize = 4;

/// The sidewalks' (state, cover 0..8) per slot from the ground's depth (0.1 mm), cover 0..8, hours since the last
/// snowfall, crust and air temperature (0.01 degC): S0 cleared within 4 h (trampled before), S1's cleared frontages
/// within 12 h, the rest trampled to a path in 6 h, S2 trampled after a day; a cleared walk refreezes icy below 0 degC
/// with a crust. Never whiter than the ground.
pub fn walk_states(depth: i64, cover: i64, age_h: i64, crust: bool, t_air: i64) -> [[i64; 2]; WALKS] {
    let c = cover.clamp(0, 8);
    if depth < 5 || c == 0 {
        return [[W_CLEAR, 0]; WALKS];
    }
    let cleared = |after: i64, before: [i64; 2]| -> [i64; 2] {
        if age_h >= after {
            if crust && t_air < 0 { [W_ICY, 2.min(c)] } else { [W_SHOVELLED, 1.min(c)] }
        } else {
            before
        }
    };
    let s0 = cleared(4, [W_TRAMPLED, 5]);
    let s1c = cleared(12, [W_TRAMPLED, 6]);
    let s1u = if age_h >= 6 { [W_TRAMPLED, 7] } else { [W_COVERED, 8] };
    let s2 = if age_h >= 24 { [W_TRAMPLED, 7] } else { [W_COVERED, 8] };
    let mut out = [s0, s1c, s1u, s2];
    for o in out.iter_mut() {
        o[1] = o[1].min(c);
    }
    out
}

/// Residential frontages whose sidewalk is cleared, permille (plan 2.8: "many never cleared").
pub const WALK_CLEARED_PERMILLE: u64 = 600;

/// A block face's fixed draw in [0, 2^32): BLAKE2s-32 of "fid|side" (little-endian), the same on every machine.
pub fn face_draw(fid: &str, side: i8) -> u64 {
    use blake2::digest::consts::U4;
    use blake2::{Blake2s, Digest};
    let mut h = Blake2s::<U4>::new();
    h.update(format!("{fid}|{side}").as_bytes());
    let d = h.finalize();
    u32::from_le_bytes([d[0], d[1], d[2], d[3]]) as u64
}

/// The sidewalk slot (0 S0 main streets, 1 S1 cleared, 2 S1 left, 3 S2 unmaintained) of a slab beside a road of
/// plow class cls (NO_CLASS: none found), its feature and side: P0-P1 S0; P2-P3 residential, cleared on
/// WALK_CLEARED_PERMILLE of the block faces by face_draw; the rest S2.
pub fn walk_slot(cls: u8, fid: &str, side: i8) -> u8 {
    match cls {
        0 | 1 => 0,
        2 | 3 => {
            if face_draw(fid, side) * 1000 < WALK_CLEARED_PERMILLE << 32 { 1 } else { 2 }
        }
        _ => 3,
    }
}

/// Cut discs (x, y, r) of crossings: each polyline's centre (the mean of its points) and its farthest point from it
/// plus half its width and BANK_CLEAR_M.
pub fn crossing_discs(lines: &[Vec<[f64; 2]>], widths: &[f64]) -> Vec<[f64; 3]> {
    lines.iter().zip(widths).filter(|(l, _)| !l.is_empty()).map(|(l, w)| {
        let n = l.len() as f64;
        let (mut cx, mut cy) = (0.0, 0.0);
        for p in l.iter() {
            cx += p[0];
            cy += p[1];
        }
        let (cx, cy) = (cx / n, cy / n);
        let r = l.iter().map(|p| ((p[0] - cx) * (p[0] - cx) + (p[1] - cy) * (p[1] - cy)).sqrt()).fold(0.0, f64::max);
        [cx, cy, r + 0.5 * w + BANK_CLEAR_M]
    }).collect()
}

/// The road look of a state, for the mod and the photos.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Look {
    pub shares: [[i64; ZONES]; CLASSES],        // ground photo: eighths per class and zone
    pub deck_shares: [[i64; ZONES]; CLASSES],   // bridge decks
    pub marking: [i64; CLASSES],                // permille of the paint shown
    pub deck_cover: [i64; CLASSES],             // permille of a deck's surface under snow
    pub walks: [[i64; 2]; WALKS],               // (state, cover 0..8)
    pub bank_bin: [i64; CLASSES],
    pub dirt: [i64; CLASSES],
}

/// The look of a state: road rows (P0..P4 then the bridges), the ground's depth (0.1 mm), cover, age (h), crust, air
/// temperature, the regime, the previous bank bins (-1: none; hysteresis).
pub fn look(roads: &[RoadOut], depth: i64, cover: i64, age_h: i64, crust: bool, t_air: i64, regime: i64,
            prev_banks: &[i64]) -> Look {
    let mut l = Look::default();
    let c = cover.clamp(0, 8);
    for k in 0..CLASSES {
        let r = roads.get(k).copied().unwrap_or_default();
        l.shares[k] = zone_shares(r.cond, r.track, r.edge_cond);
        let b = roads.get(CLASSES + k).copied().unwrap_or(r);
        l.deck_shares[k] = zone_shares(b.cond, b.track, b.edge_cond);
        l.marking[k] = marking_vis(&l.shares[k], c);
        l.deck_cover[k] = l.deck_shares[k][Z_LANE] * c * 1000 / 64;
        let banks = if snow::plowed_class(regime, k) { r.banks } else { 0 };
        let prev = prev_banks.get(k).copied().unwrap_or(-1);
        l.bank_bin[k] = if c == 0 { 0 } else { bank_bin(bank_height_mm(banks, k), prev) };
        l.dirt[k] = dirt_bin(age_h, snow::salted_class(regime, k));
    }
    l.walks = walk_states(depth, c, age_h, crust, t_air);
    l
}

// ---------------------------------------------------------------------------------------------------------------------
// The lane sheet's geometry

/// m of the edge zone (at most 0.3 of the half width), the strip between the directions (half of it), a wheel track's
/// half width and its offset from the lane's centre (a car's track ~1.55 m) [guess, from the photos of plowed
/// streets: Minneapolis, Montreal].
pub const EDGE_M: f64 = 0.8;
pub const CENTRE_HW_M: f64 = 0.3;
pub const TRACK_HW_M: f64 = 0.35;     // (the worn path: tyres and their wander)
pub const GAUGE_HALF_M: f64 = 0.78;
pub const LANE_MIN_M: f64 = 2.5;
pub const LANE_MAX_M: f64 = 4.6;
pub const LANE_TYPICAL_M: f64 = 3.3;

/// The zone of a point lat m left of a road's centre line (half width hw m, lanes, one way), at texel size texel m
/// (a narrow strip is drawn at least half a texel wide each side: unbroken one-texel lines).
pub fn zone_of(lat: f64, hw: f64, lanes: u8, oneway: bool, texel: f64) -> usize {
    let a = lat.abs();
    let edge_w = EDGE_M.min(0.3 * hw);
    if a > hw - edge_w {
        return Z_EDGE;
    }
    let half_px = 0.5 * texel;
    let mut n = (lanes as f64).max(1.0);
    let w = 2.0 * hw;
    // (mapped lanes kept unless they cannot be: narrower than LANE_MIN_M, or wider than two lanes (a parking lane
    // beside each is common: Minneapolis' 10 m residential streets are two 5 m "lanes" driven near their middles)
    if w / n < LANE_MIN_M {
        n = (w / LANE_TYPICAL_M).floor().max(1.0);
    } else if w / n > 2.0 * LANE_MAX_M {
        n = (w / LANE_TYPICAL_M + 0.5).floor().max(1.0);
    }
    if !oneway && n >= 2.0 && a < CENTRE_HW_M.max(half_px) {
        return Z_EDGE;
    }
    let lw = w / n;
    let pos = (lat + hw).clamp(0.0, w);
    let i = (pos / lw).floor().min(n - 1.0);
    let off = (pos - (i + 0.5) * lw).abs();
    if (off - GAUGE_HALF_M).abs() <= TRACK_HW_M.max(half_px) && lw >= 2.0 * (GAUGE_HALF_M + TRACK_HW_M) {
        return Z_TRACK;
    }
    Z_LANE
}

/// The ground photo's road mask (px x px, row 0 north): per texel 1 + zone * CLASSES + class, 0 off the roads, from
/// the painted carriageways' segments in pixel coordinates (col, row) with their half widths (px), each segment's road
/// number (surface._road_segments' roads_of: one road's segments share it), class (NO_CLASS: P2), lanes and one way.
/// Where two roads cover a texel it is a junction: their lower class number, no tracks or edge (churned).
#[allow(clippy::too_many_arguments)]
pub fn zone_raster(segs: &[[f64; 4]], hw: &[f64], road: &[i64], cls: &[u8], lanes: &[u8], oneway: &[bool], px: usize,
                   texel_m: f64) -> Vec<u8> {
    let n = px * px;
    let mut code = vec![0u8; n];
    let mut owner = vec![i64::MIN; n];
    let mut dn = vec![f64::INFINITY; n];
    const JUNCTION: i64 = i64::MIN + 1;
    let m = segs.len().min(hw.len()).min(road.len()).min(cls.len()).min(lanes.len()).min(oneway.len());
    for i in 0..m {
        let [ax, ay, bx, by] = segs[i];
        let h = hw[i];
        if !(h > 0.0) || !ax.is_finite() || !ay.is_finite() || !bx.is_finite() || !by.is_finite() {
            continue;
        }
        let c = if cls[i] as usize >= CLASSES { 2u8 } else { cls[i] };
        let (dx, dy) = (bx - ax, by - ay);
        let len2 = dx * dx + dy * dy;
        let len = len2.sqrt();
        let c0 = ((ax.min(bx) - h).floor().max(0.0)) as usize;
        let c1 = ((ax.max(bx) + h).ceil().min(px as f64)) as usize;
        let r0 = ((ay.min(by) - h).floor().max(0.0)) as usize;
        let r1 = ((ay.max(by) + h).ceil().min(px as f64)) as usize;
        let hw_m = h * texel_m;
        for r in r0..r1 {
            let py = r as f64 + 0.5;
            for col in c0..c1 {
                let pxx = col as f64 + 0.5;
                let (vx, vy) = (pxx - ax, py - ay);
                let t = if len2 > 0.0 { ((vx * dx + vy * dy) / len2).clamp(0.0, 1.0) } else { 0.0 };
                let (qx, qy) = (ax + t * dx - pxx, ay + t * dy - py);
                let d = (qx * qx + qy * qy).sqrt();
                if d > h {
                    continue;
                }
                // (rows run south: a positive cross product is to the right of the direction of travel in the
                // picture, left in the world)
                let side = if len > 0.0 { if dx * vy - dy * vx > 0.0 { 1.0 } else { -1.0 } } else { 1.0 };
                let k = r * px + col;
                let me = road[i];
                if owner[k] == i64::MIN || (owner[k] == me && d / h < dn[k]) {
                    let z = zone_of(side * d * texel_m, hw_m, lanes[i], oneway[i], texel_m);
                    code[k] = 1 + (z * CLASSES) as u8 + c;
                    owner[k] = me;
                    dn[k] = d / h;
                } else if owner[k] != me {
                    let prev = (code[k] - 1) % CLASSES as u8;
                    code[k] = 1 + prev.min(c);
                    owner[k] = JUNCTION;
                }
            }
        }
    }
    code
}

/// A road line set: points, each line's offsets into them, half width (m), class, lanes, one way.
pub struct Lines<'a> {
    pub pts: &'a [[f64; 2]],
    pub off: &'a [usize],
    pub hw: &'a [f64],
    pub cls: &'a [u8],
}

impl Lines<'_> {
    fn count(&self) -> usize {
        self.off.len().saturating_sub(1).min(self.hw.len()).min(self.cls.len())
    }
    fn line(&self, l: usize) -> &[[f64; 2]] {
        &self.pts[self.off[l].min(self.pts.len())..self.off[l + 1].min(self.pts.len())]
    }
}

/// Distance from p to segment a-b, and the side (+1 left of a->b, -1 right).
fn seg_dist(p: [f64; 2], a: [f64; 2], b: [f64; 2]) -> (f64, f64) {
    let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
    let (vx, vy) = (p[0] - a[0], p[1] - a[1]);
    let len2 = dx * dx + dy * dy;
    let t = if len2 > 0.0 { ((vx * dx + vy * dy) / len2).clamp(0.0, 1.0) } else { 0.0 };
    let (qx, qy) = (a[0] + t * dx - p[0], a[1] + t * dy - p[1]);
    let side = if dx * vy - dy * vx >= 0.0 { 1.0 } else { -1.0 };
    ((qx * qx + qy * qy).sqrt(), side)
}

/// A uniform grid over line segments (cell GRID_M): the segments near a point.
const GRID_M: f64 = 24.0;
struct SegGrid {
    x0: f64,
    y0: f64,
    nx: usize,
    ny: usize,
    cells: Vec<Vec<(u32, u32)>>, // (line, first point of the segment)
}

impl SegGrid {
    fn new(lines: &Lines, pad: f64) -> SegGrid {
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for p in lines.pts {
            if p[0].is_finite() && p[1].is_finite() {
                x0 = x0.min(p[0]);
                y0 = y0.min(p[1]);
                x1 = x1.max(p[0]);
                y1 = y1.max(p[1]);
            }
        }
        if !x0.is_finite() {
            return SegGrid { x0: 0.0, y0: 0.0, nx: 0, ny: 0, cells: Vec::new() };
        }
        // (the grid reaches as far as any segment's half width + pad: a point off a wide deck's end, inside its
        // half width but past its points' box, found nothing and a bank stood under the deck, Hanger Lane)
        let reach = pad + lines.hw.iter().take(lines.count()).fold(0.0f64, |a, &h| if h.is_finite() { a.max(h) } else { a });
        x0 -= reach;
        y0 -= reach;
        let nx = (((x1 + reach - x0) / GRID_M).ceil() as usize).clamp(1, 4096);
        let ny = (((y1 + reach - y0) / GRID_M).ceil() as usize).clamp(1, 4096);
        let mut cells = vec![Vec::new(); nx * ny];
        for l in 0..lines.count() {
            let ln = lines.line(l);
            let r = lines.hw[l] + pad;
            for i in 0..ln.len().saturating_sub(1) {
                let (a, b) = (ln[i], ln[i + 1]);
                let gx0 = (((a[0].min(b[0]) - r - x0) / GRID_M).floor().max(0.0) as usize).min(nx - 1);
                let gx1 = (((a[0].max(b[0]) + r - x0) / GRID_M).floor().max(0.0) as usize).min(nx - 1);
                let gy0 = (((a[1].min(b[1]) - r - y0) / GRID_M).floor().max(0.0) as usize).min(ny - 1);
                let gy1 = (((a[1].max(b[1]) + r - y0) / GRID_M).floor().max(0.0) as usize).min(ny - 1);
                for gy in gy0..=gy1 {
                    for gx in gx0..=gx1 {
                        cells[gy * nx + gx].push((l as u32, i as u32));
                    }
                }
            }
        }
        SegGrid { x0, y0, nx, ny, cells }
    }

    fn near(&self, p: [f64; 2]) -> &[(u32, u32)] {
        if self.nx == 0 {
            return &[];
        }
        let gx = (p[0] - self.x0) / GRID_M;
        let gy = (p[1] - self.y0) / GRID_M;
        if gx < 0.0 || gy < 0.0 || gx >= self.nx as f64 || gy >= self.ny as f64 {
            return &[];
        }
        &self.cells[gy as usize * self.nx + gx as usize]
    }
}

/// The nearest line of each point within its half width + extra m: (line or -1, its class or NO_CLASS, side +1 left
/// / -1 right of it). Ties go to the lower line index (a fixed order).
pub fn class_at(points: &[[f64; 2]], lines: &Lines, extra: f64) -> Vec<(i32, u8, i8)> {
    let grid = SegGrid::new(lines, extra);
    points.iter().map(|&p| {
        let mut best = (f64::INFINITY, -1i32, 1.0f64);
        for &(l, i) in grid.near(p) {
            let ln = lines.line(l as usize);
            let (d, side) = seg_dist(p, ln[i as usize], ln[i as usize + 1]);
            if d <= lines.hw[l as usize] + extra && (d < best.0 || (d == best.0 && (l as i32) < best.1)) {
                best = (d, l as i32, side);
            }
        }
        if best.1 < 0 { (-1, NO_CLASS, 0) } else { (best.1, lines.cls[best.1 as usize], best.2 as i8) }
    }).collect()
}

/// Junction points of a line set: a point where line ends and inner vertices of different lines meet (within 0.5 m)
/// with ends + 2 x inner vertices >= 3 (a road continuing in another feature is 2 ends: no junction), and its radius
/// (the widest half width there + JUNCTION_CLEAR_M: the corner's curb return).
pub const JUNCTION_CLEAR_M: f64 = 4.0;
/// cos 35 degrees: two features meeting end to end bend more than this at a corner (a junction for the banks)
pub const CORNER_COS: f64 = 0.819;

fn close2(a: [f64; 2], b: [f64; 2]) -> bool {
    (a[0] - b[0]) * (a[0] - b[0]) + (a[1] - b[1]) * (a[1] - b[1]) <= 0.25
}

pub fn junctions(lines: &Lines) -> Vec<([f64; 2], f64)> {
    // every vertex with its line, whether it is an end, the line's half width
    let mut v: Vec<([f64; 2], usize, bool)> = Vec::new();
    for l in 0..lines.count() {
        let ln = lines.line(l);
        for (i, p) in ln.iter().enumerate() {
            v.push((*p, l, i == 0 || i + 1 == ln.len()));
        }
    }
    // sort by a 1 m cell then by coordinates (a fixed order)
    let key = |p: &[f64; 2]| ((p[0].floor()) as i64, (p[1].floor()) as i64);
    let mut idx: Vec<usize> = (0..v.len()).collect();
    idx.sort_by(|&a, &b| key(&v[a].0).cmp(&key(&v[b].0)).then(a.cmp(&b)));
    let mut cell_of: std::collections::BTreeMap<(i64, i64), Vec<usize>> = std::collections::BTreeMap::new();
    for &i in &idx {
        cell_of.entry(key(&v[i].0)).or_default().push(i);
    }
    let mut out: Vec<([f64; 2], f64)> = Vec::new();
    let mut taken = vec![false; v.len()];
    for &i in &idx {
        if taken[i] {
            continue;
        }
        let p = v[i].0;
        let (cx, cy) = key(&p);
        let mut members: Vec<usize> = Vec::new();
        for dx in -1..=1 {
            for dy in -1..=1 {
                if let Some(list) = cell_of.get(&(cx + dx, cy + dy)) {
                    for &j in list {
                        let q = v[j].0;
                        let (ex, ey) = (q[0] - p[0], q[1] - p[1]);
                        if ex * ex + ey * ey <= 0.25 {
                            members.push(j);
                        }
                    }
                }
            }
        }
        members.sort_unstable();
        // per line: an end counts 1, an inner vertex 2 (a line passing through)
        let mut per: Vec<(usize, i32)> = Vec::new();
        for &j in &members {
            let w = if v[j].2 { 1 } else { 2 };
            match per.iter_mut().find(|e| e.0 == v[j].1) {
                Some(e) => e.1 = e.1.max(w),
                None => per.push((v[j].1, w)),
            }
        }
        let score: i32 = per.iter().map(|e| e.1).sum();
        // (two ends meeting at a corner, one road turning into another feature: their bodies overlap inside the bend
        // as at a junction)
        let corner = per.len() == 2 && score == 2 && {
            let dir = |l: usize| -> [f64; 2] {
                let ln = lines.line(l);
                let (a, b) = if close2(ln[0], p) { (ln[0], ln[1.min(ln.len() - 1)]) }
                             else { (ln[ln.len() - 1], ln[ln.len().saturating_sub(2)]) };
                let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
                let n = (dx * dx + dy * dy).sqrt().max(1e-9);
                [dx / n, dy / n]
            };
            let (a, b) = (dir(per[0].0), dir(per[1].0));
            // straight on: the two leave the point in opposite directions (dot -1); a bend of more than 35 degrees
            a[0] * b[0] + a[1] * b[1] > -CORNER_COS
        };
        if per.len() >= 2 && (score >= 3 || corner) {
            let r = per.iter().map(|e| lines.hw[e.0]).fold(0.0, f64::max) + JUNCTION_CLEAR_M;
            out.push((p, r));
            for &j in &members {
                taken[j] = true;
            }
        }
    }
    out
}

/// Whether a line's end is open (a bank tapers there): no other banked line's end or vertex within 0.5 m (a road
/// carried on as a service road or track, never plowed, ends the bank as a dead end does).
fn end_open(lines: &Lines, l: usize, p: [f64; 2], height: &[f64; CLASSES]) -> bool {
    for o in 0..lines.count() {
        let c = lines.cls[o] as usize;
        if o == l || c >= CLASSES || !(height[c] > 0.0) {
            continue;
        }
        for q in lines.line(o) {
            let (ex, ey) = (q[0] - p[0], q[1] - p[1]);
            if ex * ex + ey * ey <= 0.25 {
                return false;
            }
        }
    }
    true
}

/// A deterministic value in [0, 1) at an integer cell (a bank's crest varies along it).
fn hash01(a: i64, b: i64) -> f64 {
    let mut h = (a as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15) ^ (b as u64).wrapping_mul(0xC2B2_AE3D_27D4_EB4F);
    h ^= h >> 33;
    h = h.wrapping_mul(0xFF51_AFD7_ED55_8CCD);
    h ^= h >> 33;
    (h >> 11) as f64 / (1u64 << 53) as f64
}

/// The bank mesh: (xy (n, 2) frame m, height above the ground (n), triangles (m, 3), uv (n, 2), class per triangle).
pub struct BankMesh {
    pub xy: Vec<[f64; 2]>,
    pub h: Vec<f64>,
    pub tris: Vec<[u32; 3]>,
    pub uv: Vec<[f32; 2]>,
    pub tri_cls: Vec<u8>,
}

pub const BANK_STEP_M: f64 = 2.5;
pub const BANK_INSET_M: f64 = 0.15;   // the bank's inner foot this far inside the carriageway's edge (the gutter)
pub const BANK_TAPER_M: f64 = 3.0;
pub const BANK_CLEAR_M: f64 = 1.0;    // ... and no bank within this of another road's carriageway
pub const BANK_END_M: f64 = 4.0;      // ... nor this near an open end
pub const DUP_M: f64 = 1.0;           // a line within this of another everywhere is a copy of it
pub const BANK_FOLD_M: f64 = 0.3;     // a bank point this far inside its own road's carriageway: folded (a tight bend)
pub const BANK_PROFILE: [(f64, f64); 5] = [(0.0, 0.0), (0.22, 0.62), (0.5, 1.0), (0.78, 0.62), (1.0, 0.0)];

/// Snowbanks along the plowed roads' edges: lines (the roads' ground parts: none on decks or in tunnels), the drawn
/// height (m) per class (0: none), cut discs (x, y, r: crossings, a driveway's mouth), cut lines `over` (the bridge
/// decks: none at a deck's abutment or under a deck overhead, BANK_CLEAR_M past its half width) and the junctions of
/// the lines themselves (junctions(): corners too); none within BANK_END_M of an open end (a deck's or tunnel's
/// abutment, a dead end); each side of each line in BANK_STEP_M sections of a rounded ridge (BANK_PROFILE across
/// a base of 2.4 h, its crest varying +-15 % along), tapered over BANK_TAPER_M at every cut and open end, only inside
/// the box (x0, y0, x1, y1).
pub fn banks(lines: &Lines, height: &[f64; CLASSES], cuts: &[[f64; 3]], over: &Lines, bbox: [f64; 4]) -> BankMesh {
    let mut m = BankMesh { xy: Vec::new(), h: Vec::new(), tris: Vec::new(), uv: Vec::new(), tri_cls: Vec::new() };
    let junc = junctions(lines);
    let grid = SegGrid::new(lines, BANK_CLEAR_M + 4.0);
    let over_grid = SegGrid::new(over, BANK_CLEAR_M + 4.0);
    // each line's continuations: lines with an end meeting one of its ends away from any junction (one road carried
    // on by another feature: their bodies overlap at the joint, no cut there); a junction's partners are not skipped:
    // their bodies cut the bank wherever they reach it (a side street at a slant reaches past the junction's disc)
    let touch: Vec<Vec<usize>> = (0..lines.count()).map(|l| {
        let ln = lines.line(l);
        let mut t = Vec::new();
        if ln.is_empty() {
            return t;
        }
        let close = |a: &[f64; 2], b: &[f64; 2]| (a[0] - b[0]) * (a[0] - b[0]) + (a[1] - b[1]) * (a[1] - b[1]) <= 0.25;
        for e in [ln[0], ln[ln.len() - 1]] {
            if junc.iter().any(|(q, _)| close(q, &e)) {
                continue;
            }
            for o in 0..lines.count() {
                let lo = lines.line(o);
                if o != l && !lo.is_empty() && (close(&lo[0], &e) || close(&lo[lo.len() - 1], &e)) && !t.contains(&o) {
                    t.push(o);
                }
            }
        }
        t
    }).collect();
    // a line mapped twice (every point of it within DUP_M of an earlier line, and the earlier one's of it): the copy
    // draws no bank and cuts none (else each copy's bank stood in the other's carriageway, or both were cut away)
    let near_line = |p: [f64; 2], o: usize| -> bool {
        grid.near(p).iter().any(|&(q, i2)| {
            q as usize == o && seg_dist(p, lines.line(o)[i2 as usize], lines.line(o)[i2 as usize + 1]).0 <= DUP_M
        })
    };
    let dup: Vec<bool> = (0..lines.count()).map(|l| {
        let ln = lines.line(l);
        !ln.is_empty() && (0..l).any(|o| {
            let lo = lines.line(o);
            !lo.is_empty() && ln.iter().all(|&p| near_line(p, o)) && lo.iter().all(|&p| near_line(p, l))
        })
    }).collect();
    for l in 0..lines.count() {
        let c = lines.cls[l] as usize;
        if c >= CLASSES || !(height[c] > 0.0) || dup[l] {
            continue;
        }
        let hgt = height[c];
        let base = (2.4 * hgt).max(0.9);
        let ln = lines.line(l);
        if ln.len() < 2 {
            continue;
        }
        // resample at BANK_STEP_M (the vertices kept)
        let mut s_pts: Vec<([f64; 2], f64)> = vec![(ln[0], 0.0)];
        let mut s_acc = 0.0;
        for i in 0..ln.len() - 1 {
            let (a, b) = (ln[i], ln[i + 1]);
            let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
            let len = (dx * dx + dy * dy).sqrt();
            if !(len > 1e-6) {
                continue;
            }
            let k = (len / BANK_STEP_M).ceil().max(1.0) as usize;
            for j in 1..=k {
                let t = j as f64 / k as f64;
                s_pts.push(([a[0] + dx * t, a[1] + dy * t], s_acc + len * t));
            }
            s_acc += len;
        }
        let ns = s_pts.len();
        if ns < 2 {
            continue;
        }
        let open0 = end_open(lines, l, ln[0], height);
        let open1 = end_open(lines, l, ln[ln.len() - 1], height);
        for side in [1.0f64, -1.0] {
            // per section: its normal (central difference), the crest point, cut or not
            let mut nrm = vec![[0.0f64; 2]; ns];
            for (i, n) in nrm.iter_mut().enumerate() {
                let a = s_pts[i.saturating_sub(1)].0;
                let b = s_pts[(i + 1).min(ns - 1)].0;
                let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
                let len = (dx * dx + dy * dy).sqrt().max(1e-9);
                *n = [-dy / len * side, dx / len * side];
            }
            let off0 = lines.hw[l] - BANK_INSET_M;
            let total = s_pts[ns - 1].1;
            let cut: Vec<bool> = (0..ns).map(|i| {
                let p = s_pts[i].0;
                if (open0 && s_pts[i].1 < BANK_END_M) || (open1 && total - s_pts[i].1 < BANK_END_M) {
                    return true;
                }
                let crest = [p[0] + nrm[i][0] * (off0 + 0.5 * base), p[1] + nrm[i][1] * (off0 + 0.5 * base)];
                if crest[0] < bbox[0] - base || crest[0] > bbox[2] + base || crest[1] < bbox[1] - base
                    || crest[1] > bbox[3] + base {
                    return true;
                }
                // folded into a carriageway: inside a tight bend (of this line or across a joint with its
                // continuation) the offset section reaches back over the road; every profile point must stay outside
                // this line's and its continuations' carriageways
                for &(u, _) in BANK_PROFILE.iter() {
                    let o = off0 + u * base;
                    let q = [p[0] + nrm[i][0] * o, p[1] + nrm[i][1] * o];
                    for &(o2, i2) in grid.near(q) {
                        let o2 = o2 as usize;
                        if o2 != l && !touch[l].contains(&o2) {
                            continue;
                        }
                        let lo = lines.line(o2);
                        let (d, _) = seg_dist(q, lo[i2 as usize], lo[i2 as usize + 1]);
                        if d < lines.hw[o2] - BANK_FOLD_M {
                            return true;
                        }
                    }
                }
                for (q, r) in &junc {
                    let (ex, ey) = (q[0] - p[0], q[1] - p[1]);
                    if ex * ex + ey * ey <= r * r {
                        return true;
                    }
                }
                for d in cuts {
                    let (ex, ey) = (d[0] - crest[0], d[1] - crest[1]);
                    if ex * ex + ey * ey <= d[2] * d[2] {
                        return true;
                    }
                }
                for &(o, i2) in grid.near(crest) {
                    if o as usize == l || dup[o as usize] {
                        continue;
                    }
                    let lo = lines.line(o as usize);
                    let (d, _) = seg_dist(crest, lo[i2 as usize], lo[i2 as usize + 1]);
                    // (a continuation of the same road: only where its carriageway is wider than this one's and
                    // reaches the bank's inner slope (a road widening at a feature joint); a same-width joint keeps
                    // its bank running through)
                    let clear = if touch[l].contains(&(o as usize)) { 0.3 * base } else { BANK_CLEAR_M + 0.5 * base };
                    if d <= lines.hw[o as usize] + clear {
                        return true;
                    }
                }
                for &(o, i2) in over_grid.near(crest) {
                    let lo = over.line(o as usize);
                    let (d, _) = seg_dist(crest, lo[i2 as usize], lo[i2 as usize + 1]);
                    if d <= over.hw[o as usize] + BANK_CLEAR_M + 0.5 * base {
                        return true;
                    }
                }
                false
            }).collect();
            // distance along to the nearest cut or open end: the taper
            let mut fac = vec![0.0f64; ns];
            let mut last = if open0 { Some(0.0) } else { None };
            let mut dist_prev = vec![f64::INFINITY; ns];
            for i in 0..ns {
                if cut[i] {
                    last = Some(s_pts[i].1);
                }
                if let Some(s) = last {
                    dist_prev[i] = s_pts[i].1 - s;
                }
            }
            let mut next = if open1 { Some(s_pts[ns - 1].1) } else { None };
            for i in (0..ns).rev() {
                if cut[i] {
                    next = Some(s_pts[i].1);
                }
                let dn = next.map(|s| s - s_pts[i].1).unwrap_or(f64::INFINITY);
                let d = dist_prev[i].min(dn);
                fac[i] = if cut[i] { 0.0 } else { (d / BANK_TAPER_M).min(1.0) };
            }
            // sections: 5 profile points each
            let mut first: Vec<Option<u32>> = vec![None; ns];
            for i in 0..ns {
                let keep = fac[i] > 0.0 || (i > 0 && fac[i - 1] > 0.0) || (i + 1 < ns && fac[i + 1] > 0.0);
                if !keep {
                    continue;
                }
                let p = s_pts[i].0;
                let var = 0.85 + 0.3 * hash01((p[0] / 3.0).floor() as i64 * 2 + (side > 0.0) as i64,
                                               (p[1] / 3.0).floor() as i64);
                let h = hgt * fac[i] * var;
                first[i] = Some(m.xy.len() as u32);
                for (k, &(u, f)) in BANK_PROFILE.iter().enumerate() {
                    let o = off0 + u * base * (0.4 + 0.6 * fac[i]);
                    m.xy.push([p[0] + nrm[i][0] * o, p[1] + nrm[i][1] * o]);
                    m.h.push(h * f);
                    m.uv.push([k as f32 / 4.0, (s_pts[i].1 / 4.0) as f32]);
                }
            }
            for i in 0..ns - 1 {
                let (Some(a), Some(b)) = (first[i], first[i + 1]) else { continue };
                if fac[i] <= 0.0 && fac[i + 1] <= 0.0 {
                    continue;
                }
                for k in 0..4u32 {
                    // (wound so the ridge faces up on both sides)
                    let (a0, a1, b0, b1) = (a + k, a + k + 1, b + k, b + k + 1);
                    if side > 0.0 {
                        m.tris.push([a0, b0, a1]);
                        m.tris.push([a1, b0, b1]);
                    } else {
                        m.tris.push([a0, a1, b0]);
                        m.tris.push([a1, b1, b0]);
                    }
                    m.tri_cls.push(c as u8);
                    m.tri_cls.push(c as u8);
                }
            }
        }
    }
    m
}

pub mod py {
    use super::*;
    use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
    use pyo3::exceptions::PyValueError;
    use pyo3::prelude::*;

    fn pts_of(a: &PyReadonlyArray2<'_, f64>) -> PyResult<Vec<[f64; 2]>> {
        let v = a.as_array();
        if v.ncols() != 2 {
            return Err(PyValueError::new_err("snowroad: points (n, 2)"));
        }
        Ok(v.rows().into_iter().map(|r| [r[0], r[1]]).collect())
    }

    fn off_of(off: &PyReadonlyArray1<'_, i64>, n: usize) -> PyResult<Vec<usize>> {
        let o: Vec<usize> = off.as_array().iter().map(|&v| v.max(0) as usize).collect();
        if o.is_empty() || o.windows(2).any(|w| w[1] < w[0]) || *o.last().unwrap() > n {
            return Err(PyValueError::new_err("snowroad: line offsets rising, within the points"));
        }
        Ok(o)
    }

    /// Plow classes of road class names.
    #[pyfunction]
    pub fn snowroad_classes(names: Vec<String>) -> Vec<u8> {
        names.iter().map(|s| plow_class(s)).collect()
    }

    /// The ground photo's road mask (px, px) uint8 (zone_raster).
    #[pyfunction]
    #[allow(clippy::too_many_arguments)]
    pub fn snowroad_zones<'py>(py: Python<'py>, segs: PyReadonlyArray2<'py, f64>, hw: PyReadonlyArray1<'py, f64>,
                               road: PyReadonlyArray1<'py, i64>, cls: PyReadonlyArray1<'py, u8>,
                               lanes: PyReadonlyArray1<'py, u8>, oneway: PyReadonlyArray1<'py, bool>, px: usize,
                               texel_m: f64) -> PyResult<Bound<'py, PyArray2<u8>>> {
        let s = segs.as_array();
        if s.ncols() != 4 || !(1..=16384).contains(&px) || !(texel_m > 0.0) {
            return Err(PyValueError::new_err("snowroad_zones: segments (m, 4), 1 <= px <= 16384, texel > 0"));
        }
        let sv: Vec<[f64; 4]> = s.rows().into_iter().map(|r| [r[0], r[1], r[2], r[3]]).collect();
        let (hw, road, cls, lanes, ow) = (hw.as_array().to_vec(), road.as_array().to_vec(), cls.as_array().to_vec(),
                                          lanes.as_array().to_vec(), oneway.as_array().to_vec());
        let out = py.detach(|| zone_raster(&sv, &hw, &road, &cls, &lanes, &ow, px, texel_m));
        Ok(numpy::ndarray::Array2::from_shape_vec((px, px), out).expect("px x px").into_pyarray(py))
    }

    /// (line, class, side) of each point's nearest line within its half width + extra (class_at).
    #[pyfunction]
    pub fn snowroad_class_at<'py>(py: Python<'py>, points: PyReadonlyArray2<'py, f64>,
                                  pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                                  hw: PyReadonlyArray1<'py, f64>, cls: PyReadonlyArray1<'py, u8>, extra: f64)
                                  -> PyResult<(Bound<'py, PyArray1<i32>>, Bound<'py, PyArray1<u8>>,
                                               Bound<'py, PyArray1<i8>>)> {
        let p = pts_of(&points)?;
        let lp = pts_of(&pts)?;
        let o = off_of(&off, lp.len())?;
        let (hw, cls) = (hw.as_array().to_vec(), cls.as_array().to_vec());
        let res = py.detach(|| class_at(&p, &Lines { pts: &lp, off: &o, hw: &hw, cls: &cls }, extra));
        let (mut a, mut b, mut c) = (Vec::with_capacity(res.len()), Vec::with_capacity(res.len()),
                                     Vec::with_capacity(res.len()));
        for (l, k, s) in res {
            a.push(l);
            b.push(k);
            c.push(s);
        }
        Ok((a.into_pyarray(py), b.into_pyarray(py), c.into_pyarray(py)))
    }

    /// The bank mesh (banks): (xy (n, 2), height (n), tris (m, 3) uint32, uv (n, 2) float32, class per tri) or None.
    #[pyfunction]
    #[pyo3(signature = (pts, off, hw, cls, height, cuts, bbox, over=None))]
    #[allow(clippy::too_many_arguments, clippy::type_complexity)]
    pub fn snowroad_banks<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                               hw: PyReadonlyArray1<'py, f64>, cls: PyReadonlyArray1<'py, u8>, height: Vec<f64>,
                               cuts: PyReadonlyArray2<'py, f64>, bbox: (f64, f64, f64, f64),
                               over: Option<(PyReadonlyArray2<'py, f64>, PyReadonlyArray1<'py, i64>,
                                             PyReadonlyArray1<'py, f64>)>)
                               -> PyResult<Option<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>,
                                                   Bound<'py, PyArray2<u32>>, Bound<'py, PyArray2<f32>>,
                                                   Bound<'py, PyArray1<u8>>)>> {
        let lp = pts_of(&pts)?;
        let o = off_of(&off, lp.len())?;
        let (hw, cls) = (hw.as_array().to_vec(), cls.as_array().to_vec());
        if height.len() != CLASSES {
            return Err(PyValueError::new_err("snowroad_banks: a height per class (5)"));
        }
        let mut h = [0.0f64; CLASSES];
        h.copy_from_slice(&height);
        let cv = cuts.as_array();
        if cv.ncols() != 3 && cv.nrows() > 0 {
            return Err(PyValueError::new_err("snowroad_banks: cuts (k, 3): x, y, r"));
        }
        let cuts: Vec<[f64; 3]> = cv.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect();
        let b = [bbox.0, bbox.1, bbox.2, bbox.3];
        let (op, oo, ohw) = match over {
            Some((p, o2, h2)) => {
                let pv = pts_of(&p)?;
                let ov = off_of(&o2, pv.len())?;
                (pv, ov, h2.as_array().to_vec())
            }
            None => (Vec::new(), vec![0usize], Vec::new()),
        };
        let ocls = vec![0u8; ohw.len()];
        let mesh = py.detach(|| banks(&Lines { pts: &lp, off: &o, hw: &hw, cls: &cls }, &h, &cuts,
                                      &Lines { pts: &op, off: &oo, hw: &ohw, cls: &ocls }, b));
        if mesh.tris.is_empty() {
            return Ok(None);
        }
        let n = mesh.xy.len();
        let xy = numpy::ndarray::Array2::from_shape_vec((n, 2), mesh.xy.into_iter().flatten().collect()).unwrap();
        let t = numpy::ndarray::Array2::from_shape_vec((mesh.tris.len(), 3), mesh.tris.into_iter().flatten().collect())
            .unwrap();
        let uv = numpy::ndarray::Array2::from_shape_vec((n, 2), mesh.uv.into_iter().flatten().collect()).unwrap();
        Ok(Some((xy.into_pyarray(py), mesh.h.into_pyarray(py), t.into_pyarray(py), uv.into_pyarray(py),
                 mesh.tri_cls.into_pyarray(py))))
    }

    /// The road look of a state (look): road rows as snow_state gives them (P0..P4 then bridges: cond, depth, tp,
    /// edge cond, edge depth, grit, track permille, banks), the ground's depth (0.1 mm), cover 0..8, age h, crust, air
    /// temperature (0.01 degC), the regime, the previous bank bins. Returns a dict.
    #[pyfunction]
    #[allow(clippy::too_many_arguments)]
    pub fn snowroad_look<'py>(py: Python<'py>, roads: Vec<Vec<i64>>, depth: i64, cover: i64, age_h: i64, crust: bool,
                              t_air: i64, regime: i64, prev_banks: Vec<i64>)
                              -> PyResult<Bound<'py, pyo3::types::PyDict>> {
        let rows: Vec<RoadOut> = roads.iter().map(|r| {
            let g = |i: usize| r.get(i).copied().unwrap_or(0);
            RoadOut { cond: g(0).clamp(0, 12) as u8, depth: g(1), tp: g(2), edge_cond: g(3).clamp(0, 12) as u8,
                      edge_depth: g(4), grit: g(5) != 0, track: g(6), banks: g(7) }
        }).collect();
        let l = look(&rows, depth, cover, age_h, crust, t_air, regime.clamp(0, 4), &prev_banks);
        let d = pyo3::types::PyDict::new(py);
        d.set_item("shares", l.shares.iter().map(|r| r.to_vec()).collect::<Vec<_>>())?;
        d.set_item("deck_shares", l.deck_shares.iter().map(|r| r.to_vec()).collect::<Vec<_>>())?;
        d.set_item("marking", l.marking.to_vec())?;
        d.set_item("deck_cover", l.deck_cover.to_vec())?;
        d.set_item("walks", l.walks.iter().map(|r| r.to_vec()).collect::<Vec<_>>())?;
        d.set_item("bank_bin", l.bank_bin.to_vec())?;
        d.set_item("dirt", l.dirt.to_vec())?;
        Ok(d)
    }

    /// Sidewalk slots (walk_slot) of slabs: their road's class, feature id and side.
    #[pyfunction]
    pub fn snowroad_walk_slots(cls: Vec<u8>, fids: Vec<String>, sides: Vec<i8>) -> PyResult<Vec<u8>> {
        if cls.len() != fids.len() || cls.len() != sides.len() {
            return Err(PyValueError::new_err("snowroad_walk_slots: one class, feature and side a slab"));
        }
        Ok(cls.iter().zip(&fids).zip(&sides).map(|((&c, f), &s)| walk_slot(c, f, s)).collect())
    }

    /// Crossing cut discs (crossing_discs): polylines (each (k, 2+)), widths.
    #[pyfunction]
    pub fn snowroad_crossing_discs<'py>(py: Python<'py>, lines: Vec<PyReadonlyArray2<'py, f64>>, widths: Vec<f64>)
                                        -> PyResult<Bound<'py, PyArray2<f64>>> {
        if lines.len() != widths.len() {
            return Err(PyValueError::new_err("snowroad_crossing_discs: a width a line"));
        }
        let ls: Vec<Vec<[f64; 2]>> = lines.iter().map(|a| {
            let v = a.as_array();
            if v.ncols() < 2 { Vec::new() } else { v.rows().into_iter().map(|r| [r[0], r[1]]).collect() }
        }).collect();
        let d = crossing_discs(&ls, &widths);
        Ok(numpy::ndarray::Array2::from_shape_vec((d.len(), 3), d.into_iter().flatten().collect()).unwrap()
            .into_pyarray(py))
    }

    pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(snowroad_walk_slots, m)?)?;
        m.add_function(wrap_pyfunction!(snowroad_crossing_discs, m)?)?;
        m.add_function(wrap_pyfunction!(snowroad_classes, m)?)?;
        m.add_function(wrap_pyfunction!(snowroad_zones, m)?)?;
        m.add_function(wrap_pyfunction!(snowroad_class_at, m)?)?;
        m.add_function(wrap_pyfunction!(snowroad_banks, m)?)?;
        m.add_function(wrap_pyfunction!(snowroad_look, m)?)?;
        m.add("SNOWROAD_SLOTS", SLOTS)?;
        m.add("SNOWROAD_BANK_DRAW_MM", BANK_DRAW_MM.to_vec())?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dm::Dm;

    #[test]
    fn walk_slots() {
        assert_eq!(walk_slot(1, "w/1", 1), 0);
        assert_eq!(walk_slot(4, "w/1", 1), 3);
        assert_eq!(walk_slot(NO_CLASS, "", 0), 3);
        // residential faces: cleared on about 60 %, fixed per face
        let n = (0..2000).filter(|i| walk_slot(3, &format!("w/{i}"), 1) == 1).count();
        assert!((1100..1300).contains(&n), "{n}");
        assert_eq!(walk_slot(3, "w/7", -1), walk_slot(3, "w/7", -1));
        let d = crossing_discs(&[vec![[-60.0, -6.0], [-60.0, 6.0]]], &[3.0]);
        assert_eq!(d, vec![[-60.0, 0.0, 6.0 + 1.5 + BANK_CLEAR_M]]);
    }

    #[test]
    fn classes() {
        assert_eq!(plow_class("motorway_link"), 0);
        assert_eq!(plow_class("secondary"), 1);
        assert_eq!(plow_class("tertiary"), 2);
        assert_eq!(plow_class("residential"), 3);
        assert_eq!(plow_class("service"), 4);
        assert_eq!(plow_class("track"), 4);
    }

    #[test]
    fn shares_by_condition() {
        // a salted main road after the storm: bare lanes, slush thrown to the edge
        assert_eq!(zone_shares(snow::SALT_WET, 900, snow::SLUSH), [0, 0, 5]);
        // a packed residential street: grey lanes, darker tracks, white edges
        let s = zone_shares(snow::PACKED, 1000, snow::LOOSE);
        assert_eq!(s, [6, 2, 8]);
        // fresh loose snow, no traffic yet: white everywhere
        assert_eq!(zone_shares(snow::LOOSE, 0, snow::LOOSE), [8, 8, 8]);
        // a dusting churned by traffic: wet black tracks
        assert_eq!(zone_shares(snow::DUSTING, 1000, snow::DUSTING), [5, 0, 6]);
        // the edge is never cleaner than the lanes
        assert_eq!(zone_shares(snow::PACKED, 0, snow::DRY)[2], 6);
    }

    #[test]
    fn markings() {
        assert_eq!(marking_vis(&[0, 0, 5], 8), 1000);
        assert_eq!(marking_vis(&[8, 8, 8], 8), 0); // hidden under a full cover
        let tracked = marking_vis(&[6, 3, 8], 8);
        assert!(tracked > 200 && tracked < 500, "{tracked}"); // readable under tracks
        assert_eq!(marking_vis(&[8, 8, 8], 0), 1000); // no snow on the ground at all
    }

    #[test]
    fn bank_bins() {
        // 30 mm of water plowed off a 7 m street: about 0.4 m
        let h = bank_height_mm(3000, 3);
        assert!((350..=500).contains(&h), "{h}");
        assert_eq!(bank_bin(h, -1), 1);
        assert_eq!(bank_bin(0, -1), 0);
        assert_eq!(bank_bin(2000, -1), 4);
        // hysteresis: 460 mm stays 1 from 1, 520 moves on
        assert_eq!(bank_bin(460, 1), 1);
        assert_eq!(bank_bin(520, 1), 2);
        assert_eq!(bank_bin(440, 2), 2);
        assert_eq!(bank_bin(380, 2), 1);
    }

    #[test]
    fn walks() {
        let w = walk_states(800, 8, 2, false, -500);
        assert_eq!(w[0], [W_TRAMPLED, 5]);
        assert_eq!(w[2], [W_COVERED, 8]);
        let w = walk_states(800, 8, 30, true, -500);
        assert_eq!(w[0], [W_ICY, 2]);
        assert_eq!(w[1], [W_ICY, 2]);
        assert_eq!(w[3], [W_TRAMPLED, 7]);
        assert_eq!(walk_states(0, 0, 30, false, 0), [[W_CLEAR, 0]; WALKS]);
        // never whiter than the ground
        assert!(walk_states(30, 4, 0, false, -100).iter().all(|s| s[1] <= 4));
    }

    #[test]
    fn zones_across_a_street() {
        // a 10 m two-way street, two lanes, 0.5 m texels
        let (hw, t) = (5.0, 0.5);
        assert_eq!(zone_of(4.6, hw, 2, false, t), Z_EDGE);
        assert_eq!(zone_of(-4.6, hw, 2, false, t), Z_EDGE);
        assert_eq!(zone_of(0.1, hw, 2, false, t), Z_EDGE); // between the directions
        assert_eq!(zone_of(0.1, hw, 2, true, t), Z_LANE); // one way: no centre ridge
        // lane centre at 2.5 m left; its tracks at 2.5 -+ 0.78
        assert_eq!(zone_of(2.5 - 0.78, hw, 2, false, t), Z_TRACK);
        assert_eq!(zone_of(2.5 + 0.78, hw, 2, false, t), Z_TRACK);
        assert_eq!(zone_of(2.5, hw, 2, false, t), Z_LANE);
    }

    #[test]
    fn raster_and_junction() {
        // two crossing roads in a 64 px picture at 0.5 m: a P1 east-west, a P3 north-south
        let segs = [[0.0, 32.0, 64.0, 32.0], [32.0, 0.0, 32.0, 64.0]];
        let code = zone_raster(&segs, &[10.0, 8.0], &[0, 1], &[1, 3], &[2, 2], &[false, false], 64, 0.5);
        let at = |c: usize, r: usize| code[r * 64 + c];
        assert_eq!(at(32, 32), 1 + 1); // the junction: lane zone of the lower class number
        assert_eq!(at(5, 32), 1 + (Z_EDGE * CLASSES) as u8 + 1); // the P1's centre ridge, away from the junction
        assert_eq!(at(5, 23), 1 + (Z_EDGE * CLASSES) as u8 + 1); // its edge
        assert_eq!(at(5, 5), 0); // no road
        // a track texel exists on the P1 away from the junction
        assert!((0..64).any(|r| at(5, r) == 1 + (Z_TRACK * CLASSES) as u8 + 1));
        // the same inputs: the same bytes
        assert_eq!(code, zone_raster(&segs, &[10.0, 8.0], &[0, 1], &[1, 3], &[2, 2], &[false, false], 64, 0.5));
    }

    fn cross_lines() -> (Vec<[f64; 2]>, Vec<usize>, Vec<f64>, Vec<u8>) {
        // an east-west P1 through (0, 0) as two features meeting there, a P3 from the south ending at (0, 0), a
        // north-south P3 continuing at (100, 0) in two features (no junction)
        let pts = vec![[-200.0, 0.0], [0.0, 0.0], [0.0, 0.0], [200.0, 0.0], [0.0, -200.0], [0.0, 0.0],
                       [100.0, 10.0], [100.0, 100.0], [100.0, 100.0], [100.0, 200.0]];
        (pts, vec![0, 2, 4, 6, 8, 10], vec![5.0, 5.0, 4.0, 4.0, 4.0], vec![1, 1, 3, 3, 3])
    }

    #[test]
    fn junction_points() {
        let (pts, off, hw, cls) = cross_lines();
        let l = Lines { pts: &pts, off: &off, hw: &hw, cls: &cls };
        let j = junctions(&l);
        assert_eq!(j.len(), 1, "{j:?}");
        assert_eq!(j[0].0, [0.0, 0.0]);
        assert_eq!(j[0].1, 5.0 + JUNCTION_CLEAR_M);
    }

    #[test]
    fn banks_break_at_junctions_and_cuts() {
        let (pts, off, hw, cls) = cross_lines();
        let l = Lines { pts: &pts, off: &off, hw: &hw, cls: &cls };
        let h = [0.0, 0.6, 0.0, 0.3, 0.0];
        let none = Lines { pts: &[], off: &[0], hw: &[], cls: &[] };
        let m = banks(&l, &h, &[[-100.0, 6.0, 4.0]], &none, [-300.0, -300.0, 300.0, 300.0]);
        assert!(!m.tris.is_empty());
        assert_eq!(m.xy.len(), m.h.len());
        let crest = |x: f64, y: f64| {
            m.xy.iter().zip(&m.h).filter(|(p, _)| (p[0] - x).abs() < 1.5 && (p[1] - y).abs() < 1.5)
                .map(|(_, h)| *h).fold(0.0, f64::max)
        };
        // full height away from everything, on the P1's north side (5 - 0.15 + 0.72 m out)
        assert!(crest(-50.0, 5.57) > 0.45, "{}", crest(-50.0, 5.57));
        // none at the junction, none at the crossing cut
        assert!(crest(-3.0, 5.57) < 1e-9);
        assert!(crest(-100.0, 5.57) < 1e-9);
        // the continuing P3 at (100, 100): banks run through the joint
        assert!(crest(104.4, 100.0) > 0.2, "{}", crest(104.4, 100.0));
        // every triangle's class is a plowed class with a height
        assert!(m.tri_cls.iter().all(|&c| c == 1 || c == 3));
        // deterministic
        let m2 = banks(&l, &h, &[[-100.0, 6.0, 4.0]], &none, [-300.0, -300.0, 300.0, 300.0]);
        // a deck overhead across the P1 at x = -150: no bank under it
        let dpts = [[-150.0, -50.0], [-150.0, 50.0]];
        let deck = Lines { pts: &dpts, off: &[0, 2], hw: &[6.0], cls: &[0] };
        let m3 = banks(&l, &h, &[], &deck, [-300.0, -300.0, 300.0, 300.0]);
        let under = m3.xy.iter().zip(&m3.h).any(|(p, h)| (p[0] + 150.0).abs() < 6.5 && *h > 1e-9 && p[1] > 0.0);
        assert!(!under);
        assert_eq!(m.xy, m2.xy);
        assert_eq!(m.tris, m2.tris);
    }

    #[test]
    fn banks_at_a_widening_joint_and_a_copy() {
        // a 5 m half width road continuing as a 9 m one at (0, 0); a copy of the narrow one
        let pts = [[-100.0, 0.0], [0.0, 0.0], [0.0, 0.0], [100.0, 0.0], [-100.0, 0.3], [0.0, 0.3]];
        let l = Lines { pts: &pts, off: &[0, 2, 4, 6], hw: &[5.0, 9.0, 5.0], cls: &[3, 3, 3] };
        let none = Lines { pts: &[], off: &[0], hw: &[], cls: &[] };
        let m = banks(&l, &[0.0, 0.0, 0.0, 0.6, 0.0], &[], &none, [-300.0, -300.0, 300.0, 300.0]);
        // no bank vertex of the narrow road inside the wide road's carriageway (x > 0, |y| < 9)
        assert!(!m.xy.iter().zip(&m.h).any(|(p, h)| *h > 0.03 && p[0] > 0.5 && p[1].abs() < 8.75));
        // the narrow road keeps its bank away from the joint, drawn once (not twice by its copy)
        let near = m.xy.iter().zip(&m.h).filter(|(p, h)| **h > 0.3 && (p[0] + 50.0).abs() < 1.3 && p[1] > 0.0).count();
        assert!(near > 0 && near <= 3, "{near}"); // (one section of three crest-ish points; its copy would double it)
    }

    #[test]
    fn banks_never_fold_into_a_bend() {
        // a hairpin: east 60 m, then back west 12 m north (a U of radius 6 m about (60, 6))
        let mut pts = vec![[0.0, 0.0]];
        for k in 0..=12 {
            let a = -std::f64::consts::FRAC_PI_2 + std::f64::consts::PI * k as f64 / 12.0;
            pts.push([60.0 + 6.0 * a.dcos(), 6.0 + 6.0 * a.dsin()]);   // (dm: test_determinism's platform-maths scan)
        }
        pts.push([0.0, 12.0]);
        let n = pts.len();
        let l = Lines { pts: &pts, off: &[0, n], hw: &[4.0], cls: &[3] };
        let none = Lines { pts: &[], off: &[0], hw: &[], cls: &[] };
        let m = banks(&l, &[0.0, 0.0, 0.0, 0.6, 0.0], &[], &none, [-100.0, -100.0, 200.0, 200.0]);
        assert!(!m.tris.is_empty());
        for (p, h) in m.xy.iter().zip(&m.h) {
            if *h <= 0.03 {
                continue;
            }
            let d = (0..n - 1).map(|i| seg_dist(*p, pts[i], pts[i + 1]).0).fold(f64::INFINITY, f64::min);
            assert!(d >= 4.0 - BANK_FOLD_M - 1e-9, "a bank point {p:?} {d} m from the road");
        }
    }

    #[test]
    fn banks_end_where_a_road_goes_on_as_a_service_road() {
        let pts = [[-100.0, 0.0], [0.0, 0.0], [0.0, 0.0], [30.0, 0.0]];
        let l = Lines { pts: &pts, off: &[0, 2, 4], hw: &[4.0, 3.0], cls: &[3, 4] };
        let none = Lines { pts: &[], off: &[0], hw: &[], cls: &[] };
        let m = banks(&l, &[0.0, 0.0, 0.0, 0.6, 0.0], &[], &none, [-200.0, -200.0, 200.0, 200.0]);
        assert!(!m.xy.iter().zip(&m.h).any(|(p, h)| *h > 0.03 && p[0] > -BANK_END_M));
        assert!(m.xy.iter().zip(&m.h).any(|(p, h)| *h > 0.3 && p[0] < -20.0));
    }

    #[test]
    fn grid_reaches_a_wide_deck_end() {
        // a 2-point deck of half width 18 m ending at (0, 0); a point 15 m past its end is inside it
        let pts = [[0.0, 100.0], [0.0, 0.0]];
        let l = Lines { pts: &pts, off: &[0, 2], hw: &[18.0], cls: &[0] };
        let r = class_at(&[[3.0, -15.0]], &l, 0.0);
        assert_eq!(r[0].0, 0);
    }

    #[test]
    fn nearest_class() {
        let (pts, off, hw, cls) = cross_lines();
        let l = Lines { pts: &pts, off: &off, hw: &hw, cls: &cls };
        let r = class_at(&[[-50.0, 3.0], [-50.0, -3.0], [2.0, -100.0], [500.0, 500.0]], &l, 2.0);
        assert_eq!((r[0].0, r[0].1, r[0].2), (0, 1, 1));
        assert_eq!(r[1].2, -1);
        assert_eq!(r[2].1, 3);
        assert_eq!(r[3].1, NO_CLASS);
    }

    #[test]
    fn look_of_a_storm() {
        let mut roads = vec![RoadOut::default(); 2 * CLASSES];
        roads[0] = RoadOut { cond: snow::SALT_WET, edge_cond: snow::SLUSH, track: 900, banks: 2000, ..Default::default() };
        roads[3] = RoadOut { cond: snow::PACKED, edge_cond: snow::LOOSE, track: 800, banks: 3000, ..Default::default() };
        let l = look(&roads, 1200, 8, 30, false, -400, 2, &[-1; 5]);
        assert_eq!(l.marking[0], 1000);
        assert!(l.marking[3] < 500);
        assert!(l.bank_bin[0] >= 1 && l.bank_bin[3] >= 1);
        assert_eq!(l.bank_bin[4], 0); // P4 never plowed
        assert_eq!(l.dirt[0], 1); // salted, a day old: grey
        assert_eq!(l.walks[0][0], W_SHOVELLED);
    }
}
