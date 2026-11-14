//! The piers under a railway's or a walkway's bridge piece (railways._pier_sites, footbridges): nominal sites every
//! `every` m from the piece's start (the same sites in every tile that draws a clip of it), each moved along the piece
//! by up to 40 % of a span when it would stand on a road or a footway under the bridge, or where the deck is too low
//! over the ground there, else left out. Ported from railways._piers / _sited, whose road test counted only the roads
//! the track crosses at grade, never the ones under the bridge: a pier stood on Caledonian Road's kerb under the North
//! London line in London (the user's report 2026-11-12).

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

use crate::dm::Dm;

/// Shifts tried for a blocked site, as shares of a span (roads.pier_sites' order).
const SHIFTS: [f64; 5] = [0.0, 0.25, -0.25, 0.4, -0.4];

fn interp(s: &[f64], v: &[f64], x: f64) -> f64 {
    let n = s.len();
    if n == 0 {
        return f64::NAN;
    }
    if x <= s[0] {
        return v[0];
    }
    if x >= s[n - 1] {
        return v[n - 1];
    }
    // first index with s[k] > x (np.interp: the left segment holds equal arcs)
    let k = s.partition_point(|&a| a <= x);
    let (s0, s1) = (s[k - 1], s[k]);
    if s1 <= s0 {
        return v[k];
    }
    let t = (x - s0) / (s1 - s0);
    v[k - 1] + t * (v[k] - v[k - 1])
}

fn seg_dist(p: [f64; 2], a: [f64; 2], b: [f64; 2]) -> f64 {
    let d = [b[0] - a[0], b[1] - a[1]];
    let l2 = (d[0] * d[0] + d[1] * d[1]).max(1e-12);
    let t = (((p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1]) / l2).clamp(0.0, 1.0);
    (p[0] - (a[0] + t * d[0])).dhypot(p[1] - (a[1] + t * d[1]))
}

/// Pier sites of one piece: [(arc, x, y, ux, uy, z_top, z_ground)] for the arcs inside [lo, hi].
/// s, xy, u (the piece's left unit across it), top (the deck's top), zg (the ground) per point; obstacles: segments
/// a-b, each with the distance `clear` a pier's centre keeps from it.
#[allow(clippy::too_many_arguments)]
pub fn sites(s: &[f64], xy: &[[f64; 2]], u: &[[f64; 2]], top: &[f64], zg: &[f64], lo: f64, hi: f64, every: f64,
             under: f64, min_h: f64, oa: &[[f64; 2]], ob: &[[f64; 2]], clear: &[f64]) -> Vec<[f64; 7]> {
    let n = s.len();
    let mut out = Vec::new();
    if n < 2 || !(every > 0.0) {
        return out;
    }
    let len = s[n - 1];
    let xs: Vec<f64> = xy.iter().map(|p| p[0]).collect();
    let ys: Vec<f64> = xy.iter().map(|p| p[1]).collect();
    let uxs: Vec<f64> = u.iter().map(|p| p[0]).collect();
    let uys: Vec<f64> = u.iter().map(|p| p[1]).collect();
    // (the obstacles' boxes: a site tests only those within its reach)
    let boxes: Vec<[f64; 4]> = oa.iter().zip(ob).zip(clear).map(|((a, b), c)| {
        [a[0].min(b[0]) - c, a[1].min(b[1]) - c, a[0].max(b[0]) + c, a[1].max(b[1]) + c]
    }).collect();
    let mut k = 1usize;
    loop {
        let nominal = every * k as f64;
        if nominal >= len - every / 2.0 {
            break;
        }
        k += 1;
        // a site whose every shift lies outside this clip is another tile's
        if nominal + 0.4 * every < lo || nominal - 0.4 * every > hi {
            continue;
        }
        for sh in SHIFTS {
            let sc = nominal + sh * every;
            if sc < every / 2.0 || sc > len - every / 2.0 {
                continue;
            }
            let zt = interp(s, top, sc) - under;
            let g = interp(s, zg, sc);
            if !(zt - g >= min_h) {
                continue;
            }
            let p = [interp(s, &xs, sc), interp(s, &ys, sc)];
            let blocked = boxes.iter().enumerate().any(|(m, bx)| {
                p[0] >= bx[0] && p[0] <= bx[2] && p[1] >= bx[1] && p[1] <= bx[3]
                    && seg_dist(p, oa[m], ob[m]) <= clear[m]
            });
            if blocked {
                continue;
            }
            if sc >= lo && sc <= hi {
                let (ux, uy) = (interp(s, &uxs, sc), interp(s, &uys, sc));
                out.push([sc, p[0], p[1], ux, uy, zt, g]);
            }
            break;
        }
    }
    out
}

/// (centres (n, 2), along (n, 2), across (n, 2), z_ground (n,), z_top (n,)) of the piers of one piece drawn in its
/// clip [s[i], s[j]] (railways._pier_sites: the caller sinks them PIER_FOOT_M under the ground round their corners).
#[pyfunction]
#[pyo3(signature = (s, xy, u, top, zg, i, j, every, under, min_h, oa, ob, clear))]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn rail_pier_sites<'py>(py: Python<'py>, s: PyReadonlyArray1<'py, f64>, xy: PyReadonlyArray2<'py, f64>,
                            u: PyReadonlyArray2<'py, f64>, top: PyReadonlyArray1<'py, f64>,
                            zg: PyReadonlyArray1<'py, f64>, i: usize, j: usize, every: f64, under: f64, min_h: f64,
                            oa: PyReadonlyArray2<'py, f64>, ob: PyReadonlyArray2<'py, f64>,
                            clear: PyReadonlyArray1<'py, f64>)
    -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>,
                 Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    let sv: Vec<f64> = s.as_array().iter().copied().collect();
    let n = sv.len();
    let rows2 = |a: &PyReadonlyArray2<'py, f64>| -> PyResult<Vec<[f64; 2]>> {
        let v = a.as_array();
        if v.nrows() > 0 && v.ncols() < 2 {
            return Err(pyo3::exceptions::PyValueError::new_err("arrays of points need two columns"));
        }
        Ok(v.rows().into_iter().map(|r| [r[0], r[1]]).collect())
    };
    let (xyv, uv, oav, obv) = (rows2(&xy)?, rows2(&u)?, rows2(&oa)?, rows2(&ob)?);
    let topv: Vec<f64> = top.as_array().iter().copied().collect();
    let zgv: Vec<f64> = zg.as_array().iter().copied().collect();
    let cv: Vec<f64> = clear.as_array().iter().copied().collect();
    if xyv.len() != n || uv.len() != n || topv.len() != n || zgv.len() != n || oav.len() != obv.len()
        || oav.len() != cv.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("rail_pier_sites: mismatched lengths"));
    }
    let got = if n >= 2 && i < n && j < n {
        sites(&sv, &xyv, &uv, &topv, &zgv, sv[i], sv[j], every, under, min_h, &oav, &obv, &cv)
    } else {
        Vec::new()
    };
    let m = got.len();
    let mut c = numpy::ndarray::Array2::<f64>::zeros((m, 2));
    let mut along = numpy::ndarray::Array2::<f64>::zeros((m, 2));
    let mut across = numpy::ndarray::Array2::<f64>::zeros((m, 2));
    let mut zg_out = Vec::with_capacity(m);
    let mut zt_out = Vec::with_capacity(m);
    for (k, r) in got.iter().enumerate() {
        let un = r[3].dhypot(r[4]).max(1e-9);
        let (ux, uy) = (r[3] / un, r[4] / un);
        c[[k, 0]] = r[1];
        c[[k, 1]] = r[2];
        along[[k, 0]] = uy;
        along[[k, 1]] = -ux;
        across[[k, 0]] = ux;
        across[[k, 1]] = uy;
        zg_out.push(r[6]);
        zt_out.push(r[5]);
    }
    let _ = py;
    Ok((c.into_pyarray(py), along.into_pyarray(py), across.into_pyarray(py), zg_out.into_pyarray(py),
        zt_out.into_pyarray(py)))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(len: f64) -> (Vec<f64>, Vec<[f64; 2]>, Vec<[f64; 2]>) {
        let n = (len / 4.0) as usize + 1;
        let s: Vec<f64> = (0..n).map(|k| k as f64 * len / (n - 1) as f64).collect();
        let xy = s.iter().map(|&a| [a, 0.0]).collect();
        let u = s.iter().map(|_| [0.0, 1.0]).collect();
        (s, xy, u)
    }

    #[test]
    fn a_road_under_the_bridge_moves_the_pier_off_it() {
        let (s, xy, u) = line(100.0);
        let top = vec![10.0; s.len()];
        let zg = vec![0.0; s.len()];
        // no obstacle: sites at 25, 50, 75
        let got = sites(&s, &xy, &u, &top, &zg, 0.0, 100.0, 25.0, 1.4, 1.5, &[], &[], &[]);
        assert_eq!(got.iter().map(|r| r[0]).collect::<Vec<_>>(), vec![25.0, 50.0, 75.0]);
        // a road across at x = 50 (clear 8 m): that pier moves 10 m along (0.4 span), the others stay
        let got = sites(&s, &xy, &u, &top, &zg, 0.0, 100.0, 25.0, 1.4, 1.5, &[[50.0, -20.0]], &[[50.0, 20.0]], &[8.0]);
        assert_eq!(got.iter().map(|r| r[0]).collect::<Vec<_>>(), vec![25.0, 60.0, 75.0]);
        // wider than 0.4 of a span either side: left out
        let got = sites(&s, &xy, &u, &top, &zg, 0.0, 100.0, 25.0, 1.4, 1.5, &[[50.0, -20.0]], &[[50.0, 20.0]], &[12.0]);
        assert_eq!(got.iter().map(|r| r[0]).collect::<Vec<_>>(), vec![25.0, 75.0]);
    }

    #[test]
    fn a_clip_draws_only_its_own_piers_and_a_low_deck_none() {
        let (s, xy, u) = line(100.0);
        let top = vec![10.0; s.len()];
        let zg = vec![0.0; s.len()];
        let got = sites(&s, &xy, &u, &top, &zg, 40.0, 100.0, 25.0, 1.4, 1.5, &[], &[], &[]);
        assert_eq!(got.iter().map(|r| r[0]).collect::<Vec<_>>(), vec![50.0, 75.0]);
        let low = vec![2.5; s.len()];
        assert!(sites(&s, &xy, &u, &low, &zg, 0.0, 100.0, 25.0, 1.4, 1.5, &[], &[], &[]).is_empty());
    }
}
