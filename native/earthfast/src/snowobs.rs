//! Observed snow cover (local weather S1, ~/bnge-tests/localweather/PLAN_FINAL.md S-1 and probe P-A): the satellite
//! snow maps read in the kernel, and each map's pixels turned into an observed snow share per 150 m band and aspect
//! class of a forcing cell (with our DEM), plus the cell's observed snowline per aspect class. The snow field
//! (snowfield.rs) fuses these with the snow kernel's bands: observed snow is the evidence that old snow still lies
//! above the model's ground, and observed bare ground vetoes snow the model invented (the user, 2026-11-27: "snow
//! should only be shown when its actually snowed").
//!
//! Sources:
//!   - NOAA IMS 1 km (NSIDC G02156 v1.3, the GIS GeoTIFF, gzipped, no login; Northern Hemisphere, daily, about a day
//!     late): an uncompressed 24576 x 24576 u8 TIFF of one-row strips on a polar stereographic grid (true scale 60 N,
//!     straight-down meridian 80 W, WGS 84 as NSIDC documents it (the file's own semi-minor axis, 6,356,257 m, moves a
//!     pixel by at most 0.2 of its size at mid-latitudes), 1 km pixels, the grid's upper-left corner at
//!     (-12,288,000, 12,288,000) m). Classes: 0 outside the hemisphere, 1 open water, 2 land without snow, 3 sea or
//!     lake ice, 4 snow-covered land. The layout is checked against the file's own tags after reading (an IMS version
//!     that changes it is refused, never read wrong).
//!   - MODIS Terra and VIIRS NOAA-20 / NOAA-21 NDSI snow cover through NASA GIBS (paletted PNG tiles, EPSG:4326,
//!     the "500m" matrix set: at level 7, 2.25-degree tiles of 512 px): NDSI x 100 (0..100) on land seen clear, flags
//!     from 200 (missing, no decision, night, inland water, ocean, cloud, saturated, fill). The palette is matched to
//!     the layer's GIBS colour map by colour (the glue reads the map's entries).
//!
//! Pure and deterministic: integer counts and heights; the projection in f64 through the crate's deterministic maths
//! (dm), DEM sums in a fixed order. The same bytes in give the same counts out on every platform.

use crate::dm::Dm;
use std::io::Read;

// ---------------------------------------------------------------------------------------------------------------
// Observation classes (what each map's pixel says about the land under it)

/// No observation (outside the map, water, cloud, night, missing).
pub const OBS_NONE: u8 = 0;
/// Land seen without snow.
pub const OBS_BARE: u8 = 1;
/// Land seen snow-covered.
pub const OBS_SNOW: u8 = 2;

// ---------------------------------------------------------------------------------------------------------------
// NOAA IMS 1 km

pub const IMS_N: usize = 24576;
const IMS_ORIGIN_M: f64 = 12_288_000.0;
const IMS_PX_M: f64 = 1000.0;
const IMS_DATA_AT: usize = 8; // the first strip's offset (after the TIFF header)
const WGS_A: f64 = 6_378_137.0;
const WGS_F: f64 = 1.0 / 298.257_223_563;
const IMS_LAT_TS: f64 = 60.0;
const IMS_LON0: f64 = -80.0;

/// IMS class -> observation class.
pub fn ims_obs(v: u8) -> u8 {
    match v {
        2 => OBS_BARE,
        4 => OBS_SNOW,
        _ => OBS_NONE,
    }
}

fn ps_t(phi: f64, e: f64) -> f64 {
    let s = phi.dsin();
    (std::f64::consts::FRAC_PI_4 - phi / 2.0).dtan() / ((1.0 - e * s) / (1.0 + e * s)).dpowf(e / 2.0)
}

/// IMS grid metres (x east, y north of the pole) of a WGS 84 point (Snyder's polar stereographic, variant B).
pub fn ims_xy(lat: f64, lon: f64) -> (f64, f64) {
    let e = (WGS_F * (2.0 - WGS_F)).sqrt();
    let pc = IMS_LAT_TS.to_radians();
    let sc = pc.dsin();
    let mc = pc.dcos() / (1.0 - e * e * sc * sc).sqrt();
    let rho = WGS_A * mc * ps_t(lat.to_radians(), e) / ps_t(pc, e);
    let (sl, cl) = (lon - IMS_LON0).to_radians().dsin_cos();
    (rho * sl, -rho * cl)
}

/// The IMS pixel (row, column) holding a point, None south of the equator or off the grid.
pub fn ims_pixel(lat: f64, lon: f64) -> Option<(usize, usize)> {
    if !(lat.is_finite() && lon.is_finite()) || lat <= 0.0 || lat > 90.0 {
        return None;
    }
    let (x, y) = ims_xy(lat, lon);
    let c = ((x + IMS_ORIGIN_M) / IMS_PX_M).floor();
    let r = ((IMS_ORIGIN_M - y) / IMS_PX_M).floor();
    if c < 0.0 || r < 0.0 || c >= IMS_N as f64 || r >= IMS_N as f64 {
        return None;
    }
    Some((r as usize, c as usize))
}

/// A window of the IMS grid: rows r0..=r1, columns c0..=c1 (row-major), the classes as stored.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ImsWindow {
    pub r0: usize,
    pub c0: usize,
    pub rows: usize,
    pub cols: usize,
    pub v: Vec<u8>,
}

impl ImsWindow {
    pub fn at(&self, r: usize, c: usize) -> Option<u8> {
        if r < self.r0 || c < self.c0 || r >= self.r0 + self.rows || c >= self.c0 + self.cols {
            return None;
        }
        Some(self.v[(r - self.r0) * self.cols + (c - self.c0)])
    }
}

fn u16_at(b: &[u8], i: usize) -> Option<u16> {
    Some(u16::from_le_bytes([*b.get(i)?, *b.get(i + 1)?]))
}

fn u32_at(b: &[u8], i: usize) -> Option<u32> {
    Some(u32::from_le_bytes([*b.get(i)?, *b.get(i + 1)?, *b.get(i + 2)?, *b.get(i + 3)?]))
}

fn f64_at(b: &[u8], i: usize) -> Option<f64> {
    let mut a = [0u8; 8];
    a.copy_from_slice(b.get(i..i + 8)?);
    Some(f64::from_le_bytes(a))
}

/// Checks the IMS file's own tags (its tail: the bytes after the strips, starting at file offset `base`) against the
/// layout read: one-row uncompressed 8-bit strips in order from offset 8, 1 km pixels, the grid's corner, a polar
/// stereographic projection with true scale at 60 N about 80 W.
fn ims_check_tags(tail: &[u8], base: usize, ifd: usize) -> Result<(), String> {
    let at = |off: usize| -> Result<usize, String> {
        off.checked_sub(base).filter(|&i| i < tail.len()).ok_or_else(|| format!("IMS tag data at {off} not in the tail"))
    };
    let i0 = at(ifd)?;
    let n = u16_at(tail, i0).ok_or("IMS: short IFD")? as usize;
    let mut tags = std::collections::BTreeMap::new();
    for k in 0..n {
        let e = i0 + 2 + 12 * k;
        let tag = u16_at(tail, e).ok_or("IMS: short IFD entry")?;
        let typ = u16_at(tail, e + 2).ok_or("IMS: short IFD entry")?;
        let cnt = u32_at(tail, e + 4).ok_or("IMS: short IFD entry")? as usize;
        let val = u32_at(tail, e + 8).ok_or("IMS: short IFD entry")?;
        let valu16 = u16_at(tail, e + 8).ok_or("IMS: short IFD entry")?;
        tags.insert(tag, (typ, cnt, val, valu16));
    }
    let short = |tag: u16| -> Result<u32, String> {
        match tags.get(&tag) {
            Some(&(3, 1, _, v)) => Ok(v as u32),
            Some(&(4, 1, v, _)) => Ok(v),
            other => Err(format!("IMS: tag {tag} is {other:?}")),
        }
    };
    for (tag, want) in [(256u16, IMS_N as u32), (257, IMS_N as u32), (258, 8), (259, 1), (277, 1), (278, 1)] {
        let v = short(tag)?;
        if v != want {
            return Err(format!("IMS: tag {tag} = {v}, the reader expects {want}"));
        }
    }
    // strip offsets and byte counts: one row each, in order from offset 8
    let (t273, c273, o273, _) = *tags.get(&273).ok_or("IMS: no strip offsets")?;
    let (t279, c279, o279, _) = *tags.get(&279).ok_or("IMS: no strip byte counts")?;
    if c273 != IMS_N || c279 != IMS_N || t273 != 4 || !(t279 == 3 || t279 == 4) {
        return Err("IMS: the strips are not one row each".into());
    }
    let (so, sc) = (at(o273 as usize)?, at(o279 as usize)?);
    for r in 0..IMS_N {
        let off = u32_at(tail, so + 4 * r).ok_or("IMS: short strip offsets")? as usize;
        let cnt = if t279 == 3 { u16_at(tail, sc + 2 * r).ok_or("IMS: short counts")? as usize } else {
            u32_at(tail, sc + 4 * r).ok_or("IMS: short counts")? as usize
        };
        if off != IMS_DATA_AT + r * IMS_N || cnt != IMS_N {
            return Err(format!("IMS: strip {r} at {off} ({cnt} bytes), the reader expects one row in order"));
        }
    }
    // the georeference: 1 km pixels, the corner tie point, polar stereographic (15) at 60 N, 80 W
    let dbl = |tag: u16, n: usize| -> Result<Vec<f64>, String> {
        let &(t, c, o, _) = tags.get(&tag).ok_or(format!("IMS: no tag {tag}"))?;
        if t != 12 || c < n {
            return Err(format!("IMS: tag {tag} is not {n} doubles"));
        }
        let i = at(o as usize)?;
        (0..n).map(|k| f64_at(tail, i + 8 * k).ok_or(format!("IMS: short tag {tag}"))).collect()
    };
    let scale = dbl(33550, 2)?;
    let tie = dbl(33922, 6)?;
    if scale != [IMS_PX_M, IMS_PX_M] || tie[3] != -IMS_ORIGIN_M || tie[4] != IMS_ORIGIN_M || tie[0] != 0.0 || tie[1] != 0.0 {
        return Err(format!("IMS: pixel scale {scale:?} / tie point {tie:?} changed"));
    }
    let &(tk, ck, ok, _) = tags.get(&34735).ok_or("IMS: no GeoKeyDirectory")?;
    if tk != 3 || ck < 4 {
        return Err("IMS: bad GeoKeyDirectory".into());
    }
    let gk = at(ok as usize)?;
    let nkeys = u16_at(tail, gk + 6).ok_or("IMS: short geo keys")? as usize;
    let mut keys = std::collections::BTreeMap::new();
    for k in 0..nkeys {
        let e = gk + 8 + 8 * k;
        keys.insert(u16_at(tail, e).ok_or("IMS: short geo key")?,
                    (u16_at(tail, e + 2).ok_or("IMS")?, u16_at(tail, e + 6).ok_or("IMS")?));
    }
    let gd = match tags.get(&34736) {
        Some(&(_, c, _, _)) => dbl(34736, c.min(64))?,
        None => Vec::new(),
    };
    let key_dbl = |key: u16| -> Option<f64> {
        let &(loc, idx) = keys.get(&key)?;
        if loc == 34736 { gd.get(idx as usize).copied() } else { None }
    };
    let ct = keys.get(&3075).map(|&(loc, v)| if loc == 0 { v } else { 0 });
    if ct != Some(15) || key_dbl(3081) != Some(IMS_LAT_TS) || key_dbl(3095) != Some(IMS_LON0) {
        return Err(format!("IMS: projection changed (transform {ct:?}, origin latitude {:?}, pole meridian {:?})",
                           key_dbl(3081), key_dbl(3095)));
    }
    Ok(())
}

/// Windows of an IMS 1 km day file (the .tif.gz bytes as downloaded), each (r0, r1, c0, c1) inclusive, in one pass
/// through the stream (the file is inflated once, only the windows kept: ~600 MB inflate, a few MB held). The file's
/// tags are checked after the read; any layout other than v1.3's is an error.
pub fn ims_windows(gz: &[u8], boxes: &[(usize, usize, usize, usize)]) -> Result<Vec<ImsWindow>, String> {
    for &(r0, r1, c0, c1) in boxes {
        if r0 > r1 || c0 > c1 || r1 >= IMS_N || c1 >= IMS_N {
            return Err(format!("IMS: bad window {r0}..{r1} x {c0}..{c1}"));
        }
    }
    let mut out: Vec<ImsWindow> = boxes.iter().map(|&(r0, r1, c0, c1)| ImsWindow {
        r0, c0, rows: r1 - r0 + 1, cols: c1 - c0 + 1, v: Vec::with_capacity((r1 - r0 + 1) * (c1 - c0 + 1)),
    }).collect();
    let mut z = flate2::read::MultiGzDecoder::new(gz);
    let mut head = [0u8; IMS_DATA_AT];
    z.read_exact(&mut head).map_err(|e| format!("IMS: header: {e}"))?;
    if &head[0..4] != b"II*\0" {
        return Err("IMS: not a little-endian TIFF".into());
    }
    let ifd = u32::from_le_bytes([head[4], head[5], head[6], head[7]]) as usize;
    let data_end = IMS_DATA_AT + IMS_N * IMS_N;
    if ifd < data_end {
        return Err(format!("IMS: the IFD at {ifd} lies inside the pixels"));
    }
    let last = boxes.iter().map(|b| b.1).max().unwrap_or(0);
    let mut row = vec![0u8; IMS_N];
    for r in 0..IMS_N {
        z.read_exact(&mut row).map_err(|e| format!("IMS: row {r}: {e}"))?;
        if r <= last {
            for (w, &(r0, r1, c0, c1)) in out.iter_mut().zip(boxes) {
                if r >= r0 && r <= r1 {
                    w.v.extend_from_slice(&row[c0..=c1]);
                }
            }
        }
    }
    let mut tail = Vec::new();
    z.read_to_end(&mut tail).map_err(|e| format!("IMS: tags: {e}"))?;
    ims_check_tags(&tail, data_end, ifd)?;
    Ok(out)
}

// ---------------------------------------------------------------------------------------------------------------
// GIBS NDSI tiles

/// GIBS's EPSG:4326 "500m" matrix set at level 7: tiles of 512 px, 2.25 degrees, from (90 N, 180 W).
pub const GIBS_TILE_PX: usize = 512;
pub const GIBS_L7_DEG: f64 = 2.25;
/// NDSI x 100 from which a clear pixel counts as snow-covered: NDSI 0.35, about half the pixel snow (fractional snow
/// cover = 1.45 NDSI - 0.01, Salomonson and Appel), as IMS's binary 1 km "snow-covered" [guess: P-A checks it].
pub const NDSI_SNOW: u8 = 35;

/// A GIBS NDSI data value -> observation class: 0..100 land seen clear (snow from NDSI_SNOW), flags from 200 none.
pub fn ndsi_obs(v: u8, snow_from: u8) -> u8 {
    if v <= 100 {
        if v >= snow_from { OBS_SNOW } else { OBS_BARE }
    } else {
        OBS_NONE
    }
}

/// The data values (0..255) of a GIBS paletted PNG tile: the palette's colours matched to the layer's colour map
/// (cmap_rgb 0xRRGGBB -> cmap_value), each pixel's value by its palette index. (width, height, values row-major).
/// A palette colour the map does not have, or a non-paletted PNG, is an error (a changed layer is refused).
pub fn gibs_values(png_bytes: &[u8], cmap_rgb: &[u32], cmap_value: &[u8]) -> Result<(usize, usize, Vec<u8>), String> {
    if cmap_rgb.len() != cmap_value.len() {
        return Err("GIBS: colour map arrays differ in length".into());
    }
    let mut map = std::collections::BTreeMap::new();
    for (&rgb, &v) in cmap_rgb.iter().zip(cmap_value) {
        if let Some(&old) = map.get(&rgb) {
            if old != v {
                return Err(format!("GIBS: colour {rgb:06x} maps to {old} and {v}"));
            }
        }
        map.insert(rgb, v);
    }
    let mut dec = png::Decoder::new(std::io::Cursor::new(png_bytes));
    dec.set_transformations(png::Transformations::IDENTITY);
    let mut reader = dec.read_info().map_err(|e| format!("GIBS PNG: {e}"))?;
    let info = reader.info();
    if info.color_type != png::ColorType::Indexed || info.bit_depth != png::BitDepth::Eight {
        return Err(format!("GIBS PNG: {:?} {:?}, the reader expects 8-bit paletted", info.color_type, info.bit_depth));
    }
    let pal = info.palette.as_ref().ok_or("GIBS PNG: no palette")?.to_vec();
    let (w, h) = (info.width as usize, info.height as usize);
    let mut lut = [255u8; 256];
    let mut known = [false; 256];
    for (i, c) in pal.chunks_exact(3).enumerate().take(256) {
        let rgb = (c[0] as u32) << 16 | (c[1] as u32) << 8 | c[2] as u32;
        if let Some(&v) = map.get(&rgb) {
            lut[i] = v;
            known[i] = true;
        }
    }
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let frame = reader.next_frame(&mut buf).map_err(|e| format!("GIBS PNG: {e}"))?;
    let stride = frame.line_size;
    let mut out = Vec::with_capacity(w * h);
    for r in 0..h {
        for &ix in &buf[r * stride..r * stride + w] {
            if !known[ix as usize] {
                return Err(format!("GIBS PNG: palette entry {ix} is not in the colour map"));
            }
            out.push(lut[ix as usize]);
        }
    }
    Ok((w, h, out))
}

// ---------------------------------------------------------------------------------------------------------------
// Our DEM under each observation pixel

/// One observation pixel's ground from our DEM: mean height (m), aspect class (-1 facing the pole, 0 flat or facing
/// east / west, +1 facing the equator: snow.rs Site.aspect), the mean position of its DEM samples (degrees x 1e5) and
/// how many there were.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct PixGround {
    pub z: i32,
    pub aspect: i8,
    pub lat_e5: i32,
    pub lon_e5: i32,
    pub n: u32,
}

/// A slope below this (tan, about 6 degrees) is flat for the aspect class.
pub const ASPECT_FLAT_TAN: f64 = 0.10;
/// The downslope direction within 60 degrees of the pole (or the equator) for that class.
pub const ASPECT_COS: f64 = 0.5;
const M_PER_DEG_LAT: f64 = 110_574.0;
const M_PER_DEG_LON_EQ: f64 = 111_320.0;

/// The aspect class of a mean gradient (dz/dx east, dz/dy north, m/m) at a latitude.
pub fn aspect_class(gx: f64, gy: f64, lat: f64) -> i8 {
    let s = gx.dhypot(gy);
    if !(s >= ASPECT_FLAT_TAN) {
        return 0;
    }
    // the downslope direction's poleward component (north in the north, south in the south)
    let pole = if lat >= 0.0 { -gy / s } else { gy / s };
    if pole >= ASPECT_COS {
        -1
    } else if pole <= -ASPECT_COS {
        1
    } else {
        0
    }
}

/// The ground of each observation pixel from a DEM grid (heights m, row-major nrows x ncols, row 0 at `north`, column 0
/// at `west`, `res` degrees a sample; NaN is no data): every DEM sample (but the grid's outer ring) goes to the pixel
/// `pixel_of(lat, lon)` gives (an index below npix, or None), with its central-difference gradient. Sums in sample
/// order, f64: deterministic.
pub fn pixel_ground<F: Fn(f64, f64) -> Option<usize>>(dem: &[f32], nrows: usize, ncols: usize, north: f64, west: f64,
                                                      res: f64, npix: usize, pixel_of: F) -> Vec<PixGround> {
    let mut sz = vec![0f64; npix];
    let mut sgx = vec![0f64; npix];
    let mut sgy = vec![0f64; npix];
    let mut sla = vec![0f64; npix];
    let mut slo = vec![0f64; npix];
    let mut cnt = vec![0u32; npix];
    let dy = res * M_PER_DEG_LAT;
    for i in 1..nrows.saturating_sub(1) {
        let lat = north - (i as f64 + 0.5) * res;
        let dx = res * M_PER_DEG_LON_EQ * lat.to_radians().dcos();
        for j in 1..ncols.saturating_sub(1) {
            let z = dem[i * ncols + j];
            if !z.is_finite() {
                continue;
            }
            let lon = west + (j as f64 + 0.5) * res;
            let Some(p) = pixel_of(lat, lon) else { continue };
            if p >= npix {
                continue;
            }
            let (e, w) = (dem[i * ncols + j + 1], dem[i * ncols + j - 1]);
            let (n, s) = (dem[(i - 1) * ncols + j], dem[(i + 1) * ncols + j]);
            if e.is_finite() && w.is_finite() && n.is_finite() && s.is_finite() {
                sgx[p] += (e as f64 - w as f64) / (2.0 * dx);
                sgy[p] += (n as f64 - s as f64) / (2.0 * dy);
            }
            sz[p] += z as f64;
            sla[p] += lat;
            slo[p] += lon;
            cnt[p] += 1;
        }
    }
    (0..npix).map(|p| {
        let n = cnt[p];
        if n == 0 {
            return PixGround::default();
        }
        let k = n as f64;
        let lat = sla[p] / k;
        PixGround { z: (sz[p] / k).round() as i32, aspect: aspect_class(sgx[p] / k, sgy[p] / k, lat),
                    lat_e5: (lat * 1e5).round() as i32, lon_e5: (slo[p] / k * 1e5).round() as i32, n }
    }).collect()
}

// ---------------------------------------------------------------------------------------------------------------
// Bands and the observed snowline

/// The absolute band grid (S-2): row k at k x BAND_M metres, the band of a height the row nearest it.
pub const BAND_M: i32 = 150;

/// The band row (k) of a height: the nearest multiple of BAND_M.
pub fn band_of(z: i32) -> i32 {
    (z + BAND_M / 2).div_euclid(BAND_M)
}

/// Observed counts per band row k0..k0+n: [bare, snow] facing the pole (with the flat ones), and facing the equator
/// (with the flat ones). Pixels without an observation or ground, or outside the rows, are not counted.
pub fn band_counts(ground: &[PixGround], obs: &[u8], k0: i32, n: usize) -> Vec<[u32; 4]> {
    let mut out = vec![[0u32; 4]; n];
    for (g, &o) in ground.iter().zip(obs) {
        if g.n == 0 || o == OBS_NONE {
            continue;
        }
        let k = band_of(g.z) - k0;
        if k < 0 || k as usize >= n {
            continue;
        }
        let s = (o == OBS_SNOW) as usize;
        let row = &mut out[k as usize];
        if g.aspect <= 0 {
            row[s] += 1;
        }
        if g.aspect >= 0 {
            row[2 + s] += 1;
        }
    }
    out
}

/// A cell's observed snowline for one aspect column of its band counts: the line (m, between rows: the lower edge of a
/// row, k x BAND_M - BAND_M / 2) that misclassifies the fewest pixels (snow below it, bare above it); among equally good
/// lines the middle one (the lower of two). (line m, misclassified, total). All snow: the line below the lowest row;
/// none: above the highest; no pixels: total 0.
pub fn snowline(counts: &[[u32; 4]], k0: i32, col: usize) -> (i32, u32, u32) {
    let n = counts.len();
    let bare: Vec<u32> = counts.iter().map(|r| r[2 * col]).collect();
    let snow: Vec<u32> = counts.iter().map(|r| r[2 * col + 1]).collect();
    let total: u32 = bare.iter().sum::<u32>() + snow.iter().sum::<u32>();
    // the line below row i (i = 0..=n): snow in rows < i are errors, bare in rows >= i are errors
    let mut errs = Vec::with_capacity(n + 1);
    let mut snow_below = 0u32;
    let mut bare_above: u32 = bare.iter().sum();
    for i in 0..=n {
        errs.push(snow_below + bare_above);
        if i < n {
            snow_below += snow[i];
            bare_above -= bare[i];
        }
    }
    let best = *errs.iter().min().unwrap_or(&0);
    let ties: Vec<usize> = (0..=n).filter(|&i| errs[i] == best).collect();
    let i = ties[(ties.len() - 1) / 2];
    ((k0 + i as i32) * BAND_M - BAND_M / 2, best, total)
}

/// The band counts of the pixels whose DEM samples' mean position lies in a cell (south <= lat < north, west <= lon
/// < east, degrees x 1e5).
pub fn cell_counts(ground: &[PixGround], obs: &[u8], cell: (i32, i32, i32, i32), k0: i32, n: usize) -> Vec<[u32; 4]> {
    let (s, w, nn, e) = cell;
    let (g, o): (Vec<PixGround>, Vec<u8>) = ground.iter().zip(obs)
        .filter(|(g, _)| g.n > 0 && g.lat_e5 >= s && g.lat_e5 < nn && g.lon_e5 >= w && g.lon_e5 < e)
        .map(|(g, &o)| (*g, o)).unzip();
    band_counts(&g, &o, k0, n)
}

#[cfg(not(snow_standalone))]
pub mod py {
    //! The tools' and the snow field's calls (tools/snowobs_probe.py; the server's snow field glue).
    use super::*;
    use numpy::ndarray::Array2;
    use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
    use pyo3::exceptions::PyValueError;
    use pyo3::prelude::*;

    type Ground<'py> = (Bound<'py, PyArray1<i32>>, Bound<'py, PyArray1<i8>>, Bound<'py, PyArray1<i32>>,
                        Bound<'py, PyArray1<i32>>, Bound<'py, PyArray1<u32>>);

    fn ground_out<'py>(py: Python<'py>, g: Vec<PixGround>) -> Ground<'py> {
        (g.iter().map(|p| p.z).collect::<Vec<_>>().into_pyarray(py),
         g.iter().map(|p| p.aspect).collect::<Vec<_>>().into_pyarray(py),
         g.iter().map(|p| p.lat_e5).collect::<Vec<_>>().into_pyarray(py),
         g.iter().map(|p| p.lon_e5).collect::<Vec<_>>().into_pyarray(py),
         g.iter().map(|p| p.n).collect::<Vec<_>>().into_pyarray(py))
    }

    fn ground_in(z: &PyReadonlyArray1<'_, i32>, aspect: &PyReadonlyArray1<'_, i8>, lat: &PyReadonlyArray1<'_, i32>,
                 lon: &PyReadonlyArray1<'_, i32>, n: &PyReadonlyArray1<'_, u32>) -> PyResult<Vec<PixGround>> {
        let (z, a, la, lo, n) = (z.as_slice()?, aspect.as_slice()?, lat.as_slice()?, lon.as_slice()?, n.as_slice()?);
        if a.len() != z.len() || la.len() != z.len() || lo.len() != z.len() || n.len() != z.len() {
            return Err(PyValueError::new_err("snowobs: ground arrays differ in length"));
        }
        Ok((0..z.len()).map(|i| PixGround { z: z[i], aspect: a[i].signum(), lat_e5: la[i], lon_e5: lo[i], n: n[i] })
            .collect())
    }

    /// IMS 1 km windows of a day file (the .tif.gz bytes): boxes [(r0, r1, c0, c1)] inclusive -> one uint8 array
    /// (rows, cols) each. ValueError on a file whose layout or tags differ from v1.3's.
    #[pyfunction]
    pub fn snowobs_ims_windows<'py>(py: Python<'py>, gz: &[u8], boxes: Vec<(usize, usize, usize, usize)>)
                                    -> PyResult<Vec<Bound<'py, PyArray2<u8>>>> {
        let w = py.detach(|| ims_windows(gz, &boxes)).map_err(PyValueError::new_err)?;
        w.into_iter().map(|w| {
            Array2::from_shape_vec((w.rows, w.cols), w.v).map(|a| a.into_pyarray(py))
                .map_err(|e| PyValueError::new_err(e.to_string()))
        }).collect()
    }

    /// The IMS pixels (row, column) of points; -1 where none (the south, off the grid).
    #[pyfunction]
    pub fn snowobs_ims_pixels<'py>(py: Python<'py>, lat: PyReadonlyArray1<'py, f64>, lon: PyReadonlyArray1<'py, f64>)
                                   -> PyResult<(Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>)> {
        let (la, lo) = (lat.as_slice()?, lon.as_slice()?);
        if la.len() != lo.len() {
            return Err(PyValueError::new_err("snowobs_ims_pixels: lat and lon differ in length"));
        }
        let (mut r, mut c) = (Vec::with_capacity(la.len()), Vec::with_capacity(la.len()));
        for (&a, &b) in la.iter().zip(lo) {
            match ims_pixel(a, b) {
                Some((y, x)) => {
                    r.push(y as i64);
                    c.push(x as i64);
                }
                None => {
                    r.push(-1);
                    c.push(-1);
                }
            }
        }
        Ok((r.into_pyarray(py), c.into_pyarray(py)))
    }

    /// The data values of a GIBS paletted PNG tile by the layer's colour map (rgb 0xRRGGBB -> value): uint8 (h, w).
    #[pyfunction]
    pub fn snowobs_gibs_values<'py>(py: Python<'py>, png: &[u8], cmap_rgb: Vec<u32>, cmap_value: Vec<u8>)
                                    -> PyResult<Bound<'py, PyArray2<u8>>> {
        let (w, h, v) = py.detach(|| gibs_values(png, &cmap_rgb, &cmap_value)).map_err(PyValueError::new_err)?;
        Ok(Array2::from_shape_vec((h, w), v).map_err(|e| PyValueError::new_err(e.to_string()))?.into_pyarray(py))
    }

    /// Observation classes (OBS_NONE / OBS_BARE / OBS_SNOW) of IMS values ("ims") or GIBS NDSI values ("ndsi", snow
    /// from `snow_from`, default NDSI_SNOW).
    #[pyfunction]
    #[pyo3(signature = (values, kind, snow_from=NDSI_SNOW))]
    pub fn snowobs_classes<'py>(py: Python<'py>, values: PyReadonlyArray1<'py, u8>, kind: &str, snow_from: u8)
                                -> PyResult<Bound<'py, PyArray1<u8>>> {
        let v = values.as_slice()?;
        let out: Vec<u8> = match kind {
            "ims" => v.iter().map(|&x| ims_obs(x)).collect(),
            "ndsi" => v.iter().map(|&x| ndsi_obs(x, snow_from)).collect(),
            _ => return Err(PyValueError::new_err(format!("snowobs_classes: unknown kind {kind}"))),
        };
        Ok(out.into_pyarray(py))
    }

    /// The ground under an IMS window's pixels (rows r0.., columns c0.., rows x cols, row-major) from a DEM grid
    /// (float32 heights, row 0 at `north`, column 0 at `west`, `res` degrees): (z m, aspect class, lat x 1e5, lon x 1e5,
    /// DEM samples) per pixel.
    #[pyfunction]
    #[allow(clippy::too_many_arguments)]
    pub fn snowobs_ground_ims<'py>(py: Python<'py>, dem: PyReadonlyArray2<'py, f32>, north: f64, west: f64, res: f64,
                                   r0: usize, c0: usize, rows: usize, cols: usize) -> PyResult<Ground<'py>> {
        let a = dem.as_array();
        let (nr, nc) = a.dim();
        let d: Vec<f32> = a.iter().copied().collect();
        let g = py.detach(|| pixel_ground(&d, nr, nc, north, west, res, rows * cols, |la, lo| {
            let (r, c) = ims_pixel(la, lo)?;
            if r < r0 || c < c0 || r >= r0 + rows || c >= c0 + cols {
                return None;
            }
            Some((r - r0) * cols + (c - c0))
        }));
        Ok(ground_out(py, g))
    }

    /// The ground under a regular lat / lon grid's pixels (a GIBS window: row 0 at g_north, column 0 at g_west, g_res
    /// degrees, rows x cols) from a DEM grid: as snowobs_ground_ims.
    #[pyfunction]
    #[allow(clippy::too_many_arguments)]
    pub fn snowobs_ground_grid<'py>(py: Python<'py>, dem: PyReadonlyArray2<'py, f32>, north: f64, west: f64, res: f64,
                                    g_north: f64, g_west: f64, g_res: f64, rows: usize, cols: usize)
                                    -> PyResult<Ground<'py>> {
        let a = dem.as_array();
        let (nr, nc) = a.dim();
        let d: Vec<f32> = a.iter().copied().collect();
        let g = py.detach(|| pixel_ground(&d, nr, nc, north, west, res, rows * cols, |la, lo| {
            let r = ((g_north - la) / g_res).floor();
            let c = ((lo - g_west) / g_res).floor();
            if r < 0.0 || c < 0.0 || r >= rows as f64 || c >= cols as f64 {
                return None;
            }
            Some(r as usize * cols + c as usize)
        }));
        Ok(ground_out(py, g))
    }

    /// The band counts (n x 4 int64: pole bare, pole snow, equator bare, equator snow; flat pixels in both) of the
    /// pixels in a cell (south, west, north, east degrees x 1e5) for rows k0..k0+n.
    #[pyfunction]
    #[allow(clippy::too_many_arguments)]
    pub fn snowobs_cell_counts<'py>(py: Python<'py>, z: PyReadonlyArray1<'py, i32>, aspect: PyReadonlyArray1<'py, i8>,
                                    lat_e5: PyReadonlyArray1<'py, i32>, lon_e5: PyReadonlyArray1<'py, i32>,
                                    n_dem: PyReadonlyArray1<'py, u32>, obs: PyReadonlyArray1<'py, u8>,
                                    cell: (i32, i32, i32, i32), k0: i32, n: usize)
                                    -> PyResult<Bound<'py, PyArray2<i64>>> {
        let g = ground_in(&z, &aspect, &lat_e5, &lon_e5, &n_dem)?;
        let o = obs.as_slice()?;
        if o.len() != g.len() {
            return Err(PyValueError::new_err("snowobs_cell_counts: obs and ground differ in length"));
        }
        let c = cell_counts(&g, o, cell, k0, n);
        let flat: Vec<i64> = c.iter().flat_map(|r| r.iter().map(|&v| v as i64)).collect();
        Ok(Array2::from_shape_vec((c.len(), 4), flat).map_err(|e| PyValueError::new_err(e.to_string()))?
            .into_pyarray(py))
    }

    /// A cell's observed snowline for a column (0 pole, 1 equator) of its band counts from row k0: (line m,
    /// misclassified, total).
    #[pyfunction]
    pub fn snowobs_snowline(counts: PyReadonlyArray2<'_, i64>, k0: i32, col: usize) -> PyResult<(i32, u32, u32)> {
        let a = counts.as_array();
        if a.ncols() != 4 {
            return Err(PyValueError::new_err("snowobs_snowline: counts need 4 columns"));
        }
        let c: Vec<[u32; 4]> = a.rows().into_iter()
            .map(|r| [r[0].max(0) as u32, r[1].max(0) as u32, r[2].max(0) as u32, r[3].max(0) as u32]).collect();
        Ok(snowline(&c, k0, col.min(1)))
    }

    pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(snowobs_ims_windows, m)?)?;
        m.add_function(wrap_pyfunction!(snowobs_ims_pixels, m)?)?;
        m.add_function(wrap_pyfunction!(snowobs_gibs_values, m)?)?;
        m.add_function(wrap_pyfunction!(snowobs_classes, m)?)?;
        m.add_function(wrap_pyfunction!(snowobs_ground_ims, m)?)?;
        m.add_function(wrap_pyfunction!(snowobs_ground_grid, m)?)?;
        m.add_function(wrap_pyfunction!(snowobs_cell_counts, m)?)?;
        m.add_function(wrap_pyfunction!(snowobs_snowline, m)?)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn projection_matches_known_pixels() {
        // (checked against the 2026-11-26 file: Kansas land, the Atlantic sea, Lake Superior water)
        let (x, y) = ims_xy(90.0, 0.0);
        assert!(x.abs() < 1e-6 && y.abs() < 1e-6);
        assert_eq!(ims_pixel(90.0, 0.0), Some((12288, 12288)));
        // the 80 W meridian runs straight down from the pole: x = 0
        let (x, y) = ims_xy(45.0, -80.0);
        assert!(x.abs() < 1e-6 && y < 0.0);
        assert_eq!(ims_pixel(-10.0, 0.0), None);
        // Zermatt (Snyder's formulas, by hand): 4,933,212 E, -193,826 N
        let (x, y) = ims_xy(45.0, 7.75);
        assert!((x - 4_933_212.0).abs() < 2.0 && (y + 193_826.0).abs() < 2.0, "{x} {y}");
    }

    fn tiny_ims(rows: &[(usize, u8)]) -> Vec<u8> {
        // a real-layout IMS file is 600 MB inflated: the test builds its stream with the same layout lazily
        use std::io::Write;
        let mut gz = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
        let data_end = IMS_DATA_AT + IMS_N * IMS_N;
        gz.write_all(b"II*\0").unwrap();
        gz.write_all(&(data_end as u32).to_le_bytes()).unwrap();
        let mut row = vec![1u8; IMS_N];
        for r in 0..IMS_N {
            let v = rows.iter().find(|x| x.0 == r).map(|x| x.1).unwrap_or(1);
            row.iter_mut().for_each(|b| *b = v);
            row[r % IMS_N] = 4;
            gz.write_all(&row).unwrap();
        }
        gz.write_all(&ims_tail(data_end)).unwrap();
        gz.finish().unwrap()
    }

    fn ims_tail(base: usize) -> Vec<u8> {
        // IFD at base: 14 entries, then the arrays
        let entries = 14usize;
        let ifd_len = 2 + 12 * entries + 4;
        let so = base + ifd_len;
        let sc = so + 4 * IMS_N;
        let ps = sc + 4 * IMS_N;
        let tp = ps + 24;
        let gk = tp + 48;
        let gd = gk + 8 * 4;
        let mut t = Vec::new();
        t.extend_from_slice(&(entries as u16).to_le_bytes());
        let mut ent = |tag: u16, typ: u16, cnt: u32, val: u32| {
            t.extend_from_slice(&tag.to_le_bytes());
            t.extend_from_slice(&typ.to_le_bytes());
            t.extend_from_slice(&cnt.to_le_bytes());
            t.extend_from_slice(&val.to_le_bytes());
        };
        ent(256, 3, 1, IMS_N as u32);
        ent(257, 3, 1, IMS_N as u32);
        ent(258, 3, 1, 8);
        ent(259, 3, 1, 1);
        ent(262, 3, 1, 1);
        ent(273, 4, IMS_N as u32, so as u32);
        ent(277, 3, 1, 1);
        ent(278, 3, 1, 1);
        ent(279, 4, IMS_N as u32, sc as u32);
        ent(284, 3, 1, 1);
        ent(33550, 12, 3, ps as u32);
        ent(33922, 12, 6, tp as u32);
        ent(34735, 3, 16, gk as u32);
        ent(34736, 12, 2, gd as u32);
        t.extend_from_slice(&0u32.to_le_bytes());
        for r in 0..IMS_N {
            t.extend_from_slice(&((IMS_DATA_AT + r * IMS_N) as u32).to_le_bytes());
        }
        for _ in 0..IMS_N {
            t.extend_from_slice(&(IMS_N as u32).to_le_bytes());
        }
        for v in [1000.0f64, 1000.0, 0.0, 0.0, 0.0, 0.0, -12_288_000.0, 12_288_000.0, 0.0] {
            t.extend_from_slice(&v.to_le_bytes());
        }
        for v in [1u16, 1, 0, 3, 3075, 0, 1, 15, 3081, 34736, 1, 0, 3095, 34736, 1, 1] {
            t.extend_from_slice(&v.to_le_bytes());
        }
        for v in [60.0f64, -80.0] {
            t.extend_from_slice(&v.to_le_bytes());
        }
        assert_eq!(t.len(), gd + 16 - base);
        t
    }

    #[test]
    #[ignore] // (600 MB through the deflater: run with --ignored)
    fn ims_reader_reads_windows_and_checks_tags() {
        let gz = tiny_ims(&[(100, 2), (101, 4)]);
        let w = ims_windows(&gz, &[(99, 101, 0, 3), (5000, 5001, 5000, 5002)]).unwrap();
        assert_eq!(w[0].v, vec![1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4]);
        assert_eq!(w[0].at(101, 3), Some(4));
        assert_eq!(w[1].v, vec![4, 1, 1, 1, 4, 1]);
        // a layout the reader does not know: refused
        let mut bad = flate2::read::MultiGzDecoder::new(&gz[..]);
        let mut all = Vec::new();
        bad.read_to_end(&mut all).unwrap();
        let e = IMS_DATA_AT + IMS_N * IMS_N + 2 + 12 * 2 + 8; // tag 258's value
        all[e] = 16;
        use std::io::Write;
        let mut g2 = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::fast());
        g2.write_all(&all).unwrap();
        assert!(ims_windows(&g2.finish().unwrap(), &[(0, 0, 0, 0)]).is_err());
    }

    #[test]
    fn gibs_palette_by_colour() {
        // a 3 x 2 paletted PNG: palette 0 = (0,255,0) NDSI 0, 1 = (240,240,180) NDSI 53, 2 = (0,191,255) cloud 250
        let mut png_bytes = Vec::new();
        {
            let mut enc = png::Encoder::new(&mut png_bytes, 3, 2);
            enc.set_color(png::ColorType::Indexed);
            enc.set_depth(png::BitDepth::Eight);
            enc.set_palette(vec![0, 255, 0, 240, 240, 180, 0, 191, 255]);
            let mut w = enc.write_header().unwrap();
            w.write_image_data(&[0, 1, 2, 2, 1, 0]).unwrap();
        }
        let rgb = [0x00ff00u32, 0xf0f0b4, 0x00bfff];
        let val = [0u8, 53, 250];
        let (w, h, v) = gibs_values(&png_bytes, &rgb, &val).unwrap();
        assert_eq!((w, h, v.clone()), (3, 2, vec![0, 53, 250, 250, 53, 0]));
        let o: Vec<u8> = v.iter().map(|&x| ndsi_obs(x, NDSI_SNOW)).collect();
        assert_eq!(o, vec![OBS_BARE, OBS_SNOW, OBS_NONE, OBS_NONE, OBS_SNOW, OBS_BARE]);
        // a palette colour not in the map: refused
        assert!(gibs_values(&png_bytes, &rgb[..2], &val[..2]).is_err());
    }

    #[test]
    fn aspect_classes() {
        // the north: a slope descending northward faces the pole
        assert_eq!(aspect_class(0.0, -0.3, 45.0), -1);
        assert_eq!(aspect_class(0.0, 0.3, 45.0), 1);
        assert_eq!(aspect_class(0.3, 0.0, 45.0), 0);
        assert_eq!(aspect_class(0.0, -0.05, 45.0), 0);
        // the south: descending southward faces the pole
        assert_eq!(aspect_class(0.0, 0.3, -45.0), -1);
    }

    #[test]
    fn ground_under_pixels() {
        // a 6 x 6 DEM ramp rising 30 m a sample to the south (a north-facing slope), pixels = left / right half
        let (n, res) = (6usize, 0.001);
        let dem: Vec<f32> = (0..n * n).map(|k| (1000 + 30 * (k / n)) as f32).collect();
        let g = pixel_ground(&dem, n, n, 46.0, 7.0, res, 2, |_la, lo| Some(if lo < 7.003 { 0 } else { 1 }));
        assert_eq!(g[0].n + g[1].n, 16);
        assert_eq!(g[0].aspect, -1);
        assert_eq!(g[0].z, 1075);
    }

    #[test]
    fn bands_and_snowline() {
        assert_eq!(band_of(0), 0);
        assert_eq!(band_of(74), 0);
        assert_eq!(band_of(75), 1);
        assert_eq!(band_of(-76), -1);
        // a clean line between 2,175 and 2,325 m (rows 14, 15 bare, 16.. snow), the pole side 150 m lower
        let mut ground = Vec::new();
        let mut obs = Vec::new();
        for z in (1900..3000).step_by(30) {
            for aspect in [-1i8, 1] {
                ground.push(PixGround { z, aspect, n: 9, ..Default::default() });
                let line = if aspect < 0 { 2175 } else { 2325 };
                obs.push(if z >= line { OBS_SNOW } else { OBS_BARE });
            }
        }
        let k0 = 12;
        let c = band_counts(&ground, &obs, k0, 10);
        let (lp, ep, tp) = snowline(&c, k0, 0);
        let (le, ee, te) = snowline(&c, k0, 1);
        assert_eq!((lp, ep), (2175, 0));
        assert_eq!((le, ee), (2325, 0));
        assert!(tp > 0 && te > 0);
        // all snow: the line below the lowest row; nothing: total 0
        let all = vec![[0, 5, 0, 5]; 3];
        assert_eq!(snowline(&all, 10, 0), (1425, 0, 15));
        assert_eq!(snowline(&[[0u32; 4]; 3], 10, 0).2, 0);
    }
}
