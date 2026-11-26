//! The scanner's own checks on a tile's scene, beside the M0 audit's located cells (roadsys audit::audit_findings):
//!
//! - the virtual drive: each lane's centre row of the audit's lane cells (its drivable height every 0.5 m, as the
//!   audit traced it: terrain, deck top, tunnel floor) read as a car drives it:
//!   - `BSTEP`: a step in the surface: one 0.5 m difference off the median of the 8 around it by over STEP_MIN
//!     (mask 1: between the ground and a deck, 2: a tunnel floor; a steady grade or a smooth curve has none);
//!   - `RIDE`: the vertical acceleration of a car at the road class's speed over a 5 m chord (v^2 times the second
//!     difference over 5 m), over RIDE_G (v: in g, + a sag, - a crest; a car leaves the road over a crest at -1);
//!   - `GRADE`: the 10 m grade over the class's limit;
//! - `ENDSTEP`: at each end of a mapped part (a deck's end, a tunnel's mouth, a way's end where another goes on) the
//!   last cell of each lane against the cell of the road going on 1 m further (mask as BSTEP); the hand-over lips only
//!   see steps under 15 cm and the rows stop at a part's end;
//! - the drawn road against the physics: `BURIED` (the terrain over the drawn road surface by over 4 cm: ground
//!   showing through), `FLOATSKIN` (the drawn surface over 12 cm above what the car drives on), `MISSING` (a paved
//!   lane with no drawn road surface and no paved terrain paint);
//! - `MARKOFF`: painted markings 1.5 to 3 m outside every mapped road's half width (a service way's parking bays
//!   allowed 6 m more);
//! - `WIDTH`: the drawn road's width across a lane (at 10 m stations, away from junctions, parallel roads and part
//!   ends) against its resolved width (OSM width / lanes tags or the class's default): v = built - planned.
use crate::pack::LineMeta;
use crate::scene::Built;
use roadsys::audit::{ray_z, Finding, Tri, FLAG_MARK, KIND_DECK, KIND_ROAD, KIND_TUNNEL, KIND_WALK, KIND_WALKCOL, UP};
use std::collections::BTreeMap;

pub const STEP_MIN: f64 = 0.03;
pub const RIDE_G: f64 = 0.5;
const RIDE_CHORD: i64 = 10;         // cells (5 m) either side
const END_OUT: f64 = 1.0;
const END_STEP: f64 = 0.05;
const BURIED: f64 = 0.04;
const FLOAT: f64 = 0.12;
const MARK_OFF: f64 = 1.5;
const MARK_SERVICE: f64 = 6.0;
const MARK_FAR: f64 = 3.0;

/// m/s a car drives a road class at (the ride check), and the 10 m grade over which the class is flagged.
pub fn class_speed(cls: &str) -> (f64, f64) {
    let mph = |m: f64| m * 0.44704;
    match cls {
        "motorway" => (mph(70.0), 0.08),
        "trunk" => (mph(60.0), 0.10),
        "motorway_link" | "trunk_link" => (mph(45.0), 0.10),
        "primary" => (mph(50.0), 0.14),
        "secondary" => (mph(45.0), 0.16),
        "primary_link" | "secondary_link" | "tertiary" | "tertiary_link" => (mph(40.0), 0.18),
        "service" | "track" | "living_street" => (mph(20.0), 0.30),
        _ => (mph(30.0), 0.22),
    }
}

/// 2 m buckets of triangle ids.
pub struct TriGrid { cells: BTreeMap<(i64, i64), Vec<u32>> }

impl TriGrid {
    pub fn new(tris: &[Tri], keep: impl Fn(&Tri) -> bool) -> TriGrid {
        let mut cells: BTreeMap<(i64, i64), Vec<u32>> = BTreeMap::new();
        for (k, t) in tris.iter().enumerate() {
            if !keep(t) { continue; }
            let (mut a, mut b) = ([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]);
            for v in &t.p { for c in 0..2 { a[c] = a[c].min(v[c]); b[c] = b[c].max(v[c]); } }
            let (i0, i1, j0, j1) = ((a[0] / 2.0).floor() as i64, (b[0] / 2.0).floor() as i64, (a[1] / 2.0).floor() as i64, (b[1] / 2.0).floor() as i64);
            if (i1 - i0 + 1) * (j1 - j0 + 1) > 40000 { continue; }
            for i in i0..=i1 { for j in j0..=j1 { cells.entry((i, j)).or_default().push(k as u32); } }
        }
        TriGrid { cells }
    }
    pub fn at(&self, x: f64, y: f64) -> &[u32] {
        self.cells.get(&((x / 2.0).floor() as i64, (y / 2.0).floor() as i64)).map(|v| v.as_slice()).unwrap_or(&[])
    }
    /// Heights of the faces over (x, y).
    pub fn zs(&self, tris: &[Tri], x: f64, y: f64) -> Vec<f64> {
        self.at(x, y).iter().filter_map(|&k| ray_z(&tris[k as usize], x, y)).collect()
    }
}

/// 8 m buckets of line segments (distance queries).
pub struct SegGrid { cells: BTreeMap<(i64, i64), Vec<(u32, u32)>> }

impl SegGrid {
    pub fn new(lines: &[LineMeta]) -> SegGrid {
        let mut cells: BTreeMap<(i64, i64), Vec<(u32, u32)>> = BTreeMap::new();
        for (li, l) in lines.iter().enumerate() {
            for j in 0..l.xy.len().saturating_sub(1) {
                let (a, b) = (l.xy[j], l.xy[j + 1]);
                let (i0, i1) = ((a[0].min(b[0]) / 8.0).floor() as i64, (a[0].max(b[0]) / 8.0).floor() as i64);
                let (j0, j1) = ((a[1].min(b[1]) / 8.0).floor() as i64, (a[1].max(b[1]) / 8.0).floor() as i64);
                if (i1 - i0 + 1) * (j1 - j0 + 1) > 20000 { continue; }
                for i in i0..=i1 { for jj in j0..=j1 { cells.entry((i, jj)).or_default().push((li as u32, j as u32)); } }
            }
        }
        SegGrid { cells }
    }
    /// (line, distance) of every line within r of (x, y) (its nearest segment).
    pub fn near(&self, lines: &[LineMeta], x: f64, y: f64, r: f64) -> Vec<(u32, f64)> {
        let mut best: BTreeMap<u32, f64> = BTreeMap::new();
        let (i0, i1) = (((x - r) / 8.0).floor() as i64, ((x + r) / 8.0).floor() as i64);
        let (j0, j1) = (((y - r) / 8.0).floor() as i64, ((y + r) / 8.0).floor() as i64);
        for i in i0..=i1 {
            for j in j0..=j1 {
                let Some(v) = self.cells.get(&(i, j)) else { continue };
                for &(li, s) in v {
                    let l = &lines[li as usize];
                    let d = seg_dist(l.xy[s as usize], l.xy[s as usize + 1], [x, y]);
                    if d <= r { let e = best.entry(li).or_insert(f64::INFINITY); if d < *e { *e = d; } }
                }
            }
        }
        best.into_iter().collect()
    }
}

pub fn seg_dist(a: [f64; 2], b: [f64; 2], p: [f64; 2]) -> f64 {
    let ab = [b[0] - a[0], b[1] - a[1]];
    let l2 = ab[0] * ab[0] + ab[1] * ab[1];
    let u = if l2 < 1e-12 { 0.0 } else { (((p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1]) / l2).clamp(0.0, 1.0) };
    let q = [a[0] + ab[0] * u - p[0], a[1] + ab[1] * u - p[1]];
    (q[0] * q[0] + q[1] * q[1]).sqrt()
}

/// The across offsets (0.5 m cells, the audit's) of each lane's centre on a line.
pub fn lane_rows(l: &LineMeta) -> Vec<i64> {
    let h = l.width / 2.0 - 0.5;
    if h < 0.25 { return Vec::new(); }
    let nlat = ((h - 0.25) / 0.5).floor() as i64;
    let n = l.lanes.max(1) as usize;
    let dw = if l.drive > 0.5 { l.drive.min(l.width) } else { l.width };
    let mut out: Vec<i64> = (0..n).map(|i| {
        let o = -dw / 2.0 + (i as f64 + 0.5) * dw / n as f64;
        ((o / 0.5).round() as i64).clamp(-nlat, nlat)
    }).collect();
    out.sort_unstable();
    out.dedup();
    out
}

struct Cell { st: i64, z: f64, on: u8, x: f64, y: f64 }

/// What a step lies between: 1 the ground and a deck, 2 a tunnel floor, 8 a kerb or sidewalk (the lane's row runs
/// onto one).
fn trans_mask(a: u8, b: u8) -> u32 {
    let deck = 1 + KIND_DECK;
    let tun = 1 + KIND_TUNNEL;
    let walk = |k: u8| k == 1 + KIND_WALK || k == 1 + KIND_WALKCOL;
    let mut m = 0;
    if (a == deck) != (b == deck) { m |= 1; }
    if (a == tun) != (b == tun) { m |= 2; }
    if walk(a) || walk(b) { m |= 8; }
    m
}

fn median(v: &mut [f64]) -> f64 {
    v.sort_by(|a, b| a.total_cmp(b));
    let n = v.len();
    if n % 2 == 1 { v[n / 2] } else { (v[n / 2 - 1] + v[n / 2]) / 2.0 }
}

pub fn run(b: &Built, found: &[Finding], out: &mut Vec<Finding>) {
    let tris = &b.scene.tris;
    let lines = &b.lines;
    // the lane-centre rows of the audit's cells
    let centre: Vec<Vec<i64>> = lines.iter().map(lane_rows).collect();
    let mut rows: BTreeMap<(u32, i64), Vec<Cell>> = BTreeMap::new();
    let mut by_pos: BTreeMap<(i64, i64), Vec<(u32, f64, u8)>> = BTreeMap::new();
    for f in found.iter().filter(|f| f.code == "SA") {
        by_pos.entry(((f.x / 0.5).floor() as i64, (f.y / 0.5).floor() as i64)).or_default().push((f.line, f.z, f.kind));
        let li = f.line as usize;
        if li < centre.len() && centre[li].contains(&f.across) {
            rows.entry((f.line, f.across)).or_default().push(Cell { st: f.station, z: f.z, on: f.kind, x: f.x, y: f.y });
        }
    }
    let skin = TriGrid::new(tris, |t| t.kind == KIND_ROAD && t.flags & FLAG_MARK == 0 && t.cls == UP);
    for ((li, ac), row) in rows.iter_mut() {
        row.sort_by_key(|c| c.st);
        let l = &lines[*li as usize];
        let (v, grade_max) = class_speed(&l.cls);
        let at: BTreeMap<i64, usize> = row.iter().enumerate().map(|(i, c)| (c.st, i)).collect();
        let z = |s: i64| at.get(&s).map(|&i| row[i].z).filter(|z| z.is_finite());
        let base = |f: Finding, c: &Cell| Finding { line: *li, across: *ac, station: c.st, level: l.level, z: c.z, ..Finding { x: c.x, y: c.y, ..f } };
        // steps
        let mut stepped: Vec<i64> = Vec::new();
        for c in row.iter() {
            let s = c.st;
            let (Some(z0), Some(z1)) = (z(s - 1), z(s)) else { continue };
            let d = z1 - z0;
            // (against the trend before it and the trend after it: a kink, where the grade changes, matches one)
            let mut before: Vec<f64> = (s - 4..s).filter_map(|k| Some(z(k)? - z(k - 1)?)).collect();
            let mut after: Vec<f64> = (s + 1..=s + 4).filter_map(|k| Some(z(k)? - z(k - 1)?)).collect();
            let e = match (before.len() >= 2, after.len() >= 2) {
                (true, true) => {
                    let (eb, ea) = (d - median(&mut before), d - median(&mut after));
                    if eb.abs() < ea.abs() { eb } else { ea }
                }
                (true, false) => d - median(&mut before),
                (false, true) => d - median(&mut after),
                _ => continue,
            };
            if e.abs() <= STEP_MIN { continue; }
            let prev = &row[at[&(s - 1)]];
            if !b.owned((prev.x + c.x) / 2.0, (prev.y + c.y) / 2.0) { continue; }
            stepped.push(s);
            out.push(base(Finding { v: e, mask: trans_mask(prev.on, c.on), x: (prev.x + c.x) / 2.0, y: (prev.y + c.y) / 2.0,
                                    ..Finding::at("BSTEP", 0.0, 0.0) }, c));
        }
        // ride and grade: local extremes over the threshold, none within the chord of a step (the step explains it)
        let mut acc: Vec<(i64, f64, f64)> = Vec::new();
        for c in row.iter() {
            let s = c.st;
            let (Some(za), Some(zb), Some(zc)) = (z(s - RIDE_CHORD), z(s), z(s + RIDE_CHORD)) else { continue };
            let chord = RIDE_CHORD as f64 * 0.5;
            let g = v * v * (za - 2.0 * zb + zc) / (chord * chord) / 9.81;
            let grade = (zc - za) / (2.0 * chord);
            acc.push((s, g, grade));
        }
        for i in 0..acc.len() {
            let (s, g, grade) = acc[i];
            let c = &row[at[&s]];
            if !b.owned(c.x, c.y) { continue; }
            if stepped.iter().any(|&t| (t - s).abs() <= RIDE_CHORD) { continue; }
            let nb = |j: usize| acc.get(j).filter(|a| (a.0 - s).abs() == 1);
            let local = |f: &dyn Fn(&(i64, f64, f64)) -> f64| {
                let m = f(&acc[i]);
                [i.wrapping_sub(1), i + 1].iter().all(|&j| nb(j).map_or(true, |a| f(a) <= m))
            };
            if g.abs() > RIDE_G && local(&|a| a.1.abs()) {
                out.push(base(Finding { v: g, ..Finding::at("RIDE", 0.0, 0.0) }, c));
            }
            if grade.abs() > grade_max && local(&|a| a.2.abs()) && l.level != 2 {
                out.push(base(Finding { v: grade, mask: (grade_max * 1000.0) as u32, ..Finding::at("GRADE", 0.0, 0.0) }, c));
            }
        }
        // the drawn road over the driven ground (ground lanes on the terrain)
        if l.level == 0 {
            for c in row.iter() {
                if c.on != 0 || !c.z.is_finite() || !b.owned(c.x, c.y) { continue; }
                let zs = skin.zs(tris, c.x, c.y);
                let near = zs.iter().copied().filter(|s| (s - c.z).abs() <= 1.0)
                    .min_by(|a, b2| (a - c.z).abs().total_cmp(&(b2 - c.z).abs()));
                match near {
                    None => {
                        if l.paved && b.ter.paved_at(c.x, c.y) == Some(false) && zs.is_empty() {
                            out.push(base(Finding::at("MISSING", 0.0, 0.0), c));
                        }
                    }
                    Some(s) => {
                        let dz = s - c.z;
                        if dz < -BURIED { out.push(base(Finding { v: -dz, ..Finding::at("BURIED", 0.0, 0.0) }, c)); }
                        if dz > FLOAT { out.push(base(Finding { v: dz, ..Finding::at("FLOATSKIN", 0.0, 0.0) }, c)); }
                    }
                }
            }
        }
    }
    // part ends: each lane's last cell against the road going on 1 m further
    let mut row_ends: BTreeMap<u32, Vec<i64>> = BTreeMap::new();
    for (li, ac) in rows.keys() { row_ends.entry(*li).or_default().push(*ac); }
    for (li, l) in lines.iter().enumerate() {
        if l.xy.len() < 2 { continue; }
        let li = li as u32;
        let Some(acs) = row_ends.get(&li) else { continue };
        let len: f64 = l.xy.windows(2).map(|w| ((w[1][0] - w[0][0]).powi(2) + (w[1][1] - w[0][1]).powi(2)).sqrt()).sum();
        let last = ((len - 0.25) / 0.5).floor() as i64;
        let n = l.xy.len();
        for (p, q, st, dir) in [(l.xy[0], l.xy[1], 0i64, 1i64), (l.xy[n - 1], l.xy[n - 2], last, -1)] {
            let d = [p[0] - q[0], p[1] - q[1]];
            let dl = (d[0] * d[0] + d[1] * d[1]).sqrt();
            if dl < 1e-6 { continue; }
            let t = [d[0] / dl, d[1] / dl];     // (out of the part's end)
            // (the audit's across axis: left of the line's direction from its start, positive)
            let left = if dir == 1 { [t[1], -t[0]] } else { [-t[1], t[0]] };
            for &ac in acs {
                let row = &rows[&(li, ac)];
                let inner: Vec<&Cell> = row.iter().filter(|c| (c.st - st).abs() <= 2 && c.z.is_finite()).collect();
                let Some(cin) = inner.iter().min_by_key(|c| (c.st - st).abs()) else { continue };
                // the row's own grade over its last 4 m, carried 1 m on
                let g = {
                    let far = row.iter().filter(|c| (c.st - (st + dir * 8)).abs() <= 1 && c.z.is_finite()).min_by_key(|c| (c.st - (st + dir * 8)).abs());
                    far.map(|f| (cin.z - f.z) / (((cin.x - f.x).powi(2) + (cin.y - f.y).powi(2)).sqrt().max(0.5))).unwrap_or(0.0)
                };
                let o = [p[0] + t[0] * END_OUT + left[0] * ac as f64 * 0.5, p[1] + t[1] * END_OUT + left[1] * ac as f64 * 0.5];
                let key = ((o[0] / 0.5).floor() as i64, (o[1] / 0.5).floor() as i64);
                let mut zout: Option<(f64, u8)> = None;
                for dx in -1..=1 { for dy in -1..=1 {
                    let Some(v) = by_pos.get(&(key.0 + dx, key.1 + dy)) else { continue };
                    for &(ol, z, on) in v {
                        if ol == li || !z.is_finite() { continue; }
                        if zout.map_or(true, |(w, _)| (z - cin.z).abs() < (w - cin.z).abs()) { zout = Some((z, on)); }
                    }
                } }
                let Some((zo, on)) = zout else { continue };
                let step = zo - (cin.z + g * END_OUT);
                if step.abs() <= END_STEP || step.abs() > 3.0 { continue; }     // (over 3 m: another road crossing)
                if !b.owned(p[0], p[1]) { continue; }
                out.push(Finding { code: "ENDSTEP", x: p[0], y: p[1], z: cin.z, v: step, line: li, station: st, across: ac,
                                   level: l.level, kind: 0, mask: trans_mask(cin.on, on) | if l.level != 0 { 4 } else { 0 } });
            }
        }
    }
    // markings away from every mapped road
    let segs = SegGrid::new(lines);
    for t in tris.iter().take_while(|t| (t.entry as usize) < b.own_entries) {
        if t.kind != KIND_ROAD || t.flags & FLAG_MARK == 0 || t.cls != UP { continue; }
        let c = [(t.p[0][0] + t.p[1][0] + t.p[2][0]) / 3.0, (t.p[0][1] + t.p[1][1] + t.p[2][1]) / 3.0];
        if !b.owned(c[0], c[1]) { continue; }
        // (a service way's parking bays reach MARK_SERVICE past its half width; past MARK_FAR from every road the
        // paint is a lot or a yard the map has no ways in, not a misplaced marking)
        let near = segs.near(lines, c[0], c[1], 25.0);
        let excess = near.iter().map(|&(li, d)| {
            let l = &lines[li as usize];
            d - l.width / 2.0 - if l.cls == "service" { MARK_SERVICE } else { 0.0 }
        }).fold(f64::INFINITY, f64::min);
        if excess > MARK_OFF && excess < MARK_FAR {
            out.push(Finding { v: excess, z: (t.p[0][2] + t.p[1][2] + t.p[2][2]) / 3.0, ..Finding::at("MARKOFF", c[0], c[1]) });
        }
    }
    // the drawn width across ground lanes
    for (li, l) in lines.iter().enumerate() {
        if l.level != 0 || !l.paved || l.xy.len() < 2 || l.width < 2.0 { continue; }
        let mut s_next = 15.0;
        let mut dist = 0.0;
        let total: f64 = l.xy.windows(2).map(|w| ((w[1][0] - w[0][0]).powi(2) + (w[1][1] - w[0][1]).powi(2)).sqrt()).sum();
        for w in l.xy.windows(2) {
            let (a, bb) = (w[0], w[1]);
            let seg = ((bb[0] - a[0]).powi(2) + (bb[1] - a[1]).powi(2)).sqrt();
            if seg < 1e-6 { continue; }
            let t = [(bb[0] - a[0]) / seg, (bb[1] - a[1]) / seg];
            let nrm = [-t[1], t[0]];
            while s_next < dist + seg {
                let u = s_next - dist;
                s_next += 10.0;
                if s_next - 10.0 > total - 15.0 { break; }
                let c = [a[0] + t[0] * u, a[1] + t[1] * u];
                if !b.owned(c[0], c[1]) { continue; }
                // away from junctions and parallel roads: no other mapped road within both half widths + 3 m
                let crowd = segs.near(lines, c[0], c[1], l.width / 2.0 + 20.0).iter()
                    .any(|&(o, d)| o as usize != li && d < l.width / 2.0 + lines[o as usize].width / 2.0 + 3.0);
                if crowd { continue; }
                let zc = skin.zs(tris, c[0], c[1]);
                let Some(z0) = zc.iter().copied().fold(None, |m: Option<f64>, z| Some(m.map_or(z, |m| m.max(z)))) else { continue };
                let reach = l.width / 2.0 + 4.0;
                let mut ext = [0.0f64; 2];
                for (k, sgn) in [1.0f64, -1.0].iter().enumerate() {
                    let (mut last, mut d) = (0.0, 0.25);
                    while d <= reach {
                        let p = [c[0] + nrm[0] * d * sgn, c[1] + nrm[1] * d * sgn];
                        if skin.zs(tris, p[0], p[1]).iter().any(|z| (z - z0).abs() < 1.5) { last = d; }
                        else if d - last > 0.75 { break; }
                        d += 0.25;
                    }
                    ext[k] = last;
                }
                let built = ext[0] + ext[1];
                if built < 0.75 * l.width - 0.5 || built > 1.3 * l.width + 1.5 {
                    out.push(Finding { v: built - l.width, z: l.width, line: li as u32, level: 0,
                                       ..Finding::at("WIDTH", c[0], c[1]) });
                }
            }
            dist += seg;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lm(lanes: u8, width: f64, drive: f64) -> LineMeta {
        LineMeta { id: "1".into(), level: 0, width, drive, lanes, oneway: false, paved: true, cls: "primary".into(), xy: vec![[0.0, 0.0], [10.0, 0.0]] }
    }

    #[test]
    fn rows_at_lane_centres() {
        assert_eq!(lane_rows(&lm(2, 8.0, 7.0)), vec![-4, 4]);       // +-1.75 m -> cells -4 / 4 (0.5 m)
        assert_eq!(lane_rows(&lm(1, 4.0, 3.5)), vec![0]);
        assert!(lane_rows(&lm(1, 1.0, 1.0)).is_empty());
    }

    #[test]
    fn class_speeds() {
        assert!((class_speed("motorway").0 - 31.29).abs() < 0.01);
        assert!(class_speed("residential").0 < class_speed("primary").0);
    }
}
