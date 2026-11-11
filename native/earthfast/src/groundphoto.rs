//! L0 ground ("fake it until it loads"): a block's stand-in photo from its own ground layers, for a teleport's first
//! block whose aerial imagery is not on disk yet (a cold place: the photo took 4-10 s, the stand-in from the imagery
//! could not be made, and the car waited for it, 2026-11-11). Each sample's material colour, shaded by the slope
//! (a sun in the north-west, as the photos' shadows), enlarged bilinearly to the photo's size.

use numpy::{IntoPyArray, PyArray3, PyReadonlyArray2};
use pyo3::prelude::*;

/// layer (n, n) u8: a material index per sample (row 0 = south, the .ter's rows); z (n, n) f32: the heights (m);
/// palette (k, 3) u8: the colour of each index (an index past it: the last); sq: m between samples; px: the photo's
/// size. Returns rgb (px, px, 3) u8, row 0 = north (the base photo's rows), texel centres at (i + 0.5) * n / px.
#[pyfunction]
pub fn ground_photo<'py>(py: Python<'py>, layer: PyReadonlyArray2<'py, u8>, z: PyReadonlyArray2<'py, f32>,
                         palette: PyReadonlyArray2<'py, u8>, sq: f64, px: usize, shade: f64)
                         -> PyResult<Bound<'py, PyArray3<u8>>> {
    let (layer, z, palette) = (layer.as_array(), z.as_array(), palette.as_array());
    let n = layer.shape()[0];
    if n < 2 || layer.shape()[1] != n || z.shape() != layer.shape() || palette.shape()[0] == 0
        || palette.shape()[1] < 3 || px == 0 {
        return Err(pyo3::exceptions::PyValueError::new_err("ground_photo: bad shapes"));
    }
    let k = palette.shape()[0];
    let pal: Vec<[f32; 3]> = (0..k).map(|i| [palette[[i, 0]] as f32, palette[[i, 1]] as f32, palette[[i, 2]] as f32])
        .collect();
    let layer = layer.to_owned();
    let z = z.to_owned();
    let out = py.detach(move || {
        // the samples' colours, shaded: Lambert of a sun at 45 deg elevation from the north-west, normalised to 1 on
        // flat ground and blended in by `shade`
        let (sx, sy, sz) = (-0.5f32, 0.5f32, 0.70710677f32);
        let mut col = vec![[0.0f32; 3]; n * n];
        for r in 0..n {
            for c in 0..n {
                let zl = z[[r, c.saturating_sub(1)]];
                let zr = z[[r, (c + 1).min(n - 1)]];
                let zd = z[[r.saturating_sub(1), c]];
                let zu = z[[(r + 1).min(n - 1), c]];
                let dx = (zr - zl) / (((c + 1).min(n - 1) - c.saturating_sub(1)) as f32 * sq as f32);
                let dy = (zu - zd) / (((r + 1).min(n - 1) - r.saturating_sub(1)) as f32 * sq as f32);
                let len = (dx * dx + dy * dy + 1.0).sqrt();
                let lam = ((-dx * sx - dy * sy + sz) / len / sz).max(0.0);
                let f = 1.0 + shade as f32 * (lam - 1.0);
                let p = pal[(layer[[r, c]] as usize).min(k - 1)];
                col[r * n + c] = [p[0] * f, p[1] * f, p[2] * f];
            }
        }
        let mut rgb = vec![0u8; px * px * 3];
        let scale = n as f64 / px as f64;
        for i in 0..px {
            // texel row i (north first) -> sample row (south first)
            let gy = ((px - 1 - i) as f64 + 0.5) * scale - 0.5;
            let r0 = gy.floor().clamp(0.0, (n - 1) as f64) as usize;
            let r1 = (r0 + 1).min(n - 1);
            let fy = (gy - r0 as f64).clamp(0.0, 1.0) as f32;
            for j in 0..px {
                let gx = (j as f64 + 0.5) * scale - 0.5;
                let c0 = gx.floor().clamp(0.0, (n - 1) as f64) as usize;
                let c1 = (c0 + 1).min(n - 1);
                let fx = (gx - c0 as f64).clamp(0.0, 1.0) as f32;
                let (a, b, cc, d) = (col[r0 * n + c0], col[r0 * n + c1], col[r1 * n + c0], col[r1 * n + c1]);
                for ch in 0..3 {
                    let top = a[ch] * (1.0 - fx) + b[ch] * fx;
                    let bot = cc[ch] * (1.0 - fx) + d[ch] * fx;
                    rgb[(i * px + j) * 3 + ch] = (top * (1.0 - fy) + bot * fy).round().clamp(0.0, 255.0) as u8;
                }
            }
        }
        rgb
    });
    let arr = numpy::ndarray::Array3::from_shape_vec((px, px, 3), out)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
    Ok(arr.into_pyarray(py))
}
