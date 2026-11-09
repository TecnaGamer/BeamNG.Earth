//! The clouds seen from space (server spaceclouds.py): a satellite day's true-colour mosaic turned into a cloud layer's
//! colour and opacity, and the curved shell they are drawn on.
//!
//! cloud_layer(rgb (h, w, 3) u8) -> (rgb (h, w, 3) u8, opacity (h, w) u8): a pixel is cloud as it is bright and grey
//! (clouds and snow are white or grey; ground, sea and sand are darker or coloured): opacity = smoothstep of its
//! brightness over [B0, B1] times one minus smoothstep of its saturation over [S0, S1]. Its colour is the pixel's own,
//! lifted toward white where it is cloud (a cloud's thin edge over dark sea kept the sea's blue).
//!
//! shell_lonlat(lat0, lon0, radius_m, rings, segs) -> (lonlat (n, 2) f64, tris (m, 3) u32): a disc of `rings` rings
//! (denser near the centre: r = radius (i / rings)^1.5) by `segs` segments round (lat0, lon0) on the sphere (great-circle
//! destinations), the centre a vertex of its own; triangles counter-clockwise seen from above.
use numpy::{PyArray2, PyArray3, PyReadonlyArray3};
use pyo3::prelude::*;

const B0: f32 = 0.38;
const B1: f32 = 0.72;
const S0: f32 = 0.10;
const S1: f32 = 0.32;
const R_EARTH: f64 = 6_371_000.0;

fn smooth(x: f32, a: f32, b: f32) -> f32 {
    let t = ((x - a) / (b - a)).clamp(0.0, 1.0);
    t * t * (3.0 - 2.0 * t)
}

pub fn layer(rgb: &[u8]) -> (Vec<u8>, Vec<u8>) {
    let n = rgb.len() / 3;
    let mut col = vec![0u8; n * 3];
    let mut op = vec![0u8; n];
    for i in 0..n {
        let (r, g, b) = (rgb[3 * i] as f32 / 255.0, rgb[3 * i + 1] as f32 / 255.0, rgb[3 * i + 2] as f32 / 255.0);
        let mx = r.max(g).max(b);
        let mn = r.min(g).min(b);
        let bright = (r + g + b) / 3.0;
        let sat = if mx > 1e-6 { (mx - mn) / mx } else { 0.0 };
        let a = smooth(bright, B0, B1) * (1.0 - smooth(sat, S0, S1));
        op[i] = (a * 255.0).round() as u8;
        for (k, v) in [r, g, b].iter().enumerate() {
            let w = (*v + (1.0 - *v) * 0.35 * (1.0 - a)).min(1.0);     // (a thin edge: whiter than what is under it)
            col[3 * i + k] = (w.max(bright) * 255.0).round() as u8;
        }
    }
    (col, op)
}

pub fn shell(lat0: f64, lon0: f64, radius: f64, rings: usize, segs: usize) -> (Vec<[f64; 2]>, Vec<[u32; 3]>) {
    let (p1, l1) = (lat0.to_radians(), lon0.to_radians());
    let mut pts = vec![[lon0, lat0]];
    for i in 1..=rings {
        let d = radius * (i as f64 / rings as f64).powf(1.5) / R_EARTH;
        for j in 0..segs {
            let th = 2.0 * std::f64::consts::PI * j as f64 / segs as f64;
            let p2 = (p1.sin() * d.cos() + p1.cos() * d.sin() * th.cos()).clamp(-1.0, 1.0).asin();
            let l2 = l1 + (th.sin() * d.sin() * p1.cos()).atan2(d.cos() - p1.sin() * p2.sin());
            let mut lon = l2.to_degrees();
            lon = (lon + 540.0).rem_euclid(360.0) - 180.0;
            pts.push([lon, p2.to_degrees()]);
        }
    }
    let at = |i: usize, j: usize| -> u32 { (1 + (i - 1) * segs + j % segs) as u32 };
    let mut tris = Vec::new();
    for j in 0..segs {               // (bearing clockwise from north: east is j = segs / 4; counter-clockwise from above)
        tris.push([0, at(1, j + 1), at(1, j)]);
    }
    for i in 1..rings {
        for j in 0..segs {
            let (a, b, c, d) = (at(i, j), at(i, j + 1), at(i + 1, j), at(i + 1, j + 1));
            tris.push([a, b, d]);
            tris.push([a, d, c]);
        }
    }
    (pts, tris)
}

/// night_lights(rgb (h, w, 3) u8, lat (h,) per row, lon (w,) per column, sun_lat, sun_lon) -> (h, w, 3) u8: a night-
/// lights map's lights alone (its brightness over the dark base L0 it is drawn on, eased by LGAMMA so a town's faint
/// glow stays and a city's core still outshines its suburbs) times how dark it is there (the sun's elevation over the point: 1 below N0 degrees, 0 above N1), for an
/// additive emissive layer that shows the lit cities on the night side and fades across the terminator.
const L0: f32 = 0.11;
const LGAMMA: f32 = 0.6;
const N0: f64 = -6.0;
const N1: f64 = 3.0;
/// (the map's lights are a grey-white; from orbit the streets glow sodium orange: half the pixel's own colour, half its
/// brightness in this tint)
const WARM: [f32; 3] = [1.0, 0.72, 0.38];

pub fn lights(rgb: &[u8], lat: &[f64], lon: &[f64], sun_lat: f64, sun_lon: f64) -> Vec<u8> {
    let (h, w) = (lat.len(), lon.len());
    let mut out = vec![0u8; h * w * 3];
    let (sd, cd) = (sun_lat.to_radians().sin(), sun_lat.to_radians().cos());
    for r in 0..h {
        let (sl, cl) = (lat[r].to_radians().sin(), lat[r].to_radians().cos());
        for c in 0..w {
            let i = r * w + c;
            if 3 * i + 2 >= rgb.len() {
                break;
            }
            let el = (sl * sd + cl * cd * (lon[c] - sun_lon).to_radians().cos()).clamp(-1.0, 1.0).asin().to_degrees();
            let night = 1.0 - smooth(el as f32, N0 as f32, N1 as f32);
            if night <= 0.0 {
                continue;
            }
            let (pr, pg, pb) = (rgb[3 * i] as f32 / 255.0, rgb[3 * i + 1] as f32 / 255.0, rgb[3 * i + 2] as f32 / 255.0);
            let lum = (pr + pg + pb) / 3.0;
            let k = ((lum - L0) / (1.0 - L0)).clamp(0.0, 1.0).powf(LGAMMA) * night;
            for (j, v) in [pr, pg, pb].iter().enumerate() {
                let c = 0.5 * v + 0.5 * lum * WARM[j];
                out[3 * i + j] = (c * k * 255.0).round().min(255.0) as u8;
            }
        }
    }
    out
}

#[pyfunction]
pub fn night_lights<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, lat: numpy::PyReadonlyArray1<'py, f64>,
                         lon: numpy::PyReadonlyArray1<'py, f64>, sun_lat: f64, sun_lon: f64)
                         -> PyResult<Bound<'py, PyArray3<u8>>> {
    let a = rgb.as_array();
    let (h, w) = (a.shape()[0], a.shape()[1]);
    let (la, lo) = (lat.as_slice()?.to_vec(), lon.as_slice()?.to_vec());
    if a.shape()[2] != 3 || la.len() != h || lo.len() != w {
        return Err(pyo3::exceptions::PyValueError::new_err("rgb (h, w, 3), lat (h,), lon (w,)"));
    }
    let flat: Vec<u8> = a.iter().copied().collect();
    let out = py.detach(|| lights(&flat, &la, &lo, sun_lat, sun_lon));
    let out = numpy::ndarray::Array3::from_shape_vec((h, w, 3), out)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok(PyArray3::from_owned_array(py, out))
}

#[pyfunction]
pub fn cloud_layer<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>)
                        -> PyResult<(Bound<'py, PyArray3<u8>>, Bound<'py, PyArray2<u8>>)> {
    let a = rgb.as_array();
    let (h, w, c) = (a.shape()[0], a.shape()[1], a.shape()[2]);
    if c != 3 {
        return Err(pyo3::exceptions::PyValueError::new_err("rgb must be (h, w, 3)"));
    }
    let flat: Vec<u8> = a.iter().copied().collect();
    let (col, op) = py.detach(|| layer(&flat));
    let col = numpy::ndarray::Array3::from_shape_vec((h, w, 3), col)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let op = numpy::ndarray::Array2::from_shape_vec((h, w), op)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok((PyArray3::from_owned_array(py, col), PyArray2::from_owned_array(py, op)))
}

#[pyfunction]
pub fn shell_lonlat<'py>(py: Python<'py>, lat0: f64, lon0: f64, radius_m: f64, rings: usize, segs: usize)
                         -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<u32>>)> {
    if rings < 1 || segs < 3 || !(radius_m > 0.0) {
        return Err(pyo3::exceptions::PyValueError::new_err("rings >= 1, segs >= 3, radius > 0"));
    }
    let (pts, tris) = shell(lat0, lon0, radius_m, rings, segs);
    let p = numpy::ndarray::Array2::from_shape_vec((pts.len(), 2), pts.into_iter().flatten().collect())
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    let t = numpy::ndarray::Array2::from_shape_vec((tris.len(), 3), tris.into_iter().flatten().collect())
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok((PyArray2::from_owned_array(py, p), PyArray2::from_owned_array(py, t)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn white_cloud_dark_sea_coloured_ground() {
        let (_c, op) = layer(&[240, 240, 245, 20, 40, 80, 90, 120, 60, 210, 170, 120]);
        assert!(op[0] > 240, "a white cloud: {}", op[0]);
        assert_eq!(op[1], 0, "dark sea");
        assert_eq!(op[2], 0, "green ground");
        assert!(op[3] < 40, "bright sand is coloured: {}", op[3]);
    }

    #[test]
    fn lights_only_at_night() {
        let rgb = [255u8, 220, 150, 20, 20, 40];          // a city light, the dark base
        let night = lights(&rgb, &[45.0], &[10.0, 10.0], 0.0, -170.0);   // the sun on the far side: night
        assert!(night[0] > 180 && night[3] == 0, "{:?}", night);
        assert!(night[0] > night[1] && night[1] > night[2], "warm: {:?}", night);
        let day = lights(&rgb, &[45.0], &[10.0, 10.0], 0.0, 10.0);       // the sun nearly overhead: day
        assert_eq!(day[0], 0);
    }

    #[test]
    fn shell_rings_reach_the_radius() {
        let (pts, tris) = shell(45.0, 7.0, 1_000_000.0, 8, 16);
        assert_eq!(pts.len(), 1 + 8 * 16);
        assert_eq!(tris.len(), 16 + 7 * 16 * 2);
        let p = pts[1 + 7 * 16];                    // the outer ring, due north
        let dlat = (p[1] - 45.0).to_radians() * R_EARTH;
        assert!((dlat - 1_000_000.0).abs() < 1000.0, "{}", dlat);
    }
}
