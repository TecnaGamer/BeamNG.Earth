//! buildings.fill_photo in Rust (2026-10-23: 0.30 s of a block's 0.93 s base photo on a Steam Deck, its push-pull
//! pyramid and Pillow's Gaussian blur). Bit for bit what the numpy / Pillow 13 version made: numpy's float32 sums of
//! the 2x2 blocks in its own order, Pillow's float ("F") bilinear resize (double coefficients and accumulation) and
//! its Gaussian blur (three extended box blurs a direction, 8-bit fixed point, ImagingGaussianBlur).

use numpy::{IntoPyArray, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::prelude::*;

/// Pillow's precompute_coeffs for the bilinear filter (support 1), box (0, in): per output index, (xmin, the
/// normalised weights).
fn bilinear_coeffs(in_size: usize, out_size: usize) -> Vec<(usize, Vec<f64>)> {
    let scale = in_size as f32 as f64 / out_size as f64;
    let filterscale = if scale < 1.0 { 1.0 } else { scale };
    let support = 1.0 * filterscale;
    let inv = 1.0 / filterscale;
    (0..out_size).map(|xx| {
        let center = 0.0 + (xx as f64 + 0.5) * scale;
        let xmin = ((center - support + 0.5) as i64).max(0);
        let xmax = ((center + support + 0.5) as i64).min(in_size as i64) - xmin;
        let mut k: Vec<f64> = (0..xmax.max(0)).map(|x| {
            let mut t = ((x + xmin) as f64 - center + 0.5) * inv;
            if t < 0.0 { t = -t; }
            if t < 1.0 { 1.0 - t } else { 0.0 }
        }).collect();
        let ww: f64 = k.iter().fold(0.0, |a, &v| a + v);
        if ww != 0.0 {
            for v in k.iter_mut() { *v /= ww; }
        }
        (xmin as usize, k)
    }).collect()
}

/// Pillow's resize of a float image (h, w) to (oh, ow), BILINEAR (kx, ky: bilinear_coeffs(w, ow), (h, oh)): the
/// horizontal pass, then the vertical.
fn resize_f(src: &[f32], h: usize, w: usize, oh: usize, ow: usize, kx: &[(usize, Vec<f64>)], ky: &[(usize, Vec<f64>)])
            -> Vec<f32> {
    let mut tmp = vec![0f32; h * ow];
    for y in 0..h {
        let line = &src[y * w..(y + 1) * w];
        for (xx, (xmin, k)) in kx.iter().enumerate() {
            let mut ss = 0.0f64;
            for (x, &c) in k.iter().enumerate() {
                ss += line[x + xmin] as f64 * c;
            }
            tmp[y * ow + xx] = ss as f32;
        }
    }
    let mut out = vec![0f32; oh * ow];
    let mut ss = vec![0f64; ow];
    for (yy, (ymin, k)) in ky.iter().enumerate() {
        ss.iter_mut().for_each(|v| *v = 0.0);
        for (y, &c) in k.iter().enumerate() {       // (the same sum per pixel, in y order; rows at a time)
            let line = &tmp[(y + ymin) * ow..(y + ymin + 1) * ow];
            for xx in 0..ow {
                ss[xx] += line[xx] as f64 * c;
            }
        }
        for xx in 0..ow {
            out[yy * ow + xx] = ss[xx] as f32;
        }
    }
    out
}

/// buildings._push_pull: c (h, w, nc) float32 with weights w (h, w) in [0, 1]: every pixel with w < 1 blended toward
/// a smooth fill from the weighted pyramid of the pixels around it.
fn push_pull(c: Vec<f32>, w: &[f32], h: usize, wd: usize, nc: usize) -> Vec<f32> {
    let (mut lo, mut hi) = (f32::INFINITY, f32::NEG_INFINITY);
    for &v in w { lo = lo.min(v); hi = hi.max(v); }
    if lo >= 1.0 || h.max(wd) <= 1 || hi <= 0.0 {
        return c;
    }
    let (hh, ww) = ((h + 1) / 2, (wd + 1) / 2);
    let at_w = |j: usize, i: usize| if j < h && i < wd { w[j * wd + i] } else { 0.0 };
    let mut sw = vec![0f32; hh * ww];
    let mut sc = vec![0f32; hh * ww * nc];
    for j in 0..hh {
        for i in 0..ww {
            let (j0, j1, i0, i1) = (2 * j, 2 * j + 1, 2 * i, 2 * i + 1);
            // numpy's order: (00 + 01) + (10 + 11) for the 2-d weights, ((00 + 01) + 10) + 11 for the 3-d colours
            sw[j * ww + i] = (at_w(j0, i0) + at_w(j0, i1)) + (at_w(j1, i0) + at_w(j1, i1));
            for ch in 0..nc {
                let p = |jj: usize, ii: usize| if jj < h && ii < wd {
                    c[(jj * wd + ii) * nc + ch] * w[jj * wd + ii]
                } else { 0.0 };
                sc[(j * ww + i) * nc + ch] = ((p(j0, i0) + p(j0, i1)) + p(j1, i0)) + p(j1, i1);
            }
        }
    }
    let mut cc = vec![0f32; hh * ww * nc];
    let mut cw = vec![0f32; hh * ww];
    for q in 0..hh * ww {
        let d = sw[q].max(1e-6f32);
        for ch in 0..nc {
            cc[q * nc + ch] = sc[q * nc + ch] / d;
        }
        cw[q] = sw[q].min(1.0);
    }
    let coarse = push_pull(cc, &cw, hh, ww, nc);
    let mut out = c;
    let mut plane = vec![0f32; hh * ww];
    let (kx, ky) = (bilinear_coeffs(ww, ww * 2), bilinear_coeffs(hh, hh * 2));
    for ch in 0..nc {
        for q in 0..hh * ww { plane[q] = coarse[q * nc + ch]; }
        let up = resize_f(&plane, hh, ww, hh * 2, ww * 2, &kx, &ky);
        for j in 0..h {
            for i in 0..wd {
                let q = j * wd + i;
                let wv = w[q];
                out[q * nc + ch] = out[q * nc + ch] * wv + up[j * ww * 2 + i] * (1.0 - wv);
            }
        }
    }
    out
}

/// Pillow's _gaussian_blur_radius (float arithmetic, its doubles where the C has them).
fn gaussian_box_radius(radius: f32, passes: i32) -> f32 {
    let sigma2: f32 = radius * radius / passes as f32;
    let big_l: f32 = (12.0f64 * sigma2 as f64 + 1.0).sqrt() as f32;
    let l: f32 = ((big_l as f64 - 1.0) / 2.0).floor() as f32;
    let mut a: f32 = (2.0 * l + 1.0) * (l * (l + 1.0) - 3.0 * sigma2);
    a /= 6.0 * (sigma2 - (l + 1.0) * (l + 1.0));
    l + a
}

/// Pillow's ImagingLineBoxBlur8 on one line of one channel (stride: the step between its samples).
fn line_box_blur(out: &mut [u8], inp: &[u8], radius: i32, edge_a: i32, edge_b: i32, ww: u32, fw: u32) {
    let lastx = inp.len() as i32 - 1;
    let g = |x: i32| inp[x as usize] as u32;
    let mut acc: u32 = g(0).wrapping_mul((radius + 1) as u32);
    for x in 0..edge_a - 1 {
        acc = acc.wrapping_add(g(x));
    }
    acc = acc.wrapping_add(g(lastx).wrapping_mul((radius - edge_a + 1) as u32));
    let mut step = |x: i32, sub: i32, add: i32, left: i32, right: i32, acc: &mut u32| {
        *acc = acc.wrapping_add(g(add).wrapping_sub(g(sub)));
        let bulk = acc.wrapping_mul(ww).wrapping_add((g(left) + g(right)).wrapping_mul(fw));
        out[x as usize] = (bulk.wrapping_add(1 << 23) >> 24) as u8;
    };
    if edge_a <= edge_b {
        for x in 0..edge_a { step(x, 0, x + radius, 0, x + radius + 1, &mut acc); }
        for x in edge_a..edge_b { step(x, x - radius - 1, x + radius, x - radius - 1, x + radius + 1, &mut acc); }
        for x in edge_b..=lastx { step(x, x - radius - 1, lastx, x - radius - 1, lastx, &mut acc); }
    } else {
        for x in 0..edge_b { step(x, 0, x + radius, 0, x + radius + 1, &mut acc); }
        for x in edge_b..edge_a { step(x, 0, lastx, 0, lastx, &mut acc); }
        for x in edge_a..=lastx { step(x, x - radius - 1, lastx, x - radius - 1, lastx, &mut acc); }
    }
}

/// Pillow's ImagingHorizontalBoxBlur over n lines of length len (one channel each, contiguous), in place.
fn box_blur_lines(data: &mut [u8], len: usize, float_radius: f32) {
    let radius = float_radius as i32;
    let ww: u32 = ((1u32 << 24) as f32 / (float_radius * 2.0 + 1.0)) as u32;
    let fw: u32 = ((1u32 << 24).wrapping_sub(((radius * 2 + 1) as u32).wrapping_mul(ww))) / 2;
    let edge_a = (radius + 1).min(len as i32);
    let edge_b = (len as i32 - radius - 1).max(0);
    let mut line = vec![0u8; len];
    for chunk in data.chunks_mut(len) {
        line_box_blur(&mut line, chunk, radius, edge_a, edge_b, ww, fw);
        chunk.copy_from_slice(&line);
    }
}

/// box_blur_lines down the columns of an (h, w) plane, in place: the same sums, every column's window moved a row at a
/// time (Pillow transposes the image and blurs its rows; the numbers are the same).
fn box_blur_cols(data: &mut [u8], h: usize, w: usize, float_radius: f32) {
    let radius = float_radius as i32;
    let ww: u32 = ((1u32 << 24) as f32 / (float_radius * 2.0 + 1.0)) as u32;
    let fw: u32 = ((1u32 << 24).wrapping_sub(((radius * 2 + 1) as u32).wrapping_mul(ww))) / 2;
    let len = h as i32;
    let edge_a = (radius + 1).min(len);
    let edge_b = (len - radius - 1).max(0);
    let lastx = len - 1;
    let row = |y: i32| y as usize * w;
    let mut acc = vec![0u32; w];
    for i in 0..w {
        let mut a = (data[i] as u32).wrapping_mul((radius + 1) as u32);
        for y in 0..edge_a - 1 {
            a = a.wrapping_add(data[row(y) + i] as u32);
        }
        acc[i] = a.wrapping_add((data[row(lastx) + i] as u32).wrapping_mul((radius - edge_a + 1) as u32));
    }
    let mut out = vec![0u8; h * w];
    let mut step = |y: i32, sub: i32, add: i32, left: i32, right: i32| {
        let (rs, ra, rl, rr, ro) = (row(sub), row(add), row(left), row(right), row(y));
        for i in 0..w {
            let a = acc[i].wrapping_add((data[ra + i] as u32).wrapping_sub(data[rs + i] as u32));
            acc[i] = a;
            let bulk = a.wrapping_mul(ww).wrapping_add((data[rl + i] as u32 + data[rr + i] as u32).wrapping_mul(fw));
            out[ro + i] = (bulk.wrapping_add(1 << 23) >> 24) as u8;
        }
    };
    if edge_a <= edge_b {
        for y in 0..edge_a { step(y, 0, y + radius, 0, y + radius + 1); }
        for y in edge_a..edge_b { step(y, y - radius - 1, y + radius, y - radius - 1, y + radius + 1); }
        for y in edge_b..=lastx { step(y, y - radius - 1, lastx, y - radius - 1, lastx); }
    } else {
        for y in 0..edge_b { step(y, 0, y + radius, 0, y + radius + 1); }
        for y in edge_b..edge_a { step(y, 0, lastx, 0, lastx); }
        for y in edge_a..=lastx { step(y, y - radius - 1, lastx, y - radius - 1, lastx); }
    }
    data.copy_from_slice(&out);
}

/// Pillow's GaussianBlur(radius) of an (h, w, nc) 8-bit image, each channel apart (as its 4-byte pixels).
fn gaussian_blur(img: &[u8], h: usize, w: usize, nc: usize, radius: f32) -> Vec<u8> {
    let r = gaussian_box_radius(radius, 3);
    let mut out = vec![0u8; h * w * nc];
    if r == 0.0 {
        out.copy_from_slice(img);
        return out;
    }
    // the channels apart on their own threads (the same work; a block photo waits for it)
    let planes: Vec<Vec<u8>> = std::thread::scope(|sc| {
        let jobs: Vec<_> = (0..nc).map(|ch| sc.spawn(move || {
            let mut plane: Vec<u8> = (0..h * w).map(|q| img[q * nc + ch]).collect();
            for _ in 0..3 { box_blur_lines(&mut plane, w, r); }
            for _ in 0..3 { box_blur_cols(&mut plane, h, w, r); }
            plane
        })).collect();
        jobs.into_iter().map(|j| j.join().unwrap()).collect()
    });
    for (ch, plane) in planes.iter().enumerate() {
        for q in 0..h * w { out[q * nc + ch] = plane[q]; }
    }
    out
}

/// buildings.fill_photo: rgb (h, w, 3) with the masked pixels replaced by a smooth fill from the unmasked ground around
/// them (the push-pull pyramid), softened by a Gaussian blur of `radius` pixels (Pillow's). Returns a new array.
#[pyfunction]
pub fn fill_photo<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, mask: PyReadonlyArray2<'py, bool>, radius: f64)
                       -> PyResult<Bound<'py, numpy::PyArray3<u8>>> {
    let img = rgb.as_array();
    let m = mask.as_array();
    let (h, w, nc) = (img.shape()[0], img.shape()[1], img.shape()[2]);
    if m.shape() != [h, w] {
        return Err(pyo3::exceptions::PyValueError::new_err("fill_photo: rgb (h, w, c), mask (h, w)"));
    }
    let src: Vec<u8> = img.iter().copied().collect();
    let msk: Vec<bool> = m.iter().copied().collect();
    let out = py.detach(move || {
        if !msk.iter().any(|&v| !v) {
            return src;
        }
        let c: Vec<f32> = src.iter().map(|&v| v as f32).collect();
        let wt: Vec<f32> = msk.iter().map(|&v| if v { 0.0 } else { 1.0 }).collect();
        let filled = push_pull(c, &wt, h, w, nc);
        let clipped: Vec<u8> = filled.iter().map(|&v| v.clamp(0.0, 255.0) as u8).collect();
        let soft = gaussian_blur(&clipped, h, w, nc, radius as f32);
        let mut res = src;
        for q in 0..h * w {
            if msk[q] {
                for ch in 0..nc { res[q * nc + ch] = soft[q * nc + ch]; }
            }
        }
        res
    });
    Ok(numpy::ndarray::Array3::from_shape_vec((h, w, nc), out)
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?
        .into_pyarray(py))
}
