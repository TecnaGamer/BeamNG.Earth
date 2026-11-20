//! French IGN RPG (`RPG.LATEST:parcelles_graphiques`) vineyard (`VRC`, `VRN`, `VRT`, `VRG`, group `21`) and olive /
//! truffle orchard (`OLI`, `TRU`, group `23`) parcel filtering, Rust polygon deduplication against Overture `vineyard`
//! and `orchard` polygons, and oriented row sampling (server/ignrpg.py, server/vegetation.py).
//!
//! All geometry, Web Mercator projection, Overture overlap deduplication, dominant-edge orientation
//! (`vegclumps::edge_axis_core`), lattice sampling, CHM mask rasterisation and `blake2` PRNG run in Rust using
//! deterministic `dm::Dm` maths.
use crate::dm::Dm;

use blake2::{Blake2s256, Digest};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyReadwriteArray2};
use numpy::ndarray::Array2;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde_json::Value;

const R: f64 = 6378137.0;
const MAX_LAT: f64 = 85.0511287798066;

pub const KIND_VINEYARD: u8 = 1;
pub const KIND_OLIVE: u8 = 2;

const VINE_ALONG_M: f64 = 15.9;
const VINE_ACROSS_M: f64 = 7.35;
const VINE_H_M: f64 = 2.6;
const VINE_MIN_M2: f64 = 300.0;
const VINE_CELL_MAX: usize = 40_000;

const OLIVE_ALONG_M: f64 = 6.0;
const OLIVE_ACROSS_M: f64 = 6.0;
const OLIVE_MIN_M2: f64 = 400.0;
const OLIVE_CELL_MAX: usize = 4_500;

const MAX_STEPS: i64 = 400;
const EDGE_AXIS_DEG: f64 = 8.0;

// Metropolitan France coarse outline (lon, lat), matching server/bdtopo.py
const MAINLAND: &[(f64, f64)] = &[
    (2.50, 51.12), (2.95, 50.82), (3.30, 50.82), (3.75, 50.56), (4.25, 50.38), (4.72, 50.21), (4.90, 50.20),
    (4.95, 49.82), (5.45, 49.62), (5.85, 49.58), (6.40, 49.50), (6.75, 49.25), (7.10, 49.18), (7.65, 49.10),
    (8.27, 49.01), (7.95, 48.62), (7.70, 48.30), (7.65, 47.95), (7.62, 47.60), (7.20, 47.52), (6.98, 47.52),
    (6.50, 47.12), (6.20, 46.75), (6.15, 46.42), (6.00, 46.30), (6.25, 46.32), (6.55, 46.47), (6.85, 46.45),
    (6.85, 46.12), (7.08, 45.85), (7.20, 45.48), (6.90, 45.12), (7.10, 44.85), (6.95, 44.45), (7.12, 44.20),
    (7.72, 44.12), (7.60, 43.70), (7.10, 43.38), (6.20, 42.95), (5.00, 43.10), (4.20, 43.35), (3.40, 43.10),
    (3.10, 42.40), (2.55, 42.30), (1.95, 42.32), (1.40, 42.40), (0.70, 42.78), (0.00, 42.62), (-0.75, 42.88),
    (-1.40, 42.98), (-1.85, 43.30), (-1.95, 43.55), (-1.55, 44.60), (-1.50, 45.40), (-2.45, 46.55), (-2.85, 47.15),
    (-3.60, 47.55), (-4.85, 47.70), (-5.25, 48.40), (-4.80, 48.80), (-3.00, 49.00), (-1.95, 48.75), (-2.05, 49.75),
    (-1.20, 49.80), (-0.20, 49.45), (0.05, 49.75), (1.40, 50.15), (1.50, 51.00),
];

// Corsica and overseas departments (lon0, lat0, lon1, lat1)
const BOXES: &[(f64, f64, f64, f64)] = &[
    (8.50, 41.33, 9.62, 43.05),
    (-61.90, 15.80, -60.95, 16.55),
    (-61.25, 14.35, -60.75, 14.90),
    (-54.70, 2.05, -51.55, 5.85),
    (55.15, -21.42, 55.88, -20.85),
    (44.95, -13.05, 45.35, -12.60),
];

#[inline]
fn to3857(lon: f64, lat: f64) -> (f64, f64) {
    let lat_c = lat.clamp(-MAX_LAT, MAX_LAT);
    let x = lon.to_radians() * R;
    let y = (std::f64::consts::PI / 4.0 + lat_c.to_radians() / 2.0).dtan().dln() * R;
    (x, y)
}

#[inline]
fn to4326(x: f64, y: f64) -> (f64, f64) {
    let lon = (x / R).to_degrees();
    let lat = (2.0 * (y / R).dexp().datan() - std::f64::consts::PI / 2.0).to_degrees();
    (lon, lat)
}

#[inline]
fn in_mainland(x: f64, y: f64) -> bool {
    let mut c = false;
    let n = MAINLAND.len();
    let mut j = n - 1;
    for i in 0..n {
        let (x1, y1) = MAINLAND[i];
        let (x2, y2) = MAINLAND[j];
        if (y1 > y) != (y2 > y) && x < (x2 - x1) * (y - y1) / (y2 - y1) + x1 {
            c = !c;
        }
        j = i;
    }
    c
}

pub(crate) fn covers_core(bounds: (f64, f64, f64, f64)) -> bool {
    let (lon0, lat0, lon1, lat1) = bounds;
    for &(b0, b1, b2, b3) in BOXES {
        if lon1 >= b0 && lon0 <= b2 && lat1 >= b1 && lat0 <= b3 {
            return true;
        }
    }
    if lon1 < -5.3 || lon0 > 8.3 || lat1 < 42.2 || lat0 > 51.2 {
        return false;
    }
    let test_pts = [
        (lon0, lat0),
        (lon1, lat0),
        (lon0, lat1),
        (lon1, lat1),
        (0.5 * (lon0 + lon1), 0.5 * (lat0 + lat1)),
    ];
    for (x, y) in test_pts {
        if in_mainland(x, y) {
            return true;
        }
    }
    MAINLAND.iter().any(|&(mx, my)| mx >= lon0 && mx <= lon1 && my >= lat0 && my <= lat1)
}

/// Does a cell (lon0, lat0, lon1, lat1) overlap France? Pure, no network.
#[pyfunction]
pub fn ignrpg_covers(bounds: (f64, f64, f64, f64)) -> bool {
    covers_core(bounds)
}

#[inline]
fn hash_seed(id: &str, kind: u8, part_idx: usize, x0: f64, y0: f64) -> u64 {
    let mut h = Blake2s256::new();
    h.update(b"ignrpg:vine:v1:");
    h.update(id.as_bytes());
    h.update(&[kind]);
    h.update(&(part_idx as u64).to_le_bytes());
    h.update(&x0.to_bits().to_le_bytes());
    h.update(&y0.to_bits().to_le_bytes());
    let d = h.finalize();
    u64::from_le_bytes(d[..8].try_into().unwrap())
}

#[inline]
fn hash01(seed: u64, a: i64, b: i64, salt: u64) -> f64 {
    let mut h = Blake2s256::new();
    h.update(&seed.to_le_bytes());
    h.update(&a.to_le_bytes());
    h.update(&b.to_le_bytes());
    h.update(&salt.to_le_bytes());
    let d = h.finalize();
    let u = u64::from_le_bytes(d[..8].try_into().unwrap());
    (u >> 11) as f64 / ((1u64 << 53) as f64)
}

/// Classify IGN RPG `code_cultu` / `code_group` into `KIND_VINEYARD` (1), `KIND_OLIVE` (2), or `0` (ignored).
#[inline]
pub(crate) fn classify_rpg(code_cultu: &str, code_group: &str) -> u8 {
    let c = code_cultu.trim();
    let g = code_group.trim();
    if c.eq_ignore_ascii_case("VRC")
        || c.eq_ignore_ascii_case("VRN")
        || c.eq_ignore_ascii_case("VRT")
        || c.eq_ignore_ascii_case("VRG")
        || g == "21"
    {
        return KIND_VINEYARD;
    }
    if c.eq_ignore_ascii_case("OLI") || c.eq_ignore_ascii_case("TRU") || g == "23" {
        return KIND_OLIVE;
    }
    0
}

fn ring_area_3857(ring: &[[f64; 2]]) -> f64 {
    let n = ring.len();
    if n < 3 {
        return 0.0;
    }
    let mut a2 = 0.0f64;
    for i in 0..n {
        let j = (i + 1) % n;
        let (x0, y0) = to3857(ring[i][0], ring[i][1]);
        let (x1, y1) = to3857(ring[j][0], ring[j][1]);
        a2 += x0 * y1 - x1 * y0;
    }
    (a2 * 0.5).abs()
}

fn parse_ring_coords(val: &Value) -> Option<Vec<[f64; 2]>> {
    let arr = val.as_array()?;
    let mut pts: Vec<[f64; 2]> = Vec::with_capacity(arr.len());
    for p in arr {
        let pa = p.as_array()?;
        if pa.len() < 2 {
            return None;
        }
        let lon = pa[0].as_f64()?;
        let lat = pa[1].as_f64()?;
        if !(lon.is_finite() && lat.is_finite()) {
            return None;
        }
        pts.push([lon, lat]);
    }
    if pts.len() >= 2 {
        let first = pts[0];
        let last = pts[pts.len() - 1];
        if (first[0] - last[0]).abs() <= 1e-9 && (first[1] - last[1]).abs() <= 1e-9 {
            pts.pop();
        }
    }
    if pts.len() >= 3 { Some(pts) } else { None }
}

fn extract_poly_parts(geom: &Value) -> Vec<Vec<Vec<[f64; 2]>>> {
    let Some(gtype) = geom.get("type").and_then(|v| v.as_str()) else {
        return Vec::new();
    };
    let Some(coords) = geom.get("coordinates").and_then(|v| v.as_array()) else {
        return Vec::new();
    };
    let mut out = Vec::new();
    if gtype == "Polygon" {
        let mut rings = Vec::new();
        for (ri, rval) in coords.iter().enumerate() {
            if let Some(r) = parse_ring_coords(rval) {
                rings.push(r);
            } else if ri == 0 {
                break;
            }
        }
        if !rings.is_empty() {
            out.push(rings);
        }
    } else if gtype == "MultiPolygon" {
        for part_val in coords {
            let Some(part_arr) = part_val.as_array() else {
                continue;
            };
            let mut rings = Vec::new();
            for (ri, rval) in part_arr.iter().enumerate() {
                if let Some(r) = parse_ring_coords(rval) {
                    rings.push(r);
                } else if ri == 0 {
                    break;
                }
            }
            if !rings.is_empty() {
                out.push(rings);
            }
        }
    }
    out
}

#[allow(clippy::type_complexity)]
pub(crate) fn parse_parcels_core(
    data: &[u8],
) -> Result<(Vec<[f64; 2]>, Vec<i64>, Vec<i64>, Vec<u8>, Vec<u64>), String> {
    if data.is_empty() {
        return Ok((Vec::new(), vec![0], vec![0], Vec::new(), Vec::new()));
    }
    let root: Value = serde_json::from_slice(data).map_err(|e| format!("ignrpg JSON parse error: {e}"))?;
    let features = if let Some(arr) = root.as_array() {
        arr.as_slice()
    } else if let Some(arr) = root.get("features").and_then(|v| v.as_array()) {
        arr.as_slice()
    } else {
        &[]
    };

    let mut records: Vec<(u64, u8, String, usize, Vec<Vec<[f64; 2]>>)> = Vec::new();
    for (fi, feat) in features.iter().enumerate() {
        let Some(props) = feat.get("properties").and_then(|v| v.as_object()) else {
            continue;
        };
        let cultu = props.get("code_cultu")
            .or_else(|| props.get("CODE_CULTU"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let group = props.get("code_group")
            .or_else(|| props.get("CODE_GROUP"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let kind = classify_rpg(cultu, group);
        if kind == 0 {
            continue;
        }
        let min_m2 = if kind == KIND_VINEYARD { VINE_MIN_M2 } else { OLIVE_MIN_M2 };
        let Some(geom) = feat.get("geometry") else {
            continue;
        };
        let fid = feat.get("id").and_then(|v| v.as_str())
            .or_else(|| props.get("id_parcel").and_then(|v| v.as_str()))
            .or_else(|| props.get("ID_PARCEL").and_then(|v| v.as_str()))
            .or_else(|| props.get("cleabs").and_then(|v| v.as_str()))
            .map(|s| s.to_string())
            .unwrap_or_else(|| format!("f{fi}"));

        for (pi, rings) in extract_poly_parts(geom).into_iter().enumerate() {
            if rings.is_empty() || ring_area_3857(&rings[0]) < min_m2 {
                continue;
            }
            let p0 = rings[0][0];
            let seed = hash_seed(&fid, kind, pi, p0[0], p0[1]);
            records.push((seed, kind, fid.clone(), pi, rings));
        }
    }

    records.sort_by(|a, b| {
        a.0.cmp(&b.0)
            .then_with(|| a.1.cmp(&b.1))
            .then_with(|| a.2.cmp(&b.2))
            .then_with(|| a.3.cmp(&b.3))
    });
    records.dedup_by(|a, b| a.0 == b.0 && a.1 == b.1 && a.4 == b.4);

    let mut lonlat: Vec<[f64; 2]> = Vec::new();
    let mut coord_off: Vec<i64> = vec![0];
    let mut ring_off: Vec<i64> = vec![0];
    let mut kinds: Vec<u8> = Vec::with_capacity(records.len());
    let mut seeds: Vec<u64> = Vec::with_capacity(records.len());

    for (seed, kind, _fid, _pi, rings) in records {
        for r in rings {
            lonlat.extend_from_slice(&r);
            coord_off.push(lonlat.len() as i64);
        }
        ring_off.push((coord_off.len() - 1) as i64);
        kinds.push(kind);
        seeds.push(seed);
    }

    Ok((lonlat, coord_off, ring_off, kinds, seeds))
}

/// Parse an IGN RPG GeoJSON payload, filtering for French vineyard (`KIND_VINEYARD = 1`) and olive/truffle
/// (`KIND_OLIVE = 2`) parcels.
/// Returns `(lonlat (N, 2), coord_off (R + 1,), ring_off (P + 1,), kinds (P,), seeds (P,))`.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn ignrpg_parse_parcels<'py>(
    py: Python<'py>,
    data: &[u8],
) -> PyResult<(
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<u8>>,
    Bound<'py, PyArray1<u64>>,
)> {
    let (lonlat, coord_off, ring_off, kinds, seeds) =
        py.detach(|| parse_parcels_core(data)).map_err(PyValueError::new_err)?;
    let n = lonlat.len();
    let flat: Vec<f64> = lonlat.into_iter().flat_map(|p| [p[0], p[1]]).collect();
    Ok((
        Array2::from_shape_vec((n, 2), flat).unwrap().into_pyarray(py),
        coord_off.into_pyarray(py),
        ring_off.into_pyarray(py),
        kinds.into_pyarray(py),
        seeds.into_pyarray(py),
    ))
}

#[inline]
fn inside_ring(r: &[[f64; 2]], px: f64, py_: f64) -> bool {
    let n = r.len();
    if n < 3 {
        return false;
    }
    let mut c = 0u32;
    for i in 0..n {
        let (x0, y0) = (r[i][0], r[i][1]);
        let (x1, y1) = (r[(i + 1) % n][0], r[(i + 1) % n][1]);
        if y0 == y1 {
            continue;
        }
        let (lo, hi) = (y0.min(y1), y0.max(y1));
        if py_ >= lo && py_ < hi && x0 + (py_ - y0) * ((x1 - x0) / (y1 - y0)) > px {
            c += 1;
        }
    }
    c & 1 == 1
}

#[inline]
fn inside_part(rings: &[Vec<[f64; 2]>], px: f64, py_: f64) -> bool {
    if rings.is_empty() || !inside_ring(&rings[0], px, py_) {
        return false;
    }
    !rings[1..].iter().any(|h| h.len() >= 3 && inside_ring(h, px, py_))
}

fn ring_bbox_and_interior(r: &[[f64; 2]]) -> ([f64; 4], [f64; 2]) {
    let n = r.len();
    if n == 0 {
        return ([0.0, 0.0, 0.0, 0.0], [0.0, 0.0]);
    }
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    let (mut a2, mut cx, mut cy) = (0.0f64, 0.0f64, 0.0f64);
    let (bx0, by0) = (r[0][0], r[0][1]);
    for i in 0..n {
        let p = r[i];
        let q = r[(i + 1) % n];
        x0 = x0.min(p[0]);
        x1 = x1.max(p[0]);
        y0 = y0.min(p[1]);
        y1 = y1.max(p[1]);
        let (ax, ay) = (p[0] - bx0, p[1] - by0);
        let (bx, by) = (q[0] - bx0, q[1] - by0);
        let cr = ax * by - bx * ay;
        a2 += cr;
        cx += (ax + bx) * cr;
        cy += (ay + by) * cr;
    }
    let (mut px, mut py) = if a2.abs() > 1e-9 {
        (bx0 + cx / (3.0 * a2), by0 + cy / (3.0 * a2))
    } else {
        (0.5 * (x0 + x1), 0.5 * (y0 + y1))
    };
    if !inside_ring(r, px, py) {
        let y = py + 1e-6;
        let mut cuts: Vec<f64> = Vec::new();
        for i in 0..n {
            let (xi, yi) = (r[i][0], r[i][1]);
            let (xj, yj) = (r[(i + 1) % n][0], r[(i + 1) % n][1]);
            if (yi > y) != (yj > y) {
                cuts.push((xj - xi) * (y - yi) / (yj - yi) + xi);
            }
        }
        cuts.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        let mut best = -1.0;
        let mut k = 0;
        while k + 1 < cuts.len() {
            let w = cuts[k + 1] - cuts[k];
            if w > best {
                best = w;
                px = 0.5 * (cuts[k] + cuts[k + 1]);
                py = y;
            }
            k += 2;
        }
    }
    ([x0, y0, x1, y1], [px, py])
}

/// Does RPG outer ring `a` (with bbox `ab` and interior point `ac`) overlap Overture outer ring `b` (with bbox `bb`
/// and interior point `bc`)? Nudges vertex tests slightly inward toward each polygon's interior point so adjacent
/// parcels sharing a cadastre border do not false-trigger.
fn polygons_overlap(a: &[[f64; 2]], ab: &[f64; 4], ac: &[f64; 2], b: &[[f64; 2]], bb: &[f64; 4], bc: &[f64; 2]) -> bool {
    if ab[2] <= bb[0] || ab[0] >= bb[2] || ab[3] <= bb[1] || ab[1] >= bb[3] {
        return false;
    }
    if inside_ring(b, ac[0], ac[1]) || inside_ring(a, bc[0], bc[1]) {
        return true;
    }
    for p in a {
        let x = p[0] + 0.05 * (ac[0] - p[0]);
        let y = p[1] + 0.05 * (ac[1] - p[1]);
        if inside_ring(b, x, y) {
            return true;
        }
    }
    for p in b {
        let x = p[0] + 0.05 * (bc[0] - p[0]);
        let y = p[1] + 0.05 * (bc[1] - p[1]);
        if inside_ring(a, x, y) {
            return true;
        }
    }
    // Check overlap of bounding box intersection with a 3x3 interior grid
    let ix0 = ab[0].max(bb[0]);
    let iy0 = ab[1].max(bb[1]);
    let ix1 = ab[2].min(bb[2]);
    let iy1 = ab[3].min(bb[3]);
    if ix1 > ix0 + 1.0 && iy1 > iy0 + 1.0 {
        for gx in 1..=3 {
            let x = ix0 + (ix1 - ix0) * (gx as f64 / 4.0);
            for gy in 1..=3 {
                let y = iy0 + (iy1 - iy0) * (gy as f64 / 4.0);
                if inside_ring(a, x, y) && inside_ring(b, x, y) {
                    return true;
                }
            }
        }
    }
    false
}

pub(crate) fn dedup_core(
    rpg_ll: &[[f64; 2]],
    rpg_co: &[i64],
    rpg_ro: &[i64],
    rpg_kinds: &[u8],
    ov_ll: &[[f64; 2]],
    ov_co: &[i64],
    ov_ro: &[i64],
    ov_po: &[i64],
    ov_feats: &[i64],
) -> Vec<bool> {
    let n_rpg = rpg_kinds.len().min(rpg_ro.len().saturating_sub(1));
    if n_rpg == 0 {
        return Vec::new();
    }
    let mut ov_polys: Vec<(Vec<[f64; 2]>, [f64; 4], [f64; 2])> = Vec::new();
    for &fi in ov_feats {
        if fi < 0 {
            continue;
        }
        let fi = fi as usize;
        if fi + 1 >= ov_po.len() {
            continue;
        }
        let p0 = ov_po[fi].max(0) as usize;
        let p1 = (ov_po[fi + 1].max(0) as usize).min(ov_ro.len().saturating_sub(1));
        for p in p0..p1 {
            let r0 = ov_ro[p].max(0) as usize;
            let r1 = ov_ro[p + 1].max(0) as usize;
            if r1 <= r0 || r0 + 1 >= ov_co.len() {
                continue;
            }
            let c0 = ov_co[r0].max(0) as usize;
            let c1 = (ov_co[r0 + 1].max(0) as usize).min(ov_ll.len());
            if c1 < c0 + 3 {
                continue;
            }
            let mut ring: Vec<[f64; 2]> = (c0..c1).map(|k| {
                let (x, y) = to3857(ov_ll[k][0], ov_ll[k][1]);
                [x, y]
            }).collect();
            if ring.len() >= 2 && (ring[0][0] - ring[ring.len() - 1][0]).abs() < 1e-6
                && (ring[0][1] - ring[ring.len() - 1][1]).abs() < 1e-6
            {
                ring.pop();
            }
            if ring.len() >= 3 {
                let (bb, bc) = ring_bbox_and_interior(&ring);
                ov_polys.push((ring, bb, bc));
            }
        }
    }

    let mut keep = vec![true; n_rpg];
    if ov_polys.is_empty() {
        return keep;
    }

    for p in 0..n_rpg {
        let r0 = rpg_ro[p].max(0) as usize;
        if r0 + 1 >= rpg_co.len() {
            keep[p] = false;
            continue;
        }
        let c0 = rpg_co[r0].max(0) as usize;
        let c1 = (rpg_co[r0 + 1].max(0) as usize).min(rpg_ll.len());
        if c1 < c0 + 3 {
            keep[p] = false;
            continue;
        }
        let ring: Vec<[f64; 2]> = (c0..c1).map(|k| {
            let (x, y) = to3857(rpg_ll[k][0], rpg_ll[k][1]);
            [x, y]
        }).collect();
        let (ab, ac) = ring_bbox_and_interior(&ring);
        if ov_polys.iter().any(|(ov_r, bb, bc)| polygons_overlap(&ring, &ab, &ac, ov_r, bb, bc)) {
            keep[p] = false;
        }
    }
    keep
}

/// Deduplicate IGN RPG polygons against existing Overture `vineyard` / `orchard` polygons in Web Mercator metres.
/// Returns `keep (P,)` bool array (`true` when the RPG parcel does NOT overlap any Overture polygon in `ov_feats`).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn ignrpg_dedup<'py>(
    py: Python<'py>,
    rpg_lonlat: PyReadonlyArray2<'py, f64>,
    rpg_coord_off: PyReadonlyArray1<'py, i64>,
    rpg_ring_off: PyReadonlyArray1<'py, i64>,
    rpg_kinds: PyReadonlyArray1<'py, u8>,
    ov_lonlat: PyReadonlyArray2<'py, f64>,
    ov_coord_off: PyReadonlyArray1<'py, i64>,
    ov_ring_off: PyReadonlyArray1<'py, i64>,
    ov_part_off: PyReadonlyArray1<'py, i64>,
    ov_feats: PyReadonlyArray1<'py, i64>,
) -> Bound<'py, PyArray1<bool>> {
    let r_arr = rpg_lonlat.as_array();
    let rpg_ll: Vec<[f64; 2]> = (0..r_arr.nrows()).map(|i| [r_arr[[i, 0]], r_arr[[i, 1]]]).collect();
    let rpg_co = rpg_coord_off.as_slice().unwrap_or(&[]).to_vec();
    let rpg_ro = rpg_ring_off.as_slice().unwrap_or(&[]).to_vec();
    let r_kinds = rpg_kinds.as_slice().unwrap_or(&[]).to_vec();

    let o_arr = ov_lonlat.as_array();
    let ov_ll: Vec<[f64; 2]> = (0..o_arr.nrows()).map(|i| [o_arr[[i, 0]], o_arr[[i, 1]]]).collect();
    let ov_co = ov_coord_off.as_slice().unwrap_or(&[]).to_vec();
    let ov_ro = ov_ring_off.as_slice().unwrap_or(&[]).to_vec();
    let ov_po = ov_part_off.as_slice().unwrap_or(&[]).to_vec();
    let ov_fs = ov_feats.as_slice().unwrap_or(&[]).to_vec();

    let keep = py.detach(|| dedup_core(&rpg_ll, &rpg_co, &rpg_ro, &r_kinds, &ov_ll, &ov_co, &ov_ro, &ov_po, &ov_fs));
    keep.into_pyarray(py)
}

#[inline]
fn is_blocked(blocked: &[u8], n_r: usize, n_c: usize, grid: (f64, f64, f64, f64), px: f64, py_: f64) -> bool {
    if blocked.is_empty() || n_r == 0 || n_c == 0 {
        return false;
    }
    let (x0, y0, dx, dy) = grid;
    let c = ((px - x0) / dx).floor() as i64;
    let r = ((y0 - py_) / dy).floor() as i64;
    if c >= 0 && (c as usize) < n_c && r >= 0 && (r as usize) < n_r {
        blocked[(r as usize) * n_c + (c as usize)] != 0
    } else {
        false
    }
}

fn mark_part_on_mask(rings_3857: &[Vec<[f64; 2]>], mask: &mut [u8], n_r: usize, n_c: usize, grid: (f64, f64, f64, f64)) {
    if mask.is_empty() || n_r == 0 || n_c == 0 || rings_3857.is_empty() {
        return;
    }
    let (x0, y0, dx, dy) = grid;
    let mut edges: Vec<(f64, f64, f64, f64)> = Vec::new();
    let (mut cmin, mut rmin, mut cmax, mut rmax) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for ring in rings_3857 {
        let m = ring.len();
        if m < 3 {
            continue;
        }
        for k in 0..m {
            let (c, rr) = ((ring[k][0] - x0) / dx, (y0 - ring[k][1]) / dy);
            let next = ring[(k + 1) % m];
            let (c2, r2) = ((next[0] - x0) / dx, (y0 - next[1]) / dy);
            cmin = cmin.min(c);
            cmax = cmax.max(c);
            rmin = rmin.min(rr);
            rmax = rmax.max(rr);
            edges.push((c, rr, c2, r2));
        }
    }
    if edges.is_empty() {
        return;
    }
    let c0 = (cmin.floor() as i64).max(0);
    let r0 = (rmin.floor() as i64).max(0);
    let c1 = (cmax.ceil() as i64 + 1).min(n_c as i64);
    let r1 = (rmax.ceil() as i64 + 1).min(n_r as i64);
    if c1 <= c0 || r1 <= r0 {
        return;
    }
    let mut row_edges: Vec<(f64, f64, f64, f64)> = Vec::new();
    for row in r0..r1 {
        let ty = row as f64 + 0.5;
        row_edges.clear();
        row_edges.extend(edges.iter().copied().filter(|&(_, ya, _, yb)| (ya >= ty) != (yb >= ty)));
        if row_edges.is_empty() {
            continue;
        }
        for col in c0..c1 {
            let tx = col as f64 + 0.5;
            let mut inside = false;
            for &(xa, ya, xb, yb) in &row_edges {
                let f0 = ya >= ty;
                let f1 = yb >= ty;
                if f0 != f1 && (((yb - ty) * (xa - xb) >= (xb - tx) * (ya - yb)) == f1) {
                    inside = !inside;
                }
            }
            if inside {
                mask[(row as usize) * n_c + (col as usize)] = 1;
            }
        }
    }
}

#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub(crate) fn sample_parcels_core(
    ll: &[[f64; 2]],
    coord_off: &[i64],
    ring_off: &[i64],
    kinds: &[u8],
    seeds: &[u64],
    keep: &[bool],
    want_kind: u8,
    blocked: &[u8],
    n_r: usize,
    n_c: usize,
    mask: &mut [u8],
    grid: (f64, f64, f64, f64),
    bounds: (f64, f64, f64, f64),
) -> (Vec<f64>, Vec<f64>, Vec<f32>, Vec<f32>, Vec<f32>, Vec<f32>, Vec<f32>) {
    let (lon0, lat0, lon1, lat1) = bounds;
    let (ex0, ey0) = to3857(lon0, lat0);
    let (ex1, ey1) = to3857(lon1, lat1);

    let n_parts = seeds
        .len()
        .min(kinds.len())
        .min(keep.len())
        .min(ring_off.len().saturating_sub(1));
    let mut rows: Vec<(f64, f64, f32, f32, f32, f32, f32)> = Vec::new();

    let (base_along, base_across, cell_max) = if want_kind == KIND_VINEYARD {
        (VINE_ALONG_M, VINE_ACROSS_M, VINE_CELL_MAX)
    } else {
        (OLIVE_ALONG_M, OLIVE_ACROSS_M, OLIVE_CELL_MAX)
    };

    for p in 0..n_parts {
        if !keep[p] || kinds[p] != want_kind {
            continue;
        }
        let r_start = ring_off[p].max(0) as usize;
        let r_end = (ring_off[p + 1].max(0) as usize).min(coord_off.len().saturating_sub(1));
        if r_end <= r_start {
            continue;
        }
        let mut rings_3857: Vec<Vec<[f64; 2]>> = Vec::with_capacity(r_end - r_start);
        let mut first_lat = 44.0f64;
        for r in r_start..r_end {
            let c_start = coord_off[r].max(0) as usize;
            let c_end = (coord_off[r + 1].max(0) as usize).min(ll.len());
            if c_end < c_start + 3 {
                continue;
            }
            if r == r_start {
                first_lat = ll[c_start][1];
            }
            let ring_xy: Vec<[f64; 2]> = (c_start..c_end).map(|k| {
                let (x, y) = to3857(ll[k][0], ll[k][1]);
                [x, y]
            }).collect();
            rings_3857.push(ring_xy);
        }
        if rings_3857.is_empty() || rings_3857[0].len() < 3 {
            continue;
        }

        mark_part_on_mask(&rings_3857, mask, n_r, n_c, grid);

        let (ux, uy) = crate::vegclumps::edge_axis_core(&rings_3857[0], EDGE_AXIS_DEG);
        let (vx, vy) = (-uy, ux);
        let sec = 1.0 / first_lat.clamp(-85.0, 85.0).to_radians().dcos().max(0.05);

        let (mut amin, mut amax, mut bmin, mut bmax) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
        for pt in &rings_3857[0] {
            let a = pt[0] * ux + pt[1] * uy;
            let b = pt[0] * vx + pt[1] * vy;
            amin = amin.min(a);
            amax = amax.max(a);
            bmin = bmin.min(b);
            bmax = bmax.max(b);
        }
        let (mut along, mut across) = (base_along * sec, base_across * sec);
        let mut factor = 1i64;
        while factor < 16 && (((amax - amin) / along).ceil() as i64 > MAX_STEPS
            || ((bmax - bmin) / across).ceil() as i64 > MAX_STEPS)
        {
            factor += 1;
            along = base_along * (factor as f64) * sec;
            across = base_across * (factor as f64) * sec;
        }

        let seed = seeds[p];
        let phase_a = hash01(seed, 1, 0, 81) * along;
        let phase_b = hash01(seed, 2, 0, 81) * across;
        let i0 = ((amin - phase_a) / along).floor() as i64;
        let i1 = ((amax - phase_a) / along).ceil() as i64;
        let j0 = ((bmin - phase_b) / across).floor() as i64;
        let j1 = ((bmax - phase_b) / across).ceil() as i64;
        if i1 - i0 > MAX_STEPS || j1 - j0 > MAX_STEPS {
            continue;
        }

        let k_end = if want_kind == KIND_VINEYARD {
            (0.5 * VINE_ALONG_M - 1.0) * sec
        } else {
            0.0
        };
        let yaw0 = uy.datan2(ux).to_degrees().rem_euclid(360.0);

        for i in i0..=i1 {
            let a0 = (i as f64) * along + phase_a;
            for j in j0..=j1 {
                let b0 = (j as f64) * across + phase_b;
                let (mut px, mut py_) = (a0 * ux + b0 * vx, a0 * uy + b0 * vy);
                if want_kind == KIND_OLIVE {
                    let ja = (hash01(seed, i, j, 85) - 0.5) * 0.8 * sec;
                    let jb = (hash01(seed, i, j, 86) - 0.5) * 0.6 * sec;
                    px += ja * ux + jb * vx;
                    py_ += ja * uy + jb * vy;
                }
                if !(px >= ex0 && px < ex1 && py_ > ey0 && py_ <= ey1) {
                    continue;
                }
                if !inside_part(&rings_3857, px, py_) || is_blocked(blocked, n_r, n_c, grid, px, py_) {
                    continue;
                }
                if k_end > 0.0 {
                    let e1 = (px + k_end * ux, py_ + k_end * uy);
                    let e2 = (px - k_end * ux, py_ - k_end * uy);
                    if !inside_part(&rings_3857, e1.0, e1.1)
                        || !inside_part(&rings_3857, e2.0, e2.1)
                        || is_blocked(blocked, n_r, n_c, grid, e1.0, e1.1)
                        || is_blocked(blocked, n_r, n_c, grid, e2.0, e2.1)
                    {
                        continue;
                    }
                }
                let uh = hash01(seed, i, j, 82);
                let us = hash01(seed, i, j, 83);
                let ur = hash01(seed, i, j, 84);
                let (lon, lat) = to4326(px, py_);
                if want_kind == KIND_VINEYARD {
                    let height = (VINE_H_M * (0.95 + 0.10 * us)) as f32;
                    let radius = 1.2f32;
                    let yaw = (if uh < 0.5 { yaw0 + 180.0 } else { yaw0 }).rem_euclid(360.0) as f32;
                    let rank = (uh * 0.15) as f32;
                    let scale = 1.0f32;
                    rows.push((lon, lat, height, radius, yaw, rank, scale));
                } else {
                    // Height in [3.6, 4.4] m so trees.lua CATALOG.mediterranean picks trees_italy/olive (4.3 m)
                    let height = (3.6 + 0.8 * uh) as f32;
                    let radius = 0.12 * height + 1.0;
                    let yaw = (yaw0 + (us - 0.5) * 36.0).rem_euclid(360.0) as f32;
                    let rank = (ur * 0.18) as f32;
                    let scale = (0.90 + 0.20 * us) as f32;
                    rows.push((lon, lat, height, radius, yaw, rank, scale));
                }
            }
        }
    }

    if rows.len() > cell_max {
        let mut order: Vec<usize> = (0..rows.len()).collect();
        order.sort_by(|&a, &b| {
            rows[a].5.partial_cmp(&rows[b].5).unwrap_or(std::cmp::Ordering::Equal).then_with(|| a.cmp(&b))
        });
        order.truncate(cell_max);
        order.sort_unstable();
        rows = order.into_iter().map(|i| rows[i]).collect();
    }

    let m = rows.len();
    let (mut lons, mut lats) = (Vec::with_capacity(m), Vec::with_capacity(m));
    let (mut hs, mut rads, mut yaws, mut ranks, mut scales) = (
        Vec::with_capacity(m),
        Vec::with_capacity(m),
        Vec::with_capacity(m),
        Vec::with_capacity(m),
        Vec::with_capacity(m),
    );
    for (lo, la, h, rd, yw, rk, sc) in rows {
        lons.push(lo);
        lats.push(la);
        hs.push(h);
        rads.push(rd);
        yaws.push(yw);
        ranks.push(rk);
        scales.push(sc);
    }
    (lons, lats, hs, rads, yaws, ranks, scales)
}

/// Sample oriented rows for kept IGN RPG parcels of `want_kind` (`KIND_VINEYARD = 1` or `KIND_OLIVE = 2`),
/// marking their footprints on `mask` and keeping items off `blocked` CHM pixels.
/// Returns `(lon, lat, height, radius, yaw, rank, scale)`.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn ignrpg_sample_parcels<'py>(
    py: Python<'py>,
    lonlat: PyReadonlyArray2<'py, f64>,
    coord_off: PyReadonlyArray1<'py, i64>,
    ring_off: PyReadonlyArray1<'py, i64>,
    kinds: PyReadonlyArray1<'py, u8>,
    seeds: PyReadonlyArray1<'py, u64>,
    keep: PyReadonlyArray1<'py, bool>,
    want_kind: u8,
    blocked: PyReadonlyArray2<'py, u8>,
    mut mask: PyReadwriteArray2<'py, u8>,
    grid: (f64, f64, f64, f64),
    bounds: (f64, f64, f64, f64),
) -> PyResult<(
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f32>>,
    Bound<'py, PyArray1<f32>>,
    Bound<'py, PyArray1<f32>>,
    Bound<'py, PyArray1<f32>>,
    Bound<'py, PyArray1<f32>>,
)> {
    let ll_arr = lonlat.as_array();
    let ll: Vec<[f64; 2]> = (0..ll_arr.nrows()).map(|i| [ll_arr[[i, 0]], ll_arr[[i, 1]]]).collect();
    let co = coord_off.as_slice().unwrap_or(&[]).to_vec();
    let ro = ring_off.as_slice().unwrap_or(&[]).to_vec();
    let kd = kinds.as_slice().unwrap_or(&[]).to_vec();
    let sd = seeds.as_slice().unwrap_or(&[]).to_vec();
    let kp = keep.as_slice().unwrap_or(&[]).to_vec();
    let bl_arr = blocked.as_array();
    let (n_r, n_c) = (bl_arr.nrows(), bl_arr.ncols());
    let bl: Vec<u8> = bl_arr.iter().copied().collect();

    let mut mask_arr = mask.as_array_mut();
    if mask_arr.nrows() != n_r || mask_arr.ncols() != n_c {
        return Err(PyValueError::new_err("ignrpg_sample_parcels: mask shape must match blocked"));
    }
    let mask_slice = mask_arr.as_slice_mut().ok_or_else(|| PyValueError::new_err("mask must be contiguous"))?;

    let (lons, lats, hs, rads, yaws, ranks, scales) = py.detach(|| {
        sample_parcels_core(&ll, &co, &ro, &kd, &sd, &kp, want_kind, &bl, n_r, n_c, mask_slice, grid, bounds)
    });

    Ok((
        lons.into_pyarray(py),
        lats.into_pyarray(py),
        hs.into_pyarray(py),
        rads.into_pyarray(py),
        yaws.into_pyarray(py),
        ranks.into_pyarray(py),
        scales.into_pyarray(py),
    ))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(ignrpg_covers, m)?)?;
    m.add_function(wrap_pyfunction!(ignrpg_parse_parcels, m)?)?;
    m.add_function(wrap_pyfunction!(ignrpg_dedup, m)?)?;
    m.add_function(wrap_pyfunction!(ignrpg_sample_parcels, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classifies_vineyard_and_olive_codes() {
        assert_eq!(classify_rpg("VRC", ""), KIND_VINEYARD);
        assert_eq!(classify_rpg("VRN", ""), KIND_VINEYARD);
        assert_eq!(classify_rpg("VRT", ""), KIND_VINEYARD);
        assert_eq!(classify_rpg("", "21"), KIND_VINEYARD);
        assert_eq!(classify_rpg("OLI", ""), KIND_OLIVE);
        assert_eq!(classify_rpg("TRU", ""), KIND_OLIVE);
        assert_eq!(classify_rpg("", "23"), KIND_OLIVE);
        assert_eq!(classify_rpg("BTH", "1"), 0);
        assert_eq!(classify_rpg("LAV", "24"), 0);
    }

    #[test]
    fn deduplicates_overlapping_overture_parcels_and_keeps_unmapped() {
        let geojson = br#"{
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": "vine_dup",
                    "properties": { "code_cultu": "VRC" },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[4.7400, 43.9940], [4.7412, 43.9940], [4.7412, 43.9948], [4.7400, 43.9948], [4.7400, 43.9940]]]
                    }
                },
                {
                    "type": "Feature",
                    "id": "vine_new",
                    "properties": { "code_cultu": "VRN" },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[4.7430, 43.9940], [4.7445, 43.9940], [4.7445, 43.9948], [4.7430, 43.9948], [4.7430, 43.9940]]]
                    }
                },
                {
                    "type": "Feature",
                    "id": "olive_new",
                    "properties": { "code_cultu": "OLI" },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[4.7460, 43.9940], [4.7475, 43.9940], [4.7475, 43.9948], [4.7460, 43.9948], [4.7460, 43.9940]]]
                    }
                }
            ]
        }"#;
        let (ll, co, ro, kinds, seeds) = parse_parcels_core(geojson).unwrap();
        assert_eq!(kinds.len(), 3);

        // Overture polygon overlapping vine_dup (4.7400..4.7412, 43.9940..43.9948)
        let ov_ll = vec![
            [4.7399, 43.9939],
            [4.7413, 43.9939],
            [4.7413, 43.9949],
            [4.7399, 43.9949],
            [4.7399, 43.9939],
        ];
        let ov_co = vec![0i64, 5];
        let ov_ro = vec![0i64, 1];
        let ov_po = vec![0i64, 1];
        let ov_feats = vec![0i64];

        let keep = dedup_core(&ll, &co, &ro, &kinds, &ov_ll, &ov_co, &ov_ro, &ov_po, &ov_feats);
        assert_eq!(keep.iter().filter(|&&k| k).count(), 2, "overlapping vine_dup dropped, 2 unmapped kept");

        let (x0, y1) = to3857(4.7390, 43.9960);
        let grid = (x0, y1, 2.0, 2.0);
        let blocked = vec![0u8; 256 * 256];
        let mut mask_v = vec![0u8; 256 * 256];
        let bounds = (4.7390, 43.9930, 4.7490, 43.9960);
        let (vlons, _vlats, vhs, _vrads, _vyaws, _vranks, _vscales) =
            sample_parcels_core(&ll, &co, &ro, &kinds, &seeds, &keep, KIND_VINEYARD, &blocked, 256, 256, &mut mask_v, grid, bounds);
        assert!(!vlons.is_empty(), "unmapped vineyard sampled");
        assert!(vlons.iter().all(|&lo| lo >= 4.7425), "no points inside deduplicated vine_dup");
        assert!(vhs.iter().all(|&h| (2.4..=2.9).contains(&h)));

        let mut mask_o = vec![0u8; 256 * 256];
        let (olons, _olats, ohs, ..) =
            sample_parcels_core(&ll, &co, &ro, &kinds, &seeds, &keep, KIND_OLIVE, &blocked, 256, 256, &mut mask_o, grid, bounds);
        assert!(!olons.is_empty(), "unmapped olive grove sampled");
        assert!(ohs.iter().all(|&h| (3.6..=4.4).contains(&h)));
    }
}
