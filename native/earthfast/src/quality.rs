//! The per-cell data-quality record ("q", the adaptive world-generation plan's section 2.2, job J2): how complete a z14
//! map cell's data is, so later ladders can tell "mapped" from "probably none" from "unknown" and gates can be split by
//! data quality. One kernel for the CIP builder (tools/build_cip.py stores the record's data part as section "q") and
//! for the server (server/quality.py: the CIP cell's section, or made here from the same layers when the cell has
//! none), so both give the same bytes.
//!
//! Two steps, both pure functions of their inputs (integer sums, no hash-map order, deterministic maths):
//! 1. `counts`: one cell's own sums (cm of road per class and tag, buildings, grid squares, three 64 x 64 bitmaps) from
//!    its layers (roads, connectors, buildings, land_use, infrastructure, water: the cell files' arrays as CIP holds
//!    them) and its lane-tag slice (t:lanes JSON, optional). Not stored anywhere (~9 kB, in memory only).
//! 2. `record`: the data part from the 3 x 3 cells' counts (the centre's own block, the neighbourhood's summed block,
//!    the centre's grid checks that need its neighbours' bitmaps). `meta` adds the source block (DEM, imagery, canopy:
//!    the server's own tables, never stored: a table change needs no new build).
//!
//! Data part (little-endian, DATA_LEN bytes):
//! ```text
//! 0   u8  'Q'   1 u8 VERSION   2 u8 flags (1: all 9 cells counted, 2: the centre's lane tags known, 4: its OSM road
//!                                         tags joined)   3 u8 cells counted (of 9)
//! 4   own block (BLOCK bytes)       4 + BLOCK   the 3 x 3 block (the same fields over the 9 cells' sums)
//! 4 + 2 BLOCK  u32 buildings with no drivable road within ~60 m (grid), u16 built-up squares with no building within
//!              one square, u16 0
//! ```
//! Block: 8 road classes x (u32 metres, 13 x u8 tag share), u32 m of sidewalk footways, u16 junctions, u16 junction
//! controls (signals / stop / give way nodes), u8 share of drivable length drawn coarse (a feature of >= 100 m with <= 2
//! vertices a 100 m), u8 share in straight segments > 200 m (not motorway), u32 buildings, u32 footprint m2, 10 x u8
//! building shares, 6 x u16 grid squares (road, building, built-up land use, forest, farmland, water) of 64 x 64.
//! Shares: 0-200 (0.5 % steps); 255 nothing to share (no length / no building); 254 not in this release's inputs.
//! Source block (META_LEN): see `meta`.
use std::collections::{BTreeMap, BTreeSet};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList, PyTuple};

use crate::dm::Dm;

/// The record's rules and layout: bump with any change of what a field is made from (a stored section of another
/// version is not read: the server makes the record again).
pub const VERSION: u8 = 1;
const MAGIC: u8 = b'Q';
const COUNTS_MAGIC: u8 = b'q';
pub const GRID: usize = 64;
const SQ: usize = GRID * GRID;
const WORDS: usize = SQ / 64;
pub const NCLS: usize = 8;
pub const NTAG: usize = 13;
const NBLD: usize = 10;
const NSQ: usize = 6;
pub const BLOCK: usize = NCLS * (4 + NTAG) + 4 + 4 + 2 + 8 + NBLD + 2 * NSQ;
pub const DATA_LEN: usize = 4 + 2 * BLOCK + 8;
pub const META_LEN: usize = 20;
const R_EARTH: f64 = 6_371_008.8;
const NA: u8 = 254;
const NONE: u8 = 255;
const FAR_M: f64 = 60.0;

pub const CLASSES: [&str; NCLS] = ["motorway", "primary", "secondary", "tertiary", "local", "service", "track",
                                   "path"];
pub const TAGS: [&str; NTAG] = ["lanes", "width", "surface", "maxspeed", "sidewalk", "lit", "smoothness", "oneway",
                                "name", "lane_detail", "parking", "cycleway", "shoulder"];
pub const BLD_SHARES: [&str; NBLD] = ["height", "levels", "roof_shape", "roof_colour", "roof_material",
                                      "facade_material", "facade_colour", "parts", "ml", "other_src"];
pub const SQUARES: [&str; NSQ] = ["road", "building", "builtup", "forest", "farmland", "water"];
const T_LANE_DETAIL: usize = 9;
// (OSM lane-level keys other than "lanes" itself and "junction": the t:lanes shard's columns, railtags.LANE_COLUMNS)
const LANE_PLAIN: [&str; 2] = ["lanes", "junction"];

fn road_class(c: &str) -> usize {
    match c {
        "motorway" | "trunk" => 0,
        "primary" => 1,
        "secondary" => 2,
        "tertiary" => 3,
        "residential" | "unclassified" | "living_street" => 4,
        "service" => 5,
        "pedestrian" | "footway" | "sidewalk" | "crosswalk" | "steps" | "path" | "cycleway" | "bridleway" => 7,
        _ => 6, // track, unknown and anything newer
    }
}

// ------------------------------------------------------------------------------------------------ inputs
/// One array of a layer: numpy dtype string, shape, bytes (C order): cip.arrays_section's items.
pub struct Col {
    pub dtype: String,
    #[allow(dead_code)] // (the columns are read by their item count: kept as cip.arrays_section gives them)
    pub shape: Vec<u64>,
    pub data: Vec<u8>,
}

#[derive(Default)]
pub struct Layer {
    pub cols: BTreeMap<String, Col>,
}

fn kind_size(dt: &str) -> Option<(u8, u8, usize)> {
    let b = dt.as_bytes();
    if b.len() < 3 {
        return None;
    }
    let n: usize = dt[2..].parse().ok()?;
    if b[0] == b'>' && n > 1 {
        return None; // (big-endian: never made by the server)
    }
    Some((b[0], b[1], n))
}

impl Layer {
    fn col(&self, n: &str) -> Option<&Col> {
        self.cols.get(n)
    }

    fn rows(&self) -> usize {
        self.col("kind").map(|c| c.data.len()).or_else(|| self.col("own").map(|c| c.data.len())).unwrap_or(0)
    }

    fn floats(&self, n: &str) -> Option<Vec<f64>> {
        let c = self.col(n)?;
        match kind_size(&c.dtype)? {
            (_, b'f', 8) => Some(c.data.chunks_exact(8).map(|x| f64::from_le_bytes(x.try_into().unwrap())).collect()),
            (_, b'f', 4) => {
                Some(c.data.chunks_exact(4).map(|x| f32::from_le_bytes(x.try_into().unwrap()) as f64).collect())
            }
            _ => None,
        }
    }

    fn ints(&self, n: &str) -> Option<Vec<i64>> {
        let c = self.col(n)?;
        let (_, k, sz) = kind_size(&c.dtype)?;
        if !matches!(k, b'i' | b'u' | b'b') || sz == 0 || sz > 8 {
            return None;
        }
        Some(c.data.chunks_exact(sz).map(|x| {
            let mut w = [0u8; 8];
            w[..sz].copy_from_slice(x);
            let u = u64::from_le_bytes(w);
            if k == b'i' && sz < 8 {
                let sh = 64 - 8 * sz as u32;
                ((u << sh) as i64) >> sh
            } else {
                u as i64
            }
        }).collect())
    }

    fn width(&self, n: &str) -> Option<(&Col, usize)> {
        let c = self.col(n)?;
        match kind_size(&c.dtype)? {
            (_, b'U', w) => Some((c, w)),
            _ => None,
        }
    }

    /// A unicode column's values ('<U' w: w code points a row, trailing NULs dropped; a bad code point: U+FFFD).
    fn strs(&self, n: &str) -> Option<Vec<String>> {
        let (c, w) = self.width(n)?;
        if w == 0 {
            return Some(vec![String::new(); self.rows()]);
        }
        Some(c.data.chunks_exact(4 * w).map(|row| {
            let mut s = String::new();
            for cp in row.chunks_exact(4) {
                let u = u32::from_le_bytes(cp.try_into().unwrap());
                if u == 0 {
                    break;
                }
                s.push(char::from_u32(u).unwrap_or('\u{fffd}'));
            }
            s
        }).collect())
    }

    /// Per row: is the unicode value not empty?
    fn nonempty(&self, n: &str) -> Option<Vec<bool>> {
        let (c, w) = self.width(n)?;
        if w == 0 {
            return Some(vec![false; self.rows()]);
        }
        Some(c.data.chunks_exact(4 * w).map(|row| row[..4] != [0, 0, 0, 0]).collect())
    }

    /// Per feature (n rows): has rule "rule.<name>" a row for it?
    fn rule_rows(&self, name: &str, n: usize) -> Option<Vec<bool>> {
        let f = self.ints(&format!("rule.{name}.feature"))?;
        let mut out = vec![false; n];
        for i in f {
            if i >= 0 && (i as usize) < n {
                out[i as usize] = true;
            }
        }
        Some(out)
    }
}

/// A layer's geometry (mapgeom.Features: kind, part_off, ring_off, coord_off, lonlat).
struct Geom {
    kind: Vec<i64>,
    part_off: Vec<i64>,
    ring_off: Vec<i64>,
    coord_off: Vec<i64>,
    ll: Vec<f64>,
}

impl Geom {
    fn of(l: &Layer) -> Result<Geom, String> {
        let n = l.rows();
        let g = Geom {
            kind: l.ints("kind").unwrap_or_default(),
            part_off: l.ints("part_off").unwrap_or_else(|| vec![0]),
            ring_off: l.ints("ring_off").unwrap_or_else(|| vec![0]),
            coord_off: l.ints("coord_off").unwrap_or_else(|| vec![0]),
            ll: l.floats("lonlat").unwrap_or_default(),
        };
        let ok = g.kind.len() == n && g.part_off.len() == n + 1
            && g.ring_off.len() as i64 == g.part_off.last().copied().unwrap_or(0) + 1
            && g.coord_off.len() as i64 == g.ring_off.last().copied().unwrap_or(0) + 1
            && g.ll.len() as i64 == 2 * g.coord_off.last().copied().unwrap_or(0)
            && g.part_off.windows(2).all(|w| w[0] <= w[1]) && g.ring_off.windows(2).all(|w| w[0] <= w[1])
            && g.coord_off.windows(2).all(|w| w[0] <= w[1]);
        if !ok {
            return Err("geometry offsets do not match".into());
        }
        Ok(g)
    }

    fn parts(&self, f: usize) -> std::ops::Range<usize> {
        self.part_off[f] as usize..self.part_off[f + 1] as usize
    }

    fn rings(&self, p: usize) -> std::ops::Range<usize> {
        self.ring_off[p] as usize..self.ring_off[p + 1] as usize
    }

    fn coords(&self, r: usize) -> &[f64] {
        &self.ll[2 * self.coord_off[r] as usize..2 * self.coord_off[r + 1] as usize]
    }
}

// ------------------------------------------------------------------------------------------------ the cell
/// Cell (z, x, y): its edges (deterministic maths: the same bits everywhere), metres a degree, the 64 x 64 grid (square
/// columns from the west edge, rows from the north edge, linear in latitude inside the cell).
pub struct Cell {
    pub lon0: f64,
    pub lon1: f64,
    pub lat0: f64,
    pub lat1: f64,
    kx: f64,
    ky: f64,
}

fn tile_lat(y: f64, n: f64) -> f64 {
    (std::f64::consts::PI * (1.0 - 2.0 * y / n)).dsinh().datan().to_degrees()
}

impl Cell {
    pub fn new(z: u32, x: u32, y: u32) -> Cell {
        let n = (1u64 << z) as f64;
        let lon0 = x as f64 / n * 360.0 - 180.0;
        let lon1 = (x as f64 + 1.0) / n * 360.0 - 180.0;
        let lat1 = tile_lat(y as f64, n);
        let lat0 = tile_lat(y as f64 + 1.0, n);
        let k = R_EARTH * std::f64::consts::PI / 180.0;
        let c = (0.5 * (lat0 + lat1)).to_radians().dcos();
        Cell { lon0, lon1, lat0, lat1, kx: k * c, ky: k }
    }

    fn gx(&self, lon: f64) -> f64 {
        (lon - self.lon0) / (self.lon1 - self.lon0) * GRID as f64
    }

    fn gy(&self, lat: f64) -> f64 {
        (self.lat1 - lat) / (self.lat1 - self.lat0) * GRID as f64
    }

    fn metres(&self, dlon: f64, dlat: f64) -> f64 {
        let (a, b) = (dlon * self.kx, dlat * self.ky);
        (a * a + b * b).sqrt()
    }

    /// The width of a grid square (m, east-west).
    pub fn square_m(&self) -> f64 {
        (self.lon1 - self.lon0) * self.kx / GRID as f64
    }

    /// The part of segment a-b inside the cell (Liang-Barsky), or None.
    fn clip(&self, a: (f64, f64), b: (f64, f64)) -> Option<((f64, f64), (f64, f64))> {
        let (dx, dy) = (b.0 - a.0, b.1 - a.1);
        let (mut t0, mut t1) = (0.0f64, 1.0f64);
        for (p, q) in [(-dx, a.0 - self.lon0), (dx, self.lon1 - a.0), (-dy, a.1 - self.lat0), (dy, self.lat1 - a.1)] {
            if p == 0.0 {
                if q < 0.0 {
                    return None;
                }
            } else {
                let r = q / p;
                if p < 0.0 {
                    if r > t1 {
                        return None;
                    }
                    if r > t0 {
                        t0 = r;
                    }
                } else {
                    if r < t0 {
                        return None;
                    }
                    if r < t1 {
                        t1 = r;
                    }
                }
            }
        }
        if t1 <= t0 {
            return None;
        }
        Some(((a.0 + t0 * dx, a.1 + t0 * dy), (a.0 + t1 * dx, a.1 + t1 * dy)))
    }
}

#[derive(Clone)]
pub struct Bits(pub [u64; WORDS]);

impl Bits {
    fn new() -> Bits {
        Bits([0; WORDS])
    }
    fn set(&mut self, i: usize, j: usize) {
        let k = j * GRID + i;
        self.0[k / 64] |= 1 << (k % 64);
    }
    pub fn get(&self, i: usize, j: usize) -> bool {
        let k = j * GRID + i;
        self.0[k / 64] >> (k % 64) & 1 == 1
    }
    fn count(&self) -> u32 {
        self.0.iter().map(|w| w.count_ones()).sum()
    }
}

/// Mark the squares segment a-b (grid coordinates) passes through (samples every quarter square).
fn mark_segment(bits: &mut Bits, a: (f64, f64), b: (f64, f64)) {
    let g = GRID as f64;
    if (a.0 < 0.0 && b.0 < 0.0) || (a.0 >= g && b.0 >= g) || (a.1 < 0.0 && b.1 < 0.0) || (a.1 >= g && b.1 >= g) {
        return;
    }
    let len = (b.0 - a.0).abs().max((b.1 - a.1).abs());
    if !len.is_finite() || len > 4.0 * g * 64.0 {
        return;
    }
    let steps = (len * 4.0).ceil() as usize + 1;
    for s in 0..=steps {
        let t = s as f64 / steps as f64;
        let (x, y) = (a.0 + t * (b.0 - a.0), a.1 + t * (b.1 - a.1));
        if (0.0..g).contains(&x) && (0.0..g).contains(&y) {
            bits.set(x as usize, y as usize);
        }
    }
}

/// Mark the squares whose centre lies inside polygon part p (even-odd over its rings) of geometry g.
fn fill_part(bits: &mut Bits, cell: &Cell, g: &Geom, p: usize) {
    let mut ys = (f64::INFINITY, f64::NEG_INFINITY);
    let mut rings: Vec<Vec<(f64, f64)>> = Vec::new();
    for r in g.rings(p) {
        let pts: Vec<(f64, f64)> = g.coords(r).chunks_exact(2).map(|c| (cell.gx(c[0]), cell.gy(c[1]))).collect();
        for q in &pts {
            if q.1 < ys.0 {
                ys.0 = q.1;
            }
            if q.1 > ys.1 {
                ys.1 = q.1;
            }
        }
        rings.push(pts);
    }
    if !(ys.0.is_finite() && ys.1.is_finite()) || ys.1 < 0.0 || ys.0 >= GRID as f64 {
        return;
    }
    let j0 = (ys.0 - 0.5).ceil().max(0.0) as usize;
    let j1 = ((ys.1 - 0.5).floor().min(GRID as f64 - 1.0)) as i64;
    let mut xs: Vec<f64> = Vec::new();
    for j in j0 as i64..=j1 {
        let yc = j as f64 + 0.5;
        xs.clear();
        for pts in &rings {
            let n = pts.len();
            for k in 0..n {
                let (a, b) = (pts[k], pts[(k + 1) % n]);
                if (a.1 <= yc) != (b.1 <= yc) {
                    xs.push(a.0 + (yc - a.1) * (b.0 - a.0) / (b.1 - a.1));
                }
            }
        }
        xs.retain(|v| v.is_finite());
        xs.sort_by(|a, b| a.total_cmp(b));
        for pair in xs.chunks_exact(2) {
            let i0 = (pair[0] - 0.5).ceil().max(0.0);
            let i1 = (pair[1] - 0.5).ceil().min(GRID as f64);
            let mut i = i0;
            while i < i1 {
                bits.set(i as usize, j as usize);
                i += 1.0;
            }
        }
    }
}

// ------------------------------------------------------------------------------------------------ counts
/// One cell's own sums (full precision; the record rounds them).
#[derive(Clone)]
pub struct Counts {
    len_cm: [u64; NCLS],
    tag_cm: [[u64; NTAG]; NCLS],
    base_cm: [[u64; NTAG]; NCLS], // (the class length where the tag is in the inputs)
    sidewalk_cm: u64,
    junctions: u32,
    controls: u32,
    drive_cm: u64,
    coarse_cm: u64,
    straight_cm: u64,
    bld: u32,
    bld_m2: u64,
    bld_k: [u32; NBLD],
    squares: [u32; NSQ],
    lanes_known: bool,
    osm_joined: bool,
    road: Bits,
    bldbits: Bits,
    builtup: Bits,
    bldgrid: Vec<u16>,
}

impl Counts {
    fn empty() -> Counts {
        Counts {
            len_cm: [0; NCLS], tag_cm: [[0; NTAG]; NCLS], base_cm: [[0; NTAG]; NCLS], sidewalk_cm: 0, junctions: 0,
            controls: 0, drive_cm: 0, coarse_cm: 0, straight_cm: 0, bld: 0, bld_m2: 0, bld_k: [0; NBLD],
            squares: [0; NSQ], lanes_known: false, osm_joined: false, road: Bits::new(), bldbits: Bits::new(),
            builtup: Bits::new(), bldgrid: vec![0; SQ],
        }
    }

    fn add(&mut self, o: &Counts) {
        for c in 0..NCLS {
            self.len_cm[c] += o.len_cm[c];
            for t in 0..NTAG {
                self.tag_cm[c][t] += o.tag_cm[c][t];
                self.base_cm[c][t] += o.base_cm[c][t];
            }
        }
        self.sidewalk_cm += o.sidewalk_cm;
        self.junctions += o.junctions;
        self.controls += o.controls;
        self.drive_cm += o.drive_cm;
        self.coarse_cm += o.coarse_cm;
        self.straight_cm += o.straight_cm;
        self.bld += o.bld;
        self.bld_m2 += o.bld_m2;
        for k in 0..NBLD {
            self.bld_k[k] += o.bld_k[k];
        }
        for k in 0..NSQ {
            self.squares[k] += o.squares[k];
        }
    }

    pub fn to_bytes(&self) -> Vec<u8> {
        let mut w = Vec::with_capacity(16 * 1024);
        w.push(COUNTS_MAGIC);
        w.push(VERSION);
        w.push(self.lanes_known as u8 | (self.osm_joined as u8) << 1);
        for c in 0..NCLS {
            w.extend_from_slice(&self.len_cm[c].to_le_bytes());
            for t in 0..NTAG {
                w.extend_from_slice(&self.tag_cm[c][t].to_le_bytes());
                w.extend_from_slice(&self.base_cm[c][t].to_le_bytes());
            }
        }
        for v in [self.sidewalk_cm, self.drive_cm, self.coarse_cm, self.straight_cm, self.bld_m2] {
            w.extend_from_slice(&v.to_le_bytes());
        }
        for v in [self.junctions, self.controls, self.bld] {
            w.extend_from_slice(&v.to_le_bytes());
        }
        for v in self.bld_k.iter().chain(self.squares.iter()) {
            w.extend_from_slice(&v.to_le_bytes());
        }
        for b in [&self.road, &self.bldbits, &self.builtup] {
            for v in b.0 {
                w.extend_from_slice(&v.to_le_bytes());
            }
        }
        for v in &self.bldgrid {
            w.extend_from_slice(&v.to_le_bytes());
        }
        w
    }

    pub fn from_bytes(b: &[u8]) -> Result<Counts, String> {
        let want = 3 + NCLS * 8 * (1 + 2 * NTAG) + 5 * 8 + 3 * 4 + (NBLD + NSQ) * 4 + 3 * WORDS * 8 + SQ * 2;
        if b.len() != want || b[0] != COUNTS_MAGIC || b[1] != VERSION {
            return Err("not a quality counts blob of this version".into());
        }
        let mut at = 3usize;
        let mut u64n = || {
            let v = u64::from_le_bytes(b[at..at + 8].try_into().unwrap());
            at += 8;
            v
        };
        let mut c = Counts::empty();
        for k in 0..NCLS {
            c.len_cm[k] = u64n();
            for t in 0..NTAG {
                c.tag_cm[k][t] = u64n();
                c.base_cm[k][t] = u64n();
            }
        }
        c.sidewalk_cm = u64n();
        c.drive_cm = u64n();
        c.coarse_cm = u64n();
        c.straight_cm = u64n();
        c.bld_m2 = u64n();
        let mut at = 3 + NCLS * 8 * (1 + 2 * NTAG) + 5 * 8;
        let mut u32n = || {
            let v = u32::from_le_bytes(b[at..at + 4].try_into().unwrap());
            at += 4;
            v
        };
        c.junctions = u32n();
        c.controls = u32n();
        c.bld = u32n();
        for k in 0..NBLD {
            c.bld_k[k] = u32n();
        }
        for k in 0..NSQ {
            c.squares[k] = u32n();
        }
        let mut at = 3 + NCLS * 8 * (1 + 2 * NTAG) + 5 * 8 + 3 * 4 + (NBLD + NSQ) * 4;
        for bits in [&mut c.road, &mut c.bldbits, &mut c.builtup] {
            for v in bits.0.iter_mut() {
                *v = u64::from_le_bytes(b[at..at + 8].try_into().unwrap());
                at += 8;
            }
        }
        for v in c.bldgrid.iter_mut() {
            *v = u16::from_le_bytes(b[at..at + 2].try_into().unwrap());
            at += 2;
        }
        c.lanes_known = b[2] & 1 != 0;
        c.osm_joined = b[2] & 2 != 0;
        Ok(c)
    }
}

fn cm(m: f64) -> u64 {
    if m.is_finite() && m > 0.0 {
        (m * 100.0).round() as u64
    } else {
        0
    }
}

/// An OSM way id from an Overture source record ("w123@4" -> 123).
fn way_of(rec: &str) -> Option<u64> {
    let d = rec.strip_prefix('w')?;
    let end = d.find(|c: char| !c.is_ascii_digit()).unwrap_or(d.len());
    if end == 0 {
        return None;
    }
    d[..end].parse().ok()
}

/// The ways of a t:lanes slice (railtags.cell_slice's JSON: {"ways": [[id, {tags}], ...], ...}) with a lane-level key
/// beyond "lanes" / "junction".
fn lane_detail_ways(json: &[u8]) -> Result<BTreeSet<u64>, String> {
    let v: serde_json::Value = serde_json::from_slice(json).map_err(|e| format!("t:lanes: {e}"))?;
    let mut out = BTreeSet::new();
    if let Some(ways) = v.get("ways").and_then(|w| w.as_array()) {
        for w in ways {
            let (Some(id), Some(tags)) = (w.get(0), w.get(1).and_then(|t| t.as_object())) else { continue };
            let id = id.as_u64().or_else(|| id.as_str().and_then(|s| s.parse().ok()));
            let Some(id) = id else { continue };
            if tags.iter().any(|(k, val)| !LANE_PLAIN.contains(&k.as_str()) && !val.is_null()) {
                out.insert(id);
            }
        }
    }
    Ok(out)
}

fn polygon_area_m2(cell: &Cell, g: &Geom, f: usize) -> f64 {
    let mut total = 0.0;
    for p in g.parts(f) {
        let mut part = 0.0;
        for (k, r) in g.rings(p).enumerate() {
            let c = g.coords(r);
            let n = c.len() / 2;
            let mut s = 0.0;
            for i in 0..n {
                let j = (i + 1) % n;
                let (x0, y0) = ((c[2 * i] - cell.lon0) * cell.kx, (c[2 * i + 1] - cell.lat0) * cell.ky);
                let (x1, y1) = ((c[2 * j] - cell.lon0) * cell.kx, (c[2 * j + 1] - cell.lat0) * cell.ky);
                s += x0 * y1 - x1 * y0;
            }
            let a = 0.5 * s.abs();
            if k == 0 {
                part += a;
            } else {
                part -= a;
            }
        }
        if part > 0.0 {
            total += part;
        }
    }
    total
}

/// One cell's counts from its layers (name -> Layer: roads, connectors, buildings, land_use, infrastructure, water;
/// a missing one counts as empty) and its t:lanes slice (None: lane tags unknown).
pub fn counts(z: u32, x: u32, y: u32, layers: &BTreeMap<String, Layer>, lanes: Option<&[u8]>) -> Result<Counts, String> {
    let cell = Cell::new(z, x, y);
    let empty = Layer::default();
    let lay = |n: &str| layers.get(n).unwrap_or(&empty);
    let mut c = Counts::empty();
    let lane_ways = match lanes {
        Some(j) => Some(lane_detail_ways(j)?),
        None => None,
    };
    c.lanes_known = lane_ways.is_some();

    // --- roads
    let roads = lay("roads");
    let n = roads.rows();
    let rg = Geom::of(roads).map_err(|e| format!("roads: {e}"))?;
    let subtype = roads.strs("attr.subtype").unwrap_or_else(|| vec![String::new(); n]);
    let class = roads.strs("attr.class").unwrap_or_else(|| vec![String::new(); n]);
    let subclass = roads.strs("attr.subclass").unwrap_or_else(|| vec![String::new(); n]);
    let srcrec = roads.strs("attr.src_record").unwrap_or_else(|| vec![String::new(); n]);
    let fin = |name: &str| roads.floats(name).map(|v| v.iter().map(|x| x.is_finite()).collect::<Vec<bool>>());
    let osm_lanes = fin("attr.osm_lanes");
    let osm_width = fin("attr.osm_width");
    let osm_speed = fin("attr.osm_maxspeed");
    let osm_surface = roads.nonempty("attr.osm_surface");
    let osm_sidewalk = roads.nonempty("attr.osm_sidewalk");
    let osm_lit = roads.nonempty("attr.osm_lit");
    let osm_smooth = roads.nonempty("attr.osm_smoothness");
    let osm_oneway = roads.nonempty("attr.osm_oneway");
    let name = roads.nonempty("attr.name");
    c.osm_joined = osm_lanes.is_some();
    let r_width = roads.rule_rows("width", n);
    let r_surface = roads.rule_rows("surface", n);
    let r_speed = roads.rule_rows("max_speed_kmh", n);
    let r_oneway = roads.rule_rows("oneway", n);
    let either = |a: &Option<Vec<bool>>, b: &Option<Vec<bool>>, f: usize| -> Option<bool> {
        match (a, b) {
            (None, None) => None,
            _ => Some(a.as_ref().is_some_and(|v| v[f]) || b.as_ref().is_some_and(|v| v[f])),
        }
    };
    let mut drivable = vec![false; n];
    for f in 0..n {
        if subtype[f] != "road" || rg.kind[f] != 2 {
            continue;
        }
        let cls = road_class(&class[f]);
        drivable[f] = cls < 7;
        let (mut inside, mut full, mut nv, mut straight) = (0.0f64, 0.0f64, 0usize, 0.0f64);
        for p in rg.parts(f) {
            for r in rg.rings(p) {
                let co = rg.coords(r);
                nv += co.len() / 2;
                for s in 1..co.len() / 2 {
                    let a = (co[2 * s - 2], co[2 * s - 1]);
                    let b = (co[2 * s], co[2 * s + 1]);
                    let seg = cell.metres(b.0 - a.0, b.1 - a.1);
                    full += seg;
                    if let Some((p0, p1)) = cell.clip(a, b) {
                        let l = cell.metres(p1.0 - p0.0, p1.1 - p0.1);
                        inside += l;
                        if seg > 200.0 && (1..7).contains(&cls) {
                            straight += l;
                        }
                    }
                    if cls < 7 {
                        mark_segment(&mut c.road, (cell.gx(a.0), cell.gy(a.1)), (cell.gx(b.0), cell.gy(b.1)));
                    }
                }
            }
        }
        let l = cm(inside);
        if l == 0 {
            continue;
        }
        c.len_cm[cls] += l;
        if class[f] == "footway" && subclass[f] == "sidewalk" {
            c.sidewalk_cm += l;
        }
        if cls < 7 {
            c.drive_cm += l;
            if full >= 100.0 && (nv as f64) * 100.0 <= 2.0 * full {
                c.coarse_cm += l;
            }
            c.straight_cm += cm(straight);
        }
        let way = way_of(&srcrec[f]);
        let tags: [Option<bool>; NTAG] = [
            osm_lanes.as_ref().map(|v| v[f]),
            either(&osm_width, &r_width, f),
            either(&osm_surface, &r_surface, f),
            either(&osm_speed, &r_speed, f),
            osm_sidewalk.as_ref().map(|v| v[f]),
            osm_lit.as_ref().map(|v| v[f]),
            osm_smooth.as_ref().map(|v| v[f]),
            either(&osm_oneway, &r_oneway, f),
            name.as_ref().map(|v| v[f]),
            lane_ways.as_ref().map(|s| way.is_some_and(|w| s.contains(&w))),
            None,
            None,
            None,
        ];
        for (t, v) in tags.iter().enumerate() {
            if let Some(has) = v {
                c.base_cm[cls][t] += l;
                if *has {
                    c.tag_cm[cls][t] += l;
                }
            }
        }
    }
    debug_assert_eq!(TAGS[T_LANE_DETAIL], "lane_detail");

    // --- junctions: the cell's own connectors that 3 or more drivable road ends / passes meet
    let conn = lay("connectors");
    if let (Some(ids), Some(own)) = (conn.strs("ids"), conn.ints("own")) {
        if let (Some(rf), Some(rv), Some(rs)) =
            (roads.ints("rule.connectors.feature"), roads.strs("rule.connectors.value"), roads.floats("rule.connectors.start"))
        {
            let mut arms: BTreeMap<&str, u32> = BTreeMap::new();
            for k in 0..rf.len().min(rv.len()).min(rs.len()) {
                let f = rf[k];
                if f < 0 || f as usize >= n || !drivable[f as usize] {
                    continue;
                }
                let end = rs[k] <= 1e-6 || rs[k] >= 1.0 - 1e-6;
                *arms.entry(rv[k].as_str()).or_insert(0) += if end { 1 } else { 2 };
            }
            for (i, id) in ids.iter().enumerate() {
                if own.get(i).copied().unwrap_or(0) != 0 && arms.get(id.as_str()).copied().unwrap_or(0) >= 3 {
                    c.junctions += 1;
                }
            }
        }
    }
    let infra = lay("infrastructure");
    if let (Some(cls), Some(own)) = (infra.strs("attr.class"), infra.ints("own")) {
        for (i, v) in cls.iter().enumerate() {
            if own.get(i).copied().unwrap_or(0) != 0 && matches!(v.as_str(), "traffic_signals" | "stop" | "give_way") {
                c.controls += 1;
            }
        }
    }

    // --- buildings (own: their bbox centre in this cell)
    let b = lay("buildings");
    let nb = b.rows();
    if nb > 0 {
        let bg = Geom::of(b).map_err(|e| format!("buildings: {e}"))?;
        let own = b.ints("own").unwrap_or_default();
        let cx = b.floats("attr.cx").unwrap_or_default();
        let cy = b.floats("attr.cy").unwrap_or_default();
        let fl = |n: &str| b.floats(n).map(|v| v.iter().map(|x| x.is_finite()).collect::<Vec<bool>>());
        let has: [Option<Vec<bool>>; 8] = [
            fl("attr.height"), fl("attr.num_floors"), b.nonempty("attr.roof_shape"), b.nonempty("attr.roof_color"),
            b.nonempty("attr.roof_material"), b.nonempty("attr.facade_material"), b.nonempty("attr.facade_color"),
            b.ints("attr.has_parts").map(|v| v.iter().map(|&x| x != 0).collect()),
        ];
        let src = b.strs("attr.src_dataset").unwrap_or_else(|| vec![String::new(); nb]);
        for f in 0..nb {
            if own.get(f).copied().unwrap_or(0) == 0 {
                continue;
            }
            c.bld += 1;
            c.bld_m2 += polygon_area_m2(&cell, &bg, f).round() as u64;
            for (k, h) in has.iter().enumerate() {
                if h.as_ref().is_some_and(|v| v[f]) {
                    c.bld_k[k] += 1;
                }
            }
            let s = src[f].as_str();
            if s == "OpenStreetMap" {
            } else if s.contains("ML") || s.contains("Open Buildings") || s.contains("Microsoft") || s.contains("Google") {
                c.bld_k[8] += 1;
            } else {
                c.bld_k[9] += 1;
            }
            if let (Some(&px), Some(&py)) = (cx.get(f), cy.get(f)) {
                let (gx, gy) = (cell.gx(px), cell.gy(py));
                if gx.is_finite() && gy.is_finite() {
                    let i = gx.max(0.0).min(GRID as f64 - 1.0) as usize;
                    let j = gy.max(0.0).min(GRID as f64 - 1.0) as usize;
                    c.bldbits.set(i, j);
                    let k = j * GRID + i;
                    c.bldgrid[k] = c.bldgrid[k].saturating_add(1);
                }
            }
        }
    }

    // --- areas on the grid
    let lu = lay("land_use");
    let (mut forest, mut farm, mut water) = (Bits::new(), Bits::new(), Bits::new());
    if lu.rows() > 0 {
        let g = Geom::of(lu).map_err(|e| format!("land_use: {e}"))?;
        let sub = lu.strs("attr.subtype").unwrap_or_else(|| vec![String::new(); lu.rows()]);
        let cls = lu.strs("attr.class").unwrap_or_else(|| vec![String::new(); lu.rows()]);
        for f in 0..lu.rows() {
            if g.kind[f] != 3 {
                continue;
            }
            let target = if matches!(sub[f].as_str(), "residential" | "developed" | "education" | "medical"
                | "religious" | "military") {
                &mut c.builtup
            } else if matches!(cls[f].as_str(), "forest" | "wood") {
                &mut forest
            } else if sub[f] == "agriculture" {
                &mut farm
            } else {
                continue;
            };
            for p in g.parts(f) {
                fill_part(target, &cell, &g, p);
            }
        }
    }
    let wl = lay("water");
    if wl.rows() > 0 {
        let g = Geom::of(wl).map_err(|e| format!("water: {e}"))?;
        for f in 0..wl.rows() {
            if g.kind[f] == 3 {
                for p in g.parts(f) {
                    fill_part(&mut water, &cell, &g, p);
                }
            }
        }
    }
    c.squares = [c.road.count(), c.bldbits.count(), c.builtup.count(), forest.count(), farm.count(), water.count()];
    Ok(c)
}

// ------------------------------------------------------------------------------------------------ the record
fn share(part: u64, whole: u64) -> u8 {
    if whole == 0 {
        return NONE;
    }
    (((part.min(whole) as u128) * 200 + whole as u128 / 2) / whole as u128) as u8
}

fn sat16(v: u64) -> u16 {
    v.min(u16::MAX as u64) as u16
}

fn sat32(v: u64) -> u32 {
    v.min(u32::MAX as u64) as u32
}

fn block(c: &Counts, out: &mut Vec<u8>) {
    let start = out.len();
    for k in 0..NCLS {
        out.extend_from_slice(&sat32((c.len_cm[k] + 50) / 100).to_le_bytes());
        for t in 0..NTAG {
            out.push(if c.len_cm[k] == 0 {
                NONE
            } else if c.base_cm[k][t] == 0 {
                NA
            } else {
                share(c.tag_cm[k][t], c.base_cm[k][t])
            });
        }
    }
    out.extend_from_slice(&sat32((c.sidewalk_cm + 50) / 100).to_le_bytes());
    out.extend_from_slice(&sat16(c.junctions as u64).to_le_bytes());
    out.extend_from_slice(&sat16(c.controls as u64).to_le_bytes());
    out.push(share(c.coarse_cm, c.drive_cm));
    out.push(share(c.straight_cm, c.drive_cm));
    out.extend_from_slice(&c.bld.to_le_bytes());
    out.extend_from_slice(&sat32(c.bld_m2).to_le_bytes());
    for k in 0..NBLD {
        out.push(share(c.bld_k[k] as u64, c.bld as u64));
    }
    for k in 0..NSQ {
        out.extend_from_slice(&sat16(c.squares[k] as u64).to_le_bytes());
    }
    debug_assert_eq!(out.len() - start, BLOCK);
}

/// The far-building radius in squares (~FAR_M) at cell (z, x, y).
pub fn far_radius(z: u32, x: u32, y: u32) -> usize {
    (FAR_M / Cell::new(z, x, y).square_m()).round().max(1.0) as usize
}

/// The data part from the 3 x 3 cells' counts (row-major from the north-west: index 4 the centre; None: not counted).
pub fn record(z: u32, x: u32, y: u32, nine: &[Option<Counts>]) -> Result<Vec<u8>, String> {
    if nine.len() != 9 {
        return Err("9 cells wanted".into());
    }
    let centre = nine[4].as_ref().ok_or("the centre's counts are missing")?;
    let mut sum = Counts::empty();
    let mut n = 0u8;
    for c in nine.iter().flatten() {
        sum.add(c);
        n += 1;
    }
    let mut out = Vec::with_capacity(DATA_LEN + META_LEN);
    out.push(MAGIC);
    out.push(VERSION);
    out.push((n == 9) as u8 | (centre.lanes_known as u8) << 1 | (centre.osm_joined as u8) << 2);
    out.push(n);
    block(centre, &mut out);
    block(&sum, &mut out);
    // the centre's grid checks over the 3 x 3 bitmaps: global square (gi, gj) = (64 + i, 64 + j) for the centre
    let rad = far_radius(z, x, y) as i64;
    let g = GRID as i64;
    let at = |bits: fn(&Counts) -> &Bits, gi: i64, gj: i64| -> bool {
        if !(0..3 * g).contains(&gi) || !(0..3 * g).contains(&gj) {
            return false;
        }
        let k = (gj / g * 3 + gi / g) as usize;
        nine[k].as_ref().is_some_and(|c| bits(c).get((gi % g) as usize, (gj % g) as usize))
    };
    let near = |bits: fn(&Counts) -> &Bits, gi: i64, gj: i64, r: i64| -> bool {
        for dj in -r..=r {
            for di in -r..=r {
                if at(bits, gi + di, gj + dj) {
                    return true;
                }
            }
        }
        false
    };
    let (mut far, mut nobld) = (0u64, 0u64);
    for j in 0..g {
        for i in 0..g {
            let (gi, gj) = (g + i, g + j);
            let nb = centre.bldgrid[(j * g + i) as usize] as u64;
            if nb > 0 && !near(|c| &c.road, gi, gj, rad) {
                far += nb;
            }
            if centre.builtup.get(i as usize, j as usize) && !near(|c| &c.bldbits, gi, gj, 1) {
                nobld += 1;
            }
        }
    }
    out.extend_from_slice(&sat32(far).to_le_bytes());
    out.extend_from_slice(&sat16(nobld).to_le_bytes());
    out.extend_from_slice(&0u16.to_le_bytes());
    debug_assert_eq!(out.len(), DATA_LEN);
    Ok(out)
}

// ------------------------------------------------------------------------------------------------ the source block
/// FNV-1a (32 bits) of a source name: the record's imagery source id (stable, no table).
pub fn name_id(s: &str) -> u32 {
    let mut h: u32 = 0x811c_9dc5;
    for b in s.bytes() {
        h ^= b as u32;
        h = h.wrapping_mul(0x0100_0193);
    }
    h
}

/// The latest year 1990-2099 written in s as a 4-digit number of its own (a service's "Ortho_2023", "2023_2024_2025"),
/// or 0.
fn year_in(s: &str) -> u16 {
    let b = s.as_bytes();
    let mut best = 0u16;
    let mut i = 0;
    while i < b.len() {
        if b[i].is_ascii_digit() {
            let j = (i..b.len()).find(|&k| !b[k].is_ascii_digit()).unwrap_or(b.len());
            if j - i == 4 {
                let v: u16 = s[i..j].parse().unwrap_or(0);
                if (1990..2100).contains(&v) && v > best {
                    best = v;
                }
            }
            i = j;
        } else {
            i += 1;
        }
    }
    best
}

fn res_dm(lat: f64, zoom: u32, px: f64) -> u16 {
    let m = 40_075_016.686 * lat.to_radians().dcos() / (px * (1u64 << zoom) as f64);
    (m * 10.0).round().clamp(0.0, 65535.0) as u16
}

pub struct Imagery {
    pub name: String,
    pub url: String,
    pub max_zoom: u32,
    pub leaf_off: bool,
}

/// The source block (META_LEN bytes): dem_src 0 unknown / 1 Mapterhorn z12 (Copernicus GLO-30, ground-filtered) /
/// 2 Mapterhorn lidar deeper than z12 / 3 Hong Kong DTM / 4 polar Copernicus; dem_zoom the deepest Mapterhorn zoom (0
/// unknown); dem_bits (1 a surface model, 2 HRDEM bias corrections, 4 HRDEM near terrain, 8 Mapterhorn's list unknown);
/// roadz_bits (1 US 3DEP lidar, 2 Hong Kong, 4 IGN, 8 NVDB); imagery: the finest covering source (by its finest zoom,
/// then chain order), its name id, zoom, ground resolution, year (from its name or service, 0 unknown), flags (1 flown
/// leaf-off, 2 generated ground the better choice: nothing at 1 m or finer, or older than 2015), candidates; canopy model.
pub fn meta(z: u32, x: u32, y: u32, dem_src: u8, dem_zoom: u32, dem_bits: u8, roadz_bits: u8, img: &[Imagery],
            canopy: u8) -> Vec<u8> {
    let cell = Cell::new(z, x, y);
    let lat = 0.5 * (cell.lat0 + cell.lat1);
    let dem_res: u16 = match dem_src {
        1 | 4 => 300,
        2 => res_dm(lat, dem_zoom, 512.0),
        3 => 5,
        _ => 0,
    };
    let mut out = Vec::with_capacity(META_LEN);
    out.push(dem_src);
    out.push(dem_zoom.min(255) as u8);
    out.extend_from_slice(&dem_res.to_le_bytes());
    out.push(dem_bits);
    out.push(roadz_bits);
    let mut best: Option<&Imagery> = None;
    for s in img {
        if best.is_none_or(|b| s.max_zoom > b.max_zoom) {
            best = Some(s);
        }
    }
    match best {
        Some(s) => {
            let year = year_in(&s.name).max(year_in(&s.url));
            let gg = s.max_zoom < 17 || (year != 0 && year < 2015);
            out.extend_from_slice(&name_id(&s.name).to_le_bytes());
            out.push(s.max_zoom.min(255) as u8);
            out.push(s.leaf_off as u8 | (gg as u8) << 1);
            out.extend_from_slice(&res_dm(lat, s.max_zoom, 256.0).to_le_bytes());
            out.extend_from_slice(&year.to_le_bytes());
        }
        None => {
            out.extend_from_slice(&0u32.to_le_bytes());
            out.push(0);
            out.push(2);
            out.extend_from_slice(&0u16.to_le_bytes());
            out.extend_from_slice(&0u16.to_le_bytes());
        }
    }
    out.push(img.len().min(255) as u8);
    out.push(canopy);
    out.extend_from_slice(&0u16.to_le_bytes());
    debug_assert_eq!(out.len(), META_LEN);
    out
}

// ------------------------------------------------------------------------------------------------ reading a record
/// [(field, value)] of a record (data part, or data part + source block): the one place that knows the layout (maps,
/// tests and later consumers read fields by name). Shares as stored (0-200, 254, 255).
pub fn fields(r: &[u8]) -> Result<Vec<(String, i64)>, String> {
    if r.len() < DATA_LEN || r[0] != MAGIC {
        return Err("not a quality record".into());
    }
    if r[1] != VERSION {
        return Err(format!("quality record version {} (this kernel reads {VERSION})", r[1]));
    }
    let mut out: Vec<(String, i64)> = vec![("version".into(), r[1] as i64), ("flags".into(), r[2] as i64),
                                           ("cells".into(), r[3] as i64)];
    let u16a = |a: usize| u16::from_le_bytes([r[a], r[a + 1]]) as i64;
    let u32a = |a: usize| u32::from_le_bytes(r[a..a + 4].try_into().unwrap()) as i64;
    for (pre, base) in [("own", 4usize), ("nb", 4 + BLOCK)] {
        let mut a = base;
        for cls in CLASSES {
            out.push((format!("{pre}.{cls}.m"), u32a(a)));
            a += 4;
            for t in TAGS {
                out.push((format!("{pre}.{cls}.{t}"), r[a] as i64));
                a += 1;
            }
        }
        out.push((format!("{pre}.sidewalk_footway_m"), u32a(a)));
        out.push((format!("{pre}.junctions"), u16a(a + 4)));
        out.push((format!("{pre}.controls"), u16a(a + 6)));
        out.push((format!("{pre}.coarse"), r[a + 8] as i64));
        out.push((format!("{pre}.straight"), r[a + 9] as i64));
        out.push((format!("{pre}.buildings"), u32a(a + 10)));
        out.push((format!("{pre}.footprint_m2"), u32a(a + 14)));
        a += 18;
        for k in BLD_SHARES {
            out.push((format!("{pre}.bld.{k}"), r[a] as i64));
            a += 1;
        }
        for k in SQUARES {
            out.push((format!("{pre}.sq.{k}"), u16a(a)));
            a += 2;
        }
        debug_assert_eq!(a, base + BLOCK);
    }
    let a = 4 + 2 * BLOCK;
    out.push(("own.far_buildings".into(), u32a(a)));
    out.push(("own.builtup_nobld_sq".into(), u16a(a + 4)));
    if r.len() >= DATA_LEN + META_LEN {
        let m = DATA_LEN;
        out.push(("dem.src".into(), r[m] as i64));
        out.push(("dem.zoom".into(), r[m + 1] as i64));
        out.push(("dem.res_dm".into(), u16a(m + 2)));
        out.push(("dem.bits".into(), r[m + 4] as i64));
        out.push(("roadz.bits".into(), r[m + 5] as i64));
        out.push(("img.id".into(), u32a(m + 6)));
        out.push(("img.zoom".into(), r[m + 10] as i64));
        out.push(("img.flags".into(), r[m + 11] as i64));
        out.push(("img.res_dm".into(), u16a(m + 12)));
        out.push(("img.year".into(), u16a(m + 14)));
        out.push(("img.candidates".into(), r[m + 16] as i64));
        out.push(("canopy".into(), r[m + 17] as i64));
    }
    Ok(out)
}

// ------------------------------------------------------------------------------------------------ Python
fn layer_from_py(o: &Bound<'_, PyAny>) -> PyResult<Layer> {
    let mut l = Layer::default();
    for item in o.cast::<PyList>()?.iter() {
        let t = item.cast::<PyTuple>()?;
        let name: String = t.get_item(0)?.extract()?;
        l.cols.insert(name, Col {
            dtype: t.get_item(1)?.extract()?,
            shape: t.get_item(2)?.extract()?,
            data: t.get_item(3)?.cast::<PyBytes>()?.as_bytes().to_vec(),
        });
    }
    Ok(l)
}

/// One cell's counts blob (bytes, this kernel's own: only `quality_record` reads it) from its layers {name: [(array
/// name, dtype str, shape, bytes)]} (cip.arrays_section's lists; roads, connectors, buildings, land_use,
/// infrastructure, water: a missing one is empty) and its t:lanes slice JSON (None: unknown).
#[pyfunction]
#[pyo3(signature = (z, x, y, layers, lanes=None))]
pub fn quality_counts<'py>(py: Python<'py>, z: u32, x: u32, y: u32, layers: &Bound<'py, PyDict>,
                           lanes: Option<&[u8]>) -> PyResult<Bound<'py, PyBytes>> {
    let mut ls = BTreeMap::new();
    for (k, v) in layers.iter() {
        ls.insert(k.extract::<String>()?, layer_from_py(&v)?);
    }
    let lanes = lanes.map(|b| b.to_vec());
    let got = py.detach(move || counts(z, x, y, &ls, lanes.as_deref()).map(|c| c.to_bytes()))
        .map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &got))
}

/// The record's data part (DATA_LEN bytes) of cell (z, x, y) from the 3 x 3 cells' counts blobs (row-major from the
/// north-west, the centre 5th; None where a cell was not counted).
#[pyfunction]
pub fn quality_record<'py>(py: Python<'py>, z: u32, x: u32, y: u32, nine: Vec<Option<Vec<u8>>>)
                           -> PyResult<Bound<'py, PyBytes>> {
    let cs: Vec<Option<Counts>> = nine.iter().map(|b| b.as_deref().map(Counts::from_bytes).transpose())
        .collect::<Result<_, _>>().map_err(PyValueError::new_err)?;
    let got = py.detach(move || record(z, x, y, &cs)).map_err(PyValueError::new_err)?;
    Ok(PyBytes::new(py, &got))
}

/// The source block (META_LEN bytes); imagery: the covering sources [(name, url, finest zoom there, leaf_off)].
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn quality_meta<'py>(py: Python<'py>, z: u32, x: u32, y: u32, dem_src: u8, dem_zoom: u32, dem_bits: u8,
                         roadz_bits: u8, imagery: Vec<(String, String, u32, bool)>, canopy: u8)
                         -> PyResult<Bound<'py, PyBytes>> {
    let img: Vec<Imagery> = imagery.into_iter()
        .map(|(name, url, max_zoom, leaf_off)| Imagery { name, url, max_zoom, leaf_off }).collect();
    Ok(PyBytes::new(py, &meta(z, x, y, dem_src, dem_zoom, dem_bits, roadz_bits, &img, canopy)))
}

/// [(field, value)] of a record (see `fields`).
#[pyfunction]
pub fn quality_fields(record: &[u8]) -> PyResult<Vec<(String, i64)>> {
    fields(record).map_err(PyValueError::new_err)
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(quality_counts, m)?)?;
    m.add_function(wrap_pyfunction!(quality_record, m)?)?;
    m.add_function(wrap_pyfunction!(quality_meta, m)?)?;
    m.add_function(wrap_pyfunction!(quality_fields, m)?)?;
    m.add("QUALITY_VERSION", VERSION)?;
    m.add("QUALITY_DATA_LEN", DATA_LEN)?;
    m.add("QUALITY_META_LEN", META_LEN)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn col_f64(v: &[f64], shape: Vec<u64>) -> Col {
        Col { dtype: "<f8".into(), shape, data: v.iter().flat_map(|x| x.to_le_bytes()).collect() }
    }
    fn col_i64(v: &[i64]) -> Col {
        Col { dtype: "<i8".into(), shape: vec![v.len() as u64], data: v.iter().flat_map(|x| x.to_le_bytes()).collect() }
    }
    fn col_u8(v: &[u8], dt: &str) -> Col {
        Col { dtype: dt.into(), shape: vec![v.len() as u64], data: v.to_vec() }
    }
    fn col_str(v: &[&str], w: usize) -> Col {
        let mut data = Vec::new();
        for s in v {
            let cps: Vec<u32> = s.chars().map(|c| c as u32).collect();
            for k in 0..w {
                data.extend_from_slice(&cps.get(k).copied().unwrap_or(0).to_le_bytes());
            }
        }
        Col { dtype: format!("<U{w}"), shape: vec![v.len() as u64], data }
    }

    /// One line road across cell (14, x, y) at its middle latitude.
    fn one_road(cell: &Cell, class: &str, lanes: f64) -> Layer {
        let lat = 0.5 * (cell.lat0 + cell.lat1);
        let mut l = Layer::default();
        l.cols.insert("kind".into(), col_u8(&[2], "|u1"));
        l.cols.insert("own".into(), col_u8(&[1], "|b1"));
        l.cols.insert("part_off".into(), col_i64(&[0, 1]));
        l.cols.insert("ring_off".into(), col_i64(&[0, 1]));
        l.cols.insert("coord_off".into(), col_i64(&[0, 2]));
        l.cols.insert("lonlat".into(), col_f64(&[cell.lon0 - 0.01, lat, cell.lon1 + 0.01, lat], vec![2, 2]));
        l.cols.insert("attr.subtype".into(), col_str(&["road"], 4));
        l.cols.insert("attr.class".into(), col_str(&[class], 12));
        l.cols.insert("attr.subclass".into(), col_str(&[""], 1));
        l.cols.insert("attr.src_record".into(), col_str(&["w42@1"], 8));
        l.cols.insert("attr.osm_lanes".into(), col_f64(&[lanes], vec![1]));
        l
    }

    #[test]
    fn a_road_across_the_cell() {
        let (z, x, y) = (14, 4705, 6244);
        let cell = Cell::new(z, x, y);
        let mut ls = BTreeMap::new();
        ls.insert("roads".to_string(), one_road(&cell, "primary", 2.0));
        let lanes = br#"{"kind":"Ways","ways":[[42,{"turn:lanes":"left|through"}]],"nodes":[]}"#;
        let c = counts(z, x, y, &ls, Some(lanes)).unwrap();
        let width_m = (cell.lon1 - cell.lon0) * cell.kx;
        assert!((c.len_cm[1] as f64 / 100.0 - width_m).abs() < 0.02);
        assert_eq!(c.tag_cm[1][0], c.len_cm[1]);            // lanes on all of it
        assert_eq!(c.tag_cm[1][T_LANE_DETAIL], c.len_cm[1]); // turn:lanes
        assert_eq!(c.base_cm[1][1], 0);                      // width: not in the inputs
        assert_eq!(c.road.count(), 64);
        let blob = c.to_bytes();
        let back = Counts::from_bytes(&blob).unwrap();
        assert_eq!(back.to_bytes(), blob);
        let nine: Vec<Option<Counts>> = (0..9).map(|k| if k == 4 { Some(back.clone()) } else { None }).collect();
        let r = record(z, x, y, &nine).unwrap();
        assert_eq!(r.len(), DATA_LEN);
        let f: BTreeMap<String, i64> = fields(&r).unwrap().into_iter().collect();
        assert_eq!(f["own.primary.lanes"], 200);
        assert_eq!(f["own.primary.width"], NA as i64);
        assert_eq!(f["own.local.lanes"], NONE as i64);
        assert_eq!(f["cells"], 1);
        assert_eq!(f["flags"] & 1, 0);
    }

    #[test]
    fn meta_and_years() {
        assert_eq!(year_in("SixInchImagery2023_2024_2025"), 2025);
        assert_eq!(year_in("EPSG:3857/{z}"), 0);
        assert_eq!(year_in("v1_0/Actueel"), 0);
        let m = meta(14, 4705, 6244, 2, 16, 0, 1, &[
            Imagery { name: "eox2023".into(), url: String::new(), max_zoom: 14, leaf_off: false },
            Imagery { name: "md6in".into(), url: "SixInchImagery2023_2024_2025".into(), max_zoom: 17, leaf_off: true },
        ], 1);
        assert_eq!(m.len(), META_LEN);
        assert_eq!(m[10], 17);
        assert_eq!(m[11], 1);
        assert_eq!(u16::from_le_bytes([m[14], m[15]]), 2025);
    }
}
