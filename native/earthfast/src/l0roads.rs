//! L0 roads ("fake it until it loads", retro_optimization_study I): a provisional block's road corridors flattened
//! into its own heights before any road plan exists, so the roads painted into it can be driven without the
//! elevation data's bumps and side slopes for the 20-120 s until the graded block (grading.py's plans) replaces it.
//! A cheap stand-in for the grading, not a copy: each road's profile is its own centreline's heights in the block
//! (a running median, then a Gaussian-weighted local line of the grading's per-class sigma), its core (carriageway +
//! shoulder) laid flat across at that profile, the ground eased back over T_MIN..T_MAX (1:SLOPE_RUN) beyond. A side
//! road ending at a main road takes its height at the main road's edge, eased in along it (the grading's junction
//! snap); where cores overlap the higher class rank wins; the targets are rounded over a few metres last (the
//! junctions' kinks). Measured against the graded blocks (Pittsburgh's South Side Slopes, Genoa, 2026-11-11): bumps on
//! the roads 4.5 / 1.3 a km raw, 0.6 / 0.3 here, 0.9 / 0.5 graded; side slope 6.6 / 3.3 % raw, 1.1 / 0.7 % here.
//! Order-independent and local: a sample's value depends only on the roads within T_MAX of it and their heights within
//! 3 sigma (plus a junction's taper) along them, so two overlapping blocks agree except within that reach of their
//! edges.

use numpy::{PyReadonlyArray1, PyReadonlyArray2, PyReadwriteArray2};
use pyo3::prelude::*;

fn bilinear(u: &numpy::ndarray::ArrayView2<f64>, gx: f64, gy: f64) -> f64 {
    let (ny, nx) = (u.shape()[0], u.shape()[1]);
    if !(gx >= 0.0 && gy >= 0.0 && gx <= (nx - 1) as f64 && gy <= (ny - 1) as f64) {
        return f64::NAN;
    }
    let i0 = (gx.floor() as usize).min(nx - 1);
    let j0 = (gy.floor() as usize).min(ny - 1);
    let i1 = (i0 + 1).min(nx - 1);
    let j1 = (j0 + 1).min(ny - 1);
    let (fx, fy) = (gx - i0 as f64, gy - j0 as f64);
    let a = u[[j0, i0]] * (1.0 - fx) + u[[j0, i1]] * fx;
    let b = u[[j1, i0]] * (1.0 - fx) + u[[j1, i1]] * fx;
    a * (1.0 - fy) + b * fy
}

/// The smoothed profile of one polyline: stations every `step` m of arc from its start (and its end), their heights
/// from the grid (NaN off it), a running median over `median_m`, then a Gaussian of `sigma` (both over the valid
/// stations only). Returns (station arc positions, heights).
fn profile(u: &numpy::ndarray::ArrayView2<f64>, pts: &[[f64; 2]], x0: f64, y0: f64, sq: f64, step: f64,
           median_m: f64, sigma: f64) -> (Vec<f64>, Vec<f64>) {
    let mut cum = vec![0.0f64; pts.len()];
    for k in 1..pts.len() {
        cum[k] = cum[k - 1] + ((pts[k][0] - pts[k - 1][0]).powi(2) + (pts[k][1] - pts[k - 1][1]).powi(2)).sqrt();
    }
    let total = *cum.last().unwrap_or(&0.0);
    let n = ((total / step).floor() as usize) + 1;
    let mut s = Vec::with_capacity(n + 1);
    for k in 0..n {
        s.push(k as f64 * step);
    }
    if total - s[n - 1] > 1e-6 {
        s.push(total);
    }
    let mut z = Vec::with_capacity(s.len());
    let mut seg = 0usize;
    for &sk in &s {
        while seg + 2 < pts.len() && cum[seg + 1] < sk {
            seg += 1;
        }
        let l = (cum[seg + 1] - cum[seg]).max(1e-12);
        let t = ((sk - cum[seg]) / l).clamp(0.0, 1.0);
        let x = pts[seg][0] + t * (pts[seg + 1][0] - pts[seg][0]);
        let y = pts[seg][1] + t * (pts[seg + 1][1] - pts[seg][1]);
        z.push(bilinear(u, (x - x0) / sq, (y - y0) / sq));
    }
    // running median (a deck or a car left in the elevation data: a spike a few samples wide)
    let m = s.len();
    let half_m = (median_m / 2.0).max(0.0);
    let mut med = vec![f64::NAN; m];
    let mut buf: Vec<f64> = Vec::new();
    let (mut lo, mut hi) = (0usize, 0usize);
    for k in 0..m {
        while lo < m && s[lo] < s[k] - half_m - 1e-9 { lo += 1; }
        while hi < m && s[hi] <= s[k] + half_m + 1e-9 { hi += 1; }
        buf.clear();
        buf.extend(z[lo..hi].iter().copied().filter(|v| v.is_finite()));
        if buf.is_empty() { continue; }
        buf.sort_by(|a, b| a.partial_cmp(b).unwrap());
        let c = buf.len();
        med[k] = if c % 2 == 1 { buf[c / 2] } else { 0.5 * (buf[c / 2 - 1] + buf[c / 2]) };
    }
    // a Gaussian-weighted local linear fit over 3 sigma: the plain weighted mean of a road ending on a slope (one-sided
    // window) was pulled up or down the hill by ~0.8 sigma x the grade (a side road 0.7 m off the main road it joins)
    let mut out = vec![f64::NAN; m];
    if sigma <= 1e-6 {
        out.copy_from_slice(&med);
        return (s, out);
    }
    let reach = 3.0 * sigma;
    let inv = 1.0 / (2.0 * sigma * sigma);
    let (mut lo, mut hi) = (0usize, 0usize);
    for k in 0..m {
        while lo < m && s[lo] < s[k] - reach { lo += 1; }
        while hi < m && s[hi] <= s[k] + reach { hi += 1; }
        let (mut s0, mut s1, mut s2, mut t0, mut t1) = (0.0, 0.0, 0.0, 0.0, 0.0);
        for q in lo..hi {
            let v = med[q];
            if !v.is_finite() { continue; }
            let d = s[q] - s[k];
            let w = (-d * d * inv).exp();
            s0 += w;
            s1 += w * d;
            s2 += w * d * d;
            t0 += w * v;
            t1 += w * d * v;
        }
        if s0 <= 0.0 { continue; }
        let det = s0 * s2 - s1 * s1;
        out[k] = if det > 1e-9 * s0 * s0 * step * step {
            let b = (s0 * t1 - s1 * t0) / det;
            (t0 - b * s1) / s0
        } else {
            t0 / s0
        };
    }
    (s, out)
}

fn smoothstep(e: f64) -> f64 {
    let e = e.clamp(0.0, 1.0);
    e * e * (3.0 - 2.0 * e)
}

/// A running box sum of 2h + 1 samples along each row of v (nx x ny), in place (outside the grid: 0).
fn box_rows(v: &mut [f64], nx: usize, ny: usize, h: usize, line: &mut Vec<f64>) {
    for r in 0..ny {
        let row = &mut v[r * nx..(r + 1) * nx];
        if row.iter().all(|&x| x == 0.0) { continue; }
        line.clear();
        line.extend_from_slice(row);
        let mut acc: f64 = line[..h.min(nx)].iter().sum();
        for c in 0..nx {
            if c + h < nx { acc += line[c + h]; }
            if c > h { acc -= line[c - h - 1]; }
            row[c] = acc;
        }
    }
}

/// box_rows along each column (a sweep down the rows with a running sum per column: contiguous reads).
fn box_cols(v: &mut [f64], nx: usize, ny: usize, h: usize, line: &mut Vec<f64>) {
    line.clear();
    line.extend_from_slice(v);
    let mut acc = vec![0.0f64; nx];
    for r in 0..h.min(ny) {
        for c in 0..nx { acc[c] += line[r * nx + c]; }
    }
    for r in 0..ny {
        if r + h < ny {
            let add = &line[(r + h) * nx..(r + h + 1) * nx];
            for c in 0..nx { acc[c] += add[c]; }
        }
        if r > h {
            let sub = &line[(r - h - 1) * nx..(r - h) * nx];
            for c in 0..nx { acc[c] -= sub[c]; }
        }
        v[r * nx..(r + 1) * nx].copy_from_slice(&acc);
    }
}

/// The profile height at arc position s (linear between stations; NaN where both neighbours are).
fn at(st: &[f64], prof: &[f64], s: f64) -> f64 {
    let pos = st.partition_point(|&v| v <= s);
    if pos == 0 { return prof[0]; }
    if pos >= st.len() { return prof[st.len() - 1]; }
    let (s0, s1, h0, h1) = (st[pos - 1], st[pos], prof[pos - 1], prof[pos]);
    if !h0.is_finite() { return h1; }
    if !h1.is_finite() { return h0; }
    let f = if s1 > s0 { (s - s0) / (s1 - s0) } else { 0.0 };
    h0 + f * (h1 - h0)
}

/// The point at arc position s of polyline p (cum: its vertices' arc positions).
fn point_at(p: &[[f64; 2]], cum: &[f64], s: f64) -> (f64, f64) {
    let i = cum.partition_point(|&v| v <= s).clamp(1, p.len() - 1) - 1;
    let l = cum[i + 1] - cum[i];
    let t = if l > 1e-12 { ((s - cum[i]) / l).clamp(0.0, 1.0) } else { 0.0 };
    (p[i][0] + t * (p[i + 1][0] - p[i][0]), p[i][1] + t * (p[i + 1][1] - p[i][1]))
}

/// (distance, arc position of the foot) from (x, y) to polyline p (cum: its vertices' arc positions).
fn nearest(p: &[[f64; 2]], cum: &[f64], x: f64, y: f64) -> (f64, f64) {
    let mut best = (f64::MAX, 0.0);
    for i in 0..p.len() - 1 {
        let (ax, ay, dx, dy) = (p[i][0], p[i][1], p[i + 1][0] - p[i][0], p[i + 1][1] - p[i][1]);
        let l2 = dx * dx + dy * dy;
        let t = if l2 > 1e-12 { (((x - ax) * dx + (y - ay) * dy) / l2).clamp(0.0, 1.0) } else { 0.0 };
        let d = ((x - ax - t * dx).powi(2) + (y - ay - t * dy).powi(2)).sqrt();
        if d < best.0 { best = (d, cum[i] + t * (cum[i + 1] - cum[i])); }
    }
    best
}

/// u (ny, nx) [row = y index, col = x index]: heights of the lattice x0 + sq * col, y0 + sq * row, changed in place.
/// Roads: polyline k is pts[starts[k] .. starts[k + 1]] (m, 2) in the same plane; core[k] its flat half width (m),
/// sigma[k] / median_m[k] its profile smoothing (m), rank[k] its class rank. A road ending within join_reach m of a
/// higher rank's core, or of an equal rank's away from that one's ends (a T junction), takes that road's height there
/// (when they differ by at most join_max_dz m), eased in along it at join_grade over join_ease_min .. join_taper m (the
/// grading's junction snap, grading.JUNCTION_*); the higher ranks are laid over the lower by their ease (a main road
/// keeps its height across a side road's mouth), equal ranks blend by 1 / (1 + (d / blend_r)^blend_pow) (grading.BLEND_R).
/// t_min / t_max / slope_run: the ease back to the ground beyond the core, slope_run x the height change, between t_min
/// and t_max m. step: m between profile stations. blur_m: the targets rounded over this (a Gaussian's sigma, m; 0: not). Returns (samples changed, the largest |change| m, ends joined).
#[pyfunction]
#[pyo3(signature = (u, x0, y0, sq, pts, starts, core, sigma, median_m, rank, t_min, t_max, slope_run, step,
                    join_reach, join_max_dz, join_grade, join_taper, join_ease_min, blend_r, blend_pow, blur_m))]
#[allow(clippy::too_many_arguments)]
pub fn l0_flatten(py: Python<'_>, mut u: PyReadwriteArray2<'_, f64>, x0: f64, y0: f64, sq: f64,
                  pts: PyReadonlyArray2<'_, f64>, starts: PyReadonlyArray1<'_, i64>, core: PyReadonlyArray1<'_, f64>,
                  sigma: PyReadonlyArray1<'_, f64>, median_m: PyReadonlyArray1<'_, f64>,
                  rank: PyReadonlyArray1<'_, f64>, t_min: f64, t_max: f64, slope_run: f64, step: f64,
                  join_reach: f64, join_max_dz: f64, join_grade: f64, join_taper: f64, join_ease_min: f64,
                  blend_r: f64, blend_pow: f64, blur_m: f64)
                  -> PyResult<(usize, f64, usize)> {
    let pts = pts.as_array();
    let starts = starts.as_array();
    let (core, sigma, median_m, rank) = (core.as_array(), sigma.as_array(), median_m.as_array(), rank.as_array());
    let nk = starts.len().saturating_sub(1);
    if core.len() < nk || sigma.len() < nk || median_m.len() < nk || rank.len() < nk || pts.shape()[1] < 2 {
        return Err(pyo3::exceptions::PyValueError::new_err("l0_flatten: array lengths disagree"));
    }
    if !(sq > 0.0) || !(step > 0.0) {
        return Err(pyo3::exceptions::PyValueError::new_err("l0_flatten: sq and step must be > 0"));
    }
    let mut uv = u.as_array_mut();
    let polys: Vec<Vec<[f64; 2]>> = (0..nk).map(|k| {
        let (a, b) = (starts[k].max(0) as usize, (starts[k + 1].max(0) as usize).min(pts.shape()[0]));
        (a..b).map(|i| [pts[[i, 0]], pts[[i, 1]]]).collect()
    }).collect();
    let core: Vec<f64> = core.iter().copied().collect();
    let sigma: Vec<f64> = sigma.iter().copied().collect();
    let median_m: Vec<f64> = median_m.iter().copied().collect();
    let rank: Vec<f64> = rank.iter().copied().collect();
    Ok(py.detach(move || {
        let (ny, nx) = (uv.shape()[0], uv.shape()[1]);
        let orig = uv.to_owned();
        let ov = orig.view();
        let gx_max = (nx - 1) as f64 * sq + x0;
        let gy_max = (ny - 1) as f64 * sq + y0;
        // the roads that reach the grid, their profiles and vertices' arc positions
        let mut live: Vec<usize> = Vec::new();
        let mut boxes = vec![[0.0f64; 4]; nk];
        let mut cums: Vec<Vec<f64>> = vec![Vec::new(); nk];
        let mut profs: Vec<(Vec<f64>, Vec<f64>)> = vec![(Vec::new(), Vec::new()); nk];
        for k in 0..nk {
            let p = &polys[k];
            if p.len() < 2 || !(core[k] > 0.0) { continue; }
            let reach = core[k] + t_max;
            let (mut bx0, mut by0, mut bx1, mut by1) = (f64::MAX, f64::MAX, f64::MIN, f64::MIN);
            for q in p { bx0 = bx0.min(q[0]); by0 = by0.min(q[1]); bx1 = bx1.max(q[0]); by1 = by1.max(q[1]); }
            if bx1 + reach < x0 || by1 + reach < y0 || bx0 - reach > gx_max || by0 - reach > gy_max { continue; }
            boxes[k] = [bx0, by0, bx1, by1];
            let mut cum = vec![0.0f64; p.len()];
            for i in 1..p.len() {
                cum[i] = cum[i - 1] + ((p[i][0] - p[i - 1][0]).powi(2) + (p[i][1] - p[i - 1][1]).powi(2)).sqrt();
            }
            cums[k] = cum;
            profs[k] = profile(&ov, p, x0, y0, sq, step, median_m[k], sigma[k]);
            live.push(k);
        }
        // junctions: each end of a road in a higher rank's core takes its height (the highest ranks first, so the
        // height taken is that road's final one). Its stations inside that core lie on the other road's flat
        // carriageway (the overlay below lays it there anyway): they take its height; from the first one outside (the
        // junction's edge) the road eases from that height back to its own profile, at join_grade over join_taper m at
        // most. Measured at the edge, not at the end: a steep side road's first metres flattened by the main road's
        // core and its own grade beyond made a 20 % ramp at the core's edge (Genoa, 2026-11-11).
        let mut by_rank = live.clone();
        by_rank.sort_by(|&a, &b| rank[b].partial_cmp(&rank[a]).unwrap_or(std::cmp::Ordering::Equal).then(a.cmp(&b)));
        let mut joined = 0usize;
        for &k in &by_rank {
            let total = *cums[k].last().unwrap();
            if total <= 1e-6 { continue; }
            for end in 0..2 {
                let q = if end == 0 { polys[k][0] } else { polys[k][polys[k].len() - 1] };
                let mut best: Option<(f64, f64, usize, f64)> = None;    // (rank, distance, road, its height there)
                for &j in &live {
                    if j == k || rank[j] < rank[k] { continue; }
                    let r = core[j] + join_reach;
                    let b = boxes[j];
                    if q[0] < b[0] - r || q[0] > b[2] + r || q[1] < b[1] - r || q[1] > b[3] + r { continue; }
                    let (d, s) = nearest(&polys[j], &cums[j], q[0], q[1]);
                    if d > r { continue; }
                    // (an equal rank only where this road ends on its side: not where the two meet end to end, one
                    // street going on as the next or the arms of a crossing, whose ends share their height already)
                    let tj = *cums[j].last().unwrap();
                    if rank[j] == rank[k] && (s < r || s > tj - r) { continue; }
                    let h = at(&profs[j].0, &profs[j].1, s);
                    if !h.is_finite() { continue; }
                    if best.map_or(true, |(br, bd, _, _)| rank[j] > br || (rank[j] == br && d < bd)) {
                        best = Some((rank[j], d, j, h));
                    }
                }
                let Some((_, _, j, h_end)) = best else { continue };
                // (another level where they meet: an end under a road, a driveway's ramp, bad data; measured at the
                // end itself, where they meet: at the core's edge a steep side road is already its grade x the core
                // above the main road, 1.6 m up an 18 % street in Pittsburgh)
                let own_end = at(&profs[k].0, &profs[k].1, if end == 0 { 0.0 } else { total });
                if !own_end.is_finite() || (h_end - own_end).abs() > join_max_dz { continue; }
                let (stj, prj) = (profs[j].0.clone(), profs[j].1.clone());
                let st = profs[k].0.clone();
                let n = st.len();
                let cap = 3.0 * core[j] + join_reach;       // (still inside past this: beside it, not joining it)
                let mut inside: Vec<(usize, f64)> = Vec::new();
                let mut edge: Option<(f64, f64, f64)> = None;    // (arc from this end, the other's height there, own)
                let mut last: Option<(f64, f64)> = None;          // (arc from this end, the other's height) inside
                for m in 0..n {
                    let i = if end == 0 { m } else { n - 1 - m };
                    let from_end = if end == 0 { st[i] } else { total - st[i] };
                    if from_end > cap { break; }
                    let (x, y) = point_at(&polys[k], &cums[k], st[i]);
                    let (d, sj) = nearest(&polys[j], &cums[j], x, y);
                    let hj = at(&stj, &prj, sj);
                    if !hj.is_finite() { break; }
                    if d <= core[j] {
                        inside.push((i, hj));
                        last = Some((from_end, hj));
                        continue;
                    }
                    let own = profs[k].1[i];
                    if own.is_finite() { edge = Some((from_end, hj, own)); }
                    break;
                }
                let Some((fe, he, own_e)) = edge else { continue };
                let dz = he - own_e;
                // past the edge the other road's flat carriageway carried on level, eased into this road's own
                // profile (its grade too, not only its height); the other road's grade along this one taken from the
                // stations inside overshot where the nearest point on it jumped between its vertices
                let _ = last;
                let prof = &mut profs[k].1;
                for &(i, hj) in &inside { prof[i] = hj; }
                let len = (dz.abs() / join_grade.max(1e-3)).max(join_ease_min).clamp(step, join_taper)
                    .min(((total - fe) * 0.5).max(step));
                for i in 0..n {
                    let from_end = if end == 0 { st[i] } else { total - st[i] };
                    if from_end < fe || !prof[i].is_finite() { continue; }
                    let t = (from_end - fe) / len;
                    if t >= 1.0 { continue; }
                    prof[i] += (he - prof[i]) * (1.0 - smoothstep(t));
                }
                joined += 1;
            }
        }
        // the corridors raster, the roads by rank, lowest first: each rank's roads blended among themselves (weights
        // a^2), then laid over what the lower ranks made by their ease a
        let mut order = live.clone();
        order.sort_by(|&a, &b| rank[a].partial_cmp(&rank[b]).unwrap_or(std::cmp::Ordering::Equal).then(a.cmp(&b)));
        let mut sw = vec![0.0f64; nx * ny];
        let mut swh = vec![0.0f64; nx * ny];
        let mut amax = vec![0.0f32; nx * ny];
        let mut tgt = vec![0.0f64; nx * ny];       // the target height so far
        let mut alpha = vec![0.0f32; nx * ny];     // how much of it the ground takes (0: untouched)
        let mut touched: Vec<usize> = Vec::new();
        let mut g = 0usize;
        while g < order.len() {
            let mut g_end = g;
            while g_end < order.len() && rank[order[g_end]] == rank[order[g]] { g_end += 1; }
            touched.clear();
            for &k in &order[g..g_end] {
                let p = &polys[k];
                let reach = core[k] + t_max;
                let (st, prof) = (&profs[k].0, &profs[k].1);
                let cum = &cums[k];
                for i in 0..p.len() - 1 {
                    let (ax, ay, bx, by) = (p[i][0], p[i][1], p[i + 1][0], p[i + 1][1]);
                    let (dx, dy) = (bx - ax, by - ay);
                    let l2 = dx * dx + dy * dy;
                    let (sa, l) = (cum[i], cum[i + 1] - cum[i]);
                    let cmin = (((ax.min(bx) - reach - x0) / sq).floor() as i64).max(0);
                    let cmax = (((ax.max(bx) + reach - x0) / sq).ceil() as i64).min(nx as i64 - 1);
                    let rmin = (((ay.min(by) - reach - y0) / sq).floor() as i64).max(0);
                    let rmax = (((ay.max(by) + reach - y0) / sq).ceil() as i64).min(ny as i64 - 1);
                    if cmin > cmax || rmin > rmax { continue; }
                    for r in rmin..=rmax {
                        let py_ = y0 + r as f64 * sq;
                        for c in cmin..=cmax {
                            let px = x0 + c as f64 * sq;
                            let (qx, qy) = (px - ax, py_ - ay);
                            let t = if l2 > 1e-12 { ((qx * dx + qy * dy) / l2).clamp(0.0, 1.0) } else { 0.0 };
                            let (ex, ey) = (qx - t * dx, qy - t * dy);
                            let d2 = ex * ex + ey * ey;
                            if d2 > reach * reach { continue; }
                            let d = d2.sqrt();
                            let h = at(st, prof, sa + t * l);    // the profile at the foot of the perpendicular
                            if !h.is_finite() { continue; }
                            let idx = r as usize * nx + c as usize;
                            let z0 = ov[[r as usize, c as usize]];
                            let a = if d <= core[k] { 1.0 } else {
                                let tw = (slope_run * (h - z0).abs()).clamp(t_min, t_max);
                                let e = (d - core[k]) / tw;
                                if e >= 1.0 { continue; }
                                1.0 - smoothstep(e)
                            };
                            if sw[idx] == 0.0 { touched.push(idx); }
                            let w = (a * a / (1.0 + (d / blend_r).powf(blend_pow))).max(1e-12);
                            sw[idx] += w;
                            swh[idx] += w * h;
                            if a as f32 > amax[idx] { amax[idx] = a as f32; }
                        }
                    }
                }
            }
            for &idx in &touched {
                let h = swh[idx] / sw[idx];
                let a = amax[idx];
                if alpha[idx] <= 0.0 {
                    tgt[idx] = h;
                    alpha[idx] = a;
                } else {
                    tgt[idx] += a as f64 * (h - tgt[idx]);
                    if a > alpha[idx] { alpha[idx] = a; }
                }
                sw[idx] = 0.0;
                swh[idx] = 0.0;
                amax[idx] = 0.0;
            }
            g = g_end;
        }
        // the target and its share rounded over blur_m (a Gaussian, normalised by the share): the rank overlays and
        // junction edges left kinks in the carriageways (a grade change in a metre), now a curve over a few metres;
        // a flat cross-section stays flat and the ground's own bumps do not come back (only the target is blurred)
        let mut tgt = tgt;
        if blur_m > 0.0 {
            // three box passes of 2h + 1 samples a way (~ a Gaussian of blur_m, O(1) a sample whatever its width)
            let h = (((4.0 * (blur_m / sq).powi(2) + 1.0).sqrt() - 1.0) / 2.0).round().max(1.0) as usize;
            let mut at_ = vec![0.0f64; nx * ny];     // alpha * target
            let mut a_ = vec![0.0f64; nx * ny];      // alpha
            for idx in 0..nx * ny {
                if alpha[idx] > 0.0 { a_[idx] = alpha[idx] as f64; at_[idx] = a_[idx] * tgt[idx]; }
            }
            let mut line = Vec::new();
            for _ in 0..3 {
                box_rows(&mut at_, nx, ny, h, &mut line);
                box_rows(&mut a_, nx, ny, h, &mut line);
                box_cols(&mut at_, nx, ny, h, &mut line);
                box_cols(&mut a_, nx, ny, h, &mut line);
            }
            // (only where the roads reach, the share itself unblurred: blurred, it would spread the flattening past
            // the ease)
            for idx in 0..nx * ny {
                if alpha[idx] > 0.0 && a_[idx] > 1e-12 { tgt[idx] = at_[idx] / a_[idx]; }
            }
        }
        let mut n = 0usize;
        let mut dmax = 0.0f64;
        for r in 0..ny {
            for c in 0..nx {
                let idx = r * nx + c;
                if alpha[idx] <= 0.0 { continue; }
                let z0 = ov[[r, c]];
                let dz = alpha[idx] as f64 * (tgt[idx] - z0);
                if dz != 0.0 {
                    uv[[r, c]] = z0 + dz;
                    n += 1;
                    dmax = dmax.max(dz.abs());
                }
            }
        }
        (n, dmax, joined)
    }))
}
