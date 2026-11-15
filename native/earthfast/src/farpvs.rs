//! The far field's potentially visible set (the retro study's item C: Quake's PVS / Doom's REJECT for far textures):
//! which parts of the anchor's plane can be seen from a set of viewpoints (the player's terrain block and a look-ahead
//! round it, each eye at its ground plus a height), from coarse elevation rasters. A far tile whose footprint holds no
//! visible cell is hidden behind terrain from all of them and keeps a small texture (earthd's far PVS) until it is not.
//!
//! - far_pvs: the visibility of each ring's cells, in blocks of gran x gran cells, from each viewpoint.
//!
//! Conservative where it can be: the occluders come from the rings' min rasters with the unrefracted earth's drop (lower
//! than the ground), the targets from the max rasters with the refracted earth's (higher), so a cell is hidden only when
//! terrain that is surely there hides all the ground that may be there; a cell outside every ring is never hidden (the
//! caller treats a tile not wholly inside them as visible).
use crate::dm::Dm;

use numpy::ndarray::Array3;
use numpy::{IntoPyArray, PyArray3, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// One ring: its (min, max) heights interleaved ([j * n + i], row j north), its south-west corner and cell size.
struct Ring {
    lohi: Vec<[f32; 2]>,
    x0: f64,
    y0: f64,
    cell: f64,
    n: usize,
}

/// The visibility of every ring's cells in blocks of `gran` cells (one byte a block, 1: some cell of it seen) from each
/// of the viewpoints views (k, 3: x, y, eye height above sea level): per ring a (k, blocks, blocks) array. For each
/// viewpoint `azimuths` rays, each marched through the rings in order (ring r while the ray is surely inside it, then
/// the next; steps of the ring's cell), from d0 m out to rmax m, with the running horizon (the highest occluder angle so
/// far) and an early end once nothing in reach can rise above it (zmax: the highest target anywhere). rings: (lo, hi,
/// x0, y0, cell): n x n rasters of the min / max heights above sea level of the cells [x0 + i cell, x0 + (i + 1) cell)
/// x [y0 + j cell, ...) (row j north), nested (each the next's middle). r_occ / r_tgt: the earth's radius the
/// occluders' / targets' drop goes by (d^2 / 2r).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn far_pvs<'py>(py: Python<'py>, rings: Vec<(PyReadonlyArray2<'py, f32>, PyReadonlyArray2<'py, f32>, f64, f64, f64)>,
                    views: PyReadonlyArray2<'py, f64>, azimuths: usize, gran: usize, d0: f64, rmax: f64, r_occ: f64,
                    r_tgt: f64) -> PyResult<Vec<Bound<'py, PyArray3<u8>>>> {
    if rings.is_empty() || gran == 0 || azimuths == 0 {
        return Err(PyValueError::new_err("far_pvs: rings, gran > 0 and azimuths > 0"));
    }
    let mut rs = Vec::with_capacity(rings.len());
    for (lo, hi, x0, y0, cell) in rings.iter() {
        let (l, h) = (lo.as_array(), hi.as_array());
        let n = l.nrows();
        if l.ncols() != n || h.shape() != l.shape() || !(*cell > 0.0) {
            return Err(PyValueError::new_err("far_pvs: square lo / hi rasters of the same size and a cell > 0"));
        }
        let lohi = l.iter().zip(h.iter()).map(|(&a, &b)| [a, b]).collect();
        rs.push(Ring { lohi, x0: *x0, y0: *y0, cell: *cell, n });
    }
    let v = views.as_array();
    if v.ncols() != 3 {
        return Err(PyValueError::new_err("far_pvs: views (k, 3): x, y, eye"));
    }
    let views: Vec<[f64; 3]> = v.rows().into_iter().map(|r| [r[0], r[1], r[2]]).collect();
    let out = py.detach(|| run(&rs, &views, azimuths, gran, d0, rmax, r_occ, r_tgt));
    Ok(out.into_iter().map(|a| a.into_pyarray(py)).collect())
}

#[allow(clippy::too_many_arguments)]
fn run(rs: &[Ring], views: &[[f64; 3]], azimuths: usize, gran: usize, d0: f64, rmax: f64, r_occ: f64, r_tgt: f64)
       -> Vec<Array3<u8>> {
    let gns: Vec<usize> = rs.iter().map(|r| r.n.div_ceil(gran)).collect();
    let mut vis: Vec<Array3<u8>> = gns.iter().map(|&g| Array3::zeros((views.len(), g, g))).collect();
    let zmax = rs.iter().flat_map(|r| r.lohi.iter().map(|p| p[1])).fold(f32::MIN, f32::max) as f64;
    let (ko, kt) = (0.5 / r_occ, 0.5 / r_tgt);
    let dirs: Vec<(f64, f64)> = (0..azimuths).map(|k| {
        let a = (k as f64 + 0.5) * std::f64::consts::TAU / azimuths as f64;
        (a.dcos(), a.dsin())
    }).collect();
    let last = rs.len() - 1;
    for (vi, &[px, py, eye]) in views.iter().enumerate() {
        // how far each inner ring surely holds a ray from this viewpoint (its edge's nearest distance, less a cell;
        // the next ring goes on from there); the last one: to where the ray leaves it (or rmax)
        let cuts: Vec<f64> = rs.iter().map(|r| {
            let w = r.n as f64 * r.cell;
            (px - r.x0).min(r.x0 + w - px).min(py - r.y0).min(r.y0 + w - py) - r.cell
        }).collect();
        // the highest a target can reach at d or beyond: a / d - d kt with a = zmax - eye, falling with d for a > 0;
        // for a <= 0 rising to its peak -2 sqrt(-a kt) at d = sqrt(-a / kt), then falling
        let a = zmax - eye;
        let (peak_d, peak) = if a <= 0.0 { ((-a / kt).sqrt(), -2.0 * (-a * kt).sqrt()) } else { (0.0, 0.0) };
        let mut outs: Vec<_> = vis.iter_mut().map(|g| g.index_axis_mut(numpy::ndarray::Axis(0), vi)).collect();
        for &(c, s) in &dirs {
            let mut hor = f64::NEG_INFINITY;
            let mut d = d0;
            'ray: for (k, r) in rs.iter().enumerate() {
                let cut = if k == last {
                    let w = r.n as f64 * r.cell;
                    let exit = |p: f64, lo: f64, u: f64| {
                        if u > 1e-12 { (lo + w - p) / u } else if u < -1e-12 { (lo - p) / u } else { f64::INFINITY }
                    };
                    exit(px, r.x0, c).min(exit(py, r.y0, s)).min(rmax)
                } else {
                    cuts[k]
                };
                let g = outs[k].as_slice_mut().expect("a fresh standard-layout array");
                let gn = gns[k];
                let inv = 1.0 / r.cell;
                let (fx, fy) = ((px - r.x0) * inv, (py - r.y0) * inv);
                let n = r.n;
                while d < cut {
                    let id = 1.0 / d;
                    let bound = if a <= 0.0 && d < peak_d { peak } else { a * id - d * kt };
                    if hor > bound {
                        break 'ray;             // nothing farther can rise above the horizon
                    }
                    let i = (fx + c * d * inv).floor();
                    let j = (fy + s * d * inv).floor();
                    if i >= 0.0 && j >= 0.0 && (i as usize) < n && (j as usize) < n {
                        let (i, j) = (i as usize, j as usize);
                        let [lo, hi] = r.lohi[j * n + i];
                        if (hi as f64 - eye) * id - d * kt > hor {
                            g[(j / gran) * gn + i / gran] = 1;
                        }
                        let o = (lo as f64 - eye) * id - d * ko;
                        if o > hor {
                            hor = o;
                        }
                    }
                    d += r.cell;
                }
            }
        }
    }
    vis
}

// --- a set's texture sizes (farpvs.Set.tex_px / hidden) ---------------------------------------------------------------

/// Python's max / min of two floats (the first wins a tie; NaN never replaces an earlier value).
fn pmax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

fn pmin(a: f64, b: f64) -> f64 {
    if b < a { b } else { a }
}

fn floor_i(v: f64) -> i64 {
    v.floor() as i64
}

/// One ring's lowest ground and steepest slope rasters (farpvs.Rings: lo and slopes, n x n, row j north).
struct GroundRing<'a> {
    lo: numpy::ndarray::ArrayView2<'a, f32>,
    sl: numpy::ndarray::ArrayView2<'a, f32>,
    x0: f64,
    y0: f64,
    cell: f64,
}

/// The rules of farpvs.Set.tex_px (the module's constants).
struct TexRules {
    min_level: i64,
    near_m: f64,
    slope_min_level: i64,
    slope_near_m: f64,
    oversample: f64,
    aniso: f64,
    r_occ: f64,
    slope_px_min: i64,
    tex_px: i64,
    slope_sizing: bool,
    quantile: f64,
}

/// numpy's max of a float32 window (NaN spreads), or the linear quantile q of it (np.quantile's default method).
fn window_stat(w: numpy::ndarray::ArrayView2<'_, f32>, q: f64, take_max: bool) -> f64 {
    if take_max || q >= 1.0 {
        let mut m = f32::NEG_INFINITY;
        for &v in w.iter() {
            if v.is_nan() {
                return f64::NAN;
            }
            if v > m {
                m = v;
            }
        }
        return m as f64;
    }
    let mut v: Vec<f64> = w.iter().map(|&x| x as f64).collect();
    if v.iter().any(|x| x.is_nan()) {
        return f64::NAN;
    }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let pos = q.max(0.0) * (v.len() - 1) as f64;
    let (k, t) = (pos.floor() as usize, pos - pos.floor());
    let (a, b) = (v[k], v[(k + 1).min(v.len() - 1)]);
    if t >= 0.5 { b - (b - a) * (1.0 - t) } else { a + (b - a) * t }
}

fn window_min(w: numpy::ndarray::ArrayView2<'_, f32>) -> f64 {
    let mut m = f32::INFINITY;
    for &v in w.iter() {
        if v.is_nan() {
            return f64::NAN;
        }
        if v < m {
            m = v;
        }
    }
    m as f64
}

/// Rings.tile_ground: (lowest ground, steepest slope) under a footprint (a cell round it) from the finest ring holding
/// it whole, or None.
fn tile_ground(rings: &[GroundRing<'_>], b: &[f64; 4], q: f64) -> Option<(f64, f64)> {
    use numpy::ndarray::s;
    for r in rings {
        let n = r.lo.nrows() as i64;
        let (i0, j0) = (floor_i((b[0] - r.x0) / r.cell) - 1, floor_i((b[1] - r.y0) / r.cell) - 1);
        let (i1, j1) = (floor_i((b[2] - r.x0) / r.cell) + 1, floor_i((b[3] - r.y0) / r.cell) + 1);
        if i0 < 0 || j0 < 0 || i1 >= n || j1 >= n {
            continue;
        }
        let (i0, j0, i1, j1) = (i0 as usize, j0 as usize, i1 as usize + 1, j1 as usize + 1);
        let slope = window_stat(r.sl.slice(s![j0..j1, i0..i1]), q, false);
        return Some((window_min(r.lo.slice(s![j0..j1, i0..i1])), slope));
    }
    None
}

/// The set (farpvs.Set): its square, the rings' reach, the highest the eye can be in the square (None: no ground
/// under it), the screen's pixels per radian, and whether no viewpoint fell in the rings.
struct SetArgs {
    cx: f64,
    cy: f64,
    half: f64,
    reach: f64,
    eye_top: Option<f64>,
    ppr: f64,
    empty: bool,
}

/// Set._hidden: no visible block over the footprint (grown by one block) in any ring's summed-area table.
fn hidden(sats: &[(numpy::ndarray::ArrayView2<'_, i32>, f64, f64, f64, i64)], st: &SetArgs, rules: &TexRules,
          b: &[f64; 4], level: i64) -> bool {
    if level < rules.min_level || st.empty {
        return false;
    }
    let (x0, y0, x1, y1) = (b[0], b[1], b[2], b[3]);
    let r = st.half + rules.near_m;
    if x0 < st.cx + r && x1 > st.cx - r && y0 < st.cy + r && y1 > st.cy - r {
        return false;
    }
    if x0 < -st.reach || y0 < -st.reach || x1 > st.reach || y1 > st.reach {
        return false;
    }
    for (sat, rx0, ry0, bs, n) in sats {
        let i0 = 0.max(floor_i((x0 - rx0) / bs) - 1);
        let j0 = 0.max(floor_i((y0 - ry0) / bs) - 1);
        let i1 = (*n).min(floor_i((x1 - rx0) / bs) + 2);
        let j1 = (*n).min(floor_i((y1 - ry0) / bs) + 2);
        if i0 >= i1 || j0 >= j1 {
            continue;
        }
        let (i0, j0, i1, j1) = (i0 as usize, j0 as usize, i1 as usize, j1 as usize);
        let v = sat[[j1, i1]] as i64 - sat[[j0, i1]] as i64 - sat[[j1, i0]] as i64 + sat[[j0, i0]] as i64;
        if v > 0 {
            return false;
        }
    }
    true
}

/// Set._screen_px: the texture px the screen resolves of a tile from the square (full at most, up to top where it
/// resolves more).
#[allow(clippy::too_many_arguments)]
fn screen_px(rings: &[GroundRing<'_>], st: &SetArgs, rules: &TexRules, b: &[f64; 4], level: i64, size: f64, full: i64,
             top: i64) -> i64 {
    let top = if top != 0 { top } else { full };
    let Some(eye_top) = st.eye_top else { return full };
    let dx = pmax(pmax(b[0] - (st.cx + st.half), (st.cx - st.half) - b[2]), 0.0);
    let dy = pmax(pmax(b[1] - (st.cy + st.half), (st.cy - st.half) - b[3]), 0.0);
    let d = dx.dhypot(dy);
    let near = level < rules.slope_min_level || d < rules.slope_near_m;
    if near && top <= full {
        return full;
    }
    let need = if near {
        d / (st.ppr * rules.oversample)
    } else {
        let Some((zmin, slope)) = tile_ground(rings, b, rules.quantile) else { return full };
        let sin_a = pmin(1.0, pmax(0.0, (eye_top - zmin) / d) + d / (2.0 * rules.r_occ) + slope);
        d / (st.ppr * rules.oversample) * pmax(1.0, 1.0 / (rules.aniso * pmax(sin_a, 1e-6)))
    };
    let mut px = full;
    while px * 2 <= top && size / px as f64 > need {
        px *= 2;
    }
    while !near && px > rules.slope_px_min && size / (px.div_euclid(2)) as f64 <= need {
        px = px.div_euclid(2);
    }
    px
}

/// farpvs.Set.tex_px of many tiles at once: tiles (k, 8) [x0, y0, x1, y1, level, size, full, top] (top: the most
/// the tile may get, 0: full). Returns (px, hidden) per tile. sats: the set's per-ring summed-area tables (sat, x0, y0,
/// block m, blocks); rings: (lo, slopes, x0, y0, cell) per ring, finest first; set: (cx, cy, half, reach, eye_top or
/// None, ppr, empty); rules: (MIN_LEVEL, NEAR_M, SLOPE_MIN_LEVEL, SLOPE_NEAR_M, OVERSAMPLE, ANISO, R_OCC, SLOPE_PX_MIN,
/// TEX_PX, SLOPE_SIZING, SLOPE_QUANTILE). The GIL is released while they are worked out.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn far_pvs_tex_px<'py>(py: Python<'py>, sats: Vec<(PyReadonlyArray2<'py, i32>, f64, f64, f64, i64)>,
                           rings: Vec<(PyReadonlyArray2<'py, f32>, PyReadonlyArray2<'py, f32>, f64, f64, f64)>,
                           set: (f64, f64, f64, f64, Option<f64>, f64, bool),
                           rules: (i64, f64, i64, f64, f64, f64, f64, i64, i64, bool, f64),
                           tiles: PyReadonlyArray2<'py, f64>) -> PyResult<(Vec<i64>, Vec<bool>)> {
    let t = tiles.as_array();
    if t.ncols() != 8 {
        return Err(PyValueError::new_err("far_pvs_tex_px: tiles (k, 8)"));
    }
    for (lo, sl, ..) in &rings {
        if lo.as_array().shape() != sl.as_array().shape() {
            return Err(PyValueError::new_err("far_pvs_tex_px: lo and slope rasters of the same size"));
        }
    }
    for (sat, _, _, _, n) in &sats {
        let a = sat.as_array();
        let s = a.shape();
        if *n < 0 || s[0] < *n as usize + 1 || s[1] < *n as usize + 1 {
            return Err(PyValueError::new_err("far_pvs_tex_px: summed-area tables (n + 1, n + 1)"));
        }
    }
    let sv: Vec<_> = sats.iter().map(|(a, x0, y0, b, n)| (a.as_array(), *x0, *y0, *b, *n)).collect();
    let rv: Vec<GroundRing<'_>> = rings.iter().map(|(lo, sl, x0, y0, cell)| GroundRing {
        lo: lo.as_array(), sl: sl.as_array(), x0: *x0, y0: *y0, cell: *cell }).collect();
    let st = SetArgs { cx: set.0, cy: set.1, half: set.2, reach: set.3, eye_top: set.4, ppr: set.5, empty: set.6 };
    let rules = TexRules { min_level: rules.0, near_m: rules.1, slope_min_level: rules.2, slope_near_m: rules.3,
                           oversample: rules.4, aniso: rules.5, r_occ: rules.6, slope_px_min: rules.7, tex_px: rules.8,
                           slope_sizing: rules.9, quantile: rules.10 };
    let rows: Vec<[f64; 8]> = t.rows().into_iter().map(|r| [r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]]).collect();
    // (the views borrow numpy buffers: they are read-only and outlive the call, so the GIL can go meanwhile)
    let work = || {
        let mut px = Vec::with_capacity(rows.len());
        let mut hid = Vec::with_capacity(rows.len());
        for r in &rows {
            let b = [r[0], r[1], r[2], r[3]];
            let (level, size, full, top) = (r[4] as i64, r[5], r[6] as i64, r[7] as i64);
            let h = hidden(&sv, &st, &rules, &b, level);
            let mut p = if rules.slope_sizing { screen_px(&rv, &st, &rules, &b, level, size, full, top) } else { full };
            if h {
                p = p.min(rules.tex_px);
            }
            px.push(p);
            hid.push(h);
        }
        (px, hid)
    };
    Ok(if rows.len() > 64 { py.detach(work) } else { work() })
}

#[cfg(test)]
mod tests {
    use super::*;
    use numpy::ndarray::Array2;

    fn ring(n: usize, cell: f64, f: impl Fn(f64, f64) -> f32) -> (Array2<f32>, f64, f64, f64) {
        let x0 = -(n as f64) * cell / 2.0;
        let a = Array2::from_shape_fn((n, n), |(j, i)| f(x0 + (i as f64 + 0.5) * cell, x0 + (j as f64 + 0.5) * cell));
        (a, x0, x0, cell)
    }

    /// The OR of the views' visibility (one ring, its blocks).
    fn vis_of(lo: &Array2<f32>, x0: f64, cell: f64, views: &[[f64; 3]], gran: usize) -> Array2<u8> {
        let n = lo.nrows();
        let lohi = lo.iter().map(|&v| [v, v]).collect();
        let rs = vec![Ring { lohi, x0, y0: x0, cell, n }];
        let v = run(&rs, views, 1024, gran, 50.0, 1e9, 6.371e6, 7.3e6).remove(0);
        v.fold_axis(numpy::ndarray::Axis(0), 0u8, |&acc, &b| acc | b)
    }

    #[test]
    fn flat_ground_all_seen_within_the_horizon() {
        let (a, x0, _, cell) = ring(200, 100.0, |_, _| 0.0);
        // an eye at 1000 m over a flat sea sees ~113 km: the whole 20 km square (in blocks of 2 x 2 cells: a ray
        // stepping a cell at a time can pass a cell's corner without a sample in it)
        let v = vis_of(&a, x0, cell, &[[0.0, 0.0, 1000.0]], 2);
        assert_eq!(v.iter().filter(|&&b| b == 0).count(), 0);
        // ... and from 2 m: the sea's horizon is ~5 km away, the 1 km round the eye all seen
        let v = vis_of(&a, x0, cell, &[[0.0, 0.0, 2.0]], 2);
        assert!(v.slice(numpy::ndarray::s![45..55, 45..55]).iter().all(|&b| b == 1));
        assert_eq!(v[[50, 2]], 0);              // 9.5 km away: below it
    }

    #[test]
    fn a_wall_hides_what_is_behind_it() {
        // a 500 m wall at x = 2 km .. 2.2 km, ground at 0 elsewhere; the eye at 2 m at the origin
        let (a, x0, _, cell) = ring(200, 100.0, |x, _| if (2000.0..2200.0).contains(&x) { 500.0 } else { 0.0 });
        let v = vis_of(&a, x0, cell, &[[0.0, 0.0, 2.0]], 1);
        let at = |x: f64, y: f64| v[[((y - x0) / cell) as usize, ((x - x0) / cell) as usize]];
        assert_eq!(at(1000.0, 0.0), 1);         // before the wall
        assert_eq!(at(2050.0, 0.0), 1);         // the wall itself
        assert_eq!(at(6000.0, 0.0), 0);         // behind it
        assert_eq!(at(-6000.0, 0.0), 1);        // the other way
        // a second viewpoint high above sees over it (its own shadow right behind the wall: 2000 m / 2200 m slope)
        let v2 = vis_of(&a, x0, cell, &[[0.0, 0.0, 2.0], [0.0, 0.0, 2000.0]], 1);
        let at2 = |x: f64, y: f64| v2[[((y - x0) / cell) as usize, ((x - x0) / cell) as usize]];
        assert_eq!(at2(6000.0, 0.0), 1);
        assert_eq!(at2(2350.0, 0.0), 0);
    }

    #[test]
    fn granularity_ors_cells_and_views_apart() {
        let (a, x0, _, cell) = ring(200, 100.0, |x, _| if (2000.0..2200.0).contains(&x) { 500.0 } else { 0.0 });
        let v = vis_of(&a, x0, cell, &[[0.0, 0.0, 2.0]], 4);
        assert_eq!(v.nrows(), 50);
        let fine = vis_of(&a, x0, cell, &[[0.0, 0.0, 2.0]], 1);
        for j in 0..50 {
            for i in 0..50 {
                let any = (0..4).any(|dj| (0..4).any(|di| fine[[4 * j + dj, 4 * i + di]] == 1));
                assert_eq!(v[[j, i]] == 1, any);
            }
        }
        // two views in one call: each its own, the same as alone
        let n = a.nrows();
        let rs = vec![Ring { lohi: a.iter().map(|&v| [v, v]).collect(), x0, y0: x0, cell, n }];
        let both = run(&rs, &[[0.0, 0.0, 2.0], [0.0, 0.0, 2000.0]], 1024, 1, 50.0, 1e9, 6.371e6, 7.3e6).remove(0);
        let lone = run(&rs, &[[0.0, 0.0, 2000.0]], 1024, 1, 50.0, 1e9, 6.371e6, 7.3e6).remove(0);
        assert_eq!(both.index_axis(numpy::ndarray::Axis(0), 1), lone.index_axis(numpy::ndarray::Axis(0), 0));
        assert_ne!(both.index_axis(numpy::ndarray::Axis(0), 0), both.index_axis(numpy::ndarray::Axis(0), 1));
    }
}
