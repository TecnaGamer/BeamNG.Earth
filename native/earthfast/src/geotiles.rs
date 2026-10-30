//! Origin-independent tiles ("geotiles", .claude/work/geotiles_plan.md): a global Web-Mercator tile grid whose content
//! is made once in each tile's own frame (the ENU tangent plane at the tile's centre) and placed into any anchor's world.
//!
//! The world convention of a frame F (server roads.py / heightmap, mod geodesy.lua geodeticToWorld): a place's x, y are
//! the east / north of its point at h = 0, its z the up of the point at its height. world_map takes points of one
//! frame's world into another's exactly (world -> lat / lon / h -> world); placement gives the rigid move (rotation R,
//! translation t, made exact at a pivot) and the shear c that carries a point's height difference from the pivot
//! (p' = R p + t + c (p.z - pivot.z)): what the mod applies to a Forest or a shape placed from its tile's frame.
//!
//! The grid's arithmetic is mirrored in the mod (geotile.lua); the keys and the tile corners agree bit for bit (the
//! same IEEE operations; tests/test_geotiles.py runs both).

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;
use std::f64::consts::PI;

const A: f64 = 6378137.0;
const F: f64 = 1.0 / 298.257223563;
const B: f64 = A * (1.0 - F);
const E2: f64 = F * (2.0 - F);
const EP2: f64 = E2 / (1.0 - E2);
pub const MAX_LAT: f64 = 85.0511287798066;
const EARTH_CIRC: f64 = 40075016.686;

pub fn geo_to_ecef(lat: f64, lon: f64, h: f64) -> [f64; 3] {
    let (phi, lam) = (lat.to_radians(), lon.to_radians());
    let (sl, cl) = (phi.sin(), phi.cos());
    let nr = A / (1.0 - E2 * sl * sl).sqrt();
    let rr = (nr + h) * cl;
    [rr * lam.cos(), rr * lam.sin(), (nr * (1.0 - E2) + h) * sl]
}

/// Heikkinen's closed form, as lib.rs enu_to_geodetic (the polar axis handled).
pub fn ecef_to_geo(x: f64, y: f64, z: f64) -> (f64, f64, f64) {
    let p2 = x * x + y * y;
    let p = p2.sqrt();
    let z2 = z * z;
    let (la, h);
    if p < 1e-6 {
        la = if z >= 0.0 { 90.0 } else { -90.0 };
        h = z.abs() - B;
    } else {
        let f_ = 54.0 * B * B * z2;
        let g = p2 + (1.0 - E2) * z2 - E2 * (A * A - B * B);
        let c = E2 * E2 * f_ * p2 / (g * g * g);
        let s = (1.0 + c + (c * c + 2.0 * c).sqrt()).cbrt();
        let k = s + 1.0 + 1.0 / s;
        let pp = f_ / (3.0 * k * k * g * g);
        let q = (1.0 + 2.0 * E2 * E2 * pp).sqrt();
        let r0 = -pp * E2 * p / (1.0 + q)
            + (0.5 * A * A * (1.0 + 1.0 / q) - pp * (1.0 - E2) * z2 / (q * (1.0 + q)) - 0.5 * pp * p2).max(0.0).sqrt();
        let t = (p - E2 * r0) * (p - E2 * r0);
        let uu = (t + z2).sqrt();
        let vv = (t + (1.0 - E2) * z2).sqrt();
        let z0 = B * B * z / (A * vv);
        h = uu * (1.0 - B * B / (A * vv));
        la = (z + EP2 * z0).atan2(p).to_degrees();
    }
    (la, y.atan2(x).to_degrees(), h)
}

/// An ENU frame tangent to the ellipsoid at (lat, lon, 0): origin in ECEF, rows E, N, U.
#[derive(Clone, Copy)]
pub struct Frame {
    o: [f64; 3],
    r: [[f64; 3]; 3],
}

impl Frame {
    pub fn new(lat: f64, lon: f64) -> Frame {
        let (la, lo) = (lat.to_radians(), lon.to_radians());
        let (sla, cla, slo, clo) = (la.sin(), la.cos(), lo.sin(), lo.cos());
        Frame {
            o: geo_to_ecef(lat, lon, 0.0),
            r: [[-slo, clo, 0.0], [-sla * clo, -sla * slo, cla], [cla * clo, cla * slo, sla]],
        }
    }

    pub fn to_enu(&self, p: [f64; 3]) -> [f64; 3] {
        let (dx, dy, dz) = (p[0] - self.o[0], p[1] - self.o[1], p[2] - self.o[2]);
        let r = &self.r;
        [r[0][0] * dx + r[0][1] * dy + r[0][2] * dz,
         r[1][0] * dx + r[1][1] * dy + r[1][2] * dz,
         r[2][0] * dx + r[2][1] * dy + r[2][2] * dz]
    }

    pub fn from_enu(&self, e: [f64; 3]) -> [f64; 3] {
        let r = &self.r;
        [self.o[0] + r[0][0] * e[0] + r[1][0] * e[1] + r[2][0] * e[2],
         self.o[1] + r[0][1] * e[0] + r[1][1] * e[1] + r[2][1] * e[2],
         self.o[2] + r[0][2] * e[0] + r[1][2] * e[1] + r[2][2] * e[2]]
    }

    /// The world convention: x, y of the place's point at h = 0, z the up of the point at its height.
    pub fn geodetic_to_world(&self, lat: f64, lon: f64, h: f64) -> [f64; 3] {
        let g = self.to_enu(geo_to_ecef(lat, lon, 0.0));
        let u = if h == 0.0 { g[2] } else { self.to_enu(geo_to_ecef(lat, lon, h))[2] };
        [g[0], g[1], u]
    }

    /// The exact inverse of geodetic_to_world: lat / lon of the place whose h = 0 point has these x, y (the plane
    /// point (x, y, 0) lies d^2 / 2R over the ellipsoid: three corrections), h whose up is z (Newton on the up, whose
    /// rate is the cosine between the frame's up and the place's normal).
    pub fn world_to_geodetic(&self, x: f64, y: f64, z: f64) -> (f64, f64, f64) {
        let (mut qx, mut qy) = (x, y);
        let (mut lat, mut lon, _) = {
            let p = self.from_enu([qx, qy, 0.0]);
            ecef_to_geo(p[0], p[1], p[2])
        };
        for _ in 0..3 {
            let g = self.to_enu(geo_to_ecef(lat, lon, 0.0));
            qx += x - g[0];
            qy += y - g[1];
            let p = self.from_enu([qx, qy, 0.0]);
            let got = ecef_to_geo(p[0], p[1], p[2]);
            lat = got.0;
            lon = got.1;
        }
        let (phi, lam) = (lat.to_radians(), lon.to_radians());
        let nrm = [phi.cos() * lam.cos(), phi.cos() * lam.sin(), phi.sin()];
        let rate = self.r[2][0] * nrm[0] + self.r[2][1] * nrm[1] + self.r[2][2] * nrm[2];
        let mut h = {
            let p = self.from_enu([x, y, z]);
            ecef_to_geo(p[0], p[1], p[2]).2
        };
        for _ in 0..2 {
            let u = self.to_enu(geo_to_ecef(lat, lon, h))[2];
            h += (z - u) / rate;
        }
        (lat, lon, h)
    }

    /// The rigid ENU move into other: p_other = R p + t (R = E_o E_s^T, t = E_o (O_s - O_o)).
    pub fn rigid_to(&self, other: &Frame) -> ([[f64; 3]; 3], [f64; 3]) {
        let (a, b) = (&self.r, &other.r);
        let mut r = [[0.0f64; 3]; 3];
        for i in 0..3 {
            for j in 0..3 {
                r[i][j] = b[i][0] * a[j][0] + b[i][1] * a[j][1] + b[i][2] * a[j][2];
            }
        }
        let d = [self.o[0] - other.o[0], self.o[1] - other.o[1], self.o[2] - other.o[2]];
        let t = [b[0][0] * d[0] + b[0][1] * d[1] + b[0][2] * d[2],
                 b[1][0] * d[0] + b[1][1] * d[1] + b[1][2] * d[2],
                 b[2][0] * d[0] + b[2][1] * d[1] + b[2][2] * d[2]];
        (r, t)
    }
}

/// One point of src's world in dst's world, exactly.
pub fn world_map_pt(src: &Frame, dst: &Frame, p: [f64; 3]) -> [f64; 3] {
    let (la, lo, h) = src.world_to_geodetic(p[0], p[1], p[2]);
    dst.geodetic_to_world(la, lo, h)
}

/// The placement of src's content in dst's world at pivot (src world): (R, t, c), p' = R p + t + c (p.z - pivot.z),
/// exact at the pivot, c the shear per metre of height (central difference over +-DZ m: the map is smooth).
pub fn placement_of(src: &Frame, dst: &Frame, pivot: [f64; 3]) -> ([[f64; 3]; 3], [f64; 3], [f64; 3]) {
    let (r, mut t) = src.rigid_to(dst);
    let rig = |p: [f64; 3], t: &[f64; 3]| -> [f64; 3] {
        [r[0][0] * p[0] + r[0][1] * p[1] + r[0][2] * p[2] + t[0],
         r[1][0] * p[0] + r[1][1] * p[1] + r[1][2] * p[2] + t[1],
         r[2][0] * p[0] + r[2][1] * p[1] + r[2][2] * p[2] + t[2]]
    };
    let w = world_map_pt(src, dst, pivot);
    let rp = rig(pivot, &t);
    for k in 0..3 {
        t[k] += w[k] - rp[k];
    }
    const DZ: f64 = 50.0;
    let up = world_map_pt(src, dst, [pivot[0], pivot[1], pivot[2] + DZ]);
    let dn = world_map_pt(src, dst, [pivot[0], pivot[1], pivot[2] - DZ]);
    let mut c = [0.0f64; 3];
    for k in 0..3 {
        c[k] = (up[k] - dn[k]) / (2.0 * DZ) - r[k][2];
    }
    (r, t, c)
}

// --- the grid ----------------------------------------------------------------------------------------------------

/// The tile (x, y) of lat / lon at zoom z: x east from -180, y south from +MAX_LAT (geotile.lua M.tileOf: the same
/// operations). Clamped into the grid.
pub fn tile_of(lat: f64, lon: f64, z: u32) -> (i64, i64) {
    let n = (1u64 << z) as f64;
    let la = lat.max(-MAX_LAT).min(MAX_LAT) * PI / 180.0;
    let xf = (lon + 180.0) / 360.0 * n;
    let yf = (1.0 - (la.tan() + 1.0 / la.cos()).ln() / PI) / 2.0 * n;
    let top = n - 1.0;
    (xf.floor().max(0.0).min(top) as i64, yf.floor().max(0.0).min(top) as i64)
}

/// Latitude of the tile row edge yf (0 = the north edge) at zoom z.
pub fn lat_of(yf: f64, z: u32) -> f64 {
    let n = (1u64 << z) as f64;
    (PI * (1.0 - 2.0 * yf / n)).sinh().atan() * 180.0 / PI
}

pub fn lon_of(xf: f64, z: u32) -> f64 {
    let n = (1u64 << z) as f64;
    xf / n * 360.0 - 180.0
}

/// (lat, lon) of a tile's centre (its frame's origin).
pub fn centre(z: u32, x: i64, y: i64) -> (f64, f64) {
    (lat_of(y as f64 + 0.5, z), lon_of(x as f64 + 0.5, z))
}

/// (south, west, north, east) of a tile.
pub fn bounds(z: u32, x: i64, y: i64) -> (f64, f64, f64, f64) {
    (lat_of(y as f64 + 1.0, z), lon_of(x as f64, z), lat_of(y as f64, z), lon_of(x as f64 + 1.0, z))
}

/// The tile's corners in frame f's world at h = 0: SW, SE, NE, NW.
pub fn quad(f: &Frame, z: u32, x: i64, y: i64) -> [[f64; 2]; 4] {
    let (s, w, n, e) = bounds(z, x, y);
    let mut q = [[0.0f64; 2]; 4];
    for (k, (la, lo)) in [(s, w), (s, e), (n, e), (n, w)].iter().enumerate() {
        let p = f.geodetic_to_world(*la, *lo, 0.0);
        q[k] = [p[0], p[1]];
    }
    q
}

/// m from (px, py) to a convex quad (0 inside).
pub fn quad_dist(q: &[[f64; 2]; 4], px: f64, py: f64) -> f64 {
    let mut inside = true;
    let mut best = f64::INFINITY;
    let mut sign = 0.0f64;
    for k in 0..4 {
        let (a, b) = (q[k], q[(k + 1) % 4]);
        let (dx, dy) = (b[0] - a[0], b[1] - a[1]);
        let cr = dx * (py - a[1]) - dy * (px - a[0]);
        if cr != 0.0 {
            if sign == 0.0 {
                sign = cr.signum();
            } else if cr.signum() != sign {
                inside = false;
            }
        }
        let l2 = (dx * dx + dy * dy).max(1e-12);
        let t = (((px - a[0]) * dx + (py - a[1]) * dy) / l2).clamp(0.0, 1.0);
        best = best.min((px - (a[0] + t * dx)).hypot(py - (a[1] + t * dy)));
    }
    if inside { 0.0 } else { best }
}

/// The tiles of zoom z whose quad in anchor f's world comes within r of (x, y), nearest first (ties by x, y).
pub fn near(f: &Frame, z: u32, x: f64, y: f64, r: f64) -> Vec<(i64, i64, f64)> {
    let (la, lo, _) = f.world_to_geodetic(x, y, 0.0);
    let (kx, ky) = tile_of(la, lo, z);
    let n = 1i64 << z;
    let side = EARTH_CIRC * la.to_radians().cos().max(0.01) / n as f64;
    let rings = (r / side).ceil() as i64 + 1;
    let mut out = Vec::new();
    for dy in -rings..=rings {
        let ty = ky + dy;
        if ty < 0 || ty >= n {
            continue;
        }
        for dx in -rings..=rings {
            let tx = (kx + dx).rem_euclid(n);
            let d = quad_dist(&quad(f, z, tx, ty), x, y);
            if d <= r {
                out.push((tx, ty, d));
            }
        }
    }
    out.sort_by(|a, b| a.2.partial_cmp(&b.2).unwrap().then(a.0.cmp(&b.0)).then(a.1.cmp(&b.1)));
    out
}

// --- Python ------------------------------------------------------------------------------------------------------

/// geotiles.tiles_of: the zoom-z tile (x, y) of every point (lat, lon arrays, degrees).
#[pyfunction]
pub fn geo_tile_of<'py>(py: Python<'py>, lat: PyReadonlyArray1<'py, f64>, lon: PyReadonlyArray1<'py, f64>, z: u32)
                        -> (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>) {
    let (la, lo) = (lat.as_array(), lon.as_array());
    let m = la.len();
    let (xs, ys): (Vec<i64>, Vec<i64>) = py.allow_threads(|| (0..m).map(|i| tile_of(la[i], lo[i], z)).unzip());
    (xs.into_pyarray(py), ys.into_pyarray(py))
}

/// geotiles: (centre lat, lon, south, west, north, east) of tile (z, x, y).
#[pyfunction]
pub fn geo_tile_info(z: u32, x: i64, y: i64) -> (f64, f64, f64, f64, f64, f64) {
    let (cl, cn) = centre(z, x, y);
    let (s, w, n, e) = bounds(z, x, y);
    (cl, cn, s, w, n, e)
}

/// geotiles.quad: tile (z, x, y)'s corners (4, 2) in the world of the frame at (lat0, lon0).
#[pyfunction]
pub fn geo_tile_quad<'py>(py: Python<'py>, lat0: f64, lon0: f64, z: u32, x: i64, y: i64) -> Bound<'py, PyArray2<f64>> {
    let q = quad(&Frame::new(lat0, lon0), z, x, y);
    numpy::ndarray::Array2::from_shape_vec((4, 2), q.iter().flatten().copied().collect()).unwrap().into_pyarray(py)
}

/// geotiles.quad_dist: m from (x, y) to a quad (4, 2) (0 inside).
#[pyfunction]
pub fn geo_quad_dist(q: PyReadonlyArray2<'_, f64>, x: f64, y: f64) -> f64 {
    let a = q.as_array();
    let mut qq = [[0.0f64; 2]; 4];
    for k in 0..4 {
        qq[k] = [a[[k, 0]], a[[k, 1]]];
    }
    quad_dist(&qq, x, y)
}

/// geotiles.tiles_near: [(x, y, m)] of the zoom-z tiles within r of (x, y) in the world of the frame (lat0, lon0).
#[pyfunction]
pub fn geo_tiles_near(py: Python<'_>, lat0: f64, lon0: f64, z: u32, x: f64, y: f64, r: f64) -> Vec<(i64, i64, f64)> {
    let f = Frame::new(lat0, lon0);
    py.allow_threads(|| near(&f, z, x, y, r))
}

/// geotiles.world_map: points (n, 3) of the world of the frame (slat, slon) in that of (dlat, dlon), exactly.
#[pyfunction]
pub fn geo_world_map<'py>(py: Python<'py>, slat: f64, slon: f64, dlat: f64, dlon: f64,
                          pts: PyReadonlyArray2<'py, f64>) -> Bound<'py, PyArray2<f64>> {
    let (s, d) = (Frame::new(slat, slon), Frame::new(dlat, dlon));
    let p = pts.as_array();
    let (m, k) = (p.nrows(), p.ncols());
    let out: Vec<f64> = py.allow_threads(|| {
        let mut out = Vec::with_capacity(m * 3);
        for i in 0..m {
            let z = if k > 2 { p[[i, 2]] } else { 0.0 };
            let w = if slat == dlat && slon == dlon { [p[[i, 0]], p[[i, 1]], z] } else {
                world_map_pt(&s, &d, [p[[i, 0]], p[[i, 1]], z]) };
            out.extend_from_slice(&w);
        }
        out
    });
    numpy::ndarray::Array2::from_shape_vec((m, 3), out).unwrap().into_pyarray(py)
}

/// geotiles: the world of the frame (lat0, lon0) at points (n, 3) -> (lat, lon, h) (the exact inverse of
/// geodetic_to_world).
#[pyfunction]
pub fn geo_world_to_geodetic<'py>(py: Python<'py>, lat0: f64, lon0: f64, pts: PyReadonlyArray2<'py, f64>)
                                  -> (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>) {
    let f = Frame::new(lat0, lon0);
    let p = pts.as_array();
    let (m, k) = (p.nrows(), p.ncols());
    let (la, lo, h) = py.allow_threads(|| {
        let (mut la, mut lo, mut h) = (Vec::with_capacity(m), Vec::with_capacity(m), Vec::with_capacity(m));
        for i in 0..m {
            let g = f.world_to_geodetic(p[[i, 0]], p[[i, 1]], if k > 2 { p[[i, 2]] } else { 0.0 });
            la.push(g.0);
            lo.push(g.1);
            h.push(g.2);
        }
        (la, lo, h)
    });
    (la.into_pyarray(py), lo.into_pyarray(py), h.into_pyarray(py))
}

/// geotiles.placement: (R as 9 numbers row-major, t (3), c (3)) placing the world of the frame (slat, slon) in that of
/// (dlat, dlon) at pivot (px, py, pz) of the source: p' = R p + t + c (p.z - pz).
#[pyfunction]
pub fn geo_placement(slat: f64, slon: f64, dlat: f64, dlon: f64, px: f64, py_: f64, pz: f64)
                     -> (Vec<f64>, Vec<f64>, Vec<f64>) {
    let (r, t, c) = placement_of(&Frame::new(slat, slon), &Frame::new(dlat, dlon), [px, py_, pz]);
    (r.iter().flatten().copied().collect(), t.to_vec(), c.to_vec())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn world_round_trip() {
        let f = Frame::new(39.2834, -76.5934);
        for &(la, lo, h) in &[(39.29, -76.40, 1650.0), (39.10, -76.80, -20.0), (39.2834, -76.5934, 0.0)] {
            let w = f.geodetic_to_world(la, lo, h);
            let (a, b, c) = f.world_to_geodetic(w[0], w[1], w[2]);
            let w2 = f.geodetic_to_world(a, b, c);
            for k in 0..3 {
                assert!((w2[k] - w[k]).abs() < 1e-7, "{k}: {} {}", w2[k], w[k]);
            }
        }
    }

    #[test]
    fn placement_exact_near_pivot() {
        let (s, d) = (Frame::new(39.30, -76.60), Frame::new(39.30, -76.43));
        let piv = [0.0, 0.0, 1650.0];
        let (r, t, c) = placement_of(&s, &d, piv);
        for &p in &[[100.0, -80.0, 1700.0], [-120.0, 110.0, 1550.0], [0.0, 0.0, 1650.0]] {
            let w = world_map_pt(&s, &d, p);
            let mut q = [0.0; 3];
            for k in 0..3 {
                q[k] = r[k][0] * p[0] + r[k][1] * p[1] + r[k][2] * p[2] + t[k] + c[k] * (p[2] - piv[2]);
            }
            for k in 0..3 {
                assert!((q[k] - w[k]).abs() < 2e-4, "{k}: {} {}", q[k], w[k]);
            }
        }
    }

    #[test]
    fn tiles_partition() {
        let (x, y) = tile_of(39.2834, -76.5934, 17);
        let (s, w, n, e) = bounds(17, x, y);
        assert!(s <= 39.2834 && 39.2834 < n && w <= -76.5934 && -76.5934 < e);
        let f = Frame::new(39.2834, -76.5934);
        let got = near(&f, 17, 0.0, 0.0, 0.0);
        assert_eq!(got.len(), 1);
        assert_eq!((got[0].0, got[0].1), (x, y));
    }
}
