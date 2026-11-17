//! Neutral additive ground detail, derived privately from installed stock PNGs.
//! Broad albedo shading is removed before use so it cannot tint entire cliffs.
use crate::{
    dm::Dm,
    texdds::{data_dds, decode_image},
};
use pyo3::{exceptions::PyValueError, prelude::*, types::PyBytes};

const DETAIL_STD: f64 = 0.065;
const MIN_STD: f64 = 1e-6;
const LUMA: [f64; 3] = [0.2126, 0.7152, 0.0722];

fn linear(value: u8) -> f64 {
    let x = value as f64 / 255.0;
    if x <= 0.04045 {
        x / 12.92
    } else {
        ((x + 0.055) / 1.055).dpowf(2.4)
    }
}

/// A separable, periodic Gaussian keeps the authored texture's wrap seamless.
fn blur(values: &[f64], w: usize, h: usize) -> Vec<f64> {
    let sigma = w as f64 / 64.0;
    let radius = (4.0 * sigma).ceil() as isize;
    let mut kernel: Vec<f64> = (-radius..=radius)
        .map(|i| (-(i * i) as f64 / (2.0 * sigma * sigma)).dexp())
        .collect();
    let total: f64 = kernel.iter().sum();
    for k in &mut kernel {
        *k /= total;
    }
    let mut horizontal = vec![0.0; w * h];
    let mut result = vec![0.0; w * h];
    for y in 0..h {
        for x in 0..w {
            for (i, k) in kernel.iter().enumerate() {
                let xx = (x as isize + i as isize - radius).rem_euclid(w as isize) as usize;
                horizontal[y * w + x] += values[y * w + xx] * k;
            }
        }
    }
    for y in 0..h {
        for x in 0..w {
            for (i, k) in kernel.iter().enumerate() {
                let yy = (y as isize + i as isize - radius).rem_euclid(h as isize) as usize;
                result[y * w + x] += horizontal[yy * w + x] * k;
            }
        }
    }
    result
}

fn detail(w: usize, h: usize, channels: usize, pixels: &[u8]) -> Vec<u8> {
    let luminance: Vec<f64> = pixels
        .chunks_exact(channels)
        .map(|p| {
            (0..3)
                .map(|c| linear(p[if channels == 1 { 0 } else { c }]) * LUMA[c])
                .sum()
        })
        .collect();
    let low = blur(&luminance, w, h);
    let mut values: Vec<f64> = luminance.iter().zip(low).map(|(v, l)| (v - l) / (l + 0.1)).collect();
    let count = values.len() as f64;
    let mean = values.iter().sum::<f64>() / count;
    let std = (values.iter().map(|v| (v - mean).sq()).sum::<f64>() / count)
        .sqrt()
        .max(MIN_STD);
    for v in &mut values {
        *v = (0.5 + (*v - mean) * DETAIL_STD / std).clamp(0.15, 0.85);
    }
    let offset = 0.5 - values.iter().sum::<f64>() / count;
    values
        .iter()
        .flat_map(|v| [((*v + offset) * 255.0).round_ties_even().clamp(0.0, 255.0) as u8; 3])
        .collect()
}

fn scalar(source: &[u8], ao: bool) -> Result<Vec<u8>, String> {
    let (w, h, channels, pixels) = decode_image(source)?;
    let values: Vec<f64> = pixels.chunks_exact(channels).map(|p| p[0] as f64 / 255.0).collect();
    let mean = values.iter().sum::<f64>() / values.len() as f64;
    let bytes = values
        .iter()
        .map(|v| {
            // Floor AO so quantization never moves neutral .5 above the no-op.
            if ao {
                (v * 0.5 * 255.0).floor() as u8
            } else {
                ((0.5 + 0.5 * (v - mean)).clamp(0.0, 1.0) * 255.0).round_ties_even() as u8
            }
        })
        .collect();
    data_dds(w, h, 1, bytes)
}

/// RGB BC7 detail plus BC4 AO/roughness, with complete data-space mip chains.
#[pyfunction]
pub fn cliff_ground_maps<'py>(
    py: Python<'py>,
    color: &[u8],
    ao: &[u8],
    roughness: &[u8],
) -> PyResult<(Bound<'py, PyBytes>, Bound<'py, PyBytes>, Bound<'py, PyBytes>)> {
    let result = py
        .detach(|| -> Result<_, String> {
            let (w, h, channels, pixels) = decode_image(color)?;
            if !w.is_power_of_two() || !h.is_power_of_two() {
                return Err("power-of-two stock colour required".into());
            }
            let color = data_dds(w, h, 3, detail(w, h, channels, &pixels))?;
            Ok((color, scalar(ao, true)?, scalar(roughness, false)?))
        })
        .map_err(PyValueError::new_err)?;
    Ok((
        PyBytes::new(py, &result.0),
        PyBytes::new(py, &result.1),
        PyBytes::new(py, &result.2),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn flat_albedo_becomes_neutral_rgb() {
        assert_eq!(detail(8, 8, 3, &[64, 128, 192].repeat(64)), vec![128; 8 * 8 * 3]);
    }
    #[test]
    fn periodic_filter_preserves_wrap_symmetry_and_energy() {
        let mut source = vec![0.0; 128 * 8];
        source[0] = 1.0;
        let filtered = blur(&source, 128, 8);
        assert!((filtered.iter().sum::<f64>() - 1.0).abs() < 1e-12);
        assert!((filtered[1] - filtered[127]).abs() < 1e-12);
        assert!((filtered[128] - filtered[7 * 128]).abs() < 1e-12);
    }
}
