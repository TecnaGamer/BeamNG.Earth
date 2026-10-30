//! The far field's global tiles (geotiles phase 3b, docs/V1_4_SPEC.md 3.7 M6 on the geotiles grid,
//! .claude/work/geotiles_plan.md): far levels L >= 3 as tiles of the Web-Mercator quadtree (key m<z>_<x>_<y>), each
//! made once in its own frame (the ENU plane at its centre, the world convention: x, y of a place at h = 0, z at its
//! height) and placed rigidly into any anchor's world by the mod (geotile.lua placement at the tile's pivot), so a
//! rebase builds and sends none of them again. The anchor's own quadtree (L <= 2, which must tile its plane exactly and
//! meet the terrain block) fills the refined finest global tiles; its nodes across that region's edge are clipped to
//! it (pieces, each the node's mesh cut by one global tile's quad; skirts along the cut).
//!
//! - far_global_select: the global part of a far-field selection in an anchor's world (the distance rule on each
//!   tile's real size, hysteresis, holds, forced boxes, the disc / square radius, the horizon cull), and the refined
//!   tiles of the finest global zoom (the region the anchor's tiles fill);
//! - far_box_cover: an anchor node's overlap with each of the region's quads (area and the clipped polygon);
//! - far_overlap_pairs: which adds and removes of a far-field change overlap (the swap groups of mixed keys);
//! - far_global_mesh: a global tile's mesh from its DEM heights on its Mercator grid (positions in its frame, the RTIN
//!   simplification, normals, palette coordinates, the Mercator UV, skirts);
//! - far_clip_mesh: a node's mesh cut by a convex polygon (Sutherland-Hodgman per triangle, a cut vertex interpolated
//!   on the triangle it cuts, so its height is that triangle's exactly), with skirts along the cut.

use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2};
use pyo3::prelude::*;
use std::collections::HashMap;

use crate::geotiles::{geo_to_ecef, lat_of, lon_of, near, quad, quad_dist, centre, Frame};

type P2 = [f64; 2];

/// Signed area (counter-clockwise positive).
pub fn poly_area(p: &[P2]) -> f64 {
    let n = p.len();
    if n < 3 {
        return 0.0;
    }
    let mut a = 0.0;
    for k in 0..n {
        let (u, v) = (p[k], p[(k + 1) % n]);
        a += u[0] * v[1] - v[0] * u[1];
    }
    0.5 * a
}

/// Sutherland-Hodgman: subject clipped by a convex polygon (either orientation).
pub fn clip_convex(subject: &[P2], clip: &[P2]) -> Vec<P2> {
    let s = if poly_area(clip) >= 0.0 { 1.0 } else { -1.0 };
    let mut out: Vec<P2> = subject.to_vec();
    let m = clip.len();
    for k in 0..m {
        if out.is_empty() {
            break;
        }
        let (a, b) = (clip[k], clip[(k + 1) % m]);
        let side = |p: &P2| s * ((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]));
        let inp = std::mem::take(&mut out);
        let n = inp.len();
        for i in 0..n {
            let (p, q) = (inp[i], inp[(i + 1) % n]);
            let (dp, dq) = (side(&p), side(&q));
            if dp >= 0.0 {
                out.push(p);
            }
            if (dp >= 0.0) != (dq >= 0.0) {
                let t = dp / (dp - dq);
                out.push([p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])]);
            }
        }
    }
    out
}

fn bbox(p: &[P2]) -> [f64; 4] {
    let mut b = [f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY];
    for q in p {
        b[0] = b[0].min(q[0]);
        b[1] = b[1].min(q[1]);
        b[2] = b[2].max(q[0]);
        b[3] = b[3].max(q[1]);
    }
    b
}

fn overlaps(a: &[f64; 4], b: &[f64; 4]) -> bool {
    a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3]
}

// --- selection -------------------------------------------------------------------------------------------------------

struct Horizon {
    reach: f64,
    min_d: f64,
    r: f64,
    base: Vec<f64>,
    n: usize,
    half: f64,
    cell: f64,
    /// (zb, x0, y0, rows, cols, maxima): the highest DEM block per Web-Mercator tile of zoom zb from (x0, y0): a
    /// global node's zmax from these is the same for every anchor (the anchor's plane grid's cells are not)
    merc: Option<(u32, i64, i64, usize, usize, Vec<f64>)>,
}

impl Horizon {
    /// The highest terrain under a bbox (the anchor's zmax grid, farfield.HorizonGrid: base[j, i] the L6 node
    /// (i - n/2, j - n/2)); +inf where the bbox reaches past the grid (never culled).
    fn zmax(&self, b: &[f64; 4]) -> f64 {
        if b[0] < -self.half || b[1] < -self.half || b[2] > self.half || b[3] > self.half {
            return f64::INFINITY;
        }
        let ix = |v: f64| (((v + self.half) / self.cell).floor() as i64).clamp(0, self.n as i64 - 1) as usize;
        let mut m = f64::NEG_INFINITY;
        for j in ix(b[1])..=ix(b[3]) {
            for i in ix(b[0])..=ix(b[2]) {
                m = m.max(self.base[j * self.n + i]);
            }
        }
        m
    }

    /// The highest terrain of global node (z, x, y) from the Mercator table (its zoom-zb tiles, one more round them);
    /// None without a table, +inf where it reaches past the table.
    fn zmax_merc(&self, z: u32, x: i64, y: i64) -> Option<f64> {
        let (zb, x0, y0, rows, cols, ref m) = *self.merc.as_ref()?;
        let (lo_x, hi_x, lo_y, hi_y) = if z >= zb {
            let sh = z - zb;
            (x >> sh, x >> sh, y >> sh, y >> sh)
        } else {
            let sh = zb - z;
            (x << sh, ((x + 1) << sh) - 1, y << sh, ((y + 1) << sh) - 1)
        };
        let n = 1i64 << zb;
        let (mut bx0, mut bx1) = (lo_x - 1 - x0, hi_x + 1 - x0);
        // the table's columns may run past the antimeridian (unwrapped round the anchor)
        if bx1 < 0 {
            bx0 += n;
            bx1 += n;
        }
        if bx0 >= cols as i64 {
            bx0 -= n;
            bx1 -= n;
        }
        let (by0, by1) = (lo_y - 1 - y0, hi_y + 1 - y0);
        if bx0 < 0 || by0 < 0 || bx1 >= cols as i64 || by1 >= rows as i64 {
            return Some(f64::INFINITY);
        }
        let mut v = f64::NEG_INFINITY;
        for r in by0 as usize..=by1 as usize {
            for c in bx0 as usize..=bx1 as usize {
                v = v.max(m[r * cols + c]);
            }
        }
        Some(v)
    }

    /// farfield.Horizon.keeps for a node d m away (horizontally) with bbox b.
    fn keeps(&self, d: f64, b: &[f64; 4], node: (u32, i64, i64)) -> bool {
        let zm = self.zmax_merc(node.0, node.1, node.2).unwrap_or_else(|| self.zmax(b));
        d <= self.min_d || d <= self.reach + (2.0 * self.r * zm.max(0.0)).sqrt()
    }
}

fn quad_flat(q: &[[f64; 2]; 4]) -> Vec<f64> {
    q.iter().flatten().copied().collect()
}

fn edge_mean(q: &[[f64; 2]; 4]) -> f64 {
    let mut s = 0.0;
    for k in 0..4 {
        let (a, b) = (q[k], q[(k + 1) % 4]);
        s += (b[0] - a[0]).hypot(b[1] - a[1]);
    }
    s / 4.0
}

/// The global part of a far-field selection for a player at (px, py) of the anchor (lat0, lon0)'s world.
/// zf: the finest global zoom (far level 3); max_level: the coarsest far level (its zoom zf - (max_level - 3) are the
/// roots); k[L]: K of level L (L = 3 + zf - z), rmin: the largest near radius (a switch distance is at least it);
/// h: hysteresis; prev / hold: global nodes (z, x, y) refined last time / whose state must not change now; forced:
/// boxes (x0, y0, x1, y1) under which every node splits; disc: keep nodes nearer than radius (else the square of
/// half-size radius); horizon: (reach, min_d, R', base (n, n), half, cell, merc) the per-node line-of-sight cull (merc:
/// (zb, x0, y0, maxima (rows, cols)) the highest DEM block per Mercator tile of zoom zb, or None: the plane grid).
/// Returns (tiles [(z, x, y, quad (8), real edge m)], refined [(z, x, y)], regions [(x, y, quad (8))]: the refined
/// tiles of zoom zf, which the anchor's own nodes fill).
#[pyfunction]
#[pyo3(signature = (lat0, lon0, px, py, radius, disc, zf, max_level, k, rmin, h, prev, hold, forced, horizon=None))]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn far_global_select<'py>(lat0: f64, lon0: f64, px: f64, py: f64, radius: f64, disc: bool, zf: u32,
                              max_level: i32, k: Vec<f64>, rmin: f64, h: f64, prev: Vec<(u32, i64, i64)>,
                              hold: Vec<(u32, i64, i64)>, forced: Vec<(f64, f64, f64, f64)>,
                              horizon: Option<(f64, f64, f64, PyReadonlyArray2<'py, f64>, f64, f64,
                                               Option<(u32, i64, i64, PyReadonlyArray2<'py, f64>)>)>)
                         -> PyResult<(Vec<(u32, i64, i64, Vec<f64>, f64)>, Vec<(u32, i64, i64)>, Vec<(i64, i64, Vec<f64>)>)> {
    let hz = horizon.map(|(reach, min_d, r, base, half, cell, merc)| {
        let a = base.as_array();
        let merc = merc.map(|(zb, x0, y0, m)| {
            let m = m.as_array();
            (zb, x0, y0, m.nrows(), m.ncols(), m.iter().copied().collect())
        });
        Horizon { reach, min_d, r, base: a.iter().copied().collect(), n: a.nrows(), half, cell, merc }
    });
    let f = Frame::new(lat0, lon0);
    let zroot = (zf as i32 - (max_level - 3)).max(0) as u32;
    let prev: std::collections::HashSet<(u32, i64, i64)> = prev.into_iter().collect();
    let hold: std::collections::HashSet<(u32, i64, i64)> = hold.into_iter().collect();
    let sq = [px - radius, py - radius, px + radius, py + radius];
    let reach = if disc { radius } else { radius * std::f64::consts::SQRT_2 };
    let mut stack: Vec<(u32, i64, i64)> = near(&f, zroot, px, py, reach).into_iter().map(|(x, y, _)| (zroot, x, y)).collect();
    stack.reverse();
    let (mut tiles, mut refined, mut regions) = (Vec::new(), Vec::new(), Vec::new());
    while let Some((z, x, y)) = stack.pop() {
        let q = quad(&f, z, x, y);
        let b = bbox(&q);
        let d = quad_dist(&q, px, py);
        if disc {
            if d >= radius {
                continue;
            }
        } else if !overlaps(&b, &sq) {
            continue;
        }
        if let Some(hz) = &hz {
            if !hz.keeps(d, &b, (z, x, y)) {
                continue;
            }
        }
        let lvl = (3 + zf as i64 - z as i64) as usize;
        let size = edge_mean(&q);
        let node = (z, x, y);
        let split = if forced.iter().any(|fb| overlaps(&b, &[fb.0, fb.1, fb.2, fb.3])) {
            true
        } else if hold.contains(&node) {
            prev.contains(&node)
        } else {
            let lim = (k[lvl.min(k.len() - 1)] * size).max(rmin);
            d < lim || (prev.contains(&node) && d < (1.0 + h) * lim)
        };
        if split {
            refined.push(node);
            if z >= zf {
                regions.push((x, y, quad_flat(&q)));
            } else {
                let n = 1i64 << (z + 1);
                for (dx, dy) in [(1, 1), (0, 1), (1, 0), (0, 0)] {
                    stack.push((z + 1, (2 * x + dx).rem_euclid(n), 2 * y + dy));
                }
            }
        } else {
            tiles.push((z, x, y, quad_flat(&q), size));
        }
    }
    Ok((tiles, refined, regions))
}

fn quads_of(a: &numpy::ndarray::ArrayView2<'_, f64>) -> Vec<Vec<P2>> {
    (0..a.nrows()).map(|r| (0..a.ncols() / 2).map(|c| [a[[r, 2 * c]], a[[r, 2 * c + 1]]]).collect()).collect()
}

/// An anchor node's box [x0, x1] x [y0, y1] against the region's quads (n, 8: corners x, y): [(quad index, overlap
/// area m^2, the overlap polygon flat)] of every quad it overlaps by more than min_area.
#[pyfunction]
pub fn far_box_cover(quads: PyReadonlyArray2<'_, f64>, x0: f64, y0: f64, x1: f64, y1: f64, min_area: f64)
                     -> Vec<(usize, f64, Vec<f64>)> {
    let qs = quads_of(&quads.as_array());
    let sq = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
    let bb = [x0, y0, x1, y1];
    let mut out = Vec::new();
    for (i, q) in qs.iter().enumerate() {
        let qb = bbox(q);
        if !(qb[0] <= bb[2] && bb[0] <= qb[2] && qb[1] <= bb[3] && bb[1] <= qb[3]) {
            continue;
        }
        let p = clip_convex(&sq, q);
        let a = poly_area(&p).abs();
        if a > min_area {
            out.push((i, a, p.iter().flatten().copied().collect()));
        }
    }
    out
}

/// Pairs (i, j) of polygons (flat x, y lists) on different sides (side[i] != side[j]) that overlap by more than
/// min_area m^2 and more than rel of the smaller one's area (i < j): the quads' straight edges stand off the curved
/// parallels by up to ~L^2 tan(lat) / 8R (13 m on a 31 km tile), slivers that are no overlap.
#[pyfunction]
pub fn far_overlap_pairs(py: Python<'_>, polys: Vec<Vec<f64>>, side: Vec<u8>, min_area: f64, rel: f64)
                         -> Vec<(usize, usize)> {
    let ps: Vec<Vec<P2>> = polys.iter().map(|v| v.chunks(2).map(|c| [c[0], c[1]]).collect()).collect();
    py.allow_threads(|| {
        let bbs: Vec<[f64; 4]> = ps.iter().map(|p| bbox(p)).collect();
        let areas: Vec<f64> = ps.iter().map(|p| poly_area(p).abs()).collect();
        let mut order: Vec<usize> = (0..ps.len()).collect();
        order.sort_by(|a, b| bbs[*a][0].partial_cmp(&bbs[*b][0]).unwrap_or(std::cmp::Ordering::Equal));
        let mut out = Vec::new();
        for (oi, &i) in order.iter().enumerate() {
            for &j in &order[oi + 1..] {
                if bbs[j][0] >= bbs[i][2] {
                    break;
                }
                if side[i] == side[j] || !overlaps(&bbs[i], &bbs[j]) {
                    continue;
                }
                let c = clip_convex(&ps[i], &ps[j]);
                if poly_area(&c).abs() > min_area.max(rel * areas[i].min(areas[j])) {
                    out.push((i.min(j), i.max(j)));
                }
            }
        }
        out.sort_unstable();
        out
    })
}

// --- meshes ----------------------------------------------------------------------------------------------------------

/// Height of the grid's triangulated surface (cells split SW-NE where i + j is even: farfield.surface_at) at grid-index
/// coordinates (gx east, gy north).
fn surface_at(g: &[f64], n: usize, gx: f64, gy: f64) -> f64 {
    let c = n - 1;
    let i = (gx.floor() as i64).clamp(0, c as i64 - 1) as usize;
    let j = (gy.floor() as i64).clamp(0, c as i64 - 1) as usize;
    let (a, b) = (gx - i as f64, gy - j as f64);
    let (z00, z10, z01, z11) = (g[j * n + i], g[j * n + i + 1], g[(j + 1) * n + i], g[(j + 1) * n + i + 1]);
    if (i + j) % 2 == 0 {
        if a >= b { z00 + a * (z10 - z00) + b * (z11 - z10) } else { z00 + a * (z11 - z01) + b * (z01 - z00) }
    } else if a + b <= 1.0 {
        z00 + a * (z10 - z00) + b * (z01 - z00)
    } else {
        z11 + (1.0 - a) * (z01 - z11) + (1.0 - b) * (z10 - z11)
    }
}

fn grid_tris(c: usize) -> Vec<[i64; 3]> {
    let nx = c + 1;
    let mut out = Vec::with_capacity(2 * c * c);
    for j in 0..c {
        for i in 0..c {
            let a = (j * nx + i) as i64;
            let (b, cc, d) = (a + 1, a + nx as i64, a + nx as i64 + 1);
            if (i + j) % 2 == 0 {
                out.push([a, b, d]);
                out.push([a, d, cc]);
            } else {
                out.push([a, b, cc]);
                out.push([b, d, cc]);
            }
        }
    }
    out
}

fn sub(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[0] - b[0], a[1] - b[1], a[2] - b[2]]
}

fn cross(a: [f64; 3], b: [f64; 3]) -> [f64; 3] {
    [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]
}

const WATER_U: f64 = 0.02;      // farfield.WATER_U

type Mesh<'py> = (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>,
                  Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>);

fn arr2<'py, T: numpy::Element>(py: Python<'py>, v: Vec<T>, cols: usize) -> Bound<'py, PyArray2<T>> {
    let rows = v.len() / cols.max(1);
    numpy::ndarray::Array2::from_shape_vec((rows, cols), v).unwrap().into_pyarray(py)
}

/// The world z (up in the frame) of a place.
fn up_of(f: &Frame, lat: f64, lon: f64, h: f64) -> f64 {
    f.to_enu(geo_to_ecef(lat, lon, h))[2]
}

/// A global tile's mesh (farfield.build_global): elev (n+1)^2 DEM heights at its Mercator grid's vertices ([j, i], j
/// north from its south edge, i east), refe (2n+1)^2 the reference at half the spacing (the geometric error), eps:
/// the RTIN simplification's tolerance in vertex spacings (0: the whole grid).
/// Returns (positions (m, 3) relative to (0, 0, pz) in the tile's frame (world convention), normals (m, 3), palette uv
/// (m, 2), planar (m, 2): the Mercator position in the tile (u east, v north), tris (t, 3) counter-clockwise from +Z,
/// grid (n+1)^2: the mesh's surface's world z at the vertices, pz, err, skirt depth, size: the tile's mean edge (m)).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn far_global_mesh<'py>(py: Python<'py>, z: u32, x: i64, y: i64, elev: PyReadonlyArray2<'py, f64>,
                            refe: PyReadonlyArray2<'py, f64>, eps: f64)
                            -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>,
                                         Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>,
                                         f64, f64, f64, f64)> {
    let e = elev.as_array();
    let r = refe.as_array();
    let nv = e.nrows();
    let c = nv.wrapping_sub(1);
    if e.ncols() != nv || nv < 3 || r.nrows() != 2 * c + 1 || r.ncols() != 2 * c + 1 {
        return Err(pyo3::exceptions::PyValueError::new_err("far_global_mesh: (n+1)^2 heights and a (2n+1)^2 reference"));
    }
    let ev: Vec<f64> = e.iter().copied().collect();
    let rv: Vec<f64> = r.iter().copied().collect();
    let res = py.allow_threads(move || {
        let (cl, cn) = centre(z, x, y);
        let f = Frame::new(cl, cn);
        let (xf0, yf1) = (x as f64, y as f64 + 1.0);
        let ll = |i: f64, j: f64, steps: f64| (lat_of(yf1 - j / steps, z), lon_of(xf0 + i / steps, z));
        // the grid: x, y at h = 0, z at the height
        let mut p = vec![[0.0f64; 3]; nv * nv];
        for j in 0..nv {
            for i in 0..nv {
                let (la, lo) = ll(i as f64, j as f64, c as f64);
                let g = f.to_enu(geo_to_ecef(la, lo, 0.0));
                let h = ev[j * nv + i];
                p[j * nv + i] = [g[0], g[1], if h == 0.0 { g[2] } else { up_of(&f, la, lo, h) }];
            }
        }
        let u0: Vec<f64> = p.iter().map(|q| q[2]).collect();
        let size = {
            let corner = |k: usize| p[k];
            let (sw, se, ne, nw) = (corner(0), corner(c), corner(c * nv + c), corner(c * nv));
            let d = |a: [f64; 3], b: [f64; 3]| (b[0] - a[0]).hypot(b[1] - a[1]);
            (d(sw, se) + d(se, ne) + d(ne, nw) + d(nw, sw)) / 4.0
        };
        let sp = size / c as f64;
        let (tris, u) = if eps > 0.0 && c.is_power_of_two() {
            let (t, s, _) = crate::rtin_core(&u0, nv, eps * sp);
            (t, s)
        } else {
            (grid_tris(c), u0.clone())
        };
        // the geometric error: the reference at half the spacing against the mesh's surface
        let nr = 2 * c + 1;
        let mut err = 0.0f64;
        for rj in 0..nr {
            for ri in 0..nr {
                let (la, lo) = ll(ri as f64, rj as f64, (2 * c) as f64);
                let ru = up_of(&f, la, lo, rv[rj * nr + ri]);
                let s = surface_at(&u, nv, ri as f64 / 2.0, rj as f64 / 2.0);
                err = err.max((ru - s).abs());
            }
        }
        // normals: central differences of the full grid (its detail in the light), one-sided at the edges
        let at = |i: usize, j: usize| { let q = p[j * nv + i]; [q[0], q[1], u0[j * nv + i]] };
        let mut nrm = vec![[0.0f64; 3]; nv * nv];
        let mut uv = vec![[0.0f64; 2]; nv * nv];
        for j in 0..nv {
            for i in 0..nv {
                let (i0, i1) = (i.saturating_sub(1), (i + 1).min(c));
                let (j0, j1) = (j.saturating_sub(1), (j + 1).min(c));
                let di = sub(at(i1, j), at(i0, j));
                let dj = sub(at(i, j1), at(i, j0));
                let mut n3 = cross(di, dj);
                let l = (n3[0] * n3[0] + n3[1] * n3[1] + n3[2] * n3[2]).sqrt().max(1e-12);
                n3 = [n3[0] / l, n3[1] / l, n3[2] / l];
                nrm[j * nv + i] = n3;
                // palette: height along u, slope along v (farfield.build_tile)
                let (ex, ey) = (ev[j * nv + i1] - ev[j * nv + i0], ev[j1 * nv + i] - ev[j0 * nv + i]);
                let (dx, dy) = (di[0].hypot(di[1]).max(1e-9), dj[0].hypot(dj[1]).max(1e-9));
                let slope = ((ex / dx).hypot(ey / dy)).clamp(0.0, 1.0);
                let el = ev[j * nv + i];
                let water = el <= 0.5 && slope < 0.02;
                uv[j * nv + i] = [if water { WATER_U } else { 0.1 + 0.88 * (el / 5000.0).clamp(0.0, 1.0) }, 0.5 - 0.47 * slope];
            }
        }
        let pz = (u.iter().sum::<f64>() / u.len() as f64).round();
        // the vertices the mesh uses, in grid order
        let mut new_of = vec![-1i64; nv * nv];
        for t in &tris {
            for &v in t {
                new_of[v as usize] = 0;
            }
        }
        let (mut pos, mut nn, mut uvs, mut pl) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
        let mut cnt = 0i64;
        for k in 0..nv * nv {
            if new_of[k] < 0 {
                continue;
            }
            new_of[k] = cnt;
            cnt += 1;
            let (i, j) = (k % nv, k / nv);
            pos.extend_from_slice(&[p[k][0], p[k][1], u[k] - pz]);
            nn.extend_from_slice(&nrm[k]);
            uvs.extend_from_slice(&uv[k]);
            pl.extend_from_slice(&[i as f64 / c as f64, j as f64 / c as f64]);
        }
        let mut tv: Vec<i64> = tris.iter().flat_map(|t| t.iter().map(|&v| new_of[v as usize])).collect();
        // skirts: the edge runs ordered so each faces outwards (farfield._skirt), lowered copies
        let skirt = 2.0f64.max(0.02 * size).max(2.5 * err + 1.0);
        let g = |i: usize, j: usize| j * nv + i;
        let runs: [Vec<usize>; 4] = [
            (0..nv).map(|i| g(i, 0)).collect(),
            (0..nv).map(|j| g(c, j)).collect(),
            (0..nv).rev().map(|i| g(i, c)).collect(),
            (0..nv).rev().map(|j| g(0, j)).collect(),
        ];
        for run in runs.iter() {
            let used: Vec<usize> = run.iter().copied().filter(|&k| new_of[k] >= 0).collect();
            let base = cnt;
            for &k in &used {
                pos.extend_from_slice(&[p[k][0], p[k][1], u[k] - pz - skirt]);
                nn.extend_from_slice(&nrm[k]);
                uvs.extend_from_slice(&uv[k]);
                let (i, j) = (k % nv, k / nv);
                pl.extend_from_slice(&[i as f64 / c as f64, j as f64 / c as f64]);
                cnt += 1;
            }
            for w in 0..used.len().saturating_sub(1) {
                let (t0, t1) = (new_of[used[w]], new_of[used[w + 1]]);
                let (l0, l1) = (base + w as i64, base + w as i64 + 1);
                tv.extend_from_slice(&[t0, l0, t1, t1, l0, l1]);
            }
        }
        (pos, nn, uvs, pl, tv, u, pz, err, skirt, size)
    });
    let (pos, nn, uvs, pl, tv, u, pz, err, skirt, size) = res;
    Ok((arr2(py, pos, 3), arr2(py, nn, 3), arr2(py, uvs, 2), arr2(py, pl, 2), arr2(py, tv, 3), arr2(py, u, nv),
        pz, err, skirt, size))
}

const ATTR: usize = 10;         // x y z nx ny nz u v pu pv

/// A mesh cut by a convex polygon (poly (k, 2), in the positions' x, y): every triangle clipped (Sutherland-Hodgman
/// against the polygon's half-planes, a cut vertex interpolated on the edge it cuts: its height and attributes the
/// triangle's own), the vertical (skirt) triangles too; plus skirts `depth` m deep along the cut, facing outwards.
/// Returns (positions, normals, uv, planar, tris).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn far_clip_mesh<'py>(py: Python<'py>, positions: PyReadonlyArray2<'py, f64>, normals: PyReadonlyArray2<'py, f64>,
                          uv: PyReadonlyArray2<'py, f64>, planar: PyReadonlyArray2<'py, f64>,
                          tris: PyReadonlyArray2<'py, i64>, poly: PyReadonlyArray2<'py, f64>, depth: f64)
                          -> PyResult<Mesh<'py>> {
    let (p, n, u, pl, t, q) = (positions.as_array(), normals.as_array(), uv.as_array(), planar.as_array(),
                               tris.as_array(), poly.as_array());
    let m = p.nrows();
    let mut verts: Vec<[f64; ATTR]> = (0..m)
        .map(|i| [p[[i, 0]], p[[i, 1]], p[[i, 2]], n[[i, 0]], n[[i, 1]], n[[i, 2]], u[[i, 0]], u[[i, 1]],
                  pl[[i, 0]], pl[[i, 1]]])
        .collect();
    let tri: Vec<[usize; 3]> = (0..t.nrows()).map(|r| [t[[r, 0]] as usize, t[[r, 1]] as usize, t[[r, 2]] as usize]).collect();
    let mut clip: Vec<P2> = (0..q.nrows()).map(|r| [q[[r, 0]], q[[r, 1]]]).collect();
    if poly_area(&clip) < 0.0 {
        clip.reverse();
    }
    let res = py.allow_threads(move || {
        let k = clip.len();
        let side = |e: usize, v: &[f64; ATTR]| {
            let (a, b) = (clip[e], clip[(e + 1) % k]);
            (b[0] - a[0]) * (v[1] - a[1]) - (b[1] - a[1]) * (v[0] - a[0])
        };
        let mut index: HashMap<[u64; 3], usize> = HashMap::new();
        for (i, v) in verts.iter().enumerate() {
            index.entry([v[0].to_bits(), v[1].to_bits(), v[2].to_bits()]).or_insert(i);
        }
        let mut out_t: Vec<[usize; 3]> = Vec::new();
        let mut ground: Vec<bool> = Vec::new();
        for tr in &tri {
            let flat = {
                let (a, b, c) = (verts[tr[0]], verts[tr[1]], verts[tr[2]]);
                ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])).abs() > 1e-9
            };
            // the polygon of this triangle inside every half-plane, by vertex index (new ones appended)
            let mut cur: Vec<usize> = tr.to_vec();
            for e in 0..k {
                if cur.is_empty() {
                    break;
                }
                let inp = std::mem::take(&mut cur);
                let l = inp.len();
                for i in 0..l {
                    let (ia, ib) = (inp[i], inp[(i + 1) % l]);
                    let (da, db) = (side(e, &verts[ia]), side(e, &verts[ib]));
                    if da >= 0.0 {
                        cur.push(ia);
                    }
                    if (da >= 0.0) != (db >= 0.0) {
                        // interpolated from the lexicographically first end, so both triangles of an edge agree
                        let (s, tt, ds, dt) = if verts[ia][..3] <= verts[ib][..3] { (ia, ib, da, db) } else { (ib, ia, db, da) };
                        let w = ds / (ds - dt);
                        let (vs, vt) = (verts[s], verts[tt]);
                        let mut v = [0.0f64; ATTR];
                        for a in 0..ATTR {
                            v[a] = vs[a] + w * (vt[a] - vs[a]);
                        }
                        let key = [v[0].to_bits(), v[1].to_bits(), v[2].to_bits()];
                        let id = *index.entry(key).or_insert_with(|| {
                            verts.push(v);
                            verts.len() - 1
                        });
                        cur.push(id);
                    }
                }
                cur.dedup();
                if cur.len() > 1 && cur[0] == cur[cur.len() - 1] {
                    cur.pop();
                }
            }
            if cur.len() < 3 {
                continue;
            }
            for i in 1..cur.len() - 1 {
                let (a, b, c) = (verts[cur[0]], verts[cur[i]], verts[cur[i + 1]]);
                let cr = cross(sub([b[0], b[1], b[2]], [a[0], a[1], a[2]]), sub([c[0], c[1], c[2]], [a[0], a[1], a[2]]));
                if cr[0] * cr[0] + cr[1] * cr[1] + cr[2] * cr[2] < 1e-12 {
                    continue;
                }
                out_t.push([cur[0], cur[i], cur[i + 1]]);
                ground.push(flat);
            }
        }
        // skirts along the cut: ground triangle edges on a polygon edge with no twin
        let on_edge = |v: &[f64; ATTR]| -> Option<usize> {
            (0..k).find(|&e| {
                let (a, b) = (clip[e], clip[(e + 1) % k]);
                let l = (b[0] - a[0]).hypot(b[1] - a[1]).max(1e-12);
                (side(e, v) / l).abs() < 1e-6
            })
        };
        let mut edges: HashMap<(usize, usize), usize> = HashMap::new();
        for (ti, tr) in out_t.iter().enumerate() {
            if !ground[ti] {
                continue;
            }
            for s in 0..3 {
                *edges.entry((tr[s], tr[(s + 1) % 3])).or_insert(0) += 1;
            }
        }
        let mut lows: HashMap<usize, usize> = HashMap::new();
        let mut bound: Vec<(usize, usize)> = edges.keys().copied()
            .filter(|&(a, b)| !edges.contains_key(&(b, a)))
            .filter(|&(a, b)| match (on_edge(&verts[a]), on_edge(&verts[b])) {
                (Some(ea), Some(eb)) => ea == eb || (side(eb, &verts[a]).abs() < 1e-6 && side(ea, &verts[b]).abs() < 1e-6),
                _ => false,
            })
            .collect();
        bound.sort_unstable();
        for (a, b) in bound {
            let mut low = |i: usize, verts: &mut Vec<[f64; ATTR]>| -> usize {
                *lows.entry(i).or_insert_with(|| {
                    let mut v = verts[i];
                    v[2] -= depth;
                    verts.push(v);
                    verts.len() - 1
                })
            };
            let la = low(a, &mut verts);
            let lb = low(b, &mut verts);
            out_t.push([a, la, b]);
            out_t.push([b, la, lb]);
        }
        // the vertices used, in order
        let mut new_of = vec![usize::MAX; verts.len()];
        let mut order = Vec::new();
        for tr in &out_t {
            for &v in tr {
                if new_of[v] == usize::MAX {
                    new_of[v] = 0;
                }
            }
        }
        for (i, slot) in new_of.iter_mut().enumerate() {
            if *slot == 0 {
                *slot = order.len();
                order.push(i);
            }
        }
        let (mut pos, mut nn, mut uvs, mut pls) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
        for &i in &order {
            let v = verts[i];
            pos.extend_from_slice(&v[0..3]);
            nn.extend_from_slice(&v[3..6]);
            uvs.extend_from_slice(&v[6..8]);
            pls.extend_from_slice(&v[8..10]);
        }
        let tv: Vec<i64> = out_t.iter().flat_map(|tr| tr.iter().map(|&v| new_of[v] as i64)).collect();
        (pos, nn, uvs, pls, tv)
    });
    let (pos, nn, uvs, pls, tv) = res;
    Ok((arr2(py, pos, 3), arr2(py, nn, 3), arr2(py, uvs, 2), arr2(py, pls, 2), arr2(py, tv, 3)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clip_square_by_triangle() {
        let sq = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0]];
        let half = [[1.0, -1.0], [3.0, -1.0], [3.0, 3.0], [1.0, 3.0]];
        let c = clip_convex(&sq, &half);
        assert!((poly_area(&c) - 2.0).abs() < 1e-12);
    }

    #[test]
    fn surface_matches_corners() {
        let g = vec![0.0, 1.0, 2.0, 3.0];
        assert_eq!(surface_at(&g, 2, 0.0, 0.0), 0.0);
        assert_eq!(surface_at(&g, 2, 1.0, 1.0), 3.0);
    }
}
