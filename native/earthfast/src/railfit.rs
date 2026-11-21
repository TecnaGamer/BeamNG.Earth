//! Railway-at-road fixes (server BNE_RAIL_ROAD_FIT; the user's in-game reports r63 / r64 / r92, 2026-11-17): a track
//! part's level crossings (railways.crossing_records: the 3D track's cut and the crossing deck from one record),
//! railways.dead_ends' road test (no buffer stop where the same track goes on past a road) and railstations._synth's
//! trim (a worked-out platform off the roads). Deterministic: elementwise products and sums, np.arange's fill
//! (start + i * delta), libm hypot, the at-grade test of railways._blocked (nearcross.rs), the carriageway test of
//! roads._Segs.near (points_near_segments' expression) and the crossing of railstations._crossings (station_crossings').
use crate::dm::Dm;
use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

/// np.arange(start, stop, step) for floats: ceil((stop - start) / step) values, the first two start and start + step,
/// then start + i * delta with delta = (start + step) - start (numpy's DOUBLE_fill).
pub(crate) fn np_arange(start: f64, stop: f64, step: f64) -> Vec<f64> {
    let len = ((stop - start) / step).ceil();
    if len.is_nan() || len <= 0.0 {
        return Vec::new();
    }
    let n = len as usize;
    let mut v = Vec::with_capacity(n);
    v.push(start);
    if n > 1 {
        let s1 = start + step;
        v.push(s1);
        let delta = s1 - start;
        for i in 2..n {
            v.push(start + (i as f64) * delta);
        }
    }
    v
}

/// The carriageway segments of roads._Segs (a -> b, half width h): which points lie within h + gap of one
/// (points_near_segments' numbers: |ab|^2 floored at 1e-9, the clipped projection, libm hypot, strictly nearer).
struct Segs {
    s: Vec<[f64; 6]>,
}

impl Segs {
    fn new(a: &numpy::ndarray::ArrayView2<f64>, b: &numpy::ndarray::ArrayView2<f64>,
           h: &numpy::ndarray::ArrayView1<f64>) -> Segs {
        let n = a.nrows().min(b.nrows()).min(h.len());
        Segs { s: (0..n).map(|k| {
            let (a0, a1) = (a[[k, 0]], a[[k, 1]]);
            let (ab0, ab1) = (b[[k, 0]] - a0, b[[k, 1]] - a1);
            [a0, a1, ab0, ab1, (ab0 * ab0 + ab1 * ab1).max(1e-9), h[k]]
        }).collect() }
    }

    fn near(&self, p0: f64, p1: f64, gap: f64) -> bool {
        self.s.iter().any(|g| {
            let t = (((p0 - g[0]) * g[2] + (p1 - g[1]) * g[3]) / g[4]).clamp(0.0, 1.0);
            (p0 - (g[0] + t * g[2])).dhypot(p1 - (g[1] + t * g[3])) < g[5] + gap
        })
    }
}

/// Does the polyline w (rows j0..j1 of wp) cross the line m (railstations._crossings / station_crossings' test: a pair
/// of segments, |den| >= 1e-9, both parameters in [0, 1])?
fn crosses(wp: &numpy::ndarray::ArrayView2<f64>, j0: usize, j1: usize, m: &[[f64; 2]]) -> bool {
    for i in j0..j1.saturating_sub(1) {
        let (a0x, a0y) = (wp[[i, 0]], wp[[i, 1]]);
        let (rx, ry) = (wp[[i + 1, 0]] - a0x, wp[[i + 1, 1]] - a0y);
        for j in 0..m.len().saturating_sub(1) {
            let (b0x, b0y) = (m[j][0], m[j][1]);
            let (sx, sy) = (m[j + 1][0] - b0x, m[j + 1][1] - b0y);
            let den = rx * sy - ry * sx;
            if den.abs() < 1e-9 || den.is_nan() {
                continue;
            }
            let (qx, qy) = (b0x - a0x, b0y - a0y);
            let tt = (qx * sy - qy * sx) / den;
            let uu = (qx * ry - qy * rx) / den;
            if (0.0..=1.0).contains(&tt) && (0.0..=1.0).contains(&uu) {
                return true;
            }
        }
    }
    false
}

/// railstations._off_roads: the indices of a worked-out platform's line samples to keep (line (k, 2), out (k, 2) the
/// unit side it lies on; its cross-section at the offsets offs (edge, middle, back) from the track's axis), or None.
/// Blocked: a cross-section point within a carriageway (ca -> cb, half width ch) + road_clear, or within a walkway
/// that crosses the platform's middle line (wp (w, 2) way after way, woff (W + 1,), half width whw (W,)) +
/// walk_clear. None blocked: every index. Else the free run holding the sample nearest arc (ss's first nearest) or the
/// longest (the earlier of equal ones), None when shorter than min_len or than 2 samples.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn station_platform_trim(line: PyReadonlyArray2<'_, f64>, out: PyReadonlyArray2<'_, f64>,
                             offs: PyReadonlyArray1<'_, f64>, ca: PyReadonlyArray2<'_, f64>,
                             cb: PyReadonlyArray2<'_, f64>, ch: PyReadonlyArray1<'_, f64>, road_clear: f64,
                             wp: PyReadonlyArray2<'_, f64>, woff: PyReadonlyArray1<'_, i64>,
                             whw: PyReadonlyArray1<'_, f64>, walk_clear: f64, ss: PyReadonlyArray1<'_, f64>, arc: f64,
                             min_len: f64) -> PyResult<Option<Vec<i64>>> {
    let (l, o, offs, wp, woff, whw, ss) = (line.as_array(), out.as_array(), offs.as_array(), wp.as_array(),
                                           woff.as_array(), whw.as_array(), ss.as_array());
    let k = l.nrows();
    let nw = woff.len().saturating_sub(1);
    if l.ncols() < 2 || o.nrows() != k || o.ncols() < 2 || offs.len() < 2 || ss.len() != k || whw.len() < nw
        || woff.iter().any(|&v| v < 0 || v as usize > wp.nrows()) || (1..woff.len()).any(|i| woff[i] < woff[i - 1]) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "station_platform_trim: line (k, 2), out (k, 2), offs (3,), ss (k,), woff rising within wp, whw (W,)"));
    }
    let cars = Segs::new(&ca.as_array(), &cb.as_array(), &ch.as_array());
    // the walkways crossing the middle line (line + out * offs[1]) as segments of their own
    let mid: Vec<[f64; 2]> = (0..k).map(|i| [l[[i, 0]] + o[[i, 0]] * offs[1], l[[i, 1]] + o[[i, 1]] * offs[1]]).collect();
    let mut wsegs: Vec<[f64; 6]> = Vec::new();
    for w in 0..nw {
        let (j0, j1) = (woff[w] as usize, woff[w + 1] as usize);
        if j1 < j0 + 2 || !crosses(&wp, j0, j1, &mid) {
            continue;
        }
        for j in j0..j1 - 1 {
            let (a0, a1) = (wp[[j, 0]], wp[[j, 1]]);
            let (ab0, ab1) = (wp[[j + 1, 0]] - a0, wp[[j + 1, 1]] - a1);
            wsegs.push([a0, a1, ab0, ab1, (ab0 * ab0 + ab1 * ab1).max(1e-9), whw[w]]);
        }
    }
    let walks = Segs { s: wsegs };
    let mut blocked = vec![false; k];
    for &of in offs.iter() {
        for i in 0..k {
            if blocked[i] {
                continue;
            }
            let (px, py) = (l[[i, 0]] + o[[i, 0]] * of, l[[i, 1]] + o[[i, 1]] * of);
            if cars.near(px, py, road_clear) || walks.near(px, py, walk_clear) {
                blocked[i] = true;
            }
        }
    }
    if !blocked.iter().any(|&v| v) {
        return Ok(Some((0..k as i64).collect()));
    }
    let free: Vec<usize> = (0..k).filter(|&i| !blocked[i]).collect();
    if free.is_empty() {
        return Ok(None);
    }
    let mut runs: Vec<(usize, usize)> = Vec::new();
    for &i in &free {
        match runs.last_mut() {
            Some(r) if r.1 + 1 == i => r.1 = i,
            _ => runs.push((i, i)),
        }
    }
    // np.argmin(np.abs(ss - arc)): the first nearest (a NaN is the minimum and the first one wins)
    let mut at = 0usize;
    let mut best = f64::INFINITY;
    for (i, &s) in ss.iter().enumerate() {
        let v = (s - arc).abs();
        if v.is_nan() {
            at = i;
            break;
        }
        if v < best {
            best = v;
            at = i;
        }
    }
    let run = match runs.iter().find(|r| r.0 <= at && at <= r.1) {
        Some(&r) => r,
        None => {
            // max(runs, key=(length, -start)): the longest, the earlier of equal lengths
            let mut pick = runs[0];
            for &r in &runs[1..] {
                let (lr, lp) = (ss[r.1] - ss[r.0], ss[pick.1] - ss[pick.0]);
                if lr > lp {
                    pick = r;
                }
            }
            pick
        }
    };
    if run.1 - run.0 + 1 < 2 || ss[run.1] - ss[run.0] < min_len {
        return Ok(None);
    }
    Ok(Some((run.0 as i64..=run.1 as i64).collect()))
}

/// One road's per-point at-grade test on a track (railways._blocked's for that road alone: its nearest segment, the
/// first of equals, nearer than its reach with its start's height within dz of the bed), the reach grown by
/// rail_out * |cos| of the angle between the track (dirs: per point) and that segment: a shallow crossing's outer
/// rail, rail_out off the axis, stays out of the carriageway, not in its lane.
#[allow(clippy::too_many_arguments)]
fn road_on(c: usize, p: &[[f64; 2]], dirs: &[[f64; 2]], z: &[f64], d: &[[f64; 3]], st: &[usize], reach: &[f64],
           dz: f64, rail_out: f64) -> Vec<bool> {
    let (j0, j1) = (st[c], st[c + 1]);
    let mut out = vec![false; p.len()];
    if j1 < j0 + 2 {
        return out;
    }
    for (i, q) in p.iter().enumerate() {
        let (mut best, mut bj) = (f64::INFINITY, usize::MAX);
        for j in j0..j1 - 1 {
            let (ax, ay) = (d[j][0], d[j][1]);
            let (bx, by) = (d[j + 1][0] - ax, d[j + 1][1] - ay);
            let l2 = (bx * bx + by * by).max(1e-12);
            let t = (((q[0] - ax) * bx + (q[1] - ay) * by) / l2).clamp(0.0, 1.0);
            let dd = (q[0] - (ax + t * bx)).dhypot(q[1] - (ay + t * by));
            if dd < best || bj == usize::MAX {
                best = dd;
                bj = j;
            }
        }
        if bj == usize::MAX {
            continue;
        }
        let (sx, sy) = (d[bj + 1][0] - d[bj][0], d[bj + 1][1] - d[bj][1]);
        let (sl, tl) = (sx.dhypot(sy), dirs[i][0].dhypot(dirs[i][1]));
        let cos = if sl > 1e-9 && tl > 1e-9 { ((sx * dirs[i][0] + sy * dirs[i][1]) / (sl * tl)).abs() } else { 0.0 };
        out[i] = best < reach[c] + rail_out * cos && (z[i] - d[bj][2]).abs() < dz;
    }
    out
}

/// railways.crossing_records (BNE_RAIL_ROAD_FIT): the level crossings of one track part (xy (n, 2), its bed z (n,)
/// before any flush), each road (the crossers' pack: pts (m, 3) [x, y, road z], starts (C + 1,), reach (C,), dz) only
/// where it crosses this part: the anchors (a_road, a_lo, a_hi: index ranges of the part where the road's centre line
/// crosses it or the part runs down the street) hold the road's at-grade points round them (the run of its own test
/// holding an anchored point, at most max_run along the part past the anchor: a run decided by points the tile's
/// clipped part holds, the same in every tile drawing it), nothing else of it (a road beside the track that crosses
/// another one, or this one elsewhere, cuts nothing here). Returns (blk (n,), runs (r, 2) [a, b] of the joined points, start_t (r,), end_t (r,)):
/// a run's edge on the section before it (a - 1 -> a) and after it (b -> b + 1) as the fraction of the section still
/// off the crossing (`samples` - 1 points then `bisect` halvings, each with the run's own roads' test at the bed heights
/// interpolated: the same test that made the run), NaN where the run reaches the part's end. Each road's reach grows
/// by rail_out * |cos| of the crossing's angle (road_on: the outer rail out of the carriageway on a shallow crossing).
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn rail_crossing_runs<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                               pts: PyReadonlyArray2<'py, f64>, starts: PyReadonlyArray1<'py, i64>,
                               reach: PyReadonlyArray1<'py, f64>, dz: f64, a_road: PyReadonlyArray1<'py, i64>,
                               a_lo: PyReadonlyArray1<'py, i64>, a_hi: PyReadonlyArray1<'py, i64>, samples: usize,
                               bisect: usize, max_run: f64, rail_out: f64)
                               -> PyResult<(Bound<'py, PyArray1<bool>>, Bound<'py, PyArray2<i64>>,
                                            Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>)> {
    let (x, zz, d, st, r) = (xy.as_array(), z.as_array(), pts.as_array(), starts.as_array(), reach.as_array());
    let (ar, alo, ahi) = (a_road.as_array(), a_lo.as_array(), a_hi.as_array());
    let n = x.nrows();
    let nd = d.nrows();
    let nc = st.len().saturating_sub(1);
    let na = ar.len();
    if x.ncols() < 2 || zz.len() != n || d.ncols() < 3 || r.len() < nc || samples < 2 || alo.len() != na
        || ahi.len() != na || st.iter().any(|&v| v < 0 || v as usize > nd) || (1..st.len()).any(|i| st[i] < st[i - 1])
        || ar.iter().any(|&v| v < 0 || v as usize >= nc)
        || (0..na).any(|k| alo[k] < 0 || ahi[k] < alo[k] || ahi[k] as usize >= n.max(1)) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "rail_crossing_runs: xy (n, 2), z (n,), pts (m, 3), starts rising within pts, reach (C,), anchors within \
             the part and the roads, samples >= 2"));
    }
    let dv: Vec<[f64; 3]> = (0..nd).map(|j| [d[[j, 0]], d[[j, 1]], d[[j, 2]]]).collect();
    let sv: Vec<usize> = st.iter().map(|&v| v as usize).collect();
    let rv: Vec<f64> = r.iter().copied().collect();
    let xv: Vec<[f64; 2]> = (0..n).map(|i| [x[[i, 0]], x[[i, 1]]]).collect();
    let zv: Vec<f64> = zz.iter().copied().collect();
    let anchors: Vec<(usize, usize, usize)> = (0..na).map(|k| (ar[k] as usize, alo[k] as usize, ahi[k] as usize)).collect();
    let (blk, runs, ts, te) = py.detach(|| {
        // per road with anchors (in road order): its own test, the runs of it holding an anchored point kept
        let mut roads: Vec<usize> = anchors.iter().map(|a| a.0).collect();
        roads.sort_unstable();
        roads.dedup();
        let mut owner: Vec<Vec<usize>> = vec![Vec::new(); n];
        // (the track's direction per point: a section's own for points along it, the neighbours' at a vertex)
        let dirs: Vec<[f64; 2]> = (0..n).map(|i| {
            let (a, b) = (i.saturating_sub(1), (i + 1).min(n - 1));
            [xv[b][0] - xv[a][0], xv[b][1] - xv[a][1]]
        }).collect();
        let mut s = vec![0.0f64; n];        // (arc lengths: a run reaches at most max_run past its anchor)
        for i in 1..n {
            s[i] = s[i - 1] + (xv[i][0] - xv[i - 1][0]).dhypot(xv[i][1] - xv[i - 1][1]);
        }
        for &c in &roads {
            let on = road_on(c, &xv, &dirs, &zv, &dv, &sv, &rv, dz, rail_out);
            let mut keep = vec![false; n];
            for &(_c, lo, hi) in anchors.iter().filter(|a| a.0 == c) {
                let hi = hi.min(n - 1);
                let (smin, smax) = (s[lo] - max_run, s[hi] + max_run);
                for i in lo..=hi {
                    if !on[i] {
                        continue;
                    }
                    let (mut a, mut b) = (i, i);
                    while a > 0 && on[a - 1] && s[a - 1] >= smin { a -= 1; }
                    while b + 1 < n && on[b + 1] && s[b + 1] <= smax { b += 1; }
                    for q in a..=b { keep[q] = true; }
                }
            }
            for i in 0..n {
                if keep[i] { owner[i].push(c); }
            }
        }
        let blk: Vec<bool> = owner.iter().map(|o| !o.is_empty()).collect();
        let t: Vec<f64> = (1..samples).map(|i| i as f64 / samples as f64).collect();
        let mut runs: Vec<[i64; 2]> = Vec::new();
        let (mut ts, mut te) = (Vec::new(), Vec::new());
        let mut i = 0usize;
        while i < n {
            if !blk[i] { i += 1; continue; }
            let a = i;
            while i + 1 < n && blk[i + 1] { i += 1; }
            let b = i;
            i += 1;
            let mut mine: Vec<usize> = (a..=b).flat_map(|q| owner[q].iter().copied()).collect();
            mine.sort_unstable();
            mine.dedup();
            // the run's roads' test at points of a section (k -> k + 1), bed heights interpolated
            let test = |k: usize, tt: &[f64]| -> Vec<bool> {
                let (dx, dy, dzb) = (xv[k + 1][0] - xv[k][0], xv[k + 1][1] - xv[k][1], zv[k + 1] - zv[k]);
                let p: Vec<[f64; 2]> = tt.iter().map(|&s| [xv[k][0] + dx * s, xv[k][1] + dy * s]).collect();
                let zp: Vec<f64> = tt.iter().map(|&s| zv[k] + dzb * s).collect();
                let dk = vec![[dx, dy]; tt.len()];
                let mut on = vec![false; tt.len()];
                for &c in &mine {
                    let got = road_on(c, &p, &dk, &zp, &dv, &sv, &rv, dz, rail_out);
                    for (o, g) in on.iter_mut().zip(got) { *o = *o || g; }
                }
                on
            };
            let refine = |k: usize, entering: bool| -> f64 {
                // entering: off at t = 0, on at t = 1 (the section before the run); else on at 0, off at 1
                let on = test(k, &t);
                let (mut t_off, mut t_on) = if entering {
                    match on.iter().position(|&v| v) {
                        Some(j) => (if j == 0 { 0.0 } else { t[j - 1] }, t[j]),
                        None => (t[t.len() - 1], 1.0),
                    }
                } else {
                    match on.iter().rposition(|&v| v) {
                        Some(j) => (if j + 1 < t.len() { t[j + 1] } else { 1.0 }, t[j]),
                        None => (t[0], 0.0),
                    }
                };
                for _ in 0..bisect {
                    let tm = 0.5 * (t_off + t_on);
                    if test(k, &[tm])[0] { t_on = tm; } else { t_off = tm; }
                }
                t_off
            };
            ts.push(if a > 0 { refine(a - 1, true) } else { f64::NAN });
            te.push(if b + 1 < n { refine(b, false) } else { f64::NAN });
            runs.push([a as i64, b as i64]);
        }
        (blk, runs, ts, te)
    });
    let nr = runs.len();
    let flat: Vec<i64> = runs.into_iter().flatten().collect();
    Ok((blk.into_pyarray(py), Array2::from_shape_vec((nr, 2), flat).unwrap().into_pyarray(py), ts.into_pyarray(py),
        te.into_pyarray(py)))
}

/// railways._road_ends (BNE_RAIL_ROAD_FIT) for every candidate end at once: the track ending at p[e] (running into it
/// along the unit d[e], its bed z_end[e], part part[e] of rails (m, 2) with beds rz (m,), part after part: off (R + 1,))
/// goes on past a road: the line ahead (p + d * np.arange(step, ahead_m + 1e-9, step)) meets a carriageway (ca -> cb,
/// half width ch) and leaves it again (its far edge), and beyond that edge, within ahead_m, another part has an END
/// (its first or last point) within join_m of the line, the part running on from it away from the road along d
/// (cos >= cos_min), its gauge (gauge per part) within 1 cm of this part's and its rail heads within dz of z_end: the
/// same track's other end on the far side of a paved-over crossing. A track passing by (a through line beside a real
/// siding) has no end there. (A lifecycle cut's end is known by its provenance, not here.)
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn rail_road_ends(p: PyReadonlyArray2<'_, f64>, d: PyReadonlyArray2<'_, f64>, z_end: PyReadonlyArray1<'_, f64>,
                      part: PyReadonlyArray1<'_, i64>, rails: PyReadonlyArray2<'_, f64>, rz: PyReadonlyArray1<'_, f64>,
                      off: PyReadonlyArray1<'_, i64>, ca: PyReadonlyArray2<'_, f64>, cb: PyReadonlyArray2<'_, f64>,
                      ch: PyReadonlyArray1<'_, f64>, gauge: PyReadonlyArray1<'_, f64>, step: f64, ahead_m: f64,
                      join_m: f64, cos_min: f64, dz: f64) -> PyResult<Vec<bool>> {
    let (p, d, ze, part, q, qz, off) = (p.as_array(), d.as_array(), z_end.as_array(), part.as_array(), rails.as_array(),
                                        rz.as_array(), off.as_array());
    let gauge = gauge.as_array();
    let ne = p.nrows();
    let nq = q.nrows();
    if p.ncols() < 2 || d.nrows() != ne || d.ncols() < 2 || ze.len() != ne || part.len() != ne || q.ncols() < 2
        || qz.len() != nq || off.iter().any(|&v| v < 0 || v as usize > nq) || (1..off.len()).any(|i| off[i] < off[i - 1])
        || gauge.len() + 1 != off.len().max(1) || part.iter().any(|&v| v < 0 || v as usize >= gauge.len()) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "rail_road_ends: p (E, 2), d (E, 2), z_end (E,), part (E,), rails (m, 2), rz (m,), off rising within rails, \
             gauge (R,)"));
    }
    let segs = Segs::new(&ca.as_array(), &cb.as_array(), &ch.as_array());
    let steps = np_arange(step, ahead_m + 1e-9, step);
    let nr = off.len().saturating_sub(1);
    let mut out = vec![false; ne];
    for e in 0..ne {
        let (px, py_, dx, dy) = (p[[e, 0]], p[[e, 1]], d[[e, 0]], d[[e, 1]]);
        let on: Vec<bool> = steps.iter().map(|&s| segs.near(px + dx * s, py_ + dy * s, 0.0)).collect();
        let first = match on.iter().position(|&v| v) {
            Some(i) => i,
            None => continue,
        };
        let mut far = first;
        while far + 1 < on.len() && on[far + 1] {
            far += 1;
        }
        if far + 1 >= on.len() {
            continue;                   // (no far edge within the reach)
        }
        let beyond = steps[far];
        'parts: for o in 0..nr {
            if o as i64 == part[e] {
                continue;
            }
            let (j0, j1) = (off[o] as usize, off[o + 1] as usize);
            if j1 < j0 + 2 {
                continue;
            }
            if (gauge[o] - gauge[part[e] as usize]).abs() > 0.01 {
                continue;
            }
            // (its two ends, each with the direction the part runs on from it)
            for (j, k) in [(j0, j0 + 1), (j1 - 1, j1 - 2)] {
                let (vx, vy) = (q[[j, 0]] - px, q[[j, 1]] - py_);
                let along = vx * dx + vy * dy;
                if !(along > beyond && along <= ahead_m) {
                    continue;
                }
                if (vx * dy - vy * dx).abs() > join_m {
                    continue;
                }
                let (tx, ty) = (q[[k, 0]] - q[[j, 0]], q[[k, 1]] - q[[j, 1]]);
                let tl = tx.dhypot(ty);
                if !(tl > 1e-9) || (tx * dx + ty * dy) / tl < cos_min {
                    continue;
                }
                if !((qz[j] - ze[e]).abs() <= dz) {
                    continue;
                }
                out[e] = true;
                break 'parts;
            }
        }
    }
    Ok(out)
}
