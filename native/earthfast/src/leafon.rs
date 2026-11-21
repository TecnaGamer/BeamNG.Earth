//! Leaf-off imagery takes its coarse chroma and part of its brightness from a leafy reference.
use numpy::ndarray::{Array2, Array3, ArrayView2, ArrayView3};
use numpy::{IntoPyArray, PyArray2, PyArray3, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::{exceptions::PyValueError, prelude::*};

use crate::fartex::{resize_bilinear_rgb, resize_box_rgb, resize_f32};
use crate::texdds::pil_gaussian_blur;

// Pillow RGB/YCbCr tables round each channel contribution separately, with six fractional bits.
// The addition of 0.5 followed by truncation applies to negative terms too.
const fn table(coefficient: f64, origin: i32) -> [i32; 256] {
    let mut values = [0; 256];
    let mut i = 0;
    while i < 256 {
        values[i] = ((i as i32 - origin) as f64 * coefficient * 64.0 + 0.5) as i32;
        i += 1;
    }
    values
}

fn ycbcr(p: &[u8]) -> [f32; 3] {
    const YR: [i32; 256] = table(0.299, 0);
    const YG: [i32; 256] = table(0.587, 0);
    const YB: [i32; 256] = table(0.114, 0);
    const CBR: [i32; 256] = table(-0.16874, 0);
    const CBG: [i32; 256] = table(-0.33126, 0);
    const CRG: [i32; 256] = table(-0.41869, 0);
    const CRB: [i32; 256] = table(-0.08131, 0);
    let (r, g, b) = (p[0] as usize, p[1] as usize, p[2] as usize);
    [
        ((YR[r] + YG[g] + YB[b]) >> 6) as f32,
        (128 + ((CBR[r] + CBG[g] + 32 * b as i32) >> 6)) as f32,
        (128 + ((32 * r as i32 + CRG[g] + CRB[b]) >> 6)) as f32,
    ]
}

fn rgb(ycc: [f32; 3]) -> [u8; 3] {
    const RCR: [i32; 256] = table(1.402, 128);
    const GCB: [i32; 256] = table(-0.34414, 128);
    const GCR: [i32; 256] = table(-0.71414, 128);
    const BCB: [i32; 256] = table(1.772, 128);
    let [y, cb, cr] = ycc.map(|v| v.round_ties_even() as u8);
    let (cb, cr) = (cb as usize, cr as usize);
    [RCR[cr], GCB[cb] + GCR[cr], BCB[cb]].map(|v| (y as i32 + (v >> 6)).clamp(0, 255) as u8)
}

fn luma(p: &[u8]) -> u8 {
    ((19595 * p[0] as u32 + 38470 * p[1] as u32 + 7471 * p[2] as u32 + 32768) >> 16) as u8
}

type Fields = (Array3<u8>, Array2<f32>, Array2<f32>);

fn fields(small: ArrayView3<u8>, reference: ArrayView3<u8>) -> Option<Fields> {
    let (h, w, _) = reference.dim();
    let mut rim = Array3::zeros(reference.dim());
    for ch in 0..3 {
        let mut plane: Vec<u8> = reference
            .index_axis(numpy::ndarray::Axis(2), ch)
            .iter()
            .copied()
            .collect();
        pil_gaussian_blur(&mut plane, w, h, 1.0);
        for (pixel, value) in rim
            .index_axis_mut(numpy::ndarray::Axis(2), ch)
            .iter_mut()
            .zip(plane)
        {
            *pixel = value;
        }
    }
    let pixels = rim.as_slice().unwrap();
    let veg = Array2::from_shape_fn((h, w), |(y, x)| {
        let i = 3 * (y * w + x);
        let sum = pixels[i] as f32 + pixels[i + 1] as f32 + pixels[i + 2] as f32;
        let t = ((pixels[i + 1] as f32 / (sum + 1.0) - 0.345) / 0.025).clamp(0.0, 1.0);
        t * t * (3.0 - 2.0 * t)
    });
    if !veg.iter().any(|&v| v > 0.01) {
        return None;
    }
    let small = small.as_standard_layout();
    let mut ys: Vec<_> = small
        .as_slice()
        .unwrap()
        .chunks_exact(3)
        .map(luma)
        .collect();
    pil_gaussian_blur(&mut ys, w, h, 1.0);
    let gain = Array2::from_shape_fn((h, w), |(y, x)| {
        let i = y * w + x;
        ((luma(&pixels[3 * i..]) as f32 + 4.0) / (ys[i] as f32 + 4.0)).clamp(0.5, 1.5)
    });
    Some((rim, veg, gain))
}

fn blend(
    src: ArrayView3<u8>,
    rim: ArrayView3<u8>,
    veg: ArrayView2<f32>,
    gain: ArrayView2<f32>,
) -> Array3<u8> {
    let (src, rim) = (src.as_standard_layout(), rim.as_standard_layout());
    let mut out = Array3::zeros(src.dim());
    for ((((pixel, p), reference), &v), &g) in out
        .as_slice_mut()
        .unwrap()
        .chunks_exact_mut(3)
        .zip(src.as_slice().unwrap().chunks_exact(3))
        .zip(rim.as_slice().unwrap().chunks_exact(3))
        .zip(veg)
        .zip(gain)
    {
        let mut ycc = ycbcr(p);
        let rc = ycbcr(reference);
        ycc[0] *= 1.0 + v * 0.8 * (g - 1.0);
        for ch in 1..3 {
            ycc[ch] += v * (rc[ch] - ycc[ch]);
        }
        pixel.copy_from_slice(&rgb(ycc));
    }
    out
}

pub(crate) fn correct(src: ArrayView3<u8>, reference: ArrayView3<u8>) -> Option<Array3<u8>> {
    let (rh, rw, _) = reference.dim();
    let (h, w, _) = src.dim();
    let small = resize_box_rgb(src, rw, rh);
    let (rim, veg, gain) = fields(small.view(), reference)?;
    let rim = resize_bilinear_rgb(rim.view(), w, h);
    let veg = resize_f32(veg.view(), w, h);
    let gain = resize_f32(gain.view(), w, h);
    Some(blend(src, rim.view(), veg.view(), gain.view()))
}

fn check_rgb(a: ArrayView3<u8>) -> PyResult<()> {
    if a.is_empty() || a.dim().2 != 3 {
        return Err(PyValueError::new_err("leaf_on: expected RGB"));
    }
    Ok(())
}

#[pyfunction]
pub fn leaf_on<'py>(
    py: Python<'py>,
    src: PyReadonlyArray3<'py, u8>,
    reference: PyReadonlyArray3<'py, u8>,
) -> PyResult<Option<Bound<'py, PyArray3<u8>>>> {
    let (a, r) = (src.as_array(), reference.as_array());
    check_rgb(a)?;
    check_rgb(r)?;
    Ok(py.detach(|| correct(a, r)).map(|out| out.into_pyarray(py)))
}

type PyFields<'py> = (
    Bound<'py, PyArray3<u8>>,
    Bound<'py, PyArray2<f32>>,
    Bound<'py, PyArray2<f32>>,
);

// Rectangular resizing depends on the installed Pillow. The filters on either side are shared.
#[pyfunction]
pub fn leaf_fields<'py>(
    py: Python<'py>,
    small: PyReadonlyArray3<'py, u8>,
    reference: PyReadonlyArray3<'py, u8>,
) -> PyResult<Option<PyFields<'py>>> {
    let (a, r) = (small.as_array(), reference.as_array());
    check_rgb(a)?;
    if a.dim() != r.dim() {
        return Err(PyValueError::new_err("leaf_fields: shape mismatch"));
    }
    Ok(py.detach(|| fields(a, r)).map(|(rim, veg, gain)| {
        (
            rim.into_pyarray(py),
            veg.into_pyarray(py),
            gain.into_pyarray(py),
        )
    }))
}

#[pyfunction]
pub fn leaf_blend<'py>(
    py: Python<'py>,
    src: PyReadonlyArray3<'py, u8>,
    rim: PyReadonlyArray3<'py, u8>,
    veg: PyReadonlyArray2<'py, f32>,
    gain: PyReadonlyArray2<'py, f32>,
) -> PyResult<Bound<'py, PyArray3<u8>>> {
    let (a, b, v, g) = (
        src.as_array(),
        rim.as_array(),
        veg.as_array(),
        gain.as_array(),
    );
    check_rgb(a)?;
    if a.dim() != b.dim() || a.shape()[..2] != *v.shape() || v.dim() != g.dim() {
        return Err(PyValueError::new_err("leaf_blend: shape mismatch"));
    }
    Ok(py.detach(|| blend(a, b, v, g)).into_pyarray(py))
}

/// Fall peak, winter bare interval and spring flush by local day and latitude.
#[pyfunction]
pub fn season_amounts(mut day: i32, lat: f64) -> (f64, f64, f64) {
    if lat.abs() < 23.0 {
        return (0., 0., 0.);
    }
    if lat < 0.0 {
        day = (day + 182) % 365;
    }
    let doy = day as f64;
    let shift = (lat.abs() - 40.) * 2.5;
    let fall = (1.0 - (doy - (300. - shift)).abs() / 28.).max(0.);
    let (w0, w1) = (330. - shift, 425. + shift);
    let d = if doy >= w0 - 20. { doy } else { doy + 365. };
    let winter = ((d - (w0 - 20.)) / 20.)
        .max(0.)
        .min(((w1 + 20. - d) / 20.).max(0.))
        .min(1.);
    let spring = (1.0 - (doy - (112. + shift)).abs() / 22.).max(0.);
    (fall, winter, spring)
}
