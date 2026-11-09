//! Germany's mapped town signs (Ortstafeln, Zeichen 310 / 311: OpenStreetMap's traffic_sign=city_limit nodes, which
//! the lane shards carry: tools/build_lane_tags.py) on a road tile's roads, for the town signs (server/townentry.py
//! mapped_signs is the glue). The rule the estimate follows (VwV-StVO zu den Zeichen 310 und 311 Rn. 1: where the
//! closed development begins) is the town signs' estimate of it from the land cover, 60-250 m off at 10 of the 25
//! mapped Ortstafeln around Barbing (2026-11-07); a mapped one stands where it really is:
//! - town_sign_snap: each mapped node onto the nearest road line within snap_m (a node is a vertex of its way, or a
//!   few metres beside it);
//! - town_sign_select: an estimated sign's change is left out where a mapped sign of one of its two towns stands
//!   within reach_m (the mapped one replaces it), and of two mapped signs of a town on one line closer than dedupe_m
//!   the first is kept (a node mapped for each direction, or twice).
//! Deterministic: the same lines, nodes and changes give the same answer in every tile.

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// Each node's nearest point on the lines (pts (n, 2) in metres, line r its rows off[r] .. off[r + 1]) within snap_m:
/// [line index, arc length along it, distance], [-1, 0, inf] for none. A tie goes to the lower line index.
pub fn snap(pts: &[[f64; 2]], off: &[i64], nodes: &[[f64; 2]], snap_m: f64) -> Vec<[f64; 3]> {
    let mut out = vec![[-1.0, 0.0, f64::INFINITY]; nodes.len()];
    let nr = off.len().saturating_sub(1);
    for r in 0..nr {
        let (a, b) = (off[r].max(0) as usize, (off[r + 1].max(0) as usize).min(pts.len()));
        if b < a + 2 {
            continue;
        }
        let (mut lo, mut hi) = ([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]);
        for p in &pts[a..b] {
            for d in 0..2 {
                lo[d] = lo[d].min(p[d]);
                hi[d] = hi[d].max(p[d]);
            }
        }
        for (k, q) in nodes.iter().enumerate() {
            if q[0] < lo[0] - snap_m || q[0] > hi[0] + snap_m || q[1] < lo[1] - snap_m || q[1] > hi[1] + snap_m {
                continue;
            }
            let mut acc = 0.0;
            for j in a..b - 1 {
                let (p0, p1) = (pts[j], pts[j + 1]);
                let (dx, dy) = (p1[0] - p0[0], p1[1] - p0[1]);
                let len2 = dx * dx + dy * dy;
                let len = len2.sqrt();
                let t = if len2 > 0.0 { (((q[0] - p0[0]) * dx + (q[1] - p0[1]) * dy) / len2).clamp(0.0, 1.0) } else { 0.0 };
                let (x, y) = (p0[0] + t * dx, p0[1] + t * dy);
                let dist = ((q[0] - x).powi(2) + (q[1] - y).powi(2)).sqrt();
                if dist <= snap_m && dist < out[k][2] {
                    out[k] = [r as f64, acc + t * len, dist];
                }
                acc += len;
            }
        }
    }
    out
}

/// changes (c, 2): the estimated changes' points, change_towns their two towns (-1: none); signs (k, 4): the mapped
/// signs [x, y, line, arc length], sign_towns their towns. Returns (keep the change?, keep the sign?).
pub fn select(changes: &[[f64; 2]], change_towns: &[[i64; 2]], signs: &[[f64; 4]], sign_towns: &[i64], reach_m: f64,
              dedupe_m: f64) -> (Vec<bool>, Vec<bool>) {
    let keep_c = changes.iter().zip(change_towns).map(|(p, t)| {
        !signs.iter().zip(sign_towns).any(|(q, &st)| {
            st >= 0 && (st == t[0] || st == t[1]) && ((p[0] - q[0]).powi(2) + (p[1] - q[1]).powi(2)).sqrt() <= reach_m
        })
    }).collect();
    let mut keep_s = vec![true; signs.len()];
    for k in 0..signs.len() {
        for j in 0..k {
            if keep_s[j] && sign_towns[j] == sign_towns[k] && signs[j][2] == signs[k][2]
                && (signs[j][3] - signs[k][3]).abs() < dedupe_m {
                keep_s[k] = false;
                break;
            }
        }
    }
    (keep_c, keep_s)
}

fn rows<const N: usize>(a: &numpy::ndarray::ArrayView2<'_, f64>) -> Vec<[f64; N]> {
    a.outer_iter().map(|r| std::array::from_fn(|d| r[d])).collect()
}

/// Python: town_sign_snap(pts (n, 2), off (r + 1), nodes (k, 2), snap_m) -> (k, 3) [line, arc length, distance].
#[pyfunction]
pub fn town_sign_snap<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                           nodes: PyReadonlyArray2<'py, f64>, snap_m: f64) -> Bound<'py, PyArray2<f64>> {
    let (p, n) = (rows::<2>(&pts.as_array()), rows::<2>(&nodes.as_array()));
    let o = off.as_array().to_vec();
    let got = py.detach(|| snap(&p, &o, &n, snap_m));
    let flat: Vec<f64> = got.iter().flatten().copied().collect();
    numpy::ndarray::Array2::from_shape_vec((got.len(), 3), flat).expect("shape").into_pyarray(py)
}

/// Python: town_sign_select(changes (c, 2), change_towns (c, 2), signs (k, 4), sign_towns (k), reach_m, dedupe_m)
/// -> (keep changes (c) bool, keep signs (k) bool).
#[pyfunction]
pub fn town_sign_select<'py>(py: Python<'py>, changes: PyReadonlyArray2<'py, f64>,
                             change_towns: PyReadonlyArray2<'py, i64>, signs: PyReadonlyArray2<'py, f64>,
                             sign_towns: PyReadonlyArray1<'py, i64>, reach_m: f64, dedupe_m: f64)
                             -> (Bound<'py, PyArray1<bool>>, Bound<'py, PyArray1<bool>>) {
    let c = rows::<2>(&changes.as_array());
    let ct: Vec<[i64; 2]> = change_towns.as_array().outer_iter().map(|r| [r[0], r[1]]).collect();
    let s = rows::<4>(&signs.as_array());
    let st = sign_towns.as_array().to_vec();
    let (kc, ks) = py.detach(|| select(&c, &ct, &s, &st, reach_m, dedupe_m));
    (kc.into_pyarray(py), ks.into_pyarray(py))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_node_onto_the_nearest_line() {
        // a 200 m road east along y = 0 and a 100 m one north at x = 300; a node 4 m beside the first at x = 120, one
        // on the second's vertex, one 30 m from both (none within 15 m)
        let pts = [[0.0, 0.0], [100.0, 0.0], [200.0, 0.0], [300.0, 0.0], [300.0, 100.0]];
        let got = snap(&pts, &[0, 3, 5], &[[120.0, 4.0], [300.0, 100.0], [250.0, 30.0]], 15.0);
        assert_eq!(got[0][0], 0.0);
        assert!((got[0][1] - 120.0).abs() < 1e-9 && (got[0][2] - 4.0).abs() < 1e-9);
        assert_eq!(got[1][0], 1.0);
        assert!((got[1][1] - 100.0).abs() < 1e-9);
        assert_eq!(got[2][0], -1.0);
    }

    #[test]
    fn a_mapped_sign_replaces_the_estimate_of_its_town() {
        // changes of towns 3 (out of town: -1) at x = 0 and x = 1000 and of town 4 at x = 50; a mapped sign of town 3
        // at x = 120: the first change goes (120 m), the second stays (880 m), town 4's stays
        let changes = [[0.0, 0.0], [1000.0, 0.0], [50.0, 0.0]];
        let towns = [[3, -1], [-1, 3], [4, -1]];
        let signs = [[120.0, 0.0, 0.0, 120.0], [130.0, 0.0, 0.0, 130.0], [125.0, 0.0, 1.0, 5.0]];
        let (kc, ks) = select(&changes, &towns, &signs, &[3, 3, 3], 300.0, 25.0);
        assert_eq!(kc, vec![false, true, true]);
        // the second mapped sign 10 m along the same line from the first: left out; the third on another line stays
        assert_eq!(ks, vec![true, false, true]);
        let (kc, _) = select(&changes, &towns, &signs, &[-1, -1, -1], 300.0, 25.0);
        assert_eq!(kc, vec![true, true, true], "a sign of no town replaces nothing");
    }
}
