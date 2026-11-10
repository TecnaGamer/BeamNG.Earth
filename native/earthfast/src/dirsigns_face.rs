//! The face of a direction board: the picture the board shows (dirsigns.rs places and meshes the boards; server/
//! dirsigns.py composes what each says). One PNG a board, drawn here: the background and its white edge line, one row
//! a way to go (an arrow, the road number plates, the places ahead in the local language with the English name
//! smaller under it, their distances), all in metres scaled to px_per_m. Text is rasterised by fontdue from the two
//! typefaces the server hands over (dirsign_set_fonts: a regular face and a condensed one for names too long for the
//! board); plates and arrows are drawn here with anti-aliased edges.
//!
//! Deterministic: the same content and style are the same bytes (the server names the file by their hash).

use std::sync::OnceLock;

use fontdue::{Font, FontSettings};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

static FONTS: OnceLock<(Font, Font)> = OnceLock::new();

/// The typefaces (TTF / OTF bytes): the regular one, then the condensed one. The first call wins.
#[pyfunction]
pub fn dirsign_set_fonts(main: Vec<u8>, condensed: Vec<u8>) -> PyResult<()> {
    let load = |b: Vec<u8>| Font::from_bytes(b, FontSettings::default()).map_err(|e| PyValueError::new_err(e.to_string()));
    let pair = (load(main)?, load(condensed)?);
    let _ = FONTS.set(pair);
    Ok(())
}

type Rgb = [f32; 3];

struct Canvas {
    w: usize,
    h: usize,
    px: Vec<f32>,       // RGB
}

impl Canvas {
    fn new(w: usize, h: usize, c: Rgb) -> Canvas {
        let mut px = Vec::with_capacity(w * h * 3);
        for _ in 0..w * h { px.extend_from_slice(&c); }
        Canvas { w, h, px }
    }

    fn blend(&mut self, x: i64, y: i64, c: Rgb, a: f32) {
        if x < 0 || y < 0 || x as usize >= self.w || y as usize >= self.h || a <= 0.0 { return; }
        let i = (y as usize * self.w + x as usize) * 3;
        let a = a.min(1.0);
        for k in 0..3 { self.px[i + k] += (c[k] - self.px[i + k]) * a; }
    }

    /// A rounded rectangle (px) filled with c.
    fn rrect(&mut self, x0: f32, y0: f32, x1: f32, y1: f32, r: f32, c: Rgb) {
        let (cx, cy, hx, hy) = ((x0 + x1) / 2.0, (y0 + y1) / 2.0, (x1 - x0) / 2.0, (y1 - y0) / 2.0);
        let r = r.min(hx).min(hy);
        for y in (y0.floor() as i64 - 1)..=(y1.ceil() as i64 + 1) {
            for x in (x0.floor() as i64 - 1)..=(x1.ceil() as i64 + 1) {
                let (px, py) = (x as f32 + 0.5 - cx, y as f32 + 0.5 - cy);
                let (qx, qy) = (px.abs() - (hx - r), py.abs() - (hy - r));
                let d = (qx.max(0.0).powi(2) + qy.max(0.0).powi(2)).sqrt() + qx.max(qy).min(0.0) - r;
                self.blend(x, y, c, (0.5 - d).clamp(0.0, 1.0));
            }
        }
    }

    /// A polygon (px) filled with c, 4 x 4 samples a pixel (even-odd).
    fn poly(&mut self, pts: &[[f32; 2]], c: Rgb) {
        let (mut x0, mut y0, mut x1, mut y1) = (f32::MAX, f32::MAX, f32::MIN, f32::MIN);
        for p in pts { x0 = x0.min(p[0]); y0 = y0.min(p[1]); x1 = x1.max(p[0]); y1 = y1.max(p[1]); }
        for y in (y0.floor() as i64)..=(y1.ceil() as i64) {
            for x in (x0.floor() as i64)..=(x1.ceil() as i64) {
                let mut hit = 0;
                for sy in 0..4 {
                    for sx in 0..4 {
                        let (qx, qy) = (x as f32 + (sx as f32 + 0.5) / 4.0, y as f32 + (sy as f32 + 0.5) / 4.0);
                        let mut inside = false;
                        let mut j = pts.len() - 1;
                        for i in 0..pts.len() {
                            let (a, b) = (pts[i], pts[j]);
                            if (a[1] > qy) != (b[1] > qy) && qx < (b[0] - a[0]) * (qy - a[1]) / (b[1] - a[1]) + a[0] { inside = !inside; }
                            j = i;
                        }
                        if inside { hit += 1; }
                    }
                }
                if hit > 0 { self.blend(x, y, c, hit as f32 / 16.0); }
            }
        }
    }

    /// A thick line from p to q.
    fn line(&mut self, p: [f32; 2], q: [f32; 2], w: f32, c: Rgb) {
        let (dx, dy) = (q[0] - p[0], q[1] - p[1]);
        let l = (dx * dx + dy * dy).sqrt().max(1e-6);
        let (nx, ny) = (-dy / l * w / 2.0, dx / l * w / 2.0);
        self.poly(&[[p[0] + nx, p[1] + ny], [q[0] + nx, q[1] + ny], [q[0] - nx, q[1] - ny], [p[0] - nx, p[1] - ny]], c);
    }
}

fn fonts() -> PyResult<&'static (Font, Font)> {
    FONTS.get().ok_or_else(|| PyValueError::new_err("dirsign_set_fonts was not called"))
}

/// The width of text at size px (advances and the kerning the font has).
fn text_w(f: &Font, text: &str, size: f32) -> f32 {
    let mut x = 0.0;
    let mut prev: Option<char> = None;
    for c in text.chars() {
        if let Some(p) = prev { x += f.horizontal_kern(p, c, size).unwrap_or(0.0); }
        x += f.metrics(c, size).advance_width;
        prev = Some(c);
    }
    x
}

/// Text with its left end at x and its baseline at y.
fn draw_text(cv: &mut Canvas, f: &Font, text: &str, size: f32, x: f32, base: f32, c: Rgb) {
    let mut x = x;
    let mut prev: Option<char> = None;
    for ch in text.chars() {
        if let Some(p) = prev { x += f.horizontal_kern(p, ch, size).unwrap_or(0.0); }
        let (m, bm) = f.rasterize(ch, size);
        let gx = (x + m.xmin as f32).round() as i64;
        let gy = (base - m.height as f32 - m.ymin as f32).round() as i64;
        for row in 0..m.height {
            for col in 0..m.width {
                let a = bm[row * m.width + col] as f32 / 255.0;
                if a > 0.0 { cv.blend(gx + col as i64, gy + row as i64, c, a); }
            }
        }
        x += m.advance_width;
        prev = Some(ch);
    }
}

/// Which way a row goes: -1 left, 0 straight, 1 right (an arrow in the first column).
fn arrow(cv: &mut Canvas, turn: i64, x: f32, y: f32, w: f32, h: f32, c: Rgb) {
    let t = h * 0.16;           // the shaft's thickness
    let head = h * 0.42;
    let (cx, base_y, top_y) = (x + w / 2.0, y + h, y);
    match turn {
        0 => {
            cv.line([cx, base_y], [cx, top_y + head * 0.6], t, c);
            cv.poly(&[[cx, top_y], [cx - head * 0.62, top_y + head], [cx + head * 0.62, top_y + head]], c);
        }
        _ => {
            let s = if turn < 0 { -1.0 } else { 1.0 };
            let turn_y = y + h * 0.42;
            let end_x = cx + s * w * 0.46;
            cv.line([cx - s * w * 0.12, base_y], [cx - s * w * 0.12, turn_y + t / 2.0], t, c);
            cv.line([cx - s * w * 0.12 - s * t / 2.0, turn_y], [end_x - s * head * 0.55, turn_y], t, c);
            cv.poly(&[[end_x, turn_y], [end_x - s * head, turn_y - head * 0.62], [end_x - s * head, turn_y + head * 0.62]], c);
        }
    }
}

fn rgb(v: &[f64], at: usize) -> Rgb { [v[at] as f32 / 255.0, v[at + 1] as f32 / 255.0, v[at + 2] as f32 / 255.0] }

/// One row: the turn (-1 / 0 / 1), the plates [(kind, text)], the places [(local name, English name or "", distance
/// text or "")].
type Row = (i64, Vec<(i64, String)>, Vec<(String, String, String)>);

/// The picture of a board. style: [bg r g b, ink r g b, trim r g b, cap height m (the local names' capitals), English
/// name size (share of the local), min width m, max width m, edge inset m, trim width m]. plates: per kind [shape
/// (0 rectangle, 1 pill), fill r g b, ink r g b, edge r g b]. Returns (PNG bytes, width m, height m).
#[pyfunction]
pub fn dirsign_face(rows: Vec<Row>, style: Vec<f64>, plates: Vec<Vec<f64>>, px_per_m: f64) -> PyResult<(Vec<u8>, f64, f64)> {
    if style.len() < 17 || rows.is_empty() { return Err(PyValueError::new_err("bad face description")); }
    let (main, cond) = fonts()?;
    let (bg, ink, trim) = (rgb(&style, 0), rgb(&style, 3), rgb(&style, 6));
    let (cap, en_share, min_w, max_w, inset, trim_w) = (style[9] as f32, style[10] as f32, style[11] as f32, style[12] as f32, style[13] as f32, style[14] as f32);
    let ppm = px_per_m as f32;
    let cap_ratio = main.metrics('H', 100.0).height as f32 / 100.0;
    let pad = cap * 0.75 + inset + trim_w;
    let gap = cap * 0.55;
    // the sizes in m: the local names at `size` (em), the English at en_share of it
    let size_em = cap / cap_ratio;
    let plate_h = cap * 1.45;
    let arrow_w = cap * 1.7;

    // the column widths (m) for a scale s of the text in a face (main / condensed); text_w is in px, sizes em m * ppm
    let measure_m = |face: &Font, s: f32| -> (f32, f32, f32) {
        let (mut name_w, mut km_w, mut plates_w) = (0.0f32, 0.0f32, 0.0f32);
        for (_, plates_, dests) in &rows {
            let mut pw = 0.0;
            for (_, t) in plates_ { pw += text_w(main, t, size_em * 0.78 * s * ppm) / ppm + cap * 0.9 + gap * 0.35; }
            plates_w = plates_w.max(pw);
            for (loc, en, km) in dests {
                name_w = name_w.max(text_w(face, loc, size_em * s * ppm) / ppm);
                if !en.is_empty() { name_w = name_w.max(text_w(face, en, size_em * en_share * s * ppm) / ppm); }
                km_w = km_w.max(text_w(main, km, size_em * s * ppm) / ppm);
            }
        }
        (name_w, km_w, plates_w)
    };
    let fixed = |km_w: f32, plates_w: f32| pad * 2.0 + arrow_w + gap + plates_w + gap + if km_w > 0.0 { gap + km_w } else { 0.0 };
    // the widest board that fits: the regular face, then the condensed, then smaller (down to 0.72)
    let mut face: &Font = main;
    let mut s = 1.0f32;
    let (mut name_w, mut km_w, mut plates_w) = measure_m(face, s);
    if fixed(km_w, plates_w) + name_w > max_w {
        face = cond;
        let m = measure_m(face, s);
        name_w = m.0; km_w = m.1; plates_w = m.2;
        while fixed(km_w, plates_w) + name_w > max_w && s > 0.72 {
            s -= 0.04;
            let m = measure_m(face, s);
            name_w = m.0; km_w = m.1; plates_w = m.2;
        }
    }
    let width_m = (fixed(km_w, plates_w) + name_w).clamp(min_w, max_w.max(min_w));
    // rows
    let loc_em = size_em * s;
    let en_em = size_em * en_share * s;
    let mut heights = Vec::new();
    for (_, _, dests) in &rows {
        let mut h = 0.0;
        for (_, en, _) in dests {
            h += cap * s * 1.0 + if en.is_empty() { 0.0 } else { cap * s * en_share * 1.0 + cap * 0.28 };
            h += cap * 0.45;
        }
        heights.push(h.max(plate_h + cap * 0.3).max(arrow_w * 0.9));
    }
    let height_m = heights.iter().sum::<f32>() + pad * 2.0;
    let (wpx, hpx) = ((width_m * ppm).ceil() as usize, (height_m * ppm).ceil() as usize);
    if wpx == 0 || hpx == 0 || wpx > 4096 || hpx > 4096 { return Err(PyValueError::new_err("face too large")); }
    let mut cv = Canvas::new(wpx, hpx, trim);
    // the field inside the white edge line
    let e = (inset * ppm).max(1.0);
    cv.rrect(e, e, wpx as f32 - e, hpx as f32 - e, 0.0, bg);
    let tw = (trim_w * ppm).max(1.0);
    cv.rrect(e + tw, e + tw, wpx as f32 - e - tw, hpx as f32 - e - tw, 0.0, bg);
    let mut y = pad;
    for (ri, (turn, plates_, dests)) in rows.iter().enumerate() {
        let rh = heights[ri];
        // the arrow, vertically centred
        arrow(&mut cv, *turn, pad * ppm, (y + (rh - arrow_w * 0.9) / 2.0) * ppm, arrow_w * ppm * 0.9, arrow_w * 0.9 * ppm, ink);
        // the plates after it
        let mut x = pad + arrow_w + gap;
        for (kind, t) in plates_ {
            let spec = plates.get((*kind).max(0) as usize).or_else(|| plates.first());
            let Some(sp) = spec else { continue };
            let (shape, fill, pink, edge) = (sp[0] as i64, rgb(sp, 1), rgb(sp, 4), rgb(sp, 7));
            let size = size_em * 0.78 * s * ppm;
            let tw_px = text_w(main, t, size);
            let pw = tw_px + cap * 0.9 * ppm;
            let (px0, py0) = (x * ppm, (y + (rh - plate_h) / 2.0) * ppm);
            let r = if shape == 1 { plate_h * ppm / 2.0 } else { plate_h * ppm * 0.12 };
            cv.rrect(px0, py0, px0 + pw, py0 + plate_h * ppm, r, edge);
            let bw = plate_h * ppm * 0.07;
            cv.rrect(px0 + bw, py0 + bw, px0 + pw - bw, py0 + plate_h * ppm - bw, (r - bw).max(0.0), fill);
            let capm = main.metrics('H', size).height as f32;
            draw_text(&mut cv, main, t, size, px0 + (pw - tw_px) / 2.0, py0 + (plate_h * ppm + capm) / 2.0, pink);
            x += pw / ppm + gap * 0.35;
        }
        // the places: local name, English under it smaller, distance at the right
        let name_x = pad + arrow_w + gap + plates_w + gap;
        let km_right = width_m - pad;
        let mut ty = y + cap * 0.22;
        let total: f32 = dests.iter().map(|(_, en, _)| cap * s + if en.is_empty() { 0.0 } else { cap * s * en_share + cap * 0.28 } + cap * 0.45).sum();
        ty += ((rh - total) / 2.0).max(0.0);
        for (loc, en, km) in dests {
            let base = ty + cap * s;
            draw_text(&mut cv, face, loc, loc_em * ppm, name_x * ppm, base * ppm, ink);
            if !km.is_empty() {
                let kw = text_w(main, km, loc_em * ppm);
                draw_text(&mut cv, main, km, loc_em * ppm, km_right * ppm - kw, base * ppm, ink);
            }
            ty = base;
            if !en.is_empty() {
                ty += cap * 0.28 + cap * s * en_share;
                draw_text(&mut cv, face, en, en_em * ppm, name_x * ppm, ty * ppm, ink);
            }
            ty += cap * 0.45;
        }
        y += rh;
    }
    let out = finish(&cv, wpx, hpx)?;
    Ok((out, (wpx as f32 / ppm) as f64, (hpx as f32 / ppm) as f64))
}

/// The PNG of a canvas, stretched up to power-of-two sizes.
fn finish(cv: &Canvas, wpx: usize, hpx: usize) -> PyResult<Vec<u8>> {
    // The engine's texture cooker takes only power-of-two sizes (in game 2026-11-02: "Size must be a power of two", the
    // board stayed IMPORT FAILED): the picture stretched up to the next ones (bilinear), the mesh's UV 0..1 stretches
    // it back to the board's own proportions.
    let (tw_px, th_px) = (wpx.next_power_of_two().max(16), hpx.next_power_of_two().max(16));
    let mut raw = Vec::with_capacity(tw_px * th_px * 3);
    for y in 0..th_px {
        let sy = ((y as f32 + 0.5) * hpx as f32 / th_px as f32 - 0.5).clamp(0.0, hpx as f32 - 1.0);
        let (y0, fy) = (sy.floor() as usize, sy.fract());
        let y1 = (y0 + 1).min(hpx - 1);
        for x in 0..tw_px {
            let sx = ((x as f32 + 0.5) * wpx as f32 / tw_px as f32 - 0.5).clamp(0.0, wpx as f32 - 1.0);
            let (x0, fx) = (sx.floor() as usize, sx.fract());
            let x1 = (x0 + 1).min(wpx - 1);
            for k in 0..3 {
                let p = |xx: usize, yy: usize| cv.px[(yy * wpx + xx) * 3 + k];
                let v = (p(x0, y0) * (1.0 - fx) + p(x1, y0) * fx) * (1.0 - fy) + (p(x0, y1) * (1.0 - fx) + p(x1, y1) * fx) * fy;
                raw.push((v * 255.0 + 0.5).clamp(0.0, 255.0) as u8);
            }
        }
    }
    let mut out = Vec::new();
    {
        let mut enc = png::Encoder::new(&mut out, tw_px as u32, th_px as u32);
        enc.set_color(png::ColorType::Rgb);
        enc.set_depth(png::BitDepth::Eight);
        let mut w = enc.write_header().map_err(|e| PyValueError::new_err(e.to_string()))?;
        w.write_image_data(&raw).map_err(|e| PyValueError::new_err(e.to_string()))?;
    }
    Ok(out)
}

/// The picture of a town entry or exit sign: the settlement's name (the English name smaller under it where there is
/// one) on a plain field with a thin edge line; an exit has the diagonal bar across it. style: [bg r g b, ink r g b,
/// edge r g b, bar r g b, cap height m, English name size (share of the local), min width m, max width m, edge inset m,
/// edge line m, bar width m]. Returns (PNG bytes, width m, height m).
#[pyfunction]
pub fn dirsign_town_face(name: String, en: String, exit: bool, style: Vec<f64>, px_per_m: f64) -> PyResult<(Vec<u8>, f64, f64)> {
    if style.len() < 21 || name.trim().is_empty() { return Err(PyValueError::new_err("bad sign description")); }
    let (main, cond) = fonts()?;
    let (bg, ink, edge, bar) = (rgb(&style, 0), rgb(&style, 3), rgb(&style, 6), rgb(&style, 9));
    let (cap, en_share, min_w, max_w, inset, line_w, bar_w) =
        (style[12] as f32, style[13] as f32, style[14] as f32, style[15] as f32, style[16] as f32, style[17] as f32, style[18] as f32);
    let ppm = px_per_m as f32;
    let cap_ratio = main.metrics('H', 100.0).height as f32 / 100.0;
    let size_em = cap / cap_ratio;
    let pad = cap * 0.7 + inset + line_w;
    let measure = |face: &Font, s: f32| -> f32 {
        let mut w = text_w(face, &name, size_em * s * ppm) / ppm;
        if !en.is_empty() { w = w.max(text_w(face, &en, size_em * en_share * s * ppm) / ppm); }
        w
    };
    let mut face: &Font = main;
    let mut s = 1.0f32;
    if measure(face, s) + pad * 2.0 > max_w {
        face = cond;
        while measure(face, s) + pad * 2.0 > max_w && s > 0.5 { s -= 0.04; }
    }
    let width_m = (measure(face, s) + pad * 2.0).clamp(min_w, max_w.max(min_w));
    let lines_h = cap * s + if en.is_empty() { 0.0 } else { cap * s * en_share + cap * 0.35 };
    let height_m = lines_h + pad * 2.0;
    let (wpx, hpx) = ((width_m * ppm).ceil() as usize, (height_m * ppm).ceil() as usize);
    if wpx == 0 || hpx == 0 || wpx > 4096 || hpx > 4096 { return Err(PyValueError::new_err("sign too large")); }
    let mut cv = Canvas::new(wpx, hpx, edge);
    let e = (inset * ppm).max(1.0);
    let lw = (line_w * ppm).max(1.0);
    cv.rrect(e, e, wpx as f32 - e, hpx as f32 - e, 0.0, bg);
    cv.rrect(e + lw, e + lw, wpx as f32 - e - lw, hpx as f32 - e - lw, 0.0, bg);
    let centred = |cv: &mut Canvas, text: &str, em: f32, base: f32| {
        let tw = text_w(face, text, em * ppm);
        draw_text(cv, face, text, em * ppm, (wpx as f32 - tw) / 2.0, base * ppm, ink);
    };
    let mut base = pad + cap * s;
    centred(&mut cv, &name, size_em * s, base);
    if !en.is_empty() {
        base += cap * 0.35 + cap * s * en_share;
        centred(&mut cv, &en, size_em * s * en_share, base);
    }
    if exit {
        // from the upper right to the lower left corner of the field, over the name
        let m = e + lw + 0.012 * ppm;
        cv.line([wpx as f32 - m, m], [m, hpx as f32 - m], (bar_w * ppm).max(2.0), bar);
    }
    let out = finish(&cv, wpx, hpx)?;
    Ok((out, (wpx as f32 / ppm) as f64, (hpx as f32 / ppm) as f64))
}
