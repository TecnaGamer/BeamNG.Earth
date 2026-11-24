//! Private stock-derived contact maps, made before streaming. No stock data is
//! compiled into the module; Python reads it from the player's installation.
use crate::{
    dm::Dm,
    texdds::{decode_image, scalar_levels_dds},
};
use pyo3::{exceptions::PyValueError, prelude::*, types::PyBytes};

const ALPHA_REF: f64 = 224.0 / 255.0;
const EROSION_BASE: f64 = 0.505;
const EROSION_RANGE: f64 = 0.52;
const HEIGHT_FLOOR: f64 = 0.51;
const LOOKUP_WIDTH: usize = 1024;
const LOOKUP_HEIGHT: usize = 8;

/// Remove the stock albedo's average tint before applying satellite vertex
/// colours. Average in linear light, then encode the multiplier for Material.
/// Zero-energy channels cannot be neutralized and must not produce infinity.
fn neutral_factor(channels: usize, pixels: &[u8]) -> Result<[f64; 4], String> {
    let count = pixels.len() / channels;
    let mut mean = [0.0; 3];
    for pixel in pixels.chunks_exact(channels) {
        for channel in 0..3 {
            let value = pixel[if channels == 1 { 0 } else { channel }] as f64 / 255.0;
            mean[channel] += if value <= 0.04045 {
                value / 12.92
            } else {
                ((value + 0.055) / 1.055).dpowf(2.4)
            };
        }
    }
    let mut factor = [1.0; 4];
    for channel in 0..3 {
        if mean[channel] <= 0.0 {
            return Err("stock albedo has a zero-energy colour channel".into());
        }
        factor[channel] = 1.055 * (count as f64 / mean[channel]).dpowf(1.0 / 2.4) - 0.055;
    }
    Ok(factor)
}

/// Compute the contact material multiplier without Python image dependencies.
#[pyfunction]
pub fn cliff_contact_color_factor(py: Python<'_>, source: &[u8]) -> PyResult<[f64; 4]> {
    py.detach(|| {
        let (_, _, channels, pixels) = decode_image(source)?;
        neutral_factor(channels, &pixels)
    })
    .map_err(PyValueError::new_err)
}

fn coverage_power() -> f64 {
    ALPHA_REF.dln() / EROSION_BASE.dln()
}
fn byte(value: f64) -> u8 {
    (value * 255.0).round_ties_even().clamp(0.0, 255.0) as u8
}

fn coverage_levels(mut w: usize, mut h: usize, mut height: Vec<f64>) -> Vec<(usize, usize, Vec<u8>)> {
    let min = height.iter().copied().fold(f64::INFINITY, f64::min);
    let max = height.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let span = (max - min).max(1.0);
    for value in &mut height {
        *value = HEIGHT_FLOOR + (1.0 - HEIGHT_FLOOR) * (*value - min) / span;
    }
    let mut levels = Vec::new();
    loop {
        levels.push((w, h, height.iter().map(|v| byte(v.dpowf(coverage_power()))).collect()));
        if w == 1 && h == 1 {
            break;
        }
        let (nw, nh) = ((w / 2).max(1), (h / 2).max(1));
        let mut next = Vec::with_capacity(nw * nh);
        for y in 0..nh {
            for x in 0..nw {
                let (x0, x1) = (x * 2, (x * 2 + 1).min(w - 1));
                let (y0, y1) = (y * 2, (y * 2 + 1).min(h - 1));
                // Filter source height before the power transform. Averaging
                // coverage instead changes the cutout boundary at each LOD.
                next.push(
                    (height[y0 * w + x0] + height[y0 * w + x1] + height[y1 * w + x0] + height[y1 * w + x1]) * 0.25,
                );
            }
        }
        (w, h, height) = (nw, nh, next);
    }
    levels
}

fn decode_height(source: &[u8]) -> Result<(usize, usize, Vec<f64>), String> {
    let mut decoder = png::Decoder::new(std::io::Cursor::new(source));
    decoder.set_transformations(png::Transformations::EXPAND);
    let mut reader = decoder.read_info().map_err(|e| e.to_string())?;
    let mut bytes = vec![0; reader.output_buffer_size()];
    let info = reader.next_frame(&mut bytes).map_err(|e| e.to_string())?;
    let (w, h) = (info.width as usize, info.height as usize);
    if !w.is_power_of_two() || !h.is_power_of_two() {
        return Err("power-of-two source height required".into());
    }
    let channels = info.color_type.samples();
    let pixels = &bytes[..info.buffer_size()];
    let height = match info.bit_depth {
        png::BitDepth::Sixteen => pixels
            .chunks_exact(channels * 2)
            .map(|p| u16::from_be_bytes([p[0], p[1]]) as f64)
            .collect(),
        png::BitDepth::Eight => pixels.chunks_exact(channels).map(|p| p[0] as f64).collect(),
        _ => return Err("unsupported source height depth".into()),
    };
    Ok((w, h, height))
}

/// Stock height PNG bytes to two BC4 DDS maps, preserving 16-bit source detail.
/// The lookup deliberately has no mips: UV1 is an erosion threshold, not space.
#[pyfunction]
pub fn cliff_contact_maps<'py>(py: Python<'py>, source: &[u8]) -> PyResult<(Bound<'py, PyBytes>, Bound<'py, PyBytes>)> {
    let result = py
        .detach(|| -> Result<_, String> {
            let (w, h, height) = decode_height(source)?;
            let coverage = scalar_levels_dds(coverage_levels(w, h, height))?;
            let row: Vec<u8> = (0..LOOKUP_WIDTH)
                .map(|x| {
                    let t = x as f64 / (LOOKUP_WIDTH - 1) as f64;
                    byte(0.5 * (EROSION_BASE / (EROSION_BASE + EROSION_RANGE * t)).dpowf(coverage_power()))
                })
                .collect();
            let threshold = scalar_levels_dds(vec![(LOOKUP_WIDTH, LOOKUP_HEIGHT, row.repeat(LOOKUP_HEIGHT))])?;
            Ok((coverage, threshold))
        })
        .map_err(PyValueError::new_err)?;
    Ok((PyBytes::new(py, &result.0), PyBytes::new(py, &result.1)))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn neutral_color_averages_linear_light() {
        let factor = neutral_factor(3, &[0, 255, 128, 255, 255, 128]).unwrap();
        assert!((factor[1] - 1.0).abs() < 1e-12);
        assert!((factor[0] - (1.055 * 2.0_f64.dpowf(1.0 / 2.4) - 0.055)).abs() < 1e-12);
        assert!(factor[2] > factor[0]);
        assert_eq!(factor[3], 1.0);
        assert!(neutral_factor(3, &[0, 128, 255]).is_err());
    }
    #[test]
    fn mip_filters_height_before_coverage() {
        let levels = coverage_levels(2, 2, vec![0.0, 65535.0, 65535.0, 0.0]);
        assert_eq!(levels.len(), 2);
        assert_eq!(levels[1].2, vec![byte(0.755_f64.dpowf(coverage_power()))]);
        assert_ne!(
            levels[1].2[0],
            ((levels[0].2[0] as u16 + levels[0].2[1] as u16) / 2) as u8
        );
    }
    #[test]
    fn rectangular_source_reaches_single_texel() {
        let levels = coverage_levels(4, 2, vec![10.0; 8]);
        assert_eq!(
            levels.iter().map(|v| (v.0, v.1)).collect::<Vec<_>>(),
            vec![(4, 2), (2, 1), (1, 1)]
        );
    }
}
