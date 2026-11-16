//! The tram rails in the street, for the physics only (plan 046, stage C): the street's paving is drawn with its rails
//! (decals, no height), so a car's wheel felt nothing of them. Here each rail of a street run is a raised strip in the
//! collision mesh (never drawn): the head a few millimetres over the paving, ~6 cm wide, and the counter-rail outside it
//! with a gap between (the groove, as high as the paving: the user's decision 2026-11-13, no flange-riding tram mod in
//! the streets yet, so no real groove in the road's collision). The wheels feel the kerb of the head going over, and
//! a tyre in the rails is led by them. The material is metal (its own ground model: less grip, wet especially).
//! Two calls: the street runs of a tile (street_runs: the same rule as the paving's), then for each run's sample points
//! (physics_points) the heights of the road's surface (the caller's ground) go back for the triangles (physics_mesh).
use super::lines::{clip_polyline, densify, offset_polyline, Segs, P2};
use super::track::{chain_parts, in_street, runs, paving_width};
use crate::dm::Dm;

use numpy::{IntoPyArray, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// m the rail head stands over the paving (5-8 mm: a wheel feels the edge, a car is not thrown).
pub const HEAD_H_M: f64 = 0.007;
/// m: the head's width (the rail's top and its flange gap, as rails.RAIL_W_M's 17 cm ribbon has the head in the middle).
pub const HEAD_W_M: f64 = 0.065;
/// m: the groove between the head and the counter-rail (as high as the paving).
pub const GROOVE_W_M: f64 = 0.045;
/// m: the counter-rail's width.
pub const GUARD_W_M: f64 = 0.04;
/// m the strips reach under the paving (a wall to stand on).
pub const FOOT_M: f64 = 0.02;
/// m between the samples of a run.
pub const STEP_M: f64 = 2.0;
/// The material the strips are in (a metal ground model, never drawn).
pub const MATERIAL: &str = "bne_tram_rail_collider";

/// m a strip's height comes up over at its true ends (a wheel meets a ramp, not a 7 mm wall).
pub const TAPER_M: f64 = 0.3;

/// A street run: its points (cut to the tile), its gauge and whether each end is the run's own (a strip's end: the strips
/// taper there) and not the tile's cut (the next tile goes on).
pub struct StreetRun {
    pub line: Vec<P2>,
    pub gauge: f64,
    pub true_start: bool,
    pub true_end: bool,
}

/// The street runs of the tram parts [(line, gauge)] in the box (x0, y0, x1, y1) of a tile: the stretches within
/// STREET_MARGIN_M of a carriageway (all) or on one of eligible, as the paving's rule has them (found on the whole chain, the
/// decals' runs the same), each chain of ways that meet end to end one line; cut to the box. Runs shorter than 2 points are
/// left out.
pub fn street_runs(parts: &[(Vec<P2>, f64)], bx: [f64; 4], all: &Segs, eligible: &Segs) -> Vec<StreetRun> {
    let mut out = Vec::new();
    for (line, g) in chain_parts(parts) {
        let full = densify(&line, 1.0);
        if full.len() < 2 {
            continue;
        }
        let a = in_street(&full, all);
        let street: Vec<bool> = full
            .iter()
            .zip(&a)
            .map(|(&p, &s)| s || (!eligible.is_empty() && eligible.near(p, -0.3)))
            .collect();
        for (i, j, paved) in runs(&street) {
            if !paved || j <= i {
                continue;
            }
            let whole = &full[i..=j];
            let same = |p: P2, q: P2| (p[0] - q[0]).abs() < 1e-6 && (p[1] - q[1]).abs() < 1e-6;
            for piece in clip_polyline(whole, bx[0], bx[1], bx[2], bx[3]) {
                if piece.len() < 2 {
                    continue;
                }
                let (ts, te) = (same(piece[0], whole[0]), same(piece[piece.len() - 1], whole[whole.len() - 1]));
                out.push(StreetRun { line: piece, gauge: g, true_start: ts, true_end: te });
            }
        }
    }
    out
}

/// The points a run is sampled at: both ends, every STEP_M between, and TAPER_M in from each true end (where the strips'
/// height has come up).
pub fn physics_points(run: &[P2], taper_start: bool, taper_end: bool) -> Vec<P2> {
    let d = densify(run, STEP_M);
    // (densify gives at most STEP_M between the points; thin it to about STEP_M)
    let mut out: Vec<P2> = Vec::new();
    for p in d {
        match out.last() {
            Some(l) if (p[0] - l[0]).dhypot(p[1] - l[1]) < STEP_M * 0.6 => {}
            _ => out.push(p),
        }
    }
    if let (Some(l), Some(e)) = (out.last().copied(), run.last().copied()) {
        if (l[0] - e[0]).dhypot(l[1] - e[1]) > 1e-6 {
            if (l[0] - e[0]).dhypot(l[1] - e[1]) < STEP_M * 0.6 && out.len() > 1 {
                let n = out.len();
                out[n - 1] = e;
            } else {
                out.push(e);
            }
        }
    }
    // the taper's inner points: TAPER_M along the first and the last segment
    let n = out.len();
    if n >= 2 {
        let mut ins: Vec<(usize, P2)> = Vec::new();
        let at = |a: P2, b: P2, m: f64| {
            let l = (b[0] - a[0]).dhypot(b[1] - a[1]);
            if l > 2.0 * m {
                Some([a[0] + (b[0] - a[0]) * m / l, a[1] + (b[1] - a[1]) * m / l])
            } else {
                None
            }
        };
        if taper_start {
            if let Some(q) = at(out[0], out[1], TAPER_M) {
                ins.push((1, q));
            }
        }
        if taper_end {
            if let Some(q) = at(out[n - 1], out[n - 2], TAPER_M) {
                ins.push((n - 1, q));
            }
        }
        for (k, (idx, q)) in ins.into_iter().enumerate() {
            out.insert(idx + k, q);
        }
    }
    out
}

fn smooth(t: f64) -> f64 {
    let t = t.clamp(0.0, 1.0);
    t * t * (3.0 - 2.0 * t)
}

/// The triangles of the strips along a run sampled at pts (physics_points) with the road's surface heights z there:
/// (vertices, triangles). The strips' height comes up over TAPER_M from a true end (taper_start / taper_end).
pub fn physics_mesh(pts: &[P2], z: &[f64], gauge: f64, taper_start: bool, taper_end: bool) -> (Vec<[f64; 3]>, Vec<[u32; 3]>) {
    let (mut v, mut t): (Vec<[f64; 3]>, Vec<[u32; 3]>) = (Vec::new(), Vec::new());
    if pts.len() < 2 || z.len() != pts.len() {
        return (v, t);
    }
    // the arc of each sample, for the taper
    let mut arc = vec![0.0f64; pts.len()];
    for i in 1..pts.len() {
        arc[i] = arc[i - 1] + (pts[i][0] - pts[i - 1][0]).dhypot(pts[i][1] - pts[i - 1][1]);
    }
    let total = arc[pts.len() - 1];
    let height = |i: usize| -> f64 {
        let a = if taper_start { smooth(arc[i] / TAPER_M) } else { 1.0 };
        let b = if taper_end { smooth((total - arc[i]) / TAPER_M) } else { 1.0 };
        HEAD_H_M * a.min(b)
    };
    // the strips' offsets from the centre line, left (+) and right (-) of it
    let h0 = gauge / 2.0;
    let head = (h0, h0 + HEAD_W_M);
    let guard = (head.1 + GROOVE_W_M, head.1 + GROOVE_W_M + GUARD_W_M);
    for sg in [1.0f64, -1.0] {
        for (a, b) in [head, guard] {
            let (la, lb) = (offset_polyline(pts, sg * a), offset_polyline(pts, sg * b));
            let base = v.len() as u32;
            let n = pts.len() as u32;
            // per sample: the inner and outer top vertices, then the inner and outer feet
            for i in 0..pts.len() {
                let top = z[i] + height(i);
                let foot = z[i] - FOOT_M;
                v.push([la[i][0], la[i][1], top]);
                v.push([lb[i][0], lb[i][1], top]);
                v.push([la[i][0], la[i][1], foot]);
                v.push([lb[i][0], lb[i][1], foot]);
            }
            for i in 0..n - 1 {
                let (p, q) = (base + 4 * i, base + 4 * (i + 1));
                // the top (facing up, whichever side of the line it is on), the inner wall and the outer wall
                let (a0, b0, a1, b1) = (p, p + 1, q, q + 1);
                if sg > 0.0 {
                    t.push([a0, a1, b1]);
                    t.push([a0, b1, b0]);
                } else {
                    t.push([a0, b1, a1]);
                    t.push([a0, b0, b1]);
                }
                let (fa0, fb0, fa1, fb1) = (p + 2, p + 3, q + 2, q + 3);
                // walls: the inner one faces the track's middle (toward -sg), the outer one away from it
                if sg > 0.0 {
                    t.push([a0, fa0, fa1]);
                    t.push([a0, fa1, a1]);
                    t.push([b0, b1, fb1]);
                    t.push([b0, fb1, fb0]);
                } else {
                    t.push([a0, fa1, fa0]);
                    t.push([a0, a1, fa1]);
                    t.push([b0, fb1, b1]);
                    t.push([b0, fb0, fb1]);
                }
            }
        }
    }
    (v, t)
}

/// The paving's width a strip run shares with the rails (for the glue's tests).
pub fn strip_width_of(gauge: f64) -> f64 {
    paving_width(gauge)
}

fn pts2(a: &PyReadonlyArray2<f64>) -> PyResult<Vec<P2>> {
    let v = a.as_array();
    if v.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("tram: points need two columns"));
    }
    Ok((0..v.nrows()).map(|i| [v[[i, 0]], v[[i, 1]]]).collect())
}

/// The street runs of a tile's ground tram parts (end to end: xy (n, 2), part_off (p + 1,), gauges (p,)) in the box bx,
/// among the carriageways (a, b, hw: their segments) and the eligible ones: [(run points (k, 2), gauge, true_start,
/// true_end)] as arrays.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn tram_street_runs<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, part_off: PyReadonlyArray1<'py, i64>,
                             gauges: PyReadonlyArray1<'py, f64>, bx: [f64; 4], a: PyReadonlyArray2<'py, f64>,
                             b: PyReadonlyArray2<'py, f64>, hw: PyReadonlyArray1<'py, f64>,
                             ea: PyReadonlyArray2<'py, f64>, eb: PyReadonlyArray2<'py, f64>,
                             ehw: PyReadonlyArray1<'py, f64>)
                             -> PyResult<Vec<(Bound<'py, numpy::PyArray2<f64>>, f64, bool, bool)>> {
    let p = pts2(&xy)?;
    let (off, g) = (part_off.as_array().to_vec(), gauges.as_array().to_vec());
    if off.is_empty() || off.len() != g.len() + 1 || off[off.len() - 1] as usize != p.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("tram_street_runs: parts and offsets differ"));
    }
    let parts: Vec<(Vec<P2>, f64)> = (0..g.len()).map(|k| (p[off[k] as usize..off[k + 1] as usize].to_vec(), g[k])).collect();
    let seg = |a: &PyReadonlyArray2<f64>, b: &PyReadonlyArray2<f64>, hw: &PyReadonlyArray1<f64>| -> PyResult<Segs> {
        let (pa, pb) = (pts2(a)?, pts2(b)?);
        Ok(Segs::new(&pa, &pb, &hw.as_array().to_vec()))
    };
    let (all, eligible) = (seg(&a, &b, &hw)?, seg(&ea, &eb, &ehw)?);
    let out = py.detach(|| street_runs(&parts, bx, &all, &eligible));
    Ok(out
        .into_iter()
        .map(|r| {
            let flat: Vec<f64> = r.line.iter().flat_map(|q| [q[0], q[1]]).collect();
            (numpy::ndarray::Array2::from_shape_vec((r.line.len(), 2), flat).unwrap().into_pyarray(py), r.gauge, r.true_start,
             r.true_end)
        })
        .collect())
}

/// The sample points of a street run (physics_points): (k, 2).
#[pyfunction]
pub fn tram_physics_points<'py>(py: Python<'py>, run: PyReadonlyArray2<'py, f64>, taper_start: bool, taper_end: bool)
                                -> PyResult<Bound<'py, numpy::PyArray2<f64>>> {
    let pts = physics_points(&pts2(&run)?, taper_start, taper_end);
    let flat: Vec<f64> = pts.iter().flat_map(|q| [q[0], q[1]]).collect();
    Ok(numpy::ndarray::Array2::from_shape_vec((pts.len(), 2), flat).unwrap().into_pyarray(py))
}

/// The rails' strips (physics_mesh) along sample points pts (k, 2) over the surface heights z (k,): (vertices (n, 3), triangles
/// (m, 3)).
#[pyfunction]
pub fn tram_physics_mesh<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>, gauge: f64,
                              taper_start: bool, taper_end: bool)
                              -> PyResult<(Bound<'py, numpy::PyArray2<f64>>, Bound<'py, numpy::PyArray2<u32>>)> {
    let (p, zz) = (pts2(&pts)?, z.as_array().to_vec());
    let (v, t) = physics_mesh(&p, &zz, gauge, taper_start, taper_end);
    let vf: Vec<f64> = v.iter().flat_map(|q| *q).collect();
    let tf: Vec<u32> = t.iter().flat_map(|q| *q).collect();
    Ok((numpy::ndarray::Array2::from_shape_vec((v.len(), 3), vf).unwrap().into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((t.len(), 3), tf).unwrap().into_pyarray(py)))
}

/// The numbers of the strips for the glue and its tests.
#[pyfunction]
pub fn tram_physics_consts(py: Python<'_>) -> PyResult<Bound<'_, pyo3::types::PyDict>> {
    let d = pyo3::types::PyDict::new(py);
    for (k, v) in [("HEAD_H_M", HEAD_H_M), ("HEAD_W_M", HEAD_W_M), ("GROOVE_W_M", GROOVE_W_M), ("GUARD_W_M", GUARD_W_M),
                   ("FOOT_M", FOOT_M), ("STEP_M", STEP_M)] {
        d.set_item(k, v)?;
    }
    d.set_item("MATERIAL", MATERIAL)?;
    Ok(d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_street_runs_strips_stand_a_few_mm_over_the_surface_with_a_groove_between_head_and_counter_rail() {
        let pts: Vec<P2> = (0..=10).map(|i| [2.0 * i as f64, 0.0]).collect();
        let z = vec![5.0; 11];
        let (v, t) = physics_mesh(&pts, &z, 1.435, false, false);
        // 2 rails x 2 strips x 11 samples x 4 vertices; 10 segments x 6 triangles per strip
        assert_eq!(v.len(), 2 * 2 * 11 * 4);
        assert_eq!(t.len(), 2 * 2 * 10 * 6);
        let top = v.iter().map(|p| p[2]).fold(f64::NEG_INFINITY, f64::max);
        assert!((top - (5.0 + HEAD_H_M)).abs() < 1e-9, "{top}");
        // the head's inner face is exactly at the gauge's half, the counter-rail beyond the groove
        let ys: Vec<f64> = v.iter().filter(|p| p[2] > 5.0).map(|p| p[1]).collect();
        let near = ys.iter().cloned().filter(|y| *y > 0.0).fold(f64::INFINITY, f64::min);
        assert!((near - 1.435 / 2.0).abs() < 1e-9, "{near}");
        // every top face points up
        for tri in &t {
            let (a, b, c) = (v[tri[0] as usize], v[tri[1] as usize], v[tri[2] as usize]);
            let n = [(b[1] - a[1]) * (c[2] - a[2]) - (b[2] - a[2]) * (c[1] - a[1]),
                     (b[2] - a[2]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[2] - a[2]),
                     (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])];
            if a[2] > 5.0 && b[2] > 5.0 && c[2] > 5.0 {
                assert!(n[2] > 0.0, "the tops face up");
            }
        }
    }

    #[test]
    fn the_street_runs_are_the_paved_stretches_and_the_samples_are_two_metres_apart() {
        let (a, b) = ([[-50.0, 0.0]], [[40.0, 0.0]]);
        let street = Segs::new(&a, &b, &[4.0]);
        let none = Segs::default();
        let line: Vec<P2> = (0..=100).map(|i| [i as f64, 2.0]).collect();
        let runs = street_runs(&[(line, 1.435)], [0.0, 0.0, 128.0, 128.0], &street, &none);
        assert_eq!(runs.len(), 1, "one paved run: the street ends at x = 40");
        let last = runs[0].line[runs[0].line.len() - 1];
        assert!(last[0] > 35.0 && last[0] < 50.0, "{last:?}");
        let pts = physics_points(&runs[0].line, true, true);
        assert!(pts.windows(2).all(|w| (w[1][0] - w[0][0]).dhypot(w[1][1] - w[0][1]) <= STEP_M + 1e-9));
        assert!(pts.windows(2).skip(1).take(pts.len().saturating_sub(3)).all(|w| (w[1][0] - w[0][0]).dhypot(w[1][1] - w[0][1]) >= STEP_M * 0.5 - 1e-9));
        assert!(runs[0].true_start && runs[0].true_end, "the strip's own ends");
    }

    #[test]
    fn the_strips_come_up_over_a_third_of_a_metre_at_a_true_end_and_not_at_a_tiles_cut() {
        let line: Vec<P2> = (0..=20).map(|i| [i as f64, 0.0]).collect();
        let pts = physics_points(&line, true, false);
        assert!((pts[1][0] - TAPER_M).abs() < 1e-9, "a sample TAPER_M in from the true start: {:?}", &pts[..3]);
        let z = vec![3.0; pts.len()];
        let (v, _t) = physics_mesh(&pts, &z, 1.435, true, false);
        let top_at = |x: f64| v.iter().filter(|p| (p[0] - x).abs() < 1e-9 && p[2] > 3.0 - 1e-9).map(|p| p[2]).fold(f64::NEG_INFINITY, f64::max);
        assert!((top_at(0.0) - 3.0).abs() < 1e-9, "flush with the paving at the end");
        assert!((top_at(pts[1][0]) - (3.0 + HEAD_H_M)).abs() < 1e-9, "full height at TAPER_M");
        let (v2, _t2) = physics_mesh(&pts, &z, 1.435, false, false);
        assert!(v2.iter().any(|p| p[0] == 0.0 && (p[2] - (3.0 + HEAD_H_M)).abs() < 1e-9), "no taper at a tile's cut");
    }
}
