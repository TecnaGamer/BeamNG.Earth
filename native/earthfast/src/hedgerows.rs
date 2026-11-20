//! France's linear hedgerows (IGN BD TOPO `haie`, server/bdtopo.py + server/vegetation.py):
//! - `bdtopo_hedgerows`: samples `BDTOPO_V3:haie` polylines in Web Mercator metres (`dm::Dm`), clips to the cell's
//!   bounds and the CHM grid's free pixels (`!blocked`), deduplicates per CHM pixel, dilates a 2-pixel exclusion mask
//!   so generic CHM tops and scatter do not double-place, and assigns cypress / olive windbreak trees and shrub points
//!   in Provence / Mediterranean France (deciduous trees and shrubs further north) using a deterministic `blake2` PRNG.
use crate::dm::Dm;

use blake2::{digest::consts::U8, Blake2s, Digest};
use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

const EARTH_R: f64 = 6_378_137.0;
const HALF_WORLD: f64 = 20_037_508.342_789_244;
const STEP_GROUND_M: f64 = 2.4;
const PROVENCE_LAT_MIN: f64 = 41.3;
const PROVENCE_LAT_MAX: f64 = 45.2;
const PROVENCE_LON_MIN: f64 = 2.0;
const PROVENCE_LON_MAX: f64 = 9.7;

#[inline]
fn to3857(lon: f64, lat: f64) -> (f64, f64) {
    let x = lon.to_radians() * EARTH_R;
    let clat = lat.clamp(-85.0, 85.0).to_radians();
    let y = (std::f64::consts::FRAC_PI_4 + 0.5 * clat).dtan().dln() * EARTH_R;
    (x, y)
}

#[inline]
fn to4326(x: f64, y: f64) -> (f64, f64) {
    let lon = (x / EARTH_R).to_degrees();
    let lat = (2.0 * (y / EARTH_R).dexp().datan() - std::f64::consts::FRAC_PI_2).to_degrees();
    (lon, lat)
}

#[inline]
fn blake2_u01(gx: i64, gy: i64, salt: u64) -> f64 {
    let mut h = Blake2s::<U8>::new();
    h.update(gx.to_le_bytes());
    h.update(gy.to_le_bytes());
    h.update(salt.to_le_bytes());
    let d = h.finalize();
    let mut b = [0u8; 8];
    b.copy_from_slice(&d);
    (u64::from_le_bytes(b) >> 11) as f64 / ((1u64 << 53) as f64)
}

#[inline]
fn in_provence(lat: f64, lon: f64) -> bool {
    (PROVENCE_LAT_MIN..=PROVENCE_LAT_MAX).contains(&lat) && (PROVENCE_LON_MIN..=PROVENCE_LON_MAX).contains(&lon)
}

#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub(crate) fn hedgerows_core(
    off: &[i64],
    ll: numpy::ndarray::ArrayView2<f64>,
    line_h: &[f32],
    hs: numpy::ndarray::ArrayView2<f32>,
    blocked: numpy::ndarray::ArrayView2<bool>,
    grid: (f64, f64, f64, f64),
    bounds: (f64, f64, f64, f64),
    groups: (u8, u8, u8, u8),
    kinds: (u8, u8),
) -> (
    Vec<f64>,
    Vec<f64>,
    Vec<f32>,
    Vec<f32>,
    Vec<u8>,
    Vec<u8>,
    Vec<f32>,
    Vec<f32>,
    Vec<f32>,
    Vec<bool>,
) {
    let (n_r, n_c) = hs.dim();
    let mut mask = vec![false; n_r * n_c];
    let n_lines = off.len().saturating_sub(1).min(line_h.len());
    if n_lines == 0 || n_r == 0 || n_c == 0 {
        return (
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            Vec::new(),
            mask,
        );
    }
    let (x0, y0, dx, dy) = grid;
    let (lon0, lat0, lon1, lat1) = bounds;
    let (ex0, ey0) = to3857(lon0, lat0);
    let (ex1, ey1) = to3857(lon1, lat1);
    let lat_mid = 0.5 * (lat0 + lat1);
    let m_scale = 1.0 / lat_mid.to_radians().dcos().max(0.05);
    let step_3857 = STEP_GROUND_M * m_scale;
    let (g_cypress, g_olive, g_deciduous, g_shrub) = groups;
    let (kind_tree, kind_bush) = kinds;

    let mut claimed = vec![false; n_r * n_c];
    let mut out_lon = Vec::new();
    let mut out_lat = Vec::new();
    let mut out_h = Vec::new();
    let mut out_rad = Vec::new();
    let mut out_kind = Vec::new();
    let mut out_grp = Vec::new();
    let mut out_rank = Vec::new();
    let mut out_yaw = Vec::new();
    let mut out_scale = Vec::new();

    for l in 0..n_lines {
        let a = off[l].max(0) as usize;
        let b = (off[l + 1].max(0) as usize).min(ll.nrows());
        if b < a + 2 {
            continue;
        }
        let h_attr = if line_h[l].is_finite() && line_h[l] > 0.0 { line_h[l] } else { 0.0 };
        for k in a..(b - 1) {
            let (lon_a, lat_a) = (ll[[k, 0]], ll[[k, 1]]);
            let (lon_b, lat_b) = (ll[[k + 1, 0]], ll[[k + 1, 1]]);
            if !(lon_a.is_finite() && lat_a.is_finite() && lon_b.is_finite() && lat_b.is_finite()) {
                continue;
            }
            let (hx0, hy0) = to3857(lon_a, lat_a);
            let (hx1, hy1) = to3857(lon_b, lat_b);
            let (sdx, sdy) = (hx1 - hx0, hy1 - hy0);
            let seg_len = sdx.dhypot(sdy);
            if !seg_len.is_finite() || seg_len < 1.0 {
                continue;
            }
            let (ux, uy) = (sdx / seg_len, sdy / seg_len);
            let seg_yaw = uy.datan2(ux).to_degrees().rem_euclid(360.0) as f32;
            let n_steps = (seg_len / step_3857).floor().max(1.0) as usize;
            let ds = seg_len / (n_steps as f64);
            for s in 0..n_steps {
                let t = (s as f64 + 0.5) * ds;
                let px = hx0 + t * ux;
                let py = hy0 + t * uy;
                if !(px >= ex0 && px < ex1 && py > ey0 && py <= ey1) {
                    continue;
                }
                let c_f = ((px - x0) / dx).floor();
                let r_f = ((y0 - py) / dy).floor();
                if !(c_f >= 0.0 && r_f >= 0.0 && c_f < n_c as f64 && r_f < n_r as f64) {
                    continue;
                }
                let (r, c) = (r_f as usize, c_f as usize);
                let lin = r * n_c + c;
                if blocked[[r, c]] || claimed[lin] {
                    continue;
                }
                claimed[lin] = true;
                let r0 = r.saturating_sub(2);
                let r1 = (r + 2).min(n_r - 1);
                let c0 = c.saturating_sub(2);
                let c1 = (c + 2).min(n_c - 1);
                for mr in r0..=r1 {
                    for mc in c0..=c1 {
                        mask[mr * n_c + mc] = true;
                    }
                }

                let gx = ((px + HALF_WORLD) / dx).floor() as i64;
                let gy = ((HALF_WORLD - py) / dy).floor() as i64;
                let u_h = blake2_u01(gx, gy, 31);
                let u_yaw = blake2_u01(gx, gy, 32);
                let u_rank = blake2_u01(gx, gy, 33);
                let u_sc = blake2_u01(gx, gy, 34);
                let u_sp = blake2_u01(gx, gy, 35);

                let (lon, lat) = to4326(px, py);
                let provence = in_provence(lat, lon);
                let h_chm = if hs[[r, c]].is_finite() && hs[[r, c]] > 0.0 { hs[[r, c]] } else { 0.0 };
                let h_raw = h_chm.max(h_attr) as f64;
                let h_base = if h_raw >= 1.2 {
                    h_raw
                } else if provence {
                    7.2
                } else {
                    4.5
                };
                let is_tall = h_base >= 3.0;
                let (height, radius, kind, grp, yaw) = if !is_tall {
                    if provence && u_sp < 0.55 {
                        (1.65f32, 0.45f32, kind_bush, g_cypress, seg_yaw)
                    } else {
                        ((1.4 + 0.8 * u_h) as f32, 0.45f32, kind_bush, g_shrub, (u_yaw * 360.0) as f32)
                    }
                } else if provence {
                    if u_sp < 0.72 {
                        (
                            (h_base * (0.88 + 0.24 * u_h)).clamp(3.5, 14.0) as f32,
                            0.35f32,
                            kind_tree,
                            g_cypress,
                            (u_yaw * 360.0) as f32,
                        )
                    } else if u_sp < 0.90 {
                        (
                            (3.8 + 2.2 * u_h) as f32,
                            0.85f32,
                            kind_tree,
                            g_olive,
                            (u_yaw * 360.0) as f32,
                        )
                    } else {
                        ((1.5 + 0.9 * u_h) as f32, 0.55f32, kind_bush, g_shrub, (u_yaw * 360.0) as f32)
                    }
                } else if u_sp < 0.78 {
                    (
                        (h_base * (0.88 + 0.24 * u_h)).clamp(3.5, 14.0) as f32,
                        0.65f32,
                        kind_tree,
                        g_deciduous,
                        (u_yaw * 360.0) as f32,
                    )
                } else {
                    ((1.5 + 0.9 * u_h) as f32, 0.55f32, kind_bush, g_shrub, (u_yaw * 360.0) as f32)
                };

                out_lon.push(lon);
                out_lat.push(lat);
                out_h.push(height);
                out_rad.push(radius);
                out_kind.push(kind);
                out_grp.push(grp);
                out_rank.push(u_rank as f32);
                out_yaw.push(yaw);
                out_scale.push((0.9 + 0.2 * u_sc) as f32);
            }
        }
    }
    (out_lon, out_lat, out_h, out_rad, out_kind, out_grp, out_rank, out_yaw, out_scale, mask)
}

/// Sample IGN BD TOPO `haie` polylines into vegetation items `(lon, lat, height, radius, kind, group, rank, yaw, scale)`
/// and a 2-pixel dilated CHM exclusion `mask (rows, cols)`.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn bdtopo_hedgerows<'py>(
    py: Python<'py>,
    line_off: PyReadonlyArray1<'py, i64>,
    lonlat: PyReadonlyArray2<'py, f64>,
    line_h: PyReadonlyArray1<'py, f32>,
    hs: PyReadonlyArray2<'py, f32>,
    blocked: PyReadonlyArray2<'py, bool>,
    grid: (f64, f64, f64, f64),
    bounds: (f64, f64, f64, f64),
    groups: (u8, u8, u8, u8),
    kinds: (u8, u8),
) -> PyResult<(
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f64>>,
    Bound<'py, PyArray1<f32>>,
    Bound<'py, PyArray1<f32>>,
    Bound<'py, PyArray1<u8>>,
    Bound<'py, PyArray1<u8>>,
    Bound<'py, PyArray1<f32>>,
    Bound<'py, PyArray1<f32>>,
    Bound<'py, PyArray1<f32>>,
    Bound<'py, PyArray2<bool>>,
)> {
    let off = line_off.as_slice().unwrap_or(&[]);
    let ll = lonlat.as_array();
    let lh = line_h.as_slice().unwrap_or(&[]);
    let hs_a = hs.as_array();
    let bl_a = blocked.as_array();
    if hs_a.dim() != bl_a.dim() {
        return Err(PyValueError::new_err("bdtopo_hedgerows: hs and blocked must have the same shape"));
    }
    if !(grid.2 > 0.0 && grid.3 > 0.0) {
        return Err(PyValueError::new_err("bdtopo_hedgerows: grid steps must be > 0"));
    }
    let (n_r, n_c) = hs_a.dim();
    let (lon, lat, h, rad, kind, grp, rank, yaw, scale, mask) =
        hedgerows_core(off, ll, lh, hs_a, bl_a, grid, bounds, groups, kinds);
    let mask_arr = Array2::from_shape_vec((n_r, n_c), mask).unwrap();
    Ok((
        lon.into_pyarray(py),
        lat.into_pyarray(py),
        h.into_pyarray(py),
        rad.into_pyarray(py),
        kind.into_pyarray(py),
        grp.into_pyarray(py),
        rank.into_pyarray(py),
        yaw.into_pyarray(py),
        scale.into_pyarray(py),
        mask_arr.into_pyarray(py),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mercator_roundtrip_and_blake2_determinism() {
        let (lon, lat) = (4.805, 43.949); // Avignon / Provence
        let (x, y) = to3857(lon, lat);
        let (lon2, lat2) = to4326(x, y);
        assert!((lon - lon2).abs() < 1e-9);
        assert!((lat - lat2).abs() < 1e-9);
        let u1 = blake2_u01(12345, -67890, 31);
        let u2 = blake2_u01(12345, -67890, 31);
        assert_eq!(u1, u2);
        assert!((0.0..1.0).contains(&u1));
        assert_ne!(u1, blake2_u01(12345, -67890, 32));
    }

    #[test]
    fn provence_hedgerow_samples_cypress_olive_and_shrubs_deterministically() {
        let bounds = (4.80, 43.94, 4.82, 43.96);
        let (x0, y1) = to3857(bounds.0, bounds.3);
        let (x1, y0) = to3857(bounds.2, bounds.1);
        let (n_r, n_c) = (256usize, 256usize);
        let dx = (x1 - x0) / (n_c as f64);
        let dy = (y1 - y0) / (n_r as f64);
        let hs = Array2::<f32>::zeros((n_r, n_c));
        let blocked = Array2::<bool>::from_elem((n_r, n_c), false);
        // A 120m hedgerow line across the cell in Provence
        let ll = Array2::from_shape_vec((2, 2), vec![4.805, 43.950, 4.807, 43.950]).unwrap();
        let off = [0i64, 2i64];
        let lh = [8.0f32];
        let out1 = hedgerows_core(
            &off,
            ll.view(),
            &lh,
            hs.view(),
            blocked.view(),
            (x0, y1, dx, dy),
            bounds,
            (25, 26, 0, 2),
            (0, 1),
        );
        let out2 = hedgerows_core(
            &off,
            ll.view(),
            &lh,
            hs.view(),
            blocked.view(),
            (x0, y1, dx, dy),
            bounds,
            (25, 26, 0, 2),
            (0, 1),
        );
        assert!(!out1.0.is_empty());
        assert_eq!(out1.0, out2.0);
        assert_eq!(out1.5, out2.5);
        assert!(out1.5.contains(&25), "cypress windbreak trees present");
        assert!(out1.5.iter().all(|&g| g == 25 || g == 26 || g == 2));
        assert!(out1.9.iter().any(|&m| m), "2-pixel dilated exclusion mask set");
    }
}
