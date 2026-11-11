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
        (a.cos(), a.sin())
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
