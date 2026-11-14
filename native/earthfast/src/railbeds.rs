//! The rail beds of one node window of railprofile's lattice (plan 035): the long smoothing of the ground parts' beds
//! (railprofile.smooth) and the switches brought to the lines they join (railprofile.join_beds). Each node profiles the
//! lines of a window ~1.2 km wide, ~90 nodes a city's 16 tiles, and join_beds' numpy per end per other part (every
//! part's two ends against every other ground part's line) was ~1.5 s of a 16-tile probe at Penn Station. The same
//! products, sums and comparisons in the same order as the numpy version (elementwise: bit for bit), np.interp's own
//! formula and np.argmin's first-of-equals (a NaN first).
use crate::dm::Dm;

use numpy::{IntoPyArray, PyArray1, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// np.minimum / np.maximum: a NaN either side wins.
fn nmin(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b < a { b } else { a }
}
fn nmax(a: f64, b: f64) -> f64 {
    if a.is_nan() || b.is_nan() { f64::NAN } else if b > a { b } else { a }
}
/// np.clip(x, lo, hi) (maximum, then minimum).
fn clip(x: f64, lo: f64, hi: f64) -> f64 {
    nmin(nmax(x, lo), hi)
}

/// railways._arc: [0, cumsum(hypot(diff))].
fn arc(xy: &[[f64; 2]]) -> Vec<f64> {
    let mut s = vec![0.0f64; xy.len()];
    for k in 1..xy.len() {
        s[k] = s[k - 1] + (xy[k][0] - xy[k - 1][0]).dhypot(xy[k][1] - xy[k - 1][1]);
    }
    s
}

/// railways._avg: the running mean over 2r + 1 values, the ends extended by odd reflection.
fn avg(v: &[f64], r: usize) -> Vec<f64> {
    let n = v.len();
    let r = r.min(n.saturating_sub(1));
    if r < 1 {
        return v.to_vec();
    }
    let mut pad = Vec::with_capacity(n + 2 * r);
    for k in (1..=r).rev() {
        pad.push(2.0 * v[0] - v[k]); // v[r:0:-1]
    }
    pad.extend_from_slice(v);
    for k in 0..r {
        pad.push(2.0 * v[n - 1] - v[n - 2 - k]); // v[-2:-r-2:-1]
    }
    let mut c = Vec::with_capacity(pad.len() + 1);
    c.push(0.0f64);
    let mut acc = 0.0f64;
    for &p in &pad {
        acc += p;
        c.push(acc);
    }
    let w = (2 * r + 1) as f64;
    (0..n).map(|i| (c[i + 2 * r + 1] - c[i]) / w).collect()
}

/// railprofile.smooth of one part.
fn smooth(xy: &[[f64; 2]], zb: &[f64], steps: usize, max_cut: f64, pin: f64) -> Vec<f64> {
    let n = zb.len();
    if n < 8 {
        return zb.to_vec();
    }
    let r = steps.min((n - 1) / 2);
    let z = avg(&avg(zb, r), (r / 2).max(1));
    let s = arc(xy);
    let last = s[n - 1];
    (0..n)
        .map(|i| {
            let d = clip(z[i] - zb[i], -max_cut, max_cut);
            let w = clip(nmin(s[i], last - s[i]) / pin, 0.0, 1.0);
            zb[i] + d * (w * w * (3.0 - 2.0 * w))
        })
        .collect()
}

/// railprofile._on_line: (distance, arc along xy) of xy's point nearest p.
fn on_line(p: [f64; 2], xy: &[[f64; 2]], s: &[f64]) -> (f64, f64) {
    let (mut best, mut k, mut tk) = (f64::NAN, usize::MAX, 0.0f64);
    for j in 0..xy.len() - 1 {
        let (a, b) = (xy[j], xy[j + 1]);
        let ab = [b[0] - a[0], b[1] - a[1]];
        let num = (p[0] - a[0]) * ab[0] + (p[1] - a[1]) * ab[1];
        let den = nmax(ab[0] * ab[0] + ab[1] * ab[1], 1e-12);
        let t = clip(num / den, 0.0, 1.0);
        let d = ((a[0] + t * ab[0]) - p[0]).dhypot((a[1] + t * ab[1]) - p[1]);
        // (np.argmin: the first NaN, else the first of the smallest)
        if k == usize::MAX || (!best.is_nan() && (d.is_nan() || d < best)) {
            best = d;
            k = j;
            tk = t;
        }
    }
    (best, s[k] + tk * (s[k + 1] - s[k]))
}

/// np.interp(x, xp, fp) for one x (xp ascending): numpy's formula and its NaN fallbacks.
fn interp(x: f64, xp: &[f64], fp: &[f64]) -> f64 {
    let n = xp.len();
    if x.is_nan() {
        return x;
    }
    if n == 1 {
        return fp[0];
    }
    if x > xp[n - 1] {
        return fp[n - 1];
    }
    if x < xp[0] {
        return fp[0];
    }
    // (the largest j with xp[j] <= x: numpy's binary_search_with_guess)
    let j = xp.partition_point(|&v| v <= x) - 1;
    if j == n - 1 || xp[j] == x {
        return fp[j];
    }
    let slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
    let mut r = slope * (x - xp[j]) + fp[j];
    if r.is_nan() {
        r = slope * (x - xp[j + 1]) + fp[j + 1];
        if r.is_nan() && fp[j] == fp[j + 1] {
            r = fp[j];
        }
    }
    r
}

struct Parts {
    pts: Vec<[f64; 2]>,
    off: Vec<usize>,
}

impl Parts {
    fn xy(&self, p: usize) -> &[[f64; 2]] {
        &self.pts[self.off[p]..self.off[p + 1]]
    }
}

fn parts_of(xy: &PyReadonlyArray2<'_, f64>, off: &PyReadonlyArray1<'_, i64>, n: usize) -> PyResult<Parts> {
    let xyv = xy.as_array();
    if xyv.ncols() < 2 || xyv.nrows() != n {
        return Err(pyo3::exceptions::PyValueError::new_err("rail beds: xy needs (n, 2), as many rows as z"));
    }
    let pts: Vec<[f64; 2]> = xyv.rows().into_iter().map(|r| [r[0], r[1]]).collect();
    let o: Vec<i64> = off.as_array().iter().copied().collect();
    if o.is_empty() || o[0] != 0 || *o.last().unwrap() as usize != n || o.windows(2).any(|w| w[1] < w[0]) {
        return Err(pyo3::exceptions::PyValueError::new_err("rail beds: inconsistent part offsets"));
    }
    Ok(Parts { pts, off: o.iter().map(|&v| v as usize).collect() })
}

/// railprofile.smooth of the parts `which` of the parts (points xy[off[p]..off[p+1]], beds z the same), the others as
/// they are: the beds, end to end.
#[pyfunction]
#[pyo3(signature = (xy, off, z, which, steps, max_cut, pin))]
#[allow(clippy::too_many_arguments)]
pub fn rail_bed_smooth<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                            z: PyReadonlyArray1<'py, f64>, which: PyReadonlyArray1<'py, bool>, steps: usize,
                            max_cut: f64, pin: f64) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let zv: Vec<f64> = z.as_array().iter().copied().collect();
    let parts = parts_of(&xy, &off, zv.len())?;
    let wh: Vec<bool> = which.as_array().iter().copied().collect();
    if wh.len() + 1 != parts.off.len() {
        return Err(pyo3::exceptions::PyValueError::new_err("rail_bed_smooth: one flag per part"));
    }
    let out = py.detach(|| {
        let mut out = zv.clone();
        for (p, &w) in wh.iter().enumerate() {
            if w {
                let (a, b) = (parts.off[p], parts.off[p + 1]);
                let got = smooth(parts.xy(p), &zv[a..b], steps, max_cut, pin);
                out[a..b].copy_from_slice(&got);
            }
        }
        out
    });
    Ok(out.into_pyarray(py))
}

/// railprofile.join_beds: the beds z (end to end, per part of xy / off) with each eligible part's end (elig: a ground
/// track, not a tram, two points or more) that lies on another eligible part's line (a different feature: group) brought
/// to that part's bed there, run out over reach_m. inside: (x0, y0, x1, y1), an end outside it left alone; NaN for none.
#[pyfunction]
#[pyo3(signature = (xy, off, z, group, elig, inside, join_m, interior_m, reach_m))]
#[allow(clippy::too_many_arguments)]
pub fn rail_join_beds<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                           z: PyReadonlyArray1<'py, f64>, group: PyReadonlyArray1<'py, i64>,
                           elig: PyReadonlyArray1<'py, bool>, inside: (f64, f64, f64, f64), join_m: f64,
                           interior_m: f64, reach_m: f64) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let zv: Vec<f64> = z.as_array().iter().copied().collect();
    let parts = parts_of(&xy, &off, zv.len())?;
    let gv: Vec<i64> = group.as_array().iter().copied().collect();
    let ev: Vec<bool> = elig.as_array().iter().copied().collect();
    let np_ = parts.off.len() - 1;
    if gv.len() != np_ || ev.len() != np_ {
        return Err(pyo3::exceptions::PyValueError::new_err("rail_join_beds: one group and flag per part"));
    }
    let out = py.detach(|| {
        let ground: Vec<usize> = (0..np_).filter(|&p| ev[p] && parts.off[p + 1] - parts.off[p] >= 2).collect();
        let mut arcs: Vec<Vec<f64>> = vec![Vec::new(); np_];
        let mut boxes: Vec<([f64; 2], [f64; 2])> = vec![([0.0; 2], [0.0; 2]); np_];
        for &p in &ground {
            let xy = parts.xy(p);
            arcs[p] = arc(xy);
            let (mut lo, mut hi) = (xy[0], xy[0]);
            for q in &xy[1..] {
                for c in 0..2 {
                    lo[c] = nmin(lo[c], q[c]);
                    hi[c] = nmax(hi[c], q[c]);
                }
            }
            boxes[p] = ([lo[0] - join_m, lo[1] - join_m], [hi[0] + join_m, hi[1] + join_m]);
        }
        let zb = |p: usize| &zv[parts.off[p]..parts.off[p + 1]];
        let has_inside = !(inside.0.is_nan() || inside.1.is_nan() || inside.2.is_nan() || inside.3.is_nan());
        let mut out = zv.clone();
        for &p in &ground {
            let xy = parts.xy(p);
            let n = xy.len();
            let s = &arcs[p];
            let big_s = s[n - 1];
            let mut delta: [Option<f64>; 2] = [None, None];
            for (e, idx) in [(0usize, 0usize), (1, n - 1)] {
                let pt = xy[idx];
                if has_inside && !(inside.0 <= pt[0] && pt[0] <= inside.2 && inside.1 <= pt[1] && pt[1] <= inside.3) {
                    continue;
                }
                let mut best: Option<(f64, f64)> = None;
                for &q in &ground {
                    if q == p || gv[q] == gv[p] {
                        continue;
                    }
                    let (lo, hi) = boxes[q];
                    if pt[0] < lo[0] || pt[1] < lo[1] || pt[0] > hi[0] || pt[1] > hi[1] {
                        continue;
                    }
                    let aq = &arcs[q];
                    let (d, at) = on_line(pt, parts.xy(q), aq);
                    if d < join_m && interior_m < at && at < aq[aq.len() - 1] - interior_m
                        && best.is_none_or(|b| d < b.0) {
                        best = Some((d, interp(at, aq, zb(q))));
                    }
                }
                let zp = zb(p)[idx];
                if let Some(b) = best {
                    if zp.is_finite() {
                        delta[e] = Some(b.1 - zp);
                    }
                }
            }
            if delta[0].is_none() && delta[1].is_none() {
                continue;
            }
            let both = delta[0].is_some() && delta[1].is_some();
            let l = if reach_m < big_s { reach_m } else { big_s }; // min(JOIN_REACH_M, S)
            let dst = &mut out[parts.off[p]..parts.off[p + 1]];
            if both && big_s <= 2.0 * l {
                let zp = zb(p);
                let z0 = zp[0] + delta[0].unwrap();
                let z1 = zp[n - 1] + delta[1].unwrap();
                let den = if big_s >= 1e-9 { big_s } else { 1e-9 };
                for i in 0..n {
                    let u = clip(s[i] / den, 0.0, 1.0);
                    dst[i] = z0 + (z1 - z0) * u;
                }
                continue;
            }
            let lden = if l >= 1e-9 { l } else { 1e-9 };
            let mut corr = vec![0.0f64; n];
            for (e, d) in delta.iter().enumerate() {
                let Some(d) = *d else { continue };
                for i in 0..n {
                    let dist = if e == 0 { s[i] } else { big_s - s[i] };
                    let w = clip(1.0 - dist / lden, 0.0, 1.0);
                    corr[i] += d * w * w * (3.0 - 2.0 * w);
                }
            }
            for i in 0..n {
                dst[i] += corr[i];
            }
        }
        out
    });
    Ok(out.into_pyarray(py))
}
