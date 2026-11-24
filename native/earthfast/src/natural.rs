//! OpenStreetMap's mapped natural features in the generator (server/natural.py; the user, 2026-11-22: "feel free to use
//! as many [OSM/Overture data points] as possible if they would improve the generator"). Each use is behind its own
//! switch (BNE_NATURAL_TREES / _LEAF / _GROUND / _CLIFFS / _BATHY, off by default); these are their kernels:
//! - natural_tree_info: a mapped tree's (or tree row's or forest's) tags -> its stock species group, height, crown
//!   (genus / species / taxon, leaf_type / leaf_cycle, height, circumference, diameter_crown, with their units);
//! - natural_trees: the mapped single trees and tree rows placed where they stand (never on a building, water, a
//!   deck; a trunk on a road's drawn corridor moved out of it as the canopy's street trees are, else left out), and
//!   the canopy model's tops that are those same trees dropped (no double trees);
//! - natural_leaf_raster: the forests' leaf type / cycle on the canopy grid (the smaller polygon over the larger);
//! - natural_ground_paint: rock, scree, sand, beach, dune, glacier, wetland polygons and the cliffs' rock bands on a
//!   physics block's material map;
//! - natural_cliff_stations / natural_cliff_pieces / natural_cliff_fit: Italy's rock-face pieces (trees.lua "cliff",
//!   as roads.cliff_rows on road cuts) along mapped natural=cliff lines, their face toward the line's right (OSM: the
//!   bottom on the right of the way's direction), sized to the ground's drop across the line;
//! - natural_bathy_target: the depth polygons' bands (0 / 10 / 50 / 100 ... m) as a lake or sea floor target with a
//!   ramp across each band's edge.
//! Deterministic: f64 in a fixed order, dm's maths, no hashed iteration; ties by input order.
use crate::dm::Dm;

use numpy::ndarray::{Array2, ArrayView2};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

const R: f64 = 6378137.0;
const MAX_LAT: f64 = 85.0511287798066;

// --- species ------------------------------------------------------------------------------------------------------
// The species slots natural_tree_info answers in (server natural.SPECIES lists the vegetation group of each, in this
// order: the stock shapes' groups, trees.lua's CATALOG).
pub const S_DECIDUOUS: u8 = 0;
pub const S_CONIFER: u8 = 1;
pub const S_OAK: u8 = 2;
pub const S_BEECH: u8 = 3;
pub const S_BIRCH: u8 = 4;
pub const S_POPLAR: u8 = 5;
pub const S_PINE: u8 = 6;
pub const S_FIR: u8 = 7;
pub const S_REDWOOD: u8 = 8;
pub const S_CYPRESS: u8 = 9;
pub const S_JUNIPER: u8 = 10;
pub const S_OLIVE: u8 = 11;
pub const S_HOLM_OAK: u8 = 12;
pub const S_PALM: u8 = 13;
pub const S_EUCALYPTUS: u8 = 14;
pub const S_COUNT: usize = 15;
pub const UNKNOWN: u8 = 255;

// leaf codes (natural_tree_info's / natural_leaf_raster's): type in the low bits, cycle above
pub const LEAF_BROAD: u8 = 1;
pub const LEAF_NEEDLE: u8 = 2;
pub const LEAF_MIXED: u8 = 3;
pub const CYCLE_DECIDUOUS: u8 = 4;
pub const CYCLE_EVERGREEN: u8 = 8;
pub const CYCLE_MIXED: u8 = 12;

/// The species slot of a genus (lower case) and the species' own epithet (lower case, "" none); UNKNOWN when the
/// genus has no stock shape of its own (a broadleaf the generic deciduous trees stand for: the caller's leaf type).
fn genus_slot(genus: &str, epithet: &str) -> u8 {
    match genus {
        "pinus" => S_PINE,
        "picea" | "abies" | "pseudotsuga" | "tsuga" | "cedrus" | "larix" | "pseudolarix" | "araucaria" => S_FIR,
        "sequoia" | "sequoiadendron" | "metasequoia" | "taxodium" | "cryptomeria" => S_REDWOOD,
        "cupressus" | "chamaecyparis" | "thuja" | "cupressocyparis" | "xcupressocyparis" | "cuprocyparis"
        | "hesperocyparis" | "taxus" | "calocedrus" | "platycladus" | "thujopsis" => S_CYPRESS,
        "juniperus" => S_JUNIPER,
        "quercus" => match epithet {
            "ilex" | "suber" | "virginiana" | "agrifolia" | "coccifera" | "rotundifolia" | "chrysolepis"
            | "wislizeni" | "calliprinos" | "myrsinifolia" | "glauca" | "fusiformis" => S_HOLM_OAK,
            _ => S_OAK,
        },
        "fagus" | "carpinus" | "ostrya" | "nothofagus" => S_BEECH,
        "betula" | "alnus" => S_BIRCH,
        "populus" => match epithet {
            "tremula" | "tremuloides" | "grandidentata" => S_BIRCH,     // (aspens: the aspen shapes)
            _ => S_POPLAR,
        },
        "olea" => S_OLIVE,
        "eucalyptus" | "corymbia" | "angophora" => S_EUCALYPTUS,
        "phoenix" | "washingtonia" | "trachycarpus" | "cocos" | "syagrus" | "roystonea" | "chamaerops" | "butia"
        | "livistona" | "sabal" | "archontophoenix" | "dypsis" | "brahea" | "jubaea" | "elaeis" | "areca"
        | "veitchia" | "wodyetia" | "ptychosperma" | "pritchardia" | "bismarckia" | "howea" | "rhapis" => S_PALM,
        // (evergreen broadleaves: the holm oaks stand in)
        "laurus" | "ceratonia" | "arbutus" | "cinnamomum" | "ligustrum" | "ilex" | "photinia" | "pittosporum"
        | "schinus" | "nerium" => S_HOLM_OAK,
        _ => UNKNOWN,
    }
}

fn words_lower(s: &str) -> (String, String) {
    // (the genus and the epithet of a scientific name: "Platanus × hispanica" -> ("platanus", "hispanica"),
    // "Quercus ilex" -> ("quercus", "ilex"); "x" / "×" hybrid marks and cultivars in quotes skipped)
    let mut it = s.split(|c: char| c.is_whitespace() || c == ';').filter(|w| {
        let w = w.trim();
        !w.is_empty() && w != "×" && w != "x" && w != "X" && !w.starts_with('\'') && !w.starts_with('"')
    });
    let g = it.next().unwrap_or("").trim_start_matches('×').to_ascii_lowercase();
    let e = it.next().unwrap_or("").to_ascii_lowercase();
    (g, e)
}

/// A length tag in metres: "10", "10 m", "5m", "15.5", "1,5", "30 ft", "30'", "110 cm"; NaN when not one. A bare
/// number over `cm_over` is taken as centimetres (a circumference written as 110).
fn parse_len(s: &str, cm_over: f64) -> f64 {
    let t = s.trim().to_ascii_lowercase().replace(',', ".");
    if t.is_empty() {
        return f64::NAN;
    }
    let num_end = t.find(|c: char| !(c.is_ascii_digit() || c == '.')).unwrap_or(t.len());
    let v: f64 = match t[..num_end].parse() {
        Ok(v) => v,
        Err(_) => return f64::NAN,
    };
    let unit = t[num_end..].trim();
    let k = match unit {
        "" => if v > cm_over { 0.01 } else { 1.0 },
        "m" | "meter" | "meters" | "metre" | "metres" => 1.0,
        "cm" => 0.01,
        "mm" => 0.001,
        "ft" | "'" | "feet" | "foot" => 0.3048,
        "in" | "\"" | "inch" | "inches" => 0.0254,
        _ => return f64::NAN,
    };
    let m = v * k;
    if m.is_finite() && m > 0.0 { m } else { f64::NAN }
}

fn leaf_code(leaf_type: &str, leaf_cycle: &str) -> u8 {
    let t = match leaf_type.trim() {
        "broadleaved" | "broadleaf" => LEAF_BROAD,
        "needleleaved" | "needleleaf" | "coniferous" => LEAF_NEEDLE,
        "mixed" => LEAF_MIXED,
        _ => 0,
    };
    let c = match leaf_cycle.trim() {
        "deciduous" => CYCLE_DECIDUOUS,
        "evergreen" => CYCLE_EVERGREEN,
        "mixed" | "semi_deciduous" | "semi_evergreen" => CYCLE_MIXED,
        _ => 0,
    };
    t | c
}

fn tag<'a>(m: &'a serde_json::Map<String, serde_json::Value>, k: &str) -> &'a str {
    m.get(k).and_then(|v| v.as_str()).unwrap_or("")
}

/// Per feature's tags (Overture's source_tags as JSON text; "" none): (species slot or UNKNOWN, leaf code, height m,
/// crown diameter m, circumference m), NaN where not tagged or not plausible (height 1-120 m, crown 0.5-60 m,
/// circumference 0.05-25 m). The slot: the genus's (genus, else species' or taxon's first word) where it has a stock
/// shape; else the leaf type's (needleleaved: S_CONIFER, broadleaved: S_DECIDUOUS); else S_DECIDUOUS for any other
/// named genus (every conifer genus has its own slot); else UNKNOWN.
#[pyfunction]
pub fn natural_tree_info<'py>(py: Python<'py>, tags: Vec<String>)
        -> (Bound<'py, PyArray1<u8>>, Bound<'py, PyArray1<u8>>, Bound<'py, PyArray1<f32>>, Bound<'py, PyArray1<f32>>,
            Bound<'py, PyArray1<f32>>) {
    let n = tags.len();
    let (mut slot, mut leaf) = (vec![UNKNOWN; n], vec![0u8; n]);
    let (mut h, mut cr, mut circ) = (vec![f32::NAN; n], vec![f32::NAN; n], vec![f32::NAN; n]);
    for (i, t) in tags.iter().enumerate() {
        let v: serde_json::Value = match serde_json::from_str(t) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let m = match v.as_object() {
            Some(m) => m,
            None => continue,
        };
        let lc = leaf_code(tag(m, "leaf_type"), tag(m, "leaf_cycle"));
        leaf[i] = lc;
        let mut s = UNKNOWN;
        let mut named = false;
        for key in ["genus", "species", "taxon", "genus:la", "species:la"] {
            let val = tag(m, key);
            if val.is_empty() {
                continue;
            }
            let (g, e) = words_lower(val);
            named |= !g.is_empty();
            let e = if key == "genus" || key == "genus:la" {
                words_lower(tag(m, "species")).1
            } else { e };
            s = genus_slot(&g, &e);
            if s != UNKNOWN {
                break;
            }
        }
        if s == UNKNOWN {
            // (a named genus without a shape of its own: a broadleaf, the conifers' genera all have theirs)
            s = match lc & 3 {
                LEAF_NEEDLE => S_CONIFER,
                LEAF_BROAD => S_DECIDUOUS,
                _ if named => S_DECIDUOUS,
                _ => UNKNOWN,
            };
        }
        slot[i] = s;
        let hv = parse_len(tag(m, "height"), 1e9);
        if (1.0..=120.0).contains(&hv) {
            h[i] = hv as f32;
        }
        let cv = parse_len(tag(m, "diameter_crown"), 1e9);
        if (0.5..=60.0).contains(&cv) {
            cr[i] = cv as f32;
        }
        let ci = parse_len(tag(m, "circumference"), 25.0);
        if (0.05..=25.0).contains(&ci) {
            circ[i] = ci as f32;
        }
    }
    (slot.into_pyarray(py), leaf.into_pyarray(py), h.into_pyarray(py), cr.into_pyarray(py), circ.into_pyarray(py))
}

// --- placement ----------------------------------------------------------------------------------------------------
#[inline]
fn mix(i: i64, j: i64, salt: u64) -> f64 {
    let mut x = (i as u64).wrapping_mul(0x9E3779B97F4A7C15) ^ (j as u64).wrapping_add(salt).wrapping_mul(0xC2B2AE3D27D4EB4F);
    x ^= x >> 31;
    x = x.wrapping_mul(0xBF58476D1CE4E5B9);
    x ^= x >> 29;
    (x >> 11) as f64 / (1u64 << 53) as f64
}

/// A bucket grid of points (Web Mercator m) for radius queries.
struct Buckets {
    cell: f64,
    keys: Vec<(i64, i64, usize)>,       // sorted (bx, by, index)
}

impl Buckets {
    fn new(xy: &[(f64, f64)], cell: f64) -> Self {
        let mut keys: Vec<(i64, i64, usize)> = xy.iter().enumerate()
            .map(|(i, &(x, y))| ((x / cell).floor() as i64, (y / cell).floor() as i64, i)).collect();
        keys.sort_unstable();
        Buckets { cell, keys }
    }

    /// Indices of the points within the buckets touching the disc (x, y, r), in index order per bucket.
    fn near(&self, x: f64, y: f64, r: f64, out: &mut Vec<usize>) {
        out.clear();
        let (bx0, bx1) = (((x - r) / self.cell).floor() as i64, ((x + r) / self.cell).floor() as i64);
        let (by0, by1) = (((y - r) / self.cell).floor() as i64, ((y + r) / self.cell).floor() as i64);
        for bx in bx0..=bx1 {
            for by in by0..=by1 {
                let lo = self.keys.partition_point(|&(a, b, _)| (a, b) < (bx, by));
                let mut k = lo;
                while k < self.keys.len() && self.keys[k].0 == bx && self.keys[k].1 == by {
                    out.push(self.keys[k].2);
                    k += 1;
                }
            }
        }
    }
}

struct Grid<'a> {
    hs: ArrayView2<'a, f32>,
    blocked: ArrayView2<'a, u8>,
    road: ArrayView2<'a, u8>,
    curb: ArrayView2<'a, u8>,
    x0: f64,
    y0: f64,
    dx: f64,
    dy: f64,
}

impl Grid<'_> {
    fn px(&self, x: f64, y: f64) -> Option<(usize, usize)> {
        let c = ((x - self.x0) / self.dx).floor();
        let r = ((self.y0 - y) / self.dy).floor();
        let (n_r, n_c) = (self.hs.shape()[0] as f64, self.hs.shape()[1] as f64);
        if c >= 0.0 && r >= 0.0 && c < n_c && r < n_r { Some((r as usize, c as usize)) } else { None }
    }

    /// The canopy's highest top within r (Web Mercator m) of (x, y), 0 when none.
    fn canopy_max(&self, x: f64, y: f64, r: f64) -> f64 {
        let rr = (r / self.dx).ceil() as i64;
        let (c0, r0) = (((x - self.x0) / self.dx).floor() as i64, ((self.y0 - y) / self.dy).floor() as i64);
        let (n_r, n_c) = (self.hs.shape()[0] as i64, self.hs.shape()[1] as i64);
        let mut best = 0.0f64;
        for dr in -rr..=rr {
            for dc in -rr..=rr {
                if dr * dr + dc * dc > rr * rr {
                    continue;
                }
                let (row, col) = (r0 + dr, c0 + dc);
                if row < 0 || col < 0 || row >= n_r || col >= n_c {
                    continue;
                }
                let v = self.hs[[row as usize, col as usize]] as f64;
                if v.is_finite() && v > best {
                    best = v;
                }
            }
        }
        best
    }
}

/// The mapped trees of a cell placed (see the module's notes). Positions in Web Mercator m. pts (m, 2): every mapped
/// single tree at hand (the cell's and its neighbours': they drop the canopy tops near them too), slot / height /
/// crown / circumference per tree (natural_tree_info's); rows: tree rows' lines (k, 2) with row_off (r + 1) and
/// per row slot / height / crown. Grid: the canopy's smoothed heights hs and its masks (blocked: buildings, water,
/// decks; road: the roads' drawn corridors; curb: the roads + their tree pit margin; push (3, n_r, n_c): the move out
/// of a corridor in pixels, vegetation's), x0 y0 dx dy. bounds: the cell's (ex0, ey0, ex1, ey1): the trees in it are
/// placed. tops (t, 2): the canopy's tree tops; sec: Web Mercator m per ground m; params: [row spacing m, dedupe
/// minimum m, canopy search m, tree minimum m, wall clearance m (no trunk within it of a blocked pixel: a building's
/// wall, the water's edge)]; defaults: a height per slot. -> (x, y, height, crown radius (ground m),
/// slot, from_row, height source (the evidence model's rank: 1 its tag, 2 the canopy model, 3 its trunk's girth, 5 its
/// species' usual), keep_tops)
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn natural_trees<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, slot: PyReadonlyArray1<'py, u8>,
                          height: PyReadonlyArray1<'py, f32>, crown: PyReadonlyArray1<'py, f32>,
                          circ: PyReadonlyArray1<'py, f32>, rows: PyReadonlyArray2<'py, f64>,
                          row_off: PyReadonlyArray1<'py, i64>, row_slot: PyReadonlyArray1<'py, u8>,
                          row_height: PyReadonlyArray1<'py, f32>, row_crown: PyReadonlyArray1<'py, f32>,
                          hs: PyReadonlyArray2<'py, f32>, blocked: PyReadonlyArray2<'py, u8>,
                          road: PyReadonlyArray2<'py, u8>, curb: PyReadonlyArray2<'py, u8>,
                          push: PyReadonlyArray3<'py, f32>, grid: Vec<f64>, bounds: Vec<f64>,
                          tops: PyReadonlyArray2<'py, f64>, sec: f64, params: Vec<f64>, defaults: Vec<f64>)
        -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f32>>,
                     Bound<'py, PyArray1<f32>>, Bound<'py, PyArray1<u8>>, Bound<'py, PyArray1<bool>>,
                     Bound<'py, PyArray1<u8>>, Bound<'py, PyArray1<bool>>)> {
    if grid.len() != 4 || bounds.len() != 4 || params.len() != 5 || defaults.len() != S_COUNT {
        return Err(PyValueError::new_err("natural_trees: grid 4, bounds 4, params 5, defaults per slot"));
    }
    let (pv, sv, hv, cv, civ) = (pts.as_array(), slot.as_slice()?, height.as_slice()?, crown.as_slice()?,
                                 circ.as_slice()?);
    let (rv, ro, rs, rh, rc) = (rows.as_array(), row_off.as_slice()?, row_slot.as_slice()?, row_height.as_slice()?,
                                row_crown.as_slice()?);
    let g = Grid { hs: hs.as_array(), blocked: blocked.as_array(), road: road.as_array(), curb: curb.as_array(),
                   x0: grid[0], y0: grid[1], dx: grid[2], dy: grid[3] };
    let pu = push.as_array();
    let tv = tops.as_array();
    let (row_sp_m, dedupe_m, search_m, tree_min_m) = (params[0], params[1], params[2], params[3]);
    let wall_px = (params[4] * sec / g.dx).ceil().max(0.0) as i64;
    let (nr_all, nc_all) = (g.hs.shape()[0] as i64, g.hs.shape()[1] as i64);
    // (a blocked pixel within wall_px: the trunk would stand in or against a building, in the water)
    let near_blocked = |r: i64, c: i64| -> bool {
        for dr in -wall_px..=wall_px {
            for dc in -wall_px..=wall_px {
                if dr * dr + dc * dc > wall_px * wall_px {
                    continue;
                }
                let (rr, cc) = (r + dr, c + dc);
                if rr >= 0 && cc >= 0 && rr < nr_all && cc < nc_all && g.blocked[[rr as usize, cc as usize]] != 0 {
                    return true;
                }
            }
        }
        false
    };
    let (bx0, by0, bx1, by1) = (bounds[0], bounds[1], bounds[2], bounds[3]);
    // candidates: the single trees, then the rows' stations (each row from its own start: the same stations in every
    // cell it crosses), a station within 0.6 spacing of a single tree left out (the row's trees mapped one by one)
    struct Cand { x: f64, y: f64, slot: u8, h: f64, crown: f64, circ: f64, row: bool }
    let mut cands: Vec<Cand> = (0..pv.shape()[0]).map(|i| Cand {
        x: pv[[i, 0]], y: pv[[i, 1]], slot: sv[i], h: hv[i] as f64, crown: cv[i] as f64, circ: civ[i] as f64,
        row: false }).collect();
    let singles: Vec<(f64, f64)> = cands.iter().map(|c| (c.x, c.y)).collect();
    let sb = Buckets::new(&singles, 16.0 * sec);
    let mut near = Vec::new();
    for r in 0..rs.len() {
        let (a, b) = (ro[r] as usize, ro[r + 1] as usize);
        if b < a + 2 || b > rv.shape()[0] {
            continue;
        }
        let cd = rc[r] as f64;
        let sp = (if cd.is_finite() { cd.max(row_sp_m * 0.6) } else { row_sp_m }) * sec;
        let mut next = sp * 0.5;          // (the first a half spacing in from the row's start)
        let mut s0 = 0.0f64;
        for k in a..b - 1 {
            let (xa, ya, xb, yb) = (rv[[k, 0]], rv[[k, 1]], rv[[k + 1, 0]], rv[[k + 1, 1]]);
            let len = (xb - xa).dhypot(yb - ya);
            while next <= s0 + len {
                let f = if len > 0.0 { (next - s0) / len } else { 0.0 };
                let (x, y) = (xa + (xb - xa) * f, ya + (yb - ya) * f);
                sb.near(x, y, 0.6 * sp, &mut near);
                let taken = near.iter().any(|&j| (singles[j].0 - x).dhypot(singles[j].1 - y) < 0.6 * sp);
                if !taken {
                    cands.push(Cand { x, y, slot: rs[r], h: rh[r] as f64, crown: cd, circ: f64::NAN, row: true });
                }
                next += sp;
            }
            s0 += len;
        }
    }
    // each candidate's height and crown: its tag; else the canopy's top within search_m; else from its trunk's girth;
    // else its species' usual
    let mut hh = Vec::with_capacity(cands.len());
    let mut crr = Vec::with_capacity(cands.len());
    let mut hsrc = Vec::with_capacity(cands.len());
    for c in &cands {
        let mut h = c.h;
        let mut src = 1u8;
        if !h.is_finite() {
            let top = g.canopy_max(c.x, c.y, search_m * sec);
            if top >= tree_min_m {
                h = top;
                src = 2;
            } else if c.circ.is_finite() {
                let dbh = c.circ / std::f64::consts::PI;
                h = (3.0 + 28.0 * dbh).clamp(3.0, 35.0);
                src = 3;
            } else {
                let s = if (c.slot as usize) < S_COUNT { c.slot as usize } else { S_DECIDUOUS as usize };
                h = defaults[s];
                src = 5;
            }
        }
        hsrc.push(src);
        let cr = if c.crown.is_finite() { c.crown / 2.0 } else { 0.12 * h + 1.0 };
        hh.push(h);
        crr.push(cr);
    }
    // the canopy's tops that are a mapped tree: within its crown (dedupe_m at least) of one
    let all: Vec<(f64, f64)> = cands.iter().map(|c| (c.x, c.y)).collect();
    let rmax = crr.iter().cloned().fold(dedupe_m, f64::max) * sec;
    let ab = Buckets::new(&all, rmax.max(1.0));
    let nt = tv.shape()[0];
    let mut keep_tops = vec![true; nt];
    for t in 0..nt {
        let (x, y) = (tv[[t, 0]], tv[[t, 1]]);
        ab.near(x, y, rmax, &mut near);
        for &j in &near {
            let r = crr[j].max(dedupe_m) * sec;
            if (all[j].0 - x).dhypot(all[j].1 - y) <= r {
                keep_tops[t] = false;
                break;
            }
        }
    }
    // the cell's own: on its grid, never on a building / water / deck, out of a road's corridor
    let (mut ox, mut oy, mut oh, mut ocr, mut os, mut orow) = (vec![], vec![], vec![], vec![], vec![], vec![]);
    let mut ohs = vec![];
    for (j, c) in cands.iter().enumerate() {
        if !(c.x >= bx0 && c.x < bx1 && c.y >= by0 && c.y < by1) {
            continue;
        }
        let (r, col) = match g.px(c.x, c.y) {
            Some(p) => p,
            None => continue,
        };
        let (mut x, mut y) = (c.x, c.y);
        if near_blocked(r as i64, col as i64) {
            continue;
        }
        if g.road[[r, col]] != 0 {
            let (pc, pr) = (pu[[0, r, col]] as f64, pu[[1, r, col]] as f64);
            if !(pc.is_finite() && pr.is_finite()) {
                continue;
            }
            let nc = (col as f64 + pc).round() as i64;
            let nr = (r as f64 + pr).round() as i64;
            let (n_r, n_c) = (g.hs.shape()[0] as i64, g.hs.shape()[1] as i64);
            if nc < 0 || nr < 0 || nc >= n_c || nr >= n_r {
                continue;
            }
            let (nr, nc) = (nr as usize, nc as usize);
            if near_blocked(nr as i64, nc as i64) || g.curb[[nr, nc]] != 0 {
                continue;
            }
            // (to the target pixel's centre, as the canopy's tops stand: the masks are judged there, and a trunk
            // kept where it is mapped could stand up to half a pixel inside the drawn kerb)
            x = g.x0 + (nc as f64 + 0.5) * g.dx;
            y = g.y0 - (nr as f64 + 0.5) * g.dy;
            if !(x >= bx0 && x < bx1 && y >= by0 && y < by1) {
                continue;
            }
        }
        ox.push(x);
        oy.push(y);
        oh.push(hh[j] as f32);
        ocr.push(crr[j] as f32);
        os.push(c.slot);
        orow.push(c.row);
        ohs.push(hsrc[j]);
    }
    Ok((ox.into_pyarray(py), oy.into_pyarray(py), oh.into_pyarray(py), ocr.into_pyarray(py), os.into_pyarray(py),
        orow.into_pyarray(py), ohs.into_pyarray(py), keep_tops.into_pyarray(py)))
}

/// Hashed choices of placed items at Web Mercator positions (x, y): (u1 .. u_k) per item, keyed on the decimetre
/// grid and salt (the same item from every cell and anchor). -> (n, k) f64 in [0, 1)
#[pyfunction]
pub fn natural_hash<'py>(py: Python<'py>, x: PyReadonlyArray1<'py, f64>, y: PyReadonlyArray1<'py, f64>,
                         salts: Vec<u64>) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (xv, yv) = (x.as_slice()?, y.as_slice()?);
    let mut out = Array2::<f64>::zeros((xv.len(), salts.len()));
    for i in 0..xv.len() {
        let (a, b) = ((xv[i] * 10.0).floor() as i64, (yv[i] * 10.0).floor() as i64);
        for (k, &s) in salts.iter().enumerate() {
            out[[i, k]] = mix(a, b, s);
        }
    }
    Ok(out.into_pyarray(py))
}

/// The conifer share of canopy trees from their forest's leaf code (natural_leaf_raster's): needleleaved 0.9,
/// broadleaved 0.1, mixed 1/2, else the regional share given. (Not 1 and 0: a forest's polygon is drawn coarser than
/// its stands, and a few of the other kind keep the look of the rasters' mix it refines.)
#[pyfunction]
pub fn natural_leaf_share<'py>(py: Python<'py>, code: PyReadonlyArray1<'py, u8>, share: PyReadonlyArray1<'py, f64>)
        -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (cv, sv) = (code.as_slice()?, share.as_slice()?);
    if cv.len() != sv.len() {
        return Err(PyValueError::new_err("natural_leaf_share: a share per code"));
    }
    let out: Vec<f64> = cv.iter().zip(sv).map(|(&c, &s)| match c & 3 {
        LEAF_NEEDLE => 0.9,
        LEAF_BROAD => 0.1,
        LEAF_MIXED => 0.5,
        _ => s,
    }).collect();
    Ok(out.into_pyarray(py))
}

/// The groups of canopy or mapped trees after their forest's leaf cycle (outside the tropics, |lat| > tropics):
/// an evergreen broadleaf (code broadleaved or untyped, cycle evergreen) of group `generic` becomes `evergreen`; with
/// season, a deciduous one of the `bare` groups becomes leafoff[0] (north) / leafoff[1] (south). Others unchanged.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn natural_leaf_groups<'py>(py: Python<'py>, group: PyReadonlyArray1<'py, u8>, code: PyReadonlyArray1<'py, u8>,
                                lat: PyReadonlyArray1<'py, f64>, generic: u8, evergreen: u8, bare: Vec<u8>,
                                leafoff: Vec<u8>, season: bool, tropics: f64) -> PyResult<Bound<'py, PyArray1<u8>>> {
    let (gv, cv, lv) = (group.as_slice()?, code.as_slice()?, lat.as_slice()?);
    if cv.len() != gv.len() || lv.len() != gv.len() || leafoff.len() != 2 {
        return Err(PyValueError::new_err("natural_leaf_groups: a code and a latitude per group; two leafoff"));
    }
    let out: Vec<u8> = (0..gv.len()).map(|i| {
        let (g, c, la) = (gv[i], cv[i], lv[i]);
        if la.abs() <= tropics || c & 3 == LEAF_NEEDLE {
            return g;
        }
        match c & 12 {
            CYCLE_EVERGREEN if g == generic => evergreen,
            CYCLE_DECIDUOUS if season && bare.contains(&g) => if la >= 0.0 { leafoff[0] } else { leafoff[1] },
            _ => g,
        }
    }).collect();
    Ok(out.into_pyarray(py))
}

// --- polygons on grids ---------------------------------------------------------------------------------------------
#[inline]
fn crossings(edges: &[(f64, f64, f64, f64)], tx: f64, ty: f64) -> bool {
    let mut inside = false;
    for &(x0, y0, x1, y1) in edges {
        let f0 = y0 >= ty;
        let f1 = y1 >= ty;
        if f0 != f1 && (((y1 - ty) * (x0 - x1) >= (x1 - tx) * (y0 - y1)) == f1) {
            inside = !inside;
        }
    }
    inside
}

/// One part's edges in grid pixels (its rings XOR'd) and its pixel box; None when empty.
#[allow(clippy::type_complexity)]
fn part_edges(pts: &[(f64, f64)], co: &[i64], ro: &[i64], p: usize)
        -> Option<(Vec<(f64, f64, f64, f64)>, f64, f64, f64, f64, f64)> {
    let mut edges = Vec::new();
    let (mut cmin, mut rmin, mut cmax, mut rmax) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    let mut area = 0.0f64;
    for r in ro[p] as usize..ro[p + 1] as usize {
        let (a, b) = (co[r] as usize, co[r + 1] as usize);
        if b <= a || b > pts.len() {
            continue;
        }
        let mut ring = 0.0f64;
        for k in a..b {
            let (c, rr) = pts[k];
            cmin = cmin.min(c);
            cmax = cmax.max(c);
            rmin = rmin.min(rr);
            rmax = rmax.max(rr);
            let (c2, r2) = if k + 1 < b { pts[k + 1] } else { pts[a] };
            ring += c * r2 - c2 * rr;
            edges.push((c, rr, c2, r2));
        }
        area += if r == ro[p] as usize { ring.abs() } else { -ring.abs() };
    }
    if edges.is_empty() { None } else { Some((edges, cmin, rmin, cmax, rmax, (area / 2.0).max(0.0))) }
}

/// Fill the part's pixels (centres inside) on an n_r x n_c grid with f(row, col).
fn fill_part(edges: &[(f64, f64, f64, f64)], cmin: f64, rmin: f64, cmax: f64, rmax: f64, n_r: usize, n_c: usize,
             mut f: impl FnMut(usize, usize)) {
    let c0 = (cmin.floor() as i64).max(0);
    let r0 = (rmin.floor() as i64).max(0);
    let c1 = (cmax.ceil() as i64 + 1).min(n_c as i64);
    let r1 = (rmax.ceil() as i64 + 1).min(n_r as i64);
    let mut row_edges = Vec::new();
    for row in r0..r1 {
        let ty = row as f64 + 0.5;
        row_edges.clear();
        row_edges.extend(edges.iter().copied().filter(|&(_, ya, _, yb)| (ya >= ty) != (yb >= ty)));
        if row_edges.is_empty() {
            continue;
        }
        for col in c0..c1 {
            if crossings(&row_edges, col as f64 + 0.5, ty) {
                f(row as usize, col as usize);
            }
        }
    }
}

fn merc_px(ll: ArrayView2<'_, f64>, x0: f64, y0: f64, dx: f64, dy: f64) -> Vec<(f64, f64)> {
    (0..ll.shape()[0]).map(|k| {
        let lon = ll[[k, 0]];
        let lat = ll[[k, 1]].clamp(-MAX_LAT, MAX_LAT);
        let x = lon.to_radians() * R;
        let y = (std::f64::consts::PI / 4.0 + lat.to_radians() / 2.0).dtan().dln() * R;
        ((x - x0) / dx, (y0 - y) / dy)
    }).collect()
}

/// The parts of the selected features in painting order: the larger first (a smaller polygon inside one wins),
/// ties by input order. -> (feature slot in feats, part)
fn parts_by_area(pts: &[(f64, f64)], co: &[i64], ro: &[i64], po: &[i64], feats: &[i64])
        -> Vec<(usize, usize, f64)> {
    let mut parts = Vec::new();
    for (s, &i) in feats.iter().enumerate() {
        let i = i as usize;
        if i + 1 >= po.len() {
            continue;
        }
        let mut area = 0.0;
        for p in po[i] as usize..po[i + 1] as usize {
            if let Some(e) = part_edges(pts, co, ro, p) {
                area += e.5;
            }
        }
        for p in po[i] as usize..po[i + 1] as usize {
            parts.push((s, p, area));
        }
    }
    parts.sort_by(|a, b| b.2.partial_cmp(&a.2).unwrap_or(std::cmp::Ordering::Equal).then(a.0.cmp(&b.0))
        .then(a.1.cmp(&b.1)));
    parts
}

/// The selected polygons' codes (one per feature in feats) on the canopy grid (rows n_r, cols n_c; x0 y0 the top left
/// corner in Web Mercator m, dx dy m a pixel): 0 where none; the smaller polygon's code where they overlap.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn natural_leaf_raster<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>,
                                coord_off: PyReadonlyArray1<'py, i64>, ring_off: PyReadonlyArray1<'py, i64>,
                                part_off: PyReadonlyArray1<'py, i64>, feats: PyReadonlyArray1<'py, i64>,
                                codes: PyReadonlyArray1<'py, u8>, n_r: usize, n_c: usize, x0: f64, y0: f64, dx: f64,
                                dy: f64) -> PyResult<Bound<'py, PyArray2<u8>>> {
    let (co, ro, po, fs, cs) = (coord_off.as_slice()?, ring_off.as_slice()?, part_off.as_slice()?, feats.as_slice()?,
                                codes.as_slice()?);
    if cs.len() != fs.len() {
        return Err(PyValueError::new_err("natural_leaf_raster: a code per feature"));
    }
    let pts = merc_px(lonlat.as_array(), x0, y0, dx, dy);
    let mut out = Array2::<u8>::zeros((n_r, n_c));
    for (s, p, _) in parts_by_area(&pts, co, ro, po, fs) {
        if let Some((edges, cmin, rmin, cmax, rmax, _)) = part_edges(&pts, co, ro, p) {
            let v = cs[s];
            fill_part(&edges, cmin, rmin, cmax, rmax, n_r, n_c, |r, c| out[[r, c]] = v);
        }
    }
    Ok(out.into_pyarray(py))
}

/// A physics block's material map (h, w) with the natural polygons and cliff bands painted. coef (3, 2): surface.
/// grid_affine's least squares map (lon - lo0, lat - la0, 1) -> (col, row), lo0 / la0 given. Polygons: the selected
/// features (feats, mats: a material per feature; the smaller over the larger). Lines (line_feats, line_mats,
/// line_w: a band's width in pixels on the line's right, line_top: its width on the left): each line's band.
/// allow (256, 256): allow[m, b] != 0 where material m may be painted over the land cover's material b (a mapped
/// polygon refines the rasters' ground, never paints over what they found growing there).
/// -> (the new map, the number of samples changed)
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn natural_ground_paint<'py>(py: Python<'py>, idx: PyReadonlyArray2<'py, u8>, coef: PyReadonlyArray2<'py, f64>,
                                 lo0: f64, la0: f64, lonlat: PyReadonlyArray2<'py, f64>,
                                 coord_off: PyReadonlyArray1<'py, i64>, ring_off: PyReadonlyArray1<'py, i64>,
                                 part_off: PyReadonlyArray1<'py, i64>, feats: PyReadonlyArray1<'py, i64>,
                                 mats: PyReadonlyArray1<'py, u8>, line_feats: PyReadonlyArray1<'py, i64>,
                                 line_mats: PyReadonlyArray1<'py, u8>, line_w: f64, line_top: f64,
                                 allow: PyReadonlyArray2<'py, u8>)
        -> PyResult<(Bound<'py, PyArray2<u8>>, usize)> {
    let al = allow.as_array();
    if al.shape() != [256, 256] {
        return Err(PyValueError::new_err("natural_ground_paint: allow (256, 256)"));
    }
    let (co, ro, po) = (coord_off.as_slice()?, ring_off.as_slice()?, part_off.as_slice()?);
    let (fs, ms, lf, lm) = (feats.as_slice()?, mats.as_slice()?, line_feats.as_slice()?, line_mats.as_slice()?);
    if ms.len() != fs.len() || lm.len() != lf.len() {
        return Err(PyValueError::new_err("natural_ground_paint: a material per feature"));
    }
    let cf = coef.as_array();
    if cf.shape() != [3, 2] {
        return Err(PyValueError::new_err("natural_ground_paint: coef (3, 2)"));
    }
    let ll = lonlat.as_array();
    let pts: Vec<(f64, f64)> = (0..ll.shape()[0]).map(|k| {
        let (a, b) = (ll[[k, 0]] - lo0, ll[[k, 1]] - la0);
        (a * cf[[0, 0]] + b * cf[[1, 0]] + cf[[2, 0]], a * cf[[0, 1]] + b * cf[[1, 1]] + cf[[2, 1]])
    }).collect();
    let src = idx.as_array();
    let (h, w) = (src.shape()[0], src.shape()[1]);
    let mut out = src.to_owned();
    let mut changed = 0usize;
    for (s, p, _) in parts_by_area(&pts, co, ro, po, fs) {
        if let Some((edges, cmin, rmin, cmax, rmax, _)) = part_edges(&pts, co, ro, p) {
            if cmax < 0.0 || rmax < 0.0 || cmin > w as f64 || rmin > h as f64 {
                continue;
            }
            let v = ms[s];
            fill_part(&edges, cmin, rmin, cmax, rmax, h, w, |r, c| {
                if al[[v as usize, src[[r, c]] as usize]] != 0 {
                    out[[r, c]] = v;
                }
            });
        }
    }
    // the cliffs' bands: pixels within line_w on the right of a segment (the face's foot) or line_top on its left
    for (s, &i) in lf.iter().enumerate() {
        let i = i as usize;
        if i + 1 >= po.len() {
            continue;
        }
        let v = lm[s];
        for p in po[i] as usize..po[i + 1] as usize {
            for r in ro[p] as usize..ro[p + 1] as usize {
                let (a, b) = (co[r] as usize, co[r + 1] as usize);
                if b < a + 2 || b > pts.len() {
                    continue;
                }
                for k in a..b - 1 {
                    let ((c0, r0), (c1, r1)) = (pts[k], pts[k + 1]);
                    let (ex, ey) = (c1 - c0, r1 - r0);
                    let len = ex.dhypot(ey);
                    if len <= 0.0 {
                        continue;
                    }
                    let reach = line_w.max(line_top) + 1.0;
                    let cl = ((c0.min(c1) - reach).floor() as i64).max(0);
                    let rl = ((r0.min(r1) - reach).floor() as i64).max(0);
                    let ch = ((c0.max(c1) + reach).ceil() as i64 + 1).min(w as i64);
                    let rh = ((r0.max(r1) + reach).ceil() as i64 + 1).min(h as i64);
                    for row in rl..rh {
                        for col in cl..ch {
                            let (px, py) = (col as f64 + 0.5 - c0, row as f64 + 0.5 - r0);
                            let t = (px * ex + py * ey) / (len * len);
                            if !(0.0..=1.0).contains(&t) {
                                continue;
                            }
                            // signed distance: the right of the way's direction in (col, row) (row down): ex * py -
                            // ey * px > 0 is the right on a map with the north up
                            let side = (ex * py - ey * px) / len;
                            let (ru, cu) = (row as usize, col as usize);
                            if side <= line_w && side >= -line_top && al[[v as usize, src[[ru, cu]] as usize]] != 0 {
                                out[[ru, cu]] = v;
                            }
                        }
                    }
                }
            }
        }
    }
    for r in 0..h {
        for c in 0..w {
            if out[[r, c]] != src[[r, c]] {
                changed += 1;
            }
        }
    }
    Ok((out.into_pyarray(py), changed))
}

// --- cliffs -------------------------------------------------------------------------------------------------------
/// Stations along lines (k, 2) in Web Mercator m (line_off: r + 1), every `step` m from each line's start: (x, y,
/// right-hand unit normal nx, ny (the face's outward direction: the bottom on the way's right), line index, the
/// station's distance along its line).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn natural_cliff_stations<'py>(py: Python<'py>, lines: PyReadonlyArray2<'py, f64>,
                                   line_off: PyReadonlyArray1<'py, i64>, step: f64)
        -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>)> {
    let (lv, lo) = (lines.as_array(), line_off.as_slice()?);
    if step <= 0.0 {
        return Err(PyValueError::new_err("natural_cliff_stations: step > 0"));
    }
    let mut out: Vec<[f64; 5]> = Vec::new();
    let mut ids = Vec::new();
    for li in 0..lo.len().saturating_sub(1) {
        let (a, b) = (lo[li] as usize, lo[li + 1] as usize);
        if b < a + 2 || b > lv.shape()[0] {
            continue;
        }
        let mut next = step * 0.5;
        let mut s0 = 0.0;
        for k in a..b - 1 {
            let (xa, ya, xb, yb) = (lv[[k, 0]], lv[[k, 1]], lv[[k + 1, 0]], lv[[k + 1, 1]]);
            let len = (xb - xa).dhypot(yb - ya);
            if len <= 0.0 {
                continue;
            }
            let (tx, ty) = ((xb - xa) / len, (yb - ya) / len);
            while next <= s0 + len {
                let f = (next - s0) / len;
                // (Web Mercator y is north: the right of (tx, ty) is (ty, -tx))
                out.push([xa + (xb - xa) * f, ya + (yb - ya) * f, ty, -tx, next]);
                ids.push(li as i64);
                next += step;
            }
            s0 += len;
        }
    }
    let mut arr = Array2::<f64>::zeros((out.len(), 5));
    for (i, r) in out.iter().enumerate() {
        for k in 0..5 {
            arr[[i, k]] = r[k];
        }
    }
    Ok((arr.into_pyarray(py), ids.into_pyarray(py)))
}

/// Rock-face pieces along the stations (natural_cliff_stations', their line ids, ordered along each line) where the
/// ground drops (drop: m from the top side to the foot, measured by the caller across each station). shapes: the
/// stock pieces as roads.CLIFF_SHAPES (height, bottom share, front depth, width, depth per unit scale) flattened,
/// tall_m: the height from which the tall one; params: [min drop m, sink m, over m, spacing share, max height m];
/// grid / masks: a piece's footprint never on blocked or road pixels (shrunk until clear, else left out); sec: Web
/// Mercator m per ground m. -> (x, y (the pivot), yaw deg, height m, jitter) per piece
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn natural_cliff_pieces<'py>(py: Python<'py>, st: PyReadonlyArray2<'py, f64>, ids: PyReadonlyArray1<'py, i64>,
                                 drop: PyReadonlyArray1<'py, f64>, shapes: Vec<f64>, tall_m: f64, params: Vec<f64>,
                                 blocked: PyReadonlyArray2<'py, u8>, road: PyReadonlyArray2<'py, u8>, grid: Vec<f64>,
                                 sec: f64)
        -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f32>>,
                     Bound<'py, PyArray1<f32>>, Bound<'py, PyArray1<f32>>)> {
    if shapes.len() != 10 || params.len() != 5 || grid.len() != 4 {
        return Err(PyValueError::new_err("natural_cliff_pieces: two shapes of 5, params 5, grid 4"));
    }
    let (sv, iv, dv) = (st.as_array(), ids.as_slice()?, drop.as_slice()?);
    let (bv, rv) = (blocked.as_array(), road.as_array());
    let (n_r, n_c) = (bv.shape()[0] as i64, bv.shape()[1] as i64);
    let (x0, y0, dx, dy) = (grid[0], grid[1], grid[2], grid[3]);
    let (min_drop, sink, over, spacing, max_h) = (params[0], params[1], params[2], params[3], params[4]);
    let clear = |x: f64, y: f64| -> bool {
        let c = ((x - x0) / dx).floor() as i64;
        let r = ((y0 - y) / dy).floor() as i64;
        if c < 0 || r < 0 || c >= n_c || r >= n_r {
            return true;        // (off the grid: the neighbour cell's ground, not known here)
        }
        bv[[r as usize, c as usize]] == 0 && rv[[r as usize, c as usize]] == 0
    };
    let (mut ox, mut oy, mut oyaw, mut oh, mut oj) = (vec![], vec![], vec![], vec![], vec![]);
    let n = sv.shape()[0];
    let mut i = 0usize;
    let mut next_at = f64::NEG_INFINITY;
    let mut line = i64::MIN;
    while i < n {
        if iv[i] != line {
            line = iv[i];
            next_at = f64::NEG_INFINITY;
        }
        let (x, y, nx, ny, s) = (sv[[i, 0]], sv[[i, 1]], sv[[i, 2]], sv[[i, 3]], sv[[i, 4]]);
        if s < next_at || !(dv[i].is_finite() && dv[i] >= min_drop) {
            i += 1;
            continue;
        }
        let jit = mix((x * 10.0).floor() as i64, (y * 10.0).floor() as i64, 41);
        let mut hh = (dv[i] + sink + over).min(max_h) * (0.9 + 0.2 * jit);
        let k = if hh >= tall_m { 5 } else { 0 };
        let (sh, _frac, front, width, depth) = (shapes[k], shapes[k + 1], shapes[k + 2], shapes[k + 3], shapes[k + 4]);
        let mut sc = hh / sh;
        // its local -y (the front) toward the foot: yaw from the outward normal, as roads.cliff_rows
        let yaw = nx.datan2(-ny).to_degrees() + (jit - 0.5) * 20.0;
        let (c, sn) = (yaw.to_radians().dcos(), yaw.to_radians().dsin());
        let (ux, uy, vx, vy) = (c, sn, -sn, c);        // its local x (along) and y (back, into the cliff)
        let mut placed = false;
        while sc >= 0.25 {
            let (px, py) = (x - nx * front * sc * sec, y - ny * front * sc * sec);      // its pivot: the front on the line
            let mut ok = true;
            'fp: for a in [-1.0, -0.5, 0.0, 0.5, 1.0] {
                for b in [-front * 0.6, depth - front] {
                    let qx = px + (ux * a * width * sc / 2.0 + vx * b * sc) * sec;
                    let qy = py + (uy * a * width * sc / 2.0 + vy * b * sc) * sec;
                    if !clear(qx, qy) {
                        ok = false;
                        break 'fp;
                    }
                }
            }
            if ok {
                hh = sc * sh;
                ox.push(px);
                oy.push(py);
                oyaw.push((yaw.rem_euclid(360.0)) as f32);
                oh.push(hh as f32);
                oj.push(jit as f32);
                next_at = s + (spacing * width * sc).max(1.5) * sec;
                placed = true;
                break;
            }
            sc *= 0.8;
        }
        if !placed {
            next_at = s + 3.0 * sec;
        }
        i += 1;
    }
    Ok((ox.into_pyarray(py), oy.into_pyarray(py), oyaw.into_pyarray(py), oh.into_pyarray(py), oj.into_pyarray(py)))
}

/// The ground probes of placed cliff pieces in a tile's frame: per piece (e, n, yaw deg, height m) its face point (the
/// front on the line), the foot (probe m out of the face) and the top (probe m behind it). shapes / tall_m as
/// natural_cliff_pieces'. -> (3n, 2) e, n: faces, then feet, then tops
#[pyfunction]
pub fn natural_cliff_probes<'py>(py: Python<'py>, e: PyReadonlyArray1<'py, f64>, n: PyReadonlyArray1<'py, f64>,
                                 yaw: PyReadonlyArray1<'py, f64>, h: PyReadonlyArray1<'py, f64>, shapes: Vec<f64>,
                                 tall_m: f64, probe: f64) -> PyResult<Bound<'py, PyArray2<f64>>> {
    let (ev, nv, yv, hv) = (e.as_slice()?, n.as_slice()?, yaw.as_slice()?, h.as_slice()?);
    if shapes.len() != 10 {
        return Err(PyValueError::new_err("natural_cliff_probes: two shapes of 5"));
    }
    let m = ev.len();
    let mut out = Array2::<f64>::zeros((3 * m, 2));
    for i in 0..m {
        let k = if hv[i] >= tall_m { 5 } else { 0 };
        let sc = hv[i] / shapes[k];
        let front = shapes[k + 2] * sc;
        let (c, s) = (yv[i].to_radians().dcos(), yv[i].to_radians().dsin());
        let (bx, by) = (-s, c);          // local +y: back into the cliff
        let (fx, fy) = (ev[i] - bx * front, nv[i] - by * front);
        out[[i, 0]] = fx;
        out[[i, 1]] = fy;
        out[[m + i, 0]] = fx - bx * probe;
        out[[m + i, 1]] = fy - by * probe;
        out[[2 * m + i, 0]] = fx + bx * probe;
        out[[2 * m + i, 1]] = fy + by * probe;
    }
    Ok(out.into_pyarray(py))
}

/// The pivots' z and heights of cliff pieces from the drawn ground at their probes (natural_cliff_probes' z, 3n):
/// the bottom sink m under the lower of face and foot, the top over m above the top probe (the piece's own height
/// where the ground there does not rise), at most max_h; the pivot its shape's bottom share up. -> (z, height)
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn natural_cliff_fit<'py>(py: Python<'py>, h: PyReadonlyArray1<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                              jit: PyReadonlyArray1<'py, f64>, shapes: Vec<f64>, tall_m: f64, sink: f64, over: f64,
                              max_h: f64) -> PyResult<(Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    let (hv, zv, jv) = (h.as_slice()?, z.as_slice()?, jit.as_slice()?);
    let m = hv.len();
    if zv.len() != 3 * m || jv.len() != m || shapes.len() != 10 {
        return Err(PyValueError::new_err("natural_cliff_fit: z 3 per piece, a jitter each, two shapes"));
    }
    let (mut oz, mut oh) = (vec![0.0; m], vec![0.0; m]);
    for i in 0..m {
        let (zf, zb, zt) = (zv[i], zv[m + i], zv[2 * m + i]);
        let bottom = zf.min(zb) - sink - jv[i] * 0.5;
        let top = zt.max(bottom + hv[i] * 0.5) + over;
        let hh = (top - bottom).clamp(1.0, max_h);
        let k = if hh >= tall_m { 5 } else { 0 };
        oz[i] = bottom + shapes[k + 1] * hh;
        oh[i] = hh;
    }
    Ok((oz.into_pyarray(py), oh.into_pyarray(py)))
}

// --- bathymetry ---------------------------------------------------------------------------------------------------
/// The depth polygons on a sea carver's raster (n x n z16 pixels from (ox, oy): each feature's depth band, the
/// deepest one where they overlap) and its floor target: within a band the target ramps from the next shallower
/// band's depth at its edge to its own over ramp_px pixels (edges only between pixels of the raster: outside it is
/// unknown). NaN where no polygon. feats / depth: the features and their band depths (m).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn natural_bathy_target<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>,
                                 coord_off: PyReadonlyArray1<'py, i64>, ring_off: PyReadonlyArray1<'py, i64>,
                                 part_off: PyReadonlyArray1<'py, i64>, feats: PyReadonlyArray1<'py, i64>,
                                 depth: PyReadonlyArray1<'py, f64>, ox: i64, oy: i64, n: usize, zoom: u32,
                                 ramp_px: f64) -> PyResult<Bound<'py, PyArray2<f32>>> {
    let (co, ro, po, fs, dv) = (coord_off.as_slice()?, ring_off.as_slice()?, part_off.as_slice()?, feats.as_slice()?,
                                depth.as_slice()?);
    if dv.len() != fs.len() {
        return Err(PyValueError::new_err("natural_bathy_target: a depth per feature"));
    }
    let ll = lonlat.as_array();
    let world = (256u64 << zoom) as f64;
    let pts: Vec<(f64, f64)> = (0..ll.shape()[0]).map(|k| {
        let lon = ll[[k, 0]];
        let lat = ll[[k, 1]].clamp(-MAX_LAT, MAX_LAT);
        let gx = (lon + 180.0) / 360.0 * world;
        let s = lat.to_radians().dsin();
        let gy = (0.5 - ((1.0 + s) / (1.0 - s)).dln() / (4.0 * std::f64::consts::PI)) * world;
        (gx - ox as f64, gy - oy as f64)
    }).collect();
    let mut band = Array2::<f64>::from_elem((n, n), f64::NAN);
    for (s, p, _) in parts_by_area(&pts, co, ro, po, fs) {
        if let Some((edges, cmin, rmin, cmax, rmax, _)) = part_edges(&pts, co, ro, p) {
            let d = dv[s];
            if !d.is_finite() {
                continue;
            }
            fill_part(&edges, cmin, rmin, cmax, rmax, n, n, |r, c| {
                let v = &mut band[[r, c]];
                if v.is_nan() || d > *v {
                    *v = d;
                }
            });
        }
    }
    // the bands present, shallow to deep
    let mut levels: Vec<f64> = band.iter().cloned().filter(|v| v.is_finite()).collect();
    levels.sort_by(|a, b| a.partial_cmp(b).unwrap());
    levels.dedup();
    let mut target = band.mapv(|v| v as f32);
    if ramp_px > 0.0 && levels.len() > 1 {
        let big = 1e9f64;
        for w in 1..levels.len() {
            let (prev, lev) = (levels[w - 1], levels[w]);
            // the distance (pixels, chamfer 3-4 / 3) from each pixel of the bands >= lev to the nearest pixel of a
            // shallower band (a NaN pixel inside the raster counts as shallower only for the first band: land)
            let mut d = Array2::<f64>::from_elem((n, n), big);
            for r in 0..n {
                for c in 0..n {
                    let v = band[[r, c]];
                    if !(v.is_finite() && v >= lev) {
                        d[[r, c]] = 0.0;
                    }
                }
            }
            for r in 0..n {
                for c in 0..n {
                    let mut v = d[[r, c]];
                    if v == 0.0 { continue; }
                    if r > 0 { v = v.min(d[[r - 1, c]] + 1.0); if c > 0 { v = v.min(d[[r - 1, c - 1]] + 4.0 / 3.0); }
                               if c + 1 < n { v = v.min(d[[r - 1, c + 1]] + 4.0 / 3.0); } }
                    if c > 0 { v = v.min(d[[r, c - 1]] + 1.0); }
                    d[[r, c]] = v;
                }
            }
            for r in (0..n).rev() {
                for c in (0..n).rev() {
                    let mut v = d[[r, c]];
                    if v == 0.0 { continue; }
                    if r + 1 < n { v = v.min(d[[r + 1, c]] + 1.0); if c + 1 < n { v = v.min(d[[r + 1, c + 1]] + 4.0 / 3.0); }
                                   if c > 0 { v = v.min(d[[r + 1, c - 1]] + 4.0 / 3.0); } }
                    if c + 1 < n { v = v.min(d[[r, c + 1]] + 1.0); }
                    d[[r, c]] = v;
                }
            }
            for r in 0..n {
                for c in 0..n {
                    let v = band[[r, c]];
                    if v.is_finite() && v >= lev {
                        let f = (d[[r, c]] / ramp_px).min(1.0);
                        let t = prev + (lev - prev) * f;
                        // (the deepest band's own ramp decides: each band ramps from the one above it)
                        if (v - lev).abs() < 1e-9 || t > target[[r, c]] as f64 {
                            target[[r, c]] = t as f32;
                        }
                    }
                }
            }
        }
    }
    Ok(target.into_pyarray(py))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(natural_tree_info, m)?)?;
    m.add_function(wrap_pyfunction!(natural_trees, m)?)?;
    m.add_function(wrap_pyfunction!(natural_hash, m)?)?;
    m.add_function(wrap_pyfunction!(natural_leaf_share, m)?)?;
    m.add_function(wrap_pyfunction!(natural_leaf_groups, m)?)?;
    m.add_function(wrap_pyfunction!(natural_leaf_raster, m)?)?;
    m.add_function(wrap_pyfunction!(natural_ground_paint, m)?)?;
    m.add_function(wrap_pyfunction!(natural_cliff_stations, m)?)?;
    m.add_function(wrap_pyfunction!(natural_cliff_pieces, m)?)?;
    m.add_function(wrap_pyfunction!(natural_cliff_probes, m)?)?;
    m.add_function(wrap_pyfunction!(natural_cliff_fit, m)?)?;
    m.add_function(wrap_pyfunction!(natural_bathy_target, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lengths_parse_with_units() {
        assert_eq!(parse_len("10", 1e9), 10.0);
        assert_eq!(parse_len("5m", 1e9), 5.0);
        assert_eq!(parse_len("1,5 m", 1e9), 1.5);
        assert!((parse_len("30 ft", 1e9) - 9.144).abs() < 1e-9);
        assert_eq!(parse_len("110", 25.0), 1.1);
        assert_eq!(parse_len("1.10", 25.0), 1.1);
        assert!((parse_len("110 cm", 25.0) - 1.1).abs() < 1e-12);
        assert!(parse_len("tall", 1e9).is_nan());
        assert!(parse_len("10 furlongs", 1e9).is_nan());
    }

    #[test]
    fn genera_take_their_shapes() {
        assert_eq!(genus_slot("pinus", "sylvestris"), S_PINE);
        assert_eq!(genus_slot("quercus", "ilex"), S_HOLM_OAK);
        assert_eq!(genus_slot("quercus", "robur"), S_OAK);
        assert_eq!(genus_slot("populus", "tremula"), S_BIRCH);
        assert_eq!(genus_slot("populus", "nigra"), S_POPLAR);
        assert_eq!(genus_slot("platanus", "hispanica"), UNKNOWN);
        assert_eq!(words_lower("Platanus × hispanica"), ("platanus".into(), "hispanica".into()));
        assert_eq!(words_lower("Tilia 'Greenspire'"), ("tilia".into(), "".into()));
    }

    #[test]
    fn leaf_codes() {
        assert_eq!(leaf_code("broadleaved", "deciduous"), LEAF_BROAD | CYCLE_DECIDUOUS);
        assert_eq!(leaf_code("needleleaved", ""), LEAF_NEEDLE);
        assert_eq!(leaf_code("", "evergreen"), CYCLE_EVERGREEN);
        assert_eq!(leaf_code("mixed", "mixed"), LEAF_MIXED | CYCLE_MIXED);
    }
}
