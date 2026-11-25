//! Snow on the ground photos (winter plan W2, BNE_WINTER; server/snowcover.py is the glue): the block's base photo and
//! every far tile take the same snow term, after their roads and rock, from
//!   - a band table per forcing cell (the snow state the W1 kernel or a debug preset gives: up to 8 rows of (band
//!     elevation m, cover 0..8, depth bin 0..6, age 0..3) per table, linear in elevation between rows), blended
//!     between the four nearest cell centres by the texel's lat / lon;
//!   - the texel's geodetic height (the texture's height grid, frame z, plus the frame's curvature drop at that point)
//!     raised on slopes facing the pole and lowered on slopes facing the sun (ASPECT_M: the white line is lower on the
//!     shaded side), so the snow line is cut by our DEM, not by the weather grid;
//!   - permanent snow above a climatic snowline (snow_ela: by latitude, only where the Koppen map has a polar / alpine
//!     class near the point), in any season;
//!   - nothing on steep ground (STEEP: rock shows), the photo's dark texels (tree crowns, their shadows) keeping most of
//!     their colour (the forest's darker speckle), texels already snow-white left as they are (snowy imagery is not
//!     whitened twice), and partial cover broken into world-anchored patches finer than the texel can show faded out.
//!
//! The cover is evaluated on a coarse grid (about COARSE_M a node) and interpolated; the patch noise and the blend are
//! per texel. Pure: f64 / f32 arithmetic and the crate's deterministic maths only (no platform libm), the same bits on
//! every platform. Off (no SnowLook made) the photos are untouched.

use numpy::ndarray::{Array2, Array3, ArrayView2, ArrayView3};
use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::dm::Dm;

pub const ROWS: usize = 8;          // band rows per table at most
pub const ROAD_CLASSES: usize = 5;  // plow classes P0..P4 (W3): the share of the ground's cover a road keeps, 0..8
const RD: usize = ROAD_CLASSES + 1; // ... and then the share of still water frozen and snow-covered (lake ice), 0..8
pub const N_PARAMS: usize = 88;
const NO_ELA: f64 = 1.0e9;

/// The constants snowcover.py passes (snowcover.PARAMS, in this order; a few slots kept for their place: canopy_sat,
/// canopy_sat_floor, winter_dark, read no more since the evergreen share decides a crown's look).
#[derive(Clone, Copy, Debug)]
#[allow(dead_code)]
pub struct SnowParams {
    aspect_m: f64,              // m the line moves on a slope facing the pole (up) / the sun (down), at shade 1
    steep_tan: (f64, f64),      // slope (tan) where snow starts to slide off, and where none holds
    perm_band: (f64, f64),      // m below / above the climatic snowline over which permanent snow fades in
    patch_m: (f64, f64),        // patch noise scales (m): grass tips, drifts
    patch_w: f64,               // weight of the first scale (the second: 1 - it)
    soft: f64,                  // the patch edge's width in cover units
    gain: f64,                  // cover -> patch threshold: a = (cover * gain - bias - noise) / soft + 0.5
    bias: f64,
    canopy_v: (f64, f64),       // photo value (max channel) below which a green texel is canopy (fully at v0)
    canopy_keep: f64,           // share of its colour a canopy texel keeps under full cover
    canopy_rel: (f64, f64),     // ... or its value against the local mean below which it is (a darker stand)
    canopy_green: (f64, f64),   // green over red (G - R, 0..1) from which a dark texel is a crown, not shade
    canopy_r_m: f64,            // m: the local mean's radius
    canopy_gb: (f64, f64),      // green against blue (G - B): bluer than this is shade or water, not a crown
    canopy_pct: (f64, f64),     // the ground's reference value: at least this share of the texture's this percentile
    water_v: (f64, f64),        // a flat, dark, blue texel is open water (the photo's own: no snow on it, W6 ices it)
    water_slope: (f64, f64),    // ... flat: slope (tan) below which
    canopy_sat: (f64, f64),     // saturation from which a crown keeps all of canopy_keep (a bare, grey crown less) ...
    canopy_sat_floor: f64,      // ... at least this share of it
    treeline_band: f64,         // m: crowns fade out over this band about the climatic treeline (none above it)
    struct_keep: f64,           // share of its colour a textured texel keeps (a town's streets, roofs, yards' trees
                                // and their shadows at 10 m; a forest), where the texel is coarse enough to say
    struct_cv: (f64, f64),      // the texture: the value's coefficient of variation in a 5 x 5 texel window
    struct_texel: (f64, f64),   // m a texel over which the window rules fade in (finer: the photo's own detail)
    winter_dark: f64,           // a kept crown or textured texel darker by this share under full cover (low sun)
    rock_rel: (f64, f64),       // on steep ground a texel this much darker than the ground round it is bare rock
    lake_frac: (f64, f64),      // share of water round a water texel from which it is still water (a lake, not a river)
    lake_r_m: f64,              // m: the radius of that share
    sea_h: (f64, f64),          // m: water below this geodetic height is the sea (it stays open)
    flat: (f64, f64),           // the DEM's water: geodetic height steps (per m) between grid nodes below which the
                                // ground is a water surface (the DEMs' hydro-flattened lakes and rivers) ...
    flat_v: (f64, f64),         // ... where the photo there is no brighter than this (value) ...
    flat_cv: (f64, f64),        // ... and smooth (its window's coefficient of variation, coarse texels)
    montane: (f64, f64, f64),   // m: mid-latitude crowns are evergreen from this height to this one (spruce, fir,
                                // pine forests of the mountains), at most this share
    bare: [f64; 3],             // a leafless crown's colour over snow (sRGB): trunks, twigs and their shadows
    bare_k: f64,                // ... its share of a deciduous crown's texel under full cover (the rest: the snow seen
                                // through)
    ever_dark: f64,             // an evergreen crown darker by this share under full cover (low winter sun)
    open_dark: f64,             // open water darker by this share under full cover (the winter sky it mirrors)
    white_v: (f64, f64),        // photo value above which a neutral texel counts as snow already
    white_sat: f64,             // ... at most this saturation
    shade: (f64, f64, f64),     // the snow's brightness: base + gain * photo luminance, at most max
    colour: [[f64; 3]; 4],      // the snow's colour (sRGB) by age: fresh, settled, old, dirty
    opacity: [f64; 7],          // the snow's opacity by depth bin D0..D6 (a dusting lets the ground show)
    coarse_m: f64,              // m between the cover's grid nodes (at least one texel)
    coarse_max: f64,            // nodes a side at most
    perm_depth: f64,            // depth bin and age of permanent snow
    perm_age: f64,
}

impl SnowParams {
    pub fn from_slice(p: &[f64]) -> PyResult<SnowParams> {
        if p.len() != N_PARAMS {
            return Err(PyValueError::new_err(format!("snow params: {} values, expected {}", p.len(), N_PARAMS)));
        }
        let c = |i: usize| [p[i], p[i + 1], p[i + 2]];
        Ok(SnowParams {
            aspect_m: p[0], steep_tan: (p[1], p[2]), perm_band: (p[3], p[4]), patch_m: (p[5], p[6]), patch_w: p[7],
            soft: p[8].max(1e-6), gain: p[9], bias: p[10], canopy_v: (p[11], p[12]), canopy_keep: p[13],
            white_v: (p[14], p[15]), white_sat: p[16], shade: (p[17], p[18], p[19]),
            colour: [c(20), c(23), c(26), c(29)],
            opacity: [p[32], p[33], p[34], p[35], p[36], p[37], p[38]],
            coarse_m: p[39], coarse_max: p[40].max(2.0), perm_depth: p[41], perm_age: p[42],
            canopy_rel: (p[43], p[44]), canopy_green: (p[45], p[46]), canopy_r_m: p[47],
            canopy_gb: (p[48], p[49]), canopy_pct: (p[50], p[51]), water_v: (p[52], p[53]),
            water_slope: (p[54], p[55]), canopy_sat: (p[56], p[57]), canopy_sat_floor: p[58],
            treeline_band: p[59], struct_keep: p[60], struct_cv: (p[61], p[62]), struct_texel: (p[63], p[64]),
            winter_dark: p[65], rock_rel: (p[66], p[67]), lake_frac: (p[68], p[69]), lake_r_m: p[70],
            sea_h: (p[71], p[72]), flat: (p[73], p[74]), flat_v: (p[75], p[76]), flat_cv: (p[77], p[78]),
            montane: (p[79], p[80], p[81]), bare: [p[82], p[83], p[84]], bare_k: p[85], ever_dark: p[86], open_dark: p[87],
        })
    }
}

/// The band tables of the forcing cells round a texture: cell (ci0 + i, cj0 + j) (ci = floor((lon + 180) / res),
/// cj = floor((90 - lat) / res)) uses table idx[j, i] (-1: no snow there).
#[derive(Clone, Debug)]
pub struct SnowField {
    res: f64,
    ci0: i64,
    cj0: i64,
    idx: Array2<i32>,
    tables: Vec<[[f64; 4]; ROWS]>,
    nrows: Vec<usize>,
    road: Vec<[f64; RD]>,
}

/// (cover 0..1, depth bin 0..6, age 0..3) of one table at height h.
fn table_at(t: &[[f64; 4]; ROWS], n: usize, h: f64) -> (f64, f64, f64) {
    let row = |r: &[f64; 4]| (r[1] / 8.0, r[2], r[3]);
    if n == 0 {
        return (0.0, 0.0, 0.0);
    }
    if h <= t[0][0] {
        return row(&t[0]);
    }
    for k in 0..n - 1 {
        let (a, b) = (&t[k], &t[k + 1]);
        if h < b[0] {
            let w = if b[0] > a[0] { (h - a[0]) / (b[0] - a[0]) } else { 1.0 };
            // depth and age are those of the snow that is there: a bare row's own say nothing
            let (d, g) = if a[1] <= 0.0 { (b[2], b[3]) } else if b[1] <= 0.0 { (a[2], a[3]) }
                         else { (a[2] + (b[2] - a[2]) * w, a[3] + (b[3] - a[3]) * w) };
            return ((a[1] + (b[1] - a[1]) * w) / 8.0, d, g);
        }
    }
    row(&t[n - 1])
}

impl SnowField {
    pub fn new(res: f64, ci0: i64, cj0: i64, idx: ArrayView2<i32>, tables: ArrayView3<i32>,
               nrows: ArrayView2<i32>) -> PyResult<SnowField> {
        // nrows: (n, 1 + RD): the row count, then the table's road shares, then its lake ice
        let n = tables.shape()[0];
        if !(res > 0.0) || tables.shape()[1] != ROWS || tables.shape()[2] != 4 || nrows.shape() != [n, 1 + RD]
            || idx.is_empty() {
            return Err(PyValueError::new_err("snow field: res > 0, tables (n, 8, 4), counts (n, 7), a cell raster"));
        }
        let mut tabs = Vec::with_capacity(n);
        let mut counts = Vec::with_capacity(n);
        let mut road = Vec::with_capacity(n);
        for t in 0..n {
            let c = nrows[[t, 0]];
            if !(1..=ROWS as i32).contains(&c) {
                return Err(PyValueError::new_err("snow field: 1 to 8 rows a table"));
            }
            let mut tab = [[0.0f64; 4]; ROWS];
            for (r, row) in tab.iter_mut().enumerate().take(c as usize) {
                for (q, v) in row.iter_mut().enumerate() {
                    *v = tables[[t, r, q]] as f64;
                }
                if r > 0 && tables[[t, r, 0]] < tables[[t, r - 1, 0]] {
                    return Err(PyValueError::new_err("snow field: band rows by elevation, lowest first"));
                }
            }
            tabs.push(tab);
            counts.push(c as usize);
            let mut rd = [0.0f64; RD];
            for (k, v) in rd.iter_mut().enumerate() {
                *v = (nrows[[t, 1 + k]] as f64 / 8.0).clamp(0.0, 1.0);
            }
            road.push(rd);
        }
        if idx.iter().any(|&i| i >= n as i32) {
            return Err(PyValueError::new_err("snow field: a cell names a table that is not there"));
        }
        Ok(SnowField { res, ci0, cj0, idx: idx.to_owned(), tables: tabs, nrows: counts, road })
    }

    /// (cover, depth, age, road shares) at (lat, lon, h): the four nearest cells' tables blended bilinearly by the
    /// point's position between their centres (cells outside the raster: the nearest inside).
    fn at(&self, lat: f64, lon: f64, h: f64) -> (f64, f64, f64, [f64; RD]) {
        let (nj, ni) = (self.idx.shape()[0] as i64, self.idx.shape()[1] as i64);
        let u = (lon + 180.0) / self.res - 0.5 - self.ci0 as f64;
        let v = (90.0 - lat) / self.res - 0.5 - self.cj0 as f64;
        let (u0, v0) = (u.floor(), v.floor());
        let (fu, fv) = (u - u0, v - v0);
        let (mut c, mut d, mut g) = (0.0, 0.0, 0.0);
        let mut rd = [0.0f64; RD];
        let mut wr = 0.0;
        for (dj, wv) in [(0i64, 1.0 - fv), (1, fv)] {
            for (di, wu) in [(0i64, 1.0 - fu), (1, fu)] {
                let w = wu * wv;
                if w <= 0.0 {
                    continue;
                }
                let j = (v0 as i64 + dj).clamp(0, nj - 1) as usize;
                let i = (u0 as i64 + di).clamp(0, ni - 1) as usize;
                let t = self.idx[[j, i]];
                if t < 0 {
                    continue;
                }
                let (tc, td, tg) = table_at(&self.tables[t as usize], self.nrows[t as usize], h);
                c += w * tc;
                d += w * td;
                g += w * tg;
                for (k, r) in rd.iter_mut().enumerate() {
                    *r += w * self.road[t as usize][k];
                }
                wr += w;
            }
        }
        if wr > 0.0 {
            for r in rd.iter_mut() {
                *r /= wr;
            }
        }
        (c, d, g, rd)
    }
}

fn smooth(x: f64, a: f64, b: f64) -> f64 {
    let t = if b > a { ((x - a) / (b - a)).clamp(0.0, 1.0) } else if x >= a { 1.0 } else { 0.0 };
    t * t * (3.0 - 2.0 * t)
}

/// Smooth value noise in [0, 1] on the unit lattice (f64 lattice coordinates: world metres in f32 would lose the
/// fine scales far from the frame's origin).
fn vnoise(x: f64, y: f64, seed: i64) -> f64 {
    let h = |ix: i64, iy: i64| -> f64 {
        let mut v = ix.wrapping_mul(73856093) ^ iy.wrapping_mul(19349663) ^ seed.wrapping_mul(83492791);
        v = (v ^ (v >> 13)).wrapping_mul(1274126177);
        ((v ^ (v >> 16)) & 0xFFFF) as f64 / 65535.0
    };
    let (x0, y0) = (x.floor(), y.floor());
    let (mut fx, mut fy) = (x - x0, y - y0);
    fx = fx * fx * (3.0 - 2.0 * fx);
    fy = fy * fy * (3.0 - 2.0 * fy);
    let (ix, iy) = (x0 as i64, y0 as i64);
    let (a, b, c, d) = (h(ix, iy), h(ix + 1, iy), h(ix, iy + 1), h(ix + 1, iy + 1));
    let top = a + (b - a) * fx;
    top + ((c + (d - c) * fx) - top) * fy
}

/// A georeferenced uint8 grid of the winter packs (winterpacks.py: the leaf type's evergreen share, JRC's water share):
/// row 0 at north, column 0 at west, res degrees a cell, value / scale, nodata no data.
#[derive(Clone, Debug)]
pub struct PackGrid {
    v: Array2<u8>,
    wide: Array2<u8>,       // the 3 x 3 cells' mean round each cell (no data counted full): a wide body of water
    near: Array2<u8>,       // the 3 x 3 cells' max: water within a cell of here (a river narrower than its cells)
    north: f64,
    west: f64,
    res: f64,
    scale: f64,
    nodata: Option<u8>,
}

impl PackGrid {
    fn new(v: Array2<u8>, north: f64, west: f64, res: f64, scale: f64, nodata: Option<u8>) -> PackGrid {
        let (nj, ni) = (v.shape()[0], v.shape()[1]);
        let full = scale.round().clamp(0.0, 254.0) as u32;
        let wide = Array2::from_shape_fn((nj, ni), |(j, i)| {
            let (mut sum, mut n) = (0u32, 0u32);
            for jj in j.saturating_sub(1)..=(j + 1).min(nj - 1) {
                for ii in i.saturating_sub(1)..=(i + 1).min(ni - 1) {
                    let x = v[[jj, ii]];
                    sum += if Some(x) == nodata { full } else { x as u32 };
                    n += 1;
                }
            }
            ((sum + n / 2) / n) as u8
        });
        let near = Array2::from_shape_fn((nj, ni), |(j, i)| {
            let mut m = 0u8;
            for jj in j.saturating_sub(1)..=(j + 1).min(nj - 1) {
                for ii in i.saturating_sub(1)..=(i + 1).min(ni - 1) {
                    let x = v[[jj, ii]];
                    m = m.max(if Some(x) == nodata { full as u8 } else { x });
                }
            }
            m
        });
        PackGrid { v, wide, near, north, west, res, scale, nodata }
    }

    /// The wide-body share (the 3 x 3 cells' mean) at (lat, lon), bilinear; None outside.
    fn wide_at(&self, lat: f64, lon: f64) -> Option<f64> {
        self.grid_at(&self.wide, lat, lon)
    }

    /// The nearby share (the 3 x 3 cells' max) at (lat, lon), bilinear; None outside.
    fn near_at(&self, lat: f64, lon: f64) -> Option<f64> {
        self.grid_at(&self.near, lat, lon)
    }

    fn grid_at(&self, grid: &Array2<u8>, lat: f64, lon: f64) -> Option<f64> {
        let (nj, ni) = (grid.shape()[0] as i64, grid.shape()[1] as i64);
        let fj = (self.north - lat) / self.res - 0.5;
        let fi = (lon - self.west) / self.res - 0.5;
        if fj < -1.0 || fi < -1.0 || fj > nj as f64 || fi > ni as f64 {
            return None;
        }
        let (j0, i0) = (fj.floor(), fi.floor());
        let (wj, wi) = (fj - j0, fi - i0);
        let mut val = 0.0;
        for (dj, w1) in [(0i64, 1.0 - wj), (1, wj)] {
            for (di, w2) in [(0i64, 1.0 - wi), (1, wi)] {
                let j = (j0 as i64 + dj).clamp(0, nj - 1) as usize;
                let i = (i0 as i64 + di).clamp(0, ni - 1) as usize;
                val += w1 * w2 * grid[[j, i]] as f64 / self.scale;
            }
        }
        Some(val)
    }

    /// (value 0..1 bilinear over the cells with data, share of the four with no data) at (lat, lon); None outside.
    fn at(&self, lat: f64, lon: f64) -> Option<(f64, f64)> {
        let (nj, ni) = (self.v.shape()[0] as i64, self.v.shape()[1] as i64);
        let fj = (self.north - lat) / self.res - 0.5;
        let fi = (lon - self.west) / self.res - 0.5;
        if fj < -1.0 || fi < -1.0 || fj > nj as f64 || fi > ni as f64 {
            return None;
        }
        let (j0, i0) = (fj.floor(), fi.floor());
        let (wj, wi) = (fj - j0, fi - i0);
        let (mut val, mut w, mut nd) = (0.0, 0.0, 0.0);
        for (dj, w1) in [(0i64, 1.0 - wj), (1, wj)] {
            for (di, w2) in [(0i64, 1.0 - wi), (1, wi)] {
                let j = (j0 as i64 + dj).clamp(0, nj - 1) as usize;
                let i = (i0 as i64 + di).clamp(0, ni - 1) as usize;
                let x = self.v[[j, i]];
                if Some(x) == self.nodata {
                    nd += w1 * w2;
                } else {
                    val += w1 * w2 * x as f64 / self.scale;
                    w += w1 * w2;
                }
            }
        }
        Some((if w > 0.0 { val / w } else { f64::NAN }, nd))
    }
}

type PackGridArg<'py> = (PyReadonlyArray2<'py, u8>, f64, f64, f64, f64, i32);

fn pack_grid(a: Option<PackGridArg<'_>>) -> PyResult<Option<PackGrid>> {
    Ok(match a {
        Some((v, north, west, res, scale, nodata)) => {
            let v = v.as_array().to_owned();
            if v.is_empty() || !(res > 0.0) || !(scale > 0.0) {
                return Err(PyValueError::new_err("pack grid: values, res > 0, scale > 0"));
            }
            Some(PackGrid::new(v, north, west, res, scale, (0..=255).contains(&nodata).then_some(nodata as u8)))
        }
        None => None,
    })
}

/// The share of evergreen crowns in a Koppen class's forests (climate.py's legend, 1-30): a climate fallback until a
/// leaf-type map (MODIS MCD12C1 / ESA CCI land cover: see snowcover.py) is packed. Tropical and Mediterranean
/// forests evergreen; the oceanic and humid subtropical ones mixed; hot-summer continental (Minneapolis, Chicago,
/// Moscow's south) mostly deciduous; warm-summer continental mixed; subarctic (taiga) and alpine tundra evergreen.
const EVERGREEN: [f64; 31] = [0.5,
    0.9, 0.9, 0.8,              // Af Am Aw
    0.6, 0.6, 0.6, 0.6,         // BWh BWk BSh BSk (junipers, pinyon, pines where trees stand)
    0.8, 0.8, 0.7,              // Csa Csb Csc
    0.5, 0.5, 0.6,              // Cwa Cwb Cwc
    0.35, 0.35, 0.6,            // Cfa Cfb Cfc
    0.6, 0.6, 0.8, 0.85,        // Dsa Dsb Dsc Dsd
    0.2, 0.45, 0.8, 0.85,       // Dwa Dwb Dwc Dwd
    0.2, 0.45, 0.85, 0.85,      // Dfa Dfb Dfc Dfd
    0.8, 0.8];                  // ET EF

/// The evergreen share at each (lat, lon) (the Koppen grid kop at res deg, its four pixels blended; the sea's pixels
/// left out).
#[pyfunction]
pub fn snow_evergreen<'py>(py: Python<'py>, lat: PyReadonlyArray1<'py, f64>, lon: PyReadonlyArray1<'py, f64>,
                           kop: PyReadonlyArray2<'py, u8>, res: f64) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (la, lo, k) = (lat.as_array(), lon.as_array(), kop.as_array());
    if la.len() != lo.len() || k.is_empty() || !(res > 0.0) {
        return Err(PyValueError::new_err("snow_evergreen: lat and lon of one length, a Koppen grid, res > 0"));
    }
    let (nj, ni) = (k.shape()[0] as i64, k.shape()[1] as i64);
    let out: Vec<f64> = py.detach(|| la.iter().zip(lo.iter()).map(|(&a, &o)| {
        let fj = (90.0 - a) / res - 0.5;
        let fi = (o + 180.0) / res - 0.5;
        let (j0, i0) = (fj.floor(), fi.floor());
        let (wj, wi) = (fj - j0, fi - i0);
        let (mut e, mut w) = (0.0, 0.0);
        for (dj, w1) in [(0i64, 1.0 - wj), (1, wj)] {
            for (di, w2) in [(0i64, 1.0 - wi), (1, wi)] {
                let j = (j0 as i64 + dj).clamp(0, nj - 1) as usize;
                let i = (i0 as i64 + di).rem_euclid(ni) as usize;
                let c = k[[j, i]] as usize;
                if (1..=30).contains(&c) {
                    e += w1 * w2 * EVERGREEN[c];
                    w += w1 * w2;
                }
            }
        }
        if w > 0.0 { e / w } else { EVERGREEN[0] }
    }).collect());
    Ok(out.into_pyarray(py))
}

/// The climatic snowline (m) at |lat| (deg), before the Koppen gate: the equilibrium line of mountain glaciers,
/// highest in the dry subtropics, near sea level past 75 deg.
const ELA_TABLE: [(f64, f64); 14] = [(0.0, 5000.0), (15.0, 5400.0), (25.0, 5800.0), (32.0, 5400.0), (40.0, 4300.0),
    (45.0, 3200.0), (50.0, 2500.0), (55.0, 1900.0), (60.0, 1400.0), (65.0, 950.0), (70.0, 550.0), (75.0, 250.0),
    (80.0, 50.0), (90.0, 0.0)];
const ELA_E_PEN: f64 = 800.0;       // m higher where the polar / alpine class is only at the edge of the point

/// The climatic treeline (m) by |lat| (deg): no crowns above it (the alpine meadows, scree and rock above it are not
/// forest, however dark the photo).
const TREELINE_TABLE: [(f64, f64); 11] = [(0.0, 3800.0), (20.0, 3900.0), (30.0, 3800.0), (35.0, 3700.0), (40.0, 3500.0),
    (45.0, 2400.0), (50.0, 1800.0), (60.0, 900.0), (66.0, 450.0), (70.0, 150.0), (90.0, 0.0)];

fn treeline_lat(lat: f64) -> f64 {
    let a = lat.abs().min(90.0);
    for k in 0..TREELINE_TABLE.len() - 1 {
        let (l0, z0) = TREELINE_TABLE[k];
        let (l1, z1) = TREELINE_TABLE[k + 1];
        if a <= l1 {
            return z0 + (z1 - z0) * (a - l0) / (l1 - l0);
        }
    }
    0.0
}

fn ela_lat(lat: f64) -> f64 {
    let a = lat.abs().min(90.0);
    for k in 0..ELA_TABLE.len() - 1 {
        let (l0, z0) = ELA_TABLE[k];
        let (l1, z1) = ELA_TABLE[k + 1];
        if a <= l1 {
            return z0 + (z1 - z0) * (a - l0) / (l1 - l0);
        }
    }
    0.0
}

fn e_share(kop: ArrayView2<u8>, res: f64, lat: f64, lon: f64) -> f64 {
    let (nj, ni) = (kop.shape()[0] as i64, kop.shape()[1] as i64);
    let fj = (90.0 - lat) / res - 0.5;
    let fi = (lon + 180.0) / res - 0.5;
    let (j0, i0) = (fj.floor(), fi.floor());
    let (wj, wi) = (fj - j0, fi - i0);
    let mut e = 0.0;
    for (dj, w1) in [(0i64, 1.0 - wj), (1, wj)] {
        for (di, w2) in [(0i64, 1.0 - wi), (1, wi)] {
            let j = (j0 as i64 + dj).clamp(0, nj - 1) as usize;
            let i = (i0 as i64 + di).rem_euclid(ni) as usize;
            if kop[[j, i]] >= 29 {
                e += w1 * w2;
            }
        }
    }
    e
}

/// The climatic snowline (m above the geoid's datum, as the DEM) at each (lat, lon): ELA_TABLE by latitude, raised by
/// up to ELA_E_PEN where the Koppen grid kop (climate.py: 0.1 deg, classes 29 ET / 30 EF polar and alpine) has
/// only a part of the point's four pixels polar, and none (1e9) where it has none: permanent snow only in a polar or
/// alpine climate.
#[pyfunction]
pub fn snow_ela<'py>(py: Python<'py>, lat: PyReadonlyArray1<'py, f64>, lon: PyReadonlyArray1<'py, f64>,
                     kop: PyReadonlyArray2<'py, u8>, res: f64) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (la, lo, k) = (lat.as_array(), lon.as_array(), kop.as_array());
    if la.len() != lo.len() || k.is_empty() || !(res > 0.0) {
        return Err(PyValueError::new_err("snow_ela: lat and lon of one length, a Koppen grid, res > 0"));
    }
    let out: Vec<f64> = py.detach(|| la.iter().zip(lo.iter()).map(|(&a, &o)| {
        let e = e_share(k, res, a, o);
        if e > 0.0 { ela_lat(a) + ELA_E_PEN * (1.0 - e) } else { NO_ELA }
    }).collect());
    Ok(out.into_pyarray(py))
}

/// True when any Koppen pixel within one pixel of the box (south, west, north, east) is polar / alpine (29, 30):
/// where snow_ela can be finite, so the permanent snow is in the key of a texture there and nowhere else.
#[pyfunction]
pub fn snow_perm_possible(kop: PyReadonlyArray2<'_, u8>, res: f64, south: f64, west: f64, north: f64, east: f64)
                          -> PyResult<bool> {
    let k = kop.as_array();
    if k.is_empty() || !(res > 0.0) {
        return Err(PyValueError::new_err("snow_perm_possible: a Koppen grid, res > 0"));
    }
    let (nj, ni) = (k.shape()[0] as i64, k.shape()[1] as i64);
    let j0 = (((90.0 - north) / res).floor() as i64 - 1).clamp(0, nj - 1);
    let j1 = (((90.0 - south) / res).floor() as i64 + 1).clamp(0, nj - 1);
    let i0 = ((west + 180.0) / res).floor() as i64 - 1;
    let i1 = (((east + 180.0) / res).floor() as i64 + 1).min(i0 + ni - 1);
    for j in j0..=j1 {
        for i in i0..=i1 {
            if k[[j as usize, i.rem_euclid(ni) as usize]] >= 29 {
                return Ok(true);
            }
        }
    }
    Ok(false)
}

/// Bilinear sample of a grid g at fractional (row fy, column fx), clamped inside.
#[inline]
fn bil(g: &ArrayView2<f64>, fy: f64, fx: f64) -> f64 {
    let (ny, nx) = (g.shape()[0], g.shape()[1]);
    let fy = fy.clamp(0.0, (ny - 1) as f64);
    let fx = fx.clamp(0.0, (nx - 1) as f64);
    let (j0, i0) = ((fy.floor() as usize).min(ny.saturating_sub(2)), (fx.floor() as usize).min(nx.saturating_sub(2)));
    let (wy, wx) = (fy - j0 as f64, fx - i0 as f64);
    let (j1, i1) = ((j0 + 1).min(ny - 1), (i0 + 1).min(nx - 1));
    (g[[j0, i0]] * (1.0 - wx) + g[[j0, i1]] * wx) * (1.0 - wy) + (g[[j1, i0]] * (1.0 - wx) + g[[j1, i1]] * wx) * wy
}

/// Everything a texture's snow needs: its square in the frame, the height grid, the geodetic grid over the square,
/// the band field and the constants. Made by snowcover.look (Python) and handed to finish_far_photo or apply().
#[pyclass(module = "earthfast", frozen)]
pub struct SnowLook {
    area: (f64, f64, f64),          // the texture's square: x0, y0 (south-west), size (frame m)
    origin: (f64, f64),             // the frame's lat0, lon0 (world-anchored noise)
    u: Array2<f64>,                 // heights (frame z) [j north from uy0, i east from ux0]
    gx: Array2<f64>,                // their gradient (dz/dx, dz/dy per m)
    gy: Array2<f64>,
    uarea: (f64, f64, f64),         // ux0, uy0, spacing
    geo: [Array2<f64>; 5],          // lat, lon, zoff (geodetic h - frame z), ela, evergreen share, on G x G nodes
    field: Option<SnowField>,
    p: SnowParams,
    road: Option<Array2<u8>>,       // the texture's plow class per texel (0 none, 1..5 P0..P4), when known
    leaf: Option<PackGrid>,         // the leaf type pack's evergreen share (in place of the climate's where it has data)
    trees: Option<PackGrid>,        // ... and its tree cover (a stand the photo cannot show: a leaf-off photo's forest)
    wsh: Option<PackGrid>,          // the water share pack (JRC GSW: lakes and rivers the photo cannot tell)
    water: Option<Array2<u8>>,      // the mapped water per texel (WATER_*), when known
}

/// The mapped water mask's values (snowcover.water_classes): land, moving water (rivers, canals, streams: open), still
/// water (lakes, ponds, reservoirs: frozen by the tables' ice share), the sea (open).
#[allow(dead_code)]
pub const WATER_LAND: u8 = 0;
pub const WATER_MOVING: u8 = 1;
pub const WATER_STILL: u8 = 2;
pub const WATER_SEA: u8 = 3;

struct Coarse {
    s: usize,
    nk: usize,
    c: Vec<f64>,        // cover (before the slope taper)
    st: Vec<f64>,       // the slope taper (1 flat, 0 a wall)
    h: Vec<f64>,        // geodetic height
    fl: Vec<f64>,       // the largest geodetic height step to a neighbour node, per m (0 on a flattened lake)
    ever: Vec<f64>,     // the evergreen share
    wsh: Vec<f64>,      // the water pack's share (-1: no pack)
    wsea: Vec<f64>,     // the water pack's no-data share (the open sea past the mapped coast)
    wwide: Vec<f64>,    // the water pack's wide-body share (a lake or reservoir, not a river)
    trees: Vec<f64>,    // the leaf pack's tree cover (-1: no pack)
    wnear: Vec<f64>,    // the water pack's nearby share (the 3 x 3 cells' max)
    o: Vec<f64>,        // opacity
    g: Vec<f64>,        // age
    rd: Vec<[f64; RD]>,
    sl: Vec<f64>,       // slope (tan)
    tree: Vec<f64>,     // 1 below the climatic treeline, 0 above
}

struct Pt {
    c: f64,             // cover before the slope taper
    steep: f64,         // the slope taper
    d: f64,
    g: f64,
    rd: [f64; RD],
    slope: f64,
    tree: f64,          // 1 below the climatic treeline, 0 above
    h: f64,
    ever: f64,          // the evergreen share of the crowns there
    wsh: f64,           // the water pack's share there (-1 none)
    wsea: f64,
    wwide: f64,
    trees: f64,
    wnear: f64,
}

impl SnowLook {
    /// (cover, depth, age, road shares) at frame point (x, y).
    fn point(&self, x: f64, y: f64) -> (f64, f64, f64, [f64; RD]) {
        let q = self.point_slope(x, y);
        (q.c * q.steep, q.d, q.g, q.rd)
    }

    /// Everything the photo's grid wants at a frame point.
    fn point_slope(&self, x: f64, y: f64) -> Pt {
        let (x0, y0, size) = self.area;
        let gn = (self.geo[0].shape()[0] - 1) as f64;
        let (fy, fx) = ((y0 + size - y) / size * gn, (x - x0) / size * gn);
        let lat = bil(&self.geo[0].view(), fy, fx);
        let lon = bil(&self.geo[1].view(), fy, fx);
        let zoff = bil(&self.geo[2].view(), fy, fx);
        let ela = bil(&self.geo[3].view(), fy, fx);
        let ever_climate = bil(&self.geo[4].view(), fy, fx);
        let (ux0, uy0, usp) = self.uarea;
        let (uy, ux) = ((y - uy0) / usp, (x - ux0) / usp);
        let z = bil(&self.u.view(), uy, ux);
        let (dx, dy) = (bil(&self.gx.view(), uy, ux), bil(&self.gy.view(), uy, ux));
        let h = z + zoff;
        let tan2 = dx * dx + dy * dy;
        // the slope facing the pole (+1 straight down a 90 deg wall toward it): -(gradient toward the pole) / |n|
        let pole = if lat >= 0.0 { dy } else { -dy };
        let shade = -pole / (1.0 + tan2).sqrt();
        let he = h + self.p.aspect_m * shade;
        let (mut c, mut d, mut g, rd) = match &self.field {
            Some(f) => f.at(lat, lon, he),
            None => (0.0, 0.0, 0.0, [0.0; RD]),
        };
        if ela < NO_ELA * 0.5 {
            let pc = smooth(he, ela - self.p.perm_band.0, ela + self.p.perm_band.1);
            if pc > c {
                g = g + (self.p.perm_age - g) * ((pc - c) / pc.max(1e-9));
                c = pc;
            }
            d = d.max(self.p.perm_depth * pc);
        }
        let steep = 1.0 - smooth(tan2.sqrt(), self.p.steep_tan.0, self.p.steep_tan.1);
        let tl = treeline_lat(lat);
        let tree = 1.0 - smooth(h, tl - 0.5 * self.p.treeline_band, tl + 0.5 * self.p.treeline_band);
        let (m0, m1, mmax) = self.p.montane;
        // (the leaf type pack's share where it has data, else the climate's; the mountains' conifers either way)
        let base = self.leaf.as_ref().and_then(|l| l.at(lat, lon)).map(|(v, _)| v).filter(|v| v.is_finite())
            .unwrap_or(ever_climate);
        let ever = base.max(mmax * smooth(h, m0, m1));
        let (wsh, wsea) = match self.wsh.as_ref().and_then(|w| w.at(lat, lon)) {
            Some((v, nd)) => (if v.is_finite() { v } else { 1.0 }, nd),
            None => (-1.0, 0.0),
        };
        let wwide = self.wsh.as_ref().and_then(|w| w.wide_at(lat, lon)).unwrap_or(0.0);
        let trees = self.trees.as_ref().and_then(|t| t.at(lat, lon)).map(|(v, _)| v).filter(|v| v.is_finite())
            .unwrap_or(-1.0);
        let wnear = self.wsh.as_ref().and_then(|w| w.near_at(lat, lon)).unwrap_or(0.0);
        Pt { c, steep, d, g, rd, slope: tan2.sqrt(), tree, h, ever, wsh, wsea, wwide, trees, wnear }
    }

    fn opacity(&self, d: f64) -> f64 {
        let d = d.clamp(0.0, 6.0);
        let k = (d.floor() as usize).min(5);
        let w = d - k as f64;
        self.p.opacity[k] + (self.p.opacity[k + 1] - self.p.opacity[k]) * w
    }

    fn coarse(&self, px: usize) -> Option<Coarse> {
        let (x0, y0, size) = self.area;
        let t = size / px as f64;
        let mut s = ((self.p.coarse_m / t).round() as usize).max(1);
        s = s.max(px.div_ceil(self.p.coarse_max as usize));
        // (the cover follows the height grid: nodes twice as dense as its spacing resolve it; finer is only cost)
        let u_nodes = ((size / self.uarea.2).ceil() as usize).max(1) * 2;
        s = s.max(px.div_ceil(u_nodes.max(16)));
        let nk = (px - 1) / s + 2;
        let n = nk * nk;
        let (mut c, mut o, mut g, mut rd) = (vec![0.0; n], vec![0.0; n], vec![0.0; n], vec![[0.0; RD]; n]);
        let (mut st, mut hh) = (vec![0.0; n], vec![0.0; n]);
        let mut sl = vec![0.0; n];
        let mut tree = vec![0.0; n];
        let mut ever = vec![0.0; n];
        let (mut wsh, mut wsea, mut wwide, mut trees) = (vec![0.0; n], vec![0.0; n], vec![0.0; n], vec![0.0; n]);
        let mut wnear = vec![0.0; n];
        let mut any = false;
        for r in 0..nk {
            let y = y0 + size - ((r * s) as f64 + 0.5) * t;
            for k in 0..nk {
                let x = x0 + ((k * s) as f64 + 0.5) * t;
                let pt = self.point_slope(x, y);
                let q = r * nk + k;
                sl[q] = pt.slope;
                tree[q] = pt.tree;
                ever[q] = pt.ever;
                wsh[q] = pt.wsh;
                wsea[q] = pt.wsea;
                wwide[q] = pt.wwide;
                trees[q] = pt.trees;
                wnear[q] = pt.wnear;
                c[q] = pt.c;
                st[q] = pt.steep;
                hh[q] = pt.h;
                o[q] = self.opacity(pt.d);
                g[q] = pt.g;
                rd[q] = pt.rd;
                if pt.c > 1e-4 {
                    any = true;
                }
            }
        }
        let sp = (s as f64 * t).max(1e-6);
        let fl: Vec<f64> = (0..n).map(|q| {
            let (r, k) = (q / nk, q % nk);
            let mut m = 0.0f64;
            for dr in -2i64..=2 {
                for dk in -2i64..=2 {
                    let (r2, k2) = (r as i64 + dr, k as i64 + dk);
                    if (dr, dk) != (0, 0) && r2 >= 0 && k2 >= 0 && (r2 as usize) < nk && (k2 as usize) < nk {
                        let d = ((dr * dr + dk * dk) as f64).sqrt();
                        m = m.max((hh[q] - hh[r2 as usize * nk + k2 as usize]).abs() / (sp * d));
                    }
                }
            }
            m
        }).collect();
        any.then_some(Coarse { s, nk, c, st, h: hh, fl, ever, wsh, wsea, wwide, trees, wnear, o, g, rd, sl, tree })
    }

    /// The photo with its snow (rgb (px, px, 3), row 0 north, covering the square), or None: no snow on it.
    /// road: an optional (px, px) plow class per texel (0 not a road, 1..5 = P0..P4): the cover there times the
    /// table's share for that class (W3's hook; None: the roads take the ground's cover).
    pub fn apply_core<'a>(&'a self, img: ArrayView3<u8>, road: Option<ArrayView2<'a, u8>>) -> Option<Array3<u8>> {
        let px = img.shape()[0];
        if px < 2 || img.shape()[1] != px || img.shape()[2] != 3 {
            return None;
        }
        // (the look's own road mask when none is given and it is the texture's size)
        let own_road = self.road.as_ref().filter(|r| r.shape() == [px, px]).map(|r| r.view());
        let road = match road { Some(r) => Some(r), None => own_road };
        let wmask = self.water.as_ref().filter(|r| r.shape() == [px, px]).map(|r| r.as_standard_layout().into_owned());
        let wmask = wmask.as_ref().map(|r| r.as_slice().expect("standard layout"));
        let cg = self.coarse(px)?;
        let (x0, y0, size) = self.area;
        let t = size / px as f64;
        let p = &self.p;
        let m_lat = 111320.0;
        let m_lon = m_lat * self.origin.0.to_radians().dcos();
        let (ex0, ny0) = (self.origin.1 * m_lon + x0, self.origin.0 * m_lat + y0 + size);
        // patch noise weights: a scale finer than ~1.5 texels fades to its mean (it would alias)
        let fade = |scale: f64| (1.0 - 1.5 * t / scale).clamp(0.0, 1.0);
        let (w1, w2) = (p.patch_w * fade(p.patch_m.0), (1.0 - p.patch_w) * fade(p.patch_m.1));
        let wn = (w1 + w2).min(1.0);
        let own = img.as_standard_layout();
        let src = own.as_slice().expect("standard layout");
        let inv255 = 1.0 / 255.0;
        // the photo rules' smoothsteps of a channel value or a channel difference, tabled (the same values)
        let by_v = |a: f64, b: f64| -> Vec<f64> { (0..256).map(|v| smooth(v as f64 * inv255, a, b)).collect() };
        let by_d = |a: f64, b: f64| -> Vec<f64> { (0..511).map(|d| smooth((d as f64 - 255.0) * inv255, a, b)).collect() };
        let (t_dark, t_white, t_wv) = (by_v(p.canopy_v.0, p.canopy_v.1), by_v(p.white_v.0, p.white_v.1),
                                       by_v(p.water_v.0, p.water_v.1));
        let (t_wbg, t_wbr) = (by_d(-0.01, 0.02), by_d(0.03, 0.07));
        let water_hue = |r: usize, g: usize, b: usize| if b > r {
            (1.0 - t_wv[r.max(g).max(b)]) * t_wbg[b + 255 - g] * t_wbr[b + 255 - r] } else { 0.0 };
        // the DEM's water (a hydro-flattened surface) where the photo is no brighter than water and not lush green:
        // rivers and lakes whatever their colour in the photo (a muddy river is grey-brown in the USGS mosaic)
        let (t_fv, t_lush) = (by_v(p.flat_v.0, p.flat_v.1), by_d(0.03, 0.08));
        let flat_water = |r: usize, g: usize, b: usize, fl: f64| {
            (1.0 - smooth(fl, p.flat.0, p.flat.1)) * (1.0 - t_fv[r.max(g).max(b)]) * (1.0 - t_lush[g + 255 - r])
        };
        let node_r = |m: f64| ((m / (cg.s as f64 * t)).round() as usize).max(1);
        let (mut vm, wf, hgr, hgb) = local_value(src, px, cg.s, cg.nk, node_r(p.canopy_r_m), node_r(p.lake_r_m),
                                       &|r, g, b, q| water_hue(r, g, b).max(flat_water(r, g, b, cg.fl[q])));
        // the ground's reference brightness: the local mean, at least a share of the texture's own percentile (a forest
        // wider than the local radius is still darker than the open ground of its texture)
        let vref = p.canopy_pct.1 * value_percentile(src, p.canopy_pct.0);
        for v in vm.iter_mut() {
            *v = v.max(vref);
        }
        // the window rules (a coarse texel: 10 m Sentinel-2, 30 m far tiles): the canopy and the texture read from a 5 x 5
        // texel window, not the texel alone (one texel's JPEG block decided a crown and showed as squares)
        let coarse_w = smooth(t, p.struct_texel.0, p.struct_texel.1);
        let win = (coarse_w > 0.0).then(|| window_stats(src, px, 2));
        let mut out = src.to_vec();
        let road_own = road.map(|r| r.as_standard_layout().into_owned());
        let road_s = road_own.as_ref().map(|r| r.as_slice().expect("standard layout"));
        let full = (1.0 + p.bias + p.soft * 0.5) / p.gain;     // cover at which every texel is white
        let lin_k = 1.0 / (p.gain - p.bias).max(1e-6);
        let lw = (0.299f64, 0.587f64, 0.114f64);
        let s = cg.s;
        let nk = cg.nk;
        let inv_s = 1.0 / s as f64;
        let mut changed = false;
        // the drift scale's noise (smooth at its 20 m) on a grid of its own, a quarter of its scale apart whatever the
        // cover's grid (a block photo and a far tile sample the same world noise: no seam in the patches), interpolated
        let sn = ((p.patch_m.1 * 0.25 / t).floor() as usize).max(1);
        let nn = (px - 1) / sn + 2;
        let n2: Vec<f64> = if w2 > 0.0 {
            (0..nn * nn).map(|q| {
                let (r, k) = (q / nn, q % nn);
                let ex = ex0 + ((k * sn) as f64 + 0.5) * t;
                let ny = ny0 - ((r * sn) as f64 + 0.5) * t;
                vnoise(ex / p.patch_m.1, ny / p.patch_m.1, 12)
            }).collect()
        } else { Vec::new() };
        let inv_sn = 1.0 / sn as f64;
        let mut rn = vec![0.0f64; nn];
        // the coarse fields along this texel row (vertical lerp once a row; horizontal per texel)
        let mut rows = vec![[0.0f64; 18]; nk];      // c, steep, o, g, slope, tree, vm, water share, height, flat,
                                                    // evergreen share, hue G - R, G - B
        let mut rr = vec![[0.0f64; RD]; nk];
        for i in 0..px {
            let (r0, wy) = (i / s, (i % s) as f64 * inv_s);
            let (a0, a1) = (r0 * nk, (r0 + 1) * nk);
            if (0..nk).all(|k| cg.c[a0 + k] <= 1e-4 && cg.c[a1 + k] <= 1e-4) {
                continue;
            }
            let lv = |v: &[f64], k: usize| v[a0 + k] * (1.0 - wy) + v[a1 + k] * wy;
            for k in 0..nk {
                rows[k] = [lv(&cg.c, k), lv(&cg.st, k), lv(&cg.o, k), lv(&cg.g, k), lv(&cg.sl, k), lv(&cg.tree, k),
                           lv(&vm, k), lv(&wf, k), lv(&cg.h, k), lv(&cg.fl, k), lv(&cg.ever, k), lv(&hgr, k),
                           lv(&hgb, k), lv(&cg.wsh, k), lv(&cg.wsea, k), lv(&cg.wwide, k), lv(&cg.trees, k),
                           lv(&cg.wnear, k)];
                for q in 0..RD {
                    rr[k][q] = cg.rd[a0 + k][q] * (1.0 - wy) + cg.rd[a1 + k][q] * wy;
                }
            }
            let ny = ny0 - (i as f64 + 0.5) * t;
            if w2 > 0.0 {
                let (b0, wn_y) = ((i / sn) * nn, (i % sn) as f64 * inv_sn);
                for k in 0..nn {
                    rn[k] = n2[b0 + k] * (1.0 - wn_y) + n2[b0 + nn + k] * wn_y;
                }
            }
            for j in 0..px {
                let k0 = j / s;
                if cg.c[a0 + k0] <= 1e-4 && cg.c[a0 + k0 + 1] <= 1e-4 && cg.c[a1 + k0] <= 1e-4
                    && cg.c[a1 + k0 + 1] <= 1e-4 {
                    continue;
                }
                let wx = (j % s) as f64 * inv_s;
                let hl = |f: usize| rows[k0][f] + (rows[k0 + 1][f] - rows[k0][f]) * wx;
                let hr = |f: usize| rr[k0][f] + (rr[k0 + 1][f] - rr[k0][f]) * wx;
                let q = (i * px + j) * 3;
                let (ri, gi, bi) = (src[q] as usize, src[q + 1] as usize, src[q + 2] as usize);
                let (r, g, b) = (ri as f64, gi as f64, bi as f64);
                let mxi = ri.max(gi).max(bi);
                let mx = mxi as f64;
                let mn = r.min(g).min(b);
                let v = mx * inv255;
                let sat = if mx > 0.0 { (mx - mn) / mx } else { 0.0 };
                let vml = hl(6).max(1e-3);
                // steep ground: its darker texels are bare rock, the rest (the photo's snow, firn, light scree) white
                // (a smooth fade of the cover by the height grid's slope left soft brown smudges a grid cell wide)
                let rock = 1.0 - smooth(v / vml, p.rock_rel.0, p.rock_rel.1);
                let mut c = hl(0) * (1.0 - (1.0 - hl(1)) * rock);
                if let Some(rm) = road_s {
                    let k = rm[i * px + j] as usize;
                    if (1..=ROAD_CLASSES).contains(&k) {
                        c *= hr(k - 1);
                    }
                }
                // partial cover: world-anchored patches where the texel can show them, else an even share
                let a = if wn <= 0.0 {
                    ((c * p.gain - p.bias) * lin_k).clamp(0.0, 1.0)
                } else if c >= full && wn >= 1.0 {
                    1.0
                } else {
                    let a_lin = ((c * p.gain - p.bias) * lin_k).clamp(0.0, 1.0);
                    let ex = ex0 + (j as f64 + 0.5) * t;
                    let mut n = 0.0;
                    if w1 > 0.0 { n += w1 * vnoise(ex / p.patch_m.0, ny / p.patch_m.0, 11); }
                    if w2 > 0.0 {
                        let (kn, wxn) = (j / sn, (j % sn) as f64 * inv_sn);
                        n += w2 * (rn[kn] + (rn[kn + 1] - rn[kn]) * wxn);
                    }
                    let a_noise = ((c * p.gain - p.bias - n / wn) / p.soft + 0.5).clamp(0.0, 1.0);
                    wn * a_noise + (1.0 - wn) * a_lin
                };
                if a <= 0.0 {
                    continue;
                }
                // the colour the crown and texture rules read: the texel's own, or at coarse texels its window's
                let (cr, cgg, cb, cv, cvar) = match &win {
                    Some(w) => {
                        let o = i * px + j;
                        let m = |x: f64, y: f32| x + (y as f64 - x) * coarse_w;
                        (m(r, w.r[o]), m(g, w.g[o]), m(b, w.b[o]), m(v, w.v[o]) , w.cv[o] as f64)
                    }
                    None => (r, g, b, v, 0.0),
                };
                let tree = hl(5);
                // water first (it wins over a crown): the map's where known; else the photo's dark blue water, or a
                // hydro-flattened DEM surface where the photo is no brighter than water (a lake whatever its colour)
                let mut keep_mul = 1.0;
                let mut is_water = 0.0f64;
                let mut ice_w = 0.0f64;      // frozen, snow-covered still water: flat and as bright as a field
                let mut open_w = 0.0f64;     // open water under a winter sky: darker than the summer photo's
                if let Some(wm) = wmask {
                    // the map's water: authoritative where it is known
                    match wm[i * px + j] {
                        WATER_MOVING | WATER_SEA => { keep_mul = 0.0; is_water = 1.0; open_w = 1.0; }
                        WATER_STILL => { keep_mul *= hr(ROAD_CLASSES); is_water = 1.0; }
                        _ => {}
                    }
                } else if self.wsh.is_some() && hl(13) >= 0.0 {
                    // JRC's water share where the pack is had: water where the cell is mostly water and the photo no
                    // brighter than water and not lush green (a muddy river is grey-brown in the USGS mosaic); a wide
                    // body (most of its 445 m cell) still water, freezing by the ice share; the open sea past the
                    // coast (the pack's no data) and below sea_h open
                    let share = hl(13);
                    let lush = smooth((g - r) * inv255, 0.03, 0.08);
                    let photo_ok = (1.0 - t_fv[mxi]) * (1.0 - lush);
                    let hue = water_hue(ri, gi, bi) * (1.0 - smooth(hl(4), p.water_slope.0, p.water_slope.1));
                    // (a river narrower than the pack's 445 m cells: water within a cell of here, where the photo
                    // is smooth and darker than the ground round it: the Mississippi through Minneapolis, its cells
                    // 0.3-0.5 water)
                    let river = smooth(hl(17), 0.15, 0.35) * photo_ok * (1.0 - smooth(cvar, 0.05, 0.10))
                        * (1.0 - smooth(v / vml, 0.85, 1.0));
                    let water = (smooth(share, 0.2, 0.45) * photo_ok).max(hue * smooth(share, 0.05, 0.2)).max(river);
                    if water > 0.0 {
                        // (a wide body: the water share of the 3 x 3 cells round it, ~1.3 km: a lake's rim and a
                        // reservoir whose level moves are still water; a river 200 m wide is not)
                        let lake = smooth(hl(15), 0.3, 0.5);
                        let sea = hl(14).max(1.0 - smooth(hl(8), p.sea_h.0, p.sea_h.1));
                        let frozen = lake * hr(ROAD_CLASSES) * (1.0 - sea);
                        keep_mul *= 1.0 - water * (1.0 - frozen);
                        is_water = water;
                        ice_w = water * frozen;
                        open_w = water * (1.0 - frozen);
                    }
                } else {
                    let hue = water_hue(ri, gi, bi) * (1.0 - smooth(hl(4), p.water_slope.0, p.water_slope.1));
                    // (the DEM's water only as part of a wide body: the water share round it, blurred over lake_r_m,
                    // so a quantized plateau a grid node or two wide never shows as a pale rectangle in a forest)
                    let dem = flat_water(ri, gi, bi, hl(9)) * (1.0 - smooth(cvar, p.flat_cv.0, p.flat_cv.1))
                        * smooth(hl(7), p.lake_frac.0, p.lake_frac.1);
                    let water = hue.max(dem);
                    if water > 0.0 {
                        // open: the sea (by height) and moving water (a narrow body: a river, a canal); still water (a
                        // wide body: a lake, a reservoir) whitens as far as its ice is frozen and covered (the tables'
                        // ice share; W6 makes it drivable)
                        let lake = smooth(hl(7), p.lake_frac.0, p.lake_frac.1);
                        let sea = 1.0 - smooth(hl(8), p.sea_h.0, p.sea_h.1);
                        let frozen = lake * hr(ROAD_CLASSES) * (1.0 - sea);
                        keep_mul *= 1.0 - water * (1.0 - frozen);
                        is_water = water;
                        open_w = water * (1.0 - frozen);
                    }
                }
                // a crown (green, darker than the stand round it or simply dark): an evergreen one keeps most of its
                // colour (near black under snow); a deciduous one is a bare crown over the snow, a light grey-brown
                // (the evergreen share: the climate's, the mountains' conifers; snow_evergreen)
                let mut crown_k = 0.0f64;
                let mut bare_t = 0.0f64;
                // (the hue: the texel's own blended with the blurred one round it; the blurred one alone at coarse texels)
                let hw = 0.5 + 0.5 * coarse_w;
                let gr = (cgg - cr) * inv255 * (1.0 - hw) + hl(11) * hw;
                let gb = (cgg - cb) * inv255 * (1.0 - hw) + hl(12) * hw;
                if gr > p.canopy_green.0 && tree > 0.0 && is_water < 1.0 {
                    let green = smooth(gr, p.canopy_green.0, p.canopy_green.1) * smooth(gb, p.canopy_gb.0, p.canopy_gb.1);
                    if green > 0.0 {
                        let rel = 1.0 - smooth(cv / vml, p.canopy_rel.0, p.canopy_rel.1);
                        let dark = 1.0 - t_dark[((cv * 255.0).round() as usize).min(255)];
                        let canopy = green * smooth(rel.max(dark), 0.15, 0.6) * tree * (1.0 - is_water);
                        let ever = hl(10).clamp(0.0, 1.0);
                        crown_k = canopy * p.canopy_keep * ever;
                        bare_t = canopy * (1.0 - ever) * p.bare_k;
                    }
                }
                // a leafless stand in a leaf-off photo (brown or grey, textured, darker than the open ground round it,
                // at a coarse texel): a bare crown too (a field as dark is smooth, and goes white)
                if coarse_w > 0.0 && tree > 0.0 && is_water < 1.0 && gb - gr > -0.01 {
                    let stand = smooth(cvar, 0.04, 0.08) * (1.0 - smooth(v / vml, 0.75, 0.97)) * tree * coarse_w
                        * (1.0 - is_water);
                    bare_t = bare_t.max(stand * p.bare_k);
                }
                // the leaf pack's tree cover: in a forested cell the darker half of the texels (crowns and their
                // shadows, a leaf-off photo's brown stand included) are crowns, split by the evergreen share
                let tc = hl(16);
                if tc > 0.0 && tree > 0.0 && is_water < 1.0 {
                    let prior = smooth(tc, 0.3, 0.7) * (1.0 - smooth(v / vml, 0.85, 1.1)) * tree * (1.0 - is_water)
                        * (1.0 - t_white[mxi]);
                    let ever = hl(10).clamp(0.0, 1.0);
                    crown_k = crown_k.max(prior * p.canopy_keep * ever);
                    bare_t = bare_t.max(prior * (1.0 - ever) * p.bare_k);
                }
                // a textured coarse texel's darker part (a town's streets, alleys, roofs' shaded sides) keeps some of
                // its colour; smooth ground (yards, parks, fields, ski runs) goes white
                let mut dark_k = crown_k;
                if coarse_w > 0.0 && tree > 0.0 {
                    let darker = 1.0 - smooth(v / vml, 0.9, 1.15);
                    dark_k = dark_k.max(p.struct_keep * smooth(cvar, p.struct_cv.0, p.struct_cv.1) * darker * tree
                                        * coarse_w * (1.0 - is_water) * (1.0 - bare_t / p.bare_k.max(1e-6)));
                }
                keep_mul *= 1.0 - dark_k;
                if v > p.white_v.0 {
                    keep_mul *= 1.0 - t_white[mxi] * (1.0 - smooth(sat, p.white_sat, p.white_sat * 2.0));
                }
                let amt = a * hl(2) * keep_mul;
                // (open water in winter: Sentinel-2 shows open rivers near black between white banks, the low sun and
                // the dark sky they mirror; the summer photo's is grey-brown)
                let water_dim = 1.0 - p.open_dark * open_w * c.min(1.0);
                if amt <= 0.0 && water_dim >= 1.0 {
                    continue;
                }
                let lum = (lw.0 * r + lw.1 * g + lw.2 * b) * inv255;
                let age = hl(3).clamp(0.0, 3.0);
                let ka = (age.floor() as usize).min(2);
                let wa = age - ka as f64;
                let sh = (p.shade.0 + p.shade.1 * lum).min(p.shade.2);
                let sh = sh + (p.shade.2 - sh) * ice_w;
                let dim = (1.0 - p.ever_dark * crown_k * c.min(1.0)) * water_dim;
                let amt = amt.max(0.0);
                let bt = bare_t * c.min(1.0);
                // (a bare stand keeps its texture: crowns and gaps, lighter and darker than the stand's mean)
                let bare_tex = (v / vml).clamp(0.5, 1.4);
                for ch in 0..3 {
                    let snow = (p.colour[ka][ch] + (p.colour[ka + 1][ch] - p.colour[ka][ch]) * wa) * sh;
                    let col = snow + (p.bare[ch] * bare_tex - snow) * bt;
                    let sv = src[q + ch] as f64;
                    out[q + ch] = ((sv + (col - sv) * amt) * dim + 0.5).floor().clamp(0.0, 255.0) as u8;
                }
                changed = true;
            }
        }
        let out = Array3::from_shape_vec((px, px, 3), out).expect("sized");
        changed.then_some(out)
    }

    /// Cover (0..255) and depth bin (0..6) at frame points (xs, ys) (the terrain block's samples: surface.py sends
    /// deep-covered ones to the snow layer); the same values the photo's grid gives there.
    pub fn sample_core(&self, xs: &[f64], ys: &[f64]) -> (Vec<u8>, Vec<u8>) {
        let mut cov = Vec::with_capacity(xs.len());
        let mut dep = Vec::with_capacity(xs.len());
        for (&x, &y) in xs.iter().zip(ys) {
            let (c, d, _, _) = self.point(x, y);
            cov.push((c * 255.0 + 0.5).floor().clamp(0.0, 255.0) as u8);
            dep.push(if c > 1e-4 { (d + 0.5).floor().clamp(0.0, 6.0) as u8 } else { 0 });
        }
        (cov, dep)
    }
}

/// Round each coarse node (nk x nk, s texels apart): the photo's mean value (max channel, 0..1) over a box of rv
/// nodes, and its share of water-coloured texels (water(r, g, b): 0..1) over a box of rw nodes (edges clamped).
fn local_value(src: &[u8], px: usize, s: usize, nk: usize, rv: usize, rw: usize,
               water: &dyn Fn(usize, usize, usize, usize) -> f64) -> (Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>) {
    let mut cell = vec![0.0f64; nk * nk];
    let mut wet = vec![0.0f64; nk * nk];
    let mut cgr = vec![0.0f64; nk * nk];       // mean G - R, G - B (0..1): the hue the crown test reads
    let mut cgb = vec![0.0f64; nk * nk];
    let half = s / 2;
    for a in 0..nk {
        let i0 = (a * s).saturating_sub(half).min(px - 1);
        let i1 = (a * s + s - half).clamp(i0 + 1, px);
        for b in 0..nk {
            let j0 = (b * s).saturating_sub(half).min(px - 1);
            let j1 = (b * s + s - half).clamp(j0 + 1, px);
            let mut sum = 0u64;
            let mut w = 0.0f64;
            let (mut sgr, mut sgb) = (0i64, 0i64);
            for i in i0..i1 {
                for j in j0..j1 {
                    let q = (i * px + j) * 3;
                    let (r, g, bb) = (src[q], src[q + 1], src[q + 2]);
                    sum += r.max(g).max(bb) as u64;
                    sgr += g as i64 - r as i64;
                    sgb += g as i64 - bb as i64;
                    w += water(r as usize, g as usize, bb as usize, a * nk + b);
                }
            }
            let n = ((i1 - i0) * (j1 - j0)) as f64;
            cell[a * nk + b] = sum as f64 / (n * 255.0);
            wet[a * nk + b] = w / n;
            cgr[a * nk + b] = sgr as f64 / (n * 255.0);
            cgb[a * nk + b] = sgb as f64 / (n * 255.0);
        }
    }
    // (the hue blurred over a node each way: a source mosaic's JPEG chroma blocks, 8-16 texels, never decide a crown)
    (box_nodes(&cell, nk, rv), box_nodes(&wet, nk, rw), box_nodes(&cgr, nk, 1), box_nodes(&cgb, nk, 1))
}

/// The mean of a box of r nodes each way (edges clamped: the box shrinks), by running sums along rows, then columns.
fn box_nodes(cell: &[f64], nk: usize, r: usize) -> Vec<f64> {
    let mut tmp = vec![0.0f64; nk * nk];
    let mut pre = vec![0.0f64; nk + 1];
    for a in 0..nk {
        for b in 0..nk {
            pre[b + 1] = pre[b] + cell[a * nk + b];
        }
        for b in 0..nk {
            let (b0, b1) = (b.saturating_sub(r), (b + r).min(nk - 1));
            tmp[a * nk + b] = (pre[b1 + 1] - pre[b0]) / (b1 - b0 + 1) as f64;
        }
    }
    let mut out = vec![0.0f64; nk * nk];
    for b in 0..nk {
        for a in 0..nk {
            pre[a + 1] = pre[a] + tmp[a * nk + b];
        }
        for a in 0..nk {
            let (a0, a1) = (a.saturating_sub(r), (a + r).min(nk - 1));
            out[a * nk + b] = (pre[a1 + 1] - pre[a0]) / (a1 - a0 + 1) as f64;
        }
    }
    out
}

/// A texel's (2 r + 1)^2 window (edges clamped: the window shrinks): its mean R, G, B, value (max channel, 0..1) and
/// the value's coefficient of variation, by column sums kept down the rows.
struct Window {
    r: Vec<f32>,
    g: Vec<f32>,
    b: Vec<f32>,
    v: Vec<f32>,
    cv: Vec<f32>,
}

fn window_stats(src: &[u8], px: usize, rad: usize) -> Window {
    let n = px * px;
    let mut w = Window { r: vec![0.0; n], g: vec![0.0; n], b: vec![0.0; n], v: vec![0.0; n], cv: vec![0.0; n] };
    // column sums over the rows i - rad ..= i + rad: r, g, b, v, v^2 (integers)
    let mut col = vec![[0u64; 5]; px];
    let add = |col: &mut Vec<[u64; 5]>, i: usize, sign: bool| {
        for j in 0..px {
            let q = (i * px + j) * 3;
            let (r, g, b) = (src[q] as u64, src[q + 1] as u64, src[q + 2] as u64);
            let v = r.max(g).max(b);
            let e = [r, g, b, v, v * v];
            for k in 0..5 {
                if sign { col[j][k] += e[k] } else { col[j][k] -= e[k] }
            }
        }
    };
    for i in 0..rad.min(px - 1) {
        add(&mut col, i, true);
    }
    for i in 0..px {
        if i + rad < px {
            add(&mut col, i + rad, true);
        }
        if i > rad {
            add(&mut col, i - rad - 1, false);
        }
        let rows = (i + rad).min(px - 1) + 1 - i.saturating_sub(rad);
        // running sums along the row
        let mut run = [0u64; 5];
        for c in col.iter().take(rad.min(px - 1)) {
            for k in 0..5 { run[k] += c[k] }
        }
        for j in 0..px {
            if j + rad < px {
                for k in 0..5 { run[k] += col[j + rad][k] }
            }
            if j > rad {
                for k in 0..5 { run[k] -= col[j - rad - 1][k] }
            }
            let cols = (j + rad).min(px - 1) + 1 - j.saturating_sub(rad);
            let cnt = (rows * cols) as f64;
            let o = i * px + j;
            let (mr, mg, mb, mv) = (run[0] as f64 / cnt, run[1] as f64 / cnt, run[2] as f64 / cnt, run[3] as f64 / cnt);
            let var = (run[4] as f64 / cnt - mv * mv).max(0.0);
            w.r[o] = mr as f32;
            w.g[o] = mg as f32;
            w.b[o] = mb as f32;
            w.v[o] = (mv / 255.0) as f32;
            w.cv[o] = if mv > 1.0 { (var.sqrt() / mv) as f32 } else { 0.0 };
        }
    }
    w
}

/// The q-quantile (0..1) of the photo's value (max channel, 0..1), from its histogram.
fn value_percentile(src: &[u8], q: f64) -> f64 {
    let mut hist = [0u64; 256];
    for px in src.chunks_exact(3) {
        hist[px[0].max(px[1]).max(px[2]) as usize] += 1;
    }
    let n: u64 = hist.iter().sum();
    let want = ((n as f64) * q.clamp(0.0, 1.0)).ceil() as u64;
    let mut acc = 0u64;
    for (v, &h) in hist.iter().enumerate() {
        acc += h;
        if acc >= want.max(1) {
            return v as f64 / 255.0;
        }
    }
    1.0
}

fn gradient(u: &Array2<f64>, sp: f64) -> (Array2<f64>, Array2<f64>) {
    let (ny, nx) = (u.shape()[0], u.shape()[1]);
    let d = |a: usize, n: usize, f: &dyn Fn(usize) -> f64| -> f64 {
        if n < 2 { 0.0 } else if a == 0 { (f(1) - f(0)) / sp } else if a == n - 1 { (f(n - 1) - f(n - 2)) / sp }
        else { (f(a + 1) - f(a - 1)) / (2.0 * sp) }
    };
    let gx = Array2::from_shape_fn((ny, nx), |(j, i)| d(i, nx, &|q| u[[j, q]]));
    let gy = Array2::from_shape_fn((ny, nx), |(j, i)| d(j, ny, &|q| u[[q, i]]));
    (gx, gy)
}

#[pymethods]
impl SnowLook {
    /// area (x0, y0, size): the texture's square (frame m); origin (lat0, lon0): the frame's; u: the height grid
    /// (frame z, [j north, i east]) at uarea (ux0, uy0, spacing); geo (lat, lon, zoff, ela): G x G grids over the
    /// square (row 0 north, corner to corner); field: None or (res, ci0, cj0, idx (nj, ni) int32, tables (n, 8, 4)
    /// int32, counts (n, 6) int32: rows, then the road shares P0..P4 in eighths); params: snowcover.PARAMS; road: the
    /// texture's plow class per texel (px x px uint8: 0 none, 1..5 P0..P4), or None.
    #[new]
    #[pyo3(signature = (area, origin, u, uarea, geo, field, params, road=None, water=None, leaf=None, wsh=None,
                        trees=None))]
    #[allow(clippy::type_complexity, clippy::too_many_arguments)]
    fn new(area: (f64, f64, f64), origin: (f64, f64), u: PyReadonlyArray2<'_, f64>, uarea: (f64, f64, f64),
           geo: (PyReadonlyArray2<'_, f64>, PyReadonlyArray2<'_, f64>, PyReadonlyArray2<'_, f64>,
                 PyReadonlyArray2<'_, f64>, PyReadonlyArray2<'_, f64>),
           field: Option<(f64, i64, i64, PyReadonlyArray2<'_, i32>, PyReadonlyArray3<'_, i32>, PyReadonlyArray2<'_, i32>)>,
           params: PyReadonlyArray1<'_, f64>, road: Option<PyReadonlyArray2<'_, u8>>,
           water: Option<PyReadonlyArray2<'_, u8>>, leaf: Option<PackGridArg<'_>>,
           wsh: Option<PackGridArg<'_>>, trees: Option<PackGridArg<'_>>) -> PyResult<Self> {
        let p = SnowParams::from_slice(params.as_slice()?)?;
        let u = u.as_array().to_owned();
        let g = [geo.0.as_array().to_owned(), geo.1.as_array().to_owned(), geo.2.as_array().to_owned(),
                 geo.3.as_array().to_owned(), geo.4.as_array().to_owned()];
        if u.shape()[0] < 2 || u.shape()[1] < 2 || !(uarea.2 > 0.0) || !(area.2 > 0.0)
            || g.iter().any(|a| a.shape() != g[0].shape() || a.shape()[0] < 2 || a.shape()[1] < 2) {
            return Err(PyValueError::new_err("SnowLook: a 2 x 2 height grid or larger, spacing > 0, five geo grids alike"));
        }
        let field = match field {
            Some((res, ci0, cj0, idx, tables, counts)) => Some(SnowField::new(res, ci0, cj0, idx.as_array(),
                                                                               tables.as_array(), counts.as_array())?),
            None => None,
        };
        let (gx, gy) = gradient(&u, uarea.2);
        let road = road.map(|r| r.as_array().to_owned());
        let water = water.map(|r| r.as_array().to_owned());
        let (leaf, wsh, trees) = (pack_grid(leaf)?, pack_grid(wsh)?, pack_grid(trees)?);
        Ok(SnowLook { area, origin, u, gx, gy, uarea, geo: g, field, p, road, water, leaf, wsh, trees })
    }

    /// The photo with its snow, or None (no texel changed). road: see apply_core.
    #[pyo3(signature = (rgb, road=None))]
    fn apply<'py>(&self, py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, road: Option<PyReadonlyArray2<'py, u8>>)
                  -> PyResult<Option<Bound<'py, numpy::PyArray3<u8>>>> {
        let a = rgb.as_array();
        let rm = road.as_ref().map(|r| r.as_array());
        if a.shape()[0] != a.shape()[1] || a.shape()[2] != 3 || rm.is_some_and(|r| r.shape() != &a.shape()[..2]) {
            return Err(PyValueError::new_err("SnowLook.apply: a square RGB texture, a road mask of its size"));
        }
        let out = py.detach(|| self.apply_core(a, rm));
        Ok(out.map(|o| o.into_pyarray(py)))
    }

    /// (cover uint8 0..255, depth bin uint8) at frame points xs, ys (sample_core).
    fn sample<'py>(&self, py: Python<'py>, xs: PyReadonlyArray1<'py, f64>, ys: PyReadonlyArray1<'py, f64>)
                   -> PyResult<(Bound<'py, PyArray1<u8>>, Bound<'py, PyArray1<u8>>)> {
        let (x, y) = (xs.as_slice()?, ys.as_slice()?);
        if x.len() != y.len() {
            return Err(PyValueError::new_err("SnowLook.sample: xs and ys of one length"));
        }
        let (c, d) = py.detach(|| self.sample_core(x, y));
        Ok((c.into_pyarray(py), d.into_pyarray(py)))
    }

    /// Any snow on the square at all (the coarse grid of a px texture)?
    fn any(&self, px: usize) -> bool {
        px >= 2 && self.coarse(px).is_some()
    }
}
