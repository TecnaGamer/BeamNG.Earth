//! The far textures' and photos' DDS payload on the CPU (fartex.rs dds_levels, imagery.encode_dds): the mip levels in
//! integers and BC1 by Intel's ISPC texture compressor.
//!
//! - mip_levels_int: imagery.mip_arrays' chain (linear-light 2 x 2 averages, each level from the previous unsharpened
//!   one, sharpened by a 3 x 3 unsharp mask where both sides are >= 4, back to sRGB) in fixed point: linear light in
//!   LIN_BITS bits from the float LUT, the box sums rounded once, the sharpen in 1/65536ths (in 1/256ths a black
//!   texel beside bright ones came out up to 7 sRGB steps off), the sRGB byte by srgb_thresholds (the float path's
//!   bytes). Within one sRGB step of mip_levels_core (the float path) on every texel (2 in 10,000 differ, Washington
//!   DC's aerial, 2026-11-13); the textures are never shared or hashed, so the integer path's own bits are fine.
//!   RGB or RGBA (650: the terrain photo's BC7 packed copy, bc7.mips, whose float chain was 5x its GPU encode's CPU,
//!   506 ms a 4096² photo); an alpha (its ambient occlusion) averaged as it is, never sharpened.
//! - bc1_blocks: BC1 (4-colour, opaque) blocks of an RGB level by ISPC's CompressBlocksBC1 (the intel_tex_2 crate's
//!   prebuilt kernels: SSE2 to AVX-512 picked at run time; 10x our bc1_block at the same PSNR, measured 2026-11-13);
//!   bc1_encode_core where the kernels are not linked (any target but x86_64 Linux / Windows).
//!
//! Both split large images over par::threads() (every output from its own inputs: the same bytes on any number).

use numpy::ndarray::{Array3, ArrayView1, ArrayView3};

use crate::par;

/// Linear light in fixed point: 1.0 = 1 << LIN_BITS (the darkest sRGB step, (1/255)^2.2, is ~5 units: kept apart from
/// black; the 3 x 3 sharpen's sums stay in i64)
const LIN_BITS: u32 = 20;
const LIN_ONE: i64 = 1 << LIN_BITS;
/// The sRGB lookup's coarse table: one entry per 2^SHIFT linear units
const SHIFT: u32 = 4;
/// The sharpen amount in fixed point (9 x LIN_ONE x 2 x SHARPEN_ONE well inside i64)
const SHARPEN_ONE: i64 = 1 << 16;

/// The linear values of the 256 sRGB bytes (the float LUT, rounded to fixed point).
fn lin_lut(lut: &ArrayView1<f32>) -> [u32; 256] {
    let mut t = [0u32; 256];
    for (k, v) in t.iter_mut().enumerate() {
        let f = (lut[k] as f64).clamp(0.0, 1.0);
        *v = (f * LIN_ONE as f64).round() as u32;
    }
    t
}

/// The sRGB byte of a fixed-point linear value (srgb_thresholds: the byte the float path gives for the value
/// l / LIN_ONE, by a coarse table and at most a few steps).
struct ToSrgb {
    thi: [u32; 255],
    coarse: Vec<u8>,
}

impl ToSrgb {
    fn get() -> &'static ToSrgb {
        static T: std::sync::OnceLock<ToSrgb> = std::sync::OnceLock::new();
        T.get_or_init(|| {
            let th = crate::srgb_thresholds();
            let mut thi = [0u32; 255];
            for k in 0..255 {
                // byte >= k + 1 where l / LIN_ONE >= th[k]: l >= ceil(th[k] * LIN_ONE) (exact in f64)
                thi[k] = ((th[k] as f64) * LIN_ONE as f64).ceil() as u32;
            }
            let n = ((LIN_ONE as usize) >> SHIFT) + 1;
            let mut coarse = vec![0u8; n];
            let mut b = 0usize;
            for (j, c) in coarse.iter_mut().enumerate() {
                let l = (j as u32) << SHIFT;
                while b < 255 && thi[b] <= l {
                    b += 1;
                }
                *c = b as u8;
            }
            ToSrgb { thi, coarse }
        })
    }

    #[inline(always)]
    fn byte(&self, l: i64) -> u8 {
        let l = l.clamp(0, LIN_ONE) as u32;
        let mut b = self.coarse[(l >> SHIFT) as usize] as usize;
        while b < 255 && self.thi[b] <= l {
            b += 1;
        }
        b as u8
    }
}

/// Values per thread at least (a 256-512 px far texture's levels on the calling thread).
const MIP_VALUES_PER_THREAD: usize = 1 << 18;

/// An alpha byte in fixed point: a << ALPHA_SHIFT (alpha is linear: averaged as it is, never sharpened; the byte back
/// rounded, as mip_levels_core's (c * 255 + 0.5))
const ALPHA_SHIFT: u32 = 12;

/// mip_levels_core's levels 1.. of an RGB image (h, w, 3), or an RGBA one (h, w, 4: the terrain photo's packed copy,
/// its alpha the ambient occlusion), in fixed point (see the module doc). The integer maths alone: the same bytes on
/// every platform and number of threads.
pub fn mip_levels_int(img: ArrayView3<u8>, lut: ArrayView1<f32>, sharpen: f32) -> Vec<Array3<u8>> {
    match img.shape()[2] {
        3 => mip_levels_nc::<3>(img, lut, sharpen),
        4 => mip_levels_nc::<4>(img, lut, sharpen),
        n => panic!("mip_levels_int: RGB or RGBA, not {n} channels"),
    }
}

/// The fixed-point value of byte v in channel c (colour: linear light by the LUT; alpha: as it is)
#[inline(always)]
fn to_fixed(ll: &[u32; 256], c: usize, v: u8) -> u32 {
    if c < 3 { ll[v as usize] } else { (v as u32) << ALPHA_SHIFT }
}

fn mip_levels_nc<const NC: usize>(img: ArrayView3<u8>, lut: ArrayView1<f32>, sharpen: f32) -> Vec<Array3<u8>> {
    let (mut h, mut w) = (img.shape()[0], img.shape()[1]);
    let owned;
    let src: &[u8] = match img.as_slice() {
        Some(sl) => sl,
        None => {
            owned = img.iter().copied().collect::<Vec<u8>>();
            &owned
        }
    };
    let ll = lin_lut(&lut);
    let sq = (sharpen as f64 * SHARPEN_ONE as f64).round() as i64;
    let mut out = Vec::new();
    // level 1 from the bytes at once
    let mut lin: Vec<u32>;
    if h > 1 && w > 1 {
        let (nh, nw) = (h / 2, w / 2);
        let row0 = w * NC;
        lin = vec![0u32; nh * nw * NC];
        par::rows(&mut lin, nw * NC, par::threads_for(nh * nw * NC, MIP_VALUES_PER_THREAD), |y, r| {
            let (ra, rb) = (&src[(2 * y) * row0..(2 * y + 1) * row0], &src[(2 * y + 1) * row0..(2 * y + 2) * row0]);
            for x in 0..nw {
                for c in 0..NC {
                    let a = 2 * x * NC + c;
                    let s = to_fixed(&ll, c, ra[a]) + to_fixed(&ll, c, ra[a + NC]) + to_fixed(&ll, c, rb[a])
                        + to_fixed(&ll, c, rb[a + NC]);
                    r[x * NC + c] = (s + 2) >> 2;
                }
            }
        });
        h = nh;
        w = nw;
    } else {
        lin = src.iter().enumerate().map(|(i, &v)| to_fixed(&ll, i % NC, v)).collect();
        if h.max(w) <= 1 {
            return out;
        }
        // (a 1 x n or n x 1 image: halved along its long side below)
        let (nh, nw) = (if h > 1 { h / 2 } else { 1 }, if w > 1 { w / 2 } else { 1 });
        lin = halve::<NC>(&lin, h, w, nh, nw);
        h = nh;
        w = nw;
    }
    out.push(finish::<NC>(&lin, h, w, sq));
    while h.max(w) > 1 {
        let (nh, nw) = (if h > 1 { h / 2 } else { 1 }, if w > 1 { w / 2 } else { 1 });
        lin = halve::<NC>(&lin, h, w, nh, nw);
        h = nh;
        w = nw;
        out.push(finish::<NC>(&lin, h, w, sq));
    }
    out
}

/// The next level's fixed-point values (nh x nw of h x w, each side halved where it is over 1): the mean of its 2 x 2
/// (or 2 x 1) texels, rounded once.
fn halve<const NC: usize>(lin: &[u32], h: usize, w: usize, nh: usize, nw: usize) -> Vec<u32> {
    let mut nl = vec![0u32; nh * nw * NC];
    let row = w * NC;
    par::rows(&mut nl, nw * NC, par::threads_for(nh * nw * NC, MIP_VALUES_PER_THREAD), |y, r| {
        if nh < h && nw < w {
            let (ra, rb) = (&lin[(2 * y) * row..(2 * y + 1) * row], &lin[(2 * y + 1) * row..(2 * y + 2) * row]);
            for x in 0..nw {
                for c in 0..NC {
                    let a = 2 * x * NC + c;
                    r[x * NC + c] = (ra[a] + ra[a + NC] + rb[a] + rb[a + NC] + 2) >> 2;
                }
            }
        } else if nh < h {
            let (ra, rb) = (&lin[(2 * y) * row..(2 * y + 1) * row], &lin[(2 * y + 1) * row..(2 * y + 2) * row]);
            for (k, o) in r.iter_mut().enumerate() {
                *o = (ra[k] + rb[k] + 1) >> 1;
            }
        } else {
            let ra = &lin[y * row..(y + 1) * row];
            for x in 0..nw {
                for c in 0..NC {
                    let a = 2 * x * NC + c;
                    r[x * NC + c] = (ra[a] + ra[a + NC] + 1) >> 1;
                }
            }
        }
    });
    nl
}

#[inline(always)]
fn alpha_byte(l: u32) -> u8 {
    ((l + (1 << (ALPHA_SHIFT - 1))) >> ALPHA_SHIFT).min(255) as u8
}

/// A level's bytes from its (unsharpened) fixed-point values: the colour sharpened by sq / SHARPEN_ONE (3 x 3 box,
/// edge-padded) where both sides are >= 4, back to sRGB; the alpha as averaged.
fn finish<const NC: usize>(lin: &[u32], h: usize, w: usize, sq: i64) -> Array3<u8> {
    let to = ToSrgb::get();
    let mut bytes = vec![0u8; h * w * NC];
    let sharp = h.min(w) >= 4 && sq > 0;
    let row = w * NC;
    par::rows(&mut bytes, row, par::threads_for(h * row, MIP_VALUES_PER_THREAD), |y, r| {
        if !sharp {
            for (k, b) in r.iter_mut().enumerate() {
                let l = lin[y * row + k];
                *b = if k % NC < 3 { to.byte(l as i64) } else { alpha_byte(l) };
            }
            return;
        }
        let (ya, yb) = (y.saturating_sub(1), (y + 1).min(h - 1));
        let (ra, rm, rb) = (&lin[ya * row..(ya + 1) * row], &lin[y * row..(y + 1) * row], &lin[yb * row..(yb + 1) * row]);
        for x in 0..w {
            let (xa, xb) = (x.saturating_sub(1), (x + 1).min(w - 1));
            for c in 0..NC.min(3) {
                let (i0, i1, i2) = (xa * NC + c, x * NC + c, xb * NC + c);
                let sum = ra[i0] as i64 + ra[i1] as i64 + ra[i2] as i64 + rm[i0] as i64 + rm[i1] as i64
                    + rm[i2] as i64 + rb[i0] as i64 + rb[i1] as i64 + rb[i2] as i64;
                let v = rm[i1] as i64;
                // v + sharpen (v - sum / 9), rounded to the nearest unit
                let num = sq * (9 * v - sum);
                let l = v + (num + 9 * SHARPEN_ONE / 2).div_euclid(9 * SHARPEN_ONE);
                r[i1] = to.byte(l);
            }
            if NC == 4 {
                r[x * NC + 3] = alpha_byte(rm[x * NC + 3]);
            }
        }
    });
    Array3::from_shape_vec((h, w, NC), bytes).expect("sized")
}

/// The MSVC runtime's "floating point used" marker, which MSVC-built objects reference (the ISPC kernels' Windows
/// libraries, linked into a windows-gnu build by tools/build_native.py --windows); MinGW's runtime has none. Its value
/// is never read.
#[cfg(all(windows, target_env = "gnu"))]
#[no_mangle]
#[used]
pub static _fltused: i32 = 0;

/// Blocks per thread at least (a 512 px texture's 16 k blocks on the calling thread).
const BC1_BLOCKS_PER_THREAD: usize = 32768;

/// BC1 blocks of an RGB level (h, w, 3), row-major, its edges padded by repetition (bc1_encode_core's layout).
pub fn bc1_blocks(a: ArrayView3<u8>) -> Vec<u8> {
    #[cfg(all(target_arch = "x86_64", any(target_os = "linux", windows)))]
    {
        if std::env::var_os("BNE_BC1_REF").is_none() {
            return bc1_ispc(a);
        }
    }
    crate::bc1_encode_core(a)
}

/// Whether bc1_blocks uses ISPC's kernels in this build.
pub fn ispc_linked() -> bool {
    cfg!(all(target_arch = "x86_64", any(target_os = "linux", windows)))
}

#[cfg(all(target_arch = "x86_64", any(target_os = "linux", windows)))]
fn bc1_ispc(a: ArrayView3<u8>) -> Vec<u8> {
    let (h, w, nc) = (a.shape()[0], a.shape()[1], a.shape()[2]);
    let (nby, nbx) = (h.div_ceil(4), w.div_ceil(4));
    let (ph, pw) = (nby * 4, nbx * 4);
    // RGBA (the kernels' input), its sides padded to whole blocks by repeating the last row / column
    let own = a.as_standard_layout();
    let src = own.as_slice().expect("standard layout");
    let stride = pw * 4;
    let mut rgba = vec![255u8; ph * stride];
    for y in 0..ph {
        let sy = y.min(h - 1);
        let (srow, drow) = (&src[sy * w * nc..(sy + 1) * w * nc], &mut rgba[y * stride..(y + 1) * stride]);
        for x in 0..pw {
            let sx = x.min(w - 1);
            drow[x * 4..x * 4 + 3].copy_from_slice(&srow[sx * nc..sx * nc + 3]);
        }
    }
    let mut out = vec![0u8; nby * nbx * 8];
    let row = nbx * 8;
    // a block row a call (each row's blocks from its own texels), the rows in bands over the threads
    par::rows(&mut out, row, par::threads_for(nby * nbx, BC1_BLOCKS_PER_THREAD), |by, blocks| {
        let surf = intel_tex_2::RgbaSurface {
            data: &rgba[by * 4 * stride..(by + 1) * 4 * stride],
            width: pw as u32,
            height: 4,
            stride: stride as u32,
        };
        intel_tex_2::bc1::compress_blocks_into(&surf, blocks);
    });
    out
}

/// The DDS payload's levels: level 0 and mip_levels_int's, each as BC1 blocks.
pub fn dds_levels(a: ArrayView3<u8>, lut: ArrayView1<f32>, sharpen: f32) -> Vec<Vec<u8>> {
    let mut blobs = vec![bc1_blocks(a)];
    for l in mip_levels_int(a, lut, sharpen) {
        blobs.push(bc1_blocks(l.view()));
    }
    blobs
}

/// bc1_blocks of an (h, w, 3) uint8 image (tests and benchmarks: the far textures' encoder against bc1_encode's).
#[pyo3::pyfunction]
pub fn bc1_encode_fast<'py>(py: pyo3::Python<'py>, img: numpy::PyReadonlyArray3<'py, u8>)
                            -> pyo3::PyResult<pyo3::Bound<'py, pyo3::types::PyBytes>> {
    let a = img.as_array();
    if a.shape()[2] != 3 || a.shape()[0] == 0 || a.shape()[1] == 0 {
        return Err(pyo3::exceptions::PyValueError::new_err("bc1_encode_fast: rgb (h, w, 3)"));
    }
    let out = py.detach(|| bc1_blocks(a));
    Ok(pyo3::types::PyBytes::new(py, &out))
}

/// mip_levels_int's levels 1.. of an (h, w, 3) or (h, w, 4) uint8 image: the far textures' chain, and since 650 the
/// terrain photo's packed copy's (bc7.mips; RGBA from 650).
#[pyo3::pyfunction]
pub fn mip_levels_fast<'py>(py: pyo3::Python<'py>, rgb: numpy::PyReadonlyArray3<'py, u8>,
                            lut: numpy::PyReadonlyArray1<'py, f32>, sharpen: f32)
                            -> pyo3::PyResult<Vec<pyo3::Bound<'py, numpy::PyArray3<u8>>>> {
    use numpy::IntoPyArray;
    let (img, lut) = (rgb.as_array(), lut.as_array());
    if !matches!(img.shape()[2], 3 | 4) || lut.len() != 256 || img.shape()[0] == 0 || img.shape()[1] == 0 {
        return Err(pyo3::exceptions::PyValueError::new_err("mip_levels_fast: (h, w, 3 | 4) uint8, lut of 256"));
    }
    let levels = py.detach(|| mip_levels_int(img, lut, sharpen));
    Ok(levels.into_iter().map(|a| a.into_pyarray(py)).collect())
}

/// Whether the far textures' BC1 runs on ISPC's kernels in this build.
#[pyo3::pyfunction]
pub fn bc1_ispc_linked() -> bool {
    ispc_linked()
}

use numpy::{IntoPyArray, PyArray3, PyReadonlyArray1, PyReadonlyArray3};
use pyo3::{exceptions::PyValueError, prelude::*, types::PyBytes};

/// Weight of the forest floor at each mip, in log2 metres (shared by the BC1 and BC7 photo encoders).
#[pyfunction]
fn fade_weights(mut texel: f64, levels: usize, near: f64, far: f64) -> Vec<f64> {
    let span = libm::log2(far / near);
    (0..levels).map(|_| {
        let weight = if texel > 0.0 { (libm::log2(far / texel) / span).clamp(0.0, 1.0) } else { 1.0 };
        texel *= 2.0;
        weight
    }).collect()
}

fn mix(a: ArrayView3<u8>, c: ArrayView3<u8>, q: u32) -> Array3<u8> {
    numpy::ndarray::Zip::from(a).and(c).map_collect(|&x, &y| {
        ((y as u32 * (256 - q) + x as u32 * q + 128) >> 8) as u8
    })
}

#[pyfunction]
fn photo_mix<'py>(py: Python<'py>, floor: PyReadonlyArray3<'py, u8>, canopy: PyReadonlyArray3<'py, u8>, q: u32)
                  -> PyResult<Bound<'py, PyArray3<u8>>> {
    let (a, c) = (floor.as_array(), canopy.as_array());
    if a.shape() != c.shape() || q > 256 {
        return Err(PyValueError::new_err("photo_mix: different image shapes or weight outside 0..256"));
    }
    Ok(py.detach(|| mix(a, c, q)).into_pyarray(py))
}

/// Apply all changed RGB texels to the canopy copy (roads, roofs and cliff paint survive the mip fade).
#[pyfunction]
fn photo_keep_painted<'py>(py: Python<'py>, before: PyReadonlyArray3<'py, u8>, after: PyReadonlyArray3<'py, u8>,
                           canopy: PyReadonlyArray3<'py, u8>) -> PyResult<Bound<'py, PyArray3<u8>>> {
    let (a, b, c) = (before.as_array(), after.as_array(), canopy.as_array());
    if a.shape() != b.shape() || a.shape() != c.shape() || a.shape()[2] != 3 {
        return Err(PyValueError::new_err("photo_keep_painted: expected three RGB images of the same shape"));
    }
    let out = py.detach(|| {
        let mut out = c.as_standard_layout().into_owned();
        let (a, b) = (a.as_standard_layout(), b.as_standard_layout());
        for ((old, new), dst) in a.as_slice().unwrap().chunks_exact(3).zip(b.as_slice().unwrap().chunks_exact(3))
            .zip(out.as_slice_mut().unwrap().chunks_exact_mut(3)) {
            if old != new {
                dst.copy_from_slice(new);
            }
        }
        out
    });
    Ok(out.into_pyarray(py))
}

/// imagery.encode_dds's legacy DXT1 and sRGB DX10 headers both use depth 0 and mip caps, including at 1x1.
fn far_dds_header(w: usize, h: usize, n: usize, srgb: bool) -> Vec<u8> {
    let mut header = vec![0; if srgb { 148 } else { 128 }];
    header[..4].copy_from_slice(b"DDS ");
    for (offset, value) in [(4, 124), (8, 0xa1007), (12, h as u32), (16, w as u32),
                            (20, (w.div_ceil(4) * h.div_ceil(4) * 8) as u32), (28, n as u32),
                            (76, 32), (80, 4), (108, 0x401008)] {
        header[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
    }
    header[84..88].copy_from_slice(if srgb { b"DX10" } else { b"DXT1" });
    if srgb {
        header[128] = 72; // DXGI_FORMAT_BC1_UNORM_SRGB
        header[132] = 3;  // D3D10_RESOURCE_DIMENSION_TEXTURE2D
        header[140] = 1;  // one array slice
    }
    header
}

type Fade<'a> = (ArrayView3<'a, u8>, f64, f64, f64);

pub(crate) fn photo_dds(a: ArrayView3<u8>, lut: ArrayView1<f32>, sharpen: f32, srgb: bool, fade: Option<Fade>) -> Vec<u8> {
    let levels = mip_levels_int(a, lut, sharpen);
    let n = levels.len() + 1;
    let mut out = far_dds_header(a.shape()[1], a.shape()[0], n, srgb);
    let floor = std::iter::once(a.view()).chain(levels.iter().map(|l| l.view()));
    if let Some((canopy, texel, near, far)) = fade {
        let canopy_levels = mip_levels_int(canopy, lut, sharpen);
        let crowns = std::iter::once(canopy.view()).chain(canopy_levels.iter().map(|l| l.view()));
        for ((a, c), weight) in floor.zip(crowns).zip(fade_weights(texel, n, near, far)) {
            let q = (weight * 256.0).round_ties_even() as u32;
            out.extend(match q {
                0 => bc1_blocks(c),
                256 => bc1_blocks(a),
                _ => bc1_blocks(mix(a, c, q).view()),
            });
        }
    } else {
        for image in floor {
            out.extend(bc1_blocks(image));
        }
    }
    out
}

#[pyfunction]
fn encode_photo_dds<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, lut: PyReadonlyArray1<'py, f32>,
                         sharpen: f32, srgb: bool, fade: Option<(PyReadonlyArray3<'py, u8>, f64, f64, f64)>)
                         -> PyResult<Bound<'py, PyBytes>> {
    let (a, lut) = (rgb.as_array(), lut.as_array());
    let fade = fade.as_ref().map(|(c, t, near, far)| (c.as_array(), *t, *near, *far));
    if a.is_empty() || a.shape()[2] != 3 || lut.len() != 256 || fade.as_ref().is_some_and(|f| f.0.shape() != a.shape()) {
        return Err(PyValueError::new_err("encode_photo_dds: expected nonempty matching RGB images and a 256-entry LUT"));
    }
    let out = py.detach(|| photo_dds(a, lut, sharpen, srgb, fade));
    Ok(PyBytes::new(py, &out))
}

pub fn register_photo(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(fade_weights, m)?)?;
    m.add_function(wrap_pyfunction!(photo_mix, m)?)?;
    m.add_function(wrap_pyfunction!(photo_keep_painted, m)?)?;
    m.add_function(wrap_pyfunction!(encode_photo_dds, m)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dm::Dm;
    use numpy::ndarray::Array1;

    fn lut() -> Array1<f32> {
        Array1::from_iter((0..256).map(|k| (k as f64 / 255.0).dpowf(2.2) as f32))
    }

    fn noise(h: usize, w: usize, nc: usize, seed: u64) -> Array3<u8> {
        let mut s = seed;
        Array3::from_shape_fn((h, w, nc), |_| {
            s = s.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            (s >> 56) as u8
        })
    }

    /// The chain written out plainly (one thread, no tables but the sRGB thresholds): what mip_levels_int must give,
    /// bit for bit (650's RGBA chain, bc7.mips).
    fn reference(img: &Array3<u8>, lut: &Array1<f32>, sharpen: f32) -> Vec<Array3<u8>> {
        let (mut h, mut w, nc) = (img.shape()[0], img.shape()[1], img.shape()[2]);
        let ll = lin_lut(&lut.view());
        let th = crate::srgb_thresholds();
        let to_srgb = |l: i64| -> u8 {
            let l = l.clamp(0, LIN_ONE);
            th.iter().filter(|&&t| ((t as f64) * LIN_ONE as f64).ceil() as i64 <= l).count() as u8
        };
        let sq = (sharpen as f64 * SHARPEN_ONE as f64).round() as i64;
        let mut lin: Vec<i64> = img.iter().enumerate()
            .map(|(i, &v)| if i % nc < 3 { ll[v as usize] as i64 } else { (v as i64) << ALPHA_SHIFT }).collect();
        let mut out = Vec::new();
        while h.max(w) > 1 {
            let (nh, nw) = ((h / 2).max(1), (w / 2).max(1));
            let mut nl = vec![0i64; nh * nw * nc];
            for y in 0..nh {
                for x in 0..nw {
                    for c in 0..nc {
                        let ys: Vec<usize> = if nh < h { vec![2 * y, 2 * y + 1] } else { vec![y] };
                        let xs: Vec<usize> = if nw < w { vec![2 * x, 2 * x + 1] } else { vec![x] };
                        let s: i64 = ys.iter().flat_map(|&yy| xs.iter().map(move |&xx| (yy, xx)))
                            .map(|(yy, xx)| lin[(yy * w + xx) * nc + c]).sum();
                        let n = (ys.len() * xs.len()) as i64;
                        nl[(y * nw + x) * nc + c] = (s + n / 2) / n;
                    }
                }
            }
            lin = nl;
            h = nh;
            w = nw;
            let mut b = Array3::<u8>::zeros((h, w, nc));
            for y in 0..h {
                for x in 0..w {
                    for c in 0..nc {
                        let v = lin[(y * w + x) * nc + c];
                        b[[y, x, c]] = if c == 3 {
                            ((v + (1 << (ALPHA_SHIFT - 1))) >> ALPHA_SHIFT).min(255) as u8
                        } else if h.min(w) >= 4 && sq > 0 {
                            let mut sum = 0i64;
                            for dy in [-1i64, 0, 1] {
                                for dx in [-1i64, 0, 1] {
                                    let yy = (y as i64 + dy).clamp(0, h as i64 - 1) as usize;
                                    let xx = (x as i64 + dx).clamp(0, w as i64 - 1) as usize;
                                    sum += lin[(yy * w + xx) * nc + c];
                                }
                            }
                            to_srgb(v + (sq * (9 * v - sum) + 9 * SHARPEN_ONE / 2).div_euclid(9 * SHARPEN_ONE))
                        } else {
                            to_srgb(v)
                        };
                    }
                }
            }
            out.push(b);
        }
        out
    }

    #[test]
    fn rgba_chain_matches_the_reference() {
        let lut = lut();
        for (h, w, seed) in [(64usize, 64usize, 1u64), (32, 8, 2), (16, 1, 3), (1, 4, 4)] {
            for nc in [3usize, 4] {
                let img = noise(h, w, nc, seed);
                let got = mip_levels_int(img.view(), lut.view(), 0.35);
                let want = reference(&img, &lut, 0.35);
                assert_eq!(got.len(), want.len(), "{h}x{w}x{nc}");
                for (k, (a, b)) in got.iter().zip(&want).enumerate() {
                    assert_eq!(a, b, "{h}x{w}x{nc} level {}", k + 1);
                }
            }
        }
    }

    #[test]
    fn rgba_colour_is_the_rgb_chain_and_alpha_is_averaged() {
        let lut = lut();
        let rgba = noise(128, 128, 4, 9);
        let rgb = rgba.slice(numpy::ndarray::s![.., .., 0..3]).to_owned();
        let a = mip_levels_int(rgba.view(), lut.view(), 0.35);
        let b = mip_levels_int(rgb.view(), lut.view(), 0.35);
        for (x, y) in a.iter().zip(&b) {
            assert_eq!(x.slice(numpy::ndarray::s![.., .., 0..3]), y.view(), "the colour as the RGB chain's");
        }
        let mut flat = rgba.clone();
        flat.slice_mut(numpy::ndarray::s![.., .., 3]).fill(251);
        for l in mip_levels_int(flat.view(), lut.view(), 0.35) {
            assert!(l.slice(numpy::ndarray::s![.., .., 3]).iter().all(|&v| v == 251), "a flat AO stays flat");
        }
    }
}
