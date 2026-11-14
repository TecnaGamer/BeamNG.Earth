//! vegetation.py's kernels for the stock clump trees and planted rows (the asset survey, 2026-11-10; GitHub #1074:
//! "trees cost too much"):
//! - veg_clumps: a forest's canopy trees gathered on a world-fixed grid; a square with enough of them gets one item of
//!   a stock clump shape (East Coast USA's tree_douglasfir_group_low, tree_beech_forest_group ...: one item for 4-8
//!   trees), shown beyond the near ring in their place (trees.lua CLUMP_M), and its trees are flagged.
//! - field_lattice: the points of a lattice along a field's axis inside its rings (orchards' trees, vineyards' rows,
//!   hay bales), i-major (numpy's meshgrid 'ij' order, the orchard rows' Python before it).
use crate::dm::Dm;

use std::collections::BTreeMap;

use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

const NONE: u8 = 255;
const HALF_WORLD: f64 = 20037508.342789244;      // Web Mercator's half extent (m)

#[inline]
fn mix(i: i64, j: i64, salt: u64) -> f64 {
    let mut x = (i as u64).wrapping_mul(0x9E3779B97F4A7C15) ^ (j as u64).wrapping_add(salt).wrapping_mul(0xC2B2AE3D27D4EB4F);
    x ^= x >> 31;
    x = x.wrapping_mul(0xBF58476D1CE4E5B9);
    x ^= x >> 29;
    (x >> 11) as f64 / (1u64 << 53) as f64
}

/// The clumps of a cell's canopy trees. x, y: Web Mercator m; group / kind / height / rank / lcls per item; clump_of:
/// a group's clump group (NONE: none); tree_kind: the kind of a tree; skip_class: a land cover class whose trees stay
/// single (built-up land: street trees); cell: the grid's square (Web Mercator m, from the world's edge: a whole number
/// of them a geotile, so a square never straddles two); min_members: trees a square needs. A square's clump stands at
/// its trees' centroid, as tall as the mean of their taller half, with the lowest of their ranks (it shows wherever
/// one of them would), the group most of them map to (ties: the lower id). Returns (member (n,), cx, cy, height,
/// rank, group, u (a hashed [0, 1) per clump)). Deterministic: squares in key order.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn veg_clumps<'py>(py: Python<'py>, x: PyReadonlyArray1<'py, f64>, y: PyReadonlyArray1<'py, f64>,
                       group: PyReadonlyArray1<'py, u8>, kind: PyReadonlyArray1<'py, u8>,
                       height: PyReadonlyArray1<'py, f32>, rank: PyReadonlyArray1<'py, f32>,
                       lcls: PyReadonlyArray1<'py, u8>, clump_of: Vec<u8>, tree_kind: u8, skip_class: u8, cell: f64,
                       min_members: usize)
                       -> PyResult<(Bound<'py, PyArray1<bool>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>,
                                    Bound<'py, PyArray1<f32>>, Bound<'py, PyArray1<f32>>, Bound<'py, PyArray1<u8>>,
                                    Bound<'py, PyArray1<f64>>)> {
    let (x, y, group, kind, height, rank, lcls) = (x.as_array(), y.as_array(), group.as_array(), kind.as_array(),
                                                   height.as_array(), rank.as_array(), lcls.as_array());
    let n = x.len();
    if [y.len(), group.len(), kind.len(), height.len(), rank.len(), lcls.len()].iter().any(|&l| l != n) {
        return Err(PyValueError::new_err("veg_clumps: a value per item"));
    }
    if !(cell > 0.0) || min_members < 1 {
        return Err(PyValueError::new_err("veg_clumps: cell > 0, min_members >= 1"));
    }
    let x = x.to_vec();
    let y = y.to_vec();
    let group = group.to_vec();
    let kind = kind.to_vec();
    let height = height.to_vec();
    let rank = rank.to_vec();
    let lcls = lcls.to_vec();
    let out = py.detach(move || {
        let mut squares: BTreeMap<(i64, i64), Vec<usize>> = BTreeMap::new();
        for i in 0..n {
            let g = group[i] as usize;
            if kind[i] != tree_kind || lcls[i] == skip_class || g >= clump_of.len() || clump_of[g] == NONE {
                continue;
            }
            if !(x[i].is_finite() && y[i].is_finite()) {
                continue;
            }
            let key = (((x[i] + HALF_WORLD) / cell).floor() as i64, ((y[i] + HALF_WORLD) / cell).floor() as i64);
            squares.entry(key).or_default().push(i);
        }
        let mut member = vec![false; n];
        let (mut cx, mut cy, mut ch, mut cr, mut cg, mut cu) = (vec![], vec![], vec![], vec![], vec![], vec![]);
        for (&(i, j), ids) in &squares {
            if ids.len() < min_members {
                continue;
            }
            let mut votes = [0usize; 256];
            let (mut sx, mut sy, mut rmin) = (0.0f64, 0.0f64, f32::INFINITY);
            let mut hs: Vec<f32> = Vec::with_capacity(ids.len());
            for &k in ids {
                votes[clump_of[group[k] as usize] as usize] += 1;
                sx += x[k];
                sy += y[k];
                rmin = rmin.min(rank[k]);
                hs.push(height[k]);
                member[k] = true;
            }
            let mut best = 0usize;
            for g in 0..256 {
                if votes[g] > votes[best] {
                    best = g;
                }
            }
            hs.sort_by(|a, b| b.partial_cmp(a).unwrap_or(std::cmp::Ordering::Equal));
            let top = hs.len().div_ceil(2);
            let h = hs[..top].iter().map(|&v| v as f64).sum::<f64>() / top as f64;
            let m = ids.len() as f64;
            cx.push(sx / m);
            cy.push(sy / m);
            ch.push(h as f32);
            cr.push(rmin);
            cg.push(best as u8);
            cu.push(mix(i, j, 0x5eed));
        }
        (member, cx, cy, ch, cr, cg, cu)
    });
    let (member, cx, cy, ch, cr, cg, cu) = out;
    Ok((member.into_pyarray(py), cx.into_pyarray(py), cy.into_pyarray(py), ch.into_pyarray(py), cr.into_pyarray(py),
        cg.into_pyarray(py), cu.into_pyarray(py)))
}

/// polygon.contains_points' test (even-odd crossings of a ray toward +x: an edge counts when lo <= y < hi and it
/// crosses y right of the point) for one point and one ring.
fn inside_ring(r: &[[f64; 2]], px: f64, py_: f64) -> bool {
    let n = r.len();
    if n < 3 {
        return false;
    }
    let mut c = 0u32;
    for i in 0..n {
        let (x0, y0) = (r[i][0], r[i][1]);
        let (x1, y1) = (r[(i + 1) % n][0], r[(i + 1) % n][1]);
        if y0 == y1 {
            continue;
        }
        let (lo, hi) = (y0.min(y1), y0.max(y1));
        if py_ >= lo && py_ < hi && x0 + (py_ - y0) * ((x1 - x0) / (y1 - y0)) > px {
            c += 1;
        }
    }
    c & 1 == 1
}

/// The lattice points (i * along + phase_a) u + (j * across + phase_b) v (v = u turned left) over the rings' extent
/// along u and v, inside the first ring and outside the others (holes), i-major. rings: (k, 2) vertices (open), ring_off
/// (r + 1,). None when the lattice would be over max_steps along either axis. Returns (pts (m, 2), i (m,), j (m,)).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn field_lattice<'py>(py: Python<'py>, rings: PyReadonlyArray2<'py, f64>, ring_off: PyReadonlyArray1<'py, i64>,
                          u: (f64, f64), along: f64, across: f64, phase_a: f64, phase_b: f64, max_steps: i64)
                          -> PyResult<Option<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>,
                                              Bound<'py, PyArray1<i64>>)>> {
    let (r, off) = (rings.as_array(), ring_off.as_array());
    if off.len() < 2 || !(along > 0.0 && across > 0.0) {
        return Err(PyValueError::new_err("field_lattice: one ring at least, along / across > 0"));
    }
    let rs: Vec<Vec<[f64; 2]>> = (0..off.len() - 1).map(|q| {
        (off[q] as usize..off[q + 1] as usize).map(|k| [r[[k, 0]], r[[k, 1]]]).collect()
    }).collect();
    let (ux, uy) = u;
    let (vx, vy) = (-uy, ux);
    let (mut amin, mut amax, mut bmin, mut bmax) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
    for p in &rs[0] {
        let (a, b) = (p[0] * ux + p[1] * uy, p[0] * vx + p[1] * vy);
        amin = amin.min(a); amax = amax.max(a);
        bmin = bmin.min(b); bmax = bmax.max(b);
    }
    if rs[0].len() < 3 {
        return Ok(None);
    }
    let (i0, i1) = (((amin - phase_a) / along).floor() as i64, ((amax - phase_a) / along).ceil() as i64);
    let (j0, j1) = (((bmin - phase_b) / across).floor() as i64, ((bmax - phase_b) / across).ceil() as i64);
    if i1 - i0 > max_steps || j1 - j0 > max_steps {
        return Ok(None);
    }
    let (pts, ii, jj) = py.detach(|| {
        let (mut pts, mut ii, mut jj) = (Vec::new(), Vec::new(), Vec::new());
        for i in i0..=i1 {
            let a = i as f64 * along + phase_a;
            for j in j0..=j1 {
                let b = j as f64 * across + phase_b;
                let (px, py_) = (a * ux + b * vx, a * uy + b * vy);
                if !inside_ring(&rs[0], px, py_) || rs[1..].iter().any(|h| h.len() >= 3 && inside_ring(h, px, py_)) {
                    continue;
                }
                pts.push(px);
                pts.push(py_);
                ii.push(i);
                jj.push(j);
            }
        }
        (pts, ii, jj)
    });
    let m = ii.len();
    Ok(Some((Array2::from_shape_vec((m, 2), pts).unwrap().into_pyarray(py), ii.into_pyarray(py),
             jj.into_pyarray(py))))
}

/// The direction a field's rows run along (vineyards, hay windrows): its dominant edge direction, the angle (mod 180
/// degrees, 1-degree bins) whose edges within +-window_deg add up to the most length, refined to their length-weighted
/// mean (doubled angles); a rectangle's long side, a trapezoid's parallel sides, where the principal axis of an odd
/// shape ran across its rows (Saint-Emilion's parcels, 2026-11-10 aerial). ring: (k, 2) vertices (open or closed).
/// Returns a unit (ux, uy) pointing east, or north when it runs north-south; (1, 0) for a degenerate ring.
#[pyfunction]
pub fn edge_axis(ring: PyReadonlyArray2<'_, f64>, window_deg: f64) -> (f64, f64) {
    let r = ring.as_array();
    let pts: Vec<[f64; 2]> = (0..r.nrows()).map(|i| [r[[i, 0]], r[[i, 1]]]).collect();
    edge_axis_core(&pts, window_deg)
}

pub(crate) fn edge_axis_core(r: &[[f64; 2]], window_deg: f64) -> (f64, f64) {
    let k = r.len();
    let mut bins = [0.0f64; 180];
    let mut edges: Vec<(f64, f64)> = Vec::with_capacity(k);       // (angle deg in [0, 180), length)
    for i in 0..k {
        let j = (i + 1) % k;
        let (dx, dy) = (r[j][0] - r[i][0], r[j][1] - r[i][1]);
        let l = dx.dhypot(dy);
        if !(l > 0.0) || !l.is_finite() {
            continue;
        }
        let a = dy.datan2(dx).to_degrees().rem_euclid(180.0);
        edges.push((a, l));
        bins[(a as usize).min(179)] += l;
    }
    if edges.is_empty() {
        return (1.0, 0.0);
    }
    let w = window_deg.max(0.0).round() as i64;
    let (mut best, mut best_l) = (0i64, f64::NEG_INFINITY);
    for b in 0..180i64 {
        let s: f64 = (-w..=w).map(|d| bins[(b + d).rem_euclid(180) as usize]).sum();
        if s > best_l {
            best_l = s;
            best = b;
        }
    }
    let centre = best as f64 + 0.5;
    let (mut sx, mut sy) = (0.0f64, 0.0f64);
    for &(a, l) in &edges {
        let d = (a - centre + 90.0).rem_euclid(180.0) - 90.0;      // signed difference mod 180
        if d.abs() <= w as f64 + 0.5 {
            let t = (2.0 * (centre + d)).to_radians();              // doubled: a direction, not a heading
            sx += l * t.dcos();
            sy += l * t.dsin();
        }
    }
    let a = if sx == 0.0 && sy == 0.0 { centre.to_radians() } else { 0.5 * sy.datan2(sx) };
    let (mut ux, mut uy) = (a.dcos(), a.dsin());
    if ux < -1e-9 || (ux.abs() <= 1e-9 && uy < 0.0) {
        ux = -ux;
        uy = -uy;
    }
    (ux, uy)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ring_test_is_even_odd() {
        let sq = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]];
        assert!(inside_ring(&sq, 5.0, 5.0));
        assert!(!inside_ring(&sq, 15.0, 5.0));
        assert!(!inside_ring(&sq, 5.0, 10.0));       // (the top edge: hi excluded)
        assert!(inside_ring(&sq, 5.0, 0.0));         // (the bottom edge: lo included)
    }

    #[test]
    fn edge_axis_of_a_skewed_parcel_is_its_long_sides() {
        // a parallelogram: long sides along +x (100 m), short ones slanted 45 degrees: its principal axis is skewed
        let (ux, uy) = edge_axis_core(&[[0.0, 0.0], [100.0, 0.0], [160.0, 60.0], [60.0, 60.0]], 8.0);
        assert!((ux - 1.0).abs() < 1e-9 && uy.abs() < 1e-9, "{ux} {uy}");
        let (ux, uy) = edge_axis_core(&[[0.0, 0.0], [0.0, 100.0], [-10.0, 100.0], [-10.0, 0.0]], 8.0);
        assert!(ux.abs() < 1e-9 && (uy - 1.0).abs() < 1e-9, "north-south: {ux} {uy}");
        assert_eq!(edge_axis_core(&[[1.0, 1.0]], 8.0), (1.0, 0.0));
    }

    #[test]
    fn mix_is_uniformish_and_stable() {
        let a = mix(3, 4, 1);
        assert_eq!(a, mix(3, 4, 1));
        assert!(a >= 0.0 && a < 1.0);
        assert_ne!(a, mix(4, 3, 1));
    }
}
