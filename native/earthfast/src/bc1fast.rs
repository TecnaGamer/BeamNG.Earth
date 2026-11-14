//! The far textures' and photos' DDS payload on the CPU (fartex.rs dds_levels, imagery.encode_dds): the mip levels in
//! integers and BC1 by Intel's ISPC texture compressor.
//!
//! - mip_levels_int: imagery.mip_arrays' chain (linear-light 2 x 2 averages, each level from the previous unsharpened
//!   one, sharpened by a 3 x 3 unsharp mask where both sides are >= 4, back to sRGB) in fixed point: linear light in
//!   LIN_BITS bits from the float LUT, the box sums rounded once, the sharpen in 1/65536ths (in 1/256ths a black
//!   texel beside bright ones came out up to 7 sRGB steps off), the sRGB byte by srgb_thresholds (the float path's
//!   bytes). Within one sRGB step of mip_levels_core (the float path) on every texel (2 in 10,000 differ, Washington
//!   DC's aerial, 2026-11-13); the textures are never shared or hashed, so the integer path's own bits are fine.
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

/// mip_levels_core's levels 1.. of an RGB image (h, w, 3) in fixed point (see the module doc).
pub fn mip_levels_int(img: ArrayView3<u8>, lut: ArrayView1<f32>, sharpen: f32) -> Vec<Array3<u8>> {
    const NC: usize = 3;
    assert_eq!(img.shape()[2], NC, "mip_levels_int: RGB");
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
                    let s = ll[ra[a] as usize] + ll[ra[a + NC] as usize] + ll[rb[a] as usize] + ll[rb[a + NC] as usize];
                    r[x * NC + c] = (s + 2) >> 2;
                }
            }
        });
        h = nh;
        w = nw;
    } else {
        lin = src.iter().map(|&v| ll[v as usize]).collect();
        if h.max(w) <= 1 {
            return out;
        }
        // (a 1 x n or n x 1 image: halved along its long side below)
        let (nh, nw) = (if h > 1 { h / 2 } else { 1 }, if w > 1 { w / 2 } else { 1 });
        lin = halve(&lin, h, w, nh, nw);
        h = nh;
        w = nw;
    }
    out.push(finish(&lin, h, w, sq));
    while h.max(w) > 1 {
        let (nh, nw) = (if h > 1 { h / 2 } else { 1 }, if w > 1 { w / 2 } else { 1 });
        lin = halve(&lin, h, w, nh, nw);
        h = nh;
        w = nw;
        out.push(finish(&lin, h, w, sq));
    }
    out
}

/// The next level's linear values (nh x nw of h x w, each side halved where it is over 1): the mean of its 2 x 2 (or
/// 2 x 1) texels, rounded once.
fn halve(lin: &[u32], h: usize, w: usize, nh: usize, nw: usize) -> Vec<u32> {
    const NC: usize = 3;
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

/// A level's sRGB bytes from its (unsharpened) linear values: sharpened by sq / SHARPEN_ONE (3 x 3 box, edge-padded) where
/// both sides are >= 4.
fn finish(lin: &[u32], h: usize, w: usize, sq: i64) -> Array3<u8> {
    const NC: usize = 3;
    let to = ToSrgb::get();
    let mut bytes = vec![0u8; h * w * NC];
    let sharp = h.min(w) >= 4 && sq > 0;
    let row = w * NC;
    par::rows(&mut bytes, row, par::threads_for(h * row, MIP_VALUES_PER_THREAD), |y, r| {
        if !sharp {
            for (k, b) in r.iter_mut().enumerate() {
                *b = to.byte(lin[y * row + k] as i64);
            }
            return;
        }
        let (ya, yb) = (y.saturating_sub(1), (y + 1).min(h - 1));
        let (ra, rm, rb) = (&lin[ya * row..(ya + 1) * row], &lin[y * row..(y + 1) * row], &lin[yb * row..(yb + 1) * row]);
        for x in 0..w {
            let (xa, xb) = (x.saturating_sub(1), (x + 1).min(w - 1));
            for c in 0..NC {
                let (i0, i1, i2) = (xa * NC + c, x * NC + c, xb * NC + c);
                let sum = ra[i0] as i64 + ra[i1] as i64 + ra[i2] as i64 + rm[i0] as i64 + rm[i1] as i64
                    + rm[i2] as i64 + rb[i0] as i64 + rb[i1] as i64 + rb[i2] as i64;
                let v = rm[i1] as i64;
                // v + sharpen (v - sum / 9), rounded to the nearest unit
                let num = sq * (9 * v - sum);
                let l = v + (num + 9 * SHARPEN_ONE / 2).div_euclid(9 * SHARPEN_ONE);
                r[i1] = to.byte(l);
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

/// mip_levels_int's levels 1.. of an (h, w, 3) uint8 image (tests and benchmarks: against mip_levels').
#[pyo3::pyfunction]
pub fn mip_levels_fast<'py>(py: pyo3::Python<'py>, rgb: numpy::PyReadonlyArray3<'py, u8>,
                            lut: numpy::PyReadonlyArray1<'py, f32>, sharpen: f32)
                            -> pyo3::PyResult<Vec<pyo3::Bound<'py, numpy::PyArray3<u8>>>> {
    use numpy::IntoPyArray;
    let (img, lut) = (rgb.as_array(), lut.as_array());
    if img.shape()[2] != 3 || lut.len() != 256 || img.shape()[0] == 0 || img.shape()[1] == 0 {
        return Err(pyo3::exceptions::PyValueError::new_err("mip_levels_fast: rgb (h, w, 3), lut of 256"));
    }
    let levels = py.detach(|| mip_levels_int(img, lut, sharpen));
    Ok(levels.into_iter().map(|a| a.into_pyarray(py)).collect())
}

/// Whether the far textures' BC1 runs on ISPC's kernels in this build.
#[pyo3::pyfunction]
pub fn bc1_ispc_linked() -> bool {
    ispc_linked()
}
