//! roadmesh.ribbons' resampling and mitred normals for many chunks at once (roadmesh._resample + _normals: numpy
//! calls per chunk were ~0.9 s of 8 Bratislava road tiles, 2026-10-28). The same float64 operations as numpy's, in
//! its order: the results equal the numpy version's bit for bit.

use numpy::{IntoPyArray, PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::prelude::*;

type Row = (usize, [f64; 2], [f64; 2]);

/// Replace isolated clamped miters by a bevel on the outside and the true offset intersection on the inside.
/// Inner samples covered by the join collapse onto that intersection instead of folding backwards. A join must
/// fit on its straight arms, and joins may not overlap; short or ambiguous mapped turns keep the existing layout.
fn bevel_rows(xs: &[f64], ys: &[f64], nx: &[f64], ny: &[f64], s: &[f64], half: f64) -> Option<Vec<Row>> {
    let n = xs.len();
    if n < 3 || half <= 0.0 { return None; }
    let mut normals = Vec::with_capacity(n - 1);
    for i in 0..n - 1 {
        let (dx, dy) = (xs[i + 1] - xs[i], ys[i + 1] - ys[i]);
        let len = dx.hypot(dy);
        if len < 1e-9 { return None; }
        normals.push([-dy / len, dx / len]);
    }
    let dot = |a: [f64; 2], b: [f64; 2]| a[0] * b[0] + a[1] * b[1];
    let mut joins = Vec::new();
    for i in 1..n - 1 {
        let (a, b) = (normals[i - 1], normals[i]);
        let cos = dot(a, b).clamp(-1.0, 1.0);
        if cos >= -0.5 || cos <= -1.0 + 1e-8 { continue; }
        let trim = half * ((1.0 - cos) / (1.0 + cos)).sqrt();
        let (mut start, mut end) = (i - 1, i + 1);
        while start > 0 && dot(normals[start - 1], a) > 1.0 - 1e-10 { start -= 1; }
        while end < n - 1 && dot(normals[end], b) > 1.0 - 1e-10 { end += 1; }
        if trim > s[i] - s[start] || trim > s[end] - s[i] { continue; }
        let turn = a[0] * b[1] - a[1] * b[0];
        // (a + b) / (1 + cos) is the intersection of the two constant-width offset lines.
        let side = turn.signum();
        let inner = [xs[i] + side * half * (a[0] + b[0]) / (1.0 + cos),
                     ys[i] + side * half * (a[1] + b[1]) / (1.0 + cos)];
        joins.push((i, start, end, trim, side, inner));
    }
    if joins.is_empty() { return None; }
    let mut left: Vec<_> = (0..n).map(|i| [xs[i] + nx[i] * half, ys[i] + ny[i] * half]).collect();
    let mut right: Vec<_> = (0..n).map(|i| [xs[i] - nx[i] * half, ys[i] - ny[i] * half]).collect();
    let mut extra = vec![None; n];
    let mut changed = false;
    for &(i, start, end, trim, side, inner) in &joins {
        if joins.iter().any(|&(j, _, _, other, _, _)| j != i && (s[j] - s[i]).abs() <= trim + other) { continue; }
        let (a, b) = (normals[i - 1], normals[i]);
        for j in start..=end {
            if (s[j] - s[i]).abs() <= trim + 1e-8 {
                if side > 0.0 { left[j] = inner; } else { right[j] = inner; }
            }
        }
        if side > 0.0 {
            right[i] = [xs[i] - half * a[0], ys[i] - half * a[1]];
            extra[i] = Some((inner, [xs[i] - half * b[0], ys[i] - half * b[1]]));
        } else {
            left[i] = [xs[i] + half * a[0], ys[i] + half * a[1]];
            extra[i] = Some(([xs[i] + half * b[0], ys[i] + half * b[1]], inner));
        }
        changed = true;
    }
    if !changed { return None; }
    let mut rows = Vec::with_capacity(n + joins.len());
    for i in 0..n {
        rows.push((i, left[i], right[i]));
        if let Some((l, r)) = extra[i] { rows.push((i, l, r)); }
    }
    Some(rows)
}

/// numpy.interp for increasing xp (x at or past the last xp: fp's last value).
fn interp(x: f64, xp: &[f64], fp: &[f64]) -> f64 {
    let n = xp.len();
    if x <= xp[0] {
        return fp[0];
    }
    if x >= xp[n - 1] {
        return fp[n - 1];
    }
    // the last j with xp[j] <= x
    let mut lo = 0usize;
    let mut hi = n - 1;
    while hi - lo > 1 {
        let mid = (lo + hi) / 2;
        if xp[mid] <= x { lo = mid } else { hi = mid }
    }
    let slope = (fp[lo + 1] - fp[lo]) / (xp[lo + 1] - xp[lo]);
    let v = slope * (x - xp[lo]) + fp[lo];
    if v.is_nan() {
        // (numpy: a zero-length step's slope is nan: then the side value)
        let w = slope * (x - xp[lo + 1]) + fp[lo + 1];
        if w.is_nan() && fp[lo] == fp[lo + 1] { return fp[lo]; }
        return w;
    }
    v
}

/// Per chunk c (points pts[off[c]..off[c + 1]] (k, 3)): resampled every `step` m with its ends kept (numpy's
/// linspace), and the left / right edges at half[c] m along the mitred normals (cos clipped at 0.5). Returns (left
/// (m, 2), right (m, 2), arc s (m,), z (m,), out offsets (chunks + 1)); eligible sharp joins add a bevel row. A chunk
/// of zero length gives its first point.
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn ribbon_edges<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, off: PyReadonlyArray1<'py, i64>,
                         half: PyReadonlyArray1<'py, f64>, step: f64)
                         -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>,
                             Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<i64>>) {
    let (pts, off, half) = (pts.as_array(), off.as_array(), half.as_array());
    let (l, r, s_out, z_out, o) = py.allow_threads(|| {
        let mut l: Vec<f64> = Vec::new();
        let mut r: Vec<f64> = Vec::new();
        let mut s_out: Vec<f64> = Vec::new();
        let mut z_out: Vec<f64> = Vec::new();
        let mut o: Vec<i64> = vec![0];
        for c in 0..off.len().saturating_sub(1) {
            let (a, b) = (off[c] as usize, off[c + 1] as usize);
            let k = b - a;
            let (px, py_, pz): (Vec<f64>, Vec<f64>, Vec<f64>) =
                ((a..b).map(|i| pts[[i, 0]]).collect(), (a..b).map(|i| pts[[i, 1]]).collect(),
                 (a..b).map(|i| pts[[i, 2]]).collect());
            let mut s = vec![0.0f64; k];
            for i in 1..k {
                s[i] = s[i - 1] + (px[i] - px[i - 1]).hypot(py_[i] - py_[i - 1]);
            }
            let (xs, ys, ts, zs): (Vec<f64>, Vec<f64>, Vec<f64>, Vec<f64>);
            if k == 0 {
                o.push(l.len() as i64 / 2);
                continue;
            }
            if s[k - 1] <= 1e-6 {
                xs = vec![px[0]]; ys = vec![py_[0]]; ts = vec![s[0]]; zs = vec![pz[0]];
            } else {
                let n = 1usize.max((s[k - 1] / step).ceil() as usize);
                // numpy.linspace(0, stop, n + 1): step = stop / n, t[i] = i * step, the last exactly stop
                let st = s[k - 1] / n as f64;
                let mut t: Vec<f64> = (0..=n).map(|i| if i == n { s[k - 1] } else { i as f64 * st }).collect();
                // A lattice sample just either side of a hairpin cuts across its apex. Retain the sharp mapped
                // knot so its bevel is independent of whether the road's length happens to divide by `step`.
                let mut added_knot = false;
                for i in 1..k - 1 {
                    let (ax, ay) = (px[i] - px[i - 1], py_[i] - py_[i - 1]);
                    let (bx, by) = (px[i + 1] - px[i], py_[i + 1] - py_[i]);
                    let lengths = ax.hypot(ay) * bx.hypot(by);
                    if lengths > 1e-12 && (ax * bx + ay * by) / lengths < -0.5 - 1e-12 {
                        t.retain(|v| (*v - s[i]).abs() > 1e-8);
                        t.push(s[i]);
                        added_knot = true;
                    }
                }
                if added_knot { t.sort_by(f64::total_cmp); }
                xs = t.iter().map(|&x| interp(x, &s, &px)).collect();
                ys = t.iter().map(|&x| interp(x, &s, &py_)).collect();
                zs = t.iter().map(|&x| interp(x, &s, &pz)).collect();
                ts = t;
            }
            let m = xs.len();
            // _normals (times half)
            let mut nx = vec![0.0f64; m];
            let mut ny = vec![0.0f64; m];
            let mut clamped = false;
            if m >= 2 {
                let mut snx = vec![0.0f64; m - 1];
                let mut sny = vec![0.0f64; m - 1];
                for i in 0..m - 1 {
                    let (dx, dy) = (xs[i + 1] - xs[i], ys[i + 1] - ys[i]);
                    let mut len = dx.hypot(dy);
                    if len < 1e-9 { len = 1e-9; }
                    let (tx, ty) = (dx / len, dy / len);
                    snx[i] = -ty;
                    sny[i] = tx;
                }
                nx[0] = snx[0]; ny[0] = sny[0];
                nx[m - 1] = snx[m - 2]; ny[m - 1] = sny[m - 2];
                for i in 1..m - 1 {
                    let (mx, my) = (snx[i - 1] + snx[i], sny[i - 1] + sny[i]);
                    let mut ml = mx.hypot(my);
                    if ml < 1e-9 { ml = 1.0; }
                    let (mx, my) = (mx / ml, my / ml);
                    let raw_cos = mx * snx[i] + my * sny[i];
                    clamped |= raw_cos < 0.5;
                    let cos = raw_cos.clamp(0.5, 1.0);
                    nx[i] = mx / cos;
                    ny[i] = my / cos;
                }
            }
            let bevel = if clamped { bevel_rows(&xs, &ys, &nx, &ny, &ts, half[c]) } else { None };
            if let Some(rows) = bevel {
                for (i, lp, rp) in rows {
                    l.extend_from_slice(&lp); r.extend_from_slice(&rp);
                    s_out.push(ts[i]); z_out.push(zs[i]);
                }
            } else {
                for i in 0..m {
                    let (hx, hy) = (nx[i] * half[c], ny[i] * half[c]);
                    l.push(xs[i] + hx); l.push(ys[i] + hy);
                    r.push(xs[i] - hx); r.push(ys[i] - hy);
                    s_out.push(ts[i]);
                    z_out.push(zs[i]);
                }
            }
            o.push(s_out.len() as i64);
        }
        (l, r, s_out, z_out, o)
    });
    let m = s_out.len();
    (numpy::ndarray::Array2::from_shape_vec((m, 2), l).expect("left").into_pyarray(py),
     numpy::ndarray::Array2::from_shape_vec((m, 2), r).expect("right").into_pyarray(py),
     s_out.into_pyarray(py), z_out.into_pyarray(py), o.into_pyarray(py))
}
