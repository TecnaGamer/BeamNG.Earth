//! The mod's block-compressed textures made at install time (2026-11-13): the download carries the textures' lossless
//! sources (PNG, ~10x smaller than BC7 once zipped), install.py / the launcher encode them here into the exact DDS the
//! game loads (it must never cook a PNG at run time: the TextureCooker deadlock, pmtiles-freeze 2026-11-11).
//!
//! One call encodes a batch: every source decoded and its mip chain made (one texture per worker), then every mip level
//! cut into strips of block rows that all the workers encode, then each DDS written through a temporary file renamed
//! into place. No interpreter lock is held. Deterministic: the same source gives the same bytes on every machine
//! (texture-compressor's backends all produce its scalar bytes; the mips are plain f32 sums with libm's powers, in a
//! fixed order), so a DDS made on one machine is the one made on any other.
//!
//! Mips are the old dev tool's (tools/mod_textures_dds.py, which made the shipped DDS of 2026-11-11 with bc7enc): the
//! full chain to 1 x 1, 2 x 2 box averages: colour in linear light (alpha-weighted where alpha varies), data as they
//! are, normals as unit vectors renormalised at each level. Formats: BC7 (sRGB-flagged or not), BC5 (normal x, y), BC4.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;
use texture_compressor::{encode, Effort, Format, ImageView, Settings};

/// The encoder's identity, part of every cached DDS's key (install.py): bump it when the bytes for a source change.
pub const TEXDDS_ID: &str = "texdds-1 texture-compressor-0.2.4 quality";

const DXGI_BC4: u32 = 80;
const DXGI_BC5: u32 = 83;
const DXGI_BC7: u32 = 98;
const DXGI_BC7_SRGB: u32 = 99;
const STRIP_ROWS: usize = 64; // (texel rows per work item: 16 block rows)

#[derive(Clone, Copy, PartialEq)]
enum Role {
    Color,
    Normal,
    Data,
}

/// One mip level, as the encoder takes it: `chans` interleaved bytes per texel (1 BC4, 2 BC5, 4 BC7).
struct Level {
    w: usize,
    h: usize,
    chans: usize,
    px: Vec<u8>,
}

struct Prepared {
    dxgi: u32,
    levels: Vec<Level>,
}

fn parse_format(f: &str) -> Result<u32, String> {
    Ok(match f {
        "BC4" => DXGI_BC4,
        "BC5" => DXGI_BC5,
        "BC7" => DXGI_BC7,
        "BC7_SRGB" => DXGI_BC7_SRGB,
        _ => return Err(format!("unknown format {f:?} (BC4, BC5, BC7, BC7_SRGB)")),
    })
}

fn parse_role(r: &str) -> Result<Role, String> {
    Ok(match r {
        "color" => Role::Color,
        "normal" => Role::Normal,
        "data" => Role::Data,
        _ => return Err(format!("unknown role {r:?} (color, normal, data)")),
    })
}

/// (width, height, channels 1 / 3 / 4, 8-bit texels) of a PNG: grey stays one channel, grey + alpha and palettes
/// become RGB(A); an alpha channel that is 255 everywhere is dropped (as the dev tool's load()).
fn decode_png(data: &[u8]) -> Result<(usize, usize, usize, Vec<u8>), String> {
    let mut dec = png::Decoder::new(std::io::Cursor::new(data));
    dec.set_transformations(png::Transformations::EXPAND | png::Transformations::STRIP_16);
    let mut reader = dec.read_info().map_err(|e| format!("png: {e}"))?;
    let mut buf = vec![0u8; reader.output_buffer_size()];
    let info = reader.next_frame(&mut buf).map_err(|e| format!("png: {e}"))?;
    buf.truncate(info.buffer_size());
    let (w, h) = (info.width as usize, info.height as usize);
    let n = w * h;
    let (chans, px): (usize, Vec<u8>) = match info.color_type {
        png::ColorType::Grayscale => (1, buf),
        png::ColorType::GrayscaleAlpha => {
            let mut o = Vec::with_capacity(n * 4);
            for t in buf.chunks_exact(2) {
                o.extend_from_slice(&[t[0], t[0], t[0], t[1]]);
            }
            (4, o)
        }
        png::ColorType::Rgb => (3, buf),
        png::ColorType::Rgba => (4, buf),
        png::ColorType::Indexed => return Err("png: palette not expanded".into()),
    };
    if px.len() != n * chans {
        return Err("png: unexpected buffer size".into());
    }
    if chans == 4 && px.chunks_exact(4).all(|t| t[3] == 255) {
        let mut o = Vec::with_capacity(n * 3);
        for t in px.chunks_exact(4) {
            o.extend_from_slice(&t[..3]);
        }
        return Ok((w, h, 3, o));
    }
    Ok((w, h, chans, px))
}

/// (width, height, channels 3 / 4, 8-bit texels) of a WebP (lossless or not; alpha 255 everywhere dropped).
fn decode_webp(data: &[u8]) -> Result<(usize, usize, usize, Vec<u8>), String> {
    let mut d = image_webp::WebPDecoder::new(std::io::Cursor::new(data)).map_err(|e| format!("webp: {e}"))?;
    if d.is_animated() {
        return Err("webp: animated".into());
    }
    let (w, h) = d.dimensions();
    let (w, h) = (w as usize, h as usize);
    let chans = if d.has_alpha() { 4 } else { 3 };
    let mut buf = vec![0u8; d.output_buffer_size().ok_or("webp: too big")?];
    d.read_image(&mut buf).map_err(|e| format!("webp: {e}"))?;
    if buf.len() != w * h * chans {
        return Err("webp: unexpected buffer size".into());
    }
    if chans == 4 && buf.chunks_exact(4).all(|t| t[3] == 255) {
        let mut o = Vec::with_capacity(w * h * 3);
        for t in buf.chunks_exact(4) {
            o.extend_from_slice(&t[..3]);
        }
        return Ok((w, h, 3, o));
    }
    Ok((w, h, chans, buf))
}

fn decode_image(data: &[u8]) -> Result<(usize, usize, usize, Vec<u8>), String> {
    if data.len() >= 12 && &data[..4] == b"RIFF" && &data[8..12] == b"WEBP" {
        decode_webp(data)
    } else if data.starts_with(b"\x89PNG") {
        decode_png(data)
    } else {
        Err("not a PNG or WebP".into())
    }
}

// --- maps derived from a colour map --------------------------------------------------------------------------------
// The texture sets' normal and roughness maps are functions of their colour map (the dev tools that made them:
// tools/ingest_rail_textures.py normal_map / roughness_map, tools/make_sett_texture.py normal_map / roughness), so the
// download carries the colour map only and the other two are made here, the same bytes as those tools write
// (checked on all 28 maps, server/tests/test_mod_textures.py).

/// In-place radix-2 FFT of (re, im), n a power of two; inverse: the conjugate transform divided by n.
fn fft(re: &mut [f64], im: &mut [f64], inverse: bool) {
    let n = re.len();
    let mut j = 0;
    for i in 1..n {
        let mut bit = n >> 1;
        while j & bit != 0 {
            j ^= bit;
            bit >>= 1;
        }
        j |= bit;
        if i < j {
            re.swap(i, j);
            im.swap(i, j);
        }
    }
    let sign = if inverse { 1.0 } else { -1.0 };
    let mut len = 2;
    while len <= n {
        let ang = sign * 2.0 * std::f64::consts::PI / len as f64;
        for k in 0..len / 2 {
            let (wr, wi) = (libm::cos(ang * k as f64), libm::sin(ang * k as f64));
            let mut s = 0;
            while s < n {
                let (a, b) = (s + k, s + k + len / 2);
                let (xr, xi) = (re[b] * wr - im[b] * wi, re[b] * wi + im[b] * wr);
                re[b] = re[a] - xr;
                im[b] = im[a] - xi;
                re[a] += xr;
                im[a] += xi;
                s += len;
            }
        }
        len <<= 1;
    }
    if inverse {
        let inv = 1.0 / n as f64;
        for v in re.iter_mut().chain(im.iter_mut()) {
            *v *= inv;
        }
    }
}

/// ingest_rail_textures.blur: a periodic gaussian blur by the FFT, the transfer exp(-2 pi^2 sigma^2 (fx^2 + fy^2)) at
/// numpy's fftfreq, of a [h][w] field (sides powers of two). Separable: rows, then columns.
fn blur_fft(g: &mut [f64], w: usize, h: usize, sigma: f64) {
    let freq = |k: usize, n: usize| -> f64 {
        let k = if k < n.div_ceil(2) { k as f64 } else { k as f64 - n as f64 };
        k / n as f64
    };
    let pass = |data: &mut Vec<f64>, n: usize, count: usize, stride: usize, step: usize| {
        let gain: Vec<f64> = (0..n)
            .map(|k| {
                let f = freq(k, n);
                libm::exp(-2.0 * (std::f64::consts::PI * sigma).powi(2) * (f * f))
            })
            .collect();
        let (mut re, mut im) = (vec![0f64; n], vec![0f64; n]);
        for c in 0..count {
            for i in 0..n {
                re[i] = data[c * stride + i * step];
                im[i] = 0.0;
            }
            fft(&mut re, &mut im, false);
            for i in 0..n {
                re[i] *= gain[i];
                im[i] *= gain[i];
            }
            fft(&mut re, &mut im, true);
            for i in 0..n {
                data[c * stride + i * step] = re[i];
            }
        }
    };
    let mut v = g.to_vec();
    pass(&mut v, w, h, w, 1);
    pass(&mut v, h, w, 1, w);
    g.copy_from_slice(&v);
}

/// The mean of each texel's first three channels, as numpy's a.mean(2) (r + g + b, then / 3).
fn luminance(w: usize, h: usize, c: usize, px: &[u8]) -> Vec<f64> {
    (0..w * h)
        .map(|i| {
            let t = &px[i * c..i * c + c];
            if c >= 3 {
                (t[0] as f64 + t[1] as f64 + t[2] as f64) / 3.0
            } else {
                t[0] as f64
            }
        })
        .collect()
}

/// ingest_rail_textures.normal_map(a, strength): RGB.
fn normal_fft(w: usize, h: usize, c: usize, px: &[u8], strength: f64) -> Vec<u8> {
    let mut g: Vec<f64> = luminance(w, h, c, px).into_iter().map(|v| v / 255.0).collect();
    blur_fft(&mut g, w, h, 0.8);
    let sc = strength * h as f64 / 64.0;
    let mut out = vec![0u8; w * h * 3];
    for y in 0..h {
        for x in 0..w {
            let at = |xx: usize, yy: usize| g[yy * w + xx];
            let dx = (at((x + 1) % w, y) - at((x + w - 1) % w, y)) * 0.5;
            let dy = (at(x, (y + 1) % h) - at(x, (y + h - 1) % h)) * 0.5;
            let n = [-dx * sc, dy * sc, 1.0];
            let l = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]).sqrt();
            for k in 0..3 {
                out[(y * w + x) * 3 + k] = (((n[k] / l) * 0.5 + 0.5) * 255.0 + 0.5) as u8;
            }
        }
    }
    out
}

/// ingest_rail_textures.roughness_map(a, lo, hi): one channel.
fn rough_fft(w: usize, h: usize, c: usize, px: &[u8], lo: f64, hi: f64) -> Vec<u8> {
    let g = luminance(w, h, c, px);
    let (mn, mx) = g.iter().fold((f64::INFINITY, f64::NEG_INFINITY), |(a, b), &v| (a.min(v), b.max(v)));
    let span = (mx - mn).max(1e-9);
    g.iter().map(|&v| ((hi - (hi - lo) * ((v - mn) / span)) * 255.0 + 0.5) as u8).collect()
}

/// Pillow's ImageFilter.GaussianBlur(radius) of an 8-bit grey image (libImaging/BoxBlur.c: three extended box blurs
/// along x, then three along y, edges extended, 8-bit rounding after each pass), bit for bit.
fn pil_gaussian_blur(img: &mut [u8], w: usize, h: usize, radius: f32) {
    let passes = 3i32;
    let sigma2: f32 = radius * radius / passes as f32;
    let big_l: f32 = (12.0f64 * sigma2 as f64 + 1.0).sqrt() as f32;
    let l: f32 = ((big_l as f64 - 1.0) / 2.0).floor() as f32;
    let mut a: f32 = (2.0 * l + 1.0) * (l * (l + 1.0) - 3.0 * sigma2);
    a /= 6.0 * (sigma2 - (l + 1.0) * (l + 1.0));
    let r: f32 = l + a;
    let line = |input: &[u8], out: &mut [u8]| {
        let n = input.len();
        let lastx = n as i64 - 1;
        let radius = r as i64;
        let ww: u32 = ((1u32 << 24) as f32 / (r * 2.0 + 1.0)) as u32;
        let fw: u32 = ((1u32 << 24).wrapping_sub(((radius * 2 + 1) as u32).wrapping_mul(ww))) / 2;
        let edge_a = (radius + 1).min(n as i64);
        let edge_b = (n as i64 - radius - 1).max(0);
        let px = |i: i64| input[i as usize] as u32;
        let mut acc: u32 = px(0).wrapping_mul((radius + 1) as u32);
        for x in 0..edge_a - 1 {
            acc = acc.wrapping_add(px(x));
        }
        acc = acc.wrapping_add(px(lastx).wrapping_mul((radius - edge_a + 1) as u32));
        let mut step = |x: i64, sub: i64, add: i64, left: i64, right: i64, acc: &mut u32| {
            *acc = acc.wrapping_add((px(add) as i32 - px(sub) as i32) as u32);
            let bulk = acc.wrapping_mul(ww).wrapping_add((px(left) + px(right)).wrapping_mul(fw));
            out[x as usize] = (bulk.wrapping_add(1 << 23) >> 24) as u8;
        };
        if edge_a <= edge_b {
            for x in 0..edge_a {
                step(x, 0, x + radius, 0, x + radius + 1, &mut acc);
            }
            for x in edge_a..edge_b {
                step(x, x - radius - 1, x + radius, x - radius - 1, x + radius + 1, &mut acc);
            }
            for x in edge_b..=lastx {
                step(x, x - radius - 1, lastx, x - radius - 1, lastx, &mut acc);
            }
        } else {
            for x in 0..edge_b {
                step(x, 0, x + radius, 0, x + radius + 1, &mut acc);
            }
            for x in edge_b..edge_a {
                step(x, 0, lastx, 0, lastx, &mut acc);
            }
            for x in edge_a..=lastx {
                step(x, x - radius - 1, lastx, x - radius - 1, lastx, &mut acc);
            }
        }
    };
    let mut tmp = vec![0u8; w.max(h)];
    for _ in 0..passes {
        for y in 0..h {
            line(&img[y * w..(y + 1) * w], &mut tmp[..w]);
            img[y * w..(y + 1) * w].copy_from_slice(&tmp[..w]);
        }
    }
    let mut col = vec![0u8; h];
    for _ in 0..passes {
        for x in 0..w {
            for y in 0..h {
                col[y] = img[y * w + x];
            }
            line(&col, &mut tmp[..h]);
            for y in 0..h {
                img[y * w + x] = tmp[y];
            }
        }
    }
}

/// make_sett_texture.normal_map(rgb) with NORMAL_STRENGTH = strength and SIZE = the image's width: RGB.
fn normal_box(w: usize, h: usize, c: usize, px: &[u8], strength: f64) -> Vec<u8> {
    let mut lum: Vec<u8> = luminance(w, h, c, px).into_iter().map(|v| v.clamp(0.0, 255.0) as u8).collect();
    pil_gaussian_blur(&mut lum, w, h, 1.4);
    let g: Vec<f64> = lum.iter().map(|&v| v as f64 / 255.0).collect();
    let size = w as f64;
    let mut out = vec![0u8; w * h * 3];
    for y in 0..h {
        for x in 0..w {
            let at = |xx: usize, yy: usize| g[yy * w + xx];
            let dx = (at((x + 1) % w, y) - at((x + w - 1) % w, y)) * 0.5 * size / 64.0;
            let dy = (at(x, (y + 1) % h) - at(x, (y + h - 1) % h)) * 0.5 * size / 64.0;
            let n = [-dx * strength, dy * strength, 1.0];
            let l = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]).sqrt();
            for k in 0..3 {
                out[(y * w + x) * 3 + k] = (((n[k] / l) * 0.5 + 0.5) * 255.0) as u8;
            }
        }
    }
    out
}

/// numpy's np.percentile(v, q) (linear: virtual index n q / 100 + (1 - q / 100) - 1, numpy's _lerp) of sorted values.
fn percentile(sorted: &[f64], q: f64) -> f64 {
    let n = sorted.len();
    let qf = q / 100.0;
    let virt = n as f64 * qf + (1.0 + qf * (1.0 - 1.0 - 1.0)) - 1.0;
    let lo = virt.floor();
    let t = virt - lo;
    let i = (lo.max(0.0) as usize).min(n - 1);
    let (a, b) = (sorted[i], sorted[(i + 1).min(n - 1)]);
    let d = b - a;
    if t >= 0.5 {
        b - d * (1.0 - t)
    } else {
        a + d * t
    }
}

/// make_sett_texture.roughness(rgb): grey as RGB.
fn rough_box(w: usize, h: usize, c: usize, px: &[u8]) -> Vec<u8> {
    let lum = luminance(w, h, c, px);
    let mut s = lum.clone();
    s.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let (lo, hi) = (percentile(&s, 5.0), percentile(&s, 95.0));
    let span = (hi - lo).max(1.0);
    let mut out = Vec::with_capacity(w * h * 3);
    for &v in &lum {
        let t = ((v - lo) / span).clamp(0.0, 1.0);
        let r = ((0.97 - 0.17 * t) * 255.0) as u8;
        out.extend_from_slice(&[r, r, r]);
    }
    out
}

/// The derived map named by `derive` ("normal_fft:S", "rough_fft:LO:HI", "normal_box:S", "rough_box") of a colour map:
/// (channels, texels).
fn derive_map(w: usize, h: usize, c: usize, px: &[u8], derive: &str) -> Result<(usize, Vec<u8>), String> {
    let parts: Vec<&str> = derive.split(':').collect();
    let num = |i: usize| -> Result<f64, String> {
        parts.get(i).ok_or(format!("derive {derive:?}: a number missing"))?.parse::<f64>().map_err(|e| format!("derive {derive:?}: {e}"))
    };
    Ok(match parts[0] {
        "normal_fft" => (3, normal_fft(w, h, c, px, num(1)?)),
        "rough_fft" => (1, rough_fft(w, h, c, px, num(1)?, num(2)?)),
        "normal_box" => (3, normal_box(w, h, c, px, num(1)?)),
        "rough_box" => (3, rough_box(w, h, c, px)),
        _ => return Err(format!("unknown derive {derive:?}")),
    })
}

/// A float image [h][w][c] halved: each texel the mean of its 2 x 2 (or 2 x 1 / 1 x 2 at a side of 1) source texels.
fn half(src: &[f32], w: usize, h: usize, c: usize) -> (Vec<f32>, usize, usize) {
    let (nw, nh) = ((w / 2).max(1), (h / 2).max(1));
    let (sx, sy) = (if w > 1 { 2 } else { 1 }, if h > 1 { 2 } else { 1 });
    let inv = 1.0f32 / (sx * sy) as f32;
    let mut out = vec![0f32; nw * nh * c];
    for y in 0..nh {
        for x in 0..nw {
            for k in 0..c {
                let mut s = 0f32;
                for dy in 0..sy {
                    for dx in 0..sx {
                        s += src[((y * sy + dy) * w + (x * sx + dx)) * c + k];
                    }
                }
                out[(y * nw + x) * c + k] = s * inv;
            }
        }
    }
    (out, nw, nh)
}

fn srgb_to_linear(v: u8) -> f32 {
    let x = v as f64 / 255.0;
    (if x <= 0.04045 { x / 12.92 } else { libm::pow((x + 0.055) / 1.055, 2.4) }) as f32
}

fn linear_to_srgb(v: f32) -> u8 {
    let x = (v as f64).clamp(0.0, 1.0);
    let s = if x <= 0.0031308 { x * 12.92 } else { 1.055 * libm::pow(x, 1.0 / 2.4) - 0.055 };
    (s * 255.0).round_ties_even().clamp(0.0, 255.0) as u8
}

fn q8(v: f32) -> u8 {
    v.round_ties_even().clamp(0.0, 255.0) as u8
}

/// The mip chain [level 0, 1, ...] down to 1 x 1, each level with `c` channels as made (1, 3 or 4).
fn mip_chain(w: usize, h: usize, c: usize, px: Vec<u8>, role: Role) -> Vec<(usize, usize, Vec<u8>)> {
    let mut levels = vec![];
    match role {
        Role::Normal => {
            let n = w * h;
            let mut cur = vec![0f32; n * 3];
            for i in 0..n {
                let mut v = [0f32; 3];
                for k in 0..3 {
                    v[k] = px[i * c + k.min(c - 1)] as f32 / 127.5 - 1.0;
                }
                let l = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt().max(1e-6);
                for k in 0..3 {
                    cur[i * 3 + k] = v[k] / l;
                }
            }
            levels.push((w, h, px));
            let (mut cw, mut ch) = (w, h);
            while cw.max(ch) > 1 {
                let (mut nxt, nw, nh) = half(&cur, cw, ch, 3);
                let mut u = vec![0u8; nw * nh * 3];
                for i in 0..nw * nh {
                    let v = &mut nxt[i * 3..i * 3 + 3];
                    let l = (v[0] * v[0] + v[1] * v[1] + v[2] * v[2]).sqrt().max(1e-6);
                    for k in 0..3 {
                        v[k] /= l;
                        u[i * 3 + k] = q8((v[k] + 1.0) * 127.5);
                    }
                }
                levels.push((nw, nh, u));
                cur = nxt;
                cw = nw;
                ch = nh;
            }
            // (a grey source's normal map: levels of 3 channels from one; level 0 made 3 to match)
            if c != 3 {
                let (lw, lh, p0) = &levels[0];
                let mut o = Vec::with_capacity(lw * lh * 3);
                for i in 0..lw * lh {
                    for k in 0..3 {
                        o.push(p0[i * c + k.min(c - 1)]);
                    }
                }
                levels[0] = (*lw, *lh, o);
            }
        }
        Role::Color if c >= 3 => {
            let n = w * h;
            let lut: Vec<f32> = (0..=255u8).map(srgb_to_linear).collect();
            let alpha = c == 4;
            let mut rgb = vec![0f32; n * 3];
            let mut a = vec![0f32; if alpha { n } else { 0 }];
            for i in 0..n {
                for k in 0..3 {
                    rgb[i * 3 + k] = lut[px[i * c + k] as usize];
                }
                if alpha {
                    a[i] = px[i * 4 + 3] as f32 / 255.0;
                }
            }
            levels.push((w, h, px));
            let (mut cw, mut ch) = (w, h);
            while cw.max(ch) > 1 {
                if alpha {
                    let mut pre = vec![0f32; cw * ch * 4];
                    for i in 0..cw * ch {
                        for k in 0..3 {
                            pre[i * 4 + k] = rgb[i * 3 + k] * a[i];
                        }
                        pre[i * 4 + 3] = a[i];
                    }
                    let (pre, nw, nh) = half(&pre, cw, ch, 4);
                    let (avg, _, _) = half(&rgb, cw, ch, 3);
                    let mut nrgb = vec![0f32; nw * nh * 3];
                    let mut na = vec![0f32; nw * nh];
                    let mut u = vec![0u8; nw * nh * 4];
                    for i in 0..nw * nh {
                        let al = pre[i * 4 + 3];
                        for k in 0..3 {
                            nrgb[i * 3 + k] = if al > 1e-4 { pre[i * 4 + k] / al.max(1e-4) } else { avg[i * 3 + k] };
                            u[i * 4 + k] = linear_to_srgb(nrgb[i * 3 + k]);
                        }
                        na[i] = al;
                        u[i * 4 + 3] = q8(al * 255.0);
                    }
                    levels.push((nw, nh, u));
                    rgb = nrgb;
                    a = na;
                    cw = nw;
                    ch = nh;
                } else {
                    let (nrgb, nw, nh) = half(&rgb, cw, ch, 3);
                    let u = nrgb.iter().map(|&v| linear_to_srgb(v)).collect();
                    levels.push((nw, nh, u));
                    rgb = nrgb;
                    cw = nw;
                    ch = nh;
                }
            }
        }
        _ => {
            let mut cur: Vec<f32> = px.iter().map(|&v| v as f32).collect();
            levels.push((w, h, px));
            let (mut cw, mut ch) = (w, h);
            while cw.max(ch) > 1 {
                let (nxt, nw, nh) = half(&cur, cw, ch, c);
                levels.push((nw, nh, nxt.iter().map(|&v| q8(v)).collect()));
                cur = nxt;
                cw = nw;
                ch = nh;
            }
        }
    }
    levels
}

/// A level's texels in the encoder's layout: `chans` 1 (BC4: the first channel), 2 (BC5: x, y), 4 (BC7: RGBA, grey
/// repeated, alpha 255 where there is none).
fn to_encoder(w: usize, h: usize, c: usize, px: &[u8], dxgi: u32) -> Level {
    let n = w * h;
    let chans = match dxgi {
        DXGI_BC4 => 1,
        DXGI_BC5 => 2,
        _ => 4,
    };
    let mut o = Vec::with_capacity(n * chans);
    for i in 0..n {
        let t = &px[i * c..i * c + c];
        match chans {
            1 => o.push(t[0]),
            2 => o.extend_from_slice(&[t[0], t[c.min(2) - 1]]),
            _ => match c {
                1 => o.extend_from_slice(&[t[0], t[0], t[0], 255]),
                2 => o.extend_from_slice(&[t[0], t[0], t[0], t[1]]),
                3 => o.extend_from_slice(&[t[0], t[1], t[2], 255]),
                _ => o.extend_from_slice(t),
            },
        }
    }
    Level { w, h, chans, px: o }
}

fn prepare(src: &[u8], format: &str, role: &str, derive: &str) -> Result<Prepared, String> {
    let dxgi = parse_format(format)?;
    let role = parse_role(role)?;
    let (w, h, mut c, mut px) = decode_image(src)?;
    if !derive.is_empty() {
        (c, px) = derive_map(w, h, c, &px, derive)?;
    }
    if dxgi == DXGI_BC4 && c > 1 {
        px = px.chunks_exact(c).map(|t| t[0]).collect();
        c = 1;
    }
    if w == 0 || h == 0 || w & (w - 1) != 0 || h & (h - 1) != 0 {
        return Err(format!("{w} x {h}: the sides must be powers of two (the game refuses others compressed)"));
    }
    if dxgi == DXGI_BC5 && c < 2 {
        return Err("BC5 needs two channels".into());
    }
    let levels = mip_chain(w, h, c, px, role)
        .into_iter()
        .map(|(lw, lh, p)| {
            let lc = p.len() / (lw * lh);
            to_encoder(lw, lh, lc, &p, dxgi)
        })
        .collect();
    Ok(Prepared { dxgi, levels })
}

fn settings(dxgi: u32) -> Settings {
    let f = match dxgi {
        DXGI_BC4 => Format::Bc4,
        DXGI_BC5 => Format::Bc5,
        _ => Format::Bc7,
    };
    Settings::new(f, Effort::Quality)
}

/// The block bytes of rows [y0, y1) of a level (y0 a multiple of 4).
fn encode_rows(lv: &Level, dxgi: u32, y0: usize, y1: usize) -> Result<Vec<u8>, String> {
    let stride = lv.w * lv.chans;
    let data = &lv.px[y0 * stride..y1 * stride];
    let (w, h) = (lv.w as u32, (y1 - y0) as u32);
    let view = match lv.chans {
        1 => ImageView::r8(data, w, h, stride),
        2 => ImageView::rg8(data, w, h, stride),
        _ => ImageView::rgba8(data, w, h, stride),
    }
    .map_err(|e| format!("encoder: {e:?}"))?;
    encode(view, &settings(dxgi)).map_err(|e| format!("encoder: {e:?}"))
}

fn dds_header(w: usize, h: usize, mips: usize, dxgi: u32) -> Vec<u8> {
    let block = if dxgi == DXGI_BC4 { 8 } else { 16 };
    let linear = (w.div_ceil(4)).max(1) * (h.div_ceil(4)).max(1) * block;
    let mut o = Vec::with_capacity(148);
    let u = |o: &mut Vec<u8>, v: u32| o.extend_from_slice(&v.to_le_bytes());
    o.extend_from_slice(b"DDS ");
    u(&mut o, 124);
    u(&mut o, 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000); // CAPS HEIGHT WIDTH PIXELFORMAT MIPMAPCOUNT LINEARSIZE
    u(&mut o, h as u32);
    u(&mut o, w as u32);
    u(&mut o, linear as u32);
    u(&mut o, 1);
    u(&mut o, mips as u32);
    o.extend_from_slice(&[0u8; 44]);
    u(&mut o, 32);
    u(&mut o, 0x4);
    o.extend_from_slice(b"DX10");
    for _ in 0..5 {
        u(&mut o, 0);
    }
    u(&mut o, if mips > 1 { 0x401008 } else { 0x1000 });
    for _ in 0..4 {
        u(&mut o, 0);
    }
    u(&mut o, dxgi);
    u(&mut o, 3);
    u(&mut o, 0);
    u(&mut o, 1);
    u(&mut o, 0);
    o
}

/// Run f(i) for i in 0..n on `threads` workers (an index handed out at a time).
fn parallel<F: Fn(usize) + Sync>(n: usize, threads: usize, f: F) {
    let next = AtomicUsize::new(0);
    let workers = threads.max(1).min(n.max(1));
    std::thread::scope(|s| {
        for _ in 0..workers {
            s.spawn(|| loop {
                let i = next.fetch_add(1, Ordering::Relaxed);
                if i >= n {
                    break;
                }
                f(i);
            });
        }
    });
}


/// One job: (source bytes, format, role, derive).
type Job = (Vec<u8>, String, String, String);

/// DDS files of a batch of jobs; Err per job that failed.
fn encode_batch(jobs: &[Job], threads: usize) -> Vec<Result<Vec<u8>, String>> {
    let threads = if threads == 0 { std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4) } else { threads };
    // 1. sources decoded (and derived), mips made: one texture per worker, the biggest first
    let mut order: Vec<usize> = (0..jobs.len()).collect();
    order.sort_by_key(|&i| std::cmp::Reverse(jobs[i].0.len()));
    let prepared: Vec<Mutex<Option<Result<Prepared, String>>>> = (0..jobs.len()).map(|_| Mutex::new(None)).collect();
    parallel(order.len(), threads, |k| {
        let i = order[k];
        let r = prepare(&jobs[i].0, &jobs[i].1, &jobs[i].2, &jobs[i].3);
        *prepared[i].lock().unwrap() = Some(r);
    });
    let prepared: Vec<Result<Prepared, String>> =
        prepared.into_iter().map(|m| m.into_inner().unwrap().unwrap()).collect();
    // 2. every level cut into strips of block rows, all encoded by all the workers
    let mut items = vec![]; // (job, level, y0, y1)
    for (j, p) in prepared.iter().enumerate() {
        if let Ok(p) = p {
            for (l, lv) in p.levels.iter().enumerate() {
                let mut y = 0;
                while y < lv.h {
                    let y1 = (y + STRIP_ROWS).min(lv.h);
                    items.push((j, l, y, y1));
                    y = y1;
                }
            }
        }
    }
    let out: Vec<Mutex<Option<Result<Vec<u8>, String>>>> = (0..items.len()).map(|_| Mutex::new(None)).collect();
    parallel(items.len(), threads, |k| {
        let (j, l, y0, y1) = items[k];
        let p = prepared[j].as_ref().unwrap();
        *out[k].lock().unwrap() = Some(encode_rows(&p.levels[l], p.dxgi, y0, y1));
    });
    // 3. each DDS put together in order
    let mut files: Vec<Result<Vec<u8>, String>> = prepared
        .iter()
        .map(|p| match p {
            Ok(p) => Ok(dds_header(p.levels[0].w, p.levels[0].h, p.levels.len(), p.dxgi)),
            Err(e) => Err(e.clone()),
        })
        .collect();
    for (k, m) in out.into_iter().enumerate() {
        let j = items[k].0;
        let r = m.into_inner().unwrap().unwrap();
        if let Ok(f) = &mut files[j] {
            match r {
                Ok(b) => f.extend_from_slice(&b),
                Err(e) => files[j] = Err(e),
            }
        }
    }
    files
}

/// Encode a batch of sources into DDS files: jobs [(source path, DDS path, format, role, derive)]. A source is a PNG or
/// a WebP (lossless for these); format "BC7_SRGB" / "BC7" / "BC5" / "BC4"; role "color" / "normal" / "data" (how the
/// mips are made); derive "" (the source itself) or the map made from the source colour map ("normal_fft:S",
/// "rough_fft:LO:HI", "normal_box:S", "rough_box": the dev tools' own functions). threads 0: one per CPU. Each DDS is
/// written to "<path>.tmp" and renamed into place (its folder made). Returns one string per job: "" when written, else
/// what went wrong (the other jobs are still written).
#[pyfunction]
pub fn texture_dds_files(py: Python<'_>, jobs: Vec<(String, String, String, String, String)>, threads: usize) -> Vec<String> {
    py.detach(|| {
        let mut errs = vec![String::new(); jobs.len()];
        let mut work: Vec<Job> = vec![];
        let mut index = vec![];
        for (i, (src, _dst, fmt, role, derive)) in jobs.iter().enumerate() {
            match std::fs::read(src) {
                Ok(b) => {
                    work.push((b, fmt.clone(), role.clone(), derive.clone()));
                    index.push(i);
                }
                Err(e) => errs[i] = format!("{src}: {e}"),
            }
        }
        let files = encode_batch(&work, threads);
        drop(work);
        for (k, r) in files.into_iter().enumerate() {
            let i = index[k];
            let (src, dst) = (&jobs[i].0, &jobs[i].1);
            match r {
                Err(e) => errs[i] = format!("{src}: {e}"),
                Ok(data) => {
                    let tmp = format!("{dst}.tmp");
                    let res = std::path::Path::new(dst)
                        .parent()
                        .map_or(Ok(()), std::fs::create_dir_all)
                        .and_then(|_| std::fs::write(&tmp, &data))
                        .and_then(|_| std::fs::rename(&tmp, dst));
                    if let Err(e) = res {
                        let _ = std::fs::remove_file(&tmp);
                        errs[i] = format!("{dst}: {e}");
                    }
                }
            }
        }
        errs
    })
}

/// One DDS from a source's bytes (format, role and derive as texture_dds_files); threads 0: one per CPU.
#[pyfunction]
#[pyo3(signature = (source, format, role, derive="", threads=0))]
pub fn texture_dds<'py>(
    py: Python<'py>,
    source: &[u8],
    format: &str,
    role: &str,
    derive: &str,
    threads: usize,
) -> PyResult<Bound<'py, PyBytes>> {
    let job = vec![(source.to_vec(), format.to_string(), role.to_string(), derive.to_string())];
    let r = py.detach(|| encode_batch(&job, threads)).pop().unwrap();
    match r {
        Ok(b) => Ok(PyBytes::new(py, &b)),
        Err(e) => Err(PyValueError::new_err(e)),
    }
}

/// (width, height, channels, texels) of a source as the encoder sees it (decoded, the derived map made when derive is
/// given; before the BC4 channel pick and the mips): for the tests and the dev tools.
#[pyfunction]
#[pyo3(signature = (source, derive=""))]
pub fn texture_pixels<'py>(py: Python<'py>, source: &[u8], derive: &str) -> PyResult<(usize, usize, usize, Bound<'py, PyBytes>)> {
    let r = py.detach(|| -> Result<(usize, usize, usize, Vec<u8>), String> {
        let (w, h, c, px) = decode_image(source)?;
        if derive.is_empty() {
            return Ok((w, h, c, px));
        }
        let (c, px) = derive_map(w, h, c, &px, derive)?;
        Ok((w, h, c, px))
    });
    let (w, h, c, px) = r.map_err(PyValueError::new_err)?;
    Ok((w, h, c, PyBytes::new(py, &px)))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(texture_dds_files, m)?)?;
    m.add_function(wrap_pyfunction!(texture_dds, m)?)?;
    m.add_function(wrap_pyfunction!(texture_pixels, m)?)?;
    m.add("TEXDDS_ID", TEXDDS_ID)?;
    Ok(())
}
