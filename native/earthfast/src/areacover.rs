//! Saved areas (the user, 2026-10-28: "add the option to select an area and ... download all data"): the tiles an
//! area's download plan walks, its size, and which cache keys lie in a saved area (pinned: never evicted or cleared).
//! server/areas.py is the glue. Saved routes (the user, 2026-10-29: "options on pre downloading/generating along a gps
//! route"): a corridor (a polyline and a half width) instead of a box: its tiles at any zoom (corridor_tiles), its
//! length and area, and the cache keys under it (keys_in_cover, from its tiles at a pin zoom).

use std::collections::{HashMap, HashSet};

use pyo3::prelude::*;

use crate::cachemap::{tail_tile, tile_of};

const EARTH_R_KM: f64 = 6371.0088;
const MAX_TILES: u64 = 4_000_000;

/// The tiles (x, y) of zoom z under the box (degrees south, west, north, east), with `ring` more tiles round it, in
/// rows from the north-west; an error past MAX_TILES. Never wraps the antimeridian (an area does not cross it).
#[pyfunction]
pub fn area_tiles(s: f64, w: f64, n: f64, e: f64, z: u8, ring: u32) -> PyResult<Vec<(u32, u32)>> {
    if z > 24 || !(s.is_finite() && w.is_finite() && n.is_finite() && e.is_finite()) || n < s || e < w {
        return Err(pyo3::exceptions::PyValueError::new_err("area_tiles: bad box"));
    }
    let last = ((1u64 << z) - 1) as i64;
    let (xa, ya) = tile_of(n, w, z);
    let (xb, yb) = tile_of(s, e, z);
    let r = ring as i64;
    let (x0, x1) = ((xa as i64 - r).max(0), (xb as i64 + r).min(last));
    let (y0, y1) = ((ya as i64 - r).max(0), (yb as i64 + r).min(last));
    if ((x1 - x0 + 1) as u64) * ((y1 - y0 + 1) as u64) > MAX_TILES {
        return Err(pyo3::exceptions::PyValueError::new_err("area_tiles: too many tiles"));
    }
    let mut out = Vec::with_capacity(((x1 - x0 + 1) * (y1 - y0 + 1)) as usize);
    for y in y0..=y1 {
        for x in x0..=x1 {
            out.push((x as u32, y as u32));
        }
    }
    Ok(out)
}

/// The box's area on the sphere (km2).
#[pyfunction]
pub fn area_km2(s: f64, w: f64, n: f64, e: f64) -> f64 {
    if !(n > s && e > w) {
        return 0.0;
    }
    EARTH_R_KM * EARTH_R_KM * (e - w).to_radians() * (n.to_radians().sin() - s.to_radians().sin())
}

/// Normalised Web-Mercator x (0..1 east from -180) and y (0..1 south from the top) of lat / lon.
fn norm(lat: f64, lon: f64) -> (f64, f64) {
    let lat = lat.clamp(-85.051_128_779_806_6, 85.051_128_779_806_6).to_radians();
    ((lon + 180.0) / 360.0, (1.0 - lat.tan().asinh() / std::f64::consts::PI) / 2.0)
}

/// For each cache key: does its z/x/y tile ("<prefix>/z/x/y.ext") touch one of the boxes (degrees south, west,
/// north, east)? Keys that are not tiles: false.
#[pyfunction]
pub fn keys_in_boxes(py: Python<'_>, keys: Vec<String>, boxes: Vec<(f64, f64, f64, f64)>) -> Vec<bool> {
    let nb: Vec<(f64, f64, f64, f64)> = boxes.iter().map(|&(s, w, n, e)| {
        let (xa, ya) = norm(n, w);
        let (xb, yb) = norm(s, e);
        (xa, ya, xb, yb)
    }).collect();
    py.allow_threads(|| keys.iter().map(|k| {
        let Some((z, x, y, _)) = tail_tile(k, 0) else { return false };
        let size = 1.0 / (1u64 << z) as f64;
        let (tx0, ty0) = (x as f64 * size, y as f64 * size);
        let (tx1, ty1) = (tx0 + size, ty0 + size);
        nb.iter().any(|&(xa, ya, xb, yb)| tx0 < xb && tx1 > xa && ty0 < yb && ty1 > ya)
    }).collect())
}

const EARTH_R_M: f64 = 6_371_008.8;
const SUB_M: f64 = 500.0;           // a segment is walked in pieces of at most max(SUB_M, 2 half): their boxes stay small

fn m_per_deg() -> f64 {
    EARTH_R_M * std::f64::consts::PI / 180.0
}

/// Latitude of a Web-Mercator tile row edge (y in tiles at zoom z).
fn row_lat(y: f64, z: u8) -> f64 {
    let n = (1u64 << z) as f64;
    (std::f64::consts::PI * (1.0 - 2.0 * y / n)).sinh().atan().to_degrees()
}

/// Distance from point p to the segment a..b (local metres), and the segment's parameter t of the nearest point.
fn pt_seg(p: (f64, f64), a: (f64, f64), b: (f64, f64)) -> (f64, f64) {
    let (dx, dy) = (b.0 - a.0, b.1 - a.1);
    let l2 = dx * dx + dy * dy;
    let t = if l2 > 0.0 { (((p.0 - a.0) * dx + (p.1 - a.1) * dy) / l2).clamp(0.0, 1.0) } else { 0.0 };
    let (qx, qy) = (a.0 + t * dx, a.1 + t * dy);
    (((p.0 - qx).powi(2) + (p.1 - qy).powi(2)).sqrt(), t)
}

/// Distance from point p to the axis-aligned rectangle (x0, y0, x1, y1) (0 inside).
fn pt_rect(p: (f64, f64), r: (f64, f64, f64, f64)) -> f64 {
    let dx = (r.0 - p.0).max(0.0).max(p.0 - r.2);
    let dy = (r.1 - p.1).max(0.0).max(p.1 - r.3);
    (dx * dx + dy * dy).sqrt()
}

/// Does the segment a..b cross the rectangle (Liang-Barsky)?
fn seg_hits_rect(a: (f64, f64), b: (f64, f64), r: (f64, f64, f64, f64)) -> bool {
    let (dx, dy) = (b.0 - a.0, b.1 - a.1);
    let (mut t0, mut t1) = (0.0f64, 1.0f64);
    for (p, q) in [(-dx, a.0 - r.0), (dx, r.2 - a.0), (-dy, a.1 - r.1), (dy, r.3 - a.1)] {
        if p == 0.0 {
            if q < 0.0 {
                return false;
            }
        } else {
            let t = q / p;
            if p < 0.0 {
                t0 = t0.max(t);
            } else {
                t1 = t1.min(t);
            }
            if t0 > t1 {
                return false;
            }
        }
    }
    true
}

/// Distance between the segment a..b and the rectangle r (0 when they touch).
fn seg_rect(a: (f64, f64), b: (f64, f64), r: (f64, f64, f64, f64)) -> f64 {
    if seg_hits_rect(a, b, r) {
        return 0.0;
    }
    let mut d = pt_rect(a, r).min(pt_rect(b, r));
    for c in [(r.0, r.1), (r.2, r.1), (r.0, r.3), (r.2, r.3)] {
        d = d.min(pt_seg(c, a, b).0);
    }
    d
}

/// The corridor's tiles: {(x, y): the distance along the route (m) where it is first reached}.
fn corridor_map(lats: &[f64], lons: &[f64], half_m: f64, z: u8) -> Result<HashMap<(u32, u32), f64>, String> {
    if lats.len() != lons.len() || lats.is_empty() || z > 24 || !(half_m.is_finite() && half_m >= 0.0) {
        return Err("corridor: bad route".into());
    }
    if lats.iter().chain(lons.iter()).any(|v| !v.is_finite()) || lats.iter().any(|v| v.abs() > 85.06) {
        return Err("corridor: a point off the map".into());
    }
    let n = (1u64 << z) as f64;
    let k = m_per_deg();
    let mut out: HashMap<(u32, u32), f64> = HashMap::new();
    let mut along = 0.0;
    let pts: Vec<(f64, f64)> = lats.iter().copied().zip(lons.iter().copied()).collect();
    let segs: Vec<((f64, f64), (f64, f64))> = if pts.len() == 1 {
        vec![(pts[0], pts[0])]
    } else {
        pts.windows(2).map(|w| (w[0], w[1])).collect()
    };
    for (pa, pb) in segs {
        let mid = ((pa.0 + pb.0) / 2.0).to_radians().cos().max(1e-6);
        let seg_len = (((pb.0 - pa.0) * k).powi(2) + ((pb.1 - pa.1) * k * mid).powi(2)).sqrt();
        let pieces = ((seg_len / SUB_M.max(2.0 * half_m)).ceil() as usize).max(1);
        for i in 0..pieces {
            let (f0, f1) = (i as f64 / pieces as f64, (i + 1) as f64 / pieces as f64);
            let a = (pa.0 + (pb.0 - pa.0) * f0, pa.1 + (pb.1 - pa.1) * f0);
            let b = (pa.0 + (pb.0 - pa.0) * f1, pa.1 + (pb.1 - pa.1) * f1);
            // local metres round a: x east, y north (equirectangular at the piece's latitude)
            let c = ((a.0 + b.0) / 2.0).to_radians().cos().max(1e-6);
            let to_m = |lat: f64, lon: f64| ((lon - a.1) * k * c, (lat - a.0) * k);
            let (am, bm) = (to_m(a.0, a.1), to_m(b.0, b.1));
            let plen = ((bm.0 - am.0).powi(2) + (bm.1 - am.1).powi(2)).sqrt();
            let dlat = half_m / k;
            let dlon = half_m / (k * c);
            let (s, nn) = (a.0.min(b.0) - dlat, a.0.max(b.0) + dlat);
            let (w, e) = (a.1.min(b.1) - dlon, a.1.max(b.1) + dlon);
            let (xa, ya) = tile_of(nn, w, z);
            let (xb, yb) = tile_of(s, e, z);
            if ((xb - xa + 1) as u64) * ((yb - ya + 1) as u64) > MAX_TILES {
                return Err("corridor: too many tiles".into());
            }
            for y in ya..=yb {
                let (lat_n, lat_s) = (row_lat(y as f64, z), row_lat(y as f64 + 1.0, z));
                for x in xa..=xb {
                    let (lon_w, lon_e) = (x as f64 / n * 360.0 - 180.0, (x as f64 + 1.0) / n * 360.0 - 180.0);
                    let (x0, y0) = to_m(lat_s, lon_w);
                    let (x1, y1) = to_m(lat_n, lon_e);
                    let r = (x0, y0, x1, y1);
                    if seg_rect(am, bm, r) > half_m {
                        continue;
                    }
                    let t = pt_seg(((x0 + x1) / 2.0, (y0 + y1) / 2.0), am, bm).1;
                    let at = along + t * plen;
                    let e = out.entry((x, y)).or_insert(f64::INFINITY);
                    if at < *e {
                        *e = at;
                    }
                }
            }
            along += plen;
        }
        if out.len() as u64 > MAX_TILES {
            return Err("corridor: too many tiles".into());
        }
    }
    Ok(out)
}

/// The tiles (x, y, along) of zoom z whose square comes within half_m (m) of the route (lats / lons: its points in order),
/// with `ring` more tiles round them, nearest the route's start first (the distance along it where each is first
/// reached; ties by row, then column; a ring tile takes its neighbour's). An error past MAX_TILES. Deterministic.
#[pyfunction]
pub fn corridor_tiles(lats: Vec<f64>, lons: Vec<f64>, half_m: f64, z: u8, ring: u32) -> PyResult<Vec<(u32, u32, f64)>> {
    let mut m = corridor_map(&lats, &lons, half_m, z).map_err(pyo3::exceptions::PyValueError::new_err)?;
    if ring > 0 {
        let last = ((1u64 << z) - 1) as i64;
        let r = ring as i64;
        let mut core: Vec<((u32, u32), f64)> = m.iter().map(|(k, v)| (*k, *v)).collect();
        core.sort_by(|a, b| a.0.cmp(&b.0));
        for ((x, y), at) in core {
            for dy in -r..=r {
                for dx in -r..=r {
                    let (nx, ny) = (x as i64 + dx, y as i64 + dy);
                    if nx < 0 || ny < 0 || nx > last || ny > last {
                        continue;
                    }
                    let e = m.entry((nx as u32, ny as u32)).or_insert(f64::INFINITY);
                    if at < *e {
                        *e = at;
                    }
                }
            }
        }
        if m.len() as u64 > MAX_TILES {
            return Err(pyo3::exceptions::PyValueError::new_err("corridor: too many tiles"));
        }
    }
    let mut v: Vec<((u32, u32), f64)> = m.into_iter().collect();
    v.sort_by(|a, b| a.1.total_cmp(&b.1).then(a.0 .1.cmp(&b.0 .1)).then(a.0 .0.cmp(&b.0 .0)));
    Ok(v.into_iter().map(|(k, at)| (k.0, k.1, at)).collect())
}

/// The route's length (m, along its points) and its corridor's area (km2: length x 2 half + the round ends, pi
/// half^2; where the route doubles back the overlap is counted twice).
#[pyfunction]
pub fn corridor_size(lats: Vec<f64>, lons: Vec<f64>, half_m: f64) -> (f64, f64) {
    let k = m_per_deg();
    let mut len = 0.0;
    for i in 1..lats.len().min(lons.len()) {
        let c = ((lats[i] + lats[i - 1]) / 2.0).to_radians().cos();
        len += (((lats[i] - lats[i - 1]) * k).powi(2) + ((lons[i] - lons[i - 1]) * k * c).powi(2)).sqrt();
    }
    let h = half_m.max(0.0);
    (len, (len * 2.0 * h + std::f64::consts::PI * h * h) / 1e6)
}

/// For each cache key: does its z/x/y tile ("<prefix>/z/x/y.ext") overlap one of the cover tiles (z, x, y) (the saved
/// routes' corridors as tiles of a pin zoom)? A key's tile overlaps when it is one of them or an ancestor of one, or
/// one of them is its ancestor. Keys that are not tiles: false.
#[pyfunction]
pub fn keys_in_cover(py: Python<'_>, keys: Vec<String>, cover: Vec<(u8, u32, u32)>) -> Vec<bool> {
    let exact: HashSet<(u8, u32, u32)> = cover.iter().copied().filter(|t| t.0 < 32).collect();
    let mut up: HashSet<(u8, u32, u32)> = HashSet::new();
    for &(z, x, y) in &exact {
        for d in 0..=z {
            if !up.insert((z - d, x >> d, y >> d)) && d > 0 {
                break;      // (its ancestors are in already)
            }
        }
    }
    let zmin = exact.iter().map(|t| t.0).min().unwrap_or(0);
    py.allow_threads(|| keys.iter().map(|k| {
        let Some((z, x, y, _)) = tail_tile(k, 0) else { return false };
        if up.contains(&(z, x, y)) {
            return true;
        }
        let mut d = 1u8;
        while z >= d && z - d >= zmin {
            if exact.contains(&(z - d, x >> d, y >> d)) {
                return true;
            }
            d += 1;
        }
        false
    }).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn corridor() {
        // a 10 km route east along 39 N, 1 km each side, z14 (~1.9 km tiles there)
        let lats = vec![39.0, 39.0];
        let lons = vec![-76.6, -76.6 + 10_000.0 / (m_per_deg() * 39f64.to_radians().cos())];
        let m = corridor_map(&lats, &lons, 1000.0, 14).unwrap();
        assert!(m.len() >= 6 && m.len() <= 21, "{}", m.len());
        let (x0, y0) = tile_of(39.0, -76.6, 14);
        assert_eq!(m[&(x0, y0)], 0.0);
        assert!(corridor_map(&lats, &lons, 3000.0, 14).unwrap().len() > m.len());
        assert_eq!(corridor_map(&[39.0], &[-76.6], 0.0, 14).unwrap().len(), 1);
        let (len, km2) = corridor_size(lats.clone(), lons.clone(), 1000.0);
        assert!((len - 10_000.0).abs() < 1.0, "{len}");
        assert!((km2 - (20.0 + std::f64::consts::PI)).abs() < 0.01, "{km2}");
    }

    #[test]
    fn rect_distance() {
        let r = (0.0, 0.0, 10.0, 10.0);
        assert_eq!(seg_rect((-5.0, 5.0), (15.0, 5.0), r), 0.0);
        assert!((seg_rect((-5.0, 15.0), (15.0, 15.0), r) - 5.0).abs() < 1e-9);
        assert!((seg_rect((13.0, 14.0), (20.0, 20.0), r) - 5.0).abs() < 1e-9);
    }

    #[test]
    fn km2() {
        // a 1 x 1 degree box at the equator: ~12,364 km2
        let a = area_km2(0.0, 0.0, 1.0, 1.0);
        assert!((a - 12364.0).abs() < 20.0, "{a}");
    }
}
