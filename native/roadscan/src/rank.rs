//! Located findings (every tile's .tsv) into ranked issues: each finding given an issue kind and a severity rule,
//! the same kind clustered within a few metres (single linkage), each cluster's worst member its place, severity and
//! magnitude, its score from the severity and its extent; mileposts along the route; two states' issues compared.
use std::collections::BTreeMap;
use std::fmt::Write as _;

/// One finding as the scan wrote it.
#[derive(Clone, Debug)]
pub struct Rec {
    pub code: String, pub lat: f64, pub lon: f64, pub z: f64, pub v: f64, pub level: u8, pub kind: u8, pub mask: u32,
    pub line: String, pub cls: String, pub tile: String,
}

pub const HEADER: &str = "code\tlat\tlon\tz\tv\tlevel\tkind\tmask\tline\tcls\ttile";

pub fn rec_line(r: &Rec) -> String {
    format!("{}\t{:.8}\t{:.8}\t{:.3}\t{:.4}\t{}\t{}\t{}\t{}\t{}\t{}", r.code, r.lat, r.lon, r.z, r.v, r.level, r.kind, r.mask,
            r.line, r.cls, r.tile)
}

pub fn parse_rec(s: &str) -> Option<Rec> {
    let w: Vec<&str> = s.split('\t').collect();
    if w.len() != 11 || w[0] == "code" { return None; }
    Some(Rec { code: w[0].into(), lat: w[1].parse().ok()?, lon: w[2].parse().ok()?, z: w[3].parse().unwrap_or(f64::NAN),
               v: w[4].parse().unwrap_or(f64::NAN), level: w[5].parse().ok()?, kind: w[6].parse().ok()?,
               mask: w[7].parse().ok()?, line: w[8].into(), cls: w[9].into(), tile: w[10].into() })
}

/// An issue kind's description for the report, and the unit of its magnitude.
pub fn describe(kind: &str) -> (&'static str, &'static str) {
    match kind {
        "hole" => ("nothing to drive on in a ground lane (a hole in the terrain, no road surface)", "cells"),
        "deck_missing" => ("a mapped bridge lane with no deck under it (road over air)", "cells"),
        "tunnel_floor_missing" => ("a tunnel lane with no floor", "cells"),
        "double_surface" => ("two surfaces within 5 cm under a lane (z-fighting, snagging collision)", "cells"),
        "obstruction" => ("a wall, deck, tunnel or barrier part inside a lane's 4.5 m envelope", "cells"),
        "sidewalk_in_lane" => ("a sidewalk or kerb collider inside a lane", "cells"),
        "rail_in_lane" => ("a rail part inside a lane", "cells"),
        "wall_across_lane" => ("a barrier or parapet across a lane at its own level", "cells"),
        "ground_in_structure" => ("the terrain inside a deck's or tunnel's clearance", "m"),
        "tunnel_roofless" => ("a tunnel lane with no vault over it", "cells"),
        "blocked_opening" => ("under a structure with less than 2.5 m open", "m"),
        "low_clearance" => ("under 4.25 m to the lowest part overhead", "m"),
        "pier_in_lane" => ("a pier, abutment or wall face within 1.5 m of a lower lane", "cells"),
        "step" => ("a step in the drive surface", "m"),
        "crossfall_step" => ("a step across a lane", "m"),
        "deck_end_step" => ("a step where a deck meets the ground or another deck", "m"),
        "tunnel_mouth_step" => ("a step at a tunnel floor's end", "m"),
        "tunnel_mouth_hole" => ("nothing to drive on out of a tunnel's end", "cells"),
        "kerb_step" => ("a lane's centre runs onto a kerb or sidewalk (a step onto it)", "m"),
        "lip" => ("a lip at a colliding surface's free edge (hand-over to the terrain or the next piece)", "mm"),
        "open_terrain_hole" => ("a terrain hole nothing covers (sky or void through the ground)", "squares"),
        "open_hole_edge" => ("an open edge of a terrain hole (no floor, roof or wall closing it)", "edges"),
        "crest" => ("a crest a car feels at the class's speed (negative g lifts it; -1 g leaves the road)", "g"),
        "dip" => ("a sag or bump a car feels at the class's speed", "g"),
        "steep_grade" => ("a 10 m grade over the road class's limit", "grade"),
        "terrain_over_road" => ("the terrain over the drawn road surface (ground showing through)", "m"),
        "road_floating" => ("the drawn road surface floating over what the car drives on", "m"),
        "missing_road" => ("a mapped paved lane's centre with no drawn road and no paved paint under it (the road missing, narrower than mapped or shifted off its line)", "cells"),
        "bridge_on_ground" => ("a mapped bridge lane with no deck, driven on the ground", "cells"),
        "marking_off_road" => ("painted markings outside every mapped road", "m"),
        "width_mismatch" => ("the drawn road's width against its resolved OSM width", "m"),
        _ => ("", ""),
    }
}

/// The issue kind of a finding and its magnitude (None: not an issue).
pub fn kind_of(r: &Rec) -> Option<(&'static str, f64)> {
    let deck_or_tunnel = |mask: u32, level: u8| if mask & 1 != 0 || (mask & 4 != 0 && level == 1) { "deck_end_step" }
        else if mask & 2 != 0 || (mask & 4 != 0 && level == 2) { "tunnel_mouth_step" } else { "step" };
    Some(match r.code.as_str() {
        // (a mapped bridge lane with no deck over ground the car can drive on: built on the ground, seen, not fallen
        // through; a step where the deck stops is the drive's)
        "HOLE" => (match r.level { 0 => "hole", 1 if r.v.is_finite() => "bridge_on_ground", 1 => "deck_missing",
                                   _ => "tunnel_floor_missing" }, 1.0),
        "DO" => ("double_surface", 1.0),
        "EN" => {
            let m = r.mask;
            let hard = m & (1 << 3 | 1 << 4 | 1 << 5 | 1 << 6 | 1 << 9) != 0;
            let rail = m & (1 << 7 | 1 << 8) != 0;
            (if hard { "obstruction" } else if rail { "rail_in_lane" } else { "sidewalk_in_lane" }, 1.0)
        }
        "PARAPET" => ("wall_across_lane", 1.0),
        "TE" => ("ground_in_structure", r.v),
        "RF" => ("tunnel_roofless", 1.0),
        "CS" => (if r.v < 2.5 { "blocked_opening" } else { "low_clearance" }, r.v),
        "PI" => ("pier_in_lane", 1.0),
        // (the audit's second difference on every lane cell: under 10 cm it is mostly a steep crossfall or banking on
        // the 1 m terrain lattice; the drive's own rows judge the smaller steps along the lanes)
        "STEP" => { if r.v.abs() < 0.10 { return None; } (if r.kind == 2 { "crossfall_step" } else { "step" }, r.v.abs()) }
        "BSTEP" | "ENDSTEP" => (if r.mask & 8 != 0 { "kerb_step" } else { deck_or_tunnel(r.mask, r.level) }, r.v),
        "LIP" => ("lip", r.v),
        "HO" => ("open_terrain_hole", 1.0),
        "HE" => ("open_hole_edge", 1.0),
        "MOUTH" => { if r.v <= 0.10 { return None; } ("tunnel_mouth_step", r.v) }
        "MOUTHHOLE" => ("tunnel_mouth_hole", 1.0),
        "RIDE" => (if r.v < 0.0 { "crest" } else { "dip" }, r.v),
        "GRADE" => ("steep_grade", r.v),
        "BURIED" => ("terrain_over_road", r.v),
        "FLOATSKIN" => ("road_floating", r.v),
        "MISSING" => ("missing_road", 1.0),
        "MARKOFF" => ("marking_off_road", r.v),
        "WIDTH" => ("width_mismatch", r.v),
        _ => return None,
    })
}

/// Severity (0 trace: not listed, 1 minor, 2 major, 3 critical) of a cluster of `n` findings whose worst magnitude
/// is `m`. The thresholds are the corpus's hold thresholds and the drivability design's (bridgerewrite DESIGN.md
/// section 10) where they exist.
pub fn severity(kind: &str, m: f64, n: usize) -> u8 {
    let a = m.abs();
    match kind {
        "hole" | "deck_missing" | "tunnel_floor_missing" => if n >= 2 { 3 } else { 1 },
        "double_surface" => if n >= 8 { 2 } else if n >= 2 { 1 } else { 0 },
        "obstruction" => if n >= 4 { 3 } else { 2 },
        "sidewalk_in_lane" | "rail_in_lane" => if n >= 8 { 2 } else { 1 },
        "wall_across_lane" => 3,
        "ground_in_structure" => if a < 2.0 { 3 } else { 2 },
        "tunnel_roofless" => if n >= 4 { 2 } else { 1 },
        "blocked_opening" => 3,
        "low_clearance" => if a < 3.5 { 2 } else { 1 },
        "pier_in_lane" => 2,
        "step" | "crossfall_step" | "deck_end_step" | "tunnel_mouth_step" =>
            if a > 0.15 { 3 } else if a > 0.05 { 2 } else if a > 0.03 { 1 } else { 0 },
        "tunnel_mouth_hole" => 3,
        "kerb_step" => if a > 0.10 && n >= 2 { 2 } else { 1 },
        "lip" => if a > 60.0 { 3 } else if a > 25.0 { 2 } else if a > 10.0 { 1 } else { 0 },
        "open_terrain_hole" => if n >= 4 { 2 } else { 1 },
        "open_hole_edge" => 1,
        "crest" | "dip" => if a > 1.2 { 3 } else if a > 0.8 { 2 } else { 1 },
        "steep_grade" => 1,
        "terrain_over_road" => if n < 3 { 0 } else if a > 0.15 { 2 } else { 1 },
        "road_floating" => if n < 3 { 0 } else if a > 0.3 { 2 } else { 1 },
        "missing_road" => if n >= 20 { 2 } else if n >= 6 { 1 } else { 0 },
        "bridge_on_ground" => if n >= 8 { 1 } else { 0 },
        "marking_off_road" => if n >= 6 { 1 } else { 0 },
        "width_mismatch" => 1,
        _ => 0,
    }
}

fn radius(kind: &str) -> f64 {
    match kind {
        "crest" | "dip" | "steep_grade" | "width_mismatch" | "marking_off_road" | "missing_road" | "terrain_over_road"
        | "road_floating" => 8.0,
        _ => 3.0,
    }
}

pub const SEV_WEIGHT: [f64; 4] = [0.0, 1.0, 4.0, 16.0];

/// How much a road class's issues weigh in the ranking (the roads most players drive first; an issue with no road,
/// a terrain hole or a lip, counts in full).
pub fn class_weight(cls: &str) -> f64 {
    match cls {
        "motorway" | "trunk" | "" => 1.0,
        "motorway_link" | "trunk_link" | "primary" | "secondary" => 0.8,
        "primary_link" | "secondary_link" | "tertiary" | "tertiary_link" => 0.7,
        "residential" | "unclassified" | "living_street" | "raceway" => 0.5,
        _ => 0.3,
    }
}

#[derive(Clone, Debug)]
pub struct Issue {
    pub kind: &'static str, pub sev: u8, pub score: f64, pub lat: f64, pub lon: f64, pub z: f64, pub mag: f64, pub n: usize,
    pub level: u8, pub cls: String, pub line: String, pub tile: String, pub mile: f64, pub off_m: f64, pub on_route: bool,
    pub lat0: f64, pub lon0: f64, pub lat1: f64, pub lon1: f64,
}

const M_PER_DEG: f64 = 111_320.0;

pub fn dist_m(a: (f64, f64), b: (f64, f64)) -> f64 {
    let c = ((a.0 + b.0) / 2.0).to_radians().cos();
    let dy = (a.0 - b.0) * M_PER_DEG;
    let dx = (a.1 - b.1) * M_PER_DEG * c;
    (dx * dx + dy * dy).sqrt()
}

/// Single-linkage clusters (indices into recs) of the same kind within each kind's radius, in a stable order.
fn clusters(items: &[(usize, &'static str, f64)], recs: &[Rec]) -> Vec<Vec<usize>> {
    let mut by_kind: BTreeMap<&'static str, Vec<usize>> = BTreeMap::new();
    for (k, &(_, kind, _)) in items.iter().enumerate() { by_kind.entry(kind).or_default().push(k); }
    let mut out = Vec::new();
    for (kind, ks) in by_kind {
        let r = radius(kind);
        let cell = |i: usize| {
            let p = &recs[items[i].0];
            let c = p.lat.to_radians().cos().max(0.01);
            ((p.lat * M_PER_DEG / r).floor() as i64, (p.lon * M_PER_DEG * c / r).floor() as i64)
        };
        let mut grid: BTreeMap<(i64, i64), Vec<usize>> = BTreeMap::new();
        for &k in &ks { grid.entry(cell(k)).or_default().push(k); }
        let mut parent: BTreeMap<usize, usize> = ks.iter().map(|&k| (k, k)).collect();
        fn find(p: &mut BTreeMap<usize, usize>, k: usize) -> usize {
            let mut r = k;
            while p[&r] != r { r = p[&r]; }
            let mut c = k;
            while p[&c] != r { let n = p[&c]; p.insert(c, r); c = n; }
            r
        }
        for &k in &ks {
            let (ci, cj) = cell(k);
            let pk = &recs[items[k].0];
            for di in -1..=1 { for dj in -1..=1 {
                let Some(v) = grid.get(&(ci + di, cj + dj)) else { continue };
                for &o in v {
                    if o <= k { continue; }
                    let po = &recs[items[o].0];
                    if dist_m((pk.lat, pk.lon), (po.lat, po.lon)) <= r {
                        let (a, b) = (find(&mut parent, k), find(&mut parent, o));
                        if a != b { let (lo, hi) = (a.min(b), a.max(b)); parent.insert(hi, lo); }
                    }
                }
            } }
        }
        let mut groups: BTreeMap<usize, Vec<usize>> = BTreeMap::new();
        for &k in &ks { let root = find(&mut parent, k); groups.entry(root).or_default().push(k); }
        out.extend(groups.into_values());
    }
    out
}

/// Mileposts: (miles along the route, metres off it) of a point.
pub struct Route { pts: Vec<(f64, f64)>, cum: Vec<f64>, grid: BTreeMap<(i64, i64), Vec<usize>> }

const RCELL: f64 = 0.01;    // degrees

impl Route {
    pub fn new(pts: &[[f64; 2]]) -> Route {
        let pts: Vec<(f64, f64)> = pts.iter().map(|p| (p[0], p[1])).collect();
        let mut cum = vec![0.0];
        for w in pts.windows(2) { cum.push(cum.last().unwrap() + dist_m(w[0], w[1])); }
        let mut grid: BTreeMap<(i64, i64), Vec<usize>> = BTreeMap::new();
        for (i, w) in pts.windows(2).enumerate() {
            let (a0, a1) = ((w[0].0.min(w[1].0) / RCELL).floor() as i64, (w[0].0.max(w[1].0) / RCELL).floor() as i64);
            let (b0, b1) = ((w[0].1.min(w[1].1) / RCELL).floor() as i64, (w[0].1.max(w[1].1) / RCELL).floor() as i64);
            if (a1 - a0 + 1) * (b1 - b0 + 1) > 10000 { continue; }
            for a in a0..=a1 { for b in b0..=b1 { grid.entry((a, b)).or_default().push(i); } }
        }
        Route { pts, cum, grid }
    }
    pub fn len_m(&self) -> f64 { *self.cum.last().unwrap_or(&0.0) }
    pub fn locate(&self, lat: f64, lon: f64) -> (f64, f64) {
        if self.pts.len() < 2 { return (f64::NAN, f64::NAN); }
        let (ci, cj) = ((lat / RCELL).floor() as i64, (lon / RCELL).floor() as i64);
        let mut best = (f64::INFINITY, f64::NAN);
        for ring in 0..4i64 {
            for a in ci - ring..=ci + ring { for b in cj - ring..=cj + ring {
                if (a - ci).abs() != ring && (b - cj).abs() != ring { continue; }
                let Some(v) = self.grid.get(&(a, b)) else { continue };
                for &i in v {
                    let (p, q) = (self.pts[i], self.pts[i + 1]);
                    let c = lat.to_radians().cos();
                    let (ax, ay) = ((p.1 - lon) * M_PER_DEG * c, (p.0 - lat) * M_PER_DEG);
                    let (bx, by) = ((q.1 - lon) * M_PER_DEG * c, (q.0 - lat) * M_PER_DEG);
                    let (dx, dy) = (bx - ax, by - ay);
                    let l2 = dx * dx + dy * dy;
                    let u = if l2 < 1e-12 { 0.0 } else { (-(ax * dx + ay * dy) / l2).clamp(0.0, 1.0) };
                    let (x, y) = (ax + dx * u, ay + dy * u);
                    let d = (x * x + y * y).sqrt();
                    if d < best.0 { best = (d, self.cum[i] + u * (self.cum[i + 1] - self.cum[i])); }
                }
            } }
            if best.0.is_finite() && ring >= 1 { break; }
        }
        if !best.0.is_finite() {        // (far from the route: every segment)
            for i in 0..self.pts.len() - 1 {
                let d = dist_m(self.pts[i], (lat, lon));
                if d < best.0 { best = (d, self.cum[i]); }
            }
        }
        (best.1 / 1609.344, best.0)
    }
}

pub fn rank(recs: &[Rec], route: Option<&Route>, halfwidth: f64) -> Vec<Issue> {
    let items: Vec<(usize, &'static str, f64)> = recs.iter().enumerate()
        .filter_map(|(i, r)| kind_of(r).map(|(k, m)| (i, k, m))).collect();
    let mut out = Vec::new();
    for g in clusters(&items, recs) {
        let worst = *g.iter().max_by(|&&a, &&b| items[a].2.abs().total_cmp(&items[b].2.abs()).then(b.cmp(&a))).unwrap();
        let (ri, kind, mag) = items[worst];
        let r = &recs[ri];
        let sev = severity(kind, mag, g.len());
        if sev == 0 { continue; }
        let (mut lat0, mut lon0, mut lat1, mut lon1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for &k in &g {
            let p = &recs[items[k].0];
            lat0 = lat0.min(p.lat); lat1 = lat1.max(p.lat); lon0 = lon0.min(p.lon); lon1 = lon1.max(p.lon);
        }
        let (mile, off) = route.map(|rt| rt.locate(r.lat, r.lon)).unwrap_or((f64::NAN, f64::NAN));
        let on_route = route.is_none() || off <= halfwidth;
        let score = SEV_WEIGHT[sev as usize] * (1.0 + 0.25 * (1.0 + g.len() as f64).ln()) * if on_route { 1.0 } else { 0.5 }
            * class_weight(&r.cls);
        out.push(Issue { kind, sev, score, lat: r.lat, lon: r.lon, z: r.z, mag, n: g.len(), level: r.level, cls: r.cls.clone(),
                         line: r.line.clone(), tile: r.tile.clone(), mile, off_m: off, on_route, lat0, lon0, lat1, lon1 });
    }
    out.sort_by(|a, b| b.score.total_cmp(&a.score).then(b.mag.abs().total_cmp(&a.mag.abs()))
        .then(a.lat.total_cmp(&b.lat)).then(a.lon.total_cmp(&b.lon)).then(a.kind.cmp(b.kind)));
    out
}

pub fn json_str(s: &str) -> String {
    let mut o = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""), '\\' => o.push_str("\\\\"), '\n' => o.push_str("\\n"), '\t' => o.push_str("\\t"),
            c if (c as u32) < 0x20 => { let _ = write!(o, "\\u{:04x}", c as u32); }
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

pub fn num(v: f64) -> String { if v.is_finite() { format!("{:.6}", v).trim_end_matches('0').trim_end_matches('.').to_string() } else { "null".into() } }

pub fn issue_json(i: &Issue, rank: usize) -> String {
    let (what, unit) = describe(i.kind);
    format!("{{\"rank\":{},\"kind\":{},\"severity\":{},\"score\":{},\"lat\":{:.7},\"lon\":{:.7},\"z\":{},\"magnitude\":{},\"unit\":{},\
\"cells\":{},\"level\":{},\"cls\":{},\"line\":{},\"tile\":{},\"mile\":{},\"off_route_ft\":{},\"on_route\":{},\"box\":[{:.7},{:.7},{:.7},{:.7}],\"what\":{}}}",
        rank, json_str(i.kind), i.sev, num(i.score), i.lat, i.lon, num(i.z), num(i.mag), json_str(unit), i.n, i.level,
        json_str(&i.cls), json_str(&i.line), json_str(&i.tile), num(i.mile), num(i.off_m * 3.28084), i.on_route,
        i.lat0, i.lon0, i.lat1, i.lon1, json_str(what))
}

pub const ISSUE_HEADER: &str = "rank\tkind\tseverity\tscore\tlat\tlon\tmagnitude\tcells\tlevel\tcls\tline\ttile\tmile\toff_m\ton_route\tlat0\tlon0\tlat1\tlon1";

pub fn issue_tsv(i: &Issue, rank: usize) -> String {
    format!("{}\t{}\t{}\t{:.4}\t{:.8}\t{:.8}\t{:.4}\t{}\t{}\t{}\t{}\t{}\t{:.4}\t{:.2}\t{}\t{:.8}\t{:.8}\t{:.8}\t{:.8}", rank, i.kind, i.sev,
            i.score, i.lat, i.lon, i.mag, i.n, i.level, i.cls, i.line, i.tile, i.mile, i.off_m, i.on_route as u8, i.lat0, i.lon0,
            i.lat1, i.lon1)
}

#[derive(Clone, Debug)]
pub struct Row { pub rank: usize, pub kind: String, pub sev: u8, pub score: f64, pub lat: f64, pub lon: f64, pub mag: f64,
                 pub n: usize, pub tile: String, pub mile: f64, pub on_route: bool, pub level: u8, pub cls: String,
                 pub line: String, pub bbox: [f64; 4] }

pub fn parse_issue(s: &str) -> Option<Row> {
    let w: Vec<&str> = s.split('\t').collect();
    if w.len() < 15 || w[0] == "rank" { return None; }
    let (lat, lon): (f64, f64) = (w[4].parse().ok()?, w[5].parse().ok()?);
    let f = |k: usize, d: f64| w.get(k).and_then(|v| v.parse().ok()).unwrap_or(d);
    Some(Row { rank: w[0].parse().ok()?, kind: w[1].into(), sev: w[2].parse().ok()?, score: w[3].parse().ok()?,
               lat, lon, mag: w[6].parse().unwrap_or(f64::NAN), n: w[7].parse().ok()?,
               tile: w[11].into(), mile: w[12].parse().unwrap_or(f64::NAN), on_route: w[14] == "1",
               level: w[8].parse().unwrap_or(0), cls: w[9].into(), line: w[10].into(),
               bbox: [f(15, lat), f(16, lon), f(17, lat), f(18, lon)] })
}

/// A before / after comparison of two states' issues on the same corridor: each B issue matched to the nearest
/// unmatched A issue of its kind within MATCH_M (B in rank order), the rest new (B) or gone (A).
pub const MATCH_M: f64 = 6.0;

/// A matched pair's change: worse when B's severity is higher or its extent grew by over a quarter (and 4 cells),
/// better the other way.
pub fn status(a: &Row, b: &Row) -> &'static str {
    let grew = |x: &Row, y: &Row| y.n as f64 >= 1.25 * x.n as f64 + 4.0;
    if b.sev > a.sev || (b.sev == a.sev && grew(a, b)) { "worse" }
    else if b.sev < a.sev || grew(b, a) { "better" } else { "same" }
}

pub fn compare(a: &[Row], b: &[Row], tiles_both: &dyn Fn(&str) -> bool) -> (Vec<(Option<usize>, Option<usize>)>, String) {
    let mut used = vec![false; a.len()];
    let mut pairs = Vec::new();
    for (j, y) in b.iter().enumerate() {
        if !tiles_both(&y.tile) { continue; }
        let mut best: Option<(f64, usize)> = None;
        for (i, x) in a.iter().enumerate() {
            if used[i] || x.kind != y.kind { continue; }
            let d = dist_m((x.lat, x.lon), (y.lat, y.lon));
            if d <= MATCH_M && best.map_or(true, |(bd, _)| d < bd) { best = Some((d, i)); }
        }
        if let Some((_, i)) = best { used[i] = true; pairs.push((Some(i), Some(j))); } else { pairs.push((None, Some(j))); }
    }
    for (i, x) in a.iter().enumerate() { if !used[i] && tiles_both(&x.tile) { pairs.push((Some(i), None)); } }
    let mut by: BTreeMap<(String, &'static str), [usize; 4]> = BTreeMap::new();
    for &(i, j) in &pairs {
        let (kind, sev, what) = match (i, j) {
            (Some(i), Some(j)) => (b[j].kind.clone(), b[j].sev.max(a[i].sev), status(&a[i], &b[j])),
            (None, Some(j)) => (b[j].kind.clone(), b[j].sev, "new"),
            (Some(i), None) => (a[i].kind.clone(), a[i].sev, "gone"),
            _ => continue,
        };
        by.entry((kind, what)).or_default()[sev.min(3) as usize] += 1;
    }
    let mut s = String::new();
    for ((k, w), c) in &by { let _ = writeln!(s, "{}\t{}\t{}\t{}\t{}", k, w, c[1], c[2], c[3]); }
    (pairs, s)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rec(code: &str, lat: f64, lon: f64, v: f64) -> Rec {
        Rec { code: code.into(), lat, lon, z: 0.0, v, level: 0, kind: 1, mask: 0, line: "w1".into(), cls: "primary".into(), tile: "g17_1_1".into() }
    }

    #[test]
    fn clusters_and_ranks() {
        let d = 1.0 / M_PER_DEG;      // 1 m of latitude
        let recs = vec![rec("BSTEP", 40.0, -75.0, 0.2), rec("BSTEP", 40.0 + d, -75.0, 0.1), rec("BSTEP", 40.0 + 50.0 * d, -75.0, 0.04),
                        rec("LIP", 40.0, -75.0, 3.0), rec("RIDE", 40.0 + 90.0 * d, -75.0, -0.9)];
        let is = rank(&recs, None, 40.0);
        assert_eq!(is.len(), 3);                // (the 3 mm lip is a trace)
        assert_eq!(is[0].kind, "step");
        assert_eq!(is[0].sev, 3);
        assert_eq!(is[0].n, 2);
        assert!((is[0].mag - 0.2).abs() < 1e-9);
        assert_eq!(is[1].kind, "crest");
        assert_eq!(is[2].sev, 1);
    }

    #[test]
    fn mileposts() {
        let d = 1.0 / M_PER_DEG;
        let r = Route::new(&[[40.0, -75.0], [40.0 + 1609.344 * d, -75.0], [40.0 + 2.0 * 1609.344 * d, -75.0]]);
        let (mi, off) = r.locate(40.0 + 1609.344 * 1.5 * d, -75.0 + 10.0 * d / 40f64.to_radians().cos());
        assert!((mi - 1.5).abs() < 1e-3, "{}", mi);
        assert!((off - 10.0).abs() < 0.05, "{}", off);
    }

    #[test]
    fn compare_states() {
        let row = |k: &str, lat: f64, sev: u8| Row { rank: 1, kind: k.into(), sev, score: 1.0, lat, lon: 0.0, mag: 0.1, n: 1, tile: "t".into(), mile: 0.0, on_route: true,
                                                     level: 0, cls: String::new(), line: String::new(), bbox: [lat, 0.0, lat, 0.0] };
        let d = 1.0 / M_PER_DEG;
        let a = vec![row("step", 0.0, 2), row("lip", 10.0 * d, 1)];
        let b = vec![row("step", 2.0 * d, 3), row("hole", 30.0 * d, 3)];
        let (pairs, _) = compare(&a, &b, &|_| true);
        assert_eq!(pairs, vec![(Some(0), Some(0)), (None, Some(1)), (Some(1), None)]);
    }
}
