//! Far textures from the finished imagery pyramid (geotiles phase 3a, .claude/work/geotiles_plan.md): a texture of a
//! far tile (or a block's ground photo) is a warp of the finished Web-Mercator source tiles (imagery.Imagery.finished:
//! de-shadowed, colour-matched and tone-mapped once per source tile, kept on disk) into the tile's footprint in the
//! anchor's plane; only this warp (and the DXT encode) is per anchor.
//!
//! - warp_rgba: Pillow's Image.transform(MESH, BILINEAR) of an RGBA mosaic, byte for byte (lib.rs mesh_warp).
//! - tile_warp: one source's finished tiles placed in their mosaic, warped, and composited into the texture where it
//!   is not filled yet (the chain's first source with data wins per texel, imagery._render's rule).
//! - season_img: season.grade of a texture (the canopy, the world-anchored noise, the half-size path), Pillow's
//!   resampling where season.py used it.
//! - dds_levels: the texture's mip chain and its BC1 blocks (imagery.encode_dds) in one call.

use numpy::ndarray::{s, Array2, Array3, ArrayView2, ArrayView3};
use numpy::{IntoPyArray, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3};
use pyo3::types::PyBytes;
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

use crate::finished_tile::{CachedTile, Pixels};
use crate::{season_grade_core, season_noise_at, sm_f32};

pub const TILE: usize = 256;

/// Pillow's Image.transform(MESH, BILINEAR) of an RGBA (or RGB) image, exactly: see lib.rs mesh_warp. threads: the
/// mesh rows on up to this many threads (the same numbers; a block's 1024 px ground photo waits for its warp, ~50 ms
/// on a Steam Deck on one thread; the far textures are warped one per far thread, so one each).
pub fn warp_rgba(a: ArrayView3<u8>, qx: ArrayView2<f64>, qy: ArrayView2<f64>, cells: usize, cw: usize, px: usize,
                 threads: usize) -> Array3<u8> {
    let (ih, iw, bands) = (a.shape()[0] as i64, a.shape()[1] as i64, a.shape()[2]);
    let src_own = a.as_standard_layout();
    let src = src_own.as_slice().expect("standard layout");
    let rowlen = iw as usize * bands;
    let mut out = numpy::ndarray::Array3::<u8>::zeros((px, px, bands));
    let band_len = (cw * px * bands).max(1);
    let nthreads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1).clamp(1, threads.max(1));
    let flat = out.as_slice_mut().expect("a new array is contiguous");
    let mut bands_v: Vec<(usize, &mut [u8])> = flat.chunks_mut(band_len).enumerate().filter(|(r, _)| *r < cells).collect();
    let mut groups: Vec<Vec<(usize, &mut [u8])>> = (0..nthreads).map(|_| Vec::new()).collect();
    for (k, item) in bands_v.drain(..).enumerate() {
        groups[k % nthreads].push(item);
    }
    let clipx = |x: i64| -> usize { (if x < 0 { 0 } else if x < iw { x } else { iw - 1 }) as usize };
    let clipy = |y: i64| -> usize { (if y < 0 { 0 } else if y < ih { y } else { ih - 1 }) as usize };
    // the premultiplied source value (Pillow's rgbA2rgba: MULDIV255 for colour, alpha as is)
    let pm = |o: usize, b: usize| -> f64 {
        let v = src[o + b] as u32;
        if b == 3 || bands < 4 {
            return v as f64;
        }
        let t = v * src[o + 3] as u32 + 128;
        (((t >> 8) + t) >> 8) as f64
    };
    let warp_band = |r: usize, band: &mut [u8]| {
        for c in 0..cells {
            let (bx0, by0) = (c * cw, r * cw);
            let (bx1, by1) = (((c + 1) * cw).min(px), ((r + 1) * cw).min(px));
            let (w, h) = ((c + 1) * cw - c * cw, (r + 1) * cw - r * cw);
            let (nwx, nwy) = (qx[[r, c]], qy[[r, c]]);
            let (swx, swy) = (qx[[r + 1, c]], qy[[r + 1, c]]);
            let (sex, sey) = (qx[[r + 1, c + 1]], qy[[r + 1, c + 1]]);
            let (nex, ney) = (qx[[r, c + 1]], qy[[r, c + 1]]);
            let as_ = 1.0 / w as f64;
            let at = 1.0 / h as f64;
            let a0 = nwx;
            let a1 = (nex - nwx) * as_;
            let a2 = (swx - nwx) * at;
            let a3 = (sex - swx - nex + nwx) * as_ * at;
            let a4 = nwy;
            let a5 = (ney - nwy) * as_;
            let a6 = (swy - nwy) * at;
            let a7 = (sey - swy - ney + nwy) * as_ * at;
            for y in by0..by1 {
                for x in bx0..bx1 {
                    let xin = (x - bx0) as f64 + 0.5;
                    let yin = (y - by0) as f64 + 0.5;
                    let mut xx = a0 + a1 * xin + a2 * yin + a3 * xin * yin;
                    let mut yy = a4 + a5 * xin + a6 * yin + a7 * xin * yin;
                    if xx < 0.0 || xx >= iw as f64 || yy < 0.0 || yy >= ih as f64 {
                        continue;                       // fill: 0
                    }
                    xx -= 0.5;
                    yy -= 0.5;
                    let xi = if xx < 0.0 { xx.floor() as i64 } else { xx as i64 };
                    let yi = if yy < 0.0 { yy.floor() as i64 } else { yy as i64 };
                    let dx = xx - xi as f64;
                    let dy = yy - yi as f64;
                    let (x0, x1) = (clipx(xi), clipx(xi + 1));
                    let row0 = clipy(yi);
                    let has1 = yi + 1 >= 0 && yi + 1 < ih;
                    let o00 = row0 * rowlen + x0 * bands;
                    let o01 = row0 * rowlen + x1 * bands;
                    let (o10, o11) = if has1 {
                        let r1 = (yi + 1) as usize * rowlen;
                        (r1 + x0 * bands, r1 + x1 * bands)
                    } else {
                        (o00, o01)
                    };
                    let mut px_ = [0u8; 4];
                    let d = ((y - by0) * px + x) * bands;
                    // every tap opaque (the common case): premultiplying is the identity (MULDIV255(v, 255) = v) and
                    // the result's alpha 255, so the colour as it is: the same numbers, without the per-tap work
                    if bands == 4 {
                        let (t00, t01) = (&src[o00..o00 + 4], &src[o01..o01 + 4]);
                        let (t10, t11) = (&src[o10..o10 + 4], &src[o11..o11 + 4]);
                        if t00[3] == 255 && t01[3] == 255 && t10[3] == 255 && t11[3] == 255 {
                            let dst = &mut band[d..d + 4];
                            for b in 0..3 {
                                let (p0, p1) = (t00[b] as f64, t01[b] as f64);
                                let v1 = p0 + (p1 - p0) * dx;
                                let v2 = if has1 {
                                    let (q0, q1) = (t10[b] as f64, t11[b] as f64);
                                    q0 + (q1 - q0) * dx
                                } else {
                                    v1
                                };
                                dst[b] = (v1 + (v2 - v1) * dy) as u8;
                            }
                            dst[3] = 255;
                            continue;
                        }
                    }
                    for b in 0..bands {
                        let p0 = pm(o00, b);
                        let p1 = pm(o01, b);
                        let v1 = p0 + (p1 - p0) * dx;
                        let v2 = if has1 {
                            let q0 = pm(o10, b);
                            let q1 = pm(o11, b);
                            q0 + (q1 - q0) * dx
                        } else {
                            v1
                        };
                        px_[b] = (v1 + (v2 - v1) * dy) as u8;
                    }
                    // back from premultiplied (Pillow's rgba2rgbA)
                    if bands == 4 {
                        let al = px_[3] as i32;
                        if al != 255 && al != 0 {
                            for b in 0..3 {
                                px_[b] = ((255 * px_[b] as i32) / al).clamp(0, 255) as u8;
                            }
                        }
                    }
                    band[d..d + bands].copy_from_slice(&px_[..bands]);
                }
            }
        }
    };
    if nthreads == 1 {
        for g in groups {
            for (r, band) in g {
                warp_band(r, band);
            }
        }
    } else {
        std::thread::scope(|sc| {
            for g in groups {
                let wb = &warp_band;
                sc.spawn(move || {
                    for (r, band) in g {
                        wb(r, band);
                    }
                });
            }
        });
    }
    out
}

/// The photo tiles a far texture's footprint needs (imagery._core_tiles): (tx0, tx1, ty0, ty1), the tile range the
/// bilinear taps of its texels g .. px - g - 1 (both axes) touch, placed exactly as warp_rgba places them (a cell's
/// corners qx, qy, mosaic or global pixel coordinates, interpolated at the texel centre; taps at floor(p - 0.5) and
/// the next pixel). Its gutter's texels are left out: they take what is on hand (imagery.Imagery._render).
#[pyfunction]
pub fn core_tiles(qx: PyReadonlyArray2<'_, f64>, qy: PyReadonlyArray2<'_, f64>, cells: usize, cw: usize, g: usize,
                  px: usize) -> PyResult<(i64, i64, i64, i64)> {
    footprint_tiles(qx.as_array(), qy.as_array(), cells, cw, g, px)
}

pub(crate) fn footprint_tiles(qx: ArrayView2<f64>, qy: ArrayView2<f64>, cells: usize, cw: usize, g: usize,
                              px: usize) -> PyResult<(i64, i64, i64, i64)> {
    if qx.shape() != [cells + 1, cells + 1] || qy.shape() != [cells + 1, cells + 1] || cells * cw != px
        || cells == 0 || 2 * g >= px {
        return Err(PyValueError::new_err("core_tiles: the mesh is not (cells + 1)^2, cells * cw != px or no footprint"));
    }
    let (mut x0, mut x1, mut y0, mut y1) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
    let mut at = |y: usize, x: usize| {
        let (r, c) = ((y / cw).min(cells - 1), (x / cw).min(cells - 1));
        let (bx0, by0) = (c * cw, r * cw);
        let (as_, at_) = (1.0 / cw as f64, 1.0 / cw as f64);
        let (nwx, nwy) = (qx[[r, c]], qy[[r, c]]);
        let (swx, swy) = (qx[[r + 1, c]], qy[[r + 1, c]]);
        let (sex, sey) = (qx[[r + 1, c + 1]], qy[[r + 1, c + 1]]);
        let (nex, ney) = (qx[[r, c + 1]], qy[[r, c + 1]]);
        let xin = (x - bx0) as f64 + 0.5;
        let yin = (y - by0) as f64 + 0.5;
        // (warp_rgba's own arithmetic, term for term)
        let xx = nwx + (nex - nwx) * as_ * xin + (swx - nwx) * at_ * yin + (sex - swx - nex + nwx) * as_ * at_ * xin * yin;
        let yy = nwy + (ney - nwy) * as_ * xin + (swy - nwy) * at_ * yin + (sey - swy - ney + nwy) * as_ * at_ * xin * yin;
        x0 = x0.min(xx);
        x1 = x1.max(xx);
        y0 = y0.min(yy);
        y1 = y1.max(yy);
    };
    let (lo, hi) = (g, px - g - 1);
    for i in lo..=hi {
        at(lo, i);
        at(hi, i);
        at(i, lo);
        at(i, hi);
    }
    const EPS: f64 = 1e-6;
    let t = TILE as i64;
    let tile = |p: f64| (p.floor() as i64).div_euclid(t);
    Ok((tile(x0 - 0.5 - EPS), tile((x1 - 0.5 + EPS).floor() + 1.0), tile(y0 - 0.5 - EPS),
        tile((y1 - 0.5 + EPS).floor() + 1.0)))
}

/// A finished tile as tile_warp takes it: its RGB (256, 256, 3) decoded, or its JPEG (decoded here, on the warp's
/// threads: imagery.Imagery keeps the finished tiles as their JPEGs, the disk's bytes).
#[pyclass(frozen)]
pub struct DecodedTile(Array3<u8>);

#[derive(FromPyObject)]
pub enum TileSrc<'py> {
    Decoded(Bound<'py, DecodedTile>),
    Cached(Bound<'py, CachedTile>),
    Rgb(PyReadonlyArray3<'py, u8>),
    Jpeg(Bound<'py, PyBytes>),
}

#[derive(FromPyObject)]
pub enum TileMask<'py> {
    Cached(Bound<'py, CachedTile>),
    Array(PyReadonlyArray2<'py, bool>),
}

enum Src<'a> {
    Rgb(ArrayView3<'a, u8>),
    Jpeg(&'a [u8]),
}

/// A JPEG's RGB as zune-jpeg decodes it, (256, 256) checked.
pub fn decode_jpeg(data: &[u8]) -> Result<Vec<u8>, String> {
    use zune_jpeg::zune_core::{bytestream::ZCursor, colorspace::ColorSpace, options::DecoderOptions};
    let opts = DecoderOptions::default().jpeg_set_out_colorspace(ColorSpace::RGB);
    let mut d = zune_jpeg::JpegDecoder::new_with_options(ZCursor::new(data), opts);
    let px = d.decode().map_err(|e| format!("{e:?}"))?;
    match d.dimensions() {
        Some((w, h)) if w == TILE && h == TILE && px.len() == TILE * TILE * 3 => Ok(px),
        other => Err(format!("a finished tile of {other:?}, not {TILE} x {TILE} RGB")),
    }
}

/// The RGB of a finished tile's JPEG (imagery: a texture is the same whichever way its tiles came).
#[pyfunction]
pub fn jpeg_rgb<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, numpy::PyArray3<u8>>> {
    let px = py.detach(|| decode_jpeg(data)).map_err(PyValueError::new_err)?;
    Ok(Array3::from_shape_vec((TILE, TILE, 3), px).expect("checked").into_pyarray(py))
}

/// One texture while Imagery resolves its chain, with its RGB and coverage owned by Rust.
#[pyclass]
pub struct PhotoCanvas {
    image: Array3<u8>,
    filled: Array2<u8>,
    gutter: usize,
    remaining: usize,
}

#[pymethods]
impl PhotoCanvas {
    #[new]
    fn new(px: usize, gutter: usize) -> PyResult<Self> {
        if px == 0 || gutter > (px - 1) / 2 || px.checked_mul(px).and_then(|n| n.checked_mul(3)).is_none() {
            return Err(PyValueError::new_err("PhotoCanvas: empty footprint"));
        }
        Ok(Self { image: Array3::zeros((px, px, 3)), filled: Array2::zeros((px, px)), gutter, remaining: px * px })
    }

    /// Source tile coordinates under mesh cells with remaining holes, including the bilinear taps.
    fn unfilled_tiles(&self, py: Python<'_>, cells: usize, cw: usize, mesh: &crate::photomesh::SourceMesh) -> PyResult<std::collections::BTreeSet<(i64, i64)>> {
        let (fx, fy) = (mesh.x.view(), mesh.y.view());
        let px = self.filled.nrows();
        if cells == 0 || px == 0 || cw != px / cells || fx.dim() != (cells + 1, cells + 1) || fx.dim() != fy.dim() {
            return Err(PyValueError::new_err("unfilled_tiles: invalid mesh"));
        }
        let tile = |p: f64| (p.floor() as i64).div_euclid(TILE as i64);
        Ok(py.detach(|| {
            let mut keys = std::collections::BTreeSet::new();
            for r in 0..cells {
                for c in 0..cells {
                    let end = |i| if i + 1 == cells { px } else { (i + 1) * cw };
                    if self.filled.slice(s![r*cw..end(r), c*cw..end(c)]).iter().all(|&v| v != 0) { continue; }
                    let range = |a: ArrayView2<f64>| {
                        let quad = a.slice(s![r..=r+1, c..=c+1]);
                        let lo = quad.iter().copied().fold(f64::INFINITY, f64::min);
                        let hi = quad.iter().copied().fold(f64::NEG_INFINITY, f64::max);
                        tile(lo - 1.0)..=tile(hi + 1.0)
                    };
                    let xs = range(fx);
                    for ty in range(fy) {
                        keys.extend(xs.clone().map(|tx| (tx, ty)));
                    }
                }
            }
            keys
        }))
    }

    /// Unfilled footprint texels at the poles, so a custom projection may supply their cap mask.
    fn holes<'py>(&self, py: Python<'py>) -> PyResult<(Bound<'py, numpy::PyArray1<usize>>, Bound<'py, numpy::PyArray1<usize>>)> {
        if self.filled.is_empty() { return Err(PyValueError::new_err("photo canvas already finished")); }
        let g = self.gutter;
        let hi = self.filled.nrows() - g;
        let (ys, xs): (Vec<_>, Vec<_>) = py.detach(|| self.filled.slice(s![g..hi,g..hi]).indexed_iter()
            .filter_map(|((r,c), &v)| (v == 0).then_some((r + g, c + g))).unzip());
        Ok((ys.into_pyarray(py), xs.into_pyarray(py)))
    }

    /// Fill known empty texels first, then extend the nearest footprint edge into any unresolved gutter.
    fn fill<'py>(&mut self, py: Python<'py>, fallback: [u8; 3], polar: [u8; 3], cap: Option<PyReadonlyArray1<'py, bool>>)
                  -> PyResult<(usize, usize)> {
        let cap = cap.as_ref().map(|a| a.as_array());
        let (mut none, mut snow) = (0, 0);
        let px = self.filled.nrows();
        if px == 0 { return Err(PyValueError::new_err("photo canvas already finished")); }
        let (g, hi) = (self.gutter, px - self.gutter);
        py.detach(|| {
            if self.remaining == 0 { return; }
            for ((r, c), &f) in self.filled.slice(s![g..hi, g..hi]).indexed_iter() {
                if f != 0 { continue; }
                let k = none + snow;
                let color = if cap.as_ref().is_some_and(|a| a.get(k) == Some(&true)) {
                    snow += 1;
                    polar
                } else {
                    none += 1;
                    fallback
                };
                for b in 0..3 { self.image[[r+g, c+g, b]] = color[b]; }
            }
            if g > 0 {
                for ((r, c), &v) in self.filled.indexed_iter() {
                    if v == 0 && (r < g || r >= hi || c < g || c >= hi) {
                        for b in 0..3 { self.image[[r,c,b]] = self.image[[r.clamp(g,hi-1), c.clamp(g,hi-1), b]]; }
                    }
                }
            }
        });
        if cap.is_some_and(|a| a.len() != none + snow) { return Err(PyValueError::new_err("invalid polar mask")); }
        self.filled = Array2::default((0, 0));
        Ok((none, snow))
    }

    /// Both composites stay native while the lazy layer takes its reference's low-frequency colour.
    fn correct_lazy(&mut self, py: Python<'_>, reference: &PhotoCanvas, sigma: f32, leaf_on: bool) -> PyResult<()> {
        if !self.filled.is_empty() || !reference.filled.is_empty() || self.image.is_empty() || reference.image.is_empty() {
            return Err(PyValueError::new_err("correct_lazy: expected finished canvases"));
        }
        py.detach(|| {
            let matched = matched(self.image.view(), reference.image.view(), sigma);
            self.image = if leaf_on {
                crate::leafon::correct(matched.view(), reference.image.view()).unwrap_or(matched)
            } else { matched };
        });
        Ok(())
    }

    fn take_rgb<'py>(&mut self, py: Python<'py>) -> PyResult<Bound<'py, numpy::PyArray3<u8>>> {
        if !self.filled.is_empty() || self.image.is_empty() {
            return Err(PyValueError::new_err("expected a finished photo canvas"));
        }
        Ok(std::mem::take(&mut self.image).into_pyarray(py))
    }

    #[allow(clippy::too_many_arguments, clippy::type_complexity)]
    fn warp<'py>(&mut self, py: Python<'py>, tiles: Vec<(usize, usize, TileSrc<'py>, u8, Option<TileMask<'py>>)>,
                 nx: usize, ny: usize, qx: PyReadonlyArray2<'py, f64>, qy: PyReadonlyArray2<'py, f64>,
                 cells: usize, cw: usize) -> PyResult<(u64, u64, bool, Vec<(usize, DecodedTile)>)> {
        self.warp_views(py, tiles, nx, ny, qx.as_array(), qy.as_array(), cells, cw)
    }

    fn warp_mesh<'py>(&mut self, py: Python<'py>, tiles: Vec<(usize, usize, TileSrc<'py>, u8, Option<TileMask<'py>>)>,
                      mesh: &crate::photomesh::SourceMesh, cells: usize, cw: usize)
                      -> PyResult<(u64, u64, bool, Vec<(usize, DecodedTile)>)> {
        self.warp_views(py, tiles, mesh.tiles.2, mesh.tiles.3, mesh.qx.view(), mesh.qy.view(), cells, cw)
    }
}

impl PhotoCanvas {
    fn warp_views<'py>(&mut self, py: Python<'py>, tiles: Vec<(usize, usize, TileSrc<'py>, u8, Option<TileMask<'py>>)>,
                       nx: usize, ny: usize, qx: ArrayView2<f64>, qy: ArrayView2<f64>, cells: usize, cw: usize)
                       -> PyResult<(u64, u64, bool, Vec<(usize, DecodedTile)>)> {
        let views: Vec<(usize, usize, Src, u8, Option<ArrayView2<bool>>)> = tiles.iter()
            .map(|(c, r, t, al, h)| (*c, *r, match t {
                TileSrc::Decoded(t) => Src::Rgb(t.get().0.view()),
                TileSrc::Rgb(a) => Src::Rgb(a.as_array()),
                TileSrc::Jpeg(b) => Src::Jpeg(b.as_bytes()),
                TileSrc::Cached(t) => match &t.get().pixels {
                    Pixels::Rgb(a) => Src::Rgb(a.view()), Pixels::Jpeg(b) => Src::Jpeg(b),
                }
            }, *al, h.as_ref().and_then(|m| match m {
                TileMask::Array(a) => Some(a.as_array()), TileMask::Cached(t) => t.get().hole.as_ref().map(|h| h.view()),
            }))).collect();
        for (c, r, t, _, h) in &views {
            let bad_rgb = matches!(t, Src::Rgb(a) if a.shape() != [TILE, TILE, 3]);
            if bad_rgb || *c >= nx || *r >= ny || h.as_ref().is_some_and(|m| m.shape() != [TILE, TILE]) {
                return Err(PyValueError::new_err(format!("tile_warp: tile ({c}, {r}) of a {nx} x {ny} mosaic")));
            }
        }
        if qx.shape() != [cells + 1, cells + 1] || qy.shape() != [cells + 1, cells + 1] {
            return Err(PyValueError::new_err("tile_warp: the mesh is not (cells + 1)^2"));
        }
        let mut o = self.image.view_mut();
        let mut f = self.filled.view_mut();
        let px = o.shape()[0];
        if px == 0 || f.nrows() != px || cells.checked_mul(cw) != Some(px) {
            return Err(PyValueError::new_err("tile_warp: out (px, px, 3), filled (px, px), cells * cw == px"));
        }
        let got = py.detach(|| {
            // the JPEGs decoded on up to 4 threads
            let jpegs: Vec<(usize, &[u8])> = views.iter().enumerate()
                .filter_map(|(i, v)| if let Src::Jpeg(b) = v.2 { Some((i, b)) } else { None }).collect();
            let mut decoded: Vec<Option<Result<Vec<u8>, String>>> = (0..views.len()).map(|_| None).collect();
            let nthreads = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(1).clamp(1, 4)
                .min(jpegs.len().max(1));
            if nthreads <= 1 {
                for (i, b) in &jpegs {
                    decoded[*i] = Some(decode_jpeg(b));
                }
            } else {
                let chunk = jpegs.len().div_ceil(nthreads);
                let res: Vec<Vec<(usize, Result<Vec<u8>, String>)>> = std::thread::scope(|sc| {
                    let hs: Vec<_> = jpegs.chunks(chunk).map(|part| sc.spawn(move || {
                        part.iter().map(|(i, b)| (*i, decode_jpeg(b))).collect::<Vec<_>>()
                    })).collect();
                    hs.into_iter().map(|h| h.join().expect("a JPEG decode thread panicked")).collect()
                });
                for (i, r) in res.into_iter().flatten() {
                    decoded[i] = Some(r);
                }
            }
            let (mh, mw) = (ny * TILE, nx * TILE);
            let mut mos = Array3::<u8>::zeros((mh, mw, 4));
            {
                let ms = mos.as_slice_mut().expect("a new array is contiguous");
                for (k, (c, r, t, al, h)) in views.iter().enumerate() {
                    let own;
                    let sl: &[u8] = match t {
                        Src::Rgb(a) => {
                            own = a.as_standard_layout();
                            own.as_slice().expect("standard layout")
                        }
                        Src::Jpeg(_) => match decoded[k].as_ref().expect("decoded above") {
                            Ok(v) => v.as_slice(),
                            Err(e) => return Err(PyValueError::new_err(format!("tile_warp: tile ({c}, {r}): {e}"))),
                        },
                    };
                    let hm = h.as_ref().map(|m| m.as_standard_layout());
                    let hs = hm.as_ref().map(|m| m.as_slice().expect("standard layout"));
                    for y in 0..TILE {
                        let o0 = ((r * TILE + y) * mw + c * TILE) * 4;
                        let row = &sl[y * TILE * 3..(y + 1) * TILE * 3];
                        let dst = &mut ms[o0..o0 + TILE * 4];
                        for (d, s3) in dst.chunks_exact_mut(4).zip(row.chunks_exact(3)) {
                            d[0] = s3[0];
                            d[1] = s3[1];
                            d[2] = s3[2];
                            d[3] = *al;
                        }
                        if let Some(hs) = hs {
                            for (x, &hole) in hs[y * TILE..(y + 1) * TILE].iter().enumerate() {
                                if hole {
                                    dst[x * 4 + 3] = 0;
                                }
                            }
                        }
                    }
                }
            }
            // (a block's 1024 px photo on 4 threads; a far texture on its far thread alone)
            let res = warp_rgba(mos.view(), qx, qy, cells, cw, px, if px >= 1024 { 4 } else { 1 });
            let (mut hits, mut relit, mut full) = (0u64, 0u64, true);
            let rs = res.as_slice().expect("a new array is contiguous");
            let mut put = |k: usize, fv: &mut u8, ov: &mut [u8]| {
                if *fv != 0 {
                    return;
                }
                let al = rs[k * 4 + 3];
                if al >= 254 {
                    ov.copy_from_slice(&rs[k * 4..k * 4 + 3]);
                    *fv = 1;
                    hits += 1;
                    if al == 254 {
                        relit += 1;
                    }
                } else {
                    full = false;
                }
            };
            let coverage = f.as_slice_mut().expect("owned contiguous coverage");
            let rgb = o.as_slice_mut().expect("owned contiguous RGB");
            for (k, (fv, ov)) in coverage.iter_mut().zip(rgb.chunks_exact_mut(3)).enumerate() {
                put(k, fv, ov);
            }
            let decoded: Vec<(usize, Vec<u8>)> = decoded.into_iter().enumerate()
                .filter_map(|(i, d)| d.and_then(|r| r.ok()).map(|v| (i, v))).collect();
            let g = self.gutter;
            if !full && g > 0 {
                full = f.slice(s![g..px-g,g..px-g]).iter().all(|&v| v != 0);
            }
            Ok((hits, relit, full, decoded))
        });
        let (hits, relit, full, decoded) = got?;
        self.remaining -= hits as usize;
        Ok((hits, relit, full, decoded.into_iter().map(|(i, v)| {
            (i, DecodedTile(Array3::from_shape_vec((TILE, TILE, 3), v).expect("checked")))
        }).collect()))
    }
}

// --- the season grade of a texture (season.grade) ---------------------------------------------------------------
// Pillow's resampling (libImaging/Resample.c) where season.py used it: BILINEAR on mode "F" images (the world metres,
// the noise, the colour ratio, upscaled) and BOX on RGB (the half-size copy of a big texture), the same coefficients
// and passes (horizontal over the rows the vertical one reads, then vertical; doubles for "F", 22-bit fixed point
// with rounding for 8-bit), so the grade is season.py's numbers.

const PRECISION_BITS: u32 = 32 - 8 - 2;

fn bilinear_filter(x: f64) -> f64 {
    let x = x.abs();
    if x < 1.0 { 1.0 - x } else { 0.0 }
}

fn box_filter(x: f64) -> f64 {
    if x > -0.5 && x <= 0.5 { 1.0 } else { 0.0 }
}

struct Coeffs {
    bounds: Vec<(usize, usize)>,    // per output index: (first input index, count)
    kk: Vec<f64>,                   // ksize weights per output index
    ksize: usize,
}

/// Resample.c precompute_coeffs (the whole input: box 0 .. in_size).
fn pil_coeffs(in_size: usize, out_size: usize, support: f64, filter: fn(f64) -> f64) -> Coeffs {
    let scale = in_size as f64 / out_size as f64;
    let filterscale = if scale < 1.0 { 1.0 } else { scale };
    let support = support * filterscale;
    let ksize = (support.ceil() as usize) * 2 + 1;
    let mut kk = vec![0f64; out_size * ksize];
    let mut bounds = Vec::with_capacity(out_size);
    for xx in 0..out_size {
        let center = (xx as f64 + 0.5) * scale;
        let mut ww = 0.0f64;
        let ss = 1.0 / filterscale;
        let mut xmin = (center - support + 0.5) as i64;      // (C's int cast: toward zero)
        if xmin < 0 {
            xmin = 0;
        }
        let mut xmax = (center + support + 0.5) as i64;
        if xmax > in_size as i64 {
            xmax = in_size as i64;
        }
        xmax -= xmin;
        let k = &mut kk[xx * ksize..(xx + 1) * ksize];
        for x in 0..xmax.max(0) as usize {
            let w = filter(((x as i64 + xmin) as f64 - center + 0.5) * ss);
            k[x] = w;
            ww += w;
        }
        for x in 0..xmax.max(0) as usize {
            if ww != 0.0 {
                k[x] /= ww;
            }
        }
        bounds.push((xmin as usize, xmax.max(0) as usize));
    }
    Coeffs { bounds, kk, ksize }
}

/// Image.fromarray(a, "F").resize((ow, oh), Image.BILINEAR) as float32 (each output value's sum in Pillow's order).
pub fn resize_f32(a: ArrayView2<f32>, ow: usize, oh: usize) -> Array2<f32> {
    let (ih, iw) = (a.shape()[0], a.shape()[1]);
    let own = a.as_standard_layout();
    let s = own.as_slice().expect("standard layout");
    let ch = pil_coeffs(iw, ow, 1.0, bilinear_filter);
    let cv = pil_coeffs(ih, oh, 1.0, bilinear_filter);
    let y0 = cv.bounds[0].0;
    let y1 = cv.bounds[oh - 1].0 + cv.bounds[oh - 1].1;
    let mut tmp = vec![0f32; (y1 - y0) * ow];
    for y in y0..y1 {
        let row = &s[y * iw..(y + 1) * iw];
        let trow = &mut tmp[(y - y0) * ow..(y - y0 + 1) * ow];
        for (xx, t) in trow.iter_mut().enumerate() {
            let (xmin, n) = ch.bounds[xx];
            let k = &ch.kk[xx * ch.ksize..xx * ch.ksize + n];
            let mut ss = 0.0f64;
            for (j, &w) in k.iter().enumerate() {
                ss += row[xmin + j] as f64 * w;
            }
            *t = ss as f32;
        }
    }
    let mut out = vec![0f32; oh * ow];
    let mut acc = vec![0f64; ow];
    for yy in 0..oh {
        let (ymin, n) = cv.bounds[yy];
        acc.iter_mut().for_each(|v| *v = 0.0);
        for j in 0..n {
            let w = cv.kk[yy * cv.ksize + j];
            let trow = &tmp[(ymin + j - y0) * ow..(ymin + j - y0 + 1) * ow];
            for (v, &t) in acc.iter_mut().zip(trow) {
                *v += t as f64 * w;
            }
        }
        for (o, &v) in out[yy * ow..(yy + 1) * ow].iter_mut().zip(&acc) {
            *o = v as f32;
        }
    }
    Array2::from_shape_vec((oh, ow), out).expect("sized")
}

fn coeffs_8bpc(c: &Coeffs) -> Vec<i32> {
    c.kk.iter().map(|&w| if w < 0.0 { (-0.5 + w * (1u32 << PRECISION_BITS) as f64) as i32 }
                        else { (0.5 + w * (1u32 << PRECISION_BITS) as f64) as i32 }).collect()
}

fn clip8(v: i32) -> u8 {
    (v >> PRECISION_BITS).clamp(0, 255) as u8
}

/// Image.fromarray(rgb, "RGB").resize((ow, oh), Image.BOX).
pub fn resize_box_rgb(a: ArrayView3<u8>, ow: usize, oh: usize) -> Array3<u8> {
    resize_rgb(a, ow, oh, 0.5, box_filter)
}

pub(crate) fn resize_bilinear_rgb(a: ArrayView3<u8>, ow: usize, oh: usize) -> Array3<u8> {
    resize_rgb(a, ow, oh, 1.0, bilinear_filter)
}

fn resize_rgb(a: ArrayView3<u8>, ow: usize, oh: usize, support: f64, filter: fn(f64) -> f64) -> Array3<u8> {
    let (ih, iw) = (a.shape()[0], a.shape()[1]);
    let own = a.as_standard_layout();
    let src = own.as_slice().expect("standard layout");
    let ch = pil_coeffs(iw, ow, support, filter);
    let cv = pil_coeffs(ih, oh, support, filter);
    let (kh, kv) = (coeffs_8bpc(&ch), coeffs_8bpc(&cv));
    let y0 = cv.bounds[0].0;
    let y1 = cv.bounds[oh - 1].0 + cv.bounds[oh - 1].1;
    let mut tmp = vec![0u8; (y1 - y0) * ow * 3];
    for y in y0..y1 {
        let row = &src[y * iw * 3..(y + 1) * iw * 3];
        let trow = &mut tmp[(y - y0) * ow * 3..(y - y0 + 1) * ow * 3];
        for xx in 0..ow {
            let (xmin, n) = ch.bounds[xx];
            let k = &kh[xx * ch.ksize..xx * ch.ksize + n];
            let mut ss = [1i32 << (PRECISION_BITS - 1); 3];
            for (t, &w) in k.iter().enumerate() {
                let o = (xmin + t) * 3;
                for c in 0..3 {
                    ss[c] += row[o + c] as i32 * w;
                }
            }
            for c in 0..3 {
                trow[xx * 3 + c] = clip8(ss[c]);
            }
        }
    }
    let mut out = vec![0u8; oh * ow * 3];
    for yy in 0..oh {
        let (ymin, n) = cv.bounds[yy];
        let k = &kv[yy * cv.ksize..yy * cv.ksize + n];
        let orow = &mut out[yy * ow * 3..(yy + 1) * ow * 3];
        for (i, o) in orow.iter_mut().enumerate() {
            let mut ss = 1i32 << (PRECISION_BITS - 1);
            for (t, &w) in k.iter().enumerate() {
                ss += tmp[(ymin + t - y0) * ow * 3 + i] as i32 * w;
            }
            *o = clip8(ss);
        }
    }
    Array3::from_shape_vec((oh, ow, 3), out).expect("sized")
}

/// Resize the RGB ground photo before its roads and rock are drawn.
#[pyfunction]
pub fn photo_resize<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, px: usize)
                         -> PyResult<Bound<'py, numpy::PyArray3<u8>>> {
    let a = rgb.as_array();
    if a.is_empty() || a.shape()[0] != a.shape()[1] || a.shape()[2] != 3 || px == 0 {
        return Err(PyValueError::new_err("photo_resize: expected nonempty square RGB and a positive size"));
    }
    Ok(py.detach(|| resize_bilinear_rgb(a, px, px)).into_pyarray(py))
}

/// season._box: the mean over (2r + 1)^2 windows, edges clamped, by numpy's float64 cumulative sums (the same
/// operations in the same order), as float32.
fn box_mean(a: &Array2<f32>, r: usize) -> Array2<f32> {
    let (h, w) = (a.shape()[0], a.shape()[1]);
    let pad = r + 1;
    let (ph, pw) = (h + 2 * pad, w + 2 * pad);
    let mut c = vec![0f64; ph * pw];
    for i in 0..ph {
        let si = (i as i64 - pad as i64).clamp(0, h as i64 - 1) as usize;
        for j in 0..pw {
            let sj = (j as i64 - pad as i64).clamp(0, w as i64 - 1) as usize;
            let v = a[[si, sj]] as f64;
            c[i * pw + j] = if i > 0 { c[(i - 1) * pw + j] + v } else { v };      // cumsum(0)
        }
    }
    for i in 0..ph {
        for j in 1..pw {
            c[i * pw + j] += c[i * pw + j - 1];                                     // cumsum(1)
        }
    }
    let k = 2 * r + 1;
    let kk = (k * k) as f64;
    Array2::from_shape_fn((h, w), |(i, j)| {
        let v = c[(i + k) * pw + j + k] - c[i * pw + j + k] - c[(i + k) * pw + j] + c[i * pw + j];
        (v / kk) as f32
    })
}

/// Constants of season.py the grade reads (passed from it: one place for them).
#[derive(Clone, Copy)]
pub struct SeasonParams {
    pub fall: f64,
    pub winter: f64,
    pub spring: f64,
    pub fall_yellow: f64,
    pub winter_desat: f64,
    pub canopy_v: (f64, f64),
    pub canopy_std: (f64, f64),
    pub patch_m: (f64, f64, f64),
    pub noise_grid: usize,
    pub canopy_texture_max_m: f64,
    pub half_px: usize,
}

/// season.grade_full (its _noise_and_canopy, then season_grade's loop); None: no vegetation texel (unchanged).
fn grade_full(rgb: ArrayView3<u8>, xm: ArrayView2<f32>, ym: ArrayView2<f32>, p: &SeasonParams, pal: ArrayView2<f32>)
              -> Option<Array3<u8>> {
    let (hh, ww) = (rgb.shape()[0], rgb.shape()[1]);
    let v = Array2::from_shape_fn((hh, ww), |(y, x)| {
        rgb[[y, x, 0]].max(rgb[[y, x, 1]]).max(rgb[[y, x, 2]]) as f32 / 255.0f32
    });
    let texel = (1e-3f64).max((xm[[0, ww - 1]] - xm[[0, 0]]).abs() as f64 / (1usize.max(ww - 1)) as f64);
    let canopy = if texel <= p.canopy_texture_max_m {
        let m1 = box_mean(&v, 2);
        let vv = v.mapv(|a| a * a);
        let m2 = box_mean(&vv, 2);
        Array2::from_shape_fn((hh, ww), |(y, x)| {
            let (a, b) = (m1[[y, x]], m2[[y, x]]);
            let sd = (b - a * a).max(0.0f32).sqrt();
            sm_f32(sd, p.canopy_std.0, p.canopy_std.1) * (1.0f32 - sm_f32(a, p.canopy_v.0, p.canopy_v.1))
        })
    } else {
        v.mapv(|a| 1.0f32 - sm_f32(a, p.canopy_v.0, p.canopy_v.1))
    };
    let f = 1usize.max((2.0 / texel) as usize).max((hh.max(ww) + p.noise_grid - 1) / p.noise_grid);
    let (sh, sw) = ((hh + f - 1) / f, (ww + f - 1) / f);
    let (p0, p1, p2) = (p.patch_m.0 as f32, p.patch_m.1 as f32, p.patch_m.2 as f32);
    let n_s = Array2::from_shape_fn((sh, sw), |(i, j)| {
        let (x, y) = (xm[[i * f, j * f]], ym[[i * f, j * f]]);
        0.75f32 * season_noise_at(x / p0, y / p0, 1) + 0.25f32 * season_noise_at(x / p1, y / p1, 2)
    });
    let pick_s = Array2::from_shape_fn((sh, sw), |(i, j)| {
        let (x, y) = (xm[[i * f, j * f]], ym[[i * f, j * f]]);
        season_noise_at(x / p2, y / p2, 3)
    });
    let (n, pick) = if f == 1 { (n_s, pick_s) } else {
        (resize_f32(n_s.view(), ww, hh), resize_f32(pick_s.view(), ww, hh))
    };
    season_grade_core(rgb, n.view(), pick.view(), canopy.view(), p.fall, p.winter, p.spring, pal, p.fall_yellow,
                      p.winter_desat)
}

/// round half to even for a float32 well inside +-2^22 (f32::round_ties_even's value; without SSE4.1 that is a libm
/// call per texel): adding 1.5 * 2^23 leaves no fraction bits, so the default rounding mode does it.
#[inline]
pub(crate) fn rint_small(x: f32) -> f32 {
    const C: f32 = 12_582_912.0;
    if x.abs() < 4_194_304.0 { (x + C) - C } else { x.round_ties_even() }
}

/// The texels' world metres for the grade: given per texel (h, w), or as a grid over the texture upscaled to (h, w)
/// by Pillow's BILINEAR about its minimum; only the rows and columns 0, step, 2 step... (nh x nw) are made (each
/// output of the resize is its own sum: the same values as the full upscale's there).
enum Metres<'a> {
    Full(ArrayView2<'a, f32>),
    Grid(ArrayView2<'a, f32>),
}

impl Metres<'_> {
    fn at(&self, hh: usize, ww: usize, step: usize, nh: usize, nw: usize) -> Array2<f32> {
        match self {
            Metres::Full(a) => a.slice(numpy::ndarray::s![..;step, ..;step]).slice(numpy::ndarray::s![..nh, ..nw])
                .to_owned(),
            Metres::Grid(g) => {
                let m = g.iter().copied().fold(f32::INFINITY, f32::min);
                let mut o = resize_f32_sub(g.mapv(|v| v - m).view(), ww, hh, step, nh, nw);
                o.mapv_inplace(|v| v + m);
                o
            }
        }
    }
}

/// resize_f32's output at rows and columns 0, step, ... (nh x nw) of the (oh, ow) result.
fn resize_f32_sub(a: ArrayView2<f32>, ow: usize, oh: usize, step: usize, nh: usize, nw: usize) -> Array2<f32> {
    let (ih, iw) = (a.shape()[0], a.shape()[1]);
    let own = a.as_standard_layout();
    let s = own.as_slice().expect("standard layout");
    let ch = pil_coeffs(iw, ow, 1.0, bilinear_filter);
    let cv = pil_coeffs(ih, oh, 1.0, bilinear_filter);
    let y0 = cv.bounds[0].0;
    let y1 = cv.bounds[oh - 1].0 + cv.bounds[oh - 1].1;
    let mut tmp = vec![0f32; (y1 - y0) * nw];
    for y in y0..y1 {
        let row = &s[y * iw..(y + 1) * iw];
        for j in 0..nw {
            let xx = j * step;
            let (xmin, n) = ch.bounds[xx];
            let k = &ch.kk[xx * ch.ksize..xx * ch.ksize + n];
            let mut ss = 0.0f64;
            for (t, &w) in k.iter().enumerate() {
                ss += row[xmin + t] as f64 * w;
            }
            tmp[(y - y0) * nw + j] = ss as f32;
        }
    }
    Array2::from_shape_fn((nh, nw), |(i, j)| {
        let yy = i * step;
        let (ymin, n) = cv.bounds[yy];
        let mut ss = 0.0f64;
        for t in 0..n {
            ss += tmp[(ymin + t - y0) * nw + j] as f64 * cv.kk[yy * cv.ksize + t];
        }
        ss as f32
    })
}

/// season.grade: grade_full, or at half_px and bigger on a half-size copy (BOX) applied as a per-channel colour
/// ratio (BILINEAR up). None: unchanged.
fn grade(rgb: ArrayView3<u8>, xm: Metres, ym: Metres, p: &SeasonParams, pal: ArrayView2<f32>) -> Option<Array3<u8>> {
    let (hh, ww) = (rgb.shape()[0], rgb.shape()[1]);
    if hh.min(ww) < p.half_px {
        let (x, y) = (xm.at(hh, ww, 1, hh, ww), ym.at(hh, ww, 1, hh, ww));
        return grade_full(rgb, x.view(), y.view(), p, pal);
    }
    let (sw, sh) = (ww / 2, hh / 2);
    let small = resize_box_rgb(rgb, sw, sh);
    let (xs, ys) = (xm.at(hh, ww, 2, sh, sw), ym.at(hh, ww, 2, sh, sw));
    let g = grade_full(small.view(), xs.view(), ys.view(), p, pal)?;
    let rgb_own = rgb.as_standard_layout();
    let rs = rgb_own.as_slice().expect("standard layout");
    let (gs, ss) = (g.as_slice().expect("a new array"), small.as_slice().expect("a new array"));
    let ups: Vec<Array2<f32>> = (0..3).map(|c| {
        let ratio = Array2::from_shape_fn((sh, sw), |(y, x)| {
            let i = (y * sw + x) * 3 + c;
            (gs[i] as f32 + 2.0f32) / (ss[i] as f32 + 2.0f32)
        });
        resize_f32(ratio.view(), ww, hh)
    }).collect();
    let (u0, u1, u2) = (ups[0].as_slice().expect("new"), ups[1].as_slice().expect("new"),
                        ups[2].as_slice().expect("new"));
    let mut out = vec![0u8; hh * ww * 3];
    for ((((o, r), &a), &b), &c) in out.chunks_exact_mut(3).zip(rs.chunks_exact(3)).zip(u0).zip(u1).zip(u2) {
        o[0] = rint_small(r[0] as f32 * a).clamp(0.0, 255.0) as u8;
        o[1] = rint_small(r[1] as f32 * b).clamp(0.0, 255.0) as u8;
        o[2] = rint_small(r[2] as f32 * c).clamp(0.0, 255.0) as u8;
    }
    Some(Array3::from_shape_vec((hh, ww, 3), out).expect("sized"))
}

type GradeSettings = (f64, f64, f64, f64, f64, (f64, f64), (f64, f64), (f64, f64, f64), usize, f64, usize);

/// season.grade of an RGB texture (h, w, 3) at its texels' world metres xm, ym (h, w); None: unchanged. xm, ym of
/// another shape: a grid over the texture (imagery._seasoned's 17 x 17), upscaled to (h, w) here as it did (Pillow's
/// BILINEAR of the grid less its minimum, the minimum added back in float32). params: (fall, winter, spring,
/// fall_yellow, winter_desat, canopy_v (2), canopy_std (2), patch_m (3), noise_grid, canopy_texture_max_m, half_px).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn season_img<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, xm: PyReadonlyArray2<'py, f32>,
                       ym: PyReadonlyArray2<'py, f32>, pal: PyReadonlyArray2<'py, f32>,
                       params: GradeSettings)
                       -> PyResult<Option<Bound<'py, numpy::PyArray3<u8>>>> {
    let (a, xm, ym, pal) = (rgb.as_array(), xm.as_array(), ym.as_array(), pal.as_array());
    let (hh, ww) = (a.shape()[0], a.shape()[1]);
    if a.shape()[2] != 3 || xm.shape() != ym.shape() || xm.is_empty() || hh < 2 || ww < 2 {
        return Err(PyValueError::new_err("season_img: rgb (h, w, 3), xm and ym of one shape, h and w >= 2"));
    }
    let out = py.detach(|| grade_at(a, xm, ym, pal, params));
    Ok(out.map(|o| o.into_pyarray(py)))
}

fn grade_at(a: ArrayView3<u8>, xm: ArrayView2<f32>, ym: ArrayView2<f32>, pal: ArrayView2<f32>,
            params: GradeSettings) -> Option<Array3<u8>> {
    let (hh, ww, _) = a.dim();
    let p = SeasonParams { fall: params.0, winter: params.1, spring: params.2, fall_yellow: params.3,
                           winter_desat: params.4, canopy_v: params.5, canopy_std: params.6, patch_m: params.7,
                           noise_grid: params.8.max(1), canopy_texture_max_m: params.9, half_px: params.10 };
    if xm.shape() == [hh, ww] {
        grade(a, Metres::Full(xm), Metres::Full(ym), &p, pal)
    } else {
        grade(a, Metres::Grid(xm), Metres::Grid(ym), &p, pal)
    }
}

#[derive(FromPyObject)]
pub enum Rendered<'py> {
    Canvas(PyRef<'py, PhotoCanvas>),
    Rgb(PyReadonlyArray3<'py, u8>),
}

type GradeInput<'py> = (PyReadonlyArray2<'py, f32>, PyReadonlyArray2<'py, f32>, PyReadonlyArray2<'py, f32>, GradeSettings);

/// Shade a source composite. A photo that still needs road paint returns its RGB; otherwise
/// the same detached call delivers the complete DDS, without exporting a pixel buffer to Python.
#[pyfunction]
/// snow (625): the texture's snow (snowlook.rs, winter on), after its rock and road paint.
#[pyo3(signature = (source, season, rock, dds, *, path=None, paint=None, snow=None))]
#[allow(clippy::too_many_arguments)]
pub fn finish_far_photo<'py>(py: Python<'py>, source: Rendered<'py>,
                    season: Option<GradeInput<'py>>, rock: Option<(PyReadonlyArray2<'py, f64>, crate::RockArea)>,
                    dds: Option<(PyReadonlyArray1<'py, f32>, f32, bool)>, path: Option<std::path::PathBuf>,
                    paint: Option<&crate::paintlist::PaintLayers>, snow: Option<PyRef<'py, crate::snowlook::SnowLook>>)
                    -> PyResult<Bound<'py, PyAny>> {
    let img = match &source {
        Rendered::Canvas(c) if c.filled.is_empty() => c.image.view(),
        Rendered::Canvas(_) => return Err(PyValueError::new_err("expected a finished photo canvas")),
        Rendered::Rgb(a) => a.as_array(),
    };
    let dds = dds.as_ref().map(|(lut, sharp, srgb)| (lut.as_array(), *sharp, *srgb));
    let season = season.as_ref().map(|(x,y,p,s)| (x.as_array(), y.as_array(), p.as_array(), *s));
    let rock = rock.as_ref().map(|(u,s)| (u.as_array(), *s));
    let (h,w,c) = img.dim();
    if (path.is_some() && dds.is_none()) || h == 0 || h != w || c != 3 || dds.is_some_and(|(lut, _, _)| lut.len() != 256) || season.as_ref().is_some_and(|(x,y,p,_)|
        h < 2 || x.is_empty() || x.dim() != y.dim() || p.dim() != (3,3)) {
        return Err(PyValueError::new_err("far_dds: invalid square RGB texture, season grid or palette"));
    }
    if let Some(layers) = paint { layers.validate(img.dim())?; }
    enum Finished { Dds(Vec<u8>), Written(usize), Rgb(Array3<u8>) }
    let snow_ref = snow.as_deref();
    let output = py.detach(|| -> std::io::Result<Finished> {
        let graded = season.and_then(|(x,y,p,s)| grade_at(img, x, y, p, s));
        let a = graded.as_ref().map_or_else(|| img.view(), |v| v.view());
        let shaded = rock.and_then(|(u,s)| crate::rock_core(a, u, s));
        let mut owned = shaded.or(graded);
        if let Some(layers) = paint {
            owned = Some(layers.apply(owned.unwrap_or_else(|| img.to_owned())));
        }
        if let Some(sl) = snow_ref {
            let snowy = sl.apply_core(owned.as_ref().map_or_else(|| img.view(), |v| v.view()), None);
            if snowy.is_some() {
                owned = snowy;
            }
        }
        if let Some((lut, sharpen, srgb)) = dds {
            let a = owned.as_ref().map_or_else(|| img.view(), |v| v.view());
            let data = crate::bc1fast::photo_dds(a, lut, sharpen, srgb, None);
            if let Some(path) = path {
                std::fs::write(path, &data)?;
                Ok(Finished::Written(data.len()))
            } else {
                Ok(Finished::Dds(data))
            }
        } else {
            Ok(Finished::Rgb(owned.unwrap_or_else(|| img.to_owned())))
        }
    })?;
    Ok(match output {
        Finished::Written(size) => size.into_pyobject(py)?.into_any(),
        Finished::Dds(data) => PyBytes::new(py, &data).into_any(),
        Finished::Rgb(image) => image.into_pyarray(py).into_any(),
    })
}

/// imagery.encode_dds' payload in one call (one GIL hand-over a texture: each was a wait of up to the interpreter's
/// switch interval behind its busy threads, 2026-10-29): rgb (h, w, 3) and its mip chain (linear light by lut,
/// sharpened) as BC1 blocks, level after level.
/// 287 (on 285): on the CPU by bc1fast.rs (the mips in integers, ISPC's BC1 kernels: a 1024² texture with its mips
/// ~4 ms of CPU against 64 ms, and against a GPU round trip of 24 ms that the game's frames paid for on a Steam Deck).
/// gpu: the GPU's encoder (gpu.rs; float32 mips and maths) only in a build without the ISPC kernels. Returns (the
/// blocks, the level count, the GPU's error if it failed: the CPU encoded them).
#[pyfunction]
pub fn dds_levels<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, lut: PyReadonlyArray1<'py, f32>, sharpen: f32,
                       gpu: bool) -> PyResult<(Bound<'py, PyBytes>, usize, Option<String>)> {
    let (a, lut) = (rgb.as_array(), lut.as_array());
    if a.shape()[2] != 3 || lut.len() != 256 {
        return Err(PyValueError::new_err("dds_levels: rgb (h, w, 3), lut of 256"));
    }
    if !gpu || crate::bc1fast::ispc_linked() {
        let (data, n) = py.detach(|| {
            let blobs = crate::bc1fast::dds_levels(a, lut, sharpen);
            (blobs.concat(), blobs.len())
        });
        return Ok((PyBytes::new(py, &data), n, None));
    }
    let (data, n, err) = py.detach(|| {
        let mut levels = vec![a.as_standard_layout().into_owned()];
        levels.extend(crate::mip_levels_core(a, lut, sharpen));
        let mut err = None;
        let mut blobs: Option<Vec<Vec<u8>>> = None;
        if gpu {
            match crate::gpu::get() {
                Some(g) => {
                    let refs: Vec<(usize, usize, &[u8])> = levels.iter()
                        .map(|l| (l.shape()[1], l.shape()[0], l.as_slice().expect("standard layout"))).collect();
                    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| crate::gpu::bc1_encode(g, &refs)))
                        .unwrap_or_else(|_| Err("the GPU kernel panicked".to_string())) {
                        Ok(b) => blobs = Some(b),
                        Err(e) => err = Some(e),
                    }
                }
                None => err = Some("no GPU".to_string()),
            }
        }
        let blobs = blobs.unwrap_or_else(|| levels.iter().map(|l| crate::bc1_encode_core(l.view())).collect());
        let n = blobs.len();
        (blobs.concat(), n, err)
    });
    Ok((PyBytes::new(py, &data), n, err))
}

/// Match a lazy source's low-frequency colour to the next source in the chain. The RGB Gaussian
/// blur is three independent 8-bit Pillow blurs, and its gain is resized as mode F.
#[pyfunction]
pub fn match_colours<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, reference: PyReadonlyArray3<'py, u8>,
                          sigma: f32) -> PyResult<Bound<'py, numpy::PyArray3<u8>>> {
    let (a, b) = (rgb.as_array(), reference.as_array());
    let nc = a.dim().2;
    let rc = b.dim().2;
    if a.is_empty() || b.is_empty() || nc != 3 || rc != 3 {
        return Err(PyValueError::new_err("match_colours: expected two nonempty RGB images"));
    }
    Ok(py.detach(|| matched(a, b, sigma)).into_pyarray(py))
}

fn matched(a: ArrayView3<u8>, b: ArrayView3<u8>, sigma: f32) -> Array3<u8> {
    let (h, w, _) = a.dim();
    let (rh, rw, _) = b.dim();
    let small = resize_box_rgb(a, rw, rh);
    let gain = blurred_gain(small.view(), b, sigma);
    let mut out = Array3::zeros((h, w, 3));
    for ch in 0..3 {
        let g = gain.slice(s![.., .., ch]);
        let full;
        let g = if (h, w) == (rh, rw) { g } else {
            full = resize_f32(g, w, h);
            full.view()
        };
        numpy::ndarray::Zip::from(out.slice_mut(s![.., .., ch])).and(a.slice(s![.., .., ch])).and(g)
            .for_each(|v, &p, &g| *v = rint_small(p as f32 * g) as u8);
    }
    out
}

fn blurred_gain(a: ArrayView3<u8>, b: ArrayView3<u8>, sigma: f32) -> Array3<f32> {
    let (h, w, _) = a.dim();
    let mut gain = Array3::zeros((h, w, 3));
    for ch in 0..3 {
        let mut src: Vec<u8> = a.slice(s![.., .., ch]).iter().copied().collect();
        let mut dst: Vec<u8> = b.slice(s![.., .., ch]).iter().copied().collect();
        crate::texdds::pil_gaussian_blur(&mut src, w, h, sigma.max(1.0));
        crate::texdds::pil_gaussian_blur(&mut dst, w, h, sigma.max(1.0));
        for ((g, p), r) in gain.slice_mut(s![.., .., ch]).iter_mut().zip(src).zip(dst) {
            *g = ((r as f32 + 4.0) / (p as f32 + 4.0)).clamp(1.0 / 3.0, 3.0);
        }
    }
    gain
}

/// Rectangular callers may need Pillow-version-specific resize ordering on either side of this filter.
#[pyfunction]
pub fn photo_colour_gain<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, reference: PyReadonlyArray3<'py, u8>,
                              sigma: f32) -> PyResult<Bound<'py, numpy::PyArray3<f32>>> {
    let (a, b) = (rgb.as_array(), reference.as_array());
    if a.is_empty() || a.shape()[2] != 3 || a.dim() != b.dim() {
        return Err(PyValueError::new_err("photo_colour_gain: expected two matching nonempty RGB images"));
    }
    Ok(py.detach(|| blurred_gain(a, b, sigma)).into_pyarray(py))
}

/// Cut a generated far tile directly from its cached ENU squares. Two taps may be on
/// opposite sides of a square boundary. Their multiply-add order is genground.render_far's.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn cut_ground<'py>(py: Python<'py>, squares: Vec<(PyReadonlyArray3<'py, u8>, Option<PyReadonlyArray3<'py, u8>>)>,
                       nx: usize, ny: usize, side: usize, px: usize, x0: f64, y0: f64, size: f64,
                       west: f64, north: f64, spacing: f64)
                       -> PyResult<(Bound<'py, numpy::PyArray3<u8>>, Option<Bound<'py, numpy::PyArray3<u8>>>)> {
    let views: Vec<_> = squares.iter().map(|(a,c)| (a.as_array(), c.as_ref().map(|b| b.as_array()))).collect();
    if views.is_empty() || nx.checked_mul(ny) != Some(views.len()) || side == 0 || px == 0 || !(spacing > 0.0)
        || views.iter().any(|(a,c)| a.dim() != (side,side,3) || c.is_some_and(|b| b.dim() != a.dim())) {
        return Err(PyValueError::new_err("cut_ground: invalid grid of RGB squares"));
    }
    let (w, h) = (nx * side, ny * side);
    let texel = size / px as f64;
    let (floor, canopy) = py.detach(|| {
        let taps = |v: f64, max: usize| {
            let v = v.clamp(0.0, (max - 1) as f64);
            let lo = v as usize;
            (lo, (lo + 1).min(max - 1), v - lo as f64)
        };
        let xs: Vec<_> = (0..px).map(|x| taps((x0 + (x as f64 + 0.5)*texel - west)/spacing - 0.5,w)).collect();
        let ys: Vec<_> = (0..px).map(|y| taps((north - (y0 + size - (y as f64 + 0.5)*texel))/spacing - 0.5,h)).collect();
        let cut = |tiles: Vec<ArrayView3<u8>>| {
            let at = |x: usize,y: usize,ch| {
                // Python enumerates the squares from the south. Each image stores its north row first.
                tiles[(ny - 1 - y / side)*nx + x / side][[y % side,x % side,ch]] as f64
            };
            Array3::from_shape_fn((px,px,3), |(y,x,ch)| {
                let (x0,x1,fx) = xs[x];
                let (y0,y1,fy) = ys[y];
                let top = at(x0,y0,ch)*(1.0-fx) + at(x1,y0,ch)*fx;
                let bot = at(x0,y1,ch)*(1.0-fx) + at(x1,y1,ch)*fx;
                (top*(1.0-fy) + bot*fy).round_ties_even() as u8
            })
        };
        let floor = cut(views.iter().map(|v| v.0).collect());
        let crowns: Option<Vec<_>> = views.iter().map(|v| v.1).collect();
        (floor, crowns.map(cut))
    });
    Ok((floor.into_pyarray(py), canopy.map(|c| c.into_pyarray(py))))
}
