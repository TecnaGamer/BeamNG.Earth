//! Cross-sections from OpenStreetMap's street tags (BNE_ROADSYS_XSEC=on): where a road's parking, cycle lanes, bus
//! lanes, shoulders, sidewalks and carriageway width are mapped, its strips come from them instead of a guess from the
//! lane count. A segment's tags arrive as one canonical string (server/roadsys.py _xsec_text: "key=value" pairs
//! sorted by key, joined by U+001F); an empty string is a road without them (today's rules, byte for byte).
//!
//! The model, per side of the carriageway, from the lanes outward:
//!   lanes | buffer | cycle lane | parking | shoulder (the gutter in a town)        [kerb | track | sidewalk]
//! - parking (parking:<side>=lane / half_on_kerb / on_kerb / street_side / shoulder / no / separate, with
//!   :orientation parallel / diagonal / perpendicular, :width, :restriction; the older parking:lane:<side>=parallel ...
//!   with parking:lane:<side>:<orientation>=on_street / half_on_kerb / on_kerb / ...): the share on the carriageway
//!   is a strip (half_on_kerb: half of it), its width the mapped one or the country's standard by orientation;
//! - cycle lanes (cycleway / cycleway:<side>[:lane / :width / :buffer]=lane, opposite_lane): a painted strip, advisory
//!   (dashed) or exclusive; tracks (track, opposite_track) lie beyond the kerb;
//! - bus lanes (busway[:<side>]=lane, bus:lanes / psv:lanes with the outermost lane designated): the outermost
//!   traffic lane of that side is a bus lane (it is one of the mapped `lanes`, as OSM counts them);
//! - shoulders (shoulder / shoulder:<side>[:width / :surface]): their mapped width, unpaved where the surface says;
//! - sidewalks and kerbs (sidewalk:<side>[:width / :kerb], kerb:height): modelled (Side.walk, Side.kerb), not built yet.
//! Lanes: a mapped carriageway width (width:carriageway, then OSM width, then est_width) less the strips and the
//! shoulders, shared by the lanes, when that gives a plausible lane width (LANE_MIN..LANE_MAX); OSM width that is
//! too narrow for its strips is tried as the width between the parking lanes; otherwise the lanes keep today's width
//! (a contradictory or implausible width falls back to inference, recorded in Lay.reject). Without a mapped width
//! the strips first take the room today's rules left beside the lanes (the shoulder that held a town street's assumed
//! parking), then widen the road.
//!
//! Mapped when present, else inferred: every value has its tag first and one fallback, `prior` (the only place the
//! model's defaults live: the country's standard widths, the gutter, and for a side whose tags say nothing today's
//! rules: no strip, the room today's shoulder holds). A later job can replace `prior` with priors learned from
//! well-mapped places (by country, class, setting) without touching the rest. `Lay.tags` records which attributes
//! came from tags (T_*), for the share of road length mapped vs inferred.
//!
//! Pure: + - * / only, std's exact decimal parse; no maps iterated into outputs.

pub const K_PARK: u8 = 4;
pub const K_CYCLE: u8 = 5;
pub const K_BUFFER: u8 = 6;
/// face tags only: a lane slot marked as a bus lane (the slot stays K_LANE)
pub const K_BUS: u8 = 7;
/// face tags only: an unpaved shoulder (the slot stays K_SHOULDER)
pub const K_SOFT: u8 = 8;

pub const SEP: char = '\u{1f}';
pub const GUTTER_M: f64 = 0.3;          // (prior Gutter today) a kerbed street's edge beyond its last strip: today's least shoulder
pub const LANE_MIN: f64 = 2.5;          // m per lane a mapped width may give (marked lanes)
pub const LANE_MIN_UNMARKED: f64 = 2.25;
pub const LANE_MAX: f64 = 4.6;
pub const BUFFER_YES_M: f64 = 0.5;      // (prior BufferYes today)
pub const XSEC_FORMAT: u32 = 2;         // (2: provenance and the priors' function)

/// Lay.tags: the attributes a segment's tags gave (the rest inferred, `prior`), sides in the segment's direction.
pub const T_PARK_L: u16 = 1;
pub const T_PARK_R: u16 = 2;
pub const T_CYCLE_L: u16 = 4;
pub const T_CYCLE_R: u16 = 8;
pub const T_SHOULDER_L: u16 = 16;
pub const T_SHOULDER_R: u16 = 32;
pub const T_WIDTH: u16 = 64;            // the lanes from a mapped carriageway width
pub const T_BUS: u16 = 128;
pub const T_WALK_L: u16 = 256;
pub const T_WALK_R: u16 = 512;

/// The road whose cross-section is inferred, as the priors read it.
#[derive(Clone, Copy, Debug)]
pub struct Ctx<'a> { pub code: &'a str, pub cls: &'a str, pub urban: bool, pub oneway: bool }

/// A value the model infers where the tags do not give it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Attr {
    /// m of a parking lane of this orientation (1 parallel, 2 diagonal, 3 perpendicular) mapped without a width
    ParkWidth(u8),
    /// m of a painted cycle lane mapped without a width (exclusive), and an advisory one
    CycleWidth,
    AdvisoryWidth,
    /// m of a cycle track / sidewalk mapped without a width
    TrackWidth,
    WalkWidth,
    /// m of a buffer mapped as "yes"
    BufferYes,
    /// m of a kerbed street's edge beyond its last strip
    Gutter,
    /// m of a shoulder mapped without a width, at least
    ShoulderYes,
    /// m of parking / painted cycle lane on a side whose tags say nothing about it (today: 0, the room today's
    /// shoulder holds stays there)
    Park,
    Cycle,
}

/// The model's one fallback: the value of attribute a where the tags do not give it, for the road c. Today the
/// country's standards and today's rules; meant to be replaced by priors learned from well-mapped places.
pub fn prior(a: Attr, c: &Ctx) -> f64 {
    let (pp, pd, pq, cyc, adv, track, walk) = standards(c.code);
    match a {
        Attr::ParkWidth(2) => pd,
        Attr::ParkWidth(3) => pq,
        Attr::ParkWidth(_) => pp,
        Attr::CycleWidth => cyc,
        Attr::AdvisoryWidth => adv,
        Attr::TrackWidth => track,
        Attr::WalkWidth => walk,
        Attr::BufferYes => BUFFER_YES_M,
        Attr::Gutter => GUTTER_M,
        Attr::ShoulderYes => 1.0,
        Attr::Park | Attr::Cycle => 0.0,
    }
}         // the model's version (in every key through the segments' digests)

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Side {
    pub cycle: f64,         // m of a painted cycle lane on the carriageway (0: none)
    pub adv: bool,          // the cycle lane is advisory (dashed line)
    pub buffer: f64,        // m of a painted buffer between the cycle lane and the traffic lanes
    pub park: f64,          // m of parking on the carriageway
    pub park_orient: u8,    // 0 none, 1 parallel, 2 diagonal, 3 perpendicular
    pub park_known: bool,   // the side's parking is mapped (any value but "yes"/unknown)
    pub park_cars: bool,    // cars may park on it (no no_parking / no_stopping restriction)
    pub kerb_park: f64,     // m of parking on the sidewalk (half_on_kerb's other half, on_kerb)
    pub shoulder: f64,      // m of mapped shoulder (-1: not mapped)
    pub soft: bool,         // the shoulder is unpaved
    pub track: f64,         // m of cycle track beyond the kerb
    pub walk: f64,          // m of sidewalk (0: none, -1: unknown)
    pub kerb: f64,          // m kerb height (-1: unknown)
    pub bus: bool,          // the outermost traffic lane of this side is a bus lane
    pub cycle_known: bool,  // the side's cycleway is mapped (lane, track, no, shared_lane, ...)
}

impl Side {
    fn none() -> Side { Side { shoulder: -1.0, walk: -1.0, kerb: -1.0, ..Default::default() } }
    pub fn strips(&self) -> f64 { self.buffer + self.cycle + self.park }
}

/// A segment's tags read, sides in the segment's own direction (its points' order: the OSM way's).
#[derive(Clone, Debug, PartialEq)]
pub struct XSec {
    pub left: Side,
    pub right: Side,
    pub cw: f64,            // width:carriageway (-1: none)
    pub width: f64,         // OSM width (-1)
    pub est: f64,           // est_width (-1)
}

/// The strips of one side as built (stroke terms).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct SideW { pub buffer: f64, pub cycle: f64, pub park: f64, pub shoulder: f64, pub adv: bool, pub soft: bool, pub bus: bool }

impl SideW {
    pub fn strips(&self) -> f64 { self.buffer + self.cycle + self.park }
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Lay {
    pub lane_w: f64,        // m per lane (-1: today's)
    pub left: SideW,
    pub right: SideW,
    pub src: u8,            // 0 today's lanes, 1 width:carriageway, 2 width, 4 est_width
    pub reject: u8,         // 1: a mapped width gave no plausible lane width (today's lanes kept); 2: the strips squeezed into it; 3: squeezed under half (contradictory tags)
    pub tags: u16,          // T_*: what came from tags (the rest from `prior`)
}

/// Where a laid-out value came from (the adaptive plan's evidence ladder reads this: J3 turns it into its ranks).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Src { Tag, Prior }

/// The attributes Lay records the source of (per side where it has one).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Field { Park, Cycle, Shoulder, Walk, LaneWidth, Bus }

impl Lay {
    /// Where field f (of the left side when left, else the right; LaneWidth and Bus: the road's) came from: its
    /// segment's tags, or `prior` (an inferred default: "unknown" in the tags, never "none").
    pub fn source(&self, f: Field, left: bool) -> Src {
        let bit = match (f, left) {
            (Field::Park, true) => T_PARK_L, (Field::Park, false) => T_PARK_R,
            (Field::Cycle, true) => T_CYCLE_L, (Field::Cycle, false) => T_CYCLE_R,
            (Field::Shoulder, true) => T_SHOULDER_L, (Field::Shoulder, false) => T_SHOULDER_R,
            (Field::Walk, true) => T_WALK_L, (Field::Walk, false) => T_WALK_R,
            (Field::LaneWidth, _) => T_WIDTH, (Field::Bus, _) => T_BUS,
        };
        if self.tags & bit != 0 { Src::Tag } else { Src::Prior }
    }
}

fn get<'a>(t: &'a [(&'a str, &'a str)], k: &str) -> Option<&'a str> {
    t.binary_search_by(|x| x.0.cmp(k)).ok().map(|i| t[i].1)
}

/// m of a width value ("2.5", "2.5 m", "8 ft", "8'", "2,5"), None when not a plain positive number.
pub fn metres(v: &str) -> Option<f64> {
    let v = v.trim();
    let (num, unit) = match v.find(|c: char| !(c.is_ascii_digit() || c == '.' || c == ',')) {
        Some(i) => (&v[..i], v[i..].trim()),
        None => (v, ""),
    };
    let num = num.replace(',', ".");
    let x: f64 = num.parse().ok()?;
    let f = match unit { "" | "m" => 1.0, "ft" | "'" | "feet" => 0.3048, "cm" => 0.01, _ => return None };
    let m = x * f;
    if m > 0.0 && m < 100.0 { Some(m) } else { None }
}

fn in_range(v: Option<f64>, lo: f64, hi: f64) -> Option<f64> { v.filter(|&x| x >= lo && x <= hi) }

/// The country's standard widths: (parallel, diagonal, perpendicular parking, cycle lane, advisory lane, track, walk).
fn standards(code: &str) -> (f64, f64, f64, f64, f64, f64, f64) {
    match code {
        "US" | "CA" => (2.4, 5.0, 5.5, 1.5, 1.5, 1.8, 1.5),
        "AU" | "NZ" => (2.3, 5.0, 5.4, 1.5, 1.5, 1.8, 1.5),
        "DE" | "AT" | "CH" => (2.0, 4.5, 5.0, 1.85, 1.5, 2.0, 2.5),
        "NL" | "BE" => (2.0, 4.5, 5.0, 1.75, 1.6, 2.0, 2.0),
        "DK" | "SE" | "NO" | "FI" => (2.0, 4.5, 5.0, 1.7, 1.5, 2.2, 2.0),
        "GB" | "IE" => (2.0, 4.5, 4.8, 1.5, 1.5, 2.0, 2.0),
        "JP" => (2.0, 4.5, 5.0, 1.5, 1.5, 2.0, 2.0),
        _ => (2.0, 4.5, 5.0, 1.5, 1.5, 2.0, 2.0),
    }
}

fn orient_of(v: &str) -> u8 {
    match v { "parallel" => 1, "diagonal" => 2, "perpendicular" => 3, _ => 0 }
}

/// A side's parking from the tags (`side` "left" / "right").
fn parking(t: &[(&str, &str)], side: &str, c: &Ctx, s: &mut Side) {
    let k = |suffix: &str| -> Option<&str> {
        get(t, &format!("parking:{side}{suffix}")).or_else(|| get(t, &format!("parking:both{suffix}")))
    };
    // (the current scheme first; the older parking:lane:* where the current one says nothing)
    let (mut pos, mut orient, mut cars) = (None::<&str>, 0u8, true);
    if let Some(v) = k("") {
        pos = Some(v);
        orient = orient_of(k(":orientation").unwrap_or(""));
        if let Some(r) = k(":restriction") { if matches!(r, "no_parking" | "no_stopping" | "no_standing") { cars = false; } }
    } else {
        let lk = |suffix: &str| -> Option<&str> {
            get(t, &format!("parking:lane:{side}{suffix}")).or_else(|| get(t, &format!("parking:lane:both{suffix}")))
        };
        if let Some(v) = lk("") {
            let o = orient_of(v);
            if o > 0 {
                orient = o;
                let p = lk(&format!(":{v}")).unwrap_or("on_street");
                pos = Some(match p { "on_street" | "painted_area_only" | "marked" => "lane", "half_on_kerb" => "half_on_kerb",
                                     "on_kerb" | "shoulder" => if p == "shoulder" { "shoulder" } else { "on_kerb" },
                                     "lay_by" | "street_side" => "street_side", _ => "lane" });
            } else {
                pos = Some(match v { "marked" => "lane", "no_parking" | "no_stopping" | "no_standing" | "no" | "fire_lane" => "no",
                                     "separate" => "separate", _ => "yes" });
            }
        }
    }
    let Some(pos) = pos else { return };
    let orient = if orient == 0 { 1 } else { orient };
    let std_w = prior(Attr::ParkWidth(orient), c);
    let w = in_range(k(":width").and_then(metres), 1.5, 7.0).unwrap_or(std_w);
    s.park_known = pos != "yes";
    s.park_cars = cars;
    match pos {
        "lane" | "yes" | "shoulder" => { s.park = w; s.park_orient = orient; }
        "half_on_kerb" => { s.park = w * 0.5; s.kerb_park = w * 0.5; s.park_orient = orient; }
        "on_kerb" => { s.kerb_park = w; s.park_orient = orient; }
        _ => {}     // street_side (bays beside the carriageway), no, separate
    }
}

fn is_soft(v: &str) -> bool {
    matches!(v, "gravel" | "grass" | "dirt" | "unpaved" | "compacted" | "ground" | "fine_gravel" | "earth" | "sand"
                | "pebblestone" | "grass_paver" | "mud")
}

/// The tags of a segment (an empty string: None). code: its country; dir: its one-way flow along its points (1,
/// -1, 0 two-way); lht: left-hand traffic.
pub fn parse(text: &str, c: &Ctx, dir: i64, lht: bool) -> Option<XSec> {
    if text.is_empty() { return None; }
    let mut t: Vec<(&str, &str)> = text.split(SEP).filter_map(|kv| kv.split_once('=')).map(|(k, v)| (k.trim(), v.trim())).collect();
    t.sort();
    t.dedup_by(|a, b| a.0 == b.0);
    let mut sides = [Side::none(), Side::none()];      // [left, right] of the way
    // the driver's side (a one-way's cycleway=lane, busway=lane): right of travel (left in LHT); travel along the
    // points unless dir == -1
    let near = |left_side: bool| -> bool {
        if dir == 0 { return true; }
        let travel_right_is_way_right = (dir >= 0) != lht;
        // the near side of travel, in way terms
        if travel_right_is_way_right { !left_side } else { left_side }
    };
    for (i, side) in ["left", "right"].iter().enumerate() {
        let s = &mut sides[i];
        let left_side = i == 0;
        parking(&t, side, c, s);
        // cycle lanes and tracks
        let cv = get(&t, &format!("cycleway:{side}")).or_else(|| get(&t, "cycleway:both")).or_else(|| {
            let base = get(&t, "cycleway")?;
            match base {
                "lane" | "track" if near(left_side) => Some(base),
                "lane" | "track" => None,
                "opposite_lane" | "opposite_track" if dir != 0 && !near(left_side) => Some(base),
                _ => None,
            }
        });
        let ck = |suffix: &str| -> Option<&str> {
            get(&t, &format!("cycleway:{side}{suffix}")).or_else(|| get(&t, &format!("cycleway:both{suffix}")))
                .or_else(|| get(&t, &format!("cycleway{suffix}")))
        };
        s.cycle_known = cv.is_some() || get(&t, &format!("cycleway:{side}")).is_some() || get(&t, "cycleway:both").is_some()
            || get(&t, "cycleway").is_some();
        match cv {
            Some("lane") | Some("opposite_lane") => {
                let kind = ck(":lane").unwrap_or("");
                if kind != "pictogram" {
                    s.adv = matches!(kind, "advisory" | "suggestion" | "soft_lane");
                    let w = in_range(ck(":width").and_then(metres).or_else(|| get(&t, "cycleway:width").and_then(metres)), 0.8, 3.0);
                    s.cycle = w.unwrap_or(prior(if s.adv { Attr::AdvisoryWidth } else { Attr::CycleWidth }, c));
                    let buf = ck(":buffer").or_else(|| ck(":buffer:left"));
                    s.buffer = match buf { Some("yes") => prior(Attr::BufferYes, c), Some(v) => in_range(metres(v), 0.0, 2.0).unwrap_or(0.0), None => 0.0 };
                }
            }
            Some("track") | Some("opposite_track") => {
                s.track = in_range(ck(":width").and_then(metres), 0.8, 4.0).unwrap_or(prior(Attr::TrackWidth, c));
            }
            _ => {}
        }
        // bus lanes
        let bv = get(&t, &format!("busway:{side}")).or_else(|| get(&t, "busway:both")).or_else(|| {
            match get(&t, "busway")? { "lane" if dir == 0 || near(left_side) => Some("lane"), _ => None }
        });
        if bv == Some("lane") { s.bus = true; }
        // shoulders
        let sh = get(&t, &format!("shoulder:{side}")).or_else(|| get(&t, "shoulder:both")).or_else(|| match get(&t, "shoulder")? {
            "yes" | "both" => Some("yes"),
            "no" => Some("no"),
            v if v == *side => Some("yes"),
            "left" | "right" => Some("no"),
            _ => None,
        });
        let shk = |suffix: &str| -> Option<&str> {
            get(&t, &format!("shoulder:{side}{suffix}")).or_else(|| get(&t, &format!("shoulder:both{suffix}"))).or_else(|| get(&t, &format!("shoulder{suffix}")))
        };
        match sh {
            Some("no") => s.shoulder = 0.0,
            Some("yes") => {
                s.shoulder = in_range(shk(":width").and_then(metres), 0.3, 5.0).unwrap_or(-2.0);   // -2: there, width unknown
                s.soft = shk(":surface").map(is_soft).unwrap_or(false);
            }
            _ => {}
        }
        // sidewalks and kerbs (modelled; nothing built from them yet)
        let wv = get(&t, &format!("sidewalk:{side}")).or_else(|| get(&t, "sidewalk:both")).or_else(|| match get(&t, "sidewalk")? {
            "both" => Some("yes"),
            "no" | "none" => Some("no"),
            "separate" => Some("separate"),
            v if v == *side => Some("yes"),
            "left" | "right" => Some("no"),
            _ => None,
        });
        let wk = |suffix: &str| -> Option<&str> {
            get(&t, &format!("sidewalk:{side}{suffix}")).or_else(|| get(&t, &format!("sidewalk:both{suffix}"))).or_else(|| get(&t, &format!("sidewalk{suffix}")))
        };
        match wv {
            Some("yes") => s.walk = in_range(wk(":width").and_then(metres), 0.5, 10.0).unwrap_or(prior(Attr::WalkWidth, c)),
            Some("no") | Some("separate") => s.walk = 0.0,
            _ => {}
        }
        s.kerb = match wk(":kerb") { Some("raised") => 0.12, Some("rolled") => 0.08, Some("lowered") => 0.03, Some("flush") | Some("no") => 0.0, _ => -1.0 };
        if let Some(h) = get(&t, &format!("kerb:{side}:height")).or_else(|| get(&t, "kerb:height")).and_then(metres) {
            let h = if h > 1.0 { h * 0.01 } else { h };      // ("12" in cm)
            if h <= 0.4 { s.kerb = h; }
        }
    }
    // the outermost lane of a direction designated for buses (bus:lanes / psv:lanes): the travel direction's near side
    let bus_last = |k: &str| get(&t, k).map(|v| v.rsplit('|').next().unwrap_or("").trim() == "designated").unwrap_or(false);
    if dir == 0 {
        let (fwd, bwd) = (bus_last("bus:lanes:forward") || bus_last("psv:lanes:forward"), bus_last("bus:lanes:backward") || bus_last("psv:lanes:backward"));
        // forward travel's near side: the way's right (left in LHT); backward's the other
        if fwd { sides[if lht { 0 } else { 1 }].bus = true; }
        if bwd { sides[if lht { 1 } else { 0 }].bus = true; }
    } else if bus_last("bus:lanes") || bus_last("psv:lanes") {
        let i = if near(true) { 0 } else { 1 };
        sides[i].bus = true;
    }
    let num = |k: &str, lo: f64, hi: f64| in_range(get(&t, k).and_then(metres), lo, hi).unwrap_or(-1.0);
    Some(XSec { left: sides[0], right: sides[1], cw: num("width:carriageway", 2.0, 40.0), width: num("width", 2.0, 40.0),
                est: num("est_width", 2.0, 40.0) })
}

impl XSec {
    /// The same with its sides in the other direction (a stroke running against the segment's points).
    pub fn flipped(&self) -> XSec { XSec { left: self.right, right: self.left, ..self.clone() } }

    /// Whether anything on it changes the strips or the lanes.
    pub fn shapes(&self) -> bool {
        let sh = |s: &Side| s.strips() > 0.0 || s.park_known || s.shoulder != -1.0 || s.bus;
        sh(&self.left) || sh(&self.right) || self.cw > 0.0 || self.est > 0.0
    }
}

/// The built strips (stroke terms: x's sides already the stroke's), from x, the carriageway's lane count n, today's
/// lane width and today's shoulders (left, right); urban: a town street (the gutter where its edge is mapped);
/// marked: its lanes are mapped (lanes=*); motorway: motorway / trunk (lanes keep the region's width).
pub fn layout(x: &XSec, c: &Ctx, n: i64, lane_today: f64, sh_today: (f64, f64), marked: bool, motorway: bool) -> Lay {
    layout_w(x, c, n, lane_today, sh_today, marked, motorway, true)
}

/// layout, and with widen false never wider than today's carriageway without a mapped width (a structure: its deck
/// meets approaches and abutments drawn for today's width; the strips squeezed in with the lanes at their least).
#[allow(clippy::too_many_arguments)]
pub fn layout_w(x: &XSec, c: &Ctx, n: i64, lane_today: f64, sh_today: (f64, f64), marked: bool, motorway: bool,
                widen: bool) -> Lay {
    let urban = c.urban;
    let gutter = prior(Attr::Gutter, c);
    let side = |s: &Side, today: f64| -> SideW {
        // (a side's parking / cycle lane: its tags', else the prior's)
        let park = if s.park_known || s.park > 0.0 { s.park } else { prior(Attr::Park, c) };
        let cycle = if s.cycle_known { s.cycle } else { prior(Attr::Cycle, c) };
        let strips = s.buffer + cycle + park;
        let sh = if s.shoulder >= 0.0 { s.shoulder.max(gutter) }
                 else if s.shoulder == -2.0 { today.max(prior(Attr::ShoulderYes, c)) }
                 else if urban && (s.park_known || park > 0.0) { gutter }
                 else { (today - strips).max(gutter).min(today.max(gutter)) };
        SideW { buffer: s.buffer, cycle, park, shoulder: sh, adv: s.adv, soft: s.soft && s.shoulder != 0.0, bus: s.bus }
    };
    let (l, r) = (side(&x.left, sh_today.0), side(&x.right, sh_today.1));
    let mut tags = 0u16;
    for (s, p, cy, sh, w) in [(&x.left, T_PARK_L, T_CYCLE_L, T_SHOULDER_L, T_WALK_L), (&x.right, T_PARK_R, T_CYCLE_R, T_SHOULDER_R, T_WALK_R)] {
        if s.park_known || s.park > 0.0 { tags |= p; }
        if s.cycle_known { tags |= cy; }
        if s.shoulder != -1.0 { tags |= sh; }
        if s.walk >= 0.0 { tags |= w; }
        if s.bus { tags |= T_BUS; }
    }
    let mut out = Lay { lane_w: -1.0, left: l, right: r, src: 0, reject: 0, tags };
    if motorway || n < 1 { return out; }
    let lo = if marked { LANE_MIN } else { LANE_MIN_UNMARKED };
    let nn = n as f64;
    let strips = l.buffer + l.cycle + l.park + r.buffer + r.cycle + r.park;
    let shoulders = l.shoulder + r.shoulder;
    let mut tried = false;
    for (src, w) in [(1u8, x.cw), (2u8, x.width), (4u8, x.est)] {
        if !(w > 0.0) { continue; }
        tried = true;
        let lw = (w - strips - shoulders) / nn;
        if lw >= lo && lw <= LANE_MAX { out.lane_w = lw; out.src = src; out.tags |= T_WIDTH; return out; }
        if lw < lo && strips > 0.0 {
            // a mapped width too narrow for its lanes beside the mapped strips: the width is the kerb-to-kerb one (the
            // area:highway outlines say so: read as the width between the parking it drew Berlin's streets 1-2 m too
            // wide), the lanes at their least, the strips squeezed into the rest while each keeps half its width
            // (under half of them left: the tags contradict each other, recorded as 3; none left: no strips, the
            // lanes share the width; the mapped width stays the total either way)
            let rest = w - shoulders - lo * nn;
            if w - shoulders > 2.0 * nn {
                let f = if rest > 0.0 { rest / strips } else { 0.0 };
                for sd in [&mut out.left, &mut out.right] { sd.buffer *= f; sd.cycle *= f; sd.park *= f; }
                out.lane_w = if rest > 0.0 { lo } else { (w - shoulders) / nn };
                out.src = src; out.reject = if f >= 0.5 { 2 } else { 3 }; out.tags |= T_WIDTH;
                return out;
            }
        }
    }
    if tried { out.reject = 1; }
    // no mapped width (or none plausible): today's carriageway is the estimate of the kerb-to-kerb width (its class
    // widths were fitted to aerials), so strips that do not fit the room beside today's lanes narrow the lanes, to
    // their least, before they widen the road (Wroclaw's cycle lanes added on top of it: area:highway bias +0.7 -> +1.2 m)
    let budget = nn * lane_today + sh_today.0.max(gutter) + sh_today.1.max(gutter);
    let need = nn * lane_today + strips + shoulders;
    if need > budget + 1e-9 && lane_today > lo {
        let lw = (budget - strips - shoulders) / nn;
        out.lane_w = if lw > lo { lw } else { lo };
        if !widen && lw < lo {
            let rest = budget - shoulders - lo * nn;
            let f = if rest > 0.0 && strips > 0.0 { rest / strips } else { 0.0 };
            for sd in [&mut out.left, &mut out.right] { sd.buffer *= f; sd.cycle *= f; sd.park *= f; }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ctx(code: &'static str) -> Ctx<'static> { Ctx { code, cls: "residential", urban: false, oneway: false } }

    #[test]
    fn every_default_is_the_priors_and_tags_win_over_them() {
        let c = ctx("DE");
        assert_eq!(prior(Attr::ParkWidth(1), &c), 2.0);
        assert_eq!(prior(Attr::CycleWidth, &c), 1.85);
        assert_eq!(prior(Attr::Park, &c), 0.0);
        let x = parse(&tx(&[("parking:right", "lane"), ("parking:right:width", "2.3"), ("cycleway:left", "no")]), &c, 0, false).unwrap();
        assert_eq!(x.right.park, 2.3);
        let l = layout(&x, &Ctx { urban: true, ..c }, 2, 3.3, (1.0, 1.0), true, false);
        assert_eq!(l.tags, T_PARK_R | T_CYCLE_L);
        assert_eq!(l.source(Field::Park, false), Src::Tag);
        assert_eq!(l.source(Field::Park, true), Src::Prior);
        // the side without tags keeps today's room (the prior's: no strip)
        assert_eq!(l.left.park, 0.0);
        assert_eq!(l.left.shoulder, 1.0);
    }

    fn tx(pairs: &[(&str, &str)]) -> String {
        let mut v: Vec<String> = pairs.iter().map(|(k, v)| format!("{k}={v}")).collect();
        v.sort();
        v.join(&SEP.to_string())
    }

    #[test]
    fn widths_parse_in_metres_and_feet() {
        assert_eq!(metres("2.5"), Some(2.5));
        assert_eq!(metres("2,5 m"), Some(2.5));
        assert_eq!(metres("10 ft"), Some(3.048));
        assert_eq!(metres("8'"), Some(8.0 * 0.3048));
        assert_eq!(metres("wide"), None);
        assert_eq!(metres("-3"), None);
    }

    #[test]
    fn parking_both_sides_narrows_the_lanes_of_a_mapped_width() {
        let x = parse(&tx(&[("parking:both", "lane"), ("parking:both:orientation", "parallel"), ("width", "11")]), &ctx("DE"), 0, false).unwrap();
        assert_eq!(x.left.park, 2.0);
        assert_eq!(x.right.park, 2.0);
        let l = layout(&x, &Ctx { urban: true, ..ctx("US") }, 2, 3.3, (1.0, 1.0), true, false);
        assert_eq!(l.src, 2);
        // 11 - 2 x 2.0 parking - 2 x 0.3 gutter = 6.4 m for two lanes
        assert!((l.lane_w - 3.2).abs() < 1e-12, "{}", l.lane_w);
        assert_eq!(l.left.shoulder, GUTTER_M);
    }

    #[test]
    fn the_older_parking_lane_scheme_and_half_on_kerb() {
        let x = parse(&tx(&[("parking:lane:right", "diagonal"), ("parking:lane:right:diagonal", "half_on_kerb"),
                            ("parking:lane:left", "no_stopping")]), &ctx("NL"), 0, false).unwrap();
        assert_eq!(x.right.park_orient, 2);
        assert_eq!(x.right.park, 2.25);
        assert_eq!(x.right.kerb_park, 2.25);
        assert!(x.left.park_known && x.left.park == 0.0);
    }

    #[test]
    fn a_one_way_cycleway_lane_is_on_the_drivers_side_and_opposite_lane_on_the_other() {
        let x = parse(&tx(&[("cycleway", "lane")]), &ctx("FR"), 1, false).unwrap();
        assert!(x.right.cycle > 0.0 && x.left.cycle == 0.0);
        let x = parse(&tx(&[("cycleway", "opposite_lane")]), &ctx("FR"), 1, false).unwrap();
        assert!(x.left.cycle > 0.0 && x.right.cycle == 0.0);
        // a one-way against its points: travel's right is the way's left
        let x = parse(&tx(&[("cycleway", "lane")]), &ctx("FR"), -1, false).unwrap();
        assert!(x.left.cycle > 0.0 && x.right.cycle == 0.0);
        // left-hand traffic
        let x = parse(&tx(&[("cycleway", "lane")]), &ctx("GB"), 1, true).unwrap();
        assert!(x.left.cycle > 0.0);
    }

    #[test]
    fn advisory_lanes_buffers_and_mapped_widths() {
        let x = parse(&tx(&[("cycleway:right", "lane"), ("cycleway:right:lane", "advisory"), ("cycleway:right:width", "1.25"),
                            ("cycleway:right:buffer", "yes")]), &ctx("DE"), 0, false).unwrap();
        assert!(x.right.adv);
        assert_eq!(x.right.cycle, 1.25);
        assert_eq!(x.right.buffer, BUFFER_YES_M);
        let x = parse(&tx(&[("cycleway:both", "lane"), ("cycleway:both:lane", "pictogram")]), &ctx("DE"), 0, false).unwrap();
        assert_eq!(x.left.cycle, 0.0);
    }

    #[test]
    fn an_implausible_width_falls_back_to_todays_lanes() {
        // 30 m on a two-lane street: a right of way, not a carriageway
        let x = parse(&tx(&[("width", "30"), ("parking:both", "no")]), &ctx("US"), 0, false).unwrap();
        let l = layout(&x, &Ctx { urban: true, ..ctx("US") }, 2, 3.3, (2.3, 2.3), true, false);
        assert_eq!(l.lane_w, -1.0);
        assert_eq!(l.reject, 1);
        // parking mapped "no" on a town street: the shoulders are the gutter
        assert_eq!(l.left.shoulder, GUTTER_M);
    }

    #[test]
    fn a_width_too_narrow_for_its_parking_squeezes_the_strips_into_it() {
        let x = parse(&tx(&[("width", "6.5"), ("parking:both", "lane")]), &ctx("US"), 0, false).unwrap();
        let l = layout(&x, &Ctx { urban: true, ..ctx("US") }, 2, 3.3, (0.3, 0.3), true, false);
        // 6.5 - 0.6 gutters - 2 x 2.5 lanes = 0.9 m of the 4.8 m of parking: under half, a contradiction; the width holds
        assert_eq!((l.src, l.reject), (2, 3));
        assert_eq!(l.lane_w, LANE_MIN);
        assert!((l.left.park + l.right.park - 0.9).abs() < 1e-9);
        let x = parse(&tx(&[("width", "9.5"), ("parking:both", "lane")]), &ctx("US"), 0, false).unwrap();
        let l = layout(&x, &Ctx { urban: true, ..ctx("US") }, 2, 3.3, (0.3, 0.3), true, false);
        assert!((l.left.park + l.right.park + 2.0 * l.lane_w + 0.6 - 9.5).abs() < 1e-9);
    }

    #[test]
    fn strips_take_todays_room_first_without_a_width() {
        // a cycle lane on a road without parking room: the lanes narrow (to 2.55 m), the road keeps today's width
        let x = parse(&tx(&[("cycleway:both", "lane")]), &ctx("US"), 0, false).unwrap();
        let l = layout(&x, &Ctx { urban: true, ..ctx("US") }, 2, 3.3, (0.3, 0.3), true, false);
        assert!((l.lane_w - 1.8).abs() > 0.0 && l.lane_w == LANE_MIN.max(3.3 - 1.5), "{}", l.lane_w);
        assert_eq!(l.tags & T_WIDTH, 0);
        let x = parse(&tx(&[("cycleway:right", "lane")]), &ctx("US"), 0, false).unwrap();
        let l = layout(&x, &Ctx { urban: true, ..ctx("US") }, 2, 3.3, (2.3, 2.3), true, false);
        assert_eq!(l.lane_w, -1.0);
        assert!((l.right.shoulder - 0.8).abs() < 1e-12);
        assert_eq!(l.left.shoulder, 2.3);
    }

    #[test]
    fn bus_lanes_and_shoulders() {
        let x = parse(&tx(&[("busway:right", "lane"), ("shoulder", "right"), ("shoulder:width", "2"), ("shoulder:surface", "gravel")]), &ctx("US"), 0, false).unwrap();
        assert!(x.right.bus && !x.left.bus);
        assert_eq!(x.right.shoulder, 2.0);
        assert!(x.right.soft);
        assert_eq!(x.left.shoulder, 0.0);
        let x = parse(&tx(&[("bus:lanes:forward", "|designated"), ("bus:lanes:backward", "designated|")]), &ctx("US"), 0, false).unwrap();
        assert!(x.right.bus && !x.left.bus);
    }

    #[test]
    fn sidewalks_and_kerbs_are_read() {
        let x = parse(&tx(&[("sidewalk:left", "yes"), ("sidewalk:left:width", "3"), ("sidewalk:right", "separate"), ("sidewalk:left:kerb", "lowered")]), &ctx("DE"), 0, false).unwrap();
        assert_eq!(x.left.walk, 3.0);
        assert_eq!(x.right.walk, 0.0);
        assert_eq!(x.left.kerb, 0.03);
        assert!(!x.shapes());
    }

    #[test]
    fn the_same_tags_in_any_order_read_the_same() {
        let a = "parking:right=lane\u{1f}width=9\u{1f}cycleway:left=lane";
        let b = "width=9\u{1f}cycleway:left=lane\u{1f}parking:right=lane";
        assert_eq!(parse(a, &ctx("DE"), 0, false), parse(b, &ctx("DE"), 0, false));
    }
}

#[cfg(test)]
mod net_tests {
    use crate::geom::P2;
    use crate::io::{Place, Seg};
    use crate::{mark, net, part, prof, product};

    fn seg(fid: &str, pts: &[[f64; 2]], dir: i64, cons: &[(&str, usize)], xsec: &str) -> Seg {
        Seg { fid: fid.into(), code: "DE".into(), cls_s: "residential".into(), pave: 11.0, drive: 6.6, inferred: 2, cls: 6,
              dir, lanes: if dir == 0 { 2 } else { 1 }, lfw: -1, lbw: -1, kmh: 50.0, lane_w: 3.3, urban: true, pts: pts.to_vec(),
              cons: cons.iter().map(|(c, v)| (c.to_string(), *v)).collect(), xsec: xsec.replace(';', "\u{1f}"), ..Default::default() }
    }

    fn street(xa: &str, xb: &str, on: bool) -> Place {
        let segs = vec![
            seg("a", &[[-200.0, 0.0], [0.0, 0.0]], 0, &[("n0", 1)], xa),
            seg("b", &[[0.0, 0.0], [200.0, 0.0]], 0, &[("n0", 0)], xb),
            seg("c", &[[0.0, -150.0], [0.0, 0.0]], 0, &[("n0", 1)], ""),
        ];
        Place { segs, xsec: on, ..Default::default() }
    }

    fn built(p: &Place) -> (net::Net, part::Built, mark::Marks) {
        let mut n = net::build(p, &(0..p.segs.len()).collect::<Vec<_>>());
        let lay = part::layout(&mut n);
        let pr = prof::solve(&n, &|pts: &[P2]| vec![0.0; pts.len()], None);
        let b = part::mesh(&n, &lay, &pr, None);
        let (m, _) = mark::marks(&n, &b.mesh, &b.rows, &p.style, None);
        (n, b, m)
    }

    fn stroke_of<'a>(n: &'a net::Net, x: f64) -> &'a net::Stroke {
        n.strokes.iter().find(|st| st.line.p.iter().any(|q| (q[0] - x).abs() < 1.0 && q[1].abs() < 1e-6)).unwrap()
    }

    #[test]
    fn parking_on_both_sides_takes_its_strips_out_of_the_mapped_width() {
        let p = street("parking:both=lane;width=11", "", true);
        let (n, b, _m) = built(&p);
        let st = stroke_of(&n, -200.0);
        assert_eq!(st.slots.len(), 2 + 2 + 6);
        let s = if st.line.p[0][0] < -100.0 { 100.0 } else { st.len() - 100.0 };
        let mut off = Vec::new();
        st.offsets(s, &mut off);
        // 0.3 gutter + 2.0 parking + 2 x 3.2 lanes + 2.0 parking + 0.3 gutter, centred on the line
        assert!((off[0] - 5.5).abs() < 1e-9 && (off[off.len() - 1] + 5.5).abs() < 1e-9, "{off:?}");
        let w: Vec<f64> = (0..st.slots.len()).map(|k| st.slots[k].w(s)).collect();
        let lanes: Vec<f64> = (0..w.len()).filter(|&k| st.slots[k].kind == net::K_LANE).map(|k| w[k]).collect();
        assert!(lanes.iter().all(|&v| (v - 3.2).abs() < 1e-9), "{w:?}");
        assert!(b.mesh.tag.iter().any(|t| t.kind == super::K_PARK));
        assert_eq!(n.stats.xsec_width, 1);
    }

    #[test]
    fn the_model_on_without_tags_builds_todays_surface_and_markings() {
        let (_n0, b0, m0) = built(&street("", "", false));
        let (_n1, b1, m1) = built(&street("", "", true));
        assert_eq!(b0.mesh.v, b1.mesh.v);
        assert_eq!(b0.mesh.t, b1.mesh.t);
        assert_eq!(m0.v, m1.v);
        assert_eq!(m0.t, m1.t);
        assert!(b0.mesh.tag.iter().zip(b1.mesh.tag.iter()).all(|(a, b)| a.kind == b.kind && a.piece == b.piece));
    }

    #[test]
    fn a_cycle_lane_gets_its_lines_and_a_bus_lane_its_face() {
        let (_n0, _b0, m0) = built(&street("", "", true));
        let (n, b, m) = built(&street("cycleway:right=lane;busway:left=lane;lanes=4", "", true));
        assert!(m.t.len() > m0.t.len());
        assert!(b.mesh.tag.iter().any(|t| t.kind == super::K_CYCLE));
        assert!(b.mesh.tag.iter().any(|t| t.kind == super::K_BUS));
        assert!(n.stats.xsec_shaped >= 1);
    }

    #[test]
    fn a_one_way_without_placement_centres_its_whole_carriageway() {
        let p = Place { segs: vec![seg("o", &[[-200.0, 0.0], [200.0, 0.0]], 1, &[], "cycleway:right=lane;parking:right=lane;parking:left=no")],
                        xsec: true, ..Default::default() };
        let (n, _b, _m) = built(&p);
        let st = &n.strokes[0];
        let mut off = Vec::new();
        st.offsets(200.0, &mut off);
        // lanes 3.3, cycle 1.85, parking 2.0 on the right, centred: left edge of the lanes at (3.3 + 3.85) / 2 - 0 ...
        let (l, r) = (off[1], off[off.len() - 2]);     // inside the shoulders
        assert!((l + r).abs() < 1e-9, "{off:?}");
    }

    #[test]
    fn the_same_tile_bytes_whatever_the_input_order() {
        let mut p = street("parking:both=lane;width=11;cycleway:both=lane", "parking:right=half_on_kerb", true);
        let q = [[-128.0, -128.0], [128.0, -128.0], [128.0, 128.0], [-128.0, 128.0]];
        let g = |pts: &[P2]| pts.iter().map(|v| 10.0 + 0.01 * v[0]).collect::<Vec<f64>>();
        prof::store_clear();
        let a = product::build(&p, q, (0, 0), 1, &g, net::Opts::default());
        p.segs.reverse();
        prof::store_clear();
        let b = product::build(&p, q, (0, 0), 1, &g, net::Opts::default());
        assert_eq!(a.bytes, b.bytes);
        assert_eq!(a.piece_keys, b.piece_keys);
        // the tags are in the keys
        let mut p2 = p.clone();
        p2.segs.iter_mut().for_each(|s| if s.fid == "b" { s.xsec = "parking:right=no".into(); });
        prof::store_clear();
        let c = product::build(&p2, q, (0, 0), 1, &g, net::Opts::default());
        assert_ne!(a.piece_keys, c.piece_keys);
    }
}
