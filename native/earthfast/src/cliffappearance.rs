//! Appearance rules shared by production generation and the read-only map preview.
use crate::dm::Dm;
use numpy::{
    ndarray::Array2, IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyUntypedArrayMethods,
};
use pyo3::{exceptions::PyValueError, prelude::*};

type Ring = Vec<(f64, f64)>;
type Rule = (i16, Vec<i32>, Vec<Ring>);
const LUMA: [f64; 3] = [0.2126, 0.7152, 0.0722];
const PALETTES: [[f64; 3]; 4] = [
    [245.0; 3],
    [255.0, 235.0, 205.0],
    [238.0, 238.0, 232.0],
    [235.0, 245.0, 255.0],
];

fn contains(lat: f64, lon: f64, ring: &[(f64, f64)]) -> bool {
    if ring.is_empty() {
        return false;
    }
    let mut inside = false;
    for i in 0..ring.len() {
        let (x0, y0) = ring[i];
        let (x1, y1) = ring[(i + 1) % ring.len()];
        if (y0 > lat) != (y1 > lat) && lon < (x1 - x0) * (lat - y0) / (y1 - y0) + x0 {
            inside = !inside;
        }
    }
    inside
}

fn family(lat: f64, lon: f64, biome: i32, rules: &[Rule]) -> i16 {
    if !lat.is_finite() || !lon.is_finite() {
        return -1;
    }
    let mut selected = -1;
    // Rules arrive in ascending (priority, stable key) order, from one registry.
    for (id, biomes, rings) in rules {
        if (biomes.is_empty() || biomes.contains(&biome))
            && (rings.is_empty() || rings.iter().any(|r| contains(lat, lon, r)))
        {
            selected = *id;
        }
    }
    selected
}

/// Select material families for (N,) latitude, longitude and biome arrays.
/// Rules are ordered by priority; -1 denotes an unmatched or invalid location.
#[pyfunction]
pub fn cliff_families<'py>(
    py: Python<'py>,
    lat: PyReadonlyArray1<'py, f64>,
    lon: PyReadonlyArray1<'py, f64>,
    biomes: PyReadonlyArray1<'py, i32>,
    rules: Vec<Rule>,
) -> PyResult<Bound<'py, PyArray1<i16>>> {
    if lat.len() != lon.len() || lat.len() != biomes.len() {
        return Err(PyValueError::new_err(
            "one latitude, longitude and biome per point required",
        ));
    }
    let (la, lo, b) = (lat.as_array(), lon.as_array(), biomes.as_array());
    let out: Vec<_> = py.detach(|| {
        la.iter()
            .zip(lo.iter())
            .zip(b.iter())
            .map(|((&a, &o), &b)| family(a, o, b, &rules))
            .collect()
    });
    Ok(out.into_pyarray(py))
}

pub fn linear(v: f64) -> f64 {
    if v <= 0.04045 {
        v / 12.92
    } else {
        ((v + 0.055) / 1.055).dpowf(2.4)
    }
}
pub fn srgb(v: f64) -> f64 {
    if v <= 0.0031308 {
        12.92 * v
    } else {
        1.055 * v.dpowf(1.0 / 2.4) - 0.055
    }
}
fn light(rgb: [f64; 3]) -> f64 {
    (0..3).map(|i| rgb[i] * LUMA[i]).sum()
}

/// Convert a flat array between sRGB and linear light, retaining HDR headroom.
#[pyfunction]
pub fn cliff_color_transfer<'py>(
    py: Python<'py>,
    values: PyReadonlyArray1<'py, f64>,
    encode: bool,
) -> Bound<'py, PyArray1<f64>> {
    let values = values.as_array();
    let out: Vec<_> = py.detach(|| {
        values
            .iter()
            .map(|&v| if encode { srgb(v.max(0.0)) } else { linear(v) })
            .collect()
    });
    out.into_pyarray(py)
}

fn material_gain(rgb: [f64; 3], reference: [f64; 3], amount: f64, hue: Option<f64>) -> [f64; 3] {
    let target = std::array::from_fn(|k| (1.0 - amount) * reference[k] + amount * rgb[k]);
    if let Some(hue) = hue {
        // Transfer luminance separately: full per-channel white balance can turn
        // pale layers green when the authored material's darker layers are red.
        let luminance = light(target).max(1e-8);
        let ref_light = light(reference);
        let chroma: [f64; 3] = std::array::from_fn(|k| {
            ((target[k] / luminance) / (reference[k] / ref_light))
                .clamp(0.5, 2.0)
                .dpowf(hue)
        });
        let adjusted_light = light(std::array::from_fn(|k| reference[k] * chroma[k]));
        chroma.map(|v| v * luminance / adjusted_light)
    } else {
        std::array::from_fn(|k| target[k] / reference[k])
    }
}

fn canyon_target(photo: [f64; 3], reference: [f64; 3], light_blend: f64) -> [f64; 3] {
    let rgb = photo.map(|v| v.clamp(0.0, 255.0));
    let luminance = light(rgb);
    let mut target = reference;
    if luminance > 35.0 && rgb[1] < rgb[0] * 1.12 && rgb[2] < rgb[0] * 1.12 {
        target =
            rgb.map(|v| linear(((0.7 * v + 0.3 * luminance) * 155.0 / luminance.max(1.0) / 255.0).clamp(0.0, 1.0)));
    }
    let target_light = light(target).max(1e-8);
    let luminance = (1.0 - light_blend) * light(reference) + light_blend * target_light;
    target.map(|v| v * luminance / target_light)
}

/// RGB samples (N, 3) to vertex RGBA8. Ordinary inputs are linear-light samples;
/// canyon inputs are photo bytes and have their baked shadows removed first.
/// Missing imagery uses a neutral multiplier cancelling material headroom.
#[pyfunction]
#[pyo3(signature = (rgb, reference, amount, headroom, exposure, present, hue=None, canyon_light=None))]
#[allow(clippy::too_many_arguments)]
pub fn cliff_material_tints<'py>(
    py: Python<'py>,
    rgb: PyReadonlyArray2<'py, f64>,
    reference: [f64; 3],
    amount: f64,
    headroom: f64,
    exposure: f64,
    present: bool,
    hue: Option<f64>,
    canyon_light: Option<f64>,
) -> PyResult<Bound<'py, PyArray2<u8>>> {
    let rgb = rgb.as_array();
    if rgb.ncols() != 3
        || reference.iter().any(|v| !v.is_finite() || *v <= 0.0)
        || !headroom.is_finite()
        || headroom <= 0.0
        || !exposure.is_finite()
        || !amount.is_finite()
        || hue.is_some_and(|v| !v.is_finite())
        || canyon_light.is_some_and(|v| !v.is_finite())
        || rgb.iter().any(|v| !v.is_finite())
    {
        return Err(PyValueError::new_err(
            "finite Nx3 colours, positive reference and headroom required",
        ));
    }
    let out = py.detach(|| {
        rgb.rows()
            .into_iter()
            .flat_map(|row| {
                let photo = [row[0], row[1], row[2]];
                let gain = if let Some(blend) = canyon_light {
                    material_gain(canyon_target(photo, reference, blend), reference, 1.0, hue).map(|v| v / headroom)
                } else if present {
                    material_gain(photo, reference, amount, hue).map(|v| v * exposure / headroom)
                } else {
                    [1.0 / headroom; 3]
                };
                let color = gain.map(|v| (srgb(v.clamp(0.0, 1.0)) * 255.0).round_ties_even() as u8);
                [color[0], color[1], color[2], 255]
            })
            .collect()
    });
    Ok(Array2::from_shape_vec((rgb.nrows(), 4), out).unwrap().into_pyarray(py))
}

fn rock_tint(biome: usize, photo: [f64; 3], family: i16, reference: [f64; 3]) -> [u8; 3] {
    let mut mineral = PALETTES[biome];
    let mut base = mineral.map(|v| v / 2.0);
    let rgb = photo.map(|v| v.clamp(0.0, 255.0));
    let luminance = light(rgb);
    // Reject vegetation, water and shadows; don't bake the photograph's lighting into the rock.
    if luminance > 35.0 && rgb[1] < rgb[0] * 1.12 && rgb[2] < rgb[0] * 1.12 {
        let target = 180.0 + 0.25 * (luminance.clamp(130.0, 210.0) - 180.0);
        let stone = rgb.map(|v| (0.7 * v + 0.3 * luminance) * target / luminance.max(1.0));
        mineral = stone;
        let scan = [155.0, 152.0, 147.0];
        for k in 0..3 {
            base[k] =
                (linear((stone[k] / 255.0).clamp(0.0, 1.0)) / linear(scan[k] / 255.0) / 2.0).clamp(0.12, 1.0) * 255.0;
        }
    }
    if family == 0 || family == 2 {
        let rendered = std::array::from_fn(|k| linear((base[k] / 255.0).clamp(0.0, 1.0)) * reference[k]);
        let target = mineral.map(|v| linear((v / 255.0).clamp(0.0, 1.0)));
        let gain = light(rendered) / light(target).max(1e-8);
        base = std::array::from_fn(|k| srgb((target[k] * gain / reference[k]).clamp(0.0, 1.0)) * 255.0);
    }
    base.map(|v| v.clamp(0.0, 255.0).round_ties_even() as u8)
}

/// Satellite-driven vertex tints: RGB samples (N, 3), family/biome indices (N,),
/// and linear-light material means (4, 3) produce sRGB byte colours (N, 3).
/// Optional Forest palette calibration is (canyon reference, luminance blend,
/// hue blend, headroom, byte step); without it the result remains a vertex tint.
#[pyfunction]
#[pyo3(signature = (biomes, rgb, families, means, palette=None))]
pub fn cliff_rock_tints<'py>(
    py: Python<'py>,
    biomes: PyReadonlyArray1<'py, i32>,
    rgb: PyReadonlyArray2<'py, f64>,
    families: PyReadonlyArray1<'py, i16>,
    means: PyReadonlyArray2<'py, f64>,
    palette: Option<([f64; 3], f64, f64, f64, f64)>,
) -> PyResult<Bound<'py, PyArray2<u8>>> {
    let n = biomes.len();
    if rgb.shape() != [n, 3] || families.len() != n || means.shape() != [4, 3] {
        return Err(PyValueError::new_err(
            "one RGB, biome and family per point and four RGB material means required",
        ));
    }
    let (b, c, f, m) = (biomes.as_array(), rgb.as_array(), families.as_array(), means.as_array());
    if let Some((reference, light, hue, headroom, step)) = palette {
        if reference.iter().any(|v| !v.is_finite() || *v <= 0.0)
            || [light, hue, headroom, step].iter().any(|v| !v.is_finite())
            || headroom <= 0.0
            || step <= 0.0
        {
            return Err(PyValueError::new_err(
                "finite canyon calibration and positive palette step/headroom required",
            ));
        }
    }
    if b.iter().any(|&v| !(0..4).contains(&v))
        || f.iter().any(|&v| !(0..4).contains(&v))
        || c.iter().any(|v| !v.is_finite())
        || m.iter().any(|v| !v.is_finite() || *v <= 0.0)
    {
        return Err(PyValueError::new_err(
            "finite colours, positive material means and valid family/biome indices required",
        ));
    }
    let out = py.detach(|| {
        (0..n)
            .flat_map(|i| {
                let photo = [c[[i, 0]], c[[i, 1]], c[[i, 2]]];
                let reference = [m[[f[i] as usize, 0]], m[[f[i] as usize, 1]], m[[f[i] as usize, 2]]];
                let mut tint = rock_tint(b[i] as usize, photo, f[i], reference);
                if let Some((canyon_reference, light, hue, headroom, step)) = palette {
                    if f[i] == 3 {
                        let gain = material_gain(
                            canyon_target(photo, canyon_reference, light),
                            canyon_reference,
                            1.0,
                            Some(hue),
                        );
                        tint = gain.map(|v| (srgb((v / headroom).clamp(0.0, 1.0)) * 255.0).round_ties_even() as u8);
                    }
                    // Match the rendered cliff albedo before quantizing. Shared
                    // palettes bound private Forest materials across all rocks.
                    tint = std::array::from_fn(|k| {
                        let rendered = srgb(linear(tint[k] as f64 / 255.0) * reference[k]) * 255.0;
                        ((rendered / step).round_ties_even() * step).clamp(0.0, 255.0) as u8
                    });
                }
                tint
            })
            .collect()
    });
    Ok(Array2::from_shape_vec((n, 3), out).unwrap().into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn canyon_discards_photo_shadows_and_vegetation() {
        let reference = [0.18, 0.12, 0.09];
        for photo in [[0.0; 3], [30.0, 120.0, 20.0]] {
            let target = canyon_target(photo, reference, 0.45);
            for k in 0..3 {
                assert!((target[k] - reference[k]).abs() < 1e-14);
            }
        }
        let a = canyon_target([190.0, 170.0, 150.0], reference, 0.45);
        let b = canyon_target([95.0, 85.0, 75.0], reference, 0.45);
        for k in 0..3 {
            assert!((a[k] - b[k]).abs() < 1e-14);
        }
    }

    #[test]
    fn neutral_material_balance_preserves_authored_detail() {
        let reference = [0.18, 0.12, 0.09];
        for hue in [None, Some(0.0), Some(0.85), Some(1.0)] {
            for gain in material_gain(reference, reference, 0.45, hue) {
                assert!((gain - 1.0).abs() < 1e-14);
            }
        }
        for v in [0.0, 0.02, 0.1, 0.5, 1.0, 4.0] {
            assert!((srgb(linear(v)) - v).abs() < 1e-7);
        }
    }

    #[test]
    fn region_priority_fallback_and_invalid_coordinates() {
        let ring = vec![(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)];
        let rules = vec![(0, vec![], vec![]), (2, vec![1], vec![]), (3, vec![], vec![ring])];
        assert_eq!(family(1.0, 1.0, 1, &rules), 3);
        assert_eq!(family(5.0, 5.0, 1, &rules), 2);
        assert_eq!(family(5.0, 5.0, 0, &rules), 0);
        assert_eq!(family(f64::NAN, 1.0, 1, &rules), -1);
    }
    #[test]
    fn vegetation_and_shadows_keep_mineral_fallback() {
        let reference = [0.5, 0.4, 0.3];
        let fallback = rock_tint(1, [0.0; 3], 2, reference);
        assert_eq!(fallback, rock_tint(1, [40.0, 160.0, 30.0], 2, reference));
        assert_eq!(fallback, rock_tint(1, [10.0; 3], 2, reference));
        assert_ne!(fallback, rock_tint(1, [190.0, 170.0, 150.0], 2, reference));
    }
}
