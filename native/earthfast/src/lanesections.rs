//! lanesections.py's numeric kernels (PR #880 by DerEifrige-f, ported 2026-11-12):
//! - geodesic_lengths: WGS84 geodesic metres between consecutive lon/lat vertices (Vincenty's inverse), the metric a
//!   merged highway's per-source lane stretches are located in (their fractions along the feature);
//! - lane_runs: the pieces of a lane separator where the growing carriageway has room for it (room >= 0), each with
//!   the metres along the line before its start (its dashes' phase).
use crate::dm::Dm;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

const A: f64 = 6378137.0;
const F: f64 = 1.0 / 298.257223563;
const B: f64 = A * (1.0 - F);
const R_MEAN: f64 = 6371008.8;

/// Vincenty's inverse distance of one edge (radians); the mean sphere's haversine when it does not converge (near
/// antipodal: no road edge).
fn vincenty(lon1: f64, lat1: f64, lon2: f64, lat2: f64) -> f64 {
    let u1 = ((1.0 - F) * lat1.dtan()).datan();
    let u2 = ((1.0 - F) * lat2.dtan()).datan();
    let (s1, c1) = u1.dsin_cos();
    let (s2, c2) = u2.dsin_cos();
    let pi = std::f64::consts::PI;
    let l = (lon2 - lon1 + pi).rem_euclid(2.0 * pi) - pi;
    let mut lam = l;
    let (mut ss, mut cs, mut sigma, mut ca2, mut mid) = (0.0, 1.0, 0.0, 1.0, 0.0);
    let mut converged = false;
    for _ in 0..200 {
        let (sl, cl) = lam.dsin_cos();
        ss = (c2 * sl).dhypot(c1 * s2 - s1 * c2 * cl);
        cs = s1 * s2 + c1 * c2 * cl;
        sigma = ss.datan2(cs);
        let sa = if ss > 1e-15 { c1 * c2 * sl / ss } else { 0.0 };
        ca2 = (1.0 - sa * sa).max(0.0);
        mid = if ca2 > 1e-15 { cs - 2.0 * s1 * s2 / ca2 } else { 0.0 };
        let c = F / 16.0 * ca2 * (4.0 + F * (4.0 - 3.0 * ca2));
        let next = l + (1.0 - c) * F * sa * (sigma + c * ss * (mid + c * cs * (-1.0 + 2.0 * mid * mid)));
        let done = (next - lam).abs() <= 1e-12;
        lam = next;
        if done {
            converged = true;
            break;
        }
    }
    if !converged {
        let h = ((lat2 - lat1) / 2.0).dsin().powi(2) + lat1.dcos() * lat2.dcos() * (l / 2.0).dsin().powi(2);
        return 2.0 * R_MEAN * h.clamp(0.0, 1.0).sqrt().dasin();
    }
    let u2_ = ca2 * (A * A - B * B) / (B * B);
    let aa = 1.0 + u2_ / 16384.0 * (4096.0 + u2_ * (-768.0 + u2_ * (320.0 - 175.0 * u2_)));
    let bb = u2_ / 1024.0 * (256.0 + u2_ * (-128.0 + u2_ * (74.0 - 47.0 * u2_)));
    let ds = bb * ss * (mid + bb / 4.0 * (cs * (-1.0 + 2.0 * mid * mid)
        - bb / 6.0 * mid * (-3.0 + 4.0 * ss * ss) * (-3.0 + 4.0 * mid * mid)));
    B * aa * (sigma - ds)
}

pub fn lengths(ll: &[[f64; 2]]) -> Vec<f64> {
    let r = std::f64::consts::PI / 180.0;
    ll.windows(2).map(|w| vincenty(w[0][0] * r, w[0][1] * r, w[1][0] * r, w[1][1] * r)).collect()
}

/// geodesy.segment_lengths: (k - 1,) metres between the consecutive lon/lat rows of (k, >= 2).
#[pyfunction]
pub fn geodesic_lengths<'py>(py: Python<'py>, lonlat: PyReadonlyArray2<'py, f64>) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let a = lonlat.as_array();
    if a.ncols() < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("geodesic_lengths: lon/lat columns"));
    }
    let ll: Vec<[f64; 2]> = (0..a.nrows()).map(|k| [a[[k, 0]], a[[k, 1]]]).collect();
    Ok(lengths(&ll).into_pyarray(py))
}

/// The runs of line (k, c >= 2) where room >= 0 (k,), cut where room crosses zero (linear), each as (first row,
/// number of rows) into the stacked points and its metres along line before it (xy distance).
pub fn runs(line: &[f64], c: usize, room: &[f64]) -> (Vec<f64>, Vec<(usize, usize, f64)>) {
    let k = room.len();
    let mut pts: Vec<f64> = Vec::new();
    let mut out = Vec::new();
    if k == 0 {
        return (pts, out);
    }
    let p = |i: usize| &line[i * c..(i + 1) * c];
    let mut dist = vec![0.0; k];
    for i in 1..k {
        let (a, b) = (p(i - 1), p(i));
        dist[i] = dist[i - 1] + (b[0] - a[0]).dhypot(b[1] - a[1]);
    }
    if room.iter().all(|r| *r >= -1e-6) {
        pts.extend_from_slice(&line[..k * c]);
        out.push((0, k, 0.0));
        return (pts, out);
    }
    let keep: Vec<bool> = room.iter().map(|r| *r >= 0.0).collect();
    let mut i = 0;
    while i < k {
        if !keep[i] {
            i += 1;
            continue;
        }
        let a = i;
        while i < k && keep[i] {
            i += 1;
        }
        let b = i;
        let start = pts.len() / c;
        let mut before = dist[a];
        if a > 0 {
            let t = -room[a - 1] / (room[a] - room[a - 1]);
            let (q0, q1) = (p(a - 1), p(a));
            for j in 0..c {
                pts.push(q0[j] + t * (q1[j] - q0[j]));
            }
            before = dist[a - 1] + t * (dist[a] - dist[a - 1]);
        }
        pts.extend_from_slice(&line[a * c..b * c]);
        if b < k {
            let t = -room[b - 1] / (room[b] - room[b - 1]);
            let (q0, q1) = (p(b - 1), p(b));
            for j in 0..c {
                pts.push(q0[j] + t * (q1[j] - q0[j]));
            }
        }
        let n = pts.len() / c - start;
        if n >= 2 {
            out.push((start, n, before));
        } else {
            pts.truncate(start * c);
        }
    }
    (pts, out)
}

/// roads._lane_runs: (points (m, c), starts (r + 1,) rows into points, before (r,) m along the line).
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn lane_runs<'py>(py: Python<'py>, line: PyReadonlyArray2<'py, f64>, room: PyReadonlyArray1<'py, f64>)
    -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<f64>>)> {
    let la = line.as_array();
    let ra = room.as_array();
    let (k, c) = (la.nrows(), la.ncols());
    if c < 2 || ra.len() != k {
        return Err(pyo3::exceptions::PyValueError::new_err("lane_runs: a line (k, >= 2) and k room values"));
    }
    let lv: Vec<f64> = la.iter().copied().collect();
    let rv: Vec<f64> = ra.iter().copied().collect();
    let (pts, rr) = runs(&lv, c, &rv);
    let m = pts.len() / c;
    let mut starts: Vec<i64> = rr.iter().map(|r| r.0 as i64).collect();
    starts.push(m as i64);
    let before: Vec<f64> = rr.iter().map(|r| r.2).collect();
    let arr = numpy::ndarray::Array2::from_shape_vec((m, c), pts).unwrap();
    Ok((arr.into_pyarray(py), starts.into_pyarray(py), before.into_pyarray(py)))
}

// ---- split's new layer (kernel 235) --------------------------------------------------------------------------------
// lanesections.split rebuilt the whole roads layer of a cell holding a lane section in Python (every feature's rings,
// every rule row of every table, one by one: ~0.9 CPU-s of a 16-tile I-83 probe, 2026-11-12). The jobs (the new
// features, sorted) are decided in Python; their geometry and the rows they keep are gathered here.

type Coords<'py> = (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>,
                    Bound<'py, PyArray2<f64>>);

/// split's geometry: job j a copy of old feature old[j] (all its parts and rings, as part_off / ring_off / coord_off /
/// coords (N, 2) hold them), or, where moved[j], one part of one ring: pts[pts_off[j] .. pts_off[j + 1]]. Returns the
/// new (part_off, ring_off, coord_off, coords).
#[pyfunction]
pub fn lane_split_coords<'py>(
    py: Python<'py>,
    part_off: PyReadonlyArray1<'py, i64>,
    ring_off: PyReadonlyArray1<'py, i64>,
    coord_off: PyReadonlyArray1<'py, i64>,
    coords: PyReadonlyArray2<'py, f64>,
    old: PyReadonlyArray1<'py, i64>,
    moved: PyReadonlyArray1<'py, bool>,
    pts: PyReadonlyArray2<'py, f64>,
    pts_off: PyReadonlyArray1<'py, i64>,
) -> PyResult<Coords<'py>> {
    let (po, ro, co, xy) = (part_off.as_array(), ring_off.as_array(), coord_off.as_array(), coords.as_array());
    let (old, moved, pts, poff) = (old.as_array(), moved.as_array(), pts.as_array(), pts_off.as_array());
    let bad = |m: &str| pyo3::exceptions::PyValueError::new_err(format!("lane_split_coords: {m}"));
    let k = old.len();
    if moved.len() != k || poff.len() != k + 1 || xy.ncols() < 2 || pts.ncols() < 2 || po.is_empty() {
        return Err(bad("old / moved / pts_off must match; coords and pts (N, 2)"));
    }
    let nf = po.len() - 1;
    let ok_range = |a: i64, b: i64, n: usize| a >= 0 && b >= a && (b as usize) <= n;
    let (mut npo, mut nro, mut nco) = (vec![0i64], vec![0i64], vec![0i64]);
    let mut out: Vec<f64> = Vec::new();
    for j in 0..k {
        if moved[j] {
            let (a, b) = (poff[j], poff[j + 1]);
            if !ok_range(a, b, pts.nrows()) {
                return Err(bad("bad pts_off"));
            }
            for q in a as usize..b as usize {
                out.push(pts[[q, 0]]);
                out.push(pts[[q, 1]]);
            }
            nco.push((out.len() / 2) as i64);
            nro.push(nco.len() as i64 - 1);
        } else {
            let i = old[j];
            if i < 0 || i as usize >= nf {
                return Err(bad("bad old"));
            }
            let i = i as usize;
            if !ok_range(po[i], po[i + 1], ro.len().saturating_sub(1)) {
                return Err(bad("bad part_off"));
            }
            for p in po[i] as usize..po[i + 1] as usize {
                if !ok_range(ro[p], ro[p + 1], co.len().saturating_sub(1)) {
                    return Err(bad("bad ring_off"));
                }
                for r in ro[p] as usize..ro[p + 1] as usize {
                    if !ok_range(co[r], co[r + 1], xy.nrows()) {
                        return Err(bad("bad coord_off"));
                    }
                    for q in co[r] as usize..co[r + 1] as usize {
                        out.push(xy[[q, 0]]);
                        out.push(xy[[q, 1]]);
                    }
                    nco.push((out.len() / 2) as i64);
                }
                nro.push(nco.len() as i64 - 1);
            }
        }
        npo.push(nro.len() as i64 - 1);
    }
    let m = out.len() / 2;
    Ok((
        npo.into_pyarray(py),
        nro.into_pyarray(py),
        nco.into_pyarray(py),
        numpy::ndarray::Array2::from_shape_vec((m, 2), out).expect("coords").into_pyarray(py),
    ))
}

/// Python's max(x, 0.) and min(x, 1.) (a NaN stays: the first argument wins a tie or an unordered pair).
fn clamp01(x: f64) -> f64 {
    let y = if 0.0 > x { 0.0 } else { x };
    if 1.0 < y { 1.0 } else { y }
}

type Rows<'py> = (Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<f64>>,
                  Bound<'py, PyArray1<f64>>);

/// split's rows of one rule table (feature, start, end per row): job j (old feature old[j], its section a[j] .. b[j]
/// of it) keeps all of the feature's rows (in row order) when it is the whole feature (0 .. 1), else those over its
/// section (a point row within 1e-7 of it, a span overlapping it), their fractions the section's. Returns (rows into
/// the table, job of each, start, end), the jobs in order.
#[pyfunction]
pub fn lane_split_rules<'py>(
    py: Python<'py>,
    feature: PyReadonlyArray1<'py, i64>,
    start: PyReadonlyArray1<'py, f64>,
    end: PyReadonlyArray1<'py, f64>,
    old: PyReadonlyArray1<'py, i64>,
    a: PyReadonlyArray1<'py, f64>,
    b: PyReadonlyArray1<'py, f64>,
) -> PyResult<Rows<'py>> {
    let (feat, st, en, old, a, b) =
        (feature.as_array(), start.as_array(), end.as_array(), old.as_array(), a.as_array(), b.as_array());
    if st.len() != feat.len() || en.len() != feat.len() || a.len() != old.len() || b.len() != old.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("lane_split_rules: feature / start / end, old / a / b"));
    }
    // each feature's rows, ascending (RuleTable.rows)
    let mut order: Vec<usize> = (0..feat.len()).collect();
    order.sort_by_key(|&r| feat[r]);           // (stable: rows of one feature stay ascending)
    let mut first: std::collections::HashMap<i64, (usize, usize)> = std::collections::HashMap::new();
    let mut s = 0usize;
    while s < order.len() {
        let f = feat[order[s]];
        let mut e = s + 1;
        while e < order.len() && feat[order[e]] == f {
            e += 1;
        }
        first.insert(f, (s, e));
        s = e;
    }
    let (mut rows, mut jobs, mut us, mut vs) = (Vec::new(), Vec::new(), Vec::new(), Vec::new());
    for j in 0..old.len() {
        let Some(&(s, e)) = first.get(&old[j]) else { continue };
        let (aj, bj) = (a[j], b[j]);
        let whole = aj == 0.0 && bj == 1.0;
        for &r in &order[s..e] {
            let (u, v) = (st[r], en[r]);
            if whole {
                rows.push(r as i64);
                jobs.push(j as i64);
                us.push(clamp01(u));
                vs.push(clamp01(v));
                continue;
            }
            let point = (u - v).abs() < 1e-8;
            if (point && aj - 1e-7 <= u && u <= bj + 1e-7) || (!point && v > aj && u < bj) {
                rows.push(r as i64);
                jobs.push(j as i64);
                us.push(clamp01((u - aj) / (bj - aj)));
                vs.push(clamp01((v - aj) / (bj - aj)));
            }
        }
    }
    Ok((rows.into_pyarray(py), jobs.into_pyarray(py), us.into_pyarray(py), vs.into_pyarray(py)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_degree_of_the_equator() {
        let d = lengths(&[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]);
        assert!((d[0] - A * std::f64::consts::PI / 180.0).abs() < 1e-5);
        assert!((d[1] - 110574.3885578).abs() < 1e-5);
        assert_eq!(lengths(&[[10.0, 48.0], [10.0, 48.0]]), vec![0.0]);
    }

    #[test]
    fn runs_cut_at_zero_room() {
        let line = [0.0, 0.0, 10.0, 0.0, 20.0, 0.0];
        let (pts, rr) = runs(&line, 2, &[-1.0, 1.0, 1.0]);
        assert_eq!(rr.len(), 1);
        assert!((pts[0] - 5.0).abs() < 1e-12 && (rr[0].2 - 5.0).abs() < 1e-12);
    }
}
