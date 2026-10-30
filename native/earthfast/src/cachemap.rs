//! The map's cache view (the user, 2026-10-28: "in the map add a view that shows what's been cached (and at what
//! detail)"): an in-memory index of the download cache's tiles by place, and the overlay's grid of levels for a view.
//!
//! The cache's keys ("dem/mapterhorn/15/9372/12534.webp", "imagery/usgs/17/.../....jpg",
//! "mapdata/v2/overture/<release>/roads.7/14/x/y.npz") end in z/x/y Web-Mercator tiles; server/covermap.py gives the
//! prefixes and the class each one counts as (rules). A ".404" marker is the source saying it has nothing there: kept
//! apart (the "miss" set), it still counts as resolved for the summary's readiness, never as detail.
//!
//! Each class keeps its tiles in a BTreeMap ordered by (z, x, y): a view's grid walks only the columns under the view
//! at each zoom, so a query costs the tiles near the view, not the whole cache, and adding or removing a key is one
//! map update (the cache's hook: new and removed keys only).

use std::collections::{BTreeMap, HashMap};

use pyo3::prelude::*;
use pyo3::types::PyBytes;

pub const NZ: usize = 25;          // zooms 0..=24
const MAX_CELLS: usize = 16384;    // a grid's cells at most (128 x 128)

#[allow(dead_code)]
pub const MODE_SUMMARY: u8 = 0;
pub const MODE_ELEVATION: u8 = 1;
pub const MODE_IMAGERY: u8 = 2;
pub const MODE_MAPDATA: u8 = 3;
pub const MODE_WORLD: u8 = 4;
pub const PARTIAL: u8 = 0x80;      // a detail level met on less than half of the cell

type Tiles = BTreeMap<(u8, u32, u32), u32>;

#[derive(Default)]
struct Layer {
    data: Tiles,
    miss: Tiles,
}

/// The z/x/y tail of a cache key ("<anything>/z/x/y.ext", or "x/y.ext" when the rule fixes the zoom) and whether it is
/// a 404 marker; None when the tail is not a tile.
pub fn tail_tile(tail: &str, fixed_z: u8) -> Option<(u8, u32, u32, bool)> {
    let mut parts = tail.rsplit('/');
    let last = parts.next()?;
    let (ys, ext) = last.split_once('.')?;
    let y: u32 = ys.parse().ok()?;
    let x: u32 = parts.next()?.parse().ok()?;
    let z: u8 = if fixed_z > 0 { fixed_z } else { parts.next()?.parse().ok()? };
    if z as usize >= NZ || (x as u64) >= (1u64 << z) || (y as u64) >= (1u64 << z) {
        return None;
    }
    Some((z, x, y, ext == "404"))
}

fn bump(m: &mut Tiles, k: (u8, u32, u32), sign: i64) -> bool {
    if sign > 0 {
        *m.entry(k).or_insert(0) += sign as u32;
        true
    } else if let Some(c) = m.get_mut(&k) {
        let left = (*c as i64) + sign;
        if left <= 0 {
            m.remove(&k);
        } else {
            *c = left as u32;
        }
        true
    } else {
        false
    }
}

/// Per output cell (row-major, w x h at zoom gz from x0, y0) and zoom: the share of the cell covered by tiles of
/// that zoom (cov[cell * NZ + z]; coarser tiles cover their cells whole).
fn coverage(m: &Tiles, gz: u8, x0: u32, y0: u32, w: u32, h: u32, cov: &mut [f32]) {
    let (x0, y0, w, h) = (x0 as u64, y0 as u64, w as u64, h as u64);
    for z in 0..NZ as u8 {
        if z <= gz {
            let s = (gz - z) as u32;
            let (tx0, tx1, ty0, ty1) = (x0 >> s, (x0 + w - 1) >> s, y0 >> s, (y0 + h - 1) >> s);
            for (&(_, tx, ty), _) in m.range((z, tx0 as u32, 0)..=(z, tx1 as u32, u32::MAX)) {
                let (tx, ty) = (tx as u64, ty as u64);
                if ty < ty0 || ty > ty1 {
                    continue;
                }
                let (cx0, cx1) = ((tx << s).max(x0), (((tx + 1) << s) - 1).min(x0 + w - 1));
                let (cy0, cy1) = ((ty << s).max(y0), (((ty + 1) << s) - 1).min(y0 + h - 1));
                for cy in cy0..=cy1 {
                    for cx in cx0..=cx1 {
                        cov[(((cy - y0) * w + (cx - x0)) as usize) * NZ + z as usize] = 1.0;
                    }
                }
            }
        } else {
            let s = (z - gz) as u32;
            let part = 0.25f32.powi(s as i32);
            let (tx0, tx1) = (x0 << s, ((x0 + w) << s) - 1);
            let (ty0, ty1) = (y0 << s, ((y0 + h) << s) - 1);
            if tx0 > u32::MAX as u64 {
                continue;
            }
            for (&(_, tx, ty), _) in m.range((z, tx0 as u32, 0)..=(z, tx1.min(u32::MAX as u64) as u32, u32::MAX)) {
                let ty = ty as u64;
                if ty < ty0 || ty > ty1 {
                    continue;
                }
                let (cx, cy) = ((tx as u64 >> s) - x0, (ty >> s) - y0);
                cov[((cy * w + cx) as usize) * NZ + z as usize] += part;
            }
        }
    }
}

/// The detail level of one cell's coverage: the deepest zoom whose tiles (with the deeper ones) cover at least half of
/// it; else the deepest zoom it has at all, marked PARTIAL; 0 for none.
pub fn detail(c: &[f32]) -> u8 {
    let mut cum = 0.0f32;
    for z in (0..NZ).rev() {
        cum += c[z].min(1.0);
        if cum >= 0.5 - 1e-6 {
            return (z as u8).max(1);
        }
    }
    for z in (0..NZ).rev() {
        if c[z] > 0.0 {
            return (z as u8).max(1) | PARTIAL;
        }
    }
    0
}

fn full(level: u8) -> u8 {
    if level & PARTIAL != 0 { 0 } else { level }
}

/// The index (server/covermap.py keeps one per server and feeds it the cache's keys).
#[pyclass(module = "earthfast")]
pub struct CoverIndex {
    rules: Vec<(String, u16, u8)>,     // (prefix, class, fixed zoom or 0), longest prefix first
    layers: HashMap<u16, Layer>,
    caps: HashMap<(u32, u32), u8>,     // Mapterhorn's deepest zoom per z6 tile (dem.coverage_index)
    cap_default: u8,
}

impl CoverIndex {
    fn apply(&mut self, keys: &[String], sign: i64) -> usize {
        let mut n = 0;
        for key in keys {
            let Some(&(ref pre, class, fz)) = self.rules.iter().find(|r| key.starts_with(r.0.as_str())) else {
                continue;
            };
            let Some((z, x, y, miss)) = tail_tile(&key[pre.len()..], fz) else { continue };
            let l = self.layers.entry(class).or_default();
            if bump(if miss { &mut l.miss } else { &mut l.data }, (z, x, y), sign) {
                n += 1;
            }
        }
        n
    }

    fn cov(&self, class: u16, miss_too: bool, gz: u8, x0: u32, y0: u32, w: u32, h: u32) -> Vec<f32> {
        let mut cov = vec![0.0f32; (w * h) as usize * NZ];
        if let Some(l) = self.layers.get(&class) {
            coverage(&l.data, gz, x0, y0, w, h, &mut cov);
            if miss_too {
                coverage(&l.miss, gz, x0, y0, w, h, &mut cov);
            }
        }
        cov
    }

    /// Map data per cell: 2 where at least half of the cell's z14 cells have every drive layer, 1 where any has any.
    fn mapdata(&self, layer_classes: &[u16], drive_mask: u32, gz: u8, x0: u32, y0: u32, w: u32, h: u32) -> Vec<u8> {
        const CZ: u8 = 14;
        let (x0l, y0l, wl, hl) = (x0 as u64, y0 as u64, w as u64, h as u64);
        let (cx0, cx1, cy0, cy1) = if gz <= CZ {
            let s = (CZ - gz) as u32;
            (x0l << s, ((x0l + wl) << s) - 1, y0l << s, ((y0l + hl) << s) - 1)
        } else {
            let s = (gz - CZ) as u32;
            (x0l >> s, (x0l + wl - 1) >> s, y0l >> s, (y0l + hl - 1) >> s)
        };
        let mut masks: HashMap<(u32, u32), u32> = HashMap::new();
        for (i, c) in layer_classes.iter().enumerate().take(32) {
            if let Some(l) = self.layers.get(c) {
                for (&(_, x, y), _) in l.data.range((CZ, cx0 as u32, 0)..=(CZ, cx1 as u32, u32::MAX)) {
                    if (y as u64) >= cy0 && (y as u64) <= cy1 {
                        *masks.entry((x, y)).or_insert(0) |= 1 << i;
                    }
                }
            }
        }
        let n = (w * h) as usize;
        let mut out = vec![0u8; n];
        if gz >= CZ {
            let s = (gz - CZ) as u32;
            for j in 0..h {
                for i in 0..w {
                    let k = (((x0 + i) >> s), ((y0 + j) >> s));
                    if let Some(&m) = masks.get(&k) {
                        out[(j * w + i) as usize] = if m & drive_mask == drive_mask { 2 } else if m != 0 { 1 } else { 0 };
                    }
                }
            }
        } else {
            let s = (CZ - gz) as u32;
            let mut nfull = vec![0u64; n];
            let mut nany = vec![0u64; n];
            for (&(x, y), &m) in masks.iter() {
                let (i, j) = (((x as u64) >> s) - x0l, ((y as u64) >> s) - y0l);
                let k = (j * wl + i) as usize;
                if m != 0 {
                    nany[k] += 1;
                }
                if m & drive_mask == drive_mask {
                    nfull[k] += 1;
                }
            }
            let half = (1u64 << (2 * s)) / 2;
            for k in 0..n {
                out[k] = if nfull[k] > 0 && nfull[k] >= half.max(1) { 2 } else if nany[k] > 0 { 1 } else { 0 };
            }
        }
        out
    }
}

#[pymethods]
impl CoverIndex {
    /// rules: [(prefix, class, fixed zoom (0: the key has z/x/y))]; the longest matching prefix decides.
    #[new]
    fn new(mut rules: Vec<(String, u16, u8)>) -> Self {
        rules.sort_by(|a, b| b.0.len().cmp(&a.0.len()).then(a.0.cmp(&b.0)));
        CoverIndex { rules, layers: HashMap::new(), caps: HashMap::new(), cap_default: 12 }
    }

    /// Cache keys added (new files): how many were tiles of a known class.
    fn add(&mut self, py: Python<'_>, keys: Vec<String>) -> usize {
        py.allow_threads(|| self.apply(&keys, 1))
    }

    /// Cache keys removed (evicted, cleared, forgotten).
    fn remove(&mut self, py: Python<'_>, keys: Vec<String>) -> usize {
        py.allow_threads(|| self.apply(&keys, -1))
    }

    /// Rectangles (class, south, west, north, east) in degrees counted (sign 1) or taken out again (-1), each at the
    /// zoom where it spans about two tiles a side (cover_rect_tiles): what was built for some place (the revisit
    /// cache's items).
    fn add_rects(&mut self, rows: Vec<(u16, f64, f64, f64, f64)>, sign: i64) -> usize {
        let mut n = 0;
        for (class, s, w, nn, e) in rows {
            let l = self.layers.entry(class).or_default();
            for (z, x, y) in rect_tiles(s, w, nn, e) {
                bump(&mut l.data, (z, x, y), sign);
                n += 1;
            }
        }
        n
    }

    /// Mapterhorn's deepest zoom per z6 tile [(x, y, zoom)] (dem.coverage_index) and the zoom elsewhere: where the
    /// near terrain's DEM zoom stops (the summary's "ready to drive" asks no deeper than that).
    fn set_dem_caps(&mut self, caps: Vec<(u32, u32, u8)>, default: u8) {
        self.caps = caps.into_iter().map(|(x, y, z)| ((x, y), z)).collect();
        self.cap_default = default;
    }

    /// (data tiles, miss markers) of a class.
    fn count(&self, class: u16) -> (usize, usize) {
        self.layers.get(&class).map_or((0, 0), |l| (l.data.len(), l.miss.len()))
    }

    /// The overlay's levels (bytes, w x h row-major, north row first) for the zoom-gz cells x0..x0+w, y0..y0+h.
    /// Modes: 0 summary (0 none, 1 some data, 2 far-field data, 3 ready to drive), 1 elevation / 2 imagery (the
    /// detail zoom, | 0x80 where it covers less than half the cell), 3 map data (0, 1 some layers, 2 every drive
    /// layer), 4 world (0, 1 far tiles by an older version / settings, 2 far tiles, 3 near tiles older, 4 near tiles).
    #[pyo3(signature = (gz, x0, y0, w, h, mode, dem=1, img=2, layers=vec![], drive_mask=0, hm_zoom=16, far_dem=11,
                        drive_img=16, far_img=13, some_dem=10, some_img=11, world=vec![]))]
    #[allow(clippy::too_many_arguments)]
    fn grid<'py>(&self, py: Python<'py>, gz: u8, x0: u32, y0: u32, w: u32, h: u32, mode: u8, dem: u16, img: u16,
                 layers: Vec<u16>, drive_mask: u32, hm_zoom: u8, far_dem: u8, drive_img: u8, far_img: u8,
                 some_dem: u8, some_img: u8, world: Vec<(u16, bool, bool)>) -> PyResult<Bound<'py, PyBytes>> {
        if gz as usize >= NZ || w == 0 || h == 0 || (w as usize) * (h as usize) > MAX_CELLS
            || (x0 as u64 + w as u64) > (1u64 << gz) || (y0 as u64 + h as u64) > (1u64 << gz) {
            return Err(pyo3::exceptions::PyValueError::new_err("cache map: bad grid"));
        }
        let out = py.allow_threads(|| {
            let n = (w * h) as usize;
            let mut out = vec![0u8; n];
            match mode {
                MODE_ELEVATION | MODE_IMAGERY => {
                    let cov = self.cov(if mode == MODE_ELEVATION { dem } else { img }, false, gz, x0, y0, w, h);
                    for k in 0..n {
                        out[k] = detail(&cov[k * NZ..(k + 1) * NZ]);
                    }
                }
                MODE_MAPDATA => out = self.mapdata(&layers, drive_mask, gz, x0, y0, w, h),
                MODE_WORLD => {
                    for (class, near, current) in world.iter() {
                        let cov = self.cov(*class, false, gz, x0, y0, w, h);
                        let lv = match (near, current) { (false, false) => 1, (false, true) => 2, (true, false) => 3,
                                                         (true, true) => 4 };
                        for k in 0..n {
                            if cov[k * NZ..(k + 1) * NZ].iter().any(|&v| v > 0.0) {
                                out[k] = out[k].max(lv);
                            }
                        }
                    }
                }
                _ => {
                    let demc = self.cov(dem, true, gz, x0, y0, w, h);
                    let demd = self.cov(dem, false, gz, x0, y0, w, h);
                    let imgc = self.cov(img, true, gz, x0, y0, w, h);
                    let imgd = self.cov(img, false, gz, x0, y0, w, h);
                    let md = self.mapdata(&layers, drive_mask, gz, x0, y0, w, h);
                    for j in 0..h {
                        for i in 0..w {
                            let k = (j * w + i) as usize;
                            let (dr, ir) = (full(detail(&demc[k * NZ..(k + 1) * NZ])),
                                            full(detail(&imgc[k * NZ..(k + 1) * NZ])));
                            let s6 = gz.saturating_sub(6) as u32;
                            let cap = *self.caps.get(&((x0 + i) >> s6, (y0 + j) >> s6)).unwrap_or(&self.cap_default);
                            let drive_dem = hm_zoom.min(cap.max(1));
                            out[k] = if dr >= drive_dem && ir >= drive_img && md[k] == 2 {
                                3
                            } else if dr >= far_dem && ir >= far_img {
                                2
                            } else if (detail(&demd[k * NZ..(k + 1) * NZ]) & !PARTIAL) >= some_dem
                                || (detail(&imgd[k * NZ..(k + 1) * NZ]) & !PARTIAL) >= some_img || md[k] > 0 {
                                1
                            } else {
                                0
                            };
                        }
                    }
                }
            }
            out
        });
        Ok(PyBytes::new(py, &out))
    }
}

/// Web-Mercator tile (x, y) of lat / lon at zoom z (clamped to the world).
pub fn tile_of(lat: f64, lon: f64, z: u8) -> (u32, u32) {
    let n = (1u64 << z) as f64;
    let lat = lat.clamp(-85.051_128_779_806_6, 85.051_128_779_806_6).to_radians();
    let x = ((lon + 180.0) / 360.0 * n).floor().clamp(0.0, n - 1.0);
    let y = ((1.0 - lat.tan().asinh() / std::f64::consts::PI) / 2.0 * n).floor().clamp(0.0, n - 1.0);
    (x as u32, y as u32)
}

/// The tiles a rectangle (degrees) is counted in: at the zoom where it spans about two tiles a side (6..=16; at most
/// 64 tiles).
pub fn rect_tiles(s: f64, w: f64, n: f64, e: f64) -> Vec<(u8, u32, u32)> {
    if !(s.is_finite() && w.is_finite() && n.is_finite() && e.is_finite()) || n < s || e < w {
        return vec![];
    }
    let span = ((e - w) / 360.0).max(1e-9);
    let z = ((1.0 / span).log2().floor() as i64).clamp(6, 16) as u8;
    let (xa, ya) = tile_of(n, w, z);
    let (xb, yb) = tile_of(s, e, z);
    let mut out = Vec::new();
    for y in ya..=yb {
        for x in xa..=xb {
            if out.len() >= 64 {
                return out;
            }
            out.push((z, x, y));
        }
    }
    out
}

/// Python: the tile rectangles of rect_tiles (tests, the World mode's rows).
#[pyfunction]
pub fn cover_rect_tiles(s: f64, w: f64, n: f64, e: f64) -> Vec<(u8, u32, u32)> {
    rect_tiles(s, w, n, e)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tails() {
        assert_eq!(tail_tile("15/9372/12534.webp", 0), Some((15, 9372, 12534, false)));
        assert_eq!(tail_tile("17/1/2.404", 0), Some((17, 1, 2, true)));
        assert_eq!(tail_tile("4704/6244.npz", 14), Some((14, 4704, 6244, false)));
        assert_eq!(tail_tile("1/5/0.jpg", 0), None);       // x past the zoom's width
        assert_eq!(tail_tile("download_urls.json", 0), None);
    }

    #[test]
    fn detail_levels() {
        let mut c = vec![0.0f32; NZ];
        assert_eq!(detail(&c), 0);
        c[16] = 0.25;
        assert_eq!(detail(&c), 16 | PARTIAL);
        c[12] = 1.0;
        assert_eq!(detail(&c), 12);
        c[15] = 0.3;
        assert_eq!(detail(&c), 15);
    }
}
