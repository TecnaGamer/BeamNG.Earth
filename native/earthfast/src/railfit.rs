//! Railway-at-road fixes (server BNE_RAIL_ROAD_FIT; the user's in-game reports r63 / r64 / r92, 2026-11-17): the
//! decisions of railways._run_ends (a level crossing's exact edge on a track), railways.dead_ends' road test (no buffer
//! stop where a track ends at a road), rails._deck_line (the crossing deck from edge to edge) and railstations._synth's
//! trim (a worked-out platform off the roads). The numbers are the Python versions' to the bit, in their order:
//! elementwise products and sums as numpy's, np.arange's fill (start + i * delta), np.interp's formula, libm hypot,
//! the carriageway test of roads._Segs.near (earthfast.points_near_segments' expression) and the crossing of
//! railstations._crossings (station_crossings' expression).
use crate::dm::Dm;
use crate::gradesep::np_interp;
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

/// railways._run_ends: (xy', zb', blk', src) with a point added at each edge of a level crossing (where blk changes
/// between two points k, k + 1): `samples` - 1 points along the section (t = i / samples) tried with railways._blocked
/// (earthfast.near_crossers on the crossers' pack: pts (n, 3), starts, reach, dz), then `bisect` halvings between the
/// last point off and the first on; the point off kept where it falls inside the section. src: each point's index in
/// xy, -1 for an added one.
#[pyfunction]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
pub fn rail_run_ends<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, zb: PyReadonlyArray1<'py, f64>,
                          blk: PyReadonlyArray1<'py, bool>, pts: PyReadonlyArray2<'py, f64>,
                          starts: PyReadonlyArray1<'py, i64>, reach: PyReadonlyArray1<'py, f64>, dz: f64,
                          samples: usize, bisect: usize)
                          -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<bool>>,
                                       Bound<'py, PyArray1<i64>>)> {
    let (x, z, b, d, st, r) = (xy.as_array(), zb.as_array(), blk.as_array(), pts.as_array(), starts.as_array(),
                               reach.as_array());
    let n = x.nrows();
    let nd = d.nrows();
    let nc = st.len().saturating_sub(1);
    if x.ncols() < 2 || z.len() != n || b.len() != n || d.ncols() < 3 || r.len() < nc || samples < 2
        || st.iter().any(|&v| v < 0 || v as usize > nd) || (1..st.len()).any(|i| st[i] < st[i - 1]) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "rail_run_ends: xy (n, 2), zb (n,), blk (n,), pts (m, 3), starts rising within pts, reach (C,), samples >= 2"));
    }
    let dv: Vec<[f64; 3]> = (0..nd).map(|j| [d[[j, 0]], d[[j, 1]], d[[j, 2]]]).collect();
    let sv: Vec<usize> = st.iter().map(|&v| v as usize).collect();
    let rv: Vec<f64> = r.iter().copied().collect();
    let xv: Vec<[f64; 2]> = (0..n).map(|i| [x[[i, 0]], x[[i, 1]]]).collect();
    let zv: Vec<f64> = z.iter().copied().collect();
    let bv: Vec<bool> = b.iter().copied().collect();
    let (rows, zs, bs, src) = py.detach(|| {
        let blocked = |p: &[[f64; 2]], zz: &[f64]| crate::nearcross::near_crossers(p, zz, &dv, &sv, &rv, dz);
        // t = np.arange(1, samples, dtype=float64) / samples (exact integers over samples)
        let t: Vec<f64> = (1..samples).map(|i| i as f64 / samples as f64).collect();
        let mut tt = Vec::with_capacity(t.len() + 2);
        tt.push(0.0);
        tt.extend_from_slice(&t);
        tt.push(1.0);
        let mut add: Vec<Option<f64>> = vec![None; n];
        for k in 0..n.saturating_sub(1) {
            if bv[k] == bv[k + 1] {
                continue;
            }
            let (dx, dy, dzb) = (xv[k + 1][0] - xv[k][0], xv[k + 1][1] - xv[k][1], zv[k + 1] - zv[k]);
            let p: Vec<[f64; 2]> = t.iter().map(|&ti| [xv[k][0] + dx * ti, xv[k][1] + dy * ti]).collect();
            let zz: Vec<f64> = t.iter().map(|&ti| zv[k] + dzb * ti).collect();
            let on = blocked(&p, &zz);
            let any = on.iter().any(|&v| v);
            let (mut t_off, mut t_on);
            if !bv[k] {
                let i = if any { on.iter().position(|&v| v).unwrap() + 1 } else { t.len() + 1 };
                t_off = tt[i - 1];
                t_on = tt[i];
            } else {
                let i = if any { t.len() - 1 - on.iter().rev().position(|&v| v).unwrap() + 1 } else { 0 };
                t_on = tt[i];
                t_off = tt[i + 1];
            }
            for _ in 0..bisect {
                let tm = 0.5 * (t_off + t_on);
                let pm = [xv[k][0] + dx * tm, xv[k][1] + dy * tm];
                if blocked(&[pm], &[zv[k] + dzb * tm])[0] {
                    t_on = tm;
                } else {
                    t_off = tm;
                }
            }
            if 0.0 < t_off && t_off < 1.0 {
                add[k] = Some(t_off);
            }
        }
        let mut rows: Vec<f64> = Vec::with_capacity(2 * n + 8);
        let (mut zs, mut bs, mut src) = (Vec::with_capacity(n + 4), Vec::with_capacity(n + 4), Vec::with_capacity(n + 4));
        for k in 0..n {
            rows.extend_from_slice(&xv[k]);
            zs.push(zv[k]);
            bs.push(bv[k]);
            src.push(k as i64);
            if let Some(tb) = add[k] {
                rows.push(xv[k][0] + (xv[k + 1][0] - xv[k][0]) * tb);
                rows.push(xv[k][1] + (xv[k + 1][1] - xv[k][1]) * tb);
                zs.push(zv[k] + (zv[k + 1] - zv[k]) * tb);
                bs.push(false);
                src.push(-1);
            }
        }
        (rows, zs, bs, src)
    });
    let m = zs.len();
    Ok((Array2::from_shape_vec((m, 2), rows).unwrap().into_pyarray(py), zs.into_pyarray(py), bs.into_pyarray(py),
        src.into_pyarray(py)))
}

/// railways._across_road for every candidate end at once: end e at p[e] running into it along the unit d[e] (part
/// part[e] of the tracks: rails (m, 2) part after part, off (R + 1,)) ends at a road when the line ahead (points
/// p + d * np.arange(step, ahead_m + 1e-9, step)) meets a carriageway (ca -> cb, half width ch: within it) within
/// road_end_m, or anywhere with another part's point within join_m of the line from the first point on it to the last
/// point ahead.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn rail_road_ends(p: PyReadonlyArray2<'_, f64>, d: PyReadonlyArray2<'_, f64>, part: PyReadonlyArray1<'_, i64>,
                      rails: PyReadonlyArray2<'_, f64>, off: PyReadonlyArray1<'_, i64>, ca: PyReadonlyArray2<'_, f64>,
                      cb: PyReadonlyArray2<'_, f64>, ch: PyReadonlyArray1<'_, f64>, step: f64, ahead_m: f64,
                      road_end_m: f64, join_m: f64) -> PyResult<Vec<bool>> {
    let (p, d, part, q, off) = (p.as_array(), d.as_array(), part.as_array(), rails.as_array(), off.as_array());
    let ne = p.nrows();
    let nq = q.nrows();
    if p.ncols() < 2 || d.nrows() != ne || d.ncols() < 2 || part.len() != ne || q.ncols() < 2
        || off.iter().any(|&v| v < 0 || v as usize > nq) || (1..off.len()).any(|i| off[i] < off[i - 1]) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "rail_road_ends: p (E, 2), d (E, 2), part (E,), rails (m, 2), off rising within rails"));
    }
    let segs = Segs::new(&ca.as_array(), &cb.as_array(), &ch.as_array());
    let steps = np_arange(step, ahead_m + 1e-9, step);
    let nr = off.len().saturating_sub(1);
    let mut out = vec![false; ne];
    for e in 0..ne {
        let (px, py_, dx, dy) = (p[[e, 0]], p[[e, 1]], d[[e, 0]], d[[e, 1]]);
        let ahead: Vec<[f64; 2]> = steps.iter().map(|&s| [px + dx * s, py_ + dy * s]).collect();
        let first = match ahead.iter().position(|a| segs.near(a[0], a[1], 0.0)) {
            Some(i) => i,
            None => continue,
        };
        if ((first + 1) as f64) * step <= road_end_m {
            out[e] = true;
            continue;
        }
        let a = ahead[first];
        let b = ahead[ahead.len() - 1];
        let (ab0, ab1) = (b[0] - a[0], b[1] - a[1]);
        let l2 = (ab0 * ab0 + ab1 * ab1).max(1e-9);
        'parts: for o in 0..nr {
            if o as i64 == part[e] {
                continue;
            }
            let (j0, j1) = (off[o] as usize, off[o + 1] as usize);
            if j1 <= j0 {
                continue;
            }
            // (float(np.min(dnp.hypot(...))) < join_m: np.min of a NaN is NaN, and NaN < join_m is false)
            let mut best = f64::INFINITY;
            for j in j0..j1 {
                let (q0, q1) = (q[[j, 0]], q[[j, 1]]);
                let t = (((q0 - a[0]) * ab0 + (q1 - a[1]) * ab1) / l2).clamp(0.0, 1.0);
                let dd = ((a[0] + t * ab0) - q0).dhypot((a[1] + t * ab1) - q1);
                if dd.is_nan() || best.is_nan() {
                    best = f64::NAN;
                } else if dd < best {
                    best = dd;
                }
            }
            if best < join_m {
                out[e] = true;
                break 'parts;
            }
        }
    }
    Ok(out)
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

/// rails._deck_line: the deck line of the street run piece[a..b] (piece (n, 2)): the points every `fine` m from
/// s[a] - search to s[b] + search (np.arange's), those within a carriageway (ca -> cb, half width ch) + pad, the run of
/// them holding the one nearest the run's middle (the first nearest), each end bisected `bisect` times towards the
/// point off it (unless at the window's end); the piece's points strictly between and the two ends. None without one.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn rail_deck_line<'py>(py: Python<'py>, piece: PyReadonlyArray2<'py, f64>, a: usize, b: usize,
                           ca: PyReadonlyArray2<'py, f64>, cb: PyReadonlyArray2<'py, f64>, ch: PyReadonlyArray1<'py, f64>,
                           pad: f64, fine: f64, search: f64, bisect: usize) -> PyResult<Option<Bound<'py, PyArray2<f64>>>> {
    let pc = piece.as_array();
    let n = pc.nrows();
    if pc.ncols() < 2 || n < 2 || a >= n || b >= n {
        return Err(pyo3::exceptions::PyValueError::new_err("rail_deck_line: piece (n >= 2, 2), a, b < n"));
    }
    let segs = Segs::new(&ca.as_array(), &cb.as_array(), &ch.as_array());
    let xs: Vec<f64> = (0..n).map(|i| pc[[i, 0]]).collect();
    let ys: Vec<f64> = (0..n).map(|i| pc[[i, 1]]).collect();
    let mut s = vec![0.0f64; n];
    for i in 1..n {
        s[i] = s[i - 1] + (xs[i] - xs[i - 1]).dhypot(ys[i] - ys[i - 1]);
    }
    let lo = { let v = s[a] - search; if v > 0.0 { v } else { 0.0 } };         // (Python's max(0.0, v))
    let hi = { let v = s[b] + search; if v < s[n - 1] { v } else { s[n - 1] } };   // (min(s[-1], v))
    if hi - lo < fine {
        return Ok(None);
    }
    let t = np_arange(lo, hi + 1e-9, fine);
    let on: Vec<bool> = t.iter().map(|&v| segs.near(np_interp(v, &s, &xs), np_interp(v, &s, &ys), pad)).collect();
    if !on.iter().any(|&v| v) {
        return Ok(None);
    }
    let mid = 0.5 * (s[a] + s[b]);
    let mut k = usize::MAX;
    let mut best = f64::INFINITY;
    for (i, &v) in on.iter().enumerate() {      // (np.argmin: the first minimum, the first NaN if any)
        if !v {
            continue;
        }
        let dd = (t[i] - mid).abs();
        if dd.is_nan() {
            k = i;
            break;
        }
        if k == usize::MAX || dd < best {
            best = dd;
            k = i;
        }
    }
    let (mut i, mut j) = (k, k);
    while i > 0 && on[i - 1] {
        i -= 1;
    }
    while j + 1 < on.len() && on[j + 1] {
        j += 1;
    }
    if j <= i {
        return Ok(None);
    }
    let edge = |k: usize, out: isize| -> f64 {
        if out < 0 || out as usize >= t.len() {
            return t[k];
        }
        let (mut t_on, mut t_off) = (t[k], t[out as usize]);
        for _ in 0..bisect {
            let tm = 0.5 * (t_on + t_off);
            if segs.near(np_interp(tm, &s, &xs), np_interp(tm, &s, &ys), pad) {
                t_on = tm;
            } else {
                t_off = tm;
            }
        }
        t_on
    };
    let ta = edge(i, i as isize - 1);
    let tb = edge(j, j as isize + 1);
    let mut rows: Vec<f64> = vec![np_interp(ta, &s, &xs), np_interp(ta, &s, &ys)];
    for q in 0..n {
        if s[q] > ta && s[q] < tb {
            rows.push(xs[q]);
            rows.push(ys[q]);
        }
    }
    rows.push(np_interp(tb, &s, &xs));
    rows.push(np_interp(tb, &s, &ys));
    let m = rows.len() / 2;
    Ok(Some(Array2::from_shape_vec((m, 2), rows).unwrap().into_pyarray(py)))
}

/// grading.seam_on_ground (BNE_RAIL_SEAM_DEM): the heights railroadseam.couple is to read for the roads' points (pts
/// (m, 2), their plans' zs (m,), the grader's DEM there dem (m,), road after road: off (R + 1,), half widths hw (R,)),
/// for the bed on xy (n, 2) whose own ground (the DEM) is ground (n,), its band's half width `half`: per road point
/// within reach + its half width + half of a bed vertex, the room to the nearest bed vertex (the first of equals,
/// libm hypot, less both half widths, at least 0) allows room / slope + tol of height between the two bands; the
/// natural difference of the ground (dem - that bed vertex's ground) past that allowance is taken off the road's
/// height (the ground's own slope beside the track: a road up the hillside asks nothing of the bed); within it nothing
/// is. Points farther keep their zs.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
pub fn rail_seam_natural<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, ground: PyReadonlyArray1<'py, f64>,
                              half: f64, pts: PyReadonlyArray2<'py, f64>, zs: PyReadonlyArray1<'py, f64>,
                              dem: PyReadonlyArray1<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                              hw: PyReadonlyArray1<'py, f64>, reach: f64, slope: f64, tol: f64)
                              -> PyResult<Bound<'py, PyArray1<f64>>> {
    let (b, g, p, zs, dem, off, hw) = (xy.as_array(), ground.as_array(), pts.as_array(), zs.as_array(), dem.as_array(),
                                       off.as_array(), hw.as_array());
    let (n, m) = (b.nrows(), p.nrows());
    let nr = off.len().saturating_sub(1);
    if b.ncols() < 2 || g.len() != n || p.ncols() < 2 || zs.len() != m || dem.len() != m || hw.len() < nr
        || off.iter().any(|&v| v < 0 || v as usize > m) || (1..off.len()).any(|i| off[i] < off[i - 1]) {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "rail_seam_natural: xy (n, 2), ground (n,), pts (m, 2), zs (m,), dem (m,), off rising within pts, hw (R,)"));
    }
    let mut out: Vec<f64> = zs.iter().copied().collect();
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for i in 0..n {
        x0 = x0.min(b[[i, 0]]); x1 = x1.max(b[[i, 0]]);
        y0 = y0.min(b[[i, 1]]); y1 = y1.max(b[[i, 1]]);
    }
    for r in 0..nr {
        let h = hw[r];
        let g_ = reach + h + half + 1.0;     // (a point past the bed's box by more than its reach: no vertex within it)
        for j in off[r] as usize..off[r + 1] as usize {
            let (px, py_) = (p[[j, 0]], p[[j, 1]]);
            if px < x0 - g_ || px > x1 + g_ || py_ < y0 - g_ || py_ > y1 + g_ {
                continue;
            }
            let (mut best, mut bi) = (f64::INFINITY, usize::MAX);
            for i in 0..n {
                let d = (b[[i, 0]] - px).dhypot(b[[i, 1]] - py_);
                if d < best {
                    best = d;
                    bi = i;
                }
            }
            if bi == usize::MAX || !(best <= reach + h + half) {
                continue;
            }
            let room = (best - h - half).max(0.0);
            let allow = room / slope + tol;
            let nat = dem[j] - g[bi];
            let excess = nat.abs() - allow;
            if excess > 0.0 {
                out[j] = zs[j] - if nat < 0.0 { -excess } else { excess };
            }
        }
    }
    Ok(out.into_pyarray(py))
}
