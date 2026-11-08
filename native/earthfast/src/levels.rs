//! A road layer's off-ground ranges from its map tags, every feature at once (roads.tag_level_ranges: 0.3-0.8 s of a
//! cold Manhattan tile's cell facts as ~65k Python calls, 2026-11-02; the road-semantics port's first step,
//! .claude/work/road_semantics_rust_plan.md).
//!
//! Kinds: 1 bridge, 2 tunnel (so (start, end, kind) tuples sort as Python's ("bridge" < "tunnel")).

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

const BRIDGE: i8 = 1;
const TUNNEL: i8 = 2;

type R = (f64, f64, i8);

fn cmp_r(x: &R, y: &R) -> std::cmp::Ordering {
    x.0.partial_cmp(&y.0)
        .unwrap_or(std::cmp::Ordering::Equal)
        .then(x.1.partial_cmp(&y.1).unwrap_or(std::cmp::Ordering::Equal))
        .then(x.2.cmp(&y.2))
}

/// Python's max(a, b): a unless b > a.
fn pmax(a: f64, b: f64) -> f64 {
    if b > a { b } else { a }
}

/// numpy's pairwise sum of a float64 reduction (np.sum / np.mean, contiguous or a strided column: checked against
/// numpy 2.5 on 3,000 random arrays).
pub(crate) fn np_pairwise(a: &[f64]) -> f64 {
    let n = a.len();
    if n < 8 {
        let mut res = -0.0f64;
        for &v in a {
            res += v;
        }
        res
    } else if n <= 128 {
        let mut r = [0.0f64; 8];
        r.copy_from_slice(&a[..8]);
        let mut i = 8;
        while i < n - (n % 8) {
            for j in 0..8 {
                r[j] += a[i + j];
            }
            i += 8;
        }
        let mut res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        while i < n {
            res += a[i];
            i += 1;
        }
        res
    } else {
        let mut n2 = n / 2;
        n2 -= n2 % 8;
        np_pairwise(&a[..n2]) + np_pairwise(&a[n2..])
    }
}

pub(crate) fn np_sum(a: &[f64]) -> f64 {
    if a.is_empty() {
        return 0.0;
    }
    np_pairwise(a)
}

/// roads._feature_length_m of the rings ring_off[part_off[i]]..ring_off[part_off[i + 1]].
fn feature_length(ll: &numpy::ndarray::ArrayView2<f64>, co: &[i64], ro: &[i64], po: &[i64], i: usize) -> f64 {
    let mut total = 0.0f64;
    for p in po[i] as usize..po[i + 1] as usize {
        for r in ro[p] as usize..ro[p + 1] as usize {
            let (a, b) = (co[r] as usize, co[r + 1] as usize);
            if b - a < 2 {
                continue;
            }
            let lat: Vec<f64> = (a..b).map(|k| ll[[k, 1]]).collect();
            let c = (np_sum(&lat) / (b - a) as f64).to_radians().cos();
            let seg: Vec<f64> = (a + 1..b)
                .map(|k| ((ll[[k, 0]] - ll[[k - 1, 0]]) * c).hypot(ll[[k, 1]] - ll[[k - 1, 1]]))
                .collect();
            total += np_sum(&seg) * 111320.0;
        }
    }
    total
}

fn one(flags: &mut Vec<R>, level: &mut Vec<R>, ways: &[(f64, f64)], length: &mut dyn FnMut() -> f64,
       passage_m: f64, tunnel_gap_m: f64) -> Vec<R> {
    flags.sort_by(cmp_r);
    level.sort_by(cmp_r);
    let mut out: Vec<R> = flags.clone();
    for &(a0, b, k) in level.iter() {
        let mut a = a0;
        let snap = out.clone();
        for &(fa, fb, _) in &snap {
            if fb <= a || fa >= b {
                continue;
            }
            if fa > a {
                out.push((a, fa, k));
            }
            a = pmax(a, fb);
            if a >= b {
                break;
            }
        }
        if a < b {
            out.push((a, b, k));
        }
    }
    out.sort_by(cmp_r);
    let mut merged: Vec<R> = Vec::with_capacity(out.len());
    for &(a, b, k) in &out {
        if let Some(last) = merged.last_mut() {
            if a < last.1 - 1e-9 || (a <= last.1 + 1e-9 && k == last.2) {
                *last = (last.0, pmax(last.1, b), if last.2 == TUNNEL || k == TUNNEL { TUNNEL } else { BRIDGE });
                continue;
            }
        }
        merged.push((a, b, k));
    }
    if merged.len() >= 3 {
        let l = length();
        for n in 1..merged.len() - 1 {
            let (a, b, k) = merged[n];
            if k == TUNNEL && (b - a) * l < passage_m && merged[n - 1].2 == BRIDGE && merged[n + 1].2 == BRIDGE
                && (merged[n - 1].1 - a).abs() < 1e-6 && (b - merged[n + 1].0).abs() < 1e-6 {
                merged[n] = (a, b, BRIDGE);
            }
        }
        let mut joined: Vec<R> = Vec::with_capacity(merged.len());
        for &(a, b, k) in &merged {
            if let Some(last) = joined.last_mut() {
                if k == last.2 && a <= last.1 + 1e-9 {
                    *last = (last.0, b, k);
                    continue;
                }
            }
            joined.push((a, b, k));
        }
        merged = joined;
    }
    if merged.iter().filter(|r| r.2 == TUNNEL).count() > 1 {
        let l = length();
        let mut joined: Vec<R> = Vec::with_capacity(merged.len());
        for &(a, b, k) in &merged {
            if let Some(last) = joined.last_mut() {
                // (a gap is one tunnel's only where its source way goes on through it: Overture split the Fort McHenry
                // Tunnel's range inside its one OSM way; a gap that is a way of its own is open ground between two
                // tunnels: the courtyard between two building passages, the track between its underpasses under the
                // R0 and its ramps at Strombeek-Bever, merged into one 157 m "tunnel" cut through the R0's lanes)
                // (ways: the segment's source ways' rows, one per way; none when it is one way)
                if k == TUNNEL && last.2 == TUNNEL && (a - last.1) * l < tunnel_gap_m
                    && (ways.is_empty() || ways.iter().any(|&(s, e)| s < last.1 - 1e-6 && e > a + 1e-6)) {
                    *last = (last.0, b, TUNNEL);
                    continue;
                }
            }
            joined.push((a, b, k));
        }
        merged = joined;
    }
    merged
}

/// roads.tag_level_ranges for features 0..n: flag rows (feature, start, end, kind) and level rows likewise (kind 0:
/// none; the rows with end <= start or kind 0 are left out here), the source way rows (feature, start, end: one per
/// way; a feature without rows joins its tunnel ranges by their gap alone), the
/// layer's lonlat (k, >= 2) and offsets for the lengths. Returns (off (n + 1,), ranges (m, 2), kinds (m,)): feature
/// i's ranges are rows off[i]..off[i + 1].
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn tag_level_ranges<'py>(py: Python<'py>, n: usize, f_feat: PyReadonlyArray1<'py, i64>,
                             f_start: PyReadonlyArray1<'py, f64>, f_end: PyReadonlyArray1<'py, f64>,
                             f_kind: PyReadonlyArray1<'py, i8>, l_feat: PyReadonlyArray1<'py, i64>,
                             l_start: PyReadonlyArray1<'py, f64>, l_end: PyReadonlyArray1<'py, f64>,
                             l_kind: PyReadonlyArray1<'py, i8>, w_feat: PyReadonlyArray1<'py, i64>,
                             w_start: PyReadonlyArray1<'py, f64>, w_end: PyReadonlyArray1<'py, f64>,
                             lonlat: PyReadonlyArray2<'py, f64>,
                             coord_off: PyReadonlyArray1<'py, i64>, ring_off: PyReadonlyArray1<'py, i64>,
                             part_off: PyReadonlyArray1<'py, i64>, passage_m: f64, tunnel_gap_m: f64)
                             -> (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i8>>) {
    let mut ways: Vec<Vec<(f64, f64)>> = vec![Vec::new(); n];
    let (feat, st, en) = (w_feat.as_array(), w_start.as_array(), w_end.as_array());
    for j in 0..feat.len() {
        let i = feat[j];
        if i >= 0 && (i as usize) < n && en[j] > st[j] {
            ways[i as usize].push((st[j], en[j]));
        }
    }
    let ll = lonlat.as_array();
    let co: Vec<i64> = coord_off.as_array().to_vec();
    let ro: Vec<i64> = ring_off.as_array().to_vec();
    let po: Vec<i64> = part_off.as_array().to_vec();
    let mut fl: Vec<Vec<R>> = vec![Vec::new(); n];
    let mut lv: Vec<Vec<R>> = vec![Vec::new(); n];
    for (rows, feat, st, en, kd) in [(&mut fl, &f_feat, &f_start, &f_end, &f_kind),
                                     (&mut lv, &l_feat, &l_start, &l_end, &l_kind)] {
        let (feat, st, en, kd) = (feat.as_array(), st.as_array(), en.as_array(), kd.as_array());
        for j in 0..feat.len() {
            let i = feat[j];
            if i < 0 || i as usize >= n || kd[j] == 0 || !(en[j] > st[j]) {
                continue;
            }
            rows[i as usize].push((st[j], en[j], kd[j]));
        }
    }
    let mut off = vec![0i64; n + 1];
    let mut flat: Vec<f64> = Vec::new();
    let mut kinds: Vec<i8> = Vec::new();
    for i in 0..n {
        if !fl[i].is_empty() || !lv[i].is_empty() {
            let mut cached: Option<f64> = None;
            let mut length = || *cached.get_or_insert_with(|| feature_length(&ll, &co, &ro, &po, i));
            let got = one(&mut fl[i], &mut lv[i], &ways[i], &mut length, passage_m, tunnel_gap_m);
            for (a, b, k) in got {
                flat.push(a);
                flat.push(b);
                kinds.push(k);
            }
        }
        off[i + 1] = kinds.len() as i64;
    }
    let m = kinds.len();
    (off.into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((m, 2), flat).expect("ranges").into_pyarray(py),
     kinds.into_pyarray(py))
}
