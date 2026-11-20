//! French IGN RPG (`RPG.LATEST:parcelles_graphiques`) lavender parcel filtering (`LAV`, `LVH`) and oriented row
//! sampling (server/ignrpg.py, server/vegetation.py).
//!
//! All geometry, projection, dominant-edge orientation (`vegclumps::edge_axis_core`), lattice sampling, CHM mask
//! rasterisation and `blake2` PRNG run in Rust using deterministic `dm::Dm` maths.
use crate::dm::Dm;

use blake2::{Blake2s256, Digest};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyReadwriteArray2};
use numpy::ndarray::Array2;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use serde_json::Value;

const R: f64 = 6378137.0;
const MAX_LAT: f64 = 85.0511287798066;

const LAV_ALONG_M: f64 = 2.45;
const LAV_ACROSS_M: f64 = 2.72;
const LAV_MIN_M2: f64 = 150.0;
const LAV_MAX_STEPS: i64 = 600;
const LAV_CELL_MAX: usize = 40_000;
const EDGE_AXIS_DEG: f64 = 8.0;

// Metropolitan France coarse outline (lon, lat), matching server/bdtopo.py
const MAINLAND: &[(f64, f64)] = &[
    (2.50, 51.12), (2.95, 50.82), (3.30, 50.82), (3.75, 50.56), (4.25, 50.38), (4.72, 50.21), (4.90, 50.20),
    (4.95, 49.82), (5.45, 49.62), (5.85, 49.58), (6.40, 49.50), (6.75, 49.25), (7.10, 49.18), (7.65, 49.10),
    (8.27, 49.01), (7.95, 48.62), (7.70, 48.30), (7.65, 47.95), (7.62, 47.60), (7.20, 47.52), (6.98, 47.52),
    (6.50, 47.12), (6.20, 46.75), (6.15, 46.42), (6.00, 46.30), (6.25, 46.32), (6.55, 46.47), (6.85, 46.45),
    (6.85, 46.12), (7.08, 45.85), (7.20, 45.48), (6.90, 45.12), (7.10, 44.85), (7.95 - 1.0, 44.45), (7.12, 44.20),
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
fn hash_seed(id: &str, part_idx: usize, x0: f64, y0: f64) -> u64 {
    let mut h = Blake2s256::new();
    h.update(b"ignrpg:lav:v1:");
    h.update(id.as_bytes());
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

#[inline]
fn is_lavender_code(code: &str) -> bool {
    let s = code.trim();
    s.eq_ignore_ascii_case("LAV") || s.eq_ignore_ascii_case("LVH")
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

pub(crate) fn parse_lavender_core(data: &[u8]) -> Result<(Vec<[f64; 2]>, Vec<i64>, Vec<i64>, Vec<u64>), String> {
    if data.is_empty() {
        return Ok((Vec::new(), vec![0], vec![0], Vec::new()));
    }
    let root: Value = serde_json::from_slice(data).map_err(|e| format!("ignrpg JSON parse error: {e}"))?;
    let features = if let Some(arr) = root.as_array() {
        arr.as_slice()
    } else if let Some(arr) = root.get("features").and_then(|v| v.as_array()) {
        arr.as_slice()
    } else {
        &[]
    };

    let mut records: Vec<(u64, String, usize, Vec<Vec<[f64; 2]>>)> = Vec::new();
    for (fi, feat) in features.iter().enumerate() {
        let Some(props) = feat.get("properties").and_then(|v| v.as_object()) else {
            continue;
        };
        let code = props.get("code_cultu")
            .or_else(|| props.get("CODE_CULTU"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        if !is_lavender_code(code) {
            continue;
        }
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
            if rings.is_empty() || ring_area_3857(&rings[0]) < LAV_MIN_M2 {
                continue;
            }
            let p0 = rings[0][0];
            let seed = hash_seed(&fid, pi, p0[0], p0[1]);
            records.push((seed, fid.clone(), pi, rings));
        }
    }

    records.sort_by(|a, b| a.0.cmp(&b.0).then_with(|| a.1.cmp(&b.1)).then_with(|| a.2.cmp(&b.2)));
    records.dedup_by(|a, b| a.0 == b.0 && a.3 == b.3);

    let mut lonlat: Vec<[f64; 2]> = Vec::new();
    let mut coord_off: Vec<i64> = vec![0];
    let mut ring_off: Vec<i64> = vec![0];
    let mut seeds: Vec<u64> = Vec::with_capacity(records.len());

    for (seed, _fid, _pi, rings) in records {
        for r in rings {
            lonlat.extend_from_slice(&r);
            coord_off.push(lonlat.len() as i64);
        }
        ring_off.push((coord_off.len() - 1) as i64);
        seeds.push(seed);
    }

    Ok((lonlat, coord_off, ring_off, seeds))
}

/// Parse an IGN RPG GeoJSON payload, filtering for `LAV` / `LVH` parcels with area >= LAV_MIN_M2.
/// Returns `(lonlat (N, 2), coord_off (R + 1,), ring_off (P + 1,), seeds (P,))`.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn ignrpg_parse_lavender<'py>(
    py: Python<'py>,
    data: &[u8],
) -> PyResult<(
    Bound<'py, PyArray2<f64>>,
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<i64>>,
    Bound<'py, PyArray1<u64>>,
)> {
    let (lonlat, coord_off, ring_off, seeds) = py.detach(|| parse_lavender_core(data))
        .map_err(PyValueError::new_err)?;
    let n = lonlat.len();
    let flat: Vec<f64> = lonlat.into_iter().flat_map(|p| [p[0], p[1]]).collect();
    Ok((
        Array2::from_shape_vec((n, 2), flat).unwrap().into_pyarray(py),
        coord_off.into_pyarray(py),
        ring_off.into_pyarray(py),
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
pub(crate) fn lavender_rows_core(
    ll: &[[f64; 2]],
    coord_off: &[i64],
    ring_off: &[i64],
    seeds: &[u64],
    blocked: &[u8],
    n_r: usize,
    n_c: usize,
    mask: &mut [u8],
    grid: (f64, f64, f64, f64),
    bounds: (f64, f64, f64, f64),
) -> (Vec<f64>, Vec<f64>, Vec<f32>, Vec<f32>, Vec<f32>, Vec<f32>) {
    let (lon0, lat0, lon1, lat1) = bounds;
    let (ex0, ey0) = to3857(lon0, lat0);
    let (ex1, ey1) = to3857(lon1, lat1);

    let n_parts = seeds.len().min(ring_off.len().saturating_sub(1));
    let mut rows: Vec<(f64, f64, f32, f32, f32, f32)> = Vec::new();

    for p in 0..n_parts {
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
        let (mut along, mut across) = (LAV_ALONG_M * sec, LAV_ACROSS_M * sec);
        let mut factor = 1i64;
        while factor < 16 && (((amax - amin) / along).ceil() as i64 > LAV_MAX_STEPS
            || ((bmax - bmin) / across).ceil() as i64 > LAV_MAX_STEPS)
        {
            factor += 1;
            along = LAV_ALONG_M * (factor as f64) * sec;
            across = LAV_ACROSS_M * (factor as f64) * sec;
        }

        let seed = seeds[p];
        let phase_a = hash01(seed, 1, 0, 71) * along;
        let phase_b = hash01(seed, 2, 0, 71) * across;
        let i0 = ((amin - phase_a) / along).floor() as i64;
        let i1 = ((amax - phase_a) / along).ceil() as i64;
        let j0 = ((bmin - phase_b) / across).floor() as i64;
        let j1 = ((bmax - phase_b) / across).ceil() as i64;
        if i1 - i0 > LAV_MAX_STEPS || j1 - j0 > LAV_MAX_STEPS {
            continue;
        }

        let k_end = (0.5 * LAV_ALONG_M - 0.25) * sec;
        let yaw0 = uy.datan2(ux).to_degrees().rem_euclid(360.0);

        for i in i0..=i1 {
            let a = (i as f64) * along + phase_a;
            for j in j0..=j1 {
                let b = (j as f64) * across + phase_b;
                let px = a * ux + b * vx;
                let py_ = a * uy + b * vy;
                if !(px >= ex0 && px < ex1 && py_ > ey0 && py_ <= ey1) {
                    continue;
                }
                let e1 = (px + k_end * ux, py_ + k_end * uy);
                let e2 = (px - k_end * ux, py_ - k_end * uy);
                if !inside_part(&rings_3857, px, py_)
                    || !inside_part(&rings_3857, e1.0, e1.1)
                    || !inside_part(&rings_3857, e2.0, e2.1)
                {
                    continue;
                }
                if is_blocked(blocked, n_r, n_c, grid, px, py_)
                    || is_blocked(blocked, n_r, n_c, grid, e1.0, e1.1)
                    || is_blocked(blocked, n_r, n_c, grid, e2.0, e2.1)
                {
                    continue;
                }
                let uh = hash01(seed, i, j, 72);
                let us = hash01(seed, i, j, 73);
                let ur = hash01(seed, i, j, 74);
                let yaw = (if uh < 0.5 { yaw0 + 180.0 } else { yaw0 }).rem_euclid(360.0) as f32;
                let height = (0.66 + 0.14 * uh) as f32;
                let scale = (0.92 + 0.16 * us) as f32;
                let rank = (ur * 0.25) as f32;
                let (lon, lat) = to4326(px, py_);
                rows.push((lon, lat, height, yaw, rank, scale));
            }
        }
    }

    if rows.len() > LAV_CELL_MAX {
        let mut order: Vec<usize> = (0..rows.len()).collect();
        order.sort_by(|&a, &b| {
            rows[a].4.partial_cmp(&rows[b].4).unwrap_or(std::cmp::Ordering::Equal).then_with(|| a.cmp(&b))
        });
        order.truncate(LAV_CELL_MAX);
        order.sort_unstable();
        rows = order.into_iter().map(|i| rows[i]).collect();
    }

    let m = rows.len();
    let (mut lons, mut lats) = (Vec::with_capacity(m), Vec::with_capacity(m));
    let (mut hs, mut yaws, mut ranks, mut scales) = (
        Vec::with_capacity(m),
        Vec::with_capacity(m),
        Vec::with_capacity(m),
        Vec::with_capacity(m),
    );
    for (lo, la, h, yw, rk, sc) in rows {
        lons.push(lo);
        lats.push(la);
        hs.push(h);
        yaws.push(yw);
        ranks.push(rk);
        scales.push(sc);
    }
    (lons, lats, hs, yaws, ranks, scales)
}

/// Sample oriented lavender rows (`LAV_ALONG_M` x `LAV_ACROSS_M`) inside parsed IGN RPG `LAV`/`LVH` polygons,
/// marking the parcels on `lav_mask` and keeping items off `blocked` CHM pixels.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn ignrpg_lavender_rows<'py>(
    py: Python<'py>,
    lonlat: PyReadonlyArray2<'py, f64>,
    coord_off: PyReadonlyArray1<'py, i64>,
    ring_off: PyReadonlyArray1<'py, i64>,
    seeds: PyReadonlyArray1<'py, u64>,
    blocked: PyReadonlyArray2<'py, u8>,
    mut lav_mask: PyReadwriteArray2<'py, u8>,
    grid: (f64, f64, f64, f64),
    bounds: (f64, f64, f64, f64),
) -> PyResult<(
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f32>>,
    Bound<'py, PyArray1<f32>>,
    Bound<'py, PyArray1<f32>>,
    Bound<'py, PyArray1<f32>>,
)> {
    let ll_arr = lonlat.as_array();
    let ll: Vec<[f64; 2]> = (0..ll_arr.nrows()).map(|i| [ll_arr[[i, 0]], ll_arr[[i, 1]]]).collect();
    let co = coord_off.as_slice().unwrap_or(&[]).to_vec();
    let ro = ring_off.as_slice().unwrap_or(&[]).to_vec();
    let sd = seeds.as_slice().unwrap_or(&[]).to_vec();
    let bl_arr = blocked.as_array();
    let (n_r, n_c) = (bl_arr.nrows(), bl_arr.ncols());
    let bl: Vec<u8> = bl_arr.iter().copied().collect();

    let mut mask_arr = lav_mask.as_array_mut();
    if mask_arr.nrows() != n_r || mask_arr.ncols() != n_c {
        return Err(PyValueError::new_err("ignrpg_lavender_rows: lav_mask shape must match blocked"));
    }
    let mask_slice = mask_arr.as_slice_mut().ok_or_else(|| PyValueError::new_err("lav_mask must be contiguous"))?;

    let (lons, lats, hs, yaws, ranks, scales) = py.detach(|| {
        lavender_rows_core(&ll, &co, &ro, &sd, &bl, n_r, n_c, mask_slice, grid, bounds)
    });

    Ok((
        lons.into_pyarray(py),
        lats.into_pyarray(py),
        hs.into_pyarray(py),
        yaws.into_pyarray(py),
        ranks.into_pyarray(py),
        scales.into_pyarray(py),
    ))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(ignrpg_covers, m)?)?;
    m.add_function(wrap_pyfunction!(ignrpg_parse_lavender, m)?)?;
    m.add_function(wrap_pyfunction!(ignrpg_lavender_rows, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn covers_france_and_rejects_outside() {
        // Sault plateau (Provence)
        assert!(covers_core((5.35, 44.07, 5.38, 44.09)));
        // Baltimore, USA
        assert!(!covers_core((-76.62, 39.28, -76.58, 39.30)));
    }

    #[test]
    fn filters_lav_and_lvh_only_and_samples_oriented_rows() {
        let geojson = br#"{
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": "parcel_lav_1",
                    "properties": { "code_cultu": "LAV", "id_parcel": "101" },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[5.3650, 44.0815], [5.3662, 44.0815], [5.3662, 44.0820], [5.3650, 44.0820], [5.3650, 44.0815]]]
                    }
                },
                {
                    "type": "Feature",
                    "id": "parcel_lvh_2",
                    "properties": { "code_cultu": "LVH", "id_parcel": "102" },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[5.3670, 44.0815], [5.3676, 44.0815], [5.3676, 44.0822], [5.3670, 44.0822], [5.3670, 44.0815]]]
                    }
                },
                {
                    "type": "Feature",
                    "id": "parcel_wheat_3",
                    "properties": { "code_cultu": "BTH", "id_parcel": "103" },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[5.3680, 44.0815], [5.3690, 44.0815], [5.3690, 44.0822], [5.3680, 44.0822], [5.3680, 44.0815]]]
                    }
                }
            ]
        }"#;
        let (ll, co, ro, seeds) = parse_lavender_core(geojson).unwrap();
        assert_eq!(seeds.len(), 2, "only LAV and LVH kept");
        let (x0, y1) = to3857(5.3640, 44.0830);
        let grid = (x0, y1, 2.0, 2.0);
        let blocked = vec![0u8; 128 * 128];
        let mut mask = vec![0u8; 128 * 128];
        let bounds = (5.3640, 44.0810, 5.3700, 44.0830);
        let (lons, lats, hs, yaws, ranks, scales) =
            lavender_rows_core(&ll, &co, &ro, &seeds, &blocked, 128, 128, &mut mask, grid, bounds);
        assert!(lons.len() > 50, "sampled lavender rows: {}", lons.len());
        assert!(mask.iter().any(|&v| v != 0), "lav_mask marked");
        assert!(hs.iter().all(|&h| (0.66..=0.80).contains(&h)));
        assert!(scales.iter().all(|&s| (0.92..=1.08).contains(&s)));
        assert!(ranks.iter().all(|&r| (0.0..=0.25).contains(&r)));

        // Determinism check
        let mut mask2 = vec![0u8; 128 * 128];
        let (lons2, lats2, hs2, yaws2, ranks2, scales2) =
            lavender_rows_core(&ll, &co, &ro, &seeds, &blocked, 128, 128, &mut mask2, grid, bounds);
        assert_eq!(lons, lons2);
        assert_eq!(lats, lats2);
        assert_eq!(hs, hs2);
        assert_eq!(yaws, yaws2);
        assert_eq!(ranks, ranks2);
        assert_eq!(scales, scales2);
    }
}
