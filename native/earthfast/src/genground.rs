//! Generated ground (server/genground.py, BNE_GEN_GROUND; the user, 2026-11-15: "What if instead of trying to use the
//! highest res pics, we use them as a base and generate the extra detail?"): a block's 1 m base colour made from the
//! 10 m Sentinel-2 photo and the map's 1 m regions (land cover, land use, canopy heights, water, roads, buildings)
//! instead of a regional 1 m aerial photo, whose hosts were the world's bottleneck (their per-host limits queued ~70 %
//! of their requests in the telemetry of 2026-11-15) and which many countries do not have at all.
//!
//! - gg_fill_polys: polygons (texel coordinates) filled into a u32 raster at the texel centres, even-odd within a
//!   polygon, later polygons over earlier ones.
//! - gg_regions: each texel's kind of ground and region from the land cover's kind, the land-use and water polygons,
//!   the canopy heights and the road / building cover (the rules at gg_regions).
//! - gen_ground: each texel's colour. Near a region's border, the mean Sentinel-2 colour of the region's own texels
//!   that lie far enough inside it that the 10 m photo's blur has mixed nothing else in (a forest edge, a field's
//!   edge, a lawn beside a road get the 1 m border of the vectors, not the photo's 10 m smear); inside, the photo
//!   itself (its own colour variation is real). Then the kind's fine detail (value noise at a few scales; trees:
//!   crowns shaded from the canopy heights, or cellular crowns where there are none), in world-locked coordinates so
//!   neighbouring blocks agree along their seam. AI super-resolution was ruled out (it makes things up, and its
//!   output differs between GPUs).
//!
//! Deterministic: an integer distance transform, integer sums, f32 / f64 arithmetic and sqrt only (no libm), each
//! output row from its inputs alone (the same bits on any number of threads).

use numpy::{IntoPyArray, PyArray2, PyArray3, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3, PyReadwriteArray2};
use pyo3::prelude::*;

use crate::groundpaint::noise_hash;
use crate::par;

// Kinds of ground (server/genground.py KINDS: the same numbers)
pub const NONE: u8 = 0;
pub const TREE: u8 = 1;
pub const SHRUB: u8 = 2;
pub const GRASS: u8 = 3;
pub const CROP: u8 = 4;
pub const BUILT: u8 = 5;
pub const BARE: u8 = 6;
#[allow(dead_code)]
pub const SNOW: u8 = 7;
pub const WATER: u8 = 8;
pub const WETLAND: u8 = 9;
pub const LAWN: u8 = 10;
pub const PAVED: u8 = 11;
pub const ROOF: u8 = 12;
const NKINDS: usize = 13;

// cover bits (gg_regions)
const COVER_WATER: u8 = 1;
const COVER_PAVED: u8 = 2;
const COVER_ROOF: u8 = 4;

const CANOPY_NODATA: u8 = 255;
const TREE_MIN_M: u8 = 3;           // canopy at least this tall: a tree (Meta / WRI CHM, 1.2 m pixels)
const SHRUB_MIN_M: u8 = 1;          // ... at least this: a shrub where the land cover says trees

// gen_ground's borders (m)
const PURE_M: f64 = 8.0;            // a texel this far inside its region: its photo colour is the region's own
const SEMI_M: f64 = 3.0;            // ... failing those (a dense town: hardly anything is 8 m from a border), this far
const BLEND_M: f64 = 10.0;          // the region's mean fades into the photo itself from the border to this far in
const NEAR_R_M: f64 = 16.0;         // the region's pure texels are looked for this far round (every NEAR_STEP_M)
const NEAR_STEP_M: f64 = 2.0;
const FAR_R_M: f64 = 48.0;          // ... then this far (every FAR_STEP_M), then the kind's in that window
const FAR_STEP_M: f64 = 4.0;
const LATTICE: usize = 4;           // texels between the points the regions' colours are found at (gen_ground)

/// Fallback colours (sRGB) of a kind without any texel of its own nearby to take the colour from (region_mean), a
/// quarter of it mixed into the photo.
const PALETTE: [[f32; 3]; NKINDS] = [
    [0.0, 0.0, 0.0],        // NONE: the photo
    [46.0, 60.0, 36.0],     // TREE
    [86.0, 92.0, 58.0],     // SHRUB
    [98.0, 112.0, 66.0],    // GRASS
    [124.0, 118.0, 84.0],   // CROP
    [120.0, 116.0, 106.0],  // BUILT
    [150.0, 136.0, 112.0],  // BARE
    [228.0, 232.0, 236.0],  // SNOW
    [40.0, 58.0, 70.0],     // WATER
    [84.0, 92.0, 66.0],     // WETLAND
    [86.0, 112.0, 60.0],    // LAWN
    [0.0, 0.0, 0.0],        // PAVED: the photo (the roads are painted over it)
    [0.0, 0.0, 0.0],        // ROOF: the photo (the roofs are filled in after)
];

/// The fine detail of each kind: (feature size m, luminance amplitude) of up to three octaves of value noise, the
/// amplitude of a yellow / green shift (vegetation's patchiness, at 24 m), and the crowns (cell m, amplitude) drawn
/// where there are no canopy heights (0: none).
struct Detail {
    octaves: [(f64, f32); 3],
    tint: f32,
    crowns: (f64, f32),
}

const DETAIL: [Detail; NKINDS] = [
    Detail { octaves: [(0.0, 0.0), (0.0, 0.0), (0.0, 0.0)], tint: 0.0, crowns: (0.0, 0.0) },          // NONE
    Detail { octaves: [(9.0, 0.05), (2.2, 0.05), (0.0, 0.0)], tint: 4.0, crowns: (5.0, 0.30) },       // TREE
    Detail { octaves: [(7.0, 0.06), (1.8, 0.06), (0.0, 0.0)], tint: 5.0, crowns: (2.6, 0.20) },       // SHRUB
    Detail { octaves: [(14.0, 0.06), (3.5, 0.045), (1.3, 0.035)], tint: 6.0, crowns: (0.0, 0.0) },    // GRASS
    Detail { octaves: [(26.0, 0.045), (6.0, 0.03), (1.4, 0.03)], tint: 5.0, crowns: (0.0, 0.0) },     // CROP
    Detail { octaves: [(7.0, 0.07), (2.4, 0.05), (1.1, 0.04)], tint: 2.0, crowns: (0.0, 0.0) },       // BUILT
    Detail { octaves: [(16.0, 0.07), (4.0, 0.06), (1.2, 0.05)], tint: 0.0, crowns: (0.0, 0.0) },      // BARE
    Detail { octaves: [(12.0, 0.025), (2.5, 0.015), (0.0, 0.0)], tint: 0.0, crowns: (0.0, 0.0) },     // SNOW
    Detail { octaves: [(30.0, 0.02), (0.0, 0.0), (0.0, 0.0)], tint: 0.0, crowns: (0.0, 0.0) },        // WATER
    Detail { octaves: [(9.0, 0.07), (2.2, 0.05), (0.0, 0.0)], tint: 5.0, crowns: (0.0, 0.0) },        // WETLAND
    Detail { octaves: [(9.0, 0.04), (2.0, 0.03), (1.1, 0.025)], tint: 4.0, crowns: (0.0, 0.0) },      // LAWN
    Detail { octaves: [(6.0, 0.02), (1.2, 0.025), (0.0, 0.0)], tint: 0.0, crowns: (0.0, 0.0) },       // PAVED
    Detail { octaves: [(0.0, 0.0), (0.0, 0.0), (0.0, 0.0)], tint: 0.0, crowns: (0.0, 0.0) },          // ROOF
];

// the crowns' sun (unit vector east, north, up: from the north-west at 45 degrees, as the photos' shadows and
// groundphoto.rs) and how strongly the canopy's slope shades them
const SUN: (f32, f32, f32) = (-0.5, 0.5, 0.70710677);
const CANOPY_SHADE: f32 = 0.5;

/// Smooth value noise in [-1, 1] at (x, y) lattice units (groundpaint::value_noises' formula).
#[inline]
fn vnoise(x: f64, y: f64, seed: u64) -> f64 {
    let (x0, y0) = (x.floor(), y.floor());
    let (fx, fy) = (x - x0, y - y0);
    let fx = fx * fx * (3.0 - 2.0 * fx);
    let fy = fy * fy * (3.0 - 2.0 * fy);
    let (ix, iy) = (x0 as i64, y0 as i64);
    let a = noise_hash(ix, iy, seed);
    let b = noise_hash(ix + 1, iy, seed);
    let c = noise_hash(ix, iy + 1, seed);
    let d = noise_hash(ix + 1, iy + 1, seed);
    let top = (b - a) * fx + a;
    let bot = (d - c) * fx + c;
    (bot - top) * fy + top
}

/// Cellular crowns: a dome (0 at a crown's rim and between crowns, 1 at its top) round one jittered point per cell of
/// `cell` metres, the nearest point's.
#[inline]
fn crown(x: f64, y: f64, cell: f64, seed: u64) -> f32 {
    let (gx, gy) = (x / cell, y / cell);
    let (cx, cy) = (gx.floor() as i64, gy.floor() as i64);
    let mut best = f64::INFINITY;
    for dy in -1..=1i64 {
        for dx in -1..=1i64 {
            let (ix, iy) = (cx + dx, cy + dy);
            let px = ix as f64 + 0.5 + 0.4 * noise_hash(ix, iy, seed);
            let py = iy as f64 + 0.5 + 0.4 * noise_hash(ix, iy, seed + 1);
            let (ddx, ddy) = (gx - px, gy - py);
            let d2 = ddx * ddx + ddy * ddy;
            if d2 < best {
                best = d2;
            }
        }
    }
    // crown radius 0.62 cells: neighbouring crowns touch
    let r2 = 0.62 * 0.62;
    if best >= r2 { 0.0 } else { (1.0 - best / r2) as f32 }
}

/// Chamfer (3-4) distance of every texel to the nearest texel of another region, in thirds of a texel (a texel with a
/// 4-neighbour of another region: 3). Two passes, integer: the same everywhere.
fn border_distance(region: &[u32], n: usize) -> Vec<u32> {
    let big = u32::MAX / 4;
    let mut d = vec![big; n * n];
    for r in 0..n {
        for c in 0..n {
            let v = region[r * n + c];
            let edge = (c > 0 && region[r * n + c - 1] != v) || (c + 1 < n && region[r * n + c + 1] != v)
                || (r > 0 && region[(r - 1) * n + c] != v) || (r + 1 < n && region[(r + 1) * n + c] != v);
            if edge {
                d[r * n + c] = 3;
            }
        }
    }
    for r in 0..n {
        for c in 0..n {
            let mut v = d[r * n + c];
            if c > 0 { v = v.min(d[r * n + c - 1] + 3); }
            if r > 0 {
                v = v.min(d[(r - 1) * n + c] + 3);
                if c > 0 { v = v.min(d[(r - 1) * n + c - 1] + 4); }
                if c + 1 < n { v = v.min(d[(r - 1) * n + c + 1] + 4); }
            }
            d[r * n + c] = v;
        }
    }
    for r in (0..n).rev() {
        for c in (0..n).rev() {
            let mut v = d[r * n + c];
            if c + 1 < n { v = v.min(d[r * n + c + 1] + 3); }
            if r + 1 < n {
                v = v.min(d[(r + 1) * n + c] + 3);
                if c + 1 < n { v = v.min(d[(r + 1) * n + c + 1] + 4); }
                if c > 0 { v = v.min(d[(r + 1) * n + c - 1] + 4); }
            }
            d[r * n + c] = v;
        }
    }
    d
}

/// Polygons filled into out (h, w) u32 at the texel centres (col + 0.5, row + 0.5): xy (m, 2) texel coordinates
/// (col, row) of every ring's points, ring_off (rings + 1) the rings' point ranges, poly_off (polygons + 1) the
/// polygons' ring ranges, values (polygons) what each writes. Even-odd over a polygon's rings; later polygons over
/// earlier ones. Returns the texels written.
#[pyfunction]
pub fn gg_fill_polys(py: Python<'_>, mut out: PyReadwriteArray2<'_, u32>, xy: PyReadonlyArray2<'_, f64>,
                     ring_off: PyReadonlyArray1<'_, i64>, poly_off: PyReadonlyArray1<'_, i64>,
                     values: PyReadonlyArray1<'_, u32>) -> PyResult<usize> {
    let (xy, ring_off, poly_off, values) = (xy.as_array(), ring_off.as_array(), poly_off.as_array(), values.as_array());
    let np = values.len();
    if xy.ndim() != 2 || (xy.shape()[0] > 0 && xy.shape()[1] < 2) || poly_off.len() != np + 1 || ring_off.is_empty() {
        return Err(pyo3::exceptions::PyValueError::new_err("gg_fill_polys: bad shapes"));
    }
    let nr = ring_off.len() - 1;
    let npts = xy.shape()[0] as i64;
    for k in 0..nr {
        if ring_off[k] < 0 || ring_off[k + 1] < ring_off[k] || ring_off[k + 1] > npts {
            return Err(pyo3::exceptions::PyValueError::new_err("gg_fill_polys: bad ring offsets"));
        }
    }
    for k in 0..np {
        if poly_off[k] < 0 || poly_off[k + 1] < poly_off[k] || poly_off[k + 1] as usize > nr {
            return Err(pyo3::exceptions::PyValueError::new_err("gg_fill_polys: bad polygon offsets"));
        }
    }
    let mut arr = out.as_array_mut();
    let (h, w) = (arr.shape()[0], arr.shape()[1]);
    let xy = xy.to_owned();
    let ring_off = ring_off.to_owned();
    let poly_off = poly_off.to_owned();
    let values = values.to_owned();
    let mut grid = arr.to_owned();
    let written = py.detach(|| {
        let mut written = 0usize;
        let mut xs: Vec<f64> = Vec::new();
        for p in 0..np {
            let (r0, r1) = (poly_off[p] as usize, poly_off[p + 1] as usize);
            let (mut ymin, mut ymax) = (f64::INFINITY, f64::NEG_INFINITY);
            for ring in r0..r1 {
                for q in ring_off[ring] as usize..ring_off[ring + 1] as usize {
                    ymin = ymin.min(xy[[q, 1]]);
                    ymax = ymax.max(xy[[q, 1]]);
                }
            }
            if !(ymax >= ymin) {
                continue;
            }
            let ra = ((ymin - 0.5).ceil().max(0.0)) as usize;
            let rb = (ymax - 0.5).floor().min(h as f64 - 1.0);
            if rb < 0.0 {
                continue;
            }
            let rb = rb as usize;
            for row in ra..=rb.min(h.saturating_sub(1)) {
                let ty = row as f64 + 0.5;
                xs.clear();
                for ring in r0..r1 {
                    let (a, b) = (ring_off[ring] as usize, ring_off[ring + 1] as usize);
                    if b < a + 3 {
                        continue;
                    }
                    for q in a..b {
                        let q2 = if q + 1 < b { q + 1 } else { a };
                        let (xa, ya, xb, yb) = (xy[[q, 0]], xy[[q, 1]], xy[[q2, 0]], xy[[q2, 1]]);
                        if (ya > ty) != (yb > ty) {
                            xs.push(xa + (ty - ya) * (xb - xa) / (yb - ya));
                        }
                    }
                }
                if xs.len() < 2 {
                    continue;
                }
                xs.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
                for pair in xs.chunks(2) {
                    if pair.len() < 2 {
                        break;
                    }
                    // texels whose centre col + 0.5 lies in [xa, xb)
                    let ca = (pair[0] - 0.5).ceil().max(0.0);
                    let cb = (pair[1] - 0.5).ceil().min(w as f64);
                    if cb <= ca {
                        continue;
                    }
                    for col in ca as usize..cb as usize {
                        grid[[row, col]] = values[p];
                        written += 1;
                    }
                }
            }
        }
        written
    });
    arr.assign(&grid);
    Ok(written)
}

/// Each texel's (kind, region) (n, n) from: base (n, n) the land cover's kind (KINDS: WorldCover's class, built-up land
/// a lawn where surface.lawns says so); poly (n, n) 0 or 1 + the index of the land-use / water polygon over it, whose
/// kinds are poly_kind; canopy (n, n) the canopy height (m, 255: none); cover (n, n) bits: 1 water, 2 paved (roads,
/// lots, pads), 4 roof (the buildings' grown footprints). Rules, in order:
/// - an open polygon (lawn, grass, crop, shrub, bare, wetland) makes its texels its kind, but the land cover's trees
///   and water inside stay (a park's trees); a forest polygon makes its texels trees, but built-up land, bare ground
///   and water stay; a water polygon makes them water;
/// - canopy heights, where there are some: TREE_MIN_M or taller is a tree (except on water); a texel the land cover
///   calls trees but lower is a shrub (SHRUB_MIN_M and over) or grass;
/// - then the cover: water, paved, roof (each over the ones before).
/// The region is the kind and the polygon (each polygon's texels of a kind are a region of their own), except for
/// paved and roof (one region each).
#[pyfunction]
pub fn gg_regions<'py>(py: Python<'py>, base: PyReadonlyArray2<'py, u8>, poly: PyReadonlyArray2<'py, u32>,
                       poly_kind: PyReadonlyArray1<'py, u8>, canopy: PyReadonlyArray2<'py, u8>,
                       cover: PyReadonlyArray2<'py, u8>)
                       -> PyResult<(Bound<'py, PyArray2<u8>>, Bound<'py, PyArray2<u32>>)> {
    let (base, poly, pk, canopy, cover) = (base.as_array(), poly.as_array(), poly_kind.as_array(), canopy.as_array(),
                                           cover.as_array());
    let sh = base.shape().to_vec();
    if poly.shape() != &sh[..] || canopy.shape() != &sh[..] || cover.shape() != &sh[..] {
        return Err(pyo3::exceptions::PyValueError::new_err("gg_regions: bad shapes"));
    }
    let (h, w) = (sh[0], sh[1]);
    let (base, poly, pk, canopy, cover) = (base.to_owned(), poly.to_owned(), pk.to_owned(), canopy.to_owned(),
                                           cover.to_owned());
    let (kind, region) = py.detach(|| {
        let mut kind = vec![0u8; h * w];
        let mut region = vec![0u32; h * w];
        for r in 0..h {
            for c in 0..w {
                let mut k = base[[r, c]];
                if k as usize >= NKINDS {
                    k = NONE;
                }
                let p = poly[[r, c]];
                let mut pid = 0u32;
                if p > 0 && (p as usize) <= pk.len() {
                    pid = p;
                    let want = pk[p as usize - 1];
                    match want {
                        LAWN | GRASS | CROP | SHRUB | BARE | WETLAND => {
                            if k != TREE && k != WATER { k = want; }
                        }
                        TREE => {
                            if k != BUILT && k != BARE && k != WATER { k = TREE; }
                        }
                        WATER => k = WATER,
                        _ => {}
                    }
                }
                let ch = canopy[[r, c]];
                if ch != CANOPY_NODATA && k != WATER {
                    if ch >= TREE_MIN_M {
                        k = TREE;
                    } else if k == TREE {
                        k = if ch >= SHRUB_MIN_M { SHRUB } else { GRASS };
                    }
                }
                let cv = cover[[r, c]];
                if cv & COVER_WATER != 0 { k = WATER; }
                if cv & COVER_PAVED != 0 { k = PAVED; }
                if cv & COVER_ROOF != 0 { k = ROOF; }
                kind[r * w + c] = k;
                region[r * w + c] = if k == PAVED || k == ROOF { k as u32 } else { (pid << 4) | k as u32 };
            }
        }
        (kind, region)
    });
    let ka = numpy::ndarray::Array2::from_shape_vec((h, w), kind)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let ra = numpy::ndarray::Array2::from_shape_vec((h, w), region)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok((ka.into_pyarray(py), ra.into_pyarray(py)))
}

/// The mean photo colour (sum, count) of texels in the window of radius `rad` texels round (r, c), on the lattice of
/// `step` texels from the block's corner, that satisfy keep.
#[inline]
fn window_sum(s2: &[u8], n: usize, r: usize, c: usize, rad: usize, step: usize,
              keep: impl Fn(usize) -> bool) -> ([u32; 3], u32) {
    let r0 = r.saturating_sub(rad).div_ceil(step) * step;
    let c0 = c.saturating_sub(rad).div_ceil(step) * step;
    let r1 = (r + rad).min(n - 1);
    let c1 = (c + rad).min(n - 1);
    let mut s = [0u32; 3];
    let mut k = 0u32;
    let mut y = r0;
    while y <= r1 {
        let mut x = c0;
        while x <= c1 {
            let i = y * n + x;
            if keep(i) {
                s[0] += s2[i * 3] as u32;
                s[1] += s2[i * 3 + 1] as u32;
                s[2] += s2[i * 3 + 2] as u32;
                k += 1;
            }
            x += step;
        }
        y += step;
    }
    (s, k)
}

/// gen_ground's windows (texels) and purity thresholds (thirds of a texel, border_distance's units).
struct Windows {
    near_r: usize,
    near_s: usize,
    far_r: usize,
    far_s: usize,
    pure3: u32,
    semi3: u32,
}

/// The colour of texel (r, c)'s region: the mean photo colour of the first of, in order, the region's pure texels
/// nearby, then farther, its fairly pure ones (SEMI_M), the kind's pure, then fairly pure texels farther round, the
/// region's texels nearby whatever their purity; else a quarter of the kind's palette colour in px. Water's: of the
/// texels no brighter than that mean.
#[allow(clippy::too_many_arguments)]
fn region_mean(s2: &[u8], kind: &[u8], region: &[u32], dist: &[u32], n: usize, r: usize, c: usize, w: &Windows,
               px: [f32; 3]) -> [f32; 3] {
    let i = r * n + c;
    let (k, reg) = (kind[i], region[i]);
    let tries: [(usize, usize, bool, u32); 6] = [
        (w.near_r, w.near_s, true, w.pure3), (w.far_r, w.far_s, true, w.pure3), (w.far_r, w.far_s, true, w.semi3),
        (w.far_r, w.far_s, false, w.pure3), (w.far_r, w.far_s, false, w.semi3), (w.near_r, 1, true, 0)];
    for (rad, step, own, min_d) in tries {
        let keep = |j: usize| dist[j] >= min_d && if own { region[j] == reg } else { kind[j] == k };
        let (mut s, mut cnt) = window_sum(s2, n, r, c, rad, step, keep);
        if cnt > 0 && k == WATER {
            // water: the texels no brighter than the mean (boats, moorings, piers the map does not have)
            let lum = (s[0] + s[1] + s[2]) / cnt;
            let lum_of = |j: usize| s2[j * 3] as u32 + s2[j * 3 + 1] as u32 + s2[j * 3 + 2] as u32;
            let (s_, cnt_) = window_sum(s2, n, r, c, rad, step, |j| keep(j) && lum_of(j) <= lum);
            if cnt_ > 0 {
                (s, cnt) = (s_, cnt_);
            }
        }
        if cnt > 0 {
            return [s[0] as f32 / cnt as f32, s[1] as f32 / cnt as f32, s[2] as f32 / cnt as f32];
        }
    }
    let p = PALETTE[k as usize];
    if p[0] > 0.0 {
        [px[0] + (p[0] - px[0]) * 0.25, px[1] + (p[1] - px[1]) * 0.25, px[2] + (p[2] - px[2]) * 0.25]
    } else {
        px
    }
}

/// The generated ground (n, n, 3) u8, row 0 = north: s2 (n, n, 3) the Sentinel-2 photo rendered over the block's
/// texels (enlarged: its own 10 m detail); kind / region (n, n) from gg_regions; canopy (n, n) heights (m, 255: none);
/// (ox, oy) the world-locked east / north (m) of texel (0, 0)'s centre (texel (r, c) at ox + c t, oy - r t); t: m a
/// texel.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn gen_ground<'py>(py: Python<'py>, s2: PyReadonlyArray3<'py, u8>, kind: PyReadonlyArray2<'py, u8>,
                       region: PyReadonlyArray2<'py, u32>, canopy: PyReadonlyArray2<'py, u8>, ox: f64, oy: f64,
                       t: f64) -> PyResult<Bound<'py, PyArray3<u8>>> {
    let (s2a, kind, region, canopy) = (s2.as_array(), kind.as_array(), region.as_array(), canopy.as_array());
    let n = kind.shape()[0];
    if n < 2 || kind.shape()[1] != n || region.shape() != kind.shape() || canopy.shape() != kind.shape()
        || s2a.shape() != [n, n, 3] || !(t > 0.0) || !ox.is_finite() || !oy.is_finite() {
        return Err(pyo3::exceptions::PyValueError::new_err("gen_ground: bad shapes"));
    }
    let s2: Vec<u8> = s2a.iter().copied().collect();
    let kind: Vec<u8> = kind.iter().map(|&k| if (k as usize) < NKINDS { k } else { NONE }).collect();
    let region: Vec<u32> = region.iter().copied().collect();
    let canopy: Vec<u8> = canopy.iter().copied().collect();
    let out = py.detach(move || {
        let tx = |m: f64| -> usize { ((m / t).round() as usize).max(1) };
        let dist = border_distance(&region, n);
        let pure3 = (PURE_M / t * 3.0).round() as u32;
        let blend3 = BLEND_M / t * 3.0;
        let win = Windows { near_r: tx(NEAR_R_M), near_s: tx(NEAR_STEP_M), far_r: tx(FAR_R_M), far_s: tx(FAR_STEP_M),
                            pure3, semi3: (SEMI_M / t * 3.0).round() as u32 };
        // the canopy heights smoothed over 3 x 3 (the 1.2 m pixels resampled to the texels: their stairs); none where
        // a texel or most of its neighbours have none
        let mut hsm = vec![f32::NAN; n * n];
        for r in 0..n {
            for c in 0..n {
                if canopy[r * n + c] == CANOPY_NODATA {
                    continue;
                }
                let (mut s, mut k) = (0u32, 0u32);
                for y in r.saturating_sub(1)..=(r + 1).min(n - 1) {
                    for x in c.saturating_sub(1)..=(c + 1).min(n - 1) {
                        let v = canopy[y * n + x];
                        if v != CANOPY_NODATA {
                            s += v as u32;
                            k += 1;
                        }
                    }
                }
                if k >= 5 {
                    hsm[r * n + c] = s as f32 / k as f32;
                }
            }
        }
        let threads = par::threads_for(n * n, 65536);
        // the regions' colours on a lattice of every LATTICE texels (interpolated between the lattice points of the
        // texel's own region; a texel with none of its region round it asks its own): a tenth of the windows
        let na = (n - 1) / LATTICE + 1;
        let need = |i: usize| kind[i] != NONE && ((dist[i] as f64) < blend3 + 3.0 * 1.5 * LATTICE as f64
                                                   || kind[i] == WATER);
        let mut lat = vec![[0f32; 4]; na * na];
        par::rows(&mut lat, na, threads, |a, row| {
            for (b, v) in row.iter_mut().enumerate() {
                let (r, c) = (a * LATTICE, b * LATTICE);
                let i = r * n + c;
                if need(i) {
                    let px = [s2[i * 3] as f32, s2[i * 3 + 1] as f32, s2[i * 3 + 2] as f32];
                    let m = region_mean(&s2, &kind, &region, &dist, n, r, c, &win, px);
                    *v = [m[0], m[1], m[2], 1.0];
                }
            }
        });
        let mut col = vec![0f32; n * n * 3];
        par::rows(&mut col, n * 3, threads, |r, row| {
            for c in 0..n {
                let i = r * n + c;
                let k = kind[i];
                let px = [s2[i * 3] as f32, s2[i * 3 + 1] as f32, s2[i * 3 + 2] as f32];
                let d = dist[i];
                // the region's own colour near its border (water: everywhere, the photo's boats, piers and their
                // 10 m blur are not the water's colour)
                let mut base = px;
                if ((d as f64) < blend3 || k == WATER) && k != NONE {
                    let (a0, b0) = (r / LATTICE, c / LATTICE);
                    let fy = (r - a0 * LATTICE) as f32 / LATTICE as f32;
                    let fx = (c - b0 * LATTICE) as f32 / LATTICE as f32;
                    let (mut acc, mut wsum) = ([0f32; 3], 0f32);
                    for (da, db, w) in [(0, 0, (1.0 - fy) * (1.0 - fx)), (0, 1, (1.0 - fy) * fx),
                                        (1, 0, fy * (1.0 - fx)), (1, 1, fy * fx)] {
                        let (a, b) = (a0 + da, b0 + db);
                        if w <= 0.0 || a >= na || b >= na {
                            continue;
                        }
                        let v = lat[a * na + b];
                        if v[3] > 0.0 && region[a * LATTICE * n + b * LATTICE] == region[i] {
                            acc = [acc[0] + w * v[0], acc[1] + w * v[1], acc[2] + w * v[2]];
                            wsum += w;
                        }
                    }
                    let mean = if wsum > 1e-4 {
                        [acc[0] / wsum, acc[1] / wsum, acc[2] / wsum]
                    } else {
                        region_mean(&s2, &kind, &region, &dist, n, r, c, &win, px)
                    };
                    // smoothstep from the border (all the mean) to BLEND_M in (all the photo)
                    let x = if k == WATER { 0.0 } else { ((d as f64 - 3.0) / (blend3 - 3.0)).clamp(0.0, 1.0) as f32 };
                    let a = x * x * (3.0 - 2.0 * x);
                    base = [mean[0] + (px[0] - mean[0]) * a, mean[1] + (px[1] - mean[1]) * a,
                            mean[2] + (px[2] - mean[2]) * a];
                }
                // the kind's fine detail, world-locked
                let det = &DETAIL[k as usize];
                let (wx, wy) = (ox + c as f64 * t, oy - r as f64 * t);
                let mut f = 1.0f32;
                for (o, &(size, amp)) in det.octaves.iter().enumerate() {
                    if size > 0.0 && amp > 0.0 {
                        f += amp * vnoise(wx / size, wy / size, 101 + 16 * k as u64 + o as u64) as f32;
                    }
                }
                if k == TREE || k == SHRUB {
                    let hc = hsm[i];
                    if !hc.is_nan() {
                        // crowns shaded by the canopy's slope (its own heights' gradient, m / m)
                        let at = |y: usize, x: usize| { let v = hsm[y * n + x]; if v.is_nan() { hc } else { v } };
                        let (cl, cr) = (c.saturating_sub(1), (c + 1).min(n - 1));
                        let (ru, rd) = (r.saturating_sub(1), (r + 1).min(n - 1));
                        let hx = (at(r, cr) - at(r, cl)) / (((cr - cl).max(1)) as f32 * t as f32);
                        let hy = (at(ru, c) - at(rd, c)) / (((rd - ru).max(1)) as f32 * t as f32);
                        let len = (hx * hx + hy * hy + 1.0).sqrt();
                        let lam = ((-hx * SUN.0 - hy * SUN.1 + SUN.2) / len).max(0.0);
                        f *= (1.0 + CANOPY_SHADE * (lam / SUN.2 - 1.0)).clamp(0.45, 1.25);
                    } else if det.crowns.0 > 0.0 {
                        let dome = crown(wx, wy, det.crowns.0, 7 + k as u64);
                        f *= 1.0 + det.crowns.1 * (dome.sqrt() - 0.55);
                    }
                }
                let mut rgb = [base[0] * f, base[1] * f, base[2] * f];
                if det.tint > 0.0 {
                    let tn = det.tint * vnoise(wx / 24.0, wy / 24.0, 977) as f32;
                    rgb[0] += tn;
                    rgb[1] += 0.5 * tn;
                    rgb[2] -= 0.6 * tn;
                }
                row[c * 3..c * 3 + 3].copy_from_slice(&rgb);
            }
        });
        // the borders' texels (a 4-neighbour in another region) antialiased: the mean of their 3 x 3 (the regions'
        // 1 m stairs; the photo's are softer)
        let mut out = vec![0u8; n * n * 3];
        par::rows(&mut out, n * 3, threads, |r, row| {
            for c in 0..n {
                let i = r * n + c;
                for ch in 0..3 {
                    let v = if dist[i] <= 3 {
                        let (mut sum, mut k) = (0f32, 0f32);
                        for y in r.saturating_sub(1)..=(r + 1).min(n - 1) {
                            for x in c.saturating_sub(1)..=(c + 1).min(n - 1) {
                                sum += col[(y * n + x) * 3 + ch];
                                k += 1.0;
                            }
                        }
                        sum / k
                    } else {
                        col[i * 3 + ch]
                    };
                    row[c * 3 + ch] = v.round().clamp(0.0, 255.0) as u8;
                }
            }
        });
        out
    });
    let arr = numpy::ndarray::Array3::from_shape_vec((n, n, 3), out)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok(arr.into_pyarray(py))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(gg_fill_polys, m)?)?;
    m.add_function(wrap_pyfunction!(gg_regions, m)?)?;
    m.add_function(wrap_pyfunction!(gen_ground, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn distance_is_three_at_borders() {
        let n = 8;
        let region: Vec<u32> = (0..n * n).map(|i| if i % n < 4 { 1 } else { 2 }).collect();
        let d = border_distance(&region, n);
        assert_eq!(d[3], 3);
        assert_eq!(d[4], 3);
        assert_eq!(d[2], 6);
        assert_eq!(d[0], 12);
    }

    #[test]
    fn crowns_and_noise_are_bounded() {
        for i in 0..1000 {
            let (x, y) = (i as f64 * 0.37 - 100.0, i as f64 * 0.91 + 5.0e6);
            let v = vnoise(x, y, 3);
            assert!((-1.0..=1.0).contains(&v));
            let cr = crown(x, y, 5.0, 7);
            assert!((0.0..=1.0).contains(&cr));
        }
    }
}
