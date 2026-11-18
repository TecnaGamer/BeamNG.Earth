//! Generated ground, stage 5 (the user, 2026-11-16: "As well as any shadows that appear from mountains and buildings";
//! "better methods of shadow removal for harsh shadows"): the capture sun's light taken out of the Sentinel-2 colour
//! the generated ground starts from, since the game lights the ground again and the two doubled up (slope shading,
//! mountain shadows in valleys, long building shadows in cities).
//!
//! The rank-1 core of the Codex report (.claude/work/codex-harsh-shadows-2026-11-16.md), sized for one block:
//! - geometry: the terrain's cast shadow and Lambert term on a coarse DEM grid (a bounded march towards the sun), and
//!   the buildings' and canopy's cast shadows at the texels (a shadow-volume sweep over their heights above the
//!   ground, the receiver at its own height: a roof or crown in the open is lit);
//! - a continuous matte: the geometry says where a shadow may be (dilated by the registration error), the image says
//!   how dark it is there: alpha = (1 - lum / lit) / (1 - k) against the same kind's lit texels round it (an
//!   independent gate per data cell for the buildings, so a flat city is not skipped by the terrain's);
//! - relight: I' = I x (rho + lam') / (rho + lam' (1 - alpha)) with the slope part capped (deshadow.py's model),
//!   rho from the measured shadow / lit ratio k; noise-aware (a box blur inside the shadow, by gain, before it); a
//!   texel too dark to recover (clipped umbra) is not boosted but marked lost: gen_ground fills it from the same
//!   kind's lit texels round it (its gap table).
//!
//! Deterministic: f32 / f64 sums in fixed orders, no libm (the suns' trigonometry is dmath's, in Python), every
//! texel's result a function of the data within the halo (the sweep's heights capped at reach x tan(elevation)).

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3, PyReadwriteArray3};
use pyo3::prelude::*;

use crate::par;

/// one sun: (east, north) of the unit horizontal vector towards it, tan / sin / cos of its elevation
#[derive(Clone, Copy)]
struct Sun {
    se: f32,
    sn: f32,
    tan: f32,
    sin: f32,
    cos: f32,
}

fn sun_of(row: &[f32]) -> Sun {
    Sun { se: row[0], sn: row[1], tan: row[2].max(0.0), sin: row[3], cos: row[4] }
}

/// m of terrain over the ray to the sun for a full shadow (the DEM's noise, the soft terminator)
const SOFT_T: f32 = 4.0;
/// m of shadow volume over a receiver for a full building / canopy shadow, and the first part ignored
const SOFT_B: f32 = 1.5;
const LIFT_B: f32 = 0.25;
/// the slope shading only partly trusted (bare-earth DEM, registration: deshadow.py's), and capped
const LAM_STRENGTH: f32 = 0.7;
const LAM_CAP: f32 = 1.6;
/// the total gain's cap; past G_REC (or darker than LOST_LUM) a shadowed texel is not recoverable
const GMAX: f32 = 4.0;
const G_REC: f32 = 3.0;
const LOST_LUM: u32 = 10;
/// the box blur inside a shadow at full strength (gain 3 and over)
const DENOISE: f32 = 0.6;
/// the image's lit reference: cells of REF_CELL texels on the global grid, the same kind's lit texels REF_R cells round
/// (at least REF_MIN of them), else REF_R2 cells round (within the halo: every block alike); none: not relit
const REF_CELL: usize = 8;
const REF_R: usize = 2;
const REF_R2: usize = 12;
const REF_MIN: u64 = 16;

#[inline]
fn luma(p: [u8; 3]) -> u32 {
    (54 * p[0] as u32 + 183 * p[1] as u32 + 19 * p[2] as u32 + 128) >> 8
}

/// The shadow volume of heights h (rows, cols; m above the ground, row 0 north, texel t m) for sun s: z(p) =
/// max(h(p), z(one step towards the sun) - tan x the step's length, 0), the step's neighbour linearly interpolated,
/// outside the grid 0. Heights capped at reach x tan: an occluder's shadow reaches at most `reach` m along the sun's
/// direction (and as far sideways), so a texel's value depends on the heights within ~1.42 reach of it only.
fn sweep(h: &[f32], rows: usize, cols: usize, t: f32, s: Sun, reach: f32) -> Vec<f32> {
    let cap = reach * s.tan;
    let hc = |i: usize| if h[i].is_finite() { h[i].min(cap).max(0.0) } else { 0.0 };
    let mut z = vec![0f32; rows * cols];
    if s.se.abs() < 1e-6 && s.sn.abs() < 1e-6 {
        for (i, v) in z.iter_mut().enumerate() {
            *v = hc(i);
        }
        return z;
    }
    if s.se.abs() >= s.sn.abs() {
        // one column towards the sun per step (east: the next column), its row offset dr
        let dc: i64 = if s.se > 0.0 { 1 } else { -1 };
        let dr = -s.sn / s.se.abs();
        let drop = s.tan * t * (1.0 + dr * dr).sqrt();
        let order: Vec<usize> = if dc > 0 { (0..cols).rev().collect() } else { (0..cols).collect() };
        for c in order {
            let cp = c as i64 + dc;
            for r in 0..rows {
                let i = r * cols + c;
                let mut v = hc(i);
                if cp >= 0 && (cp as usize) < cols {
                    let y = r as f32 + dr;
                    let y0 = y.floor();
                    let f = y - y0;
                    let a = y0 as i64;
                    let get = |rr: i64| if rr >= 0 && (rr as usize) < rows { z[rr as usize * cols + cp as usize] } else { 0.0 };
                    let p = get(a) * (1.0 - f) + get(a + 1) * f;
                    v = v.max(p - drop);
                }
                z[i] = v;
            }
        }
    } else {
        // one row towards the sun per step (north: the row before), its column offset dc
        let dr: i64 = if s.sn > 0.0 { -1 } else { 1 };
        let dc = s.se / s.sn.abs();
        let drop = s.tan * t * (1.0 + dc * dc).sqrt();
        let order: Vec<usize> = if dr < 0 { (0..rows).collect() } else { (0..rows).rev().collect() };
        for r in order {
            let rp = r as i64 + dr;
            for c in 0..cols {
                let i = r * cols + c;
                let mut v = hc(i);
                if rp >= 0 && (rp as usize) < rows {
                    let x = c as f32 + dc;
                    let x0 = x.floor();
                    let f = x - x0;
                    let a = x0 as i64;
                    let get = |cc: i64| if cc >= 0 && (cc as usize) < cols { z[rp as usize * cols + cc as usize] } else { 0.0 };
                    let p = get(a) * (1.0 - f) + get(a + 1) * f;
                    v = v.max(p - drop);
                }
                z[i] = v;
            }
        }
    }
    z
}

#[inline]
fn bilinear(g: &[f32], rows: usize, cols: usize, y: f32, x: f32) -> f32 {
    let (y0, x0) = (y.floor(), x.floor());
    let (fy, fx) = (y - y0, x - x0);
    let (r0, c0) = (y0 as usize, x0 as usize);
    let (r1, c1) = ((r0 + 1).min(rows - 1), (c0 + 1).min(cols - 1));
    let top = g[r0 * cols + c0] * (1.0 - fx) + g[r0 * cols + c1] * fx;
    let bot = g[r1 * cols + c0] * (1.0 - fx) + g[r1 * cols + c1] * fx;
    top * (1.0 - fy) + bot * fy
}

/// (alpha, lam) at the nodes of a DEM grid (rows, cols, res m apart, row 0 north): alpha the terrain's cast shadow
/// (the most ground over the ray to the sun within `steps` nodes, soft over SOFT_T), lam the Lambert term of the
/// node's normal (central differences).
fn terrain(hn: &[f32], rows: usize, cols: usize, res: f32, s: Sun, steps: usize) -> (Vec<f32>, Vec<f32>) {
    let threads = par::threads_for(rows * cols * steps.max(1) / 16, 65536);
    let mut out = vec![[0f32; 2]; rows * cols];
    par::rows(&mut out, cols, threads, |r, row| {
        for (c, o) in row.iter_mut().enumerate() {
            let h = hn[r * cols + c];
            let mut ex = f32::NEG_INFINITY;
            for k in 1..=steps {
                let y = r as f32 - k as f32 * s.sn;
                let x = c as f32 + k as f32 * s.se;
                if y < 0.0 || x < 0.0 || y > (rows - 1) as f32 || x > (cols - 1) as f32 {
                    break;
                }
                let e = bilinear(hn, rows, cols, y, x) - (h + k as f32 * res * s.tan);
                if e > ex {
                    ex = e;
                }
            }
            let alpha = if ex > 0.0 { (ex / SOFT_T).min(1.0) } else { 0.0 };
            let (cl, cr) = (c.saturating_sub(1), (c + 1).min(cols - 1));
            let (ru, rd) = (r.saturating_sub(1), (r + 1).min(rows - 1));
            let hx = (hn[r * cols + cr] - hn[r * cols + cl]) / ((cr - cl).max(1) as f32 * res);
            let hy = (hn[ru * cols + c] - hn[rd * cols + c]) / ((rd - ru).max(1) as f32 * res);
            let len = (hx * hx + hy * hy + 1.0).sqrt();
            let lam = ((-hx * s.se * s.cos - hy * s.sn * s.cos + s.sin) / len).max(0.0);
            *o = [alpha, lam];
        }
    });
    (out.iter().map(|v| v[0]).collect(), out.iter().map(|v| v[1]).collect())
}

/// Otsu's threshold of 8-bit values (counts per value), the lowest best.
fn otsu(hist: &[u64; 256]) -> u32 {
    let total: u64 = hist.iter().sum();
    let sum_all: f64 = hist.iter().enumerate().map(|(v, &n)| v as f64 * n as f64).sum();
    let (mut wb, mut sb, mut best, mut thr) = (0u64, 0f64, -1f64, 0u32);
    for (v, &n) in hist.iter().enumerate() {
        wb += n;
        if wb == 0 {
            continue;
        }
        let wf = total - wb;
        if wf == 0 {
            break;
        }
        sb += v as f64 * n as f64;
        let (mb, mf) = (sb / wb as f64, (sum_all - sb) / wf as f64);
        let between = wb as f64 * wf as f64 * (mb - mf) * (mb - mf);
        if between > best {
            best = between;
            thr = v as u32;
        }
    }
    thr
}

/// Channel sums (r, g, b, count) of a shadow's interior (`shadow` and its 4 neighbours) and of the lit band round it
/// (not shadow, within 2 cells of one), over `ok` cells: the shadow / lit ratio k of the same surfaces either side.
fn edge_sums(rgb: &[[u8; 3]], ok: &[bool], shadow: &[bool], rows: usize, cols: usize) -> [i64; 8] {
    let mut s = [0i64; 8];
    for r in 0..rows {
        for c in 0..cols {
            let i = r * cols + c;
            if !ok[i] || rgb[i] == [0, 0, 0] {
                continue;
            }
            if shadow[i] {
                let inner = r > 0 && c > 0 && r + 1 < rows && c + 1 < cols && shadow[i - 1] && shadow[i + 1]
                    && shadow[i - cols] && shadow[i + cols];
                if inner {
                    for ch in 0..3 {
                        s[ch] += rgb[i][ch] as i64;
                    }
                    s[3] += 1;
                }
            } else {
                let mut near = false;
                'o: for y in r.saturating_sub(2)..(r + 3).min(rows) {
                    for x in c.saturating_sub(2)..(c + 3).min(cols) {
                        if shadow[y * cols + x] {
                            near = true;
                            break 'o;
                        }
                    }
                }
                if near {
                    for ch in 0..3 {
                        s[4 + ch] += rgb[i][ch] as i64;
                    }
                    s[7] += 1;
                }
            }
        }
    }
    s
}

fn rgb_vec(a: &numpy::ndarray::ArrayView3<'_, u8>) -> Vec<[u8; 3]> {
    let (h, w) = (a.shape()[0], a.shape()[1]);
    let mut v = Vec::with_capacity(h * w);
    for r in 0..h {
        for c in 0..w {
            v.push([a[[r, c, 0]], a[[r, c, 1]], a[[r, c, 2]]]);
        }
    }
    v
}

/// The capture sun of one data cell, scored for each candidate sun (cands (k, 5): se, sn, tan, sin, cos):
/// - terrain (dem (rows, cols) m at res_t m, pad_n nodes round the cell; rgb_t (rows - 2 pad, cols - 2 pad, 3) the
///   photo at the core nodes): Pearson's r of the luminance against lam x (1 - alpha);
/// - buildings and canopy (occ (n, n) m above the ground at res_b; rgb_b (n, n, 3)): the Matthews correlation of their
///   predicted shadows on the open ground (occ < 0.5 m) with its dark texels (Otsu), and the shadow's texels.
/// Returns scores (k, 4): r_t, mcc_b, shadow texels, ground texels (NaN where not scored); and, with measure (one
/// candidate), sums (2, 8): edge_sums of the buildings' shadows (row 0) and the terrain's (row 1).
#[pyfunction]
#[pyo3(signature = (dem, res_t, pad_n, rgb_t, occ, res_b, rgb_b, cands, reach, steps, measure))]
#[allow(clippy::too_many_arguments)]
pub fn gg_sun_fit<'py>(py: Python<'py>, dem: Option<PyReadonlyArray2<'py, f32>>, res_t: f64, pad_n: usize,
                       rgb_t: Option<PyReadonlyArray3<'py, u8>>, occ: Option<PyReadonlyArray2<'py, f32>>, res_b: f64,
                       rgb_b: Option<PyReadonlyArray3<'py, u8>>, cands: PyReadonlyArray2<'py, f32>, reach: f64,
                       steps: usize, measure: bool)
                       -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>)> {
    let bad = |m: &str| Err(pyo3::exceptions::PyValueError::new_err(format!("gg_sun_fit: {m}")));
    let cands = cands.as_array();
    if cands.ndim() != 2 || cands.shape()[1] < 5 {
        return bad("cands (k, 5)");
    }
    let suns: Vec<Sun> = (0..cands.shape()[0]).map(|i| sun_of(&cands.row(i).to_vec())).collect();
    let terr = match (&dem, &rgb_t) {
        (Some(d), Some(p)) => {
            let (d, p) = (d.as_array(), p.as_array());
            let (rows, cols) = (d.shape()[0], d.shape()[1]);
            if rows < 2 * pad_n + 2 || cols < 2 * pad_n + 2 || p.shape()[0] != rows - 2 * pad_n
                || p.shape()[1] != cols - 2 * pad_n || p.shape()[2] < 3 {
                return bad("dem / rgb_t shapes");
            }
            Some((d.iter().copied().collect::<Vec<f32>>(), rows, cols, rgb_vec(&p)))
        }
        _ => None,
    };
    let bld = match (&occ, &rgb_b) {
        (Some(o), Some(p)) => {
            let (o, p) = (o.as_array(), p.as_array());
            let (rows, cols) = (o.shape()[0], o.shape()[1]);
            if p.shape()[0] != rows || p.shape()[1] != cols || p.shape()[2] < 3 {
                return bad("occ / rgb_b shapes");
            }
            Some((o.iter().copied().collect::<Vec<f32>>(), rows, cols, rgb_vec(&p)))
        }
        _ => None,
    };
    let (scores, sums) = py.detach(|| {
        let mut scores = vec![[f64::NAN; 4]; suns.len()];
        let mut sums = [[0i64; 8]; 2];
        // the open ground's dark threshold (one for every candidate)
        let thr = bld.as_ref().map(|(o, _, _, p)| {
            let mut hist = [0u64; 256];
            for (i, &h) in o.iter().enumerate() {
                if h < 0.5 && p[i] != [0, 0, 0] {
                    hist[luma(p[i]).min(255) as usize] += 1;
                }
            }
            otsu(&hist)
        });
        for (k, &s) in suns.iter().enumerate() {
            if let Some((d, rows, cols, p)) = &terr {
                let (alpha, lam) = terrain(d, *rows, *cols, res_t as f32, s, steps);
                let (cr, cc) = (rows - 2 * pad_n, cols - 2 * pad_n);
                let (mut n, mut sx, mut sy, mut sxx, mut syy, mut sxy) = (0f64, 0f64, 0f64, 0f64, 0f64, 0f64);
                for r in 0..cr {
                    for c in 0..cc {
                        let q = p[r * cc + c];
                        if q == [0, 0, 0] {
                            continue;
                        }
                        let j = (r + pad_n) * cols + c + pad_n;
                        let x = (lam[j] * (1.0 - alpha[j])) as f64;
                        let y = luma(q) as f64;
                        n += 1.0;
                        sx += x;
                        sy += y;
                        sxx += x * x;
                        syy += y * y;
                        sxy += x * y;
                    }
                }
                let vx = n * sxx - sx * sx;
                let vy = n * syy - sy * sy;
                scores[k][0] = if n > 16.0 && vx > 0.0 && vy > 0.0 { (n * sxy - sx * sy) / (vx * vy).sqrt() } else { 0.0 };
                if measure {
                    let mut ok = vec![false; cr * cc];
                    let mut sh = vec![false; cr * cc];
                    let mut core = vec![[0u8; 3]; cr * cc];
                    for r in 0..cr {
                        for c in 0..cc {
                            let j = (r + pad_n) * cols + c + pad_n;
                            ok[r * cc + c] = alpha[j] >= 0.99 || alpha[j] == 0.0;
                            sh[r * cc + c] = alpha[j] >= 0.5;
                            core[r * cc + c] = p[r * cc + c];
                        }
                    }
                    sums[1] = edge_sums(&core, &ok, &sh, cr, cc);
                }
            }
            if let Some((o, rows, cols, p)) = &bld {
                let z = sweep(o, *rows, *cols, res_b as f32, s, reach as f32);
                let thr = thr.unwrap_or(0);
                let (mut tp, mut fp, mut fneg, mut tn) = (0f64, 0f64, 0f64, 0f64);
                let mut sh = vec![false; rows * cols];
                let mut ground = vec![false; rows * cols];
                for i in 0..rows * cols {
                    if !(o[i] < 0.5) || p[i] == [0, 0, 0] {
                        continue;
                    }
                    ground[i] = true;
                    let shadow = z[i] - LIFT_B >= 0.5 * SOFT_B;
                    sh[i] = shadow;
                    let dark = luma(p[i]) <= thr;
                    match (shadow, dark) {
                        (true, true) => tp += 1.0,
                        (true, false) => fp += 1.0,
                        (false, true) => fneg += 1.0,
                        (false, false) => tn += 1.0,
                    }
                }
                let den = ((tp + fp) * (tp + fneg) * (tn + fp) * (tn + fneg)).sqrt();
                scores[k][1] = if den > 0.0 { (tp * tn - fp * fneg) / den } else { 0.0 };
                scores[k][2] = tp + fp;
                scores[k][3] = tp + fp + fneg + tn;
                if measure {
                    sums[0] = edge_sums(p, &ground, &sh, *rows, *cols);
                }
            }
        }
        (scores, sums)
    });
    let mut a = numpy::ndarray::Array2::<f64>::zeros((scores.len(), 4));
    for (k, s) in scores.iter().enumerate() {
        for j in 0..4 {
            a[[k, j]] = s[j];
        }
    }
    let mut b = numpy::ndarray::Array2::<i64>::zeros((2, 8));
    for i in 0..2 {
        for j in 0..8 {
            b[[i, j]] = sums[i][j];
        }
    }
    Ok((a.into_pyarray(py), b.into_pyarray(py)))
}

/// A sliding-window dilation of a bool grid by r cells (square), rows then columns.
fn dilate(m: &[bool], rows: usize, cols: usize, r: usize) -> Vec<bool> {
    if r == 0 {
        return m.to_vec();
    }
    let mut a = vec![false; rows * cols];
    for y in 0..rows {
        let mut cnt = 0i64;
        for x in 0..(r.min(cols)) {
            cnt += m[y * cols + x] as i64;
        }
        for x in 0..cols {
            if x + r < cols {
                cnt += m[y * cols + x + r] as i64;
            }
            if x > r {
                cnt -= m[y * cols + x - r - 1] as i64;
            }
            a[y * cols + x] = cnt > 0;
        }
    }
    let mut b = vec![false; rows * cols];
    for x in 0..cols {
        let mut cnt = 0i64;
        for y in 0..(r.min(rows)) {
            cnt += a[y * cols + x] as i64;
        }
        for y in 0..rows {
            if y + r < rows {
                cnt += a[(y + r) * cols + x] as i64;
            }
            if y > r {
                cnt -= a[(y - r - 1) * cols + x] as i64;
            }
            b[y * cols + x] = cnt > 0;
        }
    }
    b
}

/// The square's photo (rgb (n, n, 3), relit in place) with the capture sun's shadows taken out. kind (n, n) the
/// generated ground's kinds (relit only where relit_kinds[kind] != 0: not roofs, crowns, water); occ (n + 2m, n + 2m)
/// the buildings' and canopy's heights over the ground (m) with m texels (shade_margin) round the square; cell (n, n) each texel's sun (an index into suns, 255: none);
/// suns (s, 10): se, sn, tan, sin, cos, terrain (0 off, 1 its cast shadows, 2 and its slope shading), buildings on
/// (0 / 1), k_r, k_g, k_b (shadow / lit ratios);
/// t the texel (m); dem (rows, cols) the ground's heights at the texels (node_r0 + i x node_step, node_c0 + j x
/// node_step) (None: no terrain term); steps its march; reach the sweep's (m). Returns lost (n, n) u8: 1 where a
/// shadowed texel could not be recovered (gen_ground fills it from the kind's lit texels round it).
#[pyfunction]
#[pyo3(signature = (rgb, kind, occ, cell, suns, relit_kinds, t, dem, node_r0, node_c0, node_step, steps, reach, gr,
                    gc))]
#[allow(clippy::too_many_arguments)]
pub fn gg_relight<'py>(py: Python<'py>, mut rgb: PyReadwriteArray3<'py, u8>, kind: PyReadonlyArray2<'py, u8>,
                       occ: PyReadonlyArray2<'py, f32>, cell: PyReadonlyArray2<'py, u8>,
                       suns: PyReadonlyArray2<'py, f32>, relit_kinds: PyReadonlyArray1<'py, u8>, t: f64,
                       dem: Option<PyReadonlyArray2<'py, f32>>, node_r0: i64, node_c0: i64, node_step: usize,
                       steps: usize, reach: f64, gr: i64, gc: i64) -> PyResult<Bound<'py, PyArray2<u8>>> {
    let bad = |m: &str| Err(pyo3::exceptions::PyValueError::new_err(format!("gg_relight: {m}")));
    let mut arr = rgb.as_array_mut();
    let (n, w) = (arr.shape()[0], arr.shape()[1]);
    let (kind, occ, cell, suns, rk) = (kind.as_array(), occ.as_array(), cell.as_array(), suns.as_array(),
                                       relit_kinds.as_array());
    if arr.shape()[2] < 3 || kind.shape() != [n, w] || occ.shape()[0] < n || occ.shape()[0] - n != occ.shape()[1].wrapping_sub(w)
        || (occ.shape()[0] - n) % 2 != 0 || cell.shape() != [n, w]
        || suns.ndim() != 2 || suns.shape()[1] < 10 || rk.len() < 32 || node_step == 0 {
        return bad("shapes");
    }
    let orig: Vec<[u8; 3]> = rgb_vec(&arr.view());
    let kind: Vec<u8> = kind.iter().copied().collect();
    // (occ has m texels more on every side: the sweep's reach from outside the square, so every block alike)
    let (om, ow) = ((occ.shape()[0] - n) / 2, occ.shape()[1]);
    let occ: Vec<f32> = occ.iter().copied().collect();
    let cell: Vec<u8> = cell.iter().copied().collect();
    let rk: Vec<bool> = rk.iter().map(|&v| v != 0).collect();
    let srow: Vec<Vec<f32>> = (0..suns.shape()[0]).map(|i| suns.row(i).to_vec()).collect();
    let dem = dem.map(|d| {
        let a = d.as_array();
        (a.iter().copied().collect::<Vec<f32>>(), a.shape()[0], a.shape()[1])
    });
    let t = t as f32;
    let out = py.detach(|| {
        let nn = n * w;
        let mut geo = vec![0f32; nn];
        let mut lamv = vec![0f32; nn];
        for (si, row) in srow.iter().enumerate() {
            if si >= 255 || !cell.iter().any(|&c| c as usize == si) {
                continue;
            }
            let s = sun_of(row);
            let (use_t, use_b) = (row[5] > 0.5 && dem.is_some(), row[6] > 0.5);
            let zb = if use_b { Some(sweep(&occ, n + 2 * om, ow, t, s, reach as f32)) } else { None };
            let tn = if use_t {
                let (d, rows, cols) = dem.as_ref().unwrap();
                Some(terrain(d, *rows, *cols, node_step as f32 * t, s, steps))
            } else {
                None
            };
            let cap = reach as f32 * s.tan;
            for i in 0..nn {
                if cell[i] as usize != si {
                    continue;
                }
                let mut g = 0f32;
                if let Some(z) = &zb {
                    let j = (i / w + om) * ow + i % w + om;
                    let own = if occ[j].is_finite() { occ[j].min(cap).max(0.0) } else { 0.0 };
                    g = ((z[j] - own - LIFT_B) / SOFT_B).clamp(0.0, 1.0);
                }
                let mut lam = s.sin;
                if let (Some((alpha, lamn)), Some((_, rows, cols))) = (&tn, &dem) {
                    let (r, c) = ((i / w) as i64, (i % w) as i64);
                    let y = ((r - node_r0) as f32 / node_step as f32).clamp(0.0, (*rows - 1) as f32);
                    let x = ((c - node_c0) as f32 / node_step as f32).clamp(0.0, (*cols - 1) as f32);
                    g = g.max(bilinear(alpha, *rows, *cols, y, x));
                    lam = bilinear(lamn, *rows, *cols, y, x);
                }
                geo[i] = g;
                lamv[i] = lam;
            }
        }
        // where a shadow may be: the geometry's, dilated by the registration error (6 m)
        let zone = dilate(&geo.iter().map(|&g| g > 0.0).collect::<Vec<bool>>(), n, w,
                          (6.0 / t).round().max(0.0) as usize);
        // the image's lit reference: per cell of REF_CELL texels (the global grid's: gr, gc) and kind, the lit texels'
        // luminance (sum, count), as summed-area tables of the kinds in the zone
        let (ro, co) = (gr.rem_euclid(REF_CELL as i64) as usize, gc.rem_euclid(REF_CELL as i64) as usize);
        let (cr, cc) = ((n + ro).div_ceil(REF_CELL), (w + co).div_ceil(REF_CELL));
        let mut present = [false; 32];
        for i in 0..nn {
            if (kind[i] as usize) < 32 && zone[i] && cell[i] != 255 {
                present[kind[i] as usize] = true;
            }
        }
        let mut sat: Vec<Option<Vec<(u64, u64)>>> = vec![None; 32];
        for (k, &p) in present.iter().enumerate() {
            if !p || !rk[k] {
                continue;
            }
            let mut cs = vec![(0u64, 0u64); cr * cc];
            for i in 0..nn {
                if kind[i] as usize != k || zone[i] || cell[i] == 255 || orig[i] == [0, 0, 0] {
                    continue;
                }
                let j = ((i / w + ro) / REF_CELL) * cc + (i % w + co) / REF_CELL;
                cs[j].0 += luma(orig[i]) as u64;
                cs[j].1 += 1;
            }
            let mut t2 = vec![(0u64, 0u64); (cr + 1) * (cc + 1)];
            for a in 0..cr {
                for b in 0..cc {
                    let v = cs[a * cc + b];
                    let up = t2[a * (cc + 1) + b + 1];
                    let left = t2[(a + 1) * (cc + 1) + b];
                    let diag = t2[a * (cc + 1) + b];
                    t2[(a + 1) * (cc + 1) + b + 1] = (v.0 + up.0 + left.0 - diag.0, v.1 + up.1 + left.1 - diag.1);
                }
            }
            sat[k] = Some(t2);
        }
        let window = |k: usize, a: usize, b: usize, rad: usize| -> (u64, u64) {
            let t2 = sat[k].as_ref().unwrap();
            let (a0, a1) = (a.saturating_sub(rad), (a + rad + 1).min(cr));
            let (b0, b1) = (b.saturating_sub(rad), (b + rad + 1).min(cc));
            let g = |y: usize, x: usize| t2[y * (cc + 1) + x];
            let (p, q, r_, s_) = (g(a1, b1), g(a0, b1), g(a1, b0), g(a0, b0));
            (p.0 + s_.0 - q.0 - r_.0, p.1 + s_.1 - q.1 - r_.1)
        };
        let threads = par::threads_for(nn, 65536);
        let mut res = vec![[0u8; 4]; nn];
        par::rows(&mut res, w, threads, |r, row| {
            for (c, o) in row.iter_mut().enumerate() {
                let i = r * w + c;
                let p = orig[i];
                *o = [p[0], p[1], p[2], 0];
                let k = kind[i] as usize;
                let si = cell[i] as usize;
                if si >= srow.len() || k >= 32 || !rk[k] || p == [0, 0, 0] {
                    continue;
                }
                let sr = &srow[si];
                let s = sun_of(sr);
                let use_lam = sr[5] > 1.5 && dem.is_some();   // (the slope shading: the terrain's fit's surer tier)
                let kc = [sr[7].clamp(0.05, 0.95), sr[8].clamp(0.05, 0.95), sr[9].clamp(0.05, 0.95)];
                let kl = (54.0 * kc[0] + 183.0 * kc[1] + 19.0 * kc[2]) / 256.0;
                let lum = luma(p);
                let alpha = if zone[i] && sat[k].is_some() {
                    let (a, b) = ((r + ro) / REF_CELL, (c + co) / REF_CELL);
                    let mut rf = window(k, a, b, REF_R);
                    if rf.1 < REF_MIN {
                        rf = window(k, a, b, REF_R2);
                    }
                    if rf.1 >= REF_MIN {
                        let lit = rf.0 as f32 / rf.1 as f32;
                        ((1.0 - lum as f32 / lit.max(1.0)) / (1.0 - kl)).clamp(0.0, 1.0)
                    } else {
                        0.0         // (no lit texel of its kind round it to tell: left as it is)
                    }
                } else {
                    0.0
                };
                let lam = if use_lam { s.sin + LAM_STRENGTH * (lamv[i] - s.sin) } else { s.sin }.max(0.0);
                let mut g = [1f32; 3];
                let mut gmax = 1f32;
                let mut changed = false;
                for ch in 0..3 {
                    let rho = kc[ch] * s.sin / (1.0 - kc[ch]);
                    let gl = ((rho + s.sin) / (rho + lam)).clamp(1.0 / LAM_CAP, LAM_CAP);
                    let gc = (gl * (rho + lam) / (rho + lam * (1.0 - alpha))).min(GMAX);
                    g[ch] = gc;
                    gmax = gmax.max(gc);
                    changed |= (gc - 1.0).abs() > 1e-3;
                }
                if !changed {
                    continue;
                }
                let lost = alpha > 0.5 && (gmax > G_REC || lum < LOST_LUM);
                let mut q = [p[0] as f32, p[1] as f32, p[2] as f32];
                if alpha > 0.0 {
                    // noise-aware: blurred inside the shadow by its gain, before the gain
                    let wgt = ((gmax - 1.0) / 2.0).clamp(0.0, 1.0) * DENOISE;
                    if wgt > 0.0 {
                        let (mut acc, mut m) = ([0u32; 3], 0u32);
                        for y in r.saturating_sub(1)..(r + 2).min(n) {
                            for x in c.saturating_sub(1)..(c + 2).min(w) {
                                let v = orig[y * w + x];
                                for ch in 0..3 {
                                    acc[ch] += v[ch] as u32;
                                }
                                m += 1;
                            }
                        }
                        for ch in 0..3 {
                            q[ch] += wgt * (acc[ch] as f32 / m as f32 - q[ch]);
                        }
                    }
                }
                for ch in 0..3 {
                    o[ch] = (q[ch] * g[ch]).clamp(0.0, 255.0).round() as u8;
                }
                o[3] = lost as u8;
            }
        });
        res
    });
    let mut lost = numpy::ndarray::Array2::<u8>::zeros((n, w));
    for r in 0..n {
        for c in 0..w {
            let v = out[r * w + c];
            arr[[r, c, 0]] = v[0];
            arr[[r, c, 1]] = v[1];
            arr[[r, c, 2]] = v[2];
            lost[[r, c]] = v[3];
        }
    }
    Ok(lost.into_pyarray(py))
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(gg_sun_fit, m)?)?;
    m.add_function(wrap_pyfunction!(gg_relight, m)?)?;
    Ok(())
}
