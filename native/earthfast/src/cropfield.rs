//! Crop fields (docs/new/008_crop-fields): what the ground photo shows between a field's edges.
//!
//! The crop map (server/cropmap.py) gives a visual group per 10 m cell. `crop_grid` cleans it (a 3 x 3 majority
//! filter, small blobs dropped) and gives every field (a connected patch of one group) its row direction (the long
//! axis of the patch) and a phase hashed from where it is in the frame, so a field cut by a block edge lines up on
//! both sides. `crop_paint` then draws structure into the photo, keeping the photo's own colours: the rows of
//! maize / sunflower / roots, a cereal's tramlines, the furrows of bare soil, and a grain of noise. How green the
//! photo is (its excess green over ~16 m) decides between plants and bare furrows, so a plant never covers a
//! brown field and a furrow never cuts a green one. Pure functions of their inputs; the noise is anchored to
//! frame coordinates, so neighbouring blocks agree.

use numpy::{PyReadonlyArray2, PyReadwriteArray3};
use pyo3::prelude::*;
use numpy::IntoPyArray;

pub const NONE: u8 = 0;
pub const CEREAL: u8 = 1;
pub const MAIZE: u8 = 2;
pub const SUNFLOWER: u8 = 3;
pub const RAPE: u8 = 4;
pub const ROOT: u8 = 5;
pub const FODDER: u8 = 6;
pub const RICE: u8 = 7;
pub const BARE: u8 = 8;
pub const PULSE: u8 = 9;
const NGROUPS: usize = 10;

const MIN_CELLS: usize = 6;            // a patch smaller than this (~600 m2 at 10 m) is not drawn as a field
const TRAM_SPACING_M: f64 = 24.0;
const SOIL: [f32; 3] = [92.0, 68.0, 42.0];

/// A cell keeps its map class with this many of the 3 x 3 cells agreeing (of 9): then the map names the crop for sure, and
/// the photo's colour does not overrule it. Fewer (a patchy edge, a noisy area): the map is a guess there.
const SURE_MIN: u8 = 6;

/// Majority filter + patches. Returns (group, angle (rad, east = 0, counter-clockwise), phase [0, 1), sure (1 where the
/// class is the cell's own and at least SURE_MIN of its 3 x 3 neighbourhood agree)) per cell.
pub fn grid_filter(g: &[u8], gh: usize, gw: usize, cell_m: f64, e0: f64, n0: f64) -> (Vec<u8>, Vec<f32>, Vec<f32>, Vec<u8>) {
    let mut f = g.to_vec();
    let mut sure = vec![0u8; gh * gw];
    for r in 0..gh {
        for c in 0..gw {
            let mut cnt = [0u8; NGROUPS];
            for dr in -1i64..=1 {
                for dc in -1i64..=1 {
                    let (rr, cc) = (r as i64 + dr, c as i64 + dc);
                    if rr >= 0 && cc >= 0 && (rr as usize) < gh && (cc as usize) < gw {
                        let v = g[rr as usize * gw + cc as usize] as usize;
                        cnt[v.min(NGROUPS - 1)] += 1;
                    }
                }
            }
            let own = g[r * gw + c] as usize;
            let (mut best, mut bn) = (own.min(NGROUPS - 1), cnt[own.min(NGROUPS - 1)]);
            for (v, &n) in cnt.iter().enumerate() {
                if n > bn { best = v; bn = n; }
            }
            f[r * gw + c] = best as u8;
            sure[r * gw + c] = (best == own.min(NGROUPS - 1) && bn >= SURE_MIN) as u8;
        }
    }
    let mut angle = vec![0f32; gh * gw];
    let mut phase = vec![0f32; gh * gw];
    let mut seen = vec![false; gh * gw];
    let mut stack: Vec<usize> = Vec::new();
    let mut cells: Vec<usize> = Vec::new();
    for s in 0..gh * gw {
        if seen[s] || f[s] == NONE { continue; }
        let grp = f[s];
        cells.clear();
        stack.push(s);
        seen[s] = true;
        while let Some(i) = stack.pop() {
            cells.push(i);
            let (r, c) = (i / gw, i % gw);
            let mut push = |rr: usize, cc: usize| {
                let j = rr * gw + cc;
                if !seen[j] && f[j] == grp { seen[j] = true; stack.push(j); }
            };
            if r > 0 { push(r - 1, c); }
            if r + 1 < gh { push(r + 1, c); }
            if c > 0 { push(r, c - 1); }
            if c + 1 < gw { push(r, c + 1); }
        }
        if cells.len() < MIN_CELLS {
            for &i in &cells { f[i] = NONE; }
            continue;
        }
        let n = cells.len() as f64;
        let (mut sx, mut sy) = (0.0, 0.0);
        for &i in &cells { sx += (i % gw) as f64; sy += (i / gw) as f64; }
        let (mx, my) = (sx / n, sy / n);
        let (mut sxx, mut sxy, mut syy) = (0.0, 0.0, 0.0);
        for &i in &cells {
            let (dx, dy) = ((i % gw) as f64 - mx, (i / gw) as f64 - my);
            sxx += dx * dx; sxy += dx * dy; syy += dy * dy;
        }
        // long axis in grid axes (x = col east, y = row south); the frame's north is the opposite of rows
        let a = -0.5 * (2.0 * sxy).atan2(sxx - syy);
        let (ce, cn) = (e0 + mx * cell_m, n0 - my * cell_m);
        let h = hash3((ce / 40.0).floor() as i64, (cn / 40.0).floor() as i64, grp as i64);
        for &i in &cells { angle[i] = a as f32; phase[i] = h as f32; }
    }
    for (i, s) in sure.iter_mut().enumerate() { if f[i] == NONE { *s = 0; } }
    (f, angle, phase, sure)
}

#[inline]
fn hash3(a: i64, b: i64, c: i64) -> f64 {
    let mut x = (a as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15) ^ (b as u64).wrapping_mul(0xC2B2_AE3D_27D4_EB4F)
        ^ (c as u64).wrapping_mul(0x1656_67B1_9E37_79F9);
    x ^= x >> 33; x = x.wrapping_mul(0xFF51_AFD7_ED55_8CCD); x ^= x >> 33; x = x.wrapping_mul(0xC4CE_B9FE_1A85_EC53);
    x ^= x >> 33;
    (x >> 11) as f64 / (1u64 << 53) as f64
}

#[inline]
fn vnoise(x: f64, y: f64, salt: i64) -> f64 {
    let (ix, iy) = (x.floor(), y.floor());
    let (fx, fy) = (x - ix, y - iy);
    let (sx, sy) = (fx * fx * (3.0 - 2.0 * fx), fy * fy * (3.0 - 2.0 * fy));
    let (ix, iy) = (ix as i64, iy as i64);
    let a = hash3(ix, iy, salt);
    let b = hash3(ix + 1, iy, salt);
    let c = hash3(ix, iy + 1, salt);
    let d = hash3(ix + 1, iy + 1, salt);
    (a + (b - a) * sx) + ((c + (d - c) * sx) - (a + (b - a) * sx)) * sy
}

#[inline]
fn smooth(x: f64, a: f64, b: f64) -> f64 {
    let t = ((x - a) / (b - a)).clamp(0.0, 1.0);
    t * t * (3.0 - 2.0 * t)
}

/// 0 = bare ground .. 1 = green, per ~16 m block of the photo, from its excess green (2g - r - b) / (r + g + b).
fn greenness(rgb: &[u8], h: usize, w: usize, px_m: f64) -> (Vec<f32>, usize, usize, usize) {
    let b = ((16.0 / px_m).round() as usize).max(1);
    let (bh, bw) = ((h + b - 1) / b, (w + b - 1) / b);
    let mut sum = vec![[0f64; 3]; bh * bw];
    let mut cnt = vec![0f64; bh * bw];
    for r in 0..h {
        for c in 0..w {
            let k = (r / b) * bw + c / b;
            let o = (r * w + c) * 3;
            sum[k][0] += rgb[o] as f64; sum[k][1] += rgb[o + 1] as f64; sum[k][2] += rgb[o + 2] as f64;
            cnt[k] += 1.0;
        }
    }
    let exg: Vec<f64> = (0..bh * bw).map(|k| {
        let (r, g, bb) = (sum[k][0] / cnt[k], sum[k][1] / cnt[k], sum[k][2] / cnt[k]);
        (2.0 * g - r - bb) / (r + g + bb).max(1.0)
    }).collect();
    let mut out = vec![0f32; bh * bw];
    for r in 0..bh {
        for c in 0..bw {
            let (mut s, mut n) = (0.0, 0.0);
            for dr in -1i64..=1 {
                for dc in -1i64..=1 {
                    let (rr, cc) = (r as i64 + dr, c as i64 + dc);
                    if rr >= 0 && cc >= 0 && (rr as usize) < bh && (cc as usize) < bw {
                        s += exg[rr as usize * bw + cc as usize]; n += 1.0;
                    }
                }
            }
            out[r * bw + c] = smooth(s / n, 0.04, 0.13) as f32;
        }
    }
    (out, bh, bw, b)
}

pub struct Geo {
    pub x0: f64,        // frame east of the photo's left edge
    pub ytop: f64,      // frame north of its top edge
    pub px_m: f64,
    pub ge0: f64,       // frame east / north of the group grid's top-left corner
    pub gn0: f64,
    pub cell_m: f64,
}

/// The group at frame point (e, n) and the grid cell its angle / phase come from: the point is bent by smooth noise
/// (so the 10 m cell edges curve), then the group is a weighted vote of the four nearest cells (rounded corners,
/// diagonal borders). None outside the grid or where no field is.
fn lookup(e: f64, n: f64, geo: &Geo, grp: &[u8], gh: usize, gw: usize) -> Option<(u8, usize)> {
    let we = e + 5.0 * (vnoise(e / 25.0, n / 25.0, 11) - 0.5) * 2.0 + 2.0 * (vnoise(e / 9.0, n / 9.0, 12) - 0.5) * 2.0;
    let wn = n + 5.0 * (vnoise(e / 25.0, n / 25.0, 13) - 0.5) * 2.0 + 2.0 * (vnoise(e / 9.0, n / 9.0, 14) - 0.5) * 2.0;
    let (gx, gy) = ((we - geo.ge0) / geo.cell_m - 0.5, (geo.gn0 - wn) / geo.cell_m - 0.5);
    let (c0, r0g) = (gx.floor(), gy.floor());
    let (tx, ty) = (gx - c0, gy - r0g);
    let mut wsum = [0f64; NGROUPS];
    let mut best_cell = [usize::MAX; NGROUPS];
    let mut best_w = [0f64; NGROUPS];
    for (dr, dc, wt) in [(0i64, 0i64, (1.0 - tx) * (1.0 - ty)), (0, 1, tx * (1.0 - ty)), (1, 0, (1.0 - tx) * ty), (1, 1, tx * ty)] {
        let (rr, cc) = (r0g as i64 + dr, c0 as i64 + dc);
        if rr < 0 || cc < 0 || rr >= gh as i64 || cc >= gw as i64 { continue; }
        let i = rr as usize * gw + cc as usize;
        let gk = (grp[i] as usize).min(NGROUPS - 1);
        wsum[gk] += wt;
        if wt >= best_w[gk] { best_w[gk] = wt; best_cell[gk] = i; }
    }
    let mut g = NONE;
    let mut gbest = 0.0;
    for k in 0..NGROUPS {
        if wsum[k] > gbest { gbest = wsum[k]; g = k as u8; }
    }
    if g == NONE || best_cell[g as usize] == usize::MAX { return None; }
    Some((g, best_cell[g as usize]))
}

/// Row period of a group (m), 0 = no rows.
fn row_period(g: u8) -> f64 {
    match g { MAIZE | SUNFLOWER | ROOT => 1.5, PULSE | RICE => 1.8, _ => 0.0 }
}

fn paint_rows(buf: &mut [u8], r0: usize, w: usize, geo: &Geo, grp: &[u8], ang: &[f32], ph: &[f32], gh: usize, gw: usize,
              green: &[f32], gb: (usize, usize, usize), dirt: Option<&[u8]>) {
    let (bh, bw, b) = gb;
    let rows = buf.len() / (w * 3);
    let fade = |period: f64| 1.0 - smooth(geo.px_m, period / 3.0, period / 2.0);
    for lr in 0..rows {
        let r = r0 + lr;
        let n = geo.ytop - (r as f64 + 0.5) * geo.px_m;
        for c in 0..w {
            if let Some(d) = dirt { if d[r * w + c] != 0 { continue; } }
            let e = geo.x0 + (c as f64 + 0.5) * geo.px_m;
            let (g, gi) = match lookup(e, n, geo, grp, gh, gw) { Some(v) => v, None => continue };
            // greenness, bilinear over the 16 m block centres
            let (fr, fc) = ((r as f64 + 0.5) / b as f64 - 0.5, (c as f64 + 0.5) / b as f64 - 0.5);
            let (r0b, c0b) = (fr.floor().max(0.0) as usize, fc.floor().max(0.0) as usize);
            let (r1b, c1b) = ((r0b + 1).min(bh - 1), (c0b + 1).min(bw - 1));
            let (tr, tc) = ((fr - r0b as f64).clamp(0.0, 1.0), (fc - c0b as f64).clamp(0.0, 1.0));
            let gv = |rr: usize, cc: usize| green[rr.min(bh - 1) * bw + cc.min(bw - 1)] as f64;
            let gn = (gv(r0b, c0b) * (1.0 - tc) + gv(r0b, c1b) * tc) * (1.0 - tr) + (gv(r1b, c0b) * (1.0 - tc) + gv(r1b, c1b) * tc) * tr;
            let a = ang[gi] as f64;
            let across = -e * a.sin() + n * a.cos();
            let phase = ph[gi] as f64;
            let mut m = 1.0 + 0.05 * (vnoise(e / 0.8, n / 0.8, 3) - 0.5) * 2.0 + 0.06 * (vnoise(e / 1.4, n / 1.4, 1) - 0.5) * 2.0
                + 0.05 * (vnoise(e / 5.0, n / 5.0, 2) - 0.5) * 2.0;
            let bare = if g == BARE { 1.0 } else { 1.0 - gn };
            let plant = if g == BARE { 0.0 } else { gn.max(0.45) };
            let rp = row_period(g);
            if rp > 0.0 {
                // rows with their own strength each (a sown row is never quite like its neighbour)
                let q = across / rp - phase;
                let amp = 0.6 + 0.8 * hash3(q.floor() as i64, (phase * 1e6) as i64, 31);
                m += 0.02 * amp * plant * fade(rp) * (std::f64::consts::TAU * q).cos();
            }
            if matches!(g, CEREAL | RAPE | FODDER) && geo.px_m <= 1.0 {
                let d = ((across - phase * TRAM_SPACING_M + TRAM_SPACING_M * 0.5).rem_euclid(TRAM_SPACING_M) - TRAM_SPACING_M * 0.5).abs();
                let cov = (1.0 - d / (0.9 + geo.px_m * 0.5)).clamp(0.0, 1.0);
                // broken here and there, as a tramline is where the sprayer's wheels pressed the crop unevenly
                m -= 0.07 * plant * cov * cov * (0.4 + 1.2 * vnoise(e / 9.0, n / 9.0, 21));
            }
            let fp = 2.0;
            let q = across / fp - phase;
            let amp = 0.6 + 0.8 * hash3(q.floor() as i64, (phase * 1e6) as i64, 32);
            m += 0.14 * amp * bare * fade(fp) * (std::f64::consts::TAU * q).cos();
            let o = (lr * w + c) * 3;
            let t = (0.32 * bare) as f32;
            for k in 0..3 {
                let v = buf[o + k] as f32 * (1.0 - t) + SOIL[k] * t;
                buf[o + k] = (v * m as f32).round().clamp(0.0, 255.0) as u8;
            }
        }
    }
}

/// (group, angle, phase, sure) per cell of the (gh, gw) crop-group grid; see the module header and SURE_MIN.
#[pyfunction]
pub fn crop_grid<'py>(py: Python<'py>, groups: PyReadonlyArray2<'py, u8>, cell_m: f64, e0: f64, n0: f64)
    -> (Bound<'py, numpy::PyArray2<u8>>, Bound<'py, numpy::PyArray2<f32>>, Bound<'py, numpy::PyArray2<f32>>,
        Bound<'py, numpy::PyArray2<u8>>) {
    let v = groups.as_array();
    let (gh, gw) = (v.shape()[0], v.shape()[1]);
    let flat: Vec<u8> = v.iter().copied().collect();
    let (f, a, p, s) = py.allow_threads(|| grid_filter(&flat, gh, gw, cell_m, e0, n0));
    let shu = |x: Vec<u8>| numpy::ndarray::Array2::from_shape_vec((gh, gw), x).unwrap();
    let shf = |x: Vec<f32>| numpy::ndarray::Array2::from_shape_vec((gh, gw), x).unwrap();
    (shu(f).into_pyarray(py), shf(a).into_pyarray(py), shf(p).into_pyarray(py), shu(s).into_pyarray(py))
}

/// The field group at frame points (e, n) (metres), from a cleaned grid (crop_grid's first array), the same lookup
/// the photo painting uses, so the ground layers and the photo's rows end where each other do.
/// geo = [x0, ytop, px_m, ge0, gn0, cell_m] (only the last three are used). Returns (group, sure) per point: sure is
/// crop_grid's flag of the cell the group was taken from.
#[pyfunction]
pub fn crop_sample<'py>(py: Python<'py>, groups: PyReadonlyArray2<'py, u8>, sure: PyReadonlyArray2<'py, u8>,
                        e: numpy::PyReadonlyArray1<'py, f64>, n: numpy::PyReadonlyArray1<'py, f64>, geo: Vec<f64>)
    -> PyResult<(Bound<'py, numpy::PyArray1<u8>>, Bound<'py, numpy::PyArray1<u8>>)> {
    if geo.len() != 6 { return Err(pyo3::exceptions::PyValueError::new_err("geo needs 6 numbers")); }
    let gs = groups.as_array().shape().to_vec();
    let (gh, gw) = (gs[0], gs[1]);
    let g: Vec<u8> = groups.as_array().iter().copied().collect();
    let sv: Vec<u8> = sure.as_array().iter().copied().collect();
    if sv.len() != g.len() { return Err(pyo3::exceptions::PyValueError::new_err("sure and groups differ in shape")); }
    let (ev, nv) = (e.as_array(), n.as_array());
    if ev.len() != nv.len() { return Err(pyo3::exceptions::PyValueError::new_err("e and n differ in length")); }
    let g_ = Geo { x0: geo[0], ytop: geo[1], px_m: geo[2], ge0: geo[3], gn0: geo[4], cell_m: geo[5] };
    let pts: Vec<(f64, f64)> = ev.iter().copied().zip(nv.iter().copied()).collect();
    let (out, flag): (Vec<u8>, Vec<u8>) = py.allow_threads(|| pts.iter().map(|&(e, n)| {
        lookup(e, n, &g_, &g, gh, gw).map(|v| (v.0, sv[v.1])).unwrap_or((NONE, 0))
    }).unzip());
    Ok((out.into_pyarray(py), flag.into_pyarray(py)))
}

/// Draws the fields into rgb (h, w, 3) in place. geo = [x0, ytop, px_m, ge0, gn0, cell_m]; dirt (h, w) u8 or (0, 0):
/// pixels left alone (a dirt road's own paint).
#[pyfunction]
pub fn crop_paint(py: Python<'_>, mut rgb: PyReadwriteArray3<'_, u8>, groups: PyReadonlyArray2<'_, u8>,
                  angle: PyReadonlyArray2<'_, f32>, phase: PyReadonlyArray2<'_, f32>, geo: Vec<f64>,
                  dirt: PyReadonlyArray2<'_, u8>) -> PyResult<usize> {
    if geo.len() != 6 { return Err(pyo3::exceptions::PyValueError::new_err("geo needs 6 numbers")); }
    let sh = rgb.as_array().shape().to_vec();
    if sh.len() != 3 || sh[2] != 3 { return Err(pyo3::exceptions::PyValueError::new_err("rgb must be (h, w, 3)")); }
    let (h, w) = (sh[0], sh[1]);
    let gs = groups.as_array().shape().to_vec();
    let (gh, gw) = (gs[0], gs[1]);
    let g: Vec<u8> = groups.as_array().iter().copied().collect();
    let a: Vec<f32> = angle.as_array().iter().copied().collect();
    let p: Vec<f32> = phase.as_array().iter().copied().collect();
    let d: Option<Vec<u8>> = if dirt.as_array().shape() == [h, w] { Some(dirt.as_array().iter().copied().collect()) } else { None };
    let g_ = Geo { x0: geo[0], ytop: geo[1], px_m: geo[2], ge0: geo[3], gn0: geo[4], cell_m: geo[5] };
    let slice = rgb.as_slice_mut().map_err(|_| pyo3::exceptions::PyValueError::new_err("rgb must be contiguous"))?;
    let painted = py.allow_threads(|| {
        let (green, bh, bw, b) = greenness(slice, h, w, g_.px_m);
        let nt = std::thread::available_parallelism().map(|n| n.get()).unwrap_or(2).clamp(1, 8);
        let rows_per = (h + nt - 1) / nt;
        std::thread::scope(|s| {
            for (i, chunk) in slice.chunks_mut(rows_per * w * 3).enumerate() {
                let (g_r, g, a, p, green, d) = (&g_, &g, &a, &p, &green, &d);
                s.spawn(move || paint_rows(chunk, i * rows_per, w, g_r, g, a, p, gh, gw, green, (bh, bw, b),
                                           d.as_deref()));
            }
        });
        g.iter().filter(|&&x| x != NONE).count()
    });
    Ok(painted)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn speckle_is_filtered_and_patch_gets_axis() {
        // 12 x 12 grid: a wide maize patch (6 rows x 12 cols) with one cereal speck inside
        let (gh, gw) = (12, 12);
        let mut g = vec![NONE; gh * gw];
        for r in 3..9 { for c in 0..12 { g[r * gw + c] = MAIZE; } }
        g[5 * gw + 5] = CEREAL;
        let (f, a, p, _) = grid_filter(&g, gh, gw, 10.0, 0.0, 120.0);
        assert_eq!(f[5 * gw + 5], MAIZE);
        // a patch that is long east-west: axis ~ 0 (mod pi)
        let ang = a[5 * gw + 2] as f64;
        assert!(ang.sin().abs() < 0.1, "angle {}", ang);
        assert!(p[5 * gw + 2] >= 0.0 && p[5 * gw + 2] < 1.0);
    }

    #[test]
    fn sure_marks_a_solid_field_not_its_ragged_edge() {
        // a 10 x 10 maize block: its middle is sure, a lone cell stuck to its edge is not
        let (gh, gw) = (14, 14);
        let mut g = vec![NONE; gh * gw];
        for r in 2..12 { for c in 2..12 { g[r * gw + c] = MAIZE; } }
        g[1 * gw + 6] = MAIZE;
        let (f, _, _, s) = grid_filter(&g, gh, gw, 10.0, 0.0, 140.0);
        assert_eq!(s[6 * gw + 6], 1, "the middle of the field");
        assert_eq!(s[0], 0, "no field, no flag");
        assert_eq!(f[1 * gw + 6], NONE);
    }

    #[test]
    fn tiny_patch_is_dropped() {
        let (gh, gw) = (8, 8);
        let mut g = vec![NONE; gh * gw];
        g[3 * gw + 3] = SUNFLOWER; g[3 * gw + 4] = SUNFLOWER; g[4 * gw + 3] = SUNFLOWER; g[4 * gw + 4] = SUNFLOWER;
        let (f, _, _, _) = grid_filter(&g, gh, gw, 10.0, 0.0, 80.0);
        assert!(f.iter().all(|&x| x == NONE));
    }

    #[test]
    fn same_field_same_phase_in_two_windows() {
        // a field seen from two grids shifted by whole cells: its phase and angle agree
        let (gh, gw) = (14, 14);
        let mut g = vec![NONE; gh * gw];
        for r in 4..10 { for c in 3..11 { g[r * gw + c] = CEREAL; } }
        let (_, a1, p1, _) = grid_filter(&g, gh, gw, 10.0, 0.0, 140.0);
        let (_, a2, p2, _) = grid_filter(&g, gh, gw, 10.0, 0.0, 140.0);
        assert_eq!(a1, a2);
        assert_eq!(p1, p2);
    }

    #[test]
    fn noise_is_deterministic() {
        assert_eq!(vnoise(3.3, 7.7, 5), vnoise(3.3, 7.7, 5));
        let v = vnoise(100.2, -40.9, 1);
        assert!((0.0..=1.0).contains(&v));
    }
}
