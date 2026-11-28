//! A terrain block's provisional base photograph from the far tiles' textures already on disk
//! (earthd.Server.write_quick_base): no imagery tile read, no network. The far field draws these textures
//! round the car; a block that came before its own photo showed the level's flat brown base, or its far
//! tiles at their own height over the drivable block (the user, 2026-11-21). Drawn from them, the block
//! shows the ground the far field showed there, on its own geometry, until its photo comes.
//!
//! The far textures are imagery.encode_dds's: BC1 (DXT1, or its DX10 sRGB header), a full mip chain, row 0
//! north, the tile's footprint plus imagery.gutter(px) texels per side. Each output texel takes the last
//! listed tile whose footprint holds its centre (the caller lists them coarsest first), bilinear at the
//! tile's mip whose texel is nearest the output's without being coarser. Texels no tile holds keep
//! `under` (when given), else are filled from their surroundings (push-pull).
use numpy::{ndarray::Array3, IntoPyArray, PyArray3, PyReadonlyArray3, PyUntypedArrayMethods};
use pyo3::{exceptions::PyValueError, prelude::*};

/// imagery.gutter(px): texels each side of a far texture outside its tile's footprint.
fn gutter(px: usize) -> usize {
    (px / 32).clamp(1, 8)
}

struct Dds<'a> {
    w: usize,
    h: usize,
    levels: usize,
    data: &'a [u8],
}

/// The BC1 blocks of a far texture's DDS, or None (another format, short).
fn parse(b: &[u8]) -> Option<Dds<'_>> {
    if b.len() < 128 || &b[..4] != b"DDS " {
        return None;
    }
    let u = |o: usize| u32::from_le_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]]) as usize;
    let (flags, h, w) = (u(8), u(12), u(16));
    let levels = if flags & 0x20000 != 0 { u(28).max(1) } else { 1 };
    let start = match &b[84..88] {
        b"DXT1" => 128,
        b"DX10" if b.len() >= 148 && matches!(u(128), 71 | 72) => 148,
        _ => return None,
    };
    if w == 0 || h == 0 || w > 1 << 15 || h > 1 << 15 {
        return None;
    }
    Some(Dds { w, h, levels, data: &b[start..] })
}

fn mip_dims(d: &Dds, k: usize) -> (usize, usize) {
    ((d.w >> k).max(1), (d.h >> k).max(1))
}

fn level_bytes(w: usize, h: usize) -> usize {
    w.div_ceil(4) * h.div_ceil(4) * 8
}

fn rgb565(c: u16) -> [i32; 3] {
    let (r, g, b) = ((c >> 11) as i32 & 31, (c >> 5) as i32 & 63, c as i32 & 31);
    [(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 3)]
}

/// Mip k of a BC1 texture as RGB (w * h * 3), or None (the file is short).
fn decode_level(d: &Dds, k: usize) -> Option<(usize, usize, Vec<u8>)> {
    let mut off = 0;
    for j in 0..k {
        let (w, h) = mip_dims(d, j);
        off += level_bytes(w, h);
    }
    let (w, h) = mip_dims(d, k);
    let blocks = d.data.get(off..off + level_bytes(w, h))?;
    let (bw, bh) = (w.div_ceil(4), h.div_ceil(4));
    let mut out = vec![0u8; w * h * 3];
    for by in 0..bh {
        for bx in 0..bw {
            let b = &blocks[(by * bw + bx) * 8..(by * bw + bx) * 8 + 8];
            let (c0, c1) = (u16::from_le_bytes([b[0], b[1]]), u16::from_le_bytes([b[2], b[3]]));
            let (a, e) = (rgb565(c0), rgb565(c1));
            let pal: [[i32; 3]; 4] = if c0 > c1 {
                [a, e, [(2 * a[0] + e[0]) / 3, (2 * a[1] + e[1]) / 3, (2 * a[2] + e[2]) / 3],
                 [(a[0] + 2 * e[0]) / 3, (a[1] + 2 * e[1]) / 3, (a[2] + 2 * e[2]) / 3]]
            } else {
                [a, e, [(a[0] + e[0]) / 2, (a[1] + e[1]) / 2, (a[2] + e[2]) / 2], [0, 0, 0]]
            };
            let idx = u32::from_le_bytes([b[4], b[5], b[6], b[7]]);
            for t in 0..16 {
                let (x, y) = (bx * 4 + t % 4, by * 4 + t / 4);
                if x < w && y < h {
                    let p = pal[(idx >> (2 * t)) as usize & 3];
                    let o = (y * w + x) * 3;
                    out[o] = p[0] as u8;
                    out[o + 1] = p[1] as u8;
                    out[o + 2] = p[2] as u8;
                }
            }
        }
    }
    Some((w, h, out))
}

/// A far tile: its texture file and its footprint (x0, y0, size m in the block's frame).
type Tile = (String, f64, f64, f64);

/// A tile ready to paint: the output rows / columns it may cover, its footprint, the decoded mip and where it lies.
struct Ready {
    r0: usize,
    r1: usize,
    c0: usize,
    c1: usize,
    fx0: f64,
    fy0: f64,
    fs: f64,
    sx0: f64,
    sy1: f64,
    tk: f64,
    w: usize,
    h: usize,
    tex: Vec<u8>,
}

/// A tile's texture read and the mip it is drawn from decoded, or None (nothing of the square, gone, another format).
fn prepare(tile: &Tile, x0: f64, y0: f64, size: f64, px: usize) -> Option<Ready> {
    let (path, fx0, fy0, fs) = (&tile.0, tile.1, tile.2, tile.3);
    if !(fs > 0.0) {
        return None;
    }
    let t = size / px as f64;
    // the output columns / rows whose texel centres may lie in the footprint [fx0, fx0 + fs) x [fy0, fy0 + fs) (each
    // checked again where painted)
    let c0 = ((fx0 - x0) / t - 0.5).ceil().max(0.0) as usize;
    let c1 = ((fx0 + fs - x0) / t - 0.5).ceil().min(px as f64).max(0.0) as usize;
    let r0 = (((y0 + size - fy0 - fs) / t - 0.5).floor() + 1.0).max(0.0) as usize;
    let r1 = (((y0 + size - fy0) / t - 0.5).floor() + 1.0).min(px as f64).max(0.0) as usize;
    if c0 >= c1 || r0 >= r1 {
        return None;
    }
    let bytes = std::fs::read(path).ok()?;      // (evicted meanwhile: the others)
    let d = parse(&bytes)?;
    if d.w <= 2 * gutter(d.w) {
        return None;
    }
    let g = gutter(d.w) as f64;
    let ts = fs / (d.w as f64 - 2.0 * g);                       // its mip 0 texel (m)
    let mut k = 0;
    while k + 1 < d.levels && ts * (1u64 << (k + 1)) as f64 <= t * 1.0001 {
        k += 1;
    }
    let (w, h, tex) = decode_level(&d, k)?;
    Some(Ready { r0, r1, c0, c1, fx0, fy0, fs, sx0: fx0 - g * ts, sy1: fy0 + fs + g * ts,
                 tk: d.w as f64 * ts / w as f64, w, h, tex })
}

/// Output row r of the tiles in order (later over earlier).
fn paint_row(ready: &[Ready], r: usize, x0: f64, y0: f64, size: f64, px: usize, row: &mut [u8], cov: &mut [bool]) {
    let t = size / px as f64;
    let wy = y0 + size - (r as f64 + 0.5) * t;
    for q in ready {
        if r < q.r0 || r >= q.r1 || wy < q.fy0 || wy >= q.fy0 + q.fs {
            continue;
        }
        let (w, h) = (q.w, q.h);
        let v = ((q.sy1 - wy) / q.tk - 0.5).clamp(0.0, (h - 1) as f64);
        let (j0, fv) = (v.floor() as usize, v - v.floor());
        let j1 = (j0 + 1).min(h - 1);
        for c in q.c0..q.c1 {
            let wx = x0 + (c as f64 + 0.5) * t;
            if wx < q.fx0 || wx >= q.fx0 + q.fs {
                continue;
            }
            let u = ((wx - q.sx0) / q.tk - 0.5).clamp(0.0, (w - 1) as f64);
            let (i0, fu) = (u.floor() as usize, u - u.floor());
            let i1 = (i0 + 1).min(w - 1);
            for ch in 0..3 {
                let at = |i: usize, j: usize| q.tex[(j * w + i) * 3 + ch] as f64;
                let top = at(i0, j0) * (1.0 - fu) + at(i1, j0) * fu;
                let bot = at(i0, j1) * (1.0 - fu) + at(i1, j1) * fu;
                row[c * 3 + ch] = (top * (1.0 - fv) + bot * fv).round().clamp(0.0, 255.0) as u8;
            }
            cov[c] = true;
        }
    }
}

const THREADS: usize = 4;

/// The tiles drawn into rgb (px * px * 3) and covered: each tile read and decoded on one of THREADS threads, the rows
/// painted in bands on them (each texel the same whatever the split).
fn paint(tiles: &[Tile], x0: f64, y0: f64, size: f64, px: usize, rgb: &mut [u8], covered: &mut [bool]) {
    let n = THREADS.min(std::thread::available_parallelism().map_or(1, |v| v.get())).max(1);
    let mut ready: Vec<Option<Ready>> = Vec::new();
    std::thread::scope(|sc| {
        let per = tiles.len().div_ceil(n).max(1);
        let parts: Vec<_> = tiles.chunks(per)
            .map(|ch| sc.spawn(move || ch.iter().map(|t| prepare(t, x0, y0, size, px)).collect::<Vec<_>>()))
            .collect();
        for p in parts {
            ready.extend(p.join().unwrap_or_default());
        }
    });
    let ready: Vec<Ready> = ready.into_iter().flatten().collect();
    if ready.is_empty() {
        return;
    }
    let band = px.div_ceil(n).max(1);
    std::thread::scope(|sc| {
        for (b, (rows, cov)) in rgb.chunks_mut(band * px * 3).zip(covered.chunks_mut(band * px)).enumerate() {
            let ready = &ready;
            sc.spawn(move || {
                for (k, (row, cv)) in rows.chunks_mut(px * 3).zip(cov.chunks_mut(px)).enumerate() {
                    paint_row(ready, b * band + k, x0, y0, size, px, row, cv);
                }
            });
        }
    });
}

/// Holes filled from the covered texels round them: coverage-weighted 2 x 2 sums up to one texel, each level's means,
/// then each hole takes its parent's mean (nearest), level by level back down to the picture.
fn push_pull(rgb: &mut [u8], covered: &[bool], px: usize) {
    let mut pyr: Vec<(usize, Vec<[f32; 4]>)> = Vec::new();
    let m = px.div_ceil(2);
    let mut first = vec![[0f32; 4]; m * m];
    for y in 0..px {
        for x in 0..px {
            if covered[y * px + x] {
                let (o, d) = ((y * px + x) * 3, &mut first[(y / 2) * m + x / 2]);
                d[0] += rgb[o] as f32;
                d[1] += rgb[o + 1] as f32;
                d[2] += rgb[o + 2] as f32;
                d[3] += 1.0;
            }
        }
    }
    pyr.push((m, first));
    while pyr.last().map_or(false, |l| l.0 > 1) {
        let (n, cur) = pyr.last().unwrap();
        let (n, m) = (*n, n.div_ceil(2));
        let mut next = vec![[0f32; 4]; m * m];
        for y in 0..n {
            for x in 0..n {
                let (s, d) = (cur[y * n + x], &mut next[(y / 2) * m + x / 2]);
                for k in 0..4 {
                    d[k] += s[k];
                }
            }
        }
        pyr.push((m, next));
    }
    for (_, lv) in pyr.iter_mut() {
        for e in lv.iter_mut() {
            if e[3] > 0.0 {
                *e = [e[0] / e[3], e[1] / e[3], e[2] / e[3], 1.0];
            }
        }
    }
    for l in (0..pyr.len() - 1).rev() {
        let (lo, hi) = pyr.split_at_mut(l + 1);
        let ((n, lv), (pn, parent)) = (&mut lo[l], &hi[0]);
        for y in 0..*n {
            for x in 0..*n {
                let e = &mut lv[y * *n + x];
                if e[3] <= 0.0 {
                    *e = parent[(y / 2) * *pn + x / 2];
                }
            }
        }
    }
    let (n1, l1) = &pyr[0];
    for y in 0..px {
        for x in 0..px {
            let e = l1[(y / 2) * *n1 + x / 2];
            if !covered[y * px + x] && e[3] > 0.0 {
                let o = (y * px + x) * 3;
                for ch in 0..3 {
                    rgb[o + ch] = e[ch].round().clamp(0.0, 255.0) as u8;
                }
            }
        }
    }
}

/// far_standin(tiles, x0, y0, size, px, under=None) -> (rgb uint8 (px, px, 3), texels the tiles covered): the square
/// [x0, x0 + size]² of the block's frame at px² (row 0 north; texel centres at (i + 0.5) * size / px), drawn from
/// tiles [(DDS path, footprint x0, y0, size)], later ones over earlier ones (coarsest first). Texels none covers
/// are under's (an (px, px, 3) picture), or without it filled from round them (none covered: all zero).
#[pyfunction]
#[pyo3(signature=(tiles, x0, y0, size, px, under=None))]
pub fn far_standin<'py>(
    py: Python<'py>,
    tiles: Vec<Tile>,
    x0: f64,
    y0: f64,
    size: f64,
    px: usize,
    under: Option<PyReadonlyArray3<'py, u8>>,
) -> PyResult<(Bound<'py, PyArray3<u8>>, usize)> {
    if px == 0 || px > 1 << 14 || !(size > 0.0) {
        return Err(PyValueError::new_err("far_standin: invalid square"));
    }
    if under.as_ref().is_some_and(|a| a.shape() != [px, px, 3]) {
        return Err(PyValueError::new_err("far_standin: under must be (px, px, 3)"));
    }
    let base: Option<Vec<u8>> = under.as_ref().map(|a| a.as_array().iter().copied().collect());
    let fill_holes = base.is_none();
    let (rgb, n) = py.detach(move || {
        let mut rgb = base.unwrap_or_else(|| vec![0u8; px * px * 3]);
        let mut covered = vec![false; px * px];
        paint(&tiles, x0, y0, size, px, &mut rgb, &mut covered);
        let n = covered.iter().filter(|&&c| c).count();
        if fill_holes && n > 0 && n < px * px {
            push_pull(&mut rgb, &covered, px);
        }
        (rgb, n)
    });
    let arr = Array3::from_shape_vec((px, px, 3), rgb).map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok((arr.into_pyarray(py), n))
}
