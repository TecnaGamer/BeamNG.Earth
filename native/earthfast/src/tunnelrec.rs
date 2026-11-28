//! (629, T1) The tunnel record (native/roadsys tunnel.rs, BNE_ROADSYS_TUNNELS=on) as the server reads it: the record's
//! section kind per point from the profile squares (roadsys_tunnel_kinds), its portals (roadsys_tunnel_mouths) and the
//! terrain holes it allows (tunnel_record_holes: the plan's hole rule, 4.4). Pure: IEEE + - * / and sqrt only, fixed
//! orders.
use crate::roadsysbind::RoadsysSquare;
use numpy::{PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use std::collections::BTreeMap;

/// Per point (n, 2) with tangent (n, 2) (zero rows: none) on map feature feats[i]: (kind, least inner cover) of the
/// record from the square weighing the point most that has the point's stroke (ties: the first in (i, j) order);
/// kind 255 where the point is on no covered run, 254 where no square has its stroke.
#[pyfunction]
pub fn roadsys_tunnel_kinds<'py>(py: Python<'py>, squares: Vec<PyRef<'py, RoadsysSquare>>, pts: PyReadonlyArray2<'py, f64>,
                                 tans: PyReadonlyArray2<'py, f64>, feats: Vec<String>)
                                 -> PyResult<(Bound<'py, PyArray1<u8>>, Bound<'py, PyArray1<f64>>)> {
    let (p, t) = (pts.as_array(), tans.as_array());
    if p.ncols() != 2 || t.ncols() != 2 || t.nrows() != p.nrows() || feats.len() != p.nrows() {
        return Err(PyValueError::new_err("roadsys_tunnel_kinds: inconsistent columns"));
    }
    let mut m: BTreeMap<(i64, i64), &roadsys::square::Square> = BTreeMap::new();
    for s in &squares { m.insert(s.sq.sq, &s.sq); }
    let mut kinds = Vec::with_capacity(p.nrows());
    let mut cover = Vec::with_capacity(p.nrows());
    for i in 0..p.nrows() {
        let q = [p[[i, 0]], p[[i, 1]]];
        let tv = [t[[i, 0]], t[[i, 1]]];
        let tt = if tv[0] == 0.0 && tv[1] == 0.0 { None } else { Some(tv) };
        let mut best: Option<(f64, (u8, f64))> = None;
        for (sq, w) in roadsys::square::weights(q) {
            let Some(s) = m.get(&sq) else { continue };
            let Some(got) = s.tunnel_at(q, tt, &feats[i]) else { continue };
            if best.map(|b| w > b.0).unwrap_or(true) { best = Some((w, got)); }
        }
        let (k, c) = best.map(|b| b.1).unwrap_or((254, f64::NAN));
        kinds.push(k);
        cover.push(c);
    }
    Ok((PyArray1::from_vec(py, kinds), PyArray1::from_vec(py, cover)))
}

/// The record's portals in the squares given: [(x, y, outward dx, dy, hole reach in m, map feature)], each from the
/// square holding it, in the squares' (i, j) order.
#[pyfunction]
pub fn roadsys_tunnel_mouths(squares: Vec<PyRef<'_, RoadsysSquare>>) -> Vec<(f64, f64, f64, f64, f64, String)> {
    let mut m: BTreeMap<(i64, i64), &roadsys::square::Square> = BTreeMap::new();
    for s in &squares { m.insert(s.sq.sq, &s.sq); }
    let mut out = Vec::new();
    for s in m.values() {
        for (p, d, hole, f, _uid) in s.tunnel_mouths() { out.push((p[0], p[1], d[0], d[1], hole, f)); }
    }
    out
}

/// The record's terrain holes of one covered section (its line xy (k, 2), floor z (k,), the tube's half width hw and
/// outer height th; hole0 / hole1: the hole's reach in from its start / end portal, 0 at a joint) on a block's lattice
/// (heights u [iy, ix] of the sample (x0 + ix * square, y0 + iy * square)): mask[iy, ix] set for the square whose
/// south-west sample that is (the engine's own rule) when one of its corners lies over the footprint (hw + margin of
/// the line), between the portal's plane and the hole's reach, and the vault top + over is not under all four
/// corners' ground (tunnel plan 4.4). Nothing else is cut: the tube runs under intact terrain.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn tunnel_record_holes(py: Python<'_>, mut mask: numpy::PyReadwriteArray2<'_, bool>, u: PyReadonlyArray2<'_, f64>,
                           x0: f64, y0: f64, square: f64, xy: PyReadonlyArray2<'_, f64>, z: PyReadonlyArray1<'_, f64>,
                           hw: f64, th: f64, hole0: f64, hole1: f64, margin: f64, over: f64) -> PyResult<()> {
    let (uv, xv, zv) = (u.as_array(), xy.as_array(), z.as_array());
    if xv.ncols() != 2 || zv.len() != xv.nrows() { return Err(PyValueError::new_err("tunnel_record_holes: inconsistent columns")); }
    let mut mv = mask.as_array_mut();
    if mv.shape() != uv.shape() { return Err(PyValueError::new_err("tunnel_record_holes: mask and heights differ")); }
    let k = xv.nrows();
    if k < 2 || !(hole0 > 0.0 || hole1 > 0.0) { return Ok(()); }
    let line: Vec<[f64; 2]> = (0..k).map(|j| [xv[[j, 0]], xv[[j, 1]]]).collect();
    let zs: Vec<f64> = zv.iter().copied().collect();
    let n = uv.shape()[0] as i64;
    let nx = uv.shape()[1] as i64;
    let hits = py.detach(|| holes(&line, &zs, hw, th, hole0, hole1, margin, over, x0, y0, square, n, nx, &|ix, iy| uv[[iy as usize, ix as usize]]));
    for (ix, iy) in hits { mv[[iy as usize, ix as usize]] = true; }
    Ok(())
}

/// The squares (ix, iy: their south-west sample) the hole rule cuts (tunnel_record_holes). h(ix, iy): a sample's height.
#[allow(clippy::too_many_arguments)]
pub fn holes(line: &[[f64; 2]], z: &[f64], hw: f64, th: f64, hole0: f64, hole1: f64, margin: f64, over: f64,
             x0: f64, y0: f64, square: f64, n: i64, nx: i64, h: &dyn Fn(i64, i64) -> f64) -> Vec<(i64, i64)> {
    let k = line.len();
    let mut sj = vec![0.0f64; k];
    for j in 1..k {
        let (dx, dy) = (line[j][0] - line[j - 1][0], line[j][1] - line[j - 1][1]);
        sj[j] = sj[j - 1] + (dx * dx + dy * dy).sqrt();
    }
    let total = sj[k - 1];
    let reach = hw + margin;
    // (station along the line, distance off it, floor there) of a point, the nearest segment's (ties: the first)
    let foot = |x: f64, y: f64| -> (f64, f64, f64) {
        let mut best = (f64::INFINITY, 0.0, z[0]);
        for j in 0..k - 1 {
            let (ax, ay) = (line[j][0], line[j][1]);
            let (bx, by) = (line[j + 1][0], line[j + 1][1]);
            let (ex, ey) = (bx - ax, by - ay);
            let l2 = ex * ex + ey * ey;
            let mut t = if l2 > 1e-12 { ((x - ax) * ex + (y - ay) * ey) / l2 } else { 0.0 };
            // (past the line's own ends the station runs on outside [0, total]: the portal's plane is s = 0 / total)
            if j > 0 && t < 0.0 { t = 0.0; }
            if j + 2 < k && t > 1.0 { t = 1.0; }
            let (fx, fy) = (ax + ex * t, ay + ey * t);
            let d = ((x - fx) * (x - fx) + (y - fy) * (y - fy)).sqrt();
            if d < best.0 {
                let seg = sj[j + 1] - sj[j];
                best = (d, sj[j] + seg * t, z[j] + (z[j + 1] - z[j]) * t.max(0.0).min(1.0));
            }
        }
        (best.1, best.0, best.2)
    };
    let (mut lox, mut loy, mut hix, mut hiy) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for p in line { lox = lox.min(p[0]); loy = loy.min(p[1]); hix = hix.max(p[0]); hiy = hiy.max(p[1]); }
    let g = reach + square;
    let ix0 = (((lox - g - x0) / square).floor() as i64).max(0);
    let iy0 = (((loy - g - y0) / square).floor() as i64).max(0);
    let ix1 = (((hix + g - x0) / square).ceil() as i64).min(nx - 2);
    let iy1 = (((hiy + g - y0) / square).ceil() as i64).min(n - 2);
    let mut out = Vec::new();
    if ix1 < ix0 || iy1 < iy0 { return out; }
    for iy in iy0..=iy1 {
        for ix in ix0..=ix1 {
            let corners = [(ix, iy), (ix + 1, iy), (ix, iy + 1), (ix + 1, iy + 1)];
            let mut cut = false;
            let mut vault = f64::NEG_INFINITY;
            let mut gmin = f64::INFINITY;
            for &(cx, cy) in &corners {
                let (x, y) = (x0 + cx as f64 * square, y0 + cy as f64 * square);
                let (s, d, zf) = foot(x, y);
                let gz = h(cx, cy);
                if gz < gmin { gmin = gz; }
                if d > reach { continue; }
                let inside = (hole0 > 0.0 && s >= 0.0 && s <= hole0) || (hole1 > 0.0 && s <= total && s >= total - hole1);
                if !inside { continue; }
                cut = true;
                if zf + th > vault { vault = zf + th; }
            }
            if cut && !(vault + over <= gmin) { out.push((ix, iy)); }
        }
    }
    out
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(roadsys_tunnel_kinds, m)?)?;
    m.add_function(wrap_pyfunction!(roadsys_tunnel_mouths, m)?)?;
    m.add_function(wrap_pyfunction!(tunnel_record_holes, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::holes;

    #[test]
    fn holes_only_at_the_mouth_and_none_under_deep_ground() {
        // a tube along +x from x = 0 (its portal) to 100 m, floor 0, height 6.8; a hill rising 1 m per metre from x = 0
        let line = [[0.0, 0.0], [100.0, 0.0]];
        let z = [0.0, 0.0];
        let ground = |ix: i64, _iy: i64| -> f64 { let x = -20.0 + ix as f64; if x < 0.0 { 0.0 } else { x } };
        let got = holes(&line, &z, 4.0, 6.8, 30.0, 0.0, 0.5, 1.0, -20.0, -20.0, 1.0, 41, 141, &ground);
        assert!(!got.is_empty());
        // nothing past the plane outward (x < -1), nothing where the ground is over the vault + 1 m (x > 8.8 at all corners)
        assert!(got.iter().all(|&(ix, _)| { let x = -20.0 + ix as f64; x >= -1.0 && x <= 8.0 }), "{:?}", got);
        // nothing beside the footprint (|y| <= 4.5 + a square)
        assert!(got.iter().all(|&(_, iy)| { let y = -20.0 + iy as f64; y >= -5.5 && y <= 4.5 }), "{:?}", got);
        // the end portal (hole1) at x = 100 cuts where the ground is low there (the hill is 100 m high: none)
        let got = holes(&line, &z, 4.0, 6.8, 0.0, 30.0, 0.5, 1.0, -20.0, -20.0, 1.0, 41, 141, &ground);
        assert!(got.is_empty(), "{:?}", got);
    }
}
